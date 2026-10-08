// Shared custom components. All instances use these controls and this diff renderer.
export const $ = (selector, parent = document) => parent.querySelector(selector);
export const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
export function node(tag, className = '', text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
export function button(label, onClick, variant = 'default') {
  const element = node('ui-button'); element.label = label; element.setAttribute('variant', variant);
  if (onClick) element.addEventListener('click', onClick);
  return element;
}
class UIButton extends HTMLElement {
  static observedAttributes = ['label', 'disabled', 'variant', 'pressed', 'detail'];
  connectedCallback() { this.render(); }
  attributeChangedCallback() { if (this.isConnected) this.render(); }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(value) { this.toggleAttribute('disabled', Boolean(value)); }
  get label() { return this.getAttribute('label') || ''; }
  set label(value) { this.setAttribute('label', value); }
  render() {
    if (!this.control) {
      this.control = node('button', 'ui-button'); this.control.type = 'button'; this.replaceChildren(this.control);
    }
    this.control.className = 'ui-button ' + (this.getAttribute('variant') || 'default');
    this.control.disabled = this.disabled;
    this.control.replaceChildren(node('span', 'button-label', this.label));
    const detail = this.getAttribute('detail');
    if (detail) this.control.append(node('small', 'button-detail', detail));
    if (this.hasAttribute('pressed')) this.control.setAttribute('aria-pressed', this.getAttribute('pressed'));
    else this.control.removeAttribute('aria-pressed');
  }
}
class UIField extends HTMLElement {
  connectedCallback() {
    if (this.mounted) return;
    const label = node(this.querySelector('label') ? 'div' : 'label', 'ui-field');
    if (this.hasAttribute('label')) label.append(node('span', 'field-label', this.getAttribute('label')));
    label.append(...this.childNodes); this.replaceChildren(label); this.mounted = true;
  }
}
class UIDialog extends HTMLElement {
  connectedCallback() {
    if (this.control) return;
    this.control = node('dialog', 'ui-dialog');
    const header = node('div', 'dialog-header');
    header.append(node('h2', '', this.getAttribute('label') || ''), button('Close', () => this.close()));
    this.control.append(header, ...this.childNodes); this.replaceChildren(this.control);
  }
  showModal() { this.control.showModal(); }
  close() { this.control.close(); }
  get open() { return Boolean(this.control?.open); }
}
customElements.define('ui-button', UIButton);
customElements.define('ui-field', UIField);
customElements.define('ui-dialog', UIDialog);
export function splitLines(text) {
  if (!text) return [];
  const lines = text.split('\n'); if (lines.at(-1) === '') lines.pop(); return lines;
}
function code(text) {
  const element = node('span', 'code');
  for (const token of text.split(/(\\[a-zA-Z@]+\*?|%.*$)/g)) {
    element.append(node('span', token.startsWith('%') ? 'syntax-comment' : token.startsWith('\\') ? 'syntax-command' : '', token));
  }
  if (!text) element.append(document.createTextNode(' '));
  return element;
}
function cell(text, number, mark = '', kind = '') {
  const element = node('div', 'diff-cell ' + kind);
  element.append(node('span', 'line-number', number ?? ''), node('span', 'line-mark', mark));
  element.append(text === null ? node('span', 'code', ' ') : code(text));
  return element;
}
class ReviewDiff extends HTMLElement {
  set model(value) { this.data = value; this.render(); }
  action(action, hunk, extra = {}) {
    this.dispatchEvent(new CustomEvent('review-action', {bubbles: true, detail: {action, id: hunk.id, ...extra}}));
  }
  render() {
    if (!this.data) return;
    const {file, context, active, busy, drafts, editing, expanded} = this.data;
    const wrapper = node('section', 'file-diff'), header = node('div', 'file-header');
    header.append(node('strong', '', file.path), button('Open full file', () => {
      this.dispatchEvent(new CustomEvent('open-source', {bubbles: true, detail: file.path}));
    })); wrapper.append(header);
    const labels = node('div', 'diff-labels'); labels.append(node('span', '', 'Original'), node('span', '', 'Update')); wrapper.append(labels);
    const base = splitLines(file.before);
    let cursor = 0, offset = 0;
    const equal = (start, end, prefix, suffix) => {
      const count = end - start;
      if (!count) return;
      const key = `${file.path}:${start}:${end}`, full = context === 'all' || expanded.has(key), n = Number(context);
      const ranges = full ? [[start, end]] : prefix ? [[Math.max(start, end - n), end]] : suffix ? [[start, Math.min(end, start + n)]] :
        count <= 2 * n ? [[start, end]] : [[start, start + n], [end - n, end]];
      let pos = start;
      const gap = (a, b) => {
        if (b <= a) return;
        const row = node('div', 'context-gap');
        row.append(button(`Show ${b - a} unchanged lines`, () => { expanded.add(key); this.render(); }, 'context')); wrapper.append(row);
      };
      for (const [a, b] of ranges) {
        gap(pos, a);
        for (let i = a; i < b; i++) {
          const row = node('div', 'diff-row'); row.append(cell(base[i], i + 1), cell(base[i], i + offset + 1)); wrapper.append(row);
        }
        pos = b;
      }
      gap(pos, end);
    };
    file.hunks.forEach((hunk, index) => {
      equal(cursor, hunk.start, index === 0, false);
      const section = node('article', 'hunk ' + hunk.decision); section.dataset.hunk = hunk.id;
      const toolbar = node('div', 'hunk-toolbar'), meta = node('div', 'hunk-meta'), actions = node('div', 'hunk-actions');
      meta.append(node('span', '', `Change ${index + 1}`), node('span', 'decision', hunk.decision));
      if (hunk.new !== hunk.suggested) meta.append(node('span', '', 'edited'));
      for (const [name, decision] of [['Accept', 'accepted'], ['Reject', 'rejected'], ['Edit', null]]) {
        const control = button(name, () => this.action(decision ? 'decision' : 'edit', hunk, decision ? {decision} : {}), name.toLowerCase());
        control.disabled = !active || busy;
        control.setAttribute('pressed', String(decision ? hunk.decision === decision : editing.has(hunk.id)));
        actions.append(control);
      }
      toolbar.append(meta, actions); section.append(toolbar);
      const oldLines = splitLines(hunk.old), newLines = splitLines(hunk.new);
      if (editing.has(hunk.id)) {
        const row = node('div', 'diff-editor-row'), left = node('div', 'editor-original'), right = node('div', 'editor-update');
        oldLines.forEach((text, i) => left.append(cell(text, hunk.start + i + 1, '-', 'removed')));
        if (!oldLines.length) left.append(cell(null, null));
        const field = node('ui-field'); field.setAttribute('label', 'Edit update');
        const input = node('textarea', 'update-editor'); input.spellcheck = false;
        input.setAttribute('aria-label', `Update for change ${index + 1} in ${file.path}`);
        input.value = drafts.get(hunk.id) ?? hunk.new;
        input.addEventListener('input', () => {
          if (input.value === hunk.new) drafts.delete(hunk.id); else drafts.set(hunk.id, input.value);
          this.dispatchEvent(new CustomEvent('draft-change', {bubbles: true}));
        }); field.append(input); right.append(field);
        const buttons = node('div', 'editor-actions');
        buttons.append(button('Cancel edit', () => this.action('cancel', hunk)), button('Save edit', () => this.action('save', hunk, {new: input.value}), 'primary'));
        if (hunk.new !== hunk.suggested) buttons.prepend(button('Restore suggestion', () => this.action('save', hunk, {new: hunk.suggested})));
        right.append(buttons); row.append(left, right); section.append(row);
      } else {
        for (let i = 0; i < Math.max(oldLines.length, newLines.length); i++) {
          const row = node('div', 'diff-row changed');
          row.append(cell(oldLines[i] ?? null, i < oldLines.length ? hunk.start + i + 1 : null, i < oldLines.length ? '-' : '', i < oldLines.length ? 'removed' : 'empty'),
            cell(newLines[i] ?? null, i < newLines.length ? hunk.start + offset + i + 1 : null, i < newLines.length ? '+' : '', i < newLines.length ? 'added' : 'empty'));
          section.append(row);
        }
      }
      wrapper.append(section); offset += newLines.length - oldLines.length; cursor = hunk.end;
    });
    equal(cursor, base.length, false, true); this.replaceChildren(wrapper);
  }
}
customElements.define('review-diff', ReviewDiff);
class SourceViewer extends HTMLElement {
  set text(value) {
    const block = node('div', 'source-lines');
    splitLines(value).forEach((text, index) => {
      const row = node('div', 'source-row'); row.append(node('span', 'line-number', index + 1), code(text)); block.append(row);
    }); this.replaceChildren(block);
  }
}
customElements.define('source-viewer', SourceViewer);
