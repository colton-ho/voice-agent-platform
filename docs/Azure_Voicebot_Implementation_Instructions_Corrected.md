# Voicebot Agent Revamp — Azure Implementation Instructions

Version: 2.0 — corrected implementation baseline
Prepared: 6 November 2026
Status: Ready for repository discovery and implementation planning; production deployment requires the gates below.

## 1. Purpose and scope

Implement a version of the existing voicebot that runs on Azure without depending on AWS services at runtime. Preserve existing business workflows, supported languages, tool integrations, monitoring, and human handoff unless an approved requirement changes them.

This is a replacement implementation specification based on the preceding architecture review. It is not a line-by-line edit of the original attachments. The original documents and repository were not available for inspection while preparing this file. Existing filenames, library versions, provider availability, infrastructure, costs, and code signatures must therefore be verified during discovery.

Do not interpret the earlier review's statement that all attachments were inspected as repository-level verification. No repository-specific implementation claim in that review should substitute for inspection and testing.

Azure-hosted does not mean that every dependency is an Azure service. This baseline uses Twilio as the external telephone provider. Any MiniMax or Anthropic-hosted inference is also an external processing dependency and needs explicit approval. A strict Azure-only processing requirement changes the provider selection and is a release-blocking decision.

## 2. Implementation decision

Use this initial implementation baseline:

| Concern | Baseline | Rule |
|---|---|---|
| Hosting | Azure Container Apps | Separate control API and real-time voice runtime |
| Telephony | Twilio Programmable Voice with bidirectional Media Streams | Confirm actual existing integration, number eligibility, routing, and transfer destinations |
| Voice engine | Cascaded Azure Speech STT → configurable LLM → Azure Speech TTS | Initial Azure-native processing baseline; benchmark before replacing production behaviour |
| Alternative TTS | Existing MiniMax through an adapter | Enable only after privacy approval and comparative testing |
| Alternative voice engine | Azure Voice Live | Feature-flagged experiment; not required for baseline completion |
| LLM | An explicitly approved, deployed model endpoint | No hard-coded model names or assumed availability |
| Data | Azure Cosmos DB for NoSQL | Design around actual history and reporting queries |
| Artifacts | Azure Blob Storage | Recording disabled until consent and retention are approved |
| Secrets | Azure Key Vault and managed identity | Third-party secrets remain necessary where the provider requires them |
| Async work | Azure Service Bus and a background worker | Post-call jobs, retries, and dead-letter handling |
| Coordination | Azure Managed Redis, if distributed runtime coordination is needed | Not a mechanism for recovering live sockets |
| Observability | Azure Monitor / Application Insights with OpenTelemetry | Exclude raw audio, credentials, and sensitive transcript content by default |
| Infrastructure | Bicep | Parameterized dev, staging, production environments |
| CI/CD | GitHub Actions with Entra workload identity federation | No long-lived Azure deployment credentials |

These are design choices, not claims that every resource, SKU, model, or feature is available in the intended region. Verify subscription support before provisioning.

### 2.1 ACS is not the default PSTN implementation

Microsoft's current Call Automation overview carries a retirement/breaking-change warning. It states that, following a Product Terms update, Call Automation, Call Recording, and Audio Streaming support only specified Teams interoperability scenarios. Do not implement a new standalone ACS PSTN voicebot based on the previous migration plan without written confirmation that the exact use case remains supported. Do not infer a retirement date from this warning. [S1]

Keep ACS as an optional future adapter only for an approved supported scenario. If reinstated, verify contractual eligibility, number acquisition, SDK lifecycle, callback security, bidirectional streaming, interruption, and transfer behaviour. ACS stop-playback messages are distinct from stopping media streaming; adding a participant is not the same as transferring a call. [S1][S2]

## 3. Required repository discovery — complete before coding

Create `docs/azure/discovery.md` containing:

1. Actual application entry points, framework, Python/runtime version, packaging, dependency lockfile, and container startup command.
2. All AWS runtime dependencies: imports, SDK calls, IAM/signing logic, URLs, environment variables, persistence, queues, monitoring, and deployment scripts.
3. Existing telephone ingress: Twilio, SIP/RTP, Chime, carrier, number country/type, and caller routing.
4. Current speech/LLM providers, exact model IDs, prompts, agent tools, language configuration, and audio formats.
5. Session lifecycle, local `ACTIVE_SESSIONS`-style state, process model, callback paths, and monitoring subscriptions.
6. Existing transfer behaviour: blind transfer, warm handoff, conference, agent queue, and failure handling.
7. Data structures, indexes, TTL semantics, transcript/recording retention, and business identifiers.
8. Existing regression tests and representative sanitized call scenarios.
9. Known constraints: target region, subscription billing location, data residency, approved third parties, concurrency, call length, availability targets, and budget.

Produce an AWS-to-Azure dependency matrix with columns: current component, observed usage, replacement, compatibility gap, migration test, owner.

Do not invent source paths or claim a particular Pipecat version/API works without checking the installed version. Retain provider-independent tests. Replace only tests that are genuinely specific to the removed AWS implementation.

## 4. Target architecture

```text
Caller
  → Twilio telephone number
  → HTTPS voice webhook → Control API (Azure Container Apps)
  → <Connect><Stream> → WSS endpoint → Voice Runtime (Azure Container Apps)
                                             │
                                             ├─ Azure Speech STT
                                             ├─ LLM provider adapter
                                             ├─ Tool execution gateway / existing business APIs
                                             └─ Azure Speech TTS → Twilio media messages

Twilio status callbacks → Control API → durable call state / owner command routing
Voice Runtime → Cosmos DB: call events, final state, transcript subject to policy
Voice Runtime / Control API → Service Bus → post-call worker
Worker → summaries / approved artifacts in Blob Storage
All components → redacted telemetry → Azure Monitor
Azure workloads → managed identity → authorized Azure resources
```

Use a provider-facing public HTTPS/WSS endpoint. Keep datastore and management access private where supported and approved. Do not require Entra login on Twilio callback routes; authenticate these using Twilio's documented request verification instead. Protect administrative and monitor interfaces separately with Entra ID.

Do not put APIM or Front Door in the audio path by default. Add a proxy only after testing its WebSocket behaviour, connection limits, routing, authentication, timeouts, and operational need. Do not claim WAF protection inspects the audio stream itself.

## 5. Code organization and interfaces

Suggested structure; adapt to the existing repository rather than creating a second application unnecessarily:

```text
src/
  api/                 # Telephone webhooks, status callbacks, admin endpoints
  runtime/             # Session lifecycle, queues, turn state, interruption
  providers/
    telephony/         # Twilio baseline, optional future adapters
    speech/            # Azure STT/TTS, optional approved alternatives
    llm/               # Approved endpoint adapters
    realtime/          # Optional Voice Live adapter
  tools/               # Validated tool schemas and authorization
  storage/             # Cosmos, Blob, shared coordination
  workers/             # Idempotent post-call jobs
  telemetry/           # Traces, metrics, redaction
infra/azure/           # Bicep and environment parameter templates
scripts/               # Data migration, reconciliation, smoke tests
 tests/                # Place under actual repository test conventions
```

Define testable contracts, using existing framework patterns where possible:

- `TelephonyAdapter`: validate webhook, start stream, decode incoming media, encode outgoing media, clear playback, transfer, hang up.
- `SpeechRecognizer`: start, push audio, partial/final transcript events, stop.
- `LanguageModel`: stream response, emit tool requests, cancel response, report usage.
- `SpeechSynthesizer`: stream audio, cancel synthesis, expose output format.
- `RealtimeVoiceEngine`: optional end-to-end voice implementation behind the same session boundary.
- `CallRepository`: create/read/update with optimistic concurrency, append deduplicated events, finalize, query history.
- `SessionDirectory`: reserve capacity, bind call to owner, renew lease, route commands, release.

Avoid broad interface abstractions that force incompatible provider behaviour into a misleading single method. Preserve capability flags and provider-specific protocol handling inside adapters.

## 6. Twilio integration

### 6.1 Webhook and stream setup

1. Implement a signed inbound voice webhook that resolves the configured agent and tenant from trusted server-side configuration.
2. Validate `X-Twilio-Signature` using the official SDK and the externally visible request URL. Account for ingress/proxy URL rewriting; trust forwarded headers only from trusted infrastructure.
3. Return valid TwiML using `<Connect><Stream>` to establish a bidirectional stream. Twilio documents this as the bidirectional Media Streams mechanism. [S3]
4. Use HTTPS/WSS on port 443. Validate the WebSocket upgrade's Twilio signature before accepting it. [S3]
5. Correlate the authenticated stream with a reserved call. Verify AccountSid/CallSid/StreamSid consistency and reject unknown, expired, or mismatched sessions.
6. Keep sensitive data out of URLs and stream parameters. Do not treat an unverified caller-provided tenant identifier as authorization.
7. Handle stream start, media, mark, stop, malformed payloads, and early disconnects.
8. Add signed status callbacks. Persist important events durably before acknowledging; deduplicate retries.
9. Configure an approved fallback TwiML or transfer route if the stream cannot start or the application is unavailable.

### 6.2 Audio contract

Twilio outbound stream audio must be base64-encoded raw `audio/x-mulaw` at 8 kHz, without WAV or other file headers. Twilio buffers media in send order and provides `clear` and `mark` messages for playback control. [S4]

Implement and test:

```text
Inbound:  Twilio base64 μ-law/8 kHz → decode → normalized PCM → configured STT input
Outbound: configured TTS audio → streaming resampler/μ-law encoder → base64 → Twilio
```

Validate actual incoming metadata and each provider's configured format. Do not merely relabel 8 kHz audio as 16 or 24 kHz. Resampling does not restore bandwidth lost over PSTN. Use a maintained audio implementation compatible with the chosen runtime; do not assume deprecated standard-library audio modules exist.

Use bounded queues, streaming resampler state, explicit output pacing, and per-call memory limits. Avoid buffering complete responses. Include waveform fixtures to test framing, sample rates, clipping, silence, and malformed frames.

### 6.3 Barge-in and playback accounting

On confirmed caller speech:

1. Increment a response generation/epoch identifier.
2. Cancel active model generation and TTS work where supported.
3. Flush locally queued audio from previous generations.
4. Send Twilio `clear` for the active stream.
5. Discard late model/TTS callbacks from previous generations.
6. Continue recognizing caller speech and start the next response only after the turn policy permits it.

Use `mark` acknowledgements to track playback. Marks received after a clear may represent cleared audio, not audio actually heard; maintain a ledger that distinguishes those outcomes. [S4]

Record both generated and confirmed-played durations where possible. Apply an explicit policy for retaining or truncating interrupted assistant content in conversation history.

### 6.4 Human handoff

Implement `transfer_to_human` as a controlled business action, not an arbitrary phone-number dialer.

- Resolve targets from an approved directory or allowlist.
- Check tenant authorization and outbound destination policy.
- For blind handoff, validate a Twilio call-update/TwiML Dial flow in a real call.
- For warm handoff, implement a separately tested conference or supervised handoff workflow.
- Account for `<Connect><Stream>` blocking later TwiML until the stream ends; test the chosen call-update/stream-close sequence rather than assuming fall-through. [S3]
- Record requested, connecting, connected, failed, and returned-to-bot states where applicable.
- Do not drop the caller solely because the agent is busy or the handoff request times out.
- Test destination no-answer, busy, rejected call, caller disconnect during transfer, and duplicate callbacks.

Transfer API signatures and TwiML must be verified against the installed SDK and current official transfer documentation before implementation.

## 7. Speech and LLM implementation

### 7.1 Cascaded baseline

- Use continuous streaming recognition, not a repeated single-utterance call over a live unbounded stream.
- Configure explicit recognition language/locale and supported language-detection behaviour. Do not assume code-switching works automatically.
- Make endpointing and VAD settings configurable and benchmarked with telephone audio.
- Stream LLM output into bounded phrase-level TTS synthesis without waiting for the entire answer.
- Define timeouts and cancellation at every stage.
- Avoid double turn detection that truncates caller speech.
- Apply a policy for uncertain ASR, silence, repeated misunderstanding, and human escalation.

### 7.2 LLM selection

Select an exact model deployment only after verifying regional availability, account eligibility, quota, hosting/processing location, authentication, tool support, latency, and data terms.

Do not preserve the original assertion that Claude is categorically unavailable through Azure/Foundry. Conversely, do not assume that a model accessible through Foundry is necessarily processed entirely inside Azure or available to the intended account. Verify the exact offer before selecting it.

Require structured tool schemas, validation, tenant authorization, argument limits, timeouts, and idempotency for side effects. Caller text and model output never grant authorization. Confirm consequential business changes with the caller according to the existing business workflow.

### 7.3 Optional Voice Live

Keep Voice Live disabled for the first baseline release unless it wins the approved benchmark.

Before enabling it, record the exact endpoint, API version, model ID, voice, audio formats, hosting geography, feature availability, cancellation behaviour, tool-event handling, and applicable billing meters. Do not invent a separately selectable `Lite/Standard/Pro` tier or silently substitute a model. Avoid defaulting to a preview API in production without explicit approval.

Run the same call tests against both pipelines. Do not change telephony and the voice engine in the same production cutover unless the risk is explicitly approved.

## 8. Runtime lifecycle and scaling

### 8.1 State model

Implement an explicit call state machine, for example:

```text
RECEIVED → CONNECTING → ACTIVE → HANDOFF_PENDING → HANDED_OFF
                         └────────────────────→ ENDED
Any nonterminal state → FAILED, with a reason and cleanup action
```

Use idempotent state transitions and event deduplication. A late event must not revive a finalized call. Treat provider event ordering as untrusted unless guaranteed by the documented protocol.

### 8.2 Separate control and media ownership

A callback or admin request can reach a different replica from the live media connection. Do not rely on cookie affinity, a global Python dictionary, or a shared database alone to locate the active runtime.

- Maintain a call-to-runtime-owner lease in shared storage.
- Reserve the call before media attachment and bind the actual owner atomically on authenticated stream acceptance.
- Route commands through an owner-addressable mechanism such as a tested Redis messaging design or dedicated broker topology.
- Require command IDs, acknowledgement, bounded retries, and idempotent command handling.
- Keep post-call durable work on Service Bus; do not assume a shared competing-consumer queue automatically targets the right runtime owner.
- Document command-loss behaviour if using non-durable Pub/Sub.

Shared state does not migrate an established telephone or model WebSocket after a process crash. Define caller fallback or termination for owner loss; do not claim transparent live-call recovery.

### 8.3 Container Apps deployment

- Start the voice runtime with at least one warm replica for controlled testing.
- Begin with one application worker per runtime container unless inter-process ownership is explicitly implemented.
- Treat one replica as a validation configuration, not high availability.
- Configure resource requests, memory limits, admission capacity, probes, minimum/maximum replicas, and a tested shutdown grace setting in IaC.
- Measure maximum safe concurrent calls per replica. Do not use HTTP requests-per-second as the sole proxy for voice capacity.
- On overload, reject or reroute new calls with a defined caller experience.
- On shutdown, stop admitting calls, mark the owner draining, cancel background work safely, and clean up leases.
- Test scale-in and platform termination; shutdown grace does not guarantee every call completes.

Use a tested blue/green media-endpoint scheme for releases: route new calls to the new deployment while existing calls continue on the old one until drained. Retain old deployment capacity until no active calls remain or an approved drain deadline is reached. Do not describe ordinary revision switching as automatically non-disruptive.

## 9. Persistence and migration

### 9.1 Data design

Create a workload-driven Cosmos DB design before creating containers. Suggested starting entities:

- `calls`: call ID, tenant ID, provider IDs, agent/config version, timestamps, state, owner metadata, handoff outcome, retention expiry.
- `call_events`: immutable event ID, call ID, tenant ID, event type, occurrence/ingest timestamps, sanitized payload.
- `transcripts`: consent/retention-controlled segments or Blob references.
- `usage`: raw provider usage with units, model/meter identifiers, and reconciliation status.

Choose partition keys and indexes based on measured reads/writes. Do not assume `/call_id` plus a composite index reproduces a caller-history secondary index without cross-partition cost. Use tenant-scoped normalized caller lookup or a dedicated lookup container when justified. Define hot-partition and large-tenant behaviour.

Do not store audio blobs or credentials in Cosmos items. Use private Blob Storage with narrowly authorized access.

### 9.2 TTL and retention

Cosmos TTL is relative to item last modification, not an immutable business expiry. Keep `expires_at` as the business retention boundary, calculate the remaining TTL on writes, and avoid resetting expired records into another full retention window. Verify current Cosmos TTL rules during implementation.

Enforce expiry on reads as well as eventual physical deletion. Define retention independently for call metadata, transcripts, recordings, logs, backups, and migration snapshots. Delete or suppress already-expired records during import. Legal hold, if required, must be an explicit approved policy.

### 9.3 Migration procedure

1. Export a consistent source dataset and record its cutover boundary.
2. Transform IDs, timestamps, expiry, encodings, and provider references explicitly.
3. Import a small test set with deterministic idempotent upserts.
4. Reconcile counts, business-key uniqueness, representative queries, timestamps, and expiry.
5. Choose a documented delta-capture or write-freeze strategy; do not assume snapshots capture subsequent changes.
6. Run the full migration and final delta process.
7. Retain rollback exports under the approved retention policy.
8. Reconcile writes created on Azure before any rollback; routing calls back to AWS alone does not synchronize data.

## 10. Security and networking

- Use separate environment resources and workload identities.
- Use managed identity and least-privilege data-plane roles where supported by each selected service.
- Retrieve third-party credentials from Key Vault; never commit them or print them.
- Use GitHub OIDC federation for deployment with separate environment approval gates.
- Restrict administrative endpoints with Entra authentication and authorization.
- Validate all telephone webhooks and media upgrades. Do not substitute a static secret query parameter for documented provider verification.
- Limit payload sizes, session duration, tool arguments, concurrency, and outbound destinations.
- Validate tool URLs against explicit policies; prevent SSRF and credential forwarding.
- Use restricted egress and private endpoints where feasible; retain the public endpoints required by the chosen provider.
- Do not infer that Foundry access, an Azure region setting, or local application hosting establishes end-to-end data residency.
- Make recording consent, transcript access, deletion, and privacy review release gates.
- Review dependency vulnerabilities, image scanning, container privileges, and log redaction in CI.

## 11. Configuration contract

The following names are application-defined configuration placeholders, not claims about Azure SDK arguments:

```dotenv
APP_ENV=development
AZURE_LOCATION=<validated-region>
PUBLIC_CONTROL_BASE_URL=https://<control-host>
PUBLIC_MEDIA_BASE_URL=wss://<media-host>
TELEPHONY_PROVIDER=twilio
VOICE_ENGINE=cascade
STT_PROVIDER=azure_speech
TTS_PROVIDER=azure_speech
LLM_PROVIDER=<approved-adapter>
LLM_ENDPOINT=<verified-endpoint>
LLM_DEPLOYMENT=<exact-approved-deployment>
LLM_API_VERSION=<verified-if-required>
SPEECH_REGION=<validated-speech-region>
SPEECH_ENDPOINT=<verified-endpoint-if-required>
STT_LOCALE=<validated-locale>
TTS_VOICE=<validated-voice>
COSMOS_ENDPOINT=<account-endpoint>
COSMOS_DATABASE=voicebot
BLOB_ACCOUNT_URL=<account-url>
SERVICEBUS_NAMESPACE=<fully-qualified-namespace>
REDIS_ENDPOINT=<if-coordination-enabled>
KEY_VAULT_URL=<vault-url>
TWILIO_ACCOUNT_SID=<account-id>
TWILIO_AUTH_TOKEN_SECRET_NAME=<key-vault-secret-name>
MAX_CONCURRENT_CALLS_PER_REPLICA=<load-tested-value>
MAX_CALL_DURATION_SECONDS=<approved-limit>
RECORDING_ENABLED=false
TRANSCRIPT_RETENTION_DAYS=<approved-value>
VOICE_LIVE_ENABLED=false
APPLICATIONINSIGHTS_CONNECTION_STRING=<environment-specific-value>
```

Commit only an example configuration without real values. Validate required variables at startup. Never silently fall back to AWS, a different model, or an unapproved external provider.

## 12. Infrastructure and CI/CD deliverables

Implement Bicep modules for:

- Container Apps environment and control/runtime/worker apps.
- Azure Container Registry.
- Managed identities and explicit role assignments.
- Key Vault and environment-specific secret references.
- Cosmos DB with approved consistency, capacity, indexing, and backup configuration.
- Blob Storage with public access disabled and approved lifecycle settings.
- Service Bus queues, retry/dead-letter configuration, and authorized identities.
- Optional Azure Managed Redis coordination resource.
- Monitor/Log Analytics/Application Insights, alert rules, budgets, and action groups.
- Speech and approved model resources where supported; document manual commercial approval/deployment prerequisites separately.
- Networking, DNS, TLS, and optional private endpoints appropriate to the selected topology.

CI/CD sequence:

1. Lint/type-check and run unit/contract tests.
2. Scan dependencies, secrets, IaC, and image.
3. Build once; identify the image by digest.
4. Run Bicep validation/what-if against the target environment.
5. Deploy staging; run storage, identity, tool, and real telephone smoke tests.
6. Require production approval.
7. Deploy the green runtime, verify readiness, and switch new-call routing.
8. Drain old runtime and monitor regressions before retiring it.

Do not apply production infrastructure or change telephone routing as part of an unapproved developer task.

## 13. Observability and cost validation

Correlate call ID, provider CallSid, stream ID, trace ID, runtime owner, agent version, and model deployment. Mask caller identifiers and restrict access.

Measure:

- Call setup success/failure and time to first greeting.
- End-of-speech to first audible response: p50/p95/p99 with measurement definition.
- STT partial/final delays, model first-token delay, TTS first-audio delay.
- Barge-in detection to playback stop; stale output after interruption.
- Active calls, admission rejections, queue depths, event-loop lag, CPU/memory.
- Tool correctness, timeout/error rate, handoff success, unexpected disconnects.
- Usage by provider, unit, model, tenant where applicable, and completed call.

Do not repeat the previous $102/month or 22% savings headline as a validated budget.

Use this cost structure:

```text
Telephone minutes by destination/number type/direction
+ Media Streams charges
+ number rental
+ transfer/conference legs and recording where used
+ actual STT/TTS/model/Voice Live usage
+ Container Apps active and idle usage
+ Cosmos capacity/RUs/storage
+ Blob capacity/operations
+ Service Bus and coordination
+ networking/egress
+ observability ingestion and retention
+ optional edge/security services
```

Retrieve current regional prices before budgeting. Do not assume Media Streams is free, VAD automatically halves billable STT duration, all voice models share an audio-token ratio, or a warm container has a fixed monthly price. Provider usage counters are provisional until reconciled with billing meters and invoices.

## 14. Implementation phases and completion conditions

### Phase A — Discovery and decisions

Deliver `discovery.md`, dependency matrix, architecture decision records, privacy/provider approvals, and the test inventory. Resolve all blocking decisions in section 16.

### Phase B — Local provider-neutral runtime

Implement interfaces and deterministic test fixtures; preserve business tools and regression tests. Add Azure adapters behind flags. Demonstrate local streaming, interruption, lifecycle cleanup, and idempotent tools without committing credentials.

### Phase C — Azure staging vertical slice

Provision staging through Bicep. Complete a real inbound telephone call through Azure Speech, approved LLM, tool execution, TTS, persistence, and post-call worker. Prove webhook authentication and managed-identity access.

### Phase D — Production hardening

Implement distributed ownership, command routing, concurrency admission, blue/green draining, migration scripts, security checks, telemetry, budget alerts, and operator runbooks. Load-test the actual telephone path where feasible and document simulator limitations.

### Phase E — Canary and cutover

Route a controlled subset of new calls to Azure. Keep existing AWS service available for rollback. Increase traffic only after agreed observation windows and acceptance thresholds. Remove AWS dependencies only after the approved rollback period and data reconciliation.

Optional Voice Live and ACS work must not block delivery of the tested baseline unless explicitly promoted through an architecture decision.

## 15. Acceptance test matrix

| Area | Required evidence |
|---|---|
| Azure hosting | Deployed Azure runtime completes calls with no AWS runtime SDK calls, credentials, or service endpoints |
| Audio | Correct μ-law/PCM conversion, clear speech, no headers in Twilio payload, no sustained one-way audio |
| Interruption | Local/provider buffers cleared, old-generation audio discarded, conversation history handled consistently |
| Languages | Representative Cantonese, Mandarin, English, and Japanese tests if those languages are confirmed in scope |
| Tools | Schema validation, tenant authorization, caller confirmation where needed, retry idempotency |
| Handoff | Success and no-answer/busy/failure flows; no premature caller drop |
| Callbacks | Invalid signature rejected; duplicates and late events do not corrupt call state |
| Scaling | Agreed concurrent calls sustained with documented per-replica capacity and overload response |
| Failure | Speech/model/tool timeout, runtime crash, broker outage, storage failure, and lost socket tested |
| Deployment | New calls move to green while old calls drain; forced drain behaviour documented |
| Data | Counts/query results/expiry reconciled; backup restore and migration rollback rehearsed |
| Privacy | Approved consent, access, retention, deletion, and redaction behaviour |
| Cost | Actual usage collected and reconciled; equivalent telephone scenarios used for comparison |
| Rollback | Telephone routing restored and Azure-created data reconciled according to runbook |

Do not invent numerical SLOs. Obtain approval for concurrent-call target, p95 response latency, barge-in stop latency, handoff success rate, availability, retention, RPO/RTO, and monthly budget. Record the approved values in `docs/azure/acceptance.md` before release.

## 16. Blocking decisions

1. Is the requirement Azure-hosted or strictly Azure-only data processing?
2. Can the current telephone provider and number be retained? What number countries/types and outbound destinations are required?
3. Which Azure subscription and regions are approved for runtime, speech, model inference, data, and disaster recovery?
4. Which exact LLM deployment and TTS voice meet quality, hosting, quota, and commercial requirements?
5. Is human handoff blind transfer, warm transfer, or queue integration?
6. What are the peak concurrency, maximum call duration, latency targets, and failure experience?
7. Are transcripts and recordings permitted, and under which consent/retention policies?
8. What is the final source data schema and acceptable migration freeze/delta method?
9. Are Redis coordination, Service Bus, and additional warm capacity approved in the operational budget?

Unanswered decisions must appear as explicit blockers or approved temporary assumptions, not hidden defaults.

## 17. Instructions for the coding agent

Follow this document as the Azure target specification.

1. Inspect the existing repository and report evidence before making repository-specific assertions.
2. First produce discovery, dependency matrix, proposed file changes, architecture decisions, and a phased implementation checklist.
3. Preserve current business logic, prompts, and tests unless an approved change requires otherwise.
4. Implement one working Azure vertical slice before large-scale refactoring.
5. Verify package/API versions against the installed dependencies and current official documentation.
6. Add unit, contract, integration, real-call, interruption, transfer, and failure tests as appropriate.
7. Generate Bicep, example configuration, CI/CD, migration scripts, and operating runbooks.
8. Report changed files, commands actually executed, results, unresolved blockers, and manual prerequisites after each phase.
9. Never claim deployment, live-call validation, quota approval, billing validation, or production readiness without evidence.
10. Do not delete AWS production resources, rotate live telephone routing, purchase numbers, or deploy production changes without explicit authorization.

## 18. Verified references and verification boundary

The following official documentation informed the telephone integration and the ACS support warning. Recheck current service terms and API versions at implementation time.

- [S1] Microsoft Learn — Call Automation overview: https://learn.microsoft.com/en-us/azure/communication-services/concepts/call-automation/call-automation
- [S2] Microsoft Learn — Audio streaming quickstart: https://learn.microsoft.com/en-us/azure/communication-services/how-tos/call-automation/audio-streaming-quickstart
- [S3] Twilio — Media Streams overview: https://www.twilio.com/docs/voice/media-streams
- [S4] Twilio — Media Streams WebSocket messages: https://www.twilio.com/docs/voice/media-streams/websocket-messages

Other Azure choices in this specification are implementation requirements and recommendations, not subscription-specific availability or pricing confirmations. Validate current Container Apps lifecycle limits, Cosmos TTL behaviour, identity support, Speech/Foundry deployment compatibility, regional SKUs, current prices, and Twilio transfer APIs before finalizing the deployment.
