// SPDX-License-Identifier: MIT OR Apache-2.0
//
// The strategies this driver offers, in the order `list` prints them.

#include <stddef.h>

#include "strategy.h"

extern const struct strategy string_concat, logical, realpath_check, walk_beneath, walk_in_root,
    openat2_beneath, openat2_in_root, openat2_walk_beneath, openat2_walk_in_root;

const struct strategy *const strategies[] = {
    &string_concat,   &logical,         &realpath_check,      &walk_beneath,         &walk_in_root,
    &openat2_beneath, &openat2_in_root, &openat2_walk_beneath, &openat2_walk_in_root, NULL,
};
