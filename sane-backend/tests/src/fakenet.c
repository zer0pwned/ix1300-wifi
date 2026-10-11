/* Fake network and fake scanner for the ix1300 test harness.
   See fakenet.h. This file does not use the interpose macros. */

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <arpa/inet.h>
#include <net/if.h>
#include <sys/ioctl.h>

#include "fakenet.h"

struct fk_model fk;

static const unsigned char KEY[4] = { 0x56, 0x45, 0x4E, 0x53 };

/* ---------------------------------------------------------------- */
/* clock                                                             */

static pthread_mutex_t clk_mu = PTHREAD_MUTEX_INITIALIZER;
static long clk_off_ms;

static long
real_ms (void)
{
  struct timespec ts;
  clock_gettime (CLOCK_MONOTONIC, &ts);
  return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

long
fk_now_ms (void)
{
  long v;
  pthread_mutex_lock (&clk_mu);
  v = real_ms () + clk_off_ms;
  pthread_mutex_unlock (&clk_mu);
  return v;
}

void
fk_advance_ms (long ms)
{
  pthread_mutex_lock (&clk_mu);
  clk_off_ms += ms;
  pthread_mutex_unlock (&clk_mu);
}

time_t
h_time (time_t * t)
{
  time_t v = (time_t) (1700000000L + fk_now_ms () / 1000);
  if (t)
    *t = v;
  return v;
}

int
h_usleep (useconds_t us)
{
  fk_advance_ms ((long) us / 1000 + 1);
  return 0;
}

int
h_clock_gettime (clockid_t c, struct timespec *ts)
{
  long ms;

  if (c != CLOCK_MONOTONIC)
    return clock_gettime (c, ts);
  ms = fk_now_ms ();
  ts->tv_sec = ms / 1000;
  ts->tv_nsec = (ms % 1000) * 1000000L;
  return 0;
}

static void
real_sleep_ms (int ms)
{
  struct timespec ts;
  ts.tv_sec = ms / 1000;
  ts.tv_nsec = (ms % 1000) * 1000000L;
  nanosleep (&ts, NULL);
}

/* ---------------------------------------------------------------- */
/* helpers                                                           */

static void
be32 (unsigned char *p, unsigned long v)
{
  p[0] = v >> 24;
  p[1] = v >> 16;
  p[2] = v >> 8;
  p[3] = v;
}

static unsigned long
gbe32 (const unsigned char *p)
{
  return ((unsigned long) p[0] << 24) | ((unsigned long) p[1] << 16)
    | ((unsigned long) p[2] << 8) | p[3];
}

void
fk_reply_data (struct fk_reply *r, const void *p, size_t n)
{
  free (r->data);
  r->data = malloc (n ? n : 1);
  if (n)
    memcpy (r->data, p, n);
  r->dlen = n;
}

void
fk_drop_port1 (void)
{
  pthread_mutex_lock (&fk.mu);
  if (fk.p1_peer > 0)
    shutdown (fk.p1_peer, SHUT_RDWR);
  pthread_mutex_unlock (&fk.mu);
}

void
fk_set_sense (int key, int asc, int ascq, int eom)
{
  memset (fk.sense, 0, sizeof (fk.sense));
  fk.sense[0] = 0x70;
  fk.sense[2] = (unsigned char) ((key & 0x0f) | (eom ? 0x40 : 0));
  fk.sense[7] = 10;
  fk.sense[12] = (unsigned char) asc;
  fk.sense[13] = (unsigned char) ascq;
}

void
fk_reset (void)
{
  pthread_mutex_destroy (&fk.mu);
  memset (&fk, 0, sizeof (fk));
  pthread_mutex_init (&fk.mu, NULL);
  fk.chunk = 40000;
  fk.temp_first = 1;
  fk.prefetch = 2;
  fk.cur = -1;
  fk.need_reserve = 1;
  fk.eject_ms = 1500;
  fk_set_sense (0, 0, 0, 0);
}

int
fk_log_has (const unsigned char *seq, int n)
{
  int i;
  for (i = 0; i + n <= fk.nlog; i++)
    if (!memcmp (fk.log + i, seq, (size_t) n))
      return 1;
  return 0;
}

void
fk_log_print (void)
{
  int i;
  fprintf (stderr, "  scanner log:");
  for (i = 0; i < fk.nlog; i++)
    fprintf (stderr, " %02X", fk.log[i]);
  fprintf (stderr, "\n");
}

/* ---------------------------------------------------------------- */
/* scanner model: one Port1 command                                  */

void
fk_model_cmd (const unsigned char *cdb, size_t cdb_len, size_t in_req,
           struct fk_reply *r)
{
  unsigned char op = cdb[0];
  unsigned char buf[0x60];

  (void) cdb_len;
  r->scsi = 0;

  switch (op)
    {
    case 0x12:
      memset (buf, 0, sizeof (buf));
      memcpy (buf + 8, "FUJITSU ", 8);
      memcpy (buf + 16, "ScanSnap iX1300 ", 16);
      fk_reply_data (r, buf, in_req < 0x60 ? in_req : 0x60);
      return;

    case 0x03:
      fk_reply_data (r, fk.sense, 18);
      fk_set_sense (0, 0, 0, 0);
      return;

    case 0xC2:
      {
        size_t n = cdb[8] ? cdb[8] : 12;
        unsigned char hw[0x30];
        int left = fk.nsheets - fk.fed;

        memset (hw, 0, sizeof (hw));
        if (fk_now_ms () < fk.eject_until)
          {
            /* D8 + E0: the scanner feeds out the rest of the stack. */
            hw[2] |= 0x80;
            hw[3] |= 0x01;
          }
        else if ((fk.cancelled && !fk.exhausted && left > 0) || fk.stuck)
          {
            /* D8 only: the paper stops in the path and in the chute [HW]. */
            hw[2] |= 0x80;
            hw[3] |= 0x01;
          }
        else if (fk.front_slot)
          {
            /* Front slot sheet [HW]: hopper empty, feed and exit sensors
               set before the job; a clear path after the sheet. */
            hw[3] |= 0x80;
            if (left > 0 && fk.cur < 0)
              {
                hw[2] |= 0x80;
                hw[3] |= 0x01;
              }
          }
        else if (left <= (fk.job ? fk.prefetch : 0))
          hw[3] |= 0x80;
        if (fk.exit_stale)
          hw[3] |= 0x01;
        fk_reply_data (r, hw, n < sizeof (hw) ? n : sizeof (hw));
        return;
      }

    case 0xD5:
      if (fk.job)
        fk.d5_in_job++;
      fk.job = 1;
      fk.cancelled = 0;
      fk.exhausted = 0;
      fk.stuck = 0;             /* the new job feeds the old paper */
      fk.fed = 0;
      fk.cur = -1;
      memset (buf, 0, 8);
      fk_reply_data (r, buf, 8);
      return;

    case 0xD6:
      /* After D8 without E0, D6 does not eject: the paper stays [HW]. */
      if (fk.cancelled && !fk.exhausted)
        fk.stuck = fk.nsheets - fk.fed;
      fk.job = 0;
      fk.cancelled = 0;
      fk.cur = -1;
      return;

    case 0xD8:
      if (fk.job)
        fk.cancelled = 1;
      return;

    case 0xE0:
      if (fk.job && fk.cancelled)
        {
          /* E0 after D8: the scanner feeds ALL remaining sheets out
             without a scan [HW, PROTOCOL.md 8.6]. */
          int n = fk.nsheets - fk.fed;

          fk.cur = -1;
          if (n <= 0)
            {
              r->scsi = 2;
              fk_set_sense (3, 0x80, 0x03, 0);
              return;
            }
          fk.ejected += n;
          fk.fed = fk.nsheets;
          fk.eject_until = fk_now_ms () + fk.eject_ms * n;
          if (fk.e0_cancel_check)
            {
              r->scsi = 2;
              fk_set_sense (5, 0x24, 0x00, 0);
            }
          return;
        }
      if (fk.job && fk.fed < fk.nsheets)
        {
          fk.cur = fk.fed++;
          fk.pos[0] = fk.pos[1] = 0;
          fk.tmp_given[0] = fk.tmp_given[1] = 0;
          return;
        }
      if (fk.job)
        fk.exhausted = 1;
      r->scsi = 2;
      fk_set_sense (3, 0x80, 0x03, 0);
      return;

    case 0x28:
      {
        int side = cdb[5] ? 1 : 0;
        struct fk_side *sd;
        size_t n;
        unsigned char *d;

        if (fk.cancelled)
          {
            /* READ after D8: invalid field [HW]. */
            r->scsi = 2;
            fk_set_sense (5, 0x24, 0x00, 0);
            return;
          }
        if (fk.cur < 0)
          {
            r->scsi = 2;
            fk_set_sense (3, 0x80, 0x03, 0);
            return;
          }
        sd = &fk.sheet[fk.cur][side];
        if (cdb[2] == 0x80)
          {
            unsigned char ps[0x22];
            memset (ps, 0, sizeof (ps));
            ps[0] = cdb[10];
            be32 (ps + 2 + 12, (unsigned long) sd->lines);
            fk_reply_data (r, ps, sizeof (ps));
            return;
          }
        if (fk.temp_first && fk.pos[side] == 0 && !fk.tmp_given[side])
          {
            fk.tmp_given[side] = 1;
            r->scsi = 2;
            fk_set_sense (3, 0x80, 0x13, 0);
            return;
          }
        n = sd->len - fk.pos[side];
        if (n > fk.chunk)
          n = fk.chunk;
        d = malloc (n + 2);
        d[0] = cdb[10];
        d[1] = cdb[11];
        memcpy (d + 2, sd->jpeg + fk.pos[side], n);
        fk.pos[side] += n;
        free (r->data);
        r->data = d;
        r->dlen = n + 2;
        if (fk.pos[side] >= sd->len)
          {
            r->scsi = 2;
            fk_set_sense (0, 0, 0, 1);
          }
        return;
      }

    default:                    /* E9, D4 and the rest */
      return;
    }
}

/* ---------------------------------------------------------------- */
/* Port1 and Port2 server threads                                    */

static int
rd (int fd, unsigned char *p, size_t n)
{
  size_t got = 0;
  while (got < n)
    {
      ssize_t r = read (fd, p + got, n - got);
      if (r <= 0)
        return -1;
      got += (size_t) r;
    }
  return 0;
}

static int
wr (int fd, const unsigned char *p, size_t n, int trickle_ms)
{
  size_t i;

  if (!trickle_ms)
    return write (fd, p, n) == (ssize_t) n ? 0 : -1;
  for (i = 0; i < n; i++)
    {
      if (write (fd, p + i, 1) != 1)
        return -1;
      real_sleep_ms (trickle_ms);
    }
  return 0;
}

static void
greet (int fd)
{
  unsigned char g[16];
  memset (g, 0, sizeof (g));
  be32 (g, 16);
  memcpy (g + 4, KEY, 4);
  wr (fd, g, sizeof (g), 0);
}

struct srv
{
  int fd;
  int port2;
};

static void
serve_port2 (int fd)
{
  unsigned char m[256], r[20];
  unsigned long len;

  if (rd (fd, m, 4))
    return;
  len = gbe32 (m);
  if (len < 12 || len > sizeof (m) || rd (fd, m + 4, len - 4))
    return;
  memset (r, 0, sizeof (r));
  memcpy (r + 4, KEY, 4);
  pthread_mutex_lock (&fk.mu);
  if (gbe32 (m + 8) == 0x11)
    {
      fk.n_reserve++;
      be32 (r, 20);
      be32 (r + 8, (unsigned long) (long) fk.reserve_result);
      if (fk.reserve_result == 0)
        fk.reserved = 1;
      pthread_mutex_unlock (&fk.mu);
      wr (fd, r, 20, 0);
    }
  else
    {
      fk.n_release++;
      fk.reserved = 0;
      be32 (r, 16);
      pthread_mutex_unlock (&fk.mu);
      wr (fd, r, 16, 0);
    }
}

static void
serve_port1 (int fd)
{
  pthread_mutex_lock (&fk.mu);
  fk.p1_peer = fd;
  pthread_mutex_unlock (&fk.mu);
  for (;;)
    {
      unsigned char h[64];
      unsigned char *out = NULL;
      unsigned char rh[40];
      unsigned long total;
      size_t cdb_len, in_req;
      struct fk_reply r;
      int handled = 0;
      size_t dlen;

      if (rd (fd, h, 64))
        return;
      total = gbe32 (h);
      if (total < 64 || total > 64 + 4096)
        return;
      if (total > 64)
        {
          out = malloc (total - 64);
          if (rd (fd, out, total - 64))
            {
              free (out);
              return;
            }
          pthread_mutex_lock (&fk.mu);
          if (h[48] == 0xE9 && total - 64 <= sizeof (fk.e9))
            {
              memset (fk.e9, 0, sizeof (fk.e9));
              memcpy (fk.e9, out, total - 64);
              fk.n_e9++;
            }
          if (h[48] == 0xD4 && total - 64 <= sizeof (fk.d4))
            {
              memset (fk.d4, 0, sizeof (fk.d4));
              memcpy (fk.d4, out, total - 64);
              fk.n_d4++;
            }
          pthread_mutex_unlock (&fk.mu);
        }
      free (out);
      cdb_len = gbe32 (h + 32);
      in_req = gbe32 (h + 36);
      if (cdb_len > 16)
        return;

      memset (&r, 0, sizeof (r));
      r.total_override = -1;
      pthread_mutex_lock (&fk.mu);
      if (fk.nlog < (int) sizeof (fk.log))
        fk.log[fk.nlog++] = h[48];
      fk.cnt[h[48]]++;
      if (fk.need_reserve && !fk.reserved && h[48] != 0x12 && h[48] != 0xC2)
        r.close_now = 1;
      else
        {
          if (fk.hook)
            handled = fk.hook (h + 48, cdb_len, in_req, &r);
          if (!handled)
            fk_model_cmd (h + 48, cdb_len, in_req, &r);
        }
      pthread_mutex_unlock (&fk.mu);

      if (r.advance_ms)
        fk_advance_ms (r.advance_ms);
      if (r.close_now)
        {
          free (r.data);
          return;
        }
      dlen = r.dlen + r.extra;
      memset (rh, 0, sizeof (rh));
      be32 (rh, r.total_override >= 0 ? (unsigned long) r.total_override
            : 40 + dlen);
      memcpy (rh + 4, KEY, 4);
      be32 (rh + 12, (unsigned long) r.scsi);
      if (wr (fd, rh, 40, r.trickle_data_only ? 0 : r.trickle_ms)
          || (r.dlen && wr (fd, r.data, r.dlen, r.trickle_ms)))
        {
          free (r.data);
          return;
        }
      if (r.extra)
        {
          unsigned char *z = calloc (1, r.extra);
          int bad = wr (fd, z, r.extra, 0);
          free (z);
          if (bad)
            {
              free (r.data);
              return;
            }
        }
      free (r.data);
    }
}

static void *
srv_main (void *arg)
{
  struct srv *sv = arg;

  greet (sv->fd);
  if (sv->port2)
    serve_port2 (sv->fd);
  else
    serve_port1 (sv->fd);
  close (sv->fd);
  free (sv);
  return NULL;
}

/* ---------------------------------------------------------------- */
/* socket table                                                      */

#define MAXFD 1024
#define QMAX 32

struct pkt
{
  unsigned char d[256];
  size_t n;
  struct sockaddr_in from;
};

struct fdinfo
{
  int kind;                     /* 0 none, 1 UDP, 2 TCP */
  int peer;                     /* TCP: the scanner end */
  int port;                     /* UDP: bound port */
  struct pkt q[QMAX];
  int qh, qn;
};

static struct fdinfo fds[MAXFD];
static pthread_mutex_t fd_mu = PTHREAD_MUTEX_INITIALIZER;

static void
q_push (int fd, const void *p, size_t n, const char *ip, int port)
{
  struct fdinfo *f = &fds[fd];
  struct pkt *k;

  if (f->qn >= QMAX)
    return;
  k = &f->q[(f->qh + f->qn) % QMAX];
  if (n > sizeof (k->d))
    n = sizeof (k->d);
  memcpy (k->d, p, n);
  k->n = n;
  memset (&k->from, 0, sizeof (k->from));
  k->from.sin_family = AF_INET;
  k->from.sin_port = htons (port);
  inet_aton (ip, &k->from.sin_addr);
  f->qn++;
}

void
fk_udp_inject (int fd, const void *p, size_t len, const char *src_ip,
               int src_port)
{
  pthread_mutex_lock (&fd_mu);
  if (fd >= 0 && fd < MAXFD && fds[fd].kind == 1)
    q_push (fd, p, len, src_ip, src_port);
  pthread_mutex_unlock (&fd_mu);
}

size_t
fk_disc_reply (unsigned char *r, const char *ip)
{
  struct in_addr a;

  memset (r, 0, 144);
  memcpy (r, KEY, 4);
  inet_aton (ip, &a);
  memcpy (r + 16, &a.s_addr, 4);
  be32 (r + 20, 53218);
  be32 (r + 24, 53219);
  memcpy (r + 40, "fake-ix1300", 11);
  memcpy (r + 104, "ScanSnap iX1300", 15);
  memcpy (r + 132, "FAKE0001", 8);
  return 144;
}

int
h_socket (int domain, int type, int proto)
{
  int fd;

  if (domain != AF_INET)
    return socket (domain, type, proto);
  if (type == SOCK_DGRAM)
    {
      fd = open ("/dev/null", O_RDWR);
      if (fd < 0 || fd >= MAXFD)
        return -1;
      pthread_mutex_lock (&fd_mu);
      memset (&fds[fd], 0, sizeof (fds[fd]));
      fds[fd].kind = 1;
      pthread_mutex_unlock (&fd_mu);
      return fd;
    }
  if (type == SOCK_STREAM)
    {
      int sv[2];
      if (socketpair (AF_UNIX, SOCK_STREAM, 0, sv) < 0)
        return -1;
      pthread_mutex_lock (&fd_mu);
      memset (&fds[sv[0]], 0, sizeof (fds[0]));
      fds[sv[0]].kind = 2;
      fds[sv[0]].peer = sv[1];
      pthread_mutex_unlock (&fd_mu);
      return sv[0];
    }
  errno = EINVAL;
  return -1;
}

int
h_connect (int fd, const struct sockaddr *sa, socklen_t sl)
{
  const struct sockaddr_in *in = (const struct sockaddr_in *) sa;
  struct srv *sv;
  pthread_t t;
  int peer;

  (void) sl;
  if (fd < 0 || fd >= MAXFD || !fds[fd].kind)
    {
      errno = EBADF;
      return -1;
    }
  if (fds[fd].kind == 1)
    return 0;
  if (fk.connect_fail)
    {
      errno = ECONNREFUSED;
      return -1;
    }
  pthread_mutex_lock (&fd_mu);
  peer = fds[fd].peer;
  fds[fd].peer = -1;
  pthread_mutex_unlock (&fd_mu);
  if (peer < 0)
    {
      errno = EISCONN;
      return -1;
    }
  sv = malloc (sizeof (*sv));
  sv->fd = peer;
  sv->port2 = ntohs (in->sin_port) == 53219;
  if (!sv->port2)
    {
      pthread_mutex_lock (&fk.mu);
      fk.port1_conns++;
      pthread_mutex_unlock (&fk.mu);
    }
  pthread_create (&t, NULL, srv_main, sv);
  pthread_detach (t);
  return 0;
}

int
h_bind (int fd, const struct sockaddr *sa, socklen_t sl)
{
  const struct sockaddr_in *in = (const struct sockaddr_in *) sa;
  (void) sl;
  if (fd >= 0 && fd < MAXFD && fds[fd].kind == 1)
    {
      fds[fd].port = ntohs (in->sin_port) ? ntohs (in->sin_port) : 40001;
      return 0;
    }
  return bind (fd, sa, sl);
}

int
h_getsockname (int fd, struct sockaddr *sa, socklen_t * sl)
{
  struct sockaddr_in *in = (struct sockaddr_in *) sa;

  if (fd >= 0 && fd < MAXFD && fds[fd].kind == 1)
    {
      memset (in, 0, sizeof (*in));
      in->sin_family = AF_INET;
      in->sin_port = htons (fds[fd].port);
      inet_aton (FK_HOST_IP, &in->sin_addr);
      *sl = sizeof (*in);
      return 0;
    }
  return getsockname (fd, sa, sl);
}

ssize_t
h_sendto (int fd, const void *p, size_t n, int fl, const struct sockaddr *to,
          socklen_t tl)
{
  const unsigned char *b = p;
  unsigned char r[256];
  size_t rn;

  if (fd < 0 || fd >= MAXFD || fds[fd].kind != 1)
    return sendto (fd, p, n, fl, to, tl);
  if (n < 32 || memcmp (b, KEY, 4))
    return (ssize_t) n;

  pthread_mutex_lock (&fd_mu);
  if (gbe32 (b + 4) == 0)
    {
      fk.n_probe++;
      if (fk.disc_mode == 2)
        {
          static const unsigned char junk[10] = "junkjunk!";
          q_push (fd, junk, sizeof (junk), "10.0.0.77", 52217);
        }
      if (fk.disc_mode == 1)
        {
          rn = fk_disc_reply (r, FK_SCANNER_IP);
          q_push (fd, r, rn, "10.0.0.66", 52217);
        }
      else
        {
          rn = fk_disc_reply (r, FK_SCANNER_IP);
          q_push (fd, r, rn, FK_SCANNER_IP, 52217);
        }
    }
  else
    {
      fk.n_ka++;
      memset (r, 0, 12);
      memcpy (r, KEY, 4);
      r[4] = 0x80;
      r[5] = 0x01;
      if (fk.ka_mode == 2)
        memcpy (r, "XXXX", 4);
      if (fk.reserved && fk.ka_mode != 1)
        q_push (fd, r, 12, fk.ka_mode == 3 ? "10.0.0.66" : FK_SCANNER_IP,
                52217);
    }
  pthread_mutex_unlock (&fd_mu);
  return (ssize_t) n;
}

static ssize_t
udp_pop (int fd, void *p, size_t n, struct sockaddr *from, socklen_t * fl)
{
  struct fdinfo *f = &fds[fd];
  struct pkt *k;
  size_t c;

  pthread_mutex_lock (&fd_mu);
  if (!f->qn)
    {
      pthread_mutex_unlock (&fd_mu);
      errno = EAGAIN;
      return -1;
    }
  k = &f->q[f->qh];
  c = k->n < n ? k->n : n;
  memcpy (p, k->d, c);
  if (from && fl)
    {
      socklen_t l = *fl < sizeof (k->from) ? *fl : sizeof (k->from);
      memcpy (from, &k->from, l);
      *fl = sizeof (k->from);
    }
  f->qh = (f->qh + 1) % QMAX;
  f->qn--;
  pthread_mutex_unlock (&fd_mu);
  return (ssize_t) c;
}

ssize_t
h_recv (int fd, void *p, size_t n, int fl)
{
  if (fd >= 0 && fd < MAXFD && fds[fd].kind == 1)
    return udp_pop (fd, p, n, NULL, NULL);
  return recv (fd, p, n, fl);
}

ssize_t
h_recvfrom (int fd, void *p, size_t n, int fl, struct sockaddr *from,
            socklen_t * sl)
{
  if (fd >= 0 && fd < MAXFD && fds[fd].kind == 1)
    return udp_pop (fd, p, n, from, sl);
  return recvfrom (fd, p, n, fl, from, sl);
}

int
h_poll (struct pollfd *p, nfds_t n, int ms)
{
  nfds_t i;
  int fake = 0, ready = 0;

  for (i = 0; i < n; i++)
    if (p[i].fd >= 0 && p[i].fd < MAXFD && fds[p[i].fd].kind == 1)
      fake = 1;
  if (!fake)
    return poll (p, n, ms);
  pthread_mutex_lock (&fd_mu);
  for (i = 0; i < n; i++)
    {
      p[i].revents = 0;
      if (p[i].fd >= 0 && p[i].fd < MAXFD && fds[p[i].fd].kind == 1
          && fds[p[i].fd].qn && (p[i].events & POLLIN))
        {
          p[i].revents = POLLIN;
          ready++;
        }
    }
  pthread_mutex_unlock (&fd_mu);
  if (!ready && ms != 0)
    real_sleep_ms (ms < 0 || ms > 5 ? 5 : ms);
  return ready;
}

int
h_close (int fd)
{
  if (fd >= 0 && fd < MAXFD)
    {
      pthread_mutex_lock (&fd_mu);
      if (fds[fd].kind == 2 && fds[fd].peer >= 0)
        close (fds[fd].peer);
      fds[fd].kind = 0;
      pthread_mutex_unlock (&fd_mu);
    }
  return close (fd);
}

int
h_ioctl (int fd, unsigned long req, void *arg)
{
  struct ifreq *ifr = arg;

  (void) fd;
  if (req == SIOCGIFADDR)
    {
      struct sockaddr_in *in = (struct sockaddr_in *) &ifr->ifr_addr;
      memset (in, 0, sizeof (*in));
      in->sin_family = AF_INET;
      inet_aton (FK_HOST_IP, &in->sin_addr);
      return 0;
    }
#ifdef SIOCGIFHWADDR
  if (req == SIOCGIFHWADDR)     /* Linux only: the MAC of the interface */
    {
      static const unsigned char mac[6] = { 0x02, 0, 0, 0, 0, 0x01 };

      memset (&ifr->ifr_hwaddr, 0, sizeof (ifr->ifr_hwaddr));
      memcpy (ifr->ifr_hwaddr.sa_data, mac, sizeof (mac));
      return 0;
    }
#endif
  errno = EINVAL;
  return -1;
}

int
h_pthread_create (pthread_t * t, const pthread_attr_t * a,
                  void *(*f) (void *), void *arg)
{
  if (fk.thread_fail)
    return EAGAIN;
  return pthread_create (t, a, f, arg);
}
