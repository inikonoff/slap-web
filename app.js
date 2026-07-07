'use strict';

// ── State ─────────────────────────────────────────────────────────────────────

let selectedFiles = [];
let pollInterval = null;

// ── Elements ──────────────────────────────────────────────────────────────────

const dropZone      = document.getElementById('drop-zone');
const dropContent   = document.getElementById('drop-content');
const thumbGrid     = document.getElementById('thumb-grid');
const btnSlap       = document.getElementById('btn-slap');

const stateUpload      = document.getElementById('state-upload');
const stateProcessing  = document.getElementById('state-processing');
const stateResult      = document.getElementById('state-result');

const progressBar  = document.getElementById('progress-bar');
const statusText   = document.getElementById('status-text');
const queueLabel   = document.getElementById('queue-label');

const resultImg    = document.getElementById('result-img');
const btnDownload  = document.getElementById('btn-download');

// ── Drag & Drop ───────────────────────────────────────────────────────────────

function onDragOver(e) {
  e.preventDefault();
  dropZone.classList.add('drag-over');
}
function onDragLeave() {
  dropZone.classList.remove('drag-over');
}
function onDrop(e) {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  handleFiles([...e.dataTransfer.files]);
}
function onFilesSelected(e) {
  handleFiles([...e.target.files]);
}

function handleFiles(files) {
  const jpegs = files.filter(f => f.type === 'image/jpeg' || f.type === 'image/jpg');
  if (jpegs.length === 0) return;

  selectedFiles = jpegs.slice(0, 30);
  renderThumbnails();
  btnSlap.disabled = selectedFiles.length < 2;
}

function renderThumbnails() {
  thumbGrid.innerHTML = '';

  selectedFiles.forEach(file => {
    const img = document.createElement('img');
    img.alt = file.name;
    const reader = new FileReader();
    reader.onload = e => { img.src = e.target.result; };
    reader.readAsDataURL(file);
    thumbGrid.appendChild(img);
  });

  const label = document.createElement('div');
  label.className = 'thumb-count';
  label.textContent = `${selectedFiles.length} кадр${plural(selectedFiles.length)} выбрано`;
  thumbGrid.appendChild(label);

  dropContent.hidden = true;
  thumbGrid.hidden = false;
}

function plural(n) {
  if (n % 10 === 1 && n % 100 !== 11) return '';
  if ([2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100)) return 'а';
  return 'ов';
}

// ── Stack ─────────────────────────────────────────────────────────────────────

async function startStack() {
  if (selectedFiles.length < 2) return;

  showState('processing');
  setProgress(2, 'Загрузка файлов на сервер...');

  const formData = new FormData();
  selectedFiles.forEach(f => formData.append('files', f));

  let jobId;
  try {
    const res = await fetch('/api/upload', { method: 'POST', body: formData });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Ошибка загрузки');
    }
    const data = await res.json();
    jobId = data.job_id;
  } catch (e) {
    showError(e.message);
    return;
  }

  pollInterval = setInterval(() => pollStatus(jobId), 1000);
}

async function pollStatus(jobId) {
  let data;
  try {
    const res = await fetch(`/api/status/${jobId}`);
    if (!res.ok) throw new Error('Статус недоступен');
    data = await res.json();
  } catch {
    return; // transient network error, retry next tick
  }

  setProgress(data.progress || 0, data.status_text || '');

  if (data.queue_position > 1) {
    queueLabel.textContent = `Очередь: вы №${data.queue_position}`;
    queueLabel.hidden = false;
  } else {
    queueLabel.hidden = true;
  }

  if (data.state === 'done') {
    clearInterval(pollInterval);
    showResult(data.result_url);
  } else if (data.state === 'error') {
    clearInterval(pollInterval);
    showError(data.error || 'Неизвестная ошибка');
  }
}

function setProgress(pct, text) {
  progressBar.style.width = `${pct}%`;
  statusText.textContent = text;
}

// ── Result ────────────────────────────────────────────────────────────────────

function showResult(url) {
  resultImg.src = url;
  btnDownload.href = url;
  btnDownload.download = `slap_result_${Date.now()}.jpg`;
  showState('result');
}

// ── Error ─────────────────────────────────────────────────────────────────────

function showError(msg) {
  showState('upload');
  // brief flash on card border
  const card = document.getElementById('card');
  card.style.borderColor = '#e8330a';
  setTimeout(() => { card.style.borderColor = ''; }, 2000);
  alert(`Ошибка: ${msg}`);
}

// ── UI helpers ────────────────────────────────────────────────────────────────

function showState(state) {
  stateUpload.hidden     = state !== 'upload';
  stateProcessing.hidden = state !== 'processing';
  stateResult.hidden     = state !== 'result';
}

function reset() {
  clearInterval(pollInterval);
  selectedFiles = [];
  dropContent.hidden = false;
  thumbGrid.hidden = true;
  thumbGrid.innerHTML = '';
  btnSlap.disabled = true;
  document.getElementById('file-input').value = '';
  setProgress(0, '');
  queueLabel.hidden = true;
  resultImg.src = '';
  showState('upload');
}

// ── Modal ─────────────────────────────────────────────────────────────────────

function openModal()  { document.getElementById('modal-overlay').hidden = false; }
function closeModal() { document.getElementById('modal-overlay').hidden = true;  }

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeModal();
});
