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

// ── Preferencias del usuario (client-side, localStorage) ─────────────────────
// Helper para las tres preferencias por defecto (vista de inicio, orden de la
// colección, plataforma). Vive en ui.js (2º módulo) para que app.js (último)
// pueda llamar a getPref en tiempo de carga al inicializar `collectionSort`
// (PS-003). Se guardan bajo una única clave `cinebox_prefs` (convención
// `cinebox_`). Cada valor almacenado es UNTRUSTED (el usuario puede editar
// localStorage a mano en devtools) → se valida contra un allow-list fijo en la
// lectura antes de aplicarse (SE-*). Todo acceso a localStorage va en try/catch
// (el modo privado puede lanzar) → degrada a los valores por defecto, sin crash.

// Allow-lists de las preferencias con lista fija. `PLATFORMS` ya existe en
// collection.js (3º módulo) y solo se lee en cuerpos de manejador (tiempo de
// llamada), así que no hay problema de orden de carga con ella.
const HOME_VIEWS = ["collection-view", "discover-view", "stats-view", "lists-view"];
const COLLECTION_SORTS = ["recent", "title-asc", "year-desc", "rating-desc", "pending-first", "watched-first"];

const PREFS_STORAGE_KEY = "cinebox_prefs";

// Devuelve el objeto de preferencias parseado, o {} ante cualquier fallo
// (localStorage inaccesible, JSON malformado, valor no-objeto).
function readPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return (parsed && typeof parsed === "object" && !Array.isArray(parsed)) ? parsed : {};
  } catch (e) {
    return {};
  }
}

// Devuelve el valor almacenado SOLO si está en `allowedList`; en caso contrario
// (ausente, corrupto, fuera del allow-list) devuelve `fallback`. Este es el
// guardián del valor inválido/corrupto (AC-11): nunca se pasa una cadena
// arbitraria a showView(...), al DOM o a un atributo.
function getPref(name, allowedList, fallback) {
  const prefs = readPrefs();
  const value = prefs[name];
  return allowedList.includes(value) ? value : fallback;
}

// Fusiona `value` en el objeto de preferencias y persiste; `null`/vacío borra
// ese campo (vuelve a "sin preferencia"), dejando los demás intactos. No-op
// silencioso si localStorage lanza (modo privado).
function setPref(name, value) {
  try {
    const prefs = readPrefs();
    if (value === null || value === undefined || value === "") delete prefs[name];
    else prefs[name] = value;
    localStorage.setItem(PREFS_STORAGE_KEY, JSON.stringify(prefs));
  } catch (e) {
    /* no-op: sin persistencia si el almacenamiento no está disponible */
  }
}
