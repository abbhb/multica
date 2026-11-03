import { queryOptions } from "@tanstack/react-query";
import { api } from "@/data/api";
import { currentSessionEpoch, sessionEpochChanged } from "@/data/session-epoch";
import { attachmentMediaURL, attachmentCapabilityDeadline, type AttachmentCapability } from "@/lib/attachment-media";

export const attachmentKeys = {
  capability: (wsId: string | null, id: string | undefined, userId: string | null, epoch: number) =>
    ["attachment-capability", wsId, id, userId, epoch] as const,
};

/** Only expiring transports need polling. Public CDN URLs stay unchanged. */
export function capabilityRefreshInterval(data?: AttachmentCapability, now = Date.now()): number | false {
  if (!data) return false;
  const deadline = attachmentCapabilityDeadline(data);
  if (!Number.isFinite(deadline)) return false;
  const remaining = deadline - now;
  // A failed signer/expired response must not cause a tight request loop.
  if (remaining <= 0) return 30_000;
  const proxy = data.attachment?.download_url.includes("/signed-download?");
  const ceiling = proxy ? 30_000 : 15 * 60_000;
  // Re-sign well before the native source is cleared, including short TTLs.
  return Math.max(1000, Math.min(ceiling, remaining - Math.min(10_000, remaining / 2)));
}

export function attachmentCapabilityOptions(wsId: string | null, id: string | undefined, userId: string | null) {
  const epoch = currentSessionEpoch();
  return queryOptions({
    queryKey: attachmentKeys.capability(wsId, id, userId, epoch),
    queryFn: async ({ signal }): Promise<AttachmentCapability> => {
      if (sessionEpochChanged(epoch)) throw new Error("Attachment session changed");
      const attachment = await api.getAttachmentForDownload(id!, { signal });
      if (sessionEpochChanged(epoch)) throw new Error("Attachment session changed");
      return { attachment, fetchedAt: Date.now(), sessionEpoch: epoch };
    },
    enabled: !!wsId && !!id && !!userId,
    staleTime: (query) => capabilityRefreshInterval(query.state.data, query.state.data?.fetchedAt) || 30_000,
    refetchInterval: (query) => capabilityRefreshInterval(query.state.data),
    // AppState's focusManager pauses polling in the background.
    refetchIntervalInBackground: false,
    gcTime: 60_000,
    retry: false,
  });
}

/** Fetch on each tap and check the session immediately before native handoff. */
export async function openAttachment(id: string, openURL: (url: string) => Promise<unknown>): Promise<void> {
  const epoch = currentSessionEpoch();
  const attachment = await api.getAttachmentForDownload(id);
  if (sessionEpochChanged(epoch)) return;
  const target = attachmentMediaURL(
    { uri: "", attachmentId: id },
    { attachment, fetchedAt: Date.now(), sessionEpoch: epoch },
  );
  if (target) await openURL(target);
}
