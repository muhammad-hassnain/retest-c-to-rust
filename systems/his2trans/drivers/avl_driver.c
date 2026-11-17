/* avl (Huawei VOS AVL, both the AVL3 offset-based API and the v2 pSelf/pKey API).
 * A fixed pool of elements E[k] = {AVL3_NODE n3; int key; AVL_NODE n2}; results that are element
 * pointers are printed as the element index (-1 = NULL).
 * AVL3 ops: i3 k v (key:=v, INIT_NODE, Insert_Or_Find) | d3 k | f3 v | fn3 v flag | first3 | last3 |
 *           next3 k | prev3 k | dump3
 * v2 ops:   i2 k v (key:=v, INIT_NODE(self=&E[k], key=&E[k].key), Insert_Or_Find) | d2 k | f2 v |
 *           fn2 v flag | next2 k | prev2 k | dump2
 * dump = root/first/last, then an in-order walk printing (index key lheight rheight parent left right). */
#include <stddef.h>
#include "avl.c"
#include "common.h"
typedef struct { AVL3_NODE n3; int key; AVL_NODE n2; } Elem;
static Elem E[256];
static AVL3_TREE t3;
static AVL_TREE t2;
static AVL3_TREE_INFO info;
static int cmp(const void *a, const void *b) {
  int x = *(const int *)a, y = *(const int *)b;
  return x < y ? -1 : x > y ? 1 : 0;
}
static long ix3(const void *n) { return n ? (long)(((const Elem *)((const char *)n - offsetof(Elem, n3))) - E) : -1; }
static long ix2(const void *n) { return n ? (long)(((const Elem *)((const char *)n - offsetof(Elem, n2))) - E) : -1; }
static long ixe(const void *p) { return p ? (long)((const Elem *)p - E) : -1; }
static void walk3(AVL3_NODE *n) {
  if (!n) return;
  walk3(n->pstLeft);
  printf(" (%ld %d %d %d %ld %ld %ld)", ix3(n), ((Elem *)((char *)n - offsetof(Elem, n3)))->key, n->sLHeight, n->sRHeight,
         ix3(n->pstParent), ix3(n->pstLeft), ix3(n->pstRight));
  walk3(n->pstRight);
}
static void walk2(AVL_NODE *n) {
  if (!n) return;
  walk2(n->pstLeft);
  printf(" (%ld %d %d %d %ld %ld %ld %ld)", ix2(n), *(int *)n->pKey, n->sLHeight, n->sRHeight, ix2(n->pstParent),
         ix2(n->pstLeft), ix2(n->pstRight), ixe(n->pSelf));
  walk2(n->pstRight);
}
static int inited = 0;
static void rg_dispatch(void) {
  if (!inited) {
    inited = 1;
    VOS_AVL3_INIT_TREE(t3, info);
    VOS_AVL_INIT_TREE(t2, cmp);
    info.pfCompare = cmp;
    info.usKeyOffset = (unsigned short)offsetof(Elem, key);
    info.usNodeOffset = (unsigned short)offsetof(Elem, n3);
  }
  const char *op = rg_tok[0];
  if (!strcmp(op, "i3")) {
    int k = (int)rg_int(rg_tok[1]);
    E[k].key = (int)rg_int(rg_tok[2]);
    VOS_AVL3_INIT_NODE(E[k].n3);
    rg_pi(ixe(VOS_AVL3_Insert_Or_Find(&t3, &E[k].n3, &info)));
  } else if (!strcmp(op, "d3")) { VOS_AVL3_Delete(&t3, &E[rg_int(rg_tok[1])].n3); }
  else if (!strcmp(op, "f3")) { int v = (int)rg_int(rg_tok[1]); rg_pi(ixe(VOS_AVL3_Find(&t3, &v, &info))); }
  else if (!strcmp(op, "fn3")) { int v = (int)rg_int(rg_tok[1]); rg_pi(ixe(AVL3_Find_Or_Find_Next(&t3, &v, (unsigned)rg_int(rg_tok[2]), &info))); }
  else if (!strcmp(op, "first3")) { rg_pi(ixe(VOS_AVL3_First(&t3, &info))); }
  else if (!strcmp(op, "last3")) { rg_pi(ixe(VOS_AVL3_Last(&t3, &info))); }
  else if (!strcmp(op, "next3")) { rg_pi(ixe(VOS_AVL3_Next(&E[rg_int(rg_tok[1])].n3, &info))); }
  else if (!strcmp(op, "prev3")) { rg_pi(ixe(VOS_AVL3_Prev(&E[rg_int(rg_tok[1])].n3, &info))); }
  else if (!strcmp(op, "dump3")) {
    rg_pi(ix3(t3.pstRoot)); rg_pi(ix3(t3.pstFirst)); rg_pi(ix3(t3.pstLast));
    walk3(t3.pstRoot);
  } else if (!strcmp(op, "i2")) {
    int k = (int)rg_int(rg_tok[1]);
    E[k].key = (int)rg_int(rg_tok[2]);
    VOS_AVL_INIT_NODE(E[k].n2, &E[k], &E[k].key);
    rg_pi(ixe(VOS_AVL_Insert_Or_Find(&t2, &E[k].n2)));
  } else if (!strcmp(op, "ri2")) {
    rg_pi(ixe(VOS_AVL_Insert_Or_Find(&t2, &E[rg_int(rg_tok[1])].n2)));   /* node already in the tree */
    rg_pi(ixe(t2.pstRoot ? ((AVL_NODE *)t2.pstRoot)->pSelf : NULL));
  } else if (!strcmp(op, "d2")) { VOS_AVL_Delete(&t2, &E[rg_int(rg_tok[1])].n2); }
  else if (!strcmp(op, "f2")) { int v = (int)rg_int(rg_tok[1]); rg_pi(ixe(VOS_AVL_Find(&t2, &v))); }
  else if (!strcmp(op, "fn2")) { int v = (int)rg_int(rg_tok[1]); rg_pi(ixe(VOS_AVL_Find_Or_Find_Next(&t2, &v, (unsigned)rg_int(rg_tok[2])))); }
  else if (!strcmp(op, "next2")) { rg_pi(ixe(VOS_AVL_Next(&E[rg_int(rg_tok[1])].n2))); }
  else if (!strcmp(op, "prev2")) { rg_pi(ixe(VOS_AVL_Prev(&E[rg_int(rg_tok[1])].n2))); }
  else if (!strcmp(op, "dump2")) {
    rg_pi(ix2(t2.pstRoot)); rg_pi(ix2(t2.pstFirst)); rg_pi(ix2(t2.pstLast));
    walk2(t2.pstRoot);
  }
}
