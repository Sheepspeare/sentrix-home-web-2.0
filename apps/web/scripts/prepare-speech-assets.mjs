#!/usr/bin/env node
/** Install the official, hash-pinned WASM + SenseVoice bundle. No npm runtime dependency. */
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { closeSync, createReadStream, openSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const lock = JSON.parse(await readFile(join(root, "speech-assets.lock.json"), "utf8"));
const output = join(root, "public/speech", lock.directory);
const arguments_ = process.argv.slice(2);
if (arguments_.length > 1) throw new Error("Usage: npm run speech:prepare -- [local-archive.tar.bz2]");
const temporary = await mkdtemp(join(tmpdir(), "sentrix-speech-"));
try {
  const archive = arguments_[0] ?? join(temporary, "speech.tar.bz2");
  if (!arguments_[0]) {
    console.log("Downloading official SenseVoice-Small / sherpa-onnx 1.12.20 (~166 MB)…");
    execFileSync("curl", ["--fail", "--location", "--retry", "2", "--connect-timeout", "20",
      "--max-time", "1800", lock.url, "--output", archive], { stdio: "inherit" });
  }
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(archive)) hash.update(chunk);
  if (hash.digest("hex") !== lock.sha256) throw new Error("Speech archive SHA-256 mismatch; not installed.");
  const stage = join(temporary, "verified");
  await mkdir(stage);
  // Extract only expected ordinary files. Never unpack arbitrary archive paths into the project.
  for (const name of lock.files) {
    const destination = join(stage, name);
    const fd = openSync(destination, "wx");
    try {
      execFileSync("tar", ["-xOf", archive, `./${lock.archiveRoot}/${name}`],
        { stdio: ["ignore", fd, "inherit"] });
    } finally { closeSync(fd); }
    if (!(await stat(destination)).size) throw new Error(`Empty speech asset: ${name}`);
  }
  await writeFile(join(stage, "manifest.json"), JSON.stringify(lock, null, 2) + "\n");
  await mkdir(output, { recursive: true });
  for (const name of [...lock.files, "manifest.json"]) {
    await rename(join(stage, name), join(output, name));
    console.log(`${name}: ${(await stat(join(output, name))).size} bytes`);
  }
  console.log(`Speech assets ready: ${output}`);
} finally {
  await rm(temporary, { recursive: true, force: true });
}
