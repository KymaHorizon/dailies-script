# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""CLI: fail fast on config, dry-run prints every command and touches nothing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dailies.cli import main
from dailies.manifest import Manifest, manifest_path

SOURCE = {"path": "/Volumes/RAID/CAMERA/A001/A001_C003_1012AB.RDC/A001_C003_1012AB_001.R3D",
          "size_bytes": 1, "mhl_verified": True}
PICTURE = {"camera": "A", "roll": "001", "clip": "C003", "reel": "A001", "width": 8192, "height": 4320,
           "fps": 50.0, "frames": 1732, "start_tc": "10:14:22:31", "end_tc": "10:14:57:13", "anamorphic": 1.0,
           "iso": 800, "kelvin": 5600, "tint": 0.0, "scene": "14A", "take": "3"}
SOUND = {"status": "matched", "files": ["/Volumes/RAID/SOUND/SD001/14A-03.wav"], "soundroll": "SD001",
         "start_tc": "10:14:20:00", "offset_samples": 113280, "channels": 8, "track_names": []}
COLOUR = {"apply": "baked", "baked_lut": "/show/cache/luts/9f3c1b.cube", "lut_hash": "9f3c1b"}


@pytest.fixture
def config_path(valid_config, write_config) -> Path:
    return write_config(valid_config)


@pytest.fixture
def day(valid_config) -> Manifest:
    """A saved manifest with two graded clips and one that has only been scanned."""
    m = Manifest.new(day="2026-10-12", day_code="SD014", unit="MU", show="TST", config_hash="sha256:x",
                     tools={}, created="2026-10-12T19:00:00Z")
    for clip_id in ("A001_C003_1012AB", "B001_C001_1012CD", "A001_C004_1012AB"):
        source = {**SOURCE, "path": SOURCE["path"].replace("A001_C003_1012AB", clip_id)}
        m.add_clip(clip_id, source=source, picture={**PICTURE, "camera": clip_id[0]}, mob_id=None)
    for clip_id in ("A001_C003_1012AB", "B001_C001_1012CD"):
        m.set_block(clip_id, stage="sync", name="sound", block=SOUND)
        m.set_block(clip_id, stage="grade", name="colour", block=COLOUR)
    m.save(manifest_path(Path(valid_config["paths"]["manifest_dir"]), "2026-10-12", "MU"))
    return m


def run(*argv: str) -> int:
    return main(list(argv))


# ------------------------------------------------------------------ check


def test_check_accepts_a_valid_config(config_path, capsys):
    assert run("check", "--config", str(config_path)) == 0
    out = capsys.readouterr().out
    assert "config ok" in out and "sha256:" in out
    assert "avid, cameras A B: crop 1944x1026 at 52,27, scale 1920x1012, pad to 1920x1080" in out


def test_bad_config_exits_2_and_lists_every_problem(valid_config, write_config, mutate, capsys):
    data = mutate(mutate(valid_config, "show.fps", 25), "framing.fit", "stretch")
    assert run("check", "--config", str(write_config(data))) == 2
    err = capsys.readouterr().err
    assert "show.fps" in err and "framing.fit" in err


def test_missing_config_exits_2(tmp_path, capsys):
    assert run("check", "--config", str(tmp_path / "absent.yaml")) == 2
    assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------- dry run


def test_render_dry_run_prints_decoder_and_ffmpeg_commands(config_path, day, capsys, valid_config):
    assert run("render", "--config", str(config_path), "--day", "2026-10-12", "--dry-run") == 1
    out = capsys.readouterr().out
    assert out.count("# render A001_C003_1012AB\n") == 2  # decode, then encode
    assert valid_config["tools"]["redline"]["path"] in out
    assert valid_config["tools"]["ffmpeg"]["path"] in out
    assert "-fps_mode passthrough" in out


def test_clip_that_cannot_be_built_is_reported_and_the_batch_continues(config_path, day, capsys):
    code = run("render", "--config", str(config_path), "--day", "2026-10-12", "--dry-run")
    captured = capsys.readouterr()
    assert code == 1
    assert "A001_C004_1012AB" in captured.err and "sync" in captured.err
    assert "# render B001_C001_1012CD" in captured.out


def test_clip_filter_is_a_glob(config_path, day, capsys):
    assert run("render", "--config", str(config_path), "--day", "2026-10-12", "--dry-run", "--clip", "B001_*") == 0
    out = capsys.readouterr().out
    assert "B001_C001_1012CD" in out and "A001_C003_1012AB" not in out


def test_clip_filter_matching_nothing_exits_2(config_path, day, capsys):
    assert run("render", "--config", str(config_path), "--day", "2026-10-12", "--dry-run", "--clip", "Z*") == 2
    assert "no clips match" in capsys.readouterr().err


def test_up_to_date_outputs_are_skipped_unless_forced(config_path, day, valid_config, capsys):
    from dailies.commands.render import build_render_plan
    from dailies.config import load_config
    from dailies.naming import DayContext

    cfg = load_config(config_path)
    path = manifest_path(cfg.paths.manifest_dir, "2026-10-12", "MU")
    manifest = Manifest.load(path)
    plan = build_render_plan(cfg, DayContext("TST", "2026-10-12", "SD014", "MU"), manifest.clip("B001_C001_1012CD"))
    for target, settings_hash in plan.settings_hashes.items():
        manifest.set_output("B001_C001_1012CD", {"target": target, "settings_hash": settings_hash,
                                                 "video": "/x", "audio": [], "rendered": "2026-10-12T21:00:00Z"})
    manifest.save(path)

    args = ("render", "--config", str(config_path), "--day", "2026-10-12", "--dry-run", "--clip", "B001_*")
    assert run(*args) == 0
    assert "up to date" in capsys.readouterr().out
    assert run(*args, "--force") == 0
    assert "-filter_complex" in capsys.readouterr().out


def test_only_stale_targets_are_rebuilt(config_path, day, capsys):
    from dailies.commands.render import build_render_plan
    from dailies.config import load_config
    from dailies.naming import DayContext

    cfg = load_config(config_path)
    path = manifest_path(cfg.paths.manifest_dir, "2026-10-12", "MU")
    manifest = Manifest.load(path)
    plan = build_render_plan(cfg, DayContext("TST", "2026-10-12", "SD014", "MU"), manifest.clip("B001_C001_1012CD"))
    manifest.set_output("B001_C001_1012CD", {"target": "avid", "settings_hash": plan.settings_hashes["avid"],
                                             "video": "/x", "audio": [], "rendered": "2026-10-12T21:00:00Z"})
    manifest.save(path)
    assert run("render", "--config", str(config_path), "--day", "2026-10-12", "--dry-run", "--clip", "B001_*") == 0
    out = capsys.readouterr().out
    assert "B001_C001_1012CD.mp4" in out and "_V.mxf" not in out


def test_dry_run_touches_nothing(config_path, day, valid_config):
    roots = [Path(valid_config["paths"][key]) for key in ("output_root", "scratch", "log_dir", "manifest_dir")]
    before = {root: sorted(p.name for p in root.rglob("*")) for root in roots}
    manifest_file = manifest_path(Path(valid_config["paths"]["manifest_dir"]), "2026-10-12", "MU")
    stamp = os.stat(manifest_file).st_mtime_ns
    run("render", "--config", str(config_path), "--day", "2026-10-12", "--dry-run")
    assert {root: sorted(p.name for p in root.rglob("*")) for root in roots} == before
    assert os.stat(manifest_file).st_mtime_ns == stamp


def test_missing_manifest_exits_2(config_path, capsys):
    assert run("render", "--config", str(config_path), "--day", "2026-10-13", "--dry-run") == 2
    assert "manifest" in capsys.readouterr().err


def test_unit_is_required_when_the_show_has_several(valid_config, write_config, mutate, capsys):
    path = write_config(mutate(valid_config, "show.units", ["MU", "2U"]))
    assert run("render", "--config", str(path), "--day", "2026-10-12", "--dry-run") == 2
    assert "--unit" in capsys.readouterr().err


def test_unknown_unit_exits_2(config_path, capsys):
    assert run("render", "--config", str(config_path), "--day", "2026-10-12", "--unit", "9U", "--dry-run") == 2
    assert "9U" in capsys.readouterr().err


# -------------------------------------------------------- not built yet


@pytest.mark.parametrize("stage", ["qc", "publish", "run"])
def test_stages_that_are_not_built_yet_say_so(config_path, stage, capsys):
    assert run(stage, "--config", str(config_path), "--day", "2026-10-12", "--dry-run") == 2
    assert "not built yet" in capsys.readouterr().err


def test_render_without_dry_run_refuses_to_execute(config_path, day, capsys):
    assert run("render", "--config", str(config_path), "--day", "2026-10-12") == 2
    assert "--dry-run" in capsys.readouterr().err


def test_bad_day_exits_2(config_path, capsys):
    assert run("render", "--config", str(config_path), "--day", "12-10-2026", "--dry-run") == 2
    assert "ISO date" in capsys.readouterr().err


def test_jobs_must_be_positive(config_path, capsys):
    with pytest.raises(SystemExit) as caught:
        run("render", "--config", str(config_path), "--day", "2026-10-12", "--dry-run", "--jobs", "0")
    assert caught.value.code == 2


def test_jobs_is_accepted(config_path, day):
    assert run("render", "--config", str(config_path), "--day", "2026-10-12", "--dry-run", "--jobs", "4",
               "--clip", "B001_*") == 0


def test_skip_path_checks_needs_dry_run(config_path, capsys):
    assert run("render", "--config", str(config_path), "--day", "2026-10-12", "--skip-path-checks") == 2
    assert "only valid together with --dry-run" in capsys.readouterr().err


def test_skip_path_checks_lets_a_dry_run_work_away_from_the_volumes(valid_config, write_config, mutate, day, capsys):
    data = mutate(valid_config, "paths.camera_root", "/Volumes/NOT_MOUNTED/CAMERA")
    args = ("render", "--config", str(write_config(data)), "--day", "2026-10-12", "--dry-run", "--clip", "B001_*")
    assert run(*args) == 2
    capsys.readouterr()
    assert run(*args, "--skip-path-checks") == 0


def test_anamorphic_warning_reaches_the_operator(config_path, valid_config, capsys):
    m = Manifest.new(day="2026-10-12", day_code="SD014", unit="MU", show="TST", config_hash="sha256:x",
                     tools={}, created="2026-10-12T19:00:00Z")
    m.add_clip("A001_C009_1012AB", source=SOURCE, picture={**PICTURE, "height": 6864, "anamorphic": 2.0}, mob_id=None)
    m.set_block("A001_C009_1012AB", stage="sync", name="sound", block=SOUND)
    m.set_block("A001_C009_1012AB", stage="grade", name="colour", block=COLOUR)
    m.save(manifest_path(Path(valid_config["paths"]["manifest_dir"]), "2026-10-12", "MU"))
    assert run("render", "--config", str(config_path), "--day", "2026-10-12", "--dry-run") == 0
    assert "warning" in capsys.readouterr().err


def test_check_does_not_mention_padding_when_the_picture_fills_the_raster(valid_config, write_config, mutate, capsys):
    path = write_config(mutate(mutate(valid_config, "decode.expected_resolution.A", [7680, 4320]), "decode.expected_resolution.B", [7680, 4320]))
    assert run("check", "--config", str(path)) == 0
    out = capsys.readouterr().out
    assert "avid, cameras A B: crop 1824x1026 at 48,27, scale 1920x1080\n" in out
    assert "pad" not in out
