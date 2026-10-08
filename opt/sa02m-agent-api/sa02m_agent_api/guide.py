# -*- coding: utf-8 -*-
"""Text the board hands an agent at initialize (resource sa02m://guide)."""

GUIDE = """\
СА-02м — сервер автоматизации (Allwinner A40i, Linux). Этот процесс — его
машинный API. Делай только то, что просит оператор. Порт панели 9999.
Руководство для человека: docs/AI_AGENT_INTEGRATION.md.

Чтение — GET /api/v1/<домен>/<глагол>?ключ=значение или tools/call.
Мутация — POST JSON. Долгое по REST сразу отдаёт job_id; результат —
GET /api/v1/jobs/<id>. Неверные аргументы — ошибка сразу, без job_id.
"wait": true отвечает синхронно. MCP долгий инструмент тоже отвечает
синхронно.

rules.run и rules.delete — {"id": "<id>"}. rules.upsert — {"scenarios": [строка]}.
mqtt.scan требует port (/dev/…). mqtt.publish — device, control, value.
devices.history — device_id и group (energy, climate, ahu, mtd) или kind
(carel, mr; к нему metric, channel), окно — range или window_s.
devices.summary — range, device_id, kwh_rub. Ответ демона — строка JSON в body.

Топики MQTT — Wiren Board: /devices/<id>/controls/<name>. Запись значения —
публикация в .../on без retain (mqtt.publish). Локальный брокер 127.0.0.1:1883
без пароля: это шина объекта, не секретный канал.

Сценарии живут в /etc/sa02m-rules/scenarios.json. Движок перечитывает файл.
rules.run пишет sidecar; rules.off публикует 0 в
/devices/sa02m-rules-<id>/controls/run/on.

Свой код клади только в /opt/sa02m-user. Юнит — sa02m-user@<имя>.service,
старт из start.sh. За пределы каталога путь не выйдет: .. и симлинки
отклоняются.

Необратимое (reboot, kernel.set, web_update.apply, factory_reset,
flasher.flash, web_creds.set) требует confirm равный имени операции.
Сначала dry_run, если не уверен.

Root (shell.exec mode=root) есть только у токена с root_capable. Не проси его
и не выдумывай обход. Токен не выпускает другие токены.

Node-RED на плате слушает :1880. Если adminAuth не включён, порт открыт в LAN
сам по себе — прокси это не меняет.

OpenAPI: ресурс sa02m://openapi. Контракт для человека:
docs/contracts/agent-api.md.
"""

PROMPTS = {
    "onboarding": {
        "description": "Изучи плату и предложи план работ, ничего не меняя.",
        "text": "Прочитай sa02m://guide и system.info. Перечисли, что уже настроено, "
                "и предложи план. Не вызывай мутации, пока оператор не подтвердит.",
    },
    "diagnose": {
        "description": "Почему устройство X не в сети.",
        "text": "По mqtt.devices.live и logs.journal (юнит sa02m-modbus-mqtt) объясни, "
                "почему названное устройство не отвечает. Не меняй конфиг.",
    },
}
