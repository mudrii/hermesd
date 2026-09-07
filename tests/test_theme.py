from __future__ import annotations

import pytest

from hermesd.theme import Theme, load_theme, normalize_skin_name

BUILTIN_SKINS = ["default", "ares", "mono", "slate", "poseidon", "sisyphus", "charizard"]


def test_default_theme_colors():
    t = Theme()
    assert t.banner_border == "#CD7F32"
    assert t.banner_title == "#FFD700"
    assert t.ui_ok == "#4caf50"
    assert t.ui_error == "#ef5350"


def test_load_theme_default(hermes_home, sample_config):
    t = load_theme(hermes_home)
    assert t.banner_title == "#FFD700"


def test_load_theme_builtin_skin(hermes_home):
    import yaml

    config_path = hermes_home / "config.yaml"
    config_path.write_text(yaml.dump({"display": {"skin": "ares"}}))
    t = load_theme(hermes_home)
    assert t.banner_border == "#9F1C1C"
    assert t.banner_title == "#C7A96B"


def test_load_theme_unknown_skin_falls_back(hermes_home):
    import yaml

    config_path = hermes_home / "config.yaml"
    config_path.write_text(yaml.dump({"display": {"skin": "nonexistent"}}))
    t = load_theme(hermes_home)
    assert t.banner_title == "#FFD700"


def test_load_theme_malformed_yaml_falls_back(hermes_home):
    config_path = hermes_home / "config.yaml"
    config_path.write_text("display: [")
    t = load_theme(hermes_home)
    assert t.banner_title == "#FFD700"


def test_load_theme_invalid_utf8_falls_back(hermes_home):
    (hermes_home / "config.yaml").write_bytes(b"display:\n  skin: \xff\n")

    t = load_theme(hermes_home)

    assert t.banner_title == "#FFD700"


def test_load_theme_read_error_falls_back(hermes_home, monkeypatch):
    config_path = hermes_home / "config.yaml"
    config_path.write_text("display:\n  skin: ares\n")

    def fail_open(*args, **kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr("builtins.open", fail_open)
    t = load_theme(hermes_home)
    assert t.banner_title == "#FFD700"


def test_load_theme_no_config(hermes_home):
    t = load_theme(hermes_home)
    assert t.banner_title == "#FFD700"


def test_theme_rich_style():
    t = Theme()
    assert t.panel_border_style == "#CD7F32"
    assert t.panel_title_style == "bold #FFD700"
    assert t.status_bar_bg == "#1A1A2E"


@pytest.mark.parametrize("skin", BUILTIN_SKINS)
def test_theme_loads_every_builtin_skin(skin: str):
    t = Theme(skin)
    assert t.skin_name == skin
    # Every color attribute must be populated for the skin.
    for attr in (
        "banner_border",
        "banner_title",
        "banner_accent",
        "banner_dim",
        "banner_text",
        "ui_accent",
        "ui_label",
        "ui_ok",
        "ui_error",
        "ui_warn",
        "prompt",
        "input_rule",
        "response_border",
        "session_label",
        "session_border",
    ):
        assert getattr(t, attr), f"skin {skin!r} left {attr} blank"


@pytest.mark.parametrize("skin", BUILTIN_SKINS)
def test_load_theme_accepts_every_builtin_skin(hermes_home, skin: str):
    import yaml

    config_path = hermes_home / "config.yaml"
    config_path.write_text(yaml.dump({"display": {"skin": skin}}))
    t = load_theme(hermes_home)
    assert t.skin_name == skin


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        *[(skin, skin) for skin in BUILTIN_SKINS],
        ("nonexistent", "default"),
        ("", "default"),
        ("DEFAULT", "default"),  # case-sensitive: unknown names fall back
        (" default", "default"),
    ],
)
def test_normalize_skin_name(given: str, expected: str):
    assert normalize_skin_name(given) == expected
