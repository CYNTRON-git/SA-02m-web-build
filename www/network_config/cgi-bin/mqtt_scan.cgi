#!/bin/bash
# mqtt_scan.cgi — Modbus bus scanner for MQTT device discovery
echo "Content-Type: application/json"
echo "Cache-Control: no-cache"
echo ""

# Pass raw stdin to Python via pipe; Python reads it from sys.stdin
python3 - << 'PYEOF'
import sys, json, os, time

try:
    import serial
except ImportError:
    print(json.dumps({'ok': False, 'error': 'pyserial not installed', 'devices': []}))
    sys.exit(0)

# ── Parse request body (POST JSON) ────────────────────────────────────────
def read_params():
    method = os.environ.get('REQUEST_METHOD', 'GET').upper()
    if method == 'POST':
        try:
            length = int(os.environ.get('CONTENT_LENGTH', 0))
            body = sys.stdin.read(length) if length > 0 else sys.stdin.read()
            return json.loads(body)
        except Exception:
            pass
    qs = os.environ.get('QUERY_STRING', '')
    if qs:
        import urllib.parse as up
        return {k: v for k, v in up.parse_qsl(qs)}
    return {}

params   = read_params()
port     = str(params.get('port',     '/dev/COM1'))
baud     = int(params.get('baudrate', 115200))
max_addr = min(int(params.get('max_addr', 32)), 247)

# ── CRC-16/Modbus ──────────────────────────────────────────────────────────
def crc16(data):
    crc = 0xFFFF
    for b in (data if isinstance(data, (bytes, bytearray)) else bytes(data)):
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc

def make_pdu(addr, func, *payload):
    data = bytes([addr, func]) + bytes(payload)
    c = crc16(data)
    return data + bytes([c & 0xFF, c >> 8])

def valid_crc(pkt):
    if len(pkt) < 4:
        return False
    c = crc16(pkt[:-2])
    return (c & 0xFF) == pkt[-2] and (c >> 8) == pkt[-1]

def read_resp(ser, timeout=0.07):
    ser.timeout = timeout
    buf = b''
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = ser.read(64)
        if chunk:
            buf += chunk
    return buf

# ── Fast Modbus broadcast scan (subcommand 0x04) ───────────────────────────
def fast_scan(ser):
    found = set()
    try:
        pdu = make_pdu(0xFD, 0x46, 0x04)
        ser.reset_input_buffer()
        ser.write(pdu)
        time.sleep(0.35)
        resp = ser.read(256)
        i = 0
        while i < len(resp) - 3:
            if resp[i] == 0xFD and resp[i+1] == 0x46 and i + 8 <= len(resp):
                pkt = resp[i:i+8]
                if valid_crc(pkt) and 1 <= pkt[2] <= 247:
                    found.add(pkt[2])
                i += 8
            else:
                i += 1
    except Exception:
        pass
    return found

# ── Standard Modbus address scan (FC03 reg 0x0000) ─────────────────────────
def std_scan(ser, max_a):
    found = {}
    for addr in range(1, max_a + 1):
        pdu = make_pdu(addr, 0x03, 0x00, 0x00, 0x00, 0x01)
        try:
            ser.reset_input_buffer()
            ser.write(pdu)
            resp = read_resp(ser, timeout=0.06)
            if (len(resp) >= 7 and resp[0] == addr and resp[1] == 0x03
                    and valid_crc(resp[:5+2])):
                found[addr] = (resp[3] << 8) | resp[4]
        except Exception:
            continue
    return found

# ── Device type detection ──────────────────────────────────────────────────
MR02M_MODULE_TYPES = {
    1:'DO6DI8', 2:'DO16', 3:'AO12', 4:'DO6', 5:'DI14',
    6:'AO6AI6', 7:'AI12', 8:'DO4DI6', 9:'TENZO2', 10:'10DIcon',
    11:'6DO5DI2AO', 12:'AI6AO2', 15:'4TO6DI',
}

def read_reg(ser, addr, reg):
    pdu = make_pdu(addr, 0x03, reg >> 8, reg & 0xFF, 0x00, 0x01)
    ser.reset_input_buffer()
    ser.write(pdu)
    resp = read_resp(ser, timeout=0.08)
    if len(resp) >= 7 and resp[0] == addr and resp[1] == 0x03:
        return (resp[3] << 8) | resp[4]
    return None

def detect_type(ser, addr):
    # MR-02m stores module_type (1-15) at reg 0x0100
    mt = read_reg(ser, addr, 0x0100)
    if mt is not None and 1 <= mt <= 15:
        return 'mr02m', mt, MR02M_MODULE_TYPES.get(mt, f'type{mt}')
    # CE-02m-3 sentinel at reg 0x0001
    r1 = read_reg(ser, addr, 0x0001)
    if r1 == 0xCE02:
        return 'ce02m3', 0, 'СЭ-02м-3'
    if r1 == 0xD712:
        return 'dtv', 0, 'ДТВ-RS-485'
    # Fallback: if reg0 is small (0–15) and no module type — likely dtv/unknown
    return 'mr02m', mt or 0, MR02M_MODULE_TYPES.get(mt, 'unknown') if mt else 'unknown'

# ── Main ──────────────────────────────────────────────────────────────────
try:
    if not os.path.exists(port):
        raise FileNotFoundError(f'Порт {port} не найден')

    ser = serial.Serial(port, baud, bytesize=8, parity='N', stopbits=1, timeout=0.1)

    # 1. Fast Modbus broadcast (quick, device-cooperative)
    fast_addrs = fast_scan(ser)

    # 2. Standard scan (exhaustive, works for any Modbus device)
    std_found = std_scan(ser, max_addr)

    # 3. Confirm fast-only addresses
    for addr in fast_addrs:
        if addr not in std_found:
            pdu = make_pdu(addr, 0x03, 0x00, 0x00, 0x00, 0x01)
            ser.reset_input_buffer()
            ser.write(pdu)
            resp = read_resp(ser, timeout=0.08)
            if len(resp) >= 7 and resp[0] == addr and resp[1] == 0x03:
                std_found[addr] = (resp[3] << 8) | resp[4]

    # 4. Detect type and build result
    devices = []
    for addr in sorted(std_found.keys()):
        dev_type, module_type, type_name = detect_type(ser, addr)
        devices.append({
            'addr':        addr,
            'type':        dev_type,
            'module_type': module_type,
            'type_name':   type_name,
            'name':        f'Устройство {addr}',
        })

    ser.close()
    print(json.dumps({'ok': True, 'devices': devices, 'port': port, 'baudrate': baud}))

except FileNotFoundError as e:
    print(json.dumps({'ok': False, 'error': str(e), 'devices': []}))
except serial.SerialException as e:
    print(json.dumps({'ok': False, 'error': f'Ошибка порта: {e}', 'devices': []}))
except PermissionError as e:
    print(json.dumps({'ok': False, 'error': f'Нет доступа к порту (нужен sudo): {e}', 'devices': []}))
except Exception as e:
    print(json.dumps({'ok': False, 'error': str(e), 'devices': []}))
PYEOF
