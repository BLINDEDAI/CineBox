# CONTEXT.md — CineBox

Estado de trabajo entre sesiones. Se lee al inicio de cada sesión y se actualiza al final.

---

## Última sesión — 2026-06-10 (actualizado)

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
