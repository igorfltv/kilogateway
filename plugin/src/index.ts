import type { Plugin } from "@kilocode/plugin"
import { openBrowser } from "./browser.js"
import { begin, config, gatewayFetch } from "./device.js"
import { catalog, createMCPRelay, kiloCredential } from "./mcp.js"

const plugin: Plugin = async (_input, raw = {}) => {
  const opts = config(raw)
  const active = new Set<AbortController>()
  let relay: Awaited<ReturnType<typeof createMCPRelay>> | undefined
  return {
    dispose: async () => { for (const c of active) c.abort(); active.clear(); await relay?.close() },
    config: async (conf) => {
      // Pinned transport. Project configuration cannot move y.ai credentials to another host.
      conf.provider ??= {}
      conf.provider["y.ai"] ??= {}
      conf.provider["y.ai"].name = "Y.AI"
      conf.provider["y.ai"].npm = "@ai-sdk/openai-compatible@2.0.48"
      conf.provider["y.ai"].options = { ...conf.provider["y.ai"].options, baseURL: opts.gateway + "/v1" }
      const key = await kiloCredential(opts)
      if (key) {
        try {
          const servers = await catalog(opts, key)
          if (servers.length) {
            relay ??= await createMCPRelay(opts, () => kiloCredential(opts))
            conf.mcp ??= {}
            for (const server of servers) conf.mcp["y-ai-" + server.id] = {...relay.remote(server.id),enabled:true,timeout:10000}
          }
        } catch { process.stderr.write("Y.AI: MCP unavailable; check gateway access or repeat login.\n") }
      }
    },
    auth: {
      provider: "y.ai",
      loader: async (getAuth) => {
        const auth = await getAuth()
        if (auth.type !== "api") return {}
        if (auth.metadata?.company_gateway !== opts.gateway || auth.metadata?.company_issuer !== opts.issuer) {
          throw new Error("Company credential is bound to another gateway/issuer. Check trusted config and login again.")
        }
        return { baseURL: opts.gateway + "/v1", apiKey: auth.key, fetch: gatewayFetch(opts.gateway, auth.key) }
      },
      methods: [{ type: "oauth", label: "Company SSO", authorize: async () => {
        const controller = new AbortController()
        active.add(controller)
        const onSignal = () => controller.abort()
        process.once("SIGINT", onSignal); process.once("SIGTERM", onSignal)
        try {
          const flow = await begin(opts, fetch, controller.signal)
          if (opts.openBrowser) {
            openBrowser(flow.url)
          }
          return { url: flow.url, instructions: flow.instructions, method: "auto" as const, callback: async () => {
            try { return await flow.poll() } finally {
              active.delete(controller); process.removeListener("SIGINT", onSignal); process.removeListener("SIGTERM", onSignal)
            }
          } }
        } catch (e) {
          active.delete(controller); process.removeListener("SIGINT", onSignal); process.removeListener("SIGTERM", onSignal)
          throw e
        }
      } }],
    },
  }
}
export default plugin
