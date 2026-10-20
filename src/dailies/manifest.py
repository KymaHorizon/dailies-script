# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Day manifest: one JSON file per shoot day and unit, the only state that matters.

- Strict schema. Unknown keys are errors, so a hand edit or a typo is caught on
  load instead of being silently carried along.
- Each stage owns its blocks and may not write anyone else's.
- The clip MobID is assigned once, when the clip first enters the manifest,
  and survives rescans and re-renders.
- Saves are atomic: temp file in the same folder, fsync, rename, fsync folder.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any, Iterable

from dailies.mobid import MobIdError, new_mob_id, validate_mob_id
from dailies.timecode import TimecodeError, tc_to_frames

SCHEMA_VERSION = 1

CLIP_STATUSES = ("scanned", "synced", "graded", "rendering", "rendered", "qc_pass", "qc_fail", "published",
                 "quarantined")
SOUND_STATUSES = ("matched", "no_sound", "partial_overlap", "multiple_candidates", "tc_implausible", "mos")
QC_RESULTS = ("pass", "fail")
COLOUR_APPLY = {"exact": "show_lut", "baked": "baked_lut"}  # apply mode -> the key that mode cannot do without

# which stage may write which clip block
STAGE_BLOCKS: dict[str, tuple[str, ...]] = {
    "scan": ("source", "picture"),
    "sync": ("sound",),
    "grade": ("colour",),
    "render": ("framing",),
}

_HEADER_KEYS = ("schema_version", "day", "day_code", "unit", "show", "config_hash", "created", "tools", "scan",
                "sound_files", "clips")
_CLIP_KEYS = ("id", "status", "mob_id", "source", "picture", "sound", "colour", "framing", "outputs", "flags",
              "errors")

# block name -> (required keys, optional keys)
_BLOCK_KEYS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "source": (("path", "size_bytes", "mhl_verified"), ("kind", "parts")),
    "picture": (
        ("camera", "roll", "clip", "reel", "width", "height", "fps", "frames", "start_tc", "end_tc", "anamorphic",
         "iso", "kelvin", "tint", "scene", "take"),
        ("tc_absolute", "tc_edge", "tc_preference", "camera_model", "dropped_frames", "codec"),
    ),
    "sound": (
        ("status",),
        ("files", "soundroll", "start_tc", "offset_samples", "channels", "track_names", "candidates", "note"),
    ),
    "colour": (("apply", "lut_hash"), ("baked_lut", "show_lut", "cdl", "cdl_source", "camera_look", "note")),
    "framing": (("decode", "crop", "crop_offset", "scale", "pad_offset", "output", "fit"), ("desqueezed",)),
    "output": (("target", "settings_hash", "video", "audio", "rendered"), ("qc",)),
    "sound_file": (
        ("path", "size_bytes", "mhl_verified", "soundroll", "scene", "take", "metadata_source", "track_names",
         "sample_rate", "channels", "bit_depth", "sample_count", "start_samples", "end_samples", "flags"),
        (),
    ),
}


class ManifestError(Exception):
    """The manifest is malformed, or a stage tried to do something it may not."""


def manifest_path(manifest_dir: Path, day: str, unit: str) -> Path:
    return Path(manifest_dir) / f"{day}_{unit}.json"


def _check_keys(block: Any, name: str) -> None:
    if not isinstance(block, dict):
        raise ManifestError(f"{name}: expected a mapping")
    required, optional = _BLOCK_KEYS[name]
    for key in required:
        if key not in block:
            raise ManifestError(f"{name}.{key}: missing")
    for key in block:
        if key not in required and key not in optional:
            raise ManifestError(f"{name}.{key}: unknown key")


def _check_block(block: Any, name: str) -> None:
    _check_keys(block, name)
    if name == "picture":
        # checked at the clip's own rate, so that a clip shot at the wrong rate can be recorded and flagged
        try:
            rate = max(1, round(float(block["fps"])))
        except (TypeError, ValueError):
            raise ManifestError(f"picture.fps: expected a number, got {block['fps']!r}") from None
        for key in ("start_tc", "end_tc"):
            try:
                tc_to_frames(block[key], fps=rate)
            except (TimecodeError, TypeError) as error:
                raise ManifestError(f"picture.{key}: {error}") from None
        if isinstance(block["frames"], bool) or not isinstance(block["frames"], int) or block["frames"] < 1:
            raise ManifestError(f"picture.frames: expected a positive integer, got {block['frames']!r}")
    elif name == "sound":
        if block["status"] not in SOUND_STATUSES:
            raise ManifestError(f"sound.status: {block['status']!r} is not one of {list(SOUND_STATUSES)}")
        offset = block.get("offset_samples")
        if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int)):
            raise ManifestError(f"sound.offset_samples: expected a whole number of samples, got {offset!r}")
    elif name == "colour":
        if block["apply"] not in COLOUR_APPLY:
            raise ManifestError(f"colour.apply: {block['apply']!r} is not one of {list(COLOUR_APPLY)}")
        needed = COLOUR_APPLY[block["apply"]]
        if not block.get(needed):
            raise ManifestError(f"colour.{needed}: required when colour is applied {block['apply']}")
    elif name == "output":
        if "qc" in block:
            _check_qc(block["qc"])
    elif name == "sound_file":
        for key in ("start_samples", "end_samples", "sample_count"):
            value = block[key]
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ManifestError(f"sound_file.{key}: expected a whole number of samples, got {value!r}")


def _check_qc(qc: Any) -> None:
    if not isinstance(qc, dict) or qc.get("result") not in QC_RESULTS:
        raise ManifestError(f"qc.result: expected one of {list(QC_RESULTS)}, got {qc!r}")


def _check_clip(clip: Any) -> None:
    if not isinstance(clip, dict):
        raise ManifestError("clip: expected a mapping")
    for key in clip:
        if key not in _CLIP_KEYS:
            raise ManifestError(f"clip.{key}: unknown key")
    for key in _CLIP_KEYS:
        if key not in clip:
            raise ManifestError(f"clip.{key}: missing in clip {clip.get('id')!r}")
    if clip["status"] not in CLIP_STATUSES:
        raise ManifestError(f"clip.status: {clip['status']!r} is not one of {list(CLIP_STATUSES)}")
    try:
        validate_mob_id(clip["mob_id"])
    except MobIdError as error:
        raise ManifestError(f"clip.mob_id: {error}") from None
    _check_block(clip["source"], "source")
    _check_block(clip["picture"], "picture")
    for name in ("sound", "colour", "framing"):
        if clip[name] is not None:
            _check_block(clip[name], name)
    for output in clip["outputs"]:
        _check_block(output, "output")


def derive_status(clip: dict, targets: Iterable[str]) -> str:
    """Status as a function of the clip's blocks. Quarantine is sticky until cleared."""
    if clip["status"] == "quarantined":
        return "quarantined"
    outputs = {o["target"]: o for o in clip["outputs"]}
    if any(o.get("qc", {}).get("result") == "fail" for o in outputs.values()):
        return "qc_fail"
    targets = tuple(targets)
    if targets and all(t in outputs for t in targets):
        if all(outputs[t].get("qc", {}).get("result") == "pass" for t in targets):
            return "qc_pass"
        return "rendered"
    if clip["colour"] is not None:
        return "graded"
    if clip["sound"] is not None:
        return "synced"
    return "scanned"


class Manifest:
    def __init__(self, data: dict):
        self._data = data

    # ------------------------------------------------------------ creation

    @classmethod
    def new(cls, *, day: str, day_code: str, unit: str, show: str, config_hash: str, tools: dict,
            created: str) -> "Manifest":
        try:
            _dt.date.fromisoformat(day)
        except (TypeError, ValueError):
            raise ManifestError(f"day: expected an ISO date like 2026-10-12, got {day!r}") from None
        return cls({
            "schema_version": SCHEMA_VERSION, "day": day, "day_code": day_code, "unit": unit, "show": show,
            "config_hash": config_hash, "created": created, "tools": copy.deepcopy(tools),
            "scan": {"folders": [], "errors": []}, "sound_files": [], "clips": [],
        })

    @classmethod
    def load(cls, path: Path | str) -> "Manifest":
        path = Path(path)
        try:
            text = path.read_text()
        except OSError as error:
            raise ManifestError(f"manifest not found or unreadable: {error}") from None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise ManifestError(f"{path}: not valid JSON, the file may be truncated: {error}") from None
        if not isinstance(data, dict):
            raise ManifestError(f"{path}: expected a JSON object")
        for key in data:
            if key not in _HEADER_KEYS:
                raise ManifestError(f"{path}: unknown key {key!r}")
        for key in _HEADER_KEYS:
            if key not in data:
                raise ManifestError(f"{path}: missing key {key!r}")
        if data["schema_version"] != SCHEMA_VERSION:
            raise ManifestError(
                f"{path}: schema_version {data['schema_version']!r} is not supported, this build reads "
                f"version {SCHEMA_VERSION}"
            )
        for clip in data["clips"]:
            _check_clip(clip)
        for entry in data["sound_files"]:
            _check_block(entry, "sound_file")
        return cls(data)

    # -------------------------------------------------------------- access

    @property
    def day(self) -> str:
        return self._data["day"]

    @property
    def tools(self) -> dict:
        return copy.deepcopy(self._data["tools"])

    @property
    def clips(self) -> list[dict]:
        return copy.deepcopy(self._data["clips"])

    @property
    def sound_files(self) -> list[dict]:
        return copy.deepcopy(self._data["sound_files"])

    def to_dict(self) -> dict:
        return copy.deepcopy(self._data)

    def _clip(self, clip_id: str) -> dict:
        for clip in self._data["clips"]:
            if clip["id"] == clip_id:
                return clip
        raise ManifestError(f"no clip {clip_id!r} in the manifest for {self.day}")

    def clip(self, clip_id: str) -> dict:
        return copy.deepcopy(self._clip(clip_id))

    # ---------------------------------------------------------------- scan

    def add_clip(self, clip_id: str, *, source: dict, picture: dict, mob_id: str | None) -> None:
        """Add a scanned clip, or refresh its scan blocks if the same source is scanned again."""
        _check_block(source, "source")
        _check_block(picture, "picture")
        existing = next((c for c in self._data["clips"] if c["id"] == clip_id), None)
        if existing is not None:
            if existing["source"]["path"] != source["path"]:
                raise ManifestError(
                    f"clip id collision: {clip_id!r} already comes from {existing['source']['path']}, "
                    f"refusing to add {source['path']}. Two cameras with the same reel name?"
                )
            existing["source"] = copy.deepcopy(source)
            existing["picture"] = copy.deepcopy(picture)
            return
        self._data["clips"].append({
            "id": clip_id,
            "status": "scanned",
            "mob_id": validate_mob_id(mob_id) if mob_id is not None else new_mob_id(),
            "source": copy.deepcopy(source),
            "picture": copy.deepcopy(picture),
            "sound": None, "colour": None, "framing": None,
            "outputs": [], "flags": [], "errors": [],
        })

    def set_sound_files(self, entries: list[dict]) -> None:
        """The day's sound inventory. Sync works from this and never from the filesystem."""
        for entry in entries:
            _check_block(entry, "sound_file")
        self._data["sound_files"] = copy.deepcopy(entries)

    def set_scan_report(self, *, folders: list[dict], errors: list[dict]) -> None:
        self._data["scan"] = {"folders": copy.deepcopy(folders), "errors": copy.deepcopy(errors)}

    def set_tools(self, tools: dict) -> None:
        self._data["tools"] = copy.deepcopy(tools)

    # -------------------------------------------------------- stage writes

    def set_block(self, clip_id: str, *, stage: str, name: str, block: dict) -> None:
        if stage not in STAGE_BLOCKS:
            raise ManifestError(f"unknown stage {stage!r}")
        if name not in STAGE_BLOCKS[stage]:
            raise ManifestError(f"{stage} may not write the {name!r} block, it owns {list(STAGE_BLOCKS[stage])}")
        _check_block(block, name)
        self._clip(clip_id)[name] = copy.deepcopy(block)

    def set_output(self, clip_id: str, output: dict) -> None:
        """Record a rendered output. Replaces the same target only, and drops its stale QC result."""
        output = {k: v for k, v in copy.deepcopy(output).items() if k != "qc"}
        _check_block(output, "output")
        outputs = self._clip(clip_id)["outputs"]
        for index, existing in enumerate(outputs):
            if existing["target"] == output["target"]:
                outputs[index] = output
                return
        outputs.append(output)

    def set_output_qc(self, clip_id: str, target: str, qc: dict) -> None:
        _check_qc(qc)
        for output in self._clip(clip_id)["outputs"]:
            if output["target"] == target:
                output["qc"] = copy.deepcopy(qc)
                return
        raise ManifestError(f"clip {clip_id!r} has no {target} output to attach a QC result to")

    def is_current(self, clip_id: str, target: str, settings_hash: str) -> bool:
        """True when this target was already rendered with exactly these settings."""
        return any(o["target"] == target and o["settings_hash"] == settings_hash
                   for o in self._clip(clip_id)["outputs"])

    def add_flag(self, clip_id: str, flag: str) -> None:
        flags = self._clip(clip_id)["flags"]
        if flag not in flags:
            flags.append(flag)

    def replace_flags(self, clip_id: str, *, owned: set[str], current: list[str]) -> None:
        """Replace the flags one stage owns, leaving every other stage's flags alone.

        Without this a rerun could only ever add flags, and a fixed problem would stay flagged for ever."""
        foreign = [flag for flag in current if flag not in owned]
        if foreign:
            raise ManifestError(f"flags {foreign} are not owned by the stage setting them, it owns {sorted(owned)}")
        clip = self._clip(clip_id)
        kept = [flag for flag in clip["flags"] if flag not in owned]
        clip["flags"] = kept + [flag for flag in dict.fromkeys(current)]

    def set_status(self, clip_id: str, status: str) -> None:
        if status not in CLIP_STATUSES:
            raise ManifestError(f"status {status!r} is not one of {list(CLIP_STATUSES)}")
        self._clip(clip_id)["status"] = status

    def quarantine(self, clip_id: str, *, stage: str, message: str, stderr: str) -> None:
        clip = self._clip(clip_id)
        clip["errors"].append({"stage": stage, "message": message, "stderr": stderr})
        clip["status"] = "quarantined"

    # --------------------------------------------------------- persistence

    def save(self, path: Path | str) -> None:
        path = Path(path)
        text = json.dumps(self._data, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with open(temp, "w") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        folder = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
