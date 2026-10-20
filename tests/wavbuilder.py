# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Build Broadcast WAV files byte by byte for tests. Test-only code."""

from __future__ import annotations

import struct


def chunk(tag: bytes, data: bytes, *, declared: int | None = None) -> bytes:
    size = len(data) if declared is None else declared
    return tag + struct.pack("<I", size) + data + (b"\x00" if len(data) % 2 else b"")


def fmt_chunk(rate: int = 48000, channels: int = 2, bits: int = 24) -> bytes:
    return chunk(b"fmt ", struct.pack("<HHIIHH", 1, channels, rate, rate * channels * bits // 8,
                                      channels * bits // 8, bits))


def bext_chunk(description: str, time_reference: int) -> bytes:
    body = (description.encode().ljust(256, b"\0") + b"Recorder".ljust(32, b"\0") + b"REF".ljust(32, b"\0")
            + b"2026-10-12" + b"10:14:20" + struct.pack("<II", time_reference & 0xFFFFFFFF, time_reference >> 32)
            + struct.pack("<H", 1) + b"\0" * 254)
    return chunk(b"bext", body)


def ixml_chunk(*, scene: str = "14A", take: str = "3", tape: str = "SD014", tracks=("MIX L", "MIX R"),
               extra: str = "") -> bytes:
    track_xml = "".join(
        f"<TRACK><CHANNEL_INDEX>{i}</CHANNEL_INDEX><INTERLEAVE_INDEX>{i}</INTERLEAVE_INDEX><NAME>{name}</NAME></TRACK>"
        for i, name in enumerate(tracks, start=1))
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><BWFXML><IXML_VERSION>2.0</IXML_VERSION><SCENE>{scene}</SCENE>'
           f"<TAKE>{take}</TAKE><TAPE>{tape}</TAPE>{extra}<TRACK_LIST><TRACK_COUNT>{len(tracks)}</TRACK_COUNT>"
           f"{track_xml}</TRACK_LIST></BWFXML>")
    return chunk(b"iXML", xml.encode())


def data_chunk(frames: int = 4800, channels: int = 2, bits: int = 24) -> bytes:
    return chunk(b"data", b"\0" * (frames * channels * bits // 8))


def riff(*chunks: bytes) -> bytes:
    body = b"WAVE" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def rf64(*chunks: bytes, data_frames: int = 4800, channels: int = 2, bits: int = 24) -> bytes:
    """RF64 with a ds64 chunk. The data chunk declares 0xFFFFFFFF and its true size sits in ds64."""
    data_bytes = data_frames * channels * bits // 8
    ds64 = chunk(b"ds64", struct.pack("<QQQI", 0, data_bytes, data_frames, 0))
    before, after = chunks
    data = chunk(b"data", b"\0" * data_bytes, declared=0xFFFFFFFF)
    body = b"WAVE" + ds64 + before + data + after
    return b"RF64" + struct.pack("<I", 0xFFFFFFFF) + body
