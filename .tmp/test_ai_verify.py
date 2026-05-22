import urllib.request, json, time, http.cookiejar

BASE = 'http://192.168.1.136:9999/api/flasher'
PORT = 'COM4'
DEVICE = {'address': 6, 'baudrate': 115200, 'parity': 'N', 'stopbits': 1}

cj = http.cookiejar.MozillaCookieJar()
cj.load('C:/Users/admin/Downloads/SA-02m-web-build/cookies.txt', ignore_discard=True, ignore_expires=True)
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def api(path, body):
    req = urllib.request.Request(BASE + path,
          json.dumps(body).encode(),
          headers={'Content-Type':'application/json'}, method='POST')
    with opener.open(req, timeout=20) as r:
        return json.loads(r.read())

def write_holding_retry(reg, value, retries=3):
    for attempt in range(retries):
        try:
            return api('/device_config/holding', {'port': PORT, 'device': DEVICE, 'reg': reg, 'value': value})
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            if e.code == 409 and 'Таймаут' in body and attempt < retries - 1:
                print(f"    timeout on attempt {attempt+1}, retrying after 1.5s...")
                time.sleep(1.5)
                continue
            raise

# Читаем снимок
snap = api('/device_config/snapshot', {
    'port': PORT, 'device': DEVICE,
    'snapshot_detail': 'full', 'active_tab': 'ai_1'
})

channels = snap.get('mr', {}).get('ai', {}).get('channels', [])

# Целевые коды
TARGETS = {
    1: (0x0001, 'NTC 10k (B3950)'),
    2: (0x0002, 'Pt1000 2-wire'),
    3: (0x001C, 'Pt100 3-wire'),
    4: (0x001C, 'Pt100 3-wire'),
    5: (0x0006, 'Термопара K TXA'),
    6: (0x0006, 'Термопара K TXA'),
}

print("=== Текущее состояние ===")
need_write = []
for ch in channels:
    n = ch['channel']
    code = ch['sensor_code']
    label = ch['sensor_label']
    meas = ch.get('measured_raw')
    scaled = ch.get('scaled_raw')
    base = ch['register_base']
    want_code, want_name = TARGETS.get(n, (None, None))
    match = '  OK ' if code == want_code else ' NEED'
    print(f"  AI{n}: 0x{code:04X} {label:<30} meas={str(meas):<12} scaled={str(scaled):<10} [{match}]")
    if want_code is not None and code != want_code:
        need_write.append((n, base, want_code, want_name))

if not need_write:
    print("\n=== Все каналы уже на нужных типах ===")
else:
    print(f"\n=== Нужно записать {len(need_write)} канал(а) ===")
    print("Освобождаем порт...")
    try:
        api('/ports/release', {'port': PORT})
    except Exception:
        pass
    time.sleep(0.5)

    for n, base, code, name in need_write:
        print(f"  AI{n}: base={base} → 0x{code:04X} ({name})")
        try:
            write_holding_retry(base, code)
            time.sleep(0.4)
            print(f"    OK")
        except Exception as e:
            print(f"    ERROR: {e}")

    time.sleep(1.0)
    print("Восстанавливаем порт...")
    try:
        api('/ports/restore', {'port': PORT})
    except Exception:
        pass

    # Финальная проверка
    time.sleep(0.5)
    snap2 = api('/device_config/snapshot', {
        'port': PORT, 'device': DEVICE,
        'snapshot_detail': 'full', 'active_tab': 'ai_1'
    })
    channels2 = snap2.get('mr', {}).get('ai', {}).get('channels', [])
    print("\n=== Финальное состояние ===")
    all_ok = True
    for ch in channels2:
        n = ch['channel']
        code = ch['sensor_code']
        want_code, _ = TARGETS.get(n, (None, None))
        status = 'OK' if code == want_code else 'FAIL'
        if code != want_code:
            all_ok = False
        scaled = ch.get('scaled_raw')
        meas = ch.get('measured_raw')
        print(f"  AI{n}: 0x{code:04X} {ch['sensor_label']:<30} meas={str(meas):<12} scaled={str(scaled):<10} [{status}]")
    print("\nRESULT:", "PASS" if all_ok else "FAIL")
