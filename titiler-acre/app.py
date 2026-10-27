"""TiTiler service exposing INPE CBERS-4A WPM COGs as WMTS for Atlas."""

from pathlib import PurePosixPath
from typing import Annotated
from urllib.parse import unquote, urlsplit

import morecantile
from fastapi import FastAPI, HTTPException, Query
from morecantile import TileMatrixSets
from starlette.middleware.cors import CORSMiddleware

from titiler.core import __version__ as titiler_version
from titiler.core.errors import DEFAULT_STATUS_CODES, add_exception_handlers
from titiler.core.factory import TilerFactory
from titiler.core.middleware import CacheControlMiddleware
from titiler.extensions import wmtsExtension

ALLOWED_PREFIX = "https://data.inpe.br/bdc/data/CB4A-WPM-PCA-FUSED/"


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
    extensions=[wmtsExtension(layer_identifier_provider=scene_identifier)],
    router_prefix="/cog",
)
app.include_router(cog.router, prefix="/cog", tags=["Cloud Optimized GeoTIFF"])

# Map rio-tiler errors (e.g. tile outside the scene) to 4xx instead of 500.
add_exception_handlers(app, DEFAULT_STATUS_CODES)

# Let browsers reuse tiles; only successful responses are cached.
app.add_middleware(
    CacheControlMiddleware,
    cachecontrol="public, max-age=86400",
    cachecontrol_max_http_code=400,
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
