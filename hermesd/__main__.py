from __future__ import annotations

import argparse
import os
import signal
import sys
import tempfile
from pathlib import Path
from types import FrameType

from hermesd import __version__
from hermesd.defaults import DEFAULT_LOG_TAIL_BYTES, DEFAULT_REFRESH_RATE, MAX_REFRESH_RATE
from hermesd.paths import default_hermes_home

# Argparse reports a type function's ValueError as "invalid <__name__> value",
# so every type function raises ArgumentTypeError with its own message instead.


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid positive integer: {value!r}") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _refresh_rate(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > MAX_REFRESH_RATE:
        raise argparse.ArgumentTypeError(f"value must be at most {MAX_REFRESH_RATE} seconds")
    return parsed


def _non_empty_path(value: str) -> Path:
    # Path("") is ".", which would silently target the working directory.
    if not value:
        raise argparse.ArgumentTypeError("path must not be empty")
    return Path(value)


def _snapshot_panel_num(value: str) -> int:
    from hermesd.panels import PANEL_NAMES

    try:
        parsed = int(value)
    except ValueError:
        parsed = None
    if parsed == 0:
        parsed = 10
    if parsed not in PANEL_NAMES:
        available = ", ".join(str(panel) for panel in sorted(PANEL_NAMES))
        raise argparse.ArgumentTypeError(f"snapshot panel must be one of: {available}")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hermesd",
        description="TUI monitoring dashboard for Hermes AI agent",
    )
    parser.add_argument(
        "--hermes-home",
        type=_non_empty_path,
        default=None,
        help="Override ~/.hermes (default: $HERMES_HOME or ~/.hermes)",
    )
    parser.add_argument(
        "--refresh-rate",
        type=_refresh_rate,
        default=DEFAULT_REFRESH_RATE,
        help=f"Polling interval in seconds (default: {DEFAULT_REFRESH_RATE})",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Read profile-scoped data from ~/.hermes/profiles/<name> (default: root only)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable color output",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Render one overview snapshot to stdout and exit",
    )
    parser.add_argument(
        "--snapshot-file",
        type=_non_empty_path,
        default=None,
        help="Write the one-shot snapshot to a file instead of stdout",
    )
    parser.add_argument(
        "--snapshot-panel",
        type=_snapshot_panel_num,
        default=None,
        help="Select a panel by number (0 aliases panel 10); text snapshots render that detail view, JSON snapshots annotate full-state output",
    )
    parser.add_argument(
        "--snapshot-format",
        choices=("text", "json"),
        default="text",
        help="Snapshot output format (default: text)",
    )
    parser.add_argument(
        "--log-tail-bytes",
        type=_positive_int,
        default=DEFAULT_LOG_TAIL_BYTES,
        help=(
            "Bytes read from the end of each log file and cron output excerpt "
            f"per refresh (default: {DEFAULT_LOG_TAIL_BYTES})"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"hermesd {__version__}",
    )
    return parser.parse_args(argv)


def resolve_hermes_home(args: argparse.Namespace) -> Path:
    cli_home: Path | None = args.hermes_home
    if cli_home is not None:
        return cli_home.expanduser()
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return default_hermes_home()


def resolve_profile_name(args: argparse.Namespace) -> str | None:
    profile: str | None = args.profile
    if profile:
        return profile
    env = os.environ.get("HERMES_PROFILE")
    if env:
        return env
    return None


def _snapshot_file_inside_hermes_home(snapshot_file: Path, hermes_home: Path) -> bool:
    output_path = snapshot_file.expanduser().resolve(strict=False)
    home_path = hermes_home.expanduser().resolve(strict=False)
    if output_path == home_path or output_path.is_relative_to(home_path):
        return True
    # Path strings miss aliases of the same directory (case-insensitive
    # filesystems: $d/HOME is $d/home), so compare existing ancestors by identity.
    try:
        home_stat = home_path.stat()
    except OSError:
        return False
    for ancestor in (output_path, *output_path.parents):
        try:
            if os.path.samestat(ancestor.stat(), home_stat):
                return True
        except OSError:
            continue
    return False


def _write_snapshot_file(output_path: Path, snapshot_text: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(snapshot_text)
        os.replace(temp_path, output_path)
    except BaseException:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    hermes_home = resolve_hermes_home(args)
    profile_name = resolve_profile_name(args)
    if not hermes_home.is_dir():
        print(f"Error: {hermes_home} does not exist", file=sys.stderr)
        sys.exit(1)
    if args.snapshot_file is not None and _snapshot_file_inside_hermes_home(
        args.snapshot_file,
        hermes_home,
    ):
        print("Error: --snapshot-file must not write under hermes home", file=sys.stderr)
        sys.exit(1)
    from hermesd.app import DashboardApp

    try:
        app = DashboardApp(
            hermes_home=hermes_home,
            refresh_rate=args.refresh_rate,
            no_color=args.no_color,
            profile_name=profile_name,
            log_tail_bytes=args.log_tail_bytes,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    if (
        args.snapshot
        or args.snapshot_file is not None
        or args.snapshot_panel is not None
        or args.snapshot_format != "text"
    ):
        # Mirror the signal handling in DashboardApp.run() so Ctrl+C/SIGTERM
        # during a slow collect exits cleanly instead of dumping a traceback.
        # The first signal is caught; the handler then re-arms the default
        # disposition so a second signal can still kill a wedged collect.
        interrupted_signal: list[int] = []

        def _snapshot_signal_handler(sig: int, frame: FrameType | None) -> None:
            interrupted_signal.append(sig)
            signal.signal(sig, signal.SIG_DFL)

        previous_sigint = signal.signal(signal.SIGINT, _snapshot_signal_handler)
        previous_sigterm = signal.signal(signal.SIGTERM, _snapshot_signal_handler)
        try:
            if args.snapshot_format == "json":
                snapshot_text = app.render_snapshot_json(panel_num=args.snapshot_panel)
            else:
                snapshot_text = app.render_snapshot_text(panel_num=args.snapshot_panel)
            if interrupted_signal:
                sig = interrupted_signal[0]
                print(
                    f"Interrupted by signal {sig}; snapshot discarded.",
                    file=sys.stderr,
                )
                sys.exit(128 + sig)
            if args.snapshot_file is not None:
                output_path = args.snapshot_file.expanduser().resolve(strict=False)
                if _snapshot_file_inside_hermes_home(output_path, hermes_home):
                    print(
                        "Error: --snapshot-file must not write under hermes home", file=sys.stderr
                    )
                    sys.exit(1)
                _write_snapshot_file(output_path, snapshot_text)
            else:
                if args.snapshot_format == "json":
                    print(snapshot_text)
                else:
                    print(snapshot_text, end="")
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)
            app.close()
        return
    app.run()


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess test
    main()
