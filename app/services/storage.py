"""Photo storage service.

Validates, compresses, and saves uploaded images.

If Cloudinary credentials are present in settings the photo is uploaded there
and a CDN URL is returned.  Otherwise the existing local-filesystem path is
used unchanged (Pillow compression → JPEG @ 800px max).

Callers get back a URL string suitable for the photo_url DB column, or None.
"""
import asyncio
import io
import uuid
from pathlib import Path

import cloudinary
import cloudinary.exceptions
import cloudinary.uploader
from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

from app.config import get_settings

settings = get_settings()

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

# Cloudinary is configured once on first use; this flag guards that call.
_cloudinary_configured = False


# ---------------------------------------------------------------------------
# Cloudinary helpers
# ---------------------------------------------------------------------------

def _cloudinary_enabled() -> bool:
    """Return True only when all three required credentials are non-empty."""
    s = settings
    return bool(s.cloudinary_cloud_name and s.cloudinary_api_key and s.cloudinary_api_secret)


def _ensure_cloudinary_configured() -> None:
    global _cloudinary_configured
    if _cloudinary_configured:
        return
    cloudinary.config(
        cloud_name=settings.cloudinary_cloud_name,
        api_key=settings.cloudinary_api_key,
        api_secret=settings.cloudinary_api_secret,
        secure=True,
    )
    _cloudinary_configured = True


def _upload_to_cloudinary(content: bytes, subfolder: str) -> str:
    """Blocking Cloudinary upload — call inside asyncio.to_thread."""
    _ensure_cloudinary_configured()
    folder = f"{settings.cloudinary_upload_folder}/{subfolder}"
    try:
        result = cloudinary.uploader.upload(
            content,
            folder=folder,
            quality="auto:good",
            width=800,
            crop="limit",
            fetch_format="auto",
            resource_type="image",
        )
    except cloudinary.exceptions.Error as exc:
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid image") from exc
    return result["secure_url"]


# ---------------------------------------------------------------------------
# Local filesystem helpers (existing behavior, kept intact)
# ---------------------------------------------------------------------------

def _ensure_dir(raw_path: str) -> Path:
    directory = Path(raw_path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _compress(content: bytes) -> Image.Image:
    """Open, validate, and convert to RGB.

    convert("RGB") does three things at once:
      1. Forces a full pixel-level decode — corrupt data raises here.
      2. Drops the alpha channel (PNG transparency).
      3. Discards all metadata including EXIF (pixel array only is kept).
    Raises HTTPException 400 for anything Pillow can't decode as an image.
    """
    try:
        img = Image.open(io.BytesIO(content))
        img = img.convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid image")
    return img


def _resize_if_needed(img: Image.Image) -> Image.Image:
    if img.width <= settings.max_photo_width:
        return img
    ratio = settings.max_photo_width / img.width
    new_size = (settings.max_photo_width, int(img.height * ratio))
    return img.resize(new_size, Image.LANCZOS)


def _process_and_save_local(content: bytes, directory: Path) -> str:
    img = _compress(content)
    img = _resize_if_needed(img)
    filename = f"{uuid.uuid4().hex}.jpg"
    dest = directory / filename
    img.save(dest, format="JPEG", quality=settings.photo_quality, optimize=True)
    # as_posix() normalises Windows backslashes and strips the leading "./"
    # that Path("./uploads/...") would otherwise keep on some platforms.
    return dest.as_posix()


def _save_local(content: bytes, subfolder: str) -> str:
    storage_path = (
        settings.receipt_storage_path if subfolder == "receipts"
        else settings.asset_storage_path
    )
    return _process_and_save_local(content, _ensure_dir(storage_path))


# ---------------------------------------------------------------------------
# Public API — signatures unchanged
# ---------------------------------------------------------------------------

async def _save_photo(file: UploadFile, subfolder: str) -> str:
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Photo too large (max 10 MB)")
    if _cloudinary_enabled():
        return await asyncio.to_thread(_upload_to_cloudinary, content, subfolder)
    return await asyncio.to_thread(_save_local, content, subfolder)


async def save_receipt_photo(file: UploadFile | None) -> str | None:
    """Save a utility-bill or shopping receipt photo. Returns DB-ready URL/path or None."""
    if file is None or not file.filename:
        return None
    return await _save_photo(file, "receipts")


async def save_asset_photo(file: UploadFile | None) -> str | None:
    """Save a shared-asset photo. Returns DB-ready URL/path or None."""
    if file is None or not file.filename:
        return None
    return await _save_photo(file, "assets")
