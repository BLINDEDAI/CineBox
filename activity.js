// CineBox — módulo "Actividad" (feed social) del SPA autenticado.
// Scope global clásico (sin import/export). Cargado ANTES de app.js (PS-003):
// api → ui → collection → modal → discover → stats → settings → activity → app.
//
// Regla de orden de carga (PS-003): las ÚNICAS sentencias de nivel superior que
// se ejecutan al cargar este archivo son la referencia DOM guardada
// `const activityViewEl = el("activity-view")` y el listener delegado enganchado
// dentro de `if (activityViewEl) { … }`. Ambas referencian solo globals de
// archivos cargados ANTES (ui.js: `el`; api.js: `api` — usado en cuerpos de
// función, tiempo de llamada). `showActivityView()` la invoca app.js (que carga
// después) al abrir la vista → corre en tiempo de llamada, todo ya cargado.
// `posterHtml`, `esc`, `showMessage` (ui.js, 2º) también se usan solo dentro de
// cuerpos de función (tiempo de llamada) → PS-003-safe.

// Verbos de acción del feed (es-ES). El valor `action` del backend es un
// identificador inglés del allow-list (`watched`/`rated`/`list_add`, US-001);
// aquí se traduce a copy es-ES visible. Literal — seguro en tiempo de carga.
const ACTIVITY_STARS_ON = "★";
const ACTIVITY_STARS_OFF = "☆";

// Estrellas como texto (★/☆) para el verbo "valoró" — decorativo (el número va
// en aria-label). Reutiliza la misma forma que public.js.
function _activityStars(rating) {
  const n = Math.max(0, Math.min(5, Number(rating) || 0));
  return ACTIVITY_STARS_ON.repeat(n) + ACTIVITY_STARS_OFF.repeat(5 - n);
}

// Avatar del actor en el feed. Estado-consciente ARIA (lección FE del bundle):
// en la rama <img> el propio alt del <img> porta el nombre accesible (SIN
// role="img" alrededor de una imagen real); en la rama de iniciales el wrapper
// lleva role="img"+aria-label y las iniciales van aria-hidden. Reutiliza los
// mismos allow-lists de origen (Supabase Storage) que app.js/public.js. Devuelve
// una cadena HTML (template-string con esc() en cada valor de usuario).
const ACTIVITY_AVATAR_RE = /^https:\/\/[a-z0-9-]+\.supabase\.co\/storage\/v1\/object\/public\/avatars\//;

function _activityAvatarHtml(username, avatarUrl) {
  const u = username || "";
  if (avatarUrl && ACTIVITY_AVATAR_RE.test(avatarUrl)) {
    // <img> real: su alt porta el nombre; sin role="img" en el wrapper.
    return `<span class="activity-avatar"><img src="${esc(avatarUrl)}" alt="Avatar de @${esc(u)}" loading="lazy"></span>`;
  }
  // Rama iniciales: wrapper role="img"+aria-label; iniciales decorativas.
  const initials = u ? esc(u.slice(0, 2).toUpperCase()) : "?";
  return `<span class="activity-avatar activity-avatar-generated" role="img" aria-label="Avatar de @${esc(u)}">` +
    `<span class="activity-avatar-initials" aria-hidden="true">${initials}</span></span>`;
}

// Construye el fragmento del verbo de acción (es-ES) por tipo de evento. El
// título y el nombre de lista se envuelven en «» y se escapan con esc().
function _activityVerbHtml(entry) {
  const title = `«${esc(entry.title || "")}»`;
  if (entry.action === "watched") {
    return `marcó ${title} como vista`;
  }
  if (entry.action === "rated") {
    const stars = _activityStars(entry.rating);
    const label = `${Math.max(0, Math.min(5, Number(entry.rating) || 0))} de 5 estrellas`;
    return `valoró ${title} <span class="activity-stars" aria-label="${esc(label)}">${esc(stars)}</span>`;
  }
  if (entry.action === "list_add") {
    return `añadió ${title} a la lista «${esc(entry.list_name || "")}»`;
  }
  return "";
}

// Renderiza una entrada del feed como <li>. El poster reutiliza posterHtml
// (ui.js). Para `list_add` con un share_token válido, la entrada enlaza a la
// lista pública /l/{token}. Todo valor de usuario pasa por esc().
function _activityEntryHtml(entry) {
  const avatar = _activityAvatarHtml(entry.username, entry.avatar_url);
  const verb = _activityVerbHtml(entry);
  const poster = `<span class="activity-poster">${posterHtml({ poster_url: entry.poster_url, title: entry.title })}</span>`;

  const body =
    `<div class="activity-line">` +
      avatar +
      `<div class="activity-text">` +
        `<span class="activity-user">@${esc(entry.username || "")}</span> ` +
        `<span class="activity-verb">${verb}</span>` +
      `</div>` +
      poster +
    `</div>`;

  // list_add con token → toda la entrada es un enlace a la lista pública.
  if (entry.action === "list_add" && entry.list_share_token) {
    return `<li class="activity-item">` +
      `<a class="activity-link" href="/l/${esc(entry.list_share_token)}">${body}</a>` +
      `</li>`;
  }
  return `<li class="activity-item">${body}</li>`;
}

// Estado vacío amistoso (AC-15): ni error, ni lista en blanco.
function _activityEmptyHtml() {
  return `<div class="activity-empty">` +
    `<p class="activity-empty-title">Tu feed está vacío</p>` +
    `<p class="activity-empty-sub">Sigue a otras personas desde su perfil público para ver aquí lo que ven, valoran y añaden a sus listas.</p>` +
    `</div>`;
}

// GET /api/feed vía api() y pinta el feed reverse-cronológico. api() ya devuelve
// {ok, status, data}; se ramifica por `ok`/`status`, nunca por strings del body.
async function showActivityView() {
  const view = el("activity-view");
  if (!view) return;
  view.innerHTML = `<div class="activity-loading" role="status">Cargando actividad…</div>`;

  const res = await api("/api/feed");
  if (!res.ok || !res.data || !res.data.ok) {
    view.innerHTML =
      `<header class="activity-head"><h1>Actividad</h1></header>` +
      `<p class="activity-error" role="status">No se ha podido cargar la actividad. Inténtalo de nuevo más tarde.</p>` +
      `<button class="btn-secondary activity-retry" type="button" data-activity-refresh>Reintentar</button>`;
    return;
  }

  const entries = Array.isArray(res.data.activity) ? res.data.activity : [];
  const head = `<header class="activity-head"><h1>Actividad</h1>` +
    `<p class="activity-sub">Lo último de las personas que sigues.</p></header>`;

  if (!entries.length) {
    view.innerHTML = head + _activityEmptyHtml();
    return;
  }

  // El backend ya entrega newest-first (ORDER BY created_at DESC).
  const items = entries.map(_activityEntryHtml).join("");
  view.innerHTML = head + `<ul class="activity-list">${items}</ul>`;
}

// ── Referencia DOM de carga + listener delegado (PS-003) ─────────────────────
// Únicas sentencias de nivel superior. `#activity-view` existe en el DOM inicial
// (index.html). El listener delegado se engancha al contenedor estable y solo
// referencia globals cargados antes (`showActivityView` es de este archivo).
// Un `data-activity-refresh` en el estado de error/vacío permite re-cargar el
// feed sin recargar la página. `showActivityView()` la invoca app.js (tiempo de
// llamada) → PS-003-safe.
const activityViewEl = el("activity-view");
if (activityViewEl) {
  activityViewEl.addEventListener("click", (ev) => {
    const refresh = ev.target.closest("[data-activity-refresh]");
    if (refresh) {
      ev.preventDefault();
      showActivityView();
    }
    // Los enlaces list_add (<a href="/l/{token}">) navegan de forma nativa.
  });
}
