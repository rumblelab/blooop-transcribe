#!/usr/bin/env node

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");

const DEBUG = process.env.BLOOOP_WRAPPER_DEBUG === "1";

function fail(msg, code = 1) {
  console.error(`\n[blooop] ${msg}\n`);
  process.exit(code);
}

function debug(msg) {
  if (DEBUG) {
    console.error(`[blooop:debug] ${msg}`);
  }
}

function isSupportedPlatform() {
  return process.platform === "darwin" && process.arch === "arm64";
}

function isExecutable(filePath) {
  try {
    fs.accessSync(filePath, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function findAllInPath(cmd) {
  const seen = new Set();
  const pathParts = (process.env.PATH || "").split(path.delimiter);
  const results = [];
  for (const p of pathParts) {
    if (!p) continue;
    const full = path.join(p, cmd);
    if (!isExecutable(full)) continue;
    const real = realPathOrNull(full) || full;
    if (seen.has(real)) continue;
    seen.add(real);
    results.push(full);
  }
  return results;
}

function realPathOrNull(filePath) {
  try {
    return fs.realpathSync(filePath);
  } catch {
    return null;
  }
}

function isSelf(candidate) {
  const a = realPathOrNull(candidate);
  const b = realPathOrNull(__filename);
  return !!a && !!b && a === b;
}

function isWrapperScript(candidate) {
  try {
    const text = fs.readFileSync(candidate, "utf8");
    return text.includes("Blooop runtime is not installed yet");
  } catch {
    return false;
  }
}

function isUsableTarget(candidate) {
  if (!candidate) return false;
  if (!isExecutable(candidate)) return false;
  if (isSelf(candidate)) return false;
  if (isWrapperScript(candidate)) return false;
  return true;
}

function resolveTarget() {
  const explicit = process.env.BLOOOP_BIN;
  if (explicit) {
    if (!isExecutable(explicit)) {
      fail(`BLOOOP_BIN is set but not executable: ${explicit}`);
    }
    if (isWrapperScript(explicit)) {
      fail(`BLOOOP_BIN points to a wrapper, not the runtime: ${explicit}`);
    }
    debug(`using BLOOOP_BIN=${explicit}`);
    return explicit;
  }

  const preferred = [
    path.join(os.homedir(), ".local", "bin", "blooop"),
    path.join(os.homedir(), ".local", "bin", "bloop"),
  ];
  for (const candidate of preferred) {
    if (isUsableTarget(candidate)) {
      debug(`using preferred target ${candidate}`);
      return candidate;
    }
    if (DEBUG) {
      debug(`skipping preferred target ${candidate}`);
    }
  }

  const pathCandidates = [
    ...findAllInPath("blooop"),
    ...findAllInPath("bloop"),
  ];
  for (const candidate of pathCandidates) {
    if (!isUsableTarget(candidate)) {
      debug(`skipping PATH candidate ${candidate}`);
      continue;
    }
    debug(`using PATH candidate ${candidate}`);
    return candidate;
  }

  return null;
}

function runTarget(target, args) {
  debug(`launching ${target} ${args.join(" ")}`.trim());
  const child = spawn(target, args, { stdio: "inherit" });
  child.on("exit", (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code == null ? 1 : code);
  });
  child.on("error", (err) => {
    fail(`Failed to launch ${target}: ${err.message}`);
  });
}

if (!isSupportedPlatform()) {
  fail("Blooop currently supports macOS on Apple Silicon only (M1/M2/M3/M4).");
}

const args = process.argv.slice(2);
const target = resolveTarget();
if (target) {
  runTarget(target, args);
} else {
  fail(
    "Blooop runtime is not installed yet.\n" +
      "Install it with:\n" +
      "  git clone https://github.com/rumblelab/blooop\n" +
      "  cd blooop\n" +
      "  ./setup.sh\n" +
      "  blooop"
  );
}
