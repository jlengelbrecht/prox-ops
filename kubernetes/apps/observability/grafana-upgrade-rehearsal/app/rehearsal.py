"""Counts-only SQLite evidence for the Grafana 13 rehearsal; never selects titles, JSON, secrets or hashes."""
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
# v13.2.3 resource_mig.go, resources.go, migration_registrar.go: org 1 is namespace "default", action 3 is a
# deletion marker, and folders/dashboards read as unified only once their MigrationID row logged success.
NAMESPACE = 'default'
UNIFIED = {'unified_dashboard_uids': ('dashboard.grafana.app', 'dashboards'),
           'unified_folder_uids': ('folder.grafana.app', 'folders')}
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
    found = {'datasources': values(con, 'SELECT uid, type, name, length(secure_json_data) > 2 FROM data_source'),
             'user_ids': values(con, 'SELECT id FROM "user" WHERE is_service_account = 0')}
    if MIGRATION_LOG in names:
        found['migrations_ok'] = values(con, f'SELECT migration_id FROM {MIGRATION_LOG} WHERE success')
        found['migrations_failed'] = con.execute(f'SELECT count(*) FROM {MIGRATION_LOG} WHERE NOT success').fetchone()[0]
    if migrated:
        for key, (group, resource) in UNIFIED.items():
            found[key] = values(con, 'SELECT name FROM resource WHERE namespace = ? AND "group" = ? AND resource = ?'
                                     ' AND action != 3', (NAMESPACE, group, resource))
        return found
    provisioned = ('SELECT dashboard_id FROM dashboard_provisioning WHERE dashboard_id IS NOT NULL'
                   if 'dashboard_provisioning' in names else 'SELECT 0 WHERE 0')
    found['dashboard_uids_user'] = values(
        con, f'SELECT uid FROM dashboard WHERE org_id = 1 AND is_folder = 0 AND id NOT IN ({provisioned})')
    found['dashboards_total'] = con.execute(
        'SELECT count(*) FROM dashboard WHERE org_id = 1 AND is_folder = 0').fetchone()[0]
    found['folder_uids'] = values(con, 'SELECT uid FROM dashboard WHERE org_id = 1 AND is_folder = 1')
    found['org_count'] = con.execute('SELECT count(*) FROM org').fetchone()[0] if 'org' in names else None
    return found


def counts(found):
    return {k: len(v) if isinstance(v, list) else v for k, v in found.items()}


def precheck():
    if (EVIDENCE / 'pre.json').exists():
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
        con.close()
    except sqlite3.DatabaseError as exc:
        return emit({'precheck': 'failed', 'reason': f'{type(exc).__name__}: {exc}'}, 2)
    record = {'recorded_at': time.time(), 'journal_mode': mode, 'wal_committed_frames': committed,
              'side_files_before': before, 'side_files_after': side_files(), 'db_bytes': DB.stat().st_size}
    plugins = plugin_ids()
    write(EVIDENCE / 'pre.json', dict(record, files=files, plugins=plugins, state=found))
    return emit(dict(record, precheck='ok', counts=counts(found), plugins=len(plugins)), 0)


def wait():
    ready = EVIDENCE / 'ready.json'
    if not ready.exists():
        pre = json.loads((EVIDENCE / 'pre.json').read_text())
        write(ready, {'seconds_precheck_to_ready': round(time.time() - pre['recorded_at'], 1)})
    print(ready.read_text(), flush=True)
    while not (EVIDENCE / 'finish').exists():
        time.sleep(5)
    return 0


def postcheck():
    pre = json.loads((EVIDENCE / 'pre.json').read_text())
    try:
        con = sqlite3.connect(DB, isolation_level=None, timeout=30)
        con.execute('PRAGMA query_only = 1')
        con.execute('PRAGMA temp_store = MEMORY')
        post = state(con, migrated=True)
        integrity = values(con, 'PRAGMA integrity_check')
        con.close()
    except sqlite3.DatabaseError as exc:
        # A missing unified table fails here; legacy rows never stand in for migrated ones.
        return emit({'postcheck': 'fail', 'reason': f'{type(exc).__name__}: {exc}'}, 1)
    old = pre['state']
    checks = {
        'integrity': integrity == ['ok'],
        'datasources_unchanged': post['datasources'] == old['datasources'],
        'users_unchanged': post['user_ids'] == old['user_ids'],
        'user_dashboards_kept': set(old['dashboard_uids_user']) <= set(post['unified_dashboard_uids']),
        'folders_unchanged': post['unified_folder_uids'] == old['folder_uids'],
        'folders_dashboards_migrated': FOLDERS_DASHBOARDS in post['migrations_ok'],
        'plugins_unchanged': plugin_ids() == pre['plugins'],
    }
    record = {'checks': checks, 'counts_pre': counts(old), 'counts_post': counts(post),
              'new_migrations': len(set(post['migrations_ok']) - set(old.get('migrations_ok', []))),
              'folders_dashboards_preexisting': FOLDERS_DASHBOARDS in old.get('migrations_ok', []),
              'provisioned_delta': len(post['unified_dashboard_uids']) - old['dashboards_total'],
              'new_files': {k: v for k, v in data_files().items() if k not in pre['files']},
              'db_bytes': DB.stat().st_size, 'side_files': side_files()}
    if (EVIDENCE / 'ready.json').exists():
        record.update(json.loads((EVIDENCE / 'ready.json').read_text()))
    write(EVIDENCE / 'post.json', dict(record, state=post))
    passed = all(checks.values())
    return emit(dict(record, postcheck='pass' if passed else 'fail'), 0 if passed else 1)


if __name__ == '__main__':
    commands = {'precheck': precheck, 'wait': wait, 'postcheck': postcheck,
                'finish': lambda: write(EVIDENCE / 'finish', {'finished_at': time.time()}) or 0}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(f'usage: rehearsal.py {{{"|".join(commands)}}}')
    sys.exit(commands[sys.argv[1]]())
