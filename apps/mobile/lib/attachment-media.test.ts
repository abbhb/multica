import { currentSessionEpoch, invalidateSessionEpoch } from "@/data/session-epoch";
import { describe, expect, it } from "vitest";
import type { Attachment } from "@multica/core/types";
import { attachmentMedia, attachmentMediaURL, attachmentMediaSequence, attachmentURLExpiry, attachmentCapabilityDeadline } from "./attachment-media";
import { collectImageSequence } from "@multica/core/attachments/image-sequence";

const id = "12345678-1234-1234-1234-123456789abc";
const attachment = {
  id,
  url: "https://api.test/uploads/workspaces/ws/old.png",
  markdown_url: `/api/attachments/${id}/download`,
  download_url: `/api/attachments/${id}/download`,
  filename: "old.png",
  content_type: "image/png",
} as Attachment;
const media = attachmentMedia(attachment.url, [attachment]);

describe("native attachment media", () => {
  it("matches legacy raw uploads and new stable links to the same attachment", () => {
    expect(media.attachmentId).toBe(id);
    expect(attachmentMedia(attachment.markdown_url, [attachment])).toEqual(media);
    expect(attachmentMedia(`mc://file/${id}`, [{ ...attachment, url: `mc://file/${id}` }]).attachmentId).toBe(id);
  });
  it("resolves stable IDs without a list, but never guesses IDs from storage names", () => {
    expect(attachmentMedia(`/api/attachments/${id}/download`).attachmentId).toBe(id);
    expect(attachmentMedia(`mc://file/${id}`).attachmentId).toBe(id);
    expect(attachmentMedia(`/uploads/${id}.png`).attachmentId).toBeUndefined();
    expect(attachmentMedia("/api/attachments/not-an-id/download").attachmentId).toBeUndefined();
  });
  it("preserves external images and unresolved old links without granting access", () => {
    const external = attachmentMedia("https://other.test/image.png", [attachment]);
    expect(attachmentMediaURL(external)).toBe("https://other.test/image.png");
    expect(attachmentMedia("/uploads/unknown.png").attachmentId).toBeUndefined();
  });
  it("never passes an auth-gated list URL to the native loader before refresh", () => {
    expect(attachmentMediaURL(media)).toBeUndefined();
    expect(attachmentMediaURL(media, { attachment: null, fetchedAt: 0, sessionEpoch: currentSessionEpoch() })).toBeUndefined();
  });
  it("uses signed URLs until the refresh safety margin, never an expired URL", () => {
    const capability = { attachment: { ...attachment, download_url: "https://api.test/api/attachments/x/signed-download?exp=60&sig=test" }, fetchedAt: 0, sessionEpoch: currentSessionEpoch() };
    expect(attachmentMediaURL(media, capability, 30_000)).toContain("signed-download");
    expect(attachmentMediaURL(media, capability, 55_000)).toBeUndefined();
    expect(attachmentMediaURL(media, capability, 61_000)).toBeUndefined();
    expect(attachmentMediaURL(media, { ...capability, fetchedAt: 59_000 }, 59_000)).toBeUndefined();
  });
  it("rejects a mismatched attachment response", () => {
    expect(attachmentMediaURL(media, { attachment: { ...attachment, id: "another" }, fetchedAt: 0, sessionEpoch: currentSessionEpoch() }, 1)).toBeUndefined();
  });
  it("uses actual CloudFront/AWS expiration, including URLs arriving near expiry", () => {
    const prefix = "https://bucket.test/uploads/file?X-Amz-Date=20261103T055600Z&X-Amz-Expires=20&X-Amz-Signature=x";
    const issuedAt = Date.parse("2026-11-03T05:56:00Z");
    const capability = { attachment: { ...attachment, download_url: prefix }, fetchedAt: issuedAt + 12_000, sessionEpoch: currentSessionEpoch() };
    expect(attachmentURLExpiry(prefix)).toBe(issuedAt + 20_000);
    expect(attachmentCapabilityDeadline(capability)).toBe(issuedAt + 15_000);
    expect(attachmentMediaURL(media, capability, issuedAt + 14_000)).toBe(prefix);
    expect(attachmentMediaURL(media, capability, issuedAt + 15_000)).toBeUndefined();
    expect(attachmentURLExpiry("https://cdn.test/file?Expires=20&Signature=x")).toBe(20_000);
    expect(attachmentURLExpiry("https://bucket.test/file?X-Amz-Date=invalid&X-Amz-Expires=20")).toBe(0);
  });
  it("decodes real CloudFront custom policies and fails closed on malformed policies", () => {
    const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString("base64").replace(/\+/g, "-").replace(/=/g, "_").replace(/\//g, "~");
    const policy = { Statement: [
      { Resource: "https://cdn.test/uploads/file.png", Condition: { DateLessThan: { "AWS:EpochTime": 60 } } },
      { Resource: "https://cdn.test/uploads/*", Condition: { DateLessThan: { "AWS:EpochTime": 20 } } },
    ] };
    const url = `https://cdn.test/uploads/file.png?Policy=${encode(policy)}&Signature=signature&Key-Pair-Id=key`;
    expect(attachmentURLExpiry(url)).toBe(20_000);
    const capability = { attachment: { ...attachment, download_url: url }, fetchedAt: 0, sessionEpoch: currentSessionEpoch() };
    expect(attachmentMediaURL(media, capability, 14_000, "https://api.test")).toBe(url);
    expect(attachmentMediaURL(media, capability, 15_000, "https://api.test")).toBeUndefined();
    for (const invalid of ["broken", encode({ Statement: [] }), encode({ Statement: [{ Condition: {} }] }), encode({ Statement: [{ Condition: { DateLessThan: { "AWS:EpochTime": "20" } } }] })]) {
      expect(attachmentURLExpiry(`https://cdn.test/file?Policy=${invalid}&Expires=9999999999&Signature=x`)).toBe(0);
    }
  });
  it("rejects authenticated fallback URLs from unsuccessful signing", () => {
    for (const download_url of [`/api/attachments/${id}/download`, "https://api.test/uploads/private.png"]) {
      expect(attachmentMediaURL(media, { attachment: { ...attachment, download_url }, fetchedAt: 0, sessionEpoch: currentSessionEpoch() }, 1, "https://api.test")).toBeUndefined();
    }
  });
  it("preserves external CDN /uploads URLs while rejecting same-origin raw uploads", () => {
    const response = (download_url: string) => ({ attachment: { ...attachment, download_url }, fetchedAt: 0, sessionEpoch: currentSessionEpoch() });
    for (const url of ["https://cdn.test/uploads/public.png", "https://cdn.test/uploads/file?Expires=60&Signature=x"]) {
      expect(attachmentMediaURL(media, response(url), 1, "https://api.test")).toBe(url);
    }
    expect(attachmentMediaURL(media, response("/uploads/private.png"), 1, "https://api.test")).toBeUndefined();
    expect(attachmentMediaURL(media, response("https://api.test/uploads/private.png?sig=not-a-capability"), 1, "https://api.test")).toBeUndefined();
  });
  it("rejects cached capabilities immediately after a session change", () => {
    const capability = { attachment: { ...attachment, download_url: "https://cdn.test/file?sig=x" }, fetchedAt: 0, sessionEpoch: currentSessionEpoch() };
    invalidateSessionEpoch();
    expect(attachmentMediaURL(media, capability, 1)).toBeUndefined();
  });
  it("preserves gallery order and clicked position across capability rotation", () => {
    const items = collectImageSequence([{ content: `![external](https://other.test/img.png)\n![old](${attachment.url})`, attachments: [attachment] }]);
    const sequence = items.map((item) => attachmentMedia(item.url, item.attachment ? [item.attachment] : undefined));
    const viewing = attachmentMediaSequence({ ...media, uri: "https://api.test/new-signature" }, sequence);
    expect(viewing.index).toBe(1);
    expect(viewing.images).toHaveLength(2);
    sequence.pop();
    expect(viewing.images).toHaveLength(2);
    expect(attachmentMediaSequence({ uri: "file:///local.png" }, sequence)).toEqual({ images: [{ uri: "file:///local.png" }], index: 0 });
  });
});
