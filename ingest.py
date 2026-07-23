#!/usr/bin/env python3
"""ingest.py — motor de captura de cambios (CDC) del Plan Vive.

El listado es un snapshot del **estado actual**: cada `id` aparece una sola vez,
con su último estado. Para reconstruir la historia de cada solicitud hay que
diffear snapshots diarios y guardar SOLO los cambios. Esto hace justo eso:

  · Lee el snapshot del día (`output/snapshots/planvive_<fecha>.csv`).
  · Lo compara contra el estado guardado en SQLite (`planvive.db`).
  · Registra eventos: altas (created), cambios de estado/prioridad,
    bajas (disappeared) y reapariciones (reappeared).
  · Materializa el estado vigente (`current_state`) y la dimensión `solicitud`.

Guardar deltas en vez de copias completas mantiene la BD compacta y permite
métricas temporales por solicitud (tiempos de resolución, transiciones, tasas de
conversión, flujo diario) — todo agregable por municipio.

Comportamiento por defecto: ingiere en orden de fecha **todos** los snapshots que
aún no estén en la BD (así la primera ejecución hace la carga *génesis* y las
siguientes van al día; también reconstruye la BD desde un archivo de CSVs).

Avisos sobre la historia:
  · Resolución diaria: un cambio ocurrió *entre* dos scrapes (precisión ±1 día).
  · Censura por la izquierda: los ids ya existentes el primer día entran como
    `created` en la fecha de génesis (no es un alta real). Las transiciones
    *observadas en directo* (de ahí en adelante) son las que dan duraciones
    precisas. `submitted_at` conserva la fecha de creación real de cada solicitud.

Uso:
    python3 ingest.py                      # ingiere snapshots nuevos -> planvive.db
    python3 ingest.py --csv ruta.csv       # ingiere un CSV concreto
    python3 ingest.py --stats              # resumen de la BD
    python3 ingest.py --db otra.db         # otra ruta de BD
"""

from __future__ import annotations

import argparse
import csv
import gzip
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

csv.field_size_limit(10_000_000)

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

SCHEMA = """
CREATE TABLE IF NOT EXISTS solicitud (
  id TEXT PRIMARY KEY,
  lote TEXT NOT NULL,
  municipality TEXT NOT NULL,
  adapted_housing INTEGER NOT NULL,
  submitted_at TEXT,          -- timestamp original de la solicitud (creación, preciso)
  first_seen TEXT NOT NULL,   -- fecha de scrape en que apareció por primera vez
  last_seen TEXT NOT NULL     -- fecha de scrape más reciente en que seguía en la lista
);
CREATE TABLE IF NOT EXISTS current_state (
  id TEXT PRIMARY KEY REFERENCES solicitud(id),
  status TEXT NOT NULL,
  priority TEXT NOT NULL,
  status_since TEXT NOT NULL, -- fecha de scrape desde la que tiene este estado
  present INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS event (
  id TEXT NOT NULL REFERENCES solicitud(id),
  scrape_date TEXT NOT NULL,  -- día en que OBSERVAMOS el cambio
  kind TEXT NOT NULL,         -- created | status | priority | disappeared | reappeared
  status TEXT,
  prev_status TEXT,
  priority TEXT,
  prev_priority TEXT,
  PRIMARY KEY (id, scrape_date, kind)
);
CREATE INDEX IF NOT EXISTS ix_event_date ON event(scrape_date);
CREATE INDEX IF NOT EXISTS ix_event_kind ON event(kind, status);
CREATE INDEX IF NOT EXISTS ix_solicitud_muni ON solicitud(municipality);
CREATE TABLE IF NOT EXISTS scrape_run (
  scrape_date TEXT PRIMARY KEY,
  scraped_at TEXT,
  total_rows INTEGER,
  new_ids INTEGER,
  status_changes INTEGER,
  priority_changes INTEGER,
  disappeared INTEGER,
  reappeared INTEGER,
  ingested_at TEXT
);
"""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def read_snapshot(path: Path) -> tuple[dict[str, dict], str]:
    opener = gzip.open if path.suffix == ".gz" else open
    rows: dict[str, dict] = {}
    scraped_at = ""
    with opener(path, "rt", encoding="utf-8", newline="") as fh:  # type: ignore[operator]
        reader = csv.DictReader(fh)
        for row in reader:
            if not scraped_at:
                scraped_at = row.get("scraped_at", "")
            rows[row["id"]] = row
    return rows, scraped_at


def snapshot_date(path: Path, scraped_at: str) -> str:
    if scraped_at[:10]:
        return scraped_at[:10]
    m = DATE_RE.search(path.name)
    return m.group(1) if m else ""


def ingest_one(conn: sqlite3.Connection, path: Path, force: bool = False) -> dict | None:
    rows, scraped_at = read_snapshot(path)
    sdate = snapshot_date(path, scraped_at)
    if not sdate:
        log(f"  ! {path.name}: no puedo determinar la fecha; lo salto")
        return None

    done = conn.execute("SELECT 1 FROM scrape_run WHERE scrape_date=?", (sdate,)).fetchone()
    if done and not force:
        log(f"  = {sdate} ya ingerido; lo salto")
        return None

    # La primera ingesta es la "génesis": la lista ya existente entra como línea
    # base. No emitimos eventos 'created' para esos 226k (no son altas reales y su
    # creación ya está en `submitted_at`); solo se registran 'created' para altas
    # observadas de un día para otro. Esto mantiene la BD compacta.
    genesis = conn.execute("SELECT COUNT(*) FROM scrape_run").fetchone()[0] == 0

    # Estado vigente actual en memoria: id -> (status, priority, present)
    cur = {
        r[0]: (r[1], r[2], r[3])
        for r in conn.execute("SELECT id, status, priority, present FROM current_state")
    }

    # Salvaguarda: un scrape defectuoso (muchas menos filas) marcaría miles de
    # bajas falsas y corrompería el historial. Si el snapshot es anómalamente
    # pequeño frente a lo vigente, se rechaza (salvo --force).
    present_count = sum(1 for _s, _p, present in cur.values() if present)
    if present_count and len(rows) < 0.5 * present_count and not force:
        log(f"  ! {sdate}: snapshot sospechoso ({len(rows):,} filas vs {present_count:,} vigentes). "
            f"Lo rechazo para no corromper el historial (usa --force para forzar).")
        return None

    ins_sol, ins_cur, ins_evt = [], [], []
    upd_lastseen, upd_state, upd_present_on = [], [], []
    n_new = n_status = n_prio = n_disap = n_reap = 0

    for rid, row in rows.items():
        status = row.get("status", "")
        priority = row.get("priority", "")
        if rid not in cur:
            adp = 1 if row.get("adapted_housing", "") in ("True", "true", "1") else 0
            ins_sol.append((rid, row.get("lote", ""), row.get("municipality", ""), adp,
                            row.get("timestamp", ""), sdate, sdate))
            ins_cur.append((rid, status, priority, sdate, 1))
            if not genesis:
                ins_evt.append((rid, sdate, "created", status, None, priority, None))
            n_new += 1
        else:
            prev_status, prev_prio, present = cur[rid]
            upd_lastseen.append((sdate, rid))
            if not present:
                ins_evt.append((rid, sdate, "reappeared", status, prev_status, priority, prev_prio))
                upd_present_on.append((rid,))
                n_reap += 1
            if status != prev_status:
                ins_evt.append((rid, sdate, "status", status, prev_status, priority, prev_prio))
                upd_state.append((status, priority, sdate, rid))
                n_status += 1
            elif priority != prev_prio:
                ins_evt.append((rid, sdate, "priority", status, prev_status, priority, prev_prio))
                upd_state.append((status, priority, sdate, rid))
                n_prio += 1

    # Bajas: presentes en BD pero ausentes hoy
    today_ids = rows.keys()
    disappeared_ids = [rid for rid, (_s, _p, present) in cur.items() if present and rid not in today_ids]
    for rid in disappeared_ids:
        prev_status, prev_prio, _ = cur[rid]
        ins_evt.append((rid, sdate, "disappeared", None, prev_status, None, prev_prio))
        n_disap += 1

    conn.executemany(
        "INSERT OR IGNORE INTO solicitud(id,lote,municipality,adapted_housing,submitted_at,first_seen,last_seen)"
        " VALUES(?,?,?,?,?,?,?)", ins_sol)
    conn.executemany(
        "INSERT OR IGNORE INTO current_state(id,status,priority,status_since,present) VALUES(?,?,?,?,?)", ins_cur)
    conn.executemany("UPDATE solicitud SET last_seen=? WHERE id=?", upd_lastseen)
    conn.executemany("UPDATE current_state SET present=1 WHERE id=?", upd_present_on)
    conn.executemany("UPDATE current_state SET status=?, priority=?, status_since=? WHERE id=?", upd_state)
    conn.executemany("UPDATE current_state SET present=0 WHERE id=?", [(rid,) for rid in disappeared_ids])
    conn.executemany(
        "INSERT OR IGNORE INTO event(id,scrape_date,kind,status,prev_status,priority,prev_priority)"
        " VALUES(?,?,?,?,?,?,?)", ins_evt)
    conn.execute(
        "INSERT OR REPLACE INTO scrape_run"
        "(scrape_date,scraped_at,total_rows,new_ids,status_changes,priority_changes,disappeared,reappeared,ingested_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (sdate, scraped_at, len(rows), n_new, n_status, n_prio, n_disap, n_reap,
         datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")))
    conn.commit()

    stats = {"date": sdate, "total": len(rows), "new": n_new, "status_changes": n_status,
             "priority_changes": n_prio, "disappeared": n_disap, "reappeared": n_reap}
    log(f"  + {sdate}: total={len(rows):,} nuevas={n_new:,} cambios_estado={n_status:,} "
        f"cambios_prioridad={n_prio:,} bajas={n_disap:,} reaparecidas={n_reap:,}")
    return stats


def pending_snapshots(conn: sqlite3.Connection, snap_dir: Path) -> list[Path]:
    ingested = {r[0] for r in conn.execute("SELECT scrape_date FROM scrape_run")}
    files = sorted(list(snap_dir.glob("planvive_*.csv")) + list(snap_dir.glob("planvive_*.csv.gz")))
    out = []
    for f in files:
        m = DATE_RE.search(f.name)
        if m and m.group(1) not in ingested:
            out.append(f)
    return out


def print_stats(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*), SUM(present) FROM current_state").fetchone()
    n_sol = conn.execute("SELECT COUNT(*) FROM solicitud").fetchone()[0]
    n_evt = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    runs = conn.execute("SELECT COUNT(*), MIN(scrape_date), MAX(scrape_date) FROM scrape_run").fetchone()
    log("── Resumen BD ──────────────────────────────")
    log(f"  solicitudes (dimensión): {n_sol:,}")
    log(f"  vigentes / presentes:    {row[0]:,} / {row[1] or 0:,}")
    log(f"  eventos registrados:     {n_evt:,}")
    log(f"  scrapes ingeridos:       {runs[0]} ({runs[1]} → {runs[2]})")
    log("  estado actual (present=1):")
    for st, c in conn.execute(
            "SELECT status, COUNT(*) FROM current_state WHERE present=1 GROUP BY status ORDER BY 2 DESC"):
        log(f"      {st:<20} {c:,}")
    log("  eventos por tipo:")
    for k, c in conn.execute("SELECT kind, COUNT(*) FROM event GROUP BY kind ORDER BY 2 DESC"):
        log(f"      {k:<14} {c:,}")


def main() -> None:
    module_dir = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Motor CDC del Plan Vive (diff de snapshots -> SQLite).")
    p.add_argument("--db", type=Path, default=module_dir / "planvive.db")
    p.add_argument("--snapshots-dir", type=Path, default=module_dir / "output" / "snapshots")
    p.add_argument("--csv", type=Path, default=None, help="Ingerir un CSV concreto (.csv o .csv.gz).")
    p.add_argument("--force", action="store_true", help="Re-ingerir aunque la fecha ya conste.")
    p.add_argument("--stats", action="store_true", help="Solo mostrar el resumen de la BD.")
    args = p.parse_args()

    conn = open_db(args.db)
    if args.stats:
        print_stats(conn)
        return

    if args.csv:
        targets = [args.csv]
    else:
        targets = pending_snapshots(conn, args.snapshots_dir)
        if not targets:
            log("No hay snapshots nuevos que ingerir.")

    log(f"BD: {args.db}")
    for path in targets:
        ingest_one(conn, path, force=args.force)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print_stats(conn)


if __name__ == "__main__":
    main()
