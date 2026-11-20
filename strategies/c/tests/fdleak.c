// SPDX-License-Identifier: MIT OR Apache-2.0
//
// A sanity check for the runner's valgrind mode: leaks one descriptor, unless
// given the argument "close". Valgrind with --track-fds=yes and
// --error-exitcode must exit with that code for the leak, and 0 otherwise.

#include <fcntl.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    int fd = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) {
        return 1;
    }
    if (argc > 1 && strcmp(argv[1], "close") == 0) {
        close(fd);
    }
    return 0;
}
