from pathlib import Path

from hermesd.collector import Collector
from hermesd.models import DashboardState, ProfilesState, ProfileSummary
from hermesd.panels import render_panel
from hermesd.theme import Theme


def _render_to_str(panel: object) -> str:
    from rich.console import Console

    console = Console(width=120, force_terminal=True, record=True)
    console.print(panel)
    return console.export_text()


def test_collect_profiles_empty_when_no_profiles_dir(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.profiles.profile_count == 0
    assert state.profiles.profiles == []
    c.close()


def test_collect_profiles_lists_profile_directories(profiled_hermes_home: Path):
    c = Collector(profiled_hermes_home)
    state = c.collect()
    assert state.profiles.profile_count == 1
    profile = state.profiles.profiles[0]
    assert profile.name == "coding"
    assert profile.session_count == 1
    assert profile.skill_count == 1
    assert profile.db_size_bytes > 0
    assert profile.soul_excerpt == ""
    assert profile.latest_log_mtime is not None
    c.close()


def test_collect_profiles_reads_soul_excerpt_when_present(profiled_hermes_home: Path):
    soul = profiled_hermes_home / "profiles" / "coding" / "SOUL.md"
    soul.write_text("Profile soul line one\nProfile soul line two\n")
    c = Collector(profiled_hermes_home)
    state = c.collect()
    assert state.profiles.profiles[0].soul_excerpt == "Profile soul line one"
    c.close()


def test_profiles_panel_compact_shows_selected_source_and_count():
    state = DashboardState(
        selected_profile="coding",
        profile_mode_label="profile:coding",
        profiles=ProfilesState(
            profile_count=2,
            profiles=[
                ProfileSummary(name="coding", session_count=1, skill_count=2, db_size_bytes=1024),
                ProfileSummary(name="research", session_count=3, skill_count=5, db_size_bytes=2048),
            ],
        ),
    )
    panel = render_panel(9, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "profile:coding" in text
    assert "2 discovered" in text


def test_profiles_panel_detail_renders_profile_rows():
    state = DashboardState(
        selected_profile="coding",
        profile_mode_label="profile:coding",
        profiles=ProfilesState(
            profile_count=2,
            profiles=[
                ProfileSummary(
                    name="coding",
                    session_count=1,
                    skill_count=2,
                    db_size_bytes=1024,
                    soul_excerpt="Code soul",
                ),
                ProfileSummary(
                    name="research",
                    session_count=3,
                    skill_count=5,
                    db_size_bytes=2048,
                    soul_excerpt="Research soul",
                ),
            ],
        ),
    )
    panel = render_panel(9, state, Theme(), detail=True, profile_view_index=1)
    text = _render_to_str(panel)
    assert "coding" in text
    assert "research" in text
    assert "Research soul" in text


def test_profiles_panel_detail_handles_no_profiles():
    state = DashboardState(profiles=ProfilesState())
    panel = render_panel(9, state, Theme(), detail=True, profile_view_index=0)
    text = _render_to_str(panel)
    assert "No profiles found" in text
