# demogen

Turn a YAML playbook and an author's brief into a narrated product-demo video.

The design, the research behind it and the milestones are in [docs/design.md](docs/design.md).

## Status: M4 (finished videos)

A run now goes all the way: it rehearses the playbook in a real browser, records a clean
take, looks at what the take shows, writes and voices narration sized to each step, and
renders `final.mp4` with a pointer, click ripples, zoom, captions and the narration mixed in.
You approve the plan before recording and the story before it is voiced.

Still to come (M5): running as a service with worker pools, Postgres and object storage.

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

## Layout

- `src/demogen/stages/`: the pipeline's stages, as plain functions over artifact references.
- `src/demogen/exec/`: where stages run (in-process today; worker pools later).
- `src/demogen/graph/`: the LangGraph control plane. Nothing else imports LangGraph.
- `src/demogen/llm/`: the provider-neutral LLM interface, the tool loop, and the Anthropic and
  LiteLLM adapters.
- `src/demogen/driver/playwright/`: the browser session, the rehearsal agent and the take.
- `src/demogen/recorder/`: screen capture (Chrome screencast into ffmpeg).
- `src/demogen/story/`: keyframe selection, the vision report, word budgets and the writer.
- `src/demogen/voice/`: clip trimming, the heard-back check, and the speech engine plugins.
- `src/demogen/timeline/`: the planner that fits narration to footage, and the edit list.
- `src/demogen/editor/`: the compositor (zoom, pointer, ripples, captions) and audio mix.
- `src/demogen/store/`: artifact storage and the content-addressed stage cache.

## Test

```bash
.venv/bin/python -m pytest
```

The browser tests drive a small fixture app in `tests/fixtures/app` with a scripted model, so
the suite needs Chromium and ffmpeg but no API key. Tests that use the real speech engines
need the `voice` extra installed.
