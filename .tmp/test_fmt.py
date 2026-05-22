channels = [
    {'n':1,'sensor':0x0001,'meas':1053827,'scaled':238, 'label':'NTC 10k (B3950)'},
    {'n':2,'sensor':0x0002,'meas':109206, 'scaled':236, 'label':'Pt1000 2-wire'},
    {'n':3,'sensor':0x001C,'meas':10949,  'scaled':243, 'label':'Pt100 3-wire'},
    {'n':4,'sensor':0x001C,'meas':10949,  'scaled':243, 'label':'Pt100 3-wire'},
    {'n':5,'sensor':0x0006,'meas':1197,   'scaled':298, 'label':'Термопара K'},
    {'n':6,'sensor':0x0006,'meas':1197,   'scaled':298, 'label':'Термопара K'},
]
S32_MAX = 2147483647
AI_CURR = {0x0005,0x0015,0x0016}
AI_RTD_3WIRE = {0x001B,0x001C,0x001D,0x001E,0x001F,0x0020,0x0021,0x0022,0x0023,0x0024,0x0025}

def eng_kind(c):
    if not c: return 'off'
    if c == 0x0004: return 'voltage_010'
    if c in AI_CURR: return 'current'
    if c == 0x0007: return 'logic'
    if c == 0x0017: return 'diff_mv'
    if c == 0x0018: return 'diff_v'
    if c in (0x0019, 0x001A): return 'temp'
    if (0x0001 <= c <= 0x0014 or 0x001B <= c <= 0x0025) and c not in {0x0004, 0x0005, 0x0007}: return 'temp'
    return 'unknown'

def raw_kind(c):
    if not c: return 'off'
    if c in (0x0004, 0x0006, 0x0017, 0x0018): return 'microvolt'
    if c in AI_CURR: return 'nanoamp'
    return 'centiohm'

def fmt_scaled(code, v):
    k = eng_kind(code)
    if k == 'off' or v is None or v == S32_MAX: return '—'
    if k == 'temp':        return f'{v/10:.1f} °C'
    if k == 'current':     return f'{v/10:.1f} мА'
    if k == 'voltage_010': return f'{v/100:.2f} В'
    if k == 'diff_mv':     return f'{v/10:.1f} мВ'
    if k == 'diff_v':      return f'{v/100:.2f} В'
    return str(v)

def fmt_meas(code, v):
    rk = raw_kind(code)
    if rk == 'off' or v is None: return '—'
    if rk == 'centiohm':
        if v == S32_MAX or v < 0: return '—'
        ohms = v / 100
        parts = [str(v), f'{ohms:.2f} Ом']
        if ohms >= 1e6: parts.append(f'{ohms/1e6:.3f} МОм')
        elif ohms >= 1000: parts.append(f'{ohms/1000:.2f} кОм')
        return ' → '.join(parts)
    if rk == 'nanoamp':
        ua = v / 1000
        parts = [str(v), f'{ua:.2f} мкА']
        if ua >= 1e6: parts.append(f'{ua/1e6:.3f} А')
        elif ua >= 1000: parts.append(f'{ua/1000:.2f} мА')
        return ' → '.join(parts)
    if rk == 'microvolt':
        mv = v / 1000
        parts = [str(v), f'{mv:.1f} мВ']
        if abs(v) >= 1e6: parts.append(f'{v/1e6:.2f} В')
        return ' → '.join(parts)
    return str(v)

print("Результаты форматирования значений:")
print()
for ch in channels:
    code = ch['sensor']
    sc = fmt_scaled(code, ch['scaled'])
    ms = fmt_meas(code, ch['meas'])
    wire = '3-пров' if code in AI_RTD_3WIRE else ('ТХА/термопара' if code == 0x0006 else 'NTC/2-пров')
    print(f"AI{ch['n']} ({ch['label']}, {wire}):")
    print(f"  Пересчитанное: {sc}")
    print(f"  Измеренное:    {ms}")
    print()
