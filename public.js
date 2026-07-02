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

  // ── Sesión del visitante (session-aware follow control) ───────────────────
  // ADR-006: esta página NO carga supabase-js. Para permitir seguir/dejar de
  // seguir sin él, leemos el token de sesión que supabase-js ya persistió en
  // localStorage bajo la clave por defecto `sb-{project_ref}-auth-token`, donde
  // project_ref es el subdominio de supabase_url (que obtenemos de /api/config,
  // ya anónimo). Todo va en try/catch: cualquier ausencia/parse-fail → tratado
  // como no-autenticado (login-link, sin error). El token es el propio token de
  // sesión del visitante, se envía solo en la cabecera Authorization sobre TLS
  // del mismo origen; nunca se registra, ni se pone en una URL, ni se renderiza.
  function _projectRefFromUrl(supabaseUrl) {
    try {
      const host = new URL(supabaseUrl).hostname; // {ref}.supabase.co
      const ref = host.split(".")[0];
      return /^[a-z0-9]+$/.test(ref) ? ref : null;
    } catch (e) {
      return null;
    }
  }

  // Devuelve el access_token persistido, o null si no hay sesión válida.
  async function readViewerToken() {
    let cfg;
    try {
      const res = await fetch("/api/config", { headers: { Accept: "application/json" } });
      cfg = await res.json();
    } catch (e) {
      return null;
    }
    if (!cfg || !cfg.supabase_url) return null;
    const ref = _projectRefFromUrl(cfg.supabase_url);
    if (!ref) return null;
    try {
      const raw = localStorage.getItem("sb-" + ref + "-auth-token");
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      const token = parsed && typeof parsed === "object" ? parsed.access_token : null;
      return typeof token === "string" && token ? token : null;
    } catch (e) {
      return null;
    }
  }

  // Fetch autenticado (Bearer). Devuelve {status, data} o {networkError}. Se
  // ramifica por STATUS (401 → login-link), nunca por strings del body.
  async function fetchAuthed(path, method, token, body) {
    let res;
    try {
      const opts = {
        method: method || "GET",
        headers: { Accept: "application/json", Authorization: "Bearer " + token },
      };
      if (body != null) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
      }
      res = await fetch(path, opts);
    } catch (e) {
      return { networkError: true };
    }
    const data = await res.json().catch(() => null);
    return { status: res.status, data };
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

  // ── Follow control (session-aware) ────────────────────────────────────────
  // Toda la zona se construye con createElement + textContent (regla XSS de la
  // página pública). Estados:
  //   - no-auth / 401 / expirado → enlace "Inicia sesión para seguir" a /
  //   - is_self → sin botón (no puedes seguirte a ti mismo)
  //   - logged-in → botón Seguir/Siguiendo que llama a POST/DELETE y voltea.
  // Se ramifica por STATUS HTTP, nunca por strings del body.
  function _loginToFollowLink() {
    const wrap = elem("div", { className: "pub-follow" });
    wrap.appendChild(elem("a", {
      className: "btn-secondary pub-follow-login",
      text: "Inicia sesión para seguir",
      attrs: { href: "/" },
    }));
    return wrap;
  }

  // Construye el botón Seguir/Siguiendo y cablea sus handlers. `state.following`
  // es el estado actual; el botón lo voltea al éxito.
  function _followButton(username, token, following) {
    const wrap = elem("div", { className: "pub-follow" });
    const btn = elem("button", { className: "btn pub-follow-btn", attrs: { type: "button" } });

    function paint() {
      // Sin innerHTML: textContent y clases.
      btn.textContent = following ? "Siguiendo" : "Seguir";
      btn.classList.toggle("is-following", following);
      btn.setAttribute("aria-pressed", following ? "true" : "false");
    }
    paint();

    btn.addEventListener("click", async () => {
      if (btn.disabled) return;
      btn.disabled = true;
      const result = following
        ? await fetchAuthed("/api/follows/" + encodeURIComponent(username), "DELETE", token)
        : await fetchAuthed("/api/follows", "POST", token, { username });
      btn.disabled = false;

      if (result.networkError) return; // sin cambio de estado; el usuario reintenta
      if (result.status === 401) {
        // Token expirado entre lecturas → degradar al login-link.
        wrap.replaceWith(_loginToFollowLink());
        return;
      }
      if (result.status === 200 && result.data && result.data.ok) {
        following = typeof result.data.following === "boolean" ? result.data.following : !following;
        paint();
      }
      // 404/400/otros: no cambiamos el estado visible (no-op tolerante).
    });

    wrap.appendChild(btn);
    return wrap;
  }

  // Resuelve el estado del follow y devuelve el nodo apropiado. Sin token →
  // login-link. Con token: GET /api/follows/{username}; is_self → null (sin
  // botón); 401 → login-link.
  async function buildFollowControl(username) {
    const token = await readViewerToken();
    if (!token) return _loginToFollowLink();

    const result = await fetchAuthed("/api/follows/" + encodeURIComponent(username), "GET", token);
    if (result.networkError) return _loginToFollowLink();
    if (result.status === 401) return _loginToFollowLink();
    if (result.status !== 200 || !result.data || !result.data.ok) return _loginToFollowLink();
    if (result.data.is_self) return null; // no se muestra botón en tu propio perfil
    if (!result.data.followable) return null; // no seguible (defensa; perfil ya público)
    return _followButton(username, token, !!result.data.following);
  }

  // ── Follower/following counts + listas públicas ───────────────────────────
  // Los counts son totales reales (incluidos participantes privados). Las listas
  // nombran SOLO perfiles públicos (cada handle → enlace de texto a /u/{username}).
  // No se pone role de contenedor "list" falso: los hijos son <a>, no listitems
  // reales, así que se OMITE el role (una promesa ARIA errónea es violación axe
  // critical — lección FE del bundle). Todo con createElement + textContent.
  function _followHandleList(handles) {
    const wrap = elem("div", { className: "pub-follow-handles" });
    for (const h of handles) {
      const uname = h && h.username ? h.username : "";
      if (!uname) continue;
      const link = elem("a", {
        className: "pub-follow-handle",
        attrs: { href: "/u/" + encodeURIComponent(uname) },
      });
      link.appendChild(elem("span", { className: "pub-at", text: "@", attrs: { "aria-hidden": "true" } }));
      link.appendChild(document.createTextNode(uname));
      wrap.appendChild(link);
    }
    return wrap;
  }

  function socialSection(profile) {
    const section = elem("section", { className: "pub-social", attrs: { "aria-label": "Seguidores y seguidos" } });

    const followersCount = Number(profile.followers_count) || 0;
    const followingCount = Number(profile.following_count) || 0;
    const followers = Array.isArray(profile.followers) ? profile.followers : [];
    const following = Array.isArray(profile.following) ? profile.following : [];

    const counts = elem("div", { className: "pub-social-counts" });
    const fBlock = elem("div", { className: "pub-social-count" });
    fBlock.appendChild(elem("span", { className: "pub-social-count-num", text: String(followersCount) }));
    fBlock.appendChild(elem("span", {
      className: "pub-social-count-label",
      text: followersCount === 1 ? "seguidor" : "seguidores",
    }));
    counts.appendChild(fBlock);

    const gBlock = elem("div", { className: "pub-social-count" });
    gBlock.appendChild(elem("span", { className: "pub-social-count-num", text: String(followingCount) }));
    gBlock.appendChild(elem("span", { className: "pub-social-count-label", text: "siguiendo" }));
    counts.appendChild(gBlock);

    section.appendChild(counts);

    if (followers.length) {
      section.appendChild(elem("p", { className: "pub-social-heading", text: "Seguidores" }));
      section.appendChild(_followHandleList(followers));
    }
    if (following.length) {
      section.appendChild(elem("p", { className: "pub-social-heading", text: "Siguiendo" }));
      section.appendChild(_followHandleList(following));
    }
    return section;
  }

  // ── Reseñas públicas (session-aware like control) ─────────────────────────
  // Todo el DOM se construye con createElement + textContent (regla XSS de la
  // página pública): el texto de la reseña y cada handle son nodos de texto,
  // NUNCA innerHTML de datos de usuario (AC-9, directiva de verificación #2).
  // El control de like es session-aware (espeja el follow control):
  //   - sin token / 401 / expirado → enlace "Inicia sesión para reaccionar"
  //   - logged-in → corazón que hace POST/DELETE con Bearer y voltea.
  // El count mostrado en la proyección es el total real (incluidos privados).
  function _loginToReactLink() {
    const wrap = elem("div", { className: "pub-review-like" });
    wrap.appendChild(elem("a", {
      className: "btn-secondary pub-review-login",
      text: "Inicia sesión para reaccionar",
      attrs: { href: "/" },
    }));
    return wrap;
  }

  // Construye el disclosure "quién dio like" para una reseña. GET perezoso de los
  // likers (solo perfiles públicos) al expandir; cada handle → enlace a
  // /u/{username} como nodo de texto.
  function _reviewLikersDisclosure(movieId, token) {
    const wrap = elem("div", { className: "pub-review-likers-wrap" });
    const toggle = elem("button", {
      className: "pub-review-likers-toggle",
      text: "quién dio like",
      attrs: { type: "button", "aria-expanded": "false" },
    });
    const list = elem("div", { className: "pub-review-likers", attrs: { hidden: "hidden" } });
    let loaded = false;
    toggle.addEventListener("click", async () => {
      const expanded = toggle.getAttribute("aria-expanded") === "true";
      if (expanded) {
        toggle.setAttribute("aria-expanded", "false");
        list.hidden = true;
        return;
      }
      toggle.setAttribute("aria-expanded", "true");
      list.hidden = false;
      if (loaded) return;
      while (list.firstChild) list.removeChild(list.firstChild);
      list.appendChild(elem("span", { className: "pub-review-likers-loading", text: "Cargando…", attrs: { role: "status" } }));
      const result = token
        ? await fetchAuthed("/api/reviews/" + movieId + "/likes", "GET", token)
        : { status: 401 };
      while (list.firstChild) list.removeChild(list.firstChild);
      if (!result || result.status !== 200 || !result.data || !result.data.ok) {
        list.appendChild(elem("span", { className: "pub-review-likers-empty", text: "No se pudo cargar." }));
        return;
      }
      loaded = true;
      const likers = Array.isArray(result.data.likers) ? result.data.likers : [];
      if (!likers.length) {
        list.appendChild(elem("span", { className: "pub-review-likers-empty", text: "Sin perfiles públicos que mostrar." }));
        return;
      }
      for (const h of likers) {
        const uname = h && h.username ? h.username : "";
        if (!uname) continue;
        const link = elem("a", {
          className: "pub-review-liker",
          attrs: { href: "/u/" + encodeURIComponent(uname) },
        });
        link.appendChild(elem("span", { className: "pub-at", text: "@", attrs: { "aria-hidden": "true" } }));
        link.appendChild(document.createTextNode(uname));
        list.appendChild(link);
      }
    });
    wrap.appendChild(toggle);
    wrap.appendChild(list);
    return wrap;
  }

  // Botón corazón session-aware. `liked` es el estado inicial (de liked_by_me);
  // el botón lo voltea al éxito. Se ramifica por STATUS HTTP, nunca por strings.
  function _reviewLikeButton(movieId, token, liked, count) {
    const wrap = elem("div", { className: "pub-review-like" });
    const btn = elem("button", { className: "pub-review-like-btn", attrs: { type: "button", "aria-label": "Me gusta" } });
    const heart = elem("span", { className: "pub-review-like-heart", attrs: { "aria-hidden": "true" } });
    const countNode = elem("span", { className: "pub-review-like-count", text: String(Math.max(0, Number(count) || 0)) });

    function paint() {
      heart.textContent = liked ? "♥" : "♡";
      btn.classList.toggle("is-liked", liked);
      btn.setAttribute("aria-pressed", liked ? "true" : "false");
    }
    btn.appendChild(heart);
    btn.appendChild(countNode);
    paint();

    btn.addEventListener("click", async () => {
      if (btn.disabled) return;
      btn.disabled = true;
      const result = await fetchAuthed("/api/reviews/" + movieId + "/likes", liked ? "DELETE" : "POST", token);
      btn.disabled = false;
      if (result.networkError) return;
      if (result.status === 401) { wrap.replaceWith(_loginToReactLink()); return; }
      if (result.status === 200 && result.data && result.data.ok) {
        liked = typeof result.data.liked === "boolean" ? result.data.liked : !liked;
        if (typeof result.data.count === "number") countNode.textContent = String(Math.max(0, result.data.count));
        paint();
      }
      // 404/otros: no-op tolerante (la reseña dejó de estar disponible).
    });
    wrap.appendChild(btn);
    return wrap;
  }

  // Construye el bloque de like de una reseña de forma asíncrona: sin token →
  // login-link; con token → GET liked_by_me → corazón toggle. El count del GET
  // es el total real. Se llena en un slot para no bloquear el render.
  async function buildReviewLikeControl(slot, movieId, fallbackCount) {
    const token = await readViewerToken();
    if (!token) { slot.appendChild(_loginToReactLink()); slot.appendChild(_reviewLikersDisclosure(movieId, null)); return; }
    const result = await fetchAuthed("/api/reviews/" + movieId + "/likes", "GET", token);
    if (result.networkError || result.status === 401 ||
        result.status !== 200 || !result.data || !result.data.ok) {
      slot.appendChild(_loginToReactLink());
      slot.appendChild(_reviewLikersDisclosure(movieId, token));
      return;
    }
    const liked = !!result.data.liked_by_me;
    const count = typeof result.data.count === "number" ? result.data.count : fallbackCount;
    slot.appendChild(_reviewLikeButton(movieId, token, liked, count));
    slot.appendChild(_reviewLikersDisclosure(movieId, token));
  }

  // Sección de reseñas públicas del perfil. Cada reseña: título + poster + texto
  // (textContent) + like_count + control session-aware. El texto de la reseña es
  // UGC → SIEMPRE textContent, nunca innerHTML (directiva de verificación #2).
  function reviewsSection(reviews) {
    const section = elem("section", { className: "pub-reviews", attrs: { "aria-label": "Reseñas" } });
    section.appendChild(elem("h2", { className: "pub-section-title", text: "Reseñas" }));
    const wrap = elem("div", { className: "pub-review-cards", attrs: { role: "list" } });
    for (const rv of reviews) {
      const movieId = Number(rv.movie_id) || 0;
      if (!movieId) continue;
      const card = elem("article", { className: "pub-review-card", attrs: { role: "listitem" } });
      card.appendChild(posterNode(rv));

      const bodyCol = elem("div", { className: "pub-review-body" });
      bodyCol.appendChild(elem("div", { className: "pub-review-title", text: rv.title || "" }));

      const meta = elem("div", { className: "pub-review-meta" });
      if (rv.year) meta.appendChild(elem("span", { className: "pub-review-year", text: String(rv.year) }));
      meta.appendChild(elem("span", {
        className: "pub-media-badge",
        text: mediaIcon(rv.media_type),
        attrs: { "aria-hidden": "true" },
      }));
      bodyCol.appendChild(meta);

      // Texto de la reseña — nodo de texto (textContent), nunca innerHTML (AC-9).
      bodyCol.appendChild(elem("blockquote", { className: "pub-review-text", text: rv.note || "" }));

      const likeSlot = elem("div", { className: "pub-review-like-slot" });
      bodyCol.appendChild(likeSlot);
      card.appendChild(bodyCol);
      wrap.appendChild(card);

      // Relleno asíncrono del control de like (lee /api/config + token). El count
      // de la proyección (like_count) es el fallback hasta que el GET confirme.
      buildReviewLikeControl(likeSlot, movieId, Number(rv.like_count) || 0)
        .catch(() => { /* degrada: sin control de like */ });
    }
    section.appendChild(wrap);
    return section;
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

    // Zona del follow control: placeholder síncrono; se rellena de forma
    // asíncrona (lee /api/config + el token de sesión). No bloquea el render.
    const followSlot = elem("div", { className: "pub-follow-slot" });
    headerSec.appendChild(followSlot);

    // Counts + listas públicas (del cuerpo _public_profile extendido; sin auth).
    headerSec.appendChild(socialSection(profile));

    root.appendChild(headerSec);

    if (profile.username) {
      buildFollowControl(profile.username).then((node) => {
        if (node) followSlot.appendChild(node);
      }).catch(() => { /* degrada silenciosamente: sin control de follow */ });
    }

    const hasCollection = Array.isArray(profile.collection) && profile.collection.length > 0;
    const hasStats = profile.stats && typeof profile.stats === "object";
    const hasLists = Array.isArray(profile.lists) && profile.lists.length > 0;
    const hasReviews = Array.isArray(profile.reviews) && profile.reviews.length > 0;

    if (hasStats) root.appendChild(statsPanel(profile.stats));

    // Reseñas publicadas del perfil (independiente de show_collection, AC-6);
    // el backend solo las emite para un perfil público con notas publicadas.
    if (hasReviews) root.appendChild(reviewsSection(profile.reviews));

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

    if (!hasCollection && !hasStats && !hasLists && !hasReviews) {
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
