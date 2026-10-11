# Standalone build of the SANE ix1300 backend.
#
# This Makefile needs a sane-backends source tree that "configure" has
# prepared (it uses include/sane/config.h and the sanei sources).
#
#   make SANE_SRC=/path/to/sane-backends
#   sudo make install SANE_SRC=/path/to/sane-backends
#
# The main build method is install-on-debian.sh.

SANE_SRC ?= ./sane-backends
CC ?= cc
CFLAGS ?= -O2 -g -Wall
MULTIARCH := $(shell $(CC) -print-multiarch 2>/dev/null)
SANEDIR ?= /usr/lib/$(MULTIARCH)/sane
CONFDIR ?= /etc/sane.d
VERSION = 1.3.1

LIB = libsane-ix1300.so.$(VERSION)
SONAME = libsane-ix1300.so.1

CPPFLAGS += -I$(SANE_SRC)/backend -I$(SANE_SRC)/include -DPATH_SANE_CONFIG_DIR=$(CONFDIR) \
            -DPATH_SANE_DATA_DIR=/usr/share -DPATH_SANE_LOCK_DIR=/var/lock/sane \
            -DV_MAJOR=1 -DV_MINOR=3
OBJS = ix1300.o ix1300-s.o sanei_init_debug.o sanei_constrain_value.o \
       sanei_config.o sane_strstatus.o

all: $(LIB)

ix1300.o: ix1300.c ix1300.h
	$(CC) $(CPPFLAGS) -DBACKEND_NAME=ix1300 $(CFLAGS) -fPIC -c -o $@ ix1300.c

ix1300-s.o: $(SANE_SRC)/backend/stubs.c
	$(CC) $(CPPFLAGS) -DBACKEND_NAME=ix1300 $(CFLAGS) -fPIC -c -o $@ $<

sanei_%.o: $(SANE_SRC)/sanei/sanei_%.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -fPIC -c -o $@ $<

sane_strstatus.o: $(SANE_SRC)/backend/sane_strstatus.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -fPIC -c -o $@ $<

$(LIB): $(OBJS)
	$(CC) -shared -Wl,-soname,$(SONAME) -o $@ $(OBJS) -ljpeg -lpthread

install: $(LIB)
	install -m 644 $(LIB) $(SANEDIR)/
	ln -sf $(LIB) $(SANEDIR)/$(SONAME)
	test -f $(CONFDIR)/ix1300.conf || install -m 644 ix1300.conf.in $(CONFDIR)/ix1300.conf
	grep -q '^ix1300$$' $(CONFDIR)/dll.conf || echo ix1300 >> $(CONFDIR)/dll.conf

# The test harness (stub SANE headers, fake scanner, no network).
test:
	$(MAKE) -C tests

clean:
	rm -f $(OBJS) $(LIB)

.PHONY: all install test clean
