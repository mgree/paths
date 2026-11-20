// SPDX-License-Identifier: MIT OR Apache-2.0
//
// realpath resolves and then checks. It calls realpath(3) on the joined path,
// checks that the result is the root or below it (comparing whole components),
// and then opens it. When creating, the file does not exist yet, so the parent
// is resolved and the name appended, as such code usually does. The check and
// the open are two separate lookups, so anything that changes in between is
// not caught.
//
// State: the root's canonical path, from realpath(3) once in prepare, so that
// it can be compared directly with what realpath(3) returns for each request.

#define _GNU_SOURCE // for realpath

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>

#include "strategy.h"

static void *prepare(const char *root) {
    return realpath(root, NULL);
}

// Return the realpath of full's parent, with full's last component appended.
static char *realpath_parent(const char *full) {
    const char *slash = strrchr(full, '/');
    size_t dir_len = (size_t)(slash - full);
    char *dir = strndup(full, dir_len == 0 ? 1 : dir_len);
    if (dir == NULL) {
        return NULL;
    }
    char *rdir = realpath(dir, NULL);
    int e = errno;
    free(dir);
    if (rdir == NULL) {
        errno = e;
        return NULL;
    }
    char *out = join(rdir, slash + 1);
    free(rdir);
    return out;
}

static int open_realpath(void *state, const char *path, int flags, const char **label) {
    const char *root = state;
    char *full = join(root, path);
    if (full == NULL) {
        return -1;
    }
    char *resolved = realpath(full, NULL);
    if (resolved == NULL && errno == ENOENT && (flags & O_CREAT)) {
        resolved = realpath_parent(full);
    }
    int e = errno;
    free(full);
    if (resolved == NULL) {
        errno = e;
        return -1;
    }
    size_t r = strlen(root);
    if (strncmp(resolved, root, r) != 0 || (resolved[r] != '\0' && resolved[r] != '/')) {
        free(resolved);
        *label = LABEL_ESCAPE;
        return -1;
    }
    int fd = open(resolved, flags, OPEN_MODE);
    e = errno;
    free(resolved);
    errno = e;
    return fd;
}

const struct strategy realpath_check = {
    .name = "realpath",
    .semantics = BENEATH,
    .unavailable = NULL,
    .prepare = prepare,
    .open = open_realpath,
    .release = free,
};
