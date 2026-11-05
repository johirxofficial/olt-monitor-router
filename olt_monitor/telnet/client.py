"""Minimal, read-only Telnet client for OLT CLI telemetry.

Many OLTs expose CPU load, RAM, board temperature, SFP diagnostics and the ONU
"last down cause" only over their CLI - not over SNMP.  This module logs in,
runs a short list of **show** commands and returns the raw text; parsing lives in
:mod:`olt_monitor.telnet.parsers`.

Safety is deliberate:

* only commands starting with ``show`` / ``display`` are ever sent (plus the
  mode switch needed by firmware that keeps ``show ont ...`` in config mode);
* a denied-word list rejects anything that could change the device
  (``write``, ``save``, ``reboot``, ``reset``, ``delete``, ``clear`` ...);
* paging is handled by answering ``--More--`` with a space instead of changing
  the terminal length;
* every socket operation has a deadline, so a hung CLI cannot block the poller.

Pure standard library: no ``telnetlib`` (removed in Python 3.13), no paramiko.
"""

from __future__ import annotations

import re
import socket
import time
from typing import Any

# ANSI colours / cursor codes and Telnet IAC option negotiation
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_IAC = re.compile(rb"\xff[\xfb-\xfe].|\xff")
_PAGING = re.compile(r"-{0,4}\s*More\s*-{0,4}|Press any key|--More--", re.IGNORECASE)
_PROMPT = re.compile(r"[>#]\s*$")
_LOGIN = re.compile(r"(?:login|user\s*name|username)\s*:\s*$", re.IGNORECASE)
_PASSWORD = re.compile(r"password\s*:\s*$", re.IGNORECASE)

# anything that could modify the device is refused outright
_FORBIDDEN = re.compile(
    r"\b(reboot|reload|reset|erase|delete|remove|write|save|copy|format|upgrade|"
    r"download|clear|undo|no|set|create|add|modify|apply|commit|restore|backup)\b",
    re.IGNORECASE,
)
_READ_ONLY = re.compile(r"^(?:show|display)\b", re.IGNORECASE)


class TelnetError(RuntimeError):
    """Connection, login or command failure (message is user-visible)."""


def is_read_only(command: str) -> bool:
    """True only for a bare, read-only show/display command."""
    text = str(command or "").strip()
    if not text or "\n" in text or "\r" in text or ";" in text or "|" in text:
        return False
    if not _READ_ONLY.match(text):
        return False
    return _FORBIDDEN.search(text) is None


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI.sub("", text)
    return "".join(ch for ch in text if ch == "\n" or ch == "\t" or 32 <= ord(ch) < 127 or ord(ch) > 160)


class TelnetSession:
    """One short-lived CLI session.  Use as a context manager."""

    def __init__(
        self,
        host: str,
        port: int = 23,
        username: str = "",
        password: str = "",
        enable_password: str = "",
        timeout: float = 8.0,
        *,
        enter_enable: bool = True,
        newline: str = "\r\n",
    ) -> None:
        self.host = str(host or "").strip()
        self.port = int(port or 23)
        self.username = str(username or "")
        self.password = str(password or "")
        self.enable_password = str(enable_password or "")
        self.timeout = max(2.0, float(timeout or 8.0))
        self.enter_enable = enter_enable
        self.newline = newline
        self._sock: socket.socket | None = None

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> "TelnetSession":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def connect(self) -> None:
        if not self.host:
            raise TelnetError("Telnet host is not configured")
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise TelnetError(f"cannot connect to {self.host}:{self.port} - {exc}") from exc
        self._sock.settimeout(1.0)
        try:
            self._login()
        except TelnetError:
            self.close()
            raise

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.sendall(f"exit{self.newline}".encode())
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # -- io --------------------------------------------------------------
    def _send(self, text: str) -> None:
        if self._sock is None:
            raise TelnetError("not connected")
        try:
            self._sock.sendall(text.encode("utf-8", errors="ignore") + self.newline.encode())
        except OSError as exc:
            raise TelnetError(f"send failed: {exc}") from exc

    def _recv(self, deadline: float, quiet: float = 0.45, stop: re.Pattern[str] | None = None) -> str:
        """Read until *stop* matches the tail, the line goes quiet, or *deadline*."""
        if self._sock is None:
            raise TelnetError("not connected")
        buffer = ""
        last = time.monotonic()
        while time.monotonic() < deadline:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                if stop is not None and stop.search(buffer):
                    break
                if buffer and time.monotonic() - last >= quiet:
                    break
                continue
            except OSError as exc:
                raise TelnetError(f"connection lost: {exc}") from exc
            if not chunk:
                break
            last = time.monotonic()
            buffer += _IAC.sub(b"", chunk).decode("utf-8", errors="ignore")
            buffer = _ANSI.sub("", buffer)
            # answer paging without changing the device's terminal settings
            if _PAGING.search(buffer[-120:]):
                buffer = _PAGING.sub("", buffer)
                try:
                    self._sock.sendall(b" ")
                except OSError:
                    break
            if stop is not None and stop.search(buffer):
                break
        return buffer

    # -- login -----------------------------------------------------------
    def _login(self) -> None:
        deadline = time.monotonic() + self.timeout
        banner = self._recv(deadline, quiet=0.8)
        text = banner.strip()

        # C-Data FD13xx and some other EPON firmware print only the banner and
        # then wait for a newline before showing "User name:".  Without this
        # nudge the login times out with "no CLI prompt appeared" even though
        # Telnet is perfectly usable.
        if not (_LOGIN.search(text) or _PASSWORD.search(text) or _PROMPT.search(text)):
            self._send("")
            text = (text + self._recv(deadline, quiet=0.8)).strip()

        if _LOGIN.search(text):
            if not self.username:
                raise TelnetError("the CLI asked for a username but none is configured")
            self._send(self.username)
            text = self._recv(deadline, quiet=0.8).strip()

        if _PASSWORD.search(text):
            self._send(self.password)
            text = self._recv(deadline, quiet=0.8).strip()

        if _LOGIN.search(text) or _PASSWORD.search(text):
            raise TelnetError("login rejected - check the Telnet username / password")

        if not _PROMPT.search(text):
            # some firmware prints a menu after login; one more read usually lands
            # on the prompt
            text = (text + self._recv(deadline, quiet=0.6)).strip()
        if not _PROMPT.search(text):
            # a few boxes only redraw the prompt after a newline (C-Data)
            self._send("")
            text = (text + self._recv(deadline, quiet=0.6)).strip()
        if not _PROMPT.search(text):
            raise TelnetError(
                "connected but no CLI prompt appeared (is this really a Telnet CLI?)"
            )

        if self.enter_enable and text.rstrip().endswith(">"):
            # Most EPON/GPON CLIs keep ``show`` in enable mode.  Some (C-Data)
            # ask for no enable password at all, so always try the mode switch
            # and only prompt for a password if the CLI asks for one.
            self._send("enable")
            reply = self._recv(deadline, quiet=0.6)
            if _PASSWORD.search(reply.strip()):
                if not self.enable_password:
                    raise TelnetError(
                        "the CLI asked for an enable password but none is configured"
                    )
                self._send(self.enable_password)
                reply = self._recv(deadline, quiet=0.6)
            if ">" in reply.strip().splitlines()[-1:] and "#" not in reply:
                raise TelnetError("enable failed - check the enable password")

    # -- commands --------------------------------------------------------
    def run(self, command: str, *, mode: str = "exec", timeout: float | None = None) -> str:
        """Run one read-only command and return its output (prompt stripped)."""
        if not is_read_only(command):
            raise TelnetError(f"refusing to run a non-read-only command: {command!r}")
        budget = time.monotonic() + (timeout or max(10.0, self.timeout * 2))

        if str(mode or "exec").lower() == "config":
            self._send("config")
            self._recv(budget, quiet=0.4, stop=_PROMPT)

        self._send(command)
        raw = self._recv(budget, quiet=0.7, stop=_PROMPT)
        if str(mode or "exec").lower() == "config":
            self._send("exit")
            self._recv(time.monotonic() + 5.0, quiet=0.3, stop=_PROMPT)

        return _strip_echo(raw, command)

    def run_many(self, commands: list[tuple[str, str]], timeout: float | None = None) -> dict[str, str]:
        """Run ``[(key, command), ...]``; failures are reported per key."""
        out: dict[str, str] = {}
        for key, command in commands:
            try:
                out[key] = self.run(command, timeout=timeout)
            except TelnetError as exc:
                out[key] = f"__ERROR__ {exc}"
        return out


def _strip_echo(raw: str, command: str) -> str:
    """Remove the echoed command line and the trailing prompt from the output."""
    text = _clean(raw).strip("\n")
    lines = text.split("\n")
    if lines and lines[0].strip().lstrip("\x00").strip() == command.strip():
        lines = lines[1:]
    if lines:
        tail = lines[-1].strip()
        if tail.endswith(">") or tail.endswith("#"):
            lines = lines[:-1]
    # drop the trailing blank lines the CLI leaves before the prompt
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def probe(
    host: str,
    port: int = 23,
    username: str = "",
    password: str = "",
    enable_password: str = "",
    timeout: float = 8.0,
) -> tuple[bool, str, dict[str, Any]]:
    """Login test for the *Test Telnet* button: one harmless command."""
    started = time.monotonic()
    try:
        with TelnetSession(
            host, port, username, password, enable_password, timeout
        ) as session:
            output = session.run("show version", timeout=timeout)
    except TelnetError as exc:
        return False, str(exc), {}
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"Telnet failed: {exc}", {}

    first = next((line.strip() for line in output.splitlines() if line.strip()), "")
    details = {
        "port": int(port or 23),
        "username": username or "-",
        "response_ms": int((time.monotonic() - started) * 1000),
        "first_line": first[:160],
    }
    return True, first or "logged in", details
