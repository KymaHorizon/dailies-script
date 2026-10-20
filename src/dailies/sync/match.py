# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Sync matching. Pure functions over integers in the sample domain.

At 50 fps and 48 kHz one frame is exactly 960 samples, so every picture
boundary is a whole number of samples and nothing here ever rounds. Sound files
start wherever the recorder was started, which is almost never on a frame
boundary, and that is fine: the offset is simply an integer number of samples.

    offset = picture_start - sound_start

Positive: the sound started before the picture, trim that many samples off its
head. Negative: the sound started late, pad the head with silence.

Timecode appears in this module only to turn the picture start into samples and
to describe a result to a human.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from dailies.timecode import frames_to_tc, tc_to_frames

# A second file has to cover at least this share of the clip before the pair is
# called ambiguous. It keeps the head of the next take, or the second half of a
# take recorded in two files, from sending every clip to review.
SUBSTANTIAL_OVERLAP = 0.5


class SyncMathError(ValueError):
    """The rates do not give whole samples per frame, so exact sync is impossible."""


@dataclass(frozen=True)
class PictureRange:
    clip_id: str
    start: int  # samples since midnight
    end: int  # exclusive
    scene: str | None
    take: str | None
    mos: bool


@dataclass(frozen=True)
class SoundRange:
    path: str
    start: int  # BWF time_reference, samples since midnight
    end: int  # exclusive, not wrapped at midnight
    scene: str | None
    take: str | None
    soundroll: str
    channels: int
    track_names: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    path: str
    offset_samples: int
    overlap_samples: int
    slate_agrees: bool | None  # None when either side has no slate, which is no opinion


@dataclass(frozen=True)
class SyncResult:
    status: str
    chosen: SoundRange | None
    offset_samples: int | None
    candidates: tuple[Candidate, ...]
    flags: tuple[str, ...]
    note: str


def _samples_per_frame(fps: int, rate: int) -> int:
    if rate % fps:
        raise SyncMathError(f"{rate} Hz at {fps} fps is not a whole number of samples per frame")
    return rate // fps


def picture_range(start_tc: str, frames: int, *, fps: int, rate: int) -> tuple[int, int]:
    spf = _samples_per_frame(fps, rate)
    start = tc_to_frames(start_tc, fps=fps) * spf
    return start, start + frames * spf


def samples_to_tc(samples: int, *, fps: int, rate: int) -> str:
    """For display only. The sample position inside the frame is dropped."""
    return frames_to_tc(samples // _samples_per_frame(fps, rate), fps=fps)


def wrap_offset(offset: int, *, rate: int) -> int:
    """A night shoot crossing midnight puts picture and sound a day apart in timecode. An offset of
    more than twelve hours is taken the short way round the clock."""
    day = 24 * 3600 * rate
    if offset > day // 2:
        return offset - day
    if offset < -(day // 2):
        return offset + day
    return offset


def _normal(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    text = value.strip().upper()
    return text.lstrip("0") or "0" if text.isdigit() else text


def _slate_agrees(picture: PictureRange, sound: SoundRange) -> bool | None:
    ours = (_normal(picture.scene), _normal(picture.take))
    theirs = (_normal(sound.scene), _normal(sound.take))
    if None in ours or None in theirs:
        return None
    return ours == theirs


def match(picture: PictureRange, sounds: Sequence[SoundRange], *, fps: int, rate: int,
          window_samples: int) -> SyncResult:
    if picture.mos:
        return SyncResult("mos", None, None, (), (), "marked MOS")

    length = picture.end - picture.start
    measured: list[tuple[Candidate, SoundRange, int, bool]] = []  # candidate, sound, gap, wrapped
    for sound in sounds:
        raw = picture.start - sound.start
        offset = wrap_offset(raw, rate=rate)
        start = sound.start + offset  # the picture start on this sound file's clock
        end = start + length
        overlap = min(end, sound.end) - max(start, sound.start)
        gap = max(sound.start - end, start - sound.end, 0)
        measured.append((Candidate(sound.path, offset, max(overlap, 0), _slate_agrees(picture, sound)),
                         sound, gap, offset != raw))

    def rank(entry: tuple[Candidate, SoundRange, int, bool]):
        candidate, sound, _, _ = entry
        return (-candidate.overlap_samples, candidate.slate_agrees is not True, sound.start, sound.path)

    overlapping = sorted((m for m in measured if m[0].overlap_samples > 0), key=rank)
    if overlapping:
        best, chosen, _, wrapped = overlapping[0]
        substantial = [m for m in overlapping if m[0].overlap_samples >= SUBSTANTIAL_OVERLAP * length]
        if len(substantial) >= 2:
            status, flags = "multiple_candidates", ["sync_review"]
            note = f"{len(substantial)} sound files cover this clip, the longest overlap was chosen"
        elif best.overlap_samples == length:
            status, flags, note = "matched", [], "sound covers the whole clip"
        else:
            status, flags = "partial_overlap", ["sound_gap"]
            missing = samples_to_tc(length - best.overlap_samples, fps=fps, rate=rate)
            note = f"sound is missing for {missing} of this clip, the gap is silence"
        if best.slate_agrees is False:
            flags.append("slate_mismatch")
            note += f". Slate mismatch: picture {picture.scene}/{picture.take}, sound {chosen.scene}/{chosen.take}"
        if wrapped:
            note += ". Offset corrected by 24 hours across midnight"
        return SyncResult(status, chosen, best.offset_samples, tuple(m[0] for m in overlapping), tuple(flags), note)

    slated = sorted((m for m in measured if m[0].slate_agrees), key=lambda m: (abs(m[0].offset_samples), m[1].path))
    if slated:
        candidate, sound, _, _ = slated[0]
        away = samples_to_tc(abs(candidate.offset_samples), fps=fps, rate=rate)
        note = (f"sound slated {sound.scene}/{sound.take} exists in {sound.path} but its timecode is {away} away "
                "from the picture, the recorder clock was probably wrong")
        return SyncResult("tc_implausible", None, None, (candidate,), ("tc_implausible",), note)
    if measured:
        candidate, sound, gap, _ = min(measured, key=lambda m: (m[2], m[1].path))
        if gap > window_samples:
            away = samples_to_tc(gap, fps=fps, rate=rate)
            note = f"the nearest sound is {away} away, jam sync was probably lost"
            return SyncResult("tc_implausible", None, None, (candidate,), ("tc_implausible",), note)
    return SyncResult("no_sound", None, None, (), ("no_sound",), "no sound file overlaps this clip")
