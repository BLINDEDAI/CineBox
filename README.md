# CineBox

> A personal movie and TV tracker with a dark noir aesthetic. Search any title, add it to your collection, rate it, track where you watched it, and get Discord notifications when you move something to your watchlist or mark it as seen.

Built with vanilla HTML/CSS/JS on the frontend and a pure Python stdlib HTTP server on the backend — no frameworks, no bundlers. Backed by PostgreSQL and Supabase for auth.

---

## Visual Design

CineBox uses a dark noir theme: near-black background (`#050915`), a deep red accent for interactive elements, gold stars for ratings, and a subtle radial gradient that gives the interface a cinematic feel. The layout features a fixed sidebar for navigation and a responsive card grid that adapts from desktop down to mobile.

---

## ✨ Features

- **Search** movies and TV shows via The Movie Database (TMDB), with poster, year, genre, and streaming providers (Spain)
- **Add to collection** with a single click — poster, metadata, and genres are filled in automatically
- **Status tracking** — four states: *Pending*, *Watching*, *Watched*, *Abandoned*
- **5-star rating** and personal notes per title
- **Watched date** — log when you actually saw it (defaults to today when marking as watched)
- **Platform tracking** — record where you watched it (Netflix, HBO Max, Prime Video, Disney+, Movistar+, Cinema, Other)
- **Episode progress** for TV shows (current season and episode)
- **Filter and search** your collection by status, media type, and title
- **Statistics view** — a quick overview of your collection breakdown
- **Discover tab** — weekly trending titles from TMDB
- **Detail panel** — synopsis, director/creator, cast, trailer link, and streaming providers
- **Discord notifications** — webhook alerts with embed and poster when you add or change a title's status
- **Owner-only notifications** — filter Discord alerts to a single Supabase user via `DISCORD_OWNER_ID`
- **Supabase authentication** — email/password login and registration, JWT-validated on every request
- **Per-user data isolation** — each user only ever sees and modifies their own collection

---

## 🛠 Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Vanilla HTML5, CSS3, JavaScript (no build step) |
| Backend | Python 3 — `http.server`, `ThreadingHTTPServer` (no framework) |
| Database | PostgreSQL via `psycopg2` |
| Auth | Supabase (email/password) — JWT verified server-side via JWKS |
| Movie data | The Movie Database (TMDB) API v3 |
| Notifications | Discord Incoming Webhooks |
| Hosting | Render (backend) + Supabase (auth + DB) |

---

## 🚀 Running Locally

### Prerequisites

- Python 3.10+
- A [Supabase](https://supabase.com) project (free tier works)
- A [TMDB API key](https://www.themoviedb.org/settings/api) (free, takes ~2 minutes)

### 1. Clone the repository

```bash
git clone https://github.com/your-username/cinebox.git
cd cinebox
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create the database table

In your Supabase project, run this SQL in the **SQL Editor**:

```sql
CREATE TABLE movies (
    id          SERIAL PRIMARY KEY,
    user_id     UUID NOT NULL,
    tmdb_id     INTEGER,
    media_type  TEXT NOT NULL DEFAULT 'movie',
    title       TEXT NOT NULL,
    year        TEXT,
    poster_url  TEXT,
    genres      TEXT,
    status      TEXT NOT NULL DEFAULT 'pendiente',
    rating      INTEGER,
    note        TEXT,
    platform    TEXT,
    watched_at  DATE,
    current_season  INTEGER,
    current_episode INTEGER,
    created_at  TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE movies ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users manage own movies"
    ON movies FOR ALL
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Fill in your values (see [Environment Variables](#-environment-variables) below).

### 5. Start the server

```bash
python server.py
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

---

## 🔑 Environment Variables

Copy `.env.example` to `.env` and set these values. The server reads `.env` at startup and never exposes it to the browser.

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL connection string from Supabase (Settings → Database → Connection string → URI) |
| `SUPABASE_URL` | Yes | Your Supabase project URL (Settings → API → Project URL) |
| `SUPABASE_ANON_KEY` | Yes | Supabase anonymous/public key (Settings → API → Project API keys) |
| `TMDB_API_KEY` | Recommended | TMDB v3 API key — without it, search and trending are disabled |
| `DISCORD_WEBHOOK_URL` | Optional | Single Discord webhook for all status changes (fallback) |
| `DISCORD_WEBHOOK_PENDIENTE` | Optional | Webhook for titles added to the watchlist |
| `DISCORD_WEBHOOK_VISTA` | Optional | Webhook for titles marked as watched |
| `DISCORD_OWNER_ID` | Optional | Supabase UUID of the account that should trigger Discord notifications — all other users are silenced |
| `PORT` | Optional | HTTP port (default: `8000`) |

---

## ☁️ Deploying to Render + Supabase

### Supabase (database and auth)

1. Create a free project at [supabase.com](https://supabase.com).
2. Run the SQL above in **SQL Editor** to create the `movies` table.
3. In **Settings → API**, copy your Project URL and anon key.
4. In **Settings → Database**, copy the connection string (URI mode).

### Render (backend + static files)

1. Push this repository to GitHub.
2. In [Render](https://render.com), create a new **Web Service** connected to your repository.
3. Set the following:
   - **Runtime**: Python 3
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `python server.py`
4. Add all environment variables from the table above under **Environment**.
5. Deploy — Render will serve both the API and the static frontend from the same process.

The frontend reads `/api/config` on load to get `SUPABASE_URL` and `SUPABASE_ANON_KEY`, so those values never need to be hardcoded in the source.

---

## License

MIT — do whatever you want with it.

---

*Movie and TV show data provided by [The Movie Database (TMDB)](https://www.themoviedb.org). This product uses the TMDB API but is not endorsed or certified by TMDB.*
