/* Entry point.
 *
 * The app is a demo harness for WSO2 AI Gateway policies. A home screen lists
 * the capabilities; each one opens a chat screen wired to its OWN
 * pre-configured LLM provider, with that policy already attached. This app
 * never configures the gateway -- it only chooses which provider answers, by
 * posting a showcase id the server maps to a URL.
 *
 * The page does not pick a model either. The gateway's routing policies do,
 * and each reply's message_start event names the model that served it, so the
 * routing decision is read back from the response rather than asserted here.
 *
 * Module order matters only for the side-effect wiring: chat.js registers
 * itself with rail.js and screens.js as it loads, so it must be imported
 * before applyHash() runs.
 */
'use strict';

import { wordmark } from './dom.js';
import { loadProviders, loadRegistry, applyHash } from './screens.js';
import { renderLauncher } from './launcher.js';
import './chat.js';

fetch('/api/config')
  .then((r) => r.json())
  .then((c) => {
    if (c.brand) {
      wordmark.textContent = c.brand.toUpperCase();
    }
    renderLauncher(c.launcher);
  })
  .catch(() => { /* cosmetic only */ });

loadProviders().then(loadRegistry).then(applyHash);
