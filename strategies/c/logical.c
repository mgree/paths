// SPDX-License-Identifier: MIT OR Apache-2.0
//
// logical: resolve the request as a string, the way `cd -L` treats `..`:
// drop `.` and empty components, let `..` cancel the component before it,
// refuse anything that would climb above the root or is absolute, then open
// root + "/" + the cleaned path. It ignores symlinks, and it changes what some
// paths mean: missing/../x becomes x, and a trailing slash disappears.
//
// State: the root's path, as given. Nothing is opened in advance.

#define _POSIX_C_SOURCE 200809L // for strdup, strtok_r and stpcpy

#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>

#include "strategy.h"

static void *prepare(const char *root) {
    return (void *)root;
}

static int open_logical(void *state, const char *path, int flags, const char **label) {
    if (path[0] == '/') {
        *label = LABEL_ESCAPE;
        return -1;
    }
    const char *root = state;
    char *copy = strdup(path); // strtok_r writes into what it splits
    char **parts = malloc(sizeof *parts * (strlen(path) / 2 + 1));
    char *full = malloc(strlen(root) + 1 + strlen(path) + 1); // worst case avoids `..` arithmetic
    int fd = -1, e = ENOMEM;
    if (copy == NULL || parts == NULL || full == NULL) {
        goto out;
    }
    // Split the request at each '/'. strtok_r skips empty components, which is
    // also why a trailing slash disappears.
    size_t depth = 0;
    char *save;
    for (char *part = strtok_r(copy, "/", &save); part != NULL; part = strtok_r(NULL, "/", &save)) {
        if (strcmp(part, "..") == 0) {
            if (depth == 0) {
                *label = LABEL_ESCAPE;
                goto out;
            }
            depth--;
        } else if (strcmp(part, ".") != 0) {
            parts[depth++] = part;
        }
    }
    // Open root + "/" + the kept components.
    char *end = stpcpy(full, root);
    for (size_t k = 0; k < depth; k++) {
        *end++ = '/';
        end = stpcpy(end, parts[k]);
    }
    fd = open(full, flags, OPEN_MODE);
    e = errno;
out:
    free(copy);
    free(parts);
    free(full);
    errno = e;
    return fd;
}

const struct strategy logical = {
    .name = "logical",
    .semantics = BENEATH,
    .unavailable = NULL,
    .prepare = prepare,
    .open = open_logical,
    .release = NULL,
};
