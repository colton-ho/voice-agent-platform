"""Dependency guard for the pipeline engine's Azure Speech STT.

Why this exists: `_build_pipeline` (bot.py) lazily imports
`pipecat.services.azure.stt.AzureSTTService`. A lazy import is invisible to
`import bot`, so the whole suite stayed green while
`azure-cognitiveservices-speech` was missing from requirements.txt AND from
.venv — the first real phone call would have died with ImportError (found
2026-11-21 while reproducing the image's dependency layer in a clean venv).

These tests fail loudly if that import, the constructor shape used by bot.py,
or the requirements declaration regresses.
"""
import inspect
import re
from pathlib import Path

import pytest


def test_azure_stt_module_imports():
    """pipecat's Azure STT module needs the Azure Speech SDK (pipecat `azure` extra)."""
    from pipecat.services.azure.stt import AzureSTTService

    assert AzureSTTService is not None


def test_azure_stt_constructor_shape_matches_bot_call_site():
    """bot.py constructs it with api_key/region/private_endpoint/sample_rate/settings.

    Pinned here so a pipecat upgrade that renames these kwargs fails in CI rather
    than on the first live call.
    """
    from pipecat.services.azure.stt import AzureSTTService

    params = inspect.signature(AzureSTTService.__init__).parameters
    for kw in ("api_key", "region", "private_endpoint", "sample_rate", "settings"):
        assert kw in params, f"AzureSTTService lost kwarg {kw!r} (bot.py call site)"


@pytest.mark.parametrize("language", ["zh-HK", "ja-JP", "en-US"])
def test_azure_stt_constructs_for_business_languages(language):
    """Construction (no network) must succeed for the A-3 acceptance languages."""
    from pipecat.services.azure.stt import AzureSTTService

    stt = AzureSTTService(
        api_key="not-a-real-key",
        region="eastus",
        private_endpoint=None,
        sample_rate=8000,
        settings=AzureSTTService.Settings(language=language),
    )
    assert isinstance(stt, AzureSTTService)


def test_speech_sdk_declared_in_requirements():
    """The SDK must stay declared — the lazy import makes its absence invisible."""
    req = Path(__file__).resolve().parent.parent / "requirements.txt"
    text = req.read_text(encoding="utf-8")
    matches = re.findall(r"^azure-cognitiveservices-speech[=~<>!]=?.*$", text, re.M)
    assert matches, "azure-cognitiveservices-speech missing from requirements.txt"
    assert any("==" in m for m in matches), "speech SDK must be version-pinned"


def test_bot_build_pipeline_uses_azure_stt():
    """Ties the guard to the real call site (fails if the pipeline switches back)."""
    bot_src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(encoding="utf-8")
    assert "from pipecat.services.azure.stt import AzureSTTService" in bot_src
    assert "AzureSTTService(" in bot_src
