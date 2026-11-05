"""Link a MikroTik PPPoE user to the ONU that serves them.

A PPPoE session on the router carries no optical information - the dBm lives on
the ONU.  This module joins the two sides so the dashboard can show, for every
user, their uptime *and* their optical power.

Matching is tried in this order (each strategy can be switched off in
``config.yaml`` -> ``correlate``):

1. ``mac``      - PPPoE ``caller-id`` equals the ONU's own MAC (exact and
                  usually correct on GPON, where the ONU bridges the customer
                  router)
2. ``fdb``      - PPPoE ``caller-id`` MAC found in the OLT bridge forwarding
                  table (the OLT learned the customer's router behind an ONU).
                  This is the reliable one on a real EPON network, because the
                  caller-id is the customer's router, not the ONU.
3. ``arp``      - user IP -> ARP/DHCP MAC -> FDB / ONU MAC
4. ``pon``       - the forwarding table only knows the PON (Photon / BDPON /
                  BTPON); the user is linked to that PON, or to its single ONU
                  when the PON has exactly one
5. ``template`` - ``onu_name_template`` such as ``pppoe-{user}``
6. ``contains`` - the username appears inside the ONU interface name
"""

from __future__ import annotations

from typing import Any, Iterable

from .logging_setup import get_logger

log = get_logger("correlate")

METHODS = ("fdb", "mac", "arp", "template", "contains", "pon")


def normalise_mac(value: Any) -> str | None:
    """Return ``AA:BB:CC:DD:EE:FF`` for anything that looks like a MAC."""
    if not value:
        return None
    if isinstance(value, (bytes, bytearray)) and len(value) == 6:
        return ":".join(f"{b:02X}" for b in value)
    text = str(value).strip().upper().replace("-", ":").replace(".", ":")
    hex_only = "".join(ch for ch in text if ch in "0123456789ABCDEF")
    if len(hex_only) != 12:
        return None
    return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))


def correlate(
    cfg: dict,
    onus: list[dict[str, Any]],
    users: list[dict[str, Any]],
    mac_by_ip: dict[str, str] | None = None,
    fdb: dict[str, Any] | None = None,
    vlan_by_mac: dict[str, int] | None = None,
    fdb_pons: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Attach ONU details to each user row (in place).

    ``onus`` is the union of every ONU of every configured OLT (each dict must
    carry ``olt_name``).  ``fdb`` maps a subscriber MAC to either
    ``(olt_name, ifindex)`` or an ONU dict.  ``fdb_pons`` maps a MAC to
    ``(olt_name, pon)`` for firmware whose forwarding table only knows the PON
    (Photon / BDPON / BTPON).

    Returns ``{"fdb": n, "mac": n, "arp": n, "template": n, "contains": n,
    "pon": n, "unmatched": n}`` for the logs/UI.
    """
    options = dict(cfg.get("correlate") or {})
    want = {
        "fdb": bool(options.get("match_by_mac_table", True)),
        "mac": bool(options.get("match_by_mac", True)),
        "arp": bool(options.get("match_by_arp", True)),
        "contains": bool(options.get("match_by_name_contains", True)),
    }
    template = str(options.get("onu_name_template") or "").strip()

    mac_by_ip = {k.upper(): v for k, v in (mac_by_ip or {}).items()}
    vlan_by_mac = {
        normalise_mac(mac) or str(mac).upper(): int(vlan)
        for mac, vlan in (vlan_by_mac or {}).items()
        if vlan
    }

    # ---- indexes -------------------------------------------------------
    onu_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    onus_by_mac: dict[str, dict[str, Any]] = {}
    onus_by_name: dict[str, dict[str, Any]] = {}
    onus_by_descr: dict[str, dict[str, Any]] = {}
    onus_by_pon: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for onu in onus:
        onu["pppoe_user"] = None
        name = str(onu.get("olt_name") or "")
        try:
            key = (name, int(onu.get("ifindex")))
        except (TypeError, ValueError):
            key = (name, -1)
        onu_by_key[key] = onu
        if onu.get("pon") is not None:
            try:
                onus_by_pon.setdefault((name, int(onu["pon"])), []).append(onu)
            except (TypeError, ValueError):
                pass

        mac = normalise_mac(onu.get("mac"))
        if mac:
            onus_by_mac.setdefault(mac, onu)
        if_name = str(onu.get("if_name") or "").strip().upper()
        if if_name:
            onus_by_name[if_name] = onu
        # Some installations write the PPPoE username (or the customer name)
        # into the ONU description.  Include it in the fuzzy fallback so an
        # OLT without a usable FDB still links those users.
        descr = str(onu.get("if_descr") or "").strip().upper()
        if descr and len(descr) >= 4 and descr not in onus_by_name:
            onus_by_descr.setdefault(descr, onu)

    fdb_index: dict[str, Any] = {}
    for mac, target in (fdb or {}).items():
        norm = normalise_mac(mac)
        if not norm:
            continue
        if isinstance(target, dict):
            fdb_index[norm] = target
        elif isinstance(target, (tuple, list)) and len(target) == 2:
            fdb_index[norm] = (str(target[0]), int(target[1]))

    def onu_from_fdb(mac: str | None) -> dict[str, Any] | None:
        if not mac:
            return None
        target = fdb_index.get(mac)
        if target is None:
            return None
        if isinstance(target, dict):
            return target
        return onu_by_key.get((target[0], target[1]))

    pon_index: dict[str, tuple[str, int]] = {}
    for mac, target in (fdb_pons or {}).items():
        norm = normalise_mac(mac)
        if not norm:
            continue
        if isinstance(target, (tuple, list)) and len(target) == 2:
            try:
                pon_index[norm] = (str(target[0]), int(target[1]))
            except (TypeError, ValueError):
                continue

    def onu_from_pon(mac: str | None) -> tuple[dict[str, Any] | None, tuple[str, int] | None]:
        """Resolve a PON-only FDB entry.

        When the PON has exactly one ONU the association is unambiguous and the
        ONU is returned directly.  With several ONUs only the PON is known, so
        the caller keeps the PON-level link (``onu_pon``) instead of inventing
        an ONU.
        """
        if not mac:
            return None, None
        target = pon_index.get(mac)
        if target is None:
            return None, None
        candidates = onus_by_pon.get(target) or []
        # An FDB entry for a PON with no known ONU would link the user to a
        # port that does not exist in the inventory - worse than no link.
        if not candidates:
            return None, None
        if len(candidates) == 1:
            return candidates[0], target
        return None, target

    counts = {method: 0 for method in METHODS}
    counts["unmatched"] = 0

    # The "contains" fallback used to loop over every ONU name for every user:
    # 8000 users x 1200 ONUs ~ 10M Python-level substring tests per pass, run
    # again after every poll - that alone kept the process CPU-bound and made
    # the web UI crawl.  One joined blob turns it into a single C-level search
    # per user; the slow scan only runs when there really is a hit.
    contains_index: dict[str, dict[str, Any]] = {}
    if want["contains"]:
        contains_index.update(onus_by_name)
        contains_index.update(onus_by_descr)
    contains_blob = "\x00".join(contains_index) if contains_index else ""

    for user in users:
        onu: dict[str, Any] | None = None
        method: str | None = None
        pon_link: tuple[str, int] | None = None
        user["onu_pon"] = None

        caller_mac = normalise_mac(user.get("caller_id"))
        address = str(user.get("address") or "").strip()
        arp_mac = normalise_mac(mac_by_ip.get(address))
        user["olt_vlan"] = vlan_by_mac.get(caller_mac or "") or vlan_by_mac.get(arp_mac or "")

        # Exact ONU hardware MAC first.  GPON ONUs frequently bridge the
        # customer router transparently, so the PPPoE caller-id *is* the ONU
        # MAC; the forwarding table can only offer a bridge-port mapping that
        # is wrong on several firmware families.  Prefer the exact match.
        if want["mac"]:
            for mac in (caller_mac, arp_mac):
                if mac and mac in onus_by_mac:
                    onu, method = onus_by_mac[mac], "mac"
                    break

        if onu is None and want["fdb"]:
            onu = onu_from_fdb(caller_mac) or onu_from_fdb(arp_mac)
            if onu is not None:
                method = "fdb"

        if onu is None and want["arp"] and arp_mac:
            onu = onu_from_fdb(arp_mac) or onus_by_mac.get(arp_mac)
            if onu is not None:
                method = "arp"

        username = str(user.get("name") or "").strip()

        if onu is None and template and username:
            candidate = template.replace("{user}", username).replace("{USER}", username.upper())
            onu = onus_by_name.get(candidate.strip().upper())
            if onu is not None:
                method = "template"

        if onu is None and contains_blob and len(username) >= 4:
            needle = username.upper()
            if needle in contains_blob:
                for name, candidate in contains_index.items():
                    if needle in name:
                        onu, method = candidate, "contains"
                        break

        # Firmware whose FDB only prints the PON (Photon / BDPON / BTPON)
        # cannot name the ONU, but the subscriber still belongs to that PON.
        if onu is None and want["fdb"] and pon_index:
            for mac in (caller_mac, arp_mac):
                onu, pon_link = onu_from_pon(mac)
                if pon_link is not None:
                    method = "pon"
                    break

        if onu is None:
            if pon_link is not None:
                olt_name, pon = pon_link
                counts["pon"] += 1
                user["onu_pon"] = pon
                user["onu_olt_name"] = olt_name
                user["onu_ifindex"] = None
                user["onu_name"] = f"pon{pon}"
                user["match_method"] = "pon"
                continue
            counts["unmatched"] += 1
            user["match_method"] = None
            user["onu_ifindex"] = None
            user["onu_name"] = None
            user["onu_olt_name"] = None
            continue

        counts[method or "unmatched"] += 1
        user["onu_ifindex"] = onu.get("ifindex")
        user["onu_name"] = onu.get("if_name")
        user["onu_olt_name"] = onu.get("olt_name")
        user["onu_pon"] = onu.get("pon")
        user["match_method"] = method
        user["rx_power_dbm"] = onu.get("rx_power_dbm")
        user["tx_power_dbm"] = onu.get("tx_power_dbm")
        user["olt_rx_power_dbm"] = onu.get("olt_rx_power_dbm")
        user["distance_m"] = onu.get("distance_m")
        onu["pppoe_user"] = user.get("name")

    if users:
        log.info(
            "correlation: %d/%d users matched an ONU (%s)",
            len(users) - counts["unmatched"], len(users), counts,
        )
    return counts


def build_fdb(olt_snapshots: Iterable[Any]) -> dict[str, tuple[str, int]]:
    """Flatten the per-OLT ``mac_map`` of every snapshot into one lookup."""
    combined: dict[str, tuple[str, int]] = {}
    for snapshot in olt_snapshots:
        name = getattr(snapshot, "name", None)
        mac_map = getattr(snapshot, "mac_map", None) or {}
        for mac, ifindex in mac_map.items():
            norm = normalise_mac(mac)
            if not norm or ifindex is None:
                continue
            combined.setdefault(norm, (str(name), int(ifindex)))
    return combined


def build_fdb_vlans(olt_snapshots: Iterable[Any]) -> dict[str, int]:
    """Flatten the per-OLT ``mac_vlans`` map of every snapshot."""
    combined: dict[str, int] = {}
    for snapshot in olt_snapshots:
        for mac, vlan in (getattr(snapshot, "mac_vlans", None) or {}).items():
            norm = normalise_mac(mac)
            if not norm or not vlan:
                continue
            combined.setdefault(norm, int(vlan))
    return combined


def build_fdb_pons(olt_snapshots: Iterable[Any]) -> dict[str, tuple[str, int]]:
    """Flatten the PON-level FDB (MAC -> PON) of every snapshot.

    Photon / BDPON / BTPON firmware learns a subscriber at the PON, not at the
    ONU, so this is the strongest association available on those devices.
    """
    combined: dict[str, tuple[str, int]] = {}
    for snapshot in olt_snapshots:
        name = str(getattr(snapshot, "name", None) or "")
        for mac, pon in (getattr(snapshot, "mac_pons", None) or {}).items():
            norm = normalise_mac(mac)
            if not norm or pon is None:
                continue
            try:
                combined.setdefault(norm, (name, int(pon)))
            except (TypeError, ValueError):
                continue
    return combined
