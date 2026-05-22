import urllib.request, json, time, http.cookiejar

BASE = 'http://192.168.1.136:9999/api/flasher'
PORT = 'COM4'
DEVICE = {'address': 6, 'baudrate': 115200, 'parity': 'N', 'stopbits': 1}

cj = http.cookiejar.MozillaCookieJar()
try:
    cj.load('C:/Users/admin/Downloads/SA-02m-web-build/cookies.txt', ignore_discard=True, ignore_expires=True)
except Exception:
    pass
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def api(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data,
          headers={'Content-Type':'application/json'}, method='POST')
    with opener.open(req, timeout=15) as r:
        return json.loads(r.read())

def get_ai_channels():
    snap = api('/device_config/snapshot', {
        'port': PORT, 'device': DEVICE,
        'snapshot_detail': 'full', 'active_tab': 'ai_1'
    })
    channels = snap.get('mr', {}).get('ai', {}).get('channels', [])
    return snap, channels

def release_port():
    try:
        r = api('/ports/release', {'port': PORT})
        print(f"  release: stopped={r.get('stopped_now')}, already={r.get('already_released')}")
    except Exception as e:
        print(f"  release error: {e}")

def restore_port():
    try:
        r = api('/ports/restore', {'port': PORT})
        print(f"  restore: restarted={r.get('restarted')}")
    except Exception as e:
        print(f"  restore error: {e}")

def write_holding(reg, value):
    return api('/device_config/holding', {'port': PORT, 'device': DEVICE, 'reg': reg, 'value': value})

# ─── Читаем текущее состояние ───────────────────────────────────────────────
print("=== Текущие типы датчиков ===")
snap, channels = get_ai_channels()
print(f"Module kind: {snap.get('kind')}")
for ch in channels:
    print(f"  AI{ch['channel']}: 0x{ch['sensor_code']:04X}  {ch['sensor_label']}")

# ─── Таблица нужных кодов ────────────────────────────────────────────────────
# AI1: NTC 10k B3950 = 0x0001
# AI2: Pt1000 2wire (α385) = 0x0002
# AI3: Pt100 3wire (α385) = 0x001C
# AI4: Pt100 3wire (α385) = 0x001C
# AI5: Термопара K (ТХА)  = 0x0006
# AI6: Термопара K (ТХА)  = 0x0006
TARGETS = {1: 0x0001, 2: 0x0002, 3: 0x001C, 4: 0x001C, 5: 0x0006, 6: 0x0006}

print("\n=== Освобождаем порт ===")
release_port()
time.sleep(0.5)

print("\n=== Применяем нужные типы датчиков ===")
for ch in channels:
    ch_num = ch['channel']
    if ch_num not in TARGETS:
        continue
    target_code = TARGETS[ch_num]
    current_code = ch['sensor_code']
    base = ch['register_base']
    label_map = {
        0x0001: 'NTC 10k (B3950)',
        0x0002: 'Pt1000 (α385)',
        0x001C: 'Pt100 (α385), 3-пров.',
        0x0006: 'Термопара K (ТХА)',
    }
    if current_code == target_code:
        print(f"  AI{ch_num}: уже 0x{target_code:04X} — без изменений")
        continue
    print(f"  AI{ch_num}: 0x{current_code:04X} → 0x{target_code:04X}  ({label_map.get(target_code,'?')})")
    write_holding(base, target_code)
    time.sleep(0.3)

# ─── Читаем снимок после записи ─────────────────────────────────────────────
time.sleep(1.0)
print("\n=== Состояние после записи ===")
snap2, channels2 = get_ai_channels()
ok = True
for ch in channels2:
    ch_num = ch['channel']
    if ch_num not in TARGETS:
        continue
    code = ch['sensor_code']
    want = TARGETS[ch_num]
    status = "OK" if code == want else f"FAIL (want 0x{want:04X})"
    meas = ch.get('measured_raw')
    scaled = ch.get('scaled_raw')
    print(f"  AI{ch_num}: 0x{code:04X}  {ch['sensor_label']}  | meas={meas}  scaled={scaled}  [{status}]")
    if code != want:
        ok = False

print()
print("RESULT:", "PASS" if ok else "FAIL — некоторые каналы не приняли тип")

print("\n=== Восстанавливаем порт ===")
restore_port()
