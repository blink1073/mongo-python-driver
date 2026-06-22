/*
 * Copyright 2009-2015 MongoDB, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "bson-endian.h"
#include "buffer.h"

/* Py_BEGIN_CRITICAL_SECTION / Py_END_CRITICAL_SECTION were introduced in
 * Python 3.13 for free-threaded builds. Provide no-op fallbacks so the code
 * compiles unchanged on older CPython and PyPy. */
#ifndef Py_BEGIN_CRITICAL_SECTION
#  define Py_BEGIN_CRITICAL_SECTION(op)
#  define Py_END_CRITICAL_SECTION()
#endif

#define INITIAL_BUFFER_SIZE 256

/* buffer_t is backed by a PyByteArray. The buffer_t struct is the sole owner
 * of the bytearray (refcount == 1) from pymongo_buffer_new until
 * pymongo_buffer_finish or pymongo_buffer_free. This single-owner invariant
 * means no other Python thread can hold a reference, so Py_BEGIN_CRITICAL_SECTION
 * calls below are always uncontended but are required for correctness when the
 * module runs without the GIL (Py_MOD_GIL_NOT_USED).
 *
 * ptr and capacity mirror PyByteArray_AS_STRING / PyByteArray_GET_SIZE and are
 * updated after every resize. Caching them avoids an extra pointer dereference
 * through the PyObject header on every write, recovering the hot-path cost to
 * parity with the old malloc-backed implementation. ptr is valid to use
 * inside a critical section because no other thread can hold a reference to the
 * bytearray (sole-owner invariant), so nothing external can trigger a resize
 * between our capacity check and our write. */
struct buffer {
    PyObject* bytearray;  /* sole owner (refcount==1) from new until finish/free */
    char* ptr;            /* PyByteArray_AS_STRING(bytearray); updated after every resize */
    int capacity;         /* logical size of bytearray; updated after every resize */
    int position;
};

/* Allocate and return a new buffer.
 * Return NULL and sets MemoryError on allocation failure. */
buffer_t pymongo_buffer_new(void) {
    buffer_t buffer = (buffer_t)malloc(sizeof(struct buffer));
    if (buffer == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    /* Use an empty bytearray + Resize instead of FromStringAndSize(NULL, N) to
     * avoid zero-initialising the initial buffer (Resize uses realloc; it does
     * not zero new bytes beyond the NUL terminator). */
    buffer->bytearray = PyByteArray_FromStringAndSize("", 0);
    if (buffer->bytearray == NULL) {
        free(buffer);
        return NULL;
    }
    if (PyByteArray_Resize(buffer->bytearray, INITIAL_BUFFER_SIZE) < 0) {
        Py_DECREF(buffer->bytearray);
        free(buffer);
        return NULL;
    }
    buffer->ptr = PyByteArray_AS_STRING(buffer->bytearray);
    buffer->capacity = INITIAL_BUFFER_SIZE;
    buffer->position = 0;
    return buffer;
}

/* Error path: discard `buffer` without returning data.
 * Call when encoding fails before reaching pymongo_buffer_finish.
 * Return non-zero on failure. */
int pymongo_buffer_free(buffer_t buffer) {
    if (buffer == NULL) {
        return 1;
    }
    Py_CLEAR(buffer->bytearray);
    free(buffer);
    return 0;
}

/* Grow `buffer` to at least `min_length` bytes.
 * Returns non-zero and sets MemoryError on failure.
 * On failure, buffer->bytearray is cleared (set to NULL). */
static int buffer_grow(buffer_t buffer, int min_length) {
    int size, old_size, result;

    if (buffer->bytearray == NULL) {
        PyErr_NoMemory();
        return 1;
    }

    size = buffer->capacity;
    if (size >= min_length) {
        return 0;
    }

    while (size < min_length) {
        old_size = size;
        size *= 2;
        if (size <= old_size) {
           /* Size did not increase. Could be an overflow
            * or size < 1. Just go with min_length. */
           size = min_length;
        }
    }

    /* See struct comment for why the per-object lock is required here.
     * Update cached ptr and capacity under the same lock so they stay in sync. */
    Py_BEGIN_CRITICAL_SECTION(buffer->bytearray);
    result = PyByteArray_Resize(buffer->bytearray, size);
    if (result == 0) {
        buffer->ptr = PyByteArray_AS_STRING(buffer->bytearray);
        buffer->capacity = size;
    }
    Py_END_CRITICAL_SECTION();

    if (result < 0) {
        Py_CLEAR(buffer->bytearray);
        return 1;
    }
    return 0;
}

/* Assure that `buffer` has at least `size` free bytes (and grow if needed).
 * Return non-zero and sets MemoryError on allocation failure.
 * Return non-zero and sets ValueError if `size` would exceed 2GiB. */
static int buffer_assure_space(buffer_t buffer, int size) {
    int new_size = buffer->position + size;
    /* Check for overflow. */
    if (new_size < buffer->position) {
        PyErr_SetString(PyExc_ValueError,
                        "Document would overflow BSON size limit");
        return 1;
    }
    /* Hot path: use cached capacity — no dereference through PyObject header. */
    if (new_size <= buffer->capacity) {
        return 0;
    }
    return buffer_grow(buffer, new_size);
}

/* Save `size` bytes from the current position in `buffer` (and grow if needed).
 * Return offset for writing, or -1 on failure.
 * Sets MemoryError or ValueError on failure. */
buffer_position pymongo_buffer_save_space(buffer_t buffer, int size) {
    int position = buffer->position;
    if (buffer_assure_space(buffer, size) != 0) {
        return -1;
    }
    buffer->position += size;
    return position;
}

/* Write `size` bytes from `data` to `buffer` (and grow if needed).
 * Return non-zero on failure.
 * Sets MemoryError or ValueError on failure. */
int pymongo_buffer_write(buffer_t buffer, const char* data, int size) {
    if (buffer_assure_space(buffer, size) != 0) {
        return 1;
    }
    /* See struct comment for why the per-object lock is required here.
     * buffer->ptr is valid: capacity check above guarantees no resize since
     * we last updated ptr in buffer_grow. */
    Py_BEGIN_CRITICAL_SECTION(buffer->bytearray);
    memcpy(buffer->ptr + buffer->position, data, size);
    Py_END_CRITICAL_SECTION();
    buffer->position += size;
    return 0;
}

void pymongo_buffer_write_byte_at(buffer_t buffer, buffer_position pos, char byte) {
    /* See struct comment for why the per-object lock is required here.
     * pos was reserved by pymongo_buffer_save_space; no resize occurs. */
    Py_BEGIN_CRITICAL_SECTION(buffer->bytearray);
    buffer->ptr[pos] = byte;
    Py_END_CRITICAL_SECTION();
}

void pymongo_buffer_write_int32_at(buffer_t buffer, buffer_position pos, int32_t data) {
    uint32_t data_le = BSON_UINT32_TO_LE(data);
    /* See struct comment for why the per-object lock is required here. */
    Py_BEGIN_CRITICAL_SECTION(buffer->bytearray);
    memcpy(buffer->ptr + pos, &data_le, 4);
    Py_END_CRITICAL_SECTION();
}

int pymongo_buffer_get_position(buffer_t buffer) {
    return buffer->position;
}

void pymongo_buffer_update_position(buffer_t buffer, buffer_position new_position) {
    buffer->position = new_position;
}

/* Success path: trim the buffer to the bytes written, return the underlying
 * PyByteArray, and free the buffer_t struct. Steals the reference — caller
 * owns the object. Return NULL on failure (OOM during trim). */
PyObject* pymongo_buffer_finish(buffer_t buffer) {
    int result;
    PyObject* ba;
    assert(buffer->bytearray != NULL);

    /* See struct comment for why the per-object lock is required here. */
    Py_BEGIN_CRITICAL_SECTION(buffer->bytearray);
    result = PyByteArray_Resize(buffer->bytearray, buffer->position);
    Py_END_CRITICAL_SECTION();

    if (result < 0) {
        Py_CLEAR(buffer->bytearray);
        free(buffer);
        return NULL;
    }

    ba = buffer->bytearray;
    buffer->bytearray = NULL;
    free(buffer);
    return ba;
}
