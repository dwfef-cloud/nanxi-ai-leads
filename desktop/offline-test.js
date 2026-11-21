// Standalone offline desktop window. It never imports the live account agent.
const { app, BrowserWindow } = require("electron");
const path = require("path");
const fs = require("fs");
const http = require("http");
const { spawn } = require("child_process");

app.setName("nanxi-offline-test");
app.setPath("userData", path.join(app.getPath("appData"), "nanxi-offline-test"));
const portArgument = process.argv.find(value => value.startsWith("--offline-port="));
const port = portArgument ? Number(portArgument.split("=")[1]) : 8901;
if (!Number.isInteger(port) || port < 8901 || port > 8999) throw new Error("Invalid offline host port");
const BASE = "http://127.0.0.1:" + port;
const BACKEND = path.resolve(__dirname, "..", "backend");
let child = null;
let window = null;
async function available() {
  return new Promise(resolve => {
    const req = http.get(BASE + "/api/offline/runs", response => {
      response.resume(); resolve(response.statusCode === 200);
    });
    req.setTimeout(1000, () => { req.destroy(); resolve(false); });
    req.on("error", () => resolve(false));
  });
}
app.whenReady().then(async () => {
  if (!await available()) {
    const python = path.join(BACKEND, ".venv", "Scripts", "python.exe");
    const logdir = path.join(BACKEND, "logs");
    fs.mkdirSync(logdir, { recursive: true });
    child = spawn(python, ["-m", "uvicorn", "offline_server:app", "--host", "127.0.0.1", "--port", String(port)], {
      cwd: BACKEND, windowsHide: true, env: { ...process.env, PYTHONIOENCODING: "utf-8", SCHEDULER_OFF: "1" },
      stdio: ["ignore", fs.openSync(path.join(logdir, "offline-host.out.log"), "a"),
        fs.openSync(path.join(logdir, "offline-host.err.log"), "a")],
    });
    child.on("error", () => {});
    child.unref(); // Closing this panel leaves an in-progress offline run intact.
    for (let n = 0; n < 40 && !await available(); n++) await new Promise(resolve => setTimeout(resolve, 250));
  }
  window = new BrowserWindow({ width: 1180, height: 850, minWidth: 650, minHeight: 600,
    title: "离线测试 · 阿里山AI获客", backgroundColor: "#0b101b",
    webPreferences: { partition: "offline-testing", nodeIntegration: false, contextIsolation: true, sandbox: true },
  });
  window.setMenu(null);
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event, url) => {
    if (url !== BASE + "/ui/offline.html") event.preventDefault();
  });
  await window.loadURL(BASE + "/ui/offline.html");
});
app.on("window-all-closed", () => app.quit());
