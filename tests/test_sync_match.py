# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Sync matching: pure functions in the sample domain. 50 fps and 48 kHz, one frame is exactly 960 samples.

Every case in the spec's fixture table that concerns sync has a test here, plus the real one hour
recorder glitch from a past show and the boundary cases around them."""

from __future__ import annotations

import pytest

from dailies.sync.match import (
    PictureRange,
    SoundRange,
    SyncMathError,
    match,
    picture_range,
    samples_to_tc,
    wrap_offset,
)

RATE, FPS, SPF = 48000, 50, 960
DAY = 24 * 3600 * RATE
WINDOW = 4 * 3600 * RATE


def S(tc: str, extra_samples: int = 0) -> int:
    hh, mm, ss, ff = (int(x) for x in tc.split(":"))
    return ((hh * 3600 + mm * 60 + ss) * FPS + ff) * SPF + extra_samples


def pic(start_tc: str, frames: int, scene=None, take=None, mos=False, clip_id="A001_C001_1012AB") -> PictureRange:
    start, end = picture_range(start_tc, frames, fps=FPS, rate=RATE)
    return PictureRange(clip_id, start, end, scene, take, mos)


def snd(start: int, seconds: float, scene=None, take=None, path="/s/SD014/x.wav", roll="SD014") -> SoundRange:
    return SoundRange(path, start, start + int(seconds * RATE), scene, take, roll, 8, ("MIX L", "MIX R"))


def run(picture, sounds):
    return match(picture, sounds, fps=FPS, rate=RATE, window_samples=WINDOW)


# ---------------------------------------------------------------- the maths


def test_one_frame_is_exactly_960_samples():
    start, end = picture_range("10:14:22:31", 1732, fps=FPS, rate=RATE)
    assert start == ((10 * 3600 + 14 * 60 + 22) * 50 + 31) * 960
    assert end - start == 1732 * 960


def test_a_rate_that_does_not_divide_into_whole_samples_is_refused():
    with pytest.raises(SyncMathError, match="whole number"):
        picture_range("10:00:00:00", 10, fps=50, rate=44101)


def test_offset_is_picture_start_minus_sound_start_and_stays_an_integer():
    result = run(pic("10:14:22:31", 1732), [snd(S("10:14:20:00", 123), 120)])
    assert result.offset_samples == S("10:14:22:31") - S("10:14:20:00", 123)
    assert isinstance(result.offset_samples, int)
    assert result.offset_samples % 960 != 0  # a recorder does not start on a frame boundary, and need not


def test_positive_offset_means_sound_started_first():
    assert run(pic("10:00:10:00", 100), [snd(S("10:00:00:00"), 60)]).offset_samples == 10 * RATE


def test_negative_offset_means_sound_started_late():
    result = run(pic("10:00:00:00", 500), [snd(S("10:00:02:00"), 60)])
    assert result.offset_samples == -2 * RATE


def test_sound_timecode_is_for_display_only():
    assert samples_to_tc(S("10:14:20:00", 500), fps=FPS, rate=RATE) == "10:14:20:00"
    assert samples_to_tc(S("23:59:59:49") + DAY, fps=FPS, rate=RATE) == "23:59:59:49"


# ------------------------------------------------------------------ matched


def test_one_file_covering_the_whole_clip_is_matched():
    result = run(pic("10:14:22:31", 1732), [snd(S("10:14:20:00"), 120)])
    assert (result.status, result.flags) == ("matched", ())
    assert result.chosen.path == "/s/SD014/x.wav"


def test_sound_that_covers_the_clip_exactly_is_matched():
    picture = pic("10:00:00:00", 250)
    assert run(picture, [SoundRange("/s/a.wav", picture.start, picture.end, None, None, "SD", 2, ())]).status == "matched"


def test_a_very_short_clip_under_a_second_syncs_normally():
    result = run(pic("10:14:22:31", 7), [snd(S("10:14:20:00"), 60)])
    assert result.status == "matched" and result.offset_samples == S("10:14:22:31") - S("10:14:20:00")


def test_one_sound_file_spanning_several_clips_gives_each_its_own_offset():
    long_take = snd(S("10:00:00:00"), 600)
    offsets = [run(pic(tc, 500, clip_id=f"A001_C00{i}"), [long_take]).offset_samples
               for i, tc in enumerate(("10:00:30:00", "10:03:00:25", "10:08:15:49"), start=1)]
    assert offsets == [S("10:00:30:00") - long_take.start, S("10:03:00:25") - long_take.start,
                       S("10:08:15:49") - long_take.start]


def test_sound_from_another_time_of_day_is_not_considered():
    sounds = [snd(S("09:00:00:00"), 60, path="/s/early.wav"), snd(S("10:14:20:00"), 120, path="/s/right.wav")]
    result = run(pic("10:14:22:31", 1732), sounds)
    assert result.status == "matched" and result.chosen.path == "/s/right.wav"
    assert [c.path for c in result.candidates] == ["/s/right.wav"]


# ---------------------------------------------------------- partial overlap


def test_sound_that_stops_before_the_clip_does_is_partial_and_flagged():
    result = run(pic("10:00:00:00", 3000), [snd(S("09:59:50:00"), 40)])  # clip is 60 s, sound covers 30 s of it
    assert (result.status, result.flags) == ("partial_overlap", ("sound_gap",))
    assert result.offset_samples == 10 * RATE


def test_sound_that_starts_after_the_clip_does_is_partial_with_a_negative_offset():
    result = run(pic("10:00:00:00", 3000), [snd(S("10:00:20:00"), 120)])
    assert result.status == "partial_overlap" and result.offset_samples == -20 * RATE


def test_ranges_that_only_touch_do_not_overlap():
    picture = pic("10:00:00:00", 500)
    assert run(picture, [snd(picture.end, 60)]).status == "no_sound"
    assert run(picture, [SoundRange("/s/a.wav", picture.start - RATE, picture.start, None, None, "SD", 2, ())]).status == "no_sound"


# ------------------------------------------------------ multiple candidates


def test_two_recorders_covering_the_clip_picks_the_longest_and_flags_for_review():
    sounds = [snd(S("10:00:05:00"), 120, path="/s/b_late.wav"), snd(S("09:59:50:00"), 120, path="/s/a_full.wav")]
    result = run(pic("10:00:00:00", 1500), sounds)
    assert (result.status, result.flags) == ("multiple_candidates", ("sync_review",))
    assert result.chosen.path == "/s/a_full.wav"
    assert [c.path for c in result.candidates] == ["/s/a_full.wav", "/s/b_late.wav"]


def test_equal_overlap_is_decided_by_slate_agreement():
    sounds = [snd(S("09:59:50:00"), 120, "14A", "2", path="/s/a.wav"), snd(S("09:59:50:00"), 120, "14A", "3", path="/s/b.wav")]
    result = run(pic("10:00:00:00", 1500, "14A", "3"), sounds)
    assert result.chosen.path == "/s/b.wav" and result.status == "multiple_candidates"


def test_ties_are_broken_the_same_way_every_time():
    a, b = snd(S("09:59:50:00"), 120, path="/s/a.wav"), snd(S("09:59:50:00"), 120, path="/s/b.wav")
    assert run(pic("10:00:00:00", 1500), [a, b]).chosen == run(pic("10:00:00:00", 1500), [b, a]).chosen


def test_a_sliver_of_the_next_take_does_not_make_it_a_candidate_for_review():
    sounds = [snd(S("09:59:50:00"), 40, path="/s/this_take.wav"), snd(S("10:00:29:00"), 60, path="/s/next_take.wav")]
    result = run(pic("10:00:00:00", 1500), sounds)  # clip is 30 s, the next take's sound overlaps its last second
    assert result.status == "matched" and result.chosen.path == "/s/this_take.wav"


def test_a_take_recorded_in_two_files_is_partial_not_multiple():
    sounds = [snd(S("09:59:50:00"), 28, path="/s/part1.wav"), snd(S("10:00:18:00"), 60, path="/s/part2.wav")]
    result = run(pic("10:00:00:00", 1500), sounds)  # 18 s from part one, 12 s from part two
    assert result.status == "partial_overlap" and result.chosen.path == "/s/part1.wav"


# ------------------------------------------------------------------ no sound


def test_a_clip_with_no_sound_at_all_is_flagged_not_held_back():
    result = run(pic("10:00:00:00", 500), [])
    assert (result.status, result.flags, result.chosen, result.offset_samples) == ("no_sound", ("no_sound",), None, None)


def test_sound_nearby_that_does_not_overlap_is_no_sound():
    result = run(pic("10:00:00:00", 500), [snd(S("10:05:00:00"), 60)])
    assert result.status == "no_sound"


# ------------------------------------------------------------ implausible TC


def test_sound_timecode_hours_away_means_jam_sync_was_lost():
    result = run(pic("10:00:00:00", 500), [snd(S("15:30:00:00"), 60)])
    assert (result.status, result.flags) == ("tc_implausible", ("tc_implausible",))
    assert result.chosen is None and result.offset_samples is None
    assert "05:29:50:00" in result.note


def test_the_window_is_a_boundary_not_a_guess():
    clip = pic("10:00:00:00", 500)
    inside = snd(clip.end + WINDOW, 60)
    outside = snd(clip.end + WINDOW + 1, 60)
    assert run(clip, [inside]).status == "no_sound"
    assert run(clip, [outside]).status == "tc_implausible"


def test_a_recorder_running_one_hour_slow_is_caught_by_the_slate():
    # real incident: after re-jamming a personal mic the recorder ran exactly one hour slow.
    # One hour is inside the four hour window, so distance alone would call this plain no_sound.
    result = run(pic("15:00:00:00", 1500, "83G", "5"), [snd(S("14:00:00:00") - 5 * RATE, 60, "83G", "5")])
    assert result.status == "tc_implausible"
    assert result.candidates[0].slate_agrees is True
    assert "01:00:05:00" in result.note


def test_one_hour_slow_sound_that_lands_on_another_clip_is_flagged_by_the_slate():
    # the slow file for 83G take 5 overlaps the clip that was really shot at 14:00, which is 82 take 1
    result = run(pic("14:00:00:00", 1500, "82", "1"), [snd(S("14:00:00:00") - 5 * RATE, 60, "83G", "5")])
    assert result.status == "matched"
    assert result.flags == ("slate_mismatch",)


def test_slates_agree_despite_padding_and_case():
    result = run(pic("10:00:00:00", 500, "14a", "03"), [snd(S("09:59:50:00"), 60, "14A", "3")])
    assert result.flags == () and result.candidates[0].slate_agrees is True


def test_a_missing_slate_is_no_opinion_not_a_mismatch():
    result = run(pic("10:00:00:00", 500, "14A", "3"), [snd(S("09:59:50:00"), 60)])
    assert result.flags == () and result.candidates[0].slate_agrees is None


# ------------------------------------------------------------- midnight wrap


def test_wrap_offset_leaves_ordinary_offsets_alone():
    assert wrap_offset(5 * RATE, rate=RATE) == 5 * RATE
    assert wrap_offset(-12 * 3600 * RATE, rate=RATE) == -12 * 3600 * RATE


def test_wrap_offset_corrects_by_a_day_in_either_direction():
    assert wrap_offset(-(DAY - 1200 * RATE), rate=RATE) == 1200 * RATE
    assert wrap_offset(DAY - 260 * RATE, rate=RATE) == -260 * RATE


def test_night_shoot_picture_after_midnight_sound_started_before():
    result = run(pic("00:10:00:00", 1500), [snd(S("23:50:00:00"), 1800)])
    assert result.status == "matched"
    assert result.offset_samples == 20 * 60 * RATE
    assert "midnight" in result.note


def test_night_shoot_picture_started_before_midnight_sound_after():
    result = run(pic("23:59:00:00", 6000), [snd(S("00:00:30:00"), 600)])  # two minute clip, sound starts 90 s in
    assert result.status == "partial_overlap"
    assert result.offset_samples == -90 * RATE


def test_midnight_wrap_never_rescues_a_genuinely_implausible_offset():
    assert run(pic("01:00:00:00", 500), [snd(S("13:30:00:00"), 60)]).status == "tc_implausible"


# ----------------------------------------------------------------------- MOS


def test_mos_clip_is_mute_and_not_flagged_even_if_sound_overlaps():
    result = run(pic("10:00:00:00", 500, mos=True), [snd(S("09:59:50:00"), 60)])
    assert (result.status, result.flags, result.chosen) == ("mos", (), None)
