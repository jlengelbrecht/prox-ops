#!/usr/bin/env python3
"""Offline packaging and serial supervisor checks."""
import importlib.util
import io
import os
from pathlib import Path
import re
import signal
import subprocess
import tarfile
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_pins_and_isolation(self):
        lock = (ROOT / "versions.lock").read_text()
        dockerfile = (ROOT / "Dockerfile").read_text()
        fixture = (ROOT / "test-local.sh").read_text()
        self.assertIn("f62d4efa4768a9ca985248f5ad931cc2e5c53f41", lock)
        self.assertIn("sha256:c67e92c8099ba22853ab4cfda7cfb494d253d6012ca82dee98ba8976772af018", dockerfile)
        self.assertIn("fdb65eb961d3d4c1959601a51bc9bc5663b0403369ad50a422af16947ac3f3ba", (ROOT / "checksums.sha256").read_text())
        for expected in ("--network none", "--read-only", "--cap-drop ALL", "--log-driver none",
                         "DOCKER_HOST=unix:///run/docker.sock", "source_git archive --format=tar",
                         "GIT_NO_REPLACE_OBJECTS=1", "GIT_NO_LAZY_FETCH=1"):
            self.assertIn(expected, fixture)
        self.assertNotIn("AFK_FACTORY_OFFLINE_SYNTH", dockerfile)
        self.assertIn("--refuse-public", dockerfile)
        self.assertIn("rm -f /etc/machine-id", dockerfile)
        self.assertIn("ORCA_INSTANCE_ID=$container_name", fixture)
        self.assertNotIn("COPY . ", dockerfile)

    def test_docker_owned_cleanup_paths(self):
        fixture = (ROOT / "test-local.sh").read_text()
        cleanup = fixture.split("cleanup() {\n", 1)[1].split("\ndock() {", 1)[0]
        cleanup = "cleanup() {\n" + cleanup
        detached = fixture.split("container_id=$(dock run -d ", 1)[1].split("\nready=0", 1)[0]
        detached = "container_id=$(dock run -d " + detached
        stop = fixture.split("dock stop --time 15", 1)[1].split("\n", 1)[0]
        stop = "dock stop --time 15" + stop
        for scenario, expected in (("success", ["stop", "rm-owned", "image-rm"]),
                                   ("remove-fails", ["stop", "rm-owned", "image-rm"]),
                                   ("partial", ["rm-owned", "image-rm"]),
                                   ("foreign", ["image-rm"])):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                script = f'''set -euo pipefail
container_name=fixture-test; container_id=''; image_tag=fixture:image; image_built=1
scratch={directory}/scratch; client_cfg={directory}/config; stage={directory}/stage
mkdir -p "$scratch" "$client_cfg" "$stage"
run_flags=()
scenario={scenario}
dock() {{
  printf '%s\\n' "$*" >> {directory}/calls
  case "$1 $2" in
    "run -d")
      if [[ "$scenario" == partial || "$scenario" == foreign ]]; then return 17; fi
      printf 'owned-id\\n';;
    "container inspect")
      if [[ "$scenario" == foreign ]]; then printf 'someone-else\\n';
      else printf '%s\\n' "$container_name"; fi;;
    "image inspect") printf '%s\\n' "$container_name";;
    "rm -f") printf 'rm-owned\\n' >> {directory}/events
      if [[ "$scenario" == remove-fails ]]; then return 9; fi;;
    "image rm") printf 'image-rm\\n' >> {directory}/events;;
    "stop --time") printf 'stop\\n' >> {directory}/events;;
  esac
}}
{cleanup}
trap cleanup EXIT
{detached}
{stop if scenario in ("success", "remove-fails") else ""}
'''
                result = subprocess.run(["bash", "-c", script], text=True,
                                        capture_output=True)
                self.assertEqual(result.returncode,
                                 17 if scenario in ("partial", "foreign") else
                                 1 if scenario == "remove-fails" else 0,
                                 result.stderr)
                events = (Path(directory) / "events").read_text().splitlines()
                self.assertEqual(events, expected)
                calls = (Path(directory) / "calls").read_text()
                self.assertNotIn("run --rm", calls)
                self.assertNotIn("rm -f owned-id", calls)

    def test_git_source_pin_ignores_replacement_and_caller_settings(self):
        fixture = (ROOT / "test-local.sh").read_text()
        source_git = "source_git() {\n" + fixture.split("source_git() {\n", 1)[1].split("\n[[", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
            git("init", "-q")
            (repo / "payload").write_text("pinned\n")
            git("add", "payload")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "pinned")
            pinned = git("rev-parse", "HEAD")
            tree = git("rev-parse", "HEAD^{tree}")
            (repo / "payload").write_text("replacement\n")
            git("add", "payload")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "replacement")
            git("replace", pinned, git("rev-parse", "HEAD"))
            trace = Path(directory) / "trace"
            env = dict(os.environ, GIT_TRACE=str(trace), GIT_CONFIG_COUNT="1",
                       GIT_CONFIG_KEY_0="core.logAllRefUpdates", GIT_CONFIG_VALUE_0="true")
            result = subprocess.run(["bash", "-c", source_git + '\nsource_git rev-parse --verify "$1^{tree}"',
                                     "bash", pinned], env=dict(env, source_repo=str(repo)),
                                    text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), tree)
            self.assertFalse(trace.exists())

    def test_private_stage_ignores_existing_symlink_hardlink_and_shared_parent(self):
        fixture = (ROOT / "test-local.sh").read_text()
        prepare = 'stage=$(mktemp -d /tmp/factory-fixture.XXXXXXXX)' + fixture.split(
            'stage=$(mktemp -d /tmp/factory-fixture.XXXXXXXX)', 1)[1].split('scratch=$(mktemp -d)', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            here = Path(directory) / "fixture"
            here.mkdir()
            for name in ("Dockerfile", "safe-extract.py", "poll-supervisor.sh"):
                (here / name).write_bytes((ROOT / name).read_bytes())
            archive = Path(directory) / "archive"
            archive.write_bytes(b"synthetic archive")
            (here / "checksums.sha256").write_text(
                "74a06ea6113b7f98d70ba2286ddaa21a9a17e25ed2bb8736a823dbc46177eb44  inputs/orca-server-linux-x64.tar.gz\n")
            repo = Path(directory) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for name, data, mode in (("bin/agent-flow-kit", "#!/bin/sh\nexit 0\n", 0o755),
                                     ("lib/module", "payload\n", 0o644),
                                     ("adapters/runtime-scope.sh", "#!/bin/sh\n", 0o755)):
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(data)
                path.chmod(mode)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", "commit", "-qm", "pinned"], check=True)
            script = ('set -euo pipefail\numask 077\n'
                      'here="$1"; source_repo="$2"; archive="$3"; commit=HEAD; '
                      'asset=orca-server-linux-x64.tar.gz; url=unused; tree=synthetic\n'
                      'source_git() { git -C "$source_repo" "$@"; }\n' + prepare +
                      'test "$(stat -c %a "$stage")" = 700\n'
                      'test -x "$stage/inputs/afk/bin/agent-flow-kit"\n')
            external = Path(directory) / "external"
            (external / "afk").mkdir(parents=True)
            sentinel = external / "afk/sentinel"
            sentinel.write_text("keep me")
            for scenario in ("symlink", "hardlink", "shared"):
                with self.subTest(scenario=scenario):
                    inputs = here / "inputs"
                    if scenario == "symlink":
                        inputs.symlink_to(external, target_is_directory=True)
                    else:
                        inputs.mkdir(mode=0o777)
                        inputs.chmod(0o777)
                        (inputs / "afk").mkdir()
                        (inputs / "afk/sentinel").write_text("keep me")
                        if scenario == "hardlink":
                            (inputs / "orca-server-linux-x64.tar.gz").hardlink_to(archive)
                    result = subprocess.run(["bash", "-c", script, "bash", str(here), str(repo), str(archive)],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(sentinel.read_text(), "keep me")
                    self.assertEqual(archive.read_bytes(), b"synthetic archive")
                    if scenario != "symlink":
                        self.assertEqual((inputs / "afk/sentinel").read_text(), "keep me")
                        self.assertEqual(inputs.stat().st_mode & 0o777, 0o777)
                        if scenario == "hardlink":
                            self.assertEqual((inputs / "orca-server-linux-x64.tar.gz").read_bytes(), b"synthetic archive")
                        for child in inputs.iterdir():
                            if child.is_dir():
                                for nested in child.iterdir():
                                    nested.unlink()
                                child.rmdir()
                            else:
                                child.unlink()
                        inputs.rmdir()
                    else:
                        inputs.unlink()

    def test_safe_extraction_rejects_escape(self):
        spec = importlib.util.spec_from_file_location("safe_extract", ROOT / "safe-extract.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name, link in (("../escape", None), ("root/link", "../../escape")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "input.tgz"
                with tarfile.open(archive, "w:gz") as tar:
                    member = tarfile.TarInfo(name)
                    if link:
                        member.type = tarfile.SYMTYPE
                        member.linkname = link
                    else:
                        member.size = 1
                    tar.addfile(member, None if link else io.BytesIO(b"x"))
                with self.assertRaises(ValueError):
                    module.extract(archive, Path(directory) / "out")

    def test_safe_extraction_preserves_server_layout(self):
        spec = importlib.util.spec_from_file_location("safe_extract", ROOT / "safe-extract.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "input.tgz"
            with tarfile.open(archive, "w:gz") as tar:
                root = tarfile.TarInfo("./orca-server")
                root.type = tarfile.DIRTYPE
                tar.addfile(root)
                member = tarfile.TarInfo("./orca-server/bin/orca-ide")
                member.size = 1
                member.mode = 0o755
                tar.addfile(member, io.BytesIO(b"x"))
            destination = Path(directory) / "out"
            module.extract(archive, destination)
            self.assertEqual((destination / "bin/orca-ide").read_bytes(), b"x")
            self.assertTrue((destination / "bin/orca-ide").stat().st_mode & 0o111)

    def supervisor(self, directory, body, cadence="1"):
        fake = Path(directory) / "fake-afk"
        fake.write_text("#!/usr/bin/env bash\n" + body)
        fake.chmod(0o755)
        copy = Path(directory) / "poll-supervisor.sh"
        copy.write_text((ROOT / "poll-supervisor.sh").read_text().replace(
            "/opt/afk/bin/agent-flow-kit", str(fake)))
        copy.chmod(0o755)
        env = dict(os.environ, AFK_POLL_CADENCE_SECONDS=cadence,
                   TEST_LOG=str(Path(directory) / "events"))
        return subprocess.Popen(["bash", str(copy)], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True)

    def test_cadence_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            for invalid in ("0", "-2", "abc", "1.5"):
                process = self.supervisor(directory, "exit 0\n", invalid)
                _, error = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 2)
                self.assertIn("positive integer", error)

    def test_long_pass_serial_and_failure_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            process = self.supervisor(directory,
                'printf "start %s\\n" "$(date +%s%N)" >> "$TEST_LOG"\n'
                'sleep 2\nprintf "end %s\\n" "$(date +%s%N)" >> "$TEST_LOG"\nexit 7\n')
            try:
                time.sleep(2.5)
                events = (Path(directory) / "events").read_text().splitlines()
                self.assertEqual([line.split()[0] for line in events], ["start", "end", "start"])
            finally:
                process.send_signal(signal.SIGTERM)
                _, error = process.communicate(timeout=5)
            self.assertIn("pass_exit=7", error)
            self.assertIn("stopped", error)

    def test_poll_output_is_suppressed_but_status_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake-afk"
            fake.write_text('#!/usr/bin/env bash\n'
                            'printf "SYNTHETIC_STDOUT_MARKER\\n"\n'
                            'printf "SYNTHETIC_STDERR_MARKER\\n" >&2\n'
                            'printf "done\\n" > "$TEST_LOG"\n'
                            'exit 7\n')
            fake.chmod(0o755)
            wrapper = Path(directory) / "poll-supervisor.sh"
            wrapper.write_text((ROOT / "poll-supervisor.sh").read_text().replace(
                "/opt/afk/bin/agent-flow-kit", str(fake)))
            env = dict(os.environ, AFK_POLL_CADENCE_SECONDS="30",
                       TEST_LOG=str(Path(directory) / "events"))
            process = subprocess.Popen(["bash", str(wrapper)], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True)
            try:
                deadline = time.monotonic() + 3
                while not (Path(directory) / "events").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((Path(directory) / "events").exists())
                time.sleep(0.1)
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(output, "")
                self.assertNotIn("SYNTHETIC_STDOUT_MARKER", error)
                self.assertNotIn("SYNTHETIC_STDERR_MARKER", error)
                self.assertRegex(error, r"poll-supervisor: pass_exit=7 at=\d{4}-\d{2}-\d{2}T")
                self.assertIn("poll-supervisor: stopped", error)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)

    def test_term_kills_owned_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            process = self.supervisor(directory,
                'sleep 30 &\nprintf "%s\\n" "$!" > "$TEST_LOG"\nwait\n')
            try:
                deadline = time.monotonic() + 3
                while not (Path(directory) / "events").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                pid = int((Path(directory) / "events").read_text())
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=5)
                self.assertIsNotNone(process.returncode)
                # A reaped process may briefly remain a zombie in a container.
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                       text=True, capture_output=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
