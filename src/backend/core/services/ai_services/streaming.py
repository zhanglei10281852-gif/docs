"""Cancellation-aware bridges between the upstream async AI stream and the
``StreamingHttpResponse`` consumed by Django.

Two runtimes are supported:

* ASGI (``PYTHON_SERVER_MODE=async``): Django cancels the streaming task on
  ``http.disconnect``. :func:`cancelable_async_stream` makes sure the
  cancellation reaches the upstream model connection.
* WSGI (``PYTHON_SERVER_MODE=sync``): :func:`cancelable_sync_stream` runs the
  async upstream in a dedicated thread (with its own event loop) and forwards
  chunks through a bounded queue. Client disconnects are detected when the
  WSGI server closes the iterator (a write failed) and, on gunicorn, by
  polling the raw client socket so an idle "thinking" stream is interrupted
  too.

In both modes the cleanup is idempotent (resources are released exactly
once), chunks or errors arriving after the cancellation are discarded
instead of being raised, while genuine upstream errors keep propagating with
their original semantics.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import select
import socket
import ssl
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from typing import Any

log = logging.getLogger(__name__)

#: Wake-up interval of a WSGI worker blocked while waiting for the next
#: upstream chunk. Every wake-up is an opportunity to notice that the client
#: went away, even while the model is still "thinking".
DISCONNECT_POLL_INTERVAL = 0.5
#: Bounded wait for the upstream-consuming thread on release. The thread is
#: daemonized, so an upstream that ignores cancellation can never pin a
#: WSGI worker longer than this.
WORKER_JOIN_TIMEOUT = 5.0
#: Maximum number of chunks buffered between the (possibly fast) upstream
#: and the (network-bound) HTTP client. Provides backpressure without ever
#: blocking the producer event loop (see ``_SyncStreamBridge._enqueue``).
QUEUE_MAX_SIZE = 64
_QUEUE_PUT_TIMEOUT = 0.1

_END = object()
_ERROR = object()


async def safe_aclose(async_iterable: AsyncIterator[Any]) -> None:
    """Close an async iterator, swallowing secondary cleanup errors.

    Closing an already exhausted or finalized async generator is a no-op.
    ``CancelledError`` / ``GeneratorExit`` are intentionally not swallowed:
    an active cancellation must keep propagating.
    """
    aclose = getattr(async_iterable, "aclose", None)
    if aclose is None:
        return
    with contextlib.suppress(Exception):
        await aclose()


async def cancelable_async_stream(
    async_iterable: AsyncIterator[str],
) -> AsyncIterator[str]:
    """Forward chunks from ``async_iterable`` to an ASGI response.

    ``async for`` does not close its iterator, so the upstream model stream
    is closed explicitly on every exit path: normal completion, genuine
    error propagation and client disconnection (Django cancels the streaming
    task on ``http.disconnect``, which unwinds this generator). Anything the
    upstream emits afterwards is discarded; an active cancellation never
    surfaces as an error.
    """
    try:
        async for chunk in async_iterable:
            yield chunk
    finally:
        await safe_aclose(async_iterable)


def build_disconnect_checker(
    meta: Mapping[str, Any] | None,
) -> Callable[[], bool] | None:
    """Build a zero-argument callable reporting client disconnections.

    Only gunicorn exposes the live client socket in the WSGI environ
    (``gunicorn.socket``). When the running server doesn't expose it
    (e.g. the Django development server), ``None`` is returned: the
    disconnect is then detected on the next write, when the WSGI server
    closes the response iterator.
    """
    if not isinstance(meta, Mapping):
        return None

    client_socket = meta.get("gunicorn.socket")
    if client_socket is None:
        return None

    def client_disconnected() -> bool:
        try:
            readable, _, _ = select.select([client_socket], [], [], 0)
        except OSError, ValueError:
            return True
        if not readable:
            return False
        try:
            # The request body has already been fully consumed when the
            # response is streamed, so readable data here can only be a
            # FIN (empty read) or a RST (OSError) from the client.
            return client_socket.recv(1, socket.MSG_PEEK) == b""
        except ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError:
            return False
        except OSError:
            return True

    return client_disconnected


class _ConsumerGoneError(Exception):
    """Internal signal: the producer noticed the cancellation while blocked
    on a full queue."""


class _SyncStreamBridge:  # pylint: disable=too-many-instance-attributes
    """Run an async iterator in a dedicated thread and expose it as a
    cancellation-aware sync iterator consumed by a WSGI worker.

    Two threads collaborate:

    * the **WSGI worker thread** iterates the bridge and blocks on a bounded
      ``queue.get`` poll loop, periodically checking the client socket;
    * the **producer thread** runs a private event loop, pulls the upstream
      chunks and pushes them to the bounded queue.

    All termination paths (natural end, upstream error, iterator close after
    a failed write, client socket closed while idle) collapse into a single
    idempotent :meth:`release`.
    """

    def __init__(
        self,
        async_iterable: AsyncIterator[str],
        is_client_disconnected: Callable[[], bool] | None,
    ) -> None:
        self._agen = async_iterable
        self._is_client_disconnected = is_client_disconnected
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=QUEUE_MAX_SIZE)
        self._cancel = threading.Event()
        self._released = threading.Event()
        self._release_lock = threading.Lock()
        self._loop_ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(
            target=self._run_event_loop,
            name="ai-upstream-stream",
            daemon=True,
        )

    # -- consumer side (WSGI worker thread) -------------------------------

    def __iter__(self) -> Iterator[str]:
        return self._iterate()

    def _iterate(self) -> Iterator[str]:
        # Lazy start: building the bridge must not consume any upstream data
        # before the response is actually iterated.
        self._thread.start()
        try:
            while True:
                try:
                    item = self._queue.get(timeout=DISCONNECT_POLL_INTERVAL)
                except queue.Empty:
                    if self._cancel.is_set():
                        return
                    if (
                        self._is_client_disconnected is not None
                        and self._is_client_disconnected()
                    ):
                        return
                    continue

                # Discard anything arriving after the cancellation, late
                # errors included.
                if self._cancel.is_set():
                    return
                if item is _END:
                    return
                if isinstance(item, tuple) and item[0] is _ERROR:
                    raise item[1]
                yield item
        finally:
            self.release()

    def release(self) -> None:
        """Release every resource exactly once.

        Safe to call repeatedly and from any termination path.
        """
        if self._released.is_set():
            return
        with self._release_lock:
            if self._released.is_set():
                return
            self._released.set()

        # Unblocks the producer's cancellation watcher, which in turn
        # cancels the upstream chunk awaited on the producer loop.
        self._cancel.set()

        self._loop_ready.wait(timeout=1.0)
        self._thread.join(timeout=WORKER_JOIN_TIMEOUT)
        if self._thread.is_alive():
            log.warning(
                "AI upstream stream did not stop within %.0fs after "
                "cancellation; leaving the daemon thread to finish",
                WORKER_JOIN_TIMEOUT,
            )

    # -- producer side (dedicated thread + private event loop) ------------

    def _run_event_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        try:
            loop.run_until_complete(self._produce())
        except Exception:  # pylint: disable=broad-except; never escape the thread
            # pragma: no cover - _produce swallows its own errors
            log.exception("AI upstream stream thread crashed")
        finally:
            loop.close()

    async def _wait_for_cancel(self) -> None:
        # Block an executor thread, not the event loop, while waiting so the
        # loop stays free to cancel the in-flight upstream await.
        while not self._cancel.is_set():
            await asyncio.to_thread(self._cancel.wait, 0.05)

    async def _enqueue(self, chunk: str) -> None:
        while not self._cancel.is_set():
            try:
                await asyncio.to_thread(
                    self._queue.put, chunk, True, _QUEUE_PUT_TIMEOUT
                )
            except queue.Full:
                continue
            return
        raise _ConsumerGoneError

    async def _produce(self) -> None:
        try:
            while True:
                if self._cancel.is_set():
                    break

                # Race the next upstream chunk against the cancellation.
                chunk_task = asyncio.ensure_future(anext(self._agen))
                cancel_task = asyncio.ensure_future(self._wait_for_cancel())
                done, _ = await asyncio.wait(
                    {chunk_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if cancel_task in done:
                    # Cancellation wins even if a chunk or an upstream error
                    # lands at the very same moment: cancel the pending
                    # anext (this throws into the upstream generator and
                    # closes the model HTTP connection) and drop the result.
                    chunk_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await chunk_task
                    await safe_aclose(self._agen)
                    return

                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task

                try:
                    chunk = chunk_task.result()
                except StopAsyncIteration:
                    break

                if self._cancel.is_set():
                    break
                try:
                    await self._enqueue(chunk)
                except _ConsumerGoneError:
                    break
                if self._cancel.is_set():
                    break
        except Exception as exc:  # pylint: disable=broad-except #noqa: BLE001
            # Genuine upstream failure: preserve its original semantics, but
            # never resurrect it once the consumer is gone.
            if not self._cancel.is_set():
                with contextlib.suppress(queue.Full):
                    self._queue.put((_ERROR, exc), timeout=_QUEUE_PUT_TIMEOUT)
        finally:
            # NOTE: ``_cancel`` must stay clear on a natural / errored end,
            # otherwise the consumer would mistake a forwarded error (or the
            # last chunks) for a cancellation and drop them.
            await safe_aclose(self._agen)
            # The consumer is already gone if the queue is full; nobody is
            # waiting for the sentinel then.
            with contextlib.suppress(queue.Full):
                self._queue.put(_END, timeout=_QUEUE_PUT_TIMEOUT)


def cancelable_sync_stream(
    async_iterable: AsyncIterator[str],
    meta: Mapping[str, Any] | None = None,
) -> Iterator[str]:
    """Expose an async AI stream as a sync iterator for WSGI, with client
    disconnects and generator closes propagated up to the upstream."""
    return iter(_SyncStreamBridge(async_iterable, build_disconnect_checker(meta)))
