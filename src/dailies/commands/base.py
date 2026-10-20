# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Command objects.

Stages never call subprocess. They build Command or Pipeline objects and hand
them to an executor. Every one is logged verbatim before it runs, as both an
argv list and a shell line, so any render can be reproduced by hand from the
log. The dry-run executor prints them and runs nothing.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Protocol, TextIO


class CommandError(ValueError):
    """A command that must never be built, let alone run."""


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.argv:
            raise CommandError("empty command")
        if not all(isinstance(arg, str) for arg in self.argv):
            raise CommandError(f"command arguments must all be strings: {self.argv!r}")
        if not os.path.isabs(self.argv[0]):
            raise CommandError(f"executable must be an absolute path pinned in the config, got {self.argv[0]!r}")

    def shell(self) -> str:
        return shlex.join(self.argv)

    def commands(self) -> tuple["Command", ...]:
        return (self,)


@dataclass(frozen=True)
class Pipeline:
    """Commands connected stdout to stdin, left to right."""

    stages: tuple[Command, ...]

    def __post_init__(self) -> None:
        if len(self.stages) < 2:
            raise CommandError("a pipeline needs at least two commands")

    def shell(self) -> str:
        return " | ".join(command.shell() for command in self.stages)

    def commands(self) -> tuple[Command, ...]:
        return self.stages


Runnable = Command | Pipeline


class CommandLog:
    """One JSON line per command, flushed immediately."""

    def __init__(self, stream: TextIO):
        self._stream = stream

    def record(self, runnable: Runnable, *, clip: str, stage: str, dry_run: bool) -> None:
        entry = {
            "clip": clip,
            "stage": stage,
            "dry_run": dry_run,
            "argv": [list(command.argv) for command in runnable.commands()],
            "shell": runnable.shell(),
        }
        self._stream.write(json.dumps(entry, ensure_ascii=True) + "\n")
        self._stream.flush()


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Executor(Protocol):
    def run(self, runnable: Runnable, *, clip: str, stage: str) -> "Result | None": ...


class DryRunExecutor:
    """Prints every command and executes none of them."""

    def __init__(self, out: TextIO, log: CommandLog):
        self._out = out
        self._log = log

    def run(self, runnable: Runnable, *, clip: str, stage: str) -> None:
        self._log.record(runnable, clip=clip, stage=stage, dry_run=True)
        self._out.write(f"# {stage} {clip}\n{runnable.shell()}\n")


class SubprocessExecutor:
    """Runs a command for real. It is logged verbatim first, so the log shows what was attempted even
    if the process never returns. A failing command is a Result, not an exception: the caller decides
    whether that quarantines a clip or stops the run."""

    def __init__(self, log: CommandLog):
        self._log = log

    def run(self, runnable: Runnable, *, clip: str, stage: str) -> Result:
        if isinstance(runnable, Pipeline):
            raise CommandError("pipeline execution is built in the render step")
        self._log.record(runnable, clip=clip, stage=stage, dry_run=False)
        try:
            done = subprocess.run(runnable.argv, capture_output=True, stdin=subprocess.DEVNULL, check=False)
        except OSError as error:
            return Result(127, "", f"{runnable.argv[0]}: {error}")
        return Result(done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"))
