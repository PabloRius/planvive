#!/usr/bin/env python3
"""planvive — scraper del listado de solicitantes del Plan Vive (Comunidad de Madrid).

Fuente pública: https://vivetuavalon.com/plan-vive/lists-inscription/

La web renderiza la tabla en el cliente consumiendo una API JSON paginada
(descubierta en el bundle `inscriptionList.*.js`):

    GET https://vivetuavalon.com/plan-vive/lists/?page=<N>&lote=<Lote X>&search=

Respuesta (estilo Django REST Framework): `count`, `next`, `previous`, `results`
(20 registros por página, page_size fijo no configurable). Cada registro ya
incluye el campo `lote`, que se vuelca como columna del CSV para filtrar.

Throttling del servidor (medido)
--------------------------------
Hay DOS límites, ambos **por IP de origen**:
  * Aplicación (DRF): responde JSON `{"detail": "...throttled... available in N
    seconds"}`.
  * Edge / infraestructura (Google Cloud): responde una página HTML `429 Too
    Many Requests`. Si se dispara, banea la IP ~3 min.
Una sola IP aguanta como mucho ~1 req/s sostenida. Para ir en paralelo hay que
repartir la carga entre varias IPs de salida -> **proxies** (ver más abajo).

Paralelismo con proxies
------------------------
El throttle es por IP, así que N IPs ~= N veces el presupuesto. El scraper
regula el ritmo **por proxy** (`--per-proxy-interval`), de modo que cada IP se
mantiene por debajo de su límite mientras el conjunto avanza en paralelo.

  * Pool de proxies (datacenter/residencial):
        --proxy http://user:pass@ip1:port --proxy http://user:pass@ip2:port
        --proxy-file proxies.txt        # una URL de proxy por línea
    Round-robin entre proxies; ante 429 el proxy entra en cooldown y la petición
    se reintenta por otro.
  * Gateway residencial rotativo (una URL, IP nueva por petición):
        --proxy http://user:pass@gw:port --rotating-gateway --workers 25

Reanudable
----------
Cada página se guarda como una línea JSON en `output/checkpoints/<lote>.jsonl`
(`{"page": N, "rows": [...]}`). Si se corta, al relanzar se saltan las páginas
completadas. El CSV se reconstruye siempre desde los checkpoints, deduplicando
por `id` (la lista es un dato vivo y puede solapar registros entre páginas).

Uso
---
    python3 planvive.py                                   # single-IP (lento)
    python3 planvive.py --proxy-file proxies.txt --workers 12
    python3 planvive.py --limit-pages 20 --output-dir /tmp/pv_test   # validar
    python3 planvive.py --rebuild-csv                     # solo reconstruir CSV
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

# --- Configuración de la fuente ------------------------------------------------

BASE_URL = "https://vivetuavalon.com"
LIST_ENDPOINT = f"{BASE_URL}/plan-vive/lists/"
PAGE_SIZE = 20  # fijo en el servidor

DEFAULT_LOTES = ["Lote 1", "Lote 2"]

CSV_FIELDS = [
    "scraped_at",       # Fecha/hora de ESTE scrape (ISO 8601) — sella el snapshot
    "id",               # Código
    "timestamp",        # Fecha y hora de la solicitud (ISO 8601 UTC)
    "municipality",     # Municipio
    "lote",             # Lote (columna de filtrado)
    "adapted_housing",  # Vivienda adaptada (True/False)
    "priority",         # Nivel de prioridad
    "status",           # Estado
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{BASE_URL}/plan-vive/lists-inscription/",
}

# Ritmo por IP. Una sola IP aguanta ~1 req/s; dejamos margen.
DEFAULT_PER_PROXY_INTERVAL = 0.9   # s entre peticiones de una MISMA IP (pool/directo)
# Gateway rotativo: cada petición sale por una IP distinta, así que el intervalo
# es un cap GLOBAL de ritmo (no por IP). La concurrencia la dan los --workers.
DEFAULT_GATEWAY_INTERVAL = 0.12    # ~8 req/s de cap global
PROXY_COOLDOWN_ON_429 = 8.0        # s que descansa un proxy tras un 429
MAX_ATTEMPTS = 12                  # 429 NO es fatal: se reintenta (rotando IP)
TIMEOUT = 30


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


# --- Endpoints de salida (una IP = un Endpoint) --------------------------------

class Endpoint:
    """Una vía de salida (directa o vía proxy) con su propio regulador de ritmo."""

    def __init__(self, proxy_url: str | None, interval: float) -> None:
        self.proxy_url = proxy_url
        self.label = proxy_url.split("@")[-1] if proxy_url else "direct"
        self._interval = interval
        self._lock = threading.Lock()
        self._next = time.monotonic()
        if proxy_url:
            handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            self.opener = urllib.request.build_opener(handler)
        else:
            self.opener = urllib.request.build_opener()

    def wait_turn(self) -> None:
        """Espaciado por-IP: no lanza otra petición hasta pasar `interval`."""
        with self._lock:
            now = time.monotonic()
            wait_for = self._next - now
            self._next = max(now, self._next) + self._interval
        if wait_for > 0:
            time.sleep(wait_for)

    def cooldown(self, seconds: float) -> None:
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


class EndpointRotator:
    """Reparte peticiones entre endpoints (round-robin, thread-safe)."""

    def __init__(self, endpoints: list[Endpoint], rotating_gateway: bool) -> None:
        self.endpoints = endpoints
        self.rotating_gateway = rotating_gateway
        self._cycle = itertools.cycle(endpoints)
        self._lock = threading.Lock()

    def next(self) -> Endpoint:
        with self._lock:
            return next(self._cycle)


# --- Contadores globales -------------------------------------------------------

class Counters:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.http_429 = 0
        self.errors = 0

    def add(self, requests: int = 0, http_429: int = 0, errors: int = 0) -> None:
        with self.lock:
            self.requests += requests
            self.http_429 += http_429
            self.errors += errors


def fetch_page(lote: str, page: int, rotator: EndpointRotator, counters: Counters) -> dict:
    """Descarga una página rotando de IP ante 429/errores. 429 no es fatal."""
    url = LIST_ENDPOINT + "?" + urlencode({"page": page, "lote": lote, "search": ""})
    last_err: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        ep = rotator.next()
        ep.wait_turn()
        req = urllib.request.Request(url, headers=HEADERS)
        counters.add(requests=1)
        try:
            with ep.opener.open(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code == 429:
                counters.add(http_429=1)
                ep.cooldown(PROXY_COOLDOWN_ON_429)
                # Backoff suave; con pool la siguiente vuelta usa otra IP.
                time.sleep(min(1.0 * attempt, 5.0))
                continue
            if exc.code in (500, 502, 503, 504):
                counters.add(errors=1)
                time.sleep(1.0 * attempt)
                continue
            counters.add(errors=1)
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_err = exc
            counters.add(errors=1)
            ep.cooldown(3.0)
            time.sleep(1.0 * attempt)
            continue
    raise RuntimeError(f"lote={lote!r} page={page} falló tras {MAX_ATTEMPTS} intentos: {last_err}")


# --- Checkpoints ---------------------------------------------------------------

def checkpoint_path(ckpt_dir: Path, lote: str) -> Path:
    return ckpt_dir / f"{lote.lower().replace(' ', '_')}.jsonl"


def load_done_pages(path: Path) -> set[int]:
    done: set[int] = set()
    if not path.exists():
        return done
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                done.add(int(json.loads(line)["page"]))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return done


def scrape_lote(
    lote: str,
    ckpt_dir: Path,
    rotator: EndpointRotator,
    counters: Counters,
    workers: int,
    limit_pages: int | None = None,
) -> dict:
    path = checkpoint_path(ckpt_dir, lote)
    done = load_done_pages(path)

    first = fetch_page(lote, 1, rotator, counters)
    count = int(first["count"])
    total_pages = max(1, math.ceil(count / PAGE_SIZE))
    if limit_pages:
        total_pages = min(total_pages, limit_pages)

    log(f"[{lote}] count={count:,} -> {total_pages:,} páginas "
        f"(ya completadas: {len(done):,}, workers={workers})")

    write_lock = threading.Lock()
    fh = path.open("a", encoding="utf-8")

    def persist(page: int, results: list[dict]) -> None:
        line = json.dumps({"page": page, "rows": results}, ensure_ascii=False)
        with write_lock:
            fh.write(line + "\n")
            fh.flush()

    if 1 not in done:
        persist(1, first["results"])
        done.add(1)

    pending = [p for p in range(2, total_pages + 1) if p not in done]
    saved_pages = len(done)
    failed_pages: list[int] = []
    fetched_now = 0
    processed = 0
    t0 = time.time()
    prog_lock = threading.Lock()

    def worker(page: int) -> tuple[int, bool]:
        try:
            data = fetch_page(lote, page, rotator, counters)
            persist(page, data["results"])
            return page, True
        except Exception as exc:  # noqa: BLE001
            log(f"[{lote}] page={page} ERROR: {exc}")
            return page, False

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(worker, p): p for p in pending}
            for fut in as_completed(futures):
                page, ok = fut.result()
                with prog_lock:
                    processed += 1
                    if ok:
                        saved_pages += 1
                        fetched_now += 1
                    else:
                        failed_pages.append(page)
                    if processed % 100 == 0 and fetched_now:
                        rate = fetched_now / max(1e-9, time.time() - t0)
                        remaining = len(pending) - processed
                        eta = (remaining / rate) / 60 if rate else 0
                        with counters.lock:
                            n429 = counters.http_429
                        log(f"[{lote}] {saved_pages:,}/{total_pages:,} "
                            f"({rate:.1f} req/s útil, 429 acum={n429}, ETA {eta:.0f} min)")
    finally:
        fh.close()

    return {
        "lote": lote,
        "count_reported": count,
        "total_pages": total_pages,
        "pages_saved": saved_pages,
        "pages_failed": sorted(set(failed_pages)),
        "runtime_seconds": round(time.time() - t0, 1),
    }


# --- Reconstrucción del CSV ----------------------------------------------------

def rebuild_csv(lotes: list[str], ckpt_dir: Path, out_csv: Path, scraped_at: str) -> dict:
    """Escribe el CSV del snapshot, sellando cada fila con `scraped_at`."""
    seen: set[str] = set()
    per_lote: dict[str, int] = {}
    rows_written = 0
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as csv_fh:
        writer = csv.DictWriter(csv_fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for lote in lotes:
            path = checkpoint_path(ckpt_dir, lote)
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for row in rec.get("rows", []):
                        rid = row.get("id")
                        if rid in seen:
                            continue
                        seen.add(rid)
                        row["scraped_at"] = scraped_at
                        writer.writerow(row)
                        rows_written += 1
                        key = row.get("lote", lote)
                        per_lote[key] = per_lote.get(key, 0) + 1
    return {"csv": str(out_csv), "rows": rows_written, "per_lote": per_lote}


# --- Construcción del pool de proxies ------------------------------------------

def build_rotator(args: argparse.Namespace) -> EndpointRotator:
    proxies: list[str] = list(args.proxy or [])
    if args.proxy_file:
        for line in Path(args.proxy_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                proxies.append(line)
    interval = args.per_proxy_interval
    if proxies:
        endpoints = [Endpoint(p, interval) for p in proxies]
        log(f"Pool de {len(endpoints)} proxy(s), intervalo {interval}s/IP"
            + (" [gateway rotativo]" if args.rotating_gateway else ""))
    else:
        endpoints = [Endpoint(None, interval)]
        log(f"Sin proxies: 1 IP directa, intervalo {interval}s")
    return EndpointRotator(endpoints, args.rotating_gateway)


def main() -> None:
    module_dir = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Scraper del listado del Plan Vive (lotes 1 y 2).")
    p.add_argument("--lotes", nargs="+", default=DEFAULT_LOTES)
    p.add_argument("--output-dir", type=Path, default=module_dir / "output")
    p.add_argument("--csv-name", default=None,
                   help="Nombre fijo del CSV (por defecto: snapshots/planvive_<fecha>.csv).")
    p.add_argument("--snapshot-date", default=None,
                   help="Fecha del snapshot YYYY-MM-DD (por defecto: hoy).")
    p.add_argument("--limit-pages", type=int, default=None, help="Máx. páginas por lote (validación).")
    p.add_argument("--rebuild-csv", action="store_true", help="Solo reconstruir CSV desde checkpoints.")
    # Proxies / paralelismo
    p.add_argument("--proxy", action="append", help="URL de proxy (repetible).")
    p.add_argument("--proxy-file", type=Path, help="Fichero con una URL de proxy por línea.")
    p.add_argument("--rotating-gateway", action="store_true",
                   help="El proxy es un gateway con IP rotativa por petición.")
    p.add_argument("--workers", type=int, default=None,
                   help="Concurrencia (por defecto: 2×nº proxies, o 1 sin proxy).")
    p.add_argument("--per-proxy-interval", type=float, default=None,
                   help=f"Segundos entre peticiones de una misma IP (def. {DEFAULT_PER_PROXY_INTERVAL}; "
                        f"gateway rotativo: cap global, def. {DEFAULT_GATEWAY_INTERVAL}).")
    args = p.parse_args()

    # Resolución del intervalo según modo (si el usuario no lo fija).
    if args.per_proxy_interval is None:
        args.per_proxy_interval = (
            DEFAULT_GATEWAY_INTERVAL if args.rotating_gateway else DEFAULT_PER_PROXY_INTERVAL
        )

    out_dir: Path = args.output_dir
    ckpt_dir = out_dir / "checkpoints"
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    # Sello temporal de ESTE scrape: fecha (clave del snapshot diario) + ISO.
    started = datetime.now().astimezone()
    scraped_at = started.isoformat(timespec="seconds")
    snapshot_date = args.snapshot_date or started.strftime("%Y-%m-%d")
    if args.csv_name:
        out_csv = out_dir / args.csv_name
    else:
        out_csv = out_dir / "snapshots" / f"planvive_{snapshot_date}.csv"

    stats: list[dict] = []
    counters = Counters()
    t0 = time.time()

    if not args.rebuild_csv:
        rotator = build_rotator(args)
        n_ep = len(rotator.endpoints)
        if args.workers:
            workers = args.workers
        elif args.rotating_gateway:
            workers = 20
        else:
            workers = max(1, 2 * n_ep) if n_ep > 1 else 1
        for lote in args.lotes:
            stats.append(scrape_lote(lote, ckpt_dir, rotator, counters, workers, args.limit_pages))

    csv_stats = rebuild_csv(args.lotes, ckpt_dir, out_csv, scraped_at)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scraped_at": scraped_at,
        "snapshot_date": snapshot_date,
        "lotes": args.lotes,
        "scrape": stats,
        "requests_total": counters.requests,
        "http_429": counters.http_429,
        "errors": counters.errors,
        "csv": csv_stats,
        "total_runtime_seconds": round(time.time() - t0, 1),
    }
    (out_dir / "_run_stats.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    log("=" * 60)
    log(f"CSV: {csv_stats['csv']}")
    log(f"Filas únicas: {csv_stats['rows']:,}")
    for lote, n in csv_stats["per_lote"].items():
        log(f"  {lote}: {n:,}")
    if counters.requests:
        pct = 100 * counters.http_429 / counters.requests
        log(f"Peticiones: {counters.requests:,} | 429: {counters.http_429:,} ({pct:.1f}%) | errores: {counters.errors:,}")
    for s in stats:
        if s["pages_failed"]:
            log(f"  ⚠️ {s['lote']} páginas fallidas ({len(s['pages_failed'])}): {s['pages_failed'][:20]}")
    log(f"Tiempo total: {summary['total_runtime_seconds']/60:.1f} min")


if __name__ == "__main__":
    main()
