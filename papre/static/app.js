import {$, $$, node, button} from './components.js';
let session, entry = null, proposal = null, sourcePath = null, busy = false, lastBuild = null, directory = null;
let importFilename = null, warningEntry = null, toastTimer, polling = false, opening = 0, buildGeneration = 0, queueGeneration = 0;
const drafts = new Map(), editing = new Set(), expanded = new Set();
function toast(message, error = false) {
  $('#toast').textContent = message; $('#toast').classList.toggle('error', error); $('#toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('#toast').hidden = true; }, error ? 10000 : 4000);
}
async function api(path, data) {
  const options = data === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json',
    'X-Review-Token': session.token, ...(session.repo_key ? {'X-Repository-Key': session.repo_key} : {})}, body: JSON.stringify(data)};
  const response = await fetch(path, options), result = await response.json();
  if (!response.ok) { const error = new Error(result.error || `Request failed (${response.status})`); error.status = response.status; throw error; }
  return result;
}
function safeNavigate() {
  if (drafts.size && !confirm('Discard the unsaved update edit?')) return false;
  drafts.clear(); editing.clear(); expanded.clear(); return true;
}
const hunks = () => proposal ? proposal.files.flatMap(file => file.hunks) : [];
const activeEntries = () => session.queue.entries.filter(item => !item.path.includes('/') && item.status !== 'skipped');
function previewStale() { if (lastBuild) $('#build-badge').textContent = 'Recompile to update'; }
async function refreshSession() {
  const next = await api('/api/session');
  const changedRepo = session && session.repo_key !== next.repo_key;
  session = next;
  if (changedRepo) { entry = proposal = sourcePath = null; drafts.clear(); editing.clear(); expanded.clear(); lastBuild = null; }
  $('#repo-path').textContent = session.repo?.root || 'No repository selected';
  $('#branch').textContent = session.repo?.branch || '';
  $('#repo-state').textContent = session.repo ? (session.repo.dirty ? 'Modified' : 'Clean') : '';
  for (const id of ['import-open', 'context-open', 'git-open']) $('#' + id).disabled = !session.repo;
  $('#demo-patch').hidden = !session.demo;
  const selected = $('#main-file').value; $('#main-file').replaceChildren();
  for (const file of session.files.filter(path => path.endsWith('.tex'))) {
    const option = node('option', '', file); option.value = file; $('#main-file').append(option);
  }
  $('#main-file').value = session.files.includes(selected) ? selected : session.main;
  for (const option of $$('#engine option')) option.disabled = !session.compiler.engines.includes(option.value);
  if (!session.compiler.engines.includes($('#engine').value)) $('#engine').value = session.compiler.engines[0] || 'pdflatex';
  $('#compiler-status').textContent = 'Detected engines: ' + (session.compiler.engines.join(', ') || 'none') +
    '. latexmk: ' + (session.compiler.latexmk ? 'available' : 'not found') + '.';
  $('#latex-install').href = session.compiler.installation.url;
  $('#latex-install').textContent = session.compiler.can_compile ? 'LaTeX setup help' : session.compiler.installation.title;
  $('#latex-install').title = session.compiler.installation.text;
  renderNav(); previewHint();
}
function renderNav() {
  const pending = activeEntries(), skipped = session.queue.entries.filter(item => item.status === 'skipped');
  const processed = session.queue.entries.filter(item => item.path.startsWith('processed/'));
  for (const [list, items, count] of [['queue-list', pending, 'queue-count'], ['skipped-list', skipped, 'skipped-count'], ['processed-list', processed, 'processed-count']]) {
    $('#' + count).textContent = items.length; $('#' + list).replaceChildren();
    for (const item of items) {
      const control = button(item.title, () => { if (safeNavigate()) openEntry(item.id).catch(error => toast(error.message, true)); }, 'nav');
      control.setAttribute('pressed', String(entry?.id === item.id && !sourcePath));
      const state = item.error ? 'Cannot apply' : item.proposal_status === 'applied' ? 'Applied' : item.status === 'processed' ? 'Reviewed' :
        item.counts ? `${item.counts.pending} pending` : item.status;
      control.setAttribute('detail', `${item.path} · ${state}`); $('#' + list).append(control);
    }
    if (!items.length) $('#' + list).append(node('div', 'sidebar-empty', list === 'queue-list' ? 'No pending patches.' : 'None'));
  }
  $('#archive-count').textContent = session.queue.archives.length; $('#archive-list').replaceChildren();
  for (const archive of session.queue.archives) {
    const control = button(archive.name, async () => {
      try { $('#archived-text').textContent = (await api('/api/queue/text?path=' + encodeURIComponent(archive.path))).text;
        $('#archive-dialog').showModal(); } catch (error) { toast(error.message, true); }
    }, 'nav'); $('#archive-list').append(control);
  }
  $('#file-list').replaceChildren();
  for (const file of session.files) {
    const control = button(file, () => { if (safeNavigate()) showSource(file).catch(error => toast(error.message, true)); }, 'nav');
    control.setAttribute('pressed', String(sourcePath === file)); $('#file-list').append(control);
  }
}
async function openEntry(id) {
  const generation = ++queueGeneration; opening++;
  try {
    const result = await api(`/api/queue/${id}/open`, {});
    if (generation !== queueGeneration) return;
    entry = result.entry; proposal = result.proposal; sourcePath = null;
    sessionStorage.setItem('review-entry:' + session.repo_key, id);
    $('#preview-selection').value = proposal?.status === 'reviewing' ? 'proposed' : 'working';
    await refreshSession();
    if (generation !== queueGeneration) return;
    renderReview(); previewStale();
    if (result.warning) showWarning(id, result.warning);
  } finally { opening--; }
}
function showWarning(id, message) {
  warningEntry = id; $('#warning-text').textContent = message;
  const item = session.queue.entries.find(item => item.id === id);
  $('#warning-skip').disabled = Boolean(item?.path.includes('/'));
  if (!$('#warning-dialog').open) $('#warning-dialog').showModal();
}
function renderReview() {
  $('#review-view').hidden = false; $('#source-view').hidden = true;
  const valid = proposal && entry?.status !== 'invalid';
  const active = valid && proposal.status === 'reviewing';
  $('#proposal-title').textContent = entry?.title || (session.repo ? 'Review queue' : 'Choose a repository to review patches');
  $('#proposal-info').textContent = entry ? `${entry.path} · ${entry.status}` : session.repo ? session.queue.root : 'Patches are read from review_queue in the selected repository.';
  $('#proposal-rationale').textContent = proposal?.rationale || ''; $('#proposal-rationale').hidden = !proposal?.rationale;
  $('#skip').hidden = !entry || entry.path.includes('/'); $('#skip').disabled = busy;
  $('#next').hidden = !session.repo; $('#next').disabled = !activeEntries().some(item => item.id !== entry?.id);
  $('#review-toolbar').hidden = !proposal; $('#apply-bar').hidden = !proposal;
  $('#changes').replaceChildren();
  if (!proposal) {
    const empty = node('p', 'empty-state', entry?.error || 'Add a patch to review_queue or choose one from the list.');
    $('#changes').append(empty);
    if (entry?.error) $('#changes').append(button('Archive', () => archiveEntry(entry.id)));
    return;
  }
  const counts = proposal.counts;
  $('#review-counts').textContent = `${counts.pending} pending · ${counts.accepted} accepted · ${counts.rejected} rejected`;
  $('#accept-all').disabled = !active || !counts.pending || busy;
  $('#reject-all').disabled = !active || !counts.pending || busy;
  if (!valid) {
    const warning = node('div', 'form-error', entry.error || 'This patch cannot apply to the current repository.');
    warning.append(button('Archive', () => archiveEntry(entry.id))); $('#changes').append(warning);
  }
  for (const file of proposal.files) {
    const diff = node('review-diff'); $('#changes').append(diff);
    diff.model = {file, context: $('#context-size').value, active, busy, drafts, editing, expanded};
  }
  $('#apply').hidden = proposal.status !== 'reviewing'; $('#undo').hidden = proposal.status !== 'applied';
  $('#apply').disabled = !active || busy || !session.allow_write || counts.pending > 0 || counts.accepted === 0 || drafts.size > 0;
  $('#undo').disabled = busy || !session.allow_write;
  $('#export-patch').href = `/api/proposals/${proposal.id}/patch?selection=accepted`;
  $('#apply-summary').textContent = proposal.status === 'applied' ? 'Accepted changes applied' : proposal.status === 'undone' ? 'Apply undone' :
    counts.pending ? `${counts.pending} changes remain` : counts.accepted ? `${counts.accepted} accepted changes · moved to processed` : 'All changes rejected · moved to processed';
  $('#apply-note').textContent = drafts.size ? 'Save or cancel your update edit.' : !session.allow_write ? 'Apply is disabled. Enable it when attaching the repository.' :
    proposal.status === 'reviewing' ? 'Review decisions are saved. Manuscript source changes only when you click Apply.' : 'Review history is retained.';
  previewHint();
}
$('#changes').addEventListener('open-source', event => {
  if (safeNavigate()) showSource(event.detail).catch(error => toast(error.message, true));
});
$('#changes').addEventListener('draft-change', () => { $('#apply').disabled = true; $('#apply-note').textContent = 'Save or cancel your update edit.'; });
$('#changes').addEventListener('review-action', event => {
  const {action, id, decision, new: update} = event.detail;
  const hunk = hunks().find(h => h.id === id);
  if (action === 'edit') { editing.add(id); renderReview(); $(`article[data-hunk="${id}"] textarea`)?.focus(); }
  else if (action === 'cancel') { editing.delete(id); drafts.delete(id); renderReview(); }
  else if (action === 'save') updateDecisions([{id, new: update, decision: update === hunk.new ? hunk.decision : 'pending'}]);
  else if (action === 'decision') updateDecisions([{id, decision, ...(drafts.has(id) ? {new: drafts.get(id)} : {})}]);
});
async function updateDecisions(decisions) {
  if (busy) return; busy = true; queueGeneration++;
  try {
    const result = await api(`/api/queue/${entry.id}/review`, {revision: proposal.revision, decisions});
    entry = result.entry; proposal = result.proposal;
    for (const decision of decisions) { drafts.delete(decision.id); editing.delete(decision.id); }
    await refreshSession(); previewStale();
  } catch (error) {
    toast(error.message, true);
    if (/changed since import|could not be applied|does not apply/.test(error.message)) showWarning(entry.id, error.message);
  } finally { busy = false; renderReview(); }
}
for (const [id, decision] of [['accept-all', 'accepted'], ['reject-all', 'rejected']]) {
  $('#' + id).addEventListener('click', () => updateDecisions(hunks().filter(h => h.decision === 'pending').map(h =>
    ({id: h.id, decision, ...(drafts.has(h.id) ? {new: drafts.get(h.id)} : {})}))));
}
for (const action of ['apply', 'undo']) $('#' + action).addEventListener('click', async () => {
  if (busy) return; busy = true; queueGeneration++; renderReview();
  try {
    const result = await api(`/api/queue/${entry.id}/${action}`, {revision: proposal.revision});
    entry = result.entry; proposal = result.proposal; await refreshSession();
    $('#preview-selection').value = 'working'; previewStale(); toast(action === 'apply' ? 'Accepted changes applied.' : 'Apply undone.');
  } catch (error) { toast(error.message, true); if (action === 'apply' && /changed since import|does not apply/.test(error.message)) showWarning(entry.id, error.message); }
  finally { busy = false; renderReview(); }
});
async function nextEntry(exclude = entry?.id) {
  const next = activeEntries().find(item => item.id !== exclude);
  entry = proposal = null; sourcePath = null;
  if (next) await openEntry(next.id);
  else { sessionStorage.removeItem('review-entry:' + session.repo_key); renderReview(); }
}
async function skipEntry(id) {
  if (busy || !safeNavigate()) return;
  busy = true; queueGeneration++;
  try {
    await api(`/api/queue/${id}/skip`, {}); $('#warning-dialog').close(); warningEntry = null;
    await refreshSession(); await nextEntry(id); toast('Skipped. The patch remains in review_queue.');
  } finally { busy = false; renderReview(); }
}
async function archiveEntry(id) {
  if (busy || !safeNavigate()) return;
  busy = true; queueGeneration++;
  try {
    await api(`/api/queue/${id}/archive`, {}); $('#warning-dialog').close(); warningEntry = null;
    await refreshSession(); await nextEntry(id); toast('Patch moved to superseded.');
  } catch (error) { toast(error.message, true); }
  finally { busy = false; renderReview(); }
}
$('#skip').addEventListener('click', () => skipEntry(entry.id).catch(error => toast(error.message, true)));
$('#next').addEventListener('click', () => { if (safeNavigate()) nextEntry().catch(error => toast(error.message, true)); });
$('#warning-skip').addEventListener('click', () => skipEntry(warningEntry).catch(error => toast(error.message, true)));
$('#warning-archive').addEventListener('click', () => archiveEntry(warningEntry));
$('#context-size').addEventListener('change', renderReview);
async function showSource(path) {
  const result = await api('/api/file?path=' + encodeURIComponent(path)); sourcePath = path;
  $('#review-view').hidden = true; $('#source-view').hidden = false;
  $('#source-title').textContent = path; $('#source-text').text = result.text; renderNav();
}
$('#back-review').addEventListener('click', () => { sourcePath = null; renderReview(); renderNav(); });

async function browseDirectory(path) {
  directory = await api('/api/directories' + (path ? '?path=' + encodeURIComponent(path) : ''));
  $('#directory-path').value = directory.path; $('#directory-up').disabled = !directory.parent;
  $('#attach-repo').disabled = !directory.is_repo;
  $('#directory-status').textContent = directory.is_repo ? 'This directory is a Git repository. Attaching creates review_queue, processed, and superseded if needed.' : 'Open a directory containing a Git repository.';
  $('#directory-list').replaceChildren();
  for (const folder of directory.directories) {
    const control = button(folder.name, () => browseDirectory(folder.path).catch(error => repoError(error.message)), 'nav');
    if (folder.is_repo) control.setAttribute('detail', 'Git repository'); $('#directory-list').append(control);
  }
  if (!directory.directories.length) $('#directory-list').append(node('p', 'sidebar-empty', 'No subdirectories.'));
}
function repoError(message) { $('#repo-error').textContent = message; $('#repo-error').hidden = false; }
async function chooseRepo() {
  if (!safeNavigate()) return;
  $('#repo-error').hidden = true; $('#repo-write').checked = session.allow_write;
  $('#repo-dialog').showModal(); await browseDirectory(session.repo?.root || session.browse_root);
}
$('#choose-repo').addEventListener('click', () => chooseRepo().catch(error => repoError(error.message)));
$('#directory-go').addEventListener('click', () => browseDirectory($('#directory-path').value).catch(error => repoError(error.message)));
$('#directory-path').addEventListener('keydown', event => { if (event.key === 'Enter') $('#directory-go').click(); });
$('#directory-up').addEventListener('click', () => browseDirectory(directory.parent).catch(error => repoError(error.message)));
$('#attach-repo').addEventListener('click', async () => {
  queueGeneration++;
  $('#attach-repo').disabled = true; $('#repo-error').hidden = true;
  try {
    session = await api('/api/repository', {path: directory.path, allow_write: $('#repo-write').checked});
    entry = proposal = sourcePath = lastBuild = null; buildGeneration++; drafts.clear(); editing.clear(); expanded.clear();
    $('#pdf-pages').hidden = $('#pdf-frame').hidden = $('#open-pdf').hidden = $('#pdf-zoom-control').hidden = true;
    $('#pdf-empty').hidden = false; $('#build-badge').textContent = 'Not compiled'; $('#compile').label = 'Compile preview';
    $('#repo-dialog').close(); await refreshSession();
    const first = activeEntries()[0]; if (first) await openEntry(first.id); else renderReview();
  } catch (error) { repoError(error.message); $('#attach-repo').disabled = false; }
});
$('#import-open').addEventListener('click', () => { $('#import-error').hidden = true; $('#import-dialog').showModal(); $('#patch-input').focus(); });
$('#patch-file').addEventListener('change', async event => {
  const file = event.target.files[0]; if (!file) return;
  if (file.size > 16 * 1024 * 1024) { toast('Patch is larger than 16 MB.', true); return; }
  importFilename = file.name; $('#patch-input').value = await file.text();
});
$('#demo-patch').addEventListener('click', async () => {
  try { $('#patch-input').value = (await api('/api/demo-patch')).patch; $('#import-title').value = 'Example manuscript edits'; importFilename = null; }
  catch (error) { toast(error.message, true); }
});
$('#import-submit').addEventListener('click', async () => {
  busy = true; queueGeneration++; $('#import-submit').disabled = true; $('#import-error').hidden = true;
  try {
    const queued = await api('/api/import', {patch: $('#patch-input').value, title: $('#import-title').value,
      rationale: $('#import-reason').value, filename: importFilename});
    $('#import-dialog').close(); $('#patch-input').value = ''; importFilename = null;
    await refreshSession();
    if (safeNavigate()) await openEntry(queued.id);
    toast('Patch added to review_queue.');
  } catch (error) { $('#import-error').textContent = error.message; $('#import-error').hidden = false; }
  finally { busy = false; $('#import-submit').disabled = false; renderReview(); }
});
$('#context-open').addEventListener('click', () => {
  $('#context-files').replaceChildren(); $('#context-fallback').hidden = true;
  for (const file of session.files) {
    const label = node('label'), input = node('input'); input.type = 'checkbox'; input.value = file;
    input.checked = file === $('#main-file').value || file.endsWith('.bib'); label.append(input, document.createTextNode(file)); $('#context-files').append(label);
  }
  $('#context-dialog').showModal();
});
$('#copy-context').addEventListener('click', async () => {
  try {
    const result = await api('/api/context', {files: $$('#context-files input:checked').map(input => input.value)});
    try { await navigator.clipboard.writeText(result.text); $('#context-dialog').close(); toast('Source and prompt copied.'); }
    catch { $('#context-fallback').value = result.text; $('#context-fallback').hidden = false; $('#context-fallback').focus(); $('#context-fallback').select(); }
  } catch (error) { toast(error.message, true); }
});
$('#git-open').addEventListener('click', async () => {
  try {
    await refreshSession(); $('#git-status').textContent = session.repo.changes || 'Working tree is clean.';
    $('#git-remotes').textContent = 'Remotes: ' + (session.repo.remotes.join(', ') || 'none');
    const events = await api('/api/events'); $('#history').replaceChildren();
    for (const event of events) {
      const row = node('div', 'history-row'); row.append(node('span', '', event.action + (event.detail ? ' · ' + event.detail : '')),
        node('small', '', new Date(event.time).toLocaleString())); $('#history').append(row);
    } $('#git-dialog').showModal();
  } catch (error) { toast(error.message, true); }
});
$('#refresh').addEventListener('click', async () => {
  if (!safeNavigate()) return;
  try { await refreshSession(); if (entry) await openEntry(entry.id); else await nextEntry(null); }
  catch (error) { toast(error.message, true); }
});
$('#toggle-preview').addEventListener('click', () => {
  $('#preview-pane').hidden = !$('#preview-pane').hidden; $('#toggle-preview').setAttribute('pressed', String(!$('#preview-pane').hidden));
});
function previewHint() {
  if (!session) return;
  const selection = $('#preview-selection').value, requires = selection !== 'working';
  $('#compile').disabled = !session.repo || !session.compiler.latexmk || !session.compiler.engines.length || !$('#main-file').value ||
    (requires && (!proposal || proposal.status !== 'reviewing' || entry?.status === 'invalid'));
  $('#preview-note').textContent = !session.compiler.can_compile ? 'Install latexmk and a LaTeX engine, then restart papre. Review works without PDF compilation.' : selection === 'working' ? 'Current repository files.' :
    selection === 'accepted' ? 'Only accepted changes.' : 'Accepted and pending changes; rejected changes are excluded.';
  if (lastBuild && (lastBuild.selection !== selection || lastBuild.main !== $('#main-file').value || lastBuild.engine !== $('#engine').value)) previewStale();
}
for (const id of ['main-file', 'preview-selection', 'engine']) $('#' + id).addEventListener('change', previewHint);
$('#toggle-log').addEventListener('click', () => { $('#build-log').hidden = !$('#build-log').hidden; $('#toggle-log').label = $('#build-log').hidden ? 'Show log' : 'Hide log'; });
$('#pdf-zoom').addEventListener('change', () => {
  $('#pdf-pages').classList.remove('zoom-125', 'zoom-150', 'zoom-200');
  if ($('#pdf-zoom').value !== 'fit') $('#pdf-pages').classList.add('zoom-' + $('#pdf-zoom').value);
});
$('#compile').addEventListener('click', async () => {
  if (drafts.size) { toast('Save or cancel the update edit before compiling.', true); return; }
  const generation = ++buildGeneration; $('#compile').disabled = true; $('#compile').label = 'Compiling…'; $('#build-badge').textContent = 'Compiling';
  $('#pdf-pages').hidden = $('#pdf-frame').hidden = $('#open-pdf').hidden = $('#pdf-zoom-control').hidden = true; $('#pdf-empty').hidden = false;
  try {
    const job = await api('/api/previews', {main: $('#main-file').value, engine: $('#engine').value,
      selection: $('#preview-selection').value, proposal: proposal?.id, revision: proposal?.revision});
    await pollBuild(job.id, generation);
  } catch (error) {
    if (generation !== buildGeneration) return;
    $('#build-badge').textContent = 'Build failed'; $('#build-log').textContent = error.message; $('#build-log').hidden = false; toast(error.message, true);
  } finally { if (generation === buildGeneration) { $('#compile').label = 'Compile preview'; previewHint(); } }
});
async function pollBuild(id, generation) {
  while (generation === buildGeneration) {
    const job = await api('/api/previews/' + id); if (generation !== buildGeneration) return;
    $('#build-log').textContent = job.log;
    if (job.status === 'running') { await new Promise(resolve => setTimeout(resolve, 800)); continue; }
    lastBuild = job;
    if (job.status === 'succeeded') {
      $('#build-badge').textContent = `${job.seconds}s · Ready`;
      if (job.pages?.length) {
        $('#pdf-pages').replaceChildren();
        job.pages.forEach((url, index) => { const figure = node('figure', 'pdf-page'), image = node('img');
          image.src = url; image.alt = `PDF page ${index + 1}`; image.loading = index === 0 ? 'eager' : 'lazy';
          figure.append(image, node('figcaption', '', `Page ${index + 1} of ${job.pages.length}`)); $('#pdf-pages').append(figure); });
        $('#pdf-pages').hidden = $('#pdf-zoom-control').hidden = false;
      } else { $('#pdf-frame').src = job.pdf; $('#pdf-frame').hidden = false; }
      $('#pdf-empty').hidden = true; $('#open-pdf').href = job.pdf; $('#open-pdf').hidden = false;
      if (job.selection !== 'working' && (proposal?.id !== job.proposal || proposal?.revision !== job.revision)) previewStale();
    } else { $('#build-badge').textContent = 'Build failed'; $('#build-log').hidden = false; $('#toggle-log').label = 'Hide log'; toast('Compilation failed. See the log.', true); }
    return;
  }
}
window.addEventListener('beforeunload', event => { if (drafts.size) { event.preventDefault(); event.returnValue = ''; } });
setInterval(async () => {
  if (!session?.repo || polling || busy || opening) return;
  polling = true; const generation = queueGeneration;
  try {
    const queue = await api('/api/queue');
    if (generation !== queueGeneration || busy || opening) return;
    session.queue = queue; renderNav();
    const current = entry && session.queue.entries.find(item => item.id === entry.id);
    if (current && !sourcePath && !drafts.size && (current.proposal !== (proposal?.id || null) ||
      (current.revision && current.revision !== proposal?.revision))) {
      await openEntry(entry.id);
    } else if (entry && !current && !drafts.size) {
      entry = proposal = null; if (!sourcePath) renderReview();
    }
  } catch { /* A manual refresh reports connection errors without interrupting editing. */ }
  finally { polling = false; }
}, 2500);
(async () => {
  try { await refreshSession(); const saved = sessionStorage.getItem('review-entry:' + session.repo_key);
    const first = session.queue.entries.find(item => item.id === saved && item.status !== 'superseded') || activeEntries()[0];
    if (first) await openEntry(first.id); else renderReview();
    if (!session.repo) await chooseRepo();
  } catch (error) { toast(error.message, true); }
})();
