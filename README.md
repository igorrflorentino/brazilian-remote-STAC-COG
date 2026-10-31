# brazilian-remote-STAC-COG

CBERS-4A WPM 2 m imagery of the state of Acre (Brazil), served as WMTS for web
maps such as [Atlas](https://app.atlas.co), which only take remote rasters
through WMS/WMTS.

`titiler-acre/` is a small [TiTiler](https://developmentseed.org/titiler/) 2.4
service on Cloud Run. It serves 33 scenes of INPE's `CB4A-WPM-PCA-FUSED`
collection (Brazil Data Cube), copied as linearly stretched JPEG COGs into a
private Cloud Storage bucket in the same region as the service.

```
data.inpe.br  (DEFLATE COGs, 3-6 GB per scene)
   |   pipeline/process_all.py: download once, stretch, recompress to JPEG 85, upload
   v
gs://brazilian-remote-stac-cog-cbers4a-wpm  (private, southamerica-east1, ~38 GiB)
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
| `Dockerfile`, `requirements.txt` | Container: Python 3.11, rasterio 1.4.4, GDAL tuned for range reads. The base image and every package are pinned (see [Deploy](#deploy)). |
| `make_links.py` | Builds the 33 WMTS links into `wmts_links.csv`. The CSV is git-ignored because it holds the service URL. |
| `scenes.txt` | The 57 candidate scenes (`YYYYMMDD_path_row`): the original 50, the 5 clearer dates of [Scene choice](#scene-choice) and the 2 dates that replaced [defective scenes](#defective-fused-scenes). |
| `scenes_acre.txt` | The 33 scenes in use, one per path/row cell with valid pixels inside Acre (`pipeline/acre_scenes.py`, `pipeline/acre_scenes.csv`). |
| `stats.txt` | Per-band rescale ranges used in the links, in INPE's raw values (see [Rescale ranges](#rescale-ranges)). |
| `stretch.txt` | Per-band linear stretch baked into each copy, raw low-high (see [Compression](#compression)). |
| `pipeline/stats_original.txt` | Earlier per-scene 2-98 % ranges from INPE's originals, for reference. It predates [Scene choice](#scene-choice): it still has the 5 replaced scenes and not the 5 that replaced them. |
| `pipeline/acre_scenes.py` | Intersects each scene's valid-data footprint with IBGE's boundary of Acre. |
| `pipeline/check_bands.py` | Checks a scene's fused bands against INPE's unfused bands of the same pass; it flags the defective red band of [20251107_233_126](#defective-fused-scenes). Run it on every candidate scene. |
| `pipeline/fetch_scene.py` | Resumable single-connection download from INPE (If-Range, backoff, throughput cap). |
| `scenes_rgb.txt` | The 7 scenes whose copy is RGB JPEG instead of YCbCr (see [Compression](#compression)). |
| `pipeline/compress_cog.py` | `write_stretched_cog`, the pipeline's stretched JPEG COG; also unstretched COG variants (`jpeg`, `jpeg_rgb`, `webp`) from an RGBA intermediate. |
| `pipeline/process_all.py` | Batch: download, stretch, compress, check, upload, one scene at a time; updates `stretch.txt`. |
| `pipeline/scene_stats.py` | Writes `stats.txt` lines; `linear_bounds` picks each copy's stretch. |
| `pipeline/check_service.py` | Smoke test of a deployed service, including each scene's rescale ranges. |
| `pipeline/test_pipeline.py` | Offline tests: stretch arithmetic, `stats.txt` and `stretch.txt`, download resume, upload retries. Run with `python -m unittest discover -s pipeline`. |
| `pipeline/compare_quality.py` | Compares unstretched codec variants with an original scene, on local files. |
| `pipeline/compare_served.py` | Compares the service's tiles with INPE's lossless originals: the measurements in [Compression](#compression). |
| `pipeline/s2_mosaic.py` | Clips INPE's Sentinel-2 3-month mosaic to Acre, for a background layer. |
| `pipeline/hybrid_layers.js` | Browser-console script that builds the [hybrid map layers](#hybrid-map-layers) and uploads them to Atlas. |
| `pipeline/fundiario_layers.py` | Builds the four [land-tenure and enforcement layers](#land-tenure-and-enforcement-layers) (CAR, settlements, IBAMA embargoes, fire hotspots) as GeoJSON from their public sources. |
| `pipeline/atlas_refresh.js` | Browser-console script that swaps the Atlas map's WMTS layers for new links, files the new scenes into their [folder](#atlas-folders) by date and checks them pixel by pixel against the service. `S.organize()` files every layer into the folders. |

## WMTS links

```
<SERVICE_URL>/cog/WMTSCapabilities.xml?url=<COG>&bidx=1&bidx=2&bidx=3&rescale=<lo,hi>&rescale=<lo,hi>&rescale=<lo,hi>&color_formula=gamma%20RGB%201.3&tile_format=webp
```

- `url` is the URL-encoded bucket path, which keeps INPE's layout below the
  `linear/` prefix (months are not zero-padded):
  `gs://brazilian-remote-stac-cog-cbers4a-wpm/linear/v001/{path}/{row}/{YYYY}/{M}/CBERS4A_WPM_PCA_RGB321_{YYYYMMDD}_{path}_{row}.tif`
- `bidx=1&bidx=2&bidx=3` selects the RGB bands. The three `rescale` values
  are `stats.txt`'s raw ranges converted to the copy's stored values with
  its `stretch.txt` line, in band order: raw `v` is stored as
  `(v - low) * 255 / (high - low)`.
- `color_formula=gamma RGB 1.3` is applied after the rescale. It lifts the
  dark forest without clipping rivers and pasture. TiTiler copies both
  parameters into the tile URL template.
- `tile_format=webp` asks for lossless WebP tiles (see [Service](#service)).
- Each document has one layer,
  `CBERS4A_WPM_PCA_RGB321_{scene}_WebMercatorQuad_default`, on
  WebMercatorQuad, limited to zooms 8-16, the range the COGs cover.
- The links leave out `nodata=0` on purpose: the COGs carry an internal mask,
  and `nodata=0` would bypass it and show JPEG edge noise as a black fringe.

The rescale is part of the URL, so changing `stats.txt` or `stretch.txt`
changes the links and the WMTS clients have to be pointed at the new ones. To
regenerate:

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

In Atlas the background layers sit in the "Fundo Sentinel-2" folder at the
bottom of the layer list. The INPE mosaic sits above EOX and is hidden: shown,
it covers EOX inside Acre. Showing the folder again shows both layers, so hide
the mosaic again by hand.

## Hybrid map layers

Twelve vector layers sit above the scenes in the Atlas map, so the imagery
reads like a hybrid map: roads, rivers, protected areas, deforestation and
services. As built on 2026-10-29 (the project now groups them in
[folders](#atlas-folders)):

| Layer | Source | Features | Style |
|---|---|---|---|
| Aeródromos (ANAC) | MInfra/ANAC public and private aerodromes (INDE WFS) | 15 | magenta (public) or purple (private) dots, name and ICAO code |
| Escolas e saúde (OSM) | OpenStreetMap `amenity` school, hospital, clinic, doctors | 471 | blue, red and orange dots from zoom 12; only health units are labelled |
| Estradas e ramais (OSM) | OpenStreetMap `highway` motorway to tertiary, unclassified, track | 8,739 | by type: BR red, AC orange, other roads yellow, vicinal beige, ramal tan; road number or name |
| Ruas urbanas (OSM) | OpenStreetMap `highway` residential, living_street | 10,856 | thin white lines with names, from zoom 12 |
| Rios e igarapés (OSM) | OpenStreetMap `waterway` river, stream | 2,348 | blue, rivers wider; names along the line |
| Alertas DETER, últimos 12 meses (INPE) | TerraBrasilis `deter-amz:deter_amz`, `uf='AC'`, since 2025-10-29 | 1,625 | outline and 30 % fill by class (clear-cut red, degradation yellow, selective logging pink, ...) |
| Desmatamento PRODES 2016-2025 (INPE) | TerraBrasilis `prodes-legal-amz:yearly_deforestation`, `state='AC'` | 55,395 | 55 % fill by year, pale yellow (2016) to magenta (2025) |
| Terras Indígenas (FUNAI via INPE) | TerraBrasilis `indigenous_area_legal_amazon`, intersecting Acre | 32 | orange outline, "TI" name |
| Unidades de Conservação (MMA/CNUC) | MMA CNUC 07/2026 (INDE WFS), plus the state Resex do Rio Gregório from TerraBrasilis | 16 | dashed green outline by group (strict protection or sustainable use), name |
| Municípios (IBGE via INPE) | TerraBrasilis `municipalities_legal_amazon`, codes `12*` | 22 | dashed white outline, name |
| Datas das imagens CBERS-4A | footprints from INPE's STAC (`CB4A-WPM-PCA-FUSED-1`) | 33 | dotted outline with the scene date, up to zoom 9.5 |
| Limite do Acre | TerraBrasilis `states_legal_amazon`, `sigla='AC'` | 1 | white outline |

- Geometries are simplified with Douglas-Peucker (0.00003-0.0001°, 3-11 m)
  and rounded to 5 decimals. Attributes are reduced to a few Portuguese fields,
  which Atlas shows in the click popup.
- Atlas renders its basemap labels (towns, villages) above every user layer,
  so the map has no separate places layer.
- DETER and PRODES are snapshots. To refresh them, rebuild and upload those
  layers with `pipeline/hybrid_layers.js`, then delete the old ones.

To rebuild, paste `pipeline/hybrid_layers.js` into the browser console of the
Atlas project and run `HY.run([...])`, `HY.uploadAll([...])` (bottom layer
first, since uploads land at the top) and `HY.applyStyle(key)` for each layer.
Then paste `pipeline/atlas_refresh.js` and run `await S.organize()` to file the
new layers into their [folders](#atlas-folders). The styles go through Atlas'
internal React state, which is not a public API; if that breaks, `HY.STYLES`
still documents the colours to set in the Layer panel.

### Land-tenure and enforcement layers

Four more vector layers, built by `pipeline/fundiario_layers.py` from public
services. Like DETER and PRODES they are snapshots; these were built on
2026-10-30:

| Layer | Source | Features | Style |
|---|---|---|---|
| Focos de calor, últimos 12 meses (INPE) | INPE Queimadas geoserver, `dados_abertos:focos_*_br_satref` (reference satellite), `estado='ACRE'` | 2,194 | orange dots |
| Embargos (IBAMA) | IBAMA open-data CSV `termo_embargo`, UF AC, rows with a polygon | 3,322 | outline and 35 % fill by status: embargoed rose, lifted and cancelled grey |
| Assentamentos (INCRA via INPE) | INPE Queimadas geoserver, `bdqueimadas:assentamentos`, `uf='ACRE'` | 112 | blue outline, 12 % fill, name |
| Imóveis rurais (CAR/SICAR) | SICAR's public WFS, `sicar:sicar_imoveis_ac` | 63,094 | outline by status: active teal, pending light grey, suspended violet, cancelled dark grey; from zoom 10 |

- The CAR is self-declared: properties overlap, and the 5,660 cancelled ones
  (of 63,094; 49,358 are active, 8,044 pending, 32 suspended) are only
  flagged. The layer keeps the property code, status, condition, type, area,
  municipality, fiscal modules and update date.
- The IBAMA CSV also holds the name and CPF/CNPJ of each embargoed person and
  the infraction and process numbers: none of them is copied. 1,072 more
  embargoes in Acre have only a point and are left out.
- INCRA's SIGEF parcels are not included: `acervofundiario.incra.gov.br` did
  not resolve from the build host and SIGEF's downloads are not scriptable. The
  settlements are INPE's copy of INCRA's layer, with no date stated.
- Atlas imported 63,090 of the 63,094 CAR properties and all the others. The
  4 it did not accept were not identified; 50 properties have self-intersecting
  rings in SICAR's data, and Atlas took almost all of them.
- What was found about the licences of these four sources is in
  [Licences](#licences): IBAMA's is open, and none is stated for the other
  three.

To rebuild, run `python pipeline/fundiario_layers.py work_fund` (a few minutes;
`--ibama-csv` reuses the 170 MB national CSV if it is already on disk). It goes
through curl, because SICAR's server fails the TLS handshake with Python's
OpenSSL 3. Atlas' built-in browser reaches neither local files nor localhost,
so upload the four files with Add data > Upload data (Atlas names each layer
after its file), rename them with `HY.rename(file, HY.NAMES[key])`, run
`HY.applyStyle(key)` for each and `await S.organize()`. In a regular browser,
`await HY.load(url, [...])` and `HY.uploadAll([...])` do it from an http server
that sends CORS headers.

### Atlas folders

The project's layers sit in seven folders, so that a whole group can be hidden
or shown at once. Top to bottom:

| Folder | Layers |
|---|---|
| Pontos de interesse | Aeródromos, Escolas e saúde |
| Vias e hidrografia | Estradas e ramais, Ruas urbanas, Rios e igarapés |
| Desmatamento (INPE) | Alertas DETER, Desmatamento PRODES |
| Fundiário e fiscalização | Focos de calor, Embargos, Assentamentos, Imóveis rurais (CAR) |
| Limites e áreas protegidas | Terras Indígenas, Unidades de Conservação, Municípios, Limite do Acre |
| Imagens CBERS-4A (2 m) | Datas das imagens, then the 33 scenes, newest first |
| Fundo Sentinel-2 | The INPE mosaic (hidden) and EOX |

- Hiding a folder hides all its layers. Showing it again shows all of them,
  including the INPE mosaic of "Fundo Sentinel-2", which has to stay hidden.
- The "Imagens" folder is kept collapsed. Atlas draws no rows for the layers
  of a collapsed folder, so the scripts open the folders they need
  (`S.expandAll`, `HY.expand`) and leave them open.
- New layers land at the top of the list, outside the folders.
  `atlas_refresh.js` files new scenes into their folder by date, and
  `await S.organize()` files every layer into its folder. `S.FOLDERS` holds
  the structure and has to match `HY.NAMES` in `hybrid_layers.js`; `S.organize`
  is safe to repeat and leaves layers it does not know below the folders.
- Folders, like the styles, go through Atlas' internal layer store.
- The project opens on the Acre view: it is the map origin (project menu, "Set
  map origin", which also sets the project's cover image).
- The credits on the map ([Attribution](#attribution)) are five stacked Atlas
  text elements south of Acre, one line each, and show only while that spot is
  on screen (the initial view does). A text element has a single line: Enter
  inside it starts editing the geometry of its box instead of adding a line.
  Its size is fixed at the zoom it was created at (44.8 px there) and scales
  with the map zoom, so they were created at zoom 8 to read at 11.2 px in the
  zoom-6 initial view. The colour starts black; set it to `#ffffff`.

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
- **Resampling:** `resampling` and `reproject` default to `bilinear` instead
  of nearest. Tiles rarely fall on the 2 m grid (z16 is about 2.36 m over
  Acre), and nearest drops or repeats source pixels. Against an
  area-averaged reference, bilinear came out 0.8 / 1.8 / 6.5 dB closer at
  z12 / z14 / z16, for about 0.05 s more per tile. The links do not change,
  and a request can still pass `resampling=nearest`.
- **WebP tiles are lossless:** the same pixels as PNG at 35-55 % of the size,
  for about 35 ms more encoding per tile. rio-tiler's default WebP is lossy
  (quality 75, 2-6 dB below the PNG). The scene links ask for
  `tile_format=webp`; without it, tiles are PNG.
- **Caching:** `Cache-Control: public, max-age=86400` on successful responses,
  except `/` and `/health`.
- **CORS:** `*` for GET, because Atlas draws tiles in WebGL.
- **`/health`:** Cloud Run reserves paths ending in `z` on `*.run.app`, so
  `/healthz` only answers locally or on a custom domain.
- **`/`:** returns the status and the attribution text.

## Compression

The 33 copies in use are JPEG quality 85 COGs with an internal 1-bit mask,
512 px blocks and AVERAGE overviews: 26 in YCbCr, and the 7 scenes listed in
`scenes_rgb.txt` in RGB. Each band is stored **linearly stretched**:

```
stored = clip(round((raw - low) * 255 / (high - low)), 0, 255)
```

- INPE's raw values are dark and narrow: the links' rescale stretched them
  4.9-6.5x on screen, and JPEG's errors with them. YCbCr's colour errors of
  1-2 levels and its small per-band bias showed up as 16 px magenta/green
  blotches at zooms 12-14 and a grey or magenta cast on forest, at any JPEG
  quality. Stored stretched, the links stretch only 1.1-2.8x.
- `low`/`high` per band (`stretch.txt`, and `linear_stretch` in the object
  metadata) are the cloud-free 0.01 / 99.99 % values of a 1/16 preview of
  the original, 2 levels wider, and never inside the link's rescale range
  plus 2 levels (`scene_stats.linear_bounds`). Brighter clouds saturate; every
  value the link shows survives. `low` stays at least 1, as 0 is no data, and
  the mask comes from the raw values.
- **Overviews are averaged from the raw values.**
  `compress_cog.write_stretched_cog` writes the raw values times 64 as UInt16
  with AVERAGE overviews, then stretches the full resolution and every
  overview through a VRT (`ScaleOffset`/`ScaleRatio`) while copying to JPEG.
  `low` comes from a 1/16 preview, which is smoother than the 2 m pixels, so
  in hazy scenes 8-12 % of the pixels fall below it. Averaging after the clip
  at `low` lifted the dark forest of the low zooms by 4-8 levels. Only the 7
  RGB copies were built this way. The other 26 predate it and still average
  after the clip, which matters little there: their worst bias in the
  measurements below is 3.2 levels.
- The rescale and gamma stay in the links; applying them before JPEG as well
  washed out the forest texture at zooms 12-14.
- **RGB for 7 scenes.** In the hazy 2026 dry-season scenes about a third of
  the pixels sit below the link's black point. Only the positive half of
  YCbCr's 4:2:0 chroma errors shows there, and the gamma is steepest near
  black, so they got a red/magenta cast of 4.6-9.0 display levels at every
  zoom on 4096 px crops, against 3.2 at most in the other scenes. Quality 92
  does not remove it. RGB JPEG has no chroma subsampling: the cast fell to 0.5
  levels at most, at about 2.7x the size.
- Served tiles of those 7 scenes against INPE's originals, YCbCr then RGB:
  median PSNR at z12 went from 26.8 to 29.9 dB and at z15 from 25.4 to 28.6 dB;
  the worst band bias at z12 went from 8.3 to 0.6 levels. At z15 it went from
  5.1 to 3.0 levels; what remains is a darkening of similar size in all three
  bands, which the earlier unstretched copies show too.
- On the pilot scene `20250922_236_124`, rendered bilinear and compared with
  the original, PSNR at z12 / z14 / z16 went from 28.7 / 26.5 / 28.5 dB to
  32.0 / 30.1 / 33.2 dB. Storing the raw values in RGB JPEG (no chroma
  subsampling) gave 30.5 / 27.2 / 30.4 dB at 1.8x the size.

Before the stretch, 24 of the copies were raw RGB JPEG to avoid the YCbCr
blotches. `compress_cog.py`'s command line still writes unstretched variants
(`jpeg`, `jpeg_rgb`, `webp`), for comparisons.

Each object records `source` (the INPE URL), `attribution`, `modifications`
and `linear_stretch` in its custom metadata.

## Coverage

The 33 scenes cover about 82 % of Acre (134,700 of 164,165 km²):

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

The fused collection has up to 13 dates per cell over Acre (316 scenes). Seven
cells now use a different date than the one first picked: five for a clearer
sky, two because the first date's colours are wrong (see
[Defective fused scenes](#defective-fused-scenes)):

| Cell | Was | Now | Inside Acre |
|---|---|---|---|
| 232/125 | 2025-09-11, 8 % cloud, hazy | 2025-08-11, clear | 1,088 km² |
| 233/125 | 2025-11-07, tinted | 2025-07-06, 1 % cloud | 3,851 km² (was 4,701) |
| 233/126 | 2026-05-12, 16 % cloud | 2025-08-06, 7 % cloud | 6,664 km² (was 5,812) |
| 237/123 | 2025-08-17, 28 % cloud | 2023-07-04, 1 % cloud | 5,517 km² |
| 241/122 | 2025-09-28, 6 % cumulus | 2025-08-28, clear | 3,070 km² |
| 242/123 | 2025-08-23, 6 % cloud | 2023-07-10, 3 % cloud | 389 km² (was 41) |

The candidates were screened on the STAC thumbnails, then measured inside
Acre on overviews of INPE's originals, with the cloud rule of
`scene_stats.py --common`. The dark level (5th percentile of the cloud-free
pixels) flags haze and smoke. The cloud-free 2024-08-15 dates of 236/124
and 236/125 were left out: their dark level is 13-36 levels above the typical
one, from fire-season smoke. Since 2026-10-30 every candidate also goes
through `pipeline/check_bands.py` (below), and its image is rendered with its
own link contrast before it is picked.

The 5 copies replaced on 2026-10-29 were deleted from the bucket.

### Defective fused scenes

INPE's PCA fusion is not always right. In `20251107_233_126`, the scene that
covers Rio Branco, the red band of the fused file has no relation to the real
red. Bright surfaces come out with little red and look blue (roofs turn cyan),
dark ones come out red (the river is orange, forest purple, pasture neon
green), and the links' 4.9-6.5x stretch of the raw values turns the band's
colour fringes into rainbow edges around every building. The scene had been picked for its clear
sky: the screening above looks at cloud and dark level, not at the bands.

Measured with `pipeline/check_bands.py` against INPE's unfused
`CB4A-WPM-L4-DN` collection, which keeps blue, green and red apart at 8 m:

- Fused red against the unfused red: correlation 0.22 over 10 sampled
  windows, against 0.64 for green and 0.44 for blue. In the unfused bands
  blue, green and red correlate 0.88-0.97 with each other.
- The red band of the other dates of cell 233/126 correlates 0.8-0.99, except
  the smoky 2024-07-30 (0.67).
- `gap`, how far the red correlation trails the mean of green and blue, is
  0.32 for 233/126. Of 49 scene dates measured over Acre, 45 have 0.05 or
  less. Haze lowers all three bands alike and leaves the gap alone.
- No rescale fixes it: one fitted to Sentinel-2 over Rio Branco corrected the
  overall tone, but the noise and the cyan roofs stayed.

The other scene of the same pass, `20251107_233_125`, is borderline: gap 0.11
(red 0.68, green 0.86, blue 0.73) and a teal cast with cyan blotches that its
other dates do not have. It was replaced too, at the cost of 850 km² inside
Acre, which the Sentinel-2 background now fills. Between a gap of 0.08 and 0.25
`check_bands.py` says "check the image": two other dates sit there, one smoky
and one natural-looking, so only the image settles it.

The replacements were measured with `check_bands.py`, the cloud rule, the dark
level and the km² inside Acre, and rendered with their own link contrast:

- **233/126:** 2025-08-06 (7 % cloud, sharp city, natural colours). 2025-07-06
  has 0 % cloud by the rule, but haze washes out the city; 2026-05-12 has
  16 % cloud; 2025-05-05, 2024-10-31, 2024-05-29 and 2023-06-23 have 25-31 %.
- **233/125:** 2025-07-06 (1 % cloud; fused bands match the unfused ones,
  0.95-0.97). 2023-07-24 covers 340 km² more, but is two years older and
  matches the unfused bands worse (0.51-0.70); 2024-07-30 is smoke-brown; the
  others have 36-44 % cloud.

Check a new candidate before it enters `scenes_acre.txt`:

```bash
python pipeline/check_bands.py 20250806_233_126
```

## Rescale ranges

All scenes share one contrast, so neighbouring scenes match on the map.
`pipeline/scene_stats.py --common` computes it from the bucket copies:

1. It reads each scene's preview (1024 px, nearest) through the service and
   maps it back to raw values with `stretch.txt`.
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
scene; every range depends on the whole set. The copies are stretched to fit
the current ranges: a new range that reaches past a scene's `stretch.txt`
values clips there, and that scene has to be rebuilt.

The 2 scenes of [Defective fused scenes](#defective-fused-scenes), added on
2026-10-30, did not go through a new `--common` run: it would shift every
scene's range by a level or two, and with it every link and every Atlas layer.
Their ranges are the common one of the other 31 scenes (39-91 / 51-90 /
69-111, recovered from `stats.txt`) shifted by their own offset from the
typical dark level (40 / 52 / 70), as in steps 3-5. The next full run folds
them in.

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
- about 18 GB of free disk (the original, the UInt16 intermediate with its
  overviews, about 1.4x the original, and the COG);
- the pipeline environment:

```bash
cd titiler-acre
python -m venv pipeline/.venv
pipeline/.venv/Scripts/python -m pip install -r pipeline/requirements.txt  # bin/ instead of Scripts/ on Linux/macOS
```

Then, with that environment active, run the steps below. `process_all.py`
needs `stats.txt` (each stretch covers the scene's rescale range) and writes
`stretch.txt`. Each run skips the scenes that already have a done marker in
its work directory.

```bash
python pipeline/process_all.py scenes_acre.txt work gs://brazilian-remote-stac-cog-cbers4a-wpm/linear
python make_links.py <SERVICE_URL>
python pipeline/check_service.py <SERVICE_URL> stats.txt $(cat scenes_acre.txt)
```

`stats.txt` itself comes from `scene_stats.py --common <SERVICE_URL>`, which
reads the copies already in the bucket (see [Rescale ranges](#rescale-ranges)).

The background mosaic is built and uploaded on its own:

```bash
python pipeline/s2_mosaic.py MOSAIC-S2-BRAZIL-3M_V1_20250701_20250930_P20250916 work_s2
gcloud storage cp work_s2/MOSAIC-S2-BRAZIL-3M_V1_20250701_20250930_P20250916_ACRE.tif \
  gs://brazilian-remote-stac-cog-cbers4a-wpm/s2-mosaic/v1/2025/MOSAIC-S2-BRAZIL-3M_V1_20250701_20250930_ACRE.tif \
  "--custom-metadata=source=https://data.inpe.br/bdc/data/s2/s2-3m/v1/2025/MOSAIC-S2-BRAZIL-3M_V1_20250701_20250930_P20250916_VISUAL.tif,attribution=INPE - Brazil Data Cube / contains modified Copernicus Sentinel data 2025,modifications=Clipped to Acre and recompressed to JPEG quality 85 (YCbCr with internal mask)"
```

The clip reads about 3 GB from data.inpe.br (about 20 minutes) and writes a
0.37 GB COG; `gcloud` splits metadata on commas, so the values have none.

A scene takes about 15-25 minutes:

- 4-13 min to download (capped at 20 MB/s by default, to stay gentle with
  data.inpe.br);
- 7-9 min to compress;
- 1-4 min to upload.

`gcloud` calls are retried for about 5 minutes, so a network blip does not
cost a recompression. Failed scenes are logged and retried on the next run.
Partial downloads resume.

## Deploy

```bash
cd titiler-acre
gcloud run deploy titiler-acre --source . --region southamerica-east1 --project <PROJECT_ID> \
  --service-account titiler-acre-run@<PROJECT_ID>.iam.gserviceaccount.com --allow-unauthenticated \
  --memory 2Gi --cpu 2 --concurrency 20 --timeout 120 --min-instances 0 --max-instances 1 --max 1
```

- `--source .` builds the `Dockerfile` with Cloud Build. `.gcloudignore`
  keeps `.venv/`, `pipeline/`, `work*/` and `wmts_links.csv` out of the upload.
- The base image digest and `requirements.txt` hold what was installed in the
  image tested on 2026-10-30, so a rebuild gets the same packages. Update them
  on purpose, for example for a security patch, then run
  `pipeline/check_service.py` against the new revision.
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
- **Costs:** storage is the main one: ~38 GiB Standard in
  southamerica-east1, about US$ 1.35 a month. Cloud Run scales to zero and
  light use stays in the free tier. Reads from the bucket in the same region
  carry no egress charge. A budget alert watches the project; it notifies, it
  does not cap spending.

## Licences

What each source's own catalogue or service says, read on 2026-10-31. It is a
reading of the metadata, not legal advice; check with a lawyer before
commercial use.

| Source | Licence found | Where | Commercial use |
|---|---|---|---|
| CBERS-4A WPM, fused (INPE/BDC) | CC BY 4.0 | `license` of the BDC STAC collection | Yes, with attribution; say the copies were changed |
| INPE Sentinel-2 mosaic | "proprietary", pointing to the Copernicus Sentinel Data Legal Notice | BDC STAC collection | Under that notice (its text was not read here) |
| EOX Sentinel-2 cloudless 2025 | CC BY-NC-SA 4.0 | EOX's release notes and pricing page | **No.** EOX sells a commercial licence (cloudless.eox.at) |
| OpenStreetMap layers | ODbL | not rechecked | Yes, with attribution; share-alike for derived databases |
| IBAMA embargoes | catalogue: "Outra (Aberta)" for the CSV dataset, ODbL for the polygon shapefile dataset | `dadosabertos.ibama.gov.br` CKAN API | Yes per the catalogue; ODbL share-alike for the shapefile |
| CNUC, MMA | CC BY | `dados.mma.gov.br` CKAN API | Yes, with attribution |
| CAR (SICAR's WFS) | none stated | CMRI decision 217/2022: public CAR data are available through the public consultation and the WMS/WFS geoserver, and says nothing about reuse | Not licensed; ask SFB |
| Fire hotspots (INPE Queimadas) | none stated | the portal's open-data page and the two documentation PDFs mention no licence or terms | Not licensed; ask INPE |
| Settlements (INCRA via INPE) | none found | INPE's geoserver states no constraints; INCRA's servers were unreachable | Not licensed; ask INCRA/INPE |
| PRODES, DETER, indigenous lands (TerraBrasilis) | secondary sources say CC BY-SA 4.0; not confirmed | TerraBrasilis pages are not readable without a browser | Unconfirmed; CC BY-SA would be share-alike |
| IBGE boundaries | no explicit licence found | search only | Unconfirmed |
| ANAC aerodromes, FUNAI | not checked | INDE's service lists "no constraints", which is not a licence | Unconfirmed |

Share-alike (ODbL, and CC BY-SA if confirmed) matters if the map or its data
are published; for internal use it matters less.

## Attribution

Imagery: INPE / Brazil Data Cube, `CB4A-WPM-PCA-FUSED`,
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The copies served
here are linearly stretched per band and recompressed (lossy JPEG quality
85), as recorded in each object's metadata and in the service's attribution
text.

Background layers:

- INPE / Brazil Data Cube, `mosaic-s2-brazil-3m-1`, contains modified
  Copernicus Sentinel data 2025. The copy here is clipped to Acre and
  recompressed.
- Sentinel-2 cloudless - https://s2maps.eu by EOX IT Services GmbH (Contains
  modified Copernicus Sentinel data 2025),
  [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).

Hybrid map layers:

- © OpenStreetMap contributors,
  [ODbL](https://opendatacommons.org/licenses/odbl/): roads, streets, rivers,
  schools and health units.
- INPE, PRODES and DETER, via TerraBrasilis; indigenous lands (FUNAI) and
  municipal and state boundaries (IBGE) as published by TerraBrasilis.
- Ministério do Meio Ambiente, Cadastro Nacional de Unidades de Conservação
  (CNUC), July 2026.
- Ministério da Infraestrutura / ANAC, registered public and private
  aerodromes.
- Serviço Florestal Brasileiro, SICAR (Cadastro Ambiental Rural), public
  consultation service; IBAMA open data, embargoed areas (without personal
  data); INCRA settlements and INPE Queimadas fire hotspots, as published by
  INPE's BDQueimadas geoserver. See [Licences](#licences) for their terms.
