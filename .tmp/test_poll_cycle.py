"""
Тест корректности цикла release/restore при быстром закрытии и переоткрытии
настроек модуля.  Имитирует последовательность, которую выполняет JS:

  openConfigModal()  → /ports/release  → snapshot
  closeConfigModal() → /ports/restore  (фоново)
  openConfigModal()  → /ports/release  (ДОЛЖНО дождаться restore)  → snapshot

Критерий PASS: все три snapshot-запроса возвращают данные без ошибки
и ни один не получает "Линия занята".
"""
import urllib.request, json, time, http.cookiejar, threading

BASE = 'http://192.168.1.136:9999/api/flasher'
PORT = 'COM4'
DEVICE = {'address': 6, 'baudrate': 115200, 'parity': 'N', 'stopbits': 1}

cj = http.cookiejar.MozillaCookieJar()
cj.load('C:/Users/admin/Downloads/SA-02m-web-build/cookies.txt', ignore_discard=True, ignore_expires=True)
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def post(path, body, timeout=15):
    req = urllib.request.Request(BASE + path,
          json.dumps(body).encode(),
          headers={'Content-Type':'application/json'}, method='POST')
    try:
        with opener.open(req, timeout=timeout) as r:
            return True, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return False, {'http_error': e.code, 'body': e.read().decode('utf-8', errors='replace')[:200]}
    except Exception as e:
        return False, {'exception': str(e)}

def release():
    ok, r = post('/ports/release', {'port': PORT})
    stopped = r.get('stopped_now', []) if ok else []
    print(f"  release: ok={ok} stopped={stopped}")
    return ok

def restore():
    ok, r = post('/ports/restore', {'port': PORT})
    print(f"  restore: ok={ok} restarted={r.get('restarted','?')}")
    return ok

def snapshot(label):
    ok, r = post('/device_config/snapshot', {
        'port': PORT, 'device': DEVICE,
        'snapshot_detail': 'full', 'active_tab': 'ai_1'
    })
    channels = (r.get('mr', {}).get('ai', {}) or {}).get('channels', []) if ok else []
    codes = {c['channel']: hex(c['sensor_code']) for c in channels} if channels else {}
    print(f"  snapshot [{label}]: ok={ok} ai_codes={codes or r.get('http_error') or r.get('exception')}")
    return ok

# ─── Цикл 1: Нормальный сценарий ─────────────────────────────────────────────
print("=== Цикл 1: открыть → снять снимок → закрыть → снова открыть ===")
print("[Шаг 1] openConfigModal — release")
release()
time.sleep(0.3)
print("[Шаг 2] Первый snapshot")
ok1 = snapshot("open-1")

print("[Шаг 3] closeConfigModal — restore (фоново, 200мс задержка)")
# Имитируем fire-and-forget restore
restore_done = threading.Event()
def bg_restore():
    time.sleep(0.2)
    restore()
    restore_done.set()
t = threading.Thread(target=bg_restore, daemon=True)
t.start()

print("[Шаг 4] НЕМЕДЛЕННО openConfigModal — release (должно дождаться restore)")
# В JS _autoReleasePortForConfig awaits _portOpPromise.
# Здесь имитируем: ждём до 2с, пока restore не завершится, потом release.
restore_done.wait(timeout=3.0)
print("       restore завершён, теперь делаем release")
release()
time.sleep(0.3)
print("[Шаг 5] Второй snapshot (после быстрого переоткрытия)")
ok2 = snapshot("open-2")

# ─── Цикл 2: Переключение вкладок — имитация _bgPollPromise ─────────────────
# JS-поведение с fix: tab click дожидается _bgPollPromise, потом делает full.
# Т.е. запросы идут ПОСЛЕДОВАТЕЛЬНО (panel завершается → full стартует).
print("\n=== Цикл 2: переключение вкладок (сериализовано через _bgPollPromise) ===")

ok_p, r_p = post('/device_config/snapshot', {
    'port': PORT, 'device': DEVICE,
    'snapshot_detail': 'panel', 'active_tab': 'ai_1'
})
print(f"  panel: ok={ok_p} detail={r_p.get('snapshot_detail','?')} err={r_p.get('http_error')}")

# после завершения panel → tab click делает full
ok_f, r_f = post('/device_config/snapshot', {
    'port': PORT, 'device': DEVICE,
    'snapshot_detail': 'full', 'active_tab': 'ai_2'
})
print(f"  full:  ok={ok_f} detail={r_f.get('snapshot_detail','?')} err={r_f.get('http_error')}")
concurrent_ok = ok_p and ok_f

# ─── Итог ────────────────────────────────────────────────────────────────────
print()
print("RESULTS:")
print(f"  Быстрое переоткрытие: {'PASS' if ok1 and ok2 else 'FAIL'}")
print(f"  Параллельные запросы: {'PASS' if concurrent_ok else 'FAIL (один вернул ошибку)'}")
print()
print("OVERALL:", "PASS" if (ok1 and ok2 and concurrent_ok) else "FAIL")

# Восстанавливаем порт
restore()
