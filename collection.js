// Mi Cineteca — colección del usuario: orden, render, CRUD y panel "Esta noche".

const collectionEl = el("collection");
const emptyEl = el("empty");
const collectionEmptyStateEl = el("collection-empty-state");
const collectionControlsEl = el("collection-controls");

const PLATFORMS = ["Netflix", "HBO Max", "Prime Video", "Disney+", "Movistar+", "Cine", "Otra"];

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
  const isCollectionEmpty = movies.length === 0;
  collectionEmptyStateEl.hidden = !isCollectionEmpty;
  collectionControlsEl.hidden = isCollectionEmpty;
  emptyEl.hidden = isCollectionEmpty || list.length !== 0;
  if (!isCollectionEmpty && list.length === 0) {
    emptyEl.textContent = "No hay títulos que coincidan con estos filtros.";
  }
  const _html = list.map((m) => `
    <article class="card" data-id="${m.id}">
      <div class="poster cursor-pointer" ${m.tmdb_id ? `data-tmdb="${esc(m.tmdb_id)}" data-type="${esc(m.media_type)}"` : `data-edit-id="${esc(m.id)}"`}>
        ${posterHtml(m)}
        <span class="status-badge ${m.status}">${{ pendiente: "Por ver", viendo: "Viendo", vista: "Vista", abandonada: "Abandonada" }[m.status] ?? m.status}</span>
        <span class="media-badge">${mediaIcon(m.media_type)}</span>
      </div>
      <div class="card-body">
        <div class="card-title">${esc(m.title)}</div>
        <div class="card-year">${esc(m.year) || "—"}</div>
        <div class="stars">${starsHtml(m.rating || 0)}</div>
      </div>
    </article>`).join("");
  collectionEl.innerHTML = _html;
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

// ---- Acciones ----
async function loadMovies() {
  if (!collectionEl.children.length) renderSkeleton();
  const { ok, data } = await api("/api/movies");
  if (ok && data.ok) {
    movies = data.movies;
    renderCollection();
    renderStatsView();
    loadLevel();
    if (pickedMovie) {
      const current = movies.find((m) => m.id === pickedMovie.id);
      if (current && current.status === "pendiente") renderPickPanel(current);
      else closePickPanel();
    }
  }
  else showMessage("No se pudo cargar tu cineteca. ¿Está arrancado server.py?", "error");
}

async function addItem(item, status) {
  // Backstop de modo invitado (AC-6): nunca una escritura user-scoped desde un
  // invitado. _guestMode/_promptSignup viven en app.js (tiempo de llamada, PS-003).
  if (_guestMode) { _promptSignup("guardar tu coleccion"); return false; }
  const { ok, data } = await api("/api/movies", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...item, status }),
  });
  if (ok && data.ok) { showMessage(`Añadida: ${item.title}`); await loadMovies(); return true; }
  else if (data.duplicate) showMessage(data.error, "error");
  else showMessage(data.error || "No se pudo añadir.", "error");
  return false;
}

async function patchMovie(id, payload) {
  if (_guestMode) { _promptSignup("gestionar tu coleccion"); return false; }
  const { ok, data } = await api(`/api/movies/${id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  if (ok && data.ok) { await loadMovies(); return true; }
  showMessage(data.error || "No se pudo actualizar.", "error");
  return false;
}



async function deleteMovie(id) {
  if (_guestMode) { _promptSignup("gestionar tu coleccion"); return; }
  const { ok } = await api(`/api/movies/${id}`, { method: "DELETE" });
  if (ok) await loadMovies();
}

function pickTonight() {
  if (_guestMode) return _promptSignup("guardar tu coleccion");
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

collectionEl.addEventListener("click", (e) => {
  const card = e.target.closest(".card");
  if (!card) return;
  const id = +card.dataset.id;
  const movie = movies.find((m) => m.id === id);
  const poster = e.target.closest(".poster[data-tmdb]");
  if (poster) { openDetail(+poster.dataset.tmdb, poster.dataset.type); return; }
  // Título sin tmdb_id: su póster abre el modal en modo edición-solo (AC-2).
  // openEditOnly vive en modal.js (4º módulo, cargado después); se llama desde
  // este cuerpo de manejador → tiempo de llamada, PS-003-safe.
  const editPoster = e.target.closest(".poster[data-edit-id]");
  if (editPoster) { openEditOnly(+editPoster.dataset.editId); return; }
  const star = e.target.closest(".star");
  if (star) {
    const value = +star.dataset.star;
    patchMovie(id, { rating: movie && movie.rating === value ? null : value });
    return;
  }
});
