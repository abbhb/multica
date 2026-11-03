import { useCallback, useState } from "react";
import { useFocusEffect } from "expo-router";
import { useCapabilityClock } from "./use-capability-clock";
import { useQuery } from "@tanstack/react-query";
import { useAuthStore } from "@/data/auth-store";
import { useWorkspaceStore } from "@/data/workspace-store";
import { attachmentCapabilityOptions } from "@/data/queries/attachments";
import { attachmentMediaURL, type AttachmentMedia } from "./attachment-media";

export function useAttachmentMedia(media: AttachmentMedia): string | undefined {
  const wsId = useWorkspaceStore((s) => s.currentWorkspaceId);
  const userId = useAuthStore((s) => s.user?.id ?? null);
  const [screenFocused, setScreenFocused] = useState(false);
  useFocusEffect(useCallback(() => {
    setScreenFocused(true);
    return () => setScreenFocused(false);
  }, []));
  const options = attachmentCapabilityOptions(wsId, media.attachmentId, userId);
  const query = useQuery({ ...options, enabled: options.enabled && screenFocused });
  const now = useCapabilityClock([query.data]);
  return attachmentMediaURL(media, query.isError ? undefined : query.data, now);
}
