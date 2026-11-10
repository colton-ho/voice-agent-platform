# pipecat-ai 1.4.0 — API notes from the installed wheel (U-001, G-1 step 1)

Purpose: Phase B+ cards must cite **this file** instead of the vendored `./pipecat` source
tree when writing Azure/Twilio/AWS/MiniMax adapters. Every claim below is evidence read off
the **installed pipecat-ai 1.4.0 wheel**, never from the vendored copy.

- **Evidence provenance**: wheel installed into the U-001 venv
  `/Users/colton/Developer/voice-agent-platform/.venv/lib/python3.13/site-packages/` (interpreter
  Python 3.13.12). All `pipecat/...` paths below are relative to that `site-packages/`.
- **Introspection method**: (a) AST parse of the wheel's own module source (works even when a
  service's optional SDK is not installed), (b) live `inspect.signature` where importable.
  Introspection transcript is pasted verbatim per class. Anything not introspectable is
  marked **unverified** — nothing here is recalled from memory.
- **Drift baseline** ("vendored 1.1.x-era" = repo `pipecat/` source copy, CHANGELOG top entry
  `[1.1.0] – 2026-04-27`, `pipecat/CHANGELOG.md:10`). Vendored files are cited as
  `vendored-pipecat/...` and used **only** for drift comparison, never as truth.
- Environment caveat: at introspection time the venv held the **base** `pipecat-ai==1.4.0`
  install; the `azure` / `aws` / `aws-nova-sonic` extras' dependency packages were not yet
  installed (see §5). This does not affect the introspected source (the wheel module files are
  identical regardless of extras), only the "LIVE import" verdicts, which are reported as-is.

Runtime version facts (also pinned by `tests/test_pinned_pipecat.py`):

```
importlib.metadata.version("pipecat-ai") == "1.4.0"
pipecat.__version__ == "1.4.0"          # attribute exists on the package
pipecat.__file__  ->  site-packages/pipecat/__init__.py   (NOT the vendored ./pipecat tree)
```

---

## 1. The six card classes — presence + constructor signatures

### 1.1 `AzureSTTService` — PRESENT

- Location: `site-packages/pipecat/services/azure/stt.py` (class at line 62)
- Introspection (AST of wheel source):

```
AzureSTTService: PRESENT
  file: .../site-packages/pipecat/services/azure/stt.py
  bases: STTService
  __init__(self, *, api_key: str, region: str | None = None, language: Language | None = Language.EN_US,
           sample_rate: int | None = None, private_endpoint: str | None = None,
           endpoint_id: str | None = None, settings: Settings | None = None,
           ttfs_p99_latency: float | None = AZURE_TTFS_P99, **kwargs)
  Settings = AzureSTTSettings
  AzureSTTSettings own fields:
    profanity: AzureProfanity | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
  LIVE import FAILED: ImportError: Missing module: No module named 'azure'
```

- Import gate: the module hard-requires the Azure Speech SDK at import time
  (`stt.py` raises `Exception: Missing module ...` when `azure.cognitiveservices.speech` is
  absent) — **`pipecat-ai[azure]` (→ `azure-cognitiveservices-speech<2,>=1.47.0`) must be
  installed before this class can be constructed** (Phase B / U-101 input).
- Drift vs vendored 1.1.x: constructor **identical** (`vendored-pipecat/src/pipecat/services/azure/stt.py:76-87`).
  Settings drift: `AzureSTTSettings` was `pass` in 1.1.x; 1.4.0 adds the `profanity` field.
  (additive — safe.)

### 1.2 `AzureTTSService` — PRESENT

- Location: `site-packages/pipecat/services/azure/tts.py` (class at line 242)
- Introspection:

```
AzureTTSService: PRESENT
  file: .../site-packages/pipecat/services/azure/tts.py
  bases: TTSService, AzureBaseTTSService
  __init__(self, *, api_key: str, region: str | None = None, private_endpoint: str | None = None,
           voice: str | None = None, sample_rate: int | None = None,
           params: AzureBaseTTSService.InputParams | None = None, settings: Settings | None = None,
           aggregate_sentences: bool | None = None, text_aggregation_mode: TextAggregationMode | None = None,
           **kwargs)
  Settings = AzureTTSSettings
  AzureTTSSettings own fields:
    emphasis, pitch, rate, role, style, style_degree, volume   # all str | None | _NotGiven
  LIVE import FAILED: ImportError: Missing module: No module named 'azure'
```

- **Drift vs vendored 1.1.x** (`vendored-pipecat/src/pipecat/services/azure/tts.py:252-264`):
  `region` was **required** (`region: str`); 1.4.0 makes it `region: str | None = None` and adds
  **`private_endpoint: str | None = None`** (Speech private-endpoint support). All other params
  unchanged. Backward-compatible for existing call shapes; new Azure deployments behind
  firewalls can now use `private_endpoint`.

### 1.3 `AzureHttpTTSService` — PRESENT

- Location: `site-packages/pipecat/services/azure/tts.py` (class at line 739)
- Introspection:

```
AzureHttpTTSService: PRESENT
  file: .../site-packages/pipecat/services/azure/tts.py
  bases: TTSService, AzureBaseTTSService
  __init__(self, *, api_key: str, region: str | None = None, private_endpoint: str | None = None,
           voice: str | None = None, sample_rate: int | None = None,
           params: AzureBaseTTSService.InputParams | None = None, settings: Settings | None = None,
           **kwargs)
  Settings = AzureTTSSettings   # shared with AzureTTSService (fields above)
  LIVE import FAILED: ImportError: Missing module: No module named 'azure'
```

- **Drift vs vendored 1.1.x** (`vendored-pipecat/src/pipecat/services/azure/tts.py:749-759`):
  same as §1.2 — `region` optional now + `private_endpoint` added. Otherwise identical.
- Note: this is the **HTTP fallback** class named in U-101 ("HTTP fallback if signatures differ");
  its signature did **not** differ adversely — either class is constructible as in 1.1.x.

### 1.4 `AzureLLMService` — PRESENT

- Location: `site-packages/pipecat/services/azure/llm.py` (class at line 25)
- Introspection (AST + live):

```
AzureLLMService: PRESENT
  file: .../site-packages/pipecat/services/azure/llm.py
  bases: OpenAILLMService
  __init__(self, *, api_key: str, endpoint: str, model: str | None = None,
           api_version: str = '2024-09-01-preview', settings: Settings | None = None, **kwargs)
  Settings = AzureLLMSettings   # own fields: (none — pass-through)
  LIVE import OK: pipecat.services.azure.llm.AzureLLMService
  inspect.signature(__init__): ((self, *, api_key: str, endpoint: str, model: str | None = None,
      api_version: str = '2024-09-01-preview', settings: AzureLLMSettings | None = None, **kwargs))
```

- Drift vs vendored 1.1.x: **identical** (`vendored-pipecat/src/pipecat/services/azure/llm.py:34-43`).
- Import gate: **no Azure SDK needed** — built on `openai.AsyncAzureOpenAI` (base dep `openai`),
  so this class is importable today. It is the natural FC-3 implementation target for U-102
  (`LLM_ENDPOINT`/`LLM_DEPLOYMENT` map to `endpoint=`/`model=`, `LLM_API_VERSION` to `api_version=`;
  note `api_version` has default `"2024-09-01-preview"` in 1.4.0 — U-003 leaves
  `LLM_API_VERSION` "if-required"; this default means the adapter can run without it, but the
  registry hook in U-102 should still decide explicitly).

### 1.5 `AzureRealtimeLLMService` — PRESENT

- Location: `site-packages/pipecat/services/azure/realtime/llm.py` (class at line 30)
- Introspection (AST + live):

```
AzureRealtimeLLMService: PRESENT
  file: .../site-packages/pipecat/services/azure/realtime/llm.py
  bases: OpenAIRealtimeLLMService
  __init__(self, *, api_key: str, base_url: str, **kwargs)
  Settings = AzureRealtimeLLMSettings   # own fields: (none — pass-through)
  LIVE import OK: pipecat.services.azure.realtime.llm.AzureRealtimeLLMService
  inspect.signature(__init__): ((self, *, api_key: str, base_url: str, **kwargs))
```

- Drift vs vendored 1.1.x: **identical** (`vendored-pipecat/src/pipecat/services/azure/realtime/llm.py:41-47`).
- `base_url` is the **full Azure WebSocket URL** incl. api-version + deployment
  (vendored docstring, same in 1.4.0 file) — this is the FC-5 "future Voice Live" candidate
  behind `VOICE_LIVE_ENABLED=false` (decision (a)); no realtime engine is registered in Phase A.

### 1.6 `TwilioFrameSerializer` — PRESENT

- Location: `site-packages/pipecat/serializers/twilio.py` (class at line 32)
- Introspection (AST + live):

```
TwilioFrameSerializer: PRESENT
  file: .../site-packages/pipecat/serializers/twilio.py
  bases: FrameSerializer
  __init__(self, stream_sid: str, call_sid: str | None = None, account_sid: str | None = None,
           auth_token: str | None = None, region: str | None = None, edge: str | None = None,
           params: InputParams | None = None)
  nested class InputParams(FrameSerializer.InputParams):
    twilio_sample_rate: int = 8000
    sample_rate: int | None = None
    auto_hang_up: bool = True
  LIVE import OK: pipecat.serializers.twilio.TwilioFrameSerializer
  inspect.signature(__init__): ((self, stream_sid: str, call_sid: str | None = None,
      account_sid: str | None = None, auth_token: str | None = None, region: str | None = None,
      edge: str | None = None, params: TwilioFrameSerializer.InputParams | None = None))
```

- Drift vs vendored 1.1.x: **identical** (ctor `vendored-pipecat/src/pipecat/serializers/twilio.py:58-67`,
  InputParams `:44-56`).
- **Audio-format behaviour** (cited from wheel source `serializers/twilio.py`):
  - Twilio wire format is **base64 μ-law at `twilio_sample_rate` (default 8000 Hz)**, mono
    (`:48`, `:54`, deserialized frame `num_channels=1` `:264`).
  - Output path: pipeline PCM → μ-law at Twilio rate via streaming resampler —
    `pcm_to_ulaw(data, frame.sample_rate, self._twilio_sample_rate, self._output_resampler)` (`:156-157`).
  - Input path: μ-law → PCM at pipeline input rate —
    `ulaw_to_pcm(payload, self._twilio_sample_rate, self._sample_rate, self._input_resampler)` (`:256-257`),
    where `self._sample_rate = self._params.sample_rate or frame.audio_in_sample_rate` (`:127`).
  - Both directions use stateful `create_stream_resampler()` instances (`:117-118`) — chunk-boundary
    safe, no file headers involved.
  - **auto_hang_up guard preserved in 1.4.0**: constructor raises `ValueError` listing
    missing `call_sid`/`account_sid`/`auth_token` when `auto_hang_up=True` without them
    (`serializers/twilio.py:96`) — matches bot.py:7266-7277's `have_hangup_creds` pattern
    (`ValueError` contract relied on by the current Twilio path and its RETAIN tests).

---

## 2. AWS / MiniMax services used by the app today

### 2.1 `AWSBedrockLLMService` — PRESENT

- Location: `site-packages/pipecat/services/aws/llm.py` (class at line 77)
- Introspection:

```
AWSBedrockLLMService: PRESENT
  file: .../site-packages/pipecat/services/aws/llm.py
  bases: LLMService[AWSBedrockLLMAdapter]
  __init__(self, *, model: str | None = None, aws_access_key: str | None = None,
           aws_secret_key: str | None = None, aws_session_token: str | None = None,
           aws_region: str | None = None, params: InputParams | None = None,
           settings: Settings | None = None, stop_sequences: list[str] | None = None,
           client_config: Config | None = None, retry_timeout_secs: float | None = 5.0,
           retry_on_timeout: bool | None = False, **kwargs)
  nested class InputParams(BaseModel):
    max_tokens, temperature, top_p, stop_sequences, latency, additional_model_request_fields
  Settings = AWSBedrockLLMSettings   # own fields: stop_sequences, latency,
                                     # enable_prompt_caching, additional_model_request_fields
  LIVE import FAILED: ImportError: Missing module: No module named 'aiobotocore'
```

- Import gate: needs the `aws` extra dependency **`aiobotocore`** at import time
  (`aws/llm.py` raises `Exception: Missing module` otherwise).
- Drift vs vendored 1.1.x: **identical** ctor + InputParams + Settings fields
  (`vendored-pipecat/src/pipecat/services/aws/llm.py:91-129`).
- Call-site compatibility (bot.py:2371-2387): `AWSBedrockLLMService(aws_region=, aws_access_key=,
  aws_secret_key=, aws_session_token=, settings=...)` with `Settings(model=, system_instruction=,
  max_tokens=256)` and `enable_prompt_caching=True` — all fields exist in 1.4.0
  (`model`/`system_instruction`/`max_tokens` are `LLMSettings` base fields,
  `site-packages/pipecat/services/settings.py:390-410`; `enable_prompt_caching` is the
  `AWSBedrockLLMSettings` own field). **Compatible as-is.**

### 2.2 `AWSTranscribeSTTService` — PRESENT

- Location: `site-packages/pipecat/services/aws/stt.py` (class at line 61)
- Introspection (AST + live):

```
AWSTranscribeSTTService: PRESENT
  file: .../site-packages/pipecat/services/aws/stt.py
  bases: WebsocketSTTService
  __init__(self, *, api_key: str | None = None, aws_access_key_id: str | None = None,
           aws_session_token: str | None = None, region: str | None = None,
           sample_rate: int | None = None, language: Language | None = None,
           settings: Settings | None = None,
           ttfs_p99_latency: float | None = AWS_TRANSCRIBE_TTFS_P99, **kwargs)
  Settings = AWSTranscribeSTTSettings   # own fields: (none — pass-through)
  LIVE import OK: pipecat.services.aws.stt.AWSTranscribeSTTService
  inspect.signature(__init__): ((self, *, api_key: str | None = None, aws_access_key_id: str | None = None,
      aws_session_token: str | None = None, region: str | None = None, sample_rate: int | None = None,
      language: Language | None = None, settings: AWSTranscribeSTTSettings | None = None,
      ttfs_p99_latency: float | None = 1.9, **kwargs))
```

- Drift vs vendored 1.1.x: **identical** (`vendored-pipecat/src/pipecat/services/aws/stt.py:72-84`).
- Call-site compatibility (bot.py:2362-2369: `region=, aws_access_key_id=, api_key=<secret>,
  aws_session_token=, sample_rate=, settings=Settings(language=...)`) — **compatible as-is**
  (note: the odd-looking `api_key=frozen.secret_key` maps to "AWS secret access key" per the
  class docstring, `aws/stt.py:88-90`).
- **Seam-patch call-site (G-1 critical)** — `asr_multilang` monkeypatches the module-level name
  `pipecat.services.aws.stt.get_presigned_url`. In 1.4.0 the name is still imported into the
  module namespace and called bare with the exact kwargs the patch contract expects:

```
site-packages/pipecat/services/aws/stt.py:37   get_presigned_url,        # module-level import
site-packages/pipecat/services/aws/stt.py:303  presigned_url = get_presigned_url(
    region=..., credentials={"access_key","secret_key","session_token"}, language_code=...,
    media_encoding=..., sample_rate=..., number_of_channels=...,
    enable_partial_results_stabilization=True, partial_results_stability="high",
    show_speaker_label=..., enable_channel_identification=...)
```

  The kwargs set is unchanged vs the 1.1.x-era shape the seam-patch was written against
  (bot.py:6113-6116 comment lists exactly these keys). **The seam patch is expected to transfer
  to 1.4.0 unchanged**; runtime behaviour is proven later by `test_transcribe_signing`/`asr-test`
  (not in U-001 scope).

### 2.3 `AWSNovaSonicLLMService` — PRESENT

- Location: `site-packages/pipecat/services/aws/nova_sonic/llm.py` (class at line 238)
- Introspection:

```
AWSNovaSonicLLMService: PRESENT
  file: .../site-packages/pipecat/services/aws/nova_sonic/llm.py
  bases: LLMService[AWSNovaSonicLLMAdapter]
  __init__(self, *, secret_access_key: str, access_key_id: str, session_token: str | None = None,
           region: str, model: str = 'amazon.nova-2-sonic-v1:0', voice_id: str = 'matthew',
           params: Params | None = None, audio_config: AudioConfig | None = None,
           settings: Settings | None = None, system_instruction: str | None = None,
           tools: ToolsSchema | list[FunctionSchema | DirectFunction] | None = None,
           session_continuation: SessionContinuationParams | None = None, **kwargs)
  Settings = AWSNovaSonicLLMSettings   # own fields: voice, endpointing_sensitivity
  LIVE import FAILED: ImportError: Missing module: No module named 'aws_sdk_bedrock_runtime'
```

- Import gate: needs the `aws-nova-sonic` extra dependency **`aws_sdk_bedrock_runtime~=0.4.0`**
  (py≥3.12) at import time.
- Drift vs vendored 1.1.x (`vendored-pipecat/src/pipecat/services/aws/nova_sonic/llm.py:251-267`):
  one change — `tools: ToolsSchema | None` widened to
  `tools: ToolsSchema | list[FunctionSchema | DirectFunction] | None` (superset — safe).
  Settings fields (`voice`, `endpointing_sensitivity`) identical in both trees
  (vendored `:234-235`). Default `model`/`voice_id` unchanged.

### 2.4 `MiniMaxHttpTTSService` / `MiniMaxTTSSettings` — PRESENT

- Location: `site-packages/pipecat/services/minimax/tts.py` (classes at lines 135 / 92)
- Introspection (AST + live):

```
MiniMaxHttpTTSService: PRESENT
  file: .../site-packages/pipecat/services/minimax/tts.py
  bases: TTSService
  __init__(self, *, api_key: str, base_url: str = 'https://api.minimax.io/v1/t2a_v2',
           group_id: str, model: str | None = None, voice_id: str | None = None,
           aiohttp_session: aiohttp.ClientSession, sample_rate: int | None = None,
           stream: bool = True, params: InputParams | None = None,
           settings: Settings | None = None, **kwargs)
  nested class InputParams(BaseModel):
    language: Language | None = Language.EN, speed=1.0, volume=1.0, pitch=0, emotion=None,
    text_normalization=None, latex_read=None, exclude_aggregated_audio=None
  Settings = MiniMaxTTSSettings   # own fields: speed, volume, pitch, emotion,
                                  # text_normalization, latex_read, language_boost
  LIVE import OK: pipecat.services.minimax.tts.MiniMaxHttpTTSService
```

- Drift vs vendored 1.1.x: **identical** — ctor (`vendored-pipecat/src/pipecat/services/minimax/tts.py:177-191`),
  InputParams (`:168-175`), `MiniMaxTTSSettings` fields (`:106-112`).
- Override seam used by `SimpleMiniMaxTTSService` (bot.py:623): 1.4.0 base still declares
  `async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]`
  (`minimax/tts.py:349`, same shape as `TTSService.run_tts` `tts_service.py:482`) —
  **the subclass's single override is compatible**. Constructor kwargs used by bot.py
  (`api_key`, `base_url`, `group_id`, `model`, `voice_id`, `aiohttp_session`) all still exist.

---

## 3. Extras inventory on pipecat-ai 1.4.0 (wheel METADATA `Provides-Extra`)

The deploy pin requests `[aws,aws-nova-sonic,silero,websocket]`
(`deploy/cloudformation.yaml:427`). **All four extras exist on 1.4.0** — verified against the
installed wheel's METADATA. What each pulls (`Requires-Dist` markers in the same METADATA):

| extra | exists on 1.4.0 | pulls (declared requirement) | used by |
|---|---|---|---|
| `aws` | yes | `aiobotocore<4,>=3` | `services/aws/llm.py` (AWSBedrockLLMService) at import time |
| `aws-nova-sonic` | yes | `aws_sdk_bedrock_runtime~=0.4.0` (py≥3.12) | `services/aws/nova_sonic/llm.py` at import time |
| `silero` | yes | **empty** — `onnxruntime~=1.24.3` moved to base deps in 1.4.0 | `audio/vad/silero.py` (import works with base install) |
| `websocket` | yes | `fastapi<1,>=0.115.6` | `transports/websocket/fastapi.py` at import time |
| `azure` (not in deploy pin) | yes | `azure-cognitiveservices-speech<2,>=1.47.0` | `services/azure/stt.py`, `services/azure/tts.py` at import time |

`requirements.txt` therefore keeps exactly the deploy extras; U-101 will need to decide whether
to add the `azure` extra (or take the `AzureHttpTTSService` route) — that decision is **not**
made here (Phase A scope fence).

Full base dependency set of pipecat-ai 1.4.0 (for reference): aiofiles, aiohttp, audioop-lts
(py≥3.13), docstring_parser, loguru, Markdown, nltk, numpy, Pillow, protobuf, pydantic,
pyloudnorm, resampy, soxr, openai, typing_extensions, numba (py<3.12: wait_for2), websockets,
pyyaml, pyyaml-include, onnxruntime.

---

## 4. API drift summary — vendored 1.1.x-era vs installed 1.4.0

| # | Surface | Vendored (1.1.x-era) | Installed 1.4.0 | Drift verdict | Migration impact |
|---|---|---|---|---|---|
| 1 | `AzureSTTService.__init__` | identical | — | none | — |
| 2 | `AzureSTTSettings` | `pass` | + `profanity` field | additive | none |
| 3 | `AzureTTSService.__init__` | `region: str` required | `region=None` + `private_endpoint=None` | additive | private-endpoint capable; old call shapes fine |
| 4 | `AzureHttpTTSService.__init__` | `region: str` required | `region=None` + `private_endpoint=None` | additive | as above |
| 5 | `AzureLLMService.__init__` | identical | — | none | U-102 maps LLM_ENDPOINT/DEPLOYMENT/API_VERSION 1:1 |
| 6 | `AzureRealtimeLLMService.__init__` | identical | — | none | FC-5 candidate unchanged |
| 7 | `TwilioFrameSerializer` (ctor + InputParams + hang-up guard) | identical | — | none | FC-1 / Phase C serializer work unaffected |
| 8 | `AWSBedrockLLMService` (ctor + InputParams + Settings) | identical | — | none | current bot.py call site compatible |
| 9 | `AWSTranscribeSTTService.__init__` | identical | — | none | current bot.py call site compatible |
| 10 | `AWSTranscribeSTTService` seam (`get_presigned_url` module name + call kwargs) | unchanged shape | — | none | `asr_multilang` seam-patch expected to transfer |
| 11 | `AWSNovaSonicLLMService` | `tools: ToolsSchema \| None` | `tools: ToolsSchema \| list[FunctionSchema \| DirectFunction] \| None` | widened (superset) | safe; bot.py passes tools schema lists |
| 12 | `MiniMaxHttpTTSService` / `MiniMaxTTSSettings` / `InputParams` / `run_tts` seam | identical | — | none | `SimpleMiniMaxTTSService` override compatible |

Cross-cutting pattern present in **both** trees: the "legacy init args / `params=` deprecated
since 0.0.105, `Settings` wins" convention (`default_settings` + `apply_update`). Callers may
keep either style; new code (Phase B) should prefer `settings=Service.Settings(...)`.

**Not audited here** (outside card scope): internal method bodies beyond the seams named above
(e.g. `SimpleMiniMaxTTSService` behaviour of its SSE parsing is app code, not SDK drift), frame
type internals, and non-listed pipecat modules.

## 5. Unverified / open items

- Behavioural (not signature) compatibility of the `get_presigned_url` seam at runtime —
  covered by existing `test_transcribe_signing` (REPLACE bucket, runs in U-005 baseline) and
  the Phase B integration slice; U-001 only proves the call-site shape.
- Live construction of the Azure STT/TTS classes (import requires `pipecat-ai[azure]`, not in
  the Phase A requirements by scope fence) — signatures above are wheel-source (AST) evidence.
- `AzureBaseTTSService.InputParams` full field list not enumerated (referenced by both TTS ctors;
  inherited settings fields of `TTSSettings`/`STTSettings`/`LLMSettings` bases only spot-checked
  where bot.py call sites depend on them: `model`, `system_instruction`, `max_tokens`
  (`services/settings.py:390-410`)).

## 6. Known pre-existing defect (record-only, NOT fixed — G-2)

`bot.py:783` calls `aioboto3.Session()` inside `SimplePollyTTSService` while no
`import aioboto3` exists anywhere in app code — constructing the Polly path raises
`NameError`. Recorded per plan risk G-2; Phase A must not fix/port it (Polly is replaced by
Azure TTS in U-101). Regression tests must not depend on the Polly path.
