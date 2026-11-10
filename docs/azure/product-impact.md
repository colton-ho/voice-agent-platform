# Product Impact Assessment — AWS→Azure Voicebot Migration (Phase 2)

Author: 简宜用 (product-designer) · Status: draft for review
Scope: product/UX surfaces and observable contracts that must remain unchanged on Azure. No interface/SDK design here (architect's remit). Analysis only — no application code was modified.
Evidence verified 2026-06-10 against the repository; items marked *unverified* were not directly observable. R-IDs cite `docs/azure/requirements.md`.

---

## 1. Surface inventory

The task brief's list is complete at the top level; this assessment splits it into the surfaces actually observable in code and adds three the brief missed (ASR test tool, per-user My History, Web/Phone defaults editors).

| # | Surface | What the user sees/does | Evidence |
|---|---|---|---|
| S-1 | **Talk page** (web voice) | Browser mic ↔ live voice conversation; engine/language/provider/voice/scenario/model selectors; push-to-talk toggle; on-demand summary modal | `static/admin/src/views/TalkView.vue`, `talkConfig.js`, `audio.js` (PTT), route `/talk` (`static/admin/src/router/index.js:56`); backend `WS /ws` (bot.py:5948), `GET /api/config` (bot.py:5493), `POST /api/summary` (bot.py:5904) |
| S-2 | **Monitor mode** | Read-only live event stream of any active call (ASR/LLM/TTS events, no audio); active-call dropdown | `static/admin/src/views/MonitorView.vue`, `useEventLog.js`, `ws.js:openMonitorWs`; `GET /api/calls` (bot.py:6490), `WS /monitor/ws` (bot.py:7292) |
| S-3 | **Telephone caller experience** | Caller dials → greeting → scenario/KB-grounded dialog in zh-HK/zh-CN/en-US/ja-JP → tool execution (`end_call`, `transfer_to_human`) → call ends; barge-in supported | Two ingresses sharing `_run_phone_session` (bot.py:6981): Chime `WS /phone/ws` (bot.py:6959) and Twilio `POST /twilio/incoming-call` + `WS /twilio/media-stream` (bot.py:7154, 7236); phone defaults from `RUNTIME_CONFIG.get_phone_defaults()` (bot.py:7014); greeting resolution bot.py:1859/2107/2336; KB scenarios bot.py:1073 + `data/acme-security-support/kb.md`; handoff `call_control_tools.py` via `HistoryRecorder.mark_transfer`/`write_outcome_row` (bot.py:1290–1340) |
| S-4 | **First-run /setup wizard** | Fresh deploy → forced `/setup` → create admin → auto-login → wizard permanently closes | `SetupView.vue`, router guard (router/index.js:70–109); `GET /api/auth/setup-status`, `POST /api/auth/setup` (bot.py:2820, 2836); README "First-run admin setup"; `docs/first-run-setup.md` |
| S-5 | **Login / session** | Username+password login → HttpOnly `vb_session` cookie; logout; role-gated nav | `LoginView.vue`, `api.js`; `POST /api/auth/login`, `/logout`, `GET /api/auth/me` (bot.py:2784, 2882, 2899) |
| S-6 | **Guest links** | Admin mints time-limited experience link (scenario + launch-config overrides) → guest lands on `/guest` → redeemed into a `guest` session confined to Talk | `GuestLanding.vue`, `guestLink.js`, UsersView mint modal; `POST /api/admin/guest-links` (bot.py:2904), `POST /api/auth/guest-login` (bot.py:2956); router guest confinement (router/index.js:125–127) |
| S-7 | **Admin backend — users/roles** | CRUD users, role/password/disable changes, self-lockout guard | `UsersView.vue`; `GET/POST /api/admin/users`, `PATCH/DELETE /api/admin/users/{username}` (bot.py:2986–3071) |
| S-8 | **Admin backend — demo/scene management** | List/rescan/detail/PATCH demos (tools, MCP servers, model, label, tags, per-lang system prompts, KB, localized fields, engine/voice/provider), preview-translate | `DemosView.vue`; `GET /api/admin/demos`, `/demos/{id}`, `POST /demos/rescan`, `PATCH /demos/{id}` (bot.py:4306–5082), `POST /demos/{id}/translate` (bot.py:5798); 8 scenario presets in `data/` |
| S-9 | **Admin backend — MCP servers** | CRUD + connection test; secret headers masked `***`; auth types none/header/sigv4 | `McpServersView.vue`; `GET/POST /api/admin/mcp-servers`, `DELETE …/{id}`, `POST …/{id}/test` (bot.py:5128–5267) |
| S-10 | **Admin backend — voices registry** | Per-provider editable voice registry (minimax / polly / nova-sonic) with CRUD; /api/config reflects writes immediately | `VoicesView.vue`; `GET/POST /api/admin/voices`, `PATCH/DELETE …/{provider}/{voice_id}` (bot.py:5345–5451) |
| S-11 | **Admin backend — dashboard/activity** | Live metrics polled every 7 s; operations audit log with actor/type/date filters | `DashboardView.vue`, `ActivityLogView.vue`; `GET /api/admin/metrics` (bot.py:3430), `GET /api/admin/activity` (bot.py:5453); `activity_store.py` allow-list redaction |
| S-12 | **Admin backend — history & exports** | Full call history (filters, cursor pagination), detail with turns+summary, Markdown/CSV export, re-summarize | `HistoryView.vue`, `_HistorySummary.vue`; `GET /api/admin/history`, `/history/{id}`, `/history/{id}/export.md`, `/history.csv`, `POST /history/{id}/summarize` (bot.py:3556–4143) |
| S-13 | **Per-user My History** | The caller's own web calls | `MyHistoryView.vue`; `GET /api/history` (user-scoped, bot.py:6572), `GET /api/history/{call_id}` (bot.py:6707), `GET /api/history/by-caller` (bot.py:6640) |
| S-14 | **Web/Phone defaults editors** | Admin edits global Web and Phone launch defaults (engine/lang/scenario/voice/provider/model, ASR filter, filler); hot-reload for new calls | `WebDefaultsView.vue`, `PhoneDefaultsView.vue`, `DefaultsForm.vue`; `GET /api/admin/config`, `PUT /api/admin/config/web|phone` (bot.py:3269–3314) |
| S-15 | **Phone numbers view** | Read-only Chime VC ↔ phone-number inventory (best-effort; errors shown inline) | `PhoneDefaultsView.vue` (pn-error block); `GET /api/admin/phone-numbers` (bot.py:3423, `_list_chime_vc_numbers` bot.py:3325) |
| S-16 | **ASR A/B test tool** (brief missed) | Admin-only, direct-URL diagnostic: one mic → two Transcribe streams (single zh-HK vs multi-language) | `AsrTestView.vue`, `asrTest.js`, route `/asr-test` (router/index.js:61); `WS /asr-test/ws` (bot.py:6368), `static/admin/src/__tests__/asrTest.spec.js` |
| S-17 | **SPA shell** | Login/role-gated nav, i18n (8 locales), theme, debug drawer | `App.vue`, `i18n/`, `theme.js`, `DebugDrawer.vue`; SPA served at `/` via StaticFiles mount (bot.py:7375–7390); `/admin` legacy redirect (bot.py:3080) |
| S-18 | **Tool registry view** | Read-only list of available agent tools (`end_call`, `transfer_to_human`, MCP/REST tools) surfaced in Demos UI | `GET /api/admin/tools` (bot.py:4294); `tools/registry.py` |

---

## 2. Contract-preservation list

"Pinned" = an automated test asserts it today; SPA = the SPA code is the only pin (regression caught only by frontend tests / manual QA).

### 2.1 Talk page (S-1)

| Contract | Detail (must not change) | Pinned by |
|---|---|---|
| `GET /api/config` | require_user. Fields consumed by SPA: `languages[{id,label,engines}]`, `models[{id,label,bedrock_id}]`, `providers[{id,label}]`, `minimax_models`, `default_minimax_model`, `engines[{id,label}]`, `demos`, `scenarios`, `default_demo`/`default_scenario`, `voices_by_provider.{minimax,polly}[{id,label,language}]`, `nova_sonic_voices[{id,label,gender,locale,lang_label,polyglot}]`, `default_language/model/provider/engine/nova_sonic_voice`, `default_voices.{minimax,polly}`, `default_prompts.{lang}.{system,greeting,summary}` | bot.py:5493–5548; `tests/test_admin_api.py:328`; SPA: `talkConfig.js` (null-safe field reads), `stores/config.js` |
| `WS /ws` auth | `?token=` short-TTL (60 s) user-bound token from `GET /api/ws-token` (require_user); missing/invalid → close 1008 "unauthorized" | bot.py:5948–5956, 5477–5490 (`_WS_TOKEN_TTL=60` bot.py:166); `tests/test_auth.py:380`; SPA `ws.js:fetchWsToken` |
| `WS /ws` query params | `engine, lang, scenario, model, provider, voice, minimax_model` + `system/greeting` overrides + `call_id/caller`; priority query > web defaults > constants; invalid scenario falls back | bot.py:5960–6017; SPA `ws.js:LAUNCH_QUERY_KEYS` |
| `WS /ws` binary frames | Client → server: 16 kHz mono PCM16 (`INPUT_SAMPLE_RATE=16000`); server → client: 24 kHz PCM16 (`OUTPUT_SAMPLE_RATE=24000`) | bot.py:281–282; SPA `audio.js` (`IN_RATE=16000`, `OUT_RATE=24000`, comment: "/ws on the server side configures audio_in_sample_rate=16000") |
| `WS /ws` text events | JSON, every event stamped `t` (seconds since session start, 3 decimals). Types: `user_speaking{value:bool}`, `asr_partial{text}`, `asr_final{text}`, `llm_start`, `llm_delta{text}`, `llm_end{text:full}`, `tts_start`, `tts_end`, `bot_speaking{value:bool}`; plus tool-call events via `emit_external` (*exact shape unverified*) | bot.py:1645–1669 (`_WATCHED` bot.py:1537–1549), `t` stamping bot.py:1577/1584; SPA `useEventLog.js` merge semantics (dedupe window, asr/llm roll-up) |
| `POST /api/summary` | require_user. Req `{language, model?, turns:[{who,text}]}`; Resp `{summary, summary_struct, language, model, turns}` or **200 + `{error}`** (error-as-data) | bot.py:5904–5945 |
| Session semantics | Web calls register in `ACTIVE_SESSIONS` (counted by dashboard) and attach to history as `web_user`; per-call config snapshot (mid-call admin edits don't affect the call) | bot.py:5970–6023, 5958–5959 |

### 2.2 Monitor mode (S-2)

| Contract | Detail | Pinned by |
|---|---|---|
| `GET /api/calls` | **require_admin**. `{calls:[{call_id, caller, started, monitors}]}` | bot.py:6490–6503; SPA `api.js:calls()` |
| `WS /monitor/ws` auth | ws-token with `role == "admin"`; else close 1008 | bot.py:7298–7301; SPA `ws.js:openMonitorWs` |
| Frame contract | `?call_id=` required → else text `{type:"error", text:"call_id required"}` + close; unknown call → `{type:"error", text:"call_id … not active"}` + close; success → `{type:"monitor_attached", call_id, t:0}` then the same event JSON as the primary session (read-only, no audio), multi-attach OK | bot.py:7303–7325 |

### 2.3 Telephone caller experience (S-3)

| Contract | Detail | Pinned by |
|---|---|---|
| `/phone/ws` wire protocol | Binary **8 kHz** PCM16 uplink (`PHONE_WIRE_SAMPLE_RATE=8000`), binary **24 kHz** PCM16 downlink + same JSON event text frames as /ws; no auth (bridge-internal) | bot.py:284–286, 6959–6978; `tests/test_phone_session.py:180–220`, `tests/test_auth.py:359`, `tests/test_phone_sample_rate.py` (8 kHz tagging, Twilio serializer untouched :129) |
| Shared session kernel | `_run_phone_session(*, call_id, caller, serializer=None)`; serializer=None (Chime) → RawPCMSerializer/8 kHz pipeline; Twilio passes `TwilioFrameSerializer` untouched | `tests/test_phone_session.py:108–174` (signatures asserted) |
| `POST /twilio/incoming-call` | 503 plain-text "Twilio integration not configured" when `TWILIO_AUTH_TOKEN`/`TWILIO_PUBLIC_BASE_URL` unset; 403 "invalid signature" on missing/invalid/wrong-host signature (signature covers query string); 200 TwiML `<Response><Connect><Stream url="wss://<public-host>/twilio/media-stream"><Parameter name="caller" …/></Stream></Connect></Response>` | bot.py:7154–7198; `tests/test_twilio_endpoints.py` (18 tests: 503, 3×403, TwiML, query-string sig, …), `tests/test_twilio_sig.py`, `tests/test_twilio_integration.py` |
| `WS /twilio/media-stream` | Disabled → accept+close 1011; no/illegal pre-`start` frame within `TWILIO_START_TIMEOUT` (10 s) or missing `streamSid` → close 1008; happy path → `TwilioFrameSerializer` (μ-law 8 kHz ↔ pipeline rates, interruption `clear`), `call_id = "twilio-<streamSid>"`, caller from `customParameters`, auto-hangup only when callSid+accountSid+token present | bot.py:7206–7289; `tests/test_twilio_endpoints.py:186–362` (incl. serializer media/`clear` behavior) |
| Caller-visible dialog | Greeting per language/scenario (`LANGUAGES` 4 business languages zh-HK/zh-CN/en-US/ja-JP, bot.py:303–335; `en-SG` + Nova-only locales bot.py:346+ are extra); KB-grounded `acme-security-support` injects KB as a seed message (bot.py:1073); barge-in (Silero VAD, bot.py:2018); phone defaults = `PHONE_*` env → runtime config (README "电话呼入默认"; per-call snapshot bot.py:7012–7021) | bot.py refs; `data/` (8 scenarios); README options lists; R-P-001/002/003/009 |
| Handoff behavior | `transfer_to_human(topic)` → records `transfer_requested` + `transfer_topic`, writes outcome row **before** teardown (external "Connect Flow Lambda" consumes it), then ends call; `end_call` likewise | bot.py:1290–1340 (comments name the downstream consumer); `call_control_tools.py` (per requirements.md R-P-005 evidence); `tests/test_call_control_tools.py`, `tests/test_call_control_scope.py` |
| Phone scope rules | Phone-leg tools gated `PHONE_TOOLS_ENABLED` (bot.py:1234); phone/web tool scope semantics preserved | `tests/test_call_control_scope.py`; R-P-004 |

### 2.4 First-run /setup wizard (S-4)

| Contract | Detail | Pinned by |
|---|---|---|
| `GET /api/auth/setup-status` | PUBLIC, returns **only** `{needs_setup: bool}`; 503 "user store unavailable" on store errors (SPA then fails open to login) | bot.py:2820–2833; `tests/test_setup.py:104–131, 315` |
| `POST /api/auth/setup` | PUBLIC, self-closing: creates first admin + auto-login (same cookie); 400 empty input; **409 forever after any user exists** (race losers map to 409, never 500) | bot.py:2836–2879; `tests/test_setup.py:131–239` |
| Wizard UX | Router forces every route → `/setup` while `needs_setup`; bounces `/setup` away once initialized; probe failure must not trap the user | router/index.js:70–109; README "First-run admin setup" |

### 2.5 Auth/session core (S-5) and guest links (S-6)

| Contract | Detail | Pinned by |
|---|---|---|
| Session cookie | `vb_session` — HttpOnly, Secure, SameSite=Lax JWT cookie, TTL `AUTH_TTL_HOURS` (default 12 h) | bot.py:150, 2754–2763; `tests/test_auth.py` |
| Login | `POST /api/auth/login {username,password}` → `{username, role}`; 401 bad/missing credentials; 503 store-unavailable; disabled users rejected | bot.py:2784–2817; `tests/test_auth.py:215–244` |
| Session probe / logout | `GET /api/auth/me` → `{username, role}` (401 when absent/expired); `POST /api/auth/logout` idempotent `{ok:true}` | bot.py:2882–2901 |
| Role gating | `require_user` 401 (guest sessions resolve to role `guest` without a store row); `require_admin` 403 for non-admin **including guests** | bot.py:2721–2751; `tests/test_auth.py:262–281`, `tests/test_guest_link.py:164–192` |
| WS token | `GET /api/ws-token` (require_user) → `{token, ttl:60}`; WS-token claims are refused by `require_user` (browser-history attribution only) | bot.py:209–225, 5477–5490; `tests/test_guest_link.py:192` |
| Guest links | Admin mint `POST /api/admin/guest-links` `{ttl_minutes 1..1440 (default 60), scenario?, lang?, engine?, voice?, provider?}` → `{token, ttl_seconds, expires_at, scenario, lang, engine, voice, provider, path:"/guest"}`; token shown once, never logged. Redeem `POST /api/auth/guest-login {token}` (PUBLIC) → same cookie, resp `{username:"guest", role:"guest", scenario, lang, engine, voice, provider}` (nulls tolerated); 401 tampered/expired/non-guest. Guest confined to `/talk` (router) and 403 on `/api/admin/*` | bot.py:2904–2981; `tests/test_guest_link.py` (mint TTL/scenario validation, redemption, confinement :362, token round-trip :390) |
| SPA auth behavior | Central 401 → `/login` redirect (`api.js`), structured error bodies surfaced (`err.body`), auth probes skip the redirect | `static/admin/src/api.js:18–55` |

### 2.6 Admin backend (S-7…S-12, S-14, S-15, S-18)

| Contract | Detail | Pinned by |
|---|---|---|
| Admin gating | Every `/api/admin/*` requires `require_admin`; `/api/config`, `/api/summary`, `/api/history*`, `/api/ws-token` require `require_user` | `tests/test_admin_api.py:170–208`, `tests/test_admin_dashboard.py:574–594` |
| `GET /api/admin/config` + `PUT /api/admin/config/web|phone` | Both segments returned; PUT persists and **hot-reloads for new calls only** (in-flight calls keep their snapshot); invalid engine rejected; partial sub-block merge (ASR filter) | bot.py:3269–3314; `tests/test_admin_api.py:210–265, 1323–1367` |
| Demos | List/detail/rescan + `PATCH /api/admin/demos/{id}` field-by-field semantics (tools, mcp_servers, model incl. null-clear, label, tags, system per lang, kb, lang, localized, engine/voice/provider with validation and null-clear); translate preview never writes | bot.py:4306–5082, 5798; `tests/test_admin_api.py:266–956`, `tests/test_demo_*.py` |
| MCP servers | CRUD upsert-by-id, masked secrets (`***` round-trip), `auth` `{type:none|header|sigv4, service?, region?}`, delete 409 `{detail:{message, demos:[…]}}` when referenced, test endpoint resolves 200 with `{ok, tools, error}` | bot.py:5128–5267; `tests/test_admin_mcp_servers.py`, `tests/test_mcp_*.py`; `api.js:154–172` |
| Voices registry | Provider-keyed `{voices:{minimax:[],polly:[],nova-sonic:[]}, live}`; per-provider attribute sets; 400 invalid / 404 absent; writes reflect into `/api/config` immediately; deleted voice never crashes a call (fallback to provider default) | bot.py:5268–5451; `tests/test_admin_api.py:957–1160`, `tests/test_voice_store.py` |
| History | List cards `{call_id, caller, started_at, ended_at, duration_s, turn_count, summary_status, intent?}` + `next_cursor` (base64 cursor, 400 on invalid); filters caller/outcome/engine/demo(+alias)/start range; detail = full row incl. turns + summary; `export.md` shape; `history.csv` truncation header; re-summarize overwrites `summary`/`summary_status` | bot.py:3556–4143, 6550–6562; `tests/test_admin_dashboard.py:128–574`, `tests/test_history.py`, `tests/test_history_api.py` |
| Per-user history | `GET /api/history` scoped to `web_user == current user`, empty `{items:[], next_cursor:null}` when disabled; `GET /api/history/by-caller` | bot.py:6572–6752; `tests/test_history_api.py` |
| Metrics (dashboard) | `_aggregate_metrics` full payload shape incl. zero/empty-table behavior; peak-concurrency semantics | bot.py:3430, 6753–6957; `tests/test_admin_dashboard.py:643–777` |
| Activity log | Cursor-paginated, filters from/to/actor/type; default-deny detail redaction (`ACTIVITY_DETAIL_VALUE_ALLOWLIST`); store-missing degrades to empty; log write never blocks the business action | bot.py:5453–5475, `activity_store.py`; `tests/test_admin_api.py:1159–1301`, `tests/test_activity_store.py` |
| Users CRUD | Safe view `{username, role, created_at, disabled}` (never `password_hash`); self-disable/self-delete rejected 400 | bot.py:2986–3071; `tests/test_auth.py:304` |
| Options/tools/scenarios | `GET /api/admin/options`, `/tools`, `/scenarios`, `/health`, `/phone-numbers` (200 even on Chime errors, `error` field carries reason) | bot.py:3122–3428, 4294, 5083; `tests/test_admin_api.py:317`, `tests/test_phone_numbers.spec.js` (SPA) |

### 2.7 SPA shell (S-17)

| Contract | Detail | Pinned by |
|---|---|---|
| Serving | SPA served at `/` (StaticFiles `html=True`), hash routing; `/admin` → `/` redirect | bot.py:7375–7390, 3080–3087 |
| Route map | `/login /setup /guest /dashboard /history /web /phone /demos /mcp-servers /voices /users /activity /talk /monitor /my-history /asr-test` | router/index.js:35–62 |
| Guest confinement | role `guest` reaches only `/talk` (+`/guest`) | router/index.js:125–127; `tests/test_guest_link.py:362` |

---

## 3. Required product changes for the Azure baseline

Expected total: **no breaking product-surface changes**. The list below is exhaustive; anything not listed here must behave identically. (Provider/SDK internals are the architect's problem; only the observable surface is specified.)

| # | Change | Type | Exactly what changes | SPA tolerance / requirement |
|---|---|---|---|---|
| C-1 | **STT/TTS provider values** | Additive | `GET /api/config`: `providers[]` gains `azure_speech` (id/label); `voices_by_provider` gains an `azure_speech` key (rows keep `{id,label,language}`); `default_provider`/`default_voices` may point at it. Field *shapes* unchanged. | `talkConfig.js` reads providers/voices generically and `reconcileSelection` already falls back to first/defaults when a persisted id disappears — forward-compatible. Keep every existing key (incl. the `bedrock_id` field name on `models[]`, which SPA treats as opaque). R-P-010, R-C-001 |
| C-2 | **LLM model values** | Additive (values change) | `models[]` entries become approved Azure deployments; the `{id,label,bedrock_id}` shape stays. No model default may be silently invented (startup fails without config — not a UI change). | Same reconcile fallback as C-1. Flag: `bedrock_id` becomes a misnomer — keep the key, do not rename (breaking for any consumer of the shape). R-P-010, R-A-006 |
| C-3 | **`nova-sonic` engine option** | **Decided — (a) FINAL 2026-11-06** | Baseline ships **without** Nova Sonic (visible capability reduction, accepted): `engines[]`→`[pipeline]`, `nova_sonic_voices` becomes empty, `PHONE_ENGINE` default `nova-sonic`→`pipeline`, and the 8 Nova-only locales (bot.py:362–453) retire from phone defaults. Capability slot kept as `RealtimeVoiceEngine` → future Azure Voice Live behind `VOICE_LIVE_ENABLED=false`; the AWS rollback path keeps `nova-sonic` untouched. | SPA tolerates (reconcileSelection falls back to remaining engine). Owner decision recorded — the reduction is accepted, not silent. R-P-010, requirements.md out-of-scope "Nova Sonic" row |
| C-4 | **`GET /api/admin/phone-numbers` data source** | Additive-in-shape, content changes — **(d) FINAL 2026-11-06** | Today returns Chime VC inventory (`voice_connectors: [...]`, bot.py:3325). Ruled: re-target to Twilio IncomingPhoneNumbers behind the **unchanged** `{voice_connectors, error}` envelope (**never remove/rename**; SPA `test_phone_numbers.spec.js` pins the current shape). | Route survives; content swaps with the telephony provider (U-207 re-target). R-A-002, R-C-003 |
| C-5 | **MCP `auth.type=sigv4`** | Degradation accepted — **(c) FINAL 2026-11-06** | SigV4 signing is an AWS credential dependency (`mcp_sigv4.py`). Ruled: the option stays valid in config/UI but degrades at connect with `{ok:false, error}` (existing `MissingCredentialsError` path); existing sigv4-configured MCP servers stop connecting after AWS removal; botocore leaves the Azure image at cutover. | Admin UI keeps rendering the option and the test endpoint's `{ok:false, error}` failure-as-data shape (U-104 implements the degradation). R-P-004, R-C-003 |
| C-6 | **Twilio fallback route (spec §6.1.9)** | Additive, caller-visible | When the media stream cannot start, an approved fallback TwiML/transfer route (or defined message) replaces today's dead air/503 plain text. New caller-experience behavior — **additive only**; the 403/503/200 contract in §2.3 must remain as-is for its existing conditions. | No SPA impact. R-A-002, R-S-006 |
| C-7 | **Handoff state visibility (spec §6.4)** | Additive — **A-2 FINAL 2026-11-06** | The external "Connect Flow Lambda" is REPLACED by in-repo Twilio transfer flows (spec §6.4) and retired at cutover; history may gain outcome states (requested/connecting/connected/failed/returned). Today's row contract (`transfer_requested`, `transfer_topic`) must remain readable. | HistoryView/_HistorySummary read `summary`/`transfer_*` fields; new fields tolerated, removal/rename forbidden. R-P-005 acceptance = Phase C tests incl. no-answer/busy/failure. R-P-005, R-T-006 |
| C-8 | **Auth: Entra ID for admin/monitor** | **Breaking (login flow)** — see §4 | The only unavoidable product-visible break. | R-S-005 |

Nothing else changes: `/ws` frame/event contracts, `/monitor/ws`, `/phone/ws`, `/twilio/*`, `/api/config` field shapes, all `/api/admin/*` route+response shapes, `/setup` semantics, guest links, history/summary/activity contracts are preserved byte-for-byte at the JSON level.

---

## 4. Auth surface note (spec §4, §10 → R-S-005)

**Product-visible impact.** Moving administrative/monitor interfaces to Entra ID replaces the username/password login (S-5) with an SSO redirect for **human** logins. This is the one approved breaking change to the product surface; everything else about auth behavior must be preserved:

- **Preserve regardless of IdP:**
  - Session semantics: one HttpOnly, Secure, SameSite cookie (`vb_session` or a documented successor) with a TTL; `GET /api/auth/me`-equivalent session probe; idempotent logout; SPA central 401→login redirect (api.js) keeps working against the same status codes.
  - **Roles**: `admin` / `user` / `guest` semantics — `require_admin` 403 behavior, guest confinement to `/talk` (router/index.js:125–127), guest never reaching `/api/admin/*`.
  - **Guest links (S-6)**: Entra covers humans only. Stateless guest tokens (mint/redeem/expiry, launch-config overrides, one-time display) must survive unchanged — they are the sales/demo workflow. Tests `tests/test_guest_link.py` remain the pin.
  - **`/ws` + `/monitor/ws` token binding**: short-TTL user-bound `?token=` flow and the admin-role check on `/monitor/ws` remain (claims may now be sourced from Entra, but the frame-level behavior is unchanged).
  - **Twilio callbacks are NOT Entra-protected**: signature verification stays the sole auth for `/twilio/*` (§2.3 contract; `tests/test_twilio_sig.py`/`test_twilio_endpoints.py` remain the pin). No query-param secrets (R-S-005).
- **Open product questions (for team-lead/§4 gates):**
  1. **`/setup` wizard fate** — **RESOLVED ((b) FINAL 2026-11-06)**: keep `/setup` through Phase C as break-glass admin bootstrap; at Entra cutover demote it to seeding the local role mapping. `GET /api/auth/setup-status`'s fail-safe UX must be redefined at that cutover, not left dangling; until then current semantics (S-4) are the contract to beat.
  2. **Local accounts** — constrained by (b): username/password break-glass survives through Phase C. Still open **after** Entra cutover: keep the break-glass path or drop `UsersView` password fields. User/role *management* surface (S-7) survives; only credential entry may change.
  3. **Admin role source**: `require_admin` must map from Entra group/app-role — the 403 contract stays, the provisioning story moves to Entra.

---

## 5. Interaction states worth preserving (observable in the SPA today)

| State | Behavior to keep | Evidence |
|---|---|---|
| Session expiry | Any 401 on non-auth API → auto-redirect `/login`; auth probes handle 401 inline | api.js:18–55 |
| Setup probe failure | `setup-status` error → fail open to login (never trap on `/setup`); backend 503 shape | router/index.js:72–82; bot.py:2833 |
| Login error | Inline error alert: 401 → "invalid credentials" copy; other → generic with detail; submit button loading state | LoginView.vue:38–106 |
| List empty | `EmptyState` component (title+description+action) on tables — e.g. VoicesView | `components/ui/EmptyState.vue`; VoicesView.vue:42–48 |
| Load/save failure | Toast `loadFailed`/`saveFailed` with message; spinner `loading`/`saving` on cards and buttons | DefaultsForm.vue:12–376; VoicesView.vue:273–357 |
| Structured API errors | 409 body `{detail:{message, demos}}` rendered without re-parsing | api.js:34–40 |
| Monitor WS errors | `{type:"error", text:…}` frames ("call_id required"/"not active") rendered in the event log | bot.py:7305–7316; useEventLog.js |
| Phone-numbers degradation | HTTP 200 with `error` field + inline `pn-error` block (never a hard failure) | api.js:121–125; PhoneDefaultsView.vue:13–22 |
| Summary failure | `POST /api/summary` returns 200 `{error:…}` — shown in the summary modal, never a crash | bot.py:5923–5937 |
| History/metrics empty | `items: []` + `next_cursor: null`; dashboard zero-metrics for empty table | bot.py:6591–6592; tests/test_admin_dashboard.py:717 |
| Guest link expired | GuestLanding shows dedicated expired/invalid message | GuestLanding.vue:14 |
| Offline WS | 1008 close codes with reason `unauthorized` distinguishable from normal close | bot.py:5955, 7300 |

---

## 6. Out-of-scope confirmation + change impact summary

**Confirmed out of scope for this migration** (per mission + requirements.md §3): no new pages; no UI redesign; no new user-facing features; no visual restyle; no new languages/scenarios/tools; no outbound dialing; no call recording (`RECORDING_ENABLED=false`); no numeric SLO claims. Additive config values (C-1/C-2) and the security-mandated auth change (C-8) are the only permitted surface deltas; the caller/admin experience for every flow in §1 must be recognizable as identical.

| Surface | Change required | Affected R-IDs |
|---|---|---|
| S-1 Talk page | **Additive** (config values C-1/C-2; possible engine reduction C-3) | R-P-001, R-P-010, R-C-001/002 |
| S-2 Monitor mode | **None** (auth provider swap only, semantics preserved) | R-P-006, R-S-005 |
| S-3 Telephone caller | **None** on wire contracts; **Additive** fallback route (C-6); default engine note (C-3) | R-P-002/003/005/009, R-A-002/003/004, R-S-006 |
| S-4 /setup wizard | **Decided — (b) FINAL 2026-11-06** (break-glass through Phase C; demotes to role-mapping seed at Entra cutover) — semantics otherwise preserved | R-P-007, R-S-005 |
| S-5 Login/session | **Breaking** (Entra login flow, C-8) — session/role semantics preserved | R-S-005, R-P-007 |
| S-6 Guest links | **None** | R-P-007, R-S-005 |
| S-7 Users/roles | **None** (credential-entry question open, §4) | R-P-007 |
| S-8 Demos/scene mgmt | **None** | R-P-003/004 |
| S-9 MCP servers | **Degradation accepted** (sigv4, C-5, (c) FINAL 2026-11-06) | R-P-004, R-C-003 |
| S-10 Voices registry | **Additive** (azure_speech voices enter the registry) | R-P-010 |
| S-11 Dashboard/activity | **None** (telemetry substrate swaps underneath; redaction semantics preserved) | R-P-008, R-A-012 |
| S-12 History/exports | **None** (Additive per A-2/C-7: transfer outcome states may appear; existing fields preserved; B7 summary-timing behavioral + C-9 re-summarize semantic, ruled 2026-11-09 — see §7) | R-P-008, R-A-007/009/010 |
| S-13 My History | **None** (B7 summary-timing behavioral — see §7) | R-P-008, R-A-010 |
| S-14 Web/Phone defaults | **Additive** (new provider/model options in editors) | R-P-010, R-C-001 |
| S-15 Phone numbers view | **Additive** (content source: Chime→Twilio, C-4) | R-A-002 |
| S-16 ASR test tool | **Decided — (e) FINAL 2026-11-06**: retire at cutover (route + `AsrTestView.vue` + `test_transcribe_signing` removed, U-501); not baseline-blocking | R-T-015 (test inventory) |
| S-17 SPA shell | **None** | R-P-007 |
| S-18 Tool registry view | **None** | R-P-004 |

**For the architect (contract constraints, not designs):** (1) `/api/config` must stay shape-compatible (forward-compatible additions only — §3 C-1/C-2); (2) Twilio webhook/media error semantics (503/403/1011/1008 codes above) are caller-visible and test-pinned; (3) ws-token + guest-token flows must survive any auth swap; (4) `bedrock_id` and `voice_connectors` field names are legacy shape warts that must not be "cleaned up" in this migration.

---

## 7. B7 摘要最終一致 S 面口徑（V9）

Context: Azure moves post-call summarization from in-process `asyncio.create_task` with a 30 s budget (bot.py:1393, 1400–1401) to Service Bus `postcall-jobs` + worker (`docs/azure/pipeline-infra-spec.md` §2.2). Summary availability changes from "within ~30 s of hang-up" to **eventually consistent** (breaking change B7, `docs/azure/bedrock-to-azure-mapping.md` §7.2), and a 30 s summarizer timeout changes from a `failed` terminal outcome to a **transient retryable failure** (permanent failure still lands in `failed`; error-as-data preserved per bot.py:1408–1415). This section defines the S-side (admin console / history views) acceptance口径 for V9. Code untouched.

### 7.1 S 面三態顯示定義與狀態機

**Wire values do not change.** `summary_status ∈ {pending, ok, failed}` is an open enum and a schema contract with the SPA (`static/admin/src/i18n/enums.js:5–11`). Companion fields unchanged: `summary` (structured object), `summary_error` (truncated ≤500 chars, error-as-data). Naming note for the implementers: `pipeline-infra-spec.md` §2.2 step 1 says "已 `done`" — that refers to the **existing `ok` value**; do not introduce a fourth value.

**Display mapping (already implemented in the SPA today):**

| State | Display (en / zh-CN) | Rendering | Evidence |
|---|---|---|---|
| `pending` | 生成中 / Generating | MyHistory: skeleton block + hint "摘要生成中…" / "Summary generating…"; History drawer: warning alert "[未生成] (生成中)" + list chip tone `warning` | MyHistoryView.vue:144 (`summary-skel`), locales zh-CN.js:859/en.js:850 (`pendingHint`); HistoryView.vue:182–195, 397–398 |
| `ok` | 已生成 / Generated | Structured summary block (`SummaryBlock`), chip tone default | HistoryView.vue:179–181; `_HistorySummary.vue` |
| `failed` | 失败 / Failed | Error alert + `summary_error` text (fallback "未知错误"/"Unknown error") + 重新生成 button | MyHistoryView.vue:148–150; HistoryView.vue:184–203; locales zh-CN.js:860–861/en.js:851–852 |

**State machine (product level):**

```text
call ends ──write_outcome_row──▶ pending            (bot.py:1323)
pending ──worker success───────▶ ok   (summary written; summary_error cleared)
pending ──permanent failure OR──▶ failed (+summary_error; terminal until re-run)
          retry exhaustion (last delivery; §2.2 + I-4)
pending ──transient failure /    (no visible change — still 生成中)
          queueing / retrying──▶ pending
failed  ──re-summarize─────────▶ pending ──▶ ok | failed
pending ──re-summarize (stale)─▶ pending (job re-enqueued)
ok      ── no re-run entry (button hidden) ──▶ terminal
```

Invariants the S 面 relies on (assert in V9):
- I-1: At any read, a post-BYE row is in exactly one of the three states (never "stateless").
- I-2: `ok` ⇒ `summary` present ∧ `summary_error` absent; `failed` ⇒ `summary_error` presented (error-as-data); `pending` ⇒ neither rendered.
- I-3: Transitions only per the machine above; a late/duplicate worker delivery never flips `ok` or `failed` back to `pending` without a re-summarize action (R-A-010 idempotency).
- I-4 (硬性不變式，裁決 2026-11-09 採納): **Every terminal path lands in `ok` or `failed`.** No row may sit in `pending` indefinitely — on the **last delivery** (`delivery_count == maxDeliveryCount`, §2.2 maxDeliveryCount=5) a still-failing job writes `failed` + `summary_error` → complete (no further abandon; 已裁決補入 infra spec §2.2，2026-11-09). DLQ 殘餘 poison 由 U-406 runbook 清掃：殘留 `pending` 超 A-3 上限（15 分鐘）一律翻 `failed`。

### 7.2 驗收斷言（V9）

**N for "掛斷後 N 分鐘內摘要可見":** 建議初值 **N = 5 分鐘**（正常路徑、無重試）。This is an acceptance hypothesis, not an SLO: the final N must be V9's measured end-to-end latency distribution (掛斷→摘要可見, p95) rounded up, and recorded in `docs/azure/acceptance.md` via G-4 before release (R-T-016 forbids invented numeric SLOs).

| # | Assertion | Suggested threshold |
|---|---|---|
| A-1 | 掛斷後首次讀到的行即為 `pending`（三態之一） | immediate |
| A-2 | 正常路徑：摘要在 N 分鐘內可見（行翻 `ok`，刷新後渲染） | N = 5 min (→ V9 p95, G-4) |
| A-3 | 含暫時性失敗重試：行在上限內到達終態（`ok` 或 `failed`） | ≤ 15 min (same provenance) |
| A-4 | 永久失敗或重試耗盡 ⇒ `failed`，且 `summary_error` 可見；**禁止**超 A-3 上限仍顯示「生成中」 | ties to I-4 |
| A-5 | 排隊中 / 執行中 / 重試中 — S 面顯示一致（皆「生成中」），不暴露內部階段、不承諾 ETA | display-only |
| A-6 | 失敗態文案 = 既有 `failedTitle` + `summary_error`/`failedFallback`；不新增錯誤文案體系 | §7.1 table |

超時/排隊中顯示什麼: the generic 「生成中 / Summary generating…」 pending state — one label for all internal stages (queued, running, between-retries). 用户失败提示文案: 「摘要生成失败」/「Summary generation failed」 + the `summary_error` detail or 「未知错误」/「Unknown error」, with the 重新生成 action available.

### 7.3 SPA 是否新增「處理中」指示 — **不需要**

理由（evidence-based）:
1. The three-state indicator **already exists**: `pending` renders as "生成中" (skeleton + `pendingHint`, warning alert, chip tone `warning`) in both MyHistoryView and HistoryView (§7.1 evidence column). B7 changes only the *duration* of `pending` (seconds → minutes), not its rendering.
2. `summary_status` lives in **history row payloads**, not `/api/config` — so this is outside the C-1 additive boundary and the C-2 `models[]`/config contract boundary entirely. No config field, no WS event, no new endpoint is involved. **No C number is consumed by the indicator.**
3. Forward-tolerance is already proven: `summarizeCurrent` patches rows with `next.summary_status ?? …` / `next.summary ?? …` and falls back to re-fetching detail (HistoryView.vue:590–604), so a summary-less response renders as "生成中" with zero code change.

Optional, non-contractual polish (cosmetic only; adopt or drop without any C registration): pending-hint copy could add "完成后刷新可见" and/or the detail drawer could auto-refetch while `pending` (e.g. 30 s interval, capped) to reduce manual refresh under eventual consistency. Explicitly **no** new WS events, polling contract, or `/api/config` field.

### 7.4 re-summarize（管理端重跑）在最終一致語義下的口徑

Today: synchronous (bot.py:4060–4133) — awaits the summarizer inline, returns 200 `{call_id, summary, summary_status:"ok"}` or 500 `{error:…}`; button shown only when `summary_status !== 'ok'` (HistoryView.vue:126); success clears `summary_error` (bot.py:4115).

**已裁決採納（2026-11-09）：異步投遞後狀態翻轉**（與 infra spec §5 「mi-control = Service Bus Data Sender（re-summarize）」對齊；單一 summarization 路徑 ⇒ 單一重試/冪等語義）:

- `POST /api/admin/history/{call_id}/summarize` → **200 `{call_id, summary_status:"pending"}`** (accepted; row flips to 生成中), worker completes to `ok`/`failed` per §7.1. Enqueue/persist failures keep today's 500 `{error:…}`. Rule preserved: re-run entry only from non-`ok` states.
- SPA impact: **zero code change** (verified tolerance, §7.3 point 3 — the `summary` field is simply omitted and `summary_status:"pending"` drives the chip). One copy-only tweak: success toast 「摘要已更新」/「Summary updated」 → 「已提交生成」/「Generation started」.
- Acceptance assertions:
  - R-1: On response return, the row/`chip` reads 生成中 (`pending`).
  - R-2: Reaches a terminal state within N (same threshold as A-2/A-3).
  - R-3: Double-clicking, or overlapping with a still-queued post-call job, never double-writes or oscillates state (idempotent per job; I-3).
  - R-4: `failed` → re-run → `pending` → `ok|failed` loop works repeatedly; `ok` keeps no re-run entry (button stays hidden).
- **已否決備選（在案備份，2026-11-09 裁決）— 同步等待**: contract stays byte-identical to today (200 with `summary`/`summary_status:"ok"`); the only product-visible difference is that a transient LLM failure at click time lands `failed` immediately (no auto-retry) — acceptable, admin can click again. **QA 若發現 SPA 契約紅可回退至此 fallback。** **C-9 已生效（2026-11-09 裁決，非 conditional）** = "re-summarize response semantic: `summary` omitted + `summary_status:"pending"` on acceptance (additive/behavioral; SPA-verified tolerant; copy tweak only)".
