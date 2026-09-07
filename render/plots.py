"""
One plotting function per parameter. Each takes (fields, meta) and returns a
matplotlib Figure. `meta` carries region bbox, run time, forecast hour, etc.

Style notes: Lambert Conformal for mid-latitude regions, Plate Carrée near the
tropics; heights in dam, MSLP in hPa every 4, temperatures in °F/°C as labelled.
"""
from __future__ import annotations

import datetime as dt
import logging

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib
matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.patheffects
import matplotlib.pyplot as plt
import matplotlib.ticker
import numpy as np
from scipy.ndimage import gaussian_filter

from config import DPI, FIG_SIZE, MODEL, SITE_NAME

log = logging.getLogger("plots")
PC = ccrs.PlateCarree()


# ---------------------------------------------------------------- helpers ---

def pick(f, *keys):
    for k in keys:
        if k in f:
            return f[k]
    raise KeyError(f"none of {keys} in fields {list(f)}")


def projection_for(bbox):
    lon0, lon1, lat0, lat1 = bbox
    if lat0 >= 0 and lat1 <= 40:      # tropics: keep it flat
        return PC
    return ccrs.LambertConformal(central_longitude=(lon0 + lon1) / 2,
                                 central_latitude=(lat0 + lat1) / 2,
                                 standard_parallels=(lat0 + 5, lat1 - 5))


def new_map(meta):
    fig = plt.figure(figsize=FIG_SIZE, dpi=DPI)
    proj = projection_for(meta["bbox"])
    ax = fig.add_axes([0.01, 0.08, 0.98, 0.825], projection=proj)
    ax.set_extent(meta["bbox"], crs=PC)
    return fig, ax


def add_basemap(ax):
    """Coastlines/borders/states. Wrapped so a missing Natural Earth download
    (no network) degrades to a plain map instead of crashing the whole run."""
    feats = [(cfeature.COASTLINE, 0.8, "#222"), (cfeature.BORDERS, 0.6, "#333"),
             (cfeature.STATES, 0.4, "#555")]
    for feat, lw, col in feats:
        f50 = feat.with_scale("50m")
        try:
            next(iter(f50.geometries()))      # forces the (cached) download now
        except Exception as e:  # noqa: BLE001
            log.warning("basemap layer unavailable (%s); skipping", str(e)[:60])
            continue
        ax.add_feature(f50, lw=lw, edgecolor=col, facecolor="none", zorder=5)
    gl = ax.gridlines(draw_labels=False, lw=0.3, color="#888", alpha=0.5, linestyle=":")
    gl.xlocator = matplotlib.ticker.MultipleLocator(10)
    gl.ylocator = matplotlib.ticker.MultipleLocator(10)


def title(fig, ax, meta, left, right_units=""):
    valid = meta["run"] + dt.timedelta(hours=meta["fhr"])
    fig.text(0.01, 0.972, f"{MODEL['name']} {MODEL['resolution']}  |  {left}",
             fontsize=13, fontweight="bold", ha="left", va="center")
    fig.text(0.01, 0.932,
             f"Init: {meta['run']:%a %d %b %Y %HZ}     Forecast hour {meta['fhr']:03d}     "
             f"Valid: {valid:%a %d %b %Y %HZ}",
             fontsize=10.5, ha="left", va="center", color="#333")
    fig.text(0.01, 0.015, f"{SITE_NAME}  ·  data: {MODEL.get('credit', '')}  ·  {meta['region_name']}",
             fontsize=8.5, ha="left", va="center", color="#666")
    if right_units:
        fig.text(0.99, 0.015, right_units, fontsize=8.5, ha="right", va="center", color="#666")


def colorbar(fig, mappable, label, ticks=None):
    cax = fig.add_axes([0.25, 0.045, 0.5, 0.014])
    cb = fig.colorbar(mappable, cax=cax, orientation="horizontal", ticks=ticks)
    cb.ax.tick_params(labelsize=8, length=2, pad=1)
    cb.set_label(label, fontsize=8.5, labelpad=2)
    return cb


def contour_labeled(ax, lon, lat, data, levels, color, lw, fmt="%d", linestyles="solid", zorder=6):
    cs = ax.contour(lon, lat, data, levels=levels, colors=color, linewidths=lw,
                    linestyles=linestyles, transform=PC, zorder=zorder)
    ax.clabel(cs, fmt=fmt, fontsize=7, inline=True, inline_spacing=2)
    return cs


def hilo(ax, lon, lat, mslp_hpa, size=40):
    """Mark pressure highs and lows."""
    from scipy.ndimage import maximum_filter, minimum_filter
    mx = maximum_filter(mslp_hpa, size)
    mn = minimum_filter(mslp_hpa, size)
    LON, LAT = np.meshgrid(lon, lat)
    for mask, sym, col in [(mslp_hpa == mx, "H", "#1848a8"), (mslp_hpa == mn, "L", "#c81e1e")]:
        ys, xs = np.where(mask)
        for y, x in zip(ys, xs):
            if 2 < y < len(lat) - 3 and 2 < x < len(lon) - 3:
                ax.text(LON[y, x], LAT[y, x], sym, color=col, fontsize=15, fontweight="bold",
                        ha="center", va="center", transform=PC, zorder=8,
                        path_effects=[matplotlib.patheffects.withStroke(linewidth=2, foreground="w")])
                ax.text(LON[y, x], LAT[y, x] - 1.2, f"{mslp_hpa[y, x]:.0f}", color=col, fontsize=7,
                        ha="center", va="top", transform=PC, zorder=8)


def barbs(ax, lon, lat, u, v, every=None, **kw):
    if every is None:
        every = max(1, len(lon) // 28)
    ax.barbs(lon[::every], lat[::every], u[::every, ::every], v[::every, ::every],
             length=5, linewidth=0.5, transform=PC, zorder=7, **kw)


def smooth(a, s=1.5):
    return gaussian_filter(a, s)


# ------------------------------------------------------------- parameters ---

def plot_z500_vort(f, meta):
    fig, ax = new_map(meta)
    lon, lat = f.lon, f.lat
    z = smooth(pick(f, "gh500") / 10)           # dam
    vort = pick(f, "absv500", "absv") * 1e5     # 1e-5 s^-1
    levels = np.arange(8, 60, 2)
    cmap = plt.get_cmap("YlOrRd")
    cf = ax.contourf(lon, lat, vort, levels=levels, cmap=cmap, extend="max", transform=PC, zorder=2)
    contour_labeled(ax, lon, lat, z, np.arange(480, 620, 6), "black", 1.0)
    add_basemap(ax)
    colorbar(fig, cf, "Absolute vorticity (10⁻⁵ s⁻¹)", ticks=levels[::2])
    title(fig, ax, meta, "500 hPa height (dam) & absolute vorticity")
    return fig


def _precip_cmap():
    bounds = [0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8]
    colors = ["#c9e8f5", "#8fd1ee", "#4fb2e0", "#1d81c8", "#1c4fa5", "#3ec24d", "#1e8f2a",
              "#f7e530", "#f7a020", "#ea3b1a", "#b41313", "#7a0f5e"]
    return mcolors.ListedColormap(colors), mcolors.BoundaryNorm(bounds, len(colors)), bounds


def plot_mslp_precip(f, meta):
    fig, ax = new_map(meta)
    lon, lat = f.lon, f.lat
    mslp = smooth(pick(f, "prmsl") / 100)
    cmap, norm, bounds = _precip_cmap()
    if "tp" in f:
        precip_in = pick(f, "tp") / 25.4
        cf = ax.pcolormesh(lon, lat, np.ma.masked_less(precip_in, 0.01), cmap=cmap, norm=norm,
                           transform=PC, zorder=2, shading="auto")
        colorbar(fig, cf, "6-hr precipitation (in)", ticks=bounds)
    else:
        ax.text(0.5, 0.5, "No accumulated precipitation at hour 000", transform=ax.transAxes,
                ha="center", fontsize=11, color="#666", zorder=9)
    if "gh1000" in f and "gh500" in f:
        thk = smooth((pick(f, "gh500") - pick(f, "gh1000")) / 10)
        ax.contour(lon, lat, thk, levels=np.arange(480, 540, 6), colors="#1a4fd6", linewidths=0.8,
                   linestyles="dashed", transform=PC, zorder=5)
        ax.contour(lon, lat, thk, levels=[540], colors="#1a4fd6", linewidths=1.6,
                   linestyles="dashed", transform=PC, zorder=5)
        ax.contour(lon, lat, thk, levels=np.arange(546, 600, 6), colors="#d62828", linewidths=0.8,
                   linestyles="dashed", transform=PC, zorder=5)
    contour_labeled(ax, lon, lat, mslp, np.arange(940, 1060, 4), "black", 1.0)
    hilo(ax, lon, lat, mslp)
    add_basemap(ax)
    title(fig, ax, meta, "MSLP (hPa), 1000–500 hPa thickness (dam) & 6-hr precipitation")
    return fig


def plot_t850_wind(f, meta):
    fig, ax = new_map(meta)
    lon, lat = f.lon, f.lat
    t = pick(f, "t850") - 273.15
    levels = np.arange(-30, 36, 2)
    cf = ax.contourf(lon, lat, t, levels=levels, cmap="RdYlBu_r", extend="both", transform=PC, zorder=2)
    ax.contour(lon, lat, t, levels=[0], colors="k", linewidths=1.2, linestyles="dashed", transform=PC, zorder=4)
    if "gh850" in f:
        contour_labeled(ax, lon, lat, smooth(pick(f, "gh850") / 10), np.arange(100, 180, 3), "black", 0.8)
    barbs(ax, lon, lat, pick(f, "u850") * 1.944, pick(f, "v850") * 1.944, color="#222")
    add_basemap(ax)
    colorbar(fig, cf, "850 hPa temperature (°C)", ticks=levels[::3])
    title(fig, ax, meta, "850 hPa temperature (°C), height (dam) & wind (kt)")
    return fig


def plot_t2m(f, meta):
    fig, ax = new_map(meta)
    lon, lat = f.lon, f.lat
    tf = (pick(f, "t2m") - 273.15) * 9 / 5 + 32
    levels = np.arange(-30, 121, 5)
    cf = ax.contourf(lon, lat, tf, levels=levels, cmap="turbo", extend="both", transform=PC, zorder=2)
    ax.contour(lon, lat, tf, levels=[32], colors="k", linewidths=1.0, linestyles="dashed", transform=PC, zorder=4)
    if "prmsl" in f:
        contour_labeled(ax, lon, lat, smooth(pick(f, "prmsl") / 100), np.arange(940, 1060, 4), "#333", 0.6)
    add_basemap(ax)
    colorbar(fig, cf, "2 m temperature (°F)", ticks=levels[::2])
    title(fig, ax, meta, "2 m temperature (°F) & MSLP (hPa)")
    return fig


def plot_wind10m(f, meta):
    fig, ax = new_map(meta)
    lon, lat = f.lon, f.lat
    u = pick(f, "u10") * 1.944
    v = pick(f, "v10") * 1.944
    spd = np.hypot(u, v)
    bounds = [10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 100]
    colors = ["#cfe8ff", "#9dcbff", "#5aa7f5", "#2e7ad8", "#2bb673", "#7ed321", "#f8e71c",
              "#f5a623", "#f05a28", "#d0021b", "#9b0c3d", "#5e0a5e"]
    cmap = mcolors.ListedColormap(colors)
    norm = mcolors.BoundaryNorm(bounds, len(colors))
    cf = ax.pcolormesh(lon, lat, np.ma.masked_less(spd, 10), cmap=cmap, norm=norm,
                       transform=PC, zorder=2, shading="auto")
    barbs(ax, lon, lat, u, v, color="#222")
    if "prmsl" in f:
        mslp = smooth(pick(f, "prmsl") / 100)
        contour_labeled(ax, lon, lat, mslp, np.arange(940, 1060, 4), "black", 0.9)
        hilo(ax, lon, lat, mslp)
    add_basemap(ax)
    colorbar(fig, cf, "10 m wind speed (kt)", ticks=bounds)
    title(fig, ax, meta, "10 m wind (kt) & MSLP (hPa)")
    return fig


def plot_pwat(f, meta):
    fig, ax = new_map(meta)
    lon, lat = f.lon, f.lat
    pw = pick(f, "pwat")
    levels = np.arange(10, 75, 2.5)
    cmap = plt.get_cmap("gist_earth_r")
    cf = ax.contourf(lon, lat, pw, levels=levels, cmap=cmap, extend="both", transform=PC, zorder=2)
    if "prmsl" in f:
        contour_labeled(ax, lon, lat, smooth(pick(f, "prmsl") / 100), np.arange(940, 1060, 4), "white", 0.7)
    add_basemap(ax)
    colorbar(fig, cf, "Precipitable water (mm)", ticks=levels[::4])
    title(fig, ax, meta, "Precipitable water (mm) & MSLP (hPa)")
    return fig


def plot_cape(f, meta):
    fig, ax = new_map(meta)
    lon, lat = f.lon, f.lat
    cape = pick(f, "cape")
    bounds = [100, 250, 500, 750, 1000, 1500, 2000, 2500, 3000, 4000, 5000, 6000]
    colors = ["#e0f3db", "#a8ddb5", "#7bccc4", "#4eb3d3", "#2b8cbe", "#f7e530", "#f5a623",
              "#f05a28", "#d0021b", "#9b0c3d", "#5e0a5e"]
    cmap = mcolors.ListedColormap(colors)
    norm = mcolors.BoundaryNorm(bounds, len(colors))
    cf = ax.pcolormesh(lon, lat, np.ma.masked_less(cape, 100), cmap=cmap, norm=norm,
                       transform=PC, zorder=2, shading="auto")
    if "u10" in f:
        barbs(ax, lon, lat, pick(f, "u10") * 1.944, pick(f, "v10") * 1.944, color="#333")
    add_basemap(ax)
    colorbar(fig, cf, "Surface-based CAPE (J/kg)", ticks=bounds)
    title(fig, ax, meta, "Surface-based CAPE (J/kg) & 10 m wind (kt)")
    return fig
