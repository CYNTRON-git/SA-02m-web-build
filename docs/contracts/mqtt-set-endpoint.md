# Контракт: `POST /cgi-bin/mqtt_set.cgi` — запись выхода устройства MQTT

Домашний адрес контракта единственного веб-эндпоинта записи выходов
(DO МР-02м, AO-уставки МР-02м и coil-выходы ДТВ) через локальный брокер MQTT.
Машинная грамматика (поля, JSON, топики) — на английском (`PROTOCOL.md`
invariant 5); пояснения — на русском.

## Запрос

`POST`, `Content-Type: application/x-www-form-urlencoded`, cookie
`session_token` обязателен (аутентификация проверяется ДО разбора тела).

| Поле | Allow-list (закрытый) | Отказ |
|---|---|---|
| `device` | `^[a-zA-Z0-9._-]+$`, длина ≤ 64 | `bad_device` |
| `control` | `^(do_([1-9]|1[0-6])|ao_([1-9]|1[0-2])|buzzer|leds)$` | `bad_control` |
| `value` | для `do_*`/`buzzer`/`leds` — ровно `0` или `1`; для `ao_*` — целое `0..1000` | `bad_value` |

`ao_N` — живая уставка аналогового выхода: целое `0..1000` = `0..10.00 В`,
пишется мостом в Holding-регистр `33 + N − 1` (тот же регистр, что «Задание»
флэшера). Грамматика `do_*`/`buzzer`/`leds` не изменилась — обратная
совместимость для развёрнутых клиентов сохранена.

## Действие

Одна публикация в **локальный** брокер (константы, не входы запроса):

```
timeout 5 mosquitto_pub -h 127.0.0.1 -p 1883 \
  -t "/devices/<device>/controls/<control>/on" -m "<value>"
```

Инварианты (твёрдые правила эндпоинта):

- **Без retain (`-r`)** — retained `/on` переигрывается при рестарте моста и
  повторно переключает реальные выходы. Проверяется валидирующей проверкой
  (ниже).
- Только loopback-листенер `1883`; внешний `1884` недостижим (host/port —
  константы).
- `timeout 5` на публикацию (floor «любой висящий вызов ограничен»).
- Каждая мутация пишет строку аудита в `/var/log/sa02m_install.log`.
- Ответ `ok:true` означает «опубликовано», НЕ «выход переключён»; подтверждение
  состояния — только echo моста через `mqtt_live.cgi` (фронтенд ждёт его сам).

## Ответы (всегда HTTP 200, JSON)

```json
{"ok":true,"device":"mr02m-COM1-5","control":"do_3","value":1}
{"ok":true,"device":"mr02m-COM4-6","control":"ao_1","value":500}
{"ok":false,"error":"unauthorized"}
{"ok":false,"error":"post_required"}
{"ok":false,"error":"bad_device"}   // также bad_control, bad_value
{"ok":false,"error":"publish_failed"}  // брокер остановлен или timeout
```

Все ошибочные пути fail-closed: неизвестный вход → отказ; сбой публикации →
ошибка, никогда не ложный `ok`.

## Валидирующая проверка (рецепт)

Локально, без устройства (scratchpad-харнесс, `web-diagnostic-tools.md`):
подложить в `PATH` фейковый `mosquitto_pub`, записывающий argv, и вызвать CGI
со стабом окружения (`REQUEST_METHOD=POST`, `CONTENT_LENGTH`, тело на stdin;
авторизация — стаб `lib_web_auth.sh` либо валидная сессия). Assert:

1. без сессии → `unauthorized`, публикации нет;
2. отказы (публикации нет): `device=a;rm` → `bad_device`; `control=do_17`,
   `control=ao_13`, `control=do_1;x` → `bad_control`; `value=2` (для `do_1`),
   `value=1001` / `value=x` (для `ao_1`) → `bad_value`;
3. валидные запросы → ровно одна публикация, топик
   `/devices/<device>/controls/<control>/on`, payload = `value`,
   **в argv нет `-r`**, есть `timeout 5`. Проверить и DO (`control=do_3`
   `value=1`), и AO (`control=ao_1` `value=500` → payload `500`; границы
   `value=0` и `value=1000` приняты).
