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
from hermesd.panels import render_panel
from hermesd.theme import load_theme

_LOG_VIEWS = ("agent", "gateway", "errors")
_DOTS_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


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
        self._home = hermes_home
        self._refresh_rate = refresh_rate
        self._collector = Collector(hermes_home)
        self._theme = load_theme(hermes_home)
        self._view = ViewState()
        self._state: DashboardState = DashboardState(hermes_home=hermes_home)
        self._running = False
        self._force_refresh = threading.Event()
        self._lock = threading.Lock()
        self._view_lock = threading.Lock()
        self._spinner_idx = 0
        self._console = Console(force_terminal=not no_color, no_color=no_color)

    def run(self) -> None:
        self._running = True
        self._state = self._collector.collect()

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
                while self._running:
                    time.sleep(0.5)
                    self._spinner_idx = (self._spinner_idx + 1) % len(_DOTS_FRAMES)
                    live.update(self._build_layout())
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            self.close()

    def close(self) -> None:
        self._running = False
        self._collector.close()

    def _signal_handler(self, sig: int, frame: object) -> None:
        self._running = False

    def _collector_loop(self) -> None:
        while self._running:
            self._force_refresh.wait(timeout=self._refresh_rate)
            self._force_refresh.clear()
            if not self._running:
                break
            try:
                new_state = self._collector.collect()
                with self._lock:
                    self._state = new_state
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
            while self._running:
                if not select.select([fd], [], [], 0.25)[0]:
                    continue
                data = _os.read(fd, 64)
                if not data:
                    break
                key = data.decode("utf-8", errors="replace")
                with self._view_lock:
                    action = self._handle_key(key)
                if action == "quit":
                    self._running = False
                    break
        except Exception:
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

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
        if key == "\t" and self._view.detail_panel == 8:
            self._view.cycle_log_view()
            return None
        if key in ("j",):
            self._view.scroll_down()
            return None
        if key in ("k",):
            self._view.scroll_up()
            return None
        if len(key) == 1 and key.isdigit() and 1 <= int(key) <= 8:
            self._view.enter_detail(int(key))
            return None
        return None

    def _build_layout(self) -> Layout:
        with self._lock:
            state = self._state
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

        layout["header"].update(self._build_header(state))

        if show_help:
            layout["body"].update(self._build_help())
        elif mode == "detail" and detail_panel:
            panel = render_panel(
                detail_panel,
                state,
                self._theme,
                detail=True,
                log_sub_view=log_sub_view,
                scroll_offset=scroll_offset,
            )
            layout["body"].update(panel)
        else:
            layout["body"].update(self._build_overview(state))

        layout["footer"].update(self._build_footer(state))
        return layout

    def _build_header(self, state: DashboardState) -> Text:
        now = datetime.now().strftime("%H:%M:%S")
        t = Text(style="on #1A1A2E")
        t.append(" ⚕ hermesd ", style=f"bold {self._theme.banner_title} on #1A1A2E")
        skin_label = f"{state.active_skin} skin" if state.active_skin != "default" else ""
        padding = " " * max(1, 60 - len(skin_label) - len(now))
        t.append(f"{padding}{skin_label}   {now} ", style=f"{self._theme.banner_dim} on #1A1A2E")
        return t

    def _build_footer(self, state: DashboardState) -> Text:
        t = Text(style="on #1A1A2E")
        if self._view.mode == "overview":
            t.append(" [1-8]", style=f"bold {self._theme.banner_title} on #1A1A2E")
            t.append(" Expand  ", style=f"{self._theme.session_border} on #1A1A2E")
            t.append("[r]", style=f"bold {self._theme.banner_title} on #1A1A2E")
            t.append(" Refresh  ", style=f"{self._theme.session_border} on #1A1A2E")
            t.append("[q]", style=f"bold {self._theme.banner_title} on #1A1A2E")
            t.append(" Quit  ", style=f"{self._theme.session_border} on #1A1A2E")
            t.append("[?]", style=f"bold {self._theme.banner_title} on #1A1A2E")
            t.append(" Help", style=f"{self._theme.session_border} on #1A1A2E")
        else:
            t.append(" [Esc]", style=f"bold {self._theme.banner_title} on #1A1A2E")
            t.append(" Back  ", style=f"{self._theme.session_border} on #1A1A2E")
            t.append("[j/k]", style=f"bold {self._theme.banner_title} on #1A1A2E")
            t.append(" Scroll  ", style=f"{self._theme.session_border} on #1A1A2E")
            if self._view.detail_panel == 8:
                t.append("[Tab]", style=f"bold {self._theme.banner_title} on #1A1A2E")
                t.append(" Switch log  ", style=f"{self._theme.session_border} on #1A1A2E")
            t.append("[q]", style=f"bold {self._theme.banner_title} on #1A1A2E")
            t.append(" Quit", style=f"{self._theme.session_border} on #1A1A2E")

        spinner = _DOTS_FRAMES[self._spinner_idx]
        stale = " (stale)" if state.is_stale else ""
        t.append(
            f"   {spinner} {self._refresh_rate}s{stale}",
            style=f"{self._theme.session_border} on #1A1A2E",
        )
        return t

    def _build_overview(self, state: DashboardState) -> Layout:
        width = self._console.width
        height = self._console.height

        if width < 100 or height < 30:
            return self._build_overview_compact(state)
        return self._build_overview_wide(state)

    def _build_overview_wide(self, state: DashboardState) -> Layout:
        body = Layout()
        body.split_column(
            Layout(name="row1", size=4),
            Layout(name="row2"),
            Layout(name="row3"),
            Layout(name="row4"),
            Layout(name="row5", size=7),
        )
        body["row1"].update(render_panel(1, state, self._theme))

        body["row2"].split_row(
            Layout(name="r2l"),
            Layout(name="r2r"),
        )
        body["row2"]["r2l"].update(render_panel(2, state, self._theme))
        body["row2"]["r2r"].update(render_panel(3, state, self._theme))

        body["row3"].split_row(
            Layout(name="r3l"),
            Layout(name="r3r"),
        )
        body["row3"]["r3l"].update(render_panel(4, state, self._theme))
        body["row3"]["r3r"].update(render_panel(5, state, self._theme))

        body["row4"].split_row(
            Layout(name="r4l"),
            Layout(name="r4r"),
        )
        body["row4"]["r4l"].update(render_panel(6, state, self._theme))
        body["row4"]["r4r"].update(render_panel(7, state, self._theme))

        body["row5"].update(render_panel(8, state, self._theme))
        return body

    def _build_overview_compact(self, state: DashboardState) -> Layout:
        body = Layout()
        body.split_column(
            Layout(name="row1", size=3),
            Layout(name="row2", size=4),
            Layout(name="row3", size=4),
            Layout(name="row4"),
            Layout(name="row5", size=4),
        )
        body["row1"].update(render_panel(1, state, self._theme))
        body["row2"].update(render_panel(2, state, self._theme))
        body["row3"].update(render_panel(3, state, self._theme))

        body["row4"].split_row(
            Layout(name="r4l"),
            Layout(name="r4r"),
        )
        body["row4"]["r4l"].update(render_panel(5, state, self._theme))
        body["row4"]["r4r"].update(render_panel(6, state, self._theme))

        body["row5"].update(render_panel(8, state, self._theme))
        return body

    def _build_help(self) -> Panel:
        import rich.box

        lines = Text()
        lines.append("  Keyboard Shortcuts\n\n", style=f"bold {self._theme.banner_title}")
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
            lines.append(f"  {key:>6}", style=f"bold {self._theme.ui_accent}")
            lines.append(f"  {desc}\n", style=self._theme.banner_text)
        return Panel(
            lines,
            title=f"[{self._theme.panel_title_style}]Help[/]",
            border_style=self._theme.panel_border_style,
            box=rich.box.HORIZONTALS,
            padding=(1, 2),
        )
