# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""The example config in the repo must always satisfy the current schema."""

from pathlib import Path

from dailies.config import load_config

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example_show.yaml"


def test_example_config_loads_without_the_show_volumes():
    config = load_config(EXAMPLE, check_paths=False)
    assert config.show.cameras == ("A", "B", "C", "D", "E")
    assert config.decode.sources == {"A": "prores", "B": "prores", "C": "prores", "D": "prores", "E": "r3d"}
    assert config.nominal_framing("avid", "E").scale == (1920, 1012)


def test_example_config_has_no_em_dashes():
    assert chr(0x2014) not in EXAMPLE.read_text()


def test_the_neutral_cdl_shipped_with_the_example_really_is_neutral():
    from dailies.grade.cdl import read_cdl

    cdl = read_cdl(EXAMPLE.parent / "cdl" / "000.cdl")
    assert cdl.is_identity and cdl.id == "000"
