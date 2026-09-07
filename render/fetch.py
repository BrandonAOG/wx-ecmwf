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
        # A run is complete when its last step's file exists on the open-data
        # server. 06/18Z runs are published to a shorter range, so probe the
        # possible final steps from longest to shortest.
        for cand in _candidate_cycles(now):
            if run_max_hour(cand, session) is not None:
                return cand
            log.info("ECMWF %s not complete yet", cand.strftime("%Y%m%d %HZ"))
        raise RuntimeError("No complete ECMWF run found in the last 48 h")
    for cand in _candidate_cycles(now):
        url = NOMADS_IDX.format(ymd=cand.strftime("%Y%m%d"), hh=cand.strftime("%H"))
        try:
            if session.head(url, timeout=20).status_code == 200:
                return cand
        except requests.RequestException as e:
            log.warning("HEAD %s failed: %s", url, e)
    raise RuntimeError("No GFS run found on NOMADS in the last 48 h")


def run_max_hour(run: dt.datetime, session: requests.Session | None = None) -> int | None:
    """Furthest forecast hour available for this run, or None if the run isn't
    complete at any known range. GFS is always the full range."""
    if MODEL["source"] != "ecmwf_opendata":
        return MODEL["hours"][-1]
    session = session or requests.Session()
    for last in MODEL.get("probe_max_hours", [MODEL["hours"][-1]]):
        url = ECMWF_FILE.format(ymd=run.strftime("%Y%m%d"), hh=run.strftime("%H"), step=last)
        try:
            if session.head(url, timeout=30, allow_redirects=True).status_code == 200:
                return last
        except requests.RequestException as e:
            log.warning("HEAD %s failed: %s", url, e)
    return None


def all_fetch_pairs(param_ids: list[str]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for pid in param_ids:
        pairs.update(PARAMS[pid]["fetch"])
    return pairs


def ecmwf_pairs(param_ids: list[str]) -> set[tuple]:
    pairs: set[tuple] = set()
    for pid in param_ids:
        pairs.update(PARAMS[pid].get("ecmwf") or [])
    return pairs


def prev_steps(param_ids: list[str]) -> dict:
    """{offset: {"fetch": set(pairs), "ecmwf": set(pairs)}} merged across products.
    offset is an int (hours back) or "f0"."""
    out: dict = {}
    for pid in param_ids:
        spec = PARAMS[pid].get("prev")
        if not spec:
            continue
        for off in spec["offsets"]:
            slot = out.setdefault(off, {"fetch": set(), "ecmwf": set()})
            slot["fetch"].update(spec.get("fetch", []))
            slot["ecmwf"].update(spec.get("ecmwf", []))
    return out


def step_for(fhr: int, offset) -> int | None:
    """Forecast step to fetch for a previous-step offset, or None if n/a."""
    step = 0 if offset == "f0" else fhr - int(offset)
    return step if 0 <= step < fhr else None


def ecmwf_requests(pairs: set[tuple], step: int) -> list[dict]:
    """Open-data requests for one step: one per pressure level, one for
    single-level fields. tp doesn't exist at step 0."""
    pl, sfc = {}, set()
    for name, lev in pairs:
        if lev is None:
            sfc.add(name)
        else:
            pl.setdefault(lev, set()).add(name)
    if step == 0:
        sfc.discard("tp")
    reqs = [{"type": "fc", "stream": "oper", "step": step, "levtype": "pl", "levelist": lev, "param": sorted(n)}
            for lev, n in pl.items()]
    if sfc:
        reqs.append({"type": "fc", "stream": "oper", "step": step, "levtype": "sfc", "param": sorted(sfc)})
    return reqs


def download_ecmwf(run: dt.datetime, step: int, pairs: set[tuple], dest: Path, retries: int = 4) -> Path:
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
            reqs = ecmwf_requests(pairs, step)
            if not reqs:
                raise RuntimeError("nothing to fetch")
            with open(tmp, "wb") as out:
                for req in reqs:
                    part = dest.with_suffix(f".{len(req['param'])}_{req.get('levelist', 'sfc')}_{req['step']}.grib2")
                    client.retrieve(date=run.strftime("%Y%m%d"), time=run.hour, target=str(part), **req)
                    out.write(part.read_bytes()); part.unlink()
            tmp.rename(dest)
            return dest
        except Exception as e:  # noqa: BLE001
            log.warning("ECMWF step %d attempt %d failed: %s", step, attempt, e)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Failed to download ECMWF step {step}")


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


BACKOFF = [5, 10, 20, 40, 60, 90]


def download(url: str, dest: Path, session: requests.Session, retries: int = 6) -> Path:
    """NOMADS returns 500/503 freely when busy; back off progressively."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=120)
            if r.status_code == 200 and len(r.content) > 1000:
                dest.write_bytes(r.content)
                return dest
            log.warning("GET %s -> %s (%d bytes), attempt %d", url[:80], r.status_code, len(r.content), attempt + 1)
        except requests.RequestException as e:
            log.warning("GET failed (attempt %d): %s", attempt + 1, e)
        time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    raise RuntimeError(f"Failed to download {url}")


def _group_of(lev: str) -> str:
    if lev.endswith("_mb"):
        return "iso"
    if lev.startswith("PV"):
        return "pv"
    if lev.startswith("top_of_atmosphere"):
        return "toa"
    return "sfc"


def download_grouped(run: dt.datetime, fhr: int, pairs: set, bbox, dest: Path,
                     session: requests.Session, retries: int = 4) -> Path:
    """GFS: NOMADS grib_filter chokes on one huge var×level request, so fetch in
    groups (isobaric / surface-ish / PV / top-of-atmosphere) and concatenate.
    A failing group is logged and skipped; the frame still renders what it can."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    groups: dict[str, set] = {}
    for var, lev in pairs:
        groups.setdefault(_group_of(lev), set()).add((var, lev))
    parts = []
    for name, grp in sorted(groups.items()):
        part = dest.with_suffix(f".{name}.grb2")
        try:
            download(build_filter_url(run, fhr, grp, bbox), part, session, retries=retries)
            parts.append(part)
        except RuntimeError as e:
            log.warning("f%03d group %s failed (%s): %s", fhr, name, sorted(grp)[:3], e)
    if not parts:
        raise RuntimeError(f"All download groups failed for f{fhr:03d}")
    with open(dest, "wb") as out:
        for part in parts:
            out.write(part.read_bytes()); part.unlink()
    return dest


class Fields(dict):
    """A dict of name -> 2D numpy array, plus shared lon/lat 1-D coordinates."""
    lon: np.ndarray
    lat: np.ndarray


# Names eccodes gives GFS/ECMWF fields at fixed heights -> the names plots.py uses
HEIGHT_NAMES = {"2t": "t2m", "10u": "u10", "10v": "v10", "2r": "rh2m", "2d": "d2m", "10si": "si10"}


def load_grib(path: Path, tag: str = "") -> Fields:
    """Read every message in a GRIB file into Fields keyed so that plots can
    tell fields apart unambiguously:

        t850, u250, gh500, r700     isobaric: shortName + level (hPa)
        t2m, u10, v10               fixed heights (renamed via HEIGHT_NAMES)
        pres_pv, u_pv, v_pv         2-PVU surface
        tp_acc                      accumulation from t=0
        tp_6                        6-hour bucket (GFS)
        refc, csnow, cape, ...      anything else: shortName
        unknown ids                 p<paramId>

    `tag` is appended to every key (e.g. "_m24" for fields fetched from
    forecast hour fhr-24) so previous-step fields can live alongside."""
    import eccodes as ec
    out = Fields()
    lat = lon = None
    with open(path, "rb") as fh:
        while True:
            h = ec.codes_grib_new_from_file(fh)
            if h is None:
                break
            try:
                name = ec.codes_get(h, "shortName")
                if name in ("unknown", "~", ""):
                    name = f"p{ec.codes_get(h, 'paramId')}"
                tol = ec.codes_get(h, "typeOfLevel")
                lev = ec.codes_get(h, "level")
                step_type = ec.codes_get(h, "stepType")
                if tol == "isobaricInhPa":
                    key = f"{name}{int(lev)}"
                elif tol == "potentialVorticity":
                    key = f"{name}_pv"
                elif tol in ("heightAboveGround", "heightAboveGroundLayer"):
                    key = HEIGHT_NAMES.get(name, f"{name}{int(lev)}m" if name in ("t", "u", "v", "r", "q") else name)
                elif tol == "surface" and name in ("t", "u", "v", "q", "r"):
                    key = f"{name}_sfc"
                else:
                    key = name
                if step_type == "accum":
                    start = int(ec.codes_get(h, "startStep")); endstep = int(ec.codes_get(h, "endStep"))
                    key += "_acc" if start == 0 else f"_{endstep - start}"
                key += tag
                ni, nj = ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")
                vals = ec.codes_get_values(h).reshape(nj, ni)
                if lat is None:
                    lats = ec.codes_get_array(h, "latitudes").reshape(nj, ni)
                    lons = ec.codes_get_array(h, "longitudes").reshape(nj, ni)
                    lat, lon = lats[:, 0].copy(), lons[0, :].copy()
                missing = ec.codes_get(h, "missingValue")
                vals = np.where(vals == missing, np.nan, vals)
                if key not in out:                 # first occurrence wins (e.g. duplicate tp records)
                    out[key] = np.asarray(vals, dtype=float)
            finally:
                ec.codes_release(h)
    if lat is None:
        raise RuntimeError(f"No data in {path}")
    lon = np.where(lon > 180, lon - 360, lon)
    order = np.argsort(lon)
    lon = lon[order]
    for k in list(out):
        out[k] = out[k][:, order]
    if lat[0] < lat[-1]:                           # plots assume north-to-south rows
        lat = lat[::-1]
        for k in list(out):
            out[k] = out[k][::-1, :]
    out.lon, out.lat = lon, lat
    return out


def merge(a: Fields, b: Fields) -> Fields:
    """Merge previous-step fields (already tagged) into the main Fields."""
    for k, v in b.items():
        if v.shape == next(iter(a.values())).shape:
            a[k] = v
    return a


def normalise(f: "Fields", fhr: int = 0) -> "Fields":
    """Map model-specific names/units onto what plots.py expects:
    prmsl [Pa], tp_6 [mm/6 h], tp_acc [mm since t0], absv500 [s^-1], pwat [mm],
    t2m, u10, v10, t850 ... GFS is the reference convention."""
    ecmwf = MODEL["source"] == "ecmwf_opendata"
    if "msl" in f and "prmsl" not in f:
        f["prmsl"] = f.pop("msl")
    if "tcwv" in f and "pwat" not in f:
        f["pwat"] = f.pop("tcwv")
    if "vo500" in f and "absv500" not in f:                      # relative -> absolute vorticity
        _, LAT = np.meshgrid(f.lon, f.lat)
        f["absv500"] = f["vo500"] + 2 * 7.2921e-5 * np.sin(np.radians(LAT))
    if ecmwf:                                                    # ECMWF tp is metres accumulated since t0
        for k in [k for k in f if k.startswith("tp_acc")]:
            f[k] = f[k] * 1000.0
        if "tp_acc" in f:
            prev = f.get("tp_acc_m6", np.zeros_like(f["tp_acc"]))
            f["tp_6"] = np.clip(f["tp_acc"] - prev, 0, None)
    # GFS: at f006 the only bucket is 0-6, keyed tp_acc. Same for tagged previous steps.
    for tag in ("", "_m6", "_m12", "_m18"):
        if f"tp_6{tag}" not in f and f"tp_acc{tag}" in f:
            f[f"tp_6{tag}"] = f[f"tp_acc{tag}"]
    for k in [k for k in f if k.startswith("tp_acc_m")]:         # 24-h totals
        pass
    if "tp_acc" in f and "tp_acc_m24" in f:
        f["tp_24"] = np.clip(f["tp_acc"] - f["tp_acc_m24"], 0, None)
    elif "tp_acc" in f and fhr <= 24:
        f["tp_24"] = f["tp_acc"]
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


def synthetic_fields(fhr: int, bbox, n=(120, 200), tags=("", "_m6", "_m12", "_m18", "_m24", "_f0")) -> Fields:
    """Fake but plausible-looking fields (with the same key scheme as
    load_grib) for testing the plots without network access."""
    lon0, lon1, lat0, lat1 = bbox
    lat = np.linspace(lat1, lat0, n[0])
    lon = np.linspace(lon0, lon1, n[1])
    LON, LAT = np.meshgrid(lon, lat)
    rng = np.random.default_rng(fhr)
    out = Fields()
    out.lon, out.lat = lon, lat
    for tag in tags:
        t = (fhr - {"": 0, "_m6": 6, "_m12": 12, "_m18": 18, "_m24": 24, "_f0": fhr}[tag]) / 24.0
        wave = np.sin(np.radians(LON * 3 + t * 40)) * np.cos(np.radians((LAT - 35) * 4))
        cold = np.clip((LAT - 30) / 25, 0, 1)
        f = {
            "gh500": 5700 - 12 * (LAT - 25) + 120 * wave, "gh700": 3000 - 7 * (LAT - 25) + 70 * wave,
            "gh850": 1500 - 4 * (LAT - 25) + 40 * wave, "gh1000": 100 + 20 * wave, "gh250": 10600 - 22 * (LAT - 25) + 200 * wave, "gh200": 12000 - 24 * (LAT - 25) + 220 * wave,
            "absv500": 2e-5 + 1.5e-4 * np.clip(wave, 0, 1) ** 2 * np.sin(np.radians(LON * 6)) ** 2,
            "u500": 25 * wave + 15, "v500": 12 * np.cos(np.radians(LON * 3 + t * 40)),
            "u700": 15 * wave + 8, "v700": 9 * np.cos(np.radians(LON * 3 + t * 40)),
            "u850": 10 * wave + 5, "v850": 8 * np.cos(np.radians(LON * 3 + t * 40)),
            "u250": 45 * wave + 25 + 20 * np.exp(-((LAT - 40) / 6) ** 2), "v250": 20 * np.cos(np.radians(LON * 3 + t * 40)),
            "u200": 50 * wave + 28 + 22 * np.exp(-((LAT - 40) / 6) ** 2), "v200": 22 * np.cos(np.radians(LON * 3 + t * 40)),
            "u300": 35 * wave + 20 + 15 * np.exp(-((LAT - 40) / 6) ** 2), "v300": 16 * np.cos(np.radians(LON * 3 + t * 40)),
            "prmsl": 101300 - 1200 * wave + 200 * np.cos(np.radians(LAT * 5)),
            "tp_6": 15 * np.clip(-wave, 0, 1) ** 3 * (rng.random(LON.shape) * 0.5 + 0.5),
            "t850": 293 - 0.5 * (LAT - 10) + 5 * wave, "t700": 283 - 0.5 * (LAT - 10) + 5 * wave,
            "t2m": 303 - 0.7 * (LAT - 10) + 4 * wave, "u10": 6 * wave + 3, "v10": 5 * np.cos(np.radians(LON * 3 + t * 40)),
            "pwat": 45 - 0.8 * (LAT - 10) + 12 * -wave, "cape": 3000 * np.clip(-wave, 0, 1) ** 2 * np.clip((40 - LAT) / 30, 0, 1),
            "r700": np.clip(60 - 40 * wave, 0, 100), "r500": np.clip(50 - 40 * wave, 0, 100), "r300": np.clip(40 - 40 * wave, 0, 100),
            "refc": np.clip(55 * np.clip(-wave, 0, 1) ** 1.5 * (rng.random(LON.shape) * 0.6 + 0.4) - 5, -10, 70),
            "csnow": (cold * np.clip(-wave, 0, 1) > 0.45).astype(float), "cicep": np.zeros_like(LAT),
            "cfrzr": ((cold * np.clip(-wave, 0, 1) > 0.38) & (cold * np.clip(-wave, 0, 1) <= 0.45)).astype(float),
            "pres_pv": 25000 + 20000 * cold + 15000 * wave, "u_pv": 40 * wave + 30, "v_pv": 20 * np.cos(np.radians(LON * 3 + t * 40)),
            "sbt124": 290 - 70 * np.clip(-wave, 0, 1) ** 2 - 10 * cold, "snod": 0.05 * cold * (1 + t) * np.clip(-wave, 0, 1),
            "t_sfc": 303 - 0.35 * (LAT - 10) + 1.5 * wave, "land": (np.sin(np.radians(LON * 2)) * np.cos(np.radians(LAT * 3)) > 0.4).astype(float),
        }
        f["crain"] = ((f["tp_6"] > 0.2) & (f["csnow"] == 0) & (f["cfrzr"] == 0)).astype(float)
        f["tp_acc"] = f["tp_6"] * max(1, (fhr / 6) * 0.6)
        for k, v in f.items():
            out[k + tag] = v
    return out
