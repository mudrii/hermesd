"""Profile-scoped path resolution and hermes-home containment rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermesd.paths import HermesPaths, default_hermes_home


def test_default_hermes_home_follows_path_home(monkeypatch, tmp_path: Path):
    fake_home = tmp_path / "fake-home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert default_hermes_home() == fake_home / ".hermes"


def test_hermes_paths_rejects_missing_profile_dir(profiled_hermes_home: Path):
    with pytest.raises(ValueError, match="Profile 'missing' does not exist"):
        HermesPaths(profiled_hermes_home, profile_name="missing")


def test_hermes_paths_rejects_symlinked_profile_escaping_profiles_dir(hermes_home: Path):
    """A profile dir that is a symlink out of profiles/ must be rejected."""
    outside = hermes_home.parent / "outside-profile"
    outside.mkdir()
    profiles_dir = hermes_home / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "sneaky").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="Invalid profile name 'sneaky'"):
        HermesPaths(hermes_home, profile_name="sneaky")


def test_hermes_paths_rejects_symlinked_profiles_root_escaping_home(hermes_home: Path):
    outside_profiles = hermes_home.parent / "outside-profiles"
    (outside_profiles / "coding").mkdir(parents=True)
    (hermes_home / "profiles").symlink_to(outside_profiles, target_is_directory=True)

    with pytest.raises(ValueError, match="Invalid profiles directory"):
        HermesPaths(hermes_home, profile_name="coding")
