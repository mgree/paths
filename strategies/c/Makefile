# SPDX-License-Identifier: MIT OR Apache-2.0
#
# build/driver        optimized, for timing
# build/driver-check  with AddressSanitizer and UBSan, for correctness runs:
#                     any memory error or undefined behaviour aborts the run,
#                     which the runner records as a driver failure

CC ?= cc
WARN = -std=c17 -Wall -Wextra -Wpedantic -Wshadow -Wstrict-prototypes -Werror
OPT = -O2 -g
CHECK = -O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined -fno-sanitize-recover=all

SRC = driver.c strategies.c common.c string_concat.c logical.c realpath.c walk.c openat2.c
HDR = strategy.h

all: build/driver build/driver-check

build/driver: $(SRC) $(HDR) | build
	$(CC) $(WARN) $(OPT) -o $@ $(SRC)

build/driver-check: $(SRC) $(HDR) | build
	$(CC) $(WARN) $(CHECK) -o $@ $(SRC)

build:
	mkdir -p build

clean:
	rm -rf build

.PHONY: all clean
