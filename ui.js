// Mi Cineteca — helpers de presentación: DOM, escaping, formato y fragmentos HTML.

const STAR = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="m12 2 3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14l-5-4.87 6.91-1.01Z"/></svg>';
const FILM = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20" rx="2.5"/><path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5"/></svg>';

const el = (id) => document.getElementById(id);

const messageEl = el("message");

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const mediaIcon = (mt) => (mt === "tv" ? "📺" : "🎬");
const mediaLabel = (mt) => (mt === "tv" ? "Serie" : "Película");

function notePreview(note) {
  const text = String(note || "").trim();
  if (!text) return "";
  return text.length > 72 ? text.slice(0, 72).trimEnd() + "…" : text;
}

function todayIsoDate() {
  return new Date().toISOString().slice(0, 10);
}

function showMessage(text, type) {
  if (!text) { messageEl.hidden = true; return; }
  messageEl.textContent = text;
  messageEl.className = "alert" + (type ? " " + type : "");
  messageEl.hidden = false;
}

function posterHtml(m) {
  if (m.poster_url) return `<img src="${esc(m.poster_url)}" alt="${esc(m.title)}" loading="lazy">`;
  return `<div class="poster-fallback">${FILM}<span>${esc(m.title)}</span></div>`;
}

function starsHtml(rating) {
  let out = "";
  for (let i = 1; i <= 5; i++) {
    out += `<button class="star ${rating >= i ? "on" : ""}" data-star="${i}" type="button" aria-label="${i} estrellas">${STAR}</button>`;
  }
  return out;
}
