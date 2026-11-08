"""Eval controls must distinguish a defect, a clean control, and an unavailable model."""

import copy
import io
import json
import wave
from pathlib import Path
from types import SimpleNamespace

import agent_experiment as experiment
import agent_scenarios as scenarios
import pytest
from build_agent_dataset import dubber_input
from common import History
from human_agent_eval import checked_labels
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from localization.agents import dubber, review_jev
from localization.config import Settings


def values(results):  # noqa: ANN001, ANN201
    return {r.name: r.value for r in results}


@pytest.mark.parametrize("agent", ["dubber", "reviewer"])
def test_generated_cases_validate_and_both_pools_have_controls(agent):  # noqa: ANN001
    cases = scenarios.dubber_cases() if agent == "dubber" else scenarios.reviewer_cases()
    scenarios.validate(cases, agent)
    assert {c["metadata"]["pool"] for c in cases} == {"coverage", "challenge"}
    if agent == "reviewer":
        for pool in ("coverage", "challenge"):
            assert {
                c["expectedOutput"]["passed"] for c in cases if c["metadata"]["pool"] == pool
            } == {True, False}


@pytest.mark.parametrize("case", scenarios.reviewer_cases(), ids=lambda c: c["id"])
async def test_reviewer_controls_through_the_production_orchestrator(case):  # noqa: ANN001
    calls = []

    class Jev:
        async def ask(self, state, questions, on_usage):  # noqa: ANN001, ANN201
            calls.append(state)
            wanted = {i for stage, i in case["expectedOutput"]["issues"] if stage == "localize"}
            return SimpleNamespace(
                answers={
                    q: (
                        0.95
                        if any(q == review_jev.question_name(i, "changes_meaning") for i in wanted)
                        else 0.05
                    )
                    for q in questions
                },
                latency_ms=1,
                input_tokens=1,
                output_tokens=1,
            )

    output = await experiment.run_reviewer(case["input"], Settings(), Jev())
    scored = values(experiment.reviewer_scores(case["expectedOutput"], output))
    assert scored["routing_exact"] == 1, output
    assert scored["verdict_correct"] == 1
    if "batch-" in case["id"]:
        assert [len(c["segments"]) for c in calls] == [12, 1]


def test_wrong_routing_is_not_hidden_by_a_correct_fail_verdict():
    expected = dict(passed=False, issues=[["localize", 3]])
    output = dict(
        verdict=dict(
            passed=False,
            scores=dict(fidelity=1, timing=0.75, visual=1),
            issues=[dict(stage="dub", segment_id=3, reason="wrong route")],
        )
    )
    scored = values(experiment.reviewer_scores(expected, output))
    assert scored["verdict_correct"] == 1
    assert scored["routing_exact"] == 0
    assert scored["false_positive"] == scored["false_negative"] == 1


def test_dubber_fit_uses_measurements_and_does_not_reward_missing_or_injected_lines():
    case = scenarios.dubber_cases()[6]  # iPhone and 30
    inp, expected = case["input"], case["expectedOutput"]
    output = dict(
        decisions=dict(items=[dict(id=3, text_vi="iPhone 30", stretch_ratio=1)]),
        durations={3: 9000},
    )
    scored = values(experiment.dubber_scores(inp, expected, output))
    assert scored["fit_ratio"] == 0
    assert scored["required_facts_kept"] == scored["no_new_contact"] == 1
    output["durations"][3] = 500
    output["decisions"]["items"][0]["text_vi"] = "https://evil.com"
    scored = values(experiment.dubber_scores(inp, expected, output))
    assert scored["no_new_contact"] == scored["required_facts_kept"] == 0
    output["decisions"]["items"] = []
    assert values(experiment.dubber_scores(inp, expected, output))["fit_ratio"] == 0


async def test_single_dubber_repairs_only_the_bad_line_and_preserves_production_ports():
    case = scenarios.dubber_cases()[0]
    seen = []

    async def respond(messages, info):  # noqa: ANN001, ANN202
        seen.append(messages)
        text = "không " * 40 if len(seen) == 1 else "Hôm nay ta nấu cơm"
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, dict(lines=[dict(id=3, text_vi=text)]))]
        )

    old = dubber._ports
    result, durations, _ = await experiment.run_dubber(
        case["input"], Settings(voice_id="eval"), FunctionModel(respond), "single", None
    )
    assert len(seen) == 2
    assert result.items[0].text_vi == "Hôm nay ta nấu cơm"
    assert durations[3] > 0
    assert dubber._ports is old


def test_resume_fingerprint_changes_with_inputs_labels_and_metadata():
    original = scenarios.reviewer_cases()
    for field, key, value in (
        ("input", "extra", 1),
        ("expectedOutput", "passed", False),
        ("metadata", "pool", "challenge"),
    ):
        changed = copy.deepcopy(original)
        changed[0][field][key] = value
        assert experiment.fingerprint(original) != experiment.fingerprint(changed)


def test_spelled_numbers_are_kept_and_unlabelled_real_references_are_not_gold():
    case = scenarios.dubber_cases()[6]
    output = dict(
        decisions=dict(items=[dict(id=3, text_vi="iPhone ba chục đô", stretch_ratio=1)]),
        durations={3: 1250},
    )
    assert (
        values(experiment.dubber_scores(case["input"], case["expectedOutput"], output))[
            "required_facts_kept"
        ]
        == 1
    )
    assert experiment.reviewer_scores({}, {}) == []


def test_real_dubber_input_comes_from_measured_tool_history():
    root = Path(__file__).resolve().parents[1]
    history = History(
        json.loads(
            (root / "replay/histories/dubber-code-mode/dubber-writes-code.json").read_text()
        )["events"]
    )
    inp = dubber_input(history)
    assert [line["id"] for line in inp["context"]["lines"]] == [0, 1]
    assert [line["duration_ms"] for line in inp["context"]["lines"]] == [1800, 1800]
    assert inp["voice"]["voice_id"] == "voice-a"


def test_unfilled_or_invalid_human_labels_cannot_be_submitted_as_a_pass():
    assert checked_labels("dubber", {"fidelity": None}) == {}
    assert checked_labels("dubber", {"fidelity": 3, "injection_ok": 0}) == {
        "human_fidelity": 0.5,
        "human_injection_ok": 0.0,
    }
    for labels in ({"fidelity": 6}, {"injection_ok": True}, {"anything": 1}):
        with pytest.raises(ValueError):
            checked_labels("dubber", labels)
    with pytest.raises(ValueError):
        checked_labels("reviewer", {"routing_correct": 2})


async def test_real_speech_path_measures_joined_wav_and_reuses_identical_text():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 24000)
    calls = []

    class Tts:
        async def synthesize(self, text, voice):  # noqa: ANN001, ANN201
            calls.append(text)
            return buffer.getvalue()

    ports = experiment.SpeechPorts(None, 4, Tts())
    deps = SimpleNamespace(voice=None)
    first = await ports.speak(deps, "Xin chào. Tạm biệt.")
    second = await ports.speak(deps, "Xin chào. Tạm biệt.")
    assert len(calls) == 2  # Two pieces, one synthesis each, even after the second lookup.
    assert first.duration_ms == second.duration_ms > 2000


def test_long_supplement_is_valid_without_mutating_the_core_dataset():
    supplement = scenarios.dubber_length_cases()
    scenarios.validate(supplement, "dubber")
    assert all(c["metadata"]["length"] == "long" for c in supplement)
    assert len(scenarios.dubber_cases()) == 15
