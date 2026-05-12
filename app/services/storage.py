"""Photo storage service.

Validates, compresses, and saves uploaded images.
Callers get back a relative path string suitable for the photo_url DB column.
"""
import asyncio
import io
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

from app.config import get_settings

settings = get_settings()

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


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


def _process_and_save(content: bytes, directory: Path) -> str:
    img = _compress(content)
    img = _resize_if_needed(img)
    filename = f"{uuid.uuid4().hex}.jpg"
    dest = directory / filename
    img.save(dest, format="JPEG", quality=settings.photo_quality, optimize=True)
    # as_posix() normalises Windows backslashes and strips the leading "./"
    # that Path("./uploads/...") would otherwise keep on some platforms.
    return dest.as_posix()


async def _save_photo(file: UploadFile, directory: Path) -> str:
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Photo too large (max 10 MB)")
    return await asyncio.to_thread(_process_and_save, content, directory)


async def save_receipt_photo(file: UploadFile | None) -> str | None:
    """Save a utility-bill or shopping receipt photo. Returns DB-ready path or None."""
    if file is None:
        return None
    return await _save_photo(file, _ensure_dir(settings.receipt_storage_path))


async def save_asset_photo(file: UploadFile | None) -> str | None:
    """Save a shared-asset photo. Returns DB-ready path or None."""
    if file is None:
        return None
    return await _save_photo(file, _ensure_dir(settings.asset_storage_path))
