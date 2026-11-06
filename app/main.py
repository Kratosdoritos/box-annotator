"""
Box Annotator — FastAPI entry point.

Run with:  uvicorn app.main:app --reload   then open http://127.0.0.1:8000
"""

import ipaddress
import os
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from . import routes, workspace

# extra host names the app may be opened by, e.g. "mypc,mypc.local" to use it from another computer
ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("ANNOTATOR_ALLOWED_HOSTS", "").split(",") if h.strip()}
# no other site may show the app inside its own page: requests from that frame would count as
# same-origin, and a page laid over it could trick the user into clicking Clear (clickjacking)
NO_FRAMING = [(b"x-frame-options", b"DENY"), (b"content-security-policy", b"frame-ancestors 'none'")]


def host_allowed(host: str) -> bool:
    """
    localhost, an IP address, or a name in ANNOTATOR_ALLOWED_HOSTS. Any other name
    means a web page pointed its own domain at this computer (DNS rebinding) to
    read the images, so it's refused.
    """
    name = host[1:host.find("]")] if host.startswith("[") else host.split(":")[0]
    name = name.lower()
    if name == "localhost" or name in ALLOWED_HOSTS:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def from_other_site(scope: dict, headers: dict) -> bool:
    """
    True for an API request another website's page made through the user's
    browser, e.g. a hidden form posting to /api/workspace/clear. Browsers mark
    those with Sec-Fetch-Site, and send an Origin with every POST/PUT/PATCH/DELETE.
    Requests from scripts or curl carry neither, and are let through.
    """
    if not scope["path"].startswith("/api/"):
        return False   # the UI page itself may be opened from a link anywhere
    site = headers.get(b"sec-fetch-site", b"").decode("latin-1")
    if site and site not in ("same-origin", "none"):   # none: typed into the address bar
        return True
    origin = headers.get(b"origin", b"").decode("latin-1")
    if origin and scope["method"] not in ("GET", "HEAD"):
        return urlsplit(origin).netloc.lower() != headers.get(b"host", b"").decode("latin-1").lower()
    return False


class CheckHost:
    """
    ASGI middleware that refuses requests addressed to a host name the app
    doesn't know and API requests made by other websites, and keeps the app
    out of other sites' frames.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_unframed(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *NO_FRAMING]
            await send(message)

        headers = dict(scope["headers"])
        host = headers.get(b"host", b"").decode("latin-1")
        reply = None
        if not host_allowed(host):
            reply = PlainTextResponse("Unknown host name; open the app at http://127.0.0.1 "
                                      "or add the name to ANNOTATOR_ALLOWED_HOSTS", status_code=400)
        elif from_other_site(scope, headers):
            reply = PlainTextResponse("Requests from other websites are refused", status_code=403)
        await (reply or self.app)(scope, receive, send_unframed)


app = FastAPI(
    title="Box Annotator",
    description="Draw labelled bounding boxes on a folder of images and export them as a COCO or YOLO dataset.",
    version="2.2.1",
)
app.include_router(routes.router)
app.include_router(workspace.router)
app.add_middleware(CheckHost)
