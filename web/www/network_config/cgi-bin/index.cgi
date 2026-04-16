#!/bin/bash
echo "Content-type: text/html; charset=UTF-8"
echo ""

check_auth() {
    [[ -n "$HTTP_COOKIE" && "$HTTP_COOKIE" =~ "session_token=cyntron_session" ]] && return 0
    return 1
}

# ─── Login page ──────────────────────────────────────────────────────────────
if ! check_auth; then
    cat <<'HTML'
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>СА-02м — Вход</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;min-height:100vh;display:flex;align-items:center;justify-content:center}
.login-wrap{display:flex;flex-direction:column;align-items:center;gap:28px}
.logo-area img{height:56px}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:36px 40px;width:340px}
.card h2{color:#e6edf3;font-size:1.1rem;font-weight:600;margin-bottom:24px;text-align:center}
label{display:block;color:#8b949e;font-size:.82rem;margin-bottom:6px;margin-top:16px}
label:first-of-type{margin-top:0}
input{width:100%;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;font-size:.95rem;padding:10px 14px;transition:border-color .2s}
input:focus{outline:none;border-color:#388bfd}
.err{color:#f85149;font-size:.82rem;margin-top:12px;text-align:center;min-height:18px}
button{margin-top:20px;width:100%;background:#1f6feb;border:none;border-radius:8px;color:#fff;cursor:pointer;font-size:.95rem;font-weight:600;padding:11px;transition:background .2s}
button:hover{background:#388bfd}
</style>
</head>
<body>
<div class="login-wrap">
  <div class="logo-area"><img src="/static/logo.svg" alt="ЦИНТРОН"></div>
  <div class="card">
    <h2>Панель управления СА-02м</h2>
    <form method="POST" action="/cgi-bin/login.cgi">
      <label>Логин</label>
      <input type="text" name="login" autocomplete="username" required autofocus>
      <label>Пароль</label>
      <input type="password" name="password" autocomplete="current-password" required>
      <div class="err" id="err"></div>
      <button type="submit">Войти</button>
    </form>
  </div>
</div>
<script>
const p=new URLSearchParams(location.search);
if(p.get('error')) document.getElementById('err').textContent='Неверный логин или пароль';
</script>
</body>
</html>
HTML
    exit 0
fi

# ─── Read current network config ─────────────────────────────────────────────
CURRENT_IP="—"
CURRENT_MASK="—"
CURRENT_GATE="—"
CURRENT_DNS="—"
if [ -f /etc/network/interfaces.d/eth0.conf ]; then
    CURRENT_IP=$(awk '/^[[:space:]]*address /{split($2,a,"/");print a[1];exit}' /etc/network/interfaces.d/eth0.conf)
    CURRENT_MASK=$(awk '/^[[:space:]]*netmask /{print $2;exit}' /etc/network/interfaces.d/eth0.conf)
    CURRENT_GATE=$(awk '/^[[:space:]]*gateway /{print $2;exit}' /etc/network/interfaces.d/eth0.conf)
    CURRENT_DNS=$(awk '/^[[:space:]]*dns-nameservers /{print $2;exit}' /etc/network/interfaces.d/eth0.conf)
fi

CURRENT_IP1=""
CURRENT_MASK1=""
CURRENT_GATE1=""
CURRENT_DNS1=""
ETH1_CHECKED=""
if [ -f /etc/network/interfaces.d/eth1.conf ]; then
    ETH1_CHECKED="checked"
    CURRENT_IP1=$(awk '/^[[:space:]]*address /{split($2,a,"/");print a[1];exit}' /etc/network/interfaces.d/eth1.conf)
    CURRENT_MASK1=$(awk '/^[[:space:]]*netmask /{print $2;exit}' /etc/network/interfaces.d/eth1.conf)
    CURRENT_GATE1=$(awk '/^[[:space:]]*gateway /{print $2;exit}' /etc/network/interfaces.d/eth1.conf)
    CURRENT_DNS1=$(awk '/^[[:space:]]*dns-nameservers /{print $2;exit}' /etc/network/interfaces.d/eth1.conf)
fi

CURRENT_TZ=$(cat /etc/timezone 2>/dev/null || echo "Europe/Moscow")
CURRENT_TIME=$(TZ="$CURRENT_TZ" date "+%d.%m.%Y %H:%M:%S")

STATUS_MSG=""
case "$QUERY_STRING" in
    *status=reboot*)        STATUS_MSG='<div class="toast toast-ok">Система перезагружается…</div>' ;;
    *status=services*)      STATUS_MSG='<div class="toast toast-ok">Службы успешно перезапущены</div>' ;;
    *status=applied*)       STATUS_MSG='<div class="toast toast-ok">Настройки сохранены</div>' ;;
    *status=time_updated*)  STATUS_MSG='<div class="toast toast-ok">Время обновлено</div>' ;;
    *status=error*)         STATUS_MSG='<div class="toast toast-err">Ошибка выполнения операции</div>' ;;
    *status=error_time*)    STATUS_MSG='<div class="toast toast-err">Некорректное значение времени</div>' ;;
    *status=error_tz*)      STATUS_MSG='<div class="toast toast-err">Ошибка выбора часового пояса</div>' ;;
esac

cat <<HTML
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>СА-02м — Управление</title>
<style>
/* ── Reset & base ── */
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh;display:flex;flex-direction:column}

/* ── Top bar ── */
.topbar{background:#161b22;border-bottom:1px solid #30363d;height:56px;display:flex;align-items:center;padding:0 24px;gap:16px;position:sticky;top:0;z-index:100}
.topbar img{height:34px}
.topbar-title{font-size:1rem;font-weight:600;color:#e6edf3}
.topbar-ip{font-size:.8rem;color:#58a6ff;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:3px 10px;margin-left:auto}
.topbar-clock{font-size:.82rem;color:#8b949e;margin-left:12px}
.topbar-logout{background:transparent;border:1px solid #f85149;color:#f85149;border-radius:8px;padding:6px 14px;cursor:pointer;font-size:.82rem;transition:background .2s;margin-left:12px}
.topbar-logout:hover{background:#f851491a}

/* ── Layout ── */
.layout{display:flex;flex:1;min-height:0}
.sidebar{background:#161b22;border-right:1px solid #30363d;width:200px;flex-shrink:0;padding:16px 0}
.nav-item{display:flex;align-items:center;gap:12px;padding:10px 20px;cursor:pointer;color:#8b949e;font-size:.88rem;transition:all .15s;border-left:3px solid transparent}
.nav-item svg{width:17px;height:17px;flex-shrink:0}
.nav-item:hover{background:#1f2937;color:#e6edf3}
.nav-item.active{color:#58a6ff;background:#1f293740;border-left-color:#58a6ff}
.main{flex:1;overflow-y:auto;padding:28px}

/* ── Tabs ── */
.tab{display:none}.tab.active{display:block}

/* ── Dashboard grid ── */
.dash-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px;margin-bottom:24px}
.widget{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px}
.widget-label{font-size:.75rem;color:#8b949e;text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px}
.widget-val{font-size:1.6rem;font-weight:700;color:#e6edf3;line-height:1}
.widget-sub{font-size:.78rem;color:#8b949e;margin-top:6px}

/* Gauge (arc) */
.gauge-wrap{position:relative;display:flex;flex-direction:column;align-items:center;gap:6px}
.gauge-svg{width:100px;height:60px;overflow:visible}
.gauge-arc-bg{fill:none;stroke:#21262d;stroke-width:9;stroke-linecap:round}
.gauge-arc{fill:none;stroke-width:9;stroke-linecap:round;transition:stroke-dasharray .6s ease}
.gauge-num{font-size:1.25rem;font-weight:700;fill:#e6edf3;text-anchor:middle;dominant-baseline:middle}
.gauge-unit{font-size:.65rem;fill:#8b949e;text-anchor:middle}

/* Progress bar */
.bar-wrap{margin-top:10px}
.bar-track{background:#21262d;border-radius:4px;height:7px;overflow:hidden}
.bar-fill{height:100%;border-radius:4px;transition:width .6s ease}
.bar-label{display:flex;justify-content:space-between;font-size:.75rem;color:#8b949e;margin-top:5px}

/* Service badges */
.svc-list{display:flex;flex-direction:column;gap:8px;margin-top:4px}
.svc-row{display:flex;align-items:center;justify-content:space-between;font-size:.82rem}
.badge{padding:2px 9px;border-radius:10px;font-size:.72rem;font-weight:600}
.badge-ok{background:#1a3a1a;color:#3fb950}
.badge-err{background:#3a1a1a;color:#f85149}
.badge-unk{background:#2a2a2a;color:#8b949e}

/* Network stat */
.net-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:4px}
.net-item{text-align:center}
.net-dir{font-size:.7rem;color:#8b949e;text-transform:uppercase;letter-spacing:.05em}
.net-bytes{font-size:.95rem;font-weight:600;color:#e6edf3;margin-top:2px}

/* ── Section headers ── */
.section-title{font-size:1rem;font-weight:600;color:#e6edf3;margin-bottom:20px;padding-bottom:12px;border-bottom:1px solid #30363d}

/* ── Forms ── */
.form-card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;max-width:560px}
.field{margin-bottom:18px}
.field label{display:block;font-size:.82rem;color:#8b949e;margin-bottom:6px}
.field input,.field select{width:100%;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;font-size:.92rem;padding:9px 13px;transition:border-color .2s}
.field input:focus,.field select:focus{outline:none;border-color:#388bfd}
.field input.invalid{border-color:#f85149}
.field-hint{font-size:.75rem;color:#8b949e;margin-top:4px}

/* ── Buttons ── */
.btn{display:inline-flex;align-items:center;gap:7px;border:none;border-radius:8px;cursor:pointer;font-size:.88rem;font-weight:600;padding:10px 20px;transition:background .2s}
.btn-primary{background:#1f6feb;color:#fff}.btn-primary:hover{background:#388bfd}
.btn-secondary{background:#21262d;color:#c9d1d9;border:1px solid #30363d}.btn-secondary:hover{background:#30363d}
.btn-warning{background:#9a4b00;color:#fff}.btn-warning:hover{background:#b85c00}
.btn-danger{background:#6e1b1b;color:#fff}.btn-danger:hover{background:#f85149}
.btn-group{display:flex;gap:12px;flex-wrap:wrap;margin-top:20px}

/* ── System controls ── */
.ctrl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px;margin-bottom:28px}
.ctrl-card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px;display:flex;flex-direction:column;gap:10px}
.ctrl-card h3{font-size:.9rem;color:#e6edf3;font-weight:600}
.ctrl-card p{font-size:.78rem;color:#8b949e;line-height:1.5}

/* ── Log ── */
.log-box{background:#0d1117;border:1px solid #30363d;border-radius:10px;padding:16px;max-height:320px;overflow-y:auto;font-family:'Courier New',monospace;font-size:.78rem;color:#8b949e;line-height:1.6;white-space:pre-wrap;word-break:break-all}
.log-box .log-ok{color:#3fb950}
.log-box .log-err{color:#f85149}

/* ── Toast ── */
.toast{position:fixed;top:70px;right:24px;border-radius:10px;padding:12px 20px;font-size:.88rem;font-weight:500;z-index:200;animation:fadeIn .3s ease}
.toast-ok{background:#1a3a1a;border:1px solid #2ea043;color:#3fb950}
.toast-err{background:#3a1a1a;border:1px solid #f85149;color:#f85149}
@keyframes fadeIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}

/* ── Uptime ── */
.uptime-big{font-size:1.4rem;font-weight:700;color:#e6edf3;margin-top:4px}
.uptime-sub{font-size:.78rem;color:#8b949e;margin-top:4px}

/* ── HW outputs ── */
.hw-card{grid-column:1/-1;max-width:100%}
.hw-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px;margin-top:8px}
.hw-item{background:#0d1117;border:1px solid #30363d;border-radius:10px;padding:14px 16px}
.hw-item-title{font-size:.78rem;color:#8b949e;margin-bottom:8px}
.hw-state{font-size:1.05rem;font-weight:700;margin-bottom:10px}
.hw-state.on{color:#3fb950}.hw-state.off{color:#6e7681}
.hw-state.na{color:#484f58;font-size:.85rem}
.hw-btns{display:flex;gap:8px;flex-wrap:wrap}
.hw-btns button{flex:1;min-width:72px;padding:8px 10px;border-radius:8px;border:1px solid #30363d;background:#21262d;color:#e6edf3;cursor:pointer;font-size:.8rem;font-weight:600}
.hw-btns button:hover{background:#30363d}
.hw-btns button.on-act{border-color:#238636;background:#23863633;color:#3fb950}
.hw-btns button.off-act{border-color:#f8514966;color:#f85149}
.eth1-line{font-size:.75rem;color:#8b949e;margin-top:6px}

/* ── Load averages ── */
.load-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:10px}
.load-item{text-align:center;background:#0d1117;border-radius:8px;padding:8px 4px}
.load-num{font-size:1rem;font-weight:700;color:#e6edf3}
.load-lbl{font-size:.67rem;color:#8b949e;margin-top:2px}

/* ── RS-485 ── */
.rs485-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-top:10px}
@media(max-width:1100px){.rs485-grid{grid-template-columns:repeat(3,1fr)}}
@media(max-width:700px){.rs485-grid{grid-template-columns:repeat(2,1fr)}}
.rs485-port{background:#0d1117;border:1px solid #30363d;border-radius:10px;padding:13px 12px;transition:border-color .4s,box-shadow .4s}
.rs485-port.absent{opacity:.38}
.rs485-port.act{border-color:#388bfd;box-shadow:0 0 10px #388bfd44}
.rs485-hdr{display:flex;align-items:center;gap:7px;margin-bottom:5px}
.rs485-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0;transition:background .4s}
.rs485-dot.on{background:#3fb950;box-shadow:0 0 5px #3fb950}
.rs485-dot.idle{background:#484f58}
.rs485-dot.absent{background:#f85149}
.rs485-name{font-size:.84rem;font-weight:700;color:#e6edf3}
.rs485-dev{font-size:.68rem;color:#484f58;margin-bottom:7px;font-family:'Courier New',monospace}
.rs485-row{display:flex;justify-content:space-between;align-items:baseline;font-size:.76rem;margin-top:3px}
.rs485-row .rl{color:#8b949e}
.rs485-row .rv{font-weight:600;color:#c9d1d9;font-family:'Courier New',monospace}
.rs485-row .rv.act{color:#58a6ff}
.rs485-err{font-size:.7rem;color:#f85149;margin-top:5px}
.rs485-open{font-size:.7rem;color:#3fb950;margin-top:3px}
.rs485-closed{font-size:.7rem;color:#484f58;margin-top:3px}

/* ── Swap mini-bar ── */
.swap-bar{margin-top:8px;padding-top:8px;border-top:1px solid #21262d}

/* ── Responsive ── */
@media(max-width:640px){
  .sidebar{display:none}
  .dash-grid{grid-template-columns:1fr 1fr}
  .main{padding:16px}
}
</style>
</head>
<body>

${STATUS_MSG}

<!-- Top bar -->
<div class="topbar">
  <img src="/static/logo.svg" alt="ЦИНТРОН">
  <span class="topbar-title">СА-02м</span>
  <span class="topbar-ip" id="tb-ip">${CURRENT_IP}</span>
  <span class="topbar-clock" id="tb-clock">${CURRENT_TIME}</span>
  <form method="POST" action="/cgi-bin/logout.cgi" style="margin:0">
    <button type="submit" class="topbar-logout">Выйти</button>
  </form>
</div>

<!-- Layout -->
<div class="layout">

  <!-- Sidebar -->
  <nav class="sidebar">
    <div class="nav-item active" data-tab="dashboard">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
      Dashboard
    </div>
    <div class="nav-item" data-tab="network">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M2 12h3M19 12h3M12 2v3M12 19v3"/><path d="M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/></svg>
      Сеть
    </div>
    <div class="nav-item" data-tab="time">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
      Время
    </div>
    <div class="nav-item" data-tab="system">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
      Система
    </div>
  </nav>

  <!-- Main content -->
  <main class="main">

    <!-- ═══ DASHBOARD ═══ -->
    <div class="tab active" id="tab-dashboard">
      <div class="section-title">Состояние системы</div>
      <div class="dash-grid">

        <!-- CPU -->
        <div class="widget">
          <div class="widget-label">CPU нагрузка</div>
          <div class="gauge-wrap">
            <svg class="gauge-svg" viewBox="0 0 100 54">
              <path class="gauge-arc-bg" d="M10,50 A40,40 0 0,1 90,50"/>
              <path class="gauge-arc" id="cpu-arc" d="M10,50 A40,40 0 0,1 90,50" stroke="#388bfd" stroke-dasharray="0 126"/>
              <text class="gauge-num" x="50" y="46" id="cpu-val">—</text>
              <text class="gauge-unit" x="50" y="56">%</text>
            </svg>
          </div>
        </div>

        <!-- RAM -->
        <div class="widget">
          <div class="widget-label">Оперативная память</div>
          <div class="widget-val" id="ram-val">—</div>
          <div class="widget-sub" id="ram-sub">из —</div>
          <div class="bar-wrap">
            <div class="bar-track"><div class="bar-fill" id="ram-bar" style="width:0%;background:#388bfd"></div></div>
            <div class="bar-label"><span id="ram-pct">0%</span><span id="ram-free-lbl">свободно —</span></div>
          </div>
          <div class="swap-bar" id="swap-block" style="display:none">
            <div class="widget-label" style="margin-bottom:6px">SWAP</div>
            <div class="bar-track"><div class="bar-fill" id="swap-bar" style="width:0%;background:#9a4b00"></div></div>
            <div class="bar-label"><span id="swap-pct">0%</span><span id="swap-lbl">0 / 0</span></div>
          </div>
        </div>

        <!-- Temperature -->
        <div class="widget">
          <div class="widget-label">Температура CPU</div>
          <div class="gauge-wrap">
            <svg class="gauge-svg" viewBox="0 0 100 54">
              <path class="gauge-arc-bg" d="M10,50 A40,40 0 0,1 90,50"/>
              <path class="gauge-arc" id="temp-arc" d="M10,50 A40,40 0 0,1 90,50" stroke="#f78166" stroke-dasharray="0 126"/>
              <text class="gauge-num" x="50" y="46" id="temp-val">—</text>
              <text class="gauge-unit" x="50" y="56">°C</text>
            </svg>
          </div>
        </div>

        <!-- Disk -->
        <div class="widget">
          <div class="widget-label">Диск (/)</div>
          <div class="widget-val" id="disk-val">—</div>
          <div class="widget-sub" id="disk-sub">из —</div>
          <div class="bar-wrap">
            <div class="bar-track"><div class="bar-fill" id="disk-bar" style="width:0%;background:#3fb950"></div></div>
            <div class="bar-label"><span id="disk-pct">0%</span><span id="disk-free-lbl">свободно —</span></div>
          </div>
        </div>

        <!-- Uptime -->
        <div class="widget">
          <div class="widget-label">Uptime</div>
          <div class="uptime-big" id="uptime-val">—</div>
          <div class="uptime-sub" id="uptime-sub">с момента запуска</div>
        </div>

        <!-- Network traffic -->
        <div class="widget">
          <div class="widget-label">Сетевой трафик (eth0)</div>
          <div class="net-grid">
            <div class="net-item">
              <div class="net-dir">↓ RX</div>
              <div class="net-bytes" id="net-rx">—</div>
            </div>
            <div class="net-item">
              <div class="net-dir">↑ TX</div>
              <div class="net-bytes" id="net-tx">—</div>
            </div>
          </div>
        </div>

        <div class="widget">
          <div class="widget-label">Ethernet 1 (eth1)</div>
          <div class="widget-val" style="font-size:1rem" id="eth1-state">—</div>
          <div class="eth1-line" id="eth1-traf">RX — · TX —</div>
        </div>

        <!-- HW: DO, beeper, alarm LED -->
        <div class="widget hw-card">
          <div class="widget-label">Дискретный выход и индикация</div>
          <div class="field-hint" style="margin-bottom:10px" id="hw-hint">Загрузка…</div>
          <div class="hw-grid">
            <div class="hw-item">
              <div class="hw-item-title">DO (дискретный выход)</div>
              <div class="hw-state na" id="hw-do-st">—</div>
              <div class="hw-btns">
                <button type="button" class="off-act" onclick="setHw('do',0)">Выкл</button>
                <button type="button" class="on-act" onclick="setHw('do',1)">Вкл</button>
              </div>
            </div>
            <div class="hw-item">
              <div class="hw-item-title">Пищалка</div>
              <div class="hw-state na" id="hw-beep-st">—</div>
              <div class="hw-btns">
                <button type="button" class="off-act" onclick="setHw('beeper',0)">Тихо</button>
                <button type="button" class="on-act" onclick="setHw('beeper',1)">Звук</button>
              </div>
            </div>
            <div class="hw-item">
              <div class="hw-item-title">Аварийный LED (красный)</div>
              <div class="hw-state na" id="hw-led-st">—</div>
              <div class="hw-btns">
                <button type="button" class="off-act" onclick="setHw('alarm_led',0)">Выкл</button>
                <button type="button" class="on-act" onclick="setHw('alarm_led',1)">Вкл</button>
              </div>
            </div>
          </div>
        </div>

        <!-- Services -->
        <div class="widget">
          <div class="widget-label">Службы</div>
          <div class="svc-list">
            <div class="svc-row"><span>nginx</span><span class="badge badge-unk" id="svc-nginx">…</span></div>
            <div class="svc-row"><span>fcgiwrap</span><span class="badge badge-unk" id="svc-fcgi">…</span></div>
            <div class="svc-row" style="margin-top:4px"><span>mplc</span><span class="badge badge-unk" id="svc-mplc">…</span></div>
          </div>
          <div class="widget-sub" id="mplc-uptime" style="margin-top:8px;font-size:.72rem"></div>
        </div>

        <!-- Load averages -->
        <div class="widget">
          <div class="widget-label">Нагрузка (load avg)</div>
          <div class="load-grid">
            <div class="load-item"><div class="load-num" id="load-1">—</div><div class="load-lbl">1 мин</div></div>
            <div class="load-item"><div class="load-num" id="load-5">—</div><div class="load-lbl">5 мин</div></div>
            <div class="load-item"><div class="load-num" id="load-15">—</div><div class="load-lbl">15 мин</div></div>
          </div>
          <div class="widget-sub" id="proc-info"></div>
          <div class="widget-sub" id="cpu-freq-info" style="margin-top:4px"></div>
        </div>

        <!-- Board/system info -->
        <div class="widget">
          <div class="widget-label">Система</div>
          <div class="widget-sub" id="board-info" style="font-size:.78rem;color:#e6edf3;font-weight:600">—</div>
          <div class="widget-sub" id="cpu-model-info" style="margin-top:4px;font-size:.72rem">—</div>
          <div class="widget-sub" id="kernel-info" style="margin-top:4px;font-size:.72rem"></div>
          <div class="widget-sub" id="disk-io-info" style="margin-top:6px;font-size:.72rem"></div>
        </div>

        <!-- RS-485 interfaces — full width -->
        <div class="widget hw-card" id="rs485-widget">
          <div class="widget-label">Интерфейсы RS-485 — состояние и активность</div>
          <div class="field-hint" style="margin-bottom:6px">TX/RX — накопленные байты с момента загрузки ОС. Подсветка порта = изменение счётчика за последний опрос.</div>
          <div class="rs485-grid" id="rs485-grid"></div>
        </div>

      </div><!-- /dash-grid -->
    </div><!-- /tab-dashboard -->

    <!-- ═══ NETWORK ═══ -->
    <div class="tab" id="tab-network">
      <div class="section-title">Ethernet 0 (eth0)</div>
      <div class="form-card" style="margin-bottom:22px">
        <form method="POST" action="/cgi-bin/apply.cgi" id="net-form">
          <input type="hidden" name="net_iface" value="eth0">
          <div class="field">
            <label>IP-адрес</label>
            <input type="text" name="ip" id="f-ip" value="${CURRENT_IP}" placeholder="192.168.1.100" required pattern="^(\d{1,3}\.){3}\d{1,3}$">
            <div class="field-hint">Формат: 192.168.1.100</div>
          </div>
          <div class="field">
            <label>Маска подсети</label>
            <input type="text" name="netmask" id="f-mask" value="${CURRENT_MASK}" placeholder="255.255.255.0" required pattern="^(\d{1,3}\.){3}\d{1,3}$">
          </div>
          <div class="field">
            <label>Шлюз</label>
            <input type="text" name="gateway" id="f-gate" value="${CURRENT_GATE}" placeholder="192.168.1.1" pattern="^(\d{1,3}\.){3}\d{1,3}$">
          </div>
          <div class="field">
            <label>DNS-серверы</label>
            <input type="text" name="dns" id="f-dns" value="${CURRENT_DNS}" placeholder="8.8.8.8">
            <div class="field-hint">При смене IP-адреса соединение прервётся</div>
          </div>
          <div class="btn-group">
            <button type="submit" class="btn btn-primary">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
              Применить eth0
            </button>
          </div>
        </form>
      </div>

      <div class="section-title">Ethernet 1 (eth1)</div>
      <div class="form-card">
        <form method="POST" action="/cgi-bin/apply.cgi" id="net-form-eth1">
          <input type="hidden" name="net_iface" value="eth1">
          <div class="field">
            <label style="display:flex;align-items:center;gap:10px;cursor:pointer">
              <input type="checkbox" name="eth1_enable" value="1" id="eth1-en" ${ETH1_CHECKED} style="width:auto">
              <span>Включить статический адрес на eth1</span>
            </label>
            <div class="field-hint">Снимите флажок и сохраните, чтобы удалить конфигурацию eth1</div>
          </div>
          <div class="field">
            <label>IP-адрес (eth1)</label>
            <input type="text" name="ip_eth1" id="f-ip1" value="${CURRENT_IP1}" placeholder="192.168.2.10" pattern="^(\d{1,3}\.){3}\d{1,3}$">
          </div>
          <div class="field">
            <label>Маска подсети</label>
            <input type="text" name="netmask_eth1" id="f-mask1" value="${CURRENT_MASK1}" placeholder="255.255.255.0" pattern="^(\d{1,3}\.){3}\d{1,3}$">
          </div>
          <div class="field">
            <label>Шлюз (необязательно)</label>
            <input type="text" name="gateway_eth1" id="f-gate1" value="${CURRENT_GATE1}" placeholder="192.168.2.1">
          </div>
          <div class="field">
            <label>DNS (необязательно)</label>
            <input type="text" name="dns_eth1" id="f-dns1" value="${CURRENT_DNS1}" placeholder="8.8.8.8">
          </div>
          <div class="btn-group">
            <button type="submit" class="btn btn-primary">Применить eth1</button>
          </div>
        </form>
      </div>
    </div><!-- /tab-network -->

    <!-- ═══ TIME ═══ -->
    <div class="tab" id="tab-time">
      <div class="section-title">Дата и время</div>
      <div class="form-card">
        <form method="POST" action="/cgi-bin/apply.cgi">
          <input type="hidden" name="skip_network" value="1">
          <input type="hidden" name="ip" value="${CURRENT_IP}">
          <input type="hidden" name="netmask" value="${CURRENT_MASK}">
          <input type="hidden" name="gateway" value="${CURRENT_GATE}">
          <input type="hidden" name="dns" value="${CURRENT_DNS}">
          <div class="field">
            <label>Дата и время</label>
            <input type="datetime-local" name="datetime" value="$(TZ=${CURRENT_TZ} date +"%Y-%m-%dT%H:%M")" required>
          </div>
          <div class="field">
            <label>Часовой пояс</label>
            <select name="timezone">
              <option value="Europe/Kaliningrad">Калининград (UTC+2)</option>
              <option value="Europe/Moscow"     $([ "$CURRENT_TZ" = "Europe/Moscow"      ] && echo selected)>Москва, Санкт-Петербург (UTC+3)</option>
              <option value="Europe/Samara"     $([ "$CURRENT_TZ" = "Europe/Samara"      ] && echo selected)>Самара, Ижевск (UTC+4)</option>
              <option value="Asia/Yekaterinburg"$([ "$CURRENT_TZ" = "Asia/Yekaterinburg" ] && echo selected)>Екатеринбург (UTC+5)</option>
              <option value="Asia/Omsk"         $([ "$CURRENT_TZ" = "Asia/Omsk"          ] && echo selected)>Омск (UTC+6)</option>
              <option value="Asia/Novosibirsk"  $([ "$CURRENT_TZ" = "Asia/Novosibirsk"   ] && echo selected)>Новосибирск, Барнаул (UTC+7)</option>
              <option value="Asia/Irkutsk"      $([ "$CURRENT_TZ" = "Asia/Irkutsk"       ] && echo selected)>Иркутск (UTC+8)</option>
              <option value="Asia/Chita"        $([ "$CURRENT_TZ" = "Asia/Chita"         ] && echo selected)>Чита (UTC+9)</option>
              <option value="Asia/Vladivostok"  $([ "$CURRENT_TZ" = "Asia/Vladivostok"   ] && echo selected)>Владивосток (UTC+10)</option>
              <option value="Asia/Magadan"      $([ "$CURRENT_TZ" = "Asia/Magadan"       ] && echo selected)>Магадан (UTC+11)</option>
              <option value="Asia/Kamchatka"    $([ "$CURRENT_TZ" = "Asia/Kamchatka"     ] && echo selected)>Камчатка (UTC+12)</option>
            </select>
          </div>
          <div class="btn-group">
            <button type="submit" class="btn btn-primary">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
              Применить
            </button>
          </div>
        </form>
      </div>
    </div><!-- /tab-time -->

    <!-- ═══ SYSTEM ═══ -->
    <div class="tab" id="tab-system">
      <div class="section-title">Управление системой</div>
      <div class="ctrl-grid">
        <div class="ctrl-card">
          <h3>Перезапуск служб</h3>
          <p>Перезапускает nginx, fcgiwrap, networking и fix-eth без перезагрузки устройства.</p>
          <form method="POST" action="/cgi-bin/restart_services.cgi">
            <button type="submit" class="btn btn-secondary">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4.5"/></svg>
              Перезапустить
            </button>
          </form>
        </div>
        <div class="ctrl-card">
          <h3>Перезагрузка устройства</h3>
          <p>Полная перезагрузка СА-02м. Соединение прервётся на несколько минут.</p>
          <form method="POST" action="/cgi-bin/reboot.cgi" onsubmit="return confirm('Перезагрузить устройство?')">
            <button type="submit" class="btn btn-danger">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18.36 6.64A9 9 0 0 1 20.77 15"/><path d="M6.16 6.16a9 9 0 1 0 12.68 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>
              Перезагрузить
            </button>
          </form>
        </div>
      </div>

      <div class="section-title" style="margin-top:8px">Журнал событий</div>
      <div style="display:flex;justify-content:flex-end;margin-bottom:8px">
        <button class="btn btn-secondary" onclick="loadLog()" style="padding:6px 14px;font-size:.8rem">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4.5"/></svg>
          Обновить
        </button>
      </div>
      <div class="log-box" id="log-box">Загрузка…</div>
    </div><!-- /tab-system -->

  </main>
</div><!-- /layout -->

<script>
// ── Navigation ──────────────────────────────────────────────────────────────
const navItems = document.querySelectorAll('.nav-item');
const tabs     = document.querySelectorAll('.tab');

navItems.forEach(item => {
  item.addEventListener('click', () => {
    navItems.forEach(n => n.classList.remove('active'));
    tabs.forEach(t => t.classList.remove('active'));
    item.classList.add('active');
    document.getElementById('tab-' + item.dataset.tab).classList.add('active');
    if (item.dataset.tab === 'system') loadLog();
  });
});

// ── Live clock ───────────────────────────────────────────────────────────────
(function tickClock() {
  const el = document.getElementById('tb-clock');
  function update() {
    el.textContent = new Date().toLocaleString('ru-RU', {
      timeZone: 'Europe/Moscow', day:'2-digit', month:'2-digit', year:'numeric',
      hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false
    });
  }
  update(); setInterval(update, 1000);
})();

// ── Helpers ───────────────────────────────────────────────────────────────
function fmtBytes(b) {
  if (b < 1024) return b + ' Б';
  if (b < 1048576) return (b/1024).toFixed(1) + ' КБ';
  if (b < 1073741824) return (b/1048576).toFixed(1) + ' МБ';
  return (b/1073741824).toFixed(2) + ' ГБ';
}
function fmtKB(kb) { return fmtBytes(kb * 1024); }

function arcDash(pct, maxArc) {
  const fill = Math.min(1, Math.max(0, pct / 100)) * maxArc;
  return fill + ' ' + (maxArc - fill);
}

function svcBadge(el, state) {
  el.textContent = state === 'active' ? 'active' : state || '?';
  el.className = 'badge ' + (state === 'active' ? 'badge-ok' : 'badge-err');
}

// ── Status polling ────────────────────────────────────────────────────────
function applyStatus(d) {
  const ARC = 126; // half-circle perimeter ≈ π*40

  // CPU
  document.getElementById('cpu-val').textContent = d.cpu_usage + '%';
  document.getElementById('cpu-arc').style.strokeDasharray = arcDash(d.cpu_usage, ARC);
  const cpuColor = d.cpu_usage > 80 ? '#f85149' : d.cpu_usage > 60 ? '#e3b341' : '#388bfd';
  document.getElementById('cpu-arc').style.stroke = cpuColor;

  // RAM
  document.getElementById('ram-val').textContent = fmtKB(d.ram_used_kb);
  document.getElementById('ram-sub').textContent = 'из ' + fmtKB(d.ram_total_kb);
  document.getElementById('ram-pct').textContent = d.ram_pct + '%';
  document.getElementById('ram-free-lbl').textContent = 'свободно ' + fmtKB(d.ram_free_kb);
  const ramBar = document.getElementById('ram-bar');
  ramBar.style.width = d.ram_pct + '%';
  ramBar.style.background = d.ram_pct > 90 ? '#f85149' : d.ram_pct > 70 ? '#e3b341' : '#388bfd';

  // Temp
  document.getElementById('temp-val').textContent = d.temp_c;
  document.getElementById('temp-arc').style.strokeDasharray = arcDash(Math.min(d.temp_c, 100), ARC);
  document.getElementById('temp-arc').style.stroke = d.temp_c > 80 ? '#f85149' : d.temp_c > 60 ? '#e3b341' : '#f78166';

  // Disk
  document.getElementById('disk-val').textContent = fmtKB(d.disk_used_kb);
  document.getElementById('disk-sub').textContent = 'из ' + fmtKB(d.disk_total_kb);
  document.getElementById('disk-pct').textContent = d.disk_pct + '%';
  document.getElementById('disk-free-lbl').textContent = 'свободно ' + fmtKB(d.disk_free_kb);
  const diskBar = document.getElementById('disk-bar');
  diskBar.style.width = d.disk_pct + '%';
  diskBar.style.background = d.disk_pct > 90 ? '#f85149' : d.disk_pct > 70 ? '#e3b341' : '#3fb950';

  // Uptime
  document.getElementById('uptime-val').textContent = d.uptime_str;

  // Network eth0
  document.getElementById('net-rx').textContent = fmtBytes(d.net_rx_bytes);
  document.getElementById('net-tx').textContent = fmtBytes(d.net_tx_bytes);

  // eth1
  const st = d.eth1_operstate || 'absent';
  const stRu = st === 'up' ? 'линк UP' : st === 'down' ? 'линк DOWN' : st === 'absent' ? 'интерфейс не найден' : st;
  document.getElementById('eth1-state').textContent = stRu;
  document.getElementById('eth1-traf').textContent = 'RX ' + fmtBytes(d.net1_rx_bytes||0) + ' · TX ' + fmtBytes(d.net1_tx_bytes||0);

  // HW GPIO
  const hint = document.getElementById('hw-hint');
  if (d.hw_configured) {
    hint.textContent = 'Управление через GPIO (см. /etc/sa02m_hw.conf)';
  } else {
    hint.textContent = 'GPIO не заданы: отредактируйте /etc/sa02m_hw.conf (SA02M_GPIO_DO, SA02M_GPIO_BEEPER, SA02M_GPIO_ALARM_LED), затем перезагрузите или экспортируйте линии.';
  }
  setHwRow('hw-do-st', d.hw_do);
  setHwRow('hw-beep-st', d.hw_beeper);
  setHwRow('hw-led-st', d.hw_alarm_led);
  document.querySelectorAll('.hw-btns button').forEach(b => {
    b.disabled = !d.hw_configured;
    b.style.opacity = d.hw_configured ? '1' : '0.35';
  });

  // Services
  svcBadge(document.getElementById('svc-nginx'), d.svc_nginx);
  svcBadge(document.getElementById('svc-fcgiwrap'), d.svc_fcgiwrap);
  svcBadge(document.getElementById('svc-mplc'), d.mplc_status);
  const mu = document.getElementById('mplc-uptime');
  if (d.mplc_status === 'active' && d.mplc_uptime_s > 0) {
    mu.textContent = 'Работает ' + fmtUptime(d.mplc_uptime_s);
  } else { mu.textContent = ''; }

  // Load averages
  document.getElementById('load-1').textContent  = d.load_1  || '—';
  document.getElementById('load-5').textContent  = d.load_5  || '—';
  document.getElementById('load-15').textContent = d.load_15 || '—';
  document.getElementById('proc-info').textContent = 'Процессов: ' + (d.proc_running||0) + ' / ' + (d.proc_total||0);
  const freqEl = document.getElementById('cpu-freq-info');
  if (d.cpu_freq_mhz) {
    const thr = d.cpu_throttle ? ' (' + d.cpu_throttle + '% от макс)' : '';
    freqEl.textContent = 'CPU: ' + d.cpu_freq_mhz + ' МГц' + thr;
  }

  // Swap
  if (d.swap_total_kb > 0) {
    document.getElementById('swap-block').style.display = 'block';
    document.getElementById('swap-pct').textContent = d.swap_pct + '%';
    document.getElementById('swap-lbl').textContent = fmtKB(d.swap_used_kb) + ' / ' + fmtKB(d.swap_total_kb);
    const sb = document.getElementById('swap-bar');
    sb.style.width = d.swap_pct + '%';
    sb.style.background = d.swap_pct > 80 ? '#f85149' : '#9a4b00';
  }

  // System info
  if (d.board) document.getElementById('board-info').textContent = d.board;
  if (d.cpu_model) document.getElementById('cpu-model-info').textContent = d.cpu_model;
  if (d.kernel) document.getElementById('kernel-info').textContent = 'Ядро: ' + d.kernel;
  if (d.disk_io_read_b !== undefined) {
    document.getElementById('disk-io-info').textContent =
      'Диск прочитано: ' + fmtBytes(d.disk_io_read_b) + ' · записано: ' + fmtBytes(d.disk_io_write_b);
  }

  // RS-485
  if (d.rs485 && d.rs485.length) renderRs485(d.rs485);

  // IP in topbar
  if (d.ip) document.getElementById('tb-ip').textContent = d.ip;
}

// ── RS-485 rendering ──────────────────────────────────────────────────────────
const _prevRs = {};

function fmtNum(n) {
  n = parseInt(n) || 0;
  if (n >= 1e6) return (n/1e6).toFixed(2) + ' М';
  if (n >= 1e3) return (n/1e3).toFixed(1) + ' К';
  return n.toString();
}

function fmtUptime(s) {
  const d = Math.floor(s/86400), h = Math.floor((s%86400)/3600), m = Math.floor((s%3600)/60);
  if (d) return d + 'д ' + h + 'ч ' + m + 'м';
  if (h) return h + 'ч ' + m + 'м';
  return m + 'м';
}

function renderRs485(ports) {
  const grid = document.getElementById('rs485-grid');
  if (!grid) return;
  ports.forEach(p => {
    const absent  = p.st === 'absent';
    const prev    = _prevRs[p.n] || {tx: p.tx, rx: p.rx};
    const actNow  = !absent && (p.tx !== prev.tx || p.rx !== prev.rx);
    _prevRs[p.n]  = {tx: p.tx, rx: p.rx};

    let card = document.getElementById('rs485c-' + p.n);
    if (!card) {
      card = document.createElement('div');
      card.id = 'rs485c-' + p.n;
      grid.appendChild(card);
    }

    card.className = 'rs485-port' + (absent ? ' absent' : '');

    if (actNow) {
      card.classList.add('act');
      clearTimeout(card._actTimer);
      card._actTimer = setTimeout(() => card.classList.remove('act'), 1600);
    }

    const dotCls = absent ? 'absent' : (p.open ? 'on' : 'idle');
    const errHtml = (p.fe || p.pe || p.oe)
      ? '<div class="rs485-err">Ош: FE=' + p.fe + ' PE=' + p.pe + ' OE=' + p.oe + '</div>' : '';
    const openHtml = absent ? '' : (p.open
      ? '<div class="rs485-open">● используется</div>'
      : '<div class="rs485-closed">○ свободен</div>');

    const txCls = actNow ? ' act' : '';
    card.innerHTML =
      '<div class="rs485-hdr">' +
        '<span class="rs485-dot ' + dotCls + '"></span>' +
        '<span class="rs485-name">RS-485-' + p.n + '</span>' +
      '</div>' +
      '<div class="rs485-dev">' + (absent ? 'не найден' : p.dev) + '</div>' +
      '<div class="rs485-row"><span class="rl">TX</span><span class="rv' + txCls + '">' + fmtNum(p.tx) + '</span></div>' +
      '<div class="rs485-row"><span class="rl">RX</span><span class="rv' + txCls + '">' + fmtNum(p.rx) + '</span></div>' +
      openHtml +
      errHtml;
  });
}

function setHwRow(id, v) {
  const el = document.getElementById(id);
  if (v === -1 || v === undefined) {
    el.textContent = 'н/д';
    el.className = 'hw-state na';
    return;
  }
  el.textContent = v ? 'ВКЛ (1)' : 'ВЫКЛ (0)';
  el.className = 'hw-state ' + (v ? 'on' : 'off');
}

function setHw(channel, value) {
  const body = 'channel=' + encodeURIComponent(channel) + '&value=' + encodeURIComponent(value);
  fetch('/cgi-bin/hw_set.cgi', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body, credentials:'same-origin'})
    .then(r => r.json())
    .then(j => {
      if (j.ok) fetchStatus();
      else if (j.error === 'gpio_not_configured') alert('GPIO не настроен в /etc/sa02m_hw.conf');
      else alert('Ошибка: ' + (j.error || 'unknown'));
    })
    .catch(() => alert('Сеть или сервер недоступны'));
}

let fetchBusy = false;
function fetchStatus() {
  if (fetchBusy) return;
  fetchBusy = true;
  fetch('/cgi-bin/status.cgi', {cache:'no-store'})
    .then(r => r.json())
    .then(d => { if (!d.error) applyStatus(d); })
    .catch(() => {})
    .finally(() => { fetchBusy = false; });
}
fetchStatus();
setInterval(fetchStatus, 4000);

// ── Log loader ────────────────────────────────────────────────────────────
function loadLog() {
  fetch('/cgi-bin/log.cgi', {cache:'no-store'})
    .then(r => r.text())
    .then(t => {
      const box = document.getElementById('log-box');
      box.innerHTML = t.split('\n').map(line => {
        if (/error|ошибк/i.test(line)) return '<span class="log-err">'+escHtml(line)+'</span>';
        if (/ok|успешн|applied|reboot|started/i.test(line)) return '<span class="log-ok">'+escHtml(line)+'</span>';
        return escHtml(line);
      }).join('\n');
      box.scrollTop = box.scrollHeight;
    })
    .catch(() => { document.getElementById('log-box').textContent = 'Не удалось загрузить журнал'; });
}
function escHtml(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── Toast auto-hide ───────────────────────────────────────────────────────
const toast = document.querySelector('.toast');
if (toast) setTimeout(() => { toast.style.opacity='0'; toast.style.transition='opacity .5s'; setTimeout(()=>toast.remove(),500); }, 4000);

// ── IP validation ─────────────────────────────────────────────────────────
document.querySelectorAll('#net-form input, #net-form-eth1 input[type=text]').forEach(inp => {
  inp.addEventListener('blur', () => {
    if (inp.pattern && inp.value && !new RegExp('^'+inp.pattern+'$').test(inp.value))
      inp.classList.add('invalid');
    else
      inp.classList.remove('invalid');
  });
});

document.getElementById('net-form-eth1').addEventListener('submit', function(ev) {
  const en = document.getElementById('eth1-en').checked;
  const ip = document.getElementById('f-ip1').value.trim();
  const mask = document.getElementById('f-mask1').value.trim();
  if (en && (!ip || !mask)) {
    ev.preventDefault();
    alert('Для включения eth1 укажите IP и маску подсети');
  }
});
</script>
</body>
</html>
HTML
