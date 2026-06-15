# CONTEXT.md — CineBox

Estado de trabajo entre sesiones. Se lee al inicio de cada sesión y se actualiza al final.

---

## Estado actual (snapshot) — 2026-06-15

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

### Rendimiento vigente
**Caché TTL en memoria dentro de `_tmdb()`** (2026-06-14): cachea las respuestas de TMDB para los
5 endpoints (search/trending/discover/details/similar). Clave = `(path, params sin api_key)`; datos
de TMDB independientes del usuario (no cruza datos entre cuentas). `TMDB_CACHE_TTL` env (default 900s,
`0` desactiva), `TMDB_CACHE_MAX` 500 con **cap duro** (2026-06-15): purga expiradas y, si sigue lleno,
desaloja las más antiguas FIFO → nunca supera 500 entradas. Errores de red no se cachean;
sin clave TMDB devuelve `None` antes de tocar la caché. El rate limiting sigue contando por usuario aun
en hit.

### Pendiente abierto (decisiones del usuario)
- **Self-host de supabase-js** para poder fijar versión + SRI (hoy usa `@2` flotante, por eso NO se
  añadió SRI). Sin decidir.
- **Versionar `.claude/`** (hooks) en el repo o dejarlo local. Sin decidir.
- **Verificación manual en producción** tras el último deploy (login+recarga, género desde modal, logout).

#### Hallazgos de la auditoría de DB vía MCP (2026-06-10) — sesiones aparte
1. **Activar Leaked Password Protection** en Supabase Auth settings (1 clic, manual del usuario).
2. **Decidir columna `total_seasons`**: está muerta (0/105 filas, el `PATCH` nunca la escribe) → cablearla
   en el `PATCH` para progreso de series, o `DROP COLUMN`. Cambio de DB → sesión nueva con plan.
3. **Alinear default de `status`** de `'pending'` (inglés) a `'pendiente'` (la app usa estados en español;
   hoy inofensivo porque `server.py` siempre setea `status`). Cambio de DB menor → sesión nueva.
4. **`REVOKE EXECUTE` en `rls_auto_enable`** para `anon`/`authenticated` (higiene, baja prioridad; es un
   event-trigger que solo *activa* RLS, severidad real baja).

---

## Sesión — 2026-06-15 (cap duro de la caché TMDB)

Quick-fix. Disparada por "analiza CineBox y qué recomiendas" → del diagnóstico se eligió el
follow-up #3 (cap blando de `_tmdb_cache`) por ser el único bug latente con riesgo de runtime.

### Hecho hoy
- **Cap duro en `_tmdb()`** (`server.py`): tras la purga oportunista de expiradas, un bucle FIFO
  desaloja las más antiguas hasta bajar del tope. El caché ya no puede crecer por encima de
  `TMDB_CACHE_MAX` (500) aunque todas las entradas estén vivas. +2 líneas, sin imports nuevos.
  Cierra la fuga de memoria latente (queries de `/api/search` = entrada no acotada).
- **Test de regresión** `test_cache_size_is_hard_capped` en `tests/test_tmdb_cache.py`: llena el
  caché al tope con entradas vivas, mete una más, assertea `len == MAX` y que la más antigua se
  desaloja (FIFO). Suite total **40/40** verde. `ruff` verde.

### Decisiones / notas
- **Tratado como quick-fix** (no SDD): cambio en memoria, no toca DB/auth/PII/dinero/perímetro →
  encaja en el waiver de `quick-fix-baseline.md §5`. El pipeline de agentes lo orquesta `/build-plan`
  (no hay en quick-fix); se compensó con `/code-review` (0 hallazgos) + `/security-review` (sin vulns,
  de hecho *mejora* la postura: cierra un vector de agotamiento de memoria).
- **Nota no-bloqueante** (del review): si `TMDB_CACHE_MAX` se hiciera configurable por env y valiera
  `0`, el `while` con `next(iter(...))` daría `StopIteration`. Hoy es constante hardcodeada 500 → seguro.
  Guarda defensiva opcional: `while _tmdb_cache and len(...) >= TMDB_CACHE_MAX`. No aplicada.

### Para empezar la próxima sesión
1. Leer este CONTEXT.md.
2. **Verificación manual en producción** sigue pendiente (arrastrada del deploy 2026-06-14).
3. Deuda estructural restante: reconciliación de schema (#2 `total_seasons` muerta + #3 default
   `status` desalineado) → esta sí es migración → `/create-specs` + `/build-plan` (sesión nueva).

---

## Sesión — 2026-06-14 (caché TTL para TMDB)

Mejora de rendimiento. Disparada por una petición de "¿qué habría que mejorar?" → se eligió la #1
del diagnóstico (caché TMDB) por mejor relación valor/riesgo.

### Hecho hoy
- **Caché TTL en memoria a nivel de `_tmdb()`** (ver snapshot → "Rendimiento vigente"). Punto único,
  transparente para los 5 endpoints; tests stubbean `_tmdb` así que no se vieron afectados.
- **6 tests nuevos** en `tests/test_tmdb_cache.py`: hit evita 2ª llamada, params distintos = entradas
  separadas, expiración tras TTL, TTL=0 desactiva, sin clave no cachea, error de red no se cachea.
  Suite total **23/23** (17 previos + 6).
- **Flujo git completo**: rama `feature/discover/tmdb-cache` → commit `8748587` → `/code-review` (1
  hallazgo baja severidad, no bloqueante) + `/security-review` (sin vulns) → merge `--no-ff` a `main`
  (`50874e1`) → push (deploy Render disparado) → rama borrada (local + origin).

### Decisiones / notas
- **Bypass documentado**: el pipeline de agentes (`reviewer→security→tester→dod-checker`) de `CLAUDE.md`
  NO se ejecutó en el commit, a petición explícita del usuario; se compensó con `/code-review` +
  `/security-review` antes del merge. Queda constancia en el mensaje de commit `8748587`.
- **gitleaks no instalado** → el pre-push salta el barrido de secretos (fail-open). Este push no
  contiene secretos. Pendiente: `choco install gitleaks` para cubrir `main` a futuro.
- **Follow-up**: `TMDB_CACHE_MAX` es cap blando (solo purga expiradas); con muchas claves distintas
  dentro del TTL (las queries de `/api/search` son entrada no acotada) la caché puede crecer por encima
  de 500 hasta que expiren. Bajo riesgo en monousuario. Fix futuro: cap duro FIFO/`OrderedDict`.

### Para empezar la próxima sesión
1. Leer este CONTEXT.md.
2. **Verificación manual en producción** tras el deploy: `/health` 200; search/discover/details OK;
   una 2ª carga de "Descubrir" no debería re-pegar a TMDB (caché funcionando).
3. Si interesa: cap duro de la caché (follow-up), o seguir con otra mejora del diagnóstico
   (self-host supabase-js + SRI, hallazgos de DB, auditoría frontend).

---

## Sesión — 2026-06-10 (adopción de buenas prácticas IA + MCP de Supabase)

Sesión de proceso/infra, no de código de la app. Disparada por un mini-curso de buenas prácticas IA.

### Hecho hoy
- **Convención de CONTEXT.md** documentada en `CLAUDE.md` (`32ef010`): snapshot arriba (in place) +
  log de sesiones append-only abajo; al cerrar, snapshot primero. Y se añadió el propio snapshot (`3c99c2f`).
- **Regla de sesiones separadas para bugs no triviales** (`492d00c`): adoptada en `CLAUDE.md` (sección
  Agentes) y en el `tester` global (`~/.claude/agents/tester.md`, fuera del repo) que ahora emite un
  "fix prompt" autocontenido. Bug no trivial → no se arregla inline, se pasa a sesión nueva.
- **MCP de Supabase de solo-lectura montado** (config local en `~/.claude.json`, scope `local`, **no
  commiteado**): servidor hospedado `mcp.supabase.com` con `read_only=true` + `project_ref` + OAuth.
  No hay PAT ni secreto en ningún archivo. Reemplaza el "no puedo ver el estado real de la DB".
- **Primera auditoría de DB con el MCP** (read-only): esquema real de `public.movies` (17 cols, RLS on,
  FK a `auth.users`) **casa sin drift** con lo que asume `server.py`. 105 filas, estados todos válidos.
  Hallados 4 pendientes (ver snapshot → "Hallazgos de la auditoría de DB"): `total_seasons` muerta,
  default `status` desalineado, 2 advisors de seguridad (`rls_auto_enable` expuesta, leaked-password off).

### Decisiones
- MCP elegido: **hospedado + OAuth** (no el npx local con PAT) por seguridad — nada sensible en disco.
- Los 4 hallazgos de DB **no se arreglan en esta sesión** (coherente con la regla recién adoptada): quedan
  registrados como pendientes para sesiones enfocadas. Ninguno es crítico.

### Para empezar la próxima sesión
1. Leer este CONTEXT.md.
2. Si se aborda un hallazgo de DB: cambios de DB (#2, #3, #4) requieren plan + confirmación antes de tocar.
3. El MCP de Supabase ya está disponible (read-only) para inspeccionar estado real cuando haga falta.

---

## Sesión — 2026-06-10 (auditoría de seguridad/calidad)

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
