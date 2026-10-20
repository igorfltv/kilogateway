#!/usr/bin/env node
import { readFile } from "node:fs/promises"
import { homedir } from "node:os"
import { join } from "node:path"
import { config } from "./device.js"

const [action, id] = process.argv.slice(2)
if (!["list", "me", "revoke"].includes(action)) {
  console.error("Usage: company-ai list | me | revoke [credential-id|current]. Requires COMPANY_GATEWAY_URL, COMPANY_OIDC_ISSUER.")
  process.exit(2)
}
try {
  const opts = config()
  const path = process.env.COMPANY_KILO_AUTH_FILE || join(process.env.XDG_DATA_HOME || join(homedir(), ".local/share"), "kilo/auth.json")
  const auth = JSON.parse(await readFile(path, "utf8"))["y.ai"]
  if (auth?.type !== "api" || !auth.key?.startsWith("cg_gc_")) throw new Error()
  if (auth.metadata?.company_gateway !== opts.gateway || auth.metadata?.company_issuer !== opts.issuer) throw new Error()
  const suffix = action === "me" ? "/me" : "/auth/cli/credentials" + (action === "revoke" ? "/" + encodeURIComponent(id || "current") : "")
  const r = await fetch(opts.gateway + suffix, { method: action === "revoke" ? "DELETE" : "GET", redirect: "error",
    headers: { Authorization: "Bearer " + auth.key }, signal: AbortSignal.timeout(15000) })
  if (!r.ok) throw new Error()
  if (r.status !== 204) console.log(JSON.stringify(await r.json(), null, 2))
  else console.log("Server credential revoked. Remove local record with: kilo auth logout y.ai")
} catch {
  console.error("Company API failed. Check configuration and login; secrets were not printed.")
  process.exitCode = 1
}
