"""Web users, roles and signed session cookies.

Three roles, deliberately small:

``viewer``      read every monitoring page, change nothing
``manager``     the above + add/edit/disable/delete OLTs and routers, poll,
                acknowledge alerts
``superadmin``  everything, including site settings, users, SQL export/import,
                database/remote-connection settings and the developer page

Passwords are stored as ``scrypt`` hashes (stdlib, no extra dependency).  The
session cookie is stateless: a compact HMAC-SHA256 signed token carrying the
user id and an expiry.  Every request re-reads the user row, so disabling a
user or changing a role takes effect on the next click - no restart needed.

The signing key lives in ``data/.session_key`` (mode 0600).  It is created on
first use and is separate from ``data/.secret_key`` (which encrypts device
passwords).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any

from . import db
from .config import db_path as resolve_db_path

SESSION_COOKIE = "olt_session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days
MIN_PASSWORD_LENGTH = 8

ROLES = ("viewer", "manager", "superadmin")
ROLE_LABELS = {
    "viewer": "Viewer",
    "manager": "Manager",
    "superadmin": "Super admin",
}
ROLE_HINTS = {
    "viewer": "read-only access to all monitoring pages",
    "manager": "manage devices, poll now, acknowledge alerts",
    "superadmin": "full access: users, settings, SQL, developer page",
}

#: Extra permissions an administrator can enable or disable per account.
#: A super admin always has every permission, independent of these flags, so
#: the system can never lock itself out of its own settings.
PERMISSIONS: tuple[dict[str, str], ...] = (
    {
        "key": "perm_devices",
        "label": "Devices",
        "hint": "add, edit, test and poll OLTs and routers",
    },
    {
        "key": "perm_tools",
        "label": "SNMP tools",
        "hint": "run read-only SNMP queries from the Tools page",
    },
    {
        "key": "perm_settings",
        "label": "Site settings",
        "hint": "change site/database settings and run backups",
    },
    {
        "key": "perm_users",
        "label": "Web users",
        "hint": "create, edit, disable and delete web accounts",
    },
    {
        "key": "perm_developer",
        "label": "Developer page",
        "hint": "read source files and build the distribution zip",
    },
    {
        "key": "perm_sql",
        "label": "SQL export/import",
        "hint": "download and import a database dump",
    },
)

PERMISSION_KEYS = tuple(item["key"] for item in PERMISSIONS)

DEFAULT_PERMISSIONS = {
    # a viewer keeps the historically read-only SNMP tools page
    "viewer": {"perm_tools": 1},
    "manager": {"perm_devices": 1, "perm_tools": 1},
    "superadmin": {key: 1 for key in PERMISSION_KEYS},
}

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


# ---------------------------------------------------------------------------
#  passwords
# ---------------------------------------------------------------------------
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_password(password: str) -> str:
    """``scrypt$n$r$p$salt$hash`` - self-describing, so parameters can change."""
    if not isinstance(password, str) or not password:
        raise ValueError("password is required")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = (stored or "").split("$", 5)
        if scheme != "scrypt":
            return False
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
        actual = hashlib.scrypt(
            (password or "").encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except Exception:
        return False
    return hmac.compare_digest(actual, expected)


def validate_password(password: str, confirm: str | None = None) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if confirm is not None and password != confirm:
        raise ValueError("the two passwords do not match")


def validate_username(username: str) -> str:
    name = (username or "").strip().lower()
    if not (3 <= len(name) <= 32):
        raise ValueError("username must be 3-32 characters")
    if not all(ch.isalnum() or ch in "._-" for ch in name):
        raise ValueError("username may only contain letters, digits, dot, dash and underscore")
    if name[0] in "._-":
        raise ValueError("username must start with a letter or digit")
    return name


# ---------------------------------------------------------------------------
#  users
# ---------------------------------------------------------------------------
def _public(row: dict[str, Any]) -> dict[str, Any]:
    user = dict(row)
    user.pop("password_hash", None)
    user["active"] = bool(user.get("active"))
    user["session_epoch"] = int(user.get("session_epoch") or 0)
    user["role_label"] = ROLE_LABELS.get(str(user.get("role")), str(user.get("role")))
    for item in PERMISSIONS:
        user[item["key"]] = bool(user.get(item["key"]))
    if not user.get("display_name"):
        user["display_name"] = user.get("username")
    return user


def count_users(path: str | Path | None) -> int:
    rows = db.query(path, "SELECT COUNT(*) AS n FROM users")
    return int(rows[0]["n"]) if rows else 0


def setup_required(path: str | Path | None) -> bool:
    """True until the first super-admin account exists."""
    return count_users(path) == 0


def list_users(path: str | Path | None) -> list[dict[str, Any]]:
    rows = db.query(
        path,
        "SELECT * FROM users ORDER BY FIELD(role, 'superadmin', 'manager', 'viewer'), username",
    )
    return [_public(row) for row in rows]


def get_user(path: str | Path | None, user_id: int) -> dict[str, Any] | None:
    rows = db.query(path, "SELECT * FROM users WHERE id = ?", [int(user_id)])
    return _public(rows[0]) if rows else None


def get_user_raw(path: str | Path | None, user_id: int) -> dict[str, Any] | None:
    rows = db.query(path, "SELECT * FROM users WHERE id = ?", [int(user_id)])
    return rows[0] if rows else None


def get_user_by_username(path: str | Path | None, username: str) -> dict[str, Any] | None:
    rows = db.query(path, "SELECT * FROM users WHERE username = ?", [(username or "").strip().lower()])
    return rows[0] if rows else None


def create_user(
    path: str | Path | None,
    username: str,
    password: str,
    role: str = "viewer",
    *,
    display_name: str = "",
    email: str = "",
    active: bool = True,
    permissions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name = validate_username(username)
    validate_password(password)
    if role not in ROLES:
        raise ValueError("unknown role")
    if get_user_by_username(path, name):
        raise ValueError(f"user {name!r} already exists")
    flags = dict(DEFAULT_PERMISSIONS.get(role) or {})
    if permissions is not None:
        flags = {
            key: 1 if permissions.get(key) else 0
            for key in PERMISSION_KEYS
        }
    stamp = db.utcnow_iso()
    columns = [
        "username", "password_hash", "role", "display_name", "email",
        "active", "created_at", "updated_at", *PERMISSION_KEYS,
    ]
    values: list[Any] = [
        name, hash_password(password), role, display_name.strip(), email.strip(),
        1 if active else 0, stamp, stamp,
    ]
    values.extend(int(flags.get(key) or 0) for key in PERMISSION_KEYS)
    placeholders = ", ".join("?" for _ in columns)
    user_id = db.execute(
        path,
        f"INSERT INTO users ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    return get_user(path, user_id) or {}


def update_user(path: str | Path | None, user_id: int, **fields: Any) -> dict[str, Any]:
    row = get_user_raw(path, user_id)
    if not row:
        raise ValueError("user not found")
    allowed = {"role", "display_name", "email", "active", *PERMISSION_KEYS}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "role":
            if value not in ROLES:
                raise ValueError("unknown role")
            sets.append("role = ?")
            params.append(value)
        elif key == "active":
            sets.append("active = ?")
            params.append(1 if value else 0)
        elif key in PERMISSION_KEYS:
            sets.append(f"{key} = ?")
            params.append(1 if value else 0)
        else:
            sets.append(f"{key} = ?")
            params.append(str(value or "").strip())
    if not sets:
        return _public(row)
    sets.append("updated_at = ?")
    params.append(db.utcnow_iso())
    params.append(int(user_id))
    db.execute(path, f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
    # never lock the system out: at least one *active* super-admin must remain
    _guard_last_admin(path)
    return get_user(path, user_id) or {}


def set_password(path: str | Path | None, user_id: int, password: str) -> None:
    validate_password(password)
    if not get_user_raw(path, user_id):
        raise ValueError("user not found")
    db.execute(
        path,
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        [hash_password(password), db.utcnow_iso(), int(user_id)],
    )
    # a changed password must also invalidate cookies issued before the change
    bump_session_epoch(path, user_id)


def delete_user(path: str | Path | None, user_id: int) -> None:
    row = get_user_raw(path, user_id)
    if not row:
        return
    db.execute(path, "DELETE FROM users WHERE id = ?", [int(user_id)])
    _guard_last_admin(path)


def _guard_last_admin(path: str | Path | None) -> None:
    rows = db.query(
        path,
        "SELECT COUNT(*) AS n FROM users WHERE role = 'superadmin' AND active = 1",
    )
    if not rows or int(rows[0]["n"]) == 0:
        # restore the most recent super-admin rather than leaving no way in
        last = db.query(
            path,
            "SELECT id FROM users WHERE role = 'superadmin' ORDER BY id DESC LIMIT 1",
        )
        if last:
            db.execute(
                path,
                "UPDATE users SET active = 1, updated_at = ? WHERE id = ?",
                [db.utcnow_iso(), int(last[0]["id"])],
            )


def authenticate(path: str | Path | None, username: str, password: str) -> dict[str, Any] | None:
    row = get_user_by_username(path, username)
    if not row or not row.get("active"):
        return None
    if not verify_password(password, str(row.get("password_hash") or "")):
        return None
    touch_login(path, int(row["id"]))
    return _public(row)


def touch_login(path: str | Path | None, user_id: int) -> None:
    db.execute(
        path,
        "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
        [db.utcnow_iso(), db.utcnow_iso(), int(user_id)],
    )


# ---------------------------------------------------------------------------
#  roles / permissions
# ---------------------------------------------------------------------------
def has_permission(user: dict[str, Any] | None, key: str) -> bool:
    """True when *user* may use the area guarded by permission *key*.

    A super admin is never limited by the flags (that is what makes the role
    "everything", and it guarantees the system stays administrable).
    """
    if not user:
        return False
    if user.get("role") == "superadmin":
        return True
    return bool(user.get(key))


def permissions_of(user: dict[str, Any] | None) -> dict[str, bool]:
    """The permission flags as booleans, ready for the templates."""
    if not user:
        return {key: False for key in PERMISSION_KEYS}
    if user.get("role") == "superadmin":
        return {key: True for key in PERMISSION_KEYS}
    return {key: bool(user.get(key)) for key in PERMISSION_KEYS}


def can_manage(user: dict[str, Any] | None) -> bool:
    return has_permission(user, "perm_devices")


def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user) and user.get("role") == "superadmin"


# ---------------------------------------------------------------------------
#  signed session cookie
# ---------------------------------------------------------------------------
def _session_key(path: str | Path | None) -> bytes:
    if path:
        data_dir = Path(path).parent
    else:
        data_dir = Path(resolve_db_path(_load_cfg())).parent
    key_file = data_dir / ".session_key"
    try:
        raw = key_file.read_bytes()
        if len(raw) >= 32:
            return raw
    except OSError:
        pass
    raw = secrets.token_bytes(32)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(raw)
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return raw


def _load_cfg() -> dict:
    from .config import load_config

    return load_config()


def session_epoch(path: str | Path | None, user_id: int) -> int:
    rows = db.query(path, "SELECT session_epoch FROM users WHERE id = ?", [int(user_id)])
    return int(rows[0].get("session_epoch") or 0) if rows else 0


def bump_session_epoch(path: str | Path | None, user_id: int) -> int:
    """Invalidate every session cookie issued for *user_id* (logout / password change)."""
    epoch = session_epoch(path, user_id) + 1
    db.execute(
        path,
        "UPDATE users SET session_epoch = ?, updated_at = ? WHERE id = ?",
        [epoch, db.utcnow_iso(), int(user_id)],
    )
    return epoch


def create_session(path: str | Path | None, user_id: int) -> str:
    payload = {
        "uid": int(user_id),
        "ver": session_epoch(path, user_id),
        "exp": int(time.time()) + SESSION_MAX_AGE,
        "nonce": secrets.token_hex(8),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(_session_key(path), body.encode("ascii"), hashlib.sha256).digest()
    return body + "." + _b64e(signature)


def read_session(path: str | Path | None, token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    try:
        expected = hmac.new(_session_key(path), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64d(signature), expected):
            return None
        payload = json.loads(_b64d(body).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp") or 0) < time.time():
        return None
    return payload


def user_from_token(path: str | Path | None, token: str | None) -> dict[str, Any] | None:
    payload = read_session(path, token)
    if not payload:
        return None
    user = get_user(path, int(payload.get("uid") or 0))
    if not user or not user.get("active"):
        return None
    if int(payload.get("ver") or 0) != int(user.get("session_epoch") or 0):
        return None
    return user


# ---------------------------------------------------------------------------
#  audit trail
# ---------------------------------------------------------------------------
def audit(
    path: str | Path | None,
    username: str,
    action: str,
    detail: str = "",
    ip: str = "",
) -> None:
    try:
        db.execute(
            path,
            "INSERT INTO audit_log (ts, username, action, detail, ip) VALUES (?, ?, ?, ?, ?)",
            [db.utcnow_iso(), username or "-", action[:64], (detail or "")[:4000], ip[:64]],
        )
    except Exception:  # never let auditing break the request
        pass


def recent_audit(path: str | Path | None, limit: int = 100) -> list[dict[str, Any]]:
    return db.query(
        path,
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
        [max(1, min(int(limit), 500))],
    )
