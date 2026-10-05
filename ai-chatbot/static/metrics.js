/* Per-turn measurements, and how they are painted.
 *
 * Everything here is observed from the response, never asserted: the served
 * model comes off message_start, the token counts off Anthropic's own usage
 * fields, the latency off the client clock. That discipline is the point --
 * the demo is claiming the gateway did something, so the evidence has to come
 * back from the gateway rather than from what the page hoped for.
 */
'use strict';

import { escapeHtml } from './markdown.js';
import { money } from './spend.js';

export const turns = [];

let emphasis = [];

export function setEmphasis(list) {
  emphasis = Array.isArray(list) ? list : [];
}

export function resetTurns() {
  turns.length = 0;
}

export function beginTurn(question, origin) {
  return {
    n: turns.length + 1,
    question,
    stepN:     origin ? origin.stepN : null,
    sendIndex: origin ? origin.sendIndex : 0,

    /* performance.now(), not Date.now(): monotonic, and unaffected by the
     * clock being moved -- which the time-based-routing demo does on purpose. */
    t0: performance.now(),
    ttft: null,          // to the first text_delta
    elapsed: null,

    model: null,
    inputTokens: null,
    outputTokens: null,
    stopReason: null,
    answer: '',

    costLimit: null,
    costRemaining: null,
    cost: null,
    headers: {},
    streamed: true,
    cacheHit: null,      // true | null -- never false; see cacheState()

    outcome: 'pending',  // ok | empty | blocked | refused | error | transport | aborted
    gatewayStatus: null,
  };
}

export function finishTurn(turn) {
  turn.elapsed = performance.now() - turn.t0;
  turns.push(turn);
  return turn;
}

export function previousTurn() {
  return turns.length ? turns[turns.length - 1] : null;
}

/* ---------- the cache verdict ----------
 *
 * semantic-cache sets X-Cache-Status on the response, verified on this gateway,
 * so when that header is present the answer is measured rather than guessed:
 *
 *   'hit'     the header said HIT. Solid badge.
 *   'miss'    the header said something else. Also measured -- and the miss is
 *             half the story, because the demo is miss-then-hit.
 *   'guess'   no header, but suspiciously fast with no output tokens. Dashed
 *             badge whose own text says "inferred", because a tooltip is
 *             invisible on a projector.
 *   null      no header and nothing suspicious. Renders nothing.
 *
 * A miss is only ever claimed from the header. Absence of evidence is still not
 * evidence of a miss, so the latency path can never produce one.
 */
export function cacheState(turn) {
  if (turn.cacheHit === true) return 'hit';
  if (turn.cacheHit === false) return 'miss';
  if (turn.streamed && turn.ttft !== null && turn.ttft < 250 && !turn.outputTokens) {
    return 'guess';
  }
  return null;
}

/* ---------- chips ---------- */

function ms(value) {
  if (value === null || value === undefined) return null;
  return value < 1000 ? Math.round(value) + ' ms' : (value / 1000).toFixed(1) + ' s';
}

function chip(text, kind) {
  return '<span class="metric' + (kind ? ' ' + kind : '') + '">'
       + escapeHtml(text) + '</span>';
}

/* Which chips this showcase leans on. Routing wants the model and the clock;
 * the guardrail wants "blocked" and a token count of zero; the spend limit
 * wants the budget moving. */
function emphasise(key) {
  return emphasis.indexOf(key) !== -1 ? 'is-emphasis' : '';
}

export function metricsHtml(turn) {
  const parts = [];

  if (turn.outcome === 'blocked') {
    parts.push(chip('blocked before the model', 'is-good'));
  } else if (turn.outcome === 'refused') {
    parts.push(chip('refused · budget exhausted', 'is-warn'));
  }

  const cache = cacheState(turn);
  if (cache === 'hit') parts.push(chip('cache hit · no model call', 'is-good'));
  if (cache === 'miss') parts.push(chip('cache miss', emphasise('cache')));
  if (cache === 'guess') parts.push(chip('cache hit? inferred from latency', 'is-guess'));

  if (turn.streamed && turn.ttft !== null) {
    parts.push(chip(ms(turn.ttft) + ' to first token', emphasise('latency')));
  }
  if (turn.elapsed !== null) {
    parts.push(chip(ms(turn.elapsed) + ' total'
                    + (turn.streamed ? '' : ' (not streamed)'), emphasise('latency')));
  }

  /* A cache hit replays the STORED response, and that body carries the usage
   * of the call that first filled the cache -- measured on this gateway: 14 in
   * / 200 out on a hit whose own request carried a ~1200-token catalogue. Those
   * numbers describe a different call, so showing them here would read as this
   * turn having billed them. It did not: a hit comes back with x-cache-status
   * and no cost headers at all, the model never being reached. */
  const inTok = turn.inputTokens, outTok = turn.outputTokens;
  if (cache === 'hit') {
    parts.push(chip('nothing billed this turn', emphasise('tokens') || 'is-good'));
  } else if (inTok !== null || outTok !== null) {
    /* Zero is the whole point on the guardrail card, so a real 0 must render
     * rather than being falsy-skipped. */
    parts.push(chip((inTok === null ? '?' : inTok) + ' in / '
                  + (outTok === null ? '?' : outTok) + ' out tokens',
                    emphasise('tokens')));
  } else if (turn.outcome === 'blocked') {
    parts.push(chip('0 tokens · the model was never called', 'is-emphasis'));
  }

  /* The step-down IS the cost-based-routing card, and it is measured: the
   * router only serves the fallback once the deep model's budget is gone. A
   * 200 with a cheaper answer in it, so it reads as good news, not a fault. */
  if (turn.spend && turn.spend.justStepped) {
    parts.push(chip('stepped down \u00b7 the ' + turn.spend.model
                    + ' budget is spent', 'is-good'));
  }

  /* No cost chip on a turn the model never served. The gateway may still send
   * an x-llm-cost header on a 400 -- reporting it here would put a price on a
   * call that never happened, in the one beat whose entire claim is that no
   * call happened. */
  if (turn.outcome !== 'blocked' && cache !== 'hit') {
    if (turn.cost) {
      parts.push(chip('$' + turn.cost + ' this call', emphasise('cost')));
    } else if (turn.budgetDelta) {
      parts.push(chip('budget \u2212$' + turn.budgetDelta.toFixed(4), emphasise('budget')));
    } else if (turn.costEstimate) {
      /* Computed here rather than read off a header -- see spend.js. The "~"
       * is not decoration: this lane publishes no cost at all. */
      parts.push(chip('~' + money(turn.costEstimate) + ' this call',
                      emphasise('cost')));
    }
  }

  if (turn.stopReason && turn.stopReason !== 'end_turn') {
    parts.push(chip('stop: ' + turn.stopReason, 'is-warn'));
  }

  return parts.length ? '<div class="metrics">' + parts.join('') + '</div>' : '';
}

/* The gateway's raw x-* headers, for the rail's disclosure.
 *
 * These are gateway-influenced strings and a guardrail assessment can quote
 * the visitor's own prompt back, so every name and value is escaped. The app
 * builds bubbles with innerHTML; an unescaped header value here would be
 * reflected XSS delivered by our own gateway. */
export function headersHtml(headers) {
  const names = Object.keys(headers || {}).sort();
  if (!names.length) return '';
  return names.map((name) =>
    '<div><b>' + escapeHtml(name) + '</b>: ' + escapeHtml(headers[name]) + '</div>'
  ).join('');
}
