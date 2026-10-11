/* Test stub of sanei_backend.h. DBG goes to the harness. */
#ifndef IXTEST_SANEI_BACKEND_H
#define IXTEST_SANEI_BACKEND_H
#include "sane.h"
extern int h_dbg_gate;
extern void h_dbg (int level, const char *fmt, ...)
  __attribute__ ((format (printf, 2, 3)));
#define DBG_LEVEL h_dbg_gate
#define DBG h_dbg
#define DBG_INIT() do { } while (0)
#define SANE_I18N(text) text
#endif
