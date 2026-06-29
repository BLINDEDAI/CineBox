// CineBox — módulo "Compartir" (perfil público + listas) del SPA autenticado.
// Scope global clásico (sin import/export). Cargado ANTES de app.js (PS-003):
// api → ui → collection → modal → discover → stats → sharing → app.
//
// Regla de orden de carga (PS-003): las sentencias de nivel superior que se
// ejecutan al cargar este archivo solo referencian globals de archivos
// cargados antes (api.js: `api`; ui.js: `el`, `esc`, `showMessage`). El
// listener delegado se engancha al cargar y es seguro porque #sharing-view
// está en el DOM inicial. `showSharingView()` la invoca app.js (que carga
// después) al abrir la vista — corre en tiempo de llamada, todo ya cargado.

// ── Estado del módulo ─────────────────────────────────────────────────────
let sharingProfile = null;   // {username, is_public, show_collection, show_stats}
let sharingLists = [];       // [{id, name, visibility, share_token, item_count, updated_at}]
let sharingExpandedListId = null; // id de la lista cuyos items están desplegados
let sharingExpandedItems = []; // items de la lista desplegada

// ── Estado del selector "Añadir a lista" ────────────────────────────────────
let pickerPayload = null;   // {tmdb_id, media_type, title, year, poster_url} del título a añadir
let pickerLists = [];       // listas del usuario cargadas al abrir el selector

// Referencia DOM de carga (guarda con `if (el)` — PS-003 / error conocido).
const sharingViewEl = el("sharing-view");
// Referencia DOM del selector (guarda con `if (listPickerEl)` — PS-003).
const listPickerEl = el("list-picker");

// ── Red ────────────────────────────────────────────────────────────────────
async function loadSharing() {
  const prof = await api("/api/profile");
  if (prof.ok && prof.data.ok) sharingProfile = prof.data.profile;
  const lists = await api("/api/lists");
  if (lists.ok && lists.data.ok) sharingLists = lists.data.lists || [];
  renderSharingView();
}

// Vista invocada desde app.js (showView) al abrir "Compartir".
function showSharingView() {
  loadSharing();
}

// ── Render ───────────────────────────────────────────────────────────────────
function _shareLink(token) {
  return location.origin + "/l/" + token;
}

function renderSharingView() {
  if (!sharingViewEl) return;
  const p = sharingProfile || { username: null, is_public: false, show_collection: false, show_stats: false };
  const hasUsername = Boolean(p.username);

  const profileLink = hasUsername ? location.origin + "/u/" + esc(p.username) : "";

  const listsHtml = sharingLists.length
    ? sharingLists.map((l) => _listRowHtml(l)).join("")
    : `<p class="muted smuted-sm">Aún no tienes listas. Crea una para empezar.</p>`;

  sharingViewEl.innerHTML = `
    <div class="collection-hero">
      <div class="hero-copy">
        <div class="hero-title-row">
          <span class="hero-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5 15.4 17.5M15.4 6.5 8.6 10.5"/></svg>
          </span>
          <div>
            <h1>Compartir</h1>
            <p>Elige tu nombre público y comparte tu perfil y tus listas.</p>
          </div>
        </div>
      </div>
    </div>

    <div class="sharing-panels">
      <section class="spanel sharing-card" aria-labelledby="sharing-username-title">
        <h2 class="spanel-title" id="sharing-username-title">Nombre de usuario</h2>
        <p class="muted smuted-sm">Tu URL pública será <code>/u/tu-nombre</code>. Solo minúsculas, números, guion y guion bajo (3–30).</p>
        <form id="sharing-username-form" class="sharing-form" novalidate>
          <label class="control-label" for="sharing-username-input">Nombre de usuario</label>
          <div class="sharing-username-row">
            <input id="sharing-username-input" class="login-input sharing-input" type="text" inputmode="latin"
                   autocomplete="off" maxlength="30" placeholder="tu-nombre"
                   value="${esc(p.username || "")}" aria-describedby="sharing-username-hint">
            <button class="btn" type="submit" data-sharing-action="save-username">Guardar</button>
          </div>
          <p id="sharing-username-hint" class="sharing-hint muted smuted-sm" role="status">${hasUsername ? "Tu perfil está disponible en " + esc(profileLink) : "Elige un nombre para poder publicar tu perfil o tus listas."}</p>
        </form>
      </section>

      <section class="spanel sharing-card" aria-labelledby="sharing-visibility-title">
        <h2 class="spanel-title" id="sharing-visibility-title">Visibilidad del perfil</h2>
        ${hasUsername ? "" : `<p class="muted smuted-sm">Guarda primero un nombre de usuario para poder publicar tu perfil.</p>`}
        <div class="sharing-toggles" role="group" aria-label="Opciones de visibilidad del perfil">
          ${_toggleHtml("is_public", "Perfil público", p.is_public, !hasUsername)}
          ${_toggleHtml("show_collection", "Mostrar mi colección", p.show_collection, false)}
          ${_toggleHtml("show_stats", "Mostrar mis estadísticas", p.show_stats, false)}
        </div>
        ${hasUsername && p.is_public ? `<p class="sharing-hint muted smuted-sm">Visible en <a class="sharing-link" href="${esc(profileLink)}">${esc(profileLink)}</a></p>` : ""}
      </section>

      <section class="spanel sharing-card sharing-lists-card" aria-labelledby="sharing-lists-title">
        <div class="sharing-lists-head">
          <h2 class="spanel-title" id="sharing-lists-title">Mis listas</h2>
        </div>
        <form id="sharing-create-form" class="sharing-create-form" novalidate>
          <label class="control-label" for="sharing-new-list-name">Nueva lista</label>
          <div class="sharing-username-row">
            <input id="sharing-new-list-name" class="login-input sharing-input" type="text" maxlength="120"
                   autocomplete="off" placeholder="Mi nueva lista">
            <button class="btn" type="submit" data-sharing-action="create-list">Crear</button>
          </div>
        </form>
        <div class="sharing-lists">${listsHtml}</div>
      </section>
    </div>`;

  // Restaura el desplegado de items si había una lista abierta.
  if (sharingExpandedListId !== null) _renderExpandedItems();
}

function _toggleHtml(field, label, checked, disabled) {
  const id = "sharing-toggle-" + field;
  return `
    <label class="sharing-toggle" for="${id}">
      <input id="${id}" type="checkbox" data-sharing-toggle="${field}" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""}>
      <span class="sharing-toggle-label">${esc(label)}</span>
    </label>`;
}

function _listRowHtml(l) {
  const count = Number(l.item_count) || 0;
  const isOpen = sharingExpandedListId === l.id;
  return `
    <article class="sharing-list" data-list-id="${esc(l.id)}">
      <div class="sharing-list-main">
        <button class="sharing-list-toggle" type="button" data-sharing-action="toggle-items" aria-expanded="${isOpen ? "true" : "false"}">
          <span class="sharing-list-name">${esc(l.name)}</span>
          <span class="sharing-list-count">${count} ${count === 1 ? "título" : "títulos"}</span>
        </button>
        <div class="sharing-list-controls">
          <label class="control-label sharing-vis-label" for="sharing-vis-${esc(l.id)}">Visibilidad</label>
          <select id="sharing-vis-${esc(l.id)}" class="select btn-sm sharing-vis-select" data-sharing-action="set-visibility" aria-label="Visibilidad de ${esc(l.name)}">
            <option value="private"  ${l.visibility === "private"  ? "selected" : ""}>Privada</option>
            <option value="unlisted" ${l.visibility === "unlisted" ? "selected" : ""}>Por enlace</option>
            <option value="public"   ${l.visibility === "public"   ? "selected" : ""}>Pública</option>
          </select>
          ${l.visibility !== "private"
            ? `<button class="btn-secondary btn-sm" type="button" data-sharing-action="copy-link" data-token="${esc(l.share_token)}">Copiar enlace</button>`
            : ""}
          <button class="icon-btn" type="button" data-sharing-action="rename-list" aria-label="Renombrar ${esc(l.name)}">✎</button>
          <button class="icon-btn" type="button" data-sharing-action="delete-list" aria-label="Eliminar ${esc(l.name)}">✕</button>
        </div>
      </div>
      <div class="sharing-list-items" data-items-for="${esc(l.id)}" ${isOpen ? "" : "hidden"}></div>
    </article>`;
}

function _renderExpandedItems() {
  if (!sharingViewEl) return;
  const container = sharingViewEl.querySelector(`[data-items-for="${sharingExpandedListId}"]`);
  if (!container) return;
  if (!sharingExpandedItems.length) {
    container.innerHTML = `<p class="muted smuted-sm">Esta lista está vacía. Añade títulos desde el detalle de un título.</p>`;
    return;
  }
  container.innerHTML = sharingExpandedItems.map((it) => `
    <div class="sharing-item" data-item-id="${esc(it.id)}">
      <span class="sharing-item-poster">${posterHtml(it)}</span>
      <span class="sharing-item-title">${esc(it.title)}${it.year ? " (" + esc(it.year) + ")" : ""}</span>
      <button class="icon-btn" type="button" data-sharing-action="remove-item" aria-label="Quitar ${esc(it.title)}">✕</button>
    </div>`).join("");
}

// ── Acciones ──────────────────────────────────────────────────────────────
async function _saveUsername(input) {
  const value = input.value.trim().toLowerCase();
  if (!value) { showMessage("Escribe un nombre de usuario.", "error"); return; }
  const { ok, data } = await api("/api/profile", {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: value }),
  });
  if (ok && data.ok) { showMessage("Nombre de usuario guardado."); await loadSharing(); }
  else if (data && data.error) showMessage(data.error, "error");
  else showMessage("No se pudo guardar el nombre de usuario.", "error");
}

async function _setProfileFlag(field, checked) {
  const { ok, data } = await api("/api/profile", {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ [field]: checked }),
  });
  if (ok && data.ok) { await loadSharing(); }
  else { showMessage((data && data.error) || "No se pudo actualizar la visibilidad.", "error"); await loadSharing(); }
}

async function _createList(input) {
  const name = input.value.trim();
  if (!name) { showMessage("Escribe un nombre para la lista.", "error"); return; }
  const { ok, data } = await api("/api/lists", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (ok && data.ok) { showMessage("Lista creada."); await loadSharing(); }
  else showMessage((data && data.error) || "No se pudo crear la lista.", "error");
}

async function _setListVisibility(listId, visibility) {
  const { ok, data } = await api("/api/lists/" + listId, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ visibility }),
  });
  if (ok && data.ok) { await loadSharing(); }
  else { showMessage((data && data.error) || "No se pudo cambiar la visibilidad.", "error"); await loadSharing(); }
}

async function _renameList(listId, currentName) {
  const name = window.prompt("Nuevo nombre de la lista:", currentName || "");
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed) { showMessage("El nombre no puede estar vacío.", "error"); return; }
  const { ok, data } = await api("/api/lists/" + listId, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: trimmed }),
  });
  if (ok && data.ok) { showMessage("Lista renombrada."); await loadSharing(); }
  else showMessage((data && data.error) || "No se pudo renombrar la lista.", "error");
}

async function _deleteList(listId, name) {
  if (!window.confirm(`¿Eliminar la lista «${name}»? Esta acción no se puede deshacer.`)) return;
  const { ok } = await api("/api/lists/" + listId, { method: "DELETE" });
  if (ok) {
    if (sharingExpandedListId === listId) { sharingExpandedListId = null; sharingExpandedItems = []; }
    showMessage("Lista eliminada.");
    await loadSharing();
  } else showMessage("No se pudo eliminar la lista.", "error");
}

async function _toggleItems(listId) {
  if (sharingExpandedListId === listId) {
    sharingExpandedListId = null;
    sharingExpandedItems = [];
    renderSharingView();
    return;
  }
  const { ok, data } = await api("/api/lists/" + listId);
  if (ok && data.ok) {
    sharingExpandedListId = listId;
    sharingExpandedItems = (data.list && data.list.items) || [];
    renderSharingView();
  } else showMessage((data && data.error) || "No se pudieron cargar los títulos.", "error");
}

async function _removeItem(listId, itemId) {
  const { ok } = await api("/api/lists/" + listId + "/items/" + itemId, { method: "DELETE" });
  if (ok) {
    sharingExpandedItems = sharingExpandedItems.filter((it) => String(it.id) !== String(itemId));
    showMessage("Título eliminado de la lista.");
    await loadSharing();
  } else showMessage("No se pudo eliminar el título.", "error");
}

async function _copyLink(token) {
  const url = _shareLink(token);
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(url);
      showMessage("Enlace copiado al portapapeles.");
      return;
    }
  } catch (e) { /* fallback abajo */ }
  window.prompt("Copia el enlace:", url);
}

// ── Selector "Añadir a lista" ───────────────────────────────────────────────
// Mensaje exacto del backend para el duplicado (409). El wrapper `api()` no
// expone el código HTTP; el cuerpo del 409 trae este texto como contrato
// documentado (server.py _add_list_item), así que distinguimos el duplicado
// por él para tratarlo como aviso no bloqueante (AC-5).
const PICKER_DUPLICATE_ERROR = "Ese título ya está en la lista";

// Abre el selector con el título a añadir. Invocada desde modal.js / collection.js
// (cuerpos de manejador → tiempo de llamada, PS-003). `payload` =
// {tmdb_id, media_type, title, year, poster_url}.
async function openAddToListPicker(payload) {
  if (!listPickerEl) return;
  pickerPayload = payload || null;
  pickerLists = [];
  _renderPicker(true);
  listPickerEl.hidden = false;
  void listPickerEl.offsetWidth;
  listPickerEl.classList.add("is-open");
  const { ok, data } = await api("/api/lists");
  if (ok && data.ok) pickerLists = data.lists || [];
  else { showMessage((data && data.error) || "No se pudieron cargar tus listas.", "error"); }
  _renderPicker(false);
  // Enfoca el primer control accionable para operabilidad por teclado.
  const focusTarget = listPickerEl.querySelector(".list-picker-choice, #list-picker-new-name");
  if (focusTarget) focusTarget.focus();
}

function closeAddToListPicker() {
  if (!listPickerEl) return;
  listPickerEl.classList.remove("is-open");
  listPickerEl.hidden = true;
  pickerPayload = null;
  pickerLists = [];
  listPickerEl.innerHTML = "";
}

function _renderPicker(loading) {
  if (!listPickerEl) return;
  const title = pickerPayload ? pickerPayload.title : "";
  let body;
  if (loading) {
    body = `<p class="muted smuted-sm">Cargando tus listas…</p>`;
  } else if (pickerLists.length) {
    const choices = pickerLists.map((l) => {
      const count = Number(l.item_count) || 0;
      return `
        <button class="list-picker-choice" type="button" data-picker-action="choose-list" data-list-id="${esc(l.id)}">
          <span class="list-picker-choice-name">${esc(l.name)}</span>
          <span class="list-picker-choice-count">${count} ${count === 1 ? "título" : "títulos"}</span>
        </button>`;
    }).join("");
    body = `
      <p class="muted smuted-sm" id="list-picker-desc">Elige una lista para añadir «${esc(title)}».</p>
      <div class="list-picker-choices" role="list">${choices}</div>
      ${_pickerCreateFormHtml()}`;
  } else {
    body = `
      <p class="muted smuted-sm" id="list-picker-desc">Aún no tienes listas. Crea una para añadir «${esc(title)}».</p>
      ${_pickerCreateFormHtml()}`;
  }
  listPickerEl.innerHTML = `
    <div class="list-picker-backdrop" data-picker-action="close"></div>
    <div class="list-picker-card" role="dialog" aria-modal="true" aria-labelledby="list-picker-title" aria-describedby="list-picker-desc">
      <button class="modal-close" type="button" data-picker-action="close" aria-label="Cerrar">✕</button>
      <h2 class="list-picker-title" id="list-picker-title">Añadir a lista</h2>
      ${body}
    </div>`;
}

function _pickerCreateFormHtml() {
  return `
    <form class="list-picker-create" id="list-picker-create-form" novalidate>
      <label class="control-label" for="list-picker-new-name">Nueva lista</label>
      <div class="list-picker-create-row">
        <input id="list-picker-new-name" class="login-input sharing-input" type="text" maxlength="120"
               autocomplete="off" placeholder="Mi nueva lista">
        <button class="btn" type="submit" data-picker-action="create-and-add">Crear y añadir</button>
      </div>
    </form>`;
}

// POST /api/lists/{id}/items con el payload actual. 201 → aviso + cierre
// (AC-1/AC-2); duplicado (409) → aviso no bloqueante, selector abierto (AC-5).
async function _pickerAddToList(listId) {
  if (!pickerPayload) return;
  const { ok, data } = await api("/api/lists/" + listId + "/items", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      tmdb_id: pickerPayload.tmdb_id,
      media_type: pickerPayload.media_type,
      title: pickerPayload.title,
      year: pickerPayload.year,
      poster_url: pickerPayload.poster_url,
    }),
  });
  if (ok && data.ok) {
    const list = pickerLists.find((l) => String(l.id) === String(listId));
    const listName = list ? list.name : "la lista";
    showMessage(`Añadido a «${listName}».`);
    closeAddToListPicker();
    // Si la lista afectada está desplegada en el gestor, refréscala para reflejar el alta.
    if (sharingExpandedListId !== null) await loadSharing();
    return;
  }
  if (data && data.error === PICKER_DUPLICATE_ERROR) {
    showMessage("Ya está en esa lista");   // AC-5: aviso no bloqueante, selector abierto
    return;
  }
  showMessage((data && data.error) || "No se pudo añadir el título.", "error");
}

// Crea la lista y, si se crea, añade el título a ella (AC-4).
async function _pickerCreateAndAdd(input) {
  const name = input.value.trim();
  if (!name) { showMessage("Escribe un nombre para la lista.", "error"); return; }
  const { ok, data } = await api("/api/lists", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (ok && data.ok && data.id) {
    pickerLists.push({ id: data.id, name, item_count: 0 });
    await _pickerAddToList(data.id);
  } else {
    showMessage((data && data.error) || "No se pudo crear la lista.", "error");
  }
}

// ── Listeners delegados (enganchados al cargar; #sharing-view ya en el DOM) ──
if (sharingViewEl) {
  sharingViewEl.addEventListener("submit", (e) => {
    const form = e.target.closest("form");
    if (!form) return;
    e.preventDefault();
    if (form.id === "sharing-username-form") {
      _saveUsername(form.querySelector("#sharing-username-input"));
    } else if (form.id === "sharing-create-form") {
      const input = form.querySelector("#sharing-new-list-name");
      _createList(input);
    }
  });

  sharingViewEl.addEventListener("change", (e) => {
    const toggle = e.target.closest("[data-sharing-toggle]");
    if (toggle) { _setProfileFlag(toggle.dataset.sharingToggle, toggle.checked); return; }
    const visSelect = e.target.closest("[data-sharing-action='set-visibility']");
    if (visSelect) {
      const listId = visSelect.closest(".sharing-list")?.dataset.listId;
      if (listId) _setListVisibility(listId, visSelect.value);
    }
  });

  sharingViewEl.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-sharing-action]");
    if (!btn) return;
    const action = btn.dataset.sharingAction;
    const listEl = btn.closest(".sharing-list");
    const listId = listEl?.dataset.listId;
    const list = listId ? sharingLists.find((l) => String(l.id) === String(listId)) : null;

    if (action === "copy-link") { _copyLink(btn.dataset.token); }
    else if (action === "toggle-items" && listId) { _toggleItems(listId); }
    else if (action === "rename-list" && list) { _renameList(listId, list.name); }
    else if (action === "delete-list" && list) { _deleteList(listId, list.name); }
    else if (action === "remove-item" && listId) {
      const itemId = btn.closest(".sharing-item")?.dataset.itemId;
      if (itemId) _removeItem(listId, itemId);
    }
  });
}

// ── Listeners delegados del selector (#list-picker ya en el DOM inicial) ─────
if (listPickerEl) {
  listPickerEl.addEventListener("click", (e) => {
    const trigger = e.target.closest("[data-picker-action]");
    if (!trigger) return;
    const action = trigger.dataset.pickerAction;
    if (action === "close") { closeAddToListPicker(); }
    else if (action === "choose-list") {
      const listId = trigger.dataset.listId;
      if (listId) _pickerAddToList(listId);
    }
  });

  listPickerEl.addEventListener("submit", (e) => {
    const form = e.target.closest("#list-picker-create-form");
    if (!form) return;
    e.preventDefault();
    _pickerCreateAndAdd(form.querySelector("#list-picker-new-name"));
  });
}
