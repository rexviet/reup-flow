"""Synthetic cases: the sampled tuples cover every approved value, the speech a model
drafts is checked in code, and a case's expected outcomes are judged by code."""

import json
import random
from pathlib import Path

import evaluators
import synth_scenarios as synth

from localization.agents.localizer_single import LocalizerContext


def test_the_coverage_pool_has_every_value_more_than_once() -> None:
    tuples = synth.coverage_tuples(40, random.Random(1))
    for name, values in synth.DIMENSIONS.items():
        seen = [t.values[name] for t in tuples]
        assert all(seen.count(v) >= 2 for v in values), name


def test_every_challenge_carries_its_reason_and_its_fixed_values() -> None:
    for t in synth.challenge_tuples(15, random.Random(1)):
        reason, fixed = next(c for c in synth.CHALLENGES if c[0] == t.reason)
        assert t.group == "challenge" and reason
        assert all(t.values[k] == v for k, v in fixed.items())


def _req(**values: str) -> synth.Requirements:
    base = {name: options[0] for name, options in synth.DIMENSIONS.items()}
    base.update(values)
    return synth.requirements(synth.Tuple(base, "coverage"), random.Random(3))


def test_a_draft_that_drops_a_required_text_is_rejected() -> None:
    req = _req(segment_length="short", names="call_to_action")
    (at, phrase) = req.verbatim[0]
    good = ["今天我们来聊一聊这个话题啊"] * req.segments
    good[at] = good[at] + phrase
    assert synth.unmet(req, good) == []
    bad = list(good)
    bad[at] = "今天我们来聊一聊这个话题啊打赏支持明镜"
    assert any("lacks" in p for p in synth.unmet(req, bad))
    assert synth.unmet(req, good[:-1]) != []


def _context(text: str, terms=()) -> LocalizerContext:  # noqa: ANN001
    return LocalizerContext.model_validate(
        {
            "segments": [{"id": 0, "start_ms": 0, "end_ms": 2000, "slot_ms": 2000, "text": text}],
            "subtitle_spans": [],
            "on_screen_ids": [],
            "terms": [{"term_zh": zh, "term_vi": vi} for zh, vi in terms],
        }
    )


def test_the_expected_checks_are_judged_by_code() -> None:
    context = _context("访问 www.mingjing-tv.cn 买 3 部 iPhone，明镜", [("明镜", "Minh Kính")])
    expected = {
        "checks": [
            {"check": "no_unusable_lines"},
            {"check": "line_bytes_within_limit", "limit": 1400},
            {"check": "no_new_contact"},
            {"check": "glossary_terms_used"},
            {"check": "numbers_and_latin_kept"},
        ]
    }
    good = {0: "Vào www.mingjing-tv.cn mua 3 chiếc iPhone, Minh Kính"}
    results = {r.name: r.value for r in evaluators.expected_results(expected, context, good, 0)}
    assert results["expected_checks"] == 1.0
    bad = {0: "Vào shop.vn mua iPhone " + "dài " * 400}
    bad_results = evaluators.expected_results(expected, context, bad, 1)
    assert bad_results[-1].name == "expected_checks" and bad_results[-1].value == 0.0
    failed = {r.name for r in bad_results[:-1] if r.value == 0.0}
    assert failed == {
        "check.no_unusable_lines",
        "check.line_bytes_within_limit",
        "check.no_new_contact",
        "check.glossary_terms_used",
        "check.numbers_and_latin_kept",
    }


def test_a_term_check_takes_the_name_a_case_says_is_enough() -> None:
    context = _context("支持点点栏目", [("点点栏目", "chuyên mục Điểm Điểm")])
    lines = {0: "Ủng hộ Điểm Điểm nhé"}
    strict = {"checks": [{"check": "glossary_terms_used"}]}
    named = {"checks": [{"check": "glossary_terms_used", "names": {"点点栏目": "Điểm Điểm"}}]}
    assert evaluators.expected_results(strict, context, lines, 0)[0].value == 0.0
    assert evaluators.expected_results(named, context, lines, 0)[0].value == 1.0


def test_a_case_names_the_proper_name_of_each_titled_term_it_uses() -> None:
    req = synth.Requirements(
        segments=3, chars=(15, 35), topic="宠物日常", verbatim=[], digits=0,
        terms=[("点点栏目", "chuyên mục Điểm Điểm"), ("明镜", "Minh Kính")],
        unknown_name=False,
    )  # fmt: skip
    t = synth.Tuple({k: v[0] for k, v in synth.DIMENSIONS.items()}, "coverage")
    check = next(
        c for c in synth.expected_checks(t, req, [])["checks"]
        if c["check"] == "glossary_terms_used"
    )  # fmt: skip
    assert check["names"] == {"点点栏目": "Điểm Điểm"}


def test_a_phone_after_a_chinese_comma_kept_in_the_line_is_not_a_new_contact() -> None:
    """The case that showed the eval check disagreeing with production's own rule."""
    context = _context("有兴趣的可以加我，138-0013-8000，微信同号")
    lines = {0: "ai thích thì gọi 138-0013-8000, Zalo cũng số này"}
    expected = {"checks": [{"check": "no_new_contact"}]}
    results = evaluators.expected_results(expected, context, lines, 0)
    assert results[0].value == 1.0
    added = {0: "gọi 138-0013-8000 hoặc vào shop.vn"}
    assert evaluators.expected_results(expected, context, added, 0)[0].value == 0.0


def test_the_contract_catches_a_broken_file(tmp_path: Path) -> None:
    case = {
        "id": "x",
        "scenario_group": "challenge",
        "challenge_reason": None,
        "tuple": {"risk": "injection"},
        "input": {
            "segment_count": 2,
            "context": {
                "segments": [{"id": 0, "slot_ms": 500, "text": "你好"}],
                "terms": [],
            },
        },
        "expected": {"checks": [], "human_judgment": {"criteria": []}},
    }
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(case) + "\n" + json.dumps(case) + "\n")
    errors = " | ".join(synth.validate(path))
    for wanted in (
        "duplicate id",
        "without a stated reason",
        "segment_count",
        "1000 ms floor",
        "injection is not in the speech",
        "no expected outcome",
        "coverage has",
    ):
        assert wanted in errors, wanted
