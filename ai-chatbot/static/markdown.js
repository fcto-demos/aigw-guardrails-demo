/* Markdown-lite renderer.
 *
 * Claude answers in markdown, so raw asterisks would look broken. This covers
 * what a chat reply actually uses, and escapes everything before formatting.
 * Pure functions -- no DOM, no app state -- which is why this is the one piece
 * that lifts out cleanly.
 */
'use strict';

export function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* Emphasis, but never inside an inline code span. */
export function inlineFormat(escaped) {
  return escaped.split(/(`[^`\n]+`)/).map((segment) => {
    if (segment.length > 1 && segment.startsWith('`') && segment.endsWith('`')) {
      return '<code>' + segment.slice(1, -1) + '</code>';
    }
    return segment
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[\s(])[*_]([^*_\n]+)[*_](?=[\s.,!?;:)]|$)/g, '$1<em>$2</em>');
  }).join('');
}

const TABLE_ROW = /^\|(.+)\|\s*$/;
const TABLE_RULE = /^\|[\s:|-]*-[\s:|-]*\|\s*$/;

export function tableCells(line) {
  return line.replace(/^\||\|\s*$/g, '').split('|').map((c) => c.trim());
}

export function renderProse(raw) {
  const out = [];
  let para = [];
  let items = [];
  let listTag = 'ul';

  const flushPara = () => {
    if (para.length) { out.push('<p>' + para.join('<br>') + '</p>'); para = []; }
  };
  const flushList = () => {
    if (items.length) {
      out.push('<' + listTag + '>'
               + items.map((i) => '<li>' + i + '</li>').join('')
               + '</' + listTag + '>');
      items = [];
    }
  };

  const lines = escapeHtml(raw).split('\n');

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const trimmed = line.trim();

    /* A pipe table: header row, a |---|---| rule, then body rows. The
     * catalogue is full of tables, so the model answers with them constantly;
     * without this they render as a wall of raw pipes. */
    if (TABLE_ROW.test(trimmed) && i + 1 < lines.length
        && TABLE_RULE.test(lines[i + 1].trim())) {
      flushPara(); flushList();
      const head = tableCells(trimmed);
      const body = [];
      i += 2;
      while (i < lines.length && TABLE_ROW.test(lines[i].trim())
             && !TABLE_RULE.test(lines[i].trim())) {
        body.push(tableCells(lines[i].trim()));
        i += 1;
      }
      i -= 1;   // the outer loop advances past the last consumed row
      out.push(
        '<div class="table-wrap"><table><thead><tr>'
        + head.map((c) => '<th>' + inlineFormat(c) + '</th>').join('')
        + '</tr></thead><tbody>'
        + body.map((row) => '<tr>'
            + row.map((c) => '<td>' + inlineFormat(c) + '</td>').join('')
            + '</tr>').join('')
        + '</tbody></table></div>');
      continue;
    }

    if (!trimmed) {
      flushPara(); flushList();
    } else if (/^#{1,4}\s+/.test(trimmed)) {
      flushPara(); flushList();
      out.push('<h3>' + inlineFormat(trimmed.replace(/^#{1,4}\s+/, '')) + '</h3>');
    } else if (/^[-*+]\s+/.test(trimmed)) {
      flushPara();
      if (listTag !== 'ul') { flushList(); listTag = 'ul'; }
      items.push(inlineFormat(trimmed.replace(/^[-*+]\s+/, '')));
    } else if (/^\d+[.)]\s+/.test(trimmed)) {
      flushPara();
      if (listTag !== 'ol') { flushList(); listTag = 'ol'; }
      items.push(inlineFormat(trimmed.replace(/^\d+[.)]\s+/, '')));
    } else {
      flushList();
      para.push(inlineFormat(trimmed));
    }
  }
  flushPara(); flushList();
  return out.join('');
}

/* Fenced code blocks split the text; odd segments are code. An unclosed fence
 * mid-stream still renders as code, which is what a reader expects to see. */
export function renderMarkdown(source) {
  return source.split('```').map((segment, index) => (
    index % 2
      ? '<pre><code>'
        + escapeHtml(segment.replace(/^[\w-]*\n?/, '').replace(/\n$/, ''))
        + '</code></pre>'
      : renderProse(segment)
  )).join('');
}
