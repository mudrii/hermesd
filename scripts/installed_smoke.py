#!/usr/bin/env python3
"""Installed-artifact smoke test for a built hermesd distribution (audit CI-19).

Run with the interpreter of a virtualenv that installed the wheel or sdist:

    python scripts/installed_smoke.py

The script imports hermesd only from the installing environment (never a
source checkout), runs every command from a temporary working directory, and
verifies behavior that ``--version`` cannot:

* text and JSON snapshots render for populated and empty Hermes homes
* panel selection is honored in both output modes
* snapshots never write under the Hermes home (read-only invariant)
* the distribution ships its runtime modules, ``py.typed`` marker, exact
  runtime dependency pins, and metadata matching ``--version``
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path

REQUIRED_DIST_FILES = (
    "hermesd/__main__.py",
    "hermesd/collector.py",
    "hermesd/collect/system.py",
    "hermesd/panels/overview.py",
    "hermesd/py.typed",
)


def _check(condition: object, message: str) -> None:
    """Fail the smoke run; unlike ``assert`` this survives ``python -O``."""
    if not condition:
        raise SystemExit(message)


def hermesd(*args: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the installed CLI from an isolated cwd, never the source tree."""
    command = [sys.executable, "-I", "-m", "hermesd"]
    if home is not None:
        command += ["--hermes-home", str(home)]
    with tempfile.TemporaryDirectory(prefix="hermesd-smoke-cwd-") as working_directory:
        return subprocess.run(
            [*command, *args],
            capture_output=True,
            text=True,
            cwd=working_directory,
            check=False,
        )


def build_home(root: Path, *, populated: bool) -> Path:
    home = root / ".hermes"
    home.mkdir(parents=True)
    if not populated:
        return home
    for name in ("logs", "sessions", "skills", "memories", "cron", "cron/output"):
        (home / name).mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n  default: smoke-model\n  provider: smoke-provider\ndisplay:\n  skin: default\n",
        encoding="utf-8",
    )
    (home / "logs" / "agent.log").write_text(
        "2026-09-13 12:00:00,000 - hermes - INFO - smoke log line one\n"
        "2026-09-13 12:00:01,000 - hermes - INFO - smoke log line two\n",
        encoding="utf-8",
    )
    skill_dir = home / "skills" / "notes" / "readme"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: readme\ndescription: Smoke fixture skill\n---\nBody\n",
        encoding="utf-8",
    )
    return home


def snapshot_state(home: Path) -> dict[Path, str]:
    state: dict[Path, str] = {}
    for path in sorted(home.rglob("*")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
        state[path.relative_to(home)] = digest
    return state


def check_missing_home(home: Path) -> None:
    """Verify the installed CLI rejects an absent Hermes home without creating it."""
    result = hermesd("--snapshot", "--no-color", home=home)
    _check(result.returncode == 1, f"missing home: unexpected exit {result.returncode}")
    _check(not result.stdout, f"missing home: unexpected output: {result.stdout!r}")
    _check("does not exist" in result.stderr, f"missing home: wrong error: {result.stderr!r}")
    _check(not home.exists(), "missing home: hermesd created the absent home")


def check_snapshots(home: Path, label: str, *, populated: bool) -> None:
    before = snapshot_state(home)

    overview = hermesd("--snapshot", "--no-color", home=home)
    _check(overview.returncode == 0, f"{label}: overview failed: {overview.stderr}")
    _check(overview.stdout.strip(), f"{label}: empty overview output")

    full_json = hermesd("--snapshot-format", "json", home=home)
    _check(full_json.returncode == 0, f"{label}: full JSON failed: {full_json.stderr}")
    payload = json.loads(full_json.stdout)
    _check(payload["panel_num"] is None, f"{label}: unexpected panel annotation")
    state = payload.get("state")
    _check(isinstance(state, dict), f"{label}: JSON payload missing state")
    if populated:
        _check(state["config"]["model"] == "smoke-model", f"{label}: fixture model not collected")
        _check(
            state["config"]["provider"] == "smoke-provider",
            f"{label}: fixture provider not collected",
        )
        _check(
            state["skills_memory"]["skills"]
            == [
                {
                    "category": "notes",
                    "description": "Smoke fixture skill",
                    "name": "readme",
                }
            ],
            f"{label}: fixture skill not collected",
        )
        _check(
            [line["message"] for line in state["logs"]["agent_lines"]]
            == [
                "smoke log line one",
                "smoke log line two",
            ],
            f"{label}: fixture logs not collected",
        )

    panel_json = hermesd("--snapshot-format", "json", "--snapshot-panel", "12", home=home)
    _check(panel_json.returncode == 0, f"{label}: panel JSON failed: {panel_json.stderr}")
    payload = json.loads(panel_json.stdout)
    _check(payload["panel_num"] == 12, f"{label}: panel number not annotated")
    _check(payload["panel_name"] == "Operations", f"{label}: wrong panel name")

    panel_text = hermesd("--snapshot-panel", "8", "--no-color", home=home)
    _check(panel_text.returncode == 0, f"{label}: panel text failed: {panel_text.stderr}")
    _check("[8] Logs" in panel_text.stdout, f"{label}: wrong text panel output")
    if populated:
        _check("smoke log line two" in panel_text.stdout, f"{label}: fixture log not rendered")

    after = snapshot_state(home)
    changed = {key for key in before if before[key] != after.get(key)} | (set(before) ^ set(after))
    _check(
        not changed,
        f"{label}: hermesd wrote under the Hermes home (read-only invariant): {sorted(changed)}",
    )


def check_distribution() -> str:
    version_output = hermesd("--version")
    _check(version_output.returncode == 0, f"--version failed: {version_output.stderr}")
    _check(str(version_output.stdout).strip(), "--version printed nothing")

    dist_version = metadata.version("hermesd")
    _check(
        dist_version in version_output.stdout,
        f"--version output {version_output.stdout!r} does not report metadata version "
        f"{dist_version}",
    )

    dist_files = {str(path) for path in (metadata.files("hermesd") or [])}
    missing = [name for name in REQUIRED_DIST_FILES if name not in dist_files]
    _check(not missing, f"distribution is missing required files: {missing}")

    runtime_requirements = [
        requirement
        for requirement in (metadata.requires("hermesd") or [])
        if "extra ==" not in requirement
    ]
    _check(runtime_requirements, "distribution metadata lists no runtime requirements")
    unpinned = [
        requirement
        for requirement in runtime_requirements
        if "==" not in requirement.split(";", 1)[0]
    ]
    _check(not unpinned, f"runtime requirements are not exact pins: {unpinned}")
    return dist_version


def main() -> int:
    import sysconfig

    import hermesd

    # The import must come from this environment's site-packages, never from
    # a source checkout that happens to be on sys.path.
    purelib = Path(sysconfig.get_paths()["purelib"]).resolve()
    origin = Path(hermesd.__file__).resolve()
    _check(
        origin.is_relative_to(purelib),
        f"hermesd imported from {origin}, not the installing environment's {purelib}",
    )

    version = check_distribution()

    with tempfile.TemporaryDirectory(prefix="hermesd-smoke-home-") as directory:
        root = Path(directory)
        check_missing_home(root / "missing")
        check_snapshots(
            build_home(root / "populated", populated=True),
            "populated home",
            populated=True,
        )
        check_snapshots(build_home(root / "empty", populated=False), "empty home", populated=False)

    print(f"installed smoke OK: hermesd {version} (text/JSON snapshots, panels, read-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
