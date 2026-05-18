/* ChatDesk — Main JavaScript */

// ─────────────────────────────────────────────
// Flash Messages Auto-dismiss
// ─────────────────────────────────────────────
document.querySelectorAll('.flash').forEach(el => {
  el.addEventListener('click', () => el.remove());
  setTimeout(() => el.remove(), 4500);
});

// ─────────────────────────────────────────────
// Modal Helpers
// ─────────────────────────────────────────────
function openModal(id) {
  const m = document.getElementById(id);
  if (m) m.classList.add('open');
}

function closeModal(id) {
  const m = document.getElementById(id);
  if (m) m.classList.remove('open');
}

// Close modal on overlay click
document.querySelectorAll('.modal-overlay').forEach(overlay => {
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.classList.remove('open');
  });
});

// ─────────────────────────────────────────────
// Confirm Delete Helper
// ─────────────────────────────────────────────
function confirmAction(message, formId) {
  if (confirm(message)) {
    const form = document.getElementById(formId);
    if (form) form.submit();
  }
}

// ─────────────────────────────────────────────
// Dashboard Live Polling
// ─────────────────────────────────────────────
function initDashboardPolling() {
  const progressEl = document.getElementById('campaign-progress');
  const progressBar = document.getElementById('progress-bar');
  const progressText = document.getElementById('progress-text');
  const statusBadge = document.getElementById('campaign-status');
  const logContainer = document.getElementById('activity-log');
  const sentTodayEl = document.getElementById('stat-sent-today');
  const onlineEl = document.getElementById('stat-online');
  const pauseBtn = document.getElementById('btn-pause');
  const resumeBtn = document.getElementById('btn-resume');

  if (!progressEl) return; // Not on dashboard

  async function poll() {
    try {
      const res = await fetch('/api/dashboard-stats');
      const d = await res.json();

      // Progress
      if (d.total > 0) {
        const pct = Math.round((d.sent / d.total) * 100);
        if (progressBar) progressBar.style.width = pct + '%';
        if (progressText) progressText.textContent = `${d.sent} / ${d.total} sent`;
      }

      // Status badge
      if (statusBadge) {
        if (d.running && !d.paused) {
          statusBadge.textContent = 'Running';
          statusBadge.className = 'badge badge-green';
        } else if (d.paused) {
          statusBadge.textContent = 'Paused';
          statusBadge.className = 'badge badge-orange';
        } else {
          statusBadge.textContent = 'Idle';
          statusBadge.className = 'badge badge-gray';
        }
      }

      // Pause/Resume buttons
      if (pauseBtn) pauseBtn.style.display = (d.running && !d.paused) ? 'inline-flex' : 'none';
      if (resumeBtn) resumeBtn.style.display = d.paused ? 'inline-flex' : 'none';

      // Activity log
      if (logContainer && d.log) {
        logContainer.innerHTML = d.log.map(l =>
          `<div class="log-entry">${escapeHtml(l)}</div>`
        ).join('');
      }

      // Stats
      if (sentTodayEl) sentTodayEl.textContent = d.sent_today;
      if (onlineEl) onlineEl.textContent = d.online_users;

    } catch(e) { /* ignore network errors */ }
  }

  poll();
  setInterval(poll, 3000);
}

function escapeHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

// ─────────────────────────────────────────────
// Pause / Resume / Stop
// ─────────────────────────────────────────────
async function campaignPause() {
  await fetch('/campaigns/pause', { method: 'POST' });
}

async function campaignResume() {
  await fetch('/campaigns/resume', { method: 'POST' });
}

function campaignStop() {
  if (!confirm('Stop this campaign? This cannot be undone.')) return;
  fetch('/campaigns/stop', { method: 'POST' })
    .then(() => location.reload());
}

// ─────────────────────────────────────────────
// Template Loader (Campaigns page)
// ─────────────────────────────────────────────
function loadTemplate(selectEl) {
  const msg = selectEl.options[selectEl.selectedIndex].dataset.message;
  if (msg) {
    const textarea = document.getElementById('campaign-message');
    if (textarea) textarea.value = msg;
  }
}

// ─────────────────────────────────────────────
// Character Counter for message textarea
// ─────────────────────────────────────────────
function initCharCounter() {
  const textarea = document.getElementById('campaign-message');
  const counter = document.getElementById('char-counter');
  if (!textarea || !counter) return;

  function update() {
    const len = textarea.value.length;
    counter.textContent = `${len} characters`;
    counter.style.color = len > 900 ? 'var(--danger-dark)' : 'var(--text-muted)';
  }
  textarea.addEventListener('input', update);
  update();
}

// ─────────────────────────────────────────────
// CSV Upload Preview
// ─────────────────────────────────────────────
function initCSVPreview() {
  const input = document.getElementById('csv-input');
  const preview = document.getElementById('csv-preview');
  if (!input || !preview) return;

  input.addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const label = document.getElementById('csv-label');
    if (label) label.textContent = file.name;

    const reader = new FileReader();
    reader.onload = (ev) => {
      const lines = ev.target.result.trim().split('\n');
      const count = Math.max(0, lines.length - 1); // minus header
      preview.textContent = `${count} rows detected in "${file.name}"`;
      preview.style.display = 'block';
    };
    reader.readAsText(file);
  });
}

// ─────────────────────────────────────────────
// Mobile sidebar toggle
// ─────────────────────────────────────────────
function toggleSidebar() {
  const sb = document.querySelector('.sidebar');
  if (sb) sb.classList.toggle('open');
}

// ─────────────────────────────────────────────
// Init
// ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initDashboardPolling();
  initCharCounter();
  initCSVPreview();
});
