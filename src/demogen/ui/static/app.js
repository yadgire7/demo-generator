// demogen's local app: start demos, review them at each pause, and watch them come together.
// Plain JavaScript with no build step. It talks only to the demogen server it was loaded from.

const POLL_MS = 1500;
const HOME_POLL_MS = 3000;

// The pipeline's parts, as the progress rail shows them, and the graph nodes each one covers.
const STAGES = [
  { label: 'Playbook', busy: 'Drafting', nodes: ['draft_playbook', 'approve_playbook', 'load_validate'] },
  { label: 'Rehearse', busy: 'Rehearsing', nodes: ['setup', 'rehearse', 'approve_plan'] },
  { label: 'Record', busy: 'Recording', nodes: ['record_take', 'verify_take', 'extract_keyframes'] },
  { label: 'Story', busy: 'Writing', nodes: ['analyze_frames', 'write_story', 'approve_story'] },
  { label: 'Voice', busy: 'Voicing', nodes: ['synthesize_step', 'qa_audio', 'plan_timeline'] },
  { label: 'Video', busy: 'Rendering', nodes: ['render', 'final_qa'] },
];

// What is happening while each node runs, in words.
const DOING = {
  draft_playbook: 'Drafting the playbook from your description',
  load_validate: 'Checking the playbook',
  setup: 'Signing in to the app',
  rehearse: 'Rehearsing in a browser: working out what to click',
  record_take: 'Recording the take',
  verify_take: 'Checking that the take did every step',
  extract_keyframes: 'Picking frames from the recording',
  analyze_frames: 'Looking at what the footage shows',
  write_story: 'Writing the narration',
  synthesize_step: 'Recording the voice-over',
  qa_audio: 'Listening back to the voice-over',
  plan_timeline: 'Fitting the narration to the picture',
  render: 'Putting the video together',
  final_qa: 'Checking the finished video',
};

const VOICES = [
  ['af_heart', 'Heart: American, warm'],
  ['af_bella', 'Bella: American, bright'],
  ['am_michael', 'Michael: American, calm'],
  ['bf_emma', 'Emma: British, clear'],
  ['bm_george', 'George: British, steady'],
];

const FALLBACK_DEFAULTS = { auto_approve: false, render: { height: 1080, zoom: 1.4, captions: true }, voice: { tts: { voice: 'af_heart' } } };

const SAMPLE_PROMPT =
  'Show how to shop at Harbour Goods: search for a t-shirt, choose the colour Navy and the size Large, ' +
  'add it to the cart, then open the cart.';

// ---------------------------------------------------------------------------------------------
// Small helpers

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(path, { method = 'GET', json, body } = {}) {
  const init = { method, headers: {} };
  if (json !== undefined) {
    init.body = JSON.stringify(json);
    init.headers['content-type'] = 'application/json';
  } else if (body !== undefined) {
    init.body = body;
  }
  let response;
  try {
    response = await fetch(`/api${path}`, init);
  } catch {
    throw new ApiError(0, 'demogen is not answering. Is “demogen ui” still running in your terminal?');
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const data = await response.json();
      detail = typeof data.detail === 'string' ? data.detail : data.detail.map((item) => item.msg).join('; ');
    } catch {
      // keep the status text
    }
    throw new ApiError(response.status, detail);
  }
  return response.status === 204 ? null : response.json();
}

const runPath = (id) => `/runs/${encodeURIComponent(id)}`;
const artifactUrl = (id, path) => `/api${runPath(id)}/artifacts/${path.split('/').map(encodeURIComponent).join('/')}`;

let uidCounter = 0;
const uid = (prefix = 'f') => `${prefix}-${++uidCounter}`;

/** Make an element. Text children are always text, never markup. */
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'class') el.className = value;
    else if (key === 'value' || key === 'checked') el[key] = value;
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

function announce(text) {
  const live = document.getElementById('live');
  live.textContent = '';
  setTimeout(() => (live.textContent = text), 50);
}

function ago(iso) {
  if (!iso) return '';
  const then = new Date(iso);
  const seconds = Math.max(0, (Date.now() - then.getTime()) / 1000);
  if (seconds < 45) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  if (seconds < 172800) return 'yesterday';
  return then.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: then.getFullYear() === new Date().getFullYear() ? undefined : 'numeric' });
}

function parseDuration(value) {
  if (typeof value === 'number') return value;
  const match = /^\s*(\d+(?:\.\d+)?)\s*(ms|s|m)?\s*$/.exec(String(value ?? ''));
  if (!match) return 0;
  return Number(match[1]) * ({ ms: 0.001, s: 1, m: 60 }[match[2]] ?? 1);
}

const wordCount = (text) => (text.trim() ? text.trim().split(/\s+/).length : 0);
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function stageIndex(next) {
  for (const node of next || []) {
    const index = STAGES.findIndex((stage) => stage.nodes.includes(node));
    if (index !== -1) return index;
  }
  return -1;
}

function phaseLabel(run) {
  switch (run.phase) {
    case 'queued':
      return ['Queued', 'quiet'];
    case 'running':
      return [run.next?.[0] === 'load_validate' ? 'Checking' : STAGES[stageIndex(run.next)]?.busy ?? 'Starting', 'busy'];
    case 'awaiting_approval':
      return ['Waiting for you', 'wait'];
    case 'done':
      return ['Done', 'done'];
    case 'interrupted':
      return ['Interrupted', 'wait'];
    default:
      return ['Needs attention', 'alert'];
  }
}

function pill(run) {
  const [text, tone] = phaseLabel(run);
  return h('span', { class: `pill pill-${tone}` }, text);
}

const awaitingWords = { playbook: 'Review the playbook', plan: 'Review the clicks', story: 'Review the narration' };

function errorLine() {
  return h('p', { class: 'error-text', role: 'alert' });
}

/** Run an action from a button: disable it while it works, and show what went wrong. */
async function act(button, errorBox, work) {
  const buttons = button ? [button] : [];
  buttons.forEach((b) => (b.disabled = true));
  if (errorBox) errorBox.textContent = '';
  try {
    await work();
  } catch (error) {
    if (errorBox) errorBox.textContent = error.message;
    else alert(error.message);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

function confirmDialog(title, text, okLabel = 'Delete') {
  const dialog = document.getElementById('confirm');
  document.getElementById('confirm-title').textContent = title;
  document.getElementById('confirm-text').textContent = text;
  document.getElementById('confirm-ok').textContent = okLabel;
  dialog.returnValue = '';
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true }));
}

let healthCache = null;
async function health() {
  if (!healthCache) healthCache = api('/settings/health').catch(() => null);
  return healthCache;
}

function healthBanner(report) {
  if (!report || report.ready) return null;
  return h(
    'div',
    { class: 'notice notice-wait', role: 'status' },
    h('h3', {}, 'Before your first demo'),
    h('p', {}, 'demogen needs a little setup on this machine:'),
    h('ul', {}, report.problems.map((problem) => h('li', {}, problem))),
    h('p', { class: 'hint' }, 'Fix these, then restart “demogen ui”.'),
  );
}

let sampleStore = null;
async function hasSampleStore() {
  if (sampleStore === null) {
    sampleStore = fetch('/examples/shop/', { method: 'HEAD' }).then((r) => r.ok).catch(() => false);
  }
  return sampleStore;
}

// What the new-demo form starts with, when another screen sends you there.
let prefill = null;

// ---------------------------------------------------------------------------------------------
// Routing

const main = document.getElementById('main');
let leave = () => {};

async function route() {
  leave();
  leave = () => {};
  const hash = location.hash.replace(/^#/, '') || '/';
  main.replaceChildren();
  window.scrollTo(0, 0);
  let cleanup;
  const run = /^\/runs\/([^/?]+)/.exec(hash);
  if (run) cleanup = await runView(decodeURIComponent(run[1]));
  else if (hash.startsWith('/new')) cleanup = await newView(hash.includes('sample'));
  else cleanup = await homeView();
  if (typeof cleanup === 'function') leave = cleanup;
  const heading = main.querySelector('h1');
  if (heading) {
    heading.tabIndex = -1;
    heading.focus({ preventScroll: true });
  }
}

window.addEventListener('hashchange', route);
route();

// ---------------------------------------------------------------------------------------------
// Home: your demos

async function homeView() {
  document.title = 'Your demos · demogen';
  const list = h('div', {}, h('p', { class: 'hint' }, 'Loading…'));
  main.append(
    h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Your demos'), h('p', { class: 'lead' }, 'Every demo is saved in its own folder under runs/.')), h('a', { class: 'button primary', href: '#/new' }, 'New demo')),
  );
  const [report] = await Promise.all([health()]);
  const banner = healthBanner(report);
  if (banner) main.append(h('div', { style: 'margin-bottom:20px' }, banner));
  main.append(list);

  let timer;
  let stopped = false;
  const load = async () => {
    let runs;
    try {
      runs = await api('/runs');
    } catch (error) {
      list.replaceChildren(h('div', { class: 'notice notice-alert' }, error.message));
      timer = setTimeout(load, HOME_POLL_MS * 2);
      return;
    }
    if (stopped) return;
    list.replaceChildren(runs.length ? runList(runs) : await emptyState());
    if (runs.some((run) => run.phase === 'running' || run.phase === 'queued')) timer = setTimeout(load, HOME_POLL_MS);
  };
  await load();
  return () => {
    stopped = true;
    clearTimeout(timer);
  };
}

function runList(runs) {
  return h(
    'ul',
    { class: 'runs' },
    runs.map((run) => {
      const showPoster = run.phase === 'done' || run.awaiting === 'story';
      const meta = [ago(run.created_at)];
      if (run.phase === 'awaiting_approval' && run.awaiting) meta.push(awaitingWords[run.awaiting]);
      else if (run.phase === 'running') meta.push(DOING[run.next?.[0]] ?? 'Working');
      return h(
        'li',
        {},
        h(
          'a',
          { class: 'run-row', href: `#/runs/${encodeURIComponent(run.run_id)}` },
          h('div', { class: 'poster', 'aria-hidden': 'true' }, showPoster ? h('img', { src: `/api${runPath(run.run_id)}/poster`, alt: '', loading: 'lazy', onerror: (e) => e.target.remove() }) : '▶'),
          h('div', { style: 'min-width:0' }, h('div', { class: 'run-name' }, run.name), h('div', { class: 'run-meta' }, meta.filter(Boolean).join(' · '))),
          pill(run),
        ),
      );
    }),
  );
}

async function emptyState() {
  const sample = await hasSampleStore();
  return h(
    'div',
    { class: 'empty' },
    h('h2', {}, 'Make your first demo'),
    h('p', { class: 'lead' }, 'Describe what the video should show, or bring a playbook you have written. You check each part before it is recorded.'),
    h(
      'div',
      { class: 'choices' },
      h('a', { class: 'choice', href: '#/new' }, h('strong', {}, 'Describe a demo'), h('span', {}, 'Say in a sentence or two what to show. A playbook is drafted for you to check.')),
      h('a', { class: 'choice', href: '#/new', onclick: () => (prefill = { tab: 'upload' }) }, h('strong', {}, 'Upload a playbook'), h('span', {}, 'Already have a playbook file? Start from it.')),
      sample
        ? h('a', { class: 'choice', href: '#/new?sample' }, h('strong', {}, 'Try it on the sample store'), h('span', {}, 'A ready-made shopping demo on Harbour Goods, a small store built into demogen.'))
        : null,
    ),
  );
}

// ---------------------------------------------------------------------------------------------
// New demo

async function newView(sample) {
  document.title = 'New demo · demogen';
  const report = await health();
  const defaults = report?.defaults ?? FALLBACK_DEFAULTS;
  const start = prefill ?? {};
  prefill = null;
  if (sample) Object.assign(start, { tab: 'prompt', prompt: SAMPLE_PROMPT, url: `${location.origin}/examples/shop/` });

  // Describe it
  const promptId = uid();
  const urlId = uid();
  const briefId = uid();
  const prompt = h('textarea', { id: promptId, rows: 4, placeholder: 'For example: show how a new user adds a task, sets a due date and marks it done.', value: start.prompt ?? '' });
  const url = h('input', { id: urlId, type: 'url', placeholder: 'https://app.example.com', value: start.url ?? '' });
  const brief = h('textarea', { id: briefId, rows: 3, placeholder: 'For example: for first-time users; friendly and brief; end by inviting them to try it.', value: start.brief ?? '' });

  // Upload
  let file = start.file ?? null;
  const fileId = uid();
  const fileInput = h('input', { id: fileId, type: 'file', accept: '.yaml,.yml,application/yaml,text/yaml' });
  const fileReport = h('div', { 'aria-live': 'polite' });
  const dropText = h('span', {}, 'Drop a playbook here, or choose a file');
  const drop = h('label', { class: 'drop', for: fileId }, h('strong', {}, '⬆'), dropText, h('span', { class: 'hint' }, 'A .yaml file'), fileInput);

  async function checkFile(chosen) {
    file = chosen;
    fileReport.replaceChildren();
    if (!file) return;
    dropText.textContent = file.name;
    const form = new FormData();
    form.append('playbook', file);
    try {
      const checked = await api('/playbooks/check', { method: 'POST', body: form });
      fileReport.replaceChildren(
        h(
          'div',
          { class: 'notice notice-done' },
          h('h3', {}, checked.name),
          h('p', { class: 'hint' }, `Starts at ${checked.base_url} · ${plural(checked.steps.length, 'step')}`),
          h('ol', { class: 'steps-preview' }, checked.steps.map((step) => h('li', {}, step.do))),
        ),
      );
    } catch (error) {
      file = null;
      fileReport.replaceChildren(h('div', { class: 'notice notice-alert', role: 'alert' }, h('h3', {}, 'This playbook has a problem'), h('p', { class: 'problems' }, error.message)));
    }
  }
  fileInput.addEventListener('change', () => checkFile(fileInput.files[0] ?? null));
  drop.addEventListener('dragover', (event) => {
    event.preventDefault();
    drop.classList.add('over');
  });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (event) => {
    event.preventDefault();
    drop.classList.remove('over');
    if (event.dataTransfer.files[0]) checkFile(event.dataTransfer.files[0]);
  });
  if (file) checkFile(file);

  // Tabs
  const panels = {
    prompt: h(
      'div',
      { role: 'tabpanel', id: 'panel-prompt', 'aria-labelledby': 'tab-prompt' },
      h('div', { class: 'field' }, h('label', { for: promptId }, 'What should the demo show?'), prompt),
      h('div', { class: 'field' }, h('label', { for: urlId }, 'Starts at', h('span', { class: 'hint' }, 'the address the demo opens')), url),
    ),
    upload: h('div', { role: 'tabpanel', id: 'panel-upload', 'aria-labelledby': 'tab-upload' }, h('div', { class: 'field' }, drop), fileReport),
  };
  let tab = start.tab === 'upload' ? 'upload' : 'prompt';
  const tabs = {};
  const selectTab = (name, focus = false) => {
    tab = name;
    for (const [key, button] of Object.entries(tabs)) {
      button.setAttribute('aria-selected', String(key === name));
      button.tabIndex = key === name ? 0 : -1;
      panels[key].hidden = key !== name;
    }
    if (focus) tabs[name].focus();
  };
  for (const [key, label] of [['prompt', 'Describe it'], ['upload', 'Upload a playbook']]) {
    tabs[key] = h('button', { type: 'button', role: 'tab', class: 'tab', id: `tab-${key}`, 'aria-controls': `panel-${key}`, onclick: () => selectTab(key) }, label);
  }
  const tabList = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'How to start' }, tabs.prompt, tabs.upload);
  tabList.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') selectTab(tab === 'prompt' ? 'upload' : 'prompt', true);
  });
  selectTab(tab);

  // Options
  const pauseId = uid();
  const straightId = uid();
  const pause = h('input', { type: 'radio', name: 'review', id: pauseId, checked: !defaults.auto_approve });
  const straight = h('input', { type: 'radio', name: 'review', id: straightId, checked: defaults.auto_approve });
  const sizeId = uid();
  const size = h('select', { id: sizeId }, h('option', { value: '1080' }, '1080p'), h('option', { value: 'take' }, 'As recorded'));
  size.value = defaults.render.height == null ? 'take' : '1080';
  const zoomOn = defaults.render.zoom > 1;
  const zoom = h('input', { type: 'checkbox', checked: zoomOn });
  const captions = h('input', { type: 'checkbox', checked: defaults.render.captions });
  const voiceId = uid();
  const voices = [...VOICES];
  if (!voices.some(([value]) => value === defaults.voice.tts.voice)) voices.unshift([defaults.voice.tts.voice, `${defaults.voice.tts.voice} (your default)`]);
  const voice = h('select', { id: voiceId }, voices.map(([value, label]) => h('option', { value }, label)));
  voice.value = defaults.voice.tts.voice;

  const options = h(
    'details',
    { class: 'options card' },
    h('summary', {}, 'Options'),
    h(
      'fieldset',
      {},
      h('legend', {}, 'Reviews'),
      h(
        'div',
        { class: 'radios' },
        h('label', { class: 'check', for: pauseId }, pause, 'Pause for my review of the playbook, the clicks and the narration'),
        h('label', { class: 'check', for: straightId }, straight, 'Go straight through to the video'),
      ),
    ),
    h('div', { class: 'row field' }, h('div', {}, h('label', { for: sizeId }, 'Video size'), size), h('div', {}, h('label', { for: voiceId }, 'Voice'), voice)),
    h('div', { class: 'field' }, h('label', { class: 'check' }, zoom, 'Zoom in on what each step acts on')),
    h('div', { class: 'field' }, h('label', { class: 'check' }, captions, 'Burn in captions')),
    h('p', { class: 'hint' }, 'A voice you have not used before is downloaded the first time.'),
  );

  function chosenOptions() {
    const chosen = {};
    const render = {};
    if (straight.checked !== defaults.auto_approve) chosen.auto_approve = straight.checked;
    const height = size.value === 'take' ? null : 1080;
    if (height !== defaults.render.height) render.height = height;
    if (zoom.checked !== zoomOn) render.zoom = zoom.checked ? 1.4 : 1;
    if (captions.checked !== defaults.render.captions) render.captions = captions.checked;
    if (Object.keys(render).length) chosen.render = render;
    if (voice.value !== defaults.voice.tts.voice) chosen.voice = { tts: { voice: voice.value } };
    return chosen;
  }

  const errorBox = errorLine();
  const create = h('button', { class: 'primary', type: 'submit' }, 'Create demo');
  const form = h(
    'form',
    { novalidate: true },
    tabList,
    panels.prompt,
    panels.upload,
    h('div', { class: 'field', style: 'margin-top:16px' }, h('label', { for: briefId }, 'Who is it for, and how should it sound?', h('span', { class: 'hint' }, 'optional')), brief),
    options,
    h('div', { class: 'bottom-bar' }, create, h('a', { href: '#/' }, 'Cancel'), h('span', { class: 'spacer' }), errorBox),
  );
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    act(create, errorBox, async () => {
      const body = new FormData();
      if (tab === 'prompt') {
        if (!prompt.value.trim()) {
          prompt.focus();
          throw new Error('Say what the demo should show.');
        }
        if (url.value.trim() && !/^https?:\/\//i.test(url.value.trim())) {
          url.focus();
          throw new Error('The start address should begin with http:// or https://');
        }
        body.append('prompt', prompt.value.trim());
        if (url.value.trim()) body.append('url', url.value.trim());
      } else {
        if (!file) throw new Error('Choose a playbook that passes the check first.');
        body.append('playbook', file);
      }
      if (brief.value.trim()) body.append('intent', new File([brief.value.trim()], 'intent.md', { type: 'text/markdown' }));
      const chosen = chosenOptions();
      if (Object.keys(chosen).length) body.append('options', JSON.stringify(chosen));
      const run = await api('/runs', { method: 'POST', body });
      location.hash = `#/runs/${encodeURIComponent(run.run_id)}`;
    });
  });

  const sampleLink = !sample && (await hasSampleStore()) ? h('a', { href: '#/new?sample' }, 'try it on the sample store') : null;
  main.append(
    h('a', { class: 'back', href: '#/' }, '← Your demos'),
    h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'New demo'), h('p', { class: 'lead' }, 'Describe what to show, or upload a playbook.', sampleLink ? [' Not sure yet? ', sampleLink, '.'] : null))),
  );
  const banner = healthBanner(report);
  if (banner) main.append(h('div', { style: 'margin-bottom:20px' }, banner));
  if (sample) main.append(h('div', { class: 'notice notice-info', style: 'margin-bottom:20px' }, 'This demo uses Harbour Goods, a sample store served by demogen at ', h('code', {}, url.value), '. Change the description if you like, then create it.'));
  main.append(form);
}

// ---------------------------------------------------------------------------------------------
// A run

async function runView(id) {
  const back = h('a', { class: 'back', href: '#/' }, '← Your demos');
  const head = h('div', { class: 'run-head' });
  const rail = h('ol', { class: 'rail', 'aria-label': 'Progress' });
  const panel = h('section', { 'aria-live': 'off' });
  main.append(back, head, rail, panel);

  let timer;
  let stopped = false;
  let shown = null; // what the panel shows now; it is redrawn only when that changes
  let status = null;

  const ctx = {
    id,
    refresh: async () => {
      shown = null;
      clearTimeout(timer);
      await tick();
    },
    rail: (options) => drawRail(rail, status, options),
  };

  async function tick() {
    try {
      status = await api(runPath(id));
    } catch (error) {
      if (stopped) return;
      if (error.status === 404) {
        head.replaceChildren(h('h1', {}, 'No such demo'));
        panel.replaceChildren(h('p', {}, 'It may have been deleted. ', h('a', { href: '#/' }, 'See your demos.')));
        rail.remove();
        return;
      }
      panel.replaceChildren(h('div', { class: 'notice notice-alert' }, error.message));
      timer = setTimeout(tick, POLL_MS * 2);
      return;
    }
    if (stopped) return;
    document.title = `${status.name} · demogen`;
    const key = [status.phase, status.awaiting?.kind, status.next.join(','), status.queue_position].join('|');
    if (key !== shown) {
      if (shown !== null) announce(announcement(status));
      shown = key;
      drawHead(head, status, ctx);
      drawRail(rail, status);
      await drawPanel(panel, status, ctx);
    }
    if (status.phase === 'running' || status.phase === 'queued') timer = setTimeout(tick, POLL_MS);
  }

  await tick();
  return () => {
    stopped = true;
    clearTimeout(timer);
  };
}

function announcement(status) {
  if (status.phase === 'awaiting_approval') return `Waiting for you: ${awaitingWords[status.awaiting.kind].toLowerCase()}`;
  if (status.phase === 'running') return DOING[status.next[0]] ?? 'Working';
  if (status.phase === 'done') return 'The video is ready';
  return phaseLabel(status)[0];
}

function drawHead(head, status, ctx) {
  const menu = h(
    'details',
    { class: 'menu' },
    h('summary', { class: 'button icon', 'aria-label': 'More actions', title: 'More actions' }, '⋯'),
    h(
      'div',
      { class: 'menu-list' },
      h('button', { type: 'button', onclick: (event) => act(null, null, async () => { event.target.closest('details').open = false; await api(`${runPath(ctx.id)}/reveal`, { method: 'POST' }); }) }, 'Show in Finder'),
      h('button', { type: 'button', class: 'danger-text', onclick: (event) => deleteRun(event, status) }, 'Delete demo…'),
    ),
  );
  const subtitle = h('div', { class: 'run-sub' }, pill(status), h('span', {}, `Started ${ago(status.created_at)}`), h('span', {}, status.started_from === 'prompt' ? 'from a description' : 'from a playbook file'));
  head.replaceChildren(h('div', { style: 'min-width:0' }, h('h1', {}, status.name), subtitle), menu);
}

async function deleteRun(event, status) {
  event.target.closest('details').open = false;
  const folder = `runs/${status.run_id}`;
  const sure = await confirmDialog('Delete this demo?', `This removes ${folder} and everything in it: the video, the recordings and the reviews. It can't be undone.`);
  if (!sure) return;
  await act(null, null, async () => {
    await api(runPath(status.run_id), { method: 'DELETE' });
    location.hash = '#/';
  });
}

function drawRail(rail, status, { failure = false } = {}) {
  if (!status) return;
  const at = stageIndex(status.next);
  const current = { running: 'running', queued: 'queued', awaiting_approval: 'waiting', interrupted: 'waiting' }[status.phase] ?? 'attention';
  const words = { done: 'done', running: 'in progress', queued: 'queued', waiting: 'waiting for you', attention: 'needs attention', todo: 'not started' };
  rail.replaceChildren(
    ...STAGES.map((stage, index) => {
      let state = 'todo';
      if (status.phase === 'done') state = 'done';
      else if (at !== -1 && index < at) state = 'done';
      else if (index === at) state = current;
      if (failure && index === 1) state = 'attention';
      let label = stage.label;
      if (index === 0 && status.started_from === 'playbook' && state === 'done') label = 'Your playbook';
      return h(
        'li',
        { class: `is-${state}`, 'aria-current': index === at && status.phase !== 'done' ? 'step' : null },
        h('span', { class: 'mark', 'aria-hidden': 'true' }, state === 'done' ? '✓' : state === 'waiting' ? '•' : state === 'attention' ? '!' : ''),
        h('span', {}, label, h('span', { class: 'visually-hidden' }, `: ${words[state]}`)),
      );
    }),
  );
}

async function drawPanel(panel, status, ctx) {
  switch (status.phase) {
    case 'queued': {
      const ahead = status.queue_position;
      panel.replaceChildren(
        h('div', { class: 'card working' }, h('div', { class: 'spinner', 'aria-hidden': 'true' }), h('div', {}, h('p', {}, h('strong', {}, `Waiting for ${plural(ahead, 'demo')} ahead of this one to finish.`)), h('p', { class: 'hint' }, 'demogen makes one demo at a time. This one starts by itself.'))),
      );
      return;
    }
    case 'running':
      panel.replaceChildren(
        h(
          'div',
          { class: 'card working' },
          h('div', { class: 'spinner', 'aria-hidden': 'true' }),
          h('div', {}, h('p', {}, h('strong', {}, `${DOING[status.next[0]] ?? 'Getting started'}…`)), h('p', { class: 'hint' }, 'You can leave this page: the demo carries on, and it will be here when you come back.')),
        ),
      );
      return;
    case 'awaiting_approval': {
      panel.replaceChildren(h('p', { class: 'hint' }, 'Loading…'));
      let review;
      try {
        review = await api(`${runPath(ctx.id)}/review`);
      } catch (error) {
        panel.replaceChildren(h('div', { class: 'notice notice-alert' }, error.message));
        return;
      }
      const draw = { playbook: playbookReview, plan: planReview, story: storyReview }[review.kind];
      panel.replaceChildren(draw(ctx, review, status));
      return;
    }
    case 'done':
      panel.replaceChildren(await donePanel(ctx, status));
      return;
    default:
      panel.replaceChildren(await attentionPanel(ctx, status));
  }
}

// What went wrong, as the fail-point log names it (demogen/failures.py).
const FAILURE_KINDS = {
  bad_ref: 'acted on something no longer on the page',
  element_not_found: 'could not find the element',
  action_failed: 'the browser could not do the action',
  evidence_not_visible: 'claimed success with text not on the page',
  fragile_target: 'chose an element with no name',
  no_effect: 'took an action that changed nothing',
  bad_arguments: 'called a tool wrongly',
  unknown_tool: 'called a tool that does not exist',
  step_unresolved: 'gave up on the step',
  turn_limit: 'ran out of turns',
  expectation_missed: 'the page did not show what rehearsal saw',
};
const FAILURE_PHASES = { rehearsal: 'Rehearsal', take: 'The take', repair: 'Repair', setup: 'Signing in' };
const OUTCOMES = { resolved: 'recovered', unresolved: 'not recovered', failed: 'stopped here' };

async function failuresOf(ctx) {
  try {
    return await api(`${runPath(ctx.id)}/failures`);
  } catch {
    return [];
  }
}

/** The page as the browser showed it when the run broke. */
function lastSeen(ctx, record) {
  const shot = record.files.screenshot;
  const where = [record.page.title, record.page.url].filter(Boolean).join(' · ');
  return h(
    'div',
    { class: 'card seen' },
    shot ? h('a', { href: artifactUrl(ctx.id, shot), target: '_blank', rel: 'noopener', class: 'frame' }, h('img', { src: artifactUrl(ctx.id, shot), alt: `What the browser showed: ${record.page.title || record.page.url || 'the page'}` })) : h('div', { class: 'frame' }, 'No picture'),
    h(
      'div',
      { class: 'stack' },
      h('h3', {}, 'What the browser was showing'),
      where ? h('p', { class: 'seen-where' }, where) : null,
      h('p', {}, `${FAILURE_PHASES[record.phase] ?? record.phase}, step “${record.step.do ?? record.step.id}”: ${FAILURE_KINDS[record.kind] ?? record.kind}.`),
      record.page.error ? h('p', { class: 'hint' }, record.page.error) : null,
    ),
  );
}

/** Every fail point of the run, oldest first. */
function failureList(ctx, records) {
  return h(
    'details',
    { class: 'fold' },
    h('summary', {}, `All fail points (${records.length})`),
    h('p', { class: 'hint', style: 'margin:6px 0' }, `Kept in runs/${ctx.id}/failures, each with a screenshot and what the page contained. Export them with “demogen failures”.`),
    h(
      'ul',
      { class: 'actions-list failure-list' },
      records.map((record) =>
        h(
          'li',
          {},
          h('span', { class: 'what' }, h('strong', {}, `${FAILURE_PHASES[record.phase] ?? record.phase} · ${record.step.id}: `), FAILURE_KINDS[record.kind] ?? record.kind, h('span', { class: 'hint' }, ` (${OUTCOMES[record.step_outcome] ?? 'under way'})`)),
          record.files.screenshot ? h('a', { href: artifactUrl(ctx.id, record.files.screenshot), target: '_blank', rel: 'noopener' }, 'Screenshot') : null,
        ),
      ),
    ),
  );
}

async function attentionPanel(ctx, status) {
  const records = await failuresOf(ctx);
  const broke = [...records].reverse().find((record) => record.step_outcome === 'failed');
  const errorBox = errorLine();
  const resume = h('button', { class: 'primary', onclick: () => act(resume, errorBox, async () => { await api(`${runPath(ctx.id)}/resume`, { method: 'POST' }); await ctx.refresh(); }) }, 'Resume');
  if (status.phase === 'failed' && status.error) {
    const lines = status.error.split('\n').filter((line) => line.trim() && !line.startsWith('The run is saved'));
    const first = lines[0] ?? status.error;
    const again = h('button', { type: 'button', onclick: () => act(again, errorBox, () => startLike(ctx, status)) }, status.started_from === 'prompt' ? 'Start again from this description' : 'Start again from this playbook');
    return h(
      'div',
      { class: 'stack' },
      h('div', { class: 'notice notice-alert', role: 'alert' }, h('h3', {}, 'This demo stopped'), h('p', {}, first.charAt(0).toUpperCase() + first.slice(1))),
      broke ? lastSeen(ctx, broke) : null,
      h('p', {}, 'Everything done so far is saved. If the cause is on your side (the app being down, a missing setting), fix it and resume: it picks up at the part that stopped. If the playbook itself needs changing, start again from it.'),
      h('details', { class: 'fold' }, h('summary', {}, 'Details'), h('pre', { class: 'details' }, status.error)),
      records.length ? failureList(ctx, records) : null,
      h('div', { class: 'bottom-bar' }, resume, again, h('span', { class: 'spacer' }), errorBox),
    );
  }
  return h(
    'div',
    { class: 'stack' },
    h('div', { class: 'notice notice-wait' }, h('h3', {}, 'This demo was interrupted'), h('p', {}, 'It was part-way through when demogen last stopped. Nothing is lost: resume to pick up where it left off.')),
    h('div', { class: 'bottom-bar' }, resume, errorBox),
  );
}

// ---------------------------------------------------------------------------------------------
// Review: the playbook

/** The playbook as it would be saved: blanks dropped, times in seconds. Used to tell whether anything changed. */
function cleanPlaybook(source) {
  const playbook = structuredClone(source);
  playbook.name = (playbook.name ?? '').trim();
  if (playbook.target) playbook.target.base_url = (playbook.target.base_url ?? '').trim();
  if (playbook.intent) {
    const intent = {};
    for (const [key, value] of Object.entries(playbook.intent)) {
      if (key === 'key_messages') {
        const messages = (value ?? []).map((message) => message.trim()).filter(Boolean);
        if (messages.length) intent.key_messages = messages;
      } else if (typeof value === 'string' ? value.trim() : value != null) {
        intent[key] = typeof value === 'string' ? value.trim() : value;
      }
    }
    if (Object.keys(intent).length) playbook.intent = intent;
    else delete playbook.intent;
  }
  playbook.steps = (playbook.steps ?? []).map((original) => {
    const step = { ...original, do: (original.do ?? '').trim() };
    for (const key of ['expect', 'say_hint']) {
      if (typeof step[key] === 'string' && step[key].trim()) step[key] = step[key].trim();
      else delete step[key];
    }
    const dwell = parseDuration(step.dwell ?? 0);
    if (dwell > 0) step.dwell = dwell;
    else delete step.dwell;
    return step;
  });
  return playbook;
}

function newStepId(steps) {
  const taken = new Set(steps.map((step) => step.id));
  let n = steps.length + 1;
  while (taken.has(`step-${n}`)) n += 1;
  return `step-${n}`;
}

function bound(object, key, { rows = 2, placeholder = '', onchange, lines = false } = {}) {
  const value = lines ? (object[key] ?? []).join('\n') : object[key] ?? '';
  const field = rows === 1 ? h('input', { type: 'text', value, placeholder }) : h('textarea', { rows, value, placeholder });
  field.addEventListener('input', () => {
    object[key] = lines ? field.value.split('\n') : field.value;
    onchange?.();
  });
  return field;
}

function labelled(label, field, hint) {
  field.id ||= uid();
  return h('div', { class: 'field' }, h('label', { for: field.id }, label, hint ? h('span', { class: 'hint' }, hint) : null), field);
}

function playbookReview(ctx, review) {
  const original = cleanPlaybook(review.playbook);
  const playbook = structuredClone(review.playbook);
  playbook.target ??= {};
  playbook.intent ??= {};
  const failure = review.failure;
  if (failure) ctx.rail({ failure: true });
  let textMode = false;

  const errorBox = errorLine();
  const primary = h('button', { class: 'primary', type: 'button' });
  const text = h('textarea', { class: 'mono', rows: 24, value: review.yaml, 'aria-label': 'The playbook as YAML', spellcheck: 'false' });
  const changed = () => (textMode ? text.value !== review.yaml : JSON.stringify(cleanPlaybook(playbook)) !== JSON.stringify(original));
  const update = () => {
    if (failure) primary.textContent = changed() ? 'Save changes and rehearse again' : 'Rehearse again';
    else primary.textContent = changed() ? 'Save changes and continue' : 'Approve';
  };
  text.addEventListener('input', update);

  const steps = h('div', {});
  function drawSteps(focusSelector) {
    steps.replaceChildren(...playbook.steps.map((step, index) => stepCard(step, index)));
    if (focusSelector) steps.querySelector(focusSelector)?.focus();
    update();
  }

  function stepCard(step, index) {
    const total = playbook.steps.length;
    const failed = failure && failure.step_id === step.id;
    const dwellValue = parseDuration(step.dwell ?? 0);
    const dwellOut = h('output', {}, dwellValue ? `${dwellValue} s` : 'none');
    const dwell = h('input', { type: 'range', min: 0, max: 4, step: 0.5, value: String(dwellValue) });
    dwell.addEventListener('input', () => {
      step.dwell = Number(dwell.value);
      dwellOut.textContent = step.dwell ? `${step.dwell} s` : 'none';
      update();
    });
    dwell.id = uid();
    const move = (to, cls) => {
      const [moved] = playbook.steps.splice(index, 1);
      playbook.steps.splice(to, 0, moved);
      drawSteps(`[data-index="${to}"] .${cls}`);
    };
    const doField = bound(step, 'do', { placeholder: 'What to do on screen, e.g. Search for “t-shirt”', onchange: update });
    return h(
      'div',
      { class: `card step-card${failed ? ' failed' : ''}`, 'data-index': index },
      h('h3', { class: 'visually-hidden' }, `Step ${index + 1}`),
      h(
        'div',
        { class: 'step-top step-top-edit' },
        h('span', { class: 'step-num', 'aria-hidden': 'true' }, index + 1),
        h('div', { class: 'step-do' }, labelled('What to do', doField, step.action ? 'its clicks are fixed in the playbook, so it is not rehearsed' : null)),
        h(
          'div',
          { class: 'step-tools' },
          h('button', { type: 'button', class: 'icon up', 'aria-label': `Move step ${index + 1} up`, disabled: index === 0, onclick: () => move(index - 1, 'up') }, '↑'),
          h('button', { type: 'button', class: 'icon down', 'aria-label': `Move step ${index + 1} down`, disabled: index === total - 1, onclick: () => move(index + 1, 'down') }, '↓'),
          h('button', {
            type: 'button', class: 'icon remove', 'aria-label': `Delete step ${index + 1}`, disabled: total === 1,
            onclick: () => { playbook.steps.splice(index, 1); drawSteps(`[data-index="${Math.min(index, playbook.steps.length - 1)}"] .remove`); },
          }, '✕'),
        ),
      ),
      failed ? h('div', { class: 'notice notice-alert field', role: 'alert' }, h('strong', {}, 'Rehearsal couldn’t do this step: '), failure.reason) : null,
      h(
        'div',
        { class: 'row' },
        labelled('Visible afterwards', bound(step, 'expect', { placeholder: 'e.g. The matching products are listed', onchange: update }), 'optional'),
        labelled('The point to make', bound(step, 'say_hint', { placeholder: 'A hint for the narration', onchange: update }), 'optional'),
      ),
      h('div', { class: 'field' }, h('label', { for: dwell.id }, 'Time on screen afterwards', h('span', { class: 'hint' }, 'longer gives the narration more room')), h('div', { class: 'dwell' }, dwell, dwellOut)),
    );
  }

  const addStep = h('button', {
    type: 'button', class: 'add-step',
    onclick: () => {
      playbook.steps.push({ id: newStepId(playbook.steps), do: '', dwell: 2 });
      drawSteps(`[data-index="${playbook.steps.length - 1}"] textarea`);
    },
  }, '+ Add a step');

  const runFields = h(
    'div',
    { class: 'card', style: 'margin-bottom:16px' },
    h('div', { class: 'row' }, labelled('Name', bound(playbook, 'name', { rows: 1, onchange: update })), labelled('Starts at', bound(playbook.target, 'base_url', { rows: 1, onchange: update }))),
    h('div', { class: 'row' }, labelled('Who it is for', bound(playbook.intent, 'audience', { rows: 1, onchange: update, placeholder: 'e.g. First-time shoppers' })), labelled('Tone', bound(playbook.intent, 'tone', { rows: 1, onchange: update, placeholder: 'e.g. Friendly and brief' }))),
    labelled('Key messages', bound(playbook.intent, 'key_messages', { rows: 3, lines: true, onchange: update, placeholder: 'One per line' }), 'one per line'),
    labelled('Call to action', bound(playbook.intent, 'cta', { rows: 1, onchange: update, placeholder: 'e.g. Try it free today' })),
  );

  const cards = h('div', {}, runFields, steps, addStep);
  const textView = h('div', { hidden: true }, h('p', { class: 'hint', style: 'margin-bottom:8px' }, 'The playbook exactly as it will be saved. It is checked before anything continues.'), text);
  const modeButton = h('button', { type: 'button', class: 'link' }, 'Edit as text');
  modeButton.addEventListener('click', async () => {
    if (!textMode && changed() && !(await confirmDialog('Edit as text?', 'The text shows the playbook as it was drafted, without the changes you made in the cards. Those changes will be set aside.', 'Edit as text'))) return;
    if (textMode && changed() && !(await confirmDialog('Back to the cards?', 'The changes you made to the text will be set aside.', 'Back to the cards'))) return;
    textMode = !textMode;
    cards.hidden = textMode;
    textView.hidden = !textMode;
    modeButton.textContent = textMode ? 'Back to the cards' : 'Edit as text';
    update();
  });

  primary.addEventListener('click', () =>
    act(primary, errorBox, async () => {
      let request = { method: 'POST' };
      if (changed()) {
        if (textMode) {
          request.body = text.value;
        } else {
          const cleaned = cleanPlaybook(playbook);
          if (!cleaned.name) throw new Error('Give the demo a name.');
          if (!cleaned.target?.base_url) throw new Error('Say which address the demo starts at.');
          const empty = cleaned.steps.findIndex((step) => !step.do);
          if (empty !== -1) {
            steps.querySelector(`[data-index="${empty}"] textarea`)?.focus();
            throw new Error(`Step ${empty + 1} needs something to do.`);
          }
          request.json = cleaned;
        }
      }
      await api(`${runPath(ctx.id)}/approve`, request);
      await ctx.refresh();
    }),
  );

  let feedback = null;
  if (review.can_feedback) {
    const box = h('textarea', { rows: 3, placeholder: 'For example: add a step that opens the cart at the end, and make the tone warmer.' });
    const send = h('button', { type: 'button' }, 'Send and redraft');
    const feedbackError = errorLine();
    send.addEventListener('click', () =>
      act(send, feedbackError, async () => {
        if (!box.value.trim()) {
          box.focus();
          throw new Error('Say what should change.');
        }
        await api(`${runPath(ctx.id)}/feedback`, { method: 'POST', json: { feedback: box.value.trim() } });
        await ctx.refresh();
      }),
    );
    feedback = h('details', { class: 'card fold', style: 'margin-top:16px' }, h('summary', {}, 'Ask for changes in words instead'), h('div', { style: 'margin-top:10px' }, labelled('What should change?', box), h('div', { style: 'display:flex;gap:12px;align-items:center;flex-wrap:wrap' }, send, feedbackError)));
  }

  drawSteps();
  const failedDo = failure ? review.playbook.steps.find((step) => step.id === failure.step_id)?.do : null;
  return h(
    'div',
    {},
    h(
      'div',
      { class: 'review-intro' },
      h('div', { class: 'page-head', style: 'margin-bottom:0' }, h('h2', {}, failure ? 'Rehearsal got stuck: check the playbook' : 'Check the playbook'), modeButton),
      failure
        ? h('div', { class: 'notice notice-alert', style: 'margin-top:10px' }, h('p', {}, h('strong', {}, `Rehearsal couldn’t do “${failedDo ?? failure.step_id}”. `), 'Change that step (or the ones before it), ', review.can_feedback ? 'ask for changes, ' : '', 'or try again as it is.'))
        : h('p', { class: 'lead' }, 'These are the steps the demo will show, in order. Change anything that is not right; nothing is recorded until you approve.'),
    ),
    cards,
    textView,
    feedback,
    h('div', { class: 'bottom-bar' }, primary, h('span', { class: 'spacer' }), errorBox),
  );
}

// ---------------------------------------------------------------------------------------------
// Review: the plan

function planReview(ctx, review) {
  const removed = new Set(); // "step:kind:index"
  const errorBox = errorLine();
  const primary = h('button', { class: 'primary', type: 'button' }, 'Approve');
  const update = () => (primary.textContent = removed.size ? 'Save changes and continue' : 'Approve');

  function row(key, words, cls = '') {
    const li = h('li', { class: cls });
    const draw = () => {
      const gone = removed.has(key);
      li.classList.toggle('removed', gone);
      li.replaceChildren(
        h('span', { class: 'what' }, words, gone ? h('span', { class: 'visually-hidden' }, ' (removed)') : null),
        h('button', {
          type: 'button', class: gone ? 'link' : 'icon', 'aria-label': gone ? `Put back: ${words}` : `Remove: ${words}`,
          onclick: () => { gone ? removed.delete(key) : removed.add(key); draw(); update(); li.querySelector('button').focus(); },
        }, gone ? 'Put back' : '✕'),
      );
    };
    draw();
    return li;
  }

  const cards = review.steps.map((step, index) =>
    h(
      'div',
      { class: 'card step-card' },
      h('div', { class: 'step-top' }, h('span', { class: 'step-num', 'aria-hidden': 'true' }, index + 1), h('h3', {}, step.do), step.pinned ? h('span', { class: 'badge' }, 'From the playbook') : null),
      h('p', { class: 'sublabel' }, 'Actions'),
      step.actions.length
        ? h('ul', { class: 'actions-list' }, step.actions.map((words, i) => row(`${step.step_id}:a:${i}`, words)))
        : h('p', { class: 'hint' }, 'No actions: the step only shows the page as it is.'),
      step.expect_text.length ? [h('p', { class: 'sublabel' }, 'Checks'), h('ul', { class: 'actions-list' }, step.expect_text.map((text, i) => row(`${step.step_id}:c:${i}`, `Will check the page shows “${text}”`, 'check-row')))] : null,
    ),
  );

  primary.addEventListener('click', () =>
    act(primary, errorBox, async () => {
      const request = { method: 'POST' };
      if (removed.size) {
        const plan = structuredClone(review.plan);
        for (const step of plan.steps) {
          step.actions = step.actions.filter((_, i) => !removed.has(`${step.step_id}:a:${i}`));
          step.expect_text = step.expect_text.filter((_, i) => !removed.has(`${step.step_id}:c:${i}`));
        }
        request.json = plan;
      }
      await api(`${runPath(ctx.id)}/approve`, request);
      await ctx.refresh();
    }),
  );

  return h(
    'div',
    {},
    h('div', { class: 'review-intro' }, h('h2', {}, 'Check the clicks'), h('p', { class: 'lead' }, 'Rehearsal worked out what to do for each step. These clicks are replayed exactly, on camera: remove any that should not be in the video. The checks make sure each step worked before the recording goes on.')),
    cards,
    h('div', { class: 'bottom-bar' }, primary, h('span', { class: 'spacer' }), errorBox),
  );
}

// ---------------------------------------------------------------------------------------------
// Review: the story

function storyReview(ctx, review) {
  const story = structuredClone(review.story);
  const originalText = review.story.steps.map((step) => step.text);
  const errorBox = errorLine();
  const primary = h('button', { class: 'primary', type: 'button' }, 'Approve');
  const changed = () => story.steps.some((step, i) => step.text.trim() !== originalText[i].trim());
  const update = () => (primary.textContent = changed() ? 'Save changes and continue' : 'Approve');

  const cards = review.steps.map((step, index) => {
    const passage = story.steps[index];
    const field = h('textarea', { rows: 3, value: passage.text });
    field.id = uid();
    const counter = h('p', { class: 'counter', id: uid() });
    field.setAttribute('aria-describedby', counter.id);
    const count = () => {
      const words = wordCount(field.value);
      counter.className = `counter${words > step.limit ? ' over' : words > step.target ? ' warn' : ''}`;
      let note = '';
      if (words > step.limit) note = ' · too long for the footage: it may be sped up or rewritten';
      else if (words > step.target) note = ' · a little long';
      else if (!words) note = ' · needs some words';
      counter.textContent = `${plural(words, 'word')} · aim ${step.target} · max ${step.limit}${note}`;
    };
    field.addEventListener('input', () => {
      passage.text = field.value;
      count();
      update();
    });
    count();
    const facts = step.happens || step.on_screen.length
      ? h('div', { class: 'facts' }, step.happens ? h('p', {}, `On screen: ${step.happens}`) : null, step.on_screen.length ? h('ul', {}, step.on_screen.map((text) => h('li', {}, `“${text}”`))) : null)
      : null;
    return h(
      'div',
      { class: 'card step-card story-step' },
      h('div', { class: 'frame' }, step.keyframe ? h('img', { src: artifactUrl(ctx.id, step.keyframe), alt: `The end of step ${index + 1}: ${step.do}`, loading: 'lazy' }) : 'No picture'),
      h(
        'div',
        {},
        h('div', { class: 'step-top', style: 'margin-bottom:8px' }, h('span', { class: 'step-num', 'aria-hidden': 'true' }, index + 1), h('h3', {}, step.do)),
        h('label', { for: field.id, class: 'hint' }, `Narration for ${step.seconds} s of footage`),
        field,
        counter,
        facts,
      ),
    );
  });

  primary.addEventListener('click', () =>
    act(primary, errorBox, async () => {
      const request = { method: 'POST' };
      if (changed()) {
        const empty = story.steps.findIndex((step) => !step.text.trim());
        if (empty !== -1) throw new Error(`Step ${empty + 1} needs some words to say.`);
        story.steps.forEach((step) => (step.text = step.text.trim()));
        request.json = story;
      }
      await api(`${runPath(ctx.id)}/approve`, request);
      await ctx.refresh();
    }),
  );

  return h(
    'div',
    {},
    h('div', { class: 'review-intro' }, h('h2', {}, 'Check the narration'), h('p', { class: 'lead' }, 'One line for each step, beside the moment it is spoken over. Keep each near its aim so the voice fits the picture.')),
    cards,
    h('div', { class: 'bottom-bar' }, primary, h('span', { class: 'spacer' }), errorBox),
  );
}

// ---------------------------------------------------------------------------------------------
// Done

/** Open the new-demo form filled in from this run: its description, or its playbook as it stands. */
async function startLike(ctx, status) {
  if (status.started_from === 'prompt') {
    const text = await fetch(artifactUrl(ctx.id, 'prompt.md')).then((r) => (r.ok ? r.text() : ''));
    prefill = { tab: 'prompt', prompt: text, url: status.target_url ?? '' };
  } else {
    const response = await fetch(artifactUrl(ctx.id, 'playbook.yaml'));
    if (!response.ok) throw new Error('This demo has no playbook to start from.');
    prefill = { tab: 'upload', file: new File([await response.text()], `${ctx.id}.yaml`, { type: 'application/yaml' }) };
  }
  location.hash = '#/new';
}

async function donePanel(ctx, status) {
  const { artifacts } = status;
  const report = await health();
  const [result, playbook] = await Promise.all([
    artifacts.report ? fetch(artifactUrl(ctx.id, artifacts.report)).then((r) => (r.ok ? r.json() : null)).catch(() => null) : null,
    fetch(artifactUrl(ctx.id, 'playbook.json')).then((r) => (r.ok ? r.json() : null)).catch(() => null),
  ]);
  const stepName = (id) => playbook?.steps.find((step) => step.id === id)?.do ?? id;

  const parts = [];
  if (artifacts.video) {
    const video = h('video', { class: 'player', controls: true, preload: 'metadata', poster: `/api${runPath(ctx.id)}/poster`, src: artifactUrl(ctx.id, artifacts.video) });
    if (artifacts.captions) video.append(h('track', { kind: 'captions', src: artifactUrl(ctx.id, artifacts.captions), srclang: 'en', label: 'Captions' }));
    const folder = report?.runs_dir ?? 'runs';
    const revealError = errorLine();
    const reveal = h('button', { type: 'button', class: 'link', onclick: () => act(reveal, revealError, () => api(`${runPath(ctx.id)}/reveal`, { method: 'POST' })) }, 'Show in Finder');
    parts.push(
      video,
      h(
        'div',
        { class: 'saved' },
        h('a', { class: 'button primary', href: artifactUrl(ctx.id, artifacts.video), download: '' }, 'Download video'),
        artifacts.captions ? h('a', { class: 'button', href: artifactUrl(ctx.id, artifacts.captions), download: '' }, 'Download captions') : null,
        h('span', {}, 'Saved to ', h('code', {}, `${folder}/${ctx.id}/${artifacts.video}`)),
        reveal,
        revealError,
      ),
    );
  } else {
    parts.push(h('div', { class: 'notice notice-wait' }, 'The run finished but made no video. See its report in the run folder.'));
  }

  // Anything worth checking, from the final report.
  if (result) {
    const notes = [];
    for (const id of result.overrunning_steps ?? []) notes.push(`The narration for “${stepName(id)}” is longer than its footage, so the picture waits for it.`);
    for (const warning of result.audio_warnings ?? []) notes.push(`The voice-over for “${stepName(warning.step_id)}” may not sound as written: it was heard as “${warning.heard}”. Worth a listen.`);
    for (const problem of result.final_check?.problems ?? []) notes.push(`In the finished video, ${problem}.`);
    parts.push(
      notes.length
        ? h('div', { class: 'notice notice-wait' }, h('h3', {}, 'Worth checking'), h('ul', {}, notes.map((note) => h('li', {}, note))))
        : h('div', { class: 'notice notice-done' }, 'Nothing to check: the voice-over matches the script and every step fits its footage.'),
    );
  }

  // Follow-ups
  const briefBox = h('textarea', { rows: 2, placeholder: 'Optional: a new brief, e.g. warmer, and aimed at shop owners' });
  briefBox.id = uid();
  const followError = errorLine();
  const rewrite = h('button', { type: 'button' }, 'Rewrite the narration');
  rewrite.addEventListener('click', () =>
    act(rewrite, followError, async () => {
      await api(`${runPath(ctx.id)}/rerun`, { method: 'POST', json: { from_stage: 'write_story', intent: briefBox.value.trim() || null } });
      await ctx.refresh();
    }),
  );
  const record = h('button', { type: 'button' }, 'Record again');
  record.addEventListener('click', () =>
    act(record, followError, async () => {
      await api(`${runPath(ctx.id)}/rerun`, { method: 'POST', json: { from_stage: 'record_take' } });
      await ctx.refresh();
    }),
  );
  const like = h('button', { type: 'button' }, 'New demo like this');
  like.addEventListener('click', () => act(like, followError, () => startLike(ctx, status)));
  parts.push(
    h('h2', { style: 'margin-top:28px' }, 'Make it better'),
    h(
      'div',
      { class: 'followups' },
      h('div', { class: 'card' }, h('h3', {}, 'New narration'), h('p', {}, 'Write the narration again over the same recording, then voice it. You review it first.'), h('label', { for: briefBox.id, class: 'visually-hidden' }, 'New brief'), briefBox, rewrite),
      h('div', { class: 'card' }, h('h3', {}, 'New recording'), h('p', {}, 'Record the same clicks again, for a cleaner take. The narration is written for the new take, and you review it.'), record),
      h('div', { class: 'card' }, h('h3', {}, 'Another demo'), h('p', {}, 'Start a new demo from this one’s description or playbook, to change it freely.'), like),
    ),
    followError,
  );
  const records = await failuresOf(ctx);
  if (records.length) parts.push(failureList(ctx, records));
  return h('div', { class: 'stack' }, ...parts);
}
