'use strict';

// ── Config ────────────────────────────────────────────────────────────────────

const MAX_FILES = 30;

function getMaxSide(count) {
  if (count <= 15) return 3072;
  return 2048;
}

// ── State ─────────────────────────────────────────────────────────────────────

let selectedFiles = [];
let pollInterval  = null;
let notifyEnabled = false;

// ── Elements ──────────────────────────────────────────────────────────────────

const dropZone         = document.getElementById('drop-zone');
const dropContent      = document.getElementById('drop-content');
const thumbGrid        = document.getElementById('thumb-grid');
const btnSlap          = document.getElementById('btn-slap');
const stateUpload      = document.getElementById('state-upload');
const stateProcessing  = document.getElementById('state-processing');
const stateResult      = document.getElementById('state-result');
const progressBar      = document.getElementById('progress-bar');
const statusText       = document.getElementById('status-text');
const queueLabel       = document.getElementById('queue-label');
const resultImg        = document.getElementById('result-img');
const btnDownload      = document.getElementById('btn-download');

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

  if (jpegs.length > MAX_FILES) {
    const excess = jpegs.length - MAX_FILES;
    alert(
      `Вы выбрали ${jpegs.length} файлов — это больше максимума (${MAX_FILES}).\n` +
      `Пожалуйста, удалите лишние ${excess} файл${pluralFile(excess)} и попробуйте снова.`
    );
    return; // не принимаем — пусть пользователь сам отберёт нужные
  }

  selectedFiles = jpegs;
  renderThumbnails();
  btnSlap.disabled = selectedFiles.length < 2;
}

function renderThumbnails() {
  thumbGrid.innerHTML = '';

  selectedFiles.forEach((file, index) => {
    const wrap = document.createElement('div');
    wrap.className = 'thumb-wrap';

    const img = document.createElement('img');
    img.alt = file.name;
    img.title = file.name;
    const reader = new FileReader();
    reader.onload = e => { img.src = e.target.result; };
    reader.readAsDataURL(file);

    const btn = document.createElement('button');
    btn.className = 'thumb-remove';
    btn.innerHTML = '✕';
    btn.title = 'Удалить кадр';
    btn.onclick = (e) => {
      e.stopPropagation();
      removeFile(index);
    };

    wrap.appendChild(img);
    wrap.appendChild(btn);
    thumbGrid.appendChild(wrap);
  });

  const maxSide = getMaxSide(selectedFiles.length);
  const label = document.createElement('div');
  label.className = 'thumb-count';
  label.textContent =
    `${selectedFiles.length} кадр${plural(selectedFiles.length)} выбрано · ` +
    `будут уменьшены до ${maxSide}px`;
  thumbGrid.appendChild(label);

  dropContent.hidden = true;
  thumbGrid.hidden   = false;
}

function removeFile(index) {
  selectedFiles = selectedFiles.filter((_, i) => i !== index);
  if (selectedFiles.length === 0) {
    dropContent.hidden = false;
    thumbGrid.hidden   = true;
    thumbGrid.innerHTML = '';
    btnSlap.disabled   = true;
  } else {
    renderThumbnails();
    btnSlap.disabled = selectedFiles.length < 2;
  }
}

function plural(n) {
  if (n % 10 === 1 && n % 100 !== 11) return '';
  if ([2,3,4].includes(n % 10) && ![12,13,14].includes(n % 100)) return 'а';
  return 'ов';
}

function pluralFile(n) {
  if (n % 10 === 1 && n % 100 !== 11) return '';
  if ([2,3,4].includes(n % 10) && ![12,13,14].includes(n % 100)) return 'а';
  return 'ов';
}

// ── Format select ────────────────────────────────────────────────────────────

function onFormatChange() {
  // ничего не делаем — значение читается при startStack()
}

// ── Client-side resize ────────────────────────────────────────────────────────

function resizeImage(file, maxSide) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(url);
      const { width: w, height: h } = img;
      let nw = w, nh = h;
      if (Math.max(w, h) > maxSide) {
        const scale = maxSide / Math.max(w, h);
        nw = Math.round(w * scale);
        nh = Math.round(h * scale);
      }
      const canvas = document.createElement('canvas');
      canvas.width  = nw;
      canvas.height = nh;
      canvas.getContext('2d').drawImage(img, 0, 0, nw, nh);
      // PNG — без потерь на этапе подготовки, сервер сам сожмёт в выбранный формат
      canvas.toBlob(blob => {
        if (blob) resolve(blob);
        else reject(new Error('Canvas toBlob failed'));
      }, 'image/png');
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error(`Не удалось прочитать ${file.name}`));
    };
    img.src = url;
  });
}

async function resizeAll() {
  const maxSide = getMaxSide(selectedFiles.length);
  const blobs = [];
  for (let i = 0; i < selectedFiles.length; i++) {
    setProgress(
      Math.round((i / selectedFiles.length) * 30),
      `Подготовка кадра ${i + 1} из ${selectedFiles.length} (${maxSide}px)...`
    );
    blobs.push(await resizeImage(selectedFiles[i], maxSide));
  }
  return blobs;
}

// ── Stack ─────────────────────────────────────────────────────────────────────

async function startStack() {
  if (selectedFiles.length < 2) return;

  showState('processing');
  renderNotifyPrompt();
  setProgress(0, 'Подготовка изображений...');

  let blobs;
  try {
    blobs = await resizeAll();
  } catch (e) {
    showError(e.message);
    return;
  }

  setProgress(32, 'Загрузка на сервер...');
  const formData = new FormData();
  const fmt = document.getElementById('fmt-select').value;        // 'jpeg' | 'png'
  const method = document.getElementById('method-select').value;  // 'sharp' | 'wavelet'
  // Отправляем как PNG — без потерь, сервер примет любой формат
  blobs.forEach((blob, i) => {
    const name = selectedFiles[i].name.replace(/\.jpe?g$/i, '.png');
    formData.append('files', blob, name);
  });
  formData.append('fmt', fmt);
  formData.append('method', method);

  let jobId;
  try {
    const res = await fetch('/api/upload', { method: 'POST', body: formData });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Ошибка загрузки');
    }
    jobId = (await res.json()).job_id;
  } catch (e) {
    showError(e.message);
    return;
  }

  setProgress(35, 'Файлы загружены, начинаем стекинг...');
  pollInterval = setInterval(() => pollStatus(jobId), 1000);
}

// ── Polling ───────────────────────────────────────────────────────────────────

async function pollStatus(jobId) {
  let data;
  try {
    const res = await fetch(`/api/status/${jobId}`);
    if (!res.ok) throw new Error();
    data = await res.json();
  } catch {
    return;
  }

  // Прогресс сервера 0–100 → масштабируем в 35–100
  const pct = 35 + Math.round((data.progress || 0) * 0.65);
  setProgress(pct, data.status_text || '');

  if (data.queue_position > 1) {
    queueLabel.textContent = `В очереди: вы №${data.queue_position}`;
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

// ── Progress ──────────────────────────────────────────────────────────────────

function setProgress(pct, text) {
  progressBar.style.width = `${Math.min(pct, 100)}%`;
  statusText.textContent  = text;
}

// ── Notifications ─────────────────────────────────────────────────────────────

const NOTIFY_PREF_KEY = 'slap_notify_pref';

function renderNotifyPrompt() {
  const container = document.getElementById('notify-prompt');
  const supported = 'Notification' in window;

  if (!supported || Notification.permission === 'denied') {
    // Уведомления недоступны на уровне браузера — не показываем предложение
    notifyEnabled = false;
    container.innerHTML = '';
    return;
  }

  const storedPref = localStorage.getItem(NOTIFY_PREF_KEY) === 'true';
  notifyEnabled = storedPref && Notification.permission === 'granted';

  container.innerHTML =
    '<label class="notify-checkbox-label">' +
    `<input type="checkbox" id="notify-checkbox" ${notifyEnabled ? 'checked' : ''} onchange="onNotifyToggle()" />` +
    '<span>🔔 Уведомить, когда будет готово?</span>' +
    '</label>';
}

function onNotifyToggle() {
  const checkbox = document.getElementById('notify-checkbox');
  if (!checkbox) return;

  if (!checkbox.checked) {
    // Пользователь снял галочку — просто выключаем, разрешение браузера не трогаем
    notifyEnabled = false;
    localStorage.setItem(NOTIFY_PREF_KEY, 'false');
    return;
  }

  if (Notification.permission === 'granted') {
    notifyEnabled = true;
    localStorage.setItem(NOTIFY_PREF_KEY, 'true');
    return;
  }

  Notification.requestPermission().then(permission => {
    notifyEnabled = permission === 'granted';
    if (notifyEnabled) {
      localStorage.setItem(NOTIFY_PREF_KEY, 'true');
    } else {
      checkbox.checked = false;
      if (permission === 'denied') {
        alert('Уведомления заблокированы в настройках браузера.');
      }
    }
  });
}

function notifyDone() {
  if (!notifyEnabled || Notification.permission !== 'granted') return;

  const options = {
    body: 'Готово — забирай свой стек 💥',
    icon: '/icon-192.png',
  };

  // На Android/Chrome при активном Service Worker прямой вызов
  // new Notification() запрещён и выбрасывает исключение — нужно
  // использовать ServiceWorkerRegistration.showNotification()
  if (navigator.serviceWorker && navigator.serviceWorker.controller) {
    navigator.serviceWorker.ready
      .then(reg => reg.showNotification('SLAP!', options))
      .catch(err => console.warn('Notification error:', err));
  } else {
    try {
      const n = new Notification('SLAP!', options);
      n.onclick = () => {
        window.focus();
        n.close();
      };
    } catch (err) {
      console.warn('Notification error:', err);
    }
  }
}

// ── Result ────────────────────────────────────────────────────────────────────

const METHOD_WORDS = { sharp: 'snap', wavelet: 'weave', hybrid: 'prism' };

function formatTimestamp(date) {
  const pad = n => String(n).padStart(2, '0');
  return `${pad(date.getDate())}${pad(date.getMonth() + 1)}${pad(date.getHours())}${pad(date.getMinutes())}`;
}

function showResult(url) {
  resultImg.src    = url;
  btnDownload.href = url;

  const method     = document.getElementById('method-select').value;
  const fmt        = document.getElementById('fmt-select').value;
  const methodWord = METHOD_WORDS[method] || method;
  const ext        = fmt === 'png' ? 'png' : 'jpg';
  const timestamp  = formatTimestamp(new Date());
  btnDownload.download = `slap_${timestamp}_${methodWord}.${ext}`;

  // Уведомление не должно блокировать показ результата ни при каких условиях
  try { notifyDone(); } catch (err) { console.warn('Notification error:', err); }
  showState('result');
}

// ── Error ─────────────────────────────────────────────────────────────────────

function showError(msg) {
  clearInterval(pollInterval);
  showState('upload');
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
  selectedFiles      = [];
  dropContent.hidden = false;
  thumbGrid.hidden   = true;
  thumbGrid.innerHTML = '';
  btnSlap.disabled   = true;
  document.getElementById('file-input').value = '';
  setProgress(0, '');
  queueLabel.hidden = true;
  resultImg.src     = '';
  showState('upload');
}

// ── Modal ─────────────────────────────────────────────────────────────────────

function openModal()  { document.getElementById('modal-overlay').hidden = false; }
function closeModal() { document.getElementById('modal-overlay').hidden = true;  }

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeModal();
});
