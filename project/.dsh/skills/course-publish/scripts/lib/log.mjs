import { createWriteStream } from "node:fs";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { paths } from "./paths.mjs";

let stream;
function getStream() {
  if (!stream) {
    mkdirSync(paths.logsDir(), { recursive: true });
    stream = createWriteStream(paths.logFile("pipeline"), { flags: "a" });
  }
  return stream;
}

function ts() {
  return new Date().toISOString();
}

function emit(level, args) {
  const line = `[${ts()}] [${level}] ${args.map(String).join(" ")}`;
  if (level === "ERROR") console.error(line);
  else console.log(line);
  try { getStream().write(line + "\n"); } catch {}
}

export const log = {
  info: (...a) => emit("INFO", a),
  warn: (...a) => emit("WARN", a),
  error: (...a) => emit("ERROR", a),
  step: (n, ...a) => emit("INFO", [`▶ ${n}`, ...a]),
  ok: (...a) => emit("INFO", ["✓", ...a]),
};
