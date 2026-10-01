import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.exceptions import DomainError
from database.codelab_generation_log_persistence import CodelabGenerationLogPersistence
from database.codelab_persistence import CodelabPersistence
from database.models import CodelabGenerationLog, CodelabProblem
from services import codelab_sandbox
from services.blog_generation_service import _closest_title, _shingles, _slugify
from services.codelab_sandbox import UnsafeCodeError, normalize_output
from services.deploy_hook_service import DeployHookService
from services.llm_provider import GenerationFailedError, LLMProvider

logger = logging.getLogger("webnest.codelab_generation")

DIFFICULTIES = ("easy", "medium", "hard")
TRACK = "python"
LANGUAGE = "python"
POINTS = {"easy": 20, "medium": 40, "hard": 80}
ESTIMATED_MINUTES = {"easy": 10, "medium": 25, "hard": 45}
DIFFICULTY_GUIDE = {
    "easy": "solvable by a beginner in about 10 minutes with one loop or a couple of built-ins; no tricky edge cases",
    "medium": "needs a standard technique (sorting, hashing, two pointers, a stack, prefix sums, simple recursion) and "
    "some care with edge cases; about 25 minutes for an intermediate learner",
    "hard": "needs a non-obvious algorithm (dynamic programming, graph search, backtracking, binary search on the "
    "answer, heaps) and an efficient solution; about 45 minutes for a strong learner",
}
# The curriculum: each run picks the topic this difficulty has covered least,
# so the catalogue grows evenly instead of wherever the model drifts.
TOPICS = {
    "easy": [
        "strings", "lists", "loops and counting", "conditionals", "basic arithmetic", "dictionaries", "sets",
        "string formatting", "list comprehension", "min, max and sums", "simple simulation", "characters and ASCII",
    ],
    "medium": [
        "sorting", "hash maps", "two pointers", "stacks", "queues", "prefix sums", "binary search", "recursion",
        "matrix traversal", "string parsing", "greedy", "sliding window", "number theory",
    ],
    "hard": [
        "dynamic programming", "graph traversal (BFS/DFS)", "backtracking", "binary search on the answer", "heaps",
        "interval scheduling", "shortest paths", "bit manipulation", "dynamic programming on strings",
        "union find",
    ],
}  # fmt: skip

MAX_ATTEMPTS_PER_PROBLEM = 4
LLM_RETRY_PAUSE_SECONDS = 10.0
MIN_EXAMPLES = 2
MAX_EXAMPLES = 3
MIN_HIDDEN_TESTS = 5
MAX_HIDDEN_TESTS = 12
MAX_TEST_INPUT_CHARS = 5_000
MAX_TITLES_IN_PROMPT = 150
# A new statement sharing this much of its 3-word phrasing with an existing
# one is the same problem reworded.
STATEMENT_SHINGLE_OVERLAP = 0.4
# Same reasoning as the blog scheduler: the daily cron fires a little "early"
# relative to when the previous run finished.
SCHEDULE_TOLERANCE = timedelta(hours=2)
# Published when the model's starter code turns out to already solve the
# problem (or is unusable): reads the input and leaves the logic to the learner.
DEFAULT_STARTER = (
    "import sys\n\n\ndef solve():\n    data = sys.stdin.read().split()\n"
    "    # Write your solution here and print the answer.\n\n\nsolve()\n"
)

_SECTION = "<{tag}>\\s*(.*?)\\s*</{tag}>"
_LONE_BACKSLASH = re.compile(r'\\(?!["\\/bfnrtu])')
# LaTeX-style inline maths such as "$N$" or "$i+1$": a short token with no
# whitespace between the dollars. Requiring no whitespace is what keeps real
# dollar signs ("costs $5 and $10", "'$' as the delimiter") untouched.
_INLINE_MATH = re.compile(r"\$([^\s$]{1,30})\$")
_CODE_FENCE = re.compile(r"^```[a-zA-Z0-9]*\s*\n|\n```\s*$")


class CodelabGenerationError(Exception):
    """A problem could not be generated and verified. Always logged; callers
    must catch it so one bad difficulty never stops the rest of a run."""


class ProblemRejected(Exception):
    """One draft failed validation or verification. The pipeline tries again
    with a fresh draft."""


@dataclass(frozen=True)
class GenerationResult:
    difficulty: str
    success: bool
    detail: str
    slug: str | None = None


class CodelabGenerationService:
    """Generates CodeLab practice problems and publishes only the ones it can
    verify. The model supplies the statement, test *inputs* and a reference
    solution; expected outputs are never taken from the model - they are what
    the reference solution prints. A problem is published only if a second
    solution, written independently from the statement alone, prints the same
    output on every test, the statement's own examples match, the runs are
    deterministic, and the tests cannot be passed by the starter code."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._codelab = CodelabPersistence(session)
        self._logs = CodelabGenerationLogPersistence(session)
        self._settings = settings
        self._llm = LLMProvider(settings)
        self._deploy_hook = DeployHookService(settings)

    async def list_recent_logs(self, limit: int = 30) -> list[CodelabGenerationLog]:
        return await self._logs.list_recent(limit=limit)

    async def due_difficulties(self) -> list[str]:
        """Difficulties with no successful generation within the configured
        interval - what the scheduled job should produce now."""
        interval = timedelta(days=self._settings.codelab_generation_interval_days) - SCHEDULE_TOLERANCE
        now = datetime.now(timezone.utc)
        due = []
        for difficulty in DIFFICULTIES:
            latest = await self._logs.latest_success_at(difficulty)
            if latest is None or now - _as_aware(latest) >= interval:
                due.append(difficulty)
        return due

    async def generate_set(self, trigger_source: str, difficulties: list[str] | tuple[str, ...] = DIFFICULTIES) -> list[GenerationResult]:
        """Generates one problem per requested difficulty. Never raises: each
        difficulty succeeds or fails on its own and is logged either way."""
        results: list[GenerationResult] = []
        for difficulty in difficulties:
            try:
                problem = await self.generate_and_publish(difficulty, trigger_source)
                results.append(GenerationResult(difficulty, True, "Problem generated and published", problem.slug))
            except CodelabGenerationError as exc:
                results.append(GenerationResult(difficulty, False, str(exc)))
            except Exception as exc:
                # One difficulty hitting a bug must not cost the other two.
                logger.critical("Unexpected error generating a %s CodeLab problem", difficulty, exc_info=True)
                results.append(GenerationResult(difficulty, False, f"Unexpected error: {type(exc).__name__}"))

        published = [r.slug for r in results if r.success]
        if published:
            # Problem pages are pre-rendered by the frontend build. One rebuild
            # covers the whole set; trigger() never raises.
            await self._deploy_hook.trigger(f"codelab problems published: {', '.join(published)}")
        return results

    async def generate_and_publish(self, difficulty: str, trigger_source: str) -> CodelabProblem:
        if difficulty not in DIFFICULTIES:
            raise CodelabGenerationError(f"Unknown difficulty {difficulty!r}")
        topic = None
        try:
            existing = await self._codelab.list_all_problems()
            topic = _pick_topic(difficulty, await self._topic_attempts(difficulty))
            problem, provider = await self._generate_verified(difficulty, topic, existing)
        except GenerationFailedError as exc:
            logger.critical("CodeLab generation failed: both LLM providers are unavailable (%s)", exc)
            await self._log(trigger_source, difficulty, topic, success=False, error=str(exc))
            raise CodelabGenerationError(str(exc)) from exc
        except CodelabGenerationError as exc:
            logger.error("CodeLab %s generation failed: %s", difficulty, exc)
            await self._log(trigger_source, difficulty, topic, success=False, error=str(exc))
            raise
        except Exception as exc:
            # Anything unforeseen (e.g. the database being unreachable) is
            # still a logged, reported failure - never an unhandled error.
            logger.critical("Unexpected error generating a %s CodeLab problem", difficulty, exc_info=True)
            await self._log(trigger_source, difficulty, topic, success=False, error=f"Unexpected error: {type(exc).__name__}")
            raise CodelabGenerationError(f"Unexpected error: {type(exc).__name__}") from exc

        await self._log(trigger_source, difficulty, topic, success=True, provider=provider, problem=problem)
        logger.info(
            "Published CodeLab problem %r (slug=%s, difficulty=%s, topic=%s, tests=%s)",
            problem.title,
            problem.slug,
            difficulty,
            topic,
            len(problem.test_cases),
        )
        return problem

    # ---- Pipeline internals ----

    async def _generate_verified(
        self, difficulty: str, topic: str, existing: list[CodelabProblem]
    ) -> tuple[CodelabProblem, str]:
        avoid_titles = [p.title for p in existing if p.title][:MAX_TITLES_IN_PROMPT]
        last_reason = "no attempt was made"
        for attempt in range(1, MAX_ATTEMPTS_PER_PROBLEM + 1):
            draft: dict | None = None
            try:
                raw, provider = await self._llm.generate_text(
                    _build_problem_prompt(difficulty, topic, avoid_titles), "Write the problem now."
                )
                draft = _parse_problem(raw)
                _reject_duplicate(draft, existing)
                fields, test_cases = await self._verify(draft, difficulty, topic, provider)
            except ProblemRejected as exc:
                last_reason = str(exc)
                logger.warning(
                    "Rejected %s CodeLab draft (%s/%s): %s", difficulty, attempt, MAX_ATTEMPTS_PER_PROBLEM, last_reason
                )
                if draft is not None and draft["title"] not in avoid_titles:
                    avoid_titles = [draft["title"], *avoid_titles]
                continue
            except GenerationFailedError as exc:
                # Usually a provider's momentary overload - worth another try
                # after a pause rather than losing this difficulty for the run.
                if attempt == MAX_ATTEMPTS_PER_PROBLEM:
                    raise
                logger.warning("LLM providers unavailable for %s draft (%s/%s): %s", difficulty, attempt, MAX_ATTEMPTS_PER_PROBLEM, exc)
                await asyncio.sleep(LLM_RETRY_PAUSE_SECONDS * attempt)
                continue
            except Exception as exc:
                # A draft shaped in a way nothing above anticipated must cost
                # one attempt, not the run.
                last_reason = f"unexpected {type(exc).__name__} while checking the draft"
                logger.error("Unexpected error checking a %s CodeLab draft (%s/%s)", difficulty, attempt, MAX_ATTEMPTS_PER_PROBLEM, exc_info=True)
                continue
            try:
                return await self._codelab.create_problem(fields, test_cases), provider
            except DomainError as exc:
                # A slug taken by a concurrent run (ConflictError) or a
                # database hiccup - try again with a fresh draft.
                last_reason = exc.message
        raise CodelabGenerationError(
            f"No verifiable {difficulty} problem after {MAX_ATTEMPTS_PER_PROBLEM} attempt(s). Last reason: {last_reason}"
        )

    async def _verify(self, draft: dict, difficulty: str, topic: str, provider: str) -> tuple[dict, list[dict]]:
        """Runs every check on a draft and returns the problem fields and test
        cases to persist. Raises ProblemRejected with the reason otherwise."""
        examples = draft["examples"]
        inputs = [example["input"] for example in examples] + draft["test_inputs"]

        # 1. Expected outputs come from running the reference solution - twice,
        #    so a non-deterministic solution is caught.
        expected = await self._run_all(draft["reference_solution"], inputs, "reference solution")
        if await self._run_all(draft["reference_solution"], inputs, "reference solution") != expected:
            raise ProblemRejected("the reference solution is not deterministic")

        # A test whose correct output is empty is passed by a program that
        # prints nothing, so it proves nothing - drop it rather than the draft.
        if any(not output for output in expected[: len(examples)]):
            raise ProblemRejected("the reference solution prints nothing for an example")
        kept = [index for index, output in enumerate(expected) if output]
        inputs = [inputs[index] for index in kept]
        expected = [expected[index] for index in kept]
        if len(inputs) - len(examples) < MIN_HIDDEN_TESTS:
            raise ProblemRejected(f"fewer than {MIN_HIDDEN_TESTS} hidden tests have a non-empty expected output")
        if len(set(expected)) < 2:
            raise ProblemRejected("every test has the same expected output")

        # 2. The example outputs the model worked out by hand are frequently
        #    miscalculated, so they are not trusted either way: the published
        #    examples show what the verified code prints. An explanation
        #    written for a different answer is dropped.
        for index, example in enumerate(examples):
            if normalize_output(example["expected_output"]) != expected[index]:
                logger.info("Example %s of %r was hand-computed wrongly; using the verified output", index + 1, draft["title"])
                example["explanation"] = None

        # 3. A second solution, written by (where possible) the other provider
        #    from the statement alone - it sees no reference code and no
        #    expected outputs - must agree on every test. A wrong reference
        #    solution or an ambiguous statement shows up as a disagreement.
        solver_code, solver_provider = await self._independent_solution(draft, provider)
        solver_outputs = await self._run_all(solver_code, inputs, f"independent solution ({solver_provider})")
        for index, (ours, theirs) in enumerate(zip(expected, solver_outputs)):
            if ours != theirs:
                raise ProblemRejected(
                    f"the independent solution ({solver_provider}) disagrees with the reference solution on test {index + 1}"
                )

        # 4. The starter code must not already pass.
        starter = await self._usable_starter(draft["starter_code"], inputs, expected)

        fields = {
            "slug": draft["slug"],
            "title": draft["title"],
            "track": TRACK,
            "language": LANGUAGE,
            "difficulty": difficulty,
            "points": POINTS[difficulty],
            "estimated_minutes": ESTIMATED_MINUTES[difficulty],
            "status": "published",
            # The curriculum topic always leads; the model's own labels follow.
            "topics": [topic, *[label for label in draft["topics"] if label != topic.lower()]][:3],
            "statement": draft["statement"],
            "constraints": draft["constraints"],
            "hints": draft["hints"],
            "starter_files": [{"name": "main.py", "language": LANGUAGE, "content": starter}],
            "examples": [
                {"input": example["input"], "expected_output": expected[index], "explanation": example["explanation"]}
                for index, example in enumerate(examples)
            ],
        }
        test_cases = [
            {"input": test_input, "expected_output": expected[index], "is_hidden": index >= len(examples), "weight": 1}
            for index, test_input in enumerate(inputs)
        ]
        return fields, test_cases

    async def _topic_attempts(self, difficulty: str) -> dict[str, int]:
        try:
            return await self._logs.attempt_counts_by_topic(difficulty)
        except Exception:
            # The rotation is a nicety - without the log, start from the top
            # of the curriculum rather than fail the run.
            logger.warning("Could not read topic history for %s; using curriculum order", difficulty, exc_info=True)
            return {}

    async def _run_all(self, code: str, inputs: list[str], label: str) -> list[str]:
        outputs = []
        for index, test_input in enumerate(inputs):
            try:
                result = await codelab_sandbox.run_python(
                    code, test_input, timeout_seconds=self._settings.codelab_solution_timeout_seconds
                )
            except UnsafeCodeError as exc:
                raise ProblemRejected(f"the {label} was not run: {exc}") from exc
            if not result.ok:
                raise ProblemRejected(f"the {label} failed on test {index + 1}: {result.error}")
            outputs.append(normalize_output(result.stdout))
        return outputs

    async def _independent_solution(self, draft: dict, author_provider: str) -> tuple[str, str]:
        raw, provider = await self._llm.generate_text_avoiding(
            _SOLVER_PROMPT, _statement_for_solver(draft), avoid=author_provider
        )
        code = _extract_section(raw, "solution")
        if not code:
            raise ProblemRejected("the independent solver returned no code")
        return code, provider

    async def _usable_starter(self, starter: str, inputs: list[str], expected: list[str]) -> str:
        if not starter:
            return DEFAULT_STARTER
        try:
            outputs = await self._run_all(starter, inputs, "starter code")
        except ProblemRejected:
            # Unsafe, crashing or slow starter code is never handed to learners.
            return DEFAULT_STARTER
        return DEFAULT_STARTER if outputs == expected else starter

    async def _log(
        self,
        trigger_source: str,
        difficulty: str,
        topic: str | None,
        success: bool,
        provider: str | None = None,
        problem: CodelabProblem | None = None,
        error: str | None = None,
    ) -> None:
        try:
            await self._logs.create(
                success=success,
                difficulty=difficulty,
                topic=topic,
                llm_used=provider,
                problem_id=problem.id if problem is not None else None,
                problem_slug=problem.slug if problem is not None else None,
                error_message=error[:2000] if error else None,
                trigger_source=trigger_source,
            )
        except Exception:
            logger.error("Could not write a codelab_generation_logs row", exc_info=True)


def _pick_topic(difficulty: str, attempts: dict[str, int]) -> str:
    """The curriculum topic this difficulty has been run on least (first in
    curriculum order on a tie). `attempts` is keyed by the curriculum's own
    topic names as recorded in the generation log - not by whatever labels
    the model put on a problem - and includes failed runs, so neither a
    relabelled topic nor one that keeps failing can pin the rotation."""
    return min(TOPICS[difficulty], key=lambda topic: attempts.get(topic, 0))


def _build_problem_prompt(difficulty: str, topic: str, avoid_titles: list[str]) -> str:
    avoid = "; ".join(f'"{t}"' for t in avoid_titles) if avoid_titles else "(none yet)"
    return f"""You write practice problems for CodeLab, the coding-practice section of Webnest
Studio. Write ONE {difficulty} Python problem on the topic: {topic}.

Difficulty "{difficulty}" means: {DIFFICULTY_GUIDE[difficulty]}.

How the problem is judged:
- The learner writes a complete Python 3 program that reads from standard
  input and prints to standard output. Their output is compared with the
  expected output exactly (trailing whitespace ignored).
- So the problem must have exactly ONE correct output for every input. Do NOT
  write a problem where several answers are acceptable ("any valid order",
  "any one of"), where the answer is a floating-point number, or where the
  output depends on randomness or the current time. If ties are possible, the
  statement must say precisely how to break them.

Requirements:
- A fresh problem with a short real-world or puzzle framing - not a renamed
  classic. Do NOT reuse or lightly reword any of these existing problems: {avoid}
- Plain text and markdown only: no LaTeX and no backslashes anywhere outside
  the code (write "1 <= n <= 100", not maths markup).
- The statement: clear markdown. Describe the task completely and precisely
  enough that a programmer could solve it without seeing any example, then a
  "**Input format**" paragraph and an "**Output format**" paragraph that
  specify every line exactly. Do not include the examples in the statement.
- "constraints": the exact limits on every input value (sizes, ranges). Keep
  sizes small enough that a correct solution runs well within 2 seconds.
- "examples": {MIN_EXAMPLES}-{MAX_EXAMPLES} worked examples, each with "input", "expected_output"
  and a one-sentence "explanation".
- "test_inputs": 8-10 further inputs that are valid under the constraints and
  different from the examples. Cover the smallest allowed input, the largest
  allowed sizes or values, duplicates, negatives or zero where allowed, and
  cases where an obvious wrong approach fails. Give inputs only - no outputs.
- "hints": 2-3 hints that guide without giving the solution away.
- "topics": 1-3 lowercase topic labels, the first being "{topic}".
- The reference solution must be a correct, complete program. It may import
  only: math, itertools, collections, functools, heapq, bisect, string, re,
  sys, fractions. No file, network or OS access.
- The starter code is what the learner starts from: it reads the input and
  leaves the logic as a comment to fill in. It must NOT solve the problem.

Reply in exactly this format and nothing else:

<statement>
(markdown statement)
</statement>
<problem_json>
{{
  "title": "...",
  "constraints": ["...", "..."],
  "hints": ["...", "..."],
  "topics": ["{topic}"],
  "examples": [{{"input": "...", "expected_output": "...", "explanation": "..."}}],
  "test_inputs": ["...", "..."]
}}
</problem_json>
<reference_solution>
(python code)
</reference_solution>
<starter_code>
(python code)
</starter_code>"""


_SOLVER_PROMPT = """You are solving a programming problem. Write a complete, correct Python 3
program that reads from standard input and prints exactly the required output.

You may import only: math, itertools, collections, functools, heapq, bisect,
string, re, sys, fractions. No file, network or OS access.

Reply in exactly this format and nothing else:

<solution>
(python code)
</solution>"""


def _statement_for_solver(draft: dict) -> str:
    """Everything a learner would see - and nothing the author's solution
    could leak through (no reference code, no hidden tests)."""
    parts = [f"# {draft['title']}", draft["statement"]]
    if draft["constraints"]:
        parts.append("Constraints:\n" + "\n".join(f"- {c}" for c in draft["constraints"]))
    # Sample inputs only, to pin down the input format. No outputs: the
    # solver has to derive the behaviour from the statement itself.
    for index, example in enumerate(draft["examples"], start=1):
        parts.append(f"Sample input {index}:\n{example['input']}")
    return "\n\n".join(parts)


def _extract_section(raw: str, tag: str) -> str:
    match = re.search(_SECTION.format(tag=tag), raw or "", re.DOTALL)
    if match is None:
        return ""
    return _CODE_FENCE.sub("", match.group(1).strip()).strip()


def _string_list(value: object, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()][:limit]


def _parse_problem(raw: str) -> dict:
    """Shape-validates the model's reply. Raises ProblemRejected on anything
    that could not become a well-formed problem."""
    text = _extract_section(raw, "problem_json")
    try:
        data = json.loads(text, strict=False)
    except json.JSONDecodeError:
        # Usually a stray backslash from maths notation ("\le", "\(") that
        # is not a valid JSON escape - keep it as a literal backslash.
        try:
            data = json.loads(_LONE_BACKSLASH.sub(r"\\\\", text), strict=False)
        except json.JSONDecodeError as exc:
            raise ProblemRejected(f"the problem JSON did not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise ProblemRejected("the problem JSON was not an object")

    title = data.get("title").strip() if isinstance(data.get("title"), str) else ""
    # Models sometimes wrap variables in LaTeX dollars ("$N$") despite the
    # prompt; the problem page renders plain markdown, so unwrap them.
    statement = _INLINE_MATH.sub(r"\1", _extract_section(raw, "statement"))
    if not title or len(title) > 200 or len(statement) < 80:
        raise ProblemRejected("the title or statement is missing or too short")

    examples = []
    for item in data.get("examples") if isinstance(data.get("examples"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("input"), str) and isinstance(item.get("expected_output"), str):
            explanation = item.get("explanation")
            examples.append(
                {
                    "input": item["input"],
                    "expected_output": item["expected_output"],
                    "explanation": explanation.strip() if isinstance(explanation, str) and explanation.strip() else None,
                }
            )
    examples = examples[:MAX_EXAMPLES]
    if len(examples) < MIN_EXAMPLES:
        raise ProblemRejected(f"fewer than {MIN_EXAMPLES} usable examples")

    seen = {example["input"].strip() for example in examples}
    test_inputs: list[str] = []
    for item in data.get("test_inputs") if isinstance(data.get("test_inputs"), list) else []:
        if isinstance(item, str) and item.strip() not in seen and len(item) <= MAX_TEST_INPUT_CHARS:
            seen.add(item.strip())
            test_inputs.append(item)
    test_inputs = test_inputs[:MAX_HIDDEN_TESTS]
    if len(test_inputs) < MIN_HIDDEN_TESTS:
        raise ProblemRejected(f"fewer than {MIN_HIDDEN_TESTS} distinct hidden test inputs")

    reference_solution = _extract_section(raw, "reference_solution")
    if not reference_solution:
        raise ProblemRejected("no reference solution was provided")

    return {
        "title": title,
        # Drop apostrophes first so "Nova's Labels" becomes "novas-labels", not "nova-s-labels".
        "slug": _slugify(title.replace("'", "").replace("’", "")),
        "statement": statement,
        "constraints": _string_list(data.get("constraints"), 20),
        "hints": _string_list(data.get("hints"), 5),
        "topics": [topic.lower() for topic in _string_list(data.get("topics"), 3)],
        "examples": examples,
        "test_inputs": test_inputs,
        "reference_solution": reference_solution,
        "starter_code": _extract_section(raw, "starter_code"),
    }


def _reject_duplicate(draft: dict, existing: list[CodelabProblem]) -> None:
    draft_shingles = _shingles(draft["statement"])
    for problem in existing:
        if problem.slug == draft["slug"]:
            raise ProblemRejected(f"slug {draft['slug']!r} already exists")
        if problem.title and _closest_title(draft["title"], [problem.title]) is not None:
            raise ProblemRejected(f"title closely matches existing problem {problem.title!r}")
        other = _shingles(problem.statement or "")
        if draft_shingles and other and len(draft_shingles & other) / len(draft_shingles | other) >= STATEMENT_SHINGLE_OVERLAP:
            raise ProblemRejected(f"statement closely matches existing problem {problem.title!r}")


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
