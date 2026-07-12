// Sync the modelpipe transport CODE into the in-repo vendor-copy, drift-guarded.
//
// WHY THIS EXISTS (the ratified consume mechanism):
//   docs/decisions/proxy-consume-mechanism.md — Option 3 (synced, drift-guarded
//   vendor-copy). The transport router was extracted to a standalone repo
//   (aadegtyarev/modelpipe; transport/policy boundary: docs/decisions/router-extraction.md).
//   The protocol KEEPS the router under src/adapter/ — so the one-command-install
//   vendoring (install-core.mjs vendorTooling, a plain recursive copy) carries it
//   unchanged and the downstream launcher's `import … from "./model-router.mjs"`
//   keeps resolving — but the in-repo copy is a GENERATED MIRROR of modelpipe at a
//   pinned ref, exactly the generated-artifact-plus-drift-guard pattern the protocol
//   already uses for the assembled agents (install-drift.test.mjs, the tools.json
//   assembled-drift-guard / install-drift rows).
//
// WHAT IT SYNCS — the transport CODE FILE ONLY:
//   modelpipe  src/router.mjs  →  this repo  src/adapter/model-router.mjs
//
//   Deliberately NOT synced: model-providers.json and model-router.example.json.
//   Those are the PROTOCOL/launcher's own POLICY-layer config and have intentionally
//   diverged from modelpipe's providers.json / routes.example.json — the in-repo
//   example carries the launcher-local `proxyUrl` key, the in-repo catalog carries
//   launcher-specific `_doc` notes. The transport/policy boundary
//   (docs/decisions/router-extraction.md) puts the router CODE on modelpipe's side and
//   the routes/providers PRESENTATION on the launcher's side, so only the code file is
//   a mirror; the config files are first-party here and are not drift-guarded against
//   modelpipe.
//
// THE COPY IS VERBATIM. The script writes modelpipe's router.mjs byte-for-byte to
//   model-router.mjs — no header rewrite, no transform. A transform would be a SECOND
//   drift surface to maintain; a verbatim copy makes the drift guard a plain
//   byte-compare against exactly what the remote serves. The vendored file therefore
//   carries modelpipe's own header comments (its self-references to test/router.test.mjs
//   etc. are modelpipe's paths, correct for the upstream file); the in-repo test
//   (model-router.test.mjs) and the launcher import by the LOCAL path regardless.
//
// PINNED REF — immutable. We pin to a full commit SHA, never a moving branch, so the
//   drift target cannot change under us. modelpipe 0.5.0 = main HEAD at this SHA.
//
// MODES:
//   (default / --write)  fetch the pinned router.mjs and write src/adapter/model-router.mjs.
//   --check              fetch the pinned router.mjs and byte-compare the in-repo copy;
//                        exit non-zero ONLY on a real drift. NETWORK-TOLERANT: if the
//                        pinned ref cannot be fetched (offline local dev), print a clear
//                        "source unreachable — drift not verified" notice and PASS (exit
//                        0) — so `node src/quality/run.mjs build` stays green offline. CI
//                        (which has network) is the real enforcement point; this is an
//                        honest CI-enforced / offline-degraded split.
//
// Run:  node src/adapter/sync-modelpipe.mjs          # write the copy
//       node src/adapter/sync-modelpipe.mjs --check  # drift guard (build beat)

import fs from "node:fs";
import https from "node:https";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));

// The pinned, immutable source — aadegtyarev/modelpipe at the 0.5.0 commit (main
// HEAD, 2026-06-30; no git tag). Bump this SHA to adopt a newer modelpipe, then re-run the sync.
const MODELPIPE_REPO = "aadegtyarev/modelpipe";
const MODELPIPE_REF = "9bd3779c109071117bfcfda7acf2cab4ac96de67"; // modelpipe 0.5.0 (main HEAD, no tag) — adds GET /v1/models
const MODELPIPE_SRC_PATH = "src/router.mjs";

// The one in-repo target — the transport code file only (NOT the policy config).
const LOCAL_TARGET = path.join(HERE, "model-router.mjs");

const RAW_URL = `https://raw.githubusercontent.com/${MODELPIPE_REPO}/${MODELPIPE_REF}/${MODELPIPE_SRC_PATH}`;

// A network/source-unreachable failure — distinguished from a real drift so --check
// can degrade gracefully offline while still failing loud on an actual mismatch.
class SourceUnreachable extends Error {}

// Fetch the pinned file over HTTPS using only the Node stdlib (no external tool, no
// auth — modelpipe is public). Rejects with SourceUnreachable on any network error,
// a non-200 status, or a timeout, so the caller can tell "offline" from "drifted".
function fetchPinned() {
  return new Promise((resolve, reject) => {
    const req = https.get(RAW_URL, { timeout: 15000 }, (res) => {
      if (res.statusCode !== 200) {
        res.resume(); // drain
        reject(new SourceUnreachable(`HTTP ${res.statusCode} fetching ${RAW_URL}`));
        return;
      }
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
      res.on("error", (err) => reject(new SourceUnreachable(err.message)));
    });
    req.on("error", (err) => reject(new SourceUnreachable(err.message)));
    req.on("timeout", () => {
      req.destroy();
      reject(new SourceUnreachable(`timed out fetching ${RAW_URL}`));
    });
  });
}

async function write() {
  const source = await fetchPinned();
  fs.writeFileSync(LOCAL_TARGET, source);
  process.stdout.write(
    `synced ${MODELPIPE_REPO}@${MODELPIPE_REF.slice(0, 7)} ${MODELPIPE_SRC_PATH} -> ${path.relative(process.cwd(), LOCAL_TARGET)}\n`,
  );
}

async function check() {
  let source;
  try {
    source = await fetchPinned();
  } catch (err) {
    if (err instanceof SourceUnreachable) {
      // Offline-degraded PASS: keep `run.mjs build` green for local dev with no
      // network. CI has network and enforces the byte-compare for real.
      process.stdout.write(
        `modelpipe source unreachable (${err.message}) — drift not verified (CI enforces; offline-degraded PASS)\n`,
      );
      return 0;
    }
    throw err;
  }
  const local = fs.existsSync(LOCAL_TARGET) ? fs.readFileSync(LOCAL_TARGET, "utf8") : null;
  if (local === source) {
    process.stdout.write(
      `OK — src/adapter/model-router.mjs is byte-identical to ${MODELPIPE_REPO}@${MODELPIPE_REF.slice(0, 7)} ${MODELPIPE_SRC_PATH}\n`,
    );
    return 0;
  }
  process.stderr.write(
    `DRIFT — src/adapter/model-router.mjs differs from ${MODELPIPE_REPO}@${MODELPIPE_REF.slice(0, 7)} ${MODELPIPE_SRC_PATH}\n` +
      `  fix: node src/adapter/sync-modelpipe.mjs   (re-sync the vendor-copy and commit it),\n` +
      `       or bump MODELPIPE_REF in this script if adopting a newer modelpipe.\n`,
  );
  return 1;
}

const mode = process.argv[2];
const action = mode === "--check" ? check : write;
action()
  .then((code) => process.exit(code || 0))
  .catch((err) => {
    process.stderr.write(`sync-modelpipe failed: ${err.message}\n`);
    process.exit(1);
  });
