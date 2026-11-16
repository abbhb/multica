// @vitest-environment node
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { loadBundledDesktopRelease } from "./bundled-desktop-release";
import { fetchLatestRelease } from "./github-release";

const directories: string[] = [];
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.restoreAllMocks(); for (const dir of directories.splice(0)) rmSync(dir, { recursive: true }); });

function bundle(files: unknown[]) {
  const dir = mkdtempSync(join(tmpdir(), "multica-desktop-bundle-"));
  directories.push(dir);
  vi.stubEnv("MULTICA_DESKTOP_BUNDLE", "true");
  vi.stubEnv("MULTICA_DESKTOP_DOWNLOAD_DIR", dir);
  writeFileSync(join(dir, "release.json"), JSON.stringify({ version: "v0.9.1", publishedAt: "2026-11-16T00:00:00.000Z", files }));
}

describe("bundled desktop release", () => {
  it("keeps downloads on this server with the bundled version and sizes", () => {
    bundle(["arm64", "x64"].map((arch) => ({ name: `multica-desktop-0.9.1-mac-${arch}.dmg`, size: 123, sha256: "a".repeat(64) })));
    const release = loadBundledDesktopRelease();
    expect(release?.version).toBe("v0.9.1");
    expect(release?.assets.macArm64Dmg).toBe("/desktop/multica-desktop-0.9.1-mac-arm64.dmg");
    expect(release?.assets.macX64Dmg).toBe("/desktop/multica-desktop-0.9.1-mac-x64.dmg");
    expect(release?.sizes.macArm64Dmg).toBe(123);
  });

  it("does not replace a missing or malformed self-host bundle with upstream installers", () => {
    bundle([{ name: "../private.dmg", size: 123, sha256: "a".repeat(64) }]);
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(loadBundledDesktopRelease()?.assets).toEqual({});
  });

  it("leaves the ordinary download page available outside bundled releases", () => {
    vi.stubEnv("MULTICA_DESKTOP_BUNDLE", "false");
    expect(loadBundledDesktopRelease()).toBeNull();
  });

  it("uses the bundle for homepage download buttons without fetching upstream", async () => {
    bundle([{ name: "multica-desktop-0.9.1-mac-arm64.dmg", size: 123, sha256: "a".repeat(64) }]);
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    expect((await fetchLatestRelease()).assets.macArm64Dmg).toBe("/desktop/multica-desktop-0.9.1-mac-arm64.dmg");
    expect(fetch).not.toHaveBeenCalled();
  });
});
