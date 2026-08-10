'use strict';

const $ = (id) => document.getElementById(id);

let pollTimer = null;
let nativePicker = false;

function showError(message) {
  const box = $('error');
  if (!message) { box.classList.add('hidden'); box.textContent = ''; return; }
  box.textContent = message;
  box.classList.remove('hidden');
}

async function api(path, options) {
  const res = await fetch(path, options);
  let body = null;
  try { body = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    throw new Error((body && body.detail) || `${res.status} ${res.statusText}`);
  }
  return body;
}

function humanBytes(n) {
  if (n === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = Math.abs(n), i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${i === 0 ? v : v.toFixed(1)} ${units[i]}`;
}

// ---------------------------------------------------------------- settings

async function loadSettings() {
  const s = await api('/api/settings');
  nativePicker = s.native_picker;
  $('backup-root').textContent = s.backup_root || 'not set';
  if (s.last_source && !$('source').value) $('source').value = s.last_source;

  const warn = $('root-warning');
  if (s.backup_root && !s.backup_root_exists) {
    warn.textContent = 'That folder is not currently reachable — is the drive mounted?';
    warn.classList.remove('hidden');
  } else {
    warn.classList.add('hidden');
  }

  if (!nativePicker) {
    for (const id of ['pick-root', 'pick-source']) {
      const btn = $(id);
      btn.disabled = true;
      btn.title = 'The native picker requires macOS. Type an absolute path instead.';
    }
  }
  $('start').disabled = !s.backup_root;
}

async function pickFolder(prompt) {
  const out = await api('/api/pick-folder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt }),
  });
  return out.cancelled ? null : out.path;
}

// ---------------------------------------------------------------- progress

function renderProgress(state) {
  const p = state.progress || {};
  const card = $('progress-card');
  if (p.phase === 'idle') { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');

  const pct = p.bytes_total > 0 ? Math.min(100, (p.bytes_done / p.bytes_total) * 100) : 0;
  $('bar-fill').style.width = `${pct}%`;

  const phase = { scanning: 'Scanning', comparing: 'Comparing hashes',
                  copying: 'Copying', finished: 'Finished' }[p.phase] || p.phase;
  $('progress-line').textContent =
    `${phase} — ${p.files_done}/${p.files_total} files · ` +
    `${humanBytes(p.bytes_done)} / ${humanBytes(p.bytes_total)} · ` +
    `${p.copied} copied, ${p.skipped} skipped, ${p.conflicts} conflicts, ${p.failed} failed`;
  $('current-file').textContent = p.current_file || '—';
}

function renderReport(report) {
  const card = $('report-card');
  if (!report) { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');

  const outcome = $('outcome');
  outcome.textContent = report.outcome;
  outcome.className = `outcome ${report.passed ? 'pass' : 'fail'}`;

  $('c-copied').textContent = report.totals.copied;
  $('c-skipped').textContent = report.totals.skipped;
  $('c-conflicts').textContent = report.totals.conflicts;
  $('c-failed').textContent = report.totals.failed;
  $('manifest-path').textContent = report.manifest_path;

  const detail = $('detail');
  detail.innerHTML = '';
  const section = (title, items, cls) => {
    if (!items || items.length === 0) return;
    const h = document.createElement('h3');
    h.textContent = title;
    const ul = document.createElement('ul');
    for (const item of items) {
      const li = document.createElement('li');
      li.className = cls;
      li.textContent = item;
      ul.appendChild(li);
    }
    detail.appendChild(h);
    detail.appendChild(ul);
  };
  section('Conflicts (destination left untouched)', report.conflicts, 'conflict');
  section('Failures', (report.failures || []).map((f) => `${f.rel_path} — ${f.error}`), 'failure');
  section('Skipped symlinks', (report.skipped_symlinks || []).map((s) => `${s.rel_path} → ${s.target}`), '');
  section('Notes', report.notes, '');
}

async function poll() {
  try {
    const state = await api('/api/backup/status');
    renderProgress(state);
    renderReport(state.report);
    if (state.error) showError(state.error);

    if (!state.running) {
      clearInterval(pollTimer);
      pollTimer = null;
      $('start').disabled = false;
    }
  } catch (err) {
    showError(err.message);
    clearInterval(pollTimer);
    pollTimer = null;
    $('start').disabled = false;
  }
}

// ---------------------------------------------------------------- actions

$('pick-root').addEventListener('click', async () => {
  showError(null);
  try {
    const path = await pickFolder('Choose the backup destination');
    if (!path) return;
    await api('/api/settings/backup-root', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ backup_root: path }),
    });
    await loadSettings();
  } catch (err) { showError(err.message); }
});

$('pick-source').addEventListener('click', async () => {
  showError(null);
  try {
    const path = await pickFolder('Choose the folder to back up');
    if (path) $('source').value = path;
  } catch (err) { showError(err.message); }
});

$('preflight').addEventListener('click', async () => {
  showError(null);
  $('preflight-out').textContent = 'Hashing the destination to size the run…';
  try {
    const source = encodeURIComponent($('source').value.trim());
    const out = await api(`/api/backup/preflight?source=${source}`);
    $('preflight-out').textContent = out.message;
  } catch (err) {
    $('preflight-out').textContent = '';
    showError(err.message);
  }
});

$('start').addEventListener('click', async () => {
  showError(null);
  $('report-card').classList.add('hidden');
  $('preflight-out').textContent = '';
  $('start').disabled = true;
  try {
    await api('/api/backup/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: $('source').value.trim() }),
    });
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(poll, 400);
    poll();
  } catch (err) {
    showError(err.message);
    $('start').disabled = false;
  }
});

loadSettings().catch((err) => showError(err.message));
poll();
