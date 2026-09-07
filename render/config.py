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
        "params": None,                 # None = every product in PARAMS
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
        "params": None,                 # None = every product whose "ecmwf" spec isn't None
        "credit": "ECMWF open data (CC-BY-4.0)",
    },
}

MODEL = MODELS[os.environ.get("WX_MODEL", "gfs").lower()]
FORECAST_HOURS = MODEL["hours"]


def model_params() -> list:
    """Product ids this model can render."""
    if MODEL["params"]:
        return list(MODEL["params"])
    key = "ecmwf" if MODEL["source"] == "ecmwf_opendata" else "fetch"
    return [pid for pid, spec in PARAMS.items() if spec.get(key) is not None]


def param_hours(pid: str) -> list:
    mh = PARAMS[pid].get("max_hour")
    return [h for h in FORECAST_HOURS if mh is None or h <= mh]

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
# `fetch`: NOMADS grib_filter (VAR, LEVEL) pairs for GFS. `ecmwf`: open-data
# (param, levelist) pairs, or None if the product can't be made from open data.
# `prev`: extra fields needed from earlier forecast hours — offsets in hours,
# or "f0" for the run's hour 0. `max_hour`: render only this far (keeps the
# site under the Pages size limit); default = model's full range.
# Field names the plot functions see are normalised in fetch.py so one plot
# function serves every model.

_MSLP = [("PRMSL", "mean_sea_level")]
_E_MSLP = [("msl", None)]
_PTYPE = [("CSNOW", "surface"), ("CICEP", "surface"), ("CFRZR", "surface"), ("CRAIN", "surface")]

PARAMS = {
    # ------------------------------------------------------ precipitation ---
    "mslp_precip": {
        "name": "MSLP & 6-hr precip", "group": "Precipitation", "plot": "plot_mslp_precip",
        "fetch": _MSLP + [("APCP", "surface"), ("HGT", "1000_mb"), ("HGT", "500_mb")],
        "ecmwf": _E_MSLP + [("tp", None), ("gh", 1000), ("gh", 500)],
        "prev": {"offsets": [6], "fetch": [], "ecmwf": [("tp", None)]},
    },
    "mslp_ptype": {
        "name": "MSLP & 6-hr precip (rain / frozen)", "group": "Precipitation", "plot": "plot_mslp_ptype",
        "fetch": _MSLP + [("APCP", "surface")] + _PTYPE, "ecmwf": None, "max_hour": 240,
    },
    "refc": {
        "name": "Simulated radar (rain / frozen)", "group": "Precipitation", "plot": "plot_refc",
        "fetch": _MSLP + [("REFC", "entire_atmosphere")] + _PTYPE, "ecmwf": None, "max_hour": 240,
    },
    "precip24": {
        "name": "24-hr accumulated precip", "group": "Precipitation", "plot": "plot_precip24",
        "fetch": _MSLP + [("APCP", "surface")], "ecmwf": _E_MSLP + [("tp", None)],
        "prev": {"offsets": [24], "fetch": [("APCP", "surface")], "ecmwf": [("tp", None)]},
    },
    "precip_total": {
        "name": "Total accumulated precip", "group": "Precipitation", "plot": "plot_precip_total",
        "fetch": _MSLP + [("APCP", "surface")], "ecmwf": _E_MSLP + [("tp", None)],
    },
    "snow24": {
        "name": "24-hr snowfall (10:1)", "group": "Precipitation", "plot": "plot_snow24",
        "fetch": _MSLP + [("APCP", "surface"), ("CSNOW", "surface")], "ecmwf": None, "max_hour": 240,
        "prev": {"offsets": [6, 12, 18], "fetch": [("APCP", "surface"), ("CSNOW", "surface")], "ecmwf": []},
    },
    "snod_total": {
        "name": "Total snow-depth change", "group": "Precipitation", "plot": "plot_snod_total",
        "fetch": _MSLP + [("SNOD", "surface")], "ecmwf": None, "max_hour": 240,
        "prev": {"offsets": ["f0"], "fetch": [("SNOD", "surface")], "ecmwf": []},
    },
    "snod24": {
        "name": "24-hr snow-depth change", "group": "Precipitation", "plot": "plot_snod24",
        "fetch": _MSLP + [("SNOD", "surface")], "ecmwf": None, "max_hour": 240,
        "prev": {"offsets": [24], "fetch": [("SNOD", "surface")], "ecmwf": []},
    },
    "pwat": {
        "name": "MSLP & precipitable water", "group": "Precipitation", "plot": "plot_pwat",
        "fetch": _MSLP + [("PWAT", "entire_atmosphere_\\(considered_as_a_single_layer\\)")],
        "ecmwf": _E_MSLP + [("tcwv", None)],
    },
    "rh700_300": {
        "name": "700–300 mb relative humidity", "group": "Precipitation", "plot": "plot_rh700_300",
        "fetch": [("RH", "700_mb"), ("RH", "500_mb"), ("RH", "300_mb"), ("HGT", "500_mb")],
        "ecmwf": [("r", 700), ("r", 500), ("r", 300), ("gh", 500)], "max_hour": 240,
    },
    # ------------------------------------------------------ upper dynamics --
    "z500_vort": {
        "name": "500 mb height, vorticity & wind", "group": "Upper dynamics", "plot": "plot_z500_vort",
        "fetch": [("HGT", "500_mb"), ("ABSV", "500_mb"), ("UGRD", "500_mb"), ("VGRD", "500_mb")],
        "ecmwf": [("gh", 500), ("vo", 500), ("u", 500), ("v", 500)],
    },
    "z500_mslp": {
        "name": "500 mb height & MSLP", "group": "Upper dynamics", "plot": "plot_z500_mslp",
        "fetch": _MSLP + [("HGT", "500_mb")], "ecmwf": _E_MSLP + [("gh", 500)],
    },
    "z700_vort": {
        "name": "700 mb height, vorticity & wind", "group": "Upper dynamics", "plot": "plot_z700_vort",
        "fetch": [("HGT", "700_mb"), ("UGRD", "700_mb"), ("VGRD", "700_mb")],
        "ecmwf": [("gh", 700), ("u", 700), ("v", 700)], "max_hour": 240,
    },
    "z850_vort": {
        "name": "850 mb height, vorticity & wind", "group": "Upper dynamics", "plot": "plot_z850_vort",
        "fetch": [("HGT", "850_mb"), ("UGRD", "850_mb"), ("VGRD", "850_mb")],
        "ecmwf": [("gh", 850), ("u", 850), ("v", 850)], "max_hour": 240,
    },
    "z850_wind": {
        "name": "850 mb height & wind speed", "group": "Upper dynamics", "plot": "plot_z850_wind",
        "fetch": [("HGT", "850_mb"), ("UGRD", "850_mb"), ("VGRD", "850_mb")],
        "ecmwf": [("gh", 850), ("u", 850), ("v", 850)],
    },
    "wind250": {
        "name": "250 mb wind & height", "group": "Upper dynamics", "plot": "plot_wind250",
        "fetch": [("HGT", "250_mb"), ("UGRD", "250_mb"), ("VGRD", "250_mb")],
        "ecmwf": [("gh", 250), ("u", 250), ("v", 250)],
    },
    "pv2": {
        "name": "2 PVU pressure & wind", "group": "Upper dynamics", "plot": "plot_pv2",
        "fetch": [("PRES", "PV=2e-06_(Km^2/kg/s)_surface"), ("UGRD", "PV=2e-06_(Km^2/kg/s)_surface"),
                  ("VGRD", "PV=2e-06_(Km^2/kg/s)_surface")],
        "ecmwf": None, "max_hour": 240,
    },
    "sim_ir": {
        "name": "Simulated IR satellite", "group": "Upper dynamics", "plot": "plot_sim_ir",
        "fetch": _MSLP + [("SBT124", "top_of_atmosphere")], "ecmwf": None, "max_hour": 240,
    },
    # ------------------------------------------------------ thermodynamics --
    "t2m": {
        "name": "2 m temperature", "group": "Thermodynamics", "plot": "plot_t2m",
        "fetch": _MSLP + [("TMP", "2_m_above_ground")], "ecmwf": _E_MSLP + [("2t", None)],
    },
    "t850_wind": {
        "name": "850 mb temperature, wind & MSLP", "group": "Thermodynamics", "plot": "plot_t850_wind",
        "fetch": _MSLP + [("TMP", "850_mb"), ("UGRD", "850_mb"), ("VGRD", "850_mb"), ("HGT", "850_mb")],
        "ecmwf": _E_MSLP + [("t", 850), ("u", 850), ("v", 850), ("gh", 850)],
    },
    "t700_wind": {
        "name": "700 mb temperature, wind & MSLP", "group": "Thermodynamics", "plot": "plot_t700_wind",
        "fetch": _MSLP + [("TMP", "700_mb"), ("UGRD", "700_mb"), ("VGRD", "700_mb"), ("HGT", "700_mb")],
        "ecmwf": _E_MSLP + [("t", 700), ("u", 700), ("v", 700), ("gh", 700)], "max_hour": 240,
    },
    "cape": {
        "name": "SBCAPE & wind crossovers", "group": "Thermodynamics", "plot": "plot_cape",
        "fetch": [("CAPE", "surface"), ("UGRD", "850_mb"), ("VGRD", "850_mb"), ("UGRD", "500_mb"), ("VGRD", "500_mb")],
        "ecmwf": None, "max_hour": 240,
    },
    # ------------------------------------------------------ surface ---------
    "wind10m": {
        "name": "MSLP & 10 m wind", "group": "Surface", "plot": "plot_wind10m",
        "fetch": _MSLP + [("UGRD", "10_m_above_ground"), ("VGRD", "10_m_above_ground")],
        "ecmwf": _E_MSLP + [("10u", None), ("10v", None)],
    },
    # ------------------------------------------------------ diagnostics -----
    "fgen700": {
        "name": "700 mb temp advection & frontogenesis", "group": "Diagnostics", "plot": "plot_fgen700",
        "fetch": [("TMP", "700_mb"), ("UGRD", "700_mb"), ("VGRD", "700_mb"), ("HGT", "700_mb")],
        "ecmwf": [("t", 700), ("u", 700), ("v", 700), ("gh", 700)], "max_hour": 240,
    },
    "fgen850": {
        "name": "850 mb temp advection & frontogenesis", "group": "Diagnostics", "plot": "plot_fgen850",
        "fetch": [("TMP", "850_mb"), ("UGRD", "850_mb"), ("VGRD", "850_mb"), ("HGT", "850_mb")],
        "ecmwf": [("t", 850), ("u", 850), ("v", 850), ("gh", 850)], "max_hour": 240,
    },
    "okubo850": {
        "name": "850 mb Okubo-Weiss & dilatation axes", "group": "Diagnostics", "plot": "plot_okubo850",
        "fetch": [("HGT", "850_mb"), ("UGRD", "850_mb"), ("VGRD", "850_mb")],
        "ecmwf": [("gh", 850), ("u", 850), ("v", 850)], "max_hour": 240,
    },
}

# Output image size (inches × dpi)
FIG_SIZE = (12, 8)
DPI = 100
