#!/bin/sh
# Зафиксировать codesyscontrol: без codemeter apt-get -f install снимает пакет CODESYS.
apt-mark hold codesyscontrol
