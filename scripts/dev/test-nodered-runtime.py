#!/usr/bin/env python3
# comment-mutation-proof-exempt: executes artifact-hash-verified Node-RED and its actual npm CLI through HTTP, credential and MQTT fixtures; source comments cannot satisfy runtime assertions.
"""Exercise real packaged Node-RED/npm against synthetic loopback fixtures."""
import argparse
import base64
import hashlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import tarfile
import threading
import tempfile
import time
import urllib.error
import urllib.request


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=os.environ.get('SA02M_NODERED_TEST_ROOT'))
    parser.add_argument('--node', type=Path, default=shutil.which('node'))
    parser.add_argument('--out', type=Path)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).resolve().parents[2] / 'etc/vendor-payloads.json')
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    if args.archive is None and os.environ.get('SA02M_NODERED_TEST_RECEIPT'):
        receipt = Path(os.environ['SA02M_NODERED_TEST_RECEIPT'])
        args.archive = receipt.parent / json.loads(receipt.read_text())['archive']
    if args.runtime is None or args.node is None or args.archive is None:
        parser.error('Provide runtime/node/archive, or the verified SA02M_NODERED_TEST_ROOT and SA02M_NODERED_TEST_RECEIPT with Node on PATH')
    if args.out is None:
        args.out = Path(tempfile.mkdtemp(prefix='sa02m-nodered-runtime-')) / 'evidence'
    runtime, out = args.runtime.resolve(), args.out.resolve()
    package_manifest = json.loads(args.manifest.read_text())['packages']['node-red']
    assert hashlib.file_digest(args.archive.open('rb'), 'sha256').hexdigest() == package_manifest['runtime']['sha256']
    verifier_path = Path(__file__).resolve().parents[2] / 'etc/sa02m-vendor-install.py'
    spec = importlib.util.spec_from_file_location('vendor_install', verifier_path)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    verifier.verify_runtime(runtime, package_manifest)
    out.mkdir(parents=True, exist_ok=False)
    user = out / 'user'
    user.mkdir()
    port, mqtt_port = free_port(), free_port()
    packages, events = {}, []
    fixture = 'node-red-contrib-sa02m-fixture'
    external = 'sa02m-external-fixture'

    def package(name, version, node_red=True):
        meta = {'name': name, 'version': version, 'main': 'index.js'}
        files = {'index.js': 'module.exports = () => 42;'}
        if node_red:
            meta['node-red'] = {'nodes': {'fixture': 'index.js'}}
            files['index.js'] = 'module.exports=function(RED){function F(c){RED.nodes.createNode(this,c);this.on("input",m=>{m.payload.token=this.credentials.token;m.payload.fixture="' + version + '";this.send(m);});}RED.nodes.registerType("sa02m-fixture",F,{credentials:{token:{type:"password"}}});};'
            files['index.html'] = '<script type="text/javascript">RED.nodes.registerType("sa02m-fixture",{category:"function",defaults:{name:{value:""}},credentials:{token:{type:"password"}},inputs:1,outputs:1,label:function(){return "fixture";}});</script>'
        files['package.json'] = json.dumps(meta)
        archive = out / f'{name}-{version}.tgz'
        with tarfile.open(archive, 'w:gz') as tf:
            for filename, content in files.items():
                b = content.encode()
                info = tarfile.TarInfo('package/' + filename)
                info.size, info.mode, info.mtime = len(b), 0o644, 0
                tf.addfile(info, io.BytesIO(b))
        packages.setdefault(name, {})[version] = (meta, archive)
        return archive

    for version in ('1.0.0', '1.0.1', '1.0.2'):
        package(fixture, version)
    package(external, '1.0.0', False)

    class Registry(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            path = self.path.split('?')[0].strip('/')
            events.append({'method': 'GET', 'path': path})
            if path.endswith('.tgz') and (out / path).is_file():
                data, content_type = (out / path).read_bytes(), 'application/octet-stream'
            elif path in packages:
                versions = {}
                for version, (meta, archive) in packages[path].items():
                    raw = archive.read_bytes()
                    versions[version] = {**meta, 'dist': {'tarball': registry_url + '/' + archive.name,
                        'shasum': hashlib.sha1(raw).hexdigest(), 'integrity': 'sha512-' + base64.b64encode(hashlib.sha512(raw).digest()).decode()}}
                data = json.dumps({'name': path, 'dist-tags': {'latest': sorted(versions)[-1]}, 'versions': versions}).encode()
                content_type = 'application/json'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    registry = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Registry)
    registry_url = f'http://127.0.0.1:{registry.server_port}'
    threading.Thread(target=registry.serve_forever, daemon=True).start()
    (user / '.npmrc').write_text(f'registry={registry_url}\naudit=false\nfund=false\nfetch-retries=0\n')
    settings = {'uiHost': '127.0.0.1', 'uiPort': port, 'httpAdminRoot': '/admin', 'flowFile': 'flows.json',
                'credentialSecret': 'synthetic-test-secret', 'functionExternalModules': True,
                'logging': {'console': {'level': 'trace'}}, 'lang': 'en-US'}
    settings_file = user / 'settings.js'
    settings_file.write_text('module.exports=' + json.dumps(settings) + ';')
    broker_script = out / 'mqtt-fixture.cjs'
    broker_script.write_text('''const net=require('net');const {createRequire}=require('module');const req=createRequire(process.argv[2]+'/package.json');const packet=req('mqtt-packet');const clients=new Map();const server=net.createServer(s=>{const parser=packet.parser();clients.set(s,new Set());s.on('data',b=>parser.parse(b));s.on('error',()=>{});s.on('close',()=>clients.delete(s));parser.on('packet',p=>{if(p.cmd==='connect')s.write(packet.generate({cmd:'connack',returnCode:0,sessionPresent:false}));if(p.cmd==='subscribe'){p.subscriptions.forEach(x=>clients.get(s).add(x.topic));s.write(packet.generate({cmd:'suback',messageId:p.messageId,granted:p.subscriptions.map(()=>0)}));}if(p.cmd==='publish'){for(const[c,t]of clients)if(t.has(p.topic))c.write(packet.generate({cmd:'publish',topic:p.topic,payload:p.payload,qos:0}));}if(p.cmd==='pingreq')s.write(packet.generate({cmd:'pingresp'}));if(p.cmd==='disconnect')s.end();});});server.listen(Number(process.argv[3]),'127.0.0.1');process.on('SIGTERM',()=>{for(const c of clients.keys())c.destroy();server.close(()=>process.exit());});''')
    broker_log = (out / 'mqtt.log').open('w')
    broker = subprocess.Popen([str(args.node), str(broker_script), str(runtime), str(mqtt_port)], stdout=broker_log, stderr=broker_log)
    env = dict(os.environ, HOME=str(user), PATH=str(args.node.parent) + ':/usr/bin:/bin')
    for key in list(env):
        if key.startswith('npm_config_') or key in ('NODE_OPTIONS', 'NODE_PATH'):
            del env[key]
    process, log, generation = None, None, 0
    checks = []

    def api(method, path, data=None, accept='application/json', language=None):
        headers = {'Accept': accept, 'Content-Type': 'application/json'}
        if language:
            headers['Accept-Language'] = language
        request = urllib.request.Request(f'http://127.0.0.1:{port}' + path, method=method, headers=headers,
            data=None if data is None else json.dumps(data).encode())
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def stop():
        nonlocal process, log
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            log.close()
            process = None

    def start():
        nonlocal process, log, generation
        generation += 1
        log = (out / f'runtime-{generation}.log').open('w')
        process = subprocess.Popen([str(args.node), str(runtime / 'red.js'), '--userDir', str(user), '--settings', str(settings_file)], env=env, stdout=log, stderr=log)
        for _ in range(200):
            assert process.poll() is None, 'runtime exited before ready'
            try:
                if api('GET', '/admin/flows')[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(.1)
        raise AssertionError('runtime startup deadline')

    def expect(method, path, data=None, status=200):
        result = api(method, path, data)
        assert result[0] == status, (method, path, result)
        return json.loads(result[1]) if result[1] else None

    def flow_result(version):
        for _ in range(200):
            status, body = api('GET', '/fixture')
            if status == 200:
                result = json.loads(body)
                if result == {'value': 42, 'answer': 6, 'token': 'synthetic-credential', 'fixture': version}:
                    return
            time.sleep(.1)
        raise AssertionError(('flow output', status, body))

    try:
        start()
        assert broker.poll() is None, 'MQTT fixture failed'
        expect('POST', '/admin/nodes', {'module': fixture, 'version': '1.0.0', 'url': registry_url + '/' + packages[fixture]['1.0.0'][1].name})
        checks.append('palette URL install with real packaged npm')
        assert expect('GET', '/admin/nodes/' + fixture)['version'] == '1.0.0'
        # /nodes/messages deliberately excludes core nodes; editor owns this catalog.
        en = api('GET', '/admin/locales/editor?lng=en-US')[1]
        ru = api('GET', '/admin/locales/editor?lng=ru')[1]
        assert en != ru and any('\u0400' <= ch <= '\u04ff' for ch in ru), 'RU/EN catalogs must differ and contain Cyrillic'
        assert api('GET', '/admin/', accept='text/html')[0] == 200
        checks.append('RU/EN editor catalog API after i18next major update')
        flows = [
            {'id': 'tab', 'type': 'tab', 'label': 'Synthetic dependencies'},
            {'id': 'in', 'z': 'tab', 'type': 'http in', 'url': '/fixture', 'method': 'get', 'wires': [['fn']]},
            {'id': 'fn', 'z': 'tab', 'type': 'function', 'func': 'msg.payload={value:extra()};return msg;', 'outputs': 1,
             'libs': [{'var': 'extra', 'module': external}], 'wires': [['audit-change']]},
            {'id': 'audit-change', 'z': 'tab', 'type': 'change', 'rules': [{'t': 'set', 'p': 'payload.answer', 'pt': 'msg', 'to': '$sum([1,2,3])', 'tot': 'jsonata'}], 'wires': [['credential']]},
            {'id': 'credential', 'z': 'tab', 'type': 'sa02m-fixture', 'credentials': {'token': 'synthetic-credential'}, 'wires': [['mqtt-out', 'wait']]},
            {'id': 'mqtt-out', 'z': 'tab', 'type': 'mqtt out', 'broker': 'broker', 'topic': 'sa02m/fixture', 'qos': '0', 'retain': 'false', 'wires': []},
            {'id': 'mqtt-in', 'z': 'tab', 'type': 'mqtt in', 'broker': 'broker', 'topic': 'sa02m/fixture', 'qos': '0', 'datatype': 'json', 'wires': [['capture']]},
            {'id': 'capture', 'z': 'tab', 'type': 'function', 'func': 'global.set("mqttFixture",msg.payload);return null;', 'outputs': 0, 'wires': []},
            {'id': 'wait', 'z': 'tab', 'type': 'function', 'func': 'const wanted=JSON.stringify(msg.payload);for(let i=0;i<100;i++){if(JSON.stringify(global.get("mqttFixture"))===wanted){msg.payload=global.get("mqttFixture");return msg;}await new Promise(r=>setTimeout(r,20));}msg.statusCode=503;msg.payload="MQTT timeout";return msg;', 'outputs': 1, 'wires': [['out']]},
            {'id': 'out', 'z': 'tab', 'type': 'http response', 'wires': []},
            {'id': 'broker', 'type': 'mqtt-broker', 'broker': '127.0.0.1', 'port': str(mqtt_port), 'protocolVersion': '4', 'clientid': 'sa02m-synthetic', 'usetls': False, 'cleansession': True, 'keepalive': '60'}
        ]
        expect('POST', '/admin/flows', flows, 204)
        flow_result('1.0.0')
        credential_file = user / 'flows_cred.json'
        assert 'synthetic-credential' not in credential_file.read_text()
        hashes = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in (user / 'flows.json', credential_file)}
        checks.append('function external-module npm install, JSONata, encrypted credentials, real MQTT nodes on loopback fixture')
        info = expect('POST', '/admin/nodes', {'module': fixture, 'version': '1.0.1'})
        assert info['pending_version'] == '1.0.1'
        stop(); start(); flow_result('1.0.1')
        assert hashes == {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in (user / 'flows.json', credential_file)}
        checks.append('registry palette upgrade, restart, preserved flow/credential bytes and decrypted result')
        expect('POST', '/admin/flows', [], 204)
        time.sleep(.3)
        expect('DELETE', '/admin/nodes/' + fixture, status=204)
        assert api('POST', '/admin/nodes', {'module': fixture, 'version': '9.9.9'})[0] >= 400
        expect('POST', '/admin/nodes', {'module': fixture, 'version': '1.0.2'})
        expect('DELETE', '/admin/nodes/' + fixture, status=204)
        expect('POST', '/admin/nodes', {'module': fixture, 'version': '1.0.0', 'url': str(packages[fixture]['1.0.0'][1])})
        expect('DELETE', '/admin/nodes/' + fixture, status=204)
        checks.append('failed registry install, successful retry, local tar install and remove')
        trace = '\n'.join(p.read_text() for p in out.glob('runtime-*.log'))
        assert str(runtime / 'node_modules/npm/bin/npm-cli.js') in trace
        assert 'allow-remote' not in trace
        result = {'ok': True, 'architecture': os.uname().machine, 'runtime': str(runtime), 'evidence': str(out),
                  'node': subprocess.check_output([str(args.node), '--version'], text=True).strip(),
                  'npm': json.loads((runtime / 'node_modules/npm/package.json').read_text())['version'],
                  'inventory_sha256': hashlib.sha256((runtime / '.sa02m-inventory.json').read_bytes()).hexdigest(),
                  'archive_sha256': package_manifest['runtime']['sha256'],
                  'source_lock_sha256': package_manifest['source_lock_sha256'],
                  'source_manifest_sha256': package_manifest['source_manifest_sha256'], 'checks': checks}
        verifier.verify_runtime(runtime, package_manifest)
        (out / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
    finally:
        stop()
        broker.terminate()
        try:
            broker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            broker.kill(); broker.wait()
        broker_log.close()
        registry.shutdown()
        (out / 'registry-requests.json').write_text(json.dumps(events, indent=2))


if __name__ == '__main__':
    main()
