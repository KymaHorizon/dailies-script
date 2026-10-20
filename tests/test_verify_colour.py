# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Reference frame check: the render's own colour chain against a graded frame from the colourist."""

from __future__ import annotations

from pathlib import Path

import pytest

from dailies.commands.verify import VerifyError, build_verify_command, parse_psnr

EXACT = {"apply": "exact", "lut_hash": "x", "show_lut": "/show/luts/show.cube", "camera_look": None,
         "cdl": {"slope": [1.1, 1.0, 0.9], "offset": [0, 0, 0], "power": [1, 1, 1], "sat": 1.0}}
BAKED = {"apply": "baked", "lut_hash": "x", "baked_lut": "/show/cache/luts/ab.cube"}


def graph_of(command) -> str:
    return command.argv[command.argv.index("-filter_complex") + 1]


def test_command_compares_the_graded_source_with_the_reference(load_valid):
    cfg = load_valid()
    cmd = build_verify_command(cfg, EXACT, Path("/f/source.tif"), Path("/f/reference.tif"))
    assert cmd.argv[:3] == (str(cfg.tools.ffmpeg.path), "-hide_banner", "-nostdin")
    assert cmd.argv[3:7] == ("-i", "/f/source.tif", "-i", "/f/reference.tif")
    assert cmd.argv[-3:] == ("-f", "null", "-")
    graph = graph_of(cmd)
    assert graph.startswith("[0:v]format=rgb48le,lutrgb=")
    assert graph.endswith("format=rgb48le[graded];[1:v]format=rgb48le[reference];[graded][reference]psnr")


def test_the_chain_is_the_one_the_render_uses(load_valid):
    from dailies.commands.render import colour_filters
    graph = graph_of(build_verify_command(load_valid(), EXACT, Path("/s.tif"), Path("/r.tif")))
    assert ",".join(colour_filters(EXACT)) in graph
    assert "ab.cube" in graph_of(build_verify_command(load_valid(), BAKED, Path("/s.tif"), Path("/r.tif")))


def test_psnr_is_read_from_ffmpegs_summary_line():
    stderr = "frame=1\n[Parsed_psnr_4 @ 0x1] PSNR r:61.20 g:63.05 b:60.11 average:61.31 min:61.31 max:61.31\n"
    assert parse_psnr(stderr) == pytest.approx(61.31)


def test_identical_frames_report_infinite_psnr():
    assert parse_psnr("[Parsed_psnr_4 @ 0x1] PSNR r:inf g:inf b:inf average:inf min:inf max:inf\n") == float("inf")


def test_missing_summary_line_is_an_error_not_a_pass():
    with pytest.raises(VerifyError, match="PSNR"):
        parse_psnr("Error: input sizes differ\n")
