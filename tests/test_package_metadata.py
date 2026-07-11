from __future__ import annotations

import re
import tomllib
from pathlib import Path


def test_flake_version_matches_project_version() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())
    expected_version = project["project"]["version"]
    flake_text = Path("flake.nix").read_text()

    match = re.search(r'\bversion = "([^"]+)";', flake_text)

    assert match is not None
    assert match.group(1) == expected_version


def test_readme_images_use_package_metadata_safe_urls() -> None:
    readme = Path("README.md").read_text()
    relative_image_links = re.findall(r"!\[[^\]]*]\((images/[^)]+)\)", readme)

    assert relative_image_links == []
