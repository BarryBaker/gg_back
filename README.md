# GGPoker Leaderboards Scraper & API

Scrapes the GGPoker Omaha Daily Leaderboard, stores results in a normalized database (SQLite locally, PostgreSQL in the cloud), and serves the data through a small HTTP API. Ships with Docker, a local DB viewer, and a remote Postgres viewer.

## Features

- Modern Selenium-based scraper (Chrome/Chromium, headless by default)
- Configurable target page and game (e.g., `PLO`)
- Normalized schema: `leaderboards`, `players` (with `country`), `update_batch`, `facts`
- Uses SQLite when `SQLITE_DB_PATH` is set; otherwise PostgreSQL when `DATABASE_URL` is set, and SQLite by default
- Lightweight Flask API for tables, pivoted data, player history, and top players
- Docker images for continuous scraping and API service
- Helper viewers for local SQLite and remote PostgreSQL

## Quick Start (Local)

1. Install dependencies

```bash
python -m venv .venv && source .venv/bin/activate  # optional
pip install -r requirements.txt
```

2. Run the scraper once (headless by default)

```bash
python ggpoker_scraper.py
```

3. Or run continuously every 5 minutes

```bash
python ggpoker_scraper.py --interval 300
```

Environment examples

```bash
export INTERVAL=300
export DB_PATH=ggpoker_leaderboards.db
export PROMO_URL=https://ggpoker.com/promotions/omaha-daily-leaderboard/
export GAME_NAME=PLO
python ggpoker_scraper.py
```

Chrome/Chromium notes

- The project auto-installs a matching chromedriver. In Docker, we use system Chromium.
- To see the browser locally, either pass `--headless` off by setting `HEADLESS=0`, or omit `HEADLESS`.

## HTTP API

Start the API locally (uses the same DB autodetection as the scraper):

```bash
python api.py
# or production-style:
python serve.py  # runs gunicorn binding to PORT (default 8000)
```

Base URL: `http://localhost:8000`

Endpoints

- GET `/health` → `{ "status": "ok" }`
- GET `/tables` → `["PLO_...", ...]` from the `leaderboards` table
- GET `/tables/<leaderboard>/data?columns=4&limit=16`
  - Returns pivoted data: first column is `player`, followed by the last N timestamps for that leaderboard
  - Sorted by the latest column (numeric, NULLS LAST), returns up to `limit` rows
- GET `/tables/<leaderboard>/player?name=<Player+Name>`
  - Returns a single row of that player's points across all timestamps for the leaderboard
  - Includes `country`
- GET `/tables/<leaderboard>/top-players?limit=50`
  - Returns best daily standings per day aggregated across the historical updates (time cutoffs differ per backend)

Example

```bash
curl http://localhost:8000/health
curl http://localhost:8000/tables
curl "http://localhost:8000/tables/PLO___0dot01_0dot02/data?columns=4&limit=16"
curl --get "http://localhost:8000/tables/PLO___0dot01_0dot02/player" --data-urlencode "name=Some Player"
curl "http://localhost:8000/tables/PLO___0dot01_0dot02/top-players?limit=25"
```

## Database

Autodetection

- If `SQLITE_DB_PATH` is set, the scraper and API use that SQLite file even when a PostgreSQL URL is also set
- Otherwise, if `DATABASE_URL` (or `DATABASE_PRIVATE_URL` / `DATABASE_PUBLIC_URL` for the API) is set to a PostgreSQL URL, the app uses PostgreSQL
- Otherwise, it falls back to SQLite (file path from `DB_PATH`, default `ggpoker_leaderboards.db`)

Normalized schema

```sql
leaderboards(id, name UNIQUE)
players(id, name UNIQUE, country)
update_batch(id, ts)
facts(leaderboard_id, update_id, player_id, points,
      PRIMARY KEY (leaderboard_id, player_id, update_id))
```

Viewers

- Local SQLite: `python view_database.py`
- Remote PostgreSQL:
  1. Set `DATABASE_URL` in `.env` using the format in `env_template.txt`.
  2. `python view_remote_database.py`

## Configuration (Environment)

Scraper

- `PROMO_URL` (default: Omaha Daily Leaderboard URL)
- `GAME_NAME` (default: `PLO`)
- `INTERVAL` run cadence in seconds; `0` runs once (default: `300` in Docker, `0` locally)
- `HEADLESS` `1/true/yes` to run headless (default: headless)
- `CHROME_BIN`, `CHROME_DRIVER_BIN`, `CHROME_TYPE` (e.g., `chromium`) for custom binaries

Database

- `DATABASE_PRIVATE_URL` → preferred by the API if set
- `DATABASE_URL` → primary Postgres URL for scraper/API
- `DATABASE_PUBLIC_URL` → last fallback for the API
- `SQLITE_DB_PATH` → explicitly use this SQLite file, taking precedence over PostgreSQL URLs
- `DB_PATH` → SQLite file path when PostgreSQL is not selected (default: `ggpoker_leaderboards.db`)
- In Docker Compose, the API and scrapers connect to `postgres:5432` as `gguser` using `POSTGRES_PASSWORD`.

API

- `PORT` (default: `8000`)

## Docker

We provide three services in `docker-compose.yml`:

- `postgres`: local PostgreSQL 16 database with a persistent Docker volume
- `scraper`: runs continuously and writes to PostgreSQL
- `api`: exposes HTTP API on port `8000`

Set `POSTGRES_PASSWORD` in an untracked `.env` file next to `docker-compose.yml`.
For a new database, generate a URL-safe password with `openssl rand -hex 32`.
Compose uses the same password to initialize PostgreSQL and build the API and
scraper connection URLs.

Build & run

```bash
docker compose up -d --build
```

The application creates its PostgreSQL tables automatically on startup. For an
existing PostgreSQL volume, set `POSTGRES_PASSWORD` to the current database
password. To rotate it, open `docker compose exec postgres psql -U gguser -d ggpoker`
and run `\password gguser`. Update `POSTGRES_PASSWORD` in `.env` and run
`docker compose up -d` to apply the new connection settings. If you use a
separate `DATABASE_URL` in `.env` for local tools, update it too.

For DBeaver, connect to PostgreSQL at `127.0.0.1:5433` using database `ggpoker`,
user `gguser`, and your PostgreSQL password. For a VPS, enter the same database
settings in DBeaver's Main tab and enable the SSH tunnel using your VPS address
and SSH login on port `22`.

Persisted data

- PostgreSQL data is stored in the `gg_postgres_data` Docker volume
- The old SQLite file in `./data` is not migrated automatically

Environment overrides (edit `docker-compose.yml` or pass with `-e`)

- `INTERVAL` (e.g., `600`)
- `DB_PATH` (container default `/data/ggpoker_leaderboards.db`)
- `DATABASE_URL` (use PostgreSQL when running multiple scraper instances)
- `PROMO_URL` and `GAME_NAME` (target page and game for each instance)
- `CHROME_BIN=/usr/bin/chromium`, `CHROME_TYPE=chromium`
- For the API: `DATABASE_PRIVATE_URL` / `DATABASE_URL` / `DATABASE_PUBLIC_URL`

Run multiple scraper instances

Multiple instances should share PostgreSQL, rather than the mounted SQLite file. The
The Compose scrapers use its local PostgreSQL service. Start one container per
target with a unique name:

```bash
docker compose build scraper
docker compose run -d --name gg_scraper_plo scraper
docker compose run -d --name gg_scraper_nlh \
  -e PROMO_URL=https://ggpoker.com//poker-promotions/holdem-daily-leaderboard/ \
  -e GAME_NAME=NLH scraper
```

The first command uses the defaults from `.env`; each additional command can override
only `PROMO_URL` and `GAME_NAME`. Do not run multiple instances against the same SQLite
file: SQLite serializes writes and can produce lock errors under concurrent scraping.

Stop

```bash
docker compose down
```

## How the Scraper Works (High-Level)

1. Opens the promo page and locates the configured game section (e.g., `PLO`)
2. Finds the embedded leaderboard iframe and opens its URL in a new tab
3. Enumerates blind-level options from the dropdown
4. Clicks through each blind level and extracts the ranking table
5. Upserts normalized rows into the database with a new `update_batch` timestamp

## Troubleshooting

- Chromedriver/Chrome mismatch: ensure Chrome is installed locally, or run in Docker
- PostgreSQL connection: verify `DATABASE_URL` and network access
- Empty `/tables`: ensure the scraper has run and written data to the DB

## Security Notes

- Do not commit `.env`; it contains credentials
- Prefer private DB URLs (`DATABASE_PRIVATE_URL`) for internal services
