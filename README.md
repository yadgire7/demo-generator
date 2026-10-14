# demogen

Turn a YAML playbook and an author's brief into a narrated product-demo video.

The design, the research behind it and the milestones are in [docs/design.md](docs/design.md).

## Status: M5 (service), in progress

From the command line, a run goes all the way: it rehearses the playbook in a real browser,
records a clean take, looks at what the take shows, writes and voices narration sized to each
step, and renders `final.mp4` with a pointer, click ripples, zoom, captions and the narration
mixed in. You approve the plan before recording and the story before it is voiced. A run can also
start from a prompt, with the playbook drafted for you; see [Starting from a prompt](#starting-from-a-prompt).

The same pipeline also runs as a service: an HTTP API, a Postgres-backed job queue and worker
pools, with artifacts in S3-compatible storage. See [Running as a service](#running-as-a-service).
Runs can be traced with OpenTelemetry; see [Tracing](#tracing). Not built yet: metrics, and
a tested container setup (the files in `deploy/` are untested).

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
browsing, Amazon among them, are not workable targets.

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

## Layout

- `src/demogen/stages/`: the pipeline's stages, as plain functions over artifact references.
- `src/demogen/exec/`: where stages run (in this process, or queued to a worker pool).
- `src/demogen/service/`: the HTTP API, the worker tasks and the list of runs.
- `src/demogen/telemetry.py`: tracing; a no-op until the environment turns it on.
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
