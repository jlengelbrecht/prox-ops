"""Offline regression tests for web-dist-patch.py.

A synthetic dist reproduces the shipped bundle's reference graph (index.html
-> entry chunk -> Plugins and Sessions chunks, Sessions importing the entry)
and the two save-filter sites, with the pins re-bound to the fixture's own
hashes. Every failure path must leave an unmodified copy behind; the applied
path must rename the cascade, keep every other file byte-identical and leave
no stale or dangling reference. The correction's semantics are checked with
the real minified `fieldIsVisible` under node when node is installed.

Run locally (not CI), like validate-browser-isolation.py:

  python3 -m pytest kubernetes/apps/ai/hermes-hindsight/app/test_web_dist_patch.py -q

Set HERMES_WEB_DIST_SRC to a copy of the pinned image's web_dist to also run
the real-artifact case against the checked-in pins.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("web_dist_patch", HERE / "web-dist-patch.py")
wdp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wdp)

# The shipped fieldIsVisible, verbatim from the minified Plugins chunk.
VISIBLE_FN = ("function N(e,t){return!e.when||Object.entries(e.when).every(([e,n])=>"
              "{let r=t[e];return String(r??``)===String(n)})}")
SITE = "let t=" + wdp.OLD + "}))"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def build_dist(root: Path, *, plugins=None, entry=None, sessions=None, index=None) -> Path:
    src = root / "web_dist"
    (src / "assets").mkdir(parents=True)
    (src / "fonts").mkdir()
    files = {
        "index.html": index if index is not None else
        '<script type="module" crossorigin src="/assets/index-0-lwe-sG.js"></script>'
        '<link rel="stylesheet" href="/assets/index-Cabc1234.css">',
        "assets/index-0-lwe-sG.js": entry if entry is not None else
        'import"./vendor-B7eZBWFZ.js";const m=["assets/PluginsPage-tfekLiaM.js",'
        '"assets/SessionsPage-CRM4U72X.js"];const P=()=>import(`./PluginsPage-tfekLiaM.js`);'
        'const S=()=>import(`./SessionsPage-CRM4U72X.js`);export{P,S};',
        "assets/PluginsPage-tfekLiaM.js": plugins if plugins is not None else
        'import{a}from"./vendor-B7eZBWFZ.js";' + VISIBLE_FN + "function save(W,H){return Object.fromEntries("
        "Object.entries(W).filter(([e])=>{" + SITE + "}function setup(W,H){return Object.fromEntries("
        "Object.entries(W).filter(([e])=>{" + SITE + "}export{save,setup};",
        "assets/SessionsPage-CRM4U72X.js": sessions if sessions is not None else
        'import{P}from"./index-0-lwe-sG.js";export const s=P;',
        "assets/vendor-B7eZBWFZ.js": "export const a=1;",
        "assets/index-Cabc1234.css": "@font-face{src:url(./inter-AbCdEfGh.woff2)}body{margin:0}",
        "assets/inter-AbCdEfGh.woff2": b"\x00binary\xff",
        "fonts/other.woff2": b"\x00font\xff",
        "favicon.ico": b"\x00ico\xff",
    }
    for rel, body in files.items():
        (src / rel).write_bytes(body.encode() if isinstance(body, str) else body)
    return src


def pin(monkeypatch, src: Path, patched_sha=None):
    """Bind the module's pins to the fixture the way the real ones bind to the image."""
    monkeypatch.setattr(wdp, "PINNED_SHA256", {rel: sha256((src / rel).read_bytes()) for rel in wdp.PINNED_SHA256})
    if patched_sha is None:
        patched_sha = sha256((src / wdp.TARGET).read_text().replace(wdp.OLD, wdp.NEW).encode())
    monkeypatch.setattr(wdp, "PATCHED_SHA256", patched_sha)


@pytest.fixture
def dist(tmp_path, monkeypatch):
    src = build_dist(tmp_path)
    pin(monkeypatch, src)
    root = tmp_path / "web-dist"
    root.mkdir()
    return src, root


def run(src: Path, root: Path) -> int:
    return wdp.main(src, root)


def status(root: Path) -> dict:
    return json.loads((root / "status.json").read_text())


def assert_passthrough(src: Path, root: Path, reason: str, capsys):
    assert tree(root / "dist") == tree(src), "pass-through must serve the image bundle unchanged"
    st = status(root)
    assert st["applied"] is False and reason in st["reason"]
    assert "WARNING: web_dist patch NOT applied" in capsys.readouterr().err
    assert not (root / "dist.new").exists()


def test_applied_renames_cascade_and_keeps_everything_else(dist, capsys):
    src, root = dist
    before = tree(src)
    assert run(src, root) == 0
    out = root / "dist"
    st = status(root)
    assert st["applied"] is True and st["reason"] == ""
    assert not (out / "status.json").exists(), "status lives outside the served directory"
    after = tree(out)
    touched = {wdp.TARGET, wdp.ENTRY, wdp.SESSIONS, "index.html"}
    assert set(after) == (set(before) - touched) | {"index.html"} | {wdp.renamed(r) for r in wdp.RENAMED}
    for rel in set(before) - touched:
        assert after[rel] == before[rel], f"{rel} must be byte-identical"
    plugins = after[wdp.renamed(wdp.TARGET)].decode()
    assert plugins.count(wdp.NEW) == 2 and wdp.OLD not in plugins
    assert sha256(after[wdp.renamed(wdp.TARGET)]) == st["patched_sha256"] == wdp.PATCHED_SHA256
    for rel in wdp.RENAMED:
        name = Path(rel).name
        assert not any(name.encode() in body for body in after.values()), f"{name} still referenced"
    have = {Path(rel).name for rel in after if rel.startswith("assets/")}
    for rel, body in after.items():
        if Path(rel).suffix in wdp.TEXT_SUFFIXES:
            refs = {m.group(1) for m in wdp.ASSET_REF.finditer(body.decode())}
            assert refs <= have, f"{rel} references missing assets {refs - have}"
    entry = after[wdp.renamed(wdp.ENTRY)].decode()
    assert entry.count("PluginsPage-tfekLiaM.hh1.js") == 2 and entry.count("SessionsPage-CRM4U72X.hh1.js") == 2
    assert after["index.html"].decode().count("index-0-lwe-sG.hh1.js") == 1
    assert "web_dist patch hh1 applied" in capsys.readouterr().out
    assert tree(src) == before, "the source dist is never written"


def test_target_hash_mismatch_passes_through(dist, monkeypatch, capsys):
    src, root = dist
    (src / wdp.TARGET).write_text((src / wdp.TARGET).read_text() + "\n// rebuilt")
    assert run(src, root) == 0
    assert_passthrough(src, root, "sha256 mismatch", capsys)


def test_wrong_site_count_passes_through(dist, monkeypatch, capsys):
    src, root = dist
    body = (src / wdp.TARGET).read_text().replace(wdp.OLD, "let z=1;" + wdp.OLD, 1)
    (src / wdp.TARGET).write_text(body + wdp.OLD)  # three sites, every hash re-pinned
    pin(monkeypatch, src)
    assert run(src, root) == 0
    assert_passthrough(src, root, "3 save-filter sites, expected 2", capsys)


def test_reference_count_mismatch_passes_through(dist, monkeypatch, capsys):
    src, root = dist
    (src / wdp.ENTRY).write_text((src / wdp.ENTRY).read_text() + '/*"assets/PluginsPage-tfekLiaM.js"*/')
    pin(monkeypatch, src)
    assert run(src, root) == 0
    assert_passthrough(src, root, "names PluginsPage-tfekLiaM.js 3x, expected 2", capsys)


def test_patched_hash_mismatch_passes_through(dist, monkeypatch, capsys):
    src, root = dist
    pin(monkeypatch, src, patched_sha="0" * 64)
    assert run(src, root) == 0
    assert_passthrough(src, root, "patched chunk sha256 mismatch", capsys)


def test_missing_pinned_file_passes_through(dist, capsys):
    src, root = dist
    (src / wdp.SESSIONS).unlink()
    assert run(src, root) == 0
    assert_passthrough(src, root, "pinned file missing: " + wdp.SESSIONS, capsys)


def test_dangling_reference_passes_through(dist, monkeypatch, capsys):
    src, root = dist
    (src / wdp.SESSIONS).write_text((src / wdp.SESSIONS).read_text() + 'import"./gone-Zz9Zz9Zz.js";')
    pin(monkeypatch, src)
    assert run(src, root) == 0
    assert_passthrough(src, root, "references missing assets: gone-Zz9Zz9Zz.js", capsys)


def test_source_without_index_fails_startup(dist, capsys):
    src, root = dist
    (src / "index.html").unlink()
    assert run(src, root) == 1
    assert not (root / "dist").exists() and not (root / "status.json").exists()
    assert "no index.html" in capsys.readouterr().err


def test_refuses_to_manage_the_source_or_a_parent(dist, capsys):
    src, root = dist
    assert run(src, src) == 1
    assert run(src, src.parent) == 1
    assert run(src, root / "absent") == 1
    assert not (src / "dist").exists() and not (src.parent / "dist").exists()
    assert "refusing to manage" in capsys.readouterr().err


def test_rerun_rebuilds_its_own_output_only(dist):
    src, root = dist
    assert run(src, root) == 0
    first = tree(root / "dist")
    (root / "dist" / "assets" / "leftover-OldOldOl.js").write_text("stale")
    (root / "dist.new").mkdir()
    (root / "dist.new" / "half").write_text("partial")
    (root / "keep.txt").write_text("not ours")
    assert run(src, root) == 0
    assert tree(root / "dist") == first
    assert not (root / "dist.new").exists()
    assert (root / "keep.txt").read_text() == "not ours"
    assert status(root)["applied"] is True


def test_passthrough_after_success_replaces_the_patched_copy(dist, monkeypatch, capsys):
    src, root = dist
    assert run(src, root) == 0
    assert status(root)["applied"] is True
    (src / wdp.ENTRY).write_text((src / wdp.ENTRY).read_text() + "\n")
    assert run(src, root) == 0
    assert_passthrough(src, root, wdp.ENTRY + " sha256 mismatch", capsys)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_correction_keeps_any_visible_row_and_drops_hidden_only_keys():
    """The replacement expression against the Hindsight schema shape."""
    script = VISIBLE_FN + """
const H={fields:[{key:"mode"},
 {key:"api_url",when:{mode:"cloud"}},{key:"api_key",when:{mode:"cloud"}},
 {key:"api_url",when:{mode:"local_external"}},{key:"api_key",when:{mode:"local_external"}},
 {key:"llm_provider",when:{mode:"local_embedded"}},
 {key:"llm_base_url",when:{mode:"local_embedded",llm_provider:"openai_compatible"}},
 {key:"bank_id"},{key:"bank_mission"}]};
const shipped=(W,H)=>Object.fromEntries(Object.entries(W).filter(([e])=>{let t=%s}));
const patched=(W,H)=>Object.fromEntries(Object.entries(W).filter(([e])=>{let t=%s}));
const W={api_url:"http://fixture:8888",api_key:"fixture-key",bank_id:"b",bank_mission:"",
 llm_provider:"openai_compatible",llm_base_url:"http://x/v1",extra_key:"kept"};
const out={};
for (const mode of ["local_external","cloud","local_embedded"]) {
  out[mode]={shipped:Object.keys(shipped({mode,...W},H)),patched:Object.keys(patched({mode,...W},H))};
}
out.blank=patched({mode:"local_external",api_url:"u",api_key:""},H);
out.noschema=Object.keys(patched({mode:"cloud",api_url:"u"},undefined));
process.stdout.write(JSON.stringify(out));
""" % (wdp.OLD, wdp.NEW)
    out = json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)
    common = ["mode", "bank_id", "bank_mission", "extra_key"]
    assert out["local_external"]["shipped"] == common, "the defect: shipped drops URL and key"
    connected = ["mode", "api_url", "api_key", "bank_id", "bank_mission", "extra_key"]
    assert out["local_external"]["patched"] == connected
    assert out["cloud"]["patched"] == out["cloud"]["shipped"] == connected
    assert out["local_embedded"]["patched"] == out["local_embedded"]["shipped"] == [
        "mode", "bank_id", "bank_mission", "llm_provider", "llm_base_url", "extra_key"]
    # A blank secret is still submitted; the server treats blank as "keep the stored key".
    assert out["blank"] == {"mode": "local_external", "api_url": "u", "api_key": ""}
    assert out["noschema"] == ["mode", "api_url"]


@pytest.mark.skipif(not os.environ.get("HERMES_WEB_DIST_SRC"), reason="HERMES_WEB_DIST_SRC not set")
def test_real_bundle_matches_checked_in_pins(tmp_path):
    src = Path(os.environ["HERMES_WEB_DIST_SRC"])
    root = tmp_path / "web-dist"
    root.mkdir()
    assert run(src, root) == 0
    st = status(root)
    assert st["applied"] is True, st["reason"]
    assert st["patched_sha256"] == wdp.PATCHED_SHA256
    for rel, want in wdp.PINNED_SHA256.items():
        assert sha256((src / rel).read_bytes()) == want
    out = tree(root / "dist")
    assert sha256(out[wdp.renamed(wdp.TARGET)]) == wdp.PATCHED_SHA256
    assert len([r for r in out if r.startswith("assets/")]) == len([p for p in (src / "assets").iterdir()])
