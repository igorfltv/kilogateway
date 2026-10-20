import type { Plugin, Credential } from "@opencode/plugin"
import { begin, config } from "./device.js"
import { openBrowser } from "./browser.js"
import { createOpenCodeTransport } from "./opencode-transport.js"

const providerID = "y.ai"
const methodID = "company-sso"

const plugin: Plugin.Plugin = {
  id: "company.y-ai.auth",
  async setup(ctx) {
    const opts = config(ctx.options)
    const registrations: { dispose(): Promise<void> }[] = []
    let pending: AbortController | undefined
    let disposed = false
    async function credential() {
      const connection = await ctx.integration.connection.active(providerID)
      const auth = connection ? await ctx.integration.connection.resolve(connection) : undefined
      if (auth?.type !== "oauth" || !auth.access.startsWith("cg_gc_") || auth.expires <= Date.now() ||
          auth.metadata?.company_gateway !== opts.gateway || auth.metadata?.company_issuer !== opts.issuer) {
        throw new Error("Run opencode auth login --provider y.ai")
      }
      return auth.access
    }
    const transport = await createOpenCodeTransport(opts.gateway, credential)
    const cleanup = async () => {
      disposed = true; pending?.abort()
      await transport.close()
      for (const item of registrations.reverse()) await item.dispose()
    }
    try {
      registrations.push(await ctx.provider.transform(editor => {
        // Native config providers may be contributed by a later transform.
        // Hooks below enforce the transport binding independently of that order.
        if (!editor.get(providerID)) return
        editor.update(providerID, provider => {
          provider.name = "Y.AI"
          provider.integrationID = providerID as unknown as typeof provider.integrationID
          provider.package = "@opencode/ai/providers/openai-compatible"
          provider.settings = { ...provider.settings, baseURL: opts.gateway + "/v1", apiKey: "yai-plugin-managed", transport: "http" }
          provider.headers = {}
        })
      }))
      registrations.push(await ctx.integration.transform(editor => {
        for (const method of editor.method.list(providerID)) editor.method.remove(providerID, method)
        if (editor.get(providerID)) editor.update(providerID, integration => { integration.name = "Y.AI" })
        editor.method.update({ integrationID: providerID,
          method: { id: methodID, type: "oauth", label: "Y.AI SSO" },
          async authorize() {
            pending?.abort()
            const controller = new AbortController()
            pending = controller
            const flow = await begin(opts, fetch, controller.signal, "opencode-cli")
            if (disposed || pending !== controller) { controller.abort(); throw new Error("Y.AI login cancelled") }
            if (opts.openBrowser) openBrowser(flow.url)
            const callback = flow.poll().then(result => {
              if (!Number.isFinite(flow.credentialExpiresAt) || flow.credentialExpiresAt! <= Date.now()) {
                throw new Error("Invalid Y.AI credential expiry")
              }
              // V2 OAuth methods persist OAuth-shaped values. Only the gateway
              // credential goes in access; there is no IdP/provider/refresh token.
              return { type: "oauth", methodID, access: result.key, refresh: "",
                expires: flow.credentialExpiresAt, metadata: result.metadata } as Credential.OAuth
            }).finally(() => { if (pending === controller) pending = undefined })
            void callback.catch(() => {})
            return { mode: "auto", url: flow.url, instructions: flow.instructions, callback }
          },
          async refresh() { throw new Error("Y.AI access expired. Run opencode auth login --provider y.ai") },
        })
      }))
      registrations.push(await ctx.session.hook("model.request", input => {
        input.baseURL = opts.gateway + "/v1"
        for (const name of Object.keys(input.headers)) {
          if (["authorization", "x-api-key", "api-key", "cookie"].includes(name.toLowerCase())) delete input.headers[name]
        }
      }, { providerID }))
      registrations.push(await ctx.session.hook("http.request", input => {
        input.request = transport.request(input.request)
      }, { providerID }))
      registrations.push(await ctx.session.hook("experimental.ws.handshake", () => {
        throw new Error("Y.AI uses HTTP transport")
      }, { providerID }))
      return cleanup
    } catch (error) {
      await cleanup()
      throw error
    }
  },
}
export default plugin
