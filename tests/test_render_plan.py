# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Render plan: one decode, one filter graph, several outputs. Built entirely
from the config and one manifest clip, nothing is executed."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from dailies.commands.base import Command, Pipeline
from dailies.commands.render import BuildError, build_render_plan
from dailies.config import load_config
from dailies.naming import DayContext

DAY = DayContext(show="TST", day="2026-10-12", day_code="SD014", unit="MU")
CLIP_ID = "A001_C003_1012AB"


@pytest.fixture
def clip() -> dict:
    return {
        "id": CLIP_ID,
        "status": "graded",
        "mob_id": "urn:smpte:umid:060a2b34.01010105.01010f20.13000000.434b3488.8c644114.8231333c.f00dea50",
        "source": {"path": f"/Volumes/RAID/CAMERA/A001/{CLIP_ID}.RDC/{CLIP_ID}_001.R3D",
                   "size_bytes": 48211234567, "mhl_verified": True},
        "picture": {"camera": "A", "roll": "001", "clip": "C003", "reel": "A001", "width": 8192, "height": 4320,
                    "fps": 50.0, "frames": 1732, "start_tc": "10:14:22:31", "end_tc": "10:14:57:13",
                    "anamorphic": 1.0, "iso": 800, "kelvin": 5600, "tint": 0.0, "scene": "14A", "take": "3"},
        "sound": {"status": "matched", "files": ["/Volumes/RAID/SOUND/SD001/14A-03.wav"], "soundroll": "SD001",
                  "start_tc": "10:14:20:00", "offset_samples": 113280, "channels": 8,
                  "track_names": ["MIX L", "MIX R"]},
        "colour": {"apply": "baked", "baked_lut": "/show/cache/luts/9f3c1b.cube", "lut_hash": "9f3c1b"},
        "framing": None, "outputs": [], "flags": [], "errors": [],
    }


@pytest.fixture
def cfg(load_valid):
    return load_valid()


def ffmpeg_of(plan) -> Command:
    last = plan.runnables[-1]
    return last.stages[-1] if isinstance(last, Pipeline) else last


def graph_of(plan) -> str:
    argv = ffmpeg_of(plan).argv
    return argv[argv.index("-filter_complex") + 1]


def output_args(plan, filename: str) -> tuple[str, ...]:
    """Arguments belonging to the output whose temp path ends with filename."""
    argv = ffmpeg_of(plan).argv
    end = next(i for i, a in enumerate(argv) if a.endswith(filename + ".partial"))
    start = max(i for i in range(end) if argv[i] == "-map" and (i < 2 or argv[i - 2] != "-map"))
    return argv[start:end + 1]


# ------------------------------------------------------------ preconditions


def test_clip_must_be_synced_first(cfg, clip):
    clip["sound"] = None
    with pytest.raises(BuildError, match="sync"):
        build_render_plan(cfg, DAY, clip)


def test_clip_must_be_graded_first(cfg, clip):
    clip["colour"] = None
    with pytest.raises(BuildError, match="grade"):
        build_render_plan(cfg, DAY, clip)


def test_wrong_fps_is_never_conformed(cfg, clip):
    clip["picture"]["fps"] = 25.0
    with pytest.raises(BuildError, match="25"):
        build_render_plan(cfg, DAY, clip)


def test_unknown_target_is_rejected(cfg, clip):
    with pytest.raises(BuildError, match="proxy"):
        build_render_plan(cfg, DAY, clip, targets=("proxy",))


def test_sound_spread_over_several_files_is_not_supported_yet(cfg, clip):
    clip["sound"]["files"].append("/Volumes/RAID/SOUND/SD001/14A-03b.wav")
    with pytest.raises(BuildError, match="one sound file"):
        build_render_plan(cfg, DAY, clip)


def test_mix_track_missing_from_the_file_fails_the_clip(cfg, clip):
    clip["sound"]["channels"] = 1
    with pytest.raises(BuildError, match="track 2"):
        build_render_plan(cfg, DAY, clip)


def test_the_plan_does_not_mutate_the_clip(cfg, clip):
    before = copy.deepcopy(clip)
    build_render_plan(cfg, DAY, clip)
    assert clip == before


# --------------------------------------------------------- decode transport


def test_file_transport_runs_the_decoder_first_then_ffmpeg(cfg, clip):
    plan = build_render_plan(cfg, DAY, clip)
    assert len(plan.runnables) == 2
    assert plan.runnables[0].argv[0] == str(cfg.tools.redline.path)
    assert plan.runnables[1].argv[0] == str(cfg.tools.ffmpeg.path)
    assert plan.intermediates == (cfg.paths.scratch / CLIP_ID,)


def test_pipe_transport_is_one_pipeline(valid_config, write_config, mutate, clip):
    piped = load_config(write_config(mutate(valid_config, "decode.transport", "pipe")))
    plan = build_render_plan(piped, DAY, clip)
    assert len(plan.runnables) == 1
    assert isinstance(plan.runnables[0], Pipeline)
    assert "pipe:0" in ffmpeg_of(plan).argv


def test_decode_size_follows_the_clip_not_the_nominal_resolution(cfg, clip):
    clip["picture"].update(width=7680, height=4320)
    plan = build_render_plan(cfg, DAY, clip)
    assert plan.framing.decode == (1920, 1080)
    assert plan.framing.scale == (1920, 1080)


# ------------------------------------------------------------ ffmpeg shape


def test_ffmpeg_preamble_and_inputs(cfg, clip):
    argv = ffmpeg_of(build_render_plan(cfg, DAY, clip)).argv
    assert argv[:4] == (str(cfg.tools.ffmpeg.path), "-hide_banner", "-nostdin", "-y")
    assert argv[4:6] == ("-framerate", "50")
    assert argv.count("-i") == 2
    assert "/Volumes/RAID/SOUND/SD001/14A-03.wav" in argv


def test_rate_is_set_on_the_input_and_never_on_an_output(cfg, clip):
    argv = ffmpeg_of(build_render_plan(cfg, DAY, clip)).argv
    assert "-r" not in argv
    first_map = argv.index("-map")
    assert "-framerate" in argv[:first_map]
    assert "-framerate" not in argv[first_map:]


def test_every_video_output_passes_frames_through_untouched(cfg, clip):
    plan = build_render_plan(cfg, DAY, clip)
    for name in (f"{CLIP_ID}_V.mxf", f"{CLIP_ID}.mp4"):
        args = output_args(plan, name)
        assert args[args.index("-fps_mode") + 1] == "passthrough"


def test_every_video_output_is_tagged_legal_range_bt709(cfg, clip):
    plan = build_render_plan(cfg, DAY, clip)
    for name in (f"{CLIP_ID}_V.mxf", f"{CLIP_ID}.mp4"):
        args = output_args(plan, name)
        for flag, value in (("-color_primaries", "bt709"), ("-color_trc", "bt709"), ("-colorspace", "bt709"),
                            ("-color_range", "tv")):
            assert args[args.index(flag) + 1] == value


# ------------------------------------------------------------- video graph


def test_video_chain_order_lut_then_crop_then_scale_then_pad_then_split(cfg, clip):
    graph = graph_of(build_render_plan(cfg, DAY, clip))
    assert graph.startswith(
        "[0:v]format=rgb48le,"
        "lut3d=file='/show/cache/luts/9f3c1b.cube':interp=tetrahedral,"
        "crop=1944:1026:52:27,"
        "scale=1920:1012:flags=lanczos:out_color_matrix=bt709:out_range=tv,"
        "format=yuv422p10le,"
        "pad=1920:1080:0:34:color=black,"
        "split=2[v_avid][v_review];"
    )


def test_lut_is_tetrahedral_and_applied_exactly_once(cfg, clip):
    graph = graph_of(build_render_plan(cfg, DAY, clip))
    assert graph.count("lut3d=") == 1
    assert "interp=tetrahedral" in graph
    assert "trilinear" not in graph


def test_nothing_drops_to_eight_bit_before_the_lut(cfg, clip):
    graph = graph_of(build_render_plan(cfg, DAY, clip))
    assert graph.index("format=rgb48le") < graph.index("lut3d=") < graph.index("format=yuv422p10le")


def test_burn_ins_are_drawn_after_the_scale(cfg, clip):
    graph = graph_of(build_render_plan(cfg, DAY, clip))
    assert graph.index("scale=1920") < graph.index("drawtext=")
    assert graph.count("drawtext=") == 8  # four fields on each of two deliverables


def test_burn_in_size_follows_the_output_height(cfg, clip):
    graph = graph_of(build_render_plan(cfg, DAY, clip))
    assert "fontsize=27" in graph  # 1080 * 0.025
    assert ":x=22:y=22" in graph  # 1080 * 0.02, rounded


def test_source_timecode_burn_in_counts_at_50(cfg, clip):
    graph = graph_of(build_render_plan(cfg, DAY, clip))
    assert r"text='SRC ':expansion=none" in graph
    assert r"timecode='10\:14\:22\:31':timecode_rate=50" in graph


def test_clean_deliverable_has_no_burn_in(valid_config, write_config, mutate, clip):
    clean = load_config(write_config(mutate(valid_config, "deliverables.review.burn_in", False)))
    plan = build_render_plan(clean, DAY, clip)
    assert graph_of(plan).count("drawtext=") == 4
    args = output_args(plan, f"{CLIP_ID}.mp4")
    assert args[:2] == ("-map", "[v_review]")


def test_single_target_needs_no_split(cfg, clip):
    graph = graph_of(build_render_plan(cfg, DAY, clip, targets=("review",)))
    assert "split=" not in graph.split(";")[0]
    assert "asplit" not in graph
    assert "[v_avid]" not in graph


def test_anamorphic_clip_is_desqueezed_and_reported(cfg, clip):
    clip["picture"].update(width=8192, height=6864, anamorphic=2.0)
    plan = build_render_plan(cfg, DAY, clip)
    assert "scale=4096:1716:flags=lanczos,crop=" in graph_of(plan)
    assert any("anamorphic" in w for w in plan.warnings)


# ------------------------------------------------------------- audio graph


def test_audio_is_synced_once_then_split_per_output(cfg, clip):
    graph = graph_of(build_render_plan(cfg, DAY, clip))
    assert (
        "[1:a]atrim=start_sample=113280,asetpts=N/SR/TB,apad=whole_len=1662720,atrim=end_sample=1662720,"
        "asplit=3[a_avid_1][a_avid_2][a_review];"
    ) in graph
    assert "[a_avid_1]pan=mono|c0=c0[avid_a1]" in graph
    assert "[a_avid_2]pan=mono|c0=c1[avid_a2]" in graph
    assert "[a_review]pan=stereo|c0=c0|c1=c1[review_a]" in graph


def test_audio_length_is_exactly_the_picture_length_in_samples(cfg, clip):
    clip["picture"]["frames"] = 7
    assert "apad=whole_len=6720,atrim=end_sample=6720" in graph_of(build_render_plan(cfg, DAY, clip))


@pytest.mark.parametrize("status", ["partial_overlap", "multiple_candidates"])
def test_flagged_but_usable_sound_is_still_rendered(cfg, clip, status):
    clip["sound"]["status"] = status
    assert "[1:a]" in graph_of(build_render_plan(cfg, DAY, clip))


@pytest.mark.parametrize("status", ["no_sound", "tc_implausible", "mos"])
def test_unsynced_clips_render_mute(cfg, clip, status):
    clip["sound"] = {"status": status}
    plan = build_render_plan(cfg, DAY, clip)
    argv = ffmpeg_of(plan).argv
    assert argv.count("-i") == 1
    assert "[1:a]" not in graph_of(plan)
    assert [o.kind for o in plan.outputs] == ["video", "video"]
    assert "-an" in output_args(plan, f"{CLIP_ID}.mp4")


# ----------------------------------------------------------------- outputs


def test_avid_video_output(cfg, clip):
    args = output_args(build_render_plan(cfg, DAY, clip), f"{CLIP_ID}_V.mxf")
    assert args[:2] == ("-map", "[avid_v]")
    for flag, value in (("-c:v", "dnxhd"), ("-profile:v", "dnxhr_lb"), ("-pix_fmt", "yuv422p"),
                        ("-timecode", "10:14:22:31"), ("-f", "mxf_opatom")):
        assert args[args.index(flag) + 1] == value
    assert "reel_name=A001_C003_1012AB" in args
    assert "material_package_name=14A-3A" in args


def test_avid_audio_is_one_mono_op_atom_file_per_mix_track_at_the_picture_edit_rate(cfg, clip):
    plan = build_render_plan(cfg, DAY, clip)
    for index in (1, 2):
        args = output_args(plan, f"{CLIP_ID}_A{index}.mxf")
        assert args[:2] == ("-map", f"[avid_a{index}]")
        for flag, value in (("-c:a", "pcm_s24le"), ("-ar", "48000"), ("-ac", "1"),
                            ("-mxf_audio_edit_rate", "50/1"), ("-timecode", "10:14:22:31"), ("-f", "mxf_opatom")):
            assert args[args.index(flag) + 1] == value
        assert "reel_name=A001_C003_1012AB" in args


def test_review_output(cfg, clip):
    args = output_args(build_render_plan(cfg, DAY, clip), f"{CLIP_ID}.mp4")
    assert args[:4] == ("-map", "[review_v]", "-map", "[review_a]")
    for flag, value in (("-c:v", "libx264"), ("-crf", "20"), ("-preset", "medium"), ("-pix_fmt", "yuv420p"),
                        ("-c:a", "aac"), ("-b:a", "192k"), ("-movflags", "+faststart"), ("-f", "mp4")):
        assert args[args.index(flag) + 1] == value


def test_outputs_render_to_a_temp_name_beside_the_final_path(cfg, clip):
    plan = build_render_plan(cfg, DAY, clip)
    day_dir = cfg.paths.output_root / "TST_20261012_SD014_MU"
    finals = {o.final for o in plan.outputs}
    assert finals == {
        day_dir / "avid" / f"{CLIP_ID}_V.mxf", day_dir / "avid" / f"{CLIP_ID}_A1.mxf",
        day_dir / "avid" / f"{CLIP_ID}_A2.mxf", day_dir / "review" / f"{CLIP_ID}.mp4",
    }
    for output in plan.outputs:
        assert output.temp.parent == output.final.parent
        assert output.temp.name == f".{output.final.name}.partial"
        assert str(output.temp) in ffmpeg_of(plan).argv
        assert str(output.final) not in ffmpeg_of(plan).argv


def test_outputs_know_their_target_and_kind(cfg, clip):
    plan = build_render_plan(cfg, DAY, clip)
    assert [(o.target, o.kind) for o in plan.outputs] == [
        ("avid", "video"), ("avid", "audio"), ("avid", "audio"), ("review", "video"),
    ]


def test_every_runnable_uses_pinned_absolute_tools(cfg, clip):
    for runnable in build_render_plan(cfg, DAY, clip).runnables:
        for command in runnable.commands():
            assert Path(command.argv[0]).is_absolute()


# ----------------------------------------------------------- settings hash


def test_settings_hash_is_stable(cfg, clip):
    first = build_render_plan(cfg, DAY, clip).settings_hashes
    second = build_render_plan(cfg, DAY, copy.deepcopy(clip)).settings_hashes
    assert first == second
    assert set(first) == {"avid", "review"}
    assert first["avid"] != first["review"]


def test_regrade_changes_every_target(cfg, clip):
    before = build_render_plan(cfg, DAY, clip).settings_hashes
    clip["colour"]["lut_hash"] = "aaaaaa"
    after = build_render_plan(cfg, DAY, clip).settings_hashes
    assert before["avid"] != after["avid"] and before["review"] != after["review"]


def test_changing_one_deliverable_rerenders_only_that_deliverable(valid_config, write_config, mutate, cfg, clip):
    before = build_render_plan(cfg, DAY, clip).settings_hashes
    changed = load_config(write_config(mutate(valid_config, "deliverables.review.crf", 18)))
    after = build_render_plan(changed, DAY, clip).settings_hashes
    assert before["avid"] == after["avid"]
    assert before["review"] != after["review"]


def test_resync_changes_the_hash(cfg, clip):
    before = build_render_plan(cfg, DAY, clip).settings_hashes
    clip["sound"]["offset_samples"] += 960
    assert build_render_plan(cfg, DAY, clip).settings_hashes["avid"] != before["avid"]


def test_tool_paths_and_versions_do_not_change_the_hash(valid_config, write_config, mutate, cfg, clip):
    before = build_render_plan(cfg, DAY, clip).settings_hashes
    bumped = load_config(write_config(mutate(valid_config, "tools.ffmpeg.version", "7.2")))
    assert build_render_plan(bumped, DAY, clip).settings_hashes == before


def test_moving_the_media_does_not_change_the_hash(cfg, clip):
    before = build_render_plan(cfg, DAY, clip).settings_hashes
    clip["source"]["path"] = clip["source"]["path"].replace("/Volumes/RAID", "/Volumes/RAID2")
    clip["sound"]["files"] = ["/Volumes/RAID2/SOUND/SD001/14A-03.wav"]
    assert build_render_plan(cfg, DAY, clip).settings_hashes == before


def test_unrelated_metadata_does_not_change_the_hash(cfg, clip):
    before = build_render_plan(cfg, DAY, clip).settings_hashes
    clip["picture"]["iso"] = 1600
    clip["flags"].append("short_clip")
    assert build_render_plan(cfg, DAY, clip).settings_hashes == before


# ------------------------------------------------- failures become BuildError


def test_framing_failure_fails_the_clip_not_the_batch(cfg, clip):
    clip["picture"].update(width=4096, height=3072)  # 4:3 cannot be fitted by width into 16:9
    with pytest.raises(BuildError, match=f"{CLIP_ID}: .*taller"):
        build_render_plan(cfg, DAY, clip)


def test_unusable_burn_in_text_fails_the_clip(cfg, clip):
    clip["picture"]["scene"] = "14A\nINJECTED"
    with pytest.raises(BuildError, match=f"{CLIP_ID}: line break"):
        build_render_plan(cfg, DAY, clip)


# ------------------------------------------------------- exact colour mode

GRADE = {"slope": [1.1, 1.0, 0.9], "offset": [0.02, 0.0, -0.01], "power": [1.05, 1.0, 0.95], "sat": 0.9}


def exact(clip, **colour):
    clip["colour"] = {"apply": "exact", "lut_hash": "sha256:ab", "show_lut": "/show/luts/show_rec709_v3.cube", **colour}
    return clip


def test_exact_mode_applies_the_cdl_then_the_untouched_show_lut(cfg, clip):
    graph = graph_of(build_render_plan(cfg, DAY, exact(clip, cdl=GRADE)))
    assert graph.startswith("[0:v]format=rgb48le,lutrgb=r='clip(pow(max(val/65535*1.1+(0.02),0),1.05),0,1)*65535'")
    assert graph.index("lutrgb=") < graph.index("colorchannelmixer=") < graph.index("lut3d=") < graph.index("crop=")
    assert "lut3d=file='/show/luts/show_rec709_v3.cube':interp=tetrahedral" in graph
    assert graph.count("lut3d=") == 1


def test_exact_mode_with_a_neutral_or_absent_cdl_is_just_the_show_lut(cfg, clip):
    neutral = {"slope": [1, 1, 1], "offset": [0, 0, 0], "power": [1, 1, 1], "sat": 1.0}
    for colour in ({"cdl": neutral}, {"cdl": None}, {}):
        graph = graph_of(build_render_plan(cfg, DAY, exact(clip, **colour)))
        assert "lutrgb" not in graph and "colorchannelmixer" not in graph
        assert graph.startswith("[0:v]format=rgb48le,lut3d=file='/show/luts/show_rec709_v3.cube':interp=tetrahedral,crop=")


def test_exact_mode_puts_the_camera_look_between_the_cdl_and_the_show_lut(cfg, clip):
    graph = graph_of(build_render_plan(cfg, DAY, exact(clip, cdl=GRADE, camera_look="/show/luts/cam_A_v2.cube")))
    assert graph.index("lutrgb=") < graph.index("cam_A_v2.cube") < graph.index("show_rec709_v3.cube")
    assert graph.count("interp=tetrahedral") == 2 and "trilinear" not in graph


def test_exact_mode_keeps_sixteen_bit_rgb_until_after_the_scale(cfg, clip):
    graph = graph_of(build_render_plan(cfg, DAY, exact(clip, cdl=GRADE)))
    assert graph.index("format=rgb48le") < graph.index("lutrgb=") < graph.index("scale=1920") < graph.index("format=yuv422p10le")


def test_the_apply_mode_is_part_of_the_settings_hash(cfg, clip):
    baked = build_render_plan(cfg, DAY, clip).settings_hashes
    clip["colour"] = {"apply": "exact", "lut_hash": clip["colour"]["lut_hash"], "show_lut": "/show/luts/show.cube"}
    assert build_render_plan(cfg, DAY, clip).settings_hashes["avid"] != baked["avid"]


def test_a_damaged_cdl_in_the_manifest_fails_the_clip(cfg, clip):
    with pytest.raises(BuildError, match="colour"):
        build_render_plan(cfg, DAY, exact(clip, cdl={"slope": [1, 1], "offset": [0, 0, 0], "power": [1, 1, 1], "sat": 1}))
