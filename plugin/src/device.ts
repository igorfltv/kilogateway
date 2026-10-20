import { randomUUID } from "node:crypto"
import { hostname } from "node:os"

export type Options = { issuer: string; gateway: string; clientId?: string; dev?: boolean; openBrowser?: boolean;
  discoveryUrl?: string; deviceUrl?: string; tokenUrl?: string; oidcAllowedOrigins?: string[] }
export type Fetcher = typeof fetch

export function trusted(value: string, dev = false): URL {
  const u = new URL(value)
  if (u.username || u.password || u.search || u.hash) throw new Error("Untrusted URL")
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(u.hostname)
  if (u.protocol !== "https:" && !(dev && loopback && u.protocol === "http:")) throw new Error("HTTPS required")
  return u
}

export function config(raw: Record<string, unknown> = {}): Options {
  const issuer = String(process.env.COMPANY_OIDC_ISSUER || raw.issuer || "")
  const gateway = String(process.env.COMPANY_GATEWAY_URL || raw.gateway || "")
  const dev = raw.dev === true || process.env.COMPANY_ALLOW_DEV_HTTP === "1"
  const i = trusted(issuer, dev); trusted(gateway, dev)
  const origins = raw.oidcAllowedOrigins ?? []
  if (!Array.isArray(origins) || origins.some(x => typeof x !== "string" || trusted(x, dev).pathname !== "/")) {
    throw new Error("OIDC allowlist requires origins")
  }
  const normalizedOrigins = origins.map(x=>trusted(x,dev).origin)
  const endpoints = {discoveryUrl: process.env.COMPANY_OIDC_DISCOVERY_URL || raw.discoveryUrl,
    deviceUrl: process.env.COMPANY_OIDC_DEVICE_URL || raw.deviceUrl,
    tokenUrl: process.env.COMPANY_OIDC_TOKEN_URL || raw.tokenUrl}
  for (const value of Object.values(endpoints)) {
    if (value && ![i.origin, ...normalizedOrigins].includes(trusted(String(value), dev).origin)) throw new Error("Untrusted OIDC endpoint")
  }
  return { issuer: issuer.replace(/\/$/, ""), gateway: gateway.replace(/\/$/, ""),
    clientId: String(raw.clientId || "kilo-cli"), dev, openBrowser: raw.openBrowser !== false,
    oidcAllowedOrigins: normalizedOrigins,
    ...Object.fromEntries(Object.entries(endpoints).filter(([,v])=>v).map(([k,v])=>[k,String(v)])) }
}

async function request(fetcher: Fetcher, url: string, init: RequestInit, signal?: AbortSignal) {
  try {
    return await fetcher(url, { ...init, redirect: "error", signal: signal
      ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000) })
  } catch {
    if (signal?.aborted) throw new Error("Company SSO cancelled")
    throw new Error("Company SSO network unavailable")
  }
}

async function exchangeFailure(response: Response): Promise<Error> {
  const messages = new Map([
    ["429:principal_provider_budget_exhausted", "Upstream-key allocation limit reached. Retrying login will not reset it; ask the gateway administrator to review the allocation limit or configure a static inference key."],
    ["429:principal_budget_exhausted", "Personal token quota exhausted; ask the gateway administrator to review the quota."],
    ["429:principal_rate_limited", "Too many requests; wait before trying again."],
    ["403:role_required", "SSO succeeded, but the account lacks the required Y.AI roles."],
    ["403:provider_not_allowed", "The account policy does not allow this inference provider."],
    ["403:principal_suspended", "The Y.AI account is suspended; contact the gateway administrator."],
    ["403:corporate_access_revoked", "Corporate access was revoked; contact the gateway administrator."],
    ["503:provisioning_reconciliation_required", "A previous upstream-key operation needs administrator reconciliation; do not repeat login."],
  ])
  try {
    const payload = await response.json() as { error?: { code?: unknown } } | null
    const code = payload?.error?.code
    const message = typeof code === "string" && messages.get(`${response.status}:${code}`)
    // Reflect only closed, known codes. Error bodies may contain sensitive upstream data.
    if (message) return new Error(`Y.AI gateway: ${message} (${code}; HTTP ${response.status})`)
  } catch {}
  return new Error(`Y.AI gateway exchange failed (HTTP ${response.status}); contact the gateway administrator`)
}

export const sleep = (ms: number, signal?: AbortSignal) => new Promise<void>((resolve, reject) => {
  if (signal?.aborted) return reject(new Error("Company SSO cancelled"))
  const onAbort = () => { clearTimeout(t); reject(new Error("Company SSO cancelled")) }
  const t = setTimeout(() => { signal?.removeEventListener("abort", onAbort); resolve() }, ms)
  signal?.addEventListener("abort", onAbort, { once: true })
})

export async function begin(opts: Options, fetcher: Fetcher = fetch, signal?: AbortSignal,
  client: "kilo-cli" | "opencode-cli" = "kilo-cli") {
  const issuer = trusted(opts.issuer, opts.dev)
  const origins = [issuer.origin, ...(opts.oidcAllowedOrigins || [])]
  const discoveryUrl = opts.discoveryUrl || opts.issuer + "/.well-known/openid-configuration"
  if (!origins.includes(trusted(discoveryUrl, opts.dev).origin)) throw new Error("Untrusted OIDC endpoint")
  const discovery = await request(fetcher, discoveryUrl, {}, signal)
  if (!discovery.ok) throw new Error("Company OIDC discovery failed")
  const d = await discovery.json() as Record<string, string>
  if (d.issuer !== opts.issuer) throw new Error("Company OIDC issuer mismatch")
  if (opts.deviceUrl) d.device_authorization_endpoint = opts.deviceUrl
  if (opts.tokenUrl) d.token_endpoint = opts.tokenUrl
  for (const field of ["device_authorization_endpoint", "token_endpoint"]) {
    if (!origins.includes(trusted(d[field], opts.dev).origin)) throw new Error("Untrusted OIDC endpoint")
  }
  const r = await request(fetcher, d.device_authorization_endpoint, { method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: opts.clientId || "kilo-cli", scope: "openid" }) }, signal)
  if (!r.ok) throw new Error("Company device authorization failed")
  const device = await r.json() as { device_code: string; user_code: string; verification_uri: string;
    verification_uri_complete?: string; expires_in: number; interval?: number }
  for (const url of [device.verification_uri, device.verification_uri_complete].filter(Boolean) as string[]) {
    if (!origins.includes(trusted(url.replace(/\?.*$/, ""), opts.dev).origin)) throw new Error("Untrusted verification URL")
    const parsed = new URL(url)
    if (parsed.username || parsed.password || parsed.hash) throw new Error("Untrusted verification URL")
  }
  if (!device.device_code || !device.user_code || !Number.isFinite(device.expires_in) || device.expires_in <= 0) {
    throw new Error("Invalid device response")
  }
  const deadline = Date.now() + Math.min(device.expires_in, 900) * 1000
  let interval = Math.max(1, Number(device.interval) || 5)
  const idem = randomUUID()
  let credentialExpiresAt: number | undefined
  return {
    get credentialExpiresAt() { return credentialExpiresAt },
    url: device.verification_uri_complete || device.verification_uri,
    instructions: `Y.AI SSO: open ${device.verification_uri}, enter code ${device.user_code}. Ctrl+C cancels.`,
    async poll(wait = sleep): Promise<{ type: "success"; key: string; metadata: Record<string, string> }> {
      const overall = AbortSignal.timeout(Math.max(1, deadline - Date.now()))
      const pollSignal = signal ? AbortSignal.any([signal, overall]) : overall
      try {
      let networkFailures = 0
      while (Date.now() < deadline) {
        await wait(Math.min(interval * 1000, deadline - Date.now()), pollSignal)
        if (Date.now() >= deadline) break
        let tokenResponse: Response
        try {
          tokenResponse = await request(fetcher, d.token_endpoint, { method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: new URLSearchParams({ client_id: opts.clientId || "kilo-cli",
              grant_type: "urn:ietf:params:oauth:grant-type:device_code", device_code: device.device_code }) }, pollSignal)
        } catch {
          if (signal?.aborted) throw new Error("Company SSO cancelled")
          if (++networkFailures > 3) throw new Error("Company SSO network unavailable")
          interval = Math.min(interval * 2, 30)
          continue
        }
        if (Date.now() >= deadline) throw new Error("Company SSO device code expired")
        if (tokenResponse.status >= 500) {
          if (++networkFailures > 3) throw new Error("Company SSO network unavailable")
          interval = Math.min(interval * 2, 30)
          continue
        }
        const t = await tokenResponse.json() as Record<string, string>
        if (t.error === "authorization_pending") continue
        if (t.error === "slow_down") { interval += 5; continue }
        if (t.error === "access_denied") throw new Error("Company SSO access denied")
        if (t.error === "expired_token") throw new Error("Company SSO device code expired")
        if (!tokenResponse.ok || !t.access_token || t.token_type?.toLowerCase() !== "bearer") throw new Error("Company token request failed")
        // Only access_token is used. Never persist a Keycloak refresh token or ID token.
        const body = JSON.stringify({ device_name: hostname().replace(/[^\w .@-]/g, "_").slice(0,80), client })
        for (let attempt = 0; attempt < 3; attempt++) {
          let response: Response
          try {
            response = await request(fetcher, opts.gateway + "/auth/cli/exchange", { method: "POST", body,
              headers: { Authorization: "Bearer " + t.access_token, "Content-Type": "application/json", "Idempotency-Key": idem } }, pollSignal)
          } catch {
            if (attempt === 2 || signal?.aborted) throw new Error("Company gateway exchange unavailable")
            await wait(1000, pollSignal); continue
          }
          if (!response.ok) throw await exchangeFailure(response)
          const result = await response.json() as { credential?: { key?: string; expires_at?: string }; base_url?: string; provider?: string }
          if (result.provider !== "company-ai" || result.base_url !== opts.gateway + "/v1" || !result.credential?.key?.startsWith("cg_gc_")) {
            throw new Error("Invalid company gateway exchange response")
          }
          credentialExpiresAt = result.credential.expires_at ? Date.parse(result.credential.expires_at) : undefined
          return { type: "success", key: result.credential.key,
            metadata: { company_gateway: opts.gateway, company_issuer: opts.issuer } }
        }
      }
      throw new Error("Company SSO device code expired")
      } catch (e) {
        if (signal?.aborted) throw new Error("Company SSO cancelled")
        if (overall.aborted || Date.now() >= deadline) throw new Error("Company SSO device code expired")
        throw e
      }
    },
  }
}

export function gatewayTarget(gateway: string, input: RequestInfo | URL): URL {
  const allowed = new URL(gateway)
  const prefix = allowed.pathname.replace(/\/$/, "")
  const u = new URL(input instanceof Request ? input.url : String(input))
    if (u.origin !== allowed.origin || u.username || u.password || u.search || u.hash ||
      ![prefix+"/v1/chat/completions", prefix+"/v1/models"].includes(u.pathname)) throw new Error("Blocked untrusted gateway request")
  return u
}

export function gatewayFetch(gateway: string, key: string, fetcher: Fetcher = fetch): Fetcher {
  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    const u = gatewayTarget(gateway, input)
    const original = new Headers(init?.headers || (input instanceof Request ? input.headers : undefined))
    const headers = new Headers({ Authorization: "Bearer " + key })
    for (const name of ["content-type", "accept"]) if (original.has(name)) headers.set(name, original.get(name)!)
    const res = await fetcher(input, { ...init, headers, redirect: "error" })
    if (res.status === 401) throw new Error("Company access expired or revoked. Run kilo auth login --provider y.ai")
    return res
  }) as Fetcher
}
