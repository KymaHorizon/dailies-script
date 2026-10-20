# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Render plan: one decode, one FFmpeg filter graph, every deliverable.

Built from the immutable config plus one manifest clip. Nothing is executed and
nothing on disk is touched here. The plan carries the commands, the temp and
final path of every output, and one settings hash per deliverable.

Rules enforced here:
- Frame rate is stated on the picture input only. Every video output carries
  -fps_mode passthrough and no output -r, because FFmpeg rejects the two
  together. QC asserts the 50/1 rate and the frame count afterwards.
- The baked LUT is applied once, in 16-bit RGB, tetrahedral, before crop and scale.
- The RGB to YUV conversion is folded into the final scale, BT.709, legal range,
  and every output is tagged to match.
- Audio is synced once in samples, padded and trimmed to the exact picture
  length, then split per output.
- Outputs are written to a temp name in the destination folder. Renaming them
  into place is the executor's job, after success.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path, PurePath

from dailies.commands.base import Command, Pipeline, Runnable
from dailies.commands.filters import FilterError, cdl_filters, drawtext, filter_value, mix_pan, sync_audio_filters
from dailies.config import Deliverable, ShowConfig, hash_of
from dailies.decoder import Decoder, make_decoder
from dailies.framing import Framing, FramingError, compute_framing, decode_size
from dailies.grade.cdl import Cdl
from dailies.naming import DayContext, burn_in_texts, clip_names, day_folder

AUDIBLE = ("matched", "partial_overlap", "multiple_candidates")
_COLOUR_TAGS = ("-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv")
_SCALE_COLOUR = ("out_color_matrix=bt709", "out_range=tv")
_WORKING_FORMAT = "yuv422p10le"


class BuildError(Exception):
    """This clip cannot be rendered as it stands. The clip is quarantined, the batch continues."""


@dataclass(frozen=True)
class OutputFile:
    target: str
    kind: str  # "video" or "audio"
    temp: Path
    final: Path


@dataclass(frozen=True)
class RenderPlan:
    clip_id: str
    runnables: tuple[Runnable, ...]
    outputs: tuple[OutputFile, ...]
    framing: Framing
    settings_hashes: dict[str, str]
    intermediates: tuple[Path, ...]
    warnings: tuple[str, ...]


def _temp(final: Path) -> Path:
    return final.with_name(f".{final.name}.partial")


def _video_codec_args(target: Deliverable) -> tuple[str, ...]:
    if target.codec == "h264":
        return ("-c:v", "libx264", "-profile:v", "high", "-crf", str(target.crf), "-preset", "medium",
                "-pix_fmt", target.pix_fmt)
    return ("-c:v", "dnxhd", "-profile:v", target.codec, "-pix_fmt", target.pix_fmt)


def _audio_codec_args(target: Deliverable, rate: int) -> tuple[str, ...]:
    if target.audio_codec == "aac":
        return ("-c:a", "aac", "-b:a", f"{target.audio_bitrate_k}k", "-ar", str(rate))
    return ("-c:a", target.audio_codec, "-ar", str(rate))


def _lut(path: str) -> str:
    return f"lut3d=file={filter_value(path)}:interp=tetrahedral"


def colour_filters(colour: dict) -> list[str]:
    """The colour chain for one clip, in 16-bit RGB, before any crop or scale.

    baked: one cube holding CDL, camera look and show LUT, as the original spec describes.
    exact: the CDL as FFmpeg filters, then each LUT untouched. Same picture a lab produces, no resampling loss.
    Tetrahedral interpolation everywhere. Trilinear bands on a log to display LUT.
    """
    if colour["apply"] == "baked":
        return [_lut(colour["baked_lut"])]
    chain: list[str] = []
    values = colour.get("cdl")
    if values is not None:
        try:
            cdl = Cdl(tuple(map(float, values["slope"])), tuple(map(float, values["offset"])),
                      tuple(map(float, values["power"])), float(values["sat"]), "")
            if not all(len(triple) == 3 for triple in (cdl.slope, cdl.offset, cdl.power)):
                raise ValueError("slope, offset and power need three values each")
        except (KeyError, TypeError, ValueError) as error:
            raise BuildError(f"unusable CDL in the colour block: {error}") from None
        chain += cdl_filters(cdl)
    if colour.get("camera_look"):
        chain.append(_lut(colour["camera_look"]))
    chain.append(_lut(colour["show_lut"]))
    return chain


def build_render_plan(config: ShowConfig, day: DayContext, clip: dict, *, targets: tuple[str, ...] | None = None,
                      decoder: Decoder | None = None) -> RenderPlan:
    clip_id = clip["id"]
    picture, sound, colour = clip["picture"], clip["sound"], clip["colour"]
    if sound is None:
        raise BuildError(f"{clip_id}: no sound block, run sync before render")
    if colour is None:
        raise BuildError(f"{clip_id}: no colour block, run grade before render")
    fps = config.show.fps
    if picture["fps"] != fps:
        raise BuildError(f"{clip_id}: clip is {picture['fps']} fps, the show is {fps} fps, refusing to conform")
    wanted = tuple(config.deliverables) if targets is None else targets
    for name in wanted:
        if name not in config.deliverables:
            raise BuildError(f"{clip_id}: unknown deliverable {name!r}, config has {list(config.deliverables)}")
    deliverables = [config.deliverables[name] for name in config.deliverables if name in wanted]

    warnings: list[str] = []
    anamorphic = float(picture["anamorphic"])
    if anamorphic != 1.0:
        warnings.append(f"{clip_id}: anamorphic desqueeze {anamorphic} applied on a spherical show")
    first = deliverables[0]
    try:
        framing = compute_framing(
            decode_size(picture["width"], picture["height"], config.decode.resolution),
            crop_percent=config.framing.crop_percent, output=(first.width, first.height), fit=config.framing.fit,
            anamorphic=anamorphic, aspect_tolerance=config.framing.aspect_tolerance,
        )
    except FramingError as error:
        raise BuildError(f"{clip_id}: {error}") from None

    audible = sound["status"] in AUDIBLE
    sound_file: str | None = None
    if audible:
        files = sound.get("files") or []
        if len(files) != 1:
            raise BuildError(f"{clip_id}: expected exactly one sound file, got {len(files)}")
        sound_file = files[0]

    names = clip_names(config.naming, day, clip_id, picture)
    texts = burn_in_texts(config.burn_in, day, clip_id, picture, sound)
    decode = (decoder or make_decoder(config)).plan(Path(clip["source"]["path"]), clip_id=clip_id,
                                                    size=framing.decode, fps=fps)

    # ------------------------------------------------------- video graph
    try:
        chain = ["format=rgb48le", *colour_filters(colour)]
    except BuildError as error:
        raise BuildError(f"{clip_id}: colour: {error}") from None
    framing_filters = list(framing.filters(final_scale_options=_SCALE_COLOUR))
    scale_at = max(i for i, f in enumerate(framing_filters) if f.startswith("scale="))
    framing_filters.insert(scale_at + 1, f"format={_WORKING_FORMAT}")
    chain += framing_filters
    labels = [f"v_{d.name}" for d in deliverables]
    if len(labels) > 1:
        chain.append(f"split={len(labels)}" + "".join(f"[{label}]" for label in labels))
        graph = ["[0:v]" + ",".join(chain)]
    else:
        graph = ["[0:v]" + ",".join(chain) + f"[{labels[0]}]"]

    fontsize = round(first.height * config.burn_in.size_ratio)
    margin = round(first.height * config.burn_in.margin_ratio)
    video_label: dict[str, str] = {}
    for target in deliverables:
        if not target.burn_in:
            video_label[target.name] = f"v_{target.name}"
            continue
        try:
            fields = [
                drawtext(font=config.burn_in.font, text=field.text, position=position, fontsize=fontsize,
                         margin=margin, timecode=field.running_timecode,
                         fps=fps if field.running_timecode else None)
                for position, field in texts.items()
            ]
        except FilterError as error:
            raise BuildError(f"{clip_id}: {error}") from None
        video_label[target.name] = f"{target.name}_v"
        graph.append(f"[v_{target.name}]" + ",".join(fields) + f"[{target.name}_v]")

    # ------------------------------------------------------- audio graph
    audio_labels: dict[str, list[str]] = {d.name: [] for d in deliverables}
    if audible:
        tracks = config.sound.mix_tracks
        try:
            sync = sync_audio_filters(offset_samples=sound["offset_samples"],
                                      picture_samples=picture["frames"] * config.show.samples_per_frame,
                                      sample_rate=config.show.audio_rate)
            consumers: list[tuple[str, str, str, str]] = []  # (target, input label, pan, output label)
            for target in deliverables:
                if target.container == "mxf_opatom":
                    for index, track in enumerate(tracks, start=1):
                        consumers.append((target.name, f"a_{target.name}_{index}",
                                          mix_pan((track,), layout="mono", channels=sound["channels"]),
                                          f"{target.name}_a{index}"))
                else:
                    consumers.append((target.name, f"a_{target.name}",
                                      mix_pan(tracks[:2], layout="stereo", channels=sound["channels"]),
                                      f"{target.name}_a"))
        except FilterError as error:
            raise BuildError(f"{clip_id}: {error}") from None
        if len(consumers) > 1:
            graph.append("[1:a]" + ",".join(sync) + f",asplit={len(consumers)}"
                         + "".join(f"[{label}]" for _, label, _, _ in consumers))
            graph += [f"[{label}]{pan}[{out}]" for _, label, pan, out in consumers]
        else:
            _, _, pan, out = consumers[0]
            graph.append("[1:a]" + ",".join((*sync, pan)) + f"[{out}]")
        for name, _, _, out in consumers:
            audio_labels[name].append(out)

    # ----------------------------------------------------------- outputs
    folder = config.paths.output_root / day_folder(config.naming, day)
    start_tc = picture["start_tc"]
    metadata = ("-metadata", f"reel_name={names.tape}", "-metadata", f"material_package_name={names.avid_clip}")
    output_args: list[str] = []
    outputs: list[OutputFile] = []
    for target in deliverables:
        video_args = ("-map", f"[{video_label[target.name]}]")
        common_video = (*_video_codec_args(target), "-fps_mode", "passthrough", *_COLOUR_TAGS,
                        "-timecode", start_tc)
        if target.container == "mxf_opatom":
            final = folder / target.name / f"{clip_id}_V.mxf"
            outputs.append(OutputFile(target.name, "video", _temp(final), final))
            output_args += [*video_args, *common_video, *metadata, "-f", "mxf_opatom", str(_temp(final))]
            for index, label in enumerate(audio_labels[target.name], start=1):
                final = folder / target.name / f"{clip_id}_A{index}.mxf"
                outputs.append(OutputFile(target.name, "audio", _temp(final), final))
                output_args += ["-map", f"[{label}]", *_audio_codec_args(target, config.show.audio_rate), "-ac", "1",
                                "-mxf_audio_edit_rate", f"{fps}/1", "-timecode", start_tc, *metadata,
                                "-f", "mxf_opatom", str(_temp(final))]
        else:
            final = folder / target.name / f"{clip_id}.{target.container}"
            outputs.append(OutputFile(target.name, "video", _temp(final), final))
            maps = [*video_args]
            audio_codec: tuple[str, ...] = ()
            if audio_labels[target.name]:
                maps += ["-map", f"[{audio_labels[target.name][0]}]"]
                audio_codec = _audio_codec_args(target, config.show.audio_rate)
            else:
                maps.append("-an")
            output_args += [*maps, *common_video, *audio_codec, "-movflags", "+faststart",
                            "-f", target.container, str(_temp(final))]

    ffmpeg = Command((
        str(config.tools.ffmpeg.path), "-hide_banner", "-nostdin", "-y", *decode.input_args,
        *(("-i", sound_file) if sound_file else ()),
        "-filter_complex", ";".join(graph), *output_args,
    ))
    runnables: tuple[Runnable, ...] = (
        (Pipeline((decode.producer, ffmpeg)),) if decode.piped else (decode.producer, ffmpeg)
    )

    # ------------------------------------------------------------ hashes
    shared = {
        "clip": clip_id,
        "mob_id": clip["mob_id"],
        "show": {"fps": fps, "audio_rate": config.show.audio_rate},
        "picture": {key: picture[key] for key in ("width", "height", "fps", "frames", "start_tc", "anamorphic")},
        "decode": {"engine": config.decode.engine, "resolution": config.decode.resolution,
                   "bit_depth": config.decode.bit_depth, "args": list(config.decode.redline.decode_args)},
        "framing": framing.to_dict(),
        "lut_hash": colour["lut_hash"],
        "colour_apply": colour["apply"],
        "names": dataclasses.asdict(names),
        "sound": ({"status": sound["status"], "files": [PurePath(sound_file).name],
                   "offset_samples": sound["offset_samples"], "mix_tracks": list(config.sound.mix_tracks)}
                  if audible else {"status": "mute"}),
    }
    burn_in = {"font": config.burn_in.font.name, "fontsize": fontsize, "margin": margin,
               "fields": {p: dataclasses.asdict(t) for p, t in texts.items()}}
    hashes = {
        target.name: hash_of({**shared, "deliverable": target, "burn_in": burn_in if target.burn_in else None})
        for target in deliverables
    }

    return RenderPlan(clip_id=clip_id, runnables=runnables, outputs=tuple(outputs), framing=framing,
                      settings_hashes=hashes, intermediates=decode.intermediates, warnings=tuple(warnings))
