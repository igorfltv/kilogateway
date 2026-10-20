/* No persistence, cookies, analytics or third-party resources. The fragment is a
   short-lived, one-use onboarding capability, never an IdP/gateway/PAT token. */
(() => {
  let ticket = location.hash.slice(1)
  history.replaceState(null, "", location.pathname)
  const endpoint = location.pathname + "/session"
  const status = document.getElementById("status"), form = document.getElementById("setup")
  const container = document.getElementById("systems"), save = document.getElementById("save")
  const controls = new Map()
  const messages = {
    mcp_pat_rejected: "PAT не принят. Проверьте токен и права доступа к системе.",
    mcp_jira_pat_rejected: "Jira не приняла PAT. Проверьте токен и его права, затем введите PAT заново или пропустите подключение.",
    mcp_confluence_pat_rejected: "Confluence не приняла PAT. Проверьте токен и его права, затем введите PAT заново или пропустите подключение.",
    mcp_invalid_pat: "Проверьте PAT: без пробелов и переносов строк.",
    mcp_setup_expired: "Время формы истекло или она уже использована. Повторите вход в терминале.",
    mcp_upstream_unavailable: "Система недоступна. Попробуйте позже или пропустите подключение.",
    mcp_upstream_rate_limited: "Система ограничила запросы. Подождите и повторите.",
    mcp_resource_not_found: "Адрес API не найден. Обратитесь к администратору gateway.",
  }
  async function request(method, body) {
    const response = await fetch(endpoint, {method, cache:"no-store", credentials:"omit", redirect:"error",
      headers: {Authorization: "Bearer " + ticket, ...(body ? {"Content-Type":"application/json"} : {})},
      body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(45000)})
    const value = await response.json()
    if (response.status === 404 && value?.error?.code === "mcp_setup_pending") return {pending:true}
    if (!response.ok) throw new Error(messages[value?.error?.code] || "Не удалось сохранить подключения. Повторите вход или обратитесь к администратору.")
    return value
  }
  function failure(error) {
    status.classList.add("error")
    status.textContent = error instanceof Error && Object.values(messages).includes(error.message)
      ? error.message : "Не удалось подключиться к gateway. Проверьте сеть и повторите вход."
  }
  async function start() {
    if (!/^ms_[\w-]+\.[\w-]+$/.test(ticket)) throw new Error(messages.mcp_setup_expired)
    let result = await request("GET")
    const deadline = Date.now() + 900000
    while (result.pending && ticket && Date.now() < deadline) {
      status.textContent = "Ожидаем завершения SSO в браузере…"
      await new Promise(resolve => setTimeout(resolve, 1000))
      result = await request("GET")
    }
    if (result.pending || !ticket) throw new Error(messages.mcp_setup_expired)
    for (const item of result.servers) {
      if (!["jira","confluence"].includes(item.id)) continue
      const fieldset = document.createElement("fieldset"), legend = document.createElement("legend")
      legend.textContent = item.name; fieldset.append(legend)
      const info = document.createElement("small")
      info.textContent = item.connected ? "PAT уже сохранён на gateway." : "Подключение ещё не настроено."
      const select = document.createElement("select"); select.id = item.id + "-action"
      for (const [value,label] of [["keep",item.connected ? "Оставить сохранённый PAT" : "Пропустить"],["replace","Ввести PAT"], ...(item.connected ? [["remove","Удалить PAT с gateway"]] : [])]) {
        const option = document.createElement("option"); option.value = value; option.textContent = label; select.append(option)
      }
      select.value = item.connected ? "keep" : "replace"
      const selectLabel = document.createElement("label"); selectLabel.htmlFor = select.id; selectLabel.textContent = "Действие"
      const input = document.createElement("input"); input.type = "password"; input.autocomplete = "new-password"
      input.id = item.id + "-pat"; input.minLength = 8; input.maxLength = 4096; input.spellcheck = false
      input.setAttribute("data-1p-ignore", "true"); input.setAttribute("data-lpignore", "true")
      const inputLabel = document.createElement("label"); inputLabel.htmlFor = input.id; inputLabel.textContent = "Персональный PAT"
      const toggle = () => { input.hidden = inputLabel.hidden = select.value !== "replace"; input.required = !input.hidden; if(input.hidden) input.value = "" }
      select.addEventListener("change",toggle); toggle()
      fieldset.append(info,selectLabel,select,inputLabel,input); container.append(fieldset)
      controls.set(item.id,{select,input})
    }
    form.hidden = false; status.textContent = "После сохранения вернитесь в терминал."
  }
  form.addEventListener("submit",async event => {
    event.preventDefault(); save.disabled = true; status.classList.remove("error"); status.textContent = "Проверяем PAT и сохраняем подключения…"
    let body = {}
    for (const [id,{select,input}] of controls) body[id] = {action:select.value, ...(select.value === "replace" ? {token:input.value} : {})}
    // Clear the DOM immediately; no token is returned by the server.
    for (const {input} of controls.values()) input.value = ""
    try {
      await request("POST", body); ticket = ""; controls.clear(); form.remove()
      status.textContent = "Готово. Подключения сохранены на gateway. Вернитесь в терминал — вход продолжится автоматически."
    } catch(error) { failure(error); save.disabled = false }
    finally { body = null }
  })
  window.addEventListener("pagehide", () => { ticket = ""; for(const {input} of controls.values()) input.value = "" })
  start().catch(failure)
})()
