# Контракт: `POST /cgi-bin/mqtt_set.cgi` — запись выхода устройства MQTT

Домашний адрес контракта единственного веб-эндпоинта записи выходов
(DO МР-02м и coil-выходы ДТВ) через локальный брокер MQTT. Машинная
грамматика (поля, JSON, топики) — на английском (`PROTOCOL.md` invariant 5);
пояснения — на русском.

## Запрос

`POST`, `Content-Type: application/x-www-form-urlencoded`, cookie
`session_token` обязателен (аутентификация проверяется ДО разбора тела).

| Поле | Allow-list (закрытый) | Отказ |
|---|---|---|
| `device` | `^[a-zA-Z0-9._-]+$`, длина ≤ 64 | `bad_device` |
| `control` | `^(do_([1-9]|1[0-6])|buzzer|leds)$` — `ao_*` сознательно отсутствует | `bad_control` |
| `value` | ровно `0` или `1` | `bad_value` |

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
2. `device=a;rm`, `control=ao_1`, `control=do_17`, `control=do_1;x`,
   `value=2` → соответствующий отказ, публикации нет;
3. валидный запрос → ровно одна публикация, топик
   `/devices/<device>/controls/<control>/on`, payload = `value`,
   **в argv нет `-r`**, есть `timeout 5`.
