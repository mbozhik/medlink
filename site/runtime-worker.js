/* The original FastAPI runs here, not a JavaScript imitation of its routes. */
const PYODIDE_BASE = "https://cdn.jsdelivr.net/pyodide/v0.29.4/full/";
let python;
let queue = Promise.resolve();

async function initialize() {
  importScripts(PYODIDE_BASE + "pyodide.js");
  python = await loadPyodide({ indexURL: PYODIDE_BASE });
  postMessage({ type: "progress", message: "Python загружен. Подключение FastAPI…" });
  await python.loadPackage(["fastapi", "httpx"]);
  const sourceResponse = await fetch(new URL("./api/main.py", self.location.href));
  if (!sourceResponse.ok) throw new Error("Не удалось загрузить api/main.py: " + sourceResponse.status);
  python.FS.writeFile("/home/pyodide/main.py", await sourceResponse.text());
  await python.runPythonAsync(`
import sys, json, httpx
sys.path.insert(0, "/home/pyodide")
import main

async def browser_request(raw):
    request = json.loads(raw)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="https://medlink.local") as client:
        response = await client.request(
            request["method"], request["path"],
            headers=request.get("headers", {}), content=request.get("body"))
        return json.dumps({"status": response.status_code,
            "statusText": response.reason_phrase,
            "headers": dict(response.headers), "body": response.text}, ensure_ascii=False)
`);
  postMessage({ type: "ready" });
}

async function handle(message) {
  if (message.type === "init") {
    await initialize();
    return;
  }
  if (!python) throw new Error("Python еще не загружен");
  if (message.type === "reset") {
    await python.runPythonAsync("main.store.reset()");
    return { reset: true };
  }
  if (message.type === "request") {
    python.globals.set("_browser_request_json", JSON.stringify(message.request));
    try {
      return JSON.parse(await python.runPythonAsync("await browser_request(_browser_request_json)"));
    } finally {
      python.globals.delete("_browser_request_json");
    }
  }
  throw new Error("Unknown worker message");
}

self.onmessage = ({ data }) => {
  // One queue serializes writes, reads, and resets to the in-memory store.
  queue = queue.then(async () => {
    try {
      const result = await handle(data);
      if (data.id !== undefined) postMessage({ id: data.id, result });
    } catch (error) {
      postMessage({ type: data.type === "init" ? "error" : "response", id: data.id, error: String(error.message || error) });
    }
  });
};
