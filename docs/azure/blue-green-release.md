# U-405 · Blue/Green Media-Endpoint Releases

Author: team-lead · Date: 2026-11-20 · Status: **scheme + tooling delivered (doc-first; no cloud execution)**
Scope: `docs/azure/implementation-plan.md:426` U-405 — "new-calls-to-green / old-calls-drain scheme, retained old capacity until drained or approved deadline, forced-drain behaviour documented"; closes **R-A-016** (`docs/azure/requirements.md:54`) and **R-T-010** (`docs/azure/requirements.md:108`). Deps consumed: U-305 (FC-7 leases, `storage/ownership.py`), U-401 (topology, `infra/bicep/`, `scripts/deploy/`). Downstream: **U-402** orchestrates this scheme in CI/CD; **U-406** builds operator runbooks on §6/§9.

**Spec anchor (§8.3, `docs/Azure_Voicebot_Implementation_Instructions_Corrected.md:248`):**
> "Use a tested blue/green media-endpoint scheme for releases: route new calls to the new deployment while existing calls continue on the old one until drained. Retain old deployment capacity until no active calls remain or an approved drain deadline is reached. **Do not describe ordinary revision switching as automatically non-disruptive.**"

That last sentence is a hard constraint on this document: §5/§6 state exactly which part of the scheme is guaranteed by the platform, which part is precondition, and what the caller experiences when something is torn down anyway. Related spec lines: `:245` (shutdown: stop admitting, mark draining, clean up leases), `:246` ("shutdown grace does not guarantee every call completes"), `:360-361` (pipeline steps 7–8: "Deploy the green runtime, verify readiness, and switch new-call routing." / "Drain old runtime and monitor regressions before retiring it.").

---

## 1. Topology as provisioned by U-401 (what this scheme operates on)

Nothing in this doc invents resources; every name below is what U-401 actually provisions.

| Item | Value | Source |
|---|---|---|
| Resource group | `rg-voicebot-<env>` | `docs/azure/u-401-deployment-plan.md` §2 |
| Container Apps env | `cae-voicebot-<env>` | `infra/bicep/modules/containerApps.bicep:236-249` |
| **Runtime app (the media endpoint)** | `ca-voicebot-<env>-runtime` | `containerApps.bicep:487` (`ca-voicebot-${environmentName}-${s.suffix}`), outputs `RUNTIME_APP_NAME`/`RUNTIME_FQDN` `:600-601` |
| Revision mode (today) | `activeRevisionsMode: 'Single'` — **see prerequisite P1** | `containerApps.bicep:498` |
| Ingress | external, `transport: 'auto'` — HTTP + WS (`/twilio/media-stream`, `/ws`) share port 7860 | `containerApps.bicep:523-534` |
| Traffic rule (today) | `[{latestRevision: true, weight: 100}]` | `containerApps.bicep:529-534` |
| Readiness/liveness probe | unauthenticated GET `/api/auth/setup-status` | `containerApps.bicep:127-141`, `:555-580` |
| Runtime scale | `runtimeMinReplicas=1` (warm) / `runtimeMaxReplicas=3` | `containerApps.bicep:100-104`, `:583-586` |
| Custom domains (D2) | optional `controlHostName` / `mediaHostName`; managed certs `cert-control-<env>` / `cert-media-<env>`; **no DNS zone in Bicep** — the operator creates CNAME → `RUNTIME_FQDN` (runbook §2 step 8 two-hop binding) | `infra/bicep/modules/network.bicep:35-53`, `containerApps.bicep:142-145`, `docs/azure/u-401-deployment-runbook.md` §2 step 8 |
| Public endpoints (frozen contract) | `PUBLIC_CONTROL_BASE_URL = https://<host>`, `PUBLIC_MEDIA_BASE_URL = wss://<host>` (two-pass backfill of `<host>` = `RUNTIME_FQDN` or the custom domains) | `config_contract.py` `_MEANINGS`; `u-401-deployment-runbook.md` §2 step 6 |
| Twilio media WebSocket (externally visible) | `${PUBLIC_MEDIA_BASE_URL}/twilio/media-stream`; TwiML `<Stream url="wss://{public_host}/twilio/media-stream">` computed **per call at webhook time** | `bot.py:7234`, `bot.py:7015` |
| Call-control webhooks | `/twilio/incoming-call` (TwiML), `/twilio/status-callback` | `bot.py:6963`, `bot.py:7128` |
| Call-to-owner lease store (drain source of truth) | Cosmos `ownership` container, FC-7 verbs | `storage/ownership.py` (U-305) |
| Per-replica admission cap | `MAX_CONCURRENT_CALLS_PER_REPLICA` (provisional 3) | `u-401-deployment-runbook.md` §1.2 |
| Max call duration (deadline sizing) | `MAX_CALL_DURATION_SECONDS` (provisional 3600) | `u-401-deployment-runbook.md` §1.2 |

---

## 2. The scheme: new calls to green, old calls drain on blue

### 2.1 Mechanism (for THIS topology)

The runtime app is a single Azure Container Apps app. The blue/green lever is **Container Apps revision traffic-splitting on `ca-voicebot-<env>-runtime`**, in **multiple revision mode**:

```
# 1. green (new revision) is created and passes readiness — see §8
# 2. routing switch — new connections go to green; blue keeps its in-flight calls
az containerapp ingress traffic set \
  --name ca-voicebot-<env>-runtime --resource-group rg-voicebot-<env> \
  --revision-weight <green>=100 <blue>=0
```

(`az containerapp ingress traffic set --revision-weight REV=W ...`, weights sum to 100. Mode prerequisite: `az containerapp revision set-mode --mode multiple` — see P1.)

Why this is "new-calls-to-green / old-calls-drain" for a **media WebSocket** endpoint:

1. **Call admission is connection admission.** A Twilio call reaches us as: TwiML webhook (`/twilio/incoming-call`, answered by whatever revision currently holds traffic) → the TwiML `<Stream>` points at `wss://<host>/twilio/media-stream` (computed per call, `bot.py:7015`) → Twilio opens a **new** WebSocket. That WS upgrade is routed by the current ingress weights — so after the switch, every NEW call's media stream lands on green.
2. **Established WebSockets stay where they are.** An in-flight call's media WS is pinned to the replica that accepted it for the whole connection lifetime. Changing traffic weights affects only admission of NEW connections/requests; established streams are not moved or re-routed. The call keeps flowing on blue until it ends.
3. **The endpoint keeps resolving.** The hostname binds at the **app** level — the stock `RUNTIME_FQDN` or the custom `mediaHostName` CNAME → `RUNTIME_FQDN` (runbook §2 step 8). Revisions share it. **A release performs zero DNS operations**; `PUBLIC_MEDIA_BASE_URL` and the Twilio-side media URL stay valid across the switchover.

Ordinary revision switching in the current **Single** mode does **not** do this: a new revision becomes latest, the old revision is deactivated and its replicas die — in-flight media streams drop mid-call. That is precisely why P1 exists.

### 2.2 State diagram

```
                       release of green starts
  ┌──────────────┐      (new revision created)      ┌───────────────────┐
  │ steady-blue  │ ───────────────────────────────► │ green-provisioning│
  │ blue: 100%   │                                  │ blue:   100%      │
  │ (all calls)  │                                  │ green:  0%*       │
  └──────────────┘                                  └─────────┬─────────┘
        ▲                                       green passes readiness
        │ rollback                              (probe + smoke, §8)
        │ (flip weights to blue)                            │
        │                                                   ▼
  ┌──────────────┐      regression in watch       ┌───────────────────┐
  │ rolled-back  │ ◄───────────────────────────── │    switched       │
  │ blue: 100%   │                                │ green: 100% (NEW  │
  │ green drains │                                │   calls)          │
  │ (roles swap) │                                │ blue: 0%, ACTIVE  │
  └──────────────┘                                │ (IN-FLIGHT calls  │
        ▲                                         │  drain here)      │
        │                                         └─────────┬─────────┘
        │ drain-complete (=0 leases, §4)  ── OR ── approved deadline
        │                                                   │
        │                                                   ▼
        │                                         ┌───────────────────┐
        │                                         │  forced-drain     │
        │                                         │  (default: announ.│
        │                                         │   BYE, §6)        │
        │                                         └─────────┬─────────┘
        │                                                   │ settled to 0
        │                                                   ▼
        └────────────────────────────────────────── ┌───────────────────┐
                                                    │  retired          │
                                                    │  blue deactivated │
                                                    └───────────────────┘

  * in multiple revision mode a new revision takes no traffic until weights say so.
```

### 2.3 Decision points

| # | Question | Yes | No |
|---|---|---|---|
| D1 | Green passes readiness (probe `/api/auth/setup-status` + smoke)? | switch weights (green 100) | stay on blue; green holds 0% (its direct FQDN/revision endpoint can be tested) |
| D2 | Drain-complete before the approved deadline? | retire blue (`--retire-when-drained`) or hold for the watch window | D3 |
| D3 | Forced-drain approved (`--force --confirm-forced-drain=<blue>`)? | announced forced-BYE (§6 B) → settle → recount | report + exit 2; options: extend deadline / forced-drain A–D |
| D4 | Settled to zero leases after forced-drain? | retire blue | exit 3 + manual options (never an automatic hard kill) |
| D5 | Regression during the watch window? | rollback (§7) — flip weights to blue | retire blue after the window |

---

## 3. Retained old capacity

Per spec `:248`: *"Retain old deployment capacity until no active calls remain or an approved drain deadline is reached."* Concretely for this topology:

1. **Blue stays ACTIVE at weight 0.** In multiple revision mode, `traffic ... --revision-weight <blue>=0` keeps the revision active; nothing deactivates it. Deactivation (`az containerapp revision deactivate`) happens **only** after drain-complete or a completed forced-drain — that is what `drain-runtime.sh --retire-when-drained` enforces (it refuses to deactivate while leases are live).
2. **Scale floor.** Blue's template carries `runtimeMinReplicas=1` (`containerApps.bicep:100-101`) — one warm replica minimum while the revision is active.
3. **Live calls hold their replicas.** Established media WebSockets are active requests for the KEDA HTTP-concurrency scale rule, so replicas hosting calls are not scaled in while the calls run.
4. **Honest caveat.** There is **no `az` command that edits an existing revision's scale** (`az containerapp revision copy` creates a *new* revision — new replicas mean new owner IDs and would break lease attribution mid-drain; it is forbidden here). Retention therefore = ACTIVE + template floor + connection-driven scale. If the platform replaces a replica anyway (scale event, node loss, crash), the calls on it die with it — the documented owner-loss semantics (`storage/ownership.py:18-26`: the call drops, there is **no** transparent handover) — and its leases self-reap within `lease_ttl_seconds` (30 s default, `storage/ownership.py:146`), so the drain accounting self-heals.

---

## 4. Drain-complete criterion

> **Drain-complete = ZERO live FC-7 lease rows attributed to any current replica of the blue revision.**

The predicate is the FC-7 counting semantics of `storage/ownership.py` (`_CosmosStore.list_leases`, `:105-117`), evaluated with the blue replica set as the owner set:

```
row.kind == "lease"
AND row.lease_expires_at > now
AND ( row.owner_id ∈ blue_replicas
      OR (owner_id absent AND row.reserved_by ∈ blue_replicas) )
```

- **Owner IDs = Container Apps replica names** (`az containerapp replica list --name ca-voicebot-<env>-runtime -g rg-voicebot-<env> --revision <blue>`). This is the binding convention for the U-305 runtime wiring: `owner_id` = replica name (the container `HOSTNAME`). Unbound reservations (`reserved_by`) count too — a reservation in handshake is a call about to exist.
- **Why "live rows only"**: a replica that died stops renewing; its leases expire within `lease_ttl_seconds` (30 s, `storage/ownership.py:146`) and the row disappears. Counting live rows therefore cannot wedge the drain on a dead replica's leftovers. The default poll interval (`--poll-seconds 30`) matches the TTL so every poll sees the reaped state.
- **Tooling**: `scripts/deploy/drain_status.py census` prints exactly this census; `scripts/deploy/drain-runtime.sh <env> <blue> --apply` polls it until zero. Both reuse `storage.ownership`'s store interface — no reimplementation of the SQL.

---

## 5. Prerequisites and honest gaps

**P1 — revision mode must become `Multiple` (infra delta — landed with U-402).**
`containerApps.bicep` now sets `activeRevisionsMode: s.httpIngress ? 'Multiple' : 'Single'` (the worker keeps `Single`: no ingress, no traffic concept), so traffic splitting and blue retention are structurally possible. The `traffic` array cannot be pinned in Bicep (it cannot name future revision suffixes) — the pipeline pins active revisions at green-creation time and then issues the single `az containerapp ingress traffic set` mutation (`docs/azure/ci-cd-pipeline.md` §4). Interim hazard unchanged: any `scripts/deploy/deploy-azure.sh <env> --apply` mid-window replays the Bicep traffic default (`latestRevision: 100`) — rule: never run `--apply` during a blue/green window; the pipeline gates this.

**P2 — the census needs the runtime to actually take leases (wiring landed).**
The drain source of truth is the FC-7 lease. `bot.py` now wires `CosmosSessionDirectory` into the media path: at stream acceptance it calls `reserve_capacity` + `bind_owner` (owner = `CONTAINER_APP_REPLICA_NAME`, else `HOSTNAME`), renews every 10 s (lease TTL 30 s), and `release`s at teardown (`bot.py` "U-305 / U-306 wiring" section). Gate: `MAX_CONCURRENT_CALLS_PER_REPLICA` — an existing §11 contract key; unset → unleased single-replica operation and a trivially zero census. Coordination-plane failures degrade to unleased (logged) instead of dropping calls; admission refusal refuses the stream (WS close 1013). **The rehearsal (§9) must still demonstrate census > 0 with real calls under a set gate before the drain-complete criterion is trusted in production.**

**P3 — operator access.**
`drain_status.py` authenticates like `storage/cosmos.py:56-68` (`DefaultAzureCredential` + `COSMOS_ENDPOINT`/`COSMOS_DATABASE`): the operator principal needs the **Cosmos DB Built-in Data Contributor** role on `cosmos-voicebot-<env>` (RBAC matrix row 4, `docs/azure/u-401-deployment-plan.md` §3) plus `az login` for the `az containerapp` commands.

---

## 6. Forced-drain behaviour (approved deadline reached, calls remain)

Options and caller experience (spec `:245-246`; "shutdown grace does not guarantee every call completes"):

| Option | Mechanism | Caller experience | When | Who |
|---|---|---|---|---|
| **A. Let them finish** | extend the wait (`--deadline`) and keep polling | unaffected — calls complete normally | default **before** the deadline | `drain-runtime.sh --apply` |
| **B. Announced forced BYE — DEFAULT at deadline** | one FC-7 `hangup` command per live call (`Command(name="hangup", payload={"reason": ...})`, vocabulary per `tests/test_ownership.py:155`) via `route_command` (`storage/ownership.py:277`) → the owner ends the call through its **normal end-of-call path** (the same semantics as the `end_call` tool) → Twilio BYE | **early but normal ending** — the caller hears a normal close/hangup. **No silent drop.** | at the approved deadline, when forcing is authorized | `drain-runtime.sh --apply --force --confirm-forced-drain=<blue>` → `drain_status.py force-end` |
| **C. Carrier-side completion** | complete the call via Twilio console/REST per CallSid | normal carrier hangup (possibly mid-conversation, but a clean disconnect) | fallback when B is unavailable | operator (manual) |
| **D. Hard teardown** | `az containerapp revision deactivate <blue>` → replicas die | **mid-audio drop = silent drop** | **never automatic**; manual last resort after explicit sign-off | operator (manual) |

**Default posture: B, with C as fallback. D is never automatic.**

- This is the spec's operator-controllable drain: admission already stopped (blue holds 0% traffic), and FC-7's command routing (`storage/ownership.py:277-355`) is the operator's per-call end lever — the runtime-side counterpart of §8.3's shutdown sequence ("stop admitting calls, mark the owner draining, ... clean up leases", spec `:245`).
- **Honesty about B's wiring**: delivery needs the FC-7 owner-addressable transport (`send`, `storage/ownership.py:136-137`). While it is unwired, `route_command` refuses truthfully (`accepted=False`, "no owner-addressable transport wired", `:282-285`), `force-end` reports that per call and exits 3, and **the tool kills nothing** — see the `DRAIN_SEND_FACTORY` seam in `scripts/deploy/drain_status.py`. The rehearsal (§9) drills this failure path.
- `drain-runtime.sh` **refuses to deactivate a revision that still owns calls**, even under `--force`: with residual leases it exits 3 and prints the option table including the manual D command with its explicit mid-audio-drop warning. A silent drop is never something this tool does quietly.

---

## 7. Rollback interplay

**Rollback = point new calls back to blue-previous.** Blue has been retained (§3) precisely so this stays possible:

```
az containerapp ingress traffic set --name ca-voicebot-<env>-runtime \
  --resource-group rg-voicebot-<env> --revision-weight <blue>=100 <green>=0
```

- New calls immediately land on blue again; the green revision's in-flight calls drain on green — the **same tool with roles swapped** (`drain-runtime.sh <env> <green> --apply`). Green is only retired once it is drained.
- This is why §8 keeps a **watch window between "switched" and "retired"**: retention is the rollback runway. Never retire blue before the window closes clean.
- **State**: the media path is stateless; call records live in Cosmos (`calls` family, U-301) and are unaffected by which revision served a call. No data migration is involved in a release or its rollback.
- **What rollback is NOT**: it is not the RG-level rebuild (`docs/azure/u-401-deployment-runbook.md` §4, infra disaster path) and not the old-world "re-apply `containerImage` at the previous tag" (`runbook` §4 row 3 — which itself points at U-405 for the media-endpoint story; this document supersedes that pointer for releases).

---

## 8. Tie-in to U-402's pipeline (the sequence this scheme orchestrates)

U-402's workflow (`docs/azure/implementation-plan.md:423`; spec §12 `:356-361`) becomes:

| Pipeline phase (spec §12) | Action | This doc / tool |
|---|---|---|
| build once, by digest | image `acrvinebot<env>.azurecr.io/voicebot:<digest>` | — |
| Bicep what-if → staging deploy + smoke → approval | unchanged | — |
| **7. green deploy** | create green revision (new `containerImage`) on `ca-voicebot-<env>-runtime` | P1 applies: multiple revision mode; green takes **0%** until the switch |
| **7. readiness** | probe `/api/auth/setup-status` green + smoke (incl. a test call on green's revision endpoint) | §2.1, D1 |
| **7. routing switch** | `az containerapp ingress traffic set ... --revision-weight <green>=100 <blue>=0` | §2.1 — the only traffic mutation in the release |
| **8. drain** | `scripts/deploy/drain-runtime.sh <env> <blue> --apply [--retire-when-drained]` | §4 (criterion), §3 (retention), §6 (deadline behaviour) |
| **8. watch** | observation window + `monitor.bicep` alerts (DLQ / app 5xx / Cosmos 429) | §7 watch window = rollback runway |
| retire | deactivate blue after drain + clean watch | `--retire-when-drained` or manual |

Tool reference (implemented in `scripts/deploy/drain-runtime.sh` + `scripts/deploy/drain_status.py`):

```
scripts/deploy/drain-runtime.sh <dev|staging|prod> <revision> [options]
  <revision> = the BLUE (draining) revision
  default mode = what-if (read-only census + plan)
  --apply                        run the drain wait loop
  --deadline <minutes>           default 60 (>= MAX_CALL_DURATION_SECONDS default 3600s)
  --poll-seconds <n>             default 30 (= FC-7 lease_ttl_seconds)
  --retire-when-drained          deactivate the revision once drained
  --force                        forced-drain at the deadline; REQUIRES --apply AND
                                 --confirm-forced-drain=<revision> (explicit confirmation)
  exit codes: 0 drained/what-if ok · 1 usage/preflight · 2 deadline w/ live leases ·
              3 forced-drain incomplete (never hard-kills replicas)

scripts/deploy/drain_status.py census    --owners <replica,...> [--format text|json|count]
scripts/deploy/drain_status.py force-end --owners <replica,...> --reason "forced-drain U-405"
```

---

## 9. Rehearsal checklist (input for U-406 runbooks; evidence for R-A-016)

Run on **staging** first (`requirements.md:54` acceptance: *"A deployment test demonstrates in-flight calls completing on the old revision while new calls land on green; forced-drain outcome documented and reviewed"*):

1. **Preconditions**: P1 mode = Multiple on the test app (record `az containerapp revision show`/mode output); P3 credentials work (`drain_status.py census` returns without auth error); P2 satisfied — the runtime writes FC-7 leases (see step 3).
2. **Two revisions**: deploy green alongside blue; green passes readiness (probe + smoke).
3. **Load blue with real calls**: N ≥ 2 real PSTN calls on blue that will outlive the switchover (long-running or held); **verify `drain_status.py census` reports N > 0** attributed to blue's replicas (this is the P2 proof — without it the criterion is untrusted).
4. **Switch**: `az containerapp ingress traffic set ... --revision-weight <green>=100 <blue>=0`.
5. **New call lands on green**: place a new call; correlate it to a green replica (census owner set / logs). *Evidence: census before/after showing the new call attributed to green.*
6. **Blue calls complete uninterrupted**: audio continues through the switch; the calls end naturally afterwards. *Evidence: the R-A-016 deployment test.*
7. **Natural drain**: `drain-runtime.sh <env> <blue> --apply` reports DRAINED as calls end; with `--retire-when-drained` blue is deactivated only after zero leases.
8. **Forced-drain drill (B)**: repeat with a short `--deadline`; hold a call past it; run `--apply --force --confirm-forced-drain=<blue>`; observe the caller hears an early but normal ending; tool exits 0 and the census reaches zero. *Evidence: forced-drain outcome per R-A-016.*
9. **Failure-path drill (B unwired)**: with `DRAIN_SEND_FACTORY` unset, `force-end` reports "no owner-addressable transport wired", the tool exits 3, **no replica is killed and no call drops** (options A/C/D printed). *Evidence: output transcript.*
10. **Rollback drill**: mid-drain of a release, flip weights back to blue (`§7`); confirm new calls return to blue and green drains with the same tool.
11. **Deadline drill for D**: manually (signed off) deactivate a revision with one live call; observe and record the mid-audio drop — this documents the caller experience of the last-resort option honestly.
12. **Archive** all outputs (census snapshots, tool transcripts, call IDs) as the U-501 acceptance evidence; U-406 turns steps 1–11 into the release-drain / forced-drain / rollback runbooks.

---

## 10. Reference index

| Fact | Source |
|---|---|
| U-405 card; U-402 sequence | `docs/azure/implementation-plan.md:426`, `:423` |
| R-A-016 / R-T-010 | `docs/azure/requirements.md:54`, `:108` |
| Spec §8.3 blue/green + shutdown clauses; §12 steps 7–8 | `docs/Azure_Voicebot_Implementation_Instructions_Corrected.md:245-248`, `:356-361` |
| Runtime app, revision mode, ingress/traffic, probes, scale | `infra/bicep/modules/containerApps.bicep:487`, `:498`, `:523-534`, `:127-141`, `:100-104` |
| Custom domains / managed certs (no DNS zone) | `infra/bicep/modules/network.bicep:35-53`; `docs/azure/u-401-deployment-runbook.md` §2 step 8 |
| PUBLIC_MEDIA_BASE_URL contract; two-pass URL backfill | `config_contract.py` `_MEANINGS`; `u-401-deployment-runbook.md` §2 step 6 |
| Twilio media WS route + per-call TwiML Stream URL | `bot.py:7234`, `bot.py:7015` |
| FC-7 leases: predicate, TTL, release/drain, owner-loss, route_command, transport seam | `storage/ownership.py:105-117`, `:146`, `:357-397`, `:18-26`, `:277-355`, `:136-137` |
| `hangup` command vocabulary | `tests/test_ownership.py:155` |
| Cosmos auth + RBAC row 4 | `storage/cosmos.py:56-68`; `docs/azure/u-401-deployment-plan.md` §3 |
| MAX_CALL_DURATION_SECONDS / MAX_CONCURRENT_CALLS_PER_REPLICA provisional values | `u-401-deployment-runbook.md` §1.2 |
| Drain tooling | `scripts/deploy/drain-runtime.sh`, `scripts/deploy/drain_status.py`, `tests/test_drain.py` |
| az containerapp CLI (traffic weights, set-mode, deactivate, replica list) | Azure CLI reference: `az containerapp ingress traffic set`, `az containerapp revision set-mode/deactivate`, `az containerapp replica list` |

*Contract freeze: FC-0..FC-8 untouched; `storage/ownership.py` semantics cited, not modified.*
