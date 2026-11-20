// SPDX-License-Identifier: MIT OR Apache-2.0
//
// The interface between the driver (driver.c) and the strategies. A strategy
// resolves a request path under a root and opens it; the driver runs it in one
// of the protocol's modes (protocol/PROTOCOL.md) and reports what happened.

#ifndef STRATEGY_H
#define STRATEGY_H

// Labels for errors that a strategy reports for its own reasons. When a
// strategy reproduces an error the kernel would give, it sets errno instead.
#define LABEL_ESCAPE "escape"           // the path would leave the root
#define LABEL_NOT_REGULAR "not-regular" // the file reached is not a regular file

// The mode argument for every open. The kernel uses it only when the call
// creates a file, which newfile and write both can, and the driver clears its
// umask, so a created file gets exactly this mode.
#define OPEN_MODE 0644

// The kernel's limit on symlinks followed in one lookup (MAXSYMLINKS).
#define MAX_HOPS 40

// What a strategy does with a path that would leave the root: BENEATH refuses
// it, and IN_ROOT keeps it inside the root, as RESOLVE_BENEATH and
// RESOLVE_IN_ROOT do. `list` prints these as the protocol's `beneath` and
// `in_root`.
enum semantics { BENEATH, IN_ROOT };

struct strategy {
    const char *name;
    enum semantics semantics;

    // Returns why this host can't run the strategy, or NULL if it can. The
    // pointer itself is NULL for a strategy that every host can run.
    const char *(*unavailable)(void);

    // Called once, before any call, with the root's absolute path. Returns
    // the strategy's state, or NULL with errno set.
    void *(*prepare)(const char *root);

    // Make one call: resolve path under the root and open it with flags (an
    // op's O_ flags plus O_CLOEXEC). Return the descriptor on success. On
    // failure, return -1 and either set errno, leaving *label NULL, or set
    // *label to one of the labels above.
    int (*open)(void *state, const char *path, int flags, const char **label);

    // Frees what prepare made; NULL if it made nothing.
    void (*release)(void *state);
};

// Every strategy this driver offers, NULL-terminated (strategies.c).
extern const struct strategy *const strategies[];

// Keep fd only if it is a regular file, clearing the O_NONBLOCK it was opened
// with. Otherwise close it and return -1, with *label set for a special file,
// or errno set if fstat or fcntl failed.
int keep_regular(int fd, const char **label);

// Return root + "/" + path as a malloc'd string, or NULL with errno set
// (common.c).
char *join(const char *root, const char *path);

// The root as the descriptor-based strategies (walk-* and openat2-*) hold it:
// a descriptor for the root directory, opened once, and the semantics.
struct root {
    int fd;
    enum semantics semantics;
};

// Open path as the root, with O_PATH. Returns 0, or -1 with errno set
// (common.c).
int root_open(struct root *root, const char *path, enum semantics semantics);

// The userspace walk from a struct root, shared by walk-* and openat2-walk-*
// (walk.c).
int walk_open(void *root, const char *path, int flags, const char **label);

#endif
