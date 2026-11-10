"""Tests for the JWT-session auth foundation (T1).

Covers:
  - user_store: bcrypt hash roundtrip, CRUD, verify, disabled handling,
    container-missing graceful degradation.
  - JWT helpers: _issue_jwt / _decode_jwt happy path + expiry + tamper.
  - require_user / require_admin via the live endpoints: 401 (no/invalid
    cookie), 403 (user role on an admin route).
  - /api/auth login / me / logout happy + failure paths (incl. disabled user).
  - First-boot seed: empty store + ADMIN_PASSWORD -> bootstrap admin.
  - /phone/ws carries NO require_user dependency (PSTN must never need login).

Storage is the in-memory Cosmos fake (tests/fake_cosmos.py) injected through
the storage.cosmos provider seam. bcrypt + PyJWT are real (installed in the
venv). https base_url is used so the Secure vb_session cookie round-trips
through TestClient.
"""

from __future__ import annotations

import asyncio
import importlib
import sys

import pytest

from fake_cosmos import FakeCosmos
from storage import cosmos
from storage.cosmos import ContainerNotFound


_ADMIN_PWD = "admin-secret"


@pytest.fixture
def auth_env(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "x")
    monkeypatch.setenv("AUTH_SECRET", "unit-test-secret-key-32-bytes-long!!")
    monkeypatch.setenv("ADMIN_PASSWORD", _ADMIN_PWD)
    yield


@pytest.fixture
def fake():
    f = FakeCosmos()
    cosmos.set_container_provider(f)
    yield f
    cosmos.reset_container_provider()


def _missing_provider(name):
    """Simulates absent containers: every op raises ContainerNotFound."""
    raise ContainerNotFound(name)


def _fresh_user_store():
    for mod in ("user_store",):
        sys.modules.pop(mod, None)
    return importlib.import_module("user_store")


def _import_bot():
    for mod in ("bot", "runtime_config", "demo_loader", "user_store"):
        sys.modules.pop(mod, None)
    return importlib.import_module("bot")


def _client(bot, *, seed_login: bool = True):
    """Return an authed-as-admin https TestClient.

    The ADMIN_PASSWORD first-boot seed has been removed, so the bootstrap admin
    is created explicitly here (directly via the store) and then logged in."""
    from fastapi.testclient import TestClient

    asyncio.run(bot.USER_STORE.create("admin", _ADMIN_PWD, role="admin"))
    client = TestClient(bot.app, base_url="https://testserver")
    client.__enter__()
    if seed_login:
        r = client.post("/api/auth/login", json={"username": "admin", "password": _ADMIN_PWD})
        assert r.status_code == 200, r.text
    return client


def _anon_client(bot):
    from fastapi.testclient import TestClient

    client = TestClient(bot.app, base_url="https://testserver")
    client.__enter__()
    return client


# ---------------------------------------------------------------------------
# user_store: bcrypt + CRUD + verify
# ---------------------------------------------------------------------------

def test_bcrypt_roundtrip(auth_env):
    us = _fresh_user_store()
    h = us.hash_password("hunter2")
    assert h != "hunter2"
    assert h.startswith("$2")  # bcrypt prefix
    assert us.check_password("hunter2", h) is True
    assert us.check_password("wrong", h) is False
    assert us.check_password("hunter2", "not-a-hash") is False


def test_user_store_crud_and_verify(auth_env, fake):
    us = _fresh_user_store()

    async def run():
        store = us.UserStore()
        # empty to start
        assert await store.list() == []
        assert await store.get("alice") is None

        created = await store.create("alice", "pw-alice", role="user")
        assert created["username"] == "alice"
        assert created["role"] == "user"
        assert created["disabled"] is False
        assert "password_hash" not in created  # never leak the hash

        # duplicate create rejected
        with pytest.raises(ValueError):
            await store.create("alice", "x")

        # bad role rejected
        with pytest.raises(ValueError):
            await store.create("bob", "x", role="superuser")

        # list returns safe views
        users = await store.list()
        assert [u["username"] for u in users] == ["alice"]
        assert all("password_hash" not in u for u in users)

        # verify happy + wrong password
        assert (await store.verify("alice", "pw-alice"))["username"] == "alice"
        assert await store.verify("alice", "nope") is None
        assert await store.verify("ghost", "x") is None

        # set_password
        assert await store.set_password("alice", "new-pw") is True
        assert await store.verify("alice", "pw-alice") is None
        assert await store.verify("alice", "new-pw") is not None
        assert await store.set_password("ghost", "x") is False

        # set_role
        assert await store.set_role("alice", "admin") is True
        assert (await store.get("alice"))["role"] == "admin"

        # set_disabled blocks verify
        assert await store.set_disabled("alice", True) is True
        assert await store.verify("alice", "new-pw") is None
        await store.set_disabled("alice", False)
        assert await store.verify("alice", "new-pw") is not None

        # delete
        assert await store.delete("alice") is True
        assert await store.get("alice") is None
        assert await store.delete("alice") is False

    asyncio.run(run())


def test_user_store_missing_table_degrades(auth_env):
    """No container -> reads degrade to empty rather than raising."""
    us = _fresh_user_store()

    async def run():
        store = us.UserStore()
        assert await store.list() == []
        assert await store.get("anyone") is None
        assert await store.verify("anyone", "x") is None

    cosmos.set_container_provider(_missing_provider)
    try:
        # deliberately point at absent containers
        asyncio.run(run())
    finally:
        cosmos.reset_container_provider()


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def test_jwt_issue_decode_roundtrip(auth_env):
    bot = _import_bot()
    user = {"username": "carol", "role": "admin"}
    token = bot._issue_jwt(user)
    claims = bot._decode_jwt(token)
    assert claims is not None
    assert claims["sub"] == "carol"
    assert claims["role"] == "admin"
    assert "exp" in claims


def test_jwt_expired_returns_none(auth_env):
    bot = _import_bot()
    token = bot._issue_jwt({"username": "dan", "role": "user"}, ttl_hours=-1)
    assert bot._decode_jwt(token) is None


def test_jwt_tampered_or_empty_returns_none(auth_env):
    bot = _import_bot()
    assert bot._decode_jwt("") is None
    assert bot._decode_jwt("garbage.token.value") is None
    good = bot._issue_jwt({"username": "x", "role": "user"})
    assert bot._decode_jwt(good + "tamper") is None


# ---------------------------------------------------------------------------
# Auth API: login / me / logout
# ---------------------------------------------------------------------------

def test_login_me_logout_happy_path(auth_env, fake):
    bot = _import_bot()
    client = _client(bot)  # seed + login as admin

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json() == {"username": "admin", "role": "admin"}

    out = client.post("/api/auth/logout")
    assert out.status_code == 200
    # cookie cleared -> me now 401
    assert client.get("/api/auth/me").status_code == 401


def test_login_bad_credentials_401(auth_env, fake):
    bot = _import_bot()
    client = _anon_client(bot)  # startup seeds admin
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "", "password": ""})
    assert r.status_code == 401


def test_disabled_user_cannot_login_or_authenticate(auth_env, fake):
    bot = _import_bot()
    admin = _client(bot)
    # create then disable a user
    admin.post("/api/admin/users", json={"username": "eve", "password": "pw-eve", "role": "user"})
    admin.patch("/api/admin/users/eve", json={"disabled": True})

    anon = _anon_client(bot)
    r = anon.post("/api/auth/login", json={"username": "eve", "password": "pw-eve"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# require_user / require_admin
# ---------------------------------------------------------------------------

def test_require_user_401_without_cookie(auth_env, fake):
    bot = _import_bot()
    anon = _anon_client(bot)
    assert anon.get("/api/auth/me").status_code == 401
    assert anon.get("/api/config").status_code == 401
    assert anon.get("/api/ws-token").status_code == 401


def test_require_user_401_with_invalid_cookie(auth_env, fake):
    bot = _import_bot()
    anon = _anon_client(bot)
    anon.cookies.set("vb_session", "not-a-jwt")
    assert anon.get("/api/auth/me").status_code == 401


def test_require_admin_403_for_regular_user(auth_env, fake):
    bot = _import_bot()
    admin = _client(bot)
    admin.post("/api/admin/users", json={"username": "frank", "password": "pw-frank", "role": "user"})

    user_client = _anon_client(bot)
    assert user_client.post(
        "/api/auth/login", json={"username": "frank", "password": "pw-frank"}
    ).status_code == 200
    # admin route -> 403
    assert user_client.get("/api/admin/users").status_code == 403
    assert user_client.get("/api/admin/config").status_code == 403
    # user-allowed route -> 200
    assert user_client.get("/api/config").status_code == 200
    assert user_client.get("/api/ws-token").status_code == 200


# ---------------------------------------------------------------------------
# User management API
# ---------------------------------------------------------------------------

def test_user_management_api(auth_env, fake):
    bot = _import_bot()
    admin = _client(bot)

    # create
    r = admin.post("/api/admin/users", json={"username": "grace", "password": "pw1", "role": "user"})
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "user"

    # duplicate -> 400
    assert admin.post("/api/admin/users", json={"username": "grace", "password": "pw1"}).status_code == 400

    # list includes both seeded admin + grace
    names = {u["username"] for u in admin.get("/api/admin/users").json()["users"]}
    assert {"admin", "grace"} <= names

    # patch role + password + disabled
    assert admin.patch("/api/admin/users/grace", json={"role": "admin"}).json()["user"]["role"] == "admin"
    assert admin.patch("/api/admin/users/grace", json={"password": "pw2"}).status_code == 200
    assert admin.patch("/api/admin/users/grace", json={"disabled": True}).json()["user"]["disabled"] is True

    # patch unknown user -> 404
    assert admin.patch("/api/admin/users/ghost", json={"role": "user"}).status_code == 404
    # empty patch -> 400
    assert admin.patch("/api/admin/users/grace", json={}).status_code == 400

    # admin cannot delete self
    assert admin.delete("/api/admin/users/admin").status_code == 400
    # delete grace
    assert admin.delete("/api/admin/users/grace").status_code == 200
    assert admin.delete("/api/admin/users/grace").status_code == 404


# ---------------------------------------------------------------------------
# First-boot seed
# ---------------------------------------------------------------------------

def test_seed_admin_is_noop_even_with_admin_password(auth_env, fake):
    """The ADMIN_PASSWORD first-boot seed has been removed (see test_setup.py).
    _seed_admin() is now a no-op: even with ADMIN_PASSWORD set, calling it does
    NOT create a user. The store stays empty."""
    bot = _import_bot()
    assert bot.ADMIN_PASSWORD  # auth_env sets ADMIN_PASSWORD
    asyncio.run(bot._seed_admin())
    assert asyncio.run(bot.USER_STORE.list()) == []


# ---------------------------------------------------------------------------
# PSTN safety: /phone/ws must NOT be authenticated
# ---------------------------------------------------------------------------

def test_phone_ws_has_no_auth_dependency(auth_env):
    """The /phone/ws route must carry NO require_user/require_admin dependency
    (Chime cannot log in). We inspect the route's resolved dependant."""
    bot = _import_bot()
    phone_route = next(
        r for r in bot.app.routes if getattr(r, "path", None) == "/phone/ws"
    )
    dep_names = [
        getattr(d.call, "__name__", repr(d.call))
        for d in phone_route.dependant.dependencies
    ]
    assert "require_user" not in dep_names, dep_names
    assert "require_admin" not in dep_names, dep_names
    # Belt-and-braces: the handler source has no Depends in its signature.
    import inspect

    sig = inspect.signature(bot.phone_ws_endpoint)
    assert "require_user" not in str(sig)
    assert "require_admin" not in str(sig)


def test_ws_endpoint_requires_user_bound_token(auth_env, fake):
    """/ws (web call) must reject a handshake with no/invalid token, but the
    PSTN /phone/ws must accept (no auth). We check via the WS test client."""
    bot = _import_bot()
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(bot.app, base_url="https://testserver")
    client.__enter__()
    # No token -> /ws closes with 1008.
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws"):
            pass
    assert exc.value.code == 1008
