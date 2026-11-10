"""Integration tests for the Admin REST API + per-call hot-reload semantics.

These hit the FastAPI app via TestClient, no real WS / Bedrock / Polly. They
verify:
- JWT session auth (401 unauthenticated, admin-cookie 200 paths)
- GET admin/config returns both segments
- PUT admin/config/phone persists to disk + same-process GET reflects change
- Validation rejects invalid engine/lang/scenario
- /api/admin/demos returns the loader's list
- /api/admin/demos/rescan re-discovers a freshly-added demo
- /api/config (Web) reads runtime defaults

Auth model: bot.py uses a Cosmos user container + bcrypt + JWT cookie (replacing
the old Basic Auth). These tests stand up fake Cosmos containers seeded with
an admin and log in to obtain the vb_session cookie; TestClient persists the
cookie across requests, so per-request calls no longer carry an auth header.

Hot-reload semantics for /phone/ws (per-call) are verified at the unit level
by inspecting RUNTIME_CONFIG.get_phone_defaults() before and after PUT — if a
running pipeline captured an earlier dict, mutating the cache afterwards does
not retroactively change it (Python dict semantics + endpoint snapshot var).
"""

import importlib
import os
import sys

import pytest

from fake_cosmos import FakeCosmos
from storage import cosmos

# These integration tests log in through user_store, which requires bcrypt for
# password hashing. In environments without bcrypt the whole login path raises
# AuthUnavailable, so the entire module is skipped (the filler validation logic
# is also covered, bcrypt-free, in tests/test_timeout_filler.py).
pytest.importorskip("bcrypt")


_ADMIN_PWD = "test-pwd"


class _SelectiveCosmos:
    """FakeCosmos wrapper simulating a partially-provisioned account: any
    container in ``absent`` raises ContainerNotFound at resolution (the old
    missing-table path); the rest delegate to a shared FakeCosmos. The
    ``_create_*_table`` helpers below "create" a table by discarding it from
    ``absent`` — same call sites as the moto era."""

    def __init__(self, absent):
        self.fake = FakeCosmos()
        self.absent = set(absent)

    def __call__(self, name):
        if name in self.absent:
            raise cosmos.ContainerNotFound(name)
        return self.fake.container(name)


_prov: _SelectiveCosmos | None = None


def _create_users_table() -> None:
    _prov.absent.discard(cosmos.USERS)


def _create_demos_table() -> None:
    _prov.absent.discard(cosmos.DEMOS)


def _create_voices_table() -> None:
    _prov.absent.discard(cosmos.VOICES)


def _create_activity_table() -> None:
    _prov.absent.discard(cosmos.ACTIVITY)


@pytest.fixture(autouse=True)
def auth_env(monkeypatch):
    global _prov
    # ADMIN_PASSWORD seeds the bootstrap admin on startup; AUTH_SECRET keeps
    # JWTs stable.
    monkeypatch.setenv("ADMIN_PASSWORD", _ADMIN_PWD)
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    # B10（BC-c1）：表名 env 退役——容器隔离全走 _SelectiveCosmos provider。
    # voices/activity 容器缺省不建（多数测试要 table-missing 兜底：voices_for
    # -> 常量；activity -> 零影响 no-op），下方 registry/activity 测试自建。
    monkeypatch.setenv("MINIMAX_API_KEY", "x")  # bot.py imports succeed even w/o real key
    # All four containers start absent; the fixture "creates" users/demos and
    # the tests below create voices/activity on demand (same call sites).
    _prov = _SelectiveCosmos(absent={cosmos.USERS, cosmos.DEMOS, cosmos.VOICES, cosmos.ACTIVITY})
    cosmos.set_container_provider(_prov)
    _create_users_table()
    _create_demos_table()
    yield
    cosmos.reset_container_provider()
    _prov = None


@pytest.fixture
def fresh_runtime_json(tmp_path, monkeypatch):
    # Force RUNTIME_CONFIG to use a temp file so tests don't pollute repo.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    monkeypatch.setenv("RUNTIME_CFG_PATH_OVERRIDE", str(cfg_dir / "runtime.json"))
    yield


def _import_app():
    """(Re-)import bot.py fresh so module-level singletons see the env."""
    for mod in list(sys.modules):
        if mod in ("bot", "runtime_config", "demo_store", "demo_loader", "user_store", "voice_store", "activity_store"):
            del sys.modules[mod]
    bot = importlib.import_module("bot")
    return bot


def _admin_client(bot):
    """Return a TestClient whose vb_session cookie is a logged-in admin.

    The ADMIN_PASSWORD first-boot seed has been removed, so the bootstrap admin
    is created explicitly here (via the store), then we trigger startup via the
    context manager and log in so subsequent calls carry the cookie.
    """
    import asyncio
    from fastapi.testclient import TestClient
    asyncio.run(bot.USER_STORE.create("admin", _ADMIN_PWD, role="admin"))
    # https base_url so the Secure vb_session cookie is stored + resent.
    client = TestClient(bot.app, base_url="https://testserver")
    client.__enter__()
    r = client.post("/api/auth/login", json={"username": "admin", "password": _ADMIN_PWD})
    assert r.status_code == 200, r.text
    return client


# Back-compat shim: old tests passed headers=_basic(); auth is now via the
# persisted cookie on the client, so the header is a harmless no-op.
def _basic(user="admin", pwd=_ADMIN_PWD):
    return {}


def test_admin_endpoints_require_auth(monkeypatch, tmp_path):
    """No session cookie -> 401; logged-in admin cookie -> 200."""
    from fastapi.testclient import TestClient
    bot = _import_app()
    anon = TestClient(bot.app, base_url="https://testserver")
    with anon:
        r = anon.get("/api/admin/config")
        assert r.status_code == 401

        # Bad credentials never set a cookie -> still 401.
        bad = anon.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert bad.status_code == 401
        r = anon.get("/api/admin/config")
        assert r.status_code == 401

    client = _admin_client(bot)
    r = client.get("/api/admin/config")
    assert r.status_code == 200


def test_non_admin_user_forbidden(monkeypatch, tmp_path):
    """A logged-in regular user hits 403 on admin routes, 200 on user routes."""
    bot = _import_app()
    client = _admin_client(bot)
    # admin creates a regular user
    r = client.post(
        "/api/admin/users",
        json={"username": "bob", "password": "pw-bob", "role": "user"},
    )
    assert r.status_code == 200, r.text

    from fastapi.testclient import TestClient
    bob = TestClient(bot.app, base_url="https://testserver")
    bob.__enter__()
    assert bob.post("/api/auth/login", json={"username": "bob", "password": "pw-bob"}).status_code == 200
    assert bob.get("/api/admin/config").status_code == 403
    # user-allowed route works
    assert bob.get("/api/auth/me").json()["role"] == "user"


def test_admin_config_get_returns_both_segments(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    bot = _import_app()
    client = _admin_client(bot)
    r = client.get("/api/admin/config", headers=_basic())
    assert r.status_code == 200
    data = r.json()
    assert "web" in data and "phone" in data
    assert "engine" in data["web"]
    assert "engine" in data["phone"]


def test_phone_put_persists_and_hot_reloads(tmp_path, monkeypatch):
    """PUT phone -> next GET reflects change in same process."""
    from fastapi.testclient import TestClient
    bot = _import_app()
    client = _admin_client(bot)

    # Re-pointed (ruling B 2026-11-09): nova-sonic left ENGINES — switching to
    # it is rejected (C-3 visible capability reduction).
    r = client.put(
        "/api/admin/config/phone",
        headers=_basic(),
        json={"engine": "nova-sonic"},
    )
    assert r.status_code == 400, r.text

    # PUT the only selectable engine
    r = client.put(
        "/api/admin/config/phone",
        headers=_basic(),
        json={"engine": "pipeline"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["phone"]["engine"] == "pipeline"

    # GET reflects change
    after = client.get("/api/admin/config", headers=_basic()).json()
    assert after["phone"]["engine"] == "pipeline"

    # Module-level RUNTIME_CONFIG agrees
    assert bot.RUNTIME_CONFIG.get_phone_defaults()["engine"] == "pipeline"


def test_phone_put_rejects_invalid_engine(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    bot = _import_app()
    client = _admin_client(bot)
    r = client.put(
        "/api/admin/config/phone",
        headers=_basic(),
        json={"engine": "bogus"},
    )
    assert r.status_code == 400


# Repo data/ root — seeded into the fake Cosmos demos container for the
# real-demo tests.
_REPO_DATA_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def test_admin_demos_lists_acme_security(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    bot = _import_app()
    client = _admin_client(bot)
    _seed_demos_into_store(bot, _REPO_DATA_ROOT)
    r = client.get("/api/admin/demos", headers=_basic())
    assert r.status_code == 200
    ids = [d["id"] for d in r.json()["demos"]]
    assert "acme-security-support" in ids


def test_admin_demos_rescan(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    bot = _import_app()
    client = _admin_client(bot)
    _seed_demos_into_store(bot, _REPO_DATA_ROOT)
    r = client.post("/api/admin/demos/rescan", headers=_basic())
    assert r.status_code == 200
    assert r.json()["count"] >= 1


def test_admin_demo_detail(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    bot = _import_app()
    client = _admin_client(bot)
    _seed_demos_into_store(bot, _REPO_DATA_ROOT)
    r = client.get("/api/admin/demos/acme-security-support", headers=_basic())
    assert r.status_code == 200
    out = r.json()
    assert out["id"] == "acme-security-support"
    assert "system" in out and "zh-HK" in out["system"]
    # Full kb_body is now returned (no 500-char truncation) so the SPA editor
    # can seed from it and round-trip without data loss. kb_chars stays for
    # display, and must equal the actual returned kb_body length(s).
    assert "kb_body" in out
    kb_body = out["kb_body"]
    kb_chars = out["kb_chars"]
    if isinstance(kb_body, dict):
        assert isinstance(kb_chars, dict)
        assert {lang: len(text or "") for lang, text in kb_body.items()} == kb_chars
        assert any(n > 0 for n in kb_chars.values())
        # Long KBs must NOT be truncated to the old 500-char preview cap.
        assert any(n > 500 for n in kb_chars.values())
    else:
        assert isinstance(kb_chars, int)
        assert len(kb_body or "") == kb_chars
        assert kb_chars > 500
    # The truncated preview field is no longer emitted.
    assert "kb_preview" not in out


def test_admin_options_payload(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    bot = _import_app()
    client = _admin_client(bot)
    r = client.get("/api/admin/options", headers=_basic())
    assert r.status_code == 200
    data = r.json()
    for key in ("languages", "engines", "providers", "models", "scenarios", "voices_by_provider"):
        assert key in data


def test_models_payload_shape_and_label_source(tmp_path, monkeypatch):
    """#16 A-2：两处 models[] payload 同形——label 取 LABELS（B9），
    id/bedrock_id 字段名冻结不动（C-2），bedrock_id 值=MODELS 值（名不动值重指）。"""
    bot = _import_app()
    client = _admin_client(bot)
    expected = [
        {"id": k, "label": bot.LABELS[k], "bedrock_id": v}
        for k, v in bot.MODELS.items()
    ]
    opts = client.get("/api/admin/options", headers=_basic())
    assert opts.status_code == 200
    assert opts.json()["models"] == expected
    cfg = client.get("/api/config")
    assert cfg.status_code == 200
    assert cfg.json()["models"] == expected


def test_api_config_uses_runtime_config(tmp_path, monkeypatch):
    """/api/config should reflect runtime web defaults, not just constants.

    /api/config is now user-allowed (require_user); the admin cookie satisfies it."""
    from fastapi.testclient import TestClient
    bot = _import_app()
    client = _admin_client(bot)

    # Set web.engine=pipeline via admin
    client.put("/api/admin/config/web", headers=_basic(), json={"engine": "pipeline"})

    # /api/config should reflect it
    r = client.get("/api/config")
    assert r.status_code == 200
    assert r.json()["default_engine"] == "pipeline"


# ---------------------------------------------------------------------------
# PATCH /api/admin/demos/{id} — per-demo `filler` config (T2)
# ---------------------------------------------------------------------------

_FILLER_DEMO_ID = "filler-test-demo"


def _write_filler_demo(data_root, filler_block=None):
    """Write a minimal-but-valid demo manifest into ``data_root`` and return
    its dir path. Optionally seed a ``filler`` block."""
    import yaml as _yaml

    demo_dir = os.path.join(str(data_root), _FILLER_DEMO_ID)
    os.makedirs(demo_dir, exist_ok=True)
    manifest = {
        "id": _FILLER_DEMO_ID,
        "label": "Filler Test Demo",
        "lang": "en-US",
        "system": {"en-US": "You are a test bot."},
        "greeting": {"en-US": "Hello."},
    }
    if filler_block is not None:
        manifest["filler"] = filler_block
    with open(os.path.join(demo_dir, "manifest.yaml"), "w", encoding="utf-8") as f:
        _yaml.safe_dump(manifest, f, allow_unicode=True)
    return demo_dir


def _seed_demos_into_store(bot, data_root):
    """Load every demo under ``data_root`` from disk via DemoLoader and put()
    each into the Cosmos-backed DemoStore, then rescan so get()/list() see them.

    DEMO_LOADER is now the Cosmos-backed DemoStore (T3); it does not scan disk,
    so tests seed the container the same way the T4 migration / PATCH bridge
    do."""
    import asyncio
    from demo_loader import DemoLoader

    disk = DemoLoader(str(data_root))
    for demo in disk._cache.values():
        asyncio.run(bot.DEMO_LOADER.put(demo))
    asyncio.run(bot.DEMO_LOADER.rescan())


def _point_loader_at(bot, data_root):
    """Point the PATCH endpoint's on-disk authoring root at a temp dir AND seed
    the Cosmos-backed DemoStore from it, so the PATCH endpoint writes to a
    throwaway manifest (never the repo's data/) and the store serves it."""
    bot.DATA_ROOT = str(data_root)
    _seed_demos_into_store(bot, data_root)


def _store_filler(bot, demo_id):
    """Read the demo's ``filler`` block back from the Cosmos-backed store.

    T5 stopped writing manifest.yaml — the store is the single source of truth —
    so filler read-back now goes through DEMO_LOADER.get (cache refreshed by the
    PATCH endpoint's rescan), not the on-disk manifest."""
    demo = bot.DEMO_LOADER.get(demo_id)
    return (demo or {}).get("filler")


def test_patch_demo_filler_only_not_blocked_by_has_content(tmp_path, monkeypatch):
    """BLOCKER regression: a body containing ONLY `filler` (no tools /
    mcp_servers / localized) must NOT be 400'd by the has_content gate."""
    from fastapi.testclient import TestClient  # noqa: F401

    bot = _import_app()
    client = _admin_client(bot)
    demo_root = tmp_path / "data"
    demo_root.mkdir()
    _write_filler_demo(demo_root)
    _point_loader_at(bot, demo_root)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"filler": {"enabled": True}},
    )
    assert r.status_code == 200, r.text


def test_patch_demo_filler_valid_persists_and_reads_back(tmp_path, monkeypatch):
    """Valid filler → 200, store read-back contains the block, and GET
    detail returns it."""
    from fastapi.testclient import TestClient  # noqa: F401

    bot = _import_app()
    client = _admin_client(bot)
    demo_root = tmp_path / "data"
    demo_root.mkdir()
    _write_filler_demo(demo_root)
    _point_loader_at(bot, demo_root)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"filler": {"enabled": True, "timeout_ms": 900, "probability": 0.3}},
    )
    assert r.status_code == 200, r.text

    # Read-back from the store (probability comes back as a float,
    # not a Decimal — _from_ddb round-trip).
    in_store = _store_filler(bot, _FILLER_DEMO_ID)
    assert in_store == {"enabled": True, "timeout_ms": 900, "probability": 0.3}
    assert isinstance(in_store["probability"], float)

    # GET detail returns the filler block (loader pass-through).
    g = client.get(f"/api/admin/demos/{_FILLER_DEMO_ID}", headers=_basic())
    assert g.status_code == 200, g.text
    assert g.json().get("filler") == {"enabled": True, "timeout_ms": 900, "probability": 0.3}


@pytest.mark.parametrize(
    "bad_filler",
    [
        {"enabled": "yes"},          # non-bool
        {"enabled": 1},              # int is not bool
        {"timeout_ms": 0},           # not > 0
        {"timeout_ms": -5},          # negative
        {"timeout_ms": True},        # bool excluded even though int subclass
        {"timeout_ms": 1.5},         # float not allowed for an int field
        {"probability": 1.5},        # out of range
        {"probability": -0.1},       # out of range
        {"probability": True},       # bool excluded
        {"probability": "0.5"},      # non-numeric
    ],
)
def test_patch_demo_filler_invalid_rejected_and_store_unchanged(tmp_path, monkeypatch, bad_filler):
    """Each invalid sub-value → 400 AND the stored filler block is untouched
    (no partial write to the store before the raise)."""
    from fastapi.testclient import TestClient  # noqa: F401

    bot = _import_app()
    client = _admin_client(bot)
    demo_root = tmp_path / "data"
    demo_root.mkdir()
    seed = {"enabled": True, "timeout_ms": 1200, "probability": 0.4}
    _write_filler_demo(demo_root, filler_block=dict(seed))
    _point_loader_at(bot, demo_root)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"filler": bad_filler},
    )
    assert r.status_code == 400, r.text
    # Store must be unchanged (validation runs before the single put()).
    assert _store_filler(bot, _FILLER_DEMO_ID) == seed


def test_patch_demo_filler_partial_merge_preserves_siblings(tmp_path, monkeypatch):
    """Sending only `enabled` must NOT clobber existing timeout_ms / probability."""
    from fastapi.testclient import TestClient  # noqa: F401

    bot = _import_app()
    client = _admin_client(bot)
    demo_root = tmp_path / "data"
    demo_root.mkdir()
    seed = {"enabled": False, "timeout_ms": 1200, "probability": 0.4}
    _write_filler_demo(demo_root, filler_block=dict(seed))
    _point_loader_at(bot, demo_root)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"filler": {"enabled": True}},
    )
    assert r.status_code == 200, r.text
    assert _store_filler(bot, _FILLER_DEMO_ID) == {
        "enabled": True,
        "timeout_ms": 1200,
        "probability": 0.4,
    }


def test_patch_demo_omitting_filler_leaves_it_untouched(tmp_path, monkeypatch):
    """A PATCH that doesn't mention `filler` (here: tools-only) must leave an
    existing filler block exactly as it was."""
    from fastapi.testclient import TestClient  # noqa: F401

    bot = _import_app()
    client = _admin_client(bot)
    demo_root = tmp_path / "data"
    demo_root.mkdir()
    seed = {"enabled": True, "timeout_ms": 1200, "probability": 0.4}
    _write_filler_demo(demo_root, filler_block=dict(seed))
    _point_loader_at(bot, demo_root)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"tools": []},
    )
    assert r.status_code == 200, r.text
    assert _store_filler(bot, _FILLER_DEMO_ID) == seed


# ---------------------------------------------------------------------------
# per-demo model (LLM) override — T1 full-path persistence (BLOCKER-1 coverage)
# ---------------------------------------------------------------------------

def _store_model(bot, demo_id):
    """Read the demo's persisted ``model`` back from the store."""
    demo = bot.DEMO_LOADER.get(demo_id)
    return (demo or {}).get("model")


def test_patch_demo_model_only_persists_to_store_and_detail(tmp_path, monkeypatch):
    """BLOCKER-1: a model-only PATCH must (a) not be blocked by has_content,
    (b) actually reach `merged` and persist to the store (the merge-loop
    fix), and (c) surface in GET detail."""
    bot = _import_app()
    client = _admin_client(bot)
    demo_root = tmp_path / "data"
    demo_root.mkdir()
    _write_filler_demo(demo_root)
    _point_loader_at(bot, demo_root)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"model": "nova-lite"},
    )
    assert r.status_code == 200, r.text
    # Persisted to the store (proves model entered `merged` → put()).
    assert _store_model(bot, _FILLER_DEMO_ID) == "nova-lite"
    # And surfaces in GET detail so the SPA editor can seed from it.
    g = client.get(f"/api/admin/demos/{_FILLER_DEMO_ID}", headers=_basic())
    assert g.status_code == 200, g.text
    assert g.json().get("model") == "nova-lite"


def test_patch_demo_invalid_model_rejected(tmp_path, monkeypatch):
    bot = _import_app()
    client = _admin_client(bot)
    demo_root = tmp_path / "data"
    demo_root.mkdir()
    _write_filler_demo(demo_root)
    _point_loader_at(bot, demo_root)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"model": "not-a-real-model"},
    )
    assert r.status_code == 400, r.text
    assert "invalid model" in r.text
    # Store unchanged (validation runs before the single put()).
    assert _store_model(bot, _FILLER_DEMO_ID) is None


def test_patch_demo_explicit_null_model_clears(tmp_path, monkeypatch):
    """Set a model, then PATCH {model: null} → cleared (inherits DEFAULT_MODEL)."""
    bot = _import_app()
    client = _admin_client(bot)
    demo_root = tmp_path / "data"
    demo_root.mkdir()
    _write_filler_demo(demo_root)
    _point_loader_at(bot, demo_root)

    r1 = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"model": "nova-lite"},
    )
    assert r1.status_code == 200, r1.text
    assert _store_model(bot, _FILLER_DEMO_ID) == "nova-lite"

    r2 = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"model": None},
    )
    assert r2.status_code == 200, r2.text
    assert _store_model(bot, _FILLER_DEMO_ID) is None


# ---------------------------------------------------------------------------
# PATCH /api/admin/demos/{id} — full-field editor, store-backed (T5)
# ---------------------------------------------------------------------------


def _setup_full_field_demo(bot, tmp_path):
    """Seed a minimal demo into the store and return (client, demo_root)."""
    demo_root = tmp_path / "data"
    demo_root.mkdir()
    _write_filler_demo(demo_root)
    _point_loader_at(bot, demo_root)


def test_patch_demo_label_only_not_blocked_and_persists(tmp_path, monkeypatch):
    """has_content gate includes label: a label-only body is NOT 400'd, and the
    new label is read back from the store after rescan."""
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"label": "Renamed Demo"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["label"] == "Renamed Demo"
    assert bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["label"] == "Renamed Demo"


def test_patch_demo_tags_persist(tmp_path, monkeypatch):
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"tags": ["alpha", "beta"]},
    )
    assert r.status_code == 200, r.text
    assert bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["tags"] == ["alpha", "beta"]


def test_patch_demo_system_per_lang_replaces_wholesale(tmp_path, monkeypatch):
    """A provided system map REPLACES the field (full-set semantics) and is
    read back from the store."""
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"system": {"en-US": "You are a NEW bot.", "zh-HK": "你係個機械人。"}},
    )
    assert r.status_code == 200, r.text
    stored = bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["system"]
    assert stored == {"en-US": "You are a NEW bot.", "zh-HK": "你係個機械人。"}


def test_patch_demo_kb_body_str_and_map(tmp_path, monkeypatch):
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)

    # str form
    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"kb_body": "plain kb text"},
    )
    assert r.status_code == 200, r.text
    assert bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["kb_body"] == "plain kb text"

    # map form (per-language)
    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"kb_body": {"en-US": "english kb", "zh-HK": "粵語 kb"}},
    )
    assert r.status_code == 200, r.text
    assert bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["kb_body"] == {
        "en-US": "english kb",
        "zh-HK": "粵語 kb",
    }


def test_patch_demo_lang_change_valid_and_invalid(tmp_path, monkeypatch):
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)

    # Valid known language → 200 and persisted.
    good = next(iter(bot.LANGUAGES))
    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"lang": good},
    )
    assert r.status_code == 200, r.text
    assert bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["lang"] == good

    # Invalid language → 400, store unchanged.
    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"lang": "xx-YY"},
    )
    assert r.status_code == 400, r.text
    assert bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["lang"] == good


def test_patch_demo_system_invalid_lang_rejected(tmp_path, monkeypatch):
    """A per-lang map with an unknown lang key → 400, store unchanged."""
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)
    before = bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["system"]

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"system": {"xx-YY": "nope"}},
    )
    assert r.status_code == 400, r.text
    assert bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["system"] == before


def test_patch_demo_label_empty_rejected(tmp_path, monkeypatch):
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"label": "   "},
    )
    assert r.status_code == 400, r.text


def test_patch_demo_id_change_rejected_but_no_op_id_ok(tmp_path, monkeypatch):
    """An id different from the path → 400 (id is the primary key); an id equal
    to the path is tolerated and never mutates the id."""
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)

    # Mismatching id → 400.
    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"id": "some-other-id", "label": "X"},
    )
    assert r.status_code == 400, r.text
    # Original demo untouched (still present under its real id, label unchanged).
    assert bot.DEMO_LOADER.get("some-other-id") is None
    assert bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["label"] == "Filler Test Demo"

    # No-op id (== path) is harmless and the rest of the body applies.
    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"id": _FILLER_DEMO_ID, "label": "Kept Id"},
    )
    assert r.status_code == 200, r.text
    assert bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["id"] == _FILLER_DEMO_ID
    assert bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["label"] == "Kept Id"


def test_patch_demo_tools_persist_to_ddb_under_tool_ids(tmp_path, monkeypatch):
    """tools (no regression) now persist to the store under the internal tool_ids
    key, readable back after rescan."""
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"tools": ["end_call"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tools"] == ["end_call"]
    assert bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["tool_ids"] == ["end_call"]


def test_patch_demo_localized_persists_to_ddb(tmp_path, monkeypatch):
    """localized fine-grained write-back (no regression) now lands in the store."""
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)

    # Seed demo has system.en-US only; add a new lang via localized.
    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"localized": {"system": {"zh-HK": "粵語系統"}}},
    )
    assert r.status_code == 200, r.text
    stored = bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["system"]
    assert stored["zh-HK"] == "粵語系統"
    assert stored["en-US"]  # sibling lang preserved


# ---------------------------------------------------------------------------
# PATCH /api/admin/demos/{id} — per-demo engine/voice/provider (this task)
# ---------------------------------------------------------------------------


def test_patch_demo_engine_voice_provider_persists_and_reads_back(tmp_path, monkeypatch):
    """A valid pipeline engine+provider+voice PATCH → 200, persists to the store
    store, and GET detail returns all three (editor seed)."""
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"engine": "pipeline", "provider": "polly", "voice": "Zhiyu"},
    )
    assert r.status_code == 200, r.text
    demo = bot.DEMO_LOADER.get(_FILLER_DEMO_ID)
    assert demo["engine"] == "pipeline"
    assert demo["provider"] == "polly"
    assert demo["voice"] == "Zhiyu"

    g = client.get(f"/api/admin/demos/{_FILLER_DEMO_ID}", headers=_basic())
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["engine"] == "pipeline"
    assert body["provider"] == "polly"
    assert body["voice"] == "Zhiyu"


def test_patch_demo_engine_only_not_blocked_by_has_content(tmp_path, monkeypatch):
    """An engine-only body must NOT be 400'd by the has_content gate
    (engine/provider/voice are content keys)."""
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)

    # Re-pointed data (ruling B 2026-11-09): nova-sonic left ENGINES — the
    # engine-only-vs-has_content rule under test is unchanged.
    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"engine": "pipeline"},
    )
    assert r.status_code == 200, r.text
    assert bot.DEMO_LOADER.get(_FILLER_DEMO_ID)["engine"] == "pipeline"


def test_patch_demo_null_clears_engine_voice(tmp_path, monkeypatch):
    """Explicit null clears a previously-set field (merged as None)."""
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)

    # First set them. Re-pointed data (ruling B 2026-11-09): the selectable
    # pipeline engine + a voice from the demo's effective (minimax) table.
    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"engine": "pipeline", "voice": "Cantonese_GentleLady"},
    )
    assert r.status_code == 200, r.text
    # Then clear with explicit nulls.
    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"engine": None, "voice": None},
    )
    assert r.status_code == 200, r.text
    demo = bot.DEMO_LOADER.get(_FILLER_DEMO_ID)
    assert demo["engine"] is None
    assert demo["voice"] is None


def test_patch_demo_nova_with_tools_accepted_and_persisted(tmp_path, monkeypatch):
    """Regression-direction: setting engine on a demo that ALSO has tools (set
    in the SAME PATCH) is ACCEPTED — no tools→engine coupling exists. Both
    fields persist. Re-pointed data (ruling B 2026-11-09): nova-sonic left
    ENGINES, so the engine under test is the selectable pipeline."""
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)

    # Pick a real registered tool id so the tools field itself validates.
    from tools.registry import REGISTRY as _TOOLS
    a_tool = next(iter(_TOOLS))

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"engine": "pipeline", "tools": [a_tool]},
    )
    assert r.status_code == 200, r.text
    demo = bot.DEMO_LOADER.get(_FILLER_DEMO_ID)
    assert demo["engine"] == "pipeline"
    assert demo["tool_ids"] == [a_tool]


@pytest.mark.parametrize(
    "bad_body",
    [
        {"engine": "bogus"},
        {"provider": "bogus"},
        {"engine": "pipeline", "provider": "minimax", "voice": "NotARealVoice"},
        {"engine": "nova-sonic", "voice": "Zhiyu"},  # polly voice not a nova voice
    ],
)
def test_patch_demo_engine_voice_invalid_rejected(tmp_path, monkeypatch, bad_body):
    """Each invalid engine/provider/voice value → 400, store unchanged."""
    bot = _import_app()
    client = _admin_client(bot)
    _setup_full_field_demo(bot, tmp_path)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json=bad_body,
    )
    assert r.status_code == 400, r.text
    demo = bot.DEMO_LOADER.get(_FILLER_DEMO_ID)
    assert demo["engine"] is None and demo["provider"] is None and demo["voice"] is None


# ===========================================================================
# Voice registry admin API + voices_for fallback (T2 / Part A)
# ===========================================================================
# Most tests above run with the voices container absent, so VOICE_STORE is NOT
# live and voices_for() returns the in-code constants (byte-identical
# fallback). The CRUD tests below create the container first and trigger the
# lifespan seed via the TestClient context manager.


def test_voices_fallback_constants_when_table_missing(tmp_path, monkeypatch):
    """No VoicesTable -> VOICE_STORE not live -> voices_for() == in-code
    constants -> resolvers + /api/config are byte-identical to before."""
    bot = _import_app()
    client = _admin_client(bot)  # __enter__ ran lifespan (seed no-op, table absent)
    assert bot.VOICE_STORE.live is False

    # voices_for returns the exact in-code dicts.
    assert bot.voices_for("minimax") == bot.MINIMAX_VOICES
    assert bot.voices_for("polly") == bot.POLLY_VOICES
    assert bot.voices_for("nova-sonic") == bot.NOVA_SONIC_VOICES

    # Resolvers behave exactly as the constants path.
    assert bot._resolve_minimax_voice("Cantonese_GentleLady") == (
        "Cantonese_GentleLady", bot.MINIMAX_VOICES["Cantonese_GentleLady"]["boost"]
    )
    # Unknown voice -> provider default (no crash).
    assert bot._resolve_minimax_voice("NoSuchVoice")[0] == bot.DEFAULT_MINIMAX_VOICE
    assert bot._resolve_polly_voice("NoSuchVoice")[0] == bot.DEFAULT_POLLY_VOICE

    # Re-pointed (ruling B 2026-11-09): C-3 empties the alias map with the
    # roster — nothing to resolve, and voices_for returns the emptied constant.
    assert bot.NOVA_SONIC_VOICE_ALIASES == {}
    assert bot.voices_for("nova-sonic") == {}

    # /api/config voices reflect the constants.
    cfg = client.get("/api/config").json()
    mm_ids = {v["id"] for v in cfg["voices_by_provider"]["minimax"]}
    assert "Cantonese_GentleLady" in mm_ids
    assert len(mm_ids) == len(bot.MINIMAX_VOICES)


def test_voice_crud_roundtrip_and_seed(tmp_path, monkeypatch):
    """With a live VoicesTable: lifespan seeds the constants once; GET lists
    them; POST a new minimax voice -> it appears in GET /api/admin/voices AND
    /api/config voices_by_provider.minimax (integration); PATCH + DELETE work;
    a deleted voice falls back to the provider default in the resolver."""
    bot = _import_app()
    _create_voices_table()
    client = _admin_client(bot)  # __enter__ runs lifespan -> seed_if_empty
    # Seeded + live.
    assert bot.VOICE_STORE.live is True
    # Every in-code voice was seeded.
    listed = client.get("/api/admin/voices").json()
    assert listed["live"] is True
    seeded_mm = {v["id"] for v in listed["voices"]["minimax"]}
    assert seeded_mm == set(bot.MINIMAX_VOICES.keys())

    # --- POST a brand-new minimax voice ---
    r = client.post(
        "/api/admin/voices",
        json={"provider": "minimax", "id": "custom_test_voice",
              "label": "Custom Test", "gender": "F", "language": "en-US", "boost": "English"},
    )
    assert r.status_code == 200, r.text

    # Appears in GET /api/admin/voices (filtered) ...
    got = client.get("/api/admin/voices?provider=minimax").json()
    ids = {v["id"] for v in got["voices"]["minimax"]}
    assert "custom_test_voice" in ids

    # ... AND in /api/config voices_by_provider.minimax (rescan ran on write).
    cfg = client.get("/api/config").json()
    cfg_ids = {v["id"] for v in cfg["voices_by_provider"]["minimax"]}
    assert "custom_test_voice" in cfg_ids

    # Resolver now resolves the live voice exactly.
    assert bot._resolve_minimax_voice("custom_test_voice")[0] == "custom_test_voice"

    # --- PATCH it ---
    r = client.patch(
        "/api/admin/voices/minimax/custom_test_voice",
        json={"label": "Renamed"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["voice"]["label"] == "Renamed"
    assert bot.VOICE_STORE.get("minimax", "custom_test_voice")["label"] == "Renamed"

    # --- DELETE it -> falls back to provider default in resolver ---
    r = client.delete("/api/admin/voices/minimax/custom_test_voice")
    assert r.status_code == 200, r.text
    assert bot.VOICE_STORE.get("minimax", "custom_test_voice") is None
    # Dangling reference no longer resolves to itself -> provider default.
    assert bot._resolve_minimax_voice("custom_test_voice")[0] == bot.DEFAULT_MINIMAX_VOICE
    # Gone from /api/config too.
    cfg = client.get("/api/config").json()
    assert "custom_test_voice" not in {v["id"] for v in cfg["voices_by_provider"]["minimax"]}


def test_voice_crud_nova_and_polly(tmp_path, monkeypatch):
    """nova-sonic + polly POST round-trip (custom rows survive C-3 — the voice
    registry/store layer is untouched), and the emptied alias map is pinned."""
    bot = _import_app()
    _create_voices_table()
    client = _admin_client(bot)

    # nova-sonic create.
    r = client.post(
        "/api/admin/voices",
        json={"provider": "nova-sonic", "id": "novacustom", "label": "Nova Custom",
              "gender": "F", "locale": "en-US", "polyglot": True, "lang_label": "English (US)"},
    )
    assert r.status_code == 200, r.text
    assert "novacustom" in {v["id"] for v in client.get("/api/config").json()["nova_sonic_voices"]}

    # polly create.
    r = client.post(
        "/api/admin/voices",
        json={"provider": "polly", "id": "pollycustom", "label": "Polly Custom",
              "gender": "M", "language": "en-US", "engine": "neural"},
    )
    assert r.status_code == 200, r.text
    assert bot._resolve_polly_voice("pollycustom") == ("pollycustom", "neural")

    # Re-pointed (ruling B 2026-11-09): the alias map is emptied with the C-3
    # roster — no alias resolution remains.
    assert bot.NOVA_SONIC_VOICE_ALIASES == {}


@pytest.mark.parametrize(
    "body, why",
    [
        ({"provider": "minimax", "id": "", "language": "en-US", "boost": "English"}, "empty id"),
        ({"provider": "bogus", "id": "x", "language": "en-US"}, "bad provider"),
        ({"provider": "minimax", "id": "x", "boost": "English"}, "minimax missing language"),
        ({"provider": "minimax", "id": "x", "language": "en-US"}, "minimax missing boost"),
        ({"provider": "polly", "id": "x", "language": "en-US"}, "polly missing engine"),
        ({"provider": "polly", "id": "x", "engine": "neural"}, "polly missing language"),
        ({"provider": "nova-sonic", "id": "x", "polyglot": True}, "nova missing locale"),
        ({"provider": "nova-sonic", "id": "x", "locale": "en-US", "polyglot": "yes"}, "nova polyglot not bool"),
    ],
)
def test_voice_validation_400s(tmp_path, monkeypatch, body, why):
    """_validate_voice_body enforces per-provider required fields -> 400."""
    bot = _import_app()
    _create_voices_table()
    client = _admin_client(bot)
    r = client.post("/api/admin/voices", json=body)
    assert r.status_code == 400, f"{why}: expected 400, got {r.status_code} {r.text}"


def test_voice_delete_and_patch_404(tmp_path, monkeypatch):
    """PATCH/DELETE on a non-existent voice -> 404."""
    bot = _import_app()
    _create_voices_table()
    client = _admin_client(bot)
    assert client.delete("/api/admin/voices/minimax/nope").status_code == 404
    assert client.patch("/api/admin/voices/minimax/nope", json={"label": "x"}).status_code == 404


def test_voices_endpoint_requires_admin(tmp_path, monkeypatch):
    """Voice CRUD is admin-gated (401 unauthenticated)."""
    from fastapi.testclient import TestClient
    bot = _import_app()
    _create_voices_table()
    anon = TestClient(bot.app, base_url="https://testserver")
    with anon:
        assert anon.get("/api/admin/voices").status_code == 401
        assert anon.post("/api/admin/voices", json={"provider": "minimax", "id": "x"}).status_code == 401


def test_voice_without_label_does_not_break_config(tmp_path, monkeypatch):
    """REGRESSION: a label-less minimax/polly POST is valid (200) and must NOT
    500 /api/config or /api/admin/options afterwards. _validate_voice_body
    defaults label->id for every provider; _voice_display tolerates a missing
    label. Reproduces the round-1 reviewer BLOCKER."""
    bot = _import_app()
    _create_voices_table()
    client = _admin_client(bot)

    # minimax + polly + nova, each WITHOUT a label.
    assert client.post(
        "/api/admin/voices",
        json={"provider": "minimax", "id": "nolabel_mm", "language": "en-US", "boost": "English"},
    ).status_code == 200
    assert client.post(
        "/api/admin/voices",
        json={"provider": "polly", "id": "nolabel_polly", "language": "en-US", "engine": "neural"},
    ).status_code == 200
    assert client.post(
        "/api/admin/voices",
        json={"provider": "nova-sonic", "id": "nolabel_nova", "locale": "en-US", "polyglot": False},
    ).status_code == 200

    # The previously-crashing endpoints stay 200 and render the label as the id.
    cfg = client.get("/api/config")
    assert cfg.status_code == 200, cfg.text
    mm = {v["id"]: v["label"] for v in cfg.json()["voices_by_provider"]["minimax"]}
    assert mm["nolabel_mm"] == "nolabel_mm"
    polly = {v["id"]: v["label"] for v in cfg.json()["voices_by_provider"]["polly"]}
    assert polly["nolabel_polly"] == "nolabel_polly"

    opts = client.get("/api/admin/options")
    assert opts.status_code == 200, opts.text


# --- Activity audit log (T3 / Part B) integration -------------------------
# These exercise the REAL FastAPI routes end-to-end against the fake Cosmos
# activity container: a write-emitting endpoint (voice POST) lands a retrievable row
# via GET /api/admin/activity; a failed login records status=failure WITHOUT a
# password; the default-deny redaction holds at the store layer; and the
# table-missing degrade leaves the underlying op working.


def test_activity_voice_post_emits_retrievable_row(tmp_path, monkeypatch):
    """A T2 voice POST emits a voice-create activity row retrievable via
    GET /api/admin/activity (cross-feature integration checkpoint)."""
    bot = _import_app()
    _create_voices_table()
    _create_activity_table()
    client = _admin_client(bot)

    r = client.post(
        "/api/admin/voices",
        json={"provider": "minimax", "id": "audit_voice",
              "label": "Audit", "gender": "F", "language": "en-US", "boost": "English"},
    )
    assert r.status_code == 200, r.text

    rows = client.get("/api/admin/activity").json()["items"]
    voice_rows = [x for x in rows if x["type"] == "voice-create"]
    assert voice_rows, f"no voice-create row in {rows}"
    row = voice_rows[0]
    assert row["target"] == "minimax/audit_voice"
    assert row["actor"] == "admin"
    assert row["status"] == "success"
    # Safe detail carried provider + voice_id + allowlisted attrs.
    assert row["detail"]["provider"] == "minimax"
    assert row["detail"]["voice_id"] == "audit_voice"


def test_activity_failed_login_logged_without_password(tmp_path, monkeypatch):
    """A failed login records status=failure with the username but NO
    password value anywhere in the persisted row."""
    bot = _import_app()
    _create_activity_table()
    # Seed a real admin so the table+app are wired, then attempt a BAD login.
    client = _admin_client(bot)  # logs in as admin (success row)

    bad = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "super-secret-wrong-pw"},
    )
    assert bad.status_code == 401

    rows = client.get("/api/admin/activity").json()["items"]
    failures = [x for x in rows if x["type"] == "login" and x["status"] == "failure"]
    assert failures, f"no failed-login row in {rows}"
    fr = failures[0]
    assert fr["detail"].get("username") == "admin"
    # The wrong password must not appear ANYWHERE in the row.
    assert "super-secret-wrong-pw" not in repr(rows)
    # And a success row exists for the earlier good login.
    assert any(x["type"] == "login" and x["status"] == "success" for x in rows)


def test_activity_oddly_named_credential_stored_name_only(tmp_path, monkeypatch):
    """End-to-end default-deny proof: an mcp upsert whose body carries a
    credential-bearing 'url'/'headers' (keys NOT matching secret substrings)
    lands an activity row that records the FIELD presence but never the raw
    value. We assert via the store: the curated detail at the mcp call site is
    {id,label,transport,enabled}, and the store backstop masks anything else."""
    bot = _import_app()
    _create_activity_table()
    client = _admin_client(bot)

    # Directly verify the store-layer backstop (the BLOCKER fix) on an oddly
    # named credential field, then confirm it is persisted name-only.
    import asyncio
    asyncio.run(bot.ACTIVITY_STORE.log(
        actor="admin", type="mcp-upsert", target="srv1",
        detail={"id": "srv1", "label": "S", "transport": "sse", "enabled": True,
                "url": "https://secret-host/mcp", "headers": {"X": "raw-tok"}},
    ))
    rows = client.get("/api/admin/activity?type=mcp-upsert").json()["items"]
    assert rows, "no mcp-upsert row"
    d = rows[0]["detail"]
    assert d["id"] == "srv1" and d["transport"] == "sse" and d["enabled"] is True
    assert d["url"] == "***changed***"
    assert d["headers"] == "***changed***"
    assert "secret-host" not in repr(rows)
    assert "raw-tok" not in repr(rows)


def test_activity_table_missing_degrades_and_op_works(tmp_path, monkeypatch):
    """With NO ActivityTable: a voice POST (the underlying op) still succeeds,
    and GET /api/admin/activity returns an empty list (best-effort no-op)."""
    bot = _import_app()
    _create_voices_table()
    # NB: activity table deliberately NOT created.
    client = _admin_client(bot)

    r = client.post(
        "/api/admin/voices",
        json={"provider": "minimax", "id": "degrade_voice",
              "label": "X", "gender": "F", "language": "en-US", "boost": "English"},
    )
    # Underlying op unaffected by the absent audit table.
    assert r.status_code == 200, r.text
    assert bot.VOICE_STORE.get("minimax", "degrade_voice") is not None

    got = client.get("/api/admin/activity")
    assert got.status_code == 200
    assert got.json() == {"items": [], "cursor": None}


def test_activity_endpoint_requires_admin(tmp_path, monkeypatch):
    """GET /api/admin/activity is admin-only (401 anon, 403 non-admin)."""
    from fastapi.testclient import TestClient
    bot = _import_app()
    _create_activity_table()
    anon = TestClient(bot.app, base_url="https://testserver")
    with anon:
        assert anon.get("/api/admin/activity").status_code == 401

    client = _admin_client(bot)
    client.post("/api/admin/users", json={"username": "carol", "password": "pw-carol", "role": "user"})
    carol = TestClient(bot.app, base_url="https://testserver")
    carol.__enter__()
    assert carol.post("/api/auth/login", json={"username": "carol", "password": "pw-carol"}).status_code == 200
    assert carol.get("/api/admin/activity").status_code == 403


def test_activity_filter_by_actor_and_type(tmp_path, monkeypatch):
    """GET filters by actor + type, newest-first."""
    bot = _import_app()
    _create_activity_table()
    client = _admin_client(bot)
    # admin creates two users → two user-create rows (target = username).
    client.post("/api/admin/users", json={"username": "u1", "password": "pw1", "role": "user"})
    client.post("/api/admin/users", json={"username": "u2", "password": "pw2", "role": "user"})

    rows = client.get("/api/admin/activity?type=user-create").json()["items"]
    targets = {r["target"] for r in rows}
    assert {"u1", "u2"} <= targets
    # No password leaked in any user-create detail.
    assert "pw1" not in repr(rows) and "pw2" not in repr(rows)

    # Actor filter: everything was done by admin.
    by_admin = client.get("/api/admin/activity?actor=admin").json()["items"]
    assert by_admin and all(r["actor"] == "admin" for r in by_admin)
    # Unknown actor → empty.
    none = client.get("/api/admin/activity?actor=nobody").json()["items"]
    assert none == []


def test_log_activity_helper_swallows_store_raise(tmp_path, monkeypatch):
    """Regression (round-1 BLOCKER): if ACTIVITY_STORE.log itself raises, the
    bot.log_activity helper must STILL swallow it and never propagate — the
    `type` parameter shadows the builtin, so the except handler must not call
    type(e). Forces the store to raise and asserts log_activity returns None
    without raising."""
    import asyncio
    bot = _import_app()

    async def _boom(*a, **k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(bot.ACTIVITY_STORE, "log", _boom)
    # Must NOT raise (and the `type` kwarg must not break the except handler).
    result = asyncio.run(bot.log_activity(actor="admin", type="login", detail={"username": "admin"}))
    assert result is None


# ---------------------------------------------------------------------------
# Global config asr_filter sub-block — PUT /api/admin/config/web (T1 §6)
# ---------------------------------------------------------------------------

def test_config_web_asr_filter_persists_and_round_trips(tmp_path, monkeypatch):
    """PUT web {asr_filter:{enabled,min_confidence}} persists + GET reflects it."""
    bot = _import_app()
    client = _admin_client(bot)

    r = client.put(
        "/api/admin/config/web",
        headers=_basic(),
        json={"asr_filter": {"enabled": True, "min_confidence": 0.2}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["web"]["asr_filter"] == {"enabled": True, "min_confidence": 0.2}

    after = client.get("/api/admin/config", headers=_basic()).json()
    assert after["web"]["asr_filter"] == {"enabled": True, "min_confidence": 0.2}
    assert bot.RUNTIME_CONFIG.get_web_defaults()["asr_filter"]["min_confidence"] == 0.2


def test_config_web_asr_filter_sub_block_partial_merge(tmp_path, monkeypatch):
    """A follow-up PUT {asr_filter:{enabled:false}} must PRESERVE the prior
    min_confidence:0.2 (sub-block read-merge-write over the SHALLOW segment
    update — the BLOCKER this task guards against)."""
    bot = _import_app()
    client = _admin_client(bot)

    r1 = client.put(
        "/api/admin/config/web",
        headers=_basic(),
        json={"asr_filter": {"enabled": True, "min_confidence": 0.2}},
    )
    assert r1.status_code == 200, r1.text

    r2 = client.put(
        "/api/admin/config/web",
        headers=_basic(),
        json={"asr_filter": {"enabled": False}},
    )
    assert r2.status_code == 200, r2.text
    # enabled flipped, min_confidence preserved.
    assert r2.json()["web"]["asr_filter"] == {"enabled": False, "min_confidence": 0.2}
    after = client.get("/api/admin/config", headers=_basic()).json()
    assert after["web"]["asr_filter"] == {"enabled": False, "min_confidence": 0.2}


def test_config_phone_asr_filter_sub_block_partial_merge(tmp_path, monkeypatch):
    """Same sub-block merge guarantee on the phone segment."""
    bot = _import_app()
    client = _admin_client(bot)

    client.put(
        "/api/admin/config/phone",
        headers=_basic(),
        json={"asr_filter": {"enabled": True, "max_chars": 6}},
    )
    r = client.put(
        "/api/admin/config/phone",
        headers=_basic(),
        json={"asr_filter": {"enabled": False}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["phone"]["asr_filter"] == {"enabled": False, "max_chars": 6}
    assert bot.RUNTIME_CONFIG.get_phone_defaults()["asr_filter"]["max_chars"] == 6


@pytest.mark.parametrize("bad", [
    {"enabled": "yes"},
    {"min_confidence": 1.5},
    {"min_confidence": -0.1},
    {"min_confidence": "x"},
    {"min_confidence": True},
    {"max_chars": -1},
    {"max_chars": True},
    {"max_words": -1},
])
def test_config_web_asr_filter_bad_rejected(tmp_path, monkeypatch, bad):
    bot = _import_app()
    client = _admin_client(bot)
    r = client.put(
        "/api/admin/config/web",
        headers=_basic(),
        json={"asr_filter": bad},
    )
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# PATCH /api/admin/demos/{id} — per-demo asr_filter config (T1 §5)
# ---------------------------------------------------------------------------

def _store_asr_filter(bot, demo_id):
    demo = bot.DEMO_LOADER.get(demo_id)
    return (demo or {}).get("asr_filter")


def test_patch_demo_asr_filter_persists_and_reads_back(tmp_path, monkeypatch):
    bot = _import_app()
    client = _admin_client(bot)
    demo_root = tmp_path / "data"
    demo_root.mkdir()
    _write_filler_demo(demo_root)
    _point_loader_at(bot, demo_root)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"asr_filter": {"enabled": True, "min_confidence": 0.3, "max_chars": 6}},
    )
    assert r.status_code == 200, r.text
    assert _store_asr_filter(bot, _FILLER_DEMO_ID) == {
        "enabled": True, "min_confidence": 0.3, "max_chars": 6,
    }
    g = client.get(f"/api/admin/demos/{_FILLER_DEMO_ID}", headers=_basic())
    assert g.json().get("asr_filter") == {"enabled": True, "min_confidence": 0.3, "max_chars": 6}


def test_patch_demo_asr_filter_partial_merge_preserves_siblings(tmp_path, monkeypatch):
    bot = _import_app()
    client = _admin_client(bot)
    demo_root = tmp_path / "data"
    demo_root.mkdir()
    _write_filler_demo(demo_root, filler_block=None)
    _point_loader_at(bot, demo_root)

    client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"asr_filter": {"enabled": True, "min_confidence": 0.3}},
    )
    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"asr_filter": {"enabled": False}},
    )
    assert r.status_code == 200, r.text
    assert _store_asr_filter(bot, _FILLER_DEMO_ID) == {"enabled": False, "min_confidence": 0.3}


@pytest.mark.parametrize("bad", [
    {"enabled": "yes"},
    {"min_confidence": 1.5},
    {"min_confidence": True},
    {"max_chars": -1},
    {"max_chars": True},
    {"max_words": -1},
])
def test_patch_demo_asr_filter_invalid_rejected(tmp_path, monkeypatch, bad):
    bot = _import_app()
    client = _admin_client(bot)
    demo_root = tmp_path / "data"
    demo_root.mkdir()
    _write_filler_demo(demo_root)
    _point_loader_at(bot, demo_root)

    r = client.patch(
        f"/api/admin/demos/{_FILLER_DEMO_ID}",
        headers=_basic(),
        json={"asr_filter": bad},
    )
    assert r.status_code == 400, r.text
