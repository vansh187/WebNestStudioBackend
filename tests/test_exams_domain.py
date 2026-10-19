"""Pure exam logic: draw, grading, validation, duplicates, certificate IDs,
pool targets and item statistics. No I/O."""

import os
import random
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SUPABASE_URL", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest

from exams.domain.certificate_id import ALPHABET, build_certificate_id, normalize_certificate_id
from exams.domain.dedupe import PoolEntry, concept_is_full, cosine, semantic_duplicate, textual_duplicate
from exams.domain.draw import DrawCandidate, DrawModule, allocate_quotas, draw
from exams.domain.grading import KeyEntry, grade, is_pass, percent_of, regrade
from exams.domain.pool import module_targets, pool_is_full
from exams.domain.validation import (
    DraftQuestion,
    check_shape,
    clean_recipient_name,
    has_raw_html,
    is_grounded,
    jaccard,
    normalize_text,
    prompt_hash,
    tokens,
)
from exams.errors import CrossCourseQuestion, PoolNotReady
from exams.services.maintenance_service import item_statistics

NOW = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)


def make_pool(course_id, module_sizes, prefix="c"):
    modules, candidates = [], []
    for m_index, (lessons, questions) in enumerate(module_sizes):
        module_id = uuid.uuid4()
        modules.append(DrawModule(id=module_id, lesson_count=lessons))
        for q in range(questions):
            candidates.append(
                DrawCandidate(
                    id=uuid.uuid4(),
                    course_id=course_id,
                    module_id=module_id,
                    difficulty=("easy", "medium", "medium", "hard", "easy")[q % 5],
                    concept=f"{prefix}-m{m_index}-concept-{q}",
                )
            )
    return modules, candidates


# --------------------------------------------------------------------------- #
# Draw
# --------------------------------------------------------------------------- #
def test_draw_returns_30_unique_questions_and_concepts_with_permutations():
    course = uuid.uuid4()
    modules, candidates = make_pool(course, [(10, 30), (5, 30), (3, 30)])
    drawn = draw(course, modules, candidates, {}, 30, random.Random(1))
    assert len(drawn) == 30
    assert len({d.question_id for d in drawn}) == 30
    by_id = {c.id: c for c in candidates}
    assert len({by_id[d.question_id].concept for d in drawn}) == 30
    for d in drawn:
        assert sorted(d.permutation) == [0, 1, 2, 3]
    assert {d.module_id for d in drawn} == {m.id for m in modules}


def test_draw_quotas_follow_lesson_counts():
    course = uuid.uuid4()
    modules, candidates = make_pool(course, [(20, 40), (10, 40)])
    drawn = draw(course, modules, candidates, {}, 30, random.Random(2))
    per_module = {m.id: sum(1 for d in drawn if d.module_id == m.id) for m in modules}
    assert per_module[modules[0].id] == 20
    assert per_module[modules[1].id] == 10


def test_every_module_gets_at_least_one_question():
    course = uuid.uuid4()
    modules, candidates = make_pool(course, [(50, 60), (1, 5), (1, 5)])
    drawn = draw(course, modules, candidates, {}, 30, random.Random(3))
    assert all(any(d.module_id == m.id for d in drawn) for m in modules)


def test_shortfall_moves_to_modules_with_spare_questions():
    course = uuid.uuid4()
    modules, candidates = make_pool(course, [(10, 3), (10, 40)])
    drawn = draw(course, modules, candidates, {}, 30, random.Random(4))
    assert sum(1 for d in drawn if d.module_id == modules[0].id) == 3
    assert len(drawn) == 30


def test_draw_is_deterministic_with_a_seeded_random():
    course = uuid.uuid4()
    modules, candidates = make_pool(course, [(10, 40), (8, 40)])
    first = draw(course, modules, candidates, {}, 30, random.Random(42))
    second = draw(course, modules, list(reversed(candidates)), {}, 30, random.Random(42))
    assert first == second


def test_pool_smaller_than_an_exam_is_not_ready():
    course = uuid.uuid4()
    modules, candidates = make_pool(course, [(10, 29)])
    with pytest.raises(PoolNotReady):
        draw(course, modules, candidates, {}, 30, random.Random(5))


def test_never_a_seen_question_while_unseen_ones_exist():
    course = uuid.uuid4()
    modules, candidates = make_pool(course, [(10, 45), (10, 45)])
    rng = random.Random(6)
    seen: dict = {}
    for attempt in range(3):  # a 90-question pool gives 3 papers with no repeats
        drawn = draw(course, modules, candidates, seen, 30, rng)
        ids = {d.question_id for d in drawn}
        assert not ids & set(seen), f"attempt {attempt + 1} repeated a question"
        for qid in ids:
            seen[qid] = NOW + timedelta(days=attempt)
    # 4th attempt: everything was seen once; it takes the oldest-seen ones.
    fourth = draw(course, modules, candidates, seen, 30, rng)
    oldest = {qid for qid, at in seen.items() if at == NOW}
    assert {d.question_id for d in fourth} == oldest


def test_retake_prefers_unseen_even_against_quota():
    course = uuid.uuid4()
    modules, candidates = make_pool(course, [(10, 40), (10, 40)])
    seen = {c.id: NOW for c in candidates if c.module_id == modules[0].id}
    drawn = draw(course, modules, candidates, seen, 30, random.Random(7))
    assert all(d.question_id not in seen for d in drawn)


def test_cross_course_isolation_over_1000_draws():
    java, advanced, postgres = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    java_modules, java_pool = make_pool(java, [(10, 45), (10, 45)], "java")
    _, adv_pool = make_pool(advanced, [(10, 90)], "adv")
    _, pg_pool = make_pool(postgres, [(10, 90)], "pg")
    java_ids = {c.id for c in java_pool}
    rng = random.Random(1000)
    for _ in range(1000):
        drawn = draw(java, java_modules, java_pool, {}, 30, rng)
        assert {d.question_id for d in drawn} <= java_ids


def test_a_question_of_another_course_is_refused_loudly():
    java, other = uuid.uuid4(), uuid.uuid4()
    modules, pool = make_pool(java, [(10, 40)])
    _, foreign = make_pool(other, [(10, 1)])
    with pytest.raises(CrossCourseQuestion):
        draw(java, modules, pool + foreign, {}, 30, random.Random(8))


def test_a_question_of_a_module_outside_the_course_is_refused():
    java = uuid.uuid4()
    modules, pool = make_pool(java, [(10, 40)])
    stray = DrawCandidate(id=uuid.uuid4(), course_id=java, module_id=uuid.uuid4(), difficulty="easy", concept="x")
    with pytest.raises(CrossCourseQuestion):
        draw(java, modules, pool + [stray], {}, 30, random.Random(9))


def test_history_of_another_course_does_not_affect_the_draw():
    java, other = uuid.uuid4(), uuid.uuid4()
    modules, pool = make_pool(java, [(10, 40)])
    _, other_pool = make_pool(other, [(10, 40)])
    seen_elsewhere = {c.id: NOW for c in other_pool}
    assert draw(java, modules, pool, seen_elsewhere, 30, random.Random(10)) == draw(
        java, modules, pool, {}, 30, random.Random(10)
    )


def test_concept_collisions_are_relaxed_rather_than_refusing_a_learner():
    course = uuid.uuid4()
    module = DrawModule(id=uuid.uuid4(), lesson_count=5)
    pool = [
        DrawCandidate(id=uuid.uuid4(), course_id=course, module_id=module.id, difficulty="easy", concept=f"c{i % 10}")
        for i in range(40)
    ]
    drawn = draw(course, [module], pool, {}, 30, random.Random(11))
    assert len({d.question_id for d in drawn}) == 30


def test_allocate_quotas_never_exceeds_availability():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    quotas = allocate_quotas({a: 10, b: 10, c: 10}, {a: 2, b: 50, c: 50}, 30)
    assert quotas[a] == 2 and sum(quotas.values()) == 30


# --------------------------------------------------------------------------- #
# Grading
# --------------------------------------------------------------------------- #
def make_key(n, module="Basics"):
    return [KeyEntry(question_id=f"q{i}", module_title=module, permutation=(2, 0, 3, 1), correct_index=0) for i in range(n)]


def test_grading_maps_displayed_answers_through_the_permutation():
    key = make_key(2)
    # permutation (2,0,3,1): original option 0 is displayed at index 1
    outcome = grade(key, {"q0": 1, "q1": 0}, 70)
    assert outcome.score == 1
    assert outcome.results[0]["correct"] is True and outcome.results[1]["correct"] is False


def test_unanswered_and_out_of_range_answers_are_wrong():
    outcome = grade(make_key(3), {"q0": 9, "q1": True}, 70)
    assert outcome.score == 0
    assert [r["answered"] for r in outcome.results] == [False, False, False]


def test_pass_boundary_is_exactly_70_percent():
    key = make_key(30)
    answers_21 = {f"q{i}": 1 for i in range(21)}
    answers_20 = {f"q{i}": 1 for i in range(20)}
    assert grade(key, answers_21, 70).passed is True
    assert grade(key, answers_20, 70).passed is False
    assert is_pass(0, 0, 70) is False


def test_percent_rounds_to_an_integer():
    assert percent_of(24, 30) == 80
    assert percent_of(2, 3) == 67
    assert percent_of(0, 0) == 0


def test_module_breakdown_keeps_order_of_first_appearance():
    key = [
        KeyEntry("a", "Loops", (0, 1, 2, 3), 0),
        KeyEntry("b", "Basics", (0, 1, 2, 3), 0),
        KeyEntry("c", "Loops", (0, 1, 2, 3), 0),
    ]
    outcome = grade(key, {"a": 0, "b": 1, "c": 0}, 70)
    assert outcome.module_breakdown == [
        {"module_title": "Loops", "correct": 2, "total": 2},
        {"module_title": "Basics", "correct": 0, "total": 1},
    ]


def test_results_hold_no_question_text_or_key():
    outcome = grade(make_key(1), {"q0": 1}, 70)
    assert set(outcome.results[0]) == {"question_id", "module_title", "answered", "correct"}


def test_regrade_removes_voided_questions_from_the_total():
    results = [{"question_id": f"q{i}", "module_title": "M", "answered": True, "correct": i < 20} for i in range(30)]
    before = regrade(results, set(), 70)
    assert (before.score, before.total, before.passed) == (20, 30, False)
    after = regrade(results, {"q29", "q28"}, 70)  # two wrong questions voided
    assert (after.score, after.total) == (20, 28)
    assert after.passed is True


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def good_draft(**overrides):
    values = dict(
        prompt="What does `javac` produce?",
        options=["Bytecode", "Machine code", "Source code", "Assembly"],
        correct_index=0,
        explanation="javac compiles .java into .class bytecode.",
        difficulty="easy",
        kind="concept",
        concept="javac output",
        lesson_ref="lesson_java_core_compiler",
        source_fact="javac compiles source files into bytecode",
        why_others_wrong=["The JIT makes machine code", "That's the input", "Java doesn't emit assembly"],
    )
    values.update(overrides)
    return DraftQuestion(**values)


def test_a_good_question_passes_the_shape_check():
    assert check_shape(good_draft(), require_rationale=True) == []


@pytest.mark.parametrize(
    "overrides,fragment",
    [
        ({"options": ["a", "b", "c"]}, "exactly 4"),
        ({"options": ["Same", "  Same ", "Other", "Third"]}, "distinct"),
        ({"options": ["One", "Two", "None of the above", "Four"]}, "all/none"),
        ({"options": ["Yes", "No", "Both A and B", "Maybe"]}, "combined"),
        ({"correct_index": 4}, "correct_index"),
        ({"explanation": ""}, "explanation"),
        ({"prompt": "x" * 1501}, "longer than 1500"),
        ({"why_others_wrong": ["one"]}, "why_others_wrong"),
        ({"options": ["A", "B", "C", "A very much longer option than all the others combined"]}, "uneven"),
        ({"prompt": "Click <script>alert(1)</script>"}, "raw HTML"),
        ({"kind": "code_output", "code": None}, "need code"),
    ],
)
def test_shape_rejections(overrides, fragment):
    problems = check_shape(good_draft(**overrides), require_rationale=True)
    assert any(fragment in p for p in problems), problems


def test_code_in_backticks_is_not_raw_html():
    assert has_raw_html("Which type is `List<String>`?") is False
    assert has_raw_html("if one = 2 then") is False
    assert has_raw_html('<img src=x onerror="alert(1)">') is True
    assert has_raw_html("[click](javascript:alert(1))") is True


def test_grounding_by_substring_or_token_overlap():
    passages = ["The javac compiler turns source files into bytecode.", "JVM runs bytecode."]
    assert is_grounded("javac compiler turns source files into bytecode", passages)
    assert is_grounded("the javac compiler turns source into bytecode quickly", passages)
    assert not is_grounded("Python uses indentation for blocks", passages)
    assert not is_grounded("bytecode", passages)  # too short to count


def test_prompt_hash_ignores_formatting_but_not_the_answer():
    a = prompt_hash(normalize_text("What does **javac** produce?"), "Bytecode")
    b = prompt_hash(normalize_text("what does javac produce"), "`bytecode`")
    c = prompt_hash(normalize_text("what does javac produce"), "Machine code")
    assert a == b and a != c


def test_jaccard_threshold_examples():
    assert jaccard(tokens("What does javac produce from source"), tokens("What does javac produce from source code")) >= 0.8
    assert jaccard(tokens("What is a JVM"), tokens("Explain garbage collection")) < 0.2


@pytest.mark.parametrize("raw,expected", [("  Asha   Verma ", "Asha Verma"), ("Zoë O'Brien-Łukasz", "Zoë O'Brien-Łukasz"), ("आशा वर्मा", "आशा वर्मा")])
def test_recipient_names_are_cleaned(raw, expected):
    assert clean_recipient_name(raw) == expected


@pytest.mark.parametrize("raw", ["A", "x" * 81, "Robert'); DROP TABLE--", "<b>Asha</b>", "12345", "...", 42])
def test_bad_recipient_names_are_refused(raw):
    with pytest.raises(ValueError):
        clean_recipient_name(raw)


# --------------------------------------------------------------------------- #
# Duplicates
# --------------------------------------------------------------------------- #
def entry(prompt, lesson, concept="c", embedding=None, answer="A"):
    norm = normalize_text(prompt)
    return PoolEntry(id=uuid.uuid4(), lesson_id=lesson, concept_key=concept, prompt_hash=prompt_hash(norm, answer),
                     tokens=frozenset(norm.split()), embedding=embedding)  # fmt: skip


def test_textual_duplicate_layers():
    lesson = uuid.uuid4()
    pool = [entry("What does javac produce from a source file", lesson)]
    assert textual_duplicate(entry("What does javac produce from a source file", lesson), pool, [], 0.8) == "duplicate_exact"
    assert textual_duplicate(entry("What does javac produce from a source file?", lesson, answer="B"), pool, [], 0.8) == "duplicate_wording"
    public = [frozenset(tokens("Which keyword declares a constant in Java"))]
    assert textual_duplicate(entry("Which keyword declares a constant in Java", lesson), [], public, 0.8) == "duplicate_public_quiz"
    assert textual_duplicate(entry("How is garbage collection triggered", lesson), pool, public, 0.8) is None


def test_concept_limit_per_lesson():
    lesson = uuid.uuid4()
    pool = [entry("q one", lesson, "loops"), entry("q two", lesson, "loops")]
    assert concept_is_full(entry("q three", lesson, "loops"), pool, 2)
    assert not concept_is_full(entry("q three", uuid.uuid4(), "loops"), pool, 2)


def test_semantic_duplicate_thresholds():
    lesson, other = uuid.uuid4(), uuid.uuid4()
    base = (1.0, 0.0, 0.0)
    near = (0.88, 0.47, 0.0)  # cosine ~0.88
    pool = [entry("a", lesson, embedding=base)]
    assert semantic_duplicate(entry("b", lesson, embedding=near), pool, 0.90, 0.85).duplicate is True
    assert semantic_duplicate(entry("b", other, embedding=near), pool, 0.90, 0.85).duplicate is False
    assert round(cosine(base, base), 6) == 1.0


# --------------------------------------------------------------------------- #
# Certificate IDs, pool targets, statistics
# --------------------------------------------------------------------------- #
def test_certificate_id_format_and_alphabet():
    rng = random.Random(1)
    cid = build_certificate_id("JAVA", 2026, rng.choice)
    assert cid.startswith("WNS-JAVA-2026-") and len(cid.split("-")[-1]) == 8
    assert all(ch in ALPHABET for ch in cid.split("-")[-1])
    assert normalize_certificate_id(f"  {cid.lower()} ") == cid
    assert normalize_certificate_id("WNS-JAVA-2026-ILOU0000") is None  # I, L, O, U aren't Crockford
    assert normalize_certificate_id("../../etc/passwd") is None


def test_pool_targets_split_90_by_lessons_with_a_minimum():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    targets = module_targets({a: 20, b: 9, c: 1}, 90, 3)
    assert sum(targets.values()) == 90
    assert targets[c] == 3 and targets[a] > targets[b]
    many = {uuid.uuid4(): 1 for _ in range(40)}
    assert set(module_targets(many, 90, 3).values()) == {3}
    assert pool_is_full({a: 2}, {a: 2}) and not pool_is_full({a: 2}, {a: 1}) and not pool_is_full({}, {})


def test_item_statistics_flag_a_question_strong_learners_miss():
    graded = []
    for i in range(40):
        strong = i < 20
        score = 28 if strong else 12
        graded.append((score, 30, [{"question_id": "bad", "correct": not strong}, {"question_id": "good", "correct": strong}]))
    stats = item_statistics(graded)
    assert stats["bad"]["discrimination"] < 0 < stats["good"]["discrimination"]
    assert stats["bad"]["answered"] == 40


def test_options_differing_only_in_punctuation_are_distinct():
    for options in (['rs.getLong(0)', 'rs.getLong(-1)', 'rs.getLong("0")', 'rs.getLong(1)'],
                    ['.row:focus', '.row:hover', '.row::focus', '.row:focus-within'],
                    ['a == b', 'a === b', 'a = b', 'a != b']):  # fmt: skip
        assert check_shape(good_draft(options=options), require_rationale=False) == []
    assert any("distinct" in p for p in check_shape(good_draft(options=["Same", "Same ", "x y", "z w"]), False))


def test_raw_html_is_turned_into_inline_code():
    from exams.domain.validation import code_wrap_html

    assert code_wrap_html('What must be first? <!DOCTYPE html>') == 'What must be first? `<!DOCTYPE html>`'
    assert code_wrap_html('<p class="note" style="color: red">Hi</p> is styled') == '`<p class="note" style="color: red">`Hi`</p>` is styled'
    assert code_wrap_html("Already `<div>` and\n```html\n<div></div>\n```") == "Already `<div>` and\n```html\n<div></div>\n```"
    assert code_wrap_html("a < b and b > c") == "a < b and b > c"
    assert code_wrap_html('<img src=x onerror="alert(1)">') == '`<img src=x onerror="alert(1)">`'
    assert not has_raw_html(code_wrap_html('<script>alert(1)</script> and <a href="javascript:x">'))


def test_generics_and_comparisons_are_left_alone_by_tag_wrapping():
    from exams.domain.validation import code_wrap_html

    for text in ("Which compiles: Map<String, List<Integer>> m?", "if a<b && c>d then", "x < y and y > z", "List<T> items"):
        assert code_wrap_html(text) == text
    assert code_wrap_html('<p class="note">Hi</p>') == '`<p class="note">`Hi`</p>`'
    assert code_wrap_html("Render <App /> then </Route>") == "Render `<App />` then `</Route>`"
    assert code_wrap_html("<b>hi</b>", whole=True) == "`<b>hi</b>`"
