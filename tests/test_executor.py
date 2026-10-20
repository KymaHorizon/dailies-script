# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""The real executor: logs verbatim before running, captures output, never raises on a failed command."""

from __future__ import annotations

import io
import json

import pytest

from dailies.commands.base import Command, CommandError, CommandLog, DryRunExecutor, Pipeline, SubprocessExecutor


def make() -> tuple[SubprocessExecutor, io.StringIO]:
    logged = io.StringIO()
    return SubprocessExecutor(CommandLog(logged)), logged


def test_captures_stdout_and_exit_code():
    executor, _ = make()
    result = executor.run(Command(("/bin/echo", "hello")), clip="c", stage="scan")
    assert (result.returncode, result.stdout, result.ok) == (0, "hello\n", True)


def test_failed_command_is_a_result_not_an_exception():
    executor, _ = make()
    result = executor.run(Command(("/bin/sh", "-c", "echo broken >&2; exit 3")), clip="c", stage="scan")
    assert (result.returncode, result.stderr, result.ok) == (3, "broken\n", False)


def test_missing_executable_is_a_failed_result():
    executor, _ = make()
    result = executor.run(Command(("/nonexistent/tool", "x")), clip="c", stage="scan")
    assert result.ok is False and result.returncode == 127
    assert "/nonexistent/tool" in result.stderr


def test_command_is_logged_before_it_runs_and_marked_as_real():
    executor, logged = make()
    executor.run(Command(("/bin/echo", "a b")), clip="A001_C003", stage="scan")
    entry = json.loads(logged.getvalue())
    assert entry["dry_run"] is False
    assert entry["shell"] == "/bin/echo 'a b'"


def test_output_that_is_not_utf8_does_not_crash():
    executor, _ = make()
    result = executor.run(Command(("/usr/bin/printf", "\\377\\376ok")), clip="c", stage="scan")
    assert result.ok and result.stdout.endswith("ok")


def test_pipelines_are_not_run_at_this_build_step():
    executor, _ = make()
    pipe = Pipeline((Command(("/bin/echo", "x")), Command(("/bin/cat",))))
    with pytest.raises(CommandError, match="pipeline"):
        executor.run(pipe, clip="c", stage="render")


def test_dry_run_returns_no_result():
    executor = DryRunExecutor(io.StringIO(), CommandLog(io.StringIO()))
    assert executor.run(Command(("/bin/echo", "x")), clip="c", stage="scan") is None
