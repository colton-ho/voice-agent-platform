"""U-402 G-1 — contract tests for the runtime image (Dockerfile + .dockerignore).

There is no Docker daemon in this environment, so the image cannot be built in
CI here. These tests are the substitute: they assert the structural invariants
whose violation is *only* observable at container run time — which is exactly
how two defects reached this branch unnoticed:

1. `azure-cognitiveservices-speech` was missing from requirements.txt. The lazy
   import inside `_build_pipeline` hid it from the whole pytest suite, so the
   first real phone call would have died at STT construction. The dependency
   layer is now reproduced in a clean venv (see
   tests/test_azure_speech_import.py) and pinned here.
2. `static/admin/dist` is gitignored Vite output that was absent from the repo,
   so the image shipped without the admin SPA and "/" 404'd at boot
   (bot.py:7929-7934 degrades to a warning instead of failing). Stage 1 of the
   Dockerfile now compiles it and hard-fails if vite emits nothing.

The tests are pure text analysis of the two files; they add no build time.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
BOT_PY = REPO_ROOT / "bot.py"

# Every instruction Docker/BuildKit accepts. Used to catch a typo'd keyword,
# which the daemon would only report at build time (unavailable here).
_KNOWN_INSTRUCTIONS = {
    "ADD", "ARG", "CMD", "COPY", "ENTRYPOINT", "ENV", "EXPOSE", "FROM",
    "HEALTHCHECK", "LABEL", "MAINTAINER", "ONBUILD", "RUN", "SHELL",
    "STOPSIGNAL", "USER", "VOLUME", "WORKDIR",
}


def _logical_lines(text: str) -> list[str]:
    """Dockerfile logical lines: comments dropped, backslash continuations joined."""
    out: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not buf and line.lstrip().startswith("#"):
            continue
        if not line.strip():
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        out.append(buf.strip())
        buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    assert DOCKERFILE.is_file(), f"runtime image definition missing: {DOCKERFILE}"
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def instructions(dockerfile_text: str) -> list[str]:
    return _logical_lines(dockerfile_text)


# --------------------------------------------------------------------------
# Structural validity — the part `docker build` would have told us
# --------------------------------------------------------------------------


def test_every_instruction_uses_a_known_keyword(instructions: list[str]) -> None:
    for line in instructions:
        keyword = line.split(None, 1)[0].upper()
        assert keyword in _KNOWN_INSTRUCTIONS, f"unknown Dockerfile instruction: {line!r}"


def test_every_copy_from_references_a_declared_stage(instructions: list[str]) -> None:
    stages = set()
    for line in instructions:
        m = re.match(r"FROM\s+\S+(?:\s+AS\s+(\S+))?$", line, re.IGNORECASE)
        if m and m.group(1):
            stages.add(m.group(1).lower())

    assert stages, "Dockerfile declares no named build stage"

    for line in instructions:
        m = re.match(r"COPY\s+--from=(\S+)\s", line, re.IGNORECASE)
        if not m:
            continue
        source = m.group(1).lower()
        # `--from=<n>` may also address a stage by index.
        assert source in stages or source.isdigit(), (
            f"COPY --from={m.group(1)} references no declared stage {sorted(stages)}"
        )


def test_runtime_stage_is_last(instructions: list[str]) -> None:
    """The final stage is what ships; it must be the python runtime, not the SPA."""
    last_from = [ln for ln in instructions if ln.upper().startswith("FROM ")][-1]
    assert "python:3.13-slim" in last_from, last_from


# --------------------------------------------------------------------------
# Dependency layer — defect 1 (the missing Speech SDK)
# --------------------------------------------------------------------------


def test_dependency_layer_installs_declared_set_with_lock_constraint(
    instructions: list[str],
) -> None:
    installs = [ln for ln in instructions if "pip install" in ln]
    assert len(installs) == 1, f"expected one pip install layer, got {installs}"
    install = installs[0]
    assert "-r requirements.txt" in install
    # The lock is a STALE freeze (missing azure-cosmos/azure-servicebus/twilio),
    # so it may only be used as a constraint, never as the install set.
    assert "-c requirements.lock" in install
    assert "pip check" in install, "dependency conflicts must fail the image build"


def test_requirements_declare_the_azure_speech_sdk() -> None:
    """Guards defect 1 at its origin: the SDK must be a declared runtime dep."""
    req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    declared = [
        ln for ln in req.splitlines()
        if ln.strip().startswith("azure-cognitiveservices-speech")
    ]
    assert declared, (
        "azure-cognitiveservices-speech must be declared: pipecat's AzureSTTService "
        "imports it lazily, so no test fails when it is absent"
    )
    assert re.search(r"==\s*\d", declared[0]), (
        f"the Speech SDK must be version-pinned, got {declared[0]!r}"
    )


# --------------------------------------------------------------------------
# Admin SPA — defect 2 (the 404 at "/")
# --------------------------------------------------------------------------


def test_admin_spa_is_compiled_in_a_node_stage(instructions: list[str]) -> None:
    node_stages = [
        ln for ln in instructions
        if ln.upper().startswith("FROM ") and ln.split()[1].lower().startswith("node:")
    ]
    assert node_stages, "no Node build stage: static/admin/dist is gitignored, so it must be built"

    assert any("npm ci" in ln for ln in instructions), "SPA deps must be installed reproducibly via npm ci"
    build = [ln for ln in instructions if "npm run build" in ln]
    assert build, "the SPA must be built inside the image"
    # A vite run that emits nothing must fail the build instead of shipping a
    # container whose "/" 404s.
    assert "test -f dist/index.html" in build[0], (
        "the SPA build step must assert dist/index.html exists"
    )


def test_spa_build_stage_uses_a_glibc_base_matching_the_lockfile(
    instructions: list[str],
) -> None:
    """Cross-file invariant: the Node base must match the native binaries in the lock.

    `npm ci` installs ONLY the optional platform packages recorded in
    package-lock.json. The lock has @esbuild/linux-x64 (glibc) and no musl
    variant, so an alpine/musl base would fall through to esbuild's postinstall
    download (or fail the build). Keep the base glibc while that holds.
    """
    node_from = next(
        (ln for ln in instructions
         if ln.upper().startswith("FROM ") and ln.split()[1].lower().startswith("node:")),
        None,
    )
    assert node_from is not None, "no Node stage"

    lock = (REPO_ROOT / "static" / "admin" / "package-lock.json").read_text(encoding="utf-8")
    has_musl_esbuild = "node_modules/@esbuild/linux-x64-musl" in lock
    has_gnu_esbuild = "node_modules/@esbuild/linux-x64\"" in lock

    if not has_musl_esbuild and has_gnu_esbuild:
        assert "alpine" not in node_from.lower(), (
            f"{node_from!r} is musl, but package-lock.json only records the glibc "
            "esbuild binary (@esbuild/linux-x64) — npm ci cannot satisfy it on Alpine. "
            "Either regenerate the lock on musl or keep a glibc base."
        )


def test_spa_dist_is_copied_from_the_build_stage_after_static(
    instructions: list[str],
) -> None:
    """`COPY static/` would clobber nothing only if the dist copy comes after it."""
    static_idx = next(
        (i for i, ln in enumerate(instructions)
         if re.match(r"COPY\s+static/\s+\./static/\s*$", ln)),
        None,
    )
    dist_idx = next(
        (i for i, ln in enumerate(instructions)
         if re.match(r"COPY\s+--from=\S+\s+/spa/dist\s+", ln)),
        None,
    )
    assert static_idx is not None, "COPY static/ ./static/ not found"
    assert dist_idx is not None, "the compiled SPA is never copied into the runtime stage"
    assert dist_idx > static_idx, (
        "COPY --from=<spa> ...static/admin/dist must come AFTER COPY static/, "
        "otherwise the generated bytes are shadowed"
    )


def test_spa_dist_copy_target_matches_the_served_directory() -> None:
    """The COPY destination must equal the path the app mounts at "/"."""
    bot_src = BOT_PY.read_text(encoding="utf-8")
    m = re.search(
        r'^ADMIN_DIST_DIR\s*=\s*os\.path\.join\(\s*STATIC_DIR\s*,\s*"admin"\s*,\s*"dist"\s*\)',
        bot_src,
        re.MULTILINE,
    )
    assert m, "ADMIN_DIST_DIR no longer resolves to static/admin/dist — update the Dockerfile"
    assert "COPY --from=spa /spa/dist ./static/admin/dist" in DOCKERFILE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Runtime posture + build context
# --------------------------------------------------------------------------


def test_container_runs_non_root_on_the_expected_port(instructions: list[str]) -> None:
    users = [ln for ln in instructions if ln.upper().startswith("USER ")]
    assert users, "container would run as root"
    assert "appuser" in users[-1] and "root" not in users[-1].split()[-1]

    assert any("EXPOSE 7860" in ln for ln in instructions), (
        "Container Apps targetPort is 7860"
    )
    cmd = [ln for ln in instructions if ln.upper().startswith("CMD ")]
    assert cmd and "bot.py" in cmd[-1], f"unexpected CMD: {cmd}"


def test_no_credentials_are_baked_into_the_image(dockerfile_text: str) -> None:
    """All 30 config placeholders arrive at run time via Container Apps env."""
    for needle in ("AUTH_SECRET=", "SPEECH_KEY=", "MINIMAX_API_KEY=", "TWILIO_AUTH_TOKEN="):
        assert needle not in dockerfile_text, (
            f"{needle} appears in the Dockerfile — secrets belong in Key Vault secretRef"
        )


def test_dockerignore_excludes_trees_that_must_never_ship() -> None:
    assert DOCKERIGNORE.is_file(), ".dockerignore missing: build context would include .venv"
    entries = {
        ln.strip() for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    }
    # vendored pipecat/ would shadow the installed pipecat-ai 1.4.0 at import
    for required in (".venv", "pipecat", "tests", "**/node_modules", "static/admin/dist"):
        assert required in entries, (
            f".dockerignore must exclude {required!r}; entries={sorted(entries)}"
        )


def test_dockerignore_exclude_rules_are_not_negated_by_later_rules() -> None:
    """A later `!pattern` re-includes a path; that must never happen for secrets."""
    patterns = [
        ln.strip() for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    negations = [p for p in patterns if p.startswith("!")]
    allowed = {"!.env.example"}
    assert set(negations) <= allowed, f"unexpected re-inclusion: {set(negations) - allowed}"
