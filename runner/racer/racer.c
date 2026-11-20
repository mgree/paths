// SPDX-License-Identifier: MIT OR Apache-2.0
//
// racer <limit> <dirfd-a> <name-a> <dirfd-b> <name-b> swaps two entries with
// renameat2's RENAME_EXCHANGE, over and over, until SIGTERM, then prints how
// many swaps it made. Each entry is a name in a directory the caller opened and
// passed as a descriptor, so no path the racer uses can exceed PATH_MAX. The
// runner runs it while a driver is in race mode, and stops it when the driver
// is done. The exchange is atomic, so at every moment both names exist, each
// naming one of the two entries. The racer stops only after an even number of
// swaps, so it leaves the tree as it found it.
//
// To run it by hand, let the shell open the directories:
//
//   racer 5 3 f 3 f-link 3<root
//
// <limit> is a backstop, in whole seconds: if nobody has stopped the racer by
// then, because the runner died, SIGALRM kills it.

#define _GNU_SOURCE // for renameat2 and RENAME_EXCHANGE

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static volatile sig_atomic_t stop = 0;

static void on_term(int sig) {
    (void)sig;
    stop = 1;
}

static _Noreturn void usage(void) {
    fprintf(stderr, "usage: racer <limit> <dirfd-a> <name-a> <dirfd-b> <name-b>\n"
                    "  each name is one component, with no '/'\n");
    exit(2);
}

// Parse a whole decimal number in [min, max], or stop with the usage message.
static long parse_decimal_long(const char *s, long min, long max) {
    char *end;
    errno = 0;
    long v = strtol(s, &end, 10);
    if (errno != 0 || end == s || *end != '\0' || v < min || v > max) {
        usage();
    }
    return v;
}

static const char *parse_name(const char *s) {
    if (*s == '\0' || strchr(s, '/') != NULL) {
        usage();
    }
    return s;
}

int main(int argc, char **argv) {
    if (argc != 6) {
        usage();
    }
    unsigned limit = (unsigned)parse_decimal_long(argv[1], 1, INT_MAX);
    int fd_a = (int)parse_decimal_long(argv[2], 0, INT_MAX), fd_b = (int)parse_decimal_long(argv[4], 0, INT_MAX);
    const char *a = parse_name(argv[3]), *b = parse_name(argv[5]);
    struct sigaction sa = {.sa_handler = on_term};
    if (sigaction(SIGTERM, &sa, NULL) < 0) {
        perror("racer: sigaction");
        return 1;
    }
    alarm(limit);
    unsigned long long swaps = 0;
    while (!stop || swaps % 2 != 0) {
        if (renameat2(fd_a, a, fd_b, b, RENAME_EXCHANGE) < 0) {
            fprintf(stderr, "racer: exchanging %s and %s: %s\n", a, b, strerror(errno));
            return 1;
        }
        swaps++;
    }
    printf("%llu\n", swaps);
    return 0;
}
