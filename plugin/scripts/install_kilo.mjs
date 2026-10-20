#!/usr/bin/env node
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import crypto from "node:crypto"
import { fileURLToPath, pathToFileURL } from "node:url"
import { execFileSync } from "node:child_process"
import { isDeepStrictEqual } from "node:util"
import jsonc from "jsonc-parser"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..")
const providerID = "y.ai"
const legacyProviderID = "company-ai"

export function parseConfig(content) {
  const errors = []
  const result = jsonc.parse(content, errors, { allowTrailingComma: true, allowEmptyContent: true }) ?? {}
  if (errors.length || typeof result !== "object" || Array.isArray(result) || result === null) {
    throw new Error("Invalid Kilo JSONC configuration; original file was not modified")
  }
  return result
}

export function mergeCompany(content, source) {
  let result = content || "{}\n"
  const before = parseConfig(result)
  const entry = source.plugin?.[0]
  if (!Array.isArray(entry) || typeof entry[0] !== "string" || !entry[1]?.issuer || !entry[1]?.gateway ||
      !source.provider?.[providerID] || source.provider[providerID].options?.baseURL !== entry[1].gateway + "/v1") {
    throw new Error("Expected validated y.ai plugin/provider configuration")
  }
  if (before.plugin !== undefined && !Array.isArray(before.plugin)) {
    throw new Error("Existing plugin field is not an array; original file was not modified")
  }
  const formattingOptions = { insertSpaces: true, tabSize: 2, eol: content.includes("\r\n") ? "\r\n" : "\n" }
  const changes = []
  const edit = (where, value, options = {}) => {
    result = jsonc.applyEdits(result, jsonc.modify(result, where, value, { formattingOptions, ...options }))
    changes.push(where.join("."))
  }
  if (!Array.isArray(before.plugin)) {
    edit(["plugin"], [entry])
  } else {
    const index = before.plugin.findIndex(x => (Array.isArray(x) ? x[0] : x) === entry[0])
    if (index < 0) edit(["plugin", before.plugin.length], entry, { isArrayInsertion: true })
    else if (!isDeepStrictEqual(before.plugin[index], entry)) edit(["plugin", index], entry)
  }
  const legacy = before.provider?.[legacyProviderID] ?? {}
  const current = before.provider?.[providerID] ?? {}
  const existing = { ...legacy, ...current, options: { ...legacy.options, ...current.options } }
  const incoming = source.provider[providerID]
  const provider = { ...existing, ...incoming, options: { ...existing.options, ...incoming.options },
    models: { ...incoming.models } }
  if (!isDeepStrictEqual(before.provider?.[providerID], provider)) edit(["provider", providerID], provider)
  if (Object.hasOwn(before.provider ?? {}, legacyProviderID)) edit(["provider", legacyProviderID], undefined)
  if (Array.isArray(before.enabled_providers)) {
    const enabled = [...new Set([...before.enabled_providers.map(x => x === legacyProviderID ? providerID : x), providerID])]
    if (!isDeepStrictEqual(before.enabled_providers, enabled)) edit(["enabled_providers"], enabled)
  }
  if (Array.isArray(before.disabled_providers)) {
    const disabled = before.disabled_providers.filter(x => ![providerID, legacyProviderID].includes(x))
    if (!isDeepStrictEqual(before.disabled_providers, disabled)) edit(["disabled_providers"], disabled)
  }
  const expected = structuredClone(before)
  const migrateModel = (where, value) => {
    if (typeof value !== "string" || !value.startsWith(legacyProviderID + "/")) return
    const renamed = providerID + value.slice(legacyProviderID.length)
    edit(where, renamed)
    let target = expected
    for (const key of where.slice(0, -1)) target = target[key]
    target[where.at(-1)] = renamed
  }
  for (const key of ["model", "small_model"]) migrateModel([key], before[key])
  for (const [agent, options] of Object.entries(before.agent ?? {})) {
    migrateModel(["agent", agent, "model"], options?.model)
  }
  const after = parseConfig(result)
  for (const key of Object.keys(before)) {
    if (["plugin", "provider", "enabled_providers", "disabled_providers"].includes(key)) continue
    if (!isDeepStrictEqual(expected[key], after[key])) throw new Error("Unrelated configuration changed")
  }
  for (const [id, value] of Object.entries(before.provider ?? {})) {
    if (![providerID, legacyProviderID].includes(id) && !isDeepStrictEqual(value, after.provider?.[id])) throw new Error("Unrelated provider changed")
  }
  return { content: result, changes }
}

export function installFile(filename, source, apply = false, merge = mergeCompany) {
  if (fs.existsSync(filename) && fs.lstatSync(filename).isSymbolicLink()) filename = fs.realpathSync(filename)
  const exists = fs.existsSync(filename)
  const original = exists ? fs.readFileSync(filename, "utf8") : "{}\n"
  const { content, changes } = merge(original, source)
  if (content === original) return { status: "unchanged", config: filename, changes: [] }
  if (!apply) return { status: "dry-run", config: filename, changes }
  const directory = path.dirname(filename)
  fs.mkdirSync(directory, { recursive: true })
  let backup = null
  if (exists) {
    const backupDir = path.join(directory, ".company-ai-backups")
    fs.mkdirSync(backupDir, { recursive: true, mode: 0o700 })
    fs.chmodSync(backupDir, 0o700)
    const hash = crypto.createHash("sha256").update(original).digest("hex").slice(0, 12)
    backup = path.join(backupDir, path.basename(filename) + "." + new Date().toISOString().replace(/[:.]/g, "-") + "." + hash)
    fs.writeFileSync(backup, original, { mode: 0o600, flag: "wx" })
  }
  const temp = path.join(directory, ".company-ai-" + crypto.randomUUID() + ".tmp")
  try {
    const mode = exists ? fs.statSync(filename).mode & 0o777 : 0o600
    fs.writeFileSync(temp, content, { mode, flag: "wx" })
    if ((exists && fs.readFileSync(filename, "utf8") !== original) || (!exists && fs.existsSync(filename))) {
      throw new Error("Config changed concurrently; original file was not replaced")
    }
    fs.renameSync(temp, filename)
  } finally {
    if (fs.existsSync(temp)) fs.unlinkSync(temp)
  }
  return { status: "installed", config: filename, backup, changes }
}

function main() {
  const args = process.argv.slice(2)
  if(args.includes("--help")) {
    console.log("Usage: yai-install --source /path/to/public-kilo-config.json [--local-plugin] [--config /path/to/kilo.jsonc] [--apply]. Default: dry-run. Inside workspace, --source can be generated from server config.")
    return
  }
  const configIndex = args.indexOf("--config")
  const sourceIndex = args.indexOf("--source")
  const filename = configIndex >= 0 ? path.resolve(args[configIndex + 1]) :
    path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config"), "kilo", "kilo.jsonc")
  let temporary
  try {
    let sourceFile
    if (sourceIndex >= 0) sourceFile = path.resolve(args[sourceIndex + 1])
    else {
      fs.mkdirSync(path.join(root, ".local"), { recursive: true })
      temporary = fs.mkdtempSync(path.join(root, ".local/kilo-registration-"))
      sourceFile = path.join(temporary, "company-ai.json")
      execFileSync(path.join(root, ".venv/bin/python"), ["tools/kilo_config.py", "--output", sourceFile],
        { cwd: root, env: { ...process.env, PYTHONPATH: path.join(root,"gateway/src")+path.delimiter+root }, stdio: "inherit" })
    }
    const source = JSON.parse(fs.readFileSync(sourceFile, "utf8"))
    if(args.includes("--local-plugin")) {
      source.plugin[0][0] = pathToFileURL(path.resolve(path.dirname(fileURLToPath(import.meta.url)),"../dist/index.js")).href
    }
    const report = installFile(filename, source, args.includes("--apply"))
    console.log(JSON.stringify(report, null, 2))
    if (args.includes("--apply")) console.log("Ready: kilo auth login --provider y.ai")
  } finally {
    if (temporary) fs.rmSync(temporary, { recursive: true, force: true })
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try { main() } catch { console.error("Company-ai registration failed; existing credentials were not modified. No secrets printed."); process.exitCode = 1 }
}
