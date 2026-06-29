// Mi Cineteca — arranque, estado compartido, auth de Supabase y cableado de eventos.

// ── Supabase & Auth ─────────────────────────────────────────────────────────
let _supabase = null;
let _currentUser = null;
let _currentSession = null; // cacheada por onAuthStateChange; evita llamar a getSession() en cada api()
let _authMode = "login"; // "login" | "register"

function _showLoginScreen() {
  const s = document.getElementById("login-screen");
  if (s) s.hidden = false;
}

function _hideLoginScreen() {
  const s = document.getElementById("login-screen");
  if (s) s.hidden = true;
}

function _setLoginMode(mode) {
  _authMode = mode;
  const heading   = document.getElementById("login-heading");
  const submit    = document.getElementById("login-submit");
  const toggle    = document.getElementById("login-toggle");
  const errorEl   = document.getElementById("login-error");
  const successEl = document.getElementById("login-success");
  if (mode === "register") {
    heading.textContent = "Crear cuenta";
    submit.textContent  = "Registrarse";
    toggle.innerHTML    = '¿Ya tienes cuenta? <span>Inicia sesión</span>';
  } else {
    heading.textContent = "Iniciar sesión";
    submit.textContent  = "Entrar";
    toggle.innerHTML    = '¿No tienes cuenta? <span>Regístrate</span>';
  }
  errorEl.hidden   = true;
  successEl.hidden = true;
}
// ───────────────────────────────────────────────────────────────────────────

// ── Estado compartido y referencias DOM ─────────────────────────────────────
const resultsEl = el("results");
const resultsSection = el("results-section");

const modalEl = el("modal");
const pickPanelEl = el("pick-panel");

let movies = [];
let filter = "todas";
let mediaFilter = "todo";
let collectionMediaFilter = "todo";
let collectionQuery = "";
let collectionSort = "recent";
let lastResults = [];
let resultsMode = "search";
let activeGenreId = null;
let discoverPage = 1;
let discoverHasMore = false;
let discoverSort = "popular";
let pickedMovie = null;
let editingProgressId = null;
let editingNoteId = null;
let editingPlatformId = null;
let editingDateId = null;

// ── Profile chip (sidebar) ──────────────────────────────────────────────────
// Holds the last fetched profile so the click handler can branch without
// re-fetching. Reset to null on logout.
let _profileState = null;

// Pure, deterministic avatar helpers (AC-5). Same username → same output.
function _avatarInitials(username) {
  const u = (username || "").trim();
  if (!u) return "?";
  return u.slice(0, 2).toUpperCase();
}

// Stable 32-bit hash (FNV-1a-ish) → two HSL hues for a deterministic gradient.
function _avatarGradient(username) {
  const u = username || "";
  let hash = 2166136261;
  for (let i = 0; i < u.length; i++) {
    hash ^= u.charCodeAt(i);
    hash = (hash * 16777619) >>> 0;
  }
  if (!u) {
    // No-username placeholder: fixed neutral gradient (no username to derive from).
    return "linear-gradient(135deg, #3a3f4b, #21242c)";
  }
  const h1 = hash % 360;
  const h2 = (h1 + 40) % 360;
  return `linear-gradient(135deg, hsl(${h1} 55% 42%), hsl(${h2} 55% 28%))`;
}

// Render/reveal the chip from the current _profileState. Username text is
// rendered text-only (textContent) — never innerHTML of user data (US-*/SE-*).
function _renderProfileChip() {
  const chip = document.getElementById("profile-chip");
  if (!chip) return;
  const username = _profileState && _profileState.username;

  chip.textContent = "";

  const avatar = document.createElement("span");
  avatar.className = "profile-chip-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = username ? _avatarInitials(username) : "?";
  // Gradient via CSSOM only — strict CSP forbids inline style= (PS-006).
  avatar.style.backgroundImage = _avatarGradient(username);

  const label = document.createElement("span");
  label.className = "profile-chip-label";
  label.textContent = username ? username : "Elige tu nombre de usuario";

  chip.appendChild(avatar);
  chip.appendChild(label);

  // Accessible name on every render (AC-7).
  chip.setAttribute(
    "aria-label",
    username ? "Tu perfil, " + username : "Configura tu perfil",
  );
  chip.hidden = false;
}

function _hideProfileChip() {
  const chip = document.getElementById("profile-chip");
  if (chip) chip.hidden = true;
  _profileState = null;
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
  if (viewId === "discover-view" && !el("discover-search-input").value.trim()) {
    if (activeGenreId !== null) loadDiscover(activeGenreId);
    else loadTrending();
  }
  if (viewId === "stats-view") renderStatsView();
  if (viewId === "sharing-view") showSharingView();
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
  if (!q) {
    if (activeGenreId !== null) loadDiscover(activeGenreId);
    else loadTrending();
    return;
  }
  activeGenreId   = null;
  discoverHasMore = false;
  discoverPage    = 1;
  renderGenreChips();
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
    showMessage("Búsqueda online desactivada: requiere configurar una clave de TMDB (ver README).", "error");
  } else {
    showMessage(data.error || "Error en la búsqueda.", "error");
  }
});

el("results-close").addEventListener("click", () => {
  el("discover-search-input").value = "";
  activeGenreId = null;
  renderGenreChips();
  loadTrending();
});

el("discover-search-input").addEventListener("input", (e) => {
  if (!e.target.value.trim()) {
    if (activeGenreId !== null) loadDiscover(activeGenreId);
    else loadTrending();
  }
});

document.querySelectorAll("[data-view-target]").forEach((btn) => {
  btn.addEventListener("click", () => showView(btn.dataset.viewTarget));
});

resultsEl.addEventListener("click", (e) => {
  const poster = e.target.closest(".poster[data-tmdb]");
  if (poster) {
    const tmdb = +poster.dataset.tmdb;
    const type = poster.dataset.type;
    const hint = lastResults.find(r => r.tmdb_id === tmdb && r.media_type === type) || {};
    openDetail(tmdb, type, hint);
    return;
  }
  const btn = e.target.closest("[data-action='add']");
  if (!btn) return;
  const idx = +btn.closest(".card").dataset.idx;
  addItem(lastResults[idx], btn.dataset.status);
});

collectionEl.addEventListener("change", (e) => {
  const sel = e.target.closest("[data-action='status-change']");
  if (!sel) return;
  const id = +e.target.closest(".card")?.dataset.id;
  const status = sel.value;
  if (!id || !status) return;
  const movie = movies.find((m) => m.id === id);
  const payload = { status };
  if (status === "vista" && movie && !movie.watched_at) payload.watched_at = todayIsoDate();
  patchMovie(id, payload);
});

el("status-filter").addEventListener("change", (e) => {
  filter = e.target.value;
  renderCollection();
});

el("discover-type-select").addEventListener("change", (e) => {
  mediaFilter = e.target.value;
  if (activeGenreId !== null) loadDiscover(activeGenreId);
  else renderResults();
});

el("discover-sort-select").addEventListener("change", (e) => {
  discoverSort = e.target.value;
  if (activeGenreId !== null) loadDiscover(activeGenreId);
});

el("collection-media-filter").addEventListener("change", (e) => {
  collectionMediaFilter = e.target.value;
  renderCollection();
});

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


modalEl.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeModal(); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (listPickerEl && !listPickerEl.hidden) closeAddToListPicker();
  else if (!modalEl.hidden) closeModal();
  else if (!pickPanelEl.hidden) closePickPanel();
  else if (editingPlatformId !== null) { editingPlatformId = null; renderCollection(); }
  else if (editingProgressId !== null) { editingProgressId = null; renderCollection(); }
  else if (editingDateId !== null) { editingDateId = null; renderCollection(); }
  else if (editingNoteId !== null) { editingNoteId = null; renderCollection(); }
});

// Profile chip — navigate by current profile state (AC-2/AC-3/AC-4).
// #profile-chip is a static element in index.html and showView is declared
// above in this file → PS-003-safe at load.
const profileChipEl = document.getElementById("profile-chip");
if (profileChipEl) {
  profileChipEl.addEventListener("click", () => {
    const username = _profileState && _profileState.username;
    const isPublic = _profileState && _profileState.is_public === true;
    if (username && isPublic) {
      location.assign("/u/" + encodeURIComponent(username));
    } else {
      // username-but-private OR no-username → settings (never build a /u/ URL).
      showView("sharing-view");
    }
  });
}

document.body.dataset.activeView = "collection-view";
renderGenreChips();

// Chips de género — delegado en la vista Descubrir (siempre en el DOM)
el("discover-view").addEventListener("click", (e) => {
  const chip = e.target.closest(".genre-chip");
  if (!chip) return;
  const genreId = +chip.dataset.genreId;
  el("discover-search-input").value = "";
  if (activeGenreId === genreId) {
    activeGenreId = null;
    renderGenreChips();
    loadTrending();
  } else {
    activeGenreId = genreId;
    renderGenreChips();
    loadDiscover(genreId);
  }
});

// Botón "Ver más" — delegado en resultsSection (ya es const, siempre en el DOM)
resultsSection.addEventListener("click", (e) => {
  if (!e.target.closest("#load-more-btn")) return;
  if (activeGenreId !== null) loadDiscover(activeGenreId, discoverPage + 1, true);
});

// ── Bootstrap ──────────────────────────────────────────────────────────────

async function initApp() {
  // 1. Cargar config de Supabase desde el servidor
  try {
    const cfg = await fetch("/api/config").then(r => r.json()).catch(() => ({}));
    if (cfg.supabase_url && cfg.supabase_anon_key && window.supabase) {
      _supabase = window.supabase.createClient(cfg.supabase_url, cfg.supabase_anon_key);
    }
  } catch (e) {
    console.warn("No se pudo cargar la config de Supabase:", e);
  }

  // 2. Listener de cambios de sesión.
  //    No llamamos a getSession() aquí dentro (footgun de supabase-js: el lock
  //    interno puede bloquearse). La sesión llega como argumento y la cacheamos;
  //    loadMovies() se difiere con queueMicrotask para no correr dentro del lock.
  if (_supabase) {
    _supabase.auth.onAuthStateChange((event, session) => {
      _currentSession = session;
      if (session) {
        _currentUser = session.user;
        _hideLoginScreen();
        _updateSidebarUser(session.user.email);
        // En la carga inicial NO recargamos aquí: el bloque «sesión inicial» de
        // más abajo ya llama a loadMovies(). El listener solo carga en logins
        // posteriores (SIGNED_IN, TOKEN_REFRESHED) → evita un doble fetch al abrir.
        if (event !== "INITIAL_SESSION") queueMicrotask(() => { loadMovies(); });
      } else {
        _currentUser = null;
        movies = [];
        renderCollection();
        const ws = document.getElementById("welcome-screen");
        if (!ws) _showLoginScreen();
      }
    });
  }

  // 3. Comprobar sesión inicial
  const session = _supabase
    ? (await _supabase.auth.getSession()).data.session
    : null;
  _currentSession = session;

  const visited = localStorage.getItem("cinebox_visited");
  const welcomeScreen = document.getElementById("welcome-screen");

  // 4. Enganchar botones de bienvenida (después de saber si hay sesión)
  if (welcomeScreen) {
    function _leaveWelcome(mode) {
      localStorage.setItem("cinebox_visited", "1");
      if (mode) _setLoginMode(mode);
      welcomeScreen.classList.add("is-hiding");
      welcomeScreen.addEventListener("transitionend", () => {
        welcomeScreen.remove();
        if (_currentUser) {
          showView("discover-view");
        } else {
          _showLoginScreen();
        }
      }, { once: true });
    }
    document.getElementById("welcome-register").addEventListener("click", () => _leaveWelcome("register"));
    document.getElementById("welcome-login").addEventListener("click",    () => _leaveWelcome("login"));
  }

  // 5. Routing inicial
  if (session) {
    _currentUser = session.user;
    if (welcomeScreen) welcomeScreen.remove();
    _hideLoginScreen();
    _updateSidebarUser(session.user.email);
    await loadMovies();
  } else if (visited) {
    // Ya visitó antes pero no tiene sesión → login
    if (welcomeScreen) welcomeScreen.remove();
    _showLoginScreen();
  }
  // else: primera visita sin sesión → se muestra la welcome screen normalmente

  // 6. Login form
  document.getElementById("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!_supabase) return;

    const email    = document.getElementById("login-email").value.trim();
    const password = document.getElementById("login-password").value;
    const errorEl   = document.getElementById("login-error");
    const successEl = document.getElementById("login-success");
    const submitBtn = document.getElementById("login-submit");

    errorEl.hidden   = true;
    successEl.hidden = true;
    submitBtn.disabled = true;
    submitBtn.textContent = _authMode === "register" ? "Registrando…" : "Entrando…";

    try {
      if (_authMode === "register") {
        const { error } = await _supabase.auth.signUp({ email, password });
        if (error) throw error;
        successEl.textContent = "¡Cuenta creada! Revisa tu email para confirmarla.";
        successEl.hidden = false;
      } else {
        const { error } = await _supabase.auth.signInWithPassword({ email, password });
        if (error) throw error;
        // onAuthStateChange se encarga del resto
      }
    } catch (err) {
      errorEl.textContent = err.message || "Error al autenticar. Inténtalo de nuevo.";
      errorEl.hidden = false;
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = _authMode === "register" ? "Registrarse" : "Entrar";
    }
  });

  // 7. Alternar login/registro
  document.getElementById("login-toggle").addEventListener("click", () => {
    _setLoginMode(_authMode === "login" ? "register" : "login");
  });

  // 8. Cerrar sesión
  document.getElementById("logout-btn").addEventListener("click", async () => {
    if (!_supabase) return;
    await _supabase.auth.signOut();
    _updateSidebarUser(null);
  });
}

function _updateSidebarUser(email) {
  const emailEl  = document.getElementById("sidebar-user-email");
  const logoutBtn = document.getElementById("logout-btn");
  if (email) {
    emailEl.textContent = email;
    emailEl.hidden  = false;
    logoutBtn.hidden = false;
    _loadProfileChip();
  } else {
    emailEl.hidden   = true;
    logoutBtn.hidden = true;
    _hideProfileChip();
  }
}

// Fetch the profile once on authentication and render the chip. Degrades
// gracefully: a failed/{ok:false} response leaves the chip hidden and never
// throws; email/logout are unaffected.
async function _loadProfileChip() {
  try {
    const { data } = await api("/api/profile");
    if (!data || !data.ok || !data.profile) {
      _hideProfileChip();
      return;
    }
    _profileState = data.profile;
    _renderProfileChip();
  } catch (e) {
    _hideProfileChip();
  }
}

initApp();
