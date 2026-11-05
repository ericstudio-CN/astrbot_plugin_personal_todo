"""Read message image references with bounded memory and no plugin disk cache."""

from __future__ import annotations

import asyncio
import base64
import binascii
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx


class MediaError(ValueError):
    """An attachment could not be safely read as a supported image."""


def image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise MediaError("图片格式无法识别，请发送 JPG、PNG、GIF 或 WebP 原图。")


def _local_bytes(path: Path, limit: int) -> bytes:
    if not path.is_file():
        raise MediaError("图片引用已失效，请重新发送图片。")
    if path.stat().st_size > limit:
        raise MediaError("图片太大，请压缩或裁剪后重发。")
    with path.open("rb") as stream:
        return stream.read(limit + 1)


async def image_data_uri(reference: str, client: httpx.AsyncClient, limit: int) -> str:
    """Resolve a platform-provided image reference, validating actual bytes."""
    if not isinstance(reference, str) or not reference:
        raise MediaError("没有可读取的图片地址，请重新发送图片。")
    if reference.startswith(("https://", "http://")):
        data = bytearray()
        try:
            async with client.stream("GET", reference, follow_redirects=True) as response:
                response.raise_for_status()
                size = response.headers.get("content-length", "")
                if size.isdigit() and int(size) > limit:
                    raise MediaError("图片太大，请压缩或裁剪后重发。")
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise MediaError("图片太大，请压缩或裁剪后重发。")
        except httpx.HTTPError as exc:
            raise MediaError("图片下载失败或地址已过期，请重新发送图片。") from exc
        content = bytes(data)
    elif reference.startswith(("data:", "base64://")):
        encoded = reference.split(",", 1)[-1] if reference.startswith("data:") else reference[9:]
        if len(encoded) > ((limit + 2) // 3) * 4 + 4:
            raise MediaError("图片太大，请压缩或裁剪后重发。")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise MediaError("图片编码无效，请重新发送图片。") from exc
    else:
        if reference.startswith("file://"):
            parsed = urlparse(reference)
            path = Path(url2pathname(parsed.path))
            if parsed.netloc and parsed.netloc != "localhost":
                raise MediaError("不支持网络文件路径，请直接发送图片。")
        else:
            path = Path(reference)
        content = await asyncio.to_thread(_local_bytes, path, limit)
    if not content or len(content) > limit:
        raise MediaError("图片为空或超过大小限制，请重新发送。")
    mime = image_mime(content)
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
