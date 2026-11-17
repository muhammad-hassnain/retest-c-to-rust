/* ORACLE: the ORIGINAL u8c (UTF-8 library) the RustAssure translation derived from.
 * Reads bytes from stdin (NUL-terminated), decodes the whole string one codepoint at
 * a time with u8next_, and prints a canonical event stream the Rust SUT must match.
 *
 * Walk logic is identical on both sides: call u8next_, print (codepoint, bytes), then
 *   n == 0  -> NUL / end-of-string: stop
 *   n <  0  -> invalid sequence: advance 1 byte and resync
 *   n >  0  -> advance n bytes
 * (capped at len+2 iterations as a safety bound).
 *
 * Output lines:  D <codepoint> <n>     then   END <steps>
 */
#include <stdio.h>
#include <stdlib.h>
#include "u8c.h"

int main(void) {
    size_t cap = 1 << 16, n = 0, r;
    unsigned char *buf = (unsigned char *)malloc(cap);
    while ((r = fread(buf + n, 1, cap - n, stdin)) > 0) {
        n += r;
        if (n + 1 >= cap) { cap *= 2; buf = (unsigned char *)realloc(buf, cap); }
    }
    buf[n] = 0; /* NUL-terminate */

    size_t pos = 0, steps = 0, guard = n + 2;
    while (pos <= n && steps < guard) {
        int ch = 0;
        int consumed = u8next_((const char *)(buf + pos), &ch);
        printf("D %d %d\n", ch, consumed);
        steps++;
        if (consumed == 0) break;          /* NUL / end */
        else if (consumed < 0) pos += 1;   /* invalid: resync */
        else pos += (size_t)consumed;
    }
    printf("END %zu\n", steps);
    free(buf);
    return 0;
}
