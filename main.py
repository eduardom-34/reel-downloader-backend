import json
import os
import re
import shutil
import tempfile
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from starlette.background import BackgroundTask
import yt_dlp

logger = logging.getLogger("reel-downloader")
logging.basicConfig(level=logging.INFO)

COOKIES_FILE = Path(__file__).parent / "cookies.txt"

INSTAGRAM_REEL_PATTERN = re.compile(
    r"https?://(www\.)?instagram\.com/(reel|reels)/[\w-]+/?",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if COOKIES_FILE.exists():
        logger.info("cookies.txt found — Instagram authentication enabled.")
    else:
        logger.warning(
            "cookies.txt not found. Only public reels will be accessible. "
            "Export your Instagram cookies with a browser extension and place "
            "the file at: %s",
            COOKIES_FILE,
        )
    yield


app = FastAPI(
    title="Instagram Reel Downloader API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # Cookies travel in the request body, not as browser credentials. Keeping
    # this False lets the wildcard origin actually work in the browser.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ReelRequest(BaseModel):
    url: str
    cookies: str | None = None

    @field_validator("url")
    @classmethod
    def validate_instagram_url(cls, v: str) -> str:
        if not INSTAGRAM_REEL_PATTERN.match(v):
            raise ValueError(
                "URL must be a valid Instagram Reel link "
                "(e.g. https://www.instagram.com/reel/ABC123/)"
            )
        return v


class ReelInfo(BaseModel):
    title: str | None = None
    thumbnail: str | None = None
    duration: float | None = None
    video_url: str | None = None
    uploader: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _YtDlpLogger:
    """Routes yt-dlp's internal diagnostics into our logger.

    With quiet=True and no_warnings=True but no logger set, yt-dlp swallows
    every intermediate warning it emits while probing Instagram (missing
    csrftoken, the direct sessionid API call failing with its real HTTP
    status, "Instagram API is not granting access", etc.) — exactly the
    detail needed to tell "cookies invalid" apart from "IP/session flagged
    by Instagram" apart from "post genuinely private". Setting this logger
    makes yt-dlp forward everything here instead of discarding it, and it
    bypasses no_warnings/quiet entirely (see YoutubeDL.report_warning).
    """

    def debug(self, msg: str) -> None:
        logger.info("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        logger.info("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        logger.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        logger.error("yt-dlp: %s", msg)


def _base_yt_dlp_opts() -> dict:
    """Return yt-dlp options shared across endpoints."""
    return {
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "logger": _YtDlpLogger(),
    }


NETSCAPE_HEADER = "# Netscape HTTP Cookie File\n"

# yt-dlp requires an explicit expiry; browsers report session cookies as 0/absent.
DEFAULT_EXPIRY = 2145916800  # 2038-01-01

# Cookies Instagram actually needs for an authenticated request.
REQUIRED_COOKIES = {"sessionid"}

# Not strictly required (the direct sessionid API call can succeed without
# it), but if that call fails/expires mid-session, yt-dlp falls back to a
# GraphQL request that needs csrftoken — without it that fallback silently
# degrades to "login required" even though sessionid looked fine.
RECOMMENDED_COOKIES = {"sessionid", "csrftoken"}


def _netscape_line(
    name: str,
    value: str,
    domain: str = ".instagram.com",
    path: str = "/",
    secure: bool = True,
    expiry: int = DEFAULT_EXPIRY,
) -> str:
    """Build one tab-separated Netscape cookie record."""
    if not domain.startswith("."):
        domain = "." + domain.lstrip(".")
    return "\t".join([
        domain,
        "TRUE",                     # include subdomains
        path or "/",
        "TRUE" if secure else "FALSE",
        str(int(expiry or DEFAULT_EXPIRY)),
        name,
        value,
    ])


def _from_header_string(cookies: str) -> list[str]:
    """Parse a `document.cookie` style string: `name=value; name2=value2`."""
    lines = []
    for pair in cookies.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name, value = name.strip(), value.strip()
        if name:
            lines.append(_netscape_line(name, value))
    return lines


def _from_json_export(cookies: str) -> list[str] | None:
    """Parse a JSON export (Cookie-Editor / EditThisCookie browser extensions)."""
    try:
        data = json.loads(cookies)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        data = data.get("cookies", data)
    if not isinstance(data, list):
        return None

    lines = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        expiry = item.get("expirationDate") or item.get("expires") or DEFAULT_EXPIRY
        try:
            expiry = int(float(expiry))
        except (ValueError, TypeError):
            expiry = DEFAULT_EXPIRY
        lines.append(_netscape_line(
            name=str(name),
            value=str(item.get("value", "")),
            domain=str(item.get("domain") or ".instagram.com"),
            path=str(item.get("path") or "/"),
            secure=bool(item.get("secure", True)),
            expiry=expiry,
        ))
    return lines or None


def _from_netscape_text(cookies: str) -> list[str]:
    """Re-tab a Netscape file whose tabs were collapsed into spaces in transit."""
    lines = []
    for raw in cookies.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        # Preserve comments except the header (re-added by the caller). The
        # `#HttpOnly_` prefix is a real record, not a comment.
        if line.lstrip().startswith("#") and not line.lstrip().startswith("#HttpOnly_"):
            continue
        if line.count("\t") == 6:
            lines.append(line)
            continue
        # maxsplit=6 keeps any whitespace inside the cookie value intact.
        parts = line.split(None, 6)
        if len(parts) == 7:
            lines.append("\t".join(parts))
        else:
            logger.warning("skipping unparseable cookie line (%d fields)", len(parts))
    return lines


def _normalize_cookies(cookies: str) -> tuple[str, list[str]]:
    """Convert any supported cookie format into a Netscape file body.

    Returns the file contents and the list of cookie names found. yt-dlp only
    accepts tab-separated Netscape files with the magic header line; anything
    else either raises "does not look like a Netscape format cookies file" or,
    worse, loads silently with zero cookies.
    """
    text = cookies.strip()
    if not text:
        return "", []

    lines = _from_json_export(text)
    fmt = "json"
    if lines is None:
        # A header string is a single line of `k=v` pairs with no tabs.
        if "\n" not in text and "\t" not in text and "=" in text:
            lines, fmt = _from_header_string(text), "header"
        else:
            lines, fmt = _from_netscape_text(text), "netscape"

    names = [line.split("\t")[5] for line in lines if line.count("\t") == 6]
    logger.info(
        "cookies normalized: detected_format=%s, records=%d, names=%s",
        fmt, len(lines), sorted(set(names))[:15],
    )

    missing = REQUIRED_COOKIES - set(names)
    if missing:
        logger.warning(
            "cookies are missing %s — Instagram will treat the request as "
            "logged out and reject private or age-restricted reels.",
            ", ".join(sorted(missing)),
        )

    missing_recommended = (RECOMMENDED_COOKIES - set(names)) - missing
    if missing_recommended:
        logger.warning(
            "cookies are missing %s — sessionid is present, but if Instagram's "
            "direct media-info call rejects it (expired/flagged session), the "
            "GraphQL fallback needs this cookie too and will otherwise report "
            "'login required' even though sessionid looked valid.",
            ", ".join(sorted(missing_recommended)),
        )

    return NETSCAPE_HEADER + "\n".join(lines) + "\n", names


def _write_temp_cookies(cookies_str: str) -> str:
    """Write cookie string to a temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="cookies_")
    with os.fdopen(fd, "w") as f:
        f.write(cookies_str)
    return path


def _apply_cookies(opts: dict, cookies: str | None) -> str | None:
    """Add cookie file to yt-dlp opts. Returns temp file path to clean up, or None."""
    if cookies and cookies.strip():
        logger.info("cookies received: %d chars", len(cookies))
        netscape, names = _normalize_cookies(cookies)
        if not names:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No se pudo leer ninguna cookie. Envía las cookies en "
                    "formato Netscape (cookies.txt), como JSON exportado por "
                    "una extensión, o como cadena 'nombre=valor; nombre2=valor2'."
                ),
            )
        tmp_path = _write_temp_cookies(netscape)
        opts["cookiefile"] = tmp_path
        return tmp_path

    logger.info("cookies received: none (request.cookies was empty/null)")
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    return None


def _cleanup(path: str | None) -> None:
    """Remove a temporary file if it exists."""
    if path and os.path.isfile(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def _download_error(exc: yt_dlp.utils.DownloadError) -> HTTPException:
    """Map a yt-dlp failure onto a status code the client can act on."""
    detail = str(exc)
    low = detail.lower()

    if "netscape format" in low:
        return HTTPException(
            status_code=400,
            detail=(
                "El archivo de cookies no tiene un formato válido. "
                "Expórtalo de nuevo en formato Netscape (cookies.txt)."
            ),
        )
    if "rate-limit" in low or "429" in low or "please wait a few minutes" in low:
        return HTTPException(
            status_code=429,
            detail="Instagram está limitando las peticiones. Intenta de nuevo en unos minutos.",
        )
    if (
        "login" in low
        or "private" in low
        or "cookies" in low
        or "empty media response" in low
        or "requested content is not available" in low
    ):
        return HTTPException(
            status_code=403,
            detail=(
                "Instagram rechazó la petición: el reel es privado o las cookies "
                "no son válidas/expiraron. Vuelve a iniciar sesión y expórtalas otra vez."
            ),
        )
    return HTTPException(status_code=404, detail=f"Could not fetch reel: {detail}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"status": "ok", "message": "Instagram Reel Downloader API"}


@app.post("/info", response_model=ReelInfo)
async def get_reel_info(request: ReelRequest):
    """Extract metadata for an Instagram Reel without downloading it."""
    logger.info("/info request: url=%s", request.url)
    opts = _base_yt_dlp_opts()
    tmp_cookie = _apply_cookies(opts, request.cookies)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(request.url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        logger.warning("yt-dlp failed for /info: %s", exc)
        raise _download_error(exc)
    except Exception as exc:
        logger.exception("unexpected error in /info")
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")
    finally:
        _cleanup(tmp_cookie)

    return ReelInfo(
        title=info.get("title") or info.get("fulltitle"),
        thumbnail=info.get("thumbnail"),
        duration=info.get("duration"),
        video_url=info.get("url"),
        uploader=info.get("uploader"),
    )


@app.post("/download")
async def download_reel(request: ReelRequest):
    """Download an Instagram Reel and stream the MP4 file back."""
    logger.info("/download request: url=%s", request.url)
    opts = _base_yt_dlp_opts()
    tmp_cookie = _apply_cookies(opts, request.cookies)

    tmp_dir = tempfile.mkdtemp(prefix="reel_")
    opts["outtmpl"] = os.path.join(tmp_dir, "%(id)s.%(ext)s")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(request.url, download=True)

        video_id = info.get("id", "reel")
        ext = info.get("ext", "mp4")
        downloaded_file = os.path.join(tmp_dir, f"{video_id}.{ext}")

        if not os.path.isfile(downloaded_file):
            files = os.listdir(tmp_dir)
            if files:
                downloaded_file = os.path.join(tmp_dir, files[0])
            else:
                raise HTTPException(
                    status_code=500, detail="Download succeeded but file not found."
                )
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except yt_dlp.utils.DownloadError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.warning("yt-dlp failed for /download: %s", exc)
        raise _download_error(exc)
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.exception("unexpected error in /download")
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")
    finally:
        _cleanup(tmp_cookie)

    title = info.get("title") or info.get("fulltitle") or video_id
    safe_title = re.sub(r'[^\w\s-]', '', title).strip()[:80]
    filename = f"{safe_title}.{ext}" if safe_title else f"{video_id}.{ext}"

    return FileResponse(
        path=downloaded_file,
        media_type=f"video/{ext}",
        filename=filename,
        # Without this the temp dir leaks on every download — on Cloud Run /tmp
        # is a tmpfs, so the container grows until it is OOM-killed.
        background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True),
    )


# ---------------------------------------------------------------------------
# Run with: uvicorn main:app --reload
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("RELOAD", "false").lower() == "true"
    uvicorn.run("main:app", host=host, port=port, reload=reload)
