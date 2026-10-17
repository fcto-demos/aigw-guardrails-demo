/* The launcher: a menu in the top bar that opens the demo's other consoles
 * (AI Workspace, observability, identity provider) in a new tab. The server
 * sends only the ones this setup runs, already grouped.
 */
'use strict';

import { escapeHtml } from './markdown.js';

const box = document.getElementById('launcher');
const button = document.getElementById('launcher-btn');
const menu = document.getElementById('launcher-menu');

function setOpen(open) {
  menu.hidden = !open;
  button.setAttribute('aria-expanded', String(open));
}

// A console that is not set up yet stays listed, greyed out, with how to set it up.
function linkHtml(link) {
  if (!link.url) {
    return '<div class="launcher-link is-off" title="' + escapeHtml(link.setup || '') + '">'
      + '<span class="launcher-name">' + escapeHtml(link.name) + '</span>'
      + '<span class="launcher-note">Not configured</span></div>';
  }
  return '<a class="launcher-link" href="' + escapeHtml(link.url) + '" target="_blank" rel="noopener">'
    + '<span class="launcher-name">' + escapeHtml(link.name) + '</span>'
    + '<span class="launcher-note">' + escapeHtml(link.note) + '</span></a>';
}

export function renderLauncher(groups) {
  if (!groups || !groups.length) return;
  menu.innerHTML = groups.map((group) => '<div class="launcher-group">'
    + '<p class="launcher-title">' + escapeHtml(group.name) + '</p>'
    + group.links.map(linkHtml).join('') + '</div>').join('');
  box.hidden = false;
}

button.addEventListener('click', () => setOpen(menu.hidden));
menu.addEventListener('click', (event) => { if (event.target.closest('a')) setOpen(false); });
document.addEventListener('click', (event) => { if (!box.contains(event.target)) setOpen(false); });
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !menu.hidden) { setOpen(false); button.focus(); }
});
