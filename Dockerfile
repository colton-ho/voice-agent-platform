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
#
# ── Why the test-only deps (pytest / pytest-asyncio / moto / httpx) stay ──
# requirements.txt is a SINGLE declared set shared by the dev venv, the frozen
# AWS deploy line and this image. Measured in a clean CPython 3.13 venv they are
# 42.7 MB of 733 MB site-packages (5.8%), dominated by moto (38.5 MB). Splitting
# a runtime-only file was rejected deliberately: dev/prod dependency DIVERGENCE
# is the exact failure class that produced the CR-C1 Speech defect below — the
# clean-room image simulation only caught the missing
# `azure-cognitiveservices-speech` because it installed the same declared set the
# tests use. Paying 5.8% for parity is the cheaper trade. Revisit only if the
# image size becomes a provisioning constraint.
#
# ── Why the admin SPA is a build stage (not a copied artifact) ──
# `static/admin/dist` is a gitignored Vite build output and is absent from the
# repo, so a plain `COPY static/` ships an image whose "/" route 404s
# (bot.py:7929-7934 warns "admin SPA dist not found"). Stage 1 compiles it.
# Verified on Node 22: `npm ci && npm run build` => dist/index.html + 1.6 MB of
# hashed assets in ~4 s. Base is `node:22-slim` (Debian/glibc) ON PURPOSE, not
# alpine: the lockfile carries @rollup/rollup-linux-x64-gnu AND
# @esbuild/linux-x64 but NO musl variant of the esbuild binary, so `npm ci` on
# musl would fall back to esbuild's postinstall download (or fail outright).
# glibc makes the install deterministic from the lock alone. The build stage's
# size does not reach the final image — stage 2 only receives `dist/`.
# The build FAILS the image if vite does not emit index.html, so a missing admin
# UI can never again ship silently as a 404.

# ── Stage 1: admin SPA (Vue 3 + Vite) ────────────────────────────────────────
# package files first so the npm layer caches independently of src edits.
FROM node:22-slim AS spa

WORKDIR /spa

COPY static/admin/package.json static/admin/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY static/admin/ ./
# `test -f` turns a silently-empty dist into a hard build failure.
RUN npm run build && test -f dist/index.html


# ── Stage 2: runtime ─────────────────────────────────────────────────────────
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
#   static/     legacy index + SPA SOURCE. The SPA's built bytes come from
#               stage 1 below, never from the repo (dist/ is gitignored), so
#               "/" cannot mount with a missing dist. ADMIN_DIST_DIR =
#               static/admin/dist (bot.py:3188, mounted bot.py:7929).
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
# The compiled SPA from stage 1. Must come AFTER `COPY static/` so it wins, and
# static/admin/dist is excluded from the build context (.dockerignore) so there
# is exactly one source of truth for those bytes — the Node build.
COPY --from=spa /spa/dist ./static/admin/dist
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
