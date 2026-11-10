"""Tests for per-demo engine/voice/provider (tech_design 9b3d9e02 §2-§4, §8).

Two layers, both bcrypt-free (mirror the _validate_filler_patch tests in
tests/test_timeout_filler.py — no auth path needed):

* ``demo_loader.normalize_demo_dict`` carries the three optional top-level keys
  (present → kept, absent → None) identically on both the manifest and DDB
  paths, and a demo with none of them is byte-identical to before.
* ``bot._validate_engine_voice_patch`` validation logic: valid pipeline / nova
  combos, 400s for every invalid value, explicit-null clears, empty body → {},
  and the engine-UNSET lenient voice rule. (Nova Sonic supports tools/MCP, so
  there is no tools→engine constraint.) Nova assertions re-pointed to the
  post-C-3 state (decision (a), ruling B 2026-11-09): engine nova-sonic is out
  of ENGINES and the nova voice table is emptied, so nova combos now 400.

The full PATCH integration path lives in tests/test_admin_api.py (skipped
without bcrypt); these cover the pure validation + normalize logic.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import demo_loader  # noqa: E402
import bot  # noqa: E402


# Concrete sample voice ids pulled from the live tables (kept here so a table
# rename surfaces as a test failure rather than silent drift).
# C-3 (decision (a), ruling B 2026-11-09): NOVA_SONIC_VOICES/ALIASES are
# emptied and engine nova-sonic left ENGINES — the two nova ids below are kept
# as pre-C-3 data whose PATCHes must now 400.
NOVA_VOICE = "tiffany"          # was in NOVA_SONIC_VOICES (now emptied)
NOVA_ALIAS = "marie"            # was NOVA_SONIC_VOICE_ALIASES -> "ambre" (map emptied)
MINIMAX_VOICE = "Cantonese_GentleLady"  # exact key in MINIMAX_VOICES
POLLY_VOICE = "Zhiyu"           # exact (case-sensitive) key in POLLY_VOICES


def _body(**kw):
    """A DemoPatchBody-like stand-in: attribute access for engine/provider/voice/model.
    The helper only reads getattr(body, key) for keys in ``fields``."""
    attrs = {"engine": None, "provider": None, "voice": None, "model": None}
    attrs.update(kw)
    return SimpleNamespace(**attrs)


# A concrete known-good model key + a known-bad one (kept here so a MODELS
# registry rename surfaces as a test failure rather than silent drift).
GOOD_MODEL = "nova-lite"        # in bot.MODELS
BAD_MODEL = "not-a-real-model"  # not in bot.MODELS


def _validate(body, fields, *, demo_engine=None, demo_provider=None):
    return bot._validate_engine_voice_patch(
        body,
        fields,
        demo_engine=demo_engine,
        demo_provider=demo_provider,
    )


# =====================================================================
# normalize_demo_dict — the three keys (AC#1)
# =====================================================================

def test_normalize_carries_three_keys_when_present():
    raw = {
        "id": "d1", "label": "D", "lang": "zh-HK", "system": {},
        "engine": "pipeline", "provider": "minimax", "voice": MINIMAX_VOICE,
    }
    out = demo_loader.normalize_demo_dict(raw, validate_tools=False)
    assert out["engine"] == "pipeline"
    assert out["provider"] == "minimax"
    assert out["voice"] == MINIMAX_VOICE


def test_normalize_absent_keys_become_none():
    raw = {"id": "d2", "label": "D", "lang": "en-US", "system": {}}
    out = demo_loader.normalize_demo_dict(raw, validate_tools=False)
    assert out["engine"] is None
    assert out["provider"] is None
    assert out["voice"] is None


def test_normalize_same_shape_manifest_and_ddb_paths():
    # validate_tools True (manifest path) and False (DDB path) must produce the
    # identical engine/voice/provider shape.
    raw = {"id": "d3", "label": "D", "lang": "en-US", "system": {},
           "engine": "nova-sonic", "voice": NOVA_VOICE}
    m = demo_loader.normalize_demo_dict(raw, validate_tools=True)
    d = demo_loader.normalize_demo_dict(raw, validate_tools=False)
    for k in ("engine", "provider", "voice"):
        assert m[k] == d[k]


def test_normalize_demo_with_none_of_three_is_byte_identical_minus_new_keys():
    # Regression / backward-compat: a demo with none of the three fields must be
    # byte-identical to today APART FROM the three new keys, which are all None
    # (so downstream that omits null values is unchanged).
    raw = {
        "id": "legacy", "label": "Legacy", "lang": "zh-CN",
        "system": {"zh-CN": "x"}, "greeting": {"zh-CN": "hi"},
        "kb_body": "body", "tool_ids": [], "mcp_servers": [], "tags": [],
    }
    out = demo_loader.normalize_demo_dict(raw, validate_tools=False)
    # The only new keys are the three, all None.
    new_keys = set(out) - {
        "id", "label", "lang", "system", "greeting", "kb_intro", "kb_ack",
        "kb_body", "tool_ids", "mcp_servers", "filler", "asr_filter", "tags",
    }
    assert new_keys == {"engine", "provider", "voice", "model"}
    assert out["engine"] is None and out["provider"] is None and out["voice"] is None
    assert out["model"] is None


# =====================================================================
# _validate_engine_voice_patch — accept valid combos (AC#2)
# =====================================================================

def test_validate_empty_body_returns_empty():
    assert _validate(_body(), set()) == {}


def test_validate_valid_pipeline_combo():
    b = _body(engine="pipeline", provider="minimax", voice=MINIMAX_VOICE)
    clean = _validate(b, {"engine", "provider", "voice"})
    assert clean == {"engine": "pipeline", "provider": "minimax", "voice": MINIMAX_VOICE}


def test_validate_valid_pipeline_polly_combo():
    b = _body(engine="pipeline", provider="polly", voice=POLLY_VOICE)
    clean = _validate(b, {"engine", "provider", "voice"})
    assert clean == {"engine": "pipeline", "provider": "polly", "voice": POLLY_VOICE}


def test_validate_valid_nova_combo():
    # Re-pointed to the post-C-3 state (ruling B 2026-11-09): engine nova-sonic
    # is out of ENGINES -> every nova combo 400s at the engine check.
    b = _body(engine="nova-sonic", voice=NOVA_VOICE)
    with pytest.raises(HTTPException) as ei:
        _validate(b, {"engine", "voice"})
    assert ei.value.status_code == 400


def test_validate_nova_voice_alias_accepted():
    # Re-pointed: the alias map is emptied (C-3) and the engine retired, so a
    # former alias id no longer validates.
    b = _body(engine="nova-sonic", voice=NOVA_ALIAS)
    with pytest.raises(HTTPException) as ei:
        _validate(b, {"engine", "voice"})
    assert ei.value.status_code == 400


# =====================================================================
# _validate_engine_voice_patch — 400s (AC#2)
# =====================================================================

@pytest.mark.parametrize("body,fields,kw", [
    # engine not in ENGINES
    (_body(engine="bogus"), {"engine"}, {}),
    # provider not in TTS_PROVIDERS
    (_body(provider="bogus"), {"provider"}, {}),
    # pipeline voice not resolvable (exact) in provider table — resolver would
    # otherwise fall back to a default and silently swallow the typo.
    (_body(engine="pipeline", provider="minimax", voice="NotARealVoice"),
     {"engine", "provider", "voice"}, {}),
    # polly is case-sensitive: lowercase of a real key is rejected.
    (_body(engine="pipeline", provider="polly", voice="zhiyu"),
     {"engine", "provider", "voice"}, {}),
    # nova voice not in NOVA_SONIC_VOICES(+aliases)
    (_body(engine="nova-sonic", voice="Cantonese_GentleLady"),
     {"engine", "voice"}, {}),
])
def test_validate_invalid_values_raise_400(body, fields, kw):
    with pytest.raises(HTTPException) as ei:
        _validate(body, fields, **kw)
    assert ei.value.status_code == 400


def test_validate_nova_with_tools_accepted():
    # Re-pointed (ruling B 2026-11-09): engine nova-sonic 400s regardless of
    # tools — the tools→engine coupling itself remains absent (see the
    # pipeline+tools test below for that regression direction).
    b = _body(engine="nova-sonic", voice=NOVA_VOICE)
    with pytest.raises(HTTPException) as ei:
        _validate(b, {"engine", "voice"})
    assert ei.value.status_code == 400


def test_validate_nova_from_demo_engine_accepted():
    # Re-pointed: engine NOT in this PATCH; demo's current engine is
    # nova-sonic -> voice validated against the (C-3 emptied) nova table ->
    # no id resolves -> 400.
    b = _body(voice=NOVA_VOICE)
    with pytest.raises(HTTPException) as ei:
        _validate(b, {"voice"}, demo_engine="nova-sonic")
    assert ei.value.status_code == 400


def test_validate_pipeline_with_tools_ok():
    # pipeline + tools is fine (and tools no longer affect validation at all).
    b = _body(engine="pipeline", provider="minimax", voice=MINIMAX_VOICE)
    clean = _validate(b, {"engine", "provider", "voice"})
    assert clean["engine"] == "pipeline"


# =====================================================================
# explicit-null clears (AC#3)
# =====================================================================

def test_validate_explicit_null_clears_each_field():
    b = _body(engine=None, provider=None, voice=None)
    clean = _validate(b, {"engine", "provider", "voice"})
    assert clean == {"engine": None, "provider": None, "voice": None}


def test_validate_null_voice_clear_does_not_validate_against_engine():
    # voice=None is a clear and must NOT be rejected even against a nova-sonic
    # engine. Re-pointed data (ruling B 2026-11-09): the engine now lives on
    # the demo (a body engine=nova-sonic itself 400s post-C-3), preserving the
    # pinned rule that a clear is never validated against the effective engine.
    b = _body(voice=None)
    clean = _validate(b, {"voice"}, demo_engine="nova-sonic")
    assert clean == {"voice": None}


# =====================================================================
# effective-engine resolution (reviewer notes #2/#3)
# =====================================================================

def test_validate_voice_uses_demo_engine_when_body_omits_engine():
    # body sets only voice; demo's current engine/provider govern the table the
    # voice is validated against. Re-pointed data (ruling B 2026-11-09) to the
    # selectable pipeline engine — the demo-engine-governs rule is unchanged.
    b = _body(voice=MINIMAX_VOICE)
    clean = _validate(b, {"voice"}, demo_engine="pipeline", demo_provider="minimax")
    assert clean == {"voice": MINIMAX_VOICE}
    # a polly voice would be rejected under demo_engine=pipeline + provider=minimax.
    with pytest.raises(HTTPException):
        _validate(_body(voice=POLLY_VOICE), {"voice"},
                  demo_engine="pipeline", demo_provider="minimax")


def test_validate_voice_uses_body_engine_over_demo_engine():
    # body sets engine=nova-sonic AND a pipeline voice → effective engine is
    # nova (body wins), so the pipeline voice is rejected. (Post-C-3 the 400
    # fires even earlier — engine nova-sonic is out of ENGINES — but the
    # body-wins merge rule it pins is unchanged.)
    b = _body(engine="nova-sonic", voice=MINIMAX_VOICE)
    with pytest.raises(HTTPException):
        _validate(b, {"engine", "voice"}, demo_engine="pipeline")


def test_validate_engine_unset_lenient_accepts_nova_or_pipeline_voice():
    # Neither body nor demo sets engine → lenient: accept a voice that resolves
    # in EITHER table (real engine decided at launch). Reviewer note #3.
    assert _validate(_body(voice=MINIMAX_VOICE), {"voice"}) == {"voice": MINIMAX_VOICE}
    assert _validate(_body(voice=POLLY_VOICE), {"voice"}) == {"voice": POLLY_VOICE}
    # Re-pointed (ruling B 2026-11-09): the C-3 emptied nova table resolves no
    # id, so a former nova voice no longer passes the lenient either-table rule.
    with pytest.raises(HTTPException):
        _validate(_body(voice=NOVA_VOICE), {"voice"})


def test_validate_engine_unset_rejects_voice_in_no_table():
    with pytest.raises(HTTPException) as ei:
        _validate(_body(voice="totally-unknown-voice"), {"voice"})
    assert ei.value.status_code == 400


def test_validate_pipeline_uses_demo_provider_when_body_omits_provider():
    # engine=pipeline in body, provider NOT in body; demo's provider is polly →
    # voice validated against the polly table.
    b = _body(engine="pipeline", voice=POLLY_VOICE)
    clean = _validate(b, {"engine", "voice"}, demo_provider="polly")
    assert clean == {"engine": "pipeline", "voice": POLLY_VOICE}
    # a minimax-only voice rejected when effective provider is polly.
    with pytest.raises(HTTPException):
        _validate(_body(engine="pipeline", voice=MINIMAX_VOICE),
                  {"engine", "voice"}, demo_provider="polly")


# =====================================================================
# _content_keys wiring (AC#3) — engine/provider/voice are content
# =====================================================================

def test_demo_patch_body_has_engine_voice_provider_fields():
    b = bot.DemoPatchBody(engine="pipeline")
    assert "engine" in b.model_fields_set
    assert b.engine == "pipeline"


def test_demo_patch_body_explicit_null_is_in_model_fields_set():
    # An explicit null must be detectable as present (clears the field).
    b = bot.DemoPatchBody(voice=None)
    assert "voice" in b.model_fields_set


def test_demo_patch_body_omitted_key_not_in_model_fields_set():
    b = bot.DemoPatchBody(label="x")
    assert "engine" not in b.model_fields_set
    assert "voice" not in b.model_fields_set
    assert "provider" not in b.model_fields_set


# =====================================================================
# per-demo model (LLM) override — T1
# =====================================================================

def test_normalize_carries_model_when_present():
    raw = {
        "id": "dm", "label": "D", "lang": "zh-HK", "system": {},
        "model": GOOD_MODEL,
    }
    out = demo_loader.normalize_demo_dict(raw, validate_tools=False)
    assert out["model"] == GOOD_MODEL


def test_normalize_model_absent_becomes_none():
    raw = {"id": "dm2", "label": "D", "lang": "en-US", "system": {}}
    out = demo_loader.normalize_demo_dict(raw, validate_tools=False)
    assert out["model"] is None


def test_normalize_model_same_shape_manifest_and_ddb_paths():
    # model must round-trip identically on both the manifest (validate_tools=True)
    # and DDB (validate_tools=False) paths — the latter is what prod read/write uses.
    raw = {"id": "dm3", "label": "D", "lang": "en-US", "system": {}, "model": GOOD_MODEL}
    m = demo_loader.normalize_demo_dict(raw, validate_tools=True)
    d = demo_loader.normalize_demo_dict(raw, validate_tools=False)
    assert m["model"] == d["model"] == GOOD_MODEL


def test_validate_valid_model_accepted():
    b = _body(model=GOOD_MODEL)
    clean = _validate(b, {"model"})
    assert clean == {"model": GOOD_MODEL}


def test_validate_invalid_model_raises_400():
    b = _body(model=BAD_MODEL)
    with pytest.raises(HTTPException) as ei:
        _validate(b, {"model"})
    assert ei.value.status_code == 400
    assert "invalid model" in ei.value.detail


def test_validate_explicit_null_model_clears():
    # model=None is a clear (inherit DEFAULT_MODEL at launch) — recorded as None.
    b = _body(model=None)
    clean = _validate(b, {"model"})
    assert clean == {"model": None}


def test_validate_model_alongside_engine_voice():
    # model coexists with engine/provider/voice in one PATCH.
    b = _body(engine="pipeline", provider="minimax", voice=MINIMAX_VOICE, model=GOOD_MODEL)
    clean = _validate(b, {"engine", "provider", "voice", "model"})
    assert clean == {
        "engine": "pipeline", "provider": "minimax",
        "voice": MINIMAX_VOICE, "model": GOOD_MODEL,
    }


def test_validate_model_omitted_not_in_clean():
    # model not in fields → not in clean (untouched).
    b = _body(engine="pipeline", provider="minimax", voice=MINIMAX_VOICE)
    clean = _validate(b, {"engine", "provider", "voice"})
    assert "model" not in clean


def test_demo_patch_body_has_model_field():
    b = bot.DemoPatchBody(model=GOOD_MODEL)
    assert "model" in b.model_fields_set
    assert b.model == GOOD_MODEL


def test_demo_patch_body_explicit_null_model_in_fields_set():
    b = bot.DemoPatchBody(model=None)
    assert "model" in b.model_fields_set


def test_demo_patch_body_omitted_model_not_in_fields_set():
    b = bot.DemoPatchBody(label="x")
    assert "model" not in b.model_fields_set
