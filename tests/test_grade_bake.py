# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Baking CDL, camera look and show LUT into one cube. Results are checked against the ASC CDL formula
computed here in plain Python and against independent tetrahedral sampling of the source LUTs."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PyOpenColorIO")

from cubebuilder import identity, log_to_display, write_cube
from dailies.grade.bake import GradeError, Recipe, bake, recipe_hash
from dailies.grade.cdl import Cdl
from dailies.grade.cube import read_cube, sample_cube

SIZE = 9
LUMA = (0.2126, 0.7152, 0.0722)
GRADE = Cdl((1.10, 1.00, 0.92), (0.02, 0.00, -0.01), (1.05, 1.00, 0.95), 0.85, "14A")
POINTS = [(0.18, 0.18, 0.18), (0.5, 0.25, 0.75), (0.9, 0.6, 0.3), (0.05, 0.4, 0.95), (0.33, 0.66, 0.1)]


def asc_cdl(rgb, cdl: Cdl, clamp: bool = False):
    sop = []
    for value, slope, offset, power in zip(rgb, cdl.slope, cdl.offset, cdl.power):
        v = value * slope + offset
        if clamp:
            v = min(max(v, 0.0), 1.0)
        sop.append(v ** power if v >= 0 else v)
    luma = sum(w * v for w, v in zip(LUMA, sop))
    out = [luma + cdl.sat * (v - luma) for v in sop]
    return tuple(min(max(v, 0.0), 1.0) for v in out) if clamp else tuple(out)


OCIO_CONFIG = """ocio_profile_version: 2
roles:
  default: REDWideGamutRGB_Log3G10
  scene_linear: REDWideGamutRGB_Log3G10
displays:
  sRGB:
    - !<View> {name: Raw, colorspace: REDWideGamutRGB_Log3G10}
colorspaces:
  - !<ColorSpace>
    name: REDWideGamutRGB_Log3G10
  - !<ColorSpace>
    name: Rec709_BT1886
    from_scene_reference: !<ExponentTransform> {value: 2}
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def gain(rgb):
    return tuple(0.5 * v + 0.1 for v in rgb)


@pytest.fixture
def luts(tmp_path):
    return {"identity": write_cube(tmp_path / "identity.cube", SIZE, identity),
            "show": write_cube(tmp_path / "show.cube", 33, log_to_display),
            "look": write_cube(tmp_path / "look.cube", SIZE, gain),
            "ocio": _write(tmp_path / "show.ocio", OCIO_CONFIG), "cache": tmp_path / "cache"}


def recipe(luts, *, cdl=None, look=None, show="identity", size=SIZE, clamp=False, transform=None) -> Recipe:
    return Recipe(cdl=cdl, camera_look=luts[look] if look else None, show_lut=luts[show], output_transform=transform,
                  decode_space="REDWideGamutRGB_Log3G10", ocio_config=luts["ocio"], bake_size=size, cdl_clamp=clamp)


def baked(luts, **kwargs):
    luts["cache"].mkdir(exist_ok=True)
    return read_cube(bake(recipe(luts, **kwargs), luts["cache"]).path)


# ------------------------------------------------------------------ content


def test_nothing_but_an_identity_lut_bakes_an_identity_cube(luts):
    cube = baked(luts)
    assert cube.size == SIZE
    assert cube.at(3, 5, 8) == pytest.approx((3 / 8, 5 / 8, 1.0), abs=1e-6)


def test_cdl_matches_the_asc_formula(luts):
    cube = baked(luts, cdl=GRADE, size=17)
    for point in POINTS:
        assert sample_cube(cube, point) == pytest.approx(asc_cdl(point, GRADE), abs=2e-3)
    assert cube.at(8, 8, 8) == pytest.approx(asc_cdl((0.5, 0.5, 0.5), GRADE), abs=1e-5)  # exact on the lattice


def test_the_cdl_is_applied_before_the_show_lut_not_after(luts):
    cube = baked(luts, cdl=GRADE, show="show", size=33)
    show = read_cube(luts["show"])
    biggest_difference = 0.0
    for point in POINTS:
        right_order = sample_cube(show, asc_cdl(point, GRADE))
        wrong_order = asc_cdl(sample_cube(show, point), GRADE)
        assert sample_cube(cube, point) == pytest.approx(right_order, abs=3e-3)
        biggest_difference = max(biggest_difference, *(abs(a - b) for a, b in zip(right_order, wrong_order)))
    assert biggest_difference > 0.03  # the two orders really are different pictures, so the test can tell them apart


def test_camera_look_sits_between_the_cdl_and_the_show_lut(luts):
    cube = baked(luts, cdl=GRADE, look="look", show="show", size=33)
    show, look = read_cube(luts["show"]), read_cube(luts["look"])
    for point in POINTS:
        expected = sample_cube(show, sample_cube(look, asc_cdl(point, GRADE)))
        assert sample_cube(cube, point) == pytest.approx(expected, abs=3e-3)


def test_neutral_cdl_changes_nothing(luts):
    neutral = Cdl((1, 1, 1), (0, 0, 0), (1, 1, 1), 1.0, "000")
    with_neutral, without = baked(luts, cdl=neutral, show="show"), baked(luts, show="show")
    flat = lambda cube: [v for rgb in cube.values for v in rgb]
    assert flat(with_neutral) == pytest.approx(flat(without), abs=1e-6)


def test_clamp_setting_changes_the_result_when_the_grade_pushes_past_white(luts):
    hot = Cdl((2.0, 1.0, 1.0), (0, 0, 0), (1, 1, 1), 0.5, "hot")
    free, clamped = baked(luts, cdl=hot, size=17), baked(luts, cdl=hot, size=17, clamp=True)
    point = (0.75, 0.25, 0.25)  # red goes to 1.5 before saturation pulls the other channels towards it
    assert sample_cube(free, point) == pytest.approx(tuple(min(v, 1.0) for v in asc_cdl(point, hot)), abs=2e-3)
    assert sample_cube(clamped, point) == pytest.approx(asc_cdl(point, hot, clamp=True), abs=2e-3)
    assert abs(sample_cube(free, point)[1] - sample_cube(clamped, point)[1]) > 0.05


def test_cube_file_is_well_formed_and_says_where_it_came_from(luts):
    luts["cache"].mkdir()
    result = bake(recipe(luts, cdl=GRADE, show="show"), luts["cache"])
    text = result.path.read_text()
    assert f"LUT_3D_SIZE {SIZE}" in text
    assert text.count("\n") >= SIZE ** 3
    assert "# recipe sha256:" in text and "show.cube" in text and "slope 1.1 1.0 0.92" in text


# --------------------------------------------------------- output transform


def test_output_transform_is_applied_last_through_the_show_ocio_config(luts):
    cube = baked(luts, look="look", transform="Rec709_BT1886", size=17)
    for point in POINTS:
        expected = tuple(v ** 2 for v in gain(point))  # from_scene_reference: decode space to display squares the value
        assert sample_cube(cube, point) == pytest.approx(expected, abs=2e-3)


def test_unknown_output_transform_is_a_grade_error(luts):
    luts["cache"].mkdir()
    with pytest.raises(GradeError, match="Rec2020_PQ"):
        bake(recipe(luts, transform="Rec2020_PQ"), luts["cache"])


def test_unreadable_lut_is_a_grade_error(luts):
    luts["cache"].mkdir()
    luts["identity"].write_text("LUT_3D_SIZE 2\nnot numbers\n")
    with pytest.raises(GradeError, match="identity.cube"):
        bake(recipe(luts), luts["cache"])


# ----------------------------------------------------------- hash and cache


def test_cache_name_is_the_recipe_hash(luts):
    luts["cache"].mkdir()
    result = bake(recipe(luts, cdl=GRADE), luts["cache"])
    assert result.lut_hash == recipe_hash(recipe(luts, cdl=GRADE))
    assert result.lut_hash.startswith("sha256:")
    assert result.path == luts["cache"] / f"{result.lut_hash[7:23]}.cube"
    assert result.baked is True


def test_second_bake_of_the_same_recipe_reuses_the_cube(luts):
    luts["cache"].mkdir()
    first = bake(recipe(luts, cdl=GRADE), luts["cache"])
    stamp = os.stat(first.path).st_mtime_ns
    second = bake(recipe(luts, cdl=GRADE), luts["cache"])
    assert second.baked is False and second.path == first.path
    assert os.stat(first.path).st_mtime_ns == stamp
    assert [p.name for p in luts["cache"].iterdir()] == [first.path.name]  # and no temp file is left behind


def test_hash_follows_lut_content_not_its_path_or_date(luts, tmp_path):
    original = recipe_hash(recipe(luts, show="show"))
    moved = tmp_path / "elsewhere" / "renamed.cube"
    moved.parent.mkdir()
    moved.write_bytes(luts["show"].read_bytes())
    assert recipe_hash(Recipe(**{**recipe(luts, show="show").__dict__, "show_lut": moved})) == original
    luts["show"].write_text(luts["show"].read_text().replace("0.0000000000 0.0000000000 0.0000000000", "0.001 0 0", 1))
    assert recipe_hash(recipe(luts, show="show")) != original


@pytest.mark.parametrize("change", [
    {"cdl": Cdl((1.10, 1.00, 0.92), (0.02, 0.00, -0.01), (1.05, 1.00, 0.95), 0.86, "14A")},
    {"look": "look"}, {"size": 17}, {"clamp": True}, {"transform": "Rec709_BT1886"}, {"cdl": None},
])
def test_anything_that_changes_the_picture_changes_the_hash(luts, change):
    base = {"cdl": GRADE, "show": "show"}
    assert recipe_hash(recipe(luts, **base)) != recipe_hash(recipe(luts, **{**base, **change}))


def test_the_cdl_id_does_not_change_the_hash_because_it_does_not_change_the_picture(luts):
    renamed = Cdl(GRADE.slope, GRADE.offset, GRADE.power, GRADE.sat, "renamed")
    assert recipe_hash(recipe(luts, cdl=GRADE)) == recipe_hash(recipe(luts, cdl=renamed))


def test_failed_bake_leaves_nothing_in_the_cache(luts, monkeypatch):
    luts["cache"].mkdir()
    monkeypatch.setattr("dailies.grade.bake.os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        bake(recipe(luts, cdl=GRADE), luts["cache"])
    assert list(luts["cache"].iterdir()) == []


def test_a_lut_that_disappeared_is_a_grade_error(luts):
    luts["show"].unlink()
    with pytest.raises(GradeError, match="cannot read"):
        recipe_hash(recipe(luts, show="show"))
