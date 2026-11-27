# Допуск зависимостей выпуска

Выпуск и золотой образ требуют свежего допуска всех областей из
`vendor-src/admission/policy.json`. Подтверждённый High или Critical даёт
`FAIL` и код 1. Неполное покрытие, ошибка провайдера, неизвестная тяжесть и
неразрешённое расхождение дают `INDETERMINATE` и код 2. При одновременном
наличии обоих условий итоговый код равен 1, пробелы перечислены в отчёте.
Moderate, Low и Info остаются с владельцем дальнейшей работы. Политика
текущего выпуска содержит пустой список общих исключений. Для одного CVE
допускается независимое заключение `not_affected` по точному артефакту и
потребителю. Отчёт сохраняет исходную тяжесть и версионное совпадение.
Изменение входов, условий применимости или данных провайдера требует повторной
проверки. Неразрешённая применимость блокирует допуск.

| Область | Входы и проверка |
| --- | --- |
| Node-RED | Lock и все именованные версии в точном tar, включая физические копии внутри npm. Официальный npm Bulk Security API и upstream semver из архива дополнены запросом точных версий в OSV. |
| Нативный Node | Проверенный ARM-архив, опубликованный индекс Node.js Security WG и upstream semver. Содержимое npm и corepack в этом архиве входит в отдельную рекурсивную инвентаризацию npm. |
| Сторонний Node | Перепись фактических путей Node и package.json существующих CLI на устройстве. Каждая физическая установка сохраняется отдельно. |
| Python продукта | Пять lock-файлов, SHA и метаданные каждого wheel, двоичные цели системного криптомоста и квитанции native prepare для каждой платы. OSV проверяет точные версии PyPI. Пакеты моста проходят по источникам Ubuntu. |
| Нативные зависимости OPC UA | SHA wheel, внутреннего ELF, SBOM, исходников и конфигурации, единственный потребитель и квитанции целевых плат. Текущее дерево OpenSSL CNA сопоставляется с проверенным архивом. Для каждого совпадения сверяются актуальный CVE Record и независимое заключение. Публичные Rust-пакеты из CycloneDX проходят проверку OSV по версиям crates.io. Собственные компоненты PyCA привязаны к версии wheel. |
| Глобальный Python | Наблюдаемые вне dpkg дистрибутивы в /usr/local проверяются по фактическим именам и версиям. Это отдельная область состояния платы. |
| HomeKit | Lock, установленный состав, RECORD и происхождение наследуемых модулей. Для закреплённого ARM wheel проверяются ELF, оба CycloneDX, версии публичных Rust-пакетов и версия встроенного OpenSSL. При установленном HomeKit сверяются фактические SHA и версия backend. Диапазоны OpenSSL с объявленной семантикой semver проверяет upstream semver. Прочие форматы сохраняются как пробел покрытия. |
| Сборка и разработка | npm lock инструментов разработки, точные инструменты и рецепты сборки PyYAML и CFFI, закреплённая библиотека разбора CVSS. |
| Ubuntu и ядро | Установленные source package/version с бинарными именами и архитектурой. История публикаций Launchpad подтверждает происхождение версии. OSV сопоставляет диапазоны Noble, Ubuntu Security Tracker уточняет статус данного source в Noble. Отдельные vendor-пакеты и ядро требуют собственного покрытия. SHA кандидата zImage на FAT фиксируется вместе с uname. |

Для USN проверяются связанные CVE с учётом конкретного пакета. Для каждой
совпавшей Ubuntu CVE сохраняются приоритет Canonical, upstream severity, статус
Noble и заявленное исправление. При `needs-triage` применимость требует
дальнейшей проверки. Пересечение порога блокировки только по upstream severity
при более низком приоритете Canonical требует отдельного решения владельца.
Успешный HTTP-ответ с пустым списком сохраняется с запросом и счётчиками.

## Запуск

На Linux amd64 используется проверенный Node из `vendor-src/nodered/admission-policy.json`.
В `targets.json` задаются `schema: 1` и объект `devices`. Для каждой платы
пути относительно файла целей указывают на `inventory` (dpkg и HomeKit),
`system_python`, `python_receipt`, `boot_artifacts`, `node_lineage` и при
наличии соответствующих установок `node_packages`, `node_versions`,
`homekit_integrity`, `homekit_origins`. Поле верхнего уровня `openssl_review`
задаёт каталог с файлами независимой проверки `review.md`,
`owned-source-sha256.json`, `openssl-release-metadata.tar.gz`,
`native-cve-metrics.json`. Политика фиксирует SHA заключения и архива OpenSSL
CNA. Поле `homekit_arm_wheel` задаёт путь к ARM-файлу из HomeKit lock.
У установленного HomeKit поле платы `homekit_native` указывает на результат
проверки UID 33 с фактическими путями, SHA ELF и SBOM, версией OpenSSL.
Поле платы `opcua_profile` сопоставляет установленный entry, конфигурацию,
launcher и карту потребителей по SHA, прочитанный durable selector, SHA
выбранной квитанции и действующий ExecStart. Для работающей службы также
проверяется командная строка действительного PID. Для остановленной службы
проверяется состояние inactive/dead. Исключения по применимости остаются
открытыми до подтверждения этого профиля.

    python3 scripts/dev/release-admission.py scan --artifact-dir /work/payload --tool-dir /work/node/bin --targets /work/evidence/targets.json --out /work/new-report
    python3 scripts/dev/release-admission.py check --artifact-dir /work/payload --tool-dir /work/node/bin --targets /work/evidence/targets.json --out /work/new-report

Если доступ к Ubuntu Security Tracker выполняется с отдельного рабочего места,
можно сохранить его официальные ответы командой ниже и передать каталог
обеим командам через `--ubuntu-capture /work/noble`. Проверяющий повторяет
запросы по сохранённым байтам, проверяет точные SHA исходных dpkg-инвентарей
и срок действия сбора.

    python3 scripts/dev/release-admission.py capture-ubuntu --targets /work/evidence/targets.json --out /work/noble

Сбор ответов OSV для npm и PyPI также допускает раздельные рабочие места.
Команда ниже требует того же checkout, полного пакета и набора свидетельств.
Для повторной оценки к `scan` и `check` добавляется
`--ecosystem-capture /work/ecosystems`.

    python3 scripts/dev/release-admission.py capture-ecosystems --artifact-dir /work/payload --targets /work/evidence/targets.json --out /work/ecosystems

`admission.json` сохраняет решение каждой области, пути физических копий,
идентификаторы, SHA входов, инструментов, политики и ответов. Содержимое
ответов хранится в `provider/`, отчёт npm в `npm/`. `check` повторяет решение
по этим материалам без сетевого доступа. Допуск действует максимум 24 часа
от начала сбора при неизменных входах. Исходное время сохраняется при
повторной проверке. Недоступность проверенного бинарного OSV-Scanner при
проверке происхождения фиксируется в свидетельствах запуска. Политика
использует официальный API OSV и проверяемые данные поставщиков.

Первичные описания провайдеров приведены в [npm Security](https://docs.npmjs.com/cli/v11/commands/npm-audit#bulk-advisory-endpoint),
[OSV](https://google.github.io/osv.dev/api/), [Ubuntu OSV](https://documentation.ubuntu.com/security/security-updates/osv/)
и [Ubuntu VEX](https://documentation.ubuntu.com/security/security-updates/vex/).
