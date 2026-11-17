/* buffer: one current buffer `cur`, built by a constructor op, then mutated/queried. After every op the
 * driver prints the call's result and the observable state through the public API:
 *   " | size len data"  = buffer_size(cur), buffer_length(cur), buffer_string(cur) (the data string).
 * ops: new | newsz n | newstr hex | newstrlen hex n | newcopy hex | app hex | appn hex n | pre hex |
 *      appf k hex int | resize n | compact | tl | tr | trim | fill c | clear | slice from to |
 *      eq hex | idx hex | print
 * appf formats: k=0 "%s", k=1 "%d", k=2 "%s=%d".
 * Strings handed to buffer_new_with_string(_length) are heap copies (the C buffer takes ownership). */
#include "common.h"
#include "buffer.h"
static buffer_t *cur = NULL;
static void state(void) {
  if (!cur) { printf(" | NULL"); return; }
  printf(" |");
  rg_pu(buffer_size(cur));
  rg_pu(buffer_length(cur));
  rg_ps(buffer_string(cur));
}
static const char *FMT[] = {"%s", "%d", "%s=%d"};
static void rg_dispatch(void) {
  const char *op = rg_tok[0];
  if (!strcmp(op, "new")) { cur = buffer_new(); }
  else if (!strcmp(op, "newsz")) { cur = buffer_new_with_size((size_t)rg_int(rg_tok[1])); }
  else if (!strcmp(op, "newstr")) { cur = buffer_new_with_string(rg_hex(rg_tok[1], NULL)); }
  else if (!strcmp(op, "newstrlen")) { cur = buffer_new_with_string_length(rg_hex(rg_tok[1], NULL), (size_t)rg_int(rg_tok[2])); }
  else if (!strcmp(op, "newcopy")) { char *s = rg_hex(rg_tok[1], NULL); cur = buffer_new_with_copy(s); free(s); }
  else if (!strcmp(op, "app")) { char *s = rg_hex(rg_tok[1], NULL); rg_pi(buffer_append(cur, s)); free(s); }
  else if (!strcmp(op, "appn")) { char *s = rg_hex(rg_tok[1], NULL); rg_pi(buffer_append_n(cur, s, (size_t)rg_int(rg_tok[2]))); free(s); }
  else if (!strcmp(op, "pre")) { char *s = rg_hex(rg_tok[1], NULL); rg_pi(buffer_prepend(cur, s)); free(s); }
  else if (!strcmp(op, "appf")) {
    int k = (int)rg_int(rg_tok[1]);
    char *s = rg_hex(rg_tok[2], NULL);
    int v = (int)rg_int(rg_tok[3]);
    int r = k == 0 ? buffer_appendf(cur, FMT[0], s) : k == 1 ? buffer_appendf(cur, FMT[1], v) : buffer_appendf(cur, FMT[2], s, v);
    rg_pi(r);
    free(s);
  }
  else if (!strcmp(op, "resize")) { rg_pi(buffer_resize(cur, (size_t)rg_int(rg_tok[1]))); }
  else if (!strcmp(op, "compact")) { rg_pi(buffer_compact(cur)); }
  else if (!strcmp(op, "tl")) { buffer_trim_left(cur); }
  else if (!strcmp(op, "tr")) { buffer_trim_right(cur); }
  else if (!strcmp(op, "trim")) { buffer_trim(cur); }
  else if (!strcmp(op, "fill")) { buffer_fill(cur, (int)rg_int(rg_tok[1])); }
  else if (!strcmp(op, "clear")) { buffer_clear(cur); }
  else if (!strcmp(op, "slice")) {
    buffer_t *s = buffer_slice(cur, (size_t)rg_int(rg_tok[1]), (ssize_t)rg_int(rg_tok[2]));
    if (!s) printf(" NULL");
    else { rg_pu(buffer_size(s)); rg_pu(buffer_length(s)); rg_ps(buffer_string(s)); buffer_free(s); }
  }
  else if (!strcmp(op, "eq")) { buffer_t *o = buffer_new_with_copy(rg_hex(rg_tok[1], NULL)); rg_pi(buffer_equals(cur, o)); buffer_free(o); }
  else if (!strcmp(op, "idx")) { char *s = rg_hex(rg_tok[1], NULL); rg_pi(buffer_indexof(cur, s)); free(s); }
  else if (!strcmp(op, "print")) { fflush(stdout); buffer_print(cur); fflush(stdout); }
  state();
}
