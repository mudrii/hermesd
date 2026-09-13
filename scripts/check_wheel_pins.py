#!/usr/bin/env python3
"""Assert that built wheel metadata pins runtime dependencies exactly.

Published requirements must resolve to the same versions exercised by the
locked CI/dev/Docker environments (audit CI-06): every runtime requirement in
the wheel metadata must be an exact ``==`` pin whose version matches uv.lock,
and the wheel must not carry any undeclared runtime dependency.

Expects exactly one ``dist/hermesd-*.whl`` next to the repository root, i.e.
run this right after ``uv build``.
"""

from __future__ import annotations

import re
import sys
import tomllib
import zipfile
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

# Runtime dependency names from [project.dependencies] (PEP 503-normalized).
RUNTIME_DEPS = ("pydantic", "pyyaml", "rich")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_versions(lock_path: Path) -> dict[str, str]:
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    versions = {
        package["name"]: package["version"]
        for package in data.get("package", [])
        if package.get("name") in RUNTIME_DEPS
    }
    missing = sorted(set(RUNTIME_DEPS) - set(versions))
    if missing:
        raise SystemExit(f"uv.lock is missing locked versions for: {', '.join(missing)}")
    return versions


def wheel_requires_dist(wheel_path: Path) -> list[str]:
    with zipfile.ZipFile(wheel_path) as wheel:
        metadata_names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise SystemExit(
                f"expected one dist-info/METADATA in {wheel_path.name}, found {metadata_names}"
            )
        metadata = wheel.read(metadata_names[0]).decode("utf-8")
    return [
        line.removeprefix("Requires-Dist:").strip()
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist:")
    ]


def split_requirement(requirement: str) -> tuple[str, str] | None:
    """Return (normalized name, version specifier) for one requirement line.

    Entries scoped to an optional extra (``; extra == 'dev'``) describe that
    extra, not the runtime dependency set, so they return ``None``.
    """
    try:
        parsed = Requirement(requirement)
    except InvalidRequirement as exc:
        raise SystemExit(f"unparseable Requires-Dist entry: {requirement!r}") from exc
    marker = str(parsed.marker) if parsed.marker is not None else ""
    if re.fullmatch(r'extra == "[^"]+"', marker):
        return None
    return normalize(parsed.name), str(parsed.specifier)


def main() -> int:
    dist = Path("dist")
    wheels = sorted(dist.glob("hermesd-*.whl"))
    if len(wheels) != 1:
        print(
            f"::error::expected exactly one hermesd wheel in {dist}/, found "
            f"{[wheel.name for wheel in wheels]}",
            file=sys.stderr,
        )
        return 1

    locked = locked_versions(Path("uv.lock"))
    requirements = wheel_requires_dist(wheels[0])
    parsed = [parsed for requirement in requirements if (parsed := split_requirement(requirement))]
    actual: dict[str, list[str]] = {}
    for name, spec in parsed:
        actual.setdefault(name, []).append(spec)

    problems: list[str] = []
    unexpected = sorted(set(actual) - set(RUNTIME_DEPS))
    if unexpected:
        problems.append(f"undeclared runtime dependencies in wheel: {', '.join(unexpected)}")
    for name in RUNTIME_DEPS:
        specs = actual.get(name, [])
        expected = f"=={locked[name]}"
        if not specs:
            problems.append(f"{name} missing from wheel metadata")
        elif len(specs) > 1:
            problems.append(f"duplicate runtime dependency in wheel: {name}")
        elif specs[0] != expected:
            problems.append(f"{name} pin {specs[0]!r} does not match locked {expected!r}")

    if problems:
        for problem in problems:
            print(f"::error::{problem}", file=sys.stderr)
        return 1

    pins = ", ".join(f"{name}=={locked[name]}" for name in RUNTIME_DEPS)
    print(f"wheel pins match uv.lock: {pins}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
