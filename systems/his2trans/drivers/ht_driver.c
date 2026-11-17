/* ht: the library's only computation is the file-local (static) FNV-1a hash_key(const char*).
 * ht.c is #included so the static function is reachable; its empty `void main(){}` is renamed. */
#define main ht_c_original_main
#include "ht.c"
#undef main
#include "common.h"
static void rg_dispatch(void) {
  if (!strcmp(rg_tok[0], "hash")) {
    char *s = rg_hex(rg_tok[1], NULL);
    rg_pu((unsigned long long)hash_key(s));
    free(s);
  }
}
