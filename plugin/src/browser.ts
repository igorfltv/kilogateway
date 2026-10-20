import { spawn } from "node:child_process"

export function openBrowser(url: string) {
  const command = process.platform === "darwin" ? "open" : process.platform === "win32" ? "explorer.exe" : "xdg-open"
  try {
    const child = spawn(command, [url], { stdio: "ignore", detached: true })
    child.on("error", () => {})
    child.unref()
  } catch {}
}
