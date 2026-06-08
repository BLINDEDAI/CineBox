# Cineteca — tu lista de películas

App local para guardar películas vistas / por ver, puntuarlas y descubrir qué ver. HTML/CSS/JS + backend Python (stdlib, sin dependencias). Busca películas reales vía la API de TMDB (proxy en el servidor; la clave nunca llega al navegador).

## Cómo ejecutar

```
python server.py
```
Abrir http://127.0.0.1:8000

Funciona desde el primer momento en **modo manual** (añadir películas a mano). Para **buscar películas reales con pósters**, configura la clave de TMDB:

1. Crea cuenta gratis en https://www.themoviedb.org/signup
2. Settings → API → solicita una **API Key** (tipo *Developer*).
3. Copia `.env.example` a `.env` y pega la clave en `TMDB_API_KEY=`.
4. Reinicia `server.py`.

## Qué hace

- Buscar películas (TMDB) y añadirlas como "Por ver" o "Vista".
- Añadir películas manualmente (sin clave).
- Cambiar estado, puntuar de 1 a 5 estrellas, eliminar.
- Registrar y editar la fecha de visionado (`YYYY-MM-DD`) al marcar como vista.
- Filtrar por Todas / Por ver / Vistas.
- **¿Qué veo hoy?**: elige al azar algo de tu lista «Por ver» (respeta el filtro de tipo) y lo resalta.
- Guarda todo en `cineteca.sqlite` (local).
- Exportar colección a JSON (`cineteca-export-YYYY-MM-DD.json`).
- Importar colección desde JSON exportado previamente.

## Avisos en Discord (opcional)

Para que se anuncie en un canal de Discord al añadir una película:
1. En Discord: Ajustes del canal → Integraciones → Webhooks → Nuevo webhook → Copiar URL.
2. Pega la URL en `.env` como `DISCORD_WEBHOOK_URL=...`.
3. Reinicia. Cada alta enviará un aviso al canal.

Úsalo solo en un servidor tuyo o con permiso del dueño.

## Seguridad / notas

- La `TMDB_API_KEY` vive solo en `.env` local; el servidor no la expone al navegador ni sirve `.env`, `.py` ni `.sqlite` por HTTP.
- Piloto **local**, no producción. Sin deploy.
- `cineteca.sqlite` se crea solo y guarda tu lista.

## Endpoints

- `GET /health`
- `GET /api/movies` · `POST /api/movies` · `PATCH /api/movies/{id}` · `DELETE /api/movies/{id}`
- `GET /api/search?q=...` (proxy TMDB)
- `GET /api/export` (descarga colección en JSON)
- `POST /api/import` (importa colección desde JSON)
