# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Sound file metadata: ffprobe for BWF time_reference and stream facts, a RIFF reader for iXML."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dailies.commands.base import Command
from dailies.scan.wav import WavError, parse_probe, probe_command, read_ixml, sound_file_entry
from wavbuilder import bext_chunk, data_chunk, fmt_chunk, ixml_chunk, rf64, riff

TIME_REFERENCE = (10 * 3600 + 14 * 60 + 20) * 48000 + 123  # deliberately not frame aligned

PROBE = json.dumps({
    "streams": [{"codec_type": "audio", "codec_name": "pcm_s24le", "sample_rate": "48000", "channels": 8,
                 "bits_per_sample": 24, "duration_ts": 1662720}],
    "format": {"tags": {"time_reference": str(TIME_REFERENCE), "comment": "sSCENE=22B\r\nsTAKE=7\r\nsTAPE=SD099\r\n"}},
})


def write(tmp_path: Path, data: bytes, name: str = "14A-03.wav") -> Path:
    path = tmp_path / "SD014" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------ ffprobe


def test_probe_command_matches_the_spec(load_valid):
    cfg = load_valid()
    assert probe_command(cfg, Path("/s/14A-03.wav")) == Command((
        str(cfg.tools.ffprobe.path), "-v", "error", "-show_format", "-show_streams", "-print_format", "json",
        "/s/14A-03.wav"))


def test_probe_facts_stay_in_the_sample_domain():
    facts = parse_probe(PROBE)
    assert facts.start_samples == TIME_REFERENCE
    assert facts.sample_count == 1662720
    assert (facts.sample_rate, facts.channels, facts.bit_depth) == (48000, 8, 24)
    assert isinstance(facts.start_samples, int)


def test_missing_time_reference_is_none_not_zero():
    data = json.loads(PROBE)
    del data["format"]["tags"]["time_reference"]
    assert parse_probe(json.dumps(data)).start_samples is None


def test_bext_description_is_kept_for_the_fallback():
    assert parse_probe(PROBE).bext == {"scene": "22B", "take": "7", "tape": "SD099"}


@pytest.mark.parametrize("text", ["", "{}", '{"streams": [], "format": {}}', "not json"])
def test_unusable_probe_output_is_a_wav_error(text):
    with pytest.raises(WavError):
        parse_probe(text)


# --------------------------------------------------------------------- iXML


def test_ixml_before_the_audio_data(tmp_path):
    path = write(tmp_path, riff(bext_chunk("", TIME_REFERENCE), ixml_chunk(), fmt_chunk(), data_chunk()))
    assert read_ixml(path) == {"scene": "14A", "take": "3", "tape": "SD014", "track_names": ["MIX L", "MIX R"]}


def test_ixml_after_the_audio_data(tmp_path):
    path = write(tmp_path, riff(fmt_chunk(), data_chunk(), ixml_chunk(scene="9", take="12")))
    assert read_ixml(path)["take"] == "12"


def test_rf64_data_size_comes_from_ds64(tmp_path):
    path = write(tmp_path, rf64(fmt_chunk(), ixml_chunk(tape="SD200")))
    assert read_ixml(path)["tape"] == "SD200"


def test_track_names_follow_channel_order(tmp_path):
    path = write(tmp_path, riff(fmt_chunk(channels=4), ixml_chunk(tracks=("MIX", "BLANK", "BOOM 1", "BOOM 2")), data_chunk()))
    assert read_ixml(path)["track_names"] == ["MIX", "BLANK", "BOOM 1", "BOOM 2"]


def test_file_without_ixml_gives_nothing(tmp_path):
    assert read_ixml(write(tmp_path, riff(fmt_chunk(), data_chunk()))) == {}


def test_blank_ixml_fields_are_dropped(tmp_path):
    path = write(tmp_path, riff(fmt_chunk(), ixml_chunk(scene="", take=" "), data_chunk()))
    assert "scene" not in read_ixml(path) and "take" not in read_ixml(path)


def test_damaged_ixml_gives_nothing_and_does_not_stop_the_scan(tmp_path):
    from wavbuilder import chunk
    path = write(tmp_path, riff(fmt_chunk(), chunk(b"iXML", b"<BWFXML><SCENE>14A</SCEN"), data_chunk()))
    assert read_ixml(path) == {}


def test_truncated_file_gives_nothing(tmp_path):
    whole = riff(fmt_chunk(), data_chunk(), ixml_chunk())
    assert read_ixml(write(tmp_path, whole[: len(whole) // 2])) == {}


def test_not_a_wav_file_is_an_error(tmp_path):
    with pytest.raises(WavError, match="RIFF"):
        read_ixml(write(tmp_path, b"ID3\x04not a wav at all"))


# ------------------------------------------------------------ manifest entry


def test_entry_prefers_ixml_over_bext_over_the_file_name(load_valid, tmp_path):
    path = write(tmp_path, riff(fmt_chunk(), ixml_chunk(), data_chunk()))
    entry = sound_file_entry(load_valid(), path, parse_probe(PROBE), read_ixml(path))
    assert (entry["scene"], entry["take"], entry["soundroll"]) == ("14A", "3", "SD014")
    assert entry["metadata_source"] == "ixml"


def test_entry_falls_back_to_the_bext_description(load_valid, tmp_path):
    path = write(tmp_path, riff(fmt_chunk(), data_chunk()))
    entry = sound_file_entry(load_valid(), path, parse_probe(PROBE), {})
    assert (entry["scene"], entry["take"], entry["soundroll"]) == ("22B", "7", "SD099")
    assert entry["metadata_source"] == "bext"


def test_entry_falls_back_to_the_file_name_then_the_folder(load_valid, tmp_path):
    path = write(tmp_path, riff(fmt_chunk(), data_chunk()), name="31C-04.wav")
    probe = json.loads(PROBE)
    del probe["format"]["tags"]["comment"]
    entry = sound_file_entry(load_valid(), path, parse_probe(json.dumps(probe)), {})
    assert (entry["scene"], entry["take"], entry["soundroll"]) == ("31C", "04", "SD014")
    assert entry["metadata_source"] == "filename"


def test_entry_with_no_metadata_anywhere(load_valid, tmp_path):
    path = write(tmp_path, riff(fmt_chunk(), data_chunk()), name="wildtrack.wav")
    probe = json.loads(PROBE)
    del probe["format"]["tags"]["comment"]
    entry = sound_file_entry(load_valid(), path, parse_probe(json.dumps(probe)), {})
    assert (entry["scene"], entry["take"], entry["metadata_source"]) == (None, None, "none")


def test_entry_carries_sample_domain_range(load_valid, tmp_path):
    path = write(tmp_path, riff(fmt_chunk(), data_chunk()))
    entry = sound_file_entry(load_valid(), path, parse_probe(PROBE), {})
    assert entry["start_samples"] == TIME_REFERENCE
    assert entry["end_samples"] == TIME_REFERENCE + 1662720
    assert entry["flags"] == []


def test_wrong_sample_rate_and_missing_timecode_are_flagged(load_valid, tmp_path):
    path = write(tmp_path, riff(fmt_chunk(), data_chunk()))
    probe = json.loads(PROBE)
    probe["streams"][0]["sample_rate"] = "96000"
    del probe["format"]["tags"]["time_reference"]
    entry = sound_file_entry(load_valid(), path, parse_probe(json.dumps(probe)), {})
    assert entry["flags"] == ["no_time_reference", "wrong_sample_rate"]
    assert entry["start_samples"] is None and entry["end_samples"] is None


# ------------------------------------------------------- remaining branches


def test_rf64_without_ds64_cannot_be_walked_past_the_audio(tmp_path):
    import struct
    from wavbuilder import chunk
    body = b"WAVE" + fmt_chunk() + chunk(b"data", b"\0" * 64, declared=0xFFFFFFFF) + ixml_chunk()
    assert read_ixml(write(tmp_path, b"RF64" + struct.pack("<I", 0xFFFFFFFF) + body)) == {}


def test_short_ds64_is_tolerated(tmp_path):
    import struct
    from wavbuilder import chunk
    body = b"WAVE" + chunk(b"ds64", b"\0" * 8) + ixml_chunk(tape="SD7") + fmt_chunk() + data_chunk()
    assert read_ixml(write(tmp_path, b"RF64" + struct.pack("<I", 0xFFFFFFFF) + body))["tape"] == "SD7"


def test_absurdly_large_ixml_chunk_is_ignored(tmp_path):
    from wavbuilder import chunk
    path = write(tmp_path, riff(fmt_chunk(), chunk(b"iXML", b"<BWFXML/>", declared=64 * 1024 * 1024), data_chunk()))
    assert read_ixml(path) == {}


def test_tracks_without_an_index_keep_their_file_order(tmp_path):
    from wavbuilder import chunk
    xml = b"<BWFXML><TRACK_LIST><TRACK><NAME>MIX</NAME></TRACK><TRACK><NAME>BOOM</NAME></TRACK></TRACK_LIST></BWFXML>"
    assert read_ixml(write(tmp_path, riff(fmt_chunk(), chunk(b"iXML", xml), data_chunk())))["track_names"] == ["MIX", "BOOM"]


def test_filename_fallback_is_off_when_the_pattern_is_null(valid_config, write_config, mutate, tmp_path):
    from dailies.config import load_config
    config = load_config(write_config(mutate(valid_config, "sound.filename_pattern", None)))
    path = write(tmp_path, riff(fmt_chunk(), data_chunk()), name="31C-04.wav")
    probe = json.loads(PROBE)
    del probe["format"]["tags"]["comment"]
    assert sound_file_entry(config, path, parse_probe(json.dumps(probe)), {})["metadata_source"] == "none"


def test_filename_that_does_not_match_the_pattern_gives_nothing(load_valid, tmp_path):
    path = write(tmp_path, riff(fmt_chunk(), data_chunk()), name="ROOMTONE.wav")
    probe = json.loads(PROBE)
    del probe["format"]["tags"]["comment"]
    assert sound_file_entry(load_valid(), path, parse_probe(json.dumps(probe)), {})["scene"] is None


def test_ixml_without_tracks_has_no_track_names(tmp_path):
    path = write(tmp_path, riff(fmt_chunk(), ixml_chunk(tracks=()), data_chunk()))
    assert "track_names" not in read_ixml(path)
