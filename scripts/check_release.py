"""Install one wheel in a new environment and exercise its public entry points."""

import argparse
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
PORTABLE_TESTS = (
    "test_store",
    "test_peers",
    "test_peer_tasks",
    "test_inspection",
    "test_file_locks",
    "test_portable_wait",
    "test_mcp",
    "test_peer_mcp",
    "test_claude",
    "test_workbench",
    "test_monitor_portable",
)


def check_workbench(command, directory, env):
    log = directory / "startup.log"
    with log.open("wb") as output:
        process = subprocess.Popen(
            [*command, "ui", "--no-open"],
            cwd=directory,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                started = log.read_text(encoding="utf-8", errors="replace")
                url = re.search(r"http://127[.]0[.]0[.]1:(\d+)/", started)
                if url:
                    break
                if process.poll() is not None:
                    raise RuntimeError(f"Workbench exited during startup: {started}")
                time.sleep(0.05)
            else:
                raise TimeoutError("Workbench did not print a loopback URL")
            http = HTTPConnection("127.0.0.1", int(url.group(1)), timeout=5)
            try:
                http.request("GET", "/api/teams")
                response = http.getresponse()
                if response.status != 200 or json.loads(response.read())["teams"] != []:
                    raise RuntimeError(
                        "Expected an empty team directory in the new workbench"
                    )
            finally:
                http.close()
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory", type=Path, help="Directory containing exactly one wheel"
    )
    parser.add_argument("--sha256", help="Expected SHA-256 from the build job")
    parser.add_argument(
        "--arch", choices=["arm64", "x64"], help="Expected runner architecture"
    )
    parser.add_argument(
        "--wheelhouse", type=Path, help="Install dependencies from local wheels offline"
    )
    args = parser.parse_args()
    wheels = sorted(args.directory.resolve().glob("*.whl"))
    if len(wheels) != 1 or not wheels[0].name.endswith("-py3-none-any.whl"):
        parser.error("Expected exactly one platform-independent wheel")
    wheel = wheels[0]
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if args.sha256 and args.sha256.lower() != digest:
        parser.error("Wheel SHA-256 differs from the build job")
    host = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(host, host)
    if args.arch and arch != args.arch:
        parser.error(f"Expected {args.arch}, found {host}")
    print(
        f"{platform.system()} {host} · Python {platform.python_version()} · {wheel.name} · sha256 {digest}",
        flush=True,
    )
    env = dict(
        os.environ,
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        PYTHONDONTWRITEBYTECODE="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
    )
    env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="native-team-release-") as temp:
        root = Path(temp).resolve()
        venv = root / "venv"

        def run(command, cwd=root, timeout=450, **options):
            return subprocess.run(
                command, cwd=cwd, env=env, check=True, timeout=timeout, **options
            )

        run([sys.executable, "-m", "venv", str(venv)])
        windows = os.name == "nt"
        bin_dir = venv / ("Scripts" if windows else "bin")
        python = bin_dir / ("python.exe" if windows else "python")
        entry = bin_dir / ("native-team.exe" if windows else "native-team")
        sources = (
            ["--no-index", "--find-links", str(args.wheelhouse.resolve())]
            if args.wheelhouse
            else []
        )
        formats = ["--only-binary=:all:"]
        # Upstream stopped publishing Intel Mac wheels in 49. Keep the same
        # cryptography release on every OS. An offline wheelhouse supplies its own wheel.
        intel_build = sys.platform == "darwin" and arch == "x64" and not args.wheelhouse
        if intel_build:
            formats.append("--no-binary=cryptography")
        run(
            [
                python,
                "-m",
                "pip",
                "install",
                *formats,
                *sources,
                "-c",
                ROOT / "requirements.lock",
                wheel,
            ],
            timeout=1200 if intel_build else 450,
        )
        run([python, "-m", "pip", "check"])
        run(
            [
                python,
                "-I",
                "-c",
                "import sys, native_team; from pathlib import Path; "
                "p=Path(native_team.__file__).resolve(); "
                "assert p.is_relative_to(Path(sys.prefix).resolve()), p; "
                "from importlib.resources import files; "
                "assert files('native_team.web_assets').joinpath('index.html').is_file(); "
                "from cryptography.hazmat.primitives import hashes; import hashlib; "
                "h=hashes.Hash(hashes.SHA256()); h.update(b'native-team'); "
                "assert h.finalize() == hashlib.sha256(b'native-team').digest()",
            ]
        )
        command = [entry, "--state", str(root / "研究 #100%")]
        run([*command, "migrate"], stdout=subprocess.PIPE)
        rows = run([*command, "teams", "--json"], stdout=subprocess.PIPE)
        if json.loads(rows.stdout) != []:
            raise RuntimeError("New install should list zero teams")
        check_workbench(command, root, env)
        # Only the fixture modules are importable from cwd. Native Team is installed in the venv.
        run([python, "-B", "-m", "unittest", "-v", *PORTABLE_TESTS], cwd=ROOT / "tests")
    print("Installed-wheel compatibility checks passed.", flush=True)


if __name__ == "__main__":
    main()
