"""The /docs and /redoc pages (only with API_DOCS_ENABLED=true, and behind the login).

FastAPI's own pages load a floating ``swagger-ui-dist@5`` / ``redoc@2`` from jsDelivr without
Subresource Integrity, plus an inline start script. These pages pin exact versions with SRI
hashes (checked against the npm tarballs) and start Swagger UI from /static, so the browser
refuses any other file the CDN might serve.
"""

from __future__ import annotations

from html import escape

from starlette.responses import HTMLResponse

CDN = "https://cdn.jsdelivr.net/npm"
# npm swagger-ui-dist@5.17.14 (tarball integrity sha512-CVbSfaLpstV65OnSjbXfVd6Sta3q3F7Cj/yYuv
# HMp1P90LztOLs6PfUnKEVAeiIVQt9u2SaPwv0LiH/OyMjHRw==) and redoc@2.1.5. To update: download the
# new files from the npm tarball and replace version and sha384 hashes together.
SWAGGER_JS = (
    f"{CDN}/swagger-ui-dist@5.17.14/swagger-ui-bundle.js",
    "sha384-wmyclcVGX/WhUkdkATwhaK1X1JtiNrr2EoYJ+diV3vj4v6OC5yCeSu+yW13SYJep",
)
SWAGGER_CSS = (
    f"{CDN}/swagger-ui-dist@5.17.14/swagger-ui.css",
    "sha384-wxLW6kwyHktdDGr6Pv1zgm/VGJh99lfUbzSn6HNHBENZlCN7W602k9VkGdxuFvPn",
)
REDOC_JS = (
    f"{CDN}/redoc@2.1.5/bundles/redoc.standalone.js",
    "sha384-0GrsyTQc9Oqd8h+b2dbc4XdR2T/DYpy0tLNNstyx+LBMUyiBbcWPbEs9aRmUcaxD",
)


def _script(src: tuple[str, str]) -> str:
    return f'<script src="{src[0]}" integrity="{src[1]}" crossorigin="anonymous"></script>'


def swagger_page(title: str, openapi_url: str) -> HTMLResponse:
    css_url, css_hash = SWAGGER_CSS
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{escape(title)} - API</title>"
        f'<link rel="stylesheet" href="{css_url}" integrity="{css_hash}" '
        'crossorigin="anonymous"></head><body>'
        f'<div id="swagger-ui" data-openapi-url="{escape(openapi_url)}"></div>'
        f'{_script(SWAGGER_JS)}<script src="/static/api-docs.js"></script></body></html>'
    )


def redoc_page(title: str, openapi_url: str) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{escape(title)} - API</title></head><body>"
        f'<redoc spec-url="{escape(openapi_url)}"></redoc>{_script(REDOC_JS)}</body></html>'
    )
