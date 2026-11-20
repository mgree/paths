// SPDX-License-Identifier: MIT OR Apache-2.0
//
// The C driver: runs the strategies in strategies.c as protocol/PROTOCOL.md
// specifies.
//
//   driver list
//   driver once                    <strategy> <op> <root> <path>
//   driver race          <seconds> <strategy> <op> <root> <path>
//   driver time <warmup> <seconds> <strategy> <op> <root> <path>
//
// Exit status: 0 when the command ran (a strategy's errors are outcomes);
// 2 for bad arguments; 3 for a strategy this host cannot run; 1 for anything
// else that stops the driver.

#define _GNU_SOURCE // for strerrorname_np and program_invocation_short_name

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "strategy.h"

// Messages name the program as it was run: driver or driver-check.
#define NAME program_invocation_short_name

static _Noreturn void usage(const char *why) {
    fprintf(stderr,
            "%s: %s\n"
            "usage: %s list\n"
            "       %s once                    <strategy> <op> <root> <path>\n"
            "       %s race          <seconds> <strategy> <op> <root> <path>\n"
            "       %s time <warmup> <seconds> <strategy> <op> <root> <path>\n",
            NAME, why, NAME, NAME, NAME, NAME);
    exit(2);
}

// Report a failed system call and stop.
static _Noreturn void die(const char *what) {
    fprintf(stderr, "%s: %s: %s\n", NAME, what, strerror(errno));
    exit(1);
}

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000u + (uint64_t)ts.tv_nsec;
}

static double parse_seconds(const char *s) {
    char *end;
    errno = 0;
    double v = strtod(s, &end);
    if (errno != 0 || end == s || *end != '\0' || !(v >= 0)) {
        usage("seconds must be a non-negative decimal number");
    }
    return v;
}

static int parse_op(const char *op) {
    if (strcmp(op, "read") == 0) {
        return O_RDONLY | O_CLOEXEC;
    }
    if (strcmp(op, "newfile") == 0) {
        return O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC;
    }
    if (strcmp(op, "write") == 0) {
        return O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC;
    }
    usage("op must be read, newfile or write");
}

static const struct strategy *find(const char *name) {
    for (const struct strategy *const *s = strategies; *s != NULL; s++) {
        if (strcmp((*s)->name, name) == 0) {
            return *s;
        }
    }
    return NULL;
}

// --- outcomes ---------------------------------------------------------------

struct outcome {
    bool ok;
    // the opened file's identity, from fstat; dev==ino==0 when the mode skips fstat
    dev_t dev;
    ino_t ino;
    char label[32];
    uint64_t count;
};

struct outcomes {
    struct outcome *v;
    size_t n, cap;
};

static void error_label(char out[32], int err, const char *label) {
    if (label != NULL) {
        snprintf(out, 32, "%s", label);
        return;
    }
    const char *name = strerrorname_np(err);
    if (name != NULL) {
        snprintf(out, 32, "%s", name);
    } else {
        snprintf(out, 32, "E%d", err);
    }
}

// Add one outcome to the tally. The search is linear, which is fine for the few
// distinct outcomes a run usually has.
//
// TODO: We can speed this up with a hashtable if races aren't racy enough.
static void count(struct outcomes *os, const struct outcome *o) {
    for (size_t i = 0; i < os->n; i++) {
        struct outcome *x = &os->v[i];
        if (x->ok == o->ok && x->dev == o->dev &&
            x->ino == o->ino && strcmp(x->label, o->label) == 0) {
            x->count++;
            return;
        }
    }
    if (os->n == os->cap) {
        os->cap = os->cap ? os->cap * 2 : 8;
        os->v = realloc(os->v, os->cap * sizeof *os->v);
        if (os->v == NULL) {
            die("realloc");
        }
    }
    os->v[os->n] = *o;
    os->v[os->n].count = 1;
    os->n++;
}

static void print_outcomes(const struct outcomes *os, bool identify) {
    for (size_t i = 0; i < os->n; i++) {
        const struct outcome *o = &os->v[i];
        if (o->ok && identify) {
            printf("OUTCOME %" PRIu64 " ok %ju %ju\n", o->count, (uintmax_t)o->dev,
                   (uintmax_t)o->ino);
        } else if (o->ok) {
            printf("OUTCOME %" PRIu64 " ok\n", o->count);
        } else {
            printf("OUTCOME %" PRIu64 " error %s\n", o->count, o->label);
        }
    }
}

// --- one call ---------------------------------------------------------------

struct call {
    const struct strategy *s;
    void *state;
    const char *path;
    int flags;
};

// Make one call and close what it opened. If paused is not NULL, fstat the
// result first, to identify the file, and store the time that took in *paused,
// because that time isn't the strategy's.
static struct outcome one_call(const struct call *c, uint64_t *paused) {
    struct outcome o = {0};
    const char *label = NULL;
    int fd = c->s->open(c->state, c->path, c->flags, &label);
    if (fd < 0) {
        error_label(o.label, errno, label);
        return o;
    }
    o.ok = true;
    if (paused != NULL) {
        uint64_t t0 = now_ns();
        struct stat st;
        if (fstat(fd, &st) < 0) {
            die("fstat");
        }
        o.dev = st.st_dev;
        o.ino = st.st_ino;
        *paused = now_ns() - t0;
    }
    if (close(fd) < 0) {
        die("close");
    }
    return o;
}

// --- histogram (PROTOCOL.md: 1 ns buckets below 16 ns, then 16 per power of 2)

#define BUCKETS (16 + (64 - 4) * 16)

static size_t bucket(uint64_t v) {
    if (v < 16) {
        return (size_t)v;
    }
    int k = 63 - __builtin_clzll(v); // 63 - # of leading 0 bits
    //                  16: skips first 16 buckets for individual ns
    //        (k - 4) * 16: skips 16 buckets for each power of 2 from 16 to v's
    // (v >> (k - 4)) & 15: selects which of the 16 buckets at this level
    return 16 + (size_t)(k - 4) * 16 + (size_t)((v >> (k - 4)) & 15);
}

// Returns the lowest value in a given bucket index
static uint64_t bucket_lower(size_t i) {
    // first 16 are individual ns values
    if (i < 16) {
        return i;
    }
    //   k: position of leading 1 bit (which power of 2 range are we in)
    // sub: which of the 16 buckets at this level
    size_t k = (i - 16) / 16 + 4, sub = (i - 16) % 16;
    // Put leading 1 back above sub, shift both up to range k, leaving the rest 0.
    return (uint64_t)(16 + sub) << (k - 4);
}

// --- modes ------------------------------------------------------------------

static void run_once(const struct call *c) {
    uint64_t paused = 0, t0 = now_ns();
    struct outcome o = one_call(c, &paused);
    uint64_t elapsed = now_ns() - t0 - paused;
    o.count = 1;
    print_outcomes(&(struct outcomes){.v = &o, .n = 1, .cap = 1}, true);
    printf("TIME %" PRIu64 "\n", elapsed);
}

static void run_race(const struct call *c, double seconds) {
    struct outcomes os = {0};
    uint64_t deadline = now_ns() + (uint64_t)(seconds * 1e9);
    uint64_t paused = 0; // race doesn't time calls, but passing it makes one_call fstat
    do {
        struct outcome o = one_call(c, &paused);
        count(&os, &o);
    } while (now_ns() < deadline);
    print_outcomes(&os, true);
    free(os.v);
}

static void run_time(const struct call *c, double warmup, double seconds) {
    uint64_t t = now_ns(), stop = t + (uint64_t)(warmup * 1e9);
    while (t < stop) {
        one_call(c, NULL);
        t = now_ns();
    }
    struct outcomes os = {0};
    static uint64_t hist[BUCKETS];
    uint64_t total = 0;
    stop = t + (uint64_t)(seconds * 1e9);
    do {
        uint64_t t0 = now_ns();
        struct outcome o = one_call(c, NULL);
        t = now_ns();
        total += t - t0;
        hist[bucket(t - t0)]++;
        count(&os, &o);
    } while (t < stop);
    print_outcomes(&os, false);
    printf("TIME %" PRIu64 "\n", total);
    for (size_t i = 0; i < BUCKETS; i++) {
        if (hist[i] != 0) {
            printf("HIST %" PRIu64 " %" PRIu64 "\n", bucket_lower(i), hist[i]);
        }
    }
    free(os.v);
}

static void list_strategies(void) {
    static const char *const semantics[] = {[BENEATH] = "beneath", [IN_ROOT] = "in_root"};
    for (const struct strategy *const *s = strategies; *s != NULL; s++) {
        const char *why = (*s)->unavailable ? (*s)->unavailable() : NULL;
        if (why == NULL) {
            printf("STRATEGY %s %s\n", (*s)->name, semantics[(*s)->semantics]);
        } else {
            printf("UNAVAILABLE %s %s %s\n", (*s)->name, semantics[(*s)->semantics], why);
        }
    }
}

int main(int argc, char **argv) {
    if (argc < 2) {
        usage("no command");
    }
    const char *cmd = argv[1];
    if (strcmp(cmd, "list") == 0) {
        if (argc != 2) {
            usage("list takes no arguments");
        }
        list_strategies();
        return 0;
    }
    int fixed; // arguments before <strategy>
    if (strcmp(cmd, "once") == 0) {
        fixed = 2;
    } else if (strcmp(cmd, "race") == 0) {
        fixed = 3;
    } else if (strcmp(cmd, "time") == 0) {
        fixed = 4;
    } else {
        usage("unknown command");
    }
    if (argc != fixed + 4) {
        usage("wrong number of arguments");
    }
    const char *name = argv[fixed], *op = argv[fixed + 1], *root = argv[fixed + 2];
    struct call c = {.path = argv[fixed + 3], .flags = parse_op(op)};
    if ((c.flags & O_EXCL) && fixed > 2) {
        usage("race and time do not take newfile");
    }
    if (root[0] != '/') {
        usage("root must be an absolute path");
    }
    c.s = find(name);
    if (c.s == NULL) {
        usage("unknown strategy");
    }
    const char *why = c.s->unavailable ? c.s->unavailable() : NULL;
    if (why != NULL) {
        fprintf(stderr, "%s: %s is unavailable: %s\n", NAME, c.s->name, why);
        return 3;
    }
    umask(0);
    c.state = c.s->prepare(root);
    if (c.state == NULL) {
        fprintf(stderr, "%s: preparing %s for %s: %s\n", NAME, c.s->name, root, strerror(errno));
        return 1;
    }
    if (fixed == 2) {
        run_once(&c);
    } else if (fixed == 3) {
        run_race(&c, parse_seconds(argv[2]));
    } else {
        run_time(&c, parse_seconds(argv[2]), parse_seconds(argv[3]));
    }
    if (c.s->release != NULL) {
        c.s->release(c.state);
    }
    if (fflush(stdout) != 0 || ferror(stdout)) {
        die("stdout");
    }
    return 0;
}
