#!/usr/bin/env python3
"""Cineteca — tu lista de películas y series, con puntuación y detalle.

Backend con Postgres (psycopg2) vía DATABASE_URL y autenticación JWT
(Supabase). Cada endpoint /api/movies requiere un token válido;
el user_id se extrae del token, nunca del cliente.

Ejecutar:  python server.py
Config en .env:
    DATABASE_URL=...
    SUPABASE_JWT_SECRET=...   (Settings → API → JWT Secret en Supabase)
    TMDB_API_KEY=...
    DISCORD_WEBHOOK_URL=...
"""
import json
import os
import re
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt as pyjwt          # PyJWT
import psycopg2
import psycopg2.extras

BASE_DIR = Path(__file__).resolve().parent
HOST, PORT = "0.0.0.0", int(os.environ.get("PORT", 8000))
BLOCKED    = (".env", ".py", ".pyc")

TMDB_IMG  = "https://image.tmdb.org/t/p/w342"
TMDB_LOGO = "https://image.tmdb.org/t/p/w45"

PLATFORMS = ("Netflix", "HBO Max", "Prime Video", "Disney+", "Movistar+", "Cine", "Otra")

TMDB_GENRES = {
    28: "Acción", 12: "Aventura", 16: "Animación", 35: "Comedia",
    80: "Crimen", 99: "Documental", 18: "Drama", 10751: "Familia",
    14: "Fantasía", 36: "Historia", 27: "Terror", 10402: "Música",
    9648: "Misterio", 10749: "Romance", 878: "Ciencia ficción",
    10770: "Película de TV", 53: "Suspense", 10752: "Bélica", 37: "Western",
    10759: "Acción y aventura", 10762: "Infantil", 10763: "Noticias",
    10764: "Reality", 10765: "Ciencia ficción y fantasía",
    10766: "Telenovela", 10767: "Late night", 10768: "Guerra y política",
}


# ── Configuración ─────────────────────────────────────────────────────────────

def load_env():
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


# ── Base de datos ─────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    """Abre conexión Postgres, hace commit al salir o rollback en error."""
    con = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    """Verifica que DATABASE_URL es accesible al arrancar."""
    with get_db() as cur:
        cur.execute("SELECT 1")


# ── JWT ───────────────────────────────────────────────────────────────────────

def verify_jwt(token: str):
    """Verifica el token con SUPABASE_JWT_SECRET (HS256).
    Devuelve el UUID del usuario (sub) o None si es inválido."""
    secret = os.environ.get("SUPABASE_JWT_SECRET", "")
    if not secret:
        return None
    try:
        payload = pyjwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"verify_aud": False},   # Supabase pone aud="authenticated"
        )
        return payload.get("sub")           # UUID del usuario
    except pyjwt.PyJWTError:
        return None


# ── Utilidades ────────────────────────────────────────────────────────────────

def parse_watched_at(value):
    """Valida fecha opcional YYYY-MM-DD. Devuelve string ISO o None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError("watched_at debe tener formato YYYY-MM-DD o estar vacío")


def webhook_for(status):
    key = "DISCORD_WEBHOOK_VISTA" if status == "vista" else "DISCORD_WEBHOOK_PENDIENTE"
    return os.environ.get(key, "").strip() or os.environ.get("DISCORD_WEBHOOK_URL", "").strip()


def notify_discord(title, year, status, media_type, poster_url=""):
    url = webhook_for(status)
    if not url:
        return
    kind    = "Serie" if media_type == "tv" else "Película"
    icon    = "📺"    if media_type == "tv" else "🎬"
    destino = {"pendiente": "Por ver", "viendo": "Viendo",
               "vista": "Vistas", "abandonada": "Abandonada"}.get(status, status)
    color   = {"vista": 0x45D667, "viendo": 0x4B86FF,
               "abandonada": 0x822832}.get(status, 0xE6B13E)
    embed = {
        "title":       f"{icon} {title} ({year or 's/f'})",
        "description": f"{kind} en **{destino}**",
        "color":       color,
    }
    if poster_url:
        embed["image"] = {"url": poster_url}
    try:
        data = json.dumps({"embeds": [embed]}).encode("utf-8")
        req  = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "User-Agent":   "Cineteca-Webhook/1.0 (+local)",
        })
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


# ── Handler HTTP ──────────────────────────────────────────────────────────────

class Handler(SimpleHTTPRequestHandler):

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _qs(self):
        return urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")

    def _get_user_id(self):
        """Extrae y verifica el JWT del header Authorization.
        Devuelve el UUID del usuario o None si falta / es inválido."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return verify_jwt(auth[7:])

    def _tmdb(self, path, extra=None):
        if not os.environ.get("TMDB_API_KEY", "").strip():
            return None
        params = {"api_key": os.environ["TMDB_API_KEY"].strip(), "language": "es-ES"}
        if extra:
            params.update(extra)
        url = f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())

    # ── GET ───────────────────────────────────────────────────────────────────

    def do_GET(self):
        path         = self.path.split("?", 1)[0]
        decoded_path = urllib.parse.unquote(path)
        if path == "/health":       return self._json(200, {"ok": True, "status": "up"})
        if path == "/api/config":
            raw = os.environ.get("SUPABASE_URL", "")
            # Normaliza la URL base: elimina doble-esquema y /rest/v1 si vienen del .env
            import re as _re
            raw = _re.sub(r'^https:https://', 'https://', raw)
            raw = _re.sub(r'/rest/v1/?$', '', raw.rstrip('/'))
            return self._json(200, {
                "supabase_url":      raw,
                "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
            })
        if path == "/api/movies":   return self._list_movies()
        if path == "/api/search":   return self._search()
        if path == "/api/trending": return self._trending()
        if path == "/api/details":  return self._details()
        parts = [p for p in decoded_path.split("/") if p]
        if (decoded_path.lower().endswith(BLOCKED)
                or any(p.startswith(".") for p in parts)):
            return self._json(404, {"ok": False, "error": "No encontrado"})
        return super().do_GET()

    def _list_movies(self):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        with get_db() as cur:
            cur.execute(
                "SELECT * FROM movies WHERE user_id = %s ORDER BY created_at DESC",
                (user_id,))
            rows = cur.fetchall()
        self._json(200, {"ok": True, "movies": [dict(r) for r in rows]})

    def _search(self):
        # Proxy TMDB — no toca la BD, no requiere auth
        query = (self._qs().get("q", [""])[0]).strip()
        if not query:
            return self._json(400, {"ok": False, "error": "Falta el término de búsqueda"})
        if not os.environ.get("TMDB_API_KEY", "").strip():
            return self._json(200, {"ok": False, "needs_key": True,
                                    "error": "Sin TMDB_API_KEY."})
        try:
            mv = (self._tmdb("/search/movie", {"query": query, "include_adult": "false"}) or {}).get("results", [])
            tv = (self._tmdb("/search/tv",    {"query": query, "include_adult": "false"}) or {}).get("results", [])
        except Exception:
            return self._json(502, {"ok": False, "error": "No se pudo consultar TMDB."})

        def pack(items, mt):
            out = []
            for m in items[:12]:
                poster = m.get("poster_path")
                out.append({
                    "tmdb_id":    m.get("id"),
                    "media_type": mt,
                    "title":      m.get("title") or m.get("name") or "Sin título",
                    "year":       (m.get("release_date") or m.get("first_air_date") or "")[:4],
                    "poster_url": (TMDB_IMG + poster) if poster else "",
                    "genre_ids":  m.get("genre_ids", []),
                })
            return out

        self._json(200, {"ok": True, "results": pack(mv, "movie") + pack(tv, "tv")})

    def _trending(self):
        if not os.environ.get("TMDB_API_KEY", "").strip():
            return self._json(200, {"ok": False, "needs_key": True})
        try:
            data = self._tmdb("/trending/all/week") or {}
        except Exception:
            return self._json(502, {"ok": False, "error": "No se pudo consultar TMDB."})
        results = []
        for m in data.get("results", []):
            mt = m.get("media_type")
            if mt not in ("movie", "tv"):
                continue
            poster = m.get("poster_path")
            results.append({
                "tmdb_id":    m.get("id"),
                "media_type": mt,
                "title":      m.get("title") or m.get("name") or "Sin título",
                "year":       (m.get("release_date") or m.get("first_air_date") or "")[:4],
                "poster_url": (TMDB_IMG + poster) if poster else "",
                "genre_ids":  m.get("genre_ids", []),
            })
            if len(results) >= 18:
                break
        self._json(200, {"ok": True, "results": results})

    def _details(self):
        q   = self._qs()
        tid = (q.get("id",   [""])[0]).strip()
        mt  = (q.get("type", ["movie"])[0]).strip()
        if not tid.isdigit() or mt not in ("movie", "tv"):
            return self._json(400, {"ok": False, "error": "Parámetros inválidos"})
        if not os.environ.get("TMDB_API_KEY", "").strip():
            return self._json(200, {"ok": False, "needs_key": True})
        try:
            d = self._tmdb(f"/{mt}/{tid}", {"append_to_response": "videos,credits,watch/providers"})
        except Exception:
            return self._json(502, {"ok": False, "error": "No se pudo consultar TMDB."})
        trailer = ""
        for v in (d.get("videos", {}) or {}).get("results", []):
            if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser"):
                trailer = "https://www.youtube.com/watch?v=" + v.get("key", "")
                break
        if mt == "tv":
            dir_label = "Creación"
            directors = [c.get("name") for c in d.get("created_by", []) if c.get("name")]
        else:
            dir_label = "Dirección"
            directors = [c.get("name") for c in (d.get("credits", {}) or {}).get("crew", [])
                         if c.get("job") == "Director"]
        cast      = [c.get("name") for c in (d.get("credits", {}) or {}).get("cast", [])[:6] if c.get("name")]
        wp_es     = (d.get("watch/providers") or {}).get("results", {}).get("ES", {})
        providers = [
            {"name": p["provider_name"], "logo": TMDB_LOGO + p["logo_path"]}
            for p in wp_es.get("flatrate", [])
            if p.get("logo_path") and p.get("provider_name")
        ]
        runtime = d.get("runtime")
        if mt == "tv" and not runtime:
            ert     = d.get("episode_run_time") or []
            runtime = ert[0] if ert else None
        self._json(200, {"ok": True, "details": {
            "overview":       d.get("overview") or "Sin sinopsis disponible.",
            "genres":         [g["name"] for g in d.get("genres", [])],
            "runtime":        runtime,
            "vote_average":   round(d.get("vote_average") or 0, 1),
            "trailer":        trailer,
            "dir_label":      dir_label,
            "directors":      directors,
            "cast":           cast,
            "providers":      providers,
            "providers_link": wp_es.get("link", ""),
        }})

    # ── POST ──────────────────────────────────────────────────────────────────

    def do_POST(self):
        if self.path != "/api/movies":
            return self._json(404, {"ok": False, "error": "Ruta no encontrada"})
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "JSON inválido"})
        title = str(data.get("title", "")).strip()
        if not title:
            return self._json(400, {"ok": False, "error": "El título es obligatorio"})
        media_type = data.get("media_type") if data.get("media_type") in ("movie", "tv") else "movie"
        status     = data.get("status") if data.get("status") in ("pendiente", "viendo", "vista", "abandonada") else "pendiente"
        try:
            watched_at = parse_watched_at(data.get("watched_at"))
        except ValueError as exc:
            return self._json(400, {"ok": False, "error": str(exc)})
        if status == "vista" and not watched_at:
            watched_at = date.today().isoformat()
        tmdb_id = data.get("tmdb_id")
        if tmdb_id not in (None, ""):
            try:
                tmdb_id = int(tmdb_id)
            except (TypeError, ValueError):
                return self._json(400, {"ok": False, "error": "tmdb_id inválido"})
        else:
            tmdb_id = None
        year   = str(data.get("year", "")).strip()
        poster = str(data.get("poster_url", "")).strip()
        genres = ", ".join(
            TMDB_GENRES[gid] for gid in (data.get("genre_ids") or [])
            if isinstance(gid, int) and gid in TMDB_GENRES
        ) or None

        with get_db() as cur:
            if tmdb_id:
                cur.execute(
                    "SELECT 1 FROM movies WHERE tmdb_id = %s AND media_type = %s AND user_id = %s",
                    (tmdb_id, media_type, user_id))
                if cur.fetchone():
                    return self._json(409, {"ok": False, "duplicate": True,
                                            "error": f"«{title}» ya está en tu cineteca."})
            cur.execute(
                "INSERT INTO movies "
                "(user_id, tmdb_id, media_type, title, year, poster_url, status, "
                " rating, created_at, watched_at, genres) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (user_id, tmdb_id, media_type, title, year, poster,
                 status, None, datetime.now(timezone.utc).isoformat(), watched_at, genres))
            new_id = cur.fetchone()["id"]

        notify_discord(title, year, status, media_type, poster)
        self._json(201, {"ok": True, "id": new_id})

    # ── PATCH ─────────────────────────────────────────────────────────────────

    def do_PATCH(self):
        m = re.match(r"^/api/movies/(\d+)$", self.path)
        if not m:
            return self._json(404, {"ok": False, "error": "Ruta no encontrada"})
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "JSON inválido"})
        movie_id   = int(m.group(1))
        fields, values = [], []
        new_status = None

        if data.get("status") in ("pendiente", "viendo", "vista", "abandonada"):
            new_status = data["status"]
            fields.append("status = %s"); values.append(new_status)
        if "rating" in data:
            r = data["rating"]
            if r is not None and (not isinstance(r, int) or not 1 <= r <= 5):
                return self._json(400, {"ok": False, "error": "rating debe ser 1-5 o null"})
            fields.append("rating = %s"); values.append(r)
        if "note" in data:
            if data["note"] is not None and not isinstance(data["note"], str):
                return self._json(400, {"ok": False, "error": "note debe ser texto o null"})
            note = "" if data["note"] is None else data["note"].strip()
            if len(note) > 500:
                return self._json(400, {"ok": False, "error": "La nota no puede superar 500 caracteres"})
            fields.append("note = %s"); values.append(note)
        if "watched_at" in data:
            try:
                watched_at = parse_watched_at(data.get("watched_at"))
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            fields.append("watched_at = %s"); values.append(watched_at)
        if "platform" in data:
            v = data["platform"]
            if v is not None and v not in PLATFORMS:
                return self._json(400, {"ok": False, "error": "Plataforma no válida"})
            fields.append("platform = %s"); values.append(v)
        for field in ("current_season", "current_episode"):
            if field in data:
                v = data[field]
                if v is not None and (not isinstance(v, int) or v < 1):
                    return self._json(400, {"ok": False, "error": f"{field} debe ser un entero positivo o null"})
                fields.append(f"{field} = %s"); values.append(v)
        if not fields:
            return self._json(400, {"ok": False, "error": "Nada que actualizar"})

        row = None
        with get_db() as cur:
            if new_status == "vista" and "watched_at" not in data:
                cur.execute(
                    "SELECT watched_at FROM movies WHERE id = %s AND user_id = %s",
                    (movie_id, user_id))
                current = cur.fetchone()
                if current and not current["watched_at"]:
                    fields.append("watched_at = %s")
                    values.append(date.today().isoformat())
            values.extend([movie_id, user_id])
            cur.execute(
                f"UPDATE movies SET {', '.join(fields)} WHERE id = %s AND user_id = %s",
                values)
            if cur.rowcount == 0:
                return self._json(404, {"ok": False, "error": "No encontrada"})
            if new_status:
                cur.execute(
                    "SELECT title, year, poster_url, media_type FROM movies "
                    "WHERE id = %s AND user_id = %s",
                    (movie_id, user_id))
                row = cur.fetchone()

        if new_status and row:
            notify_discord(row["title"], row["year"], new_status, row["media_type"], row["poster_url"])
        self._json(200, {"ok": True})

    # ── DELETE ────────────────────────────────────────────────────────────────

    def do_DELETE(self):
        m = re.match(r"^/api/movies/(\d+)$", self.path)
        if not m:
            return self._json(404, {"ok": False, "error": "Ruta no encontrada"})
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        with get_db() as cur:
            cur.execute(
                "DELETE FROM movies WHERE id = %s AND user_id = %s",
                (int(m.group(1)), user_id))
            if cur.rowcount == 0:
                return self._json(404, {"ok": False, "error": "No encontrada"})
        self._json(200, {"ok": True})

    def log_message(self, *args):
        pass


# ── Arranque ──────────────────────────────────────────────────────────────────

def main():
    load_env()
    init_db()
    handler = partial(Handler, directory=str(BASE_DIR))
    with ThreadingHTTPServer((HOST, PORT), handler) as httpd:
        tmdb = "sí" if os.environ.get("TMDB_API_KEY") else "no (modo manual)"
        hook = "sí" if (os.environ.get("DISCORD_WEBHOOK_PENDIENTE")
                        or os.environ.get("DISCORD_WEBHOOK_VISTA")
                        or os.environ.get("DISCORD_WEBHOOK_URL")) else "no"
        print(f"Cineteca en http://{HOST}:{PORT}  ·  TMDB: {tmdb}  ·  Discord: {hook}  (Ctrl+C para parar)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServidor detenido.")


if __name__ == "__main__":
    main()
