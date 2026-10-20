# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""ASC CDL reading. The three file types and both element spellings seen in real show folders."""

from __future__ import annotations

import pytest

from dailies.grade.cdl import Cdl, CdlError, read_cdl

CDL_FILE = """<?xml version="1.0" encoding="UTF-8"?>
<ColorDecisionList xmlns="urn:ASC:CDL:v1.01">
    <ColorDecision>
        <ColorCorrection id="A005C008_230605_RO25">
            <SOPNode>
                <Slope>1.006446 1.006446 1.006446</Slope>
                <Offset>0.015733 0.018482 0.031923</Offset>
                <Power>1.020748 1.020748 1.020748</Power>
            </SOPNode>
            <SATNode>
                <Saturation>0.950000</Saturation>
            </SATNode>
        </ColorCorrection>
    </ColorDecision>
</ColorDecisionList>
"""

CCC_FILE = """<?xml version="1.0" ?>
<ColorCorrectionCollection xmlns="urn:ASC:CDL:v1.2">
	<ColorCorrection id="mkt_arri35">
		<SOPNode>
			<Slope>1.075070 1.075070 1.075070</Slope>
			<Offset>-0.028255 -0.028255 -0.028255</Offset>
			<Power>1.000000 1.000000 1.000000</Power>
		</SOPNode>
		<SatNode>
			<Saturation> 1.100000</Saturation>
		</SatNode>
	</ColorCorrection>
	<ColorCorrection id="mkt_night">
		<SOPNode><Slope>0.9 0.9 1.0</Slope><Offset>0 0 0</Offset><Power>1 1 1</Power></SOPNode>
	</ColorCorrection>
</ColorCorrectionCollection>
"""

CC_FILE = """<ColorCorrection id="14A"><SOPNode><Slope>1.02 1.0 0.98</Slope><Offset>0 0 0</Offset>
<Power>1 1 1</Power></SOPNode><SatNode><Saturation>1</Saturation></SatNode></ColorCorrection>"""


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_color_decision_list_with_upper_case_sat_node(tmp_path):
    cdl = read_cdl(write(tmp_path, "A005C008_230605_RO25.cdl", CDL_FILE))
    assert cdl == Cdl((1.006446, 1.006446, 1.006446), (0.015733, 0.018482, 0.031923),
                      (1.020748, 1.020748, 1.020748), 0.95, "A005C008_230605_RO25")


def test_collection_with_mixed_case_sat_node_picks_the_wanted_id(tmp_path):
    cdl = read_cdl(write(tmp_path, "looks.ccc", CCC_FILE), wanted=("mkt_arri35",))
    assert cdl.sat == 1.1 and cdl.offset == (-0.028255, -0.028255, -0.028255)


def test_bare_color_correction_file(tmp_path):
    assert read_cdl(write(tmp_path, "14A.cc", CC_FILE)).slope == (1.02, 1.0, 0.98)


def test_missing_saturation_means_one(tmp_path):
    assert read_cdl(write(tmp_path, "looks.ccc", CCC_FILE), wanted=("mkt_night",)).sat == 1.0


def test_the_first_wanted_id_that_exists_wins(tmp_path):
    cdl = read_cdl(write(tmp_path, "looks.ccc", CCC_FILE), wanted=("A001_C001_1012AB", "mkt_night"))
    assert cdl.id == "mkt_night"


def test_a_collection_with_several_corrections_and_no_match_is_ambiguous(tmp_path):
    with pytest.raises(CdlError, match="2 corrections.*none is named"):
        read_cdl(write(tmp_path, "looks.ccc", CCC_FILE), wanted=("14A",))


def test_a_single_correction_is_used_whatever_its_id(tmp_path):
    assert read_cdl(write(tmp_path, "14A.cdl", CDL_FILE), wanted=("14A",)).id == "A005C008_230605_RO25"


def test_file_without_a_namespace_is_accepted(tmp_path):
    assert read_cdl(write(tmp_path, "x.cdl", CDL_FILE.replace(' xmlns="urn:ASC:CDL:v1.01"', ""))).sat == 0.95


@pytest.mark.parametrize("text, problem", [
    ("<ColorDecisionList><broken", "not valid XML"),
    ("<Something/>", "no ColorCorrection"),
    (CC_FILE.replace("1.02 1.0 0.98", "1.02 1.0"), "Slope"),
    (CC_FILE.replace("1.02 1.0 0.98", "1.02 one 0.98"), "Slope"),
    (CC_FILE.replace("<Slope>1.02 1.0 0.98</Slope>", ""), "Slope"),
    (CC_FILE.replace("1.02 1.0 0.98", "1.02 -1.0 0.98"), "Slope"),
    (CC_FILE.replace("<Power>1 1 1</Power>", "<Power>1 0 1</Power>"), "Power"),
    (CC_FILE.replace("<Saturation>1</Saturation>", "<Saturation>-0.5</Saturation>"), "Saturation"),
    (CC_FILE.replace("1.02 1.0 0.98", "nan 1 1"), "Slope"),
])
def test_unusable_files_are_cdl_errors(tmp_path, text, problem):
    with pytest.raises(CdlError, match=problem):
        read_cdl(write(tmp_path, "bad.cc", text))


def test_missing_file_is_a_cdl_error(tmp_path):
    with pytest.raises(CdlError, match="cannot read"):
        read_cdl(tmp_path / "absent.cdl")


def test_identity_is_recognised():
    assert Cdl((1, 1, 1), (0, 0, 0), (1, 1, 1), 1.0, "x").is_identity
    assert not Cdl((1.01, 1, 1), (0, 0, 0), (1, 1, 1), 1.0, "x").is_identity


def test_manifest_form_round_trips():
    cdl = Cdl((1.02, 1.0, 0.98), (0.0, 0.01, 0.0), (1.0, 1.0, 1.0), 0.9, "14A")
    assert cdl.to_dict() == {"slope": [1.02, 1.0, 0.98], "offset": [0.0, 0.01, 0.0], "power": [1.0, 1.0, 1.0], "sat": 0.9}


def test_saturation_that_is_not_a_number(tmp_path):
    with pytest.raises(CdlError, match="Saturation is not a number"):
        read_cdl(write(tmp_path, "bad.cc", CC_FILE.replace("<Saturation>1</Saturation>", "<Saturation>lots</Saturation>")))
