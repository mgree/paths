// SPDX-License-Identifier: MIT OR Apache-2.0
//
// openat2-beneath, openat2-in-root: one openat2(2) call relative to the root,
// with RESOLVE_BENEATH or RESOLVE_IN_ROOT (plus RESOLVE_NO_MAGICLINKS, which
// both imply today), keeping the result only if it is a regular file. The
// kernel takes at most PATH_MAX bytes of path, and has no way to scope a
// lookup to one directory while starting from another, so past PATH_MAX these
// return ENAMETOOLONG.
//
// openat2-walk-beneath, openat2-walk-in-root: the same, but past PATH_MAX they
// fall back to the userspace walk (walk.c), which has no length limit.
//
// State: a struct openat2_state, a struct root (the root's descriptor, opened
// once, and the semantics) and whether to fall back to the walk.

#define _GNU_SOURCE // for O_PATH and syscall

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/openat2.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

#include "strategy.h"

// The kernel fails a scoped lookup with EAGAIN when a rename or mount anywhere
// on the system races with a `..` step, and the caller may retry, up to
// MAX_RETRIES times. filepath-securejoin also retries EXDEV, which a race can
// cause when a directory on the path is moved out of the root mid-lookup. We
// deliberately don't, so the race fixtures can measure the difference.

struct openat2_state {
    struct root root;
    bool fallback; // walk past PATH_MAX, for the openat2-walk strategies
};

static long sys_openat2(int dirfd, const char *path, struct open_how *how) {
    return syscall(SYS_openat2, dirfd, path, how, sizeof *how);
}

static const char *unavailable(void) {
    struct open_how how = {.flags = O_PATH | O_CLOEXEC};
    long fd = sys_openat2(AT_FDCWD, "/", &how);
    if (fd >= 0) {
        close((int)fd);
        return NULL;
    }
    return errno == ENOSYS ? "this kernel has no openat2" : "openat2 is blocked on this host";
}

static void *prepare(const char *path, enum semantics semantics, bool fallback) {
    struct openat2_state *st = malloc(sizeof *st);
    if (st == NULL) {
        return NULL;
    }
    if (root_open(&st->root, path, semantics) < 0) {
        int e = errno;
        free(st);
        errno = e;
        return NULL;
    }
    st->fallback = fallback;
    return st;
}

static void release(void *state) {
    struct openat2_state *st = state;
    close(st->root.fd);
    free(st);
}

static int open_openat2(void *state, const char *path, int flags, const char **label) {
    struct openat2_state *st = state;
    if (st->fallback && strlen(path) >= PATH_MAX) {
        return walk_open(&st->root, path, flags, label);
    }
    struct open_how how = {
        .flags = (unsigned)(flags | O_NONBLOCK | O_NOCTTY),
        .mode = (flags & O_CREAT) ? OPEN_MODE : 0, // rejects non-zero mode without O_CREAT
        .resolve = (st->root.semantics == IN_ROOT ? RESOLVE_IN_ROOT : RESOLVE_BENEATH)
                   | RESOLVE_NO_MAGICLINKS,
    };
    long fd;
    int tries = 0;
    do {
        fd = sys_openat2(st->root.fd, path, &how);
    } while (fd < 0 && errno == EAGAIN && tries++ < MAX_RETRIES);
    if (fd < 0) {
        return -1;
    }
    return keep_regular((int)fd, label);
}

static void *prepare_beneath(const char *path) {
    return prepare(path, BENEATH, false);
}

static void *prepare_in_root(const char *path) {
    return prepare(path, IN_ROOT, false);
}

static void *prepare_walk_beneath(const char *path) {
    return prepare(path, BENEATH, true);
}

static void *prepare_walk_in_root(const char *path) {
    return prepare(path, IN_ROOT, true);
}

const struct strategy openat2_beneath = {
    .name = "openat2-beneath",
    .semantics = BENEATH,
    .unavailable = unavailable,
    .prepare = prepare_beneath,
    .open = open_openat2,
    .release = release,
};

const struct strategy openat2_in_root = {
    .name = "openat2-in-root",
    .semantics = IN_ROOT,
    .unavailable = unavailable,
    .prepare = prepare_in_root,
    .open = open_openat2,
    .release = release,
};

const struct strategy openat2_walk_beneath = {
    .name = "openat2-walk-beneath",
    .semantics = BENEATH,
    .unavailable = unavailable,
    .prepare = prepare_walk_beneath,
    .open = open_openat2,
    .release = release,
};

const struct strategy openat2_walk_in_root = {
    .name = "openat2-walk-in-root",
    .semantics = IN_ROOT,
    .unavailable = unavailable,
    .prepare = prepare_walk_in_root,
    .open = open_openat2,
    .release = release,
};
