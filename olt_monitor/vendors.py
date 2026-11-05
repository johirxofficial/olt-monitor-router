"""Vendor profiles: how to talk to each OLT brand.

An OLT is not just "an SNMP device": interface names, the ONU identifier
(MAC versus GPON serial), the optical OIDs and the CLI commands all differ per
vendor and firmware family.  A :class:`VendorProfile` collects those differences
in one place so the collector stays generic.

Two axes are handled independently:

* **brand** - ``vsol``, ``bdcom``, ``cdata``, ``ecom``, ``litex``, ``avies``,
  ``dbc``, ``bdpon``, ``btpon``, ``photon``, ``gpon_soho`` (and ``generic``
  when nothing is known).  It only changes cosmetics and the optional vendor
  OIDs; everything else degrades gracefully.
* **PON architecture** - ``epon`` (1.25 G, ONU identified by MAC) or ``gpon``
  (2.5 G, ONU identified by serial number).  This drives the interface-name
  patterns and how a subscriber is linked to an ONU.

``verified`` says whether the profile was tested against real hardware.  Only
VSOL (the OLT of this network) is verified; the others are best-effort profiles
built from the vendors' documented CLI/SMI and can be adjusted from the web UI
(brand / PON type / Telnet commands) without touching code.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# ---------------------------------------------------------------------------
#  interface classification
# ---------------------------------------------------------------------------
# Named groups (``pon`` / ``onu``) are what the collector reads, so a profile can
# express "EPON01ONU8" and "EPON0/1:8" with the same code.
EPON_CLASSIFY: dict[str, list[str]] = {
    "onu_regex": [
        r"^EPON(?P<pon>\d*)ONU(?P<onu>\d+)$",
        r"^EPON\d*/(?P<pon>\d+):(?P<onu>\d+)$",
        # C-Data / "EPON OLT Integrated Operating System" prints the whole
        # frame/slot/port path, e.g. "epon 0/1/1:7"
        r"^epon\s+(?P<pon>\d+/\d+/\d+):(?P<onu>\d+)$",
        # BDPON / BTPON / PHOTON firmware prints flat names ("pon1:7")
        r"^pon\s*(?P<pon>\d+)\s*[:/]\s*(?P<onu>\d+)$",
        # AVIES AV-OLT-XE08: "ONU01/01" = PON 1, ONU 1 (no EPON prefix)
        r"^ONU(?P<pon>\d+)/(?P<onu>\d+)$",
    ],
    "pon_regex": [
        r"^EPON\d*/\d+$",
        r"^epon\s+\d+/\d+/\d+$",
        # BDPON / BTPON / PHOTON: "pon1", "PON 8"
        r"^pon\s*\d+$",
        # AVIES: "PON01", "PON01:", "PON02:Area-B",
        r"^PON\s*\d+\s*(?::.*)?$",
    ],
    "uplink_regex": [
        r"^(?:GE|XGE|TE|TG|10GE)\d*/\d+$",
        r"^(?:GigaEthernet|T-GigabitEthernet|TGigaEthernet|TenGigabitEthernet|FastEthernet|Ethernet)\d*/\d+$",
        # C-Data style: lowercase, space separated, three levels
        r"^(?:ge|xge|te|tg|10ge)\s+\d+/\d+/\d+$",
        # BDPON / BTPON / PHOTON: flat "ge1", "xge2"
        r"^(?:ge|xge|te|tg|10ge)\s*\d+$",
        # AVIES: "GE01", "GE01/Uplink", "XGE01" (upper case, optional suffix)
        r"^(?:GE|XGE|TE|TG|10GE)\d+(?:/[A-Za-z0-9_.-]+)?$",
    ],
    "vlan_regex": [r"^(?:vlan|VLAN)\d+$"],
}

GPON_CLASSIFY: dict[str, list[str]] = {
    "onu_regex": [
        r"^(?:GPON|gpon-onu_)\d*/(?P<pon>\d+)(?:/\d+)*:(?P<onu>\d+)$",
        r"^GPON(?P<pon>\d*)ONU(?P<onu>\d+)$",
    ],
    "pon_regex": [r"^(?:GPON|gpon-olt_)\d*/\d+(?:/\d+)?$"],
    "uplink_regex": [
        r"^(?:GE|XGE|TE|TG|10GE)\d*/\d+$",
        r"^(?:GigaEthernet|T-GigabitEthernet|TGigaEthernet|TenGigabitEthernet|FastEthernet|Ethernet)\d*/\d+$",
    ],
    "vlan_regex": [r"^(?:vlan|VLAN)\d+$"],
}

# A single uplink regex used when the brand is unknown: it matches every common
# way an OLT prints a copper/fibre uplink but never an EPON/GPON port.
GENERIC_CLASSIFY: dict[str, list[str]] = {
    "onu_regex": EPON_CLASSIFY["onu_regex"] + GPON_CLASSIFY["onu_regex"],
    "pon_regex": EPON_CLASSIFY["pon_regex"] + GPON_CLASSIFY["pon_regex"],
    "uplink_regex": EPON_CLASSIFY["uplink_regex"],
    "vlan_regex": EPON_CLASSIFY["vlan_regex"],
}


# ---------------------------------------------------------------------------
#  Telnet / SSH CLI profiles
# ---------------------------------------------------------------------------
# Every command is a *read-only* show command.  ``mode`` is the CLI mode it must
# be executed in: some firmware only accepts ``show ont ...`` from config mode.
# ``{pon}`` / ``{onu}`` are substituted before the command is sent - never by the
# user directly.
TELNET_PROFILES: dict[str, dict[str, Any]] = {
    # VSOL V1600D, ECOM and most EPON CLI gear (the command set this network uses)
    "epon_vsol": {
        "label": "EPON (VSOL / ECOM style)",
        "notes": (
            "Login and prompt verified on a live V1600D (banner 'epon olt platform', "
            "prompt 'Hostname>' / 'Hostname#').  The account must reach privileged "
            "mode: the factory 'support' user only has show channel-group summary / "
            "show history / show ip rip / show monitor session."
        ),
        "commands": {
            "cpu": {"cmd": "show cpu", "mode": "exec"},
            "memory": {"cmd": "show memory", "mode": "exec"},
            "temperature": {"cmd": "show temperature", "mode": "exec"},
            "ports": {"cmd": "show epon optical-transceiver-diagnosis", "mode": "exec"},
            "active_onu": {"cmd": "show epon active-onu", "mode": "exec"},
            "inactive_onu": {"cmd": "show epon inactive-onu", "mode": "exec"},
            "onu_optical": {
                "cmd": "show epon onu-ctc-optical-transceiver-diagnosis interface EPON0/{pon}",
                "mode": "exec",
            },
            "onu_info": {
                "cmd": "show epon onu-information interface EPON0/{pon}",
                "mode": "exec",
            },
        },
    },
    # BDCOM EPON (P3310 / P3608 / P3612 family) - verified on a live P3608B
    # running 10.1.0F.  Every command below is read-only output.
    "bdcom_epon": {
        "label": "BDCOM EPON",
        "notes": (
            "Verified on a live P3608B 10.1.0F (support account).  The EPON "
            "tables wrap at 80 columns (a row is split over two lines), which "
            "the BDCOM parsers handle.  `show memory` is not supported on this "
            "firmware, so RAM is left empty.  Check the uplink interface names "
            "(gigaethernet0/7, tgigaethernet0/1) match your chassis."
        ),
        "commands": {
            "cpu": {"cmd": "show cpu", "mode": "exec"},
            "temperature": {"cmd": "show temperature", "mode": "exec"},
            "ports": {"cmd": "show epon optical-transceiver-diagnosis", "mode": "exec"},
            "uplinks": {
                "cmd": [
                    "show interface gigaethernet0/7",
                    "show interface tgigaethernet0/1",
                ],
                "mode": "exec",
            },
            "active_onu": {"cmd": "show epon active-onu", "mode": "exec"},
            "inactive_onu": {"cmd": "show epon inactive-onu", "mode": "exec"},
            "onu_optical": {
                "cmd": "show epon onu-ctc-optical-transceiver-diagnosis interface EPON0/{pon}",
                "mode": "exec",
            },
            "onu_info": {
                "cmd": "show epon onu-information interface EPON0/{pon}",
                "mode": "exec",
            },
            "macs": {"cmd": "show mac address-table", "mode": "exec"},
        },
    },
    # ECOM / LITEX EPON and the C-Data "OLT Integrated Operating System"
    # family.  Their show commands live in config mode and are addressed by
    # the *PON number* (``show ont optical-info 1 all``).  The per-ONU
    # inventory is ``show ont info 0/<pon> <onu> all``.
    "ecom_epon": {
        "label": "EPON (ECOM / LITEX / C-Data style)",
        "notes": (
            "Read-only command set used by ECOM, LITEX and the C-Data EPON "
            "firmware: every command is issued in config mode, optical data is "
            "per PON and the ONU inventory is per ONU."
        ),
        "commands": {
            "cpu": {"cmd": "show cpu", "mode": "exec"},
            "memory": {"cmd": "show memory", "mode": "exec"},
            "temperature": {"cmd": "show temperature", "mode": "exec"},
            "ports": {"cmd": "show ont optical-info {pon} all", "mode": "config"},
            "onu_optical": {"cmd": "show ont optical-info {pon} all", "mode": "config"},
            "onu_info": {"cmd": "show ont info 0/{pon} all", "mode": "config"},
            "onu_detail": {"cmd": "show ont info 0/{pon} {onu} all", "mode": "config"},
            "macs": {"cmd": "show mac address-table", "mode": "exec"},
        },
    },
    "cdata_epon": {
        "label": "EPON (C-Data FD11xx / FD13xx)",
        "notes": (
            "Read-only command set verified on an FD1304E-B1 (V3.4.57). "
            "C-Data keeps the ONT table in config mode, puts the forwarding "
            "table behind 'show mac all', and needs an empty enable step "
            "(no enable password).  The HTTP API is still the preferred source "
            "for per-ONU optical power and last-down cause."
        ),
        "commands": {
            "cpu": {"cmd": "show cpu", "mode": "exec"},
            "memory": {"cmd": "show memory", "mode": "exec"},
            "temperature": {"cmd": "show temperature", "mode": "exec"},
            "identity": {"cmd": "show version", "mode": "exec"},
            "macs": {"cmd": "show mac all", "mode": "exec"},
            "onu_table": {"cmd": "show ont info all", "mode": "config"},
        },
    },
    # GPON SOHO gear: BDCOM / C-Data / BTPON (the "show ont ..." family)
    "gpon_generic": {
        "label": "GPON (BDCOM / C-Data / BTPON style)",
        "commands": {
            "cpu": {"cmd": "show cpu", "mode": "exec"},
            "memory": {"cmd": "show memory", "mode": "exec"},
            "temperature": {"cmd": "show temperature", "mode": "exec"},
            "onu_optical": {"cmd": "show ont optical-info {pon} all", "mode": "config"},
            "onu_info": {"cmd": "show ont info 0/{pon} all", "mode": "config"},
            "onu_detail": {"cmd": "show ont info 0/{pon} {onu} all", "mode": "config"},
        },
    },
    # GPON where the firmware keeps the show commands in exec mode
    "gpon_exec": {
        "label": "GPON (exec mode)",
        "commands": {
            "cpu": {"cmd": "show cpu", "mode": "exec"},
            "memory": {"cmd": "show memory", "mode": "exec"},
            "temperature": {"cmd": "show temperature", "mode": "exec"},
            "onu_optical": {"cmd": "show ont optical-info {pon} all", "mode": "exec"},
            "onu_info": {"cmd": "show ont info 0/{pon} all", "mode": "exec"},
            "onu_detail": {"cmd": "show ont info 0/{pon} {onu} all", "mode": "exec"},
        },
    },
    # BDCOM GPON (GP3600 family): the ONU tables are addressed as
    # ``GPON0/<pon>`` and the uplink SFP is read with ``show interface``.
    "bdcom_gpon": {
        "label": "BDCOM GPON",
        "notes": (
            "Read-only GP3600 command set.  Per-PON ONU inventory and inactive "
            "tables use ``GPON0/<pon>``; the forwarding table is the standard "
            "``show mac address-table``."
        ),
        "commands": {
            "cpu": {"cmd": "show cpu", "mode": "exec"},
            "temperature": {"cmd": "show temperature", "mode": "exec"},
            "ports": {"cmd": "show interface gpon0/{pon}", "mode": "exec"},
            "uplinks": {"cmd": "show interface tgigaethernet0/1", "mode": "exec"},
            "onu_info": {
                "cmd": "show gpon onu-information interface GPON0/{pon}",
                "mode": "exec",
            },
            "inactive_onu": {
                "cmd": "show gpon inactive-onu interface GPON0/{pon}",
                "mode": "exec",
            },
            "macs": {"cmd": "show mac address-table", "mode": "exec"},
        },
    },
    # VSOL V1600G GPON: the command set published for the V1600G family.
    # It is deliberately limited to the commands the firmware documents.
    "vsol_gpon": {
        "label": "VSOL GPON",
        "notes": (
            "V1600G read-only command set: CPU/RAM and PON/uplink SFP DDM from "
            "config mode, forwarding table with ``show mac address-table pon``. "
            "The vendor SNMP optical profile is still used for per-ONU values."
        ),
        "commands": {
            "cpu": {"cmd": "show sys cpu-usage", "mode": "config"},
            "memory": {"cmd": "show sys mem", "mode": "config"},
            "ports": {
                "cmd": [
                    "show pon optical transceiver all",
                    "show gigabitethernet optical transceiver all",
                ],
                "mode": "config",
            },
            "inactive_onu": {"cmd": "show onu time-stamp", "mode": "config"},
            "onu_optical": {"cmd": "show pon optical transceiver", "mode": "config"},
            "macs": {"cmd": "show mac address-table pon", "mode": "config"},
        },
    },
    # BDPON / BTPON / PHOTON EPON-OLT family.  These boxes keep the PON SFP
    # diagnostics behind a *per-PON* command, so ``ports`` is a template that
    # the collector expands for every PON it knows about.
    "epon_ponport": {
        "label": "EPON (BDPON / BTPON / PHOTON)",
        "notes": (
            "Read-only CLI used by the BDPON / BTPON / PHOTON EPON-OLT family: "
            "`show cpu`, `show memory`, `show pon-port ddm-info pon<N>` and the "
            "per-PON `show ont ... pon<N> all` inventory/optical tables. The PON "
            "DDM output is a key/value block, not a table; it is parsed as "
            "temperature / voltage / bias / TX / RX and attached to the matching "
            "PON port."
        ),
        "commands": {
            "cpu": {"cmd": "show cpu", "mode": "exec"},
            "memory": {"cmd": "show memory", "mode": "exec"},
            "ports": {"cmd": "show pon-port ddm-info pon{pon}", "mode": "exec"},
            "macs": {"cmd": "show mac-address-table", "mode": "exec"},
            "onu_info": {"cmd": "show ont info pon{pon} all", "mode": "exec"},
            "onu_optical": {"cmd": "show ont optical-info pon{pon} all", "mode": "exec"},
            "onu_basic": {"cmd": "show ont basic-info pon{pon} all", "mode": "exec"},
            "onu_version": {"cmd": "show ont version pon{pon} all", "mode": "exec"},
        },
    },
}


# ---------------------------------------------------------------------------
#  SNMP health OIDs that are truly vendor neutral (RFC 2790 HOST-RESOURCES-MIB)
# ---------------------------------------------------------------------------
STANDARD_HEALTH_OIDS: dict[str, str] = {
    "hr_system_uptime": "1.3.6.1.2.1.25.1.1.0",
    "hr_processor_load": "1.3.6.1.2.1.25.3.3.1.2",
    "hr_storage_descr": "1.3.6.1.2.1.25.2.3.1.3",
    "hr_storage_units": "1.3.6.1.2.1.25.2.3.1.4",
    "hr_storage_size": "1.3.6.1.2.1.25.2.3.1.5",
    "hr_storage_used": "1.3.6.1.2.1.25.2.3.1.6",
}


# ---------------------------------------------------------------------------
#  the profiles
# ---------------------------------------------------------------------------
PROFILES: dict[str, dict[str, Any]] = {
    "vsol": {
        "label": "VSOL",
        "verified": True,
        "pon_type": "epon",
        "onu_key": "mac",
        "hints": ("vsol", "v1600", "v1600d"),
        "classify": EPON_CLASSIFY,
        "telnet_profile": "epon_vsol",
        "default_max_onus": 64,
        "notes": "Verified against the live V1600D of this network.",
    },
    "bdcom": {
        "label": "BDCOM",
        "verified": True,
        "pon_type": "epon",
        "onu_key": "mac",
        "hints": ("bdcom",),
        "classify": EPON_CLASSIFY,
        "telnet_profile": "bdcom_epon",
        "default_max_onus": 64,
        "notes": (
            "Verified against a live P3608B (EPON, 10.1.0F). BDCOM also ships "
            "GPON models - pick GPON and the 'gpon_generic' profile for those."
        ),
    },
    "cdata": {
        "label": "C-Data",
        "verified": True,
        "pon_type": "gpon",
        "onu_key": "serial",
        "hints": (
            "c-data", "cdata", "c_data",
            "epon olt integrated operating system", "ac-fd", "fd1304", "fd1104", "fd1108",
        ),
        "classify": GPON_CLASSIFY,
        "telnet_profile": "gpon_generic",
        "default_max_onus": 128,
        "notes": (
            "Verified on a live FD1304E-B1 (EPON). These OLTs expose almost "
            "nothing over SNMP, so they are read through their HTTP API "
            "(cgi-bin/h.cgi) - see the 'OLT HTTP API' block on the Devices page. "
            "Pick PON type EPON for the FD11xx/FD13xx EPON models."
        ),
    },
    "ecom": {
        "label": "ECOM",
        "verified": False,
        "pon_type": "epon",
        "onu_key": "mac",
        "hints": ("ecom", "ecom olt"),
        "classify": EPON_CLASSIFY,
        "telnet_profile": "ecom_epon",
        "default_max_onus": 64,
        "notes": (
            "ECOM EPON uses the config-mode ``show ont optical-info <pon> all`` "
            "and ``show ont info 0/<pon> <onu> all`` command set."
        ),
    },
    "litex": {
        "label": "LITEX",
        "verified": False,
        "pon_type": "epon",
        "onu_key": "mac",
        "hints": ("litex", "li-tex", "lite-tech"),
        "classify": EPON_CLASSIFY,
        "telnet_profile": "ecom_epon",
        "default_max_onus": 64,
        "notes": (
            "LITEX EPON uses the same config-mode ``show ont ...`` family as "
            "ECOM/C-Data.  Select GPON for a LITEX GPON chassis; the CLI profile "
            "then switches to the ``show ont optical-info`` GPON set."
        ),
    },
    "avies": {
        "label": "AVIES",
        "verified": False,
        "pon_type": "epon",
        "onu_key": "mac",
        "hints": ("avies", "avies olt", "av-olt"),
        "classify": EPON_CLASSIFY,
        "telnet_profile": "epon_vsol",
        "default_max_onus": 64,
        "notes": (
            "AVIES EPON is handled with the verified VSOL-style EPON CLI set. "
            "If the firmware keeps the ONT tables in config mode, choose the "
            "'ecom_epon' profile on the Devices page."
        ),
    },
    "dbc": {
        "label": "DBC",
        "verified": False,
        "pon_type": "epon",
        "onu_key": "mac",
        "hints": ("dbc", "dbc olt", "dbc epon"),
        "classify": EPON_CLASSIFY,
        "telnet_profile": "bdcom_epon",
        "default_max_onus": 64,
        "notes": (
            "DBC EPON uses the BDCOM EPON command set (``show epon active-onu``, "
            "``show epon onu-information`` and the CTC optical table)."
        ),
    },
    "bdpon": {
        "label": "BD-PON",
        "verified": False,
        "pon_type": "epon",
        "onu_key": "mac",
        "hints": ("bdpon", "bd-pon", "bd_pon", "bd pon"),
        "classify": EPON_CLASSIFY,
        "telnet_profile": "epon_ponport",
        "default_max_onus": 64,
        "notes": (
            "EPON-OLT family (flat pon1/ge1 interface names).  CPU, RAM and "
            "PON SFP DDM are read through the read-only Telnet profile "
            "'epon_ponport'."
        ),
    },
    "btpon": {
        "label": "BTPON",
        "verified": False,
        "pon_type": "epon",
        "onu_key": "mac",
        "hints": ("btpon", "bt-pon", "bt_pon"),
        "classify": EPON_CLASSIFY,
        "telnet_profile": "epon_ponport",
        "default_max_onus": 64,
        "notes": (
            "BTPON EPON-OLT family.  CPU, RAM and PON SFP DDM use the "
            "read-only 'epon_ponport' Telnet profile."
        ),
    },
    "photon": {
        "label": "PHOTON",
        "verified": True,
        "pon_type": "epon",
        "onu_key": "mac",
        "hints": (
            "photon",
            "epon-olt series",
            "internetwork operating system software epon-olt",
        ),
        "classify": EPON_CLASSIFY,
        "telnet_profile": "epon_ponport",
        "default_max_onus": 64,
        "notes": (
            "Verified against a live 4PON 'EPON-OLT Series' box (1.1.3). "
            "Interfaces are pon1..ponN and ge1..geN; CPU, RAM and PON SFP DDM "
            "are read through the read-only 'epon_ponport' Telnet profile."
        ),
    },
    "gpon_soho": {
        "label": "GPON (SOHO)",
        "verified": False,
        "pon_type": "gpon",
        "onu_key": "serial",
        "hints": ("gpon",),
        "classify": GPON_CLASSIFY,
        "telnet_profile": "gpon_generic",
        "default_max_onus": 128,
        "notes": "Generic GPON profile for an unlisted SOHO OLT.",
    },
    "auto": {
        "label": "Auto-detect",
        "verified": False,
        "pon_type": "epon",
        "onu_key": "mac",
        "hints": (),
        "classify": GENERIC_CLASSIFY,
        "telnet_profile": "epon_vsol",
        "default_max_onus": 64,
        "notes": "The brand is guessed from the device at the next poll; set it explicitly if the guess is wrong.",
    },
    "generic": {
        "label": "Generic EPON",
        "verified": False,
        "pon_type": "epon",
        "onu_key": "mac",
        "hints": (),
        "classify": GENERIC_CLASSIFY,
        "telnet_profile": "epon_vsol",
        "default_max_onus": 64,
        "notes": "Unknown brand: EPON naming is assumed. Change it on the Devices page.",
    },
}

BRAND_KEYS: tuple[str, ...] = tuple(PROFILES)
PON_TYPES: tuple[str, ...] = ("epon", "gpon")

# What the PON type implies, independent of brand
PON_DEFAULTS: dict[str, dict[str, Any]] = {
    "epon": {"onu_key": "mac", "default_max_onus": 64, "speed": "1.25 G"},
    "gpon": {"onu_key": "serial", "default_max_onus": 128, "speed": "2.5 G"},
}


# ---------------------------------------------------------------------------
#  lookups
# ---------------------------------------------------------------------------
#: how the OLTs name a PON port, e.g. "EPON0/7" (VSOL) or "epon 0/1/1" (C-Data)
_PON_TAIL = re.compile(r"(\d+(?:/\d+)*)\s*$")

#: C-Data firmware prints lower-case, space separated, three-level port names
#: ("ge 0/0/1", "epon 0/1/1") - a fingerprint no other vendor in this file uses
_CDATA_IFNAME = re.compile(r"^(?:ge|xge|epon|gpon)\s+\d+/\d+/\d+$")

#: BDPON / BTPON / PHOTON print flat names such as "pon1" and "ge1"
_PHOTON_IFNAME = re.compile(r"^(?:pon|ge|xge)\d+$", re.IGNORECASE)


def pon_number(if_name: Any) -> int | None:
    """PON port name -> one small integer, unique per OLT.

    * ``EPON0/7``      -> ``7``   (VSOL, ECOM, BDCOM)
    * ``epon 0/1/1``   -> ``11``  (C-Data: frame/slot/port folded together)
    * ``epon 0/2/4``   -> ``24``

    A chassis OLT names its ports with three numbers, so taking only the last
    one would make ``epon 0/1/1`` and ``epon 0/2/1`` the same port.  The
    ``slot * 10 + port`` fold keeps them apart (and keeps the number small
    enough for a URL).
    """
    text = str(if_name or "").strip()
    if not text:
        return None
    # AVIES appends a description after a colon, with or without a space:
    # "PON01: Area-A", "PON02:Area-B".  Keep only the port token;
    # otherwise the trailing text (or colon) hides the digits from _PON_TAIL.
    avies = re.match(r"(?i)^(PON\s*\d+)\s*:.*$", text)
    if avies:
        text = avies.group(1)
    # Some VSOL / ECOM firmware appends a free-form description to the port
    # name ("GPON0/1 Area-A,Road-1,Market").  Only the first token is the
    # port identifier; without trimming, the trailing description made the
    # port unclassifiable and the PONs page invented "EPON0/1" rows for it.
    # C-Data names are space separated ("epon 0/1/1"), so only trim when the
    # first token itself carries the numbers.
    head = re.split(r"[\s,]", text, maxsplit=1)[0]
    if head and re.search(r"\d", head):
        text = head.rstrip(":")
    match = _PON_TAIL.search(text)
    if not match:
        return None
    parts = [int(part) for part in match.group(1).split("/") if part.isdigit()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return parts[1]
    return parts[-2] * 10 + parts[-1]


def profile(key: str | None) -> dict[str, Any]:
    """Profile by key (falls back to ``generic``)."""
    return PROFILES.get(str(key or "").strip().lower(), PROFILES["generic"])


def brand_label(key: str | None) -> str:
    return str(profile(key).get("label") or "Generic")


def pon_defaults(pon_type: str | None) -> dict[str, Any]:
    return PON_DEFAULTS.get(str(pon_type or "epon").lower(), PON_DEFAULTS["epon"])


def classify_for(brand: str | None, pon_type: str | None) -> dict[str, list[str]]:
    """Interface patterns for a brand + PON type combination."""
    base = profile(brand).get("classify") or GENERIC_CLASSIFY
    if str(pon_type or "").lower() == "gpon" and base is EPON_CLASSIFY:
        return GPON_CLASSIFY
    if str(pon_type or "").lower() == "epon" and base is GPON_CLASSIFY:
        return EPON_CLASSIFY
    return base


def telnet_profile(name: str | None) -> dict[str, Any]:
    return TELNET_PROFILES.get(str(name or ""), TELNET_PROFILES["epon_vsol"])


def telnet_profile_names() -> list[str]:
    return list(TELNET_PROFILES)


#: brand + PON type -> the CLI profile that actually works on that family.
#: The Devices page still allows an explicit override; this table is only the
#: default, so adding a new brand never requires editing the collector.
_TELNET_FOR: dict[tuple[str, str], str] = {
    ("vsol", "epon"): "epon_vsol",
    ("vsol", "gpon"): "vsol_gpon",
    ("bdcom", "epon"): "bdcom_epon",
    ("bdcom", "gpon"): "bdcom_gpon",
    ("dbc", "epon"): "bdcom_epon",
    ("dbc", "gpon"): "bdcom_gpon",
    ("cdata", "epon"): "cdata_epon",
    ("cdata", "gpon"): "gpon_generic",
    ("ecom", "epon"): "ecom_epon",
    ("ecom", "gpon"): "gpon_generic",
    ("litex", "epon"): "ecom_epon",
    ("litex", "gpon"): "gpon_generic",
    ("avies", "epon"): "epon_vsol",
    ("avies", "gpon"): "gpon_generic",
    ("bdpon", "epon"): "epon_ponport",
    ("bdpon", "gpon"): "gpon_generic",
    ("btpon", "epon"): "epon_ponport",
    ("btpon", "gpon"): "gpon_generic",
    ("photon", "epon"): "epon_ponport",
    ("photon", "gpon"): "gpon_generic",
    ("gpon_soho", "gpon"): "gpon_generic",
    ("gpon_soho", "epon"): "epon_vsol",
}

#: profiles that older versions wrote into the registry by default.  When the
#: brand now has a dedicated family profile these are upgraded automatically,
#: so an existing device starts using the right commands after the update.
_LEGACY_DEFAULT_PROFILES = {
    "epon_vsol", "gpon_generic", "gpon_exec", "epon_ponport", "bdcom_epon",
}


def telnet_profile_for(
    brand: str | None,
    pon_type: str | None,
    configured: str | None = None,
) -> str:
    """Pick the CLI profile for a brand/PON pair, honouring a deliberate override.

    A profile stored by an older release as a generic default is replaced by
    the family-specific one; any other explicit profile is left alone.
    """
    brand_key = str(brand or "").strip().lower()
    pon_key = str(pon_type or "").strip().lower()
    mapped = _TELNET_FOR.get((brand_key, pon_key))
    explicit = str(configured or "").strip()

    if explicit and explicit not in ("", "auto"):
        if (
            not mapped
            or explicit == mapped
            or brand_key in ("", "auto", "generic")
            or explicit not in _LEGACY_DEFAULT_PROFILES
        ):
            return explicit
        return mapped
    if mapped:
        return mapped
    return str(profile(brand_key).get("telnet_profile") or "epon_vsol")


# ---------------------------------------------------------------------------
#  auto-detection
# ---------------------------------------------------------------------------
def detect(
    sys_descr: str | None = None,
    interface_names: Iterable[str] | None = None,
    brand_hint: str | None = None,
    pon_hint: str | None = None,
) -> dict[str, Any]:
    """Work out ``(brand, pon_type)`` from sysDescr / interface names.

    Returns ``{"brand", "pon_type", "label", "verified", "reason"}``.  An
    explicit hint (what the user selected on the Devices page) always wins, so
    detection can never override a deliberate choice.
    """
    descr = str(sys_descr or "").strip()
    lowered = descr.lower()
    names = [str(n or "").strip() for n in (interface_names or []) if str(n or "").strip()]

    hint = str(brand_hint or "").strip().lower()
    if hint and hint != "auto" and hint in PROFILES:
        prof = PROFILES[hint]
        pon_type = str(pon_hint or prof["pon_type"]).lower()
        return {
            "brand": hint,
            "pon_type": pon_type,
            "label": prof["label"],
            "verified": bool(prof.get("verified")),
            "reason": "selected manually",
        }

    found: str | None = None
    reason = ""
    for key, prof in PROFILES.items():
        for marker in prof.get("hints") or ():
            if marker and marker in lowered:
                found, reason = key, f"sysDescr contains {marker!r}"
                break
        if found:
            break

    # interface names are the second clue (some agents return a generic sysDescr)
    if found is None and names:
        joined = " ".join(names).upper()
        if any(_CDATA_IFNAME.match(name) for name in names):
            found, reason = "cdata", "interface names use the C-Data 'epon 0/1/1' style"
        elif any(_PHOTON_IFNAME.match(name) for name in names):
            found, reason = "photon", "interface names use the flat 'pon1/ge1' style"
        elif "GPON" in joined:
            found, reason = "gpon_soho", "interface names contain GPON"
        elif "EPON" in joined:
            found, reason = "vsol", "interface names contain EPON"

    if found is None:
        if "gpon" in lowered:
            found, reason = "gpon_soho", "sysDescr mentions GPON"
        elif "epon" in lowered:
            found, reason = "generic", "sysDescr mentions EPON"
        else:
            found, reason = "generic", "nothing recognised"

    prof = PROFILES[found]
    pon_type = str(pon_hint or prof["pon_type"]).lower()
    if pon_hint in (None, "", "auto"):
        # the interface names are authoritative about the PON architecture
        joined = " ".join(names).upper()
        if "GPON" in joined and "EPON" not in joined:
            pon_type = "gpon"
        elif "EPON" in joined and "GPON" not in joined:
            pon_type = "epon"
        elif not names:
            # no interface list (or a bare sysDescr probe): the description is
            # the best clue the device gives us
            if "gpon" in lowered and "epon" not in lowered:
                pon_type = "gpon"
            elif "epon" in lowered and "gpon" not in lowered:
                pon_type = "epon"

    return {
        "brand": found,
        "pon_type": pon_type,
        "label": prof["label"],
        "verified": bool(prof.get("verified")),
        "reason": reason,
    }


def compile_patterns(patterns: Any) -> list[re.Pattern[str]]:
    """Compile a string/list of regexes, ignoring invalid ones."""
    if isinstance(patterns, str):
        patterns = [patterns]
    out: list[re.Pattern[str]] = []
    for pattern in patterns or []:
        try:
            out.append(re.compile(str(pattern)))
        except re.error:
            continue
    return out
