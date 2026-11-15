import pytest
from tests.review_bridge.test_service import command

from localization.review_bridge.repository import Conflict


@pytest.mark.parametrize("mutate", [False, True])
def test_interrupted_local_update_recovers_or_preserves_concurrent_studio_edit(
    companion, monkeypatch, mutate
):
    import localization.review_bridge.repository as module

    original = module.atomic_write
    armed = [False]

    def crash(path, data):
        original(path, data)
        if path.name == "index.html" and not armed[0]:
            armed[0] = True
            raise RuntimeError("interrupted local update")

    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_write", crash)
        with pytest.raises(RuntimeError, match="interrupted"):
            companion.save("7001", 1, 17, command(companion, text="Caption durable"))
    folder = companion.repo.folder("7001", 1)
    if mutate:
        (folder / "index.html").write_text("Unsaved edit after crash")
        with pytest.raises(Conflict, match="interrupted"):
            companion.repo.materialize("7001", 1)
        assert (folder / "index.html").read_text() == "Unsaved edit after crash"
    else:
        companion.repo.materialize("7001", 1)
        assert "Caption durable" in (folder / "index.html").read_text()
        assert not (folder / ".projection-pending.json").exists()
