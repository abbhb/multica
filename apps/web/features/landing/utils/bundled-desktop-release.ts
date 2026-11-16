import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import type { LatestRelease } from "./github-release";
import { parseReleaseAssets, parseReleaseAssetSizes } from "./parse-release-assets";

const BundleSchema = z.object({
  version: z.string().regex(/^v\d+\.\d+\.\d+$/),
  publishedAt: z.iso.datetime(),
  files: z.array(z.object({
    name: z.string().regex(/^multica-desktop-\d+\.\d+\.\d+-mac-(arm64|x64)\.(dmg|zip)$/),
    size: z.number().int().positive(),
    sha256: z.string().regex(/^[a-f0-9]{64}$/),
  })).min(1),
});

export function loadBundledDesktopRelease(): LatestRelease | null {
  const directory = process.env.MULTICA_DESKTOP_DOWNLOAD_DIR;
  if (process.env.MULTICA_DESKTOP_BUNDLE !== "true" || !directory) return null;
  try {
    const bundle = BundleSchema.parse(JSON.parse(readFileSync(join(directory, "release.json"), "utf8")));
    const assets = bundle.files.map((file) => ({
      ...file, browser_download_url: `/desktop/${encodeURIComponent(file.name)}`,
    }));
    return {
      version: bundle.version,
      publishedAt: bundle.publishedAt,
      htmlUrl: `https://github.com/abbhb/multica/releases/tag/${bundle.version}`,
      assets: parseReleaseAssets(assets),
      sizes: parseReleaseAssetSizes(assets),
    };
  } catch (error) {
    console.error("[download] Bundled desktop release is unavailable", error);
    return { version: null, publishedAt: null, htmlUrl: null, assets: {}, sizes: {} };
  }
}
