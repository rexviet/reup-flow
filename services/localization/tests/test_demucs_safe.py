"""`demucs_safe` forces `weights_only=True` on every `torch.load` demucs makes.

torch and demucs are not installed next to the tests (they live only in the
image), so they are replaced with recorders. What is under test is the wrapper:
that a load which asks for `weights_only=False`, or says nothing, is turned into
`weights_only=True`, and that only the globals the pinned weights hold are allowed.
"""

import sys
import types

import pytest

from localization.providers import demucs_safe


@pytest.fixture
def fake_stack(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {"loads": [], "allowed": []}

    torch = types.ModuleType("torch")
    torch.serialization = types.SimpleNamespace(
        add_safe_globals=lambda items: seen["allowed"].extend(items)
    )
    torch.load = lambda *a, **k: seen["loads"].append(k) or "weights"

    class HTDemucs: ...

    class Float64DType: ...

    class Dtype: ...

    def scalar(*args: object) -> tuple:
        return args

    numpy = types.ModuleType("numpy")
    numpy.dtype = Dtype
    numpy.dtypes = types.SimpleNamespace(Float64DType=Float64DType)
    numpy._core = types.SimpleNamespace(multiarray=types.SimpleNamespace(scalar=scalar))

    htdemucs = types.ModuleType("demucs.htdemucs")
    htdemucs.HTDemucs = HTDemucs

    separate = types.ModuleType("demucs.separate")

    def demucs_main() -> None:
        import torch as t

        t.load("a.th", map_location="cpu")
        t.load("b.th", weights_only=False)

    separate.main = demucs_main
    demucs = types.ModuleType("demucs")
    for name, module in {
        "torch": torch,
        "numpy": numpy,
        "demucs": demucs,
        "demucs.htdemucs": htdemucs,
        "demucs.separate": separate,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return seen


def test_every_load_is_weights_only_whatever_the_caller_asked(fake_stack: dict) -> None:
    demucs_safe.main()
    assert [k["weights_only"] for k in fake_stack["loads"]] == [True, True]
    assert fake_stack["loads"][0]["map_location"] == "cpu"


def test_only_the_globals_the_weights_contain_are_allowed(fake_stack: dict) -> None:
    demucs_safe.main()
    names = sorted(f"{g.__module__}.{g.__name__}" for g in fake_stack["allowed"])
    assert names == [
        "_codecs.encode",
        "fractions.Fraction",
        "numpy.core.multiarray.scalar",
        "tests.test_demucs_safe.Dtype",
        "tests.test_demucs_safe.Float64DType",
        "tests.test_demucs_safe.HTDemucs",
    ]
