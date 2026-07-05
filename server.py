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
import email.utils
import gzip
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
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
MAX_BODY   = 64 * 1024  # 64 KB — evita OOM por Content-Length malicioso

# Allow-list de assets estáticos que la app de navegador referencia realmente.
# Serving deny-by-default (BR-1): cualquier path que no case aquí → 404. La verja
# vive en send_head() (único punto de paso de GET+HEAD) y decide pertenencia contra
# el path normalizado por translate_path y confinado a BASE_DIR (US-040), de modo que
# variantes `.`/`..`/`%2e`/slash-duplicado no pueden colarse. Un asset nuevo del
# frontend DEBE añadirse aquí o dará 404 en producción (BR-7). Sustituye a la antigua
# deny-list de 3 extensiones (subsumida por la allow-list).
STATIC_FILES = frozenset({
    "index.html", "public.html", "privacy.html", "terms.html", "about.html",
    "boot.js", "api.js", "ui.js", "collection.js", "modal.js", "discover.js",
    "stats.js", "settings.js", "activity.js", "app.js", "public.js",
    "styles.css", "landing.css", "legal.css",
    "robots.txt", "sitemap.xml",
})
STATIC_DIRS = ("assets", "vendor")   # sirve solo archivos regulares existentes debajo

# Tipos de contenido de texto elegibles para gzip (ADR-020, BR-2). Los binarios ya
# comprimidos (image/png|jpeg|webp) quedan fuera a propósito (BR-3): recomprimir gasta
# CPU sin ganancia. `.js` puede resolverse como application/javascript (Python < 3.11)
# o text/javascript (>= 3.11); ambos deben entrar o el mayor asset de texto viaja sin
# comprimir.
GZIP_TYPES = frozenset({
    "text/html", "text/css", "application/javascript", "text/javascript",
    "application/json", "image/svg+xml",
})
GZIP_MIN_SIZE = 1024   # cuerpos < 1 KB no se comprimen: la cabecera gzip los engordaría

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
# Borrado de cuenta (acción irreversible con re-verificación de contraseña): límites
# estrechos por usuario + global, aplicados justo tras la auth. Acotan el endpoint como
# oráculo de verificación de contraseña (un 401 distinto en contraseña incorrecta).
ACCOUNT_DELETE_MAX = 5        # tope por usuario en la ventana
ACCOUNT_DELETE_GLOBAL = 30    # tope agregado
# Exportación de datos (portabilidad GDPR): lectura de tres tablas de TODAS las
# filas del usuario — el endpoint autenticado más pesado. Límite modesto por
# usuario + global, aplicado justo tras la auth, como guarda DoS.
ACCOUNT_EXPORT_MAX = 10       # tope por usuario en la ventana
ACCOUNT_EXPORT_GLOBAL = 60    # tope agregado
# Importación de datos (round-trip inverso del export): una escritura autenticada
# que ingiere un archivo NO confiable. Como es escritura, los buckets son más
# estrechos que el export de solo-lectura (mismos que account-delete). Además del
# rate limit, tiene su propio tope de cuerpo (1 MB, mayor que MAX_BODY de 64 KB,
# para admitir un export realista) y topes de conteo de elementos/listas — guardas
# DoS aplicadas ANTES de tocar la DB.
ACCOUNT_IMPORT_MAX = 5        # tope por usuario en la ventana
ACCOUNT_IMPORT_GLOBAL = 30    # tope agregado
MAX_IMPORT_BODY = 1 * 1024 * 1024   # 1 MB — tope propio del import (NO el MAX_BODY de 64 KB)
MAX_IMPORT_ITEMS = 5000      # tope de elementos (títulos + items de listas)
MAX_IMPORT_LISTS = 500       # tope de listas
# Capa social (follows + feed): acciones por usuario tras un JWT válido. Límite
# por usuario + global (misma ventana deslizante), aplicado justo tras la auth,
# como guarda anti-abuso/enumeración — reflejando ACCOUNT_EXPORT_*.
FOLLOW_RATE_MAX = 60          # tope por usuario en la ventana (POST /api/follows)
FOLLOW_RATE_GLOBAL = 600      # tope agregado
FEED_RATE_MAX = 60            # tope por usuario en la ventana (GET /api/feed)
FEED_RATE_GLOBAL = 600        # tope agregado
FEED_LIMIT = 50               # nº máximo de eventos devueltos por el feed (sin paginación en v1)
PUBLIC_FOLLOW_LIST_MAX = 50   # tope de handles públicos listados en un perfil
# Capa social Fase 2 (reviews + likes, ADR-015): acciones de like por usuario tras
# un JWT válido. Límite por usuario + global (misma ventana deslizante), aplicado
# justo tras la auth en los tres verbos /api/reviews/{movie_id}/likes — reflejando
# FOLLOW_RATE_*. Los dos topes de display acotan las proyecciones públicas.
LIKE_RATE_MAX = 60            # tope por usuario en la ventana (POST/DELETE/GET likes)
LIKE_RATE_GLOBAL = 600        # tope agregado
PUBLIC_LIKE_LIST_MAX = 50     # tope de likers públicos nombrados en la lista "quién dio like"
PUBLIC_REVIEW_LIST_MAX = 100  # tope de reseñas listadas en el área de reseñas del perfil público


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
# Metadatos de temporada: iguales para todos los usuarios y cambian rara vez →
# TTL propio (~24 h) que anula TMDB_CACHE_TTL solo en esa llamada (CA-*, BR-3).
SEASON_CACHE_TTL = int(os.environ.get("SEASON_CACHE_TTL", 86400))  # s; override por-llamada
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


def verify_jwt_identity(token: str):
    """Verifica el access token de Supabase con la clave pública del JWKS
    (firma asimétrica ES256/RS256) y devuelve la identidad verificada como
    (user_id, email), o (None, None) si el token falta / es inválido.

    Ruta de verificación ÚNICA para el proyecto (verify_jwt delega aquí): solo
    firma asimétrica (sin fallback HS256), y además del chequeo de firma/
    expiración se exige aud="authenticated" y role="authenticated", de modo que
    un token que no sea una sesión de usuario real (anon, otra audience) se
    rechaza. El email SIEMPRE sale de este payload verificado — nunca de un
    decode sin verificar ni del cuerpo del cliente. Un email ausente/vacío se
    devuelve como None (el llamante lo trata como fallo de re-autenticación)."""
    try:
        client = _get_jwks_client()
        if client is None:
            return None, None
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
        return None, None
    if payload.get("role") != "authenticated":
        return None, None               # rechaza anon u otros roles
    user_id = payload.get("sub")
    if not user_id:
        return None, None               # exige sub no vacío (UUID del usuario)
    email = payload.get("email")
    email = email if isinstance(email, str) and email.strip() else None
    return user_id, email


def verify_jwt(token: str):
    """Verifica el access token de Supabase y devuelve el UUID del usuario (sub)
    o None. Delega en verify_jwt_identity para mantener una única ruta de
    verificación; descarta el email (el caso común solo necesita el user_id)."""
    user_id, _ = verify_jwt_identity(token)
    return user_id


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


def _import_int_or_none(value):
    """Coerción para current_season/current_episode/total_seasons en el import:
    None → None; int >= 1 → el int; cualquier otra cosa → False (marcador de
    inválido, distinguible de None porque `False is None` es falso)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return False
    return value


def _import_parse_timestamp(value):
    """created_at para el import: el valor del archivo si parsea como timestamp
    ISO válido, si no `datetime.now(timezone.utc).isoformat()`. Nunca lanza."""
    if isinstance(value, str) and value.strip():
        try:
            datetime.fromisoformat(value.strip())
            return value.strip()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


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
    nunca se registra el UUID en claro (LO-*: sin PII en logs). Con user_id None
    (denegación no autenticada) devuelve None: no hay identidad que hashear, así
    que la línea de auditoría lleva "user_hash": null y conserva el mismo esquema."""
    if user_id is None:
        return None
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


def _record_activity(cur, user_id, action, snapshot, rating=None, list_id=None, movie_id=None):
    """Escribe UN evento social en `activity` (append-only, ADR-014) usando el
    cursor de la MISMA transacción de la mutación disparadora — atómico con la
    acción, sin segundo round-trip. `snapshot` es un dict con la fila-caché del
    título ({title, year, poster_url, tmdb_id, media_type}); `rating` solo para
    'rated', `list_id` solo para 'list_add', `movie_id` solo para 'reviewed'
    (ADR-015: enlaza al título reseñado para leer la nota/gate ACTUALES en el
    feed — read-time visibility). `poster_url` ya viene saneado a la allow-list de
    TMDB por el sitio de llamada (nunca src arbitrario). SQL parametrizado
    (PS-002); identificadores/valores en inglés (US-001)."""
    poster = snapshot.get("poster_url") or None
    if poster and not poster.startswith("https://image.tmdb.org/"):
        poster = None   # defensa en profundidad: nunca cachear un poster no-TMDB
    cur.execute(
        "INSERT INTO activity "
        "(user_id, action, tmdb_id, media_type, title, year, poster_url, rating, list_id, movie_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (user_id, action, snapshot.get("tmdb_id"), snapshot.get("media_type"),
         snapshot.get("title"), snapshot.get("year"), poster, rating, list_id, movie_id))


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


# ── Alertas de error (Discord) ────────────────────────────────────────────────
# Manda una alerta REDACTADA a DISCORD_WEBHOOK_ERRORS cuando una excepción NO
# controlada escapa del handler (bug real; los errores esperados los gestiona cada
# endpoint y nunca llegan aquí). Opt-in: sin la env var no se emite nada. El cliente
# NUNCA recibe estos detalles — la traza solo va a tu canal privado de Discord.
_SECRET_ENV_KEYS = (
    "SUPABASE_SERVICE_KEY", "SUPABASE_ANON_KEY", "DATABASE_URL", "TMDB_API_KEY",
    "DISCORD_WEBHOOK_ERRORS", "DISCORD_WEBHOOK_PENDIENTE", "DISCORD_WEBHOOK_VISTA",
    "DISCORD_WEBHOOK_URL", "DB_PASSWORD",
)


def _redact(text):
    """Enmascara secretos antes de mandar una traza a Discord: valores de env
    conocidos, JWTs, pares clave-secreto y credenciales en URLs de Postgres."""
    if not text:
        return text
    for k in _SECRET_ENV_KEYS:
        v = os.environ.get(k, "").strip()
        if v and len(v) >= 8:
            text = text.replace(v, "[REDACTED]")
    # Claves Supabase con prefijo (formato nuevo): sb_secret_… / sb_publishable_…
    text = re.sub(r"sb_(?:secret|publishable)_[A-Za-z0-9]+", "[REDACTED_SUPABASE_KEY]", text)
    # JWTs (header.payload.signature): el prefijo eyJ ya es señal fuerte de JWT.
    text = re.sub(r"eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2}", "[REDACTED_JWT]", text)
    # Bearer <token> separado por espacio (token opaco no-JWT).
    text = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", text)
    # Cabecera Authorization COMPLETA (Basic/Bearer/Digest…) hasta fin de línea: el
    # valor entero es el secreto; un \S+ dejaría escapar 'Basic <base64>' (user:pass).
    text = re.sub(r"(?i)(authorization)\s*[:=]\s*\S[^\r\n]*", r"\1: [REDACTED]", text)
    # clave sensible = valor / clave: valor.
    text = re.sub(r"(?i)(token|password|passwd|secret|api[_-]?key)"
                  r"(['\"]?\s*[:=]\s*['\"]?)\S+", r"\1\2[REDACTED]", text)
    # credenciales en URLs de Postgres.
    text = re.sub(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s]+(@)", r"\1[REDACTED]\2", text)
    return text


# Dedupe/cooldown: si un endpoint entra en bucle de error, la MISMA traza no se
# re-alerta antes de _ALERT_COOLDOWN — evita inundar Discord y una tormenta de
# hilos. La firma es la cola YA redactada (sin secretos en las claves del dict).
_ALERT_LOCK = threading.Lock()
_ALERT_LAST = {}            # firma -> time.monotonic() del último envío
_ALERT_COOLDOWN = 300.0     # segundos


def _should_alert(redacted_tb):
    sig = redacted_tb[-300:]
    now = time.monotonic()
    with _ALERT_LOCK:
        if now - _ALERT_LAST.get(sig, 0.0) < _ALERT_COOLDOWN:
            return False
        _ALERT_LAST[sig] = now
        if len(_ALERT_LAST) > 256:      # poda para acotar memoria
            for k in [k for k, t in _ALERT_LAST.items() if now - t >= _ALERT_COOLDOWN]:
                _ALERT_LAST.pop(k, None)
        return True


def notify_error(tb_text):
    url = os.environ.get("DISCORD_WEBHOOK_ERRORS", "").strip()
    if not url:
        return
    tb = _redact(tb_text or "").strip()
    if not _should_alert(tb):    # misma traza en bucle -> una sola alerta por ventana
        return
    if len(tb) > 1800:           # la COLA de la traza (la línea de la excepción) es lo útil
        tb = "…\n" + tb[-1800:]
    embed = {
        "title":       "🚨 Error no controlado en Cinephora",
        "description": f"```\n{tb}\n```",
        "color":       0xE03131,
    }
    payload = json.dumps({"embeds": [embed]}).encode("utf-8")
    threading.Thread(target=_send_discord, args=(url, payload), daemon=True).start()


# Excepciones benignas: el cliente cerró la conexión. No son bugs → no se alerta.
# TimeoutError se EXCLUYE a propósito: un timeout de urllib contra TMDB/Supabase
# también es TimeoutError y sí queremos enterarnos de una caída/lentitud upstream.
_BENIGN_CONN_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class ErrorNotifyingServer(ThreadingHTTPServer):
    """ThreadingHTTPServer que, ante una excepción NO controlada del handler,
    además de loguear a stderr (Render) manda una alerta redactada a Discord.
    Ignora las desconexiones de cliente para no generar ruido."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        super().handle_error(request, client_address)   # conserva el log a stderr
        if isinstance(exc, _BENIGN_CONN_ERRORS):
            return
        try:
            notify_error(traceback.format_exc())
        except Exception:
            pass


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
        # HSTS solo sobre HTTPS de cara al cliente (RFC 6797): Render termina el
        # TLS y reenvia HTTP + X-Forwarded-Proto: https. Sin esa marca (dev/e2e
        # en http://localhost) no se emite, para no forzar https://localhost.
        # Mismo parseo de primer salto que _client_ip usa para X-Forwarded-For.
        xfproto = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        if xfproto == "https":
            self.send_header("Strict-Transport-Security",
                             "max-age=63072000; includeSubDomains; preload")
        # supabase-js se sirve desde el mismo origen (vendor/) con SRI; no se
        # confia en ningun CDN externo para scripts -> script-src 'self'.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "img-src 'self' https://image.tmdb.org https://*.supabase.co data: blob:; "
            "connect-src 'self' https://*.supabase.co; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'",
        )
        # Permissions-Policy: deniega (allow-list vacia `()`) toda capacidad
        # potente que la app NO usa. Incondicional como la CSP (no gateado por
        # transporte): valido y seguro en http y https. La app no invoca ninguna:
        # el avatar es <input type=file> (no camara/display-capture) y los
        # trailers abren en pestana externa (no reproductor embebido autoplay/
        # fullscreen/encrypted-media). Reduce el radio de impacto de cualquier
        # inyeccion o inclusion de terceros futura (ADR-022, BR-1/BR-1a).
        self.send_header(
            "Permissions-Policy",
            "accelerometer=(), autoplay=(), camera=(), display-capture=(), "
            "encrypted-media=(), fullscreen=(), geolocation=(), gyroscope=(), "
            "magnetometer=(), microphone=(), midi=(), payment=(), usb=()",
        )
        # CORP same-origin: impide que otros sitios incrusten nuestras respuestas
        # como subrecurso cross-origin `no-cors` (superficie de lectura Spectre).
        # No rompe los subrecursos same-origin propios (JS/CSS/imagenes/bundle
        # supabase). La imagen OG (assets/og-cinephora.png) se sirve same-origin y
        # los crawlers la piden como recurso top-level y la re-hostean en su CDN,
        # asi que same-origin no afecta la tarjeta de preview (ADR-022, BR-2/BR-2a).
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        super().end_headers()

    def version_string(self):
        # Oculta la version del stack en el header Server (ADR-022, BR-3): la base
        # stdlib devuelve "SimpleHTTP/0.6 Python/3.14.3", regalando servidor+version
        # exactos para cruzar con CVEs. Un token generico sin "Python"/"SimpleHTTP"
        # elimina esa divulgacion en su origen (Render lo re-emite como
        # x-render-origin-server). send_response() lo llama en cada respuesta.
        return "Cinephora"

    # ── Compresión gzip + Cache-Control por clase (ADR-020) ────────────────────

    def _gzip_eligible(self, ctype):
        """True si el tipo de contenido es texto elegible para gzip (BR-2/BR-3).
        Descarta el sufijo `; charset=…` antes de comparar. png/jpeg/webp (y todo
        lo que no esté en GZIP_TYPES) devuelven False: nunca se recomprimen."""
        base = (ctype or "").split(";", 1)[0].strip().lower()
        return base in GZIP_TYPES

    def _client_accepts_gzip(self):
        """True si el header Accept-Encoding ofrece `gzip` sin desactivarlo con
        `q=0` (BR-1). `gzip;q=0` significa rechazo explícito → no comprimir."""
        for token in self.headers.get("Accept-Encoding", "").split(","):
            parts = token.split(";")
            if parts[0].strip().lower() != "gzip":
                continue
            for p in parts[1:]:
                p = p.strip().lower()
                if p.startswith("q="):
                    try:
                        return float(p[2:]) > 0
                    except ValueError:
                        return False
            return True
        return False

    def _maybe_gzip(self, body, ctype):
        """Comprime `body` solo si el cliente ofrece gzip Y el tipo es elegible Y
        supera el umbral mínimo. Devuelve (bytes, comprimido?). `mtime=0` hace la
        salida determinista (BR-4). El llamante fija Content-Encoding/Content-Length
        sobre los bytes DEVUELTOS solo cuando el segundo elemento es True."""
        if (self._client_accepts_gzip() and self._gzip_eligible(ctype)
                and len(body) >= GZIP_MIN_SIZE):
            return gzip.compress(body, mtime=0), True
        return body, False

    def _cache_control_for(self, ctype):
        """Cache-Control por clase para respuestas estáticas: no-cache para HTML
        (revalida cada uso, un deploy se ve al instante, BR-6); resto de estáticos
        `public, max-age=300, must-revalidate` (BR-5). Nunca `immutable` (los nombres
        de archivo no llevan hash de contenido)."""
        base = (ctype or "").split(";", 1)[0].strip().lower()
        if base == "text/html":
            return "no-cache"
        return "public, max-age=300, must-revalidate"

    def _json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        body, gz = self._maybe_gzip(body, "application/json")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Toda respuesta JSON/dinámica es no-store (BR-7/AS-028): una caché compartida
        # jamás debe guardar una respuesta autenticada y servirla a otro usuario.
        self.send_header("Cache-Control", "no-store")
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
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

    def _tmdb(self, path, extra=None, ttl=None):
        # `ttl` (opcional) anula TMDB_CACHE_TTL SOLO para esta llamada (p. ej. el
        # endpoint de temporada pasa ttl=SEASON_CACHE_TTL, CA-*). Sin él, se usa el
        # default global de 900 s; los llamantes existentes quedan intactos.
        if not os.environ.get("TMDB_API_KEY", "").strip():
            return None
        cache_ttl = TMDB_CACHE_TTL if ttl is None else ttl
        params = {"api_key": os.environ["TMDB_API_KEY"].strip(), "language": "es-ES"}
        if extra:
            params.update(extra)
        url = f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(params)}"

        # Clave de caché: path + params ordenados, EXCLUYENDO api_key (es el
        # secreto y además constante por proceso). Determinista entre llamadas.
        cache_key = (path, tuple(sorted((k, v) for k, v in params.items() if k != "api_key")))
        if cache_ttl > 0:
            now = time.monotonic()
            with _tmdb_cache_lock:
                hit = _tmdb_cache.get(cache_key)
                if hit and hit[0] > now:
                    return hit[1]

        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())   # un error de red/HTTP se propaga (no se cachea)

        if cache_ttl > 0:
            now = time.monotonic()
            with _tmdb_cache_lock:
                if len(_tmdb_cache) >= TMDB_CACHE_MAX:   # purga oportunista de expiradas
                    for k in [k for k, (exp, _) in _tmdb_cache.items() if exp <= now]:
                        del _tmdb_cache[k]
                while len(_tmdb_cache) >= TMDB_CACHE_MAX:  # tope duro: desaloja las más antiguas (FIFO)
                    del _tmdb_cache[next(iter(_tmdb_cache))]
                _tmdb_cache[cache_key] = (now + cache_ttl, data)
        return data

    # ── GET ───────────────────────────────────────────────────────────────────

    @_db_guard
    def do_GET(self):
        path = self.path.split("?", 1)[0]
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
        m = re.match(r"^/api/tv/(\d+)/season/(\d+)$", path)
        if m:                       return self._season(int(m.group(1)), int(m.group(2)))
        if path == "/api/profile":  return self._get_profile()
        if path == "/api/lists":    return self._list_lists()
        if path == "/api/feed":     return self._feed()
        m = re.match(r"^/api/reviews/(\d+)/likes$", path)
        if m:                       return self._review_likes(int(m.group(1)))
        if path == "/api/account/export": return self._export_account()
        m = re.match(r"^/api/follows/([a-z0-9_-]{3,30})$", path)
        if m:                       return self._follow_status(m.group(1))
        if path == "/api/public/username-available": return self._username_available()
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
        # Páginas estáticas informativas/legales con URL limpia (sin auth).
        if path in ("/privacy", "/terms", "/about"):
            self.path = path + ".html"
            return super().do_GET()
        # Fall-through estático: la allow-list se aplica en send_head() (verja única
        # de GET+HEAD). Un path no allow-listed → 404 genérico allí, no aquí.
        return super().do_GET()

    # ── Serving estático: allow-list deny-by-default ───────────────────────────

    def _static_allowlisted(self):
        """Resuelve la petición a un path de archivo confinado a BASE_DIR y decide
        la pertenencia a la allow-list. Devuelve True si el path servido está
        permitido, False en caso contrario. La decisión se toma contra el path
        normalizado por translate_path (que ya resuelve %xx, `.`/`..` y slashes
        duplicados y no puede escapar de BASE_DIR, US-040), de modo que variantes
        codificadas o de traversal no cuelan un path no allow-listed por la verja.
        Los prefijos de directorio (`assets/`, `vendor/`) solo permiten archivos
        regulares existentes debajo — nunca el directorio en sí ni un subdirectorio
        (AC-5)."""
        resolved = Path(self.translate_path(self.path)).resolve()
        try:
            rel = resolved.relative_to(BASE_DIR)
        except ValueError:
            return False                    # fuera de BASE_DIR — nunca servir
        parts = rel.parts
        if not parts:                       # raíz ('/') → shell index.html
            return True
        if len(parts) == 1 and parts[0] in STATIC_FILES:
            return True
        if parts[0] in STATIC_DIRS and resolved.is_file():
            return True
        return False

    def _deny_static(self):
        """Emite el 404 genérico de denegación estática de forma HEAD-safe: mismo
        status + Content-Type/Content-Length + cabeceras de seguridad (vía
        end_headers) que en GET, pero SIN escribir body en HEAD. `send_head()` es el
        único punto de paso de GET y HEAD; el do_HEAD del stdlib nunca copia body, así
        que escribir uno en HEAD viola la semántica HEAD (RFC 7231 §4.3.2) y puede
        corromper el framing en conexiones keep-alive. El 404 de GET conserva su body."""
        body = json.dumps({"ok": False, "error": "No encontrado"},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")   # un 4xx nunca se cachea (AS-028)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_head(self):
        """Único punto de paso de GET (vía super().do_GET) y HEAD (vía do_HEAD del
        base): aplica la allow-list aquí para que HEAD no sea un bypass del gate
        (AC-8). Un path no allow-listed emite el 404 genérico HEAD-safe (con las
        cabeceras de seguridad vía end_headers) y devuelve None para que no se copie
        ningún body de archivo."""
        if not self._static_allowlisted():
            self._deny_static()
            return None
        return self._serve_static()

    def _serve_static(self):
        """Sirve un archivo allow-listed con compresión gzip condicional y
        Cache-Control por clase (ADR-020). Reemplaza la cola `super().send_head()`
        DESPUÉS de que la verja allow-list de send_head haya pasado; la verja NO
        cambia (BR-8). Conserva la revalidación condicional If-Modified-Since → 304
        del stdlib (AC-12) y la paridad HEAD/GET (AC-11): HEAD recorre el MISMO
        camino de cabeceras —incluida la compresión para obtener el Content-Length
        COMPRIMIDO— y anuncia el mismo Content-Encoding + Content-Length + Cache-Control
        que el GET equivalente, sin body (do_HEAD del base descarta el objeto devuelto).
        Nunca acorta a la longitud sin comprimir en HEAD (ese es el bug de framing)."""
        path = self.translate_path(self.path)
        if os.path.isdir(path):                        # solo la raíz '/' llega aquí:
            path = os.path.join(path, "index.html")    # la allow-list deniega otros dirs
        try:
            f = open(path, "rb")
        except OSError:
            self._deny_static()
            return None
        with f:
            fs    = os.fstat(f.fileno())
            ctype = self.guess_type(path)
            # Revalidación condicional (AC-12): mismo chequeo que SimpleHTTPRequestHandler.
            # Un 304 no lleva body ni Content-Encoding, pero SÍ el Cache-Control de clase y
            # Vary, para no perder la política en el (frecuente) camino condicional.
            if ("If-Modified-Since" in self.headers
                    and "If-None-Match" not in self.headers):
                try:
                    ims = email.utils.parsedate_to_datetime(self.headers["If-Modified-Since"])
                except (TypeError, IndexError, OverflowError, ValueError):
                    ims = None
                if ims is not None:
                    if ims.tzinfo is None:
                        ims = ims.replace(tzinfo=timezone.utc)
                    if ims.tzinfo is timezone.utc:
                        last_modif = datetime.fromtimestamp(fs.st_mtime, timezone.utc)
                        last_modif = last_modif.replace(microsecond=0)
                        if last_modif <= ims:
                            self.send_response(304)
                            self.send_header("Cache-Control", self._cache_control_for(ctype))
                            self.send_header("Vary", "Accept-Encoding")
                            self.end_headers()
                            return None
            raw = f.read()
        body, gz = self._maybe_gzip(raw, ctype)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
        self.send_header("Cache-Control", self._cache_control_for(ctype))
        # Vary en TODA respuesta estática —comprimida y sin comprimir— para que una
        # caché compartida separe las variantes gzip/identity (AC-13).
        self.send_header("Vary", "Accept-Encoding")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        return io.BytesIO(body)

    def list_directory(self, path):
        """Sin listado de directorios jamás (BR-3/AC-5): defensa en profundidad
        detrás de la verja de send_head. Cualquier path de directorio → 404 genérico
        HEAD-safe, nunca un índice navegable del árbol interno."""
        self._deny_static()
        return None

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
        cast      = [
            {"name": c.get("name"), "profile_path": c.get("profile_path") or ""}
            for c in (d.get("credits", {}) or {}).get("cast", [])[:8] if c.get("name")
        ]
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
        # Marcas del usuario para esta serie (AC-6/AC-9): valor POR-USUARIO, así que
        # es una query real scoped por user_id + tmdb_id — NUNCA de la caché _tmdb
        # compartida. Permite pintar «N/M episodios» en la recarga, sin esperar a una
        # marca de la sesión. 0 para películas (BR-1: no tienen episodios).
        watched_count = 0
        if mt == "tv":
            with get_db() as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM episode_progress "
                    "WHERE user_id = %s AND tmdb_id = %s",
                    (user_id, int(tid)))
                watched_count = cur.fetchone()["n"]
        self._json(200, {"ok": True, "details": {
            "overview":       d.get("overview") or "Sin sinopsis disponible.",
            "genres":         [g["name"] for g in d.get("genres", [])],
            "genre_ids":      [g["id"] for g in d.get("genres", []) if g.get("id")],
            "runtime":        runtime,
            "title":          d.get("title") or d.get("name") or "",
            "poster_path":    d.get("poster_path") or "",
            "backdrop_path":  d.get("backdrop_path") or "",
            "vote_average":   round(d.get("vote_average") or 0, 1),
            "trailer":        trailer,
            "dir_label":      dir_label,
            "directors":      directors,
            "cast":           cast,
            "providers":      providers,
            "providers_link": wp_es.get("link", ""),
            "total_seasons":  d.get("number_of_seasons") if mt == "tv" else None,
            # Aditivos series (BR-7 / AC-6): el total de episodios (denominador N/M)
            # y las temporadas (menos la 0/especiales) que pueblan el selector. Ambos
            # salen de la `d` ya obtenida — sin llamada TMDB extra (API-019 aditivo).
            "total_episodes": d.get("number_of_episodes") if mt == "tv" else None,
            "seasons": [
                {"season_number": s.get("season_number"), "name": s.get("name"),
                 "episode_count": s.get("episode_count")}
                for s in d.get("seasons", [])
                if s.get("season_number") not in (None, 0)
            ] if mt == "tv" else None,
            # Recuento por-usuario de episodios marcados (AC-6/AC-9): el numerador N.
            "watched_count":  watched_count,
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

    def _season(self, tmdb_id, season):
        """GET /api/tv/{tmdb_id}/season/{n} — metadatos de una temporada desde TMDB
        (still, número + título, fecha de emisión, duración, sinopsis) fusionados con
        las marcas de vista del usuario. Autenticado (PS-001) y rate-limited (PS-005:
        pega a TMDB). Proyección allow-list; `watched` por episodio deriva de las
        marcas. Error crudo de TMDB nunca se serializa (invariants → 502 genérico)."""
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        if self._rate_limited(user_id):
            return
        if not os.environ.get("TMDB_API_KEY", "").strip():
            return self._json(200, {"ok": False, "needs_key": True})
        try:
            d = self._tmdb(f"/tv/{tmdb_id}/season/{season}", ttl=SEASON_CACHE_TTL)
        except Exception:
            return self._json(502, {"ok": False, "error": "No se pudo consultar TMDB."})
        # Marcas del usuario para esta temporada en UNA query (PS-002), scoped por
        # user_id (PS-001). Set de episodios vistos para el flag `watched`.
        with get_db() as cur:
            cur.execute(
                "SELECT season, episode FROM episode_progress "
                "WHERE user_id = %s AND tmdb_id = %s AND season = %s",
                (user_id, tmdb_id, season))
            watched = {r["episode"] for r in cur.fetchall()}
        episodes = [
            {
                "episode_number": e.get("episode_number"),
                "name":           e.get("name"),
                "air_date":       e.get("air_date"),
                "runtime":        e.get("runtime"),
                "overview":       e.get("overview"),
                "still_path":     e.get("still_path"),
                "watched":        e.get("episode_number") in watched,
            }
            for e in (d.get("episodes") or [])
        ]
        self._json(200, {"ok": True, "season": {
            "season_number": d.get("season_number"),
            "name":          d.get("name"),
            "episodes":      episodes,
        }})

    # ── POST ──────────────────────────────────────────────────────────────────

    @_db_guard
    def do_POST(self):
        if self.path == "/api/account/delete":
            return self._delete_account()
        if self.path == "/api/account/import":
            return self._import_account()
        if self.path == "/api/lists":
            return self._create_list()
        if self.path == "/api/follows":
            return self._follow()
        m = re.match(r"^/api/lists/([0-9a-fA-F-]{36})/items$", self.path)
        if m:
            return self._add_list_item(m.group(1))
        m = re.match(r"^/api/reviews/(\d+)/likes$", self.path)
        if m:
            return self._like_review(int(m.group(1)))
        m = re.match(r"^/api/movies/(\d+)/episodes$", self.path)
        if m:
            return self._set_episodes(int(m.group(1)))
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
            # Evento social (ADR-014): un alta directa como 'vista' es actividad
            # 'watched'. Append en la MISMA transacción, ÚLTIMO en el bloque, sin
            # pre-check que pueda 500 — el contrato de éxito/fallo del alta (201/
            # 409/400) queda intacto. 'pendiente'/'viendo'/'abandonada' → sin evento.
            if status == "vista":
                _record_activity(cur, user_id, "watched", {
                    "tmdb_id": tmdb_id, "media_type": media_type, "title": title,
                    "year": year, "poster_url": poster})

        notify_discord(title, year, status, media_type, poster, user_id)
        self._json(201, {"ok": True, "id": new_id})

    def _set_episodes(self, movie_id):
        """POST /api/movies/{id}/episodes — marca/desmarca episodios (uno o toda la
        temporada) de una serie de la colección. Un solo endpoint (no DELETE) con un
        booleano `watched`, evitando el caveat de cuerpo en DELETE (RFC 9110 §9.3.5;
        misma razón que ADR-009). No pega a TMDB → sigue el patrón de PATCH (auth +
        scoping, sin rate limiter). Deriva y sincroniza current_season/current_episode
        (BR-8) en la MISMA transacción vía _recompute_progress."""
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "JSON inválido"})
        # Validación de cuerpo (US-040): season int≥1; watched bool obligatorio;
        # episode int≥1|null; episodes lista de int≥1|null. bool es subclase de int
        # en Python, por eso se excluye explícitamente donde se espera un entero.
        season = data.get("season")
        if not isinstance(season, int) or isinstance(season, bool) or season < 1:
            return self._json(400, {"ok": False, "error": "Parámetros inválidos"})
        watched = data.get("watched")
        if not isinstance(watched, bool):
            return self._json(400, {"ok": False, "error": "Parámetros inválidos"})
        episode = data.get("episode")
        if episode is not None and (not isinstance(episode, int) or isinstance(episode, bool) or episode < 1):
            return self._json(400, {"ok": False, "error": "Parámetros inválidos"})
        episodes = data.get("episodes")
        if episodes is not None:
            if not isinstance(episodes, list) or not all(
                    isinstance(n, int) and not isinstance(n, bool) and n >= 1 for n in episodes):
                return self._json(400, {"ok": False, "error": "Parámetros inválidos"})
        # Números de episodio afectados: `episode` (uno) ∪ `episodes` (lote). Al
        # desmarcar sin ninguno de los dos → toda la temporada (episodes vacíos).
        ep_nums = []
        if episode is not None:
            ep_nums.append(episode)
        if episodes:
            ep_nums.extend(episodes)
        ep_nums = sorted(set(ep_nums))

        with get_db() as cur:
            # Resolver el título por (id, user_id) — IDOR-safe: id ajeno/inexistente
            # es indistinguible (404). 400 si no es serie (BR-1). PS-001 scoping.
            cur.execute(
                "SELECT tmdb_id, media_type FROM movies WHERE id = %s AND user_id = %s",
                (movie_id, user_id))
            row = cur.fetchone()
            if row is None:
                return self._json(404, {"ok": False, "error": "No encontrada"})
            if row["media_type"] != "tv":
                return self._json(400, {"ok": False, "error": "Solo aplica a series"})
            tmdb_id = row["tmdb_id"]
            if tmdb_id is None:
                # Una serie sin tmdb_id no tiene metadatos de temporada; nada que marcar.
                return self._json(400, {"ok": False, "error": "La serie no tiene datos de TMDB"})
            if watched:
                # Marcar exige episodios concretos (uno o lote). Sin ninguno → nada
                # que insertar (no existe "marcar toda la temporada" sin sus números).
                for ep in ep_nums:
                    cur.execute(
                        "INSERT INTO episode_progress (user_id, tmdb_id, season, episode) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        (user_id, tmdb_id, season, ep))
            else:
                # Desmarcar: episodios concretos si se indican, si no toda la temporada.
                if ep_nums:
                    cur.execute(
                        "DELETE FROM episode_progress WHERE user_id = %s AND tmdb_id = %s "
                        "AND season = %s AND episode = ANY(%s)",
                        (user_id, tmdb_id, season, ep_nums))
                else:
                    cur.execute(
                        "DELETE FROM episode_progress WHERE user_id = %s AND tmdb_id = %s "
                        "AND season = %s",
                        (user_id, tmdb_id, season))
            # Deriva-y-sincroniza (BR-8/PS-004) en la MISMA transacción.
            watched_count, current_season, current_episode = self._recompute_progress(
                cur, user_id, tmdb_id, movie_id)
        self._json(200, {"ok": True, "current_season": current_season,
                         "current_episode": current_episode, "watched_count": watched_count})

    def _recompute_progress(self, cur, user_id, tmdb_id, movie_id):
        """Deriva la posición «¿dónde voy?» del MÁXIMO episodio visto y la sincroniza
        en movies.current_season/current_episode (BR-8). NULL/NULL cuando no quedan
        marcas. Corre DENTRO de la transacción del llamante (recibe el cursor).
        Devuelve (watched_count, current_season, current_episode)."""
        cur.execute(
            "SELECT season, episode FROM episode_progress "
            "WHERE user_id = %s AND tmdb_id = %s "
            "ORDER BY season DESC, episode DESC LIMIT 1",
            (user_id, tmdb_id))
        top = cur.fetchone()
        current_season  = top["season"]  if top else None
        current_episode = top["episode"] if top else None
        cur.execute(
            "UPDATE movies SET current_season = %s, current_episode = %s "
            "WHERE id = %s AND user_id = %s",
            (current_season, current_episode, movie_id, user_id))
        cur.execute(
            "SELECT count(*) AS n FROM episode_progress WHERE user_id = %s AND tmdb_id = %s",
            (user_id, tmdb_id))
        watched_count = cur.fetchone()["n"]
        return watched_count, current_season, current_episode

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
        new_rating = None   # rating no-nulo (1-5) puesto por ESTE PATCH → evento 'rated'
        note_in_patch = None   # texto de la nota si ESTE PATCH la trae (para el gate de publicación)
        want_public = None     # note_public solicitado por ESTE PATCH (bool) o None si no viene

        if data.get("status") in ("pendiente", "viendo", "vista", "abandonada"):
            new_status = data["status"]
            fields.append("status = %s"); values.append(new_status)
        if "rating" in data:
            r = data["rating"]
            if r is not None and (not isinstance(r, int) or not 1 <= r <= 5):
                return self._json(400, {"ok": False, "error": "rating debe ser 1-5 o null"})
            fields.append("rating = %s"); values.append(r)
            new_rating = r
        if "note" in data:
            if data["note"] is not None and not isinstance(data["note"], str):
                return self._json(400, {"ok": False, "error": "note debe ser texto o null"})
            note = "" if data["note"] is None else data["note"].strip()
            if len(note) > 500:
                return self._json(400, {"ok": False, "error": "La nota no puede superar 500 caracteres"})
            fields.append("note = %s"); values.append(note)
            note_in_patch = note
        if "note_public" in data:
            # ADR-015: opt-in per-título de publicación de la nota como reseña.
            # Validación booleana estricta (US-040); scoped WHERE id=%s AND user_id=%s (AC-16).
            if not isinstance(data["note_public"], bool):
                return self._json(400, {"ok": False, "error": "note_public debe ser booleano"})
            want_public = data["note_public"]
            fields.append("note_public = %s"); values.append(want_public)
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
        publish_transition = False   # false→true de note_public en ESTE PATCH (→ evento 'reviewed')
        with get_db() as cur:
            if new_status == "vista" and "watched_at" not in data:
                cur.execute(
                    "SELECT watched_at FROM movies WHERE id = %s AND user_id = %s",
                    (movie_id, user_id))
                current = cur.fetchone()
                if current and not current["watched_at"]:
                    fields.append("watched_at = %s")
                    values.append(date.today().isoformat())
            # ADR-015: al PUBLICAR (want_public=True) se exige una nota resultante no
            # vacía (AC-3), y se detecta la transición false→true para escribir UN
            # evento 'reviewed' fresco. Lee el estado ACTUAL (note_public + note) en
            # la misma transacción, antes del UPDATE. Scoped por user_id (AC-16).
            if want_public is True:
                cur.execute(
                    "SELECT note_public, note FROM movies WHERE id = %s AND user_id = %s",
                    (movie_id, user_id))
                prev = cur.fetchone()
                if prev is None:
                    return self._json(404, {"ok": False, "error": "No encontrada"})
                # Nota resultante = la del PATCH si viene, si no la almacenada.
                resulting_note = note_in_patch if note_in_patch is not None else (prev["note"] or "")
                if not resulting_note.strip():
                    return self._json(400, {"ok": False, "error": "Escribe una nota antes de publicarla."})
                publish_transition = not prev["note_public"]
            values.extend([movie_id, user_id])
            cur.execute(
                f"UPDATE movies SET {', '.join(fields)} WHERE id = %s AND user_id = %s",
                values)
            if cur.rowcount == 0:
                return self._json(404, {"ok": False, "error": "No encontrada"})
            # Snapshot re-seleccionado si hace falta para Discord (new_status) O
            # para un evento social ('vista' → watched, rating no-nulo → rated).
            # Añade tmdb_id al SELECT existente para la caché del feed; una sola
            # query (sin round-trip extra).
            need_watched = new_status == "vista"
            need_rated   = new_rating is not None
            if new_status or need_rated or publish_transition:
                cur.execute(
                    "SELECT title, year, poster_url, media_type, tmdb_id FROM movies "
                    "WHERE id = %s AND user_id = %s",
                    (movie_id, user_id))
                row = cur.fetchone()
            # Evento(s) social(es) (ADR-014/ADR-015): append en la MISMA transacción,
            # ÚLTIMO en el bloque, sin pre-check que pueda 500 — el contrato de la
            # mutación (200/404/400, Discord, dedup) queda intacto. Un PATCH puede
            # disparar varios (watched + rated + reviewed) → varios eventos.
            if row:
                snap = {"tmdb_id": row["tmdb_id"], "media_type": row["media_type"],
                        "title": row["title"], "year": row["year"],
                        "poster_url": row["poster_url"]}
                if need_watched:
                    _record_activity(cur, user_id, "watched", snap)
                if need_rated:
                    _record_activity(cur, user_id, "rated", snap, rating=new_rating)
                # ADR-015: en la transición false→true escribe UN evento 'reviewed'
                # FRESCO. Borra los 'reviewed' previos de ese título (única excepción
                # deliberada al append-only de activity: la reseña es estado-actual)
                # e inserta uno → republicar resurge la reseña arriba en los feeds sin
                # duplicados. La visibilidad la decide el gate en tiempo de lectura.
                if publish_transition:
                    cur.execute(
                        "DELETE FROM activity WHERE user_id = %s AND action = 'reviewed' AND movie_id = %s",
                        (user_id, movie_id))
                    _record_activity(cur, user_id, "reviewed", snap, movie_id=movie_id)

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
        mf = re.match(r"^/api/follows/([a-z0-9_-]{3,30})$", self.path)
        if mf:
            return self._unfollow(mf.group(1))
        mr = re.match(r"^/api/reviews/(\d+)/likes$", self.path)
        if mr:
            return self._unlike_review(int(mr.group(1)))
        m = re.match(r"^/api/movies/(\d+)$", self.path)
        if not m:
            return self._json(404, {"ok": False, "error": "Ruta no encontrada"})
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        movie_id = int(m.group(1))
        with get_db() as cur:
            # Metadatos previos al borrado para la purga de marcas de episodio
            # (prevención de huérfanos: no hay FK/cascade — ver migración 004).
            cur.execute(
                "SELECT media_type, tmdb_id FROM movies WHERE id = %s AND user_id = %s",
                (movie_id, user_id))
            row = cur.fetchone()
            cur.execute(
                "DELETE FROM movies WHERE id = %s AND user_id = %s",
                (movie_id, user_id))
            if cur.rowcount == 0:
                return self._json(404, {"ok": False, "error": "No encontrada"})
            # Solo series con tmdb_id: evita borrar las marcas de una serie con el
            # mismo entero como id cuando se elimina una película (GD-*, orphan-prev).
            if row and row["media_type"] == "tv" and row["tmdb_id"] is not None:
                cur.execute(
                    "DELETE FROM episode_progress WHERE user_id = %s AND tmdb_id = %s",
                    (user_id, row["tmdb_id"]))
        self._json(200, {"ok": True})

    # ── Borrado de cuenta (RTBF) ─────────────────────────────────────────────────

    def _supabase_verify_password(self, email, password):
        """Re-verifica la contraseña actual contra Supabase Auth server-side
        (POST /auth/v1/token?grant_type=password con la anon key pública).
        Devuelve True solo si Supabase responde 200. Cualquier no-200 o error de
        red/urllib → False (el llamante responde 401 genérico). La contraseña
        viaja solo en el cuerpo TLS de esta petición; nunca se registra."""
        base = supabase_base_url()
        anon = os.environ.get("SUPABASE_ANON_KEY", "").strip()
        if not base or not anon:
            return False
        url = f"{base}/auth/v1/token?grant_type=password"
        body = json.dumps({"email": email, "password": password}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "apikey":       anon,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return 200 <= resp.status < 300
        except Exception:
            # HTTPError (contraseña incorrecta → 400) y URLError (red) → fallo.
            # El error crudo de Supabase nunca se propaga al cliente.
            return False

    def _supabase_admin_delete_user(self, user_id):
        """Borra el usuario de Supabase Auth vía la admin API
        (DELETE /auth/v1/admin/users/{id}) con la service_role key (solo server).
        Devuelve True en 2xx; cualquier no-2xx o error → False (el llamante
        responde 500 genérico). La service_role key nunca llega al cliente ni a
        un log."""
        base = supabase_base_url()
        service = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
        if not base or not service:
            return False
        url = f"{base}/auth/v1/admin/users/{user_id}"
        req = urllib.request.Request(url, method="DELETE", headers={
            "apikey":        service,
            "Authorization": f"Bearer {service}",
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False

    # ── Avatar en Supabase Storage (service_role, solo servidor) ─────────────────

    def _storage_avatar_key(self, user_id):
        """Clave de objeto determinista y con dueño: `{user_id}/avatar.webp`.
        Un objeto por usuario, clave fija (FS-*: la clave siempre lleva el owner_id
        como invariante limitadora de fugas; el upsert sobrescribe, no acumula)."""
        return f"{user_id}/avatar.webp"

    def _storage_public_avatar_url(self, user_id, version):
        """URL pública canónica del avatar con cache-buster `?v={version}`. La clave
        es fija, así que el `?v=` (epoch) fuerza a los navegadores a re-leer los bytes
        nuevos tras un reemplazo. `avatar_url` lo DERIVA el servidor: el cliente nunca
        envía una URL (mitiga img-src-injection)."""
        base = supabase_base_url()
        return (f"{base}/storage/v1/object/public/avatars/"
                f"{self._storage_avatar_key(user_id)}?v={version}")

    def _supabase_storage_head_avatar(self, user_id):
        """HEAD service_role al objeto de avatar para confirmar que el cliente lo
        subió client-direct. Devuelve True en 2xx, False en 404 / cualquier no-2xx /
        error de red. Usado por la acción `set`. La service_role key nunca llega al
        cliente ni a un log."""
        base = supabase_base_url()
        service = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
        if not base or not service:
            return False
        url = f"{base}/storage/v1/object/avatars/{self._storage_avatar_key(user_id)}"
        req = urllib.request.Request(url, method="HEAD", headers={
            "apikey":        service,
            "Authorization": f"Bearer {service}",
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return 200 <= resp.status < 300
        except Exception:
            # HTTPError (404 objeto ausente) y URLError (red) → False. El error
            # crudo de Supabase nunca se propaga al cliente.
            return False

    def _supabase_storage_delete_avatar(self, user_id):
        """DELETE service_role del objeto de avatar. Idempotente: un 404 (objeto ya
        ausente) se trata como éxito. Devuelve True en 2xx o 404, False en cualquier
        otro no-2xx / error de red. Usado por `remove` y por `_delete_account` (RTBF).
        La service_role key nunca llega al cliente ni a un log."""
        base = supabase_base_url()
        service = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
        if not base or not service:
            return False
        url = f"{base}/storage/v1/object/avatars/{self._storage_avatar_key(user_id)}"
        req = urllib.request.Request(url, method="DELETE", headers={
            "apikey":        service,
            "Authorization": f"Bearer {service}",
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            # 404 = objeto ya ausente → idempotente, éxito. Otro código → fallo.
            return e.code == 404
        except Exception:
            return False

    def _delete_account(self):
        """POST /api/account/delete — borrado permanente e irreversible de la
        cuenta y todos los datos personales del usuario autenticado (RTBF).

        Flujo (orden vinculante): auth JWT (401) → rate limit por usuario+global
        (429) → validación del cuerpo password/confirm_username (400) → email del
        payload JWT VERIFICADO (401 si falta) → confirm_username == profiles.username
        (400) → re-verificación server-side de la contraseña contra Supabase Auth
        (401) → UNA transacción borrando movies/lists(→list_items cascade)/profiles
        WHERE user_id=%s → borrado del usuario de Supabase Auth vía admin API (500
        si falla; DB ya comprometida, reintento idempotente) → 200 {ok:true}.

        Todos los cuerpos de error son genéricos es-ES; el error crudo de
        Supabase/DB/urllib nunca se serializa al cliente. Emite _audit redactado
        (user_hash) en éxito y en cada denegación."""
        # 1) Auth: solo con JWT válido se llega al endpoint (PS-001). El email
        #    sale del payload VERIFICADO, nunca del cuerpo del cliente.
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        user_id, email = verify_jwt_identity(token)
        if not user_id:
            # Sin user_id no hay user_hash (caso no autenticado): _audit emite
            # "user_hash": null vía el guard None de _hash_user_id, con el MISMO
            # esquema (action/user_hash/target/timestamp) que las demás denegaciones.
            _audit("account.delete_denied", None, "unauthenticated")
            return self._json(401, {"ok": False, "error": "No autenticado"})

        # 2) Rate limit inmediatamente tras la auth (oráculo de contraseña).
        allowed, retry = rate_check([(f"account-delete:{user_id}", ACCOUNT_DELETE_MAX),
                                     ("account-delete:_global", ACCOUNT_DELETE_GLOBAL)])
        if not allowed:
            _audit("account.delete_denied", user_id, "rate_limited")
            return self._json(429, {"ok": False, "error": "Demasiados intentos, espera un momento."},
                              extra_headers={"Retry-After": retry})

        # 3) Cuerpo: password + confirm_username presentes y no vacíos (US-040).
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError):
            _audit("account.delete_denied", user_id, "incomplete")
            return self._json(400, {"ok": False, "error": "Datos incompletos"})
        password = data.get("password")
        confirm_username = data.get("confirm_username")
        if not isinstance(password, str) or not password \
                or not isinstance(confirm_username, str) or not confirm_username.strip():
            _audit("account.delete_denied", user_id, "incomplete")
            return self._json(400, {"ok": False, "error": "Datos incompletos"})

        # 4) Email del JWT verificado obligatorio para la re-verificación de la
        #    contraseña. Ausente → tratar como fallo de re-auth (401 genérico).
        if not email:
            _audit("account.delete_denied", user_id, "bad_password")
            return self._json(401, {"ok": False, "error": "Contraseña incorrecta."})

        # 5) Confirmación del username: coincidencia exacta con profiles.username
        #    (valor almacenado en minúsculas), whitespace externo recortado.
        with get_db() as cur:
            cur.execute("SELECT username FROM profiles WHERE user_id = %s", (user_id,))
            prow = cur.fetchone()
        stored_username = prow["username"] if prow else None
        if not stored_username or confirm_username.strip() != stored_username:
            _audit("account.delete_denied", user_id, "username_mismatch")
            return self._json(400, {"ok": False, "error": "La confirmación no coincide."})

        # 6) Re-verificación server-side de la contraseña contra Supabase Auth.
        if not self._supabase_verify_password(email, password):
            _audit("account.delete_denied", user_id, "bad_password")
            return self._json(401, {"ok": False, "error": "Contraseña incorrecta."})

        # 7) Borrado de datos Cinephora: UNA transacción atómica, todo por user_id
        #    (PS-002). list_items cae por FK ON DELETE CASCADE al borrar lists.
        #    Idempotente: en un reintento los borrados afectan a cero filas.
        with get_db() as cur:
            cur.execute("DELETE FROM movies WHERE user_id = %s", (user_id,))
            # RTBF de las marcas de episodio (GD-*/BR-10/AC-13): la tabla no tiene FK
            # a movies, así que la erasure es explícita (no cae por cascade).
            cur.execute("DELETE FROM episode_progress WHERE user_id = %s", (user_id,))
            # RTBF de la capa social (ADR-014/GD-*): borrar activity + follows
            # ANTES de lists, para que el cascade de activity.list_id (ON DELETE
            # CASCADE al borrar lists) no compita con este borrado explícito.
            # follows en AMBAS direcciones: el usuario desaparece también de las
            # listas de seguidores/seguidos de terceros (AC-16).
            cur.execute("DELETE FROM activity WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM follows WHERE follower_id = %s OR followed_id = %s",
                        (user_id, user_id))
            # RTBF Fase 2 (ADR-015/GD-*): borra los likes que el usuario DIO sobre
            # reseñas de terceros (scoped liker_id, AC-17). Los likes RECIBIDOS y sus
            # eventos 'reviewed' caen por el cascade de movies (sus reseñas son sus
            # movies). Va antes del borrado de movies; el orden es indiferente
            # (liker_id vs el cascade por movie_id son conjuntos disjuntos aquí).
            cur.execute("DELETE FROM likes WHERE liker_id = %s", (user_id,))
            cur.execute("DELETE FROM lists WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM profiles WHERE user_id = %s", (user_id,))

        # 8) Borrado del objeto de avatar en Storage (RTBF, GD-*): ningún dato
        #    personal debe sobrevivir a la erasure. Orden DB → avatar → auth: la
        #    erasure de DB/auth es la primaria; un fallo aquí se AUDITA (redactado,
        #    user_hash) pero NO aborta el borrado de cuenta (idempotente en reintento).
        if not self._supabase_storage_delete_avatar(user_id):
            _audit("account.avatar_erase_failed", user_id, "avatar")

        # 9) Borrado del usuario de Supabase Auth (admin API, service_role key).
        #    Tras el commit de la DB: si falla, las filas ya se fueron y el
        #    reintento re-intenta el borrado del auth user (idempotente).
        if not self._supabase_admin_delete_user(user_id):
            _audit("account.delete_denied", user_id, "auth_delete_failed")
            return self._json(500, {"ok": False,
                                    "error": "No se pudo completar la eliminación. Inténtalo de nuevo."})

        _audit("account.deleted", user_id, "account")
        self._json(200, {"ok": True})

    def _export_account(self):
        """GET /api/account/export — exportación de datos (portabilidad GDPR,
        Art. 20). Solo lectura, autenticado, acotado a la propia cuenta.

        Orden: auth (401) → rate limit por-usuario+global (429) → UNA lectura en
        un solo bloque get_db() de perfil + colección + listas + items (todas
        WHERE user_id = %s, PS-002) → 200 con el documento `export` versionado.

        Proyecciones allow-list: excluyen share_token, user_id y los ids internos
        (el id de lista solo se usa para agrupar items en Python y se descarta).
        Cuerpos de error genéricos es-ES; el error crudo nunca se serializa
        (invariants). Emite _audit redactado (user_hash) en éxito y en cada
        denegación (AU-007), como delete-account."""
        # 1) Auth primero (PS-001). Sin JWT válido no hay identidad: user_hash null.
        user_id = self._get_user_id()
        if not user_id:
            _audit("account.export_denied", None, "unauthenticated")
            return self._json(401, {"ok": False, "error": "No autenticado"})

        # 2) Rate limit inmediatamente tras la auth (guarda DoS del endpoint más
        #    pesado). Al superar cualquier bucket → 429 + Retry-After.
        allowed, retry = rate_check([(f"account-export:{user_id}", ACCOUNT_EXPORT_MAX),
                                     ("account-export:_global", ACCOUNT_EXPORT_GLOBAL)])
        if not allowed:
            _audit("account.export_denied", user_id, "rate_limited")
            return self._json(429, {"ok": False, "error": "Demasiadas solicitudes, espera un momento."},
                              extra_headers={"Retry-After": retry})

        # 3) Lecturas: un solo bloque get_db(), toda query parametrizada y
        #    filtrada por user_id (PS-002). Items en UNA query joined (sin N+1).
        with get_db() as cur:
            cur.execute(
                "SELECT username, is_public, show_collection, show_stats "
                "FROM profiles WHERE user_id = %s",
                (user_id,))
            prow = cur.fetchone()
            if prow:
                profile = dict(prow)
            else:
                # Defaults perezosos (mismos que _get_profile): documento válido.
                profile = {"username": None, "is_public": False,
                           "show_collection": False, "show_stats": False}

            # Colección: allow-list explícita — id y user_id EXCLUIDOS (GD-001).
            cur.execute(
                "SELECT tmdb_id, media_type, title, year, poster_url, status, "
                "       rating, note, watched_at, platform, current_season, "
                "       current_episode, total_seasons, genres, created_at "
                "FROM movies WHERE user_id = %s ORDER BY created_at DESC",
                (user_id,))
            collection = [dict(r) for r in cur.fetchall()]

            # Listas: allow-list — share_token y user_id EXCLUIDOS. El id se trae
            # solo para agrupar los items y se descarta antes de emitir.
            cur.execute(
                "SELECT id, name, visibility, created_at, updated_at "
                "FROM lists WHERE user_id = %s ORDER BY updated_at DESC",
                (user_id,))
            list_rows = cur.fetchall()

            # TODOS los items en UNA query joined, acotada por l.user_id (sin N+1).
            cur.execute(
                "SELECT li.list_id, li.tmdb_id, li.media_type, li.title, "
                "       li.year, li.poster_url, li.position "
                "FROM list_items li JOIN lists l ON l.id = li.list_id "
                "WHERE l.user_id = %s ORDER BY li.position, li.created_at",
                (user_id,))
            item_rows = cur.fetchall()

        # Agrupar los items por list_id en Python (sin N+1).
        items_by_list = {}
        for it in item_rows:
            items_by_list.setdefault(it["list_id"], []).append({
                "tmdb_id":    it["tmdb_id"],
                "media_type": it["media_type"],
                "title":      it["title"],
                "year":       it["year"],
                "poster_url": it["poster_url"],
                "position":   it["position"],
            })
        # El id de lista se usa solo para agrupar; se descarta del objeto emitido.
        lists = [{
            "name":       lr["name"],
            "visibility": lr["visibility"],
            "created_at": lr["created_at"],
            "updated_at": lr["updated_at"],
            "items":      items_by_list.get(lr["id"], []),
        } for lr in list_rows]

        export = {
            "schema_version": 1,
            "exported_at":    datetime.now(timezone.utc).isoformat(),
            "profile":        profile,
            "collection":     collection,
            "lists":          lists,
        }
        _audit("account.exported", user_id, "account")
        self._json(200, {"ok": True, "export": export})

    def _import_account(self):
        """POST /api/account/import — importación de datos (round-trip inverso del
        export, ADR-011). Escritura autenticada, aditiva y NO destructiva, acotada
        a la propia cuenta: solo INSERTs, nunca UPDATE/DELETE de filas existentes.

        Orden (vinculante): auth (401) → rate limit por-usuario+global (429) →
        lectura ACOTADA del cuerpo con tope propio MAX_IMPORT_BODY (Content-Length
        > tope → 413 SIN leer el cuerpo; NO usa _read_json/MAX_BODY) → json.loads
        (fallo → 400) → puerta de formato/versión (objeto + schema_version==1 +
        collection/lists arrays, si no → 422) → topes de conteo elementos/listas
        (413) → UNA transacción atómica get_db() de INSERTs validados y
        `user_id`-scoped (PS-002) → 200 con el resumen de 8 contadores.

        El archivo es NO confiable: cualquier user_id que declare y su bloque
        `profile` se IGNORAN (AC-12) — el target de escritura es SIEMPRE el `sub`
        del JWT. Un item inválido se OMITE + cuenta (nunca aborta el import); un
        problema a nivel de archivo (formato/tamaño/parse) rechaza todo. Cuerpos de
        error genéricos es-ES; el error crudo de DB/parse nunca se serializa
        (invariants). Emite _audit redactado (user_hash) en éxito y en cada
        denegación (AU-007), como export/delete-account."""
        # 1) Auth primero (PS-001, AC-14). Sin JWT válido no hay identidad: null.
        user_id = self._get_user_id()
        if not user_id:
            _audit("account.import_denied", None, "unauthenticated")
            return self._json(401, {"ok": False, "error": "No autenticado"})

        # 2) Rate limit inmediatamente tras la auth (guarda DoS de la escritura).
        allowed, retry = rate_check([(f"account-import:{user_id}", ACCOUNT_IMPORT_MAX),
                                     ("account-import:_global", ACCOUNT_IMPORT_GLOBAL)])
        if not allowed:
            _audit("account.import_denied", user_id, "rate_limited")
            return self._json(429, {"ok": False, "error": "Demasiadas solicitudes, espera un momento."},
                              extra_headers={"Retry-After": retry})

        # 3) Lectura ACOTADA del cuerpo con tope PROPIO (1 MB), NO el _read_json de
        #    64 KB. Content-Length sobre el tope → 413 SIN leer el cuerpo grande
        #    (guarda DoS/memoria); si no, leer exactamente min(CL, tope) y parsear.
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            content_length = 0
        if content_length > MAX_IMPORT_BODY:
            _audit("account.import_denied", user_id, "too_large")
            return self._json(413, {"ok": False, "error": "El archivo es demasiado grande."})
        length = max(0, min(content_length, MAX_IMPORT_BODY))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError, RecursionError):
            _audit("account.import_denied", user_id, "invalid_format")
            return self._json(400, {"ok": False, "error": "El archivo no es un JSON válido."})

        # 4) Puerta de formato/versión: objeto + schema_version==1 + collection/
        #    lists como arrays. exported_at, profile y cualquier user_id se IGNORAN
        #    (AC-8, AC-12). Nada se escribe si el archivo no es un export válido.
        if not isinstance(body, dict) or body.get("schema_version") != 1 \
                or not isinstance(body.get("collection"), list) \
                or not isinstance(body.get("lists"), list):
            _audit("account.import_denied", user_id, "invalid_format")
            return self._json(422, {"ok": False, "error": "El archivo no es un export válido de Cinephora."})
        collection = body["collection"]
        lists      = body["lists"]

        # 5) Topes de conteo: elementos (títulos + items de listas) y nº de listas
        #    (AC-9). Sobre cualquiera → 413, nada escrito.
        total_items = len(collection) + sum(
            len(lst.get("items", [])) for lst in lists if isinstance(lst, dict) and isinstance(lst.get("items"), list))
        if total_items > MAX_IMPORT_ITEMS or len(lists) > MAX_IMPORT_LISTS:
            _audit("account.import_denied", user_id, "too_large")
            return self._json(413, {"ok": False, "error": "El archivo supera el número máximo de elementos."})

        # 6) Escritura: UNA transacción atómica, toda query %s-parametrizada y
        #    `user_id`-scoped (PS-002, AC-13). Un fallo de DB revierte todo el
        #    import (sin corrupción parcial). Contadores del resumen.
        summary = {
            "titles_imported":          0,
            "titles_skipped_present":   0,
            "titles_skipped_invalid":   0,
            "lists_created":            0,
            "lists_merged":             0,
            "list_items_imported":      0,
            "list_items_skipped_present": 0,
            "list_items_skipped_invalid": 0,
        }
        try:
            with get_db() as cur:
                # ── Colección: INSERT de columna COMPLETA (fidelidad round-trip,
                #    AC-6) — NO el alta lossy de _add_movie. Item inválido → omitido
                #    + contado, nunca fatal (AC-10). Dedup por (tmdb_id, media_type,
                #    user_id) vía SELECT-antes-de-INSERT; item sin tmdb_id se inserta
                #    sin dedup (mismo comportamiento que el alta existente).
                for item in collection:
                    if not isinstance(item, dict):
                        summary["titles_skipped_invalid"] += 1
                        continue
                    row = self._import_validate_movie(item)
                    if row is None:
                        summary["titles_skipped_invalid"] += 1
                        continue
                    if row["tmdb_id"] is not None:
                        cur.execute(
                            "SELECT 1 FROM movies WHERE tmdb_id = %s AND media_type = %s AND user_id = %s",
                            (row["tmdb_id"], row["media_type"], user_id))
                        if cur.fetchone():
                            summary["titles_skipped_present"] += 1
                            continue
                    cur.execute(
                        "INSERT INTO movies "
                        "(user_id, tmdb_id, media_type, title, year, poster_url, status, "
                        " rating, note, watched_at, platform, current_season, current_episode, "
                        " total_seasons, genres, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (user_id, row["tmdb_id"], row["media_type"], row["title"], row["year"],
                         row["poster_url"], row["status"], row["rating"], row["note"],
                         row["watched_at"], row["platform"], row["current_season"],
                         row["current_episode"], row["total_seasons"], row["genres"],
                         row["created_at"]))
                    summary["titles_imported"] += 1

                # ── Listas: reconciliación por nombre exacto-recortado. Pre-cargar
                #    las listas del usuario en un mapa nombre→id (una query). Un
                #    nombre coincidente → merge en esa lista (lists_merged una vez);
                #    un nombre nuevo → INSERT ... RETURNING id (lists_created). Items:
                #    validar e insertar con ON CONFLICT DO NOTHING (NO un
                #    UniqueViolation capturado, que abortaría la transacción única).
                cur.execute("SELECT id, name FROM lists WHERE user_id = %s", (user_id,))
                name_to_id = {r["name"].strip(): r["id"] for r in cur.fetchall()}
                for entry in lists:
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get("name", "")).strip()[:200]
                    if not name:
                        continue
                    items = entry.get("items")
                    if not isinstance(items, list):
                        items = []
                    if name in name_to_id:
                        list_id = name_to_id[name]
                        summary["lists_merged"] += 1
                    else:
                        cur.execute(
                            "INSERT INTO lists (user_id, name) VALUES (%s, %s) RETURNING id",
                            (user_id, name))
                        list_id = cur.fetchone()["id"]
                        name_to_id[name] = list_id
                        summary["lists_created"] += 1
                    # position sembrado desde COALESCE(MAX(position)+1, 0) e
                    # incrementado por cada insert exitoso en esta lista.
                    cur.execute(
                        "SELECT COALESCE(MAX(position) + 1, 0) AS next_pos "
                        "FROM list_items WHERE list_id = %s",
                        (list_id,))
                    next_pos = cur.fetchone()["next_pos"]
                    for it in items:
                        li = self._import_validate_list_item(it)
                        if li is None:
                            summary["list_items_skipped_invalid"] += 1
                            continue
                        cur.execute(
                            "INSERT INTO list_items "
                            "(list_id, tmdb_id, media_type, title, year, poster_url, position) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                            "ON CONFLICT (list_id, tmdb_id, media_type) DO NOTHING",
                            (list_id, li["tmdb_id"], li["media_type"], li["title"],
                             li["year"], li["poster_url"], next_pos))
                        if cur.rowcount == 0:
                            summary["list_items_skipped_present"] += 1
                        else:
                            summary["list_items_imported"] += 1
                            next_pos += 1
        except psycopg2.Error:
            # El error crudo de DB nunca se serializa (invariants; AC-15). La
            # transacción ya se revirtió (context manager) → nada escrito.
            _audit("account.import_denied", user_id, "db_error")
            return self._json(500, {"ok": False, "error": "No se pudo importar. Inténtalo de nuevo."})

        _audit("account.imported", user_id, "account")
        self._json(200, {"ok": True, "summary": summary})

    @staticmethod
    def _import_validate_movie(item):
        """Valida un item de colección espejando los validadores de _add_movie / el
        PATCH. Devuelve un dict de columnas listo para el INSERT completo, o None si
        el item es inválido (el llamante lo omite + cuenta, nunca es fatal, AC-10).
        Un poster de origen no permitido no invalida el item: se guarda "" (AC-10)."""
        title = str(item.get("title", "")).strip()[:300]
        if not title:
            return None
        media_type = item.get("media_type")
        if media_type not in ("movie", "tv"):
            return None
        status = item.get("status")
        if status not in ("pendiente", "viendo", "vista", "abandonada"):
            return None
        rating = item.get("rating")
        if rating is not None and (not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5):
            return None
        note = item.get("note")
        if note is not None:
            if not isinstance(note, str):
                return None
            note = note.strip()[:500]
        try:
            watched_at = parse_watched_at(item.get("watched_at"))
        except ValueError:
            return None
        platform = item.get("platform")
        if platform is not None and platform not in PLATFORMS:
            return None
        current_season = _import_int_or_none(item.get("current_season"))
        if current_season is False:
            return None
        current_episode = _import_int_or_none(item.get("current_episode"))
        if current_episode is False:
            return None
        total_seasons = _import_int_or_none(item.get("total_seasons"))
        if total_seasons is False:
            return None
        # total_seasons solo aplica a series; en películas se fuerza null (como _add_movie).
        if media_type != "tv":
            total_seasons = None
        tmdb_id = item.get("tmdb_id")
        if tmdb_id not in (None, ""):
            try:
                tmdb_id = int(tmdb_id)
            except (TypeError, ValueError):
                return None
        else:
            tmdb_id = None
        year = str(item.get("year", "")).strip()[:10]
        genres = item.get("genres")
        if genres is not None and not isinstance(genres, str):
            return None
        # Tope de longitud espejando el write-path (_add_movie: 8 géneros × 40 chars).
        # Truncar, no rechazar (AC-10 / US-040) — un genres sobredimensionado nunca se
        # almacena sin acotar.
        if genres is not None:
            genres = genres[:360]
        poster = str(item.get("poster_url", "")).strip()
        # Allow-list de posters: solo TMDB; cualquier otra URL se descarta (no invalida).
        if poster and not poster.startswith("https://image.tmdb.org/"):
            poster = ""
        poster = poster[:500]
        # created_at = valor del archivo si parsea como timestamp válido, si no ahora.
        created_at = _import_parse_timestamp(item.get("created_at"))
        return {
            "tmdb_id":         tmdb_id,
            "media_type":      media_type,
            "title":           title,
            "year":            year,
            "poster_url":      poster,
            "status":          status,
            "rating":          rating,
            "note":            note,
            "watched_at":      watched_at,
            "platform":        platform,
            "current_season":  current_season,
            "current_episode": current_episode,
            "total_seasons":   total_seasons,
            "genres":          genres,
            "created_at":      created_at,
        }

    @staticmethod
    def _import_validate_list_item(it):
        """Valida un item de lista (tmdb_id int obligatorio, media_type, title,
        year, allow-list de poster). Devuelve dict listo para el INSERT o None
        (el llamante lo omite + cuenta list_items_skipped_invalid)."""
        if not isinstance(it, dict):
            return None
        tmdb_id = it.get("tmdb_id")
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            return None
        media_type = it.get("media_type")
        if media_type not in ("movie", "tv"):
            return None
        title = str(it.get("title", "")).strip()[:300]
        if not title:
            return None
        year = str(it.get("year", "")).strip()[:10]
        poster = str(it.get("poster_url", "")).strip()
        if poster and not poster.startswith("https://image.tmdb.org/"):
            poster = ""
        poster = poster[:500]
        return {"tmdb_id": tmdb_id, "media_type": media_type,
                "title": title, "year": year, "poster_url": poster}

    # ── Perfil (owner) ──────────────────────────────────────────────────────────

    def _get_profile(self):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        with get_db() as cur:
            cur.execute(
                "SELECT username, is_public, show_collection, show_stats, avatar_url "
                "FROM profiles WHERE user_id = %s",
                (user_id,))
            row = cur.fetchone()
        if row:
            profile = dict(row)
        else:
            # Defaults perezosos: nunca se crea una fila pública implícitamente.
            profile = {"username": None, "is_public": False,
                       "show_collection": False, "show_stats": False,
                       "avatar_url": None}
        self._json(200, {"ok": True, "profile": profile})

    def _patch_profile(self):
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "JSON inválido"})

        # Estado actual (para validar publish-sin-username, auditar cambios y
        # componer el `profile` devuelto con los campos no tocados).
        with get_db() as cur:
            cur.execute(
                "SELECT username, is_public, show_collection, show_stats, avatar_url "
                "FROM profiles WHERE user_id = %s",
                (user_id,))
            current = cur.fetchone()
        cur_username = current["username"] if current else None
        cur_is_public = current["is_public"] if current else False

        cols, vals = [], []     # columna → valor a escribir (orden estable)
        new_username = cur_username
        username_changed = False

        # Acción de avatar (validada de forma independiente; compone con
        # username/is_public/show_*). El cliente ya subió el objeto client-direct;
        # el servidor DERIVA `avatar_url` (nunca confía en una URL del cliente,
        # img-src-injection). Cualquier valor distinto de set/remove → 400.
        avatar_action = None
        if "avatar" in data:
            avatar_action = data["avatar"]
            if avatar_action not in ("set", "remove"):
                return self._json(400, {"ok": False, "error": "Acción de avatar inválida"})
            if avatar_action == "set":
                # HEAD service_role: confirma que el objeto existe antes de derivar
                # y almacenar la URL. Objeto ausente → 400 (no persiste imagen rota).
                if not self._supabase_storage_head_avatar(user_id):
                    return self._json(400, {"ok": False, "error": "No se encontró la imagen subida"})
                avatar_value = self._storage_public_avatar_url(user_id, int(time.time()))
                cols.append("avatar_url"); vals.append(avatar_value)
            else:  # remove
                # DELETE service_role idempotente (404 = éxito) + avatar_url = NULL.
                self._supabase_storage_delete_avatar(user_id)
                cols.append("avatar_url"); vals.append(None)

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

        # Perfil devuelto: estado previo (o defaults perezosos) con las columnas
        # recién escritas superpuestas (avatar_url incluido). `updated_at` no forma
        # parte del contrato de lectura del perfil, se excluye.
        profile = {
            "username":        cur_username,
            "is_public":       cur_is_public,
            "show_collection": current["show_collection"] if current else False,
            "show_stats":      current["show_stats"] if current else False,
            "avatar_url":      current.get("avatar_url") if current else None,
        }
        for c, v in zip(cols, vals):
            if c != "updated_at":
                profile[c] = v
        self._json(200, {"ok": True, "profile": profile})

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
            # Propiedad de la lista (AC-13 → 404 si es de otro usuario). El SELECT
            # se ensancha a `visibility` (misma query, sin round-trip extra) para
            # decidir el evento social 'list_add' tras un alta exitosa (ADR-014).
            cur.execute(
                "SELECT visibility FROM lists WHERE id = %s AND user_id = %s",
                (list_id, user_id))
            lrow = cur.fetchone()
            if not lrow:
                return self._json(404, {"ok": False, "error": "No encontrada"})
            list_visibility = lrow["visibility"]
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
            # Evento social (ADR-014): SOLO si la lista es actualmente pública y
            # tras un alta exitosa (201, no el 409 de duplicado — está en el except).
            # Append en la MISMA transacción, ÚLTIMO en el bloque, sin pre-check que
            # pueda 500 — el contrato 201/409/404/400 queda intacto. Lista no
            # pública → sin evento (AC-11).
            if list_visibility == "public":
                _record_activity(cur, user_id, "list_add", {
                    "tmdb_id": tmdb_id, "media_type": media_type, "title": title,
                    "year": year, "poster_url": poster}, list_id=list_id)
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

    def _username_available(self):
        """Endpoint público anónimo (advisory) para la elección de username en
        registro / la pasarela de primer acceso. Limiter ANTES de cualquier
        lectura de DB (ADR-006/AS-013). Comprueba SOLO usernames, nunca emails;
        nunca reserva ni escribe. `_normalize_username` (None → reason:"invalid",
        sin DB); si normaliza, una sola existencia parametrizada (PS-002) →
        reason:"taken"|"ok". Respuesta siempre vía `_json`."""
        if self._public_rate_limited():
            print("audit " + json.dumps({"action": "username_available.throttled", "timestamp": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False)); return
        raw = (self._qs().get("u") or [""])[0]
        # Acota la longitud ANTES de normalizar: el formato válido es 3-30, así
        # que un valor desmesurado es entrada malformada → 400 (US-040).
        if len(raw) > 64:
            return self._json(400, {"ok": False, "error": "Parámetro inválido"})
        norm = _normalize_username(raw)
        if norm is None:
            # Inválido / reservado → advisory sin tocar la DB.
            return self._json(200, {"ok": True, "available": False, "reason": "invalid"})
        with get_db() as cur:
            cur.execute("SELECT 1 FROM profiles WHERE username = %s", (norm,))
            taken = cur.fetchone() is not None
        if taken: return self._json(200, {"ok": True, "available": False, "reason": "taken"})
        return self._json(200, {"ok": True, "available": True, "reason": "ok"})

    def _public_profile(self, username):
        if self._public_rate_limited():
            return
        username = username.lower()
        with get_db() as cur:
            cur.execute(
                "SELECT user_id, username, is_public, show_collection, show_stats, avatar_url "
                "FROM profiles WHERE username = %s",
                (username,))
            prof = cur.fetchone()
            # AC-3: perfil inexistente o no público → 404 (no enumera).
            if not prof or not prof["is_public"]:
                return self._json(404, {"ok": False, "error": "No encontrado"})
            owner_id = prof["user_id"]
            # avatar_url es identidad de cabecera: se incluye siempre (nullable),
            # NO gateado por show_collection/show_stats.
            body = {"username": prof["username"], "avatar_url": prof.get("avatar_url")}
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
            # Seguidores / seguidos (ADR-014). Conteos = totales REALES (incluyen
            # participantes con perfil privado, AC-7). Las listas nombran SOLO
            # perfiles públicos (JOIN profiles … is_public, AC-8): un participante
            # privado cuenta pero nunca aparece ni se enlaza (GD-001, minimización).
            # Nunca se serializa email ni user_id.
            cur.execute("SELECT COUNT(*) AS c FROM follows WHERE followed_id = %s", (owner_id,))
            body["followers_count"] = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM follows WHERE follower_id = %s", (owner_id,))
            body["following_count"] = cur.fetchone()["c"]
            cur.execute(
                "SELECT p.username, p.avatar_url "
                "FROM follows f JOIN profiles p ON p.user_id = f.follower_id "
                "WHERE f.followed_id = %s AND p.is_public = TRUE "
                "ORDER BY f.created_at DESC LIMIT %s",
                (owner_id, PUBLIC_FOLLOW_LIST_MAX))
            body["followers"] = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT p.username, p.avatar_url "
                "FROM follows f JOIN profiles p ON p.user_id = f.followed_id "
                "WHERE f.follower_id = %s AND p.is_public = TRUE "
                "ORDER BY f.created_at DESC LIMIT %s",
                (owner_id, PUBLIC_FOLLOW_LIST_MAX))
            body["following"] = [dict(r) for r in cur.fetchall()]
            # Reseñas publicadas (ADR-015). Independiente de show_collection (AC-6):
            # el perfil ya es público (404 arriba si no), y publicar la nota es su
            # propio consentimiento per-título. Gate note_public=TRUE AND note<>''
            # (AC-3/AC-5). like_count = total REAL por reseña. Proyección allow-list:
            # NUNCA email ni raw user_id; `note` es el texto de la reseña
            # (render textContent en public.js, jamás innerHTML).
            cur.execute(
                "SELECT id AS movie_id, tmdb_id, media_type, title, year, poster_url, "
                "       note, created_at, "
                "       (SELECT COUNT(*) FROM likes lk WHERE lk.movie_id = movies.id) AS like_count "
                "FROM movies WHERE user_id = %s AND note_public = TRUE AND note <> '' "
                "ORDER BY created_at DESC LIMIT %s",
                (owner_id, PUBLIC_REVIEW_LIST_MAX))
            body["reviews"] = [dict(r) for r in cur.fetchall()]
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

    # ── Capa social: follows + feed (autenticado) ────────────────────────────────

    def _follow(self):
        """POST /api/follows {username} — seguir al usuario del cuerpo. Auth
        (PS-001) + rate limit por usuario+global. Resuelve username→(user_id,
        is_public): 404 'No disponible' si no resuelve O no es público (no
        enumera: privado y inexistente lucen idénticos, AC-3); 400 si es uno
        mismo (AC-4); si no INSERT … ON CONFLICT DO NOTHING (idempotente, AC-5)
        → 200 {following:true}. _audit redactado (LO-*)."""
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        allowed, retry = rate_check([(f"follow:{user_id}", FOLLOW_RATE_MAX),
                                     ("follow:_global", FOLLOW_RATE_GLOBAL)])
        if not allowed:
            return self._json(429, {"ok": False, "error": "Demasiadas peticiones, espera un momento."},
                              extra_headers={"Retry-After": retry})
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "JSON inválido"})
        username = _normalize_username(data.get("username"))
        if username is None:
            # Username malformado/reservado → no enumera (mismo 404 que inexistente).
            return self._json(404, {"ok": False, "error": "No disponible"})
        with get_db() as cur:
            cur.execute(
                "SELECT user_id, is_public FROM profiles WHERE username = %s",
                (username,))
            target = cur.fetchone()
            # AC-3: inexistente O no público → 404 idéntico (no enumera).
            if not target or not target["is_public"]:
                return self._json(404, {"ok": False, "error": "No disponible"})
            target_id = target["user_id"]
            if target_id == user_id:   # AC-4: no puedes seguirte a ti mismo.
                return self._json(400, {"ok": False, "error": "No puedes seguirte a ti mismo."})
            # AC-1/AC-5: idempotente. follower_id = SIEMPRE el caller (PS-001/AC-17);
            # el cliente nunca suministra el follower id.
            cur.execute(
                "INSERT INTO follows (follower_id, followed_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (user_id, target_id))
        _audit("follow.created", user_id, "follow")
        self._json(200, {"ok": True, "following": True})

    def _unfollow(self, username):
        """DELETE /api/follows/{username} — dejar de seguir. Auth (PS-001).
        Idempotente y no-enumerante: 200 {following:false} incluso si no había
        arista o el username no existe (AC-2). _audit redactado."""
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        norm = _normalize_username(username)
        with get_db() as cur:
            if norm is not None:
                cur.execute(
                    "DELETE FROM follows WHERE follower_id = %s "
                    "AND followed_id = (SELECT user_id FROM profiles WHERE username = %s)",
                    (user_id, norm))
        _audit("follow.deleted", user_id, "follow")
        self._json(200, {"ok": True, "following": False})

    def _follow_status(self, username):
        """GET /api/follows/{username} — estado de seguimiento del caller hacia el
        usuario nombrado; alimenta el botón de la página pública. Auth (PS-001).
        Devuelve {following, is_self, followable=(is_public and not is_self)}. Un
        username inexistente → following:false, is_self:false, followable:false."""
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        norm = _normalize_username(username)
        following = is_self = followable = False
        if norm is not None:
            with get_db() as cur:
                cur.execute(
                    "SELECT user_id, is_public FROM profiles WHERE username = %s",
                    (norm,))
                target = cur.fetchone()
                if target:
                    is_self = target["user_id"] == user_id
                    followable = bool(target["is_public"]) and not is_self
                    if not is_self:
                        cur.execute(
                            "SELECT 1 FROM follows WHERE follower_id = %s AND followed_id = %s",
                            (user_id, target["user_id"]))
                        following = cur.fetchone() is not None
        self._json(200, {"ok": True, "following": following,
                         "is_self": is_self, "followable": followable})

    # ── Likes sobre reseñas (ADR-015) ──────────────────────────────────────────

    def _review_visible(self, cur, movie_id):
        """True si el título `movie_id` es actualmente una reseña VISIBLE: existe,
        su perfil es público, note_public=TRUE y note<>''. Gate a estado ACTUAL
        (nunca snapshot). No enumera: el caller mapea None→404 idéntico tanto para
        no-visible como para inexistente."""
        cur.execute(
            "SELECT 1 FROM movies m JOIN profiles p ON p.user_id = m.user_id "
            "WHERE m.id = %s AND p.is_public = TRUE AND m.note_public = TRUE AND m.note <> ''",
            (movie_id,))
        return cur.fetchone() is not None

    def _like_count(self, cur, movie_id):
        cur.execute("SELECT COUNT(*) AS c FROM likes WHERE movie_id = %s", (movie_id,))
        return cur.fetchone()["c"]

    def _like_review(self, movie_id):
        """POST /api/reviews/{movie_id}/likes — dar me gusta a una reseña. Auth
        (PS-001, 401) + rate limit por usuario+global (429). Resuelve el objetivo
        como reseña VISIBLE (perfil público + publicada + nota no vacía): no
        visible / inexistente → 404 'No disponible' idéntico (no enumera). Si visible:
        INSERT … ON CONFLICT DO NOTHING (idempotente, AC-11; self-like permitido).
        liker_id = SIEMPRE el JWT sub (AC-16); el cliente nunca lo suministra."""
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        allowed, retry = rate_check([(f"like:{user_id}", LIKE_RATE_MAX),
                                     ("like:_global", LIKE_RATE_GLOBAL)])
        if not allowed:
            return self._json(429, {"ok": False, "error": "Demasiadas peticiones, espera un momento."},
                              extra_headers={"Retry-After": retry})
        with get_db() as cur:
            if not self._review_visible(cur, movie_id):
                return self._json(404, {"ok": False, "error": "No disponible"})
            cur.execute(
                "INSERT INTO likes (liker_id, movie_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, movie_id))
            count = self._like_count(cur, movie_id)
        _audit("like.created", user_id, "like")
        self._json(200, {"ok": True, "liked": True, "count": count})

    def _unlike_review(self, movie_id):
        """DELETE /api/reviews/{movie_id}/likes — quitar me gusta. Auth (PS-001,
        401). DELETE scoped por liker_id=caller (AC-16). Idempotente y no-enumerante:
        200 {liked:false, count} aun sin fila previa (AC-12). _audit redactado."""
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        with get_db() as cur:
            cur.execute(
                "DELETE FROM likes WHERE liker_id = %s AND movie_id = %s",
                (user_id, movie_id))
            count = self._like_count(cur, movie_id)
        _audit("like.deleted", user_id, "like")
        self._json(200, {"ok": True, "liked": False, "count": count})

    def _review_likes(self, movie_id):
        """GET /api/reviews/{movie_id}/likes — estado de like del caller + lista de
        likers públicos. Auth (PS-001, 401) + rate limit por usuario. La reseña debe
        ser VISIBLE (no visible / inexistente → 404 idéntico). `count` es el total
        REAL (todos los likers, incl. privados, AC-14); `liked_by_me` es EXISTS para
        el caller; `likers` nombra SOLO perfiles públicos (JOIN profiles is_public),
        acotado a PUBLIC_LIKE_LIST_MAX. NUNCA serializa email ni raw user_id (GD-001)."""
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        allowed, retry = rate_check([(f"like:{user_id}", LIKE_RATE_MAX),
                                     ("like:_global", LIKE_RATE_GLOBAL)])
        if not allowed:
            return self._json(429, {"ok": False, "error": "Demasiadas peticiones, espera un momento."},
                              extra_headers={"Retry-After": retry})
        with get_db() as cur:
            if not self._review_visible(cur, movie_id):
                return self._json(404, {"ok": False, "error": "No disponible"})
            count = self._like_count(cur, movie_id)
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM likes WHERE movie_id = %s AND liker_id = %s) AS liked",
                (movie_id, user_id))
            liked_by_me = cur.fetchone()["liked"]
            cur.execute(
                "SELECT p.username, p.avatar_url "
                "FROM likes lk JOIN profiles p ON p.user_id = lk.liker_id "
                "WHERE lk.movie_id = %s AND p.is_public = TRUE "
                "ORDER BY lk.created_at DESC LIMIT %s",
                (movie_id, PUBLIC_LIKE_LIST_MAX))
            likers = [dict(r) for r in cur.fetchall()]
        self._json(200, {"ok": True, "count": count, "liked_by_me": liked_by_me, "likers": likers})

    def _feed(self):
        """GET /api/feed — el feed del caller: actividad reciente de quienes sigue,
        reverse-chronological, LIMIT FEED_LIMIT. Auth (PS-001) + rate limit. UNA
        query gateada (el nuevo modelo de autorización): el caller sigue al actor
        (follows.follower_id = caller, AC-17) AND el actor es actualmente público
        (AC-12) AND la sección relevante está expuesta (show_collection para
        watched/rated AC-13; list visibility='public' para list_add AC-14). Gate a
        estado ACTUAL, nunca snapshot → re-privatizar quita eventos al instante.
        Proyección allow-list: NUNCA email, user_id ni note (GD-001). Feed vacío →
        {activity: []} (AC-15)."""
        user_id = self._get_user_id()
        if not user_id:
            return self._json(401, {"ok": False, "error": "No autenticado"})
        allowed, retry = rate_check([(f"feed:{user_id}", FEED_RATE_MAX),
                                     ("feed:_global", FEED_RATE_GLOBAL)])
        if not allowed:
            return self._json(429, {"ok": False, "error": "Demasiadas peticiones, espera un momento."},
                              extra_headers={"Retry-After": retry})
        with get_db() as cur:
            cur.execute(
                "SELECT a.action, a.tmdb_id, a.media_type, a.title, a.year, a.poster_url, "
                "       a.rating, a.created_at, p.username, p.avatar_url, "
                "       l.name AS list_name, l.share_token AS list_share_token, "
                "       a.movie_id, m.note AS review_note, "
                "       (SELECT COUNT(*) FROM likes lk WHERE lk.movie_id = a.movie_id) AS like_count, "
                "       EXISTS(SELECT 1 FROM likes lk2 WHERE lk2.movie_id = a.movie_id "
                "              AND lk2.liker_id = %s) AS liked_by_me "
                "FROM activity a "
                "JOIN follows  f ON f.followed_id = a.user_id AND f.follower_id = %s "
                "JOIN profiles p ON p.user_id = a.user_id AND p.is_public = TRUE "
                "LEFT JOIN lists  l ON l.id = a.list_id "
                "LEFT JOIN movies m ON m.id = a.movie_id "
                "WHERE ( a.action IN ('watched', 'rated') AND p.show_collection = TRUE ) "
                "   OR ( a.action = 'list_add' AND l.visibility = 'public' ) "
                # AC-6/AC-8: 'reviewed' gateado a estado ACTUAL (nota publicada + no
                # vacía); perfil público ya impuesto por el JOIN profiles. NO gateado
                # por show_collection (opt-in independiente por título).
                "   OR ( a.action = 'reviewed' AND m.note_public = TRUE AND m.note <> '' ) "
                "ORDER BY a.created_at DESC "
                "LIMIT %s",
                (user_id, user_id, FEED_LIMIT))
            rows = cur.fetchall()
        # Proyección allow-list (GD-001): solo campos consentidos; rating solo en
        # 'rated', list_name/list_share_token solo en 'list_add'. Nunca email/
        # user_id/note. Un poster no-TMDB (defensa) se degrada a None.
        activity = []
        for r in rows:
            poster = r["poster_url"]
            if poster and not str(poster).startswith("https://image.tmdb.org/"):
                poster = None
            entry = {
                "action":     r["action"],
                "username":   r["username"],
                "avatar_url": r["avatar_url"],
                "title":      r["title"],
                "poster_url": poster,
                "media_type": r["media_type"],
                "tmdb_id":    r["tmdb_id"],
                "year":       r["year"],
                "created_at": r["created_at"],
            }
            if r["action"] == "rated":
                entry["rating"] = r["rating"]
            if r["action"] == "list_add":
                entry["list_name"]        = r["list_name"]
                entry["list_share_token"] = r["list_share_token"]
            if r["action"] == "reviewed":
                # Proyección de reseña (ADR-015): la nota ACTUAL (review_note) + el id
                # del título para el control de like + conteo/estado. Nunca email/
                # user_id. El texto se escapa en el render (esc() en activity.js).
                entry["note"]        = r["review_note"]
                entry["movie_id"]    = r["movie_id"]
                entry["like_count"]  = r["like_count"]
                entry["liked_by_me"] = r["liked_by_me"]
            activity.append(entry)
        self._json(200, {"ok": True, "activity": activity})

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
    with ErrorNotifyingServer((HOST, PORT), handler) as httpd:
        tmdb = "sí" if os.environ.get("TMDB_API_KEY") else "no (modo manual)"
        hook = "sí" if (os.environ.get("DISCORD_WEBHOOK_PENDIENTE")
                        or os.environ.get("DISCORD_WEBHOOK_VISTA")
                        or os.environ.get("DISCORD_WEBHOOK_URL")) else "no"
        errhook = "sí" if os.environ.get("DISCORD_WEBHOOK_ERRORS") else "no"
        print(f"Cineteca en http://{HOST}:{PORT}  ·  TMDB: {tmdb}  ·  Discord: {hook}  ·  "
              f"Alertas error: {errhook}  (Ctrl+C para parar)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServidor detenido.")
        finally:
            if _db_pool is not None:
                _db_pool.closeall()


if __name__ == "__main__":
    main()
