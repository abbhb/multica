/**
 * Block-level image with real aspect ratio + tap-to-lightbox.
 *
 *   - Aspect ratio detection uses RN's `Image.getSize` (cross-platform,
 *     network-friendly). While dimensions resolve we lay out at 16:9 as
 *     a placeholder — same width-100% so the surrounding flow is stable
 *     and only the height shifts once the real ratio lands.
 *   - Rendering uses `expo-image` for on-disk caching + smooth fade-in
 *     transition.
 *   - Tap dispatches into the global LightboxProvider for fullscreen
 *     viewing with pinch-zoom + swipe-down-to-dismiss.
 *
 * URI resolution: known attachment records are resolved through the mobile
 * authenticated metadata API. Native image requests receive only short-lived
 * capability URLs, never the session token. Capabilities refresh while mounted;
 * unknown/external images retain their original URL.
 *
 * Cancellation: a content re-render that swaps the URI must not let the
 * previous getSize callback overwrite state — guard with a `cancelled`
 * flag in the cleanup path.
 */
import { useEffect, useMemo, useState } from "react";
import { Image as RNImage, Pressable, View } from "react-native";
import { Image as ExpoImage } from "expo-image";
import type { Attachment } from "@multica/core/types";
import { attachmentMedia } from "@/lib/attachment-media";
import { useAttachmentMedia } from "@/lib/use-attachment-media";
import { useLightbox } from "./lightbox-provider";
import { useImageSequence } from "./image-sequence";

interface Props {
  uri: string;
  alt?: string;
  attachments?: Attachment[];
}

export function MarkdownImage({ uri, attachments }: Props) {
  const { open } = useLightbox();
  const sequence = useImageSequence();
  const [aspect, setAspect] = useState<number | null>(null);

  const media = useMemo(() => attachmentMedia(uri, attachments), [uri, attachments]);
  const resolvedUri = useAttachmentMedia(media);

  useEffect(() => {
    if (!resolvedUri) return;
    let cancelled = false;
    RNImage.getSize(
      resolvedUri,
      (w, h) => {
        if (cancelled || !w || !h) return;
        setAspect(w / h);
      },
      () => {
        // Network failure / decode failure / 404 / unknown URI scheme
        // (e.g. unresolved mc://) — keep the 16:9 fallback so the slot
        // still shows the muted background instead of collapsing.
        if (!cancelled) setAspect(16 / 9);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [resolvedUri]);

  return (
    <Pressable onPress={() => open(media, sequence)}>
      <View className="rounded-lg overflow-hidden bg-muted">
        <ExpoImage
          source={resolvedUri ? { uri: resolvedUri } : undefined}
          style={{ width: "100%", aspectRatio: aspect ?? 16 / 9 }}
          cachePolicy={media.attachmentId ? "none" : "disk"}
          contentFit="contain"
          transition={150}
        />
      </View>
    </Pressable>
  );
}
