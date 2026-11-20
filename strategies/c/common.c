// SPDX-License-Identifier: MIT OR Apache-2.0

#define _GNU_SOURCE // for O_PATH

#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "strategy.h"

int keep_regular(int fd, const char **label) {
    struct stat st;
    if (fstat(fd, &st) < 0) {
        int e = errno;
        close(fd);
        errno = e;
        return -1;
    }
    if (!S_ISREG(st.st_mode)) {
        close(fd);
        *label = LABEL_NOT_REGULAR;
        return -1;
    }
    int fl = fcntl(fd, F_GETFL);
    if (fl < 0 || fcntl(fd, F_SETFL, fl & ~O_NONBLOCK) < 0) {
        int e = errno;
        close(fd);
        errno = e;
        return -1;
    }
    return fd;
}

int root_open(struct root *root, const char *path, enum semantics semantics) {
    root->fd = open(path, O_PATH | O_DIRECTORY | O_CLOEXEC);
    root->semantics = semantics;
    return root->fd < 0 ? -1 : 0;
}

char *join(const char *root, const char *path) {
    size_t r = strlen(root), p = strlen(path);
    char *s = malloc(r + 1 + p + 1);
    if (s == NULL) {
        return NULL;
    }
    memcpy(s, root, r);
    s[r] = '/';
    memcpy(s + r + 1, path, p + 1);
    return s;
}
