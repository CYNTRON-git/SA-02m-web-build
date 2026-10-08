# Контракт: `POST /cgi-bin/mqtt_set.cgi` — запись выхода устройства MQTT

Домашний адрес контракта единственного веб-эндпоинта записи выходов
(DO МР-02м, AO-уставки МР-02м и coil-выходы ДТВ) через локальный брокер MQTT.
Машинная грамматика (поля, JSON, топики) — на английском (`PROTOCOL.md`
invariant 5); пояснения — на русском.

## Запрос

`POST`, `Content-Type: application/x-www-form-urlencoded`, cookie
`session_token` обязателен (аутентификация проверяется ДО разбора тела).

Заголовок `X-SA02M-CSRF` обязателен (defense-in-depth поверх `SameSite=Lax`):
значение — CSRF-токен текущей сессии (минтуется при логине, зеркалится в
JS-читаемую cookie `sa02m_csrf`; фронтенд шлёт его через `withCsrfHeaders`).
Проверяется ПОСЛЕ аутентификации и ДО публикации. Отсутствующий/неверный токен →
`E_CSRF`, публикации нет. Политика — `docs/decisions/selective-csrf-policy.md`.
Браузерный путь только: у машинных клиентов (SCADA) пути через этот CGI нет —
они пишут напрямую в MQTT/Modbus, поэтому требование заголовка ничего не ломает.

| Поле | Allow-list (закрытый) | Отказ |
|---|---|---|
| `device` | `^[a-zA-Z0-9._-]+$`, длина ≤ 64 | `bad_device` |
| `control` | `^(do_([1-9]\|1[0-6])\|ao_([1-9]\|1[0-2])\|ai_type_([1-9]\|1[0-2])\|buzzer\|leds\|unit_on\|setpoint\|setpoint_summer\|fan_supply\|fan_step\|alarm_reset\|detection_distance\|detection_shielding_distance\|admission_confirmation_delay\|departure_disappearance_delay\|trigger_sensitivity\|maintain_sensitivity\|entrance_distance_reduction\|load_disconnect)$` | `bad_control` |
| `value` | для `do_*`/`buzzer`/`leds`/`unit_on`/`load_disconnect` — ровно `0` или `1`; для `ao_*` — целое `0..1000`; для `ai_type_*` — целое `0..42`; для `setpoint`/`setpoint_summer` — число `0..99` с не более чем одним знаком после точки; для `fan_supply` — целое `0..100`; для `fan_step` — целое `1..10`; для `alarm_reset` — ровно `1`; для семи holdings MTD262-MB — число `0..65535` с не более чем двумя знаками после точки | `bad_value` |

`ao_N` — живая уставка аналогового выхода: целое `0..1000` = `0..10.00 В`,
пишется мостом в Holding-регистр `33 + N − 1` (тот же регистр, что «Задание»
флэшера). Грамматика `do_*`/`buzzer`/`leds` не изменилась — обратная
совместимость для развёрнутых клиентов сохранена.

`ai_type_N` — код типа датчика аналогового входа `0..42` (`0` = «Выключен»).
Мост пишет Holding `400 + 7*(N−1)` (регистр 0 блока канала) сразу, без
рестарта, и правит `sensor_type` этого канала в YAML. Для ТХА и 3-проводного
RTD вместе с P-каналом пишется N-нога.

Carel (`unit_on`, `setpoint`, `setpoint_summer`, `fan_supply`, `fan_step`,
`alarm_reset`) —
те же имена, на которые мост уже подписан (`/devices/<id>/controls/<имя>/on`,
`docs/contracts/carel-ahu.md` §5). Уставка — градусы, как их публикует мост;
потолок семьи применяет мост, CGI отвергает только то, что не число `0..99`.
`fan_supply` — проценты притока c.pCOmini, `fan_step` — ступень uAria `1..10`.
`alarm_reset` — кнопка: принимается только нажатие `1`, импульс катушки сброса
мост делает сам (`carel-ahu.md` §4); `0` отвергается — «отпускания» у этой
кнопки нет.
Семь имён MTD262-MB — физическое значение holding-канала шаблона (мост сам
переводит его в слово регистра по `scale`). `net_enable`, `sys_mode` и
`fan_exhaust` этим эндпоинтом не пишутся.

`load_disconnect` — отключение (`1`) / подключение (`0`) нагрузки счётчика
«Меркурий». Значение не секрет; выполняет его мост только в роли
конфигуратора, иначе отказ без обмена (`docs/contracts/spodes-mercury.md`).

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
{"ok":false,"error":"csrf","error_code":"E_CSRF"}  // нет/неверный X-SA02M-CSRF
{"ok":false,"error":"bad_device"}   // также bad_control, bad_value
{"ok":false,"error":"publish_failed"}  // брокер остановлен или timeout
```

Все ошибочные пути fail-closed: неизвестный вход → отказ; сбой публикации →
ошибка, никогда не ложный `ok`.

## Проверка контракта

Реестровая строка `mqtt-set-contract`
(`.ai-dev/quality/checks/mqtt-set-contract.sh`, beat `build`) — единственный
исполняемый владелец инвариантов выше. Гоняет ШТАТНЫЙ CGI в песочнице с
настоящей сессией из `lib_web_auth.sh` и записывающими шимами
`mosquitto_pub`/`timeout` (набор случаев — в самом скрипте: отказы без публикации, принятые
векторы, отсутствие `-r`, loopback-константы, `timeout 5`, строка аудита,
порядок auth→CSRF→allow-list→publish). Строка `comment-mutation-proof`
дополнительно доказывает, что закомментированный `timeout 5 mosquitto_pub`
или `web_csrf_validate` роняет её в RED.

### Что именно она проверяет (рецепт, для ручного повтора)

Локально, без устройства (scratchpad-харнесс, `web-diagnostic-tools.md`):
подложить в `PATH` фейковый `mosquitto_pub`, записывающий argv, и вызвать CGI
со стабом окружения (`REQUEST_METHOD=POST`, `CONTENT_LENGTH`, тело на stdin;
авторизация — стаб `lib_web_auth.sh` либо валидная сессия). Assert:

1. без сессии → `unauthorized`, публикации нет; с валидной сессией, но без
   заголовка `X-SA02M-CSRF` (или с неверным значением) → `E_CSRF`, публикации
   нет; с валидным `X-SA02M-CSRF` — публикация проходит (см. п. 3);
2. отказы (публикации нет): `device=a;rm` → `bad_device`; `control=do_17`,
   `control=ao_13`, `control=do_1;x` → `bad_control`; `value=2` (для `do_1`),
   `value=1001` / `value=x` (для `ao_1`) → `bad_value`;
3. валидные запросы → ровно одна публикация, топик
   `/devices/<device>/controls/<control>/on`, payload = `value`,
   **в argv нет `-r`**, есть `timeout 5`. Проверить и DO (`control=do_3`
   `value=1`), и AO (`control=ao_1` `value=500` → payload `500`; границы
   `value=0` и `value=1000` приняты). Для типа датчика: `control=ai_type_1`
   `value=0` → payload `0`; `control=ai_type_12` `value=42` → payload `42`;
   `control=ai_type_13` → `bad_control`; `value=43` → `bad_value`.
