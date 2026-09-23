"""Profile health: config/.env presence and duplicated platform credential names.

Upstream parity: ``hermes doctor``'s profile section
(``hermes_cli/doctor_state.py:551-581``) flags a profile without
``config.yaml`` or ``.env`` and names platform credentials two profiles both
hold (``hermes_cli/gateway_migrate.py:403-443``). hermesd compares key NAMES
only — values are never kept, compared or displayed.
"""

from __future__ import annotations

from pathlib import Path

from hermesd.collector import Collector
from hermesd.models import DashboardState

_SECRET = "123456:ABC-should-never-render"


def _profile(home: Path, name: str, *, env: str | None = None, config: bool = True) -> Path:
    profile = home / "profiles" / name
    profile.mkdir(parents=True)
    if config:
        (profile / "config.yaml").write_text("model: {}\n")
    if env is not None:
        (profile / ".env").write_text(env)
    return profile


def _collect(home: Path) -> DashboardState:
    c = Collector(home)
    try:
        return c.collect()
    finally:
        c.close()


def test_profile_summary_reports_config_and_env_presence(hermes_home: Path):
    _profile(hermes_home, "dev", env="FOO=1\n")
    _profile(hermes_home, "bare", config=False)
    state = _collect(hermes_home)
    by_name = {p.name: p for p in state.profiles.profiles}
    assert (by_name["dev"].config_present, by_name["dev"].env_present) == (True, True)
    assert (by_name["bare"].config_present, by_name["bare"].env_present) == (False, False)


def test_duplicate_platform_credential_names_across_profiles(hermes_home: Path):
    (hermes_home / ".env").write_text(
        f"# comment\nTELEGRAM_BOT_TOKEN={_SECRET}\nOPENAI_API_KEY=sk-x\nDISCORD_BOT_TOKEN=\n"
    )
    _profile(
        hermes_home,
        "dev",
        env=f'export TELEGRAM_BOT_TOKEN="{_SECRET}"\nDISCORD_BOT_TOKEN=abc\nOPENAI_API_KEY=sk-y\n',
    )
    _profile(hermes_home, "ops", env="DISCORD_BOT_TOKEN=def\nSLACK_BOT_TOKEN=''\n")

    state = _collect(hermes_home)

    duplicates = [
        (d.key, d.platform, d.profiles) for d in state.profiles.duplicate_platform_credentials
    ]
    # A blank assignment does not hold a credential; non-platform keys (the
    # provider API keys every profile legitimately shares) are not checked.
    assert duplicates == [
        ("DISCORD_BOT_TOKEN", "discord", ["dev", "ops"]),
        ("TELEGRAM_BOT_TOKEN", "telegram", ["default", "dev"]),
    ]
    assert _SECRET not in state.model_dump_json()
    assert "profile_credentials" not in state.health.failed_sources


def test_no_duplicates_without_a_shared_key(hermes_home: Path):
    (hermes_home / ".env").write_text("TELEGRAM_BOT_TOKEN=a\n")
    _profile(hermes_home, "dev", env="SLACK_BOT_TOKEN=b\n")
    state = _collect(hermes_home)
    assert state.profiles.duplicate_platform_credentials == []


def test_profile_credentials_skip_symlinked_env(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "outside.env"
    outside.write_text("TELEGRAM_BOT_TOKEN=x\n")
    (hermes_home / ".env").write_text("TELEGRAM_BOT_TOKEN=y\n")
    profile = _profile(hermes_home, "dev")
    (profile / ".env").symlink_to(outside)
    state = _collect(hermes_home)
    assert state.profiles.duplicate_platform_credentials == []


def test_profile_credentials_keep_last_good_when_env_turns_unreadable(hermes_home: Path):
    (hermes_home / ".env").write_text("TELEGRAM_BOT_TOKEN=a\n")
    env = _profile(hermes_home, "dev", env="TELEGRAM_BOT_TOKEN=b\n") / ".env"
    c = Collector(hermes_home)
    try:
        first = c.collect()
        env.unlink()
        env.mkdir()  # a directory where the file was: unreadable, not absent
        second = c.collect()
    finally:
        c.close()
    assert len(first.profiles.duplicate_platform_credentials) == 1
    assert "profile_credentials" in second.health.failed_sources
    assert len(second.profiles.duplicate_platform_credentials) == 1
