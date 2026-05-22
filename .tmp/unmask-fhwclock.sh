#!/bin/bash
set -u
# Armbian image маскирует fake-hwclock симлинком в /lib и /usr/lib на /dev/null.
# Стандартный `systemctl unmask` не уберёт vendor-уровень. Делаем явный override
# в /etc/systemd/system/fake-hwclock.service — он имеет приоритет над vendor.
echo "[$(date +%H:%M:%S)] Создаём свой fake-hwclock.service в /etc/systemd/system/"

cat >/etc/systemd/system/fake-hwclock.service <<'EOF'
[Unit]
Description=Restore / save the current clock (SA-02m unmasked)
DefaultDependencies=no
Documentation=man:fake-hwclock(8)
Before=local-fs-pre.target
After=systemd-remount-fs.service
ConditionPathExists=!/run/systemd/fake-hwclock-loaded

[Service]
Type=oneshot
ExecStart=/usr/sbin/fake-hwclock load
ExecStart=/bin/touch /run/systemd/fake-hwclock-loaded
ExecStop=/usr/sbin/fake-hwclock save
RemainAfterExit=yes

[Install]
WantedBy=sysinit.target
EOF

# Удаляем dropdir на vendor-юнит и создаём dropdir заново для нашего.
mkdir -p /etc/systemd/system/fake-hwclock.service.d
cat >/etc/systemd/system/fake-hwclock.service.d/sa02m.conf <<'EOF'
[Service]
# Сохранять время также при штатной перезагрузке системы.
ExecStop=/usr/sbin/fake-hwclock save
EOF

systemctl daemon-reload

# Старый symlink (sysinit.target.wants → fake-hwclock.service) указывает на
# наш unit; на всякий случай enable снова создаст его при необходимости.
systemctl enable fake-hwclock.service 2>&1 | tail -3

# Если время сейчас валидное, сохраним сразу.
if [ "$(date +%Y)" -ge 2024 ]; then
    fake-hwclock save 2>/dev/null && echo "  fake-hwclock saved: $(cat /etc/fake-hwclock.data)"
fi

systemctl is-active fake-hwclock 2>&1
systemctl is-enabled fake-hwclock 2>&1
systemctl status fake-hwclock --no-pager 2>&1 | head -10
