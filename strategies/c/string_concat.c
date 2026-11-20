// SPDX-License-Identifier: MIT OR Apache-2.0
//
// string-concat calls open(root + "/" + path) and does nothing else. It is the
// baseline: the simplest open, and the negative control for the escape
// fixtures.
//
// State: the root's path, as given. Nothing is opened in advance.

#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>

#include "strategy.h"

static void *prepare(const char *root) {
    return (void *)root;
}

static int open_concat(void *state, const char *path, int flags, const char **label) {
    (void)label;
    char *full = join(state, path);
    if (full == NULL) {
        return -1;
    }
    int fd = open(full, flags, OPEN_MODE);
    int e = errno;
    free(full);
    errno = e;
    return fd;
}

const struct strategy string_concat = {
    .name = "string-concat",
    .semantics = BENEATH,
    .unavailable = NULL,
    .prepare = prepare,
    .open = open_concat,
    .release = NULL,
};
