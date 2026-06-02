#!/bin/bash
# SA-02m: replace upstream Armbian board branding in release metadata and MOTD.
# MOTD (10-armbian-header) reads BOARD_NAME/VENDOR from /etc/armbian-release last.
set -euo pipefail

BOARD_ID="${SA02M_ARMBIAN_BOARD:-SA-02m}"
BOARD_LABEL="${SA02M_ARMBIAN_BOARD_NAME:-CYNTRON SA-02m}"
VENDOR_NAME="${SA02M_ARMBIAN_VENDOR:-CYNTRON}"
VENDOR_SUPPORT="${SA02M_ARMBIAN_SUPPORT:-https://cyntron.ru}"
MOTD_HEADER="/etc/update-motd.d/10-armbian-header"

set_kv() {
    local file=$1 key=$2 value=$3 tmp
    [ -f "$file" ] || return 0
    tmp="${file}.tmp.$$"
    awk -F= -v key="$key" -v value="$value" '
        BEGIN { done = 0 }
        $1 == key {
            if (value ~ /[[:space:]"\\]/) {
                gsub(/"/, "\\\"", value)
                print key "=\"" value "\""
            } else {
                print key "=" value
            }
            done = 1
            next
        }
        { print }
        END {
            if (!done) {
                if (value ~ /[[:space:]"\\]/) {
                    gsub(/"/, "\\\"", value)
                    print key "=\"" value "\""
                } else {
                    print key "=" value
                }
            }
        }
    ' "$file" > "$tmp"
    install -m 644 "$tmp" "$file"
    rm -f "$tmp"
}

for f in /etc/armbian-release /etc/armbian-image-release; do
    set_kv "$f" BOARD "$BOARD_ID"
    set_kv "$f" BOARD_NAME "$BOARD_LABEL"
    set_kv "$f" VENDOR "$VENDOR_NAME"
    set_kv "$f" VENDORSUPPORT "$VENDOR_SUPPORT"
done

patch_motd_support_line() {
    local file=$1
    [ -f "$file" ] || return 0
    if grep -q 'cyntron.ru' "$file"; then
        return 0
    fi
    local tmp="${file}.tmp.$$"
    awk '
        /"csc"\|"tvb"\) HARDWARE_STATUS=\$'"'"'\\e\[0;91mDIY \(community maintained\)\\x1B\[0m'"'"' ;;/ {
            print "\t\t\"csc\"|\"tvb\")"
            print "\t\t\tif [[ \"${BOARD:-}\" == \"SA-02m\" || \"${VENDOR:-}\" == \"CYNTRON\" ]]; then"
            print "\t\t\t\tHARDWARE_STATUS=$'"'"'\\e[0;92mcyntron.ru\\x1B[0m'"'"'"
            print "\t\t\telse"
            print "\t\t\t\tHARDWARE_STATUS=$'"'"'\\e[0;91mDIY (community maintained)\\x1B[0m'"'"'"
            print "\t\t\tfi"
            print "\t\t\t;;"
            next
        }
        { print }
    ' "$file" > "$tmp"
    install -m 755 "$tmp" "$file"
    rm -f "$tmp"
}

patch_motd_support_line "$MOTD_HEADER"
