"""Container HTTP smoke with an authenticated, synthetic private app peer.

The peer implements the version-one byte protocol in a separate process. It is
mounted only by this harness; production has no fixture backend switch.
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path


PEER = r'''
import base64
import hashlib
import io
import json
import os
import socket
import threading
import uuid
from pathlib import Path
from PIL import Image

ROOT = Path('/run/obsidian-bridge')
REV = lambda body: 'sha256:' + hashlib.sha256(body.encode()).hexdigest()
notes = {vault: {'Owners/owner.md': '# ' + vault + ' owner\n'} for vault in ('iam', 'homelab')}
lock = threading.Lock()
receipts = {}
DIGEST = lambda vault, client, op, mutation: hashlib.sha256(json.dumps(
    [vault, client, op, mutation], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
VIEW = lambda receipt, entry: {'receipt': receipt,
                               'status': entry['state'] if entry and entry['state'] != 'reserved' else
                               ('pending' if entry else 'indeterminate'),
                               'result': entry['result'] if entry else None,
                               'error': entry['error'] if entry else None}
image = Image.new('RGB', (2, 2), (20, 40, 60))
stream = io.BytesIO()
image.save(stream, 'PNG')
picture = base64.b64encode(stream.getvalue()).decode()


def serve(vault):
    directory = ROOT / vault
    token = (directory / 'credential').read_text().strip()
    path = str(directory / (vault + '.sock'))
    address = '\0obsidian-bridge-' + hashlib.sha256((path + '\0' + token).encode()).hexdigest()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(address)
    listener.listen(16)
    while True:
        connection, _ = listener.accept()
        try:
            connection.settimeout(3)
            incoming = bytearray()
            while b'\n' not in incoming and len(incoming) <= 384 * 1024:
                piece = connection.recv(65536)
                if not piece:
                    break
                incoming.extend(piece)
            request = json.loads(incoming)
            op = request.get('op')
            args = request.get('args')
            if (request.get('v') != 1 or request.get('vault') != vault or
                    request.get('token') != token or not isinstance(args, dict)):
                error = 'unauthorized'
                result = None
            else:
                with lock:
                    error, result = operation(vault, op, args)
            reply = {'v': 1, 'vault': vault, 'ok': error is None}
            reply['result' if error is None else 'error'] = result if error is None else error
            connection.sendall(json.dumps(reply, separators=(',', ':')).encode() + b'\n')
        except (OSError, ValueError, TypeError, KeyError):
            pass
        finally:
            connection.close()


def operation(vault, op, args):
    store = notes[vault]
    if op == 'health':
        return None, {'protocol': 1, 'vault': vault,
                      'app_name': 'IAM Team' if vault == 'iam' else 'Homelab',
                      'app_root': '/vaults/' + vault,
                      'capabilities': ['health', 'list', 'read', 'search', 'create', 'append', 'embed',
                                       'reserve', 'receipt']}
    if op == 'list':
        return None, {'entries': [{'path': path, 'kind': 'file'} for path in sorted(store)]}
    if op == 'search':
        query = args.get('query', '')
        return None, {'matches': [{'path': path, 'offset': body.find(query), 'revision': REV(body)}
                                  for path, body in store.items() if query in body][:20]}
    if op == 'embed':
        if args.get('source') not in store or args.get('target') != 'picture.png':
            return 'not_found', None
        return None, {'path': 'images/picture.png', 'mime': 'image/png', 'data': picture}
    if op == 'reserve':
        if set(args) != {'client', 'operation', 'mutation'} or args['operation'] not in ('create', 'append'):
            return 'invalid_request', None
        receipt = str(uuid.uuid4())
        receipts[receipt] = {'digest': DIGEST(vault, args['client'], args['operation'], args['mutation']),
                             'operation': args['operation'], 'state': 'reserved',
                             'result': None, 'error': None}
        return None, {'receipt': receipt}
    if op == 'receipt' or (op in ('create', 'append') and 'receipt' in args):
        entry = receipts.get(args.get('receipt'))
        operation = args.get('operation') if op == 'receipt' else op
        if entry is None:
            return (None, VIEW(args.get('receipt'), None)) if op == 'receipt' else ('unknown_receipt', None)
        if (entry['operation'] != operation or
                entry['digest'] != DIGEST(vault, args.get('client'), operation, args.get('mutation'))):
            return 'unauthorized', None
        if op == 'receipt' or entry['state'] != 'reserved':
            return None, VIEW(args['receipt'], entry)
        error, result = mutate(store, op, args['mutation'])
        entry.update(state='failed' if error else 'committed', error=error, result=result)
        return None, VIEW(args['receipt'], entry)
    if op in ('create', 'append'):
        return 'invalid_request', None
    path = args.get('path')
    if not isinstance(path, str) or not path.endswith('.md') or '..' in path or path.startswith('/'):
        return 'invalid_path', None
    if op == 'read':
        if path not in store:
            return 'not_found', None
        body = store[path]
        return None, {'path': path, 'content': body, 'revision': REV(body)}
    return 'unknown_operation', None


def mutate(store, op, args):
    path = args.get('path') if isinstance(args, dict) else None
    if not isinstance(path, str) or not path.endswith('.md') or '..' in path or path.startswith('/'):
        return 'invalid_path', None
    if op == 'create':
        if path in store:
            return 'conflict', None
        body = args.get('content')
        if not isinstance(body, str):
            return 'invalid_request', None
        store[path] = body
        return None, {'path': path, 'revision': REV(body)}
    if path not in store or args.get('expected_revision') != REV(store[path]):
        return 'conflict', None
    content = args.get('content')
    if not isinstance(content, str):
        return 'invalid_request', None
    store[path] += content
    return None, {'path': path, 'revision': REV(store[path])}


for item in ('iam', 'homelab'):
    threading.Thread(target=serve, args=(item,), daemon=True).start()
threading.Event().wait()
'''

CLIENT = r'''
import asyncio
import base64
import io
import json
import os
import socket
from pathlib import Path

import httpx
from fastmcp import Client
from PIL import Image
from bridge import VaultEndpoint, _socket_address

URL = 'http://127.0.0.1:8000/mcp'
TOKENS = {client: os.environ['OBSIDIAN_MCP_TOKEN_' + client.upper()]
          for client in ('codex', 'claude', 'opencode', 'antigravity')}


def phase(label):
    print('SMOKE_PHASE:' + label, flush=True)


def body(result):
    assert not result.is_error
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def run():
    phase('peer_auth')
    directory = Path('/run/obsidian-bridge/iam')
    token = (directory / 'credential').read_text().strip()
    endpoint = VaultEndpoint('iam', 'IAM Team', Path('/vaults/iam'),
                             directory / 'iam.sock', directory / 'credential')
    for forged_vault, forged_token in (('iam', 'wrong'), ('homelab', token)):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(_socket_address(endpoint, token))
            connection.sendall((json.dumps({'v': 1, 'vault': forged_vault,
                                            'token': forged_token, 'op': 'read',
                                            'args': {'path': 'Owners/owner.md'}}) + '\n').encode())
            reply = json.loads(connection.recv(4096))
            assert reply['ok'] is False and reply['error'] == 'unauthorized'
    phase('health_auth')
    health = httpx.get('http://127.0.0.1:8000/health', timeout=5)
    assert health.status_code == 200 and health.json() == {'status': 'ok'}
    assert not any(word in health.text for word in ('iam', 'homelab', 'token', '/vaults'))
    initialize = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-06-18', 'capabilities': {},
        'clientInfo': {'name': 'packaging-smoke', 'version': '1'}}}
    for credential in ('', 'Bearer invalid-fixture-token'):
        headers = {'Accept': 'application/json, text/event-stream'}
        if credential:
            headers['Authorization'] = credential
        response = httpx.post(URL, json=initialize, headers=headers, timeout=5)
        assert response.status_code in (401, 403)
    owner = httpx.post('http://127.0.0.1:8000/owner/' + 'a' * 24,
                       headers={'Authorization': 'Bearer ' + TOKENS['codex'],
                                'X-authentik-uid': 'owner', 'Origin': 'https://approval.example.test'},
                       timeout=5)
    assert owner.status_code == 401

    phase('app_peer_tools')
    expected = {'list_vaults', 'list_entries', 'read_note', 'search_notes',
                'create_note', 'append_note', 'mutation_receipt', 'read_embedded_image'}
    for client_name, vault in (('codex', 'iam'), ('claude', 'homelab'),
                               ('opencode', 'iam'), ('antigravity', 'homelab')):
        async with Client(URL, auth=TOKENS[client_name]) as client:
            assert {tool.name for tool in await client.list_tools()} == expected
            assert body(await client.call_tool('list_vaults')) == {'vaults': [vault]}
            own = body(await client.call_tool('read_note', {
                'vault': vault, 'path': 'Owners/owner.md'}))
            assert own['content'] == '# ' + vault + ' owner\n'
            foreign = 'homelab' if vault == 'iam' else 'iam'
            for tool, arguments in (
                ('read_note', {'path': 'Owners/owner.md'}),
                ('create_note', {'path': 'Smoke/foreign.md', 'content': 'forbidden'}),
                ('append_note', {'path': 'Owners/owner.md', 'content': 'forbidden',
                                 'expected_revision': own['revision']}),
            ):
                denied = await client.call_tool(tool, {'vault': foreign, **arguments},
                                                raise_on_error=False)
                assert denied.is_error
                assert TOKENS[client_name] not in str(denied.content)
            assert (await client.call_tool('read_note', {
                'vault': foreign, 'path': 'Smoke/foreign.md'},
                raise_on_error=False)).is_error

    phase('exact_mutations')
    async with Client(URL, auth=TOKENS['codex']) as client:
        content = '# Packaged fixture\n\n  exact indentation\n'
        path = 'Smoke/packaged.md'
        created = body(await client.call_tool('create_note', {
            'vault': 'iam', 'path': path, 'content': content}))
        assert created['path'] == path and created['status'] == 'committed'
        assert len(created['receipt']) == 36
        replay = body(await client.call_tool('mutation_receipt', {
            'vault': 'iam', 'operation': 'create', 'path': path, 'content': content,
            'receipt': created['receipt']}))
        assert replay['status'] == 'committed' and replay['revision'] == created['revision']
        assert (await client.call_tool('mutation_receipt', {
            'vault': 'iam', 'operation': 'create', 'path': path, 'content': content + 'x',
            'receipt': created['receipt']}, raise_on_error=False)).is_error
        first = body(await client.call_tool('read_note', {'vault': 'iam', 'path': path}))
        assert first['content'].encode() == content.encode()
        addition = '\n  appended exactly\n'
        appended = body(await client.call_tool('append_note', {
            'vault': 'iam', 'path': path, 'content': addition,
            'expected_revision': first['revision']}))
        assert appended['status'] == 'committed' and appended['receipt'] != created['receipt']
        second = body(await client.call_tool('read_note', {'vault': 'iam', 'path': path}))
        assert second['content'].encode() == (content + addition).encode()
        assert appended['revision'] == second['revision']
        stale = await client.call_tool('append_note', {
            'vault': 'iam', 'path': path, 'content': 'stale',
            'expected_revision': first['revision']}, raise_on_error=False)
        assert stale.is_error
        assert body(await client.call_tool('read_note', {'vault': 'iam', 'path': path}))['content'] == content + addition
        image = await client.call_tool('read_embedded_image', {
            'vault': 'iam', 'source': path, 'target': 'picture.png'})
        assert not image.is_error
        blocks = [part for part in image.content if part.type == 'image']
        assert len(blocks) == 1 and blocks[0].mimeType == 'image/png'
        with Image.open(io.BytesIO(base64.b64decode(blocks[0].data))) as decoded:
            assert decoded.getpixel((0, 0)) == (20, 40, 60)
        assert (await client.call_tool('read_embedded_image', {
            'vault': 'iam', 'source': path, 'target': 'https://remote.invalid/a.png'},
            raise_on_error=False)).is_error
        assert (await client.call_tool('write_note_tool', {'vault': 'iam', 'path': 'Smoke/legacy.md',
                                                           'content': 'bypass'},
                                        raise_on_error=False)).is_error
        assert not Path('/vaults/iam/Smoke/packaged.md').exists()
        assert not Path('/vaults/iam/Smoke/legacy.md').exists()
        assert Path('/app/gateway.py').is_file()
        compatibility = Path('/app/server.py').read_text()
        assert 'obsidian_remote_mcp' not in compatibility
        assert 'def _valid_token' in compatibility and 'def _note_path' not in compatibility
        assert Path('/opt/obsidian-mcp/bridge-plugin/main.js').is_file()
        assert Path('/opt/obsidian-mcp/bridge-plugin/manifest.json').is_file()
        for protected in ('/app/gateway.py', '/opt/obsidian-mcp/bridge-plugin/main.js'):
            try:
                Path(protected).open('a')
                raise AssertionError('packaged source writable')
            except OSError:
                pass
    phase('complete')

asyncio.run(run())
'''

PEER_READY = (
    "import json; from pathlib import Path; from bridge import BridgeClient,VaultEndpoint; "
    "d=json.loads(Path('/etc/obsidian-mcp/registry.json').read_text()); "
    "e={k:VaultEndpoint(k,v['app_name'],Path(v['app_root']),Path(v['socket_path']),"
    "Path(v['credential_file'])) for k,v in d['vaults'].items()}; "
    "c=BridgeClient(e); assert all(c.ready(k)['vault']==k for k in ('iam','homelab'))"
)


def docker(*args, input=None, timeout=30):
    return subprocess.run(['docker', *args], input=input, text=True,
                          capture_output=True, timeout=timeout)


def write_private_log(path, contents):
    # Persist only fixed phase names and exception class names, never messages.
    lines = []
    for line in contents.splitlines():
        phase = re.fullmatch(r'SMOKE_PHASE:([a-z_]+)', line)
        error = re.match(r'^([A-Za-z_][\w.]*(?:Error|Exception)):', line)
        if phase:
            lines.append('SMOKE_PHASE:' + phase.group(1))
        elif error:
            lines.append(error.group(1))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        output.write(('\n'.join(lines) + '\n')[:4096])


def cleanup_fixture(name, root, *, started):
    error = False
    if started:
        try:
            logs = docker('logs', name, timeout=5)
            if logs.returncode:
                error = True
            else:
                write_private_log(root / 'container.log', logs.stdout + logs.stderr)
        except (OSError, subprocess.TimeoutExpired):
            error = True
        try:
            stopped = docker('stop', '-t', '5', name, timeout=10)
            if stopped.returncode:
                error = True
            else:
                state = docker('inspect', '-f', '{{.State.ExitCode}}', name, timeout=5)
                if state.returncode or state.stdout.strip() != '0':
                    error = True
        except (OSError, subprocess.TimeoutExpired):
            error = True
        try:
            removed = docker('rm', '-f', name, timeout=5)
            if removed.returncode:
                error = True
        except (OSError, subprocess.TimeoutExpired):
            error = True
    if error:
        raise RuntimeError('fixture cleanup failed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    name = 'obsidian-mcp-smoke-' + secrets.token_hex(6)
    with tempfile.TemporaryDirectory(prefix='obsidian-mcp-smoke-') as temporary:
        root = Path(temporary)
        runtime = root / 'runtime'
        runtime.mkdir(mode=0o700)
        vaults = root / 'vaults'
        vaults.mkdir(mode=0o700)
        for vault in ('iam', 'homelab'):
            directory = runtime / vault
            directory.mkdir(mode=0o700)
            credential = directory / 'credential'
            credential.write_text(secrets.token_urlsafe(32))
            credential.chmod(0o600)
            (vaults / vault).mkdir(mode=0o700)
        registry_dir = root / 'registry'
        registry_dir.mkdir(mode=0o755)
        registry = registry_dir / 'registry.json'
        verbs = ['list_notes', 'read_note', 'search', 'create_note', 'append_note', 'read_media']
        grants = {'codex': 'iam', 'claude': 'homelab',
                  'opencode': 'iam', 'antigravity': 'homelab'}
        registry.write_text(json.dumps({
            'vaults': {vault: {'app_name': 'IAM Team' if vault == 'iam' else 'Homelab',
                               'app_root': '/vaults/' + vault,
                               'socket_path': '/run/obsidian-bridge/' + vault + '/' + vault + '.sock',
                               'credential_file': '/run/obsidian-bridge/' + vault + '/credential',
                               'owner_ids': ['owner-uid']} for vault in ('iam', 'homelab')},
            'clients': {client: {vault: verbs} for client, vault in grants.items()},
            'owner': {'check_url': 'http://authentik.security.svc.cluster.local:9000/outpost.goauthentik.io/auth/nginx',
                      'origin': 'https://approval.example.test', 'owner_ids': ['owner-uid']},
        }))
        registry.chmod(0o444)
        registry_dir.chmod(0o555)
        peer = root / 'app_peer.py'
        peer.write_text(PEER)
        peer.chmod(0o444)
        tokens = root / 'tokens.env'
        tokens.write_text(''.join(f'OBSIDIAN_MCP_TOKEN_{client.upper()}={secrets.token_urlsafe(32)}\n'
                                  for client in grants))
        tokens.chmod(0o600)
        started = False
        try:
            command = ['run', '-d', '--name', name, '--read-only', '--network', 'none',
                       '--user', '1000:1000', '--cap-drop', 'ALL',
                       '--security-opt', 'no-new-privileges',
                       '--mount', f'type=bind,src={registry_dir},dst=/etc/obsidian-mcp,readonly',
                       '--mount', f'type=bind,src={runtime},dst=/run/obsidian-bridge',
                       '--mount', f'type=bind,src={vaults / "iam"},dst=/vaults/iam,readonly',
                       '--mount', f'type=bind,src={vaults / "homelab"},dst=/vaults/homelab,readonly',
                       '--mount', f'type=bind,src={peer},dst=/fixture/app_peer.py,readonly',
                       '--env-file', str(tokens),
                       '-e', 'OBSIDIAN_GATEWAY_REGISTRY=/etc/obsidian-mcp/registry.json', args.image]
            # Bind-mounted private runtime files must belong to the runtime UID.
            owner = docker('run', '--rm', '--network', 'none', '--user', '0:0',
                           '--mount', f'type=bind,src={runtime},dst=/fixture/runtime',
                           '--entrypoint', 'python', args.image, '-c',
                           "import os; from pathlib import Path; p=Path('/fixture/runtime'); "
                           "[os.chown(str(x),1000,1000) for x in [p,*p.rglob('*')]]")
            if owner.returncode:
                raise RuntimeError('fixture ownership setup failed')
            started = True
            launch = docker(*command)
            if launch.returncode:
                raise RuntimeError('fixture container could not start')
            peer_start = docker('exec', '-d', name, 'python', '/fixture/app_peer.py')
            if peer_start.returncode:
                raise RuntimeError('app peer could not start')
            for _ in range(30):
                ready = docker('exec', name, 'python', '-c', PEER_READY)
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('app peer did not become ready')
            for _ in range(30):
                healthy = docker('exec', name, 'python', '-c',
                                 "import json,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2)) == {'status':'ok'}")
                if healthy.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('gateway HTTP did not become ready')
            result = docker('exec', '-i', name, 'python', '-', input=CLIENT, timeout=90)
            if result.returncode:
                write_private_log(root / 'assertions.log', result.stdout + result.stderr)
                phases = re.findall(r'^SMOKE_PHASE:([a-z_]+)$', result.stdout, re.MULTILINE)
                raise RuntimeError('packaged MCP assertions failed at ' +
                                   (phases[-1] if phases else 'initialization'))
            print('packaged app gateway smoke passed: HTTP auth, four scopes, app peer mutations, image, shutdown')
        finally:
            failed = sys.exc_info()[0] is not None
            try:
                cleanup_fixture(name, root, started=started)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                if not failed:
                    raise


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.TimeoutExpired, AssertionError) as error:
        message = str(error)
        phase = re.fullmatch(r'packaged MCP assertions failed at ([a-z_]+)', message)
        raise SystemExit('image smoke failed at ' + phase.group(1) if phase else
                         'image smoke failed at runtime_or_cleanup') from None
