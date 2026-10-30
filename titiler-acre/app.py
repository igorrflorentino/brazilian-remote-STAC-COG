"""TiTiler service exposing CBERS-4A WPM COGs as WMTS for Atlas.

The COGs are JPEG copies of INPE's CB4A-WPM-PCA-FUSED scenes (CC BY 4.0,
INPE / Brazil Data Cube) kept in a private bucket and read through /vsigs/
with the Cloud Run service account.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated
from urllib.parse import unquote, urlsplit

import morecantile
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from morecantile import TileMatrixSets
from pydantic import Field
from rasterio.errors import RasterioIOError
from rio_tiler.profiles import img_profiles
from rio_tiler.types import RIOResampling, WarpResampling
from starlette.middleware.cors import CORSMiddleware

from titiler.core import __version__ as titiler_version
from titiler.core.dependencies import (
    DatasetParams,
    PartFeatureParams,
    PreviewParams,
    TileParams,
)
from titiler.core.errors import (
    DEFAULT_STATUS_CODES,
    add_exception_handlers,
    exception_handler_factory,
)
from titiler.core.factory import TilerFactory
from titiler.core.middleware import CacheControlMiddleware
from titiler.extensions import wmtsExtension

ALLOWED_PREFIX = "gs://brazilian-remote-stac-cog-cbers4a-wpm/"

# CC BY 4.0 asks to credit the source and to say the material was changed.
ATTRIBUTION = (
    "CBERS-4A WPM imagery: INPE / Brazil Data Cube (CB4A-WPM-PCA-FUSED, CC BY 4.0), "
    "linearly stretched per band and recompressed to JPEG quality 85 for this service"
)

# Largest output (pixels per side) for preview, bbox, feature and statistics.
# A 2 m scene is ~56k px wide, so uncapped requests could exhaust the 2 GiB instance.
MAX_IMAGE_SIZE = 2048

# Largest tile side and tile buffer/padding. Atlas asks for 256 px tiles; 1024
# leaves room for high-DPI clients while one tile stays a few MB of memory.
MAX_TILE_SIZE = 1024
MAX_TILE_BUFFER = 64

# WebP tiles are lossless: the same pixels as PNG at 35-55 % of the size
# (rio-tiler's default WebP is lossy q75). make_links.py asks for tile_format=webp.
img_profiles.data["webp"].update(lossless=True)

# GDAL messages meaning the COG is not in the bucket (wrong path or extension).
NOT_FOUND_MARKERS = ("HTTP response code: 404", "does not exist in the file system")


def BucketDatasetPathParams(
    url: Annotated[
        str,
        Query(description=f"CBERS-4A WPM COG URL, must start with {ALLOWED_PREFIX}"),
    ],
) -> str:
    """Accept only COGs from the bucket so the service is not an open proxy."""
    # Reject ".." (also double-encoded) so a URL cannot climb out of the prefix.
    if not url.startswith(ALLOWED_PREFIX) or ".." in unquote(url):
        raise HTTPException(
            status_code=403,
            detail=f"Only URLs starting with {ALLOWED_PREFIX} are allowed",
        )
    return url


@dataclass
class BilinearDatasetParams(DatasetParams):
    """Bilinear resampling by default instead of nearest.

    Tiles rarely fall on the 2 m grid (z16 is ~2.36 m over Acre), and nearest
    drops or repeats source pixels. Against an area-averaged reference,
    bilinear was 0.8 / 1.8 / 6.5 dB closer at z12 / z14 / z16, for ~0.05 s
    more per tile. A server default keeps the existing WMTS links unchanged.
    """

    resampling_method: Annotated[
        RIOResampling | None,
        Query(alias="resampling", description="RasterIO resampling algorithm. Defaults to `bilinear`."),
    ] = "bilinear"
    reproject_method: Annotated[
        WarpResampling | None,
        Query(alias="reproject", description="WarpKernel resampling algorithm (only used when doing re-projection). Defaults to `bilinear`."),
    ] = "bilinear"


@dataclass
class CappedPreviewParams(PreviewParams):
    """Preview/statistics sizes capped at MAX_IMAGE_SIZE."""

    max_size: Annotated[
        int,
        Field(description="Maximum image size to read onto.", ge=1, le=MAX_IMAGE_SIZE),
    ] = 1024
    height: Annotated[
        int | None,
        Field(description="Force output image height.", ge=1, le=MAX_IMAGE_SIZE),
    ] = None
    width: Annotated[
        int | None,
        Field(description="Force output image width.", ge=1, le=MAX_IMAGE_SIZE),
    ] = None


@dataclass
class CappedPartFeatureParams(PartFeatureParams):
    """Bbox/feature sizes capped at MAX_IMAGE_SIZE."""

    # Upstream defaults to None, which reads the area at native 2 m resolution.
    max_size: Annotated[
        int,
        Field(description="Maximum image size to read onto.", ge=1, le=MAX_IMAGE_SIZE),
    ] = 1024
    height: Annotated[
        int | None,
        Field(description="Force output image height.", ge=1, le=MAX_IMAGE_SIZE),
    ] = None
    width: Annotated[
        int | None,
        Field(description="Force output image width.", ge=1, le=MAX_IMAGE_SIZE),
    ] = None


@dataclass
class CappedTileParams(TileParams):
    """Tile buffer/padding capped at MAX_TILE_BUFFER."""

    buffer: Annotated[
        float | None,
        Query(description="Buffer on each side of the tile (multiple of 0.5).", gt=0, le=MAX_TILE_BUFFER),
    ] = None
    padding: Annotated[
        int | None,
        Query(description="Padding to apply to each tile edge.", gt=0, le=MAX_TILE_BUFFER),
    ] = None


def TileSizeLimit(
    tilesize: Annotated[
        int | None,
        Query(description="Tilesize in pixels.", gt=0, le=MAX_TILE_SIZE),
    ] = None,
) -> None:
    """Cap tilesize, which TilerFactory declares on the tile routes themselves."""


def scene_identifier(src_path: str) -> str:
    """Use the COG file name (without .tif) as the WMTS layer identifier."""
    return PurePosixPath(urlsplit(src_path).path).stem


app = FastAPI(
    title="titiler-acre",
    description=f"WMTS for CBERS-4A WPM 2 m COGs. {ATTRIBUTION}.",
    version=titiler_version,
)

cog = TilerFactory(
    path_dependency=BucketDatasetPathParams,
    dataset_dependency=BilinearDatasetParams,
    # Atlas works in Web Mercator; a single TMS keeps one layer per scene and
    # avoids reopening the COG once per grid on every GetCapabilities.
    supported_tms=TileMatrixSets(
        {"WebMercatorQuad": morecantile.tms.get("WebMercatorQuad")}
    ),
    img_preview_dependency=CappedPreviewParams,
    img_part_dependency=CappedPartFeatureParams,
    tile_dependency=CappedTileParams,
    extensions=[wmtsExtension(layer_identifier_provider=scene_identifier)],
    router_prefix="/cog",
)
# Router-wide, so tilejson/map links cannot carry an oversized tilesize either.
app.include_router(
    cog.router,
    prefix="/cog",
    tags=["Cloud Optimized GeoTIFF"],
    dependencies=[Depends(TileSizeLimit)],
)

# Map rio-tiler errors (e.g. tile outside the scene) to 4xx instead of 500.
add_exception_handlers(app, DEFAULT_STATUS_CODES)

_cog_not_found = exception_handler_factory(status.HTTP_404_NOT_FOUND, RasterioIOError)
_cog_io_error = exception_handler_factory(
    status.HTTP_500_INTERNAL_SERVER_ERROR, RasterioIOError
)


def rasterio_io_error_handler(request: Request, exc: RasterioIOError):
    """Return 404 when the COG is missing from the bucket, 500 for other I/O errors."""
    if any(marker in str(exc) for marker in NOT_FOUND_MARKERS):
        return _cog_not_found(request, exc)
    return _cog_io_error(request, exc)


# Registered after add_exception_handlers so it replaces the default 500 mapping.
app.add_exception_handler(RasterioIOError, rasterio_io_error_handler)

# Let browsers reuse tiles; only successful responses are cached, and the
# status endpoints are excluded so they always reflect the live service.
app.add_middleware(
    CacheControlMiddleware,
    cachecontrol="public, max-age=86400",
    cachecontrol_max_http_code=400,
    exclude_path={r"/$", r"/health"},
)
# Atlas draws tiles in a WebGL canvas, which requires CORS headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/", tags=["Status"])
def root() -> dict:
    """Service status."""
    return {
        "service": "titiler-acre",
        "status": "ok",
        "titiler_version": titiler_version,
        "allowed_prefix": ALLOWED_PREFIX,
        "attribution": ATTRIBUTION,
        "wmts": "/cog/WMTSCapabilities.xml?url=<encoded COG URL>&bidx=1&bidx=2&bidx=3",
    }


# Cloud Run reserves paths ending in "z" on *.run.app, so /health is the
# endpoint to probe there; /healthz still works locally and on custom domains.
@app.get("/healthz", tags=["Status"])
@app.get("/health", tags=["Status"])
def health() -> dict:
    """Health check."""
    return {"status": "ok"}
