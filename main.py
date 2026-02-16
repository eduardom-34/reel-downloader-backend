import os
import re
import tempfile
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ReelRequest(BaseModel):
    url: str

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

def _base_yt_dlp_opts() -> dict:
    """Return yt-dlp options shared across endpoints."""
    opts: dict = {
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"status": "ok", "message": "Instagram Reel Downloader API"}


@app.post("/info", response_model=ReelInfo)
async def get_reel_info(request: ReelRequest):
    """Extract metadata for an Instagram Reel without downloading it."""
    opts = _base_yt_dlp_opts()

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(request.url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        detail = str(exc)
        if "private" in detail.lower() or "login" in detail.lower():
            raise HTTPException(
                status_code=403,
                detail="This reel is private or requires authentication.",
            )
        raise HTTPException(status_code=404, detail=f"Could not fetch reel: {detail}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")

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
    tmp_dir = tempfile.mkdtemp(prefix="reel_")
    output_template = os.path.join(tmp_dir, "%(id)s.%(ext)s")

    opts = _base_yt_dlp_opts()
    opts["outtmpl"] = output_template

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(request.url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        detail = str(exc)
        if "private" in detail.lower() or "login" in detail.lower():
            raise HTTPException(
                status_code=403,
                detail="This reel is private or requires authentication.",
            )
        raise HTTPException(status_code=404, detail=f"Could not fetch reel: {detail}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")

    video_id = info.get("id", "reel")
    ext = info.get("ext", "mp4")
    downloaded_file = os.path.join(tmp_dir, f"{video_id}.{ext}")

    if not os.path.isfile(downloaded_file):
        files = os.listdir(tmp_dir)
        if files:
            downloaded_file = os.path.join(tmp_dir, files[0])
        else:
            raise HTTPException(status_code=500, detail="Download succeeded but file not found.")

    title = info.get("title") or info.get("fulltitle") or video_id
    safe_title = re.sub(r'[^\w\s-]', '', title).strip()[:80]
    filename = f"{safe_title}.{ext}" if safe_title else f"{video_id}.{ext}"

    return FileResponse(
        path=downloaded_file,
        media_type=f"video/{ext}",
        filename=filename,
        background=None,
    )


# ---------------------------------------------------------------------------
# Run with: uvicorn main:app --reload
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=True)
