# SPDX-License-Identifier: MIT OR Apache-2.0
#
# build/racer  swaps two names until SIGTERM, for the race fixtures

CC ?= cc
WARN = -std=c17 -Wall -Wextra -Wpedantic -Wshadow -Wstrict-prototypes -Werror

build/racer: racer.c | build
	$(CC) $(WARN) -O2 -o $@ racer.c

build:
	mkdir -p build

clean:
	rm -rf build

.PHONY: clean
