# CineBox — CLAUDE.md

Rastreador personal de películas y series. Backend Python puro (sin framework), frontend vanilla JS, PostgreSQL en Supabase.

## Stack

| Capa | Tecnología |
|------|-----------|
| Backend | Python 3, `http.server.ThreadingHTTPServer` (stdlib, sin Flask/FastAPI) |
| Base de datos | PostgreSQL vía `psycopg2`, hosteado en Supabase |
| Auth | Supabase email/password + JWT (ES256/RS256 via JWKS, fallback HS256) |
| Frontend | HTML + CSS + JS vanilla (sin build, sin bundler) |
| API externa | TMDB v3 |
| Notificaciones | Discord Incoming Webhooks (async en threads) |
| Hosting | Backend en Render |

## Archivos clave

```
server.py      — todo el backend (~660 líneas)
script.js      — todo el frontend (~1000 líneas)  ⚠️ ver abajo
index.html     — SPA de una sola página
styles.css     — estilos
.env           — secretos (no commitear)
```

---

## Barreras — leer antes de tocar nada

**`script.js` es frágil.** Cualquier cambio en ese archivo ha causado crashes en el pasado. Avisar al usuario antes de modificarlo y describir exactamente qué se va a cambiar.

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
body = self._read_json()  # ya aplica MAX_BODY (32 KB)
```

No leer `self.rfile` directamente — puede colgar el servidor con requests grandes.

### Respuestas JSON

```python
self._json(200, {"ok": True, "data": ...})
```

El método `_json` añade las cabeceras de seguridad necesarias. No usar `self.wfile.write()` directamente para respuestas JSON.

### Queries a la DB

```python
with get_db() as (conn, cur):
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

---

## Errores conocidos — no repetir

**Listeners sobre elementos que pueden no existir en el DOM.**
`document.getElementById("x").addEventListener(...)` explota si el elemento no está en la página actual. Usar siempre `const el = document.getElementById("x"); if (el) el.addEventListener(...)`.

**CSP bloqueando CDNs externos.**
La cabecera `Content-Security-Policy` del servidor controla qué scripts/estilos se cargan. Al añadir librerías externas (supabase-js, etc.), actualizar la CSP en `server.py` o el script no cargará en producción.

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
| `SUPABASE_JWT_SECRET` | Fallback HS256 para JWT | No |
| `SUPABASE_SERVICE_KEY` | No usado en runtime (reservado) | No |
| `TMDB_API_KEY` | Sin él, search/discover/details no funcionan | No |
| `DISCORD_WEBHOOK_VISTA` | Webhook cuando se marca como vista | No |
| `DISCORD_WEBHOOK_PENDIENTE` | Webhook cuando se añade a pendientes | No |
| `DISCORD_WEBHOOK_URL` | Webhook genérico de fallback | No |
| `DISCORD_OWNER_ID` | UUID Supabase — filtra notificaciones al owner | No |
| `PORT` | Puerto del servidor | No (default: 8000) |
