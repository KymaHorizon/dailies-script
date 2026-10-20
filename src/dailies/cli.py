# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""dailies command line.

Exit codes: 0 everything requested was done, 1 completed with clips that failed,
2 could not start (bad config, missing manifest, bad arguments).

Built so far: `check`, `scan`, `sync`, `grade`, `verify-colour`, and `render --dry-run`. A dry run prints every
command and writes nothing anywhere: no manifest, no log, no media.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import io
import os
import re
import sys
from typing import Sequence

from dailies.commands.base import CommandLog, DryRunExecutor, SubprocessExecutor
from dailies.commands.render import BuildError, build_render_plan
from dailies.commands.verify import VerifyError, build_verify_command, parse_psnr
from dailies.config import ConfigError, ShowConfig, load_config
from dailies.manifest import Manifest, ManifestError, manifest_path
from dailies.naming import DayContext, NamingError
from dailies.grade.stage import GradeAbort, run_grade
from dailies.scan.stage import ScanAbort, run_scan
from dailies.sync.stage import run_sync

STAGES = ("scan", "sync", "grade", "render", "qc", "publish", "run")
EXIT_OK, EXIT_CLIP_FAILURES, EXIT_CANNOT_START = 0, 1, 2


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dailies", description="Dailies pipeline for a 50 fps RED show.")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="validate the show config and print the resolved framing")
    check.add_argument("--config", required=True)
    check.add_argument("--skip-path-checks", action="store_true",
                       help="validate values only, for use away from the show volumes")
    for stage in STAGES:
        p = sub.add_parser(stage)
        p.add_argument("--config", required=True)
        p.add_argument("--day", required=True, help="shoot date, ISO format, for example 2026-10-12")
        p.add_argument("--unit", help="crew unit, required when the show has more than one")
        p.add_argument("--clip", help="one clip id or a glob")
        p.add_argument("--force", action="store_true", help="ignore settings hashes and redo the work")
        p.add_argument("--dry-run", action="store_true", help="print every command, execute nothing")
        p.add_argument("--jobs", type=_positive, help="override concurrency")
        p.add_argument("--skip-path-checks", action="store_true", help="only valid together with --dry-run")
        if stage in ("scan", "run"):
            p.add_argument("--day-code", help="shoot day code such as SD014, required the first time a day is scanned")
    verify = sub.add_parser("verify-colour", help="compare the render's colour chain for one clip with a graded reference frame")
    verify.add_argument("--config", required=True)
    verify.add_argument("--day", required=True)
    verify.add_argument("--unit")
    verify.add_argument("--clip", required=True, help="a graded clip id, its colour block is what gets checked")
    verify.add_argument("--source", required=True, help="one log frame as the decoder produces it, 16-bit TIFF, DPX or PNG")
    verify.add_argument("--reference", required=True, help="the same frame graded by the colourist, same size, full range RGB")
    verify.add_argument("--min-psnr", type=float, default=50.0, help="pass mark in dB, default 50")
    verify.add_argument("--skip-path-checks", action="store_true")
    return parser


def _fail(message: str) -> int:
    print(f"dailies: {message}", file=sys.stderr)
    return EXIT_CANNOT_START


def _check(config: ShowConfig) -> int:
    print(f"config ok: {config.show.name} ({config.show.code}), {config.show.fps} fps, "
          f"{config.show.samples_per_frame} samples per frame")
    print(f"config hash: {config.config_hash}")
    for name in config.deliverables:
        groups: dict = {}
        for camera in config.show.cameras:
            groups.setdefault(config.nominal_framing(name, camera), []).append(camera)
        for f, cameras in groups.items():
            line = (f"{name}, cameras {' '.join(cameras)}: crop {f.crop[0]}x{f.crop[1]} at "
                    f"{f.crop_offset[0]},{f.crop_offset[1]}, scale {f.scale[0]}x{f.scale[1]}")
            if f.scale != f.output:
                line += f", pad to {f.output[0]}x{f.output[1]}"
            print(line)
    return EXIT_OK


def _render_dry_run(config: ShowConfig, manifest: Manifest, day: DayContext, args: argparse.Namespace) -> int:
    clips = [c for c in manifest.clips if args.clip is None or fnmatch.fnmatchcase(c["id"], args.clip)]
    if not clips:
        return _fail(f"no clips match {args.clip!r} in the manifest for {manifest.day}"
                     if args.clip else f"the manifest for {manifest.day} has no clips")
    executor = DryRunExecutor(sys.stdout, CommandLog(io.StringIO()))
    failures = 0
    for clip in clips:
        clip_id = clip["id"]
        try:
            plan = build_render_plan(config, day, clip)
            stale = tuple(t for t, h in plan.settings_hashes.items()
                          if args.force or not manifest.is_current(clip_id, t, h))
            if not stale:
                print(f"# render {clip_id}: up to date, skipped")
                continue
            if stale != tuple(plan.settings_hashes):
                plan = build_render_plan(config, day, clip, targets=stale)
        except (BuildError, NamingError) as error:
            failures += 1
            print(f"dailies: cannot build {error}", file=sys.stderr)
            continue
        for warning in plan.warnings:
            print(f"dailies: warning: {warning}", file=sys.stderr)
        for runnable in plan.runnables:
            executor.run(runnable, clip=clip_id, stage="render")
    return EXIT_CLIP_FAILURES if failures else EXIT_OK


def _scan(config: ShowConfig, args: argparse.Namespace, unit: str) -> int:
    path = manifest_path(config.paths.manifest_dir, args.day, unit)
    day_code = args.day_code
    if day_code is not None and not re.fullmatch(r"[A-Za-z0-9]+", day_code):
        return _fail(f"--day-code {day_code!r} must be letters and digits only, it is used in folder names")
    if path.exists():
        try:
            manifest = Manifest.load(path)
        except ManifestError as error:
            return _fail(str(error))
        existing = manifest.to_dict()["day_code"]
        if day_code is not None and day_code != existing:
            return _fail(f"the manifest for {args.day} already has day code {existing}, refusing {day_code}")
        day_code = existing
    elif day_code is None:
        return _fail(f"no manifest yet for {args.day} {unit}, pass --day-code to create it")
    else:
        created = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest = Manifest.new(day=args.day, day_code=day_code, unit=unit, show=config.show.code,
                                config_hash=config.config_hash, tools={}, created=created)
    day = DayContext(show=config.show.code, day=args.day, day_code=day_code, unit=unit)

    log_file = None
    if args.dry_run:
        executor = DryRunExecutor(sys.stdout, CommandLog(io.StringIO()))
    else:
        log_file = open(config.paths.log_dir / f"{args.day}_{unit}.commands.jsonl", "a")
        executor = SubprocessExecutor(CommandLog(log_file))
    try:
        summary = run_scan(config, manifest, day, executor)
    except ScanAbort as error:
        return _fail(str(error))
    finally:
        if log_file is not None:
            log_file.close()
    for warning in summary.warnings:
        print(f"dailies: warning: {warning}", file=sys.stderr)
    if args.dry_run:
        return EXIT_OK
    manifest.save(path)
    for error in manifest.to_dict()["scan"]["errors"]:
        print(f"dailies: scan failed for {error['clip']}: {error['message']}", file=sys.stderr)
    clips = summary.clips_scanned
    print(f"{clips} clip{'' if clips == 1 else 's'} scanned, {summary.sound_files} sound file(s), "
          f"{summary.clips_failed} failed, {summary.folders_skipped} folder(s) skipped. Manifest: {path}")
    return EXIT_CLIP_FAILURES if summary.clips_failed else EXIT_OK


def _grade(config: ShowConfig, manifest: Manifest, path, day: DayContext, args: argparse.Namespace) -> int:
    if args.clip is not None and not any(fnmatch.fnmatchcase(c["id"], args.clip) for c in manifest.clips):
        return _fail(f"no clips match {args.clip!r} in the manifest for {manifest.day}")
    try:
        summary = run_grade(config, manifest, day, clip_filter=args.clip, dry_run=args.dry_run)
    except GradeAbort as error:
        return _fail(str(error))
    for line in summary.lines:
        print(line)
    for warning in summary.warnings:
        print(f"dailies: warning: {warning}", file=sys.stderr)
    counts = ", ".join(f"{count} {status}" for status, count in sorted(summary.by_cdl.items())) or "nothing to grade"
    if args.dry_run:
        print(f"{counts}. Dry run, the manifest was not changed.")
    else:
        manifest.save(path)
        print(f"{counts}, {summary.cubes_baked} cube(s) baked, colour applied {config.colour.apply}. Manifest: {path}")
    return EXIT_OK


def _verify(config: ShowConfig, manifest: Manifest, unit: str, args: argparse.Namespace) -> int:
    try:
        clip = manifest.clip(args.clip)
    except ManifestError as error:
        return _fail(str(error))
    if clip["colour"] is None:
        return _fail(f"{args.clip} has no colour block yet, run grade first")
    for name in (args.source, args.reference):
        if not os.path.isfile(name):
            return _fail(f"frame not found: {name}")
    command = build_verify_command(config, clip["colour"], args.source, args.reference)
    with open(config.paths.log_dir / f"{args.day}_{unit}.commands.jsonl", "a") as log_file:
        result = SubprocessExecutor(CommandLog(log_file)).run(command, clip=args.clip, stage="verify-colour")
    if not result.ok:
        return _fail(f"FFmpeg could not compare the frames: {result.stderr.strip()[-400:]}")
    try:
        psnr = parse_psnr(result.stderr)
    except VerifyError as error:
        return _fail(str(error))
    passed = psnr >= args.min_psnr
    print(f"{args.clip}: PSNR {psnr:.2f} dB against the reference, colour applied {clip['colour']['apply']}. "
          f"{'PASS' if passed else 'FAIL'} at {args.min_psnr:g} dB")
    return EXIT_OK if passed else EXIT_CLIP_FAILURES


def _sync(config: ShowConfig, manifest: Manifest, path, args: argparse.Namespace) -> int:
    if args.clip is not None and not any(fnmatch.fnmatchcase(c["id"], args.clip) for c in manifest.clips):
        return _fail(f"no clips match {args.clip!r} in the manifest for {manifest.day}")
    summary = run_sync(config, manifest, clip_filter=args.clip)
    for line in summary.lines:
        print(line)
    for name in summary.excluded_sound:
        print(f"dailies: warning: sound file left out of sync: {name}", file=sys.stderr)
    for warning in summary.warnings:
        print(f"dailies: warning: {warning}", file=sys.stderr)
    counts = ", ".join(f"{count} {status}" for status, count in sorted(summary.by_status.items())) or "nothing to sync"
    if args.dry_run:
        print(f"{counts}. Dry run, the manifest was not changed.")
    else:
        manifest.save(path)
        print(f"{counts}. Manifest: {path}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dry_run = getattr(args, "dry_run", False)
    if args.skip_path_checks and args.command != "check" and not dry_run:
        return _fail("--skip-path-checks is only valid together with --dry-run")
    try:
        config = load_config(args.config, check_paths=not args.skip_path_checks)
    except ConfigError as error:
        return _fail(str(error))
    if args.command == "check":
        return _check(config)

    try:
        _dt.date.fromisoformat(args.day)
    except ValueError:
        return _fail(f"--day {args.day!r} is not an ISO date like 2026-10-12")
    unit = args.unit or (config.show.units[0] if len(config.show.units) == 1 else None)
    if unit is None:
        return _fail(f"this show has units {list(config.show.units)}, pass --unit")
    if unit not in config.show.units:
        return _fail(f"unknown unit {unit!r}, the config lists {list(config.show.units)}")
    if args.command == "scan":
        return _scan(config, args, unit)
    if args.command not in ("sync", "grade", "render", "verify-colour"):
        return _fail(f"the {args.command} stage is not built yet")
    if args.command == "render" and not dry_run:
        return _fail("render can only be run with --dry-run at this build step")

    path = manifest_path(config.paths.manifest_dir, args.day, unit)
    try:
        manifest = Manifest.load(path)
    except ManifestError as error:
        return _fail(str(error))
    if args.command == "sync":
        return _sync(config, manifest, path, args)
    if args.command == "verify-colour":
        return _verify(config, manifest, unit, args)
    if args.command == "grade":
        header = manifest.to_dict()
        return _grade(config, manifest, path,
                      DayContext(show=config.show.code, day=header["day"], day_code=header["day_code"], unit=unit), args)
    header = manifest.to_dict()
    day = DayContext(show=config.show.code, day=header["day"], day_code=header["day_code"], unit=header["unit"])
    return _render_dry_run(config, manifest, day, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
