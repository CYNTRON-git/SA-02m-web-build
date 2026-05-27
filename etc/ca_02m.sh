#!/bin/bash
# Устарело: бипер и LED при старте перенесены в sa02m-pre-start.sh (sysinit.target).
# ca_02m.service (After=network.target) давал сигнал только через ~50 с после загрузки.
# Оставлен no-op, чтобы старые образы не дублировали индикацию при обновлении только скрипта.
logger -t ca_02m "skipped — boot indication handled by sa02m-pre-start.service"
exit 0
