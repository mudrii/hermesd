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


def test_load_theme_no_config(hermes_home):
    t = load_theme(hermes_home)
    assert t.banner_title == "#FFD700"


def test_theme_rich_style():
    t = Theme()
    assert t.panel_border_style == "#CD7F32"
    assert t.panel_title_style == "bold #FFD700"
    assert "bg:#1A1A2E" in t.status_bar_style


def test_theme_context_color():
    t = Theme()
    assert t.context_color(0.3) == "#8FBC8F"
    assert t.context_color(0.5) == "#FFD700"
    assert t.context_color(0.85) == "#FF8C00"
    assert t.context_color(0.96) == "#FF6B6B"
