# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Show config loading. Fail fast, report every problem at once, no defaults
that hide a missing decision."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dailies.config import ConfigError, ShowConfig, load_config


def load(write_config, data, **kwargs) -> ShowConfig:
    return load_config(write_config(data), **kwargs)


def errors_of(write_config, data, **kwargs) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        load(write_config, data, **kwargs)
    return caught.value.problems


# ---------------------------------------------------------------- happy path


def test_valid_config_loads(write_config, valid_config):
    cfg = load(write_config, valid_config)
    assert cfg.show.fps == 50
    assert cfg.show.code == "TST"
    assert cfg.framing.crop_percent == 5.0
    assert cfg.deliverables["avid"].codec == "dnxhr_lb"
    assert cfg.deliverables["review"].crf == 20
    assert cfg.paths.output_root == Path(valid_config["paths"]["output_root"])


def test_samples_per_frame_is_exact(write_config, valid_config):
    assert load(write_config, valid_config).show.samples_per_frame == 960


def test_config_is_immutable(write_config, valid_config):
    cfg = load(write_config, valid_config)
    with pytest.raises(AttributeError):
        cfg.show.fps = 25  # type: ignore[misc]


def test_camera_look_absent_on_purpose_is_none(write_config, valid_config):
    cfg = load(write_config, valid_config)
    assert cfg.colour.camera_looks["B"] is None
    assert cfg.colour.camera_looks["A"].name == "cam_A_v2.cube"


def test_output_transform_none_means_no_transform(write_config, valid_config):
    assert load(write_config, valid_config).colour.output_transform is None


def test_output_transform_name_is_kept(write_config, valid_config, mutate):
    data = mutate(valid_config, "colour.output_transform", "Rec709_BT1886")
    assert load(write_config, data).colour.output_transform == "Rec709_BT1886"


# ---------------------------------------------------------------------- hash


def test_hash_is_stable_across_key_order_and_comments(write_config, valid_config, tmp_path):
    first = load(write_config, valid_config)
    reordered = dict(reversed(list(valid_config.items())))
    path = write_config(reordered, "reordered.yaml")
    path.write_text("# a comment changes nothing\n" + path.read_text())
    assert load_config(path).config_hash == first.config_hash


def test_hash_changes_when_a_value_changes(write_config, valid_config, mutate):
    first = load(write_config, valid_config)
    second = load(write_config, mutate(valid_config, "framing.crop_percent", 4.0))
    assert first.config_hash != second.config_hash
    assert first.config_hash.startswith("sha256:")


# ------------------------------------------------------------- file problems


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml_is_a_config_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("show: [unclosed\n")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(path)


def test_top_level_must_be_a_mapping(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- one\n- two\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(path)


# --------------------------------------------------------- structural checks


@pytest.mark.parametrize(
    "section",
    ["show", "tools", "framing", "colour", "decode", "sound", "scan", "deliverables", "burn_in", "naming", "paths",
     "limits"],
)
def test_every_section_is_required(write_config, valid_config, mutate, section):
    problems = errors_of(write_config, mutate(valid_config, section, remove=True))
    assert any(section in p and "missing" in p for p in problems)


def test_unknown_key_is_rejected_with_its_path(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "framing.crop_percentage", 5.0))
    assert "framing.crop_percentage: unknown key" in problems


def test_unknown_top_level_section_is_rejected(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "colour_management", {}))
    assert "colour_management: unknown key" in problems


def test_wrong_type_is_reported_with_its_path(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "limits.decode_workers", "two"))
    assert any(p.startswith("limits.decode_workers:") and "integer" in p for p in problems)


def test_boolean_is_not_accepted_as_a_number(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "limits.decode_workers", True))
    assert any(p.startswith("limits.decode_workers:") for p in problems)


def test_all_problems_are_reported_together(write_config, valid_config, mutate):
    data = mutate(valid_config, "show.fps", 25)
    data = mutate(data, "limits.encode_workers", 0)
    data = mutate(data, "framing.fit", "stretch")
    problems = errors_of(write_config, data)
    assert len(problems) == 3


def test_error_message_lists_the_problems(write_config, valid_config, mutate):
    with pytest.raises(ConfigError) as caught:
        load(write_config, mutate(valid_config, "framing.fit", "stretch"))
    assert "framing.fit" in str(caught.value)


# ----------------------------------------------------------- show constants


def test_fps_must_be_50(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "show.fps", 25))
    assert any(p.startswith("show.fps:") for p in problems)


def test_audio_rate_must_divide_evenly_by_fps(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "show.audio_rate", 48001))
    assert any(p.startswith("show.audio_rate:") for p in problems)


def test_drop_frame_is_rejected(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "show.timecode", "drop"))
    assert any(p.startswith("show.timecode:") for p in problems)


@pytest.mark.parametrize("cameras", [[], ["A", "A"], ["AB"], ["a"], [1]])
def test_camera_letters_must_be_unique_single_capitals(write_config, valid_config, mutate, cameras):
    problems = errors_of(write_config, mutate(valid_config, "show.cameras", cameras))
    assert any(p.startswith("show.cameras") for p in problems)


def test_show_code_must_be_safe_for_folder_names(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "show.code", "MY SHOW/1"))
    assert any(p.startswith("show.code:") for p in problems)


# -------------------------------------------------------------------- tools


def test_tool_path_must_be_absolute(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "tools.ffmpeg.path", "ffmpeg"))
    assert any(p.startswith("tools.ffmpeg.path:") and "absolute" in p for p in problems)


def test_tool_must_exist(write_config, valid_config, mutate, show_tree):
    problems = errors_of(write_config, mutate(valid_config, "tools.redline.path", str(show_tree / "bin/missing")))
    assert any(p.startswith("tools.redline.path:") and "not found" in p for p in problems)


def test_tool_must_be_executable(write_config, valid_config, show_tree):
    (show_tree / "bin/ffprobe").chmod(0o644)
    problems = errors_of(write_config, valid_config)
    assert any(p.startswith("tools.ffprobe.path:") and "executable" in p for p in problems)


def test_tool_version_must_be_pinned(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "tools.ffmpeg.version", ""))
    assert any(p.startswith("tools.ffmpeg.version:") for p in problems)


# ------------------------------------------------------------------ framing


def test_crop_order_other_than_crop_then_scale_is_rejected(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "framing.crop_order", "scale_then_crop"))
    assert any(p.startswith("framing.crop_order:") for p in problems)


def test_framing_that_would_squeeze_fails_at_load(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "framing.fit", "exact"))
    assert any(p.startswith("deliverables.avid:") and "aspect" in p for p in problems)
    assert any(p.startswith("deliverables.review:") and "aspect" in p for p in problems)


def test_nominal_framing_is_available_per_deliverable(write_config, valid_config):
    cfg = load(write_config, valid_config)
    framing = cfg.nominal_framing("avid", "A")
    assert framing.crop == (1944, 1026)
    assert framing.scale == (1920, 1012)


# ------------------------------------------------------------------- colour


def test_every_camera_needs_a_look_entry(write_config, valid_config, mutate):
    data = mutate(valid_config, "colour.camera_looks", {"A": valid_config["colour"]["camera_looks"]["A"]})
    problems = errors_of(write_config, data)
    assert "colour.camera_looks.B: missing, use null if this camera deliberately has no look" in problems


def test_look_for_an_undeclared_camera_is_rejected(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "colour.camera_looks.Z", None))
    assert any(p.startswith("colour.camera_looks.Z:") for p in problems)


@pytest.mark.parametrize("key", ["colour.ocio_config", "colour.show_lut", "colour.camera_looks.A", "burn_in.font"])
def test_referenced_files_must_exist(write_config, valid_config, mutate, key, show_tree):
    problems = errors_of(write_config, mutate(valid_config, key, str(show_tree / "nowhere.cube")))
    assert any(p.startswith(key + ":") and "not found" in p for p in problems)


def test_output_transform_must_be_stated(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "colour.output_transform", remove=True))
    assert any(p.startswith("colour.output_transform:") and "missing" in p for p in problems)


@pytest.mark.parametrize("size", [1, 16, 300])
def test_bake_size_range(write_config, valid_config, mutate, size):
    problems = errors_of(write_config, mutate(valid_config, "colour.bake_size", size))
    assert any(p.startswith("colour.bake_size:") for p in problems)


# ------------------------------------------------------------------- decode


def test_unknown_decode_engine_is_rejected(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "decode.engine", "resolve"))
    assert any(p.startswith("decode.engine:") for p in problems)


def test_redline_args_may_only_use_known_placeholders(write_config, valid_config, mutate):
    data = mutate(valid_config, "decode.redline.decode_args", ["--i", "{clip}", "--gpu", "{gpu_index}"])
    problems = errors_of(write_config, data)
    assert any(p.startswith("decode.redline.decode_args:") and "gpu_index" in p for p in problems)


def test_redline_metadata_args_must_reference_the_clip(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "decode.redline.metadata_args", ["--printMeta", "1"]))
    assert any(p.startswith("decode.redline.metadata_args:") and "{clip}" in p for p in problems)


@pytest.mark.parametrize("value", [[8192], [8192, -1], "8K", [8192.5, 4320]])
def test_expected_resolution_is_two_positive_integers_per_camera(write_config, valid_config, mutate, value):
    problems = errors_of(write_config, mutate(valid_config, "decode.expected_resolution.A", value))
    assert any(p.startswith("decode.expected_resolution.A:") for p in problems)


def test_every_camera_needs_an_expected_resolution_and_a_source_kind(write_config, valid_config, mutate):
    data = mutate(valid_config, "decode.expected_resolution.B", remove=True)
    data = mutate(data, "decode.sources.B", remove=True)
    problems = errors_of(write_config, data)
    assert "decode.expected_resolution.B: missing" in problems and "decode.sources.B: missing" in problems


def test_source_kind_is_r3d_or_prores(write_config, valid_config, mutate):
    assert load(write_config, mutate(valid_config, "decode.sources.B", "prores")).decode.sources == {"A": "r3d", "B": "prores"}
    problems = errors_of(write_config, mutate(valid_config, "decode.sources.B", "xavc"))
    assert any(p.startswith("decode.sources.B:") for p in problems)


def test_settings_for_a_camera_the_show_does_not_have_are_rejected(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "decode.sources.Z", "r3d"))
    assert any(p.startswith("decode.sources.Z:") for p in problems)


def test_prores_is_framed_at_full_resolution_and_r3d_at_the_decode_resolution(write_config, valid_config, mutate):
    data = mutate(valid_config, "decode.sources.B", "prores")
    data = mutate(data, "decode.expected_resolution.B", [4096, 2160])
    cfg = load(write_config, data)
    assert cfg.nominal_framing("avid", "A").decode == (2048, 1080)
    assert cfg.nominal_framing("avid", "B").decode == (4096, 2160)
    assert cfg.nominal_framing("avid", "B").crop == (3890, 2052)
    assert cfg.nominal_framing("avid", "B").scale == (1920, 1012)


def test_a_camera_whose_picture_would_be_squeezed_fails_at_load(write_config, valid_config, mutate):
    data = mutate(valid_config, "decode.expected_resolution.B", [4096, 3072])  # 4:3 cannot be fitted by width
    problems = errors_of(write_config, data)
    assert any(p.startswith("deliverables.avid:") and "camera B" in p for p in problems)


# -------------------------------------------------------------------- sound


@pytest.mark.parametrize("tracks", [[], [0], [1, 1], ["L"]])
def test_mix_tracks_are_unique_one_based_integers(write_config, valid_config, mutate, tracks):
    problems = errors_of(write_config, mutate(valid_config, "sound.mix_tracks", tracks))
    assert any(p.startswith("sound.mix_tracks") for p in problems)


# ------------------------------------------------------------- deliverables


def test_at_least_one_deliverable(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "deliverables", {}))
    assert any(p.startswith("deliverables:") for p in problems)


def test_odd_deliverable_dimensions_are_rejected(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "deliverables.review.height", 1081))
    assert any(p.startswith("deliverables.review.height:") and "even" in p for p in problems)


def test_codec_and_container_must_agree(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "deliverables.avid.container", "mp4"))
    assert any(p.startswith("deliverables.avid.container:") for p in problems)


def test_h264_requires_a_crf(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "deliverables.review.crf", remove=True))
    assert any(p.startswith("deliverables.review.crf:") for p in problems)


def test_dnxhr_does_not_take_a_crf(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "deliverables.avid.crf", 20))
    assert any(p.startswith("deliverables.avid.crf:") for p in problems)


@pytest.mark.parametrize("audio", ["mp3", "aac", "aac_0k", "pcm_s8"])
def test_unknown_audio_format_is_rejected(write_config, valid_config, mutate, audio):
    problems = errors_of(write_config, mutate(valid_config, "deliverables.review.audio", audio))
    assert any(p.startswith("deliverables.review.audio:") for p in problems)


def test_aac_bitrate_is_parsed(write_config, valid_config):
    review = load(write_config, valid_config).deliverables["review"]
    assert (review.audio_codec, review.audio_bitrate_k) == ("aac", 192)


def test_pcm_has_no_bitrate(write_config, valid_config):
    avid = load(write_config, valid_config).deliverables["avid"]
    assert (avid.audio_codec, avid.audio_bitrate_k) == ("pcm_s24le", None)


# ------------------------------------------------------------------ burn-in


def test_unknown_burn_in_position_is_rejected(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "burn_in.fields.middle", "{clip_name}"))
    assert any(p.startswith("burn_in.fields.middle:") for p in problems)


def test_unknown_burn_in_placeholder_is_rejected(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "burn_in.fields.top_left", "{clipname}"))
    assert any(p.startswith("burn_in.fields.top_left:") and "clipname" in p for p in problems)


def test_running_timecode_must_end_its_field(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "burn_in.fields.bottom_left", "{source_tc} SRC"))
    assert any(p.startswith("burn_in.fields.bottom_left:") and "end" in p for p in problems)


@pytest.mark.parametrize("ratio", [0.0, -0.1, 0.5])
def test_size_ratio_range(write_config, valid_config, mutate, ratio):
    problems = errors_of(write_config, mutate(valid_config, "burn_in.size_ratio", ratio))
    assert any(p.startswith("burn_in.size_ratio:") for p in problems)


# ------------------------------------------------------------------- naming


def test_unknown_naming_placeholder_is_rejected(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "naming.day_folder", "{show}_{weekday}"))
    assert any(p.startswith("naming.day_folder:") and "weekday" in p for p in problems)


def test_fallback_clip_name_may_not_depend_on_slate_metadata(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "naming.avid_clip_fallback", "{scene}-{take}"))
    assert any(p.startswith("naming.avid_clip_fallback:") for p in problems)


# -------------------------------------------------------------------- paths


@pytest.mark.parametrize("key", ["camera_root", "sound_root", "output_root", "scratch", "manifest_dir", "log_dir"])
def test_path_folders_must_exist(write_config, valid_config, mutate, key, show_tree):
    problems = errors_of(write_config, mutate(valid_config, f"paths.{key}", str(show_tree / "absent")))
    assert any(p.startswith(f"paths.{key}:") and "not found" in p for p in problems)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_output_folders_must_be_writable(write_config, valid_config, show_tree):
    target = show_tree / "dailies"
    target.chmod(0o555)
    try:
        problems = errors_of(write_config, valid_config)
    finally:
        target.chmod(0o755)
    assert any(p.startswith("paths.output_root:") and "writable" in p for p in problems)


def test_lut_cache_must_be_writable_folder(write_config, valid_config, mutate, show_tree):
    problems = errors_of(write_config, mutate(valid_config, "colour.lut_cache", str(show_tree / "absent")))
    assert any(p.startswith("colour.lut_cache:") for p in problems)


def test_path_checks_can_be_skipped_for_a_dry_run(write_config, valid_config, mutate):
    data = mutate(valid_config, "paths.output_root", "/Volumes/NOT_MOUNTED/DAILIES")
    cfg = load(write_config, data, check_paths=False)
    assert cfg.paths.output_root == Path("/Volumes/NOT_MOUNTED/DAILIES")


def test_skipping_path_checks_does_not_skip_value_checks(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "show.fps", 25), check_paths=False)
    assert any(p.startswith("show.fps:") for p in problems)


# ------------------------------------------------------------------- limits


@pytest.mark.parametrize("key", ["decode_workers", "encode_workers"])
def test_worker_counts_are_positive(write_config, valid_config, mutate, key):
    problems = errors_of(write_config, mutate(valid_config, f"limits.{key}", 0))
    assert any(p.startswith(f"limits.{key}:") for p in problems)


def test_deliverables_must_share_one_raster_in_this_build(write_config, valid_config, mutate):
    data = mutate(valid_config, "deliverables.review.width", 1280)
    data = mutate(data, "deliverables.review.height", 720)
    problems = errors_of(write_config, data)
    assert any(p.startswith("deliverables:") and "same width and height" in p for p in problems)


# ------------------------------------------------------- type errors, continued


def test_section_must_be_a_mapping(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "limits", [2, 8]))
    assert "limits: expected a mapping" in problems


def test_number_must_be_a_number(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "framing.crop_percent", "five"))
    assert any(p.startswith("framing.crop_percent:") and "number" in p for p in problems)


def test_boolean_must_be_a_boolean(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "deliverables.avid.burn_in", "yes"))
    assert any(p.startswith("deliverables.avid.burn_in:") and "true or false" in p for p in problems)


def test_list_must_be_a_list(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "show.units", "MU"))
    assert "show.units: expected a list" in problems


def test_malformed_template_is_reported(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "naming.day_folder", "{show"))
    assert any(p.startswith("naming.day_folder:") and "malformed" in p for p in problems)


@pytest.mark.parametrize("args", [[], ["--i", 5], "--i {clip}"])
def test_redline_args_must_be_a_list_of_strings(write_config, valid_config, mutate, args):
    problems = errors_of(write_config, mutate(valid_config, "decode.redline.decode_args", args))
    assert any(p.startswith("decode.redline.decode_args:") for p in problems)


def test_malformed_redline_arg_template_is_reported(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "decode.redline.decode_args", ["--i", "{clip"]))
    assert any(p.startswith("decode.redline.decode_args:") and "malformed" in p for p in problems)


def test_pixel_format_must_suit_the_codec(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "deliverables.avid.pix_fmt", "yuv420p"))
    assert any(p.startswith("deliverables.avid.pix_fmt:") for p in problems)


def test_unknown_codec_is_one_problem_not_a_cascade(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "deliverables.avid.codec", "prores"))
    assert [p for p in problems if p.startswith("deliverables.avid")] == [
        p for p in problems if p.startswith("deliverables.avid.codec:")
    ]
    assert len(problems) == 1


def test_missing_audio_format_is_reported_once(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "deliverables.review.audio", remove=True))
    assert problems == ["deliverables.review.audio: missing"]


# ------------------------------------------------------ decoder engine and tools


def sdk_config(valid_config, mutate, show_tree):
    data = mutate(valid_config, "decode.engine", "sdk")
    data = mutate(data, "decode.transport", "pipe")
    data = mutate(data, "tools.r3dtool", {"path": str(show_tree / "bin/r3dtool"), "version": "0.1.0"})
    return data


def test_sdk_engine_needs_r3dtool_and_not_redline(write_config, valid_config, mutate, show_tree):
    data = sdk_config(valid_config, mutate, show_tree)
    data = mutate(data, "tools.redline", remove=True)
    data = mutate(data, "decode.redline", remove=True)
    cfg = load(write_config, data)
    assert cfg.tools.r3dtool.version == "0.1.0"
    assert cfg.tools.redline is None and cfg.decode.redline is None


def test_sdk_engine_without_r3dtool_is_rejected(write_config, valid_config, mutate, show_tree):
    data = mutate(sdk_config(valid_config, mutate, show_tree), "tools.r3dtool", remove=True)
    assert "tools.r3dtool: missing, required by decode.engine sdk" in errors_of(write_config, data)


def test_redline_engine_without_redline_is_rejected(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "tools.redline", remove=True))
    assert "tools.redline: missing, required by decode.engine redline" in problems


def test_redline_engine_needs_its_flag_templates(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "decode.redline", remove=True))
    assert "decode.redline: missing, required by decode.engine redline" in problems


def test_sdk_engine_only_pipes(write_config, valid_config, mutate, show_tree):
    data = mutate(sdk_config(valid_config, mutate, show_tree), "decode.transport", "file")
    problems = errors_of(write_config, data)
    assert any(p.startswith("decode.transport:") and "pipe" in p for p in problems)


def test_an_unused_tool_is_still_validated_when_present(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "tools.r3dtool", {"path": "r3dtool", "version": "1"}))
    assert any(p.startswith("tools.r3dtool.path:") and "absolute" in p for p in problems)


# ------------------------------------------------------------------- scan


def test_scan_folders_are_templates_under_the_roots(write_config, valid_config):
    cfg = load(write_config, valid_config)
    assert cfg.scan.camera_dir == "{day_folder}/01_CAMERA"


def test_scan_folder_template_rejects_unknown_placeholders(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "scan.camera_dir", "{reel}"))
    assert any(p.startswith("scan.camera_dir:") and "reel" in p for p in problems)


@pytest.mark.parametrize("value", ["/absolute/path", "../outside", "a/../../b"])
def test_scan_folder_must_stay_under_its_root(write_config, valid_config, mutate, value):
    problems = errors_of(write_config, mutate(valid_config, "scan.sound_dir", value))
    assert any(p.startswith("scan.sound_dir:") for p in problems)


# -------------------------------------------------------- sound filename fallback


def test_filename_pattern_may_be_null(write_config, valid_config, mutate):
    assert load(write_config, mutate(valid_config, "sound.filename_pattern", None)).sound.filename_pattern is None


def test_filename_pattern_must_be_stated(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "sound.filename_pattern", remove=True))
    assert "sound.filename_pattern: missing" in problems


def test_filename_pattern_must_compile(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "sound.filename_pattern", "(?P<scene>"))
    assert any(p.startswith("sound.filename_pattern:") and "regular expression" in p for p in problems)


def test_filename_pattern_needs_scene_and_take_groups(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "sound.filename_pattern", "^(?P<scene>\\\\d+)"))
    assert any(p.startswith("sound.filename_pattern:") and "take" in p for p in problems)


# ------------------------------------------------------------------ MOS rule


def test_mos_cameras_must_be_stated_even_if_empty(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "sound.mos_cameras", remove=True))
    assert "sound.mos_cameras: missing" in problems


def test_mos_cameras_are_loaded(write_config, valid_config, mutate):
    assert load(write_config, mutate(valid_config, "sound.mos_cameras", ["B"])).sound.mos_cameras == ("B",)


def test_mos_camera_must_be_a_show_camera(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "sound.mos_cameras", ["Z"]))
    assert any(p.startswith("sound.mos_cameras:") and "Z" in p for p in problems)


# ------------------------------------------------------------------- grade


def test_colour_apply_must_be_stated_because_it_changes_the_picture(write_config, valid_config, mutate):
    assert "colour.apply: missing" in errors_of(write_config, mutate(valid_config, "colour.apply", remove=True))


@pytest.mark.parametrize("mode", ["exact", "baked"])
def test_colour_apply_modes(write_config, valid_config, mutate, mode):
    assert load(write_config, mutate(valid_config, "colour.apply", mode)).colour.apply == mode


def test_unknown_colour_apply_mode(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "colour.apply", "resolve"))
    assert any(p.startswith("colour.apply:") for p in problems)


def test_cdl_search_is_an_ordered_list_of_templates(write_config, valid_config):
    assert load(write_config, valid_config).colour.cdl_search == ("{day_folder}/{clip_name}", "{day_folder}/{scene}",
                                                                  "{clip_name}", "{scene}")


def test_cdl_search_rejects_unknown_placeholders(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "colour.cdl_search", ["{slate}"]))
    assert any(p.startswith("colour.cdl_search:") and "slate" in p for p in problems)


@pytest.mark.parametrize("value", [[], "{scene}", ["/abs/{scene}"], ["../{scene}"], [5]])
def test_cdl_search_must_be_relative_templates(write_config, valid_config, mutate, value):
    problems = errors_of(write_config, mutate(valid_config, "colour.cdl_search", value))
    assert any(p.startswith("colour.cdl_search") for p in problems)


def test_cdl_default_may_be_null_or_a_name(write_config, valid_config, mutate):
    assert load(write_config, valid_config).colour.cdl_default == "000"
    assert load(write_config, mutate(valid_config, "colour.cdl_default", None)).colour.cdl_default is None


def test_cdl_default_must_be_stated(write_config, valid_config, mutate):
    assert "colour.cdl_default: missing" in errors_of(write_config, mutate(valid_config, "colour.cdl_default", remove=True))


def test_cdl_default_is_a_name_not_a_path(write_config, valid_config, mutate):
    problems = errors_of(write_config, mutate(valid_config, "colour.cdl_default", "looks/000.cdl"))
    assert any(p.startswith("colour.cdl_default:") for p in problems)
