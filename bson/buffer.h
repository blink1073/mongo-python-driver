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

#ifndef BUFFER_H
#define BUFFER_H

#define PY_SSIZE_T_CLEAN
#include "Python.h"

/* Note: if any of these functions return a failure condition then the buffer
 * has already been freed. */

/* A buffer */
typedef struct buffer* buffer_t;
/* A position in the buffer */
typedef int buffer_position;

/* Allocate and return a new buffer.
 * Return NULL on allocation failure. */
buffer_t pymongo_buffer_new(void);

/* Error path: discard `buffer` without returning data.
 * Call when encoding fails before reaching pymongo_buffer_finish.
 * Return non-zero on failure. */
int pymongo_buffer_free(buffer_t buffer);

/* Save `size` bytes from the current position in `buffer` (and grow if needed).
 * Return offset for writing, or -1 on allocation failure. */
buffer_position pymongo_buffer_save_space(buffer_t buffer, int size);

/* Write `size` bytes from `data` to `buffer` (and grow if needed).
 * Return non-zero on allocation failure. */
int pymongo_buffer_write(buffer_t buffer, const char* data, int size);

/* Write a single byte to `buffer` at `pos`.
 * `pos` must have been reserved with pymongo_buffer_save_space; no resize occurs. */
void pymongo_buffer_write_byte_at(buffer_t buffer, buffer_position pos, char byte);

/* Write a little-endian int32 to `buffer` at `pos`.
 * `pos` must have been reserved with pymongo_buffer_save_space; no resize occurs. */
void pymongo_buffer_write_int32_at(buffer_t buffer, buffer_position pos, int32_t data);

/* Return the number of bytes written so far. */
buffer_position pymongo_buffer_get_position(buffer_t buffer);

/* Roll the write cursor back to `position`, discarding everything written
 * after it. It is used to undo a speculative document write when a batch
 * overflows the max message size. `position` must have been obtained from
 * a prior call to pymongo_buffer_get_position. */
void pymongo_buffer_rollback(buffer_t buffer, buffer_position position);

/* For debugging only; returns a borrowed reference; the buffer remains the owner. */
PyObject* pymongo_buffer_get_bytearray(buffer_t buffer);

/* Success path: trim the buffer to the bytes written, return the underlying
 * PyByteArray, and free the buffer_t struct. Steals the reference — caller
 * owns the object. Return NULL on failure (OOM during trim). */
PyObject* pymongo_buffer_finish(buffer_t buffer);

#endif
