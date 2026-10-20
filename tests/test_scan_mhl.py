# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""ASC MHL gate. Histories are created with the real ascmhl tool, nothing is mocked."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("ascmhl")

from dailies.scan.mhl import check_mhl

ASCMHL = Path(sys.executable).with_name("ascmhl")


def seal(folder: Path) -> None:
    done = subprocess.run([str(ASCMHL), "create", "-h", "xxh64", str(folder)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@pytest.fixture
def roll(tmp_path) -> Path:
    folder = tmp_path / "A001_1012AB"
    rdc = folder / "A001_C001_1012AB.RDC"
    rdc.mkdir(parents=True)
    (rdc / "A001_C001_1012AB_001.R3D").write_bytes(b"a" * 5000)
    (rdc / "A001_C001_1012AB_002.R3D").write_bytes(b"b" * 3000)
    return folder


def test_sealed_folder_with_every_file_present_is_eligible(roll):
    seal(roll)
    gate = check_mhl(roll)
    assert gate.eligible, gate.reason
    assert gate.files == {
        "A001_C001_1012AB.RDC/A001_C001_1012AB_001.R3D": 5000,
        "A001_C001_1012AB.RDC/A001_C001_1012AB_002.R3D": 3000,
    }


def test_folder_without_an_mhl_is_skipped_never_scanned_optimistically(roll):
    gate = check_mhl(roll)
    assert not gate.eligible
    assert "no ascmhl folder" in gate.reason


def test_missing_file_makes_the_folder_ineligible(roll):
    seal(roll)
    (roll / "A001_C001_1012AB.RDC/A001_C001_1012AB_002.R3D").unlink()
    gate = check_mhl(roll)
    assert not gate.eligible
    assert "missing" in gate.reason and "A001_C001_1012AB_002.R3D" in gate.reason


def test_partly_copied_file_makes_the_folder_ineligible(roll):
    seal(roll)
    (roll / "A001_C001_1012AB.RDC/A001_C001_1012AB_002.R3D").write_bytes(b"b" * 1200)
    gate = check_mhl(roll)
    assert not gate.eligible
    assert "size" in gate.reason and "1200" in gate.reason and "3000" in gate.reason


def test_media_that_arrived_after_the_seal_makes_the_folder_ineligible(roll):
    seal(roll)
    (roll / "A001_C001_1012AB.RDC/A001_C001_1012AB_003.R3D").write_bytes(b"c" * 10)
    gate = check_mhl(roll)
    assert not gate.eligible
    assert "not listed" in gate.reason and "_003.R3D" in gate.reason


def test_a_later_generation_covers_the_new_file(roll):
    seal(roll)
    (roll / "A001_C001_1012AB.RDC/A001_C001_1012AB_003.R3D").write_bytes(b"c" * 10)
    seal(roll)
    gate = check_mhl(roll)
    assert gate.eligible, gate.reason
    assert len(gate.files) == 3


def test_system_litter_is_not_media(roll):
    seal(roll)
    (roll / ".DS_Store").write_bytes(b"x")
    assert check_mhl(roll).eligible


def test_corrupt_history_is_ineligible_not_a_crash(roll):
    seal(roll)
    manifest = next((roll / "ascmhl").glob("0001_*.mhl"))
    manifest.write_text("<hashlist><broken")
    gate = check_mhl(roll)
    assert not gate.eligible
    assert "unreadable" in gate.reason


def test_gate_reports_when_the_history_last_changed(roll):
    seal(roll)
    assert check_mhl(roll).sealed_mtime_ns > 0


def test_history_that_lists_no_files_is_ineligible(tmp_path):
    folder = tmp_path / "EMPTY"
    folder.mkdir()
    (folder / ".DS_Store").write_bytes(b"x")
    seal(folder)
    gate = check_mhl(folder)
    assert not gate.eligible and "lists no files" in gate.reason
