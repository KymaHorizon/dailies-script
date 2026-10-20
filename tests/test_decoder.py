# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Decoder interface. REDline sits behind it so an SDK decoder can replace it
without any other stage changing."""

from __future__ import annotations

from pathlib import Path

import pytest

from dailies.commands.base import Command
from dailies.decoder import DecodePlan, DecoderError, RedlineDecoder, make_decoder

CLIP = Path("/Volumes/RAID/CAMERA/A001/A001_C003_1012AB.RDC/A001_C003_1012AB_001.R3D")


def test_metadata_command_comes_from_the_config_template(load_valid):
    cfg = load_valid()
    cmd = make_decoder(cfg).metadata_command(CLIP)
    assert cmd == Command((str(cfg.tools.redline.path), "--i", str(CLIP), "--printMeta", "1"))


def test_file_transport_decodes_to_scratch_then_ffmpeg_reads_the_sequence(load_valid):
    cfg = load_valid()
    plan = make_decoder(cfg).plan(CLIP, clip_id="A001_C003_1012AB", size=(2048, 1080), fps=50)
    out_dir = cfg.paths.scratch / "A001_C003_1012AB"
    assert plan.piped is False
    assert plan.producer == Command((
        str(cfg.tools.redline.path), "--i", str(CLIP), "--outDir", str(out_dir), "--o", "A001_C003_1012AB",
        "--format", "1",
    ))
    assert plan.input_args == (
        "-framerate", "50", "-start_number", "0", "-i", str(out_dir / "A001_C003_1012AB.%06d.tif"),
    )
    assert plan.intermediates == (out_dir,)


def test_pipe_transport_feeds_raw_rgb48_into_ffmpeg(valid_config, write_config, mutate):
    from dailies.config import load_config

    cfg = load_config(write_config(mutate(valid_config, "decode.transport", "pipe")))
    plan = make_decoder(cfg).plan(CLIP, clip_id="A001_C003_1012AB", size=(2048, 1080), fps=50)
    assert plan.piped is True
    assert plan.input_args == (
        "-f", "rawvideo", "-pixel_format", "rgb48le", "-video_size", "2048x1080", "-framerate", "50", "-i", "pipe:0",
    )
    assert plan.intermediates == ()


def test_size_placeholders_are_filled(valid_config, write_config, mutate):
    from dailies.config import load_config

    args = ["--i", "{clip}", "--resizeX", "{width}", "--resizeY", "{height}", "--outDir", "{out_dir}", "--o", "{out_name}"]
    cfg = load_config(write_config(mutate(valid_config, "decode.redline.decode_args", args)))
    plan = make_decoder(cfg).plan(CLIP, clip_id="A001_C003_1012AB", size=(2048, 1080), fps=50)
    assert plan.producer.argv[3:7] == ("--resizeX", "2048", "--resizeY", "1080")


def test_clip_id_may_not_escape_the_scratch_folder(load_valid):
    with pytest.raises(DecoderError, match="clip id"):
        make_decoder(load_valid()).plan(CLIP, clip_id="../outside", size=(2048, 1080), fps=50)


def test_redline_decoder_satisfies_the_interface(load_valid):
    decoder = make_decoder(load_valid())
    assert isinstance(decoder, RedlineDecoder)
    assert decoder.name == "redline"
    assert isinstance(decoder.plan(CLIP, clip_id="A001_C003_1012AB", size=(2048, 1080), fps=50), DecodePlan)


# ---------------------------------------------------------------- sdk engine

import json

from dailies.decoder import ClipFacts, SdkDecoder

FIXTURE = Path(__file__).parent / "fixtures" / "r3dtool_meta_vraptor_spanned.json"


@pytest.fixture
def sdk(valid_config, write_config, mutate, show_tree):
    from dailies.config import load_config

    data = mutate(valid_config, "decode.engine", "sdk")
    data = mutate(data, "decode.transport", "pipe")
    data = mutate(data, "tools.r3dtool", {"path": str(show_tree / "bin/r3dtool"), "version": "0.1.0"})
    return load_config(write_config(data))


def test_sdk_engine_builds_an_sdk_decoder(sdk):
    decoder = make_decoder(sdk)
    assert isinstance(decoder, SdkDecoder) and decoder.name == "sdk"


def test_sdk_commands_go_through_r3dtool(sdk):
    decoder = make_decoder(sdk)
    exe = str(sdk.tools.r3dtool.path)
    assert decoder.version_command() == Command((exe, "version"))
    assert decoder.metadata_command(CLIP) == Command((exe, "meta", str(CLIP)))


def test_sdk_version_is_the_tool_and_sdk_build(sdk):
    text = '{"tool":"r3dtool","tool_version":"0.1.0","sdk":"R3DAPI 9.2.1-7058969 (20260608 Ma64S) [/x/]"}'
    assert make_decoder(sdk).parse_version(text) == "0.1.0 / R3DAPI 9.2.1-7058969 (20260608 Ma64S)"


def test_real_vraptor_metadata_is_parsed(sdk):
    facts = make_decoder(sdk).parse_metadata(FIXTURE.read_text())
    assert facts == ClipFacts(
        clip_name="A001_C003_0428HT", camera_id="A", roll="001", clip="C003", width=6144, height=2592, fps=25.0,
        frames=711, start_tc="00:00:44:18", tc_absolute="00:00:44:18", tc_edge="01:00:30:20",
        tc_preference="absolute", anamorphic=1.0, iso=800, kelvin=4500, tint=-30.0, scene=None, take=None,
        dropped_frames=0, camera_model="V-RAPTOR 8K VV",
    )


def test_slate_metadata_is_picked_up_when_the_loader_entered_it(sdk):
    data = json.loads(FIXTURE.read_text())
    data["metadata"].update(user_scene="14A", user_take="3")
    facts = make_decoder(sdk).parse_metadata(json.dumps(data))
    assert (facts.scene, facts.take) == ("14A", "3")


def test_blank_slate_fields_count_as_absent(sdk):
    data = json.loads(FIXTURE.read_text())
    data["metadata"].update(user_scene="  ", user_take="")
    facts = make_decoder(sdk).parse_metadata(json.dumps(data))
    assert (facts.scene, facts.take) == (None, None)


def test_edge_code_preference_is_reported(sdk):
    data = json.loads(FIXTURE.read_text())
    data["metadata"]["user_timecode_preference"] = 0
    assert make_decoder(sdk).parse_metadata(json.dumps(data)).tc_preference == "edge"


def test_fractional_rates_come_from_numerator_and_denominator(sdk):
    data = json.loads(FIXTURE.read_text())
    data["metadata"].update(framerate_numerator=24000, framerate_denominator=1001)
    assert make_decoder(sdk).parse_metadata(json.dumps(data)).fps == pytest.approx(23.976, abs=1e-3)


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"width": 1}'])
def test_unusable_metadata_output_is_a_decoder_error(sdk, text):
    with pytest.raises(DecoderError, match="metadata"):
        make_decoder(sdk).parse_metadata(text)


def test_clip_without_frames_is_a_decoder_error(sdk):
    data = json.loads(FIXTURE.read_text())
    data["frames"] = 0
    with pytest.raises(DecoderError, match="no frames"):
        make_decoder(sdk).parse_metadata(json.dumps(data))


def test_sdk_decode_is_not_built_until_the_render_step(sdk):
    with pytest.raises(DecoderError, match="render step"):
        make_decoder(sdk).plan(CLIP, clip_id="A001_C003_1012AB", size=(2048, 1080), fps=50)


def test_redline_metadata_parsing_is_honest_about_not_being_built(load_valid):
    with pytest.raises(DecoderError, match="REDline"):
        make_decoder(load_valid()).parse_metadata("anything")


def test_redline_version_command_and_unbuilt_parser(load_valid):
    cfg = load_valid()
    decoder = make_decoder(cfg)
    assert decoder.version_command() == Command((str(cfg.tools.redline.path), "--version"))
    with pytest.raises(DecoderError, match="REDline"):
        decoder.parse_version("anything")


@pytest.mark.parametrize("text", ["", "not json", "{}", '{"tool_version": "0.1.0", "sdk": 5}'])
def test_unreadable_version_output_is_a_decoder_error(sdk, text):
    with pytest.raises(DecoderError, match="version"):
        make_decoder(sdk).parse_version(text)


def test_clip_without_a_default_timecode_is_a_decoder_error(sdk):
    data = json.loads(FIXTURE.read_text())
    del data["tc_default"]
    with pytest.raises(DecoderError, match="metadata"):
        make_decoder(sdk).parse_metadata(json.dumps(data))


# ----------------------------------------------------------- ProRes through FFmpeg

from dailies.decoder import FfmpegDecoder, decoder_for

PRORES = Path(__file__).parent / "fixtures" / "ffprobe_prores_4k50.json"
MOV = Path("/Volumes/RAID/OCF/A001_1012AB/A001_C003_1012AB.RDC/A001_C003_1012AB_001.mov")


def test_the_decoder_follows_the_file_not_the_show(load_valid, sdk):
    assert isinstance(decoder_for(load_valid(), MOV), FfmpegDecoder)
    assert isinstance(decoder_for(load_valid(), CLIP), RedlineDecoder)
    assert isinstance(decoder_for(sdk, CLIP), SdkDecoder)
    assert isinstance(decoder_for(sdk, MOV.with_suffix(".MOV")), FfmpegDecoder)


def test_an_unknown_kind_of_file_has_no_decoder(load_valid):
    with pytest.raises(DecoderError, match="no decoder"):
        decoder_for(load_valid(), Path("/c/A001_C001.braw"))


def test_prores_metadata_is_one_ffprobe_call(load_valid):
    cfg = load_valid()
    assert decoder_for(cfg, MOV).metadata_command(MOV) == Command((
        str(cfg.tools.ffprobe.path), "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(MOV)))


def test_prores_facts(load_valid):
    facts = decoder_for(load_valid(), MOV).parse_metadata(PRORES.read_text())
    assert facts == ClipFacts(
        clip_name="A001_C003_1012AB", camera_id="A", roll="001", clip="C003", width=4096, height=2160, fps=50.0,
        frames=1732, start_tc="10:14:22:31", tc_absolute=None, tc_edge=None, tc_preference="container",
        anamorphic=1.0, iso=None, kelvin=None, tint=None, scene=None, take=None, dropped_frames=0,
        camera_model="", codec="ProRes HQ",
    )


def test_sdk_facts_name_their_codec(sdk):
    assert make_decoder(sdk).parse_metadata(FIXTURE.read_text()).codec == "R3D"


def test_timecode_is_taken_from_the_timecode_track_when_the_video_stream_has_none(load_valid):
    data = json.loads(PRORES.read_text())
    del data["streams"][0]["tags"]["timecode"]
    assert decoder_for(load_valid(), MOV).parse_metadata(json.dumps(data)).start_tc == "10:14:22:31"


def test_a_movie_without_timecode_is_a_decoder_error(load_valid):
    data = json.loads(PRORES.read_text())
    del data["streams"][0]["tags"]["timecode"], data["streams"][1]
    with pytest.raises(DecoderError, match="timecode"):
        decoder_for(load_valid(), MOV).parse_metadata(json.dumps(data))


def test_a_movie_that_is_not_prores_is_a_decoder_error(load_valid):
    data = json.loads(PRORES.read_text())
    data["streams"][0]["codec_name"] = "h264"
    with pytest.raises(DecoderError, match="h264"):
        decoder_for(load_valid(), MOV).parse_metadata(json.dumps(data))


def test_anamorphic_squeeze_comes_from_the_pixel_aspect(load_valid):
    data = json.loads(PRORES.read_text())
    data["streams"][0]["sample_aspect_ratio"] = "2:1"
    assert decoder_for(load_valid(), MOV).parse_metadata(json.dumps(data)).anamorphic == 2.0


@pytest.mark.parametrize("text", ["", "{}", '{"streams": [], "format": {}}'])
def test_unusable_ffprobe_output_is_a_decoder_error(load_valid, text):
    with pytest.raises(DecoderError, match="metadata"):
        decoder_for(load_valid(), MOV).parse_metadata(text)


def test_prores_is_decoded_by_ffmpeg_itself_at_full_size(load_valid):
    decoder = decoder_for(load_valid(), MOV)
    assert decoder.decoded_size(4096, 2160) == (4096, 2160)
    plan = decoder.plan(MOV, clip_id="A001_C003_1012AB", size=(4096, 2160), fps=50)
    assert plan.producer is None and plan.piped is False and plan.intermediates == ()
    assert plan.input_args == ("-i", str(MOV)) and plan.input_count == 1
    assert plan.input_filters == ("scale=in_color_matrix=bt709:in_range=tv",)


def test_spanned_prores_is_joined_inside_the_filter_graph(load_valid):
    second = MOV.with_name("A001_C003_1012AB_002.mov")
    plan = decoder_for(load_valid(), MOV).plan(MOV, clip_id="A001_C003_1012AB", size=(4096, 2160), fps=50, parts=(MOV, second))
    assert plan.input_args == ("-i", str(MOV), "-i", str(second)) and plan.input_count == 2


def test_r3d_decoders_report_the_debayer_size_and_one_input(load_valid):
    decoder = decoder_for(load_valid(), CLIP)
    assert decoder.decoded_size(8192, 4320) == (2048, 1080)
    plan = decoder.plan(CLIP, clip_id="A001_C003_1012AB", size=(2048, 1080), fps=50)
    assert plan.input_count == 1 and plan.input_filters == ()


# ---------------------------------------------------- remaining branches


def test_sdk_decoded_size_follows_the_configured_debayer_resolution(sdk):
    assert make_decoder(sdk).decoded_size(8192, 4320) == (2048, 1080)


def test_ffmpeg_decoder_version_command_and_parser(load_valid):
    cfg = load_valid()
    decoder = decoder_for(cfg, MOV)
    assert decoder.version_command() == Command((str(cfg.tools.ffprobe.path), "-version"))
    assert decoder.parse_version("ffprobe version 7.1.1 Copyright (c) 2007-2025 the FFmpeg developers\n") == "7.1.1"
    with pytest.raises(DecoderError, match="version"):
        decoder.parse_version("something else entirely")
