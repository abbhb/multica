import type { Attachment } from "@multica/core/types";
import { matchAttachmentByURL } from "@multica/core/attachments/image-sequence";
import { attachmentIdFromDownloadURL } from "@multica/core/types/attachment-url";
import { currentSessionEpoch } from "@/data/session-epoch";
import { resolveAttachmentUrl, resolveAttachmentUrlWithBase } from "./attachment-url";

/** Stable identity is kept separately from the expiring transport URL. */
export interface AttachmentMedia {
  uri: string;
  attachmentId?: string;
}

export function attachmentMedia(
  uri: string,
  attachments?: readonly Attachment[],
): AttachmentMedia {
  const match = matchAttachmentByURL(uri, attachments);
  const candidate = match ? match.download_url || match.markdown_url || match.url : uri;
  // Stable ID links can resolve even before a surrounding attachment list is
  // available. They are only a lookup hint: the app API authorizes the ID.
  // Never derive an ID from a raw storage filename or send auth to its host.
  const id = match?.id ?? attachmentIdFromDownloadURL(uri)
    ?? /^mc:\/\/file\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i.exec(uri)?.[1];
  return { uri: resolveAttachmentUrl(candidate) ?? uri, attachmentId: id };
}

export interface AttachmentCapability {
  attachment: Attachment | null;
  fetchedAt: number;
  sessionEpoch: number;
}

/** Never fall back to an auth-gated list URL or an expired capability. */
export function attachmentMediaURL(
  media: AttachmentMedia,
  capability?: AttachmentCapability,
  now = Date.now(),
  apiBase = resolveAttachmentUrl("/") ?? "",
): string | undefined {
  if (!media.attachmentId) return media.uri;
  if (!capability?.attachment || capability.attachment.id !== media.attachmentId) {
    return undefined;
  }
  if (capability.sessionEpoch !== currentSessionEpoch()) return undefined;
  // A newly arrived response may be newer than the timer-backed render clock.
  now = Math.max(now, capability.fetchedAt);
  const rawURL = capability.attachment.download_url;
  const url = resolveAttachmentUrlWithBase(rawURL, apiBase);
  if (!url || !/^https?:\/\//i.test(url)) return undefined;
  // A signing failure can return a stable authenticated endpoint. It is
  // never a capability, and native loaders cannot supply the Bearer token.
  let parsed: URL;
  try { parsed = new URL(url); }
  catch { return undefined; }
  let sameAPIOrigin = rawURL.startsWith("/");
  try { sameAPIOrigin ||= parsed.origin === new URL(apiBase).origin; }
  catch { /* An unset API base cannot identify an absolute API origin. */ }
  const pathname = parsed.pathname;
  const rawUploads = sameAPIOrigin && (pathname === "/uploads" || pathname.startsWith("/uploads/"));
  if (/^\/api\/attachments\/[^/]+\/download$/.test(pathname) || rawUploads) {
    return undefined;
  }
  if (now >= attachmentCapabilityDeadline(capability)) return undefined;
  return url;
}

/** CloudFront uses + → -, = → _, / → ~, not ordinary base64url. */
function cloudFrontPolicyExpiry(encoded: string): number {
  const base64 = encoded.replace(/-/g, "+").replace(/_/g, "=").replace(/~/g, "/");
  if (base64.length > 16_384 || base64.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(base64)) return 0;
  // Hermes does not consistently provide atob; decode the bounded policy
  // without a web/Node dependency. Policy keys and numeric dates are ASCII.
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  let bits = 0;
  let value = 0;
  let decoded = "";
  for (const char of base64.replace(/=+$/, "")) {
    value = (value << 6) | alphabet.indexOf(char);
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      decoded += String.fromCharCode((value >> bits) & 255);
    }
  }
  try {
    const policy = JSON.parse(decoded);
    if (!Array.isArray(policy?.Statement) || policy.Statement.length === 0) return 0;
    const deadlines: number[] = [];
    for (const statement of policy.Statement) {
      const epoch = statement?.Condition?.DateLessThan?.["AWS:EpochTime"];
      if (typeof epoch !== "number" || !Number.isSafeInteger(epoch) || epoch < 0) return 0;
      deadlines.push(epoch * 1000);
    }
    return Math.min(...deadlines);
  } catch { return 0; }
}

/** Read the absolute expiry used by proxy, CloudFront, and AWS/GCS signing. */
export function attachmentURLExpiry(rawURL: string): number | undefined {
  let params: URLSearchParams;
  try { params = new URL(rawURL, "https://relative.invalid").searchParams; }
  catch { return undefined; }
  const policy = params.get("Policy");
  if (policy !== null) return cloudFrontPolicyExpiry(policy);
  const epoch = params.get("exp") ?? params.get("Expires");
  if (epoch !== null) {
    const seconds = Number(epoch);
    return /^\d+$/.test(epoch) && Number.isFinite(seconds) ? seconds * 1000 : 0;
  }
  for (const prefix of ["X-Amz", "X-Goog"]) {
    const date = params.get(`${prefix}-Date`);
    const duration = params.get(`${prefix}-Expires`);
    if (date === null && duration === null) continue;
    if (!date || !duration || !/^\d+$/.test(duration)) return 0;
    const parts = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(date);
    if (!parts) return 0;
    const issuedAt = Date.parse(`${parts[1]}-${parts[2]}-${parts[3]}T${parts[4]}:${parts[5]}:${parts[6]}Z`);
    const expiresAt = issuedAt + Number(duration) * 1000;
    return Number.isFinite(expiresAt) ? expiresAt : 0;
  }
  return undefined;
}

/** Deadline leaves five seconds for a native request to reach the server. */
export function attachmentCapabilityDeadline(capability: AttachmentCapability): number {
  const url = capability.attachment?.download_url ?? "";
  const proxy = url.includes("/signed-download?");
  const signed = /[?&](?:Signature|Policy|X-Amz-Signature|X-Goog-Signature|sig)=/i.test(url);
  const ageDeadline = proxy ? capability.fetchedAt + 55_000
    : signed ? capability.fetchedAt + 20 * 60_000 : Infinity;
  const expiresAt = attachmentURLExpiry(url);
  return expiresAt === undefined ? ageDeadline : Math.min(ageDeadline, expiresAt - 5_000);
}

/** Freeze membership/order at open; signing later must not alter the index. */
export function attachmentMediaSequence(
  media: AttachmentMedia,
  sequence?: readonly AttachmentMedia[],
) {
  const images = sequence?.length ? [...sequence] : [media];
  const index = images.findIndex((item) => media.attachmentId
    ? item.attachmentId === media.attachmentId
    : item.uri === media.uri);
  return index < 0 ? { images: [media], index: 0 } : { images, index };
}
