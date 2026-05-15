"""OpenAI 兼容的 LLM 客户端适配器。

支持 OpenAI / DeepSeek / Qwen / GLM 等任何兼容 OpenAI Chat Completions
接口的服务商。通过环境变量配置：

    LLM_BASE_URL  — API 地址（默认 https://api.deepseek.com/v1）
    LLM_API_KEY   — API 密钥
    LLM_MODEL     — 模型名称（默认 gpt-5.2-chat-latest）
"""

from __future__ import annotations

import json
import logging

import httpx

from smart_badge_api.core.config import get_settings

logger = logging.getLogger(__name__)


def _get_config() -> tuple[str, str, str, float]:
    settings = get_settings()
    base_url = settings.llm_base_url.rstrip("/")
    api_key = settings.llm_api_key
    model = settings.llm_model
    if not api_key:
        raise RuntimeError(
            "请在 .env 或环境变量中设置 LLM_API_KEY。"
            "如使用 DeepSeek，在 https://platform.deepseek.com 获取。"
        )
    return base_url, api_key, model, settings.llm_timeout_seconds


def _candidate_chat_urls(base_url: str) -> list[str]:
    if base_url.endswith("/v1"):
        return [f"{base_url}/chat/completions"]
    # Most OpenAI-compatible gateways expose /v1/chat/completions. Try that
    # first to avoid paying a failed HTTP round trip for every LLM call.
    urls = [f"{base_url}/v1/chat/completions", f"{base_url}/chat/completions"]
    return urls


def chat_completion(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.3,
    max_tokens: int = 12000,
    model_override: str | None = None,
) -> str:
    """调用 LLM Chat Completions API，返回 assistant 消息文本。"""
    base_url, api_key, model, timeout = _get_config()
    if model_override:
        model = model_override
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    if model.startswith("gpt-5"):
        # Some OpenAI-compatible gateways reject reasoning_effort for GPT-5
        # aliases. Keep the request gateway-compatible and avoid fallback
        # retries; prompt-specific max_tokens still bounds output size.
        payload["max_tokens"] = max(max_tokens, 12_000)
        timeout = max(timeout, 300.0)
    elif model == "deepseek-v4-pro":
        # The current OpenAI-compatible gateway accepts deepseek-v4-pro but is
        # not consistently compatible with reasoning_effort. Let it use the
        # provider default and rely on response_format/no-response_format
        # fallbacks below.
        payload["max_tokens"] = max(max_tokens, 12_000)
        timeout = max(timeout, 360.0)

    logger.info("Calling LLM: model=%s, prompt_len=%d", model, len(user_prompt))

    last_error: Exception | None = None
    with httpx.Client(timeout=timeout) as client:
        payload_variants = [payload]
        if "reasoning_effort" in payload:
            fallback_payload = dict(payload)
            fallback_payload.pop("reasoning_effort", None)
            payload_variants.append(fallback_payload)
        elif model_override and "response_format" in payload:
            fallback_payload = dict(payload)
            fallback_payload.pop("response_format", None)
            payload_variants.append(fallback_payload)

        for payload_variant in payload_variants:
            for url in _candidate_chat_urls(base_url):
                try:
                    resp = client.post(
                        url,
                        json=payload_variant,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    if not isinstance(content, str) or not content.strip():
                        finish_reason = data["choices"][0].get("finish_reason")
                        usage = data.get("usage")
                        raise RuntimeError(
                            f"LLM 返回的 content 为空或不是字符串: finish_reason={finish_reason}, usage={usage}"
                        )
                    logger.info("LLM response received from %s, length=%d", url, len(content))
                    return content
                except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError, RuntimeError) as exc:
                    last_error = exc
                    logger.warning("LLM call failed via %s: %s", url, exc)
                    continue

    raise RuntimeError(f"LLM 调用失败：{last_error}")


def parse_json_response(text: str) -> dict:
    """从 LLM 响应中提取 JSON。

    处理可能的 markdown 代码块包裹情况。
    """
    cleaned = text.strip()
    # 去掉 ```json ... ``` 包裹
    if cleaned.startswith("```"):
        first_newline = cleaned.index("\n")
        last_fence = cleaned.rfind("```")
        cleaned = cleaned[first_newline + 1 : last_fence].strip()
    return json.loads(cleaned)
