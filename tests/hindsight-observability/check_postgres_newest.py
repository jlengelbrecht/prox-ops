#!/usr/bin/env python3
"""Native PostgreSQL SELECT-only proof of the actual newest-cohort query."""
import importlib.util
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('baseline', ROOT / 'scripts/hindsight-timing-baseline.py')
BASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)

start, end = '2026-10-03T09:00:00+00:00', '2026-10-03T09:30:00+00:00'
rows = []
sequence = 0
for bank in ('devb0x', 'obsidian'):
    for kind in ('retain', 'recall'):
        for minute in (10, 20, 25):
            sequence += 1
            rows.append(f"(UUID '00000000-0000-0000-0000-{sequence:012d}','{bank}','{kind}','completed',TIMESTAMPTZ '2026-10-03 09:{minute-2:02d}+00',TIMESTAMPTZ '2026-10-03 09:{minute-1:02d}+00',TIMESTAMPTZ '2026-10-03 09:{minute:02d}+00',0)")
rows += [
    "(UUID 'ffffffff-ffff-ffff-ffff-ffffffffffff','devb0x','retain','completed',TIMESTAMPTZ '2026-10-03 09:15+00',TIMESTAMPTZ '2026-10-03 09:18+00',TIMESTAMPTZ '2026-10-03 09:20+00',0)",
    "(UUID '00000000-0000-0000-0000-000000000000','devb0x','retain','completed',TIMESTAMPTZ '2026-10-03 08:57+00',TIMESTAMPTZ '2026-10-03 08:58+00',TIMESTAMPTZ '2026-10-03 08:59+00',0)",
    "(UUID '00000000-0000-0000-0000-000000000000','devb0x','batch_retain','completed',TIMESTAMPTZ '2026-10-03 09:23+00',TIMESTAMPTZ '2026-10-03 09:24+00',TIMESTAMPTZ '2026-10-03 09:25+00',0)",
    "(UUID '00000000-0000-0000-0000-000000000000','obsidian','retain','completed',TIMESTAMPTZ '2026-10-03 09:23+00',TIMESTAMPTZ '2026-10-03 09:24+00',TIMESTAMPTZ '2026-10-03 09:25+00',1)",
]
fixture = 'WITH fixture(operation_id,bank_id,operation_type,status,created_at,claimed_at,completed_at,retry_count) AS (VALUES ' + ','.join(rows) + '), eligible AS MATERIALIZED ('
query = BASE.SQL.replace('WITH eligible AS MATERIALIZED (', fixture, 1).replace('FROM public.async_operations', 'FROM fixture', 1)
assert 'FROM public.async_operations' not in query
sql = 'BEGIN TRANSACTION READ ONLY; SET LOCAL statement_timeout = 15000; ' + query.format(start=start, end=end, limit=2) + ' COMMIT;'
result = subprocess.run(['psql', '-X', '-q', '-A', '-t', '-w', '-v', 'ON_ERROR_STOP=1', '-c', sql],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        text=True, timeout=25, check=False)
if result.returncode:
    raise SystemExit('native PostgreSQL fixture unavailable or failed')
items = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
assert len(items) == 8, 'newest cohort size mismatch'
for bank in ('devb0x', 'obsidian'):
    for kind in ('retain', 'recall'):
        group = [item for item in items if item['bank_id'] == bank and item['operation_type'] == kind]
        assert [item['completed_at'][:16] for item in group] == ['2026-10-03T09:25', '2026-10-03T09:20'], 'newest order mismatch'
        assert [item['eligible_count'] for item in group] == ([4, 4] if (bank, kind) == ('devb0x', 'retain') else [3, 3]), 'eligible count mismatch'
        assert all('operation_id' not in item for item in group), 'operation ID leaked'
        if (bank, kind) == ('devb0x', 'retain'):
            assert float(group[1]['queue_seconds']) == 180, 'tied cutoff chose wrong operation'
print('native PostgreSQL newest cohort fixture PASS')
