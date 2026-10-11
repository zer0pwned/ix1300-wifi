#!/bin/sh
# Build and install the SANE ix1300 backend on Debian (tested on Debian 13).
#
# Usage: sudo ./install-on-debian.sh [SANE_SOURCE_DIR]
#
# The script does these steps:
#  1. It installs the build packages with apt.
#  2. It gets the sane-backends 1.3.1 source (git tag 1.3.1).
#  3. It adds the ix1300 backend to the source tree.
#  4. It builds only the ix1300 backend.
#  5. It installs the library, the configuration file and the manual page.
#
# Set NO_APT=1 to skip step 1. Set HOSTID=<16 hex digits> to write the
# host ID into a new /etc/sane.d/ix1300.conf.

set -e

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${1:-$HERE/sane-backends}
TAG=1.3.1
MULTIARCH=$(dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null || gcc -print-multiarch)
SANEDIR=/usr/lib/$MULTIARCH/sane

echo "== 1. Build packages"
if [ -z "$NO_APT" ]; then
  apt-get install -y git autoconf autoconf-archive automake libtool gettext \
    autopoint pkg-config build-essential libjpeg62-turbo-dev
fi

echo "== 2. sane-backends source ($SRC)"
if [ ! -d "$SRC/.git" ]; then
  git clone --depth 1 --branch "$TAG" \
    https://gitlab.com/sane-project/backends.git "$SRC"
fi

echo "== 3. Add the backend"
if ! grep -q ix1300 "$SRC/configure.ac"; then
  (cd "$SRC" && git apply "$HERE/sane-backends-ix1300.patch")
fi
# Copy the current sources. They can be newer than the patch.
cp "$HERE/ix1300.c" "$HERE/ix1300.h" "$HERE/ix1300.conf.in" "$SRC/backend/"
cp "$HERE/sane-ix1300.man" "$SRC/doc/"
cp "$HERE/ix1300.desc" "$SRC/doc/descriptions/"

echo "== 4. Build"
cd "$SRC"
if [ ! -f configure ] || [ configure.ac -nt configure ]; then
  ./autogen.sh
fi
if [ ! -f backend/Makefile ] || ! grep -q 'libsane-ix1300.la' backend/Makefile; then
  ./configure BACKENDS="ix1300" --prefix=/usr --sysconfdir=/etc \
    --localstatedir=/var --libdir=/usr/lib/$MULTIARCH \
    --disable-locking --without-usb --without-snmp --without-avahi \
    --without-libcurl --without-poppler-glib --disable-nls
fi
make -C include
make -C lib
make -C sanei
make -C backend libsane-ix1300.la ix1300.conf
make -C doc sane-ix1300.5

echo "== 5. Install"
SO=$(ls backend/.libs/libsane-ix1300.so.1.*.* | head -1)
install -m 644 "$SO" "$SANEDIR/"
ln -sf "$(basename "$SO")" "$SANEDIR/libsane-ix1300.so.1"
if [ ! -f /etc/sane.d/ix1300.conf ]; then
  install -m 644 backend/ix1300.conf /etc/sane.d/ix1300.conf
  if [ -n "$HOSTID" ]; then
    sed -i "s/^#hostid .*/hostid $HOSTID/" /etc/sane.d/ix1300.conf
  fi
fi
grep -q '^ix1300$' /etc/sane.d/dll.conf || echo ix1300 >> /etc/sane.d/dll.conf
install -m 644 doc/sane-ix1300.5 /usr/share/man/man5/sane-ix1300.5
echo "Installed $SANEDIR/$(basename "$SO")"
echo "Test: scanimage -L"
