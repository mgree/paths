// SPDX-License-Identifier: MIT OR Apache-2.0
//
// The C driver: runs the strategies in strategies.c as protocol/PROTOCOL.md
// specifies.
//
//   driver list
//   driver once                                 <strategy> <op> <root> <path>
//   driver race                       <seconds> <strategy> <op> <root> <path>
//   driver time <warmup-passes> <passes> <seed> <strategy> <op> <root> <requests>
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
#include <sys/mman.h>
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
            "       %s once                                 <strategy> <op> <root> <path>\n"
            "       %s race                       <seconds> <strategy> <op> <root> <path>\n"
            "       %s time <warmup-passes> <passes> <seed> <strategy> <op> <root> <requests>\n",
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

// Parse a decimal integer from 0 to 2^64 - 1. strtoull alone would accept a
// sign and leading spaces, so the first character must be a digit.
static uint64_t parse_count(const char *s, const char *what) {
    char *end;
    errno = 0;
    unsigned long long v = strtoull(s, &end, 10);
    if (s[0] < '0' || s[0] > '9' || errno != 0 || *end != '\0') {
        usage(what);
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

// --- outcomes ---------------------------------------------------------------

struct outcome {
    bool ok;
    // the opened file's identity, from fstat; dev==ino==0 when the mode skips fstat
    dev_t dev;
    ino_t ino;
    char label[32];
    uint64_t count;
    uint64_t *hist; // BUCKETS counts in time mode, NULL otherwise
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

// Add one outcome to the tally, and return the tallied outcome. The search is
// linear, which is fine for the few distinct outcomes a run usually has. If
// with_hist is true, a new outcome gets a histogram of its own.
//
// TODO: We can speed this up with a hashtable if races aren't racy enough.
static struct outcome *count(struct outcomes *os, const struct outcome *o, bool with_hist) {
    for (size_t i = 0; i < os->n; i++) {
        struct outcome *x = &os->v[i];
        if (x->ok == o->ok && x->dev == o->dev &&
            x->ino == o->ino && strcmp(x->label, o->label) == 0) {
            x->count++;
            return x;
        }
    }
    if (os->n == os->cap) {
        os->cap = os->cap ? os->cap * 2 : 8;
        os->v = realloc(os->v, os->cap * sizeof *os->v);
        if (os->v == NULL) {
            die("realloc");
        }
    }
    struct outcome *x = &os->v[os->n++];
    *x = *o;
    x->count = 1;
    x->hist = NULL;
    if (with_hist && (x->hist = calloc(BUCKETS, sizeof *x->hist)) == NULL) {
        die("calloc");
    }
    return x;
}

static void free_outcomes(struct outcomes *os) {
    for (size_t i = 0; i < os->n; i++) {
        free(os->v[i].hist);
    }
    free(os->v);
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
        count(&os, &o, false);
    } while (now_ns() < deadline);
    print_outcomes(&os, true);
    free_outcomes(&os);
}

// --- requests for time (PROTOCOL.md: a file of NUL-terminated requests) -----

struct requests {
    const char *bytes; // the file, mapped read-only, which the requests point into
    size_t size;
    const char **v;
    size_t n;
};

// Map every request in path. Each one ends with a NUL, so there is one request
// per NUL, and a file that doesn't end with one is refused. Counting the
// requests reads every page, so every page is mapped before the first call;
// MAP_POPULATE, where there is one, maps them all at once instead.
static struct requests read_requests(const char *path) {
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    struct stat st;
    if (fd < 0 || fstat(fd, &st) < 0) {
        die(path);
    }
    // mmap refuses a length of 0, so an empty file is refused before it.
    if (st.st_size == 0) {
        usage("requests must be NUL-terminated, and there must be at least one");
    }
    int flags = MAP_PRIVATE;
#ifdef MAP_POPULATE
    flags |= MAP_POPULATE;
#endif
    struct requests r = {.size = (size_t)st.st_size};
    void *bytes = mmap(NULL, r.size, PROT_READ, flags, fd, 0);
    if (bytes == MAP_FAILED) {
        die(path);
    }
    r.bytes = bytes;
    // The mapping outlives the descriptor.
    if (close(fd) < 0) {
        die(path);
    }
    if (r.bytes[r.size - 1] != '\0') {
        usage("requests must be NUL-terminated, and there must be at least one");
    }
    const char *end = r.bytes + r.size;
    for (const char *q = r.bytes; (q = memchr(q, '\0', (size_t)(end - q))) != NULL; q++) {
        r.n++;
    }
    if ((r.v = malloc(r.n * sizeof *r.v)) == NULL) {
        die("malloc");
    }
    const char *p = r.bytes;
    for (size_t i = 0; i < r.n; i++) {
        r.v[i] = p;
        p += strlen(p) + 1;
    }
    return r;
}

static void release_requests(struct requests *r) {
    free(r->v);
    if (munmap((void *)r->bytes, r->size) < 0) {
        die("munmap");
    }
}

// The generator and the shuffle PROTOCOL.md fixes, so that every driver makes
// the same calls in the same order for the same seed.
static uint64_t splitmix64(uint64_t *state) {
    uint64_t z = (*state += 0x9E3779B97F4A7C15u);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9u;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBu;
    return z ^ (z >> 31);
}

static void shuffle(const char **v, size_t n, uint64_t *state) {
    // No shuffles for singleton and empty lists!
    if (n < 2) {
        return;
    }

    for (size_t i = n - 1; i > 0; i--) {
        size_t j = (size_t)(splitmix64(state) % (i + 1));
        const char *t = v[i];
        v[i] = v[j];
        v[j] = t;
    }
}

static void run_time(const struct call *c, struct requests *r, uint64_t warmup_passes,
                     uint64_t passes, uint64_t seed) {
    struct call k = *c;
    uint64_t state = seed;
    for (uint64_t pass = 0; pass < warmup_passes; pass++) {
        shuffle(r->v, r->n, &state);
        for (size_t i = 0; i < r->n; i++) {
            k.path = r->v[i];
            one_call(&k, NULL);
        }
    }
    struct outcomes os = {0};
    uint64_t total = 0;
    for (uint64_t pass = 0; pass < passes; pass++) {
        shuffle(r->v, r->n, &state);
        for (size_t i = 0; i < r->n; i++) {
            k.path = r->v[i];
            uint64_t t0 = now_ns();
            struct outcome o = one_call(&k, NULL);
            uint64_t took = now_ns() - t0;
            total += took;
            count(&os, &o, true)->hist[bucket(took)]++;
        }
    }
    print_outcomes(&os, false);
    printf("TIME %" PRIu64 "\n", total);
    for (size_t o = 0; o < os.n; o++) {
        for (size_t i = 0; i < BUCKETS; i++) {
            if (os.v[o].hist[i] != 0) {
                printf("HIST %zu %" PRIu64 " %" PRIu64 "\n", o, bucket_lower(i), os.v[o].hist[i]);
            }
        }
    }
    free_outcomes(&os);
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

enum mode { ONCE, RACE, TIME };

// Each mode's name, and how many arguments come before <strategy>: the
// program's name, the mode's, and the mode's own.
static const struct {
    const char *name;
    enum mode mode;
    int before;
} modes[] = {
    {"once", ONCE, 2},
    {"race", RACE, 3},
    {"time", TIME, 5},
};

int main(int argc, char **argv) {
    if (argc < 2) {
        usage("no command");
    }
    const char *cmd = argv[1];

    // detect sub-command
    if (strcmp(cmd, "list") == 0) {
        if (argc != 2) {
            usage("list takes no arguments");
        }
        list_strategies();
        return 0;
    }
    size_t m = 0;
    while (m < sizeof modes / sizeof *modes && strcmp(cmd, modes[m].name) != 0) {
        m++;
    }
    if (m == sizeof modes / sizeof *modes) {
        usage("unknown command");
    }
    enum mode mode = modes[m].mode;

    // parse the rest of the arguments
    int before = modes[m].before;
    if (argc != before + 4) {
        usage("wrong number of arguments");
    }
    const char *name = argv[before], *op = argv[before + 1], *root = argv[before + 2];
    const char *last = argv[before + 3]; // <path>, or the file of <requests>
    struct call c = {.flags = parse_op(op)};
    double seconds = 0;
    uint64_t warmup_passes = 0, passes = 0, seed = 0;
    switch (mode) {
    case ONCE:
        c.path = last;
        break;
    case RACE:
        c.path = last;
        seconds = parse_seconds(argv[2]);
        break;
    case TIME:
        warmup_passes = parse_count(argv[2], "warm-up passes must be a decimal integer");
        passes = parse_count(argv[3], "passes must be a decimal integer");
        seed = parse_count(argv[4], "the seed must be a decimal integer below 2^64");
        if (passes == 0) {
            usage("there must be at least one measured pass");
        }
        break;
    }
    if ((c.flags & O_EXCL) && mode != ONCE) {
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

    // let's go!
    switch (mode) {
    case ONCE:
        run_once(&c);
        break;
    case RACE:
        run_race(&c, seconds);
        break;
    case TIME: {
        struct requests r = read_requests(last);
        run_time(&c, &r, warmup_passes, passes, seed);
        release_requests(&r);
        break;
    }
    }

    if (c.s->release != NULL) {
        c.s->release(c.state);
    }
    if (fflush(stdout) != 0 || ferror(stdout)) {
        die("stdout");
    }
    return 0;
}
