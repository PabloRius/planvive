#!/usr/bin/env python3
"""dashboard.py — genera un informe HTML autónomo del listado del Plan Vive.

Lee el **último snapshot** CSV de `output/snapshots/` (un volcado completo y
actualizado de toda la lista) y construye un único fichero HTML **autocontenido y
offline** (`output/dashboard.html`) que sirve a la vez de plataforma de
visualización y de informe compartible.

Eje temporal = el `timestamp` **de cada solicitud** (cuándo se presentó), no el
momento del scrape. Como el CSV contiene todas las solicitudes desde el inicio del
programa, un solo fichero reconstruye toda la evolución histórica.

Ojo con la semántica: el `status`/`priority` son los **actuales** (del scrape), así
que los desgloses "por fecha de solicitud" son vistas de **cohorte** (de lo
presentado en el periodo X, en qué estado está hoy) — así se etiqueta en el
informe.

Pestañas:
  * Resumen  — KPIs + tendencias (acumulado, nuevas por periodo, estado por cohorte).
  * Agregación — desglose por estado, prioridad, vivienda adaptada y municipio.
  * Datos en bruto — tabla ordenable/filtrable con exportación a CSV.

Todo se filtra por **lote** y por **periodo** (rango sobre la fecha de solicitud),
con selector de granularidad (día/semana/mes) para las tendencias.

Uso:
    python3 dashboard.py                  # último snapshot -> output/dashboard.html
    python3 dashboard.py --csv ruta.csv   # un CSV concreto
    python3 dashboard.py --no-raw         # informe ligero, sin la tabla en bruto
    python3 dashboard.py --max-raw 50000  # limita filas de la tabla en bruto
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

csv.field_size_limit(10_000_000)

SOURCE_URL = "https://vivetuavalon.com/plan-vive/lists-inscription/"
ESTADO_ORDER = ["Creada", "En trámite", "Contrato firmado", "Desistida", "Rechazada"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_epoch(ts: str) -> int:
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return 0


def build_data(csv_path: Path, include_raw: bool, max_raw: int | None) -> dict:
    print(f"  · leyendo {csv_path.name} …", file=sys.stderr, flush=True)

    by_lote: dict[str, int] = defaultdict(int)
    # Series diarias por lote: dim -> lote -> day -> count
    daily_total: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    daily_estado: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    daily_prio: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    daily_muni: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    # Cruce municipio × estado (lo más importante): lote -> day -> (muni, estado) -> n
    daily_mest: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

    estados_seen: set[str] = set()
    prio_seen: set[str] = set()
    muni_seen: set[str] = set()
    days_seen: set[str] = set()
    scraped_at = ""
    raw_rows: list[tuple] = []

    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if not scraped_at:
                scraped_at = row.get("scraped_at", "")
            lote = row.get("lote", "")
            est = row.get("status", "")
            pri = row.get("priority", "")
            mun = row.get("municipality", "")
            adp = row.get("adapted_housing", "") in ("True", "true", "1")
            ts = row.get("timestamp", "")
            day = ts[:10]
            if not DATE_RE.match(day):
                day = None

            by_lote[lote] += 1
            estados_seen.add(est); prio_seen.add(pri); muni_seen.add(mun)
            if day:
                days_seen.add(day)
                daily_total[lote][day] += 1
                daily_estado[lote][day][est] += 1
                daily_prio[lote][day][pri] += 1
                daily_muni[lote][day][mun] += 1
                daily_mest[lote][day][(mun, est)] += 1
            if include_raw:
                raw_rows.append((row.get("id", ""), parse_epoch(ts), mun, lote,
                                 1 if adp else 0, pri, est))

    lotes = sorted(by_lote)
    estados = [e for e in ESTADO_ORDER if e in estados_seen] + \
              sorted(e for e in estados_seen if e not in ESTADO_ORDER)
    prioridades = sorted(prio_seen)
    municipios = sorted(muni_seen)
    days = sorted(days_seen)
    day_idx = {d: i for i, d in enumerate(days)}
    n = len(days)

    def to_arr(day_map: dict[str, int]) -> list[int]:
        arr = [0] * n
        for d, c in day_map.items():
            arr[day_idx[d]] = c
        return arr

    def to_arr_dim(day_map: dict[str, dict[str, int]], keys: list[str]) -> dict[str, list[int]]:
        out = {k: [0] * n for k in keys}
        for d, km in day_map.items():
            i = day_idx[d]
            for k, c in km.items():
                out[k][i] = c
        return out

    def to_mest(day_map: dict) -> dict:
        # muni -> estado -> [por día]; solo municipios/estados con datos
        out: dict = {}
        for d, km in day_map.items():
            i = day_idx[d]
            for (mn, es), c in km.items():
                out.setdefault(mn, {}).setdefault(es, [0] * n)[i] = c
        return out

    ts_block = {"days": days, "lote": {}}
    for l in lotes:
        ts_block["lote"][l] = {
            "total": to_arr(daily_total[l]),
            "estado": to_arr_dim(daily_estado[l], estados),
            "prio": to_arr_dim(daily_prio[l], prioridades),
            "muni": to_arr_dim(daily_muni[l], municipios),
            "mest": to_mest(daily_mest[l]),
        }

    raw_block = None
    if include_raw and raw_rows:
        mi = {m: i for i, m in enumerate(municipios)}
        li = {l: i for i, l in enumerate(lotes)}
        pi = {p: i for i, p in enumerate(prioridades)}
        ei = {e: i for i, e in enumerate(estados)}
        rows = raw_rows[:max_raw] if max_raw else raw_rows
        enc = [[rid, tse, mi.get(mn, -1), li.get(lo, -1), adp, pi.get(pr, -1), ei.get(es, -1)]
               for (rid, tse, mn, lo, adp, pr, es) in rows]
        raw_block = {"count": len(enc), "total": len(raw_rows), "rows": enc}

    total = sum(by_lote.values())
    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "source_url": SOURCE_URL,
        "scraped_at": scraped_at,
        "snapshot_date": scraped_at[:10] if scraped_at else (days[-1] if days else ""),
        "total": total,
        "lotes": lotes,
        "estados": estados,
        "prioridades": prioridades,
        "municipios": municipios,
        "ts": ts_block,
        "raw": raw_block,
    }


def render_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return HTML_HEAD + "<script>window.DATA=" + payload + ";</script>\n" + HTML_BODY


def latest_csv(snap_dir: Path) -> Path:
    files = sorted(snap_dir.glob("planvive_*.csv"))
    if not files:
        sys.exit(f"No hay snapshots en {snap_dir} (ejecuta antes planvive.py).")
    return files[-1]


def main() -> None:
    module_dir = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Genera el informe HTML del Plan Vive.")
    p.add_argument("--snapshots-dir", type=Path, default=module_dir / "output" / "snapshots")
    p.add_argument("--csv", type=Path, default=None, help="CSV concreto (por defecto, el último snapshot).")
    p.add_argument("--out", type=Path, default=module_dir / "output" / "dashboard.html")
    p.add_argument("--no-raw", action="store_true", help="No incrustar la tabla en bruto (informe ligero).")
    p.add_argument("--max-raw", type=int, default=None, help="Limitar filas de la tabla en bruto.")
    args = p.parse_args()

    csv_path = args.csv or latest_csv(args.snapshots_dir)
    print(f"Generando informe desde {csv_path} …", file=sys.stderr)
    data = build_data(csv_path, include_raw=not args.no_raw, max_raw=args.max_raw)
    html = render_html(data)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")
    mb = args.out.stat().st_size / 1e6
    n_raw = data["raw"]["count"] if data["raw"] else 0
    print(f"OK -> {args.out}  ({mb:.1f} MB · {data['total']:,} solicitudes · "
          f"{len(data['ts']['days'])} días · {n_raw:,} filas en bruto)", file=sys.stderr)


# =============================================================================
#  Plantilla HTML (autocontenida: CSS + JS + datos incrustados, sin CDN)
# =============================================================================

HTML_HEAD = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Plan Vive · Informe de solicitudes</title>
<style>
:root{
  color-scheme: light;
  --plane:#f9f9f7; --surface:#fcfcfb; --surface-2:#f3f3ef;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,0.10);
  --good:#006300; --bad:#b23b3b;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4;
  --good-mark:#0ca30c;
  --prio0:#86b6ef; --prio1:#5598e7; --prio2:#256abf; --prio3:#0d366b;
  --seq:#2a78d6; --accent:#256abf;
}
@media (prefers-color-scheme: dark){ :root:where(:not([data-theme="light"])){
  color-scheme: dark;
  --plane:#0d0d0d; --surface:#1a1a19; --surface-2:#232320;
  --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
  --good:#0ca30c; --bad:#e66767;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181;
  --good-mark:#0ca30c;
  --prio0:#b7d3f6; --prio1:#6da7ec; --prio2:#3987e5; --prio3:#184f95;
  --seq:#3987e5; --accent:#6da7ec;
}}
:root[data-theme="dark"]{
  color-scheme: dark;
  --plane:#0d0d0d; --surface:#1a1a19; --surface-2:#232320;
  --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
  --good:#0ca30c; --bad:#e66767;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181;
  --good-mark:#0ca30c;
  --prio0:#b7d3f6; --prio1:#6da7ec; --prio2:#3987e5; --prio3:#184f95;
  --seq:#3987e5; --accent:#6da7ec;
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;font-size:14px;line-height:1.45;}
a{color:var(--accent)}
h1,h2,h3{margin:0;font-weight:600}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px 64px}
header{border-bottom:1px solid var(--border);background:var(--surface)}
.head-in{max-width:1180px;margin:0 auto;padding:18px 20px;display:flex;flex-wrap:wrap;gap:12px;align-items:baseline}
.head-in h1{font-size:19px}
.head-sub{color:var(--muted);font-size:12.5px}
.head-actions{margin-left:auto;display:flex;gap:8px;align-items:center}
.btn{font:inherit;font-size:12.5px;padding:6px 11px;border:1px solid var(--border);border-radius:8px;
  background:var(--surface);color:var(--ink);cursor:pointer}
.btn:hover{background:var(--surface-2)}
.tabs{display:flex;gap:4px;margin:18px 0 4px;border-bottom:1px solid var(--border)}
.tab{font:inherit;font-size:14px;padding:9px 14px;border:none;background:none;color:var(--ink-2);
  cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}
.tab[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
.filters{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;margin:16px 0 4px}
.field{display:flex;flex-direction:column;gap:4px}
.field label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
select,input[type=search],input[type=text]{font:inherit;font-size:13px;padding:6px 9px;border:1px solid var(--border);
  border-radius:8px;background:var(--surface);color:var(--ink);min-width:130px}
.grid{display:grid;gap:14px}
.kpis{grid-template-columns:repeat(auto-fit,minmax(170px,1fr));margin:14px 0}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px}
.kpi .label{font-size:12px;color:var(--ink-2)}
.kpi .value{font-size:30px;font-weight:600;margin-top:2px;letter-spacing:-.01em}
.kpi.hero .value{font-size:46px}
.kpi .delta{font-size:12px;margin-top:3px;color:var(--muted)}
.kpi .delta.up{color:var(--good)} .kpi .delta.down{color:var(--bad)}
.kpi .spark{margin-top:8px}
.charts{grid-template-columns:1fr 1fr;margin:14px 0}
.chart-card h3{font-size:14px} .chart-card .csub{font-size:12px;color:var(--muted);margin:2px 0 8px}
.full{grid-column:1 / -1}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin-top:8px;font-size:12px;color:var(--ink-2)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend i{width:11px;height:11px;border-radius:3px;display:inline-block}
.legend i.line{height:3px;width:16px;border-radius:2px}
svg{display:block;width:100%;height:auto;overflow:visible}
.axis-txt{fill:var(--muted);font-size:11px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--ink-2);font-weight:600;cursor:pointer;user-select:none;position:sticky;top:0;background:var(--surface)}
th.num,td.num{text-align:right;font-variant-numeric:tabular-nums}
.arrow{opacity:.3;font-size:10px} th[data-sorted] .arrow{opacity:1}
tbody tr:hover{background:var(--surface-2)}
.tablewrap{overflow:auto;max-height:620px;border:1px solid var(--border);border-radius:12px}
.barcell{position:relative}
.barcell .bar{position:absolute;left:0;top:0;bottom:0;background:color-mix(in srgb,var(--seq) 22%,transparent);z-index:0}
.barcell span{position:relative;z-index:1}
.pager{display:flex;gap:10px;align-items:center;margin-top:10px;font-size:13px;color:var(--ink-2)}
.meter{height:12px;border-radius:6px;background:color-mix(in srgb,var(--seq) 16%,transparent);overflow:hidden;margin-top:6px}
.meter > i{display:block;height:100%;background:var(--seq)}
.note{color:var(--muted);font-size:12px;margin-top:10px}
#tooltip{position:fixed;pointer-events:none;z-index:50;background:var(--surface);border:1px solid var(--border);
  border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.14);opacity:0;transition:opacity .08s;max-width:280px}
#tooltip .tt-title{color:var(--ink-2);font-size:11px;margin-bottom:4px}
#tooltip .tt-row{display:flex;align-items:center;gap:7px;justify-content:space-between}
#tooltip .tt-row b{font-variant-numeric:tabular-nums}
#tooltip .tt-key{display:inline-flex;align-items:center;gap:6px;color:var(--ink-2)}
#tooltip .tt-key i{width:14px;height:3px;border-radius:2px;display:inline-block}
@media(max-width:760px){ .charts{grid-template-columns:1fr} .kpi.hero .value{font-size:38px} }
</style>
</head>
<body>
"""

HTML_BODY = r"""
<header>
  <div class="head-in">
    <h1>Plan Vive · Listado de solicitudes</h1>
    <span class="head-sub" id="head-sub"></span>
    <div class="head-actions">
      <button class="btn" id="btn-download" title="Descargar una copia autónoma de este informe">⭳ Descargar informe</button>
      <button class="btn" id="btn-theme" title="Cambiar tema">◐</button>
    </div>
  </div>
</header>

<div class="wrap">
  <div class="tabs" role="tablist">
    <button class="tab" role="tab" data-tab="resumen" aria-selected="true">Resumen</button>
    <button class="tab" role="tab" data-tab="agregacion" aria-selected="false">Agregación</button>
    <button class="tab" role="tab" data-tab="bruto" aria-selected="false">Datos en bruto</button>
  </div>

  <div class="filters" id="filters-global">
    <div class="field">
      <label for="f-lote">Lote</label>
      <select id="f-lote"><option value="all">Todos</option></select>
    </div>
    <div class="field">
      <label for="f-range">Periodo (fecha de solicitud)</label>
      <select id="f-range">
        <option value="all">Todo el histórico</option>
        <option value="365">Último año</option>
        <option value="90">Últimos 90 días</option>
        <option value="30">Últimos 30 días</option>
      </select>
    </div>
    <div class="field" id="gran-field">
      <label for="f-gran">Agrupar por</label>
      <select id="f-gran">
        <option value="dia">Día</option>
        <option value="semana">Semana</option>
        <option value="mes" selected>Mes</option>
      </select>
    </div>
  </div>

  <section id="tab-resumen"></section>
  <section id="tab-agregacion" hidden></section>
  <section id="tab-bruto" hidden></section>
</div>

<div id="tooltip" role="status" aria-live="polite"></div>

<script>
"use strict";
const DATA = window.DATA;
const TS = DATA.ts;
const DAYS = TS.days;
const NDAYS = DAYS.length;
const CI = {id:0, ts:1, muni:2, lote:3, adapted:4, prio:5, estado:6};
const nf = new Intl.NumberFormat('es-ES');
const fmt = n => nf.format(Math.round(n));
const pct = (a,b) => b? (100*a/b) : 0;
const fmtPct = v => v.toFixed(1).replace('.',',')+'%';
function fmtCompact(n){ const a=Math.abs(n);
  if(a>=1e6) return (n/1e6).toFixed(1).replace('.',',')+'M';
  if(a>=1000) return (n/1000).toFixed(1).replace('.',',')+'K'; return fmt(n); }
function fmtDayLong(d){ const dt=new Date(d+'T00:00:00');
  return isNaN(dt)? d : dt.toLocaleDateString('es-ES',{day:'2-digit',month:'short',year:'numeric'}); }
function fmtDateTime(s){ const dt=new Date(s); return isNaN(dt)? s : dt.toLocaleString('es-ES'); }

const ESTADO_COLORS = {}; DATA.estados.forEach((e,i)=> ESTADO_COLORS[e]='var(--s'+(Math.min(i,4)+1)+')');
const LOTE_COLORS = {}; DATA.lotes.forEach((l,i)=> LOTE_COLORS[l]='var(--s'+((i%2)+1)+')');
const PRIO_COLORS = {}; DATA.prioridades.forEach((p,i)=> PRIO_COLORS[p]='var(--prio'+Math.min(i,3)+')');

const S = { lote:'all', range:'all', gran:'mes', tab:'resumen', muniMode:'abs',
  rawSort:{key:CI.ts, dir:-1}, rawPage:1, pageSize:50, muniSort:{key:'total', dir:-1} };

/* ---------------- series diarias combinadas por lote seleccionado --------------- */
const selLotes = () => S.lote==='all' ? DATA.lotes : [S.lote];
function dailyOf(dim, key){ // dim: 'total'|'adapted'|'estado'|'prio'|'muni'
  const out=new Array(NDAYS).fill(0);
  for(const l of selLotes()){ const L=TS.lote[l]; if(!L) continue;
    const arr = (dim==='total'||dim==='adapted') ? L[dim] : (L[dim] && L[dim][key]);
    if(!arr) continue; for(let i=0;i<NDAYS;i++) out[i]+=arr[i]||0; }
  return out; }
function rangeBounds(){ if(S.range==='all'||NDAYS===0) return [0,NDAYS];
  const last=DAYS[NDAYS-1]; const cut=new Date(last+'T00:00:00'); cut.setDate(cut.getDate()-(+S.range));
  const cutS=cut.toISOString().slice(0,10); let from=0; while(from<NDAYS && DAYS[from]<cutS) from++;
  return [from,NDAYS]; }
function sumR(arr){ const [a,b]=rangeBounds(); let s=0; for(let i=a;i<b;i++) s+=arr[i]||0; return s; }
function totalR(){ return sumR(dailyOf('total')); }

/* ---------------------------- bucketing temporal ---------------------------- */
function buckets(){
  const [from,to]=rangeBounds(); const groups=[]; let curKey=null,cur=null;
  for(let i=from;i<to;i++){ const d=DAYS[i]; let key;
    if(S.gran==='dia') key=d;
    else if(S.gran==='semana'){ const dt=new Date(d+'T00:00:00'); const dow=(dt.getDay()+6)%7;
      dt.setDate(dt.getDate()-dow); key=dt.toISOString().slice(0,10); }
    else key=d.slice(0,7);
    if(key!==curKey){ curKey=key; cur={key,idx:[]}; groups.push(cur); } cur.idx.push(i); }
  return groups; }
function bucketSum(daily, g){ let s=0; for(const i of g.idx) s+=daily[i]||0; return s; }
function bucketLabel(g){ const d0=DAYS[g.idx[0]]; const dt=new Date(d0+'T00:00:00');
  if(S.gran==='mes') return {label:dt.toLocaleDateString('es-ES',{month:'short',year:'2-digit'}),
    full:dt.toLocaleDateString('es-ES',{month:'long',year:'numeric'})};
  if(S.gran==='semana') return {label:dt.toLocaleDateString('es-ES',{day:'2-digit',month:'short'}),
    full:'Semana del '+dt.toLocaleDateString('es-ES',{day:'2-digit',month:'short',year:'numeric'})};
  return {label:dt.toLocaleDateString('es-ES',{day:'2-digit',month:'short'}),
    full:dt.toLocaleDateString('es-ES',{day:'2-digit',month:'short',year:'numeric'})}; }

/* ------------------------------ tooltip ------------------------------ */
const TT = document.getElementById('tooltip');
function showTT(node, x, y){ TT.innerHTML=''; TT.appendChild(node); TT.style.opacity='1';
  const r=TT.getBoundingClientRect(); let nx=x+14, ny=y+14;
  if(nx+r.width>window.innerWidth-8) nx=x-r.width-14; if(ny+r.height>window.innerHeight-8) ny=y-r.height-14;
  TT.style.left=nx+'px'; TT.style.top=ny+'px'; }
function hideTT(){ TT.style.opacity='0'; }
function ttNode(title, rows){ const f=document.createDocumentFragment();
  const t=document.createElement('div'); t.className='tt-title'; t.textContent=title; f.appendChild(t);
  for(const r of rows){ const d=document.createElement('div'); d.className='tt-row';
    const k=document.createElement('span'); k.className='tt-key';
    if(r.color){ const i=document.createElement('i'); i.style.background=r.color; k.appendChild(i);}
    k.appendChild(document.createTextNode(r.name)); const b=document.createElement('b'); b.textContent=r.val;
    d.appendChild(k); d.appendChild(b); f.appendChild(d);} return f; }

/* ------------------------------ SVG utils ------------------------------ */
const NS='http://www.w3.org/2000/svg';
function el(tag,attrs){ const e=document.createElementNS(NS,tag); for(const k in attrs) e.setAttribute(k,attrs[k]); return e; }
function niceMax(v){ if(v<=0) return 1; const p=Math.pow(10,Math.floor(Math.log10(v)));
  const n=v/p; const m=n<=1?1:n<=2?2:n<=5?5:10; return m*p; }

function lineChart(host, series, xs, opts){
  opts=opts||{}; const W=host.clientWidth||680, H=opts.h||230;
  const mL=54,mR=16,mT=12,mB=26, pw=W-mL-mR, ph=H-mT-mB, n=xs.length;
  let maxY=0; for(const s of series) for(const v of s.vals) if(v>maxY) maxY=v; maxY=niceMax(maxY||1);
  const X=i=> n<=1? mL+pw/2 : mL + pw*i/(n-1); const Y=v=> mT + ph*(1 - v/maxY);
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img'});
  for(let t=0;t<=4;t++){ const yv=maxY*t/4, y=Y(yv);
    svg.appendChild(el('line',{x1:mL,y1:y,x2:W-mR,y2:y,stroke:'var(--grid)','stroke-width':1}));
    const tx=el('text',{x:mL-8,y:y+4,'text-anchor':'end',class:'axis-txt'}); tx.textContent=fmtCompact(yv); svg.appendChild(tx); }
  const step=Math.max(1,Math.ceil(n/7));
  for(let i=0;i<n;i++){ if(i%step!==0 && i!==n-1) continue;
    const tx=el('text',{x:X(i),y:H-8,'text-anchor':'middle',class:'axis-txt'}); tx.textContent=xs[i].label; svg.appendChild(tx); }
  for(const s of series){ if(n===1){ svg.appendChild(el('circle',{cx:X(0),cy:Y(s.vals[0]),r:4,fill:s.color,stroke:'var(--surface)','stroke-width':2})); }
    else{ let d=''; s.vals.forEach((v,i)=> d+=(i?'L':'M')+X(i)+' '+Y(v)+' ');
      svg.appendChild(el('path',{d,fill:'none',stroke:s.color,'stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'}));
      svg.appendChild(el('circle',{cx:X(n-1),cy:Y(s.vals[n-1]),r:4,fill:s.color,stroke:'var(--surface)','stroke-width':2})); } }
  const cross=el('line',{x1:0,y1:mT,x2:0,y2:mT+ph,stroke:'var(--axis)','stroke-width':1,opacity:0}); svg.appendChild(cross);
  const hit=el('rect',{x:mL,y:mT,width:pw,height:ph,fill:'transparent'}); svg.appendChild(hit);
  hit.addEventListener('pointermove',ev=>{ const rect=svg.getBoundingClientRect(); const px=(ev.clientX-rect.left)*(W/rect.width);
    let i=n<=1?0:Math.round((px-mL)/pw*(n-1)); i=Math.max(0,Math.min(n-1,i));
    cross.setAttribute('x1',X(i)); cross.setAttribute('x2',X(i)); cross.setAttribute('opacity',1);
    showTT(ttNode(xs[i].full||xs[i].label, series.map(s=>({name:s.name,color:s.color,val:fmt(s.vals[i])}))), ev.clientX, ev.clientY); });
  hit.addEventListener('pointerleave',()=>{cross.setAttribute('opacity',0);hideTT();});
  host.innerHTML=''; host.appendChild(svg);
}

function stackedColumns(host, cats, xs, opts){
  opts=opts||{}; const W=host.clientWidth||680, H=opts.h||250;
  const mL=54,mR=16,mT=12,mB=26, pw=W-mL-mR, ph=H-mT-mB, n=xs.length;
  const totals=xs.map((_,i)=> cats.reduce((a,c)=>a+c.vals[i],0)); const maxY=niceMax(Math.max(1,...totals));
  const bw=Math.min(38, pw/Math.max(1,n)*0.66), gap=2;
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img'});
  for(let t=0;t<=4;t++){ const yv=maxY*t/4, y=mT+ph*(1-t/4);
    svg.appendChild(el('line',{x1:mL,y1:y,x2:W-mR,y2:y,stroke:'var(--grid)','stroke-width':1}));
    const tx=el('text',{x:mL-8,y:y+4,'text-anchor':'end',class:'axis-txt'}); tx.textContent=fmtCompact(yv); svg.appendChild(tx); }
  const cx=i=> n<=1? mL+pw/2 : mL + pw*(i+0.5)/n; const step=Math.max(1,Math.ceil(n/7));
  for(let i=0;i<n;i++){ const x=cx(i)-bw/2; let acc=0;
    for(const c of cats){ const v=c.vals[i]; if(v<=0) continue; const h=ph*v/maxY; const y=mT+ph*(1-(acc+v)/maxY);
      svg.appendChild(el('rect',{x,y:y+(acc>0?gap/2:0),width:bw,height:Math.max(0,h-(acc>0?gap/2:0)),fill:c.color})); acc+=v; }
    const hit=el('rect',{x:x-1,y:mT,width:bw+2,height:ph,fill:'transparent'});
    hit.addEventListener('pointermove',ev=>{ const rows=cats.filter(c=>c.vals[i]>0).map(c=>({name:c.name,color:c.color,val:fmt(c.vals[i])}));
      rows.push({name:'Total',color:null,val:fmt(totals[i])}); showTT(ttNode(xs[i].full||xs[i].label,rows),ev.clientX,ev.clientY); });
    hit.addEventListener('pointerleave',hideTT); svg.appendChild(hit);
    if(i%step===0 || i===n-1){ const tx=el('text',{x:cx(i),y:H-8,'text-anchor':'middle',class:'axis-txt'}); tx.textContent=xs[i].label; svg.appendChild(tx); } }
  host.innerHTML=''; host.appendChild(svg);
}

function hbars(host, items, opts){
  opts=opts||{}; const rowH=30, W=host.clientWidth||520, H=Math.max(1,items.length)*rowH+8;
  const mL=opts.labelW||140, mR=54, pw=W-mL-mR; const maxV=Math.max(1,...items.map(d=>d.val));
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img'});
  items.forEach((d,i)=>{ const y=i*rowH+4, bw=pw*d.val/maxV, barH=Math.min(20,rowH-10);
    const lab=el('text',{x:mL-10,y:y+rowH/2,'text-anchor':'end','dominant-baseline':'middle',class:'axis-txt',fill:'var(--ink-2)'}); lab.textContent=d.name; svg.appendChild(lab);
    svg.appendChild(el('rect',{x:mL,y:y+(rowH-barH)/2,width:Math.max(2,bw),height:barH,rx:4,fill:d.color||'var(--seq)'}));
    const val=el('text',{x:mL+Math.max(2,bw)+7,y:y+rowH/2,'dominant-baseline':'middle',class:'axis-txt',fill:'var(--ink-2)'}); val.textContent=fmt(d.val); svg.appendChild(val);
    const hit=el('rect',{x:0,y,width:W,height:rowH,fill:'transparent'});
    hit.addEventListener('pointermove',ev=> showTT(ttNode(d.name,[{name:opts.unit||'Solicitudes',color:d.color,val:fmt(d.val)}]),ev.clientX,ev.clientY));
    hit.addEventListener('pointerleave',hideTT); svg.appendChild(hit); });
  host.innerHTML=''; host.appendChild(svg);
}

/* barras horizontales apiladas: una fila por elemento, segmentos por categoría.
   rows: [{name, total, vals:[por cat]}]; cats:[{name,color}]; opts.mode 'abs'|'pct' */
function stackedHBars(host, rows, cats, opts){
  opts=opts||{}; const rowH=34, W=host.clientWidth||680, H=Math.max(1,rows.length)*rowH+8;
  const mL=opts.labelW||160, mR=64, pw=W-mL-mR, gap=2, barH=Math.min(22,rowH-10);
  const pctMode=opts.mode==='pct'; const maxTotal=Math.max(1,...rows.map(r=>r.total));
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img'});
  rows.forEach((r,ri)=>{ const y=ri*rowH+4;
    const lab=el('text',{x:mL-10,y:y+rowH/2,'text-anchor':'end','dominant-baseline':'middle',class:'axis-txt',fill:'var(--ink-2)'}); lab.textContent=r.name; svg.appendChild(lab);
    const rowW = pctMode? pw : pw*r.total/maxTotal; let x=mL; const denom=r.total||1;
    cats.forEach((c,ci)=>{ const v=r.vals[ci]||0; if(v<=0) return; const segW=rowW*v/denom;
      svg.appendChild(el('rect',{x,y:y+(rowH-barH)/2,width:Math.max(0,segW-gap),height:barH,fill:c.color})); x+=segW; });
    const val=el('text',{x:mL+rowW+7,y:y+rowH/2,'dominant-baseline':'middle',class:'axis-txt',fill:'var(--ink-2)'}); val.textContent= pctMode?'100%':fmt(r.total); svg.appendChild(val);
    const hit=el('rect',{x:0,y,width:W,height:rowH,fill:'transparent'});
    hit.addEventListener('pointermove',ev=>{ const tr=cats.map((c,ci)=>({name:c.name,color:c.color,val: pctMode? fmtPct(pct(r.vals[ci]||0,r.total)) : fmt(r.vals[ci]||0)})).filter((_,ci)=>(r.vals[ci]||0)>0);
      tr.push({name:'Total',color:null,val:fmt(r.total)}); showTT(ttNode(r.name,tr),ev.clientX,ev.clientY); });
    hit.addEventListener('pointerleave',hideTT); svg.appendChild(hit); });
  host.innerHTML=''; host.appendChild(svg);
}

function sparkline(vals, color){ const W=150,H=34,m=3; const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,width:W,height:H,class:'spark'});
  const mn=Math.min(...vals),mx=Math.max(...vals),sp=(mx-mn)||1,n=vals.length;
  const X=i=> n<=1? W/2 : m+(W-2*m)*i/(n-1); const Y=v=> H-m-(H-2*m)*(v-mn)/sp;
  if(n<=1){ svg.appendChild(el('circle',{cx:W/2,cy:H/2,r:3,fill:color})); return svg; }
  let d=''; vals.forEach((v,i)=> d+=(i?'L':'M')+X(i).toFixed(1)+' '+Y(v).toFixed(1)+' ');
  svg.appendChild(el('path',{d,fill:'none',stroke:color,'stroke-width':2,'stroke-linecap':'round','stroke-linejoin':'round'}));
  svg.appendChild(el('circle',{cx:X(n-1),cy:Y(vals[n-1]),r:3,fill:color})); return svg; }

/* ------------------------------- KPIs -------------------------------- */
function kpiTile(label, value, opts){ opts=opts||{}; const c=document.createElement('div'); c.className='card kpi'+(opts.hero?' hero':'');
  const l=document.createElement('div'); l.className='label'; l.textContent=label;
  const v=document.createElement('div'); v.className='value'; v.textContent=value; c.appendChild(l); c.appendChild(v);
  if(opts.delta!==undefined && opts.delta!==null){ const d=document.createElement('div'); const up=opts.delta>0,dn=opts.delta<0;
    d.className='delta'+(up?' up':dn?' down':''); d.textContent=(up?'▲ +':dn?'▼ −':'▬ ')+(opts.deltaFmt||fmt)(Math.abs(opts.delta))+(opts.deltaNote||''); c.appendChild(d); }
  else if(opts.note){ const d=document.createElement('div'); d.className='delta'; d.textContent=opts.note; c.appendChild(d); }
  if(opts.spark) c.appendChild(opts.spark); return c; }

function legendItems(items){ const w=document.createElement('div'); w.className='legend';
  for(const it of items){ const s=document.createElement('span'); const i=document.createElement('i');
    if(it.line) i.className='line'; i.style.background=it.color; s.appendChild(i); s.appendChild(document.createTextNode(it.name)); w.appendChild(s);} return w; }
function chartCard(title, sub, draw, legend, full){ const c=document.createElement('div'); c.className='card chart-card'+(full?' full':'');
  const h=document.createElement('h3'); h.textContent=title; c.appendChild(h);
  if(sub){ const s=document.createElement('div'); s.className='csub'; s.textContent=sub; c.appendChild(s); }
  const host=document.createElement('div'); c.appendChild(host); if(legend) c.appendChild(legend);
  requestAnimationFrame(()=>draw(host)); return c; }

function renderResumen(){
  const host=document.getElementById('tab-resumen'); host.innerHTML='';
  const gs=buckets(); const xs=gs.map(g=>bucketLabel(g));
  const totR=totalR();
  const contr=sumR(dailyOf('estado','Contrato firmado'));
  // nuevas últimos 30 vs 30 previos (por fecha de solicitud, sobre lote sel.)
  const tDaily=dailyOf('total');
  const last30=windowSum(tDaily,0,30), prev30=windowSum(tDaily,30,60);

  const kg=document.createElement('div'); kg.className='grid kpis';
  kg.appendChild(kpiTile('Solicitudes'+(S.range==='all'?' totales':' (periodo)'), fmt(totR),
    {hero:true, spark:sparkline(cumBuckets(gs,tDaily),'var(--accent)'), note: S.range==='all'?'Histórico completo':'En el periodo'}));
  DATA.lotes.forEach(l=>{ if(S.lote!=='all' && S.lote!==l) return; const arr=TS.lote[l]?TS.lote[l].total:[];
    kg.appendChild(kpiTile(l, fmt(sumR(arr)), {spark:sparkline(cumBuckets(gs,arr),LOTE_COLORS[l])})); });
  kg.appendChild(kpiTile('Contratos firmados', fmt(contr), {note: fmtPct(pct(contr,totR))+' del total', spark:sparkline(gs.map(g=>bucketSum(dailyOf('estado','Contrato firmado'),g)),'var(--good-mark)')}));
  kg.appendChild(kpiTile('Nuevas · últimos 30 días', fmt(last30), {delta: prev30? last30-prev30 : null, deltaNote:' vs. 30 días previos', note: prev30? null : 'por fecha de solicitud'}));
  host.appendChild(kg);

  const cg=document.createElement('div'); cg.className='grid charts';
  cg.appendChild(chartCard('Solicitudes acumuladas','Total acumulado por fecha de solicitud',
    (h)=>{ const series=selLotes().map(l=>({name:l,color:LOTE_COLORS[l],vals:cumBuckets(gs,TS.lote[l].total)})); lineChart(h,series,xs,{h:230}); },
    legendItems(selLotes().map(l=>({name:l,color:LOTE_COLORS[l],line:true})))));
  cg.appendChild(chartCard('Nuevas solicitudes por periodo','Altas en cada '+granWord(),
    (h)=>{ const cats=selLotes().map(l=>({name:l,color:LOTE_COLORS[l],vals:gs.map(g=>bucketSum(loteDaily(l,'total'),g))})); stackedColumns(h,cats,xs,{h:230}); },
    legendItems(selLotes().map(l=>({name:l,color:LOTE_COLORS[l]})))));
  cg.appendChild(chartCard('Estado actual por periodo de solicitud','Cohorte: de lo presentado en cada '+granWord()+', su estado HOY',
    (h)=>{ const cats=DATA.estados.map(e=>({name:e,color:ESTADO_COLORS[e],vals:gs.map(g=>bucketSum(dailyOf('estado',e),g))})); stackedColumns(h,cats,xs,{h:250}); },
    legendItems(DATA.estados.map(e=>({name:e,color:ESTADO_COLORS[e]}))), true));
  host.appendChild(cg);

  if(gs.length<2){ const n=document.createElement('p'); n.className='note'; n.textContent='Pocos periodos en el rango seleccionado; amplía el periodo o cambia la granularidad.'; host.appendChild(n); }
}
function granWord(){ return S.gran==='dia'?'día':S.gran==='semana'?'semana':'mes'; }
function loteDaily(l,dim,key){ const L=TS.lote[l]; if(!L) return new Array(NDAYS).fill(0);
  return (dim==='total'||dim==='adapted')? (L[dim]||[]) : ((L[dim]&&L[dim][key])||[]); }
function windowSum(daily, backFrom, backTo){ // suma días [last-backTo, last-backFrom)
  let s=0; for(let i=Math.max(0,NDAYS-backTo); i<NDAYS-backFrom; i++) s+=daily[i]||0; return s; }
function cumBuckets(gs, daily){ // acumulado real hasta el final de cada bucket
  const cum=new Array(NDAYS); let run=0; for(let i=0;i<NDAYS;i++){ run+=daily[i]||0; cum[i]=run; }
  return gs.map(g=> cum[g.idx[g.idx.length-1]]); }

/* --------------------------- Agregación tab (municipio-céntrica) --------------------------- */
function mestDaily(muni, estado){ const out=new Array(NDAYS).fill(0);
  for(const l of selLotes()){ const L=TS.lote[l]; const arr=L && L.mest && L.mest[muni] && L.mest[muni][estado];
    if(arr) for(let i=0;i<NDAYS;i++) out[i]+=arr[i]||0; } return out; }
function mestSum(muni, estado){ const arr=mestDaily(muni,estado); const [a,b]=rangeBounds(); let s=0; for(let i=a;i<b;i++) s+=arr[i]||0; return s; }
function sumRLote(l,dim,key){ const arr=loteDaily(l,dim,key); const [a,b]=rangeBounds(); let s=0; for(let i=a;i<b;i++) s+=arr[i]||0; return s; }
function muniTotalR(m){ let s=0; for(const l of selLotes()) s+=sumRLote(l,'muni',m); return s; }
function municipioRows(){ // [{name, total, est:{e:n}, vals:[por estado]}], solo con datos
  return DATA.municipios.map(m=>{ const est={}; let tot=0; const vals=DATA.estados.map(e=>{ const v=mestSum(m,e); est[e]=v; tot+=v; return v; });
    return {name:m, total:tot, est, vals}; }).filter(r=>r.total>0); }

function renderAgregacion(){
  const host=document.getElementById('tab-agregacion'); host.innerHTML='';
  const periodo = S.range==='all'?'Histórico completo':'Periodo seleccionado';
  const mrows=municipioRows();

  // 1) distribuciones globales compactas (estado y prioridad)
  const cg=document.createElement('div'); cg.className='grid charts';
  cg.appendChild(chartCard('Distribución por estado', periodo,
    (h)=>{ const items=DATA.estados.map(e=>({name:e,val:sumR(dailyOf('estado',e)),color:ESTADO_COLORS[e]})).filter(d=>d.val>0).sort((a,b)=>b.val-a.val); hbars(h,items,{labelW:130}); }));
  cg.appendChild(chartCard('Distribución por prioridad','Nivel de prioridad (P0 → P3)',
    (h)=>{ const items=DATA.prioridades.map(p=>({name:p,val:sumR(dailyOf('prio',p)),color:PRIO_COLORS[p]})).filter(d=>d.val>0); hbars(h,items,{labelW:60}); }));
  host.appendChild(cg);

  // 2) Estado por municipio (barras apiladas) — protagonista, con modo Absoluto/%
  const bcard=document.createElement('div'); bcard.className='card full';
  const bhead=document.createElement('div'); bhead.style.cssText='display:flex;align-items:baseline;gap:12px;flex-wrap:wrap';
  bhead.appendChild(Object.assign(document.createElement('h3'),{textContent:'Estado de las solicitudes por municipio'}));
  const toggle=document.createElement('div'); toggle.style.marginLeft='auto'; toggle.style.display='flex'; toggle.style.gap='4px';
  [['abs','Nº'],['pct','%']].forEach(([mode,lab])=>{ const b=document.createElement('button'); b.className='btn'; b.textContent=lab;
    if(S.muniMode===mode){ b.style.borderColor='var(--accent)'; b.style.color='var(--accent)'; }
    b.onclick=()=>{ S.muniMode=mode; renderAgregacion(); }; toggle.appendChild(b); });
  bhead.appendChild(toggle); bcard.appendChild(bhead);
  bcard.appendChild(Object.assign(document.createElement('div'),{className:'csub',
    textContent:(S.muniMode==='pct'?'Composición (%) de estados dentro de cada municipio · ':'Solicitudes por estado en cada municipio · ')+periodo}));
  const bhost=document.createElement('div'); bcard.appendChild(bhost);
  bcard.appendChild(legendItems(DATA.estados.map(e=>({name:e,color:ESTADO_COLORS[e]}))));
  const cats=DATA.estados.map(e=>({name:e,color:ESTADO_COLORS[e]}));
  const sorted=mrows.slice().sort((a,b)=>b.total-a.total);
  requestAnimationFrame(()=> stackedHBars(bhost, sorted, cats, {mode:S.muniMode, labelW:170}));
  host.appendChild(bcard);

  // 3) Tabla municipio × estado (ordenable por cualquier columna)
  const tcard=document.createElement('div'); tcard.className='card full';
  tcard.appendChild(Object.assign(document.createElement('h3'),{textContent:'Municipio × estado'}));
  tcard.appendChild(Object.assign(document.createElement('div'),{className:'csub',textContent:'Ordena pulsando en las cabeceras (p. ej. por «Contrato firmado»)'}));
  tcard.appendChild(muniEstadoTable(mrows)); host.appendChild(tcard);

  // 4) Municipio en detalle — evolución del estado (cohorte) por periodo
  host.appendChild(muniDetalle(mrows));
}

function muniEstadoTable(mrows){
  const wrap=document.createElement('div'); wrap.className='tablewrap';
  const key=S.muniSort.key, dir=S.muniSort.dir;
  const rows=mrows.slice().sort((a,b)=>{ if(key==='name') return a.name.localeCompare(b.name)*dir;
    const av = key==='total'? a.total : (a.est[key]||0); const bv = key==='total'? b.total : (b.est[key]||0); return (av-bv)*dir; });
  const maxTotal=Math.max(1,...rows.map(r=>r.total));
  const t=document.createElement('table'); const thead=document.createElement('thead'); const hr=document.createElement('tr');
  const cols=[['name','Municipio',false],...DATA.estados.map(e=>[e,e,true]),['total','Total',true]];
  cols.forEach(([k,lab,num])=>{ const th=document.createElement('th'); if(num)th.className='num';
    th.appendChild(document.createTextNode(lab+' ')); const ar=document.createElement('span'); ar.className='arrow'; ar.textContent=key===k?(dir<0?'▼':'▲'):'↕'; th.appendChild(ar);
    if(key===k) th.setAttribute('data-sorted','');
    th.onclick=()=>{ if(S.muniSort.key===k) S.muniSort.dir*=-1; else S.muniSort={key:k,dir:k==='name'?1:-1}; renderAgregacion(); }; hr.appendChild(th); });
  thead.appendChild(hr); t.appendChild(thead); const tb=document.createElement('tbody');
  rows.forEach(r=>{ const tr=document.createElement('tr');
    const td0=document.createElement('td'); td0.textContent=r.name; tr.appendChild(td0);
    DATA.estados.forEach(e=>{ const td=document.createElement('td'); td.className='num'; td.textContent=fmt(r.est[e]||0);
      td.title=fmtPct(pct(r.est[e]||0,r.total))+' del municipio'; tr.appendChild(td); });
    const tt=document.createElement('td'); tt.className='num barcell'; tt.style.fontWeight='600';
    const bar=document.createElement('div'); bar.className='bar'; bar.style.width=(100*r.total/maxTotal)+'%'; tt.appendChild(bar);
    const sp=document.createElement('span'); sp.textContent=fmt(r.total); tt.appendChild(sp); tr.appendChild(tt); tb.appendChild(tr); });
  // fila total
  const tr=document.createElement('tr'); tr.style.borderTop='2px solid var(--border)';
  const td0=document.createElement('td'); td0.style.fontWeight='600'; td0.textContent='TOTAL'; tr.appendChild(td0);
  DATA.estados.forEach(e=>{ const v=rows.reduce((a,r)=>a+(r.est[e]||0),0); const td=document.createElement('td'); td.className='num'; td.style.fontWeight='600'; td.textContent=fmt(v); tr.appendChild(td); });
  const gtot=rows.reduce((a,r)=>a+r.total,0); const tt=document.createElement('td'); tt.className='num'; tt.style.fontWeight='600'; tt.textContent=fmt(gtot); tr.appendChild(tt); tb.appendChild(tr);
  t.appendChild(tb); wrap.appendChild(t); return wrap;
}

function muniDetalle(mrows){
  const card=document.createElement('div'); card.className='card full';
  const head=document.createElement('div'); head.style.cssText='display:flex;align-items:baseline;gap:12px;flex-wrap:wrap';
  head.appendChild(Object.assign(document.createElement('h3'),{textContent:'Municipio en detalle'}));
  const sel=document.createElement('select'); sel.style.marginLeft='auto';
  const ordered=mrows.slice().sort((a,b)=>b.total-a.total);
  ordered.forEach(r=>{ const o=document.createElement('option'); o.value=r.name; o.textContent=r.name+' ('+fmt(r.total)+')'; sel.appendChild(o); });
  head.appendChild(sel); card.appendChild(head);
  const sub=document.createElement('div'); sub.className='csub'; card.appendChild(sub);
  const kwrap=document.createElement('div'); kwrap.style.cssText='display:flex;gap:10px;flex-wrap:wrap;margin:8px 0'; card.appendChild(kwrap);
  const chost=document.createElement('div'); card.appendChild(chost);
  card.appendChild(legendItems(DATA.estados.map(e=>({name:e,color:ESTADO_COLORS[e]}))));
  function draw(){ const m=sel.value; const gs=buckets(); const xs=gs.map(g=>bucketLabel(g));
    sub.textContent='Estado actual de las solicitudes de '+m+' según su '+granWord()+' de solicitud (cohorte)';
    const cats=DATA.estados.map(e=>({name:e,color:ESTADO_COLORS[e],vals:gs.map(g=>bucketSum(mestDaily(m,e),g))}));
    kwrap.innerHTML=''; const row=ordered.find(r=>r.name===m)||{est:{},total:0};
    const chip=(lab,val,col)=>{ const d=document.createElement('div'); d.style.cssText='padding:6px 10px;border:1px solid var(--border);border-radius:8px;font-size:12px';
      const b=document.createElement('b'); b.style.fontSize='15px'; if(col)b.style.color=col; b.textContent=val; d.appendChild(b); d.appendChild(document.createTextNode(' '+lab)); return d; };
    kwrap.appendChild(chip('total', fmt(row.total)));
    DATA.estados.forEach(e=>{ if((row.est[e]||0)>0) kwrap.appendChild(chip(e+' ('+fmtPct(pct(row.est[e],row.total))+')', fmt(row.est[e]))); });
    stackedColumns(chost, cats, xs, {h:240}); }
  sel.onchange=draw; requestAnimationFrame(draw); return card;
}

/* ----------------------------- Datos en bruto ----------------------------- */
function renderBruto(){
  const host=document.getElementById('tab-bruto'); host.innerHTML='';
  if(!DATA.raw){ const p=document.createElement('p'); p.className='note'; p.textContent='Este informe se generó sin la tabla en bruto (--no-raw).'; host.appendChild(p); return; }
  const info=document.createElement('p'); info.className='csub';
  info.textContent='Instantánea del '+fmtDayLong(DATA.snapshot_date)+' · '+fmt(DATA.raw.total)+' solicitudes'+(DATA.raw.count<DATA.raw.total?' (mostrando '+fmt(DATA.raw.count)+')':'')+' · el periodo y el lote de arriba también filtran esta tabla'; host.appendChild(info);
  const bar=document.createElement('div'); bar.className='filters';
  const fSearch=fieldInput('Buscar código','raw-search');
  const fMuni=fieldSelect('Municipio',['— Todos —',...DATA.municipios]);
  const fEstado=fieldSelect('Estado',['— Todos —',...DATA.estados]);
  const fPrio=fieldSelect('Prioridad',['— Todas —',...DATA.prioridades]);
  [fSearch,fMuni,fEstado,fPrio].forEach(f=>bar.appendChild(f.field));
  const exp=document.createElement('button'); exp.className='btn'; exp.textContent='⭳ Exportar CSV filtrado'; exp.style.marginLeft='auto';
  const ew=document.createElement('div'); ew.className='field'; ew.appendChild(document.createElement('label')); ew.appendChild(exp); bar.appendChild(ew); host.appendChild(bar);
  const wrap=document.createElement('div'); wrap.className='tablewrap'; host.appendChild(wrap);
  const pager=document.createElement('div'); pager.className='pager'; host.appendChild(pager);
  const cols=[['Código',CI.id,false],['Fecha y hora',CI.ts,true],['Municipio',CI.muni,false],['Lote',CI.lote,false],['Prioridad',CI.prio,false],['Estado',CI.estado,false]];
  function cutoffEpoch(){ if(S.range==='all'||NDAYS===0) return -Infinity; const last=DAYS[NDAYS-1]; const cut=new Date(last+'T00:00:00'); cut.setDate(cut.getDate()-(+S.range)); return cut.getTime()/1000; }
  function filtered(){ const q=fSearch.input.value.trim().toLowerCase();
    const mu=fMuni.input.selectedIndex-1, es=fEstado.input.selectedIndex-1, pr=fPrio.input.selectedIndex-1;
    const lf=S.lote==='all'?-1:DATA.lotes.indexOf(S.lote); const ce=cutoffEpoch(); let out=[];
    for(const r of DATA.raw.rows){ if(lf>=0 && r[CI.lote]!==lf) continue; if(r[CI.ts]<ce) continue;
      if(mu>=0 && r[CI.muni]!==mu) continue; if(es>=0 && r[CI.estado]!==es) continue; if(pr>=0 && r[CI.prio]!==pr) continue;
      if(q && !r[CI.id].toLowerCase().includes(q)) continue; out.push(r); }
    const k=S.rawSort.key, d=S.rawSort.dir;
    out.sort((a,b)=>{ let av=a[k],bv=b[k]; if(k===CI.muni){av=DATA.municipios[a[k]];bv=DATA.municipios[b[k]];} if(k===CI.estado){av=DATA.estados[a[k]];bv=DATA.estados[b[k]];}
      if(k===CI.ts) return (av-bv)*d; return String(av).localeCompare(String(bv))*d; }); return out; }
  function draw(){ const rows=filtered(); const total=rows.length; const pages=Math.max(1,Math.ceil(total/S.pageSize));
    if(S.rawPage>pages) S.rawPage=pages; const start=(S.rawPage-1)*S.pageSize, slice=rows.slice(start,start+S.pageSize);
    const t=document.createElement('table'); const thead=document.createElement('thead'); const hr=document.createElement('tr');
    cols.forEach(([lab,k,num])=>{ const th=document.createElement('th'); if(num)th.className='num'; th.appendChild(document.createTextNode(lab+' '));
      const ar=document.createElement('span'); ar.className='arrow'; ar.textContent=S.rawSort.key===k?(S.rawSort.dir<0?'▼':'▲'):'↕'; th.appendChild(ar);
      if(S.rawSort.key===k) th.setAttribute('data-sorted','');
      th.onclick=()=>{ if(S.rawSort.key===k) S.rawSort.dir*=-1; else S.rawSort={key:k,dir:k===CI.ts?-1:1}; draw(); }; hr.appendChild(th); });
    thead.appendChild(hr); t.appendChild(thead); const tb=document.createElement('tbody');
    for(const r of slice){ const tr=document.createElement('tr');
      td(tr,r[CI.id]); td(tr,new Date(r[CI.ts]*1000).toLocaleString('es-ES'),true); td(tr,DATA.municipios[r[CI.muni]]); td(tr,DATA.lotes[r[CI.lote]]);
      td(tr,DATA.prioridades[r[CI.prio]]);
      const est=DATA.estados[r[CI.estado]]; const c=document.createElement('td'); const dot=document.createElement('i');
      dot.style.cssText='display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px;background:'+ESTADO_COLORS[est]; c.appendChild(dot); c.appendChild(document.createTextNode(est)); tr.appendChild(c); tb.appendChild(tr); }
    t.appendChild(tb); wrap.innerHTML=''; wrap.appendChild(t); pager.innerHTML='';
    const prev=document.createElement('button'); prev.className='btn'; prev.textContent='‹ Anterior'; prev.disabled=S.rawPage<=1; prev.onclick=()=>{S.rawPage--;draw();};
    const next=document.createElement('button'); next.className='btn'; next.textContent='Siguiente ›'; next.disabled=S.rawPage>=pages; next.onclick=()=>{S.rawPage++;draw();};
    const lbl=document.createElement('span'); lbl.textContent='Página '+S.rawPage+' de '+fmt(pages)+' · '+fmt(total)+' filas'; pager.appendChild(prev); pager.appendChild(next); pager.appendChild(lbl); }
  function td(tr,txt,num){ const d=document.createElement('td'); if(num)d.className='num'; d.textContent=txt; tr.appendChild(d); }
  [fSearch.input,fMuni.input,fEstado.input,fPrio.input].forEach(inp=> inp.addEventListener('input',()=>{S.rawPage=1;draw();}));
  exp.onclick=()=>exportCSV(filtered()); draw();
}
function exportCSV(rows){ const head=['scraped_at','id','timestamp','municipality','lote','adapted_housing','priority','status']; const lines=[head.join(',')]; const sa=DATA.scraped_at||DATA.snapshot_date;
  for(const r of rows){ lines.push([sa,r[CI.id],new Date(r[CI.ts]*1000).toISOString(),csvq(DATA.municipios[r[CI.muni]]),DATA.lotes[r[CI.lote]],r[CI.adapted]?'True':'False',DATA.prioridades[r[CI.prio]],csvq(DATA.estados[r[CI.estado]])].join(',')); }
  download(new Blob([lines.join('\n')],{type:'text/csv;charset=utf-8'}),'planvive_'+DATA.snapshot_date+'_filtrado.csv'); }
function csvq(s){ return /[",\n]/.test(s)? '"'+s.replace(/"/g,'""')+'"' : s; }
function fieldInput(label){ const field=document.createElement('div'); field.className='field'; const l=document.createElement('label'); l.textContent=label;
  const input=document.createElement('input'); input.type='search'; input.placeholder='UUID…'; field.appendChild(l); field.appendChild(input); return {field,input}; }
function fieldSelect(label,opts){ const field=document.createElement('div'); field.className='field'; const l=document.createElement('label'); l.textContent=label;
  const input=document.createElement('select'); opts.forEach(o=>{ const op=document.createElement('option'); op.textContent=o; input.appendChild(op); }); field.appendChild(l); field.appendChild(input); return {field,input}; }

/* ------------------------------ chrome ------------------------------ */
function download(blob,name){ const u=URL.createObjectURL(blob); const a=document.createElement('a'); a.href=u; a.download=name; document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(u),1500); }
function setTab(tab){ S.tab=tab; document.querySelectorAll('.tab').forEach(b=>b.setAttribute('aria-selected', b.dataset.tab===tab));
  document.getElementById('tab-resumen').hidden=tab!=='resumen'; document.getElementById('tab-agregacion').hidden=tab!=='agregacion'; document.getElementById('tab-bruto').hidden=tab!=='bruto';
  document.getElementById('gran-field').style.display=tab==='resumen'?'':'none'; render(); }
function render(){ if(S.tab==='resumen') renderResumen(); else if(S.tab==='agregacion') renderAgregacion(); else renderBruto(); }
function initFilters(){ const fl=document.getElementById('f-lote'); DATA.lotes.forEach(l=>{ const o=document.createElement('option'); o.value=l; o.textContent=l; fl.appendChild(o); });
  fl.onchange=()=>{ S.lote=fl.value; S.rawPage=1; render(); };
  document.getElementById('f-range').onchange=e=>{ S.range=e.target.value; S.rawPage=1; render(); };
  document.getElementById('f-gran').onchange=e=>{ S.gran=e.target.value; if(S.tab==='resumen') renderResumen(); };
  document.querySelectorAll('.tab').forEach(b=> b.onclick=()=>setTab(b.dataset.tab)); }
function initTheme(){ const btn=document.getElementById('btn-theme'); btn.onclick=()=>{ const cur=document.documentElement.getAttribute('data-theme');
  const mq=window.matchMedia('(prefers-color-scheme: dark)').matches; const now=cur?cur:(mq?'dark':'light');
  document.documentElement.setAttribute('data-theme', now==='dark'?'light':'dark'); render(); }; }
function initDownload(){ document.getElementById('btn-download').onclick=()=>{ const clone=document.documentElement.cloneNode(true);
  clone.querySelectorAll('#tab-resumen,#tab-agregacion,#tab-bruto').forEach(s=>s.innerHTML=''); const tt=clone.querySelector('#tooltip'); if(tt) tt.innerHTML='';
  download(new Blob(['<!doctype html>\n'+clone.outerHTML],{type:'text/html;charset=utf-8'}),'informe_planvive_'+DATA.snapshot_date+'.html'); }; }
function initHeader(){ document.getElementById('head-sub').textContent='Instantánea '+fmtDayLong(DATA.snapshot_date)+' · '+fmt(DATA.total)+' solicitudes · histórico '+
  (DAYS.length? fmtDayLong(DAYS[0])+' → '+fmtDayLong(DAYS[NDAYS-1]) : '—')+' · generado '+fmtDateTime(DATA.generated_at); }
function initGranDefault(){ if(NDAYS>400) S.gran='mes'; else if(NDAYS>90) S.gran='semana'; else S.gran='dia';
  const g=document.getElementById('f-gran'); if(g) g.value=S.gran; }

let rT; window.addEventListener('resize',()=>{ clearTimeout(rT); rT=setTimeout(render,180); });
initGranDefault(); initFilters(); initTheme(); initDownload(); initHeader(); render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
