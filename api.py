#!/usr/bin/env python3
"""
HTTP API for listing and reading tables similarly to the viewer.

Endpoints:
  GET  /health                                  → { status: ok }
  GET  /tables                                  → ["PLO_...", ...]
  GET  /tables/<table>/columns                  → column metadata
  GET  /tables/<table>/data?last_columns=10&limit=10
      → first column + last N columns, sorted by the last column DESC with NULLS LAST,
        returns up to 'limit' rows (defaults: last_columns=10, limit=10)

Notes:
  - Prefers DATABASE_PRIVATE_URL, then DATABASE_URL, then DATABASE_PUBLIC_URL
  - Uses short-lived autocommit connections to avoid long-held locks
  - Sorting uses numeric parsing with NULLS LAST for robust ordering
  - CORS enabled
"""

import os
from decimal import Decimal
from typing import Dict, List, Tuple
from urllib.parse import urlparse

from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
import psycopg2
from psycopg2 import sql
import sqlite3


def resolve_db_config():
    """Decide whether to use PostgreSQL or SQLite based on env vars.
    Returns a tuple (driver, conn_info), where driver is 'postgres' or 'sqlite'.
    For postgres, conn_info is a parsed urlparse result; for sqlite, it's a file path.
    """
    load_dotenv()

    sqlite_path = os.getenv("SQLITE_DB_PATH")
    if sqlite_path:
        return ("sqlite", sqlite_path)

    pg_url = None
    for key in ("DATABASE_PRIVATE_URL", "DATABASE_URL", "DATABASE_PUBLIC_URL"):
        val = os.getenv(key)
        if val and (val.startswith("postgres://") or val.startswith("postgresql://")):
            pg_url = val
            break
    db_path = os.getenv("DB_PATH")
    if not pg_url and not db_path:
        # Local API runs reach the Compose database through the published host port.
        password = os.getenv("POSTGRES_PASSWORD", "")
        pg_url = f"postgresql://gguser:{password}@127.0.0.1:5433/ggpoker"
    if pg_url:
        return ("postgres", urlparse(pg_url))
    if db_path:
        return ("sqlite", db_path)
    # default to local sqlite file if nothing provided
    return ("sqlite", os.path.abspath("gg_leaderboards.db"))


def open_connection():
    """Open a short-lived autocommit connection to the configured DB."""
    driver, info = resolve_db_config()
    if driver == "sqlite":
        conn = sqlite3.connect(info)
        conn.row_factory = sqlite3.Row
        return conn
    # postgres
    parsed = info
    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=(parsed.path[1:] if parsed.path else None),
        user=parsed.username,
        password=parsed.password,
    )
    conn.autocommit = True
    return conn


def list_tables() -> List[str]:
    """List all leaderboard names (works for both PG and SQLite)."""
    driver, _ = resolve_db_config()
    with open_connection() as conn:
        cur = conn.cursor()
        if driver == "sqlite":
            cur.execute("SELECT name FROM leaderboards ORDER BY name")
            rows = cur.fetchall()
            return [row["name"] for row in rows]
        # postgres: read from leaderboards table too
        cur.execute("SELECT name FROM leaderboards ORDER BY name")
        return [r[0] for r in cur.fetchall()]


def fetch_table_data(
    leaderboard: str, limit: int, last_columns: int
) -> Dict[str, List]:
    driver, _ = resolve_db_config()
    if driver == "sqlite":
        with open_connection() as conn:
            cur = conn.cursor()
            # Resolve leaderboard id
            cur.execute("SELECT id FROM leaderboards WHERE name = ?", (leaderboard,))
            row = cur.fetchone()
            if not row:
                return {"columns": [], "rows": []}
            lb_id = row[0]

            # Latest N distinct updates for this leaderboard (ids and timestamps)
            cur.execute(
                """
                SELECT ub.id, ub.ts
                FROM update_batch ub
                JOIN facts f ON f.update_id = ub.id
                WHERE f.leaderboard_id = ?
                GROUP BY ub.id, ub.ts
                ORDER BY ub.id DESC
                LIMIT ?
                """,
                (lb_id, last_columns),
            )
            updates = cur.fetchall()
            if not updates:
                return {"columns": ["player"], "rows": []}
            update_ids_desc = [u[0] for u in updates]
            ts_headers_desc = [u[1] for u in updates]
            update_ids = list(reversed(update_ids_desc))
            ts_headers = list(reversed(ts_headers_desc))

            placeholders_updates = ",".join(["?"] * len(update_ids))
            cur.execute(
                (f"""
                    SELECT pl.name, ub.ts, f.points
                    FROM facts f
                    JOIN players pl ON pl.id = f.player_id
                    JOIN update_batch ub ON ub.id = f.update_id
                    WHERE f.leaderboard_id = ?
                      AND f.update_id IN ({placeholders_updates})
                    """),
                [lb_id, *update_ids],
            )
            all_points = cur.fetchall()
            if not all_points:
                return {"columns": ["player"] + ts_headers, "rows": []}

            players = sorted({name for (name, _ts, _pts) in all_points})

            points_map: Dict[str, Dict[str, float]] = {
                name: {ts: 0 for ts in ts_headers} for name in players
            }
            for name, ts, pts in all_points:
                if ts in points_map[name]:
                    points_map[name][ts] = pts

            rows = []
            for name in players:
                row = [name] + [points_map[name][ts] for ts in ts_headers]
                rows.append(row)

            rows.sort(key=lambda r: (r[-1] if r[-1] is not None else 0), reverse=True)
            rows = rows[:limit]

            return {"columns": ["player"] + ts_headers, "rows": rows}
    else:
        # Postgres branch
        with open_connection() as conn:
            with conn.cursor() as cur:
                # Resolve leaderboard id
                cur.execute(
                    "SELECT id FROM leaderboards WHERE name = %s", (leaderboard,)
                )
                row = cur.fetchone()
                if not row:
                    return {"columns": [], "rows": []}
                lb_id = row[0]
                # Latest N updates
                cur.execute(
                    """
                    SELECT ub.id, ub.ts
                    FROM update_batch ub
                    JOIN facts f ON f.update_id = ub.id
                    WHERE f.leaderboard_id = %s
                    GROUP BY ub.id, ub.ts
                    ORDER BY ub.id DESC
                    LIMIT %s
                    """,
                    (lb_id, last_columns),
                )
                updates = cur.fetchall()
                if not updates:
                    return {"columns": ["player"], "rows": []}
                update_ids_desc = [u[0] for u in updates]
                ts_headers_desc = [u[1] for u in updates]
                update_ids = list(reversed(update_ids_desc))
                ts_headers = list(reversed(ts_headers_desc))

                # Build IN placeholders
                in_ph = ",".join(["%s"] * len(update_ids))
                cur.execute(
                    f"""
                    SELECT pl.name, ub.ts, f.points
                    FROM facts f
                    JOIN players pl ON pl.id = f.player_id
                    JOIN update_batch ub ON ub.id = f.update_id
                    WHERE f.leaderboard_id = %s
                      AND f.update_id IN ({in_ph})
                    """,
                    tuple([lb_id] + update_ids),
                )
                all_points = cur.fetchall()
                if not all_points:
                    return {"columns": ["player"] + ts_headers, "rows": []}

                players = sorted({name for (name, _ts, _pts) in all_points})
                points_map: Dict[str, Dict[str, float]] = {
                    name: {ts: 0 for ts in ts_headers} for name in players
                }
                for name, ts, pts in all_points:
                    if ts in points_map[name]:
                        points_map[name][ts] = float(pts) if pts is not None else 0

                rows = []
                for name in players:
                    row = [name] + [points_map[name][ts] for ts in ts_headers]
                    rows.append(row)

                rows.sort(
                    key=lambda r: (r[-1] if r[-1] is not None else 0), reverse=True
                )
                rows = rows[:limit]

                return {"columns": ["player"] + ts_headers, "rows": rows}


def fetch_player_data(leaderboard: str, player_name: str) -> Dict[str, List]:
    """ """

    driver, _ = resolve_db_config()

    with open_connection() as conn:
        with conn.cursor() as cur:

            # Fetch country
            cur.execute("SELECT country FROM players WHERE name = %s", (player_name,))
            crow = cur.fetchone()
            country = crow[0] if crow and crow[0] is not None else None

            try:
                cur.execute(
                    """               with sec as (
                                          select l.name as leaderboard,
                                            to_char(timezone('UTC', ts), 'YYYY-MM-DD HH24:MI:SS') as time,ub.id,timezone('UTC', ts),
                                            DATE_TRUNC('day',timezone('UTC', ts) - INTERVAL '8 hours') as start_day,
                                            EXTRACT(epoch FROM 
                                                timezone('UTC', ts) - (DATE_TRUNC('day',timezone('UTC', ts) - INTERVAL '8 hours')+ INTERVAL '8 hours'))
                                                as secs,
                                            p.name as name ,country,points 
                                            from facts 
                                            left join players p  on player_id  = p.id 
                                            left join update_batch ub on update_id =ub.id
                                            left join leaderboards l on leaderboard_id =l.id
                                            where p.name = %s and l.name = %s
                                            order by ub.id desc,points desc),
                                        windows as (
                                        select
                                          	name,country,time,start_day,points ,secs, 
                                            LEAD(secs,1,0) OVER (ORDER BY time DESC) as secs_before,
                                            LEAD(points,1,0) OVER (ORDER BY time DESC) as points_before,
                                            LEAD(start_day,1,TIMESTAMP '1970-01-01 00:00:00') OVER (ORDER BY time DESC) as day_before
                                        from sec
                                        where secs>120
                                        ),
                                        diffs as
                                         (
                                        select 
                                            name,country,time,start_day,points ,secs/86400 as secs, 
                                            CASE
                                            WHEN start_day != day_before THEN 0
                                            ELSE secs_before/86400
                                            END AS secs_before,
                                            secs - secs_before AS sec_diff,
                                            CASE
                                            WHEN start_day != day_before THEN points
                                            ELSE points - points_before
                                            END AS point_diff
                                        from windows
                                        )
                                        select 
                                            timezone('UTC', start_day)::date as day,points,secs,secs_before,point_diff
                                        from diffs
                                        where secs>0.0020833
                                        order by day,secs_before
                                        """,
                    (player_name, leaderboard),
                )
            except Exception as e:
                print("Error executing query:", e)
                raise

            updates = cur.fetchall()
            print(
                f"Fetched {len(updates)} updates for leaderboard '{leaderboard}', player '{player_name}'"
            )
            result = {}

            for i in updates:
                try:
                    if any(i[j] is None for j in range(len(i))):
                        print("Skipping update with None value:", i)
                        continue

                    day = i[0].isoformat()
                    points = int(i[1])

                    interval_start = float(i[3])
                    interval_end = float(i[2])
                    point_diff = int(i[4])
                    if day not in result:
                        result[day] = [
                            {
                                "points": points,
                                "interval_start": interval_start,
                                "interval_end": interval_end,
                                "point_diff": point_diff,
                            }
                        ]
                    else:
                        result[day].append(
                            {
                                "points": points,
                                "interval_start": interval_start,
                                "interval_end": interval_end,
                                "point_diff": point_diff,
                            }
                        )
                except Exception as e:
                    print(i)
                    print("Error processing updates:", e)
                    raise

            # print("updates", result)
            if not updates:
                return {
                    "player": player_name,
                    "country": country,
                    "datapoints": result,
                }

            return {
                "player": player_name,
                "country": country,
                "datapoints": result,
            }


def fetch_top_players(leaderboard: str, limit: int = 10) -> List[str]:

    driver, _ = resolve_db_config()
    if driver == "sqlite":
        with open_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM leaderboards WHERE name = ?", (leaderboard,))
            row = cur.fetchone()
            if not row:
                return []
            lb_id = row[0]
            cur.execute(
                """
               WITH daily AS ( SELECT date(ub.ts) AS d, MAX(ub.id) AS update_id
                  FROM update_batch ub
                  JOIN facts f ON f.update_id = ub.id
                  WHERE f.leaderboard_id = 6 AND (ub.ts AT TIME ZONE 'UTC')::time < '8:00:00' and DATE(ub.ts) >= CURRENT_DATE - INTERVAL '30 days'
                  GROUP BY date(ub.ts)
                 )
                 SELECT pl.name, SUM(f.points) AS total
                FROM facts f
                JOIN daily d     ON d.update_id = f.update_id
                  JOIN players pl  ON pl.id = f.player_id
                  group by pl.name
                  order by total desc 
                LIMIT ?
                """,
                (lb_id, lb_id, limit),
            )
            rows = cur.fetchall()
            cur.execute(
                """
               with dates as (SELECT timezone('UTC', ub.ts)::date AS d, MAX(ub.id) AS update_id
                      FROM update_batch ub
                      JOIN facts f ON f.update_id = ub.id
                      WHERE f.leaderboard_id = 
                      GROUP BY timezone('UTC', ub.ts)::date)
  select MAX(d),MIN(d) from dates
                """,
                (lb_id,),
            )
            date_range = cur.fetchall()
            return {
                "players": [r[0] for r in rows],
                "date_range": [r[0] for r in date_range],
            }
    else:
        with open_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM leaderboards WHERE name = %s", (leaderboard,)
                )
                row = cur.fetchone()
                if not row:
                    return []
                lb_id = row[0]
                cur.execute(
                    """
                    WITH daily AS (
                      SELECT date_trunc('day', ub.ts) AS d, MAX(ub.id) AS update_id
                      FROM update_batch ub
                      JOIN facts f ON f.update_id = ub.id
                      WHERE f.leaderboard_id = %s
                        AND (ub.ts::time) < TIME '8:00:00'
                      GROUP BY date_trunc('day', ub.ts)
                    )
                    SELECT pl.name, SUM(f.points) AS total
                    FROM facts f
                    JOIN daily d    ON d.update_id = f.update_id
                    JOIN players pl ON pl.id = f.player_id
                    WHERE f.leaderboard_id = %s
                    GROUP BY pl.name
                    ORDER BY total DESC
                    LIMIT %s
                    """,
                    (lb_id, lb_id, limit),
                )
                rows = cur.fetchall()
                cur.execute(
                    """  with dates as (SELECT timezone('UTC', ub.ts)::date AS d, MAX(ub.id) AS update_id
                                      FROM update_batch ub
                                      JOIN facts f ON f.update_id = ub.id
                                      WHERE f.leaderboard_id = %s
                                      GROUP BY timezone('UTC', ub.ts)::date)
                  select MIN(d),MAX(d) from dates
                                """,
                    (lb_id,),
                )
                date_range = cur.fetchall()

                return {
                    "players": [r[0] for r in rows],
                    "date_range": date_range[0] if date_range else [None, None],
                }


app = Flask(__name__)
CORS(app)


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/tables")
def api_tables():
    try:
        return jsonify(list_tables())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/tables/<leaderboard>/data")
def api_table_data(leaderboard: str):

    try:
        # Defaults: last_columns=10, limit=15
        limit = int(request.args.get("limit", 16))
        last_cols = int(request.args.get("columns", 2))
        print(
            f"Fetching data for leaderboard '{leaderboard}' with limit={limit} and last_columns={last_cols}"
        )

        payload = fetch_table_data(leaderboard, limit=limit, last_columns=last_cols)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/tables/<table>/player")
def api_table_player(table: str):
    try:
        name = request.args.get("name")
        if not name:
            return jsonify({"error": "Missing required query parameter 'name'"}), 400

        payload = fetch_player_data(table, player_name=name)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/tables/<table>/top-players")
def api_top_players(table: str):
    try:
        limit = int(request.args.get("limit", 50))
        player_names = fetch_top_players(table, limit=limit)
        return jsonify(player_names)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    debug_env = (os.getenv("FLASK_DEBUG") or os.getenv("DEBUG") or "").lower()
    debug = debug_env in ("1", "true", "yes", "on", "dev", "development")
    app.run(host="0.0.0.0", port=port, debug=debug)
