/**
 * App-level lightbox provider for tap-to-zoom image viewing.
 *
 * Single instance mounted at the root layout. `useLightbox().open(uri)`
 * displays the image fullscreen with pinch-to-zoom, double-tap, and
 * swipe-down-to-dismiss — all handled by `react-native-image-viewing`.
 *
 * `open(uri, sequence)` opens the same viewer positioned inside a whole
 * screen's images (MUL-5752), so a horizontal swipe walks to the next one
 * and a "3 / 7" counter says where the reader is. `ImageSequenceProvider`
 * builds that array; screens that don't mount one keep passing a single URI
 * and get the previous single-image behaviour.
 *
 * Parity with web/desktop, all from the same product brief:
 *   - images only, never mixed with other attachment kinds;
 *   - the sequence is frozen when the viewer opens, so an arriving comment
 *     cannot shift the index under the reader;
 *   - boundaries stop rather than wrap. `react-native-image-viewing` pages a
 *     fixed array and does not loop, so this comes for free.
 *
 * The gesture is the platform's own horizontal paging rather than the
 * desktop chevrons — same semantics, phone-native interaction.
 */
import { createContext, use, useMemo, useState, type ReactNode } from "react";
import { View } from "react-native";
import ImageView from "react-native-image-viewing";
import { useQueries } from "@tanstack/react-query";
import { currentSessionEpoch } from "@/data/session-epoch";
import { useAuthStore } from "@/data/auth-store";
import { useWorkspaceStore } from "@/data/workspace-store";
import { attachmentCapabilityOptions } from "@/data/queries/attachments";
import {
  attachmentMediaURL,
  attachmentMediaSequence,
  type AttachmentMedia,
} from "@/lib/attachment-media";
import { useCapabilityClock } from "@/lib/use-capability-clock";
import { Text } from "@/components/ui/text";

interface LightboxApi {
  /**
   * Show `uri` fullscreen. When `sequence` contains it, the viewer opens at
   * its real position and the reader can swipe through the rest; otherwise
   * the viewer shows that one image.
   */
  open: (
    source: string | AttachmentMedia,
    sequence?: readonly AttachmentMedia[],
  ) => void;
}

const LightboxContext = createContext<LightboxApi>({
  open: () => {
    // No-op fallback when used outside provider — markdown rendering
    // shouldn't crash if a screen forgets to mount the provider.
  },
});

export function useLightbox(): LightboxApi {
  return use(LightboxContext);
}

interface Viewing {
  sessionEpoch: number;
  images: AttachmentMedia[];
  index: number;
}

export function LightboxProvider({ children }: { children: ReactNode }) {
  const [viewing, setViewing] = useState<Viewing | null>(null);
  // Tracked separately from `viewing.index` so the counter follows a swipe;
  // `viewing` itself is the frozen snapshot taken when the viewer opened.
  const [currentIndex, setCurrentIndex] = useState(0);

  const api = useMemo<LightboxApi>(
    () => ({
      open: (source, sequence) => {
        const media = typeof source === "string" ? { uri: source } : source;
        const { images, index } = attachmentMediaSequence(media, sequence);
        setCurrentIndex(index);
        setViewing({ images, index, sessionEpoch: currentSessionEpoch() });
      },
    }),
    [],
  );

  const wsId = useWorkspaceStore((s) => s.currentWorkspaceId);
  const userId = useAuthStore((s) => s.user?.id ?? null);
  const viewingActive = !!viewing && viewing.sessionEpoch === currentSessionEpoch();
  const sources = viewingActive ? viewing.images : [];
  const attachmentIds = [...new Set(sources.slice(Math.max(0, currentIndex - 1), currentIndex + 2).flatMap(
    (source) => source.attachmentId ? [source.attachmentId] : [],
  ))];
  const capabilities = useQueries({
    queries: attachmentIds.map((id) => attachmentCapabilityOptions(wsId, id, userId)),
  });
  const now = useCapabilityClock(capabilities.map((query) => query.data));
  const images = sources.map((source) => {
    const capability = capabilities[attachmentIds.indexOf(source.attachmentId ?? "")];
    return {
      uri: attachmentMediaURL(source, capability?.isError ? undefined : capability?.data, now) ?? "",
      ...(source.attachmentId ? { cache: "reload" as const } : {}),
    };
  });

  const total = viewing?.images.length ?? 0;
  // Only a sequence gets a counter — a lone image has nothing to count.
  const Footer = useMemo(
    () =>
      total > 1
        ? function LightboxCounter() {
            return (
              <View className="items-center pb-10">
                <Text className="text-sm text-white">
                  {currentIndex + 1} / {total}
                </Text>
              </View>
            );
          }
        : undefined,
    [currentIndex, total],
  );

  return (
    <LightboxContext.Provider value={api}>
      {children}
      <ImageView
        images={images}
        keyExtractor={(_image, index) => `${sources[index]?.attachmentId ?? sources[index]?.uri ?? "image"}:${index}`}
        imageIndex={viewing?.index ?? 0}
        visible={viewingActive}
        onRequestClose={() => setViewing(null)}
        onImageIndexChange={setCurrentIndex}
        FooterComponent={Footer}
      />
    </LightboxContext.Provider>
  );
}
