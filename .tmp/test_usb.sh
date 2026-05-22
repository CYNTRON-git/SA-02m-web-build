#!/bin/bash
set -a
source /etc/sa02m_hw.conf 2>/dev/null || true
set +a
source /var/www/network_config/cgi-bin/lib_hw.sh

result=$(sa02m_hw_usb_gpiod_read)
echo "HW_USB_READ=${result}"

sa02m_hw_collect_metrics
echo "HW_USB_COLLECT=${HW_USB}"
echo "PIN_USB=${PIN_USB}"
echo "HW_CFG=${HW_CFG}"
