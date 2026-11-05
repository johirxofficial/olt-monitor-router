"""MikroTik RouterOS API protocol, implemented on top of ``socket`` only.

The RouterOS API is a very small protocol:

* a *sentence* is a list of *words*, each length-prefixed;
* a zero-length word terminates the sentence;
* replies start with ``!re`` (a row), ``!done``, ``!trap`` or ``!fatal``;
* attribute words look like ``=name=value``.

Login is handled for both generations of RouterOS:

* new (6.43+): send ``/login`` with ``=name=`` and ``=password=``;
* old: the agent answers ``!done`` with ``=ret=<challenge>`` and you reply with
  ``=response=00<md5(0x00 + password + challenge)>``.
"""

from __future__ import annotations

import hashlib
import re
import socket
import ssl
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_PORT = 8728
DEFAULT_PORT_TLS = 8729
DEFAULT_TIMEOUT = 10.0

_UPTIME_TOKEN = re.compile(r"(\d+)([wdhms])", re.IGNORECASE)
_UPTIME_CLOCK = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d+))?$")
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


class RouterOSError(RuntimeError):
    """Any RouterOS API failure (connection, protocol or command error)."""


class RouterOSAuthError(RouterOSError):
    """Bad username/password."""


def parse_uptime(text: Any) -> int | None:
    """Convert a RouterOS uptime string (``1w2d3h4m5s`` or ``03:04:05``) to seconds."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    value = str(text).strip()
    if not value:
        return None

    total = 0
    found = False
    for amount, unit in _UPTIME_TOKEN.findall(value):
        found = True
        factor = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}[unit.lower()]
        total += int(amount) * factor
    if found:
        return total

    clock = _UPTIME_CLOCK.match(value)
    if clock:
        hours = int(clock.group(1) or 0)
        minutes = int(clock.group(2))
        seconds = int(clock.group(3))
        return hours * 3600 + minutes * 60 + seconds
    return None


def parse_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def parse_float(value: Any, default: float | None = None) -> float | None:
    """Extract the first number from a value like ``-13.433`` or ``-13.4dBm``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER.search(str(value))
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def parse_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "yes", "1"):
        return True
    if text in ("false", "no", "0"):
        return False
    return None


@dataclass
class Reply:
    """One complete RouterOS reply (all ``!re`` rows plus the terminator)."""

    rows: list[dict[str, str]] = field(default_factory=list)
    done: dict[str, str] = field(default_factory=dict)
    trap: dict[str, str] | None = None
    fatal: str | None = None

    @property
    def ok(self) -> bool:
        return self.fatal is None and self.trap is None

    def raise_for_status(self) -> None:
        if self.fatal:
            raise RouterOSError(f"fatal: {self.fatal}")
        if self.trap:
            raise RouterOSError(
                "trap: " + (self.trap.get("message") or str(self.trap))
            )


def _encode_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    if length < 0x4000:
        return bytes([0x80 | (length >> 8), length & 0xFF])
    if length < 0x200000:
        return bytes([0xC0 | (length >> 16), (length >> 8) & 0xFF, length & 0xFF])
    if length < 0x10000000:
        return bytes(
            [0xE0 | (length >> 24), (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF]
        )
    return bytes([0xF0]) + length.to_bytes(4, "big")


def _decode_length(first: int, read: Any) -> int:
    if first < 0x80:
        return first
    if first < 0xC0:
        return ((first & 0x3F) << 8) | read(1)[0]
    if first < 0xE0:
        chunk = read(2)
        return ((first & 0x1F) << 16) | (chunk[0] << 8) | chunk[1]
    if first < 0xF0:
        chunk = read(3)
        return ((first & 0x0F) << 24) | (chunk[0] << 16) | (chunk[1] << 8) | chunk[2]
    chunk = read(4)
    return int.from_bytes(chunk, "big")


class RouterOSApi:
    """Small synchronous RouterOS API client.

    Example
    -------
    >>> with RouterOSApi("198.51.100.1", "admin", "secret") as api:
    ...     for session in api.cmd("/ppp/active/print"):
    ...         print(session["name"], session["uptime"])
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int | None = None,
        use_tls: bool = False,
        verify_tls: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.use_tls = bool(use_tls)
        self.verify_tls = bool(verify_tls)
        self.port = int(port or (DEFAULT_PORT_TLS if self.use_tls else DEFAULT_PORT))
        self.timeout = float(timeout)
        self._sock: socket.socket | None = None
        self.logged_in = False

    # -- connection ------------------------------------------------------
    def connect(self) -> "RouterOSApi":
        if self._sock is not None:
            return self
        try:
            raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise RouterOSError(
                f"cannot connect to RouterOS {self.host}:{self.port} "
                f"(is the 'api' service enabled and reachable?) - {exc}"
            ) from exc

        raw.settimeout(self.timeout)
        if self.use_tls:
            context = ssl.create_default_context()
            if not self.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            try:
                raw = context.wrap_socket(raw, server_hostname=self.host)
            except ssl.SSLError as exc:
                raw.close()
                raise RouterOSError(f"TLS handshake failed: {exc}") from exc

        self._sock = raw
        self._login()
        return self

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self.logged_in = False

    def __enter__(self) -> "RouterOSApi":
        return self.connect()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- low level io ----------------------------------------------------
    def _recv_exact(self, count: int) -> bytes:
        sock = self._sock
        if sock is None:
            raise RouterOSError("not connected")
        chunks = bytearray()
        while len(chunks) < count:
            try:
                chunk = sock.recv(count - len(chunks))
            except socket.timeout as exc:
                raise RouterOSError("timed out waiting for the router") from exc
            except OSError as exc:
                raise RouterOSError(f"connection lost: {exc}") from exc
            if not chunk:
                raise RouterOSError("connection closed by the router")
            chunks.extend(chunk)
        return bytes(chunks)

    def _read_word(self) -> bytes:
        first = self._recv_exact(1)[0]
        length = _decode_length(first, self._recv_exact)
        return self._recv_exact(length) if length else b""

    def _read_sentence(self) -> list[str]:
        words: list[str] = []
        while True:
            word = self._read_word()
            if not word:
                break
            words.append(word.decode("utf-8", errors="replace"))
        return words

    def _send_sentence(self, words: Iterable[str]) -> None:
        sock = self._sock
        if sock is None:
            raise RouterOSError("not connected")
        payload = bytearray()
        for word in words:
            raw = word.encode("utf-8")
            payload += _encode_length(len(raw)) + raw
        payload.append(0)
        try:
            sock.sendall(bytes(payload))
        except OSError as exc:
            raise RouterOSError(f"failed to send command: {exc}") from exc

    @staticmethod
    def _parse_words(words: list[str]) -> tuple[str, dict[str, str]]:
        reply_type = words[0] if words else "!done"
        attrs: dict[str, str] = {}
        for word in words[1:]:
            if word.startswith("="):
                body = word[1:]
                key, _, value = body.partition("=")
                if key:
                    attrs[key] = value
        return reply_type, attrs

    def talk(self, *words: str) -> Reply:
        """Send one command sentence and read until it is terminated."""
        self._send_sentence(words)
        reply = Reply()
        while True:
            sentence = self._read_sentence()
            if not sentence:
                continue
            reply_type, attrs = self._parse_words(sentence)
            if reply_type == "!re":
                reply.rows.append(attrs)
            elif reply_type == "!done":
                reply.done = attrs
                return reply
            elif reply_type == "!trap":
                reply.trap = attrs
                return reply
            elif reply_type == "!fatal":
                reply.fatal = attrs.get("message", "unknown fatal error")
                self.close()
                return reply
            # unknown reply type: ignore and keep reading

    # -- login -----------------------------------------------------------
    def _login(self) -> None:
        reply = self.talk("/login", f"=name={self.username}", f"=password={self.password}")

        if reply.fatal:
            raise RouterOSError(f"fatal during login: {reply.fatal}")

        if reply.trap:
            raise RouterOSAuthError(
                "login rejected - check username/password and that the user has "
                "the 'api' policy "
                f"({reply.trap.get('message') or reply.trap})"
            )

        challenge = reply.done.get("ret")
        if challenge:
            # RouterOS < 6.43 challenge/response login
            try:
                raw_challenge = bytes.fromhex(challenge)
            except ValueError as exc:
                raise RouterOSError(f"malformed login challenge: {challenge!r}") from exc
            digest = hashlib.md5(
                b"\x00" + self.password.encode("utf-8") + raw_challenge
            ).hexdigest()
            second = self.talk(
                "/login", f"=name={self.username}", f"=response=00{digest}"
            )
            if second.fatal:
                raise RouterOSError(f"fatal during login: {second.fatal}")
            if second.trap or second.done.get("ret"):
                raise RouterOSAuthError("login failed - wrong username or password")

        self.logged_in = True

    def ensure_connected(self) -> None:
        if self._sock is None or not self.logged_in:
            self.close()
            self.connect()

    # -- convenience -----------------------------------------------------
    def cmd(self, command: str, **params: Any) -> list[dict[str, str]]:
        """Run *command* and return the ``!re`` rows.

        Keyword arguments become attribute words: ``once=True`` -> ``=once=``,
        ``interface="ether1"`` -> ``=interface=ether1``.  ``False``/``None``
        values are skipped, and underscores become dashes.
        """
        words = [command]
        for key, value in params.items():
            if value is False or value is None:
                continue
            name = key.replace("_", "-")
            if value is True:
                words.append(f"={name}=")
            else:
                words.append(f"={name}={value}")
        reply = self.talk(*words)
        reply.raise_for_status()
        return reply.rows

    def one(self, command: str, **params: Any) -> dict[str, str]:
        rows = self.cmd(command, **params)
        return rows[0] if rows else {}

    # -- RouterOS specific helpers --------------------------------------
    def identity(self) -> dict[str, str]:
        return self.one("/system/identity/print")

    def resource(self) -> dict[str, str]:
        return self.one("/system/resource/print")

    def routerboard(self) -> dict[str, str]:
        try:
            return self.one("/system/routerboard/print")
        except RouterOSError:
            return {}

    def ppp_active(self) -> list[dict[str, str]]:
        """Active PPPoE/PPTP/L2TP sessions - one row per online user.

        The ``bytes-in`` / ``bytes-out`` counters are only reported in *detail*
        mode (or via ``.proplist``), so those are requested first and the plain
        listing is used as a fallback when the device rejects them.
        """
        attempts: list[dict[str, Any]] = [
            {".proplist": "name,service,caller-id,address,uptime,session-id,bytes-in,bytes-out"},
            {"detail": True},
            {},
        ]
        best: list[dict[str, str]] = []
        for params in attempts:
            try:
                rows = self.cmd("/ppp/active/print", **params)
            except RouterOSError:
                continue
            if not rows:
                continue
            if any("bytes-in" in row or "bytes-out" in row for row in rows):
                return rows
            if not best:
                best = rows
        return best

    def ppp_secrets(self) -> list[dict[str, str]]:
        try:
            return self.cmd("/ppp/secret/print")
        except RouterOSError:
            return []

    def interfaces(self) -> list[dict[str, str]]:
        return self.cmd("/interface/print")

    def vlans(self) -> list[dict[str, str]]:
        """VLAN interfaces with their ``vlan-id`` (name -> id mapping)."""
        try:
            return self.cmd("/interface/vlan/print")
        except RouterOSError:
            return []

    def pppoe_servers(self) -> list[dict[str, str]]:
        """PPPoE servers and the interface each one is bound to."""
        try:
            return self.cmd("/interface/pppoe-server/server/print")
        except RouterOSError:
            return []

    def monitor_traffic(self, name: str) -> dict[str, str]:
        """Live rx/tx bits per second for one interface."""
        try:
            rows = self.cmd("/interface/monitor-traffic", interface=name, once=True)
        except RouterOSError:
            return {}
        return rows[0] if rows else {}

    def ethernet_monitor(self, name: str) -> dict[str, str]:
        """Ethernet/SFP diagnostics (rx/tx optical power, temperature, ...)."""
        try:
            rows = self.cmd("/interface/ethernet/monitor", numbers=name, once=True)
        except RouterOSError:
            return {}
        return rows[0] if rows else {}

    def ethernet_monitor_many(self, names: list[str]) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        if not names:
            return out
        try:
            rows = self.cmd(
                "/interface/ethernet/monitor", numbers=",".join(names), once=True
            )
        except RouterOSError:
            return out
        for row in rows:
            key = row.get("name") or row.get("number")
            if key:
                out[key] = row
        return out

    def dhcp_leases(self) -> list[dict[str, str]]:
        try:
            return self.cmd("/ip/dhcp-server/lease/print")
        except RouterOSError:
            return []

    def arp(self) -> list[dict[str, str]]:
        try:
            return self.cmd("/ip/arp/print")
        except RouterOSError:
            return []
