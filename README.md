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
gs://brazilian-remote-stac-cog-cbers4a-wpm  (private, southamerica-east1, ~31 GiB)
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
| `scenes.txt` | The 55 candidate scenes (`YYYYMMDD_path_row`): the original 50 plus the 5 clearer dates of [Scene choice](#scene-choice). |
| `scenes_acre.txt` | The 33 scenes in use, one per path/row cell with valid pixels inside Acre (`pipeline/acre_scenes.py`, `pipeline/acre_scenes.csv`). |
| `scenes_rgb.txt` | The 24 scenes stored as RGB JPEG (see [Compression](#compression)). |
| `stats.txt` | Per-band rescale ranges used in the links (see [Rescale ranges](#rescale-ranges)). |
| `pipeline/stats_original.txt` | Earlier per-scene 2-98 % ranges from INPE's originals, for reference. |
| `pipeline/acre_scenes.py` | Intersects each scene's valid-data footprint with IBGE's boundary of Acre. |
| `pipeline/fetch_scene.py` | Resumable single-connection download from INPE (If-Range, backoff, throughput cap). |
| `pipeline/compress_cog.py` | RGBA intermediate and lossy COG variants (`jpeg`, `jpeg_rgb`, `webp`). |
| `pipeline/process_all.py` | Batch: download, compress, check, upload, one scene at a time. |
| `pipeline/scene_stats.py` | Writes `stats.txt` lines. |
| `pipeline/check_service.py` | Smoke test of a deployed service. |
| `pipeline/compare_quality.py` | Compares codec variants with an original scene. |
| `pipeline/s2_mosaic.py` | Clips INPE's Sentinel-2 3-month mosaic to Acre, for a background layer. |

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

## Background layers

Two Sentinel-2 layers (10 m) can sit under the scenes, to fill the areas
with no scene:

- **EOX Sentinel-2 cloudless 2025**, an annual cloud-free mosaic with no
  visible seams over Acre. It comes straight from EOX's public WMTS,
  `https://tiles.maps.eox.at/wmts/1.0.0/WMTSCapabilities.xml`, layer
  `s2cloudless-2025_3857`.
  - License: [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/),
    **non-commercial use only**; commercial use needs a license from EOX.
  - Attribution: "Sentinel-2 cloudless - https://s2maps.eu by EOX IT
    Services GmbH (Contains modified Copernicus Sentinel data 2025)".
- **INPE's Sentinel-2 3-month mosaic, July-September 2025**
  (`mosaic-s2-brazil-3m-1`), clipped to Acre by `pipeline/s2_mosaic.py` and
  served by this service from the bucket. It shows seams between Sentinel-2
  tiles and some haze, but its license allows any use with attribution (the
  Copernicus Sentinel data legal notice). Link:

  ```
  <SERVICE_URL>/cog/WMTSCapabilities.xml?url=gs%3A%2F%2Fbrazilian-remote-stac-cog-cbers4a-wpm%2Fs2-mosaic%2Fv1%2F2025%2FMOSAIC-S2-BRAZIL-3M_V1_20250701_20250930_ACRE.tif&bidx=1&bidx=2&bidx=3
  ```

  INPE's `visual` asset is already colour-balanced 8-bit RGB, so the link has
  no rescale or gamma. A 2-98 % stretch turns it blue and brings out the
  seams.

In Atlas, keep the background layers at the bottom of the layer list; new
layers are added at the top. The INPE mosaic sits above EOX and is hidden:
shown, it covers EOX inside Acre.

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

The 33 copies in use are JPEG quality 85 with an internal 1-bit mask, 512 px
blocks and AVERAGE overviews. They differ in colour space:

- **9 scenes are YCbCr**, the GDAL COG driver's default for 3-band JPEG.
- **24 scenes are RGB, without chroma subsampling** (`scenes_rgb.txt`). Under
  a strong stretch, YCbCr's colour errors of 1-2 levels and its small
  per-band bias show up as 16 px magenta/green blotches at zooms 12-14 and a
  grey or magenta cast on forest, at any JPEG quality.
  - 19 of them are clear scenes whose per-scene rescale was as little as 20
    levels wide.
  - The other 5 are the clear replacement scenes of
    [Scene choice](#scene-choice). The common rescale still stretches them
    5-6.5x.
  - RGB files are about 2.8x larger than the YCbCr ones (2.4-2.9x).
  - The COG driver always writes 3-band JPEG as YCbCr, so these scenes go
    through the GTiff driver with `COPY_SRC_OVERVIEWS`. That gives the same
    COG layout and the same number of range requests per tile.

Each object records `source` (the INPE URL), `attribution` and `modifications`
in its custom metadata.

## Coverage

The 33 scenes cover about 83 % of Acre (135,700 of 164,165 km²):

- `CB4A-WPM-PCA-FUSED` has no scene at all for cells 238/124, 239/124,
  241/123, 241/124, 235/126 and 235/127.
- Adjacent orbits leave thin slivers between scenes.
- The fused collection starts in March 2023. INPE's unfused `CB4A-WPM-L4-DN`
  collection has 2020-2022 scenes for most of the missing cells, as separate
  pan and multispectral bands.

A Sentinel-2 layer under the scenes fills these areas (see
[Background layers](#background-layers)). It cannot show through clouds:
cloud pixels are valid data and cover it.

## Scene choice

The fused collection has up to 13 dates per cell over Acre (316 scenes). Five
cells now use a clearer date than the one first picked:

| Cell | Was | Now | Inside Acre |
|---|---|---|---|
| 232/125 | 2025-09-11, 8 % cloud, hazy | 2025-08-11, clear | 1,088 km² |
| 233/126 | 2026-05-12, 16 % cloud | 2025-11-07, clear, slight haze | 7,165 km² (was 5,903) |
| 237/123 | 2025-08-17, 28 % cloud | 2023-07-04, 1 % cloud | 5,517 km² |
| 241/122 | 2025-09-28, 6 % cumulus | 2025-08-28, clear | 3,070 km² |
| 242/123 | 2025-08-23, 6 % cloud | 2023-07-10, 3 % cloud | 389 km² (was 52) |

The candidates were screened on the STAC thumbnails, then measured inside
Acre on overviews of INPE's originals, with the cloud rule of
`scene_stats.py --common`. The dark level (5th percentile of the cloud-free
pixels) flags haze and smoke. The cloud-free 2024-08-15 dates of 236/124
and 236/125 were left out: their dark level is 13-36 levels above the typical
one, from fire-season smoke.

The 5 copies they replace stay in the bucket, unused.

## Rescale ranges

All scenes share one contrast, so neighbouring scenes match on the map.
`pipeline/scene_stats.py --common` computes it from the bucket copies:

1. It reads each scene's raw preview (1024 px) through the service.
2. It masks clouds: pixels whose darkest band is above 110 and at least 50
   levels above the scene's median. The mask is grown by about 300 m.
3. It takes each scene's dark level, the 5th percentile of its cloud-free
   pixels. Smoke and haze raise it; a low sun (July) lowers it.
4. It removes each scene's offset from the median dark level and takes the
   2-99.5 % range of the pooled cloud-free pixels. That gives the common
   range, about 39-91 / 52-91 / 70-112.
   - Forest is most of Acre: half the pooled pixels sit within 5 levels of
     the dark end. A 98 % top (79 / 79 / 97) falls inside pasture and
     clips it to white.
5. Each scene's range is the common one shifted by its own offset, from
   about -17 (July scenes) to +76 (blue band of the 2024 smoke scene).

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

The background mosaic is built and uploaded on its own:

```bash
python pipeline/s2_mosaic.py MOSAIC-S2-BRAZIL-3M_V1_20250701_20250930_P20250916 work_s2
gcloud storage cp work_s2/MOSAIC-S2-BRAZIL-3M_V1_20250701_20250930_P20250916_ACRE.tif \
  gs://brazilian-remote-stac-cog-cbers4a-wpm/s2-mosaic/v1/2025/MOSAIC-S2-BRAZIL-3M_V1_20250701_20250930_ACRE.tif \
  "--custom-metadata=source=https://data.inpe.br/bdc/data/s2/s2-3m/v1/2025/MOSAIC-S2-BRAZIL-3M_V1_20250701_20250930_P20250916_VISUAL.tif,attribution=INPE - Brazil Data Cube / contains modified Copernicus Sentinel data 2025,modifications=Clipped to Acre and recompressed to JPEG quality 85 (YCbCr with internal mask)"
```

The clip reads about 3 GB from data.inpe.br (about 20 minutes) and writes a
0.37 GB COG; `gcloud` splits metadata on commas, so the values have none.

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
- **Costs:** storage is the main one: ~31 GiB Standard in
  southamerica-east1, about US$ 1.10 a month, of which the 5 unused copies
  are 2.25 GiB. Cloud Run scales to zero and light use stays in the free
  tier. Reads from the bucket in the same region carry no egress charge. A
  budget alert watches the project; it notifies, it does not cap spending.

## Attribution

Imagery: INPE / Brazil Data Cube, `CB4A-WPM-PCA-FUSED`,
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The copies served
here are recompressed (lossy JPEG quality 85), as recorded in each object's
metadata and in the service's attribution text.

Background layers:

- INPE / Brazil Data Cube, `mosaic-s2-brazil-3m-1`, contains modified
  Copernicus Sentinel data 2025. The copy here is clipped to Acre and
  recompressed.
- Sentinel-2 cloudless - https://s2maps.eu by EOX IT Services GmbH (Contains
  modified Copernicus Sentinel data 2025),
  [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).
