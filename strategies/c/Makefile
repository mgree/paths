# SPDX-License-Identifier: MIT OR Apache-2.0
#
# build/driver        optimized, for timing
# build/driver-check  with AddressSanitizer and UBSan, for correctness runs:
#                     any memory error or undefined behaviour aborts the run,
#                     which the runner records as a driver failure
#
# make analyze        clang's static analyzer, failing on any report: memory
#                     errors on paths the tests never run, such as cleanup after
#                     a failed allocation. It does not track descriptors;
#                     valgrind does.

CC ?= cc
WARN = -std=c17 -Wall -Wextra -Wpedantic -Wshadow -Wstrict-prototypes -Werror
OPT = -O2 -g
CHECK = -O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined -fno-sanitize-recover=all
CLANG ?= clang
# clang 21 has the bounds checker as security.ArrayBound; 19 and earlier only
# as alpha.security.ArrayBoundV2. Ask clang which one it knows.
ARRAY_BOUND = $(shell $(CLANG) -cc1 -analyzer-checker-help 2>/dev/null \
                | grep -q '^  security\.ArrayBound ' \
                && echo security.ArrayBound || echo alpha.security.ArrayBoundV2)
ANALYZE = --analyze -Xclang -analyzer-checker=unix.Errno,$(ARRAY_BOUND) \
          -Xclang -analyzer-output=text -Xclang -analyzer-werror

SRC = driver.c strategies.c common.c string_concat.c logical.c realpath.c walk.c openat2.c
HDR = strategy.h

all: build/driver build/driver-check

build/driver: $(SRC) $(HDR) | build
	$(CC) $(WARN) $(OPT) -o $@ $(SRC)

build/driver-check: $(SRC) $(HDR) | build
	$(CC) $(WARN) $(CHECK) -o $@ $(SRC)

build:
	mkdir -p build

analyze:
	$(CLANG) $(WARN) $(ANALYZE) $(SRC)

clean:
	rm -rf build

.PHONY: all analyze clean
