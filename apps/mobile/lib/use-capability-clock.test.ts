import { afterEach, describe, expect, it, vi } from "vitest";
import { focusManager } from "@tanstack/react-query";
import { watchCapabilityExpiry } from "./use-capability-clock";

afterEach(() => { vi.useRealTimers(); focusManager.setFocused(true); });

describe("native capability clock", () => {
  it("forces expiry evaluation even when the refresh request never returns", () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const change = vi.fn();
    const cleanup = watchCapabilityExpiry(55_000, change);
    vi.advanceTimersByTime(54_999);
    expect(change).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(change).toHaveBeenCalledOnce();
    cleanup();
  });
  it("rechecks on foreground and removes timers/subscriptions when unmounted", () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const change = vi.fn();
    const cleanup = watchCapabilityExpiry(55_000, change);
    focusManager.setFocused(false);
    change.mockClear();
    vi.setSystemTime(70_000);
    focusManager.setFocused(true);
    expect(change).toHaveBeenCalledOnce();
    cleanup();
    change.mockClear();
    vi.runAllTimers();
    focusManager.setFocused(false);
    expect(change).not.toHaveBeenCalled();
  });
});
