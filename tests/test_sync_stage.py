# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Sync stage: works from the manifest alone, never from the filesystem."""

from __future__ import annotations

import pytest

from dailies.commands.render import build_render_plan
from dailies.manifest import Manifest
from dailies.naming import DayContext
from dailies.sync.stage import run_sync

RATE = 48000
PICTURE = {"camera": "A", "roll": "001", "clip": "C001", "reel": "A001", "width": 8192, "height": 4320, "fps": 50.0,
           "frames": 1500, "start_tc": "10:00:00:00", "end_tc": "10:00:30:00", "anamorphic": 1.0, "iso": 800,
           "kelvin": 5600, "tint": 0.0, "scene": "14A", "take": "3"}


def samples(tc: str) -> int:
    hh, mm, ss, ff = (int(x) for x in tc.split(":"))
    return ((hh * 3600 + mm * 60 + ss) * 50 + ff) * 960


def sound_file(name: str, start_tc: str, seconds: int, *, scene="14A", take="3", flags=(), start=...) -> dict:
    begin = samples(start_tc) if start is ... else start
    return {"path": f"/Volumes/RAID/SOUND/SD014/{name}", "size_bytes": 1, "mhl_verified": True, "soundroll": "SD014",
            "scene": scene, "take": take, "metadata_source": "ixml", "track_names": ["MIX L", "MIX R", "BOOM"],
            "sample_rate": RATE, "channels": 3, "bit_depth": 24, "sample_count": seconds * RATE,
            "start_samples": begin, "end_samples": None if begin is None else begin + seconds * RATE,
            "flags": list(flags)}


def manifest_with(clips: dict[str, dict], sounds: list[dict]) -> Manifest:
    m = Manifest.new(day="2026-10-12", day_code="SD014", unit="MU", show="TST", config_hash="sha256:x", tools={},
                     created="2026-10-12T19:00:00Z")
    for clip_id, overrides in clips.items():
        m.add_clip(clip_id, mob_id=None, picture={**PICTURE, **overrides},
                   source={"path": f"/Volumes/RAID/CAMERA/{clip_id}_001.R3D", "size_bytes": 1, "mhl_verified": True})
    m.set_sound_files(sounds)
    return m


def test_matched_clip_gets_a_complete_sound_block(load_valid):
    m = manifest_with({"A001_C001_1012AB": {}}, [sound_file("14A-03.wav", "09:59:50:00", 120)])
    summary = run_sync(load_valid(), m)
    sound = m.clip("A001_C001_1012AB")["sound"]
    assert sound["status"] == "matched"
    assert sound["files"] == ["/Volumes/RAID/SOUND/SD014/14A-03.wav"]
    assert sound["offset_samples"] == 10 * RATE
    assert (sound["soundroll"], sound["channels"], sound["track_names"]) == ("SD014", 3, ["MIX L", "MIX R", "BOOM"])
    assert sound["start_tc"] == "09:59:50:00"
    assert summary.by_status == {"matched": 1}


def test_clip_status_advances_to_synced(load_valid):
    m = manifest_with({"A001_C001_1012AB": {}}, [sound_file("14A-03.wav", "09:59:50:00", 120)])
    run_sync(load_valid(), m)
    assert m.clip("A001_C001_1012AB")["status"] == "synced"


def test_a_clip_without_sound_still_gets_a_block_and_a_flag(load_valid):
    m = manifest_with({"A001_C001_1012AB": {}}, [])
    run_sync(load_valid(), m)
    clip = m.clip("A001_C001_1012AB")
    assert clip["sound"]["status"] == "no_sound" and "files" not in clip["sound"]
    assert clip["flags"] == ["no_sound"]


def test_the_synced_clip_feeds_the_render_plan_unchanged(load_valid):
    config = load_valid()
    m = manifest_with({"A001_C001_1012AB": {}}, [sound_file("14A-03.wav", "09:59:50:00", 120)])
    run_sync(config, m)
    clip = m.clip("A001_C001_1012AB")
    clip["colour"] = {"apply": "baked", "baked_lut": "/c/x.cube", "lut_hash": "x"}
    plan = build_render_plan(config, DayContext("TST", "2026-10-12", "SD014", "MU"), clip)
    graph = plan.runnables[-1].argv[plan.runnables[-1].argv.index("-filter_complex") + 1]
    assert f"atrim=start_sample={10 * RATE}" in graph


def test_time_of_day_timecode_is_used_even_when_the_camera_shows_edge_code(load_valid):
    picture = {"start_tc": "01:00:00:00", "end_tc": "01:00:30:00", "tc_absolute": "10:00:00:00",
               "tc_edge": "01:00:00:00", "tc_preference": "edge"}
    m = manifest_with({"A001_C001_1012AB": picture}, [sound_file("14A-03.wav", "09:59:50:00", 120)])
    run_sync(load_valid(), m)
    assert m.clip("A001_C001_1012AB")["sound"]["offset_samples"] == 10 * RATE


def test_sound_files_without_timecode_or_at_the_wrong_rate_are_left_out_and_reported(load_valid):
    sounds = [sound_file("notc.wav", "09:59:50:00", 120, start=None, flags=["no_time_reference"]),
              sound_file("96k.wav", "09:59:50:00", 120, flags=["wrong_sample_rate"])]
    m = manifest_with({"A001_C001_1012AB": {}}, sounds)
    summary = run_sync(load_valid(), m)
    assert m.clip("A001_C001_1012AB")["sound"]["status"] == "no_sound"
    assert sorted(summary.excluded_sound) == ["96k.wav: wrong_sample_rate", "notc.wav: no_time_reference"]


def test_a_clip_at_the_wrong_rate_is_not_synced_and_not_conformed(load_valid):
    m = manifest_with({"A001_C001_1012AB": {"fps": 25.0, "start_tc": "10:00:00:00", "end_tc": "10:01:00:00"}},
                      [sound_file("14A-03.wav", "09:59:50:00", 120)])
    summary = run_sync(load_valid(), m)
    assert m.clip("A001_C001_1012AB")["sound"] is None
    assert any("25" in warning for warning in summary.warnings)


def test_mos_by_camera_rule(valid_config, write_config, mutate):
    from dailies.config import load_config
    config = load_config(write_config(mutate(valid_config, "sound.mos_cameras", ["B"])))
    m = manifest_with({"B001_C001_1012CD": {"camera": "B", "reel": "B001"}}, [sound_file("14A-03.wav", "09:59:50:00", 120)])
    run_sync(config, m)
    clip = m.clip("B001_C001_1012CD")
    assert clip["sound"] == {"status": "mos", "note": "marked MOS"} and clip["flags"] == []


@pytest.mark.parametrize("slate", [{"scene": "14A MOS"}, {"take": "mos"}, {"scene": "MOS"}])
def test_mos_by_slate(load_valid, slate):
    m = manifest_with({"A001_C001_1012AB": slate}, [sound_file("14A-03.wav", "09:59:50:00", 120)])
    run_sync(load_valid(), m)
    assert m.clip("A001_C001_1012AB")["sound"]["status"] == "mos"


def test_a_scene_that_merely_contains_the_letters_is_not_mos(load_valid):
    m = manifest_with({"A001_C001_1012AB": {"scene": "MOSCOW 3"}}, [sound_file("x.wav", "09:59:50:00", 120, scene="MOSCOW 3")])
    run_sync(load_valid(), m)
    assert m.clip("A001_C001_1012AB")["sound"]["status"] == "matched"


def test_candidates_are_recorded_for_the_clips_that_need_review(load_valid):
    sounds = [sound_file("main.wav", "09:59:50:00", 120), sound_file("backup.wav", "09:59:55:00", 120)]
    m = manifest_with({"A001_C001_1012AB": {}}, sounds)
    run_sync(load_valid(), m)
    clip = m.clip("A001_C001_1012AB")
    assert clip["sound"]["status"] == "multiple_candidates" and clip["flags"] == ["sync_review"]
    assert [c["path"].rsplit("/", 1)[1] for c in clip["sound"]["candidates"]] == ["main.wav", "backup.wav"]
    assert clip["sound"]["candidates"][0]["overlap_samples"] == 30 * RATE


def test_resync_after_a_fix_clears_the_old_flags_and_keeps_other_stages_flags(load_valid):
    m = manifest_with({"A001_C001_1012AB": {}}, [])
    config = load_valid()
    run_sync(config, m)
    m.add_flag("A001_C001_1012AB", "missing_cdl")
    m.set_sound_files([sound_file("14A-03.wav", "09:59:50:00", 120)])
    run_sync(config, m)
    clip = m.clip("A001_C001_1012AB")
    assert clip["sound"]["status"] == "matched" and clip["flags"] == ["missing_cdl"]


def test_sync_is_deterministic(load_valid):
    sounds = [sound_file("b.wav", "09:59:50:00", 120), sound_file("a.wav", "09:59:50:00", 120)]
    first, second = manifest_with({"A001_C001_1012AB": {}}, sounds), manifest_with({"A001_C001_1012AB": {}}, sounds[::-1])
    run_sync(load_valid(), first)
    run_sync(load_valid(), second)
    assert first.clip("A001_C001_1012AB")["sound"] == second.clip("A001_C001_1012AB")["sound"]


def test_clip_filter_limits_the_work(load_valid):
    m = manifest_with({"A001_C001_1012AB": {}, "B001_C001_1012CD": {"camera": "B"}}, [])
    run_sync(load_valid(), m, clip_filter="B001_*")
    assert m.clip("A001_C001_1012AB")["sound"] is None and m.clip("B001_C001_1012CD")["sound"] is not None


def test_quarantined_clip_stays_quarantined(load_valid):
    m = manifest_with({"A001_C001_1012AB": {}}, [sound_file("14A-03.wav", "09:59:50:00", 120)])
    m.quarantine("A001_C001_1012AB", stage="render", message="x", stderr="")
    run_sync(load_valid(), m)
    assert m.clip("A001_C001_1012AB")["status"] == "quarantined"
