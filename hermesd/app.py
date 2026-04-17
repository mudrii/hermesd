from __future__ import annotations

import base64
import json
import signal
import sys
import threading
import time
from dataclasses import dataclass
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

_LOG_VIEWS = ("agent", "gateway", "errors", "cron")
_DOTS_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_PANEL_NUMBERS = tuple(sorted(PANEL_NAMES))
_LOG_PANEL_NUM = next(
    panel_num for panel_num, panel_name in PANEL_NAMES.items() if panel_name == "Logs"
)
_SESSIONS_PANEL_NUM = next(
    panel_num for panel_num, panel_name in PANEL_NAMES.items() if panel_name == "Sessions"
)
_SKILLS_PANEL_NUM = next(
    panel_num
    for panel_num, panel_name in PANEL_NAMES.items()
    if panel_name == "Skills / Integrations"
)
_PROFILES_PANEL_NUM = next(
    panel_num for panel_num, panel_name in PANEL_NAMES.items() if panel_name == "Profiles"
)
_WIDE_LAYOUT_SPEC: tuple[tuple[str, int | None, tuple[int, ...]], ...] = (
    ("row1", 4, (1,)),
    ("row2", None, (2, 3)),
    ("row3", None, (4, 5)),
    ("row4", None, (6, 7)),
    ("row5", 7, (8, 9)),
    ("row6", 6, (10,)),
)
_COMPACT_LAYOUT_SPEC: tuple[tuple[str, int | None, tuple[int, ...]], ...] = (
    ("row1", 3, (1,)),
    ("row2", 4, (2,)),
    ("row3", 4, (3,)),
    ("row4", 4, (4, 5)),
    ("row5", 4, (6, 7)),
    ("row6", 4, (8, 9)),
    ("row7", None, (10,)),
)
_TALL_NARROW_LAYOUT_SPEC: tuple[tuple[str, int | None, tuple[int, ...]], ...] = tuple(
    (f"row{panel_num}", None, (panel_num,)) for panel_num in _PANEL_NUMBERS
)
_SESSION_SORTS = ("recent", "cost", "tokens")


@dataclass(frozen=True, slots=True)
class ViewSnapshot:
    mode: str
    detail_panel: int | None
    focus_panel: int
    scroll_offset: int
    log_sub_view: str
    show_help: bool
    profile_cycle_index: int
    filter_query: str
    filter_edit_mode: bool
    session_sort: str


class ViewState:
    def __init__(self) -> None:
        self.mode: str = "overview"
        self.detail_panel: int | None = None
        self.focus_panel: int = _PANEL_NUMBERS[0]
        self.scroll_offset: int = 0
        self.log_sub_view: str = "agent"
        self.show_help: bool = False
        self.profile_cycle_index: int = 0
        self.filter_query: str = ""
        self.filter_edit_mode: bool = False
        self.session_sort: str = "recent"

    def enter_detail(self, panel_num: int) -> None:
        self.mode = "detail"
        self.detail_panel = panel_num
        self.focus_panel = panel_num
        self.scroll_offset = 0
        self.profile_cycle_index = 0
        self.filter_query = ""
        self.filter_edit_mode = False
        self.session_sort = "recent"

    def exit_detail(self) -> None:
        self.mode = "overview"
        self.detail_panel = None
        self.scroll_offset = 0
        self.profile_cycle_index = 0
        self.filter_query = ""
        self.filter_edit_mode = False
        self.session_sort = "recent"

    def scroll_down(self) -> None:
        self.scroll_offset += 1

    def scroll_up(self) -> None:
        self.scroll_offset = max(0, self.scroll_offset - 1)

    def cycle_log_view(self) -> None:
        idx = _LOG_VIEWS.index(self.log_sub_view)
        self.log_sub_view = _LOG_VIEWS[(idx + 1) % len(_LOG_VIEWS)]

    def cycle_profile_view(self) -> None:
        self.profile_cycle_index += 1

    def start_filter(self) -> None:
        self.filter_edit_mode = True

    def stop_filter(self) -> None:
        self.filter_edit_mode = False

    def append_filter_char(self, char: str) -> None:
        self.filter_query += char

    def pop_filter_char(self) -> None:
        self.filter_query = self.filter_query[:-1]

    def cycle_session_sort(self) -> None:
        idx = _SESSION_SORTS.index(self.session_sort)
        self.session_sort = _SESSION_SORTS[(idx + 1) % len(_SESSION_SORTS)]

    def jump_top(self) -> None:
        self.scroll_offset = 0

    def jump_bottom(self) -> None:
        self.scroll_offset = 999_999

    def toggle_focus(self) -> None:
        if self.mode == "detail":
            self.exit_detail()
            return
        self.enter_detail(self.focus_panel)


class DashboardApp:
    def __init__(
        self,
        hermes_home: Path,
        refresh_rate: int = 5,
        no_color: bool = False,
        profile_name: str | None = None,
        log_tail_bytes: int = 32768,
    ) -> None:
        if refresh_rate <= 0:
            raise ValueError("refresh_rate must be positive")
        if log_tail_bytes <= 0:
            raise ValueError("log_tail_bytes must be positive")
        self._home = hermes_home
        self._refresh_rate = refresh_rate
        self._no_color = no_color
        self._collector = Collector(
            hermes_home,
            profile_name=profile_name,
            log_tail_bytes=log_tail_bytes,
        )
        self._theme = load_theme(hermes_home)
        self._view = ViewState()
        profile_mode_label = "root" if profile_name is None else f"profile:{profile_name}"
        self._state: DashboardState = DashboardState(
            hermes_home=hermes_home,
            selected_profile=profile_name,
            profile_mode_label=profile_mode_label,
        )
        self._running = threading.Event()
        self._force_refresh = threading.Event()
        self._lock = threading.Lock()
        self._view_lock = threading.RLock()
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

    def _capture_layout_text(self, panel_num: int | None = None, refresh: bool = False) -> str:
        if refresh:
            self._set_state(self._collector.collect())
        snapshot_console = Console(
            width=max(self._console.width, 120),
            height=max(self._console.height, 48),
            force_terminal=not self._no_color,
            no_color=self._no_color,
        )
        original_console = self._console
        original_view = self._snapshot_view_state()
        self._console = snapshot_console
        try:
            if panel_num is not None:
                self._view.enter_detail(panel_num)
            with snapshot_console.capture() as capture:
                snapshot_console.print(self._build_layout())
            return capture.get()
        finally:
            self._restore_view_state(original_view)
            self._console = original_console

    def render_snapshot_text(self, panel_num: int | None = None) -> str:
        return self._capture_layout_text(panel_num=panel_num, refresh=True)

    def render_snapshot_json(self, panel_num: int | None = None) -> str:
        state = self._collector.collect()
        self._set_state(state)
        panel_name = PANEL_NAMES[panel_num] if panel_num is not None else ""
        payload = {
            "panel_num": panel_num,
            "panel_name": panel_name,
            "state": state.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2)

    def render_current_view_text(self) -> str:
        return self._capture_layout_text(refresh=False)

    def render_snapshot(self, panel_num: int | None = None) -> None:
        self._console.print(self.render_snapshot_text(panel_num=panel_num), end="")

    def copy_current_view(self) -> str:
        copied_text = self.render_current_view_text()
        sequence = _osc52_sequence(copied_text)
        self._console.file.write(sequence)
        self._console.file.flush()
        return copied_text

    def _snapshot_view_state(self) -> ViewSnapshot:
        with self._view_lock:
            return ViewSnapshot(
                mode=self._view.mode,
                detail_panel=self._view.detail_panel,
                focus_panel=self._view.focus_panel,
                scroll_offset=self._view.scroll_offset,
                log_sub_view=self._view.log_sub_view,
                show_help=self._view.show_help,
                profile_cycle_index=self._view.profile_cycle_index,
                filter_query=self._view.filter_query,
                filter_edit_mode=self._view.filter_edit_mode,
                session_sort=self._view.session_sort,
            )

    def _restore_view_state(self, snapshot: ViewSnapshot) -> None:
        with self._view_lock:
            self._view.mode = snapshot.mode
            self._view.detail_panel = snapshot.detail_panel
            self._view.focus_panel = snapshot.focus_panel
            self._view.scroll_offset = snapshot.scroll_offset
            self._view.log_sub_view = snapshot.log_sub_view
            self._view.show_help = snapshot.show_help
            self._view.profile_cycle_index = snapshot.profile_cycle_index
            self._view.filter_query = snapshot.filter_query
            self._view.filter_edit_mode = snapshot.filter_edit_mode
            self._view.session_sort = snapshot.session_sort

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
        if key[0] == "\x1b":
            if len(key) == 1 and self._view.filter_edit_mode:
                self._view.stop_filter()
            elif len(key) == 1 and self._view.mode == "detail":
                self._view.exit_detail()
            return None
        if self._view.filter_edit_mode:
            if key in ("\r", "\n"):
                self._view.stop_filter()
            elif key in ("\x7f", "\b"):
                self._view.pop_filter_char()
            elif len(key) == 1 and key.isprintable():
                self._view.append_filter_char(key)
            return None
        if key == "q":
            return "quit"
        if key == "r":
            self._force_refresh.set()
            return "refresh"
        if key == "?":
            self._view.show_help = not self._view.show_help
            return None
        if key == "f":
            self._view.toggle_focus()
            return None
        if key == "c":
            self.copy_current_view()
            return None
        if key == "\t" and self._view.detail_panel == _LOG_PANEL_NUM:
            self._view.cycle_log_view()
            return None
        if key == "/" and self._view.detail_panel in {_SESSIONS_PANEL_NUM, _LOG_PANEL_NUM}:
            self._view.start_filter()
            return None
        if key == "s" and self._view.detail_panel == _SESSIONS_PANEL_NUM:
            self._view.cycle_session_sort()
            return None
        if key == "p" and self._view.detail_panel == _PROFILES_PANEL_NUM:
            self._view.cycle_profile_view()
            return None
        if key == "g":
            self._view.jump_top()
            return None
        if key == "G":
            self._view.jump_bottom()
            return None
        if key in ("j",):
            self._view.scroll_down()
            return None
        if key in ("k",):
            self._view.scroll_up()
            return None
        if len(key) == 1 and key.isdigit():
            panel_num = 10 if key == "0" and 10 in _PANEL_NUMBERS else int(key)
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
            profile_view_index = self._view.profile_cycle_index
            filter_query = self._view.filter_query
            filter_edit_mode = self._view.filter_edit_mode
            session_sort = self._view.session_sort
        session_message_match_ids: set[str] | None = None
        if mode == "detail" and detail_panel == _SESSIONS_PANEL_NUM and filter_query:
            from hermesd.panels.sessions import extract_message_search_query

            message_query = extract_message_search_query(filter_query)
            if message_query:
                session_message_match_ids = self._collector.search_session_ids_by_message(
                    message_query
                )

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
                profile_view_index=profile_view_index,
                filter_query=filter_query,
                session_sort=session_sort,
                session_message_match_ids=session_message_match_ids,
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
                filter_query=filter_query,
                filter_edit_mode=filter_edit_mode,
                session_sort=session_sort,
            )
        )
        return layout

    def _build_header(self, state: DashboardState, theme: Theme | None = None) -> Text:
        active_theme = theme or self._theme
        now = datetime.now().strftime("%H:%M:%S")
        t = Text(style="on #1A1A2E")
        t.append(" ⚕ hermesd ", style=f"bold {active_theme.banner_title} on #1A1A2E")
        if state.runtime.banner:
            t.append(
                f" {state.runtime.banner} ",
                style=f"bold {active_theme.ui_warn} on #1A1A2E",
            )
        right_labels = [state.profile_mode_label]
        if state.active_skin != "default":
            right_labels.insert(0, f"{state.active_skin} skin")
        right_text = "   ".join(right_labels)
        right_segment = f"{right_text}   {now} "
        padding_width = max(1, self._console.width - len(t.plain) - len(right_segment))
        t.append(
            f"{' ' * padding_width}{right_segment}",
            style=f"{active_theme.banner_dim} on #1A1A2E",
        )
        return t

    def _build_footer(
        self,
        state: DashboardState,
        theme: Theme | None = None,
        input_error: str | None = None,
        view_mode: str | None = None,
        detail_panel: int | None = None,
        filter_query: str | None = None,
        filter_edit_mode: bool | None = None,
        session_sort: str | None = None,
    ) -> Text:
        active_theme = theme or self._theme
        if input_error is None:
            with self._lock:
                footer_error = self._input_error
        else:
            footer_error = input_error
        if (
            view_mode is None
            or detail_panel is None
            or filter_query is None
            or filter_edit_mode is None
            or session_sort is None
        ):
            with self._view_lock:
                mode = self._view.mode if view_mode is None else view_mode
                panel = self._view.detail_panel if detail_panel is None else detail_panel
                query = self._view.filter_query if filter_query is None else filter_query
                editing = (
                    self._view.filter_edit_mode if filter_edit_mode is None else filter_edit_mode
                )
                sort_mode = self._view.session_sort if session_sort is None else session_sort
        else:
            mode = view_mode
            panel = detail_panel
            query = filter_query
            editing = filter_edit_mode
            sort_mode = session_sort
        t = Text(style="on #1A1A2E")
        if mode == "overview":
            t.append(
                f" [{_panel_shortcut_label()}]",
                style=f"bold {active_theme.banner_title} on #1A1A2E",
            )
            t.append(" Expand  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append("[r]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Refresh  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append("[q]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Quit  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append("[?]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Help  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append("[f]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Focus  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append("[c]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Copy", style=f"{active_theme.session_border} on #1A1A2E")
        else:
            t.append(" [Esc]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Back  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append("[f]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Toggle focus  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append("[c]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Copy  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append("[j/k]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Scroll  ", style=f"{active_theme.session_border} on #1A1A2E")
            if panel == _LOG_PANEL_NUM:
                t.append("[Tab]", style=f"bold {active_theme.banner_title} on #1A1A2E")
                t.append(" Switch log  ", style=f"{active_theme.session_border} on #1A1A2E")
            if panel in {_SESSIONS_PANEL_NUM, _LOG_PANEL_NUM}:
                t.append("[/]", style=f"bold {active_theme.banner_title} on #1A1A2E")
                t.append(" Filter  ", style=f"{active_theme.session_border} on #1A1A2E")
            if panel == _SESSIONS_PANEL_NUM:
                t.append("[s]", style=f"bold {active_theme.banner_title} on #1A1A2E")
                t.append(" Sort  ", style=f"{active_theme.session_border} on #1A1A2E")
                t.append(f"sort={sort_mode}  ", style=f"{active_theme.banner_dim} on #1A1A2E")
            if panel in {_SKILLS_PANEL_NUM, _LOG_PANEL_NUM}:
                t.append("[g/G]", style=f"bold {active_theme.banner_title} on #1A1A2E")
                t.append(" Top/bottom  ", style=f"{active_theme.session_border} on #1A1A2E")
            if panel == _PROFILES_PANEL_NUM:
                t.append("[p]", style=f"bold {active_theme.banner_title} on #1A1A2E")
                t.append(" Cycle profile  ", style=f"{active_theme.session_border} on #1A1A2E")
            if editing:
                t.append("[Enter]", style=f"bold {active_theme.banner_title} on #1A1A2E")
                t.append(" Apply  ", style=f"{active_theme.session_border} on #1A1A2E")
            if query:
                t.append(f"filter={query}  ", style=f"{active_theme.banner_dim} on #1A1A2E")
            t.append("[q]", style=f"bold {active_theme.banner_title} on #1A1A2E")
            t.append(" Quit", style=f"{active_theme.session_border} on #1A1A2E")

        spinner = _DOTS_FRAMES[self._spinner_idx]
        stale = " (stale)" if state.is_stale else ""
        t.append("  ", style=f"{active_theme.session_border} on #1A1A2E")
        t.append("●", style=f"{_health_style(state)} on #1A1A2E")
        t.append(
            f" {state.health.ok_sources}/{state.health.total_sources}",
            style=f"{active_theme.session_border} on #1A1A2E",
        )
        if state.health.failed_sources:
            failed = ",".join(state.health.failed_sources[:3])
            if len(state.health.failed_sources) > 3:
                failed = f"{failed},+{len(state.health.failed_sources) - 3}"
            t.append(f" {failed}", style=f"{active_theme.banner_dim} on #1A1A2E")
        t.append(
            f"   {spinner} {self._refresh_rate}s{stale}",
            style=f"{active_theme.session_border} on #1A1A2E",
        )
        if footer_error:
            t.append("  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append(footer_error, style=f"{active_theme.ui_error} on #1A1A2E")
        elif state.runtime.banner:
            t.append("  ", style=f"{active_theme.session_border} on #1A1A2E")
            t.append(state.runtime.banner.lower(), style=f"{active_theme.ui_warn} on #1A1A2E")
        return t

    def _build_overview(self, state: DashboardState, theme: Theme | None = None) -> Layout:
        active_theme = theme or self._theme
        width = self._console.width
        height = self._console.height

        if width < 100 and height >= 50:
            return self._build_overview_from_spec(state, active_theme, _TALL_NARROW_LAYOUT_SPEC)
        if width < 100 or height < 30:
            return self._build_overview_from_spec(state, active_theme, _COMPACT_LAYOUT_SPEC)
        return self._build_overview_from_spec(state, active_theme, _WIDE_LAYOUT_SPEC)

    def _build_overview_from_spec(
        self,
        state: DashboardState,
        theme: Theme,
        spec: tuple[tuple[str, int | None, tuple[int, ...]], ...],
    ) -> Layout:
        body = Layout()
        rows = [
            Layout(name=row_name, size=row_size) if row_size is not None else Layout(name=row_name)
            for row_name, row_size, _ in spec
        ]
        body.split_column(*rows)
        for row_name, _, panel_nums in spec:
            row = body[row_name]
            if len(panel_nums) == 1:
                row.update(render_panel(panel_nums[0], state, theme))
                continue
            row_children = [
                Layout(name=f"{row_name}_{index}") for index, _ in enumerate(panel_nums, start=1)
            ]
            row.split_row(*row_children)
            for index, panel_num in enumerate(panel_nums, start=1):
                row[f"{row_name}_{index}"].update(render_panel(panel_num, state, theme))
        return body

    def _build_help(self, theme: Theme | None = None) -> Panel:
        active_theme = theme or self._theme
        import rich.box

        lines = Text()
        lines.append("  Keyboard Shortcuts\n\n", style=f"bold {active_theme.banner_title}")
        shortcuts = [
            (_panel_shortcut_label(), "Expand panel to full-screen"),
            ("Esc", "Return to overview"),
            ("f", "Toggle focus mode for the last selected panel"),
            ("c", "Copy the current rendered view as plain text via OSC 52"),
            ("j/k", "Scroll down/up (detail mode)"),
            ("Tab", "Cycle log sub-view (logs panel)"),
            ("/", "Edit detail filter (sessions/logs)"),
            ("s", "Cycle session sort (Sessions panel)"),
            ("g/G", "Jump to top/bottom in scrollable detail views"),
            ("p", "Cycle viewed profile (Profiles panel)"),
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


def _panel_shortcut_label() -> str:
    panel_numbers = sorted(PANEL_NAMES)
    if not panel_numbers:
        return ""
    if panel_numbers == list(range(1, 11)):
        return "1-9,0"
    if panel_numbers == list(range(panel_numbers[0], panel_numbers[-1] + 1)):
        return f"{panel_numbers[0]}-{panel_numbers[-1]}"
    shortcuts = ["0" if panel_num == 10 else str(panel_num) for panel_num in panel_numbers]
    return ",".join(shortcuts)


def _health_style(state: DashboardState) -> str:
    if state.health.total_sources == 0:
        return "#B8B8C7"
    if state.health.ok_sources == state.health.total_sources:
        return "#65D38A"
    if state.health.ok_sources >= (state.health.total_sources // 2):
        return "#F2C94C"
    return "#FF6B6B"


def _osc52_sequence(text: str) -> str:
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"\033]52;c;{encoded}\a"
