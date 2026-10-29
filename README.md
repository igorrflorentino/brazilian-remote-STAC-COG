# brazilian-remote-STAC-COG

CBERS-4A WPM 2 m imagery of the state of Acre (Brazil), served as WMTS for web
maps such as [Atlas](https://app.atlas.co), which only take remote rasters
through WMS/WMTS.

`titiler-acre/` is a small [TiTiler](https://developmentseed.org/titiler/) 2.4
service on Cloud Run. It serves 33 scenes of INPE's `CB4A-WPM-PCA-FUSED`
collection (Brazil Data Cube), copied as JPEG COGs into a private Cloud Storage
bucket in the same region as the service.

```
data.inpe.br  (DEFLATE COGs, 3-6 GB per scene)
   |   pipeline/process_all.py: download once, recompress to JPEG 85, upload
   v
gs://brazilian-remote-stac-cog-cbers4a-wpm  (private, southamerica-east1, ~25 GiB)
   |   /vsigs/ range reads with the service account (storage.objectViewer only)
   v
Cloud Run "titiler-acre" (app.py)  --WMTS-->  Atlas or any WMTS client
```

Reading INPE's originals on every tile was slow and subject to INPE's rate
limit, so each scene is downloaded once and served from the bucket.

## Files

All paths are relative to `titiler-acre/`.

| File | Purpose |
|---|---|
| `app.py` | The TiTiler service (see [Service](#service)). |
| `Dockerfile`, `requirements.txt` | Container: Python 3.11, rasterio 1.4.4, GDAL tuned for range reads. |
| `make_links.py` | Builds the 33 WMTS links into `wmts_links.csv`. The CSV is git-ignored because it holds the service URL. |
| `scenes.txt` | The 50 candidate scenes (`YYYYMMDD_path_row`). |
| `scenes_acre.txt` | The 33 scenes with valid pixels inside Acre, from `pipeline/acre_scenes.py` (`pipeline/acre_scenes.csv`). |
| `scenes_rgb.txt` | The 19 scenes stored as RGB JPEG (see [Compression](#compression)). |
| `stats.txt` | Per-band rescale ranges used in the links (see [Rescale ranges](#rescale-ranges)). |
| `pipeline/stats_original.txt` | Earlier per-scene 2-98 % ranges from INPE's originals, for reference. |
| `pipeline/acre_scenes.py` | Intersects each scene's valid-data footprint with IBGE's boundary of Acre. |
| `pipeline/fetch_scene.py` | Resumable single-connection download from INPE (If-Range, backoff, throughput cap). |
| `pipeline/compress_cog.py` | RGBA intermediate and lossy COG variants (`jpeg`, `jpeg_rgb`, `webp`). |
| `pipeline/process_all.py` | Batch: download, compress, check, upload, one scene at a time. |
| `pipeline/scene_stats.py` | Writes `stats.txt` lines. |
| `pipeline/check_service.py` | Smoke test of a deployed service. |
| `pipeline/compare_quality.py` | Compares codec variants with an original scene. |

## WMTS links

```
<SERVICE_URL>/cog/WMTSCapabilities.xml?url=<COG>&bidx=1&bidx=2&bidx=3&rescale=<lo,hi>&rescale=<lo,hi>&rescale=<lo,hi>&color_formula=gamma%20RGB%201.3
```

- `url` is the URL-encoded bucket path, which keeps INPE's layout (months are
  not zero-padded):
  `gs://brazilian-remote-stac-cog-cbers4a-wpm/v001/{path}/{row}/{YYYY}/{M}/CBERS4A_WPM_PCA_RGB321_{YYYYMMDD}_{path}_{row}.tif`
- `bidx=1&bidx=2&bidx=3` selects the RGB bands, and the three `rescale`
  values come from `stats.txt`, in band order.
- `color_formula=gamma RGB 1.3` is applied after the rescale. It lifts the
  dark forest without clipping rivers and pasture. TiTiler copies both
  parameters into the tile URL template.
- Each document has one layer,
  `CBERS4A_WPM_PCA_RGB321_{scene}_WebMercatorQuad_default`, with PNG tiles on
  WebMercatorQuad, zoom 0-24.
- The links leave out `nodata=0` on purpose: the COGs carry an internal mask,
  and `nodata=0` would bypass it and show JPEG edge noise as a black fringe.

The rescale is part of the URL, so changing `stats.txt` changes the links and
the WMTS clients have to be pointed at the new ones. To regenerate:

```bash
python make_links.py <SERVICE_URL>
```

## Service

`app.py` mounts TiTiler's `TilerFactory` on `/cog` with the WMTS extension and
WebMercatorQuad only, plus:

- **URL guard:** only `url` values starting with
  `gs://brazilian-remote-stac-cog-cbers4a-wpm/` are accepted, and no `..`
  (403), so the service is not an open proxy. A missing object returns 404.
- **Size caps (422 above them):**
  - preview, bbox, feature and statistics outputs up to 2048 px per side;
  - `tilesize` up to 1024;
  - `buffer` and `padding` up to 64.
- **Caching:** `Cache-Control: public, max-age=86400` on successful responses,
  except `/` and `/health`.
- **CORS:** `*` for GET, because Atlas draws tiles in WebGL.
- **`/health`:** Cloud Run reserves paths ending in `z` on `*.run.app`, so
  `/healthz` only answers locally or on a custom domain.
- **`/`:** returns the status and the attribution text.

## Compression

All 33 copies are JPEG quality 85 with an internal 1-bit mask, 512 px blocks
and AVERAGE overviews. They differ in colour space:

- **14 scenes are YCbCr**, the GDAL COG driver's default for 3-band JPEG.
- **19 scenes are RGB, without chroma subsampling** (`scenes_rgb.txt`). Their
  rescale is narrow, as little as 20 levels stretched to 255. YCbCr's colour
  errors of 1-2 levels and its small per-band bias then show up as 16 px
  magenta/green blotches at zooms 12-14 and a grey or magenta cast on forest,
  at any JPEG quality.
  - RGB files are about 2.8x larger than the YCbCr ones (2.4-2.9x).
  - The COG driver always writes 3-band JPEG as YCbCr, so these scenes go
    through the GTiff driver with `COPY_SRC_OVERVIEWS`. That gives the same
    COG layout and the same number of range requests per tile.

Each object records `source` (the INPE URL), `attribution` and `modifications`
in its custom metadata.

## Coverage

The 33 scenes cover about 81 % of Acre (133,300 of 164,165 km²):

- `CB4A-WPM-PCA-FUSED` has no scene at all for cells 238/124, 239/124,
  241/123, 241/124, 235/126 and 235/127.
- Adjacent orbits leave thin slivers between scenes.
- The fused collection starts in March 2023. INPE's unfused `CB4A-WPM-L4-DN`
  collection has 2020-2022 scenes for most of the missing cells, as separate
  pan and multispectral bands.

## Rescale ranges

All scenes share one contrast, so neighbouring scenes match on the map.
`pipeline/scene_stats.py --common` computes it from the bucket copies:

1. It reads each scene's raw preview (1024 px) through the service.
2. It masks clouds: pixels whose darkest band is above 110 and at least 50
   levels above the scene's median. The mask is grown by about 300 m.
3. It takes each scene's dark level, the 5th percentile of its cloud-free
   pixels. Smoke and haze raise it; a low sun (July) lowers it.
4. It removes each scene's offset from the median dark level and takes the
   2-98 % range of the pooled cloud-free pixels. That gives the common range,
   about 39-90 / 53-94 / 71-115.
5. Each scene's range is the common one shifted by its own offset, from
   about -18 (July scenes) to +75 (blue band of the 2024 smoke scene).

Run it over all scenes at once, and again after adding or recompressing any
scene; every range depends on the whole set.

The earlier per-scene 2-98 % ranges (`--service`, `--inpe`) failed both
ways:

- **Cloudy scenes:** clouds set the top of the range (up to 198 levels wide),
  so the ground came out almost black.
- **Clear forest scenes:** the range was only 20-32 levels wide, so the
  stretch was about 10x. Rivers and pasture clipped to white, and noise,
  sensor striping and haze turned into colour.

## Rebuilding the bucket

Requirements:

- `gcloud`, authenticated with write access to the bucket;
- about 15 GB of free disk;
- the pipeline environment:

```bash
cd titiler-acre
python -m venv pipeline/.venv
pipeline/.venv/Scripts/python -m pip install -r pipeline/requirements.txt  # bin/ instead of Scripts/ on Linux/macOS
```

Then, with that environment active, run the steps below. Each run skips the
scenes that already have a done marker in its work directory, so use a
separate directory for each colour space:

```bash
grep -vxFf scenes_rgb.txt scenes_acre.txt > scenes_ycbcr.txt
python pipeline/process_all.py scenes_ycbcr.txt work_ycbcr gs://brazilian-remote-stac-cog-cbers4a-wpm
python pipeline/process_all.py scenes_rgb.txt work_rgb gs://brazilian-remote-stac-cog-cbers4a-wpm --rgb
python pipeline/scene_stats.py scenes_acre.txt stats.txt --common <SERVICE_URL>
python make_links.py <SERVICE_URL>
python pipeline/check_service.py <SERVICE_URL> stats.txt $(cat scenes_acre.txt)
```

A scene takes about 10-15 minutes:

- 3-10 min to download (capped at 20 MB/s by default, to stay gentle with
  data.inpe.br);
- 3-6 min to compress;
- 1-3 min to upload.

Failed scenes are logged and retried on the next run. Partial downloads
resume.

## Deploy

```bash
cd titiler-acre
gcloud run deploy titiler-acre --source . --region southamerica-east1 --project <PROJECT_ID> \
  --service-account titiler-acre-run@<PROJECT_ID>.iam.gserviceaccount.com --allow-unauthenticated \
  --memory 2Gi --cpu 2 --concurrency 20 --timeout 120 --min-instances 0 --max-instances 1 --max 1
```

- `--source .` builds the `Dockerfile` with Cloud Build. `.gcloudignore`
  keeps `.venv/`, `pipeline/` and `wmts_links.csv` out of the upload.
- The service account needs only `roles/storage.objectViewer` on the bucket.

## Operations

- **Cold start:** about 7-8 s, felt on the first GetCapabilities after the
  service has been idle.
- **Capacity:** one instance handles at most 20 concurrent requests.
- **Client caches:** clients cache tiles for 24 h. After a scene is
  recompressed, they may show old tiles for up to a day unless the link
  changes.
- **Instance cache:** each instance caches the file headers it has read.
  After objects are overwritten, a warm instance may fail on those scenes
  until it is recycled (about 15 minutes idle) or a new revision is deployed.
- **Striping:** the vertical contrast bands in some scenes, such as
  `20261025_237_125`, are already in INPE's originals.
- **Costs:** storage (~25 GiB Standard in southamerica-east1, under US$ 1 a
  month) is the main one. Cloud Run scales to zero and light use stays in the
  free tier. Reads from the bucket in the same region carry no egress charge.
  A budget alert watches the project; it notifies, it does not cap spending.

## Attribution

Imagery: INPE / Brazil Data Cube, `CB4A-WPM-PCA-FUSED`,
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The copies served
here are recompressed (lossy JPEG quality 85), as recorded in each object's
metadata and in the service's attribution text.
