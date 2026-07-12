// Self-test for the per-seat cross-endpoint model router (model-router.mjs).
//
// NO network, NO real keys. Two STUB upstream http servers on localhost stand in
// for the backends (an x-api-key one for "Anthropic", a Bearer one). The router
// is driven through its REAL http path (createRouter → a listening server → real
// http.request from a test client), so routing, the auth swap, body + streamed
// response passthrough, fail-closed behaviour, and log safety are all exercised
// on the real socket, not a mock.
//
// NOTE on the Bearer route: the official DeepSeek Anthropic-compatible endpoint
// authenticates with `x-api-key` (verified 2026-06-30; the example config uses
// that). The Bearer stub here exists purely to exercise the router's scheme-SWAP
// code path — proving the MECHANISM can emit `Authorization: Bearer <key>` for any
// backend that needs it, not a claim about DeepSeek.
//
// Run: node src/adapter/model-router.test.mjs

import http from "node:http";
import {
  createRouter,
  pickRoute,
  globToRegExp,
  modelFromBody,
  resolveAuthHeader,
  isPassthrough,
  bodyHasImageBlock,
  isImageUnsupported400,
  pickVisionRoute,
  validateConfig,
  rewriteModelInBody,
} from "./model-router.mjs";

let pass = 0;
const fails = [];
function check(name, got, want) {
  if (got === want) { pass++; return; }
  fails.push(`  ✗ ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

// A stub upstream: records every request it receives, then answers with a body
// streamed in three separate writes (proving chunked/streamed passthrough).
const STREAM_PARTS = ["chunk-A;", "chunk-B;", "chunk-C"];
const STREAM_BODY = STREAM_PARTS.join("");
function makeStub() {
  const received = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      received.push({ method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString("utf8") });
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.write(STREAM_PARTS[0]);
      res.write(STREAM_PARTS[1]);
      res.end(STREAM_PARTS[2]);
    });
  });
  return { server, received };
}

// A mode-switchable stub for the vision-fallback scenarios. `mode`:
//   "ok"      → streamed 200 (the multimodal / vision-success path)
//   "reject"  → 400 with the image-unsupported signal body
//   "badreq"  → 400 with an ambiguous (non-image) error body
function makeModeStub(initialMode) {
  const received = [];
  let mode = initialMode;
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      received.push({ url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString("utf8") });
      if (mode === "ok") {
        res.writeHead(200, { "content-type": "text/event-stream" });
        res.write(STREAM_PARTS[0]);
        res.write(STREAM_PARTS[1]);
        res.end(STREAM_PARTS[2]);
        return;
      }
      const message = mode === "reject"
        ? "this model does not support image blocks"
        : "messages: roles must alternate between user and assistant";
      res.writeHead(400, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: { message } }));
    });
  });
  return { server, received, setMode: (m) => { mode = m; } };
}

function listen(server) {
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
}
function close(server) {
  return new Promise((resolve) => server.close(resolve));
}

// Send a request THROUGH the router. `raw` (string) sends arbitrary bytes; else a
// JS object is JSON-encoded. Always carries a bogus client front-key so we can
// prove the router strips it before forwarding.
function request(port, payload, { raw } = {}) {
  return new Promise((resolve, reject) => {
    const data = Buffer.from(raw !== undefined ? raw : JSON.stringify(payload));
    const req = http.request(
      {
        hostname: "127.0.0.1", port, path: "/v1/messages", method: "POST",
        headers: {
          "content-type": "application/json",
          "x-api-key": "CLIENT-FRONT-KEY",
          "anthropic-version": "2023-06-01",
          "content-length": data.length,
        },
      },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString("utf8"), headers: res.headers }));
      },
    );
    req.on("error", reject);
    req.write(data);
    req.end();
  });
}

// A tolerant client for the mid-stream-abort case: resolves with whatever partial
// body arrived no matter HOW the connection ended (clean end, aborted, error, or a
// req-side error) — the point is to prove the router survives an upstream mid-stream
// drop without crashing, and that the partial reached the client.
function requestTolerant(port, payload) {
  return new Promise((resolve) => {
    const data = Buffer.from(JSON.stringify(payload));
    let status = 0;
    const chunks = [];
    let done = false;
    const finish = (how) => {
      if (done) return;
      done = true;
      resolve({ status, body: Buffer.concat(chunks).toString("utf8"), how });
    };
    const req = http.request(
      {
        hostname: "127.0.0.1", port, path: "/v1/messages", method: "POST",
        headers: { "content-type": "application/json", "x-api-key": "CLIENT-FRONT-KEY", "content-length": data.length },
      },
      (res) => {
        status = res.statusCode;
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => finish("end"));
        res.on("aborted", () => finish("aborted"));
        res.on("error", () => finish("error"));
        res.on("close", () => finish("close"));
      },
    );
    req.on("error", () => finish("req-error"));
    req.write(data);
    req.end();
  });
}

// Bind a throwaway server to an ephemeral port, then close it — returns a port that
// is now free, so a route pointed at it connection-refuses (the 502 upstream case).
function closedPort() {
  return new Promise((resolve) => {
    const s = http.createServer();
    s.listen(0, "127.0.0.1", () => {
      const port = s.address().port;
      s.close(() => resolve(port));
    });
  });
}

async function main() {
  // ── pure-function units (no network) ──────────────────────────────────────
  check("glob claude-* matches", globToRegExp("claude-*").test("claude-opus-4-8"), true);
  check("glob claude-* rejects deepseek", globToRegExp("claude-*").test("deepseek-chat"), false);
  check("glob escapes the dot (no wildcard)", globToRegExp("claude-4.8").test("claude-4x8"), false);
  check("modelFromBody reads model", modelFromBody(Buffer.from('{"model":"m-1"}')), "m-1");
  check("modelFromBody bad json ⇒ null", modelFromBody(Buffer.from("not json")), null);
  check("modelFromBody no model ⇒ null", modelFromBody(Buffer.from("{}")), null);
  check("modelFromBody empty ⇒ null", modelFromBody(Buffer.from("")), null);

  const sampleRoutes = [
    { match: "claude-*", base_url: "https://api.anthropic.com", auth: { header: "x-api-key", keyEnv: "K" } },
    { match: "deepseek-*", base_url: "https://api.deepseek.com/anthropic", auth: { header: "x-api-key", keyEnv: "K" } },
  ];
  check("pickRoute first match wins", pickRoute("claude-x", sampleRoutes).match, "claude-*");
  check("pickRoute unknown ⇒ null", pickRoute("gpt-4", sampleRoutes), null);
  check("pickRoute empty model ⇒ null", pickRoute("", sampleRoutes), null);
  check("resolveAuthHeader raw value", resolveAuthHeader(sampleRoutes[0], { K: "secret" }).value, "secret");
  check("resolveAuthHeader scheme prepend",
    resolveAuthHeader({ auth: { header: "Authorization", keyEnv: "K", scheme: "Bearer" } }, { K: "secret" }).value,
    "Bearer secret");
  let unsetThrew = false;
  try { resolveAuthHeader(sampleRoutes[0], {}); } catch { unsetThrew = true; }
  check("resolveAuthHeader unset env throws (fail-closed)", unsetThrew, true);
  check("isPassthrough true for string \"passthrough\"", isPassthrough({ auth: "passthrough" }), true);
  check("isPassthrough false for a key-swap object", isPassthrough(sampleRoutes[0]), false);

  // ── vision-fallback pure units (no network) ───────────────────────────────
  const imageBody = Buffer.from(JSON.stringify({
    model: "m", messages: [{ role: "user", content: [
      { type: "image", source: { type: "base64", media_type: "image/png", data: "AAAA" } },
      { type: "text", text: "hi" },
    ] }],
  }));
  check("bodyHasImageBlock detects an image block", bodyHasImageBlock(imageBody), true);
  check("bodyHasImageBlock false for text-only content array",
    bodyHasImageBlock(Buffer.from('{"model":"m","messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}]}')), false);
  check("bodyHasImageBlock false for a string content turn",
    bodyHasImageBlock(Buffer.from('{"model":"m","messages":[{"role":"user","content":"hi"}]}')), false);
  check("bodyHasImageBlock false for bad json", bodyHasImageBlock(Buffer.from("not json")), false);
  check("bodyHasImageBlock false for empty", bodyHasImageBlock(Buffer.from("")), false);

  const imageErr = Buffer.from('{"error":{"message":"this model does not support image blocks"}}');
  check("isImageUnsupported400 matches the image-unsupported signal", isImageUnsupported400(400, imageErr), true);
  check("isImageUnsupported400 ignores a non-400 status", isImageUnsupported400(500, imageErr), false);
  check("isImageUnsupported400 rejects an ambiguous 400",
    isImageUnsupported400(400, Buffer.from('{"error":{"message":"messages: roles must alternate between user and assistant"}}')), false);
  check("isImageUnsupported400 rejects a 400 mentioning image without support/block",
    isImageUnsupported400(400, Buffer.from('{"error":{"message":"image bytes were truncated"}}')), false);
  check("isImageUnsupported400 false for bad json", isImageUnsupported400(400, Buffer.from("not json")), false);

  const visionRoutes = [
    { match: "nonvis-*", base_url: "https://a.example", auth: { header: "x-api-key", keyEnv: "K" } },
    { match: "vis-*", base_url: "https://b.example", forImages: true, forImagesModel: "vendor/vis", auth: { header: "x-api-key", keyEnv: "K" } },
  ];
  check("pickVisionRoute returns the forImages route", pickVisionRoute(visionRoutes).match, "vis-*");
  check("pickVisionRoute null when none flagged", pickVisionRoute(sampleRoutes), null);
  check("validateConfig accepts one forImages route",
    validateConfig({ routes: visionRoutes }).routes.length, 2);
  let twoVisionThrew = false;
  try {
    validateConfig({ routes: [
      { match: "a-*", base_url: "https://a.example", forImages: true, forImagesModel: "vendor/a", auth: { header: "x-api-key", keyEnv: "K" } },
      { match: "b-*", base_url: "https://b.example", forImages: true, forImagesModel: "vendor/b", auth: { header: "x-api-key", keyEnv: "K" } },
    ] });
  } catch { twoVisionThrew = true; }
  check("validateConfig rejects two forImages routes", twoVisionThrew, true);
  let badVisionThrew = false;
  try {
    validateConfig({ routes: [{ match: "a-*", base_url: "https://a.example", forImages: "yes", auth: { header: "x-api-key", keyEnv: "K" } }] });
  } catch { badVisionThrew = true; }
  check("validateConfig rejects a non-true forImages", badVisionThrew, true);
  // The forImagesModel contract added by the synced modelpipe transport (the vision
  // reroute crosses to a different provider, so the forImages route MUST name the model
  // id to rewrite to — fail-closed at startup, never a guaranteed runtime 400).
  check("rewriteModelInBody replaces the model id",
    modelFromBody(rewriteModelInBody(Buffer.from('{"model":"glm-x","messages":[]}'), "vendor/m")), "vendor/m");
  check("rewriteModelInBody preserves other fields",
    JSON.parse(rewriteModelInBody(Buffer.from('{"model":"glm-x","messages":[1]}'), "vendor/m").toString()).messages[0], 1);
  check("rewriteModelInBody falsy newModel ⇒ body unchanged",
    rewriteModelInBody(Buffer.from('{"model":"glm-x"}'), "").toString(), '{"model":"glm-x"}');
  check("rewriteModelInBody bad json ⇒ body unchanged (fail-safe)",
    rewriteModelInBody(Buffer.from("not json"), "vendor/m").toString(), "not json");
  let missingModelThrew = false;
  try {
    validateConfig({ routes: [{ match: "vis-*", base_url: "https://b.example", forImages: true, auth: { header: "x-api-key", keyEnv: "K" } }] });
  } catch { missingModelThrew = true; }
  check("validateConfig rejects forImages route with no forImagesModel (fail-closed)", missingModelThrew, true);
  let strayModelThrew = false;
  try {
    validateConfig({ routes: [{ match: "x-*", base_url: "https://b.example", forImagesModel: "vendor/x", auth: { header: "x-api-key", keyEnv: "K" } }] });
  } catch { strayModelThrew = true; }
  check("validateConfig rejects forImagesModel without forImages", strayModelThrew, true);
  let emptyModelThrew = false;
  try {
    validateConfig({ routes: [{ match: "vis-*", base_url: "https://b.example", forImages: true, forImagesModel: "", auth: { header: "x-api-key", keyEnv: "K" } }] });
  } catch { emptyModelThrew = true; }
  check("validateConfig rejects an empty forImagesModel", emptyModelThrew, true);

  // ── e2e through the real socket ───────────────────────────────────────────
  const anthropicStub = makeStub();
  const bearerStub = makeStub();
  const passthroughStub = makeStub();
  const aPort = await listen(anthropicStub.server);
  const dPort = await listen(bearerStub.server);
  const pPort = await listen(passthroughStub.server);
  const deadPort = await closedPort(); // nothing listens here ⇒ upstream connect refused

  // A stub that begins a streamed response then DROPS its socket mid-stream — the
  // upstream-failure-after-headers case (sendError's headersSent branch).
  const midStub = http.createServer((req, res) => {
    req.resume();
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.write("PARTIAL-CHUNK;");
    setTimeout(() => res.socket.destroy(), 15);
  });
  const mPort = await listen(midStub);

  process.env.TEST_ANTHROPIC_KEY = "ANT-SECRET-123";
  process.env.TEST_DEEPSEEK_KEY = "DS-SECRET-456";
  delete process.env.TEST_UNSET_KEY_XYZ;
  process.env.MODEL_ROUTER_LOG = "1";

  // Tee stderr so the routing line is captured AND still visible.
  const realStderrWrite = process.stderr.write.bind(process.stderr);
  const logCapture = [];
  process.stderr.write = (chunk, ...rest) => { logCapture.push(String(chunk)); return realStderrWrite(chunk, ...rest); };

  const config = {
    listen: { host: "127.0.0.1", port: 0 },
    maxBodyBytes: 2048, // small cap so an oversize-body request trips the 413 path
    routes: [
      { match: "claude-*", base_url: `http://127.0.0.1:${aPort}`, auth: { header: "x-api-key", keyEnv: "TEST_ANTHROPIC_KEY" } },
      { match: "deepseek-*", base_url: `http://127.0.0.1:${dPort}`, auth: { header: "Authorization", scheme: "Bearer", keyEnv: "TEST_DEEPSEEK_KEY" } },
      { match: "needkey-*", base_url: `http://127.0.0.1:${aPort}`, auth: { header: "x-api-key", keyEnv: "TEST_UNSET_KEY_XYZ" } },
      // passthrough: forward the client's auth verbatim, no backend key swap.
      { match: "passthru-*", base_url: `http://127.0.0.1:${pPort}`, auth: "passthrough" },
      // upstream connect-refused ⇒ 502; mid-stream socket drop ⇒ headersSent abort.
      { match: "dead-*", base_url: `http://127.0.0.1:${deadPort}`, auth: { header: "x-api-key", keyEnv: "TEST_ANTHROPIC_KEY" } },
      { match: "midstream-*", base_url: `http://127.0.0.1:${mPort}`, auth: { header: "x-api-key", keyEnv: "TEST_ANTHROPIC_KEY" } },
    ],
  };
  const router = createRouter(config);
  const routerPort = await listen(router);

  try {
    // 1. claude-* → the x-api-key (Anthropic) backend; key value swapped; body intact.
    const r1 = await request(routerPort, { model: "claude-opus-4-8", messages: [{ role: "user", content: "BODY-SENTINEL-CLAUDE" }] });
    check("claude routed to anthropic stub", anthropicStub.received.length, 1);
    check("claude did not touch bearer stub", bearerStub.received.length, 0);
    check("claude status 200", r1.status, 200);
    check("claude streamed response intact", r1.body, STREAM_BODY);
    const aReq = anthropicStub.received[0];
    check("anthropic got backend key under x-api-key", aReq.headers["x-api-key"], "ANT-SECRET-123");
    check("anthropic did NOT get the client front-key", aReq.headers["x-api-key"] !== "CLIENT-FRONT-KEY", true);
    check("anthropic got no Authorization header", aReq.headers["authorization"], undefined);
    check("anthropic host rewritten to backend", aReq.headers["host"], `127.0.0.1:${aPort}`);
    check("anthropic request body passed through intact", JSON.parse(aReq.body).messages[0].content, "BODY-SENTINEL-CLAUDE");

    // 2. deepseek-* → the Bearer backend; auth scheme swapped; x-api-key stripped.
    const r2 = await request(routerPort, { model: "deepseek-chat", messages: [{ role: "user", content: "BODY-SENTINEL-DS" }] });
    check("deepseek routed to bearer stub", bearerStub.received.length, 1);
    check("deepseek did not re-touch anthropic stub", anthropicStub.received.length, 1);
    check("deepseek status 200", r2.status, 200);
    check("deepseek streamed response intact", r2.body, STREAM_BODY);
    const dReq = bearerStub.received[0];
    check("deepseek got Authorization: Bearer <backend key>", dReq.headers["authorization"], "Bearer DS-SECRET-456");
    check("deepseek had client x-api-key stripped", dReq.headers["x-api-key"], undefined);
    check("deepseek request body passed through intact", JSON.parse(dReq.body).messages[0].content, "BODY-SENTINEL-DS");

    // 3. unknown model fails closed (4xx) and reaches NO backend.
    const r3 = await request(routerPort, { model: "gpt-4-turbo", messages: [] });
    check("unknown model ⇒ 4xx", r3.status >= 400 && r3.status < 500, true);
    check("unknown model not forwarded to anthropic", anthropicStub.received.length, 1);
    check("unknown model not forwarded to bearer", bearerStub.received.length, 1);

    // 4. missing model + unparseable body fail closed.
    const r4 = await request(routerPort, { messages: [] });
    check("missing model ⇒ 4xx", r4.status >= 400 && r4.status < 500, true);
    const r5 = await request(routerPort, null, { raw: "this is not json" });
    check("unparseable body ⇒ 4xx", r5.status >= 400 && r5.status < 500, true);

    // 5. matched route but unset key env ⇒ fail closed, NOT forwarded.
    const r6 = await request(routerPort, { model: "needkey-1", messages: [] });
    check("unset key env ⇒ error status", r6.status >= 400, true);
    check("unset key env not forwarded", anthropicStub.received.length, 1);

    // 6. log safety: the model→host line is present; NO key/body/header anywhere.
    const logText = logCapture.join("");
    check("log names model -> host", logText.includes(`claude-opus-4-8 -> 127.0.0.1:${aPort}`), true);
    check("log leaks NO backend key", logText.includes("ANT-SECRET-123") || logText.includes("DS-SECRET-456"), false);
    check("log leaks NO client front-key", logText.includes("CLIENT-FRONT-KEY"), false);
    check("log leaks NO request body", logText.includes("BODY-SENTINEL"), false);

    // 7. passthrough (B): the client's auth header is forwarded UNCHANGED — no
    //    backend key swap, no strip. Proves auth:"passthrough" works for a
    //    subscription/OAuth session that has no backend key.
    const r7 = await request(routerPort, { model: "passthru-1", messages: [{ role: "user", content: "BODY-SENTINEL-PT" }] });
    check("passthrough status 200", r7.status, 200);
    check("passthrough streamed response intact", r7.body, STREAM_BODY);
    const pReq = passthroughStub.received[0];
    check("passthrough forwarded the client x-api-key VERBATIM", pReq.headers["x-api-key"], "CLIENT-FRONT-KEY");
    check("passthrough sent NO Authorization swap", pReq.headers["authorization"], undefined);
    check("passthrough body passed through intact", JSON.parse(pReq.body).messages[0].content, "BODY-SENTINEL-PT");
    // and the contrast: the key-swap route (case 1) DID strip the client key + swap.
    check("non-passthrough still strips client key + swaps backend key",
      anthropicStub.received[0].headers["x-api-key"] === "ANT-SECRET-123" &&
        anthropicStub.received[0].headers["x-api-key"] !== "CLIENT-FRONT-KEY", true);

    // 8. A1 — 413: a body over maxBodyBytes fails closed with 413, not forwarded.
    const big = "x".repeat(5000); // > 2048 cap
    const r8 = await request(routerPort, { model: "claude-opus-4-8", filler: big });
    check("oversize body ⇒ 413", r8.status, 413);
    check("oversize body NOT forwarded to backend", anthropicStub.received.length, 1);

    // 9. A1 — 502: the upstream connection is refused (nothing listens) ⇒ 502.
    const r9 = await request(routerPort, { model: "dead-1", messages: [] });
    check("upstream connect-refused ⇒ 502", r9.status, 502);

    // 10. A1 — mid-stream abort: the upstream drops the socket AFTER streaming began.
    //     The router must NOT crash; the partial reached the client; the connection
    //     ended (not a clean end). Surviving to run the assertions IS the no-crash proof.
    const r10 = await requestTolerant(routerPort, { model: "midstream-1", messages: [] });
    check("mid-stream: client saw the streamed 200 headers", r10.status, 200);
    check("mid-stream: partial chunk reached the client", r10.body.includes("PARTIAL-CHUNK"), true);
    check("mid-stream: connection did not end cleanly (aborted/closed/errored)", r10.how !== "end", true);
  } finally {
    process.stderr.write = realStderrWrite;
    delete process.env.TEST_ANTHROPIC_KEY;
    delete process.env.TEST_DEEPSEEK_KEY;
    delete process.env.MODEL_ROUTER_LOG;
    await close(router);
    await close(anthropicStub.server);
    await close(bearerStub.server);
    await close(passthroughStub.server);
    await close(midStub);
  }

  // ── vision fallback e2e (reactive catch-400 reroute) ──────────────────────
  // Stub A (nonvis-*): mode-switchable; Stub B (vis-*, forImages): the vision target.
  const aStub = makeModeStub("reject");
  const bStub = makeModeStub("ok");
  const aVPort = await listen(aStub.server);
  const bVPort = await listen(bStub.server);

  process.env.TEST_VISION_KEY = "VIS-SECRET-789";
  process.env.MODEL_ROUTER_LOG = "1";
  const vLogCapture = [];
  const vRealStderrWrite = process.stderr.write.bind(process.stderr);
  process.stderr.write = (chunk, ...rest) => { vLogCapture.push(String(chunk)); return vRealStderrWrite(chunk, ...rest); };

  // A request carrying an image block — text sentinel + image-data sentinel let us
  // prove the body (and the image bytes) pass through unaltered and never leak to logs.
  const imageReq = (model) => ({
    model,
    messages: [{ role: "user", content: [
      { type: "image", source: { type: "base64", media_type: "image/png", data: "IMG-DATA-SENTINEL" } },
      { type: "text", text: "VISION-BODY-SENTINEL" },
    ] }],
  });

  const visionConfig = {
    listen: { host: "127.0.0.1", port: 0 },
    routes: [
      { match: "nonvis-*", base_url: `http://127.0.0.1:${aVPort}`, auth: { header: "x-api-key", keyEnv: "TEST_VISION_KEY" } },
      // declared non-vision: an image turn must pre-route to B WITHOUT hitting A first.
      { match: "declared-*", base_url: `http://127.0.0.1:${aVPort}`, vision: false, auth: { header: "x-api-key", keyEnv: "TEST_VISION_KEY" } },
      { match: "vis-*", base_url: `http://127.0.0.1:${bVPort}`, forImages: true, forImagesModel: "vendor/vision-model", auth: { header: "x-api-key", keyEnv: "TEST_VISION_KEY" } },
    ],
  };
  const vRouter = createRouter(visionConfig);
  const vPort = await listen(vRouter);

  // A no-vision router: same nonvis route, NO forImages target (the fail-loud case).
  const noVisionConfig = {
    listen: { host: "127.0.0.1", port: 0 },
    routes: [
      { match: "nonvis-*", base_url: `http://127.0.0.1:${aVPort}`, auth: { header: "x-api-key", keyEnv: "TEST_VISION_KEY" } },
    ],
  };
  const noVisionRouter = createRouter(noVisionConfig);
  const noVisionPort = await listen(noVisionRouter);

  try {
    // V1. image request → route A 400-image → rerouted to B → client gets B's 200.
    aStub.setMode("reject");
    const v1 = await request(vPort, imageReq("nonvis-1"));
    check("V1 reroute: A received the first (failing) call", aStub.received.length, 1);
    check("V1 reroute: B received the rerouted call", bStub.received.length, 1);
    check("V1 reroute: client gets the vision route's 200", v1.status, 200);
    check("V1 reroute: client gets B's streamed body intact", v1.body, STREAM_BODY);
    // the image CONTENT bytes reached B unaltered (only `model` is rewritten, below).
    const v1b = JSON.parse(bStub.received[0].body);
    check("V1 reroute: image bytes reached B unaltered", v1b.messages[0].content[0].source.data, "IMG-DATA-SENTINEL");
    // the cross-provider hop rewrites only `model` to the vision route's forImagesModel.
    check("V1 reroute: B got the rewritten model id (forImagesModel)", v1b.model, "vendor/vision-model");
    check("V1 reroute: B got the backend key, not the client front-key", bStub.received[0].headers["x-api-key"], "VIS-SECRET-789");

    // V2. multimodal: route A returns 200 → NO reroute (B untouched). Distinct model
    //     so the V1 cache entry (nonvis-1) does not pre-route it.
    aStub.setMode("ok");
    const v2 = await request(vPort, imageReq("nonvis-mm"));
    check("V2 no-reroute: A handled it (200)", aStub.received.length, 2);
    check("V2 no-reroute: B was NOT touched", bStub.received.length, 1);
    check("V2 no-reroute: client gets A's 200", v2.status, 200);
    check("V2 no-reroute: client gets A's streamed body", v2.body, STREAM_BODY);

    // V3. 400-image but NO forImages route → clear error, not the raw upstream 400.
    aStub.setMode("reject");
    const aBefore3 = aStub.received.length;
    const v3 = await request(noVisionPort, imageReq("nonvis-1"));
    check("V3 fail-loud: A received the call", aStub.received.length, aBefore3 + 1);
    check("V3 fail-loud: client gets a clear 4xx", v3.status >= 400 && v3.status < 500, true);
    check("V3 fail-loud: error names the missing vision fallback", v3.body.includes("no vision fallback"), true);
    check("V3 fail-loud: NOT the raw upstream image-blocks text", v3.body.includes("does not support image blocks"), false);

    // V4. ambiguous 400 (not image) → relayed as-is, no reroute, B untouched.
    aStub.setMode("badreq");
    const bBefore4 = bStub.received.length;
    const v4 = await request(vPort, { model: "nonvis-amb", messages: [{ role: "user", content: "no image here" }] });
    check("V4 ambiguous: relayed as the upstream 400", v4.status, 400);
    check("V4 ambiguous: the upstream body is relayed verbatim", v4.body.includes("roles must alternate"), true);
    check("V4 ambiguous: B was NOT touched (no reroute)", bStub.received.length, bBefore4);

    // V5. session cache: a 2nd image call to nonvis-1 pre-routes to B WITHOUT hitting A.
    aStub.setMode("reject"); // would 400 if hit — proving the pre-route skipped A
    const aBefore5 = aStub.received.length;
    const v5 = await request(vPort, imageReq("nonvis-1"));
    check("V5 pre-route: A was NOT hit (cache skipped the failing call)", aStub.received.length, aBefore5);
    check("V5 pre-route: B served it directly", bStub.received.length, 2);
    check("V5 pre-route: client gets B's 200", v5.status, 200);
    check("V5 pre-route: client gets B's streamed body", v5.body, STREAM_BODY);

    // V6. loop guard: A 400-image → reroute to B, but B ALSO 400-images → clear error,
    //     NO infinite reroute (A and B each hit exactly once for this call).
    aStub.setMode("reject");
    bStub.setMode("reject");
    const aBefore6 = aStub.received.length;
    const bBefore6 = bStub.received.length;
    const v6 = await request(vPort, imageReq("nonvis-loop"));
    check("V6 loop-guard: A hit exactly once", aStub.received.length, aBefore6 + 1);
    check("V6 loop-guard: B hit exactly once (no re-reroute)", bStub.received.length, bBefore6 + 1);
    check("V6 loop-guard: client gets a clear 4xx", v6.status >= 400 && v6.status < 500, true);
    check("V6 loop-guard: error names the loop guard", v6.body.includes("loop guard"), true);

    // V7. declared non-vision (vision:false): an image turn pre-routes to B WITHOUT ever
    //     hitting A — even though A would 400/refuse, A is never called (proactive flag).
    aStub.setMode("reject");
    bStub.setMode("ok");
    const aBefore7 = aStub.received.length;
    const bBefore7 = bStub.received.length;
    const v7 = await request(vPort, imageReq("declared-1"));
    check("V7 declared-non-vision: A was NOT hit (unconditional pre-route)", aStub.received.length, aBefore7);
    check("V7 declared-non-vision: B served it directly", bStub.received.length, bBefore7 + 1);
    check("V7 declared-non-vision: client gets B's 200", v7.status, 200);
    check("V7 declared-non-vision: B got the rewritten model id", JSON.parse(bStub.received[bStub.received.length - 1].body).model, "vendor/vision-model");

    // V8. declared non-vision but a TEXT-ONLY turn: the flag only affects image turns, so
    //     it routes normally to A (no pre-route to the vision backend).
    aStub.setMode("ok");
    const aBefore8 = aStub.received.length;
    const bBefore8 = bStub.received.length;
    const v8 = await request(vPort, { model: "declared-2", messages: [{ role: "user", content: "no image here" }] });
    check("V8 declared-non-vision text: A handled it (no pre-route)", aStub.received.length, aBefore8 + 1);
    check("V8 declared-non-vision text: B NOT touched", bStub.received.length, bBefore8);
    check("V8 declared-non-vision text: client gets A's 200", v8.status, 200);

    // log safety across every vision case: no key, no body sentinel, no image bytes.
    const vLogText = vLogCapture.join("");
    check("V-log: leaks NO backend key", vLogText.includes("VIS-SECRET-789"), false);
    check("V-log: leaks NO client front-key", vLogText.includes("CLIENT-FRONT-KEY"), false);
    check("V-log: leaks NO request body sentinel", vLogText.includes("VISION-BODY-SENTINEL"), false);
    check("V-log: leaks NO image bytes", vLogText.includes("IMG-DATA-SENTINEL"), false);
    check("V-log: leaks NO 400 error body", vLogText.includes("does not support image blocks"), false);
  } finally {
    process.stderr.write = vRealStderrWrite;
    delete process.env.TEST_VISION_KEY;
    delete process.env.MODEL_ROUTER_LOG;
    await close(vRouter);
    await close(noVisionRouter);
    await close(aStub.server);
    await close(bStub.server);
  }

  if (fails.length) {
    console.log("MODEL-ROUTER SELF-TEST:");
    fails.forEach((f) => console.log(f));
    console.log(`\nFAIL — ${fails.length} case(s) failed`);
    process.exit(1);
  }
  console.log(`PASS — ${pass} passed`);
}

main().catch((err) => {
  console.log(`FAIL — unexpected error: ${err && err.stack ? err.stack : err}`);
  process.exit(1);
});
