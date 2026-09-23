from __future__ import annotations

from pathlib import Path

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

    def fail_read(path):
        raise OSError("unreadable")

    monkeypatch.setattr("hermesd.theme._read_capped", fail_read)
    t = load_theme(hermes_home)
    assert t.banner_title == "#FFD700"


def test_load_theme_no_config(hermes_home):
    t = load_theme(hermes_home)
    assert t.banner_title == "#FFD700"


def test_load_theme_corrupt_after_success_returns_last_good(hermes_home):
    config_path = hermes_home / "config.yaml"
    config_path.write_text("display:\n  skin: ares\n")
    assert load_theme(hermes_home).skin_name == "ares"

    config_path.write_text("display: [")
    t = load_theme(hermes_home)
    assert t.skin_name == "ares"
    assert t.banner_title == "#C7A96B"


def test_load_theme_oversize_config_returns_last_good(hermes_home):
    config_path = hermes_home / "config.yaml"
    config_path.write_text("display:\n  skin: ares\n")
    assert load_theme(hermes_home).skin_name == "ares"

    config_path.write_bytes(b"display:\n  skin: mono\n" + b"#" * (8 * 1024 * 1024))
    t = load_theme(hermes_home)
    assert t.skin_name == "ares"


def test_load_theme_stat_permission_error_returns_last_good(hermes_home, monkeypatch):
    config_path = hermes_home / "config.yaml"
    config_path.write_text("display:\n  skin: ares\n")
    assert load_theme(hermes_home).skin_name == "ares"

    real_stat = Path.stat

    def denied_stat(self, *args, **kwargs):
        if self == config_path:
            raise PermissionError("EACCES")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    t = load_theme(hermes_home)
    assert t.skin_name == "ares"


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


def test_load_theme_nesting_bomb_falls_back(hermes_home):
    (hermes_home / "config.yaml").write_text("[" * 20000 + "]" * 20000)

    assert load_theme(hermes_home).banner_title == "#FFD700"


def test_load_theme_nesting_bomb_after_success_returns_last_good(hermes_home):
    config_path = hermes_home / "config.yaml"
    config_path.write_text("display:\n  skin: ares\n")
    assert load_theme(hermes_home).skin_name == "ares"

    config_path.write_text("[" * 20000 + "]" * 20000)

    assert load_theme(hermes_home).skin_name == "ares"


@pytest.mark.parametrize("document", ["false\n", "0\n", "[]\n", "- a\n"])
def test_load_theme_non_mapping_config_returns_last_good(hermes_home, document):
    config_path = hermes_home / "config.yaml"
    config_path.write_text("display:\n  skin: ares\n")
    assert load_theme(hermes_home).skin_name == "ares"

    config_path.write_text(document)

    assert load_theme(hermes_home).skin_name == "ares"


def test_load_theme_empty_config_is_default(hermes_home):
    config_path = hermes_home / "config.yaml"
    config_path.write_text("display:\n  skin: ares\n")
    assert load_theme(hermes_home).skin_name == "ares"

    config_path.write_text("")

    assert load_theme(hermes_home).skin_name == "default"
