/* Element handles and the DOM helpers every other module builds on.
 *
 * Nothing here knows about showcases, steps or metrics -- it is the layer that
 * paints bubbles, the error strip, the "answered by" readout and the budget
 * bar, and it is deliberately the only module that reaches for elements by id.
 */
'use strict';

import { renderMarkdown, escapeHtml } from './markdown.js';

/* -- chrome ------------------------------------------------------------- */
export const wordmark    = document.getElementById('wordmark');
export const brandProduct = document.getElementById('brand-product');
export const statusEl    = document.getElementById('status');
export const statusText  = document.getElementById('status-text');
export const clearBtn    = document.getElementById('clear');
export const clearCacheBtn = document.getElementById('clear-cache');
export const llmSelect   = document.getElementById('llm-provider');

/* -- control strip ------------------------------------------------------- */
export const routeEl     = document.getElementById('route');
export const selectorBox    = document.getElementById('selector');
export const selectorLabel  = document.getElementById('selector-label');
export const selectorSelect = document.getElementById('selector-select');
export const signinBox   = document.getElementById('signin');
export const signinWho   = document.getElementById('signin-who');
export const signinLink  = document.getElementById('signin-link');
export const budgetBox   = document.getElementById('budget');
export const budgetText  = document.getElementById('budget-text');
export const budgetFill  = document.getElementById('budget-fill');
/* index.html explains the header-driven bar; showSpendMeter() swaps in its own
 * tooltip, so the original is kept to restore. */
const BUDGET_TITLE = budgetBox ? budgetBox.getAttribute('title') || '' : '';
const ROUTE_TITLE = routeEl ? routeEl.getAttribute('title') || '' : '';
export const railToggle  = document.getElementById('rail-toggle');

/* -- screens ------------------------------------------------------------- */
export const screenHome  = document.getElementById('screen-home');
export const screenChat  = document.getElementById('screen-chat');
export const cardsBox    = document.getElementById('cards');
export const scrollHint  = document.getElementById('scroll-hint');
export const noticeBox   = document.getElementById('notice');
export const noticeTitle = document.getElementById('notice-title');
export const noticeBody  = document.getElementById('notice-body');
export const cardWarning      = document.getElementById('card-warning');
export const cardWarningTitle = document.getElementById('card-warning-title');
export const cardWarningBody  = document.getElementById('card-warning-body');
export const cardAbout      = document.getElementById('card-about');
export const cardAboutHow   = document.getElementById('card-about-how');
export const cardAboutSetup = document.getElementById('card-about-setup');

/* -- conversation -------------------------------------------------------- */
export const transcript  = document.getElementById('transcript');
export const intro       = document.getElementById('intro');
export const introHead   = document.getElementById('intro-head');
export const introBody   = document.getElementById('intro-body');
export const introRefs   = document.getElementById('intro-refs');
export const introChips  = document.getElementById('intro-chips');
export const form        = document.getElementById('form');
export const input       = document.getElementById('input');
export const sendBtn     = document.getElementById('send');

/* -- error strip --------------------------------------------------------- */
export const alertBox     = document.getElementById('alert');
export const alertTitle   = document.getElementById('alert-title');
export const alertBody    = document.getElementById('alert-body');
export const alertDetail  = document.getElementById('alert-detail');
export const alertMetrics = document.getElementById('alert-metrics');

/* -- step rail ----------------------------------------------------------- */
export const rail        = document.getElementById('rail');
export const railTitle   = document.getElementById('rail-title');
export const railProgress = document.getElementById('rail-progress');
export const railBar     = document.getElementById('rail-bar');
export const railPrereq  = document.getElementById('rail-prereq');
export const stepsList   = document.getElementById('steps');
export const headersBox  = document.getElementById('gw-headers');

/* ---------- status line ---------- */

/* What setBusy() restores when a turn finishes. Each showcase names its own
 * provider here, so the line reads "via /anthropic-guardrail" rather than a
 * generic string -- on stage that is the reminder that the cards are different
 * providers, not different prompts. */
let idleStatus = 'via AI Gateway';

export function setIdleStatus(text) {
  idleStatus = text || 'via AI Gateway';
  if (!sendBtn.disabled) statusText.textContent = idleStatus;
}

/* ---------- scrolling ---------- */

/* The scrolling element. Was `.canvas`; the chat screen now scrolls the
 * conversation column so the step rail can scroll independently beside it. */
export function scroller() { return document.getElementById('scroll'); }

function nearBottom() {
  const c = scroller();
  if (!c) return true;
  return c.scrollHeight - c.scrollTop - c.clientHeight < 120;
}

export function scrollDown(force) {
  const c = scroller();
  if (c && (force || nearBottom())) c.scrollTop = c.scrollHeight;
}

/* ---------- bubbles ---------- */

export function addBubble(role, html) {
  if (intro && intro.parentNode) intro.remove();
  const el = document.createElement('div');
  el.className = 'msg ' + role;
  el.innerHTML = html;
  transcript.appendChild(el);
  scrollDown(true);
  return el;
}

export function typingHtml() {
  return '<div class="typing"><span></span><span></span><span></span></div>';
}

export function modelFooter(model) {
  return model ? '<p class="msg-model">' + escapeHtml(model) + '</p>' : '';
}

/* An assistant bubble: the answer, the streaming cursor while it is still
 * arriving, and the serving model as a footer. Rebuilt on every delta, so the
 * footer has to be part of the same string rather than a separate append --
 * which is also why per-turn metrics are concatenated once at the end instead
 * of being computed in here. */
/* One MCP tool call, as the gateway answered it: what was called, with what,
 * and whether the gateway let it through. The result the model got is one
 * click away. */
const TOOL_OUTCOMES = {
  ok:           ['✓', 'allowed'],
  tool_error:   ['!', 'the tool failed'],
  refused:      ['⛔', 'refused by the gateway'],
  rate_limited: ['⏱', 'rate limited by the gateway'],
  error:        ['!', 'failed'],
};

/* The tools the gateway offered the agent this turn, one line per MCP server. */
export function toolListHtml(servers) {
  return servers.map((server) => '<p class="tool-list"><span>'
    + escapeHtml(server.name) + ' tools offered by the gateway</span>'
    + (server.tools.length ? server.tools.map((n) => '<code>' + escapeHtml(n) + '</code>').join('') : '<em>none</em>')
    + '</p>').join('');
}

export function toolCallHtml(call) {
  const [icon, label] = TOOL_OUTCOMES[call.outcome] || TOOL_OUTCOMES.error;
  return '<details class="tool-call is-' + escapeHtml(call.outcome || 'error') + '">'
    + '<summary><span class="tool-icon" aria-hidden="true">' + icon + '</span>'
    + '<code>' + escapeHtml(call.name || '?') + '(' + escapeHtml(JSON.stringify(call.arguments || {}))
    + ')</code><span class="tool-status">HTTP ' + escapeHtml(String(call.gatewayStatus))
    + ': ' + label + '</span></summary>'
    + '<pre>' + escapeHtml(call.result || '') + '</pre></details>';
}

export function bubbleHtml(answer, model, streaming) {
  return renderMarkdown(answer)
    + (streaming ? '<span class="cursor"></span>' : '')
    + modelFooter(model);
}

/* ---------- busy state ---------- */

export function setBusy(busy) {
  sendBtn.disabled = busy;
  input.disabled = busy;
  statusEl.classList.toggle('is-busy', busy);
  /* One attribute disables every step button in the rail at once, including
   * any added later, without looping over them. */
  document.body.dataset.busy = busy ? '1' : '';
  if (busy) {
    statusText.textContent = 'asking the gateway...';
  } else {
    statusText.textContent = idleStatus;
    if (!railIsOpen()) input.focus();
  }
}

function railIsOpen() {
  return document.body.dataset.rail === 'open';
}

/* ---------- the error strip ---------- */

export function showError(title, body, detail) {
  alertTitle.textContent = title;
  alertBody.textContent = body;
  alertDetail.innerHTML = detail ? detailHtml(detail) : '';
  alertDetail.hidden = !detail;
  alertBox.hidden = false;
  alertBox.classList.remove('is-refusal');
  statusEl.classList.add('is-error');
  statusText.textContent = 'gateway error';
  scrollDown(true);
}

/* The gateway's own words, readable from the back of the room. A guardrail
 * answers with a JSON body; its reason and assessment are pulled out as plain
 * lines, with the raw body shown open below them. Anything else is shown as is. */
function detailHtml(detail) {
  let body = null;
  try { body = JSON.parse(detail); } catch (e) { /* not JSON */ }
  const message = body && typeof body.message === 'object' ? body.message : null;
  const rows = [];
  if (message && message.actionReason) rows.push(['Reason', message.actionReason]);
  const assessments = message && message.assessments;
  if (typeof assessments === 'string') {
    rows.push(['Detail', assessments]);
  } else if (assessments && Array.isArray(assessments.categories)) {
    // Azure scores each category; the highest one is the reason.
    rows.push(['Severity (0-7)', assessments.categories.slice()
      .sort((a, b) => b.severity - a.severity)
      .map((c) => c.category + ' ' + c.severity).join(', ')]);
  } else if (assessments && (assessments.contentPolicy || assessments.topicPolicy || assessments.wordPolicy)) {
    // Bedrock lists what tripped: content filters, denied topics, blocked words.
    const hits = [
      ...((assessments.contentPolicy || {}).filters || []).map((f) => f.type + ' (' + f.confidence + ')'),
      ...((assessments.topicPolicy || {}).topics || []).map((t) => 'topic ' + t.name),
      ...((assessments.wordPolicy || {}).customWords || []).map((w) => 'word "' + w.match + '"'),
    ];
    rows.push(['Tripped', hits.join(', ') || JSON.stringify(assessments)]);
  } else if (assessments) {
    rows.push(['Detail', JSON.stringify(assessments)]);
  }
  if (!rows.length && body && typeof body.message === 'string') rows.push(['Reason', body.message]);
  const raw = '<pre>' + escapeHtml(detail) + '</pre>';
  if (!rows.length) return raw;
  return '<dl class="verdict">'
    + rows.map(([k, v]) => '<dt>' + k + '</dt><dd>' + escapeHtml(v) + '</dd>').join('')
    + '</dl><details class="raw" open><summary>Raw gateway response</summary>' + raw + '</details>';
}

/* A policy refusing on purpose is not a fault, and must not be painted like
 * one. The guardrail block IS the demonstration -- red would say the demo
 * broke at the exact moment it worked. */
export function showRefusal(title, body, detail) {
  showError(title, body, detail);
  alertBox.classList.add('is-refusal');
  statusEl.classList.remove('is-error');
  statusText.textContent = 'blocked by policy';
}

export function clearError() {
  alertBox.hidden = true;
  alertBox.classList.remove('is-refusal');
  alertMetrics.innerHTML = '';
  alertMetrics.hidden = true;
  statusEl.classList.remove('is-error');
}

/* ---------- the "answered by" readout ---------- */

/* Which model served the reply. Read from the `model` on Anthropic's
 * message_start event -- the gateway's routing decision, observed rather than
 * assumed. The page sends no model of its own, so this is the only place the
 * answer can come from.
 *
 * The dated snapshot suffix is dropped from the readout only. Anthropic answers
 * `claude-haiku-4-5-20251001` for a request that named `claude-haiku-4-5`, and
 * measured at the 780px column that extra 66px pushes the cost meter's figures
 * past the ellipsis beside it. The family is also the form a viewer compares
 * against the step's Expect line -- and the id stays verbatim on the bubble
 * footer and in this readout's tooltip, so nothing is lost. */
export function showRoute(model) {
  if (!model) return;
  routeEl.textContent = dropDate(model);
  routeEl.title = ROUTE_TITLE + ' This reply: ' + model;
  routeEl.removeAttribute('data-idle');
}

export function resetRoute() {
  routeEl.textContent = 'no reply yet';
  routeEl.title = ROUTE_TITLE;
  routeEl.setAttribute('data-idle', 'true');
}

/* ---------- the cost budget ---------- */

/* From the llm-cost-based-ratelimit headers. Both values are strings or null:
 * null means the gateway did not report a balance, which is what happens once
 * it starts refusing with 429 -- so null must read as "unknown", never zero. */
export function showBudget(limit, remaining) {
  const lim = parseFloat(limit);
  const rem = parseFloat(remaining);
  if (!isFinite(lim) || lim <= 0) return;   // policy not reporting; leave as-is

  budgetBox.hidden = false;
  budgetBox.title = BUDGET_TITLE;
  if (!isFinite(rem)) {
    budgetText.textContent = 'budget $' + lim.toFixed(4) + ': spent';
    budgetFill.style.width = '0%';
    budgetBox.className = 'budget is-empty';
    return;
  }
  const share = Math.max(0, Math.min(1, rem / lim));
  budgetText.textContent = '$' + rem.toFixed(4) + ' of $' + lim.toFixed(2) + ' left';
  budgetFill.style.width = (share * 100).toFixed(1) + '%';
  budgetBox.className = 'budget'
    + (share === 0 ? ' is-empty' : share < 0.25 ? ' is-low' : '');
}

/* The same bar, driven by the cost-based-routing card's estimate instead of a
 * header. Dashed, and every figure prefixed "est.": see spend.js for why this
 * one number cannot be observed, and why the step-down is measured even though
 * the balance is not. Red stays reserved for "the gateway said no" -- a
 * step-down is a 200 with an answer in it, so the spent state is amber.
 *
 * The strip is one row wide and this text is nowrap+ellipsis, so it is kept
 * short enough to fit at the narrow end and the full sentence goes in the
 * title. Measured in the browser: the visible text must stay under ~240px, and
 * the model ids lose their `claude-` prefix to buy the room. */
export function showSpendMeter(state) {
  if (!state) return;
  budgetBox.hidden = false;
  const limit = state.limit;
  const share = limit > 0 ? Math.max(0, Math.min(1, state.remaining / limit)) : 0;
  const unpriced = state.unpriced
    ? ' ' + state.unpriced + ' reply(s) had no price entry, so the real spend is'
      + ' higher than this.'
    : '';

  /* Below ~1100px the control strip is holding the provider path and the Steps
   * toggle too, and measurement puts the roomy wording past the ellipsis. The
   * numbers are what must survive, so the model name is what goes. */
  const tight = window.matchMedia('(max-width: 1100px)').matches;

  if (state.exhausted) {
    budgetBox.className = 'budget is-estimated is-stepped';
    budgetText.textContent = tight
      ? 'spent, on ' + shortModel(state.fallbackModel || 'fallback')
      : 'budget spent, serving '
        + shortModel(state.fallbackModel || 'the fallback');
    budgetFill.style.width = '0%';
    budgetBox.title = 'The ' + state.model + ' budget is spent, so the gateway is'
      + ' serving ' + (state.fallbackModel || 'the fallback model') + ' instead'
      + ' -- an answer on a cheaper model rather than a refusal. This part is'
      + ' measured, not estimated: the router only serves the fallback once the'
      + ' budget is gone.';
    return;
  }

  budgetBox.className = 'budget is-estimated' + (share < 0.25 ? ' is-low' : '');
  budgetText.textContent = share === 0
    ? 'est. $' + limit.toFixed(2) + ' of $' + limit.toFixed(2)
      + ' spent' + (tight ? '' : ', steps down next')
    : 'est. $' + state.remaining.toFixed(4) + ' of $' + limit.toFixed(2)
      + ' left' + (tight ? '' : ' on ' + shortModel(state.model));
  budgetFill.style.width = (share * 100).toFixed(1) + '%';
  budgetBox.title = 'Estimated from this session\u2019s token counts and the'
    + ' gateway\u2019s own price file -- the ' + state.model + ' budget ($'
    + limit.toFixed(2) + (state.duration ? ' / ' + state.duration : '')
    + ') publishes no balance. It is shared across callers and comes back over time, so'
    + ' this is a floor on the real spend, not the balance.' + unpriced;
}

/* Model ids are long and the control strip is one row wide. The family is what
 * a viewer needs -- opus against haiku -- so the vendor prefix and the dated
 * snapshot suffix both go. The full id is still on every bubble footer. */
function shortModel(id) {
  return dropDate(id).replace(/^claude-/, '');
}

function dropDate(id) {
  return String(id || '').replace(/-\d{8}$/, '');
}

export function markBudgetExhausted() {
  budgetBox.hidden = false;
  budgetBox.title = BUDGET_TITLE;
  budgetBox.className = 'budget is-empty';
  budgetText.textContent = 'budget exhausted';
  budgetFill.style.width = '0%';
}

/* Each showcase is a different LLM provider, and llm-cost-based-ratelimit
 * counts per provider. Carrying a balance across a showcase switch would show
 * one provider's budget on another provider's screen -- so this runs on a
 * switch, but deliberately NOT on Clear chat, where the provider is unchanged. */
export function resetBudget() {
  budgetBox.hidden = true;
  budgetBox.className = 'budget';
  budgetBox.title = BUDGET_TITLE;
  budgetText.textContent = '';
  budgetFill.style.width = '100%';
}
