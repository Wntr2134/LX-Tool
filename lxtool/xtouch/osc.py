"""A minimal OSC 1.0 codec - just what talking to grandMA3 needs.

grandMA3 sends and receives plain OSC messages over UDP: an address like
/Page1/Fader201, a type tag string, and int/float/string arguments. No
bundles, no timetags, no pattern matching - so this is ~80 lines instead
of a dependency, and every byte of it is tested.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Message:
    address: str
    args: tuple = field(default_factory=tuple)


def _pad_str(s: str) -> bytes:
    raw = s.encode("utf-8") + b"\x00"
    return raw + b"\x00" * ((4 - len(raw) % 4) % 4)


def encode(msg: Message) -> bytes:
    tags = ","
    payload = b""
    for a in msg.args:
        if isinstance(a, bool):        # bool before int: bool IS an int
            tags += "T" if a else "F"
        elif isinstance(a, int):
            tags += "i"
            payload += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"
            payload += struct.pack(">f", a)
        elif isinstance(a, str):
            tags += "s"
            payload += _pad_str(a)
        else:
            raise TypeError(f"cannot encode OSC argument {a!r}")
    return _pad_str(msg.address) + _pad_str(tags) + payload


def decode(data: bytes) -> Message | None:
    """One datagram -> one Message, or None for anything malformed.

    Malformed input is expected in real life (other tools share the port);
    it must never take the bridge down.
    """
    try:
        address, rest = _take_str(data)
        if not address.startswith("/"):
            return None
        if not rest:
            return Message(address)
        tags, rest = _take_str(rest)
        if not tags.startswith(","):
            return Message(address)
        args: list = []
        for t in tags[1:]:
            if t == "i":
                (v,), rest = struct.unpack(">i", rest[:4]), rest[4:]
                args.append(v)
            elif t == "f":
                (v,), rest = struct.unpack(">f", rest[:4]), rest[4:]
                args.append(v)
            elif t == "s":
                v, rest = _take_str(rest)
                args.append(v)
            elif t == "T":
                args.append(True)
            elif t == "F":
                args.append(False)
            else:
                return None            # blobs etc: not something MA3 sends us
        return Message(address, tuple(args))
    except (struct.error, ValueError, IndexError):
        return None


def _take_str(data: bytes) -> tuple[str, bytes]:
    end = data.index(b"\x00")
    consumed = end + 1
    consumed += (4 - consumed % 4) % 4
    return data[:end].decode("utf-8"), data[consumed:]
