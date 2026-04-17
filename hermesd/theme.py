from __future__ import annotations

from pathlib import Path

_BUILTIN_SKINS: dict[str, dict[str, str]] = {
    "default": {
        "banner_border": "#CD7F32",
        "banner_title": "#FFD700",
        "banner_accent": "#FFBF00",
        "banner_dim": "#B8860B",
        "banner_text": "#FFF8DC",
        "ui_accent": "#FFBF00",
        "ui_label": "#4dd0e1",
        "ui_ok": "#4caf50",
        "ui_error": "#ef5350",
        "ui_warn": "#ffa726",
        "prompt": "#FFF8DC",
        "input_rule": "#CD7F32",
        "response_border": "#FFD700",
        "session_label": "#DAA520",
        "session_border": "#8B8682",
    },
    "ares": {
        "banner_border": "#9F1C1C",
        "banner_title": "#C7A96B",
        "banner_accent": "#DD4A3A",
        "banner_dim": "#6B1717",
        "banner_text": "#F1E6CF",
        "ui_accent": "#DD4A3A",
        "ui_label": "#C7A96B",
        "ui_ok": "#4caf50",
        "ui_error": "#ef5350",
        "ui_warn": "#ffa726",
        "prompt": "#F1E6CF",
        "input_rule": "#9F1C1C",
        "response_border": "#C7A96B",
        "session_label": "#C7A96B",
        "session_border": "#6E584B",
    },
    "mono": {
        "banner_border": "#555555",
        "banner_title": "#e6edf3",
        "banner_accent": "#aaaaaa",
        "banner_dim": "#444444",
        "banner_text": "#c9d1d9",
        "ui_accent": "#aaaaaa",
        "ui_label": "#888888",
        "ui_ok": "#888888",
        "ui_error": "#cccccc",
        "ui_warn": "#999999",
        "prompt": "#c9d1d9",
        "input_rule": "#444444",
        "response_border": "#aaaaaa",
        "session_label": "#888888",
        "session_border": "#555555",
    },
    "slate": {
        "banner_border": "#4169e1",
        "banner_title": "#7eb8f6",
        "banner_accent": "#8EA8FF",
        "banner_dim": "#4b5563",
        "banner_text": "#c9d1d9",
        "ui_accent": "#7eb8f6",
        "ui_label": "#8EA8FF",
        "ui_ok": "#63D0A6",
        "ui_error": "#F7A072",
        "ui_warn": "#e6a855",
        "prompt": "#c9d1d9",
        "input_rule": "#4169e1",
        "response_border": "#7eb8f6",
        "session_label": "#7eb8f6",
        "session_border": "#4b5563",
    },
    "poseidon": {
        "banner_border": "#2A6FB9",
        "banner_title": "#A9DFFF",
        "banner_accent": "#5DB8F5",
        "banner_dim": "#153C73",
        "banner_text": "#EAF7FF",
        "ui_accent": "#5DB8F5",
        "ui_label": "#A9DFFF",
        "ui_ok": "#4caf50",
        "ui_error": "#ef5350",
        "ui_warn": "#ffa726",
        "prompt": "#EAF7FF",
        "input_rule": "#2A6FB9",
        "response_border": "#5DB8F5",
        "session_label": "#A9DFFF",
        "session_border": "#496884",
    },
    "sisyphus": {
        "banner_border": "#B7B7B7",
        "banner_title": "#F5F5F5",
        "banner_accent": "#E7E7E7",
        "banner_dim": "#4A4A4A",
        "banner_text": "#D3D3D3",
        "ui_accent": "#E7E7E7",
        "ui_label": "#D3D3D3",
        "ui_ok": "#919191",
        "ui_error": "#E7E7E7",
        "ui_warn": "#B7B7B7",
        "prompt": "#F5F5F5",
        "input_rule": "#656565",
        "response_border": "#B7B7B7",
        "session_label": "#919191",
        "session_border": "#656565",
    },
    "charizard": {
        "banner_border": "#C75B1D",
        "banner_title": "#FFD39A",
        "banner_accent": "#F29C38",
        "banner_dim": "#7A3511",
        "banner_text": "#FFF0D4",
        "ui_accent": "#F29C38",
        "ui_label": "#FFD39A",
        "ui_ok": "#4caf50",
        "ui_error": "#ef5350",
        "ui_warn": "#ffa726",
        "prompt": "#FFF0D4",
        "input_rule": "#C75B1D",
        "response_border": "#F29C38",
        "session_label": "#FFD39A",
        "session_border": "#6C4724",
    },
}

_DEFAULT = _BUILTIN_SKINS["default"]


def normalize_skin_name(skin_name: str) -> str:
    return skin_name if skin_name in _BUILTIN_SKINS else "default"


class Theme:
    def __init__(self, skin_name: str = "default"):
        normalized_skin_name = normalize_skin_name(skin_name)
        colors = _BUILTIN_SKINS.get(normalized_skin_name, _DEFAULT)
        self.skin_name = normalized_skin_name
        self.banner_border: str = colors["banner_border"]
        self.banner_title: str = colors["banner_title"]
        self.banner_accent: str = colors["banner_accent"]
        self.banner_dim: str = colors["banner_dim"]
        self.banner_text: str = colors["banner_text"]
        self.ui_accent: str = colors["ui_accent"]
        self.ui_label: str = colors["ui_label"]
        self.ui_ok: str = colors["ui_ok"]
        self.ui_error: str = colors["ui_error"]
        self.ui_warn: str = colors["ui_warn"]
        self.prompt: str = colors["prompt"]
        self.input_rule: str = colors["input_rule"]
        self.response_border: str = colors["response_border"]
        self.session_label: str = colors["session_label"]
        self.session_border: str = colors["session_border"]

    @property
    def panel_border_style(self) -> str:
        return self.banner_border

    @property
    def panel_title_style(self) -> str:
        return f"bold {self.banner_title}"

    @property
    def status_bar_style(self) -> str:
        return f"bg:#1A1A2E {self.banner_text}"

    def context_color(self, ratio: float) -> str:
        if ratio >= 0.95:
            return "#FF6B6B"
        if ratio > 0.80:
            return "#FF8C00"
        if ratio >= 0.50:
            return "#FFD700"
        return "#8FBC8F"


def load_theme(hermes_home: Path) -> Theme:
    skin_name = "default"
    config_path = hermes_home / "config.yaml"
    if config_path.exists():
        try:
            import yaml

            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            if isinstance(cfg, dict):
                display = cfg.get("display", {})
                if isinstance(display, dict):
                    skin_name = str(display.get("skin", "default") or "default")
        except (OSError, yaml.YAMLError):
            pass
    return Theme(normalize_skin_name(skin_name))
