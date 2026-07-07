// Mi Cineteca — arranque, estado compartido, auth de Supabase y cableado de eventos.

// ── Supabase & Auth ─────────────────────────────────────────────────────────
let _supabase = null;
let _currentUser = null;
let _currentSession = null; // cacheada por onAuthStateChange; evita llamar a getSession() en cada api()
let _authMode = "login"; // "login" | "register"
// Marca que la sesión en mano es una sesión de RECUPERACIÓN (PASSWORD_RECOVERY):
// suprime el aterrizaje autenticado normal para mostrar el formulario de nueva
// contraseña. Literal — no referencia ningún global, seguro en tiempo de carga (PS-003).
let _passwordRecovery = false;

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

// Pantalla de nueva contraseña (deep-link de recuperación). Sibling de #login-screen
// (como #username-gate). Al revelarla se quita `cinephora-authed` de <html> igual que
// _showLanding, para que la puerta pre-paint no deje asomando el shell del app.
function _showPasswordRecovery() {
  const s = document.getElementById("password-recovery-screen");
  document.documentElement.classList.remove("cinephora-authed");
  if (s) s.hidden = false;
}

function _hidePasswordRecovery() {
  const s = document.getElementById("password-recovery-screen");
  if (s) s.hidden = true;
}

// La landing (#welcome-screen) es ahora una landing de marketing que se muestra
// SIEMPRE que no hay sesión (no solo la primera visita). No se elimina del DOM: se
// oculta/muestra para que reaparezca tras un logout. `cinephora-authed` (clase en
// <html>) es el gate pre-paint que pone boot.js; al mostrar la landing hay que
// quitarlo para cubrir el caso borde de token caducado.
function _showLanding() {
  const w = document.getElementById("welcome-screen");
  document.documentElement.classList.remove("cinephora-authed");
  if (w) w.hidden = false;
}

function _hideLanding() {
  const w = document.getElementById("welcome-screen");
  if (w) w.hidden = true;
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
  const forgotLink = document.getElementById("login-forgot-link");
  const resetForm  = document.getElementById("password-reset-form");
  const loginForm  = document.getElementById("login-form");
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
  // Affordance "¿Olvidaste tu contraseña?": solo en modo login. Cambiar de modo
  // también colapsa cualquier reveal abierto del formulario de reset, devolviendo
  // el formulario de inicio de sesión (esto es lo que "revierte" _setLoginMode).
  if (resetForm) resetForm.hidden = true;
  if (loginForm) loginForm.hidden = false;
  if (toggle) toggle.hidden = false;
  if (forgotLink) forgotLink.hidden = (mode === "register");
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
// Preferencia por defecto del orden de colección (AC-4/AC-5). getPref vive en
// ui.js (2º módulo, cargado antes) → seguro en tiempo de carga (PS-003). Valor
// ausente/inválido → "recent" (el default actual). El control en vivo cambia
// esta variable pero NO persiste (explicit-only, AC-9).
let collectionSort = getPref("collection_sort", COLLECTION_SORTS, "recent");
let lastResults = [];
let resultsMode = "search";
let activeGenreId = null;
let discoverPage = 1;
let discoverHasMore = false;
let discoverSort = "popular";
let pickedMovie = null;

// ── Profile chip (sidebar) ──────────────────────────────────────────────────
// Holds the last fetched profile so the click handler can branch without
// re-fetching. Reset to null on logout.
let _profileState = null;

// Guarded-<img> allowlist for uploaded avatars (custom-avatar-upload). The URL
// is set as an <img src> ONLY after it matches a Supabase-Storage-avatars origin
// (same guarded-<img> pattern as image.tmdb.org). A non-matching URL is ignored
// and the generated fallback is used — the URL is NEVER injected into innerHTML.
const _AVATAR_URL_RE = /^https:\/\/[a-z0-9-]+\.supabase\.co\/storage\/v1\/object\/public\/avatars\//;

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
  const avatarUrl = _profileState && _profileState.avatar_url;

  chip.textContent = "";

  const avatar = document.createElement("span");
  avatar.className = "profile-chip-avatar";
  avatar.setAttribute("aria-hidden", "true");
  // Uploaded avatar: render a guarded <img> only when the URL matches the
  // Storage-avatars allowlist; otherwise fall back to generated initials.
  if (avatarUrl && _AVATAR_URL_RE.test(avatarUrl)) {
    const img = document.createElement("img");
    img.className = "profile-chip-avatar-img";
    img.setAttribute("alt", "");
    img.setAttribute("loading", "lazy");
    img.setAttribute("src", avatarUrl); // src via setAttribute after allowlist match
    avatar.appendChild(img);
  } else {
    avatar.textContent = username ? _avatarInitials(username) : "?";
    // Gradient via CSSOM only — strict CSP forbids inline style= (PS-006).
    avatar.style.backgroundImage = _avatarGradient(username);
  }

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
  if (viewId === "activity-view") showActivityView();
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

// Refleja el orden por defecto aplicado (desde la preferencia) en el control en
// vivo al arrancar, para que el select coincida con el orden que se está usando.
// #collection-sort es estático en index.html y `collectionSort` se declaró antes
// en este archivo → PS-003-safe en tiempo de carga.
el("collection-sort").value = collectionSort;

el("collection-sort").addEventListener("change", (e) => {
  // Cambio en vivo: actualiza el orden de sesión y re-renderiza, pero NO llama a
  // setPref — la preferencia guardada solo cambia desde Ajustes (explicit-only, AC-9).
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
    // Plataforma por defecto (AC-6/AC-7): mismo criterio que el seam del select
    // de estado — solo si la película no tiene plataforma y hay preferencia válida.
    if (!pickedMovie.platform) {
      const defaultPlatform = getPref("default_platform", PLATFORMS, null);
      if (defaultPlatform) payload.platform = defaultPlatform;
    }
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
      // PASSWORD_RECOVERY (deep-link de recuperación) — PRIMERA rama, ADITIVA. Muestra
      // el formulario de nueva contraseña y RETORNA antes del aterrizaje normal
      // SIGNED_IN / INITIAL_SESSION (no loadMovies, no revelar el app) (AC-5/AC-11).
      if (event === "PASSWORD_RECOVERY") {
        _passwordRecovery = true;
        _currentSession = session;
        _hideLanding();
        _hideLoginScreen();
        _showPasswordRecovery();
        return;
      }
      _currentSession = session;
      if (session) {
        _currentUser = session.user;
        _hideLoginScreen();
        _hideLanding();
        _updateSidebarUser(session.user.email);
        // En la carga inicial NO recargamos aquí: el bloque «sesión inicial» de
        // más abajo ya llama a loadMovies(). El listener solo carga en logins
        // posteriores (SIGNED_IN, TOKEN_REFRESHED) → evita un doble fetch al abrir.
        if (event !== "INITIAL_SESSION") queueMicrotask(() => { loadMovies(); });
      } else {
        // Logout: limpia estado y vuelve a la landing (se muestra siempre que no
        // hay sesión). Si la landing no está en el DOM (p.ej. eliminada en un test o
        // tras borrado de cuenta), cae al login como antes (mismo fallback que la
        // lógica original `if (!ws)`).
        _currentUser = null;
        movies = [];
        renderCollection();
        const w = document.getElementById("welcome-screen");
        if (w) { _hideLoginScreen(); _showLanding(); }
        else { _showLoginScreen(); }
      }
    });
  }

  // 3. Comprobar sesión inicial
  const session = _supabase
    ? (await _supabase.auth.getSession()).data.session
    : null;
  _currentSession = session;

  const welcomeScreen = document.getElementById("welcome-screen");

  // 4. Enganchar los CTAs de la landing (después de saber si hay sesión). Desde la
  // landing se pasa a la pantalla de login/registro; la landing se oculta pero NO se
  // elimina (debe reaparecer tras un logout). El wiring es por delegación para que
  // todos los CTAs (hero, topbar, banda, footer) compartan un único handler robusto.
  function _leaveWelcome(mode) {
    if (mode) _setLoginMode(mode);
    _hideLanding();
    _showLoginScreen();
  }
  if (welcomeScreen) {
    const reg = document.getElementById("welcome-register");
    if (reg) reg.addEventListener("click", () => _leaveWelcome("register"));
    const log = document.getElementById("welcome-login");
    if (log) log.addEventListener("click", () => _leaveWelcome("login"));
    // CTAs adicionales (topbar/banda/footer) marcados con data-landing-auth.
    welcomeScreen.addEventListener("click", (e) => {
      const t = e.target.closest("[data-landing-auth]");
      if (!t) return;
      const mode = t.getAttribute("data-landing-auth") === "register" ? "register" : "login";
      _leaveWelcome(mode);
    });
  }

  // 5. Routing inicial: sesión → app; sin sesión → landing (siempre). Si getSession()
  // resolvió una sesión de RECUPERACIÓN (token del fragmento de URL), _passwordRecovery
  // está puesto y NO se revela el app ni la landing por debajo de la pantalla de
  // recuperación (el listener PASSWORD_RECOVERY ya la mostró) (AC-5).
  if (session && !_passwordRecovery) {
    _currentUser = session.user;
    _hideLanding();
    _hideLoginScreen();
    _updateSidebarUser(session.user.email);
    await loadMovies();
    // Vista de inicio por defecto (AC-2/AC-3). Un valor ausente/inválido →
    // "collection-view" (el default actual). getPref valida contra HOME_VIEWS,
    // así que nunca se pasa una cadena arbitraria a showView (SE-*). La puerta de
    // nombre de usuario (ADR-007) tiene precedencia y se muestra por encima si
    // aplica; esto solo enruta el aterrizaje autenticado normal.
    showView(getPref("home_view", HOME_VIEWS, "collection-view"));
  } else if (!_passwordRecovery) {
    // Sin sesión → landing de marketing. Caso borde: si boot.js ocultó la landing
    // pre-paint por un token CADUCADO (heurística por-presencia-de-clave, no por
    // validez), _showLanding reconcilia el estado quitando cinephora-authed y mostrando
    // la landing. Queda un breve destello del app-shell (que no se oculta) entre el
    // paint y la resolución de getSession(); es una limitación preexistente y solo
    // afecta al raro caso de sesión caducada, no al login/logout normal.
    _showLanding();
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

  // 7b. Reveal + envío de la solicitud de reset (superficie de login). Todos los
  // getElementById van guardados con `if (el)` (error conocido: el elemento puede no
  // existir). El reveal oculta el formulario de login y muestra #password-reset-form;
  // #reset-back (o cambiar de modo vía _setLoginMode) lo revierte.
  const forgotLink = document.getElementById("login-forgot-link");
  const resetForm  = document.getElementById("password-reset-form");
  const resetBack  = document.getElementById("reset-back");
  const resetEmail = document.getElementById("reset-email");
  const resetHint  = document.getElementById("reset-hint");
  const loginFormEl   = document.getElementById("login-form");
  const loginToggleEl = document.getElementById("login-toggle");

  function _showResetRequestForm() {
    if (loginFormEl) loginFormEl.hidden = true;
    if (loginToggleEl) loginToggleEl.hidden = true;
    if (forgotLink) forgotLink.hidden = true;
    if (resetForm) resetForm.hidden = false;
    if (resetHint) { resetHint.textContent = ""; resetHint.classList.remove("login-error"); }
    if (resetEmail) resetEmail.focus();
  }

  if (forgotLink) forgotLink.addEventListener("click", _showResetRequestForm);
  if (resetBack) {
    resetBack.addEventListener("click", () => {
      if (resetEmail) resetEmail.value = "";
      if (resetHint) { resetHint.textContent = ""; resetHint.classList.remove("login-error"); }
      _setLoginMode("login"); // restaura #login-form / #login-toggle / #login-forgot-link
    });
  }
  if (resetForm) {
    resetForm.addEventListener("submit", (e) => { e.preventDefault(); _requestPasswordReset(); });
  }

  // 7c. Formulario de nueva contraseña (deep-link de recuperación) + "pedir un nuevo enlace".
  const recoveryForm = document.getElementById("password-recovery-form");
  if (recoveryForm) {
    recoveryForm.addEventListener("submit", (e) => { e.preventDefault(); _submitNewPassword(); });
  }
  const recoveryAgain = document.getElementById("recovery-request-again");
  if (recoveryAgain) {
    recoveryAgain.addEventListener("click", () => {
      _passwordRecovery = false;
      _hidePasswordRecovery();
      _setLoginMode("login");
      _showLoginScreen();
      _showResetRequestForm();
    });
  }

  // 8. Cerrar sesión (footer). Comparte el camino con el botón de Ajustes → Cuenta.
  document.getElementById("logout-btn").addEventListener("click", () => { signOut(); });
}

// ── Reset de contraseña (superficie de login, pre-auth) ──────────────────────
// Anti-enumeración: en TODO camino (existe / no existe / error SDK) se muestra el
// MISMO mensaje genérico byte-idéntico; el resultado nunca se inspecciona para el
// mensaje. Un email vacío/malformado se bloquea antes con validación de formato
// (advisory) sin ninguna llamada a Supabase. El email es secreto: nunca se loguea,
// ni va a la URL, ni al backend. Cuerpo de función → tiempo de llamada (PS-003).
async function _requestPasswordReset() {
  const emailEl  = document.getElementById("reset-email");
  const hintEl   = document.getElementById("reset-hint");
  const submitEl = document.getElementById("reset-submit");
  const email = (emailEl ? emailEl.value : "").trim();

  // (1) Comprobación de formato SOLO advisory — nunca ramifica por existencia de
  // cuenta. Un email vacío/malformado → error de campo, sin llamada Supabase (AC-4).
  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  if (!email || !EMAIL_RE.test(email)) {
    if (hintEl) { hintEl.textContent = "Introduce un email válido."; hintEl.classList.add("login-error"); }
    if (emailEl) emailEl.focus();
    return;
  }

  // (2) Deshabilitar submit en vuelo. El error/resultado se traga y NUNCA se
  // inspecciona para el mensaje (anti-enumeración). redirectTo es la propia URL del
  // app (window.location.origin), nunca un query param del usuario (open-redirect).
  if (submitEl) submitEl.disabled = true;
  try {
    if (_supabase) {
      await _supabase.auth.resetPasswordForEmail(email, { redirectTo: window.location.origin });
    }
  } catch (e) {
    // Tragado — el resultado nunca se refleja en el mensaje.
  }

  // (3) SIEMPRE el mismo mensaje genérico byte-idéntico (AC-2/AC-3/AC-10).
  if (hintEl) {
    hintEl.classList.remove("login-error");
    hintEl.textContent =
      "Si existe una cuenta con ese email, te hemos enviado un enlace para restablecer la contraseña.";
  }
  if (submitEl) submitEl.disabled = false;
}

// ── Nueva contraseña (sesión de recuperación) ────────────────────────────────
// Reusa las reglas de fuerza de change-password: MIN_PASSWORD_LENGTH (settings.js,
// cargado antes de app.js — referencia solo en cuerpo de función, PS-003-safe) y los
// mismos mensajes byte-idénticos. Lee sin recortar (una contraseña puede llevar
// espacios legítimos). Sin llamada a updateUser si falla la validación cliente. En
// éxito limpia campos + confirma + enruta a login; en error muestra mensaje genérico
// de enlace caducado + revela "pedir un nuevo enlace". La contraseña nunca se loguea,
// ni va a la URL, ni al backend; el error SDK crudo nunca se renderiza.
async function _submitNewPassword() {
  const newEl    = document.getElementById("recovery-new-password");
  const repeatEl = document.getElementById("recovery-new-password-repeat");
  const hintEl   = document.getElementById("recovery-hint");
  const submitEl = document.getElementById("recovery-submit");
  const againBtn = document.getElementById("recovery-request-again");
  if (!newEl || !repeatEl) return;

  // (1) Leer SIN recortar (misma regla que _changePassword).
  const newValue = newEl.value;
  const repeat   = repeatEl.value;

  const fail = (msg, focusEl) => {
    if (hintEl) { hintEl.textContent = msg; hintEl.classList.add("login-error"); }
    if (focusEl) focusEl.focus();
  };

  // (2) Validación cliente ANTES de cualquier llamada SDK — mensajes byte-idénticos a
  // _changePassword. Cualquier fallo retorna SIN llamar a updateUser (AC-6/AC-7).
  if (newValue.length < MIN_PASSWORD_LENGTH) {
    fail("La nueva contraseña debe tener al menos 8 caracteres.", newEl);
    return;
  }
  if (newValue !== repeat) {
    fail("Las contraseñas no coinciden.", repeatEl);
    return;
  }

  // (3) Deshabilitar submit en vuelo (evita doble envío).
  if (submitEl) submitEl.disabled = true;
  if (hintEl) hintEl.classList.remove("login-error");

  let updateError = null;
  try {
    if (!_supabase) throw new Error("sin cliente supabase");
    const { error } = await _supabase.auth.updateUser({ password: newValue });
    updateError = error;
  } catch (e) {
    updateError = e; // nunca se inspecciona/renderiza — solo camino genérico
  }

  if (!updateError) {
    // (4) Éxito: limpiar campos, confirmar y enrutar a login (AC-8). La confirmación
    // se pone DESPUÉS de _setLoginMode (que oculta #login-success).
    newEl.value = "";
    repeatEl.value = "";
    _passwordRecovery = false;
    _hidePasswordRecovery();
    _setLoginMode("login");
    _showLoginScreen();
    const loginSuccess = document.getElementById("login-success");
    if (loginSuccess) {
      loginSuccess.textContent = "Contraseña actualizada. Inicia sesión con tu nueva contraseña.";
      loginSuccess.hidden = false;
    }
    if (submitEl) submitEl.disabled = false;
    return;
  }

  // (5) Error (token caducado/inválido o sin sesión de recuperación): mensaje genérico
  // + revelar "pedir un nuevo enlace" (AC-9). El error SDK crudo nunca se muestra.
  if (hintEl) { hintEl.textContent = "El enlace ha caducado o no es válido."; hintEl.classList.add("login-error"); }
  if (againBtn) againBtn.hidden = false;
  if (submitEl) submitEl.disabled = false;
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
