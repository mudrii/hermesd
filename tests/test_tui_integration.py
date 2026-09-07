"""End-to-end TUI integration test over a real pseudo-terminal.

Runs ``python -m hermesd --hermes-home <fixture>`` attached to a stdlib pty
(no pexpect), sends keystrokes (``3`` to open the Tokens detail view, ``q``
to quit), and asserts the process exits cleanly and promptly, restores the
terminal out of cbreak mode (best-effort), and — the critical rule — never
writes anything under the hermes home.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pty = pytest.importorskip("pty", reason="stdlib pty module unavailable on this platform")
fcntl = pytest.importorskip("fcntl", reason="fcntl unavailable on this platform")
termios = pytest.importorskip("termios", reason="termios unavailable on this platform")

from tests.test_readonly_invariant import _manifest  # noqa: E402


def _build_minimal_home(root: Path) -> Path:
    home = root / ".hermes"
    for sub in ("logs", "sessions", "skills", "memories", "cron/output"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("model:\n  default: gpt-5.4\n")
    (home / "logs" / "agent.log").write_text(
        "2026-04-09 15:41:58,123 - hermes - INFO - integration boot\n"
    )
    return home


def _wait_for_output(output: bytearray, needle: bytes, proc: subprocess.Popen, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in output:
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.05)
    return False


def test_tui_pty_session_quits_cleanly_and_never_writes_to_hermes_home(tmp_path: Path):
    home = _build_minimal_home(tmp_path)
    before = _manifest(home)

    try:
        master, slave = pty.openpty()
    except OSError as exc:
        pytest.skip(f"pty.openpty unavailable: {exc}")
    # Give the pty a real window size so Rich lays out deterministically.
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"HERMES_HOME", "HERMES_PROFILE", "NO_COLOR", "FORCE_COLOR"}
    }
    env["TERM"] = "xterm-256color"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hermesd",
            "--hermes-home",
            str(home),
            "--refresh-rate",
            "1",
        ],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
    )
    # Keep the slave open in the parent so we can inspect its termios after
    # the child exits (it shares the child's stdin terminal settings).

    output = bytearray()

    def drain_master() -> None:
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                return  # child exited and slave fully closed
            if not chunk:
                return
            output.extend(chunk)

    reader = threading.Thread(target=drain_master, daemon=True)
    reader.start()

    try:
        # First full render shows the overview with the gateway panel.
        assert _wait_for_output(output, b"Gateway", proc, timeout=30), (
            f"TUI never rendered the overview; output so far: {bytes(output)[-500:]!r}"
        )
        # "3" opens the Tokens detail view ("Recent Windows" only renders in
        # the detail layout, so this proves the keypress was handled).
        os.write(master, b"3")
        assert _wait_for_output(output, b"Recent Windows", proc, timeout=15), (
            "detail view for panel 3 never rendered"
        )
        os.write(master, b"q")
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert proc.returncode == 0, (
        f"hermesd exited with {proc.returncode}; tail of output: {bytes(output)[-500:]!r}"
    )

    # Best-effort: the input loop must have restored the terminal (cbreak
    # clears ICANON; the restore on quit must set it back).
    try:
        lflag = termios.tcgetattr(slave)[3]
        assert lflag & termios.ICANON, "terminal left in cbreak mode (ICANON clear)"
    except OSError:
        pass  # slave already torn down; nothing to verify
    finally:
        os.close(slave)
        os.close(master)

    # Critical rule: a full interactive session must not write under ~/.hermes.
    assert _manifest(home) == before
