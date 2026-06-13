const { app, BrowserWindow, dialog } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");

const BACKEND_PORT = 8765;
const HEALTH_URL = `http://127.0.0.1:${BACKEND_PORT}/api/health`;
const OLLAMA_URL = "http://127.0.0.1:11434/api/tags";

// Where the project (backend venv + frontend build) lives. In dev that's the
// parent dir; in a packaged .app __dirname is inside the bundle, so we read the
// absolute path baked in at build time (overridable via env for portability).
function resolveProjectRoot() {
  if (process.env.SWING_SCANNER_HOME) return process.env.SWING_SCANNER_HOME;
  if (app.isPackaged) {
    try {
      const cfg = JSON.parse(fs.readFileSync(path.join(__dirname, "app-config.json"), "utf8"));
      if (cfg.projectRoot) return cfg.projectRoot;
    } catch {
      /* fall through to dev default */
    }
  }
  return path.join(__dirname, "..");
}

const ROOT = resolveProjectRoot();

let backendProcess = null;
let ollamaProcess = null; // only set if WE started Ollama (so we only kill what we started)

// --- Ollama (local AI) ---

const isWindows = process.platform === "win32";

// The Python interpreter inside the project's virtualenv.
function venvPython() {
  const base = path.join(ROOT, "backend", ".venv");
  return isWindows ? path.join(base, "Scripts", "python.exe") : path.join(base, "bin", "python");
}

// A GUI app launched from the desktop has a minimal PATH, so look in the usual spots.
function findOllama() {
  if (isWindows) {
    const local = process.env.LOCALAPPDATA || "";
    const candidates = [
      path.join(local, "Programs", "Ollama", "ollama.exe"),
      "C:\\Program Files\\Ollama\\ollama.exe",
    ];
    return candidates.find((p) => fs.existsSync(p)) || null;
  }
  const candidates = ["/opt/homebrew/bin/ollama", "/usr/local/bin/ollama"];
  return candidates.find((p) => fs.existsSync(p)) || null;
}

// Read AI_PROVIDER from .env so we don't start Ollama when the user picked
// Claude or disabled AI entirely.
function aiProvider() {
  try {
    const env = fs.readFileSync(path.join(ROOT, ".env"), "utf8");
    const m = env.match(/^\s*AI_PROVIDER\s*=\s*(\w+)/m);
    return m ? m[1].toLowerCase() : "ollama";
  } catch {
    return "ollama";
  }
}

function checkOllama() {
  return new Promise((resolve) => {
    const req = http.get(OLLAMA_URL, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(800, () => {
      req.destroy();
      resolve(false);
    });
  });
}

// Ensure Ollama is running (start it if needed). Fire-and-forget — the AI phase
// only happens later when the user runs a scan, and the backend handles a
// not-yet-ready Ollama gracefully.
async function ensureOllama() {
  if (aiProvider() !== "ollama") return; // not using local AI
  if (await checkOllama()) return; // already running (don't adopt/kill it)
  const bin = findOllama();
  if (!bin) {
    console.warn("Ollama not found; AI analysis will be unavailable until it's installed.");
    return;
  }
  // OLLAMA_NUM_PARALLEL lets the backend analyze 2 stocks at once.
  ollamaProcess = spawn(bin, ["serve"], {
    env: { ...process.env, OLLAMA_NUM_PARALLEL: "2" },
    stdio: "ignore",
  });
  ollamaProcess.on("error", (e) => console.error("Failed to start Ollama:", e.message));
}

function checkHealth() {
  return new Promise((resolve) => {
    const req = http.get(HEALTH_URL, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(1000, () => {
      req.destroy();
      resolve(false);
    });
  });
}

function startBackend() {
  const backendDir = path.join(ROOT, "backend");
  const python = venvPython();
  if (!fs.existsSync(python)) {
    dialog.showErrorBox(
      "Backend not set up",
      `Python venv not found at ${python}.\n\nRun the setup script first (start.sh on macOS, start.ps1 on Windows).`,
    );
    app.quit();
    return null;
  }
  const proc = spawn(
    python,
    ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(BACKEND_PORT)],
    { cwd: backendDir, stdio: "inherit" },
  );
  proc.on("exit", (code) => {
    if (code !== null && code !== 0 && !app.isQuitting) {
      console.error(`Backend exited with code ${code}`);
    }
  });
  return proc;
}

async function waitForBackend(attempts = 60) {
  for (let i = 0; i < attempts; i++) {
    if (await checkHealth()) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

async function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: "#0b0e14",
    title: "Swing Scanner",
    // Inset traffic-light styling is macOS-only; Windows uses its native frame.
    ...(process.platform === "darwin" ? { titleBarStyle: "hiddenInset" } : {}),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  if (process.env.DEV) {
    // Dev mode: expects `npm run dev` running in frontend/
    await win.loadURL("http://localhost:5173");
    win.webContents.openDevTools({ mode: "detach" });
  } else {
    await win.loadFile(path.join(ROOT, "frontend", "dist", "index.html"));
  }
}

app.whenReady().then(async () => {
  // Start the local AI server (if used and not already up) — independent of the
  // backend, so kick it off without blocking startup.
  ensureOllama();

  // Reuse an already-running backend (e.g. started manually for debugging),
  // otherwise spawn our own.
  const alreadyRunning = await checkHealth();
  if (!alreadyRunning) {
    backendProcess = startBackend();
    if (!backendProcess) return;
    const up = await waitForBackend();
    if (!up) {
      dialog.showErrorBox(
        "Backend failed to start",
        "The Python backend did not respond on port 8765 within 30 seconds. Check the terminal output.",
      );
      app.quit();
      return;
    }
  }
  await createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("before-quit", () => {
  app.isQuitting = true;
  if (backendProcess) backendProcess.kill();
  if (ollamaProcess) ollamaProcess.kill(); // only set if we started it
});

app.on("window-all-closed", () => {
  app.quit();
});
