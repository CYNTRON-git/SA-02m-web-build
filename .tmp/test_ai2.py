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
    try:
        with opener.open(req, timeout=15) as r:
            return True, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return False, {'http_error': e.code, 'body': e.read().decode('utf-8', errors='replace')[:300]}

# Получаем снимок для AI5
ok, snap = api('/device_config/snapshot', {
    'port': PORT, 'device': DEVICE,
    'snapshot_detail': 'full', 'active_tab': 'ai_5'
})
print("Snapshot ok:", ok)
ai_channels = snap.get('mr', {}).get('ai', {}).get('channels', []) if ok else []
for ch in ai_channels:
    print(f"  AI{ch['channel']}: base={ch['register_base']} code=0x{ch['sensor_code']:04X} {ch['sensor_label']}")

# Попробуем написать AI5 (Термопара K = 0x0006)
ai5 = next((c for c in ai_channels if c['channel'] == 5), None)
if ai5:
    base = ai5['register_base']
    print(f"\nWriting AI5 base={base} value=0x0006 ...")
    ok2, res = api('/device_config/holding', {'port': PORT, 'device': DEVICE, 'reg': base, 'value': 0x0006})
    if ok2:
        chs = res.get('mr', {}).get('ai', {}).get('channels', [])
        for ch in chs:
            print(f"  After AI{ch['channel']}: 0x{ch['sensor_code']:04X} {ch['sensor_label']}")
    else:
        print("Write error:", res)
        
        # Пробуем через snapshot как делает frontend (после release)
        print("\nTrying ports/release first...")
        ok_r, res_r = api('/ports/release', {'port': PORT})
        print("release:", ok_r, res_r.get('stopped_now'), res_r.get('already_released'), res_r.get('inactive'))
        time.sleep(0.5)
        
        ok3, res3 = api('/device_config/holding', {'port': PORT, 'device': DEVICE, 'reg': base, 'value': 0x0006})
        print("Write after release:", ok3)
        if ok3:
            chs = res3.get('mr', {}).get('ai', {}).get('channels', [])
            for ch in chs:
                print(f"  AI{ch['channel']}: 0x{ch['sensor_code']:04X} {ch['sensor_label']}")
        else:
            print("Still error:", res3)
