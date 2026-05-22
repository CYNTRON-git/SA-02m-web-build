#!/usr/bin/env python3
import serial, time, re

s = serial.Serial("COM7", 115200, timeout=0.2)
time.sleep(0.3); s.read(s.in_waiting)
s.write(b"\n"); time.sleep(0.5); s.read(s.in_waiting)

def run(cmd, wait=10):
    s.read(s.in_waiting)
    s.write((cmd + "\n").encode())
    buf = b""; t = time.time() + wait
    while time.time() < t:
        c = s.read(s.in_waiting or 1)
        if c: buf += c; t = time.time() + 1.5
        else: time.sleep(0.05)
    return re.sub(r'\x1b\[[0-9;?]*[A-Za-z]|\r', '', buf.decode(errors="replace"))

# ZTE API - каждый параметр отдельно
print("=== ZTE network_type ===")
print(run("curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=network_type'"))

print("=== ZTE rssi ===")
print(run("curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=rssi'"))

print("=== ZTE ppp_status ===")
print(run("curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=ppp_status'"))

print("=== ZTE modem_main_state ===")
print(run("curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=modem_main_state'"))

print("=== ZTE wan_ipaddr ===")
print(run("curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=wan_ipaddr'"))

print("=== ZTE sim_imsi ===")
print(run("curl -s --max-time 5 'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=sim_imsi'"))

# Исправляем маршруты: убираем eth0-onlink, оставляем metric 200
print("=== Fix routing ===")
print(run("ip route del default via 192.168.1.1 dev eth0 2>/dev/null; ip route del default dev eth0 onlink 2>/dev/null; ip route add default via 192.168.1.1 dev eth0 metric 200; ip route"))

print("=== ping 8.8.8.8 (через ZTE) ===")
print(run("ping -c4 -W3 8.8.8.8 2>&1", wait=20))

s.close()
