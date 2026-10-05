"""Counts-only SQLite evidence for the Grafana 13 rehearsal; never selects titles, JSON, secrets or hashes."""
import hashlib
import json
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path

DATA = Path(os.environ.get('GRAFANA_DATA', '/var/lib/grafana'))
DB = DATA / 'grafana.db'
EVIDENCE = DATA / '.g13-rehearsal'
PRE, POST = EVIDENCE / 'pre.json', EVIDENCE / 'post.json'
PROVISIONING = Path(os.environ.get('GRAFANA_PROVISIONING', '/etc/grafana/provisioning'))
PROVIDERS = PROVISIONING / 'dashboards/providers.yaml'
# v13.2.3 resource_mig.go, resources.go, migration_registrar.go: org 1 is namespace "default", action 3 is a
# deletion marker, and folders/dashboards read as unified only once their MigrationID row logged success.
NAMESPACE = 'default'
# The migration walks every org and counts every non-deleted row, provisioned ones included.
ORG_NAMESPACE = f"CASE WHEN org_id = 1 THEN '{NAMESPACE}' ELSE 'org-' || org_id END"
UNIFIED = {'unified_dashboards': ('dashboard.grafana.app', 'dashboards'),
           'unified_folders': ('folder.grafana.app', 'folders')}
MIGRATION_LOG = 'unifiedstorage_migration_log'
FOLDERS_DASHBOARDS = 'folders and dashboards migration'


def emit(payload, code):
    print(json.dumps(payload, sort_keys=True), flush=True)
    return code


def write(path, payload):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=1))
    os.replace(tmp, path)


def side_files():
    return {s: DB.with_name(DB.name + s).stat().st_size
            for s in ('-wal', '-shm', '-journal') if DB.with_name(DB.name + s).exists()}


def data_files():
    return {p.name: p.stat().st_size if p.is_file() else 'dir'
            for p in sorted(DATA.iterdir()) if p != EVIDENCE}


def plugin_ids():
    ids = []
    for plugin in sorted(p for p in (DATA / 'plugins').glob('*') if p.is_dir()):
        try:
            meta = json.loads((plugin / 'plugin.json').read_text())
            ids.append([plugin.name, meta.get('id'), meta.get('info', {}).get('version')])
        except (OSError, ValueError):
            ids.append([plugin.name, None, None])
    return ids


def values(con, sql, params=()):
    return sorted(row[0] if len(row) == 1 else list(row) for row in con.execute(sql, params))


def sha256(data):
    return hashlib.sha256(data if isinstance(data, bytes) else json.dumps(data, sort_keys=True).encode()).hexdigest()


def checksum(data, big, s0=0, s1=0):
    words = struct.unpack(('>' if big else '<') + f'{len(data) // 4}I', data)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & 0xffffffff
        s1 = (s1 + words[i + 1] + s0) & 0xffffffff
    return s0, s1


def wal_frames(page):
    # SQLite silently ignores a WAL it did not write, so integrity_check alone proves nothing about it.
    raw = DB.with_name(DB.name + '-wal').read_bytes()
    magic, version, size, _, salt1, salt2 = struct.unpack('>6I', raw[:24].ljust(24, b'\0'))
    if len(raw) < 32 or magic not in (0x377f0682, 0x377f0683) or version != 3007000 or size != page:
        return None, 'WAL header does not belong to this database'
    big, sums = magic & 1, checksum(raw[:24], magic & 1)
    if sums != struct.unpack('>2I', raw[24:32]):
        return None, 'WAL header checksum mismatch'
    committed = frame = 0
    for offset in range(32, len(raw) - 24 - page + 1, 24 + page):
        pgno, commit, s1, s2, c0, c1 = struct.unpack('>6I', raw[offset:offset + 24])
        sums = checksum(raw[offset:offset + 8] + raw[offset + 24:offset + 24 + page], big, *sums)
        # Stale frames from an earlier WAL generation legitimately trail the valid ones.
        if not pgno or (s1, s2) != (salt1, salt2) or sums != (c0, c1):
            break
        frame += 1
        committed = frame if commit else committed
    return committed, None


def state(con, migrated):
    names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    need = {'data_source', 'user'} | ({'resource', MIGRATION_LOG} if migrated else {'dashboard'})
    if need - names:
        raise sqlite3.DatabaseError(f'missing tables {sorted(need - names)}')
    found = {'datasources': values(con, 'SELECT org_id, uid, type, name, length(secure_json_data) > 2 FROM data_source'),
             'user_ids': values(con, 'SELECT id FROM "user" WHERE is_service_account = 0'),
             'org_ids': values(con, 'SELECT id FROM org') if 'org' in names else None}
    if MIGRATION_LOG in names:
        found['migrations_ok'] = values(con, f'SELECT migration_id FROM {MIGRATION_LOG} WHERE success')
        found['migrations_failed'] = con.execute(f'SELECT count(*) FROM {MIGRATION_LOG} WHERE NOT success').fetchone()[0]
    if migrated:
        for key, (group, resource) in UNIFIED.items():
            found[key] = values(con, 'SELECT namespace, name FROM resource WHERE "group" = ? AND resource = ?'
                                     ' AND action != 3', (group, resource))
        if 'resource_history' in names:
            # Tells a row the migration never wrote apart from one it wrote and something later removed.
            found['history_dashboards'] = values(con, 'SELECT DISTINCT namespace, name FROM resource_history'
                                                      ' WHERE "group" = ? AND resource = ?', UNIFIED['unified_dashboards'])
        return found
    provisioned = ('SELECT dashboard_id FROM dashboard_provisioning WHERE dashboard_id IS NOT NULL'
                   if 'dashboard_provisioning' in names else 'SELECT 0 WHERE 0')
    live = 'deleted IS NULL' if 'deleted' in {row[1] for row in con.execute('PRAGMA table_info(dashboard)')} else '1'
    found['dashboards'] = values(
        con, f'SELECT {ORG_NAMESPACE}, uid, id IN ({provisioned}) FROM dashboard WHERE is_folder = 0 AND {live}')
    found['folders'] = values(con, f'SELECT {ORG_NAMESPACE}, uid FROM dashboard WHERE is_folder = 1 AND {live}')
    found['dashboards_soft_deleted'] = con.execute(
        f'SELECT count(*) FROM dashboard WHERE is_folder = 0 AND NOT ({live})').fetchone()[0]
    return found


def stored_providers(con):
    """Every file-provider name the 12 database stored, with an org it provisioned into."""
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if 'dashboard_provisioning' not in tables:
        return []
    return values(con, "SELECT coalesce(p.name, ''), coalesce(min(d.org_id), 1) FROM dashboard_provisioning p"
                       ' LEFT JOIN dashboard d ON d.id = p.dashboard_id GROUP BY p.name')


def write_providers(providers):
    """v13.2.3 deletes, in every org, each provisioned dashboard whose provider name is not configured.
    Naming every stored provider keeps them; an absent path stops walkDisk at its os.Stat before any
    sync, and disableDeletion only unprovisions should a path ever appear empty."""
    if any(not name or '$' in name for name, _ in providers):
        return 'stored provider name cannot be configured verbatim'
    absent = PROVISIONING / 'absent'
    if absent.exists():
        return 'provider path placeholder already exists'
    entries = [{'name': name, 'type': 'file', 'orgId': org, 'disableDeletion': True,
                'options': {'path': str(absent / str(i))}} for i, (name, org) in enumerate(providers)]
    PROVIDERS.parent.mkdir(parents=True, exist_ok=True)
    PROVIDERS.write_text(json.dumps({'apiVersion': 1, 'providers': entries}, indent=1))
    return None


def counts(found):
    return {k: len(v) if isinstance(v, list) else v for k, v in found.items()}


def precheck():
    if PRE.exists():
        # Provisioning lives in a pod-scoped emptyDir, so a new pod must get the recorded providers back.
        providers = json.loads(PRE.read_text()).get('providers')
        reason = 'recorded state has no provider list' if providers is None else write_providers(providers)
        if reason:
            return emit({'precheck': 'failed', 'reason': reason}, 2)
        return emit({'precheck': 'skipped', 'reason': '12-schema state already recorded'}, 0)
    if not DB.is_file():
        return emit({'precheck': 'failed', 'reason': 'grafana.db missing'}, 2)
    before, files = side_files(), data_files()
    with DB.open('rb') as f:
        head = f.read(100)
    if len(head) < 100 or not head.startswith(b'SQLite format 3\0'):
        return emit({'precheck': 'failed', 'reason': 'not a SQLite database'}, 2)
    page, committed = int.from_bytes(head[16:18], 'big'), None
    page = 65536 if page == 1 else page
    if before.get('-wal'):
        if head[18] != 2:
            return emit({'precheck': 'failed', 'reason': 'WAL present but database is not in WAL mode'}, 2)
        committed, reason = wal_frames(page)
        if reason:
            return emit({'precheck': 'failed', 'reason': reason}, 2)
    try:
        con = sqlite3.connect(DB, isolation_level=None, timeout=30)
        con.execute('PRAGMA temp_store = MEMORY')
        mode = con.execute('PRAGMA journal_mode').fetchone()[0]
        if mode == 'wal':
            busy, log, done = con.execute('PRAGMA wal_checkpoint(FULL)').fetchone()
            if busy or log != done or (committed is not None and log != committed):
                return emit({'precheck': 'failed', 'reason': 'WAL not fully checkpointed',
                             'frames': [busy, log, done, committed]}, 2)
            con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        integrity = values(con, 'PRAGMA integrity_check')
        if integrity != ['ok']:
            return emit({'precheck': 'failed', 'reason': 'integrity_check', 'problems': len(integrity)}, 2)
        found = state(con, migrated=False)
        providers = stored_providers(con)
        con.close()
    except sqlite3.DatabaseError as exc:
        return emit({'precheck': 'failed', 'reason': f'{type(exc).__name__}: {exc}'}, 2)
    reason = write_providers(providers)
    if reason:
        return emit({'precheck': 'failed', 'reason': reason}, 2)
    record = {'recorded_at': time.time(), 'journal_mode': mode, 'wal_committed_frames': committed,
              'side_files_before': before, 'side_files_after': side_files(), 'db_bytes': DB.stat().st_size}
    plugins = plugin_ids()
    write(PRE, dict(record, files=files, plugins=plugins, providers=providers, state=found))
    return emit(dict(record, precheck='ok', counts=counts(found), plugins=len(plugins), providers=len(providers)), 0)


def wait():
    ready = EVIDENCE / 'ready.json'
    if not ready.exists():
        pre = json.loads(PRE.read_text())
        write(ready, {'seconds_precheck_to_ready': round(time.time() - pre['recorded_at'], 1)})
    print(ready.read_text(), flush=True)
    while not (EVIDENCE / 'finish').exists():
        time.sleep(5)
    # The Job's own exit code is the record, so it must not trust the marker alone.
    reason = verdict()
    return emit({'rehearsal': 'fail' if reason else 'pass', 'reason': reason}, 1 if reason else 0)


def evaluate(pre):
    con = sqlite3.connect(DB, isolation_level=None, timeout=30)
    try:
        con.execute('PRAGMA query_only = 1')
        con.execute('PRAGMA temp_store = MEMORY')
        post = state(con, migrated=True)
        integrity = values(con, 'PRAGMA integrity_check')
    finally:
        con.close()
    old = pre['state']
    kept = {tuple(row) for row in post['unified_dashboards']}
    lost = [row for row in old['dashboards'] if (row[0], row[1]) not in kept]
    checks = {
        'integrity': integrity == ['ok'],
        'datasources_unchanged': post['datasources'] == old['datasources'],
        'users_unchanged': post['user_ids'] == old['user_ids'],
        'orgs_unchanged': post['org_ids'] == old['org_ids'],
        'dashboards_kept': not lost,
        'folders_unchanged': post['unified_folders'] == old['folders'],
        'folders_dashboards_migrated': FOLDERS_DASHBOARDS in post['migrations_ok'],
        'plugins_unchanged': plugin_ids() == pre['plugins'],
    }
    return post, checks, lost


def postcheck():
    pre_raw = PRE.read_bytes()
    pre = json.loads(pre_raw)
    binding = {'pre_sha256': sha256(pre_raw), 'checker_sha256': sha256(Path(__file__).read_bytes())}
    try:
        post, checks, lost = evaluate(pre)
    except sqlite3.DatabaseError as exc:
        # A missing unified table fails here; legacy rows never stand in for migrated ones.
        record = {'postcheck': 'fail', 'reason': f'{type(exc).__name__}: {exc}'}
        write(POST, {**record, **binding, 'passed': False})  # supersedes any earlier pass
        return emit(record, 1)
    old = pre['state']
    history = {tuple(row) for row in post.get('history_dashboards', [])}
    record = {'checks': checks, 'counts_pre': counts(old), 'counts_post': counts(post),
              'lost_dashboards': {'user': sum(1 for row in lost if not row[2]),
                                  'provisioned': sum(1 for row in lost if row[2]),
                                  'seen_in_history': sum(1 for row in lost if (row[0], row[1]) in history)},
              'new_migrations': len(set(post['migrations_ok']) - set(old.get('migrations_ok', []))),
              'folders_dashboards_preexisting': FOLDERS_DASHBOARDS in old.get('migrations_ok', []),
              'new_files': {k: v for k, v in data_files().items() if k not in pre['files']},
              'db_bytes': DB.stat().st_size, 'side_files': side_files()}
    if (EVIDENCE / 'ready.json').exists():
        record.update(json.loads((EVIDENCE / 'ready.json').read_text()))
    passed = all(checks.values())
    write(POST, {**record, **binding, 'passed': passed, 'state': post, 'state_sha256': sha256(post)})
    return emit(dict(record, postcheck='pass' if passed else 'fail'), 0 if passed else 1)


def verdict():
    """Why the rehearsal may not be recorded as a success, or None once it may."""
    try:
        receipt, pre_raw = json.loads(POST.read_text()), PRE.read_bytes()
    except (OSError, ValueError):
        return 'no postcheck receipt'
    if receipt.get('passed') is not True:
        return 'last postcheck failed'
    bound = receipt.get('pre_sha256') == sha256(pre_raw)
    if not bound or receipt.get('checker_sha256') != sha256(Path(__file__).read_bytes()):
        return 'postcheck receipt belongs to another precheck or checker'
    try:
        post, checks, _ = evaluate(json.loads(pre_raw))
    except sqlite3.DatabaseError:
        return 'migrated state no longer readable'
    if not all(checks.values()) or sha256(post) != receipt.get('state_sha256'):
        return 'state changed since the passing postcheck'
    return None


def finish():
    # The marker always ends the hold; only a current, bound pass lets the Job succeed.
    reason = verdict()
    write(EVIDENCE / 'finish', {'finished_at': time.time(), 'passed': reason is None})
    return emit({'finish': 'fail' if reason else 'pass', 'reason': reason}, 1 if reason else 0)


if __name__ == '__main__':
    commands = {'precheck': precheck, 'wait': wait, 'postcheck': postcheck, 'finish': finish}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(f'usage: rehearsal.py {{{"|".join(commands)}}}')
    sys.exit(commands[sys.argv[1]]())
