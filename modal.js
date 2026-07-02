// Mi Cineteca — modal de detalle: ficha TMDB, alta desde modal y títulos similares.

const modalContent = el("modal-content");
let modalContext = null;

// ---- Modal detalle ----
function closeModal() {
  modalEl.classList.remove("is-open");
  setTimeout(() => { modalEl.hidden = true; modalContent.innerHTML = ""; modalContext = null; }, 220);
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
      ${d.trailer ? `<div class="modal-trailer"><a class="btn btn-sm" href="${esc(d.trailer)}" target="_blank" rel="noopener">▶ Ver tráiler</a></div>` : ""}
      ${providersHtml}
      ${directorHtml ? `<div class="modal-credits">${directorHtml}</div>` : ""}
      <div class="modal-overview"><p>${overviewHtml}</p></div>
      ${castHtml}
      <div class="modal-similar" id="modal-similar-section"></div>
    </div>`;
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
