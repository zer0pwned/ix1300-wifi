#ifndef IX1300_H
#define IX1300_H

/*
 * This file is part of the SANE package.
 * It holds the constants and the data structures of the ix1300
 * backend. The backend controls a PFU (Fujitsu) ScanSnap iX1300
 * through the Wi-Fi network (TCP and UDP).
 *
 * See ix1300.c for the license.
 */

#include <pthread.h>
#include <signal.h>
#include <netinet/in.h>

/* ------------------------------------------------------------------ */
/* Option numbers                                                     */
/* ------------------------------------------------------------------ */

enum ix1300_Option
{
  OPT_NUM_OPTS = 0,

  OPT_STANDARD_GROUP,
  OPT_SOURCE,
  OPT_MODE,
  OPT_RES,

  OPT_GEOMETRY_GROUP,
  OPT_TL_X,
  OPT_TL_Y,
  OPT_BR_X,
  OPT_BR_Y,
  OPT_PAGE_WIDTH,
  OPT_PAGE_HEIGHT,

  OPT_ENHANCEMENT_GROUP,
  OPT_THRESHOLD,

  OPT_ADVANCED_GROUP,
  OPT_ALD,
  OPT_COMPRESS,
  OPT_HALT_ON_CANCEL,

  OPT_SENSOR_GROUP,
  OPT_SCAN_SW,
  OPT_LONG_SW,
  OPT_PAGE_LOADED,

  /* must be last */
  NUM_OPTIONS
};

/* ------------------------------------------------------------------ */
/* Protocol constants (see PROTOCOL.md)                               */
/* ------------------------------------------------------------------ */

/* The first 4 bytes of each message ("VENS"). */
#define IX_KEY            0x56454E53UL

/* The interface version that the scanner accepts. */
#define IX_IFVER          0x0010

/* Default ports. The discovery reply gives the real ports. */
#define IX_SEARCH_PORT    52217
#define IX_PORT1          53218
#define IX_PORT2          53219

/* Message sizes. */
#define IX_GREET_LEN      16
#define IX_SEARCH_LEN     32
#define IX_SEARCH_REPLY   144
#define IX_RESERVE_LEN    128
#define IX_RESERVE_REPLY  20
#define IX_RELEASE_LEN    32
#define IX_RELEASE_REPLY  16
#define IX_P1_HDR         64
#define IX_P1_REPLY_HDR   40
#define IX_TRIGGER_LEN    48

/* Port2 command codes. */
#define IX_CMD_RESERVE    0x11
#define IX_CMD_RELEASE    0x12

/* Port1 command code for one CDB. */
#define IX_P1_CDB         1

/* Reserve results (signed). */
#define IX_RES_OK           0
#define IX_RES_OCCUPIED    -4
#define IX_RES_IFVER       -5
#define IX_RES_UNREG       -6
#define IX_RES_BADID       -7
#define IX_RES_SEQUENCE    -8
#define IX_RES_USBMODE     -9

/* SCSI status values in the Port1 reply. */
#define IX_SCSI_GOOD      0
#define IX_SCSI_CHECK     2
#define IX_SCSI_BUSY      8

/* The largest data-in length for one image READ. */
#define IX_READ_MAX       0x300000

/* A reply can have this number of bytes more than the requested data-in
   length (for example the page ID and the sequence ID of a READ). A
   longer reply is a protocol error. */
#define IX_REPLY_SLACK    64

/* The largest JPEG data of one side. */
#define IX_SIDE_MAX       (64UL << 20)

/* Window ID of each side. */
#define IX_WIN_FRONT      0x00
#define IX_WIN_BACK       0x80

/* Scan area limits, in 1/1200 inch. */
#define IX_MAX_WIDTH_U    0x2880   /* 10368 = 219.5 mm */
#define IX_MAX_LENGTH_U   0xA543   /* 42307 = 895.5 mm, automatic length */
#define IX_MIN_SIZE_U     1200     /* 1 inch */

/* Value for the E9 pre-read length (it works on the iX1300). */
#define IX_PREREAD_LENGTH 0x4350

/* GET HW STATUS (C2) bits. */
#define IX_HW_HOPPER_BYTE 3
#define IX_HW_HOPPER_BIT  0x80     /* 1 = no paper in the chute */
#define IX_HW_FEED_BYTE   2
#define IX_HW_FEED_BIT    0x80     /* 1 = paper at the top/feed sensor */
#define IX_HW_EXIT_BYTE   3
#define IX_HW_EXIT_BIT    0x01     /* 1 = paper at the exit sensor */
#define IX_HW_BTN_BYTE    4
#define IX_HW_BTN_SHORT   0x01
#define IX_HW_BTN_LONG    0x20

/* Trigger packet values (code at +8). */
#define IX_TRG_SCAN       0x01
#define IX_TRG_STOP       0x02
#define IX_TRG_CANCEL     0x03
#define IX_TRG_POWER_OFF  0x10
#define IX_TRG_FORCE_REL  0x11
#define IX_TRG_REQ_REL    0x12
#define IX_TRG_POWER_ON   0x30
#define IX_TRG_LONG_FLAG  0x20     /* byte +14 */
#define IX_TRG_HISTORY    16       /* sequence numbers kept for each code */

/* ------------------------------------------------------------------ */
/* Option values                                                      */
/* ------------------------------------------------------------------ */

#define STRING_ADFFRONT   SANE_I18N("ADF Front")
#define STRING_ADFBACK    SANE_I18N("ADF Back")
#define STRING_ADFDUPLEX  SANE_I18N("ADF Duplex")

#define STRING_NONE       SANE_I18N("None")
#define STRING_JPEG       SANE_I18N("JPEG")

enum ix1300_source
{
  SOURCE_ADF_FRONT = 0,
  SOURCE_ADF_BACK,
  SOURCE_ADF_DUPLEX
};

enum ix1300_mode
{
  MODE_COLOR = 0,
  MODE_GRAY,
  MODE_LINEART
};

enum ix1300_compress
{
  COMP_NONE = 0,
  COMP_JPEG
};

/* State of the PFU job (D5 ... D6) in the scanner. */
enum ix1300_job
{
  JOB_IDLE = 0,           /* no job */
  JOB_ACTIVE,             /* D5 is confirmed, D6 is not sent */
  JOB_UNCERTAIN           /* D5 or D6 has no clear result */
};

/* How end_job ends the job (PROTOCOL.md 8.6). */
enum ix1300_end
{
  END_NORMAL = 0,         /* D6: the end of the stack (E0 -> 3/80/03) */
  END_HALT,               /* D8, D6: the paper stops in the path */
  END_CANCEL              /* D8, E0, wait, D6: the scanner feeds out the
                             rest of the stack without a scan. With
                             halt-on-cancel, the same as END_HALT. */
};

/* SANE 1.0 has no JPEG frame. The fujitsu backend uses this value. */
#ifndef SANE_FRAME_JPEG
#define SANE_FRAME_JPEG 0x0B
#endif

/* ------------------------------------------------------------------ */
/* Data structures                                                    */
/* ------------------------------------------------------------------ */

/* One side of a sheet: the complete JPEG data. */
struct ix1300_image
{
  unsigned char *data;
  size_t len;
  size_t size;
};

/* Decoder state for the current frame. The type is in ix1300.c. */
struct ix1300_decoder;

struct ix1300
{
  struct ix1300 *next;

  /* -------- device data from the discovery reply -------- */
  SANE_Device sane;
  char name[32];          /* the IP address as text */
  char model[32];
  char devname[68];
  char serial[16];
  struct in_addr addr;
  int port1;
  int port2;
  int present;            /* 1 = the last discovery found the scanner */

  /* -------- host data -------- */
  struct in_addr host_ip;
  unsigned char host_mac[6];
  unsigned char host_id[8];

  /* -------- connection state -------- */
  int is_open;
  int reserved;
  int reserve_lost;       /* set by keepalive and trigger events (atomic) */
  int p1_fd;              /* Port1 socket, -1 if not connected */
  int trig_fd;            /* UDP socket for trigger packets */
  int trig_port;
  time_t last_io;

  /* keepalive thread */
  pthread_t ka_thread;
  pthread_mutex_t ka_mutex;
  pthread_cond_t ka_cond;
  int ka_running;
  int ka_stop;

  /* -------- options -------- */
  SANE_Option_Descriptor opt[NUM_OPTIONS];
  int options_ready;

  SANE_String_Const source_list[4];
  SANE_String_Const mode_list[4];
  SANE_String_Const compress_list[3];
  SANE_Int res_list[5];
  SANE_Range x_range;
  SANE_Range y_range;
  SANE_Range pw_range;
  SANE_Range ph_range;
  SANE_Range threshold_range;

  int u_source;
  int u_mode;
  int u_res;
  SANE_Fixed u_tl_x;
  SANE_Fixed u_tl_y;
  SANE_Fixed u_br_x;
  SANE_Fixed u_br_y;
  SANE_Fixed u_page_width;
  SANE_Fixed u_page_height;
  int u_threshold;
  int u_ald;
  int u_compress;
  int u_halt;             /* halt-on-cancel */

  /* -------- sensors -------- */
  int btn_short;          /* latched short press */
  int btn_long;           /* latched long press */
  int hw_short_last;      /* last C2 value of the short press bit */
  int hw_long_last;       /* last C2 value of the long press bit */
  int trig_code[IX_TRG_HISTORY];  /* recent trigger events: code */
  int trig_seqs[IX_TRG_HISTORY];  /* and sequence number */
  int trig_n;             /* number of valid entries */
  int trig_next;          /* next entry to write */
  time_t evt_time[2];     /* time of the last short and long event */
  int hopper_empty;
  int paper_feed;         /* C2: paper at the top/feed sensor */
  int paper_exit;         /* C2: paper at the exit sensor */

  /* -------- scan state -------- */
  volatile sig_atomic_t cancel_req;
  int owner;              /* 1 = a call owns the scan data (atomic) */
  int job_state;          /* enum ix1300_job */
  int sheet;              /* sheet counter in the job */
  unsigned char *rbuf;    /* receive buffer for one READ reply */

  struct ix1300_image side[2];  /* 0 = front, 1 = back */
  int pending[2];         /* 1 = this side is ready for a frame */

  int started;            /* 1 = a frame is in progress */
  int cur_side;
  SANE_Parameters params; /* parameters of the current frame */
  size_t jpeg_pos;        /* read position for JPEG output */
  struct ix1300_decoder *dec;
};

#endif /* IX1300_H */
