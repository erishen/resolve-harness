"""Tests for the tool registry and built-in tools."""

from __future__ import annotations

import contextlib
import pytest

from resolve_harness.memory import LongTermMemory
from resolve_harness.tools import http as http_mod
from resolve_harness.tools.builtin import register_builtins
import resolve_harness.tools.builtin as builtin_mod
from resolve_harness.tools.registry import ToolError, ToolRegistry


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
            self._text = text
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                import httpx

                raise httpx.HTTPStatusError("bad", request=None, response=None)

        def iter_bytes(self, chunk_size: int = 8192):
            data = self._text.encode("utf-8")
            for i in range(0, len(data), chunk_size):
                yield data[i : i + chunk_size]

    @staticmethod
    @contextlib.contextmanager
    def _stream_cm(resp: "_Resp"):
        yield resp

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

    def test_rejects_private_host(self, registry: ToolRegistry, monkeypatch) -> None:
        calls: list[str] = []

        def fake_stream(method, url, **kwargs):
            calls.append(url)
            raise AssertionError("不应向内网地址发起请求")

        monkeypatch.setattr(http_mod.httpx, "stream", fake_stream)
        result = registry.execute("fetch", {"url": "http://127.0.0.1:8080/secret"})
        assert "SSRF" in result or "保留/内网" in result
        assert calls == []  # 根本没发出请求

    def test_returns_body_with_headers(self, registry, monkeypatch) -> None:
        def fake_stream(method, url, **kwargs):
            assert kwargs["timeout"] is not None  # bounded, never hangs forever
            return self._stream_cm(self._Resp('{"jobs": [{"title": "Python Dev"}]}'))

        monkeypatch.setattr(http_mod.httpx, "stream", fake_stream)
        result = registry.execute("fetch", {"url": "https://example.com/api"})
        assert "HTTP 200" in result
        assert "Python Dev" in result

    def test_truncates_long_body(self, registry, monkeypatch) -> None:
        monkeypatch.setattr(
            http_mod.httpx,
            "stream",
            lambda m, u, **kw: self._stream_cm(self._Resp("x" * 50000)),
        )
        result = registry.execute("fetch", {"url": "https://example.com/big"})
        assert "已截断" in result
        assert len(result) < 21_000

    def test_compacts_oversized_json(self, registry, monkeypatch) -> None:
        import json as _json

        # 列表远大于 _MAX_RECORDS，验证精简后保留的条数受上限约束
        big = _json.dumps(
            {"jobs": [{"title": f"job {i}", "desc": "y" * 500} for i in range(200)]}
        )
        monkeypatch.setattr(
            http_mod.httpx, "stream", lambda m, u, **kw: self._stream_cm(self._Resp(big))
        )
        result = registry.execute("fetch", {"url": "https://example.com/jobs"})
        assert "已精简" in result
        body = result.split("---\n", 1)[1]
        data = _json.loads(body)  # still valid JSON — never byte-sliced
        assert len(data["jobs"]) == http_mod._MAX_RECORDS
        assert data["jobs"][0]["desc"].endswith("…")  # long fields truncated

    def test_compacts_oversized_html(self, registry, monkeypatch) -> None:
        img = "<img src='https://img1.360buyimg.com/a.png'>"
        html = "<html><head><title>测试商品</title></head><body>" + img * 800 + "</body></html>"
        monkeypatch.setattr(
            http_mod.httpx, "stream", lambda m, u, **kw: self._stream_cm(self._Resp(html))
        )
        result = registry.execute("fetch", {"url": "https://example.com/product"})
        assert "已精简" in result
        assert "测试商品" in result  # <title> preserved
        assert "img1.360buyimg.com/a.png" in result  # image URL preserved
        assert "页面图片" in result

    def test_small_html_not_compacted(self, registry, monkeypatch) -> None:
        small = "<html><head><title>小页面</title></head><body>hi</body></html>"
        monkeypatch.setattr(
            http_mod.httpx, "stream", lambda m, u, **kw: self._stream_cm(self._Resp(small))
        )
        result = registry.execute("fetch", {"url": "https://example.com/small"})
        assert "已精简" not in result  # fits — returned as-is
        assert "<title>小页面</title>" in result

    def test_download_capped(self, registry, monkeypatch) -> None:
        # 超过下载上限（5MB）的响应应被截断，不会无限撑入内存
        huge = "x" * (http_mod._MAX_DOWNLOAD_BYTES + 10)
        monkeypatch.setattr(
            http_mod.httpx, "stream", lambda m, u, **kw: self._stream_cm(self._Resp(huge))
        )
        result = registry.execute("fetch", {"url": "https://example.com/huge"})
        assert "已截断" in result

    def test_network_error_returns_text(self, registry, monkeypatch) -> None:
        # 主机检查（解析失败的 .invalid 域名会被 SSRF 防护直接拒绝），此处桩掉
        # 该检查，专门验证「网络层报错以可读文本返回而非抛出」的路径。
        monkeypatch.setattr(http_mod, "_is_safe_host", lambda url: True)

        def fake_stream(method, url, **kwargs):
            raise http_mod.httpx.ConnectError("boom")  # a real httpx.HTTPError subclass

        monkeypatch.setattr(http_mod.httpx, "stream", fake_stream)
        result = registry.execute("fetch", {"url": "https://example.com/unreachable"})
        assert "请求失败" in result


class TestRunScript:
    def test_allows_allowlisted_and_writes_sandbox(self, tmp_path) -> None:
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "demo.py").write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "out = 'out.txt'\n"
            "if '--out' in sys.argv:\n"
            "    out = sys.argv[sys.argv.index('--out') + 1]\n"
            "Path(out).write_text('ran:' + ' '.join(sys.argv[1:]))\n"
        )
        saved = builtin_mod.RUN_SCRIPT_ALLOWLIST
        builtin_mod.RUN_SCRIPT_ALLOWLIST = frozenset({"demo"})
        try:
            fn = builtin_mod._make_run_script_tool(str(tmp_path), scripts_dir=str(scripts))
            out = fn("demo", "--out futures.md")
            assert "退出码 0" in out
            assert (tmp_path / "futures.md").read_text(encoding="utf-8") == "ran:--out futures.md"
            # 白名单外脚本被拒绝
            assert "不允许的脚本" in fn("evil")
        finally:
            builtin_mod.RUN_SCRIPT_ALLOWLIST = saved

    def test_no_sandbox_unavailable(self) -> None:
        fn = builtin_mod._make_run_script_tool(None)
        assert "未配置沙箱" in fn("fetch_shfe_futures")

    def test_subprocess_env_strips_secrets(self, tmp_path, monkeypatch) -> None:
        """子进程只拿最小环境：父进程的 API key 等敏感变量不得透传，
        但 PATH 等脚本运行必需变量保留。"""
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "demo.py").write_text(
            "import os, sys\n"
            "from pathlib import Path\n"
            "has_key = 'YES' if 'LLM_API_KEY' in os.environ else 'NO'\n"
            "has_path = 'YES' if os.environ.get('PATH') else 'NO'\n"
            "Path('futures.md').write_text(f'key={has_key} path={has_path}')\n"
        )
        monkeypatch.setenv("LLM_API_KEY", "sk-secret-probe")
        saved = builtin_mod.RUN_SCRIPT_ALLOWLIST
        builtin_mod.RUN_SCRIPT_ALLOWLIST = frozenset({"demo"})
        try:
            fn = builtin_mod._make_run_script_tool(str(tmp_path), scripts_dir=str(scripts))
            out = fn("demo", "")
            assert "退出码 0" in out
            body = (tmp_path / "futures.md").read_text(encoding="utf-8")
            assert "key=NO" in body, f"API key 泄漏进了子进程环境: {out}"
            assert "path=YES" in body
        finally:
            builtin_mod.RUN_SCRIPT_ALLOWLIST = saved

    def test_out_argument_confined_to_sandbox(self, tmp_path) -> None:
        """模型传入的绝对/越界 --out 必须被忽略，产出始终落在沙箱内 futures.md。"""
        import os

        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "demo.py").write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "out = 'out.txt'\n"
            "if '--out' in sys.argv:\n"
            "    out = sys.argv[sys.argv.index('--out') + 1]\n"
            "Path(out).write_text('ran:' + ' '.join(sys.argv[1:]))\n"
        )
        saved = builtin_mod.RUN_SCRIPT_ALLOWLIST
        builtin_mod.RUN_SCRIPT_ALLOWLIST = frozenset({"demo"})
        evil = "/tmp/resolve_harness_run_script_evil_probe"
        if os.path.exists(evil):
            os.remove(evil)
        try:
            fn = builtin_mod._make_run_script_tool(str(tmp_path), scripts_dir=str(scripts))
            # 尝试逃出沙箱：绝对路径
            out = fn("demo", f"--out {evil}")
            assert "退出码 0" in out
            # 沙箱内固定名落地
            assert (tmp_path / "futures.md").read_text(encoding="utf-8") == "ran:--out futures.md"
            # 越界路径绝不应被创建
            assert not os.path.exists(evil), "run_script 把文件写到了沙箱外！"
        finally:
            builtin_mod.RUN_SCRIPT_ALLOWLIST = saved
            if os.path.exists(evil):
                os.remove(evil)

    def test_real_fetch_shfe_when_network(self, tmp_path) -> None:
        import urllib.request

        try:
            urllib.request.urlopen("https://www.shfe.com.cn", timeout=5)
        except Exception:
            pytest.skip("no network to shfe.com.cn")
        fn = builtin_mod._make_run_script_tool(str(tmp_path))
        out = fn("fetch_shfe_futures")
        # 非交易日（如周末/休市）上期所不发布日行情，数据源会 404/失败——属环境
        # 性，跳过而非当作失败，保证工作日运行时该集成测试有效。
        if "404" in out or "均失败" in out:
            pytest.skip("SHFE 当日无行情（非交易日），跳过实时抓取断言")
        assert "退出码 0" in out
        md = (tmp_path / "futures.md").read_text(encoding="utf-8")
        assert "涨幅" in md
