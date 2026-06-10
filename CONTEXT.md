# CONTEXT.md — CineBox

Estado de trabajo entre sesiones. Se lee al inicio de cada sesión y se actualiza al final.

---

## Última sesión — 2026-06-10

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
