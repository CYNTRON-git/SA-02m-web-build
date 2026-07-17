# Контракт: enrollment устройства в облако (сторона устройства)

Зеркало-указатель на **авторитетный** контракт в облачном репозитории
(`CYNTRON-git/cloud` → `docs/contracts/cloud-enrollment.md` + `config/frps.toml.template`) —
**единственный дом** формы шва. Этот файл описывает только **сторону
устройства**: какие endpoint'ы дёргает агент, какой frpc-профиль он потребляет и
какой **device-side allow-list локальных портов** он навязывает. Форму запросов
и ответов НЕ дублируем — при расхождении авторитет за облачным репозиторием.

Машинная грамматика (endpoint'ы, имена и формы JSON, ключи frpc.toml) — на
английском (`PROTOCOL.md` invariant 5); прозаические пояснения — на русском.

Реализация на устройстве: `opt/sa02m-cloud-agent/sa02m-cloud-agent.py`
(`render_frpc_toml`, `finalize_enrollment`, `run_claim_flow`, `run_token_flow`,
`active_loop`). Проверка контракта: `opt/sa02m-cloud-agent/tests/test_render_frpc.py`
(quality row `py-unit-cloud`) + `opt/sa02m-cloud-agent/tests/test_agent.py`.

## 1. Endpoint'ы, которые вызывает агент (send-only)

База: `https://cloud.cyntron.ru/api/v1` (nginx терминирует TLS). Устройство
**только исходящие** запросы — нового входящего порта не открывается, командного
канала нет (F1 удалён).

| Endpoint | Когда | Что несёт устройство | Что берёт из ответа |
|---|---|---|---|
| `POST /claim` | вкладка «Облако» → «Подключить» (основной путь) | `device_id`, `hw_variant`, `fw_version` | `claim_code` (8 симв., TTL ~15 мин, идемпотентно), `expires_in_s`, `poll_interval_s` |
| `POST /claim/status` | опрос пока пользователь привязывает код в кабинете | `device_id`, `claim_code` | `state` (`claimed`/`expired`); при `claimed` — frpc-профиль + `heartbeat_interval_s` |
| `POST /enroll` | fallback для наладчиков (enroll-токен) | `enroll_token`, `device_id`, `hw_variant`, `fw_version` | `ok`, тот же frpc-профиль + `heartbeat_interval_s` |
| `POST /heartbeat` | периодически после enrollment | `device_id`, `uptime_s`, `telemetry` (в т.ч. `modules` из `/run/sa02m-rs485-roster.json`) | **игнорируется** (кроме факта доставки) — send-only |

`device_id` — `sa02m-<serial>` (charset `^[A-Za-z0-9._-]{1,64}$`).
Идентичность v1: **общий по флоту `FRP_TOKEN`** + `device_id` + `enroll_token`;
Phase 4 → per-device **mTLS** (идентичность + отзыв). Общий токен — известное
ограничение (`O2`, `docs/threat-model.md §6`), реальный фикс — Phase 4.

## 2. frpc-профиль, который потребляет устройство

Ответы `claim/status` (`claimed`) и `enroll` несут объект `frpc`:

```json
{
  "server_addr": "cloud.cyntron.ru",
  "server_port": 8890,
  "token": "<shared FRP_TOKEN>",
  "proxies": [
    { "name": "dev-<id>",     "subdomain": "<id>",     "local_port": 80,   "role": "web" },
    { "name": "dev-<id>-cfg", "subdomain": "<id>-cfg", "local_port": 9999, "role": "cfg" }
  ]
}
```

`render_frpc_toml` разворачивает это в `/etc/sa02m-cloud/frpc.toml` (0600):
`type = "http"` — единственная форма, которую принимает frps NewProxy-authz;
`localIP = "127.0.0.1"`; `transport.tls.enable = true` пиннится явно (control-leg
TLS, `O3`). Легаси одиночный fallback (`proxy_name`/`subdomain`/`local_port`) —
для до-Phase-B ответов.

## 3. Device-side allow-list локальных портов (O1 — сторона устройства)

Ключевой инвариант устройства: **устройство туннелирует ТОЛЬКО свои роли —
`{80, 9999}`.** `render_frpc_toml` отбрасывает (с `log.warning`) любой прокси,
чей `local_port` вне `ALLOWED_LOCAL_PORTS = frozenset({80, 9999})`; ту же проверку
проходит легаси fallback. Если отброшены все — конфиг без `[[proxies]]` и
`log.error` (**fail closed**: лучше без туннеля, чем зловредный).

Это **defense-in-depth**, дополняющий облачную authz, а не дублирующий её:

- облачная **frps NewProxy-authz** (subdomain-check) защищает **флот** от
  зловредного УСТРОЙСТВА (нельзя занять чужой поддомен);
- **device-side allow-list** защищает **устройство** от зловредного/
  скомпрометированного ОБЛАКА (актор `A5`, `docs/threat-model.md`), которое иначе
  продиктовало бы `local_port: 22` (SSH) или `:1883` (MQTT) и устройство бы их
  туннелировало.

Ни одна сторона не покрывает угрозу другой — оба контроля нужны.

## Проверка контракта

`opt/sa02m-cloud-agent/tests/test_render_frpc.py` — allow-list ({80,9999}
проходят, :22/:1883 отброшены, all-dropped → 0 прокси, TLS запиннен).
`opt/sa02m-cloud-agent/tests/test_agent.py` — форма frpc.toml, send-only, отсутствие
командного канала и WireGuard-остатков, `modules` verbatim из ростера.
Облачная сторона (frps authz, backend `/api/v1/*`) проверяется в облачном репо.
