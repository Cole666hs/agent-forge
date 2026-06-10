"""T3 RED — context.py test."""

from agentforge.observability.context import set_request_id, get_request_id, reset_request_id


def test_request_id_roundtrip():
    assert get_request_id() is None
    set_request_id("req_abc")
    assert get_request_id() == "req_abc"
    reset_request_id()
    assert get_request_id() is None


def test_request_id_isolated_between_contexts():
    """Setting a request_id in one context doesn't leak to another."""
    import asyncio
    results = {}

    async def task(name, rid):
        set_request_id(rid)
        await asyncio.sleep(0)  # yield to event loop
        results[name] = get_request_id()

    async def main():
        await asyncio.gather(
            task("a", "req_a"),
            task("b", "req_b"),
        )

    asyncio.run(main())
    assert results["a"] == "req_a"
    assert results["b"] == "req_b"
