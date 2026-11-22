"""Bilder für lokale Modelle vorbereiten.

Discord liefert Anhänge als PNG, JPEG, WebP, GIF oder noch exotischer.
Lokale Server (z. B. Ollama) können nicht alle Formate lesen und brechen dann
mit „Failed to load image“ ab. Deshalb wird jedes Bild einheitlich in ein
JPEG umgerechnet und auf eine vernünftige Größe verkleinert.
"""

from __future__ import annotations

import base64
import io

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_SIDE = 1568
JPEG_QUALITY = 88


class ImageError(ValueError):
    """Die Datei ist kein lesbares Bild."""


def to_jpeg(data: bytes, max_side: int = MAX_SIDE) -> bytes:
    try:
        image = Image.open(io.BytesIO(data))
        image.seek(0)                       # GIF/WebP-Animationen: erstes Bild
        image = ImageOps.exif_transpose(image)
    except (UnidentifiedImageError, OSError, EOFError, ValueError) as exc:
        raise ImageError(f"Bild nicht lesbar: {exc}") from exc

    if image.mode in ("RGBA", "LA", "P", "PA"):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")

    image.thumbnail((max_side, max_side))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def to_data_url(data: bytes, max_side: int = MAX_SIDE) -> str:
    encoded = base64.b64encode(to_jpeg(data, max_side)).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"
