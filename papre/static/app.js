import {$, $$, node, button} from './components.js';
import {revealPreview} from './layout.js';
let session, entry = null, proposal = null, sourcePath = null, busy = false, lastBuild = null, directory = null;
let importFilename = null, warningEntry = null, toastTimer, polling = false, opening = 0, buildGeneration = 0, queueGeneration = 0;
const drafts = new Map(), editing = new Set(), expanded = new Set();
let previewRun = null, previewRequests = Promise.resolve(), waitingForPreview = false;
let focusedChange = null, pdfTarget = '';
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
function previewStale() { if (lastBuild && !previewRun) $('#build-badge').textContent = 'Recompile to update'; }
async function refreshSession() {
  const next = await api('/api/session');
  const changedRepo = session && session.repo_key !== next.repo_key;
  session = next;
  if (changedRepo) { invalidatePreview(); waitingForPreview = false; entry = proposal = sourcePath = null; drafts.clear(); editing.clear(); expanded.clear(); lastBuild = null; clearPreview(); }
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
  invalidatePreview();
  const generation = ++queueGeneration; opening++;
  try {
    const result = await api(`/api/queue/${id}/open`, {});
    if (generation !== queueGeneration) return;
    entry = result.entry; proposal = result.proposal; sourcePath = null;
    sessionStorage.setItem('review-entry:' + session.repo_key, id);
    $('#preview-selection').value = proposal?.status === 'reviewing' ? 'proposed' : 'working';
    await refreshSession();
    if (generation !== queueGeneration) return;
    renderReview(); warmPreview();
    if (result.warning) showWarning(id, result.warning);
  } catch (error) { cancelBackgroundPreview(); throw error; }
  finally { opening--; }
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
    diff.model = {file, context: $('#context-size').value, active, busy, drafts, editing, expanded, locations: previewLocations()};
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
  updatePdfLocations();
}
$('#changes').addEventListener('open-source', event => {
  if (safeNavigate()) showSource(event.detail).catch(error => toast(error.message, true));
});
$('#changes').addEventListener('draft-change', () => { $('#apply').disabled = true; $('#apply-note').textContent = 'Save or cancel your update edit.'; });
$('#changes').addEventListener('review-action', event => {
  const {action, id, decision, new: update} = event.detail;
  const hunk = hunks().find(h => h.id === id);
  if (action === 'preview-section') scrollToPdf(id);
  else if (action === 'edit') { editing.add(id); renderReview(); $(`article[data-hunk="${id}"] textarea`)?.focus(); }
  else if (action === 'cancel') { editing.delete(id); drafts.delete(id); renderReview(); }
  else if (action === 'save') updateDecisions([{id, new: update, decision: update === hunk.new ? hunk.decision : 'pending'}]);
  else if (action === 'decision') updateDecisions([{id, decision, ...(drafts.has(id) ? {new: drafts.get(id)} : {})}]);
});
async function updateDecisions(decisions) {
  if (busy) return; busy = true; queueGeneration++;
  invalidatePreview();
  try {
    const result = await api(`/api/queue/${entry.id}/review`, {revision: proposal.revision, decisions});
    entry = result.entry; proposal = result.proposal;
    for (const decision of decisions) { drafts.delete(decision.id); editing.delete(decision.id); }
    await refreshSession(); warmPreview();
  } catch (error) {
    cancelBackgroundPreview();
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
    $('#preview-selection').value = 'working'; warmPreview(); toast(action === 'apply' ? 'Accepted changes applied.' : 'Apply undone.');
  } catch (error) { toast(error.message, true); if (action === 'apply' && /changed since import|does not apply/.test(error.message)) showWarning(entry.id, error.message); }
  finally { busy = false; renderReview(); }
});
async function nextEntry(exclude = entry?.id) {
  invalidatePreview();
  const next = activeEntries().find(item => item.id !== exclude);
  entry = proposal = null; sourcePath = null;
  if (next) await openEntry(next.id);
  else { sessionStorage.removeItem('review-entry:' + session.repo_key); renderReview(); cancelBackgroundPreview(); }
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

let directoryGeneration = 0;
function renderDirectory(listing) {
  directory = listing;
  $('#repo-error').hidden = true;
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
async function browseDirectory(path) {
  const generation = ++directoryGeneration;
  $('#attach-repo').disabled = true;
  const listing = await api('/api/directories' + (path ? '?path=' + encodeURIComponent(path) : ''));
  if (generation === directoryGeneration) renderDirectory(listing);
}
function repoError(message) { $('#repo-error').textContent = message; $('#repo-error').hidden = false; }
async function chooseRepo() {
  if (!safeNavigate()) return;
  $('#repo-error').hidden = true; $('#repo-write').checked = session.allow_write;
  $('#repo-dialog').showModal(); await browseDirectory(session.repo?.root || session.browse_start || session.browse_root);
}
$('#choose-repo').addEventListener('click', () => chooseRepo().catch(error => repoError(error.message)));
$('#directory-go').addEventListener('click', () => browseDirectory($('#directory-path').value).catch(error => repoError(error.message)));
$('#directory-picker').addEventListener('click', async () => {
  $('#directory-picker').disabled = true;
  $('#directory-picker').label = 'Picker open…'; $('#repo-error').hidden = true;
  try {
    const result = await api('/api/directory-picker', {path: directory?.path || session.browse_start});
    if (!result.cancelled) { directoryGeneration++; renderDirectory(result.directory); }
  } catch (error) {
    $('#directory-navigation').open = true; repoError(error.message);
  } finally { $('#directory-picker').disabled = false; $('#directory-picker').label = 'Browse…'; }
});
$('#directory-path').addEventListener('input', () => {
  directoryGeneration++;
  $('#attach-repo').disabled = !directory?.is_repo || $('#directory-path').value !== directory.path;
});
$('#directory-path').addEventListener('keydown', event => { if (event.key === 'Enter') $('#directory-go').click(); });
$('#directory-up').addEventListener('click', () => browseDirectory(directory.parent).catch(error => repoError(error.message)));
$('#attach-repo').addEventListener('click', async () => {
  if (!directory?.is_repo || $('#directory-path').value !== directory.path) return;
  queueGeneration++;
  $('#attach-repo').disabled = true; $('#repo-error').hidden = true;
  try {
    session = await api('/api/repository', {path: directory.path, allow_write: $('#repo-write').checked});
    entry = proposal = sourcePath = lastBuild = null; buildGeneration++; drafts.clear(); editing.clear(); expanded.clear();
    $('#pdf-pages').hidden = $('#pdf-frame').hidden = $('#open-pdf').hidden = $('#pdf-zoom-control').hidden = true;
    $('#build-warning').hidden = true;
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
function previewHint() {
  if (!session) return;
  const selection = $('#preview-selection').value, requires = selection !== 'working';
  $('#compile').disabled = !session.repo || !session.compiler.latexmk || !session.compiler.engines.length || !$('#main-file').value ||
    waitingForPreview || (requires && (!proposal || proposal.status !== 'reviewing' || entry?.status === 'invalid'));
  $('#preview-note').textContent = !session.compiler.can_compile ? 'Install latexmk and a LaTeX engine, then restart papre. Review works without PDF compilation.' : selection === 'working' ? 'Current repository files.' :
    selection === 'accepted' ? 'Only accepted changes.' : 'Accepted and pending changes; rejected changes are excluded.';
  if (lastBuild && (lastBuild.selection !== selection || lastBuild.main !== $('#main-file').value || lastBuild.engine !== $('#engine').value)) previewStale();
}
for (const id of ['main-file', 'preview-selection', 'engine', 'compile-strict']) $('#' + id).addEventListener('change', () => { previewHint(); updatePdfLocations(); warmPreview(); });
$('#toggle-log').addEventListener('click', () => { $('#build-log').hidden = !$('#build-log').hidden; $('#toggle-log').label = $('#build-log').hidden ? 'Show log' : 'Hide log'; });
$('#pdf-zoom').addEventListener('change', () => {
  $('#pdf-pages').classList.remove('zoom-125', 'zoom-150', 'zoom-200');
  if ($('#pdf-zoom').value !== 'fit') $('#pdf-pages').classList.add('zoom-' + $('#pdf-zoom').value);
});
function previewOptions() {
  if (!session?.repo || !session.compiler.can_compile || !$('#main-file').value) return null;
  const selection = $('#preview-selection').value;
  if (selection !== 'working' && (!proposal || proposal.status !== 'reviewing' || entry?.status === 'invalid')) return null;
  return {main: $('#main-file').value, engine: $('#engine').value, selection,
    proposal: selection === 'working' ? null : proposal?.id,
    revision: selection === 'working' ? null : proposal?.revision, strict: $('#compile-strict').checked};
}
function invalidatePreview() {
  buildGeneration++; previewRun = null; previewStale();
}
function cancelBackgroundPreview() {
  invalidatePreview(); waitingForPreview = false; $('#compile').label = 'Compile preview'; previewHint();
  const repository = session?.repo_key;
  if (repository) previewRequests = previewRequests.catch(() => {}).then(() => {
    if (session.repo_key === repository && !previewRun) return api('/api/previews/cancel', {});
  }).catch(() => {});
}
function warmPreview() {
  if (!proposal || entry?.status === 'invalid' || !previewOptions()) { cancelBackgroundPreview(); return; }
  requestPreview();
}
function waitingLabel() {
  waitingForPreview = true; $('#compile').label = 'Compiling…'; $('#compile').disabled = true;
  $('#build-badge').textContent = 'Waiting for latest preview';
}
function requestPreview(show = false) {
  const options = previewOptions(); if (!options) return Promise.resolve();
  const repository = session.repo_key, key = JSON.stringify({repository, ...options});
  if (previewRun?.key === key && !previewRun.done) {
    if (show) { previewRun.show = true; waitingLabel(); }
    return previewRun.promise;
  }
  const run = {key, generation: ++buildGeneration, show: show || waitingForPreview, job: null, done: false};
  previewRun = run; previewStale();
  if (run.show) waitingLabel(); else $('#build-badge').textContent = 'Compiling in background';
  // Serialize submissions so a slow earlier HTTP request cannot preempt a newer selection.
  const submitted = previewRequests.catch(() => {}).then(() => {
    if (run.generation !== buildGeneration || session.repo_key !== repository) return null;
    return api('/api/previews', options);
  });
  previewRequests = submitted;
  run.promise = submitted.then(job => {
    if (job && run.generation === buildGeneration) { run.job = job; return pollBuild(run); }
  }).catch(error => {
    if (run.generation !== buildGeneration) return;
    $('#build-badge').textContent = run.show ? 'Build failed' : 'Background preview unavailable';
    if (run.show) { clearPreview(); $('#build-log').textContent = error.message; $('#build-log').hidden = false; toast(error.message, true); }
  }).finally(() => {
    run.done = true;
    if (run.generation !== buildGeneration) return;
    waitingForPreview = false; $('#compile').label = 'Compile preview'; previewHint();
  });
  return run.promise;
}
$('#compile').addEventListener('click', () => {
  if (drafts.size) { toast('Save or cancel the update edit before compiling.', true); return; }
  $('#preview-settings').close(false);
  $('#toggle-preview-settings').control.focus();
  requestPreview(true);
});
async function pollBuild(run) {
  while (run.generation === buildGeneration) {
    const job = await api('/api/previews/' + run.job.id);
    if (run.generation !== buildGeneration) return;
    run.job = job;
    if (run.show) $('#build-log').textContent = job.log;
    if (job.status === 'running') { await new Promise(resolve => setTimeout(resolve, 300)); continue; }
    if (job.status === 'cancelled') { $('#build-badge').textContent = 'Preview cancelled'; return; }
    if (run.show || ['succeeded', 'with_errors'].includes(job.status)) displayBuild(job);
    else {
      $('#build-badge').textContent = 'Background build failed';
      $('#build-log').textContent = job.log;
    }
    return;
  }
}
function clearPreview() {
  $('#pdf-pages').hidden = $('#pdf-frame').hidden = $('#open-pdf').hidden = $('#pdf-zoom-control').hidden = true;
  $('#pdf-empty').hidden = false; $('#pdf-frame').removeAttribute('src');
  pdfTarget = ''; $('#pdf-location').hidden = true;
}
function displayBuild(job) {
  lastBuild = job; pdfTarget = ''; $('#build-warning').hidden = true; $('#build-log').textContent = job.log;
  const uncolored = (job.changes || []).filter(change => change.decision === 'pending' && change.note).length;
  $('#annotation-note').textContent = 'Pending prose edits are green in Proposed changes; accepted and rejected edits have no review color. ' +
    (uncolored ? `${uncolored} source change(s) could not be colored safely. ` : '') +
    'Double-click changed text in Rendered pages to return to its review section. Browser PDF uses page fragments; browser support varies. ' +
    (job.warnings || []).filter(warning => /coloring|SyncTeX|locations/.test(warning)).join(' ');
  if (job.status === 'succeeded' || job.status === 'with_errors') {
    $('#build-badge').textContent = job.status === 'with_errors' ? 'Preview with errors' : `${job.seconds}s · Ready`;
    if (job.status === 'with_errors') {
      $('#build-warning').textContent = (job.error ? job.error + ' ' : '') + 'LaTeX reported errors. This preview may be incomplete; check the log.';
      $('#build-warning').hidden = false; $('#build-log').hidden = false; $('#toggle-log').label = 'Hide log';
    }
    $('#pdf-pages').replaceChildren();
    (job.pages || []).forEach((url, index) => { const figure = node('figure', 'pdf-page'), image = node('img');
      image.src = url; image.alt = `PDF page ${index + 1}`; image.loading = index === 0 ? 'eager' : 'lazy';
      image.dataset.page = String(index + 1);
      if (job.synctex_path && job.changes?.length) {
        image.dataset.sourceNavigation = 'true';
        image.title = 'Double-click changed text to return to its review section';
      }
      figure.append(image, node('figcaption', '', `Page ${index + 1} of ${job.pages.length}`)); $('#pdf-pages').append(figure); });
    $('#pdf-empty').hidden = true; $('#open-pdf').href = job.pdf; $('#open-pdf').hidden = false; selectPdfViewer();
    revealPreview();
    updatePdfLocations();
    if (job.selection !== 'working' && (proposal?.id !== job.proposal || proposal?.revision !== job.revision)) previewStale();
  } else {
    clearPreview();
    $('#build-badge').textContent = 'Build failed'; $('#build-log').hidden = false; $('#toggle-log').label = 'Hide log';
    $('#build-warning').textContent = job.error || 'LaTeX produced no new PDF. Check the compilation log.';
    $('#build-warning').hidden = false; toast('Compilation failed. See the log.', true);
  }
}
try { $('#pdf-viewer').value = localStorage.getItem('papre.pdf-viewer') === 'pages' ? 'pages' : 'browser'; } catch { /* Optional preference. */ }
function selectPdfViewer() {
  if (!lastBuild || !['succeeded', 'with_errors'].includes(lastBuild.status)) return;
  const pages = $('#pdf-viewer').value === 'pages' && lastBuild.pages?.length;
  $('#pdf-pages').hidden = $('#pdf-zoom-control').hidden = !pages;
  $('#pdf-frame').hidden = Boolean(pages);
  const url = lastBuild.pdf + pdfTarget;
  if (!pages && $('#pdf-frame').getAttribute('src') !== url) $('#pdf-frame').src = url;
}
function previewLocations() {
  const current = lastBuild && lastBuild.pdf && lastBuild.proposal === proposal?.id && lastBuild.revision === proposal?.revision &&
    lastBuild.main === $('#main-file').value && lastBuild.selection === $('#preview-selection').value &&
    lastBuild.engine === $('#engine').value && lastBuild.strict === $('#compile-strict').checked;
  return current ? Object.fromEntries((lastBuild.changes || []).map(change => [change.id, change])) : {};
}
function updatePdfLocations() {
  const locations = previewLocations();
  for (const diff of $$('#changes review-diff')) diff.locations = locations;
  const selected = locations[focusedChange] || Object.values(locations).find(change => change.page && change.decision === 'pending') ||
    Object.values(locations).find(change => change.page);
  $('#pdf-location').hidden = !selected?.page;
  if (selected?.page) {
    $('#pdf-location').textContent = `PDF p. ${selected.page} · source ¶ ${selected.paragraph}`;
    $('#pdf-location').title = `${selected.path}:${selected.line}; source paragraphs are separated by blank lines. This is the selected change's location, not the viewer's current page.`;
  }
}
function scrollToPdf(id) {
  const location = previewLocations()[id];
  if (!location?.page) { toast('Compile a matching preview to locate this change.'); return; }
  focusedChange = id; updatePdfLocations();
  if ($('#preview-pane').hidden) $('#toggle-preview').click();
  if (!$('#pdf-pages').hidden) {
    const image = $(`#pdf-pages img[data-page="${location.page}"]`);
    if (!image) { toast(`This change is on PDF page ${location.page}. Use Open PDF for pages beyond the rendered preview.`); return; }
    const position = () => {
      const ratio = location.y / (image.naturalHeight * 72 / 120);
      $('#pdf-pages').scrollTop = image.offsetTop + ratio * image.clientHeight - 50;
    };
    if (image.complete) position(); else image.addEventListener('load', position, {once: true});
  } else {
    // Native PDF plugins expose no portable scrolling API; retain their own viewer.
    pdfTarget = '#page=' + location.page;
    $('#pdf-frame').src = lastBuild.pdf + pdfTarget;
    $('#open-pdf').href = lastBuild.pdf + pdfTarget;
  }
}
function focusReviewSection(id) {
  const section = $$('article[data-hunk]').find(item => item.dataset.hunk === id);
  if (!section) return;
  sourcePath = null; $('#source-view').hidden = true; $('#review-view').hidden = false;
  // Restore the review column when PDF is expanded, without hiding the PDF.
  if ($('.app-layout').classList.contains('pdf-expanded')) $('#expand-preview').click();
  focusedChange = id; updatePdfLocations();
  section.scrollIntoView({block: 'center', behavior: 'smooth'});
  section.classList.add('pdf-linked'); section.tabIndex = -1; section.focus({preventScroll: true});
  setTimeout(() => section.classList.remove('pdf-linked'), 2500);
  history.replaceState(null, '', '#change-' + id);
}
$('#pdf-pages').addEventListener('dblclick', async event => {
  const image = event.target.closest('img[data-source-navigation]');
  const build = lastBuild;
  if (!image || !build || !Object.keys(previewLocations()).length) return;
  const bounds = image.getBoundingClientRect();
  try {
    const result = await api(`/api/previews/${build.id}/source`, {page: Number(image.dataset.page),
      x: (event.clientX - bounds.left) / bounds.width, y: (event.clientY - bounds.top) / bounds.height});
    if (lastBuild?.id !== build.id || proposal?.id !== result.proposal || proposal?.revision !== result.revision) return;
    focusReviewSection(result.hunk);
  } catch (error) { toast(error.message); }
});
window.addEventListener('hashchange', () => {
  const id = location.hash.startsWith('#change-') ? location.hash.slice(8) : null;
  if (id && hunks().some(hunk => hunk.id === id)) focusReviewSection(id);
});
$('#pdf-viewer').addEventListener('change', () => {
  try { localStorage.setItem('papre.pdf-viewer', $('#pdf-viewer').value); } catch { /* Optional preference. */ }
  selectPdfViewer();
});
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
      entry = proposal = null; cancelBackgroundPreview(); if (!sourcePath) renderReview();
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
