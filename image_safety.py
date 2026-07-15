"""Validated ChatGPT file-reference downloads and metadata-free cover images."""

from __future__ import annotations

import io
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
import warnings
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError


ALLOWED_MIME_TYPES = {"image/jpeg", "image/png"}
MIME_BY_FORMAT = {"JPEG": "image/jpeg", "PNG": "image/png"}
EXTENSIONS_BY_FORMAT = {"JPEG": {".jpg", ".jpeg"}, "PNG": {".png"}}


def _validate_public_https_url(url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or 443
    except ValueError:
        raise ValueError("The file download URL is invalid.") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port != 443
    ):
        raise ValueError("File downloads require a public HTTPS URL on port 443.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError("The file download host could not be resolved.") from None
    if not addresses:
        raise ValueError("The file download host did not resolve to an address.")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("The file download URL resolves to a non-public address.")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        _validate_public_https_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def validate_file_reference(value: Any) -> dict[str, str | None]:
    if not isinstance(value, dict):
        raise ValueError("image must be a ChatGPT file-reference object.")
    allowed = {"download_url", "file_id", "mime_type", "file_name"}
    if set(value) - allowed:
        raise ValueError("image contains unsupported file-reference fields.")
    download_url = value.get("download_url")
    file_id = value.get("file_id")
    mime_type = value.get("mime_type")
    file_name = value.get("file_name")
    if not isinstance(download_url, str) or not download_url:
        raise ValueError("image.download_url is required.")
    if not isinstance(file_id, str) or not 1 <= len(file_id) <= 200:
        raise ValueError("image.file_id is required and must be at most 200 characters.")
    if mime_type is not None and mime_type not in ALLOWED_MIME_TYPES:
        raise ValueError("Only image/png and image/jpeg file references are accepted.")
    if file_name is not None and (
        not isinstance(file_name, str) or not 1 <= len(file_name) <= 255
    ):
        raise ValueError("image.file_name must be between 1 and 255 characters.")
    if file_name:
        suffix = ("." + file_name.rsplit(".", 1)[-1]).lower() if "." in file_name else ""
        if suffix not in {".png", ".jpg", ".jpeg"}:
            raise ValueError("The image filename must end in .png, .jpg, or .jpeg.")
    return {
        "download_url": download_url,
        "file_id": file_id,
        "mime_type": mime_type,
        "file_name": file_name,
    }


def download_file_reference(file_reference: dict[str, str | None], max_bytes: int) -> bytes:
    url = str(file_reference["download_url"])
    _validate_public_https_url(url)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "netease-music-mcp-safe/1.0", "Accept": "image/png,image/jpeg"},
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(request, timeout=10) as response:
            raw_length = response.headers.get("Content-Length")
            if raw_length:
                try:
                    if int(raw_length) > max_bytes:
                        raise ValueError("The image exceeds the configured file-size limit.")
                except ValueError as exc:
                    if "exceeds" in str(exc):
                        raise
            response_type = response.headers.get_content_type()
            if response_type not in ALLOWED_MIME_TYPES | {"application/octet-stream"}:
                raise ValueError("The download did not return a PNG or JPEG content type.")
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"The ChatGPT file download failed with HTTP {exc.code}.") from None
    except urllib.error.URLError:
        raise ValueError("The ChatGPT file download could not be reached.") from None
    if len(data) > max_bytes:
        raise ValueError("The image exceeds the configured file-size limit.")
    if not data:
        raise ValueError("The uploaded image is empty.")
    return data


def normalize_cover_image(
    file_reference: Any,
    *,
    max_bytes: int,
    max_pixels: int,
    output_size: int = 300,
) -> tuple[bytes, dict[str, Any]]:
    reference = validate_file_reference(file_reference)
    source = download_file_reference(reference, max_bytes)
    if len(source) > max_bytes:
        raise ValueError("The image exceeds the configured file-size limit.")
    if not source:
        raise ValueError("The uploaded image is empty.")
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(source)) as probe:
                source_format = probe.format
                probe.verify()
            if source_format not in MIME_BY_FORMAT:
                raise ValueError("The file contents are not a supported PNG or JPEG image.")
            actual_mime = MIME_BY_FORMAT[source_format]
            if reference["mime_type"] and reference["mime_type"] != actual_mime:
                raise ValueError("The declared MIME type does not match the image contents.")
            file_name = reference["file_name"]
            if file_name:
                suffix = "." + file_name.rsplit(".", 1)[-1].lower()
                if suffix not in EXTENSIONS_BY_FORMAT[source_format]:
                    raise ValueError("The filename extension does not match the image contents.")
            with Image.open(io.BytesIO(source)) as opened:
                opened.load()
                width, height = opened.size
                if width < 1 or height < 1 or width * height > max_pixels:
                    raise ValueError("The image dimensions exceed the configured pixel limit.")
                image = ImageOps.exif_transpose(opened).convert("RGB")
                side = min(image.size)
                left = (image.width - side) // 2
                top = (image.height - side) // 2
                image = image.crop((left, top, left + side, top + side))
                if image.size != (output_size, output_size):
                    image = image.resize((output_size, output_size), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=90, optimize=True)
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise ValueError("The file contents are not a safe PNG or JPEG image.") from None
    return output.getvalue(), {
        "file_id": reference["file_id"],
        "source_format": source_format,
        "source_mime_type": actual_mime,
        "source_width": width,
        "source_height": height,
        "final_format": "JPEG",
        "final_mime_type": "image/jpeg",
        "final_width": output_size,
        "final_height": output_size,
        "metadata_removed": True,
        "center_cropped": width != height,
    }
