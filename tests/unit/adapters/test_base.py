"""Unit tests for agentforge.adapters.base — Channel/LLM ABCs.

These tests verify the abstract contracts; concrete adapters are tested
in their own modules. We use throwaway subclasses to exercise the
abstract methods.
"""

from __future__ import annotations

import inspect
from typing import AsyncIterator, ClassVar, List

import pytest

from agentforge.adapters.base import BaseChannelAdapter, BaseLLMAdapter
from agentforge.core.message import Message


# ---------------------------------------------------------------------------
# BaseChannelAdapter — abstract contract
# ---------------------------------------------------------------------------

def test_base_channel_adapter_cannot_be_instantiated_directly():
    with pytest.raises(TypeError, match="abstract"):
        BaseChannelAdapter()  # type: ignore[abstract]


def test_subclass_missing_methods_cannot_be_instantiated():
    class IncompleteChannel(BaseChannelAdapter):
        name = "incomplete"

        async def send(self, message: Message) -> None:
            pass
        # receive, start, stop missing

    with pytest.raises(TypeError, match="abstract"):
        IncompleteChannel()  # type: ignore[abstract]


def test_complete_channel_subclass_can_be_instantiated():
    class GoodChannel(BaseChannelAdapter):
        name: ClassVar[str] = "good"

        async def send(self, message: Message) -> None:
            pass

        async def receive(self) -> AsyncIterator[Message]:
            if False:
                yield  # pragma: no cover

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    ch = GoodChannel()
    assert ch.name == "good"


def test_base_channel_adapter_io_methods_are_async():
    """All I/O methods must be async — sync I/O in adapters would block
    the event loop and break the multi-agent concurrency model.

    `send`/`start`/`stop` are coroutines. `receive` is an async generator
    (returns AsyncIterator[Message]). We verify both via the function shape
    and the type annotation.
    """
    for method in ("send", "start", "stop"):
        m = getattr(BaseChannelAdapter, method)
        assert inspect.iscoroutinefunction(m), f"{method} should be coroutine"

    receive = BaseChannelAdapter.receive
    # It's either an async generator (preferred) or a coroutine returning
    # AsyncIterator[Message]. The contract is "yields Message objects
    # without blocking" — both shapes satisfy that, async generator is
    # more idiomatic.
    sig = inspect.signature(receive)
    annotation_str = str(sig.return_annotation)
    assert "AsyncIterator" in annotation_str, (
        f"receive should return AsyncIterator, got {annotation_str}"
    )
    # And the function body must contain a yield (async generator) or
    # raise NotImplementedError (concrete subclasses override with yield).
    try:
        source = inspect.getsource(receive).strip()
        assert "yield" in source or "NotImplementedError" in source or "..." in source
    except (OSError, TypeError):
        pass  # getsource can fail on some decorated methods; not a hard fail


# ---------------------------------------------------------------------------
# BaseLLMAdapter — abstract contract
# ---------------------------------------------------------------------------

def test_base_llm_adapter_cannot_be_instantiated_directly():
    with pytest.raises(TypeError, match="abstract"):
        BaseLLMAdapter()  # type: ignore[abstract]


def test_llm_subclass_must_implement_chat():
    class BadLLM(BaseLLMAdapter):
        pass  # chat() missing

    with pytest.raises(TypeError, match="abstract"):
        BadLLM()  # type: ignore[abstract]


def test_llm_subclass_with_chat_works():
    class FakeLLM(BaseLLMAdapter):
        async def chat(self, system: str, user: str) -> str:
            return f"echo: {user}"

    llm = FakeLLM()
    # chat is async
    import asyncio
    out = asyncio.run(llm.chat("sys", "hello"))
    assert out == "echo: hello"
