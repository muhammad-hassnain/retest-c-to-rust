/* Differential driver, C side: drives the ORIGINAL CROWN quadtree library
 * (artifacts/ptrtrans/dataset/crown_dataset/quadtree/src/*.c) through its public API
 * (src/quadtree.h) from a command script on stdin, printing every return value and the
 * observable tree state. The Rust side (qt_sut.rs) reads the same script and prints the same
 * format, so the two outputs are compared token by token (floats as IEEE-754 bit patterns,
 * compared with the study's relative tolerance; see run.py).
 *
 * Commands (one per line; floats are 16 hex digits = the IEEE-754 bits; k = key id, -1 = NULL):
 *   N minx miny maxx maxy   quadtree_new; prints root bounds      (n = same, silent)
 *   I x y k                 quadtree_insert; prints ret + length  (i = same, silent)
 *   S x y                   quadtree_search; prints hit point or null
 *   W                       quadtree_walk over the tree; per node: depth, isleaf/isempty/ispointer,
 *                           bounds (nw.x nw.y se.x se.y width height), point, key id
 *   L                       tree->length
 *   K                       set tree->key_free to a logging callback (public struct field)
 *   B                       quadtree_bounds_new; prints bounds     BX x y  quadtree_bounds_extend
 *   NB minx miny maxx maxy  quadtree_node_with_bounds; prints bounds + predicates
 *   NN                      quadtree_node_new; prints predicates
 *   PN x y                  quadtree_point_new; prints point
 * At end of input the tree is released with quadtree_free (key_free reset to NULL first, so the
 * teardown is not part of the comparison: PtrTrans maps deallocation functions to Rust's Drop).
 * stdout is flushed after every command, so a crash keeps the prefix.
 */
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "quadtree.h"

#define NKEYS 65536
static int KEYS[NKEYS];

static void *key_ptr(long id) { return (id < 0 || id >= NKEYS) ? NULL : (void *)&KEYS[id]; }
static long key_id(void *k) { return k == NULL ? -1 : (long)((int *)k - KEYS); }

static double fx(const char *s) {
  uint64_t u = strtoull(s, NULL, 16);
  double d;
  memcpy(&d, &u, sizeof d);
  return d;
}

static void pf(double d) {
  uint64_t u;
  memcpy(&u, &d, sizeof u);
  printf(" f%016" PRIx64, u);
}

static void pbounds(quadtree_bounds_t *b) {
  if (b == NULL) { printf(" nobounds"); return; }
  pf(b->nw->x); pf(b->nw->y); pf(b->se->x); pf(b->se->y); pf(b->width); pf(b->height);
}

static void ppreds(quadtree_node_t *n) {
  printf(" %d%d%d", quadtree_node_isleaf(n) != 0, quadtree_node_isempty(n) != 0,
         quadtree_node_ispointer(n) != 0);
}

static int depth;
static void descent(quadtree_node_t *n) {
  printf("D %d", depth);
  ppreds(n);
  pbounds(n->bounds);
  if (n->point) { printf(" P"); pf(n->point->x); pf(n->point->y); }
  else printf(" P-");
  printf(" K%ld\n", key_id(n->key));
  depth++;
}
static void ascent(quadtree_node_t *n) { (void)n; depth--; printf("A %d\n", depth); }

static void log_key_free(void *k) { printf("KF %ld\n", key_id(k)); }
static void noop_key_free(void *k) { (void)k; }

int main(void) {
  char line[512];
  quadtree_t *tree = NULL;
  quadtree_bounds_t *bounds = NULL;
  while (fgets(line, sizeof line, stdin)) {
    char *tok[8];
    int nt = 0;
    for (char *p = strtok(line, " \t\r\n"); p && nt < 8; p = strtok(NULL, " \t\r\n")) tok[nt++] = p;
    if (nt == 0 || tok[0][0] == '#') continue;
    const char *c = tok[0];
    if (!strcmp(c, "N") || !strcmp(c, "n")) {
      tree = quadtree_new(fx(tok[1]), fx(tok[2]), fx(tok[3]), fx(tok[4]));
      if (c[0] == 'N') {
        if (!tree) printf("N null\n");
        else { printf("N"); pbounds(tree->root->bounds); printf(" L%u\n", tree->length); }
      }
    } else if (!strcmp(c, "I") || !strcmp(c, "i")) {
      int r = quadtree_insert(tree, fx(tok[1]), fx(tok[2]), key_ptr(strtol(tok[3], NULL, 10)));
      if (c[0] == 'I') printf("I %d L%u\n", r, tree->length);
    } else if (!strcmp(c, "S")) {
      quadtree_point_t *p = quadtree_search(tree, fx(tok[1]), fx(tok[2]));
      if (p) { printf("S hit"); pf(p->x); pf(p->y); printf("\n"); }
      else printf("S null\n");
    } else if (!strcmp(c, "W")) {
      depth = 0;
      quadtree_walk(tree->root, descent, ascent);
    } else if (!strcmp(c, "L")) {
      printf("L %u\n", tree->length);
    } else if (!strcmp(c, "K")) {
      tree->key_free = log_key_free;
    } else if (!strcmp(c, "B")) {
      if (bounds) quadtree_bounds_free(bounds);
      bounds = quadtree_bounds_new();
      printf("B"); pbounds(bounds); printf("\n");
    } else if (!strcmp(c, "BX")) {
      quadtree_bounds_extend(bounds, fx(tok[1]), fx(tok[2]));
      printf("BX"); pbounds(bounds); printf("\n");
    } else if (!strcmp(c, "NB")) {
      quadtree_node_t *n = quadtree_node_with_bounds(fx(tok[1]), fx(tok[2]), fx(tok[3]), fx(tok[4]));
      printf("NB"); ppreds(n); pbounds(n->bounds); printf("\n");
      quadtree_node_free(n, noop_key_free);
    } else if (!strcmp(c, "NN")) {
      quadtree_node_t *n = quadtree_node_new();
      printf("NN"); ppreds(n); pbounds(n->bounds); printf("\n");
      free(n);
    } else if (!strcmp(c, "PN")) {
      quadtree_point_t *p = quadtree_point_new(fx(tok[1]), fx(tok[2]));
      printf("PN"); pf(p->x); pf(p->y); printf("\n");
      quadtree_point_free(p);
    } else {
      printf("? %s\n", c);
    }
    fflush(stdout);
  }
  if (bounds) quadtree_bounds_free(bounds);
  if (tree) { tree->key_free = NULL; quadtree_free(tree); }
  return 0;
}
