# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Day manifest: the only state that matters. Strict schema, atomic writes,
stage ownership of blocks, MobIDs that never change."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dailies.manifest import (
    Manifest,
    ManifestError,
    derive_status,
    manifest_path,
)

TOOLS = {"ffmpeg": {"path": "/opt/homebrew/bin/ffmpeg", "version": "7.1.1"}}

SOURCE = {
    "path": "/Volumes/RAID/CAMERA/A001/A001_C003_1012AB.RDC/A001_C003_1012AB_001.R3D",
    "size_bytes": 48211234567,
    "mhl_verified": True,
}
PICTURE = {
    "camera": "A", "roll": "001", "clip": "C003", "reel": "A001",
    "width": 8192, "height": 4320, "fps": 50.0, "frames": 1732,
    "start_tc": "10:14:22:31", "end_tc": "10:14:57:13",
    "anamorphic": 1.0, "iso": 800, "kelvin": 5600, "tint": 0.0,
    "scene": "14A", "take": "3",
}
SOUND = {
    "status": "matched",
    "files": ["/Volumes/RAID/SOUND/SD001/14A-03.wav"],
    "soundroll": "SD001",
    "start_tc": "10:14:20:00",
    "offset_samples": 113280,
    "channels": 2,
    "track_names": ["MIX L", "MIX R"],
}
OUTPUT = {
    "target": "avid",
    "settings_hash": "sha256:c41e77",
    "video": "/Volumes/DAILIES/A001_C003_1012AB_V.mxf",
    "audio": ["/Volumes/DAILIES/A001_C003_1012AB_A1.mxf"],
    "rendered": "2026-10-12T21:40:03Z",
}


def new_manifest() -> Manifest:
    return Manifest.new(
        day="2026-10-12", day_code="SD014", unit="MU", show="TST",
        config_hash="sha256:abc", tools=TOOLS, created="2026-10-12T19:04:11Z",
    )


def with_clip(clip_id: str = "A001_C003_1012AB") -> Manifest:
    m = new_manifest()
    m.add_clip(clip_id, source=SOURCE, picture=PICTURE, mob_id=None)
    return m


# ------------------------------------------------------------------ header


def test_new_manifest_header():
    m = new_manifest()
    assert m.to_dict()["schema_version"] == 1
    assert m.day == "2026-10-12"
    assert m.tools == TOOLS
    assert m.clips == []


def test_day_must_be_an_iso_date():
    with pytest.raises(ManifestError, match="day"):
        Manifest.new(day="12/10/2026", day_code="SD014", unit="MU", show="TST",
                     config_hash="sha256:abc", tools=TOOLS, created="2026-10-12T19:04:11Z")


def test_manifest_path_is_one_file_per_day_and_unit(tmp_path):
    assert manifest_path(tmp_path, "2026-10-12", "MU") == tmp_path / "2026-10-12_MU.json"


# ------------------------------------------------------------------- clips


def test_add_clip_starts_scanned_with_a_mob_id():
    clip = with_clip().clip("A001_C003_1012AB")
    assert clip["status"] == "scanned"
    assert clip["mob_id"].startswith("urn:smpte:umid:")
    assert clip["flags"] == [] and clip["errors"] == [] and clip["outputs"] == []


def test_same_reel_and_clip_from_two_cameras_is_a_collision():
    m = with_clip()
    with pytest.raises(ManifestError, match="collision"):
        m.add_clip("A001_C003_1012AB", source={**SOURCE, "path": "/elsewhere/A001_C003_1012AB_001.R3D"},
                   picture=PICTURE, mob_id=None)


def test_rescanning_the_same_source_keeps_the_mob_id():
    m = with_clip()
    before = m.clip("A001_C003_1012AB")["mob_id"]
    m.add_clip("A001_C003_1012AB", source=SOURCE, picture={**PICTURE, "scene": "14B"}, mob_id=None)
    clip = m.clip("A001_C003_1012AB")
    assert clip["mob_id"] == before
    assert clip["picture"]["scene"] == "14B"
    assert len(m.clips) == 1


def test_unknown_clip_is_an_error():
    with pytest.raises(ManifestError, match="no clip"):
        new_manifest().clip("Z999_C001_0101ZZ")


def test_picture_block_rejects_unknown_keys():
    with pytest.raises(ManifestError, match="picture.colour_temp"):
        new_manifest().add_clip("A001_C003_1012AB", source=SOURCE,
                                picture={**PICTURE, "colour_temp": 5600}, mob_id=None)


def test_picture_block_requires_its_keys():
    incomplete = {k: v for k, v in PICTURE.items() if k != "frames"}
    with pytest.raises(ManifestError, match="picture.frames"):
        new_manifest().add_clip("A001_C003_1012AB", source=SOURCE, picture=incomplete, mob_id=None)


def test_picture_timecode_is_validated():
    with pytest.raises(ManifestError, match="start_tc"):
        new_manifest().add_clip("A001_C003_1012AB", source=SOURCE,
                                picture={**PICTURE, "start_tc": "10:14:22:50"}, mob_id=None)


def test_scene_and_take_may_be_absent():
    m = new_manifest()
    m.add_clip("A001_C003_1012AB", source=SOURCE, picture={**PICTURE, "scene": None, "take": None}, mob_id=None)
    assert m.clip("A001_C003_1012AB")["picture"]["scene"] is None


# ------------------------------------------------------- stage ownership


def test_a_stage_writes_its_own_block():
    m = with_clip()
    m.set_block("A001_C003_1012AB", stage="sync", name="sound", block=SOUND)
    assert m.clip("A001_C003_1012AB")["sound"]["offset_samples"] == 113280


def test_a_stage_cannot_write_another_stages_block():
    m = with_clip()
    with pytest.raises(ManifestError, match="sync may not write"):
        m.set_block("A001_C003_1012AB", stage="sync", name="colour", block={})


def test_unknown_stage_is_rejected():
    with pytest.raises(ManifestError, match="unknown stage"):
        with_clip().set_block("A001_C003_1012AB", stage="transcode", name="sound", block=SOUND)


def test_sound_status_must_be_a_known_value():
    with pytest.raises(ManifestError, match="sound.status"):
        with_clip().set_block("A001_C003_1012AB", stage="sync", name="sound",
                              block={**SOUND, "status": "probably_fine"})


def test_offset_must_be_a_whole_number_of_samples():
    with pytest.raises(ManifestError, match="offset_samples"):
        with_clip().set_block("A001_C003_1012AB", stage="sync", name="sound",
                              block={**SOUND, "offset_samples": 113280.5})


def test_blocks_are_copied_in_so_callers_cannot_mutate_the_manifest():
    m = with_clip()
    block = dict(SOUND)
    m.set_block("A001_C003_1012AB", stage="sync", name="sound", block=block)
    block["offset_samples"] = 0
    assert m.clip("A001_C003_1012AB")["sound"]["offset_samples"] == 113280


def test_clip_view_is_a_copy():
    m = with_clip()
    m.clip("A001_C003_1012AB")["status"] = "published"
    assert m.clip("A001_C003_1012AB")["status"] == "scanned"


# ----------------------------------------------------------------- outputs


def test_render_records_one_output_per_target():
    m = with_clip()
    m.set_output("A001_C003_1012AB", OUTPUT)
    m.set_output("A001_C003_1012AB", {**OUTPUT, "target": "review", "video": "/x/r.mp4", "audio": []})
    assert [o["target"] for o in m.clip("A001_C003_1012AB")["outputs"]] == ["avid", "review"]


def test_rerender_replaces_only_its_own_target():
    m = with_clip()
    m.set_output("A001_C003_1012AB", OUTPUT)
    m.set_output("A001_C003_1012AB", {**OUTPUT, "target": "review", "video": "/x/r.mp4", "audio": []})
    m.set_output("A001_C003_1012AB", {**OUTPUT, "settings_hash": "sha256:new"})
    outputs = {o["target"]: o for o in m.clip("A001_C003_1012AB")["outputs"]}
    assert outputs["avid"]["settings_hash"] == "sha256:new"
    assert outputs["review"]["settings_hash"] == "sha256:c41e77"


def test_rerender_drops_the_stale_qc_result():
    m = with_clip()
    m.set_output("A001_C003_1012AB", OUTPUT)
    m.set_output_qc("A001_C003_1012AB", "avid", {"result": "pass", "frames": 1732, "start_tc": "10:14:22:31"})
    m.set_output("A001_C003_1012AB", {**OUTPUT, "settings_hash": "sha256:new"})
    assert "qc" not in m.clip("A001_C003_1012AB")["outputs"][0]


def test_qc_needs_an_existing_output():
    with pytest.raises(ManifestError, match="no review output"):
        with_clip().set_output_qc("A001_C003_1012AB", "review", {"result": "pass"})


def test_qc_result_must_be_pass_or_fail():
    m = with_clip()
    m.set_output("A001_C003_1012AB", OUTPUT)
    with pytest.raises(ManifestError, match="qc.result"):
        m.set_output_qc("A001_C003_1012AB", "avid", {"result": "meh"})


def test_is_current_compares_settings_hashes():
    m = with_clip()
    m.set_output("A001_C003_1012AB", OUTPUT)
    assert m.is_current("A001_C003_1012AB", "avid", "sha256:c41e77")
    assert not m.is_current("A001_C003_1012AB", "avid", "sha256:different")
    assert not m.is_current("A001_C003_1012AB", "review", "sha256:c41e77")


# ------------------------------------------------------ flags and failures


def test_flags_are_unique_and_ordered():
    m = with_clip()
    for flag in ("missing_cdl", "short_clip", "missing_cdl"):
        m.add_flag("A001_C003_1012AB", flag)
    assert m.clip("A001_C003_1012AB")["flags"] == ["missing_cdl", "short_clip"]


def test_quarantine_captures_stage_and_stderr():
    m = with_clip()
    m.quarantine("A001_C003_1012AB", stage="render", message="ffmpeg exited 1", stderr="Invalid data found")
    clip = m.clip("A001_C003_1012AB")
    assert clip["status"] == "quarantined"
    assert clip["errors"] == [{"stage": "render", "message": "ffmpeg exited 1", "stderr": "Invalid data found"}]


def test_status_must_be_a_known_value():
    with pytest.raises(ManifestError, match="status"):
        with_clip().set_status("A001_C003_1012AB", "done")


# ----------------------------------------------------------------- status


def test_status_follows_the_blocks():
    m = with_clip()
    cid = "A001_C003_1012AB"
    targets = ("avid", "review")
    assert derive_status(m.clip(cid), targets) == "scanned"
    m.set_block(cid, stage="sync", name="sound", block=SOUND)
    assert derive_status(m.clip(cid), targets) == "synced"
    m.set_block(cid, stage="grade", name="colour", block={"apply": "baked", "baked_lut": "/c/9f.cube", "lut_hash": "9f"})
    assert derive_status(m.clip(cid), targets) == "graded"
    m.set_output(cid, OUTPUT)
    assert derive_status(m.clip(cid), targets) == "graded"  # one of two targets is not rendered yet
    m.set_output(cid, {**OUTPUT, "target": "review", "video": "/x/r.mp4", "audio": []})
    assert derive_status(m.clip(cid), targets) == "rendered"
    m.set_output_qc(cid, "avid", {"result": "pass"})
    assert derive_status(m.clip(cid), targets) == "rendered"
    m.set_output_qc(cid, "review", {"result": "pass"})
    assert derive_status(m.clip(cid), targets) == "qc_pass"


def test_any_failed_output_fails_the_clip():
    m = with_clip()
    cid = "A001_C003_1012AB"
    m.set_output(cid, OUTPUT)
    m.set_output_qc(cid, "avid", {"result": "fail", "check": "frame_count"})
    assert derive_status(m.clip(cid), ("avid",)) == "qc_fail"


def test_quarantine_wins_over_everything():
    m = with_clip()
    cid = "A001_C003_1012AB"
    m.set_output(cid, OUTPUT)
    m.quarantine(cid, stage="qc", message="ffprobe crashed", stderr="")
    assert derive_status(m.clip(cid), ("avid",)) == "quarantined"


# ------------------------------------------------------------- persistence


def test_save_and_load_round_trip(tmp_path):
    m = with_clip()
    m.set_block("A001_C003_1012AB", stage="sync", name="sound", block=SOUND)
    path = tmp_path / "2026-10-12_MU.json"
    m.save(path)
    assert Manifest.load(path).to_dict() == m.to_dict()


def test_save_leaves_no_temp_files(tmp_path):
    path = tmp_path / "day.json"
    with_clip().save(path)
    assert [p.name for p in tmp_path.iterdir()] == ["day.json"]


def test_failed_save_keeps_the_previous_manifest(tmp_path, monkeypatch):
    path = tmp_path / "day.json"
    first = with_clip()
    first.save(path)
    before = path.read_text()

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("dailies.manifest.os.replace", explode)
    second = with_clip()
    second.add_flag("A001_C003_1012AB", "short_clip")
    with pytest.raises(OSError):
        second.save(path)
    assert path.read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == ["day.json"]


def test_saved_json_is_sorted_and_readable(tmp_path):
    path = tmp_path / "day.json"
    with_clip().save(path)
    text = path.read_text()
    assert text.endswith("\n")
    assert json.loads(text)["clips"][0]["id"] == "A001_C003_1012AB"
    assert "\n  " in text  # indented for humans and for diffs


def test_load_rejects_unknown_top_level_keys(tmp_path):
    path = tmp_path / "day.json"
    data = with_clip().to_dict()
    data["notes"] = "hand edited"
    path.write_text(json.dumps(data))
    with pytest.raises(ManifestError, match="notes"):
        Manifest.load(path)


def test_load_rejects_a_newer_schema(tmp_path):
    path = tmp_path / "day.json"
    data = with_clip().to_dict()
    data["schema_version"] = 99
    path.write_text(json.dumps(data))
    with pytest.raises(ManifestError, match="schema_version"):
        Manifest.load(path)


def test_load_rejects_a_tampered_mob_id(tmp_path):
    path = tmp_path / "day.json"
    data = with_clip().to_dict()
    data["clips"][0]["mob_id"] = "A001"
    path.write_text(json.dumps(data))
    with pytest.raises(ManifestError, match="mob_id"):
        Manifest.load(path)


def test_load_rejects_truncated_json(tmp_path):
    path = tmp_path / "day.json"
    path.write_text('{"schema_version": 1, "clips": [')
    with pytest.raises(ManifestError, match="not valid JSON"):
        Manifest.load(path)


def test_load_missing_file_is_a_manifest_error(tmp_path):
    with pytest.raises(ManifestError, match="not found"):
        Manifest.load(tmp_path / "absent.json")


# ------------------------------------------------- load validation, continued


def _saved(tmp_path, edit) -> Path:
    data = with_clip().to_dict()
    edit(data)
    path = tmp_path / "day.json"
    path.write_text(json.dumps(data))
    return path


def test_block_must_be_a_mapping():
    with pytest.raises(ManifestError, match="picture: expected a mapping"):
        new_manifest().add_clip("A001_C003_1012AB", source=SOURCE, picture=["not", "a", "dict"], mob_id=None)


@pytest.mark.parametrize("frames", [0, -5, 17.5, True, "1732"])
def test_frame_count_must_be_a_positive_integer(frames):
    with pytest.raises(ManifestError, match="picture.frames"):
        new_manifest().add_clip("A001_C003_1012AB", source=SOURCE, picture={**PICTURE, "frames": frames}, mob_id=None)


def test_load_validates_a_stored_qc_result(tmp_path):
    def edit(data):
        data["clips"][0]["outputs"] = [{**OUTPUT, "qc": {"result": "maybe"}}]
    with pytest.raises(ManifestError, match="qc.result"):
        Manifest.load(_saved(tmp_path, edit))


def test_load_accepts_a_stored_qc_result(tmp_path):
    def edit(data):
        data["clips"][0]["outputs"] = [{**OUTPUT, "qc": {"result": "pass"}}]
    assert Manifest.load(_saved(tmp_path, edit)).clip("A001_C003_1012AB")["outputs"][0]["qc"] == {"result": "pass"}


def test_load_rejects_a_clip_that_is_not_a_mapping(tmp_path):
    with pytest.raises(ManifestError, match="clip: expected a mapping"):
        Manifest.load(_saved(tmp_path, lambda data: data["clips"].append("A001_C004")))


def test_load_rejects_unknown_clip_keys(tmp_path):
    with pytest.raises(ManifestError, match="clip.rating"):
        Manifest.load(_saved(tmp_path, lambda data: data["clips"][0].update(rating=5)))


def test_load_rejects_a_clip_with_a_missing_key(tmp_path):
    with pytest.raises(ManifestError, match="clip.flags: missing"):
        Manifest.load(_saved(tmp_path, lambda data: data["clips"][0].pop("flags")))


def test_load_rejects_an_unknown_clip_status(tmp_path):
    with pytest.raises(ManifestError, match="clip.status"):
        Manifest.load(_saved(tmp_path, lambda data: data["clips"][0].update(status="done")))


def test_load_rejects_json_that_is_not_an_object(tmp_path):
    path = tmp_path / "day.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(ManifestError, match="JSON object"):
        Manifest.load(path)


def test_load_rejects_a_missing_header_key(tmp_path):
    with pytest.raises(ManifestError, match="missing key 'config_hash'"):
        Manifest.load(_saved(tmp_path, lambda data: data.pop("config_hash")))


def test_set_status_records_a_known_status():
    m = with_clip()
    m.set_status("A001_C003_1012AB", "rendering")
    assert m.clip("A001_C003_1012AB")["status"] == "rendering"


# ------------------------------------------------------- what scan records

SOUND_FILE = {
    "path": "/Volumes/RAID/SOUND/SD014/14A-03.wav", "size_bytes": 1000, "mhl_verified": True, "soundroll": "SD014",
    "scene": "14A", "take": "3", "metadata_source": "ixml", "track_names": ["MIX L", "MIX R"], "sample_rate": 48000,
    "channels": 2, "bit_depth": 24, "sample_count": 1662720, "start_samples": 1769280123,
    "end_samples": 1770942843, "flags": [],
}


def test_new_manifest_has_an_empty_sound_inventory_and_scan_report():
    data = new_manifest().to_dict()
    assert data["sound_files"] == []
    assert data["scan"] == {"folders": [], "errors": []}


def test_scan_records_the_sound_inventory_so_sync_never_reads_the_filesystem():
    m = new_manifest()
    m.set_sound_files([SOUND_FILE])
    assert m.sound_files[0]["start_samples"] == 1769280123


def test_sound_inventory_entries_are_validated():
    with pytest.raises(ManifestError, match="sound_file.codec"):
        new_manifest().set_sound_files([{**SOUND_FILE, "codec": "pcm"}])
    with pytest.raises(ManifestError, match="sound_file.start_samples"):
        new_manifest().set_sound_files([{**SOUND_FILE, "start_samples": 1.5}])


def test_sound_file_without_timecode_is_allowed():
    m = new_manifest()
    m.set_sound_files([{**SOUND_FILE, "start_samples": None, "end_samples": None, "flags": ["no_time_reference"]}])
    assert m.sound_files[0]["start_samples"] is None


def test_scan_report_lists_skipped_folders_and_failed_clips():
    m = new_manifest()
    m.set_scan_report(folders=[{"path": "/c/A002", "kind": "camera", "eligible": False, "reason": "no ascmhl folder"}],
                      errors=[{"clip": "A001_C009_1012AB", "message": "r3dtool exited 4", "stderr": "failed to load"}])
    report = m.to_dict()["scan"]
    assert report["folders"][0]["eligible"] is False
    assert report["errors"][0]["clip"] == "A001_C009_1012AB"


def test_tool_versions_are_recorded():
    m = new_manifest()
    m.set_tools({"ffmpeg": {"path": "/opt/homebrew/bin/ffmpeg", "version": "7.1.1"}})
    assert m.tools["ffmpeg"]["version"] == "7.1.1"


def test_picture_block_may_carry_the_other_timecode_tracks():
    m = new_manifest()
    m.add_clip("A001_C003_1012AB", source=SOURCE, mob_id=None,
               picture={**PICTURE, "tc_absolute": "10:14:22:31", "tc_edge": "01:00:30:20", "tc_preference": "absolute",
                        "camera_model": "V-RAPTOR 8K VV", "dropped_frames": 0})
    assert m.clip("A001_C003_1012AB")["picture"]["tc_edge"] == "01:00:30:20"


def test_timecode_is_checked_at_the_clips_own_rate():
    # a 100 fps clip must be recordable so that it can be flagged, even though it will never render
    m = new_manifest()
    m.add_clip("A001_C003_1012AB", source=SOURCE, mob_id=None,
               picture={**PICTURE, "fps": 100.0, "start_tc": "10:14:22:87", "end_tc": "10:14:40:19"})
    assert m.clip("A001_C003_1012AB")["picture"]["fps"] == 100.0
    with pytest.raises(ManifestError, match="start_tc"):
        new_manifest().add_clip("A001_C003_1012AB", source=SOURCE, mob_id=None,
                                picture={**PICTURE, "fps": 25.0, "start_tc": "10:14:22:31"})


def test_saved_manifest_with_scan_data_round_trips(tmp_path):
    m = with_clip()
    m.set_sound_files([SOUND_FILE])
    m.set_scan_report(folders=[], errors=[])
    path = tmp_path / "day.json"
    m.save(path)
    assert Manifest.load(path).to_dict() == m.to_dict()


def test_picture_fps_must_be_a_number():
    with pytest.raises(ManifestError, match="picture.fps"):
        new_manifest().add_clip("A001_C003_1012AB", source=SOURCE, picture={**PICTURE, "fps": "fifty"}, mob_id=None)


# ------------------------------------------------------- stage owned flags


def test_a_stage_replaces_only_the_flags_it_owns():
    m = with_clip()
    cid = "A001_C003_1012AB"
    for flag in ("wrong_fps", "sound_gap", "missing_cdl"):
        m.add_flag(cid, flag)
    m.replace_flags(cid, owned={"sound_gap", "sync_review"}, current=["sync_review"])
    assert m.clip(cid)["flags"] == ["wrong_fps", "missing_cdl", "sync_review"]


def test_replacing_with_nothing_clears_the_owned_flags():
    m = with_clip()
    cid = "A001_C003_1012AB"
    m.add_flag(cid, "sound_gap")
    m.replace_flags(cid, owned={"sound_gap"}, current=[])
    assert m.clip(cid)["flags"] == []


def test_a_stage_may_not_set_a_flag_it_does_not_own():
    with pytest.raises(ManifestError, match="missing_cdl"):
        with_clip().replace_flags("A001_C003_1012AB", owned={"sound_gap"}, current=["missing_cdl"])


# ------------------------------------------------------------ colour block

EXACT = {"apply": "exact", "lut_hash": "sha256:9f", "show_lut": "/show/luts/show.cube", "camera_look": None,
         "cdl": {"slope": [1.02, 1.0, 0.98], "offset": [0, 0, 0], "power": [1, 1, 1], "sat": 1.0},
         "cdl_source": "/show/cdl/14A.cdl", "note": "CDL 14A"}
BAKED = {"apply": "baked", "lut_hash": "sha256:9f", "baked_lut": "/show/cache/luts/9f3c1b.cube"}


@pytest.mark.parametrize("block", [EXACT, BAKED])
def test_both_ways_of_applying_colour_are_recordable(block):
    m = with_clip()
    m.set_block("A001_C003_1012AB", stage="grade", name="colour", block=block)
    assert m.clip("A001_C003_1012AB")["colour"]["apply"] == block["apply"]


def test_colour_block_must_say_how_it_is_applied():
    with pytest.raises(ManifestError, match="colour.apply"):
        with_clip().set_block("A001_C003_1012AB", stage="grade", name="colour",
                              block={"lut_hash": "x", "baked_lut": "/c/x.cube"})


def test_baked_colour_needs_its_cube_and_exact_colour_needs_its_show_lut():
    with pytest.raises(ManifestError, match="colour.baked_lut"):
        with_clip().set_block("A001_C003_1012AB", stage="grade", name="colour", block={"apply": "baked", "lut_hash": "x"})
    with pytest.raises(ManifestError, match="colour.show_lut"):
        with_clip().set_block("A001_C003_1012AB", stage="grade", name="colour", block={"apply": "exact", "lut_hash": "x"})


def test_unknown_apply_mode_is_rejected():
    with pytest.raises(ManifestError, match="colour.apply"):
        with_clip().set_block("A001_C003_1012AB", stage="grade", name="colour",
                              block={"apply": "resolve", "lut_hash": "x", "show_lut": "/s.cube"})
