// Offline DOM contract for interaction adapters. These attributes describe the
// fixture schema, not verified selectors for the live platform. No live imports.
const targetIdOf = value => typeof value === "string" &&
  /^[A-Za-z0-9_:-]{1,120}$/.test(value) ? value : "";

function replyProbe(commentId, opened = false) {
  return `(() => {
    const id = ${JSON.stringify(commentId)};
    const rows = Array.from(document.querySelectorAll('[data-comment-id]'))
      .filter(el => el.getAttribute('data-comment-id') === id);
    if (rows.length !== 1) return null;
    const row = rows[0];
    if (${opened}) return row.getAttribute('data-reply-target') === id;
    const buttons = Array.from(row.querySelectorAll('button,[role="button"]'))
      .filter(el => (el.innerText || el.getAttribute('aria-label') || '').trim() === '回复');
    if (buttons.length !== 1) return null;
    const el = buttons[0], r = el.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const hit = document.elementFromPoint(x, y);
    if (el.disabled || el.getAttribute('aria-disabled') === 'true' || r.width < 4 || r.height < 4 ||
        r.left < 0 || r.top < 0 || r.right > innerWidth || r.bottom > innerHeight ||
        !hit || (hit !== el && !el.contains(hit))) return null;
    return { x, y };
  })()`;
}

function dmProbe(recipientId) {
  return `(() => {
    const id = ${JSON.stringify(recipientId)};
    const panels = Array.from(document.querySelectorAll('[data-conversation-id][data-recipient-id]'));
    if (panels.length !== 1 || panels[0].getAttribute('data-recipient-id') !== id) return null;
    const panel = panels[0];
    const editors = Array.from(panel.querySelectorAll('textarea,input,[contenteditable="true"]'));
    const buttons = Array.from(panel.querySelectorAll('button,[role="button"]'))
      .filter(el => (el.innerText || el.getAttribute('aria-label') || '').trim() === '发送');
    const point = el => {
      if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true') return null;
      const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      return r.width > 4 && r.height > 4 && r.left >= 0 && r.top >= 0 &&
        r.right <= innerWidth && r.bottom <= innerHeight && hit && (hit === el || el.contains(hit))
        ? { x, y } : null;
    };
    const editor = editors.length === 1 && editors[0];
    const messages = Array.from(panel.querySelectorAll('[data-message-id]')).map(el => ({
      id: el.getAttribute('data-message-id'), recipientId: el.getAttribute('data-recipient-id'),
      status: el.getAttribute('data-send-status'), direction: el.getAttribute('data-direction'), text: el.innerText || ''
    }));
    return { editor: point(editor), submit: buttons.length === 1 ? point(buttons[0]) : null,
      draft: editor ? String(editor.value == null ? editor.innerText || '' : editor.value).trim() : null,
      messages };
  })()`;
}

module.exports = { targetIdOf, replyProbe, dmProbe };
