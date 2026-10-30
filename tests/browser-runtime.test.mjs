/** Real Python/WASM parity check. Run: NODE_PATH=<tools>/node_modules node --test tests/browser-runtime.test.mjs */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import test from "node:test";

const root = new URL("../", import.meta.url);

test("Swagger bridge intercepts only virtual API and preserves headers/body", async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { value: "patient-token", dataset: {}, addEventListener() {} });
    return elements.get(id);
  };
  let options, message, authorized;
  const authorizations = [], logouts = [];
  const network = [];
  class Worker {
    constructor() { this.requests = []; }
    postMessage(data) { message = data; }
    terminate() {}
  }
  const context = vm.createContext({ URL, Headers, Response, Map, Promise,
    document: { baseURI: "https://mbozhik.github.io/medlink/", getElementById: element },
    window: { fetch: async (...args) => { network.push(args); return "network response"; } },
    Worker,
    SwaggerUIBundle(config) {
      options = config;
      return {
        authSelectors: { authorized: () => authorized },
        authActions: { logout(schemes) { if (!authorized) throw new Error("logout before auth initialization"); logouts.push(schemes); authorized = new Map(); } },
        preauthorizeApiKey(scheme, token) { authorizations.push({ scheme, token }); authorized = new Map([[scheme, token]]); }
      };
    }
  });
  vm.runInContext(await readFile(new URL("site/app.js", root), "utf8"), context);
  options.onComplete();
  assert.deepEqual(authorizations, [{ scheme: "HTTPBearer", token: "patient-token" }]);
  assert.equal(logouts.length, 0, "initialization must not logout an absent auth map");
  element("role").value = "doctor-token";
  vm.runInContext("authorize()", context);
  assert.equal(authorized.get("HTTPBearer"), "doctor-token");
  element("role").value = "";
  vm.runInContext("authorize()", context);
  assert.equal(logouts.length, 1);
  // Selecting anonymous before any authorization is also safe.
  authorized = undefined;
  vm.runInContext("authorize()", context);
  assert.equal(logouts.length, 1);
  vm.runInContext('worker.onmessage({data:{type:"ready"}})', context);
  const intercepted = options.requestInterceptor({ url: "https://medlink.local/api/v1/appointments" });
  assert.equal(typeof intercepted.userFetch, "function");
  assert.equal(await intercepted.userFetch("https://mbozhik.github.io/medlink/openapi.json"), "network response");
  assert.equal(network.length, 1);
  const responsePromise = intercepted.userFetch("https://medlink.local/api/v1/appointments?test=1", {
    method: "POST", headers: { Authorization: "Bearer patient-token", "Content-Type": "application/json" },
    body: '{"slotId":101}'
  });
  assert.equal(message.request.path, "/api/v1/appointments?test=1");
  assert.equal(message.request.headers.authorization, "Bearer patient-token");
  assert.equal(message.request.body, '{"slotId":101}');
  vm.runInContext(`worker.onmessage({data:{id:${message.id},result:{status:201,statusText:"Created",headers:{"content-type":"application/json"},body:'{"id":1003}'}}})`, context);
  const response = await responsePromise;
  assert.equal(response.status, 201);
  assert.deepEqual(await response.json(), { id: 1003 });
});

test("worker executes original FastAPI in Pyodide with real auth, mutations, and reset", { timeout: 240000 }, async () => {
  const require = createRequire(import.meta.url);
  const { loadPyodide } = require("pyodide");
  const source = await readFile(new URL("api/main.py", root), "utf8");
  assert((await readFile(new URL("site/api/main.py", root), "utf8")) === source, "rebuild site after API changes");
  let python;
  const messages = [];
  const context = vm.createContext({
    URL, Promise, JSON, Error,
    self: { location: { href: "https://mbozhik.github.io/medlink/runtime-worker.js" } },
    importScripts() {},
    loadPyodide: async () => {
      python = await loadPyodide({ packageBaseUrl: "https://cdn.jsdelivr.net/pyodide/v0.29.4/full/" });
      return python;
    },
    fetch: async () => ({ ok: true, text: async () => source }),
    postMessage: data => messages.push(data)
  });
  vm.runInContext(await readFile(new URL("site/runtime-worker.js", root), "utf8"), context);
  async function send(data) {
    context.self.onmessage({ data });
    await vm.runInContext("queue", context);
    const error = messages.find(m => m.error);
    assert.equal(error, undefined, JSON.stringify(error));
    return messages.findLast(m => m.id === data.id)?.result;
  }
  await send({ type: "init" });
  assert(messages.some(m => m.type === "ready"));
  let id = 0;
  const request = (method, path, body, headers = {}) => send({ id: ++id, type: "request", request: {
    method, path, headers: { "content-type": "application/json", ...headers },
    body: body === undefined ? null : JSON.stringify(body)
  } });
  const patient = { authorization: "Bearer patient-token" };
  assert.equal((await request("GET", "/api/v1/patients/me/appointments")).status, 401);
  const doctors = await request("GET", "/api/v1/doctors");
  assert.equal(doctors.status, 200);
  assert.equal(JSON.parse(doctors.body).length, 4);
  const tomorrow = new Date(Date.now() + 86400000).toLocaleDateString("sv-SE", { timeZone: "Europe/Moscow" });
  const slots = JSON.parse((await request("GET", "/api/v1/doctors/1/slots?date=" + tomorrow)).body);
  const slot = slots.find(s => s.isAvailable);
  assert(slot, "fixture has a future free slot");
  const before = JSON.parse((await request("GET", "/api/v1/patients/me/appointments", undefined, patient)).body).length;
  const created = await request("POST", "/api/v1/appointments", { slotId: slot.id, reason: "WASM parity test" }, patient);
  assert.equal(created.status, 201, created.body);
  const appointment = JSON.parse(created.body);
  assert.equal(appointment.status, "RESERVED");
  assert.equal(appointment.bookingRevision, 1);
  assert.equal((await request("POST", "/api/v1/appointments", { slotId: slot.id }, patient)).status, 409);
  const payment = await request("POST", `/api/v1/appointments/${appointment.id}/payment`, undefined, patient);
  assert.equal(payment.status, 201, payment.body);
  const paymentId = JSON.parse(payment.body).paymentId;
  assert.equal((await request("POST", "/api/v1/payments/webhook", { paymentId, status: "succeeded" }, { "x-webhook-signature": "test-signature" })).status, 200);
  const paid = JSON.parse((await request("GET", `/api/v1/appointments/${appointment.id}`, undefined, patient)).body);
  assert.equal(paid.status, "PAID");
  const missingRevision = await request("POST", "/api/v1/integrations/mis/booking-result", { appointmentId: appointment.id, result: "confirmed", misAppointmentId: "MIS-WASM" }, { "x-internal-token": "mis-stub-token" });
  assert.equal(missingRevision.status, 422);
  const confirmed = await request("POST", "/api/v1/integrations/mis/booking-result", {
    appointmentId: appointment.id, bookingRevision: 1, result: "confirmed", misAppointmentId: "MIS-WASM"
  }, { "x-internal-token": "mis-stub-token" });
  assert.equal(confirmed.status, 200);
  assert.equal(JSON.parse((await request("GET", `/api/v1/appointments/${appointment.id}`, undefined, patient)).body).status, "CONFIRMED");
  await send({ id: ++id, type: "reset" });
  const after = JSON.parse((await request("GET", "/api/v1/patients/me/appointments", undefined, patient)).body).length;
  assert.equal(after, before);
  console.log("Pyodide package versions:", await python.runPythonAsync('f"FastAPI {main.FastAPI.__module__}; httpx {httpx.__version__}"'));
});
