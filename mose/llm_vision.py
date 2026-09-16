"""Provider-aware multimodal message preparation for LLM backends."""

from __future__ import annotations

import base64
from typing import Any

from mose.config import LLMConfig
from mose.incoming_content import ImagePart, IncomingContent
from mose.observe import get_logger, log_event

logger = get_logger("llm_vision")


class VisionNotSupportedError(RuntimeError):
    """Raised when images are present but the configured provider cannot handle them."""


def build_user_message_content(incoming: IncomingContent) -> str | list[dict[str, Any]]:
    """Build OpenAI-style user content (string or multimodal parts list)."""
    text_parts: list[str] = []
    if incoming.text.strip():
        text_parts.append(incoming.text.strip())
    text_parts.extend(incoming.file_blocks)
    combined = "\n\n".join(text_parts) if text_parts else ""

    if not incoming.images:
        return combined or "(no text)"

    content: list[dict[str, Any]] = []
    if combined:
        content.append({"type": "text", "text": combined})
    for img in incoming.images:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{img.mime_type};base64,{img.data_base64}",
            },
        })
    return content


def message_has_vision(messages: list[dict[str, Any]]) -> bool:
    for msg in messages:
        raw = msg.get("content")
        if not isinstance(raw, list):
            continue
        for part in raw:
            if isinstance(part, dict) and part.get("type") == "image_url":
                return True
    return False


def count_vision_images(messages: list[dict[str, Any]]) -> int:
    n = 0
    for msg in messages:
        raw = msg.get("content")
        if not isinstance(raw, list):
            continue
        for part in raw:
            if isinstance(part, dict) and part.get("type") == "image_url":
                n += 1
    return n


def provider_supports_vision(provider: str, config: LLMConfig) -> bool:
    if not config.vision_enabled:
        return False
    p = (provider or "openai_compat").strip().lower()
    if p == "bedrock":
        return True
    return p in ("vllm", "tabby", "openai_compat")


def vision_not_supported_message(provider: str, config: LLMConfig) -> str:
    p = (provider or "openai_compat").strip().lower()
    if not config.vision_enabled:
        return (
            "Images are disabled (LLM_VISION_ENABLED=false). "
            "Set LLM_VISION_ENABLED=true and use a vision-capable model."
        )
    if p == "bedrock":
        return "Image support on Bedrock requires a vision-capable model in your configured region."
    if p == "tabby":
        return (
            "TabbyAPI: load the model with vision: true in Tabby config and use a vision-capable weights file."
        )
    if p == "vllm":
        return (
            "vLLM rejected the image(s). The running engine has a multimodal image "
            "limit of 0 — usually because the server was started with "
            "--language-model-only (language tower only, no vision encoder). "
            "Remove that flag, reload a vision-capable checkpoint, and set "
            "--limit-mm-per-prompt '{\"image\":4}' (or similar)."
        )
    return (
        "The configured LLM server must accept OpenAI-style multimodal chat "
        "(content parts with image_url data URLs)."
    )


def _strip_image_detail(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove unsupported image_url.detail (vLLM / Tabby)."""
    out: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            out.append(part)
            continue
        if part.get("type") != "image_url":
            out.append(part)
            continue
        iu = part.get("image_url")
        if isinstance(iu, dict) and "detail" in iu:
            iu = {k: v for k, v in iu.items() if k != "detail"}
            out.append({"type": "image_url", "image_url": iu})
        else:
            out.append(part)
    return out


def _mime_to_bedrock_format(mime: str) -> str:
    m = (mime or "").lower().split(";")[0].strip()
    if m in ("image/jpg", "image/jpeg"):
        return "jpeg"
    if m == "image/png":
        return "png"
    if m == "image/gif":
        return "gif"
    if m == "image/webp":
        return "webp"
    return "jpeg"


def _parse_data_url(url: str) -> tuple[str, bytes] | None:
    if not url.startswith("data:"):
        return None
    try:
        header, b64 = url.split(",", 1)
        mime = header[5:].split(";")[0] if ";" in header else header[5:]
        return mime, base64.b64decode(b64)
    except (ValueError, TypeError):
        return None


def _openai_parts_to_bedrock(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text") or ""
            if text:
                blocks.append({"text": text})
        elif ptype == "image_url":
            iu = part.get("image_url") or {}
            url = iu.get("url", "") if isinstance(iu, dict) else ""
            parsed = _parse_data_url(str(url))
            if not parsed:
                continue
            mime, raw = parsed
            blocks.append({
                "image": {
                    "format": _mime_to_bedrock_format(mime),
                    "source": {"bytes": raw},
                },
            })
    return blocks


def prepare_messages_for_provider(
    messages: list[dict[str, Any]],
    provider: str,
    config: LLMConfig,
) -> list[dict[str, Any]]:
    """Normalize multimodal messages for the target inference backend."""
    p = (provider or "openai_compat").strip().lower()
    if not message_has_vision(messages):
        return messages

    payload_mode = f"{p}_passthrough"
    out: list[dict[str, Any]] = []

    for msg in messages:
        m = dict(msg)
        raw = m.get("content")
        if isinstance(raw, list):
            if p == "bedrock":
                m["content"] = _openai_parts_to_bedrock(raw)
                payload_mode = "bedrock_image_bytes"
            else:
                m["content"] = _strip_image_detail(raw)
                payload_mode = f"{p}_data_url"
        out.append(m)

    log_event(
        logger,
        "vision_payload_prepared",
        provider=p,
        payload_mode=payload_mode,
        image_count=count_vision_images(messages),
    )
    return out


def vision_error_hint(provider: str, exc: BaseException) -> str | None:
    """Return operator hint if exc looks vision-related."""
    msg = str(exc).lower()
    patterns = (
        "image",
        "vision",
        "multimodal",
        "mm ",
        "limit-mm",
        "unsupported content",
        "invalid image",
    )
    if not any(p in msg for p in patterns):
        return None
    p = (provider or "").strip().lower()
    if p == "tabby":
        return vision_not_supported_message("tabby", LLMConfig())
    if p == "vllm":
        return vision_not_supported_message("vllm", LLMConfig())
    return (
        "The inference server rejected the image input. "
        "Confirm you are running a vision-capable model and the server supports "
        "OpenAI-style image_url parts with data: URLs."
    )


def check_vision_allowed(messages: list[dict[str, Any]], config: LLMConfig) -> None:
    """Raise VisionNotSupportedError if messages include images but provider cannot handle them."""
    if not message_has_vision(messages):
        return
    if not provider_supports_vision(config.provider, config):
        raise VisionNotSupportedError(vision_not_supported_message(config.provider, config))
