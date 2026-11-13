"""Offline tests for retain-recovery-patch.py.

The excerpts copy the pinned upstream orchestrator's anchor lines and
_classify_chunk_diff verbatim, so the inserted code runs in its real context
against stubs. Routing and seeding logic only; native QA decides recovery.
"""

import asyncio
import contextlib
import hashlib
import importlib.util
import inspect
import io
import os
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("retain_recovery_patch", HERE / "retain-recovery-patch.py")
rrp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rrp)

PINNED = HERE.parents[4] / "_bmad-output/implementation-artifacts/verified-native-context/engine/retain/orchestrator.py"

DELTA_EXCERPT = '''\
def _classify_chunk_diff(existing_by_index: dict[int, Any], new_hashes: dict[int, str]) -> _ChunkDiff:
    diff = _ChunkDiff(unchanged=[], changed=[], new=[], removed=[])
    for idx, new_hash in new_hashes.items():
        existing = existing_by_index.get(idx)
        if existing and existing.content_hash == new_hash:
            diff.unchanged.append(idx)
        elif existing:
            diff.changed.append(idx)
        else:
            diff.new.append(idx)
    for idx in existing_by_index:
        if idx not in new_hashes:
            diff.removed.append(idx)
    return diff


async def _try_delta_retain(contents_dicts, document_body_override, doc_hash_at_load, _store_owned_delta,
                            existing_by_index, new_hashes, log_buffer):
    diff = _classify_chunk_diff(existing_by_index, new_hashes)
    unchanged_indices = diff.unchanged
    changed_indices = diff.changed
    new_indices = diff.new
    removed_indices = diff.removed

    if not unchanged_indices:
        logger.info(f"Delta retain: no unchanged chunks for {effective_doc_id}, falling back to full retain")
        return None

    chunks_to_process = changed_indices + new_indices

    if not chunks_to_process and not removed_indices:
        return "metadata-only"
    return ("delta", chunks_to_process, removed_indices)
'''

ANN_EXCERPT = '''\
async def _streaming_retain_batch(pool, bank_id, effective_doc_id, is_recovery, facts_already_committed,
                                  chunk_index_offset, pipeline_aborted, all_unit_ids, log_buffer):
    if not facts_already_committed:
        pass
    else:
        # Recovery path: load committed unit IDs from DB
        async with acquire_with_retry(pool) as conn:
            rows = await conn.fetch(
                f"""
                SELECT id::text FROM {fq_table("memory_units")}
                WHERE bank_id = $1 AND document_id = $2
                ORDER BY created_at
                """,
                bank_id,
                effective_doc_id,
            )
            all_unit_ids = [row["id"] for row in rows]
            log_buffer.append(f"[streaming] Recovery: loaded {len(all_unit_ids)} unit IDs from DB")

    # ---------------------------------------------------------------------------
    # Final ANN pass: create semantic links for ALL committed units at once.
    return all_unit_ids
'''
EXCERPT = DELTA_EXCERPT + "\n\n" + ANN_EXCERPT
UNIT_SQL = "SELECT id::text FROM hs.memory_units WHERE bank_id = $1 AND document_id = $2 ORDER BY created_at"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class FakeConn:
    def __init__(self, units):
        self.units, self.queries = units, []

    async def fetch(self, sql, bank_id, document_id):
        self.queries.append((" ".join(sql.split()), (bank_id, document_id)))
        return [{"id": u} for b, d, u in self.units if (b, d) == (bank_id, document_id)]


def load_patched():
    @contextlib.asynccontextmanager
    async def acquire_with_retry(pool):
        yield pool

    ns = {"Any": object, "_ChunkDiff": types.SimpleNamespace, "hashlib": hashlib, "effective_doc_id": "d",
          "fact_extraction": types.SimpleNamespace(_sanitize_text=lambda text: text.replace("\x00", "")),
          "logger": types.SimpleNamespace(info=lambda msg: None), "acquire_with_retry": acquire_with_retry,
          "fq_table": lambda name: "hs." + name}
    exec(compile(rrp._apply(EXCERPT), "patched-excerpt", "exec"), ns)
    return ns


class AnchorTests(unittest.TestCase):
    def test_applies_each_hunk_once_only_and_leaves_the_rest_byte_identical(self):
        patched = rrp._apply(EXCERPT)
        for insert in (rrp.DELTA_INSERT, rrp.ANN_INSERT):
            self.assertEqual(patched.count(insert), 1)
        self.assertEqual(patched.replace(rrp.DELTA_INSERT, "").replace(rrp.ANN_INSERT, ""), EXCERPT)
        with self.assertRaisesRegex(rrp.PatchRefused, "delta hunk already applied"):
            rrp._apply(patched)

    def test_refuses_missing_or_duplicated_anchors(self):
        cases = {
            "delta anchor found 0": EXCERPT.replace("    chunks_to_process = changed_indices + new_indices\n", ""),
            "ann anchor found 0": EXCERPT.replace("    # Final ANN pass: create", "    # Final pass: create"),
            "ann anchor found 0 ": DELTA_EXCERPT,
            "delta anchor found 2": EXCERPT + EXCERPT,
            "ann anchor found 2": DELTA_EXCERPT + ANN_EXCERPT + ANN_EXCERPT,
        }
        for want, text in cases.items():
            with self.subTest(want), self.assertRaisesRegex(rrp.PatchRefused, want.strip()):
                rrp._apply(text)


class PinTests(unittest.TestCase):
    def test_pin_is_the_recorded_upstream_hash_and_cannot_be_overridden(self):
        self.assertEqual(rrp.PINNED_SHA256, "6cf4b736672b8efcebc5a34740d97c176a02daf10596c540ed62c5c14622267c")
        self.assertEqual(list(inspect.signature(rrp.patch_bytes).parameters), ["source"])
        for hook in ("environ", "getenv", "argparse"):
            self.assertNotIn(hook, (HERE / "retain-recovery-patch.py").read_text())

    def test_anything_but_the_pinned_bytes_is_refused(self):
        for source in (EXCERPT.encode(), b"", b"\xff\xfe", EXCERPT):
            with self.subTest(source[:8]), self.assertRaises(rrp.PatchRefused):
                rrp.patch_bytes(source)


class DeltaRoutingTests(unittest.TestCase):
    ns = load_patched()

    def route(self, chunks, stored, doc_hash, owned=False, override=None):
        logs, existing = [], {i: types.SimpleNamespace(content_hash=h) for i, h in stored.items()}
        new_hashes = {i: sha(t) for i, t in enumerate(chunks)}
        contents = [{"content": "\n".join(chunks)}]
        result = asyncio.run(self.ns["_try_delta_retain"](contents, override, doc_hash, owned, existing, new_hashes, logs))
        return result, logs

    def test_same_body_with_missing_chunks_defers_to_streaming_recovery(self):
        for stored in ({0: sha("a"), 1: sha("b")}, {0: sha("a"), 2: sha("c")}):
            result, logs = self.route(["a", "b", "c", "d"], stored, sha("a\nb\nc\nd"))
            self.assertIsNone(result)
            self.assertIn("deferring to streaming recovery", logs[-1])

    def test_body_hash_matches_the_stored_row_semantics(self):
        sanitized, override = (["a\x00", "b", "c"], {0: sha("a\x00")}), (["a", "b"], {0: sha("a")})
        self.assertIsNone(self.route(*sanitized, sha("a\nb\nc"))[0])
        self.assertEqual(self.route(*sanitized, sha("a\x00\nb\nc"))[0], ("delta", [1, 2], []))
        self.assertIsNone(self.route(*override, sha("full"), override="full")[0])
        self.assertEqual(self.route(*override, sha("a\nb"), override="full")[0], ("delta", [1], []))

    def test_ordinary_updates_keep_the_delta_path(self):
        a, b, c, x, same = sha("a"), sha("b"), sha("c"), sha("x"), sha("a\nb\nc")
        cases = {
            "append": (["a", "b", "c"], {0: a, 1: b}, sha("a\nb"), False, ("delta", [2], [])),
            "edit": (["a", "b", "c"], {0: a, 1: b, 2: x}, sha("a\nb\nx"), False, ("delta", [2], [])),
            "removed": (["a", "b"], {0: a, 1: b, 2: c}, same, False, ("delta", [], [2])),
            "changed, same hash": (["a", "b", "c"], {0: a, 1: x}, same, False, ("delta", [1, 2], [])),
            "removed, same hash": (["a", "b", "c"], {0: a, 3: x}, same, False, ("delta", [1, 2], [3])),
            "store-owned": (["a", "b", "c"], {0: a}, same, True, ("delta", [1, 2], [])),
            "no stored hash": (["a", "b", "c"], {0: a}, None, False, ("delta", [1, 2], [])),
            "unchanged": (["a", "b", "c"], {0: a, 1: b, 2: c}, same, False, "metadata-only"),
        }
        for name, (chunks, stored, doc_hash, owned, want) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.route(chunks, stored, doc_hash, owned), (want, []))


class RecoveryAnnSeedTests(unittest.TestCase):
    ns = load_patched()
    units = [("b", "d", "u1"), ("b", "x", "other"), ("b", "d", "u2"), ("b", "d", "u3"), ("b", "d", "u4")]

    def seeds(self, recovery=True, committed=False, offset=0, aborted=False, written=("u3", "u4")):
        conn = FakeConn(self.units)
        seeds = asyncio.run(self.ns["_streaming_retain_batch"](
            conn, "b", "d", recovery, committed, offset, [aborted], list(written), []))
        return seeds, conn.queries

    def test_recovery_seeds_every_unit_of_the_document_once(self):
        for _ in range(2):
            self.assertEqual(self.seeds(), (["u1", "u2", "u3", "u4"], [(UNIT_SQL, ("b", "d"))]))

    def test_other_paths_are_unchanged(self):
        cases = {
            "first-time": (dict(recovery=False), ["u3", "u4"], []),
            "later slice": (dict(offset=4), ["u3", "u4"], []),
            "aborted": (dict(aborted=True), ["u3", "u4"], []),
            "facts already committed": (dict(committed=True, written=()), ["u1", "u2", "u3", "u4"],
                                        [(UNIT_SQL, ("b", "d"))]),
        }
        for name, (kw, want, want_queries) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.seeds(**kw), (want, want_queries))


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.src = self.dir / "orchestrator.py"
        self.src.write_bytes(EXCERPT.encode())
        os.chmod(self.src, 0o640)

    def run_main(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out:
            code = rrp.main([str(a) for a in argv])
        return code, err.getvalue(), out.getvalue()

    def assert_refused(self, code, err, out):
        self.assertEqual((code, out, len(err.splitlines()), err[:29]), (1, "", 1, "ERROR: retain-recovery patch "))
        self.assertNotIn(str(self.dir), err)

    def test_refusals_exit_nonzero_and_write_nothing(self):
        (self.dir / "link.py").symlink_to(self.src)
        target = self.dir / "out" / "orchestrator.py"
        target.parent.mkdir()
        for argv in ((), (self.src,), (self.src, target, "x"), (self.src, target), (self.dir / "link.py", target),
                     (self.dir / "missing.py", target), (self.dir, target)):
            with self.subTest(argv):
                self.assert_refused(*self.run_main(*argv))
                self.assertEqual(list(target.parent.iterdir()), [])

    def test_write_is_atomic_keeps_mode_and_never_follows_or_clobbers_foreign_paths(self):
        st, target, victim = os.stat(self.src), self.dir / "out.py", self.dir / "victim.py"
        target.write_bytes(b"old")
        old_inode = os.stat(target).st_ino
        rrp._write_target(str(target), b"new", st)
        self.assertEqual((target.read_bytes(), os.stat(target).st_mode & 0o777), (b"new", 0o640))
        self.assertNotEqual(os.stat(target).st_ino, old_inode)
        victim.write_bytes(b"keep")
        (self.dir / "sym.py").symlink_to(victim)
        (self.dir / "sub").mkdir()
        (self.dir / "symdir").symlink_to(self.dir / "sub")
        for path, error in ((self.dir / "sym.py", rrp.PatchRefused), (self.dir / "sub", rrp.PatchRefused),
                            (self.src, rrp.PatchRefused), (self.dir / "symdir" / "x.py", OSError)):
            with self.subTest(path.name), self.assertRaises(error):
                rrp._write_target(str(path), b"bad", st)
        self.assertEqual((victim.read_bytes(), self.src.read_bytes()), (b"keep", EXCERPT.encode()))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["orchestrator.py", "out.py", "sub", "sym.py", "symdir", "victim.py"])

    @unittest.skipUnless(PINNED.is_file(), "pinned upstream module not present")
    def test_pinned_module_patches_once_and_round_trips(self):
        self.src.write_bytes(PINNED.read_bytes())
        target = self.dir / "patched.py"
        self.assertEqual(self.run_main(self.src, target), (0, "", "retain-recovery patch applied\n"))
        patched = target.read_bytes()
        self.assertEqual((patched, os.stat(target).st_mode & 0o777), (rrp.patch_bytes(PINNED.read_bytes()), 0o640))
        for insert in (rrp.DELTA_INSERT, rrp.ANN_INSERT):
            self.assertEqual(patched.decode().count(insert), 1)
        self.assert_refused(*self.run_main(target, self.dir / "again.py"))
