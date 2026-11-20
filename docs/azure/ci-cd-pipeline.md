# U-402 · CI/CD Pipeline Orchestration

Scope: `docs/azure/implementation-plan.md:423` U-402 — the spec §12 CI/CD sequence as
GitHub Actions. Artifacts: `.github/workflows/ci.yml` (gate), `.github/workflows/release.yml`
(release), one infra delta (`infra/bicep/modules/containerApps.bicep` revision mode, P1 of
`docs/azure/blue-green-release.md` §5). No cloud execution happened from this task; everything
below is validated offline only (see §6 gap list).

## 1. §12 sequence → workflow job mapping

| §12 step | Workflow job / step | Notes |
|---|---|---|
| 1. Lint/type-check + unit/contract tests | `ci.yml / lint-test` (reused by `release.yml / gate`) | `python -m compileall` lint stand-in (G-4) + full `pytest tests/` |
| 2. Scan dependencies, secrets, IaC, image | `ci.yml / scan` (dep/secret/IaC) + `release.yml / build` (image) | Trivy; image lane runs post-build — no image exists earlier |
| 3. Build once; identify by digest | `release.yml / build` | `az acr build` → resolve `sha256` digest → **promotion key** |
| 4. Bicep validation/what-if vs target env | `ci.yml / lint-test` (compile) + `release.yml / whatif-staging` + `release-prod` step 2 | what-if = `deploy-azure.sh` default mode (read-only) |
| 5. Deploy staging + smoke | `release.yml / deploy-staging`, `smoke-staging` | storage/identity/tool smoke = probe + signature-rejection; **real telephone smoke is manual (G-2)** and gates the prod approval |
| 6. Require production approval | `release-prod` job's `environment: prod` | GitHub Environment **required reviewers** — approving the deployment IS the gate |
| 7. Green deploy + readiness + routing switch | `release-prod` steps 5–10 | orchestrates `blue-green-release.md` §2.1/§8 exactly |
| 8. Drain old runtime + monitor | `release-prod` steps 11–14 | `drain-runtime.sh` (FC-7 census) + watch window + retire; regression → §7 rollback |

## 2. Auth model — OIDC, no stored Azure secrets

- `azure/login@v2` with federated credentials only (`id-token: write`; no SP password/secret
  exists anywhere in GitHub or the repo). Client/tenant/subscription IDs are GitHub
  **Environment variables** (`AZURE_CLIENT_ID`/`AZURE_TENANT_ID`/`AZURE_SUBSCRIPTION_ID`) on
  `staging` and `prod` — they are identifiers, not secrets.
- One-time manual setup (not a repo artifact): Entra app registration + federated credential
  per environment, subject `repo:<owner>/<repo>:environment:staging` / `…:environment:prod`;
  GitHub Environments `staging` and `prod`, with **required reviewers on `prod`** (step 6).
  RBAC: the CI principals need ACR push/pull (staging), `containerapp` contributor-ish on
  `rg-voicebot-prod` (revision/traffic ops), read on the RGs for what-if, and **Cosmos DB
  Built-in Data Contributor** on `cosmos-voicebot-prod` (drain census, blue-green-release.md
  P3), Key Vault Secrets User on `kv-voicebot-staging` (secret resolution for deploy).
- App secrets (`MINIMAX_API_KEY`, `AUTH_SECRET`, `TWILIO_AUTH_TOKEN`, `TWILIO_ACCOUNT_SID`)
  live **only in Key Vault** (the runtime reads the same KV via MI references);
  `deploy-staging` fetches them from `kv-voicebot-staging` at run time and registers
  `::add-mask::`. Fresh-environment secret seeding stays manual (`set-secrets.sh`).

## 3. Build once, promote by digest

`build` produces `acrvinebotstaging.azurecr.io/voicebot:<git-sha>` **once** and resolves its
`sha256` digest. Every later gate consumes `…/voicebot@sha256:…`:
staging deploy, then `az acr import` into `acrvinebotprod` (digest-preserving; the workflow
asserts `imported digest == built digest`), then the green revision. No step rebuilds. The
digest is materialized into `infra/bicep/params/*.bicepparam` (`containerImage`) in the
ephemeral runner checkout before `deploy-azure.sh` runs — the script's REPLACE_ME/secret
gates stay intact.

## 4. Blue/green orchestration (matches blue-green-release.md §8 row-for-row)

Green deploy = pin traffic to blue (`ingress traffic set --revision-weight <blue>=100`) →
`az containerapp update --image …@<digest> --revision-suffix g<sha>` creates green **at 0%**
→ readiness: probe `https://<green-revision-fqdn>/api/auth/setup-status` until 200 (D1) →
**the only traffic mutation**: `--revision-weight <green>=100 <blue>=0` → drain blue via
`scripts/deploy/drain-runtime.sh prod <blue> --apply --deadline …` (drain-complete = zero
FC-7 leases; no retire yet) → watch window (the rollback runway) → retire blue via
`--retire-when-drained`.

- A watch **regression** flips new calls back to blue (§7) and leaves green draining at 0%.
- Drain exit 2 (deadline with live leases) fails the run **without** rollback — forced-drain
  is a human decision (§6; the tool prints the A–D options table). Exit 3 likewise.
- The worker rolls as an ordinary revision (no media endpoint; U-304 jobs are idempotent).
- Infra delta (P1): `containerApps.bicep` `activeRevisionsMode` is now
  `s.httpIngress ? 'Multiple' : 'Single'` — Multiple is what makes revision
  traffic-splitting possible at all; the worker keeps Single (no ingress, `minReplicas=1`,
  Multiple would hold every old revision alive). The `traffic` array stays
  `[{latestRevision:true, weight:100}]` as steady-state bootstrap: Bicep cannot name future
  revisions, so the hold-at-0% pinning happens in the pipeline (step 5 of `release-prod`),
  exactly where the green revision is created.

## 5. Run model / authorization

`release.yml` is `workflow_dispatch`-only with `concurrency: release` (never cancel
mid-flight). Prod steps run solely behind the `prod` environment gate. The workflow applies
**no** prod infrastructure (prod what-if is read-only; the green runtime is a new revision).
Per the repo iron rule, the first real run requires explicit user authorization.

## 6. Honest gap list — what needs real-GitHub validation

| # | Gap | Status |
|---|---|---|
| G-1 | **Runtime image Dockerfile** (`python bot.py`, port 7860; `voice-server/Dockerfile` is the legacy Node SIP app — not this image). **RESOLVED 2026-11-20**: root `Dockerfile` + `.dockerignore` landed (G-1 card). `python:3.13-slim`; deps = `requirements.txt` with `requirements.lock` as exact-version constraint (the lock alone is stale — see G-9); explicit runtime-only COPY (flat `*.py`, `providers/ storage/ tools/ workers/ config/ static/ data/`); vendored `./pipecat` excluded so it can't shadow pip's pipecat-ai; non-root `appuser`; no baked env/secrets. | resolved |
| G-2 | **Real telephone smoke** (spec §12 step 5) cannot run in Actions: needs the staging Twilio number + PSTN calls in zh-HK/zh-CN/en-US/ja-JP. Checklist printed by `smoke-staging`; results gate the prod approval. | manual |
| G-3 | ARM/CLI query paths used by `release-prod` (`properties.latestRevisionName`, revision `properties.fqdn`) and `.bicepparam` + inline `containerImage` override behavior need confirmation on the first authenticated run against a real app. | verify-on-first-run |
| G-4 | No ruff/mypy config in the repo — `compileall` is the lint stand-in. Adopt a linter config later and extend `ci.yml`. | known limitation |
| G-5 | Actions are pinned to major/immutable tags (`azure/login@v2`, `trivy-action@0.28.0`); pin to full commit SHAs when the repo standardizes. `actionlint` unavailable in the offline validation env — run it once in CI. | known limitation |
| G-6 | After a green release, `params/prod.bicepparam` `containerImage` is stale (the green path uses `az containerapp update`, not bicep). A follow-up commit should pin it to `…@<digest>`; until then, never run `deploy-azure.sh prod --apply` during/after a release window (blue-green-release.md P1 rule). | operational rule |
| G-7 | Full `pytest tests/` as the CI gate assumes the historical "2 pre-existing failures" are fixed (task #62). If any remain, the gate is red by design until resolved. | verify-on-first-run |
| G-8 | Offline validation done here: YAML parse of both workflows (2 + 6 jobs), `az bicep build` of `main.bicep` + all 10 modules (with the P1 delta), `bicep build-params` of all 3 `.bicepparam` files — all clean. Workflows themselves have never executed; no claim is made beyond parse/compile validity. | done |
| G-9 | **Runtime image — first real build not yet run** (no Docker daemon in the authoring env). Partly de-risked offline: the dependency layer was reproduced in a clean CPython 3.13 venv (`pip install -r requirements.txt -c requirements.lock` → "No broken requirements found", 733 MB site-packages, 117 dists) and `import bot` succeeds there in 1.6 s, which confirms (a) no sdist-only pin blocks `python:3.13-slim` and needs no `build-essential`. Still open: (b) `requirements.lock` is stale (regenerated 2026-11-09; missing `azure-cosmos==4.17.1`, `azure-servicebus==7.15.0`, `twilio==9.13.0`) — used as a pip constraint file so only those 3 (+ any transitives absent from the lock) can drift from the frozen set; regenerate the lock in CI to close this. Validate the first `az acr build` / `docker build .` before relying on the green path. | verify-on-first-build |
| G-10 | **Admin SPA is now a build stage** (was a silent 404). `static/admin/dist` is gitignored Vite output absent from the repo, so the pre-existing `COPY static/` shipped an image whose `/` returned 404 (`bot.py:7929-7934` only warns). The Dockerfile now has `FROM node:22-slim AS spa` → `npm ci && npm run build && test -f dist/index.html`, copied in after `COPY static/`. De-risked offline on local Node 22.22.2: `npm ci` + `npm run build` → `dist/index.html` + 1.6 MB hashed assets in ~4 s, and with `dist` present `bot.app` mounts the SPA at `/` (verified: route name `spa` present). glibc base is required, not cosmetic — the lock records `@esbuild/linux-x64` but no musl variant. Still open: the `node:22-slim` layer has never actually run (no daemon); assert the first `az acr build` reaches the Vite step. | verify-on-first-build |
