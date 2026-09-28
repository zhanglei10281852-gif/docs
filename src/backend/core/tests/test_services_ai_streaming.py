# pylint: disable=protected-access
"""Tests for the cancellation-aware AI stream bridges.

These tests cover the contract that matters for the "Stop" feature:

* a client disconnect / iterator close propagates to the upstream AI stream
  promptly, both in WSGI (sync bridge, dedicated thread + event loop) and in
  ASGI (async wrapper, task cancellation);
* chunks and errors arriving after the cancellation are discarded;
* genuine upstream errors keep their original semantics;
* resources are released exactly once and a subsequent stream is not
  affected by leftover threads, queues or event loops.
"""

import asyncio
import contextlib
import socket
import ssl
import threading

import pytest

from core.services.ai_services import streaming
from core.services.ai_services.streaming import (
    _END,
    _ERROR,
    _SyncStreamBridge,
    build_disconnect_checker,
    cancelable_async_stream,
    cancelable_sync_stream,
)

# Tests must not wait the production poll interval (0.5s).
FAST_POLL_INTERVAL = 0.01


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    """Speed up the disconnect poll loop in the sync bridge."""
    monkeypatch.setattr(streaming, "DISCONNECT_POLL_INTERVAL", FAST_POLL_INTERVAL)
    monkeypatch.setattr(streaming, "WORKER_JOIN_TIMEOUT", 5.0)


@pytest.fixture
def fake_select_always_readable(monkeypatch):
    """Make ``select`` report the given sockets as immediately readable."""

    def fake_select(read, _write, _error, _timeout):
        return list(read), [], []

    monkeypatch.setattr(streaming.select, "select", fake_select)


class FakeSocket:
    """Minimal stand-in for the ``gunicorn.socket`` environ entry."""

    def __init__(self, recv_result):
        self._recv_result = recv_result

    def fileno(self):
        return 1

    def recv(self, _size, _flags=0):
        result = self._recv_result
        if isinstance(result, BaseException):
            raise result
        return result


def _stream_threads_alive():
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == "ai-upstream-stream" and thread.is_alive()
    ]


# -- build_disconnect_checker ----------------------------------------------


def test_disconnect_checker_unavailable_without_socket():
    """No checker is built when the server doesn't expose the client socket."""
    assert build_disconnect_checker(None) is None
    assert build_disconnect_checker({}) is None
    assert build_disconnect_checker({"other.key": object()}) is None


@pytest.mark.usefixtures("fake_select_always_readable")
def test_disconnect_checker_detects_fin():
    """An empty peek read is the client closing the connection (FIN)."""
    checker = build_disconnect_checker({"gunicorn.socket": FakeSocket(b"")})
    assert checker is not None
    assert checker() is True


@pytest.mark.usefixtures("fake_select_always_readable")
def test_disconnect_checker_detects_rst():
    """An OSError while peeking is a reset/dead connection."""
    checker = build_disconnect_checker(
        {"gunicorn.socket": FakeSocket(ConnectionResetError())}
    )
    assert checker is not None
    assert checker() is True


@pytest.mark.usefixtures("fake_select_always_readable")
def test_disconnect_checker_ssl_want_read_is_alive():
    """TLS renegotiation-like transient states must not look like a close."""
    checker = build_disconnect_checker(
        {"gunicorn.socket": FakeSocket(ssl.SSLWantReadError())}
    )
    assert checker is not None
    assert checker() is False


def test_disconnect_checker_no_data_is_alive(monkeypatch):
    """When select reports nothing readable, the client is still connected."""
    monkeypatch.setattr(streaming.select, "select", lambda _r, _w, _e, _t: ([], [], []))
    checker = build_disconnect_checker({"gunicorn.socket": FakeSocket(b"")})
    assert checker is not None
    assert checker() is False


# -- cancelable_sync_stream (WSGI) -----------------------------------------


def test_sync_stream_forwards_chunks():
    async def upstream():
        for item in ["a", "b", "c"]:
            yield item

    assert list(cancelable_sync_stream(upstream(), {})) == ["a", "b", "c"]
    assert _stream_threads_alive() == []


def test_sync_stream_empty():
    async def upstream():
        return
        yield

    assert list(cancelable_sync_stream(upstream(), {})) == []
    assert _stream_threads_alive() == []


def test_sync_stream_propagates_upstream_error():
    async def upstream():
        yield "first"
        raise ValueError("boom")

    sync_iter = iter(cancelable_sync_stream(upstream(), {}))
    assert next(sync_iter) == "first"
    with pytest.raises(ValueError, match="boom"):
        next(sync_iter)
    assert _stream_threads_alive() == []


def test_sync_stream_close_after_chunk_stops_upstream():
    """Closing the iterator after a failed write must cancel a stream that
    is still waiting for the next chunk (e.g. while the model thinks)."""
    upstream_closed = threading.Event()
    first_chunk_consumed = threading.Event()

    async def upstream():
        try:
            yield "chunk1"
            first_chunk_consumed.set()
            await asyncio.sleep(30)
            yield "late"
        finally:
            upstream_closed.set()

    sync_iter = iter(cancelable_sync_stream(upstream(), {}))
    assert next(sync_iter) == "chunk1"
    assert first_chunk_consumed.wait(5)

    # Simulates the WSGI server closing the response iterator when writing
    # the next chunk fails because the client went away.
    sync_iter.close()

    assert upstream_closed.wait(5), "upstream AI stream was not closed"
    assert _stream_threads_alive() == []


def test_sync_stream_idle_client_disconnect_is_detected(
    fake_select_always_readable,
):
    """A closed client socket must interrupt an idle stream without waiting
    for the next upstream chunk."""
    upstream_closed = threading.Event()

    async def upstream():
        try:
            await asyncio.sleep(30)
            yield "late"
        finally:
            upstream_closed.set()

    meta = {"gunicorn.socket": FakeSocket(b"")}
    sync_iter = iter(
        cancelable_sync_stream(upstream(), meta),
    )

    assert list(sync_iter) == []
    assert upstream_closed.wait(5), "idle stream kept running after disconnect"
    assert _stream_threads_alive() == []


def test_sync_stream_late_chunks_discarded_with_fast_producer():
    """A fast upstream must not pin the worker: bounded queue + cancellation
    must stop the producer even if it keeps generating after the close."""
    upstream_closed = threading.Event()

    async def upstream():
        try:
            index = 0
            while True:
                yield f"chunk-{index}"
                index += 1
                await asyncio.sleep(0)
        finally:
            upstream_closed.set()

    sync_iter = iter(cancelable_sync_stream(upstream(), {}))
    assert next(sync_iter) == "chunk-0"

    # Let the producer fill the bounded queue (it then blocks on enqueue).
    assert _stream_threads_alive() != []
    sync_iter.close()

    assert upstream_closed.wait(5), "fast producer was not stopped"
    assert _stream_threads_alive() == []


def test_sync_stream_late_error_dropped_after_cancellation():
    """An error reaching the queue once the consumer is gone must not be
    resurrected; cancellation wins the race."""

    async def upstream():
        yield "chunk1"

    bridge = _SyncStreamBridge(upstream(), None)
    sync_iter = iter(bridge)
    assert next(sync_iter) == "chunk1"

    bridge.release()
    # A late upstream error (or a late chunk / sentinel) is enqueued while
    # or right after the release: the consumer must simply stop.
    bridge._queue.put((_ERROR, RuntimeError("late boom")))
    bridge._queue.put("late chunk")
    bridge._queue.put(_END)
    assert list(sync_iter) == []

    # Idempotent release: safe to call again (resources released once).
    bridge.release()
    assert _stream_threads_alive() == []


def test_sync_stream_release_idempotent_on_natural_end():
    async def upstream():
        yield "only"

    bridge = _SyncStreamBridge(upstream(), None)
    assert list(iter(bridge)) == ["only"]
    bridge.release()
    bridge.release()
    assert _stream_threads_alive() == []


def test_sync_stream_worker_thread_is_daemon():
    """The background thread must never hold the process hostage."""

    async def upstream():
        yield "x"

    bridge = _SyncStreamBridge(upstream(), None)
    sync_iter = iter(bridge)
    assert next(sync_iter) == "x"
    assert bridge._thread.daemon is True
    list(sync_iter)


def test_sync_stream_followup_stream_unaffected(fake_select_always_readable):
    """A generation started after a cancellation must work independently,
    with no residual thread / queue / loop state leaking into it."""

    async def slow_then_late():
        try:
            await asyncio.sleep(30)
            yield "late"
        finally:
            pass

    # First generation: client goes away while the model is still thinking.
    canceled = iter(
        cancelable_sync_stream(slow_then_late(), {"gunicorn.socket": FakeSocket(b"")})
    )
    assert list(canceled) == []
    assert _stream_threads_alive() == []

    # A brand new generation right after must behave normally.
    async def upstream():
        yield "fresh1"
        yield "fresh2"

    assert list(cancelable_sync_stream(upstream(), {})) == ["fresh1", "fresh2"]
    assert _stream_threads_alive() == []


# -- cancelable_async_stream (ASGI) ----------------------------------------


def test_async_stream_forwards_chunks_and_closes_upstream():
    upstream_closed = threading.Event()

    async def upstream():
        try:
            for item in ["a", "b"]:
                yield item
        finally:
            upstream_closed.set()

    async def scenario():
        wrapped = cancelable_async_stream(upstream())
        assert await wrapped.__anext__() == "a"
        assert await wrapped.__anext__() == "b"
        with pytest.raises(StopAsyncIteration):
            await wrapped.__anext__()

    asyncio.run(scenario())
    assert upstream_closed.is_set()


def test_async_stream_task_cancellation_stops_idle_upstream():
    """Django cancels the streaming task on http.disconnect; the upstream
    model connection must be closed by the wrapper."""
    upstream_closed = threading.Event()

    async def upstream():
        try:
            await asyncio.sleep(30)
            yield "never"
        finally:
            upstream_closed.set()

    async def scenario():
        wrapped = cancelable_async_stream(upstream())

        async def consume():
            return await wrapped.__anext__()

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        # Give the finalizers a chance to schedule on the loop.
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert upstream_closed.wait(2), "upstream stream survived task cancellation"


def test_async_stream_task_cancellation_between_chunks():
    """Cancellation while the wrapper is suspended waiting for the next
    chunk must close the upstream even after some chunks were delivered."""
    upstream_closed = threading.Event()

    async def upstream():
        try:
            yield "first"
            await asyncio.sleep(30)
            yield "second"
        finally:
            upstream_closed.set()

    async def scenario():
        wrapped = cancelable_async_stream(upstream())
        received = []

        async def consume():
            async for chunk in wrapped:
                received.append(chunk)

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        await asyncio.sleep(0.05)
        return received

    received = asyncio.run(scenario())
    assert received == ["first"]
    assert upstream_closed.wait(2)


def test_async_stream_propagates_upstream_error():
    """Real upstream errors keep their original semantics."""

    async def upstream():
        yield "ok"
        raise ValueError("real failure")

    async def scenario():
        wrapped = cancelable_async_stream(upstream())
        assert await wrapped.__anext__() == "ok"
        with pytest.raises(ValueError, match="real failure"):
            await wrapped.__anext__()

    asyncio.run(scenario())


def test_async_stream_explicit_aclose_after_consumption():
    """aclosing() (as Django uses it) after partial consumption closes the
    upstream generator."""
    upstream_closed = threading.Event()

    async def upstream():
        try:
            yield "a"
            yield "b"
        finally:
            upstream_closed.set()

    async def scenario():
        wrapped = cancelable_async_stream(upstream())
        assert await wrapped.__anext__() == "a"
        await wrapped.aclose()
        # Subsequent iterations must not resurrect the stream.
        with pytest.raises(StopAsyncIteration):
            await wrapped.__anext__()

    asyncio.run(scenario())
    assert upstream_closed.is_set()


# -- misc ------------------------------------------------------------------


def test_msg_peek_constant_available():
    """The disconnect checker relies on MSG_PEEK; guard platform support."""
    assert hasattr(socket, "MSG_PEEK")
