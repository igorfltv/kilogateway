import http from "node:http"
import { randomBytes } from "node:crypto"
import { Readable } from "node:stream"
import { gatewayFetch, gatewayTarget, type Fetcher } from "./device.js"

const blocked = new Set(["authorization", "cookie", "set-cookie", "x-api-key", "api-key", "x-auth-token",
  "proxy-authorization", "proxy-authenticate", "x-yai-local", "host", "connection", "content-length",
  "transfer-encoding", "keep-alive", "te", "trailer", "upgrade", "content-encoding", "location"])

// OpenCode v2.0.26 reconstructs requests and can follow redirects. The plugin owns
// the final fetch so the real gateway credential never enters that native transport.
export async function createOpenCodeTransport(gateway: string, credential: () => Promise<string>, fetcher: Fetcher = fetch) {
  const capability = randomBytes(32).toString("base64url")
  const controllers = new Set<AbortController>()
  let closed = false
  const server = http.createServer({ maxHeaderSize: 65536 }, async (req, res) => {
    const fail = (status: number, message: string) => {
      if (res.headersSent) return res.destroy()
      res.writeHead(status, { "content-type": "application/json", "cache-control": "no-store" })
      res.end(JSON.stringify({ error: { message } }))
    }
    if (closed || req.headers["x-yai-local"] !== capability) return fail(401, "Y.AI local transport unavailable")
    const path = req.url
    if (!((req.method === "POST" && path === "/v1/chat/completions") ||
          (req.method === "GET" && path === "/v1/models"))) return fail(403, "Untrusted Y.AI request")
    const controller = new AbortController()
    controllers.add(controller)
    const cancel = () => { if (!res.writableEnded) controller.abort() }
    req.once("aborted", cancel); res.once("close", cancel)
    try {
      const chunks: Buffer[] = []
      let size = 0
      for await (const chunk of req) {
        size += chunk.length
        if (size > 1048576) return fail(413, "Y.AI request too large")
        chunks.push(Buffer.from(chunk))
      }
      const key = await credential()
      const headers = new Headers()
      for (const [name, value] of Object.entries(req.headers)) {
        if (value !== undefined && !blocked.has(name.toLowerCase())) headers.set(name, [value].flat().join(", "))
      }
      headers.set("accept-encoding", "identity")
      const response = await gatewayFetch(gateway, key, fetcher)(gateway + path, {
        method: req.method, headers, body: req.method === "GET" ? undefined : Buffer.concat(chunks),
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(150000)]),
      })
      // The final fetch rejects redirects. Never expose a Location for native follow-up.
      if (response.status >= 300 && response.status < 400) {
        await response.body?.cancel()
        return fail(502, "Y.AI redirects are blocked")
      }
      const outgoing: Record<string, string> = { "cache-control": "no-store" }
      for (const [name, value] of response.headers) if (!blocked.has(name.toLowerCase())) outgoing[name] = value
      res.writeHead(response.status, outgoing)
      if (!response.body) return res.end()
      const stream = Readable.fromWeb(response.body as import("node:stream/web").ReadableStream)
      stream.once("error", () => res.destroy())
      await new Promise<void>(resolve => { res.once("finish", resolve); res.once("close", resolve); stream.pipe(res) })
    } catch {
      fail(502, "Y.AI request failed; check access or run opencode auth login --provider y.ai")
    } finally {
      controllers.delete(controller); req.off("aborted", cancel); res.off("close", cancel)
    }
  })
  server.requestTimeout = 0
  server.headersTimeout = 15000
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject)
    server.listen(0, "127.0.0.1", () => { server.off("error", reject); resolve() })
  })
  server.on("error", () => { closed = true; for (const controller of controllers) controller.abort() })
  const origin = "http://127.0.0.1:" + (server.address() as import("node:net").AddressInfo).port
  return {
    request(request: Request): Request {
      if (closed) throw new Error("Y.AI transport disposed")
      const target = gatewayTarget(gateway, request)
      const suffix = target.pathname.slice(new URL(gateway).pathname.replace(/\/$/, "").length)
      if (!((request.method === "POST" && suffix === "/v1/chat/completions") ||
            (request.method === "GET" && suffix === "/v1/models"))) throw new Error("Untrusted Y.AI method")
      const headers = new Headers(request.headers)
      for (const name of [...headers.keys()]) if (blocked.has(name.toLowerCase())) headers.delete(name)
      headers.set("x-yai-local", capability)
      return new Request(origin + suffix, { method: request.method, headers, body: request.body,
        redirect: "error", signal: request.signal, duplex: "half" } as RequestInit)
    },
    async close() {
      closed = true
      for (const controller of controllers) controller.abort()
      await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections() })
    },
  }
}
