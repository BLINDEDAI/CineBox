// Mi Cineteca — modal de detalle: ficha TMDB, alta desde modal y títulos similares.

const modalContent = el("modal-content");
let modalContext = null;
// Id de la película que la sección de edición del modal está editando (ADR-016).
// Es un contexto LOCAL del modal: no reutiliza los flags editing* de la tarjeta
// (editingNoteId/…), que siguen siendo card-scoped. null = sin sección de edición.
let modalEditId = null;

// ---- Modal detalle ----
function closeModal() {
  modalEl.classList.remove("is-open");
  setTimeout(() => { modalEl.hidden = true; modalContent.innerHTML = ""; modalContext = null; modalEditId = null; }, 220);
}

async function openDetail(tmdbId, type, hint = {}) {
  modalContext = null;
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
  const existing = movies.find((x) => x.tmdb_id === tmdbId && x.media_type === type);
  const m = existing || hint;
  // Contexto de edición local (ADR-016): solo hay sección de edición para un
  // título de la colección; para búsqueda/descubrir/similares queda en null.
  modalEditId = existing ? existing.id : null;
  modalContext = {
    tmdbId,
    type,
    title:      m.title      || d.title || "",
    poster_url: m.poster_url || (d.poster_path ? `https://image.tmdb.org/t/p/w342${d.poster_path}` : ""),
    year:       m.year       || "",
    genre_ids:  d.genre_ids  || [],   // ids → el backend los mapea a nombres ES (consistente con la carátula)
    genres:     d.genres     || [],   // nombres; fallback si no hubiera ids
    total_seasons: d.total_seasons,   // total de temporadas TMDB (series); null si no aplica/no disponible
  };
  // AC-7 — backfill oportunista: si la serie ya está en la colección sin total
  // y TMDB ahora lo devuelve, persistir una vez y mutar la entrada en memoria
  // (sin loadMovies() para no recargar la colección con el modal abierto).
  // Fire-and-forget; silencioso ante fallo (BR-7).
  if (existing && existing.media_type === "tv" && !existing.total_seasons && d.total_seasons) {
    existing.total_seasons = d.total_seasons;
    api(`/api/movies/${existing.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ total_seasons: d.total_seasons }),
    }).catch(() => {});
  }
  const directorHtml = d.directors.length
    ? `<p><strong>${esc(d.dir_label)}:</strong> ${d.directors.map(esc).join(", ")}</p>`
    : "";
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
  const cast = d.cast || [];
  const castHtml = cast.length ? `
    <div class="modal-cast">
      <h4 class="modal-cast-title">Reparto</h4>
      <div class="modal-cast-row">
        ${cast.map((c) => `
          <div class="cast-member">
            ${c.profile_path
              ? `<img class="cast-photo" src="https://image.tmdb.org/t/p/w185${esc(c.profile_path)}" alt="" loading="lazy">`
              : `<div class="cast-photo cast-photo-fallback" aria-hidden="true">${esc((c.name || "?").slice(0, 1))}</div>`}
            <span class="cast-name">${esc(c.name || "")}</span>
          </div>`).join("")}
      </div>
    </div>` : "";
  const backdropUrl = d.backdrop_path ? `https://image.tmdb.org/t/p/w1280${esc(d.backdrop_path)}` : "";
  modalContent.innerHTML = `
    <div class="modal-hero${backdropUrl ? "" : " modal-hero-noimg"}">
      ${backdropUrl ? `<img class="modal-hero-img" src="${backdropUrl}" alt="" loading="lazy">` : ""}
      <div class="modal-hero-shade"></div>
      <div class="modal-hero-content">
        ${modalContext.poster_url ? `<img class="modal-hero-poster" src="${esc(modalContext.poster_url)}" alt="">` : ""}
        <div class="modal-hero-text">
          <h3 class="modal-title">${esc(m.title || "")}${m.year ? ` <span class="modal-title-year">(${esc(m.year)})</span>` : ""}</h3>
          <div class="modal-meta">
            <span class="chip">${mediaIcon(type)} ${type === "tv" ? "Serie" : "Película"}</span>
            ${d.runtime ? `<span class="chip">${d.runtime} min</span>` : ""}
            ${d.vote_average ? `<span class="chip">★ ${d.vote_average}</span>` : ""}
            ${d.genres.map((g) => `<span class="chip">${esc(g)}</span>`).join("")}
          </div>
        </div>
      </div>
    </div>
    <div class="modal-body">
      ${!existing ? `
      <div class="modal-add-btns" id="modal-add-btns">
        <button class="btn btn-sm" data-add-status="pendiente">+ Por ver</button>
        <button class="btn btn-sm btn-success" data-add-status="vista">✓ Vista</button>
      </div>` : `<div class="modal-status-chip"><span class="chip chip-status">${esc(existing.status)}</span></div>`}
      <div class="modal-list-action">
        <button class="btn-secondary btn-sm" type="button" id="modal-add-to-list">+ Añadir a lista</button>
      </div>
      ${existing ? `<div class="modal-edit-section" id="modal-edit-section"></div>` : ""}
      ${d.trailer ? `<div class="modal-trailer"><a class="btn btn-sm" href="${esc(d.trailer)}" target="_blank" rel="noopener">▶ Ver tráiler</a></div>` : ""}
      ${providersHtml}
      ${directorHtml ? `<div class="modal-credits">${directorHtml}</div>` : ""}
      <div class="modal-overview"><p>${overviewHtml}</p></div>
      ${castHtml}
      <div class="modal-similar" id="modal-similar-section"></div>
    </div>`;
  if (existing) _rerenderModalEditSection();
  const addBtns = document.getElementById("modal-add-btns");
  if (addBtns) {
    addBtns.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-add-status]");
      if (btn) addFromModal(btn.dataset.addStatus);
    });
  }
  const addToListBtn = document.getElementById("modal-add-to-list");
  if (addToListBtn) {
    addToListBtn.addEventListener("click", () => {
      if (!modalContext) return;
      // Cuerpo de manejador → tiempo de llamada (PS-003); openAddToListPicker vive en sharing.js.
      openAddToListPicker({
        tmdb_id:    modalContext.tmdbId,
        media_type: modalContext.type,
        title:      modalContext.title,
        year:       modalContext.year,
        poster_url: modalContext.poster_url,
      });
    });
  }
  const similarSection = document.getElementById("modal-similar-section");
  if (similarSection) similarSection.addEventListener("click", (e) => {
    const btn = e.target.closest(".similar-card[data-tmdb]");
    if (!btn) return;
    openDetail(+btn.dataset.tmdb, btn.dataset.type, {
      title: btn.dataset.title,
      poster_url: btn.dataset.poster,
      year: btn.dataset.year,
    });
  });
  api(`/api/similar?id=${tmdbId}&type=${type}`).then(({ data }) => {
    if (!similarSection.isConnected || !data.ok || !data.results.length) return;
    similarSection.innerHTML = `
      <h4 class="similar-title">Títulos similares</h4>
      <div class="similar-grid">
        ${data.results.map((r) => `
          <button class="similar-card"
            data-tmdb="${esc(r.tmdb_id)}"
            data-type="${esc(r.type)}"
            data-title="${esc(r.title)}"
            data-poster="${esc(r.poster_url)}"
            data-year="${esc(r.year)}">
            ${r.poster_url
              ? `<img src="${esc(r.poster_url)}" alt="${esc(r.title)}" loading="lazy">`
              : `<div class="similar-no-poster">${esc(r.title)}</div>`}
            <span class="similar-card-title">${esc(r.title)}</span>
          </button>`).join("")}
      </div>`;
  }).catch(() => {});
}

async function addFromModal(status) {
  if (!modalContext) return;
  const added = await addItem({
    title:      modalContext.title,
    media_type: modalContext.type,
    tmdb_id:    modalContext.tmdbId,
    year:       modalContext.year,
    poster_url: modalContext.poster_url,
    genre_ids:  modalContext.genre_ids,
    genres:     modalContext.genres,
    total_seasons: modalContext.total_seasons,
  }, status);
  if (added) closeModal();
}

// ---- Sección de edición del modal (ADR-016) ----
// Expone en el modal cada editor que la tarjeta tiene hoy (estado, valoración,
// fecha, progreso de serie, plataforma, nota + "Reseña pública", añadir a lista,
// eliminar), siempre expandidos. Reutiliza las clases de estilo de la tarjeta
// (.note-form/.progress-form/.date-form/.platform-picker/.stars/.status-select/
// .note-public-toggle). La nota se escapa con esc() (convención SPA autenticada,
// US-043). Los identificadores son ingleses (US-001); las etiquetas, es-ES.
function _modalEditSectionHtml(m) {
  const showProgress = m.media_type === "tv" && m.status !== "vista";
  const noteVal = m.note || "";
  const hasNote = noteVal.trim().length > 0;
  return `
    <h4 class="modal-edit-title">Editar</h4>
    <div class="modal-edit-grid">
      <div class="modal-edit-field">
        <label class="modal-edit-label" for="modal-edit-status">Estado</label>
        <select class="select btn-sm status-select" id="modal-edit-status" data-action="edit-status" aria-label="Cambiar estado">
          <option value="pendiente" ${m.status === "pendiente" ? "selected" : ""}>Por ver</option>
          <option value="viendo"    ${m.status === "viendo"    ? "selected" : ""}>Viendo</option>
          <option value="vista"     ${m.status === "vista"     ? "selected" : ""}>Vista</option>
          <option value="abandonada"${m.status === "abandonada"? "selected" : ""}>Abandonada</option>
        </select>
      </div>
      <div class="modal-edit-field">
        <span class="modal-edit-label" id="modal-edit-rating-label">Valoración</span>
        <div class="stars" data-action="edit-rating" role="group" aria-labelledby="modal-edit-rating-label">${starsHtml(m.rating || 0)}</div>
      </div>
      <div class="modal-edit-field">
        <label class="modal-edit-label" for="modal-edit-date">Fecha de visionado</label>
        <div class="date-form">
          <input class="date-input" id="modal-edit-date" type="date" value="${esc(m.watched_at || "")}" aria-label="Fecha de visionado">
          <button class="progress-save" data-action="edit-date-save" type="button" aria-label="Guardar fecha">✓</button>
          ${m.watched_at ? `<button class="progress-cancel" data-action="edit-date-clear" type="button" title="Quitar fecha" aria-label="Quitar fecha">—</button>` : ""}
        </div>
      </div>
      ${showProgress ? `
      <div class="modal-edit-field">
        <span class="modal-edit-label">Progreso</span>
        <div class="progress-form">
          <label class="progress-label">T<input class="progress-input" type="number" min="1"${m.total_seasons ? ` max="${esc(m.total_seasons)}"` : ""} data-field="season" value="${m.current_season ?? ""}" placeholder="—" aria-label="Temporada"></label>
          <label class="progress-label">E<input class="progress-input" type="number" min="1" data-field="episode" value="${m.current_episode ?? ""}" placeholder="—" aria-label="Episodio"></label>
          <button class="progress-save" data-action="edit-progress-save" type="button" aria-label="Guardar progreso">✓</button>
          ${m.total_seasons ? `<span class="progress-hint">de ${esc(m.total_seasons)} temporadas</span>` : ""}
        </div>
      </div>` : ""}
      <div class="modal-edit-field">
        <span class="modal-edit-label">Plataforma</span>
        <div class="platform-picker">
          ${PLATFORMS.map((p) => `<button class="platform-chip${m.platform === p ? " active" : ""}" data-action="edit-platform-pick" data-platform="${esc(p)}" type="button">${esc(p)}</button>`).join("")}
          ${m.platform ? `<button class="platform-chip platform-chip-clear" data-action="edit-platform-pick" data-platform="" type="button">✕ Quitar</button>` : ""}
        </div>
      </div>
      <div class="modal-edit-field modal-edit-field-note">
        <label class="modal-edit-label" for="modal-edit-note">Nota personal</label>
        <div class="note-form">
          <textarea class="note-textarea" id="modal-edit-note" maxlength="500" placeholder="Tu nota personal…" aria-label="Nota personal">${esc(noteVal)}</textarea>
          <label class="note-public-toggle">
            <input type="checkbox" data-note-public${m.note_public ? " checked" : ""}${hasNote ? "" : " disabled"}>
            <span class="note-public-label">Reseña pública</span>
            <span class="note-public-hint" data-note-public-hint${hasNote ? " hidden" : ""}>Escribe una nota antes de publicarla.</span>
          </label>
          <div class="note-form-actions">
            <span class="note-chars" data-note-chars>${noteVal.length}/500</span>
            <button class="progress-save" data-action="edit-note-save" type="button">✓ Guardar</button>
          </div>
        </div>
      </div>
    </div>
    <div class="modal-edit-actions">
      <button class="btn-secondary btn-sm" data-action="edit-add-to-list" type="button">+ Añadir a lista</button>
      <button class="icon-btn modal-edit-delete" data-action="edit-delete" type="button" aria-label="Eliminar ${esc(m.title || "")}">✕ Eliminar</button>
    </div>`;
}

// Re-renderiza la sección de edición desde la película en memoria más reciente
// (movies.find, AC-11). Si la película ya no está (borrada / colección recargada),
// cierra el modal con elegancia en vez de dejar un editor obsoleto.
function _rerenderModalEditSection() {
  const container = document.getElementById("modal-edit-section");
  if (!container) return;
  const movie = movies.find((x) => x.id === modalEditId);
  if (!movie) { closeModal(); return; }
  container.innerHTML = _modalEditSectionHtml(movie);
}

// Guarda un edit y, si tuvo éxito, mantiene el modal abierto re-renderizando la
// sección desde la película actualizada (AC-11). patchMovie ya hace await
// loadMovies() (refresca la colección debajo) y devuelve true en éxito; ante
// fallo muestra el error (incluido el 400 autoritativo de nota-pública, AC-8).
async function _modalEditSave(id, payload) {
  const ok = await patchMovie(id, payload);
  if (ok) _rerenderModalEditSection();
}

// Modo edición-solo (AC-2): abre el modal para un título SIN tmdb_id sin pedir
// /api/details ni /api/similar; solo renderiza la sección de edición desde la
// película en memoria. Su póster (con data-edit-id) es el disparador desde la
// tarjeta. openEditOnly vive en modal.js (4º módulo) y collection.js (3º) la
// llama desde el cuerpo del delegador → tiempo de llamada, PS-003-safe.
function openEditOnly(movieId) {
  const movie = movies.find((x) => x.id === movieId);
  if (!movie) return; // no hay nada que editar
  modalContext = null;
  modalEditId = movie.id;
  modalContent.innerHTML = `
    <div class="modal-body modal-body-editonly">
      <h3 class="modal-title modal-title-editonly">${esc(movie.title || "")}${movie.year ? ` <span class="modal-title-year">(${esc(movie.year)})</span>` : ""}</h3>
      <div class="modal-edit-section" id="modal-edit-section"></div>
    </div>`;
  modalEl.hidden = false;
  void modalEl.offsetWidth;
  modalEl.classList.add("is-open");
  _rerenderModalEditSection();
}

// Delegadores adjuntados UNA vez al cargar sobre el contenedor estable
// #modal-content (mismo patrón que el delegador de #collection), de modo que
// sobreviven al re-render de la sección tras cada guardado (ADR-016 / AC-11).
// Solo actúan sobre las acciones edit-* de la sección de edición; los demás
// controles del modal (add-btns, add-to-list por id, similares) usan sus
// propios listeners y no colisionan. modalContent se declaró arriba en este
// mismo archivo → PS-003-safe en tiempo de carga; patchMovie/deleteMovie
// (collection.js) y openAddToListPicker (settings.js) se invocan desde cuerpos
// de manejador (tiempo de llamada).
modalContent.addEventListener("click", (e) => {
  const actionEl = e.target.closest("[data-action]");
  if (!actionEl) return;
  const action = actionEl.dataset.action;
  if (!action || !action.startsWith("edit-")) return;
  const movie = movies.find((x) => x.id === modalEditId);
  if (!movie) { closeModal(); return; }
  const id = movie.id;
  if (action === "edit-rating") {
    const star = e.target.closest(".star");
    if (!star) return;
    const value = +star.dataset.star;
    _modalEditSave(id, { rating: movie.rating === value ? null : value });
  } else if (action === "edit-date-save") {
    const input = modalContent.querySelector("#modal-edit-date");
    _modalEditSave(id, { watched_at: (input && input.value) || null });
  } else if (action === "edit-date-clear") {
    _modalEditSave(id, { watched_at: null });
  } else if (action === "edit-progress-save") {
    const form = actionEl.closest(".progress-form");
    const s  = form.querySelector("[data-field='season']").value.trim();
    const ep = form.querySelector("[data-field='episode']").value.trim();
    const season  = s  ? parseInt(s, 10)  : null;
    const episode = ep ? parseInt(ep, 10) : null;
    if (s && (isNaN(season) || season < 1)) { showMessage("La temporada debe ser un número positivo.", "error"); return; }
    if (ep && (isNaN(episode) || episode < 1)) { showMessage("El episodio debe ser un número positivo.", "error"); return; }
    if (movie.total_seasons && season !== null && season > movie.total_seasons) {
      showMessage(`La temporada no puede superar el total de ${movie.total_seasons} temporadas.`, "error");
      return;
    }
    _modalEditSave(id, { current_season: season, current_episode: episode });
  } else if (action === "edit-platform-pick") {
    const platform = e.target.closest("[data-platform]")?.dataset.platform || null;
    _modalEditSave(id, { platform: platform || null });
  } else if (action === "edit-note-save") {
    const ta = modalContent.querySelector("#modal-edit-note");
    const note = ta ? ta.value.trim() : "";
    if (note.length > 500) { showMessage("La nota no puede superar 500 caracteres.", "error"); return; }
    // "Reseña pública": una nota vacía nunca puede quedar publicada (AC-8). El
    // backend es la autoridad (400); esto es solo el reflejo cliente.
    const publicCheckbox = modalContent.querySelector("[data-note-public]");
    const notePublic = !!(publicCheckbox && publicCheckbox.checked) && note.length > 0;
    _modalEditSave(id, { note, note_public: notePublic });
  } else if (action === "edit-add-to-list") {
    openAddToListPicker({
      tmdb_id:    movie.tmdb_id,
      media_type: movie.media_type,
      title:      movie.title,
      year:       movie.year,
      poster_url: movie.poster_url,
    });
  } else if (action === "edit-delete") {
    deleteMovie(id).then(() => closeModal());
  }
});

modalContent.addEventListener("change", (e) => {
  const sel = e.target.closest("[data-action='edit-status']");
  if (!sel) return;
  const movie = movies.find((x) => x.id === modalEditId);
  if (!movie) { closeModal(); return; }
  const status = sel.value;
  const payload = { status };
  // Paridad con el seam de estado de la tarjeta (app.js): al pasar a "vista",
  // rellena fecha y plataforma por defecto si faltan.
  if (status === "vista" && !movie.watched_at) payload.watched_at = todayIsoDate();
  if (status === "vista" && !movie.platform) {
    const defaultPlatform = getPref("default_platform", PLATFORMS, null);
    if (defaultPlatform) payload.platform = defaultPlatform;
  }
  _modalEditSave(movie.id, payload);
});

// Reflejo cliente en vivo (AC-8): la casilla "Reseña pública" solo puede
// activarse con una nota no vacía; al vaciar el textarea se desmarca +
// deshabilita y se muestra la pista. Delegado en el contenedor estable.
modalContent.addEventListener("input", (e) => {
  const ta = e.target.closest("#modal-edit-note");
  if (!ta) return;
  const form = ta.closest(".note-form");
  if (!form) return;
  const counter = form.querySelector("[data-note-chars]");
  const publicCheckbox = form.querySelector("[data-note-public]");
  const publicHint = form.querySelector("[data-note-public-hint]");
  if (counter) counter.textContent = `${ta.value.length}/500`;
  if (publicCheckbox) {
    const hasText = ta.value.trim().length > 0;
    publicCheckbox.disabled = !hasText;
    if (!hasText) publicCheckbox.checked = false;
    if (publicHint) publicHint.hidden = hasText;
  }
});
