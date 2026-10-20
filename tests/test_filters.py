# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""FFmpeg filter string builders. Pure string functions, no FFmpeg needed."""

from __future__ import annotations

from pathlib import Path

import pytest

from dailies.commands.filters import (
    FilterError,
    drawtext,
    filter_value,
    mix_pan,
    sync_audio_filters,
)

FONT = Path("/show/config/fonts/Courier.ttf")


# ---------------------------------------------------------------- escaping


def test_plain_text_is_only_quoted():
    assert filter_value("A001_C003_1012AB") == "'A001_C003_1012AB'"


def test_colons_are_escaped_for_the_option_parser():
    assert filter_value("10:14:22:31") == r"'10\:14\:22\:31'"


def test_backslash_is_escaped_before_anything_else():
    assert filter_value("a\\b") == "'a\\\\b'"


def test_single_quote_survives_both_parsing_levels():
    # option level turns ' into \' and the graph level cannot hold ' inside quotes,
    # so the quote is closed, an escaped quote is emitted, and the quote reopens
    assert filter_value("It's") == "'It\\'\\''s'"


def test_graph_separators_are_safe_inside_quotes():
    assert filter_value("14A, take 3; [wide]") == "'14A, take 3; [wide]'"


def test_newlines_are_rejected():
    with pytest.raises(FilterError, match="line break"):
        filter_value("two\nlines")


# ---------------------------------------------------------------- drawtext


def test_static_field_top_left():
    assert drawtext(font=FONT, text="A001_C003", position="top_left", fontsize=27, margin=22) == (
        "drawtext=fontfile='/show/config/fonts/Courier.ttf':text='A001_C003':expansion=none"
        ":fontsize=27:fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=4:x=22:y=22"
    )


@pytest.mark.parametrize(
    "position, x, y",
    [
        ("top_left", "22", "22"),
        ("top_center", "(w-tw)/2", "22"),
        ("top_right", "w-tw-22", "22"),
        ("bottom_left", "22", "h-lh-22"),
        ("bottom_center", "(w-tw)/2", "h-lh-22"),
        ("bottom_right", "w-tw-22", "h-lh-22"),
    ],
)
def test_positions(position, x, y):
    assert drawtext(font=FONT, text="x", position=position, fontsize=27, margin=22).endswith(f":x={x}:y={y}")


def test_unknown_position_is_rejected():
    with pytest.raises(FilterError, match="position"):
        drawtext(font=FONT, text="x", position="middle", fontsize=27, margin=22)


def test_running_timecode_counts_from_the_clip_start_at_the_show_rate():
    result = drawtext(font=FONT, text="SRC ", position="bottom_left", fontsize=27, margin=22,
                      timecode="10:14:22:31", fps=50)
    assert ":text='SRC '" in result
    assert result.endswith(r":timecode='10\:14\:22\:31':timecode_rate=50")


def test_running_timecode_needs_a_rate():
    with pytest.raises(FilterError, match="fps"):
        drawtext(font=FONT, text="SRC ", position="bottom_left", fontsize=27, margin=22, timecode="10:14:22:31")


def test_running_timecode_is_validated():
    with pytest.raises(FilterError, match="frame field"):
        drawtext(font=FONT, text="", position="bottom_left", fontsize=27, margin=22, timecode="10:14:22:50", fps=50)


def test_font_path_with_spaces_and_colons_is_escaped():
    result = drawtext(font=Path("/Volumes/My Show: A/Courier New.ttf"), text="x", position="top_left",
                      fontsize=27, margin=22)
    assert r"fontfile='/Volumes/My Show\: A/Courier New.ttf'" in result


# -------------------------------------------------------------- audio sync


def test_sound_started_early_is_trimmed_at_the_head():
    assert sync_audio_filters(offset_samples=113280, picture_samples=1732 * 960, sample_rate=48000) == (
        "atrim=start_sample=113280",
        "asetpts=N/SR/TB",
        "apad=whole_len=1662720",
        "atrim=end_sample=1662720",
    )


def test_sound_started_late_is_delayed_in_samples_not_milliseconds():
    assert sync_audio_filters(offset_samples=-113, picture_samples=96000, sample_rate=48000) == (
        "adelay=113S:all=1",
        "apad=whole_len=96000",
        "atrim=end_sample=96000",
    )


def test_zero_offset_still_pads_and_trims_to_the_picture_length():
    assert sync_audio_filters(offset_samples=0, picture_samples=96000, sample_rate=48000) == (
        "apad=whole_len=96000",
        "atrim=end_sample=96000",
    )


@pytest.mark.parametrize("offset", [1.5, "113280", True, None])
def test_offset_must_be_an_integer(offset):
    with pytest.raises(FilterError, match="offset_samples"):
        sync_audio_filters(offset_samples=offset, picture_samples=96000, sample_rate=48000)


def test_picture_length_must_be_positive():
    with pytest.raises(FilterError, match="picture_samples"):
        sync_audio_filters(offset_samples=0, picture_samples=0, sample_rate=48000)


# --------------------------------------------------------------------- pan


def test_mono_pan_picks_one_track_counting_from_one():
    assert mix_pan((2,), layout="mono", channels=8) == "pan=mono|c0=c1"


def test_stereo_pan_from_two_mix_tracks():
    assert mix_pan((1, 2), layout="stereo", channels=8) == "pan=stereo|c0=c0|c1=c1"


def test_single_mix_track_is_patched_to_both_stereo_channels():
    assert mix_pan((1,), layout="stereo", channels=8) == "pan=stereo|c0=c0|c1=c0"


def test_track_beyond_the_file_is_rejected():
    with pytest.raises(FilterError, match="track 3"):
        mix_pan((3,), layout="mono", channels=2)


def test_mono_pan_takes_exactly_one_track():
    with pytest.raises(FilterError, match="one track"):
        mix_pan((1, 2), layout="mono", channels=8)


def test_unknown_pan_layout_is_rejected():
    with pytest.raises(FilterError, match="layout"):
        mix_pan((1,), layout="5.1", channels=8)


# ---------------------------------------------------------------- exact CDL

from dailies.commands.filters import cdl_filters
from dailies.grade.cdl import Cdl


def test_neutral_cdl_needs_no_filters():
    assert cdl_filters(Cdl((1, 1, 1), (0, 0, 0), (1, 1, 1), 1.0, "000")) == ()


def test_slope_offset_power_is_one_lookup_per_channel_at_16_bit():
    (sop,) = cdl_filters(Cdl((1.1, 1.0, 0.9), (0.02, 0.0, -0.01), (1.05, 1.0, 0.95), 1.0, "x"))
    assert sop == ("lutrgb=r='clip(pow(max(val/65535*1.1+(0.02),0),1.05),0,1)*65535'"
                   ":g='clip(pow(max(val/65535*1.0+(0.0),0),1.0),0,1)*65535'"
                   ":b='clip(pow(max(val/65535*0.9+(-0.01),0),0.95),0,1)*65535'")


def test_saturation_is_a_rec709_luma_matrix_after_the_sop():
    filters = cdl_filters(Cdl((1.1, 1, 1), (0, 0, 0), (1, 1, 1), 0.5, "x"))
    assert len(filters) == 2 and filters[0].startswith("lutrgb=")
    assert filters[1] == ("colorchannelmixer=rr=0.6063:rg=0.3576:rb=0.0361:gr=0.1063:gg=0.8576:gb=0.0361"
                          ":br=0.1063:bg=0.3576:bb=0.5361")


def test_saturation_alone_needs_no_lookup():
    filters = cdl_filters(Cdl((1, 1, 1), (0, 0, 0), (1, 1, 1), 1.2, "x"))
    assert len(filters) == 1 and filters[0].startswith("colorchannelmixer=")


def test_each_matrix_row_sums_to_one_so_grey_stays_grey():
    (mix,) = cdl_filters(Cdl((1, 1, 1), (0, 0, 0), (1, 1, 1), 0.37, "x"))
    values = {k: float(v) for k, v in (pair.split("=") for pair in mix.split("=", 1)[1].split(":"))}
    for row in "rgb":
        assert sum(values[row + col] for col in "rgb") == pytest.approx(1.0, abs=1e-9)


def test_numbers_are_written_without_exponents_because_the_expression_parser_is_not_python():
    (sop,) = cdl_filters(Cdl((1.0, 1.0, 1.0), (0.00001, 0, 0), (1, 1, 1), 1.0, "x"))
    assert "e-" not in sop and "0.00001" in sop
