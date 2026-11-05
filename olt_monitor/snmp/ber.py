"""Minimal BER / SNMP encoding and decoding (standard library only).

Deliberately dependency-free: the whole project talks to the OLT with nothing
but ``socket``. Only the subset of SNMP v1/v2c that a monitoring tool needs is
implemented (GET, GETNEXT, GETBULK, response parsing).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---- tags ---------------------------------------------------------------
TAG_BOOLEAN = 0x01
TAG_INTEGER = 0x02
TAG_BITSTRING = 0x03
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_SEQUENCE = 0x30
TAG_IPADDRESS = 0x40
TAG_COUNTER32 = 0x41
TAG_GAUGE32 = 0x42
TAG_TIMETICKS = 0x43
TAG_OPAQUE = 0x44
TAG_COUNTER64 = 0x46

# v2c "exceptions"
TAG_NO_SUCH_OBJECT = 0x80
TAG_NO_SUCH_INSTANCE = 0x81
TAG_END_OF_MIB_VIEW = 0x82

EXCEPTION_TAGS = (TAG_NO_SUCH_OBJECT, TAG_NO_SUCH_INSTANCE, TAG_END_OF_MIB_VIEW)

# ---- PDU types ----------------------------------------------------------
PDU_GET = 0xA0
PDU_GETNEXT = 0xA1
PDU_RESPONSE = 0xA2
PDU_SET = 0xA3
PDU_GETBULK = 0xA5

# error-status values
ERR_NO_ERROR = 0
ERR_TOO_BIG = 1
ERR_NO_SUCH_NAME = 2
ERR_BAD_VALUE = 3
ERR_READ_ONLY = 4
ERR_GEN_ERR = 5

ERROR_NAMES = {
    0: "noError",
    1: "tooBig",
    2: "noSuchName",
    3: "badValue",
    4: "readOnly",
    5: "genErr",
}

UNSIGNED_TAGS = (TAG_COUNTER32, TAG_GAUGE32, TAG_TIMETICKS, TAG_COUNTER64)


class BerError(ValueError):
    """Raised when a buffer cannot be decoded as BER."""


class SnmpProtocolError(RuntimeError):
    """Raised when the agent answers with a non-zero error-status."""

    def __init__(self, status: int, index: int = 0) -> None:
        self.status = status
        self.index = index
        name = ERROR_NAMES.get(status, f"error({status})")
        super().__init__(f"SNMP error-status={name} index={index}")


# -------------------------------------------------------------------------
#  encoding
# -------------------------------------------------------------------------
def encode_length(length: int) -> bytes:
    if length < 0:
        raise BerError("length must be >= 0")
    if length < 0x80:
        return bytes([length])
    if length < 0x100:
        return bytes([0x81, length])
    if length < 0x10000:
        return bytes([0x82, (length >> 8) & 0xFF, length & 0xFF])
    if length < 0x1000000:
        return bytes([0x83, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])
    return bytes([0x84]) + length.to_bytes(4, "big")


def encode_tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + encode_length(len(content)) + content


def encode_integer(value: int) -> bytes:
    """Signed INTEGER, minimal two's-complement length."""
    if value == 0:
        return encode_tlv(TAG_INTEGER, b"\x00")
    if value > 0:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        if raw[0] & 0x80:  # keep the sign bit clear for positive numbers
            raw = b"\x00" + raw
    else:
        size = (value.bit_length() + 8) // 8
        size = max(1, size)
        raw = value.to_bytes(size, "big", signed=True)
        while len(raw) > 1 and raw[0] == 0xFF and (raw[1] & 0x80):
            raw = raw[1:]
    return encode_tlv(TAG_INTEGER, raw)


def encode_unsigned(value: int, tag: int = TAG_GAUGE32) -> bytes:
    if value < 0:
        value = 0
    size = max(1, (value.bit_length() + 7) // 8)
    return encode_tlv(tag, value.to_bytes(size, "big"))


def encode_octet_string(value: Any) -> bytes:
    if isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raw = str(value).encode("utf-8")
    return encode_tlv(TAG_OCTET_STRING, raw)


def encode_null() -> bytes:
    return encode_tlv(TAG_NULL, b"")


def encode_oid(oid: str) -> bytes:
    """Encode a dotted-decimal OID string into a BER OBJECT IDENTIFIER TLV."""
    parts = [p for p in str(oid).strip().split(".") if p != ""]
    if len(parts) < 2:
        raise BerError(f"OID needs at least two arcs: {oid!r}")
    try:
        arcs = [int(p) for p in parts]
    except ValueError as exc:
        raise BerError(f"invalid OID {oid!r}: {exc}") from exc

    if arcs[0] not in (0, 1, 2):
        raise BerError(f"first OID arc must be 0..2, got {arcs[0]}")
    if arcs[0] < 2 and arcs[1] > 39:
        raise BerError(f"second OID arc must be <=39 when first is {arcs[0]}")

    out = bytearray()
    out.append(arcs[0] * 40 + arcs[1])
    for arc in arcs[2:]:
        if arc < 0:
            raise BerError("negative OID arc")
        if arc < 0x80:
            out.append(arc)
            continue
        chunk = bytearray()
        while arc > 0:
            chunk.insert(0, arc & 0x7F)
            arc >>= 7
        for i, byte in enumerate(chunk):
            out.append(byte | 0x80 if i < len(chunk) - 1 else byte)
    return encode_tlv(TAG_OID, bytes(out))


def encode_sequence(*items: bytes) -> bytes:
    return encode_tlv(TAG_SEQUENCE, b"".join(items))


# -------------------------------------------------------------------------
#  decoding
# -------------------------------------------------------------------------
@dataclass
class Tlv:
    tag: int
    content: bytes
    end: int


def read_tlv(data: bytes, offset: int = 0) -> Tlv:
    if offset >= len(data):
        raise BerError("unexpected end of data")
    tag = data[offset]
    offset += 1
    if offset >= len(data):
        raise BerError("truncated length field")

    first = data[offset]
    offset += 1
    if first & 0x80:
        count = first & 0x7F
        if count == 0 or count > 4:
            raise BerError(f"unsupported length form (count={count})")
        if offset + count > len(data):
            raise BerError("truncated long-form length")
        length = 0
        for _ in range(count):
            length = (length << 8) | data[offset]
            offset += 1
    else:
        length = first

    end = offset + length
    if end > len(data):
        raise BerError("declared length exceeds available data")
    return Tlv(tag=tag, content=data[offset:end], end=end)


def decode_oid(content: bytes) -> str:
    if not content:
        return ""
    first = content[0]
    arcs = [first // 40, first % 40]
    value = 0
    started = False
    for byte in content[1:]:
        value = (value << 7) | (byte & 0x7F)
        started = True
        if not byte & 0x80:
            arcs.append(value)
            value = 0
            started = False
    if started:  # malformed trailing continuation byte
        arcs.append(value)
    return ".".join(str(a) for a in arcs)


def _decode_int(content: bytes, signed: bool) -> int:
    if not content:
        return 0
    value = int.from_bytes(content, "big", signed=signed)
    return value


def decode_value(tag: int, content: bytes) -> Any:
    """Return the Python value for a varbind.

    v2c exception tags and NULL decode to ``None``.  OCTET STRINGs that are not
    valid UTF-8 are returned as ``bytes`` (e.g. raw 6-byte MAC addresses).
    """
    if tag in EXCEPTION_TAGS or tag == TAG_NULL:
        return None
    if tag == TAG_INTEGER:
        return _decode_int(content, signed=True)
    if tag in UNSIGNED_TAGS:
        return _decode_int(content, signed=False)
    if tag == TAG_OID:
        return decode_oid(content)
    if tag == TAG_IPADDRESS:
        if len(content) == 4:
            return ".".join(str(b) for b in content)
        return content.hex()
    if tag == TAG_OCTET_STRING:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return bytes(content)
    if tag == TAG_BOOLEAN:
        return bool(content and content[0])
    if tag == TAG_OPAQUE:
        return bytes(content)
    return bytes(content)


def format_ticks(ticks: int | None) -> str:
    """Format TimeTicks (hundredths of a second) as ``1d 02:03:04``."""
    if ticks is None:
        return "n/a"
    total_seconds = int(ticks) // 100
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_bytes(value: int | None) -> str:
    if value is None:
        return "n/a"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} TB"


def format_bps(value: float | None) -> str:
    if value is None:
        return "n/a"
    bits = float(value)
    for unit in ("bps", "Kbps", "Mbps", "Gbps"):
        if abs(bits) < 1000.0 or unit == "Gbps":
            return f"{bits:.1f} {unit}"
        bits /= 1000.0
    return f"{bits:.1f} Gbps"


def format_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "n/a"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_dbm(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f} dBm"


def to_mac(value: Any) -> str | None:
    """Normalise a MAC address coming from SNMP (bytes or string) or an API."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        if len(raw) == 6:
            return ":".join(f"{b:02X}" for b in raw)
        text = raw.decode("utf-8", errors="ignore")
    else:
        text = str(value)
    text = text.strip().upper()
    if not text:
        return None
    hex_only = "".join(ch for ch in text if ch in "0123456789ABCDEF")
    if len(hex_only) == 12 and len(text.replace(":", "").replace("-", "").replace(".", "")) == 12:
        return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))
    return text if text else None
