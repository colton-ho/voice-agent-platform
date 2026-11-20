# syntax=docker/dockerfile:1
# U-402 G-1 — runtime image for the Azure voicebot: `python bot.py` serving
# :7860 (bot.py:7605 argparse defaults: host 0.0.0.0, port 7860). Built by
# `az acr build` with the repo root as context (.github/workflows/release.yml
# `build` job). See docs/azure/ci-cd-pipeline.md §6 (G-1 resolution).
#
# ── Dependency install strategy (chosen: requirements.txt + lock as constraint) ──
# requirements.lock is a `pip freeze` of the dev venv (exact resolved set) but
# is STALE as of this image: last regenerated 2026-11-09 and missing the three
# pinned runtime deps added afterwards — azure-cosmos==4.17.1 /
# azure-servicebus==7.15.0 (U-304) and twilio==9.13.0 (U-201), all three
# imported at bot.py import time. Installing the lock alone yields a broken
# image; installing requirements.txt alone floats unpinned entries.
# So: install the DECLARED set from requirements.txt with the lock applied as
# an exact-version constraint (-c). Every entry that exists in the lock resolves
# to its frozen version; only the 3 post-lock deps resolve at their explicit
# requirements.txt pins. `pip check` asserts coherent deps afterwards.
# Remaining known drift surface: transitives of those 3 packages that do not
# appear in the lock at all (documented as a gap in ci-cd-pipeline.md §6).
#
# ── Why not `pip install -r requirements.lock` alone ── (kept for the record):
# it omits azure-cosmos/azure-servicebus/twilio entirely (see above).

FROM python:3.13-slim

# No app config is baked in. All 30 config_contract placeholders are runtime
# env from Container Apps + Key Vault secretRef (U-401); see the ENV note at
# the bottom.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first for layer caching. Only the two requirements files are
# copied in this layer, so code edits don't invalidate the pip layer.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock \
    && pip check

# Runtime tree — exactly what bot.py imports or reads at startup:
#   *.py        all top-level app modules (bot.py + its flat imports:
#               asr_filter, asr_multilang, timeout_filler, tool_filler,
#               demo_store, demo_loader, voice_store, activity_store,
#               cascade_engine, runtime_config, mcp_config, mcp_sigv4,
#               twilio_sig, latency_observer, latency_log, user_store,
#               call_control_tools, config_contract, …). The glob keeps the
#               image complete as bot.py (owned by another teammate) evolves.
#   providers/  storage/  tools/  workers/   imported packages
#   config/     runtime.json + mcp_servers.json fallback seeds (no secrets;
#               UI/behavior prefs only — env is authoritative per U-401)
#   static/     admin SPA + legacy index (static/admin/dist mounts at "/"
#               when built; absent dist degrades to a warning, bot.py:7595)
#   data/       demo manifests data/<id>/manifest.yaml (DATA_ROOT, bot.py:128)
# Deliberately NOT copied (see .dockerignore): vendored ./pipecat (1.1.x
# reference tree — would shadow the installed pipecat-ai 1.4.0 at import),
# tests/, voice-server/ (legacy Node SIP app), infra/, docs/, scripts/,
# deploy/, .venv/, smoke_*.py (dev-only AWS probes).
COPY *.py ./
COPY providers/ ./providers/
COPY storage/ ./storage/
COPY tools/ ./tools/
COPY workers/ ./workers/
COPY config/ ./config/
COPY static/ ./static/
COPY data/ ./data/

# Non-root runtime user. /app is owned by it because the admin API writes
# config/runtime.json in place (bot.py:2804) at runtime.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Container Apps targetPort 7860 (infra/bicep/main.bicep probePath
# /api/auth/setup-status hits this port).
EXPOSE 7860

# Defaults: uvicorn on 0.0.0.0:7860 (bot.py:7605-7610). No secrets/env here —
# runtime config is injected by Container Apps env + Key Vault secretRef.
CMD ["python", "bot.py"]
