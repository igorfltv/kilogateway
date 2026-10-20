import http from "node:http"
import { randomBytes } from "node:crypto"
import { readFile } from "node:fs/promises"
import { homedir } from "node:os"
import { join } from "node:path"
import type { Options, Fetcher } from "./device.js"
import { openBrowser } from "./browser.js"

export type MCPServer = { id: "jira" | "confluence"; name: string; connected: boolean }
export async function catalog(opts: Options, key: string, fetcher: Fetcher = fetch): Promise<MCPServer[]> {
  const response = await fetcher(opts.gateway + "/mcp/catalog", {redirect:"error",
    headers:{Authorization:"Bearer " + key}, signal:AbortSignal.timeout(10000)})
  if (!response.ok) throw new Error("Y.AI MCP catalog unavailable; check gateway access")
  const value = await response.json() as {servers?: MCPServer[]}
  if (!Array.isArray(value.servers) || value.servers.some(s => !["jira","confluence"].includes(s.id) || typeof s.connected !== "boolean")) {
    throw new Error("Invalid Y.AI MCP catalog")
  }
  return value.servers.filter(s => s.connected)
}

export async function onboardMCP(opts: Options, key: string, fetcher: Fetcher, signal: AbortSignal | undefined,
  wait: (ms: number, signal?: AbortSignal) => Promise<void>, announce = (url: string) => {
    process.stderr.write("\nY.AI: enter Jira / Confluence PATs in the gateway form (or skip).\n" + url + "\n")
    if (opts.openBrowser !== false) openBrowser(url)
  }, ticket?: string) {
  const deadline = Date.now() + 900000
  const overall = AbortSignal.timeout(900000)
  const active = signal ? AbortSignal.any([signal, overall]) : overall
  async function request(path: string, method = "GET") {
    try {
      const r = await fetcher(opts.gateway + path, {method, redirect:"error", headers:{Authorization:"Bearer " + key},
        ...(method === "POST" && ticket ? {headers:{Authorization:"Bearer " + key,"Content-Type":"application/json"},body:JSON.stringify({ticket})} : {}),
        signal:AbortSignal.any([active, AbortSignal.timeout(15000)])})
      if (!r.ok) throw new Error()
      return await r.json() as Record<string, unknown>
    } catch { throw new Error(active.aborted ? "Y.AI MCP setup cancelled or expired; repeat login" : "Y.AI MCP setup unavailable; repeat login") }
  }
  const session = await request("/auth/mcp/setup", "POST")
  const expected = opts.gateway + "/auth/mcp/setup#"
  if (typeof session.id !== "string" || !/^ms_[\w-]+$/.test(session.id) || typeof session.url !== "string" ||
      !session.url.startsWith(expected) || !/^ms_[\w-]+\.[\w-]+$/.test(session.url.slice(expected.length))) {
    throw new Error("Invalid Y.AI MCP setup response")
  }
  if (ticket && session.url !== expected + ticket) throw new Error("Invalid Y.AI MCP setup binding")
  announce(session.url)
  while (Date.now() < deadline) {
    await wait(1000, active)
    if ((await request("/auth/mcp/setup/status/" + session.id)).complete === true) return
  }
  throw new Error("Y.AI MCP setup expired; repeat login")
}

export async function kiloCredential(opts: Options): Promise<string | undefined> {
  try {
    const filename = process.env.COMPANY_KILO_AUTH_FILE || join(process.env.XDG_DATA_HOME || join(homedir(), ".local/share"), "kilo/auth.json")
    const auth = JSON.parse(await readFile(filename, "utf8"))["y.ai"]
    if (auth?.type === "api" && typeof auth.key === "string" && auth.key.startsWith("cg_gc_") &&
        auth.metadata?.company_gateway === opts.gateway && auth.metadata?.company_issuer === opts.issuer) return auth.key
  } catch {}
}

// Native MCP gets a process-only loopback capability. The relay resolves the current
// Y.AI credential on EVERY request; PATs and gateway credentials never enter MCP config.
export async function createMCPRelay(opts: Options, credential: () => Promise<string | undefined>, fetcher: Fetcher = fetch) {
  const capability = randomBytes(32).toString("base64url")
  const controllers = new Set<AbortController>()
  const server = http.createServer({maxHeaderSize:16384}, async (req,res) => {
    const fail = (status: number, code: string) => {
      if (res.headersSent) return res.destroy()
      res.writeHead(status,{"Content-Type":"application/json","Cache-Control":"no-store"}); res.end(JSON.stringify({error:{code}}))
    }
    if (req.headers.origin || req.headers["x-yai-mcp"] !== capability) return fail(403,"mcp_local_access_denied")
    const target = new URL(req.url || "/", "http://127.0.0.1")
    // OpenCode v2 adds transport metadata in the query. It cannot select a
    // destination: discard it and forward only the two pinned endpoint paths.
    if (!/^\/mcp\/(jira|confluence)$/.test(target.pathname)) return fail(403,"mcp_path_denied")
    if (!["POST","GET","DELETE"].includes(req.method || "")) return fail(403,"mcp_method_denied")
    const controller = new AbortController(); controllers.add(controller)
    const cancel = () => { if (!res.writableEnded) controller.abort() }
    req.once("aborted",cancel); res.once("close",cancel)
    try {
      const chunks: Buffer[] = []; let size = 0
      for await (const chunk of req) {
        size += chunk.length
        if(size > 65536) return fail(413,"mcp_request_too_large")
        chunks.push(Buffer.from(chunk))
      }
      const key = await credential()
      if (!key) return fail(401,"mcp_login_required")
      const headers = new Headers({Authorization:"Bearer " + key,Accept:"application/json, text/event-stream"})
      if (req.method === "POST") headers.set("Content-Type","application/json")
      const version = req.headers["mcp-protocol-version"]
      if(typeof version === "string") headers.set("MCP-Protocol-Version",version)
      const upstream = await fetcher(opts.gateway + target.pathname,{method:req.method,headers,
        body:req.method === "POST" ? Buffer.concat(chunks) : undefined,redirect:"error",
        signal:AbortSignal.any([controller.signal,AbortSignal.timeout(30000)])})
      if (upstream.status >= 300 && upstream.status < 400) { await upstream.body?.cancel(); return fail(502,"mcp_redirect_blocked") }
      // The gateway is stateless and replies JSON only; never forward arbitrary headers.
      const parts: Uint8Array[] = []; let length = 0
      if (upstream.body) {
        const reader = upstream.body.getReader()
        try {
          while (true) {
            const {done,value} = await reader.read()
            if (done) break
            length += value.byteLength
            if (length > 524288) { await reader.cancel(); return fail(502,"mcp_response_too_large") }
            parts.push(value)
          }
        } finally { reader.releaseLock() }
      }
      res.writeHead(upstream.status,{"Content-Type":"application/json","Cache-Control":"no-store"}); res.end(Buffer.concat(parts))
    } catch { fail(502,"mcp_gateway_unavailable_relogin") }
    finally { controllers.delete(controller); req.off("aborted",cancel); res.off("close",cancel) }
  })
  server.requestTimeout = 35000; server.headersTimeout = 10000
  await new Promise<void>((resolve,reject) => {server.once("error",reject);server.listen(0,"127.0.0.1",()=>{server.off("error",reject);resolve()})})
  server.unref()
  server.on("error",()=>{for(const c of controllers)c.abort()})
  const origin = "http://127.0.0.1:" + (server.address() as import("node:net").AddressInfo).port
  return {
    remote(system: string) {
      if(!["jira","confluence"].includes(system)) throw new Error("Invalid Y.AI MCP system")
      return {type:"remote" as const,url:origin + "/mcp/" + system,headers:{"x-yai-mcp":capability},oauth:false as const}
    },
    async close() { for(const c of controllers)c.abort(); await new Promise<void>(resolve=>{server.close(()=>resolve());server.closeAllConnections()}) },
  }
}
