// Mi Cineteca — lógica de cliente. Habla con el backend local (server.py).

const STAR = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="m12 2 3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14l-5-4.87 6.91-1.01Z"/></svg>';
const FILM = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20" rx="2.5"/><path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5"/></svg>';

const el = (id) => document.getElementById(id);
const collectionEl = el("collection");
const resultsEl = el("results");
const resultsSection = el("results-section");
const messageEl = el("message");
const emptyEl = el("empty");
const statsEl = el("stats");
const modalEl = el("modal");
const modalContent = el("modal-content");
const pickPanelEl = el("pick-panel");

let movies = [];
let filter = "todas";
let mediaFilter = "todo";
let collectionMediaFilter = "todo";
let collectionQuery = "";
let collectionSort = "recent";
let lastResults = [];
let resultsMode = "search";
let pickedMovie = null;
let editingProgressId = null;
let editingNoteId = null;

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

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, data };
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

function renderStats() {
  const total = movies.length;
  if (!total) { statsEl.hidden = true; return; }
  const vistas = movies.filter((m) => m.status === "vista").length;
  const series = movies.filter((m) => m.media_type === "tv").length;
  const rated = movies.filter((m) => m.rating);
  const avg = rated.length ? (rated.reduce((a, m) => a + m.rating, 0) / rated.length).toFixed(1) : "—";
  statsEl.hidden = false;
  statsEl.innerHTML = `
    <div><strong>${total}</strong><span>Total</span></div>
    <div><strong>${vistas}</strong><span>Vistas</span></div>
    <div><strong>${total - vistas}</strong><span>Por ver</span></div>
    <div><strong>${total - series}/${series}</strong><span>Pelis / Series</span></div>
    <div><strong>${avg}</strong><span>Nota media</span></div>`;
}

function recentValue(m) {
  return Date.parse(m.created_at || "") || Number(m.id || 0);
}

function yearValue(m) {
  const year = Number.parseInt(m.year, 10);
  return Number.isFinite(year) ? year : -1;
}

function ratingValue(m) {
  const rating = Number(m.rating);
  return Number.isFinite(rating) ? rating : -1;
}

function byRecent(a, b) {
  return recentValue(b) - recentValue(a);
}

function sortCollection(list) {
  const sorted = [...list];
  const byTitle = (a, b) => String(a.title || "").localeCompare(String(b.title || ""), "es", { sensitivity: "base" }) || byRecent(a, b);
  const statusRank = (preferred) => (m) => (m.status === preferred ? 0 : 1);
  const comparators = {
    recent: byRecent,
    "title-asc": byTitle,
    "year-desc": (a, b) => (yearValue(b) - yearValue(a)) || byTitle(a, b),
    "rating-desc": (a, b) => (ratingValue(b) - ratingValue(a)) || byRecent(a, b),
    "pending-first": (a, b) => (statusRank("pendiente")(a) - statusRank("pendiente")(b)) || byRecent(a, b),
    "watched-first": (a, b) => (statusRank("vista")(a) - statusRank("vista")(b)) || byRecent(a, b),
  };
  sorted.sort(comparators[collectionSort] || byRecent);
  return sorted;
}

function renderCollection() {
  const q = collectionQuery.trim().toLowerCase();
  const list = sortCollection(movies.filter((m) =>
    (filter === "todas" || m.status === filter) &&
    (collectionMediaFilter === "todo" || (m.media_type || "movie") === collectionMediaFilter) &&
    (!q || `${m.title} ${m.year || ""}`.toLowerCase().includes(q))));
  emptyEl.hidden = list.length !== 0;
  emptyEl.textContent = movies.length
    ? "No hay títulos que coincidan con estos filtros."
    : "Aún no tienes nada. Ve a Descubrir para buscar películas o series, o añádela manualmente.";
  const _html = list.map((m) => `
    <article class="card" data-id="${m.id}">
      <div class="poster ${m.tmdb_id ? "cursor-pointer" : ""}" ${m.tmdb_id ? `data-tmdb="${esc(m.tmdb_id)}" data-type="${esc(m.media_type)}"` : ""}>
        ${posterHtml(m)}
        <span class="status-badge ${m.status}">${m.status === "vista" ? "Vista" : "Por ver"}</span>
        <span class="media-badge">${mediaIcon(m.media_type)}</span>
      </div>
      <div class="card-body">
        <div>
          <div class="card-title">${esc(m.title)}</div>
          <div class="card-year">${esc(m.year) || "—"}</div>
          ${m.watched_at ? `<button class="date-btn" data-action="date" type="button" title="Editar fecha de visionado">📅 Vista el ${esc(m.watched_at)}</button>` : ""}
          ${m.media_type === "tv" && m.status !== "vista" ? (
            editingProgressId === m.id
              ? `<div class="progress-form">
                   <label class="progress-label">T<input class="progress-input" type="number" min="1" data-field="season" value="${m.current_season ?? ""}" placeholder="—" aria-label="Temporada"></label>
                   <label class="progress-label">E<input class="progress-input" type="number" min="1" data-field="episode" value="${m.current_episode ?? ""}" placeholder="—" aria-label="Episodio"></label>
                   <button class="progress-save" data-action="progress-save" type="button" aria-label="Guardar">✓</button>
                   <button class="progress-cancel" data-action="progress-cancel" type="button" aria-label="Cancelar">✕</button>
                 </div>`
              : `<button class="progress-btn ${(m.current_season || m.current_episode) ? "has-progress" : ""}" data-action="progress" type="button" title="Editar progreso">${(m.current_season || m.current_episode) ? `📍 T${m.current_season ?? "?"}  E${m.current_episode ?? "?"}` : "+ Progreso T/E"}</button>`
          ) : ""}
        </div>
        ${editingNoteId === m.id
          ? `<div class="note-form">
               <textarea class="note-textarea" maxlength="500" placeholder="Tu nota personal…" aria-label="Nota personal">${esc(m.note || "")}</textarea>
               <div class="note-form-actions">
                 <span class="note-chars" data-note-chars></span>
                 <button class="progress-save" data-action="note-save" type="button">✓ Guardar</button>
                 <button class="progress-cancel" data-action="note-cancel" type="button">✕</button>
               </div>
             </div>`
          : `<button class="note-btn ${m.note ? "has-note" : ""}" data-action="note" type="button" title="Editar nota personal">
               ${m.note ? `<span>${esc(notePreview(m.note))}</span>` : "+ Nota"}
             </button>`
        }
        <div class="stars">${starsHtml(m.rating || 0)}</div>
        <div class="card-actions">
          <button class="btn-secondary btn-sm" data-action="toggle" type="button">${m.status === "vista" ? "↺ Por ver" : "✓ Vista"}</button>
          <button class="icon-btn" data-action="delete" type="button" aria-label="Eliminar">✕</button>
        </div>
      </div>
    </article>`).join("");
  collectionEl.innerHTML = _html;
  if (editingProgressId !== null) {
    const input = collectionEl.querySelector(`.card[data-id="${editingProgressId}"] .progress-input`);
    if (input) input.focus();
  }
  if (editingNoteId !== null) {
    const ta = collectionEl.querySelector(`.card[data-id="${editingNoteId}"] .note-textarea`);
    if (ta) {
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
      const counter = ta.closest(".note-form").querySelector("[data-note-chars]");
      if (counter) counter.textContent = `${ta.value.length}/500`;
      ta.addEventListener("input", () => { if (counter) counter.textContent = `${ta.value.length}/500`; });
    }
  }
}

function closePickPanel() {
  pickedMovie = null;
  pickPanelEl.hidden = true;
  pickPanelEl.innerHTML = "";
}

function renderSkeleton() {
  collectionEl.innerHTML = Array.from({ length: 8 }, () =>
    `<article class="card skeleton-card" aria-hidden="true"><div class="skeleton-poster"></div><div class="skeleton-body"></div></article>`
  ).join("");
}

function renderPickPanel(movie) {
  pickedMovie = movie;
  const canOpenDetail = Boolean(movie.tmdb_id);
  const note = String(movie.note || "").trim();
  pickPanelEl.hidden = false;
  pickPanelEl.innerHTML = `
    <div class="pick-panel-copy">
      <span class="eyebrow">Recomendación de hoy</span>
      <h2>${esc(movie.title)}</h2>
      <div class="pick-meta">
        <span>${mediaIcon(movie.media_type)} ${mediaLabel(movie.media_type)}</span>
        <span>${esc(movie.year) || "—"}</span>
      </div>
      ${note ? `<p class="pick-note">${esc(note)}</p>` : ""}
    </div>
    <div class="pick-actions">
      ${canOpenDetail ? '<button class="btn-secondary" data-pick-action="detail" type="button">Ver detalle</button>' : ""}
      <button class="btn" data-pick-action="watched" type="button">✓ Marcar como vista</button>
      <button class="icon-btn pick-close" data-pick-action="close" type="button" aria-label="Cerrar recomendación">✕</button>
    </div>`;
}

function showView(viewId) {
  document.body.dataset.activeView = viewId;
  document.querySelectorAll(".view").forEach((view) => {
    const active = view.id === viewId;
    if (active) {
      view.hidden = false;
      void view.offsetWidth;
      view.classList.add("is-active");
    } else {
      view.classList.remove("is-active");
      setTimeout(() => {
        if (!view.classList.contains("is-active")) view.hidden = true;
      }, 220);
    }
  });
  document.querySelectorAll("[data-view-target]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.viewTarget === viewId);
  });
  showMessage("");
  if (viewId === "discover-view" && !el("discover-search-input").value.trim()) loadTrending();
}

function renderResults() {
  const visibleResults = lastResults
    .map((m, i) => ({ item: m, index: i }))
    .filter(({ item }) => mediaFilter === "todo" || item.media_type === mediaFilter);

  if (lastResults.length && !visibleResults.length) {
    const label = mediaFilter === "movie" ? "películas" : "series";
    resultsEl.innerHTML = `<p class="empty">No hay ${label} en estos resultados. Prueba con “Pelis y series”.</p>`;
    resultsSection.hidden = false;
    return;
  }

  resultsEl.innerHTML = visibleResults.map(({ item: m, index: i }) => `
    <article class="card" data-idx="${i}">
      <div class="poster">
        ${posterHtml(m)}
        <span class="media-badge">${mediaIcon(m.media_type)}</span>
      </div>
      <div class="card-body">
        <div>
          <div class="card-title">${esc(m.title)}</div>
          <div class="card-year">${esc(m.year) || "—"}</div>
        </div>
        <div class="card-actions">
          <button class="btn-secondary btn-sm" data-action="add" data-status="pendiente" type="button">+ Por ver</button>
          <button class="btn btn-sm" data-action="add" data-status="vista" type="button">✓ Vista</button>
        </div>
      </div>
    </article>`).join("");
  resultsSection.hidden = visibleResults.length === 0;
}

// ---- Modal detalle ----
function closeModal() {
  modalEl.classList.remove("is-open");
  setTimeout(() => { modalEl.hidden = true; modalContent.innerHTML = ""; }, 220);
}

async function openDetail(tmdbId, type) {
  modalContent.innerHTML = '<p class="muted">Cargando...</p>';
  modalEl.hidden = false;
  void modalEl.offsetWidth;
  modalEl.classList.add("is-open");
  const { data } = await api(`/api/details?id=${tmdbId}&type=${type}`);
  if (!data.ok) {
    modalContent.innerHTML = "<p>No hay detalle disponible (¿sin TMDB key?).</p>";
    return;
  }
  const d = data.details;
  const m = movies.find((x) => x.tmdb_id === tmdbId && x.media_type === type) || {};
  const creditsHtml = [
    d.directors.length ? `<p><strong>${esc(d.dir_label)}:</strong> ${d.directors.map(esc).join(", ")}</p>` : "",
    d.cast.length ? `<p><strong>Reparto:</strong> ${d.cast.map(esc).join(", ")}</p>` : "",
  ].filter(Boolean).join("");
  const overviewHtml = d.overview ? esc(d.overview) : '<span class="muted">Sin sinopsis disponible.</span>';
  const providers = d.providers || [];
  const providersHtml = `
    <div class="modal-providers">
      <span class="providers-label">Dónde ver en España</span>
      ${providers.length
        ? `<div class="providers-logos">
            ${providers.map((p) => `<img src="${esc(p.logo)}" alt="${esc(p.name)}" title="${esc(p.name)}" class="provider-logo" loading="lazy">`).join("")}
          </div>`
        : `<span class="providers-empty">No disponible en streaming</span>`}
    </div>`;
  modalContent.innerHTML = `
    <div class="modal-head">
      ${m.poster_url ? `<img src="${esc(m.poster_url)}" alt="">` : ""}
      <div>
        <h3 class="modal-title">${esc(m.title || "")}</h3>
        <div class="modal-meta">
          <span class="chip">${mediaIcon(type)} ${type === "tv" ? "Serie" : "Película"}</span>
          ${m.year ? `<span class="chip">${esc(m.year)}</span>` : ""}
          ${d.runtime ? `<span class="chip">${d.runtime} min</span>` : ""}
          ${d.vote_average ? `<span class="chip">★ ${d.vote_average}</span>` : ""}
          ${d.genres.map((g) => `<span class="chip">${esc(g)}</span>`).join("")}
        </div>
      </div>
    </div>
    ${d.trailer ? `<div class="modal-trailer"><a class="btn btn-sm" href="${esc(d.trailer)}" target="_blank" rel="noopener">▶ Ver tráiler</a></div>` : ""}
    ${providersHtml}
    ${creditsHtml ? `<div class="modal-credits">${creditsHtml}</div>` : ""}
    <div class="modal-overview"><p>${overviewHtml}</p></div>`;
}

// ---- Acciones ----
async function loadMovies() {
  if (!collectionEl.children.length) renderSkeleton();
  const { ok, data } = await api("/api/movies");
  if (ok && data.ok) {
    movies = data.movies;
    renderCollection();
    renderStats();
    if (pickedMovie) {
      const current = movies.find((m) => m.id === pickedMovie.id);
      if (current && current.status === "pendiente") renderPickPanel(current);
      else closePickPanel();
    }
  }
  else showMessage("No se pudo cargar tu cineteca. ¿Está arrancado server.py?", "error");
}

async function addItem(item, status) {
  const { ok, data } = await api("/api/movies", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...item, status }),
  });
  if (ok && data.ok) { showMessage(`Añadida: ${item.title}`); await loadMovies(); }
  else if (data.duplicate) showMessage(data.error, "error");
  else showMessage(data.error || "No se pudo añadir.", "error");
}

async function patchMovie(id, payload) {
  const { ok, data } = await api(`/api/movies/${id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  if (ok && data.ok) { await loadMovies(); return true; }
  showMessage(data.error || "No se pudo actualizar.", "error");
  return false;
}


function editWatchedDate(movie) {
  const current = String(movie.watched_at || "");
  const next = prompt("Fecha de visionado (YYYY-MM-DD). Déjalo vacío para limpiar:", current);
  if (next === null) return;
  const watchedAt = next.trim();
  if (watchedAt && !/^\d{4}-\d{2}-\d{2}$/.test(watchedAt)) {
    showMessage("Formato inválido. Usa YYYY-MM-DD.", "error");
    return;
  }
  patchMovie(movie.id, { watched_at: watchedAt || null });
}

async function deleteMovie(id) {
  const { ok } = await api(`/api/movies/${id}`, { method: "DELETE" });
  if (ok) await loadMovies();
}

async function loadTrending() {
  resultsMode = "trending";
  el("results-title").textContent = "Tendencias de la semana";
  el("results-close").hidden = true;
  showMessage("Cargando tendencias...");
  const { data } = await api("/api/trending");
  if (data.ok) {
    lastResults = data.results;
    renderResults();
    showMessage("");
  } else if (data.needs_key) {
    lastResults = [];
    renderResults();
    showMessage("Tendencias desactivadas (sin TMDB key). Configura la clave (ver README).", "error");
  } else {
    showMessage(data.error || "No se pudieron cargar las tendencias.", "error");
  }
}

// ---- Eventos ----
el("search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = el("search-input").value.trim();
  collectionQuery = q;
  renderCollection();
});

el("search-input").addEventListener("input", (e) => {
  collectionQuery = e.target.value;
  renderCollection();
});

el("collection-sort").addEventListener("change", (e) => {
  collectionSort = e.target.value;
  renderCollection();
});

el("discover-search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = el("discover-search-input").value.trim();
  if (!q) { loadTrending(); return; }
  resultsMode = "search";
  el("results-title").textContent = "Resultados";
  el("results-close").hidden = false;
  showMessage("Buscando...");
  const { data } = await api("/api/search?q=" + encodeURIComponent(q));
  if (data.ok) {
    lastResults = data.results;
    renderResults();
    const visibleCount = lastResults.filter((m) => mediaFilter === "todo" || m.media_type === mediaFilter).length;
    showMessage(visibleCount ? "" : "Sin resultados para el tipo seleccionado.");
  } else if (data.needs_key) {
    showMessage("Búsqueda online desactivada (sin TMDB key). Usa «+ Añadir manual» en Mi colección o configura la clave (ver README).", "error");
  } else {
    showMessage(data.error || "Error en la búsqueda.", "error");
  }
});

el("results-close").addEventListener("click", () => { el("discover-search-input").value = ""; loadTrending(); });

el("discover-search-input").addEventListener("input", (e) => {
  if (!e.target.value.trim()) loadTrending();
});

document.querySelectorAll("[data-view-target]").forEach((btn) => {
  btn.addEventListener("click", () => showView(btn.dataset.viewTarget));
});

resultsEl.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action='add']");
  if (!btn) return;
  const idx = +btn.closest(".card").dataset.idx;
  addItem(lastResults[idx], btn.dataset.status);
});

collectionEl.addEventListener("click", (e) => {
  const card = e.target.closest(".card");
  if (!card) return;
  const id = +card.dataset.id;
  const movie = movies.find((m) => m.id === id);
  const poster = e.target.closest(".poster[data-tmdb]");
  if (poster) { openDetail(+poster.dataset.tmdb, poster.dataset.type); return; }
  const star = e.target.closest(".star");
  if (star) {
    const value = +star.dataset.star;
    patchMovie(id, { rating: movie && movie.rating === value ? null : value });
    return;
  }
  const action = e.target.closest("[data-action]")?.dataset.action;
  if (action === "toggle") {
    const status = movie.status === "vista" ? "pendiente" : "vista";
    const payload = { status };
    if (status === "vista" && !movie.watched_at) payload.watched_at = todayIsoDate();
    patchMovie(id, payload);
  }
  else if (action === "delete") deleteMovie(id);
  else if (action === "note") { editingNoteId = id; renderCollection(); }
  else if (action === "note-cancel") { editingNoteId = null; renderCollection(); }
  else if (action === "note-save" && movie) {
    const ta = card.querySelector(".note-textarea");
    const note = ta ? ta.value.trim() : "";
    if (note.length > 500) { showMessage("La nota no puede superar 500 caracteres.", "error"); return; }
    editingNoteId = null;
    patchMovie(movie.id, { note });
  }
  else if (action === "date" && movie) editWatchedDate(movie);
  else if (action === "progress") { editingProgressId = id; renderCollection(); }
  else if (action === "progress-cancel") { editingProgressId = null; renderCollection(); }
  else if (action === "progress-save" && movie) {
    const form = card.querySelector(".progress-form");
    const s = form.querySelector("[data-field='season']").value.trim();
    const e = form.querySelector("[data-field='episode']").value.trim();
    const season = s ? parseInt(s, 10) : null;
    const episode = e ? parseInt(e, 10) : null;
    if (s && (isNaN(season) || season < 1)) { showMessage("La temporada debe ser un número positivo.", "error"); return; }
    if (e && (isNaN(episode) || episode < 1)) { showMessage("El episodio debe ser un número positivo.", "error"); return; }
    editingProgressId = null;
    patchMovie(movie.id, { current_season: season, current_episode: episode });
  }
});

el("status-filter").addEventListener("change", (e) => {
  filter = e.target.value;
  renderCollection();
});

el("media-filter").addEventListener("click", (e) => {
  const b = e.target.closest(".seg-btn");
  if (!b) return;
  mediaFilter = b.dataset.media;
  document.querySelectorAll("#media-filter .seg-btn").forEach((x) => x.classList.toggle("active", x === b));
  renderResults();
});

el("collection-media-filter").addEventListener("change", (e) => {
  collectionMediaFilter = e.target.value;
  renderCollection();
});

function pickTonight() {
  const typeLabel = { todo: "", movie: " (película)", tv: " (serie)" }[collectionMediaFilter] || "";
  const pendientes = movies.filter((m) =>
    m.status === "pendiente" &&
    (collectionMediaFilter === "todo" || (m.media_type || "movie") === collectionMediaFilter));
  if (!pendientes.length) {
    showMessage(`No tienes nada en «Por ver»${typeLabel} para elegir.`, "error");
    return;
  }
  const choice = pendientes[Math.floor(Math.random() * pendientes.length)];
  renderPickPanel(choice);
  // Asegura que la tarjeta elegida sea visible: quita el filtro de búsqueda y
  // pon el filtro de estado en "Por ver" (o "Todas") para no esconderla.
  collectionQuery = "";
  el("search-input").value = "";
  if (filter === "vista") {
    filter = "todas";
    el("status-filter").value = "todas";
  }
  renderCollection();
  const card = collectionEl.querySelector(`.card[data-id="${choice.id}"]`);
  if (card) {
    document.querySelectorAll(".card.pick-highlight").forEach((c) => c.classList.remove("pick-highlight"));
    card.classList.add("pick-highlight");
    card.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  showMessage(`🎬 Esta noche: ${choice.title}${choice.year ? " (" + choice.year + ")" : ""}`);
}

el("pick-tonight").addEventListener("click", pickTonight);

pickPanelEl.addEventListener("click", async (e) => {
  const action = e.target.closest("[data-pick-action]")?.dataset.pickAction;
  if (!action || !pickedMovie) return;
  if (action === "close") { closePickPanel(); return; }
  if (action === "detail" && pickedMovie.tmdb_id) {
    openDetail(+pickedMovie.tmdb_id, pickedMovie.media_type || "movie");
    return;
  }
  if (action === "watched") {
    const title = pickedMovie.title;
    const payload = { status: "vista" };
    if (!pickedMovie.watched_at) payload.watched_at = todayIsoDate();
    const ok = await patchMovie(pickedMovie.id, payload);
    if (ok) showMessage(`Marcada como vista: ${title}`);
  }
});

el("manual-toggle").addEventListener("click", () => {
  const f = el("manual-form");
  f.hidden = !f.hidden;
  if (!f.hidden) el("manual-title").focus();
});

el("manual-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const title = el("manual-title").value.trim();
  if (!title) return;
  addItem({ title, year: el("manual-year").value.trim(), media_type: el("manual-type").value, poster_url: "", tmdb_id: null }, "pendiente");
  e.target.reset();
  e.target.hidden = true;
});

modalEl.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeModal(); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!modalEl.hidden) closeModal();
  else if (!pickPanelEl.hidden) closePickPanel();
  else if (editingProgressId !== null) { editingProgressId = null; renderCollection(); }
  else if (editingNoteId !== null) { editingNoteId = null; renderCollection(); }
});

document.body.dataset.activeView = "collection-view";
loadMovies();
