"""User store: Cosmos-backed account container with bcrypt password hashing.

Backs the JWT-session auth model that replaced the old shared SITE_PASSWORD /
ADMIN_PASSWORD Basic Auth (see bot.py). Storage goes through the
``storage.cosmos`` seam (container ``users``, PK /username, id=username —
pipeline-infra-spec §1.1): async methods wrap the sync container verbs in
``asyncio.to_thread`` so they never block the event loop, and graceful
degradation (log + treat as empty) when the container does not exist.

Container shape
---------------
- Container: ``users`` (§1.1 frozen). Partition key ``username``; id=username
  (point ops).
- Item fields:
    id             str   (= username, Cosmos id)
    username       str
    password_hash  str   (bcrypt, cost 12)
    role           str   ("admin" | "user")
    created_at     int   (unix seconds)
    disabled       bool

Security
--------
Passwords are NEVER stored in plaintext and the ``password_hash`` is NEVER
returned by :meth:`UserStore.get` / :meth:`UserStore.list` — those return a
"safe" view (username/role/created_at/disabled only). :meth:`UserStore.verify`
is the only path that reads the hash, and it returns the same safe view.

``bcrypt`` is imported lazily so that a missing dependency degrades only the
auth path — it must never break ``import user_store`` or, by extension,
``import bot`` (which would take down the unauthenticated PSTN ``/phone/ws``
bridge). When bcrypt is unavailable, hashing/verification raise
:class:`AuthUnavailable` at call time and the caller maps that to a 503.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from storage import cosmos
from storage.cosmos import ContainerConflict, ContainerNotFound

logger = logging.getLogger(__name__)

VALID_ROLES = ("admin", "user")
BCRYPT_COST = 12


class AuthUnavailable(RuntimeError):
    """Raised when bcrypt is not importable so password ops cannot run.

    Kept distinct from ValueError so callers can map it to HTTP 503 (server
    mis-config) rather than 400/401 (bad input)."""


def _bcrypt():
    """Lazy import of bcrypt. Raises AuthUnavailable if unavailable so a
    missing dependency degrades only the auth path, never module import."""
    try:
        import bcrypt  # type: ignore
    except Exception as e:  # pragma: no cover - exercised only without bcrypt
        raise AuthUnavailable(f"bcrypt not installed: {e}") from e
    return bcrypt


def hash_password(password: str) -> str:
    """Return a bcrypt hash (cost 12) of ``password`` as a UTF-8 str."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    bcrypt = _bcrypt()
    # bcrypt truncates at 72 bytes; encode then hash.
    salt = bcrypt.gensalt(rounds=BCRYPT_COST)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def check_password(password: str, password_hash: str) -> bool:
    """Constant-time bcrypt comparison. False on any error / malformed hash."""
    if not password or not password_hash:
        return False
    bcrypt = _bcrypt()
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def _safe_view(item: dict[str, Any]) -> dict[str, Any]:
    """Public projection of a user row — never includes ``password_hash``."""
    created = item.get("created_at")
    try:
        created = int(created) if created is not None else None
    except (TypeError, ValueError):
        created = None
    return {
        "username": item.get("username"),
        "role": item.get("role") or "user",
        "created_at": created,
        "disabled": bool(item.get("disabled")),
    }


class UserStore:
    """Cosmos-backed user account store.

    All public methods are async and run the blocking container verbs via
    ``asyncio.to_thread``. Containers resolve through ``storage.cosmos`` on
    first use, so constructing a ``UserStore`` never touches Azure (cheap to
    import). ``table_name`` is retained for signature compatibility (the env
    name is retired in B-5).
    """

    def __init__(self, table_name: str | None = None, region: str | None = None):
        self._table_name = table_name or cosmos.USERS

    def _users(self):
        return cosmos.get_container(cosmos.USERS)

    # ---- reads -----------------------------------------------------------

    async def get(self, username: str) -> dict[str, Any] | None:
        """Return the safe view of one user (no password_hash) or None."""
        if not username:
            return None
        item = await self._get_raw(username)
        return _safe_view(item) if item else None

    async def _get_raw(self, username: str) -> dict[str, Any] | None:
        """Internal: return the full row INCLUDING password_hash, or None.
        Missing container/row degrades to None (treated as empty)."""
        def _call():
            return self._users().read_item(item_id=username, partition_key=username)

        try:
            return await asyncio.to_thread(_call)
        except ContainerNotFound:
            return None
        except Exception as e:
            logger.warning(f"user_store: read failed for {username!r}: {e}")
            return None

    async def list(self) -> list[dict[str, Any]]:
        """Return safe views of all users. Missing container degrades to []."""
        def _call():
            # users 表只存少量管理帳號（§1.1 point-ops 容器），跨分區全量可接受
            return self._users().query_items("SELECT * FROM c", [])

        try:
            items = await asyncio.to_thread(_call)
        except ContainerNotFound:
            logger.warning(
                f"user_store: container for {self._table_name!r} missing; list() -> []"
            )
            return []
        # Any OTHER error (e.g. AccessDenied, throttling) must NOT be
        # swallowed as "empty": that would make _needs_setup() wrongly
        # report needs_setup=true and send an initialized deployment to the
        # /setup wizard. Re-raise so the caller surfaces a real error.
        except Exception as e:
            logger.error(f"user_store: list failed (re-raising): {e}")
            raise
        users = [_safe_view(it) for it in items]
        users.sort(key=lambda u: (u.get("username") or ""))
        return users

    # ---- writes ----------------------------------------------------------

    async def create(
        self, username: str, password: str, role: str = "user"
    ) -> dict[str, Any]:
        """Create a user. Raises ValueError if it already exists, the role is
        invalid, or username/password are empty. Returns the safe view."""
        if not isinstance(username, str) or not username.strip():
            raise ValueError("username must be a non-empty string")
        username = username.strip()
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role {role!r}: must be one of {list(VALID_ROLES)}")
        if not password:
            raise ValueError("password must be a non-empty string")

        existing = await self._get_raw(username)
        if existing is not None:
            raise ValueError(f"user {username!r} already exists")

        item = {
            "id": username,
            "username": username,
            "password_hash": hash_password(password),
            "role": role,
            "created_at": int(time.time()),
            "disabled": False,
        }

        def _call():
            return self._users().create_item(item)

        # create_item is the atomic uniqueness guard: on a concurrent collision
        # it raises ContainerConflict, NOT the ValueError that the _get_raw
        # pre-check above raises. Normalize it to the SAME
        # ValueError("already exists") so every caller sees uniform semantics
        # whether the dup was caught by the pre-check or lost the write race.
        try:
            await asyncio.to_thread(_call)
        except ContainerConflict as e:
            raise ValueError(f"user {username!r} already exists") from e
        return _safe_view(item)

    async def _patch(self, username: str, ops: list[dict[str, Any]]) -> bool:
        """Patch one user row (fields only). False iff the row vanished
        between the caller's existence check and the write."""

        def _call():
            return self._users().patch_item(username, username, ops)

        try:
            await asyncio.to_thread(_call)
            return True
        except ContainerNotFound:
            return False

    async def set_password(self, username: str, password: str) -> bool:
        """Reset a user's password. Returns False if the user does not exist."""
        if not password:
            raise ValueError("password must be a non-empty string")
        if await self._get_raw(username) is None:
            return False
        new_hash = hash_password(password)
        return await self._patch(username, [{"op": "set", "path": "/password_hash", "value": new_hash}])

    async def set_role(self, username: str, role: str) -> bool:
        """Change a user's role. Returns False if the user does not exist."""
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role {role!r}: must be one of {list(VALID_ROLES)}")
        if await self._get_raw(username) is None:
            return False
        return await self._patch(username, [{"op": "set", "path": "/role", "value": role}])

    async def set_disabled(self, username: str, disabled: bool) -> bool:
        """Enable/disable a user. Returns False if the user does not exist."""
        if await self._get_raw(username) is None:
            return False
        return await self._patch(username, [{"op": "set", "path": "/disabled", "value": bool(disabled)}])

    async def delete(self, username: str) -> bool:
        """Delete a user. Returns True if a row was removed."""
        def _call():
            return self._users().delete_item(item_id=username, partition_key=username)

        try:
            await asyncio.to_thread(_call)
        except ContainerNotFound:
            return False
        return True

    async def verify(self, username: str, password: str) -> dict[str, Any] | None:
        """Return the safe view of the user iff the password matches AND the
        account is not disabled; otherwise None.

        Constant-ish: a missing user still runs a dummy bcrypt check is overkill
        here (the table lookup already leaks timing), so we return early on miss.
        """
        item = await self._get_raw(username)
        if not item:
            return None
        if bool(item.get("disabled")):
            return None
        if not check_password(password, item.get("password_hash") or ""):
            return None
        return _safe_view(item)
