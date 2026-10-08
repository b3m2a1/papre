import {$} from './components.js';

// Layout preferences belong to this browser, independently of manuscript review state.
const preferenceKey = 'papre.layout.v1';
const defaults = {sidebarWidth: 235, previewWidth: 380, sidebarHidden: false};
let preferences = {...defaults};
try {
  const saved = JSON.parse(localStorage.getItem(preferenceKey));
  for (const key of ['sidebarWidth', 'previewWidth']) {
    if (Number.isFinite(saved?.[key])) preferences[key] = Math.max(160, Math.min(3000, saved[key]));
  }
  if (typeof saved?.sidebarHidden === 'boolean') preferences.sidebarHidden = saved.sidebarHidden;
} catch { /* Storage can be unavailable; resizing still works. */ }

const layout = $('.app-layout'), sidebar = $('#sidebar'), preview = $('#preview-pane');
const sidebarSplitter = $('#sidebar-splitter'), previewSplitter = $('#preview-splitter');
let expanded = false, scrollBeforeExpand = 0, previewVisibilityChosen = false;
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
function save() {
  try { localStorage.setItem(preferenceKey, JSON.stringify(preferences)); } catch { /* Optional persistence. */ }
}
function bounds() {
  const width = layout.getBoundingClientRect().width;
  // Keep space for the source and preview at narrower desktop widths.
  const sidebarMax = Math.max(160, Math.min(420, width - (!preview.hidden && width > 900 ? 676 : 328)));
  const sidebarWidth = clamp(preferences.sidebarWidth, 160, sidebarMax);
  const workspaceWidth = width - (preferences.sidebarHidden ? 0 : sidebarWidth + 8);
  return {sidebarMax, sidebarWidth, previewMax: Math.max(300, workspaceWidth - 368)};
}
function render() {
  const {sidebarMax, sidebarWidth, previewMax} = bounds();
  const previewWidth = clamp(preferences.previewWidth, 300, previewMax);
  layout.style.setProperty('--sidebar-width', `${sidebarWidth}px`);
  layout.style.setProperty('--preview-width', `${previewWidth}px`);
  layout.classList.toggle('sidebar-hidden', preferences.sidebarHidden || expanded);
  layout.classList.toggle('pdf-expanded', expanded);
  sidebar.hidden = sidebarSplitter.hidden = preferences.sidebarHidden || expanded;
  previewSplitter.hidden = preview.hidden || expanded;
  $('#toggle-sidebar').label = sidebar.hidden ? 'Show sidebar' : 'Hide sidebar';
  $('#toggle-sidebar').setAttribute('expanded', String(!sidebar.hidden));
  $('#expand-preview').label = expanded ? 'Restore split view' : 'Expand PDF';
  $('#expand-preview').setAttribute('icon', expanded ? 'restore' : 'expand');
  $('#expand-preview').setAttribute('pressed', String(expanded));
  $('#toggle-preview').setAttribute('pressed', String(!preview.hidden));
  sidebarSplitter.range = {min: 160, max: sidebarMax, value: sidebarWidth};
  previewSplitter.range = {min: 300, max: previewMax, value: previewWidth};
}
function setExpanded(value) {
  if (value === expanded) return;
  $('#preview-settings').close(false);
  if (value) {
    scrollBeforeExpand = window.scrollY; preview.hidden = false;
  }
  expanded = value; render();
  window.scrollTo(0, expanded ? 0 : scrollBeforeExpand);
}
$('#toggle-sidebar').addEventListener('click', () => {
  if (expanded) { setExpanded(false); preferences.sidebarHidden = false; }
  else preferences.sidebarHidden = !preferences.sidebarHidden;
  render(); save();
});
$('#toggle-preview').addEventListener('click', () => {
  previewVisibilityChosen = true;
  if (expanded) setExpanded(false);
  $('#preview-settings').close(false);
  preview.hidden = !preview.hidden; render();
});
export function revealPreview() {
  if (previewVisibilityChosen || !preview.hidden) return;
  preview.hidden = false; render();
}
$('#expand-preview').addEventListener('click', () => setExpanded(!expanded));
$('#toggle-preview-settings').addEventListener('click', () => {
  const settings = $('#preview-settings');
  if (settings.open) settings.close();
  else settings.show($('#toggle-preview-settings').control);
});
$('#preview-settings').addEventListener('drawer-toggle', event => {
  $('#toggle-preview-settings').setAttribute('expanded', String(event.detail.open));
  $('.preview-content').classList.toggle('settings-open', event.detail.open);
});
for (const [splitter, key, direction] of [[sidebarSplitter, 'sidebarWidth', 1], [previewSplitter, 'previewWidth', -1]]) {
  splitter.addEventListener('splitter-change', event => {
    const {phase, delta, edge} = event.detail;
    if (phase === 'start') document.body.classList.add('resizing-panels');
    if (phase === 'resize') {
      const min = Number(splitter.getAttribute('aria-valuemin')), max = Number(splitter.getAttribute('aria-valuemax'));
      const current = splitter.value;
      preferences[key] = edge ? (edge === 'min' ? min : max) : clamp(current + direction * delta, min, max);
      render();
    }
    if (phase === 'end') { document.body.classList.remove('resizing-panels'); save(); }
  });
}
window.addEventListener('resize', render);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !event.defaultPrevented && expanded && !$('dialog[open]')) {
    setExpanded(false); $('#expand-preview').control.focus();
  }
});
// Wrapped toolbars change the available height on small screens.
new ResizeObserver(entries => {
  layout.style.setProperty('--toolbar-height', `${entries[0].target.getBoundingClientRect().height}px`);
}).observe($('.app-toolbar'));
render();
