/* qsort: quickSort(int arr[], int low, int high), partition(int arr[], int low, int high).
 * ops: "qs n a0..a{n-1} low high" -> sorted array ; "part n a.. low high" -> ret + array */
#include "common.h"
void quickSort(int arr[], int low, int high);
int partition(int arr[], int low, int high);
static void rg_dispatch(void) {
  const char *op = rg_tok[0];
  int n = (int)rg_int(rg_tok[1]);
  int *a = malloc(sizeof(int) * (n > 0 ? n : 1));
  for (int i = 0; i < n; i++) a[i] = (int)rg_int(rg_tok[2 + i]);
  int low = (int)rg_int(rg_tok[2 + n]), high = (int)rg_int(rg_tok[3 + n]);
  if (!strcmp(op, "qs")) {
    quickSort(a, low, high);
  } else if (!strcmp(op, "part")) {
    rg_pi(partition(a, low, high));
  }
  for (int i = 0; i < n; i++) rg_pi(a[i]);
  free(a);
}
