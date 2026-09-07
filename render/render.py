#!/usr/bin/env python3
"""
Render GFS maps for the site.

    python render/render.py                      # latest available run, all regions/params
    python render/render.py --run 2026090612     # specific run
    python render/render.py --hours 0-48/6 --regions conus natl --params z500_vort mslp_precip
    python render/render.py --synthetic          # no network: fake fields, for testing plots

Output:
    site/images/gfs/<run>/<region>/<param>/f<hhh>.png
    site/manifest.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import shutil
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import matplotlib.pyplot as plt  # noqa: E402
import requests  # noqa: E402

import plots  # noqa: E402
import storage  # noqa: E402
from config import (FORECAST_HOURS, KEEP_RUNS, MODEL, PARAMS, REGIONS)  # noqa: E402
from fetch import (all_fetch_pairs, build_filter_url, crop, download, download_ecmwf,
                   latest_available_run, load_grib, synthetic_fields)  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("render")

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
PAD = 6  # degrees of extra data around each region so edge contours aren't clipped


def parse_hours(spec: str) -> list[int]:
    if "-" in spec:
        rng, _, step = spec.partition("/")
        a, b = (int(x) for x in rng.split("-"))
        return list(range(a, b + 1, int(step or 6)))
    return [int(x) for x in spec.split(",")]


def padded(bbox):
    lon0, lon1, lat0, lat1 = bbox
    return (lon0 - PAD, lon1 + PAD, max(lat0 - PAD, -89), min(lat1 + PAD, 89))


def render_frame(run_iso: str, fhr: int, region: str, param_ids: list[str],
                 grib_path: str | None, out_dir: str, synthetic: bool) -> list[str]:
    """Render every requested parameter for one (hour, region). Runs in a worker."""
    run = dt.datetime.fromisoformat(run_iso)
    bbox = REGIONS[region]["bbox"]
    fields = synthetic_fields(fhr, padded(bbox)) if synthetic else crop(load_grib(Path(grib_path)), padded(bbox))
    meta = {"run": run, "fhr": fhr, "bbox": bbox, "region": region,
            "region_name": REGIONS[region]["name"]}
    written = []
    for pid in param_ids:
        fn = getattr(plots, PARAMS[pid]["plot"])
        dest = Path(out_dir) / region / pid / f"f{fhr:03d}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            fig = fn(fields, meta)
            fig.savefig(dest, dpi=fig.dpi, facecolor="white")
            plt.close(fig)
            compress_png(dest)
            written.append(str(dest))
        except Exception as e:  # noqa: BLE001
            log.exception("failed %s %s f%03d: %s", region, pid, fhr, e)
    return written


def compress_png(path: Path):
    """Palette-quantize the PNG: these maps have few distinct colours, so this
    roughly halves the file with no visible change."""
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        im.save(path, optimize=True)
    except Exception as e:  # noqa: BLE001
        log.warning("compress failed for %s: %s", path.name, e)


def write_manifest(run_id: str, hours: list[int], regions: list[str], param_ids: list[str]):
    man_path = SITE / "manifest.json"
    manifest = None
    if storage.enabled():
        manifest = storage.get_json("manifest.json")   # merge with what's already published
    if manifest is None and man_path.exists():
        try:
            manifest = json.loads(man_path.read_text())
        except json.JSONDecodeError:
            manifest = None
    manifest = manifest or {"model": {}, "regions": {}, "params": {}}
    runs = [r for r in manifest.get("model", {}).get("runs", []) if r["id"] != run_id]
    runs.append({
        "id": run_id,
        "init": dt.datetime.strptime(run_id, "%Y%m%d%H").replace(tzinfo=dt.timezone.utc).isoformat(),
        "hours": hours, "regions": regions, "params": param_ids,
    })
    runs.sort(key=lambda r: r["id"], reverse=True)
    manifest["model"] = {"id": MODEL["id"], "name": MODEL["name"], "resolution": MODEL["resolution"],
                         "credit": MODEL.get("credit", ""), "runs": runs[:KEEP_RUNS]}
    manifest["regions"] = {k: {"name": v["name"]} for k, v in REGIONS.items()}
    manifest["params"] = {k: {"name": v["name"], "group": v["group"]} for k, v in PARAMS.items()}
    manifest["path"] = "images/{model}/{run}/{region}/{param}/f{hour}.png"
    manifest["generated"] = dt.datetime.now(dt.timezone.utc).isoformat()
    man_path.write_text(json.dumps(manifest, indent=1))
    return manifest


def prune_runs(keep_ids: list[str]):
    if storage.enabled():
        for rid in storage.list_prefixes(f"images/{MODEL['id']}"):
            if rid not in keep_ids:
                storage.delete_prefix(f"images/{MODEL['id']}/{rid}/")
        return
    img_root = SITE / "images" / MODEL["id"]
    if not img_root.exists():
        return
    for d in img_root.iterdir():
        if d.is_dir() and d.name not in keep_ids:
            log.info("pruning old run %s", d.name)
            shutil.rmtree(d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="YYYYMMDDHH; default = latest available on NOMADS")
    ap.add_argument("--hours", default=None, help="e.g. 0-120/6 or 0,6,12")
    ap.add_argument("--regions", nargs="*", default=list(REGIONS))
    ap.add_argument("--params", nargs="*", default=MODEL["params"])
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--synthetic", action="store_true", help="fake data, no network")
    ap.add_argument("--keep-grib", action="store_true")
    args = ap.parse_args()

    hours = parse_hours(args.hours) if args.hours else FORECAST_HOURS
    session = requests.Session()
    session.headers["User-Agent"] = "wxmodels-renderer (github actions)"

    if args.run:
        run = dt.datetime.strptime(args.run, "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
    elif args.synthetic:
        run = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        run = run.replace(hour=(run.hour // 6) * 6)
    else:
        run = latest_available_run(session=session)
    run_id = run.strftime("%Y%m%d%H")
    log.info("rendering %s run %s: %d hours × %d regions × %d params",
             MODEL["name"], run_id, len(hours), len(args.regions), len(args.params))

    out_dir = SITE / "images" / MODEL["id"] / run_id
    grib_dir = Path(tempfile.mkdtemp(prefix="wx_grib_")) if not args.keep_grib else ROOT / "grib" / run_id
    pairs = all_fetch_pairs(args.params)

    # 1. download (sequential; both servers rate-limit aggressive parallel clients)
    #    GFS: one regional subset per (hour, region). ECMWF: one global file per hour,
    #    shared by every region (open data has no bbox subsetting).
    grib_paths: dict[tuple[int, str], str | None] = {}
    for fhr in hours:
        if args.synthetic:
            for region in args.regions:
                grib_paths[(fhr, region)] = None
            continue
        if MODEL["source"] == "ecmwf_opendata":
            dest = grib_dir / f"global_f{fhr:03d}.grib2"
            try:
                download_ecmwf(run, fhr, args.params, dest)
                for region in args.regions:
                    grib_paths[(fhr, region)] = str(dest)
            except RuntimeError as e:
                log.error("%s", e)
            continue
        for region in args.regions:
            url = build_filter_url(run, fhr, pairs, padded(REGIONS[region]["bbox"]))
            dest = grib_dir / f"{region}_f{fhr:03d}.grb2"
            try:
                download(url, dest, session)
                grib_paths[(fhr, region)] = str(dest)
            except RuntimeError as e:
                log.error("%s", e)

    # 2. render in parallel
    jobs = [(fhr, region, p) for (fhr, region), p in grib_paths.items()]
    n_done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(render_frame, run.isoformat(), fhr, region, args.params,
                          p, str(out_dir), args.synthetic) for fhr, region, p in jobs]
        for fut in as_completed(futs):
            n_done += len(fut.result())
            if n_done % 25 == 0:
                log.info("%d images written", n_done)
    log.info("done: %d images", n_done)

    # 3. publish: R2 if configured, else leave in site/ for the Pages artifact
    if storage.enabled():
        storage.upload_dir(out_dir, f"images/{MODEL['id']}/{run_id}")
    manifest = write_manifest(run_id, hours, args.regions, args.params)
    if storage.enabled():
        storage.put_json(manifest, "manifest.json")
    prune_runs([r["id"] for r in manifest["model"]["runs"]])
    if storage.enabled():
        shutil.rmtree(out_dir, ignore_errors=True)   # don't ship images in the Pages artifact too
        (SITE / "manifest.json").unlink(missing_ok=True)
    if not args.keep_grib:
        shutil.rmtree(grib_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
