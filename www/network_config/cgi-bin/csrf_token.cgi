#!/bin/bash
# csrf_token.cgi — GET-only: the caller's own X-SA02M-CSRF value for the
# session named by its cookie (1.0.6.53). The panel calls it to refresh a token
# it lost (a session older than the sa02m_csrf mirror cookie — the upgrade
# window — or a token file gone) instead of logging the user out on E_CSRF
# (docs/decisions/selective-csrf-policy.md «Механика токена» / «Реакция панели»).
#
# Not in the cgi-csrf-policy ledger by design: no sudo, no mutation primitive,
# no client input read (no query, no body). Its one write — minting the
# <hash>.csrf file when it is missing (web_csrf_get_or_create) — is the same
# self-scoped act login.cgi performs, for the caller's OWN live session only.
# Cross-origin readability is the CSRF property itself: the session cookie is
# SameSite=Lax (a cross-site fetch carries none → unauthorized), the body is
# JSON with nosniff (never script-embeddable), and Cache-Control: no-store keeps
# any proxy from serving one session's token to another. Served by nginx's
# generic /cgi-bin/ location; shipped in the offline pack by the
# www/network_config/ allow-list line. Harness: scripts/dev/test-cgi-csrf-behaviour.sh 11–14.
# shellcheck disable=SC1091
. "$(dirname "$0")/lib_web_auth.sh"

_headers() {
  printf 'Content-type: application/json; charset=UTF-8\r\n'
  printf 'Cache-Control: no-store\r\n'
  printf 'X-Content-Type-Options: nosniff\r\n\r\n'
}

if [ "${REQUEST_METHOD:-GET}" != "GET" ]; then
  _headers
  printf '{"ok":false,"error":"method_not_allowed"}\n'
  exit 0
fi

web_session_check_cookie || {
  _headers
  printf '{"error":"unauthorized","ok":false}\n'
  exit 0
}

tok=$(web_csrf_get_or_create) || tok=""
tok="${tok//$'\r'/}"
# Shape-asserted before it reaches the JSON — hex only, no escaper needed.
if [[ ! "$tok" =~ ^[a-f0-9]{64}$ ]]; then
  _headers
  printf '{"ok":false,"error":"csrf_unavailable"}\n'
  exit 0
fi

_headers
printf '{"ok":true,"csrf":"%s"}\n' "$tok"
