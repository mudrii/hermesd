from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from hermesd import __version__


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _snapshot_panel_num(value: str) -> int:
    from hermesd.panels import PANEL_NAMES

    parsed = 10 if value == "0" else int(value)
    if parsed not in PANEL_NAMES:
        raise argparse.ArgumentTypeError("snapshot panel must be 1-9 or 0 (panel 10)")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hermesd",
        description="TUI monitoring dashboard for Hermes AI agent",
    )
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=None,
        help="Override ~/.hermes (default: $HERMES_HOME or ~/.hermes)",
    )
    parser.add_argument(
        "--refresh-rate",
        type=_positive_int,
        default=5,
        help="Polling interval in seconds (default: 5)",
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
        type=Path,
        default=None,
        help="Write the one-shot snapshot to a file instead of stdout",
    )
    parser.add_argument(
        "--snapshot-panel",
        type=_snapshot_panel_num,
        default=None,
        help="Select panel 1-9 or 0 (panel 10); text snapshots render that detail view, JSON snapshots annotate full-state output",
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
        default=32768,
        help="Bytes read from the end of each log file per refresh (default: 32768)",
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
    return Path.home() / ".hermes"


def resolve_profile_name(args: argparse.Namespace) -> str | None:
    profile = args.profile
    if isinstance(profile, str) and profile:
        return profile
    env = os.environ.get("HERMES_PROFILE")
    if env:
        return env
    return None


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    hermes_home = resolve_hermes_home(args)
    profile_name = resolve_profile_name(args)
    if not hermes_home.is_dir():
        print(f"Error: {hermes_home} does not exist", file=sys.stderr)
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
        if args.snapshot_format == "json":
            snapshot_text = app.render_snapshot_json(panel_num=args.snapshot_panel)
        else:
            snapshot_text = app.render_snapshot_text(panel_num=args.snapshot_panel)
        if args.snapshot_file is not None:
            args.snapshot_file.write_text(snapshot_text)
        else:
            if args.snapshot_format == "json":
                print(snapshot_text)
            else:
                app.render_snapshot(panel_num=args.snapshot_panel)
        app.close()
        return
    app.run()


if __name__ == "__main__":
    main()
