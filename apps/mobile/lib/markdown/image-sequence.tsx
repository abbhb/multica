/**
 * Screen-scoped image sequence for the lightbox (MUL-5752).
 *
 * A screen that can show several images (an issue: description + every
 * comment; a chat session: every message) mounts this once with its blocks in
 * render order. Every `MarkdownImage` below it then opens the lightbox
 * positioned inside that sequence instead of on its own, so a swipe walks to
 * the next image and the viewer can say "3 / 7".
 *
 * Mirrors the web/desktop `ImageSequenceProvider`
 * (packages/views/editor/image-sequence-context.tsx) — same product contract,
 * different presentation. The ordering itself is NOT re-derived here: both
 * clients call `collectImageSequence` from @multica/core so an issue's images
 * are counted the same way on every client (mobile parity rule: mirror the
 * shared logic, own the UI).
 *
 * Stable attachment identities are preserved here. Native previews fetch
 * credential-free capabilities separately; expiring signatures never change
 * a clicked image's sequence position.
 */
import { createContext, use, useMemo, type ReactNode } from "react";
import {
  collectImageSequence,
  type ImageSequenceBlock,
} from "@multica/core/attachments/image-sequence";
import { attachmentMedia, type AttachmentMedia } from "@/lib/attachment-media";

const ImageSequenceContext = createContext<readonly AttachmentMedia[]>([]);

/**
 * Stable image sources for the surrounding screen, in render order. Empty when
 * no provider is mounted — the lightbox then shows the tapped image alone.
 */
export function useImageSequence(): readonly AttachmentMedia[] {
  return use(ImageSequenceContext);
}

export function ImageSequenceProvider({
  blocks,
  children,
}: {
  blocks: readonly (ImageSequenceBlock | null | undefined)[];
  children: ReactNode;
}) {
  const uris = useMemo(
    () =>
      collectImageSequence(blocks).map(
        (item) => attachmentMedia(item.url, item.attachment ? [item.attachment] : undefined),
      ),
    [blocks],
  );
  return (
    <ImageSequenceContext.Provider value={uris}>
      {children}
    </ImageSequenceContext.Provider>
  );
}
