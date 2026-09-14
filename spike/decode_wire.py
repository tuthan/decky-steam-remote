"""Inspect a captured base64 protobuf wire payload without inventing a schema.

Usage: python decode_wire.py /path/to/captured-payload.base64
Field numbers and wire values are evidence only, not width/height/mode labels.
"""
import base64
import json
from pathlib import Path
import sys


def varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("truncated varint")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ValueError("varint exceeds 64 bits")
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise ValueError("invalid varint")


def decode(payload):
    if len(payload) > 24000:
        raise ValueError("payload too large")
    data = base64.b64decode(payload.strip(), validate=True)
    fields = []
    pos = 0
    while pos < len(data):
        if len(fields) >= 256:
            raise ValueError("too many fields")
        key, pos = varint(data, pos)
        number, wire = key >> 3, key & 7
        if not 1 <= number <= 0x1FFFFFFF:
            raise ValueError("invalid field number")
        field = {"field": number, "wire": wire}
        if wire == 0:
            field["unsigned_varint"], pos = varint(data, pos)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, pos = varint(data, pos)
            else:
                size = 8 if wire == 1 else 4
            if size > len(data) - pos:
                raise ValueError("truncated field")
            raw = data[pos:pos + size]
            pos += size
            field.update(length=size, hex=raw.hex())
        else:
            raise ValueError(f"unsupported wire type {wire}; do not infer schema")
        fields.append(field)
    return {"bytes": len(data), "fields": fields, "schema_verified": False}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python decode_wire.py captured-payload.base64")
    print(json.dumps(decode(Path(sys.argv[1]).read_text()), indent=2))
