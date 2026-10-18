# Demo Generator: Research-Backed System Design

## Context

The goal is to turn a **YAML playbook** plus an **author's intent** into a finished, narrated product-demo MP4 with no manual editing:

1. **Recorder**: an LLM reads the playbook and drives Playwright to perform the steps on screen while the session is recorded.
2. **Story-teller**: understands the playbook, the recorded frames and the author's intent. It writes narration that fits the video, then voices it with TTS. STT is used for QA and timing.
3. **Editor**: combines video and audio (plus captions, cursor and zoom) into the final file.

The system must work with any LLM provider without being tied to one SDK. It must also be resumable from any stage (LangGraph state, interrupts, resume), and start as a CLI built to grow into a service.

### Decisions
| Topic | Decision |
|---|---|
| Audio | TTS for narration, plus STT (faster-whisper/WhisperX) for QA, word timestamps and captions |
| Timing | **Hybrid per-step**: record once and log per-step windows. Write narration per step to fit its window. The editor freeze-frames or trims idle footage to line steps up, with a tempo change of ±10% at most |
| Stack | **Python core** (LangGraph Python, Playwright Python) |
| Scale | **CLI first, service-ready**: SQLite and local FS behind interfaces, later swapped for Postgres, S3 and workers |
| LLM driving | **Resolve once, replay**: a rehearsal pass resolves steps into a cached lockfile, the take is replayed deterministically, and self-heal runs only on failure |
| Targets | Public, authenticated and localhost/staging web apps in v1. **Desktop UI is phase 2** (interfaces are designed now; OS not yet decided) |
| Licensing | **Commercial-safe defaults only** (Apache/MIT/BSD/CC-BY). Paid APIs are opt-in plugins |
| HITL | Default interrupts: **approve the action plan** (before the take) and **approve/edit the story** (before TTS) |
| v1 polish | Burned-in captions + SRT, cursor highlight + click ripples, auto-zoom on action. No intro/outro or music in v1 |

---

## Research summary (what informed each choice)

**Prior art (validates the approach):**
- `tandryukha/aidemo`: a declarative storyboard, deterministic replay, an audio-first fit in which only idle footage is trimmed or held, Kokoro locally, captions from Whisper word timestamps, and ffmpeg compose.
- `grodriguez1983/ai-demo-generator-oss`: YAML segments with `narration_hint`. It records twice, calibrating dwell to audio length because Playwright navigation drifts. It runs whisper.cpp QA gates.
- Lesson from both: **timing drift is the core hard problem**. Both log per-step timestamps and fit at step granularity instead of fitting to the total video length.

**Browser automation:**
- Playwright MCP / `@playwright/cli` work from **accessibility snapshots with element refs**. The CLI writes snapshots to disk and uses about 4× fewer tokens.
- Stagehand popularised "AI act() + selector caching". browser-use re-reasons every step (expensive, nondeterministic).
- Conclusion: an in-process Playwright toolset that exposes ARIA snapshots and refs to the LLM during **rehearsal**, then a cached deterministic replay for the **take**. That is the Stagehand pattern, built ourselves so it stays LLM-agnostic.

**Screen capture:**
- Playwright `recordVideo` has its bitrate hardcoded at about 1 Mbit/s on Chromium. It is not configurable and produces artifacts, so it is not good enough for marketing-quality demos.
- Playwright 1.59 added `page.screencast` (frame streaming, action annotations).
- **CDP `Page.startScreencast` → ffmpeg** (Puppeteer's ScreenRecorder pattern) gives control over CRF, fps and scale. Frames arrive only on change, so they must be timestamped and converted from VFR to CFR.
- **Headful Chrome under Xvfb + ffmpeg x11grab** is the most proven smooth capture in Docker. It is the service-phase recorder.
- OBS + obs-websocket only pays off for multi-source scenes (and for desktop in phase 2).
- rrweb replay misses canvas, WebGL and shadow DOM, so it is rejected.

**TTS (commercial-safe):**
- **Kokoro-82M** (Apache-2.0, runs on CPU, ranked high in 2026 open-weight leaderboards, fixed voices). This is the default.
- **Chatterbox** (MIT, cloning from about 5 s of audio, multilingual) for a brand voice.
- Excluded by default as non-commercial: Fish S2 Pro, Voxtral, some F5-TTS weights.
- Excluded as research-only/pulled: VibeVoice.
- Paid plugins: ElevenLabs and OpenAI-compatible endpoints.

**STT/alignment:**
- **faster-whisper** (MIT) as the default, for mature word timestamps.
- **WhisperX** forced alignment for precise word boundaries.
- Parakeet v3 (CC-BY-4.0) as an optional, faster English engine.

**Orchestration:**
- LangGraph saves a checkpoint after every node (SqliteSaver for dev, PostgresSaver for production). `interrupt()` + `Command(resume=…)` handle HITL, keyed by `thread_id`.
- Caveat: there is **no durability inside a node** and no automatic crash re-entry. We mitigate this with idempotent, content-addressed nodes and a `resume` command.
- Temporal and Restate add real durable execution but cost extra infrastructure. They are deferred, and the interfaces leave room for them.

**LLM abstraction:**
- LiteLLM gives one OpenAI-shaped API, including tool calls and vision, across 100+ providers (Anthropic, OpenAI, Gemini, Bedrock, Ollama, vLLM).
- LangChain `init_chat_model` is the alternative.
- The plan: our own thin `LLMProvider` protocol, with LiteLLM as the default adapter, so no business code imports a vendor SDK.

**Video understanding:**
- For UI frames, **resolution matters more than frame count**. Uniform 1 fps sampling is a weak baseline.
- So: **event-driven keyframes** (step boundaries, post-action, scene changes) at full resolution, sent to any vision-capable LLM through the protocol.

**Editor:**
- Raw ffmpeg expressions for cursor and zoom are unmaintainable.
- MoviePy is slow on long frame sequences.
- Remotion is source-available and needs a company license.
- So: a **PyAV/NumPy per-frame compositor** (cursor, ripple, zoom) piped to ffmpeg, plus ffmpeg/libass for captions, audio mixing and loudnorm.

---

## Orchestration: options researched and the tradeoff

**What the workload needs.** The pipeline is fixed and mostly linear: about 12 stages, 2 human pauses, 1 bounded rewrite loop and 1 fan-out. Runs take minutes and volume is low (demos, not events). The "agentic" parts (the rehearsal tool loop and the story writer) live *inside* single stages, so the orchestrator never has to model LLM reasoning. Two separate capabilities are needed:

- **(A) Run-state semantics**: checkpoint, pause for a human, resume, fork/rerun from a stage, loop.
- **(B) Distributed execution** (service phase): a queue, separate worker pools (browser / GPU / CPU), concurrency limits and crash recovery.

**Cost reality.** Orchestrator fees are negligible at this volume next to LLM, TTS and render compute. The real costs are **infrastructure to operate** and **engineering time**.

| Option | License | (A) State / HITL / fork | (B) Queue / pools / recovery | Infra to run | Verdict |
|---|---|---|---|---|---|
| **LangGraph** (library) | MIT | **Best.** `interrupt()`/`Command(resume)`, checkpoint history, `update_state` fork, `Send` fan-out | **None.** Runs in one process, no auto-recovery, no worker dispatch. LangGraph Platform adds it at about $0.001 per node, or self-hosted through sales | None for the CLI. Postgres plus *your own queue* for the service | Strong on A. Needs a second system for B |
| **DBOS Transact** (library) | MIT (verify) | Good. Step checkpoints, `recv`/`send`/events for HITL, durable sleep, fork from step (by index, less ergonomic) | **Built in.** Durable Postgres queues with global and per-worker concurrency, rate limits, auto-recovery on restart | Postgres only (SQLite for dev) | Best single-dependency balance. Younger. Its management UI (Conductor) is paid, about $99/mo and up. Workflow code must be deterministic |
| **Temporal** | MIT server | Good. Signals/updates for HITL, reset-to-event | **Best.** Task queues per pool, retries, timeouts, proven at very large scale | Self-host means 7+ components plus a DB (estimates from $300 to $4,500/mo plus ops time). Cloud is about $50 per 1M actions, which is cents per demo | Overkill for v1. Steep learning curve. Right at high multi-tenant scale |
| **Restate** | Server BSL 1.1 (my recollection, unverified), SDK MIT | Good. Journaled steps, awakeables for HITL | Good. Single binary, suspends when idle | One binary, but needed even for the CLI | BSL blocks some orgs. Smaller Python ecosystem |
| **Hatchet** | MIT (one source says Apache-2.0) | Fair. DAG plus durable tasks/events | **Good.** Postgres-backed queue, worker affinity (GPU routing), fairness, built-in UI | Engine + Postgres | A good *executor* under another control plane. Heavy for a CLI |
| **Prefect 3** | Apache-2.0 | Good. `pause/suspend_flow_run` with typed input (`wait_for_input` is experimental), task cache keyed on inputs | Good. Work pools (Docker/K8s), OSS UI | Server + DB for pause and UI | Viable, but oriented to data pipelines. Resume re-runs the flow and relies on the cache |
| **Dagster** | Apache-2.0 | Poor fit. Assets are global, not per-run. No HITL primitive | Good | Daemon + webserver + DB | Rejected |
| **Burr / pydantic-graph / LlamaIndex Workflows** | Apache / MIT | Like LangGraph but thinner (Burr: SQLite/PG persisters + UI; pydantic-graph: file persistence only; LlamaIndex: ephemeral unless paired with DBOS) | None | None | Same class as LangGraph with smaller ecosystems. No gain |
| **Inngest / Trigger.dev / Step Functions / Argo** | Mixed | — | — | Metered per execution, TS-first, or cloud/K8s lock-in | Rejected for a Python, CLI-first, self-hosted system |

**Key design consequence.** Stages are already engine-agnostic: plain functions taking and returning artifact refs, idempotent and content-addressed. The engine therefore touches only about one file (`graph/build.py`), and "rerun from stage X with changed inputs" works on *any* engine through the artifact cache. So the choice can be reversed later.

**Decision: LangGraph as control plane + a pluggable `StageExecutor`.**
- LangGraph owns run state (A), where it is strongest and already familiar.
- A `StageExecutor` port runs each stage:
  - `LocalExecutor` in v1 (in-process, zero infrastructure).
  - `QueueExecutor` in the service phase (Hatchet, or arq/Redis), which routes `record` to browser workers, `tts`/`stt` to the GPU pool and `render` to the CPU pool. The remote stages are idempotent, so at-least-once delivery is safe and LangGraph stays the single source of truth.
- A small stale-run reaper re-invokes threads whose heartbeat has expired. This covers LangGraph's lack of auto-recovery.
- Checkpoints hold refs only, with a TTL, so there is no checkpoint bloat.

**Strongest alternative: DBOS end-to-end.** One dependency covers A and B, and the service infrastructure is Postgres only. The costs: weaker fork/time-travel ergonomics, a less natural fit for short-lived CLI processes, and a younger project.

**Escalation path.** If volume or multi-tenancy outgrows this, port `graph/build.py` to Temporal.

---

## Architecture

```
playbook.yaml + intent.md
        │
        ▼
┌────────────────────────── LangGraph StateGraph (thread_id = run_id) ───────────────────────────┐
│ load_validate → setup(seed, auth) → rehearse ──► [INTERRUPT approve plan] → record_take         │
│   → verify_take → extract_keyframes → analyze_frames(VLM) → write_story ──► [INTERRUPT story]   │
│   → synthesize (fan-out per step via Send) → qa_audio(STT) → plan_timeline ──┐                  │
│        ▲                                                   too-long steps    │                  │
│        └──────────────────── write_story (rewrite flagged steps, max N) ◄────┘                  │
│   → render → final_qa → done                                                                    │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
   Checkpointer: SqliteSaver (v1) │ PostgresSaver (service)
   ArtifactStore: LocalFS (v1) │ S3/MinIO (service)    State holds artifact refs + hashes, never blobs
```

### Control plane vs execution plane
- **Stage** = a pure function `(StageInput refs, config) -> StageOutput refs`, registered by name in `stages/`. It has no LangGraph imports.
- **LangGraph node** = a thin wrapper: `executor.run(stage_name, input_refs)`, then merge the output refs into state. Interrupts, loop edges and `Send` fan-out live only in `graph/build.py`.
- **`StageExecutor` port** (`exec/base.py`):
  - `LocalExecutor` (v1, in-process).
  - `QueueExecutor` (M5): submits to a named pool (`browser`, `gpu`, `cpu`) and awaits the result. Delivery is at-least-once, which is safe because stages are idempotent and content-addressed. The backend is Hatchet or arq/Redis, chosen in M5.
- **Reaper** (M5): each run writes a heartbeat. Stale threads are re-invoked with `graph.invoke(None, config)` from their last checkpoint.
- **Checkpoint hygiene**: state holds refs and hashes only, with a TTL/pruning policy. A `state_version` field and a migration hook guard against schema drift on old threads.

### Cross-cutting principles
- **Idempotent, content-addressed nodes.** Each node computes `key = hash(node_version, inputs, config)`. If the artifact already exists, the node skips. This gives crash-resume (`demogen resume`) and cheap partial reruns (e.g. a new intent re-runs only the story, voice and render nodes, with no re-record).
- **Plugin registries** through Python entry points: `demogen.llm`, `demogen.tts`, `demogen.stt`, `demogen.driver`, `demogen.recorder`. They are selected in config.
- **Per-role models** in config: `planner` (rehearsal tool use), `vision` (frame analysis), `writer` (narration). Each can use a different provider or model.
- **Secrets**: the playbook references `${secret:NAME}`, resolved from env or a secret store. Secrets are redacted from logs and from every LLM prompt, and typed through the executor without ever passing through the LLM.

### Component design

**1. Playbook (`playbook/schema.py`, Pydantic)**: BDD-flavoured, intent-level steps with optional hard overrides:
```yaml
version: 1
name: Create an invoice
target:
  kind: web                     # phase 2: desktop
  base_url: https://staging.example.com
  auth: { profile: staging-admin }        # Playwright storage_state, created by setup
  viewport: { width: 1440, height: 900, device_scale_factor: 2 }
setup:                          # runs before the take, not recorded
  seed: "make seed-demo"
  steps: [{ do: "Log in as ${secret:DEMO_USER}" }]
intent:                         # optional; can also come from intent.md / CLI
  audience: finance managers
  tone: confident, friendly
  key_messages: ["Invoices in under a minute"]
  cta: "Start a free trial"
steps:
  - id: open-invoices
    do: "Open Invoices from the sidebar"
    expect: "Invoice list is visible"
    say_hint: "Everything starts in one place"
    dwell: 1.5s
  - id: new-invoice
    do: "Create an invoice for Acme Corp for 1200"
    data: { customer: Acme Corp, amount: 1200 }
    expect: "Draft shows total $1,200"
    focus: "the total field"     # zoom hint
    action:                      # optional override; skips LLM resolution
      - click: { role: button, name: "New invoice" }
```

**2. LLM layer (`llm/`)**
- `LLMProvider` protocol: `complete(messages, tools?, response_schema?, images?) -> LLMResult`, with normalized tool calls and usage.
- Default adapter: `LiteLLMProvider`. Optional: `LangChainProvider` (wraps `init_chat_model`).
- Structured output uses Pydantic schemas: native JSON-schema mode where the provider supports it, a validate-and-retry fallback elsewhere.
- A minimal provider-agnostic tool loop (`llm/agent_loop.py`) runs the rehearsal agent, so there is no framework lock-in.

**3. Driver (`driver/`)**: `Driver` protocol: `rehearse(step) -> ResolvedStep`, `execute(resolved, events)`.
- `PlaywrightDriver` (v1):
  - **Rehearsal agent**: tools `snapshot()` (ARIA snapshot with refs), `click(ref)`, `fill(ref, text)`, `press`, `scroll`, `goto`, `wait_for`, `assert_expect`. The LLM resolves each `do` and verifies `expect`. Each step is stored as robust locators (role+name first, text, then CSS fallback) plus a post-condition, in `actions.lock.json` (keyed by step hash + target URL).
  - **Executor (take)**: no LLM. It moves the mouse along Bézier paths, types at a human cadence and applies `dwell`. It emits `events.jsonl` (step_start/end, action, element bbox, cursor samples, clicks) on the **recorder clock**.
  - **Self-heal**: if a cached locator fails, the take is aborted and the LLM re-resolves that step against the live snapshot. The lockfile is updated and the take restarts (takes are atomic, so the footage stays clean).
- Optional `mcp` mode: expose the same toolset **as an MCP server** so external agents (Claude Code, etc.) can author or repair lockfiles. The take always runs in-process for precise event timing.
- Phase 2: a `DesktopDriver` using vision computer use plus OS accessibility APIs.

**4. Recorder (`recorder/`)**: `Recorder` protocol: `start(ctx) -> Clock`, `stop() -> VideoArtifact`.
- `CdpScreencastRecorder` (v1 default): JPEG/PNG frames with CDP timestamps go to an ffmpeg pipe, encoded as VFR then normalized to 30/60 fps CFR. CRF is configurable and DSF=2 gives crisp text. The cursor is **not** rendered in-page; the editor draws it from events.
- `PlaywrightVideoRecorder` (fallback/debug; recordVideo or 1.59 `page.screencast`).
- `XvfbFfmpegRecorder` (service phase, Linux containers, headful Chrome).
- Phase 2: `OsCaptureRecorder` (ffmpeg avfoundation/gdigrab/x11grab, or OBS websocket).

**5. Story-teller (`story/`)**
- `extract_keyframes`: full-resolution frames at each step start, after each action, at step end, and at scene changes (ffmpeg `select='gt(scene,…)'`).
- `analyze_frames`: the vision model produces per-step **facts** (what is visible, what changed, concrete values on screen), which ground the narration in the real UI.
- `write_story`: the writer model receives the playbook, intent, facts and **per-step word budgets** (`budget = window_s × target_wps`, default 2.4 wps, ±15%). It outputs Pydantic `Story{hook?, steps:[{step_id, text, emphasis[]}], outro?}`. A deterministic validator flags over-budget steps for a targeted rewrite (bounded retries).
- Interrupt: the author approves the story or submits edits (`demogen approve <run> --edit story.yaml`).

**6. Voice (`voice/`)**
- `TTSProvider.synthesize(text, voice, speed) -> AudioClip(path, duration)`. Adapters: `kokoro` (default), `chatterbox`, `openai_compat` (also covers local servers like speaches), `elevenlabs` (paid plugin).
- Clips are synthesized per step (fan-out through `Send`, parallel and cacheable).
- `STTProvider.transcribe(audio) -> words[{w, start, end}]`. Adapters: `faster_whisper` (default), `whisperx`, `parakeet`.
- `qa_audio` computes WER against the script to catch cut-offs or hallucinations (regenerating above a threshold), trims leading and trailing silence to the true speech span, and keeps word timings for captions.

**7. Timeline planner (`timeline/`, pure and deterministic)**: per step, with video window V and speech length A:
- A ≤ V: place the audio (with configurable lead-in) and keep the footage. If idle tail time exceeds a threshold, trim or speed up **idle-only** spans (no events, no scene change).
- V < A ≤ 1.10·V: apply `atempo` to the audio (0.9–1.1).
- A > 1.10·V: **freeze-frame** the step's settled end frame, or slow idle spans to cover the gap.
- A > 1.6·V: send the step back to `write_story` to be shortened (loop edge).
- Output: `edl.json` (source spans with speed/freeze, audio placements, overlay keyframes for cursor, ripples, zoom and captions). Boundaries are snapped so A/V drift stays ≤ 200 ms per step.

**8. Editor (`editor/`)**
- `compositor.py`: PyAV decodes the source and applies the EDL spans. Per frame it draws:
  - a smoothed cursor and highlight halo from `events.jsonl`
  - click ripples
  - auto-zoom (eased crop+scale toward the action bbox or `focus`)
  The result is piped to ffmpeg (libx264, CRF ~18).
- `captions.py`: builds an ASS file from the STT word timings (styled, burned in with libass) and also exports an SRT.
- Audio: concatenate placed clips, then `loudnorm` to -16 LUFS. The audio is muxed in to produce `final.mp4`, `final.srt` and `edl.json`.
- `final_qa`: checks the ffprobe duration, runs STT on the final mix (WER against the story) and checks drift at step boundaries.

### Run directory / artifacts
```
runs/<run_id>/
  playbook.yaml  intent.md  config.resolved.yaml
  actions.lock.json  events.jsonl  raw.mp4
  keyframes/<step>/*.png  facts.json  story.json
  audio/<step>.wav  words.json  edl.json
  final.mp4  final.srt  report.json
```

### CLI (`cli.py`, Typer)
- `demogen run playbook.yaml --intent intent.md [--auto-approve]`
- `demogen status <run>`
- `demogen approve <run> [--edit story.yaml|plan.json]`
- `demogen resume <run>`
- `demogen rerun <run> --from write_story`: uses LangGraph `update_state` / checkpoint replay. Cached nodes upstream are reused.

---

## Repo layout (new repo)
```
demo-generator/
  pyproject.toml            # uv; deps: langgraph, langgraph-checkpoint-sqlite, litellm, playwright,
                            # pydantic, typer, av, numpy, pillow, kokoro, faster-whisper, jiwer
  src/demogen/
    cli.py  config.py  plugins.py
    playbook/{schema,loader,secrets}.py
    graph/{state,build}.py            # the only LangGraph-aware code
    stages/*.py                       # engine-agnostic stage functions (one per pipeline stage)
    exec/{base,local,queue}.py        # StageExecutor port; queue.py lands in M5
    llm/{base,litellm_provider,langchain_provider,agent_loop}.py
    driver/{base}.py  driver/playwright/{tools,rehearsal,executor,humanize,lockfile}.py
    recorder/{base,cdp_screencast,playwright_video}.py
    story/{keyframes,vision,writer,budget}.py
    voice/{qa}.py  voice/tts/{base,kokoro,chatterbox,openai_compat,elevenlabs}.py  voice/stt/{base,faster_whisper,whisperx}.py
    timeline/{planner,edl}.py
    editor/{compositor,captions,audio,render}.py  editor/overlays/{cursor,ripple,zoom}.py
    store/{artifacts,cache}.py
  examples/playbooks/{todomvc.yaml, local_fixture.yaml}
  tests/{unit,integration,fixtures/app/}
```

## Milestones
- **M0 skeleton**: schema, config, plugin registry, `LLMProvider` + LiteLLM, ArtifactStore, the stage registry + `StageExecutor` port with `LocalExecutor`, graph with stub stages, SqliteSaver, CLI run/status/approve/resume, and interrupt round-trip.
- **M1 record**: Playwright tools, rehearsal agent, lockfile, humanized executor, CDP recorder, `events.jsonl`, self-heal, auth `storage_state`, seed hook.
- **M2 story**: keyframes, vision facts, budgeted writer, validator/rewrite loop, story interrupt.
- **M3 voice**: Kokoro + faster-whisper, per-step fan-out, audio QA.
- **M4 timeline + editor**: EDL planner, compositor (cursor, ripple, zoom), captions, audio mix, final QA.
- **M5 service-ready**: PostgresSaver, S3 store, Dockerized `XvfbFfmpegRecorder`, FastAPI, `QueueExecutor` (Hatchet or arq/Redis) with `browser`/`gpu`/`cpu` pools, the stale-run reaper, checkpoint TTL, Langfuse/OTel tracing.
- **Phase 2**: desktop driver and OS-capture recorder (OS to be chosen later).

## Verification
- **Unit**: playbook schema and secret redaction; budget math; timeline planner table tests (every V/A branch, drift ≤ 200 ms); EDL → frame mapping; lockfile locator fallback order.
- **Integration (CI, no network keys)**: a local fixture app (static HTML served on localhost) and a `FakeLLMProvider` that replays recorded responses. Run the whole pipeline with Kokoro and faster-whisper `tiny`. Assert:
  - `final.mp4` exists
  - its ffprobe duration matches the EDL
  - STT of the final audio gives WER < 0.15 against `story.json`
  - captions are present in the SRT
- **Resume**: kill the process during `synthesize`, then run `demogen resume`. Assert that the recorder and rehearsal are **not** re-invoked (cache hits) and the run finishes.
- **Interrupt**: run without `--auto-approve`, check that it pauses at plan approval and again at story approval. `approve --edit` changes the narration of exactly the edited steps.
- **Rerun**: `rerun --from write_story` with a new intent leaves `raw.mp4` untouched and produces new audio and a new final file.
- **Engine isolation**: an import-linter rule (or a test) asserts that `langgraph` is imported only under `demogen/graph/`. Stages run standalone in unit tests through `LocalExecutor`.
- **Real-provider smoke** (manual): `examples/playbooks/todomvc.yaml` against `demo.playwright.dev/todomvc`, run with two different LLM providers through config only, to prove the system works with any provider.

## Key risks and mitigations
- CDP frame irregularity: timestamp every frame and normalize VFR to CFR. Fall back to the Xvfb recorder.
- The app's state differs between rehearsal and take: a seed hook, `expect` post-conditions checked during the take, and an abort-and-heal on mismatch.
- No durability inside a node: the take is atomic, and every node is idempotent with cached artifacts.
- LLM nondeterminism: locked actions, schema-validated outputs and bounded rewrite loops.
- Secrets leaking to the LLM: values are resolved only inside the executor, and snapshots and prompts are redacted.

## Implementation notes (where the build departs from the plan above)

Recorded as each milestone lands.

**M1 (recording)**
- **A native Anthropic adapter sits beside LiteLLM.** Current Claude models only accept their
  own thinking blocks back in an unedited conversation, so each assistant turn must be replayed
  exactly as received. `LLMResult.message` therefore carries the adapter's native copy of the
  turn; business code still sees only the neutral interface.
- **Sharp video needs a launch flag, not only the viewport setting.** Chrome's screencast
  captures at CSS-pixel size unless the browser is launched with `--force-device-scale-factor`.
  With it, a 1440x900 viewport at scale 2 records at 2880x1800 and about 60 frames a second.
- **Frames are laid onto a constant-rate timeline as they arrive** (the previous frame is
  repeated across gaps), instead of encoding variable-rate and normalising afterwards.
- **The lockfile is per run.** Sharing resolved steps across runs (keyed by step and URL) is
  not built.
- **Pinned steps are trusted.** Their `expect` is not checked, and if one fails the run stops
  with an error; the model is never asked to replace an action the author pinned.
- **The seed command runs before every browser stage** (setup, rehearsal, each take and each
  repair), because rehearsal changes the app's state.
- **Not built yet:** the recorder plugin registry (there is one recorder), the MCP server mode,
  and the Playwright `recordVideo` fallback recorder.

**M2 (story)**
- **Keyframes are scaled, not full resolution.** They are capped at 1920 px on the long edge
  (`keyframe_max_edge`, up to the vision model's 2576 px maximum) to keep image cost in check.
- **"Scene change" frames are narrower than planned.** A frame is added only for a state that
  came and went between two keyframes and held for at least 0.2 s. The change detector's
  threshold sits above hover highlights, so a very small or faint toast can be missed.
- **Vision runs once per batch of steps** (up to 20 images a call), each batch opening with
  the screen as its first step found it, instead of one call per step.
- **Narration is per step only.** There is no separate hook or outro and no emphasis markup;
  the call to action goes into the final passage. Standalone passages need footage the
  timeline planner (M4) will create.
- **Word limits.** The writer aims for `seconds x target_wps` words and may not exceed 1.44
  times that (the 1.6 overrun the editor can cover, less 10% for pace variation). A reply
  that stays slightly long after three attempts is accepted and left to the post-voicing loop.
- **Finding: action-speed takes leave little room to talk.** On the TodoMVC example the four
  steps ran 4.2, 2.4, 1.7 and 1.6 seconds, so passages came out at 11, 6, 5 and 5 words.
  `dwell` is the author's lever today. Raising the default rest between steps, or letting
  narration length drive held frames more freely, is a decision for M4.

**M3 (voice)**
- **Checking and re-voicing happen per clip, inside `synthesize_step`.** The plan put
  regeneration in `qa_audio`; doing it where the clip is made keeps retries local to one
  passage and cached with it. `qa_audio` now only gathers word timings and warnings.
- **A clip that never passes does not stop the run.** Speech-to-text mishears product names,
  so a failed check is a warning in `report.json`, not an error.
- **The check is word error rate plus a cut-off test.** A clip also fails if the script's
  first or last word was not heard, whatever its error rate, because on a five-word passage
  one lost word is still under most thresholds.
- **Captions use the script's words, not the transcript's.** Script words are paired with
  heard words by edit distance and take their times; unheard words share the gap they sit in.
  This is lighter than WhisperX forced alignment and needs no second model.
- **Silence is trimmed by level, not by word timestamps**, which are not sample-accurate.
- **No `jiwer` dependency:** the alignment that times the words also yields the error rate.
- **Whisper is fed decoded samples through ffmpeg**, because faster-whisper's own decoder
  failed against the installed PyAV.
- **`target_wps` default raised from 2.4 to 3.0** after measuring Kokoro's default voice at
  3.1 to 3.9 words a second on real passages. On the TodoMVC example this turned passages of
  11, 6, 5 and 5 words into 13, 8, 5 and 6, and the voiced clips ran 0.82 to 1.20 times their
  steps' time on screen.
- **Speech engines are an optional install** (`demogen[voice]`), since PyTorch is large and
  other engines can be plugged in instead.
- **Only Kokoro and faster-whisper are built.** Chatterbox, OpenAI-compatible and ElevenLabs
  adapters are not.
- **Fixed in passing (M1 bug):** a video frame arriving after an aborted recording was written
  to the stopped encoder, and the error surfaced inside the browser's event handling, breaking
  the next browser call. The recorder now drops late frames and reports a write failure when
  it is stopped. This affected takes that fail and are repaired on pages that keep repainting.

**M4 (timeline and editor)**
- **The compositor uses ffmpeg pipes and Pillow, not PyAV.** ffmpeg decodes the take to raw
  frames and encodes the result; Pillow crops, scales and draws each frame in between. That
  avoids a second media library (PyAV was already clashing with faster-whisper).
- **Captions are drawn by the compositor, not by libass.** The Homebrew ffmpeg build has
  neither the subtitle nor the text filter, so depending on them would not be portable.
  Captions use Pillow's built-in sans unless `render.font` names a font file.
- **Idle footage is never trimmed or sped up.** The plan allowed for cutting long static
  waits; the edit list can express cuts, but the planner does not make them yet.
- **A clip either fits, is sped up by at most 10%, or gets a held frame.** Narration is never
  slowed down, and a held frame is always the settled end of the step.
- **Zoom is per step and conservative.** It frames the elements the step acts on, goes in
  just before the first action and comes out when the screen settles. It is skipped when the
  targets are too far apart or the step is too brief. The playbook's `focus` hint is not used
  yet: nothing resolves it to an element.
- **Loudness is normalised in one pass** (`loudnorm` to -16 LUFS) while encoding.
- **The pointer keeps a constant size** through zooms.
- **Final check.** Besides length and word error rate on the final mix, drift is the median
  difference between when a passage's words are heard and when they were planned. On the
  TodoMVC example it was under 20 ms for every passage.
- **The writer is also held to speakable characters**, after a stray `]` in a reply reached
  a caption on the first full run.
- **Fixed in passing (M3 bug):** Whisper sometimes heard a clip's last word twice ("Enter.
  Enter.") because trimmed clips end right after the speech. About one clip in four was
  affected on one passage; each still passed its check, with a word error rate of 0.11
  instead of 0. The Whisper adapter now appends 0.6 s of silence before transcribing.
- **`report.json` no longer lists stub stages**: there are none left.
- **Open, by design:** how freely narration may stretch footage. Kept as planned (hold up to
  1.6 times the step, then rewrite) pending a look at more real videos.

**M5 (service), in progress**
- **The queue is Procrastinate**, chosen over Hatchet and arq when M5 began: it keeps jobs in
  the Postgres the service already needs, arq is now maintenance-only, and Hatchet is a
  further service to run. Its lack of a dashboard is the cost.
- **A stage's result does not travel through the queue.** The worker writes it to the run's
  stage cache, which the control side reads once the job has succeeded. A failure's reason
  travels the same way.
- **The runtime is split into `prepare_*` and `advance`.** The API checks a request and stores
  what it brings; a `control` worker runs the pipeline forward. The command line does both in
  one process, through the same code.
- **Job status is read with plain SQL.** Procrastinate's blocking status call deadlocks when
  used inside one of its own workers, which is exactly where a run's driver waits on stages.
- **Steps for one run are serialised** by a queue lock per run, and the API refuses changes
  to a run that is busy.
- **The reaper is a periodic job** on the control pool: it requeues jobs whose worker stopped
  sending heartbeats, prunes old jobs, and expires old checkpoints. Verified by killing a
  worker process mid-stage.
- **`final_qa` runs on the `gpu` pool**, since it listens to the result with the speech model;
  only gpu workers need the speech engines installed.
- **Dropped: the Xvfb recorder.** The headless recorder already captures 60 frames a second
  at 2x and needs nothing a container lacks.
- **Verified** against Postgres 17 and MinIO installed locally, including a real run over
  HTTP with three worker processes and real models (about a minute and a half, end to end).
- **Not verified:** the Docker files, AWS S3 itself, more than one machine, and load.
- **Not built:** per-user access, a dashboard.

**Tracing (OpenTelemetry)**
- **Chosen over Langfuse**, to stay vendor-neutral. Spans go through one small module, `telemetry.py`;
  only the API package is a core dependency, and the SDK and exporter are an optional extra.
- **Turned on by the standard environment variables only.** No demogen-specific switch.
- **One trace per advance of a run, not per run.** A run pauses for a person, sometimes for
  days; a trace that long is no use. Every span carries the run's id to tie them together.
- **Trace context rides in the job's arguments**, so a trace continues from the API, through
  the control worker, into the pool that runs each stage. It also survives LangGraph's own
  worker threads, which copy the context.
- **Model spans follow the GenAI conventions** and record token counts but never content.
- **Speech and model calls are traced by wrapping the provider** where it is looked up, so
  every adapter is covered without touching any.
- **FastAPI emits its own request spans** once tracing is on; nothing was added for that.
- **Verified** with a stand-in collector receiving real OTLP exports: a command-line run
  (34 spans, one trace) and a service run (55 spans in one trace, across the API and three
  worker processes).
- **Found through the first traces, and fixed:** the Anthropic adapter reported only uncached
  input tokens (2 to 4 a call, where the true figure was thousands); and it asked for prompt
  caching on one-off requests, which pay the cache-write premium and are never read back.
  Caching is now requested only for tool loops.
- **Not built:** metrics, log correlation, and any sampling policy (everything is recorded).

**Drafting a playbook from a prompt (added after M5)**
- **Two new graph nodes in front of `load_validate`**: `draft_playbook`, and an approval pause
  that also accepts feedback in words and loops back to redraft. A run given a playbook file
  skips both.
- **The drafter works from the prompt alone**, by choice: it has not seen the site.
  Steps are intent-level, so this is workable; a step the site cannot do surfaces at rehearsal.
- **The model fills a smaller schema than the playbook's own** (no pinned actions, data
  blocks or setup), which is converted and then checked against the real playbook rules; a
  draft that breaks them goes back to the model with the reasons.
- **Feedback in words is taken only for the drafted playbook**, by choice. The plan
  and story pauses still take an edited file.
- **It uses the `planner` model role**; no new role to configure.
- **An author's edited playbook is stored as they wrote it**, comments included.
- **Verified with the real model:** a draft and a feedback-driven redraft for a shopping
  prompt, and a TodoMVC prompt carried through to a finished 21-second video.

**Real-site runs (2026-10-13): what broke, and what was done**
- **Sites that guard against automated browsing are not workable targets.** A large retail
  site began serving a bot check in place of its home page after a few automated sessions,
  and such sites' terms usually forbid automated access anyway. The tool does not click
  through such checks and must not grow ways around them. Use sites built for automation
  practice, your own app, or the bundled test shops.
- **Fixed: typed text in the take could differ from rehearsal.** Rehearsal replaced a field's
  value; the take cleared the field and then typed. On the Toolshop the quantity field
  refills itself with 1 when emptied, so typing 2 gave 12, the cart total was wrong, and the
  step's expected text never appeared. The take now selects the existing text and types over it.
- **Fixed: a missed expectation said only "Timeout".** It now names the text that did not
  appear and says that rehearsal had shown it.
- **Fixed: stray actions in rehearsed plans.** On the Toolshop the model added three hovers and
  a direct jump to the cart page, all recorded and due to be replayed. The cause: the
  rehearsal agent had no way to look at the page again except by acting, so it hovered to
  refresh its view. It now has a `wait` tool that records nothing; hovers that change nothing
  on the page and gotos to the address already open are left out of the plan, and the model
  is told so. Two fresh Toolshop rehearsals afterwards each recorded only the step's own
  actions, and the plan replayed to a finished video with no manual edit.
- **Open: weak evidence.** One of those rehearsals chose the word "cart" as proof that the
  item was added, though it is on the page before the click too. Evidence that was already
  visible when the step began could be refused.
- **Open: repair attempts are wasted on failures re-rehearsal cannot fix.** The typing bug
  cost two repairs and three takes before the run gave up.
- **Fixed since: locators anchored on per-load random ids** (see below).
- **Fixed since: nothing was saved when a step failed.** Runs keep the error that stopped them
  in `status.json`, and every failure is kept with a screenshot (see below).
- **Fixed since: a playbook that fails rehearsal** now comes back for review with the reason
  (see the local app notes below); a drafted one can take feedback.
- **Open: narration starts when its step starts**, so a passage can describe a result a
  second or two before the click that produces it.
- **Measured cost** at Opus 5.5 list prices: about $0.65 for the Toolshop run (including the
  failed take's two repairs; rehearsal was $0.47 of it) and about $0.46 for the sample store
  run (including one repeated rehearsal).
- **Added: `examples/shop`**, a self-contained sample store with search, colour and size
  options and a cart, and `examples/playbooks/shop.yaml`.

**The local app (2026-10-15)**
- **What it is.** `demogen ui` serves a small web app on 127.0.0.1:8765 for people who should
  never have to open a JSON file: start a demo from words or an uploaded playbook, review the
  playbook, the plan and the story, watch progress, and get the video. It is a web app served
  by demogen itself (no Node.js build), with rich editing at each review, one run at a time
  with a queue, and a failed rehearsal returning to the playbook review.
- **One API, two backends.** `service/api.py` now takes a `Backend` (runtime, run list,
  submit, busy, describe, delete, reveal). The service's Procrastinate-backed `Service` is one;
  `ui/local.py`'s `LocalBackend` is the other: `Runtime.local(runs/)`, a queue and one worker
  thread. The run list is read from the run folders, each with a `status.json` (name, start,
  finish, last error), so command-line runs appear in the app and a restart loses nothing; a
  run that was under way when the app stopped shows as interrupted, with Resume. New
  endpoints serve both: the review screens' data (`GET /runs/{id}/review`: steps, actions in
  plain words, passages with their word budgets and keyframes), a playbook check, delete,
  reveal in the file manager (local only), a poster frame, and a health check naming what to
  fix. `POST /runs` takes `options` (JSON) laid over the default settings.
- **No token locally.** The local server binds to 127.0.0.1 and refuses any request whose
  `Host` is not localhost, which also defeats DNS rebinding from a web page. The service still
  requires its token.
- **A failed rehearsal is a review, not a crash.** When the model reports a step cannot be
  done, `rehearse` returns the failure instead of raising; the graph routes it to
  `approve_playbook`, which pauses even with `auto_approve`, carrying the step and reason. A
  `rehearsal_attempt` counter keeps an unchanged re-approval from being answered from the
  cache. Feedback in words is refused for an uploaded playbook (there is no draft to redo).
  Browser and model errors still stop the run.
- **Validation errors in plain words.** Pydantic's messages are turned into one line per
  problem, naming the step ("step 2 › do: is missing").
- **Tests.** API tests over the local backend with stand-in stages, and a Playwright test that
  takes a demo from description to video in the app with an edit at every review and reloads
  mid-run.

**Locators and fail points (2026-10-15)**
- **What broke.** A run on a real retail site stopped at the take. Rehearsal had clicked a
  search result's picture, a link with no accessible name, so its only locator was a CSS path
  anchored on `#86625a76-…`, an id the site generates on every load; the take could not find it.
  The repair then opened a fresh browser and could not find the search box on the first step,
  by role or by its stable id, minutes after both had worked: most likely a bot check served
  to a fresh automated session. Nothing recorded the page, and the error named the repair's
  failure only, under the broken step's name.
- **Locators.** The CSS path builder passes over ids and test ids that look generated (UUIDs,
  React's useId, runs of four or more digits, long letter-and-digit hashes). In rehearsal an
  element with no accessible name is questioned once: the model is asked for a named element
  that does the same (a product's title link rather than its picture), and may repeat the
  action to use the unnamed one anyway. The `rehearse` stage is at version 2 for this.
- **Fail points.** These runs are meant to seed training data for computer-use agents, with
  the failures usable in RL. So every failure is a record in `failures/` with its
  context, not a log line: the task, the step and what earlier steps did, the action and the
  model's tool call as made, the snapshot the model chose from, a screenshot and snapshot of
  the page at the failure, the take or repair attempt, and the step's outcome (`resolved`
  when the agent recovered, `unresolved`, or `failed` when the run stopped there). Recorded:
  the model's mistakes in rehearsal, repair and setup (via an `on_error` hook in the tool loop,
  with a `kind` on `ToolError` and `ActionFailed`), actions that changed nothing, giving up,
  running out of turns, every broken take step, and every failed replay before a repair.
  `demogen failures` exports them as JSON Lines; the API serves them at
  `GET /runs/{id}/failures`; the app shows the last one's screenshot on a stopped run. Each
  is also an OpenTelemetry span event. Successful steps are not recorded as trajectories yet:
  a record carries what came before it only as the earlier steps' instructions.
- **Errors say both things.** A failed repair now reports the take's own failure and then
  the repair's, and, when a step that had worked cannot be replayed, says the site may be
  showing this browser a different page.

**Larkspur Market, the test shop (2026-10-15)**
- **Why.** The sample store (`examples/shop`) is one static page with hash routes and no server
  state. Demos and computer-use training data need a fuller store: a server-backed shop with
  sign-in and account, a test-card checkout, and browsing with filters, sorting and pages,
  started by `demogen shop`. Deliberately tricky UI
  (banners, modals, delayed elements) is left out for now.
- **How.** `src/demogen/testshop/`: FastAPI with Jinja2 pages rendered on the server and plain
  HTML forms, so every step is a real navigation with a real address; a small script only
  swaps the product picture with the colour and hides unused address fields. A fixed catalog
  of 29 products, pictures drawn as SVG, no network. Labels throughout, a single named link
  per product card, sold-out sizes named "XS, sold out", and no two links with the same name
  on a page (the "Home" department became "Home & Kitchen").
- **Same shop for every browser.** Cart, checkout and the orders placed belong to the
  session, so rehearsal and the take, each in a fresh browser, see identical pages: an empty
  cart and order 1001. A playbook that signs in during setup reuses that browser's cookies
  across stages, so `POST /__reset` returns every session to a fresh start, keeping sign-ins;
  it is the playbook's `setup.seed`.
- **Payments.** Only the published test cards are accepted (4242… succeeds, 4000…0002 is
  declined at "Place order"); any other number, valid or not, is refused.
- **Dependencies.** FastAPI, uvicorn, python-multipart and Jinja2 are now core dependencies;
  they were only in the `service` extra, so `demogen ui` failed on a plain install.
- **Tests.** The flows through the server, a guest purchase in a real browser, and a demogen
  take of the whole purchase with pinned actions.

## Sources
[aidemo](https://github.com/tandryukha/aidemo) · [ai-demo-generator-oss](https://github.com/grodriguez1983/ai-demo-generator-oss) · [Playwright 1.59 release](https://github.com/microsoft/playwright/releases/tag/v1.59.0) · [Playwright recordVideo bitrate issue #31424](https://github.com/microsoft/playwright/issues/31424) · [Puppeteer ScreenRecorder](https://app.unpkg.com/puppeteer-core@25.4.0/files/src/node/ScreenRecorder.ts) · [Playwright MCP vs CLI](https://morphllm.com/playwright-mcp) · [Stagehand/browser-use comparison](https://www.pkgpulse.com/guides/stagehand-vs-playwright-ai-vs-browser-use-ai-web-2026) · [Xvfb + ffmpeg recording](https://kroket.io/blog/recording-headless-firefox.html) · [LangGraph HITL](https://docs.langchain.com/oss/python/langgraph/human-in-the-loop) · [LangGraph vs Temporal](https://temporal.io/blog/temporal-langgraph-plugin-durable-execution) · [LiteLLM vs LangChain](https://toolhalla.ai/compare/langchain-vs-litellm) · [Open TTS 2026](https://pinggy.io/blog/best_open_source_self_hosted_text_to_speech_models/) · [Open-weight TTS licenses](https://builderai.tools/blog/open-weight-text-to-speech-models-2026) · [WhisperX](https://arxiv.org/abs/2303.00747v1) · [Parakeet vs Whisper](https://openwhispr.com/blog/parakeet-vs-whisper-vs-nemotron) · [Frame sampling for VLMs](https://arxiv.org/pdf/2509.14769) · [Remotion vs ffmpeg](https://thegtmdirectory.com/compare/ffmpeg-vs-remotion) · [rrweb limits (Browserbase)](https://browserbase.com/blog/session-recordings) · [Durable engines compared (Upstash)](https://upstash.com/blog/durable-workflow-engines-compared-every-major-option-in-2026) · [Restate/DBOS/Hatchet beyond Temporal](https://cloudrps.com/blog/durable-execution-restate-dbos-hatchet-beyond-temporal/) · [DBOS workflows](https://docs.dbos.dev/python/tutorials/workflow-tutorial) · [DBOS pricing](https://www.dbos.dev/dbos-pricing) · [Temporal Cloud pricing](https://docs.temporal.io/cloud/pricing) · [Temporal self-host cost](https://automationatlas.io/guides/temporal-cloud-vs-self-hosted-2026/) · [Hatchet v1](https://hn.svelte.dev/item/43572733) · [Prefect pause/resume](https://docs.prefect.io/v3/develop/pause-resume) · [Burr persistence](https://burr.apache.org/concepts/state-persistence/) · [pydantic-graph persistence](https://ai.pydantic.dev/api/pydantic_graph/persistence) · [LlamaIndex durable workflows](https://developers.llamaindex.ai/python/workflows/durable_workflows/) · [LangGraph checkpointing postmortem](https://pub.towardsai.net/langgraph-checkpointing-is-not-free-a-production-postmortem-398bc86861f4) · [LangGraph Platform pricing](https://www.langchain.com/langgraph-platform-pricing)
