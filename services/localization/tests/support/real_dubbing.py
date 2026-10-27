"""The real dubbing activities, wired to a store and a provider the test chooses, for
the tests that need audio and a cache rather than stand-ins."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from localization.activities.dub_script import DubScriptActivities
from localization.activities.dubber_ports import StoreDubberPorts
from localization.activities.synthesize import BeforePut, SynthesizeActivities
from localization.agents import dubber as dubber_module
from localization.agents.temporal import DUBBER_MODEL_ALIAS, bind_model_factory
from localization.storage.object_store import ObjectStore
from tests.support.dub_fakes import DubFakes, ScriptedDubber, by_name


class RealDubbing:
    """Everything of the stage that touches the cache is real. The final track is a
    stand-in: building it needs a music stem and a video length these tests do not
    model (the activity has its own tests)."""

    # How the Dubber answers: the tool loop, on its scripted model.
    mode = "tools"

    def __init__(
        self,
        store: ObjectStore,
        tts,
        *,
        model: str = "tts-1",
        engine: str | None = "engine-a",
        before_put: BeforePut | None = None,
    ) -> None:
        self.synthesis = SynthesizeActivities(
            store, tts, model=model, engine=lambda: engine, before_put=before_put
        )
        self.dub = DubScriptActivities(store, self.synthesis.synthesizer)
        self.store = store
        self.dubber = ScriptedDubber()
        self._track = by_name(DubFakes().activities())["assemble_audio"]

    def activities(self) -> list[Callable]:
        s, d = self.synthesis, self.dub
        return [
            s.voice_profile,
            s.plan_synthesis,
            s.synthesize_segments,
            d.measure_fit,
            d.write_dubbed_script,
            d.commit_dub,
            self._track,
        ]

    @contextmanager
    def bound(self) -> Iterator[None]:
        dubber_module.bind_ports(StoreDubberPorts(self.store, self.synthesis.synthesizer))
        model = self.dubber.model()
        bind_model_factory(lambda: model, DUBBER_MODEL_ALIAS)
        try:
            yield
        finally:
            dubber_module.bind_ports(None)
            bind_model_factory(None, DUBBER_MODEL_ALIAS)
