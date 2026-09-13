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


def run_pip_audit(args: list[str]) -> tuple[int, Path | None]:
    """Run pip-audit once; return (exit code, path to its JSON report or None)."""
    output_dir = tempfile.mkdtemp(prefix="pip-audit-")
    report = Path(output_dir) / "audit.json"
    completed = subprocess.run(
        ["pip-audit", "--format", "json", "--output", str(report), *args],
        check=False,
    )
    return completed.returncode, (report if report.is_file() else None)


def summarize_findings(report_path: Path) -> int:
    """Print one line per vulnerable dependency; return the finding count."""
    data = json.loads(report_path.read_text(encoding="utf-8"))
    count = 0
    for dependency in data.get("dependencies", []):
        vulns = dependency.get("vulns") or []
        if not vulns:
            continue
        count += len(vulns)
        for vuln in vulns:
            fix_versions = ",".join(vuln.get("fix_versions") or []) or "none known"
            print(
                f"VULNERABLE {dependency.get('name')}=={dependency.get('version')} "
                f"{vuln.get('id')} fix: {fix_versions}"
            )
    return count


def main() -> int:
    if shutil.which("pip-audit") is None:
        print("::error::pip-audit is not installed in the active environment", file=sys.stderr)
        return 2

    args = sys.argv[1:]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        code, report = run_pip_audit(args)
        if code == 0:
            print("pip-audit: no known vulnerabilities")
            return 0
        if code == 1 and report is not None:
            findings = summarize_findings(report)
            print(f"pip-audit: {findings} known vulnerability finding(s) — blocking")
            return 1
        # Scanner/infrastructure failure: transient network or index trouble
        # is the common case, so retry a bounded number of times before
        # reporting the scan itself as failed.
        if attempt < MAX_ATTEMPTS:
            print(f"pip-audit scan failed (exit {code}); retrying", file=sys.stderr)
            continue
        print(
            f"::error::pip-audit scan failed after {MAX_ATTEMPTS} attempts "
            f"(exit {code}) — infrastructure failure, not a clean scan",
            file=sys.stderr,
        )
        return 2
    return 2  # pragma: no cover - unreachable


if __name__ == "__main__":
    raise SystemExit(main())
