# CONTEXT.md — CineBox

Estado de trabajo entre sesiones. Se lee al inicio de cada sesión y se actualiza al final.

---

## Estado actual (snapshot) — 2026-06-10

Resumen consolidado del proyecto. El **registro por sesiones** está más abajo.

### Qué es
CineBox: rastreador personal de películas y series. Backend Python puro (stdlib, sin framework),
frontend vanilla JS sin bundler, PostgreSQL en Supabase, deploy en Render. Auth Supabase
(email/password) con JWT asimétrico (ES256/RS256 vía JWKS).

### Qué está construido (funcional)
- **Colección personal**: añadir, editar (estado vista/pendiente, rating, nota), borrar. Filtrado y
  orden (reciente / año / valoración). Estado vacío y skeletons de carga.
- **«Esta noche»**: panel que sugiere un título al azar de los pendientes.
- **Descubrir**: búsqueda TMDB, trending, discover por géneros (chips), con mapeo de géneros
  película≠serie. Modal de detalle con quick-add y sección **«Títulos similares»**.
- **Estadísticas + sistema de niveles**: 6 niveles por puntos (vista=10, rating=5, nota=5); cálculo
  100% en backend (`compute_level` / constante `LEVELS`). Barras de progreso vía CSSOM (CSP estricta).
- **Notificaciones Discord** async (vista / pendiente) en threads, filtradas al owner.

### Endpoints (server.py)
- `GET` — `/api/config`, `/api/movies`, `/api/level`, `/api/search`, `/api/trending`,
  `/api/discover`, `/api/details`, `/api/similar`, `/health`
- `POST /api/movies` · `PATCH /api/movies/{id}` · `DELETE /api/movies/{id}`
- Todos los que tocan DB exigen JWT válido (`aud`/`role=authenticated`). Los 5 que pegan a TMDB pasan
  por rate limiting (60/usuario + 300/global por 60s → 429).

### Hardening vigente
JWT asimétrico-only · pool DB acotado y gateado (503 si saturado) · rate limiting TMDB ·
`socket timeout` 15s (Slowloris) · validación POST (`title`≤300, `year`≤10, `poster_url` solo
`image.tmdb.org`) · CSP estricta · `MAX_BODY` 64KB.

### Pendiente abierto (decisiones del usuario)
- **Self-host de supabase-js** para poder fijar versión + SRI (hoy usa `@2` flotante, por eso NO se
  añadió SRI). Sin decidir.
- **Versionar `.claude/`** (hooks) en el repo o dejarlo local. Sin decidir.
- **Verificación manual en producción** tras el último deploy (login+recarga, género desde modal, logout).
- Posible mejora de infra: **MCP de Postgres/Supabase de solo-lectura** para inspeccionar schema/datos
  reales (propuesto, no iniciado).

---

## Última sesión — 2026-06-10 (auditoría de seguridad/calidad)

Auditoría completa del proyecto (reviewer + security sobre todo el código) y corrección de
todos los hallazgos. **9 commits, pusheados a `origin/main`** (`716fa60..5795885`).

### Hecho hoy
- **JWT endurecido** (`d8ca1d0`): eliminado el fallback HS256; solo firma asimétrica JWKS
  (ES256/RS256). Ahora se exige `audience="authenticated"` y `role="authenticated"`, y `sub` no vacío.
  `except` ampliado (ValueError/OSError) → JWKS malformado o red caída devuelve 401, no 500.
  `PyJWT>=2.8,<3` pineado. **Decisión del usuario:** eliminar HS256 por completo (su proyecto Supabase
  firma asimétrico). `SUPABASE_JWT_SECRET` ya no se usa en runtime.
- **CSP / barras de estadísticas** (`5c03453`): los `style="width:X%"` inline estaban bloqueados por la
  CSP estricta → barras siempre al 100%. Ahora `data-pct` + se fija el ancho vía CSSOM
  (`element.style`, no sujeto a `style-src`). Estados vacíos → clase `.smuted-sm`. CSP sin tocar.
- **Bug: géneros no se guardaban al añadir desde el modal** (`c559204`): `addFromModal` no mandaba
  `genre_ids`. Causa raíz del "no se actualiza" era además **3 instancias viejas de `server.py`** ocupando
  el puerto 8000 (ver Aprendizajes). Fix: `/api/details` ahora devuelve `genre_ids`; el modal los envía;
  el backend los mapea con `TMDB_GENRES` → nombres ES consistentes con la ruta de la carátula. **Verificado
  por el usuario en vivo.**
- **#4 Pool de conexiones DB** (`9e69ae5`): `get_db()` usa `ThreadedConnectionPool(1, DB_POOL_MAX=10)`
  gateado por un `BoundedSemaphore`; los hilos sobrantes esperan ≤`DB_WAIT_TIMEOUT`(10s) y devuelven **503**
  (decorador `_db_guard`). A prueba de excepciones (semáforo liberado siempre; conexiones rotas descartadas).
  `DATABASE_URL` es el **pooler de Supabase (pgbouncer 6543)** → pool cliente modesto.
- **#4 Rate limiting** (`8d4fd58`): ventana deslizante en memoria sobre los 5 endpoints TMDB
  (`search/trending/discover/details/similar`): **por usuario 60/60s + global 300/60s** (el global cierra
  el abuso multi-cuenta). Supera → **429 + Retry-After**. Registro atómico. Por proceso (no compartido).
- **#5/#6 Hardening** (`739d6cd`): `Handler.timeout=15s` (Slowloris); `title`≤300, `year`≤10, y `poster_url`
  solo si empieza por `https://image.tmdb.org/` (cierra SSRF/tracking vía el embed de Discord).
- **Limpieza** (`5795885`): quitada rama muerta `toggle` (collection.js); el listener auth ya no recarga en
  `INITIAL_SESSION` (sin doble `loadMovies` al abrir; login/refresh siguen cargando); docs (`MAX_BODY`=64KB,
  `boot.js`, `DB_POOL_MAX`, rate limiting, pool) en CLAUDE.md y `.env.example`.
- **Hook arreglado** (local, `.claude/` no versionado): `block_dirty_commit.py` ahora permite `.env.example`
  (el escaneo de secretos sigue activo sobre su contenido); `.env*` reales siguen bloqueados.

### Cómo se probó (DoD punto 3)
- Arranque: `python server.py` → `/health` 200. Sintaxis: `ast.parse` (server.py) + `node --check` (módulos JS) en cada cambio.
- Pool: contra Supabase real — init_db, reutilización, concurrencia 20 hilos/pool 4 (0 errores), recuperación tras error de query, timeout→DBBusy con pool operativo después.
- Rate limiting: unit (límite, aislamiento por key, thread-safety 50 hilos/max 10 = exactamente 10, tope global + atomicidad) y **E2E HTTP** (3×200 → 429 + `Retry-After:60`).
- Validación POST: posters maliciosos/`javascript:` descartados, caps aplicados.
- Cada cambio de lógica pasó **reviewer + security** (verde tras aplicar sus correcciones).

### Pendiente / decisiones para el usuario
- **#10 (no hecho a propósito):** NO añadir SRI al `<script>` de supabase-js — usa `@2` (versión flotante) y el
  hash rompería en la próxima minor. El fix correcto es **self-hostear** el bundle (descargar, servir desde el
  dominio, fijar versión, CSP `script-src 'self'`). Pendiente de decidir si se hace.
- **Versionar `.claude/`** (hooks) en el repo, o dejarlo como config local — pendiente de decidir.
- **Verificación manual en producción tras el deploy:** (1) login + recarga → carga la colección (JWT
  asimétrico, sin doble fetch); (2) añadir desde el modal → el género sube en Estadísticas; (3) logout limpia.

### Aprendizajes (no repetir)
- **Puerto 8000 ocupado por instancias viejas:** `python server.py` no recarga en caliente; si `Ctrl+C` no mató
  el proceso, el nuevo no bindea y **sigue sirviendo el viejo** (código sin cambios). Síntoma típico: "reinicié y
  el fix no aplica". Comprobar con `Get-NetTCPConnection -LocalPort 8000` / `Get-CimInstance Win32_Process` y matar
  las huérfanas antes de relanzar.
- **Artefacto de visualización en Windows:** las herramientas muestran `/` como `\` en parte de la salida
  (vimos `\api\movies`, `\health`, `\ //comentario`). NO son bugs — verificar con `node --check`/`grep` los bytes
  reales antes de "arreglar" una falsa alarma (nos pasó con `deleteMovie` y con un comentario en app.js).

### Para empezar la próxima sesión
1. Leer este CONTEXT.md.
2. Confirmar que el deploy de Render terminó y hacer la verificación manual de arriba.
3. Decidir sobre #10 (self-host supabase-js) y versionado de `.claude/`.

---

## Sesión — 2026-06-10 (refactor frontend + CSP)

### Hecho hoy
- **`script.js` dividido en 7 módulos de scope global** (split mecánico, sin cambios de lógica):
  `api.js → ui.js → collection.js → modal.js → discover.js → stats.js → app.js`, cargados con
  `<script defer>` en ese orden. `script.js` eliminado (se conservó como `.bak` durante el proceso y luego se borró).
  - Regla de oro descubierta: cualquier sentencia de nivel superior que se ejecuta al cargar
    (un `addEventListener`, `const x = el(...)`) solo puede referir globals del mismo archivo o de uno
    cargado antes. Por eso `const collectionEl` vive en `collection.js` (su delegador lo usa al cargar),
    no en `app.js` — moverlo daba `ReferenceError` en carga. Detalle documentado en `CLAUDE.md`.
  - `CLAUDE.md` "Archivos clave" reescrito con el mapa de módulos/funciones y la regla de orden de carga.
  - Commit `8279a0e`. Agentes reviewer + security + tester en verde.
- **Violaciones CSP preexistentes corregidas** (commit `145ef68`, separado):
  - `<script>` inline (flag `cinebox_visited`) → `boot.js` (en `<head>`, **sin defer**, para conservar el
    timing pre-paint del que depende `html.cinebox-visited` para evitar el flash de bienvenida).
  - Estilos inline del footer → reglas `.app-credit` en `styles.css`. Sin cambio en la cabecera CSP.
- **Pusheado a `origin/main`** (`eecc752..145ef68`) → deploy automático en Render disparado.

### Pendiente
- **Hard-refresh de la página en producción** (`Ctrl+Shift+R`) tras el deploy para soltar el `index.html`
  cacheado; entonces la consola queda limpia (sin errores de inline script/style).
- Único aviso restante esperado: el sourcemap `.map` de jsdelivr (solo debug, inofensivo, fuera de alcance).

### Para empezar la próxima sesión
1. Leer este CONTEXT.md.
2. Confirmar que el deploy de Render terminó y que la consola del navegador está limpia.

---

## Sesión anterior — 2026-06-10 (Títulos similares)

### Hecho hoy
- **Feature "Títulos similares"** implementada y verificada:
  - Nuevo endpoint `GET /api/similar` en `server.py` (auth requerida, hasta 6 resultados de TMDB)
  - `openDetail` en `script.js` acepta `hint={}` para títulos fuera de la colección
  - Sección de posters similares al fondo del modal de detalle, con listener delegado
  - Bugs encontrados y corregidos vía agentes: race condition en fetch, `.catch()` faltante, auth guard faltante
  - 29 tests automáticos pasados; 6 tests manuales de browser pendientes de verificar

### Pendiente
- **6 tests manuales** del tester: visual render del modal, click a título similar, stale guard, resultados vacíos, comportamiento sin TMDB key
- **Commit pendiente** — cambios en `server.py`, `script.js`, `styles.css` listos para commitear

### Para empezar la próxima sesión
1. Leer este CONTEXT.md.
2. Hacer commit si no se hizo.
3. Verificar los 6 tests manuales si no se hicieron.

---

## Sesión anterior — 2026-06-10

### Hecho hoy
- **Hooks instalados** en la configuración de Claude Code para este proyecto.
- **Hook de seguridad probado** y funcionando.
- Añadido este flujo de CONTEXT.md al proyecto: se creó este archivo y se añadió la instrucción a `CLAUDE.md` ("leer CONTEXT.md al inicio, actualizarlo al final").

### Pendiente
- **Los hooks requieren ejecutar el comando `/hooks` al inicio de cada sesión en Windows** para que queden activos. No se cargan automáticamente — hay que invocarlos manualmente.

### Para empezar la próxima sesión
1. Leer este CONTEXT.md.
2. Ejecutar `/hooks` para activar los hooks (requisito en Windows).
3. Continuar con la tarea que toque.

### Decisiones importantes
- Se adopta CONTEXT.md como mecanismo de traspaso de contexto entre sesiones, complementando a MEMORY.md/PROGRESS.md.
- En Windows, los hooks no persisten automáticamente entre sesiones: se asume invocación manual con `/hooks`.
