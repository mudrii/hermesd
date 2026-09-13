"""Content-free inventory of the app-level Desktop plugin root."""

from __future__ import annotations

import stat
from itertools import islice
from pathlib import Path

from hermesd.collect.common import _exists_strict, _path_resolves_under
from hermesd.models import DesktopPluginInfo

# One lstat for each retained directory and its entry file on every refresh.
# Keep the root bounded because ~/.hermes is untrusted input.
DESKTOP_PLUGIN_LIMIT = 200


def _confined_directory(path: Path, root: Path) -> bool:
    if path.is_symlink() or not _path_resolves_under(path, root):
        return False
    try:
        return stat.S_ISDIR(path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


def _confined_regular_file(path: Path, root: Path) -> bool:
    if path.is_symlink() or not _path_resolves_under(path, root):
        return False
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


def read_desktop_plugins(root: Path, hermes_home: Path) -> tuple[list[DesktopPluginInfo], bool]:
    """Return root ``<name>/plugin.js`` entries without reading JavaScript.

    The app-level root and marker are defined by
    ``apps/desktop/electron/desktop-plugins-root.ts:1-38``. Desktop extensions
    belong to the local app, not the selected agent profile. Only directory and
    regular-file metadata is inspected; load and enablement state is not stored
    safely on disk and cannot be inferred here.
    """
    if root.is_symlink() or not _path_resolves_under(root, hermes_home):
        raise RuntimeError("desktop plugin root is unsafe")
    if not _exists_strict(root):
        return [], False
    if not _confined_directory(root, hermes_home):
        raise RuntimeError("desktop plugin root is not a confined directory")

    entries = list(islice(root.iterdir(), DESKTOP_PLUGIN_LIMIT + 1))
    truncated = len(entries) > DESKTOP_PLUGIN_LIMIT
    plugins: list[DesktopPluginInfo] = []
    for child in sorted(entries[:DESKTOP_PLUGIN_LIMIT], key=lambda path: path.name):
        if not _confined_directory(child, root):
            continue
        if not _confined_regular_file(child / "plugin.js", root):
            continue
        plugins.append(DesktopPluginInfo(name=child.name))
    return plugins, truncated
