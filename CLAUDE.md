# Cinephora — CLAUDE.md

Rastreador personal de películas y series. Backend Python puro (sin framework), frontend vanilla JS, PostgreSQL en Supabase.

## Commands

| Acción | Comando |
|--------|---------|
| Iniciar servidor | `python server.py` (puerto 8000) |
| Tests unitarios | `python -m unittest discover -s tests` |
| Tests E2E (navegador) | `pip install -r requirements-dev.txt && playwright install chromium`, luego `pytest tests/e2e/` (ver `tests/e2e/README`; ADR-003) |
| Deploy | `git push origin main` (auto-deploy en Render) |
| Base de datos | Supabase — no hay comandos locales de DB |
| Entorno | Variables en `.env` — **nunca commitear este archivo** |

---

## Flujo de sesión

At the start of each session, read CONTEXT.md if it exists. At the end, update it with what was done and what's next.

### Convención de CONTEXT.md
- **Sección superior:** snapshot del estado actual — se actualiza *in place* cada sesión.
- **Debajo:** registro de sesiones append-only con fecha — nunca se borra, solo se añade.
- **Al cerrar sesión:** primero actualizar el snapshot, luego añadir la entrada de la sesión.

## Stack

| Capa | Tecnología |
|------|-----------|
| Backend | Python 3, `http.server.ThreadingHTTPServer` (stdlib, sin Flask/FastAPI) |
| Base de datos | PostgreSQL vía `psycopg2`, hosteado en Supabase |
| Auth | Supabase email/password + JWT asimétrico (ES256/RS256 via JWKS); se exige `aud=authenticated` y `role=authenticated` |
| Frontend | HTML + CSS + JS vanilla (sin build, sin bundler) |
| API externa | TMDB v3 |
| Notificaciones | Discord Incoming Webhooks (async en threads) |
| Hosting | Backend en Render |

## Archivos clave

```
server.py   — todo el backend (~660 líneas)
index.html  — SPA de una sola página; carga boot.js (síncrono) + los 7 módulos JS (<script defer>)
boot.js     — script síncrono en <head>: marca <html> con la clase cinephora-visited
              (de localStorage) ANTES de pintar, para evitar el flash de bienvenida
styles.css  — estilos
.env        — secretos (no commitear)
```

### Frontend — 7 módulos de scope global (sin bundler)

`script.js` se dividió en 7 archivos. Son scripts clásicos (no ES modules): todo vive
en el scope global, sin `import`/`export`. Se cargan con `<script defer>` en **este
orden exacto** (lo imponen las dependencias en tiempo de carga):

`api.js → ui.js → collection.js → modal.js → discover.js → stats.js → app.js`

| Archivo | Responsabilidad | Funciones / símbolos |
|---------|-----------------|----------------------|
| `api.js` | Red y token | `_getToken`, `api` |
| `ui.js` | Helpers de presentación | `el`, `esc`, `mediaIcon`, `mediaLabel`, `notePreview`, `todayIsoDate`, `showMessage`, `posterHtml`, `starsHtml` (+ consts `STAR`, `FILM`, `messageEl`) |
| `collection.js` | Colección: orden, render, CRUD, «Esta noche» | `recentValue`, `yearValue`, `ratingValue`, `byRecent`, `sortCollection`, `renderCollection`, `renderSkeleton`, `closePickPanel`, `renderPickPanel`, `loadMovies`, `addItem`, `patchMovie`, `deleteMovie`, `pickTonight` + delegador `click` de la colección (+ consts `collectionEl`, `emptyEl`, `collectionEmptyStateEl`, `collectionControlsEl`, `PLATFORMS`) |
| `modal.js` | Modal de detalle TMDB | `closeModal`, `openDetail`, `addFromModal` (+ `modalContent`, `modalContext`) |
| `discover.js` | Descubrir / búsqueda | `renderResults`, `renderGenreChips`, `loadDiscover`, `loadTrending` (+ const `DISCOVER_GENRES`) |
| `stats.js` | Estadísticas y nivel | `loadLevel`, `renderStatsView` (+ `levelData`) |
| `app.js` | Arranque, estado compartido, auth, listeners | auth de Supabase (`_setLoginMode`, etc.), `showView`, estado global (`movies`, `filter`, `lastResults`, `editing*`, …), refs DOM compartidas (`resultsEl`, `modalEl`, `pickPanelEl`), todos los `addEventListener` restantes, `initApp`, `_updateSidebarUser` |

⚠️ **Regla de orden de carga (no romper).** Cualquier sentencia de nivel superior que
se ejecute en el acto al cargar el archivo (un `addEventListener`, una llamada como
`renderGenreChips()`, un `const x = el(...)`) solo puede referirse a globals declarados
en el **mismo archivo más arriba** o en un archivo **cargado antes**. Los *cuerpos* de
funciones sí pueden usar globals de archivos posteriores (corren en tiempo de llamada,
ya cargado todo). Por eso `const collectionEl` vive en `collection.js` y no en `app.js`:
su delegador `click` lo usa al cargar. Mover una declaración a un archivo más tardío que
su primer uso inmediato = `ReferenceError` en carga y app muerta.

---

## Barreras — leer antes de tocar nada

**El frontend (7 módulos, ver «Archivos clave») es frágil.** Cambios en el front han causado crashes en el pasado. Avisar al usuario antes de tocar cualquier módulo, describir exactamente qué se va a cambiar y respetar el orden de carga.

**Parar antes de acciones destructivas:** cambios en la DB (migraciones, DROP, ALTER), sobrescribir archivos, deploys. Confirmar primero.

**Plan antes de cambios grandes.** Si el cambio toca más de 2 funciones o añade un endpoint nuevo: describir el plan y esperar aprobación antes de ejecutar.

**Un cambio a la vez.** Hacer el cambio mínimo, parar, reportar resultado. No encadenar cambios sin confirmación.

**Verificar arranque tras cambios en backend.** Después de editar `server.py`, confirmar que el servidor levanta sin errores antes de dar la tarea por hecha.

---

## Convenciones del código

### Auth — obligatorio en todos los endpoints que tocan la DB

```python
user_id = verify_jwt(self.headers.get("Authorization", ""))
if not user_id:
    return self._json(401, {"error": "No autorizado"})
```

Sin esto el usuario puede acceder a datos ajenos. No hay excepciones.

### Lectura de JSON del cuerpo

```python
body = self._read_json()  # ya aplica MAX_BODY (64 KB)
```

No leer `self.rfile` directamente — puede colgar el servidor con requests grandes.

### Respuestas JSON

```python
self._json(200, {"ok": True, "data": ...})
```

El método `_json` añade las cabeceras de seguridad necesarias. No usar `self.wfile.write()` directamente para respuestas JSON.

### Queries a la DB

```python
with get_db() as cur:
    cur.execute("SELECT * FROM movies WHERE user_id = %s AND id = %s", (user_id, movie_id))
```

- Siempre `%s` como placeholder (psycopg2, no `?`)
- Siempre filtrar por `user_id` además de ID de recurso
- El context manager hace commit/rollback automático

### Discord

```python
threading.Thread(target=_notify_discord, args=(...), daemon=True).start()
```

Las notificaciones van en thread separado — nunca en el hilo de respuesta HTTP.

### Conexiones a la DB — pool acotado

`get_db()` toma conexiones de un `ThreadedConnectionPool(1, DB_POOL_MAX)` gateado por
un semáforo del mismo tamaño (inicializado en `main()` antes de `init_db`). Los hilos
sobrantes esperan hasta `DB_WAIT_TIMEOUT` (10 s) y, si no hay slot, el endpoint devuelve
**503** (vía el decorador `_db_guard` sobre los `do_*`). No volver a abrir conexiones
sueltas con `psycopg2.connect`: usar siempre `with get_db() as cur:`.

### Rate limiting — endpoints que pegan a TMDB

Los 6 endpoints que consumen la clave TMDB (`/api/search`, `/api/trending`,
`/api/discover`, `/api/details`, `/api/similar`, `/api/tv/{id}/season/{n}`) pasan por `self._rate_limited(user_id)`
justo tras la auth. Ventana deslizante en memoria (por proceso): **por usuario**
`RATE_MAX` (60) y **global** `RATE_GLOBAL_MAX` (300) por `RATE_WINDOW` (60 s). Al superar
cualquiera → **429 + `Retry-After`**. Las constantes viven en `server.py`. Si se añade
otro endpoint que llame a `_tmdb(...)`, aplicarle el mismo patrón.

---

## Errores conocidos — no repetir

**Listeners sobre elementos que pueden no existir en el DOM.**
`document.getElementById("x").addEventListener(...)` explota si el elemento no está en la página actual. Usar siempre `const el = document.getElementById("x"); if (el) el.addEventListener(...)`.

**CSP bloqueando CDNs externos.**
La cabecera `Content-Security-Policy` del servidor controla qué scripts/estilos se cargan. Al añadir librerías externas, actualizar la CSP en `server.py` o el script no cargará en producción (PS-006). **Nota:** supabase-js ya NO se carga desde un CDN — se sirve self-host con SRI (ver «supabase-js vendored» abajo), y por eso `script-src` es `'self'` (sin `cdn.jsdelivr.net`). No reintroducir un CDN para scripts.

### supabase-js vendored (self-host + SRI)

El cliente Supabase JS (auth/login/JWT) se sirve **desde el propio origen** con
Subresource Integrity, no desde jsDelivr. Esto cierra un agujero de cadena de
suministro: el navegador solo ejecuta el bundle si sus bytes coinciden con el hash.

| Dato | Valor |
|------|-------|
| Versión fijada | `@supabase/supabase-js` **2.108.1** |
| Archivo | `vendor/supabase-js/2.108.1/supabase.min.js` (UMD canónico de npm) |
| SRI (en `index.html`) | `sha384-EjUdIVmzWliPzdzhxZ9ZoO0etXLKWuUPUftAGxP6qH6Lm4oLwoLaJR0Ba4pIDiDL` |
| CSP | `script-src 'self'` (sin `cdn.jsdelivr.net`) |
| Fallback | **Ninguno** — fail-closed: si el SRI falla, el script no se ejecuta |

El `<script>` sigue **síncrono en `<head>`** (PS-003): el símbolo global `supabase`
debe existir antes de `boot.js` y los 7 módulos `defer`. No añadir `defer`/`async`.

**Suite E2E de navegador (ADR-003).** Estas garantías de cadena de suministro de
runtime de navegador (supabase-js servido desde el propio origen sin petición a
`cdn.jsdelivr.net`; bundle manipulado bloqueado por SRI sin fallback a CDN) ahora
tienen una suite automatizada Playwright/pytest bajo `tests/e2e/`, separada del gate
`unittest`. El fixture (`tests/e2e/conftest.py`) arranca el `server.Handler` real sin
DB. Ejecutar con `pytest tests/e2e/` (instalar primero con `pip install -r
requirements-dev.txt && playwright install chromium`). Ver `tests/e2e/README`.

**Actualizar versión** (cambio atómico — bump del archivo + del hash en el mismo
commit): re-descargar el UMD canónico de npm, recalcular el SRI sha384, actualizar
`src` + `integrity` en `index.html`, y este bloque + `vendor/supabase-js/README`.
El procedimiento completo (comandos) está en `vendor/supabase-js/README`.

**`Content-Length` negativo.**
Al construir respuestas HTTP manualmente, calcular el tamaño del body en bytes (`len(body.encode())`) antes de escribir la cabecera. Un valor negativo o incorrecto rompe la conexión en algunos clientes.

**IDs de géneros de TMDB: películas ≠ series.**
El endpoint `/api/discover` usa géneros distintos según `media_type`. El ID 10759 ("Acción y Aventura") existe solo para TV; para películas es 28 ("Acción"). Hay una tabla de mapeo en `server.py` (~línea 354) — consultarla antes de añadir géneros al discover.

---

## Sistema de niveles

Cada usuario acumula puntos por su actividad. **El cálculo vive 100% en `server.py` — el cliente nunca suma puntos.**

### Puntuación

| Acción | Puntos | Cómo se cuenta |
|--------|--------|----------------|
| Película/serie vista | 10 | `status = 'vista'` |
| Valoración puesta | 5 | `rating IS NOT NULL` |
| Nota escrita | 5 | `note IS NOT NULL AND note <> ''` |

Las tres son independientes: un mismo título visto + valorado + con nota suma 20 pts.

### Niveles

| Nivel | Nombre | Rango de puntos |
|-------|--------|-----------------|
| 1 | Espectador | 0–49 |
| 2 | Aficionado | 50–149 |
| 3 | Cinéfilo | 150–349 |
| 4 | Crítico | 350–699 |
| 5 | Experto | 700–1199 |
| 6 | Maestro | 1200+ |

La tabla es la constante `LEVELS` en `server.py` (única fuente de verdad). La función `compute_level(points)` devuelve nivel, nombre y progreso al siguiente.

### Endpoint `GET /api/similar`

- Requiere auth (`_get_user_id`) → `401` si no.
- Params: `id` (TMDB id, dígitos) y `type` (`movie` | `tv`) → `400` si inválidos.
- Llama a TMDB `/{type}/{id}/similar`. Si no hay clave TMDB o TMDB falla: devuelve `{ok: true, results: []}` (sin error).
- Respuesta: `{ok, results: [{tmdb_id, type, title, year, poster_url}]}` — máximo 6 items, filtrados si no tienen `id`.
- Frontend: `openDetail` acepta tercer parámetro `hint={}` con `{title, poster_url, year}` para títulos que no están en la colección del usuario. La sección "Títulos similares" se inyecta al fondo del modal de detalle de forma no bloqueante.

---

### Endpoint `GET /api/level`

- Requiere auth (`_get_user_id`) → `401` si no.
- Una sola query agregada (`COUNT(*) FILTER (...)`) filtrada por `user_id`; no trae filas.
- Respuesta: `{ok, points, level, name, current_min, next_min, next_name, points_into_level, points_to_next, progress_pct}`. En el nivel máximo `next_*` es `null` y `progress_pct` es 100.

El frontend lo pinta en Estadísticas (`renderStatsView` en `script.js`): tarjeta con nombre del nivel, puntos y barra de progreso. Se refresca solo porque `loadMovies()` llama a `loadLevel()`, y toda mutación (vista/rating/nota) pasa por `loadMovies()`.

---

## Variables de entorno

Todas van en `.env` (ver `.env.example`). Las marcadas **requeridas** crashean el servidor si faltan.

| Variable | Uso | Requerida |
|----------|-----|-----------|
| `DATABASE_URL` | Conexión PostgreSQL | **Sí** |
| `SUPABASE_URL` | URL del proyecto Supabase | **Sí** |
| `SUPABASE_ANON_KEY` | Clave pública (se envía al browser) | **Sí** |
| `SUPABASE_JWT_SECRET` | Ya no se usa en runtime (HS256 eliminado) | No |
| `SUPABASE_SERVICE_KEY` | Borrado de cuenta (`POST /api/account/delete`): elimina el usuario en Supabase Auth vía la admin API. Solo servidor; nunca al cliente ni a logs | Sí (para borrar cuenta) |
| `TMDB_API_KEY` | Sin él, search/discover/details no funcionan | No |
| `DISCORD_WEBHOOK_VISTA` | Webhook cuando se marca como vista | No |
| `DISCORD_WEBHOOK_PENDIENTE` | Webhook cuando se añade a pendientes | No |
| `DISCORD_WEBHOOK_URL` | Webhook genérico de fallback | No |
| `DISCORD_OWNER_ID` | UUID Supabase — filtra notificaciones al owner | No |
| `PORT` | Puerto del servidor | No (default: 8000) |
| `DB_POOL_MAX` | Máx. conexiones del pool a Postgres (por proceso). Subir solo si el pooler de Supabase lo aguanta; con varias instancias, el total es `instancias × DB_POOL_MAX` | No (default: 10) |

## Agentes — obligatorio antes de cualquier commit/push

### Cuándo entra cada agente

| Cambio | reviewer | security | tester | dod-checker |
|--------|----------|----------|--------|-------------|
| server.py (endpoints, auth, DB, validación) | ✅ | ✅ | ✅ | ✅ |
| JS con lógica de negocio (collection, modal, discover, stats, app) | ✅ | ✅ | ✅ | ✅ |
| JS sin lógica de negocio (ui.js, boot.js) | ✅ | ❌ | ✅ | ✅ |
| styles.css | ❌ | ❌ | ❌ | ✅ |
| index.html / boot.js sin scripts nuevos | ✅ | ✅ | ✅ | ✅ |
| Refactor mecánico (mover código sin cambiar lógica) | ✅ | ✅ | ✅ | ✅ |
| CLAUDE.md / CONTEXT.md / docs | ❌ | ❌ | ❌ | ✅ |

### Orden de ejecución
reviewer → security (si aplica) → tester (si aplica) → dod-checker

### Regla de cambios mixtos
Si un cambio toca varios tipos de archivo, aplica el criterio
más estricto de todos los archivos tocados.

### Regla de duda
Si no está claro en qué fila cae → pasar los cuatro.
No hacer commit sin que dod-checker haya dado DONE.

### Sesiones separadas: bug no trivial → fix en sesión nueva
Cuando `tester` (o `reviewer`/`security`) encuentra un bug **no trivial**, NO se
arregla en la misma sesión: contamina el contexto de test/review con detalles de
implementación y empeora las dos tareas. En su lugar:

1. La sesión actual produce un **prompt autocontenido** para el fix: síntoma
   (observado vs esperado + error literal), reproducción, causa raíz si se conoce
   (archivo/función), criterio de aceptación y restricciones (qué NO tocar).
   El agente `tester` ya emite este prompt en su sección "Fix prompts for new sessions".
2. Se abre una **sesión nueva** con ese prompt → arregla enfocada solo en el fix,
   sin arrastrar el ruido de la verificación.
3. Se vuelve a la sesión original y se **relanza** la verificación (`tester`).

Trivial (typo, guard de una línea, off-by-one obvio) → arreglar inline. Ante la
duda, separar.
