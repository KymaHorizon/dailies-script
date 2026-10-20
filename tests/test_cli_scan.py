# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""dailies scan from the command line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("ascmhl")

from dailies.cli import main
from dailies.manifest import Manifest, manifest_path
from stubtools import install_stub_tools, r3d, seal


@pytest.fixture
def show(valid_config, write_config, mutate, show_tree):
    install_stub_tools(show_tree / "bin")
    data = mutate(valid_config, "decode.engine", "sdk")
    data = mutate(data, "decode.transport", "pipe")
    data = mutate(data, "tools.r3dtool", {"path": str(show_tree / "bin/r3dtool"), "version": "0.1.0"})
    camera = Path(data["paths"]["camera_root"]) / "TST_20261012_SD014_MU" / "01_CAMERA"
    camera.mkdir(parents=True)
    r3d(camera / "A001_1012AB", "A001_C001_1012AB")
    seal(camera / "A001_1012AB")
    return write_config(data), data, camera


def scan(config_path, *extra: str) -> int:
    return main(["scan", "--config", str(config_path), "--day", "2026-10-12", *extra])


def saved(data) -> Manifest:
    return Manifest.load(manifest_path(Path(data["paths"]["manifest_dir"]), "2026-10-12", "MU"))


def test_first_scan_creates_the_day_manifest(show, capsys):
    config_path, data, _ = show
    assert scan(config_path, "--day-code", "SD014") == 0
    manifest = saved(data)
    assert [c["id"] for c in manifest.clips] == ["A001_C001_1012AB"]
    header = manifest.to_dict()
    assert (header["day_code"], header["unit"], header["show"]) == ("SD014", "MU", "TST")
    assert header["config_hash"].startswith("sha256:")
    assert "1 clip scanned" in capsys.readouterr().out


def test_a_new_day_needs_its_day_code(show, capsys):
    assert scan(show[0]) == 2
    assert "--day-code" in capsys.readouterr().err


@pytest.mark.parametrize("code", ["SD 14", "sd014/x", ""])
def test_day_code_must_be_safe_for_folder_names(show, code, capsys):
    assert scan(show[0], "--day-code", code) == 2


def test_a_later_scan_reuses_the_manifest_and_its_mob_ids(show):
    config_path, data, camera = show
    scan(config_path, "--day-code", "SD014")
    before = saved(data).clips[0]["mob_id"]
    r3d(camera / "B001_1012CD", "B001_C001_1012CD")
    seal(camera / "B001_1012CD")
    assert scan(config_path) == 0
    clips = {c["id"]: c for c in saved(data).clips}
    assert set(clips) == {"A001_C001_1012AB", "B001_C001_1012CD"}
    assert clips["A001_C001_1012AB"]["mob_id"] == before


def test_a_conflicting_day_code_is_refused(show, capsys):
    config_path, _, _ = show
    scan(config_path, "--day-code", "SD014")
    assert scan(config_path, "--day-code", "SD015") == 2
    assert "SD014" in capsys.readouterr().err


def test_unreadable_clip_gives_exit_1_and_the_manifest_is_still_saved(show, capsys):
    config_path, data, camera = show
    r3d(camera / "B001_1012CD", "B001_C001_BROKEN")
    seal(camera / "B001_1012CD")
    assert scan(config_path, "--day-code", "SD014") == 1
    assert len(saved(data).clips) == 1
    assert "B001_C001_BROKEN" in capsys.readouterr().err


def test_skipped_folder_is_reported_but_is_not_a_failure(show, capsys):
    config_path, _, camera = show
    r3d(camera / "C001_1012EF", "C001_C001_1012EF")
    assert scan(config_path, "--day-code", "SD014") == 0
    assert "skipped C001_1012EF" in capsys.readouterr().err


def test_wrong_tool_version_exits_2_and_saves_nothing(show, write_config, mutate, capsys):
    _, data, _ = show
    config_path = write_config(mutate(data, "tools.ffmpeg.version", "6.0"))
    assert scan(config_path, "--day-code", "SD014") == 2
    assert "tools.ffmpeg" in capsys.readouterr().err
    assert not manifest_path(Path(data["paths"]["manifest_dir"]), "2026-10-12", "MU").exists()


def test_every_command_is_logged_verbatim(show):
    config_path, data, _ = show
    scan(config_path, "--day-code", "SD014")
    log = Path(data["paths"]["log_dir"]) / "2026-10-12_MU.commands.jsonl"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(e["argv"][0][1] == "meta" and e["clip"] == "A001_C001_1012AB" for e in entries)
    assert all(e["dry_run"] is False and e["stage"] == "scan" for e in entries)


def test_dry_run_prints_commands_and_writes_nothing(show, capsys):
    config_path, data, _ = show
    assert scan(config_path, "--day-code", "SD014", "--dry-run") == 0
    assert " meta " in capsys.readouterr().out
    assert list(Path(data["paths"]["manifest_dir"]).iterdir()) == []
    assert list(Path(data["paths"]["log_dir"]).iterdir()) == []


def test_a_damaged_manifest_is_never_overwritten(show, capsys):
    config_path, data, _ = show
    path = manifest_path(Path(data["paths"]["manifest_dir"]), "2026-10-12", "MU")
    path.write_text('{"schema_version": 1, "clips": [')
    assert scan(config_path, "--day-code", "SD014") == 2
    assert "not valid JSON" in capsys.readouterr().err
    assert path.read_text() == '{"schema_version": 1, "clips": ['
