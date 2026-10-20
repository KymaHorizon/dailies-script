# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""R3D discovery: spanned files are one logical clip, addressed by the first file."""

from __future__ import annotations

from pathlib import Path

from dailies.scan.clips import discover_clips


def make(root: Path, *names: str, size: int = 10) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * size)


def test_single_file_clip(tmp_path):
    make(tmp_path, "A001_1012AB.RDM/A001_C001_1012AB.RDC/A001_C001_1012AB_001.R3D")
    (clip,) = discover_clips(tmp_path)
    assert clip.clip_name == "A001_C001_1012AB"
    assert clip.first.name == "A001_C001_1012AB_001.R3D"
    assert clip.gaps == ()


def test_spanned_files_are_one_clip_addressed_by_the_first_part(tmp_path):
    rdc = "A001.RDM/A001_C003_0428HT.RDC/"
    make(tmp_path, rdc + "A001_C003_0428HT_002.R3D", rdc + "A001_C003_0428HT_001.R3D", rdc + "A001_C003_0428HT_003.R3D")
    (clip,) = discover_clips(tmp_path)
    assert clip.first.name == "A001_C003_0428HT_001.R3D"
    assert [p.name[-7:-4] for p in clip.parts] == ["001", "002", "003"]
    assert clip.size_bytes == 30


def test_clips_are_sorted_by_name(tmp_path):
    make(tmp_path, "B/B001_C001_1012CD_001.R3D", "A/A001_C002_1012AB_001.R3D", "A/A001_C001_1012AB_001.R3D")
    assert [c.clip_name for c in discover_clips(tmp_path)] == ["A001_C001_1012AB", "A001_C002_1012AB", "B001_C001_1012CD"]


def test_missing_span_part_is_reported(tmp_path):
    make(tmp_path, "A/A001_C001_1012AB_001.R3D", "A/A001_C001_1012AB_003.R3D")
    (clip,) = discover_clips(tmp_path)
    assert clip.gaps == (2,)


def test_missing_first_part_is_reported_and_nothing_is_addressed_blindly(tmp_path):
    make(tmp_path, "A/A001_C001_1012AB_002.R3D")
    (clip,) = discover_clips(tmp_path)
    assert clip.gaps == (1,)


def test_extension_case_does_not_matter(tmp_path):
    make(tmp_path, "A/A001_C001_1012AB_001.r3d")
    assert len(discover_clips(tmp_path)) == 1


def test_sidecars_and_hidden_files_are_ignored(tmp_path):
    make(tmp_path, "A/A001_C001_1012AB_001.R3D", "A/A001_C001_1012AB.RMD", "A/._A001_C001_1012AB_001.R3D", "A/notes.txt")
    assert len(discover_clips(tmp_path)) == 1


def test_same_clip_name_in_two_folders_stays_two_clips(tmp_path):
    # two cameras with the same reel name: both are reported so the manifest can refuse the collision
    make(tmp_path, "cam1/A001_C001_1012AB_001.R3D", "cam2/A001_C001_1012AB_001.R3D")
    assert len(discover_clips(tmp_path)) == 2


def test_empty_folder(tmp_path):
    assert discover_clips(tmp_path) == []


# ------------------------------------------------------------------ ProRes


def test_prores_movies_are_discovered_with_their_kind(tmp_path):
    make(tmp_path, "A001.RDM/A001_C001_1012AB.RDC/A001_C001_1012AB_001.mov", "E001.RDM/E001_C001_1012EF.RDC/E001_C001_1012EF_001.R3D")
    assert [(c.clip_name, c.kind) for c in discover_clips(tmp_path)] == [("A001_C001_1012AB", "mov"), ("E001_C001_1012EF", "r3d")]


def test_spanned_prores_is_one_clip_with_every_part_in_order(tmp_path):
    make(tmp_path, "A/A001_C002_1012AB_002.mov", "A/A001_C002_1012AB_001.mov")
    (clip,) = discover_clips(tmp_path)
    assert [p.name for p in clip.parts] == ["A001_C002_1012AB_001.mov", "A001_C002_1012AB_002.mov"]


def test_a_movie_without_a_span_number_is_a_single_part_clip(tmp_path):
    make(tmp_path, "A/A001_C003_042822.MOV")
    (clip,) = discover_clips(tmp_path)
    assert (clip.clip_name, clip.kind, clip.gaps) == ("A001_C003_042822", "mov", ())


def test_a_movie_and_an_r3d_with_the_same_name_stay_two_clips(tmp_path):
    make(tmp_path, "A/A001_C001_1012AB_001.mov", "A/A001_C001_1012AB_001.R3D")
    assert sorted(c.kind for c in discover_clips(tmp_path)) == ["mov", "r3d"]


def test_an_r3d_without_a_span_number_is_not_a_camera_original(tmp_path):
    make(tmp_path, "A/A001_C001_1012AB.R3D")  # a trimmed or exported file, never what a camera writes
    assert discover_clips(tmp_path) == []
