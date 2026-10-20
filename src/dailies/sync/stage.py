# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Sync stage. Works from the manifest alone: the clips and the sound inventory that scan recorded.

Nothing here reads the filesystem or runs a command, so a sync is instant, repeatable, and safe to
rerun whenever the sound inventory or a slate changes. A clip with no usable sound still gets a sound
block and still renders, mute. Nothing is held back because sync failed.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import PurePath

from dailies.config import ShowConfig
from dailies.manifest import Manifest, derive_status
from dailies.sync.match import PictureRange, SoundRange, match, picture_range, samples_to_tc

SYNC_FLAGS = {"sound_gap", "sync_review", "slate_mismatch", "tc_implausible", "no_sound"}
AUDIBLE = ("matched", "partial_overlap", "multiple_candidates")
_MOS_WORD = re.compile(r"\bMOS\b", re.IGNORECASE)


@dataclass
class SyncSummary:
    by_status: dict[str, int] = field(default_factory=dict)
    excluded_sound: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)  # one human readable line per clip


def _usable_sound(manifest: Manifest, summary: SyncSummary) -> list[SoundRange]:
    usable = []
    for entry in manifest.sound_files:
        name = PurePath(entry["path"]).name
        blocking = [flag for flag in entry["flags"] if flag in ("no_time_reference", "wrong_sample_rate")]
        if blocking or entry["start_samples"] is None:
            summary.excluded_sound.append(f"{name}: {', '.join(blocking) or 'no_time_reference'}")
            continue
        usable.append(SoundRange(entry["path"], entry["start_samples"], entry["end_samples"], entry["scene"],
                                 entry["take"], entry["soundroll"], entry["channels"], tuple(entry["track_names"])))
    return usable


def _is_mos(config: ShowConfig, picture: dict) -> bool:
    if picture["camera"] in config.sound.mos_cameras:
        return True
    return any(_MOS_WORD.search(picture.get(key) or "") for key in ("scene", "take"))


def run_sync(config: ShowConfig, manifest: Manifest, *, clip_filter: str | None = None) -> SyncSummary:
    summary = SyncSummary()
    fps, rate = config.show.fps, config.show.audio_rate
    window = int(config.sound.tc_window_hours * 3600 * rate)
    sounds = _usable_sound(manifest, summary)
    targets = tuple(config.deliverables)

    for clip in manifest.clips:
        clip_id, picture = clip["id"], clip["picture"]
        if clip_filter is not None and not fnmatch.fnmatchcase(clip_id, clip_filter):
            continue
        if picture["fps"] != fps:
            summary.warnings.append(f"{clip_id}: clip is {picture['fps']:g} fps, not synced and never conformed")
            continue
        # Time of day is what the recorder shares with the camera. If the camera displays edge code,
        # the absolute track still carries time of day, so sync always prefers it.
        start_tc = picture.get("tc_absolute") or picture["start_tc"]
        start, end = picture_range(start_tc, picture["frames"], fps=fps, rate=rate)
        result = match(PictureRange(clip_id, start, end, picture["scene"], picture["take"], _is_mos(config, picture)),
                       sounds, fps=fps, rate=rate, window_samples=window)

        block: dict = {"status": result.status, "note": result.note}
        if result.status in AUDIBLE:
            chosen = result.chosen
            block.update(files=[chosen.path], soundroll=chosen.soundroll, offset_samples=result.offset_samples,
                         start_tc=samples_to_tc(chosen.start, fps=fps, rate=rate), channels=chosen.channels,
                         track_names=list(chosen.track_names))
        if result.candidates and result.status != "matched":
            block["candidates"] = [dataclasses.asdict(candidate) for candidate in result.candidates]
        manifest.set_block(clip_id, stage="sync", name="sound", block=block)
        manifest.replace_flags(clip_id, owned=SYNC_FLAGS, current=list(result.flags))
        manifest.set_status(clip_id, derive_status(manifest.clip(clip_id), targets))

        summary.by_status[result.status] = summary.by_status.get(result.status, 0) + 1
        detail = f"offset {result.offset_samples} samples, {PurePath(result.chosen.path).name}" \
            if result.chosen else result.note
        summary.lines.append(f"{clip_id}  {result.status}  {detail}")
        if result.flags:
            summary.warnings.append(f"{clip_id}: {result.status}: {result.note}")
    return summary
