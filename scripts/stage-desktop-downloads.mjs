#!/usr/bin/env node
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const [input, version, destination] = process.argv.slice(2);
if (!input || !/^\d+\.\d+\.\d+$/.test(version ?? "")) {
  throw new Error("Usage: node scripts/stage-desktop-downloads.mjs <installer-directory> <X.Y.Z>");
}
const output = destination ? resolve(destination) : join(root, "desktop-downloads");
const wanted = new Set(["arm64", "x64"].flatMap((arch) =>
  ["dmg", "zip"].map((ext) => `multica-desktop-${version}-mac-${arch}.${ext}`),
));
function walk(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? walk(path) : [path];
  });
}
const artifacts = walk(resolve(input)).filter((path) => wanted.has(basename(path)));
if (artifacts.length !== wanted.size || new Set(artifacts.map((path) => basename(path))).size !== wanted.size) {
  throw new Error(`Expected one DMG and ZIP for each Mac architecture at version ${version}`);
}
mkdirSync(output, { recursive: true });
const files = artifacts.sort().map((path) => {
  const name = basename(path);
  const bytes = readFileSync(path);
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  copyFileSync(path, join(output, name));
  return { name, size: bytes.length, sha256 };
});
writeFileSync(join(output, "release.json"), JSON.stringify({ version: `v${version}`, publishedAt: new Date().toISOString(), files }, null, 2) + "\n");
writeFileSync(join(output, "SHA256SUMS"), files.map(({ name, sha256 }) => `${sha256}  ${name}\n`).join(""));
console.log(`Staged ${files.length} desktop installers for v${version} in ${output}`);
