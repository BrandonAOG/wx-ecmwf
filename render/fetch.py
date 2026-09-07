"""
Download GFS subsets from NOAA NOMADS and load them into plain numpy arrays.

The grib_filter CGI lets us request only the variables/levels/bbox we need, so
each forecast hour is a few MB. GRIB decoding uses cfgrib (needs the eccodes
system library: `apt install libeccodes-dev` or `conda install eccodes`).
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import requests

from config import (MODEL, NOMADS_DIR, NOMADS_FILE, NOMADS_FILTER, NOMADS_IDX,
                    PARAMS)

# ECMWF open data file layout (one GRIB2 per step, all params)
ECMWF_FILE = "https://data.ecmwf.int/forecasts/{ymd}/{hh}z/ifs/0p25/oper/{ymd}{hh}0000-{step}h-oper-fc.grib2"

log = logging.getLogger("fetch")

# cfgrib short names for each (VAR, LEVEL) pair we ask NOMADS for.
CFGRIB_NAMES = {
    ("HGT", "500_mb"): "gh",
    ("HGT", "850_mb"): "gh",
    ("HGT", "1000_mb"): "gh",
    ("ABSV", "500_mb"): "absv",
    ("PRMSL", "mean_sea_level"): "prmsl",
    ("APCP", "surface"): "tp",
    ("TMP", "850_mb"): "t",
    ("TMP", "2_m_above_ground"): "t2m",
    ("UGRD", "850_mb"): "u",
    ("VGRD", "850_mb"): "v",
    ("UGRD", "10_m_above_ground"): "u10",
    ("VGRD", "10_m_above_ground"): "v10",
    ("PWAT", "entire_atmosphere_\\(considered_as_a_single_layer\\)"): "pwat",
    ("CAPE", "surface"): "cape",
}


def _candidate_cycles(now: dt.datetime):
    """Cycles in MODEL['cycles'], newest first, that are old enough to be complete."""
    start = (now - dt.timedelta(hours=MODEL["min_age_hours"])).replace(minute=0, second=0, microsecond=0)
    c = start
    for _ in range(48):
        if c.hour in MODEL["cycles"]:
            yield c
        c -= dt.timedelta(hours=1)


def latest_available_run(now: dt.datetime | None = None,
                         session: requests.Session | None = None) -> dt.datetime:
    """Newest cycle that's actually on the server."""
    now = now or dt.datetime.now(dt.timezone.utc)
    session = session or requests.Session()
    if MODEL["source"] == "ecmwf_opendata":
        # A run is complete when its last step's file exists on the open-data server.
        last = MODEL["hours"][-1]
        for cand in _candidate_cycles(now):
            url = ECMWF_FILE.format(ymd=cand.strftime("%Y%m%d"), hh=cand.strftime("%H"), step=last)
            try:
                r = session.head(url, timeout=30, allow_redirects=True)
                if r.status_code == 200:
                    return cand
                log.info("ECMWF %s not complete yet (HTTP %s)", cand.strftime("%Y%m%d %HZ"), r.status_code)
            except requests.RequestException as e:
                log.warning("HEAD %s failed: %s", url, e)
        raise RuntimeError("No complete ECMWF run found in the last 48 h")
    for cand in _candidate_cycles(now):
        url = NOMADS_IDX.format(ymd=cand.strftime("%Y%m%d"), hh=cand.strftime("%H"))
        try:
            if session.head(url, timeout=20).status_code == 200:
                return cand
        except requests.RequestException as e:
            log.warning("HEAD %s failed: %s", url, e)
    raise RuntimeError("No GFS run found on NOMADS in the last 48 h")


def all_fetch_pairs(param_ids: list[str]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for pid in param_ids:
        pairs.update(PARAMS[pid]["fetch"])
    return pairs


def ecmwf_requests(param_ids: list[str], fhr: int) -> list[dict]:
    """Open-data requests for one forecast hour: one for pressure-level fields,
    one for single-level fields, plus tp at the previous step so 6-h precip
    can be de-accumulated (open-data tp is accumulated from t=0)."""
    pl, sfc = {}, set()
    for pid in param_ids:
        for name, lev in (PARAMS[pid].get("ecmwf") or []):
            if lev is None:
                sfc.add(name)
            else:
                pl.setdefault(lev, set()).add(name)
    reqs = []
    for lev, names in pl.items():
        reqs.append({"type": "fc", "stream": "oper", "step": fhr, "levtype": "pl",
                     "levelist": lev, "param": sorted(names)})
    if sfc:
        if "tp" in sfc and fhr == 0:
            sfc.discard("tp")
        if sfc:
            reqs.append({"type": "fc", "stream": "oper", "step": fhr, "levtype": "sfc", "param": sorted(sfc)})
        if "tp" in sfc and fhr >= 6:
            reqs.append({"type": "fc", "stream": "oper", "step": fhr - 6, "levtype": "sfc", "param": ["tp"]})
    return reqs


def download_ecmwf(run: dt.datetime, fhr: int, param_ids: list[str], dest: Path, retries: int = 4) -> Path:
    """Fetch all messages for one hour into a single GRIB file. The client uses
    the .index files to byte-range only the requested fields."""
    from ecmwf.opendata import Client
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    client = Client(source="ecmwf", model="ifs", resol="0p25")
    tmp = dest.with_suffix(".part")
    for attempt in range(retries):
        try:
            with open(tmp, "wb") as out:
                for req in ecmwf_requests(param_ids, fhr):
                    part = dest.with_suffix(f".{len(req['param'])}_{req.get('levelist', 'sfc')}_{req['step']}.grib2")
                    client.retrieve(date=run.strftime("%Y%m%d"), time=run.hour, target=str(part), **req)
                    out.write(part.read_bytes()); part.unlink()
            tmp.rename(dest)
            return dest
        except Exception as e:  # noqa: BLE001
            log.warning("ECMWF f%03d attempt %d failed: %s", fhr, attempt, e)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Failed to download ECMWF f{fhr:03d}")


def build_filter_url(run: dt.datetime, fhr: int, pairs: set[tuple[str, str]],
                     bbox: tuple[float, float, float, float]) -> str:
    """grib_filter URL for one forecast hour, all variables, one bounding box."""
    lon0, lon1, lat0, lat1 = bbox
    # grib_filter wants 0..360 longitudes
    left = lon0 % 360
    right = lon1 % 360
    q = {
        "dir": NOMADS_DIR.format(ymd=run.strftime("%Y%m%d"), hh=run.strftime("%H")),
        "file": NOMADS_FILE.format(hh=run.strftime("%H"), fhr=fhr),
        "subregion": "",
        "leftlon": f"{left:g}",
        "rightlon": f"{right:g}",
        "toplat": f"{lat1:g}",
        "bottomlat": f"{lat0:g}",
    }
    for var, lev in pairs:
        q[f"var_{var}"] = "on"
        q[f"lev_{lev}"] = "on"
    return NOMADS_FILTER + "?" + urlencode(q, safe="\\()")


def download(url: str, dest: Path, session: requests.Session, retries: int = 4) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=120)
            if r.status_code == 200 and len(r.content) > 1000:
                dest.write_bytes(r.content)
                return dest
            log.warning("GET %s -> %s (%d bytes)", url[:80], r.status_code, len(r.content))
        except requests.RequestException as e:
            log.warning("GET failed (%s): %s", attempt, e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Failed to download {url}")


class Fields(dict):
    """A dict of name -> 2D numpy array, plus shared lon/lat 1-D coordinates."""
    lon: np.ndarray
    lat: np.ndarray


def load_grib(path: Path) -> Fields:
    """Read every message in a GRIB2 file into a Fields dict keyed by cfgrib
    short name (with the level appended when the same name occurs at several
    levels, e.g. gh500 / gh850 / gh1000)."""
    import cfgrib  # imported lazily so --synthetic mode works without eccodes

    out = Fields()
    datasets = cfgrib.open_datasets(str(path), backend_kwargs={"indexpath": ""})
    lon = lat = None
    for ds in datasets:
        if lon is None:
            lon = ds["longitude"].values
            lat = ds["latitude"].values
        for name, da in ds.data_vars.items():
            arr = da.values
            if "step" in da.dims and da.sizes["step"] == 2:      # tp at fhr-6 and fhr
                out[f"{name}_prev"] = np.asarray(arr[0], dtype=float)
                arr = arr[1]
                da = da.isel(step=1)
            # cfgrib may stack multiple levels in one variable
            if "isobaricInhPa" in da.dims:
                for i, lev in enumerate(da["isobaricInhPa"].values):
                    out[f"{name}{int(lev)}"] = np.asarray(arr[i], dtype=float)
            else:
                lev = da.coords.get("isobaricInhPa")
                key = f"{name}{int(lev.values)}" if lev is not None and lev.ndim == 0 else name
                out[key] = np.asarray(arr, dtype=float)
    if lon is None:
        raise RuntimeError(f"No data in {path}")
    lon = np.where(lon > 180, lon - 360, lon)
    order = np.argsort(lon)
    lon = lon[order]
    for k in list(out):
        out[k] = out[k][:, order]
    out.lon, out.lat = lon, lat
    return normalise(out)


def normalise(f: "Fields") -> "Fields":
    """Map model-specific names/units onto the names plots.py expects
    (GFS/cfgrib conventions): prmsl [Pa], tp [mm per 6 h], absv500 [s^-1],
    pwat [mm], t2m, u10, v10, t850..."""
    if "msl" in f and "prmsl" not in f:
        f["prmsl"] = f.pop("msl")
    if "tcwv" in f and "pwat" not in f:
        f["pwat"] = f.pop("tcwv")
    if "vo500" in f and "absv500" not in f:                      # relative -> absolute vorticity
        _, LAT = np.meshgrid(f.lon, f.lat)
        f["absv500"] = f["vo500"] + 2 * 7.2921e-5 * np.sin(np.radians(LAT))
    if "tp" in f and MODEL["source"] == "ecmwf_opendata":         # m accumulated since t0 -> mm per 6 h
        prev = f.pop("tp_prev", np.zeros_like(f["tp"]))
        f["tp"] = np.clip(f["tp"] - prev, 0, None) * 1000.0
    return f


def crop(f: "Fields", bbox) -> "Fields":
    """Cut a global grid down to a bbox (lon0, lon1, lat0, lat1)."""
    lon0, lon1, lat0, lat1 = bbox
    li = np.where((f.lon >= lon0) & (f.lon <= lon1))[0]
    la = np.where((f.lat >= lat0) & (f.lat <= lat1))[0]
    if len(li) < 4 or len(la) < 4:
        return f
    out = Fields()
    out.lon, out.lat = f.lon[li], f.lat[la]
    for k, v in f.items():
        out[k] = v[np.ix_(la, li)]
    return out


def synthetic_fields(fhr: int, bbox, n=(120, 200)) -> Fields:
    """Fake but physically plausible-looking fields for testing the plots
    without network access to NOMADS."""
    lon0, lon1, lat0, lat1 = bbox
    lat = np.linspace(lat1, lat0, n[0])
    lon = np.linspace(lon0, lon1, n[1])
    LON, LAT = np.meshgrid(lon, lat)
    t = fhr / 24.0
    wave = np.sin(np.radians(LON * 3 + t * 40)) * np.cos(np.radians((LAT - 35) * 4))
    out = Fields()
    out.lon, out.lat = lon, lat
    out["gh500"] = 5700 - 12 * (LAT - 25) + 120 * wave
    out["gh850"] = 1500 - 4 * (LAT - 25) + 40 * wave
    out["gh1000"] = 100 + 20 * wave
    out["absv"] = (2e-5 + 1.5e-4 * np.clip(wave, 0, 1) ** 2 * np.sin(np.radians(LON * 6))**2)
    out["prmsl"] = 101300 - 1200 * wave + 200 * np.cos(np.radians(LAT * 5))
    out["tp"] = 15 * np.clip(-wave, 0, 1) ** 3 * (np.random.default_rng(fhr).random(LON.shape) * 0.5 + 0.5)
    out["t850"] = 293 - 0.5 * (LAT - 10) + 5 * wave
    out["u850"] = 10 * wave + 5
    out["v850"] = 8 * np.cos(np.radians(LON * 3 + t * 40))
    out["t2m"] = 303 - 0.7 * (LAT - 10) + 4 * wave
    out["u10"] = 6 * wave + 3
    out["v10"] = 5 * np.cos(np.radians(LON * 3 + t * 40))
    out["pwat"] = 45 - 0.8 * (LAT - 10) + 12 * -wave
    out["cape"] = 3000 * np.clip(-wave, 0, 1) ** 2 * np.clip((40 - LAT) / 30, 0, 1)
    return out
