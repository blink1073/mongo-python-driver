# Copyright 2021-present MongoDB, Inc.
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

"""Test the CRUD unified spec tests."""
from __future__ import annotations

import os
import pathlib
import sys

sys.path[0:0] = [""]

from test import unittest
from test.asynchronous.unified_format import generate_test_classes

_IS_SYNC = False

# Location of JSON test specifications.
if _IS_SYNC:
    _TEST_PATH = os.path.join(pathlib.Path(__file__).resolve().parent, "crud", "unified")
else:
    _TEST_PATH = os.path.join(pathlib.Path(__file__).resolve().parent.parent, "crud", "unified")

# Generate unified tests.
globals().update(generate_test_classes(_TEST_PATH, module=__name__))

if __name__ == "__main__":
    unittest.main()
