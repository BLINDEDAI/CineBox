// Mi Cineteca — Descubrir: resultados de búsqueda, chips de género, discover y tendencias.

const DISCOVER_GENRES = [
  { id: 28,    name: "Acción" },
  { id: 27,    name: "Terror" },
  { id: 35,    name: "Comedia" },
  { id: 18,    name: "Drama" },
  { id: 878,   name: "Ciencia ficción" },
  { id: 16,    name: "Animación" },
  { id: 53,    name: "Suspense" },
  { id: 10749, name: "Romance" },
  { id: 12,    name: "Aventura" },
  { id: 80,    name: "Crimen" },
];

function renderResults() {
  const visibleResults = lastResults
    .map((m, i) => ({ item: m, index: i }))
    .filter(({ item }) => mediaFilter === "todo" || item.media_type === mediaFilter);

  const loadMoreEl = el("discover-load-more");

  if (lastResults.length && !visibleResults.length) {
    const label = mediaFilter === "movie" ? "películas" : "series";
    resultsEl.innerHTML = `<p class="empty">No hay ${label} en estos resultados. Prueba con "Pelis y series".</p>`;
    resultsSection.hidden = false;
    if (loadMoreEl) loadMoreEl.hidden = true;
    return;
  }

  resultsEl.innerHTML = visibleResults.map(({ item: m, index: i }) => `
    <article class="card" data-idx="${i}">
      <div class="poster${m.tmdb_id ? " cursor-pointer" : ""}"${m.tmdb_id ? ` data-tmdb="${esc(m.tmdb_id)}" data-type="${esc(m.media_type)}"` : ""}>
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
  if (loadMoreEl) loadMoreEl.hidden = !(resultsMode === "discover" && discoverHasMore && visibleResults.length > 0);
}

function renderGenreChips() {
  const container = el("genre-chips");
  if (!container) return;
  container.innerHTML = DISCOVER_GENRES.map((g) =>
    `<button class="genre-chip${activeGenreId === g.id ? " active" : ""}" data-genre-id="${g.id}" type="button">${esc(g.name)}</button>`
  ).join("");
}

async function loadDiscover(genreId, page = 1, append = false) {
  const genre = DISCOVER_GENRES.find((g) => g.id === genreId);
  resultsMode = "discover";
  el("results-title").textContent = genre ? genre.name : "Por género";
  el("results-close").hidden = true;
  if (!append) showMessage("Cargando...");
  const type = mediaFilter === "todo" ? "all" : mediaFilter;
  const { data } = await api(`/api/discover?genre_id=${genreId}&type=${type}&page=${page}&sort=${discoverSort}`);
  if (data.ok) {
    lastResults = append ? [...lastResults, ...data.results] : data.results;
    discoverPage   = data.page;
    discoverHasMore = data.has_more;
    renderResults();
    showMessage("");
  } else if (data.needs_key) {
    lastResults     = [];
    discoverHasMore = false;
    renderResults();
    showMessage("Descubrir desactivado (sin TMDB key). Configura la clave (ver README).", "error");
  } else {
    showMessage(data.error || "No se pudieron cargar los resultados.", "error");
  }
}

async function loadTrending() {
  discoverHasMore = false;
  discoverPage    = 1;
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
