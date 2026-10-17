/* What the gateway did with one turn, as a row of steps: the API key check,
 * each policy on the lane in the order it acts, then the model.
 *
 * Built only from what the response showed: the served model, the cache
 * header, the guardrail that named itself, the budget refusal. A policy the
 * response says nothing about is shown as applied, never as having done
 * something it may not have done.
 */
'use strict';

import { escapeHtml } from './markdown.js';
import { cacheState } from './metrics.js';

const ROUTERS = ['semantic-model-routing', 'time-based-model-routing',
  'cost-based-model-routing', 'intelligent-model-routing', 'model-round-robin'];
const GUARDS = ['regex-guardrail', 'semantic-prompt-guard'];
// Acts on the response, so it comes after the model.
const RESPONSE_PHASE = ['llm-cost'];

const ICONS = { ok: '✓', acted: '→', blocked: '✕', skipped: '–' };

function node(name, state, note) { return { name, state, note }; }

// "SemanticPromptGuard" and "semantic-prompt-guard" are the same guard.
function squash(name) { return String(name || '').toLowerCase().replace(/[^a-z]/g, ''); }

function shortModel(id) { return String(id || '').replace(/-\d{8}$/, ''); }

export function pipelineHtml(turn, showcase) {
  if (!showcase || showcase.mcp) return '';
  if (!['ok', 'blocked', 'refused'].includes(turn.outcome)) return '';

  const names = (showcase.policies || []).map((p) => p.split(' ')[0]);
  const before = names.filter((n) => !RESPONSE_PHASE.includes(n));
  const after = names.filter((n) => RESPONSE_PHASE.includes(n));
  const model = shortModel(turn.model);
  const cache = cacheState(turn);

  /* The guard that refused: the one that named itself, or else the first
   * guard on the lane, since a block can only have come from one of them. */
  let blocker = null;
  if (turn.outcome === 'blocked') {
    blocker = before.find((n) => squash(n) === squash(turn.guard))
           || before.find((n) => GUARDS.includes(n)) || null;
  }

  const nodes = [node('api-key-auth', 'ok', 'key valid')];
  let halted = false;
  for (const name of before) {
    if (halted) {
      nodes.push(node(name, 'skipped', 'not reached'));
    } else if (name === blocker) {
      nodes.push(node(name, 'blocked', 'blocked'));
      halted = true;
    } else if (name === 'llm-cost-based-ratelimit') {
      if (turn.outcome === 'refused') {
        nodes.push(node(name, 'blocked', 'budget spent'));
        halted = true;
      } else {
        nodes.push(node(name, 'ok', 'within budget'));
      }
    } else if (name === 'semantic-cache') {
      if (cache === 'hit') {
        nodes.push(node(name, 'acted', 'hit, answered from cache'));
        halted = true;
      } else {
        nodes.push(node(name, 'ok', cache === 'miss' ? 'miss' : 'checked'));
      }
    } else if (ROUTERS.includes(name)) {
      nodes.push(node(name, 'acted', model ? 'picked ' + model : 'routed'));
    } else if (name.startsWith('openai-to-')) {
      nodes.push(/claude/.test(model)
        ? node(name, 'acted', 'translated') : node(name, 'skipped', 'not needed'));
    } else if (GUARDS.includes(name)) {
      nodes.push(node(name, 'ok', 'passed'));
    } else {
      nodes.push(node(name, 'ok', 'applied'));
    }
  }

  const answered = turn.outcome === 'ok' && !halted;
  nodes.push(answered
    ? node(model || 'model', 'acted', 'answered')
    : node('model', 'skipped', 'not called'));
  for (const name of after) {
    nodes.push(answered
      ? node(name, 'ok', turn.cost ? '$' + turn.cost : 'counted')
      : node(name, 'skipped', 'nothing to count'));
  }

  return '<div class="pipeline" aria-label="What the gateway did">'
    + '<span class="pipe-label">Gateway</span>'
    + nodes.map((n) => '<span class="pipe-step is-' + n.state + '">'
        + '<i aria-hidden="true">' + ICONS[n.state] + '</i>'
        + '<b>' + escapeHtml(n.name) + '</b> ' + escapeHtml(n.note) + '</span>')
      .join('<span class="pipe-arrow" aria-hidden="true">›</span>')
    + '</div>';
}
