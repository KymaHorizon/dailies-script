# dailies-script

A command-line dailies pipeline for on-set data management. It takes sealed camera
and sound offloads, reads what the camera and the recorder wrote, syncs sound to
picture in the sample domain, resolves each clip's colour decisions and plans
synced, graded, burned-in editorial and review media. Every external command it
runs is logged verbatim, and every stage can be run as a dry run that prints the
commands and writes nothing.

It is a library and a CLI in plain Python 3.11 with one required dependency,
PyYAML. FFmpeg, ffprobe, OpenColorIO and a small bridge to the RED R3D SDK do the
heavy lifting, as external processes or as optional extras.

## What it does

- **Clip discovery.** Finds RED clips under a day folder and groups spanned files
  (`_001`, `_002`, ...) into one logical clip, addressing only the first file
  because the decoder follows the span itself. ProRes movies are discovered the
  same way.
- **Metadata decoding.** R3D clips are read through `r3dtool`, a small C++
  bridge to the RED R3D SDK that prints clip properties and every metadata item
  as JSON. ProRes movies are read with `ffprobe`. Both arrive as the same
  `ClipFacts` record: clip name, camera, roll, size, rate, frame count, the
  camera's default timecode plus absolute and edge timecode, ISO, white balance,
  anamorphic factor, dropped frames and slate fields.
- **ASC MHL gate.** A camera or sound folder is scanned only when it carries an
  ASC MHL history (as written by Silverstack and other offload tools) and every
  listed file is present at the listed size, with nothing unlisted. A folder that
  is still copying or was never sealed is skipped with a reason, never scanned
  optimistically. Reading uses the `ascmhl` reference library.
- **WAV probing.** `ffprobe` gives the BWF `time_reference` and the bext
  description; a small RIFF walker that also understands RF64 and BW64 reads the
  iXML chunk, which ffprobe does not expose. Scene, take and sound roll are
  resolved from iXML, then bext, then a filename pattern, then the folder name.
- **Sound sync.** Pure integer arithmetic in samples: offset equals picture start
  minus sound start, with midnight wrap, a plausibility window, overlap
  classification, slate agreement checks, MOS cameras and ambiguous pairs sent to
  review. A clip with no usable sound still renders, mute, and is flagged.
- **Timecode.** Non-drop timecode at an integer rate. A drop-frame separator is
  an error, not something to adapt to.
- **CDL and LUT handling.** ASC CDL files in `.cdl`, `.ccc` and `.cc` form, both
  `SATNode` spellings, strict about values. Per clip the chain is CDL, camera
  look, show LUT, optional output transform. It is applied either `exact` (CDL as
  FFmpeg filters, LUTs untouched, within 0.3 ten-bit codes of OpenColorIO) or
  `baked` (one OpenColorIO cube per distinct recipe, cached by content hash). A
  `.cube` reader with a tetrahedral sampler checks bakes against independent
  maths, and `verify-colour` compares the render's own colour chain against a
  colourist's reference frame by PSNR.
- **Framing.** Centred percentage crop, scale and pad in integer arithmetic,
  rounded down to even. A crop or scale that would distort the picture fails
  loudly instead of being squeezed.
- **Render planning.** One decode, one FFmpeg filter graph, every deliverable
  (DNxHR in MXF OP-Atom for Avid, H.264 MP4 for review), burn-ins from templates,
  sample-accurate audio, BT.709 legal-range tagging, and one settings hash per
  deliverable so an unchanged clip is skipped on a rerun.
- **Command and executor model.** Stages never call `subprocess`. They build
  `Command` and `Pipeline` objects and hand them to an executor. Every command is
  logged verbatim as both an argv list and a shell line before it runs, so any
  render can be reproduced by hand from the log. The dry-run executor prints and
  runs nothing.
- **Day manifest.** One JSON file per shoot day and unit is the only state. Strict
  schema, each stage owns its own blocks, atomic saves, and a SMPTE UMID MobID
  per clip that is assigned once and survives rescans and re-renders.

## Who it is for

DITs and data managers who want their dailies to be reproducible and inspectable
rather than hidden behind a GUI, dailies labs that need a scriptable second
opinion on sync, colour and framing, and pipeline developers who want a tested
reference for the arithmetic: samples per frame, crop maths, CDL semantics, MobID
layout. The test suite documents every rule the code holds to.

## Status

This is version 0.1.0, extracted from a working on-set pipeline. What is built:

| Command | State |
| --- | --- |
| `check` | validates a show config completely and prints the resolved framing |
| `scan` | reads R3D and ProRes metadata, gates on ASC MHL, reads WAV metadata, writes the manifest |
| `sync` | matches sound to picture from the manifest alone |
| `grade` | resolves CDL, looks and LUTs per clip, bakes cubes when asked |
| `verify-colour` | PSNR check of the colour chain against a reference frame |
| `render --dry-run` | builds and prints every decoder and FFmpeg command |

Not built yet: executing `render`, `qc`, `publish`, `run`, the AAF stringout and
concurrency. Two decoder gaps to know about: with `decode.engine: sdk` scan reads
R3D metadata through `r3dtool` but the SDK decode path for render is not written,
and with `decode.engine: redline` the render dry run works but REDline metadata
output is not parsed. Scan a real R3D day with `sdk`, dry-run its render with
`redline`.

The pipeline is currently fixed at 50 fps non-drop with 48 kHz sound (exactly 960
samples per frame). Other rates are refused at config load rather than converted.
Generalising the rate is the obvious next step.

## Requirements

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) (or pip).
- FFmpeg and ffprobe 5.1 or newer; 7.x is what the suite is checked against.
  `-fps_mode` is required, so FFmpeg 4.x will not do.
- OpenColorIO 2.3 or newer (the `opencolorio` wheel) for baked colour and the
  colour tests.
- `ascmhl` for the MHL gate.
- To read R3D: the RED R3D SDK (see below), or REDline.

Tools are pinned by absolute path and version in the show config. PATH is never
consulted, so a stray old FFmpeg cannot be picked up by accident.

## Install

```sh
git clone https://github.com/KymaHorizon/dailies-script.git
cd dailies-script
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e ".[dev,aaf,mhl,colour]"
.venv/bin/dailies check --config config/example_show.yaml --skip-path-checks
```

The extras are independent: `mhl` for the ASC MHL gate, `colour` for OpenColorIO
baking, `aaf` for pyaaf2 (reserved for the AAF stage), `dev` for the tests.

### The RED R3D SDK bridge

Camera metadata for R3D clips is read through `r3dtool`, the small C++ program in
`native/r3dtool/`. It links the RED R3D SDK, which is **not in this repository and
must never be added to it**. RED's licence forbids redistributing the SDK's headers,
libraries, samples and documentation, so each user downloads the SDK from RED,
accepts RED's licence, and builds the bridge locally:

```sh
R3DSDK_ROOT=/path/to/R3DSDKv9_2_1 native/r3dtool/build.sh
```

The binary and the SDK's dynamic libraries land in `native/build/`, which is
git-ignored. Point `tools.r3dtool.path` in your show config at the binary and pin
its reported version, for example `"0.1.0 / R3DAPI 9.2.1"`. The SDK only
initialises when its libraries sit beside the binary; set `R3DTOOL_LIBDIR` to
override. macOS and Linux are supported by `build.sh`.

`r3dtool.cpp` is original code written against the SDK's public API. It contains
no RED sample code. If you contribute to it, keep it that way.

## Quick start

1. Copy `config/example_show.yaml` to a show config and edit it. Every key is
   required and unknown keys are errors, so a typo cannot fall back to a default.
   Validate with `dailies check --config show.yaml`, which reports every problem
   at once and prints the crop, scale and pad per deliverable and camera.
2. Offload camera and sound media with a tool that writes ASC MHL histories, into
   the folders the config's `scan` section describes.
3. Run the stages for the day. The day code is given once, when the day is first
   scanned, and is then read from the manifest.

```sh
dailies scan   --config show.yaml --day 2026-10-12 --day-code SD014 [--dry-run]
dailies sync   --config show.yaml --day 2026-10-12 [--clip 'A001_*'] [--dry-run]
dailies grade  --config show.yaml --day 2026-10-12 [--clip 'A001_*'] [--dry-run]
dailies render --config show.yaml --day 2026-10-12 --dry-run [--clip 'A001_*'] [--force]
dailies verify-colour --config show.yaml --day 2026-10-12 --clip A001_C003_1012AB \
        --source log_frame.tif --reference graded_by_colourist.tif [--min-psnr 50]
```

Exit codes: 0 everything requested was done, 1 completed with clips that failed,
2 could not start. Every command a stage executes is appended verbatim to
`log_dir/<day>_<unit>.commands.jsonl`. The manifest lands in `manifest_dir` as
`<day>_<unit>.json` and is the single source of truth for every later stage.

## The show config

One YAML file per show, version controlled, nothing day specific in it. The
sections are `show` (rate, cameras, units), `tools` (pinned paths and versions),
`framing`, `colour` (OCIO config, decode space, show LUT, per-camera looks, CDL
search order, `exact` or `baked`), `decode` (engine, resolution, per-camera
sources and expected rasters, REDline argument templates), `sound`, `scan`
(where a day's media sits), `deliverables`, `burn_in`, `naming`, `paths` and
`limits`. The example file is commented and is itself exercised by the tests.

## Safety rules

These are the rules the code is built around. Tests hold the code to them.

- Config plus manifest fully determine the output. No stage reads the filesystem
  to decide anything, except `scan`, whose job that is.
- Every external command is a `Command` object, logged verbatim before it runs.
  Stages never call `subprocess` directly.
- `--dry-run` prints every command and writes nothing: no manifest, no log, no
  media.
- The pipeline never sends email, publishes, uploads, deletes camera media or
  issues a tape format command. There is no outbound path of any kind.
- Fail fast on config, fail per clip on media. A failed clip is recorded with
  its stderr and the batch continues. Two clips with the same name from
  different folders stop the run.
- Decisions that change the picture have no default. Colour application mode,
  crop, fit and rasters must all be stated.
- Outputs render to a `.partial` name beside the final path. Renaming is the
  executor's job, so a crashed render never leaves a plausible-looking file.
- Tool versions are recorded in the manifest and are not part of the settings
  hash, so a tool upgrade does not silently force a re-render and does not
  silently skip one either: the operator sees the version change.
- Nothing from the RED R3D SDK is stored in the repository.

## Running the tests

```sh
.venv/bin/pytest
.venv/bin/pytest --cov=dailies --cov-branch
```

The suite has 738 tests. The unit tests run with no media and no external
tools; stub executables stand in for FFmpeg, ffprobe and the decoders. The
integration tests under `tests/integration` hand real commands to real tools
and skip when those are absent:

- `tests/integration/test_ffmpeg_accepts_the_plan.py` renders synthetic picture
  and sound through the generated FFmpeg command. It looks for FFmpeg at
  `DAILIES_FFMPEG`, then in the usual Homebrew and system locations.
- `tests/integration/test_verify_colour_real.py` needs the same FFmpeg and
  OpenColorIO.
- `tests/integration/test_r3dtool_real.py` needs a built `native/build/r3dtool`
  and a real clip named in `DAILIES_TEST_R3D`.

The suite is kept at 100 percent line and branch coverage. New code comes with
its failing test first.

## Layout

| Module | Role |
| --- | --- |
| `config.py` | Show YAML to immutable dataclasses, strict keys, every problem reported together, config hash |
| `manifest.py` | One JSON per shoot day and unit, strict schema, stage ownership of blocks, atomic save, persistent MobIDs |
| `framing.py` | Crop, scale and pad maths in integers, rounds down to even, refuses to distort |
| `timecode.py` | Non-drop timecode at an integer rate |
| `mobid.py` | SMPTE UMID generation, compatible with pyaaf2 |
| `naming.py` | Day folder, tape name, Avid clip name and burn-in text from templates |
| `decoder.py` | Decoder interface: r3dtool (R3D SDK), REDline, and ffprobe/FFmpeg for ProRes |
| `commands/base.py` | Command and Pipeline objects, the verbatim command log, the dry-run and subprocess executors |
| `commands/filters.py` | FFmpeg filter strings: two-level escaping, burn-ins, sample-accurate sync |
| `commands/render.py` | One decode, one filter graph, every deliverable, one settings hash per deliverable |
| `commands/verify.py` | Reference frame check by PSNR |
| `scan/clips.py` | R3D discovery, spanned files grouped into one clip |
| `scan/mhl.py` | ASC MHL gate: sealed, complete, nothing unlisted |
| `scan/wav.py` | ffprobe for BWF time_reference, a RIFF, RF64 and BW64 reader for iXML |
| `scan/stage.py` | The scan stage: tool version check, gate, decoder, sound inventory, flags |
| `sync/match.py` | Pure sample-domain matching: overlap, midnight wrap, classification, slate checks |
| `sync/stage.py` | The sync stage: manifest in, sound blocks and flags out, no filesystem and no commands |
| `grade/cdl.py` | ASC CDL reader |
| `grade/cube.py` | `.cube` reader and tetrahedral sampler |
| `grade/bake.py` | OpenColorIO bake of CDL, camera look and show LUT into one cube, cached by content hash |
| `grade/stage.py` | The grade stage: find each clip's CDL, neutral default, flags, colour block |
| `cli.py` | Argument parsing and exit codes |
| `native/r3dtool/` | C++ bridge to the R3D SDK, source only |

## Licence

Apache License 2.0. See `LICENSE`. Third-party software this project depends on
or invokes, and the external RED R3D SDK requirement, are listed in `NOTICE`.

Built by Kyma Horizon Ltd, London, for on-set data management on scripted productions.
