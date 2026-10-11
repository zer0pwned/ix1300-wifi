# Test harness of the ix1300 backend. It needs a C compiler and the
# libjpeg headers. It does not need sane-backends: the stub headers are
# in include/sane. It does not use the network or a scanner.
#
#   make           build and run the tests on ../ix1300.c (with ASan)
#   make orig      run the same tests on orig/ix1300.c (an older copy of
#                  the backend, not in the repository). Failures there
#                  show the old faults. Without orig/, the target does
#                  nothing.
#   make test-real decode scanner JPEGs (REAL_JPEGS) through the backend.
#                  Default: the synthetic JPEGs in service/tests/data and
#                  real scans in ../../scans (if they exist)
#   make clean
#
# Set JPEG_PREFIX if the libjpeg headers are not in /opt/homebrew or /usr.

CC ?= cc
JPEG_PREFIX ?= $(firstword $(wildcard /opt/homebrew/opt/jpeg-turbo /opt/homebrew/opt/jpeg /opt/homebrew /usr/local /usr))
SAN ?= -fsanitize=address,undefined -fno-omit-frame-pointer
CFLAGS ?= -g -O1 -Wall -Wextra -Wno-unused-parameter -Wno-unused-function -Wno-deprecated-declarations
CPPFLAGS += -Isrc -Iinclude -I$(JPEG_PREFIX)/include
LDLIBS += -L$(JPEG_PREFIX)/lib -ljpeg -lpthread

HDRS = src/fakenet.h $(wildcard include/sane/*.h)

all: test

ix1300-test: src/harness.c src/fakenet.c ../ix1300.c ../ix1300.h $(HDRS)
	$(CC) $(CPPFLAGS) -DIX_SRC='"../../ix1300.c"' $(CFLAGS) $(SAN) -o $@ \
	  src/harness.c src/fakenet.c $(LDLIBS)

ix1300-test-orig: src/harness.c src/fakenet.c orig/ix1300.c orig/ix1300.h $(HDRS)
	$(CC) $(CPPFLAGS) -DIX_SRC='"../orig/ix1300.c"' $(CFLAGS) $(SAN) -o $@ \
	  src/harness.c src/fakenet.c $(LDLIBS)

test: ix1300-test
	ASAN_OPTIONS=detect_leaks=0 ./ix1300-test

ifneq ($(wildcard orig/ix1300.c),)
orig: ix1300-test-orig
	-ASAN_OPTIONS=detect_leaks=0 ./ix1300-test-orig
else
orig:
	@echo "orig/ix1300.c not found: nothing to do"
endif

# Decode scanner JPEGs through the backend (real scans are optional).
REAL_JPEGS ?= $(wildcard ../../scans/raw/*/*.jpg ../../service/tests/data/*.jpg)
test-real: ix1300-test
	ASAN_OPTIONS=detect_leaks=0 IXTEST_JPEGS="$(REAL_JPEGS)" ./ix1300-test real_jpegs

clean:
	rm -rf ix1300-test ix1300-test-orig *.dSYM

.PHONY: all test test-real orig clean
