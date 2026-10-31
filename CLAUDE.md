# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CBERS-4A WPM 2 m imagery of Acre (Brazil), served as WMTS to an [Atlas](https://app.atlas.co) map (project `BdQn9uAknQ4zlYkZN2zi`) that monitors rural assets. Everything lives in `titiler-acre/`; the root `README.md` is the long-form record (measurements, decisions, licences) and the place to put findings. Its numbers (38 scenes, coverage, snapshot dates) are quoted in several sections, so update every mention when a figure changes.

## Commands

All from `titiler-acre/`, in the pipeline environment (`python -m venv pipeline/.venv && pipeline/.venv/bin/pip install -r pipeline/requirements.txt`; `app.py` has its own pinned `requirements.txt`).

```bash
python -m unittest discover -s pipeline                      # all offline tests (no network, no GCP)
python -m unittest discover -s pipeline -k test_last_time    # one test, by name substring
ruff check --select F,E9 pipeline app.py make_links.py       # no ruff config in the repo; this is the check used
node --check pipeline/atlas_refresh.js                       # same for hybrid_layers.js
python make_links.py <SERVICE_URL>                           # rewrites wmts_links.csv (git-ignored: holds the service URL)
python pipeline/check_updates.py [--service-url URL]         # read-only monthly check of upstream changes
python pipeline/check_service.py <SERVICE_URL> stats.txt $(cat scenes_acre.txt)   # smoke test of a deployed service
```

Rebuilding the bucket (`process_all.py`), `scene_stats.py --common` and the deploy (`gcloud run deploy --source .`) need `gcloud` and write to the user's GCP project, so run them only when asked; the steps and flags are in the README ("Rebuilding the bucket", "Deploy").

## Architecture

```
data.inpe.br COGs --pipeline/process_all.py--> gs://brazilian-remote-stac-cog-cbers4a-wpm (private)
                                                      | /vsigs/ range reads
                                               app.py (TiTiler on Cloud Run) --WMTS--> Atlas
```

- **Scenes are copies, not proxies.** `process_all.py` downloads each INPE scene once, bakes a per-band linear stretch (`stretch.txt`), writes a JPEG 85 COG with an internal mask (`compress_cog.py`; YCbCr, or RGB for the scenes in `scenes_rgb.txt`) and uploads it. It is idempotent: done markers in the work dir, resumable downloads.
- **Two sets of ranges.** `stats.txt` holds per-band display ranges in INPE's raw values; `make_links.py` converts them to the copy's stored values through its `stretch.txt` line and puts them in each WMTS URL as `rescale`. So editing `stats.txt` or `stretch.txt` changes the links, and Atlas layers must be swapped (`atlas_refresh.js`). Clients cache tiles 24 h.
- **Plain text files are the configuration:** `scenes_acre.txt` (the 38 scenes, one per path/row cell), `scenes_pansharpened.txt` (5 cells with no fused scene), `scenes_rgb.txt`, `stats.txt`, `stretch.txt`, `snapshots.txt`. `test_pipeline.py` (`DataFiles`) checks that they agree with each other; run the tests after touching any.
- **Pansharpened cells.** Where INPE's fused collection has no scene, `pansharpen_scene.py` builds one from the unfused L4 bands (pan 2 m + R/G/B from BAND3/2/1) with SFIM and a fixed DN gain of 0.32, then it goes through the same `process_all.py` path. Its stats line is computed from the file (`scene_stats.preview_ranges`), not by a `--common` rerun.
- **`app.py`** only accepts `url=` values under the bucket prefix (not an open proxy), serves lossless WebP tiles and caps image/tile sizes for the 2 GiB instance. The Dockerfile pins the base image by digest and the requirements by version; update them on purpose.
- **Atlas has no public API for any of this.** `pipeline/hybrid_layers.js` (`HY`) builds vector layers in the page and uploads them; `pipeline/atlas_refresh.js` (`S`) swaps WMTS layers, verifies tiles pixel by pixel and files layers into folders. Both are pasted into the browser console of the Atlas project page and reach Atlas' layer store through Turbopack internals, which can break with Atlas builds. `HY.NAMES` (final layer names) and `S.FOLDERS` (folder structure) must stay in sync: names are how layers are found. Atlas strips query strings from WMS connection URLs and has no TIME control, so a WMS layer cannot be narrowed to a period.
- **Snapshot vs live layers.** DETER (12 months), PRODES, CAR, embargoes, settlements and fires (12 months) are vector snapshots built by `hybrid_layers.js` and `fundiario_layers.py`; their build dates are in `snapshots.txt`, which `check_updates.py` reads. Update that file when a layer is rebuilt. DETER history, fires of the current month and MapBiomas Alerta are live WMS layers added by hand.
- `fundiario_layers.py` drops personal data (IBAMA names and CPF/CNPJ, SIGEF technicians and owners); keep it that way. SIGEF needs a shapefile downloaded by hand with a gov.br login (`--only sigef --sigef-shp FILE`).

## Working with the user

- The user writes in Brazilian Portuguese; answer in Portuguese. Code, comments, commits and the README are in English; Atlas layer names are in Portuguese.
- The built-in browser cannot reach localhost or local files, so large GeoJSON for Atlas is uploaded by the user (Add data > Upload data). The user uploads their own asset layers last: do not try to load them.
- PRs go to `main` by rebase merge (linear history). Merge, enable auto-merge or create scheduled tasks only after an explicit yes.
