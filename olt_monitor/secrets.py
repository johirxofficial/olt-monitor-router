"""Reversible at-rest encryption for the secrets kept in the device registry.

SNMP communities, RouterOS passwords and Telnet credentials have to be usable
by the poller, so hashing is not an option - they must be decryptable.  This
module implements a small, dependency-free envelope:

    enc:v1:<base64( nonce | ciphertext | tag )>

* **ChaCha20** (RFC 8439, pure Python) encrypts the value.
* **HMAC-SHA256** authenticates the nonce and ciphertext (encrypt-then-MAC).
* The two sub-keys are derived from a 32-byte master key stored in
  ``data/.secret_key`` in the local data directory (mode 0600, created on first
  use).  The database itself is MySQL; the key never leaves the host.

Anything that does not start with ``enc:v1:`` is treated as plain text, so an
old plain-text registry keeps working and is re-sealed on the next save.

The key file is the only thing that matters: back it up together with the
database, and never commit it.  If it is lost the encrypted values simply stop decrypting and
the device has to be re-entered - nothing else breaks.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
from pathlib import Path
from typing import Any

PREFIX = "enc:v1:"
KEY_FILENAME = ".secret_key"
_NONCE_BYTES = 12
_TAG_BYTES = 32
_KEY_BYTES = 32

_KEY_CACHE: dict[str, bytes] = {}


# ---------------------------------------------------------------------------
#  ChaCha20 (RFC 8439) - block function and stream cipher
# ---------------------------------------------------------------------------
def _mask(value: int) -> int:
    return value & 0xFFFFFFFF


def _rotl(value: int, bits: int) -> int:
    return _mask((value << bits) | (value >> (32 - bits)))


def _quarter(state: list[int], a: int, b: int, c: int, d: int) -> None:
    state[a] = _mask(state[a] + state[b])
    state[d] = _rotl(state[d] ^ state[a], 16)
    state[c] = _mask(state[c] + state[d])
    state[b] = _rotl(state[b] ^ state[c], 12)
    state[a] = _mask(state[a] + state[b])
    state[d] = _rotl(state[d] ^ state[a], 8)
    state[c] = _mask(state[c] + state[d])
    state[b] = _rotl(state[b] ^ state[c], 7)


def _block(key: bytes, counter: int, nonce: bytes) -> bytes:
    constants = struct.unpack("<4I", b"expand 32-byte k")
    state = list(constants) + list(struct.unpack("<8I", key)) + [counter] + list(
        struct.unpack("<3I", nonce)
    )
    working = state[:]
    for _ in range(10):  # 10 double rounds = 20 rounds
        _quarter(working, 0, 4, 8, 12)
        _quarter(working, 1, 5, 9, 13)
        _quarter(working, 2, 6, 10, 14)
        _quarter(working, 3, 7, 11, 15)
        _quarter(working, 0, 5, 10, 15)
        _quarter(working, 1, 6, 11, 12)
        _quarter(working, 2, 7, 8, 13)
        _quarter(working, 3, 4, 9, 14)
    return struct.pack("<16I", *[_mask(working[i] + state[i]) for i in range(16)])


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 1  # RFC 8439: block 0 is reserved for Poly1305
    while len(out) < length:
        out.extend(_block(key, counter, nonce))
        counter += 1
    return bytes(out[:length])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


# ---------------------------------------------------------------------------
#  key file
# ---------------------------------------------------------------------------
def key_path_for(db_path: str | Path) -> Path:
    """The key lives next to the database, so both travel together."""
    return Path(db_path).resolve().parent / KEY_FILENAME


def _read_key(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except OSError:
        return b""
    return data[: _KEY_BYTES] if len(data) >= _KEY_BYTES else b""


def master_key(path: Path) -> bytes:
    """Load the master key, creating the file atomically on first use."""
    cached = _KEY_CACHE.get(str(path))
    if cached:
        return cached

    existing = _read_key(path)
    if existing:
        _KEY_CACHE[str(path)] = existing
        return existing

    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = os.urandom(_KEY_BYTES)
    try:
        # O_EXCL: if two processes race, exactly one of them creates the file
        handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = _read_key(path)
        if existing:
            _KEY_CACHE[str(path)] = existing
            return existing
        # a zero-length file from a crashed writer: replace it
        path.write_bytes(fresh)
    else:
        with os.fdopen(handle, "wb") as fh:
            fh.write(fresh)

    try:  # best effort on POSIX; Windows ignores the mode
        os.chmod(path, 0o600)
    except OSError:
        pass

    _KEY_CACHE[str(path)] = fresh
    return fresh


def _subkeys(path: Path) -> tuple[bytes, bytes]:
    master = master_key(path)
    enc = hmac.new(master, b"olt-monitor/enc", hashlib.sha256).digest()
    mac = hmac.new(master, b"olt-monitor/mac", hashlib.sha256).digest()
    return enc, mac


# ---------------------------------------------------------------------------
#  public API
# ---------------------------------------------------------------------------
def is_sealed(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def seal(value: Any, db_path: str | Path) -> str:
    """Encrypt *value* for storage.  Empty values and already-sealed stay as-is."""
    text = "" if value is None else str(value)
    if not text or is_sealed(text):
        return text

    path = key_path_for(db_path)
    enc_key, mac_key = _subkeys(path)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = _xor(text.encode("utf-8"), _keystream(enc_key, nonce, len(text.encode("utf-8"))))
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    payload = base64.b64encode(nonce + ciphertext + tag).decode("ascii")
    return PREFIX + payload


def reveal(value: Any, db_path: str | Path) -> str:
    """Decrypt a stored value.  Plain text passes through unchanged.

    A value that cannot be authenticated (wrong/rotated key, corrupted row)
    decrypts to an empty string instead of raising: the device then shows as
    "credential missing" and can be re-entered, rather than breaking the poller.
    """
    text = "" if value is None else str(value)
    if not text:
        return ""
    if not is_sealed(text):
        return text

    try:
        raw = base64.b64decode(text[len(PREFIX):].encode("ascii"), validate=True)
    except (ValueError, TypeError):
        return ""
    if len(raw) <= _NONCE_BYTES + _TAG_BYTES:
        return ""

    nonce = raw[:_NONCE_BYTES]
    ciphertext = raw[_NONCE_BYTES:-_TAG_BYTES]
    tag = raw[-_TAG_BYTES:]

    path = key_path_for(db_path)
    try:
        enc_key, mac_key = _subkeys(path)
    except OSError:
        return ""

    expected = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        return ""
    try:
        return _xor(ciphertext, _keystream(enc_key, nonce, len(ciphertext))).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return ""


def seal_many(values: dict[str, Any], fields: tuple[str, ...], db_path: str | Path) -> dict[str, Any]:
    """:func:`seal` every key of *values* listed in *fields*."""
    out = dict(values)
    for field in fields:
        if field in out and out[field] not in (None, ""):
            out[field] = seal(out[field], db_path)
    return out


def reveal_many(values: dict[str, Any], fields: tuple[str, ...], db_path: str | Path) -> dict[str, Any]:
    """:func:`reveal` every key of *values* listed in *fields*."""
    out = dict(values)
    for field in fields:
        if field in out and out[field]:
            out[field] = reveal(out[field], db_path)
    return out
