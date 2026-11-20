// SPDX-License-Identifier: MIT OR Apache-2.0
//
// walk-beneath, walk-in-root: resolve the path one component at a time, in
// userspace, holding a descriptor for every directory on the way. Nothing is
// looked up by a path string longer than one component, so there is no window
// between a check and a use, and no PATH_MAX limit.
//
//   - Directories are opened with O_PATH | O_NOFOLLOW: no read permission is
//     needed, and nothing is ever opened for I/O on the way (a FIFO used as a
//     directory gives ENOTDIR instead of blocking).
//   - `..` pops our own stack of descriptors; it never asks the kernel, so a
//     directory renamed during the walk cannot carry it out of the root.
//   - Symlinks are read and their text resolved in place, at most MAX_HOPS of
//     them. Leaving the root (`..` at the root, an absolute link or request)
//     is refused under beneath semantics, and clamped to the root under
//     in-root semantics, as RESOLVE_BENEATH and RESOLVE_IN_ROOT do.
//   - The last component is opened with O_NOFOLLOW (a symlink there is read
//     and followed like any other, except by newfile, whose O_EXCL never
//     follows), and kept only if it is a regular file.
//
// State: a struct root, the root's descriptor (opened once) and the semantics.

#define _GNU_SOURCE // for O_PATH

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "strategy.h"

static void *prepare(const char *path, enum semantics semantics) {
    struct root *root = malloc(sizeof *root);
    if (root == NULL) {
        return NULL;
    }
    if (root_open(root, path, semantics) < 0) {
        int e = errno;
        free(root);
        errno = e;
        return NULL;
    }
    return root;
}

static void release(void *state) {
    struct root *root = state;
    close(root->fd);
    free(root);
}

// The directories walked so far; dirs[0] is the root, which we do not own.
struct stack {
    int *dirs;
    size_t n, cap;
};

static int top(const struct stack *s) {
    return s->dirs[s->n - 1];
}

static bool push(struct stack *s, int fd) {
    if (s->n == s->cap) {
        size_t cap = s->cap * 2;
        int *dirs = realloc(s->dirs, cap * sizeof *dirs);
        if (dirs == NULL) {
            return false;
        }
        s->dirs = dirs;
        s->cap = cap;
    }
    s->dirs[s->n++] = fd;
    return true;
}

static void pop_to(struct stack *s, size_t n) {
    while (s->n > n) {
        close(s->dirs[--s->n]);
    }
}

// What is left to resolve: rest[pos...]. A symlink's text replaces the link's
// own component at the front.
struct todo {
    char *rest;
    size_t pos;
};

// Replace the component just consumed by the link's text.
static bool replace_front(struct todo *t, const char *text, size_t len) {
    size_t tail = strlen(t->rest + t->pos);
    char *rest = malloc(len + tail + 1);
    if (rest == NULL) {
        return false;
    }
    memcpy(rest, text, len);
    memcpy(rest + len, t->rest + t->pos, tail + 1);
    free(t->rest);
    t->rest = rest;
    t->pos = 0;
    return true;
}

int walk_open(void *state, const char *path, int flags, const char **label) {
    struct root *root = state;
    struct stack s = {.dirs = malloc(16 * sizeof(int)), .n = 0, .cap = 16};
    struct todo t = {.rest = strdup(path), .pos = 0};
    int fd = -1, e = 0, hops = 0;
    char *name = NULL;
    char link[PATH_MAX];
    if (s.dirs == NULL || t.rest == NULL) {
        e = ENOMEM;
        goto out;
    }
    s.dirs[s.n++] = root->fd;
    bool segment_start = true; // at the start of the request or of a link's text

    // we parse manually, not with strtok_r, to handle empty components and trailing slashes
    for (;;) {
        if (segment_start && t.rest[t.pos] == '/') {
            if (root->semantics == BENEATH) {
                *label = LABEL_ESCAPE;
                goto out;
            }
            pop_to(&s, 1);
        }
        segment_start = false;
        t.pos += strspn(t.rest + t.pos, "/");
        // The next component, and whether it is the last. A path that ends in
        // `/`, `.` or `..` ends at a directory, which is opened as ".".
        size_t start = t.pos, len = strcspn(t.rest + start, "/"), end = start + len;
        bool last = t.rest[end] == '\0';
        t.pos = end;
        free(name);
        name = len == 0 ? strdup(".") : strndup(t.rest + start, len);
        if (name == NULL) {
            e = ENOMEM;
            goto out;
        }

        if (strcmp(name, "..") == 0) {
            if (s.n > 1) {
                pop_to(&s, s.n - 1);
            } else if (root->semantics == BENEATH) {
                *label = LABEL_ESCAPE;
                goto out;
            }
            if (!last) {
                continue;
            }
            strcpy(name, ".");
        } else if (strcmp(name, ".") == 0 && !last) {
            continue;
        }

        int linkfd = -1;
        if (!last) {
            int dfd = openat(top(&s), name, O_PATH | O_NOFOLLOW | O_CLOEXEC);
            if (dfd < 0) {
                e = errno;
                goto out;
            }
            struct stat sb;
            if (fstat(dfd, &sb) < 0) {
                e = errno;
                close(dfd);
                goto out;
            }
            if (S_ISDIR(sb.st_mode)) {
                if (!push(&s, dfd)) {
                    close(dfd);
                    e = ENOMEM;
                    goto out;
                }
                continue;
            }
            if (!S_ISLNK(sb.st_mode)) {
                close(dfd);
                e = ENOTDIR; // what the kernel says for a file used as a directory
                goto out;
            }
            linkfd = dfd;
        } else {
            fd = openat(top(&s), name, flags | O_NOFOLLOW | O_NONBLOCK | O_NOCTTY, OPEN_MODE);
            if (fd >= 0) {
                fd = keep_regular(fd, label);
                e = errno;
                goto out;
            }
            e = errno;
            if (e != ELOOP || strcmp(name, ".") == 0) {
                goto out;
            }
            // ELOOP: the last component may be a symlink; read and follow it.
            linkfd = openat(top(&s), name, O_PATH | O_NOFOLLOW | O_CLOEXEC);
            struct stat sb;
            if (linkfd < 0 || fstat(linkfd, &sb) < 0 || !S_ISLNK(sb.st_mode)) {
                if (linkfd >= 0) {
                    close(linkfd);
                }
                errno = e;
                goto out;
            }
        }

        // linkfd is a symlink: replace its component with its text.
        if (++hops > MAX_HOPS) {
            close(linkfd);
            e = ELOOP;
            goto out;
        }
        ssize_t n = readlinkat(linkfd, "", link, sizeof link);
        e = errno;
        close(linkfd);
        if (n < 0) {
            goto out;
        }
        if ((size_t)n == sizeof link) {
            e = ENAMETOOLONG;
            goto out;
        }
        if (!replace_front(&t, link, (size_t)n)) {
            e = ENOMEM;
            goto out;
        }
        segment_start = true;
    }

out:
    free(name);
    free(t.rest);
    if (s.dirs != NULL) {
        pop_to(&s, 1);
        free(s.dirs);
    }
    if (fd < 0 && *label == NULL) {
        errno = e;
    }
    return fd;
}

static void *prepare_beneath(const char *path) {
    return prepare(path, BENEATH);
}

static void *prepare_in_root(const char *path) {
    return prepare(path, IN_ROOT);
}

const struct strategy walk_beneath = {
    .name = "walk-beneath",
    .semantics = BENEATH,
    .unavailable = NULL,
    .prepare = prepare_beneath,
    .open = walk_open,
    .release = release,
};

const struct strategy walk_in_root = {
    .name = "walk-in-root",
    .semantics = IN_ROOT,
    .unavailable = NULL,
    .prepare = prepare_in_root,
    .open = walk_open,
    .release = release,
};
