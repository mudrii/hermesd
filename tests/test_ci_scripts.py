from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts import check_wheel_pins, installed_smoke, pip_audit_gate


def _run_audit_gate(
    tmp_path: Path,
    *,
    exit_code: int,
    report_text: str | None,
) -> tuple[subprocess.CompletedProcess[str], list[Path]]:
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    executable = executable_dir / "pip-audit"
    executable.write_text(
        f"#!{sys.executable}\n"
        """import os
import sys
from pathlib import Path

report = Path(sys.argv[sys.argv.index("--output") + 1])
with Path(os.environ["FAKE_ATTEMPTS"]).open("a", encoding="utf-8") as handle:
    handle.write(f"{report}\\n")
if "FAKE_REPORT" in os.environ:
    report.write_text(os.environ["FAKE_REPORT"], encoding="utf-8")
raise SystemExit(int(os.environ["FAKE_EXIT_CODE"]))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    attempts_path = tmp_path / "attempts.txt"
    environment = os.environ | {
        "FAKE_ATTEMPTS": str(attempts_path),
        "FAKE_EXIT_CODE": str(exit_code),
        "PATH": str(executable_dir),
    }
    if report_text is not None:
        environment["FAKE_REPORT"] = report_text
    completed = subprocess.run(
        [sys.executable, str(Path("scripts/pip_audit_gate.py").resolve())],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    report_paths = [Path(line) for line in attempts_path.read_text(encoding="utf-8").splitlines()]
    return completed, report_paths


@pytest.mark.parametrize(
    ("exit_code", "report_text"),
    [
        (1, None),
        (1, ""),
        (1, "{}"),
        (1, "not json"),
        (0, "not json"),
        (
            1,
            '{"dependencies":[{"name":"package","vulns":[{"id":"PYSEC-1","fix_versions":[]}]}]}',
        ),
    ],
)
def test_pip_audit_invalid_or_inconsistent_report_is_a_scanner_failure(
    tmp_path: Path,
    exit_code: int,
    report_text: str | None,
) -> None:
    completed, report_paths = _run_audit_gate(
        tmp_path,
        exit_code=exit_code,
        report_text=report_text,
    )

    assert completed.returncode == 2
    assert len(report_paths) == pip_audit_gate.MAX_ATTEMPTS
    assert "infrastructure failure" in completed.stderr


def test_pip_audit_rejects_skipped_third_party_dependency(
    tmp_path: Path,
) -> None:
    report = {
        "dependencies": [
            {"name": "local-package", "skip_reason": "could not determine package version"}
        ],
        "fixes": [],
    }

    completed, report_paths = _run_audit_gate(
        tmp_path,
        exit_code=0,
        report_text=json.dumps(report),
    )

    assert completed.returncode == 2
    assert len(report_paths) == pip_audit_gate.MAX_ATTEMPTS
    assert "skipped local-package" in completed.stderr
    assert "audit skipped unexpected dependencies: local-package" in completed.stderr
    assert "no known vulnerabilities" not in completed.stdout


def test_pip_audit_reports_expected_local_project_skip_without_claiming_complete_scan(
    tmp_path: Path,
) -> None:
    report = {
        "dependencies": [
            {"name": "hermesd", "skip_reason": "could not find project on PyPI"},
            {"name": "rich", "version": "14.3.3", "vulns": []},
        ],
        "fixes": [],
    }

    completed, report_paths = _run_audit_gate(
        tmp_path,
        exit_code=0,
        report_text=json.dumps(report),
    )

    assert completed.returncode == 0
    assert len(report_paths) == 1
    assert "skipped hermesd" in completed.stderr
    assert "no known vulnerability findings; 1 dependency scan(s) skipped" in completed.stdout


def test_pip_audit_valid_findings_remain_blocking(tmp_path: Path) -> None:
    report = {
        "dependencies": [
            {
                "name": "vulnerable-package",
                "version": "1.0",
                "vulns": [{"id": "PYSEC-1", "fix_versions": ["1.1"]}],
            }
        ],
        "fixes": [],
    }

    completed, report_paths = _run_audit_gate(
        tmp_path,
        exit_code=1,
        report_text=json.dumps(report),
    )

    assert completed.returncode == 1
    assert len(report_paths) == 1
    assert "VULNERABLE vulnerable-package==1.0 PYSEC-1 fix: 1.1" in completed.stdout


def test_pip_audit_temporary_report_directory_is_removed(
    tmp_path: Path,
) -> None:
    completed, report_paths = _run_audit_gate(
        tmp_path,
        exit_code=0,
        report_text='{"dependencies": [], "fixes": []}',
    )

    assert completed.returncode == 0
    assert len(report_paths) == 1
    assert not report_paths[0].parent.exists()


def _write_wheel(path: Path, requirements: list[str]) -> None:
    metadata = "Metadata-Version: 2.5\nName: hermesd\nVersion: 1\n"
    metadata += "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements)
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("hermesd-1.dist-info/METADATA", metadata)


def _write_lock(path: Path) -> None:
    path.write_text(
        """
[[package]]
name = "pydantic"
version = "2.13.4"

[[package]]
name = "pyyaml"
version = "6.0.3"

[[package]]
name = "rich"
version = "14.3.3"
""".lstrip(),
        encoding="utf-8",
    )


def test_wheel_pin_gate_rejects_duplicate_runtime_requirements(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "dist").mkdir()
    _write_lock(tmp_path / "uv.lock")
    _write_wheel(
        tmp_path / "dist" / "hermesd-1-py3-none-any.whl",
        [
            "pydantic==2.13.4",
            "pyyaml==6.0.3",
            "rich>=14",
            "rich==14.3.3",
        ],
    )
    monkeypatch.chdir(tmp_path)

    assert check_wheel_pins.main() == 1
    assert "duplicate runtime dependency in wheel: rich" in capsys.readouterr().err


def test_requirement_marker_value_named_extra_is_not_treated_as_an_optional_extra() -> None:
    assert check_wheel_pins.split_requirement('ruff==0.16.6; extra == "dev"') is None
    assert check_wheel_pins.split_requirement('rich==14.3.3; platform_release == "extra"') == (
        "rich",
        "==14.3.3",
    )


def test_installed_smoke_removes_each_temporary_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    working_directories: list[Path] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        working_directories.append(Path(str(kwargs["cwd"])))
        return subprocess.CompletedProcess([], 0, "hermesd 1\n", "")

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(installed_smoke.subprocess, "run", fake_run)

    assert installed_smoke.hermesd("--version").returncode == 0
    assert len(working_directories) == 1
    assert not working_directories[0].exists()


def test_installed_smoke_checks_a_missing_home_without_creating_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing_home = tmp_path / "does-not-exist"
    calls: list[tuple[tuple[str, ...], Path | None]] = []

    def fake_hermesd(*args: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append((args, home))
        return subprocess.CompletedProcess([], 1, "", f"Error: {home} does not exist\n")

    monkeypatch.setattr(installed_smoke, "hermesd", fake_hermesd)

    installed_smoke.check_missing_home(missing_home)

    assert calls == [(("--snapshot", "--no-color"), missing_home)]
    assert not missing_home.exists()


def test_installed_smoke_rejects_empty_data_for_a_populated_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = installed_smoke.build_home(tmp_path, populated=True)

    def fake_hermesd(*args: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
        if "--snapshot-format" in args:
            payload = {"panel_num": None, "state": {"config": {"model": ""}}}
            return subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        return subprocess.CompletedProcess([], 0, "overview\n", "")

    monkeypatch.setattr(installed_smoke, "hermesd", fake_hermesd)

    with pytest.raises(SystemExit, match="fixture model not collected"):
        installed_smoke.check_snapshots(home, "populated home", populated=True)


def test_installed_smoke_checks_survive_python_optimize() -> None:
    """`python -O` strips assert statements; smoke checks must not rely on them."""
    import ast

    tree = ast.parse(Path("scripts/installed_smoke.py").read_text(encoding="utf-8"))
    assert not [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)]


def test_installed_smoke_missing_home_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_hermesd(*args: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(installed_smoke, "hermesd", fake_hermesd)

    with pytest.raises(SystemExit, match="missing home: unexpected exit 0") as exc:
        installed_smoke.check_missing_home(tmp_path / "missing")
    assert exc.value.code != 0


def test_sdist_includes_ci_helpers_lockfile_and_policy_docs() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    includes = set(project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"])

    assert {"docs/", "scripts/", "uv.lock"} <= includes
