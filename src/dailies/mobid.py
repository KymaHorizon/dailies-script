# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""MobID generation.

A MobID is Avid's unique id for a piece of media, a 32 byte SMPTE UMID. Avid
links bin clips to MXF essence by this id and never by filename. One is
generated per clip when the clip first enters the manifest and is never
regenerated, so the MXF wrap, the ALE and the AAF can all carry the same value
and a re-render of one clip cannot disturb any other.

The layout matches what pyaaf2 produces for a new mob, so the string can be
handed to pyaaf2 unchanged.
"""

from __future__ import annotations

import re
import uuid

_PREFIX = "urn:smpte:umid:060a2b34.01010105.01010f20.13000000"
_UMID = re.compile(r"^urn:smpte:umid:(?:[0-9a-f]{8}\.){7}[0-9a-f]{8}$")


class MobIdError(ValueError):
    """Not a SMPTE UMID string."""


def new_mob_id(material: uuid.UUID | None = None) -> str:
    digits = (material or uuid.uuid4()).hex
    return ".".join([_PREFIX, digits[0:8], digits[8:16], digits[16:24], digits[24:32]])


def validate_mob_id(value: object) -> str:
    if not isinstance(value, str) or not _UMID.match(value):
        raise MobIdError(f"not a SMPTE UMID string: {value!r}")
    return value
