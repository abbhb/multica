import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

const script = new URL("./stage-desktop-downloads.mjs", import.meta.url);

test("stages matching Mac installers with verifiable checksums", () => {
  const root = mkdtempSync(join(tmpdir(), "multica-stage-desktop-"));
  try {
    const bytes = Buffer.from("test-created installer fixture");
    for (const arch of ["arm64", "x64"]) {
      for (const ext of ["dmg", "zip"]) writeFileSync(join(root, `multica-desktop-0.9.1-mac-${arch}.${ext}`), bytes);
    }
    const output = join(root, "bundle");
    const result = spawnSync(process.execPath, [script.pathname, root, "0.9.1", output], { encoding: "utf8" });
    assert.equal(result.status, 0, result.stderr);
    const manifest = JSON.parse(readFileSync(join(output, "release.json"), "utf8"));
    assert.equal(manifest.version, "v0.9.1");
    assert.equal(manifest.files.length, 4);
    for (const file of manifest.files) {
      assert.deepEqual(readFileSync(join(output, file.name)), bytes);
      assert.equal(file.sha256, createHash("sha256").update(bytes).digest("hex"));
      assert.match(readFileSync(join(output, "SHA256SUMS"), "utf8"), new RegExp(`${file.sha256}  ${file.name}`));
    }
  } finally { rmSync(root, { recursive: true }); }
});

test("rejects a release with a missing architecture", () => {
  const root = mkdtempSync(join(tmpdir(), "multica-stage-desktop-"));
  try {
    writeFileSync(join(root, "multica-desktop-0.9.1-mac-arm64.dmg"), "incomplete");
    const result = spawnSync(process.execPath, [script.pathname, root, "0.9.1", join(root, "bundle")]);
    assert.notEqual(result.status, 0);
  } finally { rmSync(root, { recursive: true }); }
});
