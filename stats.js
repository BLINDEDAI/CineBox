// Mi Cineteca — Estadísticas: nivel del servidor y panel de métricas.

let levelData = null; // nivel calculado en el servidor (/api/level); null hasta que carga

async function loadLevel() {
  const { ok, data } = await api("/api/level");
  if (ok && data.ok) {
    levelData = data;
    renderStatsView();
  }
}

// ---- Estadísticas ----
function renderStatsView() {
  const container = el("stats-content");
  if (!container) return;

  const thisYear = new Date().getFullYear().toString();
  const vistas = movies.filter((m) => m.status === "vista");
  const pelisVistas = vistas.filter((m) => m.media_type !== "tv").length;
  const seriesVistas = vistas.filter((m) => m.media_type === "tv").length;
  const esteAnio = vistas.filter((m) => (m.watched_at || "").startsWith(thisYear)).length;
  const pendientes = movies.filter((m) => m.status === "pendiente").length;
  const rated = movies.filter((m) => m.rating);
  const avgRating = rated.length
    ? (rated.reduce((a, m) => a + m.rating, 0) / rated.length).toFixed(1)
    : null;

  // Contar géneros
  const genreCount = {};
  for (const m of movies) {
    if (!m.genres) continue;
    for (const g of m.genres.split(", ")) {
      genreCount[g] = (genreCount[g] || 0) + 1;
    }
  }
  const topGenres = Object.entries(genreCount)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6);
  const maxGenre = topGenres[0]?.[1] || 1;

  const statCard = (value, label, sub = "") => `
    <div class="scard">
      <div class="scard-value">${value}</div>
      <div class="scard-label">${label}</div>
      ${sub ? `<div class="scard-sub">${sub}</div>` : ""}
    </div>`;

  const starsRow = rated.length ? [1, 2, 3, 4, 5].map((s) => {
    const count = rated.filter((m) => m.rating === s).length;
    const pct = Math.round((count / rated.length) * 100);
    return `<div class="srating-row">
      <span class="srating-star">${"★".repeat(s)}</span>
      <div class="srating-bar-wrap"><div class="srating-bar" data-pct="${pct}"></div></div>
      <span class="srating-count">${count}</span>
    </div>`;
  }).join("") : `<p class="muted smuted-sm">Aún no has puntuado nada.</p>`;

  const genresHtml = topGenres.length ? topGenres.map(([name, count]) => {
    const pct = Math.round((count / maxGenre) * 100);
    return `<div class="sgenre-row">
      <span class="sgenre-name">${esc(name)}</span>
      <div class="sgenre-bar-wrap"><div class="sgenre-bar" data-pct="${pct}"></div></div>
      <span class="sgenre-count">${count}</span>
    </div>`;
  }).join("") : `<p class="muted smuted-sm">Añade títulos desde Descubrir para ver tus géneros.</p>`;

  // Contar plataformas
  const platformCount = {};
  for (const m of movies) {
    if (!m.platform) continue;
    platformCount[m.platform] = (platformCount[m.platform] || 0) + 1;
  }
  const topPlatforms = Object.entries(platformCount).sort((a, b) => b[1] - a[1]);
  const maxPlatform = topPlatforms[0]?.[1] || 1;

  const platformsHtml = topPlatforms.length ? topPlatforms.map(([name, count]) => {
    const pct = Math.round((count / maxPlatform) * 100);
    return `<div class="sgenre-row">
      <span class="sgenre-name">${esc(name)}</span>
      <div class="sgenre-bar-wrap"><div class="splatform-bar" data-pct="${pct}"></div></div>
      <span class="sgenre-count">${count}</span>
    </div>`;
  }).join("") : `<p class="muted smuted-sm">Aún no has marcado dónde viste ningún título.</p>`;

  let levelHtml = "";
  if (levelData) {
    const nextTxt = levelData.next_name
      ? `faltan ${levelData.points_to_next} pts para ${esc(levelData.next_name)}`
      : "Nivel máximo alcanzado";
    levelHtml = `
      <div class="slevel">
        <div class="slevel-head">
          <span class="slevel-name">Nivel ${levelData.level} · ${esc(levelData.name)}</span>
          <span class="slevel-points">${levelData.points} pts</span>
        </div>
        <div class="slevel-bar-wrap"><div class="slevel-bar" data-pct="${levelData.progress_pct}"></div></div>
        <div class="slevel-next">${nextTxt}</div>
      </div>`;
  }

  container.innerHTML = `
    ${levelHtml}
    <div class="stats-grid">
      ${statCard(pelisVistas, "Películas vistas")}
      ${statCard(seriesVistas, "Series vistas")}
      ${statCard(esteAnio, `Vistas en ${thisYear}`)}
      ${statCard(pendientes, "Pendientes")}
    </div>
    <div class="stats-panels">
      <div class="spanel">
        <h3 class="spanel-title">Valoraciones</h3>
        ${avgRating ? `<div class="savg-rating"><span class="savg-number">${avgRating}</span><span class="savg-star">★</span><span class="savg-total">sobre ${rated.length} valoradas</span></div>` : ""}
        <div class="srating-list">${starsRow}</div>
      </div>
      <div class="spanel">
        <h3 class="spanel-title">Géneros favoritos</h3>
        <div class="sgenre-list">${genresHtml}</div>
      </div>
      <div class="spanel">
        <h3 class="spanel-title">Plataformas</h3>
        <div class="sgenre-list">${platformsHtml}</div>
      </div>
    </div>`;

  // El ancho de cada barra se fija vía CSSOM (element.style), no con un
  // atributo style= en el HTML: la CSP estricta (default-src 'self', sin
  // 'unsafe-inline') bloquea los style inline pero no las mutaciones JS.
  container.querySelectorAll("[data-pct]").forEach((bar) => {
    bar.style.width = (Number(bar.dataset.pct) || 0) + "%";
  });
}
