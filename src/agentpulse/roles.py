"""Multi-agent roles: Planner and Evaluator.

These are single-shot LLM calls that produce *structured* output (JSON),
unlike the Specialist's free-form tool loop:

- Planner: objective -> plan (list of subtasks with self-contained instructions)
- Evaluator: objective + plan + executed results -> pass/fail verdict with feedback

Both go through a tolerant JSON parser (strips markdown fences, extracts the
first JSON object, retries once on failure) so they work across providers
that don't guarantee strict JSON mode.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .llm import LiteLLMRouter

# -- JSON extraction ------------------------------------------------------------


def extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON object extraction from model output.

    Handles ```json fences, stray prose around the object, and trailing
    commas (common LLM mistakes). Returns None when nothing parseable.
    """
    if not text:
        return None
    text = text.strip()

    # strip markdown fences
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    # first '{' ... last '}' as the candidate object
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    candidate = text[start : end + 1]

    for attempt in (candidate, _fix_trailing_commas(candidate)):
        try:
            parsed = json.loads(attempt)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _fix_trailing_commas(text: str) -> str:
    # remove commas directly before } or ] (LLMs love trailing commas)
    return re.sub(r",\s*([}\]])", r"\1", text)


# -- Planner --------------------------------------------------------------------


PLANNER_PROMPT = """You are the PLANNER of a multi-agent task system. You never execute work yourself; you break objectives into subtasks that specialist executors will carry out.

Objective: {objective}

Break it into 2-5 concrete subtasks. IMPORTANT: every subtask is executed CONCURRENTLY in its own isolated sandbox directory, so each one MUST be fully self-contained:

- A subtask can NEVER depend on files or results produced by another subtask — they run in parallel and cannot see each other's files.
- Any data a subtask needs (a web fetch, a computation, a file it must create) must be obtained INSIDE that subtask itself.
- If a step truly depends on an earlier one, merge them into ONE subtask rather than chaining.
- Spell out exactly what to produce and how; the executor only sees your instruction.

Constraints:
- File I/O only happens inside the task's sandbox. Use RELATIVE paths only (e.g. `jobs_raw.json` or `notes/plan.md`). Absolute paths like /tmp, /app, /root, /home are rejected by the executor — they must never appear in instructions or artifacts.
- Each subtask's expected artifacts should be files it creates itself.

Language: write every `title`, `instruction` and `artifacts` in {language} — never in English.

Output ONLY a JSON object, no prose, in this exact shape:
{{
  "subtasks": [
    {{"title": "short title", "instruction": "detailed self-contained instruction", "artifacts": ["expected artifact paths or outputs"]}}
  ]
}}"""


_LANGUAGE_NAMES = {"zh": "简体中文", "en": "English", "ja": "日本語"}


def _lang_name(language: str) -> str:
    return _LANGUAGE_NAMES.get(language, language)


class Planner:
    def __init__(
        self,
        router: LiteLLMRouter,
        *,
        language: str = "zh",
        max_retries: int = 2,
        verbose: bool = False,
        model: str | None = None,
    ) -> None:
        self.router = router
        self.language = language
        self.max_retries = max_retries
        self.verbose = verbose
        self.model = model  # per-agent LLM override (None -> router default)

    def plan(self, objective: str) -> list[dict[str, Any]]:
        """Return a list of subtask dicts ({title, instruction, artifacts})."""
        lang = _lang_name(self.language)
        messages = [{"role": "user", "content": PLANNER_PROMPT.format(objective=objective, language=lang)}]
        last_error = "no usable plan output"
        for attempt in range(1, self.max_retries + 1):
            response = self.router.complete(messages, temperature=0.2, model=self.model)
            content = response.get("content") or ""
            parsed = extract_json(content)
            subtasks = (parsed or {}).get("subtasks") if parsed else None
            if isinstance(subtasks, list) and subtasks:
                normalized: list[dict[str, Any]] = []
                for i, st in enumerate(subtasks):
                    if not isinstance(st, dict) or not st.get("title") or not st.get("instruction"):
                        continue
                    normalized.append(
                        {
                            "index": i,
                            "title": str(st["title"])[:120],
                            "instruction": str(st["instruction"]),
                            "artifacts": [
                                str(a) for a in (st.get("artifacts") or []) if isinstance(a, str)
                            ],
                        }
                    )
                if normalized:
                    return normalized
            last_error = f"attempt {attempt} produced unparseable plan: {content[:200]!r}"
            # force a retry with explicit instruction to emit valid JSON
            messages = [
                {"role": "user", "content": PLANNER_PROMPT.format(objective=objective, language=lang)},
                {
                    "role": "assistant",
                    "content": "I must output ONLY a valid JSON object of the requested shape.",
                },
                {"role": "user", "content": "Output the JSON now."},
            ]
        raise ValueError(f"Planner failed after {self.max_retries} attempts: {last_error}")


# -- Evaluator --------------------------------------------------------------------


EVALUATOR_PROMPT = """You are the EVALUATOR of a multi-agent task system. You never execute work yourself; you verify whether an objective was truly met.

Objective: {objective}

Planned subtasks:
{plan}

Executed results (summaries from specialist executors):
{results}

Final deliverable:
{deliverable}

Judge strictly whether the objective is fully satisfied. If anything is unverified, missing, or vague, it must NOT pass. Output ONLY a JSON object, no prose:
{{
  "passed": true or false,
  "score": 0-100,
  "feedback": "what is missing or how to improve",
  "missing": ["concrete missing items"]
}}

Language: write every `feedback` and every item of `missing` in {language} — never in English."""


class Evaluator:
    def __init__(
        self,
        router: LiteLLMRouter,
        *,
        language: str = "zh",
        max_retries: int = 2,
        verbose: bool = False,
        model: str | None = None,
    ) -> None:
        self.router = router
        self.language = language
        self.max_retries = max_retries
        self.verbose = verbose
        self.model = model  # per-agent LLM override (None -> router default)

    def evaluate(
        self,
        objective: str,
        plan: list[dict[str, Any]],
        results: list[dict[str, Any]],
        deliverable: str,
    ) -> dict[str, Any]:
        """Return {passed, score, feedback, missing} (with safe defaults)."""
        prompt = EVALUATOR_PROMPT.format(
            objective=objective,
            plan=_format_plan(plan),
            results=_format_results(results),
            deliverable=deliverable[:6000] or "(empty)",
            language=_lang_name(self.language),
        )
        messages = [{"role": "user", "content": prompt}]
        last_error = "no usable verdict output"
        for attempt in range(1, self.max_retries + 1):
            response = self.router.complete(messages, temperature=0.0, model=self.model)
            content = response.get("content") or ""
            parsed = extract_json(content)
            if parsed is not None and "passed" in parsed:
                return {
                    "passed": bool(parsed["passed"]),
                    "score": int(parsed.get("score", 0)),
                    "feedback": str(parsed.get("feedback", "")),
                    "missing": [str(m) for m in parsed.get("missing") or []],
                }
            last_error = f"attempt {attempt} produced unparseable verdict: {content[:200]!r}"
            messages.append(
                {"role": "assistant", "content": "I must output ONLY a valid JSON object of the requested shape."}
            )
            messages.append({"role": "user", "content": "Output the JSON now."})
        raise ValueError(f"Evaluator failed after {self.max_retries} attempts: {last_error}")


# -- helpers ----------------------------------------------------------------------


def _format_plan(plan: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{st['index']}] {st['title']}: {st['instruction']}" for st in plan)


def _format_results(results: list[dict[str, Any]]) -> str:
    if not results:
        return "(no subtask results)"
    return "\n".join(
        f"- [{r['index']}] {r['title']}: {r.get('summary', '')[:800]}" for r in results
    )
