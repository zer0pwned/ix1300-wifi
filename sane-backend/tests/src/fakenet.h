/* Fake network and fake scanner for the ix1300 test harness.
 *
 * The harness includes ix1300.c after this file. The macros at the end
 * send the socket, clock and thread calls of the backend to the h_*
 * functions in fakenet.c. Port1 and Port2 use AF_UNIX socket pairs, and a
 * thread plays the scanner. UDP sockets are queues in memory. The sandbox
 * of the test machine does not allow a bind to a local port, so the fake
 * does not use one.
 */
#ifndef IXTEST_FAKENET_H
#define IXTEST_FAKENET_H

#include <stddef.h>
#include <stdint.h>
#include <pthread.h>
#include <poll.h>
#include <time.h>
#include <unistd.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <netinet/in.h>

#define FK_SCANNER_IP "10.0.0.5"
#define FK_HOST_IP    "10.0.0.2"
#define FK_MAX_SHEETS 8

/* One reply of the fake scanner to one Port1 command. */
struct fk_reply
{
  int scsi;                     /* SCSI status in the reply header */
  unsigned char *data;          /* data-in (malloc), or NULL */
  size_t dlen;
  long total_override;          /* >= 0: write this value as "total" */
  size_t extra;                 /* add this number of zero bytes */
  int close_now;                /* close the connection, no reply */
  int trickle_ms;               /* real delay between reply bytes */
  int advance_ms;               /* advance the fake clock first */
};

struct fk_side
{
  const unsigned char *jpeg;
  size_t len;
  int lines;                    /* pixel size READ: real lines */
};

struct fk_model
{
  pthread_mutex_t mu;

  /* configuration */
  int nsheets;
  struct fk_side sheet[FK_MAX_SHEETS][2];
  size_t chunk;                 /* image bytes in one READ reply */
  int temp_first;               /* 3/80/13 for the first READ of a side */
  int prefetch;                 /* sheets in the paper path */
  int reserve_result;
  int ka_mode;                  /* 0 OK, 1 no reply, 2 bad key, 3 other IP */
  int disc_mode;                /* 0 normal, 1 spoof only, 2 junk first */
  int need_reserve;             /* Port1: close for commands without reserve */
  int connect_fail;
  int thread_fail;
  int e0_cancel_check;          /* E0 after D8: CHECK 5/24/00 (else GOOD) */
  long eject_ms;                /* eject time of one sheet after D8 + E0 */

  /* Hook of the test: return 1 if it filled the reply. */
  int (*hook) (const unsigned char *cdb, size_t cdb_len, size_t in_req,
               struct fk_reply * r);

  /* state */
  int reserved;
  int job;
  int fed;
  int cur;
  size_t pos[2];
  int tmp_given[2];
  unsigned char sense[18];
  int d5_in_job;                /* D5 while a job was active */
  int cancelled;                /* D8 in a job: the motor stopped [HW] */
  long eject_until;             /* fake clock: the eject after D8 + E0 ends */
  int ejected;                  /* sheets fed out without a scan */
  int stuck;                    /* sheets left in the scanner after D6 */
  int cnt[256];                 /* commands seen, by opcode */
  int n_reserve, n_release, n_ka, n_probe;
  int port1_conns;
  int p1_peer;
  unsigned char log[4096];
  int nlog;
};

extern struct fk_model fk;

void fk_reset (void);
void fk_advance_ms (long ms);
long fk_now_ms (void);
void fk_set_sense (int key, int asc, int ascq, int eom);
void fk_udp_inject (int fd, const void *p, size_t len, const char *src_ip,
                    int src_port);
size_t fk_disc_reply (unsigned char *r, const char *ip);
int fk_log_has (const unsigned char *seq, int n);
void fk_log_print (void);
void fk_reply_data (struct fk_reply *r, const void *p, size_t n);
void fk_model_cmd (const unsigned char *cdb, size_t cdb_len, size_t in_req,
                   struct fk_reply *r);
void fk_drop_port1 (void);

int h_socket (int domain, int type, int proto);
int h_connect (int fd, const struct sockaddr *sa, socklen_t sl);
int h_bind (int fd, const struct sockaddr *sa, socklen_t sl);
int h_getsockname (int fd, struct sockaddr *sa, socklen_t * sl);
ssize_t h_sendto (int fd, const void *p, size_t n, int fl,
                  const struct sockaddr *to, socklen_t tl);
ssize_t h_recv (int fd, void *p, size_t n, int fl);
ssize_t h_recvfrom (int fd, void *p, size_t n, int fl,
                    struct sockaddr *from, socklen_t * fl2);
int h_poll (struct pollfd *p, nfds_t n, int ms);
int h_close (int fd);
int h_ioctl (int fd, unsigned long req, void *arg);
time_t h_time (time_t * t);
int h_usleep (useconds_t us);
int h_clock_gettime (clockid_t c, struct timespec *ts);
int h_pthread_create (pthread_t * t, const pthread_attr_t * a,
                      void *(*f) (void *), void *arg);

#endif
