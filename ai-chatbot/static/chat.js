/* One turn: post the conversation, read the gateway's SSE, paint as it arrives.
 *
 * The failure contract here is load-bearing and predates the showcase work: on
 * any gateway failure BOTH bubbles come back down and the pending user message
 * is dropped from history, because Anthropic rejects two consecutive user
 * turns -- a question with no answer beside it cannot be carried forward.
 */
'use strict';

import { renderMarkdown, escapeHtml } from './markdown.js';
import {
  transcript, intro, form, input, sendBtn, clearBtn, clearCacheBtn,
  alertBox, alertMetrics, addBubble, typingHtml, bubbleHtml, toolListHtml, toolCallHtml, setBusy,
  showError, showRefusal, clearError, showRoute, showBudget, selectorBox, selectorSelect,
  markBudgetExhausted, showSpendMeter, scrollDown, resetRoute, signinBox,
} from './dom.js';
import { activeId, activeShowcase, activeProvider, onSessionReset } from './screens.js';
import {
  beginTurn, finishTurn, previousTurn, metricsHtml, turns, resetTurns,
} from './metrics.js';
import { markRunning, recordTurn, showHeaders, findOrigin } from './rail.js';
import { recordSpend } from './spend.js';

/* The conversation. Text only: thinking blocks and their signatures are never
 * stored and never sent back, which is what keeps multi-turn safe here. */
let messages = [];
let inFlight = null;

/* Sent with every turn; MCP cards forward it to the gateway, where the rate
 * limit counts per conversation. A new conversation gets a new id. */
let conversation = crypto.randomUUID();

export async function ask(question, origin) {
  if (!activeShowcase) return;

  /* The rail hands out prompts to copy, so a step's question arrives through
   * the composer like any other. Match it back to its step so the Expect line
   * and the verdict still work. */
  if (!origin) origin = findOrigin(question);

  /* A disabled showcase (opened from a direct link) never sends. */
  if (activeShowcase.status === 'disabled') {
    showError('This showcase is disabled',
              (activeShowcase.blockedBy || []).join(' ') || (activeShowcase.title || activeId) + ' is disabled.',
              '');
    return;
  }

  /* An unwired showcase is refused here rather than at the gateway: there is
   * no provider to call, and the reason is more useful than a 503 would be. */
  if (!activeShowcase.configured) {
    showError('No provider wired for this showcase',
              (activeShowcase.unwiredReason || 'no provider configured')
              + ' -- ' + (activeShowcase.title || activeId) + ' has no gateway provider for '
              + activeProvider + '.',
              '');
    return;
  }

  clearError();
  const userBubble = addBubble('user', renderMarkdown(question));
  messages.push({ role: 'user', content: question });

  const bubble = addBubble('assistant', typingHtml());
  setBusy(true);
  markRunning(origin);

  let answer = '';
  let tools = '';   // MCP cards: one line per tool call, above the answer
  let servedModel = null;
  let stopReason = null;
  let failed = false;
  let aborted = false;
  inFlight = new AbortController();
  const turn = beginTurn(question, origin);
  const before = previousTurn();

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      /* A showcase *id*, never a URL. The server owns the id-to-provider
       * mapping; see the comment at the lookup in server.py. */
      body: JSON.stringify({ messages, showcase: activeId, provider: activeProvider, conversation,
                             choice: selectorBox.hidden ? undefined : selectorSelect.value }),
      signal: inFlight.signal,
    });

    if (!response.ok) {
      let payload = {};
      try { payload = await response.json(); } catch (e) { /* non-JSON body */ }
      failed = true;

      /* server.py wraps the gateway's status in its own, so the gateway's real
       * verdict is in the payload -- never in response.status. */
      turn.gatewayStatus = payload.gatewayStatus || null;
      turn.headers = payload.headers || {};
      turn.cost = payload.cost || null;
      /* A guardrail's assessment header rides on the 400, so this is the one
       * response the disclosure most needs to show. */
      showHeaders(turn.headers);
      /* A guardrail on the answer steps in after the model has replied, so the
       * tokens were spent; one on the request stops the call before it. */
      let verdict = {};
      try { verdict = JSON.parse(payload.gatewayBody).message || {}; }
      catch (e) { /* not a guardrail body */ }
      turn.afterModel = verdict.direction === 'RESPONSE';
      turn.inputTokens = turn.afterModel ? null : 0;
      turn.outputTokens = turn.afterModel ? null : 0;

      const outcome = payload.gatewayOutcome;
      turn.outcome = outcome === 'policy_block' || outcome === 'signin_required' ? 'blocked'
                   : outcome === 'budget_exhausted' ? 'refused'
                   : 'error';

      const status = payload.gatewayStatus
        ? ' (gateway returned HTTP ' + payload.gatewayStatus + ')'
        : '';
      /* A card's own limit (a token quota) is not the cost budget. */
      if (turn.outcome === 'refused' && !payload.limitTitle) markBudgetExhausted();
      else showBudget(payload.costLimit, payload.costRemaining);

      if (outcome === 'signin_required') {
        /* The gateway's 401 is the cue any MCP client acts on: sign the user in. */
        showRefusal('Sign-in required',
                    (payload.error || 'This MCP server needs a signed-in user.') + status
                    + '. Use Sign in, above.',
                    payload.gatewayBody || '');
        signinBox.classList.add('is-attention');
      } else if (turn.outcome === 'blocked') {
        /* The block IS the demonstration, so it must not be painted as a
         * fault. The metrics strip carries the evidence: zero tokens.
         * Guardrails name themselves in the body, which tells apart two
         * guardrails on the same lane. */
        const guard = verdict.interveningGuardrail || '';
        showRefusal('Blocked by a gateway policy' + (guard ? ': ' + guard : ''),
                    (payload.error || 'The request was refused.') + status,
                    payload.gatewayBody || '');
      } else if (turn.outcome === 'refused') {
        showError(payload.limitTitle || 'Cost budget exhausted',
                  (payload.error || 'The request failed.') + status,
                  payload.gatewayBody || '');
      } else {
        showError('Gateway error',
                  (payload.error || 'The request failed.') + status,
                  payload.gatewayBody || payload.detail || payload.hint || '');
      }
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      /* SSE events are separated by a blank line. Keep the tail: it may be a
       * partially received event. */
      const chunks = buffer.split(/\r?\n\r?\n/);
      buffer = chunks.pop();

      for (const chunk of chunks) {
        let data = '';
        let name = 'message';
        for (const line of chunk.split(/\r?\n/)) {
          if (line.startsWith('event:')) name = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trim();
        }
        if (!data) continue;

        let event;
        try { event = JSON.parse(data); } catch (e) { continue; }

        if (name === 'gateway_meta') {
          showBudget(event.costLimit, event.costRemaining);
          turn.costLimit = event.costLimit;
          turn.costRemaining = event.costRemaining;
          turn.cost = event.cost || null;
          turn.headers = event.headers || {};
          turn.cacheHit = event.cacheHit;
          turn.streamed = event.streamed !== false;
          showHeaders(turn.headers);
          /* The budget delta is only meaningful against the previous turn on
           * the SAME provider, which a showcase switch resets. */
          const prev = before && parseFloat(before.costRemaining);
          const now = parseFloat(event.costRemaining);
          if (isFinite(prev) && isFinite(now) && prev > now) {
            turn.budgetDelta = prev - now;
          }
          continue;
        }
        if (name === 'tool_list') {
          tools += toolListHtml(event.servers || []);
          bubble.innerHTML = tools + typingHtml();
          continue;
        }
        if (name === 'tool_call') {
          tools += toolCallHtml(event);
          bubble.innerHTML = tools + typingHtml();
          continue;
        }
        if (name === 'gateway_error') {
          failed = true;
          turn.outcome = 'error';
          showError('Gateway error', event.error || 'Unparseable response.',
                    event.body || '');
          continue;
        }

        /* Anthropic can abort a stream part-way with its own error event --
         * an overloaded_error under load, for instance. Without this the turn
         * would end with an empty answer and a bland "no text" notice, hiding
         * a transient fault the user could simply retry. */
        if (event.type === 'error') {
          failed = true;
          turn.outcome = 'error';
          const err = event.error || {};
          showError('Upstream error',
                    err.message || 'The model stopped mid-response.',
                    err.type ? 'type: ' + err.type : '');
          continue;
        }

        /* The gateway's routing decision, as served. It arrives before the
         * first token, so the readout updates while the dots are still up. */
        if (event.type === 'message_start' && event.message) {
          servedModel = event.message.model || servedModel;
          turn.model = servedModel;
          const usage = event.message.usage || {};
          turn.inputTokens = usage.input_tokens === undefined
            ? null : usage.input_tokens;
          showRoute(servedModel);
          bubble.innerHTML = tools + typingHtml()
            + (servedModel ? '<p class="msg-model">' + escapeHtml(servedModel)
                             + '</p>' : '');
          continue;
        }

        /* Only text_delta paints. A reasoning model also emits thinking_delta
         * and signature_delta through this gateway; those are reasoning, not
         * an answer, and must never reach the bubble. The dots stay up
         * meanwhile -- and the deep route is exactly where they show up. */
        if (event.type === 'content_block_delta' && event.delta) {
          if (event.delta.type === 'text_delta') {
            if (turn.ttft === null) turn.ttft = performance.now() - turn.t0;
            answer += event.delta.text || '';
            bubble.innerHTML = tools + bubbleHtml(answer, servedModel, true);
            scrollDown(false);
          }
        } else if (event.type === 'message_delta') {
          if (event.delta) stopReason = event.delta.stop_reason || stopReason;
          /* usage sits at the TOP level of message_delta, not inside delta.
           * server.py's non-streaming fallback synthesises it in the same
           * position, so this one read covers both paths. */
          if (event.usage && event.usage.output_tokens !== undefined) {
            turn.outputTokens = event.usage.output_tokens;
          }
          /* Anthropic reports input_tokens immediately, in message_start --
           * OpenAI-compatible and Gemini wires only know it once the stream
           * finishes (server.py's _build_upstream_openai/_gemini send
           * message_start with usage: {}), so message_start alone left this
           * showing "?" for every non-Anthropic provider. Backfill it here,
           * the same way output_tokens already is. */
          if (event.usage && event.usage.input_tokens !== undefined
              && event.usage.input_tokens !== null) {
            turn.inputTokens = event.usage.input_tokens;
          }
        }
      }
    }

    if (answer.trim()) {
      turn.answer = answer;
      turn.outcome = 'ok';
      /* Cost-based routing publishes no balance, so its meter is folded in
       * here from this turn's tokens -- and the step-down it detects is what
       * the card exists to show. A no-op on every other showcase. */
      showSpendMeter(recordSpend(turn));
      bubble.innerHTML = tools + bubbleHtml(answer, servedModel, false)
                       + metricsHtml(finishSnapshot(turn));
      messages.push({ role: 'assistant', content: answer });
    } else if (!failed) {
      /* Verified against this gateway: given a small max_tokens, a reasoning
       * model can spend the whole budget on internal reasoning, returning no
       * text at all. The question stays on screen so the notice reads in
       * context, but its history entry goes: Anthropic rejects two user turns
       * in a row, so a user message with no answer beside it cannot be
       * carried forward. */
      turn.outcome = 'empty';
      dropPendingQuestion();
      bubble.innerHTML = tools + '<p class="notice">' + (stopReason === 'max_tokens'
        ? 'The model hit its token limit before writing an answer. Raise MAX_TOKENS in config.env and try again.'
        : 'The gateway returned no text for that request.') + '</p>';
    }
  } catch (error) {
    if (error.name === 'AbortError') {
      aborted = true;
      turn.outcome = 'aborted';
    } else {
      failed = true;
      turn.outcome = 'transport';
      showError('Connection lost',
                'Could not reach the chatbot server. Is server.py still running?',
                String(error));
    }
  } finally {
    turn.stopReason = stopReason;
    if (turn.elapsed === null) finishTurn(turn);
    recordTurn(origin, turn);

    /* A blocked turn has no bubble to hang metrics on -- both were removed --
     * so the evidence goes into the error strip instead. On the guardrail card
     * that strip is the entire payload. */
    if (!alertBox.hidden) {
      const html = metricsHtml(turn);
      alertMetrics.innerHTML = html;
      alertMetrics.hidden = !html;
    }

    if (failed || aborted) {
      /* The turn never happened. Take both bubbles back down so the transcript
       * matches the history that will actually be sent next time; the error
       * strip is what explains the gap. */
      bubble.remove();
      userBubble.remove();
      dropPendingQuestion();
      if (!transcript.querySelector('.msg')) transcript.appendChild(intro);
      if (failed) {
        input.value = question;   // hand the question back rather than lose it
        autoGrow();
      }
    }
    inFlight = null;
    setBusy(false);
    scrollDown(false);
  }
}

/* finishTurn() records elapsed and pushes the turn; the bubble needs those
 * numbers before it is painted, and the finally block must not push twice. */
function finishSnapshot(turn) {
  return finishTurn(turn);
}

/* Remove the trailing user turn, if it is still unanswered. */
function dropPendingQuestion() {
  if (messages.length && messages[messages.length - 1].role === 'user') {
    messages.pop();
  }
}

/* ---------- composer ---------- */

function autoGrow() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 168) + 'px';
}

function submit() {
  const question = input.value.trim();
  if (!question || sendBtn.disabled) return;
  input.value = '';
  autoGrow();
  ask(question, null);
}

form.addEventListener('submit', (e) => { e.preventDefault(); submit(); });
input.addEventListener('input', autoGrow);
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
});

/* Delegated on #transcript so chips keep working across #intro being detached
 * and reattached. */
transcript.addEventListener('click', (e) => {
  const chip = e.target.closest('.chip');
  if (chip && !sendBtn.disabled) {
    ask(chip.dataset.ask || chip.textContent.trim(), null);
  }
});

document.getElementById('alert-close').addEventListener('click', clearError);

clearBtn.addEventListener('click', () => {
  if (inFlight) inFlight.abort();
  messages = [];
  conversation = crypto.randomUUID();
  resetTurns();
  clearError();
  transcript.innerHTML = '';
  transcript.appendChild(intro);
  resetRoute();
  /* Deliberately NOT resetBudget(): the provider has not changed, so its
   * counter is still the truth. A showcase switch is what resets it. */
  setBusy(false);
});

/* Empties the gateway's semantic cache, so the next question goes to the
 * model again. The button says how it went for a moment. */
clearCacheBtn.addEventListener('click', async () => {
  clearCacheBtn.disabled = true;
  let label = 'Cache not cleared';
  try {
    const response = await fetch('/api/cache/clear', {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
    const data = await response.json();
    if (response.ok) {
      label = data.cleared === null ? 'Cache cleared'
        : 'Cache cleared: ' + data.cleared + (data.cleared === 1 ? ' answer' : ' answers');
    } else if (data.error) {
      label += ': ' + data.error;
    }
  } catch (error) {
    label += ': ' + error;
  }
  clearCacheBtn.textContent = label;
  setTimeout(() => {
    clearCacheBtn.textContent = 'Clear cache';
    clearCacheBtn.disabled = false;
  }, 3000);
});

/* screens.js clears the transcript on a showcase switch; the message history
 * and any in-flight request belong to this module. */
onSessionReset(() => {
  if (inFlight) inFlight.abort();
  messages = [];
  conversation = crypto.randomUUID();
});
