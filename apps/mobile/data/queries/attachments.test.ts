import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import { api } from "@/data/api";
import { currentSessionEpoch, invalidateSessionEpoch } from "@/data/session-epoch";
import { attachmentCapabilityOptions, capabilityRefreshInterval, openAttachment } from "./attachments";

vi.mock("@/data/api", () => ({ api: { getAttachmentForDownload: vi.fn() } }));
afterEach(() => vi.clearAllMocks());
const signed = { id: "a", download_url: "https://cdn.test/file?sig=fresh" } as never;

describe("mobile attachment capabilities", () => {
  it("scopes the cache to workspace, user, and credential epoch", () => {
    const key = attachmentCapabilityOptions("ws", "a", "user").queryKey;
    expect(key).not.toEqual(attachmentCapabilityOptions("other", "a", "user").queryKey);
    expect(key).not.toEqual(attachmentCapabilityOptions("ws", "a", "other").queryKey);
    invalidateSessionEpoch();
    expect(key).not.toEqual(attachmentCapabilityOptions("ws", "a", "user").queryKey);
    expect(attachmentCapabilityOptions("ws", undefined, "user").enabled).toBe(false);
    expect(attachmentCapabilityOptions(null, "a", "user").enabled).toBe(false);
    expect(attachmentCapabilityOptions("ws", "a", null).enabled).toBe(false);
    expect(attachmentCapabilityOptions("ws", "a", "user").refetchIntervalInBackground).toBe(false);
  });
  it("polls only expiring URLs, using a shorter interval for proxy capabilities", () => {
    const data = (download_url: string) => ({ attachment: { id: "a", download_url } as never, fetchedAt: 0, sessionEpoch: currentSessionEpoch() });
    expect(capabilityRefreshInterval(data("/api/attachments/a/signed-download?sig=x"), 0)).toBe(30_000);
    expect(capabilityRefreshInterval(data("https://cdn.test/file?Signature=x"), 0)).toBe(15 * 60_000);
    expect(capabilityRefreshInterval(data("https://cdn.test/public.png"))).toBe(false);
    expect(capabilityRefreshInterval()).toBe(false);
  });
  it("refreshes short CloudFront and AWS TTLs before the source expires", () => {
    const signedData = (download_url: string) => ({ attachment: { id: "a", download_url } as never, fetchedAt: 0, sessionEpoch: currentSessionEpoch() });
    const cloudfront = signedData("https://cdn.test/file?Expires=20&Signature=x");
    const aws = signedData("https://bucket.test/file?X-Amz-Date=19700101T000000Z&X-Amz-Expires=20&X-Amz-Signature=x");
    expect(capabilityRefreshInterval(cloudfront, 0)).toBe(7500);
    expect(capabilityRefreshInterval(aws, 0)).toBe(7500);
    expect(capabilityRefreshInterval(aws, 10_000)).toBe(2500);
    expect(capabilityRefreshInterval(aws, 16_000)).toBe(30_000);
    const policy = Buffer.from(JSON.stringify({ Statement: [{ Resource: "https://cdn.test/file", Condition: { DateLessThan: { "AWS:EpochTime": 20 } } }] })).toString("base64").replace(/\+/g, "-").replace(/=/g, "_").replace(/\//g, "~");
    expect(capabilityRefreshInterval(signedData(`https://cdn.test/file?Policy=${policy}&Signature=x&Key-Pair-Id=k`), 0)).toBe(7500);
  });
  it("forwards cancellation and deduplicates metadata reads", async () => {
    vi.mocked(api.getAttachmentForDownload).mockResolvedValue(null);
    const client = new QueryClient();
    await Promise.all([client.fetchQuery(attachmentCapabilityOptions("ws", "a", "user")), client.fetchQuery(attachmentCapabilityOptions("ws", "a", "user"))]);
    expect(api.getAttachmentForDownload).toHaveBeenCalledTimes(1);
    expect(api.getAttachmentForDownload).toHaveBeenCalledWith("a", { signal: expect.any(AbortSignal) });
    client.clear();
  });
  it("refreshes every download tap and never opens on denial", async () => {
    const open = vi.fn().mockResolvedValue(undefined);
    vi.mocked(api.getAttachmentForDownload).mockResolvedValue(signed);
    await openAttachment("a", open);
    expect(open).toHaveBeenCalledWith("https://cdn.test/file?sig=fresh");
    await openAttachment("a", open);
    expect(api.getAttachmentForDownload).toHaveBeenCalledTimes(2);
    open.mockClear();
    vi.mocked(api.getAttachmentForDownload).mockRejectedValue(new Error("403"));
    await expect(openAttachment("a", open)).rejects.toThrow("403");
    expect(open).not.toHaveBeenCalled();
    vi.mocked(api.getAttachmentForDownload).mockResolvedValue(null);
    await openAttachment("a", open);
    expect(open).not.toHaveBeenCalled();
  });
  it("does not hand an in-flight response to Safari after logout/sign-in", async () => {
    const open = vi.fn();
    vi.mocked(api.getAttachmentForDownload).mockImplementation(async () => {
      invalidateSessionEpoch();
      return signed;
    });
    await openAttachment("a", open);
    expect(open).not.toHaveBeenCalled();
  });
  it("does not cache a response from a replaced credential", async () => {
    vi.mocked(api.getAttachmentForDownload).mockImplementation(async () => {
      invalidateSessionEpoch();
      return signed;
    });
    const client = new QueryClient();
    await expect(client.fetchQuery(attachmentCapabilityOptions("ws", "a", "user"))).rejects.toThrow("session changed");
    client.clear();
  });
});
