"""Local image store for DeepSeek vision input.

Image bytes live outside session JSON.  Sessions only keep metadata and
internal ``image_ref`` placeholders; the runtime hydrates those placeholders to
base64 data URLs immediately before an LLM request.
"""
from __future__ import annotations

import base64
import copy
import os
import re
import shutil
from pathlib import Path

from .. import config
from ..errors import AgentError, InvalidRequest
from ..util import is_relative_to, new_id, now_iso

IMAGE_ID_RE = re.compile(r"^img_[a-f0-9]{12}$")
IMAGE_TYPES = {
    "png": {"mime": "image/png", "ext": "png"},
    "jpeg": {"mime": "image/jpeg", "ext": "jpg"},
    "gif": {"mime": "image/gif", "ext": "gif"},
    "webp": {"mime": "image/webp", "ext": "webp"},
}


class ImageNotFound(AgentError):
    code = "image_not_found"
    http_status = 404


class ImageMissing(AgentError):
    code = "image_missing"
    http_status = 409


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[:6] not in {b"GIF87a", b"GIF89a"}:
        return None
    return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    index = 2
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while index + 1 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            break
        length = int.from_bytes(data[index:index + 2], "big")
        if length < 2 or index + length > len(data):
            break
        if marker in sof_markers:
            if index + 7 > len(data):
                break
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            return width, height
        index += length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    kind = data[12:16]
    if kind == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if kind == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if kind == b"VP8L":
        if data[20] != 0x2F or len(data) < 25:
            return None
        b0, b1, b2, b3 = data[21:25]
        width = 1 + ((b0 | (b1 << 8)) & 0x3FFF)
        height = 1 + (((b1 >> 6) | (b2 << 2) | (b3 << 10)) & 0x3FFF)
        return width, height
    return None


def sniff_image(data: bytes) -> dict | None:
    """Return type/size metadata from actual bytes, never from a filename."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    raw = bytes(data)
    for kind, size_fn in (
        ("png", _png_size),
        ("jpeg", _jpeg_size),
        ("gif", _gif_size),
        ("webp", _webp_size),
    ):
        dimensions = size_fn(raw)
        if dimensions:
            width, height = dimensions
            if width <= 0 or height <= 0:
                continue
            info = IMAGE_TYPES[kind]
            return {
                "kind": kind,
                "mime": info["mime"],
                "ext": info["ext"],
                "width": int(width),
                "height": int(height),
            }
    return None


def sanitize_name(name: str, ext: str) -> str:
    value = Path(str(name or "")).name.strip()
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    suffix = Path(value).suffix[:12]
    base = value[: -len(suffix)] if suffix else value
    limit = max(1, config.MAX_IMAGE_NAME_CHARS - len(suffix))
    base = base[:limit].strip()
    if not base:
        base = "image"
    return f"{base}{suffix or '.' + ext}"


class ImageStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "uploads"
        self.root.mkdir(parents=True, exist_ok=True)

    # -- validation / paths -------------------------------------------------

    @staticmethod
    def _session_id(session_id: str) -> str:
        if not re.match(config.SESSION_ID_RE, session_id or ""):
            raise InvalidRequest("会话 ID 不合法")
        return session_id

    @staticmethod
    def _image_id(image_id: str) -> str:
        if not IMAGE_ID_RE.match(image_id or ""):
            raise ImageNotFound("图片不存在")
        return image_id

    def _session_dir(self, session_id: str) -> Path:
        session_id = self._session_id(session_id)
        folder = self.root / session_id
        if not is_relative_to(folder, self.root):
            raise InvalidRequest("图片路径不合法")
        return folder

    def _find_path(self, session_id: str, image_id: str) -> Path:
        image_id = self._image_id(image_id)
        folder = self._session_dir(session_id)
        try:
            matches = sorted(folder.glob(f"{image_id}.*"))
        except OSError:
            matches = []
        for path in matches:
            if path.is_file() and is_relative_to(path, folder):
                return path
        raise ImageNotFound("图片不存在")

    # -- create / read ------------------------------------------------------

    def create(self, session_id: str, data: bytes, name: str = "") -> dict:
        session_id = self._session_id(session_id)
        raw = bytes(data or b"")
        if not raw:
            raise InvalidRequest("图片内容为空", code="invalid_image")
        if len(raw) > config.MAX_IMAGE_BYTES:
            raise InvalidRequest(
                f"单张图片不能超过 {config.MAX_IMAGE_BYTES // (1024 * 1024)} MiB",
                code="image_too_large",
                http_status=413,
            )
        info = sniff_image(raw)
        if not info:
            raise InvalidRequest(
                "仅支持 JPEG、PNG、GIF、WebP 图片",
                code="unsupported_image_type",
                http_status=415,
            )
        if info["width"] > config.MAX_IMAGE_SIDE or info["height"] > config.MAX_IMAGE_SIDE:
            raise InvalidRequest(
                f"图片单边不能超过 {config.MAX_IMAGE_SIDE} 像素",
                code="image_too_large",
                http_status=413,
            )

        folder = self._session_dir(session_id)
        folder.mkdir(parents=True, exist_ok=True)
        image_id = new_id("img")
        path = folder / f"{image_id}.{info['ext']}"
        tmp = folder / f".{image_id}.part"
        try:
            with open(tmp, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return {
            "id": image_id,
            "mime": info["mime"],
            "bytes": len(raw),
            "width": info["width"],
            "height": info["height"],
            "name": sanitize_name(name, info["ext"]),
            "created_at": now_iso(),
        }

    def read(self, session_id: str, image_id: str) -> tuple[bytes, dict]:
        self._image_id(image_id)
        try:
            path = self._find_path(session_id, image_id)
        except ImageNotFound:
            raise ImageMissing("图片文件不存在，请重新上传") from None
        try:
            raw = path.read_bytes()
        except OSError:
            raise ImageMissing("图片文件不可读，请重新上传") from None
        info = sniff_image(raw)
        if not info:
            raise ImageMissing("图片文件已损坏，请重新上传")
        return raw, info

    # -- cleanup ------------------------------------------------------------

    def delete_session(self, session_id: str) -> None:
        try:
            folder = self._session_dir(session_id)
        except AgentError:
            return
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)

    def purge(self) -> int:
        removed = 0
        try:
            children = list(self.root.iterdir())
        except OSError:
            return 0
        for child in children:
            if not child.is_dir():
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        return removed

    # -- request hydration -------------------------------------------------

    def hydrate_messages(
        self,
        messages: list[dict],
        session_id: str,
        max_bytes: int | None = None,
    ) -> tuple[list[dict], dict]:
        """Replace internal image_ref blocks with base64 image_url blocks.

        The newest image refs are selected first under a raw-byte budget.  Older
        refs remain as a short text placeholder so the request stays below the
        API body limit.
        """
        budget = int(max_bytes or config.MAX_REQUEST_IMAGE_BYTES)
        hydrated = copy.deepcopy(messages or [])
        refs: list[tuple[int, int, str]] = []
        for message_index, message in enumerate(hydrated):
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block_index, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "image_ref":
                    if role != "user":
                        raise InvalidRequest(
                            "图片只能出现在用户消息中",
                            code="image_role_invalid",
                        )
                    image_id = str(block.get("image_id") or "")
                    self._image_id(image_id)
                    refs.append((message_index, block_index, image_id))

        selected: set[tuple[int, int]] = set()
        data_urls: dict[str, str] = {}
        used = 0
        for message_index, block_index, image_id in reversed(refs):
            if image_id in data_urls:
                selected.add((message_index, block_index))
                continue
            raw, info = self.read(session_id, image_id)
            if used + len(raw) > budget:
                continue
            encoded = base64.b64encode(raw).decode("ascii")
            data_urls[image_id] = f"data:{info['mime']};base64,{encoded}"
            used += len(raw)
            selected.add((message_index, block_index))

        for message_index, message in enumerate(hydrated):
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block_index, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "image_ref":
                    continue
                image_id = str(block.get("image_id") or "")
                if (message_index, block_index) in selected:
                    content[block_index] = {
                        "type": "image_url",
                        "image_url": {
                            "url": data_urls[image_id],
                            "detail": "auto",
                        },
                    }
                else:
                    content[block_index] = {
                        "type": "text",
                        "text": "[更早的图片已省略]",
                    }

        sent = len(selected)
        return hydrated, {
            "images_sent": sent,
            "images_omitted": max(0, len(refs) - sent),
            "image_bytes": used,
        }
