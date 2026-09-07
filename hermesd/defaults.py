"""Runtime defaults shared by the CLI, the TUI app and the collector.

Defined once so a change cannot drift between the argparse default, the
DashboardApp keyword default and the Collector keyword default.
"""

from __future__ import annotations

# Seconds between collector passes.
DEFAULT_REFRESH_RATE = 5
# Bytes read from the end of each log file and cron output excerpt per refresh.
DEFAULT_LOG_TAIL_BYTES = 32768
