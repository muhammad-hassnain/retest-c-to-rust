/* Determinism shim (LD_PRELOAD) for CRUST-Bench tests whose C output is nondeterministic run to
 * run (carrays seeds rand() from gettimeofday()+getpid(); fs_c prints elapsed time). It pins the
 * clock and the pid to fixed values. Used for the C and every Rust build alike, and only for
 * samples whose C reference output differs between two plain runs (crust/run.py records which).
 */
#define _GNU_SOURCE
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

int gettimeofday(struct timeval *tv, void *tz) {
  (void)tz;
  if (tv) { tv->tv_sec = 1700000000; tv->tv_usec = 123456; }
  return 0;
}
int clock_gettime(clockid_t c, struct timespec *ts) {
  (void)c;
  if (ts) { ts->tv_sec = 1700000000; ts->tv_nsec = 123456789; }
  return 0;
}
time_t time(time_t *t) {
  if (t) *t = 1700000000;
  return 1700000000;
}
clock_t clock(void) { return 0; }
pid_t getpid(void) { return 4242; }
