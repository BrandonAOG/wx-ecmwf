"""
Everything you'd want to tweak lives here: which model this repo renders,
which regions and parameters, forecast hours, and how many runs to keep.

One repo renders one model. Pick it with the WX_MODEL environment variable
(set in the GitHub Actions workflow); default is gfs.
"""
import os

SITE_NAME = "WxModels"

# --------------------------------------------------------------- models -----

MODELS = {
    "gfs": {
        "id": "gfs",
        "name": "GFS",
        "resolution": "0.25°",
        "source": "nomads",
        "cycles": [0, 6, 12, 18],
        "min_age_hours": 3.5,          # how long after cycle time f000..f384 are complete
        # 3-hourly to 240 h, 12-hourly to 384 h
        "hours": list(range(0, 241, 6)) + list(range(252, 361, 12)),
        "params": ["z500_vort", "mslp_precip", "t850_wind", "t2m", "wind10m", "pwat", "cape"],
        "credit": "NOAA/NCEP GFS via NOMADS",
    },
    "ecmwf": {
        "id": "ecmwf",
        "name": "ECMWF",
        "resolution": "0.25°",
        "source": "ecmwf_opendata",
        "cycles": [0, 12],             # 06/18 only run to 90 h; skip them for now
        "min_age_hours": 8,
        # open data: 3-hourly to 144 h, 6-hourly to 240 h
        "hours": list(range(0, 241, 6)),
        "params": ["z500_vort", "mslp_precip", "t850_wind", "t2m", "wind10m", "pwat"],  # no CAPE in open data
        "credit": "ECMWF open data (CC-BY-4.0)",
    },
}

MODEL = MODELS[os.environ.get("WX_MODEL", "gfs").lower()]
FORECAST_HOURS = MODEL["hours"]

# How many runs to keep. Only meaningful when images persist between jobs
# (R2 storage); a plain Pages deploy only ever contains the run just rendered.
KEEP_RUNS = 8

# NOMADS (GFS)
NOMADS_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
NOMADS_DIR = "/gfs.{ymd}/{hh}/atmos"
NOMADS_FILE = "gfs.t{hh}z.pgrb2.0p25.f{fhr:03d}"
NOMADS_IDX = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f000.idx"

# -------------------------------------------------------------- regions -----
# lon/lat bounding box (lon in -180..180). Padding is added on fetch so
# contours don't get clipped at the frame edge.
REGIONS = {
    "conus": {"name": "United States", "bbox": (-126, -66, 23, 50)},
    "natl":  {"name": "North Atlantic", "bbox": (-100, -10, 5, 45)},
    "epac":  {"name": "East Pacific",   "bbox": (-150, -85, 3, 35)},
    "namer": {"name": "North America",  "bbox": (-140, -50, 12, 62)},
    "gulf":  {"name": "Gulf & Florida",  "bbox": (-100, -74, 16, 33)},
    "carib": {"name": "Caribbean",       "bbox": (-92, -55, 7, 28)},
}

# ----------------------------------------------------------- parameters -----
# `fetch` lists NOMADS grib_filter var/lev pairs (GFS). `ecmwf` lists open-data
# (param, levelist) pairs. `plot` is the function in plots.py. Field names the
# plot functions see are normalised in fetch.normalise(), so one plot function
# serves every model.
PARAMS = {
    "z500_vort": {
        "name": "500 hPa height & vorticity", "group": "Upper air", "plot": "plot_z500_vort",
        "fetch": [("HGT", "500_mb"), ("ABSV", "500_mb")],
        "ecmwf": [("gh", 500), ("vo", 500)],
    },
    "mslp_precip": {
        "name": "MSLP & 6-hr precipitation", "group": "Surface", "plot": "plot_mslp_precip",
        "fetch": [("PRMSL", "mean_sea_level"), ("APCP", "surface"), ("HGT", "1000_mb"), ("HGT", "500_mb")],
        "ecmwf": [("msl", None), ("tp", None), ("gh", 1000), ("gh", 500)],
    },
    "t850_wind": {
        "name": "850 hPa temperature & wind", "group": "Upper air", "plot": "plot_t850_wind",
        "fetch": [("TMP", "850_mb"), ("UGRD", "850_mb"), ("VGRD", "850_mb"), ("HGT", "850_mb")],
        "ecmwf": [("t", 850), ("u", 850), ("v", 850), ("gh", 850)],
    },
    "t2m": {
        "name": "2 m temperature", "group": "Surface", "plot": "plot_t2m",
        "fetch": [("TMP", "2_m_above_ground"), ("PRMSL", "mean_sea_level")],
        "ecmwf": [("2t", None), ("msl", None)],
    },
    "wind10m": {
        "name": "10 m wind & MSLP", "group": "Surface", "plot": "plot_wind10m",
        "fetch": [("UGRD", "10_m_above_ground"), ("VGRD", "10_m_above_ground"), ("PRMSL", "mean_sea_level")],
        "ecmwf": [("10u", None), ("10v", None), ("msl", None)],
    },
    "pwat": {
        "name": "Precipitable water", "group": "Moisture", "plot": "plot_pwat",
        "fetch": [("PWAT", "entire_atmosphere_\\(considered_as_a_single_layer\\)"), ("PRMSL", "mean_sea_level")],
        "ecmwf": [("tcwv", None), ("msl", None)],
    },
    "cape": {
        "name": "Surface CAPE", "group": "Severe", "plot": "plot_cape",
        "fetch": [("CAPE", "surface"), ("UGRD", "10_m_above_ground"), ("VGRD", "10_m_above_ground")],
        "ecmwf": None,
    },
}

# Output image size (inches × dpi)
FIG_SIZE = (12, 8)
DPI = 100
