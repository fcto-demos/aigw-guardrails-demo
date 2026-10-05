/* Model identity, and nothing else.
 *
 * Anthropic answers with the dated snapshot id -- `claude-haiku-4-5-20251001`
 * for a request that named `claude-haiku-4-5` -- so an exact comparison would
 * fail every routing step against the real gateway while passing against a
 * mock. Verified on the hosted provider on 2026-09-07.
 *
 * Two modules need this: the rail, to grade `expect.model`, and the spend
 * meter, to decide which budget a reply was charged against. It lives here so
 * they cannot drift apart, and so neither has to import the other.
 */
'use strict';

/* Match on the family prefix at a dash boundary, which stops
 * `claude-haiku-45` satisfying `claude-haiku-4-5`. Note it does NOT stop a
 * shorter family prefix matching a longer one -- `claude-haiku-4-5` would
 * satisfy an expectation of `claude-haiku-4`. That cannot arise from
 * showcases.json, which names full model ids, and guarding it would mean
 * parsing version numbers. */
export function modelMatches(served, expected) {
  if (!served || !expected) return false;
  return served === expected || served.startsWith(expected + '-');
}
