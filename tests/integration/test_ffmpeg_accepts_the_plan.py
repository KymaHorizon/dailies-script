# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Integration: hand the generated render command to a real FFmpeg.

The pipeline itself executes nothing at this build step. This test stands in
for the executor so that the command builder is proven against the real parser
and muxers, with synthetic picture and sound in place of REDline and a recorder.
Skipped when no FFmpeg 5.1 or newer is installed at the usual locations.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from dailies.commands.render import build_render_plan
from dailies.config import load_config
from dailies.naming import DayContext

CANDIDATES = [os.environ.get("DAILIES_FFMPEG", ""), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]
FONTS = ["/System/Library/Fonts/Supplemental/Courier New.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
FRAMES = 25  # half a second at 50 fps
OFFSET = 1000  # samples, deliberately not a multiple of 960 or of 48


def _find_ffmpeg() -> Path | None:
    for candidate in CANDIDATES:
        if candidate and Path(candidate).is_file():
            out = subprocess.run([candidate, "-hide_banner", "-h", "full"], capture_output=True, text=True).stdout
            if "-fps_mode" in out:
                return Path(candidate)
    return None


FFMPEG = _find_ffmpeg()
FONT = next((Path(f) for f in FONTS if Path(f).is_file()), None)
pytestmark = pytest.mark.skipif(FFMPEG is None or FONT is None, reason="needs FFmpeg 5.1+ and a monospace font")


def probe(path: Path) -> dict:
    ffprobe = FFMPEG.with_name("ffprobe")
    out = subprocess.run([str(ffprobe), "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    import copy, stat, yaml
    root = tmp_path_factory.mktemp("show")
    for folder in ("camera", "sound", "dailies", "scratch", "manifests", "logs", "cache", "cdl"):
        (root / folder).mkdir()
    (root / "show.ocio").write_text("ocio_profile_version: 2\n")
    lut = root / "cache" / "identity.cube"
    lut.write_text("LUT_3D_SIZE 2\n" + "".join(f"{r} {g} {b}\n" for b in (0, 1) for g in (0, 1) for r in (0, 1)))
    redline = root / "REDline"
    redline.write_text("#!/bin/sh\n")
    redline.chmod(redline.stat().st_mode | stat.S_IXUSR)
    wav = root / "sound" / "14A-03.wav"
    subprocess.run([str(FFMPEG), "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=2",
                    "-ac", "8", "-c:a", "pcm_s24le", str(wav)], check=True)

    data = {
        "show": {"name": "Smoke", "code": "SMK", "fps": 50, "audio_rate": 48000, "timecode": "non_drop",
                 "cameras": ["A"], "units": ["MU"]},
        "tools": {name: {"path": str(path), "version": "test"} for name, path in
                  (("ffmpeg", FFMPEG), ("ffprobe", FFMPEG.with_name("ffprobe")), ("redline", redline))},
        "framing": {"crop_percent": 5.0, "crop_order": "crop_then_scale", "fit": "width", "aspect_tolerance": 0.005},
        "colour": {"ocio_config": str(root / "show.ocio"), "decode_space": "REDWideGamutRGB_Log3G10",
                   "output_transform": "none", "show_lut": str(lut), "camera_looks": {"A": None},
                   "cdl_dir": str(root / "cdl"), "lut_cache": str(root / "cache"), "bake_size": 65, "cdl_clamp": False, "apply": "baked",
                   "cdl_search": ["{scene}"], "cdl_default": None},
        "decode": {"engine": "redline", "resolution": "quarter", "bit_depth": 16, "transport": "pipe",
                   "sources": {"A": "r3d"}, "expected_resolution": {"A": [8192, 4320]}, "camera_letter_from": "reel_prefix",
                   "redline": {"metadata_args": ["--i", "{clip}"], "decode_args": ["--i", "{clip}"],
                               "intermediate_pattern": "{out_name}.%06d.tif", "intermediate_start_number": 0}},
        "sound": {"mix_tracks": [1, 2], "tc_window_hours": 4.0, "filename_pattern": None, "mos_cameras": []},
        "scan": {"camera_dir": "{day_folder}", "sound_dir": "{day_folder}"},
        "deliverables": {
            "avid": {"codec": "dnxhr_lb", "container": "mxf_opatom", "width": 1920, "height": 1080,
                     "pix_fmt": "yuv422p", "audio": "pcm_s24le", "burn_in": True},
            "review": {"codec": "h264", "container": "mp4", "width": 1920, "height": 1080, "pix_fmt": "yuv420p",
                       "crf": 30, "audio": "aac_192k", "burn_in": True}},
        "burn_in": {"font": str(FONT), "size_ratio": 0.025, "margin_ratio": 0.02,
                    "fields": {"top_left": "{clip_name}", "top_right": "{camera} / {scene}-{take}",
                               "bottom_left": "SRC {source_tc}", "bottom_right": "SND {sound_tc}"}},
        "naming": {"day_folder": "{show}_{date}_{day_code}_{unit}", "tape": "{clip_name}",
                   "avid_clip": "{scene}-{take}{camera}", "avid_clip_fallback": "{clip_name}"},
        "paths": {"camera_root": str(root / "camera"), "sound_root": str(root / "sound"),
                  "output_root": str(root / "dailies"), "scratch": str(root / "scratch"),
                  "manifest_dir": str(root / "manifests"), "log_dir": str(root / "logs")},
        "limits": {"decode_workers": 1, "encode_workers": 1},
    }
    config_path = root / "show.yaml"
    config_path.write_text(yaml.safe_dump(data))
    config = load_config(config_path)
    clip = {
        "id": "A001_C003_1012AB", "status": "graded",
        "mob_id": "urn:smpte:umid:060a2b34.01010105.01010f20.13000000.434b3488.8c644114.8231333c.f00dea50",
        "source": {"path": str(root / "camera" / "A001_C003_1012AB_001.R3D"), "size_bytes": 1, "mhl_verified": True},
        "picture": {"camera": "A", "roll": "001", "clip": "C003", "reel": "A001", "width": 8192, "height": 4320,
                    "fps": 50.0, "frames": FRAMES, "start_tc": "10:14:22:31", "end_tc": "10:14:23:06",
                    "anamorphic": 1.0, "iso": 800, "kelvin": 5600, "tint": 0.0, "scene": "It's 14A", "take": "3"},
        "sound": {"status": "matched", "files": [str(wav)], "soundroll": "SD001", "start_tc": "10:14:20:00",
                  "offset_samples": OFFSET, "channels": 8, "track_names": []},
        "colour": {"apply": "baked", "baked_lut": str(lut), "lut_hash": "identity"},
        "framing": None, "outputs": [], "flags": [], "errors": [],
    }
    plan = build_render_plan(config, DayContext("SMK", "2026-10-12", "SD001", "MU"), clip)

    # stand in for REDline: full white 16-bit RGB frames at the planned decode size, written to stdout
    width, height = plan.framing.decode
    fake_decoder = [str(FFMPEG), "-v", "error", "-f", "lavfi", "-i", f"color=white:s={width}x{height}:r=50",
                    "-frames:v", str(FRAMES), "-pix_fmt", "rgb48le", "-f", "rawvideo", "pipe:1"]
    ffmpeg_cmd = plan.runnables[0].stages[-1]
    for output in plan.outputs:
        output.temp.parent.mkdir(parents=True, exist_ok=True)
    shell = shlex.join(fake_decoder) + " | " + ffmpeg_cmd.shell()
    done = subprocess.run(shell, shell=True, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-3000:]
    return {o.final.name: o.temp for o in plan.outputs}


def test_every_planned_output_was_written(rendered):
    assert set(rendered) == {"A001_C003_1012AB_V.mxf", "A001_C003_1012AB_A1.mxf", "A001_C003_1012AB_A2.mxf",
                             "A001_C003_1012AB.mp4"}
    assert all(path.stat().st_size > 0 for path in rendered.values())


@pytest.mark.parametrize("name", ["A001_C003_1012AB_V.mxf", "A001_C003_1012AB.mp4"])
def test_video_keeps_every_frame_at_exactly_50(rendered, name):
    video = next(s for s in probe(rendered[name])["streams"] if s["codec_type"] == "video")
    assert video["r_frame_rate"] == "50/1"
    assert int(video["nb_read_frames"]) == FRAMES
    assert (video["width"], video["height"]) == (1920, 1080)


@pytest.mark.parametrize("name", ["A001_C003_1012AB_V.mxf", "A001_C003_1012AB.mp4"])
def test_video_is_tagged_legal_range_bt709(rendered, name):
    video = next(s for s in probe(rendered[name])["streams"] if s["codec_type"] == "video")
    assert video.get("color_range") == "tv"
    assert video.get("color_space") == "bt709"


def test_mxf_carries_the_start_timecode(rendered):
    assert probe(rendered["A001_C003_1012AB_V.mxf"])["format"]["tags"]["timecode"] == "10:14:22:31"


@pytest.mark.parametrize("name", ["A001_C003_1012AB_V.mxf", "A001_C003_1012AB_A1.mxf", "A001_C003_1012AB_A2.mxf"])
def test_mxf_packages_carry_the_avid_clip_name_and_the_tape_name(rendered, name):
    # ffprobe stops reporting reel_name once the material package is named, so read the MXF structure itself
    mxf = pytest.importorskip("aaf2.mxf")
    packages = {type(p).__name__: [] for p in mxf.MXFFile(str(rendered[name])).packages()}
    for package in mxf.MXFFile(str(rendered[name])).packages():
        packages[type(package).__name__].append(package.data.get("Name"))
    assert packages["MXFMaterialPackage"] == ["It's 14A-3A"]
    assert "A001_C003_1012AB" in packages["MXFSourcePackage"]


@pytest.mark.parametrize("name", ["A001_C003_1012AB_A1.mxf", "A001_C003_1012AB_A2.mxf"])
def test_avid_audio_is_mono_and_exactly_as_long_as_the_picture(rendered, name):
    audio = next(s for s in probe(rendered[name])["streams"] if s["codec_type"] == "audio")
    assert audio["channels"] == 1
    assert audio["sample_rate"] == "48000"
    assert int(audio["duration_ts"]) == FRAMES * 960


def luma_patch(path: Path, x: int, y: int) -> bytes:
    """Raw Y plane of a 16x16 patch. Kept as YUV so that no range conversion happens on the way out."""
    done = subprocess.run([str(FFMPEG), "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", f"crop=16:16:{x}:{y}",
                           "-f", "rawvideo", "-pix_fmt", "yuv422p", "pipe:1"], capture_output=True, check=True)
    assert len(done.stdout) == 16 * 16 * 2
    return done.stdout[:256]


def test_white_lands_on_legal_white_not_full_range(rendered):
    luma = luma_patch(rendered["A001_C003_1012AB_V.mxf"], 960, 540)
    assert 233 <= min(luma) and max(luma) <= 237  # legal white is 235, full range would be 255


def test_letterbox_bars_are_legal_black(rendered):
    luma = luma_patch(rendered["A001_C003_1012AB_V.mxf"], 960, 8)
    assert 14 <= min(luma) and max(luma) <= 18  # legal black is 16
