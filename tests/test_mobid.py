# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""MobIDs are generated once per clip and persisted. They must be valid SMPTE
UMID strings that pyaaf2 accepts unchanged."""

import re
import uuid

import pytest

from dailies.mobid import MobIdError, new_mob_id, validate_mob_id

UMID = re.compile(r"^urn:smpte:umid:([0-9a-f]{8}\.){7}[0-9a-f]{8}$")


def test_new_mob_id_is_a_umid_string():
    assert UMID.match(new_mob_id())


def test_new_mob_ids_are_unique():
    assert len({new_mob_id() for _ in range(200)}) == 200


def test_mob_id_embeds_the_supplied_uuid():
    material = uuid.UUID("434b3488-8c64-4114-8231-333cf00dea50")
    assert new_mob_id(material) == (
        "urn:smpte:umid:060a2b34.01010105.01010f20.13000000.434b3488.8c644114.8231333c.f00dea50"
    )


def test_pyaaf2_accepts_the_mob_id_unchanged():
    mobid = pytest.importorskip("aaf2.mobid")
    value = new_mob_id()
    assert str(mobid.MobID(value)) == value


def test_validate_accepts_a_foreign_umid():
    value = "urn:smpte:umid:060a2b34.01010105.01010d00.13874c52.52947134.3f874c52.00529471.343f8700"
    assert validate_mob_id(value) == value


@pytest.mark.parametrize("bad", ["", "A001_C003", "urn:smpte:umid:060a2b34", "urn:smpte:umid:" + "zzzzzzzz." * 7 + "zzzzzzzz", None])
def test_validate_rejects_anything_else(bad):
    with pytest.raises(MobIdError):
        validate_mob_id(bad)
