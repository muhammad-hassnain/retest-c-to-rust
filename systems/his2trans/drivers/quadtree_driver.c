/* quadtree. ops:
 *  "new minx miny maxx maxy"  quadtree_new -> NULL|ok (+ walk dump)
 *  "ins x y key"              quadtree_insert -> ret, tree->length
 *  "srch x y"                 quadtree_search -> NULL | point x y
 *  "walk"                     quadtree_walk(root, descent, ascent): one "D..." token group per node
 *                             (isleaf isempty ispointer, bounds nw.x nw.y se.x se.y width height,
 *                             point or "-", key) and "A" on ascent
 *  "bounds x y ..."           quadtree_bounds_new + quadtree_bounds_extend over the (x,y) pairs
 *  "nwb minx miny maxx maxy"  quadtree_node_with_bounds -> predicates + bounds; then node_free
 *  "pnew x y"                 quadtree_point_new -> x y
 * Keys are small integers cast to void*; the tree's key_free is NULL (as quadtree_new leaves it). */
#include "common.h"
#include "quadtree.h"
static quadtree_t *tree = NULL;
static void pbounds(quadtree_bounds_t *b) {
  if (!b) { printf(" nob"); return; }
  rg_pd(b->nw->x); rg_pd(b->nw->y); rg_pd(b->se->x); rg_pd(b->se->y); rg_pd(b->width); rg_pd(b->height);
}
static void descent(quadtree_node_t *n) {
  printf(" D");
  rg_pi(quadtree_node_isleaf(n)); rg_pi(quadtree_node_isempty(n)); rg_pi(quadtree_node_ispointer(n));
  pbounds(n->bounds);
  if (n->point) { rg_pd(n->point->x); rg_pd(n->point->y); } else printf(" -");
  rg_pu((unsigned long long)(uintptr_t)n->key);
}
static void ascent(quadtree_node_t *n) { (void)n; printf(" A"); }
static void nofree(void *k) { (void)k; }
static void rg_dispatch(void) {
  const char *op = rg_tok[0];
  if (!strcmp(op, "new")) {
    tree = quadtree_new(rg_dbl(rg_tok[1]), rg_dbl(rg_tok[2]), rg_dbl(rg_tok[3]), rg_dbl(rg_tok[4]));
    if (!tree) { printf(" NULL"); return; }
    rg_pu(tree->length);
    quadtree_walk(tree->root, descent, ascent);
  } else if (!strcmp(op, "ins")) {
    int r = quadtree_insert(tree, rg_dbl(rg_tok[1]), rg_dbl(rg_tok[2]), (void *)(uintptr_t)rg_int(rg_tok[3]));
    rg_pi(r);
    rg_pu(tree->length);
  } else if (!strcmp(op, "srch")) {
    quadtree_point_t *p = quadtree_search(tree, rg_dbl(rg_tok[1]), rg_dbl(rg_tok[2]));
    if (!p) printf(" NULL"); else { rg_pd(p->x); rg_pd(p->y); }
  } else if (!strcmp(op, "walk")) {
    rg_pu(tree->length);
    quadtree_walk(tree->root, descent, ascent);
  } else if (!strcmp(op, "bounds")) {
    quadtree_bounds_t *b = quadtree_bounds_new();
    for (int i = 1; i + 1 < rg_ntok; i += 2) quadtree_bounds_extend(b, rg_dbl(rg_tok[i]), rg_dbl(rg_tok[i + 1]));
    pbounds(b);
    quadtree_bounds_free(b);
  } else if (!strcmp(op, "nwb")) {
    quadtree_node_t *n = quadtree_node_with_bounds(rg_dbl(rg_tok[1]), rg_dbl(rg_tok[2]), rg_dbl(rg_tok[3]), rg_dbl(rg_tok[4]));
    descent(n);
    quadtree_node_free(n, nofree);
  } else if (!strcmp(op, "pnew")) {
    quadtree_point_t *p = quadtree_point_new(rg_dbl(rg_tok[1]), rg_dbl(rg_tok[2]));
    rg_pd(p->x); rg_pd(p->y);
    quadtree_point_free(p);
  }
}
