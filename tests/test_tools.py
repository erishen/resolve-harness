"""Tests for the tool registry and built-in tools."""

from __future__ import annotations

import pytest

from agentpulse.memory import LongTermMemory
from agentpulse.tools import http as http_mod
from agentpulse.tools.builtin import register_builtins
from agentpulse.tools.registry import ToolError, ToolRegistry


class TestRegistry:
    def test_register_and_execute(self) -> None:
        reg = ToolRegistry()

        @reg.register
        def echo(text: str) -> str:
            """Echo back the text."""
            return text

        assert reg.get("echo") is not None
        assert reg.execute("echo", {"text": "hi"}) == "hi"
        assert reg.names() == ["echo"]

    def test_register_with_explicit_metadata(self) -> None:
        reg = ToolRegistry()

        @reg.register(name="multiply", description="Multiply two numbers")
        def mul(a: int, b: int) -> int:
            return a * b

        schema = reg.get("multiply").schema()
        assert schema["function"]["name"] == "multiply"
        assert schema["function"]["description"] == "Multiply two numbers"
        assert schema["function"]["parameters"]["required"] == ["a", "b"]
        assert reg.execute("multiply", {"a": 3, "b": 4}) == "12"

    def test_duplicate_name_raises(self) -> None:
        reg = ToolRegistry()
        reg.register(lambda: 1, name="dup")
        with pytest.raises(ValueError, match="already registered"):
            reg.register(lambda: 2, name="dup")

    def test_unknown_tool_raises(self) -> None:
        reg = ToolRegistry()
        with pytest.raises(ToolError, match="unknown tool"):
            reg.execute("missing", {})

    def test_result_serialization(self) -> None:
        reg = ToolRegistry()
        reg.register(lambda: {"a": 1}, name="obj")
        assert reg.execute("obj") == '{"a": 1}'

    def test_require_approval_flag(self) -> None:
        reg = ToolRegistry()

        @reg.register(require_approval=True)
        def risky(path: str) -> str:
            """Write something."""
            return path

        @reg.register
        def safe(x: int) -> int:
            """Harmless."""
            return x

        assert reg.needs_approval("risky") is True
        assert reg.needs_approval("safe") is False
        assert reg.needs_approval("missing") is False
        assert reg.approval_tools() == ["risky"]


class TestBuiltins:
    @pytest.fixture()
    def registry(self, tmp_path) -> ToolRegistry:
        memory = LongTermMemory(tmp_path / "m.db")
        reg = ToolRegistry()
        register_builtins(reg, memory)
        yield reg
        memory.close()

    def test_builtin_names(self, registry: ToolRegistry) -> None:
        assert set(registry.names()) == {
            "get_current_time",
            "remember",
            "recall",
            "list_memories",
            "fetch",
        }

    def test_write_file_and_fetch_require_approval(self, tmp_path) -> None:
        """Side-effect tools (sandbox writes, network) need human approval;
        read-only tools run straight through."""
        memory = LongTermMemory(tmp_path / "m.db")
        reg = ToolRegistry()
        try:
            register_builtins(reg, memory, sandbox_dir=str(tmp_path / "sandbox"))
            assert reg.needs_approval("write_file") is True
            assert reg.needs_approval("fetch") is True
            for safe in ("read_file", "list_files", "get_current_time", "remember", "recall", "list_memories"):
                assert reg.needs_approval(safe) is False, safe
        finally:
            memory.close()

    def test_get_current_time(self, registry: ToolRegistry) -> None:
        result = registry.execute("get_current_time", {})
        import datetime

        datetime.datetime.fromisoformat(result)  # must parse
        assert "+" in result  # has tz offset

    def test_remember_then_recall(self, registry: ToolRegistry) -> None:
        registry.execute("remember", {"key": "theme", "value": "dark"})
        assert registry.execute("recall", {"key": "theme"}) == "theme = dark"
        assert registry.execute("recall", {"key": "missing"}) == "在 scope 'default' 中未找到 'missing'"

    def test_list_memories(self, registry: ToolRegistry) -> None:
        registry.execute("remember", {"key": "a", "value": 1})
        registry.execute("remember", {"key": "b", "value": 2})
        listing = registry.execute("list_memories", {})
        assert "- a" in listing and "- b" in listing


class TestFetch:
    """fetch is offline-tested: the HTTP call is stubbed, never hits the network."""

    class _Resp:
        def __init__(self, text: str, status_code: int = 200) -> None:
            self.text = text
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                import httpx

                raise httpx.HTTPStatusError("bad", request=None, response=None)

    @pytest.fixture()
    def registry(self, tmp_path) -> ToolRegistry:
        memory = LongTermMemory(tmp_path / "m.db")
        reg = ToolRegistry()
        register_builtins(reg, memory)
        yield reg
        memory.close()

    def test_rejects_non_http_scheme(self, registry: ToolRegistry) -> None:
        result = registry.execute("fetch", {"url": "file:///etc/passwd"})
        assert "仅支持 http/https" in result

    def test_returns_body_with_headers(self, registry, monkeypatch) -> None:
        def fake_get(url, **kwargs):
            assert kwargs["timeout"] is not None  # bounded, never hangs forever
            return self._Resp('{"jobs": [{"title": "Python Dev"}]}')

        monkeypatch.setattr(http_mod.httpx, "get", fake_get)
        result = registry.execute("fetch", {"url": "https://example.com/api"})
        assert "HTTP 200" in result
        assert "Python Dev" in result

    def test_truncates_long_body(self, registry, monkeypatch) -> None:
        monkeypatch.setattr(http_mod.httpx, "get", lambda url, **kw: self._Resp("x" * 50000))
        result = registry.execute("fetch", {"url": "https://example.com/big"})
        assert "已截断" in result
        assert len(result) < 21_000

    def test_compacts_oversized_json(self, registry, monkeypatch) -> None:
        import json as _json

        big = _json.dumps(
            {"jobs": [{"title": f"job {i}", "desc": "y" * 500} for i in range(50)]}
        )
        monkeypatch.setattr(http_mod.httpx, "get", lambda url, **kw: self._Resp(big))
        result = registry.execute("fetch", {"url": "https://example.com/jobs"})
        assert "已精简" in result
        body = result.split("---\n", 1)[1]
        data = _json.loads(body)  # still valid JSON — never byte-sliced
        assert len(data["jobs"]) == 3
        assert data["jobs"][0]["desc"].endswith("…")  # long fields truncated

    def test_compacts_oversized_html(self, registry, monkeypatch) -> None:
        img = "<img src='https://img1.360buyimg.com/a.png'>"
        html = "<html><head><title>测试商品</title></head><body>" + img * 800 + "</body></html>"
        monkeypatch.setattr(http_mod.httpx, "get", lambda url, **kw: self._Resp(html))
        result = registry.execute("fetch", {"url": "https://example.com/product"})
        assert "已精简" in result
        assert "测试商品" in result  # <title> preserved
        assert "img1.360buyimg.com/a.png" in result  # image URL preserved
        assert "页面图片" in result

    def test_small_html_not_compacted(self, registry, monkeypatch) -> None:
        small = "<html><head><title>小页面</title></head><body>hi</body></html>"
        monkeypatch.setattr(http_mod.httpx, "get", lambda url, **kw: self._Resp(small))
        result = registry.execute("fetch", {"url": "https://example.com/small"})
        assert "已精简" not in result  # fits — returned as-is
        assert "<title>小页面</title>" in result

    def test_network_error_returns_text(self, registry, monkeypatch) -> None:
        def fake_get(url, **kwargs):
            raise http_mod.httpx.ConnectError("boom")  # a real httpx.HTTPError subclass

        monkeypatch.setattr(http_mod.httpx, "get", fake_get)
        result = registry.execute("fetch", {"url": "https://unreachable.invalid"})
        assert "请求失败" in result
