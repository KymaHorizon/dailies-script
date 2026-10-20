# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Scan stage end to end on a temp tree. Real subprocesses through the real executor: stub r3dtool
and ffprobe scripts stand in for the SDK and FFmpeg, rolls are sealed with the real ascmhl tool."""

from __future__ import annotations

import io
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("ascmhl")

from dailies.commands.base import CommandLog, DryRunExecutor, SubprocessExecutor
from dailies.config import load_config
from dailies.manifest import Manifest
from dailies.naming import DayContext
from dailies.scan.stage import ScanAbort, run_scan
from stubtools import install_stub_tools, r3d, seal
from wavbuilder import bext_chunk, data_chunk, fmt_chunk, ixml_chunk, riff

DAY = DayContext(show="TST", day="2026-10-12", day_code="SD014", unit="MU")

@pytest.fixture
def env(valid_config, write_config, mutate, show_tree):
    """A config on the sdk engine with stub tools, plus the day's camera and sound folders."""
    install_stub_tools(show_tree / "bin")
    data = mutate(valid_config, "decode.engine", "sdk")
    data = mutate(data, "decode.transport", "pipe")
    data = mutate(data, "decode.camera_letter_from", "camera_id")
    data = mutate(data, "tools.r3dtool", {"path": str(show_tree / "bin/r3dtool"), "version": "0.1.0 / R3DAPI 9.2.1"})
    config = load_config(write_config(data))
    camera = config.paths.camera_root / "TST_20261012_SD014_MU" / "01_CAMERA"
    sound = config.paths.sound_root / "TST_20261012_SD014_MU" / "02_SOUND"
    camera.mkdir(parents=True)
    sound.mkdir(parents=True)
    return config, camera, sound, data


def fresh() -> Manifest:
    return Manifest.new(day="2026-10-12", day_code="SD014", unit="MU", show="TST", config_hash="sha256:x",
                        tools={}, created="2026-10-12T19:00:00Z")


def scan(config, manifest=None, executor=None):
    manifest = manifest or fresh()
    executor = executor or SubprocessExecutor(CommandLog(io.StringIO()))
    return manifest, run_scan(config, manifest, DAY, executor)


# -------------------------------------------------------------- happy path


def test_sealed_rolls_are_scanned_into_the_manifest(env):
    config, camera, sound, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB")
    r3d(camera / "A001_1012AB", "A001_C002_1012AB", parts=3)
    r3d(camera / "B001_1012CD", "B001_C001_1012CD")
    seal(camera / "A001_1012AB")
    seal(camera / "B001_1012CD")
    manifest, summary = scan(config)
    assert [c["id"] for c in manifest.clips] == ["A001_C001_1012AB", "A001_C002_1012AB", "B001_C001_1012CD"]
    assert (summary.clips_scanned, summary.clips_failed, summary.folders_skipped) == (3, 0, 0)


def test_spanned_clip_is_one_entry_addressed_by_its_first_file(env):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C002_1012AB", parts=3)
    seal(camera / "A001_1012AB")
    manifest, _ = scan(config)
    (clip,) = manifest.clips
    assert clip["source"]["path"].endswith("A001_C002_1012AB_001.R3D")
    assert clip["source"]["size_bytes"] == 100 + 200 + 300
    assert clip["source"]["mhl_verified"] is True


def test_picture_block_comes_from_the_decoder(env):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB", facts={"metadata": {"user_scene": "14A", "user_take": "3"}})
    seal(camera / "A001_1012AB")
    picture = scan(config)[0].clips[0]["picture"]
    assert (picture["camera"], picture["roll"], picture["clip"], picture["reel"]) == ("A", "001", "C001", "A001")
    assert (picture["width"], picture["height"], picture["fps"], picture["frames"]) == (8192, 4320, 50.0, 1732)
    assert (picture["scene"], picture["take"]) == ("14A", "3")
    assert picture["tc_edge"] == "01:00:00:00"


def test_end_timecode_is_exclusive_as_in_the_spec_and_in_an_ale(env):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB")
    seal(camera / "A001_1012AB")
    picture = scan(config)[0].clips[0]["picture"]
    assert (picture["start_tc"], picture["end_tc"]) == ("10:14:22:31", "10:14:57:13")  # 1732 frames later


def test_tool_versions_are_checked_and_recorded(env):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB")
    seal(camera / "A001_1012AB")
    tools = scan(config)[0].tools
    assert tools["ffmpeg"] == {"path": str(config.tools.ffmpeg.path), "version": "7.1.1"}
    assert tools["r3dtool"]["version"].startswith("0.1.0 / R3DAPI 9.2.1-test")


def test_rescan_keeps_the_mob_id_and_adds_nothing(env):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB")
    seal(camera / "A001_1012AB")
    manifest, _ = scan(config)
    before = manifest.clips[0]["mob_id"]
    scan(config, manifest)
    assert [c["mob_id"] for c in manifest.clips] == [before]


# ---------------------------------------------------------------- the gate


def test_unsealed_roll_is_skipped_with_a_reason_and_the_rest_continues(env):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB")
    r3d(camera / "B001_1012CD", "B001_C001_1012CD")
    seal(camera / "A001_1012AB")
    manifest, summary = scan(config)
    assert [c["id"] for c in manifest.clips] == ["A001_C001_1012AB"]
    assert summary.folders_skipped == 1
    skipped = [f for f in manifest.to_dict()["scan"]["folders"] if not f["eligible"]]
    assert skipped[0]["path"].endswith("B001_1012CD") and "no ascmhl folder" in skipped[0]["reason"]


def test_a_whole_day_sealed_as_one_history_is_accepted(env):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB")
    r3d(camera / "B001_1012CD", "B001_C001_1012CD")
    seal(camera)
    manifest, summary = scan(config)
    assert len(manifest.clips) == 2 and summary.folders_skipped == 0


def test_missing_day_folder_stops_the_run(env):
    config, camera, _, _ = env
    camera.rmdir()
    with pytest.raises(ScanAbort, match="01_CAMERA"):
        scan(config)


# ------------------------------------------------------------ media errors


def test_a_clip_the_decoder_cannot_read_is_reported_and_the_batch_continues(env):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB")
    r3d(camera / "A001_1012AB", "A001_C002_BROKEN")
    seal(camera / "A001_1012AB")
    manifest, summary = scan(config)
    assert [c["id"] for c in manifest.clips] == ["A001_C001_1012AB"]
    assert summary.clips_failed == 1
    (error,) = manifest.to_dict()["scan"]["errors"]
    assert error["clip"] == "A001_C002_BROKEN" and "failed to load clip" in error["stderr"]


def test_a_span_with_a_missing_part_is_not_scanned(env):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB", parts=3)
    (camera / "A001_1012AB/A001_C001_1012AB.RDC/A001_C001_1012AB_002.R3D").unlink()
    seal(camera / "A001_1012AB")
    manifest, summary = scan(config)
    assert manifest.clips == [] and summary.clips_failed == 1
    assert "span part 002 is missing" in manifest.to_dict()["scan"]["errors"][0]["message"]


def test_two_cameras_with_the_same_reel_name_stop_the_run(env):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB")
    r3d(camera / "A001_DUPLICATE", "A001_C001_1012AB")
    seal(camera / "A001_1012AB")
    seal(camera / "A001_DUPLICATE")
    with pytest.raises(ScanAbort, match="collision"):
        scan(config)


# -------------------------------------------------------------------- flags


@pytest.mark.parametrize("facts, flag", [
    ({"metadata": {"framerate_numerator": 25}, "tc_default": ["10:14:22:11", "x"]}, "wrong_fps"),
    ({"width": 6144, "height": 2592}, "unexpected_resolution"),
    ({"metadata": {"user_timecode_preference": 0}}, "edge_timecode"),
    ({"frames": 20}, "short_clip"),
    ({"metadata": {"dropped_frame_count": 2}}, "dropped_frames"),
    ({"metadata": {"pixel_aspect_ratio": 2}}, "anamorphic"),
    ({"metadata": {"camera_id": "Z"}}, "unknown_camera"),
])
def test_anything_unexpected_is_flagged_loudly_and_never_adapted_to(env, facts, flag):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB", facts=facts)
    seal(camera / "A001_1012AB")
    manifest, summary = scan(config)
    assert flag in manifest.clips[0]["flags"]
    assert any(flag in warning for warning in summary.warnings)


def test_a_normal_clip_carries_no_flags(env):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB")
    seal(camera / "A001_1012AB")
    assert scan(config)[0].clips[0]["flags"] == []


def test_camera_letter_can_come_from_the_reel_prefix(env, write_config, mutate):
    _, camera, _, data = env
    config = load_config(write_config(mutate(data, "decode.camera_letter_from", "reel_prefix")))
    r3d(camera / "B001_1012CD", "B001_C001_1012CD", facts={"metadata": {"camera_id": "A"}})
    seal(camera / "B001_1012CD")
    assert scan(config)[0].clips[0]["picture"]["camera"] == "B"


# -------------------------------------------------------------------- sound


def wav(folder: Path, name: str, **ixml) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(riff(bext_chunk("", 1769280123), ixml_chunk(**ixml), fmt_chunk(), data_chunk()))


def test_sound_inventory_is_recorded_from_sealed_sound_rolls(env):
    config, camera, sound, _ = env
    wav(sound / "SD014", "14A-03.wav", scene="14A", take="3", tape="SD014")
    wav(sound / "SD014", "14A-04.WAV", scene="14A", take="4", tape="SD014")
    seal(sound / "SD014")
    manifest, summary = scan(config)
    assert summary.sound_files == 2
    first = manifest.sound_files[0]
    assert (first["scene"], first["take"], first["soundroll"], first["mhl_verified"]) == ("14A", "3", "SD014", True)
    assert first["start_samples"] == 1769280123 and first["end_samples"] == 1769280123 + 4800


def test_unsealed_sound_roll_is_skipped(env):
    config, _, sound, _ = env
    wav(sound / "SD014", "14A-03.wav")
    manifest, summary = scan(config)
    assert manifest.sound_files == [] and summary.folders_skipped == 1


def test_unreadable_sound_file_is_reported_and_the_rest_continue(env):
    config, _, sound, _ = env
    wav(sound / "SD014", "14A-03.wav")
    wav(sound / "SD014", "BROKEN.wav")
    seal(sound / "SD014")
    manifest, summary = scan(config)
    assert len(manifest.sound_files) == 1 and summary.clips_failed == 1
    assert manifest.to_dict()["scan"]["errors"][0]["clip"] == "BROKEN.wav"


def test_missing_sound_folder_is_a_warning_because_a_day_can_be_all_mos(env):
    config, camera, sound, _ = env
    sound.rmdir()
    r3d(camera / "A001_1012AB", "A001_C001_1012AB")
    seal(camera / "A001_1012AB")
    manifest, summary = scan(config)
    assert len(manifest.clips) == 1
    assert any("02_SOUND" in warning for warning in summary.warnings)


# ------------------------------------------------------------ tools and dry run


def test_a_tool_that_is_not_the_pinned_version_stops_the_run(env, write_config, mutate):
    _, camera, _, data = env
    config = load_config(write_config(mutate(data, "tools.ffmpeg.version", "6.0")))
    with pytest.raises(ScanAbort, match=r"tools.ffmpeg.*6\.0.*7\.1\.1"):
        scan(config)


def test_dry_run_prints_the_commands_and_changes_nothing(env):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB")
    seal(camera / "A001_1012AB")
    out = io.StringIO()
    manifest, summary = scan(config, executor=DryRunExecutor(out, CommandLog(io.StringIO())))
    assert manifest.clips == [] and manifest.tools == {}
    assert f"{config.tools.r3dtool.path} meta " in out.getvalue()
    assert "A001_C001_1012AB_001.R3D" in out.getvalue()
    assert summary.clips_scanned == 0


# ------------------------------------------------------- remaining branches

from stubtools import script


def test_a_tool_that_cannot_report_its_version_stops_the_run(env, show_tree):
    config, _, _, _ = env
    script(show_tree / "bin/ffmpeg", "#!{python}\nimport sys\nsys.stderr.write('dyld: library not loaded')\nsys.exit(1)\n")
    with pytest.raises(ScanAbort, match="tools.ffmpeg.*failed to report its version.*library not loaded"):
        scan(config)


def test_unreadable_version_output_stops_the_run(env, show_tree):
    config, _, _, _ = env
    script(show_tree / "bin/ffprobe", "#!{python}\nprint('something else entirely')\n")
    with pytest.raises(ScanAbort, match="tools.ffprobe: unreadable version output"):
        scan(config)


def test_unreadable_decoder_version_stops_the_run(env, show_tree):
    config, _, _, _ = env
    script(show_tree / "bin/r3dtool", "#!{python}\nprint('not json')\n")
    with pytest.raises(ScanAbort, match="tools.r3dtool: unreadable version output"):
        scan(config)


def test_decoder_output_that_cannot_be_used_fails_that_clip_only(env):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB", facts={"metadata": {"iso": "not a number"}})
    r3d(camera / "A001_1012AB", "A001_C002_1012AB")
    seal(camera / "A001_1012AB")
    manifest, summary = scan(config)
    assert [c["id"] for c in manifest.clips] == ["A001_C002_1012AB"] and summary.clips_failed == 1


def test_timecode_that_is_impossible_at_the_clips_rate_fails_that_clip(env):
    config, camera, _, _ = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB", facts={"tc_default": ["10:14:22:77", "x"]})
    seal(camera / "A001_1012AB")
    manifest, summary = scan(config)
    assert manifest.clips == [] and "frame field" in manifest.to_dict()["scan"]["errors"][0]["message"]


def test_dry_run_prints_the_ffprobe_commands_too(env):
    config, _, sound, _ = env
    wav(sound / "SD014", "14A-03.wav")
    seal(sound / "SD014")
    out = io.StringIO()
    manifest, _ = scan(config, executor=DryRunExecutor(out, CommandLog(io.StringIO())))
    assert "-show_format -show_streams -print_format json" in out.getvalue()
    assert manifest.sound_files == []


def test_sound_file_flags_reach_the_operator(env, show_tree, write_config, mutate):
    _, _, sound, data = env
    config = load_config(write_config(mutate(data, "show.audio_rate", 96000)))
    wav(sound / "SD014", "14A-03.wav")
    seal(sound / "SD014")
    manifest, summary = scan(config)
    assert manifest.sound_files[0]["flags"] == ["wrong_sample_rate"]
    assert "14A-03.wav: wrong_sample_rate" in summary.warnings


def test_a_rescan_clears_flags_that_no_longer_apply(env, write_config, mutate):
    config, camera, _, data = env
    r3d(camera / "A001_1012AB", "A001_C001_1012AB", facts={"width": 6144, "height": 2592})
    seal(camera / "A001_1012AB")
    manifest, _ = scan(config)
    assert manifest.clips[0]["flags"] == ["unexpected_resolution"]
    manifest.add_flag("A001_C001_1012AB", "missing_cdl")  # another stage's flag must survive
    corrected = load_config(write_config(mutate(data, "decode.expected_resolution.A", [6144, 2592])))
    scan(corrected, manifest)
    assert manifest.clips[0]["flags"] == ["missing_cdl"]


# ------------------------------------------------------------------ ProRes

from stubtools import mov


@pytest.fixture
def mixed(env, write_config, mutate):
    """Camera A records ProRes at 4096x2160, camera B stays on R3D."""
    _, camera, sound, data = env
    data = mutate(mutate(data, "decode.sources.A", "prores"), "decode.expected_resolution.A", [4096, 2160])
    return load_config(write_config(data)), camera


def test_a_prores_clip_is_scanned_through_ffprobe(mixed):
    config, camera = mixed
    mov(camera / "A001_1012AB", "A001_C001_1012AB")
    seal(camera / "A001_1012AB")
    manifest, summary = scan(config)
    (clip,) = manifest.clips
    assert clip["source"]["kind"] == "mov" and clip["source"]["path"].endswith("A001_C001_1012AB_001.mov")
    picture = clip["picture"]
    assert (picture["camera"], picture["reel"], picture["codec"]) == ("A", "A001", "ProRes HQ")
    assert (picture["width"], picture["height"], picture["frames"]) == (4096, 2160, 500)
    assert (picture["start_tc"], picture["end_tc"]) == ("10:00:00:00", "10:00:10:00")
    assert picture["iso"] is None and clip["flags"] == []


def test_r3d_and_prores_cameras_are_scanned_in_one_day(mixed):
    config, camera = mixed
    mov(camera / "A001_1012AB", "A001_C001_1012AB")
    r3d(camera / "B001_1012CD", "B001_C001_1012CD")
    seal(camera / "A001_1012AB")
    seal(camera / "B001_1012CD")
    manifest, _ = scan(config)
    assert {c["id"]: (c["source"]["kind"], c["picture"]["codec"]) for c in manifest.clips} == {
        "A001_C001_1012AB": ("mov", "ProRes HQ"), "B001_C001_1012CD": ("r3d", "R3D")}


def test_spanned_prores_is_one_clip_whose_frames_add_up(mixed):
    config, camera = mixed
    mov(camera / "A001_1012AB", "A001_C001_1012AB", parts=2,
        facts=[{"nb_frames": "500"}, {"nb_frames": "120", "tags": {"timecode": "10:00:10:00"}}])
    seal(camera / "A001_1012AB")
    manifest, _ = scan(config)
    (clip,) = manifest.clips
    assert clip["picture"]["frames"] == 620 and clip["picture"]["end_tc"] == "10:00:12:20"
    assert [Path(p).name for p in clip["source"]["parts"]] == ["A001_C001_1012AB_001.mov", "A001_C001_1012AB_002.mov"]
    assert clip["source"]["size_bytes"] == 300


def test_spanned_prores_whose_timecode_does_not_continue_is_not_scanned(mixed):
    config, camera = mixed
    mov(camera / "A001_1012AB", "A001_C001_1012AB", parts=2,
        facts=[{"nb_frames": "500"}, {"nb_frames": "120", "tags": {"timecode": "10:05:00:00"}}])
    seal(camera / "A001_1012AB")
    manifest, summary = scan(config)
    assert manifest.clips == [] and summary.clips_failed == 1
    assert "does not continue" in manifest.to_dict()["scan"]["errors"][0]["message"]


def test_a_camera_that_delivers_the_wrong_kind_of_file_is_flagged(env):
    config, camera, _, _ = env  # here camera A is expected to record R3D
    mov(camera / "A001_1012AB", "A001_C001_1012AB")
    seal(camera / "A001_1012AB")
    manifest, summary = scan(config)
    assert "unexpected_source" in manifest.clips[0]["flags"]
    assert any("unexpected_source" in w for w in summary.warnings)
