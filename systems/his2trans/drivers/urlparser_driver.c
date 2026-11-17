/* urlparser (url.h, header-only). ops: "isp s" "isssh s" (bool) ; "gp|ga|ghn|gh|gpath|gpn|gs|gq|gha|gport url"
 * (url_get_protocol/auth/hostname/host/path/pathname/search/query/hash/port -> string|NULL) ;
 * "parse url" -> NULL | the 11 fields in struct order ; "insp url" -> url_inspect (library printf).
 * Every call gets its own heap copy of the input (the C API takes char*). */
#include "common.h"
#include "url.h"
static void rg_dispatch(void) {
  const char *op = rg_tok[0];
  char *u = rg_hex(rg_tok[1], NULL);
  if (!strcmp(op, "isp")) rg_pi(url_is_protocol(u));
  else if (!strcmp(op, "isssh")) rg_pi(url_is_ssh(u));
  else if (!strcmp(op, "gp")) rg_ps(url_get_protocol(u));
  else if (!strcmp(op, "ga")) rg_ps(url_get_auth(u));
  else if (!strcmp(op, "ghn")) rg_ps(url_get_hostname(u));
  else if (!strcmp(op, "gh")) rg_ps(url_get_host(u));
  else if (!strcmp(op, "gpath")) rg_ps(url_get_path(u));
  else if (!strcmp(op, "gpn")) rg_ps(url_get_pathname(u));
  else if (!strcmp(op, "gs")) rg_ps(url_get_search(u));
  else if (!strcmp(op, "gq")) rg_ps(url_get_query(u));
  else if (!strcmp(op, "gha")) rg_ps(url_get_hash(u));
  else if (!strcmp(op, "gport")) rg_ps(url_get_port(u));
  else if (!strcmp(op, "parse")) {
    url_data_t *d = url_parse(u);
    if (!d) printf(" NULL");
    else {
      rg_ps(d->href); rg_ps(d->protocol); rg_ps(d->host); rg_ps(d->auth); rg_ps(d->hostname);
      rg_ps(d->pathname); rg_ps(d->search); rg_ps(d->path); rg_ps(d->hash); rg_ps(d->query); rg_ps(d->port);
    }
  } else if (!strcmp(op, "insp")) {
    printf(" ");
    url_inspect(u);
  }
}
