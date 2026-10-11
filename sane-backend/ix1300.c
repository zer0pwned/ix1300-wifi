/* sane - Scanner Access Now Easy.

   This file is part of the SANE package. It is a SANE backend for the
   PFU (Fujitsu) ScanSnap iX1300 on a Wi-Fi network.

   Copyright (C) 2026 The ix1300 backend authors

   --------------------------------------------------------------------------

   This program is free software; you can redistribute it and/or
   modify it under the terms of the GNU General Public License as
   published by the Free Software Foundation; either version 2 of the
   License, or (at your option) any later version.

   This program is distributed in the hope that it will be useful, but
   WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
   General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with this program.  If not, see <https://www.gnu.org/licenses/>.

   As a special exception, the authors of SANE give permission for
   additional uses of the libraries contained in this release of SANE.

   The exception is that, if you link a SANE library with other files
   to produce an executable, this does not by itself cause the
   resulting executable to be covered by the GNU General Public
   License.  Your use of that executable is in no way restricted on
   account of linking the SANE library code into it.

   This exception does not, however, invalidate any other reasons why
   the executable file might be covered by the GNU General Public
   License.

   If you submit changes to SANE to the maintainers to be included in
   a subsequent release, you agree by submitting the changes that
   those changes may be distributed with this exception intact.

   If you write modifications of your own for SANE, it is your choice
   whether to permit this exception to apply to your modifications.
   If you do not wish that, delete this exception notice.

   --------------------------------------------------------------------------

   How the backend works:

   - The scanner answers a UDP probe on port 52217. The backend uses
     this probe to find scanners (discovery).
   - Port2 (TCP 53219) receives the reserve and release messages. The
     scanner accepts commands only from the host that has the reserve.
   - Port1 (TCP 53218) receives SCSI-type commands (CDB) in a frame.
   - The scanner sends one JPEG image for each side of a sheet. The
     backend gets the full sheet in sane_start. Then sane_read decodes
     the JPEG data with libjpeg, line by line.
   - A thread sends a keepalive packet while the device is open. If the
     keepalive stops, the scanner frees the reserve. If the scanner does
     not answer the keepalive, or if it sends a power or release event,
     the next sane_start makes a new reserve.
   - sane_start and sane_read own the scan data while they run.
     sane_cancel only sets a flag, so a signal handler can call it. The
     call that runs stops at the next safe point (a wait for a Port1
     reply in sane_start stops in CANCEL_TICK_MS) and ends the job. When
     no call runs, the next sane_start, sane_read, option change or
     sane_close ends the job.
   - If the result of D5 or D6 is not clear (the job is uncertain), the
     next sane_start sends D8 and D6, and reads C2 before the next D5.
     Paper of the old job in the paper path stops the new job
     (SANE_STATUS_JAMMED).
   - The scanner feeds the whole stack in one job (D5 ... D6). The
     frontend sets the page limit (scanimage --batch-count). A cancel
     with sheets left in the job sends D8 (cancel read), then E0. On the
     iX1300, E0 after D8 feeds out ALL the remaining sheets without a
     scan. The backend waits for an empty paper path, then sends D6.
     With the option halt-on-cancel, the backend sends only D8 and D6:
     the paper stops in the paper path, and the user must open the ADF
     top section and remove it (PROTOCOL.md 8.6).

   The source code is divided in sections. Search for the tag "@@".

   Debug levels (SANE_DEBUG_IX1300):
     1  errors
     5  important events and sense data
    10  function start and finish
    15  each network command
    20  data that the backend sends and receives
    30  hex dumps
*/

#include "../include/sane/config.h"

#include <errno.h>
#include <fcntl.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <ctype.h>
#include <pthread.h>

#include <sys/types.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <net/if.h>
#include <ifaddrs.h>
#include <poll.h>

#include <jpeglib.h>
#include <jerror.h>

#include "../include/sane/sane.h"
#include "../include/sane/sanei.h"
#include "../include/sane/saneopts.h"
#include "../include/sane/sanei_backend.h"
#include "../include/sane/sanei_config.h"

#include "ix1300.h"

#define BUILD 1
#define IX1300_CONFIG_FILE "ix1300.conf"

#ifndef MM_PER_INCH
#define MM_PER_INCH 25.4
#endif

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif

/* Maximum page size in mm. */
#define MAX_WIDTH_MM  (IX_MAX_WIDTH_U * MM_PER_INCH / 1200.0)
#define MAX_LENGTH_MM (IX_MAX_LENGTH_U * MM_PER_INCH / 1200.0)
#define MIN_SIZE_MM   (IX_MIN_SIZE_U * MM_PER_INCH / 1200.0)

/* The longest time without new image data on one side, in seconds. */
#define PAPER_WAIT_S  60

/* The longest time for one side, in seconds. */
#define SIDE_WAIT_S   600

/* Cancel: the longest wait for an empty paper path after D8 and E0, in
   seconds. */
#define FLUSH_WAIT_S  120

/* Cancel: the time that the exit sensor can stay set after the other
   sensors are clear, in seconds. The exit bit can stay set without
   paper (PROTOCOL.md 8.4 and 8.5). */
#define EXIT_WAIT_S   5

/* Time for the keepalive replies, in milliseconds. */
#define KA_WAIT_MS    3000

/* A wait for a Port1 reply in a scan examines the cancel flag at this
   interval, in milliseconds. */
#define CANCEL_TICK_MS 100

/* End of a job: the scanner can give BUSY to D8 or D6 (for example while
   it ends the command of an old connection). BUSY means that the scanner
   did not do the command. Send the command again after BUSY_WAIT_MS, at
   most BUSY_TRIES times in all. */
#define BUSY_TRIES    20
#define BUSY_WAIT_MS  250
#define KA_GAP_MS     500

/* The scanner gives the same event again in this time. Ignore it. */
#define EVENT_HOLD_S  2

/* @@ Section 1 - global data and configuration */

static struct ix1300 *ix_devices = NULL;
static const SANE_Device **sane_devArray = NULL;

#define MAX_NET 16

static struct
{
  int auto_search;
  int n_net;
  char net[MAX_NET][64];
  char iface[IFNAMSIZ];
  unsigned char hostid[8];
  int hostid_set;
  int timeout;            /* seconds for one TCP reply */
  int reserve_timeout;    /* reserve byte +34 */
  int keepalive;          /* seconds between keepalive packets */
  int discovery_ms;       /* time to wait for discovery replies */
  int trigger_port;       /* 0 = the system selects a port */
} cfg;

static void
cfg_defaults (void)
{
  memset (&cfg, 0, sizeof (cfg));
  cfg.timeout = 30;
  cfg.reserve_timeout = 30;
  cfg.keepalive = 15;
  cfg.discovery_ms = 3000;
  cfg.trigger_port = 0;
}

/* @@ Section 2 - small helpers */

static void
put_be16 (unsigned char *p, unsigned int v)
{
  p[0] = (v >> 8) & 0xff;
  p[1] = v & 0xff;
}

static void
put_be32 (unsigned char *p, unsigned long v)
{
  p[0] = (v >> 24) & 0xff;
  p[1] = (v >> 16) & 0xff;
  p[2] = (v >> 8) & 0xff;
  p[3] = v & 0xff;
}

static unsigned int
get_be16 (const unsigned char *p)
{
  return ((unsigned int) p[0] << 8) | p[1];
}

static unsigned long
get_be32 (const unsigned char *p)
{
  return ((unsigned long) p[0] << 24) | ((unsigned long) p[1] << 16)
    | ((unsigned long) p[2] << 8) | p[3];
}

static long
get_sbe32 (const unsigned char *p)
{
  unsigned long v = get_be32 (p);
  if (v & 0x80000000UL)
    return -(long) ((~v + 1) & 0xffffffffUL);
  return (long) v;
}

static void
hexdump (int level, const char *comment, const unsigned char *p, size_t l)
{
  size_t i;
  char line[70];
  char *ptr;

  if (DBG_LEVEL < level)
    return;

  DBG (level, "%s (%lu bytes)\n", comment, (unsigned long) l);
  if (l > 256)
    l = 256;

  ptr = line;
  for (i = 0; i < l; i++)
    {
      if (i % 16 == 0)
        {
          if (i)
            DBG (level, "%s\n", line);
          ptr = line;
          sprintf (ptr, "%3.3lx:", (unsigned long) i);
          ptr += 4;
        }
      sprintf (ptr, " %2.2x", p[i]);
      ptr += 3;
    }
  if (l)
    DBG (level, "%s\n", line);
}

/* Monotonic time in milliseconds. */
static long
now_ms (void)
{
  struct timespec ts;

  clock_gettime (CLOCK_MONOTONIC, &ts);
  return (long) ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

/* Ownership of the scan data and of Port1. sane_start, sane_read,
   sane_close, an option change and the C2 sensor read take it. A call that
   does not get it must not touch the scan data. These functions use only
   atomic operations, so a signal handler can call them. */
static int
own_enter (struct ix1300 *s)
{
  int idle = 0;

  return __atomic_compare_exchange_n (&s->owner, &idle, 1, 0,
                                      __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE);
}

static void
own_leave (struct ix1300 *s)
{
  __atomic_store_n (&s->owner, 0, __ATOMIC_RELEASE);
}

/* The cancel flag. sane_cancel sets it, also from a signal handler.
   These functions use only lock-free atomic operations on a
   sig_atomic_t, so they are safe in a signal handler. */
static void
cancel_set (struct ix1300 *s)
{
  __atomic_store_n (&s->cancel_req, 1, __ATOMIC_SEQ_CST);
}

static int
cancel_pending (struct ix1300 *s)
{
  return __atomic_load_n (&s->cancel_req, __ATOMIC_SEQ_CST) != 0;
}

/* Clear the flag. Return 1 if it was set. */
static int
cancel_take (struct ix1300 *s)
{
  return __atomic_exchange_n (&s->cancel_req, 0, __ATOMIC_SEQ_CST) != 0;
}

static void
set_reserve_lost (struct ix1300 *s, int v)
{
  __atomic_store_n (&s->reserve_lost, v, __ATOMIC_RELEASE);
}

static int
get_reserve_lost (struct ix1300 *s)
{
  return __atomic_load_n (&s->reserve_lost, __ATOMIC_ACQUIRE);
}

/* Convert a length in mm (fixed point) to pixels. */
static int
mm_to_px (SANE_Fixed mm, int dpi)
{
  return (int) (SANE_UNFIX (mm) * dpi / MM_PER_INCH + 0.5);
}

/* Convert a length in mm (fixed point) to 1/1200 inch. */
static unsigned long
mm_to_u (SANE_Fixed mm)
{
  return (unsigned long) (SANE_UNFIX (mm) * 1200.0 / MM_PER_INCH + 0.5);
}

static int
parse_hostid (const char *str, unsigned char *id)
{
  int i;
  unsigned int b;

  if (strlen (str) != 16)
    return -1;
  for (i = 0; i < 8; i++)
    {
      if (!isxdigit ((unsigned char) str[2 * i])
          || !isxdigit ((unsigned char) str[2 * i + 1]))
        return -1;
      if (sscanf (str + 2 * i, "%2x", &b) != 1)
        return -1;
      id[i] = (unsigned char) b;
    }
  return 0;
}

/* Read the configuration file. Use the defaults if it is not there. */
static void
read_config (void)
{
  FILE *fp;
  char line[PATH_MAX];
  char key[64], val[PATH_MAX];

  DBG (10, "read_config: start\n");

  cfg_defaults ();

  fp = sanei_config_open (IX1300_CONFIG_FILE);
  if (!fp)
    {
      DBG (5, "read_config: no config file, use 'net auto'\n");
      cfg.auto_search = 1;
      return;
    }

  while (sanei_config_read (line, sizeof (line), fp))
    {
      int n;

      if (line[0] == '#' || line[0] == '\0')
        continue;

      key[0] = val[0] = '\0';
      n = sscanf (line, "%63s %1023s", key, val);
      if (n < 1)
        continue;

      if (!strcmp (key, "net") && n == 2)
        {
          if (!strcmp (val, "auto"))
            cfg.auto_search = 1;
          else if (cfg.n_net < MAX_NET)
            {
              snprintf (cfg.net[cfg.n_net], sizeof (cfg.net[0]), "%s", val);
              cfg.n_net++;
            }
        }
      else if (!strcmp (key, "hostid") && n == 2)
        {
          if (parse_hostid (val, cfg.hostid))
            DBG (1, "read_config: bad hostid '%s', use 16 hex digits\n", val);
          else
            cfg.hostid_set = 1;
        }
      else if (!strcmp (key, "iface") && n == 2)
        snprintf (cfg.iface, sizeof (cfg.iface), "%s", val);
      else if (!strcmp (key, "timeout") && n == 2)
        {
          cfg.timeout = atoi (val);
          if (cfg.timeout < 5)
            cfg.timeout = 5;
        }
      else if (!strcmp (key, "reserve-timeout") && n == 2)
        {
          cfg.reserve_timeout = atoi (val);
          if (cfg.reserve_timeout < 1 || cfg.reserve_timeout > 245)
            cfg.reserve_timeout = 30;
        }
      else if (!strcmp (key, "keepalive") && n == 2)
        {
          cfg.keepalive = atoi (val);
          if (cfg.keepalive < 2)
            cfg.keepalive = 2;
        }
      else if (!strcmp (key, "discovery-timeout") && n == 2)
        {
          cfg.discovery_ms = atoi (val);
          if (cfg.discovery_ms < 100)
            cfg.discovery_ms = 100;
        }
      else if (!strcmp (key, "trigger-port") && n == 2)
        {
          cfg.trigger_port = atoi (val);
          if (cfg.trigger_port < 0 || cfg.trigger_port > 65535)
            cfg.trigger_port = 0;
        }
      else
        DBG (5, "read_config: ignore line '%s'\n", line);
    }
  fclose (fp);

  if (!cfg.auto_search && !cfg.n_net)
    cfg.auto_search = 1;

  /* The scanner frees the reserve after reserve-timeout + 10 s without a
     keepalive. One keepalive takes up to KA_WAIT_MS. Keep a margin of
     2 s. */
  if (cfg.keepalive > cfg.reserve_timeout + 10 - KA_WAIT_MS / 1000 - 2)
    {
      int k = cfg.reserve_timeout + 10 - KA_WAIT_MS / 1000 - 2;
      DBG (1, "read_config: keepalive %d s is too long for reserve-timeout "
           "%d s, use %d s\n", cfg.keepalive, cfg.reserve_timeout, k);
      cfg.keepalive = k;
    }

  DBG (10, "read_config: finish\n");
}

/* @@ Section 3 - host address and discovery */

/* Get the IPv4 address and the MAC address of an interface. */
static int
iface_addr (const char *ifname, struct in_addr *ip, unsigned char *mac)
{
  struct ifreq ifr;
  int fd;
  int ret = -1;

  fd = socket (AF_INET, SOCK_DGRAM, 0);
  if (fd < 0)
    return -1;

  memset (&ifr, 0, sizeof (ifr));
  snprintf (ifr.ifr_name, sizeof (ifr.ifr_name), "%s", ifname);

  if (ip)
    {
      if (ioctl (fd, SIOCGIFADDR, &ifr) < 0)
        goto out;
      *ip = ((struct sockaddr_in *) &ifr.ifr_addr)->sin_addr;
    }
  if (mac)
    {
#ifdef SIOCGIFHWADDR
      if (ioctl (fd, SIOCGIFHWADDR, &ifr) < 0)
        goto out;
      memcpy (mac, ifr.ifr_hwaddr.sa_data, 6);
#else
      memset (mac, 0, 6);
#endif
    }
  ret = 0;

out:
  close (fd);
  return ret;
}

/* Find the local address and MAC that the host uses for TARGET. */
static SANE_Status
host_addr_for (struct in_addr target, struct in_addr *ip, unsigned char *mac)
{
  struct sockaddr_in sa;
  socklen_t sl = sizeof (sa);
  struct ifaddrs *ifa_list, *ifa;
  int fd;
  char ifname[IFNAMSIZ] = "";

  if (cfg.iface[0])
    {
      if (iface_addr (cfg.iface, ip, mac))
        {
          DBG (1, "host_addr_for: cannot read interface %s\n", cfg.iface);
          return SANE_STATUS_IO_ERROR;
        }
      return SANE_STATUS_GOOD;
    }

  /* A connected UDP socket shows the local address of the route. */
  fd = socket (AF_INET, SOCK_DGRAM, 0);
  if (fd < 0)
    return SANE_STATUS_IO_ERROR;
  memset (&sa, 0, sizeof (sa));
  sa.sin_family = AF_INET;
  sa.sin_port = htons (IX_SEARCH_PORT);
  sa.sin_addr = target;
  if (connect (fd, (struct sockaddr *) &sa, sizeof (sa)) < 0
      || getsockname (fd, (struct sockaddr *) &sa, &sl) < 0)
    {
      DBG (1, "host_addr_for: no route to %s\n", inet_ntoa (target));
      close (fd);
      return SANE_STATUS_IO_ERROR;
    }
  close (fd);
  *ip = sa.sin_addr;

  if (getifaddrs (&ifa_list) == 0)
    {
      for (ifa = ifa_list; ifa; ifa = ifa->ifa_next)
        {
          if (!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_INET)
            continue;
          if (((struct sockaddr_in *) ifa->ifa_addr)->sin_addr.s_addr
              == ip->s_addr)
            {
              snprintf (ifname, sizeof (ifname), "%s", ifa->ifa_name);
              break;
            }
        }
      freeifaddrs (ifa_list);
    }

  if (!ifname[0] || iface_addr (ifname, NULL, mac))
    {
      DBG (1, "host_addr_for: cannot find the MAC address of %s\n",
           inet_ntoa (*ip));
      return SANE_STATUS_IO_ERROR;
    }

  DBG (15, "host_addr_for: %s uses %s (%s)\n", inet_ntoa (target),
       inet_ntoa (*ip), ifname);
  return SANE_STATUS_GOOD;
}

/* Make a 32-byte search (flag 0) or keepalive (flag 1) packet. */
static void
make_search (unsigned char *p, int flag, struct in_addr ip,
             const unsigned char *mac)
{
  memset (p, 0, IX_SEARCH_LEN);
  put_be32 (p, IX_KEY);
  put_be32 (p + 4, flag);
  memcpy (p + 8, &ip.s_addr, 4);
  memcpy (p + 12, mac, 6);
  put_be16 (p + 24, IX_IFVER);
}

/* Copy a text field. Remove the spaces at the end. */
static void
copy_field (char *dst, size_t dlen, const unsigned char *src, size_t slen)
{
  size_t n = 0;

  while (n < slen && n + 1 < dlen && src[n])
    {
      dst[n] = (char) src[n];
      n++;
    }
  dst[n] = '\0';
  while (n > 0 && dst[n - 1] == ' ')
    dst[--n] = '\0';
}

/* Add or update a device from a discovery reply that came from FROM.
   Return the device, or NULL if the reply is not valid. */
static struct ix1300 *
attach_reply (const unsigned char *r, size_t len,
              const struct sockaddr_in *from)
{
  struct ix1300 *s;
  struct in_addr addr;
  char product[20];
  char ipstr[32];
  unsigned long p1, p2;

  if (len < IX_SEARCH_REPLY || get_be32 (r) != IX_KEY)
    return NULL;
  if (get_be16 (r + 6) == 0xffff)
    {
      DBG (5, "attach_reply: error reply, ignore it\n");
      return NULL;
    }

  copy_field (product, sizeof (product), r + 104, 16);
  if (!strstr (product, "iX1300"))
    {
      DBG (5, "attach_reply: product '%s' is not an iX1300\n", product);
      return NULL;
    }

  memcpy (&addr.s_addr, r + 16, 4);
  snprintf (ipstr, sizeof (ipstr), "%s", inet_ntoa (addr));

  /* The scanner sends the reply from its own address. */
  if (from->sin_family != AF_INET || from->sin_addr.s_addr != addr.s_addr)
    {
      DBG (5, "attach_reply: reply for %s from %s, ignore it\n", ipstr,
           inet_ntoa (from->sin_addr));
      return NULL;
    }

  p1 = get_be32 (r + 20);
  p2 = get_be32 (r + 24);
  if (p1 == 0 || p1 > 65535 || p2 == 0 || p2 > 65535)
    {
      DBG (5, "attach_reply: %s gives bad ports %lu/%lu, ignore it\n",
           ipstr, p1, p2);
      return NULL;
    }

  for (s = ix_devices; s; s = s->next)
    if (!strcmp (s->name, ipstr))
      break;

  if (!s)
    {
      s = calloc (1, sizeof (*s));
      if (!s)
        return NULL;
      s->p1_fd = -1;
      s->trig_fd = -1;
      pthread_mutex_init (&s->ka_mutex, NULL);
      pthread_cond_init (&s->ka_cond, NULL);
      s->next = ix_devices;
      ix_devices = s;
    }

  snprintf (s->name, sizeof (s->name), "%s", ipstr);
  s->addr = addr;
  s->port1 = (int) p1;
  s->port2 = (int) p2;
  snprintf (s->model, sizeof (s->model), "%s", product);
  copy_field (s->devname, sizeof (s->devname), r + 40, 64);
  copy_field (s->serial, sizeof (s->serial), r + 132, 12);
  s->present = 1;

  s->sane.name = s->name;
  s->sane.vendor = "FUJITSU";
  s->sane.model = s->model;
  s->sane.type = "sheetfed scanner";

  DBG (5, "attach_reply: %s '%s' serial %s ports %d/%d owner %s flags %02x\n",
       s->name, s->devname, s->serial, s->port1, s->port2,
       get_be16 (r + 4) ? "us" : (r[120] | r[121] | r[122] | r[123]) ?
       "other host" : "none", r[36]);

  if (s->port1 != IX_PORT1 || (r[36] & 0x08))
    DBG (1, "attach_reply: %s uses TLS ports. This backend has no TLS.\n",
         s->name);
  return s;
}

/* Send probes and collect the replies.
   TARGETS: unicast addresses. BCAST: also send broadcast probes.
   A scanner in power-save mode can miss a probe. So the backend sends
   the probes again each 0.5 s, until 0.5 s before the end of the wait. */

#define MAX_PROBES 32
#define PROBE_GAP_MS 500

struct probe
{
  struct sockaddr_in to;
  unsigned char pkt[IX_SEARCH_LEN];
};

static int
add_probe (struct probe *pr, int n, struct in_addr to, struct in_addr hip,
           const unsigned char *hmac)
{
  int i;

  if (n >= MAX_PROBES)
    return n;
  for (i = 0; i < n; i++)
    if (pr[i].to.sin_addr.s_addr == to.s_addr)
      return n;
  memset (&pr[n].to, 0, sizeof (pr[n].to));
  pr[n].to.sin_family = AF_INET;
  pr[n].to.sin_port = htons (IX_SEARCH_PORT);
  pr[n].to.sin_addr = to;
  make_search (pr[n].pkt, 0, hip, hmac);
  return n + 1;
}

static void
do_search (const struct in_addr *targets, int n, int bcast)
{
  struct probe pr[MAX_PROBES];
  unsigned char rep[512];
  struct timeval t0, t1;
  int fd, i, on = 1;
  int np = 0;
  int sent_rounds = 0;
  int replies = 0;
  int found[MAX_NET];

  DBG (10, "do_search: start\n");

  if (n > MAX_NET)
    n = MAX_NET;
  memset (found, 0, sizeof (found));
  for (i = 0; i < n; i++)
    {
      struct in_addr hip;
      unsigned char hmac[6];

      if (host_addr_for (targets[i], &hip, hmac) != SANE_STATUS_GOOD)
        continue;
      np = add_probe (pr, np, targets[i], hip, hmac);
    }

  if (bcast)
    {
      struct ifaddrs *ifa_list, *ifa;

      if (getifaddrs (&ifa_list) == 0)
        {
          for (ifa = ifa_list; ifa; ifa = ifa->ifa_next)
            {
              struct in_addr hip, all;
              unsigned char hmac[6];

              if (!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_INET)
                continue;
              if (!(ifa->ifa_flags & IFF_UP)
                  || (ifa->ifa_flags & IFF_LOOPBACK)
                  || !(ifa->ifa_flags & IFF_BROADCAST)
                  || !ifa->ifa_broadaddr)
                continue;
              if (cfg.iface[0] && strcmp (cfg.iface, ifa->ifa_name))
                continue;

              hip = ((struct sockaddr_in *) ifa->ifa_addr)->sin_addr;
              if (iface_addr (ifa->ifa_name, NULL, hmac))
                memset (hmac, 0, 6);
              np = add_probe (pr, np,
                              ((struct sockaddr_in *) ifa->ifa_broadaddr)->
                              sin_addr, hip, hmac);
              DBG (15, "do_search: broadcast on %s\n", ifa->ifa_name);

              /* Also the limited broadcast, one time. */
              all.s_addr = htonl (INADDR_BROADCAST);
              np = add_probe (pr, np, all, hip, hmac);
            }
          freeifaddrs (ifa_list);
        }
    }

  if (!np)
    {
      DBG (5, "do_search: no probe to send\n");
      return;
    }

  fd = socket (AF_INET, SOCK_DGRAM, 0);
  if (fd < 0)
    {
      DBG (1, "do_search: socket: %s\n", strerror (errno));
      return;
    }
  setsockopt (fd, SOL_SOCKET, SO_BROADCAST, &on, sizeof (on));

  gettimeofday (&t0, NULL);
  for (;;)
    {
      struct pollfd pfd;
      struct sockaddr_in from;
      socklen_t fl = sizeof (from);
      struct ix1300 *dev;
      long ms, wait;
      ssize_t r;

      gettimeofday (&t1, NULL);
      ms = (t1.tv_sec - t0.tv_sec) * 1000 + (t1.tv_usec - t0.tv_usec) / 1000;

      /* Send the next round of probes. Always send the first round. */
      if (ms >= sent_rounds * PROBE_GAP_MS
          && (sent_rounds == 0
              || sent_rounds * PROBE_GAP_MS <= cfg.discovery_ms - PROBE_GAP_MS))
        {
          for (i = 0; i < np; i++)
            {
              if (sendto (fd, pr[i].pkt, IX_SEARCH_LEN, 0,
                          (struct sockaddr *) &pr[i].to,
                          sizeof (pr[i].to)) < 0)
                DBG (5, "do_search: sendto %s: %s\n",
                     inet_ntoa (pr[i].to.sin_addr), strerror (errno));
              else
                DBG (15, "do_search: probe to %s\n",
                     inet_ntoa (pr[i].to.sin_addr));
            }
          sent_rounds++;
        }

      if (ms >= cfg.discovery_ms)
        break;

      wait = cfg.discovery_ms - ms;
      if (sent_rounds * PROBE_GAP_MS <= cfg.discovery_ms - PROBE_GAP_MS
          && sent_rounds * PROBE_GAP_MS - ms < wait)
        wait = sent_rounds * PROBE_GAP_MS - ms;
      if (wait < 1)
        wait = 1;

      pfd.fd = fd;
      pfd.events = POLLIN;
      if (poll (&pfd, 1, (int) wait) <= 0)
        continue;

      memset (&from, 0, sizeof (from));
      r = recvfrom (fd, rep, sizeof (rep), 0, (struct sockaddr *) &from, &fl);
      if (r <= 0)
        continue;
      hexdump (30, "do_search: reply", rep, (size_t) r);
      dev = attach_reply (rep, (size_t) r, &from);
      if (!dev)
        continue;

      /* With unicast probes only, stop when all targets replied. */
      for (i = 0; i < n; i++)
        if (!found[i] && targets[i].s_addr == dev->addr.s_addr)
          {
            found[i] = 1;
            replies++;
          }
      if (!bcast && replies >= n)
        break;
    }

  close (fd);
  DBG (10, "do_search: finish\n");
}

/* Find scanners with the configuration file settings. */
static void
find_devices (void)
{
  struct in_addr targets[MAX_NET];
  struct ix1300 *s;
  int i, n = 0;

  read_config ();

  for (s = ix_devices; s; s = s->next)
    if (!s->is_open)
      s->present = 0;

  for (i = 0; i < cfg.n_net; i++)
    {
      if (inet_aton (cfg.net[i], &targets[n]))
        n++;
      else
        DBG (1, "find_devices: '%s' is not an IPv4 address\n", cfg.net[i]);
    }

  do_search (targets, n, cfg.auto_search);
}

/* @@ Section 4 - TCP transport */

static SANE_Status
send_all (int fd, const unsigned char *buf, size_t len)
{
  while (len)
    {
      ssize_t r = send (fd, buf, len, MSG_NOSIGNAL);
      if (r < 0)
        {
          if (errno == EINTR)
            continue;
          DBG (1, "send_all: %s\n", strerror (errno));
          return SANE_STATUS_IO_ERROR;
        }
      buf += r;
      len -= (size_t) r;
    }
  return SANE_STATUS_GOOD;
}

/* Receive LEN bytes before DEADLINE (see now_ms). *GOT shows how many
   bytes arrived. The deadline is for all the bytes, not for each recv.
   If STOP is not NULL, the wait examines *STOP each CANCEL_TICK_MS and
   after a signal. When *STOP is set, return SANE_STATUS_CANCELLED. */
static SANE_Status
recv_all (int fd, unsigned char *buf, size_t len, size_t *got, long deadline,
          volatile sig_atomic_t *stop)
{
  size_t n = 0;

  while (n < len)
    {
      struct pollfd pfd;
      long left = deadline - now_ms ();
      long tick = stop ? CANCEL_TICK_MS : 1000;
      ssize_t r;
      int pr;

      if (stop && __atomic_load_n (stop, __ATOMIC_SEQ_CST))
        {
          if (got)
            *got = n;
          DBG (5, "recv_all: cancel after %lu of %lu bytes\n",
               (unsigned long) n, (unsigned long) len);
          return SANE_STATUS_CANCELLED;
        }
      if (left <= 0)
        {
          if (got)
            *got = n;
          DBG (1, "recv_all: timeout after %lu of %lu bytes\n",
               (unsigned long) n, (unsigned long) len);
          return SANE_STATUS_IO_ERROR;
        }
      pfd.fd = fd;
      pfd.events = POLLIN;
      pfd.revents = 0;
      pr = poll (&pfd, 1, left > tick ? (int) tick : (int) left);
      if (pr < 0 && errno != EINTR)
        {
          if (got)
            *got = n;
          DBG (1, "recv_all: poll: %s\n", strerror (errno));
          return SANE_STATUS_IO_ERROR;
        }
      if (pr <= 0)
        continue;

      r = recv (fd, buf + n, len - n, 0);
      if (r < 0)
        {
          if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
            continue;
          if (got)
            *got = n;
          DBG (1, "recv_all: %s\n", strerror (errno));
          return SANE_STATUS_IO_ERROR;
        }
      if (r == 0)
        {
          if (got)
            *got = n;
          DBG (5, "recv_all: the scanner closed the connection (%lu/%lu)\n",
               (unsigned long) n, (unsigned long) len);
          return SANE_STATUS_IO_ERROR;
        }
      n += (size_t) r;
    }
  if (got)
    *got = n;
  return SANE_STATUS_GOOD;
}

/* Connect to a TCP port of the scanner and read the greeting. STOP: see
   recv_all. */
static SANE_Status
tcp_open (struct ix1300 *s, int port, int *fdp, volatile sig_atomic_t *stop)
{
  SANE_Status ret;
  struct sockaddr_in sa;
  struct timeval tv;
  unsigned char greet[IX_GREET_LEN];
  int fd, on = 1;

  fd = socket (AF_INET, SOCK_STREAM, 0);
  if (fd < 0)
    {
      DBG (1, "tcp_open: socket: %s\n", strerror (errno));
      return SANE_STATUS_IO_ERROR;
    }

  tv.tv_sec = cfg.timeout;
  tv.tv_usec = 0;
  setsockopt (fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof (tv));
  setsockopt (fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof (tv));
  setsockopt (fd, IPPROTO_TCP, TCP_NODELAY, &on, sizeof (on));
  fcntl (fd, F_SETFD, FD_CLOEXEC);

  memset (&sa, 0, sizeof (sa));
  sa.sin_family = AF_INET;
  sa.sin_port = htons (port);
  sa.sin_addr = s->addr;

  if (connect (fd, (struct sockaddr *) &sa, sizeof (sa)) < 0)
    {
      DBG (1, "tcp_open: connect to %s:%d: %s\n", s->name, port,
           strerror (errno));
      close (fd);
      return SANE_STATUS_IO_ERROR;
    }

  ret = recv_all (fd, greet, sizeof (greet), NULL,
                  now_ms () + cfg.timeout * 1000L, stop);
  if (ret != SANE_STATUS_GOOD || get_be32 (greet + 4) != IX_KEY)
    {
      if (ret != SANE_STATUS_CANCELLED)
        {
          DBG (1, "tcp_open: no correct greeting on port %d\n", port);
          ret = SANE_STATUS_IO_ERROR;
        }
      close (fd);
      return ret;
    }
  hexdump (30, "tcp_open: greeting", greet, sizeof (greet));

  *fdp = fd;
  return SANE_STATUS_GOOD;
}

/* Send one message to Port2 and get the reply. */
static SANE_Status
port2_xfer (struct ix1300 *s, const unsigned char *msg, size_t len,
            unsigned char *reply, size_t rlen)
{
  SANE_Status ret;
  int fd;

  ret = tcp_open (s, s->port2, &fd, NULL);
  if (ret != SANE_STATUS_GOOD)
    return ret;

  hexdump (30, "port2_xfer: send", msg, len);
  ret = send_all (fd, msg, len);
  if (ret == SANE_STATUS_GOOD)
    ret = recv_all (fd, reply, rlen, NULL, now_ms () + cfg.timeout * 1000L,
                    NULL);
  if (ret == SANE_STATUS_GOOD)
    {
      hexdump (30, "port2_xfer: reply", reply, rlen);
      /* +0 is the length, +4 the key, +8 the result. */
      if (get_be32 (reply + 4) != IX_KEY || get_be32 (reply) < 12)
        {
          DBG (1, "port2_xfer: bad reply (key %08lx, length %lu)\n",
               get_be32 (reply + 4), get_be32 (reply));
          ret = SANE_STATUS_IO_ERROR;
        }
    }

  close (fd);
  return ret;
}

/* @@ Section 5 - reserve, release, keepalive and triggers */

static SANE_Status
do_reserve (struct ix1300 *s, long *result)
{
  unsigned char m[IX_RESERVE_LEN];
  unsigned char r[IX_RESERVE_REPLY];
  struct tm tm;
  time_t now = time (NULL);
  long gmtoff = 0;
  SANE_Status ret;

  DBG (10, "do_reserve: start\n");

  localtime_r (&now, &tm);
#if defined(__USE_MISC) || defined(__linux__) || defined(__APPLE__)
  gmtoff = tm.tm_gmtoff;
#endif

  memset (m, 0, sizeof (m));
  put_be32 (m, IX_RESERVE_LEN);
  put_be32 (m + 4, IX_KEY);
  put_be32 (m + 8, IX_CMD_RESERVE);
  memcpy (m + 16, s->host_mac, 6);
  put_be16 (m + 32, IX_IFVER);
  m[34] = (unsigned char) cfg.reserve_timeout;
  put_be32 (m + 36, 0);                 /* mode 0: normal scan */
  put_be32 (m + 40, 0);                 /* no password */
  memcpy (m + 44, &s->host_ip.s_addr, 4);
  put_be32 (m + 48, (unsigned long) s->trig_port);
  put_be16 (m + 100, tm.tm_year + 1900);
  m[102] = tm.tm_mon + 1;
  m[103] = tm.tm_mday;
  m[104] = tm.tm_hour;
  m[105] = tm.tm_min;
  m[106] = tm.tm_sec;
  memcpy (m + 108, s->host_id, 8);
  put_be32 (m + 116, (unsigned long) gmtoff);

  ret = port2_xfer (s, m, sizeof (m), r, sizeof (r));
  if (ret != SANE_STATUS_GOOD)
    return ret;

  *result = get_sbe32 (r + 8);
  DBG (10, "do_reserve: finish, result %ld\n", *result);
  return SANE_STATUS_GOOD;
}

static SANE_Status
do_release (struct ix1300 *s)
{
  unsigned char m[IX_RELEASE_LEN];
  unsigned char r[IX_RELEASE_REPLY];
  SANE_Status ret;
  long res;

  DBG (10, "do_release: start\n");

  memset (m, 0, sizeof (m));
  put_be32 (m, IX_RELEASE_LEN);
  put_be32 (m + 4, IX_KEY);
  put_be32 (m + 8, IX_CMD_RELEASE);
  memcpy (m + 16, s->host_mac, 6);
  put_be32 (m + 24, 0);                 /* normal release, not force */

  ret = port2_xfer (s, m, sizeof (m), r, sizeof (r));
  if (ret != SANE_STATUS_GOOD)
    {
      DBG (1, "do_release: no reply. The scanner frees the reserve "
           "after %d s.\n", cfg.reserve_timeout + 10);
      return ret;
    }
  res = get_sbe32 (r + 8);
  if (res)
    DBG (1, "do_release: result %ld\n", res);
  DBG (10, "do_release: finish\n");
  return res ? SANE_STATUS_IO_ERROR : SANE_STATUS_GOOD;
}

/* Send a keepalive. The scanner answers only while we have the reserve.
   A scanner in Wi-Fi power save mode can lose packets, so send the packet
   up to 3 times, KA_GAP_MS apart, and wait up to KA_WAIT_MS for one valid
   reply. Return 1 if the reserve is OK. Set or clear reserve_lost. */
static int
send_keepalive (struct ix1300 *s)
{
  unsigned char pkt[IX_SEARCH_LEN];
  unsigned char rep[64];
  struct sockaddr_in sa;
  long t0 = now_ms ();
  int fd, sent = 0, ok = 0;

  fd = socket (AF_INET, SOCK_DGRAM, 0);
  if (fd < 0)
    return 0;

  make_search (pkt, 1, s->host_ip, s->host_mac);
  memset (&sa, 0, sizeof (sa));
  sa.sin_family = AF_INET;
  sa.sin_port = htons (IX_SEARCH_PORT);
  sa.sin_addr = s->addr;

  while (!ok)
    {
      struct pollfd pfd;
      struct sockaddr_in from;
      socklen_t fl = sizeof (from);
      long el = now_ms () - t0;
      long wait;
      ssize_t r;

      if (el >= KA_WAIT_MS)
        break;
      if (sent < 3 && el >= sent * KA_GAP_MS)
        {
          sendto (fd, pkt, sizeof (pkt), 0, (struct sockaddr *) &sa,
                  sizeof (sa));
          sent++;
        }
      wait = KA_WAIT_MS - el;
      if (sent < 3 && sent * KA_GAP_MS - el < wait)
        wait = sent * KA_GAP_MS - el;
      if (wait < 1)
        wait = 1;

      pfd.fd = fd;
      pfd.events = POLLIN;
      pfd.revents = 0;
      if (poll (&pfd, 1, (int) wait) <= 0)
        continue;
      memset (&from, 0, sizeof (from));
      r = recvfrom (fd, rep, sizeof (rep), 0, (struct sockaddr *) &from, &fl);
      if (r < 6)
        continue;
      if (from.sin_addr.s_addr != s->addr.s_addr)
        {
          DBG (5, "send_keepalive: reply from %s, ignore it\n",
               inet_ntoa (from.sin_addr));
          continue;
        }
      if (get_be32 (rep) != IX_KEY || get_be16 (rep + 4) != 0x8001)
        {
          DBG (5, "send_keepalive: bad reply, ignore it\n");
          continue;
        }
      ok = 1;
    }
  close (fd);

  if (ok)
    DBG (20, "send_keepalive: reply OK\n");
  else
    DBG (1, "send_keepalive: no reply. The reserve can be lost.\n");
  set_reserve_lost (s, !ok);
  return ok;
}

static void *
keepalive_main (void *arg)
{
  struct ix1300 *s = arg;

  pthread_mutex_lock (&s->ka_mutex);
  while (!s->ka_stop)
    {
      struct timespec ts;

      clock_gettime (CLOCK_REALTIME, &ts);
      ts.tv_sec += cfg.keepalive;
      pthread_cond_timedwait (&s->ka_cond, &s->ka_mutex, &ts);
      if (s->ka_stop)
        break;
      pthread_mutex_unlock (&s->ka_mutex);
      send_keepalive (s);
      pthread_mutex_lock (&s->ka_mutex);
    }
  pthread_mutex_unlock (&s->ka_mutex);
  return NULL;
}

static SANE_Status
keepalive_start (struct ix1300 *s)
{
  s->ka_stop = 0;
  if (pthread_create (&s->ka_thread, NULL, keepalive_main, s) != 0)
    {
      DBG (1, "keepalive_start: cannot start the thread\n");
      return SANE_STATUS_NO_MEM;
    }
  s->ka_running = 1;
  return SANE_STATUS_GOOD;
}

static void
keepalive_stop (struct ix1300 *s)
{
  if (!s->ka_running)
    return;
  pthread_mutex_lock (&s->ka_mutex);
  s->ka_stop = 1;
  pthread_cond_signal (&s->ka_cond);
  pthread_mutex_unlock (&s->ka_mutex);
  pthread_join (s->ka_thread, NULL);
  s->ka_running = 0;
}

/* Open the UDP socket for trigger packets (scan button). */
static SANE_Status
trigger_open (struct ix1300 *s)
{
  struct sockaddr_in sa;
  socklen_t sl = sizeof (sa);
  int fd;

  fd = socket (AF_INET, SOCK_DGRAM, 0);
  if (fd < 0)
    return SANE_STATUS_IO_ERROR;
  fcntl (fd, F_SETFD, FD_CLOEXEC);

  memset (&sa, 0, sizeof (sa));
  sa.sin_family = AF_INET;
  sa.sin_port = htons (cfg.trigger_port);
  if (bind (fd, (struct sockaddr *) &sa, sizeof (sa)) < 0)
    {
      DBG (5, "trigger_open: port %d is not free, use any port\n",
           cfg.trigger_port);
      sa.sin_port = 0;
      if (bind (fd, (struct sockaddr *) &sa, sizeof (sa)) < 0)
        {
          close (fd);
          return SANE_STATUS_IO_ERROR;
        }
    }
  getsockname (fd, (struct sockaddr *) &sa, &sl);
  s->trig_fd = fd;
  s->trig_port = ntohs (sa.sin_port);
  DBG (15, "trigger_open: trigger port %d\n", s->trig_port);
  return SANE_STATUS_GOOD;
}

static void
latch_event (struct ix1300 *s, int is_long, const char *from)
{
  time_t now = time (NULL);

  if (now - s->evt_time[is_long] < EVENT_HOLD_S)
    return;
  s->evt_time[is_long] = now;
  if (is_long)
    s->btn_long = 1;
  else
    s->btn_short = 1;
  DBG (5, "latch_event: %s press (%s)\n", is_long ? "long" : "short", from);
}

/* The scanner sends each event 3 times with the same sequence number
   (+16). A copy can stay in the socket queue for a long time, and UDP can
   change the order. So an event is new only if its sequence number is not
   in the last IX_TRG_HISTORY events with the same code. */
static int
trigger_is_new (struct ix1300 *s, int code, int seq)
{
  int i;

  for (i = 0; i < s->trig_n; i++)
    if (s->trig_code[i] == code && s->trig_seqs[i] == seq)
      return 0;
  s->trig_code[s->trig_next] = code;
  s->trig_seqs[s->trig_next] = seq;
  s->trig_next = (s->trig_next + 1) % IX_TRG_HISTORY;
  if (s->trig_n < IX_TRG_HISTORY)
    s->trig_n++;
  return 1;
}

static void
trigger_forget (struct ix1300 *s)
{
  s->trig_n = 0;
  s->trig_next = 0;
}

/* Read all trigger packets that are in the socket queue. Accept only
   48-byte packets from the scanner with a known code. This is a source
   filter on the LAN, not an authentication. */
static void
read_triggers (struct ix1300 *s)
{
  unsigned char p[128];
  struct sockaddr_in from;
  socklen_t fl = sizeof (from);
  ssize_t r;

  if (s->trig_fd < 0)
    return;

  memset (&from, 0, sizeof (from));
  while ((r = recvfrom (s->trig_fd, p, sizeof (p), MSG_DONTWAIT,
                        (struct sockaddr *) &from, &fl)) > 0)
    {
      unsigned long code;

      if (from.sin_family != AF_INET
          || from.sin_addr.s_addr != s->addr.s_addr)
        {
          DBG (5, "read_triggers: packet from %s, ignore it\n",
               inet_ntoa (from.sin_addr));
          goto next;
        }
      if (r != IX_TRIGGER_LEN || get_be32 (p + 4) != IX_KEY)
        {
          DBG (5, "read_triggers: bad packet (%ld bytes), ignore it\n",
               (long) r);
          goto next;
        }
      code = get_be32 (p + 8);
      hexdump (30, "read_triggers: packet", p, (size_t) r);

      switch (code)
        {
        case IX_TRG_POWER_ON:
          if (trigger_is_new (s, (int) code, p[16]))
            {
              /* The scanner starts the sequence numbers again. */
              trigger_forget (s);
              trigger_is_new (s, (int) code, p[16]);
              DBG (1, "read_triggers: the scanner is on again. The "
                   "reserve is lost.\n");
              set_reserve_lost (s, 1);
            }
          break;
        case IX_TRG_POWER_OFF:
        case IX_TRG_FORCE_REL:
          if (trigger_is_new (s, (int) code, p[16]))
            {
              DBG (1, "read_triggers: event 0x%lx. The reserve is lost.\n",
                   code);
              set_reserve_lost (s, 1);
            }
          break;
        case IX_TRG_SCAN:
          if (trigger_is_new (s, (int) code, p[16]))
            latch_event (s, (p[14] & IX_TRG_LONG_FLAG) ? 1 : 0, "trigger");
          break;
        case IX_TRG_STOP:
        case IX_TRG_CANCEL:
        case IX_TRG_REQ_REL:
          if (trigger_is_new (s, (int) code, p[16]))
            DBG (5, "read_triggers: event code 0x%lx\n", code);
          break;
        default:
          DBG (5, "read_triggers: unknown code 0x%lx, ignore it\n", code);
          break;
        }
    next:
      fl = sizeof (from);
      memset (&from, 0, sizeof (from));
    }
}

/* @@ Section 6 - Port1 commands */

static void
p1_close (struct ix1300 *s)
{
  if (s->p1_fd >= 0)
    {
      close (s->p1_fd);
      s->p1_fd = -1;
    }
}

/* A command that gives the same result if the scanner gets it two times.
   Only these commands go again after a lost reply. */
static int
cmd_can_repeat (unsigned char op)
{
  switch (op)
    {
    case 0x12:                  /* INQUIRY */
    case 0xC2:                  /* GET HW STATUS */
    case 0xD8:                  /* CANCEL READ */
    case 0xE9:                  /* pre-read mode */
    case 0xD4:                  /* scan parameters */
      return 1;
    default:
      return 0;
    }
}

/* If Port1 was idle, make sure that the scanner did not close it. A
   closed connection shows end of file before we send a command. Then it
   is safe to connect again: the scanner did not get the command. */
static void
p1_check_idle (struct ix1300 *s)
{
  struct pollfd pfd;
  unsigned char c;
  ssize_t r;

  if (s->p1_fd < 0 || time (NULL) - s->last_io <= 5)
    return;
  pfd.fd = s->p1_fd;
  pfd.events = POLLIN;
  pfd.revents = 0;
  if (poll (&pfd, 1, 0) <= 0)
    return;
  r = recv (s->p1_fd, &c, 1, MSG_PEEK | MSG_DONTWAIT);
  if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
    return;
  if (r > 0)
    DBG (1, "do_cmd: data without a command on Port1, connect again\n");
  else
    DBG (5, "do_cmd: the scanner closed the idle connection, connect "
         "again\n");
  p1_close (s);
}

/* Send one CDB to Port1 and get the reply.
   IN_REQ is the data-in length in the frame. IN gets up to IN_CAP bytes
   (IN_CAP >= IN_REQ). A reply with more than IN_REQ + IX_REPLY_SLACK
   bytes is an error. *SCSI gets the SCSI status. The full reply must
   come in cfg.timeout seconds.
   In the scan I/O of sane_start (s->scan_io), a cancel stops the wait for
   the reply at once. Then the reply is not complete, so close Port1 and
   return SANE_STATUS_CANCELLED. The caller must then end the job. */
static SANE_Status
do_cmd_x (struct ix1300 *s, const unsigned char *cdb, size_t cdb_len,
          const unsigned char *out, size_t out_len,
          unsigned char *in, size_t in_req, size_t in_cap, size_t *in_len,
          int *scsi)
{
  unsigned char frame[IX_P1_HDR + 256];
  unsigned char rh[IX_P1_REPLY_HDR];
  SANE_Status ret = SANE_STATUS_IO_ERROR;
  unsigned long total, status, dlen;
  volatile sig_atomic_t *stop = s->scan_io ? &s->cancel_req : NULL;
  long deadline = 0;
  size_t keep;
  int attempt;

  if (in_len)
    *in_len = 0;
  *scsi = -1;

  if (cdb_len > 16 || out_len > sizeof (frame) - IX_P1_HDR
      || in_req > 0x7fffffffUL)
    return SANE_STATUS_INVAL;
  if (!in)
    in_cap = 0;

  memset (frame, 0, IX_P1_HDR);
  put_be32 (frame, IX_P1_HDR + out_len);
  put_be32 (frame + 4, IX_KEY);
  put_be32 (frame + 8, IX_P1_CDB);
  memcpy (frame + 16, s->host_mac, 6);
  memcpy (frame + 24, &s->host_ip.s_addr, 4);
  put_be32 (frame + 32, cdb_len);
  put_be32 (frame + 36, in_req);
  put_be32 (frame + 40, out_len);
  memcpy (frame + 48, cdb, cdb_len);
  if (out_len)
    memcpy (frame + IX_P1_HDR, out, out_len);

  DBG (15, "do_cmd: cdb %02x, out %lu, in %lu\n", cdb[0],
       (unsigned long) out_len, (unsigned long) in_req);
  hexdump (30, "do_cmd: frame", frame, IX_P1_HDR + out_len);

  p1_check_idle (s);

  for (attempt = 0; attempt < 2; attempt++)
    {
      size_t got = 0;

      /* Do not send a new command after a cancel. */
      if (stop && cancel_pending (s))
        return SANE_STATUS_CANCELLED;
      if (s->p1_fd < 0)
        {
          ret = tcp_open (s, s->port1, &s->p1_fd, stop);
          if (ret != SANE_STATUS_GOOD)
            return ret;
        }

      deadline = now_ms () + cfg.timeout * 1000L;
      ret = send_all (s->p1_fd, frame, IX_P1_HDR + out_len);
      if (ret == SANE_STATUS_GOOD)
        ret = recv_all (s->p1_fd, rh, sizeof (rh), &got, deadline, stop);
      if (ret == SANE_STATUS_GOOD)
        break;

      p1_close (s);
      if (ret == SANE_STATUS_CANCELLED)
        {
          DBG (5, "do_cmd: cdb %02x: cancel in the wait for the reply\n",
               cdb[0]);
          return ret;
        }
      /* The scanner can have done the command before the reply was lost.
         Send it again only if this is safe. */
      if (attempt || got || !cmd_can_repeat (cdb[0]))
        {
          DBG (1, "do_cmd: cdb %02x: no reply. The command is not sent "
               "again.\n", cdb[0]);
          return ret;
        }
      DBG (5, "do_cmd: connect again and send the command again\n");
    }

  s->last_io = time (NULL);
  hexdump (30, "do_cmd: reply header", rh, sizeof (rh));

  total = get_be32 (rh);
  if (get_be32 (rh + 4) != IX_KEY || total < IX_P1_REPLY_HDR)
    {
      DBG (1, "do_cmd: cdb %02x: bad reply header (key %08lx, total %lu)\n",
           cdb[0], get_be32 (rh + 4), total);
      p1_close (s);
      return SANE_STATUS_IO_ERROR;
    }

  status = get_be32 (rh + 8);
  *scsi = (int) get_be32 (rh + 12);
  dlen = total - IX_P1_REPLY_HDR;

  if (dlen > in_req + IX_REPLY_SLACK)
    {
      DBG (1, "do_cmd: cdb %02x: reply of %lu bytes for %lu requested\n",
           cdb[0], dlen, (unsigned long) in_req);
      p1_close (s);
      return SANE_STATUS_IO_ERROR;
    }

  keep = dlen < in_cap ? dlen : in_cap;
  if (keep)
    {
      ret = recv_all (s->p1_fd, in, keep, NULL, deadline, stop);
      if (ret != SANE_STATUS_GOOD)
        {
          p1_close (s);
          return ret;
        }
    }
  if (dlen > keep)
    {
      /* Discard the bytes that the caller did not ask for (at most
         IX_REPLY_SLACK bytes after the requested length). */
      unsigned char junk[IX_REPLY_SLACK];
      unsigned long rest = dlen - keep;

      DBG (5, "do_cmd: cdb %02x: discard %lu extra bytes\n", cdb[0], rest);
      while (rest)
        {
          size_t n = rest < sizeof (junk) ? rest : sizeof (junk);
          ret = recv_all (s->p1_fd, junk, n, NULL, deadline, stop);
          if (ret != SANE_STATUS_GOOD)
            {
              p1_close (s);
              return ret;
            }
          rest -= n;
        }
    }
  if (in_len)
    *in_len = keep;

  if (status != 0)
    {
      DBG (1, "do_cmd: cdb %02x: frame status 0x%lx\n", cdb[0], status);
      return SANE_STATUS_IO_ERROR;
    }

  DBG (15, "do_cmd: cdb %02x: scsi %d, data %lu\n", cdb[0], *scsi, dlen);
  return SANE_STATUS_GOOD;
}

/* do_cmd_x for a caller buffer of IN_MAX bytes. */
static SANE_Status
do_cmd (struct ix1300 *s, const unsigned char *cdb, size_t cdb_len,
        const unsigned char *out, size_t out_len,
        unsigned char *in, size_t in_max, size_t *in_len, int *scsi)
{
  return do_cmd_x (s, cdb, cdb_len, out, out_len, in, in_max, in_max,
                   in_len, scsi);
}

/* Translate sense data to a SANE status. This is the fujitsu map. */
static SANE_Status
sense_status (const unsigned char *sd, size_t len)
{
  unsigned int key, asc, ascq;
  int eom, ili;

  if (len < 14)
    {
      DBG (1, "sense_status: short sense data\n");
      return SANE_STATUS_IO_ERROR;
    }

  key = sd[2] & 0x0f;
  eom = (sd[2] >> 6) & 1;
  ili = (sd[2] >> 5) & 1;
  asc = sd[12];
  ascq = sd[13];

  DBG (5, "sense_status: sense=%#x asc=%#x ascq=%#x eom=%d ili=%d\n",
       key, asc, ascq, eom, ili);

  switch (key)
    {
    case 0x0:
      if (asc == 0x80)
        return SANE_STATUS_GOOD;
      if (asc || ascq)
        return SANE_STATUS_IO_ERROR;
      if (eom)
        return SANE_STATUS_EOF;
      return SANE_STATUS_GOOD;

    case 0x2:
      if (asc || ascq)
        return SANE_STATUS_IO_ERROR;
      return SANE_STATUS_DEVICE_BUSY;

    case 0x3:
      if (asc != 0x80)
        return SANE_STATUS_IO_ERROR;
      switch (ascq)
        {
        case 0x01:
          DBG (5, "sense_status: paper jam\n");
          return SANE_STATUS_JAMMED;
        case 0x02:
          DBG (5, "sense_status: cover open\n");
          return SANE_STATUS_COVER_OPEN;
        case 0x03:
          DBG (5, "sense_status: hopper empty\n");
          return SANE_STATUS_NO_DOCS;
        case 0x04:
        case 0x08:
        case 0x09:
        case 0x0c:
          return SANE_STATUS_JAMMED;
        case 0x07:
          DBG (5, "sense_status: double feed\n");
          return SANE_STATUS_JAMMED;
        case 0x13:
          return SANE_STATUS_DEVICE_BUSY;
        case 0x20:
        case 0x30:
          return SANE_STATUS_NO_DOCS;
        case 0x22:
          return SANE_STATUS_CANCELLED;
        case 0x32:
          return SANE_STATUS_DEVICE_BUSY;
        default:
          return SANE_STATUS_IO_ERROR;
        }

    default:
      return SANE_STATUS_IO_ERROR;
    }
}

static SANE_Status
request_sense (struct ix1300 *s, unsigned char *sd, size_t *len)
{
  static const unsigned char cdb[6] = { 0x03, 0, 0, 0, 18, 0 };
  SANE_Status ret;
  int scsi;

  memset (sd, 0, 18);
  ret = do_cmd (s, cdb, sizeof (cdb), NULL, 0, sd, 18, len, &scsi);
  if (ret == SANE_STATUS_GOOD)
    hexdump (20, "request_sense: data", sd, *len);
  if (ret == SANE_STATUS_GOOD && scsi != IX_SCSI_GOOD)
    {
      DBG (1, "request_sense: scsi status %d\n", scsi);
      ret = SANE_STATUS_IO_ERROR;
    }
  return ret;
}

/* Send a command. Translate a CHECK CONDITION to a SANE status. */
static SANE_Status
simple_cmd (struct ix1300 *s, const unsigned char *cdb, size_t cdb_len,
            const unsigned char *out, size_t out_len,
            unsigned char *in, size_t in_max, size_t *in_len)
{
  SANE_Status ret;
  unsigned char sd[18];
  size_t sl = 0;
  int scsi;

  ret = do_cmd (s, cdb, cdb_len, out, out_len, in, in_max, in_len, &scsi);
  if (ret != SANE_STATUS_GOOD)
    return ret;

  if (scsi == IX_SCSI_GOOD)
    return SANE_STATUS_GOOD;
  if (scsi == IX_SCSI_BUSY)
    {
      DBG (5, "simple_cmd: cdb %02x: busy\n", cdb[0]);
      return SANE_STATUS_DEVICE_BUSY;
    }
  if (scsi != IX_SCSI_CHECK)
    {
      DBG (1, "simple_cmd: cdb %02x: scsi status %d\n", cdb[0], scsi);
      return SANE_STATUS_IO_ERROR;
    }

  ret = request_sense (s, sd, &sl);
  if (ret != SANE_STATUS_GOOD)
    return ret;
  ret = sense_status (sd, sl);
  if (ret == SANE_STATUS_GOOD || ret == SANE_STATUS_EOF)
    return SANE_STATUS_GOOD;
  DBG (5, "simple_cmd: cdb %02x: %s\n", cdb[0], sane_strstatus (ret));
  return ret;
}

/* GET HW STATUS (C2). Update the sensor values. */
static SANE_Status
get_hw_status (struct ix1300 *s, unsigned char *buf, size_t len)
{
  unsigned char cdb[10] = { 0xC2, 0, 0, 0, 0, 0, 0, 0, 0, 0 };
  SANE_Status ret;
  size_t got = 0;
  int sh, lg;

  cdb[8] = (unsigned char) len;
  memset (buf, 0, len);
  ret = simple_cmd (s, cdb, sizeof (cdb), NULL, 0, buf, len, &got);
  if (ret != SANE_STATUS_GOOD)
    return ret;
  if (got < 5)
    {
      DBG (1, "get_hw_status: short reply (%lu)\n", (unsigned long) got);
      return SANE_STATUS_IO_ERROR;
    }
  hexdump (20, "get_hw_status: data", buf, got);

  s->hopper_empty = (buf[IX_HW_HOPPER_BYTE] & IX_HW_HOPPER_BIT) ? 1 : 0;
  s->paper_feed = (buf[IX_HW_FEED_BYTE] & IX_HW_FEED_BIT) ? 1 : 0;
  s->paper_exit = (buf[IX_HW_EXIT_BYTE] & IX_HW_EXIT_BIT) ? 1 : 0;

  /* Latch a button only when the bit changes from 0 to 1. */
  sh = (buf[IX_HW_BTN_BYTE] & IX_HW_BTN_SHORT) ? 1 : 0;
  lg = (buf[IX_HW_BTN_BYTE] & IX_HW_BTN_LONG) ? 1 : 0;
  if (sh && !s->hw_short_last)
    latch_event (s, 0, "C2");
  if (lg && !s->hw_long_last)
    latch_event (s, 1, "C2");
  s->hw_short_last = sh;
  s->hw_long_last = lg;

  return SANE_STATUS_GOOD;
}

/* Read the trigger packets and C2. C2 needs Port1: skip it if a
   different call uses Port1, or if the reserve is not sure. */
static void
read_sensors (struct ix1300 *s)
{
  unsigned char hw[12];

  read_triggers (s);
  if (s->reserved && !get_reserve_lost (s) && own_enter (s))
    {
      get_hw_status (s, hw, sizeof (hw));
      own_leave (s);
    }
}

/* @@ Section 7 - JPEG helpers */

/* Add N bytes to the side image. A side has at most IX_SIDE_MAX bytes. */
static SANE_Status
image_append (struct ix1300_image *img, const unsigned char *p, size_t n)
{
  size_t need;

  if (!n)
    return SANE_STATUS_GOOD;
  if (img->len > IX_SIDE_MAX || n > IX_SIDE_MAX - img->len)
    {
      DBG (1, "image_append: the side is larger than %lu bytes\n",
           (unsigned long) IX_SIDE_MAX);
      return SANE_STATUS_IO_ERROR;
    }
  need = img->len + n;
  if (need > img->size)
    {
      size_t ns = img->size ? img->size : 0x100000;
      unsigned char *nd;

      while (ns < need)
        ns = (ns > IX_SIDE_MAX / 2) ? IX_SIDE_MAX : ns * 2;
      nd = realloc (img->data, ns);
      if (!nd)
        {
          DBG (1, "image_append: out of memory (%lu bytes)\n",
               (unsigned long) ns);
          return SANE_STATUS_NO_MEM;
        }
      img->data = nd;
      img->size = ns;
    }
  memcpy (img->data + img->len, p, n);
  img->len += n;
  return SANE_STATUS_GOOD;
}

static void
image_free (struct ix1300_image *img)
{
  free (img->data);
  img->data = NULL;
  img->len = img->size = 0;
}

/* Find the SOF marker. Return its offset, or 0 if there is none. */
static size_t
jpeg_find_sof (const unsigned char *j, size_t len)
{
  size_t i = 2;

  if (len < 4 || j[0] != 0xFF || j[1] != 0xD8)
    return 0;

  while (i + 9 < len && j[i] == 0xFF)
    {
      unsigned int m = j[i + 1];

      if (m == 0xFF)
        {
          i++;
          continue;
        }
      if (m >= 0xC0 && m <= 0xCF && m != 0xC4 && m != 0xC8 && m != 0xCC)
        return i;
      if (m == 0xDA || m == 0xD9)
        return 0;
      i += 2 + get_be16 (j + i + 2);
    }
  return 0;
}

/* The scanner pads the image to the window length. Write the real
   line count into the SOF marker, so that the decoder stops there. */
static void
jpeg_fix_height (struct ix1300_image *img, int lines)
{
  size_t i = jpeg_find_sof (img->data, img->len);
  unsigned int old;

  if (!i)
    {
      DBG (1, "jpeg_fix_height: no SOF marker\n");
      return;
    }
  old = get_be16 (img->data + i + 5);
  if (lines > 0 && (unsigned int) lines < old)
    {
      put_be16 (img->data + i + 5, (unsigned int) lines);
      DBG (5, "jpeg_fix_height: %u -> %d lines\n", old, lines);
    }
}

/* Decoder state. */
struct ix_jpeg_err
{
  struct jpeg_error_mgr pub;
  jmp_buf jb;
};

struct ix1300_decoder
{
  struct jpeg_decompress_struct cinfo;  /* must be the first member */
  struct ix_jpeg_err jerr;
  struct jpeg_source_mgr src;
  int created;
  int failed;             /* 1 after a decoder error or missing data */
  unsigned char *scan;    /* one decoded line */
  unsigned char *line;    /* one output line */
  int img_w, img_h, comps;
  int x0, y0;
  int rows_read;
  int lines_out;
  int line_len;
  int line_pos;
};

static void
ix_error_exit (j_common_ptr cinfo)
{
  struct ix_jpeg_err *e = (struct ix_jpeg_err *) cinfo->err;
  char msg[JMSG_LENGTH_MAX];

  (*cinfo->err->format_message) (cinfo, msg);
  DBG (1, "libjpeg: %s\n", msg);
  longjmp (e->jb, 1);
}

static void
ix_output_message (j_common_ptr cinfo)
{
  char msg[JMSG_LENGTH_MAX];

  (*cinfo->err->format_message) (cinfo, msg);
  DBG (5, "libjpeg: %s\n", msg);
}

/* libjpeg continues after a warning with data that it makes itself (for
   example gray lines after the end of the data). Thus a warning about
   corrupt or missing data is an error of the frame. Extra bytes before a
   marker do not change the image. */
static void
ix_emit_message (j_common_ptr cinfo, int level)
{
  struct ix1300_decoder *d = (struct ix1300_decoder *) cinfo;

  if (level >= 0)
    return;                     /* trace message */
  cinfo->err->num_warnings++;
  if (cinfo->err->msg_code != JWRN_EXTRANEOUS_DATA)
    d->failed = 1;
  (*cinfo->err->output_message) (cinfo);
}

static void
ix_src_init (j_decompress_ptr cinfo)
{
  (void) cinfo;
}

static boolean
ix_src_fill (j_decompress_ptr cinfo)
{
  static const JOCTET eoi[2] = { 0xFF, JPEG_EOI };
  struct ix1300_decoder *d = (struct ix1300_decoder *) cinfo;

  /* The decoder needs more data than the scanner sent: the side is not
     complete. Give an EOI marker, so that libjpeg stops. */
  if (!d->failed)
    DBG (1, "ix_src_fill: the JPEG data stops before the end of the "
         "image\n");
  d->failed = 1;
  cinfo->src->next_input_byte = eoi;
  cinfo->src->bytes_in_buffer = 2;
  return TRUE;
}

static void
ix_src_skip (j_decompress_ptr cinfo, long n)
{
  if (n <= 0)
    return;
  if ((size_t) n > cinfo->src->bytes_in_buffer)
    n = (long) cinfo->src->bytes_in_buffer;
  cinfo->src->next_input_byte += n;
  cinfo->src->bytes_in_buffer -= (size_t) n;
}

static void
ix_src_term (j_decompress_ptr cinfo)
{
  (void) cinfo;
}

static void
decoder_free (struct ix1300 *s)
{
  struct ix1300_decoder *d = s->dec;

  if (!d)
    return;
  if (d->created)
    jpeg_destroy_decompress (&d->cinfo);
  free (d->scan);
  free (d->line);
  free (d);
  s->dec = NULL;
}

/* @@ Section 8 - scan job */

/* Cancel after D8: send E0 and wait for an empty paper path.
   On the iX1300, E0 after D8 makes the scanner feed out ALL the remaining
   sheets without a scan. A READ after D8 gives sense 5/24/00, so the
   backend sends no READ (PROTOCOL.md 8.6). The backend ignores the
   sense of E0, with these exceptions: "no paper" (nothing to feed out),
   a paper jam and an open cover (the paper cannot move). The wait stops
   when C2 shows an empty chute, no paper at the feed sensor and no paper
   at the exit sensor (or EXIT_WAIT_S later, because the exit bit can
   stay set), or after FLUSH_WAIT_S. */
static void
flush_paper (struct ix1300 *s)
{
  static const unsigned char e0[6] = { 0xE0, 0, 0, 0, 0, 0 };
  unsigned char sd[18];
  unsigned char hw[0x30];
  size_t sl = 0;
  long start, clear_at = -1;
  SANE_Status r;
  int scsi;

  r = do_cmd (s, e0, sizeof (e0), NULL, 0, NULL, 0, NULL, &scsi);
  if (r != SANE_STATUS_GOOD)
    {
      DBG (1, "flush_paper: E0 failed: %s\n", sane_strstatus (r));
      return;
    }
  if (scsi == IX_SCSI_CHECK
      && request_sense (s, sd, &sl) == SANE_STATUS_GOOD && sl >= 14)
    {
      r = sense_status (sd, sl);
      DBG (5, "flush_paper: E0 sense %x/%02x/%02x\n", sd[2] & 0x0f, sd[12],
           sd[13]);
      if (r == SANE_STATUS_NO_DOCS)
        {
          DBG (5, "flush_paper: no paper in the scanner\n");
          return;
        }
      if (r == SANE_STATUS_JAMMED || r == SANE_STATUS_COVER_OPEN)
        {
          DBG (1, "flush_paper: %s. Remove the paper.\n", sane_strstatus (r));
          return;
        }
    }

  DBG (5, "flush_paper: the scanner feeds out the rest of the stack "
       "without a scan\n");
  start = now_ms ();
  for (;;)
    {
      long now;

      if (get_hw_status (s, hw, sizeof (hw)) != SANE_STATUS_GOOD)
        {
          DBG (1, "flush_paper: C2 failed\n");
          return;
        }
      now = now_ms ();
      if (s->hopper_empty && !s->paper_feed)
        {
          if (!s->paper_exit)
            break;
          if (clear_at < 0)
            clear_at = now;
          else if (now - clear_at >= EXIT_WAIT_S * 1000L)
            {
              DBG (5, "flush_paper: the exit sensor stays set\n");
              break;
            }
        }
      else
        clear_at = -1;
      if (now - start >= FLUSH_WAIT_S * 1000L)
        {
          DBG (1, "flush_paper: the paper path is not empty after %d s. "
               "Open the ADF top section and remove the paper.\n",
               FLUSH_WAIT_S);
          return;
        }
      usleep (500000);
    }
  DBG (5, "flush_paper: the paper path is empty after %ld ms\n",
       now_ms () - start);
}

/* Send D8 or D6 (6-byte CDB, no data) at the end of a job. BUSY means
   that the scanner did not do the command, so send it again after a
   short wait (at most BUSY_TRIES times). This is not a replay: the
   backend never sends the command again after a lost reply. */
static SANE_Status
end_cmd (struct ix1300 *s, const unsigned char *cdb, int *scsi)
{
  SANE_Status r;
  int i;

  for (i = 1;; i++)
    {
      r = do_cmd (s, cdb, 6, NULL, 0, NULL, 0, NULL, scsi);
      if (r != SANE_STATUS_GOOD || *scsi != IX_SCSI_BUSY || i >= BUSY_TRIES)
        return r;
      DBG (5, "end_job: cdb %02x: busy, send it again\n", cdb[0]);
      usleep (BUSY_WAIT_MS * 1000);
    }
}

/* End the job. MODE (enum ix1300_end):
   - END_NORMAL: D6. This is the normal end of the stack.
   - END_HALT: D8 (cancel read), then D6. The paper stops in the paper
     path and in the chute. D6 does not eject it.
   - END_CANCEL: D8, then flush_paper (E0 and a wait), then D6. The
     scanner feeds out the rest of the stack without a scan. With the
     option halt-on-cancel, or if the job state is not JOB_ACTIVE, the
     same as END_HALT. For JOB_UNCERTAIN, E0 could feed out the paper of
     the next job.
   After a good D6 the job state is JOB_IDLE. If D6 has no clear result,
   the state is JOB_UNCERTAIN, and the next sane_start sends D8 and D6
   again before a new job. After the end of an uncertain job, set
   path_check: the next job_setup reads C2 and does not send D5 while
   paper of the old job is in the paper path. Return the first error. */
static SANE_Status
end_job (struct ix1300 *s, int mode)
{
  static const unsigned char d8[6] = { 0xD8, 0, 0, 0, 0, 0 };
  static const unsigned char d6[6] = { 0xD6, 0, 0, 0, 0, 0 };
  SANE_Status ret = SANE_STATUS_GOOD, r;
  unsigned char sd[18];
  size_t sl = 0;
  int scsi;
  int uncertain = (s->job_state == JOB_UNCERTAIN);

  if (s->job_state == JOB_IDLE)
    return SANE_STATUS_GOOD;

  DBG (10, "end_job: start, %s, mode %d\n",
       s->job_state == JOB_ACTIVE ? "active" : "uncertain", mode);
  if (mode != END_NORMAL)
    {
      r = end_cmd (s, d8, &scsi);
      if (r == SANE_STATUS_GOOD && scsi == IX_SCSI_CHECK)
        r = request_sense (s, sd, &sl);
      else if (r == SANE_STATUS_GOOD && scsi != IX_SCSI_GOOD)
        r = SANE_STATUS_IO_ERROR;
      if (r != SANE_STATUS_GOOD)
        {
          DBG (1, "end_job: D8 (cancel read) failed: %s\n",
               sane_strstatus (r));
          ret = r;
        }
      else if (mode == END_CANCEL && s->job_state == JOB_ACTIVE)
        {
          if (s->u_halt)
            DBG (5, "end_job: halt-on-cancel: paper can stay in the "
                 "scanner. If it does, open the ADF top section and remove it.\n");
          else
            flush_paper (s);
        }
    }

  r = end_cmd (s, d6, &scsi);
  if (r == SANE_STATUS_GOOD && scsi == IX_SCSI_CHECK)
    {
      r = request_sense (s, sd, &sl);
      if (r == SANE_STATUS_GOOD && sl >= 14 && (sd[2] & 0x0f)
          && s->job_state == JOB_ACTIVE)
        {
          DBG (1, "end_job: D6 sense %x/%02x/%02x\n", sd[2] & 0x0f, sd[12],
               sd[13]);
          r = SANE_STATUS_IO_ERROR;
        }
      /* For JOB_UNCERTAIN, an error sense means: there is no job. */
    }
  else if (r == SANE_STATUS_GOOD && scsi != IX_SCSI_GOOD)
    r = SANE_STATUS_IO_ERROR;

  if (r != SANE_STATUS_GOOD)
    {
      DBG (1, "end_job: D6 (end job) failed: %s. The scanner can still "
           "have the job.\n", sane_strstatus (r));
      s->job_state = JOB_UNCERTAIN;
      return r;
    }

  s->job_state = JOB_IDLE;
  if (uncertain)
    s->path_check = 1;
  free (s->rbuf);
  s->rbuf = NULL;
  DBG (10, "end_job: finish\n");
  return ret;
}

/* Start a job: D8, E9, D4, C2, D5. */
static SANE_Status
job_setup (struct ix1300 *s)
{
  static const unsigned char d8[6] = { 0xD8, 0, 0, 0, 0, 0 };
  static const unsigned char e9[10] = { 0xE9, 0, 0, 0, 0, 0, 0, 0x20, 0, 0 };
  static const unsigned char d4[6] = { 0xD4, 0, 0, 0, 0x50, 0 };
  static const unsigned char d5[6] = { 0xD5, 0, 0, 0, 0x08, 0x08 };
  static const unsigned char params[14] = {
    0x00, 0x03, 0x01, 0x01, 0x88, 0x00, 0xC1,
    0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x00
  };
  unsigned char pre[32];
  unsigned char data[0x50];
  unsigned char *desc = data + 32;
  unsigned char hw[0x30];
  unsigned char jr[8];
  unsigned long width, length;
  int dpi = s->u_res;
  int color = (s->u_mode == MODE_COLOR);
  int card = IX_SOURCE_IS_CARD (s->u_source);
  SANE_Status ret;
  int scsi;

  DBG (10, "job_setup: start (%s)\n", card ? "front slot" : "chute");

  if (!s->rbuf)
    {
      s->rbuf = malloc (IX_READ_MAX + IX_REPLY_SLACK);
      if (!s->rbuf)
        return SANE_STATUS_NO_MEM;
    }

  /* D8: cancel an old read. A CHECK CONDITION here is not an error. */
  ret = do_cmd (s, d8, sizeof (d8), NULL, 0, NULL, 0, NULL, &scsi);
  if (ret != SANE_STATUS_GOOD)
    return ret;
  if (scsi == IX_SCSI_CHECK)
    {
      unsigned char sd[18];
      size_t sl;
      request_sense (s, sd, &sl);
    }

  /* E9: pre-read mode. These values work on the iX1300. The front slot
     uses another length [HW]. */
  memset (pre, 0, sizeof (pre));
  put_be16 (pre, dpi);
  put_be16 (pre + 2, dpi);
  put_be32 (pre + 4, IX_MAX_WIDTH_U);
  put_be32 (pre + 8, card ? IX_FRONT_PREREAD_LENGTH : IX_PREREAD_LENGTH);
  pre[12] = 0x05;
  ret = simple_cmd (s, e9, sizeof (e9), pre, sizeof (pre), NULL, 0, NULL);
  if (ret != SANE_STATUS_GOOD)
    return ret;

  /* D4: scan parameters (32 bytes) and one window descriptor (48 bytes). */
  width = mm_to_u (s->u_page_width);
  if (width > IX_MAX_WIDTH_U)
    width = IX_MAX_WIDTH_U;
  if (s->u_page_height >= s->ph_range.max)
    length = IX_MAX_LENGTH_U;
  else
    length = mm_to_u (s->u_page_height);
  if (length > IX_MAX_LENGTH_U)
    length = IX_MAX_LENGTH_U;

  memset (data, 0, sizeof (data));
  memcpy (data, params, sizeof (params));
  if (card)
    {
      data[1] = IX_FRONT_D4_PATH;        /* front slot [HW] */
      data[4] = IX_FRONT_D4_BYTE4;
    }
  data[31] = 0x30;                       /* descriptor length */
  desc[1] = 0x10;                        /* JPEG */
  put_be16 (desc + 2, dpi);
  put_be16 (desc + 4, dpi);
  desc[6] = color ? 0x05 : 0x02;
  desc[7] = (dpi == 300) ? 0x82 : 0x81;
  desc[8] = (dpi == 300) ? 0x0D : (dpi == 600) ? 0x0A : 0x05;
  put_be32 (desc + 10, width);
  put_be32 (desc + 14, length);
  desc[18] = 0x04;
  desc[30] = 0x01;

  DBG (5, "job_setup: %d dpi, %s, width %lu, length %lu (1/1200 inch)\n",
       dpi, color ? "color" : "gray", width, length);
  hexdump (20, "job_setup: D4 data", data, sizeof (data));

  ret = simple_cmd (s, d4, sizeof (d4), data, sizeof (data), NULL, 0, NULL);
  if (ret != SANE_STATUS_GOOD)
    return ret;

  /* C2: is there paper on the selected path? Before the job: paper in
     the chute clears the hopper bit. A sheet in the front slot sets the
     feed sensor bit with an empty hopper [HW]. The exit sensor bit alone
     is not paper: it can stay set after a halt. */
  ret = get_hw_status (s, hw, sizeof (hw));
  if (ret != SANE_STATUS_GOOD)
    return ret;
  if (card && !(s->hopper_empty && s->paper_feed))
    {
      DBG (5, "job_setup: no paper in the front slot%s\n",
           s->hopper_empty ? "" : " (paper is in the chute; select an "
           "ADF source)");
      return SANE_STATUS_NO_DOCS;
    }
  if (s->path_check)
    {
      /* The old job had no clear result. D8 stops the motor, and D6 does
         not eject, so paper of the old job can stay in the paper path. D5
         would feed it into the new job. Paper at the feed sensor with
         paper in the chute, or with an ADF source, is such paper. A
         sheet in the front slot also sets the feed sensor bit, thus for
         a Card source with an empty chute the check cannot see a
         difference. */
      if (s->paper_feed && (!card || !s->hopper_empty))
        {
          DBG (1, "job_setup: after the end of an uncertain job, paper is "
               "in the paper path. Open the ADF top section, remove the "
               "paper, and start again.\n");
          return SANE_STATUS_JAMMED;
        }
      DBG (5, "job_setup: the paper path is clear after the uncertain "
           "job\n");
      s->path_check = 0;
    }
  if (!card && s->hopper_empty)
    {
      if (s->paper_feed)
        DBG (1, "job_setup: no paper in the chute: paper is in the front "
             "slot; select a Card source\n");
      else
        DBG (5, "job_setup: no paper in the chute\n");
      return SANE_STATUS_NO_DOCS;
    }

  /* D5: start the job. If the reply is lost or not clear, the scanner
     can have a job. Then the state stays JOB_UNCERTAIN, and end_job sends
     D8 and D6. */
  memset (jr, 0, sizeof (jr));
  s->job_state = JOB_UNCERTAIN;
  ret = simple_cmd (s, d5, sizeof (d5), jr, sizeof (jr), jr, sizeof (jr),
                    NULL);
  if (ret != SANE_STATUS_GOOD)
    return ret;

  s->job_state = JOB_ACTIVE;
  s->sheet = 0;

  DBG (10, "job_setup: finish\n");
  return SANE_STATUS_GOOD;
}

/* Add the image data of one READ reply (after the 2-byte page ID and
   sequence ID) to the side. *GOT counts the image bytes of the side, also
   with KEEP = 0. *LAST gets the time of the last new data. */
static SANE_Status
side_add (struct ix1300 *s, int page, int keep, struct ix1300_image *img,
          size_t len, size_t *got, long *last)
{
  size_t n;

  if (len <= 2)
    return SANE_STATUS_GOOD;
  if (s->rbuf[0] != (unsigned char) page)
    DBG (5, "read_side: page ID %d in the reply, %d expected\n",
         s->rbuf[0], page & 0xff);
  n = len - 2;
  if (*got > IX_SIDE_MAX || n > IX_SIDE_MAX - *got)
    {
      DBG (1, "read_side: the side is larger than %lu bytes\n",
           (unsigned long) IX_SIDE_MAX);
      return SANE_STATUS_IO_ERROR;
    }
  if (keep)
    {
      SANE_Status ret = image_append (img, s->rbuf + 2, n);
      if (ret != SANE_STATUS_GOOD)
        return ret;
    }
  *got += n;
  *last = now_ms ();
  return SANE_STATUS_GOOD;
}

/* READ the image of one side. With KEEP = 0, discard the data.
   The rules (PROTOCOL.md 8.3 and 8.5):
   - BUSY, or sense 3/80/13 (temporary no data): send the same READ again.
   - Sense key 0 with EOM: the end of the side. This reply holds data.
   - Sense key 0 without EOM: keep the data and read on.
   - Sense 3/80/03 (no paper) on the first READ of the front, before any
     data: the normal end of the stack (SANE_STATUS_NO_DOCS). At a
     different point, the sheet is lost: SANE_STATUS_JAMMED.
   - No new data for PAPER_WAIT_S, or a side that is not complete in
     SIDE_WAIT_S: SANE_STATUS_IO_ERROR. The C2 hopper bit does not show
     the end of the stack, because the scanner pulls sheets in early. */
static SANE_Status
read_side (struct ix1300 *s, int win, int page, int keep,
           struct ix1300_image *img)
{
  unsigned char cdb[12];
  unsigned char sd[18];
  long start = now_ms ();
  long last = start;
  SANE_Status ret;
  size_t len, sl;
  size_t got = 0;
  int seq = 0;
  int scsi;
  int chunks = 0;

  DBG (10, "read_side: start, window 0x%02x page %d\n", win, page);

  for (;;)
    {
      unsigned int key, asc, ascq;
      long now = now_ms ();

      if (cancel_pending (s))
        return SANE_STATUS_CANCELLED;
      if (now - last > PAPER_WAIT_S * 1000L)
        {
          DBG (1, "read_side: window 0x%02x: no new image data for %d s "
               "(%lu bytes)\n", win, PAPER_WAIT_S, (unsigned long) got);
          return SANE_STATUS_IO_ERROR;
        }
      if (now - start > SIDE_WAIT_S * 1000L)
        {
          DBG (1, "read_side: window 0x%02x: the side is not complete in "
               "%d s\n", win, SIDE_WAIT_S);
          return SANE_STATUS_IO_ERROR;
        }

      memset (cdb, 0, sizeof (cdb));
      cdb[0] = 0x28;
      cdb[5] = (unsigned char) win;
      cdb[6] = 0x30;
      cdb[10] = (unsigned char) page;
      cdb[11] = (unsigned char) seq;

      ret = do_cmd_x (s, cdb, sizeof (cdb), NULL, 0, s->rbuf, IX_READ_MAX,
                      IX_READ_MAX + IX_REPLY_SLACK, &len, &scsi);
      if (ret != SANE_STATUS_GOOD)
        return ret;

      if (scsi == IX_SCSI_BUSY)
        {
          usleep (50000);
          continue;
        }

      if (scsi == IX_SCSI_GOOD)
        {
          ret = side_add (s, page, keep, img, len, &got, &last);
          if (ret != SANE_STATUS_GOOD)
            return ret;
          seq++;
          chunks++;
          continue;
        }

      if (scsi != IX_SCSI_CHECK)
        {
          DBG (1, "read_side: scsi status %d\n", scsi);
          return SANE_STATUS_IO_ERROR;
        }

      /* REQUEST SENSE uses its own buffer. rbuf keeps the data. */
      ret = request_sense (s, sd, &sl);
      if (ret != SANE_STATUS_GOOD)
        return ret;
      if (sl < 14)
        {
          DBG (1, "read_side: short sense data (%lu bytes)\n",
               (unsigned long) sl);
          return SANE_STATUS_IO_ERROR;
        }
      key = sd[2] & 0x0f;
      asc = sd[12];
      ascq = sd[13];

      if (key == 3 && asc == 0x80 && ascq == 0x13)
        {
          /* Temporary no data: discard the data and send the same READ. */
          usleep (100000);
          continue;
        }

      if (key == 0)
        {
          /* No error. This reply also holds image data. */
          ret = side_add (s, page, keep, img, len, &got, &last);
          if (ret != SANE_STATUS_GOOD)
            return ret;
          chunks++;
          if (sd[2] & 0x40)
            break;              /* EOM: the end of the side */
          DBG (5, "read_side: sense key 0 without EOM (%02x/%02x), "
               "read on\n", asc, ascq);
          seq++;
          continue;
        }

      if (key == 3 && asc == 0x80 && ascq == 0x03)
        {
          if (win == IX_WIN_FRONT && seq == 0 && got == 0)
            {
              DBG (5, "read_side: no paper: the end of the stack\n");
              return SANE_STATUS_NO_DOCS;
            }
          DBG (1, "read_side: window 0x%02x: no paper in the middle of the "
               "sheet (%lu bytes). The sheet is lost.\n", win,
               (unsigned long) got);
          return SANE_STATUS_JAMMED;
        }

      ret = sense_status (sd, sl);
      if (ret == SANE_STATUS_GOOD || ret == SANE_STATUS_EOF)
        ret = SANE_STATUS_IO_ERROR;
      return ret;
    }

  DBG (5, "read_side: window 0x%02x: %d replies, %lu bytes, %ld ms\n",
       win, chunks, (unsigned long) got, now_ms () - start);
  return SANE_STATUS_GOOD;
}

/* READ data type 0x80 (pixel size). *LINES gets the real line count. */
static SANE_Status
get_pixelsize (struct ix1300 *s, int win, int page, int *lines)
{
  unsigned char cdb[12];
  unsigned char buf[0x40];
  unsigned char *p = buf;
  SANE_Status ret;
  size_t len;

  memset (cdb, 0, sizeof (cdb));
  cdb[0] = 0x28;
  cdb[2] = 0x80;
  cdb[5] = (unsigned char) win;
  cdb[8] = 0x20;
  cdb[10] = (unsigned char) page;

  *lines = 0;
  ret = simple_cmd (s, cdb, sizeof (cdb), NULL, 0, buf, sizeof (buf), &len);
  if (ret != SANE_STATUS_GOOD)
    return ret;
  if (len == 0x22)
    {
      p += 2;
      len -= 2;
    }
  if (len < 16)
    {
      DBG (1, "get_pixelsize: short reply (%lu)\n", (unsigned long) len);
      return SANE_STATUS_IO_ERROR;
    }
  hexdump (20, "get_pixelsize: data", p, len);
  *lines = (int) get_be32 (p + 12);
  DBG (5, "get_pixelsize: window 0x%02x: %lu x %lu, real lines %d\n",
       win, get_be32 (p), get_be32 (p + 4), *lines);
  return SANE_STATUS_GOOD;
}

/* Feed one sheet and get both sides. Keep the sides that the source
   option asks for. */
static SANE_Status
read_sheet (struct ix1300 *s)
{
  static const unsigned char e0[6] = { 0xE0, 0, 0, 0, 0, 0 };
  int keep[2];
  int i;
  SANE_Status ret;

  DBG (10, "read_sheet: start, sheet %d\n", s->sheet);

  keep[0] = !IX_SOURCE_IS_BACK (s->u_source);
  keep[1] = !IX_SOURCE_IS_FRONT (s->u_source);

  for (i = 0; i < 2; i++)
    {
      image_free (&s->side[i]);
      s->pending[i] = 0;
    }

  /* E0: feed the next sheet. */
  ret = simple_cmd (s, e0, sizeof (e0), NULL, 0, NULL, 0, NULL);
  if (ret != SANE_STATUS_GOOD)
    return ret;

  for (i = 0; i < 2; i++)
    {
      int win = i ? IX_WIN_BACK : IX_WIN_FRONT;
      int page = (2 * s->sheet + i) & 0xff;

      ret = read_side (s, win, page, keep[i], &s->side[i]);
      if (ret != SANE_STATUS_GOOD)
        return ret;

      if (keep[i])
        {
          int lines = 0;

          if (s->side[i].len < 4)
            {
              DBG (1, "read_sheet: no image data for side %d\n", i);
              return SANE_STATUS_IO_ERROR;
            }
          if (s->u_ald
              && get_pixelsize (s, win, page, &lines) == SANE_STATUS_GOOD)
            jpeg_fix_height (&s->side[i], lines);
          s->pending[i] = 1;
        }
    }

  s->sheet++;

  /* Do not use the C2 hopper bit here. The scanner pulls the next sheets
     into the paper path before the host asks for them. Then the bit shows
     "empty", but sheets are still in the scanner. The next E0 or READ
     gives sense 3/80/03 when there is no more paper. */

  DBG (10, "read_sheet: finish\n");
  return SANE_STATUS_GOOD;
}

/* @@ Section 9 - frames and decoding */

/* Compute the crop area and the parameters of a frame. */
static SANE_Status
frame_geometry (struct ix1300 *s, int img_w, int img_h,
                int *x0, int *y0, int *w, int *h)
{
  int dpi = s->u_res;
  int pw = mm_to_px (s->u_page_width, dpi);
  int base = img_w > pw ? (img_w - pw) / 2 : 0;
  int x1, y1;

  *x0 = base + mm_to_px (s->u_tl_x, dpi);
  x1 = base + mm_to_px (s->u_br_x, dpi);
  *y0 = mm_to_px (s->u_tl_y, dpi);
  y1 = mm_to_px (s->u_br_y, dpi);

  if (x1 > img_w)
    x1 = img_w;
  if (y1 > img_h)
    y1 = img_h;
  if (*x0 >= x1 || *y0 >= y1)
    {
      DBG (1, "frame_geometry: the area is outside the image %dx%d\n",
           img_w, img_h);
      return SANE_STATUS_INVAL;
    }
  *w = x1 - *x0;
  *h = y1 - *y0;
  return SANE_STATUS_GOOD;
}

/* Check the JPEG size against the scanner limits at DPI. */
static SANE_Status
check_raster (int w, int h, int comps, int dpi)
{
  long wmax = (long) IX_MAX_WIDTH_U * dpi / 1200 + 16;
  long hmax = (long) IX_MAX_LENGTH_U * dpi / 1200 + 16;

  if (w <= 0 || h <= 0 || w > wmax || h > hmax || (comps != 1 && comps != 3))
    {
      DBG (1, "check_raster: JPEG %dx%d with %d components is not "
           "possible at %d dpi\n", w, h, comps, dpi);
      return SANE_STATUS_IO_ERROR;
    }
  return SANE_STATUS_GOOD;
}

static SANE_Status
frame_start (struct ix1300 *s, int side)
{
  struct ix1300_image *img = &s->side[side];
  struct ix1300_decoder *d;
  SANE_Parameters *p = &s->params;
  SANE_Status ret;
  int w, h;

  DBG (10, "frame_start: start, side %d\n", side);

  s->cur_side = side;
  s->pending[side] = 0;
  memset (p, 0, sizeof (*p));
  p->last_frame = SANE_TRUE;
  p->depth = 8;

  if (s->u_compress == COMP_JPEG)
    {
      size_t i = jpeg_find_sof (img->data, img->len);
      int comps;

      if (!i)
        return SANE_STATUS_IO_ERROR;
      h = (int) get_be16 (img->data + i + 5);
      w = (int) get_be16 (img->data + i + 7);
      comps = img->data[i + 9];
      ret = check_raster (w, h, comps, s->u_res);
      if (ret != SANE_STATUS_GOOD)
        return ret;
      p->format = SANE_FRAME_JPEG;
      p->pixels_per_line = w;
      p->lines = h;
      p->bytes_per_line = w * comps;
      s->jpeg_pos = 0;
      s->started = 1;
      DBG (5, "frame_start: JPEG %dx%d, %d components, %lu bytes\n",
           w, h, comps, (unsigned long) img->len);
      return SANE_STATUS_GOOD;
    }

  d = calloc (1, sizeof (*d));
  if (!d)
    return SANE_STATUS_NO_MEM;
  s->dec = d;

  d->cinfo.err = jpeg_std_error (&d->jerr.pub);
  d->jerr.pub.error_exit = ix_error_exit;
  d->jerr.pub.emit_message = ix_emit_message;
  d->jerr.pub.output_message = ix_output_message;

  if (setjmp (d->jerr.jb))
    {
      decoder_free (s);
      return SANE_STATUS_IO_ERROR;
    }

  jpeg_create_decompress (&d->cinfo);
  d->created = 1;

  d->src.init_source = ix_src_init;
  d->src.fill_input_buffer = ix_src_fill;
  d->src.skip_input_data = ix_src_skip;
  d->src.resync_to_restart = jpeg_resync_to_restart;
  d->src.term_source = ix_src_term;
  d->src.next_input_byte = img->data;
  d->src.bytes_in_buffer = img->len;
  d->cinfo.src = &d->src;

  jpeg_read_header (&d->cinfo, TRUE);
  d->cinfo.out_color_space =
    (d->cinfo.num_components == 1) ? JCS_GRAYSCALE : JCS_RGB;
  jpeg_start_decompress (&d->cinfo);

  d->img_w = (int) d->cinfo.output_width;
  d->img_h = (int) d->cinfo.output_height;
  d->comps = d->cinfo.output_components;

  ret = check_raster (d->img_w, d->img_h, d->comps, s->u_res);
  if (ret == SANE_STATUS_GOOD && d->failed)
    ret = SANE_STATUS_IO_ERROR;
  if (ret != SANE_STATUS_GOOD)
    {
      decoder_free (s);
      return ret;
    }

  ret = frame_geometry (s, d->img_w, d->img_h, &d->x0, &d->y0, &w, &h);
  if (ret != SANE_STATUS_GOOD)
    {
      decoder_free (s);
      return ret;
    }

  p->pixels_per_line = w;
  p->lines = h;
  switch (s->u_mode)
    {
    case MODE_COLOR:
      p->format = SANE_FRAME_RGB;
      p->bytes_per_line = w * 3;
      break;
    case MODE_GRAY:
      p->format = SANE_FRAME_GRAY;
      p->bytes_per_line = w;
      break;
    default:
      p->format = SANE_FRAME_GRAY;
      p->depth = 1;
      p->bytes_per_line = (w + 7) / 8;
      break;
    }

  d->scan = malloc ((size_t) d->img_w * d->comps);
  d->line = malloc ((size_t) p->bytes_per_line);
  if (!d->scan || !d->line)
    {
      decoder_free (s);
      return SANE_STATUS_NO_MEM;
    }
  d->line_len = p->bytes_per_line;
  d->line_pos = d->line_len;   /* the line buffer is empty */

  s->started = 1;
  DBG (5, "frame_start: side %d, JPEG %dx%d (%d comp), frame %dx%d at %d,%d\n",
       side, d->img_w, d->img_h, d->comps, w, h, d->x0, d->y0);
  return SANE_STATUS_GOOD;
}

static void
frame_finish (struct ix1300 *s)
{
  decoder_free (s);
  if (s->started)
    image_free (&s->side[s->cur_side]);
  s->started = 0;
}

/* Read one JPEG line into d->scan. Return 0 on a decoder error, on
   missing data, or after a libjpeg warning about corrupt data. */
static int
decode_row (struct ix1300_decoder *d)
{
  JSAMPROW row = d->scan;

  if (d->failed)
    return 0;
  if (setjmp (d->jerr.jb))
    {
      d->failed = 1;
      return 0;
    }
  if (d->cinfo.output_scanline >= d->cinfo.output_height
      || jpeg_read_scanlines (&d->cinfo, &row, 1) != 1)
    {
      d->failed = 1;
      return 0;
    }
  if (d->failed)
    return 0;
  d->rows_read++;
  return 1;
}

/* Make the next output line in d->line. The backend does not make lines
   that are not in the JPEG data: on a decoder error, it returns
   SANE_STATUS_IO_ERROR. */
static SANE_Status
build_line (struct ix1300 *s)
{
  struct ix1300_decoder *d = s->dec;
  int w = s->params.pixels_per_line;
  int x;
  const unsigned char *in;

  while (!d->failed && d->rows_read < d->y0)
    decode_row (d);

  if (!decode_row (d))
    {
      DBG (1, "build_line: the JPEG data of side %d is not complete or not "
           "valid (line %d of %d)\n", s->cur_side, d->lines_out,
           s->params.lines);
      return SANE_STATUS_IO_ERROR;
    }

  in = d->scan + (size_t) d->x0 * d->comps;
  switch (s->u_mode)
    {
    case MODE_COLOR:
      if (d->comps == 3)
        memcpy (d->line, in, (size_t) w * 3);
      else
        for (x = 0; x < w; x++)
          d->line[3 * x] = d->line[3 * x + 1] = d->line[3 * x + 2] = in[x];
      break;

    case MODE_GRAY:
      if (d->comps == 1)
        memcpy (d->line, in, (size_t) w);
      else
        for (x = 0; x < w; x++)
          d->line[x] = (unsigned char) ((in[3 * x] * 77
                                         + in[3 * x + 1] * 150
                                         + in[3 * x + 2] * 29) >> 8);
      break;

    default:
      memset (d->line, 0, d->line_len);
      for (x = 0; x < w; x++)
        {
          int g = (d->comps == 1) ? in[x]
            : ((in[3 * x] * 77 + in[3 * x + 1] * 150
                + in[3 * x + 2] * 29) >> 8);
          if (g < s->u_threshold)
            d->line[x >> 3] |= (unsigned char) (0x80 >> (x & 7));
        }
      break;
    }

  d->lines_out++;
  d->line_pos = 0;
  return SANE_STATUS_GOOD;
}

/* @@ Section 10 - options */

static void
init_options (struct ix1300 *s)
{
  SANE_Option_Descriptor *o;
  int i;

  if (s->options_ready)
    return;

  memset (s->opt, 0, sizeof (s->opt));
  for (i = 0; i < NUM_OPTIONS; i++)
    {
      s->opt[i].name = "filler";
      s->opt[i].size = sizeof (SANE_Word);
      s->opt[i].cap = SANE_CAP_INACTIVE;
    }

  s->source_list[0] = STRING_ADFFRONT;
  s->source_list[1] = STRING_ADFBACK;
  s->source_list[2] = STRING_ADFDUPLEX;
  s->source_list[3] = STRING_CARDFRONT;
  s->source_list[4] = STRING_CARDBACK;
  s->source_list[5] = STRING_CARDDUPLEX;
  s->source_list[6] = NULL;

  s->mode_list[0] = SANE_VALUE_SCAN_MODE_COLOR;
  s->mode_list[1] = SANE_VALUE_SCAN_MODE_GRAY;
  s->mode_list[2] = SANE_VALUE_SCAN_MODE_LINEART;
  s->mode_list[3] = NULL;

  s->compress_list[0] = STRING_NONE;
  s->compress_list[1] = STRING_JPEG;
  s->compress_list[2] = NULL;

  s->res_list[0] = 4;
  s->res_list[1] = 150;
  s->res_list[2] = 200;
  s->res_list[3] = 300;
  s->res_list[4] = 600;

  s->pw_range.min = SANE_FIX (MIN_SIZE_MM);
  s->pw_range.max = SANE_FIX (MAX_WIDTH_MM);
  s->pw_range.quant = 0;
  s->ph_range.min = SANE_FIX (MIN_SIZE_MM);
  s->ph_range.max = SANE_FIX (MAX_LENGTH_MM);
  s->ph_range.quant = 0;
  s->x_range.min = 0;
  s->x_range.max = s->pw_range.max;
  s->x_range.quant = 0;
  s->y_range.min = 0;
  s->y_range.max = s->ph_range.max;
  s->y_range.quant = 0;
  s->threshold_range.min = 0;
  s->threshold_range.max = 255;
  s->threshold_range.quant = 1;

  /* default values */
  s->u_source = SOURCE_ADF_FRONT;
  s->u_mode = MODE_COLOR;
  s->u_res = 300;
  s->u_page_width = s->pw_range.max;
  s->u_page_height = s->ph_range.max;
  s->u_tl_x = 0;
  s->u_tl_y = 0;
  s->u_br_x = s->u_page_width;
  s->u_br_y = s->u_page_height;
  s->u_threshold = 128;
  s->u_ald = 1;
  s->u_compress = COMP_NONE;
  s->u_halt = 0;

  o = &s->opt[OPT_NUM_OPTS];
  o->name = "";
  o->title = SANE_TITLE_NUM_OPTIONS;
  o->desc = SANE_DESC_NUM_OPTIONS;
  o->type = SANE_TYPE_INT;
  o->cap = SANE_CAP_SOFT_DETECT;

  o = &s->opt[OPT_STANDARD_GROUP];
  o->name = SANE_NAME_STANDARD;
  o->title = SANE_TITLE_STANDARD;
  o->desc = SANE_DESC_STANDARD;
  o->type = SANE_TYPE_GROUP;
  o->cap = 0;
  o->size = 0;

  o = &s->opt[OPT_SOURCE];
  o->name = SANE_NAME_SCAN_SOURCE;
  o->title = SANE_TITLE_SCAN_SOURCE;
  o->desc = SANE_I18N ("Selects the scan source. ADF: the paper chute. "
                       "With normal loading (print side down), the print "
                       "is on the back. Card: the front slot (return "
                       "path), one sheet at a time, no long paper.");
  o->type = SANE_TYPE_STRING;
  o->constraint_type = SANE_CONSTRAINT_STRING_LIST;
  o->constraint.string_list = s->source_list;
  o->size = 32;
  o->cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;

  o = &s->opt[OPT_MODE];
  o->name = SANE_NAME_SCAN_MODE;
  o->title = SANE_TITLE_SCAN_MODE;
  o->desc = SANE_DESC_SCAN_MODE;
  o->type = SANE_TYPE_STRING;
  o->constraint_type = SANE_CONSTRAINT_STRING_LIST;
  o->constraint.string_list = s->mode_list;
  o->size = 32;
  o->cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;

  o = &s->opt[OPT_RES];
  o->name = SANE_NAME_SCAN_RESOLUTION;
  o->title = SANE_TITLE_SCAN_RESOLUTION;
  o->desc = SANE_DESC_SCAN_RESOLUTION;
  o->type = SANE_TYPE_INT;
  o->unit = SANE_UNIT_DPI;
  o->constraint_type = SANE_CONSTRAINT_WORD_LIST;
  o->constraint.word_list = s->res_list;
  o->cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;

  o = &s->opt[OPT_GEOMETRY_GROUP];
  o->name = SANE_NAME_GEOMETRY;
  o->title = SANE_TITLE_GEOMETRY;
  o->desc = SANE_DESC_GEOMETRY;
  o->type = SANE_TYPE_GROUP;
  o->cap = 0;
  o->size = 0;

  o = &s->opt[OPT_TL_X];
  o->name = SANE_NAME_SCAN_TL_X;
  o->title = SANE_TITLE_SCAN_TL_X;
  o->desc = SANE_DESC_SCAN_TL_X;
  o->type = SANE_TYPE_FIXED;
  o->unit = SANE_UNIT_MM;
  o->constraint_type = SANE_CONSTRAINT_RANGE;
  o->constraint.range = &s->x_range;
  o->cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;

  o = &s->opt[OPT_TL_Y];
  o->name = SANE_NAME_SCAN_TL_Y;
  o->title = SANE_TITLE_SCAN_TL_Y;
  o->desc = SANE_DESC_SCAN_TL_Y;
  o->type = SANE_TYPE_FIXED;
  o->unit = SANE_UNIT_MM;
  o->constraint_type = SANE_CONSTRAINT_RANGE;
  o->constraint.range = &s->y_range;
  o->cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;

  o = &s->opt[OPT_BR_X];
  o->name = SANE_NAME_SCAN_BR_X;
  o->title = SANE_TITLE_SCAN_BR_X;
  o->desc = SANE_DESC_SCAN_BR_X;
  o->type = SANE_TYPE_FIXED;
  o->unit = SANE_UNIT_MM;
  o->constraint_type = SANE_CONSTRAINT_RANGE;
  o->constraint.range = &s->x_range;
  o->cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;

  o = &s->opt[OPT_BR_Y];
  o->name = SANE_NAME_SCAN_BR_Y;
  o->title = SANE_TITLE_SCAN_BR_Y;
  o->desc = SANE_DESC_SCAN_BR_Y;
  o->type = SANE_TYPE_FIXED;
  o->unit = SANE_UNIT_MM;
  o->constraint_type = SANE_CONSTRAINT_RANGE;
  o->constraint.range = &s->y_range;
  o->cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;

  o = &s->opt[OPT_PAGE_WIDTH];
  o->name = SANE_NAME_PAGE_WIDTH;
  o->title = SANE_TITLE_PAGE_WIDTH;
  o->desc = SANE_DESC_PAGE_WIDTH;
  o->type = SANE_TYPE_FIXED;
  o->unit = SANE_UNIT_MM;
  o->constraint_type = SANE_CONSTRAINT_RANGE;
  o->constraint.range = &s->pw_range;
  o->cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;

  o = &s->opt[OPT_PAGE_HEIGHT];
  o->name = SANE_NAME_PAGE_HEIGHT;
  o->title = SANE_TITLE_PAGE_HEIGHT;
  o->desc = SANE_I18N ("Specifies the height of the media. The maximum "
                       "value gives the automatic length of the scanner.");
  o->type = SANE_TYPE_FIXED;
  o->unit = SANE_UNIT_MM;
  o->constraint_type = SANE_CONSTRAINT_RANGE;
  o->constraint.range = &s->ph_range;
  o->cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;

  o = &s->opt[OPT_ENHANCEMENT_GROUP];
  o->name = SANE_NAME_ENHANCEMENT;
  o->title = SANE_TITLE_ENHANCEMENT;
  o->desc = SANE_DESC_ENHANCEMENT;
  o->type = SANE_TYPE_GROUP;
  o->cap = 0;
  o->size = 0;

  o = &s->opt[OPT_THRESHOLD];
  o->name = SANE_NAME_THRESHOLD;
  o->title = SANE_TITLE_THRESHOLD;
  o->desc = SANE_I18N ("Lineart mode: the backend makes a pixel black "
                       "when its gray value is less than this value.");
  o->type = SANE_TYPE_INT;
  o->constraint_type = SANE_CONSTRAINT_RANGE;
  o->constraint.range = &s->threshold_range;
  o->cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT | SANE_CAP_INACTIVE;

  o = &s->opt[OPT_ADVANCED_GROUP];
  o->name = SANE_NAME_ADVANCED;
  o->title = SANE_TITLE_ADVANCED;
  o->desc = SANE_DESC_ADVANCED;
  o->type = SANE_TYPE_GROUP;
  o->cap = 0;
  o->size = 0;

  o = &s->opt[OPT_ALD];
  o->name = "ald";
  o->title = SANE_I18N ("Auto length detection");
  o->desc = SANE_I18N ("The scanner finds the end of the paper. The backend "
                       "removes the empty area after the paper.");
  o->type = SANE_TYPE_BOOL;
  o->cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT | SANE_CAP_ADVANCED;

  o = &s->opt[OPT_COMPRESS];
  o->name = "compression";
  o->title = SANE_I18N ("Compression");
  o->desc = SANE_I18N ("None: the backend decodes the JPEG data to raw "
                       "lines. JPEG: the backend gives the JPEG data of "
                       "the scanner. Most frontends cannot use JPEG.");
  o->type = SANE_TYPE_STRING;
  o->constraint_type = SANE_CONSTRAINT_STRING_LIST;
  o->constraint.string_list = s->compress_list;
  o->size = 16;
  o->cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT | SANE_CAP_ADVANCED;

  /* The name, title and text of the fujitsu backend. */
  o = &s->opt[OPT_HALT_ON_CANCEL];
  o->name = "halt-on-cancel";
  o->title = SANE_I18N ("Halt on Cancel");
  o->desc = SANE_I18N ("Request driver to halt the paper feed instead of "
                       "eject during a cancel.");
  o->type = SANE_TYPE_BOOL;
  o->cap = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT | SANE_CAP_ADVANCED;

  o = &s->opt[OPT_SENSOR_GROUP];
  o->name = SANE_NAME_SENSORS;
  o->title = SANE_TITLE_SENSORS;
  o->desc = SANE_DESC_SENSORS;
  o->type = SANE_TYPE_GROUP;
  o->cap = 0;
  o->size = 0;

  o = &s->opt[OPT_SCAN_SW];
  o->name = SANE_NAME_SCAN;
  o->title = SANE_TITLE_SCAN;
  o->desc = SANE_I18N ("Short press of the scan button. The value is yes "
                       "one time after each press.");
  o->type = SANE_TYPE_BOOL;
  o->unit = SANE_UNIT_NONE;
  o->cap = SANE_CAP_SOFT_DETECT | SANE_CAP_HARD_SELECT | SANE_CAP_ADVANCED;

  o = &s->opt[OPT_LONG_SW];
  o->name = "long-press";
  o->title = SANE_I18N ("Long press");
  o->desc = SANE_I18N ("Long press (about 3 s) of the scan button. The "
                       "value is yes one time after each press.");
  o->type = SANE_TYPE_BOOL;
  o->unit = SANE_UNIT_NONE;
  o->cap = SANE_CAP_SOFT_DETECT | SANE_CAP_HARD_SELECT | SANE_CAP_ADVANCED;

  o = &s->opt[OPT_PAGE_LOADED];
  o->name = SANE_NAME_PAGE_LOADED;
  o->title = SANE_TITLE_PAGE_LOADED;
  o->desc = SANE_DESC_PAGE_LOADED;
  o->type = SANE_TYPE_BOOL;
  o->unit = SANE_UNIT_NONE;
  o->cap = SANE_CAP_SOFT_DETECT | SANE_CAP_HARD_SELECT | SANE_CAP_ADVANCED;

  s->options_ready = 1;
}

static void
update_option_caps (struct ix1300 *s)
{
  if (s->u_mode == MODE_LINEART)
    {
      s->opt[OPT_THRESHOLD].cap &= ~SANE_CAP_INACTIVE;
      s->opt[OPT_COMPRESS].cap |= SANE_CAP_INACTIVE;
      s->u_compress = COMP_NONE;
    }
  else
    {
      s->opt[OPT_THRESHOLD].cap |= SANE_CAP_INACTIVE;
      s->opt[OPT_COMPRESS].cap &= ~SANE_CAP_INACTIVE;
    }
  s->x_range.max = s->u_page_width;
  s->y_range.max = s->u_page_height;
}

static int
string_index (SANE_String_Const *list, const char *val)
{
  int i;

  for (i = 0; list[i]; i++)
    if (!strcmp (list[i], val))
      return i;
  return -1;
}

/* Translate a reserve result to a SANE status. */
static SANE_Status
reserve_status (struct ix1300 *s, long result)
{
  switch (result)
    {
    case IX_RES_OK:
      return SANE_STATUS_GOOD;
    case IX_RES_OCCUPIED:
      DBG (1, "reserve: %s: another host uses the scanner (-4)\n", s->name);
      return SANE_STATUS_DEVICE_BUSY;
    case IX_RES_UNREG:
    case IX_RES_BADID:
      DBG (1, "reserve: %s: the scanner refuses host ID "
           "%02X%02X%02X%02X%02X%02X%02X%02X (result %ld: %s). Register "
           "this ID in the scanner, or set the registered ID with 'hostid' "
           "in %s.\n",
           s->name, s->host_id[0], s->host_id[1], s->host_id[2],
           s->host_id[3], s->host_id[4], s->host_id[5], s->host_id[6],
           s->host_id[7], result,
           result == IX_RES_UNREG ? "not registered" : "not the registered ID",
           IX1300_CONFIG_FILE);
      return SANE_STATUS_ACCESS_DENIED;
    case IX_RES_USBMODE:
      DBG (1, "reserve: %s: the scanner is in USB mode (-9)\n", s->name);
      return SANE_STATUS_DEVICE_BUSY;
    default:
      DBG (1, "reserve: %s: reserve failed, result %ld\n", s->name, result);
      return SANE_STATUS_IO_ERROR;
    }
}

/* Before a new job: make sure that we have the reserve. A lost keepalive
   reply or a power or release event sets reserve_lost. Then send a
   keepalive again. If there is no reply, the scanner lost the reserve
   (for example after a power cycle): search the scanner again (this also
   wakes it up) and make a new reserve. */
static SANE_Status
reserve_check (struct ix1300 *s)
{
  SANE_Status ret;
  long result = 0;

  read_triggers (s);
  if (s->reserved && !get_reserve_lost (s))
    return SANE_STATUS_GOOD;
  if (s->reserved && send_keepalive (s))
    {
      DBG (5, "reserve_check: the scanner answers, the reserve is OK\n");
      return SANE_STATUS_GOOD;
    }

  DBG (1, "reserve_check: %s: the reserve is lost, reserve again\n",
       s->name);
  p1_close (s);
  s->reserved = 0;
  /* After a power cycle the scanner has no job. But if only the
     keepalive replies were lost, the scanner can still have our job. Thus
     a job that was not idle is now uncertain: sane_start ends it (D8, D6)
     on the new connection and reads C2 before the next D5. */
  if (s->job_state != JOB_IDLE)
    s->job_state = JOB_UNCERTAIN;

  do_search (&s->addr, 1, 0);
  ret = do_reserve (s, &result);
  if (ret != SANE_STATUS_GOOD)
    return ret;
  ret = reserve_status (s, result);
  if (ret != SANE_STATUS_GOOD)
    return ret;

  s->reserved = 1;
  s->last_io = time (NULL);
  trigger_forget (s);
  set_reserve_lost (s, 0);
  DBG (5, "reserve_check: %s: reserve OK again\n", s->name);
  return SANE_STATUS_GOOD;
}

/* @@ Section 11 - SANE interface */

static SANE_Status do_cancel (struct ix1300 *s, int mode);

SANE_Status
sane_init (SANE_Int * version_code, SANE_Auth_Callback authorize)
{
  (void) authorize;

  DBG_INIT ();
  DBG (10, "sane_init: start\n");

  if (version_code)
    *version_code = SANE_VERSION_CODE (SANE_CURRENT_MAJOR, SANE_CURRENT_MINOR,
                                       BUILD);

  DBG (5, "sane_init: ix1300 backend %d.%d.%d, from %s\n",
       SANE_CURRENT_MAJOR, SANE_CURRENT_MINOR, BUILD, PACKAGE_STRING);

  cfg_defaults ();

  DBG (10, "sane_init: finish\n");
  return SANE_STATUS_GOOD;
}

SANE_Status
sane_get_devices (const SANE_Device *** device_list, SANE_Bool local_only)
{
  struct ix1300 *s;
  int n = 0;

  DBG (10, "sane_get_devices: start\n");

  if (local_only)
    {
      /* The scanner is a network device. */
      DBG (5, "sane_get_devices: local_only is set, but list network "
           "scanners\n");
    }

  find_devices ();

  for (s = ix_devices; s; s = s->next)
    if (s->present)
      n++;

  free (sane_devArray);
  sane_devArray = calloc ((size_t) n + 1, sizeof (*sane_devArray));
  if (!sane_devArray)
    return SANE_STATUS_NO_MEM;

  n = 0;
  for (s = ix_devices; s; s = s->next)
    if (s->present)
      sane_devArray[n++] = &s->sane;
  sane_devArray[n] = NULL;

  if (device_list)
    *device_list = sane_devArray;

  DBG (10, "sane_get_devices: finish, %d devices\n", n);
  return SANE_STATUS_GOOD;
}

SANE_Status
sane_open (SANE_String_Const name, SANE_Handle * handle)
{
  struct ix1300 *s = NULL;
  SANE_Status ret;
  long result = 0;
  unsigned char inq[0x60];
  static const unsigned char inq_cdb[6] = { 0x12, 0, 0, 0, 0x60, 0 };
  size_t len;
  char vendor[9], model[17];

  DBG (10, "sane_open: start\n");

  if (name && !strncmp (name, "ix1300:", 7))
    name += 7;

  read_config ();
  if (!name || !name[0])
    {
      if (!ix_devices)
        find_devices ();
    }

  if (!name || !name[0])
    {
      for (s = ix_devices; s; s = s->next)
        if (s->present)
          break;
    }
  else
    {
      for (s = ix_devices; s; s = s->next)
        if (!strcmp (s->name, name))
          break;

      if (!s)
        {
          struct in_addr a;

          if (!inet_aton (name, &a))
            {
              DBG (1, "sane_open: '%s' is not an IPv4 address\n", name);
              return SANE_STATUS_INVAL;
            }
          do_search (&a, 1, 0);
          for (s = ix_devices; s; s = s->next)
            if (!strcmp (s->name, name))
              break;
        }
    }

  if (!s)
    {
      DBG (1, "sane_open: no scanner '%s'\n", name ? name : "");
      return SANE_STATUS_INVAL;
    }

  if (s->is_open)
    {
      DBG (1, "sane_open: %s is already open\n", s->name);
      return SANE_STATUS_DEVICE_BUSY;
    }

  ret = host_addr_for (s->addr, &s->host_ip, s->host_mac);
  if (ret != SANE_STATUS_GOOD)
    return ret;

  if (cfg.hostid_set)
    memcpy (s->host_id, cfg.hostid, 8);
  else
    {
      DBG (1, "sane_open: no 'hostid' in %s. Use the MAC address.\n",
           IX1300_CONFIG_FILE);
      memcpy (s->host_id, s->host_mac, 6);
      s->host_id[6] = s->host_id[7] = 0;
    }

  ret = trigger_open (s);
  if (ret != SANE_STATUS_GOOD)
    return ret;

  ret = do_reserve (s, &result);
  if (ret != SANE_STATUS_GOOD)
    goto fail;
  ret = reserve_status (s, result);
  if (ret != SANE_STATUS_GOOD)
    goto fail;

  s->reserved = 1;
  set_reserve_lost (s, 0);
  trigger_forget (s);
  s->last_io = time (NULL);
  DBG (5, "sane_open: %s: reserve OK, trigger port %d\n", s->name,
       s->trig_port);

  ret = simple_cmd (s, inq_cdb, sizeof (inq_cdb), NULL, 0, inq, sizeof (inq),
                    &len);
  if (ret != SANE_STATUS_GOOD || len < 32)
    {
      DBG (1, "sane_open: INQUIRY failed\n");
      if (ret == SANE_STATUS_GOOD)
        ret = SANE_STATUS_IO_ERROR;
      goto fail;
    }
  copy_field (vendor, sizeof (vendor), inq + 8, 8);
  copy_field (model, sizeof (model), inq + 16, 16);
  DBG (5, "sane_open: INQUIRY: '%s' '%s'\n", vendor, model);

  ret = keepalive_start (s);
  if (ret != SANE_STATUS_GOOD)
    goto fail;

  init_options (s);
  update_option_caps (s);

  s->is_open = 1;
  s->present = 1;
  cancel_take (s);
  s->owner = 0;
  s->scan_io = 0;
  s->started = 0;
  s->job_state = JOB_IDLE;
  s->path_check = 0;
  *handle = s;

  DBG (10, "sane_open: finish\n");
  return SANE_STATUS_GOOD;

fail:
  p1_close (s);
  if (s->reserved)
    {
      if (do_release (s) != SANE_STATUS_GOOD)
        DBG (1, "sane_open: the release failed\n");
      s->reserved = 0;
    }
  if (s->trig_fd >= 0)
    {
      close (s->trig_fd);
      s->trig_fd = -1;
    }
  return ret;
}

const SANE_Option_Descriptor *
sane_get_option_descriptor (SANE_Handle handle, SANE_Int option)
{
  struct ix1300 *s = handle;

  if (option < 0 || option >= NUM_OPTIONS)
    return NULL;
  return &s->opt[option];
}

SANE_Status
sane_control_option (SANE_Handle handle, SANE_Int option,
                     SANE_Action action, void *val, SANE_Int * info)
{
  struct ix1300 *s = handle;
  SANE_Option_Descriptor *o;
  SANE_Status ret;
  SANE_Int dummy = 0;

  if (!info)
    info = &dummy;
  *info = 0;

  if (option < 0 || option >= NUM_OPTIONS)
    return SANE_STATUS_INVAL;
  o = &s->opt[option];

  if (!SANE_OPTION_IS_ACTIVE (o->cap))
    {
      DBG (5, "sane_control_option: option %d is not active\n", option);
      return SANE_STATUS_INVAL;
    }

  if (action == SANE_ACTION_GET_VALUE)
    {
      SANE_Word *w = val;

      DBG (20, "sane_control_option: get %s\n", o->name);
      switch (option)
        {
        case OPT_NUM_OPTS:
          *w = NUM_OPTIONS;
          break;
        case OPT_SOURCE:
          strcpy (val, s->source_list[s->u_source]);
          break;
        case OPT_MODE:
          strcpy (val, s->mode_list[s->u_mode]);
          break;
        case OPT_RES:
          *w = s->u_res;
          break;
        case OPT_TL_X:
          *w = s->u_tl_x;
          break;
        case OPT_TL_Y:
          *w = s->u_tl_y;
          break;
        case OPT_BR_X:
          *w = s->u_br_x;
          break;
        case OPT_BR_Y:
          *w = s->u_br_y;
          break;
        case OPT_PAGE_WIDTH:
          *w = s->u_page_width;
          break;
        case OPT_PAGE_HEIGHT:
          *w = s->u_page_height;
          break;
        case OPT_THRESHOLD:
          *w = s->u_threshold;
          break;
        case OPT_ALD:
          *w = s->u_ald;
          break;
        case OPT_COMPRESS:
          strcpy (val, s->compress_list[s->u_compress]);
          break;
        case OPT_HALT_ON_CANCEL:
          *w = s->u_halt;
          break;
        case OPT_SCAN_SW:
          read_sensors (s);
          *w = s->btn_short;
          s->btn_short = 0;
          break;
        case OPT_LONG_SW:
          read_sensors (s);
          *w = s->btn_long;
          s->btn_long = 0;
          break;
        case OPT_PAGE_LOADED:
          read_sensors (s);
          if (IX_SOURCE_IS_CARD (s->u_source))
            *w = (s->hopper_empty && s->paper_feed) ? SANE_TRUE : SANE_FALSE;
          else
            *w = s->hopper_empty ? SANE_FALSE : SANE_TRUE;
          break;
        default:
          return SANE_STATUS_INVAL;
        }
      return SANE_STATUS_GOOD;
    }

  if (action != SANE_ACTION_SET_VALUE)
    return SANE_STATUS_INVAL;

  if (!SANE_OPTION_IS_SETTABLE (o->cap))
    {
      DBG (5, "sane_control_option: option %d is not settable\n", option);
      return SANE_STATUS_INVAL;
    }

  /* sane_cancel only sets a flag (see sane_cancel). End the job here, so
     that the frontend can change the options after sane_cancel. */
  if (cancel_pending (s))
    {
      if (!own_enter (s))
        return SANE_STATUS_DEVICE_BUSY;
      if (cancel_pending (s) && do_cancel (s, END_CANCEL) != SANE_STATUS_GOOD)
        DBG (1, "sane_control_option: the job did not end correctly\n");
      own_leave (s);
    }

  /* The options of a job stay the same until the job ends: D4 has the old
     values, and the sides in memory use them. sane_cancel ends the job. */
  if (s->started || s->pending[0] || s->pending[1]
      || s->job_state == JOB_ACTIVE)
    {
      DBG (5, "sane_control_option: cannot change options in a job. "
           "Call sane_cancel first.\n");
      return SANE_STATUS_DEVICE_BUSY;
    }

  ret = sanei_constrain_value (o, val, info);
  if (ret != SANE_STATUS_GOOD)
    return ret;

  DBG (20, "sane_control_option: set %s\n", o->name);

  switch (option)
    {
    case OPT_SOURCE:
      {
        int i = string_index (s->source_list, val);
        if (i < 0)
          return SANE_STATUS_INVAL;
        s->u_source = i;
        *info |= SANE_INFO_RELOAD_PARAMS;
        break;
      }
    case OPT_MODE:
      {
        int i = string_index (s->mode_list, val);
        if (i < 0)
          return SANE_STATUS_INVAL;
        s->u_mode = i;
        update_option_caps (s);
        *info |= SANE_INFO_RELOAD_PARAMS | SANE_INFO_RELOAD_OPTIONS;
        break;
      }
    case OPT_RES:
      s->u_res = *(SANE_Word *) val;
      *info |= SANE_INFO_RELOAD_PARAMS;
      break;
    case OPT_TL_X:
      s->u_tl_x = *(SANE_Word *) val;
      *info |= SANE_INFO_RELOAD_PARAMS;
      break;
    case OPT_TL_Y:
      s->u_tl_y = *(SANE_Word *) val;
      *info |= SANE_INFO_RELOAD_PARAMS;
      break;
    case OPT_BR_X:
      s->u_br_x = *(SANE_Word *) val;
      *info |= SANE_INFO_RELOAD_PARAMS;
      break;
    case OPT_BR_Y:
      s->u_br_y = *(SANE_Word *) val;
      *info |= SANE_INFO_RELOAD_PARAMS;
      break;
    case OPT_PAGE_WIDTH:
      {
        SANE_Fixed v = *(SANE_Word *) val;
        /* br-x follows the page width if it was at the old width. */
        if (s->u_br_x == s->u_page_width || s->u_br_x > v)
          s->u_br_x = v;
        if (s->u_tl_x >= s->u_br_x)
          s->u_tl_x = 0;
        s->u_page_width = v;
        update_option_caps (s);
        *info |= SANE_INFO_RELOAD_PARAMS | SANE_INFO_RELOAD_OPTIONS;
        break;
      }
    case OPT_PAGE_HEIGHT:
      {
        SANE_Fixed v = *(SANE_Word *) val;
        if (s->u_br_y == s->u_page_height || s->u_br_y > v)
          s->u_br_y = v;
        if (s->u_tl_y >= s->u_br_y)
          s->u_tl_y = 0;
        s->u_page_height = v;
        update_option_caps (s);
        *info |= SANE_INFO_RELOAD_PARAMS | SANE_INFO_RELOAD_OPTIONS;
        break;
      }
    case OPT_THRESHOLD:
      s->u_threshold = *(SANE_Word *) val;
      break;
    case OPT_ALD:
      s->u_ald = *(SANE_Word *) val ? 1 : 0;
      *info |= SANE_INFO_RELOAD_PARAMS;
      break;
    case OPT_COMPRESS:
      {
        int i = string_index (s->compress_list, val);
        if (i < 0)
          return SANE_STATUS_INVAL;
        s->u_compress = i;
        *info |= SANE_INFO_RELOAD_PARAMS;
        break;
      }
    case OPT_HALT_ON_CANCEL:
      s->u_halt = *(SANE_Word *) val ? 1 : 0;
      break;
    default:
      return SANE_STATUS_INVAL;
    }

  return SANE_STATUS_GOOD;
}

SANE_Status
sane_get_parameters (SANE_Handle handle, SANE_Parameters * params)
{
  struct ix1300 *s = handle;
  SANE_Parameters *p = params;
  int dpi, w;

  DBG (10, "sane_get_parameters: start\n");

  if (s->started)
    {
      *p = s->params;
    }
  else
    {
      /* An estimate from the options. sane_start gives exact values. */
      dpi = s->u_res;
      memset (p, 0, sizeof (*p));
      w = mm_to_px (s->u_br_x, dpi) - mm_to_px (s->u_tl_x, dpi);
      p->pixels_per_line = w;
      p->lines = s->u_ald ? -1
        : mm_to_px (s->u_br_y, dpi) - mm_to_px (s->u_tl_y, dpi);
      p->last_frame = SANE_TRUE;
      p->depth = 8;

      if (s->u_compress == COMP_JPEG && s->u_mode != MODE_LINEART)
        {
          p->format = SANE_FRAME_JPEG;
          p->bytes_per_line = w * (s->u_mode == MODE_COLOR ? 3 : 1);
        }
      else if (s->u_mode == MODE_COLOR)
        {
          p->format = SANE_FRAME_RGB;
          p->bytes_per_line = w * 3;
        }
      else if (s->u_mode == MODE_GRAY)
        {
          p->format = SANE_FRAME_GRAY;
          p->bytes_per_line = w;
        }
      else
        {
          p->format = SANE_FRAME_GRAY;
          p->depth = 1;
          p->bytes_per_line = (w + 7) / 8;
        }
    }

  DBG (15, "sane_get_parameters: format %d, %d x %d, bpl %d, depth %d\n",
       p->format, p->pixels_per_line, p->lines, p->bytes_per_line,
       p->depth);
  DBG (10, "sane_get_parameters: finish\n");
  return SANE_STATUS_GOOD;
}

/* Stop the job and free the scan data. Only the owner (see own_enter)
   can call it. MODE is the end_job mode. Return the result of end_job.
   Clear the cancel flag first: a cancel in this function stays set for
   the next call. A cancel does not stop the I/O of end_job. */
static SANE_Status
do_cancel (struct ix1300 *s, int mode)
{
  SANE_Status ret;
  int i;

  DBG (10, "do_cancel: start\n");
  cancel_take (s);
  s->scan_io = 0;
  frame_finish (s);
  for (i = 0; i < 2; i++)
    {
      image_free (&s->side[i]);
      s->pending[i] = 0;
    }
  ret = end_job (s, mode);
  DBG (10, "do_cancel: finish\n");
  return ret;
}

static SANE_Status
start_owned (struct ix1300 *s)
{
  SANE_Status ret, r;
  int side;

  if (!s->is_open)
    return SANE_STATUS_INVAL;

  /* A frame that the frontend did not read to the end. */
  if (s->started)
    frame_finish (s);

  /* A sane_cancel when no call ran only set the flag. End that job now,
     then start a new one. */
  if (cancel_pending (s) && do_cancel (s, END_CANCEL) != SANE_STATUS_GOOD)
    DBG (1, "sane_start: the old job did not end correctly\n");

  if (!s->pending[0] && !s->pending[1])
    {
      if (s->job_state != JOB_ACTIVE)
        {
          ret = reserve_check (s);
          if (ret != SANE_STATUS_GOOD)
            goto fail;
          if (s->job_state == JOB_UNCERTAIN)
            {
              /* Bounded recovery: D8 and D6 here, and again in do_cancel
                 if this fails. Then job_setup reads C2 (path_check)
                 before D5. No other command goes again. */
              DBG (5, "sane_start: end the old job first\n");
              ret = end_job (s, END_HALT);
              if (s->job_state != JOB_IDLE)
                goto fail;
            }
          /* From here, a cancel stops the wait for a reply. */
          s->scan_io = 1;
          ret = job_setup (s);
          if (ret != SANE_STATUS_GOOD)
            goto fail;
        }

      s->scan_io = 1;
      ret = read_sheet (s);
      s->scan_io = 0;
      if (ret != SANE_STATUS_GOOD)
        goto fail;
    }

  side = s->pending[0] ? 0 : 1;
  if (!s->pending[side])
    {
      ret = SANE_STATUS_IO_ERROR;
      goto fail;
    }

  ret = frame_start (s, side);
  if (ret != SANE_STATUS_GOOD)
    goto fail;

  return SANE_STATUS_GOOD;

fail:
  s->scan_io = 0;
  /* A signal for a cancel can also stop a system call with an error (for
     example connect with EINTR). Then the cause is the cancel. */
  if (ret == SANE_STATUS_IO_ERROR && cancel_pending (s))
    ret = SANE_STATUS_CANCELLED;
  DBG (5, "sane_start: %s\n", sane_strstatus (ret));
  /* At the normal end of the stack, D6 is sufficient (PROTOCOL.md 8.5).
     After a cancel, feed out the rest of the stack (or halt). After an
     error, send D8 and D6 only: the paper path can have a jam. */
  r = do_cancel (s, ret == SANE_STATUS_NO_DOCS ? END_NORMAL
                 : ret == SANE_STATUS_CANCELLED ? END_CANCEL : END_HALT);
  if (ret == SANE_STATUS_NO_DOCS && r != SANE_STATUS_GOOD)
    {
      DBG (1, "sane_start: the job did not end correctly\n");
      ret = SANE_STATUS_IO_ERROR;
    }
  return ret;
}

SANE_Status
sane_start (SANE_Handle handle)
{
  struct ix1300 *s = handle;
  SANE_Status ret;

  DBG (10, "sane_start: start\n");

  if (!own_enter (s))
    {
      DBG (1, "sane_start: a different call uses the device\n");
      return SANE_STATUS_DEVICE_BUSY;
    }
  ret = start_owned (s);
  own_leave (s);

  DBG (10, "sane_start: finish, %s\n", sane_strstatus (ret));
  return ret;
}

static SANE_Status
read_owned (struct ix1300 *s, SANE_Byte * buf, SANE_Int max_len,
            SANE_Int * len)
{
  struct ix1300_decoder *d;

  if (cancel_pending (s))
    {
      if (do_cancel (s, END_CANCEL) != SANE_STATUS_GOOD)
        DBG (1, "sane_read: the job did not end correctly\n");
      return SANE_STATUS_CANCELLED;
    }
  if (!s->started)
    return SANE_STATUS_EOF;
  if (max_len == 0)
    return SANE_STATUS_GOOD;

  if (s->u_compress == COMP_JPEG)
    {
      struct ix1300_image *img = &s->side[s->cur_side];
      size_t n = img->len - s->jpeg_pos;

      if (!n)
        {
          frame_finish (s);
          return SANE_STATUS_EOF;
        }
      if (n > (size_t) max_len)
        n = (size_t) max_len;
      memcpy (buf, img->data + s->jpeg_pos, n);
      s->jpeg_pos += n;
      *len = (SANE_Int) n;
      return SANE_STATUS_GOOD;
    }

  d = s->dec;
  if (d->failed)
    return SANE_STATUS_IO_ERROR;
  while (max_len > 0)
    {
      int n;

      if (d->line_pos >= d->line_len)
        {
          if (d->lines_out >= s->params.lines)
            break;
          if (build_line (s) != SANE_STATUS_GOOD)
            break;
        }
      n = d->line_len - d->line_pos;
      if (n > max_len)
        n = max_len;
      memcpy (buf, d->line + d->line_pos, (size_t) n);
      d->line_pos += n;
      buf += n;
      max_len -= n;
      *len += n;
    }

  if (cancel_pending (s))
    {
      *len = 0;
      if (do_cancel (s, END_CANCEL) != SANE_STATUS_GOOD)
        DBG (1, "sane_read: the job did not end correctly\n");
      return SANE_STATUS_CANCELLED;
    }
  if (d->failed)
    {
      /* Give the good lines first. The next call gives the error. */
      if (*len > 0)
        return SANE_STATUS_GOOD;
      return SANE_STATUS_IO_ERROR;
    }
  if (*len == 0)
    {
      DBG (10, "sane_read: end of frame, side %d\n", s->cur_side);
      frame_finish (s);
      return SANE_STATUS_EOF;
    }
  return SANE_STATUS_GOOD;
}

SANE_Status
sane_read (SANE_Handle handle, SANE_Byte * buf, SANE_Int max_len,
           SANE_Int * len)
{
  struct ix1300 *s = handle;
  SANE_Status ret;

  if (!len)
    return SANE_STATUS_INVAL;
  *len = 0;
  if (!buf || max_len < 0)
    {
      DBG (1, "sane_read: bad buffer or length %d\n", (int) max_len);
      return SANE_STATUS_INVAL;
    }
  if (!own_enter (s))
    return cancel_pending (s) ? SANE_STATUS_CANCELLED
      : SANE_STATUS_DEVICE_BUSY;
  ret = read_owned (s, buf, max_len, len);
  own_leave (s);
  return ret;
}

/* SANE allows a call of sane_cancel at any time, also from a signal
   handler or a different thread. Thus sane_cancel only sets the cancel
   flag (a lock-free atomic store). It does no I/O, no memory operation
   and no debug output, because these are not safe in a signal handler.
   - A sane_start that runs stops at its next safe point: a wait for a
     Port1 reply stops in CANCEL_TICK_MS (do_cmd_x closes Port1). Then
     sane_start ends the job (D8, E0, wait, D6) and frees the data.
   - A sane_read that runs stops after its current line.
   - When no call runs, the next sane_start, sane_read, sane_close or
     option change ends the job. */
void
sane_cancel (SANE_Handle handle)
{
  struct ix1300 *s = handle;

  if (s)
    cancel_set (s);
}

void
sane_close (SANE_Handle handle)
{
  struct ix1300 *s = handle;
  int i;

  DBG (10, "sane_close: start\n");

  if (!s->is_open)
    return;

  /* Wait for a call in a different thread (at most cfg.timeout s). */
  for (i = 0; !own_enter (s); i++)
    {
      if (i >= cfg.timeout * 20)
        {
          DBG (1, "sane_close: a different call uses the device\n");
          return;
        }
      usleep (50000);
    }

  if (do_cancel (s, END_CANCEL) != SANE_STATUS_GOOD)
    DBG (1, "sane_close: the job did not end correctly\n");
  keepalive_stop (s);
  p1_close (s);
  if (s->reserved)
    {
      if (do_release (s) != SANE_STATUS_GOOD)
        DBG (1, "sane_close: the release failed\n");
      s->reserved = 0;
    }
  if (s->trig_fd >= 0)
    {
      close (s->trig_fd);
      s->trig_fd = -1;
    }
  free (s->rbuf);
  s->rbuf = NULL;
  s->is_open = 0;
  own_leave (s);

  DBG (10, "sane_close: finish\n");
}

void
sane_exit (void)
{
  struct ix1300 *s, *next;

  DBG (10, "sane_exit: start\n");

  for (s = ix_devices; s; s = next)
    {
      next = s->next;
      if (s->is_open)
        sane_close (s);
      pthread_mutex_destroy (&s->ka_mutex);
      pthread_cond_destroy (&s->ka_cond);
      free (s);
    }
  ix_devices = NULL;

  free (sane_devArray);
  sane_devArray = NULL;

  DBG (10, "sane_exit: finish\n");
}

SANE_Status
sane_set_io_mode (SANE_Handle handle, SANE_Bool non_blocking)
{
  (void) handle;
  return non_blocking ? SANE_STATUS_UNSUPPORTED : SANE_STATUS_GOOD;
}

SANE_Status
sane_get_select_fd (SANE_Handle handle, SANE_Int * fdp)
{
  (void) handle;
  (void) fdp;
  return SANE_STATUS_UNSUPPORTED;
}
