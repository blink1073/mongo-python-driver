# Copyright 2015-present MongoDB, Inc.
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
from __future__ import annotations

import asyncio
import copy
import datetime
import sys
import time
from typing import Any

sys.path[0:0] = [""]

from test import (
    IntegrationTest,
    client_context,
    client_knobs,
    sanitize_cmd,
    unittest,
)
from test.utils_shared import (
    EventListener,
    OvertCommandListener,
    wait_until,
)

from bson.int64 import Int64
from bson.objectid import ObjectId
from bson.son import SON
from pymongo import CursorType, DeleteOne, InsertOne, UpdateOne, monitoring
from pymongo.errors import AutoReconnect, NotPrimaryError, OperationFailure
from pymongo.read_preferences import ReadPreference
from pymongo.synchronous.command_cursor import CommandCursor
from pymongo.synchronous.helpers import next
from pymongo.write_concern import WriteConcern

_IS_SYNC = True


class TestCommandMonitoring(IntegrationTest):
    listener: EventListener

    @classmethod
    def setUpClass(cls) -> None:
        cls.listener = OvertCommandListener()

    @client_context.require_connection
    def setUp(self) -> None:
        super().setUp()
        self.listener.reset()
        self.client = self.rs_or_single_client(event_listeners=[self.listener], retryWrites=False)

    def test_started_simple(self):
        self.client.pymongo_test.command("ping")
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        self.assertEqualCommand(SON([("ping", 1)]), started.command)
        self.assertEqual("ping", started.command_name)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertIsInstance(started.request_id, int)

    def test_succeeded_simple(self):
        self.client.pymongo_test.command("ping")
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertEqual("ping", succeeded.command_name)
        self.assertEqual(self.client.address, succeeded.connection_id)
        self.assertEqual(1, succeeded.reply.get("ok"))
        self.assertIsInstance(succeeded.request_id, int)
        self.assertIsInstance(succeeded.duration_micros, int)

    def test_failed_simple(self):
        try:
            self.client.pymongo_test.command("oops!")
        except OperationFailure:
            pass
        started = self.listener.started_events[0]
        failed = self.listener.failed_events[0]
        self.assertEqual(0, len(self.listener.succeeded_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        self.assertIsInstance(failed, monitoring.CommandFailedEvent)
        self.assertEqual("oops!", failed.command_name)
        self.assertEqual(self.client.address, failed.connection_id)
        self.assertEqual(0, failed.failure.get("ok"))
        self.assertIsInstance(failed.request_id, int)
        self.assertIsInstance(failed.duration_micros, int)

    def test_find_one(self):
        self.client.pymongo_test.test.find_one()
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        self.assertEqualCommand(
            SON([("find", "test"), ("filter", {}), ("limit", 1), ("singleBatch", True)]),
            started.command,
        )
        self.assertEqual("find", started.command_name)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertIsInstance(started.request_id, int)

    def test_find_and_get_more(self):
        self.client.pymongo_test.test.drop()
        self.client.pymongo_test.test.insert_many([{} for _ in range(10)])
        self.listener.reset()
        cursor = self.client.pymongo_test.test.find(projection={"_id": False}, batch_size=4)
        for _ in range(4):
            next(cursor)
        cursor_id = cursor.cursor_id
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        self.assertEqualCommand(
            SON(
                [("find", "test"), ("filter", {}), ("projection", {"_id": False}), ("batchSize", 4)]
            ),
            started.command,
        )
        self.assertEqual("find", started.command_name)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertIsInstance(started.request_id, int)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual("find", succeeded.command_name)
        self.assertIsInstance(succeeded.request_id, int)
        self.assertEqual(cursor.address, succeeded.connection_id)
        csr = succeeded.reply["cursor"]
        self.assertEqual(csr["id"], cursor_id)
        self.assertEqual(csr["ns"], "pymongo_test.test")
        self.assertEqual(csr["firstBatch"], [{} for _ in range(4)])

        self.listener.reset()
        # Next batch. Exhausting the cursor could cause a getMore
        # that returns id of 0 and no results.
        next(cursor)
        try:
            started = self.listener.started_events[0]
            succeeded = self.listener.succeeded_events[0]
            self.assertEqual(0, len(self.listener.failed_events))
            self.assertIsInstance(started, monitoring.CommandStartedEvent)
            self.assertEqualCommand(
                SON([("getMore", cursor_id), ("collection", "test"), ("batchSize", 4)]),
                started.command,
            )
            self.assertEqual("getMore", started.command_name)
            self.assertEqual(self.client.address, started.connection_id)
            self.assertEqual("pymongo_test", started.database_name)
            self.assertIsInstance(started.request_id, int)
            self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeeded.duration_micros, int)
            self.assertEqual("getMore", succeeded.command_name)
            self.assertIsInstance(succeeded.request_id, int)
            self.assertEqual(cursor.address, succeeded.connection_id)
            csr = succeeded.reply["cursor"]
            self.assertEqual(csr["id"], cursor_id)
            self.assertEqual(csr["ns"], "pymongo_test.test")
            self.assertEqual(csr["nextBatch"], [{} for _ in range(4)])
        finally:
            # Exhaust the cursor to avoid kill cursors.
            tuple(cursor.to_list())

    def test_find_with_explain(self):
        cmd = SON([("explain", SON([("find", "test"), ("filter", {})]))])
        self.client.pymongo_test.test.drop()
        self.client.pymongo_test.test.insert_one({})
        self.listener.reset()
        coll = self.client.pymongo_test.test
        # Test that we publish the unwrapped command.
        if self.client.is_mongos:
            coll = coll.with_options(read_preference=ReadPreference.PRIMARY_PREFERRED)
        res = coll.find().explain()
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        self.assertEqualCommand(cmd, started.command)
        self.assertEqual("explain", started.command_name)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertIsInstance(started.request_id, int)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual("explain", succeeded.command_name)
        self.assertIsInstance(succeeded.request_id, int)
        self.assertEqual(self.client.address, succeeded.connection_id)
        self.assertEqual(res, succeeded.reply)

    def _test_find_options(self, query, expected_cmd):
        coll = self.client.pymongo_test.test
        coll.drop()
        coll.create_index("x")
        coll.insert_many([{"x": i} for i in range(5)])

        # Test that we publish the unwrapped command.
        self.listener.reset()
        if self.client.is_mongos:
            coll = coll.with_options(read_preference=ReadPreference.PRIMARY_PREFERRED)

        cursor = coll.find(**query)

        next(cursor)
        try:
            started = self.listener.started_events[0]
            succeeded = self.listener.succeeded_events[0]
            self.assertEqual(0, len(self.listener.failed_events))
            self.assertIsInstance(started, monitoring.CommandStartedEvent)
            self.assertEqualCommand(expected_cmd, started.command)
            self.assertEqual("find", started.command_name)
            self.assertEqual(self.client.address, started.connection_id)
            self.assertEqual("pymongo_test", started.database_name)
            self.assertIsInstance(started.request_id, int)
            self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeeded.duration_micros, int)
            self.assertEqual("find", succeeded.command_name)
            self.assertIsInstance(succeeded.request_id, int)
            self.assertEqual(self.client.address, succeeded.connection_id)
        finally:
            # Exhaust the cursor to avoid kill cursors.
            tuple(cursor.to_list())

    def test_find_options(self):
        query = {
            "filter": {},
            "hint": [("x", 1)],
            "max_time_ms": 10000,
            "max": {"x": 10},
            "min": {"x": -10},
            "return_key": True,
            "show_record_id": True,
            "projection": {"x": False},
            "skip": 1,
            "no_cursor_timeout": True,
            "sort": [("_id", 1)],
            "allow_partial_results": True,
            "comment": "this is a test",
            "batch_size": 2,
        }

        cmd = {
            "find": "test",
            "filter": {},
            "hint": SON([("x", 1)]),
            "comment": "this is a test",
            "maxTimeMS": 10000,
            "max": {"x": 10},
            "min": {"x": -10},
            "returnKey": True,
            "showRecordId": True,
            "sort": SON([("_id", 1)]),
            "projection": {"x": False},
            "skip": 1,
            "batchSize": 2,
            "noCursorTimeout": True,
            "allowPartialResults": True,
        }

        if client_context.version < (4, 1, 0, -1):
            query["max_scan"] = 10
            cmd["maxScan"] = 10

        self._test_find_options(query, cmd)

    @client_context.require_version_max(3, 7, 2)
    def test_find_snapshot(self):
        # Test "snapshot" parameter separately, can't combine with "sort".
        query = {"filter": {}, "snapshot": True}

        cmd = {"find": "test", "filter": {}, "snapshot": True}

        self._test_find_options(query, cmd)

    def test_command_and_get_more(self):
        self.client.pymongo_test.test.drop()
        self.client.pymongo_test.test.insert_many([{"x": 1} for _ in range(10)])
        self.listener.reset()
        coll = self.client.pymongo_test.test
        # Test that we publish the unwrapped command.
        if self.client.is_mongos:
            coll = coll.with_options(read_preference=ReadPreference.PRIMARY_PREFERRED)
        cursor = coll.aggregate([{"$project": {"_id": False, "x": 1}}], batchSize=4)
        for _ in range(4):
            next(cursor)
        cursor_id = cursor.cursor_id
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        self.assertEqualCommand(
            SON(
                [
                    ("aggregate", "test"),
                    ("pipeline", [{"$project": {"_id": False, "x": 1}}]),
                    ("cursor", {"batchSize": 4}),
                ]
            ),
            started.command,
        )
        self.assertEqual("aggregate", started.command_name)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertIsInstance(started.request_id, int)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual("aggregate", succeeded.command_name)
        self.assertIsInstance(succeeded.request_id, int)
        self.assertEqual(cursor.address, succeeded.connection_id)
        expected_cursor = {
            "id": cursor_id,
            "ns": "pymongo_test.test",
            "firstBatch": [{"x": 1} for _ in range(4)],
        }
        self.assertEqualCommand(expected_cursor, succeeded.reply.get("cursor"))

        self.listener.reset()
        next(cursor)
        try:
            started = self.listener.started_events[0]
            succeeded = self.listener.succeeded_events[0]
            self.assertEqual(0, len(self.listener.failed_events))
            self.assertIsInstance(started, monitoring.CommandStartedEvent)
            self.assertEqualCommand(
                SON([("getMore", cursor_id), ("collection", "test"), ("batchSize", 4)]),
                started.command,
            )
            self.assertEqual("getMore", started.command_name)
            self.assertEqual(self.client.address, started.connection_id)
            self.assertEqual("pymongo_test", started.database_name)
            self.assertIsInstance(started.request_id, int)
            self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeeded.duration_micros, int)
            self.assertEqual("getMore", succeeded.command_name)
            self.assertIsInstance(succeeded.request_id, int)
            self.assertEqual(cursor.address, succeeded.connection_id)
            expected_result = {
                "cursor": {
                    "id": cursor_id,
                    "ns": "pymongo_test.test",
                    "nextBatch": [{"x": 1} for _ in range(4)],
                },
                "ok": 1.0,
            }
            self.assertEqualReply(expected_result, succeeded.reply)
        finally:
            # Exhaust the cursor to avoid kill cursors.
            tuple(cursor.to_list())

    def test_get_more_failure(self):
        address = self.client.address
        coll = self.client.pymongo_test.test
        cursor_id = Int64(12345)
        cursor_doc = {"id": cursor_id, "firstBatch": [], "ns": coll.full_name}
        cursor = CommandCursor(coll, cursor_doc, address)
        try:
            next(cursor)
        except Exception:
            pass
        started = self.listener.started_events[0]
        self.assertEqual(0, len(self.listener.succeeded_events))
        failed = self.listener.failed_events[0]
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        self.assertEqualCommand(
            SON([("getMore", cursor_id), ("collection", "test")]), started.command
        )
        self.assertEqual("getMore", started.command_name)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertIsInstance(started.request_id, int)
        self.assertIsInstance(failed, monitoring.CommandFailedEvent)
        self.assertIsInstance(failed.duration_micros, int)
        self.assertEqual("getMore", failed.command_name)
        self.assertIsInstance(failed.request_id, int)
        self.assertEqual(cursor.address, failed.connection_id)
        self.assertEqual(0, failed.failure.get("ok"))

    @client_context.require_replica_set
    @client_context.require_secondaries_count(1)
    def test_not_primary_error(self):
        address = next(iter(client_context.client.secondaries))
        client = self.single_client(*address, event_listeners=[self.listener])
        # Clear authentication command results from the listener.
        client.admin.command("ping")
        self.listener.reset()
        error = None
        try:
            client.pymongo_test.test.find_one_and_delete({})
        except NotPrimaryError as exc:
            error = exc.errors
        started = self.listener.started_events[0]
        failed = self.listener.failed_events[0]
        self.assertEqual(0, len(self.listener.succeeded_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        self.assertIsInstance(failed, monitoring.CommandFailedEvent)
        self.assertEqual("findAndModify", failed.command_name)
        self.assertEqual(address, failed.connection_id)
        self.assertEqual(0, failed.failure.get("ok"))
        self.assertIsInstance(failed.request_id, int)
        self.assertIsInstance(failed.duration_micros, int)
        self.assertEqual(error, failed.failure)

    @client_context.require_no_mongos
    def test_exhaust(self):
        self.client.pymongo_test.test.drop()
        self.client.pymongo_test.test.insert_many([{} for _ in range(11)])
        self.listener.reset()
        cursor = self.client.pymongo_test.test.find(
            projection={"_id": False}, batch_size=5, cursor_type=CursorType.EXHAUST
        )
        next(cursor)
        cursor_id = cursor.cursor_id
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        self.assertEqualCommand(
            SON(
                [("find", "test"), ("filter", {}), ("projection", {"_id": False}), ("batchSize", 5)]
            ),
            started.command,
        )
        self.assertEqual("find", started.command_name)
        self.assertEqual(cursor.address, started.connection_id)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertIsInstance(started.request_id, int)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual("find", succeeded.command_name)
        self.assertIsInstance(succeeded.request_id, int)
        self.assertEqual(cursor.address, succeeded.connection_id)
        expected_result = {
            "cursor": {
                "id": cursor_id,
                "ns": "pymongo_test.test",
                "firstBatch": [{} for _ in range(5)],
            },
            "ok": 1,
        }
        self.assertEqualReply(expected_result, succeeded.reply)

        self.listener.reset()
        tuple(cursor.to_list())
        self.assertEqual(0, len(self.listener.failed_events))
        for event in self.listener.started_events:
            self.assertIsInstance(event, monitoring.CommandStartedEvent)
            self.assertEqualCommand(
                SON([("getMore", cursor_id), ("collection", "test"), ("batchSize", 5)]),
                event.command,
            )
            self.assertEqual("getMore", event.command_name)
            self.assertEqual(cursor.address, event.connection_id)
            self.assertEqual("pymongo_test", event.database_name)
            self.assertIsInstance(event.request_id, int)
        for event in self.listener.succeeded_events:
            self.assertIsInstance(event, monitoring.CommandSucceededEvent)
            self.assertIsInstance(event.duration_micros, int)
            self.assertEqual("getMore", event.command_name)
            self.assertIsInstance(event.request_id, int)
            self.assertEqual(cursor.address, event.connection_id)
        # Last getMore receives a response with cursor id 0.
        self.assertEqual(0, self.listener.succeeded_events[-1].reply["cursor"]["id"])

    def test_kill_cursors(self):
        with client_knobs(kill_cursor_frequency=0.01):
            self.client.pymongo_test.test.drop()
            self.client.pymongo_test.test.insert_many([{} for _ in range(10)])
            cursor = self.client.pymongo_test.test.find().batch_size(5)
            next(cursor)
            cursor_id = cursor.cursor_id
            self.listener.reset()
            cursor.close()
            time.sleep(2)
            started = self.listener.started_events[0]
            succeeded = self.listener.succeeded_events[0]
            self.assertEqual(0, len(self.listener.failed_events))
            self.assertIsInstance(started, monitoring.CommandStartedEvent)
            # There could be more than one cursor_id here depending on
            # when the thread last ran.
            self.assertIn(cursor_id, started.command["cursors"])
            self.assertEqual("killCursors", started.command_name)
            self.assertIs(type(started.connection_id), tuple)
            self.assertEqual(cursor.address, started.connection_id)
            self.assertEqual("pymongo_test", started.database_name)
            self.assertIsInstance(started.request_id, int)
            self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeeded.duration_micros, int)
            self.assertEqual("killCursors", succeeded.command_name)
            self.assertIsInstance(succeeded.request_id, int)
            self.assertIs(type(succeeded.connection_id), tuple)
            self.assertEqual(cursor.address, succeeded.connection_id)
            # There could be more than one cursor_id here depending on
            # when the thread last ran.
            self.assertIn(
                cursor_id, succeeded.reply["cursorsUnknown"] + succeeded.reply["cursorsKilled"]
            )

    def test_non_bulk_writes(self):
        coll = self.client.pymongo_test.test
        coll.drop()
        self.listener.reset()

        # Implied write concern insert_one
        res = coll.insert_one({"x": 1})
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON(
            [
                ("insert", coll.name),
                ("ordered", True),
                ("documents", [{"_id": res.inserted_id, "x": 1}]),
            ]
        )
        self.assertEqualCommand(expected, started.command)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertEqual("insert", started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get("ok"))
        self.assertEqual(1, reply.get("n"))

        # Unacknowledged insert_one
        self.listener.reset()
        coll = coll.with_options(write_concern=WriteConcern(w=0))
        res = coll.insert_one({"x": 1})
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON(
            [
                ("insert", coll.name),
                ("ordered", True),
                ("documents", [{"_id": res.inserted_id, "x": 1}]),
                ("writeConcern", {"w": 0}),
            ]
        )
        self.assertEqualCommand(expected, started.command)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertEqual("insert", started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        self.assertEqualReply(succeeded.reply, {"ok": 1})

        # Explicit write concern insert_one
        self.listener.reset()
        coll = coll.with_options(write_concern=WriteConcern(w=1))
        res = coll.insert_one({"x": 1})
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON(
            [
                ("insert", coll.name),
                ("ordered", True),
                ("documents", [{"_id": res.inserted_id, "x": 1}]),
                ("writeConcern", {"w": 1}),
            ]
        )
        self.assertEqualCommand(expected, started.command)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertEqual("insert", started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get("ok"))
        self.assertEqual(1, reply.get("n"))

        # delete_many
        self.listener.reset()
        res = coll.delete_many({"x": 1})
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON(
            [
                ("delete", coll.name),
                ("ordered", True),
                ("deletes", [SON([("q", {"x": 1}), ("limit", 0)])]),
                ("writeConcern", {"w": 1}),
            ]
        )
        self.assertEqualCommand(expected, started.command)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertEqual("delete", started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get("ok"))
        self.assertEqual(res.deleted_count, reply.get("n"))

        # replace_one
        self.listener.reset()
        oid = ObjectId()
        res = coll.replace_one({"_id": oid}, {"_id": oid, "x": 1}, upsert=True)
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON(
            [
                ("update", coll.name),
                ("ordered", True),
                (
                    "updates",
                    [
                        SON(
                            [
                                ("q", {"_id": oid}),
                                ("u", {"_id": oid, "x": 1}),
                                ("multi", False),
                                ("upsert", True),
                            ]
                        )
                    ],
                ),
                ("writeConcern", {"w": 1}),
            ]
        )
        self.assertEqualCommand(expected, started.command)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertEqual("update", started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get("ok"))
        self.assertEqual(1, reply.get("n"))
        self.assertEqual([{"index": 0, "_id": oid}], reply.get("upserted"))

        # update_one
        self.listener.reset()
        res = coll.update_one({"x": 1}, {"$inc": {"x": 1}})
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON(
            [
                ("update", coll.name),
                ("ordered", True),
                (
                    "updates",
                    [
                        SON(
                            [
                                ("q", {"x": 1}),
                                ("u", {"$inc": {"x": 1}}),
                                ("multi", False),
                                ("upsert", False),
                            ]
                        )
                    ],
                ),
                ("writeConcern", {"w": 1}),
            ]
        )
        self.assertEqualCommand(expected, started.command)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertEqual("update", started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get("ok"))
        self.assertEqual(1, reply.get("n"))

        # update_many
        self.listener.reset()
        res = coll.update_many({"x": 2}, {"$inc": {"x": 1}})
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON(
            [
                ("update", coll.name),
                ("ordered", True),
                (
                    "updates",
                    [
                        SON(
                            [
                                ("q", {"x": 2}),
                                ("u", {"$inc": {"x": 1}}),
                                ("multi", True),
                                ("upsert", False),
                            ]
                        )
                    ],
                ),
                ("writeConcern", {"w": 1}),
            ]
        )
        self.assertEqualCommand(expected, started.command)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertEqual("update", started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get("ok"))
        self.assertEqual(1, reply.get("n"))

        # delete_one
        self.listener.reset()
        _ = coll.delete_one({"x": 3})
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON(
            [
                ("delete", coll.name),
                ("ordered", True),
                ("deletes", [SON([("q", {"x": 3}), ("limit", 1)])]),
                ("writeConcern", {"w": 1}),
            ]
        )
        self.assertEqualCommand(expected, started.command)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertEqual("delete", started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get("ok"))
        self.assertEqual(1, reply.get("n"))

        self.assertEqual(0, coll.count_documents({}))

        # write errors
        coll.insert_one({"_id": 1})
        try:
            self.listener.reset()
            coll.insert_one({"_id": 1})
        except OperationFailure:
            pass
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON(
            [
                ("insert", coll.name),
                ("ordered", True),
                ("documents", [{"_id": 1}]),
                ("writeConcern", {"w": 1}),
            ]
        )
        self.assertEqualCommand(expected, started.command)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertEqual("insert", started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get("ok"))
        self.assertEqual(0, reply.get("n"))
        errors = reply.get("writeErrors")
        self.assertIsInstance(errors, list)
        error = errors[0]
        self.assertEqual(0, error.get("index"))
        self.assertIsInstance(error.get("code"), int)
        self.assertIsInstance(error.get("errmsg"), str)

    def test_insert_many(self):
        # This always uses the bulk API.
        coll = self.client.pymongo_test.test
        coll.drop()
        self.listener.reset()

        big = "x" * (1024 * 1024 * 4)
        docs = [{"_id": i, "big": big} for i in range(6)]
        coll.insert_many(docs)
        started = self.listener.started_events
        succeeded = self.listener.succeeded_events
        self.assertEqual(0, len(self.listener.failed_events))
        documents = []
        count = 0
        operation_id = started[0].operation_id
        self.assertIsInstance(operation_id, int)
        for start, succeed in zip(started, succeeded):
            self.assertIsInstance(start, monitoring.CommandStartedEvent)
            cmd = sanitize_cmd(start.command)
            self.assertEqual(["insert", "ordered", "documents"], list(cmd.keys()))
            self.assertEqual(coll.name, cmd["insert"])
            self.assertIs(True, cmd["ordered"])
            documents.extend(cmd["documents"])
            self.assertEqual("pymongo_test", start.database_name)
            self.assertEqual("insert", start.command_name)
            self.assertIsInstance(start.request_id, int)
            self.assertEqual(self.client.address, start.connection_id)
            self.assertIsInstance(succeed, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeed.duration_micros, int)
            self.assertEqual(start.command_name, succeed.command_name)
            self.assertEqual(start.request_id, succeed.request_id)
            self.assertEqual(start.connection_id, succeed.connection_id)
            self.assertEqual(start.operation_id, operation_id)
            self.assertEqual(succeed.operation_id, operation_id)
            reply = succeed.reply
            self.assertEqual(1, reply.get("ok"))
            count += reply.get("n", 0)
        self.assertEqual(documents, docs)
        self.assertEqual(6, count)

    def test_insert_many_unacknowledged(self):
        coll = self.client.pymongo_test.test
        coll.drop()
        unack_coll = coll.with_options(write_concern=WriteConcern(w=0))
        self.listener.reset()

        # Force two batches on legacy servers.
        big = "x" * (1024 * 1024 * 12)
        docs = [{"_id": i, "big": big} for i in range(6)]
        unack_coll.insert_many(docs)
        started = self.listener.started_events
        succeeded = self.listener.succeeded_events
        self.assertEqual(0, len(self.listener.failed_events))
        documents = []
        operation_id = started[0].operation_id
        self.assertIsInstance(operation_id, int)
        for start, succeed in zip(started, succeeded):
            self.assertIsInstance(start, monitoring.CommandStartedEvent)
            cmd = sanitize_cmd(start.command)
            cmd.pop("writeConcern", None)
            self.assertEqual(["insert", "ordered", "documents"], list(cmd.keys()))
            self.assertEqual(coll.name, cmd["insert"])
            self.assertIs(True, cmd["ordered"])
            documents.extend(cmd["documents"])
            self.assertEqual("pymongo_test", start.database_name)
            self.assertEqual("insert", start.command_name)
            self.assertIsInstance(start.request_id, int)
            self.assertEqual(self.client.address, start.connection_id)
            self.assertIsInstance(succeed, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeed.duration_micros, int)
            self.assertEqual(start.command_name, succeed.command_name)
            self.assertEqual(start.request_id, succeed.request_id)
            self.assertEqual(start.connection_id, succeed.connection_id)
            self.assertEqual(start.operation_id, operation_id)
            self.assertEqual(succeed.operation_id, operation_id)
            self.assertEqual(1, succeed.reply.get("ok"))
        self.assertEqual(documents, docs)

        def check():
            return coll.count_documents({}) == 6

        wait_until(check, "insert documents with w=0")

    def test_bulk_write(self):
        coll = self.client.pymongo_test.test
        coll.drop()
        self.listener.reset()

        coll.bulk_write(
            [
                InsertOne({"_id": 1}),
                UpdateOne({"_id": 1}, {"$set": {"x": 1}}),
                DeleteOne({"_id": 1}),
            ]
        )
        started = self.listener.started_events
        succeeded = self.listener.succeeded_events
        self.assertEqual(0, len(self.listener.failed_events))
        operation_id = started[0].operation_id
        pairs = list(zip(started, succeeded))
        self.assertEqual(3, len(pairs))
        for start, succeed in pairs:
            self.assertIsInstance(start, monitoring.CommandStartedEvent)
            self.assertEqual("pymongo_test", start.database_name)
            self.assertIsInstance(start.request_id, int)
            self.assertEqual(self.client.address, start.connection_id)
            self.assertIsInstance(succeed, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeed.duration_micros, int)
            self.assertEqual(start.command_name, succeed.command_name)
            self.assertEqual(start.request_id, succeed.request_id)
            self.assertEqual(start.connection_id, succeed.connection_id)
            self.assertEqual(start.operation_id, operation_id)
            self.assertEqual(succeed.operation_id, operation_id)

        expected = SON([("insert", coll.name), ("ordered", True), ("documents", [{"_id": 1}])])
        self.assertEqualCommand(expected, started[0].command)
        expected = SON(
            [
                ("update", coll.name),
                ("ordered", True),
                (
                    "updates",
                    [
                        SON(
                            [
                                ("q", {"_id": 1}),
                                ("u", {"$set": {"x": 1}}),
                                ("multi", False),
                                ("upsert", False),
                            ]
                        )
                    ],
                ),
            ]
        )
        self.assertEqualCommand(expected, started[1].command)
        expected = SON(
            [
                ("delete", coll.name),
                ("ordered", True),
                ("deletes", [SON([("q", {"_id": 1}), ("limit", 1)])]),
            ]
        )
        self.assertEqualCommand(expected, started[2].command)

    @client_context.require_failCommand_fail_point
    def test_bulk_write_command_network_error(self):
        coll = self.client.pymongo_test.test
        self.listener.reset()

        insert_network_error = {
            "configureFailPoint": "failCommand",
            "mode": {"times": 1},
            "data": {
                "failCommands": ["insert"],
                "closeConnection": True,
            },
        }
        with self.fail_point(insert_network_error):
            with self.assertRaises(AutoReconnect):
                coll.bulk_write([InsertOne({"_id": 1})])
        failed = self.listener.failed_events
        self.assertEqual(1, len(failed))
        event = failed[0]
        self.assertEqual(event.command_name, "insert")
        self.assertIsInstance(event.failure, dict)
        self.assertEqual(event.failure["errtype"], "AutoReconnect")
        self.assertTrue(event.failure["errmsg"])

    @client_context.require_failCommand_fail_point
    def test_bulk_write_command_error(self):
        coll = self.client.pymongo_test.test
        self.listener.reset()

        insert_command_error = {
            "configureFailPoint": "failCommand",
            "mode": {"times": 1},
            "data": {
                "failCommands": ["insert"],
                "closeConnection": False,
                "errorCode": 10107,  # Not primary
            },
        }
        with self.fail_point(insert_command_error):
            with self.assertRaises(NotPrimaryError):
                coll.bulk_write([InsertOne({"_id": 1})])
        failed = self.listener.failed_events
        self.assertEqual(1, len(failed))
        event = failed[0]
        self.assertEqual(event.command_name, "insert")
        self.assertIsInstance(event.failure, dict)
        self.assertEqual(event.failure["code"], 10107)
        self.assertTrue(event.failure["errmsg"])

    def test_write_errors(self):
        coll = self.client.pymongo_test.test
        coll.drop()
        self.listener.reset()

        try:
            coll.bulk_write(
                [
                    InsertOne({"_id": 1}),
                    InsertOne({"_id": 1}),
                    InsertOne({"_id": 1}),
                    DeleteOne({"_id": 1}),
                ],
                ordered=False,
            )
        except OperationFailure:
            pass
        started = self.listener.started_events
        succeeded = self.listener.succeeded_events
        self.assertEqual(0, len(self.listener.failed_events))
        operation_id = started[0].operation_id
        pairs = list(zip(started, succeeded))
        errors = []
        for start, succeed in pairs:
            self.assertIsInstance(start, monitoring.CommandStartedEvent)
            self.assertEqual("pymongo_test", start.database_name)
            self.assertIsInstance(start.request_id, int)
            self.assertEqual(self.client.address, start.connection_id)
            self.assertIsInstance(succeed, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeed.duration_micros, int)
            self.assertEqual(start.command_name, succeed.command_name)
            self.assertEqual(start.request_id, succeed.request_id)
            self.assertEqual(start.connection_id, succeed.connection_id)
            self.assertEqual(start.operation_id, operation_id)
            self.assertEqual(succeed.operation_id, operation_id)
            if "writeErrors" in succeed.reply:
                errors.extend(succeed.reply["writeErrors"])

        self.assertEqual(2, len(errors))
        fields = {"index", "code", "errmsg"}
        for error in errors:
            self.assertLessEqual(fields, set(error))

    def test_first_batch_helper(self):
        # Regardless of server version and use of helpers._first_batch
        # this test should still pass.
        self.listener.reset()
        tuple((self.client.pymongo_test.test.list_indexes()).to_list())
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON([("listIndexes", "test"), ("cursor", {})])
        self.assertEqualCommand(expected, started.command)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertEqual("listIndexes", started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        self.assertIn("cursor", succeeded.reply)
        self.assertIn("ok", succeeded.reply)

        self.listener.reset()

    @client_context.require_version_max(6, 1, 99)
    def test_sensitive_commands(self):
        listener = EventListener()
        client = self.rs_or_single_client(event_listeners=[listener])
        listeners = client._event_listeners

        listener.reset()
        cmd = SON([("getnonce", 1)])
        listeners.publish_command_start(cmd, "pymongo_test", 12345, client.address, None)  # type: ignore[arg-type]
        delta = datetime.timedelta(milliseconds=100)
        listeners.publish_command_success(
            delta,
            {"nonce": "e474f4561c5eb40b", "ok": 1.0},
            "getnonce",
            12345,
            self.client.address,  # type: ignore[arg-type]
            None,
            database_name="pymongo_test",
        )
        started = listener.started_events[0]
        succeeded = listener.succeeded_events[0]
        self.assertEqual(0, len(listener.failed_events))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        self.assertEqual({}, started.command)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertEqual("getnonce", started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertEqual(succeeded.duration_micros, 100000)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        self.assertEqual({}, succeeded.reply)


class TestGlobalListener(IntegrationTest):
    listener: EventListener
    saved_listeners: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.listener = OvertCommandListener()
        # We plan to call register(), which internally modifies _LISTENERS.
        cls.saved_listeners = copy.deepcopy(monitoring._LISTENERS)
        monitoring.register(cls.listener)

    @client_context.require_connection
    def setUp(self):
        super().setUp()
        self.listener.reset()
        self.client = self.single_client()
        # Get one (authenticated) socket in the pool.
        self.client.pymongo_test.command("ping")

    @classmethod
    def tearDownClass(cls):
        monitoring._LISTENERS = cls.saved_listeners

    def test_simple(self):
        self.client.pymongo_test.command("ping")
        started = self.listener.started_events[0]
        succeeded = self.listener.succeeded_events[0]
        self.assertEqual(0, len(self.listener.failed_events))
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        self.assertEqualCommand(SON([("ping", 1)]), started.command)
        self.assertEqual("ping", started.command_name)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertEqual("pymongo_test", started.database_name)
        self.assertIsInstance(started.request_id, int)


class TestEventClasses(unittest.TestCase):
    def test_command_event_repr(self):
        request_id, connection_id, operation_id, db_name = 1, ("localhost", 27017), 2, "admin"
        event = monitoring.CommandStartedEvent(
            {"ping": 1}, db_name, request_id, connection_id, operation_id
        )
        self.assertEqual(
            repr(event),
            "<CommandStartedEvent ('localhost', 27017) db: 'admin', "
            "command: 'ping', operation_id: 2, service_id: None, server_connection_id: None>",
        )
        delta = datetime.timedelta(milliseconds=100)
        event = monitoring.CommandSucceededEvent(
            delta, {"ok": 1}, "ping", request_id, connection_id, operation_id, database_name=db_name
        )
        self.assertEqual(
            repr(event),
            "<CommandSucceededEvent ('localhost', 27017) db: 'admin', "
            "command: 'ping', operation_id: 2, duration_micros: 100000, "
            "service_id: None, server_connection_id: None>",
        )
        event = monitoring.CommandFailedEvent(
            delta, {"ok": 0}, "ping", request_id, connection_id, operation_id, database_name=db_name
        )
        self.assertEqual(
            repr(event),
            "<CommandFailedEvent ('localhost', 27017) db: 'admin', "
            "command: 'ping', operation_id: 2, duration_micros: 100000, "
            "failure: {'ok': 0}, service_id: None, server_connection_id: None>",
        )

    def test_server_heartbeat_event_repr(self):
        connection_id = ("localhost", 27017)
        event = monitoring.ServerHeartbeatStartedEvent(connection_id)
        self.assertEqual(
            repr(event), "<ServerHeartbeatStartedEvent ('localhost', 27017) awaited: False>"
        )
        delta = 0.1
        event = monitoring.ServerHeartbeatSucceededEvent(
            delta,
            {"ok": 1},  # type: ignore[arg-type]
            connection_id,
        )
        self.assertEqual(
            repr(event),
            "<ServerHeartbeatSucceededEvent ('localhost', 27017) "
            "duration: 0.1, awaited: False, reply: {'ok': 1}>",
        )
        event = monitoring.ServerHeartbeatFailedEvent(
            delta,
            "ERROR",  # type: ignore[arg-type]
            connection_id,
        )
        self.assertEqual(
            repr(event),
            "<ServerHeartbeatFailedEvent ('localhost', 27017) "
            "duration: 0.1, awaited: False, reply: 'ERROR'>",
        )

    def test_server_event_repr(self):
        server_address = ("localhost", 27017)
        topology_id = ObjectId("000000000000000000000001")
        event = monitoring.ServerOpeningEvent(server_address, topology_id)
        self.assertEqual(
            repr(event),
            "<ServerOpeningEvent ('localhost', 27017) topology_id: 000000000000000000000001>",
        )
        event = monitoring.ServerDescriptionChangedEvent(
            "PREV",  # type: ignore[arg-type]
            "NEW",  # type: ignore[arg-type]
            server_address,
            topology_id,
        )
        self.assertEqual(
            repr(event),
            "<ServerDescriptionChangedEvent ('localhost', 27017) changed from: PREV, to: NEW>",
        )
        event = monitoring.ServerClosedEvent(server_address, topology_id)
        self.assertEqual(
            repr(event),
            "<ServerClosedEvent ('localhost', 27017) topology_id: 000000000000000000000001>",
        )

    def test_topology_event_repr(self):
        topology_id = ObjectId("000000000000000000000001")
        event = monitoring.TopologyOpenedEvent(topology_id)
        self.assertEqual(repr(event), "<TopologyOpenedEvent topology_id: 000000000000000000000001>")
        event = monitoring.TopologyDescriptionChangedEvent(
            "PREV",  # type: ignore[arg-type]
            "NEW",  # type: ignore[arg-type]
            topology_id,
        )
        self.assertEqual(
            repr(event),
            "<TopologyDescriptionChangedEvent "
            "topology_id: 000000000000000000000001 "
            "changed from: PREV, to: NEW>",
        )
        event = monitoring.TopologyClosedEvent(topology_id)
        self.assertEqual(repr(event), "<TopologyClosedEvent topology_id: 000000000000000000000001>")


if __name__ == "__main__":
    unittest.main()
