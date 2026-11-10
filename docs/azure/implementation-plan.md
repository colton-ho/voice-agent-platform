# Implementation Plan — AWS→Azure Voicebot Migration (Phase 4 Deliverable)

Author: 拆得细 (feature-planner, 功能计划拆分师) · Status: draft for review · Work language: English
Repository root: `/Users/colton/Developer/voice-agent-platform` (all paths below are repo-relative)
Frozen inputs: `docs/azure/requirements.md` (R-IDs, D-1..D-9, A-1..A-4) · `docs/azure/product-impact.md` (18 surfaces, ~40 pinned contracts) · `docs/azure/discovery.md` (§10 file plan, §11 gaps G-1..G-12, §12 ADR register (a)–(e) — user-finalized 2026-11-06) · `docs/azure/dependency-matrix.md` · target spec `docs/Azure_Voicebot_Implementation_Instructions_Corrected.md` (§5 interfaces, §11 config, §14 phases).
Evidence rules: every claim cites discovery/dependency-matrix rows or `file:line`; anything not observable is marked **unverified**. This plan is planning only — no application code was modified.

**Contract freeze (GLOBAL RULE 3):** the Phase A interface contracts FC-1..FC-8 and the provider-registry selection rules in §3 are frozen by this document. Any later deviation routes back through solution-architect (高见远) before implementation.

---

## 1. Phase mapping — USER phases A–F ↔ spec §14 Phases A–E

| USER phase | Scope (user's words) | Maps to spec §14 | Material difference (explained) |
|---|---|---|---|
| **A** | Provider interfaces + config validation | **spec Phase B, first half** ("interfaces and deterministic test fixtures") | Spec Phase A (discovery + decisions) is **DONE** — `discovery.md`, `dependency-matrix.md`, `requirements.md`, `product-impact.md` exist (R-A-021 satisfied). Our work therefore starts at spec Phase B. Spec Phase B also bundles "Azure adapters behind flags" and a local demo; the USER defers adapters to Phase B and keeps Phase A strictly contracts + registry + config validation. |
| **B** | Azure adapters + voice-call vertical slice | **spec Phase B, second half** ("Azure adapters behind flags", local streaming/interruption/lifecycle/idempotent tools) | Spec B expects adapter work *alongside* Twilio serializer work; the USER isolates the voice-engine vertical slice (Azure STT→LLM→TTS end-to-end locally) from all telephony wire work, which moves to Phase C. |
| **C** | Twilio webhook/audio/streaming/barge-in/handoff | **spec §6 (all of it) + spec Phase C's telephony half** | Spec spreads §6 across Phases B/C; the USER consolidates every Twilio-facing behaviour into one phase. Net-new spec features absent today (G-9: status callbacks, fallback TwiML, WS-upgrade signature check) and the §6.3 mark/clear ledger land here. |
| **D** | Persistence / post-call / session ownership / capacity | **spec Phase C's persistence half + spec Phase D first half (§8.1–8.3)** | Spec Phase C requires persistence inside the staging vertical slice; the USER defers Cosmos/worker/ownership until after the Twilio surface works (Phases A–C run on the existing store modules, consistent with D-8 "no migration in Phases A–C" and D-9 single-replica validation config). Call reservation/ownership bind (§6.1.5/§8.2) is placed here. |
| **E** | Infra / CI-CD / telemetry / migration / runbooks | **spec Phase D bulk (§9.3, §12, §13) + §8.3 release mechanics** | Spec Phase D mixes ownership code with infra; the USER separates them. Bicep, GH Actions OIDC, OTel, migration tooling, blue/green drain, runbooks land here. |
| **F** | Automated tests + deployment-readiness | **spec Phase D tail + spec Phase E (prepared, not executed)** | Spec Phase E *executes* canary/cutover; USER Phase F ends at deployment-readiness evidence (acceptance matrix §15, `docs/azure/acceptance.md`, rollback rehearsal). Actual canary/cutover is authorization-gated (R-O-004, gate G-7) and is NOT inside F. |

Phase-level invariants that hold across all phases:
- All HTTP/WS endpoint contracts stay byte-identical (product-impact.md §2; `bedrock_id`/`voice_connectors` field names must NOT be renamed).
- Twilio error codes/TwiML (503/403/200/1011/1008) are test-pinned and must not change.
- Existing store interfaces (`user_store`/`demo_store`/`voice_store`/`activity_store` method signatures) are preserved; only internals swap in Phase D.
- `bot.py` remains the composition root — no second application (spec §5; discovery §10 principle).

---

## 2. Work breakdown — batches, dependencies, parallelism

### 2.1 Phase A batches (fully specified in §4)

```text
Batch 0   U-001  environment + pipecat-ai==1.4.0 pin + lockfile      (single owner)
             │
Batch 1   ┌──┴───┐
       U-002    U-003          ← run in parallel
       contracts  config
          │  └──┬──┘
Batch 2   U-004  provider registry (explicit selection)
             │
Batch 3   U-005  Phase A gate: 29-RETAIN-green + isolation + close-out
```

- **Batch 0 must land first** (team SOP: shared foundation). Nothing may claim pipecat SDK compatibility before U-001 (G-1).
- Batch 1 is genuinely parallel: U-002 (contracts) and U-003 (config) share no files and no imports.
- U-004 consumes both (it types factories against FC contracts and reads the §11 selection vars).
- U-005 is the phase gate and close-out (R-O-005).

### 2.2 Cross-phase skeleton batches (sequencing only, §5)

```text
A (U-001..U-005)
 └─ B:  U-101 ┐
      U-102 ─┼─ U-103 ──► (local vertical slice)
      U-104 ┘             (parallel to U-101/102)
      U-105 (MUST-FIX: pipecat↔mcp pairing + import-smoke CI gate — CR-3)
 └─ C:  U-201 ─► U-202 ─► U-204 ─► U-206
              └─ U-203 ─┘ (parallel to U-202 after U-201)
              U-205 (needs U-301 — cross-phase dependency, see note)
              U-207 (parallel, decision (d))
 └─ D:  U-301 ┐
      U-302 ─┼─ U-303
      U-306 ─┤
              └─ U-304
      U-305 (needs U-202 + U-301)
 └─ E:  U-401 ─► U-402 ;  U-403, U-404, U-405, U-406, U-407 (parallel where noted)
 └─ F:  U-501 ─► U-502 ─► U-503
```

Cross-phase edges that must not be missed:
- **U-205 (Twilio status callbacks) ← U-301 (CallRepository)**: §6.1.8 requires "persist important events durably before acknowledging" — durable writes need the repository. If strict phase order C→D is enforced by the user, U-205 moves to the front of Phase D; its code is otherwise ready in C.
- **U-305 (ownership/capacity) ← U-202 (stream reservation flow)**: reservation exists in C on a minimal single-replica `SessionDirectory` implementation; D upgrades the internals behind the same frozen contract (FC-7).
- **U-103 (slice wiring) ← U-001**: any code that constructs pipecat services must run in the pinned 1.4.0 environment.

---

## 3. Frozen Phase A contracts (single definition site)

Placements adapted from discovery §10 ("§5 contract → current implementation to wrap"):

| Frozen ID | spec §5 name | File (new) | Wraps (later phases; discovery §10) |
|---|---|---|---|
| FC-1 | `TelephonyAdapter` | `providers/base.py` | `twilio_sig.py` + `/twilio/incoming-call` + `/twilio/media-stream` + pipecat `TwilioFrameSerializer` (bot.py:7154–7289) |
| FC-2 | `SpeechRecognizer` | `providers/base.py` | `AWSTranscribeSTTService` call site bot.py:2362–2369 (replaced by Azure STT in B) |
| FC-3 | `LanguageModel` | `providers/base.py` | `AWSBedrockLLMService` call sites bot.py:2371–2387, 5624–5679 (replaced in B) |
| FC-4 | `SpeechSynthesizer` | `providers/base.py` | `SimpleMiniMaxTTSService`/`SimplePollyTTSService` via `_build_tts` bot.py:1757 (replaced in B) |
| FC-5 | `RealtimeVoiceEngine` | `providers/base.py` | `_build_nova_sonic_pipeline` bot.py:2086 → future Voice Live behind `VOICE_LIVE_ENABLED=false` |
| FC-6 | `CallRepository` | `storage/base.py` | `HistoryRecorder` bot.py:1254–1421 |
| FC-7 | `SessionDirectory` | `storage/base.py` | `ACTIVE_SESSIONS` + lock bot.py:1137–1221 |
| FC-8 | (helper) `SignatureValidator` | `providers/base.py` | pluggable so decision A-1 (SDK vs stdlib) blocks nothing |

No pipecat, boto3/botocore/aioboto3, or Azure SDK types appear in these signatures — adapters translate at the boundary. This is deliberate (G-1: 1.4.0 wheel signatures are **unverified** until U-001 runs).

### 3.1 Shared value types (`providers/base.py`)

```python
@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int                       # 8000 | 16000 | 24000 in this system
    encoding: Literal["pcm16", "mulaw"]
    channels: int = 1

@dataclass(frozen=True)
class TranscriptEvent:
    text: str
    is_final: bool
    language: str | None = None

@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    name: str | None = None

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters_schema: dict                # JSON Schema dict — same shape tools/registry.assemble_tools_schema emits

@dataclass(frozen=True)
class ToolRequest:
    id: str
    name: str
    arguments: dict

@dataclass(frozen=True)
class UsageReport:
    provider: str
    model: str
    input_units: int
    output_units: int
    raw: dict                              # provider-native counters preserved verbatim

@dataclass(frozen=True)
class ModelEvent:
    kind: Literal["delta", "tool_request", "usage", "end"]
    text: str | None = None                # kind in {"delta","end"}
    tool_request: ToolRequest | None = None
    usage: UsageReport | None = None

@dataclass(frozen=True)
class SynthesisChunk:
    audio: bytes
    format: AudioFormat
    is_final: bool = False
```

### 3.2 Provider-facing interfaces (`providers/base.py`)

Method sets follow spec §5 verbatim; no extra verbs.

```python
class SignatureValidator(Protocol):        # FC-8 — pluggable (decision A-1)
    def is_valid(self, *, url: str, params: Mapping[str, str], signature: str) -> bool: ...

class TelephonyAdapter(ABC):               # FC-1
    supports_transfer: bool                # capability flag (decision D-5/G-5)
    @abstractmethod
    def validate_webhook(self, *, url: str, params: Mapping[str, str], signature: str,
                         validator: SignatureValidator) -> bool: ...
    @abstractmethod
    def start_stream(self, *, stream_url: str, caller: str) -> str:
        """Provider handshake payload (Twilio: TwiML <Connect><Stream> document)."""
    @abstractmethod
    def decode_incoming_media(self, *, payload: Mapping[str, object]) -> bytes:
        """Provider media frame (Twilio: base64 μ-law/8 kHz) -> PCM16 bytes."""
    @abstractmethod
    def encode_outgoing_media(self, *, pcm: bytes) -> str:
        """PCM16 bytes -> provider outbound media payload (Twilio: base64 μ-law/8 kHz)."""
    @abstractmethod
    def clear_playback(self) -> Mapping[str, object]:
        """Provider message flushing queued playback (Twilio: `clear`)."""
    @abstractmethod
    def transfer(self, *, destination: str) -> str:
        """Provider message/TwiML performing the transfer (gated by D-5/G-5)."""
    @abstractmethod
    def hang_up(self) -> Mapping[str, object]: ...

class SpeechRecognizer(ABC):               # FC-2
    supports_language_detection: bool      # capability flag (spec §7.1)
    @abstractmethod async def start(self, *, language: str, input_format: AudioFormat) -> None: ...
    @abstractmethod async def push_audio(self, pcm: bytes) -> None: ...
    @abstractmethod def events(self) -> AsyncIterator[TranscriptEvent]: ...
    @abstractmethod async def stop(self) -> None: ...

class LanguageModel(ABC):                  # FC-3
    supports_tool_calls: bool
    @abstractmethod
    def stream(self, *, messages: Sequence[ChatMessage],
               tools: Sequence[ToolSpec] | None = None) -> AsyncIterator[ModelEvent]: ...
    @abstractmethod async def cancel(self) -> None: ...
    @abstractmethod def usage(self) -> UsageReport: ...

class SpeechSynthesizer(ABC):              # FC-4
    @abstractmethod
    def synthesize(self, text_stream: AsyncIterator[str]) -> AsyncIterator[SynthesisChunk]: ...
    @abstractmethod async def cancel(self) -> None: ...
    @abstractmethod def output_format(self) -> AudioFormat: ...

class RealtimeVoiceEngine(ABC):            # FC-5 — optional; not needed for baseline (spec §7.3)
    @abstractmethod async def start(self, *, context: Mapping[str, object]) -> None: ...
    @abstractmethod async def push_audio(self, pcm: bytes) -> None: ...
    @abstractmethod def events(self) -> AsyncIterator[TranscriptEvent | ModelEvent | SynthesisChunk]: ...
```

### 3.3 Persistence/coordination interfaces (`storage/base.py`)

```python
class CallState(str, Enum):                # spec §8.1 state machine
    RECEIVED, CONNECTING, ACTIVE, HANDOFF_PENDING, HANDED_OFF, ENDED, FAILED

@dataclass(frozen=True)
class CallRecord:
    call_id: str
    state: CallState
    started_at: datetime
    tenant_id: str | None = None
    provider_ids: Mapping[str, str] = None       # e.g. {"twilio_call_sid": ..., "stream_sid": ...}
    ended_at: datetime | None = None
    expires_at: datetime | None = None           # business retention boundary (spec §9.2)
    owner: str | None = None
    handoff_outcome: str | None = None
    version: str | int = 0                       # optimistic-concurrency token

@dataclass(frozen=True)
class CallEvent:
    event_id: str                                  # dedup key
    call_id: str
    event_type: str
    occurred_at: datetime
    payload: Mapping[str, object]                  # sanitized — no audio, no credentials (spec §9.1)

@dataclass(frozen=True)
class HistoryPage:
    items: Sequence[CallRecord]
    next_cursor: str | None

class CallRepository(ABC):                 # FC-6
    @abstractmethod async def create(self, record: CallRecord) -> CallRecord: ...
    @abstractmethod async def get(self, call_id: str) -> CallRecord | None: ...
    @abstractmethod async def update(self, call_id: str, changes: Mapping[str, object],
                                     *, expected_version: str | int) -> CallRecord: ...   # StaleVersionError
    @abstractmethod async def append_events(self, call_id: str, events: Sequence[CallEvent]) -> int: ...
        # dedup by event_id; returns appended count; CallFinalizedError on finalized call
    @abstractmethod async def finalize(self, call_id: str, *, outcome: Mapping[str, object],
                                       expected_version: str | int) -> CallRecord: ...
    @abstractmethod async def query_history(self, *, caller: str | None = None,
                                            tenant_id: str | None = None,
                                            cursor: str | None = None,
                                            limit: int = 50) -> HistoryPage: ...

@dataclass(frozen=True)
class Reservation:  call_id: str; expires_at: datetime
@dataclass(frozen=True)
class Lease:        call_id: str; owner_id: str; expires_at: datetime
@dataclass(frozen=True)
class Command:      command_id: str; name: str; payload: Mapping[str, object]
@dataclass(frozen=True)
class CommandAck:   command_id: str; accepted: bool; detail: str | None = None

class SessionDirectory(ABC):              # FC-7 — spec §5 verbs exactly
    @abstractmethod async def reserve_capacity(self, *, call_id: str, ttl_seconds: int) -> Reservation: ...
        # raises CapacityExceededError (admission per MAX_CONCURRENT_CALLS_PER_REPLICA)
    @abstractmethod async def bind_owner(self, *, call_id: str, owner_id: str) -> Lease: ...
        # atomic, at authenticated stream acceptance (spec §8.2)
    @abstractmethod async def renew_lease(self, *, call_id: str, owner_id: str) -> Lease: ...
    @abstractmethod async def route_command(self, *, call_id: str, command: Command) -> CommandAck: ...
        # idempotent on command_id
    @abstractmethod async def release(self, *, call_id: str, owner_id: str) -> None: ...
```

Frozen exception homes: `ConfigError` → `config_contract.py`; `MissingSelectionError`, `UnsupportedProviderError` → `providers/registry.py`; `StaleVersionError`, `CallFinalizedError`, `CapacityExceededError` → `storage/base.py`.

### 3.4 Provider-registry selection rules (frozen semantics, implemented in U-004)

- Kinds: `TELEPHONY | STT | TTS | LLM | REALTIME`.
- Selection variables (spec §11): `TELEPHONY_PROVIDER`, `STT_PROVIDER`, `TTS_PROVIDER`, `LLM_PROVIDER`; engine selection via `VOICE_ENGINE`.
- `VOICE_ENGINE=cascade` → composition root composes FC-2 + FC-3 + FC-4. Any other `VOICE_ENGINE` value resolves kind `REALTIME` (FC-5). (Decision (a) FINAL 2026-11-06: the baseline engine set is `cascade` only; a REALTIME name resolves FC-5 and fails loudly while unregistered — `nova-sonic` must never resolve, and the AWS rollback path keeps it untouched.)
- **No default values, no fallback of any kind** (R-C-002). Missing/empty selection → `MissingSelectionError` naming the config variable. Unknown/unregistered name → `UnsupportedProviderError` listing registered names. In particular, AWS provider names (`aws_transcribe`, `bedrock`, `polly`, `nova-sonic`) must never resolve.
- Factories are `Callable[[AppConfig], <contract instance>]`, registered explicitly per implementation module.

---

## 4. Phase A task cards (immediate phase — implement in order of batches)

Scope fence for ALL Phase A cards: **new files only** — `bot.py`, every existing root module, `voice-server/`, `deploy/`, and all 46 existing `tests/` files stay byte-identical (the AWS path remains untouched and isolated). This is both a requirement ("AWS path untouched + isolated") and the acceptance mechanism of U-005.

### 【U-001】Runtime environment, pipecat-ai 1.4.0 pin, lockfile, G-1 API check

- **Batch / owner**: Batch 0 / single owner (shared foundation — no duplicate implementation).
- **Description**: Establish a reproducible Python environment matching the deployment pin, capture a lockfile, defuse the vendored-pipecat import trap, and record what the installed 1.4.0 wheel actually ships (first step of G-1). **Before this lands, no code may claim pipecat SDK compatibility.**
- **Inputs (evidence)**: deploy pins `pipecat-ai[aws,aws-nova-sonic,silero,websocket]==1.4.0` (cloudformation.yaml:427); code guard `PINNED_PIPECAT = "1.4.0"` (bot.py:6090, version-guard bot.py:6143–6149); deploy uses Python 3.12 (cloudformation.yaml:400; README:73–74) while the local workspace runs 3.13.12 (discovery §1); no root lockfile exists (A-4 gap); `import pipecat` from repo root currently resolves to an empty namespace over vendored `pipecat/` (1.1.x-era, reference-only — discovery §1). Vendored source ships `AzureSTTService`, `AzureTTSService`/`AzureHttpTTSService`, `AzureLLMService`, `AzureRealtimeLLMService`, `TwilioFrameSerializer` — but **1.4.0 wheel contents are unverified** (discovery §1, G-1).
- **Outputs / files (create)**:
  - `requirements.txt` — declared pins: `pipecat-ai==1.4.0` (extras only as far as they exist on 1.4.0 — record what you used) + the minimal set needed to import `bot.py` and run tests (fastapi, uvicorn, aiohttp, python-dotenv, boto3 per cloudformation.yaml:426–429 — **no `aioboto3`**, dropped per CR-1: unsatisfiable with pipecat-ai 1.4.0 and its only reference is dead code bot.py:783 — plus test tooling).
  - `requirements.lock` — full freeze (`pip freeze`) from a clean install of `requirements.txt` in a fresh venv; header comment records interpreter version used.
  - `tests/test_pinned_pipecat.py` — asserts `pipecat.__version__ == "1.4.0"` (reuse the version-guard pattern, bot.py:6143–6149 precedent) **and** `pipecat.__file__` resolves inside site-packages, not the repo-root `pipecat/` directory (namespace-package trap).
  - `docs/azure/pipecat-1.4.0-api-notes.md` — for each of `AzureSTTService`, `AzureTTSService`, `AzureHttpTTSService`, `AzureLLMService`, `AzureRealtimeLLMService`, `TwilioFrameSerializer`: **present/absent in installed 1.4.0 + constructor signature as introspected** (paste introspection output; mark anything not introspectable as unverified). Phase B cards cite this file instead of the vendored source.
- **Contracts**: none (enables FC adoption later).
- **Config touched**: none.
- **Dependencies**: none. Blocks U-002..U-005 and all of Phase B.
- **Acceptance criteria**:
  1. Fresh venv (prefer Python 3.12 per cloudformation.yaml:400; if 3.13 is used instead, the lockfile header records that discrepancy — do not silently ignore it).
  2. `pip install -r requirements.txt` succeeds; `pytest --collect-only tests/` collects all 46 test files (collection, not passing — passing is U-005's gate).
  3. `tests/test_pinned_pipecat.py` green (version + import-origin assertions).
  4. `docs/azure/pipecat-1.4.0-api-notes.md` exists and honestly states presence/absence for the six adapter classes (G-1 step 1 closed; drift risk reduced from "unknown" to "documented").
  5. No tracked file modified (new files only).
- **Self-test**: `python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m pytest tests/test_pinned_pipecat.py -q && .venv/bin/python -m pytest --collect-only tests/ -q` — all green/collecting.

### 【U-002】Interface contracts FC-1..FC-8 (contract freeze)

- **Batch / owner**: Batch 1 / lane A (parallel with U-003).
- **Description**: Create the provider-neutral contract modules exactly as frozen in §3. This is the freeze artifact for the seven spec §5 names plus the pluggable `SignatureValidator`.
- **Inputs**: §3 of this plan (single definition site); spec §5; discovery §10 mapping table.
- **Outputs / files (create)**: `providers/__init__.py`, `providers/base.py` (FC-0 types, FC-1..FC-5, FC-8), `storage/__init__.py`, `storage/base.py` (FC-6, FC-7 + `CallState`, `StaleVersionError`, `CallFinalizedError`, `CapacityExceededError`), `tests/test_provider_contracts.py`, `tests/test_storage_contracts.py`.
- **Contracts**: defines FC-1..FC-8. Method sets and names are exactly §3 — no extra verbs, no provider types.
- **Config touched**: none.
- **Dependencies**: U-001 (test environment). None else — runs parallel to U-003.
- **Acceptance criteria**:
  1. Names frozen: `TelephonyAdapter`, `SpeechRecognizer`, `LanguageModel`, `SpeechSynthesizer`, `RealtimeVoiceEngine`, `CallRepository`, `SessionDirectory`, `SignatureValidator` — spelled exactly as spec §5 (R-T-015 contributes the new tests; contracts serve R-A-005/006/007/014 in later phases).
  2. Contract tests prove: each ABC rejects direct instantiation (`TypeError`); a minimal stub subclass per interface instantiates and exposes every abstract member; capability flags (`supports_transfer`, `supports_language_detection`, `supports_tool_calls`) exist on the declared classes.
  3. Import purity: `providers/base.py` and `storage/base.py` import nothing from `pipecat`, `boto3`, `botocore`, `aioboto3`, or any Azure SDK (assertable by grep — see self-test).
  4. New files only; existing tests untouched.
- **Self-test**: `python -m pytest tests/test_provider_contracts.py tests/test_storage_contracts.py -q` and `grep -nE "^(import|from) (pipecat|boto3|botocore|aioboto3|azure)" providers/base.py storage/base.py` → no matches.
- **Deviation CR-2**: `CallRecord.provider_ids` widened to `Mapping[str, str] | None = None`（§3.3 骨架冻结的唯一豁免）— see docs/azure/dependency-matrix.md Contract rulings.

### 【U-003】Azure config schema + startup validation + `.env.example`

- **Batch / owner**: Batch 1 / lane B (parallel with U-002).
- **Description**: Implement the spec §11 configuration contract as a pure, testable loader with fail-fast, all-errors-at-once validation and **no silent fallback** (R-C-001, R-C-002). This is the single place that defines which §11 placeholders are required vs conditional vs defaulted.
- **Inputs**: spec §11 placeholder block (names are referenced, not re-invented); R-C-001/002/003; D-4 (no default model); discovery §10 config-mapping table (old env → §11 names — referenced for R-C-003 traceability, not implemented here).
- **Outputs / files (create)**:
  - `config_contract.py` — `@dataclass(frozen=True) AppConfig` (one field per §11 placeholder), `class ConfigError(Exception)` carrying **all** violations with placeholder meanings, and one pure entry point `load_config(env: Mapping[str, str]) -> AppConfig` (used as startup validation by the composition root from Phase B on).
  - `.env.example` — the spec §11 block verbatim as placeholders + short comments; **no real values** (R-C-002).
  - `tests/test_config_contract.py`.
- **Contracts**: none (consumed by U-004 via `AppConfig`).
- **Config touched (spec §11 names)**: all of them — validation matrix (frozen here):
  - **Required, non-empty**: `APP_ENV`, `AZURE_LOCATION`, `PUBLIC_CONTROL_BASE_URL`, `PUBLIC_MEDIA_BASE_URL`, `TELEPHONY_PROVIDER`, `VOICE_ENGINE`, `STT_PROVIDER`, `TTS_PROVIDER`, `LLM_PROVIDER`, `LLM_ENDPOINT`, `LLM_DEPLOYMENT`, `SPEECH_REGION`, `STT_LOCALE`, `TTS_VOICE`, `COSMOS_ENDPOINT`, `COSMOS_DATABASE`, `BLOB_ACCOUNT_URL`, `SERVICEBUS_NAMESPACE`, `KEY_VAULT_URL`, `MAX_CONCURRENT_CALLS_PER_REPLICA`, `MAX_CALL_DURATION_SECONDS`, `TRANSCRIPT_RETENTION_DAYS`, `APPLICATIONINSIGHTS_CONNECTION_STRING`. Integer vars must parse as int > 0.
  - **Conditional**: `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN_SECRET_NAME` required when `TELEPHONY_PROVIDER=twilio`; `LLM_API_VERSION` and `SPEECH_ENDPOINT` are optional in Phase A ("if-required" per §11) — the Phase B LLM adapter declares whether it needs `LLM_API_VERSION` (registry hook; see U-102).
  - **Defaulted (only these two)**: `RECORDING_ENABLED=false`, `VOICE_LIVE_ENABLED=false` — strict `true`/`false` parse; any other value is an error. No other default exists anywhere (D-4 posture).
  - **Optional**: `REDIS_ENDPOINT` (coordination only, D-9).
- **Dependencies**: U-001. Parallel to U-002.
- **Acceptance criteria**:
  1. Missing/invalid variables → `ConfigError` listing **every** problem with its placeholder meaning (R-C-001).
  2. Unset `LLM_PROVIDER`/`LLM_ENDPOINT`/`LLM_DEPLOYMENT`/`STT_PROVIDER`/`TTS_PROVIDER` → startup failure, never a fallback (R-C-002; explicit test).
  3. `RECORDING_ENABLED`/`VOICE_LIVE_ENABLED` default `false` and gate nothing by themselves in Phase A — they are only parsed and exposed on `AppConfig` (R-C-002).
  4. A legacy-only environment (AWS-era names: `AWS_REGION`, `USERS_TABLE`, `TWILIO_AUTH_TOKEN`, …) fails validation listing §11 names — the loader never reads legacy variables (R-C-003 direction).
  5. `.env.example` committed with placeholders only; a scan finds no secret-looking values (R-C-002).
- **Self-test**: `python -m pytest tests/test_config_contract.py -q` (include a parametrized case per validation-matrix row).

### 【U-004】Provider registry with explicit selection

- **Batch / owner**: Batch 2 / lane A (or whoever frees up).
- **Description**: Implement the frozen registry semantics (§3.4): explicit, config-driven provider selection with loud failure and **zero silent AWS fallback**. Phase A ships the registry with stub factories in tests only; real adapters register themselves in Phase B.
- **Inputs**: §3.2–§3.4; R-C-002 ("never silently fall back to AWS, a different model, or an unapproved external provider"); discovery §10 `providers/` layout.
- **Outputs / files (create)**: `providers/registry.py` (`ProviderKind`, `ProviderRegistry.register/create`, `MissingSelectionError`, `UnsupportedProviderError`), `tests/test_provider_registry.py`.
- **Contracts**: consumes FC-1..FC-5 (factory return typing) and `AppConfig` (U-003).
- **Config touched**: `TELEPHONY_PROVIDER`, `STT_PROVIDER`, `TTS_PROVIDER`, `LLM_PROVIDER`, `VOICE_ENGINE`.
- **Dependencies**: U-002 (contract types) + U-003 (config). Nothing else.
- **Acceptance criteria**:
  1. `create(kind, config)` derives the selection strictly from the mapped §11 variable; empty/missing → `MissingSelectionError` naming that variable (R-C-001/002).
  2. Unknown name → `UnsupportedProviderError` listing registered names; explicitly tested for `bedrock`, `aws_transcribe`, `polly`, `nova-sonic` (no silent AWS fallback).
  3. `VOICE_ENGINE=cascade` composes FC-2/3/4 via their factories; any other engine value resolves kind `REALTIME` and fails when unregistered (decision (a) default, still overridable once Voice Live exists).
  4. Registration is explicit (`register(kind, name, factory)`); duplicate registration of the same `(kind, name)` raises; stub-factory tests prove the returned object is checked against the declared contract ABC.
- **Self-test**: `python -m pytest tests/test_provider_registry.py -q`.

### 【U-005】Phase A gate — retained-suite baseline, isolation proof, close-out report

- **Batch / owner**: Batch 3 / both developers (integration gate).
- **Description**: Prove Phase A changed nothing that existed before and everything new works; produce the phase close-out artifact required by R-O-005.
- **Inputs**: discovery §8 test inventory (30 RETAIN / 12 ADAPT / 4 REPLACE-RETIRE of 46 pytest files; 12 SPA specs RETAIN — **CR-3 reclassification**: `test_tool_filler` moved RETAIN→ADAPT, inventory is 29/13/4, fixture fix due by Phase B exit); R-T-015; R-O-005.
- **Outputs / files (create)**: `docs/azure/phase-a-report.md`.
- **Contracts / config**: none new.
- **Dependencies**: U-001..U-004.
- **Acceptance criteria**:
  1. **Baseline honesty**: run the full suite once on the pre-change tree and once after; the 29 RETAIN files (discovery §8 groups A–F, I and the named pure-logic files — CR-3 reclassified `test_tool_filler` → ADAPT) pass with no new failures. If any were already red before Phase A, the report records them as pre-existing with output evidence — the current suite state is **unverified** until this run.
  2. **Isolation**: `git diff --stat` shows **no modified tracked files** — Phase A added only: `requirements.txt`, `requirements.lock`, `providers/`, `storage/`, `config_contract.py`, `.env.example`, `tests/test_pinned_pipecat.py`, `tests/test_provider_contracts.py`, `tests/test_storage_contracts.py`, `tests/test_config_contract.py`, `tests/test_provider_registry.py`, `docs/azure/pipecat-1.4.0-api-notes.md`, this plan and the report. (AWS path untouched + isolated, verifiable.)
  3. **Test inventory update (R-T-015)**: the report appends the 5 new test files to the discovery §8 inventory and re-affirms 29 RETAIN green / 13 ADAPT → user Phase D / 4 REPLACE-RETIRE at cutover + `test_phone_numbers` re-target.
  4. **Close-out report (R-O-005)**: changed files, commands actually executed, results, unresolved blockers (at report time: decisions (a)–(e) + A-1, A-2/A-3 open), and manual prerequisites — one section each.
  5. Decision gate note: the report explicitly states that decisions (a)–(e) and A-1 are running on the LOCAL-development defaults of §6 and **require user confirmation before Phase A is declared closed**. **[GATE UPDATE 2026-11-06: the user confirmed (a)–(e), A-1, A-2 and A-3 — all FINAL per §7 ADR register; the blocking-decision register shows ZERO open items (A-3 = "preserve all, test the 4"). The confirmation is recorded in phase-a-report.md §4.3/§5; Phase A closure still awaits the review/QA conclusions (tasks #6/#7).]**
- **Self-test**: `python -m pytest tests/ -q` (full run in the pinned venv) + `git diff --stat` (empty for tracked files) + report exists and contains the five R-O-005 sections.

---

## 5. Skeleton cards — user Phases B–F (sequencing only)

Format: ID · goal · key deliverables · dependencies · R-IDs/gaps closed.

### Phase B — Azure adapters + voice-call vertical slice

- **U-101 · Azure Speech STT/TTS adapters.** Deliverables: `providers/speech/azure_speech.py` implementing FC-2/FC-4 by wrapping the 1.4.0 Azure services per `docs/azure/pipecat-1.4.0-api-notes.md` (HTTP fallback if signatures differ — G-1); registry entries for `STT_PROVIDER=azure_speech`/`TTS_PROVIDER=azure_speech`; optional MiniMax adapter behind FC-4 **only if G-1/G-2 approve** (else `SimpleMiniMaxTTSService` path untouched). Deps: U-001/002/004. Closes: dependency-matrix rows *Transcribe STT*, *Polly TTS*, *MiniMax TTS*; G-1 consumption; R-P-010 (choice capability).
- **U-102 · LLM adapter over approved endpoint.** Deliverables: `providers/llm/azure_openai.py` implementing FC-3 from `LLM_PROVIDER/LLM_ENDPOINT/LLM_DEPLOYMENT/LLM_API_VERSION`; declares whether `LLM_API_VERSION` is required (registry hook per U-003 matrix); fixture-driven tests only until G-2 picks the deployment. Deps: U-002/003/004. Closes: dependency-matrix rows *Bedrock LLM* / *Bedrock model IDs*; R-A-006 (schema/validation/idempotency parts); D-4 posture.
- **U-103 · Voice-call vertical slice (local).** Deliverables: `bot.py` composition-root wiring behind explicit registry selection (this is where `load_config` becomes real startup validation); `VOICE_ENGINE=cascade` path STT→LLM→TTS streaming (phrase-level, spec §7.1), local streaming/interruption/lifecycle-cleanup/idempotent-tool demo per spec Phase B completion; existing store/session modules untouched. Deps: U-101, U-102. Closes: spec Phase B completion conditions; R-P-001/004/009 preservation; R-A-005 first half.
- **U-104 · MCP sigv4 degradation (decision (c) FINAL 2026-11-06).** Deliverables: keep `auth.type=sigv4` valid in config/UI, degrade at connect with `{ok:false, error}` via the existing `MissingCredentialsError` path (mcp_sigv4.py:41–46); `test_mcp_sigv4.py` → degradation test (old file retires at cutover, F). Deps: U-005. Closes: discovery gap G-5, product-impact C-5, dependency-matrix *IAM/SigV4* row.
- **U-105 · pipecat↔mcp version pairing — MUST-FIX (CR-3 attached gap flag).** The current resolver pair (pipecat-ai 1.4.0 + mcp 2.3.0) raises `ImportError: cannot import name 'streamablehttp_client' from 'mcp.client.streamable_http'` at `pipecat/services/mcp_service.py:28`, which breaks the **live** MCP tool path (`from pipecat.services.mcp_service import MCPClient`, bot.py:1909 — R-P-004), not just the retired `test_mcp_sigv4`. Deliverables: install and pin the `mcp` SDK version that pipecat-ai 1.4.0 actually declares/supports (record the pairing in `requirements.txt` + regenerate `requirements.lock`); add an import-smoke (`python -c "import pipecat.services.mcp_service"` or a dedicated smoke test) to the **RETAIN CI gate** alongside `tests/test_pinned_pipecat.py`; the same gate adopts the RETAIN set, so the `test_tool_filler` fixture-only fix (CR-3 item 2) lands here at the latest. Deps: U-001. Closes: CR-3 attached gap flag (dependency-matrix "Contract rulings"); protects the live MCP path.

### Phase C — Twilio webhook / audio / streaming / barge-in / handoff

- **U-201 · Webhook + WS-upgrade signature layer (pluggable).** Deliverables: FC-1 Twilio adapter `providers/telephony/twilio_adapter.py`; `SignatureValidator` implementation per A-1 ruling (**FINAL 2026-11-06**: official Twilio SDK `RequestValidator` on the Azure path; keep `twilio_sig.py` stdlib HMAC-SHA1 as the independent test oracle; the `twilio` SDK dependency enters here, Phase C); external-URL signing via `PUBLIC_CONTROL_BASE_URL`/`PUBLIC_MEDIA_BASE_URL`; preserve pinned 503/403/200/TwiML contracts exactly (product-impact §2.3). Deps: U-002, U-103. Closes: spec §6.1.1–6.1.4, R-S-005/006 (Twilio half), gap G-8; consumes decision A-1.
- **U-202 · Stream reservation + correlation + frame handling.** Deliverables: §6.1.5–6.1.7 — reservation record created at webhook, AccountSid/CallSid/StreamSid consistency check at stream acceptance, reject unknown/expired/mismatched sessions; no sensitive data in URLs/params; start/media/mark/stop/malformed/early-disconnect handling; minimal single-replica `SessionDirectory` implementation of FC-7 (`reserve_capacity`/`bind_owner`/`release`) — superseded in Phase D behind the same contract. Deps: U-201. Closes: R-A-002, R-S-006, gap G-9 (part).
- **U-203 · Audio contract §6.2.** Deliverables: base64 μ-law/8 kHz ↔ normalized PCM both directions (FC-1 decode/encode), streaming resampler with state, bounded queues, output pacing, per-call memory limits, no file headers; waveform fixtures (framing, sample rates, clipping, silence, malformed frames). Deps: U-201 (parallel with U-202). Closes: R-A-003, R-T-002.
- **U-204 · Barge-in §6.3 incl. mark/clear ledger.** Deliverables: generation/epoch increment on confirmed speech; cancel model+TTS; flush local queues; Twilio `clear`; discard late callbacks from prior epochs; **mark ledger distinguishing cleared-vs-played audio**; explicit interrupted-assistant-content history policy (documented + tested); generated vs confirmed-played duration accounting. Deps: U-203. Closes: R-A-004, R-T-003, gap G-9 (part).
- **U-205 · Status callbacks + fallback TwiML (§6.1.8–6.1.9).** Deliverables: signed status-callback endpoint; persist-before-ack + retry dedup (needs U-301 — see §2.2 cross-phase note); approved fallback TwiML/transfer route when the stream cannot start. Deps: U-202, U-301. Closes: R-A-002 (fallback), R-T-007, gap G-9 (rest).
- **U-206 · Human handoff §6.4 (decision D-5 gated; A-2 FINAL 2026-11-06).** Deliverables: under the D-5 default, preserve record+hangup semantics (`transfer_requested` + `transfer_topic`, write-before-teardown — R-P-005) while implementing the §6.4 target shape behind `supports_transfer`; target resolution from approved allowlist + tenant authorization + destination policy; no caller drop on busy/no-answer. **A-2 ruling consumed here**: the external "Connect Flow Lambda" is REPLACED by these in-repo Twilio transfer flows (spec §6.4) and retired at cutover; R-P-005 acceptance = Phase C tests incl. **no-answer / busy / failure** cases. Deps: U-202. Closes: R-P-005, R-T-006, S-007 limit (R-S-007); gap G-4/A-2 closed by in-repo ownership.
- **U-207 · Phone-numbers view re-source (decision (d) FINAL 2026-11-06).** Deliverables: `/api/admin/phone-numbers` content Chime→Twilio behind the **unchanged** `{voice_connectors, error}` envelope (never remove/rename; `voice_connectors` is a pinned legacy field name); `test_phone_numbers` re-targeted to same-envelope assertions. Deps: U-201. Closes: product-impact C-4, gap G-6, dependency-matrix row *Chime* (admin surface part).

### Phase D — Persistence / post-call / session ownership / capacity

- **U-301 · Cosmos `CallRepository` (FC-6).** Deliverables: `storage/cosmos_calls.py` over §9.1 entities `calls`/`call_events`/`transcripts`/`usage`; optimistic concurrency (`StaleVersionError`), dedup append (`CallEvent.event_id`), finalize semantics (late/duplicate events never revive a finalized call — `CallFinalizedError`), history query with cursor; `expires_at` business boundary + remaining-TTL-on-write per §9.2. Deps: U-002. Closes: R-A-007/009, R-P-008 (history reads), dependency-matrix *call history* row.
- **U-302 · Store adapters behind existing interfaces.** Deliverables: `storage/` Cosmos adapters for `user_store`/`demo_store`/`voice_store`/`activity_store` with **method signatures unchanged** (discovery §10 principle); default-deny redaction semantics preserved; 8 scenario presets round-trip byte-faithfully. Deps: U-002 (types only). Closes: dependency-matrix DynamoDB rows, R-P-003/007/008, R-C-003 (env-name replacement).
- **U-303 · Test port: 12 ADAPT files.** Deliverables: moto/boto3-backed fixtures → storage-adapter contract tests for the 12 ADAPT files (discovery §8 groups G/H + `test_mcp_integration`), assertions preserved. Deps: U-301, U-302. Closes: R-T-015 ADAPT bucket.
- **U-304 · Service Bus post-call worker.** Deliverables: `workers/` idempotent post-call jobs (summaries, approved artifacts), retries, dead-letter; summary flow moves off in-process `asyncio.create_task` (bot.py:1393). Deps: U-301. Closes: R-A-010, dependency-matrix *Service Bus* row.
- **U-305 · Distributed ownership + capacity (FC-7 upgrade).** Deliverables: call-to-runtime-owner lease in shared storage, atomic owner bind, lease renewal, owner-addressable command routing (command IDs, ack, bounded retries, idempotent), admission per `MAX_CONCURRENT_CALLS_PER_REPLICA`, drain/shutdown lease cleanup, documented owner-loss caller experience (no transparent recovery claims). Supersedes U-202's minimal implementation behind the same frozen contract. Deps: U-202, U-301. Closes: R-A-014/015/022, gap G-3, spec §8.2/8.3.
- **U-306 · Call state machine (§8.1).** Deliverables: explicit `CallState` transitions with idempotent transitions, event dedup, late-event rules; unit tests for every transition incl. duplicate/late/out-of-order and finalize-then-late-event. Deps: U-301. Closes: R-A-013.

### Phase E — Infra / CI-CD / telemetry / migration / runbooks

- **U-401 · Bicep modules + environments.** Deliverables: §12 module list (Container Apps env + control/runtime/worker, ACR, identities + least-privilege role assignments, Key Vault, Cosmos, Blob with public access disabled, Service Bus retry/DLQ, optional Redis, Monitor/Log Analytics/App Insights + alerts/budgets/action groups, networking/DNS/TLS), parameterized dev/staging/prod, `az deployment what-if` per env. Deps: D-phase runtime. Closes: R-A-017, R-S-001/002/009, R-O-003.
- **U-402 · CI/CD pipeline.** Deliverables: GitHub Actions OIDC sequence per §12 (lint/tests → scans → build-once-by-digest → Bicep what-if → staging deploy + smoke → approval gate → green deploy + readiness + routing switch → drain + watch). Deps: U-401. Closes: R-A-018, R-S-004.
- **U-403 · Telemetry + redaction.** Deliverables: OTel + App Insights with correlation (call ID, CallSid, stream ID, trace ID, owner, agent version, model deployment); §13 metric list each with a documented measurement source (no invented SLOs); redaction default-on (no audio/credentials/sensitive transcript; masked caller IDs). Deps: U-301 (IDs exist). Closes: R-A-012, R-T-012 (redaction part).
- **U-404 · Migration tooling (D-8 gated).** Deliverables: §9.3 export/transform/test-import/reconcile/delta/full-run/rollback scripts for the DynamoDB sources documented in discovery §7. Deps: U-301/302. Closes: R-A-019, R-T-011.
- **U-405 · Blue/green media-endpoint releases.** Deliverables: new-calls-to-green / old-calls-drain scheme, retained old capacity until drained or approved deadline, forced-drain behaviour documented. Deps: U-305, U-401. Closes: R-A-016, R-T-010.
- **U-406 · Operator runbooks.** Deliverables: release drain, forced drain, rollback, broker outage, storage failure, owner loss, overload, Twilio outage/fallback (R-O-001); rehearsal records for drain + rollback. Deps: U-405. Closes: R-O-001, R-T-009 (runbook side).
- **U-407 · Entra ID auth for admin/monitor (C-8, decision (b) FINAL 2026-11-06).** Deliverables: the one approved breaking surface change (product-impact §4); preserve session/role/guest/ws-token semantics exactly; `/setup` follows decision (b): break-glass through Phase C, demote to role-mapping seed at Entra cutover. **Placement note**: user phases A–F scope strings do not name auth; placed in E as the security-infrastructure phase and it blocks F. Deps: none open — (b) user-confirmed 2026-11-06. Closes: R-S-005, gap G-7, product-impact C-8.

### Phase F — Automated tests + deployment-readiness

- **U-501 · Acceptance matrix execution + `acceptance.md`.** Deliverables: evidence per spec §15 rows (real-call suites, four languages per A-3 scope — **A-3 FINAL 2026-11-06: the 4 business languages zh-HK/zh-CN/en-US/ja-JP**, while all `LANGUAGES` entries incl. en-SG keep working on both paths; Nova-only locales retire from phone defaults per (a) — interruption, transfer, failure injection, load test with D-6 values); record approved numeric targets in `docs/azure/acceptance.md` (R-T-016 — **no invented SLOs**); execute cutover test disposition: retire `test_mcp_sigv4`, `test_transcribe_signing`, `test_nova_voices`, `test_chime_provisioner`, retire `asr-test/ws` route + `AsrTestView.vue` per decision (e) (keep the version-guard pattern as design input). Deps: all of D/E. Closes: R-T-001..016, G-4, decisions (e) and A-3.
- **U-502 · Cost/usage reconciliation.** Deliverables: `usage` rows per completed call with units and meter IDs; reconciliation of provider counters vs billing meters/invoices using the §13 cost structure; identical call scripts for AWS-vs-Azure comparison. Deps: U-403, U-501. Closes: R-A-020, R-T-013.
- **U-503 · Deployment-readiness + cutover plan.** Deliverables: canary/cutover plan with observation windows and rollback route (execution stays authorization-gated — R-O-004/G-7), rollback rehearsal + Azure-created-data reconciliation procedure (R-T-014), privacy release gates recorded (R-S-010), final phase close-out (R-O-005). Deps: U-501, U-502. Closes: R-O-002/004/005, R-T-014, R-S-010.

---

## 6. Parallel assignment suggestion (Phase A)

| Developer | Cards | Notes |
|---|---|---|
| Dev-A (foundation lane) | U-001 → U-002 → U-004 | Owns the shared environment and the contract freeze; U-001 is single-owner (no duplicates). |
| Dev-B (config lane) | U-003 (after U-001) → assists U-004 review | U-002 ∥ U-003 is the parallel window — zero shared files. |
| Dev-A + Dev-B | U-005 | Gate run + close-out report jointly. |

If only one developer is available, run U-001 → U-002 → U-003 → U-004 → U-005 strictly; the batches remain valid (the parallel window simply collapses).

---

## 7. Decision / ADR register — FINALIZED (user-confirmed 2026-11-06)

These entries ran as LOCAL-development defaults while Phase A executed (U-005 §5 recorded the gate). **User decisions confirmed 2026-11-06**: (a)–(e), A-1, A-2 and A-3 below are now **FINAL and binding** on Phases B–F. **The blocking-decision register shows ZERO open items** (D-1…D-9 run on the approved temporary assumptions of requirements §5). Any change to a FINAL entry routes back through solution-architect + the user (spec §16) — no silent re-opening.

| # | Decision | Finalized ruling (user-confirmed 2026-11-06) | Binding consequences (frozen) | Consumed by | Status |
|---|---|---|---|---|---|
| (a) | Nova Sonic disposition | **DROP `nova-sonic` from the baseline engine set.** Capability slot stays FC-5 `RealtimeVoiceEngine` → future Azure Voice Live behind `VOICE_LIVE_ENABLED=false` | `engines[]`→`[pipeline]` only; `PHONE_ENGINE` default `nova-sonic`→`pipeline`; `nova_sonic_voices` empties (C-3); the 8 Nova-only locales (bot.py:362–453) retire from phone defaults; the AWS rollback path keeps `nova-sonic` untouched | U-103 engine set; U-501 (`test_nova_voices` retire); product-impact C-3 | **FINAL** |
| (b) | `/setup` wizard vs Entra | Keep `/setup` through Phase C as break-glass admin bootstrap; at Entra cutover demote it to seeding the local role mapping | Current `/setup`/`test_setup` semantics remain the contract to beat until Entra cutover; `setup-status` fail-safe UX redefined at that cutover, never left dangling | U-407; product-impact §4 Q1 | **FINAL** |
| (c) | MCP sigv4 degradation | Keep `auth.type=sigv4` valid in config/UI; degrade at connect with `{ok:false, error}` via the existing `MissingCredentialsError` path; drop botocore from the Azure image at cutover | Degrade-at-connect behaviour frozen; `mcp_sigv4.py` retires from runtime at cutover | U-104; U-501 (`test_mcp_sigv4` → degradation test) | **FINAL** |
| (d) | Phone-numbers source | Re-target `_list_chime_vc_numbers` → Twilio IncomingPhoneNumbers behind the **unchanged** `{voice_connectors, error}` envelope | Envelope + field names (`voice_connectors`) frozen; content swap only | U-207; product-impact C-4 | **FINAL** |
| (e) | `asr-test/ws` retire | **Retire** at cutover | Route + `AsrTestView.vue` + `test_transcribe_signing` removed at cutover; not baseline-blocking | U-501; product-impact S-16 | **FINAL** |
| A-1 | Twilio signature: official SDK vs stdlib (spec §6.1.2 vs `twilio_sig.py` deliberate stdlib, G-8) | **Azure path uses the official twilio SDK `RequestValidator`**; stdlib HMAC-SHA1 `twilio_sig.py` remains the independent **test oracle** | The `twilio` SDK dependency enters at Phase C / U-201 (NOT in Phase A requirements); FC-8 `SignatureValidator` remains the frozen seam | U-201 | **FINAL** |
| A-2 | External "Connect Flow Lambda" post-BYE handoff consumer (G-4) | **REPLACED by in-repo Twilio transfer flows (spec §6.4)**; the external Lambda is retired at cutover | R-P-005 acceptance = Phase C tests incl. **no-answer / busy / failure**; the "unowned external system" gap closes via in-repo ownership; record+hangup (D-5) preserved until U-206 lands | U-206; product-impact C-7 | **FINAL** |
| A-3 | Language scope (requirements §7) | **"Preserve all, test the 4"** — every existing `LANGUAGES` entry keeps working on both paths (en-SG included — zero product change, consistent with the no-visible-reduction rule); acceptance tests cover exactly the 4 business languages zh-HK / zh-CN / en-US / ja-JP per spec §15 | R-T-004 acceptance scope frozen to the 4 business languages; the 8 Nova-only locales follow decision (a) — they retire from phone defaults with nova-sonic; no `LANGUAGES` entry is removed | U-501 (acceptance matrix); R-T-004 | **FINAL** |

---

## 8. Risk register

| Risk | Evidence | Impact | Mitigation (planned) |
|---|---|---|---|
| **G-1 pipecat drift** — pinned 1.4.0 in deploy only; vendored source is 1.1.x-era; nothing installed in dev env; 1.4.0 Azure adapter signatures unverified | cloudformation.yaml:427; bot.py:6090–6149; discovery §1, §11 G-1 | Phase B adapters could be built against wrong APIs; seam-patch (`asr_multilang` monkeypatch of `get_presigned_url`) may not transfer | U-001 installs the real pin + lockfile + `pipecat-1.4.0-api-notes.md` **before any code claims SDK compatibility**; FC contracts carry no pipecat types; version-guard test pins `pipecat.__version__` and import origin |
| **G-2 pre-existing defect** — `SimplePollyTTSService` calls `aioboto3.Session()` (bot.py:783) with no `import aioboto3` anywhere → NameError on the Polly path | bot.py:783; repo-wide grep (discovery §4, §11 G-2) | Polly path viability unverified; a naive "fix while passing" would violate scope | Recorded as a known defect only. Phase A must NOT fix/port it; regression tests must not depend on the Polly path; Polly is replaced by Azure TTS in U-101 |
| **G-3 multi-replica gap** — all session/monitor/buffer state in-process (`ACTIVE_SESSIONS`, `HISTORY_BUFFERS`, single uvicorn) | bot.py:1138, 1230, 7398–7403; discovery §11 G-3 | Cannot claim HA; callback landing on non-owner replica would be lost | Phases A–C single-replica is the validation config (D-9); no HA claims anywhere; FC-7 reserves the semantics now, U-305 implements them in Phase D |
| **G-4 external handoff consumer (RESOLVED by A-2, 2026-11-06)** — post-BYE "Connect Flow Lambda" lives outside this repo | call_control_tools.py:113–116; bot.py:1300–1308; discovery §11 G-4 | Was blocking full R-P-005 acceptance | A-2 FINAL: replaced by **in-repo Twilio transfer flows (spec §6.4)** owned in this repo; external Lambda retired at cutover; R-P-005 acceptance = U-206 Phase C tests incl. **no-answer/busy/failure**; D-5 record+hangup preserved until U-206 lands |
| **CR-3 pipecat↔mcp pairing** — pipecat 1.4.0 + resolved mcp 2.3.0 raise `ImportError: streamablehttp_client` at `pipecat/services/mcp_service.py:28`, breaking the **live** MCP tool path (`MCPClient`, bot.py:1909 — R-P-004), not just the retired `test_mcp_sigv4` | dependency-matrix CR-3 item 3 (attached gap flag); phase-a-report.md §4.3 C5; bot.py:1909 | Live MCP tool calls fail at import on the pinned pair | **U-105 (Phase B must-fix)**: pin the mcp SDK version pipecat-ai 1.4.0 declares/supports + regenerate `requirements.lock`; `import pipecat.services.mcp_service` import-smoke enters the RETAIN CI gate alongside `tests/test_pinned_pipecat.py` |
| **D-4 no-default-model policy** — exact LLM deployment/TTS voice unchosen (G-2) | requirements.md D-4; spec §7.2/§11 | Any compiled-in default would be an unapproved silent fallback | U-003 makes `LLM_PROVIDER/LLM_ENDPOINT/LLM_DEPLOYMENT` hard-required at startup; U-102 tests run on fixtures only until G-2 lands; registry refuses unknown names |
| Suite-green baseline unknown — discovery classified 46 test files but recorded no run result; dev env ran 3.13.12 vs deploy 3.12 | discovery §1, §8 (unverified run state) | "Keep 30 RETAIN green" has no recorded baseline | U-005 runs before/after baselines and records pre-existing failures with evidence; U-001 lockfile header records the interpreter used |

---

## 9. Phase A NON-goals (explicit)

1. **No Azure SDK adapter implementations** — Azure STT/TTS/LLM adapters are user Phase B (U-101/U-102). Phase A ships contracts and registry only.
2. **No infrastructure** — no Bicep, no CI/CD, no cloud provisioning, no Azure resources (Phase E).
3. **No commits from this planning deliverable** — this step produced documentation only; Phase A code lands as new files via the implementation task under normal review (and per U-005, without modifying any tracked file).
4. **No AWS-path changes** — `bot.py`, all existing modules, `voice-server/`, `deploy/`, and all 46 existing test files remain untouched in Phase A. The G-2 Polly defect is recorded, not fixed.
5. **No decisions silently hardened** — (a)–(e), A-1, A-2 and A-3 were confirmed by the USER on 2026-11-06 and are FINAL in §7 (frozen; any change routes through solution-architect + user per spec §16). The blocking-decision register now shows **zero open items**.
6. **No secrets, no real values** — `.env.example` placeholders only; nothing credential-shaped is committed (R-C-002, R-S-003).

---

## 10. Task-card quality rules honored

- **简洁为荣，冗余为耻**: Phase A is exactly 5 cards (1 foundation, 2 parallel core, 1 registry, 1 gate) — every card maps 1:1 to a mission-mandated scope item; no card exists that another card already covers.
- **Contract-frozen**: interface names and method sets are defined once (§3) and referenced as FC-1..FC-8 from every card; nothing re-defines an interface inside a card. Deviations route through solution-architect.
- **Self-contained cards**: each Phase A card carries inputs, outputs, exact file paths, config names, dependencies, verifiable acceptance criteria with R-ID refs, and an executable self-test command.
- **Explicit dependencies**: §2.2 records the two cross-phase edges (U-205←U-301, U-305←U-202) so parallel work never deadlocks on an unstated wait.
