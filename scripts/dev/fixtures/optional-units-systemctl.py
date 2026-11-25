#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
root=Path(os.environ['TARGET_ROOT']); state_path=root/'manager.json'
state=json.loads(state_path.read_text()); args=sys.argv[1:]; verb=args[0]
with (root/'manager-calls').open('a') as f: f.write(json.dumps(args)+'\n')
if verb=='daemon-reload': sys.exit(0)
unit=args[-1]; item=state.setdefault(unit,{'enabled':'disabled','active':'inactive','timestamp':1})
p=root/'etc/systemd/system'/unit; r=root/'run/systemd/system'/unit
mask=next((('masked' if q==p else 'masked-runtime') for q in (p,r) if q.is_symlink() and os.readlink(q)=='/dev/null'),None)
exists=p.exists() or p.is_symlink() or r.exists() or r.is_symlink()
if verb=='cat':
    if exists: print(str(p)); sys.exit(0)
    sys.exit(1)
if verb=='is-enabled':
    value=mask or (item['enabled'] if exists else 'not-found'); print(value)
    sys.exit(4 if value=='not-found' else 0 if value.startswith('enabled') else 1)
if verb=='is-active': print(item['active']); sys.exit(0 if item['active']=='active' else 3)
if verb=='show':
    print('no' if 'NeedDaemonReload' in args else item['timestamp']); sys.exit(0)
if verb in ('unmask','mask'): raise SystemExit('unexpected mask mutation')
if verb in ('enable','disable'): item['enabled']='enabled' if verb=='enable' else 'disabled'
elif verb in ('start','restart'):
    if mask or not exists: raise SystemExit('start of unavailable unit')
    expected={'sa02m-status-cache.service':['sa02m-status-cache.py','sa02m-status-part'],
              'sa02m-phase-energy.service':['sa02m-phase-energy/mqtt_service.py','sa02m-phase-energy/phase_energy.py','sa02m-phase-energy/energy_state.py']}
    if not all((root/'usr/local/libexec'/name).is_file() for name in expected[unit]):
        raise SystemExit('start before runtime installation')
    item['active']='active'; item['timestamp']+=1
elif verb=='stop': item['active']='inactive'
else: raise SystemExit('unexpected systemctl verb '+verb)
state_path.write_text(json.dumps(state))
