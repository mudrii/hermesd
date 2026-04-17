from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class HermesPaths:
    root_home: Path
    profile_name: str | None = None

    def __post_init__(self) -> None:
        if self.profile_name is None:
            return
        profile_home = self.root_home / "profiles" / self.profile_name
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
