/* Shared helpers for the His2Trans C-side differential drivers (study-written, not from the artifact).
 *
 * Protocol (identical in drivers/common.rs): every stdin line is one API call "op arg...".
 * Strings are hex-encoded ("~" = empty); doubles are 16-hex-digit IEEE-754 bit patterns; integers
 * are decimal. Every call prints one line: the op name, then its observable results as tokens:
 *   " d:<16 hex>"  a double (compared with the VERT relative tolerance by run.py)
 *   " s:<hex>"     a string ("s:~" = empty), " NULL" a null pointer, " <int>" an integer.
 * Library functions that print (rgba_inspect, buffer_print, url_data_inspect, genann_write) write
 * into the same stdout stream, between the op name and the newline.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

static int rg_ntok;
static char *rg_tok[8192];

static void rg_split(char *line) {
  rg_ntok = 0;
  char *s = strtok(line, " \t\r\n");
  while (s && rg_ntok < 8192) {
    rg_tok[rg_ntok++] = s;
    s = strtok(NULL, " \t\r\n");
  }
}

/* heap-allocated, NUL-terminated copy of the decoded bytes (callers may free or hand it over) */
static char *rg_hex(const char *h, size_t *len) {
  size_t n = (strcmp(h, "~") == 0) ? 0 : strlen(h) / 2;
  char *out = malloc(n + 1);
  for (size_t i = 0; i < n; i++) {
    unsigned int b;
    sscanf(h + 2 * i, "%2x", &b);
    out[i] = (char)b;
  }
  out[n] = 0;
  if (len) *len = n;
  return out;
}

static double rg_dbl(const char *h) {
  uint64_t u = strtoull(h, NULL, 16);
  double d;
  memcpy(&d, &u, 8);
  return d;
}

static long long rg_int(const char *s) { return strtoll(s, NULL, 10); }

static void rg_pd(double d) {
  uint64_t u;
  memcpy(&u, &d, 8);
  printf(" d:%016llx", (unsigned long long)u);
}

static void rg_psn(const char *s, size_t n) {
  printf(" s:");
  if (n == 0) { printf("~"); return; }
  for (size_t i = 0; i < n; i++) printf("%02x", (unsigned char)s[i]);
}

static void rg_ps(const char *s) {
  if (!s) { printf(" NULL"); return; }
  rg_psn(s, strlen(s));
}

static void rg_pi(long long v) { printf(" %lld", v); }
static void rg_pu(unsigned long long v) { printf(" %llu", v); }

static void rg_dispatch(void);

int main(void) {
  char *line = NULL;
  size_t cap = 0;
  while (getline(&line, &cap, stdin) > 0) {
    rg_split(line);
    if (rg_ntok == 0) continue;
    printf("%s", rg_tok[0]);
    rg_dispatch();
    printf("\n");
    fflush(stdout);
  }
  free(line);
  return 0;
}
