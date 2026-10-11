/* Test harness for the SANE ix1300 backend.
 *
 * The harness compiles ix1300.c (IX_SRC) together with stub SANE headers
 * and a fake network (fakenet.c). A thread plays the scanner on Port1 and
 * Port2. Each test runs in a child process. A crash or an
 * AddressSanitizer report in the child is a failure of the test.
 *
 * Usage: ix1300-test [-l] [TEST ...]
 *   -l    list the tests
 *   TEST  run only these tests (default: all tests)
 * Environment: IXTEST_DEBUG=<level> prints the DBG output of the backend.
 */

#include <errno.h>
#include <fcntl.h>
#include <setjmp.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <time.h>
#include <unistd.h>
#include <pthread.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <net/if.h>
#include <ifaddrs.h>
#include <poll.h>

#include "fakenet.h"
#include "sane/sane.h"
#include "sane/sanei.h"
#include "sane/sanei_config.h"
#include "sane/sanei_backend.h"

/* ---------------------------------------------------------------- */
/* SANE library stubs                                                */

int h_dbg_gate = 30;
static int h_dbg_print;
static void (*h_dbg_hook) (const char *msg);
static const char *h_watch;
static int h_watch_seen;
static const char *h_config;

void
h_dbg (int level, const char *fmt, ...)
{
  char msg[1024];
  va_list ap;

  va_start (ap, fmt);
  vsnprintf (msg, sizeof (msg), fmt, ap);
  va_end (ap);
  if (level <= h_dbg_print)
    fprintf (stderr, "[ix1300 %2d] %s", level, msg);
  if (h_watch && strstr (msg, h_watch))
    h_watch_seen = 1;
  if (h_dbg_hook)
    h_dbg_hook (msg);
}

FILE *
sanei_config_open (const char *name)
{
  (void) name;
  if (!h_config)
    return NULL;
  return fmemopen ((void *) h_config, strlen (h_config), "r");
}

char *
sanei_config_read (char *str, int n, FILE * stream)
{
  char *p;
  size_t l;

  if (!fgets (str, n, stream))
    return NULL;
  l = strlen (str);
  while (l && isspace ((unsigned char) str[l - 1]))
    str[--l] = '\0';
  p = str;
  while (isspace ((unsigned char) *p))
    p++;
  memmove (str, p, strlen (p) + 1);
  return str;
}

SANE_Status
sanei_constrain_value (const SANE_Option_Descriptor * opt, void *value,
                       SANE_Word * info)
{
  (void) opt;
  (void) value;
  (void) info;
  return SANE_STATUS_GOOD;
}

SANE_String_Const
sane_strstatus (SANE_Status st)
{
  static const char *n[] = { "GOOD", "UNSUPPORTED", "CANCELLED",
    "DEVICE_BUSY", "INVAL", "EOF", "JAMMED", "NO_DOCS", "COVER_OPEN",
    "IO_ERROR", "NO_MEM", "ACCESS_DENIED"
  };
  if ((int) st >= 0 && (int) st < 12)
    return n[st];
  return "?";
}

/* ---------------------------------------------------------------- */
/* The backend, with the network calls sent to the fake              */

#define socket(a, b, c) h_socket (a, b, c)
#define connect(a, b, c) h_connect (a, b, c)
#define bind(a, b, c) h_bind (a, b, c)
#define getsockname(a, b, c) h_getsockname (a, b, c)
#define sendto(a, b, c, d, e, f) h_sendto (a, b, c, d, e, f)
#define recv(a, b, c, d) h_recv (a, b, c, d)
#define recvfrom(a, b, c, d, e, f) h_recvfrom (a, b, c, d, e, f)
#define poll(a, b, c) h_poll (a, b, c)
#define close(a) h_close (a)
#define ioctl(a, b, c) h_ioctl (a, b, c)
#define time(a) h_time (a)
#define usleep(a) h_usleep (a)
#define clock_gettime(a, b) h_clock_gettime (a, b)
#define pthread_create(a, b, c, d) h_pthread_create (a, b, c, d)

#include IX_SRC

#undef socket
#undef connect
#undef bind
#undef getsockname
#undef sendto
#undef recv
#undef recvfrom
#undef poll
#undef close
#undef ioctl
#undef time
#undef usleep
#undef clock_gettime
#undef pthread_create

/* ---------------------------------------------------------------- */
/* test helpers                                                      */

static const char *CONF =
  "net 10.0.0.5\nhostid 0123456789ABCDEF\niface fake0\n"
  "discovery-timeout 1000\n";

static SANE_Handle H;
static char fail_msg[512];

#define CHECK(cond, ...) do { if (!(cond)) { \
  snprintf (fail_msg, sizeof (fail_msg), __VA_ARGS__); return 1; } } while (0)

/* A test JPEG: RGB, 4:4:4, pattern that changes in x and y. */
static unsigned char *
make_jpeg (int w, int h, size_t *len)
{
  struct jpeg_compress_struct c;
  struct jpeg_error_mgr e;
  unsigned char *out = NULL;
  unsigned long outlen = 0;
  unsigned char *row = malloc ((size_t) w * 3);
  int x, y;

  c.err = jpeg_std_error (&e);
  jpeg_create_compress (&c);
  jpeg_mem_dest (&c, &out, &outlen);
  c.image_width = (JDIMENSION) w;
  c.image_height = (JDIMENSION) h;
  c.input_components = 3;
  c.in_color_space = JCS_RGB;
  jpeg_set_defaults (&c);
  jpeg_set_quality (&c, 90, TRUE);
  c.comp_info[0].h_samp_factor = c.comp_info[0].v_samp_factor = 1;
  jpeg_start_compress (&c, TRUE);
  for (y = 0; y < h; y++)
    {
      JSAMPROW r = row;
      for (x = 0; x < w; x++)
        {
          row[3 * x] = (unsigned char) ((x * 7 + y * 3) & 0xff);
          row[3 * x + 1] = (unsigned char) ((y * 5) & 0xff);
          row[3 * x + 2] = (unsigned char) (((x / 8 + y / 8) & 1) ? 200 : 30);
        }
      jpeg_write_scanlines (&c, &r, 1);
    }
  jpeg_finish_compress (&c);
  jpeg_destroy_compress (&c);
  free (row);
  *len = outlen;
  return out;
}

/* Reference decode: the first LINES rows, RGB. */
static unsigned char *
ref_decode (const unsigned char *j, size_t len, int lines, size_t *outlen)
{
  struct jpeg_decompress_struct d;
  struct jpeg_error_mgr e;
  unsigned char *out;
  size_t bpl;
  int y;

  d.err = jpeg_std_error (&e);
  jpeg_create_decompress (&d);
  jpeg_mem_src (&d, (unsigned char *) j, (unsigned long) len);
  jpeg_read_header (&d, TRUE);
  d.out_color_space = JCS_RGB;
  jpeg_start_decompress (&d);
  bpl = (size_t) d.output_width * 3;
  out = malloc (bpl * (size_t) lines);
  for (y = 0; y < lines; y++)
    {
      JSAMPROW r = out + bpl * (size_t) y;
      jpeg_read_scanlines (&d, &r, 1);
    }
  jpeg_abort_decompress (&d);
  jpeg_destroy_decompress (&d);
  *outlen = bpl * (size_t) lines;
  return out;
}

#define IMG_W 320
#define IMG_H 400
static unsigned char *J;
static size_t JLEN;

/* Load NSHEETS sheets with the test JPEG on each side. */
static void
load_sheets (int n, int lines)
{
  int i;
  if (!J)
    J = make_jpeg (IMG_W, IMG_H, &JLEN);
  fk.nsheets = n;
  for (i = 0; i < n && i < FK_MAX_SHEETS; i++)
    {
      fk.sheet[i][0].jpeg = fk.sheet[i][1].jpeg = J;
      fk.sheet[i][0].len = fk.sheet[i][1].len = JLEN;
      fk.sheet[i][0].lines = fk.sheet[i][1].lines = lines;
    }
}

static SANE_Status
open_dev (void)
{
  h_config = h_config ? h_config : CONF;
  sane_init (NULL, NULL);
  return sane_open ("10.0.0.5", &H);
}

static SANE_Status
set_str (int opt, const char *v)
{
  char buf[64];
  snprintf (buf, sizeof (buf), "%s", v);
  return sane_control_option (H, opt, SANE_ACTION_SET_VALUE, buf, NULL);
}

static SANE_Status
set_int (int opt, SANE_Word v)
{
  return sane_control_option (H, opt, SANE_ACTION_SET_VALUE, &v, NULL);
}

static SANE_Word
get_int (int opt)
{
  SANE_Word v = -1;
  sane_control_option (H, opt, SANE_ACTION_GET_VALUE, &v, NULL);
  return v;
}

/* Read one frame. Return the last status. */
static SANE_Status
read_frame (unsigned char **out, size_t *n, int chunk)
{
  unsigned char *buf = malloc ((size_t) chunk);
  unsigned char *acc = NULL;
  size_t total = 0;
  SANE_Status st;

  for (;;)
    {
      SANE_Int len = 0;
      st = sane_read (H, buf, chunk, &len);
      if (len > 0)
        {
          acc = realloc (acc, total + (size_t) len);
          memcpy (acc + total, buf, (size_t) len);
          total += (size_t) len;
        }
      if (st != SANE_STATUS_GOOD)
        break;
    }
  free (buf);
  if (out)
    *out = acc;
  else
    free (acc);
  if (n)
    *n = total;
  return st;
}

static int
is_read_data (const unsigned char *cdb)
{
  return cdb[0] == 0x28 && cdb[2] == 0;
}

/* ---------------------------------------------------------------- */
/* tests                                                             */

/* Normal duplex job of two sheets. All frames decode correctly. */
static int
t_duplex_decode (void)
{
  unsigned char *img, *ref;
  size_t n, rn;
  int f;
  SANE_Status st;

  load_sheets (2, IMG_H - 16);
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  set_str (OPT_SOURCE, "ADF Duplex");
  ref = ref_decode (J, JLEN, IMG_H - 16, &rn);
  for (f = 0; f < 4; f++)
    {
      st = sane_start (H);
      CHECK (st == SANE_STATUS_GOOD, "frame %d: sane_start %s", f,
             sane_strstatus (st));
      st = read_frame (&img, &n, 32768);
      CHECK (st == SANE_STATUS_EOF, "frame %d: read %s", f,
             sane_strstatus (st));
      CHECK (n == rn && !memcmp (img, ref, rn),
             "frame %d: %lu bytes, %lu expected, or other pixels", f,
             (unsigned long) n, (unsigned long) rn);
      free (img);
    }
  st = sane_start (H);
  CHECK (st == SANE_STATUS_NO_DOCS, "end of stack: %s", sane_strstatus (st));
  sane_cancel (H);
  CHECK (fk.job == 0, "the job is still active in the scanner");
  sane_close (H);
  return 0;
}

/* B1: the front READ of sheet 2 gives no data (busy). C2 shows an empty
   hopper (prefetch). The result must be an error, not NO_DOCS. */
static int
hook_b1a (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  (void) cl;
  (void) in;
  if (is_read_data (cdb) && fk.cur == 1 && cdb[5] == 0)
    {
      r->scsi = 8;
      r->advance_ms = 200;
      return 1;
    }
  return 0;
}

static int
t_b1_timeout_prefetch (void)
{
  SANE_Status st;

  load_sheets (3, IMG_H);
  fk.hook = hook_b1a;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet 1 start");
  CHECK (read_frame (NULL, NULL, 65536) == SANE_STATUS_EOF, "sheet 1 read");
  st = sane_start (H);
  CHECK (st != SANE_STATUS_NO_DOCS && st != SANE_STATUS_GOOD,
         "sheet 2 without data gave %s (an error is necessary)",
         sane_strstatus (st));
  sane_close (H);
  return 0;
}

/* B1: sense 3/80/03 on the back of sheet 2 is a lost sheet, not the
   normal end of the stack. */
static int
hook_b1b (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  (void) cl;
  (void) in;
  if (is_read_data (cdb) && fk.cur == 1 && cdb[5] == 0x80)
    {
      r->scsi = 2;
      fk_set_sense (3, 0x80, 0x03, 0);
      return 1;
    }
  return 0;
}

static int
t_b1_back_paper_lost (void)
{
  SANE_Status st;

  load_sheets (2, IMG_H);
  fk.hook = hook_b1b;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  set_str (OPT_SOURCE, "ADF Duplex");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet 1 front");
  read_frame (NULL, NULL, 65536);
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet 1 back");
  read_frame (NULL, NULL, 65536);
  st = sane_start (H);
  CHECK (st != SANE_STATUS_NO_DOCS && st != SANE_STATUS_GOOD,
         "no paper on the back of sheet 2 gave %s", sane_strstatus (st));
  sane_close (H);
  return 0;
}

/* B1: sense 3/80/03 after front data of sheet 2 is a lost sheet. */
static int
hook_b1c (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  (void) cl;
  (void) in;
  if (is_read_data (cdb) && fk.cur == 1 && cdb[5] == 0 && fk.pos[0] > 0)
    {
      r->scsi = 2;
      fk_set_sense (3, 0x80, 0x03, 0);
      return 1;
    }
  return 0;
}

static int
t_b1_front_lost_after_data (void)
{
  SANE_Status st;

  load_sheets (2, IMG_H);
  fk.chunk = 2000;
  fk.hook = hook_b1c;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet 1");
  read_frame (NULL, NULL, 65536);
  st = sane_start (H);
  CHECK (st != SANE_STATUS_NO_DOCS && st != SANE_STATUS_GOOD,
         "no paper after front data of sheet 2 gave %s", sane_strstatus (st));
  sane_close (H);
  return 0;
}

/* B1 (regression): E0 is good (prefetch), but the first READ of the new
   sheet gives 3/80/03. This is the normal end: NO_DOCS. */
static int
hook_b1e (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  (void) cl;
  (void) in;
  if (cdb[0] == 0xE0 && fk.job && fk.fed >= fk.nsheets)
    {
      fk.cur = -1;
      return 1;                 /* good, but no sheet */
    }
  return 0;
}

static int
t_b1_end_on_first_read (void)
{
  SANE_Status st;

  load_sheets (1, IMG_H);
  fk.hook = hook_b1e;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet 1");
  read_frame (NULL, NULL, 65536);
  st = sane_start (H);
  CHECK (st == SANE_STATUS_NO_DOCS, "end on the first READ gave %s",
         sane_strstatus (st));
  sane_cancel (H);
  CHECK (fk.job == 0, "the job is still active in the scanner");
  sane_close (H);
  return 0;
}

/* B2: a CHECK CONDITION with sense key 0 and no EOM does not end the side. */
static int
hook_b2 (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  if (is_read_data (cdb) && cdb[5] == 0 && fk.tmp_given[0] && fk.pos[0] == 0)
    {
      fk_model_cmd (cdb, cl, in, r);
      r->scsi = 2;
      fk_set_sense (0, 0, 0, 0);
      return 1;
    }
  return 0;
}

static int
t_b2_key0_no_eom (void)
{
  unsigned char *img;
  size_t n;
  SANE_Status st;

  load_sheets (1, IMG_H);
  fk.chunk = 4000;
  fk.hook = hook_b2;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  set_str (OPT_COMPRESS, "JPEG");
  st = sane_start (H);
  CHECK (st == SANE_STATUS_GOOD, "start %s", sane_strstatus (st));
  st = read_frame (&img, &n, 65536);
  CHECK (st == SANE_STATUS_EOF, "read %s", sane_strstatus (st));
  CHECK (n == JLEN && !memcmp (img, J, JLEN),
         "front side has %lu of %lu JPEG bytes", (unsigned long) n,
         (unsigned long) JLEN);
  free (img);
  sane_close (H);
  return 0;
}

/* B4: a reply header with total < 40 is not valid. */
static int
hook_b4a (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  (void) cl;
  (void) in;
  if (cdb[0] == 0xE9)
    {
      r->total_override = 12;
      return 1;
    }
  return 0;
}

static int
t_b4_short_header (void)
{
  SANE_Status st;

  load_sheets (1, IMG_H);
  fk.hook = hook_b4a;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  st = sane_start (H);
  CHECK (st != SANE_STATUS_GOOD, "reply total 12 was accepted (%s)",
         sane_strstatus (st));
  sane_close (H);
  return 0;
}

/* B4: a reply with much more data than requested is not valid. */
static int
hook_b4b (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  (void) cl;
  (void) in;
  if (cdb[0] == 0xD4)
    {
      r->extra = 5000;
      return 1;
    }
  return 0;
}

static int
t_b4_oversize_reply (void)
{
  SANE_Status st;

  load_sheets (1, IMG_H);
  fk.hook = hook_b4b;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  st = sane_start (H);
  CHECK (st != SANE_STATUS_GOOD, "5000 extra data bytes were accepted");
  sane_close (H);
  return 0;
}

/* B4: a slow side that makes progress (9 s between chunks, 90 s in
   total) must not time out. */
static int b4c_busy;
static int
hook_b4c (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  (void) cl;
  (void) in;
  if (is_read_data (cdb) && cdb[5] == 0 && fk.tmp_given[0])
    {
      b4c_busy = !b4c_busy;
      if (b4c_busy)
        {
          r->scsi = 8;
          r->advance_ms = 9000;
          return 1;
        }
    }
  return 0;
}

static int
t_b4_progress_refresh (void)
{
  SANE_Status st;

  load_sheets (1, IMG_H);
  fk.chunk = JLEN / 10 + 1;
  fk.hook = hook_b4c;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  st = sane_start (H);
  CHECK (st == SANE_STATUS_GOOD, "slow side with progress gave %s",
         sane_strstatus (st));
  sane_close (H);
  return 0;
}

/* B4: GOOD READ replies without image data for 200 s must time out. */
static int b4d_count;
static int
hook_b4d (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  unsigned char pfx[2];

  (void) cl;
  (void) in;
  if (is_read_data (cdb) && cdb[5] == 0 && fk.tmp_given[0] && b4d_count < 200)
    {
      b4d_count++;
      pfx[0] = cdb[10];
      pfx[1] = cdb[11];
      fk_reply_data (r, pfx, 2);
      r->advance_ms = 1000;
      return 1;
    }
  return 0;
}

static int
t_b4_no_progress_good (void)
{
  SANE_Status st;

  load_sheets (1, IMG_H);
  fk.hook = hook_b4d;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  st = sane_start (H);
  CHECK (st != SANE_STATUS_GOOD,
         "200 s of empty GOOD replies were accepted (%d replies)", b4d_count);
  sane_close (H);
  return 0;
}

/* B4: a reply that comes 1 byte each 250 ms (about 15 s) must stop at the
   frame deadline (timeout 5 s). */
static int
hook_b4e (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  if (is_read_data (cdb) && cdb[5] == 0 && fk.tmp_given[0] && fk.pos[0] == 0)
    {
      size_t keep = fk.chunk;
      fk.chunk = 20;
      fk_model_cmd (cdb, cl, in, r);
      fk.chunk = keep;
      r->trickle_ms = 250;
      return 1;
    }
  return 0;
}

static int
t_b4_trickle (void)
{
  SANE_Status st;
  long t0;

  h_config = "net 10.0.0.5\nhostid 0123456789ABCDEF\niface fake0\n"
    "discovery-timeout 1000\ntimeout 5\n";
  load_sheets (1, IMG_H);
  fk.hook = hook_b4e;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  t0 = fk_now_ms ();
  st = sane_start (H);
  CHECK (st != SANE_STATUS_GOOD,
         "a reply of 15 s was accepted with timeout 5 (%ld ms)",
         fk_now_ms () - t0);
  sane_close (H);
  return 0;
}

/* B4: image_append with a length near SIZE_MAX must not wrap. */
static int
t_b4_append_wrap (void)
{
  struct ix1300_image img;
  unsigned char src[8] = { 0 };
  SANE_Status st;

  img.data = malloc (16);
  img.len = (size_t) -1 - 2;
  img.size = 16;
  st = image_append (&img, src, sizeof (src));
  CHECK (st != SANE_STATUS_GOOD, "image_append wrapped and gave GOOD");
  img.len = 0;
  image_free (&img);
  return 0;
}

/* B4: a side of more than 64 MiB is not valid. */
static int
hook_b4g (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  static int n;
  if (is_read_data (cdb) && cdb[5] == 0 && fk.tmp_given[0])
    {
      if (fk.pos[0] < fk.sheet[0][0].len)
        {
          fk_model_cmd (cdb, cl, in, r);
          r->scsi = 0;          /* more data follows */
          return 1;
        }
      if (n++ < 24)
        {
          unsigned char *d = calloc (1, 0x300000);
          d[0] = cdb[10];
          d[1] = cdb[11];
          free (r->data);
          r->data = d;
          r->dlen = 0x300000;
          return 1;
        }
      r->scsi = 2;
      fk_set_sense (0, 0, 0, 1);
      return 1;
    }
  return 0;
}

static int
t_b4_side_limit (void)
{
  SANE_Status st;

  load_sheets (1, IMG_H);
  fk.hook = hook_b4g;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  set_str (OPT_COMPRESS, "JPEG");
  st = sane_start (H);
  CHECK (st != SANE_STATUS_GOOD, "a side of 72 MiB was accepted");
  sane_close (H);
  return 0;
}

/* B5: a JPEG that stops in the entropy data must give IO_ERROR. */
static int
run_truncated (size_t cut, SANE_Status * end, size_t *got, size_t *want)
{
  static unsigned char *t;
  SANE_Status st;

  if (!J)
    J = make_jpeg (IMG_W, IMG_H, &JLEN);
  load_sheets (1, IMG_H);
  t = malloc (cut);
  memcpy (t, J, cut);
  fk.sheet[0][0].jpeg = fk.sheet[0][1].jpeg = t;
  fk.sheet[0][0].len = fk.sheet[0][1].len = cut;
  if (open_dev () != SANE_STATUS_GOOD)
    return 1;
  st = sane_start (H);
  if (st != SANE_STATUS_GOOD)
    {
      *end = st;
      *got = 0;
      *want = 0;
      return 0;
    }
  *end = read_frame (NULL, got, 65536);
  *want = (size_t) IMG_W * 3 * IMG_H;
  return 0;
}

static size_t
sos_offset (void)
{
  size_t i;
  for (i = 2; i + 1 < JLEN; i++)
    if (J[i] == 0xFF && J[i + 1] == 0xDA)
      return i;
  return 0;
}

static int
t_b5_truncated_entropy (void)
{
  SANE_Status st;
  size_t got, want;

  if (!J)
    J = make_jpeg (IMG_W, IMG_H, &JLEN);
  CHECK (!run_truncated (JLEN * 6 / 10, &st, &got, &want), "open failed");
  CHECK (st != SANE_STATUS_EOF && st != SANE_STATUS_GOOD,
         "a JPEG cut at 60%% gave %s after %lu of %lu bytes",
         sane_strstatus (st), (unsigned long) got, (unsigned long) want);
  sane_close (H);
  return 0;
}

static int
t_b5_truncated_after_header (void)
{
  SANE_Status st;
  size_t got, want;

  if (!J)
    J = make_jpeg (IMG_W, IMG_H, &JLEN);
  CHECK (!run_truncated (sos_offset () + 20, &st, &got, &want),
         "open failed");
  CHECK (st != SANE_STATUS_EOF && st != SANE_STATUS_GOOD,
         "a JPEG cut after the SOS header gave %s after %lu of %lu bytes",
         sane_strstatus (st), (unsigned long) got, (unsigned long) want);
  sane_close (H);
  return 0;
}

static int
t_b5_truncated_offsets (void)
{
  int k;
  size_t sos;

  if (!J)
    J = make_jpeg (IMG_W, IMG_H, &JLEN);
  sos = sos_offset ();
  for (k = 1; k <= 4; k++)
    {
      pid_t pid = fork ();
      int ws;
      if (pid == 0)
        {
          SANE_Status st;
          size_t got, want, cut = sos + (JLEN - sos) * (size_t) k / 5;
          fk_reset ();
          if (run_truncated (cut, &st, &got, &want))
            _exit (2);
          _exit (st == SANE_STATUS_EOF || st == SANE_STATUS_GOOD ? 1 : 0);
        }
      waitpid (pid, &ws, 0);
      CHECK (WIFEXITED (ws) && WEXITSTATUS (ws) == 0,
             "cut at %d/5 of the entropy data was not an error", k);
    }
  return 0;
}

/* B6: sane_read with max_len -1 in the JPEG frame mode. */
static int
t_b6_jpeg_negative_len (void)
{
  unsigned char *buf = malloc (1);
  SANE_Int len = 0;
  SANE_Status st;

  load_sheets (1, IMG_H);
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  set_str (OPT_COMPRESS, "JPEG");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "start");
  st = sane_read (H, buf, -1, &len);
  CHECK (st == SANE_STATUS_INVAL && len == 0,
         "max_len -1 gave %s, len %d", sane_strstatus (st), len);
  free (buf);
  sane_close (H);
  return 0;
}

/* B6: sane_read with max_len 0 must not end the frame. */
static int
t_b6_decoded_zero_len (void)
{
  unsigned char b[4];
  SANE_Int len = 0;
  SANE_Status st;
  size_t n;

  load_sheets (1, IMG_H);
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "start");
  st = sane_read (H, b, 0, &len);
  CHECK (st == SANE_STATUS_GOOD && len == 0, "max_len 0 gave %s",
         sane_strstatus (st));
  st = read_frame (NULL, &n, 65536);
  CHECK (st == SANE_STATUS_EOF && n == (size_t) IMG_W * 3 * IMG_H,
         "after max_len 0: %s, %lu bytes", sane_strstatus (st),
         (unsigned long) n);
  sane_close (H);
  return 0;
}

/* B3: sane_cancel (as from a signal handler) in a libjpeg call. */
static int b3_fired;
static void
hook_cancel_libjpeg (const char *msg)
{
  if (!b3_fired && !strncmp (msg, "libjpeg:", 8))
    {
      b3_fired = 1;
      sane_cancel (H);
    }
}

static int
t_b3_cancel_in_decode (void)
{
  SANE_Status st;
  size_t got;

  if (!J)
    J = make_jpeg (IMG_W, IMG_H, &JLEN);
  load_sheets (1, IMG_H);
  fk.sheet[0][0].len = fk.sheet[0][1].len = JLEN / 2;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "start");
  h_dbg_hook = hook_cancel_libjpeg;
  st = read_frame (NULL, &got, 4096);
  h_dbg_hook = NULL;
  CHECK (b3_fired, "the libjpeg warning did not come");
  CHECK (st == SANE_STATUS_CANCELLED || st == SANE_STATUS_IO_ERROR,
         "read after cancel gave %s", sane_strstatus (st));
  sane_cancel (H);
  CHECK (fk.job == 0, "the job is still active in the scanner");
  sane_close (H);
  return 0;
}

/* B3: sane_cancel after the end-of-side REQUEST SENSE, before the
   backend copies the data of the READ reply. */
static void
hook_cancel_sense (const char *msg)
{
  if (!b3_fired && strstr (msg, "000: 70 00 40"))
    {
      b3_fired = 1;
      sane_cancel (H);
    }
}

static int
t_b3_cancel_after_sense (void)
{
  SANE_Status st;

  load_sheets (1, IMG_H);
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  h_dbg_hook = hook_cancel_sense;
  st = sane_start (H);
  h_dbg_hook = NULL;
  CHECK (b3_fired, "the end-of-side sense did not come");
  CHECK (st == SANE_STATUS_CANCELLED, "start after cancel gave %s",
         sane_strstatus (st));
  CHECK (fk.job == 0, "the job is still active in the scanner");
  sane_close (H);
  return 0;
}

/* B7: E0 is accepted, then the reply is lost. The backend must not send
   E0 again. */
static int
hook_b7a (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  if (cdb[0] == 0xE0 && fk.cnt[0xE0] == 2)
    {
      fk_model_cmd (cdb, cl, in, r);
      r->close_now = 1;
      return 1;
    }
  return 0;
}

static int
t_b7_no_replay_e0 (void)
{
  SANE_Status st;

  load_sheets (3, IMG_H);
  fk.hook = hook_b7a;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet 1");
  read_frame (NULL, NULL, 65536);
  fk_advance_ms (10000);
  st = sane_start (H);
  CHECK (fk.cnt[0xE0] == 2, "E0 was sent %d times (2 expected), start %s",
         fk.cnt[0xE0], sane_strstatus (st));
  CHECK (st != SANE_STATUS_GOOD, "start after a lost E0 reply gave GOOD");
  sane_close (H);
  return 0;
}

/* B7 (regression): the scanner closed an idle Port1 connection. The
   backend connects again and sends E0 one time. */
static int
t_b7_idle_reconnect (void)
{
  SANE_Status st;

  load_sheets (2, IMG_H);
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet 1");
  read_frame (NULL, NULL, 65536);
  fk_advance_ms (10000);
  fk_drop_port1 ();
  usleep (20000);
  st = sane_start (H);
  CHECK (st == SANE_STATUS_GOOD, "sheet 2 after reconnect: %s",
         sane_strstatus (st));
  CHECK (fk.cnt[0xE0] == 2, "E0 sent %d times", fk.cnt[0xE0]);
  sane_close (H);
  return 0;
}

/* B7: the reply of D5 is lost. The scanner has a job, so the backend
   must end it (D6). */
static int
hook_b7c (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  if (cdb[0] == 0xD5)
    {
      fk_model_cmd (cdb, cl, in, r);
      r->close_now = 1;
      return 1;
    }
  return 0;
}

static int
t_b7_d5_reply_lost (void)
{
  SANE_Status st;

  load_sheets (1, IMG_H);
  fk.hook = hook_b7c;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  st = sane_start (H);
  CHECK (st != SANE_STATUS_GOOD, "start gave GOOD");
  fk.hook = NULL;
  sane_cancel (H);
  CHECK (fk.job == 0, "the scanner job is still active (D6 count %d)",
         fk.cnt[0xD6]);
  sane_close (H);
  return 0;
}

/* B7: D6 fails at the end of a job. The next job must end the old job
   first, not send D5 into an active job. */
static int b7d_once;
static int
hook_b7d (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  (void) cl;
  (void) in;
  if (cdb[0] == 0xD6 && !b7d_once)
    {
      b7d_once = 1;
      r->close_now = 1;
      return 1;
    }
  return 0;
}

static int
t_b7_d6_failure (void)
{
  SANE_Status st;

  load_sheets (1, IMG_H);
  fk.hook = hook_b7d;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet 1");
  read_frame (NULL, NULL, 65536);
  st = sane_start (H);
  CHECK (st != SANE_STATUS_GOOD, "end of stack gave GOOD");
  CHECK (fk.job == 1, "test error: D6 did not fail");
  fk.fed = 0;                   /* load the paper again */
  st = sane_start (H);
  CHECK (fk.cnt[0xD6] >= 2, "no D6 after the failed D6 (start %s)",
         sane_strstatus (st));
  CHECK (fk.d5_in_job == 0, "D5 was sent into an active job (start %s)",
         sane_strstatus (st));
  CHECK (st == SANE_STATUS_GOOD, "the next job: %s", sane_strstatus (st));
  sane_close (H);
  return 0;
}

/* B8: the scanner lost the reserve (no keepalive reply). sane_start must
   reserve again, not fail on Port1. */
static int
t_b8_reserve_lost (void)
{
  SANE_Status st;

  load_sheets (1, IMG_H);
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  fk.reserved = 0;
  fk_drop_port1 ();
  send_keepalive ((struct ix1300 *) H);
  st = sane_start (H);
  CHECK (st == SANE_STATUS_GOOD, "start after a lost reserve: %s "
         "(reserve count %d)", sane_strstatus (st), fk.n_reserve);
  CHECK (fk.n_reserve == 2, "reserve count %d", fk.n_reserve);
  sane_close (H);
  return 0;
}

static void
make_trigger (unsigned char *p, int code, int seq, int is_long)
{
  memset (p, 0, 48);
  p[3] = 48;
  p[4] = 0x56;
  p[5] = 0x45;
  p[6] = 0x4E;
  p[7] = 0x53;
  p[11] = (unsigned char) code;
  p[14] = is_long ? 0x20 : 0;
  p[16] = (unsigned char) seq;
}

/* B8: a power-on trigger shows that the reserve is lost. */
static int
t_b8_power_trigger (void)
{
  unsigned char p[48];
  struct ix1300 *s;
  SANE_Status st;

  load_sheets (1, IMG_H);
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  s = H;
  fk.reserved = 0;
  fk_drop_port1 ();
  make_trigger (p, 0x30, 1, 0);
  fk_udp_inject (s->trig_fd, p, 48, FK_SCANNER_IP, 52218);
  get_int (OPT_SCAN_SW);
  st = sane_start (H);
  CHECK (st == SANE_STATUS_GOOD, "start after power on: %s",
         sane_strstatus (st));
  sane_close (H);
  return 0;
}

/* B8: sane_open must fail if the keepalive thread does not start. */
static int
t_b8_thread_fail (void)
{
  SANE_Status st;

  fk.thread_fail = 1;
  st = open_dev ();
  CHECK (st != SANE_STATUS_GOOD, "open without keepalive thread gave GOOD");
  CHECK (fk.reserved == 0, "the reserve was not released");
  return 0;
}

/* B8: a keepalive interval longer than the reserve lifetime. */
static int
t_b8_keepalive_cadence (void)
{
  h_config = "net 10.0.0.5\nhostid 0123456789ABCDEF\niface fake0\n"
    "discovery-timeout 1000\nkeepalive 60\nreserve-timeout 30\n";
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  CHECK (cfg.keepalive + 3 < cfg.reserve_timeout + 10,
         "keepalive %d s with a reserve lifetime of %d s", cfg.keepalive,
         cfg.reserve_timeout + 10);
  sane_close (H);
  return 0;
}

/* B9: a discovery reply from a different address. */
static int
t_b9_discovery_spoof (void)
{
  SANE_Status st;

  fk.disc_mode = 1;
  st = open_dev ();
  CHECK (st != SANE_STATUS_GOOD, "a reply from 10.0.0.66 for 10.0.0.5 "
         "was accepted");
  return 0;
}

/* B9: a junk datagram before the real reply. */
static int
t_b9_discovery_junk_first (void)
{
  SANE_Status st;

  fk.disc_mode = 2;
  st = open_dev ();
  CHECK (st == SANE_STATUS_GOOD, "junk before the reply: open %s",
         sane_strstatus (st));
  sane_close (H);
  return 0;
}

static int
trigger_test (const char *src, size_t len, int seq_a, int seq_b, int seq_c,
              int *events)
{
  unsigned char p[64];
  struct ix1300 *s;
  int seqs[3];
  int i;

  seqs[0] = seq_a;
  seqs[1] = seq_b;
  seqs[2] = seq_c;
  if (open_dev () != SANE_STATUS_GOOD)
    return 1;
  s = H;
  *events = 0;
  for (i = 0; i < 3; i++)
    {
      if (seqs[i] < 0)
        continue;
      memset (p, 0, sizeof (p));
      make_trigger (p, 1, seqs[i], 0);
      fk_udp_inject (s->trig_fd, p, len, src, 52218);
      *events += get_int (OPT_SCAN_SW) == SANE_TRUE;
      fk_advance_ms (3000);
    }
  sane_close (H);
  return 0;
}

static int
t_b9_trigger_valid (void)
{
  int ev;
  CHECK (!trigger_test (FK_SCANNER_IP, 48, 5, -1, -1, &ev), "open failed");
  CHECK (ev == 1, "a valid trigger gave %d events", ev);
  return 0;
}

static int
t_b9_trigger_spoof (void)
{
  int ev;
  CHECK (!trigger_test ("10.0.0.66", 48, 5, -1, -1, &ev), "open failed");
  CHECK (ev == 0, "a trigger from 10.0.0.66 gave %d events", ev);
  return 0;
}

static int
t_b9_trigger_size (void)
{
  int ev;
  CHECK (!trigger_test (FK_SCANNER_IP, 64, 5, -1, -1, &ev), "open failed");
  CHECK (ev == 0, "a 64-byte trigger gave %d events", ev);
  return 0;
}

static int
t_b9_trigger_replay (void)
{
  int ev;
  CHECK (!trigger_test (FK_SCANNER_IP, 48, 5, 6, 5, &ev), "open failed");
  CHECK (ev == 2, "sequence 5, 6, 5 gave %d events (2 expected)", ev);
  return 0;
}

static int
t_b9_keepalive_bad_key (void)
{
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  fk.ka_mode = 2;
  h_watch = "send_keepalive: reply OK";
  h_watch_seen = 0;
  send_keepalive ((struct ix1300 *) H);
  CHECK (!h_watch_seen, "a keepalive reply without the VENS key was OK");
  fk.ka_mode = 3;
  send_keepalive ((struct ix1300 *) H);
  CHECK (!h_watch_seen, "a keepalive reply from 10.0.0.66 was OK");
  fk.ka_mode = 0;
  send_keepalive ((struct ix1300 *) H);
  CHECK (h_watch_seen, "a correct keepalive reply was not OK");
  sane_close (H);
  return 0;
}

/* B10: options do not change while a duplex sheet is pending. */
static int
t_b10_option_lock (void)
{
  SANE_Status st;

  load_sheets (1, IMG_H);
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  set_str (OPT_SOURCE, "ADF Duplex");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "front");
  read_frame (NULL, NULL, 65536);
  st = set_int (OPT_RES, 600);
  CHECK (st == SANE_STATUS_DEVICE_BUSY,
         "resolution change with a pending back side gave %s",
         sane_strstatus (st));
  sane_cancel (H);
  st = set_int (OPT_RES, 600);
  CHECK (st == SANE_STATUS_GOOD, "after sane_cancel: %s",
         sane_strstatus (st));
  sane_close (H);
  return 0;
}


/* ---------------------------------------------------------------- */
/* Cancel with sheets left in the job (PROTOCOL.md 8.6)            */

/* Index of the option NAME, or -1. */
static int
find_opt (const char *name)
{
  int i;
  for (i = 0; i < NUM_OPTIONS; i++)
    {
      const SANE_Option_Descriptor *d = sane_get_option_descriptor (H, i);
      if (d && d->name && !strcmp (d->name, name))
        return i;
    }
  return -1;
}

/* Position of the last OP in the scanner log, or -1. */
static int
log_last (unsigned char op)
{
  int i;
  for (i = fk.nlog - 1; i >= 0; i--)
    if (fk.log[i] == op)
      return i;
  return -1;
}

/* The single-sheet option is gone. halt-on-cancel is an advanced bool
   option (the name, title and text of the fujitsu backend), false by
   default. */
static int
t_c1_options (void)
{
  const SANE_Option_Descriptor *d;
  int i;

  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  CHECK (find_opt ("single-sheet") < 0, "the single-sheet option exists");
  i = find_opt ("halt-on-cancel");
  CHECK (i >= 0, "no halt-on-cancel option");
  d = sane_get_option_descriptor (H, i);
  CHECK (d->type == SANE_TYPE_BOOL, "halt-on-cancel is not a bool");
  CHECK (d->cap & SANE_CAP_ADVANCED, "halt-on-cancel is not advanced");
  CHECK (SANE_OPTION_IS_SETTABLE (d->cap), "halt-on-cancel is not settable");
  CHECK (!strcmp (d->title, "Halt on Cancel"), "title \"%s\"", d->title);
  CHECK (!strcmp (d->desc, "Request driver to halt the paper feed instead "
                  "of eject during a cancel."), "text \"%s\"", d->desc);
  CHECK (get_int (i) == SANE_FALSE, "halt-on-cancel is not false");
  CHECK (set_int (i, SANE_TRUE) == SANE_STATUS_GOOD, "set failed");
  CHECK (get_int (i) == SANE_TRUE, "halt-on-cancel did not change");
  sane_close (H);
  return 0;
}

/* Default cancel after sheet 1 of 5: D8, E0, wait for an empty paper
   path, then D6. The scanner feeds the 4 other sheets out without a
   scan. No READ after D8. */
static int
t_c2_cancel_flush (void)
{
  int d8, e0, d6, i;

  load_sheets (5, IMG_H);
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet 1");
  CHECK (read_frame (NULL, NULL, 65536) == SANE_STATUS_EOF, "read");
  sane_cancel (H);
  d8 = log_last (0xD8);
  e0 = log_last (0xE0);
  d6 = log_last (0xD6);
  CHECK (fk.job == 0, "the job is still active in the scanner");
  CHECK (d8 > 0 && e0 > d8 && d6 > e0, "order D8 %d, E0 %d, D6 %d", d8, e0,
         d6);
  for (i = d8; i < fk.nlog; i++)
    CHECK (fk.log[i] != 0x28, "READ after D8");
  CHECK (fk.ejected == 4, "%d sheets ejected (4 expected)", fk.ejected);
  CHECK (fk.stuck == 0, "%d sheets stay in the scanner", fk.stuck);
  CHECK (fk_now_ms () >= fk.eject_until, "D6 before the end of the eject");
  sane_close (H);
  return 0;
}

/* halt-on-cancel: D8, then D6 only. The paper stays in the path. */
static int
t_c3_cancel_halt (void)
{
  int d8, i;

  load_sheets (5, IMG_H);
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  i = find_opt ("halt-on-cancel");
  CHECK (i >= 0, "no halt-on-cancel option");
  CHECK (set_int (i, SANE_TRUE) == SANE_STATUS_GOOD, "set failed");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet 1");
  CHECK (read_frame (NULL, NULL, 65536) == SANE_STATUS_EOF, "read");
  sane_cancel (H);
  d8 = log_last (0xD8);
  CHECK (fk.job == 0, "the job is still active in the scanner");
  CHECK (d8 > 0 && log_last (0xD6) > d8, "no D8 before D6");
  CHECK (log_last (0xE0) < d8, "E0 after D8");
  CHECK (fk.ejected == 0, "%d sheets ejected", fk.ejected);
  CHECK (fk.stuck == 4, "%d sheets stay in the scanner (4 expected)",
         fk.stuck);
  sane_close (H);
  return 0;
}

/* E0 after D8 gives CHECK 5/24/00. The backend ignores the sense, waits
   for the empty path, and sends D6. */
static int
t_c4_flush_e0_sense (void)
{
  load_sheets (4, IMG_H);
  fk.e0_cancel_check = 1;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  set_str (OPT_SOURCE, "ADF Duplex");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet 1 front");
  CHECK (read_frame (NULL, NULL, 65536) == SANE_STATUS_EOF, "read");
  sane_cancel (H);
  CHECK (fk.job == 0, "the job is still active in the scanner");
  CHECK (fk.ejected == 3, "%d sheets ejected (3 expected)", fk.ejected);
  CHECK (fk_now_ms () >= fk.eject_until, "D6 before the end of the eject");
  /* The next job starts normally. */
  load_sheets (1, IMG_H);
  fk.fed = 0;
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "next job");
  CHECK (read_frame (NULL, NULL, 65536) == SANE_STATUS_EOF, "next read");
  sane_close (H);
  return 0;
}

/* The path does not become clear: the wait has a limit, then D6. */
static int
hook_c5 (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  if (cdb[0] == 0xC2 && fk.cancelled)
    {
      unsigned char hw[0x30];
      memset (hw, 0, sizeof (hw));
      hw[2] = 0x80;
      hw[3] = 0x01;
      fk_reply_data (r, hw, in < sizeof (hw) ? in : sizeof (hw));
      return 1;
    }
  return 0;
}

static int
t_c5_flush_limit (void)
{
  long t0;

  load_sheets (3, IMG_H);
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet 1");
  read_frame (NULL, NULL, 65536);
  fk.hook = hook_c5;
  t0 = fk_now_ms ();
  sane_cancel (H);
  CHECK (fk.job == 0, "the job is still active in the scanner");
  CHECK (fk_now_ms () - t0 >= 100000L && fk_now_ms () - t0 < 200000L,
         "the wait took %ld ms", fk_now_ms () - t0);
  sane_close (H);
  return 0;
}

/* The normal end of the stack (E0 -> 3/80/03) sends D6 only: no D8 after
   the job start, nothing ejected. */
static int
t_c6_normal_end (void)
{
  int f;

  load_sheets (2, IMG_H);
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  for (f = 0; f < 2; f++)
    {
      CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet %d", f + 1);
      read_frame (NULL, NULL, 65536);
    }
  CHECK (sane_start (H) == SANE_STATUS_NO_DOCS, "end of stack");
  CHECK (fk.cnt[0xD8] == 1, "%d D8 commands (1 expected)", fk.cnt[0xD8]);
  CHECK (fk.cnt[0xD6] == 1, "%d D6 commands", fk.cnt[0xD6]);
  CHECK (fk.ejected == 0 && fk.stuck == 0, "ejected %d, stuck %d",
         fk.ejected, fk.stuck);
  sane_cancel (H);
  CHECK (fk.cnt[0xD8] == 1, "sane_cancel after the end sent D8");
  sane_close (H);
  return 0;
}

/* sane_cancel in a READ (from a signal handler): the job ends with the
   flush. */
static int c7_fired;
static void
hook_c7 (const char *msg)
{
  if (!c7_fired && strstr (msg, "000: 70 00 40"))
    {
      c7_fired = 1;
      sane_cancel (H);
    }
}

static int
t_c7_cancel_in_read (void)
{
  SANE_Status st;

  load_sheets (3, IMG_H);
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  h_dbg_hook = hook_c7;
  st = sane_start (H);
  h_dbg_hook = NULL;
  CHECK (c7_fired, "the end-of-side sense did not come");
  CHECK (st == SANE_STATUS_CANCELLED, "start gave %s", sane_strstatus (st));
  CHECK (fk.job == 0, "the job is still active in the scanner");
  CHECK (fk.ejected == 2, "%d sheets ejected (2 expected)", fk.ejected);
  sane_close (H);
  return 0;
}

/* sane_close with sheets left: the same flush as sane_cancel. */
static int
t_c8_close_flush (void)
{
  load_sheets (3, IMG_H);
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet 1");
  read_frame (NULL, NULL, 65536);
  sane_close (H);
  CHECK (fk.job == 0, "the job is still active in the scanner");
  CHECK (fk.ejected == 2, "%d sheets ejected (2 expected)", fk.ejected);
  return 0;
}

/* A failed D6 leaves the job state uncertain. The next sane_start ends
   the old job with D8 and D6 only: E0 there would feed out the paper of
   the new job. */
static int c9_once;
static int
hook_c9 (const unsigned char *cdb, size_t cl, size_t in, struct fk_reply *r)
{
  (void) cl;
  (void) in;
  if (cdb[0] == 0xD6 && !c9_once)
    {
      c9_once = 1;
      r->close_now = 1;
      return 1;
    }
  return 0;
}

static int
t_c9_uncertain_no_flush (void)
{
  int e0;

  load_sheets (1, IMG_H);
  fk.hook = hook_c9;
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "sheet 1");
  read_frame (NULL, NULL, 65536);
  CHECK (sane_start (H) != SANE_STATUS_GOOD, "end of stack gave GOOD");
  CHECK (fk.job == 1, "test error: D6 did not fail");
  load_sheets (2, IMG_H);
  fk.fed = 0;
  e0 = fk.cnt[0xE0];
  CHECK (sane_start (H) == SANE_STATUS_GOOD, "next job");
  CHECK (fk.cnt[0xE0] == e0 + 1, "%d E0 commands (1 expected)",
         fk.cnt[0xE0] - e0);
  CHECK (fk.ejected == 0, "%d sheets ejected", fk.ejected);
  sane_close (H);
  return 0;
}


/* Real scanner JPEGs (IXTEST_JPEGS: a list of files). The backend writes
   a smaller height into the SOF marker (as for --ald). The frame must
   decode without a libjpeg warning and agree with a reference decode. */
static int
t_real_jpegs (void)
{
  const char *list = getenv ("IXTEST_JPEGS");
  char *copy, *tok, *save = NULL;
  int files = 0;

  if (!list || !*list)
    {
      printf ("  (real_jpegs: IXTEST_JPEGS is not set, nothing to do)\n");
      return 0;
    }
  CHECK (open_dev () == SANE_STATUS_GOOD, "open failed");
  h_watch = "libjpeg:";
  h_watch_seen = 0;
  copy = strdup (list);
  for (tok = strtok_r (copy, " ", &save); tok;
       tok = strtok_r (NULL, " ", &save))
    {
      FILE *f = fopen (tok, "rb");
      unsigned char *jp, *img, *ref;
      long fl;
      size_t sof, n, rn, bpl;
      int h, lines;
      SANE_Status st;

      CHECK (f != NULL, "cannot open %s", tok);
      fseek (f, 0, SEEK_END);
      fl = ftell (f);
      fseek (f, 0, SEEK_SET);
      jp = malloc ((size_t) fl);
      CHECK (fread (jp, 1, (size_t) fl, f) == (size_t) fl, "read %s", tok);
      fclose (f);
      sof = jpeg_find_sof (jp, (size_t) fl);
      CHECK (sof, "%s: no SOF", tok);
      h = (int) get_be16 (jp + sof + 5);
      bpl = (size_t) get_be16 (jp + sof + 7) * 3;
      for (lines = h - 100; lines <= h; lines += 100)
        {
          load_sheets (1, lines);
          fk.sheet[0][0].jpeg = fk.sheet[0][1].jpeg = jp;
          fk.sheet[0][0].len = fk.sheet[0][1].len = (size_t) fl;
          fk.chunk = 0x300000 - 2;
          fk.fed = 0;
          st = sane_start (H);
          CHECK (st == SANE_STATUS_GOOD, "%s: start %s", tok, sane_strstatus (st));
          st = read_frame (&img, &n, 1 << 20);
          CHECK (st == SANE_STATUS_EOF, "%s: read %s", tok, sane_strstatus (st));
          ref = ref_decode (jp, (size_t) fl, lines, &rn);
          CHECK (n == rn, "%s: %lu bytes, %lu expected", tok, (unsigned long) n,
                 (unsigned long) rn);
          CHECK (!memcmp (img, ref, rn - 2 * bpl), "%s: other pixels", tok);
          st = sane_start (H);
          CHECK (st == SANE_STATUS_NO_DOCS, "%s: end %s", tok,
                 sane_strstatus (st));
          CHECK (!h_watch_seen, "%s: libjpeg gave a warning", tok);
          printf ("  real_jpegs: %s: %dx%d -> %d lines OK\n", tok,
                  (int) (bpl / 3), h, lines);
          free (img);
          free (ref);
        }
      free (jp);
      files++;
    }
  free (copy);
  CHECK (files > 0, "no file");
  sane_close (H);
  return 0;
}

/* ---------------------------------------------------------------- */

struct test
{
  const char *name;
  int (*fn) (void);
};

static const struct test tests[] = {
  {"duplex_decode", t_duplex_decode},
  {"b1_timeout_prefetch", t_b1_timeout_prefetch},
  {"b1_back_paper_lost", t_b1_back_paper_lost},
  {"b1_front_lost_after_data", t_b1_front_lost_after_data},
  {"b1_end_on_first_read", t_b1_end_on_first_read},
  {"b2_key0_no_eom", t_b2_key0_no_eom},
  {"b3_cancel_in_decode", t_b3_cancel_in_decode},
  {"b3_cancel_after_sense", t_b3_cancel_after_sense},
  {"b4_short_header", t_b4_short_header},
  {"b4_oversize_reply", t_b4_oversize_reply},
  {"b4_progress_refresh", t_b4_progress_refresh},
  {"b4_no_progress_good", t_b4_no_progress_good},
  {"b4_trickle", t_b4_trickle},
  {"b4_append_wrap", t_b4_append_wrap},
  {"b4_side_limit", t_b4_side_limit},
  {"b5_truncated_entropy", t_b5_truncated_entropy},
  {"b5_truncated_after_header", t_b5_truncated_after_header},
  {"b5_truncated_offsets", t_b5_truncated_offsets},
  {"b6_jpeg_negative_len", t_b6_jpeg_negative_len},
  {"b6_decoded_zero_len", t_b6_decoded_zero_len},
  {"b7_no_replay_e0", t_b7_no_replay_e0},
  {"b7_idle_reconnect", t_b7_idle_reconnect},
  {"b7_d5_reply_lost", t_b7_d5_reply_lost},
  {"b7_d6_failure", t_b7_d6_failure},
  {"b8_reserve_lost", t_b8_reserve_lost},
  {"b8_power_trigger", t_b8_power_trigger},
  {"b8_thread_fail", t_b8_thread_fail},
  {"b8_keepalive_cadence", t_b8_keepalive_cadence},
  {"b9_discovery_spoof", t_b9_discovery_spoof},
  {"b9_discovery_junk_first", t_b9_discovery_junk_first},
  {"b9_trigger_valid", t_b9_trigger_valid},
  {"b9_trigger_spoof", t_b9_trigger_spoof},
  {"b9_trigger_size", t_b9_trigger_size},
  {"b9_trigger_replay", t_b9_trigger_replay},
  {"b9_keepalive_bad_key", t_b9_keepalive_bad_key},
  {"b10_option_lock", t_b10_option_lock},
  {"c1_options", t_c1_options},
  {"c2_cancel_flush", t_c2_cancel_flush},
  {"c3_cancel_halt", t_c3_cancel_halt},
  {"c4_flush_e0_sense", t_c4_flush_e0_sense},
  {"c5_flush_limit", t_c5_flush_limit},
  {"c6_normal_end", t_c6_normal_end},
  {"c7_cancel_in_read", t_c7_cancel_in_read},
  {"c8_close_flush", t_c8_close_flush},
  {"c9_uncertain_no_flush", t_c9_uncertain_no_flush},
  {"real_jpegs", t_real_jpegs},
};

#define NTESTS ((int) (sizeof (tests) / sizeof (tests[0])))

static int
run_one (const struct test *t)
{
  int pipefd[2];
  pid_t pid;
  int ws;
  char msg[512] = "";
  ssize_t n;

  if (pipe (pipefd) < 0)
    return 1;
  pid = fork ();
  if (pid == 0)
    {
      int r;
      close (pipefd[0]);
      alarm (90);
      fk_reset ();
      r = t->fn ();
      if (r)
        {
          if (write (pipefd[1], fail_msg, strlen (fail_msg)) < 0)
            r = 1;
          if (getenv ("IXTEST_LOG"))
            fk_log_print ();
        }
      _exit (r ? 1 : 0);
    }
  close (pipefd[1]);
  n = read (pipefd[0], msg, sizeof (msg) - 1);
  if (n > 0)
    msg[n] = '\0';
  close (pipefd[0]);
  waitpid (pid, &ws, 0);
  if (WIFEXITED (ws) && WEXITSTATUS (ws) == 0)
    {
      printf ("PASS %s\n", t->name);
      return 0;
    }
  if (WIFSIGNALED (ws))
    printf ("FAIL %s: crash, signal %d\n", t->name, WTERMSIG (ws));
  else if (!msg[0])
    printf ("FAIL %s: exit %d (AddressSanitizer or abort)\n", t->name,
            WEXITSTATUS (ws));
  else
    printf ("FAIL %s: %s\n", t->name, msg);
  return 1;
}

int
main (int argc, char **argv)
{
  int i, j, fails = 0, runs = 0;
  const char *dl = getenv ("IXTEST_DEBUG");

  signal (SIGPIPE, SIG_IGN);
  setvbuf (stdout, NULL, _IOLBF, 0);
  if (dl)
    h_dbg_print = atoi (dl);

  if (argc > 1 && !strcmp (argv[1], "-l"))
    {
      for (i = 0; i < NTESTS; i++)
        printf ("%s\n", tests[i].name);
      return 0;
    }
  for (i = 0; i < NTESTS; i++)
    {
      int sel = argc < 2;
      for (j = 1; j < argc; j++)
        if (!strcmp (argv[j], tests[i].name))
          sel = 1;
      if (!sel)
        continue;
      runs++;
      fails += run_one (&tests[i]);
    }
  printf ("%d tests, %d failed (%s)\n", runs, fails, IX_SRC);
  return fails ? 1 : 0;
}
