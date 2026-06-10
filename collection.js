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
      <div class="poster ${m.tmdb_id ? "cursor-pointer" : ""}" ${m.tmdb_id ? `data-tmdb="${esc(m.tmdb_id)}" data-type="${esc(m.media_type)}"` : ""}>
        ${posterHtml(m)}
        <span class="status-badge ${m.status}">${{ pendiente: "Por ver", viendo: "Viendo", vista: "Vista", abandonada: "Abandonada" }[m.status] ?? m.status}</span>
        <span class="media-badge">${mediaIcon(m.media_type)}</span>
      </div>
      <div class="card-body">
        <div>
          <div class="card-title">${esc(m.title)}</div>
          <div class="card-year">${esc(m.year) || "—"}</div>
          ${editingDateId === m.id
            ? `<div class="date-form">
                 <input class="date-input" type="date" value="${esc(m.watched_at || '')}" aria-label="Fecha de visionado">
                 <button class="progress-save" data-action="date-save" type="button" aria-label="Guardar">✓</button>
                 ${m.watched_at ? `<button class="progress-cancel" data-action="date-clear" type="button" title="Quitar fecha">—</button>` : ""}
                 <button class="progress-cancel" data-action="date-cancel" type="button" aria-label="Cancelar">✕</button>
               </div>`
            : m.watched_at
              ? `<button class="date-btn" data-action="date" type="button" title="Editar fecha de visionado">📅 Vista el ${esc(m.watched_at)}</button>`
              : ""}
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
          ${editingPlatformId === m.id
            ? `<div class="platform-picker">
                 ${PLATFORMS.map((p) => `<button class="platform-chip${m.platform === p ? " active" : ""}" data-action="platform-pick" data-platform="${esc(p)}" type="button">${esc(p)}</button>`).join("")}
                 ${m.platform ? `<button class="platform-chip platform-chip-clear" data-action="platform-pick" data-platform="" type="button">✕ Quitar</button>` : ""}
                 <button class="progress-cancel" data-action="platform-cancel" type="button" aria-label="Cancelar">✕</button>
               </div>`
            : `<button class="platform-btn${m.platform ? " has-platform" : ""}" data-action="platform-open" type="button" title="Dónde la vi">${m.platform ? `▶ ${esc(m.platform)}` : "+ Plataforma"}</button>`
          }
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
          <select class="select btn-sm status-select" data-action="status-change" aria-label="Cambiar estado">
            <option value="pendiente" ${m.status === "pendiente" ? "selected" : ""}>Por ver</option>
            <option value="viendo"    ${m.status === "viendo"    ? "selected" : ""}>Viendo</option>
            <option value="vista"     ${m.status === "vista"     ? "selected" : ""}>Vista</option>
            <option value="abandonada"${m.status === "abandonada"? "selected" : ""}>Abandonada</option>
          </select>
          <button class="icon-btn" data-action="delete" type="button" aria-label="Eliminar">✕</button>
        </div>
      </div>
    </article>`).join("");
  collectionEl.innerHTML = _html;
  if (editingProgressId !== null) {
    const input = collectionEl.querySelector(`.card[data-id="${editingProgressId}"] .progress-input`);
    if (input) input.focus();
  }
  if (editingDateId !== null) {
    const input = collectionEl.querySelector(`.card[data-id="${editingDateId}"] .date-input`);
    if (input) input.focus();
  }
  if (editingPlatformId !== null) {
    const chip = collectionEl.querySelector(`.card[data-id="${editingPlatformId}"] .platform-chip`);
    if (chip) chip.focus();
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
  const { ok, data } = await api(`/api/movies/${id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  if (ok && data.ok) { await loadMovies(); return true; }
  showMessage(data.error || "No se pudo actualizar.", "error");
  return false;
}



async function deleteMovie(id) {
  const { ok } = await api(`/api/movies/${id}`, { method: "DELETE" });
  if (ok) await loadMovies();
}

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
    const next = { pendiente: "viendo", viendo: "vista", vista: "pendiente", abandonada: "pendiente" };
    const status = next[movie.status] ?? "pendiente";
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
  else if (action === "date") { editingDateId = id; renderCollection(); }
  else if (action === "date-cancel") { editingDateId = null; renderCollection(); }
  else if (action === "date-clear" && movie) {
    editingDateId = null;
    patchMovie(movie.id, { watched_at: null });
  }
  else if (action === "date-save" && movie) {
    const input = card.querySelector(".date-input");
    const watchedAt = input ? input.value : null;
    editingDateId = null;
    patchMovie(movie.id, { watched_at: watchedAt || null });
  }
  else if (action === "platform-open") { editingPlatformId = id; renderCollection(); }
  else if (action === "platform-cancel") { editingPlatformId = null; renderCollection(); }
  else if (action === "platform-pick") {
    const platform = e.target.closest("[data-platform]")?.dataset.platform || null;
    editingPlatformId = null;
    patchMovie(id, { platform: platform || null });
  }
  else if (action === "progress") { editingProgressId = id; renderCollection(); }
  else if (action === "progress-cancel") { editingProgressId = null; renderCollection(); }
  else if (action === "progress-save" && movie) {
    const form = card.querySelector(".progress-form");
    const s = form.querySelector("[data-field='season']").value.trim();
    const ep = form.querySelector("[data-field='episode']").value.trim();
    const season = s ? parseInt(s, 10) : null;
    const episode = ep ? parseInt(ep, 10) : null;
    if (s && (isNaN(season) || season < 1)) { showMessage("La temporada debe ser un número positivo.", "error"); return; }
    if (ep && (isNaN(episode) || episode < 1)) { showMessage("El episodio debe ser un número positivo.", "error"); return; }
    editingProgressId = null;
    patchMovie(movie.id, { current_season: season, current_episode: episode });
  }
});
