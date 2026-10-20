# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Names derived from config templates plus manifest facts."""

from __future__ import annotations

import pytest

from dailies.naming import DayContext, NamingError, burn_in_texts, clip_names, day_folder

DAY = DayContext(show="TST", day="2026-10-12", day_code="SD014", unit="MU")
PICTURE = {"camera": "A", "roll": "001", "clip": "C003", "reel": "A001", "scene": "14A", "take": "3",
           "start_tc": "10:14:22:31"}


def test_day_folder_uses_a_compact_date(load_valid):
    assert day_folder(load_valid().naming, DAY) == "TST_20261012_SD014_MU"


def test_tape_is_the_full_source_clip_name(load_valid):
    names = clip_names(load_valid().naming, DAY, "A001_C003_1012AB", PICTURE)
    assert names.tape == "A001_C003_1012AB"


def test_avid_clip_name_comes_from_the_slate(load_valid):
    names = clip_names(load_valid().naming, DAY, "A001_C003_1012AB", PICTURE)
    assert names.avid_clip == "14A-3A"


@pytest.mark.parametrize("missing", [{"scene": None}, {"take": None}, {"scene": ""}, {"scene": None, "take": None}])
def test_missing_slate_metadata_falls_back_to_the_source_name(load_valid, missing):
    names = clip_names(load_valid().naming, DAY, "A001_C003_1012AB", {**PICTURE, **missing})
    assert names.avid_clip == "A001_C003_1012AB"
    assert names.used_fallback is True


def test_names_may_not_contain_path_separators(load_valid):
    with pytest.raises(NamingError, match="separator"):
        clip_names(load_valid().naming, DAY, "A001_C003_1012AB", {**PICTURE, "scene": "14/A"})


def test_burn_in_fields_are_resolved_per_position(load_valid):
    cfg = load_valid()
    sound = {"status": "matched", "start_tc": "10:14:20:00", "soundroll": "SD001"}
    texts = burn_in_texts(cfg.burn_in, DAY, "A001_C003_1012AB", PICTURE, sound)
    assert texts["top_left"].text == "A001_C003_1012AB"
    assert texts["top_right"].text == "A / 14A-3"
    assert texts["bottom_right"].text == "SND 10:14:20:00"


def test_running_timecode_is_split_from_its_label(load_valid):
    sound = {"status": "matched", "start_tc": "10:14:20:00", "soundroll": "SD001"}
    field = burn_in_texts(load_valid().burn_in, DAY, "A001_C003_1012AB", PICTURE, sound)["bottom_left"]
    assert field.text == "SRC "
    assert field.running_timecode == "10:14:22:31"


@pytest.mark.parametrize("status, shown", [("mos", "MOS"), ("no_sound", "NO SYNC"), ("tc_implausible", "NO SYNC")])
def test_unsynced_clips_say_so_in_the_sound_field(load_valid, status, shown):
    texts = burn_in_texts(load_valid().burn_in, DAY, "A001_C003_1012AB", PICTURE, {"status": status})
    assert texts["bottom_right"].text == f"SND {shown}"


def test_missing_slate_values_burn_in_as_blank(load_valid):
    sound = {"status": "mos"}
    texts = burn_in_texts(load_valid().burn_in, DAY, "A001_C003_1012AB", {**PICTURE, "scene": None, "take": None}, sound)
    assert texts["top_right"].text == "A / -"
