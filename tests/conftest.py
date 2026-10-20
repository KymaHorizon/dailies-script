# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures. A valid show config is built on a temp filesystem so that
each test can break exactly one thing and assert on exactly one error."""

from __future__ import annotations

import copy
import stat
from pathlib import Path

import pytest
import yaml


def _touch(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _tool(path: Path) -> Path:
    _touch(path, "#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def show_tree(tmp_path: Path) -> Path:
    """Directory tree with every file and folder the valid config points at."""
    root = tmp_path / "show"
    for folder in ("camera", "sound", "dailies", "scratch", "manifests", "logs", "cache/luts", "cdl"):
        (root / folder).mkdir(parents=True)
    _touch(root / "config/show.ocio", "ocio_profile_version: 2\n")
    _touch(root / "config/fonts/Courier.ttf")
    _touch(root / "luts/show_rec709_v3.cube")
    _touch(root / "luts/cam_A_v2.cube")
    for name in ("ffmpeg", "ffprobe", "REDline", "r3dtool"):
        _tool(root / "bin" / name)
    return root


@pytest.fixture
def valid_config(show_tree: Path) -> dict:
    r = show_tree
    return {
        "show": {
            "name": "Test Show",
            "code": "TST",
            "fps": 50,
            "audio_rate": 48000,
            "timecode": "non_drop",
            "cameras": ["A", "B"],
            "units": ["MU"],
        },
        "tools": {
            "ffmpeg": {"path": str(r / "bin/ffmpeg"), "version": "7.1.1"},
            "ffprobe": {"path": str(r / "bin/ffprobe"), "version": "7.1.1"},
            "redline": {"path": str(r / "bin/REDline"), "version": "64.0.25"},
        },
        "framing": {
            "crop_percent": 5.0,
            "crop_order": "crop_then_scale",
            "fit": "width",
            "aspect_tolerance": 0.005,
        },
        "colour": {
            "ocio_config": str(r / "config/show.ocio"),
            "decode_space": "REDWideGamutRGB_Log3G10",
            "output_transform": "none",
            "show_lut": str(r / "luts/show_rec709_v3.cube"),
            "camera_looks": {"A": str(r / "luts/cam_A_v2.cube"), "B": None},
            "cdl_dir": str(r / "cdl"),
            "lut_cache": str(r / "cache/luts"),
            "bake_size": 65,
            "cdl_clamp": False,
            "apply": "exact",
            "cdl_search": ["{day_folder}/{clip_name}", "{day_folder}/{scene}", "{clip_name}", "{scene}"],
            "cdl_default": "000",
        },
        "decode": {
            "engine": "redline",
            "resolution": "quarter",
            "bit_depth": 16,
            "transport": "file",
            "sources": {"A": "r3d", "B": "r3d"},
            "expected_resolution": {"A": [8192, 4320], "B": [8192, 4320]},
            "camera_letter_from": "reel_prefix",
            "redline": {
                "metadata_args": ["--i", "{clip}", "--printMeta", "1"],
                "decode_args": ["--i", "{clip}", "--outDir", "{out_dir}", "--o", "{out_name}", "--format", "1"],
                "intermediate_pattern": "{out_name}.%06d.tif",
                "intermediate_start_number": 0,
            },
        },
        "sound": {"mix_tracks": [1, 2], "tc_window_hours": 4.0,
                  "filename_pattern": "^(?P<scene>[0-9]+[A-Z]*)-(?P<take>[0-9]+)", "mos_cameras": []},
        "scan": {"camera_dir": "{day_folder}/01_CAMERA", "sound_dir": "{day_folder}/02_SOUND"},
        "deliverables": {
            "avid": {
                "codec": "dnxhr_lb",
                "container": "mxf_opatom",
                "width": 1920,
                "height": 1080,
                "pix_fmt": "yuv422p",
                "audio": "pcm_s24le",
                "burn_in": True,
            },
            "review": {
                "codec": "h264",
                "container": "mp4",
                "width": 1920,
                "height": 1080,
                "pix_fmt": "yuv420p",
                "crf": 20,
                "audio": "aac_192k",
                "burn_in": True,
            },
        },
        "burn_in": {
            "font": str(r / "config/fonts/Courier.ttf"),
            "size_ratio": 0.025,
            "margin_ratio": 0.02,
            "fields": {
                "top_left": "{clip_name}",
                "top_right": "{camera} / {scene}-{take}",
                "bottom_left": "SRC {source_tc}",
                "bottom_right": "SND {sound_tc}",
            },
        },
        "naming": {
            "day_folder": "{show}_{date}_{day_code}_{unit}",
            "tape": "{clip_name}",
            "avid_clip": "{scene}-{take}{camera}",
            "avid_clip_fallback": "{clip_name}",
        },
        "paths": {
            "camera_root": str(r / "camera"),
            "sound_root": str(r / "sound"),
            "output_root": str(r / "dailies"),
            "scratch": str(r / "scratch"),
            "manifest_dir": str(r / "manifests"),
            "log_dir": str(r / "logs"),
        },
        "limits": {"decode_workers": 2, "encode_workers": 8},
    }


@pytest.fixture
def write_config(tmp_path: Path):
    """Write a config dict as YAML and return its path."""

    def _write(data: dict, name: str = "show.yaml") -> Path:
        path = tmp_path / name
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        return path

    return _write


@pytest.fixture
def mutate():
    """Return a deep copy of a dict with one dotted key set or removed."""

    def _mutate(data: dict, dotted: str, value=..., *, remove: bool = False) -> dict:
        out = copy.deepcopy(data)
        node = out
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node[key]
        if remove:
            del node[leaf]
        else:
            node[leaf] = value
        return out

    return _mutate


@pytest.fixture
def load_valid(valid_config, write_config):
    """Load the valid config, optionally with one dotted key changed."""
    from dailies.config import load_config

    def _load(**kwargs):
        return load_config(write_config(valid_config), **kwargs)

    return _load
