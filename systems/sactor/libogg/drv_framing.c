/* Library-level differential driver for libogg framing.c (ogg_stream_* / ogg_sync_* / ogg_page_*).
 *
 * The SAME driver source is linked against (a) the original libogg C (framing.c, upstream 0288fad)
 * and (b) a SACTOR Rust translation of framing.c (cdylib, C ABI, as SACTOR's own E2E check links
 * it). Only the library differs between the two binaries.
 *
 * Input: a call-sequence script on stdin (gen_cases.py). Encoder stream E, decoder stream D, sync
 * state Y, and a "wire" byte buffer holding every page E produced (which the script may corrupt,
 * truncate, reorder or drop before feeding it to Y).
 * Output: one line per op with every value the public API returns: return codes, page header
 * bytes, a 64-bit FNV-1a of each page body and packet, the ogg_page_* accessors, packet fields.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ogg/ogg.h>

static unsigned long long lcg_state;
static unsigned char lcg_byte(void) {
  lcg_state = lcg_state * 6364136223846793005ULL + 1442695040888963407ULL;
  return (unsigned char)(lcg_state >> 33);
}
static unsigned long long fnv(const unsigned char *p, long n) {
  unsigned long long h = 1469598103934665603ULL;
  if (!p) return 0;
  for (long i = 0; i < n; i++) { h ^= p[i]; h *= 1099511628211ULL; }
  return h;
}

#define MAXPAGES 4096
static unsigned char *wire = NULL;
static long wire_len = 0, wire_cap = 0, feed_pos = 0;
static long pg_off[MAXPAGES], pg_len[MAXPAGES];
static int npages = 0;

static void wire_append(const unsigned char *p, long n) {
  if (wire_len + n > wire_cap) {
    wire_cap = (wire_len + n) * 2 + 64;
    wire = realloc(wire, wire_cap);
  }
  if (n > 0) memcpy(wire + wire_len, p, n);
  wire_len += n;
}

static void page_summary(long ln, const char *tag, const ogg_page *og) {
  printf("%ld %s hlen=%ld blen=%ld ver=%d cont=%d bos=%d eos=%d gran=%lld serial=%d pageno=%ld packets=%d\n",
         ln, tag, og->header_len, og->body_len, ogg_page_version(og), ogg_page_continued(og),
         ogg_page_bos(og), ogg_page_eos(og), (long long)ogg_page_granulepos(og), ogg_page_serialno(og),
         ogg_page_pageno(og), ogg_page_packets(og));
  printf("   hdr=");
  for (long i = 0; i < og->header_len; i++) printf("%02x", og->header[i]);
  printf(" body=%016llx\n", fnv(og->body, og->body_len));
}

static void packet_summary(long ln, const char *tag, int ret, const ogg_packet *op) {
  if (ret == 1)
    printf("%ld %s ret=%d bytes=%ld bos=%ld eos=%ld gran=%lld pno=%lld h=%016llx\n", ln, tag, ret, op->bytes,
           op->b_o_s, op->e_o_s, (long long)op->granulepos, (long long)op->packetno, fnv(op->packet, op->bytes));
  else
    printf("%ld %s ret=%d\n", ln, tag, ret);
}

static ogg_stream_state E, D;
static ogg_sync_state Y;
static ogg_page P;       /* last page returned by ogg_sync_pageout / pageseek */
static int haveP = 0;

static void encoder_page(long ln, const char *tag, int ret, ogg_page *og) {
  if (ret == 0) { printf("%ld %s ret=0\n", ln, tag); return; }
  printf("%ld %s ret=%d\n", ln, tag, ret);
  page_summary(ln, "  epage", og);
  if (npages < MAXPAGES) { pg_off[npages] = wire_len; pg_len[npages] = og->header_len + og->body_len; npages++; }
  wire_append(og->header, og->header_len);
  wire_append(og->body, og->body_len);
}

static int feed(long ln, long n) {
  long left = wire_len - feed_pos;
  if (n > left) n = left;
  if (n < 0) n = 0;
  char *buf = ogg_sync_buffer(&Y, n);
  if (!buf) { printf("%ld feed %ld buffer=NULL\n", ln, n); return -1; }
  if (n > 0) memcpy(buf, wire + feed_pos, n);
  feed_pos += n;
  int r = ogg_sync_wrote(&Y, n);
  printf("%ld feed %ld wrote=%d\n", ln, n, r);
  return (int)n;
}

int main(void) {
  char line[65536], op[32];
  long ln = 0;
  memset(&E, 0, sizeof E); memset(&D, 0, sizeof D); memset(&Y, 0, sizeof Y); memset(&P, 0, sizeof P);
  int Einit = 0, Dinit = 0, Yinit = 0;
  setvbuf(stdout, NULL, _IOLBF, 0);
  while (fgets(line, sizeof line, stdin)) {
    ln++;
    if (sscanf(line, "%31s", op) != 1) continue;
    char *a = line + strlen(op);
    ogg_page og;
    if (!strcmp(op, "einit")) {
      int s; sscanf(a, "%d", &s);
      printf("%ld einit %d\n", ln, ogg_stream_init(&E, s)); Einit = 1;
    } else if (!strcmp(op, "pin")) {
      long len; unsigned long long seed; long bos, eos; long long gran, pno;
      sscanf(a, "%ld %llu %ld %ld %lld %lld", &len, &seed, &bos, &eos, &gran, &pno);
      unsigned char *d = malloc(len > 0 ? len : 1);
      lcg_state = seed;
      for (long i = 0; i < len; i++) d[i] = lcg_byte();
      ogg_packet p; memset(&p, 0, sizeof p);
      p.packet = d; p.bytes = len; p.b_o_s = bos; p.e_o_s = eos; p.granulepos = gran; p.packetno = pno;
      int r = ogg_stream_packetin(&E, &p);
      free(d);
      printf("%ld pin %ld ret=%d eos=%d\n", ln, len, r, ogg_stream_eos(&E));
    } else if (!strcmp(op, "iov")) {
      /* iov N L1 .. LN SEED EOS GRAN */
      int n, off = 0, k; long lens[64]; unsigned long long seed; long eos; long long gran;
      sscanf(a, "%d%n", &n, &k); off += k;
      if (n > 64) n = 64;
      for (int i = 0; i < n; i++) { sscanf(a + off, "%ld%n", &lens[i], &k); off += k; }
      sscanf(a + off, "%llu %ld %lld", &seed, &eos, &gran);
      ogg_iovec_t iov[64];
      lcg_state = seed;
      for (int i = 0; i < n; i++) {
        unsigned char *d = malloc(lens[i] > 0 ? lens[i] : 1);
        for (long j = 0; j < lens[i]; j++) d[j] = lcg_byte();
        iov[i].iov_base = d; iov[i].iov_len = (size_t)lens[i];
      }
      int r = ogg_stream_iovecin(&E, iov, n, eos, gran);
      for (int i = 0; i < n; i++) free(iov[i].iov_base);
      printf("%ld iov %d ret=%d\n", ln, n, r);
    } else if (!strcmp(op, "pout")) {
      encoder_page(ln, "pout", ogg_stream_pageout(&E, &og), &og);
    } else if (!strcmp(op, "flush")) {
      encoder_page(ln, "flush", ogg_stream_flush(&E, &og), &og);
    } else if (!strcmp(op, "poutf")) {
      int n; sscanf(a, "%d", &n);
      encoder_page(ln, "poutf", ogg_stream_pageout_fill(&E, &og, n), &og);
    } else if (!strcmp(op, "flushf")) {
      int n; sscanf(a, "%d", &n);
      encoder_page(ln, "flushf", ogg_stream_flush_fill(&E, &og, n), &og);
    } else if (!strcmp(op, "drain")) {
      /* drain MODE [N]: call pageout(0)/flush(1)/pageout_fill(2)/flush_fill(3) until it returns 0 */
      int m, n = 0, r, it = 0; sscanf(a, "%d %d", &m, &n);
      do {
        if (m == 0) r = ogg_stream_pageout(&E, &og);
        else if (m == 1) r = ogg_stream_flush(&E, &og);
        else if (m == 2) r = ogg_stream_pageout_fill(&E, &og, n);
        else r = ogg_stream_flush_fill(&E, &og, n);
        encoder_page(ln, "drain", r, &og);
      } while (r != 0 && ++it < 100000);
    } else if (!strcmp(op, "eeos")) {
      printf("%ld eeos %d\n", ln, ogg_stream_eos(&E));
    } else if (!strcmp(op, "echeck")) {
      printf("%ld echeck %d\n", ln, ogg_stream_check(&E));
    } else if (!strcmp(op, "ereset")) {
      printf("%ld ereset %d\n", ln, ogg_stream_reset(&E));
    } else if (!strcmp(op, "eserial")) {
      int s; sscanf(a, "%d", &s);
      printf("%ld eserial %d\n", ln, ogg_stream_reset_serialno(&E, s));
    } else if (!strcmp(op, "wcorrupt")) {
      long pos; int x; sscanf(a, "%ld %d", &pos, &x);
      if (wire_len > 0) { pos %= wire_len; wire[pos] ^= (unsigned char)x; }
      printf("%ld wcorrupt %ld\n", ln, wire_len > 0 ? pos : -1L);
    } else if (!strcmp(op, "wdrop")) {
      int k; sscanf(a, "%d", &k);
      if (k >= 0 && k < npages) {
        long o = pg_off[k], l = pg_len[k];
        memmove(wire + o, wire + o + l, wire_len - o - l);
        wire_len -= l;
        for (int i = k + 1; i < npages; i++) { pg_off[i - 1] = pg_off[i] - l; pg_len[i - 1] = pg_len[i]; }
        npages--;
      }
      printf("%ld wdrop %d wire=%ld\n", ln, k, wire_len);
    } else if (!strcmp(op, "wswap")) {
      int k; sscanf(a, "%d", &k);
      if (k >= 0 && k + 1 < npages) {
        long o = pg_off[k], l1 = pg_len[k], l2 = pg_len[k + 1];
        unsigned char *tmp = malloc(l1 + l2);
        memcpy(tmp, wire + o + l1, l2);
        memcpy(tmp + l2, wire + o, l1);
        memcpy(wire + o, tmp, l1 + l2);
        free(tmp);
        pg_len[k] = l2; pg_off[k + 1] = o + l2; pg_len[k + 1] = l1;
      }
      printf("%ld wswap %d\n", ln, k);
    } else if (!strcmp(op, "wins")) {
      /* wins POS N SEED : insert N garbage bytes at POS (pages after POS are not re-indexed) */
      long pos, n; unsigned long long seed; sscanf(a, "%ld %ld %llu", &pos, &n, &seed);
      if (pos > wire_len) pos = wire_len;
      if (pos < 0) pos = 0;
      unsigned char *g = malloc(n > 0 ? n : 1);
      lcg_state = seed;
      for (long i = 0; i < n; i++) g[i] = lcg_byte();
      long old = wire_len;
      wire_append(g, n);   /* grow */
      memmove(wire + pos + n, wire + pos, old - pos);
      memcpy(wire + pos, g, n);
      free(g);
      printf("%ld wins %ld %ld\n", ln, pos, n);
    } else if (!strcmp(op, "wtrunc")) {
      long n; sscanf(a, "%ld", &n);
      if (n < wire_len && n >= 0) wire_len = n;
      printf("%ld wtrunc %ld\n", ln, wire_len);
    } else if (!strcmp(op, "wcrc")) {
      /* recompute the CRC of page K with ogg_page_checksum_set on a copy whose CRC field is zeroed */
      int k; sscanf(a, "%d", &k);
      if (k >= 0 && k < npages && pg_len[k] >= 27) {
        unsigned char *pg = malloc(pg_len[k]);
        memcpy(pg, wire + pg_off[k], pg_len[k]);
        long hl = 27 + pg[26];
        if (hl <= pg_len[k]) {
          ogg_page c; c.header = pg; c.header_len = hl; c.body = pg + hl; c.body_len = pg_len[k] - hl;
          unsigned char orig[4]; memcpy(orig, pg + 22, 4);
          memset(pg + 22, 0, 4);
          ogg_page_checksum_set(&c);
          printf("%ld wcrc %d crc=%02x%02x%02x%02x same=%d\n", ln, k, pg[22], pg[23], pg[24], pg[25], !memcmp(orig, pg + 22, 4));
        } else printf("%ld wcrc %d short\n", ln, k);
        free(pg);
      } else printf("%ld wcrc %d none\n", ln, k);
    } else if (!strcmp(op, "yinit")) {
      printf("%ld yinit %d\n", ln, ogg_sync_init(&Y)); Yinit = 1; feed_pos = 0;
    } else if (!strcmp(op, "feed")) {
      long n; sscanf(a, "%ld", &n); feed(ln, n);
    } else if (!strcmp(op, "ywrote")) {
      long n; sscanf(a, "%ld", &n);
      printf("%ld ywrote %ld = %d\n", ln, n, ogg_sync_wrote(&Y, n));
    } else if (!strcmp(op, "ybuf")) {
      long n; sscanf(a, "%ld", &n);
      char *b = ogg_sync_buffer(&Y, n);
      if (b && n > 0) memset(b, 'Z', n);   /* a later ywrote <= n must not expose uninitialised bytes */
      printf("%ld ybuf %ld %s\n", ln, n, b ? "ok" : "NULL");
    } else if (!strcmp(op, "ypout")) {
      int r = ogg_sync_pageout(&Y, &P);
      printf("%ld ypout ret=%d\n", ln, r);
      haveP = (r == 1);
      if (r == 1) page_summary(ln, "  ypage", &P);
    } else if (!strcmp(op, "yseek")) {
      long r = ogg_sync_pageseek(&Y, &P);
      printf("%ld yseek ret=%ld\n", ln, r);
      haveP = (r > 0);
      if (r > 0) page_summary(ln, "  ypage", &P);
    } else if (!strcmp(op, "dinit")) {
      int s; sscanf(a, "%d", &s);
      printf("%ld dinit %d\n", ln, ogg_stream_init(&D, s)); Dinit = 1;
    } else if (!strcmp(op, "dpin")) {
      if (haveP) printf("%ld dpin %d\n", ln, ogg_stream_pagein(&D, &P));
      else printf("%ld dpin nopage\n", ln);
    } else if (!strcmp(op, "dpout")) {
      ogg_packet p; memset(&p, 0, sizeof p);
      int r = ogg_stream_packetout(&D, &p);
      packet_summary(ln, "dpout", r, &p);
    } else if (!strcmp(op, "dpeek")) {
      ogg_packet p; memset(&p, 0, sizeof p);
      int r = ogg_stream_packetpeek(&D, &p);
      packet_summary(ln, "dpeek", r, &p);
    } else if (!strcmp(op, "dpeekn")) {
      printf("%ld dpeekn %d\n", ln, ogg_stream_packetpeek(&D, NULL));
    } else if (!strcmp(op, "decode")) {
      /* decode CHUNK [AUTOINIT]: feed CHUNK bytes at a time; every page -> pagein; drain packets.
         AUTOINIT=1: (re)init D with the serial of the first page if D is not yet initialised. */
      long chunk; int autoinit = 0; sscanf(a, "%ld %d", &chunk, &autoinit);
      int guard = 0;
      /* libogg's documented order is buffer -> wrote -> pageout. Calling pageout on a fresh sync
         state computes NULL + 0 in the C (framing.c:649), which is UB in C17; feed first. */
      if (feed_pos == 0 && wire_len > 0) feed(ln, chunk);
      for (;;) {
        int r = ogg_sync_pageout(&Y, &P);
        if (r == 0) {
          if (feed_pos >= wire_len) break;
          feed(ln, chunk);
          continue;
        }
        if (r < 0) { printf("%ld decode hole %d\n", ln, r); continue; }
        haveP = 1;
        page_summary(ln, "  dpage", &P);
        if (autoinit && !Dinit) { printf("%ld dinit %d\n", ln, ogg_stream_init(&D, ogg_page_serialno(&P))); Dinit = 1; }
        printf("%ld dpin %d\n", ln, ogg_stream_pagein(&D, &P));
        for (;;) {
          ogg_packet p; memset(&p, 0, sizeof p);
          int q = ogg_stream_packetout(&D, &p);
          if (q == 0) break;
          packet_summary(ln, "  pkt", q, &p);
          if (++guard > 200000) break;
        }
        if (++guard > 200000) break;
      }
      printf("%ld decode done eos=%d\n", ln, Dinit ? ogg_stream_eos(&D) : -9);
    } else if (!strcmp(op, "ycheck")) {
      printf("%ld ycheck %d\n", ln, ogg_sync_check(&Y));
    } else if (!strcmp(op, "yreset")) {
      printf("%ld yreset %d\n", ln, ogg_sync_reset(&Y)); haveP = 0;
    } else if (!strcmp(op, "dcheck")) {
      printf("%ld dcheck %d\n", ln, ogg_stream_check(&D));
    } else if (!strcmp(op, "dreset")) {
      printf("%ld dreset %d\n", ln, ogg_stream_reset(&D));
    } else if (!strcmp(op, "dserial")) {
      int s; sscanf(a, "%d", &s);
      printf("%ld dserial %d\n", ln, ogg_stream_reset_serialno(&D, s));
    } else if (!strcmp(op, "deos")) {
      printf("%ld deos %d\n", ln, ogg_stream_eos(&D));
    } else if (!strcmp(op, "heap")) {
      /* heap-allocated states, released with the *_destroy calls (which free the struct) */
      ogg_stream_state *os = malloc(sizeof *os);
      ogg_sync_state *oy = malloc(sizeof *oy);
      int a1 = ogg_stream_init(os, 7), a2 = ogg_sync_init(oy);
      ogg_packet p; memset(&p, 0, sizeof p);
      unsigned char d[300]; for (int i = 0; i < 300; i++) d[i] = (unsigned char)i;
      p.packet = d; p.bytes = 300; p.b_o_s = 1;
      int a3 = ogg_stream_packetin(os, &p);
      char *b = ogg_sync_buffer(oy, 100);
      if (b) memset(b, 'O', 100);
      int a4 = ogg_sync_wrote(oy, 100);
      int a5 = ogg_stream_destroy(os), a6 = ogg_sync_destroy(oy);
      printf("%ld heap %d %d %d %d %d %d\n", ln, a1, a2, a3, a4, a5, a6);
    } else if (!strcmp(op, "pclear")) {
      ogg_packet *p = malloc(sizeof *p);
      memset(p, 0x5a, sizeof *p);
      p->packet = malloc(16); p->bytes = 16;
      ogg_packet_clear(p);
      int allzero = 1;
      for (size_t i = 0; i < sizeof *p; i++) if (((unsigned char *)p)[i]) allzero = 0;
      printf("%ld pclear zero=%d\n", ln, allzero);
      free(p);
    } else if (!strcmp(op, "eclear")) {
      int r1 = ogg_stream_clear(&E);   /* sequenced: argument evaluation order is unspecified */
      int r2 = ogg_stream_check(&E);
      printf("%ld eclear %d check=%d\n", ln, r1, r2); Einit = 0;
    } else if (!strcmp(op, "yclear")) {
      int r1 = ogg_sync_clear(&Y);
      int r2 = ogg_sync_check(&Y);
      printf("%ld yclear %d check=%d\n", ln, r1, r2); Yinit = 0;
    } else {
      printf("%ld ?? %s\n", ln, op);
    }
  }
  if (Einit) ogg_stream_clear(&E);
  if (Dinit) ogg_stream_clear(&D);
  if (Yinit) ogg_sync_clear(&Y);
  free(wire);
  printf("end\n");
  return 0;
}
