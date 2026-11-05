"""SNMP v1 / v2c client (standard library only).

Implements GET, GETNEXT and GETBULK against UDP/161 with request-id matching,
retries and a proper subtree walk.  No third-party SNMP package required, so
there is nothing to break when a library changes its API.
"""

from __future__ import annotations

import random
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import ber
from .ber import (
    ERR_NO_SUCH_NAME,
    PDU_GET,
    PDU_GETBULK,
    PDU_GETNEXT,
    PDU_RESPONSE,
    SnmpProtocolError,
)

DEFAULT_PORT = 161
DEFAULT_TIMEOUT = 4.0
DEFAULT_RETRIES = 2


class SnmpError(Exception):
    """Base class for SNMP transport problems."""


class SnmpTimeout(SnmpError):
    """No (matching) response arrived before the timeout."""


@dataclass
class VarBind:
    oid: str
    tag: int
    value: Any

    @property
    def is_exception(self) -> bool:
        return self.tag in ber.EXCEPTION_TAGS

    @property
    def is_counter(self) -> bool:
        return self.tag == ber.TAG_COUNTER64

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"VarBind({self.oid!r}, tag=0x{self.tag:02X}, value={self.value!r})"


@dataclass
class SnmpResponse:
    status: int
    index: int
    varbinds: list[VarBind] = field(default_factory=list)


def _normalise_version(version: Any) -> int:
    """Return 0 for SNMPv1 and 1 for SNMPv2c."""
    text = str(version).strip().lower()
    if text in ("1", "v1", "snmpv1"):
        return 0
    if text in ("2", "2c", "v2", "v2c", "snmpv2c", "snmpv2"):
        return 1
    raise ValueError(f"unsupported SNMP version: {version!r} (use '1' or '2c')")


class SnmpClient:
    """Synchronous SNMP v1/v2c client.

    Not thread-safe: create one instance per worker thread.
    """

    def __init__(
        self,
        host: str,
        community: str = "public",
        version: Any = "2c",
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        max_repetitions: int = 25,
    ) -> None:
        self.host = host
        self.community = community
        self.port = int(port)
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.max_repetitions = max(1, int(max_repetitions))
        self.version_code = _normalise_version(version)

        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    # -- plumbing --------------------------------------------------------
    @property
    def version_label(self) -> str:
        return "1" if self.version_code == 0 else "2c"

    def _socket(self) -> socket.socket:
        if self._sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
            try:
                sock.connect((self.host, self.port))
            except OSError as exc:
                sock.close()
                raise SnmpError(f"cannot reach {self.host}:{self.port}: {exc}") from exc
            self._sock = sock
        return self._sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "SnmpClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _build(self, pdu_tag: int, oids: Iterable[str], request_id: int,
               non_repeaters: int = 0, max_repetitions: int = 0) -> bytes:
        varbinds = b"".join(
            ber.encode_sequence(ber.encode_oid(oid), ber.encode_null()) for oid in oids
        )
        if pdu_tag == PDU_GETBULK:
            middle = ber.encode_integer(non_repeaters) + ber.encode_integer(max_repetitions)
        else:
            middle = ber.encode_integer(0) + ber.encode_integer(0)
        pdu = ber.encode_tlv(
            pdu_tag,
            ber.encode_integer(request_id) + middle + ber.encode_sequence(varbinds),
        )
        return ber.encode_sequence(
            ber.encode_integer(self.version_code),
            ber.encode_octet_string(self.community),
            pdu,
        )

    def _parse(self, data: bytes, request_id: int) -> SnmpResponse | None:
        """Parse a datagram; returns None when it belongs to another request."""
        try:
            message = ber.read_tlv(data, 0)
            if message.tag != ber.TAG_SEQUENCE:
                return None
            version = ber.read_tlv(message.content, 0)
            community = ber.read_tlv(message.content, version.end)
            pdu = ber.read_tlv(message.content, community.end)

            if ber.decode_value(version.tag, version.content) != self.version_code:
                return None
            if community.content.decode("utf-8", errors="replace") != self.community:
                return None
            if pdu.tag != PDU_RESPONSE:
                return None

            rid = ber.read_tlv(pdu.content, 0)
            if ber.decode_value(rid.tag, rid.content) != request_id:
                return None
            status = ber.read_tlv(pdu.content, rid.end)
            index = ber.read_tlv(pdu.content, status.end)
            vb_list = ber.read_tlv(pdu.content, index.end)

            varbinds: list[VarBind] = []
            offset = 0
            while offset < len(vb_list.content):
                try:
                    one = ber.read_tlv(vb_list.content, offset)
                except ber.BerError:
                    break
                offset = one.end
                try:
                    oid_tlv = ber.read_tlv(one.content, 0)
                    val_tlv = ber.read_tlv(one.content, oid_tlv.end)
                except ber.BerError:
                    break
                varbinds.append(
                    VarBind(
                        oid=ber.decode_oid(oid_tlv.content),
                        tag=val_tlv.tag,
                        value=ber.decode_value(val_tlv.tag, val_tlv.content),
                    )
                )

            return SnmpResponse(
                status=int(ber.decode_value(status.tag, status.content) or 0),
                index=int(ber.decode_value(index.tag, index.content) or 0),
                varbinds=varbinds,
            )
        except (ber.BerError, IndexError, ValueError, UnicodeDecodeError):
            return None

    def _transact(
        self,
        pdu_tag: int,
        oids: list[str],
        bulk: bool = False,
        max_repetitions: int | None = None,
    ) -> SnmpResponse:
        request_id = random.randint(1, 0x7FFFFFF0)
        if bulk:
            packet = self._build(
                pdu_tag, oids, request_id,
                non_repeaters=0,
                max_repetitions=max(1, int(max_repetitions or self.max_repetitions)),
            )
        else:
            packet = self._build(pdu_tag, oids, request_id)

        last_error: Exception | None = None
        with self._lock:
            sock = self._socket()
            for _attempt in range(self.retries + 1):
                try:
                    sock.send(packet)
                except OSError as exc:  # socket went away -> rebuild once
                    self.close()
                    sock = self._socket()
                    try:
                        sock.send(packet)
                    except OSError as exc2:
                        last_error = exc2
                        continue

                deadline = time.monotonic() + self.timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        sock.settimeout(remaining)
                        data = sock.recv(65535)
                    except socket.timeout:
                        break
                    except OSError as exc:
                        # ICMP port-unreachable shows up here on Windows
                        last_error = exc
                        break
                    response = self._parse(data, request_id)
                    if response is not None:
                        return response
                    # a datagram from an older/slower request: keep waiting

        raise SnmpTimeout(
            f"no SNMP response from {self.host}:{self.port} "
            f"(community={self.community!r}) after {self.retries + 1} attempt(s)"
            + (f": {last_error}" if last_error else "")
        )

    # -- public API ------------------------------------------------------
    def get(self, oids: str | Iterable[str]) -> list[VarBind]:
        """GET one or more OIDs.  Missing objects come back as exception tags."""
        if isinstance(oids, str):
            oids = [oids]
        oid_list = list(oids)
        if not oid_list:
            return []
        response = self._transact(PDU_GET, oid_list)
        if response.status == ERR_NO_SUCH_NAME:  # v1
            return [VarBind(oid, ber.TAG_NO_SUCH_OBJECT, None) for oid in oid_list]
        if response.status != 0:
            raise SnmpProtocolError(response.status, response.index)
        return response.varbinds

    def get_map(self, oids: str | Iterable[str]) -> dict[str, Any]:
        """Tolerant GET: returns ``{oid: value}`` and never raises on soft errors."""
        try:
            return {vb.oid: vb.value for vb in self.get(oids)}
        except (SnmpError, SnmpProtocolError):
            return {}

    def get_value(self, oid: str, default: Any = None) -> Any:
        try:
            binds = self.get([oid])
        except (SnmpError, SnmpProtocolError):
            return default
        for vb in binds:
            if vb.oid == oid and not vb.is_exception:
                return vb.value
        return default

    def get_next(self, oid: str) -> VarBind | None:
        """GETNEXT; returns None at the end of the MIB view."""
        try:
            response = self._transact(PDU_GETNEXT, [oid])
        except (SnmpError, SnmpProtocolError):
            return None
        if response.status == ERR_NO_SUCH_NAME or not response.varbinds:
            return None
        vb = response.varbinds[0]
        if vb.is_exception:
            return None
        return vb

    def walk(
        self,
        base_oid: str,
        max_rows: int = 100_000,
        use_bulk: bool | None = None,
    ) -> list[VarBind]:
        """Walk the subtree below *base_oid*.

        Uses GETBULK on v2c (far fewer round-trips over ~400 interfaces) and
        falls back to GETNEXT automatically, also when a device rejects
        GETBULK with ``genErr``.
        """
        base = base_oid.rstrip(".")
        if use_bulk is None:
            use_bulk = self.version_code == 1

        results: list[VarBind] = []
        current = base

        if use_bulk:
            # Slow GPON agents (VSOL V1600G1B) answer a 25-repetition GETBULK
            # in ~8 s but a 10-repetition one in ~2 s.  A fixed 4 s timeout
            # therefore used to cut a 500-row table off after the first
            # response.  Shrink the repetition count when that happens instead
            # of giving up, and fall back to GETNEXT only as a last resort.
            reps = max(1, int(self.max_repetitions))
            bulk_failed = False
            while len(results) < max_rows:
                try:
                    response = self._transact(
                        PDU_GETBULK, [current], bulk=True, max_repetitions=reps
                    )
                except SnmpError:
                    if reps > 8:
                        reps = max(1, reps // 3)
                        continue
                    bulk_failed = True
                    break
                if response.status != 0:
                    bulk_failed = True  # device dislikes GETBULK -> GETNEXT
                    break
                if not response.varbinds:
                    break

                progressed = False
                stop = False
                for vb in response.varbinds:
                    if vb.is_exception or not self._in_subtree(vb.oid, base):
                        stop = True
                        break
                    results.append(vb)
                    progressed = True
                    if len(results) >= max_rows:
                        break
                if stop or not progressed:
                    return results

                last = results[-1].oid
                if last == current:
                    bulk_failed = True
                    break
                current = last
            if not bulk_failed:
                return results
            # Re-start below so a partial/duplicated bulk result cannot leak
            # into the GETNEXT result.
            results = []
            current = base

        # ---- GETNEXT fallback ----
        while len(results) < max_rows:
            vb = self.get_next(current)
            if vb is None or not self._in_subtree(vb.oid, base):
                break
            results.append(vb)
            if vb.oid == current:
                break
            current = vb.oid
        return results

    def walk_map(
        self,
        base_oid: str,
        max_rows: int = 100_000,
        use_bulk: bool | None = None,
    ) -> dict[str, Any]:
        """Like :meth:`walk` but keyed by the trailing table index."""
        out: dict[str, Any] = {}
        for vb in self.walk(base_oid, max_rows=max_rows, use_bulk=use_bulk):
            index = vb.oid[len(base_oid.rstrip(".")) + 1 :]
            if index:
                out[index] = vb.value
        return out

    def walk_many(
        self,
        base_oids: Iterable[str],
        max_rows: int = 100_000,
        max_repetitions: int | None = None,
    ) -> dict[str, list[VarBind]]:
        """Walk several table columns in one GETBULK stream.

        GPON optical data lives in one table row per ONU and several columns.
        Asking for all columns in the same GETBULK turns 5 separate 500-row
        walks into one interleaved pass, which is what makes a full VSOL
        V1600G1B poll fit in a sensible amount of time.
        """
        bases = [b.rstrip(".") for b in base_oids if b]
        out: dict[str, list[VarBind]] = {base: [] for base in bases}
        if not bases:
            return out
        use_bulk = self.version_code == 1
        if not use_bulk or len(bases) == 1:
            for base in bases:
                out[base] = self.walk(base, max_rows=max_rows)
            return out

        reps = max(1, int(max_repetitions or min(self.max_repetitions, 10)))
        current = list(bases)
        active = list(bases)
        while active:
            current_by_base = dict(zip(active, current))
            try:
                response = self._transact(
                    PDU_GETBULK, current, bulk=True, max_repetitions=reps
                )
            except SnmpError:
                # fall back to separate GETNEXT walks, but only for columns
                # that still have no data
                for base in list(active):
                    if not out[base]:
                        out[base] = self.walk(base, max_rows=max_rows, use_bulk=False)
                break
            if response.status != 0 or not response.varbinds:
                break

            progressed: dict[str, bool] = {base: False for base in active}
            next_current: dict[str, str] = {}
            for vb in response.varbinds:
                if vb.is_exception:
                    continue
                owner = None
                for base in bases:
                    if self._in_subtree(vb.oid, base):
                        owner = base
                        break
                if owner is None or owner not in progressed:
                    continue
                if len(out[owner]) >= max_rows:
                    continue
                out[owner].append(vb)
                next_current[owner] = vb.oid
                progressed[owner] = True

            new_active: list[str] = []
            for base in active:
                # No value in a full response means this column is exhausted.
                # A value that does not advance the cursor would loop forever.
                if progressed[base] and next_current.get(base) != current_by_base.get(base):
                    if len(out[base]) < max_rows:
                        new_active.append(base)
            if not new_active:
                break
            active = new_active
            current = [next_current.get(base, base) for base in active]
            reps = max(1, min(reps, 10))
        return out

    @staticmethod
    def _in_subtree(oid: str, base: str) -> bool:
        return oid == base or oid.startswith(base + ".")
