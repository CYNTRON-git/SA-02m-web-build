# Альтернатива: Home Assistant + Yaha Cloud

Поддерживаемый **внешний** путь для пользователей с отдельным хостом Home
Assistant. Это **не** primary-путь SA-02m и **не** входит в базовый образ.

Native-путь (Cyntron Alice Gateway) — [ALICE_INTEGRATION.md](ALICE_INTEGRATION.md).

## Поток

```text
SA-02m MQTT → Home Assistant (внешний хост)
           → компонент Yandex Smart Home
           → Yaha Cloud relay
           → навык «Yaha Cloud»
           → Алиса
```

## Требования к внешнему HA

- Home Assistant ≥ 2025.12
- HACS + компонент `yandex_smart_home`
- тип подключения «Облачное (Yaha Cloud)»
- MQTT-брокер SA-02m доступен с HA (`IP:1883`, см. [MQTT_TOPICS.md](MQTT_TOPICS.md))
- привязка навыка «Yaha Cloud» в приложении «Дом с Алисой» (не Quasar web)

Источники:

- [WB: HA_Alice](https://wiki.wirenboard.com/wiki/HA_Alice)
- [WB: Home Assistant — установка](https://wiki.wirenboard.com/wiki/Home_Assistant)
- [Yaha Cloud docs](https://docs.yaha-cloud.ru/latest/)

## Почему не на самом SA-02m

- ~492 MiB RAM и нагрузка industrial-стека (CODESYS / MPLC / Node-RED)
- armhf HA EOL (2025)
- Docker + volume конфликтуют с принятой моделью образа

## Доверие / privacy

| Узел | Что видит |
|---|---|
| Yaha Cloud | сущности HA (по политике Yaha; шифрование/анонимизация — в их docs) |
| Яндекс | fulfillment навыка |
| SA-02m backup | только локальные conf/MQTT; **не** `.storage` HA |

Владелец внешнего HA сам бэкапит конфигурацию HA.

## Приёмочный тест (ручной, внешняя лаборатория)

1. Изменить MQTT control на SA-02m (`…/controls/…/on`).
2. Убедиться, что entity в HA обновилась.
3. Запрос к Алисе через навык Yaha отражает новое состояние.
