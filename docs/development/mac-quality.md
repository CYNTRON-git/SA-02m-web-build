# Проверки проекта на Mac

Запускайте канонический build gate из корня Git-дерева:

```sh
~/cyntron-build/venvs/nms-edge/bin/python scripts/dev/mac-quality.py build --touched
```

Без `--touched` исполняются все строки выбранного beat. Для известной базы можно
передать `--touched <commit>`; также поддерживаются `review` и `ship`.
Адаптер использует действующий `.ai-dev/quality/tools.json` и его штатный
`run.mjs`: фильтрация scope, PASS/PARTIAL/SKIP/FAIL и итоговый exit code остаются
в каноническом runner. Исходные команды и assertions не переписываются.

Нужны уже установленные Node, Docker с контекстом `colima-cyntron`, образ
`cyntron-build:sa-native-quality`, venv `nms-edge` (Python Playwright,
paho-mqtt, pytest, openpyxl) и venv `firmware` (pyserial). Пути задаются
`--build-root ~/cyntron-build`, `--context` и `--image`. Для frontend нужен
`npm ci --prefix scripts/dev` и штатный Chromium этой версии Playwright
(`cd scripts/dev && npx playwright install chromium`). Адаптер ничего не
скачивает, не перезапускает Colima и не меняет пользовательские контейнеры.

На Mac выполняются строки с суффиксом `-browser` или `-card-smoke`, строки
`ui-layout` и `sh-modal-layout-smoke`, а также команды с проверкой Playwright.
Node использует Chromium своего Playwright. Для `status-snapshot-browser`
адаптер добавляет venv `nms-edge` в начало `PATH`, чтобы команда из реестра
нашла нужный `python3`. Пути и версии Chromium записываются в `tools.json`.
Serial fixtures получают `PYTHON` из venv `firmware`.

Linux-строки запускаются с `docker run --init`. Процесс init занимает PID 1,
и проверка шлюза в `install-maintenance` получает обычный PID тестового процесса.
Колёса pure Python из `nms-edge` копируются во временный каталог. Файл `.pth`
делает зависимости доступными дочерним fixtures, которые заменяют `PYTHONPATH`.

Артефакты находятся в `.tmp/mac-quality-<timestamp>/`. В `quality.log` есть
полный вывод, в `runner.log` — только сообщения канонического runner.
`summary.json` берёт вердикты из `runner.log`, поэтому собственные строки
`PASS` и `FAIL` внутри fixture не попадают в сводку. В `rows.jsonl` записаны
исходная команда, платформа, код выхода и содержимое skip-файла. `--artifacts`
задаёт новый каталог внутри проекта. Для строгого запуска задайте `QUALITY_STRICT=1`.

Сетевая проверка cache-bust-r может дать SKIP при отсутствии нужных origin refs;
это отдельный непроверенный gate, не PASS. Unittest может явно пропустить
платформенный вариант; canonical runner показывает PARTIAL и число таких тестов.
Недостающий paho нельзя трактовать как неисправность LED ownership: импорт
реального bridge producer намеренно закрывает admission при недоступной зависимости.
