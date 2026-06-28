#!/usr/bin/env python3
"""Cineteca — tu lista de películas y series, con puntuación y detalle.

Backend con Postgres (psycopg2) vía DATABASE_URL y autenticación JWT
(Supabase). Cada endpoint /api/movies requiere un token válido;
el user_id se extrae del token, nunca del cliente.

Ejecutar:  python server.py
Config en .env:
    DATABASE_URL=...
    SUPABASE_URL=...          (para descubrir el JWKS y verificar el token)
    TMDB_API_KEY=...
    DISCORD_WEBHOOK_URL=...
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import partial, wraps
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt as pyjwt          # PyJWT
from jwt import PyJWKClient
import psycopg2
import psycopg2.errors
import psycopg2.extras
import psycopg2.pool

BASE_DIR = Path(__file__).resolve().parent
HOST, PORT = "0.0.0.0", int(os.environ.get("PORT", 8000))
BLOCKED    = (".env", ".py", ".pyc")
MAX_BODY   = 64 * 1024  # 64 KB — evita OOM por Content-Length malicioso

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

# Pool de conexiones (inicializado en main(), antes de init_db). El semáforo
# gatea el pool al mismo tamaño: si todas las conexiones están en uso, los
# hilos sobrantes esperan en cola en vez de petar con PoolError. Mantiene
# acotado el nº de conexiones al pooler de Supabase ante un pico de tráfico.
_db_pool = None
_db_sem = None
DB_WAIT_TIMEOUT = 10  # s máximos esperando un slot del pool antes de rendirse (503)
DB_CONNECT_TIMEOUT = 5  # s máximos por intento de TCP connect; con DB caída falla rápido
DB_CHECKOUT_TRIES = 3   # intentos del liveness check antes de rendirse (fijo y pequeño:
                        # 1 conexión muerta por timeout de NAT se recupera en 1 reintento;
                        # acotar a maxconn arriesga latencia/inanición con la DB caída)


class DBBusy(Exception):
    """No se obtuvo conexión del pool dentro de DB_WAIT_TIMEOUT (saturación)."""


def init_pool(maxconn):
    global _db_pool, _db_sem
    # keepalives TCP: el pooler de Supabase / el NAT de Render cierran en silencio
    # las conexiones ociosas. Sin sondas, el pool entrega una conexión muerta y el
    # primer execute peta con "could not send data to server: Connection timed out"
    # (→ 502). Las sondas mantienen viva la conexión y detectan la caída a tiempo.
    # connect_timeout acota cada TCP connect: con la DB caída, abrir una conexión
    # nueva falla en DB_CONNECT_TIMEOUT s en vez de colgarse hasta el timeout del SO.
    _db_pool = psycopg2.pool.ThreadedConnectionPool(
        1, maxconn, os.environ["DATABASE_URL"],
        connect_timeout=DB_CONNECT_TIMEOUT,
        keepalives=1, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=5,
    )
    _db_sem = threading.BoundedSemaphore(maxconn)


def _checkout_live():
    """Saca del pool una conexión garantizada viva. Si los keepalives no llegaron
    a tiempo y el pool entrega una conexión que el pooler ya cerró, el SELECT 1
    falla: se descarta (putconn close=True → el pool abre otra) y se reintenta,
    hasta DB_CHECKOUT_TRIES veces; si todas están muertas, propaga el último error.
    Debe llamarse con un slot del semáforo ya adquirido."""
    last_exc = None
    for _ in range(DB_CHECKOUT_TRIES):
        con = _db_pool.getconn()
        try:
            with con.cursor() as cur:
                cur.execute("SELECT 1")
            con.rollback()   # cerrar la transacción implícita del probe
            return con
        except Exception as exc:
            last_exc = exc
            try:
                _db_pool.putconn(con, close=True)
            except Exception:
                pass
    raise last_exc if last_exc else psycopg2.OperationalError("no live DB connection")


@contextmanager
def get_db():
    """Toma una conexión del pool (esperando si está lleno, hasta
    DB_WAIT_TIMEOUT → DBBusy), commit al salir o rollback en error, y la
    devuelve al pool. Si la conexión quedó rota (rollback falló o el pooler
    la cerró), se descarta para que el pool abra otra.

    El semáforo se libera SIEMPRE (finally anidado), de modo que un fallo de
    putconn() no puede fugar permisos y bloquear el servidor."""
    if not _db_sem.acquire(timeout=DB_WAIT_TIMEOUT):
        raise DBBusy("pool de conexiones saturado")
    con = None
    broken = False
    try:
        con = _checkout_live()
        try:
            with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur
            con.commit()
        except Exception:
            broken = True                 # asumir rota; si el rollback va bien, reutilizable
            try:
                con.rollback()
                broken = bool(con.closed)
            except Exception:
                broken = True             # rollback falló → conexión muerta, descartar
            raise
    finally:
        try:
            if con is not None:
                _db_pool.putconn(con, close=broken)
        except Exception:
            pass                          # nunca dejar que putconn impida liberar el semáforo
        finally:
            _db_sem.release()


def _db_guard(method):
    """Convierte una saturación del pool (DBBusy) en un 503 limpio en vez de
    propagar la excepción y devolver un 500 con traceback."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except DBBusy:
            return self._json(503, {"ok": False,
                                    "error": "Servidor ocupado, reintenta en unos segundos."})
    return wrapper


# ── Rate limiting ─────────────────────────────────────────────────────────────
# Ventana deslizante en memoria por usuario, sobre los endpoints que pegan a
# TMDB con NUESTRA clave. Evita que una cuenta agote la cuota de TMDB o el pool.
# Es por proceso (no compartido entre instancias) y los límites son constantes
# de código — suficiente para la escala actual.
_rate_lock = threading.Lock()
_rate_hits = {}            # key -> lista de timestamps monotónicos dentro de la ventana
RATE_WINDOW = 60           # ventana deslizante (segundos) para todos los buckets
RATE_MAX = 60              # tope por usuario en la ventana
RATE_GLOBAL_MAX = 300      # tope agregado (todos los usuarios) — protege la clave
                           # TMDB de abuso vía múltiples cuentas
# Perímetro público anónimo (sin clave por usuario): los endpoints /api/public/*
# no tienen user_id, así que se limitan por IP (primer salto de X-Forwarded-For,
# fallback a client_address) más un bucket global que acota el daño total ante un
# scraper que rota IPs. Reutiliza rate_check() con la misma ventana deslizante.
PUBLIC_RATE_MAX = 60       # tope por IP en la ventana
PUBLIC_RATE_GLOBAL = 600   # tope agregado del perímetro público


def rate_check(buckets):
    """`buckets`: lista de (key, limit). Ventana deslizante por key. Devuelve
    (permitido, retry_after_s) y solo registra el hit en TODOS los buckets si
    TODOS están por debajo de su límite (atómico). Thread-safe."""
    now = time.monotonic()
    with _rate_lock:
        if len(_rate_hits) > 1000:   # barrido oportunista de claves inactivas
            for k in [k for k, v in _rate_hits.items()
                      if not any(now - t < RATE_WINDOW for t in v)]:
                del _rate_hits[k]
        windows = {}
        for key, limit in buckets:
            hits = [t for t in _rate_hits.get(key, []) if now - t < RATE_WINDOW]
            windows[key] = hits
            if len(hits) >= limit:
                _rate_hits[key] = hits
                return False, max(1, int(RATE_WINDOW - (now - hits[0])) + 1)
        for key, hits in windows.items():   # todos por debajo → registrar el hit
            hits.append(now)
            _rate_hits[key] = hits
        return True, 0


# ── Caché TMDB ─────────────────────────────────────────────────────────────────
# Caché en memoria con TTL sobre las respuestas de TMDB. Los datos de TMDB son
# independientes del usuario (un trending/discover/details es idéntico para todos),
# así que cachearlos es seguro y no cruza datos entre cuentas. Recorta llamadas a
# TMDB, latencia y presión sobre la cuota/clave. Por proceso (no compartido entre
# instancias) — suficiente a esta escala, como el rate limiting.
_tmdb_cache = {}            # clave -> (expiry_monotónico, valor)
_tmdb_cache_lock = threading.Lock()
TMDB_CACHE_TTL = int(os.environ.get("TMDB_CACHE_TTL", 900))  # s; 0 = desactiva la caché
TMDB_CACHE_MAX = 500        # tope duro de entradas; purga expiradas + desaloja FIFO al superarlo


def init_db():
    """Verifica que DATABASE_URL es accesible al arrancar."""
    with get_db() as cur:
        cur.execute("SELECT 1")


# ── JWT ───────────────────────────────────────────────────────────────────────

def supabase_base_url():
    """URL base del proyecto Supabase, normalizada desde SUPABASE_URL.
    Tolera el doble esquema (https:https://) y el sufijo /rest/v1 del .env."""
    raw = os.environ.get("SUPABASE_URL", "")
    raw = re.sub(r'^https:https://', 'https://', raw)
    raw = re.sub(r'/rest/v1/?$', '', raw.rstrip('/'))
    return raw


# Inicializado en main() antes de arrancar el servidor para evitar race conditions.
_jwks_client = None

def _get_jwks_client():
    return _jwks_client


def verify_jwt(token: str):
    """Verifica el access token de Supabase con la clave pública del JWKS
    (firma asimétrica ES256/RS256). Devuelve el UUID del usuario (sub) o None.

    Solo se acepta firma asimétrica: no hay fallback HS256. Además del
    chequeo de firma/expiración se exige aud="authenticated" y
    role="authenticated", de modo que un token que no sea una sesión de
    usuario real (anon, otra audience) se rechaza."""
    try:
        client = _get_jwks_client()
        if client is None:
            return None
        signing_key = client.get_signing_key_from_jwt(token)
        payload = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",   # rechaza tokens de otra audience
            leeway=60,                  # tolera desfase de reloj (iat/exp/nbf)
        )
    except (pyjwt.PyJWTError, ValueError, OSError):
        # PyJWTError: token/firma inválidos. ValueError: JWKS devolvió un body
        # no-JSON. OSError: red caída al traer el JWKS. Todo → 401, no 500.
        return None
    if payload.get("role") != "authenticated":
        return None                     # rechaza anon u otros roles
    user_id = payload.get("sub")
    return user_id if user_id else None  # exige sub no vacío (UUID del usuario)


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


# Sistema de niveles. Cada entrada: (puntos mínimos, número, nombre).
# Única fuente de verdad del cálculo de niveles — el cliente nunca suma puntos.
LEVELS = [
    (0,    1, "Espectador"),
    (50,   2, "Aficionado"),
    (150,  3, "Cinéfilo"),
    (350,  4, "Crítico"),
    (700,  5, "Experto"),
    (1200, 6, "Maestro"),
]
POINTS_VISTA = 10
POINTS_RATING = 5
POINTS_NOTE = 5


def compute_level(points):
    """Dado un total de puntos, devuelve el bloque de nivel + progreso al siguiente."""
    current = LEVELS[0]
    nxt = None
    for i, lvl in enumerate(LEVELS):
        if points >= lvl[0]:
            current = lvl
            nxt = LEVELS[i + 1] if i + 1 < len(LEVELS) else None
    current_min, number, name = current
    if nxt is None:  # nivel máximo
        return {
            "points": points, "level": number, "name": name,
            "current_min": current_min, "next_min": None, "next_name": None,
            "points_into_level": points - current_min, "points_to_next": 0,
            "progress_pct": 100,
        }
    next_min, _, next_name = nxt
    span = next_min - current_min
    into = points - current_min
    return {
        "points": points, "level": number, "name": name,
        "current_min": current_min, "next_min": next_min, "next_name": next_name,
        "points_into_level": into, "points_to_next": next_min - points,
        "progress_pct": round(into * 100 / span) if span else 0,
    }


# ── Perfiles públicos y listas ──────────────────────────────────────────────────

# Nombres reservados: colisionan con rutas reales o paths estáticos. Bloqueados
# como username para que /u/<username> nunca pise un endpoint o un archivo servido.
RESERVED_USERNAMES = frozenset({
    "api", "u", "l", "admin", "assets", "health", "config", "vendor", "public",
})
_USERNAME_RE = re.compile(r"^[a-z0-9_-]{3,30}$")
LIST_VISIBILITY = ("private", "unlisted", "public")


def _normalize_username(raw):
    """Helper puro (unit-testable). Normaliza y valida un username elegido por el
    usuario: minúsculas, formato [a-z0-9_-] de 3 a 30, no reservado. Devuelve el
    username normalizado o None si no es válido (incluyendo None/no-str de entrada)."""
    if not isinstance(raw, str):
        return None
    name = raw.strip().lower()
    if not _USERNAME_RE.match(name):
        return None
    if name in RESERVED_USERNAMES:
        return None
    return name


def _hash_user_id(user_id):
    """Hash estable y no reversible del user_id para la traza de auditoría —
    nunca se registra el UUID en claro (LO-*: sin PII en logs)."""
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]


def _audit(action, user_id, target):
    """Una línea estructurada y redactada por cambio de visibilidad/consentimiento
    (set-username / publish-unpublish / list-visibility). Sin PII en claro: el
    user_id va hasheado y no se incluye email, username ni share_token (LO-*)."""
    entry = {
        "action":    action,
        "user_hash": _hash_user_id(user_id),
        "target":    target,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    print("audit " + json.dumps(entry, ensure_ascii=False))


def _public_collection_projection(rows):
    """Proyección pública allow-list de la colección: SOLO campos consentidos.
    NUNCA serializa note, email ni user_id (GD-001 / minimización de datos)."""
    return [{
        "title":          r["title"],
        "poster_url":     r["poster_url"],
        "status":         r["status"],
        "rating":         r["rating"],
        "media_type":     r["media_type"],
        "current_season": r["current_season"],
        "total_seasons":  r["total_seasons"],
    } for r in rows]


def webhook_for(status):
    key = "DISCORD_WEBHOOK_VISTA" if status == "vista" else "DISCORD_WEBHOOK_PENDIENTE"
    return os.environ.get(key, "").strip() or os.environ.get("DISCORD_WEBHOOK_URL", "").strip()


def _send_discord(url, payload_bytes):
    try:
        req = urllib.request.Request(url, data=payload_bytes, headers={
            "Content-Type": "application/json",
            "User-Agent":   "Cineteca-Webhook/1.0 (+local)",
        })
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


def notify_discord(title, year, status, media_type, poster_url="", user_id=None):
    owner = os.environ.get("DISCORD_OWNER_ID", "").strip()
    if owner and user_id != owner:
        return
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
    payload = json.dumps({"embeds": [embed]}).encode("utf-8")
    threading.Thread(target=_send_discord, args=(url, payload), daemon=True).start()


# ── Handler HTTP ──────────────────────────────────────────────────────────────

def _json_default(o):
    """Serializa tipos que json no maneja de serie.
    Postgres devuelve date/datetime como objetos Python → ISO 8601."""
    if isinstance(o, (date, datetime)):   # datetime es subclase de date
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


class Handler(SimpleHTTPRequestHandler):

    # StreamRequestHandler aplica este timeout a CADA operación del socket de la
    # petición (lectura del request y escritura de la respuesta): si una se
    # estanca más de este tiempo, la conexión se corta. Frena Slowloris
    # (Content-Length grande + body a cuentagotas) que, si no, bloquearía un
    # hilo del pool. 15 s sobra para leer un body de 64 KB o escribir el JSON;
    # el tiempo de la llamada saliente a TMDB no cuenta (no toca este socket).
    timeout = 15

    def end_headers(self):
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # supabase-js se sirve desde el mismo origen (vendor/) con SRI; no se
        # confia en ningun CDN externo para scripts -> script-src 'self'.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "img-src 'self' https://image.tmdb.org data: blob:; "
            "connect-src 'self' https://*.supabase.co; "
            "frame-ancestors 'none'",
        )
        super().end_headers()

    def _json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def _rate_limited(self, user_id):
        """Aplica el límite de tasa TMDB a `user_id`. Si lo supera, responde
        429 + Retry-After y devuelve True (el endpoint debe retornar)."""
        allowed, retry = rate_check([(f"tmdb:{user_id}", RATE_MAX),
                                     ("tmdb:_global", RATE_GLOBAL_MAX)])
        if allowed:
            return False
        self._json(429, {"ok": False, "error": "Demasiadas peticiones, espera un momento."},
                   extra_headers={"Retry-After": retry})
        return True

    def _client_ip(self):
        """IP del cliente para el limiter público: primer salto de
        X-Forwarded-For (Render lo fija delante de nuestra app) con fallback a
        la dirección del socket. El primer salto es el cliente real cuando hay
        un proxy de confianza; un cliente directo puede falsearlo, por eso el
        bucket global acota el daño total (ver caveat del threat model)."""
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
        return self.client_address[0]

    def _public_rate_limited(self):
        """Limiter por IP + global para el perímetro público anónimo. Si se
        supera cualquiera de los dos, responde 429 + Retry-After y devuelve True
        (el endpoint debe retornar ANTES de cualquier lectura de la DB)."""
        ip = self._client_ip()
        allowed, retry = rate_check([(f"public:{ip}", PUBLIC_RATE_MAX),
                                     ("public:_global", PUBLIC_RATE_GLOBAL)])
        if allowed:
            return False
        self._json(429, {"ok": False, "error": "Demasiadas peticiones, espera un momento."},
                   extra_headers={"Retry-After": retry})
        return True

    def _read_json(self):
        length = max(0, min(int(self.headers.get("Content-Length", 0)), MAX_BODY))
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

        # Clave de caché: path + params ordenados, EXCLUYENDO api_key (es el
        # secreto y además constante por proceso). Determinista entre llamadas.
        cache_key = (path, tuple(sorted((k, v) for k, v in params.items() if k != "api_key")))
        if TMDB_CACHE_TTL > 0:
            now = time.monotonic()
            with _tmdb_cache_lock:
                hit = _tmdb_cache.get(cache_key)
                if hit and hit[0] > now:
                    return hit[1]

        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())   # un error de red/HTTP se propaga (no se cachea)

        if TMDB_CACHE_TTL > 0:
            now = time.monotonic()
            with _tmdb_cache_lock:
                if len(_tmdb_cache) >= TMDB_CACHE_MAX:   # purga oportunista de expiradas
                    for k in [k for k, (exp, _) in _tmdb_cache.items() if exp <= now]:
                        del _tmdb_cache[k]
                while len(_tmdb_cache) >= TMDB_CACHE_MAX:  # tope duro: desaloja las más antiguas (FIFO)
                    del _tmdb_cache[next(iter(_tmdb_cache))]
                _tmdb_cache[cache_key] = (now + TMDB_CACHE_TTL, data)
        return data

    # ── GET ───────────────────────────────────────────────────────────────────

    @_db_guard
    def do_GET(self):
        path         = self.path.split("?", 1)[0]
        decoded_path = urllib.parse.unquote(path)
        if path == "/health":       return self._json(200, {"ok": True, "status": "up"})
        if path == "/api/config":   return self._json(200, {
            "supabase_url":      supabase_base_url(),
            "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
        })
        if path == "/api/movies":   return self._list_movies()
        if path == "/api/level":    return self._level()
        if path == "/api/search":   return self._search()
        if path == "/api/trending": return self._trending()
        if path == "/api/discover": return self._discover()
        if path == "/api/details":  return self._details()
        if path == "/api/similar":  return self._similar()
        if path == "/api/profile":  return self._get_profile()
        if path == "/api/lists":    return self._list_lists()
        m = re.match(r"^/api/lists/([0-9a-fA-F-]{36})$", path)
        if m:                       return self._get_list(m.group(1))
        m = re.match(r"^/api/public/profile/([a-z0-9_-]{3,30})$", path)
        if m:                       return self._public_profile(m.group(1))
        m = re.match(r"^/api/public/list/([0-9a-fA-F-]{36})$", path)
        if m:                       return self._public_list(m.group(1))
        # Páginas públicas (sin auth): sirven el HTML estático y dejan que
        # public.js resuelva perfil-vs-lista por location.pathname. Va ANTES del
        # fallthrough estático para que /u y /l no caigan en el servidor de archivos.
        if re.match(r"^/u/[a-z0-9_-]{3,30}$", path) or re.match(r"^/l/[0-9a-fA-F-]{36}$", path):
            self.path = "/public.html"
            return super().do_GET()
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

    def _level(self):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        with get_db() as cur:
            cur.execute(
                "SELECT "
                "  COUNT(*) FILTER (WHERE status = 'vista')                AS vistas, "
                "  COUNT(*) FILTER (WHERE rating IS NOT NULL)              AS valoradas, "
                "  COUNT(*) FILTER (WHERE note IS NOT NULL AND note <> '') AS notas "
                "FROM movies WHERE user_id = %s",
                (user_id,))
            row = cur.fetchone()
        vistas    = row["vistas"]    or 0
        valoradas = row["valoradas"] or 0
        notas     = row["notas"]     or 0
        points = (vistas * POINTS_VISTA
                  + valoradas * POINTS_RATING
                  + notas * POINTS_NOTE)
        self._json(200, {"ok": True, **compute_level(points)})

    def _search(self):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        if self._rate_limited(user_id):
            return
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
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        if self._rate_limited(user_id):
            return
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

    # IDs de géneros de películas → IDs equivalentes en TV (TMDB usa catálogos distintos)
    _MOVIE_TO_TV_GENRE = {
        28:  10759,  # Acción          → Acción y aventura
        12:  10759,  # Aventura        → Acción y aventura
        27:   9648,  # Terror          → Misterio (más cercano en TV)
        878: 10765,  # Ciencia ficción → Ciencia ficción y fantasía
        53:     80,  # Suspense        → Crimen (más cercano en TV)
    }

    def _discover(self):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        if self._rate_limited(user_id):
            return
        q = self._qs()
        genre_id_str = (q.get("genre_id", [""])[0]).strip()
        media_type   = (q.get("type",     ["all"])[0]).strip()
        if not genre_id_str.isdigit():
            return self._json(400, {"ok": False, "error": "genre_id inválido"})
        if media_type not in ("movie", "tv", "all"):
            return self._json(400, {"ok": False, "error": "type debe ser movie, tv o all"})
        page_str  = (q.get("page", ["1"])[0]).strip()
        page      = max(1, min(int(page_str) if page_str.isdigit() else 1, 20))
        sort_key  = (q.get("sort", ["popular"])[0]).strip()
        genre_id  = int(genre_id_str)
        tv_genre  = str(self._MOVIE_TO_TV_GENRE.get(genre_id, genre_id))
        if not os.environ.get("TMDB_API_KEY", "").strip():
            return self._json(200, {"ok": False, "needs_key": True})

        _sort_map = {
            "popular": ("popularity.desc",          "popularity.desc"),
            "rating":  ("vote_average.desc",         "vote_average.desc"),
            "recent":  ("primary_release_date.desc", "first_air_date.desc"),
        }
        mv_sort, tv_sort = _sort_map.get(sort_key, _sort_map["popular"])
        base = {"include_adult": "false", "page": str(page)}
        if sort_key == "rating":
            base["vote_count.gte"] = "100"
        mv_extra = {**base, "sort_by": mv_sort, "with_genres": genre_id_str}
        tv_extra = {**base, "sort_by": tv_sort, "with_genres": tv_genre}

        def pack_item(m, mt):
            poster = m.get("poster_path")
            return {
                "tmdb_id":    m.get("id"),
                "media_type": mt,
                "title":      m.get("title") or m.get("name") or "Sin título",
                "year":       (m.get("release_date") or m.get("first_air_date") or "")[:4],
                "poster_url": (TMDB_IMG + poster) if poster else "",
                "genre_ids":  m.get("genre_ids", []),
            }

        try:
            results  = []
            has_more = False
            if media_type == "all":
                mv_data = self._tmdb("/discover/movie", mv_extra) or {}
                tv_data = self._tmdb("/discover/tv",    tv_extra) or {}
                mv = mv_data.get("results", [])
                tv = tv_data.get("results", [])
                for i in range(max(len(mv[:9]), len(tv[:9]))):
                    if i < len(mv) and len(results) < 18: results.append(pack_item(mv[i], "movie"))
                    if i < len(tv) and len(results) < 18: results.append(pack_item(tv[i], "tv"))
                has_more = (page < (mv_data.get("total_pages") or 1) or
                            page < (tv_data.get("total_pages") or 1))
            elif media_type == "movie":
                mv_data  = self._tmdb("/discover/movie", mv_extra) or {}
                results  = [pack_item(m, "movie") for m in mv_data.get("results", [])[:18]]
                has_more = page < (mv_data.get("total_pages") or 1)
            else:
                tv_data  = self._tmdb("/discover/tv", tv_extra) or {}
                results  = [pack_item(m, "tv") for m in tv_data.get("results", [])[:18]]
                has_more = page < (tv_data.get("total_pages") or 1)
        except Exception:
            return self._json(502, {"ok": False, "error": "No se pudo consultar TMDB."})
        self._json(200, {"ok": True, "results": results, "page": page, "has_more": has_more})

    def _details(self):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        if self._rate_limited(user_id):
            return
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
            "genre_ids":      [g["id"] for g in d.get("genres", []) if g.get("id")],
            "runtime":        runtime,
            "title":          d.get("title") or d.get("name") or "",
            "poster_path":    d.get("poster_path") or "",
            "vote_average":   round(d.get("vote_average") or 0, 1),
            "trailer":        trailer,
            "dir_label":      dir_label,
            "directors":      directors,
            "cast":           cast,
            "providers":      providers,
            "providers_link": wp_es.get("link", ""),
            "total_seasons":  d.get("number_of_seasons") if mt == "tv" else None,
        }})

    def _similar(self):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        if self._rate_limited(user_id):
            return
        q   = self._qs()
        tid = (q.get("id",   [""])[0]).strip()
        mt  = (q.get("type", ["movie"])[0]).strip()
        if not tid.isdigit() or mt not in ("movie", "tv"):
            return self._json(400, {"ok": False, "error": "Parámetros inválidos"})
        if not os.environ.get("TMDB_API_KEY", "").strip():
            return self._json(200, {"ok": True, "results": []})
        try:
            data = self._tmdb(f"/{mt}/{tid}/similar")
        except Exception:
            return self._json(200, {"ok": True, "results": []})
        items = []
        for r in (data.get("results") or [])[:6]:
            if not r.get("id"):
                continue
            poster = r.get("poster_path")
            items.append({
                "tmdb_id":    r.get("id"),
                "type":       mt,
                "title":      r.get("title") or r.get("name") or "",
                "year":       (r.get("release_date") or r.get("first_air_date") or "")[:4],
                "poster_url": TMDB_IMG + poster if poster else "",
            })
        self._json(200, {"ok": True, "results": items})

    # ── POST ──────────────────────────────────────────────────────────────────

    @_db_guard
    def do_POST(self):
        if self.path == "/api/lists":
            return self._create_list()
        m = re.match(r"^/api/lists/([0-9a-fA-F-]{36})/items$", self.path)
        if m:
            return self._add_list_item(m.group(1))
        if self.path != "/api/movies":
            return self._json(404, {"ok": False, "error": "Ruta no encontrada"})
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "JSON inválido"})
        title = str(data.get("title", "")).strip()[:300]
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
        year   = str(data.get("year", "")).strip()[:10]
        poster = str(data.get("poster_url", "")).strip()
        # Solo aceptamos posters servidos por TMDB; cualquier otra URL se descarta.
        if poster and not poster.startswith("https://image.tmdb.org/"):
            poster = ""
        poster = poster[:500]
        genres = ", ".join(
            TMDB_GENRES[gid] for gid in (data.get("genre_ids") or [])
            if isinstance(gid, int) and gid in TMDB_GENRES
        )
        if not genres:
            # Fallback: nombres de género ya resueltos. El alta desde el modal
            # de detalle no tiene genre_ids (TMDB /details devuelve nombres),
            # así que el cliente envía la lista de nombres en `genres`.
            raw = data.get("genres")
            if isinstance(raw, list):
                genres = ", ".join(
                    str(g).strip()[:40] for g in raw[:8]
                    if isinstance(g, str) and g.strip()
                )
        genres = genres or None

        total_seasons = data.get("total_seasons")
        if total_seasons is not None and (not isinstance(total_seasons, int) or total_seasons < 1):
            return self._json(400, {"ok": False, "error": "total_seasons debe ser un entero positivo o null"})
        # BR-1: el total de temporadas solo aplica a series; en películas se fuerza null.
        if media_type != "tv":
            total_seasons = None

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
                " rating, created_at, watched_at, genres, total_seasons) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (user_id, tmdb_id, media_type, title, year, poster,
                 status, None, datetime.now(timezone.utc).isoformat(), watched_at, genres,
                 total_seasons))
            new_id = cur.fetchone()["id"]

        notify_discord(title, year, status, media_type, poster, user_id)
        self._json(201, {"ok": True, "id": new_id})

    # ── PATCH ─────────────────────────────────────────────────────────────────

    @_db_guard
    def do_PATCH(self):
        if self.path == "/api/profile":
            return self._patch_profile()
        m = re.match(r"^/api/lists/([0-9a-fA-F-]{36})$", self.path)
        if m:
            return self._patch_list(m.group(1))
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
        for field in ("current_season", "current_episode", "total_seasons"):
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
            notify_discord(row["title"], row["year"], new_status, row["media_type"], row["poster_url"], user_id)
        self._json(200, {"ok": True})

    # ── DELETE ────────────────────────────────────────────────────────────────

    @_db_guard
    def do_DELETE(self):
        mm = re.match(r"^/api/lists/([0-9a-fA-F-]{36})/items/([0-9a-fA-F-]{36})$", self.path)
        if mm:
            return self._delete_list_item(mm.group(1), mm.group(2))
        ml = re.match(r"^/api/lists/([0-9a-fA-F-]{36})$", self.path)
        if ml:
            return self._delete_list(ml.group(1))
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

    # ── Perfil (owner) ──────────────────────────────────────────────────────────

    def _get_profile(self):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        with get_db() as cur:
            cur.execute(
                "SELECT username, is_public, show_collection, show_stats "
                "FROM profiles WHERE user_id = %s",
                (user_id,))
            row = cur.fetchone()
        if row:
            profile = dict(row)
        else:
            # Defaults perezosos: nunca se crea una fila pública implícitamente.
            profile = {"username": None, "is_public": False,
                       "show_collection": False, "show_stats": False}
        self._json(200, {"ok": True, "profile": profile})

    def _patch_profile(self):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "JSON inválido"})

        # Estado actual (para validar publish-sin-username y para auditar cambios).
        with get_db() as cur:
            cur.execute(
                "SELECT username, is_public, show_collection, show_stats "
                "FROM profiles WHERE user_id = %s",
                (user_id,))
            current = cur.fetchone()
        cur_username = current["username"] if current else None
        cur_is_public = current["is_public"] if current else False

        cols, vals = [], []     # columna → valor a escribir (orden estable)
        new_username = cur_username
        username_changed = False
        if "username" in data:
            raw = data["username"]
            if raw is None or (isinstance(raw, str) and raw.strip() == ""):
                return self._json(400, {"ok": False, "error": "El nombre de usuario no puede quedar vacío"})
            norm = _normalize_username(raw)
            if norm is None:
                return self._json(400, {"ok": False,
                                        "error": "Nombre de usuario inválido (3-30, a-z 0-9 _ - y no reservado)"})
            new_username = norm
            username_changed = (norm != cur_username)
            cols.append("username"); vals.append(norm)

        for flag in ("is_public", "show_collection", "show_stats"):
            if flag in data:
                v = data[flag]
                if not isinstance(v, bool):
                    return self._json(400, {"ok": False, "error": f"{flag} debe ser booleano"})
                cols.append(flag); vals.append(v)

        if not cols:
            return self._json(400, {"ok": False, "error": "Nada que actualizar"})

        # AC-1: no se puede publicar el perfil sin un username válido.
        target_is_public = data.get("is_public", cur_is_public)
        if target_is_public and not new_username:
            return self._json(400, {"ok": False,
                                    "error": "Define un nombre de usuario antes de hacer público tu perfil"})

        cols.append("updated_at"); vals.append(datetime.now(timezone.utc).isoformat())
        # UPSERT: la fila puede no existir todavía (defaults perezosos en GET).
        # Columnas controladas por código (no input del usuario); valores siempre %s.
        insert_cols = ", ".join(["user_id", *cols])
        insert_ph   = ", ".join(["%s"] * (len(cols) + 1))
        update_set  = ", ".join(f"{c} = %s" for c in cols)
        params = [user_id, *vals, *vals]
        with get_db() as cur:
            try:
                cur.execute(
                    f"INSERT INTO profiles ({insert_cols}) VALUES ({insert_ph}) "
                    f"ON CONFLICT (user_id) DO UPDATE SET {update_set}",
                    params)
            except psycopg2.errors.UniqueViolation:
                # AC-2: username ya tomado (case-insensitive vía lowercase + UNIQUE).
                return self._json(409, {"ok": False, "error": "Ese nombre de usuario ya está en uso"})

        if username_changed:
            _audit("profile.username_set", user_id, "profile")
        if "is_public" in data and data["is_public"] != cur_is_public:
            _audit("profile.publish" if data["is_public"] else "profile.unpublish", user_id, "profile")
        self._json(200, {"ok": True})

    # ── Listas (owner) ───────────────────────────────────────────────────────────

    def _list_lists(self):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        with get_db() as cur:
            cur.execute(
                "SELECT l.id, l.name, l.visibility, l.share_token, l.updated_at, "
                "       COUNT(li.id) AS item_count "
                "FROM lists l LEFT JOIN list_items li ON li.list_id = l.id "
                "WHERE l.user_id = %s "
                "GROUP BY l.id ORDER BY l.updated_at DESC",
                (user_id,))
            rows = cur.fetchall()
        self._json(200, {"ok": True, "lists": [dict(r) for r in rows]})

    def _create_list(self):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "JSON inválido"})
        name = str(data.get("name", "")).strip()[:200]
        if not name:
            return self._json(400, {"ok": False, "error": "El nombre de la lista es obligatorio"})
        with get_db() as cur:
            cur.execute(
                "INSERT INTO lists (user_id, name) VALUES (%s, %s) RETURNING id",
                (user_id, name))
            new_id = cur.fetchone()["id"]
        self._json(201, {"ok": True, "id": new_id})

    def _get_list(self, list_id):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        with get_db() as cur:
            cur.execute(
                "SELECT id, name, visibility, share_token, updated_at "
                "FROM lists WHERE id = %s AND user_id = %s",
                (list_id, user_id))
            lst = cur.fetchone()
            if not lst:
                return self._json(404, {"ok": False, "error": "No encontrada"})
            cur.execute(
                "SELECT id, tmdb_id, media_type, title, year, poster_url, position "
                "FROM list_items WHERE list_id = %s ORDER BY position, created_at",
                (list_id,))
            items = cur.fetchall()
        body = dict(lst)
        body["items"] = [dict(i) for i in items]
        self._json(200, {"ok": True, "list": body})

    def _patch_list(self, list_id):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "JSON inválido"})

        new_visibility = None
        cols, vals = [], []
        if "name" in data:
            name = str(data.get("name", "")).strip()[:200]
            if not name:
                return self._json(400, {"ok": False, "error": "El nombre de la lista no puede quedar vacío"})
            cols.append("name"); vals.append(name)
        if "visibility" in data:
            vis = data["visibility"]
            if vis not in LIST_VISIBILITY:
                return self._json(400, {"ok": False, "error": "Visibilidad no válida"})
            new_visibility = vis
            cols.append("visibility"); vals.append(vis)
        item_order = None
        if "item_order" in data:
            item_order = data["item_order"]
            if not isinstance(item_order, list) or not all(isinstance(x, str) for x in item_order):
                return self._json(400, {"ok": False, "error": "item_order debe ser una lista de ids"})

        if not cols and item_order is None:
            return self._json(400, {"ok": False, "error": "Nada que actualizar"})

        # AC-1: no se puede hacer pública/unlisted una lista sin username.
        if new_visibility in ("unlisted", "public"):
            with get_db() as cur:
                cur.execute("SELECT username FROM profiles WHERE user_id = %s", (user_id,))
                prow = cur.fetchone()
            if not (prow and prow["username"]):
                return self._json(400, {"ok": False,
                                        "error": "Define un nombre de usuario antes de compartir una lista"})

        with get_db() as cur:
            if cols:
                cols.append("updated_at"); vals.append(datetime.now(timezone.utc).isoformat())
                set_clause = ", ".join(f"{c} = %s" for c in cols)
                cur.execute(
                    f"UPDATE lists SET {set_clause} WHERE id = %s AND user_id = %s",
                    [*vals, list_id, user_id])
                if cur.rowcount == 0:
                    return self._json(404, {"ok": False, "error": "No encontrada"})
            else:
                # Solo reordenar: verifica propiedad explícitamente (AC-13 → 404).
                cur.execute(
                    "SELECT 1 FROM lists WHERE id = %s AND user_id = %s",
                    (list_id, user_id))
                if not cur.fetchone():
                    return self._json(404, {"ok": False, "error": "No encontrada"})
            if item_order is not None:
                # Reordena solo los items que pertenecen a esta lista; ids ajenos
                # se ignoran (el WHERE list_id acota el efecto).
                for pos, item_id in enumerate(item_order):
                    cur.execute(
                        "UPDATE list_items SET position = %s WHERE id = %s AND list_id = %s",
                        (pos, item_id, list_id))

        if new_visibility is not None:
            _audit("list.visibility_set", user_id, f"visibility={new_visibility}")
        self._json(200, {"ok": True})

    def _delete_list(self, list_id):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        with get_db() as cur:
            # list_items cae por FK ON DELETE CASCADE.
            cur.execute(
                "DELETE FROM lists WHERE id = %s AND user_id = %s",
                (list_id, user_id))
            if cur.rowcount == 0:
                return self._json(404, {"ok": False, "error": "No encontrada"})
        self._json(200, {"ok": True})

    def _add_list_item(self, list_id):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "JSON inválido"})
        tmdb_id = data.get("tmdb_id")
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            return self._json(400, {"ok": False, "error": "tmdb_id inválido"})
        media_type = data.get("media_type")
        if media_type not in ("movie", "tv"):
            return self._json(400, {"ok": False, "error": "media_type debe ser movie o tv"})
        title = str(data.get("title", "")).strip()[:300]
        if not title:
            return self._json(400, {"ok": False, "error": "El título es obligatorio"})
        year   = str(data.get("year", "")).strip()[:10]
        poster = str(data.get("poster_url", "")).strip()
        # Misma allow-list que el alta de películas: solo posters de TMDB.
        if poster and not poster.startswith("https://image.tmdb.org/"):
            poster = ""
        poster = poster[:500]

        with get_db() as cur:
            # Propiedad de la lista (AC-13 → 404 si es de otro usuario).
            cur.execute(
                "SELECT 1 FROM lists WHERE id = %s AND user_id = %s",
                (list_id, user_id))
            if not cur.fetchone():
                return self._json(404, {"ok": False, "error": "No encontrada"})
            cur.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) AS next_pos "
                "FROM list_items WHERE list_id = %s",
                (list_id,))
            next_pos = cur.fetchone()["next_pos"]
            try:
                cur.execute(
                    "INSERT INTO list_items "
                    "(list_id, tmdb_id, media_type, title, year, poster_url, position) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (list_id, tmdb_id, media_type, title, year, poster, next_pos))
                new_id = cur.fetchone()["id"]
            except psycopg2.errors.UniqueViolation:
                # Dedup (list_id, tmdb_id, media_type) → 409.
                return self._json(409, {"ok": False, "error": "Ese título ya está en la lista"})
        self._json(201, {"ok": True, "id": new_id})

    def _delete_list_item(self, list_id, item_id):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        with get_db() as cur:
            # Propiedad vía JOIN a lists.user_id: un item de otra lista/usuario → 404.
            cur.execute(
                "DELETE FROM list_items li USING lists l "
                "WHERE li.id = %s AND li.list_id = %s "
                "AND l.id = li.list_id AND l.user_id = %s",
                (item_id, list_id, user_id))
            if cur.rowcount == 0:
                return self._json(404, {"ok": False, "error": "No encontrada"})
        self._json(200, {"ok": True})

    # ── Endpoints públicos (sin auth, rate-limit por IP) ─────────────────────────

    def _public_profile(self, username):
        if self._public_rate_limited():
            return
        username = username.lower()
        with get_db() as cur:
            cur.execute(
                "SELECT user_id, username, is_public, show_collection, show_stats "
                "FROM profiles WHERE username = %s",
                (username,))
            prof = cur.fetchone()
            # AC-3: perfil inexistente o no público → 404 (no enumera).
            if not prof or not prof["is_public"]:
                return self._json(404, {"ok": False, "error": "No encontrado"})
            owner_id = prof["user_id"]
            body = {"username": prof["username"]}
            # AC-4: la colección solo si show_collection.
            if prof["show_collection"]:
                cur.execute(
                    "SELECT title, poster_url, status, rating, media_type, "
                    "       current_season, total_seasons "
                    "FROM movies WHERE user_id = %s ORDER BY created_at DESC",
                    (owner_id,))
                body["collection"] = _public_collection_projection(cur.fetchall())
            # AC-6: stats solo si show_stats. Reutiliza el mismo agregado que _level
            # + compute_level (PS-004), parametrizado por el user_id del perfil.
            if prof["show_stats"]:
                cur.execute(
                    "SELECT "
                    "  COUNT(*) FILTER (WHERE status = 'vista')                AS vistas, "
                    "  COUNT(*) FILTER (WHERE rating IS NOT NULL)              AS valoradas, "
                    "  COUNT(*) FILTER (WHERE note IS NOT NULL AND note <> '') AS notas "
                    "FROM movies WHERE user_id = %s",
                    (owner_id,))
                srow = cur.fetchone()
                points = ((srow["vistas"] or 0) * POINTS_VISTA
                          + (srow["valoradas"] or 0) * POINTS_RATING
                          + (srow["notas"] or 0) * POINTS_NOTE)
                body["stats"] = compute_level(points)
            # AC-9 / AC-10: solo las listas públicas en el perfil; nunca unlisted.
            cur.execute(
                "SELECT l.id, l.name, l.share_token, COUNT(li.id) AS item_count "
                "FROM lists l LEFT JOIN list_items li ON li.list_id = l.id "
                "WHERE l.user_id = %s AND l.visibility = 'public' "
                "GROUP BY l.id ORDER BY l.updated_at DESC",
                (owner_id,))
            body["lists"] = [dict(r) for r in cur.fetchall()]
        self._json(200, {"ok": True, "profile": body})

    def _public_list(self, share_token):
        if self._public_rate_limited():
            return
        with get_db() as cur:
            cur.execute(
                "SELECT l.name, l.visibility, p.username AS owner_username "
                "FROM lists l LEFT JOIN profiles p ON p.user_id = l.user_id "
                "WHERE l.share_token = %s",
                (share_token,))
            lst = cur.fetchone()
            # AC-7 / AC-11: privada o inexistente → 404 (el token re-privatizado muere).
            if not lst or lst["visibility"] == "private":
                return self._json(404, {"ok": False, "error": "No encontrada"})
            cur.execute(
                "SELECT tmdb_id, media_type, title, year, poster_url "
                "FROM list_items li JOIN lists l ON l.id = li.list_id "
                "WHERE l.share_token = %s ORDER BY li.position, li.created_at",
                (share_token,))
            items = cur.fetchall()
        self._json(200, {"ok": True, "list": {
            "name":           lst["name"],
            "owner_username": lst["owner_username"],
            "items":          [dict(i) for i in items],
        }})

    def log_message(self, *args):
        pass


# ── Arranque ──────────────────────────────────────────────────────────────────

def main():
    global _jwks_client
    load_env()
    init_pool(int(os.environ.get("DB_POOL_MAX", 10)))
    init_db()
    base = supabase_base_url()
    if base:
        _jwks_client = PyJWKClient(f"{base}/auth/v1/.well-known/jwks.json")
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
        finally:
            if _db_pool is not None:
                _db_pool.closeall()


if __name__ == "__main__":
    main()
