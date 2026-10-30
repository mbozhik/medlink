"use strict";
const statusLine = document.getElementById("runtime-status");
const resetButton = document.getElementById("reset");
const retryButton = document.getElementById("retry");
const roleSelect = document.getElementById("role");
const nativeFetch = window.fetch.bind(window);
const pending = new Map();
let worker, nextId = 0, ready = false, ui;

function setStatus(message, error = false) {
  statusLine.textContent = message;
  statusLine.dataset.error = String(error);
}

function failRuntime(message) {
  ready = false;
  resetButton.disabled = roleSelect.disabled = true;
  retryButton.hidden = false;
  setStatus("Не удалось запустить Python. Проверьте интернет и повторите загрузку. " + message, true);
  for (const { reject } of pending.values()) reject(new Error(message));
  pending.clear();
}

function startRuntime() {
  if (worker) worker.terminate();
  ready = false;
  retryButton.hidden = true;
  setStatus("Загрузка Python… Первый запуск загружает среду выполнения и может занять несколько минут.");
  worker = new Worker(new URL("./runtime-worker.js", document.baseURI));
  worker.onerror = (event) => failRuntime(event.message || "Ошибка Web Worker");
  worker.onmessage = ({ data }) => {
    if (data.type === "progress") setStatus(data.message);
    else if (data.type === "ready") {
      ready = true;
      resetButton.disabled = roleSelect.disabled = false;
      setStatus("Python готов. Запросы выполняются в этой вкладке.");
    } else if (data.type === "error") failRuntime(data.error);
    else if (pending.has(data.id)) {
      const { resolve, reject } = pending.get(data.id);
      pending.delete(data.id);
      data.error ? reject(new Error(data.error)) : resolve(data.result);
    }
  };
  worker.postMessage({ type: "init" });
}

function callWorker(type, request) {
  if (!ready) return Promise.reject(new Error("Python еще загружается. Дождитесь сообщения о готовности."));
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    pending.set(id, { resolve, reject });
    worker.postMessage({ id, type, request });
  });
}

async function browserFetch(url, options = {}) {
  const target = new URL(url, document.baseURI);
  if (target.origin !== "https://medlink.local" || !target.pathname.startsWith("/api/v1/")) {
    return nativeFetch(url, options);
  }
  const headers = Object.fromEntries(new Headers(options.headers || {}).entries());
  const response = await callWorker("request", {
    method: options.method || "GET", path: target.pathname + target.search,
    headers, body: options.body == null ? null : String(options.body)
  });
  return new Response(response.body, {
    status: response.status, statusText: response.statusText, headers: response.headers
  });
}

function authorize() {
  if (roleSelect.value) {
    ui.preauthorizeApiKey("HTTPBearer", roleSelect.value);
    return;
  }
  // Swagger does not create its authorized map until the first authorization.
  // Calling logout before that initialization throws inside its reducer.
  const authorized = ui.authSelectors.authorized();
  if (authorized && authorized.has("HTTPBearer")) ui.authActions.logout(["HTTPBearer"]);
}

if (typeof SwaggerUIBundle !== "function") {
  failRuntime("Не загружен Swagger UI с CDN. Обновите страницу после восстановления соединения.");
} else {
  ui = SwaggerUIBundle({
    url: new URL("./openapi.json", document.baseURI).href,
    dom_id: "#swagger-ui", deepLinking: true, docExpansion: "list",
    defaultModelsExpandDepth: -1, displayRequestDuration: true,
    persistAuthorization: false, validatorUrl: null,
    requestInterceptor(request) {
      request.userFetch = browserFetch;
      return request;
    },
    onComplete: authorize
  });
  startRuntime();
}

roleSelect.addEventListener("change", authorize);
retryButton.addEventListener("click", startRuntime);
resetButton.addEventListener("click", async () => {
  resetButton.disabled = true;
  try {
    await callWorker("reset");
    setStatus("Данные сброшены. Тестовые записи и расписание восстановлены.");
  } catch (error) {
    setStatus("Не удалось сбросить данные: " + error.message, true);
  } finally { resetButton.disabled = !ready; }
});
