"""Nova Sonic voice table tests for bot.py (task b4f4f827 — T4).

Hermetic (no network, no boto3 calls): only inspects the module-level
``NOVA_SONIC_VOICES`` table, the ``NOVA_SONIC_VOICE_ALIASES`` migration map,
and the alias+fallback resolution logic shared by the two voice-resolution
points (``_build_nova_sonic_pipeline`` and the ``/ws`` entry).

Assertions re-pointed to the post-C-3 state (decision (a), team-lead ruling B
2026-11-09): 空表 (table emptied) / 無 alias (alias map emptied) / 默認值
一致性 (DEFAULT_NOVA_SONIC_VOICE invalidated to ""). Test names are kept from
the roster era on purpose ("只把斷言重指"); this file's retirement stays with
U-501 per decision (a).
"""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture
def bot_mod(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "x")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pwd")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    for mod in list(sys.modules):
        if mod in ("bot", "runtime_config", "demo_loader", "mcp_config"):
            del sys.modules[mod]
    return importlib.import_module("bot")


# Pre-C-3 expectation was the official AWS Nova 2 Sonic 16-voice roster
# (sonic-language-support.html). C-3 empties it — the roster is in git history
# (restore together with ENGINES for the AWS rollback path).
EXPECTED_VOICES: set[str] = set()
REQUIRED_FIELDS = {"label", "gender", "locale", "lang_label"}


def _resolve(bot_mod, voice):
    """Mirror the alias-then-fallback logic used at both resolution points."""
    voice = bot_mod.NOVA_SONIC_VOICE_ALIASES.get(voice, voice)
    return voice if voice in bot_mod.NOVA_SONIC_VOICES else bot_mod.DEFAULT_NOVA_SONIC_VOICE


def test_all_16_voices_present(bot_mod):
    # 空表: name kept from the roster era; the roster retired with C-3.
    assert set(bot_mod.NOVA_SONIC_VOICES) == EXPECTED_VOICES
    assert len(bot_mod.NOVA_SONIC_VOICES) == 0


def test_each_voice_has_required_fields(bot_mod):
    # Vacuous while the table is empty; the shape invariant stays pinned for
    # the AWS-rollback restore (and any custom rows' contract).
    assert bot_mod.NOVA_SONIC_VOICES == {}
    for vid, v in bot_mod.NOVA_SONIC_VOICES.items():
        missing = REQUIRED_FIELDS - set(v)
        assert not missing, f"{vid} missing fields: {missing}"
        assert v["gender"] in ("F", "M"), vid
        assert "-" in v["locale"], vid
        assert v["label"], vid


def test_polyglot_flags(bot_mod):
    # 空表: tiffany/matthew polyglot rows retired with the roster.
    assert bot_mod.NOVA_SONIC_VOICES == {}


def test_kiara_arjun_dual_lang_label(bot_mod):
    # 空表: kiara/arjun dual-lang rows retired with the roster.
    assert bot_mod.NOVA_SONIC_VOICES == {}


def test_alias_map_resolves_old_ids(bot_mod):
    # 無 alias: the migration map is emptied alongside the roster (C-3) — an
    # alias pointing at a ghost voice id would be dangling.
    assert bot_mod.NOVA_SONIC_VOICE_ALIASES == {}


def test_aliases_point_at_real_voices(bot_mod):
    # Vacuous no-dangle invariant kept (alias targets must stay real voices).
    for old, new in bot_mod.NOVA_SONIC_VOICE_ALIASES.items():
        assert new in bot_mod.NOVA_SONIC_VOICES, f"alias {old}->{new} dangling"
        assert old not in bot_mod.NOVA_SONIC_VOICES, f"alias {old} should be retired"


def test_resolution_aliased(bot_mod):
    # Aliases are gone -> every former alias id falls back to the default.
    assert _resolve(bot_mod, "marie") == bot_mod.DEFAULT_NOVA_SONIC_VOICE
    assert _resolve(bot_mod, "sofia") == bot_mod.DEFAULT_NOVA_SONIC_VOICE
    assert _resolve(bot_mod, "ana") == bot_mod.DEFAULT_NOVA_SONIC_VOICE


def test_resolution_valid_passthrough(bot_mod):
    # Table emptied -> no id passes through; everything falls back.
    assert _resolve(bot_mod, "lorenzo") == bot_mod.DEFAULT_NOVA_SONIC_VOICE
    assert _resolve(bot_mod, "tiffany") == bot_mod.DEFAULT_NOVA_SONIC_VOICE


def test_resolution_unknown_falls_back_to_default(bot_mod):
    # 默認值一致性: the default is invalidated alongside the emptied table and
    # remains what unknown ids resolve to (both resolution points unchanged).
    assert bot_mod.DEFAULT_NOVA_SONIC_VOICE == ""
    assert bot_mod.DEFAULT_NOVA_SONIC_VOICE not in bot_mod.NOVA_SONIC_VOICES
    assert _resolve(bot_mod, "does-not-exist") == bot_mod.DEFAULT_NOVA_SONIC_VOICE
    # An old id that is neither valid nor aliased also falls back.
    assert _resolve(bot_mod, "greta") == bot_mod.DEFAULT_NOVA_SONIC_VOICE
