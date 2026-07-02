// CineBox — página pública anónima (perfiles y listas compartidas).
// Autocontenida: NO carga supabase-js ni los 7 módulos del SPA autenticado.
// Read-only. Todo el contenido del usuario se renderiza como TEXTO (textContent),
// nunca con innerHTML de datos de usuario (prevención de XSS — invariants § Security).

(function () {
  "use strict";

  const root = document.getElementById("public-root");

  // Guarded-<img> allow-list para avatares subidos (custom-avatar-upload). Esta
  // página es autónoma (no carga app.js), así que lleva su PROPIA comprobación de
  // origen. El src solo se fija (vía setAttribute) tras casar un origen de
  // Supabase-Storage-avatars — nunca se interpola la URL en HTML.
  const AVATAR_URL_RE = /^https:\/\/[a-z0-9-]+\.supabase\.co\/storage\/v1\/object\/public\/avatars\//;

  // ── Helpers DOM (sin innerHTML de datos de usuario) ───────────────────────
  function elem(tag, opts) {
    const node = document.createElement(tag);
    if (!opts) return node;
    if (opts.className) node.className = opts.className;
    if (opts.text != null) node.textContent = String(opts.text); // textContent = seguro
    if (opts.attrs) {
      for (const k in opts.attrs) {
        if (opts.attrs[k] != null) node.setAttribute(k, String(opts.attrs[k]));
      }
    }
    if (opts.children) {
      for (const child of opts.children) {
        if (child) node.appendChild(child);
      }
    }
    return node;
  }

  function clearRoot() {
    while (root.firstChild) root.removeChild(root.firstChild);
  }

  function mediaIcon(mt) { return mt === "tv" ? "📺" : "🎬"; }

  const STATUS_LABEL = {
    pendiente: "Por ver", viendo: "Viendo", vista: "Vista", abandonada: "Abandonada",
  };

  // Estrellas como texto (★/☆) — sin botones interactivos (vista de solo lectura).
  function starsText(rating) {
    const n = Math.max(0, Math.min(5, Number(rating) || 0));
    return "★".repeat(n) + "☆".repeat(5 - n);
  }

  // ── Poster: imagen TMDB (CSP permite https://image.tmdb.org) o fallback ───
  function posterNode(item) {
    const wrap = elem("div", { className: "pub-poster" });
    const url = item.poster_url;
    // Solo aceptamos URLs de TMDB (mismo allow-list que el backend); cualquier
    // otra cosa cae al fallback. La URL va como atributo src vía setAttribute,
    // nunca interpolada en HTML.
    if (url && /^https:\/\/image\.tmdb\.org\//.test(url)) {
      const img = elem("img", {
        attrs: { src: url, alt: item.title || "", loading: "lazy" },
      });
      wrap.appendChild(img);
    } else {
      const fb = elem("div", { className: "pub-poster-fallback" });
      fb.appendChild(elem("span", { className: "pub-poster-fallback-icon", text: "🎬", attrs: { "aria-hidden": "true" } }));
      fb.appendChild(elem("span", { text: item.title || "" }));
      wrap.appendChild(fb);
    }
    return wrap;
  }

  // ── Mensajes de estado (404 / 429 / error de red) ─────────────────────────
  function showState(title, detail) {
    clearRoot();
    const box = elem("div", { className: "pub-state" });
    box.appendChild(elem("h1", { className: "pub-state-title", text: title }));
    if (detail) box.appendChild(elem("p", { className: "pub-state-detail", text: detail }));
    const home = elem("a", { className: "btn pub-state-home", text: "Ir a CineBox", attrs: { href: "/" } });
    box.appendChild(home);
    root.appendChild(box);
  }

  function showNotFound() {
    showState("No disponible", "Esta página no existe o su propietario la ha hecho privada.");
  }

  function showRateLimited() {
    showState("Demasiadas solicitudes", "Has alcanzado el límite de peticiones. Espera unos instantes e inténtalo de nuevo.");
  }

  function showGenericError() {
    showState("Algo ha ido mal", "No se ha podido cargar el contenido. Inténtalo de nuevo más tarde.");
  }

  // ── Fetch público (sin Authorization; mismo origen) ───────────────────────
  async function fetchPublic(path) {
    let res;
    try {
      res = await fetch(path, { headers: { Accept: "application/json" } });
    } catch (e) {
      return { networkError: true };
    }
    if (res.status === 404) return { notFound: true };
    if (res.status === 429) return { rateLimited: true };
    if (!res.ok) return { error: true };
    const data = await res.json().catch(() => null);
    if (!data || !data.ok) return { error: true };
    return { data };
  }

  // ── Render: rejilla de colección ──────────────────────────────────────────
  function collectionGrid(items) {
    const grid = elem("div", { className: "pub-grid", attrs: { role: "list" } });
    for (const item of items) {
      const card = elem("article", { className: "pub-card", attrs: { role: "listitem" } });
      card.appendChild(posterNode(item));

      const badges = elem("div", { className: "pub-card-badges" });
      if (item.status) {
        badges.appendChild(elem("span", {
          className: "pub-status-badge " + String(item.status).replace(/[^a-z]/gi, ""),
          text: STATUS_LABEL[item.status] || item.status,
        }));
      }
      badges.appendChild(elem("span", { className: "pub-media-badge", text: mediaIcon(item.media_type), attrs: { "aria-hidden": "true" } }));
      card.appendChild(badges);

      const body = elem("div", { className: "pub-card-body" });
      body.appendChild(elem("div", { className: "pub-card-title", text: item.title || "" }));

      const meta = elem("div", { className: "pub-card-meta" });
      if (item.media_type === "tv" && item.current_season) {
        const seasonTxt = item.total_seasons
          ? "T" + item.current_season + " de " + item.total_seasons
          : "T" + item.current_season;
        meta.appendChild(elem("span", { className: "pub-card-season", text: seasonTxt }));
      }
      if (item.rating) {
        meta.appendChild(elem("span", {
          className: "pub-card-stars",
          text: starsText(item.rating),
          attrs: { "aria-label": item.rating + " de 5 estrellas" },
        }));
      }
      if (meta.childNodes.length) body.appendChild(meta);

      card.appendChild(body);
      grid.appendChild(card);
    }
    return grid;
  }

  // ── Render: panel de estadísticas (proyección de compute_level) ───────────
  function statsPanel(stats) {
    const panel = elem("section", { className: "pub-stats", attrs: { "aria-label": "Estadísticas" } });
    panel.appendChild(elem("h2", { className: "pub-section-title", text: "Estadísticas" }));

    const head = elem("div", { className: "pub-level-head" });
    head.appendChild(elem("span", {
      className: "pub-level-name",
      text: "Nivel " + (stats.level ?? "?") + " · " + (stats.name || ""),
    }));
    head.appendChild(elem("span", { className: "pub-level-points", text: (stats.points ?? 0) + " pts" }));
    panel.appendChild(head);

    const barWrap = elem("div", { className: "pub-level-bar-wrap" });
    const bar = elem("div", { className: "pub-level-bar" });
    // El ancho se fija vía CSSOM (no atributo style= inline): la CSP estricta
    // bloquea estilos inline pero no las mutaciones JS de element.style.
    bar.style.width = (Number(stats.progress_pct) || 0) + "%";
    barWrap.appendChild(bar);
    panel.appendChild(barWrap);

    const nextTxt = stats.next_name
      ? "Faltan " + (stats.points_to_next ?? 0) + " pts para " + stats.next_name
      : "Nivel máximo alcanzado";
    panel.appendChild(elem("div", { className: "pub-level-next", text: nextTxt }));
    return panel;
  }

  // ── Render: tarjetas de listas públicas del perfil ────────────────────────
  function publicListsSection(lists) {
    const section = elem("section", { className: "pub-lists", attrs: { "aria-label": "Listas públicas" } });
    section.appendChild(elem("h2", { className: "pub-section-title", text: "Listas públicas" }));
    const wrap = elem("div", { className: "pub-list-cards", attrs: { role: "list" } });
    for (const list of lists) {
      const count = Number(list.item_count) || 0;
      const card = elem("a", {
        className: "pub-list-card",
        attrs: { href: "/l/" + encodeURIComponent(list.share_token), role: "listitem" },
      });
      card.appendChild(elem("span", { className: "pub-list-card-name", text: list.name || "Lista" }));
      card.appendChild(elem("span", {
        className: "pub-list-card-count",
        text: count + (count === 1 ? " título" : " títulos"),
      }));
      wrap.appendChild(card);
    }
    section.appendChild(wrap);
    return section;
  }

  // Avatar del perfil público: <img> guardado (src vía setAttribute solo tras
  // casar la allow-list de Storage) cuando hay avatar_url válido; si no, un
  // avatar generado (iniciales + gradiente CSSOM, sin inline style=). Lleva un
  // nombre accesible (alt) — parte de la identidad de cabecera.
  function avatarNode(profile) {
    const username = profile.username || "";
    const url = profile.avatar_url;
    const wrap = elem("div", { className: "pub-profile-avatar" });
    if (url && AVATAR_URL_RE.test(url)) {
      const img = elem("img", {
        className: "pub-profile-avatar-img",
        attrs: { alt: "Avatar de @" + username, loading: "lazy" },
      });
      img.setAttribute("src", url); // src tras casar la allow-list
      wrap.appendChild(img);
    } else {
      wrap.classList.add("pub-profile-avatar-generated");
      wrap.setAttribute("aria-label", "Avatar de @" + username);
      wrap.setAttribute("role", "img");
      const initials = username ? username.slice(0, 2).toUpperCase() : "?";
      wrap.appendChild(elem("span", { className: "pub-profile-avatar-initials", text: initials, attrs: { "aria-hidden": "true" } }));
      wrap.style.backgroundImage = _publicAvatarGradient(username);
    }
    return wrap;
  }

  // Gradiente determinista (mismo algoritmo FNV-1a que app.js _avatarGradient):
  // mismo username → mismo gradiente. Se fija vía CSSOM (la CSP prohíbe style=).
  function _publicAvatarGradient(username) {
    const u = username || "";
    let hash = 2166136261;
    for (let i = 0; i < u.length; i++) {
      hash ^= u.charCodeAt(i);
      hash = (hash * 16777619) >>> 0;
    }
    if (!u) return "linear-gradient(135deg, #3a3f4b, #21242c)";
    const h1 = hash % 360;
    const h2 = (h1 + 40) % 360;
    return "linear-gradient(135deg, hsl(" + h1 + " 55% 42%), hsl(" + h2 + " 55% 28%))";
  }

  // ── Render perfil completo ────────────────────────────────────────────────
  function renderProfile(profile) {
    clearRoot();
    document.title = "@" + (profile.username || "") + " · CINEBOX";

    const headerSec = elem("section", { className: "pub-profile-head" });
    headerSec.appendChild(elem("p", { className: "pub-eyebrow", text: "Perfil público" }));
    headerSec.appendChild(avatarNode(profile));
    const h1 = elem("h1", { className: "pub-profile-name" });
    h1.appendChild(elem("span", { className: "pub-at", text: "@", attrs: { "aria-hidden": "true" } }));
    h1.appendChild(document.createTextNode(profile.username || ""));
    headerSec.appendChild(h1);
    root.appendChild(headerSec);

    const hasCollection = Array.isArray(profile.collection) && profile.collection.length > 0;
    const hasStats = profile.stats && typeof profile.stats === "object";
    const hasLists = Array.isArray(profile.lists) && profile.lists.length > 0;

    if (hasStats) root.appendChild(statsPanel(profile.stats));

    if (Array.isArray(profile.collection)) {
      const colSec = elem("section", { className: "pub-collection", attrs: { "aria-label": "Colección" } });
      colSec.appendChild(elem("h2", { className: "pub-section-title", text: "Colección" }));
      if (hasCollection) {
        colSec.appendChild(collectionGrid(profile.collection));
      } else {
        colSec.appendChild(elem("p", { className: "pub-empty", text: "Esta colección está vacía." }));
      }
      root.appendChild(colSec);
    }

    if (hasLists) root.appendChild(publicListsSection(profile.lists));

    if (!hasCollection && !hasStats && !hasLists) {
      root.appendChild(elem("p", { className: "pub-empty", text: "Este perfil aún no muestra contenido público." }));
    }
  }

  // ── Render lista compartida ───────────────────────────────────────────────
  function renderList(list) {
    clearRoot();
    document.title = (list.name || "Lista") + " · CINEBOX";

    const headSec = elem("section", { className: "pub-list-head" });
    headSec.appendChild(elem("p", { className: "pub-eyebrow", text: "Lista compartida" }));
    headSec.appendChild(elem("h1", { className: "pub-list-title", text: list.name || "Lista" }));
    if (list.owner_username) {
      const by = elem("p", { className: "pub-list-owner" });
      by.appendChild(document.createTextNode("de "));
      const link = elem("a", {
        className: "pub-list-owner-link",
        text: "@" + list.owner_username,
        attrs: { href: "/u/" + encodeURIComponent(list.owner_username) },
      });
      by.appendChild(link);
      headSec.appendChild(by);
    }
    root.appendChild(headSec);

    const items = Array.isArray(list.items) ? list.items : [];
    if (items.length) {
      root.appendChild(collectionGrid(items));
    } else {
      root.appendChild(elem("p", { className: "pub-empty", text: "Esta lista está vacía." }));
    }
  }

  // ── Router: decide perfil vs lista por location.pathname ──────────────────
  async function route() {
    const path = location.pathname;

    const profileMatch = path.match(/^\/u\/([a-z0-9_-]{3,30})\/?$/);
    if (profileMatch) {
      const username = profileMatch[1];
      const result = await fetchPublic("/api/public/profile/" + encodeURIComponent(username));
      if (result.notFound) return showNotFound();
      if (result.rateLimited) return showRateLimited();
      if (result.networkError || result.error) return showGenericError();
      return renderProfile(result.data.profile || {});
    }

    const listMatch = path.match(/^\/l\/([0-9a-fA-F-]{36})\/?$/);
    if (listMatch) {
      const token = listMatch[1];
      const result = await fetchPublic("/api/public/list/" + encodeURIComponent(token));
      if (result.notFound) return showNotFound();
      if (result.rateLimited) return showRateLimited();
      if (result.networkError || result.error) return showGenericError();
      return renderList(result.data.list || {});
    }

    showNotFound();
  }

  route();
})();
