"""Unit tests for ouroboros.llm — Cloud.ru payload sanitizer & reasoning_content fallback."""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestNormalizeContentForCloud(unittest.TestCase):
    """Test _normalize_content_for_cloud: list content becomes string."""

    def test_list_with_text_blocks_concatenated(self):
        """[{\"text\":\"a\"}, {\"text\":\"b\"}] -> \"a\\n\\nb\"."""
        from ouroboros.llm import LLMClient

        content = [{"text": "hello"}, {"text": "world"}]
        result = LLMClient._normalize_content_for_cloud(content)
        self.assertEqual(result, "hello\n\nworld")

    def test_list_with_single_text_block(self):
        """[{\"text\":\"only\"}] -> \"only\"."""
        from ouroboros.llm import LLMClient

        content = [{"text": "only"}]
        result = LLMClient._normalize_content_for_cloud(content)
        self.assertEqual(result, "only")

    def test_list_without_type_field(self):
        """Blocks without type field: text key is used."""
        from ouroboros.llm import LLMClient

        content = [{"text": "part1"}, {"other": "ignored", "text": "part2"}]
        result = LLMClient._normalize_content_for_cloud(content)
        self.assertEqual(result, "part1\n\npart2")

    def test_list_with_output_text(self):
        """output_text key is used when text is absent."""
        from ouroboros.llm import LLMClient

        content = [{"output_text": "from api"}]
        result = LLMClient._normalize_content_for_cloud(content)
        self.assertEqual(result, "from api")

    def test_string_passthrough(self):
        """String content is returned as-is."""
        from ouroboros.llm import LLMClient

        content = "plain string"
        result = LLMClient._normalize_content_for_cloud(content)
        self.assertEqual(result, "plain string")

    def test_none_returns_empty(self):
        """None -> \"\"."""
        from ouroboros.llm import LLMClient

        result = LLMClient._normalize_content_for_cloud(None)
        self.assertEqual(result, "")

    def test_empty_list_returns_empty(self):
        """[] -> \"\"."""
        from ouroboros.llm import LLMClient

        result = LLMClient._normalize_content_for_cloud([])
        self.assertEqual(result, "")


def _make_mock_response(content, reasoning_content=None, tool_calls=None):
    """Build a mock OpenAI ChatCompletion response object."""
    message = {"role": "assistant", "content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return MagicMock(
        model_dump=MagicMock(return_value={
            "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        })
    )


class TestReasoningContentFallback(unittest.TestCase):
    """_chat_cloud promotes reasoning_content → content when content is empty."""

    def _call(self, content, reasoning_content=None):
        from ouroboros.llm import LLMClient
        client = LLMClient(api_key="test-key")
        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create.return_value = _make_mock_response(
            content=content, reasoning_content=reasoning_content,
        )
        client._client = mock_openai_client
        return client._chat_cloud(
            messages=[{"role": "user", "content": "test"}],
            model="zai-org/GLM-4.7",
            tools=None, max_tokens=4096, tool_choice="auto",
        )

    def test_reasoning_content_used_when_content_empty(self):
        """Empty content + reasoning_content present → content = reasoning_content."""
        msg, _ = self._call(content=None, reasoning_content="Это ответ из reasoning_content")
        self.assertEqual(msg["content"], "Это ответ из reasoning_content")

    def test_reasoning_content_used_when_content_whitespace(self):
        """Whitespace-only content + reasoning_content → content = reasoning_content."""
        msg, _ = self._call(content="   ", reasoning_content="real answer")
        self.assertEqual(msg["content"], "real answer")

    def test_normal_content_not_overridden(self):
        """When content is present, reasoning_content is ignored."""
        msg, _ = self._call(content="normal reply", reasoning_content="should be ignored")
        self.assertEqual(msg["content"], "normal reply")

    def test_both_empty_stays_empty(self):
        """Both content and reasoning_content empty → content stays empty."""
        msg, _ = self._call(content=None, reasoning_content=None)
        self.assertIn(msg.get("content"), (None, "", "   "))


class TestSanitizeToolsForCloud(unittest.TestCase):
    """_sanitize_tools_for_cloud fixes tool schemas for Cloud.ru."""

    def _sanitize(self, tools):
        from ouroboros.llm import LLMClient
        return LLMClient._sanitize_tools_for_cloud(tools)

    def test_required_dict_becomes_empty_list(self):
        """required={} (invalid) → required=[]."""
        tools = [{
            "type": "function",
            "function": {
                "name": "my_tool",
                "parameters": {
                    "type": "object",
                    "properties": {"arg": {"type": "string"}},
                    "required": {},
                },
            },
        }]
        result = self._sanitize(tools)
        self.assertEqual(result[0]["function"]["parameters"]["required"], [])

    def test_required_int_becomes_empty_list(self):
        """required=42 (invalid) → required=[]."""
        tools = [{
            "type": "function",
            "function": {
                "name": "my_tool",
                "parameters": {"type": "object", "properties": {}, "required": 42},
            },
        }]
        result = self._sanitize(tools)
        self.assertEqual(result[0]["function"]["parameters"]["required"], [])

    def test_valid_required_list_preserved(self):
        """required=["a","b"] stays intact."""
        tools = [{
            "type": "function",
            "function": {
                "name": "my_tool",
                "parameters": {"type": "object", "properties": {"a": {}, "b": {}}, "required": ["a", "b"]},
            },
        }]
        result = self._sanitize(tools)
        self.assertEqual(result[0]["function"]["parameters"]["required"], ["a", "b"])

    def test_missing_parameters_gets_defaults(self):
        """Tool without parameters gets type=object, properties={}, required=[]."""
        tools = [{"type": "function", "function": {"name": "no_params"}}]
        result = self._sanitize(tools)
        params = result[0]["function"]["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertEqual(params["properties"], {})
        self.assertEqual(params["required"], [])

    def test_cache_control_stripped(self):
        """cache_control is removed from top-level tool dict."""
        tools = [{
            "type": "function",
            "cache_control": {"type": "ephemeral"},
            "function": {"name": "t", "parameters": {"type": "object", "properties": {}}},
        }]
        result = self._sanitize(tools)
        self.assertNotIn("cache_control", result[0])

    def test_pydantic_like_object_converted(self):
        """Object with .model_dump() is converted to dict."""
        class FakePydantic:
            def model_dump(self):
                return {
                    "type": "function",
                    "function": {
                        "name": "pydantic_tool",
                        "parameters": {"type": "object", "properties": {"x": {"type": "int"}}},
                    },
                }
        result = self._sanitize([FakePydantic()])
        self.assertEqual(result[0]["function"]["name"], "pydantic_tool")
        self.assertEqual(result[0]["function"]["parameters"]["type"], "object")

    def test_json_roundtrip_produces_clean_output(self):
        """Output is pure JSON-serializable (no custom objects)."""
        import json
        tools = [{
            "type": "function",
            "function": {
                "name": "t",
                "parameters": {"type": "object", "properties": {"a": {"type": "string"}}},
            },
        }]
        result = self._sanitize(tools)
        roundtripped = json.loads(json.dumps(result))
        self.assertEqual(result, roundtripped)


class TestCloudruEnableToolsFlag(unittest.TestCase):
    """CLOUDRU_ENABLE_TOOLS flag gates whether tools are sent."""

    def _run_sanitize(self, tools_in_kwargs, enable_flag):
        from ouroboros.llm import LLMClient
        client = LLMClient(api_key="test-key")
        kwargs = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1024}
        if tools_in_kwargs:
            kwargs["tools"] = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object", "properties": {}}}}]
            kwargs["tool_choice"] = "auto"
        with patch.dict(os.environ, {"CLOUDRU_ENABLE_TOOLS": enable_flag}):
            result = client._sanitize_cloud_payload(
                kwargs["messages"], kwargs,
            )
        return result

    def test_tools_stripped_when_flag_off(self):
        """Without CLOUDRU_ENABLE_TOOLS=1, tools are removed."""
        result = self._run_sanitize(tools_in_kwargs=True, enable_flag="0")
        self.assertNotIn("tools", result)
        self.assertNotIn("tool_choice", result)

    def test_tools_present_when_flag_on(self):
        """With CLOUDRU_ENABLE_TOOLS=1, sanitized tools are sent."""
        result = self._run_sanitize(tools_in_kwargs=True, enable_flag="1")
        self.assertIn("tools", result)
        self.assertEqual(len(result["tools"]), 1)

    def test_no_tools_no_crash(self):
        """No tools in kwargs + flag on → no crash, no tools key."""
        result = self._run_sanitize(tools_in_kwargs=False, enable_flag="1")
        self.assertNotIn("tools", result)
