#!/usr/bin/env node
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { spawn } from "node:child_process"

export function normalizeArgs(args: string[]): string[] {
  if (args[0] !== "auth" || args[1] !== "login") return args
  const result = args.slice()
  const matches = result.map((arg, i) => arg === "--provider" || arg.startsWith("--provider=") ? i : -1).filter(i => i >= 0)
  if (!matches.length) return result
  if (matches.length !== 1) throw new Error("Use --provider once")
  const index = matches[0]
  const inline = result[index].startsWith("--provider=")
  const value = inline ? result[index].slice("--provider=".length) : result[index + 1]
  if (!value || value.startsWith("-")) throw new Error("--provider requires an integration ID")
  result.splice(index, inline ? 1 : 2)
  result.splice(2, 0, value)
  return result
}

function main() {
  const runtime = path.join(os.homedir(), ".local/share/y-ai/opencode/runtime.json")
  const binary = process.env.YAI_OPENCODE_BINARY || JSON.parse(fs.readFileSync(runtime, "utf8")).binary
  if (typeof binary !== "string" || fs.realpathSync(binary) === fileURLToPath(import.meta.url)) throw new Error("Invalid OpenCode native runtime")
  const child = spawn(binary, normalizeArgs(process.argv.slice(2)), { stdio: "inherit", env: process.env })
  const interrupt = () => child.kill("SIGINT")
  const terminate = () => child.kill("SIGTERM")
  process.on("SIGINT", interrupt); process.on("SIGTERM", terminate)
  child.once("error", () => { console.error("OpenCode v2 could not start; check the installed native runtime"); process.exitCode = 1 })
  child.once("exit", (code, signal) => {
    process.removeListener("SIGINT", interrupt); process.removeListener("SIGTERM", terminate)
    if (signal) process.kill(process.pid, signal)
    else process.exitCode = code ?? 1
  })
}
if (process.argv[1] && fs.existsSync(process.argv[1]) && fs.realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { main() } catch { console.error("OpenCode v2 setup incomplete; run the Y.AI OpenCode installer"); process.exitCode = 1 }
}
