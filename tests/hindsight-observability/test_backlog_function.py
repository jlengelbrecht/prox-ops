"""The backlog function DDL is a deterministic, guarded wrapper around the canonical CLI query, bound to the pinned API."""
import contextlib
import importlib.util
import io
import pathlib
import re
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load('backlog_function', 'scripts/hindsight-backlog-function.py')
TARGET = 'kubernetes/apps/database/hindsight-backlog/app/runnable-backlog.sql'


def drifted(old=None, new=None, **attributes):
    """A fresh canonical module with SQL_BODY's first `old` replaced and attributes overridden."""
    module = MODULE.load_canonical()
    if old is not None:
        module.SQL_BODY = module.SQL_BODY.replace(old, new, 1)
    for name, value in attributes.items():
        setattr(module, name, value)
    return module


def function_body(ddl):
    return ddl.split('AS $function$\n', 1)[1].split('$function$;', 1)[0]


def run_main(argv, **patches):
    with contextlib.ExitStack() as stack:
        error = stack.enter_context(mock.patch('sys.stderr', new_callable=io.StringIO))
        for name, value in patches.items():
            stack.enter_context(mock.patch.object(MODULE, name, value))
        try:
            code = MODULE.main(argv)
        except SystemExit as exit_:
            code = exit_.code
    return code, error.getvalue()


class RenderTests(unittest.TestCase):
    def test_committed_ddl_is_the_exact_render_of_the_canonical_query(self):
        ddl = MODULE.render()
        self.assertEqual(ddl, MODULE.render())
        self.assertEqual((ROOT / TARGET).read_bytes(), ddl.encode())
        self.assertIsNone(MODULE.check(ROOT / TARGET))
        query = MODULE.load_canonical().build_sql(256)[:-1]
        self.assertEqual(function_body(ddl).split(' RETURN (\n', 1)[1].split('\n );\n', 1)[0], query)
        self.assertEqual(ddl.count(query), 1)
        self.assertIn('> 256 OR (SELECT count(*) FROM listed) > 256', query)

    def test_canonical_drift_or_hand_edit_fails_check(self):
        with mock.patch.object(MODULE, 'load_canonical', return_value=drifted("'graph_maintenance', ", '')):
            self.assertEqual(MODULE.check(ROOT / TARGET), 'generated DDL is stale or hand-edited; run --write')
        with tempfile.TemporaryDirectory() as tmp:
            edited = pathlib.Path(tmp) / 'edited.sql'
            edited.write_text(MODULE.render().replace("'HSB01'", "'P0001'", 1))
            self.assertIsNotNone(MODULE.check(edited))

    def test_rebound_unembeddable_or_side_effecting_canonical_query_is_refused(self):
        cases = [drifted(DEFAULT_MAX_GROUPS=512), drifted('::jsonb;', '::jsonb; SELECT 1;'),
                 drifted('FROM ops o', 'FROM ops o, $x$y$x$ q'), drifted('FROM ops o', 'FROM ops o FOR UPDATE'),
                 drifted('FROM ops o', 'FROM ops o FOR SHARE'), drifted('now()', "set_config('a', 'b', true)"),
                 drifted('now()', 'pg_sleep(1)'), drifted('now()', "pg_notify('a', 'b')::text")]
        for module in cases:
            with self.subTest(), self.assertRaises(MODULE.RenderError):
                MODULE.render(module)

    def test_function_takes_no_input_runs_static_sql_and_fixes_its_header(self):
        ddl = MODULE.render()
        self.assertEqual(re.findall(r'runnable_backlog\([^)]*\)', ddl),
                         ['runnable_backlog()'] * ddl.count('runnable_backlog('))
        header = ddl.split('AS $function$', 1)[0].split('CREATE OR REPLACE FUNCTION ', 1)[1]
        self.assertEqual(header, 'hindsight_metrics.runnable_backlog()\n RETURNS jsonb\n LANGUAGE plpgsql\n'
                                 ' STABLE\n SECURITY DEFINER\n SET search_path = pg_catalog, pg_temp\n')
        lower = function_body(ddl).lower()
        for dynamic in ('execute', 'format(', 'quote_', 'sqlerrm', 'sqlstate', 'get stacked', 'declare', '$1'):
            self.assertNotIn(dynamic, lower)
        self.assertEqual(lower.count("errcode = 'hsb01', message = 'hindsight backlog unknown'"), 2)
        self.assertIn('exception when others then', lower)
        unqualified = re.findall(r'\bfrom\s+(?!public\.|ops\b|bank_ids\b|active\b|grouped\b|listed\b|'
                                 r'pg_class\b|pg_attribute\b|epoch\b|now\(\))(\w+)', lower)
        self.assertEqual(unqualified, [])

    def test_ddl_is_one_guarded_transaction_with_exact_grants(self):
        ddl = MODULE.render()
        top = [line for line in ddl.splitlines()
               if line.startswith(('BEGIN;', 'SET LOCAL', 'DO $', 'CREATE', 'REVOKE', 'GRANT', 'COMMIT;'))]
        self.assertEqual(top, [
            'BEGIN;', 'SET LOCAL search_path = pg_catalog, pg_temp;', "SET LOCAL lock_timeout = '5s';",
            "SET LOCAL statement_timeout = '30s';", 'DO $guard$',
            'CREATE SCHEMA IF NOT EXISTS hindsight_metrics AUTHORIZATION hindsight;',
            'CREATE OR REPLACE FUNCTION hindsight_metrics.runnable_backlog()',
            'REVOKE ALL ON SCHEMA hindsight_metrics FROM PUBLIC;',
            'REVOKE ALL ON FUNCTION hindsight_metrics.runnable_backlog() FROM PUBLIC;',
            'GRANT USAGE ON SCHEMA hindsight_metrics TO hindsight_backlog_metrics;',
            'GRANT EXECUTE ON FUNCTION hindsight_metrics.runnable_backlog() TO hindsight_backlog_metrics;',
            'DO $verify$', 'COMMIT;'])
        guard = ddl.split('DO $guard$', 1)[1].split('$guard$;', 1)[0]
        verify = ddl.split('DO $verify$', 1)[1].split('$verify$;', 1)[0]
        # Membership is refused in both directions: a member of the reader would inherit EXECUTE.
        for refusal in ("current_user <> 'hindsight'", 'NOT rolcreaterole', 'r.oid IN (m.member, m.roleid)',
                        "nspowner <> 'hindsight'::regrole", 'pg_type t', "p.proowner <> 'hindsight'::regrole"):
            self.assertIn(refusal, guard)
        for attribute in ("'hindsight_backlog_metrics=U/hindsight']", "'hindsight_backlog_metrics=X/hindsight']",
                          'p.prosecdef', "ARRAY['search_path=pg_catalog, pg_temp']", 'pg_depend'):
            self.assertIn(attribute, verify)
        for absent in ('create role', 'alter role', 'password', 'create table', 'owner to', 'grant select',
                       'grant hindsight', 'grant all', 'to public', 'create extension'):
            self.assertNotIn(absent, ddl.lower())

    def test_schema_guard_covers_every_column_the_canonical_query_reads(self):
        sources = MODULE.load_canonical().SQL_SOURCES
        selected = sources.split('SELECT', 1)[1].split('FROM public.async_operations', 1)[0]
        self.assertEqual(tuple(name.strip() for name in selected.split(',')), MODULE.REQUIRED_COLUMNS)
        self.assertIn('SELECT bank_id FROM public.banks', sources)
        body = function_body(MODULE.render())
        self.assertIn('ANY (ARRAY[' + ', '.join(f"'{c}'" for c in MODULE.REQUIRED_COLUMNS) + ']::name[])) <> 9', body)
        self.assertIn("version_num = 'd1e2f3a4b5c6'", body)
        self.assertIn('(SELECT count(*) FROM public.alembic_version) <> 1', body)
        self.assertLess(body.index('THEN\n  RAISE'), body.index('RETURN ('))


class VersionBindingTests(unittest.TestCase):
    MANIFEST = (ROOT / 'kubernetes/apps/ai/hermes-hindsight/app/hindsight.yaml').read_text()

    def test_supported_image_is_the_pinned_api_image_field(self):
        self.assertEqual(MODULE.manifest_api_image(self.MANIFEST), MODULE.SUPPORTED_API_IMAGE)
        self.assertIn(MODULE.SUPPORTED_API_IMAGE, MODULE.render())
        without_api = self.MANIFEST.replace('repository: ghcr.io/vectorize-io/hindsight-api\n', '', 1)
        self.assertIsNone(MODULE.manifest_api_image(without_api), 'control-plane image mistaken for the api')

    def test_any_bump_of_the_api_image_field_fails_check_and_write(self):
        tag = MODULE.SUPPORTED_API_IMAGE.split(':', 1)[1]
        text = self.MANIFEST
        self.assertEqual(text.count(tag), 1)
        bumps = {
            'tag': text.replace(tag, tag.replace('0.9.2@', '0.9.3@')),
            'digest': text.replace(tag, tag[:-4] + '0000'),
            'repository': text.replace('repository: ghcr.io/vectorize-io/hindsight-api\n',
                                       'repository: ghcr.io/example/hindsight-api\n'),
            # The old tag surviving only in a comment must not satisfy the binding.
            'tag moved to comment': text.replace(f'tag: "{tag}"', f'# tag: "{tag}"'),
            'second api tag': text.replace(f'tag: "{tag}"', f'tag: "{tag}"\n        tag: "{tag}"'),
        }
        for label, bumped in bumps.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                manifest, out = pathlib.Path(tmp) / 'hindsight.yaml', pathlib.Path(tmp) / 'out.sql'
                manifest.write_text(bumped)
                with mock.patch.object(MODULE, 'MANIFEST', manifest):
                    self.assertIn('requalify', MODULE.check(ROOT / TARGET))
                    self.assertEqual(run_main(['--write'], TARGET=out)[0], 1)
                self.assertFalse(out.exists())


class MainTests(unittest.TestCase):
    def test_write_regenerates_only_the_fixed_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / 'app' / 'runnable-backlog.sql'
            self.assertEqual(run_main(['--write'], TARGET=target), (0, ''))
            self.assertEqual(target.read_bytes(), (ROOT / TARGET).read_bytes())
            self.assertEqual(run_main(['--check', str(target)])[0], 0)
        for argv in (['--write', 'other.sql'], [], ['--check', TARGET, '--write']):
            with self.subTest(argv=argv):
                self.assertEqual(run_main(argv)[0], 2)

    def test_check_fails_with_a_fixed_message_and_never_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / 'runnable-backlog.sql'
            target.write_text('unchanged')
            for path in ('', 'does/not/exist.sql'):
                with self.subTest(path=path):
                    self.assertEqual(run_main(['--check', path], TARGET=target),
                                     (1, 'generated DDL missing; run --write\n'))
            self.assertEqual(target.read_text(), 'unchanged')


class DeploymentTests(unittest.TestCase):
    def test_no_kustomization_maps_the_generated_sql(self):
        self.assertEqual(list((ROOT / TARGET).parent.parent.glob('**/kustomization.y*ml')), [])
        for path in (ROOT / 'kubernetes').rglob('*.y*ml'):
            text = path.read_text(errors='replace')
            self.assertNotIn('hindsight-backlog', text, path)
            self.assertNotIn('runnable-backlog', text, path)

    def test_workflow_triggers_on_bound_paths_and_runs_every_gate(self):
        workflow = (ROOT / '.github/workflows/hindsight-baseline.yaml').read_text()
        for path in ('scripts/hindsight-backlog-function.py', TARGET,
                     'tests/hindsight-observability/test_backlog_function.py',
                     'kubernetes/apps/ai/hermes-hindsight/app/hindsight.yaml', 'scripts/hindsight-queue-backlog.py'):
            self.assertIn('      - ' + path + '\n', workflow)
        self.assertIn('python -m unittest discover -s tests/hindsight-observability -p test_backlog_function.py -v',
                      workflow)
        # --check must run on the committed file; regenerating first would hide drift.
        self.assertIn(f'python scripts/hindsight-backlog-function.py --check {TARGET}\n', workflow)
        self.assertNotIn('--write', workflow)


if __name__ == '__main__':
    unittest.main()
