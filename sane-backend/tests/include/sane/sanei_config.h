/* Test stub of sanei_config.h. The harness gives the file text. */
#ifndef IXTEST_SANEI_CONFIG_H
#define IXTEST_SANEI_CONFIG_H
#include <stdio.h>
extern FILE *sanei_config_open (const char *name);
extern char *sanei_config_read (char *str, int n, FILE *stream);
#endif
