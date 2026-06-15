from __future__ import annotations

import time

from hermesd.models import DashboardState, ProfilesState, ProfileSummary
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str


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
    text = render_to_str(panel, no_color=True)
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
    text = render_to_str(panel, no_color=True)
    assert "coding" in text
    assert "research" in text
    assert "Research soul" in text


def test_profiles_panel_detail_formats_large_sizes_and_future_timestamps():
    state = DashboardState(
        profiles=ProfilesState(
            profile_count=1,
            profiles=[
                ProfileSummary(
                    name="future",
                    db_size_bytes=5 * 1024 * 1024 * 1024,
                    latest_log_mtime=time.time() + 3600,
                ),
            ],
        ),
    )
    panel = render_panel(9, state, Theme(), detail=True)
    text = render_to_str(panel, no_color=True)
    assert "5.0 GB" in text
    assert "(future)" in text


def test_profiles_panel_detail_formats_small_sizes_and_past_timestamps():
    mtime = time.time() - 3600
    state = DashboardState(
        profiles=ProfilesState(
            profile_count=3,
            profiles=[
                ProfileSummary(name="tiny", db_size_bytes=500, latest_log_mtime=mtime),
                ProfileSummary(name="medium", db_size_bytes=5 * 1024 * 1024),
                ProfileSummary(name="huge", db_size_bytes=2 * 1024**4),
            ],
        ),
    )
    panel = render_panel(9, state, Theme(), detail=True)
    text = render_to_str(panel, no_color=True)
    assert "500 B" in text
    assert "5.0 MB" in text
    assert "2.0 TB" in text
    assert "(future)" not in text
    from datetime import datetime

    assert datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") in text


def test_profiles_panel_detail_handles_no_profiles():
    state = DashboardState(profiles=ProfilesState())
    panel = render_panel(9, state, Theme(), detail=True, profile_view_index=0)
    text = render_to_str(panel, no_color=True)
    assert "No profiles found" in text
