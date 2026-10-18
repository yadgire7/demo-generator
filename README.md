# demogen

[![GitHub stars](https://img.shields.io/github/stars/yadgire7/demo-generator?style=social)](https://github.com/yadgire7/demo-generator/stargazers)

[![Tests](https://github.com/yadgire7/demo-generator/actions/workflows/tests.yml/badge.svg)](https://github.com/yadgire7/demo-generator/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Describe a demo in a sentence, or write a playbook, and get a narrated product-demo video: an
LLM rehearses the steps in a real browser, the clicks are replayed on camera, the footage is
narrated to fit, voiced, and edited with zoom, pointer and captions. You review the playbook,
the clicks and the narration before each is used.

![A demo demogen made of Larkspur Market, its built-in test shop](docs/media/larkspur-demo.gif)

*Made from one sentence: "Search for a navy t-shirt, open the Crewneck T-Shirt, choose size L,
add it to the cart, and check out as a guest with the test card." Shown here at double speed
and silent; the real video is 47 seconds, narrated and captioned.*

The design, the research behind it and the milestones are in [docs/design.md](docs/design.md).

## Quickstart

You need Python 3.11+, `ffmpeg` and an Anthropic API key (or any model LiteLLM supports).

```bash
git clone https://github.com/yadgire7/demo-generator && cd demo-generator
python3 -m venv .venv && .venv/bin/pip install -e ".[voice]"
.venv/bin/playwright install chromium
cp demogen.example.yaml demogen.yaml
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
```

Then start the test shop and the app, in two terminals:

```bash
.venv/bin/demogen shop     # Larkspur Market, at http://127.0.0.1:8400
```

```bash
.venv/bin/demogen ui       # opens the app in your browser
```

In the app, choose **Describe a demo**, set **Starts at** to `http://127.0.0.1:8400`, and
describe what to show. A video like the one above costs about $0.50 in model calls with
Claude Opus.

## System design

Three ways in share one control plane. Each change to a run is checked and stored first, then a
LangGraph graph advances the run until it pauses for a review or finishes, checkpointing as it
goes so any run can be resumed, rerun from a stage, or picked up by another worker.

```mermaid
flowchart TB
  subgraph entry["Ways in"]
    direction LR
    CLI["demogen CLI<br/>run · approve · resume · rerun"]
    APP["demogen ui<br/>local web app"]
    API["HTTP API<br/>(service, token)"]
  end

  subgraph control["Control plane"]
    direction LR
    RT["Runtime<br/>prepare, then advance"]
    G["LangGraph graph<br/>stages, review pauses, loops"]
    CP[("Checkpoints<br/>SQLite or Postgres")]
  end

  subgraph exec["Execution"]
    direction LR
    LX["Local executor<br/>(in process)"]
    QX["Queue executor<br/>(Procrastinate)"]
    WK["Worker pools<br/>browser · cpu · gpu"]
  end

  subgraph stages["Stages: plain functions over artifact refs"]
    ST["draft · rehearse · record · analyze<br/>write · voice · plan · render · QA"]
  end

  subgraph store["Storage"]
    direction LR
    AS[("Artifacts<br/>run folder or S3")]
    SC[("Stage cache<br/>content-addressed")]
    FL[("Fail points<br/>failures/ + screenshots")]
  end

  subgraph ext["Models and tools"]
    direction LR
    LLM["LLMs<br/>Anthropic · LiteLLM"]
    PW["Playwright Chromium<br/>+ screen recorder"]
    TTS["Kokoro TTS"]
    STT["faster-whisper STT"]
    FF["ffmpeg compositor"]
  end

  WEB(["Target web app<br/>e.g. Larkspur Market"])
  OT["OpenTelemetry traces"]

  CLI --> RT
  APP --> RT
  API --> RT
  RT --> G
  G <--> CP
  G --> LX
  G --> QX
  QX --> WK
  LX --> ST
  WK --> ST
  ST <--> AS
  ST <--> SC
  ST --> FL
  ST --> LLM
  ST --> PW
  PW --> WEB
  ST --> TTS
  ST --> STT
  ST --> FF
  G -.-> OT
  ST -.-> OT
```

The pipeline a run goes through. The hexagons are the three reviews, where the run waits for
you (or goes straight through with auto-approve):

```mermaid
flowchart TD
  PR["Prompt"] --> DR["draft_playbook<br/>(LLM)"]
  DR --> RP{{"Review: playbook"}}
  RP -- "changes in words" --> DR
  PB["Playbook YAML"] --> LV
  RP -- "approve or edit" --> LV["load_validate"]
  LV --> SU["setup<br/>sign in, unrecorded"]
  SU --> RH["rehearse<br/>LLM drives the browser,<br/>its actions become the plan"]
  RH -- "a step cannot be done" --> RP
  RH --> RC{{"Review: clicks"}}
  RC --> TK["record_take<br/>replay the plan on camera"]
  TK -- "a step breaks" --> HL["repair that step<br/>(LLM), take again"]
  HL --> TK
  TK --> VT["verify_take"]
  VT --> KF["extract_keyframes"]
  KF --> AF["analyze_frames<br/>(vision LLM)"]
  AF --> WS["write_story<br/>(LLM, sized to each step)"]
  WS --> RS{{"Review: narration"}}
  RS --> SY["synthesize_step × steps<br/>(Kokoro, in parallel)"]
  SY --> QA["qa_audio<br/>(faster-whisper listens back)"]
  QA --> TL["plan_timeline<br/>fit voice to footage"]
  TL -- "too long for its footage" --> WS
  TL --> RN["render<br/>zoom, pointer, ripples, captions, mix"]
  RN --> FQ["final_qa"]
  FQ --> OUT[["final.mp4 · captions · report"]]
  RH -.-> FP[("fail points")]
  TK -.-> FP
  HL -.-> FP
```

## What it does

From the app or the command line, a run goes all the way: it rehearses the playbook in a real browser,
records a clean take, looks at what the take shows, writes and voices narration sized to each
step, and renders `final.mp4` with a pointer, click ripples, zoom, captions and the narration
mixed in. You approve the plan before recording and the story before it is voiced. A run can also
start from a prompt, with the playbook drafted for you; see [Starting from a prompt](#starting-from-a-prompt).
Every failure on the way is kept with a screenshot, as data for training computer-use agents;
see [Fail points](#fail-points).

For most people the easiest way in is the app: `demogen ui` opens it in your browser. See
[The app](#the-app).

The same pipeline also runs as a service: an HTTP API, a Postgres-backed job queue and worker
pools, with artifacts in S3-compatible storage. See [Running as a service](#running-as-a-service).
Runs can be traced with OpenTelemetry; see [Tracing](#tracing).

How recording works:

- **Rehearsal.** A model reads the page's accessibility snapshot and performs each step. What
  it does is saved to `actions.lock.json` as durable locators (role and name, then a CSS path).
- **The take.** The plan is replayed with no model in the loop: the pointer glides, text is
  typed at a human pace, and each step's expected text is checked. If a step breaks, the model
  repairs that step and the take starts over.
- **Pinned steps.** A step with an `action:` list in the playbook skips the model entirely.
  A playbook whose every step is pinned needs no model at all.
- **Signing in.** `setup.steps` run before rehearsal and are never recorded. Credentials are
  written as `${secret:NAME}`, read from the environment, and hidden from the model and from
  every artifact. The saved browser state (`auth/` in the run folder) does hold session
  cookies, so treat run folders as private.

How the story is written:

- **Keyframes.** For each step, the settled screen after every action is saved from the take
  (`keyframes/`), plus anything that appeared and vanished in between, such as a toast.
- **Facts.** A vision model reports what each step's frames show (`facts.json`), so the
  narration describes the real screen and not what the playbook hoped for.
- **Narration.** A writer model gets your brief, the facts and a word budget per step, worked
  out from how long that step is on screen (`story.json`). A passage that still runs long
  once voiced goes back to be shortened; the rest are left word for word.
- **Short steps mean short lines.** A step that takes two seconds on screen carries about
  five words. Give a step more room with `dwell:` in the playbook.
- **What leaves your machine.** Keyframes are sent to the vision model as images. A secret
  the app displays on screen is removed from the model's written report, but it is in the
  image the model was sent.

How the narration is voiced:

- **One clip per step** (`audio/<step>.wav`), spoken by Kokoro, a small open model that runs
  on a CPU, with the silence at each end trimmed off.
- **Each clip is heard back.** Whisper transcribes it and the result is compared with the
  script. A clip that is cut off or misread is voiced again; if no attempt passes, the closest
  is kept and listed under `audio_warnings` in `report.json` for you to listen to.
- **Word timings** for captions are saved in `words.json`: the script's own words and
  spelling, at the times they were heard.
- **Pace.** `target_wps` is how fast the voice reads; the writer sizes passages with it. The
  default (3.0) suits Kokoro's default voice. Change it if you change the voice or its speed.

How the video is put together:

- **Timeline.** Each clip plays from the start of its step. A clip that fits leaves the take
  untouched; one up to 10% long is sped up to fit; a longer one holds the step's last frame
  until it finishes. `edl.json` records every decision.
- **Pointer.** The take records no pointer. It is drawn afterwards from the logged path, with
  a halo, and a ring spreads from each click.
- **Zoom.** The view moves in on the element a step acts on and back out once the screen has
  settled, so the result is seen in full. `render.zoom: 1` turns it off.
- **Captions** are burned in from the narration's own words and also written to `final.srt`.
- **Checks.** The finished file is measured against its plan: its length, whether the
  narration heard in the final mix matches the story, and whether each passage plays when
  planned. Anything off is listed under `final_check.problems` in `report.json`.

## The app

```bash
.venv/bin/demogen ui
```

This starts demogen on `http://127.0.0.1:8765` and opens it in your browser. From there you
can describe a demo in words or upload a playbook, then check each part before it is made:

- **The playbook**, as one card per step: what to do, what should be visible afterwards, the
  point to make, and how long to stay on screen. Steps can be reordered, added and removed, or
  the whole file edited as text. A drafted playbook can also be sent back with changes in words.
- **The clicks** rehearsal worked out, in plain words ("Type “t-shirt” into the "Search" box"),
  any of which can be taken out before they are recorded.
- **The narration**, one line per step beside the frame it is spoken over, with a live word
  count against the room the footage leaves.

When it is done the video plays in the page, and it is saved to `runs/<run-id>/final.mp4` as
with the command line. Runs started from the command line show up in the app too, since both
use the same `runs/` folder; closing the app loses nothing, and a run that was under way can be
resumed. One demo is made at a time; others wait their turn.

If rehearsal cannot do a step, the run comes back to the playbook with the step marked and the
reason, so you can change it and rehearse again.

The app only answers on this machine: it listens on 127.0.0.1 and refuses requests addressed
to any other host name. `--port`, `--config`, `--runs-dir` and `--no-open` change the defaults.
Try it first with **Try it on the sample store**, which needs no setup beyond the model key.

## Fail points

Every time something goes wrong on the way to a video, demogen keeps it in the run's
`failures/` folder: a JSON record, a screenshot of the page at that moment, its accessibility
snapshot and, for the model's own mistakes, the snapshot the model was looking at when it
acted. That covers the model's mistakes in rehearsal, repair and sign-in (an element no
longer on the page, evidence that is not there, an action that changed nothing, giving up),
and every take or replay that broke. Each record says how its step ended: whether the agent
recovered from the mistake, gave up, or stopped the run there.

They explain a stopped run (the app shows what the browser was showing, and `demogen status`
names the screenshot), and they are meant to be gathered as training data for computer-use
agents:

```bash
.venv/bin/demogen failures --out failures.jsonl        # every run; or name runs to export
```

One JSON object per line (schema `demogen.failure/1`, described in
[src/demogen/failures.py](src/demogen/failures.py)); its `files` are relative to its run's
folder. Snapshots and messages are passed through the playbook's secret redactor, so typed
credentials appear only as `${secret:NAME}`; a screenshot shows the screen as it was.

## Starting from a prompt

You do not have to write the playbook yourself. Describe the demo and one is drafted for you:

```bash
.venv/bin/demogen run --prompt "Show how to add two todos, complete one and filter to what is left" --url https://demo.playwright.dev/todomvc
```

The run pauses on the draft (`playbook.yaml` in the run folder). From there you can:

```bash
.venv/bin/demogen approve <run-id>                                  # use it as it is
.venv/bin/demogen approve <run-id> --feedback "Drop the last step"  # have it redrafted, as often as you like
.venv/bin/demogen approve <run-id> --edit my-playbook.yaml          # replace it with your own file
```

Good to know:

- **The drafter has not seen the site.** It writes steps by intent ("Search for…", "Choose the
  colour Navy") from your prompt alone. Whether the site can do them is found out at
  rehearsal, which stops with the reason if a step cannot be done.
- **`--url` is optional** when the prompt names a well-known site; check `base_url` in the
  draft either way.
- **Sign-in is never drafted.** Add a `setup` block by editing the playbook if the demo needs one.
- **Feedback in words works only at this pause.** The plan and the story are changed by
  sending an edited file.

## Larkspur Market: a test shop

`demogen shop` runs Larkspur Market on this machine, at `http://127.0.0.1:8400`: a small
online store with real page addresses, built to make demos of and to test demogen against.

- **Browsing:** departments, search, filters (colour, size, price, in stock), sorting and
  pages; product pages with colours, sizes (some sold out), quantity, stock and reviews.
- **Cart:** add, change quantities, remove; free delivery from $50.
- **Sign-in and account:** a test account (`demo@larkspur.test`, password `larkspur-demo`)
  with an order history and saved addresses.
- **Checkout:** delivery, payment and review, with validation errors. Only published test
  cards are taken: `4242 4242 4242 4242` succeeds, `4000 0000 0000 0002` is declined. No
  payment is ever taken.

Every control has a proper label, so demogen finds elements by name. Carts and orders belong
to the browser's session, so each fresh browser (which is what demogen opens for rehearsal and
for the take) meets the same shop: an empty cart, and a first order numbered 1001.
`POST /__reset` puts every session back to that, still signed in; use it as a playbook's
`setup.seed` when the playbook signs in during setup, since that browser's cookies carry over.

```bash
.venv/bin/demogen shop
```

```bash
.venv/bin/demogen run --url http://127.0.0.1:8400 --prompt "Search for a navy t-shirt, open the Crewneck T-Shirt, choose size L, add it to the cart, and check out as a guest with the test card"
```

Or start from [examples/playbooks/larkspur.yaml](examples/playbooks/larkspur.yaml), or give the
same prompt and address to the app (`demogen ui`).

## A sample store to try it on

`examples/shop` is a small self-contained store, Harbour Goods, with search, colour and size
options and a cart. It needs no network and never changes between rehearsal and recording,
so it is a dependable place to try a shopping demo:

```bash
python3 -m http.server 8377 --directory examples/shop
```

```bash
.venv/bin/demogen run examples/playbooks/shop.yaml
```

Or have the playbook drafted: `demogen run --url http://127.0.0.1:8377 --prompt "Show how to
search for a t-shirt, choose Navy and size Large, add it to the cart and open the cart"`.

Public sites built for automation practice also work; the Toolshop at
practicesoftwaretesting.com has been run end to end. Sites that guard against automated
browsing are not workable targets, and demogen does not try to get past their checks.

## Known limitations

- **Web apps only.** Desktop apps are designed for but not built.
- **Sites that block automation do not work.** Bot checks stop rehearsal, and demogen does not
  try to get past them. Use your own app, staging, sites built for automation practice, or
  the bundled test shops.
- **Narration can run ahead of the picture** within a long step: each passage starts when its
  step starts, so a step with many actions may describe its result before it appears. Short,
  one-outcome steps avoid it.
- **Weak checks.** Rehearsal sometimes picks evidence that was on the page before the step.
- **Keep the install path short.** espeak-ng, which the default voice uses for pronunciation,
  cannot read its data from a path of 160 characters or more; demogen stops with an
  explanation if the virtual environment sits that deep.
- **Tested on macOS** with Chromium. Linux runs the test suite in CI; Windows is untried.
- **Untested:** the container files in `deploy/`, AWS S3 itself (MinIO is tested), and more
  than one worker machine. Not built: metrics, per-user access.

## Requirements

Python 3.11+, `ffmpeg` on the PATH, and Chromium for Playwright:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,voice]"
.venv/bin/playwright install chromium
cp demogen.example.yaml demogen.yaml
```

The `voice` extra installs the default speech engines (Kokoro and faster-whisper, with
PyTorch: about 1.5 GB). Their model weights download on first use.

API keys and demo secrets are read from the environment or from a `.env` file in the working
directory (git ignores it).

## Use

```bash
.venv/bin/demogen run examples/playbooks/todomvc.yaml --intent brief.md
.venv/bin/demogen approve <run-id>                 # approve the action plan
.venv/bin/demogen approve <run-id> --edit story.yaml   # approve the story, with edits
.venv/bin/demogen status <run-id>
.venv/bin/demogen resume <run-id>                  # after a crash or failed stage
.venv/bin/demogen rerun <run-id> --from write_story --intent new-brief.md
```

Runs live in `./runs/<run-id>/` (override with `--runs-dir` or `DEMOGEN_RUNS_DIR`).
Settings come from `./demogen.yaml`; see [demogen.example.yaml](demogen.example.yaml).

## Running as a service

Install the extra (`pip install -e ".[service,voice]"`) and point every process at the same
Postgres and bucket:

```bash
export DEMOGEN_DATABASE_URL=postgresql://user:pass@host:5432/demogen
export DEMOGEN_S3_BUCKET=demogen            # unset: artifacts stay in DEMOGEN_RUNS_DIR (one machine only)
export DEMOGEN_S3_ENDPOINT=http://host:9000 # for MinIO and the like; unset means AWS
export DEMOGEN_API_TOKEN=choose-a-long-random-string
export DEMOGEN_CONFIG=/path/to/demogen.yaml # settings for runs started without their own
```

Then, each in its own process (or container, or machine):

```bash
demogen migrate                                          # once: creates the tables
demogen worker --pool control --pool cpu --concurrency 8  # drives runs; cheap stages and rendering
demogen worker --pool browser --concurrency 1             # rehearsal and recording (needs Chromium)
demogen worker --pool gpu --concurrency 2                 # speech (needs the voice extra)
demogen serve --host 0.0.0.0 --port 8000
```

A run over HTTP, with `Authorization: Bearer $DEMOGEN_API_TOKEN` on every request:

| Request | What it does |
|---|---|
| `POST /runs` (form: `playbook` or `prompt`, optional `url`, `intent`, `config`, `run_id`) | Start a run from a playbook, or from a prompt to have one drafted |
| `POST /runs/{id}/feedback` (JSON: `feedback`) | Send a drafted playbook back to be revised |
| `GET /runs`, `GET /runs/{id}` | List runs; one run's phase, what it waits on, and its artifacts |
| `POST /runs/{id}/approve` (optional body: the edited playbook, plan or story) | Approve the playbook, plan or story |
| `POST /runs/{id}/resume` | Continue a run that failed |
| `POST /runs/{id}/rerun` (JSON: `from_stage`, optional `intent`) | Run again from a stage |
| `GET /runs/{id}/artifacts/{path}` | Download an artifact, such as `final.mp4` |

How it holds together:

- **Pools.** A `control` worker drives each run and hands every stage to the pool it belongs
  to. Stages are idempotent and their results are cached in the run's storage, so a job that
  runs twice does no harm.
- **Failures.** A stage that fails stops its run with the reason shown in `GET /runs/{id}`;
  `resume` picks up from there. A worker that dies mid-job is noticed within about a minute
  and its job is given to another.
- **One step at a time.** Requests that would change a run while it is busy are refused (409).
- **Housekeeping.** Checkpoints of runs finished more than `DEMOGEN_CHECKPOINT_TTL_DAYS`
  (default 30) ago are deleted. Artifacts are kept; such a run can no longer be resumed or rerun.
- **Access.** One shared token: anyone who has it can see and change every run. Saved
  sign-in sessions (`auth/`) are never served.

`deploy/Dockerfile` and `deploy/compose.yaml` describe this layout for Docker. They were
written without Docker to hand and have not been built or run.

## Tracing

Install the extra (`pip install -e ".[otel]"`) and set the standard OpenTelemetry variables
in every process, command line or service:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318   # OTLP over HTTP
# or, to see what is recorded without a collector:
export OTEL_TRACES_EXPORTER=console
```

With neither set, nothing is recorded and nothing is sent. What a trace holds:

- **One trace each time a run moves forward** (start, approve, resume, rerun), tagged with
  the run's id. In the service it begins at the API request and follows the run through the
  queue into each worker.
- **A span per stage**, with whether its result came from cache. In the service, a second
  span covers the wait for a worker.
- **A span per model call**: the model, the role it was used for, and tokens in and out,
  including how many were read from the prompt cache. Prompts and replies are never recorded.
- **A span per speech call**: characters voiced, words heard back.

Processes report as `demogen-cli`, `demogen-api` and `demogen-worker`; `OTEL_SERVICE_NAME`
overrides that. Every request is traced, so a client that polls run status often will
produce a steady stream of small API traces; sample at the collector if that matters.

## Star history

[![Star history chart](https://api.star-history.com/svg?repos=yadgire7/demo-generator&type=Date)](https://www.star-history.com/#yadgire7/demo-generator&Date)

## Layout

- `src/demogen/stages/`: the pipeline's stages, as plain functions over artifact references.
- `src/demogen/exec/`: where stages run (in this process, or queued to a worker pool).
- `src/demogen/service/`: the HTTP API, the worker tasks and the list of runs.
- `src/demogen/ui/`: the app (`demogen ui`): its web server, the one-machine backend, the
  review screens' data, and the page itself in `static/` (plain HTML, CSS and JavaScript).
- `src/demogen/telemetry.py`: tracing; a no-op until the environment turns it on.
- `src/demogen/failures.py`: the fail-point log, and reading it back for export.
- `src/demogen/testshop/`: Larkspur Market, the test shop (`demogen shop`).
- `src/demogen/graph/`: the LangGraph control plane. Nothing else imports LangGraph.
- `src/demogen/llm/`: the provider-neutral LLM interface, the tool loop, and the Anthropic and
  LiteLLM adapters.
- `src/demogen/driver/playwright/`: the browser session, the rehearsal agent and the take.
- `src/demogen/recorder/`: screen capture (Chrome screencast into ffmpeg).
- `src/demogen/story/`: keyframe selection, the vision report, word budgets and the writer.
- `src/demogen/voice/`: clip trimming, the heard-back check, and the speech engine plugins.
- `src/demogen/timeline/`: the planner that fits narration to footage, and the edit list.
- `src/demogen/editor/`: the compositor (zoom, pointer, ripples, captions) and audio mix.
- `src/demogen/store/`: artifact storage (local folders or S3) and the content-addressed stage cache.

## Test

```bash
.venv/bin/python -m pytest
```

The browser tests drive a small fixture app in `tests/fixtures/app` with a scripted model, so
the suite needs Chromium and ffmpeg but no API key. Tests that use the real speech engines
need the `voice` extra installed. The service tests start their own throwaway Postgres and
MinIO, and are skipped if those are not installed.
