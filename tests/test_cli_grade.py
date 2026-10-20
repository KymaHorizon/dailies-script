# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""dailies grade and dailies verify-colour from the command line."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from cubebuilder import log_to_display, identity, write_cube
from dailies.cli import main
from dailies.manifest import Manifest, manifest_path
from test_grade_stage import cdl_file, manifest_with


@pytest.fixture
def day(valid_config, write_config):
    write_cube(Path(valid_config["colour"]["show_lut"]), 9, log_to_display)
    write_cube(Path(valid_config["colour"]["camera_looks"]["A"]), 9, identity)
    path = manifest_path(Path(valid_config["paths"]["manifest_dir"]), "2026-10-12", "MU")
    manifest_with(A001_C001_1012AB={}, B001_C001_1012CD={"camera": "B", "scene": "20"}).save(path)
    cdl_file(Path(valid_config["colour"]["cdl_dir"]), "14A.cdl")
    return write_config(valid_config), path, valid_config


def grade(config_path, *extra: str) -> int:
    return main(["grade", "--config", str(config_path), "--day", "2026-10-12", *extra])


def test_grade_records_colour_and_reports_each_clip(day, capsys):
    config_path, path, _ = day
    assert grade(config_path) == 0
    clips = {c["id"]: c for c in Manifest.load(path).clips}
    assert clips["A001_C001_1012AB"]["colour"]["cdl"]["slope"][0] == 1.1
    assert clips["B001_C001_1012CD"]["flags"] == ["missing_cdl"]
    out = capsys.readouterr()
    assert "A001_C001_1012AB  found  CDL 14A.cdl" in out.out
    assert "1 found, 1 missing" in out.out
    assert "B001_C001_1012CD" in out.err


def test_grade_dry_run_saves_nothing(day, capsys):
    config_path, path, _ = day
    stamp = os.stat(path).st_mtime_ns
    assert grade(config_path, "--dry-run") == 0
    assert os.stat(path).st_mtime_ns == stamp and "Dry run" in capsys.readouterr().out


def test_grade_clip_filter_matching_nothing_exits_2(day, capsys):
    assert grade(day[0], "--clip", "Z*") == 2


def test_unreadable_show_lut_exits_2_and_saves_nothing(day, capsys, write_config, mutate):
    _, path, data = day
    Path(data["colour"]["show_lut"]).write_text("LUT_3D_SIZE 2\nnot numbers\n")
    stamp = os.stat(path).st_mtime_ns
    assert grade(write_config(mutate(data, "colour.apply", "baked"))) == 2
    assert os.stat(path).st_mtime_ns == stamp
    assert "show_rec709_v3.cube" in capsys.readouterr().err


# ------------------------------------------------------------ verify-colour


def fake_ffmpeg(path: Path, psnr: str, code: int = 0) -> None:
    path.write_text(f"#!{sys.executable}\nimport sys\n"
                    f"sys.stderr.write('[Parsed_psnr_4 @ 0x1] PSNR r:{psnr} g:{psnr} b:{psnr} average:{psnr} min:{psnr} max:{psnr}\\n')\n"
                    f"sys.exit({code})\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def verify(config_path, tmp_path, *extra: str) -> int:
    source, reference = tmp_path / "source.tif", tmp_path / "reference.tif"
    source.write_bytes(b"x")
    reference.write_bytes(b"x")
    return main(["verify-colour", "--config", str(config_path), "--day", "2026-10-12", "--clip", "A001_C001_1012AB",
                 "--source", str(source), "--reference", str(reference), *extra])


def test_verify_passes_when_the_frames_agree(day, tmp_path, capsys):
    config_path, _, data = day
    grade(config_path)
    fake_ffmpeg(Path(data["tools"]["ffmpeg"]["path"]), "62.40")
    assert verify(config_path, tmp_path) == 0
    assert "62.40 dB" in capsys.readouterr().out


def test_verify_fails_below_the_threshold(day, tmp_path, capsys):
    config_path, _, data = day
    grade(config_path)
    fake_ffmpeg(Path(data["tools"]["ffmpeg"]["path"]), "38.10")
    assert verify(config_path, tmp_path) == 1
    assert "38.10 dB" in capsys.readouterr().out
    assert verify(config_path, tmp_path, "--min-psnr", "35") == 0


def test_verify_needs_a_graded_clip(day, tmp_path, capsys):
    assert verify(day[0], tmp_path) == 2
    assert "grade" in capsys.readouterr().err


def test_verify_reports_an_ffmpeg_failure(day, tmp_path, capsys):
    config_path, _, data = day
    grade(config_path)
    Path(data["tools"]["ffmpeg"]["path"]).write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('sizes differ')\nsys.exit(1)\n")
    assert verify(config_path, tmp_path) == 2
    assert "sizes differ" in capsys.readouterr().err


def test_verify_needs_both_frames_to_exist(day, tmp_path, capsys):
    config_path = day[0]
    grade(config_path)
    assert main(["verify-colour", "--config", str(config_path), "--day", "2026-10-12", "--clip", "A001_C001_1012AB",
                 "--source", str(tmp_path / "nope.tif"), "--reference", str(tmp_path / "nope2.tif")]) == 2
    assert "nope.tif" in capsys.readouterr().err


def test_verify_of_a_clip_that_is_not_in_the_manifest(day, tmp_path, capsys):
    source = tmp_path / "s.tif"
    source.write_bytes(b"x")
    assert main(["verify-colour", "--config", str(day[0]), "--day", "2026-10-12", "--clip", "Z999_C001_0101ZZ",
                 "--source", str(source), "--reference", str(source)]) == 2
    assert "no clip" in capsys.readouterr().err


def test_verify_without_a_psnr_line_is_never_a_pass(day, tmp_path, capsys):
    config_path, _, data = day
    grade(config_path)
    Path(data["tools"]["ffmpeg"]["path"]).write_text(f"#!{sys.executable}\nprint('nothing useful')\n")
    assert verify(config_path, tmp_path) == 2
    assert "no PSNR summary" in capsys.readouterr().err
