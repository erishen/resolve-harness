"""Runtime code generation: let the agent write its own fast-path detectors.

When a query misses the built-in fast path, the harness asks the LLM whether
it can be solved deterministically with a pure-Python function. If yes, the
LLM emits a `detect(text) -> str | None` function which is:

  1. validated against an AST whitelist (no imports, no dunder access),
  2. executed with a whitelisted builtins namespace (no I/O, no eval),
  3. run under a hard timeout,
  4. on success, persisted as a plugin under `data/fastpath_plugins/` so the
     next identical query hits it without any LLM call.

Safety model: the generated code can compute on strings/numbers/lists, but
cannot import, open files, touch the network, reach dunder attributes, or
call eval/exec — it can only ever answer a text query.
"""

from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import inspect
import math
import re
from pathlib import Path
from typing import Any, Callable

# -- whitelists -------------------------------------------------------------------

# Modules the generated code may import — pure computation only, no I/O.
_SAFE_MODULES: dict[str, Any] = {"re": re, "math": math}


def _safe_import(name: str, *args: Any, **kwargs: Any) -> Any:
    if name.split(".")[0] not in _SAFE_MODULES:
        raise ImportError(f"module {name!r} is not allowed")
    return _SAFE_MODULES[name.split(".")[0]]


# Builtins the generated code may call (no I/O, no eval/exec, no introspection).
SAFE_BUILTINS: dict[str, Any] = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "sorted": sorted,
    "reversed": reversed,
    "range": range,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "dict": dict,
    "list": list,
    "set": set,
    "tuple": tuple,
    "repr": repr,
    "format": format,
    "pow": pow,
    "divmod": divmod,
    "ord": ord,
    "chr": chr,
    "isinstance": isinstance,
    "str_contains": lambda a, b: b in a,
    "__import__": _safe_import,  # restricted: only re / math
}

# Names that are never allowed even if they appear as plain Names.
_FORBIDDEN_NAMES = {
    "open", "eval", "exec", "compile", "globals", "locals", "getattr",
    "setattr", "vars", "dir", "__import__", "input", "exit", "quit",
    "memoryview", "breakpoint", "help",
}

# Method-ish attributes allowed on objects (e.g. text.upper(), parts.split()).
# Anything starting with "_" is already banned; this is a second, explicit list
# for extra safety on the most dangerous looking ones.
_FORBIDDEN_ATTRS = {"eval", "exec", "format", "globals", "locals", "mro", "subclasses", "init"}

# AST nodes generated code is allowed to use.
_ALLOWED_NODES = (
    ast.Module,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Return,
    ast.Expr,
    ast.Assign,
    ast.AnnAssign,
    ast.Call,
    ast.Name,
    ast.Constant,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.If,
    ast.IfExp,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.Subscript,
    ast.Slice,
    ast.Lambda,
    ast.comprehension,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.keyword,
    ast.Starred,
    ast.Load,
    ast.Store,
    ast.Del,
    ast.Pass,
    # operators (pure math/logic — ast.walk visits them as nodes)
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.LShift, ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd, ast.MatMult,
    ast.UAdd, ast.USub, ast.Not, ast.Invert,
    ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
    ast.In, ast.NotIn,
    ast.Attribute,  # safe methods only: no dunder (checked in validate_ast)
    ast.For,
    ast.Break,
    ast.Continue,
    ast.Try,
    ast.ExceptHandler,
    ast.Import,  # module restricted to re/math (checked in validate_ast)
    ast.ImportFrom,
    ast.alias,  # name container inside import statements
    ast.JoinedStr,  # f-strings (pure formatting, safe)
    ast.FormattedValue,
)


class CodeGenError(ValueError):
    """Raised when generated code fails validation or execution."""


def validate_ast(tree: ast.AST) -> None:
    """Reject anything outside the whitelist. Raises CodeGenError."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            raise CodeGenError(f"forbidden construct: {type(node).__name__}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in _SAFE_MODULES:
                    raise CodeGenError(f"forbidden import: {alias.name!r}")
            continue
        if isinstance(node, ast.ImportFrom):
            if not node.module or node.module.split(".")[0] not in _SAFE_MODULES:
                raise CodeGenError(f"forbidden import: {node.module!r}")
            continue
        if not isinstance(node, _ALLOWED_NODES):
            raise CodeGenError(f"unsupported construct: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise CodeGenError(f"forbidden name: {node.id!r}")
        if isinstance(node, ast.Attribute):
            attr = node.attr
            if attr.startswith("_") or attr in _FORBIDDEN_ATTRS:
                raise CodeGenError(f"forbidden attribute: {attr!r}")


def run_detector(source: str, text: str, timeout: float = 3.0) -> str | None:
    """Validate + execute a generated `detect(text) -> str | None`.

    Returns the answer string, or None when the function returns None /
    raises / is unsafe / times out. Never raises for user input.
    """
    try:
        tree = ast.parse(source, mode="exec")
        validate_ast(tree)
    except (SyntaxError, CodeGenError) as exc:
        return None

    safe_globals: dict[str, Any] = {"__builtins__": SAFE_BUILTINS}
    namespace: dict[str, Any] = {}
    try:
        exec(compile(tree, "<generated>", "exec"), safe_globals, namespace)  # noqa: S102 - sandboxed
    except Exception:  # noqa: BLE001
        return None

    detect = namespace.get("detect")
    if not callable(detect):
        return None

    try:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(detect, text)
            result = future.result(timeout=timeout)
        except Exception:  # noqa: BLE001 - timeout, exception, anything
            result = None
        finally:
            # workers are daemon threads; don't join a runaway generator
            pool.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001
        return None

    if isinstance(result, str) and result.strip():
        return result.strip()
    return None


def extract_code(llm_output: str) -> str | None:
    """Pull a Python function out of the model's reply.

    Handles ```python fences or a bare def-block; returns None when the model
    decided the query is not code-resolvable (NONE / 无法 / no code).
    """
    text = llm_output.strip()
    if not text:
        return None
    # explicit negative answer
    if re.fullmatch(r"(?i)\s*(NONE|NO|无|无法|不能|不需要)\s*", text):
        return None
    if text.upper().startswith("NONE") or "cannot" in text.lower() and "def " not in text:
        return None
    # fenced block
    fenced = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    # bare function starting at "def detect"
    m = re.search(r"def\s+detect\s*\(.*", text, re.DOTALL)
    if m:
        return m.group(0).strip()
    return None


# -- plugin persistence ----------------------------------------------------------------

# default plugin dir: <project root>/data/fastpath_plugins
def default_plugin_dir() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent / "data" / "fastpath_plugins"
    return Path.cwd() / "data" / "fastpath_plugins"


def save_plugin(
    source: str,
    plugin_dir: str | Path | None = None,
    trigger: str | None = None,
) -> str:
    """Persist a validated detector source as a plugin file. Returns its name.

    The filename is a hash of the source, so identical detectors dedupe to a
    single file (idempotent — re-saving is a no-op). An optional trigger
    query is written as a header comment so the plugin stays human-readable.
    """
    # validate before persisting — never write unsafe code to disk
    try:
        tree = ast.parse(source, mode="exec")
        validate_ast(tree)
    except (SyntaxError, CodeGenError) as exc:
        raise CodeGenError(f"refusing to persist unsafe plugin: {exc}") from exc

    directory = Path(plugin_dir) if plugin_dir else default_plugin_dir()
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:10]
    name = f"gen_{digest}"
    target = directory / f"{name}.py"
    if target.exists():
        return name  # already persisted — dedupe

    body = source
    if trigger:
        one_line = " ".join(trigger.split())[:80]
        body = f"# trigger: {one_line}\n{source}"
    target.write_text(body, encoding="utf-8")
    return name


# module-level plugin cache: {plugin_dir: (file_stats, detectors)}
# file_stats maps name -> (mtime, size); any change triggers a full reload,
# so newly persisted plugins are picked up on the next query.
_load_cache: dict[str, tuple[dict[str, tuple[float, int]], list[Callable[[str], str | None]]]] = {}


def load_plugins(plugin_dir: str | Path | None = None) -> list[Callable[[str], str | None]]:
    """Load persisted detector plugins (also sandboxed — same validator).

    Returns a list of `detect` callables from every valid plugin file.
    Results are cached per directory and invalidated when any file's
    mtime/size changes (e.g. a new plugin persisted by codegen).
    """
    directory = Path(plugin_dir) if plugin_dir else default_plugin_dir()
    key = str(directory)
    if not directory.exists():
        _load_cache.pop(key, None)
        return []

    stats: dict[str, tuple[float, int]] = {}
    for py in sorted(directory.glob("*.py")):
        try:
            s = py.stat()
        except OSError:
            continue
        stats[py.name] = (s.st_mtime, s.st_size)

    cached = _load_cache.get(key)
    if cached is not None and cached[0] == stats:
        return cached[1]

    detectors: list[Callable[[str], str | None]] = []
    for py in sorted(directory.glob("*.py")):
        source = py.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, mode="exec")
            validate_ast(tree)
        except (SyntaxError, CodeGenError):
            continue  # skip corrupt/unsafe plugin
        safe_globals: dict[str, Any] = {"__builtins__": SAFE_BUILTINS}
        namespace: dict[str, Any] = {}
        try:
            exec(compile(tree, str(py), "exec"), safe_globals, namespace)  # noqa: S102
        except Exception:  # noqa: BLE001
            continue
        detect = namespace.get("detect")
        if callable(detect):
            detectors.append(detect)

    _load_cache[key] = (stats, detectors)
    return detectors


# -- LLM-driven generation -------------------------------------------------------------

CODE_GEN_PROMPT = r"""你是 fast-path 代码生成器。判断下面的用户问题能否用**纯 Python 函数**确定性解决（数学计算、格式转换、字符串/列表处理、日期等——不需要网络、文件或外部库）。

问题：{query}

如果能解决，只输出一个 Python 函数（不要解释、不要多余文字）：

def detect(text: str) -> str | None:
    # 从 text 中提取所需信息，返回完整自然的答案字符串（中文）；
    # 如果 text 不是这类问题，返回 None。
    ...

规则：
- 可以 import 的模块：仅 re、math（正则、数学函数）。禁止其它任何 import、禁止 open、网络访问、eval/exec/compile、getattr/setattr、任何以下划线开头的方法（如 __class__）、类定义、全局变量读写。
- 可以使用 try/except 兜住解析失败（except 里返回 None）。
- 只能使用：算术运算、len/str/int/float/abs/round/min/max/sum/sorted/range/enumerate/zip/reversed/dict/list/set/tuple/repr/format/pow/divmod/ord/chr/isinstance，以及字符串/列表/字典的基本方法和下标。
- 函数必须健壮：对不相关输入返回 None，绝不抛异常。
- 正则转义提醒：在代码块里写原始字符串 r'\d+'（单个反斜杠），千万不要写成 '\\d'（双反斜杠会让正则去匹配字面反斜杠）。
- 答案要完整自然，例如「2 加 3 等于 5。」。

如果不能确定性解决（需要常识、写作、开放推理）→ 只输出 NONE。"""


def codegen_solve(
    router: Any,
    query: str,
    *,
    plugin_dir: str | Path | None = None,
    max_attempts: int = 2,
) -> str | None:
    """Ask the LLM to generate a detector for `query`, validate & run it.

    On success the answer is returned AND the detector is persisted as a
    plugin, so the next identical query resolves without any model call.
    Returns None when the model declines / code is unsafe / doesn't answer.

    Generation is retried once with feedback when the first candidate fails
    to match (LLM output is nondeterministic).
    """
    last_error = "detect() did not match the query"
    for attempt in range(max_attempts):
        prompt = CODE_GEN_PROMPT.format(query=query)
        if attempt > 0:
            prompt += (
                "\n\n你上一次生成的 detect() 无法匹配该问题"
                f"（{last_error}）。请重新生成修正后的函数，确保能返回答案。"
            )
        try:
            response = router.complete(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
            )
        except Exception:  # noqa: BLE001
            return None
        source = extract_code(response.get("content") or "")
        if not source:
            return None  # the model declined (NONE / no code) — stop
        answer = run_detector(source, query)
        if answer is not None:
            try:
                save_plugin(source, plugin_dir, trigger=query)
            except CodeGenError:
                pass  # answer stands even if persistence is refused
            return answer
        last_error = "上一版代码校验未通过或未命中"
    return None


# -- plugin management (list / delete / promote to source) ------------------------


def _plugin_dir(plugin_dir: str | Path | None) -> Path:
    return Path(plugin_dir) if plugin_dir else default_plugin_dir()


def _plugin_filename(name: str) -> str:
    """Only allow names of the form gen_<sha10> — no path traversal."""
    if not re.fullmatch(r"gen_[0-9a-f]{10}", name):
        raise CodeGenError(f"invalid plugin name: {name!r}")
    return f"{name}.py"


def list_plugins(plugin_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """List persisted plugins with metadata + source (for the management UI)."""
    directory = _plugin_dir(plugin_dir)
    if not directory.exists():
        return []
    out: list[dict[str, Any]] = []
    for py in sorted(directory.glob("gen_*.py")):
        source = py.read_text(encoding="utf-8")
        trigger = ""
        m = re.search(r"^# trigger: (.*)$", source, re.MULTILINE)
        if m:
            trigger = m.group(1).strip()
        try:
            s = py.stat()
        except OSError:
            continue
        out.append(
            {
                "name": py.stem,
                "trigger": trigger,
                "source": source,
                "mtime": s.st_mtime,
                "size": s.st_size,
                "builtin": False,
            }
        )
    return out


def list_builtin_detectors() -> list[dict[str, Any]]:
    """Promoted detectors merged into `generated_detectors.py` (shipped with
    the source). Read-only: they cannot be deleted or promoted again."""
    from . import generated_detectors as _gd

    out: list[dict[str, Any]] = []
    for fn in getattr(_gd, "DETECTORS", []) or []:
        doc = inspect.getdoc(fn) or ""
        m = re.search(r"trigger:\s*(.*)", doc)
        try:
            source = inspect.getsource(fn)
        except (OSError, TypeError):
            source = ""
        out.append(
            {
                "name": fn.__name__,
                "trigger": m.group(1).strip() if m else "",
                "source": source,
                "mtime": 0,
                "size": len(source.encode("utf-8")),
                "builtin": True,
                "kind": "内置晋升",
            }
        )
    return out


def delete_plugin(name: str, plugin_dir: str | Path | None = None) -> bool:
    """Delete one persisted plugin file by name. Returns False if absent."""
    directory = _plugin_dir(plugin_dir)
    try:
        target = directory / _plugin_filename(name)
    except CodeGenError:
        return False
    if not target.exists():
        return False
    target.unlink()
    return True


# target file that promoted detectors are merged into (relative to package root)
def default_promote_target() -> Path:
    here = Path(__file__).resolve()
    return here.parent / "generated_detectors.py"


_GENERATED_HEADER = '''"""晋升的 fast-path 检测器（插件整理生成；函数体可自由修改，整体结构勿手改）。

由管理界面从 data/fastpath_plugins/ 选中合并生成。每个函数对应一个曾在
运行时生成过的确定性模式，晋升后成为内置检测器，随源码一起提交。
"""

from typing import Callable


'''


def promote_plugins(
    names: list[str],
    *,
    plugin_dir: str | Path | None = None,
    target_path: str | Path | None = None,
) -> int:
    """Merge selected plugins into `generated_detectors.py` and remove their
    runtime files.

    Each plugin becomes `detect_promoted_<n>` with its trigger query kept as a
    docstring, all collected in a module-level `DETECTORS` list. Returns the
    number of detectors promoted.
    """
    directory = _plugin_dir(plugin_dir)
    sources: list[tuple[str, str]] = []  # (trigger, source)
    for name in names:
        try:
            target = directory / _plugin_filename(name)
        except CodeGenError:
            continue
        if not target.exists():
            continue
        source = target.read_text(encoding="utf-8")
        # must still be safe before it becomes permanent source
        try:
            tree = ast.parse(source, mode="exec")
            validate_ast(tree)
        except (SyntaxError, CodeGenError):
            continue
        trigger = ""
        m = re.search(r"^# trigger: (.*)$", source, re.MULTILINE)
        if m:
            trigger = m.group(1).strip()
        sources.append((trigger, source))

    if not sources:
        raise CodeGenError("no valid plugins selected to promote")

    parts: list[str] = [_GENERATED_HEADER]
    func_names: list[str] = []
    for i, (trigger, source) in enumerate(sources, 1):
        func_name = f"detect_promoted_{i}"
        # strip the trigger comment line and rename the function
        body = re.sub(r"^# trigger: .*$", "", source, flags=re.MULTILINE)
        body = re.sub(
            r"^def\s+detect\b",
            f"def {func_name}",
            body,
            count=1,
            flags=re.MULTILINE,
        ).strip("\n")
        # insert trigger docstring as the first line of the function body
        doc = f'    """trigger: {trigger}"""' if trigger else f'    """promoted detector"""'
        def_line_end = body.find("\n")
        if def_line_end != -1:
            body = body[: def_line_end + 1] + doc + "\n" + body[def_line_end + 1 :]
        else:
            body += "\n" + doc
        parts.append(body + "\n\n\n")
        func_names.append(func_name)

    parts.append(f"DETECTORS: list[Callable[[str], str | None]] = [{', '.join(func_names)}]\n")
    target = Path(target_path) if target_path else default_promote_target()
    target.write_text("".join(parts), encoding="utf-8")

    # runtime copies are now redundant — remove them
    removed = 0
    for name in names:
        try:
            if delete_plugin(name, directory):
                removed += 1
        except CodeGenError:
            pass
    return len(func_names)
