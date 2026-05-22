"""
Полный тест смены типов датчиков для 6AI6AO (адрес 6):
  AI1: NTC 10k (B3950)         = 0x0001
  AI2: Pt1000 2-wire (α385)    = 0x0002
  AI3: Pt100 3-wire (α385)     = 0x001C
  AI4: Pt100 3-wire (α385)     = 0x001C
  AI5: Термопара K (ТХА)       = 0x0006
  AI6: Термопара K (ТХА)       = 0x0006
"""
import urllib.request, json, time, http.cookiejar

BASE = 'http://192.168.1.136:9999/api/flasher'
PORT = 'COM4'
DEVICE = {'address': 6, 'baudrate': 115200, 'parity': 'N', 'stopbits': 1}

cj = http.cookiejar.MozillaCookieJar()
cj.load('C:/Users/admin/Downloads/SA-02m-web-build/cookies.txt', ignore_discard=True, ignore_expires=True)
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def api_post(path, body):
    req = urllib.request.Request(BASE + path,
          json.dumps(body).encode(),
          headers={'Content-Type':'application/json'}, method='POST')
    try:
        with opener.open(req, timeout=20) as r:
            return True, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_str = e.read().decode('utf-8', errors='replace')[:300]
        return False, {'http_error': e.code, 'body': body_str}

def snapshot():
    ok, s = api_post('/device_config/snapshot', {
        'port': PORT, 'device': DEVICE,
        'snapshot_detail': 'full', 'active_tab': 'ai_1'
    })
    assert ok, f"snapshot failed: {s}"
    return s.get('mr', {}).get('ai', {}).get('channels', [])

def write_sensor_code(base, code):
    ok, r = api_post('/device_config/holding', {
        'port': PORT, 'device': DEVICE, 'reg': base, 'value': code
    })
    return ok, r

TARGETS = {1: 0x0001, 2: 0x0002, 3: 0x001C, 4: 0x001C, 5: 0x0006, 6: 0x0006}
LABELS  = {
    0x0001: 'NTC 10k (B3950)',
    0x0002: 'Pt1000 2-wire (α385)',
    0x001C: 'Pt100 3-wire (α385)',
    0x0006: 'Термопара K (ТХА)',
    0x0000: 'Выключен',
}

# ── Шаг 0: Сброс всех каналов в Выключен ────────────────────────────────────
print("=== Шаг 0: сброс всех в 0x0000 ===")
channels = snapshot()
for ch in channels:
    n, base = ch['channel'], ch['register_base']
    if ch['sensor_code'] != 0:
        ok, r = write_sensor_code(base, 0x0000)
        print(f"  AI{n}: → 0x0000  {'OK' if ok else 'ERR:'+str(r)}")
        time.sleep(0.3)

time.sleep(1)
channels = snapshot()
print("  После сброса:", {c['channel']: hex(c['sensor_code']) for c in channels})

# ── Шаг 1: Применяем нужные типы ────────────────────────────────────────────
print("\n=== Шаг 1: применяем целевые типы ===")
channels = snapshot()
ch_map = {c['channel']: c for c in channels}
fail = False
for n, target in TARGETS.items():
    ch = ch_map.get(n)
    if not ch:
        print(f"  AI{n}: канал не найден!")
        fail = True
        continue
    base = ch['register_base']
    ok, r = write_sensor_code(base, target)
    if ok:
        new_code = r.get('mr', {}).get('ai', {}).get('channels', [{}])
        # Находим нужный канал в ответе
        new_ch = next((c for c in r.get('mr', {}).get('ai', {}).get('channels', []) if c['channel'] == n), None)
        result_code = new_ch['sensor_code'] if new_ch else '?'
        match = result_code == target
        print(f"  AI{n}: 0x{ch['sensor_code']:04X} → 0x{target:04X}  response=0x{result_code:04X}  {'OK' if match else 'MISMATCH'}")
        if not match:
            fail = True
    else:
        print(f"  AI{n}: WRITE ERROR: {r}")
        fail = True
    time.sleep(0.3)

# ── Шаг 2: Верификация ───────────────────────────────────────────────────────
time.sleep(1)
print("\n=== Шаг 2: финальная верификация ===")
channels = snapshot()
all_ok = True

S32_MAX = 2147483647
AI_CURR = {0x0005, 0x0015, 0x0016}

def eng_kind(c):
    if not c: return 'off'
    if c in AI_CURR: return 'current'
    if c == 0x0004: return 'voltage_010'
    if c == 0x0007: return 'logic'
    if c in (0x0017,): return 'diff_mv'
    if c in (0x0018,): return 'diff_v'
    if (0x0001<=c<=0x0014 or 0x001B<=c<=0x0025) and c not in {0x0004,0x0005,0x0007}: return 'temp'
    return 'unknown'

def raw_kind(c):
    if not c: return 'off'
    if c in (0x0004, 0x0006, 0x0017, 0x0018): return 'microvolt'
    if c in AI_CURR: return 'nanoamp'
    return 'centiohm'

def fmt_scaled(code, v):
    k = eng_kind(code)
    if k=='off' or v is None or v==S32_MAX: return '—'
    if k=='temp': return f'{v/10:.1f} °C'
    if k=='current': return f'{v/10:.1f} мА'
    if k=='voltage_010': return f'{v/100:.2f} В'
    if k in ('diff_mv',): return f'{v/10:.1f} мВ'
    if k in ('diff_v',): return f'{v/100:.2f} В'
    return str(v)

def fmt_meas(code, v):
    rk = raw_kind(code)
    if rk=='off' or v is None: return '—'
    if rk=='centiohm':
        if v==S32_MAX or v<0: return '—'
        ohms = v/100
        parts = [str(v), f'{ohms:.2f} Ом']
        if ohms>=1e6: parts.append(f'{ohms/1e6:.3f} МОм')
        elif ohms>=1000: parts.append(f'{ohms/1000:.2f} кОм')
        return ' → '.join(parts)
    if rk=='nanoamp':
        ua = v/1000
        parts = [str(v), f'{ua:.2f} мкА']
        if ua>=1000: parts.append(f'{ua/1000:.2f} мА')
        return ' → '.join(parts)
    if rk=='microvolt':
        mv = v/1000
        parts = [str(v), f'{mv:.1f} мВ']
        if abs(v)>=1e6: parts.append(f'{v/1e6:.2f} В')
        return ' → '.join(parts)
    return str(v)

for ch in channels:
    n = ch['channel']
    code = ch['sensor_code']
    want = TARGETS.get(n)
    if want is None:
        continue
    match = code == want
    if not match:
        all_ok = False
    sc = fmt_scaled(code, ch.get('scaled_raw'))
    ms = fmt_meas(code, ch.get('measured_raw'))
    status = ' OK ' if match else 'FAIL'
    print(f"  AI{n}: 0x{code:04X} {ch['sensor_label']:<30} пересчитанное={sc:<14} измеренное={ms}  [{status}]")

print()
print("RESULT:", "PASS" if all_ok else "FAIL")
