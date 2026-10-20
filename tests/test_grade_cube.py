# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Reading and sampling .cube files."""

from __future__ import annotations

import pytest

from cubebuilder import identity, log_to_display, write_cube
from dailies.grade.cube import CubeError, read_cube, sample_cube


def test_read_cube_size_and_order(tmp_path):
    cube = read_cube(write_cube(tmp_path / "id.cube", 3, identity))
    assert cube.size == 3 and len(cube.values) == 27
    assert cube.values[1] == pytest.approx((0.5, 0.0, 0.0))  # red changes fastest
    assert cube.values[3] == pytest.approx((0.0, 0.5, 0.0))
    assert cube.values[9] == pytest.approx((0.0, 0.0, 0.5))


def test_comments_titles_and_domain_lines_are_skipped(tmp_path):
    path = write_cube(tmp_path / "id.cube", 2, identity)
    path.write_text("# made by a test\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n" + path.read_text())
    assert read_cube(path).size == 2


def test_lattice_points_are_returned_exactly(tmp_path):
    cube = read_cube(write_cube(tmp_path / "show.cube", 5, log_to_display))
    assert sample_cube(cube, (0.5, 0.25, 1.0)) == pytest.approx(log_to_display((0.5, 0.25, 1.0)), abs=1e-9)


def test_identity_is_identity_between_lattice_points(tmp_path):
    cube = read_cube(write_cube(tmp_path / "id.cube", 5, identity))
    assert sample_cube(cube, (0.13, 0.77, 0.42)) == pytest.approx((0.13, 0.77, 0.42), abs=1e-9)


def test_input_outside_the_domain_is_clamped(tmp_path):
    cube = read_cube(write_cube(tmp_path / "id.cube", 5, identity))
    assert sample_cube(cube, (1.4, -0.2, 0.5)) == pytest.approx((1.0, 0.0, 0.5))


@pytest.mark.parametrize("text, problem", [
    ("0 0 0\n1 1 1\n", "LUT_3D_SIZE"),
    ("LUT_3D_SIZE 2\n0 0 0\n1 1 1\n", "expected 8"),
    ("LUT_3D_SIZE 2\n" + "0 0\n" * 8, "three numbers"),
    ("LUT_1D_SIZE 4\n0 0 0\n", "1D"),
    ("LUT_3D_SIZE 2\n" + "0 0 zero\n" * 8, "three numbers"),
])
def test_unusable_cube_files(tmp_path, text, problem):
    path = tmp_path / "bad.cube"
    path.write_text(text)
    with pytest.raises(CubeError, match=problem):
        read_cube(path)


def test_missing_cube_file(tmp_path):
    with pytest.raises(CubeError, match="cannot read"):
        read_cube(tmp_path / "absent.cube")
