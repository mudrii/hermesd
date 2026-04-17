from __future__ import annotations

import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from hermesd.collector import Collector
from hermesd.models import DashboardState
from hermesd.panels import PANEL_NAMES, render_panel
from hermesd.theme import Theme, load_theme

_LOG_VIEWS = ("agent", "gateway", "errors")
_DOTS_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_PANEL_NUMBERS = tuple(sorted(PANEL_NAMES))
_LOG_PANEL_NUM = next(
    panel_num for panel_num, panel_name in PANEL_NAMES.items() if panel_name == "Logs"
)


class ViewState:
    def __init__(self) -> None:
        self.mode: str = "overview"
        self.detail_panel: int | None = None
        self.scroll_offset: int = 0
        self.log_sub_view: str = "agent"
        self.show_help: bool = False

    def enter_detail(self, panel_num: int) -> None:
        self.mode = "detail"
        self.detail_panel = panel_num
        self.scroll_offset = 0

    def exit_detail(self) -> None:
        self.mode = "overview"
        self.detail_panel = None
        self.scroll_offset = 0

    def scroll_down(self) -> None:
        self.scroll_offset += 1

    def scroll_up(self) -> None:
        self.scroll_offset = max(0, self.scroll_offset - 1)

    def cycle_log_view(self) -> None:
        idx = _LOG_VIEWS.index(self.log_sub_view)
        self.log_sub_view = _LOG_VIEWS[(idx + 1) % len(_LOG_VIEWS)]


class DashboardApp:
    def __init__(self, hermes_home: Path, refresh_rate: int = 5, no_color: bool = False) -> None:
        if refresh_rate <= 0:
            raise ValueError("refresh_rate must be positive")
        self._home = hermes_home
        self._refresh_rate = refresh_rate
        self._collector = Collector(hermes_home)
        self._theme = load_theme(hermes_home)
        self._view = ViewState()
        self._state: DashboardState = DashboardState(hermes_home=hermes_home)
        self._running = threading.Event()
        self._force_refresh = threading.Event()
        self._lock = threading.Lock()
        self._view_lock = threading.Lock()
        self._spinner_idx = 0
        self._input_error: str | None = None
        self._console = Console(force_terminal=not no_color, no_color=no_color)

    def run(self) -> None:
        self._running.set()
        self._set_state(self._collector.collect())

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        collector_thread = threading.Thread(target=self._collector_loop, daemon=True)
        collector_thread.start()

        input_thread = threading.Thread(target=self._input_loop, daemon=True)
        input_thread.start()

        try:
            with Live(
                self._build_layout(), console=self._console, refresh_per_second=2, screen=True
            ) as live:
                while self._running.is_set():
                    time.sleep(0.5)
                    self._spinner_idx = (self._spinner_idx + 1) % len(_DOTS_FRAMES)
                    live.update(self._build_layout())
        except KeyboardInterrupt:
            pass
        finally:
            self._running.clear()
            self.close()

    def close(self) -> None:
        self._running.clear()
        self._force_refresh.set()
        self._collector.close()

    def _signal_handler(self, sig: int, frame: object) -> None:
        self._running.clear()
        self._force_refresh.set()

    def _collector_loop(self) -> None:
        while self._running.is_set():
            self._force_refresh.wait(timeout=self._refresh_rate)
            self._force_refresh.clear()
            if not self._running.is_set():
                break
            try:
                new_state = self._collector.collect()
                self._set_state(new_state)
            except Exception:
                with self._lock:
                    self._state = self._state.model_copy(update={"is_stale": True})

    def _input_loop(self) -> None:
        if not sys.stdin.isatty():
            return
        import os as _os
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while self._running.is_set():
                if not select.select([fd], [], [], 0.25)[0]:
                    continue
                data = _os.read(fd, 64)
                if not data:
                    break
                key = data.decode("utf-8", errors="replace")
                with self._view_lock:
                    action = self._handle_key(key)
                if action == "quit":
                    self._running.clear()
                    break
        except Exception as exc:
            with self._lock:
                self._input_error = f"input error: {exc}"
            self._running.clear()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _set_state(self, state: DashboardState) -> None:
        next_theme: Theme | None = None
        with self._lock:
            current_skin = self._theme.skin_name
        if current_skin != state.active_skin:
            next_theme = load_theme(self._home)
        with self._lock:
            self._state = state
            if next_theme is not None:
                self._theme = next_theme

    def _handle_key(self, key: str) -> str | None:
        if not key:
            return None
        if key == "q":
            return "quit"
        if key == "r":
            self._force_refresh.set()
            return "refresh"
        if key == "?":
            self._view.show_help = not self._view.show_help
            return None
        if key[0] == "\x1b":
            if len(key) == 1 and self._view.mode == "detail":
                self._view.exit_detail()
            return None
        if key == "\t" and self._view.detail_panel == _LOG_PANEL_NUM:
            self._view.cycle_log_view()
            return None
        if key in ("j",):
            self._view.scroll_down()
            return None
        if key in ("k",):
            self._view.scroll_up()
            return None
        if len(key) == 1 and key.isdigit():
            panel_num = int(key)
            if panel_num in _PANEL_NUMBERS:
                self._view.enter_detail(panel_num)
            return None
        return None

    def _build_layout(self) -> Layout:
        with self._lock:
            state = self._state
            theme = self._theme
            input_error = self._input_error
        with self._view_lock:
            mode = self._view.mode
            detail_panel = self._view.detail_panel
            scroll_offset = self._view.scroll_offset
            log_sub_view = self._view.log_sub_view
            show_help = self._view.show_help

        layout = Layout()
        layout.split_column(
            Layout(name="header", size=1),
            Layout(name="body"),
            Layout(name="footer", size=1),
        )

        layout["header"].update(self._build_header(state, theme))

        if show_help:
            layout["body"].update(self._build_help(theme))
        elif mode == "detail" and detail_panel:
            panel = render_panel(
                detail_panel,
                state,
                theme,
                detail=True,
                log_sub_view=log_sub_view,
                scroll_offset=scroll_offset,
            )
            layout["body"].update(panel)
        else:
            layout["body"].update(self._build_overview(state, theme))

        layout["footer"].update(
            self._build_footer(
                state,
                theme,
                input_error=input_error,
                view_mode=mode,
                detail_panel=detail_panel,
            )
        )
        return layout

    def _build_header(self, state: DashboardState, theme: Theme | None = None) -> Text:
        active_theme = theme or self._theme
        now = datetime.now().strftime("%H:%M:%S")
        t = Text(style="on #1A1A2E")
        t.append(" ⚕ hermesd ", style=f"bold {active_theme.banner_title} on #1A1A2E")
        skin_label = f"{state.active_skin} skin" if state.active_skin != "default" else ""
        padding = " " * max(1, 60 - len(skin_label) - len(now))
        t.append(f"{padding}{skin_label}   {now} ", style=f"{active_theme.banner_dim} on #1A1A2E")
        return t

    def _build_footer(
        self,
        state: DashboardState,
        theme: Theme | None = None,
        input_error: str | None = None,
        view_mode: str | None = None,
        detail_panel: int | None = None,
    ) -> Text:
        active_theme = theme or self._theme
        if input_error is None:
            with self._lock:
                footer_error = self._input_error
        else:
            footer_error = input_error
        if view_mode is None or detail_panel is None:
            with self._view_lock:
                mode = self._view.mode if view_mode is None else view_mode
                panel = self._view.detail_panel if detail_panel is None else detail_panel
        else:
            mode = view_mode
            panel = detail_panel
        t = Text(style="on #1A1A2E")
        if mode == "overview":
            t.append(" [1-8]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Expand  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append("[r]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Refresh  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append("[q]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Quit  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append("[?]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Help", style=f"{active_theme.session_border} on #1A1A2E")
        else:
            t.append(" [Esc]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Back  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append("[j/k]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Scroll  ", style=f"{active_theme.session_border} on #1A1A2E")
            if panel == _LOG_PANEL_NUM:
                t.append("[Tab]", style=f"bold {active_theme.banner_title} on #1A1A2E")
                t.append(" Switch log  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append("[q]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Quit", style=f"{active_theme.session_border} on #1A1A2E")

        spinner = _DOTS_FRAMES[self._spinner_idx]
        stale = " (stale)" if state.is_stale else ""
        t.append(
            f"   {spinner} {self._refresh_rate}s{stale}",
            style=f"{active_theme.session_border} on #1A1A2E",
        )
        if footer_error:
            t.append("  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append(footer_error, style=f"{active_theme.ui_error} on #1A1A2E")
        return t

    def _build_overview(self, state: DashboardState, theme: Theme | None = None) -> Layout:
        active_theme = theme or self._theme
        width = self._console.width
        height = self._console.height

        if width < 100 or height < 30:
            return self._build_overview_compact(state, active_theme)
        return self._build_overview_wide(state, active_theme)

    def _build_overview_wide(self, state: DashboardState, theme: Theme) -> Layout:
        body = Layout()
        body.split_column(
            Layout(name="row1", size=4),
            Layout(name="row2"),
            Layout(name="row3"),
            Layout(name="row4"),
            Layout(name="row5", size=7),
        )
        body["row1"].update(render_panel(1, state, theme))

        body["row2"].split_row(
            Layout(name="r2l"),
            Layout(name="r2r"),
        )
        body["row2"]["r2l"].update(render_panel(2, state, theme))
        body["row2"]["r2r"].update(render_panel(3, state, theme))

        body["row3"].split_row(
            Layout(name="r3l"),
            Layout(name="r3r"),
        )
        body["row3"]["r3l"].update(render_panel(4, state, theme))
        body["row3"]["r3r"].update(render_panel(5, state, theme))

        body["row4"].split_row(
            Layout(name="r4l"),
            Layout(name="r4r"),
        )
        body["row4"]["r4l"].update(render_panel(6, state, theme))
        body["row4"]["r4r"].update(render_panel(7, state, theme))

        body["row5"].update(render_panel(8, state, theme))
        return body

    def _build_overview_compact(self, state: DashboardState, theme: Theme) -> Layout:
        body = Layout()
        body.split_column(
            Layout(name="row1", size=3),
            Layout(name="row2", size=4),
            Layout(name="row3", size=4),
            Layout(name="row4", size=4),
            Layout(name="row5", size=4),
            Layout(name="row6"),
        )
        body["row1"].update(render_panel(1, state, theme))
        body["row2"].update(render_panel(2, state, theme))
        body["row3"].update(render_panel(3, state, theme))

        body["row4"].split_row(
            Layout(name="r4l"),
            Layout(name="r4r"),
        )
        body["row4"]["r4l"].update(render_panel(4, state, theme))
        body["row4"]["r4r"].update(render_panel(5, state, theme))

        body["row5"].split_row(
            Layout(name="r5l"),
            Layout(name="r5r"),
        )
        body["row5"]["r5l"].update(render_panel(6, state, theme))
        body["row5"]["r5r"].update(render_panel(7, state, theme))

        body["row6"].update(render_panel(8, state, theme))
        return body

    def _build_help(self, theme: Theme | None = None) -> Panel:
        active_theme = theme or self._theme
        import rich.box

        lines = Text()
        lines.append("  Keyboard Shortcuts\n\n", style=f"bold {active_theme.banner_title}")
        shortcuts = [
            ("1-8", "Expand panel to full-screen"),
            ("Esc", "Return to overview"),
            ("j/k", "Scroll down/up (detail mode)"),
            ("Tab", "Cycle log sub-view (logs panel)"),
            ("r", "Force refresh"),
            ("q", "Quit"),
            ("?", "Toggle this help"),
        ]
        for key, desc in shortcuts:
            lines.append(f"  {key:>6}", style=f"bold {active_theme.ui_accent}")
            lines.append(f"  {desc}\n", style=active_theme.banner_text)
        return Panel(
            lines,
            title=f"[{active_theme.panel_title_style}]Help[/]",
            border_style=active_theme.panel_border_style,
            box=rich.box.HORIZONTALS,
            padding=(1, 2),
        )
