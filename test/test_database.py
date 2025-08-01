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

"""Test the database module."""
from __future__ import annotations

import re
import sys
from typing import Any, Iterable, List, Mapping, Union

from pymongo.synchronous.command_cursor import CommandCursor

sys.path[0:0] = [""]

from test import IntegrationTest, client_context, unittest
from test.test_custom_types import DECIMAL_CODECOPTS
from test.utils_shared import (
    IMPOSSIBLE_WRITE_CONCERN,
    OvertCommandListener,
    wait_until,
)

from bson.codec_options import CodecOptions
from bson.dbref import DBRef
from bson.int64 import Int64
from bson.objectid import ObjectId
from bson.regex import Regex
from bson.son import SON
from pymongo import helpers_shared
from pymongo.errors import (
    CollectionInvalid,
    ExecutionTimeout,
    InvalidName,
    InvalidOperation,
    OperationFailure,
    WriteConcernError,
)
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.synchronous import auth
from pymongo.synchronous.collection import Collection
from pymongo.synchronous.database import Database
from pymongo.synchronous.helpers import next
from pymongo.synchronous.mongo_client import MongoClient
from pymongo.write_concern import WriteConcern

_IS_SYNC = True


class TestDatabaseNoConnect(unittest.TestCase):
    """Test Database features on a client that does not connect."""

    client: MongoClient

    @classmethod
    def setUpClass(cls):
        cls.client = MongoClient(connect=False)

    def test_name(self):
        self.assertRaises(TypeError, Database, self.client, 4)
        self.assertRaises(InvalidName, Database, self.client, "my db")
        self.assertRaises(InvalidName, Database, self.client, 'my"db')
        self.assertRaises(InvalidName, Database, self.client, "my\x00db")
        self.assertRaises(InvalidName, Database, self.client, "my\u0000db")
        self.assertEqual("name", Database(self.client, "name").name)

    def test_get_collection(self):
        codec_options = CodecOptions(tz_aware=True)
        write_concern = WriteConcern(w=2, j=True)
        read_concern = ReadConcern("majority")
        coll = self.client.pymongo_test.get_collection(
            "foo", codec_options, ReadPreference.SECONDARY, write_concern, read_concern
        )
        self.assertEqual("foo", coll.name)
        self.assertEqual(codec_options, coll.codec_options)
        self.assertEqual(ReadPreference.SECONDARY, coll.read_preference)
        self.assertEqual(write_concern, coll.write_concern)
        self.assertEqual(read_concern, coll.read_concern)

    def test_getattr(self):
        db = self.client.pymongo_test
        self.assertIsInstance(db["_does_not_exist"], Collection)

        with self.assertRaises(AttributeError) as context:
            db._does_not_exist

        # Message should be: "AttributeError: Database has no attribute
        # '_does_not_exist'. To access the _does_not_exist collection,
        # use database['_does_not_exist']".
        self.assertIn("has no attribute '_does_not_exist'", str(context.exception))

    def test_iteration(self):
        db = self.client.pymongo_test
        msg = "'Database' object is not iterable"
        # Iteration fails
        with self.assertRaisesRegex(TypeError, msg):
            for _ in db:  # type: ignore[misc] # error: "None" not callable  [misc]
                break
        # Index fails
        with self.assertRaises(TypeError):
            _ = db[0]
        # next fails
        with self.assertRaisesRegex(TypeError, "'Database' object is not iterable"):
            _ = next(db)
        # .next() fails
        with self.assertRaisesRegex(TypeError, "'Database' object is not iterable"):
            _ = db.next()
        # Do not implement typing.Iterable.
        self.assertNotIsInstance(db, Iterable)


class TestDatabase(IntegrationTest):
    def test_equality(self):
        self.assertNotEqual(Database(self.client, "test"), Database(self.client, "mike"))
        self.assertEqual(Database(self.client, "test"), Database(self.client, "test"))

        # Explicitly test inequality
        self.assertFalse(Database(self.client, "test") != Database(self.client, "test"))

    def test_hashable(self):
        self.assertIn(self.client.test, {Database(self.client, "test")})

    def test_get_coll(self):
        db = Database(self.client, "pymongo_test")
        self.assertEqual(db.test, db["test"])
        self.assertEqual(db.test, Collection(db, "test"))
        self.assertNotEqual(db.test, Collection(db, "mike"))
        self.assertEqual(db.test.mike, db["test.mike"])

    def test_repr(self):
        name = "Database"
        self.assertEqual(
            repr(Database(self.client, "pymongo_test")),
            "{}({!r}, {})".format(name, self.client, repr("pymongo_test")),
        )

    def test_create_collection(self):
        db = Database(self.client, "pymongo_test")

        db.test.insert_one({"hello": "world"})
        with self.assertRaises(CollectionInvalid):
            db.create_collection("test")

        db.drop_collection("test")

        with self.assertRaises(TypeError):
            db.create_collection(5)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            db.create_collection(None)  # type: ignore[arg-type]
        with self.assertRaises(InvalidName):
            db.create_collection("coll..ection")  # type: ignore[arg-type]

        test = db.create_collection("test")
        self.assertIn("test", db.list_collection_names())
        test.insert_one({"hello": "world"})
        self.assertEqual((db.test.find_one())["hello"], "world")

        db.drop_collection("test.foo")
        db.create_collection("test.foo")
        self.assertIn("test.foo", db.list_collection_names())
        with self.assertRaises(CollectionInvalid):
            db.create_collection("test.foo")

    def test_list_collection_names(self):
        db = Database(self.client, "pymongo_test")
        db.test.insert_one({"dummy": "object"})
        db.test.mike.insert_one({"dummy": "object"})

        colls = db.list_collection_names()
        self.assertIn("test", colls)
        self.assertIn("test.mike", colls)
        for coll in colls:
            self.assertNotIn("$", coll)

        db.systemcoll.test.insert_one({})
        no_system_collections = db.list_collection_names(
            filter={"name": {"$regex": r"^(?!system\.)"}}
        )
        for coll in no_system_collections:
            self.assertFalse(coll.startswith("system."))
        self.assertIn("systemcoll.test", no_system_collections)

        # Force more than one batch.
        db = self.client.many_collections
        for i in range(101):
            db["coll" + str(i)].insert_one({})
        # No Error
        try:
            db.list_collection_names()
        finally:
            self.client.drop_database("many_collections")

    def test_list_collection_names_filter(self):
        listener = OvertCommandListener()
        client = self.rs_or_single_client(event_listeners=[listener])
        db = client[self.db.name]
        db.capped.drop()
        db.create_collection("capped", capped=True, size=4096)
        db.capped.insert_one({})
        db.non_capped.insert_one({})
        self.addCleanup(client.drop_database, db.name)
        filter: Union[None, Mapping[str, Any]]
        # Should not send nameOnly.
        for filter in ({"options.capped": True}, {"options.capped": True, "name": "capped"}):
            listener.reset()
            names = db.list_collection_names(filter=filter)
            self.assertEqual(names, ["capped"])
            self.assertNotIn("nameOnly", listener.started_events[0].command)

        # Should send nameOnly (except on 2.6).
        for filter in (None, {}, {"name": {"$in": ["capped", "non_capped"]}}):
            listener.reset()
            names = db.list_collection_names(filter=filter)
            self.assertIn("capped", names)
            self.assertIn("non_capped", names)
            command = listener.started_events[0].command
            self.assertIn("nameOnly", command)
            self.assertTrue(command["nameOnly"])

    def test_check_exists(self):
        listener = OvertCommandListener()
        client = self.rs_or_single_client(event_listeners=[listener])
        db = client[self.db.name]
        db.drop_collection("unique")
        db.create_collection("unique", check_exists=True)
        self.assertIn("listCollections", listener.started_command_names())
        listener.reset()
        db.drop_collection("unique")
        db.create_collection("unique", check_exists=False)
        self.assertGreater(len(listener.started_events), 0)
        self.assertNotIn("listCollections", listener.started_command_names())

    def test_list_collections(self):
        self.client.drop_database("pymongo_test")
        db = Database(self.client, "pymongo_test")
        db.test.insert_one({"dummy": "object"})
        db.test.mike.insert_one({"dummy": "object"})

        results = db.list_collections()
        colls = [result["name"] for result in results]

        # All the collections present.
        self.assertIn("test", colls)
        self.assertIn("test.mike", colls)

        # No collection containing a '$'.
        for coll in colls:
            self.assertNotIn("$", coll)

        # Duplicate check.
        coll_cnt: dict = {}
        for coll in colls:
            try:
                # Found duplicate.
                coll_cnt[coll] += 1
                self.fail("Found duplicate")
            except KeyError:
                coll_cnt[coll] = 1
        coll_cnt: dict = {}

        # Check if there are any collections which don't exist.
        self.assertLessEqual(set(colls), {"test", "test.mike", "system.indexes"})

        colls = (db.list_collections(filter={"name": {"$regex": "^test$"}})).to_list()
        self.assertEqual(1, len(colls))

        colls = (db.list_collections(filter={"name": {"$regex": "^test.mike$"}})).to_list()
        self.assertEqual(1, len(colls))

        db.drop_collection("test")

        db.create_collection("test", capped=True, size=4096)
        results = db.list_collections(filter={"options.capped": True})
        colls = [result["name"] for result in results]

        # Checking only capped collections are present
        self.assertIn("test", colls)
        self.assertNotIn("test.mike", colls)

        # No collection containing a '$'.
        for coll in colls:
            self.assertNotIn("$", coll)

        # Duplicate check.
        coll_cnt = {}
        for coll in colls:
            try:
                # Found duplicate.
                coll_cnt[coll] += 1
                self.fail("Found duplicate")
            except KeyError:
                coll_cnt[coll] = 1
        coll_cnt = {}

        # Check if there are any collections which don't exist.
        self.assertLessEqual(set(colls), {"test", "system.indexes"})

        self.client.drop_database("pymongo_test")

    def test_list_collection_names_single_socket(self):
        client = self.rs_or_single_client(maxPoolSize=1)
        client.drop_database("test_collection_names_single_socket")
        db = client.test_collection_names_single_socket
        for i in range(200):
            db.create_collection(str(i))

        db.list_collection_names()  # Must not hang.
        client.drop_database("test_collection_names_single_socket")

    def test_drop_collection(self):
        db = Database(self.client, "pymongo_test")

        with self.assertRaises(TypeError):
            db.drop_collection(5)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            db.drop_collection(None)  # type: ignore[arg-type]

        db.test.insert_one({"dummy": "object"})
        self.assertIn("test", db.list_collection_names())
        db.drop_collection("test")
        self.assertNotIn("test", db.list_collection_names())

        db.test.insert_one({"dummy": "object"})
        self.assertIn("test", db.list_collection_names())
        db.drop_collection("test")
        self.assertNotIn("test", db.list_collection_names())

        db.test.insert_one({"dummy": "object"})
        self.assertIn("test", db.list_collection_names())
        db.drop_collection(db.test)
        self.assertNotIn("test", db.list_collection_names())

        db.test.insert_one({"dummy": "object"})
        self.assertIn("test", db.list_collection_names())
        db.test.drop()
        self.assertNotIn("test", db.list_collection_names())
        db.test.drop()

        db.drop_collection(db.test.doesnotexist)

        if client_context.is_rs:
            db_wc = Database(self.client, "pymongo_test", write_concern=IMPOSSIBLE_WRITE_CONCERN)
            with self.assertRaises(WriteConcernError):
                db_wc.drop_collection("test")

    def test_validate_collection(self):
        db = self.client.pymongo_test

        with self.assertRaises(TypeError):
            db.validate_collection(5)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            db.validate_collection(None)  # type: ignore[arg-type]

        db.test.insert_one({"dummy": "object"})

        with self.assertRaises(OperationFailure):
            db.validate_collection("test.doesnotexist")
        with self.assertRaises(OperationFailure):
            db.validate_collection(db.test.doesnotexist)

        self.assertTrue(db.validate_collection("test"))
        self.assertTrue(db.validate_collection(db.test))
        self.assertTrue(db.validate_collection(db.test, full=True))
        self.assertTrue(db.validate_collection(db.test, scandata=True))
        self.assertTrue(db.validate_collection(db.test, scandata=True, full=True))
        self.assertTrue(db.validate_collection(db.test, True, True))

    @client_context.require_version_min(4, 3, 3)
    @client_context.require_no_standalone
    def test_validate_collection_background(self):
        db = self.client.pymongo_test.with_options(write_concern=WriteConcern(w="majority"))
        db.test.insert_one({"dummy": "object"})
        coll = db.test
        self.assertTrue(db.validate_collection(coll, background=False))
        # The inMemory storage engine does not support background=True.
        if client_context.storage_engine != "inMemory":
            # background=True requires the collection exist in a checkpoint.
            self.client.admin.command("fsync")
            self.assertTrue(db.validate_collection(coll, background=True))
            self.assertTrue(db.validate_collection(coll, scandata=True, background=True))
            # The server does not support background=True with full=True.
            # Assert that we actually send the background option by checking
            # that this combination fails.
            with self.assertRaises(OperationFailure):
                db.validate_collection(coll, full=True, background=True)

    def test_command(self):
        self.maxDiff = None
        db = self.client.admin
        first = db.command("buildinfo")
        second = db.command({"buildinfo": 1})
        third = db.command("buildinfo", 1)
        self.assertEqualReply(first, second)
        self.assertEqualReply(second, third)

    # We use 'aggregate' as our example command, since it's an easy way to
    # retrieve a BSON regex from a collection using a command.
    def test_command_with_regex(self):
        db = self.client.pymongo_test
        db.test.drop()
        db.test.insert_one({"r": re.compile(".*")})
        db.test.insert_one({"r": Regex(".*")})

        result = db.command("aggregate", "test", pipeline=[], cursor={})
        for doc in result["cursor"]["firstBatch"]:
            self.assertIsInstance(doc["r"], Regex)

    def test_command_bulkWrite(self):
        # Ensure bulk write commands can be run directly via db.command().
        if client_context.version.at_least(8, 0):
            self.client.admin.command(
                {
                    "bulkWrite": 1,
                    "nsInfo": [{"ns": self.db.test.full_name}],
                    "ops": [{"insert": 0, "document": {}}],
                }
            )
        self.db.command({"insert": "test", "documents": [{}]})
        self.db.command({"update": "test", "updates": [{"q": {}, "u": {"$set": {"x": 1}}}]})
        self.db.command({"delete": "test", "deletes": [{"q": {}, "limit": 1}]})
        self.db.test.drop()

    def test_cursor_command(self):
        db = self.client.pymongo_test
        db.test.drop()

        docs = [{"_id": i, "doc": i} for i in range(3)]
        db.test.insert_many(docs)

        cursor = db.cursor_command("find", "test")

        self.assertIsInstance(cursor, CommandCursor)

        result_docs = cursor.to_list()
        self.assertEqual(docs, result_docs)

    def test_cursor_command_invalid(self):
        with self.assertRaises(InvalidOperation):
            self.db.cursor_command("usersInfo", "test")

    @client_context.require_no_fips
    def test_password_digest(self):
        with self.assertRaises(TypeError):
            auth._password_digest(5)  # type: ignore[arg-type, call-arg]
        with self.assertRaises(TypeError):
            auth._password_digest(True)  # type: ignore[arg-type, call-arg]
        with self.assertRaises(TypeError):
            auth._password_digest(None)  # type: ignore[arg-type, call-arg]

        self.assertIsInstance(auth._password_digest("mike", "password"), str)
        self.assertEqual(
            auth._password_digest("mike", "password"), "cd7e45b3b2767dc2fa9b6b548457ed00"
        )
        self.assertEqual(
            auth._password_digest("Gustave", "Dor\xe9"), "81e0e2364499209f466e75926a162d73"
        )

    def test_id_ordering(self):
        # PyMongo attempts to have _id show up first
        # when you iterate key/value pairs in a document.
        # This isn't reliable since python dicts don't
        # guarantee any particular order. This will never
        # work right in Jython or any Python or environment
        # with hash randomization enabled (e.g. tox).
        db = self.client.pymongo_test
        db.test.drop()
        db.test.insert_one(SON([("hello", "world"), ("_id", 5)]))

        db = self.client.get_database(
            "pymongo_test", codec_options=CodecOptions(document_class=SON[str, Any])
        )
        cursor = db.test.find()
        for x in cursor:
            for k, _v in x.items():
                self.assertEqual(k, "_id")
                break

    def test_deref(self):
        db = self.client.pymongo_test
        db.test.drop()

        with self.assertRaises(TypeError):
            db.dereference(5)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            db.dereference("hello")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            db.dereference(None)  # type: ignore[arg-type]

        self.assertEqual(None, db.dereference(DBRef("test", ObjectId())))
        obj: dict[str, Any] = {"x": True}
        key = (db.test.insert_one(obj)).inserted_id
        self.assertEqual(obj, db.dereference(DBRef("test", key)))
        self.assertEqual(obj, db.dereference(DBRef("test", key, "pymongo_test")))
        with self.assertRaises(ValueError):
            db.dereference(DBRef("test", key, "foo"))

        self.assertEqual(None, db.dereference(DBRef("test", 4)))
        obj = {"_id": 4}
        db.test.insert_one(obj)
        self.assertEqual(obj, db.dereference(DBRef("test", 4)))

    def test_deref_kwargs(self):
        db = self.client.pymongo_test
        db.test.drop()

        db.test.insert_one({"_id": 4, "foo": "bar"})
        db = self.client.get_database(
            "pymongo_test", codec_options=CodecOptions(document_class=SON[str, Any])
        )
        self.assertEqual(
            SON([("foo", "bar")]), db.dereference(DBRef("test", 4), projection={"_id": False})
        )

    # TODO some of these tests belong in the collection level testing.
    def test_insert_find_one(self):
        db = self.client.pymongo_test
        db.test.drop()

        a_doc = SON({"hello": "world"})
        a_key = (db.test.insert_one(a_doc)).inserted_id
        self.assertIsInstance(a_doc["_id"], ObjectId)
        self.assertEqual(a_doc["_id"], a_key)
        self.assertEqual(a_doc, db.test.find_one({"_id": a_doc["_id"]}))
        self.assertEqual(a_doc, db.test.find_one(a_key))
        self.assertEqual(None, db.test.find_one(ObjectId()))
        self.assertEqual(a_doc, db.test.find_one({"hello": "world"}))
        self.assertEqual(None, db.test.find_one({"hello": "test"}))

        b = db.test.find_one()
        assert b is not None
        b["hello"] = "mike"
        db.test.replace_one({"_id": b["_id"]}, b)

        self.assertNotEqual(a_doc, db.test.find_one(a_key))
        self.assertEqual(b, db.test.find_one(a_key))
        self.assertEqual(b, db.test.find_one())

        count = 0
        for _ in db.test.find():
            count += 1
        self.assertEqual(count, 1)

    def test_long(self):
        db = self.client.pymongo_test
        db.test.drop()
        db.test.insert_one({"x": 9223372036854775807})
        retrieved = (db.test.find_one())["x"]
        self.assertEqual(Int64(9223372036854775807), retrieved)
        self.assertIsInstance(retrieved, Int64)
        db.test.delete_many({})
        db.test.insert_one({"x": Int64(1)})
        retrieved = (db.test.find_one())["x"]
        self.assertEqual(Int64(1), retrieved)
        self.assertIsInstance(retrieved, Int64)

    def test_delete(self):
        db = self.client.pymongo_test
        db.test.drop()

        db.test.insert_one({"x": 1})
        db.test.insert_one({"x": 2})
        db.test.insert_one({"x": 3})
        length = 0
        for _ in db.test.find():
            length += 1
        self.assertEqual(length, 3)

        db.test.delete_one({"x": 1})
        length = 0
        for _ in db.test.find():
            length += 1
        self.assertEqual(length, 2)

        db.test.delete_one(db.test.find_one())  # type: ignore[arg-type]
        db.test.delete_one(db.test.find_one())  # type: ignore[arg-type]
        self.assertEqual(db.test.find_one(), None)

        db.test.insert_one({"x": 1})
        db.test.insert_one({"x": 2})
        db.test.insert_one({"x": 3})

        self.assertTrue(db.test.find_one({"x": 2}))
        db.test.delete_one({"x": 2})
        self.assertFalse(db.test.find_one({"x": 2}))

        self.assertTrue(db.test.find_one())
        db.test.delete_many({})
        self.assertFalse(db.test.find_one())

    def test_command_response_without_ok(self):
        # Sometimes (SERVER-10891) the server's response to a badly-formatted
        # command document will have no 'ok' field. We should raise
        # OperationFailure instead of KeyError.
        with self.assertRaises(OperationFailure):
            helpers_shared._check_command_response({}, None)

        try:
            helpers_shared._check_command_response({"$err": "foo"}, None)
        except OperationFailure as e:
            self.assertEqual(e.args[0], "foo, full error: {'$err': 'foo'}")
        else:
            self.fail("_check_command_response didn't raise OperationFailure")

    def test_mongos_response(self):
        error_document = {
            "ok": 0,
            "errmsg": "outer",
            "raw": {"shard0/host0,host1": {"ok": 0, "errmsg": "inner"}},
        }

        with self.assertRaises(OperationFailure) as context:
            helpers_shared._check_command_response(error_document, None)

        self.assertIn("inner", str(context.exception))

        # If a shard has no primary and you run a command like dbstats, which
        # cannot be run on a secondary, mongos's response includes empty "raw"
        # errors. See SERVER-15428.
        error_document = {"ok": 0, "errmsg": "outer", "raw": {"shard0/host0,host1": {}}}

        with self.assertRaises(OperationFailure) as context:
            helpers_shared._check_command_response(error_document, None)

        self.assertIn("outer", str(context.exception))

        # Raw error has ok: 0 but no errmsg. Not a known case, but test it.
        error_document = {"ok": 0, "errmsg": "outer", "raw": {"shard0/host0,host1": {"ok": 0}}}

        with self.assertRaises(OperationFailure) as context:
            helpers_shared._check_command_response(error_document, None)

        self.assertIn("outer", str(context.exception))

    @client_context.require_test_commands
    @client_context.require_no_mongos
    def test_command_max_time_ms(self):
        self.client.admin.command("configureFailPoint", "maxTimeAlwaysTimeOut", mode="alwaysOn")
        try:
            db = self.client.pymongo_test
            db.command("count", "test")
            with self.assertRaises(ExecutionTimeout):
                db.command("count", "test", maxTimeMS=1)
            pipeline = [{"$project": {"name": 1, "count": 1}}]
            # Database command helper.
            db.command("aggregate", "test", pipeline=pipeline, cursor={})
            with self.assertRaises(ExecutionTimeout):
                db.command(
                    "aggregate",
                    "test",
                    pipeline=pipeline,
                    cursor={},
                    maxTimeMS=1,
                )
            # Collection helper.
            db.test.aggregate(pipeline=pipeline)
            with self.assertRaises(ExecutionTimeout):
                db.test.aggregate(pipeline, maxTimeMS=1)
        finally:
            self.client.admin.command("configureFailPoint", "maxTimeAlwaysTimeOut", mode="off")

    def test_with_options(self):
        codec_options = DECIMAL_CODECOPTS
        read_preference = ReadPreference.SECONDARY_PREFERRED
        write_concern = WriteConcern(j=True)
        read_concern = ReadConcern(level="majority")

        # List of all options to compare.
        allopts = [
            "name",
            "client",
            "codec_options",
            "read_preference",
            "write_concern",
            "read_concern",
        ]

        db1 = self.client.get_database(
            "with_options_test",
            codec_options=codec_options,
            read_preference=read_preference,
            write_concern=write_concern,
            read_concern=read_concern,
        )

        # Case 1: swap no options
        db2 = db1.with_options()
        for opt in allopts:
            self.assertEqual(getattr(db1, opt), getattr(db2, opt))

        # Case 2: swap all options
        newopts = {
            "codec_options": CodecOptions(),
            "read_preference": ReadPreference.PRIMARY,
            "write_concern": WriteConcern(w=1),
            "read_concern": ReadConcern(level="local"),
        }
        db2 = db1.with_options(**newopts)  # type: ignore[arg-type, call-overload]
        for opt in newopts:
            self.assertEqual(getattr(db2, opt), newopts.get(opt, getattr(db1, opt)))


class TestDatabaseAggregation(IntegrationTest):
    def setUp(self):
        super().setUp()
        self.pipeline: List[Mapping[str, Any]] = [
            {"$listLocalSessions": {}},
            {"$limit": 1},
            {"$addFields": {"dummy": "dummy field"}},
            {"$project": {"_id": 0, "dummy": 1}},
        ]
        self.result = {"dummy": "dummy field"}
        self.admin = self.client.admin

    def test_database_aggregation(self):
        with self.admin.aggregate(self.pipeline) as cursor:
            result = next(cursor)
            self.assertEqual(result, self.result)

    @client_context.require_no_mongos
    def test_database_aggregation_fake_cursor(self):
        coll_name = "test_output"
        write_stage: dict
        if client_context.version < (4, 3):
            db_name = "admin"
            write_stage = {"$out": coll_name}
        else:
            # SERVER-43287 disallows writing with $out to the admin db, use
            # $merge instead.
            db_name = "pymongo_test"
            write_stage = {"$merge": {"into": {"db": db_name, "coll": coll_name}}}
        output_coll = self.client[db_name][coll_name]
        output_coll.drop()
        self.addCleanup(output_coll.drop)

        admin = self.admin.with_options(write_concern=WriteConcern(w=0))
        pipeline = self.pipeline[:]
        pipeline.append(write_stage)
        with admin.aggregate(pipeline) as cursor:
            with self.assertRaises(StopIteration):
                next(cursor)

        def lambda_fn():
            return output_coll.find_one()

        result = wait_until(lambda_fn, "read unacknowledged write")
        self.assertEqual(result["dummy"], self.result["dummy"])

    def test_bool(self):
        with self.assertRaises(NotImplementedError):
            bool(Database(self.client, "test"))


if __name__ == "__main__":
    unittest.main()
