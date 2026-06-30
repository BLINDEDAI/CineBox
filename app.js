// Mi Cineteca — arranque, estado compartido, auth de Supabase y cableado de eventos.

// ── Supabase & Auth ─────────────────────────────────────────────────────────
let _supabase = null;
let _currentUser = null;
let _currentSession = null; // cacheada por onAuthStateChange; evita llamar a getSession() en cada api()
let _authMode = "login"; // "login" | "register"

// ── Username validation (mirror of server-side _normalize_username) ──────────
// Client-side mirror of server.py's _USERNAME_RE + RESERVED_USERNAMES. Advisory
// only — the authoritative validation + uniqueness is the PATCH /api/profile
// claim (a 409 there is the real "taken" signal; see api() status field).
const _USERNAME_RE = /^[a-z0-9_-]{3,30}$/;
const _RESERVED_USERNAMES = new Set([
  "api", "u", "l", "admin", "assets", "health", "config", "vendor", "public",
]);

// Returns an es-ES error string when the candidate is locally invalid, or null
// when it is well-formed (still subject to the authoritative availability check).
function _usernameFormatError(raw) {
  const name = (raw || "").trim().toLowerCase();
  if (!name) return "Elige un nombre de usuario.";
  if (!_USERNAME_RE.test(name)) {
    return "Usa entre 3 y 30 caracteres: minúsculas, números, guion y guion bajo.";
  }
  if (_RESERVED_USERNAMES.has(name)) return "Ese nombre está reservado, elige otro.";
  return null;
}

// Advisory availability check against the anonymous endpoint. Returns one of
// "ok" | "taken" | "invalid" | "unknown" — "unknown" on any network/HTTP error
// so a failed check degrades to advisory-unknown and never hard-blocks (the
// authoritative claim still protects integrity).
async function _checkUsernameAvailable(name) {
  try {
    const { ok, data } = await api(
      "/api/public/username-available?u=" + encodeURIComponent(name),
    );
    if (!ok || !data || data.ok !== true) return "unknown";
    if (data.available === true) return "ok";
    return data.reason === "invalid" ? "invalid" : "taken";
  } catch (e) {
    return "unknown";
  }
}

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
  const usernameField = document.getElementById("login-username-field");
  const usernameHint  = document.getElementById("login-username-hint");
  if (mode === "register") {
    heading.textContent = "Crear cuenta";
    submit.textContent  = "Registrarse";
    toggle.innerHTML    = '¿Ya tienes cuenta? <span>Inicia sesión</span>';
    if (usernameField) usernameField.hidden = false;
  } else {
    heading.textContent = "Iniciar sesión";
    submit.textContent  = "Entrar";
    toggle.innerHTML    = '¿No tienes cuenta? <span>Regístrate</span>';
    if (usernameField) usernameField.hidden = true;
  }
  if (usernameHint) usernameHint.textContent = "";
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

// ── One-time blocking username gate ──────────────────────────────────────────
// Shown when an authenticated user has no username (legacy account, raced new
// user, or absent/failed metadata claim). Non-dismissable until a valid, available
// username is accepted via the authoritative PATCH /api/profile claim.
let _usernameGateLastFocus = null;

function _showUsernameGate() {
  const gate = document.getElementById("username-gate");
  if (!gate) return;
  _usernameGateLastFocus = document.activeElement;
  const hint = document.getElementById("username-gate-hint");
  if (hint) hint.textContent = "";
  gate.hidden = false;
  gate.classList.add("is-open");
  // Focus the first control on open (mirrors the #list-picker dialog pattern).
  const input = document.getElementById("username-gate-input");
  if (input) input.focus();
}

function _hideUsernameGate() {
  const gate = document.getElementById("username-gate");
  if (!gate || gate.hidden) return;
  gate.classList.remove("is-open");
  gate.hidden = true;
  if (_usernameGateLastFocus && typeof _usernameGateLastFocus.focus === "function") {
    _usernameGateLastFocus.focus();
  }
  _usernameGateLastFocus = null;
}

// Validate + check availability + claim authoritatively. On 200 reveal the app;
// on 409 keep the gate open with "ya está en uso"; on 400/other keep it open.
async function _submitUsernameGate() {
  const input = document.getElementById("username-gate-input");
  const hint  = document.getElementById("username-gate-hint");
  const submitBtn = document.getElementById("username-gate-submit");
  const name = (input ? input.value : "").trim().toLowerCase();

  const fmtError = _usernameFormatError(name);
  if (fmtError) {
    if (hint) hint.textContent = fmtError;
    if (input) input.focus();
    return;
  }
  if (hint) hint.textContent = "Comprobando disponibilidad…";
  const availability = await _checkUsernameAvailable(name);
  if (availability === "taken") {
    if (hint) hint.textContent = "Ese nombre ya está en uso, elige otro.";
    if (input) input.focus();
    return;
  }
  if (availability === "invalid") {
    if (hint) hint.textContent = "Ese nombre no es válido, elige otro.";
    if (input) input.focus();
    return;
  }

  if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = "Guardando…"; }
  try {
    const { status } = await api("/api/profile", {
      method: "PATCH",
      body: JSON.stringify({ username: name }),
    });
    if (status === 200) {
      // Re-fetch the canonical profile, hide the gate, reveal the app + chip.
      const { data } = await api("/api/profile");
      if (data && data.ok && data.profile) _profileState = data.profile;
      if (hint) hint.textContent = "";
      _hideUsernameGate();
      _renderProfileChip();
      return;
    }
    if (status === 409) {
      if (hint) hint.textContent = "Ese nombre ya está en uso, elige otro.";
      if (input) input.focus();
      return;
    }
    if (hint) hint.textContent = "Ese nombre no es válido, elige otro.";
    if (input) input.focus();
  } catch (e) {
    if (hint) hint.textContent = "No se pudo guardar, inténtalo de nuevo.";
  } finally {
    if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = "Guardar y continuar"; }
  }
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
  if (viewId === "lists-view") showListsView();
  if (viewId === "settings-view") showSettingsView();
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
      showView("settings-view");
    }
  });
}

// One-time username gate wiring (PS-003-safe: #username-gate is static in
// index.html; el/esc/api/showView/showMessage are all earlier-loaded globals).
const usernameGateEl = el("username-gate");
if (usernameGateEl) {
  const gateForm = el("username-gate-form");
  if (gateForm) {
    gateForm.addEventListener("submit", (e) => {
      e.preventDefault();
      _submitUsernameGate();
    });
  }
  // Real focus-trap: keep Tab inside the gate and swallow Escape (non-dismissable).
  usernameGateEl.addEventListener("keydown", (e) => {
    if (usernameGateEl.hidden) return;
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); return; }
    if (e.key !== "Tab") return;
    const focusable = usernameGateEl.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
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
    const usernameInput = document.getElementById("login-username");
    const usernameHint  = document.getElementById("login-username-hint");

    errorEl.hidden   = true;
    successEl.hidden = true;

    // Register mode: a valid + available username is required before signUp (AC-1/AC-2/AC-3).
    let desiredUsername = null;
    if (_authMode === "register") {
      desiredUsername = (usernameInput ? usernameInput.value : "").trim().toLowerCase();
      const fmtError = _usernameFormatError(desiredUsername);
      if (fmtError) {
        if (usernameHint) usernameHint.textContent = fmtError;
        if (usernameInput) usernameInput.focus();
        return; // submit blocked client-side; signUp not called
      }
      if (usernameHint) usernameHint.textContent = "Comprobando disponibilidad…";
      const availability = await _checkUsernameAvailable(desiredUsername);
      if (availability === "taken") {
        if (usernameHint) usernameHint.textContent = "Ese nombre ya está en uso, elige otro.";
        if (usernameInput) usernameInput.focus();
        return;
      }
      if (availability === "invalid") {
        if (usernameHint) usernameHint.textContent = "Ese nombre no es válido, elige otro.";
        if (usernameInput) usernameInput.focus();
        return;
      }
      // "ok" or "unknown" (advisory) → proceed; the claim is authoritative.
      if (usernameHint) usernameHint.textContent = "";
    }

    submitBtn.disabled = true;
    submitBtn.textContent = _authMode === "register" ? "Registrando…" : "Entrando…";

    try {
      if (_authMode === "register") {
        const { error } = await _supabase.auth.signUp({
          email,
          password,
          options: { data: { desired_username: desiredUsername } },
        });
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

  // 8. Cerrar sesión (footer). Comparte el camino con el botón de Ajustes → Cuenta.
  document.getElementById("logout-btn").addEventListener("click", () => { signOut(); });
}

// Cierre de sesión único. Lo invocan tanto el botón del footer (#logout-btn)
// como el de Ajustes → Cuenta (#settings-logout-btn, en settings.js). Cuerpo de
// función → tiempo de llamada (PS-003), así que settings.js (cargado antes) lo
// resuelve sin problema. `_updateSidebarUser(null)` dispara `resetSettingsState()`.
async function signOut() {
  if (!_supabase) return;
  await _supabase.auth.signOut();
  _updateSidebarUser(null);
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
    // Logout: limpia el estado cacheado + el DOM de Ajustes/Mis listas para que
    // una cuenta posterior nunca vea datos de la anterior (AC-8 / AC-9).
    // resetSettingsState vive en settings.js (cargado antes); cuerpo de función
    // → tiempo de llamada, PS-003-safe.
    resetSettingsState();
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
    // No username yet (legacy account or raced new user): try a silent auto-claim
    // from the Supabase user_metadata carrier, else fall back to the blocking gate.
    if (!_profileState.username) {
      await _claimOrGateUsername();
      return;
    }
    _hideUsernameGate();
    _renderProfileChip();
  } catch (e) {
    _hideProfileChip();
  }
}

// First-login auto-claim + one-time gate trigger. The desired_username in
// user_metadata is untrusted: the server re-validates it at the PATCH claim.
async function _claimOrGateUsername() {
  const desired =
    _currentUser &&
    _currentUser.user_metadata &&
    _currentUser.user_metadata.desired_username;
  if (desired) {
    try {
      const { status } = await api("/api/profile", {
        method: "PATCH",
        body: JSON.stringify({ username: desired }),
      });
      if (status === 200) {
        // Claimed authoritatively — re-fetch the canonical profile and proceed.
        const { data } = await api("/api/profile");
        if (data && data.ok && data.profile && data.profile.username) {
          _profileState = data.profile;
          _hideUsernameGate();
          _renderProfileChip();
          return;
        }
      }
      // 409 (taken) / 400 (invalid/reserved) / anything else → fall through to gate.
    } catch (e) {
      // network error → gate
    }
  }
  _showUsernameGate();
}

initApp();
