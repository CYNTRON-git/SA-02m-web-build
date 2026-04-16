#!/bin/bash
echo "Content-type: text/html; charset=utf-8"
echo "Location: /cgi-bin/index.cgi?status=reboot"
echo ""
sleep 2
sudo reboot
exit 0
