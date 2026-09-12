"""Path resolution for every on-disk Hermes source.

Two resolvers, two scopes — and only two:

* ``shared_path(*parts)`` always resolves under ``root_home`` (``~/.hermes``).
  ``shared_home`` is a hard alias for ``root_home``, so "shared" is not a third
  scope; use it only where hermes-agent itself anchors at
  ``get_default_hermes_root()``, or for a machine-global source.
* ``profile_path(*parts)`` resolves under the selected profile's home, or under
  ``root_home`` when no profile is selected. Use it for anything hermes-agent
  resolves through ``get_hermes_home()``, which is the *profile* home whenever a
  profile is active.

Which scope owns which source is recorded per ``source_name`` in
``.codex/rules/source-ownership.md``; that table is checked against these call
sites by ``tests/test_collector_profiles.py``.

Scope is re-derived on every call, never cached: ``profile_path`` runs
``_validate_profile_home`` each time so a symlink swapped in after construction
cannot redirect a read outside ``root_home/profiles``. Do not hoist a resolved
path into ``__init__`` as an "optimization" — that is the attack the revalidation
defeats.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath


def default_hermes_home() -> Path:
    return Path.home() / ".hermes"


@dataclass(frozen=True, slots=True)
class HermesPaths:
    root_home: Path
    profile_name: str | None = None

    def __post_init__(self) -> None:
        if self.profile_name is None:
            return
        if not _is_valid_profile_name(self.profile_name):
            raise ValueError(f"Invalid profile name '{self.profile_name}'")
        profiles_path = self.root_home / "profiles"
        profiles_home = profiles_path.resolve(strict=False)
        root_home = self.root_home.resolve(strict=False)
        if profiles_path.is_symlink() or not profiles_home.is_relative_to(root_home):
            raise ValueError("Invalid profiles directory")
        profile_home = (profiles_home / self.profile_name).resolve(strict=False)
        if not profile_home.is_relative_to(profiles_home):
            raise ValueError(f"Invalid profile name '{self.profile_name}'")
        if not profile_home.is_dir():
            raise ValueError(f"Profile '{self.profile_name}' does not exist")

    @property
    def shared_home(self) -> Path:
        return self.root_home

    @property
    def profile_home(self) -> Path:
        if self.profile_name is None:
            return self.root_home
        return self.root_home / "profiles" / self.profile_name

    @property
    def profile_mode_label(self) -> str:
        if self.profile_name is None:
            return "root"
        return f"profile:{self.profile_name}"

    def shared_path(self, *parts: str) -> Path:
        return self.shared_home.joinpath(*parts)

    def profile_path(self, *parts: str) -> Path:
        self._validate_profile_home()
        return self.profile_home.joinpath(*parts)

    def _validate_profile_home(self) -> None:
        """Revalidate profile confinement after construction-time symlink swaps."""
        if self.profile_name is None:
            return
        profiles_path = self.root_home / "profiles"
        profile_path = profiles_path / self.profile_name
        root_home = self.root_home.resolve(strict=False)
        profiles_home = profiles_path.resolve(strict=False)
        profile_home = profile_path.resolve(strict=False)
        if (
            profiles_path.is_symlink()
            or not profiles_home.is_relative_to(root_home)
            or not profile_home.is_relative_to(profiles_home)
        ):
            raise ValueError(f"Profile '{self.profile_name}' escaped profiles directory")


def _is_valid_profile_name(profile_name: str) -> bool:
    if profile_name in {"", ".", ".."}:
        return False
    return PurePath(profile_name).name == profile_name
