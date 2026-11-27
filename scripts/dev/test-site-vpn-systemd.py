#!/usr/bin/env python3
"""Run after installing the units in an explicitly disposable, disconnected Linux PID1."""
import ipaddress
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import time

IP = '/usr/sbin/ip'
ROOT = Path('/etc/sa02m/site-vpn')
APPS = Path('/etc/sa02m/site-vpn-apps')
TEMP = Path('/run/sa02m-private-vpn-test')
THIS = Path(__file__).resolve()


def command(*args, stdin=None):
    return subprocess.run(args, input=stdin, text=True, capture_output=True, check=True, timeout=100).stdout.strip()


def guard():
    links = json.loads(command(IP, '-j', 'link', 'show'))
    safe = (os.environ.get('SA02M_DISPOSABLE_SITE_VPN') == '1' and os.geteuid() == 0
            and (Path('/.dockerenv').exists() or Path('/run/.containerenv').exists())
            and Path('/proc/1/comm').read_text().strip() == 'systemd'
            and not Path('/etc/armbian-release').exists() and not Path('/dev/watchdog').exists()
            and set(r['ifname'] for r in links) == {'lo'} and not TEMP.exists())
    if not safe:
        sys.exit('Requires a fresh, disconnected disposable systemd container and SA02M_DISPOSABLE_SITE_VPN=1')


def credentials():
    alg = ['-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes']
    command('openssl','req','-x509',*alg,'-keyout','ca.key','-out','ca.pem','-days','1','-subj','/CN=isolated site test')
    for name,kind in [('peer','serverAuth'),('client','clientAuth')]:
        command('openssl','req','-new',*alg,'-keyout',name+'.key','-out',name+'.csr','-subj','/CN='+name)
        Path(name+'.ext').write_text('basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage='+kind+'\n')
        command('openssl','x509','-req','-in',name+'.csr','-CA','ca.pem','-CAkey','ca.key','-CAcreateserial',
                '-out',name+'.pem','-days','1','-extfile',name+'.ext')


def private_file(path, text):
    path.write_text(text)
    path.chmod(0o600)


def setup():
    TEMP.mkdir(mode=0o700)
    os.chdir(TEMP)
    # A dynamic workload cannot read the developer account's private checkout.
    public=Path('/run/sa02m-private-vpn-test-app')
    public.mkdir(mode=0o755)
    proof=public/'application.py'
    proof.write_bytes(THIS.read_bytes())
    proof.chmod(0o644)
    command(IP,'netns','add','test-peer')
    command(IP,'link','add','outer','type','veth','peer','name','peerlink')
    command(IP,'link','set','peerlink','netns','test-peer')
    command(IP,'addr','add','192.0.2.1/30','dev','outer')
    command(IP,'link','set','outer','up')
    command(IP,'-n','test-peer','link','set','lo','up')
    command(IP,'-n','test-peer','addr','add','192.0.2.2/30','dev','peerlink')
    command(IP,'-n','test-peer','link','set','peerlink','up')
    command(IP,'link','add','site0','type','veth','peer','name','routepeer')
    command(IP,'link','set','routepeer','up')
    command(IP,'addr','add','198.51.100.1/24','dev','site0')
    command(IP,'link','set','site0','up')
    command(IP,'route','add','203.0.113.0/28','dev','site0')
    command(IP,'-n','test-peer','link','add','wgpeer','type','wireguard')
    credentials()
    for backend,v4,v6 in [('wg','10.254.19.2','2001:db8:21::2'),('ovpn','10.253.19.1','2001:db8:20::1')]:
        private = ROOT/backend
        private.mkdir(mode=0o700,parents=True)
        config={'routes':['0.0.0.0/0','::/0']}
        if backend=='wg':
            keys=[command('wg','genkey')+'\n' for _ in range(2)]
            pub=[command('wg','pubkey',stdin=key) for key in keys]
            private_file(TEMP/'peer.wg',keys[1])
            command(IP,'netns','exec','test-peer','wg','set','wgpeer','private-key',TEMP/'peer.wg',
                    'listen-port','37925','peer',pub[0],'allowed-ips','10.254.19.1/32,2001:db8:21::1/128')
            command(IP,'-n','test-peer','addr','add',v4+'/24','dev','wgpeer')
            command(IP,'-n','test-peer','-6','addr','add',v6+'/64','dev','wgpeer','nodad')
            command(IP,'-n','test-peer','link','set','wgpeer','up')
            private_file(private/'wireguard.conf','[Interface]\nPrivateKey = '+keys[0]+'\n[Peer]\nPublicKey = '+pub[1]+
                         '\nEndpoint = 192.0.2.2:37925\nAllowedIPs = 0.0.0.0/0, ::/0\n')
            config.update(backend='wireguard',addresses=['10.254.19.1/24','2001:db8:21::1/64'])
        else:
            for name in ('ca.pem','client.pem','client.key'):
                private_file(private/name,(TEMP/name).read_text())
            config.update(backend='openvpn',remote='192.0.2.2',proto='udp4',port=37923,server_name='peer')
        private_file(private/'profile.json',json.dumps(config))
        app=APPS/backend
        app.mkdir(mode=0o755,parents=True)
        (app/'resolv.conf').write_text('nameserver '+v4+'\noptions timeout:1 attempts:2\n')
        (app/'nsswitch.conf').write_text('hosts: files dns\n')
        (app/'run').write_text('#!/usr/bin/python3\n'+
            'import runpy, sys\nsys.argv = ["proof","application",'+repr(v4)+','+repr(v6)+']\n'+
            'runpy.run_path('+repr(str(proof))+',run_name="__main__")\n')
        (app/'run').chmod(0o755)
        drop=Path('/run/systemd/system')/('sa02m-site-vpn-workload@'+backend+'.service.d')
        drop.mkdir(parents=True)
        (drop/'test.conf').write_text('[Service]\nType=oneshot\nRemainAfterExit=yes\n')
    command('systemctl','daemon-reload')


def application(v4,v6):
    if os.geteuid()==0:
        raise AssertionError('workload was not deprivileged')
    try:
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()
    except OSError:
        pass
    else:
        raise AssertionError('host resolver or D-Bus socket family available')
    addrs={item[4][0] for item in socket.getaddrinfo('site.test.',None,type=socket.SOCK_DGRAM)}
    if addrs != {v4,v6}:
        raise AssertionError('private site DNS returned unexpected addresses')
    for family,address in [(socket.AF_INET,v4),(socket.AF_INET6,v6)]:
        with socket.socket(family,socket.SOCK_DGRAM) as test:
            test.settimeout(4)
            test.sendto(b'ordinary site session',(address,39282))
            if test.recv(256)!=b'ordinary site session':
                raise AssertionError('unexpected application reply')


def responder():
    landing=[]
    for fam,endpoint in [(socket.AF_INET,'0.0.0.0'),(socket.AF_INET6,'::')]:
        for port in (53,39282):
            sock=socket.socket(fam,socket.SOCK_DGRAM)
            if fam==socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
            sock.bind((endpoint,port))
            landing.append(sock)
    print('ready',flush=True)
    while True:
        active,_,_=select.select(landing,[],[])
        for sock in active:
            body,peer=sock.recvfrom(1024)
            if sock.getsockname()[1]==39282:
                sock.sendto(body,peer)
                continue
            requested=b'\x04site\x04test\x00'
            if body[12:12+len(requested)].lower()!=requested:
                continue
            end=12+len(requested)
            qtype,qclass=struct.unpack('!HH',body[end:end+4])
            if qclass!=1 or qtype not in (1,28):
                continue
            # The query source is the enrolled client's site tunnel address.
            wireguard=peer[0].startswith('10.254.') or peer[0].startswith('2001:db8:21:')
            v4,v6=('10.254.19.2','2001:db8:21::2') if wireguard else ('10.253.19.1','2001:db8:20::1')
            answer=ipaddress.ip_address(v4 if qtype==1 else v6).packed
            msg=(body[:2]+struct.pack('!HHHHH',0x8180,1,1,0,0)+body[12:end+4]+b'\xc0\x0c'+
                 struct.pack('!HHIH',qtype,1,0,len(answer))+answer)
            sock.sendto(msg,peer)


def exercise(site,v4,v6):
    vpn='sa02m-site-vpn@'+site+'.service'
    app='sa02m-site-vpn-workload@'+site+'.service'
    group='sa02m-site@'+site+'.target'
    for trial in range(2):
        command('systemctl','start',group)
        assert command('systemctl','show','-p','SystemCallArchitectures','--value',app)=='native'
        blocked=command('systemctl','show','-p','SystemCallFilter','--value',app)
        assert blocked.startswith('~') and {'io_uring_setup','io_uring_enter','io_uring_register'}<=set(blocked[1:].split())

        assert command('systemctl','is-active',vpn,app).splitlines()==['active','active']
        assert command('systemctl','show','-p','Result','--value',app)=='success'
        pid=command('systemctl','show','-p','MainPID','--value',vpn)
        fd=os.open('/proc/'+pid+'/ns/net',os.O_RDONLY)
        try:
            ns=f'/proc/{os.getpid()}/fd/{fd}'
            links=json.loads(command('nsenter','--net='+ns,'--',IP,'-j','link','show'))
            assert len(links)==2 and 'lo' in [r['ifname'] for r in links]
            if trial==0:
                # A failed provider stops the old application and loses its private
                # namespace. The active site target requests a new pair.
                command('systemctl','kill','--kill-whom=main','--signal=SIGKILL',vpn)
                deadline=time.monotonic()+35
                while time.monotonic()<deadline:
                    newpid=command('systemctl','show','-p','MainPID','--value',vpn)
                    newstate=command('systemctl','show','-p','ActiveState','--value',app)
                    if newpid not in ('0',pid) and newstate=='active':
                        break
                    time.sleep(.3)
                else:
                    raise AssertionError('site workload did not recover after VPN helper failure')
                assert command('systemctl','is-active',group)=='active'
                assert command('systemctl','show','-p','Result','--value',app)=='success'
            command('systemctl','stop',group)
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                if all(command('systemctl','show','-p','ActiveState','--value',u)=='inactive' for u in (group,app,vpn)):
                    break
                time.sleep(.1)
            else:
                raise AssertionError('site group did not complete shutdown')
            assert command('systemctl','show','-p','ActiveState','--value',app)=='inactive'
            code=('import socket,sys; s=socket.socket(int(sys.argv[1]),socket.SOCK_DGRAM)\n'
                  'try:s.connect((sys.argv[2],39282))\nexcept OSError as e:print(e.errno)')
            for family,address in [(socket.AF_INET,v4),(socket.AF_INET6,v6)]:
                if trial==1:
                    outcome=command('nsenter','--net='+ns,'--',sys.executable,'-c',code,str(int(family)),address)
                    assert outcome in ('101','113'),(site,trial,address,command('nsenter','--net='+ns,'--',IP,'-j','link','show'))
        finally:
            os.close(fd)
        assert json.loads(command(IP,'-j','route','get','203.0.113.8'))[0]['dev']=='site0'


def reject_private_key_errors():
    source=ROOT/'wg'
    for key in ('PrivateKey','PresharedKey'):
        name='invalid-'+key.lower()
        profile=ROOT/name
        profile.mkdir(mode=0o700)
        marker='synthetic-marker-do-not-log-'+name
        lines=(source/'wireguard.conf').read_text().splitlines()
        if key=='PrivateKey':
            lines=[key+' = '+marker if line.startswith(key+' =') else line for line in lines]
        else:
            lines.append(key+' = '+marker)
        private_file(profile/'wireguard.conf','\n'.join(lines)+'\n')
        private_file(profile/'profile.json',(source/'profile.json').read_text())
        unit='sa02m-site-vpn@'+name+'.service'
        result=subprocess.run(['systemctl','start',unit],stdin=subprocess.DEVNULL,capture_output=True,timeout=20)
        command('systemctl','stop',unit)
        assert result.returncode!=0
        command('journalctl','--sync')
        log=command('journalctl','--no-pager','-b','-u',unit)
        assert marker not in log and 'wg rejected the private site configuration' in log


def main():
    guard()
    setup()
    procs=[]
    try:
        server=[IP,'netns','exec','test-peer','openvpn','--dev','testpeer','--dev-type','tun','--disable-dco',
                '--proto','udp4','--local','192.0.2.2','--port','37923','--ca','ca.pem','--cert','peer.pem','--key','peer.key',
                '--topology','subnet','--server','10.253.19.0','255.255.255.0','--server-ipv6','2001:db8:20::/64',
                '--dh','none','--remote-cert-tls','client','--log',str(TEMP/'peer.log')]
        procs.append(subprocess.Popen(server))
        deadline=time.monotonic()+10
        while not (TEMP/'peer.log').exists() or 'Initialization Sequence Completed' not in (TEMP/'peer.log').read_text():
            if procs[0].poll() is not None or time.monotonic()>=deadline:
                raise RuntimeError('local TLS peer failed')
            time.sleep(.1)
        service=subprocess.Popen([IP,'netns','exec','test-peer',sys.executable,str(THIS),'peer'],stdout=subprocess.PIPE,text=True)
        procs.append(service)
        assert select.select([service.stdout],[],[],4)[0] and service.stdout.readline().strip()=='ready'
        exercise('wg','10.254.19.2','2001:db8:21::2')
        exercise('ovpn','10.253.19.1','2001:db8:20::1')
        reject_private_key_errors()
        print('PASS: stock systemd units, per-site DNS A/AAAA, rootless IPv4 and IPv6 UDP, loss and restart, ordinary route retained (two rounds per backend).')
    finally:
        for proc in reversed(procs):
            proc.terminate()
            proc.wait(timeout=10)


if __name__=='__main__':
    if sys.argv[1:]==['peer']:
        responder()
    elif len(sys.argv)==4 and sys.argv[1]=='application':
        application(*sys.argv[2:])
    elif not sys.argv[1:]:
        main()
    else:
        sys.exit('invalid arguments')
