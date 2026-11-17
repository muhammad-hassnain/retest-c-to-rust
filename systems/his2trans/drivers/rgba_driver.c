/* rgba: rgba_from_string(const char*, short *ok), rgba_new(uint32_t), rgba_to_string(rgba_t, buf, len),
 * rgba_inspect(uint32_t). ops: "fs <hex>" -> ok value ; "new u32" -> r g b a ; "ts r g b a" -> string ;
 * "insp u32" -> library printf */
#include "common.h"
#include "rgba.h"
static void rg_dispatch(void) {
  const char *op = rg_tok[0];
  if (!strcmp(op, "fs")) {
    char *s = rg_hex(rg_tok[1], NULL);
    short ok = 0;
    uint32_t v = rgba_from_string(s, &ok);
    rg_pi(ok);
    rg_pu(ok ? v : 0);
    rg_pu(v);
    free(s);
  } else if (!strcmp(op, "new")) {
    rgba_t c = rgba_new((uint32_t)strtoull(rg_tok[1], NULL, 10));
    rg_pd(c.r); rg_pd(c.g); rg_pd(c.b); rg_pd(c.a);
  } else if (!strcmp(op, "ts")) {
    rgba_t c;
    c.r = rg_dbl(rg_tok[1]); c.g = rg_dbl(rg_tok[2]); c.b = rg_dbl(rg_tok[3]); c.a = rg_dbl(rg_tok[4]);
    char buf[512];
    memset(buf, 0, sizeof buf);
    rgba_to_string(c, buf, sizeof buf);
    rg_ps(buf);
  } else if (!strcmp(op, "insp")) {
    printf(" ");
    rgba_inspect((uint32_t)strtoull(rg_tok[1], NULL, 10));
  }
}
