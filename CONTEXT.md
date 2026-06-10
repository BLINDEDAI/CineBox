# CONTEXT.md — CineBox

Estado de trabajo entre sesiones. Se lee al inicio de cada sesión y se actualiza al final.

---

## Última sesión — 2026-06-10 (refactor frontend + CSP)

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
