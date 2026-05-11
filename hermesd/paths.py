from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath


@dataclass(frozen=True, slots=True)
class HermesPaths:
    root_home: Path
    profile_name: str | None = None

    def __post_init__(self) -> None:
        if self.profile_name is None:
            return
        if not _is_valid_profile_name(self.profile_name):
            raise ValueError(f"Invalid profile name '{self.profile_name}'")
        profiles_home = (self.root_home / "profiles").resolve(strict=False)
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
        return self.profile_home.joinpath(*parts)


def _is_valid_profile_name(profile_name: str) -> bool:
    if profile_name in {"", ".", ".."}:
        return False
    return PurePath(profile_name).name == profile_name
