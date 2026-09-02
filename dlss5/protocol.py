"""Wire format of the DLSS 5 neural-rendering worker (protocol version 4).

The worker is a native D3D12 process that hosts ReShade plus the RenoDX DLSS 5
add-on and evaluates NGX feature 18.  It speaks a small binary protocol over
stdin/stdout:

    client -> worker : video header (once)
    worker -> client : setup response (once)
    client -> worker : frame header + RGBA8 pixels + FP16 motion  (per frame)
    worker -> client : result header + RGBA8 pixels               (per frame)

All structures are little-endian and tightly packed.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import BinaryIO

import numpy as np

VIDEO_MAGIC = 0x34563544
SETUP_MAGIC = 0x34505553
FRAME_MAGIC = 0x314D5246
OUT_MAGIC = 0x3154554F

VIDEO_HEADER = struct.Struct("<14I4f")
SETUP_RESPONSE = struct.Struct("<12I")
FRAME_HEADER = struct.Struct("<4Iq")
RESULT_HEADER = struct.Struct("<5Iq")


def pack_video_header(
    *,
    input_width: int,
    input_height: int,
    output_width: int,
    output_height: int,
    warmup_frames: int,
    frame_count: int,
    perf_quality: int,
    native: dict[str, int | float],
) -> bytes:
    """Build the one-time session header from validated native settings."""
    return VIDEO_HEADER.pack(
        VIDEO_MAGIC,
        input_width,
        input_height,
        output_width,
        output_height,
        warmup_frames,
        frame_count,
        perf_quality,
        int(native["dlss_model_preset"]),
        int(native["profile"]),
        int(native["preset"]),
        int(native["style"]),
        int(native["auto_mask"]),
        int(native["ui_correction"]),
        float(native["intensity"]),
        float(native["local_tone"]),
        float(native["local_structure"]),
        float(native["skin_structure"]),
    )


@dataclass(frozen=True, slots=True)
class SetupResponse:
    """Dimensions and limits the worker negotiated for this session."""

    ok: int
    result: int
    render_width: int
    render_height: int
    output_width: int
    output_height: int
    minimum_width: int
    minimum_height: int
    maximum_width: int
    maximum_height: int
    applied_model_preset: int

    @classmethod
    def unpack(cls, payload: bytes) -> "SetupResponse":
        fields = SETUP_RESPONSE.unpack(payload)
        if fields[0] != SETUP_MAGIC:
            raise RuntimeError(
                "The native worker did not answer with a version-4 setup response. "
                "The installed runtime is incompatible with this node."
            )
        return cls(*fields[1:])


@dataclass(frozen=True, slots=True)
class ResultHeader:
    """Per-frame acknowledgement that precedes the returned pixels."""

    index: int
    ok: int
    byte_count: int
    ngx_result: int
    pts: int

    @classmethod
    def unpack(cls, payload: bytes) -> "ResultHeader":
        magic, index, ok, byte_count, ngx_result, pts = RESULT_HEADER.unpack(payload)
        if magic != OUT_MAGIC:
            raise RuntimeError("The native worker sent a malformed frame result header.")
        return cls(index, ok, byte_count, ngx_result, pts)


def pack_frame_header(index: int, reset: bool, pts: int) -> bytes:
    return FRAME_HEADER.pack(FRAME_MAGIC, index, int(reset), 0, pts)


def read_exact(stream: BinaryIO, size: int) -> bytes:
    """Read exactly ``size`` bytes or raise ``EOFError`` when the worker dies."""
    buffer = bytearray(size)
    view = memoryview(buffer)
    offset = 0
    while offset < size:
        count = stream.readinto(view[offset:])
        if not count:
            raise EOFError(f"worker stopped after {offset} of {size} bytes")
        offset += count
    return bytes(buffer)


def read_into(stream: BinaryIO, target: np.ndarray) -> None:
    """Fill a contiguous array straight from the pipe, without a copy."""
    view = memoryview(target).cast("B")
    offset = 0
    while offset < len(view):
        count = stream.readinto(view[offset:])
        if not count:
            raise EOFError(f"worker stopped after {offset} of {len(view)} bytes")
        offset += count


def payload_bytes(array: np.ndarray, dtype: np.dtype) -> memoryview:
    """Return a byte view of ``array``, converting only when necessary."""
    return memoryview(np.ascontiguousarray(array, dtype=dtype)).cast("B")
