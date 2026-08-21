# Copyright 2009-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test built in connection-pooling with threads."""

from __future__ import annotations

import asyncio
import gc
import os
import platform
import random
import socket
import ssl
import sys
import time
from unittest.mock import patch

from bson.codec_options import DEFAULT_CODEC_OPTIONS
from bson.son import SON
from pymongo import AsyncMongoClient, message, timeout
from pymongo.errors import AutoReconnect, ConnectionFailure, DuplicateKeyError
from pymongo.hello import HelloCompat
from pymongo.lock import _async_cond_wait, _async_create_lock
from pymongo.monitoring import (
    ConnectionCheckOutFailedEvent,
    ConnectionCheckOutFailedReason,
    PoolClearedEvent,
    _EventListeners,
)
from test.asynchronous.utils import async_get_pool, async_joinall, flaky

sys.path[0:0] = [""]

from pymongo.asynchronous import pool as pool_module
from pymongo.asynchronous.pool import Pool, PoolOptions
from pymongo.socket_checker import SocketChecker
from test.asynchronous import AsyncIntegrationTest, async_client_context, unittest
from test.asynchronous.helpers import ConcurrentRunner
from test.utils_shared import CMAPListener, delay

try:
    import OpenSSL

    _HAVE_PYOPENSSL = True
except ImportError:
    _HAVE_PYOPENSSL = False

_IS_SYNC = False


N = 10
DB = "pymongo-pooling-tests"


async def gc_collect_until_done(tasks, timeout=60):
    start = time.time()
    running = list(tasks)
    while running:
        assert (time.time() - start) < timeout, "Tasks timed out"
        for t in running:
            await t.join(0.1)
            if not t.is_alive():
                running.remove(t)
        gc.collect()


class MongoTask(ConcurrentRunner):
    """A thread/Task that uses a AsyncMongoClient."""

    def __init__(self, client):
        super().__init__()
        self.daemon = True  # Don't hang whole test if task hangs.
        self.client = client
        self.db = self.client[DB]
        self.passed = False

    async def run(self):
        await self.run_mongo_thread()
        self.passed = True

    async def run_mongo_thread(self):
        raise NotImplementedError


class InsertOneAndFind(MongoTask):
    async def run_mongo_thread(self):
        for _ in range(N):
            rand = random.randint(0, N)
            _id = (await self.db.sf.insert_one({"x": rand})).inserted_id
            assert rand == (await self.db.sf.find_one(_id))["x"]


class Unique(MongoTask):
    async def run_mongo_thread(self):
        for _ in range(N):
            await self.db.unique.insert_one({})  # no error


class NonUnique(MongoTask):
    async def run_mongo_thread(self):
        for _ in range(N):
            try:
                await self.db.unique.insert_one({"_id": "jesse"})
            except DuplicateKeyError:
                pass
            else:
                raise AssertionError("Should have raised DuplicateKeyError")


class SocketGetter(MongoTask):
    """Utility for TestPooling.

    Checks out a socket and holds it forever. Used in
    test_no_wait_queue_timeout.
    """

    def __init__(self, client, pool):
        super().__init__(client)
        self.state = "init"
        self.pool = pool
        self.sock = None

    async def run_mongo_thread(self):
        self.state = "get_socket"

        # Call 'pin_cursor' so we can hold the socket.
        async with self.pool.checkout() as sock:
            sock.pin_cursor()
            self.sock = sock

        self.state = "connection"

    async def release_conn(self):
        if self.sock:
            await self.sock.unpin()
            self.sock = None
            return True
        return False


async def run_cases(client, cases):
    tasks = []
    n_runs = 5

    for case in cases:
        for _i in range(n_runs):
            t = case(client)
            await t.start()
            tasks.append(t)

    for t in tasks:
        await t.join()

    for t in tasks:
        assert t.passed, "%s.run() threw an exception" % repr(t)


class _TestPoolingBase(AsyncIntegrationTest):
    """Base class for all connection-pool tests."""

    @async_client_context.require_connection
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.c = await self.async_rs_or_single_client()
        db = self.c[DB]
        await db.unique.drop()
        await db.test.drop()
        await db.unique.insert_one({"_id": "jesse"})
        await db.test.insert_many([{} for _ in range(10)])

    async def create_pool(self, pair=None, *args, **kwargs):
        if pair is None:
            pair = (await async_client_context.host, await async_client_context.port)
        # Start the pool with the correct ssl options.
        pool_options = async_client_context.client._topology_settings.pool_options
        kwargs["ssl_context"] = pool_options._ssl_context
        kwargs["tls_allow_invalid_hostnames"] = pool_options.tls_allow_invalid_hostnames
        kwargs["server_api"] = pool_options.server_api
        pool = Pool(pair, PoolOptions(*args, **kwargs))
        await pool.ready()
        return pool


class TestPooling(_TestPoolingBase):
    async def test_max_pool_size_validation(self):
        host, port = await async_client_context.host, await async_client_context.port
        self.assertRaises(ValueError, AsyncMongoClient, host=host, port=port, maxPoolSize=-1)

        self.assertRaises(ValueError, AsyncMongoClient, host=host, port=port, maxPoolSize="foo")

        c = AsyncMongoClient(host=host, port=port, maxPoolSize=100, connect=False)
        self.assertEqual(c.options.pool_options.max_pool_size, 100)

    async def test_no_disconnect(self):
        await run_cases(self.c, [NonUnique, Unique, InsertOneAndFind])

    async def test_pool_reuses_open_socket(self):
        # Test Pool's _check_closed() method doesn't close a healthy socket.
        cx_pool = await self.create_pool(max_pool_size=10)
        cx_pool._check_interval_seconds = 0  # Always check.
        async with cx_pool.checkout() as conn:
            pass

        async with cx_pool.checkout() as new_connection:
            self.assertEqual(conn, new_connection)

        self.assertEqual(1, len(cx_pool.conns))

    async def test_get_socket_and_exception(self):
        # get_socket() returns socket after a non-network error.
        cx_pool = await self.create_pool(max_pool_size=1, wait_queue_timeout=1)
        with self.assertRaises(ZeroDivisionError):
            async with cx_pool.checkout() as conn:
                1 / 0

        # Socket was returned, not closed.
        async with cx_pool.checkout() as new_connection:
            self.assertEqual(conn, new_connection)

        self.assertEqual(1, len(cx_pool.conns))

    async def test_checkout_event_listener_failure_no_leak(self):
        # Connection is returned to the pool when publish_connection_checked_out raises.
        cx_pool = await self.create_pool(
            max_pool_size=1, event_listeners=_EventListeners([CMAPListener()])
        )

        with patch.object(
            cx_pool.opts._event_listeners,
            "publish_connection_checked_out",
            side_effect=RuntimeError("simulated failure"),
        ):
            with self.assertRaises(RuntimeError):
                async with cx_pool.checkout():
                    pass

        # Connection was returned to the pool — not leaked.
        self.assertEqual(1, len(cx_pool.conns))
        self.assertEqual(0, cx_pool.active_sockets)

        # Pool is still functional.
        async with cx_pool.checkout():
            pass

    async def test_get_conn_reused_connection_rolls_back_on_cancel(self):
        # _get_conn's reused-connection bookkeeping (registering the
        # cancel_context for a connection popped from the idle queue) must
        # roll back pool accounting on failure, the same all-or-nothing
        # contract _get_conn already provides when connect() fails for a
        # brand new connection.
        cx_pool = await self.create_pool(max_pool_size=1)

        async with cx_pool.checkout() as conn:
            pass
        self.assertEqual(1, len(cx_pool.conns))
        reused_context = conn.cancel_context

        class _CancelOnReusedContext(set):
            def add(self, item):
                if item is reused_context:
                    raise asyncio.CancelledError()
                super().add(item)

        cx_pool.active_contexts = _CancelOnReusedContext(cx_pool.active_contexts)

        with self.assertRaises(asyncio.CancelledError):
            async with cx_pool.checkout():
                pass

        # Bookkeeping must be rolled back, not left half-updated.
        self.assertEqual(0, cx_pool.active_sockets)
        self.assertEqual(0, cx_pool.requests)
        self.assertEqual(0, cx_pool.operation_count)

    async def test_pool_removes_closed_socket(self):
        # Test that Pool removes explicitly closed socket.
        cx_pool = await self.create_pool()

        async with cx_pool.checkout() as conn:
            # Use Connection's API to close the socket.
            await conn.close_conn(None)

        self.assertEqual(0, len(cx_pool.conns))

    async def test_pool_removes_dead_socket(self):
        # Test that Pool removes dead socket and the socket doesn't return
        # itself PYTHON-344
        cx_pool = await self.create_pool(max_pool_size=1, wait_queue_timeout=1)
        cx_pool._check_interval_seconds = 0  # Always check.

        async with cx_pool.checkout() as conn:
            # Simulate a closed socket without telling the Connection it's
            # closed.
            await conn.conn.close()
            self.assertTrue(conn.conn_closed())

        async with cx_pool.checkout() as new_connection:
            self.assertEqual(0, len(cx_pool.conns))
            self.assertNotEqual(conn, new_connection)

        self.assertEqual(1, len(cx_pool.conns))

        # Semaphore was released.
        async with cx_pool.checkout():
            pass

    async def test_socket_closed(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((await async_client_context.host, await async_client_context.port))
        socket_checker = SocketChecker()
        self.assertFalse(socket_checker.socket_closed(s))
        s.close()
        self.assertTrue(socket_checker.socket_closed(s))

    async def test_socket_checker(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((await async_client_context.host, await async_client_context.port))
        socket_checker = SocketChecker()
        # Socket has nothing to read.
        self.assertFalse(socket_checker.select(s, read=True))
        self.assertFalse(socket_checker.select(s, read=True, timeout=0))
        self.assertFalse(socket_checker.select(s, read=True, timeout=0.05))
        # Socket is writable.
        self.assertTrue(socket_checker.select(s, write=True, timeout=None))
        self.assertTrue(socket_checker.select(s, write=True))
        self.assertTrue(socket_checker.select(s, write=True, timeout=0))
        self.assertTrue(socket_checker.select(s, write=True, timeout=0.05))
        # Make the socket readable
        _, msg, _, _ = message._op_msg(0, SON([("ping", 1)]), "admin", None, DEFAULT_CODEC_OPTIONS)
        s.sendall(msg)
        # Block until the socket is readable.
        self.assertTrue(socket_checker.select(s, read=True, timeout=None))
        self.assertTrue(socket_checker.select(s, read=True))
        self.assertTrue(socket_checker.select(s, read=True, timeout=0))
        self.assertTrue(socket_checker.select(s, read=True, timeout=0.05))
        # Socket is still writable.
        self.assertTrue(socket_checker.select(s, write=True, timeout=None))
        self.assertTrue(socket_checker.select(s, write=True))
        self.assertTrue(socket_checker.select(s, write=True, timeout=0))
        self.assertTrue(socket_checker.select(s, write=True, timeout=0.05))
        s.close()
        self.assertTrue(socket_checker.socket_closed(s))

    async def test_return_socket_after_reset(self):
        pool = await self.create_pool()
        async with pool.checkout() as sock:
            self.assertEqual(pool.active_sockets, 1)
            self.assertEqual(pool.operation_count, 1)
            await pool.reset()

        self.assertTrue(sock.closed)
        self.assertEqual(0, len(pool.conns))
        self.assertEqual(pool.active_sockets, 0)
        self.assertEqual(pool.operation_count, 0)

    async def test_pool_check(self):
        # Test that Pool recovers from two connection failures in a row.
        # This exercises code at the end of Pool._check().
        cx_pool = await self.create_pool(max_pool_size=1, connect_timeout=1, wait_queue_timeout=1)
        cx_pool._check_interval_seconds = 0  # Always check.
        self.addAsyncCleanup(cx_pool.close)

        async with cx_pool.checkout() as conn:
            # Simulate a closed socket without telling the Connection it's
            # closed.
            await conn.conn.close()

        # Swap pool's address with a bad one.
        address, cx_pool.address = cx_pool.address, ("foo.com", 1234)
        with self.assertRaises(AutoReconnect):
            async with cx_pool.checkout():
                pass

        # Back to normal, semaphore was correctly released.
        cx_pool.address = address
        async with cx_pool.checkout():
            pass

    async def test_wait_queue_timeout(self):
        wait_queue_timeout = 2  # Seconds
        pool = await self.create_pool(max_pool_size=1, wait_queue_timeout=wait_queue_timeout)
        self.addAsyncCleanup(pool.close)

        async with pool.checkout():
            start = time.time()
            with self.assertRaises(ConnectionFailure):
                async with pool.checkout():
                    pass

        duration = time.time() - start
        self.assertLess(
            abs(wait_queue_timeout - duration),
            1,
            f"Waited {duration:.2f} seconds for a socket, expected {wait_queue_timeout:f}",
        )

    async def test_wait_queue_timeout_does_not_leak_operation_count(self):
        # A checkout that fails while waiting for a pool slot must not leave
        # operation_count, requests, or active_sockets incremented, and must
        # emit exactly one ConnectionCheckOutFailedEvent, with reason TIMEOUT.
        wait_queue_timeout = 1  # Seconds
        listener = CMAPListener()
        pool = await self.create_pool(
            max_pool_size=1,
            wait_queue_timeout=wait_queue_timeout,
            event_listeners=_EventListeners([listener]),
        )
        self.addAsyncCleanup(pool.close)

        async with pool.checkout():
            self.assertEqual(pool.operation_count, 1)
            self.assertEqual(pool.requests, 1)
            self.assertEqual(pool.active_sockets, 1)
            listener.reset()
            with self.assertRaises(ConnectionFailure):
                async with pool.checkout():
                    pass
            # The failed second checkout must not have left any counter
            # incremented for its own (failed) attempt.
            self.assertEqual(pool.operation_count, 1)
            self.assertEqual(pool.requests, 1)
            self.assertEqual(pool.active_sockets, 1)

            failed_events = listener.events_by_type(ConnectionCheckOutFailedEvent)
            self.assertEqual(len(failed_events), 1, [e.reason for e in failed_events])
            self.assertEqual(failed_events[0].reason, ConnectionCheckOutFailedReason.TIMEOUT)

        self.assertEqual(pool.operation_count, 0)
        self.assertEqual(pool.requests, 0)
        self.assertEqual(pool.active_sockets, 0)

    async def test_paused_pool_checkout_failure_does_not_leak_or_double_emit(self):
        # A checkout that fails because the pool is paused must not leave
        # operation_count, requests, or active_sockets incremented, and must
        # emit exactly one ConnectionCheckOutFailedEvent. With no outstanding
        # checkouts a slot is immediately available, so this exercises the
        # fast path's readiness check.
        listener = CMAPListener()
        pool = await self.create_pool(max_pool_size=1, event_listeners=_EventListeners([listener]))
        self.addAsyncCleanup(pool.close)

        await pool.reset()  # Pause the pool.
        listener.reset()
        with self.assertRaises(AutoReconnect):
            async with pool.checkout():
                pass

        self.assertEqual(pool.operation_count, 0)
        self.assertEqual(pool.requests, 0)
        self.assertEqual(pool.active_sockets, 0)

        failed_events = listener.events_by_type(ConnectionCheckOutFailedEvent)
        self.assertEqual(len(failed_events), 1, [e.reason for e in failed_events])
        self.assertEqual(failed_events[0].reason, ConnectionCheckOutFailedReason.CONN_ERROR)

    async def test_checkout_failed_event_is_emitted_under_the_pool_lock(self):
        # A failing readiness check must publish its
        # ConnectionCheckOutFailedEvent while still holding the pool mutex, so
        # that _reset()'s PoolClearedEvent is always recorded first
        # (PYTHON-3519). Listeners run synchronously inside the publish call,
        # so this one sees whether the emitting code holds the mutex.
        locked_while_emitting = []
        pool_ref: list = []

        class LockObservingListener(CMAPListener):
            def connection_check_out_failed(self, event):
                locked_while_emitting.append(pool_ref[0].lock.locked())
                super().connection_check_out_failed(event)

        listener = LockObservingListener()
        pool = await self.create_pool(max_pool_size=1, event_listeners=_EventListeners([listener]))
        self.addAsyncCleanup(pool.close)
        pool_ref.append(pool)

        await pool.reset()  # Pause the pool.
        listener.reset()
        with self.assertRaises(AutoReconnect):
            async with pool.checkout():
                pass

        self.assertEqual(
            [True],
            locked_while_emitting,
            "ConnectionCheckOutFailedEvent must be published while the pool mutex is held",
        )

    async def test_checkout_failed_event_is_emitted_under_the_pool_lock_slow_path(self):
        # Same guarantee as the test above, for the slow path. That test
        # pauses an idle pool, so only the fast path runs and a slow-path
        # regression would not fail it. Here the only slot is taken, so the
        # checkout blocks on size_cond and reset() wakes it.
        locked_while_emitting = []
        pool_ref: list = []

        class LockObservingListener(CMAPListener):
            def connection_check_out_failed(self, event):
                locked_while_emitting.append(pool_ref[0].lock.locked())
                super().connection_check_out_failed(event)

        listener = LockObservingListener()
        pool = await self.create_pool(max_pool_size=1, event_listeners=_EventListeners([listener]))
        self.addAsyncCleanup(pool.close)
        pool_ref.append(pool)

        errors: list = []

        async def blocked_checkout():
            try:
                async with pool.checkout():
                    pass
            except BaseException as exc:
                errors.append(exc)

        # The checkout must be parked on size_cond before the reset, or it
        # would fail on the fast path and duplicate the test above. Flag it
        # from inside the wait, while size_cond is still held.
        parked: list = []
        real_cond_wait = _async_cond_wait

        async def flagging_cond_wait(condition, timeout):
            if condition is pool.size_cond:
                parked.append(True)
            return await real_cond_wait(condition, timeout)

        with patch.object(pool_module, "_async_cond_wait", flagging_cond_wait):
            async with pool.checkout():
                listener.reset()
                task = ConcurrentRunner(target=blocked_checkout, name="blocked_checkout")
                await task.start()

                start = time.monotonic()
                while not parked:  # noqa: ASYNC110, RUF100
                    self.assertLess(
                        time.monotonic() - start, 30, "checkout never blocked on size_cond"
                    )
                    await asyncio.sleep(0.01)

                await pool.reset()  # Pause the pool and wake the blocked checkout.
                await task.join(30)
                self.assertFalse(task.is_alive(), "blocked checkout never finished")

        self.assertEqual(1, len(errors), f"expected exactly one failed checkout, got {errors}")
        self.assertIsInstance(errors[0], AutoReconnect)
        self.assertEqual(
            [True],
            locked_while_emitting,
            "ConnectionCheckOutFailedEvent must be published while the pool mutex is held",
        )
        # PYTHON-3519: the clear that caused the failure must be recorded first.
        self.assertEqual(
            [PoolClearedEvent, ConnectionCheckOutFailedEvent],
            [
                type(event)
                for event in listener.events_by_type(
                    (PoolClearedEvent, ConnectionCheckOutFailedEvent)
                )
            ],
        )

    async def test_uncontended_checkout_pool_lock_acquisitions(self):
        # An uncontended checkout must do its operation_count, requests and
        # active_sockets bookkeeping in one critical section; splitting that
        # back apart would pass every other test in this file.
        #
        # Two acquisitions are expected for a warm checkout: one for the
        # bookkeeping, one to register the cancel context. size_cond and
        # _max_connecting_cond are separate objects, so this does not count
        # their blocks.
        pool = await self.create_pool(max_pool_size=1)
        self.addAsyncCleanup(pool.close)

        # Check a connection out and back in first, so this measurement
        # covers a warm pool and does not include connection establishment.
        async with pool.checkout():
            pass

        acquires = 0
        real_lock = pool.lock

        class CountingLock:
            async def __aenter__(self):
                nonlocal acquires
                acquires += 1
                return await real_lock.__aenter__()

            async def __aexit__(self, *args):
                return await real_lock.__aexit__(*args)

            def __getattr__(self, name):
                return getattr(real_lock, name)

        pool.lock = CountingLock()  # type: ignore[assignment]
        try:
            async with pool.checkout():
                checkout_acquires = acquires
        finally:
            pool.lock = real_lock

        self.assertEqual(
            2,
            checkout_acquires,
            f"an uncontended checkout should acquire the pool lock twice, once for "
            f"counter bookkeeping and once to register the cancel context, got "
            f"{checkout_acquires}",
        )

    async def test_contended_checkout_pool_lock_acquisitions(self):
        # Same guarantee as the test above, for a checkout that waits for a
        # slot: its bookkeeping belongs in the one size_cond critical section.
        #
        # Driven from a single task, because checkin() acquires the pool lock
        # twice and would pollute the count. The slot is freed from inside the
        # condition wait, where a real waiter would be woken.
        pool = await self.create_pool(max_pool_size=1)
        self.addAsyncCleanup(pool.close)

        async with pool.checkout():
            pass

        # Make the fast path's slot check fail so the slow path runs.
        pool.requests = pool.max_pool_size

        real_cond_wait = _async_cond_wait

        async def releasing_cond_wait(condition, timeout):
            if condition is pool.size_cond:
                pool.requests = 0
                return True
            return await real_cond_wait(condition, timeout)

        acquires = 0
        real_lock = pool.lock

        class CountingLock:
            async def __aenter__(self):
                nonlocal acquires
                acquires += 1
                return await real_lock.__aenter__()

            async def __aexit__(self, *args):
                return await real_lock.__aexit__(*args)

            def __getattr__(self, name):
                return getattr(real_lock, name)

        pool.lock = CountingLock()  # type: ignore[assignment]
        try:
            with patch.object(pool_module, "_async_cond_wait", releasing_cond_wait):
                async with pool.checkout():
                    checkout_acquires = acquires
        finally:
            pool.lock = real_lock

        self.assertEqual(
            2,
            checkout_acquires,
            f"a checkout that waited for a slot should acquire the pool lock twice, "
            f"once for counter bookkeeping and once to register the cancel context, "
            f"got {checkout_acquires}",
        )

    async def test_no_wait_queue_timeout(self):
        # Verify get_socket() with no wait_queue_timeout blocks forever.
        pool = await self.create_pool(max_pool_size=1)
        self.addAsyncCleanup(pool.close)

        # Reach max_size.
        async with pool.checkout() as s1:
            t = SocketGetter(self.c, pool)
            await t.start()
            while t.state != "get_socket":  # noqa: ASYNC110, RUF100
                await asyncio.sleep(0.1)

            await asyncio.sleep(1)
            self.assertEqual(t.state, "get_socket")

        while t.state != "connection":  # noqa: ASYNC110, RUF100
            await asyncio.sleep(0.1)

        self.assertEqual(t.state, "connection")
        self.assertEqual(t.sock, s1)
        # Cleanup
        await t.release_conn()
        await t.join()
        await pool.close()

    async def test_checkout_more_than_max_pool_size(self):
        pool = await self.create_pool(max_pool_size=2)

        socks = []
        for _ in range(2):
            # Call 'pin_cursor' so we can hold the socket.
            async with pool.checkout() as sock:
                sock.pin_cursor()
                socks.append(sock)

        tasks = []
        for _ in range(10):
            t = SocketGetter(self.c, pool)
            await t.start()
            tasks.append(t)
        await asyncio.sleep(1)
        for t in tasks:
            self.assertEqual(t.state, "get_socket")
        # Cleanup
        for socket_info in socks:
            await socket_info.unpin()
        while tasks:
            to_remove = []
            for t in tasks:
                if await t.release_conn():
                    to_remove.append(t)
                    await t.join()
            for t in to_remove:
                tasks.remove(t)
            await asyncio.sleep(0.05)
        await pool.close()

    async def test_maxConnecting(self):
        client = await self.async_rs_or_single_client()
        await self.client.test.test.insert_one({})
        self.addAsyncCleanup(self.client.test.test.delete_many, {})
        pool = await async_get_pool(client)
        docs = []

        # Run 50 short running operations
        async def find_one():
            docs.append(await client.test.test.find_one({}))

        tasks = [ConcurrentRunner(target=find_one) for _ in range(50)]
        for task in tasks:
            await task.start()
        for task in tasks:
            await task.join(10)

        self.assertEqual(len(docs), 50)
        self.assertLessEqual(len(pool.conns), 50)
        # TLS and auth make connection establishment more expensive than
        # the query which leads to more threads hitting maxConnecting.
        # The end result is fewer total connections and better latency.
        if async_client_context.tls and async_client_context.auth_enabled:
            self.assertLessEqual(len(pool.conns), 30)
        else:
            self.assertLessEqual(len(pool.conns), 50)
        # MongoDB 4.4.1 with auth + ssl:
        # maxConnecting = 2:         6 connections in ~0.231+ seconds
        # maxConnecting = unbounded: 50 connections in ~0.642+ seconds
        #
        # MongoDB 4.4.1 with no-auth no-ssl Python 3.8:
        # maxConnecting = 2:         15-22 connections in ~0.108+ seconds
        # maxConnecting = unbounded: 30+ connections in ~0.140+ seconds
        print(len(pool.conns))

    @async_client_context.require_failCommand_appName
    async def test_csot_timeout_message(self):
        client = await self.async_rs_or_single_client(appName="connectionTimeoutApp")
        # Mock an operation failing due to pymongo.timeout().
        mock_connection_timeout = {
            "configureFailPoint": "failCommand",
            "mode": "alwaysOn",
            "data": {
                "blockConnection": True,
                "blockTimeMS": 1000,
                "failCommands": ["find"],
                "appName": "connectionTimeoutApp",
            },
        }

        await client.db.t.insert_one({"x": 1})

        async with self.fail_point(mock_connection_timeout):
            with self.assertRaises(Exception) as error:
                with timeout(0.5):
                    await client.db.t.find_one({"$where": delay(2)})

        self.assertIn("(configured timeouts: timeoutMS: 500.0ms", str(error.exception))

    @async_client_context.require_failCommand_appName
    async def test_socket_timeout_message(self):
        client = await self.async_rs_or_single_client(
            socketTimeoutMS=500, appName="connectionTimeoutApp"
        )
        # Mock an operation failing due to socketTimeoutMS.
        mock_connection_timeout = {
            "configureFailPoint": "failCommand",
            "mode": "alwaysOn",
            "data": {
                "blockConnection": True,
                "blockTimeMS": 1000,
                "failCommands": ["find"],
                "appName": "connectionTimeoutApp",
            },
        }

        await client.db.t.insert_one({"x": 1})

        async with self.fail_point(mock_connection_timeout):
            with self.assertRaises(Exception) as error:
                await client.db.t.find_one({"$where": delay(2)})

        self.assertIn(
            "(configured timeouts: socketTimeoutMS: 500.0ms, connectTimeoutMS: 20000.0ms)",
            str(error.exception),
        )

    @async_client_context.require_failCommand_appName
    async def test_connection_timeout_message(self):
        # Mock a connection creation failing due to timeout.
        mock_connection_timeout = {
            "configureFailPoint": "failCommand",
            "mode": "alwaysOn",
            "data": {
                "blockConnection": True,
                "blockTimeMS": 1000,
                "failCommands": [HelloCompat.LEGACY_CMD, "hello"],
                "appName": "connectionTimeoutApp",
            },
        }

        client = await self.async_rs_or_single_client(
            connectTimeoutMS=500,
            socketTimeoutMS=500,
            appName="connectionTimeoutApp",
            heartbeatFrequencyMS=1000000,
        )
        await client.admin.command("ping")
        pool = await async_get_pool(client)
        await pool.reset_without_pause()
        async with self.fail_point(mock_connection_timeout):
            with self.assertRaises(Exception) as error:
                await client.admin.command("ping")

        self.assertIn(
            "(configured timeouts: socketTimeoutMS: 500.0ms, connectTimeoutMS: 500.0ms)",
            str(error.exception),
        )

    @async_client_context.require_failCommand_appName
    async def test_pool_backpressure_preserves_existing_connections(self):
        client = await self.async_rs_or_single_client()
        coll = client.pymongo_test.t
        pool = await async_get_pool(client)
        await coll.insert_many([{"x": 1} for _ in range(10)])
        t = SocketGetter(self.c, pool)
        await t.start()
        while t.state != "connection":  # noqa: ASYNC110, RUF100
            await asyncio.sleep(0.1)

        assert not t.sock.conn_closed()

        # Mock a session establishment overload.
        mock_connection_fail = {
            "configureFailPoint": "failCommand",
            "mode": {"times": 1},
            "data": {
                "closeConnection": True,
            },
        }

        async with self.fail_point(mock_connection_fail):
            await coll.find_one({})

        # Make sure the existing socket was not affected.
        assert not t.sock.conn_closed()

        # Cleanup
        await t.release_conn()
        await t.join()
        await pool.close()


class TestPoolMaxSize(_TestPoolingBase):
    @unittest.skipIf(
        sys.platform == "darwin" and "CI" in os.environ,
        "PYTHON-5861: $where is too slow on macOS CI",
    )
    async def test_max_pool_size(self):
        max_pool_size = 4
        c = await self.async_rs_or_single_client(maxPoolSize=max_pool_size)
        collection = c[DB].test

        # Need one document.
        await collection.drop()
        await collection.insert_one({})

        # ntasks had better be much larger than max_pool_size to ensure that
        # max_pool_size connections are actually required at some point in this
        # test's execution.
        cx_pool = await async_get_pool(c)
        ntasks = 10
        tasks = []
        lock = _async_create_lock()
        self.n_passed = 0

        async def f():
            for _ in range(5):
                await collection.find_one({"$where": delay(0.1)})
                assert len(cx_pool.conns) <= max_pool_size

            async with lock:
                self.n_passed += 1

        for _i in range(ntasks):
            t = ConcurrentRunner(target=f)
            tasks.append(t)
            await t.start()

        await async_joinall(tasks)
        self.assertEqual(ntasks, self.n_passed)
        self.assertGreater(len(cx_pool.conns), 1)
        self.assertEqual(0, cx_pool.requests)

    @unittest.skipIf(
        sys.platform == "darwin" and "CI" in os.environ,
        "PYTHON-5861: $where is too slow on macOS CI",
    )
    async def test_max_pool_size_none(self):
        c = await self.async_rs_or_single_client(maxPoolSize=None)
        collection = c[DB].test

        # Need one document.
        await collection.drop()
        await collection.insert_one({})

        cx_pool = await async_get_pool(c)
        ntasks = 10
        tasks = []
        lock = _async_create_lock()
        self.n_passed = 0

        async def f():
            for _ in range(5):
                await collection.find_one({"$where": delay(0.1)})

            async with lock:
                self.n_passed += 1

        for _i in range(ntasks):
            t = ConcurrentRunner(target=f)
            tasks.append(t)
            await t.start()

        await async_joinall(tasks)
        self.assertEqual(ntasks, self.n_passed)
        self.assertGreater(len(cx_pool.conns), 1)
        self.assertEqual(cx_pool.max_pool_size, float("inf"))

    async def test_max_pool_size_zero(self):
        c = await self.async_rs_or_single_client(maxPoolSize=0)
        pool = await async_get_pool(c)
        self.assertEqual(pool.max_pool_size, float("inf"))

    async def test_max_pool_size_with_connection_failure(self):
        # The pool acquires its semaphore before attempting to connect; ensure
        # it releases the semaphore on connection failure.
        test_pool = Pool(
            ("somedomainthatdoesntexist.org", 27017),
            PoolOptions(max_pool_size=1, connect_timeout=1, socket_timeout=1, wait_queue_timeout=1),
        )
        await test_pool.ready()

        # First call to get_socket fails; if pool doesn't release its semaphore
        # then the second call raises "ConnectionFailure: Timed out waiting for
        # socket from pool" instead of AutoReconnect.
        for _i in range(2):
            with self.assertRaises(AutoReconnect) as context:
                async with test_pool.checkout():
                    pass

            # Testing for AutoReconnect instead of ConnectionFailure, above,
            # is sufficient right *now* to catch a semaphore leak. But that
            # seems error-prone, so check the message too.
            self.assertNotIn("waiting for socket from pool", str(context.exception))


class TestPoolHandleConnectionError(unittest.TestCase):
    """PYTHON-5919: PyOpenSSL raises OpenSSL.SSL.SysCallError/ZeroReturnError
    (not ssl.SSLEOFError/ssl.SSLZeroReturnError) when the server closes the
    socket during the TLS handshake, e.g. when an ingress rate limiter rejects
    a connection. Pool._handle_connection_error must recognize these as
    handshake-EOF errors and still add the SystemOverloadedError label.
    """

    def _make_pool(self):
        return Pool(("localhost", 27017), PoolOptions())

    def test_stdlib_ssl_eof_error_is_labeled_overloaded(self):
        pool = self._make_pool()
        err = AutoReconnect("connection closed")
        err.__cause__ = ssl.SSLEOFError("EOF occurred in violation of protocol")
        pool._handle_connection_error(err)
        self.assertTrue(err.has_error_label("SystemOverloadedError"))

    @unittest.skipUnless(_HAVE_PYOPENSSL, "PyOpenSSL is not available.")
    def test_pyopenssl_syscall_error_is_labeled_overloaded(self):
        from OpenSSL.SSL import SysCallError

        pool = self._make_pool()
        err = AutoReconnect("connection closed")
        err.__cause__ = SysCallError(-1, "Unexpected EOF")
        pool._handle_connection_error(err)
        self.assertTrue(err.has_error_label("SystemOverloadedError"))

    @unittest.skipUnless(_HAVE_PYOPENSSL, "PyOpenSSL is not available.")
    def test_pyopenssl_zero_return_error_is_labeled_overloaded(self):
        from OpenSSL.SSL import ZeroReturnError

        pool = self._make_pool()
        err = AutoReconnect("connection closed")
        err.__cause__ = ZeroReturnError()
        pool._handle_connection_error(err)
        self.assertTrue(err.has_error_label("SystemOverloadedError"))

    def test_certificate_error_is_not_labeled_overloaded(self):
        pool = self._make_pool()
        err = AutoReconnect("connection closed")
        err.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
        pool._handle_connection_error(err)
        self.assertFalse(err.has_error_label("SystemOverloadedError"))


if __name__ == "__main__":
    unittest.main()
