# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Non-drop timecode at an integer frame rate.

The show runs at 50 fps with no drop-frame anywhere, so a semicolon separator
is an error and not something to adapt to. Frames are for display and for the
container timecode. Sync maths lives in the sample domain.
"""

from __future__ import annotations

import re

_TC = re.compile(r"^(\d{2}):(\d{2}):(\d{2}):(\d{2})$")


class TimecodeError(ValueError):
    """A timecode string or frame count that cannot be valid for this show."""


def tc_to_frames(tc: str, *, fps: int) -> int:
    """Frames since midnight: (hh*3600 + mm*60 + ss) * fps + ff."""
    if ";" in tc:
        raise TimecodeError(f"drop-frame timecode {tc!r} is not valid at {fps} fps")
    match = _TC.match(tc)
    if not match:
        raise TimecodeError(f"malformed timecode {tc!r}, expected HH:MM:SS:FF")
    hh, mm, ss, ff = (int(group) for group in match.groups())
    if hh > 23 or mm > 59 or ss > 59:
        raise TimecodeError(f"timecode {tc!r} has a field out of range")
    if ff >= fps:
        raise TimecodeError(f"timecode {tc!r} frame field exceeds {fps - 1} at {fps} fps")
    return (hh * 3600 + mm * 60 + ss) * fps + ff


def frames_to_tc(frames: int, *, fps: int) -> str:
    """Timecode for a frame count, wrapping at 24 hours."""
    if frames < 0:
        raise TimecodeError(f"negative frame count {frames}")
    frames %= 24 * 3600 * fps
    seconds, ff = divmod(frames, fps)
    minutes, ss = divmod(seconds, 60)
    hh, mm = divmod(minutes, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"
