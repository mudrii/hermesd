#!/usr/bin/env python3
"""Run pip-audit as a blocking gate with classified outcomes (audit CI-12).

Outcome classes:

* clean    — audit succeeded, no known vulnerabilities (exit 0).
* findings — known vulnerabilities in the audited set (exit 1). Each finding
  is listed with its known fix version when one exists.
* scanner  — pip-audit itself failed (network, index, crash). Retried once,
  then reported as an infrastructure failure (exit 2) so a failed scan can
  never look clean and cannot be confused with a vulnerability finding.

Usage: pip_audit_gate.py [pip-audit arguments...]
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_ATTEMPTS = 2
# The installed-environment audit sees hermesd itself, which is intentionally
# audited from source rather than resolved from PyPI. Every third-party skip is
# incomplete security evidence and must fail the gate.
ALLOWED_SKIPPED_DEPENDENCIES = frozenset({"hermesd"})


def run_pip_audit(args: list[str], report_path: Path) -> int:
    """Run pip-audit once and write its JSON report to ``report_path``."""
    completed = subprocess.run(
        ["pip-audit", "--format", "json", "--output", str(report_path), *args],
        check=False,
    )
    return completed.returncode


def summarize_report(report_path: Path) -> tuple[int, list[tuple[str, str]]]:
    """Print findings and return their count plus any skipped dependencies."""
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable JSON report: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("dependencies"), list):
        raise ValueError("JSON report has no dependencies list")

    count = 0
    skipped: list[tuple[str, str]] = []
    for dependency in data["dependencies"]:
        if not isinstance(dependency, dict) or not isinstance(dependency.get("name"), str):
            raise ValueError("JSON report contains an invalid dependency entry")
        name = dependency["name"]
        if "skip_reason" in dependency:
            reason = dependency["skip_reason"]
            if not isinstance(reason, str):
                raise ValueError(f"JSON report contains an invalid skip reason for {name}")
            skipped.append((name, reason))
            continue

        version = dependency.get("version")
        if not isinstance(version, str):
            raise ValueError(f"JSON report contains no version for {name}")
        vulns = dependency.get("vulns")
        if not isinstance(vulns, list):
            raise ValueError(f"JSON report contains no vulnerability list for {name}")
        if not vulns:
            continue
        count += len(vulns)
        for vuln in vulns:
            if not isinstance(vuln, dict) or not isinstance(vuln.get("id"), str):
                raise ValueError(f"JSON report contains an invalid vulnerability for {name}")
            fix_values = vuln.get("fix_versions")
            if not isinstance(fix_values, list) or not all(
                isinstance(version, str) for version in fix_values
            ):
                raise ValueError(f"JSON report contains invalid fix versions for {name}")
            fix_versions = ",".join(vuln.get("fix_versions") or []) or "none known"
            print(f"VULNERABLE {name}=={version} {vuln['id']} fix: {fix_versions}")
    return count, skipped


def main(args: list[str] | None = None) -> int:
    if shutil.which("pip-audit") is None:
        print("::error::pip-audit is not installed in the active environment", file=sys.stderr)
        return 2

    audit_args = sys.argv[1:] if args is None else args
    with tempfile.TemporaryDirectory(prefix="pip-audit-") as output_dir:
        report_path = Path(output_dir) / "audit.json"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            report_path.unlink(missing_ok=True)
            try:
                code = run_pip_audit(audit_args, report_path)
            except OSError as exc:
                failure = f"could not execute pip-audit: {exc}"
            else:
                try:
                    findings, skipped = summarize_report(report_path)
                except ValueError as exc:
                    failure = f"exit {code} with invalid report ({exc})"
                else:
                    for name, reason in skipped:
                        print(f"pip-audit: skipped {name}: {reason}", file=sys.stderr)
                    if code == 1 and findings:
                        print(f"pip-audit: {findings} known vulnerability finding(s) — blocking")
                        return 1
                    unexpected_skips = [
                        name
                        for name, _reason in skipped
                        if name not in ALLOWED_SKIPPED_DEPENDENCIES
                    ]
                    if code == 0 and not findings and not unexpected_skips:
                        if skipped:
                            print(
                                "pip-audit: no known vulnerability findings; "
                                f"{len(skipped)} dependency scan(s) skipped"
                            )
                        else:
                            print("pip-audit: no known vulnerabilities")
                        return 0
                    if unexpected_skips:
                        failure = "audit skipped unexpected dependencies: " + ", ".join(
                            unexpected_skips
                        )
                    else:
                        failure = f"exit {code} does not match the JSON report"

            if attempt < MAX_ATTEMPTS:
                print(f"pip-audit scan failed ({failure}); retrying", file=sys.stderr)
                continue
            print(
                f"::error::pip-audit scan failed after {MAX_ATTEMPTS} attempts "
                f"({failure}) — infrastructure failure, not a clean scan",
                file=sys.stderr,
            )
            return 2
    return 2  # pragma: no cover - unreachable


if __name__ == "__main__":
    raise SystemExit(main())
