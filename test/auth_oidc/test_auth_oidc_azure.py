# Copyright 2023-present MongoDB, Inc.
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

"""Test MONGODB-OIDC Authentication."""

import os
import sys
import unittest

sys.path[0:0] = [""]

from pymongo import MongoClient
from pymongo.auth_oidc import _CACHE as _oidc_cache
from pymongo.azure_helpers import _CACHE


class TestAuthOIDCAzure(unittest.TestCase):
    uri: str

    @classmethod
    def setUpClass(cls):
        cls.uri = os.environ["MONGODB_URI"]

    def setUp(self):
        _oidc_cache.clear()
        _CACHE.clear()

    def test_connect(self):
        client = MongoClient(self.uri)
        client.test.test.find_one()
        client.close()

    def test_connect_allowed_hosts_ignored(self):
        client = MongoClient(self.uri)
        client.test.test.find_one()
        client.close()

    def test_main_cache_is_not_used(self):
        # Create a new client using the AZURE device workflow.
        # Ensure that a ``find`` operation does not add credentials to the cache.
        client = MongoClient(self.uri)
        client.test.test.find_one()
        client.close()

        # Ensure that the cache has been cleared.
        authenticator = list(_oidc_cache.values())[0]
        self.assertIsNone(authenticator.idp_info)

    def test_azure_cache_is_used(self):
        # Create a new client using the AZURE device workflow.
        # Ensure that a ``find`` operation does not add credentials to the cache.
        client = MongoClient(self.uri)
        client.test.test.find_one()
        client.close()

        assert len(_CACHE) == 1


if __name__ == "__main__":
    unittest.main()