/* ORACLE: the ORIGINAL libcsv (v3.0.3) the RustAssure translation derived from.
 * Reads CSV bytes from stdin, parses with csv_parse/csv_fini using recording
 * callbacks, and prints a canonical event stream the Rust SUT must reproduce
 * byte-for-byte. Config (delim/quote/space/term/options) is set EXPLICITLY so
 * both sides are driven identically -- no reliance on either side's defaults.
 *
 * Canonical output lines:
 *   F <len> <hex...>   field callback (cb1): reported length + field bytes
 *   F NULL             field callback with a NULL pointer (CSV_EMPTY_IS_NULL)
 *   R <c>              row  callback (cb2): terminating character
 *   P <processed>      bytes consumed by csv_parse
 *   Z <fini>           csv_fini return value
 *   E <error>          csv_error(p) after fini
 *
 *   argv: [options] [delim] [quote]   (decimal byte values; defaults 0 , ")
 */
#include <stdio.h>
#include <stdlib.h>
#include "csv.h"

static int my_space(unsigned char c) { return (c == CSV_SPACE || c == CSV_TAB); }
static int my_term(unsigned char c)  { return (c == CSV_CR || c == CSV_LF); }

static void cb1(void *s, size_t len, void *data) {
    (void)data;
    if (s == NULL) { printf("F NULL\n"); return; }
    unsigned char *p = (unsigned char *)s;
    printf("F %zu", len);
    for (size_t i = 0; i < len; i++) printf(" %02x", p[i]);
    printf("\n");
}

static void cb2(int c, void *data) {
    (void)data;
    printf("R %d\n", c);
}

int main(int argc, char **argv) {
    unsigned char options = (argc > 1) ? (unsigned char)atoi(argv[1]) : 0;
    unsigned char delim   = (argc > 2) ? (unsigned char)atoi(argv[2]) : CSV_COMMA;
    unsigned char quote   = (argc > 3) ? (unsigned char)atoi(argv[3]) : CSV_QUOTE;

    struct csv_parser p;
    if (csv_init(&p, options) != 0) { printf("INIT_FAIL\n"); return 2; }
    csv_set_delim(&p, delim);
    csv_set_quote(&p, quote);
    csv_set_space_func(&p, my_space);
    csv_set_term_func(&p, my_term);

    size_t cap = 1 << 16, n = 0, r;
    unsigned char *buf = (unsigned char *)malloc(cap);
    while ((r = fread(buf + n, 1, cap - n, stdin)) > 0) {
        n += r;
        if (n == cap) { cap *= 2; buf = (unsigned char *)realloc(buf, cap); }
    }

    size_t processed = csv_parse(&p, buf, n, cb1, cb2, NULL);
    printf("P %zu\n", processed);
    int fini = csv_fini(&p, cb1, cb2, NULL);
    printf("Z %d\n", fini);
    printf("E %d\n", csv_error(&p));
    csv_free(&p);
    free(buf);
    return 0;
}
