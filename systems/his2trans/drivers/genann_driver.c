/* genann. ops:
 *  "sig|sigc|thr|lin d"                  activation functions
 *  "init i hl h o"                       genann_init -> NULL or fields + every weight
 *  "randz i hl h o"                      init, zero weights, genann_randomize -> weights
 *  "run i hl h o acth acto scale x..."   init, weights := W(k, scale), set activations, genann_run
 *                                        -> outputs + every neuron output
 *  "train i hl h o acth acto scale lr reps x... t..."  as run, then genann_train reps times -> weights
 *  "copy i hl h o scale"                 copy, then mutate original -> copy fields + weights
 *  "write i hl h o scale"                genann_write(ann, stdout)
 *  "read <hex>"                          genann_read(FILE* holding the bytes) -> NULL or fields + weights
 * W(k, scale) = (double)((k*37) % 101 - 50) / 100.0 * scale  (same IEEE operations on both sides).
 * Activation codes: 0 sigmoid_cached (the default), 1 sigmoid, 2 threshold, 3 linear. */
#include "common.h"
#include "genann.h"
static genann_actfun act_of(int c) {
  switch (c) {
    case 1: return genann_act_sigmoid;
    case 2: return genann_act_threshold;
    case 3: return genann_act_linear;
    default: return genann_act_sigmoid_cached;
  }
}
static double W(int k, double scale) { return (double)((k * 37) % 101 - 50) / 100.0 * scale; }
static void dump(const genann *a) {
  if (!a) { printf(" NULL"); return; }
  rg_pi(a->inputs); rg_pi(a->hidden_layers); rg_pi(a->hidden); rg_pi(a->outputs);
  rg_pi(a->total_weights); rg_pi(a->total_neurons);
  printf(" w");
  for (int i = 0; i < a->total_weights; i++) rg_pd(a->weight[i]);
}
static void rg_dispatch(void) {
  const char *op = rg_tok[0];
  if (!strcmp(op, "sig")) { rg_pd(genann_act_sigmoid(rg_dbl(rg_tok[1]))); return; }
  if (!strcmp(op, "sigc")) { rg_pd(genann_act_sigmoid_cached(rg_dbl(rg_tok[1]))); return; }
  if (!strcmp(op, "thr")) { rg_pd(genann_act_threshold(rg_dbl(rg_tok[1]))); return; }
  if (!strcmp(op, "lin")) { rg_pd(genann_act_linear(rg_dbl(rg_tok[1]))); return; }
  if (!strcmp(op, "seed")) { srand((unsigned)rg_int(rg_tok[1])); return; }   /* srand(s): C library PRNG seed */
  if (!strcmp(op, "read")) {
    size_t n;
    char *bytes = rg_hex(rg_tok[1], &n);
    FILE *f = tmpfile();
    fwrite(bytes, 1, n, f);
    rewind(f);
    fflush(stdout);
    genann *a = genann_read(f);
    fflush(stderr);
    dump(a);
    if (a) genann_free(a);
    fclose(f);
    free(bytes);
    return;
  }
  int i = (int)rg_int(rg_tok[1]), hl = (int)rg_int(rg_tok[2]), h = (int)rg_int(rg_tok[3]), o = (int)rg_int(rg_tok[4]);
  genann *a = genann_init(i, hl, h, o);
  if (!strcmp(op, "init")) { dump(a); if (a) genann_free(a); return; }
  if (!a) { printf(" NULL"); return; }
  if (!strcmp(op, "randz")) {
    for (int k = 0; k < a->total_weights; k++) a->weight[k] = 0.0;
    genann_randomize(a);
    dump(a);
  } else if (!strcmp(op, "run") || !strcmp(op, "train")) {
    int acth = (int)rg_int(rg_tok[5]), acto = (int)rg_int(rg_tok[6]);
    if (strcmp(rg_tok[7], "r")) { /* "r" = keep the weights genann_init drew */
      double scale = rg_dbl(rg_tok[7]);
      for (int k = 0; k < a->total_weights; k++) a->weight[k] = W(k, scale);
    }
    a->activation_hidden = act_of(acth);
    a->activation_output = act_of(acto);
    if (!strcmp(op, "run")) {
      double *x = malloc(sizeof(double) * i);
      for (int k = 0; k < i; k++) x[k] = rg_dbl(rg_tok[8 + k]);
      const double *out = genann_run(a, x);
      printf(" out");
      for (int k = 0; k < o; k++) rg_pd(out[k]);
      printf(" neurons");
      for (int k = 0; k < a->total_neurons; k++) rg_pd(a->output[k]);
      free(x);
    } else {
      double lr = rg_dbl(rg_tok[8]);
      int reps = (int)rg_int(rg_tok[9]);
      double *x = malloc(sizeof(double) * i), *t = malloc(sizeof(double) * o);
      for (int k = 0; k < i; k++) x[k] = rg_dbl(rg_tok[10 + k]);
      for (int k = 0; k < o; k++) t[k] = rg_dbl(rg_tok[10 + i + k]);
      for (int r = 0; r < reps; r++) genann_train(a, x, t, lr);
      dump(a);
      const double *out = genann_run(a, x);
      printf(" out");
      for (int k = 0; k < o; k++) rg_pd(out[k]);
      free(x); free(t);
    }
  } else if (!strcmp(op, "copy")) {
    double scale = rg_dbl(rg_tok[5]);
    for (int k = 0; k < a->total_weights; k++) a->weight[k] = W(k, scale);
    genann *c = genann_copy(a);
    for (int k = 0; k < a->total_weights; k++) a->weight[k] = -1.0;
    dump(c);
    genann_free(c);
  } else if (!strcmp(op, "write")) {
    double scale = rg_dbl(rg_tok[5]);
    for (int k = 0; k < a->total_weights; k++) a->weight[k] = W(k, scale);
    printf(" ");
    genann_write(a, stdout);
  }
  genann_free(a);
}
