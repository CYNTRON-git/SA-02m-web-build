#!/bin/bash
echo "Content-type: text/html; charset=utf-8"
echo "Location: /cgi-bin/index.cgi?status=services"
echo ""

            {
            echo "Restart services"
            } >> /var/log/sa02m_install.log 2>&1


#sudo /bin/systemctl restart nginx fcgiwrap networking.service fix-eth.service >/dev/null 2>&1
sudo /bin/systemctl restart nginx fcgiwrap networking.service fix-eth.service >> /var/log/sa02m_install.log 2>&1

exit 0
