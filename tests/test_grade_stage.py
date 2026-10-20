# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Grade stage: resolve each clip's colour decisions, record them, bake only when asked to."""

from __future__ import annotations

from pathlib import Path

import pytest

from cubebuilder import identity, log_to_display, write_cube
from dailies.commands.render import build_render_plan
from dailies.config import load_config
from dailies.grade.stage import GradeAbort, run_grade
from dailies.manifest import Manifest
from dailies.naming import DayContext

DAY = DayContext(show="TST", day="2026-10-12", day_code="SD014", unit="MU")
PICTURE = {"camera": "A", "roll": "001", "clip": "C001", "reel": "A001", "width": 8192, "height": 4320, "fps": 50.0,
           "frames": 1500, "start_tc": "10:00:00:00", "end_tc": "10:00:30:00", "anamorphic": 1.0, "iso": 800,
           "kelvin": 5600, "tint": 0.0, "scene": "14A", "take": "3"}
CDL = """<ColorDecisionList><ColorDecision><ColorCorrection id="{id}"><SOPNode><Slope>{slope} 1.0 1.0</Slope>
<Offset>0 0 0</Offset><Power>1 1 1</Power></SOPNode><SATNode><Saturation>1.0</Saturation></SATNode>
</ColorCorrection></ColorDecision></ColorDecisionList>"""


@pytest.fixture
def show(valid_config, write_config, mutate):
    """Real cube files where the config's LUT paths point, plus a way to reload with changes."""
    write_cube(Path(valid_config["colour"]["show_lut"]), 9, log_to_display)
    write_cube(Path(valid_config["colour"]["camera_looks"]["A"]), 9, identity)

    def load(**changes):
        data = valid_config
        for dotted, value in changes.items():
            data = mutate(data, dotted.replace("__", "."), value)
        return load_config(write_config(data))

    return load, Path(valid_config["colour"]["cdl_dir"]), Path(valid_config["colour"]["lut_cache"])


def cdl_file(folder: Path, name: str, slope: float = 1.1, ident: str | None = None) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CDL.format(id=ident or Path(name).stem, slope=slope))
    return path


def manifest_with(**clips) -> Manifest:
    m = Manifest.new(day="2026-10-12", day_code="SD014", unit="MU", show="TST", config_hash="sha256:x", tools={},
                     created="2026-10-12T19:00:00Z")
    for clip_id, overrides in clips.items():
        m.add_clip(clip_id, mob_id=None, picture={**PICTURE, **overrides},
                   source={"path": f"/c/{clip_id}_001.R3D", "size_bytes": 1, "mhl_verified": True})
        m.set_block(clip_id, stage="sync", name="sound", block={"status": "mos"})
    return m


# ---------------------------------------------------------- finding the CDL


def test_a_clip_specific_cdl_in_the_day_folder_wins(show):
    load, cdl_dir, _ = show
    cdl_file(cdl_dir, "TST_20261012_SD014_MU/A001_C001_1012AB.cdl", 1.20)
    cdl_file(cdl_dir, "TST_20261012_SD014_MU/14A.cdl", 1.10)
    cdl_file(cdl_dir, "14A.cdl", 1.05)
    m = manifest_with(A001_C001_1012AB={})
    run_grade(load(), m, DAY)
    colour = m.clip("A001_C001_1012AB")["colour"]
    assert colour["cdl"]["slope"] == [1.2, 1.0, 1.0]
    assert colour["cdl_source"].endswith("TST_20261012_SD014_MU/A001_C001_1012AB.cdl")
    assert m.clip("A001_C001_1012AB")["flags"] == []


def test_the_scene_cdl_is_used_when_there_is_no_clip_cdl(show):
    load, cdl_dir, _ = show
    cdl_file(cdl_dir, "14A.ccc", 1.05)
    m = manifest_with(A001_C001_1012AB={}, B001_C001_1012CD={"camera": "B"})
    run_grade(load(), m, DAY)
    assert {c["colour"]["cdl"]["slope"][0] for c in m.clips} == {1.05}


def test_no_cdl_falls_back_to_the_neutral_default_and_is_still_flagged(show):
    load, cdl_dir, _ = show
    cdl_file(cdl_dir, "000.cdl", 1.0)
    m = manifest_with(A001_C001_1012AB={})
    summary = run_grade(load(), m, DAY)
    clip = m.clip("A001_C001_1012AB")
    assert clip["colour"]["cdl_source"].endswith("000.cdl") and clip["colour"]["cdl"]["slope"] == [1.0, 1.0, 1.0]
    assert clip["flags"] == ["missing_cdl"]
    assert summary.by_cdl == {"default": 1}


def test_no_cdl_and_no_default_is_show_lut_only(show):
    load, _, _ = show
    m = manifest_with(A001_C001_1012AB={})
    run_grade(load(colour__cdl_default=None), m, DAY)
    clip = m.clip("A001_C001_1012AB")
    assert clip["colour"]["cdl"] is None and clip["colour"]["cdl_source"] is None
    assert clip["flags"] == ["missing_cdl"]


def test_a_configured_default_that_is_not_on_disk_is_reported(show):
    load, _, _ = show
    m = manifest_with(A001_C001_1012AB={})
    summary = run_grade(load(), m, DAY)
    assert m.clip("A001_C001_1012AB")["flags"] == ["missing_cdl"]
    assert any("000" in warning and "not found" in warning for warning in summary.warnings)


def test_an_unreadable_cdl_is_flagged_and_never_replaced_by_a_less_specific_one(show):
    load, cdl_dir, _ = show
    (cdl_dir / "A001_C001_1012AB.cdl").write_text("<ColorDecisionList><broken")
    cdl_file(cdl_dir, "14A.cdl", 1.10)
    m = manifest_with(A001_C001_1012AB={})
    run_grade(load(), m, DAY)
    clip = m.clip("A001_C001_1012AB")
    assert clip["colour"]["cdl"] is None
    assert clip["flags"] == ["cdl_unreadable"]
    assert "not valid XML" in clip["colour"]["note"]


def test_a_clip_without_a_scene_skips_the_scene_templates(show):
    load, cdl_dir, _ = show
    cdl_file(cdl_dir, ".cdl", 1.5)  # what "{scene}.cdl" would be with an empty scene, must never match
    m = manifest_with(A001_C001_1012AB={"scene": None})
    run_grade(load(colour__cdl_default=None), m, DAY)
    assert m.clip("A001_C001_1012AB")["colour"]["cdl"] is None


def test_a_scene_name_cannot_walk_out_of_the_cdl_folder(show):
    load, cdl_dir, _ = show
    cdl_file(cdl_dir.parent, "outside.cdl", 1.5)
    m = manifest_with(A001_C001_1012AB={"scene": "../outside"})
    run_grade(load(colour__cdl_default=None), m, DAY)
    assert m.clip("A001_C001_1012AB")["colour"]["cdl"] is None


def test_a_collection_is_searched_for_the_clip_then_the_scene(show):
    load, cdl_dir, _ = show
    (cdl_dir / "14A.ccc").write_text(
        '<ColorCorrectionCollection><ColorCorrection id="14A"><SOPNode><Slope>1.3 1 1</Slope><Offset>0 0 0</Offset>'
        '<Power>1 1 1</Power></SOPNode></ColorCorrection><ColorCorrection id="other"><SOPNode><Slope>2 1 1</Slope>'
        '<Offset>0 0 0</Offset><Power>1 1 1</Power></SOPNode></ColorCorrection></ColorCorrectionCollection>')
    m = manifest_with(A001_C001_1012AB={})
    run_grade(load(), m, DAY)
    assert m.clip("A001_C001_1012AB")["colour"]["cdl"]["slope"][0] == 1.3


# ------------------------------------------------------------ colour block


def test_exact_mode_records_the_chain_and_bakes_nothing(show):
    load, cdl_dir, cache = show
    cdl_file(cdl_dir, "14A.cdl")
    m = manifest_with(A001_C001_1012AB={}, B001_C001_1012CD={"camera": "B"})
    config = load()
    summary = run_grade(config, m, DAY)
    a, b = (m.clip(c)["colour"] for c in ("A001_C001_1012AB", "B001_C001_1012CD"))
    assert a["apply"] == "exact" and "baked_lut" not in a
    assert a["show_lut"] == str(config.colour.show_lut)
    assert a["camera_look"] == str(config.colour.camera_looks["A"]) and b["camera_look"] is None
    assert a["lut_hash"] != b["lut_hash"] and a["lut_hash"].startswith("sha256:")
    assert list(cache.iterdir()) == [] and summary.cubes_baked == 0


def test_baked_mode_bakes_one_cube_per_distinct_look(show):
    load, cdl_dir, cache = show
    cdl_file(cdl_dir, "14A.cdl")
    m = manifest_with(A001_C001_1012AB={}, A001_C002_1012AB={}, B001_C001_1012CD={"camera": "B"})
    summary = run_grade(load(colour__apply="baked"), m, DAY)
    cubes = {m.clip(c["id"])["colour"]["baked_lut"] for c in m.clips}
    assert len(cubes) == 2 and summary.cubes_baked == 2  # camera A has a look, camera B does not
    assert sorted(p.name for p in cache.iterdir()) == sorted(Path(c).name for c in cubes)


def test_regrading_one_scene_changes_only_that_scenes_hash(show):
    load, cdl_dir, _ = show
    cdl_file(cdl_dir, "14A.cdl", 1.10)
    cdl_file(cdl_dir, "15.cdl", 1.10)
    m = manifest_with(A001_C001_1012AB={"scene": "14A"}, A001_C002_1012AB={"scene": "15"})
    config = load()
    run_grade(config, m, DAY)
    before = {c["id"]: c["colour"]["lut_hash"] for c in m.clips}
    cdl_file(cdl_dir, "14A.cdl", 1.15)
    run_grade(config, m, DAY)
    after = {c["id"]: c["colour"]["lut_hash"] for c in m.clips}
    assert before["A001_C001_1012AB"] != after["A001_C001_1012AB"]
    assert before["A001_C002_1012AB"] == after["A001_C002_1012AB"]


def test_grading_again_with_no_changes_changes_nothing(show):
    load, cdl_dir, _ = show
    cdl_file(cdl_dir, "14A.cdl")
    m = manifest_with(A001_C001_1012AB={})
    config = load()
    run_grade(config, m, DAY)
    first = m.to_dict()
    run_grade(config, m, DAY)
    assert m.to_dict() == first


def test_status_advances_and_flags_are_replaced_not_piled_up(show):
    load, cdl_dir, _ = show
    m = manifest_with(A001_C001_1012AB={})
    config = load(colour__cdl_default=None)
    run_grade(config, m, DAY)
    assert m.clip("A001_C001_1012AB")["status"] == "graded" and m.clip("A001_C001_1012AB")["flags"] == ["missing_cdl"]
    m.add_flag("A001_C001_1012AB", "sound_gap")
    cdl_file(cdl_dir, "14A.cdl")
    run_grade(config, m, DAY)
    assert m.clip("A001_C001_1012AB")["flags"] == ["sound_gap"]


def test_the_graded_clip_feeds_the_render_plan_in_both_modes(show):
    load, cdl_dir, _ = show
    cdl_file(cdl_dir, "14A.cdl")
    for mode in ("exact", "baked"):
        config = load(colour__apply=mode)
        m = manifest_with(A001_C001_1012AB={})
        run_grade(config, m, DAY)
        plan = build_render_plan(config, DAY, m.clip("A001_C001_1012AB"))
        graph = plan.runnables[-1].argv[plan.runnables[-1].argv.index("-filter_complex") + 1]
        assert ("lutrgb=" in graph) == (mode == "exact")


def test_clip_filter_and_dry_run(show):
    load, cdl_dir, cache = show
    m = manifest_with(A001_C001_1012AB={}, B001_C001_1012CD={"camera": "B"})
    summary = run_grade(load(colour__apply="baked"), m, DAY, clip_filter="B001_*", dry_run=True)
    assert summary.lines and list(cache.iterdir()) == []
    assert all(c["colour"] is None for c in m.clips)


def test_a_bake_that_would_lose_accuracy_is_warned_about(show):
    load, _, _ = show
    m = manifest_with(A001_C001_1012AB={})
    summary = run_grade(load(colour__apply="baked", colour__bake_size=17), m, DAY)  # the show LUT here has 9 points
    assert not any("bake_size" in w for w in summary.warnings)  # 17 is 2 x (9 - 1) + 1, so it lines up
    summary = run_grade(load(colour__apply="baked", colour__bake_size=33, colour__show_lut=str(
        write_cube(Path(load().colour.show_lut).with_name("big.cube"), 65, identity))), m, DAY)
    assert any("bake_size 33 is smaller than" in w for w in summary.warnings)


def test_an_unreadable_show_lut_stops_the_run_because_every_clip_depends_on_it(show):
    load, _, _ = show
    config = load(colour__apply="baked")
    config.colour.show_lut.write_text("LUT_3D_SIZE 2\nnot numbers\n")
    with pytest.raises(GradeAbort, match="show_rec709_v3.cube"):
        run_grade(config, manifest_with(A001_C001_1012AB={}), DAY)


def test_an_unreadable_default_cdl_is_flagged_too(show):
    load, cdl_dir, _ = show
    (cdl_dir / "000.cdl").write_text("<ColorDecisionList><broken")
    m = manifest_with(A001_C001_1012AB={})
    run_grade(load(), m, DAY)
    clip = m.clip("A001_C001_1012AB")
    assert clip["flags"] == ["cdl_unreadable"] and "default" in clip["colour"]["note"]


def test_a_bake_size_that_does_not_line_up_with_the_lut_is_warned_about(show):
    load, _, _ = show
    summary = run_grade(load(colour__apply="baked", colour__bake_size=20), manifest_with(A001_C001_1012AB={}), DAY)
    assert any("does not line up" in w and "use 17 or 9" in w for w in summary.warnings)


def test_a_show_lut_that_vanishes_mid_run_stops_the_run(show):
    load, _, _ = show
    config = load()
    config.colour.show_lut.unlink()
    with pytest.raises(GradeAbort, match="cannot read"):
        run_grade(config, manifest_with(A001_C001_1012AB={}), DAY)
