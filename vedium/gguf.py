# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Minimal GGUF metadata reader used for automatic prompt-format detection.

Only the key/value block at the front of the file is parsed; tensor data is
never touched, so detection stays cheap even for very large models.
"""

import struct


_FMT = {
    0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i",
    6: "f", 7: "B", 10: "Q", 11: "q", 12: "d",
}
_TYPE_STRING = 8
_TYPE_ARRAY = 9


def read_metadata(path: str, wanted) -> dict:
    """Return selected GGUF metadata values, or what could be read safely.

    A wanted key beginning with ``.`` matches a metadata-key suffix. This is
    useful for architecture-specific names such as ``qwen35.block_count``.
    """
    found = {}
    try:
        with open(path, "rb") as handle:
            if handle.read(4) != b"GGUF":
                return {}
            struct.unpack("<I", handle.read(4))[0]  # GGUF version
            handle.read(8)  # tensor count
            kv_count = struct.unpack("<Q", handle.read(8))[0]

            def read_str():
                size = struct.unpack("<Q", handle.read(8))[0]
                return handle.read(size).decode("utf-8", errors="replace")

            def read_value(value_type):
                if value_type == _TYPE_STRING:
                    return read_str()
                if value_type == _TYPE_ARRAY:
                    element_type = struct.unpack("<I", handle.read(4))[0]
                    count = struct.unpack("<Q", handle.read(8))[0]
                    for _ in range(count):
                        read_value(element_type)
                    return None
                fmt = _FMT.get(value_type)
                if fmt is None:
                    raise ValueError(f"unknown GGUF value type {value_type}")
                return struct.unpack("<" + fmt, handle.read(struct.calcsize(fmt)))[0]

            for _ in range(kv_count):
                key = read_str()
                value_type = struct.unpack("<I", handle.read(4))[0]
                value = read_value(value_type)
                if value is None:
                    continue
                for want in wanted:
                    if key == want or (want.startswith(".") and key.endswith(want)):
                        found[want] = value
                if len(found) == len(wanted):
                    break
        return found
    except Exception:
        return found
