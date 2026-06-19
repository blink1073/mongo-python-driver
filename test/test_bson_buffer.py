"""Regression tests for PYTHON-3449: buffer.c rewritten in terms of PyByteArray.

Each test maps to a specific memory safety concern identified during the spike.
"""

from __future__ import annotations

import threading

import pytest

import bson

pytestmark = pytest.mark.default
from bson import DEFAULT_CODEC_OPTIONS, _dict_to_bson

_requires_c_ext = pytest.mark.skipif(not bson.has_c(), reason="C extension not available")


class TestResizeWithNestedBackfill:
    """Resize + backfill correctness (stale-pointer hazard).

    Encodes a deeply nested document that forces multiple buffer growths and
    exercises the save_space + backfill pattern (the document length field is
    written at a saved offset *after* all child elements are encoded). If the
    PyByteArray pointer were stale after a resize, the backfilled length would
    be wrong and decode() would raise or return corrupt data.
    """

    def test_deep_nesting_round_trip(self):
        # ~300 bytes per level forces a resize before the outer doc length is backfilled
        doc: dict = {"a" * 50: "b" * 250}
        for _ in range(20):
            doc = {"nested": doc}
        encoded = bson.encode(doc)
        assert bson.decode(encoded) == doc

    def test_wide_document_round_trip(self):
        # Many keys causes many save_space + backfill cycles
        doc = {str(i): "x" * 100 for i in range(100)}
        encoded = bson.encode(doc)
        assert bson.decode(encoded) == doc


class TestSequentialEncodesNoCorruption:
    """Many sequential encodes (regression for use-after-free / double-free).

    Allocator reuse of freed memory would surface as a corrupt decode if a
    use-after-free existed in pymongo_buffer_finish or pymongo_buffer_free.
    """

    def test_varying_sizes(self):
        docs = [{"i": i, "data": "x" * (i % 500)} for i in range(2000)]
        results = [bson.encode(d) for d in docs]
        for doc, enc in zip(docs, results):
            assert bson.decode(enc) == doc


class TestConcurrentEncoding:
    """Concurrent encoding (free-threaded Python / Py_BEGIN_CRITICAL_SECTION).

    Each thread owns its own buffer, so a race would show up as a corrupt
    result rather than a crash/deadlock.
    """

    def test_concurrent_threads(self):
        doc = {"k": "v" * 100, "nested": {"a": list(range(50))}}
        expected = bson.encode(doc)
        errors: list[bytes] = []

        def encode_and_check():
            for _ in range(500):
                result = bson.encode(doc)
                if result != expected:
                    errors.append(result)

        threads = [threading.Thread(target=encode_and_check) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"Got {len(errors)} corrupt result(s)"


class TestPublicAPIReturnTypes:
    """bson.encode() must return bytes; _dict_to_bson() must return bytearray.

    Guards the bytes() wrapper in bson/__init__.py from being accidentally
    removed, and confirms the internal function exposes bytearray.
    """

    def test_encode_returns_bytes(self):
        result = bson.encode({"x": 1})
        assert type(result) is bytes, f"expected bytes, got {type(result)}"

    @_requires_c_ext
    def test_dict_to_bson_returns_bytearray(self):
        result = _dict_to_bson({"x": 1}, False, DEFAULT_CODEC_OPTIONS)
        assert type(result) is bytearray, f"expected bytearray, got {type(result)}"  # type: ignore[comparison-overlap]

    def test_encode_and_dict_to_bson_agree(self):
        doc = {"a": 1, "b": "hello"}
        assert bson.encode(doc) == bytes(_dict_to_bson(doc, False, DEFAULT_CODEC_OPTIONS))


class TestIsValidAcceptsBytearray:
    """bson.is_valid() must accept bytearray after the isinstance fix."""

    @_requires_c_ext
    def test_is_valid_accepts_bytearray(self):
        ba = _dict_to_bson({"x": 1}, False, DEFAULT_CODEC_OPTIONS)
        assert isinstance(ba, bytearray)
        assert bson.is_valid(ba)

    def test_is_valid_rejects_non_bytes(self):
        with pytest.raises(TypeError):
            bson.is_valid("not bytes")  # type: ignore[arg-type]


class TestEncodeFailureNoLeak:
    """Encoding failures must not leak memory (error-path safety).

    Peak memory must stay bounded after repeated failures. A growing peak
    indicates a leak in the C buffer error path.
    """

    def test_no_leak_on_repeated_failures(self):
        tracemalloc = pytest.importorskip("tracemalloc")
        tracemalloc.start()
        for _ in range(1000):
            with pytest.raises(Exception):  # noqa: B017
                bson.encode({1: "non-string key"})  # type: ignore[arg-type,dict-item]
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert peak < 5 * 1024 * 1024, f"peak memory {peak} bytes exceeds 5 MiB"
