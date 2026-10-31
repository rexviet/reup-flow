"""pydantic-ai's instrumentation, as every agent of this service uses it.

On (the default, `TRACE_AGENT_CONTENT=true`), an agent's spans carry what it says and
does: the prompt and the answer of each model request, the code the Dubber writes, the
arguments and the result of each tool call, the final output. That includes text read
from the video, which is untrusted and is recorded as the model saw it. Off, they keep
the shape of the run (which tool was called when, the tools' definitions), timings, token
counts and costs, and none of that text; an error keeps its type, not its message.
Binary content (the Reviewer's frames, a clip) is never recorded.
"""

from typing import Any

from opentelemetry import trace
from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings

# Whether agent spans carry content. Read by the Reviewer, which is built per review and
# has its own instrumentation; set once by `instrument_agents` when the worker starts.
_record_content = True


def agent_instrumentation(
    tracer_provider: Any = None, *, include_content: bool | None = None
) -> InstrumentationSettings:
    """pydantic-ai's instrumentation as every agent here uses it. Never binary content:
    a frame or a clip would make a span the size of the media, base64 encoded."""
    return InstrumentationSettings(
        tracer_provider=tracer_provider,
        include_binary_content=False,
        include_content=_record_content if include_content is None else include_content,
    )


def instrument_agents(provider: trace.TracerProvider, *, include_content: bool) -> None:
    """Turns on pydantic-ai's instrumentation for every agent of this process: the agent
    run, each model request and each tool call (the ones code makes too) become spans."""
    global _record_content
    _record_content = include_content
    Agent.instrument_all(agent_instrumentation(provider, include_content=include_content))
