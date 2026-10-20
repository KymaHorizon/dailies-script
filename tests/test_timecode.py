# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Non-drop timecode at an integer frame rate. No drop-frame support, by design."""

import pytest

from dailies.timecode import TimecodeError, frames_to_tc, tc_to_frames


def test_tc_to_frames_uses_the_spec_formula():
    assert tc_to_frames("10:14:22:31", fps=50) == (10 * 3600 + 14 * 60 + 22) * 50 + 31


def test_zero_timecode_is_frame_zero():
    assert tc_to_frames("00:00:00:00", fps=50) == 0


def test_last_frame_of_the_day():
    assert tc_to_frames("23:59:59:49", fps=50) == 24 * 3600 * 50 - 1


def test_frame_field_above_49_is_rejected_at_50_fps():
    with pytest.raises(TimecodeError, match="frame field"):
        tc_to_frames("10:00:00:50", fps=50)


def test_drop_frame_separator_is_rejected():
    with pytest.raises(TimecodeError, match="drop-frame"):
        tc_to_frames("10:00:00;10", fps=50)


@pytest.mark.parametrize("bad", ["", "10:00:00", "10:00:00:00:00", "aa:bb:cc:dd", "24:00:00:00", "10:60:00:00", "10:00:60:00", "1:2:3:4"])
def test_malformed_timecode_is_rejected(bad):
    with pytest.raises(TimecodeError):
        tc_to_frames(bad, fps=50)


def test_frames_to_tc_round_trip():
    assert frames_to_tc(tc_to_frames("10:14:57:13", fps=50), fps=50) == "10:14:57:13"


def test_frames_to_tc_wraps_at_midnight():
    assert frames_to_tc(24 * 3600 * 50 + 5, fps=50) == "00:00:00:05"


def test_negative_frame_count_is_rejected():
    with pytest.raises(TimecodeError):
        frames_to_tc(-1, fps=50)
