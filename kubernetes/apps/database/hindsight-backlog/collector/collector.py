"""Contract core of the read-only collector for the Hindsight runnable-backlog aggregate.

A poll feeds POLL_SCRIPT to one fresh psql session: a budgeted READ ONLY transaction that prechecks its
own identity, cap, TLS and budgets from the catalog, then calls hindsight_metrics.runnable_backlog().
This module holds the pure parts of a poll. Every failure is a fixed result class meaning unknown, never
zero. It has no process runner, schedule, HTTP server or signal handling, so it spawns, listens and
starts nothing. Written to the Python 3.9 standard library of the pinned CloudNativePG image; nothing
deploys it yet.
"""
import collections
import datetime
import hashlib
import json
import math
import os
import re
import stat

READER_ROLE = 'hindsight_backlog_metrics'
CREDENTIALS = ('/secrets/username', '/secrets/password')
HOLD = 180.0
MAX_AGE = 30.0
MAX_SKEW = 5.0
MAX_SECRET = 1024
MAX_ITEMS = 256
MAX_BANK = 256
MAX_COUNT = 10 ** 12

CLASSES = ('ok', 'capacity_exceeded', 'credentials', 'precheck_cap', 'precheck_temp', 'precheck_privilege',
           'precheck_tls', 'precheck_budget', 'precheck_placement', 'hsb01', 'statement_cancel', 'permission',
           'auth', 'tls', 'connect', 'other_sqlstate', 'deadline', 'oversized', 'malformed', 'stale_or_skewed')
SQLSTATES = {'HSC01': 'precheck_cap', 'HSC02': 'precheck_temp', 'HSC03': 'precheck_privilege',
             'HSC04': 'precheck_tls', 'HSC05': 'precheck_budget', 'HSC06': 'precheck_placement',
             'HSB01': 'hsb01', '53400': 'hsb01', '55P03': 'hsb01', '57014': 'statement_cancel',
             '42501': 'permission', '42883': 'permission', '3F000': 'permission',
             '28P01': 'auth', '28000': 'auth', '53300': 'connect'}
# libpq connection failures carry no SQLSTATE; LC_ALL=C keeps these phrases untranslated.
AUTH_PHRASES = ('password authentication failed', 'scram', 'no password supplied')
TLS_PHRASES = ('ssl', 'certificate')

COUNTS = ('pending', 'payload_null_pending', 'deferred', 'due', 'runnable', 'serialization_blocked',
          'processing', 'assigned_pending', 'assigned_runnable', 'future_created_runnable')
AGE, LATENESS = 'runnable_oldest_task_age_seconds', 'runnable_oldest_retry_lateness_seconds'
TOP_KEYS = frozenset(('capacity_exceeded', 'observed_at', 'banks', 'groups'))
BANK_KEYS = frozenset(('bank', 'registered'))
GROUP_KEYS = frozenset(('bank', 'operation_type', AGE, LATENESS) + COUNTS)
# The rendering of the committed function's observed_at is assumed, not yet
# proven against a live server; a different rendering is malformed, never loosened.
OBSERVED_AT = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00')
OPERATION = re.compile(r'[a-z][a-z0-9_]{0,63}')
SAFE_BANK = re.compile(r'[A-Za-z0-9._:@-]{1,128}')
SQLSTATE = re.compile(r'\b(?:ERROR|FATAL):\s+([0-9A-Z]{5})[ ]*$', re.M)

# The fixed environment for psql. Nothing is inherited, so PGOPTIONS, PGSERVICE,
# PGPASSFILE, HOME and any other PG* variable cannot reach libpq. The PostgreSQL
# bin directory comes first so the real psql runs, not the Debian wrapper.
CHILD_ENV = {
    'PATH': '/usr/lib/postgresql/16/bin:/usr/bin:/bin', 'LC_ALL': 'C',
    'PGHOST': 'postgres-rw.database.svc.cluster.local', 'PGPORT': '5432', 'PGDATABASE': 'hindsight',
    'PGSSLMODE': 'verify-full', 'PGSSLROOTCERT': '/tls/ca.crt', 'PGCONNECT_TIMEOUT': '10',
    'PGAPPNAME': 'hindsight-backlog-collector', 'PGTARGETSESSIONATTRS': 'read-write',
}
PSQL_ARGV = ('psql', '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=sqlstate', '-f', '-')

# The budgets are re-issued in every transaction and verified by HSC05, so no
# role default, RESET or inherited option can loosen a poll. The precheck reads
# only the catalog; ON_ERROR_STOP keeps the function from running after a failure.
POLL_SCRIPT = b"""BEGIN READ ONLY;
SET LOCAL search_path = pg_catalog, pg_temp;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '1s';
SET LOCAL max_parallel_workers_per_gather = 0;
SET LOCAL idle_in_transaction_session_timeout = '10s';
DO $pre$
BEGIN
 IF current_setting('temp_file_limit') <> '128MB'
    OR (SELECT source FROM pg_settings WHERE name = 'temp_file_limit') IS DISTINCT FROM 'user' THEN
  RAISE EXCEPTION USING ERRCODE = 'HSC01', MESSAGE = 'collector precheck';
 END IF;
 IF has_database_privilege(current_user, current_database(), 'TEMPORARY') THEN
  RAISE EXCEPTION USING ERRCODE = 'HSC02', MESSAGE = 'collector precheck';
 END IF;
 IF current_user <> 'hindsight_backlog_metrics' OR session_user <> current_user
    OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolconnlimit = 2 AND NOT rolsuper
                   AND NOT rolinherit AND NOT rolcreaterole AND NOT rolcreatedb AND NOT rolreplication
                   AND NOT rolbypassrls)
    OR EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid IN (m.member, m.roleid)
               WHERE r.rolname = current_user)
    OR has_parameter_privilege(current_user, 'temp_file_limit', 'SET')
    OR NOT has_function_privilege(current_user, 'hindsight_metrics.runnable_backlog()', 'EXECUTE')
    OR to_regclass('public.async_operations') IS NULL OR to_regclass('public.banks') IS NULL
    OR has_any_column_privilege(current_user, to_regclass('public.async_operations'), 'SELECT')
    OR has_any_column_privilege(current_user, to_regclass('public.banks'), 'SELECT') THEN
  RAISE EXCEPTION USING ERRCODE = 'HSC03', MESSAGE = 'collector precheck';
 END IF;
 IF NOT COALESCE((SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()), false) THEN
  RAISE EXCEPTION USING ERRCODE = 'HSC04', MESSAGE = 'collector precheck';
 END IF;
 IF current_setting('statement_timeout') <> '5s' OR current_setting('lock_timeout') <> '1s'
    OR current_setting('max_parallel_workers_per_gather') <> '0'
    OR current_setting('transaction_read_only') <> 'on' THEN
  RAISE EXCEPTION USING ERRCODE = 'HSC05', MESSAGE = 'collector precheck';
 END IF;
 IF current_database() <> 'hindsight' OR pg_is_in_recovery() THEN
  RAISE EXCEPTION USING ERRCODE = 'HSC06', MESSAGE = 'collector precheck';
 END IF;
END
$pre$;
SELECT hindsight_metrics.runnable_backlog();
COMMIT;
"""

# One finished psql run. returncode is None when the child never ran or was
# killed; timed_out and oversized mark a run whose output was discarded.
RunResult = collections.namedtuple('RunResult', 'returncode stdout stderr timed_out oversized')
Sample = collections.namedtuple('Sample', 'observed banks groups labels')
# Sample health as the poller records it. results counts polls by class; the
# timestamps are None until set, so a first boot exports no phantom zero.
Status = collections.namedtuple('Status', 'version results valid capacity last_success observed')


class Snapshot(collections.namedtuple('Snapshot', 'with_data without_data expires')):
    """Immutable pre-rendered exposition; the data variant stops being served at its monotonic expiry."""
    __slots__ = ()

    def body(self, now):
        return self.with_data if self.with_data is not None and now < self.expires else self.without_data


class Unknown(Exception):
    """A poll outcome that means unknown; it carries only the fixed result class."""

    def __init__(self, result):
        super().__init__(result)
        self.result = result


def first_boot(version):
    return Status(version, dict.fromkeys(CLASSES, 0), False, False, None, None)


def _read_secret(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise Unknown('credentials') from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise Unknown('credentials')
        data = b''
        while len(data) <= MAX_SECRET:
            chunk = os.read(descriptor, MAX_SECRET + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        raise Unknown('credentials') from None
    finally:
        os.close(descriptor)
    if len(data) > MAX_SECRET:
        raise Unknown('credentials')
    try:
        text = (data[:-1] if data.endswith(b'\n') else data).decode('utf-8')
    except UnicodeDecodeError:
        raise Unknown('credentials') from None
    if not text or any(ord(char) < 0x20 or ord(char) == 0x7f for char in text):
        raise Unknown('credentials')
    return text


def read_credentials(paths=CREDENTIALS):
    """Read the username and password files fresh for one poll; any doubt is the credentials class."""
    username, password = (_read_secret(path) for path in paths)
    if username != READER_ROLE:
        raise Unknown('credentials')
    return username, password


def child_env(username, password):
    return dict(CHILD_ENV, PGUSER=username, PGPASSWORD=password)


def classify(result):
    """The failure class of a finished run, or None when psql exited 0 and its output still needs parsing."""
    if result.timed_out:
        return 'deadline'
    if result.oversized:
        return 'oversized'
    if result.returncode == 0:
        return None
    stderr = result.stderr.decode('ascii', 'replace')
    code = SQLSTATE.search(stderr)
    if code:
        return SQLSTATES.get(code.group(1), 'other_sqlstate')
    if result.returncode == 2:
        lowered = stderr.lower()
        if any(phrase in lowered for phrase in AUTH_PHRASES):
            return 'auth'
        return 'tls' if any(phrase in lowered for phrase in TLS_PHRASES) else 'connect'
    # A child that never ran, or psql's own exit 1, has no closer class in the fixed enum.
    return 'other_sqlstate' if result.returncode == 3 else 'connect'


def _require(condition):
    if not condition:
        raise ValueError


def _unique_object(pairs):
    _require(len({key for key, _ in pairs}) == len(pairs))
    return dict(pairs)


def _reject_constant(name):
    raise ValueError(name)


def _count(value):
    _require(type(value) is int and 0 <= value <= MAX_COUNT)
    return value


def _age(value):
    if value is None:
        return None
    _require(type(value) in (int, float) and math.isfinite(value) and value >= 0)
    return value


def bank_label(bank):
    if SAFE_BANK.fullmatch(bank):
        return bank
    return 'h:' + hashlib.sha256(bank.encode('utf-8')).hexdigest()[:16]


def _validate(doc):
    _require(type(doc) is dict and set(doc) == TOP_KEYS and doc['capacity_exceeded'] is False)
    observed, banks, groups = doc['observed_at'], doc['banks'], doc['groups']
    _require(type(observed) is str and OBSERVED_AT.fullmatch(observed))
    observed = datetime.datetime.fromisoformat(observed)
    _require(observed.utcoffset() == datetime.timedelta(0))
    _require(type(banks) is list and type(groups) is list and len(banks) <= MAX_ITEMS and len(groups) <= MAX_ITEMS)
    registered = {}
    for item in banks:
        _require(type(item) is dict and set(item) == BANK_KEYS and type(item['registered']) is bool)
        bank = item['bank']
        _require(type(bank) is str and 1 <= len(bank) <= MAX_BANK and bank not in registered)
        registered[bank] = item['registered']
    seen, parsed = set(), []
    for item in groups:
        _require(type(item) is dict and set(item) == GROUP_KEYS)
        key = (item['bank'], item['operation_type'])
        _require(type(key[0]) is str and key[0] in registered and type(key[1]) is str
                 and OPERATION.fullmatch(key[1]) and key not in seen)
        seen.add(key)
        c = {name: _count(item[name]) for name in COUNTS}
        age, lateness = _age(item[AGE]), _age(item[LATENESS])
        # Invariants of the SQL predicates: the three pending states partition pending,
        # runnable is the unblocked part of due, and the ages exist only for runnable rows.
        # Lateness is a nullable minimum, so runnable rows without a retry time leave it null.
        _require(c['pending'] == c['payload_null_pending'] + c['deferred'] + c['due']
                 and c['runnable'] <= c['due'] and c['serialization_blocked'] == c['due'] - c['runnable']
                 and c['assigned_pending'] <= c['pending'] and c['assigned_runnable'] <= c['runnable']
                 and c['future_created_runnable'] <= c['runnable'] and c['pending'] + c['processing'] >= 1
                 and (c['runnable'] == 0) == (age is None) and (c['runnable'] > 0 or lateness is None))
        parsed.append((key[0], key[1], c, age, lateness))
    labels = {bank: bank_label(bank) for bank in registered}
    _require(len(set(labels.values())) == len(labels))
    return Sample(observed.timestamp(), registered, tuple(parsed), labels)


def parse_sample(stdout, now):
    """Validate one psql result against the aggregate contract; any doubt discards the whole sample."""
    try:
        text = stdout.decode('utf-8')
        _require(text.endswith('\n') and '\n' not in text[:-1] and '\r' not in text)
        doc = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise Unknown('malformed') from None
    if type(doc) is dict and set(doc) == {'capacity_exceeded'} and doc['capacity_exceeded'] is True:
        raise Unknown('capacity_exceeded')
    try:
        sample = _validate(doc)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise Unknown('malformed') from None
    if now - sample.observed > MAX_AGE or sample.observed - now > MAX_SKEW:
        raise Unknown('stale_or_skewed')
    return sample


def hold_expiry(sample, received, received_mono):
    """Monotonic instant a sample stops being served, HOLD seconds after its database observed_at.

    Fixing it at receipt on the monotonic clock means a later wall-clock step cannot extend the hold."""
    return received_mono + HOLD - (received - sample.observed)


def _number(value):
    if type(value) is int:
        return str(value)
    _require(math.isfinite(value))
    return repr(float(value))


def _escape(text, quote=True):
    text = text.replace('\\', '\\\\').replace('\n', '\\n')
    return text.replace('"', '\\"') if quote else text


def _labels(labels):
    return '{%s}' % ','.join('%s="%s"' % (name, _escape(value)) for name, value in labels) if labels else ''


def render(status, sample=None):
    """Prometheus text exposition of the poll status and, when given, one held sample."""
    lines = []

    def family(name, kind, text, samples):
        if samples:
            name = 'hindsight_backlog_' + name
            lines.append('# HELP %s %s\n# TYPE %s %s\n' % (name, _escape(text, quote=False), name, kind))
            lines.extend('%s%s%s %s\n' % (name, suffix, _labels(labels), _number(value))
                         for suffix, labels, value in samples)

    def gauge(name, text, value):
        family(name, 'gauge', text, [] if value is None else [('', (), value)])

    family('collector_info', 'gauge', 'Collector build; version is a sha256 prefix of collector.py.',
           [('', (('version', status.version),), 1)])
    gauge('sample_valid', '1 only when the latest poll returned a valid sample.', int(status.valid))
    gauge('capacity_exceeded', '1 when the latest poll reported more than 256 groups or banks.',
          int(status.capacity))
    family('polls_total', 'counter', 'Polls by result class; every class except ok means unknown.',
           [('', (('result', name),), status.results[name]) for name in CLASSES])
    gauge('last_success_timestamp_seconds', 'Collector clock when the latest valid sample arrived.',
          status.last_success)
    gauge('observed_timestamp_seconds', 'Database observed_at of the latest valid sample; gate data on it.',
          status.observed)
    if sample is not None:
        _render_data(sample, family, gauge)
    return ''.join(lines).encode('utf-8')


def _render_data(sample, family, gauge):
    tasks, ages, lateness = [], [], []
    runnable, active = dict.fromkeys(sample.banks, 0), dict.fromkeys(sample.banks, 0)
    for bank, operation, counts, age, late in sorted(sample.groups, key=lambda g: (sample.labels[g[0]], g[1])):
        labels = (('bank_id', sample.labels[bank]), ('operation_type', operation))
        tasks.extend(('', labels + (('state', state),), counts[state]) for state in COUNTS)
        if age is not None:
            ages.append(('', labels, age))
        if late is not None:
            lateness.append(('', labels, late))
        runnable[bank] += counts['runnable']
        active[bank] += counts['pending'] + counts['processing']
    banks = sorted(sample.banks, key=sample.labels.get)
    family('tasks', 'gauge', 'Pending and processing operations by bank, operation type and state.', tasks)
    family(AGE, 'gauge', 'Age of the oldest runnable operation.', ages)
    family(LATENESS, 'gauge', 'Lateness of the oldest runnable retry.', lateness)
    for name, text, values in (
            ('bank_runnable', 'Runnable operations per bank; 0 is a known zero.', runnable),
            ('bank_active', 'Pending plus processing operations per bank; 0 is a known zero.', active),
            ('bank_registered', '1 when the bank is listed in public.banks.',
             {bank: int(sample.banks[bank]) for bank in banks})):
        family(name, 'gauge', text, [('', (('bank_id', sample.labels[bank]),), values[bank]) for bank in banks])
    gauge('runnable', 'Runnable operations across all banks.', sum(runnable.values()))
    gauge('active', 'Pending plus processing operations across all banks.', sum(active.values()))
    gauge('groups', 'Bank and operation type groups in the sample.', len(sample.groups))
    gauge('banks', 'Banks in the sample.', len(sample.banks))


def publish(status, sample=None, expires=0.0):
    """Render both variants once, so a scrape only selects bytes and never sees a partial update."""
    # Withdrawn data is never valid, so a stalled poller cannot hold sample_valid at 1 past expiry.
    without = render(status if sample is None else status._replace(valid=False))
    if sample is None:
        return Snapshot(None, without, 0.0)
    return Snapshot(render(status, sample), without, expires)
