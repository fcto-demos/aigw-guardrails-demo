/* The step rail: the demo script, rendered.
 *
 * Built once when a showcase mounts. State changes flip attributes and never
 * rebuild markup -- a rail that re-rendered would lose its scroll position and
 * the presenter's place in it halfway through a run.
 */
'use strict';

import { escapeHtml } from './markdown.js';
import { rail, railTitle, railPrereq, stepsList, headersBox } from './dom.js';
import { modelMatches } from './models.js';
import { cacheState, headersHtml, turns } from './metrics.js';
import { modelForRole } from './screens.js';

/* expect.modelRole ("small"/"large") -> the active provider's actual model
 * id for that role; expect.model (a literal id) passes straight through.
 * See screens.js's modelForRole() for where the mapping comes from. */
function resolveExpectModel(expect) {
  if (expect.modelRole) return modelForRole(expect.modelRole) || expect.model || null;
  return expect.model || null;
}

/* stepN -> { state, sends: [{ state, verdict, turn }] } */
export const stepState = new Map();

let steps = [];

const VERDICT = {
  pending: '',
  running: 'running…',
  passed: 'passed',
  failed: 'failed',
  done: 'ran',
};

/* ---------- building ---------- */

export function buildRail(showcase) {
  steps = showcase.steps || [];
  stepState.clear();
  headersBox.hidden = true;
  headersBox.querySelector('.gw-headers-body').innerHTML = '';

  document.body.dataset.rail = steps.length ? '' : 'none';
  railTitle.textContent = steps.length ? 'Demo steps' : '';

  railPrereq.innerHTML = prereqHtml(showcase);
  stepsList.innerHTML = steps.map(stepHtml).join('');

  for (const step of steps) {
    stepState.set(step.n, {
      state: 'pending',
      sends: (step.sends || []).map(() => ({ state: 'pending', verdict: '', turn: null })),
    });
  }
}

function prereqHtml(showcase) {
  const items = showcase.prerequisites || [];
  if (!items.length) return '';
  return '<h3>Before you start</h3><ul>'
    + items.map((p) => '<li>' + escapeHtml(p.text || '')
        + (p.command ? commandHtml(p.command) : '') + '</li>').join('')
    + '</ul>';
}

function commandHtml(command) {
  return '<pre><code>' + escapeHtml(command) + '</code></pre>'
       + '<button type="button" class="copy-btn" data-copy="'
       + escapeHtml(command) + '">Copy</button>';
}

function stepHtml(step) {
  const op = step.operator
    ? '<div class="step-op"><p>' + escapeHtml(step.operator.text || '') + '</p>'
      + (step.operator.command ? commandHtml(step.operator.command) : '')
      + '</div>'
    : '';

  const sends = (step.sends || []).map((send, i) =>
    '<div class="step-send" data-send="' + i + '" data-state="pending">'
    + '<p class="step-q">' + escapeHtml(send.ask || '') + '</p>'
    + '<button type="button" class="copy-btn step-copy" data-copy="'
    + escapeHtml(send.ask || '') + '">Copy prompt</button>'
    + (send.expect && send.expect.text
        ? '<p class="step-expect"><b>Expect</b> ' + escapeHtml(send.expect.text) + '</p>'
        : '')
    + '<p class="step-result"></p>'
    + '</div>').join('');

  const contains = step.contains
    ? '<details class="step-check"><summary>Correct answer contains</summary><p>'
      + escapeHtml(step.contains) + '</p></details>'
    : '';

  return '<li class="step" data-step="' + step.n + '" data-state="pending">'
    + '<div class="step-head">'
    + '<span class="step-n">' + step.n + '</span>'
    + '<h3 class="step-title">' + escapeHtml(step.title || '') + '</h3>'
    + '<span class="step-verdict" aria-live="polite"></span>'
    + '</div>'
    + op + sends + contains
    + '</li>';
}

/* ---------- state ---------- */

function stepFor(n) { return steps.find((s) => s.n === n); }

/* Which step a question belongs to.
 *
 * The rail hands out prompts to copy rather than sending them, so a turn
 * arrives through the composer with no idea which step it came from. Matching
 * on the question text puts it back, which is what keeps the Expect lines and
 * the differsFrom / fasterThan checks working at all.
 *
 * Prefer a slot that has not run yet: time-based routing asks the *identical*
 * question in two steps on purpose, and the second ask has to land on the
 * second step rather than re-grading the first. */
export function findOrigin(question) {
  const wanted = (question || '').trim().toLowerCase();
  if (!wanted) return null;

  const matches = [];
  for (const step of steps) {
    (step.sends || []).forEach((send, sendIndex) => {
      if ((send.ask || '').trim().toLowerCase() === wanted) {
        matches.push({ stepN: step.n, sendIndex });
      }
    });
  }
  if (!matches.length) return null;

  const pending = matches.find((m) => {
    const state = stepState.get(m.stepN);
    const slot = state && state.sends[m.sendIndex];
    return slot && slot.state === 'pending';
  });
  return pending || matches[0];
}

export function sendFor(n, index) {
  const step = stepFor(n);
  return step && step.sends ? step.sends[index] : null;
}

export function markRunning(origin) {
  if (!origin) return;
  const state = stepState.get(origin.stepN);
  if (!state) return;
  state.state = 'running';
  if (state.sends[origin.sendIndex]) {
    state.sends[origin.sendIndex].state = 'running';
    state.sends[origin.sendIndex].verdict = '';
  }
  renderStep(origin.stepN);
}

export function recordTurn(origin, turn) {
  if (!origin) return;
  const state = stepState.get(origin.stepN);
  if (!state) return;
  const send = sendFor(origin.stepN, origin.sendIndex);
  const slot = state.sends[origin.sendIndex];
  if (!slot) return;

  const verdict = evaluateExpect(send && send.expect, turn);
  slot.turn = turn;
  slot.verdict = verdict.text;
  slot.state = verdict.ok === true ? 'passed'
             : verdict.ok === false ? 'failed' : 'done';

  /* The step is only as good as its worst send, and it stays "running" while
   * any send has not been fired. */
  const states = state.sends.map((s) => s.state);
  state.state = states.indexOf('failed') !== -1 ? 'failed'
              : states.indexOf('pending') !== -1 ? 'running'
              : states.indexOf('done') !== -1 ? 'done'
              : 'passed';
  renderStep(origin.stepN);
}

function renderStep(n) {
  const li = stepsList.querySelector('.step[data-step="' + n + '"]');
  const state = stepState.get(n);
  if (!li || !state) return;
  li.dataset.state = state.state;
  li.querySelector('.step-verdict').textContent = VERDICT[state.state] || '';
  state.sends.forEach((slot, i) => {
    const row = li.querySelector('.step-send[data-send="' + i + '"]');
    if (!row) return;
    row.dataset.state = slot.state;
    row.querySelector('.step-result').textContent = slot.verdict || '';
  });
}

/* ---------- expectations ----------
 *
 * `ok: null` is a deliberate third answer: the step ran, and the app is not
 * going to pretend it graded something it never measured. "Correct answer
 * contains Spain as Zone A" needs another model call to check; a green tick
 * that really means "it ran" is worse than no tick, because on stage it will
 * be believed.
 */
const OUTCOME_TEXT = {
  blocked: 'blocked by a gateway policy — the model was never called',
  refused: 'refused — the cost budget is exhausted',
  error: 'the gateway returned an error',
  transport: 'could not reach the chatbot server',
  empty: 'the gateway returned no text',
  aborted: 'cancelled',
};

export function evaluateExpect(expect, turn) {
  /* A step whose expectation is prose has nothing to grade -- but if the turn
   * did not actually succeed, saying nothing would read as "nothing happened".
   * An unexpected refusal is the most useful thing the rail can report. */
  const unexpected = !expect || !(expect.blocked || expect.gatewayStatus);
  if (unexpected && turn.outcome !== 'ok') {
    return { ok: false, text: OUTCOME_TEXT[turn.outcome] || turn.outcome };
  }

  /* No expectation declared at all -- the cost-routing steps are deliberately
   * like this, because which send crosses the budget depends on what the lane
   * already spent this hour. Report what served and grade nothing: the same
   * answer as a step whose expectation is unmeasurable prose, and for the same
   * reason. */
  if (!expect) return ran(turn);

  if (expect.blocked) {
    return turn.outcome === 'blocked' && !turn.outputTokens
      ? { ok: true, text: 'blocked at the gateway · 0 tokens · the model was never called' }
      : { ok: false, text: 'not blocked — outcome was ' + turn.outcome };
  }

  if (expect.gatewayStatus) {
    return turn.gatewayStatus === expect.gatewayStatus
      ? { ok: true, text: 'gateway returned ' + turn.gatewayStatus + ', as expected' }
      : { ok: false, text: 'gateway returned '
                         + (turn.gatewayStatus || turn.outcome)
                         + ', expected ' + expect.gatewayStatus };
  }

  /* modelRole ("small"/"large") is what a step declares -- resolveExpectModel
   * turns that into whichever provider is currently active's actual model
   * id, so the same step grades correctly under any provider. expect.model
   * (a literal id) still works too, for a step that genuinely means one
   * specific model regardless of provider. */
  const expectModel = resolveExpectModel(expect);
  if (expectModel) {
    return modelMatches(turn.model, expectModel)
      ? { ok: true, text: 'served by ' + turn.model }
      : { ok: false, text: 'served by ' + (turn.model || 'nothing')
                         + ', expected ' + expectModel };
  }

  if (expect.differsFrom) {
    const other = turnOf(expect.differsFrom.step, expect.differsFrom.send || 0);
    if (!other) return { ok: null, text: 'nothing to compare against yet' };
    return other.answer.trim() === turn.answer.trim()
      ? { ok: false, text: 'identical to the previous answer — the similarity '
                         + 'threshold is too loose; raise it and redeploy' }
      : { ok: true, text: 'a different answer, as it should be' };
  }

  if (expect.fasterThan) {
    const other = turnOf(expect.fasterThan.step, expect.fasterThan.send || 0);
    if (!other || !other.elapsed) return { ok: null, text: 'no baseline recorded yet' };
    const factor = other.elapsed / turn.elapsed;
    return factor >= (expect.fasterThan.factor || 2)
      ? { ok: true, text: factor.toFixed(1) + '× faster than step '
                        + expect.fasterThan.step }
      : { ok: false, text: 'only ' + factor.toFixed(1) + '× faster than step '
                         + expect.fasterThan.step + ' — that is not a cache hit' };
  }

  /* X-Cache-Status makes this a real measurement, so it grades in both
   * directions. `cache: false` is the priming step asserting a MISS -- on a
   * cache still warm from a rehearsal that step hits too, which silently makes
   * the next step's speed comparison meaningless. Better to say so here than
   * let it read as a failure of the cache. Only the latency inference stays
   * ungraded: a guess must not turn into a green tick. */
  if (expect.cache !== undefined) {
    const state = cacheState(turn);
    if (state === 'guess') {
      return { ok: null, text: 'fast enough to look like a hit, but no cache '
                             + 'header was seen' };
    }
    if (state === null) {
      return { ok: null, text: 'no cache signal in the response' };
    }
    const hit = state === 'hit';
    if (hit === Boolean(expect.cache)) {
      return { ok: true, text: hit
        ? 'served from cache · the model was never called'
        : 'cache miss, as expected — the model answered and filled the cache' };
    }
    return hit
      ? { ok: false, text: 'already cached — this should have been a miss. The '
                         + 'cache is still warm from an earlier run; flush Redis '
                         + 'or wait out the hour, or the next step proves nothing' }
      : { ok: false, text: 'cache miss — the gateway called the model. Prime it '
                         + 'with the identical wording first' };
  }

  /* Nothing checkable was declared. The step ran; the app is not going to
   * pretend it graded a claim it never measured. */
  return ran(turn);
}

function ran(turn) {
  return { ok: null, text: turn.model ? 'ran · served by ' + turn.model : 'ran' };
}

function turnOf(stepN, sendIndex) {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    if (turns[i].stepN === stepN && turns[i].sendIndex === sendIndex
        && turns[i].answer) {
      return turns[i];
    }
  }
  return null;
}

/* ---------- gateway headers disclosure ---------- */

export function showHeaders(headers) {
  const html = headersHtml(headers);
  headersBox.hidden = !html;
  headersBox.querySelector('.gw-headers-body').innerHTML = html;
}

/* ---------- wiring ---------- */

export function railIsOverlay() {
  return window.matchMedia('(max-width: 1023px)').matches;
}

export function openRail() {
  document.body.dataset.rail = 'open';
  document.getElementById('rail-toggle').setAttribute('aria-expanded', 'true');
}

export function closeRail() {
  if (document.body.dataset.rail === 'open') document.body.dataset.rail = '';
  document.getElementById('rail-toggle').setAttribute('aria-expanded', 'false');
}

rail.addEventListener('click', (e) => {
  const copy = e.target.closest('.copy-btn');
  if (copy) copyPrompt(copy);
});

/* Copying is now the only way a prompt gets out of the rail, so it must not
 * have a single point of failure. navigator.clipboard needs a secure context
 * and transient user activation and can still be refused; the execCommand
 * fallback is deprecated but works where the async API will not. If both fail
 * the text is selected instead, so the presenter can hit Cmd-C rather than
 * retype a 200-character question in front of an audience. */
function copyPrompt(button) {
  const text = button.dataset.copy || '';
  const original = button.dataset.label || button.textContent;
  button.dataset.label = original;

  const settle = (message) => {
    button.textContent = message;
    setTimeout(() => { button.textContent = original; }, 1500);
  };

  const legacy = () => {
    const scratch = document.createElement('textarea');
    scratch.value = text;
    scratch.setAttribute('readonly', '');
    scratch.style.cssText = 'position:fixed;top:-1000px;opacity:0';
    document.body.appendChild(scratch);
    scratch.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    scratch.remove();
    return ok;
  };

  const selectInstead = () => {
    const q = button.parentNode.querySelector('.step-q')
           || button.parentNode.querySelector('pre');
    if (!q) { settle('Copy failed'); return; }
    const range = document.createRange();
    range.selectNodeContents(q);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    settle('Selected — press \u2318C');
  };

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text)
      .then(() => settle('Copied'))
      .catch(() => { if (legacy()) settle('Copied'); else selectInstead(); });
    return;
  }
  if (legacy()) settle('Copied'); else selectInstead();
}

document.getElementById('rail-close').addEventListener('click', closeRail);
document.getElementById('rail-toggle').addEventListener('click', () => {
  if (document.body.dataset.rail === 'open') closeRail(); else openRail();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeRail();
});
