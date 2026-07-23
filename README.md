# planvive

Scraper del **listado de solicitantes del Plan Vive** de la Comunidad de Madrid,
publicado en <https://vivetuavalon.com/plan-vive/lists-inscription/>.

Dos piezas:

1. **`planvive.py`** — descarga el listado completo de los lotes indicados (por
   defecto **Lote 1** y **Lote 2**) y lo vuelca a un CSV con columna `lote` para
   filtrar. Cada ejecución genera un **snapshot diario** sellado con la fecha/hora
   del scrape (`output/snapshots/planvive_<fecha>.csv`), pensado para re-scrapear
   a diario y construir una serie temporal.
2. **`dashboard.py`** — lee todos los snapshots y genera un **informe HTML
   autónomo** (`output/dashboard.html`): tendencias temporales, agregación y datos
   en bruto filtrables/ordenables. Un solo fichero, offline, compartible.

## Cómo funciona

La web no trae los datos en el HTML: la tabla se rellena en el navegador
consumiendo una API JSON paginada que se descubre en el bundle
`inscriptionList.*.js`:

```
GET https://vivetuavalon.com/plan-vive/lists/?page=<N>&lote=<Lote X>&search=
```

Respuesta (estilo Django REST Framework):

```json
{
  "count": 97479,
  "next": "…?page=2…",
  "previous": null,
  "results": [
    {
      "id": "35e9b269-…",
      "timestamp": "2024-09-19T08:00:16.411761Z",
      "municipality": "Alcalá de Henares",
      "lote": "Lote 1",
      "adapted_housing": false,
      "priority": "P1",
      "status": "Desistida"
    }
  ]
}
```

Cada registro ya incluye el campo `lote`, que se usa como columna del CSV.

### Comportamiento del servidor (medido)

- **Tamaño de página fijo = 20**. Los parámetros `page_size`/`limit` se ignoran.
- **Dos límites de tasa, ambos por IP de origen:**
  - *Aplicación (DRF)*: responde JSON `{"detail": "...throttled... available in N
    seconds"}`.
  - *Edge / infraestructura (Google Cloud)*: responde una página **HTML** `429
    Too Many Requests`. Si se dispara, **banea la IP ~3 min**.
- Una sola IP aguanta como mucho **~1 req/s** sostenida. Un arranque a ~3 req/s
  parece ir bien unos cientos de peticiones y luego colapsa (ventana deslizante).
- Volumen (a fecha de creación): Lote 1 ≈ 97 k, Lote 2 ≈ 129 k registros, es
  decir **~11.300 páginas** (~80 MB de JSON en total).

### Velocidad y proxies

El throttle es **por IP**, así que la única forma de ir en paralelo es repartir
la carga entre varias IPs de salida (proxies). El scraper regula el ritmo
**por proxy** (`--per-proxy-interval`), manteniendo cada IP por debajo de su
límite mientras el conjunto avanza en paralelo.

| Modo | Comando | ETA aprox. |
|------|---------|-----------|
| 1 IP directa (lento, educado) | `python3 planvive.py` | ~3-4 h |
| Pool de proxies (datacenter/residencial) | `--proxy-file proxies.txt --workers 12` | ~10-20 min |
| Gateway residencial rotativo | `--proxy http://user:pass@gw:port --rotating-gateway --workers 25` | pocos min |

- **Pool** (`--proxy` repetible o `--proxy-file`): round-robin entre proxies;
  ante 429 el proxy entra en *cooldown* y la petición se reintenta por otro.
- **Gateway rotativo** (`--rotating-gateway`): una URL que da IP nueva por
  petición; sube `--workers`.
- `--per-proxy-interval` (def. 0,9 s): espaciado entre peticiones de una misma
  IP. Bájalo si tus IPs aguantan más.

> Las credenciales de proxy **no deben commitearse**. Usa `proxies.txt` (está en
> `.gitignore`) o pásalas por `--proxy`.

## Reanudable

Cada página descargada se guarda como una línea JSON en
`output/checkpoints/<lote>.jsonl` (`{"page": N, "rows": [...]}`). Si el proceso
se interrumpe, al relanzarlo se saltan las páginas ya completadas. El CSV final
se reconstruye siempre desde los checkpoints, **deduplicando por `id`** (la lista
es un dato vivo y las inserciones pueden solapar registros entre páginas).

## Uso

```bash
# Snapshot de hoy -> output/snapshots/planvive_<fecha>.csv (1 IP, lento)
python3 planvive.py

# Con proxies (recomendado para el listado completo)
python3 planvive.py --proxy-file proxies.txt --rotating-gateway --workers 24 --per-proxy-interval 0.06

# Solo un lote
python3 planvive.py --lotes "Lote 1"

# Validación: pocas páginas a un dir aparte (mide % de 429)
python3 planvive.py --proxy-file proxies.txt --limit-pages 30 --output-dir /tmp/pv_test

# Reconstruir el CSV del snapshot desde los checkpoints, sin volver a descargar
python3 planvive.py --rebuild-csv
```

Sin dependencias externas: solo librería estándar de Python 3.11.

### Opciones

| Flag | Por defecto | Descripción |
|------|-------------|-------------|
| `--lotes` | `"Lote 1" "Lote 2"` | Lotes a descargar. |
| `--output-dir` | `./output` | Directorio de salida (snapshots + checkpoints). |
| `--snapshot-date` | hoy | Fecha del snapshot `YYYY-MM-DD` (clave del CSV diario). |
| `--csv-name` | (auto) | Nombre fijo del CSV; por defecto `snapshots/planvive_<fecha>.csv`. |
| `--limit-pages` | (todas) | Limita el nº de páginas por lote (pruebas). |
| `--rebuild-csv` | — | Solo reconstruye el CSV desde checkpoints. |

## Snapshots diarios y sello temporal

Cada ejecución escribe `output/snapshots/planvive_<fecha>.csv`. Todas las filas
llevan una columna **`scraped_at`** con la fecha/hora exacta de ese scrape, que es
lo que fija el punto temporal del snapshot. Re-ejecutar el mismo día sobrescribe
el CSV de ese día; cada día nuevo añade un snapshot y, por tanto, un punto en las
tendencias del dashboard.

Columnas del CSV:

| Columna | Descripción |
|---------|-------------|
| `scraped_at` | **Fecha/hora de este scrape (ISO 8601).** Sella el snapshot. |
| `id` | Código de la solicitud (UUID). |
| `timestamp` | Fecha y hora de la solicitud (ISO 8601 UTC). |
| `municipality` | Municipio. |
| `lote` | **Lote — columna de filtrado.** |
| `adapted_housing` | Vivienda adaptada (`True`/`False`). |
| `priority` | Nivel de prioridad (`P0`–`P3`). |
| `status` | Estado (`Creada`, `En trámite`, `Contrato firmado`, `Desistida`, `Rechazada`). |

También se escribe `output/_run_stats.json` con el resumen de la ejecución.

## Dashboard / informe HTML

```bash
python3 dashboard.py                 # último snapshot -> output/dashboard.html
python3 dashboard.py --csv ruta.csv  # un CSV concreto
python3 dashboard.py --no-raw        # informe ligero (sin la tabla en bruto)
python3 dashboard.py --max-raw 50000 # limita filas de la tabla en bruto
```

`dashboard.py` lee el **último snapshot** de `output/snapshots/` (un volcado
completo y actualizado de toda la lista) y genera un único `output/dashboard.html`
**autocontenido** (CSS + JS + datos incrustados, sin CDN — se abre offline con
doble clic).

**Eje temporal = el `timestamp` de cada solicitud** (cuándo se presentó), no el
momento del scrape. Como un solo CSV contiene todas las solicitudes desde el
inicio del programa, reconstruye toda la evolución histórica sin necesidad de
varios snapshots.

> ⚠️ **Cohorte, no historia de estados.** El `status`/`priority` son los
> **actuales** (del scrape). Por eso los desgloses "por fecha de solicitud" son
> vistas de cohorte: *de lo presentado en el periodo X, en qué estado está hoy* —
> no cómo estaba entonces. Para una historia real de cambios de estado harían
> falta varios snapshots (que el scraper sigue guardando a diario).

Tres pestañas:

- **Resumen** — KPIs (total, por lote, contratos firmados, nuevas en los últimos
  30 días con variación) con *sparklines*, más tendencias: solicitudes
  **acumuladas** por lote, **nuevas por periodo** y **estado actual por periodo de
  solicitud** (cohorte).
- **Agregación (centrada en municipios)** — el desglose clave es **estado de las
  solicitudes por municipio**: barras apiladas por municipio (modo nº o %), tabla
  **municipio × estado** ordenable por cualquier columna (p. ej. por «Contrato
  firmado»), y un **detalle por municipio** con su evolución de estados por periodo
  de solicitud. Además, distribuciones globales por estado y por prioridad.
- **Datos en bruto** — la instantánea completa como la web oficial pero más limpia:
  ordenable por cualquier columna (por defecto por fecha), filtrable (lote,
  municipio, estado, prioridad, búsqueda por código) y con **exportación a CSV**
  del subconjunto filtrado (el CSV conserva la columna `adapted_housing` del
  origen aunque no se muestre en la tabla).

Filtros globales arriba: **lote**, **periodo** (rango sobre la fecha de solicitud
— filtra las 3 pestañas) y **granularidad** día/semana/mes para las tendencias.
Tema claro/oscuro; y botón **«Descargar informe»** que guarda una copia autónoma
del HTML para compartir — quien la abra ve exactamente los mismos datos.

El informe es grande porque la pestaña de datos en bruto incrusta el snapshot
completo (~14 MB con 226 k filas). Para compartir algo más ligero usa `--no-raw`
o `--max-raw`.

## Historial de cambios (CDC) y base de datos

El listado es un snapshot del **estado actual**: cada `id` aparece una sola vez,
con su último estado. No se puede ver cómo evolucionó una solicitud desde un solo
CSV. Para reconstruir la historia (y medir *cuánto tarda* en resolverse una
solicitud) hay que **diffear snapshots diarios** y guardar solo los cambios.

`ingest.py` hace ese *change-data-capture* sobre una BD **SQLite** (`planvive.db`,
librería estándar):

```bash
python3 ingest.py            # ingiere snapshots nuevos de output/snapshots/
python3 ingest.py --stats    # resumen de la BD
```

Compara el snapshot del día contra el estado guardado y registra **altas**
(`created`), **cambios de estado/prioridad**, **bajas** (`disappeared`) y
**reapariciones**. Materializa `current_state` (estado vigente) y la dimensión
`solicitud` (con `submitted_at` = creación real, `first_seen`/`last_seen`).
Guardar deltas en vez de copias completas mantiene la BD compacta (la primera
carga *génesis* es ~68 MB; cada día siguiente solo añade los cambios).

Tablas: `solicitud`, `current_state`, `event` (log de cambios), `scrape_run`
(metadatos + contadores por día). Con esto se derivan, **agregadas por
municipio**: tiempos de resolución, matriz de transiciones, tasa de conversión y
flujo diario (altas/cambios/bajas).

> **Avisos.** (1) *Resolución diaria*: un cambio ocurrió entre dos scrapes (±1
> día). (2) *Censura por la izquierda*: los 226 k ya existentes el primer día
> entran como línea base sin historia previa; las duraciones **precisas** solo
> salen de transiciones observadas en directo. Por eso conviene **arrancar el
> ingester cuanto antes** — cada día sin diffear es historia que se pierde.

## Flujo diario

`run_daily.sh` encadena scrape → ingesta CDC → dashboard:

```bash
./run_daily.sh
```

Ideal para un `cron` diario. Cada ejecución guarda el snapshot del día, actualiza
`planvive.db` con los cambios y regenera `output/dashboard.html` — sin cargar nada
a mano.

## Producción: GitHub Actions + Pages + Releases

`.github/workflows/daily_pipeline.yml` automatiza todo a coste cero (cron 03:00
UTC, o disparo manual). En cada ejecución:

1. **Restaura** `planvive.db` desde el asset del release rodante `db-latest`
   (los runners son efímeros; así el histórico persiste).
2. **Scrapea** el snapshot del día (proxies desde el secret `PROXIES_TXT`).
3. **Ingiere** los cambios en la BD (`ingest.py`).
4. **Genera** el dashboard y lo publica en **GitHub Pages** (solo el HTML).
5. **Archiva** el snapshot comprimido `planvive_<fecha>.csv.gz` como asset de un
   release `snapshot-<fecha>` (archivo histórico, permite reconstruir la BD).
6. **Persiste** `planvive.db.gz` de vuelta en `db-latest` (`gh release upload
   --clobber`).

Requisitos:

- El workflow asume que **`src/planvive/` es la raíz del repo** de GitHub (usa
  `python3 planvive.py`). Si publicas el monorepo, mueve el fichero a la raíz y
  añade `working-directory: src/planvive`.
- Secret **`PROXIES_TXT`** con el contenido de `proxies.txt` (Settings → Secrets
  → Actions). El `GITHUB_TOKEN` estándar basta para releases y Pages.
- Activa **Pages** en modo *Deploy from a branch → `gh-pages`*.
- Los snapshots CSV y `planvive.db` **no** se commitean (van a releases); ver
  `.gitignore`.

> Ojo: publicar en Pages sirve el listado completo (anonimizado, ya público en la
> web oficial). Si prefieres una página pública más ligera, cambia el paso de
> dashboard a `python3 dashboard.py --no-raw` (informe de ~40 KB sin la tabla en
> bruto).
