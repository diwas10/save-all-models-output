"""Schema-less protobuf reader: walks the wire format and pulls out the
human-readable strings. Used for Antigravity, which stores conversation
steps as protobuf blobs with no published .proto schema."""


def _varint(b, i):
    shift = val = 0
    while True:
        if i >= len(b):
            raise ValueError("truncated varint")
        c = b[i]; i += 1
        val |= (c & 0x7F) << shift
        if not c & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def decode(b: bytes, depth=0):
    """Return list of (field_number, value) where value is int | str | list | bytes."""
    out, i = [], 0
    while i < len(b):
        key, i = _varint(b, i)
        field, wt = key >> 3, key & 7
        if field == 0:
            raise ValueError("field 0")
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = int.from_bytes(b[i:i + 8], "little"), i + 8
        elif wt == 5:
            v, i = int.from_bytes(b[i:i + 4], "little"), i + 4
        elif wt == 2:
            n, i = _varint(b, i)
            if i + n > len(b):
                raise ValueError("truncated")
            raw = b[i:i + n]; i += n
            v = None
            if depth < 40 and raw:
                try:
                    v = decode(raw, depth + 1)
                except ValueError:
                    v = None
            if v is None or (v and _looks_text(raw)):
                try:
                    v = raw.decode("utf-8")
                except UnicodeDecodeError:
                    v = raw if v is None else v
        else:
            raise ValueError(f"wire type {wt}")
        out.append((field, v))
    return out


def _looks_text(raw: bytes) -> bool:
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(ch.isprintable() or ch in "\n\t\r" for ch in s)
    return len(s) >= 2 and printable / len(s) > 0.95


def strings(b: bytes, min_len=1):
    """All text strings in the message, in order, with their field path."""
    res = []

    def walk(items, path):
        for f, v in items:
            p = f"{path}.{f}" if path else str(f)
            if isinstance(v, str):
                if len(v) >= min_len:
                    res.append((p, v))
            elif isinstance(v, list):
                walk(v, p)

    try:
        walk(decode(b), "")
    except ValueError:
        pass
    return res
