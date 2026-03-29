"""
Ouroboros — LLM client.

The only module that communicates with LLM APIs (Cloud.ru Foundation Models + optional local).
Contract: chat(), default_model(), available_models(), add_usage().
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_LIGHT_MODEL = "zai-org/GLM-4.7"


def normalize_reasoning_effort(value: str, default: str = "medium") -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def reasoning_rank(value: str) -> int:
    order = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
    return int(order.get(str(value or "").strip().lower(), 3))


def add_usage(total: Dict[str, Any], usage: Dict[str, Any]) -> None:
    """Accumulate usage from one LLM call into a running total."""
    for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_write_tokens"):
        total[k] = int(total.get(k) or 0) + int(usage.get(k) or 0)
    if usage.get("cost"):
        total["cost"] = float(total.get("cost") or 0) + float(usage["cost"])


def fetch_remote_pricing() -> Dict[str, Tuple[float, float, float]]:
    """Stub: Cloud.ru does not expose a public pricing API. Returns empty dict."""
    return {}


# Backward-compatible alias (legacy callers)
fetch_openrouter_pricing = fetch_remote_pricing
fetch_cloudru_pricing = fetch_remote_pricing


class LLMClient:
    """LLM API wrapper. Routes calls to Cloud.ru Foundation Models or a local llama-cpp-python server."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://foundation-models.api.cloud.ru/v1",
    ):
        # Cloud.ru Foundation Models — OpenAI-compatible API.
        # API key is a "Key Secret" string stored in API_KEY.
        self._api_key = api_key or os.environ.get("API_KEY", "")
        self._base_url = base_url
        self._client = None
        self._local_client = None
        self._local_port: Optional[int] = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            key_len = len(self._api_key or "")
            log.info(
                "Initializing Cloud.ru LLM client (base_url=%s, api_key_len=%d, has_key=%s)",
                self._base_url,
                key_len,
                bool(key_len),
            )

            self._client = OpenAI(
                base_url=self._base_url,
                api_key=self._api_key,
            )
        return self._client

    def _get_local_client(self):
        port = int(os.environ.get("LOCAL_MODEL_PORT", "8766"))
        if self._local_client is None or self._local_port != port:
            from openai import OpenAI
            self._local_client = OpenAI(
                base_url=f"http://127.0.0.1:{port}/v1",
                api_key="local",
            )
            self._local_port = port
        return self._local_client

    @staticmethod
    def _strip_cache_control(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Strip cache_control from message content blocks (not supported by Cloud.ru)."""
        import copy
        cleaned = copy.deepcopy(messages)
        for msg in cleaned:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)
        return cleaned

    @staticmethod
    def _normalize_content_for_cloud(content: Any) -> str:
        """Convert list content to plain string for Cloud.ru compatibility.

        If content is a list of parts (e.g. [{"text":"..."}] or blocks without type),
        concatenate all text into one string. Otherwise return content as string.
        """
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    text = block.get("text") or block.get("output_text") or ""
                    if text:
                        parts.append(str(text))
            return "\n\n".join(parts)
        return str(content)

    @staticmethod
    def _to_dict(obj: Any) -> Any:
        """Convert pydantic models / arbitrary objects to plain dicts."""
        if isinstance(obj, dict):
            return obj
        for method in ("model_dump", "dict"):
            fn = getattr(obj, method, None)
            if callable(fn):
                return fn()
        return obj

    @staticmethod
    def _fix_schema_required(schema: Dict[str, Any]) -> None:
        """Recursively fix `required` fields in a JSON Schema for Cloud.ru.

        Valid list[str] is kept; empty list is removed entirely (Cloud.ru
        misinterprets ``[]`` as ``{}``); any other type is removed.
        """
        if not isinstance(schema, dict):
            return
        req = schema.get("required")
        if req is not None:
            if isinstance(req, list) and all(isinstance(r, str) for r in req):
                if not req:
                    del schema["required"]
            else:
                del schema["required"]
        for prop in (schema.get("properties") or {}).values():
            if isinstance(prop, dict):
                LLMClient._fix_schema_required(prop)
        for kw in ("items", "additionalProperties"):
            sub = schema.get(kw)
            if isinstance(sub, dict):
                LLMClient._fix_schema_required(sub)

    @staticmethod
    def _sanitize_tools_for_cloud(tools: List[Any]) -> List[Dict[str, Any]]:
        """Sanitize tools schema for Cloud.ru compatibility.

        - Convert pydantic models to dicts
        - Ensure parameters.type="object" and parameters.properties is a dict
        - Recursively fix `required` to be list[str] at every nesting level
        - Strip cache_control
        - Return a clean copy via json roundtrip
        """
        fixed: List[Dict[str, Any]] = []
        for tool in tools:
            t = LLMClient._to_dict(tool)
            if not isinstance(t, dict):
                continue
            t.pop("cache_control", None)

            func = t.get("function")
            if isinstance(func, dict):
                params = LLMClient._to_dict(func.get("parameters") or {})
                if not isinstance(params, dict):
                    params = {}
                params.setdefault("type", "object")
                params.setdefault("properties", {})
                if not isinstance(params.get("properties"), dict):
                    params["properties"] = {}

                LLMClient._fix_schema_required(params)
                func["parameters"] = params
            fixed.append(t)

        return json.loads(json.dumps(fixed))

    def _sanitize_cloud_payload(
        self,
        messages: List[Dict[str, Any]],
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Sanitize payload for Cloud.ru: normalize content, handle tools via flag, cap max_tokens."""
        import copy
        clean_messages = copy.deepcopy(messages)
        content_types: List[str] = []
        for msg in clean_messages:
            content = msg.get("content")
            orig_type = "str" if isinstance(content, str) else "list" if isinstance(content, list) else type(content).__name__
            content_types.append(orig_type)
            msg["content"] = self._normalize_content_for_cloud(content)

        tools_sent = "tools" in kwargs
        enable_tools = os.environ.get("CLOUDRU_ENABLE_TOOLS", "1").strip() not in ("0", "false", "no")

        if tools_sent and enable_tools:
            raw_tools = kwargs.pop("tools")
            sanitized_tools = self._sanitize_tools_for_cloud(raw_tools)
            kwargs["tools"] = sanitized_tools
            for idx, st in enumerate(sanitized_tools):
                fn = st.get("function", {})
                req = fn.get("parameters", {}).get("required")
                if req is not None and not isinstance(req, list):
                    log.error("BUG: tool %d (%s) still has non-list required: %r", idx, fn.get("name"), req)
            log.debug("Cloud.ru sanitized %d tools", len(sanitized_tools))
        else:
            for key in ("tools", "tool_choice"):
                kwargs.pop(key, None)

        for key in (
            "parallel_tool_calls",
            "response_format", "stream", "stream_options",
        ):
            kwargs.pop(key, None)

        # Cap max_tokens
        mt = kwargs.get("max_tokens")
        if mt is None or (isinstance(mt, (int, float)) and int(mt) > 4096):
            kwargs["max_tokens"] = 4096

        kwargs["messages"] = clean_messages

        log.debug(
            "Cloud.ru payload: messages=%d, content_types=%s, tools_sent=%s, tools_enabled=%s, max_tokens=%s",
            len(clean_messages),
            content_types,
            tools_sent,
            enable_tools,
            kwargs.get("max_tokens"),
        )
        return kwargs

    def _fetch_generation_cost(self, generation_id: str) -> Optional[float]:
        """Stub: Cloud.ru does not expose a generation cost API."""
        return None

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
        use_local: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Single LLM call. Returns: (response_message_dict, usage_dict with cost).

        When use_local=True, routes to the local llama-cpp-python server
        and strips parameters unsupported by the local server.
        """
        if use_local:
            return self._chat_local(messages, tools, max_tokens, tool_choice)

        return self._chat_cloud(messages, model, tools, max_tokens, tool_choice)

    def _chat_local(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        max_tokens: int,
        tool_choice: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Send a chat request to the local llama-cpp-python server."""
        client = self._get_local_client()

        clean_messages = self._strip_cache_control(messages)
        # Flatten multipart content blocks to plain strings (local server doesn't support arrays)
        for msg in clean_messages:
            content = msg.get("content")
            if isinstance(content, list):
                msg["content"] = "\n\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )

        clean_tools = None
        if tools:
            clean_tools = [
                {k: v for k, v in t.items() if k != "cache_control"}
                for t in tools
            ]

        # Cap max_tokens to fit within the model's context window
        local_max = min(max_tokens, 2048)
        try:
            from ouroboros.local_model import get_manager
            ctx_len = get_manager().get_context_length()
            if ctx_len > 0:
                local_max = min(max_tokens, max(256, ctx_len // 4))
        except Exception:
            pass

        kwargs: Dict[str, Any] = {
            "model": "local-model",
            "messages": clean_messages,
            "max_tokens": local_max,
        }
        if clean_tools:
            kwargs["tools"] = clean_tools
            kwargs["tool_choice"] = tool_choice

        resp = client.chat.completions.create(**kwargs)
        resp_dict = resp.model_dump()
        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        usage["cost"] = 0.0
        return msg, usage

    def _chat_cloud(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]],
        max_tokens: int,
        tool_choice: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Send a chat request to Cloud.ru Foundation Models."""
        client = self._get_client()

        clean_messages = self._strip_cache_control(messages)

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": clean_messages,
            "max_tokens": max_tokens if max_tokens is not None else 4096,
        }
        if tools:
            clean_tools = [
                {k: v for k, v in t.items() if k != "cache_control"}
                for t in tools
            ]
            kwargs["tools"] = clean_tools
            kwargs["tool_choice"] = tool_choice

        kwargs = self._sanitize_cloud_payload(clean_messages, kwargs)

        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:
            body = getattr(e, 'body', None) or getattr(e, 'response', None)
            log.error("Cloud.ru API error: %s | body=%s | model=%s", e, body, model)
            raise
        resp_dict = resp.model_dump()
        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        # GLM-4.7 puts its answer in reasoning_content instead of content.
        # Promote reasoning_content → content so downstream code works uniformly.
        content = msg.get("content")
        reasoning = msg.get("reasoning_content")
        if (not content or not str(content).strip()) and reasoning and str(reasoning).strip():
            log.info("Cloud.ru: content empty, using reasoning_content (%d chars)", len(str(reasoning)))
            msg["content"] = str(reasoning)

        return msg, usage

    def vision_query(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        model: str = "GigaChat/GigaChat-2-Max",
        max_tokens: int = 1024,
        reasoning_effort: str = "low",
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Send a vision query to an LLM. Lightweight — no tools, no loop.

        Args:
            prompt: Text instruction for the model
            images: List of image dicts. Each dict must have either:
                - {"url": "https://..."} — for URL images
                - {"base64": "<b64>", "mime": "image/png"} — for base64 images
            model: VLM-capable model ID
            max_tokens: Max response tokens
            reasoning_effort: Effort level

        Returns:
            (text_response, usage_dict)
        """
        # Build multipart content
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            if "url" in img:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": img["url"]},
                })
            elif "base64" in img:
                mime = img.get("mime", "image/png")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{img['base64']}"},
                })
            else:
                log.warning("vision_query: skipping image with unknown format: %s", list(img.keys()))

        messages = [{"role": "user", "content": content}]
        response_msg, usage = self.chat(
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
        )
        text = response_msg.get("content") or ""
        return text, usage

    def default_model(self) -> str:
        """Return the single default model from env. LLM switches via tool if needed."""
        return os.environ.get("OUROBOROS_MODEL", "zai-org/GLM-4.7")

    def available_models(self) -> List[str]:
        """Return list of available models from env (for switch_model tool schema)."""
        main = os.environ.get("OUROBOROS_MODEL", "zai-org/GLM-4.7")
        code = os.environ.get("OUROBOROS_MODEL_CODE", "")
        light = os.environ.get("OUROBOROS_MODEL_LIGHT", "")
        models = [main]
        if code and code != main:
            models.append(code)
        if light and light != main and light != code:
            models.append(light)
        return models
