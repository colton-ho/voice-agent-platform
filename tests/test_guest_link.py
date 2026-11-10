"""Integration + unit tests for the admin temporary guest experience link.

Covers tech_design §2/§4/§6 (task T1):
- `_issue_guest_token` mints a guest-marked JWT; `_decode_jwt` round-trips and
  rejects an expired one.
- `require_user` accepts a guest token WITHOUT touching USER_STORE.get, and
  `require_admin` 403s a guest.
- POST /api/admin/guest-links (admin-only) validates ttl / scenario, returns the
  token once, and audit-logs the parameters but NEVER the token.
- POST /api/auth/guest-login (public) sets the vb_session cookie for a valid
  guest token and 401s tampered / expired / non-guest tokens.
- Defense-in-depth: a guest cookie is 403'd on a sample /api/admin/* yet 200 on
  /api/config and can mint /api/ws-token.

Reuses the store-fixture / TestClient patterns from tests/test_admin_api.py
(an admin is created via the store, then logs in to obtain the vb_session
cookie). Storage is the in-memory Cosmos fake injected through the
storage.cosmos provider seam. The guest paths themselves need no bcrypt, but
the admin login that mints links does, so the whole module importorskips
bcrypt like test_admin_api.
"""

import importlib
import sys
import time

import pytest

from fake_cosmos import FakeCosmos
from storage import cosmos

pytest.importorskip("bcrypt")


_ADMIN_PWD = "test-pwd"


@pytest.fixture(autouse=True)
def auth_env(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    monkeypatch.setenv("MINIMAX_API_KEY", "x")
    f = FakeCosmos()
    cosmos.set_container_provider(f)
    yield f
    cosmos.reset_container_provider()


def _import_app():
    for mod in list(sys.modules):
        if mod in ("bot", "runtime_config", "demo_store", "demo_loader", "user_store", "voice_store", "activity_store"):
            del sys.modules[mod]
    return importlib.import_module("bot")


def _admin_client(bot):
    import asyncio
    from fastapi.testclient import TestClient
    asyncio.run(bot.USER_STORE.create("admin", _ADMIN_PWD, role="admin"))
    client = TestClient(bot.app, base_url="https://testserver")
    client.__enter__()
    r = client.post("/api/auth/login", json={"username": "admin", "password": _ADMIN_PWD})
    assert r.status_code == 200, r.text
    return client


def _guest_client(bot, token):
    """A TestClient whose vb_session cookie is a guest token (no login call)."""
    from fastapi.testclient import TestClient
    client = TestClient(bot.app, base_url="https://testserver")
    client.__enter__()
    client.cookies.set("vb_session", token)
    return client


# ---------------------------------------------------------------------------
# AC1 — _issue_guest_token mint + decode round-trip + expiry
# ---------------------------------------------------------------------------


def test_issue_guest_token_marker_role_exp_and_constants():
    bot = _import_app()
    assert bot.GUEST_TTL_DEFAULT_MIN == 60
    assert bot.GUEST_TTL_MAX_MIN == 1440

    before = int(time.time())
    token, exp = bot._issue_guest_token(60)
    claims = bot._decode_jwt(token)
    assert claims is not None
    assert claims["guest"] is True
    assert claims["role"] == "guest"
    assert claims["sub"] == "guest"
    # exp ≈ now + 60min (allow a couple seconds of clock slack).
    assert before + 60 * 60 <= exp <= int(time.time()) + 60 * 60 + 2
    assert abs(claims["exp"] - exp) <= 2
    # No scenario passed → not present.
    assert "scenario" not in claims


def test_issue_guest_token_with_scenario():
    bot = _import_app()
    token, _ = bot._issue_guest_token(30, scenario="default")
    claims = bot._decode_jwt(token)
    assert claims["scenario"] == "default"
    assert claims["guest"] is True


def test_expired_guest_token_decodes_to_none():
    bot = _import_app()
    # Negative TTL → exp already in the past → _decode_jwt returns None.
    token, _ = bot._issue_guest_token(-1)
    assert bot._decode_jwt(token) is None


# ---------------------------------------------------------------------------
# AC2 — require_user guest branch (no USER_STORE.get) + require_admin 403
# ---------------------------------------------------------------------------


def test_require_user_accepts_guest_without_user_store_get():
    import asyncio
    bot = _import_app()

    async def _boom(*a, **k):
        raise AssertionError("USER_STORE.get must NOT be called for a guest token")

    bot.USER_STORE.get = _boom  # assert-not-called
    token, _ = bot._issue_guest_token(60, scenario="default")
    user = asyncio.run(bot.require_user(vb_session=token))
    assert user["username"] == "guest"
    assert user["role"] == "guest"
    assert user["disabled"] is False
    assert user["created_at"] is None
    assert user["scenario"] == "default"


def test_require_admin_403s_a_guest():
    import asyncio
    from fastapi import HTTPException
    bot = _import_app()
    token, _ = bot._issue_guest_token(60)
    guest = asyncio.run(bot.require_user(vb_session=token))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(bot.require_admin(user=guest))
    assert ei.value.status_code == 403


def test_require_user_still_rejects_ws_token():
    """The ws rejection must still hold — a ws-token is never a session."""
    import asyncio
    from fastapi import HTTPException
    bot = _import_app()
    ws = bot._issue_ws_token("alice", role="user")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(bot.require_user(vb_session=ws))
    assert ei.value.status_code == 401


# ---------------------------------------------------------------------------
# AC3 — POST /api/admin/guest-links: auth, validation, audit
# ---------------------------------------------------------------------------


def test_guest_links_requires_admin_anon_401_nonadmin_403():
    from fastapi.testclient import TestClient
    bot = _import_app()
    anon = TestClient(bot.app, base_url="https://testserver")
    with anon:
        assert anon.post("/api/admin/guest-links", json={}).status_code == 401

    client = _admin_client(bot)
    client.post("/api/admin/users", json={"username": "bob", "password": "pw-bob", "role": "user"})
    bob = TestClient(bot.app, base_url="https://testserver")
    bob.__enter__()
    assert bob.post("/api/auth/login", json={"username": "bob", "password": "pw-bob"}).status_code == 200
    assert bob.post("/api/admin/guest-links", json={}).status_code == 403


def test_guest_links_admin_200_default_ttl():
    bot = _import_app()
    client = _admin_client(bot)
    r = client.post("/api/admin/guest-links", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ttl_seconds"] == 60 * 60
    assert body["path"] == "/guest"
    assert body["scenario"] is None
    assert isinstance(body["expires_at"], int)
    # The returned token is a valid guest token.
    claims = bot._decode_jwt(body["token"])
    assert claims["guest"] is True


@pytest.mark.parametrize("bad_ttl", [0, 1441, "x", True, 1.5])
def test_guest_links_invalid_ttl_400(bad_ttl):
    bot = _import_app()
    client = _admin_client(bot)
    r = client.post("/api/admin/guest-links", json={"ttl_minutes": bad_ttl})
    assert r.status_code == 400, f"ttl_minutes={bad_ttl!r} should 400, got {r.status_code} {r.text}"


def test_guest_links_unknown_scenario_400():
    bot = _import_app()
    client = _admin_client(bot)
    r = client.post("/api/admin/guest-links", json={"ttl_minutes": 60, "scenario": "no-such-demo"})
    assert r.status_code == 400, r.text


def test_guest_links_default_scenario_ok():
    """DEFAULT_DEMO_ID ('default') is always a valid scenario."""
    bot = _import_app()
    client = _admin_client(bot)
    r = client.post("/api/admin/guest-links", json={"scenario": "default"})
    assert r.status_code == 200, r.text
    assert r.json()["scenario"] == "default"


def test_guest_link_create_activity_has_ttl_not_token():
    bot = _import_app()
    client = _admin_client(bot)
    r = client.post("/api/admin/guest-links", json={"ttl_minutes": 120, "scenario": "default"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]

    rows = client.get("/api/admin/activity?type=guest-link-create").json()["items"]
    assert rows, "no guest-link-create row"
    row = rows[0]
    assert row["detail"]["ttl_minutes"] == 120
    assert row["detail"]["scenario"] == "default"
    # The token must NOT appear anywhere in the audit row.
    assert "token" not in row["detail"]
    assert token not in repr(rows)


# ---------------------------------------------------------------------------
# AC4 — POST /api/auth/guest-login: cookie + 401 paths + allowlist
# ---------------------------------------------------------------------------


def test_guest_login_valid_sets_cookie_and_returns_role():
    from fastapi.testclient import TestClient
    bot = _import_app()
    token, _ = bot._issue_guest_token(60, scenario="default")
    anon = TestClient(bot.app, base_url="https://testserver")
    with anon:
        r = anon.post("/api/auth/guest-login", json={"token": token})
        assert r.status_code == 200, r.text
        assert r.json() == {
            "username": "guest", "role": "guest", "scenario": "default",
            "lang": None, "engine": None, "voice": None, "provider": None,
        }
        set_cookie = r.headers.get("set-cookie", "")
        assert "vb_session=" in set_cookie


@pytest.mark.parametrize("kind", ["expired", "tampered", "ws", "missing"])
def test_guest_login_rejects_bad_tokens(kind):
    from fastapi.testclient import TestClient
    bot = _import_app()
    if kind == "expired":
        token, _ = bot._issue_guest_token(-1)
    elif kind == "tampered":
        good, _ = bot._issue_guest_token(60)
        token = good[:-3] + ("aaa" if not good.endswith("aaa") else "bbb")
    elif kind == "ws":
        token = bot._issue_ws_token("guest", role="guest")  # not a guest marker
    else:  # missing
        token = ""
    anon = TestClient(bot.app, base_url="https://testserver")
    with anon:
        r = anon.post("/api/auth/guest-login", json={"token": token})
        assert r.status_code == 401, f"{kind}: expected 401, got {r.status_code} {r.text}"


def test_guest_login_rejects_normal_session_token():
    """A normal (non-guest) user session JWT must NOT redeem as a guest link."""
    from fastapi.testclient import TestClient
    bot = _import_app()
    normal = bot._issue_jwt({"username": "alice", "role": "user"})
    anon = TestClient(bot.app, base_url="https://testserver")
    with anon:
        assert anon.post("/api/auth/guest-login", json={"token": normal}).status_code == 401


def test_guest_login_emits_activity_row():
    from fastapi.testclient import TestClient
    bot = _import_app()
    client = _admin_client(bot)  # wires app + activity store; reads the audit log
    token, _ = bot._issue_guest_token(60, scenario="default")
    # Redeem on a SEPARATE client: guest-login sets the vb_session cookie, which
    # would otherwise clobber the admin client's cookie and 403 the read below.
    anon = TestClient(bot.app, base_url="https://testserver")
    with anon:
        assert anon.post("/api/auth/guest-login", json={"token": token}).status_code == 200

    rows = client.get("/api/admin/activity?type=guest-login").json()["items"]
    assert rows, "no guest-login row"
    assert rows[0]["actor"] == "guest"
    assert rows[0]["detail"]["scenario"] == "default"


def test_activity_allowlist_entries():
    bot = _import_app()
    from activity_store import ACTIVITY_DETAIL_VALUE_ALLOWLIST
    assert ACTIVITY_DETAIL_VALUE_ALLOWLIST["guest-link-create"] == {
        "ttl_minutes", "scenario", "lang", "engine", "voice", "provider",
    }
    assert ACTIVITY_DETAIL_VALUE_ALLOWLIST["guest-login"] == {"scenario"}


# ---------------------------------------------------------------------------
# AC5 — Defense-in-depth: guest cookie confinement
# ---------------------------------------------------------------------------


def test_guest_cookie_403_on_admin_users_200_on_config_and_ws_token():
    bot = _import_app()
    token, _ = bot._issue_guest_token(60)
    guest = _guest_client(bot, token)
    # 403 on a sample /api/admin/* (guest role, not admin).
    assert guest.get("/api/admin/users").status_code == 403
    # 200 on the user-allowed Talk config.
    assert guest.get("/api/config").status_code == 200
    # Can mint a ws-token (bound to username=guest) → Talk calls work.
    r = guest.get("/api/ws-token")
    assert r.status_code == 200, r.text
    ws_claims = bot._decode_jwt(r.json()["token"])
    assert ws_claims["sub"] == "guest"
    assert ws_claims["ws"] is True


# ---------------------------------------------------------------------------
# T1 — guest token carries the FULL launch config (lang/engine/voice/provider)
# ---------------------------------------------------------------------------

# A pipeline-valid Cantonese launch quadruple (verified primitives):
#   zh-HK engines == ['pipeline']; this voice is in the minimax table.
_PIPE_LANG = "zh-HK"
_PIPE_ENGINE = "pipeline"
_PIPE_VOICE = "Cantonese_ProfessionalHost（F)"
_PIPE_PROVIDER = "minimax"


def test_issue_guest_token_embeds_full_config_and_roundtrips():
    bot = _import_app()
    token, _ = bot._issue_guest_token(
        60, scenario="default",
        lang=_PIPE_LANG, engine=_PIPE_ENGINE, voice=_PIPE_VOICE, provider=_PIPE_PROVIDER,
    )
    claims = bot._decode_jwt(token)
    assert claims["guest"] is True
    assert claims["scenario"] == "default"
    assert claims["lang"] == _PIPE_LANG
    assert claims["engine"] == _PIPE_ENGINE
    assert claims["voice"] == _PIPE_VOICE
    assert claims["provider"] == _PIPE_PROVIDER


def test_issue_guest_token_scenario_only_omits_new_fields():
    bot = _import_app()
    token, _ = bot._issue_guest_token(60, scenario="default")
    claims = bot._decode_jwt(token)
    assert claims["scenario"] == "default"
    for k in ("lang", "engine", "voice", "provider"):
        assert k not in claims


def test_issue_guest_token_no_arg_omits_all_optional_fields():
    bot = _import_app()
    token, _ = bot._issue_guest_token(60)
    claims = bot._decode_jwt(token)
    for k in ("scenario", "lang", "engine", "voice", "provider"):
        assert k not in claims


# --- _validate_launch_config -----------------------------------------------


def test_validate_launch_config_valid_pipeline_quadruple():
    bot = _import_app()
    clean = bot._validate_launch_config(
        lang=_PIPE_LANG, engine=_PIPE_ENGINE, voice=_PIPE_VOICE, provider=_PIPE_PROVIDER,
    )
    assert clean == {
        "engine": _PIPE_ENGINE, "provider": _PIPE_PROVIDER,
        "lang": _PIPE_LANG, "voice": _PIPE_VOICE,
    }


def test_validate_launch_config_all_none_empty():
    bot = _import_app()
    assert bot._validate_launch_config() == {}


def test_validate_launch_config_bad_engine_400():
    from fastapi import HTTPException
    bot = _import_app()
    with pytest.raises(HTTPException) as ei:
        bot._validate_launch_config(engine="not-an-engine")
    assert ei.value.status_code == 400


def test_validate_launch_config_bad_provider_400():
    from fastapi import HTTPException
    bot = _import_app()
    with pytest.raises(HTTPException) as ei:
        bot._validate_launch_config(provider="not-a-provider")
    assert ei.value.status_code == 400


def test_validate_launch_config_bad_lang_400():
    from fastapi import HTTPException
    bot = _import_app()
    with pytest.raises(HTTPException) as ei:
        bot._validate_launch_config(lang="zz-ZZ")
    assert ei.value.status_code == 400


def test_validate_launch_config_unresolvable_pipeline_voice_400():
    from fastapi import HTTPException
    bot = _import_app()
    with pytest.raises(HTTPException) as ei:
        bot._validate_launch_config(engine="pipeline", provider="minimax", voice="no-such-voice")
    assert ei.value.status_code == 400


def test_validate_launch_config_bad_nova_voice_400():
    from fastapi import HTTPException
    bot = _import_app()
    with pytest.raises(HTTPException) as ei:
        bot._validate_launch_config(engine="nova-sonic", voice="no-such-nova-voice")
    assert ei.value.status_code == 400


def test_validate_launch_config_nova_sonic_plus_zh_hk_400():
    """The headline reject: nova-sonic cannot serve zh-HK (engine×lang)."""
    from fastapi import HTTPException
    bot = _import_app()
    assert bot.LANGUAGES["zh-HK"].get("engines") == ["pipeline"]
    with pytest.raises(HTTPException) as ei:
        bot._validate_launch_config(lang="zh-HK", engine="nova-sonic")
    assert ei.value.status_code == 400


def test_validate_launch_config_nova_voice_alias_resolves():
    """Re-pointed (ruling B 2026-11-09): engine nova-sonic left ENGINES and the
    alias map emptied with the C-3 roster — a former alias id no longer
    validates."""
    from fastapi import HTTPException
    bot = _import_app()
    with pytest.raises(HTTPException) as ei:
        bot._validate_launch_config(engine="nova-sonic", voice="marie")
    assert ei.value.status_code == 400


# --- POST /api/admin/guest-links full set ----------------------------------


def test_guest_links_full_config_200_and_response_carries_all():
    bot = _import_app()
    client = _admin_client(bot)
    r = client.post("/api/admin/guest-links", json={
        "ttl_minutes": 90, "scenario": "default",
        "lang": _PIPE_LANG, "engine": _PIPE_ENGINE,
        "voice": _PIPE_VOICE, "provider": _PIPE_PROVIDER,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scenario"] == "default"
    assert body["lang"] == _PIPE_LANG
    assert body["engine"] == _PIPE_ENGINE
    assert body["voice"] == _PIPE_VOICE
    assert body["provider"] == _PIPE_PROVIDER
    # The minted token itself carries the full set.
    claims = bot._decode_jwt(body["token"])
    assert claims["lang"] == _PIPE_LANG
    assert claims["engine"] == _PIPE_ENGINE
    assert claims["voice"] == _PIPE_VOICE
    assert claims["provider"] == _PIPE_PROVIDER

    # The audit row records the params but NEVER the token.
    rows = client.get("/api/admin/activity?type=guest-link-create").json()["items"]
    assert rows, "no guest-link-create row"
    detail = rows[0]["detail"]
    assert detail["ttl_minutes"] == 90
    assert detail["scenario"] == "default"
    assert detail["lang"] == _PIPE_LANG
    assert detail["engine"] == _PIPE_ENGINE
    assert detail["voice"] == _PIPE_VOICE
    assert detail["provider"] == _PIPE_PROVIDER
    assert "token" not in detail
    assert body["token"] not in repr(rows)


def test_guest_links_nova_sonic_plus_zh_hk_400():
    bot = _import_app()
    client = _admin_client(bot)
    r = client.post("/api/admin/guest-links", json={"lang": "zh-HK", "engine": "nova-sonic"})
    assert r.status_code == 400, r.text


def test_guest_links_bad_field_400():
    bot = _import_app()
    client = _admin_client(bot)
    r = client.post("/api/admin/guest-links", json={"engine": "bogus-engine"})
    assert r.status_code == 400, r.text


def test_guest_links_full_config_requires_admin():
    from fastapi.testclient import TestClient
    bot = _import_app()
    anon = TestClient(bot.app, base_url="https://testserver")
    with anon:
        r = anon.post("/api/admin/guest-links", json={
            "lang": _PIPE_LANG, "engine": _PIPE_ENGINE,
            "voice": _PIPE_VOICE, "provider": _PIPE_PROVIDER,
        })
        assert r.status_code == 401


# --- POST /api/auth/guest-login back-compat + full config ------------------


def test_guest_login_full_config_token_returns_all():
    from fastapi.testclient import TestClient
    bot = _import_app()
    token, _ = bot._issue_guest_token(
        60, scenario="default",
        lang=_PIPE_LANG, engine=_PIPE_ENGINE, voice=_PIPE_VOICE, provider=_PIPE_PROVIDER,
    )
    anon = TestClient(bot.app, base_url="https://testserver")
    with anon:
        r = anon.post("/api/auth/guest-login", json={"token": token})
        assert r.status_code == 200, r.text
        assert r.json() == {
            "username": "guest", "role": "guest", "scenario": "default",
            "lang": _PIPE_LANG, "engine": _PIPE_ENGINE,
            "voice": _PIPE_VOICE, "provider": _PIPE_PROVIDER,
        }


def test_guest_login_old_scenario_only_token_returns_scenario_plus_nulls():
    """Back-compat: a token minted before this change (scenario only) still
    redeems and the new fields come back null."""
    from fastapi.testclient import TestClient
    bot = _import_app()
    token, _ = bot._issue_guest_token(60, scenario="default")
    anon = TestClient(bot.app, base_url="https://testserver")
    with anon:
        r = anon.post("/api/auth/guest-login", json={"token": token})
        assert r.status_code == 200, r.text
        assert r.json() == {
            "username": "guest", "role": "guest", "scenario": "default",
            "lang": None, "engine": None, "voice": None, "provider": None,
        }
