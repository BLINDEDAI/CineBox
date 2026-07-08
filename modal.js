// Mi Cineteca — modal de detalle: ficha TMDB, alta desde modal y títulos similares.

const modalContent = el("modal-content");
let modalContext = null;
// Id de la película que la sección de edición del modal está editando (ADR-016).
// Contexto LOCAL del modal; null = sin sección de edición. Desde Fase 3 la tarjeta
// ya no tiene editores inline (solo póster + badge + estrellas): toda la edición
// de un título de la colección vive aquí.
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
    total_episodes: d.total_episodes, // total de episodios TMDB (series); denominador de «N/M» (BR-7); null si no aplica
    seasons: d.seasons,               // [{season_number, name, episode_count}] ex-especiales; puebla el selector de temporada
    watched_count: d.watched_count,   // marcas del usuario para este título (series); numerador de «N/M» en carga/reload (AC-6/AC-9)
  };
  // Inicializa el contador en memoria desde el conteo autoritativo de /api/details
  // para que la métrica muestre «N/M episodios» ya al abrir/recargar cuando hay
  // marcas y total conocido (AC-6/AC-9), sin esperar a un toggle en sesión. Tras
  // un POST de marcado se sigue usando el watched_count de esa respuesta.
  if (existing) existing.watched_count = d.watched_count;
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
      ${!existing ? (_guestMode ? `
      <div class="modal-add-btns modal-guest-cta" id="modal-guest-cta">
        <button class="btn btn-sm" type="button" data-guest-signup="guardar en tu coleccion">Regístrate para guardar</button>
      </div>` : `
      <div class="modal-add-btns" id="modal-add-btns">
        <button class="btn btn-sm" data-add-status="pendiente">+ Por ver</button>
        <button class="btn btn-sm btn-success" data-add-status="vista">✓ Vista</button>
        <button class="btn-secondary btn-sm" type="button" id="modal-add-to-list">+ Añadir a lista</button>
      </div>`) : ""}
      ${existing ? `<div class="modal-edit-section" id="modal-edit-section"></div>` : ""}
      ${d.trailer ? `<div class="modal-trailer"><a class="btn btn-sm" href="${esc(d.trailer)}" target="_blank" rel="noopener">▶ Ver tráiler</a></div>` : ""}
      ${providersHtml}
      ${directorHtml ? `<div class="modal-credits">${directorHtml}</div>` : ""}
      <div class="modal-overview"><p>${overviewHtml}</p></div>
      ${castHtml}
      <div class="modal-similar" id="modal-similar-section"></div>
      ${(existing && existing.media_type === "tv" && existing.tmdb_id) || (_guestMode && type === "tv") ? `<div class="modal-episodes-section" id="modal-episodes-section"></div>` : ""}
    </div>`;
  if (existing) _rerenderModalEditSection();
  if ((existing && existing.media_type === "tv" && existing.tmdb_id) || (_guestMode && type === "tv")) _rerenderModalEpisodesSection();
  const addBtns = document.getElementById("modal-add-btns");
  if (addBtns) {
    addBtns.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-add-status]");
      if (btn) addFromModal(btn.dataset.addStatus);
    });
  }
  // Modo invitado: el bloque de alta se sustituye por un CTA de registro (AC-6).
  // Abrir el detalle sigue permitido (lectura publica); solo el alta pide cuenta.
  // _promptSignup vive en app.js (cargado despues) — se invoca en el cuerpo del
  // manejador (tiempo de llamada), PS-003-safe.
  const guestCta = document.getElementById("modal-guest-cta");
  if (guestCta) {
    guestCta.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-guest-signup]");
      if (btn) _promptSignup(btn.getAttribute("data-guest-signup") || "guardar en tu coleccion");
    });
  }
  const addToListBtn = document.getElementById("modal-add-to-list");
  if (addToListBtn) {
    addToListBtn.addEventListener("click", () => {
      // Backstop defensivo: este boton no se renderiza en modo invitado, pero si
      // se alcanzara, pide cuenta en vez de abrir el selector user-scoped (AC-6).
      if (_guestMode) return _promptSignup("anadir a una lista");
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
  // Backstop de modo invitado (AC-6): nunca se emite el POST de alta; se abre el
  // prompt de registro. _promptSignup/_guestMode viven en app.js (tiempo de llamada).
  if (_guestMode) return _promptSignup("guardar en tu coleccion");
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
// fecha, plataforma, nota + "Reseña pública", añadir a lista, eliminar), siempre
// expandidos. Para series con tmdb_id sustituye el antiguo editor manual T/E por
// el tracker de episodios por temporada (series-episode-progress, BR-13). Reutiliza
// las clases de la tarjeta (.note-form/.date-form/.platform-picker/.stars/
// .note-public-toggle) + las nuevas .modal-status-pill/.modal-ep-*. La nota se escapa
// con esc() (convención SPA autenticada, US-043). Identificadores en inglés
// (US-001); etiquetas es-ES.
function _modalEditSectionHtml(m) {
  const noteVal = m.note || "";
  const hasNote = noteVal.trim().length > 0;
  return `
    <h4 class="modal-edit-title">Editar</h4>
    <div class="modal-edit-grid">
      <div class="modal-edit-field">
        <span class="modal-edit-label" id="modal-edit-status-label">Estado</span>
        <div class="modal-status-pills" role="group" aria-labelledby="modal-edit-status-label">
          ${[["pendiente", "Por ver"], ["viendo", "Viendo"], ["vista", "Vista"], ["abandonada", "Abandonada"]]
            .map(([val, label]) => `<button type="button" class="modal-status-pill${m.status === val ? " is-active" : ""}" data-action="edit-status-pick" data-status="${val}" aria-pressed="${m.status === val ? "true" : "false"}">${label}</button>`)
            .join("")}
        </div>
      </div>
      <div class="modal-edit-field">
        <span class="modal-edit-label" id="modal-edit-rating-label">Valoración</span>
        <div class="stars" data-action="edit-rating" role="group" aria-labelledby="modal-edit-rating-label">${starsHtml(m.rating || 0)}</div>
      </div>
      <div class="modal-edit-field">
        <label class="modal-edit-label" for="modal-edit-date">Fecha de visionado</label>
        <div class="date-form">
          <input class="date-input" id="modal-edit-date" type="date" value="${esc((m.watched_at || "").slice(0, 10))}" aria-label="Fecha de visionado">
          <button class="progress-save" data-action="edit-date-save" type="button" aria-label="Guardar fecha">✓</button>
          ${m.watched_at ? `<button class="progress-cancel" data-action="edit-date-clear" type="button" title="Quitar fecha" aria-label="Quitar fecha">—</button>` : ""}
        </div>
      </div>
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

// Sección de episodios (series-episode-progress): bloque independiente al FINAL
// del modal (tras reparto/similares). Antes vivía dentro de .modal-edit-section;
// se extrajo para no empujar sinopsis/reparto hacia abajo con listas largas.
function _modalEpisodesSectionHtml(m) {
  return `
    <h4 class="modal-edit-title">Episodios</h4>
    <span class="modal-ep-progress" data-ep-progress${_episodeProgressText(m) ? "" : " hidden"}>${esc(_episodeProgressText(m))}</span>
    <div class="modal-ep-season-select-row">
      <span class="modal-ep-season-label">Temporada</span>
      ${_seasonButtonsHtml()}
    </div>
    <div class="modal-ep-list" data-ep-list></div>`;
}

// Re-renderiza la sección de episodios desde la película en memoria y carga la
// temporada seleccionada. Solo series con tmdb_id; si no, vacía el bloque.
function _rerenderModalEpisodesSection() {
  const container = document.getElementById("modal-episodes-section");
  if (!container) return;
  // Modo invitado (AC-7): no hay título en la colección (movies vacío, modalEditId
  // null por diseño — ningún cargador de cuenta corre). El navegador de temporadas/
  // episodios se alimenta del contexto de detalle (modalContext.tmdbId), en solo
  // lectura: el backend devuelve watched:false en cada episodio (sin marcas de
  // visto) y no hay sección de edición. Las acciones de marcado siguen tras
  // _promptSignup en el delegador de #modal-content (un invitado navega, no marca).
  const movie = _guestMode
    ? { tmdb_id: modalContext && modalContext.tmdbId, media_type: "tv" }
    : movies.find((x) => x.id === modalEditId);
  if (!movie || movie.media_type !== "tv" || !movie.tmdb_id) { container.innerHTML = ""; return; }
  container.innerHTML = _modalEpisodesSectionHtml(movie);
  const active = container.querySelector("[data-action='ep-season-select'].is-active");
  if (active) _loadEpisodeSeason(movie, +active.dataset.season);
}

// ---- Tracker de episodios por temporada (series-episode-progress) ----
// Todo lo de abajo son helpers de render/manejo en TIEMPO DE LLAMADA (PS-003):
// no hay sentencias de nivel superior nuevas, ni módulo/orden de carga nuevo.

// Métrica de progreso (BR-7 / AC-6 / AC-9): «N/M episodios» cuando hay al menos
// una marca conocida en sesión y TMDB da el total; si no, la etiqueta heredada
// «S · E» derivada de la posición persistida; si tampoco, cadena vacía (oculto).
// watched_count arranca en 0 (la tabla movies no lo trae) y se conoce tras el
// primer POST de marcado en sesión — a partir de ahí muestra «N/M».
function _episodeProgressText(m) {
  // Prefiere el contador de la película en memoria (autoritativo tras un toggle en
  // sesión, incl. 0 al desmarcar todo → gana sobre el de contexto); si no, el
  // watched_count autoritativo de /api/details (carga/reload); si no, 0.
  const watched = m.watched_count ?? (modalContext && modalContext.watched_count) ?? 0;
  const total = modalContext && modalContext.total_episodes;
  if (watched > 0 && total) return `${watched}/${total} episodios`;
  if (m.current_season != null || m.current_episode != null) {
    return `S${m.current_season ?? "?"} · E${m.current_episode ?? "?"}`;
  }
  return "";
}

// Refresca el texto de la métrica en vivo tras un marcado, sin re-render total.
function _updateEpisodeProgressDisplay(m) {
  const container = document.getElementById("modal-episodes-section");
  if (!container) return;
  const node = container.querySelector("[data-ep-progress]");
  if (!node) return;
  const text = _episodeProgressText(m);
  node.textContent = text; // textContent → seguro; sin esc necesario
  node.hidden = !text;
}

// Selector de temporada: usa modalContext.seasons (ex-especiales); si no hay,
// cae a 1..total_seasons; si tampoco, a la temporada 1 (borde graceful).
function _seasonOptions() {
  const ctx = modalContext || {};
  if (Array.isArray(ctx.seasons) && ctx.seasons.length) {
    return ctx.seasons.map((s) => ({ n: s.season_number, label: s.name || `Temporada ${s.season_number}` }));
  }
  const total = ctx.total_seasons && ctx.total_seasons > 0 ? ctx.total_seasons : 1;
  const out = [];
  for (let n = 1; n <= total; n++) out.push({ n, label: `Temporada ${n}` });
  return out;
}

// Selector de temporada como fila de botones (pills), no <select> (estilo
// WatchForge). El botón activo lleva .is-active + aria-pressed. Se pinta desde
// _seasonOptions(); el clic lo maneja el delegador (ep-season-select).
function _seasonButtonsHtml(activeSeason) {
  const opts = _seasonOptions();
  const active = activeSeason != null ? activeSeason : (opts[0] && opts[0].n);
  return `<div class="modal-ep-seasons" role="group" aria-label="Temporadas">
        ${opts.map((o) => `<button type="button" class="modal-ep-season-pill${o.n === active ? " is-active" : ""}" data-action="ep-season-select" data-season="${esc(o.n)}" aria-pressed="${o.n === active ? "true" : "false"}">${esc(o.label)}</button>`).join("")}
      </div>`;
}

// Still guardado: mismo allow-list que las carátulas/cast — solo image.tmdb.org
// a partir del still_path relativo de TMDB; si falta, placeholder de texto (FILM).
function _episodeStillHtml(stillPath) {
  if (stillPath) return `<img class="modal-ep-still" src="https://image.tmdb.org/t/p/w300${esc(stillPath)}" alt="" loading="lazy">`;
  return `<div class="modal-ep-still modal-ep-still-fallback" aria-hidden="true">${FILM}</div>`;
}

// Estado de la temporada (full/partial/none) → control de marcar/desmarcar (BR-6).
function _seasonControlHtml(seasonNumber, state) {
  const stateLabel = state === "all" ? "Temporada completa" : state === "partial" ? "Temporada a medias" : "Temporada sin ver";
  const s = esc(seasonNumber);
  return `
      <span class="modal-ep-season-state" data-ep-season-state data-state="${esc(state)}">${stateLabel}</span>
      <button class="btn-secondary btn-sm modal-ep-season-btn" type="button" data-action="ep-season-mark" data-season="${s}"${state === "all" ? " disabled" : ""}>Marcar temporada</button>
      <button class="btn-secondary btn-sm modal-ep-season-btn" type="button" data-action="ep-season-unmark" data-season="${s}"${state === "none" ? " disabled" : ""}>Desmarcar temporada</button>`;
}

// Fila de episodio: still guardado, número + título, fecha, duración, sinopsis y
// el toggle de visto (aria-pressed). Cada valor de episodio va por esc() (AC-11).
function _episodeRowHtml(ep, seasonNumber) {
  const epNum = ep.episode_number;
  const runtime = (ep.runtime === null || ep.runtime === undefined) ? "—" : `${esc(ep.runtime)} min`;
  const airDate = ep.air_date ? esc(ep.air_date) : "—";
  const overview = ep.overview ? esc(ep.overview) : "";
  const watched = !!ep.watched;
  return `
        <li class="modal-ep-item${watched ? " is-watched" : ""}" data-ep-row data-season="${esc(seasonNumber)}" data-episode="${esc(epNum)}">
          ${_episodeStillHtml(ep.still_path)}
          <div class="modal-ep-body">
            <div class="modal-ep-head">
              <span class="modal-ep-num">${esc(epNum)}</span>
              <span class="modal-ep-name">${esc(ep.name || "")}</span>
            </div>
            <div class="modal-ep-meta">
              <span class="modal-ep-air">${airDate}</span>
              <span class="modal-ep-runtime">${runtime}</span>
            </div>
            ${overview ? `<p class="modal-ep-overview">${overview}</p>` : ""}
          </div>
          <button class="modal-ep-toggle" type="button" data-action="ep-toggle" data-season="${esc(seasonNumber)}" data-episode="${esc(epNum)}" aria-pressed="${watched ? "true" : "false"}" aria-label="${watched ? "Marcar episodio como no visto" : "Marcar episodio como visto"}">${watched ? "✓ Visto" : "Marcar visto"}</button>
        </li>`;
}

// Pinta la lista de episodios + la barra de control de temporada dentro del
// contenedor [data-ep-list].
function _renderEpisodeList(listEl, season, seasonNumber) {
  const episodes = (season && season.episodes) || [];
  const total = episodes.length;
  const watchedCount = episodes.filter((e) => e.watched).length;
  const state = total === 0 ? "none" : watchedCount === total ? "all" : watchedCount === 0 ? "none" : "partial";
  listEl.innerHTML = `
      <div class="modal-ep-season-bar">${_seasonControlHtml(seasonNumber, state)}</div>
      ${total ? `<ul class="modal-ep-items" role="list">${episodes.map((ep) => _episodeRowHtml(ep, seasonNumber)).join("")}</ul>`
              : `<p class="modal-ep-empty">No hay episodios para esta temporada.</p>`}`;
}

// Carga (fetch) y pinta una temporada. Guardas anti-carrera: si el usuario cambió
// de temporada o cerró el modal mientras cargaba, no se aplica el resultado.
async function _loadEpisodeSeason(movie, seasonNumber) {
  const container = document.getElementById("modal-episodes-section");
  if (!container) return;
  const listEl = container.querySelector("[data-ep-list]");
  if (!listEl || !movie.tmdb_id) return;
  listEl.innerHTML = '<p class="modal-ep-loading">Cargando episodios…</p>';
  const { ok, data } = await api(`/api/tv/${movie.tmdb_id}/season/${seasonNumber}`);
  if (!listEl.isConnected) return; // modal cerrado / re-render mientras cargaba
  const active = container.querySelector("[data-action='ep-season-select'].is-active");
  if (!active || +active.dataset.season !== seasonNumber) return; // el usuario ya cambió de temporada
  if (!ok || !data || !data.ok) {
    listEl.innerHTML = `<p class="modal-ep-empty">${data && data.needs_key ? "Sin detalle de episodios disponible." : "No se pudieron cargar los episodios."}</p>`;
    return;
  }
  _renderEpisodeList(listEl, data.season, seasonNumber);
}

// Refleja una marca en el DOM ya pintado (sin re-render/re-fetch): actualiza las
// filas afectadas y recalcula el estado de la barra de temporada.
function _setRowWatched(row, watched) {
  row.classList.toggle("is-watched", watched);
  const toggle = row.querySelector("[data-action='ep-toggle']");
  if (!toggle) return;
  toggle.setAttribute("aria-pressed", watched ? "true" : "false");
  toggle.setAttribute("aria-label", watched ? "Marcar episodio como no visto" : "Marcar episodio como visto");
  toggle.textContent = watched ? "✓ Visto" : "Marcar visto";
}

function _refreshSeasonControl(listEl, seasonNumber) {
  const rows = Array.from(listEl.querySelectorAll(`[data-ep-row][data-season="${seasonNumber}"]`));
  const total = rows.length;
  const watchedCount = rows.filter((r) => r.classList.contains("is-watched")).length;
  const state = total === 0 ? "none" : watchedCount === total ? "all" : watchedCount === 0 ? "none" : "partial";
  const bar = listEl.querySelector(".modal-ep-season-bar");
  if (bar) bar.innerHTML = _seasonControlHtml(seasonNumber, state);
}

function _applyEpisodeMarkToDom(body) {
  const container = document.getElementById("modal-episodes-section");
  if (!container) return;
  const listEl = container.querySelector("[data-ep-list]");
  if (!listEl) return;
  const rows = listEl.querySelectorAll(`[data-ep-row][data-season="${body.season}"]`);
  if (Array.isArray(body.episodes)) {
    rows.forEach((row) => _setRowWatched(row, body.watched)); // marcado de temporada
  } else if (body.episode != null) {
    rows.forEach((row) => { if (+row.dataset.episode === body.episode) _setRowWatched(row, body.watched); });
  }
  _refreshSeasonControl(listEl, body.season);
}

// POST de marcado (individual o temporada) → actualiza la posición derivada y el
// contador en la película en memoria (BR-8) y refleja el resultado en el DOM.
async function _markEpisodes(movie, body) {
  const { ok, data } = await api(`/api/movies/${movie.id}/episodes`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!ok || !data || !data.ok) {
    showMessage("No se pudo actualizar el episodio. Inténtalo de nuevo.", "error");
    return;
  }
  movie.current_season = data.current_season;
  movie.current_episode = data.current_episode;
  movie.watched_count = data.watched_count;
  _applyEpisodeMarkToDom(body);
  _updateEpisodeProgressDisplay(movie);
}

// Re-renderiza la sección de edición desde la película en memoria más reciente
// (movies.find, AC-11). Si la película ya no está (borrada / colección recargada),
// cierra el modal con elegancia en vez de dejar un editor obsoleto. Si hay tracker
// de episodios, carga la temporada seleccionada (AC-1/AC-2).
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
  if (!action || !(action.startsWith("edit-") || action.startsWith("ep-"))) return;
  // Backstop de modo invitado (AC-6): la seccion de edicion/episodios nunca se
  // renderiza para un invitado (no hay titulos en coleccion), pero si un camino
  // la alcanzara, pide cuenta en vez de emitir una escritura user-scoped.
  if (_guestMode) return _promptSignup("gestionar tu coleccion");
  const movie = movies.find((x) => x.id === modalEditId);
  if (!movie) { closeModal(); return; }
  const id = movie.id;
  // ---- Selección de temporada (botones/pills) ----
  if (action === "ep-season-select") {
    const season = +actionEl.dataset.season;
    const container = document.getElementById("modal-episodes-section");
    if (container) {
      container.querySelectorAll("[data-action='ep-season-select']").forEach((btn) => {
        const on = +btn.dataset.season === season;
        btn.classList.toggle("is-active", on);
        btn.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }
    _loadEpisodeSeason(movie, season);
    return;
  }
  // ---- Marcado de episodios (tracker de series) ----
  if (action === "ep-toggle") {
    const season  = +actionEl.dataset.season;
    const episode = +actionEl.dataset.episode;
    const watched = actionEl.getAttribute("aria-pressed") !== "true";
    _markEpisodes(movie, { season, episode, watched });
    return;
  }
  if (action === "ep-season-mark" || action === "ep-season-unmark") {
    const season = +actionEl.dataset.season;
    const listEl = modalContent.querySelector("[data-ep-list]");
    if (!listEl) return;
    const episodes = Array.from(listEl.querySelectorAll(`[data-ep-row][data-season="${season}"]`))
      .map((row) => +row.dataset.episode);
    if (!episodes.length) return;
    _markEpisodes(movie, { season, episodes, watched: action === "ep-season-mark" });
    return;
  }
  if (action === "edit-status-pick") {
    const status = actionEl.dataset.status;
    const payload = { status };
    // Paridad con el seam de estado (app.js): al pasar a "vista", rellena fecha
    // y plataforma por defecto si faltan (AC-6/AC-7).
    if (status === "vista" && !movie.watched_at) payload.watched_at = todayIsoDate();
    if (status === "vista" && !movie.platform) {
      const defaultPlatform = getPref("default_platform", PLATFORMS, null);
      if (defaultPlatform) payload.platform = defaultPlatform;
    }
    _modalEditSave(id, payload);
    return;
  }
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
