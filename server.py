#!/usr/bin/env python3
"""Cineteca — tu lista de películas y series, con puntuación y detalle.

Stdlib pura (sin dependencias). Sirve el frontend, guarda tu lista en
cineteca.sqlite, hace de proxy a TMDB (búsqueda multi + detalle con
reparto/dirección) y, si se configura, avisa por webhook de Discord.

Piloto local, NO producción.  Ejecutar:  python server.py
Config opcional en .env:
    TMDB_API_KEY=...            (búsqueda real)
    DISCORD_WEBHOOK_URL=...     (avisos en Discord)
"""
import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / os.environ.get("CINETECA_DB", "cineteca.sqlite")
HOST, PORT = "127.0.0.1", 8000
BLOCKED = (".env", ".py", ".pyc", ".sqlite")
TMDB_IMG = "https://image.tmdb.org/t/p/w342"
TMDB_LOGO = "https://image.tmdb.org/t/p/w45"


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


def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS movies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id INTEGER,
                media_type TEXT NOT NULL DEFAULT 'movie',
                title TEXT NOT NULL,
                year TEXT,
                poster_url TEXT,
                status TEXT NOT NULL DEFAULT 'pendiente',
                rating INTEGER,
                note TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        # Migración: añadir media_type si la tabla es antigua.
        cols = [r[1] for r in con.execute("PRAGMA table_info(movies)")]
        if "media_type" not in cols:
            con.execute("ALTER TABLE movies ADD COLUMN media_type TEXT NOT NULL DEFAULT 'movie'")
        if "note" not in cols:
            con.execute("ALTER TABLE movies ADD COLUMN note TEXT")
        if "watched_at" not in cols:
            con.execute("ALTER TABLE movies ADD COLUMN watched_at TEXT")


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
    # Canal según estado; si no hay específico, usa el genérico como fallback.
    key = "DISCORD_WEBHOOK_VISTA" if status == "vista" else "DISCORD_WEBHOOK_PENDIENTE"
    return os.environ.get(key, "").strip() or os.environ.get("DISCORD_WEBHOOK_URL", "").strip()


def notify_discord(title, year, status, media_type, poster_url=""):
    url = webhook_for(status)
    if not url:
        return
    kind = "Serie" if media_type == "tv" else "Película"
    icon = "📺" if media_type == "tv" else "🎬"
    destino = "Vistas" if status == "vista" else "Por ver"
    embed = {
        "title": f"{icon} {title} ({year or 's/f'})",
        "description": f"{kind} en **{destino}**",
        "color": 0x45D667 if status == "vista" else 0xE6B13E,
    }
    if poster_url:
        embed["image"] = {"url": poster_url}
    try:
        data = json.dumps({"embeds": [embed]}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "User-Agent": "Cineteca-Webhook/1.0 (+local)",  # Discord bloquea el UA por defecto de urllib (403)
        })
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


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

    def _tmdb(self, path, extra=None):
        if not os.environ.get("TMDB_API_KEY", "").strip():
            return None
        params = {"api_key": os.environ["TMDB_API_KEY"].strip(), "language": "es-ES"}
        if extra:
            params.update(extra)
        url = f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        decoded_path = urllib.parse.unquote(path)
        if path == "/health":
            return self._json(200, {"ok": True, "status": "up"})
        if path == "/api/movies":
            return self._list_movies()
        if path == "/api/search":
            return self._search()
        if path == "/api/trending":
            return self._trending()
        if path == "/api/details":
            return self._details()
        parts = [p for p in decoded_path.split("/") if p]
        lower_path = decoded_path.lower()
        if (lower_path.endswith(BLOCKED)
                or any(p.startswith(".") for p in parts)
                or any(p == DB_PATH.name for p in parts)):
            return self._json(404, {"ok": False, "error": "No encontrado"})
        return super().do_GET()

    def _list_movies(self):
        with sqlite3.connect(DB_PATH) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM movies ORDER BY created_at DESC").fetchall()
        self._json(200, {"ok": True, "movies": [dict(r) for r in rows]})

    def _search(self):
        query = (self._qs().get("q", [""])[0]).strip()
        if not query:
            return self._json(400, {"ok": False, "error": "Falta el término de búsqueda"})
        if not os.environ.get("TMDB_API_KEY", "").strip():
            return self._json(200, {"ok": False, "needs_key": True,
                                    "error": "Sin TMDB_API_KEY: usa el modo manual o configura la clave (ver README)."})
        try:
            mv = (self._tmdb("/search/movie", {"query": query, "include_adult": "false"}) or {}).get("results", [])
            tv = (self._tmdb("/search/tv", {"query": query, "include_adult": "false"}) or {}).get("results", [])
        except Exception:
            return self._json(502, {"ok": False, "error": "No se pudo consultar TMDB."})

        def pack(items, mt):
            out = []
            for m in items[:12]:
                poster = m.get("poster_path")
                out.append({
                    "tmdb_id": m.get("id"),
                    "media_type": mt,
                    "title": m.get("title") or m.get("name") or "Sin título",
                    "year": (m.get("release_date") or m.get("first_air_date") or "")[:4],
                    "poster_url": (TMDB_IMG + poster) if poster else "",
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
                "tmdb_id": m.get("id"),
                "media_type": mt,
                "title": m.get("title") or m.get("name") or "Sin título",
                "year": (m.get("release_date") or m.get("first_air_date") or "")[:4],
                "poster_url": (TMDB_IMG + poster) if poster else "",
            })
            if len(results) >= 18:
                break
        self._json(200, {"ok": True, "results": results})

    def _details(self):
        q = self._qs()
        tid = (q.get("id", [""])[0]).strip()
        mt = (q.get("type", ["movie"])[0]).strip()
        if not tid.isdigit() or mt not in ("movie", "tv"):
            return self._json(400, {"ok": False, "error": "Parámetros inválidos"})
        if not os.environ.get("TMDB_API_KEY", "").strip():
            return self._json(200, {"ok": False, "needs_key": True})
        try:
            d = self._tmdb(f"/{mt}/{tid}", {"append_to_response": "videos,credits,watch/providers"})
        except Exception:
            return self._json(502, {"ok": False, "error": "No se pudo consultar TMDB."})
        # Tráiler
        trailer = ""
        for v in (d.get("videos", {}) or {}).get("results", []):
            if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser"):
                trailer = "https://www.youtube.com/watch?v=" + v.get("key", "")
                break
        # Dirección / creadores
        if mt == "tv":
            dir_label = "Creación"
            directors = [c.get("name") for c in d.get("created_by", []) if c.get("name")]
        else:
            dir_label = "Dirección"
            directors = [c.get("name") for c in (d.get("credits", {}) or {}).get("crew", [])
                         if c.get("job") == "Director"]
        # Reparto (top 6)
        cast = [c.get("name") for c in (d.get("credits", {}) or {}).get("cast", [])[:6] if c.get("name")]
        wp_es = (d.get("watch/providers") or {}).get("results", {}).get("ES", {})
        providers = [
            {"name": p["provider_name"], "logo": TMDB_LOGO + p["logo_path"]}
            for p in wp_es.get("flatrate", [])
            if p.get("logo_path") and p.get("provider_name")
        ]
        providers_link = wp_es.get("link", "")
        runtime = d.get("runtime")
        if mt == "tv" and not runtime:
            ert = d.get("episode_run_time") or []
            runtime = ert[0] if ert else None
        self._json(200, {"ok": True, "details": {
            "overview": d.get("overview") or "Sin sinopsis disponible.",
            "genres": [g["name"] for g in d.get("genres", [])],
            "runtime": runtime,
            "vote_average": round(d.get("vote_average") or 0, 1),
            "trailer": trailer,
            "dir_label": dir_label,
            "directors": directors,
            "cast": cast,
            "providers": providers,
            "providers_link": providers_link,
        }})

    # ---- POST ----
    def do_POST(self):
        if self.path != "/api/movies":
            return self._json(404, {"ok": False, "error": "Ruta no encontrada"})
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "JSON inválido"})
        title = str(data.get("title", "")).strip()
        if not title:
            return self._json(400, {"ok": False, "error": "El título es obligatorio"})
        media_type = data.get("media_type") if data.get("media_type") in ("movie", "tv") else "movie"
        status = data.get("status") if data.get("status") in ("pendiente", "vista") else "pendiente"
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
        year = str(data.get("year", "")).strip()
        poster = str(data.get("poster_url", "")).strip()
        with sqlite3.connect(DB_PATH) as con:
            if tmdb_id:
                exists = con.execute(
                    "SELECT 1 FROM movies WHERE tmdb_id = ? AND media_type = ?",
                    (tmdb_id, media_type)).fetchone()
                if exists:
                    return self._json(409, {"ok": False, "duplicate": True,
                                            "error": f"«{title}» ya está en tu cineteca."})
            cur = con.execute(
                "INSERT INTO movies (tmdb_id, media_type, title, year, poster_url, status, rating, created_at, watched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tmdb_id, media_type, title, year, poster,
                 status, None, datetime.now(timezone.utc).isoformat(), watched_at),
            )
            new_id = cur.lastrowid
        notify_discord(title, year, status, media_type, poster)
        self._json(201, {"ok": True, "id": new_id})

    # ---- PATCH ----
    def do_PATCH(self):
        m = re.match(r"^/api/movies/(\d+)$", self.path)
        if not m:
            return self._json(404, {"ok": False, "error": "Ruta no encontrada"})
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "JSON inválido"})
        movie_id = int(m.group(1))
        fields, values = [], []
        new_status = None
        if data.get("status") in ("pendiente", "vista"):
            new_status = data["status"]
            fields.append("status = ?"); values.append(new_status)
        if "rating" in data:
            r = data["rating"]
            if r is not None and (not isinstance(r, int) or not 1 <= r <= 5):
                return self._json(400, {"ok": False, "error": "rating debe ser 1-5 o null"})
            fields.append("rating = ?"); values.append(r)
        if "note" in data:
            if data["note"] is not None and not isinstance(data["note"], str):
                return self._json(400, {"ok": False, "error": "note debe ser texto o null"})
            note = "" if data["note"] is None else data["note"].strip()
            if len(note) > 500:
                return self._json(400, {"ok": False, "error": "La nota no puede superar 500 caracteres"})
            fields.append("note = ?"); values.append(note)
        if "watched_at" in data:
            try:
                watched_at = parse_watched_at(data.get("watched_at"))
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            fields.append("watched_at = ?"); values.append(watched_at)
        if not fields:
            return self._json(400, {"ok": False, "error": "Nada que actualizar"})
        row = None
        with sqlite3.connect(DB_PATH) as con:
            if new_status == "vista" and "watched_at" not in data:
                current = con.execute("SELECT watched_at FROM movies WHERE id = ?", (movie_id,)).fetchone()
                if current and not current[0]:
                    fields.append("watched_at = ?"); values.append(date.today().isoformat())
            values.append(movie_id)
            cur = con.execute(f"UPDATE movies SET {', '.join(fields)} WHERE id = ?", values)
            if cur.rowcount == 0:
                return self._json(404, {"ok": False, "error": "No encontrada"})
            if new_status:
                row = con.execute(
                    "SELECT title, year, poster_url, media_type FROM movies WHERE id = ?",
                    (movie_id,)).fetchone()
        if new_status and row:
            notify_discord(row[0], row[1], new_status, row[3], row[2])
        self._json(200, {"ok": True})

    # ---- DELETE ----
    def do_DELETE(self):
        m = re.match(r"^/api/movies/(\d+)$", self.path)
        if not m:
            return self._json(404, {"ok": False, "error": "Ruta no encontrada"})
        with sqlite3.connect(DB_PATH) as con:
            cur = con.execute("DELETE FROM movies WHERE id = ?", (int(m.group(1)),))
            if cur.rowcount == 0:
                return self._json(404, {"ok": False, "error": "No encontrada"})
        self._json(200, {"ok": True})

    def log_message(self, *args):
        pass


def main():
    load_env()
    init_db()
    handler = partial(Handler, directory=str(BASE_DIR))
    with ThreadingHTTPServer((HOST, PORT), handler) as httpd:
        tmdb = "sí" if os.environ.get("TMDB_API_KEY") else "no (modo manual)"
        hook = "sí" if (os.environ.get("DISCORD_WEBHOOK_PENDIENTE") or os.environ.get("DISCORD_WEBHOOK_VISTA") or os.environ.get("DISCORD_WEBHOOK_URL")) else "no"
        print(f"Cineteca en http://{HOST}:{PORT}  ·  TMDB: {tmdb}  ·  Discord: {hook}  (Ctrl+C para parar)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServidor detenido.")


if __name__ == "__main__":
    main()
