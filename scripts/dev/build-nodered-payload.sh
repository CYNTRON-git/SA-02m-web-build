#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# build-nodered-payload.sh • собирает оффлайн-payload Node-RED для СА-02м
#
# ЕДИНСТВЕННЫЙ дом команд сборки payload'а. Форма payload'а и объяснение
# «почему так» — docs/vendor-integrations.md → Node-RED (оффлайн);
# здесь только исполняемый рецепт.
#
# Что получается на выходе ($OUT):
#   node-red-<pin>.tar.gz            дерево node-red С ВЛОЖЕННЫМИ зависимостями
#                                    (единственный каталог верхнего уровня
#                                    node-red/ — ctl-скрипт это проверяет)
#   node-v<ver>-linux-armv7l.tar.xz  официальный tarball Node.js, sha256 сверен
#   nodered.service                  копия etc/systemd/system/nodered.service
#   BUILD-INFO.txt                   провенанс: чем, из чего и с какими суммами
#
# Запускать НА ПЛАТЕ (armv7l), от root, из дерева репозитория:
#   sudo bash scripts/dev/build-nodered-payload.sh --out /root/nodered-payload
#
# Живую установку не трогает: сборка идёт в отдельный каталог, `npm ci` c
# --prefix, никакого `npm install -g`.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Пин версии node-red. Один из его домов (см. quality-строку
# `nodered-pin-consistency`); фактическое дерево берётся из
# vendor-src/nodered/package-lock.json, здесь — только сверка.
NODERED_PIN_VERSION="4.1.13"
NODE_MAJOR="22"

SRC_DIR="$REPO_ROOT/vendor-src/nodered"
UNIT_SRC="$REPO_ROOT/etc/systemd/system/nodered.service"
OUT="/root/nodered-payload"
BUILD_DIR="/root/nr-build"
NODE_VERSION=""          # пусто → берём текущий LTS ветки $NODE_MAJOR
ALLOW_ANY_ARCH=0
KEEP_BUILD=0

usage() {
    cat <<EOF
Usage: sudo bash $0 [--out DIR] [--build-dir DIR] [--node-version vX.Y.Z]
                    [--allow-any-arch] [--keep-build]

  --out DIR           куда сложить payload (по умолчанию $OUT)
  --build-dir DIR     рабочий каталог сборки (по умолчанию $BUILD_DIR)
  --node-version      конкретная версия Node.js (иначе — текущий LTS ${NODE_MAJOR}.x);
                      для воспроизводимой пересборки укажите ту, что записана
                      в BUILD-INFO.txt прошлой сборки
  --allow-any-arch    собрать не на armv7l (дерево чистый JS, но это отмечается
                      в BUILD-INFO.txt)
  --keep-build        не удалять рабочий каталог после сборки
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out)            OUT="$2"; shift 2 ;;
        --build-dir)      BUILD_DIR="$2"; shift 2 ;;
        --node-version)   NODE_VERSION="$2"; shift 2 ;;
        --allow-any-arch) ALLOW_ANY_ARCH=1; shift ;;
        --keep-build)     KEEP_BUILD=1; shift ;;
        -h|--help)        usage; exit 0 ;;
        *) echo "Неизвестный аргумент: $1" >&2; usage; exit 2 ;;
    esac
done

die() { echo "[ERR] $*" >&2; exit 1; }
say() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── Pre-flight ─────────────────────────────────────────────────────────────
[ "$(id -u)" = "0" ] || die "нужен root (модами файлов payload'а управляем явно)"
[ -f "$SRC_DIR/package.json" ] && [ -f "$SRC_DIR/package-lock.json" ] \
    || die "нет $SRC_DIR/package.json + package-lock.json — payload собирается ТОЛЬКО из закоммиченного lock-файла"
[ -f "$UNIT_SRC" ] || die "нет $UNIT_SRC — unit копируется из репозитория, а не генерируется здесь"
command -v npm >/dev/null 2>&1 || die "нет npm"
command -v curl >/dev/null 2>&1 || die "нет curl"
command -v sha256sum >/dev/null 2>&1 || die "нет sha256sum"

# Сверка пина: lock-файл — источник истины дерева, константа выше — то, что
# знают потребители. Разъехались — сборка не начинается.
LOCK_NR_VERSION=$(sed -n '/"node_modules\/node-red"/,/}/p' "$SRC_DIR/package-lock.json" \
    | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' | head -n1)
[ -n "$LOCK_NR_VERSION" ] || die "не удалось прочитать версию node-red из package-lock.json"
[ "$LOCK_NR_VERSION" = "$NODERED_PIN_VERSION" ] \
    || die "package-lock.json содержит node-red $LOCK_NR_VERSION, а пин — $NODERED_PIN_VERSION"

BUILD_ARCH="$(uname -m)"
if [ "$BUILD_ARCH" != "armv7l" ] && [ "$ALLOW_ANY_ARCH" -ne 1 ]; then
    die "сборка рассчитана на плату (armv7l), здесь $BUILD_ARCH; осознанно — --allow-any-arch"
fi

# Свободное место: дереву нужно ~2× (распакованное + архив) плюс tarball Node.
for fs in "$(dirname "$BUILD_DIR")" "$(dirname "$OUT")"; do
    avail_kb=$(df -Pk "$fs" 2>/dev/null | awk 'NR==2{print $4}')
    say "df $fs: свободно $(( ${avail_kb:-0} / 1024 )) MB"
    [ "${avail_kb:-0}" -ge 700000 ] || die "на $fs меньше 700 MB — сборка не начинается"
done

[ -e "$BUILD_DIR" ] && die "$BUILD_DIR уже существует — уберите его или задайте --build-dir"
mkdir -p "$BUILD_DIR" "$OUT"

# ── 1. Дерево node-red из закоммиченного lock-файла ────────────────────────
# npm ci ставит РОВНО то, что в package-lock.json (и падает при расхождении с
# package.json) — в отличие от npm install, который перерешает диапазоны.
# --omit=optional убирает единственную нативную зависимость (@node-rs/bcrypt):
# дерево становится чистым JS — без компилятора на плате и без привязки к ABI
# конкретного мажора Node.
cp "$SRC_DIR/package.json" "$SRC_DIR/package-lock.json" "$BUILD_DIR/"
say "npm ci --omit=optional (это долго на A40i — десятки минут)"
NPM_START=$(date +%s)
( cd "$BUILD_DIR" && npm ci --omit=optional --no-audit --no-fund )
NPM_SECONDS=$(( $(date +%s) - NPM_START ))
say "npm ci завершён за ${NPM_SECONDS}s"

[ -f "$BUILD_DIR/node_modules/node-red/red.js" ] || die "после npm ci нет node_modules/node-red/red.js"

# Разрешённый манифест — до перекладки дерева, пока структура плоская.
RESOLVED_JSON="$BUILD_DIR/npm-ls.json"
( cd "$BUILD_DIR" && npm ls --all --json >"$RESOLVED_JSON" 2>/dev/null ) || true

# ── 2. Перекладка: один каталог верхнего уровня ────────────────────────────
# npm ci поднимает зависимости плоско. Если так и запаковать, в
# /usr/lib/node_modules прилетят сотни соседних каталогов. Вкладываем их в
# node-red/node_modules — резолвер Node ищет вверх по дереву и находит их
# первыми, поведение то же, а верхнеуровневый каталог ровно один.
# Правило слияния: то, что npm уже вложил в node-red/node_modules, ПОБЕЖДАЕТ
# (он вложил это осознанно — там несовместимая версия).
cd "$BUILD_DIR"
mv node_modules/node-red ./node-red
mkdir -p node-red/node_modules
rm -f node_modules/.package-lock.json   # служебный файл npm, в рантайме не нужен

# Пропущенные (уже вложенные npm'ом) записи собираются сюда: молча потерять
# их нельзя — на будущем lock-файле коллизия сменила бы версию, которую видят
# все соседи, а `rm -rf node_modules` ниже стёр бы улику.
SKIPPED=""

merge_into() {          # merge_into <исходный каталог> <каталог назначения>
    local src=$1 dst=$2 item base
    mkdir -p "$dst"
    for item in "$src"/* "$src"/.[!.]*; do
        [ -e "$item" ] || continue
        base=${item##*/}
        if [ -e "$dst/$base" ]; then
            SKIPPED="$SKIPPED ${dst#node-red/node_modules/}/$base"
            continue
        fi
        mv "$item" "$dst/$base"
    done
}

for entry in node_modules/* node_modules/.[!.]*; do
    [ -e "$entry" ] || continue
    name=${entry##*/}
    case "$name" in
        @*|.bin) merge_into "$entry" "node-red/node_modules/$name" ;;
        *)
            if [ -e "node-red/node_modules/$name" ]; then
                SKIPPED="$SKIPPED $name"
                continue
            fi
            mv "$entry" "node-red/node_modules/$name"
            ;;
    esac
done
if [ -n "$SKIPPED" ]; then
    say "ВНИМАНИЕ: у node-red уже были свои копии, поднятые версии отброшены:$SKIPPED"
    say "  (правило слияния: вложенное npm'ом побеждает — проверьте, что это ожидаемо)"
else
    say "коллизий при перекладке нет — все поднятые пакеты перенесены"
fi
rm -rf node_modules

# Ссылки в .bin относительные и переезжают вместе с целями — кроме ссылок на
# сам пакет node-red (`.bin/node-red` → `../node-red/red.js`): он стал корнем
# дерева, а не соседом. Такие ссылки после перекладки битые; удаляем, чтобы в
# payload не уехал висячий симлинк. Точка входа на устройстве — /usr/bin/node-red,
# её создаёт установщик, а не .bin.
if [ -d node-red/node_modules/.bin ]; then
    BROKEN=$(find node-red/node_modules/.bin -maxdepth 1 -type l ! -exec test -e {} \; -print -delete | wc -l)
    say "битых ссылок в .bin удалено: $BROKEN"
fi

# ── 3. Режимы (в образ payload попадает через cp -a — моды сохраняются) ────
find node-red -type d -exec chmod 0755 {} +
find node-red -type f -exec chmod 0644 {} +
# Исполняемые точки входа и скрипты-обёртки остаются исполняемыми.
find node-red -type f -name '*.sh' -exec chmod 0755 {} +
chmod 0755 node-red/red.js
chown -R root:root node-red

NR_TARBALL="$OUT/node-red-${NODERED_PIN_VERSION}.tar.gz"
TAR_REPRO=()
tar --sort=name --version >/dev/null 2>&1 && TAR_REPRO+=(--sort=name)
say "упаковка $NR_TARBALL"
tar "${TAR_REPRO[@]}" --owner=root --group=root --numeric-owner -czf "$NR_TARBALL" node-red
TREE_KB=$(du -sk node-red | awk '{print $1}')

# Проверка того самого инварианта, который проверяет установщик.
TOPS=$(tar -tf "$NR_TARBALL" | sed 's#^\./##; s#/.*##' | grep -v '^$' | sort -u)
[ "$TOPS" = "node-red" ] || die "в архиве больше одного каталога верхнего уровня: $(echo "$TOPS" | tr '\n' ' ')"

# ── 4. Node.js: официальный tarball + сверка sha256 ────────────────────────
if [ -z "$NODE_VERSION" ]; then
    say "определяю текущий LTS Node.js ${NODE_MAJOR}.x"
    NODE_VERSION=$(curl -fsS --max-time 60 https://nodejs.org/dist/index.json \
        | tr '{' '\n' | grep '"version":"v'"$NODE_MAJOR"'\.' | grep -v '"lts":false' \
        | sed -n 's/.*"version":"\(v[0-9.]*\)".*/\1/p' | head -n1)
    [ -n "$NODE_VERSION" ] || die "не удалось определить версию Node ${NODE_MAJOR}.x с nodejs.org"
fi
case "$NODE_VERSION" in v*) ;; *) NODE_VERSION="v$NODE_VERSION" ;; esac
NODE_TARBALL="node-${NODE_VERSION}-linux-armv7l.tar.xz"
NODE_BASE="https://nodejs.org/dist/${NODE_VERSION}"
say "загрузка ${NODE_TARBALL}"
curl -fsS --max-time 900 -o "$OUT/$NODE_TARBALL" "${NODE_BASE}/${NODE_TARBALL}"
curl -fsS --max-time 120 -o "$BUILD_DIR/SHASUMS256.txt" "${NODE_BASE}/SHASUMS256.txt"
# Сверяем ТОЛЬКО нашу строку: в файле суммы всех платформ, а sha256sum -c
# упал бы на отсутствующих файлах.
grep " ${NODE_TARBALL}\$" "$BUILD_DIR/SHASUMS256.txt" >"$BUILD_DIR/our.sha256" \
    || die "в SHASUMS256.txt нет строки для ${NODE_TARBALL}"
if ! ( cd "$OUT" && sha256sum -c "$BUILD_DIR/our.sha256" ); then
    rm -f "$OUT/$NODE_TARBALL"
    die "sha256 ${NODE_TARBALL} не сошёлся — загруженный файл удалён"
fi
NODE_SHA=$(awk '{print $1}' "$BUILD_DIR/our.sha256")
# ЧЕСТНО: подпись SHASUMS256.txt.sig (GPG-ключи релиз-менеджеров Node.js) НЕ
# проверяется — цепочка доверия здесь ровно до TLS nodejs.org, не дальше.

# ── 5. Unit — копируем из репозитория, не генерируем ───────────────────────
install -m 0644 -o root -g root "$UNIT_SRC" "$OUT/nodered.service"

# ── 6. BUILD-INFO.txt — провенанс ──────────────────────────────────────────
NR_SHA=$(sha256sum "$NR_TARBALL" | awk '{print $1}')
{
    echo "SA-02m Node-RED offline payload"
    echo "built:            $(date -Is)"
    echo "node-red:         $NODERED_PIN_VERSION (из vendor-src/nodered/package-lock.json)"
    echo "build host arch:  $BUILD_ARCH$( [ "$ALLOW_ANY_ARCH" -eq 1 ] && echo '  (--allow-any-arch: собрано НЕ на плате)')"
    echo "build host uname: $(uname -srvm)"
    echo "glibc:            $(ldd --version 2>/dev/null | head -n1)"
    echo "build node:       $(node --version 2>/dev/null || echo '-')"
    echo "build npm:        $(npm --version 2>/dev/null || echo '-')"
    echo "npm command:      npm ci --omit=optional --no-audit --no-fund"
    echo "npm duration:     ${NPM_SECONDS}s"
    echo "tree layout:      единственный каталог node-red/ с вложенным node_modules/"
    echo "hoist collisions:${SKIPPED:- нет}"
    echo "tree size:        $(( TREE_KB / 1024 )) MB (распакованное дерево)"
    echo "archive:          $(basename "$NR_TARBALL")  $(du -k "$NR_TARBALL" | awk '{print int($1/1024)" MB"}')"
    echo "archive sha256:   $NR_SHA"
    echo "node tarball:     $NODE_TARBALL"
    echo "node sha256:      $NODE_SHA  (сверено с ${NODE_BASE}/SHASUMS256.txt)"
    echo "node sig check:   НЕТ — SHASUMS256.txt.sig не проверяется, доверие ограничено TLS nodejs.org"
    echo "unit:             nodered.service (копия etc/systemd/system/nodered.service)"
    echo
    echo "--- resolved dependency manifest (npm ls --all --json) ---"
    cat "$RESOLVED_JSON" 2>/dev/null || echo "(npm ls не отработал)"
} >"$OUT/BUILD-INFO.txt"

find "$OUT" -type d -exec chmod 0755 {} +
find "$OUT" -type f -exec chmod 0644 {} +
chown -R root:root "$OUT"

if [ "$KEEP_BUILD" -eq 0 ]; then
    cd /
    rm -rf "$BUILD_DIR"
fi

say "готово: $OUT"
ls -la "$OUT"
echo
echo "Дальше — доставка payload'а на устройство и установка: docs/deployment.md"
