# SA-02m flash stand (FEL → Ethernet)

Operator one-pager:

```text
Раз за смену:  .\start-stand.ps1
На каждую плату:
  1) Ethernet + USB-OTG к стенду + питание
  2) Войти в FEL  (если нужно — вставить FEL-USB)
  3) Ждать статус DONE на http://localhost:8765
```

## Layout

| Path | Role |
|---|---|
| `start-stand.ps1` / `start-stand.sh` | Shift launcher |
| `stop-stand.sh` | Stop dnsmasq/HTTP/agents |
| `fel-agent.py` | USB `1f3a:efe8` → `sunxi-fel uboot` |
| `postflash-monitor.py` | SSH/web QA → DONE/FAIL |
| `status-server.py` | UI + `/api/status` on `:8765` |
| `stand.env.example` | Copy → `stand.env` |

Runtime data (gitignored): `../stand-data/{tftpboot,images,run,logs}/`.

See [SA02M_IMAGING_GUIDE §11.4](../../../docs/SA02M_IMAGING_GUIDE.md#114-вариант-d--стенд-fel--ethernet-netinstall-серийное-производство).
