from __future__ import annotations

from hermesd.theme import Theme, load_theme


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
