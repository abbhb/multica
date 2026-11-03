import { useEffect, useState } from "react";
import { focusManager } from "@tanstack/react-query";
import { attachmentCapabilityDeadline, type AttachmentCapability } from "./attachment-media";

/** Native timers can pause in the background, so foreground also checks age. */
export function watchCapabilityExpiry(deadline: number, onChange: () => void): () => void {
  const timer = Number.isFinite(deadline)
    ? setTimeout(onChange, Math.max(0, deadline - Date.now())) : undefined;
  const unsubscribe = focusManager.subscribe(onChange);
  return () => {
    if (timer !== undefined) clearTimeout(timer);
    unsubscribe();
  };
}

/** Clear a native source at expiry even if a refresh is stalled/offline. */
export function useCapabilityClock(capabilities: readonly (AttachmentCapability | undefined)[]): number {
  const [now, setNow] = useState(Date.now);
  const deadline = Math.min(...capabilities.flatMap((capability) => {
    if (!capability) return [];
    const expiresAt = attachmentCapabilityDeadline(capability);
    return expiresAt > now ? [expiresAt] : [];
  }));
  useEffect(() => watchCapabilityExpiry(deadline, () => setNow(Date.now())), [deadline]);
  return now;
}
