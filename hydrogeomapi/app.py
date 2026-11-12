"""
Channel Geometry API
--------------------
Serves ML-derived channel geometry for NHDPlus reaches and USGS gages
(Zarrabi et al., WRR, doi:10.1029/2024WR037997; data doi:10.5281/zenodo.19208847).

Two interfaces are supported by this backend:
  1. Menu interface -> POST /extract      (conus | ids | state | huc2 | huc8 | polygon)
     dataset = "reach" (default, ids are COMIDs) | "gage" (ids are USGS site numbers)
  2. Map interface  -> POST /extract      (request_type="polygon", drawn GeoJSON)
                    -> POST /extract/shapefile   (uploaded zipped shapefile polygon)

Every response is a downloadable file in the format the user chose:
  format = "csv" | "shapefile" | "geojson"

Design notes
------------
* Region filters (state/huc2/huc8) and comid filters are plain indexed WHERE
  clauses in DuckDB against the Parquet file -> fast at 2.7M rows, no spatial DB.
* Polygon queries use a two-step filter: DuckDB bbox pre-filter (cheap, uses the
  precomputed minx/miny/maxx/maxy columns) then a precise shapely intersection on
  the small candidate set. User polygons are reprojected to DATASET_CRS first.
* A size guard per format (MAX_CSV_ROWS, MAX_SHP_ROWS, MAX_GEOJSON_ROWS, and
  MAX_MAP_ROWS for the web page's map) counts the
  matches before any geometry is read. All-of-CONUS files are built once and then
  reused for every request (see _conus_cached).
"""
import gzip
import io
import shutil
import json
import os
import re
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from shapely import from_wkb
from shapely.geometry import shape

# ------------------------------------------------------------------ config
HERE = Path(__file__).parent

# Where the data files live. By default the API uses local copies if they are in
# this folder, and otherwise reads them straight from the Hugging Face dataset.
# Set CG_DATA_URL to another base address (e.g. an S3 bucket) to switch storage.
REMOTE_BASE = os.environ.get(
    "CG_DATA_URL",
    "https://huggingface.co/datasets/Reizrb/bankfull-meanflow-conus/resolve/main")


def _data_path(filename: str) -> str:
    local = HERE / filename
    if local.exists() and not os.environ.get("CG_DATA_URL"):
        return str(local)
    return f"{REMOTE_BASE.rstrip('/')}/{filename}"

DATASET_CRS = "EPSG:4269"      # NAD83, the NHDPlusV2.1 CRS (both files are built in it)
MAX_GEOJSON_ROWS = 3_000_000   # GeoJSON download (not zipped): all of CONUS, about 2.2 GB
# "Show on the map": how many reaches the web page may draw at once. Set CG_MAX_MAP_ROWS
# on the server (in ~/hydrogeom.env) to change it without a code change.
MAX_MAP_ROWS = int(os.environ.get("CG_MAX_MAP_ROWS", "50000"))
# Map lines are simplified (downloads keep full detail): about 20 m for small areas,
# about 100 m for a whole state or HUC2 region, where a region fits on one screen.
MAP_SIMPLIFY_DEG = 0.0002
MAP_SIMPLIFY_DEG_LARGE = 0.001
MAX_SYNC_ROWS = MAX_MAP_ROWS   # older name, kept for anything that still uses it
MAX_CSV_ROWS = 3_000_000       # CSV (numbers only, no line shapes): all of CONUS


MAX_SHP_ROWS = 3_000_000       # shapefile (line shapes, zipped): all of CONUS


def _limit(fmt: str) -> int:
    f = (fmt or "csv").lower()
    return {"csv": MAX_CSV_ROWS, "shapefile": MAX_SHP_ROWS, "geojson": MAX_GEOJSON_ROWS,
            "map": MAX_MAP_ROWS}.get(f, MAX_MAP_ROWS)
ZENODO_URL = "https://zenodo.org/records/19208847"


class TooBig(Exception):
    """Raised before any geometry is loaded when a request is over the format's limit."""
    def __init__(self, n, huc2s, limit, fmt):
        self.n, self.huc2s, self.limit, self.fmt = n, huc2s, limit, fmt
GEOM_ATTRS = ["bnk_width", "bnk_depth", "mf_width", "mf_depth"]

# One entry per dataset. `id_col` is what request_type="ids" matches against.
DATASETS = {
    "reach": {
        "path": _data_path("reaches.parquet"),
        "id_col": "comid", "id_type": int,
        "cols": ["comid", "reachcode", "state", "huc2", "huc8", "stream_order",
                 "tot_da_sqkm"] + GEOM_ATTRS,
    },
    "gage": {
        "path": _data_path("gages.parquet"),
        "id_col": "site_no", "id_type": str,   # text, so leading zeros survive
        "cols": ["site_no", "station_nm", "comid", "reachcode", "state", "huc2",
                 "huc8", "da_sqkm", "lat", "lon"] + GEOM_ATTRS,
    },
}


def _ds(name: str) -> dict:
    ds = DATASETS.get(name.lower())
    if ds is None:
        raise HTTPException(400, f"Unknown dataset '{name}'. Use reach or gage.")
    if not _available(ds):
        raise HTTPException(503, f"The {name} dataset is not available on this server.")
    return ds


def _available(ds: dict) -> bool:
    p = ds["path"]
    return p.startswith("http") or Path(p).exists()

API_VERSION = "1.0.0"
API_DESCRIPTION = """
Machine-learning estimates of bankfull and mean-flow channel width and depth for
2.7 million NHDPlusV2.1 river reaches and 28,000 USGS gages across CONUS
([Zarrabi et al., 2025](https://doi.org/10.1029/2024WR037997)).

**Size per request:** CSV and shapefile, any area up to all of CONUS.
GeoJSON (not zipped, so larger files), any area up to all of CONUS.
All-of-CONUS files are prepared in advance: get them from `GET /download/conus`.

[Web page](/) | [About the data and how to cite](/about) |
[Code](https://github.com/Reizrb/HydroGeom) |
[Full dataset on Zenodo](https://doi.org/10.5281/zenodo.19208847)

Hosted on CIROH cloud computing resources (AWS). Funding for this project was provided by
NOAA, awarded to CIROH through the Cooperative Agreement with The University of Alabama
(NA22NWS4320003).
"""
app = FastAPI(title="HydroGeomAPI", version=API_VERSION, description=API_DESCRIPTION)
# allow the browser map/menu frontend to call this API
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


# The web page (HydroGeomSite) lives in the hydrogeomsite folder next to this one.
_SITE_DIRS = [HERE.parent / "hydrogeomsite", HERE / "hydrogeomsite"]
STATIC = next((d for d in _SITE_DIRS if d.exists()), _SITE_DIRS[0])
if not STATIC.exists():
    print(f"WARNING: web page folder not found (looked in {_SITE_DIRS}); serving the API only.")
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def home():
    """The web page (menu + map) for people who don't want to write code."""
    page = STATIC / "index.html"
    if page.exists():
        return FileResponse(page)
    return {"message": "Channel Geometry API. See /docs."}


# One shared DuckDB database for the whole app, so what it learns about the data
# files (their layout, which parts hold which HUCs) is cached between requests
# instead of being downloaded again from Hugging Face every time.
_DB = duckdb.connect()
_DB.execute("PRAGMA threads=2")      # CIROH AWS server: 2 vCPUs
_DB.execute("SET memory_limit = '2GB'")    # CIROH AWS server: 4 GB in total
_DB.execute(f"SET temp_directory = '{tempfile.gettempdir()}/duckdb_spill'")
for _setting in ("SET enable_object_cache = true",
                 "SET enable_http_metadata_cache = true"):
    try:
        _DB.execute(_setting)
    except Exception:          # older/newer DuckDB versions may not have both
        pass


def _load_spatial(db):
    """DuckDB's spatial extension: geometry tests and GeoJSON/shapefile writing inside
    DuckDB, so large results never have to be held in Python memory."""
    try:
        db.execute("LOAD spatial"); return
    except Exception:
        pass
    try:                                   # pip package duckdb-extension-spatial
        import duckdb_extension_spatial as pkg
        ext = next(Path(pkg.__file__).parent.rglob("spatial.duckdb_extension"))
        db.execute(f"LOAD '{ext}'"); return
    except Exception:
        pass
    db.execute("INSTALL spatial"); db.execute("LOAD spatial")


_load_spatial(_DB)


@app.get("/about", include_in_schema=False)
def about():
    """About the data: how it was made, how to cite, and contact."""
    return FileResponse(STATIC / "about.html")


def con():
    """A cursor on the shared database (safe to use from several requests at once)."""
    c = _DB.cursor()
    try:
        c.execute("SET geometry_always_xy = true")   # DuckDB 2.x: lon/lat axis order for GDAL
    except Exception:                                # older DuckDB has no such setting
        pass
    return c


# ------------------------------------------------------------------ request model
class ExtractRequest(BaseModel):
    dataset: str = Field("reach", description="reach | gage")
    request_type: str = Field(..., description="conus | ids | comids | state | huc2 | huc8 | polygon")
    format: str = Field("csv", description="csv | shapefile | geojson")
    for_map: bool = Field(False, description="Set by the web page when drawing on its map: "
                          "GeoJSON is then limited to 50,000 records")
    # one of these is used depending on request_type:
    ids: list[int | str] | None = Field(None, description="COMIDs (reach) or USGS site numbers (gage, as text)")
    comids: list[int] | None = Field(None, description="kept for older clients; same as ids")
    state: str | None = None
    huc2: str | None = None
    huc8: str | None = None
    polygon: dict | None = None   # GeoJSON geometry (from the drawn polygon), WGS84


# ------------------------------------------------------------------ query helpers
def _check_size(ds: dict, where_sql: str, params: list, fmt: str = "csv"):
    """Count matches first (cheap: no geometry is read) and stop oversized requests
    before they use up the server's memory."""
    where = f" WHERE {where_sql}" if where_sql else ""
    n, huc2s = con().execute(
        f"SELECT count(*), list(DISTINCT huc2 ORDER BY huc2) "
        f"FROM read_parquet('{ds['path']}'){where}", params).fetchone()
    if n > _limit(fmt):
        raise TooBig(n, [h for h in huc2s if h], _limit(fmt), fmt)


def _fetch_by_filter(ds: dict, where_sql: str, params: list, fmt: str = "csv") -> tuple:
    """Check the size, then return the query (WHERE clause + parameters) to export."""
    _check_size(ds, where_sql, params, fmt)
    return (where_sql, list(params))


def _fetch_by_polygon(ds: dict, geom, fmt: str = "csv") -> tuple:
    """Bounding-box pre-filter (cheap, uses the precomputed corner columns), then an
    exact intersection test, both done inside DuckDB."""
    minx, miny, maxx, maxy = geom.bounds
    bbox = "minx <= ? AND maxx >= ? AND miny <= ? AND maxy >= ?"
    bparams = [maxx, minx, maxy, miny]
    _check_size(ds, bbox, bparams, fmt)
    where = bbox + " AND ST_Intersects(ST_GeomFromWKB(geom_wkb), ST_GeomFromText(?))"
    return (where, bparams + [geom.wkt])


def _user_polygon_to_dataset_crs(geojson_geom: dict):
    """Drawn polygons arrive as GeoJSON in WGS84; reproject to the dataset CRS."""
    g = shape(geojson_geom)
    s = gpd.GeoSeries([g], crs="EPSG:4326").to_crs(DATASET_CRS)
    return s.iloc[0]


# ------------------------------------------------------------------ formatting
def _map_cols(ds: dict) -> str:
    """Only what the map's popups use, with values rounded to centimeters."""
    ids = ["comid", "stream_order"] if ds["id_col"] == "comid" else ["site_no", "station_nm"]
    return ", ".join(ids + [f"round({a}, 2) AS {a}" for a in GEOM_ATTRS])


def _stream(ds: dict, query: tuple, fmt: str, name: str = "reaches", for_map: bool = False,
            large_area: bool = False):
    """Write the result straight to a file with DuckDB (low memory), then send it."""
    where_sql, params = query
    fmt = fmt.lower()
    cols = ", ".join(ds["cols"])
    where = f" WHERE {where_sql}" if where_sql else ""
    src = f"FROM read_parquet('{ds['path']}'){where}"
    td = tempfile.mkdtemp(prefix="cg_")
    cleanup = BackgroundTask(shutil.rmtree, td, ignore_errors=True)
    try:
        if fmt == "csv":
            out = Path(td) / f"{name}.csv"
            n = con().execute(
                f"COPY (SELECT {cols} {src}) "
                f"TO '{out}' (FORMAT CSV, HEADER)", params).fetchone()[0]
            media = "text/csv"
        elif fmt == "geojson":
            out = Path(td) / f"{name}.geojson"
            if for_map:    # fewer columns, simplified lines, fewer decimals
                tol, prec = (MAP_SIMPLIFY_DEG_LARGE, 4) if large_area else (MAP_SIMPLIFY_DEG, 5)
                sel = _map_cols(ds)
                geom = f"ST_SimplifyPreserveTopology(ST_GeomFromWKB(geom_wkb), {tol})"
            else:
                sel, prec, geom = cols, 6, "ST_GeomFromWKB(geom_wkb)"
            n = con().execute(
                f"COPY (SELECT {sel}, {geom} AS geom {src}) "
                f"TO '{out}' (FORMAT GDAL, DRIVER 'GeoJSON', SRS 'EPSG:4269', "
                f"LAYER_CREATION_OPTIONS 'COORDINATE_PRECISION={prec}')", params).fetchone()[0]
            media = "application/geo+json"
        elif fmt == "shapefile":
            shp_dir = Path(td) / "shp"; shp_dir.mkdir()
            n = con().execute(
                f"COPY (SELECT {cols}, ST_GeomFromWKB(geom_wkb) AS geom {src}) "
                f"TO '{shp_dir / (name + '.shp')}' (FORMAT GDAL, DRIVER 'ESRI Shapefile', "
                f"SRS 'EPSG:4269')", params).fetchone()[0]
            out = Path(td) / f"{name}.zip"
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                for f in shp_dir.glob(f"{name}.*"):
                    z.write(f, f.name)
            media = "application/zip"
        else:
            raise HTTPException(400, f"Unknown format '{fmt}'. Use csv, shapefile, or geojson.")
    except Exception:
        shutil.rmtree(td, ignore_errors=True)
        raise
    if not n:
        shutil.rmtree(td, ignore_errors=True)
        raise HTTPException(404, "No records matched the query.")
    if for_map:
        # map data is sent compressed (about 5 times smaller); browsers unpack it automatically
        gz = out.with_name(out.name + ".gz")
        with open(out, "rb") as fin, gzip.open(gz, "wb", compresslevel=5) as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
        out.unlink()
        return FileResponse(gz, media_type=media, filename=out.name, background=cleanup,
                            headers={"Content-Encoding": "gzip"})
    return FileResponse(out, media_type=media, filename=out.name, background=cleanup)


# ------------------------------------------------------------------ CONUS cache
# An all-of-CONUS file is the same for everyone, so it is built once and then reused.
# Delete the files in CACHE_DIR whenever the data files change.
CACHE_DIR = Path(os.environ.get("CG_CACHE_DIR", "/app/cache"))
_CACHE_LOCK = threading.Lock()
_EXT = {"csv": "csv", "geojson": "geojson", "shapefile": "zip"}
_MEDIA = {"csv": "text/csv", "geojson": "application/geo+json",
          "shapefile": "application/zip"}


# Map data for a whole state or HUC2 region is also the same for everyone. It is saved
# (compressed) the first time it is asked for, or ahead of time by prepare_map_cache().
_LOCKS_GUARD = threading.Lock()
_MAP_LOCKS: dict = {}


def _lock_for(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _MAP_LOCKS.setdefault(str(path), threading.Lock())


def _map_cached(ds: dict, col: str, code: str, name: str):
    """Map data for one state or HUC2 region: built once, then reused."""
    _check_size(ds, f"{col} = ?", [code], "map")      # the map limit still applies
    cached = CACHE_DIR / "map" / f"{name}_{col}_{code}.geojson.gz"
    with _lock_for(cached):
        if not cached.exists():
            built = _stream(ds, (f"{col} = ?", [code]), "geojson", name,
                            for_map=True, large_area=True)
            cached.parent.mkdir(parents=True, exist_ok=True)
            part = cached.with_name(f"{cached.name}.{os.getpid()}.part")
            shutil.move(built.path, part)
            part.replace(cached)
            shutil.rmtree(Path(built.path).parent, ignore_errors=True)
    return FileResponse(cached, media_type="application/geo+json", filename=f"{name}.geojson",
                        headers={"Content-Encoding": "gzip"})


def _map_cache_request(ds: dict, req, rt: str):
    """Use the saved map data when the request is a whole state or HUC2 region."""
    if rt == "state" and req.state and re.fullmatch(r"[A-Za-z]{2}", req.state.strip()):
        return _map_cached(ds, "state", req.state.strip().upper(), _outname(req.dataset))
    if rt == "huc2" and req.huc2 and re.fullmatch(r"\d{1,2}", req.huc2.strip()):
        return _map_cached(ds, "huc2", req.huc2.strip().zfill(2), _outname(req.dataset))
    return None


def prepare_map_cache():
    """Build the map data for every state and HUC2 region ahead of time, so nobody waits.
    Run on the server after the data files change (update.sh --clear-cache does this)."""
    for dsname, ds in DATASETS.items():
        if not _available(ds):
            continue
        name = _outname(dsname)
        for col in ("huc2", "state"):
            try:
                codes = [r[0] for r in con().execute(
                    f"SELECT DISTINCT {col} FROM read_parquet('{ds['path']}') "
                    f"WHERE {col} IS NOT NULL ORDER BY 1").fetchall()]
            except Exception as err:
                print(f"  {name}: could not list {col} codes ({err})", flush=True)
                continue
            for code in codes:
                if not re.fullmatch(r"[A-Za-z0-9]{1,4}", str(code)):
                    continue
                t = time.time()
                try:
                    _map_cached(ds, col, str(code), name)
                    print(f"  {name} {col} {code}: ready ({time.time() - t:.0f} s)", flush=True)
                except TooBig as err:
                    print(f"  {name} {col} {code}: skipped, {err.n:,} records is over the "
                          f"map limit of {err.limit:,}", flush=True)
                except HTTPException as err:
                    print(f"  {name} {col} {code}: skipped ({err.detail})", flush=True)


def _conus_cached(ds: dict, fmt: str, name: str):
    _check_size(ds, "", [], fmt)          # same size rules as any other request
    fmt = fmt.lower()
    cached = CACHE_DIR / f"conus_{name}_{fmt}.{_EXT[fmt]}"
    with _CACHE_LOCK:                     # one build at a time; others wait, then reuse it
        if not cached.exists():
            built = _stream(ds, ("", []), fmt, name)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            part = cached.with_name(cached.name + ".part")
            shutil.move(built.path, part)
            part.replace(cached)
            shutil.rmtree(Path(built.path).parent, ignore_errors=True)
    return FileResponse(cached, media_type=_MEDIA[fmt], filename=f"{name}.{_EXT[fmt]}")


# ------------------------------------------------------------------ endpoints
def _too_big(err: TooBig, what: str) -> HTTPException:
    """413 answer that tells the user where to get large areas instead."""
    msg = (f"{what} has {err.n:,} records, more than the {err.limit:,} this API returns "
           f"in one {err.fmt.upper() if err.fmt != 'shapefile' else 'shapefile'} download. ")
    fmt = err.fmt.lower()
    if fmt == "map":
        msg = (f"{what} has {err.n:,} records, more than the {err.limit:,} the map can show "
               "at once. Try a smaller area, like a HUC8, or download the file instead.")
    elif fmt == "geojson" and err.n <= MAX_SHP_ROWS:
        msg += ("Choose Shapefile to get the line shapes for this whole area, "
                "or CSV for the width and depth values only.")
    elif fmt != "csv" and err.n <= MAX_CSV_ROWS:
        msg += "Choose CSV to get the width and depth values for this whole area."
    else:
        msg += "Ask for a smaller area, or download the full dataset from Zenodo."
    return HTTPException(413, {"message": msg, "records": err.n, "limit": err.limit,
                               "full_dataset": ZENODO_URL})


@app.post("/extract")
def extract(req: ExtractRequest):
    try:
        return _extract(req)
    except TooBig as err:
        what = {"conus": "All of CONUS", "state": f"State {req.state}",
                "huc2": f"HUC2 {req.huc2}", "polygon": "This polygon"
                }.get(req.request_type.lower(), "This request")
        raise _too_big(err, what)


def _extract(req: ExtractRequest):
    ds = _ds(req.dataset)
    _check_format(req.format)
    rt = req.request_type.lower()
    size_fmt = "map" if req.for_map and req.format.lower() == "geojson" else req.format
    if size_fmt == "map" and rt in ("state", "huc2"):
        saved = _map_cache_request(ds, req, rt)
        if saved is not None:
            return saved
    if rt == "conus":
        if size_fmt == "map":
            _check_size(ds, "", [], size_fmt)
        return _conus_cached(ds, req.format, _outname(req.dataset))
    elif rt in ("ids", "comids"):
        raw = req.ids or ([str(c) for c in req.comids] if req.comids else None)
        if not raw:
            raise HTTPException(400, "ids is required for request_type=ids")
        try:
            ids = [ds["id_type"](str(i).strip()) for i in raw]
        except ValueError:
            raise HTTPException(400, f"ids for the {req.dataset} dataset must be "
                                     f"{ds['id_type'].__name__} values")
        placeholders = ", ".join("?" * len(ids))
        gdf = _fetch_by_filter(ds, f"{ds['id_col']} IN ({placeholders})", ids, size_fmt)
    elif rt == "state":
        if not req.state:
            raise HTTPException(400, "state is required for request_type=state")
        gdf = _fetch_by_filter(ds, "state = ?", [req.state.upper()], size_fmt)
    elif rt == "huc2":
        if not req.huc2:
            raise HTTPException(400, "huc2 is required for request_type=huc2")
        gdf = _fetch_by_filter(ds, "huc2 = ?", [req.huc2.zfill(2)], size_fmt)
    elif rt == "huc8":
        if not req.huc8:
            raise HTTPException(400, "huc8 is required for request_type=huc8")
        gdf = _fetch_by_filter(ds, "huc8 = ?", [req.huc8.zfill(8)], size_fmt)
    elif rt == "polygon":
        if not req.polygon:
            raise HTTPException(400, "polygon (GeoJSON) is required for request_type=polygon")
        geom = _user_polygon_to_dataset_crs(req.polygon)
        gdf = _fetch_by_polygon(ds, geom, size_fmt)
    else:
        raise HTTPException(400, f"Unknown request_type '{req.request_type}'")

    return _stream(ds, gdf, req.format, _outname(req.dataset), for_map=(size_fmt == "map"),
                   large_area=rt in ("state", "huc2"))


def _check_format(fmt: str):
    if fmt.lower() not in ("csv", "geojson", "shapefile"):
        raise HTTPException(400, f"Unknown format '{fmt}'. Use csv, shapefile, or geojson.")


def _outname(dataset: str) -> str:
    return "gages" if dataset.lower() == "gage" else "reaches"


@app.post("/extract/shapefile")
async def extract_shapefile(
    file: UploadFile = File(..., description="zipped shapefile (.shp/.shx/.dbf/.prj)"),
    format: str = Form("csv"),
    dataset: str = Form("reach"),
    for_map: bool = Form(False),
):
    """Map interface: uploaded polygon shapefile. Reprojects to dataset CRS,
    then intersects. Accepts a .zip containing the shapefile parts."""
    ds = _ds(dataset)
    _check_format(format)
    raw = await file.read()
    with tempfile.TemporaryDirectory() as td:
        zpath = Path(td) / "upload.zip"
        zpath.write_bytes(raw)
        try:
            with zipfile.ZipFile(zpath) as z:
                z.extractall(td)
        except zipfile.BadZipFile:
            raise HTTPException(400, "Upload must be a .zip containing the shapefile parts.")
        shp = next(Path(td).rglob("*.shp"), None)
        if shp is None:
            raise HTTPException(400, "No .shp found in the uploaded zip.")
        poly = gpd.read_file(shp)          # reads .prj automatically
        if poly.crs is None:
            raise HTTPException(400, "Shapefile has no .prj / CRS; cannot reproject.")
        poly = poly.to_crs(DATASET_CRS)    # <- the step that prevents silent empty results
        geom = poly.union_all()            # merge all polygon features into one mask

    try:
        size_fmt = "map" if for_map and format.lower() == "geojson" else format
        gdf = _fetch_by_polygon(ds, geom, size_fmt)
    except TooBig as err:
        raise _too_big(err, "This polygon")
    return _stream(ds, gdf, format, _outname(dataset), for_map=(size_fmt == "map"))


@app.get("/download/conus")
def download_conus(dataset: str = "reach", format: str = "csv"):
    """All of CONUS as one file, prepared in advance. A plain link (GET), so browsers
    save it straight to disk instead of holding it in memory, even at 2 GB."""
    ds = _ds(dataset)
    _check_format(format)
    try:
        return _conus_cached(ds, format, _outname(dataset))
    except TooBig as err:
        raise _too_big(err, "All of CONUS")


@app.get("/limits")
def limits():
    """Most records per request for each format, and for drawing on the web page's map."""
    return {"csv": MAX_CSV_ROWS, "shapefile": MAX_SHP_ROWS, "geojson": MAX_GEOJSON_ROWS,
            "map": MAX_MAP_ROWS}


_COUNTS: dict = {}


@app.get("/health")
async def health():
    """Fast check used by the host: answers immediately, without reading the data."""
    return {"status": "ok"}


@app.get("/status")
def status():
    """Record counts and data source (reads the file footers once, then cached)."""
    if not _COUNTS:
        for name, ds in DATASETS.items():
            if _available(ds):
                _COUNTS[name] = con().execute(
                    f"SELECT count(*) FROM read_parquet('{ds['path']}')").fetchone()[0]
            else:
                _COUNTS[name] = "not loaded"
    source = "local files" if not DATASETS["reach"]["path"].startswith("http") else REMOTE_BASE
    return {"status": "ok", "api_version": API_VERSION, "records": _COUNTS,
            "data_source": source, "dataset_crs": DATASET_CRS}
