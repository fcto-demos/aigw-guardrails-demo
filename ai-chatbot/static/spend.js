/* The deep model's budget on the cost-based-routing card, estimated.
 *
 * WHY IT IS ESTIMATED. Every other number in this app is observed from the
 * response, and this one cannot be. The cost-routing lane carries
 * cost-based-model-routing and llm-cost, and neither publishes a balance:
 * cost-based-model-routing emits no remaining-budget headers by design (the
 * spend is not known until the body completes), and llm-cost -- unlike
 * llm-cost-based-ratelimit on the spend-limit card, which does send
 * x-ratelimit-cost-remaining-dollars -- writes its figure into gateway
 * metadata rather than a response header. So there is nothing to read.
 *
 * What there is: the token counts the active provider returns, and the
 * per-token prices the gateway's own `configs/llm-pricing/model_prices.json`
 * carries -- the same file llm-cost charges the budget from. The budget
 * AMOUNT itself comes from providers.json's costBudget (via /api/providers,
 * screens.js's costBudgetFor() -- each provider calibrated to its own
 * pricing, see provision_showcases.py's budget_for()); showcases.json only
 * declares which ROLE is the deep/fallback model and the prices dict, and
 * this module resolves the active provider's own amount/model/fallbackModel
 * and multiplies out from there.
 *
 * showcases.json's `prices` dict is a hand-copied snapshot, keyed by the
 * LITERAL model ids configured today, not derived live the way costBudget
 * is. Change a provider's models.small/large in providers.json to a model
 * not already a key here and this breaks QUIETLY: turnCost() finds no price,
 * the turn counts as "unpriced", and the meter just stops moving for that
 * provider -- no error, nothing in the console, the gateway's own budget
 * enforcement keeps working regardless since it never reads this file. Add
 * the new model's {input, output} here (same numbers as
 * ai-gateway/configs/llm-pricing/model_prices.json) whenever a model
 * changes.
 *
 * Two honesty rules follow, and the UI keeps both:
 *
 *  1. Every figure is prefixed with "~" and labelled estimated. The budget is
 *     shared across all callers and only comes back over time, so a rehearsal a
 *     moment ago already spent some of it. This session's tokens are a FLOOR
 *     on the real spend, never the balance.
 *  2. The step-down is measured, not estimated. When the gateway serves the
 *     fallback model the deep budget is gone whatever this meter says, so the
 *     observation wins: the meter is pinned to empty and says so.
 */
'use strict';

import { modelMatches } from './models.js';
import { activeProvider, modelForRole, costBudgetFor } from './screens.js';

let config = null;      // the showcase's `budget` block, or null
let activeFor = null;   // which showcase the running total belongs to
let spent = 0;
let unpriced = 0;
let exhausted = false;  // the gateway has served the fallback at least once
let stepped = false;    // ...on the most recent turn
let justStepped = false; // ...for the first time, which is the event worth a chip

/* Called on every mount. The running total survives coming back from the home
 * screen and survives Clear chat -- the gateway's window does not care that the
 * transcript was cleared -- and is dropped only on a switch to another
 * showcase, whose provider counts its own budget. */
export function configureSpend(showcase) {
  const id = showcase ? showcase.id : null;
  if (id !== activeFor) {
    activeFor = id;
    spent = 0;
    unpriced = 0;
    exhausted = false;
    stepped = false;
    justStepped = false;
  }
  const raw = (showcase && showcase.budget) || null;
  /* amount/modelRole/fallbackModelRole resolve per the ACTIVE provider --
   * each provider's budget was calibrated to its own model pricing (see
   * providers.json's costBudget and provision_showcases.py's budget_for()),
   * and the model actually served is that provider's own id, never a
   * literal shared across providers. */
  const amount = costBudgetFor(activeProvider);
  const model = raw && raw.modelRole ? modelForRole(raw.modelRole) : null;
  const fallbackModel = raw && raw.fallbackModelRole ? modelForRole(raw.fallbackModelRole) : null;
  config = raw && model && amount > 0
    ? { ...raw, model, fallbackModel, amount }
    : null;
  return spendState();
}

export function spendState() {
  if (!config) return null;
  const limit = config.amount;
  return {
    model: config.model,
    fallbackModel: config.fallbackModel || null,
    duration: config.duration || null,
    limit,
    spent,
    remaining: Math.max(0, limit - spent),
    exhausted,
    stepped,
    justStepped,
    unpriced,
  };
}

/* Per-call cost, the way llm-cost computes it: tokens x the price for the model
 * that actually served. Null when the model is unpriced or the usage did not
 * arrive -- never 0, which would read as a free call. */
export function turnCost(turn) {
  if (!config || !turn.model) return null;
  const prices = config.prices || {};
  let price = null;
  for (const name of Object.keys(prices)) {
    if (modelMatches(turn.model, name)) { price = prices[name]; break; }
  }
  if (!price) return null;
  if (turn.inputTokens === null && turn.outputTokens === null) return null;
  return (turn.inputTokens || 0) * (price.input || 0)
       + (turn.outputTokens || 0) * (price.output || 0);
}

/* Fold one completed turn into the meter. Returns the state to paint, or null
 * on a showcase that declares no budget. */
export function recordSpend(turn) {
  if (!config) return null;

  const onDeep = modelMatches(turn.model, config.model);
  const onFallback = config.fallbackModel
    && modelMatches(turn.model, config.fallbackModel);

  const cost = turnCost(turn);
  turn.costEstimate = cost;

  /* Only the deep model's calls are charged to the budget being watched. The
   * fallback has a budget of its own, deliberately large enough that it is not
   * what the card is about.
   *
   * A deep reply AFTER a step-down means the budget has come back, so the
   * running total starts again rather than staying pinned at a limit the
   * gateway has plainly stopped enforcing. */
  if (onDeep) {
    if (exhausted) { spent = 0; unpriced = 0; }
    if (cost === null) unpriced += 1; else spent += cost;
  }

  /* The router prefers the deep model whenever that budget has capacity, so a
   * reply from the fallback on THIS lane means the capacity is gone. That is
   * true on the very first call of a session too, when an earlier run already
   * spent the window -- which is exactly when a purely arithmetic meter would
   * be lying most confidently. */
  stepped = Boolean(onFallback && !onDeep);
  justStepped = stepped && !exhausted;   // the transition, for a one-off chip
  if (stepped) exhausted = true;
  if (onDeep) exhausted = false;

  turn.spend = spendState();
  return turn.spend;
}

/* "$0.0043" / "$0.00021" -- four decimals is too coarse for a haiku call. */
export function money(value) {
  if (value === null || value === undefined || !isFinite(value)) return null;
  return '$' + (value < 0.001 ? value.toFixed(5) : value.toFixed(4));
}
