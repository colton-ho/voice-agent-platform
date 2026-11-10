# Repository Discovery — AWS→Azure Voicebot Migration (Phase 3 Deliverable 1)

Author: 高见远 (solution-architect) · Status: draft for review
Target spec: `docs/Azure_Voicebot_Implementation_Instructions_Corrected.md` v2.0 §3 (9-item checklist), cross-referenced to §5 interface list and §11 config contract.
Repository: `/Users/colton/Developer/voice-agent-platform` — verified 2026-06-10 by direct file inspection. Every claim below carries `file:line` evidence; anything not directly observable is marked **unverified**.

---

## 0. Method & evidence rules

- Inspected: `bot.py` (7,403 ln), all root modules (`asr_multilang.py`, `mcp_sigv4.py`, `mcp_config.py`, `*_store.py`, `call_control_tools.py`, `twilio_sig.py`, `runtime_config.py`, `tools/registry.py`, `smoke_*.py`), `deploy/*`, `voice-server/src/*`, `tests/` (46 files), `data/` (8 scenarios), `README.md`, `pipecat/` (vendored source copy).
- Environment probes actually executed: `python3 --version`, `python3 -c "import pipecat"` (both from repo root and from `/tmp`), `pip show pipecat-ai`, `node --version`, searches for lockfiles/venvs.
- Rules applied: no invented SDK methods, model IDs, or versions; every observed fact is cited; stale docs are flagged where they contradict code (e.g. README "通话内容不持久化" vs `HistoryRecorder`).
- Not verified here (out of reach of this pass): live AWS/Twilio behaviour, real phone numbers, actual deployed EC2 state, billing, Twilio console configuration, the external Connect Flow Lambda code.

---

## 1. Entry points, framework, runtime, packaging, startup (spec §3.1)

| Item | Observed | Evidence |
|---|---|---|
| App entry | Single `FastAPI` app in `bot.py`; `uvicorn.run(app, host, port)` default `0.0.0.0:7860` | bot.py:7398–7403; docstring bot.py:7–8 |
| Framework | FastAPI + Starlette WS + pipecat pipeline runtime (`PipelineRunner`), loguru logging | bot.py:28–33, 59–61, 7095 |
| Second process | `voice-server` Node/TS SIP+RTP bridge (only for Chime ingress) | voice-server/src/server.ts; README:24–29 |
| Python version | Deploy installs **Python 3.12**; local workspace runs 3.13.12; README instructs `python3.12 -m venv` | cloudformation.yaml:400,424; README:73–74 |
| Node version | Node **20** (nodesource setup_20.x) in deploy; `voice-server/package.json` has **no `engines` field** | cloudformation.yaml:403–404; voice-server/package.json |
| Packaging / lockfile | **No root `requirements.txt`, `pyproject.toml`, or lockfile** (uv.lock/poetry.lock/Pipfile absent). Runtime deps installed ad-hoc by pip in UserData | find over repo (none); cloudformation.yaml:426–429; README:75–78 |
| Pipecat version (A-4) | **Pinned `pipecat-ai==1.4.0`** by CFN UserData and enforced in code (`PINNED_PIPECAT = "1.4.0"`, version-guard warns on drift). Local README install is *unpinned*. **Not installed in the current dev environment** (`pip show pipecat-ai` → not found; `import pipecat` from `/tmp` → ModuleNotFoundError). From repo root `import pipecat` resolves to an empty **namespace package** over the vendored `pipecat/` dir (import trap — no `__init__.py`, `__file__ = None`) | cloudformation.yaml:427; bot.py:6090, 6143–6149; README:76–78 |
| Vendored `pipecat/` | Upstream source copy, reference-only. Its `CHANGELOG.md` top entry is **[1.1.0] – 2026-04-27** (source vintage 1.1.x); version is dynamic (setuptools_scm), no `.git` → unresolvable. Excluded from the deploy tarball (`--exclude='pipecat'`); prod imports pip `pipecat-ai` from site-packages | pipecat/CHANGELOG.md:10; pipecat/pyproject.toml:5–7; pipecat/src/pipecat/__init__.py:12; deploy/deploy.sh:70; asr_multilang.py:8–13 (documents the local≠prod trap) |
| Container/system startup | Two systemd units written by UserData: `voicebot.service` → `python bot.py --host 0.0.0.0 --port 7860`; `voiceserver.service` → `node dist/server.js`. No Dockerfile, no container runtime. | cloudformation.yaml:520–528, 539–547, 558–561 |
| SPA | Vue 3 + Naive UI admin SPA served at `/` via StaticFiles (built by deploy.sh, dist shipped in tarball) | bot.py:7387–7395; deploy.sh:48–58 |

**A-4 resolved:** verified installed/pinned version = **pipecat-ai 1.4.0** (deployment truth: CFN pin + code guard). Gap remains: no local install or lockfile; the vendored source (1.1.x vintage) must **not** be used to judge 1.4.0 APIs.

**Vendored pipecat Azure adapters present** (relevant to Phase A wrap-vs-reuse): `AzureSTTService` (services/azure/stt.py:62), `AzureTTSService`/`AzureHttpTTSService` (tts.py:242, 739), `AzureLLMService` (OpenAI-compatible, llm.py:25), `AzureRealtimeLLMService` (realtime/llm.py:30). Serializers: `TwilioFrameSerializer` (serializers/twilio.py:32). Whether the *installed 1.4.0* wheel ships identical signatures is **unverified** (no install here) — must be checked against 1.4.0 at Phase B start.

---

## 2. AWS runtime dependencies — module-by-module (spec §3.2)

Application code imports `boto3` directly (bot.py:26) plus `botocore` (bot.py:2715, 3345). `aioboto3` is listed in deploy install (cloudformation.yaml:428). No SQS/SNS/EventBridge anywhere. No app-level CloudWatch SDK.

| Module | AWS surface (evidence) | IAM / signing | Env vars | Persistence |
|---|---|---|---|---|
| `bot.py` | `AWSBedrockLLMService` (import :73; built :2371–2387, :5659–5672 summary, :5766 translate); `AWSNovaSonicLLMService` (:74, :2154–2167, model `amazon.nova-2-sonic-v1:0` :2160); `AWSTranscribeSTTService` (:75, :2362–2369); `SimplePollyTTSService` custom Polly (:750–824, `polly.synthesize_speech` :793); Chime SDK Voice admin listing `chime-sdk-voice` `ListVoiceConnectors`/`ListPhoneNumbers` (:3325–3421, `CHIME_REGION="us-east-1"` :3322); DynamoDB `HistoryRecorder` (:1254–1421) + metrics scan (:6679); frozen-credential pattern `boto3.Session(...).get_credentials().get_frozen_credentials()` (:2347, :2122, :5658, :5767, :6392) | Credentials passed into pipecat AWS services as key/secret/token (:2364–2366, :2383–2385) | `AWS_REGION` (fallback `us-east-1`) :2346 etc.; `DDB_REGION` :1432; `HISTORY_TABLE`/`HISTORY_TTL_DAYS`/`HISTORY_DISABLED` :1227–1229; `PHONE_*` :2687–2693; `MINIMAX_*` :1790–1792, :872; `TWILIO_*` :7134–7136, :7203; `AUTH_SECRET`/`AUTH_TTL_HOURS` :155/:162; `ADMIN_PASSWORD` (bootstrap seed only :2710–2712); `MCP_CFG_PATH_OVERRIDE` :2706; `PHONE_TOOLS_ENABLED` :1234 | DDB history table (§7) |
| `asr_multilang.py` | **First-party Transcribe SigV4 presigned-URL signer** (stdlib hmac/hashlib only; endpoint `wss://transcribestreaming.{region}.amazonaws.com:8443` :114); adds identify-language/multi params. Fed into stock pipecat via module-seam monkeypatch of `pipecat.services.aws.stt.get_presigned_url` (bot.py:6093–6183) | SigV4-HMAC-SHA256 chain :177–200 | (creds passed in) | none |
| `mcp_sigv4.py` | SigV4 auth for MCP streamable-HTTP (e.g. Bedrock AgentCore): `botocore.auth.SigV4Auth` + `AWSRequest` (:85–86), creds from boto3 default chain (:49–63, `MissingCredentialsError` degrade :41–46). Default service `bedrock-agentcore`, region `us-east-1` (:37–38) | SigV4 request signing :66–115 | — | none |
| `mcp_config.py` | MCP server registry (file `config/mcp_servers.json`, gitignored). `ALLOWED_AUTH_TYPES = none/header/sigv4`; sigv4 defaults service `bedrock-agentcore`, region `us-east-1` (:60–64). Secret headers masked `***` | delegates to mcp_sigv4 | `MCP_CFG_PATH_OVERRIDE` (via bot.py:2706) | JSON file on disk |
| `user_store.py` | DynamoDB account table; sync boto3 resource in `asyncio.to_thread` (:42–47, :117–123). bcrypt hashing (cost 12) | default credential chain | `USERS_TABLE` (default `genaiic-voicebot-users`), `DDB_REGION`/`AWS_REGION` | PK `username` |
| `demo_store.py` | DynamoDB demo configs; scan cache, `put` writer (:51–59, :91–104) | default chain | `DEMOS_TABLE` (default `genaiic-voicebot-demos`) | PK `id` |
| `voice_store.py` | DynamoDB editable voice registry, `seed_if_empty` (:47–55, :76–99) | default chain | `VOICES_TABLE` (default `genaiic-voicebot-voices`) | PK `provider`, SK `voice_id` |
| `activity_store.py` | DynamoDB ops-audit log, default-deny redaction (:57–68, :181–198) | default chain | `ACTIVITY_TABLE` (default `genaiic-voicebot-activity`), `ACTIVITY_LOG_TTL_DAYS` | PK `day`, SK `ts_id`, TTL |
| `call_control_tools.py` | No AWS imports itself, but its phone side-effects target the DDB history row consumed by an **external "Connect Flow Lambda"** (docstring comments :5–7, :113–116) | — | — | via callbacks |
| `twilio_sig.py` | No AWS. Stdlib HMAC-SHA1 Twilio signature verification, "deliberately does NOT depend on the `twilio` SDK" (module docstring :1–21) | — | — | none |
| `runtime_config.py` | No AWS. Atomic JSON file `config/runtime.json` for web/phone defaults (docstring :1–17) | — | — | local file |
| `tools/registry.py` | No AWS. Tool catalog (`end_call`, `transfer_to_human`) + policy blurbs (header :1–30) | — | — | none |
| `smoke_bedrock.py` / `smoke_transcribe.py` / `smoke_polly.py` / `smoke_minimax.py` | Single-service connectivity probes; Bedrock/Transcribe/Polly via boto3+pipecat (:14–55 per file); MiniMax via HTTP | frozen creds (smoke_transcribe.py:55) | `AWS_REGION`, `BEDROCK_MODEL_ID`, `MINIMAX_*` | none |
| `scripts/migrate_demos_to_ddb.py` | DynamoDB migration of `data/*/manifest.yaml` into demos table (docstring :43) | default chain | `DDB_REGION` | writes demos table |
| `scripts/latency_report.py`, `latency_log.py`, `latency_observer.py`, `timeout_filler.py`, `tool_filler.py`, `asr_filter.py` | No AWS SDK (latency/observability + prompt-filler + ASR hallucination filter, pure logic) | — | `LATENCY_LOG_PATH`, `TOOL_FILLER_ENABLED`, `ASR_FILTER_*` (asr_filter.py `_env_*`) | local log file |
| `deploy/deploy.sh` | Builds SPA, tars code (**excludes `pipecat/`, `.env`, `config/runtime.json`** :66–80), uploads to S3 bucket `<stack>-deploy-<acct>-<region>`, CFN deploy | operator AWS CLI creds | `STACK_NAME`, `REGION` (default us-east-1), `BEDROCK_REGION`, `CF_PREFIX_LIST_ID` (hard-coded `pl-3b927c52` :22) | S3 artifacts |
| `deploy/cloudformation.yaml` | EC2 (t3.medium) + CloudFront + IAM; UserData: apt python3.12 + Node 20, `aws s3 cp` tarball :420, venv `pip install 'pipecat-ai[aws,aws-nova-sonic,silero,websocket]==1.4.0' boto3 aioboto3 aiohttp python-dotenv fastapi uvicorn` :426–429, SecretsManager `MinimaxSecret` :103–108 (resolved into `.env` at boot :434–436), systemd units :520–561. IAM includes `CloudWatchAgentServerPolicy` :123 (agent install itself **not observed** in UserData — unverified) | EC2 instance role | `MinimaxSecretArn` etc. | — |
| `deploy/chime_provisioner_handler.py` | CFN custom-resource Lambda driving `chime-sdk-voice`: create/reuse VC, origination→EC2:5060, associate owned numbers; NEVER orders/releases numbers (:1–25, `REGION="us-east-1"` :27). Inline copy in cloudformation.yaml guarded by `test_chime_provisioner.py` | Lambda role | — | — |
| `voice-server/` | No AWS SDK. SIP UAS + RTP; connects to bot over local WS (`PIPECAT_WS_URL`) | — | `PUBLIC_IP`, `RTP_PORT_BASE`, `RTP_PORT_COUNT`, `PIPECAT_WS_URL` (README:244–251) | none |

**Queues/async:** none in-repo. Post-call summary is an in-process `asyncio.create_task` (bot.py:1393). The only queue-like consumer is the **external Connect Flow Lambda** (outside repo, see §6). **Monitoring:** no app metrics SDK; loguru→stdout→systemd journal/`/var/log/voicebot.log` (cloudformation.yaml:528–536); `tools/registry.py:249` only mentions CloudWatch as a log destination. Service-side metrics viewed manually (`aws cloudwatch get-metric-statistics --namespace AWS/Bedrock`, docs/onboarding/03-runbook.md:132).

---

## 3. Telephone ingress (spec §3.3)

Two parallel PSTN ingresses share one session kernel `_run_phone_session` (bot.py:6981–7115):

**A. Chime SDK Voice Connector → SIP/RTP → voice-server → WS `/phone/ws`**
- Wire: SIP INVITE **UDP 5060** (voice-server/src/sip/sip-server.ts:80; server.ts:35), RTP pool **10000–10999** (`RTP_PORT_BASE=10000`, `RTP_PORT_COUNT=1000`, server.ts:19–20, README:249–250), G.711 **PCMU/8000** (sdp-parser.ts:80; rtp-session.ts:5,13 `SAMPLES_PER_PACKET=160` = 20 ms), μ-law↔PCM via `alawmulaw` (package.json:11; rtp-session.ts:10,160–184).
- Audio path: native 8 kHz PCM uplink to bot (`CHIME_SAMPLE_RATE=8000`, consts.ts:8) — pipeline path feeds 8 kHz to Transcribe directly; nova-sonic path upsamples 8 kHz→16 kHz via `UpsamplingPCMSerializer` (bot.py:7004–7067; `PHONE_WIRE_SAMPLE_RATE=8000` bot.py:284–286). Downlink 24 kHz PCM → resampled to 8 kHz for RTP (audio-utils.ts:33–38).
- No auth on `/phone/ws` (bridge-internal, README:145). call_id = `phone-<ms>` (bot.py:6971).
- Chime VC provisioning is a CFN custom resource (chime_provisioner_handler.py); **number procurement is manual console work** (README:307–317 — operational prerequisite, not code). VC region hard-coded `us-east-1` (bot.py:3320–3322; chime_provisioner_handler.py:27).

**B. Twilio → `POST /twilio/incoming-call` → TwiML `<Connect><Stream>` → WSS `/twilio/media-stream`**
- Webhook verifies `X-Twilio-Signature` via stdlib `twilio_sig.py` (bot.py:7180–7186); 503 unconfigured, 403 bad signature, 200 TwiML with `wss://<TWILIO_PUBLIC_BASE_URL host>/twilio/media-stream` + `caller` Parameter (bot.py:7154–7198).
- WS reads `connected`/`start` handshake (`TWILIO_START_TIMEOUT` 10 s, bot.py:7203–7233), then pipecat `TwilioFrameSerializer` (μ-law 8 kHz ↔ pipeline rates, interruption `clear`, optional REST auto-hang-up when callSid+accountSid+authToken present) (bot.py:7266–7277). call_id = `twilio-<streamSid>` (bot.py:7279).
- **No Twilio status-callback endpoint exists today** (only the two routes above). No TwiML `<Dial>`/`<Say>` anywhere — no blind-transfer TwiML today.
- Env: `TWILIO_AUTH_TOKEN`, `TWILIO_PUBLIC_BASE_URL` (enable gate), `TWILIO_ACCOUNT_SID` (optional, auto-hang-up only) (bot.py:7132–7142). README:203–214 (console wiring = operational prerequisite).

Web voice ingress `WS /ws` is separate (16 kHz PCM16 in / 24 kHz out, bot.py:281–282) and not telephony.

---

## 4. Speech / LLM / providers / prompts / tools / languages / audio (spec §3.4)

**Engines** (`ENGINES` bot.py:903–906): `pipeline` = Transcribe STT → Bedrock LLM → MiniMax/Polly TTS; `nova-sonic` = Nova Sonic v2 S2S. **`DEFAULT_ENGINE = "nova-sonic"`** (bot.py:907; phone default `PHONE_ENGINE` likewise README:181).

**LLM model IDs** (`MODELS` bot.py:469–476 — Bedrock inference-profile IDs, us-east-1 stack rationale comment :465–468):

| key | Bedrock ID |
|---|---|
| nova-2-lite (DEFAULT :482) | `us.amazon.nova-2-lite-v1:0` |
| nova-lite | `us.amazon.nova-lite-v1:0` |
| nova-micro | `us.amazon.nova-micro-v1:0` |
| claude-haiku-4.5 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| claude-sonnet-4.5 (TRANSLATE_MODEL :488) | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| claude-sonnet-4.6 | `us.anthropic.claude-sonnet-4-6` |

Claude-only prompt caching flag (bot.py:2376–2380). Summary + translate + re-summarize are direct Bedrock Converse calls (bot.py:5624–5679, :5766).

**Nova Sonic v2**: `AWSNovaSonicLLMService`, model `amazon.nova-2-sonic-v1:0` (bot.py:2160), voices `NOVA_SONIC_VOICES` 16 entries (bot.py:876–894) + aliases (bot.py:897), polyglot tiffany/matthew.

**STT**: `AWSTranscribeSTTService` with `Settings(language=lang["stt"])` (bot.py:2362–2369); multi-language A/B harness `/asr-test/ws` uses `_MultiLangTranscribeSTT` seam-patched presigner (bot.py:6093–6183; asr_multilang.py).

**TTS**:
- MiniMax (external, non-AWS): `SimpleMiniMaxTTSService` (bot.py:607–736) subclasses pipecat `MiniMaxHttpTTSService` (:76); SSE hex-PCM streaming; `MINIMAX_MODELS` = speech-2.8-turbo/hd (bot.py:868–872); ~30 curated `MINIMAX_VOICES` incl. Cantonese with full-width-paren IDs (bot.py:501+); pronunciation_dict Cantonese 「返」 fix (bot.py:661–662). Base URL `https://api.minimax.chat/v1/t2a_v2` (bot.py:1792). `MINIMAX_API_KEY` **required** at build (bot.py:1790).
- Polly: `SimplePollyTTSService` (bot.py:750–824) — custom because pipecat's bundled Polly "was not yielding audio frames" (comment :738–742); native 16 kHz PCM (constant :756); `POLLY_VOICES` 21 entries (bot.py:831–859). ⚠️ uses `aioboto3.Session()` (bot.py:783) but **no `import aioboto3` exists anywhere in app code** (repo-wide grep) — latent `NameError`; Polly path viability **unverified** (see §11 G-2b).

**Prompts/scenarios**: scenario presets live in DynamoDB (seeded from `data/<id>/manifest.yaml`, 8 dirs: `acme-security-support` (KB `kb.md`, seeded as synthetic user/assistant turn via `_kb_seed_messages` bot.py:1073–1102), `connect-repair`, `default`, `interviewer`, `it-helpdesk`, `it-support`, `sales`, `tutor`). Per-language system/greeting in `LANGUAGES` (bot.py:302–454) + demo overrides; `SUMMARY_PROMPTS` (bot.py:1107–1125).

**Languages** (`LANGUAGES` bot.py:302–454, quote of structure): 4 business languages — `zh-HK` (stt `Language.ZH_HK`, engines `[pipeline]`), `zh-CN` (`ZH_CN`, `[pipeline]`), `en-US` (`Language.EN`, `[pipeline, nova-sonic]`), `ja-JP` (`Language.JA`, `[pipeline]`) — plus `en-SG` (stt `Language.EN` workaround comment :348–351, `[pipeline]`) and 8 **Nova-only** locales (`en-GB`, `en-AU`, `en-IN`, `fr-FR`, `it-IT`, `de-DE`, `es-US`, `pt-BR`) with `stt: None, engines: ["nova-sonic"]` (:362–453). (A-3 FINAL 2026-11-06 — "preserve all, test the 4": every existing entry keeps working on both paths (en-SG included, zero product change); acceptance covers exactly the 4 business languages; the 8 Nova-only locales retire from phone defaults per (a).)

**Agent tools**: `tools/registry.py` registry (`end_call`, `transfer_to_human`, 4-language policy blurbs, per-demo allow-lists via `assemble_tools_schema`); MCP tools via pipecat `MCPClient` (bot.py:1909, 5181) with auth types none/header/**sigv4** (mcp_config.py:60); REST tools (admin, tests/test_admin_rest_tools.py); phone-scope gate `PHONE_TOOLS_ENABLED` (bot.py:1234).

**Audio formats per path** (verified): web `/ws` 16 kHz PCM16 in / 24 kHz PCM16 out (bot.py:281–282); phone wire 8 kHz PCM16 (bot.py:284–286); Twilio μ-law 8 kHz base64 via `TwilioFrameSerializer`; resampling in `voice-server/src/audio-utils.ts` (8↔16↔24 kHz) and pipecat serializers; no WAV headers (`add_wav_header=False`, bot.py:2357).

---

## 5. Session lifecycle & process model (spec §3.5)

- **State**: `ACTIVE_SESSIONS: dict[str, dict]` (bot.py:1138) — shape `{call_id, caller, started, emits: [primary+monitors], monitors: []}` (bot.py:1176–1183), guarded by `_SESSIONS_LOCK` (bot.py:1137). **In-memory only; single process** (uvicorn without workers, bot.py:7398–7403; one systemd unit). No cross-replica story — spec §8.2 R-A-014 gap.
- **Event fan-out**: `EventBroadcaster` (pipecat `BaseObserver`, bot.py:1528) emits `_WATCHED` frame events (bot.py:1537–1549) as JSON with `t` timestamps to `session_emits(call_id)` = primary WS + all `/monitor/ws` attaches (`_multi_emit` bot.py:1153–1165). Monitor attach/detach bot.py:1195–1215.
- **Lifecycle**: `/ws` (bot.py:5948) and `_run_phone_session` (bot.py:6981) both: read per-call config snapshot (mid-call edits don't affect the call, bot.py:7012–7021) → `HistoryRecorder.attach` → build engine pipeline → `PipelineRunner.run` → `finally`: close sessions/MCP clients, `session_unregister`, schedule `flush_turns_and_summarize`, `log_activity` call-end (bot.py:7094–7115).
- **call_id namespace**: `web-<ms>` (bot.py:5972), `phone-<ms>` (bot.py:6971), `twilio-<streamSid>` (bot.py:7279).
- **Callbacks**: Twilio has **no status callbacks** (§3); the only post-call callback story is the external Connect Flow Lambda reading the DDB outcome row after SIP BYE (§6).
- **Monitoring subscriptions**: `/monitor/ws` admin-role ws-token (bot.py:7292–7325); dashboard `/api/admin/metrics` aggregates from DDB history scan + ACTIVE_SESSIONS (bot.py:3430, 6679, 6753–6957).
- **Other local state**: `HISTORY_BUFFERS` (bot.py:1230, in-memory turn buffers), `config/runtime.json` + `config/mcp_servers.json` (single-node files), `_SESSIONS_LOCK` asyncio.

---

## 6. Transfer / handoff behaviour (spec §3.6)

- Current behaviour = **record intent + hang up** (NOT a carrier transfer): `transfer_to_human(topic)` handler (call_control_tools.py:164–193) → `mark_transfer` sets `transfer_requested=True`, `transfer_topic` on the history meta (bot.py:1290–1298) → emit tool event + `history_append` → after `GRACE_SECONDS=2.0` (call_control_tools.py:24) `write_outcome()` **awaited before** `EndTaskFrame` (call_control_tools.py:123–137; comment :113–116: "guarantees the row exists by the time a Connect Flow Lambda fires after the BYE"). `end_call` identical minus the transfer flags; single-flight `state["ending"]` guard (call_control_tools.py:121, 145–151).
- The **post-BYE "Connect Flow Lambda" is OUTSIDE this repository** (evidence: call_control_tools.py:113–116; bot.py:1300–1308 "downstream Connect Flow Lambda"; no Lambda code in repo except the Chime provisioner). Its Azure replacement is currently unowned (A-2).
- No TwiML `<Dial>`, no warm transfer, no conference, no agent queue in code (grep: no `<Dial` anywhere).
- Failure handling today: `write_outcome` failure is logged and the call still ends (call_control_tools.py:126–131); outcome write idempotent via `meta["_outcome_written"]` (bot.py:1312–1313); remote-hangup path re-covers the outcome row in `flush_turns_and_summarize` (bot.py:1349–1357). Busy/no-answer semantics do not exist (nothing to fail).
- Phone tool surface for this is scope-gated (phone passes DDB callbacks; web passes `None` — call_control_tools.py:103–112).

---

## 7. Data structures (spec §3.7)

Five DynamoDB tables (all sync boto3 resource + `asyncio.to_thread`, graceful degrade when table missing):

| Store | Env / default name | Key schema | TTL / retention | Evidence |
|---|---|---|---|---|
| users | `USERS_TABLE` → `genaiic-voicebot-users` | PK `username` (S) | none | user_store.py:11–15,47; fields username/password_hash(bcrypt cost 12)/role/created_at/disabled |
| demos | `DEMOS_TABLE` → `genaiic-voicebot-demos` | PK `id` (S) | none | demo_store.py:38–43; item = canonical demo dict (prompts per lang, kb_body, tool_ids, mcp_servers, filler, tags) |
| voices | `VOICES_TABLE` → `genaiic-voicebot-voices` | PK `provider` (S) + SK `voice_id` (S) | none | voice_store.py:17–21; providers minimax/polly/nova-sonic |
| activity | `ACTIVITY_TABLE` → `genaiic-voicebot-activity` | PK `day` (S, UTC `YYYY-MM-DD`) + SK `ts_id` (`<epoch_ms>#<uuid>`) | **TTL** per row (`ACTIVITY_LOG_TTL_DAYS`, default read at activity_store.py:68) | activity_store.py:27–30; default-deny `ACTIVITY_DETAIL_VALUE_ALLOWLIST` redaction |
| history (call log) | `HISTORY_TABLE` (env-required; empty ⇒ disabled bot.py:1229) | PK `call_id` (S) | **TTL attr `ttl`** = `started_at + HISTORY_TTL_DAYS*86400` (default 30 d) (bot.py:1228, 1316) | HistoryRecorder bot.py:1254–1421 |

History item: outcome row first (`call_id, caller, started_at, ended_at, duration_s, summary_status:"pending", ttl, transfer_requested, transfer_topic, web_user` — bot.py:1317–1333), then `update_item` adds `turns[]` (final transcript turns + tool_call events, `{who,text,t}`/`{kind,data,t}`), `turn_count`, `lang/engine/scenario/provider/voice/model/minimax_model` (bot.py:1363–1381), then `summary` + `summary_status` from async Bedrock summarization (bot.py:1395–1421).

- **Transcript persistence**: turns are persisted per-call into the history table when `HISTORY_TABLE` is set. README:364 "通话内容不持久化" is **stale** (predates `HistoryRecorder`). Web `/ws` calls persist too (web_user attribution bot.py:1330–1332). **No audio recording feature exists anywhere** (consistent with `RECORDING_ENABLED=false` default).
- **Business identifiers**: `call_id` (3 formats §5), `username`, demo `id`, `(provider, voice_id)`, activity `day/ts_id`, `web_user`, `caller` (E.164 string).
- **No S3 data persistence** — S3 used only for deploy tarballs (deploy.sh:84–85; cloudformation.yaml:420).
- Redis/SQS/ES: none.

---

## 8. Test inventory (spec §3.8) — 46 pytest files + 12 SPA specs

Classification per spec §3/§17.6: **RETAIN** = provider-independent; **ADAPT** = semantics keep, harness re-targets off moto/boto3; **REPLACE** = genuinely AWS-specific.

| Area | Files (count) | Disposition |
|---|---|---|
| A. Twilio telephony | test_twilio_sig, test_twilio_endpoints, test_twilio_integration (3) | **RETAIN** — pins the §2.3 wire contracts (503/403/200 TwiML, serializer μ-law/`clear`, handshake timeouts); Twilio stays the Azure baseline telephony |
| B. Phone session & audio | test_phone_session, test_phone_sample_rate (2) | **RETAIN** — `_run_phone_session` signature/serializer semantics (test_phone_session.py:108–174), 8 kHz wire tagging |
| C. Call control & tools | test_call_control_tools, test_call_control_scope, test_tool_registry (3) | **RETAIN** |
| D. Pure logic / latency / config | test_asr_filter, test_timeout_filler, test_idle_playout, test_latency, test_latency_log, test_latency_report, test_runtime_config (7) | **RETAIN** |
| E. MiniMax TTS | test_minimax_pronunciation (1) | **RETAIN** (gated on G-2 approval for MiniMax continuation) |
| F. MCP | test_mcp_config, test_mcp_runtime, test_admin_mcp_servers (3) RETAIN; test_mcp_integration (1) ADAPT; test_mcp_sigv4 (1) **REPLACE** → sigv4-degradation test (decision (c)) |
| G. Auth / setup / guest / per-user history | test_auth, test_setup, test_setup_integration, test_guest_link, test_history_api (5) | **ADAPT** — moto-backed DDB fixtures → storage-adapter contract tests; assertions preserved |
| H. Stores & persistence | test_activity_store, test_demo_store, test_voice_store, test_history, test_demo_db_integration, test_engine_languages, test_admin_api, test_migrate_demos (8) | **ADAPT** (moto/boto3 → Cosmos adapter); test_migrate_demos + `scripts/migrate_demos_to_ddb.py` **REPLACE** with §9.3 migration tooling |
| I. Admin API / demo surfaces | test_admin_dashboard, test_admin_rest_tools, test_demo_options, test_demo_translate, test_demo_loader, test_demo_loader_tools, test_engine_voice_patch (7) | **RETAIN** (contract pins; adapt moto-adjacent fixtures only) |
| J. AWS-provider-specific | test_nova_voices, test_chime_provisioner (2) | **RETIRE** pending decision (a) — Nova engine disposition; Chime dropped per requirements §3 out-of-scope |
| K. Phone numbers admin view | test_phone_numbers (1) | **REPLACE** — content source Chime→Twilio (decision (d)); envelope shape pin stays |
| L. Transcribe signing | test_transcribe_signing (1) | **REPLACE** — SigV4 presigner + seam-patch tests are Transcribe-specific; keep the *version-guard pattern* as a design input |
| M. Pre-existing pin drift (reclassified per CR-3) | test_tool_filler (1) | **ADAPT** — red on pipecat-ai 1.4.0 (`FunctionCallParams` gained `pipeline_worker`); it pins vendored 1.1.x fixture semantics, i.e. pre-existing drift vs the cloudformation.yaml:427 pin, not an Azure regression. Fixture-only fix (app code does not construct `FunctionCallParams`); must land by Phase B exit when the RETAIN set becomes the CI gate (R-T-015) |

Totals: 29 RETAIN · 13 ADAPT · 4 REPLACE/RETIRE (46). SPA specs in `static/admin/src/__tests__/` (12: asrTest, phoneNumbers, talkConfig, useEventLog, ws, …) all **RETAIN** (UI contract pins).

Representative sanitized call scenarios in `data/` (8): KB-grounded `acme-security-support` (multi-language `kb.*.md` in it-helpdesk too), `connect-repair` (manifest.yaml), sales/interviewer/tutor/it-support/default. Fake-websocket frame-sequence call fixtures live in tests/test_twilio_integration.py + tests/test_phone_session.py (README:240–242).

---

## 9. Known constraints & unknowns (spec §3.9)

| Constraint | Evidence |
|---|---|
| **us-east-1 hard-coding**: Bedrock model IDs are `us.*` profiles (bot.py:465–468); `CHIME_REGION="us-east-1"` (bot.py:3322); SigV4 defaults (mcp_sigv4.py:38, mcp_config.py:64); deploy defaults + CloudFront prefix list `pl-3b927c52` only valid in us-east-1 (deploy.sh:15–25; README:114); Nova Sonic availability note (docs/onboarding/03-runbook.md:21) | multiple |
| Data residency / subscription billing location | **UNKNOWN** (spec §16.3, D-3) |
| Approved third parties (MiniMax, Anthropic-hosted inference) | **UNKNOWN** — release gates G-1/G-2 (requirements.md §4) |
| Peak concurrency, max call duration, latency/availability SLOs, budget | **UNKNOWN** — D-6/G-4; no numeric values anywhere in repo (spec §15 forbids inventing) |
| Number countries/types, outbound destinations, handoff destinations allowlist | **UNKNOWN** — D-2/D-5; inbound-only observed (README:363) |
| Transcript/retention/consent policy | **UNKNOWN** — D-7/G-3; technical default `HISTORY_TTL_DAYS=30` exists but is not an approved policy |
| Twilio numbers/webhook actually live? | **unverified** (README:240–242: no real-call test possible from repo) |
| CloudWatch agent actually installed? | **unverified** (IAM policy only, cloudformation.yaml:123) |

---

## 10. Proposed file changes (initial) — adapted to THIS repo (spec §5)

Principle: **adapt, don't create a second application** (spec §5). `bot.py` stays the composition root; we add provider/config/storage seams and swap internals behind existing interfaces. Nothing below is implemented in Phase 3.

Cross-reference to spec §5 contracts → current home:

| §5 contract | Current implementation to wrap |
|---|---|
| `TelephonyAdapter` | `twilio_sig.py` + `/twilio/incoming-call` + `/twilio/media-stream` + pipecat `TwilioFrameSerializer` (bot.py:7154–7289) |
| `SpeechRecognizer` | `AWSTranscribeSTTService` call site bot.py:2362–2369 (+ seam-patch pattern bot.py:6093) |
| `LanguageModel` | `AWSBedrockLLMService` call sites bot.py:2371–2387, 5624–5679 |
| `SpeechSynthesizer` | `SimpleMiniMaxTTSService` / `SimplePollyTTSService` (bot.py:607–824) via `_build_tts` (bot.py:1757) |
| `RealtimeVoiceEngine` | `_build_nova_sonic_pipeline` (bot.py:2086) |
| `CallRepository` | `HistoryRecorder` (bot.py:1254–1421) |
| `SessionDirectory` | `ACTIVE_SESSIONS` + lock (bot.py:1137–1221) |

Proposed layout (minimal disruption):

```text
providers/                  # NEW package (spec §5 providers/ adapted to flat repo)
  telephony/twilio_adapter.py   # wraps existing twilio_sig + serializer wiring (behind TELEPHONY_PROVIDER)
  speech/azure_speech.py        # Azure STT/TTS adapters (pipecat AzureSTT/AzureTTS or HTTP)
  speech/minimax.py             # existing SimpleMiniMaxTTSService moved behind SpeechSynthesizer
  llm/azure_openai.py           # approved-endpoint adapter (LLM_PROVIDER/ENDPOINT/DEPLOYMENT/API_VERSION)
config_contract.py          # NEW: §11 names, startup validation, no-silent-fallback (R-C-001/002)
storage/                    # NEW: Cosmos adapters behind existing store interfaces (user/demo/voice/activity/history)
workers/                    # NEW: post-call Service Bus consumer (summary/artifacts) — Phase C/D
telemetry/                  # NEW: OTel setup + redaction (Phase C/D)
infra/azure/                # NEW: Bicep + params (Phase C)
deploy/aws-legacy/          # move (NOT delete) CFN/deploy.sh/chime handler at cutover — AWS path untouched now
```

Touched existing files (expected): `bot.py` (service construction sites + `_build_tts` + Twilio config gating → config_contract; keep all endpoint contracts byte-identical), the 5 store modules (internal implementation swap only), `mcp_config.py`/`mcp_sigv4.py` (sigv4 disposition per decision (c)), `voice-server/` (retired with Chime path at cutover — not modified before). Retained untouched: `tools/`, `call_control_tools.py`, `asr_filter.py`, `twilio_sig.py`, `runtime_config.py`, all RETAIN tests.

**Config mapping (old env → spec §11 contract)** — required by R-C-003:

| Current (evidence) | §11 name | Note |
|---|---|---|
| `AWS_REGION` / `DDB_REGION` | `AZURE_LOCATION`, `SPEECH_REGION` | us-east-1 hard-coding dies with AWS |
| Bedrock `MODELS` IDs (bot.py:469) | `LLM_PROVIDER`/`LLM_ENDPOINT`/`LLM_DEPLOYMENT`/`LLM_API_VERSION` | no default model — startup fails unconfigured (D-4) |
| Transcribe implicit config | `STT_PROVIDER=azure_speech`, `STT_LOCALE` | per-call `lang["stt"]` mapping stays business-side |
| MiniMax/Polly selection (`TTS_PROVIDERS` bot.py:862, `PHONE_PROVIDER`) | `TTS_PROVIDER`, `TTS_VOICE`, `SPEECH_ENDPOINT` | Polly retires; MiniMax = adapter behind G-2 |
| `PHONE_ENGINE=nova-sonic` default | `VOICE_ENGINE=cascade`, `VOICE_LIVE_ENABLED=false` | decision (a) |
| `USERS_TABLE`/`DEMOS_TABLE`/`VOICES_TABLE`/`ACTIVITY_TABLE`/`HISTORY_TABLE` | `COSMOS_ENDPOINT`/`COSMOS_DATABASE` | per-container mapping (§9.1) |
| `HISTORY_TTL_DAYS`, `ACTIVITY_LOG_TTL_DAYS` | `TRANSCRIPT_RETENTION_DAYS` (+ per-class retention) | §9.2 expires_at semantics |
| `MINIMAX_API_KEY`, `TWILIO_AUTH_TOKEN` | `TWILIO_AUTH_TOKEN_SECRET_NAME`, `KEY_VAULT_URL` | Key Vault references (R-A-011) |
| `TWILIO_PUBLIC_BASE_URL` | `PUBLIC_CONTROL_BASE_URL`, `PUBLIC_MEDIA_BASE_URL` | §6.1 external-URL signing |
| `AUTH_SECRET`/`AUTH_TTL_HOURS` (bot.py:155–162) | (retained app config) + Entra for humans (C-8) | guest tokens stay stateless |
| `HISTORY_DISABLED`, `PHONE_TOOLS_ENABLED`, `ASR_FILTER_*`, `TOOL_FILLER_ENABLED` | app-defined (keep) | behaviour switches, unchanged semantics |
| (none) | `MAX_CONCURRENT_CALLS_PER_REPLICA`, `MAX_CALL_DURATION_SECONDS`, `RECORDING_ENABLED=false`, `SERVICEBUS_NAMESPACE`, `REDIS_ENDPOINT`, `BLOB_ACCOUNT_URL`, `APPLICATIONINSIGHTS_CONNECTION_STRING` | new, §11 |

---

## 11. Compatibility gaps, assumptions, blockers (consolidated)

| # | Item | Evidence | Cross-ref | Impact |
|---|---|---|---|---|
| G-1 | **Pipecat API drift risk**: pinned 1.4.0 in deploy only; vendored source is 1.1.x-era; no lockfile; nothing installed in dev env; `asr_multilang` seam-patch couples to `get_presigned_url` internals (version-guarded) | cloudformation.yaml:427; bot.py:6090–6149; pipecat/CHANGELOG.md:10 | A-4 (resolved to 1.4.0 w/ gap), R-A-005 | Phase B must verify Azure adapter signatures against installed 1.4.0 before committing; add a lockfile |
| G-2 | **`SimplePollyTTSService` references `aioboto3` with no import anywhere in app code** — constructing it raises `NameError` unless something external injects the name; Polly path viability **unverified** | bot.py:783; repo-wide grep | R-P-010 | Polly already slated for replacement; do not port as-is; record as pre-existing defect |
| G-3 | **Multi-replica gap**: all session/monitor/history-buffer state in-process (global dicts + local files); no owner lease/command routing | bot.py:1138, 1230, 2696–2708 | R-A-014 (Phase D), spec §8.2 | Phases A–C single-replica is the validation config (D-9); do not claim HA |
| G-4 | **Handoff external system unowned**: post-BYE Connect Flow Lambda lives outside repo; Azure replacement has no owner | call_control_tools.py:113–116; bot.py:1300–1308 | A-2, R-P-005, D-5 | **A-2 FINAL (2026-11-06)**: replaced by in-repo Twilio transfer flows (spec §6.4), Lambda retired at cutover; R-P-005 acceptance = Phase C tests incl. no-answer/busy/failure; record+hangup preserved until then |
| G-5 | **MCP `auth.type=sigv4`** is an AWS-credential dependency (bedrock-agentcore default) | mcp_config.py:60–64; mcp_sigv4.py:37–38 | C-5, decision (c) | Degrade at connect time after AWS removal (already fail-safe: `MissingCredentialsError`) |
| G-6 | **`/api/admin/phone-numbers` content source is Chime** (`chime-sdk-voice`, bot.py:3325–3421) while baseline telephony is Twilio | bot.py:3320–3427 | C-4, decision (d) | Envelope pinned by SPA tests; re-source content only |
| G-7 | **Auth split**: JWT+bcrypt local users today vs Entra ID for admin/monitor (R-S-005); `/setup` bootstrap semantics collide with Entra | bot.py:141–216, 2820–2879 | C-8, decision (b) | The one approved breaking surface change |
| G-8 | **Twilio signature method (A-1)**: spec §6.1.2 mandates official SDK; repo uses audited stdlib HMAC-SHA1 | twilio_sig.py:1–21; tests/test_twilio_sig.py | A-1 | **A-1 FINAL (2026-11-06)**: official twilio SDK `RequestValidator` on the Azure path; `twilio_sig.py` kept as independent test oracle; `twilio` SDK enters at Phase C / U-201 |
| G-9 | **No Twilio status callbacks / fallback TwiML today** — spec §6.1.8–6.1.9 requires signed status callbacks + fallback route | §3 (only 2 Twilio routes) | R-A-002, C-6 | New work, additive |
| G-10 | **Region coupling**: us-east-1 in model IDs, Chime VC, SigV4 defaults, CFN prefix list | §9 table | D-3 | Azure region is config, not code |
| G-11 | **README drift**: "通话内容不持久化" contradicts `HistoryRecorder`; "5 LLMs" vs 6 in `MODELS` | README:364, 259–267 vs bot.py:469–476 | — | Docs corrected in later phases; code is truth |
| G-12 | **Manual/operational prerequisites** (not automatable from repo): Twilio console webhook config, Chime VC console setup, number procurement, secret values, subscription quotas | README:203–214, 307–317; chime_provisioner_handler.py:17–21 | §4 gates | Track as prereq items in Phase C checklist |

Assumptions recorded (not decided): D-1…D-9 temporary assumptions from requirements.md §5 stand unchanged; this discovery adds none of its own.

---

## 12. Architect decisions (a)–(e) — ADR register, FINALIZED (user-confirmed 2026-11-06)

| # | Question | Recommendation (one line) | Impact |
|---|---|---|---|
| (a) | Nova Sonic engine disposition | **Drop `nova-sonic` from the selectable engine set in the Azure baseline**; keep its capability slot as spec §5 `RealtimeVoiceEngine` mapped to future Azure Voice Live behind `VOICE_LIVE_ENABLED=false` — do not keep any AWS runtime path | `engines[]`→`[pipeline]`, `nova_sonic_voices` empties (C-3), `PHONE_ENGINE` default must move to `pipeline`, Nova-only locales (bot.py:362–453) become Voice-Live-gated/removed; test_nova_voices retires |
| (b) | `/setup` wizard under Entra ID | **Keep `/setup` through Phase C as local break-glass admin bootstrap; at Entra cutover demote it to seeding the local role mapping** — never leave `setup-status` dangling | setup/test_setup contracts preserved until Phase D; C-8 scope clarified |
| (c) | MCP `auth.type=sigv4` degradation | **Keep the option valid in config/UI, fail at connect with `{ok:false, error}` when AWS creds are absent** (existing `MissingCredentialsError` path), and drop the botocore dependency from the Azure image | test_mcp_sigv4 → degradation test; `mcp_sigv4.py` retires from runtime (C-5) |
| (d) | `/api/admin/phone-numbers` data source | **Re-target `_list_chime_vc_numbers` → Twilio IncomingPhoneNumbers behind the same `{voice_connectors, error}` envelope** (rows: `e164`/`status`, id/name → Twilio sid/friendly-name; error-as-200 preserved) | C-4; test_phone_numbers replaced with same-shape assertions |
| (e) | `asr-test/ws` tool | **Retire** — it exists solely to compare Transcribe single vs multi-language, a decision already made; re-target later only if an Azure STT A/B diagnostic is explicitly requested | Route + `AsrTestView.vue` + test_transcribe_signing removed at cutover; not baseline-blocking (S-16) |

**Status 2026-11-06 — USER DECISIONS CONFIRMED; entries (a)–(e) above are FINALIZED (binding on Phases B–F), plus these two open items resolved:**

| # | Finalized ruling (user decision, 2026-11-06) |
|---|---|
| (a) | Nova Sonic **DROP from baseline** (confirmed as recommended): `engines[]`→`[pipeline]`, `PHONE_ENGINE` default `nova-sonic`→`pipeline`, the 8 Nova-only locales (bot.py:362–453) retire from phone defaults; capability slot stays `RealtimeVoiceEngine` → future Azure Voice Live behind `VOICE_LIVE_ENABLED=false`; the AWS rollback path keeps `nova-sonic` untouched |
| (b) | Confirmed exactly as recommended — `/setup` break-glass through Phase C; demote to role-mapping seed at Entra cutover |
| (c) | Confirmed exactly as recommended — sigv4 option stays valid in config/UI, degrades at connect `{ok:false,error}`; botocore leaves the Azure image at cutover |
| (d) | Confirmed exactly as recommended — phone-numbers re-sourced to Twilio behind the unchanged `{voice_connectors, error}` envelope |
| (e) | Confirmed exactly as recommended — `asr-test/ws` + `AsrTestView.vue` retire at cutover |
| A-1 | **Official twilio SDK `RequestValidator`** on the Azure path; stdlib `twilio_sig.py` remains the independent test oracle; the `twilio` SDK dependency enters at Phase C / U-201 (not in Phase A requirements) |
| A-2 | External "Connect Flow Lambda" **REPLACED by in-repo Twilio transfer flows (spec §6.4)** and retired at cutover; R-P-005 acceptance = Phase C tests incl. **no-answer / busy / failure** |
| A-3 | **"Preserve all, test the 4"** — every existing `LANGUAGES` entry keeps working on both paths (en-SG included — zero product change, consistent with the no-visible-reduction rule); acceptance tests cover exactly the 4 business languages zh-HK / zh-CN / en-US / ja-JP per spec §15 (R-T-004); the 8 Nova-only locales follow decision (a) and retire from phone defaults with nova-sonic |

Register status: **ZERO open decision items** — (a)–(e), A-1, A-2 and A-3 all user-confirmed 2026-11-06 (A-4 resolved earlier = `pipecat-ai==1.4.0`); D-1…D-9 run on approved temporary assumptions (requirements §5). The CR-3 gap flag (pipecat 1.4.0 ↔ mcp 2.3.0 `streamablehttp_client`, live MCP path bot.py:1909) is folded into `docs/azure/implementation-plan.md` as Phase B must-fix **U-105** with an `import pipecat.services.mcp_service` smoke in the RETAIN CI gate.
