#!/usr/bin/env python3
"""Offline packaging and serial supervisor checks."""
import importlib.util
import io
import json
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
                         "DOCKER_HOST=unix:///run/docker.sock", "source_git -c tar.umask=0022 archive --format=tar",
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
            for invalid in ("0", "-2", "abc", "1.5", "86401", "99999999999999999999999999"):
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
                # The leader must drain the first pass before the next starts;
                # process startup and group scanning have no fixed wall time.
                deadline = time.monotonic() + 8
                events = []
                while len(events) < 3 and time.monotonic() < deadline:
                    log = Path(directory) / "events"
                    if log.exists():
                        events = log.read_text().splitlines()
                    if len(events) < 3:
                        time.sleep(0.02)
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


class PostPrFixTests(unittest.TestCase):
    def fixture(self):
        return (ROOT / "test-local.sh").read_text()

    def test_planned_symlink_graph(self):
        spec = importlib.util.spec_from_file_location("safe_extract", ROOT / "safe-extract.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cases = (("escape", [("a", "."), ("link", "a/../outside")], False),
                 ("cycle", [("a", "b"), ("b", "a")], False),
                 ("valid", [("a", "dir"), ("link", "a/file")], True))
        for label, links, valid in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "in.tgz"
                with tarfile.open(archive, "w:gz") as tar:
                    member = tarfile.TarInfo("root/dir/file")
                    member.size = 1
                    tar.addfile(member, io.BytesIO(b"x"))
                    for name, target in links:
                        member = tarfile.TarInfo("root/" + name)
                        member.type = tarfile.SYMTYPE
                        member.linkname = target
                        tar.addfile(member)
                out = Path(directory) / "out"
                if valid:
                    module.extract(archive, out)
                    self.assertEqual((out / "link").read_bytes(), b"x")
                else:
                    with self.assertRaises(ValueError):
                        module.extract(archive, out)
                    self.assertFalse(out.exists())

    def test_curl_disables_synthetic_config_first(self):
        line = next(line for line in self.fixture().splitlines()
                    if 'env -i PATH="$PATH" curl ' in line)
        command = line.strip().removeprefix("if ! ").removesuffix("; then")
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            (temp / ".curlrc").write_text("trace = synthetic-marker\n")
            fake = temp / "curl"
            fake.write_text('#!/usr/bin/env bash\n'
                            'printf "%s\\n" "$1" > "' + str(temp / "first") + '"\n'
                            'if [[ "$1" != --disable && -f "' + str(temp / ".curlrc") +
                            '" ]]; then touch "' + str(temp / "config-read") + '"; fi\n')
            fake.chmod(0o755)
            stage = temp / "stage"
            (stage / "inputs").mkdir(parents=True)
            script = (f'PATH="{temp}:$PATH"; stage="{stage}"; '
                      f'asset=archive; url=unused; {command}')
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((temp / "first").read_text().strip(), "--disable")
            self.assertFalse((temp / "config-read").exists())

    def test_stop_requires_running_then_clean_exit(self):
        stop = "stop_state() {\n" + self.fixture().split("stop_state() {\n", 1)[1].split(
            "# EXIT trap removes", 1)[0]
        running = dict(Running=True, Status="running")
        clean = dict(Running=False, Status="exited", ExitCode=0,
                     OOMKilled=False, Dead=False, Error="")
        cases = (("clean", running, clean, True),
                 ("sigterm", running, dict(clean, ExitCode=143), True),
                 ("already-stopped", clean, clean, False),
                 ("sigkill", running, dict(clean, ExitCode=137), False),
                 ("unexpected", running, dict(clean, ExitCode=1), False),
                 ("oom", running, dict(clean, OOMKilled=True), False),
                 ("error", running, dict(clean, Error="synthetic"), False))
        for label, before, after, accepted in cases:
            with self.subTest(label=label):
                script = ("set -euo pipefail\ncontainer_id=synthetic; stopped=0\n"
                          f"before='{json.dumps(before)}'; after='{json.dumps(after)}'\n"
                          'dock() { if [[ "$1" == stop ]]; then stopped=1; '
                          'elif (( stopped )); then printf "%s\\n" "$after"; '
                          'else printf "%s\\n" "$before"; fi; }\n' + stop)
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
                self.assertEqual("orca_graceful_stop=true" in result.stdout, accepted)

    def test_local_git_umask_and_optimized_refusals(self):
        fixture = self.fixture()
        source_git = "source_git() {\n" + fixture.split("source_git() {\n", 1)[1].split("\n[[", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            executable = repo / "bin" / "agent-flow-kit"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", "commit", "-qm", "pinned"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "tar.umask", "0077"], check=True)
            cmd = source_git + '\nsource_git -c tar.umask=0022 archive --format=tar HEAD bin'
            result = subprocess.run(["bash", "-c", cmd], env=dict(os.environ, source_repo=str(repo)),
                                    capture_output=True, check=True)
            with tarfile.open(fileobj=io.BytesIO(result.stdout)) as tar:
                self.assertEqual(tar.getmember("bin/agent-flow-kit").mode & 0o777, 0o755)
            optimized = dict(os.environ, PYTHONOPTIMIZE="1")
            inventory = fixture.split("/usr/bin/python3 -I -c 'import sys\n", 1)[1].split("' || {", 1)[0]
            result = subprocess.run(["python3", "-c", "import sys\n" + inventory],
                                    input=b"", capture_output=True, env=optimized)
            self.assertNotEqual(result.returncode, 0)
            readability = fixture.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
            result = subprocess.run(["python3", "-c", readability, str(repo / "empty")],
                                    capture_output=True, env=optimized)
            self.assertNotEqual(result.returncode, 0)
            comparison = fixture.split("/usr/bin/python3 -I -c 'import hashlib", 1)[1].split("' \"$stage", 1)[0]
            result = subprocess.run(["python3", "-c", "import hashlib" + comparison,
                                     str(repo), str(repo)], input=b"100644 blob abc\tbin/missing\0",
                                    capture_output=True, env=optimized)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotRegex(fixture, r"\bassert\s")

    def test_cleanup_on_later_mktemp_failure_and_unique_names(self):
        fixture = self.fixture()
        early = "stage=''; scratch=''; client_cfg=''" + fixture.split(
            "stage=''; scratch=''; client_cfg=''", 1)[1].split('mkdir -m 0755', 1)[0]
        names = "nonce=$(/usr/bin/python3 -I -c" + fixture.split("nonce=$(/usr/bin/python3 -I -c", 1)[1].split(
            "container_id=''", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            script = f'''set -euo pipefail
TEST_ROOT="{directory}"; TEST_COUNTER="{directory}/count"
mktemp() {{
  local n=0
  [[ ! -f "$TEST_COUNTER" ]] || n=$(cat "$TEST_COUNTER")
  n=$((n+1)); printf '%s' "$n" > "$TEST_COUNTER"
  if (( n == 3 )); then return 9; fi
  mkdir "$TEST_ROOT/alloc$n"; printf '%s\\n' "$TEST_ROOT/alloc$n"
}}
{early}
scratch=$(mktemp -d)
client_cfg=$(mktemp -d)
'''
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(directory) / "alloc1").exists())
            self.assertFalse((Path(directory) / "alloc2").exists())
            result = subprocess.run(["bash", "-c", names + '\nfirst="$container_name"; '
                                     + names + '\ntest "$first" != "$container_name"'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def wait_for(self, path):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), str(path))

    def launch_supervisor(self, directory, body, cadence="30", extras=None):
        temp = Path(directory)
        fake = temp / "fake-afk"
        fake.write_text("#!/usr/bin/env bash\n" + body)
        fake.chmod(0o755)
        copy = temp / "poll-supervisor.sh"
        source = (ROOT / "poll-supervisor.sh").read_text().replace(
            "/opt/afk/bin/agent-flow-kit", str(fake))
        source = source.replace("set -u\n", "set -u\nexport TEST_PARENT=$$\n", 1)
        copy.write_text(source)
        copy.chmod(0o755)
        env = dict(os.environ, AFK_POLL_CADENCE_SECONDS=cadence,
                   TEST_LOG=str(temp / "events"))
        if extras:
            env.update(extras)
        return subprocess.Popen(["bash", str(copy)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_term_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            fakebin = temp / "bin"
            fakebin.mkdir()
            date = fakebin / "date"
            date.write_text('#!/usr/bin/env bash\n'
                            'if [[ "$1" == +%s ]]; then kill -TERM "$TEST_PARENT"; fi\n'
                            'exec /usr/bin/date "$@"\n')
            date.chmod(0o755)
            process = self.launch_supervisor(directory, 'touch "$TEST_LOG"\n',
                                             extras=dict(PATH=str(fakebin) + ":" + os.environ["PATH"]))
            output, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(output, "")
            self.assertFalse((temp / "events").exists())
            self.assertIn("stopped", error)

    def test_term_during_group_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            fifo = temp / "release"
            os.mkfifo(fifo)
            fakebin = temp / "bin"
            fakebin.mkdir()
            setsid = fakebin / "setsid"
            setsid.write_text('#!/usr/bin/env bash\n'
                              'touch "$TEST_READY"\n'
                              'read -r _ < "$TEST_FIFO"\n'
                              'exec /usr/bin/setsid "$@"\n')
            setsid.chmod(0o755)
            process = self.launch_supervisor(directory, 'touch "$TEST_LOG"\n', extras=dict(
                PATH=str(fakebin) + ":" + os.environ["PATH"],
                TEST_READY=str(temp / "ready"), TEST_FIFO=str(fifo)))
            try:
                self.wait_for(temp / "ready")
                process.send_signal(signal.SIGTERM)
                with fifo.open("w") as release:
                    release.write("go\n")
                output, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(output, "")
                self.assertFalse((temp / "events").exists())
                self.assertIn("stopped", error)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)

    def test_term_during_sleep_and_repeated_term(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            fakebin = temp / "bin"
            fakebin.mkdir()
            sleep = fakebin / "sleep"
            sleep.write_text('#!/usr/bin/env bash\n'
                             'if (( $1 > 1000 )); then\n'
                             '  touch "$TEST_READY"\n'
                             '  exec /usr/bin/sleep 30\n'
                             'fi\n'
                             'exec /usr/bin/sleep "$@"\n')
            sleep.chmod(0o755)
            process = self.launch_supervisor(directory,
                'printf "poll\\n" >> "$TEST_LOG"\nexit 7\n', cadence="86400", extras=dict(
                    PATH=str(fakebin) + ":" + os.environ["PATH"],
                    TEST_READY=str(temp / "sleep-ready")))
            try:
                self.wait_for(temp / "sleep-ready")
                process.send_signal(signal.SIGTERM)
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(output, "")
                self.assertEqual((temp / "events").read_text().splitlines(), ["poll"])
                self.assertIn("pass_exit=7", error)
                self.assertIn("stopped", error)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)

    def test_repeated_term_reaps_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            process = self.launch_supervisor(directory,
                'sleep 30 &\nprintf "%s\\n" "$!" > "$TEST_LOG"\nwait\n')
            try:
                self.wait_for(temp / "events")
                deadline = time.monotonic() + 5
                pid = None
                while pid is None and time.monotonic() < deadline:
                    try:
                        pid = int((temp / "events").read_text().strip())
                    except ValueError:
                        time.sleep(0.01)
                self.assertIsNotNone(pid)
                process.send_signal(signal.SIGTERM)
                process.send_signal(signal.SIGTERM)
                _, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                       text=True, capture_output=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)

    def test_host_python_ignores_synthetic_startup_paths(self):
        fixture = self.fixture()
        # Every host-side Python site in the helper uses the fixed interpreter
        # and isolated mode; the one in-container readiness probe is separate.
        self.assertEqual(fixture.count("/usr/bin/python3 -I"), 7)
        self.assertEqual(len(re.findall(r"(?<![\w/])python3\b", fixture)), 1)
        inventory = fixture.split("/usr/bin/python3 -I -c 'import sys\n", 1)[1].split("' || {", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            startup = root / "sitecustomize.py"
            marker = root / "startup-ran"
            startup.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('ran')\n")
            env = dict(os.environ, PYTHONPATH=str(root))
            for cwd in (root, root / "child"):
                cwd.mkdir(exist_ok=True)
                result = subprocess.run(
                    ["/usr/bin/python3", "-I", "-c", "import sys\n" + inventory],
                    input=b"", cwd=cwd, env=env, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists())

    def test_term_ignoring_group_has_bounded_forced_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            process = self.launch_supervisor(directory,
                'trap "" TERM\n'
                'bash -c \'trap "" TERM; echo ready > "$TEST_DESC_READY"; exec /usr/bin/sleep 30\' &\n'
                'desc=$!\n'
                'while [[ ! -f "$TEST_DESC_READY" ]]; do /usr/bin/sleep 0.01; done\n'
                'printf "%s\\n" "$desc" > "$TEST_LOG"\n'
                'wait "$desc"\n',
                extras=dict(TEST_DESC_READY=str(temp / "desc-ready")))
            try:
                self.wait_for(temp / "events")
                descendant = int((temp / "events").read_text().strip())
                process.send_signal(signal.SIGTERM)
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 1, error)
                self.assertEqual(output, "")
                self.assertIn("cancellation_escalated=kill", error)
                self.assertIn("stop_outcome=forced_or_unreaped", error)
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)],
                                       text=True, capture_output=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
                self.assertEqual((temp / "events").read_text().splitlines(), [str(descendant)])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)

    def test_leader_exits_first_but_ignoring_descendant_is_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            process = self.launch_supervisor(directory,
                "trap 'exit 0' TERM\n"
                'bash -c \'trap "" TERM; echo ready > "$TEST_DESC_READY"; exec /usr/bin/sleep 30\' &\n'
                'desc=$!\n'
                'while [[ ! -f "$TEST_DESC_READY" ]]; do /usr/bin/sleep 0.01; done\n'
                'printf "%s\\n" "$desc" > "$TEST_LOG"\n'
                'wait "$desc"\n',
                extras=dict(TEST_DESC_READY=str(temp / "desc-ready")))
            descendant = None
            start = None
            try:
                self.wait_for(temp / "events")
                descendant = int((temp / "events").read_text().strip())
                start = Path(f"/proc/{descendant}/stat").read_text().rsplit(") ", 1)[1].split()[19]
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 1, error)
                self.assertEqual(output, "")
                self.assertIn("cancellation_escalated=kill", error)
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)],
                                       text=True, capture_output=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
                self.assertEqual((temp / "events").read_text().splitlines(), [str(descendant)])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)
                if descendant and start and Path(f"/proc/{descendant}/stat").exists():
                    fields = Path(f"/proc/{descendant}/stat").read_text().rsplit(") ", 1)[1].split()
                    if fields[19] == start and not fields[0].startswith("Z"):
                        os.kill(descendant, signal.SIGKILL)

    def test_natural_leader_exit_waits_for_descendant_before_next_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            process = self.launch_supervisor(directory,
                'printf "start\\n" >> "$TEST_LOG"\n'
                "bash -c 'trap \"\" TERM; echo $$ > \"$TEST_DESC_PID\"; exec /usr/bin/sleep 20' &\n"
                'while [[ ! -s "$TEST_DESC_PID" ]]; do /usr/bin/sleep 0.01; done\n'
                'touch "$TEST_CLI_DONE"\nexit 0\n', cadence="1", extras=dict(
                    TEST_DESC_PID=str(temp / "desc-pid"), TEST_CLI_DONE=str(temp / "cli-done")))
            descendant = None
            start = None
            try:
                self.wait_for(temp / "cli-done")
                descendant = int((temp / "desc-pid").read_text().strip())
                start = Path(f"/proc/{descendant}/stat").read_text().rsplit(") ", 1)[1].split()[19]
                # A second cadence passes while the first pass's child is live.
                time.sleep(1.3)
                self.assertEqual((temp / "events").read_text().splitlines(), ["start"])
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 1, error)
                self.assertEqual(output, "")
                self.assertIn("cancellation_escalated=kill", error)
                self.assertEqual((temp / "events").read_text().splitlines(), ["start"])
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)],
                                       text=True, capture_output=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)
                if descendant and start and Path(f"/proc/{descendant}/stat").exists():
                    fields = Path(f"/proc/{descendant}/stat").read_text().rsplit(") ", 1)[1].split()
                    if fields[19] == start and not fields[0].startswith("Z"):
                        os.kill(descendant, signal.SIGKILL)

    def test_stalled_wall_clock_cannot_extend_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            fakebin = temp / "bin"
            fakebin.mkdir()
            date = fakebin / "date"
            date.write_text('#!/usr/bin/env bash\n'
                            'if [[ "$1" == +%s ]]; then printf "1000000\\n"; exit; fi\n'
                            'exec /usr/bin/date "$@"\n')
            date.chmod(0o755)
            process = self.launch_supervisor(directory,
                'trap "" TERM\ntouch "$TEST_LOG"\nexec /usr/bin/sleep 30\n',
                extras=dict(PATH=str(fakebin) + ":" + os.environ["PATH"]))
            try:
                self.wait_for(temp / "events")
                start = time.monotonic()
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=8)
                self.assertLess(time.monotonic() - start, 7)
                self.assertEqual(process.returncode, 1, error)
                self.assertEqual(output, "")
                self.assertIn("cancellation_escalated=kill", error)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)

    def test_unverified_startup_and_foreign_group_are_not_signaled(self):
        source = (ROOT / "poll-supervisor.sh").read_text()
        prelaunch = source.split("launch_owned() {", 1)[0]
        probe = (prelaunch + '\nchild=$$; group_ready=1; leader_start=wrong; '
                 'kill() { printf "signal\\n" >> "$TEST_LOG"; }; '
                 'stop_owned; test ! -e "$TEST_LOG"\n')
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            result = subprocess.run(["bash", "-c", probe], env=dict(
                os.environ, TEST_LOG=str(temp / "signals")), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            fakebin = temp / "bin"
            fakebin.mkdir()
            fifo = temp / "release"
            os.mkfifo(fifo)
            setsid = fakebin / "setsid"
            setsid.write_text('#!/usr/bin/env bash\n'
                              'touch "$TEST_READY"\n'
                              'read -r _ < "$TEST_FIFO"\n'
                              'exec /usr/bin/setsid "$@"\n')
            setsid.chmod(0o755)
            date = fakebin / "date"
            date.write_text('#!/usr/bin/env bash\n'
                            'if [[ "$1" == +%s ]]; then printf "1000000\\n"; exit; fi\n'
                            'exec /usr/bin/date "$@"\n')
            date.chmod(0o755)
            process = self.launch_supervisor(directory, 'touch "$TEST_LOG"\n', extras=dict(
                PATH=str(fakebin) + ":" + os.environ["PATH"],
                TEST_READY=str(temp / "ready"), TEST_FIFO=str(fifo)))
            try:
                self.wait_for(temp / "ready")
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 1, error)
                self.assertEqual(output, "")
                self.assertFalse((temp / "events").exists())
                self.assertIn("cancellation_startup_unverified", error)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)

if __name__ == "__main__":
    unittest.main()
