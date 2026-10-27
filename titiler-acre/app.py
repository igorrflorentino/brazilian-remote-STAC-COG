"""TiTiler service exposing INPE CBERS-4A WPM COGs as WMTS for Atlas."""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated
from urllib.parse import unquote, urlsplit

import morecantile
from fastapi import FastAPI, HTTPException, Query, Request, status
from morecantile import TileMatrixSets
from pydantic import Field
from rasterio.errors import RasterioIOError
from starlette.middleware.cors import CORSMiddleware

from titiler.core import __version__ as titiler_version
from titiler.core.dependencies import PartFeatureParams, PreviewParams
from titiler.core.errors import (
    DEFAULT_STATUS_CODES,
    add_exception_handlers,
    exception_handler_factory,
)
from titiler.core.factory import TilerFactory
from titiler.core.middleware import CacheControlMiddleware
from titiler.extensions import wmtsExtension

ALLOWED_PREFIX = "https://data.inpe.br/bdc/data/CB4A-WPM-PCA-FUSED/"

# Largest output (pixels per side) for preview, bbox, feature and statistics.
# A 2 m scene is ~56k px wide, so uncapped requests could exhaust the 2 GiB instance.
MAX_IMAGE_SIZE = 2048

# GDAL messages meaning the COG is not on data.inpe.br (wrong path or extension).
NOT_FOUND_MARKERS = ("HTTP response code: 404", "does not exist in the file system")


def InpeDatasetPathParams(
    url: Annotated[
        str,
        Query(description=f"CBERS-4A WPM COG URL, must start with {ALLOWED_PREFIX}"),
    ],
) -> str:
    """Accept only COGs from the INPE collection so the service is not an open proxy."""
    # Reject ".." (also double-encoded) so a URL cannot climb out of the prefix.
    if not url.startswith(ALLOWED_PREFIX) or ".." in unquote(url):
        raise HTTPException(
            status_code=403,
            detail=f"Only URLs starting with {ALLOWED_PREFIX} are allowed",
        )
    return url


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


def scene_identifier(src_path: str) -> str:
    """Use the COG file name (without .tif) as the WMTS layer identifier."""
    return PurePosixPath(urlsplit(src_path).path).stem


app = FastAPI(
    title="titiler-acre",
    description="WMTS for INPE CBERS-4A WPM 2 m COGs (CB4A-WPM-PCA-FUSED).",
    version=titiler_version,
)

cog = TilerFactory(
    path_dependency=InpeDatasetPathParams,
    # Atlas works in Web Mercator; a single TMS keeps one layer per scene and
    # avoids reopening the COG once per grid on every GetCapabilities.
    supported_tms=TileMatrixSets(
        {"WebMercatorQuad": morecantile.tms.get("WebMercatorQuad")}
    ),
    img_preview_dependency=CappedPreviewParams,
    img_part_dependency=CappedPartFeatureParams,
    extensions=[wmtsExtension(layer_identifier_provider=scene_identifier)],
    router_prefix="/cog",
)
app.include_router(cog.router, prefix="/cog", tags=["Cloud Optimized GeoTIFF"])

# Map rio-tiler errors (e.g. tile outside the scene) to 4xx instead of 500.
add_exception_handlers(app, DEFAULT_STATUS_CODES)

_cog_not_found = exception_handler_factory(status.HTTP_404_NOT_FOUND, RasterioIOError)
_cog_io_error = exception_handler_factory(
    status.HTTP_500_INTERNAL_SERVER_ERROR, RasterioIOError
)


def rasterio_io_error_handler(request: Request, exc: RasterioIOError):
    """Return 404 when the COG is missing on data.inpe.br, 500 for other I/O errors."""
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
        "wmts": "/cog/WMTSCapabilities.xml?url=<encoded COG URL>&bidx=1&bidx=2&bidx=3",
    }


# Cloud Run reserves paths ending in "z" on *.run.app, so /health is the
# endpoint to probe there; /healthz still works locally and on custom domains.
@app.get("/healthz", tags=["Status"])
@app.get("/health", tags=["Status"])
def health() -> dict:
    """Health check."""
    return {"status": "ok"}
