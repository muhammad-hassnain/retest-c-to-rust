/* Held-out measurement for libogg: what did SACTOR's own check actually run?
 *
 * SACTOR's E2E check for libogg runs test_bitwise.o / test_framing.o (libogg's _V_SELFTEST mains,
 * shipped in sactor-datasets/ogg). We link those objects against the ORIGINAL C with
 * -Wl,--wrap=<api fn>, so every call the test makes into the library passes through these
 * wrappers, which log the call's arguments (and the stream state the defect conditions depend on)
 * to the file named by $SACTOR_TRACE, then call the real C function.
 *
 * Logged conditions (each = the trigger of one counted defect, read from the C state BEFORE the
 * call):
 *   B0  oggpackB_write(b, value, 0) with ((uint32)value >> (24 + endbit)) != 0
 *   F1  page emission (pageout/flush/_fill) on a stream whose first page is still pending
 *       (b_o_s == 0) and whose first min(lacing_fill,255) lacing values are all 255
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <ogg/ogg.h>

static FILE *T;
static FILE *tf(void) {
  if (!T) {
    const char *p = getenv("SACTOR_TRACE");
    T = fopen(p ? p : "/dev/null", "a");
  }
  return T;
}

#ifdef TRACE_BITWISE
void __real_oggpack_write(oggpack_buffer *, unsigned long, int);
void __real_oggpackB_write(oggpack_buffer *, unsigned long, int);
long __real_oggpack_read(oggpack_buffer *, int);
long __real_oggpackB_read(oggpack_buffer *, int);
long __real_oggpack_look(oggpack_buffer *, int);
long __real_oggpackB_look(oggpack_buffer *, int);
void __real_oggpack_writecopy(oggpack_buffer *, void *, long);
void __real_oggpackB_writecopy(oggpack_buffer *, void *, long);
void __real_oggpack_writetrunc(oggpack_buffer *, long);
void __real_oggpackB_writetrunc(oggpack_buffer *, long);

void __wrap_oggpack_write(oggpack_buffer *b, unsigned long v, int n) {
  fprintf(tf(), "oggpack_write %lu %d endbit=%d\n", v, n, b->endbit);
  __real_oggpack_write(b, v, n);
}
void __wrap_oggpackB_write(oggpack_buffer *b, unsigned long v, int n) {
  int cond = (n == 0) && ((((uint32_t)v) >> (24 + b->endbit)) != 0);
  fprintf(tf(), "oggpackB_write %lu %d endbit=%d%s\n", v, n, b->endbit, cond ? " COND_B0" : "");
  __real_oggpackB_write(b, v, n);
}
long __wrap_oggpack_read(oggpack_buffer *b, int n) { fprintf(tf(), "oggpack_read %d\n", n); return __real_oggpack_read(b, n); }
long __wrap_oggpackB_read(oggpack_buffer *b, int n) { fprintf(tf(), "oggpackB_read %d\n", n); return __real_oggpackB_read(b, n); }
long __wrap_oggpack_look(oggpack_buffer *b, int n) { fprintf(tf(), "oggpack_look %d\n", n); return __real_oggpack_look(b, n); }
long __wrap_oggpackB_look(oggpack_buffer *b, int n) { fprintf(tf(), "oggpackB_look %d\n", n); return __real_oggpackB_look(b, n); }
void __wrap_oggpack_writecopy(oggpack_buffer *b, void *s, long n) { fprintf(tf(), "oggpack_writecopy %ld endbit=%d\n", n, b->endbit); __real_oggpack_writecopy(b, s, n); }
void __wrap_oggpackB_writecopy(oggpack_buffer *b, void *s, long n) { fprintf(tf(), "oggpackB_writecopy %ld endbit=%d\n", n, b->endbit); __real_oggpackB_writecopy(b, s, n); }
void __wrap_oggpack_writetrunc(oggpack_buffer *b, long n) { fprintf(tf(), "oggpack_writetrunc %ld\n", n); __real_oggpack_writetrunc(b, n); }
void __wrap_oggpackB_writetrunc(oggpack_buffer *b, long n) { fprintf(tf(), "oggpackB_writetrunc %ld\n", n); __real_oggpackB_writetrunc(b, n); }
#endif

#ifdef TRACE_FRAMING
int __real_ogg_stream_packetin(ogg_stream_state *, ogg_packet *);
int __real_ogg_stream_iovecin(ogg_stream_state *, ogg_iovec_t *, int, long, ogg_int64_t);
int __real_ogg_stream_pageout(ogg_stream_state *, ogg_page *);
int __real_ogg_stream_flush(ogg_stream_state *, ogg_page *);
int __real_ogg_stream_pageout_fill(ogg_stream_state *, ogg_page *, int);
int __real_ogg_stream_flush_fill(ogg_stream_state *, ogg_page *, int);

static int cond_f1(ogg_stream_state *os) {
  if (!os || os->b_o_s != 0 || os->lacing_fill <= 0) return 0;
  long m = os->lacing_fill > 255 ? 255 : os->lacing_fill;
  for (long i = 0; i < m; i++)
    if ((os->lacing_vals[i] & 0xff) < 255) return 0;
  return 1;
}
int __wrap_ogg_stream_packetin(ogg_stream_state *os, ogg_packet *op) {
  fprintf(tf(), "ogg_stream_packetin bytes=%ld bos=%ld eos=%ld\n", op->bytes, op->b_o_s, op->e_o_s);
  return __real_ogg_stream_packetin(os, op);
}
int __wrap_ogg_stream_iovecin(ogg_stream_state *os, ogg_iovec_t *iov, int count, long e_o_s, ogg_int64_t g) {
  fprintf(tf(), "ogg_stream_iovecin count=%d eos=%ld\n", count, e_o_s);
  return __real_ogg_stream_iovecin(os, iov, count, e_o_s, g);
}
#define EMIT(name, call)                                                                 \
  fprintf(tf(), name " lacing_fill=%ld bos_pending=%d%s\n", os->lacing_fill, os->b_o_s == 0, \
          cond_f1(os) ? " COND_F1" : "");                                                \
  return call;
int __wrap_ogg_stream_pageout(ogg_stream_state *os, ogg_page *og) { EMIT("ogg_stream_pageout", __real_ogg_stream_pageout(os, og)) }
int __wrap_ogg_stream_flush(ogg_stream_state *os, ogg_page *og) { EMIT("ogg_stream_flush", __real_ogg_stream_flush(os, og)) }
int __wrap_ogg_stream_pageout_fill(ogg_stream_state *os, ogg_page *og, int n) { EMIT("ogg_stream_pageout_fill", __real_ogg_stream_pageout_fill(os, og, n)) }
int __wrap_ogg_stream_flush_fill(ogg_stream_state *os, ogg_page *og, int n) { EMIT("ogg_stream_flush_fill", __real_ogg_stream_flush_fill(os, og, n)) }
#endif
