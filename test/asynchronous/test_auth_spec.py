# Copyright 2018-present MongoDB, Inc.
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

"""Run the auth spec tests."""
from __future__ import annotations

import glob
import json
import os
import sys
import warnings
from test.asynchronous import AsyncPyMongoTestCase

import pytest

sys.path[0:0] = [""]

from test import unittest
from test.asynchronous.unified_format import generate_test_classes

from pymongo import AsyncMongoClient
from pymongo.auth_oidc_shared import OIDCCallback

pytestmark = pytest.mark.auth

_IS_SYNC = False

_TEST_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "auth")


class TestAuthSpec(AsyncPyMongoTestCase):
    pass


class SampleHumanCallback(OIDCCallback):
    def fetch(self, context):
        pass


def create_test(test_case):
    def run_test(self):
        uri = test_case["uri"]
        valid = test_case["valid"]
        credential = test_case.get("credential")

        if not valid:
            with warnings.catch_warnings():
                warnings.simplefilter("default")
                self.assertRaises(Exception, AsyncMongoClient, uri, connect=False)
        else:
            client = self.simple_client(uri, connect=False)
            credentials = client.options.pool_options._credentials
            if credential is None:
                self.assertIsNone(credentials)
            else:
                self.assertIsNotNone(credentials)
                self.assertEqual(credentials.username, credential["username"])
                self.assertEqual(credentials.password, credential["password"])
                self.assertEqual(credentials.source, credential["source"])
                if credential["mechanism"] is not None:
                    self.assertEqual(credentials.mechanism, credential["mechanism"])
                else:
                    self.assertEqual(credentials.mechanism, "DEFAULT")
                expected = credential["mechanism_properties"]
                if expected is not None:
                    actual = credentials.mechanism_properties
                    for key, value in expected.items():
                        self.assertEqual(getattr(actual, key.lower()), value)
                else:
                    if credential["mechanism"] == "MONGODB-AWS":
                        self.assertIsNone(credentials.mechanism_properties.aws_session_token)
                    else:
                        self.assertIsNone(credentials.mechanism_properties)

    return run_test


def create_tests():
    for filename in glob.glob(os.path.join(_TEST_PATH, "legacy", "*.json")):
        test_suffix, _ = os.path.splitext(os.path.basename(filename))
        with open(filename) as auth_tests:
            test_cases = json.load(auth_tests)["tests"]
            for test_case in test_cases:
                if test_case.get("optional", False):
                    continue
                test_method = create_test(test_case)
                name = str(test_case["description"].lower().replace(" ", "_"))
                setattr(TestAuthSpec, f"test_{test_suffix}_{name}", test_method)


create_tests()


globals().update(
    generate_test_classes(
        os.path.join(_TEST_PATH, "unified"),
        module=__name__,
    )
)

if __name__ == "__main__":
    unittest.main()
