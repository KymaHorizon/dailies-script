# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Framing maths: decode size, fixed crop, scale into the deliverable raster."""

import pytest

from dailies.framing import Framing, FramingError, compute_framing, decode_size


def test_quarter_decode_of_8k_17x9():
    assert decode_size(8192, 4320, "quarter") == (2048, 1080)


def test_decode_size_rounds_down_to_even():
    # 6142 / 4 = 1535.5, which must become 1534 and never 1536
    assert decode_size(6142, 3238, "quarter") == (1534, 808)


def test_decode_size_rejects_unknown_resolution():
    with pytest.raises(FramingError, match="unknown decode resolution"):
        decode_size(8192, 4320, "third")


def test_spec_crop_numbers_for_five_percent():
    f = compute_framing((2048, 1080), crop_percent=5.0, output=(1920, 1080), fit="width")
    assert f.crop == (1944, 1026)
    assert f.crop_offset == (52, 27)


def test_fit_width_letterboxes_17x9_into_16x9():
    f = compute_framing((2048, 1080), crop_percent=5.0, output=(1920, 1080), fit="width")
    assert f.scale == (1920, 1012)
    assert f.pad_offset == (0, 34)
    assert f.output == (1920, 1080)


def test_fit_width_never_distorts_beyond_tolerance():
    f = compute_framing((2048, 1080), crop_percent=5.0, output=(1920, 1080), fit="width")
    crop_aspect = f.crop[0] / f.crop[1]
    scale_aspect = f.scale[0] / f.scale[1]
    assert abs(scale_aspect / crop_aspect - 1) < 0.005


def test_fit_height_trims_sides_to_fill_the_raster():
    f = compute_framing((2048, 1080), crop_percent=5.0, output=(1920, 1080), fit="height")
    assert f.crop == (1824, 1026)
    assert f.crop_offset == (112, 27)
    assert f.scale == (1920, 1080)
    assert f.pad_offset == (0, 0)


def test_16x9_source_needs_no_padding():
    f = compute_framing((1920, 1080), crop_percent=5.0, output=(1920, 1080), fit="width")
    assert f.crop == (1824, 1026)
    assert f.scale == (1920, 1080)
    assert f.pad_offset == (0, 0)


def test_fit_exact_fails_the_clip_instead_of_squeezing():
    with pytest.raises(FramingError, match="aspect"):
        compute_framing((2048, 1080), crop_percent=5.0, output=(1920, 1080), fit="exact")


def test_fit_exact_accepts_matching_aspect():
    f = compute_framing((1920, 1080), crop_percent=5.0, output=(1920, 1080), fit="exact")
    assert f.scale == (1920, 1080)


def test_fit_width_fails_when_source_is_taller_than_the_raster():
    # a 4:3 source cannot fit a 16:9 raster by width without overflowing height
    with pytest.raises(FramingError, match="taller"):
        compute_framing((1440, 1080), crop_percent=5.0, output=(1920, 1080), fit="width")


def test_zero_crop_keeps_the_full_decode():
    f = compute_framing((2048, 1080), crop_percent=0.0, output=(1920, 1080), fit="width")
    assert f.crop == (2048, 1080)
    assert f.crop_offset == (0, 0)


@pytest.mark.parametrize("percent", [-1.0, 100.0, 150.0])
def test_crop_percent_out_of_range_is_rejected(percent):
    with pytest.raises(FramingError, match="crop_percent"):
        compute_framing((2048, 1080), crop_percent=percent, output=(1920, 1080), fit="width")


def test_odd_output_raster_is_rejected():
    with pytest.raises(FramingError, match="even"):
        compute_framing((2048, 1080), crop_percent=5.0, output=(1921, 1080), fit="width")


def test_unknown_fit_mode_is_rejected():
    with pytest.raises(FramingError, match="fit"):
        compute_framing((2048, 1080), crop_percent=5.0, output=(1920, 1080), fit="stretch")


def test_crop_never_exceeds_the_decode():
    f = compute_framing((2050, 1082), crop_percent=0.01, output=(1920, 1080), fit="width")
    assert f.crop[0] <= 2050 and f.crop[1] <= 1082
    assert f.crop[0] % 2 == 0 and f.crop[1] % 2 == 0


def test_anamorphic_desqueeze_widens_before_the_crop():
    f = compute_framing((2048, 1716), crop_percent=5.0, output=(1920, 1080), fit="width", anamorphic=2.0)
    assert f.desqueezed == (4096, 1716)
    assert f.crop == (3890, 1630)
    assert f.scale == (1920, 804)


def test_spherical_clip_reports_no_desqueeze():
    f = compute_framing((2048, 1080), crop_percent=5.0, output=(1920, 1080), fit="width")
    assert f.desqueezed is None


def test_filter_string_for_letterboxed_output():
    f = compute_framing((2048, 1080), crop_percent=5.0, output=(1920, 1080), fit="width")
    assert f.filters() == (
        "crop=1944:1026:52:27",
        "scale=1920:1012:flags=lanczos",
        "pad=1920:1080:0:34:color=black",
    )


def test_filter_string_omits_pad_when_not_needed():
    f = compute_framing((1920, 1080), crop_percent=5.0, output=(1920, 1080), fit="width")
    assert f.filters() == ("crop=1824:1026:48:27", "scale=1920:1080:flags=lanczos")


def test_filter_string_desqueezes_before_the_crop():
    f = compute_framing((2048, 1716), crop_percent=5.0, output=(1920, 1080), fit="width", anamorphic=2.0)
    assert f.filters()[0] == "scale=4096:1716:flags=lanczos"
    assert f.filters()[1].startswith("crop=3890:1630:")


def test_manifest_block_round_trip():
    f = compute_framing((2048, 1080), crop_percent=5.0, output=(1920, 1080), fit="width")
    block = f.to_dict()
    assert block == {
        "decode": [2048, 1080],
        "crop": [1944, 1026],
        "crop_offset": [52, 27],
        "scale": [1920, 1012],
        "pad_offset": [0, 34],
        "output": [1920, 1080],
        "fit": "width",
    }
    assert Framing.from_dict(block) == f


def test_final_scale_can_carry_colour_conversion_options():
    f = compute_framing((2048, 1716), crop_percent=5.0, output=(1920, 1080), fit="width", anamorphic=2.0)
    chain = f.filters(final_scale_options=("out_color_matrix=bt709", "out_range=tv"))
    assert chain[0] == "scale=4096:1716:flags=lanczos"  # the desqueeze is left alone
    assert chain[2] == "scale=1920:804:flags=lanczos:out_color_matrix=bt709:out_range=tv"


def test_fit_height_cannot_rescue_a_source_taller_than_the_raster():
    # 4:3 into 16:9: trimming the sides cannot help, and squeezing the height is refused
    with pytest.raises(FramingError, match="aspect mismatch"):
        compute_framing((1440, 1080), crop_percent=5.0, output=(1920, 1080), fit="height")
