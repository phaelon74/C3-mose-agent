"""Tests for provider-aware vision message preparation."""

from __future__ import annotations

import base64

import pytest

from mose.config import LLMConfig
from mose.incoming_content import ImagePart, IncomingContent
from mose.llm_vision import (
    build_user_message_content,
    message_has_vision,
    prepare_messages_for_provider,
    provider_supports_vision,
    vision_error_hint,
)


def test_build_user_message_text_only():
    inc = IncomingContent(text="hello")
    assert build_user_message_content(inc) == "hello"


def test_build_user_message_multimodal():
    inc = IncomingContent(
        text="what is this",
        images=[ImagePart(mime_type="image/png", data_base64="abc123")],
    )
    parts = build_user_message_content(inc)
    assert isinstance(parts, list)
    assert parts[0]["type"] == "text"
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,abc123")


def test_vllm_strips_detail():
    cfg = LLMConfig(provider="vllm")
    messages = [{
        "role": "user",
        "content": [{
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,AA==", "detail": "high"},
        }],
    }]
    out = prepare_messages_for_provider(messages, "vllm", cfg)
    iu = out[0]["content"][0]["image_url"]
    assert "detail" not in iu


def test_bedrock_converts_to_image_bytes():
    cfg = LLMConfig(provider="bedrock")
    raw = base64.b64encode(b"fakepng").decode()
    messages = [{
        "role": "user",
        "content": [{
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{raw}"},
        }],
    }]
    out = prepare_messages_for_provider(messages, "bedrock", cfg)
    block = out[0]["content"][0]
    assert "image" in block
    assert block["image"]["format"] == "png"


def test_provider_supports_vision_disabled():
    cfg = LLMConfig(provider="vllm", vision_enabled=False)
    assert not provider_supports_vision("vllm", cfg)


def test_message_has_vision():
    assert message_has_vision([{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}])


def test_vllm_zero_image_limit_hint():
    hint = vision_error_hint(
        "vllm",
        RuntimeError("At most 0 image(s) may be provided in one prompt. (parameter=image)"),
    )
    assert hint is not None
    assert "--language-model-only" in hint
    assert "limit-mm-per-prompt" in hint
