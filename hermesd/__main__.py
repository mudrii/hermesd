from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hermesd import __version__


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
        type=int,
        default=5,
        help="Polling interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable color output",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"hermesd {__version__}",
    )
    return parser.parse_args(argv)


def resolve_hermes_home(args: argparse.Namespace) -> Path:
    import os

    cli_home: Path | None = args.hermes_home
    if cli_home is not None:
        return cli_home.expanduser()
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    hermes_home = resolve_hermes_home(args)
    if not hermes_home.is_dir():
        print(f"Error: {hermes_home} does not exist", file=sys.stderr)
        sys.exit(1)
    from hermesd.app import DashboardApp

    app = DashboardApp(
        hermes_home=hermes_home,
        refresh_rate=args.refresh_rate,
        no_color=args.no_color,
    )
    app.run()


if __name__ == "__main__":
    main()
