"""End-to-end integration for the first-run admin setup flow (T4).

Unlike tests/test_setup.py (which unit-checks each piece in isolation), this
walks the WHOLE first-run sequence through a single TestClient against an
isolated, empty users container, in the exact order a real fresh deploy hits it:

    empty store
      -> GET  /api/auth/setup-status          -> {"needs_setup": true}
      -> POST /api/auth/setup {user, pwd}      -> 200 + Set-Cookie (auto-login)
      -> GET  /api/auth/me                     -> 200 (cookie authenticates)
      -> GET  /api/auth/setup-status           -> {"needs_setup": false}
      -> POST /api/auth/setup (second attempt) -> 409 (self-closed)

and a zero-regression flow against a store that ALREADY has an admin:

    seeded store
      -> GET  /api/auth/setup-status           -> {"needs_setup": false}
      -> POST /api/auth/setup                   -> 409 (never overwrites)
      -> POST /api/auth/login (existing creds)  -> 200 + Set-Cookie (unchanged)

Storage is the in-memory Cosmos fake (tests/fake_cosmos.py) injected through
the storage.cosmos provider seam; each test re-imports the module. bcrypt +
PyJWT are real. The https base_url lets the Secure vb_session cookie
round-trip through TestClient.

Mirrors the fixtures/helpers in tests/test_setup.py.
"""

from __future__ import annotations

import asyncio
import importlib
import sys

import pytest

from fake_cosmos import FakeCosmos
from storage import cosmos


_ADMIN_PWD = "seed-pwd-from-env"


@pytest.fixture
def setup_env(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "x")
    monkeypatch.setenv("AUTH_SECRET", "unit-test-secret-key-32-bytes-long!!")
    # Intentionally set: it must NOT seed an admin anymore.
    monkeypatch.setenv("ADMIN_PASSWORD", _ADMIN_PWD)
    yield


@pytest.fixture
def fake():
    f = FakeCosmos()
    cosmos.set_container_provider(f)
    yield f
    cosmos.reset_container_provider()


def _import_bot():
    for mod in ("bot", "runtime_config", "demo_loader", "user_store"):
        sys.modules.pop(mod, None)
    return importlib.import_module("bot")


def _anon_client(bot):
    """Anonymous https TestClient (runs FastAPI startup; no cookie set)."""
    from fastapi.testclient import TestClient

    client = TestClient(bot.app, base_url="https://testserver")
    client.__enter__()
    return client


def test_full_first_run_sequence_empty_table(setup_env, fake):
    """The complete happy-path first-run flow against an empty store."""
    bot = _import_bot()
    client = _anon_client(bot)

    # 1. Fresh deploy: setup needed.
    r = client.get("/api/auth/setup-status")
    assert r.status_code == 200, r.text
    assert r.json() == {"needs_setup": True}

    # Not yet logged in: /api/auth/me is unauthorized.
    assert client.get("/api/auth/me").status_code == 401

    # 2. Create the first admin. Returns 2xx and sets the session cookie.
    r = client.post(
        "/api/auth/setup", json={"username": "owner", "password": "s3cret-pw!"}
    )
    assert r.status_code in (200, 201), r.text
    assert r.json() == {"username": "owner", "role": "admin"}
    # Set-Cookie header present (auto-login) and cookie jar holds it.
    set_cookie = r.headers.get("set-cookie", "")
    assert "vb_session=" in set_cookie, set_cookie
    assert "owner" not in set_cookie  # opaque JWT, not the username verbatim
    assert "vb_session" in client.cookies

    # 3. Auto-login works: /api/auth/me succeeds with the issued cookie.
    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json() == {"username": "owner", "role": "admin"}

    # 4. Setup is now self-closed.
    assert client.get("/api/auth/setup-status").json() == {"needs_setup": False}

    # 5. A second setup attempt is rejected and changes nothing.
    r2 = client.post(
        "/api/auth/setup", json={"username": "intruder", "password": "x"}
    )
    assert r2.status_code == 409, r2.text
    users = asyncio.run(bot.USER_STORE.list())
    assert [u["username"] for u in users] == ["owner"]
    assert asyncio.run(bot.USER_STORE.get("intruder")) is None


def test_zero_regression_existing_admin(setup_env, fake):
    """A deploy that already has an admin (e.g. our prod): setup is inert and
    login is completely unchanged. This is the 'already initialized' guarantee."""
    bot = _import_bot()
    # Pre-existing admin, created the normal way.
    asyncio.run(bot.USER_STORE.create("admin", "original-pw", role="admin"))
    client = _anon_client(bot)

    # setup-status reports initialized.
    assert client.get("/api/auth/setup-status").json() == {"needs_setup": False}

    # setup is permanently closed and never touches the existing account.
    r = client.post(
        "/api/auth/setup", json={"username": "admin", "password": "hijack"}
    )
    assert r.status_code == 409, r.text
    # No cookie issued on the closed path.
    assert "vb_session" not in client.cookies

    # Existing login still works with the ORIGINAL password (unchanged).
    login = client.post(
        "/api/auth/login", json={"username": "admin", "password": "original-pw"}
    )
    assert login.status_code == 200, login.text
    assert login.json() == {"username": "admin", "role": "admin"}
    assert "vb_session=" in login.headers.get("set-cookie", "")
    # And the hijack password was NOT applied.
    assert asyncio.run(bot.USER_STORE.verify("admin", "hijack")) is None
    assert asyncio.run(bot.USER_STORE.verify("admin", "original-pw")) is not None
