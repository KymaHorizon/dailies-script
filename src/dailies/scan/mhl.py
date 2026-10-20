# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""ASC MHL gate.

A camera or sound folder is scanned only when it carries an ASC MHL history
and every file the history lists is present at the listed size. A folder that
is still being copied, or that was never sealed, is skipped with a reason. It
is never scanned optimistically. Checksums are not recomputed here: the offload
tool already did that, and a spot check is a separate, explicit command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

IGNORED_NAMES = {".DS_Store", "Thumbs.db"}


@dataclass(frozen=True)
class MhlGate:
    eligible: bool
    reason: str
    files: dict[str, int] = field(default_factory=dict)  # relative path to size in bytes
    sealed_mtime_ns: int = 0  # newest change in the history, for the poll mode stability check


def _listed_files(folder: Path) -> dict[str, int]:
    from ascmhl.history import MHLHistory  # imported here so the rest of scan works without ascmhl

    history = MHLHistory.load_from_path(str(folder))
    files: dict[str, int] = {}
    for hash_list in history.hash_lists:  # generations in order, a later one wins
        for entry in hash_list.media_hashes:
            if not entry.is_directory and entry.file_size is not None:
                files[entry.path] = int(entry.file_size)
    return files


def check_mhl(folder: Path) -> MhlGate:
    folder = Path(folder)
    history_dir = folder / "ascmhl"
    if not history_dir.is_dir():
        return MhlGate(False, f"no ascmhl folder in {folder.name}, offload not sealed")
    try:
        files = _listed_files(folder)
    except Exception as error:  # the library raises several unrelated types on a damaged history
        return MhlGate(False, f"unreadable ASC MHL history in {folder.name}: {type(error).__name__} {error}")
    if not files:
        return MhlGate(False, f"the ASC MHL history in {folder.name} lists no files")

    problems = []
    for relative, size in sorted(files.items()):
        path = folder / relative
        if not path.is_file():
            problems.append(f"missing {relative}")
        elif path.stat().st_size != size:
            problems.append(f"size of {relative} is {path.stat().st_size}, the MHL lists {size}")
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if path.is_file() and relative.parts[0] != "ascmhl" and path.name not in IGNORED_NAMES \
                and not path.name.startswith("._") and str(relative) not in files:
            problems.append(f"{relative} is not listed in the MHL")
    sealed = max(p.stat().st_mtime_ns for p in history_dir.iterdir())
    if problems:
        shown = "; ".join(problems[:5]) + (f"; and {len(problems) - 5} more" if len(problems) > 5 else "")
        return MhlGate(False, f"{folder.name}: {shown}", files, sealed)
    return MhlGate(True, "sealed and complete", files, sealed)
