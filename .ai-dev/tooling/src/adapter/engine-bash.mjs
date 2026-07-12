// Bash-command parsing helpers for the enforcement engine. Best-effort scanners,
// NOT a shell parser — permissive on a miss (persona is the fail-safe). Pure string
// work: quote masking, heredoc-body stripping, write/read target extraction, the
// pure-git test. Several port hard-won, defect-fixed logic from the previous OpenCode
// plugin (the slice-15 quote-masking + cp/mv redirect fixes); the long form lives in
// git history.

function findAbsolutePathArg(command) {
  if (typeof command !== "string" || !command) return null;
  const m = command.match(/(?:^|[\s;&|(])find[ \t]+(\/[^\s|;&"']*)/);
  return m ? m[1] : null;
}
// Mask spans where `>` is not a redirect (quoted strings, `(( ))`, `[[ ]]`) so
// the write-idiom scans never mistake them for a write target — except a quoted
// redirect TARGET, which is preserved. (slice-15 over-deny fix.)
function maskQuotedSpans(command) {
  const fill = (s) => "_".repeat(s.length);
  let out = command.replace(/\(\([^)]*\)\)|\[\[[^\]]*\]\]/g, fill);
  out = out.replace(/(\d?>>?\s*)?("[^"]*"|'[^']*')/g, (match, redirPrefix, quoted) =>
    redirPrefix !== undefined ? match : fill(quoted)
  );
  return out;
}
// Strip non-shell heredoc BODIES from a bash command before any rule pattern
// runs (applied once in evaluate(), so every predicate sees the same prepared
// string). A heredoc body fed to a non-shell consumer (python3, node, cat, …)
// is data — prose in it mentioning `git push` must not trip the verb rules
// (live 4.19.x false deny). Strict-side carve-outs — each keeps the body matched:
//   • a SHELL consumer (sh/bash/zsh/dash/…) executes its body;
//   • an ambiguous opener (several heredocs on one line, unparseable delimiter,
//     no visible consuming command);
//   • an unterminated body (no closing-delimiter line) — malformed ⇒ keep.
// Line-anchored literal scanning only — no nested quantifiers, no backtracking
// surface. The opener line and the closing-delimiter line are KEPT, so a real
// push before or after the heredoc in the same compound still matches.
// Known limit: an indirect execution of the body (python os.system, a heredoc
// written to a file and run later) is invisible here — persona is the backstop.
const SHELL_CONSUMERS = new Set(["sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "csh", "tcsh", "fish"]);
function stripHeredocBodies(command) {
  if (typeof command !== "string" || !command.includes("<<")) return command;
  const lines = command.split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    out.push(line); // the opener line itself always stays matched
    i += 1;
    const masked = maskQuotedSpans(line); // a quoted "<<" is not an operator
    const ops = [...masked.matchAll(/<{2,3}/g)].filter((m) => m[0] === "<<"); // <<< is a herestring, not a heredoc
    if (ops.length !== 1) continue; // none, or several on one line (ambiguous) ⇒ keep
    const at = ops[0].index;
    // The delimiter word, read from the RAW line (the mask hides a quoted one).
    const dm = line.slice(at).match(/^<<(-?)[ \t]*(?:'([A-Za-z0-9_.-]+)'|"([A-Za-z0-9_.-]+)"|\\?([A-Za-z0-9_.-]+))/);
    if (!dm) continue; // unparseable opener ⇒ keep
    const delim = dm[2] ?? dm[3] ?? dm[4];
    // The consuming command: the simple-command segment the operator belongs to.
    const words = masked.slice(0, at).split(/[;&|()`]/).pop().trim().split(/\s+/)
      .filter((w) => w && !/^[A-Za-z_][A-Za-z0-9_]*=/.test(w)); // skip env assignments
    if (words.length === 0) continue; // no visible consumer ⇒ ambiguous ⇒ keep
    if (words.some((w) => SHELL_CONSUMERS.has(w.slice(w.lastIndexOf("/") + 1)))) continue;
    // Find the closing-delimiter line (exact match; <<- permits leading tabs).
    let close = -1;
    for (let j = i; j < lines.length; j += 1) {
      const cand = dm[1] === "-" ? lines[j].replace(/^\t+/, "") : lines[j];
      if (cand === delim) { close = j; break; }
    }
    if (close === -1) continue; // unterminated ⇒ malformed ⇒ keep (deny-side)
    out.push(lines[close]); // body dropped; the closer and everything after stay
    i = close + 1;
  }
  return out.join("\n");
}
// A redirect target that is a stream (a fd dup or a /dev sink), never a real-file
// write. The single home for "what counts as a stream" — used by bashWriteTargets'
// target extraction AND by sshContentEdit's read-vs-write distinction. Exact-match
// allowlist: anything NOT exactly one of these is treated as a write (fail-closed —
// `/dev/null/../etc/passwd` is not `/dev/null`, so it denies).
function isStreamTarget(t) {
  return t === "/dev/null" || t === "/dev/stdout" || t === "/dev/stderr" ||
    t === "&1" || t === "&2" || t.startsWith("&");
}
// Best-effort write-target extraction from a bash command (redirect / tee /
// sed -i / cp|mv dest / dd of=). Not a shell parser — permissive on miss (the
// persona is the fail-safe). Runs on a quote-masked copy.
function bashWriteTargets(command) {
  if (typeof command !== "string" || !command) return [];
  command = maskQuotedSpans(command);
  // Drop heredoc openers (`<<EOF`, `<<-'EOF'` → masked to `<<_____`) — an operator,
  // never a write target; left in, `tee file <<EOF` extracts a phantom `<<EOF`.
  command = command.replace(/<<-?[ \t]*[\w.-]*/g, " ");
  const targets = [];
  const isStream = isStreamTarget;
  const redir = /(?:^|\s)\d?>>?\s*("[^"]*"|'[^']*'|[^\s|;&<>()]+)/g;
  let r;
  while ((r = redir.exec(command)) !== null) {
    const tok = r[1].replace(/^["']|["']$/g, "");
    if (tok && !isStream(tok)) targets.push(tok);
  }
  const tee = command.match(/(?:^|[\s;&|(])tee\b([^|;&\n]*)/);
  if (tee) for (const tok of tee[1].split(/\s+/)) {
    if (tok && !tok.startsWith("-") && !isStream(tok)) targets.push(tok.replace(/^["']|["']$/g, ""));
  }
  const sed = command.match(/(?:^|[\s;&|(])sed\b[^|;&\n]*?\s-i\b([^|;&\n]*)/);
  if (sed) for (const tok of sed[1].split(/\s+/)) {
    if (!tok || tok.startsWith("-") || isStream(tok)) continue;
    const clean = tok.replace(/^["']|["']$/g, "");
    if (/^[sy]\W/.test(clean) || (clean.includes("/") === false && /[{};]/.test(clean))) continue;
    targets.push(clean);
  }
  const cpmv = command.match(/(?:^|[\s;&|(])(?:cp|mv)\b([^|;&\n]*)/);
  if (cpmv) {
    const toks = cpmv[1].split(/\d?>>?|</)[0].split(/\s+/).filter((t) => t && !t.startsWith("-") && !isStream(t));
    if (toks.length) targets.push(toks[toks.length - 1].replace(/^["']|["']$/g, ""));
  }
  const dd = command.match(/(?:^|[\s;&|(])dd\b[^|;&\n]*?\bof=("[^"]*"|'[^']*'|[^\s|;&<>()]+)/);
  if (dd) {
    const tok = dd[1].replace(/^["']|["']$/g, "");
    if (tok && !isStream(tok)) targets.push(tok);
  }
  return targets;
}
// The pure-file-arg read commands: every non-flag token in the invocation's arg
// span is a file to read (cat/head/tail/… plus `dd if=`). Single home for the list.
const READ_FILE_COMMANDS = [
  "cat", "head", "tail", "less", "more", "nl", "tac", "rev",
  "xxd", "od", "strings", "wc", "cut", "base64", "md5sum",
  "sha1sum", "sha224sum", "sha256sum", "sha384sum", "sha512sum",
];
// The pattern-first read commands: skip leading flags, skip ONE pattern token,
// the remaining non-flag tokens are files. `sed` only when NOT `-i` (an in-place
// `sed -i` is a WRITE, owned by bashWriteTargets — excluded below).
const READ_PATTERN_COMMANDS = ["grep", "egrep", "fgrep", "awk", "sed"];
// Best-effort READ-target extraction from a bash command — symmetric to
// bashWriteTargets, conservative-first (the high-value, low-false-positive
// surface). Three families: an input redirect (`< file`), a pure-file read
// command (cat/head/…), a pattern-first read command (grep/awk/sed-without-i).
// NOT a shell parser — permissive on a parse miss (fail-OPEN; persona is the
// backstop). Runs AFTER stripHeredocBodies (applied once in evaluate()). A `~`
// token is returned verbatim so the predicate can treat it as outside-root
// (path.resolve would forge a fake in-root path).
//
// Quoted spans are MASKED FIRST (maskQuotedSpans, the same helper gitAddAll uses):
// a quoted argument body — a commit message, an echo string, a grep pattern —
// must yield NO read target, else a `git commit -m "... < /etc/shadow"` or an
// `echo "see /etc/passwd"` parses a PHANTOM out-of-root read and false-denies
// (the live dogfood regression). The accepted consequence, consistent with the
// conservative + fail-open-on-ambiguity posture: a genuinely quoted-path read
// (`cat '/etc/passwd'`) is now MISSED (fail-open) — the role-scope persona rule
// is its backstop. The unquoted real cases still deny.
function bashReadTargets(command) {
  if (typeof command !== "string" || !command) return [];
  command = maskQuotedSpans(command);
  const targets = [];
  const strip = (t) => t.replace(/^["']|["']$/g, ""); // defensive no-op post-mask
  // Family 1 — input redirect `< token`. The lookbehind/lookahead exclude `<<`
  // heredoc and `<<<` herestring; the token charclass excludes `(` so a process
  // substitution `< <(cmd)` yields no token (fail-open). Stream sinks are dropped.
  const inred = /(?<!<)<(?!<)\s*("[^"]*"|'[^']*'|[^\s|;&<>()]+)/g;
  let r;
  while ((r = inred.exec(command)) !== null) {
    const tok = strip(r[1]);
    if (tok && !isStreamTarget(tok)) targets.push(tok);
  }
  // Family 2 — pure-file read commands: every non-flag token in the invocation
  // span (to the next shell separator) is a file. A flag VALUE token (head -n 5
  // → `5`) resolves in-root and is harmless (a safe false-token, never a false
  // deny). `dd if=<token>` mirrors the write `of=` extractor.
  for (const cmd of READ_FILE_COMMANDS) {
    const inv = new RegExp(`(?:^|[\\s;&|(])${cmd}\\b([^|;&\\n]*)`, "g");
    let m;
    while ((m = inv.exec(command)) !== null) {
      for (const tok of m[1].split(/\s+/)) {
        if (!tok || tok.startsWith("-")) continue;
        targets.push(strip(tok));
      }
    }
  }
  const dd = command.match(/(?:^|[\s;&|(])dd\b[^|;&\n]*?\bif=("[^"]*"|'[^']*'|[^\s|;&<>()]+)/);
  if (dd) {
    const tok = strip(dd[1]);
    if (tok && !isStreamTarget(tok)) targets.push(tok);
  }
  // Family 3 — pattern-first read commands: drop leading flag tokens, skip ONE
  // pattern token, treat the remaining non-flag tokens as files. Closes the live
  // `grep -c . /out/of/root` case (flag -c → pattern . → file → deny) while
  // letting `grep '/etc/passwd' infile` (pattern, then in-root file) through. The
  // -e/-f multi-pattern forms are the documented highest false-deny risk
  // (`grep -e /a -e /b file` mis-reads /b as a file); conservative by design.
  for (const cmd of READ_PATTERN_COMMANDS) {
    const inv = new RegExp(`(?:^|[\\s;&|(])${cmd}\\b([^|;&\\n]*)`, "g");
    let m;
    while ((m = inv.exec(command)) !== null) {
      const span = m[1];
      if (cmd === "sed" && /\s-i\b/.test(span)) continue; // an in-place sed is a WRITE
      let patternSkipped = false;
      for (const tok of span.split(/\s+/)) {
        if (!tok || tok.startsWith("-")) continue;
        if (!patternSkipped) { patternSkipped = true; continue; } // the one pattern token
        targets.push(strip(tok));
      }
    }
  }
  return targets;
}
function isPureGitCommand(command) {
  if (typeof command !== "string" || !command) return false;
  let sawGit = false;
  for (const seg of command.split(/&&|\|\||;|\|/)) {
    const t = seg.trim();
    if (!t) continue;
    if (!/^git\b/.test(t)) return false;
    sawGit = true;
  }
  return sawGit;
}

export {
  findAbsolutePathArg,
  maskQuotedSpans,
  stripHeredocBodies,
  isStreamTarget,
  bashWriteTargets,
  bashReadTargets,
  isPureGitCommand,
};
