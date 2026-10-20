# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""dailies sync from the command line."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dailies.cli import main
from dailies.manifest import Manifest, manifest_path
from test_sync_stage import manifest_with, sound_file


@pytest.fixture
def day(valid_config, write_config):
    path = manifest_path(Path(valid_config["paths"]["manifest_dir"]), "2026-10-12", "MU")
    manifest_with({"A001_C001_1012AB": {}, "B001_C001_1012CD": {"camera": "B", "scene": "20", "take": "1", "start_tc": "11:00:00:00",
                                        "end_tc": "11:00:30:00"}},
                  [sound_file("14A-03.wav", "09:59:50:00", 120)]).save(path)
    return write_config(valid_config), path


def sync(config_path, *extra: str) -> int:
    return main(["sync", "--config", str(config_path), "--day", "2026-10-12", *extra])


def test_sync_writes_the_sound_blocks_and_reports_each_clip(day, capsys):
    config_path, path = day
    assert sync(config_path) == 0
    clips = {c["id"]: c for c in Manifest.load(path).clips}
    assert clips["A001_C001_1012AB"]["sound"]["status"] == "matched"
    assert clips["B001_C001_1012CD"]["sound"]["status"] == "no_sound"
    out = capsys.readouterr()
    assert "A001_C001_1012AB  matched  offset 480000 samples, 14A-03.wav" in out.out
    assert "1 matched, 1 no_sound" in out.out
    assert "B001_C001_1012CD: no_sound" in out.err


def test_dry_run_reports_but_saves_nothing(day, capsys):
    config_path, path = day
    stamp = os.stat(path).st_mtime_ns
    assert sync(config_path, "--dry-run") == 0
    assert "matched" in capsys.readouterr().out
    assert os.stat(path).st_mtime_ns == stamp
    assert Manifest.load(path).clips[0]["sound"] is None


def test_clip_filter(day):
    config_path, path = day
    assert sync(config_path, "--clip", "B001_*") == 0
    clips = {c["id"]: c for c in Manifest.load(path).clips}
    assert clips["A001_C001_1012AB"]["sound"] is None and clips["B001_C001_1012CD"]["sound"] is not None


def test_clip_filter_matching_nothing_exits_2(day, capsys):
    assert sync(day[0], "--clip", "Z*") == 2
    assert "no clips match" in capsys.readouterr().err


def test_missing_manifest_exits_2(valid_config, write_config, capsys):
    assert sync(write_config(valid_config)) == 2
    assert "manifest" in capsys.readouterr().err


def test_excluded_sound_files_are_reported(valid_config, write_config, capsys):
    path = manifest_path(Path(valid_config["paths"]["manifest_dir"]), "2026-10-12", "MU")
    manifest_with({"A001_C001_1012AB": {}}, [sound_file("notc.wav", "09:59:50:00", 60, start=None, flags=["no_time_reference"])]).save(path)
    assert sync(write_config(valid_config)) == 0
    assert "notc.wav: no_time_reference" in capsys.readouterr().err
