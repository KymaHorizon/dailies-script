# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Command objects, verbatim shell rendering, the command log and the dry-run executor."""

from __future__ import annotations

import io
import json
import shlex

import pytest

from dailies.commands.base import Command, CommandError, CommandLog, DryRunExecutor, Pipeline


def test_command_renders_as_a_copy_pasteable_shell_line():
    cmd = Command(("/opt/homebrew/bin/ffprobe", "-i", "/Volumes/RAID/My Sound/14A-03.wav"))
    assert cmd.shell() == "/opt/homebrew/bin/ffprobe -i '/Volumes/RAID/My Sound/14A-03.wav'"


def test_shell_rendering_round_trips_through_a_real_shell_parser():
    argv = ("/bin/ffmpeg", "-filter_complex", "[0:v]drawtext=text='It'\\''s 100%':x=1[v]", "out file.mxf")
    assert tuple(shlex.split(Command(argv).shell())) == argv


def test_command_is_immutable_and_hashable():
    cmd = Command(("/bin/true",))
    with pytest.raises(AttributeError):
        cmd.argv = ("/bin/false",)  # type: ignore[misc]
    assert hash(cmd) == hash(Command(("/bin/true",)))


def test_command_rejects_an_empty_argv():
    with pytest.raises(CommandError, match="empty"):
        Command(())


def test_command_rejects_non_string_arguments():
    with pytest.raises(CommandError, match="strings"):
        Command(("/bin/ffmpeg", "-r", 50))  # type: ignore[arg-type]


def test_command_requires_an_absolute_executable():
    with pytest.raises(CommandError, match="absolute"):
        Command(("ffmpeg", "-version"))


def test_pipeline_renders_with_pipes():
    pipe = Pipeline((Command(("/bin/decode", "clip.R3D")), Command(("/bin/ffmpeg", "-i", "-"))))
    assert pipe.shell() == "/bin/decode clip.R3D | /bin/ffmpeg -i -"


def test_pipeline_needs_at_least_two_commands():
    with pytest.raises(CommandError, match="two"):
        Pipeline((Command(("/bin/true",)),))


def test_log_writes_one_json_line_per_command():
    stream = io.StringIO()
    log = CommandLog(stream)
    log.record(Command(("/bin/ffmpeg", "-version")), clip="A001_C003", stage="render", dry_run=True)
    entry = json.loads(stream.getvalue())
    assert entry == {
        "clip": "A001_C003",
        "stage": "render",
        "dry_run": True,
        "argv": [["/bin/ffmpeg", "-version"]],
        "shell": "/bin/ffmpeg -version",
    }


def test_log_records_every_command_of_a_pipeline():
    stream = io.StringIO()
    pipe = Pipeline((Command(("/bin/decode", "a")), Command(("/bin/ffmpeg", "-i", "-"))))
    CommandLog(stream).record(pipe, clip="c", stage="render", dry_run=False)
    assert json.loads(stream.getvalue())["argv"] == [["/bin/decode", "a"], ["/bin/ffmpeg", "-i", "-"]]


def test_dry_run_prints_the_command_and_logs_it():
    out, logged = io.StringIO(), io.StringIO()
    executor = DryRunExecutor(out, CommandLog(logged))
    executor.run(Command(("/bin/ffmpeg", "-version")), clip="A001_C003", stage="render")
    assert out.getvalue() == "# render A001_C003\n/bin/ffmpeg -version\n"
    assert json.loads(logged.getvalue())["dry_run"] is True


def test_dry_run_never_executes(tmp_path):
    marker = tmp_path / "ran"
    executor = DryRunExecutor(io.StringIO(), CommandLog(io.StringIO()))
    executor.run(Command(("/usr/bin/touch", str(marker))), clip="c", stage="render")
    assert not marker.exists()
