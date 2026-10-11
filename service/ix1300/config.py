"""Configuration: TOML file (or JSON file) plus environment variable overrides.

Python 3.11 and later use tomllib. Older Python versions use the small TOML
parser in this file. It reads tables, arrays of tables, dotted keys, strings,
integers, floats, booleans, arrays and inline tables.

Environment overrides: IX1300_<PATH>=<value>. Use "__" between the parts of
the path. Examples:
  IX1300_SCANNER__IP=192.0.2.212
  IX1300_OUTPUTS__PAPERLESS__TOKEN=abc
  IX1300_PROFILES__DEFAULT__DPI=600
The type of the setting decides how the value is read: numbers, true/false,
lists and tables as JSON; all other values (tokens, URLs, addresses, header
values) stay text exactly as given. IX1300_<PATH>_FILE=<file> reads the text
from a file (for example a Docker secret).
"""
import copy
import ipaddress
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List

from . import outputs as outputs_mod
from .protocol import AUTO_LENGTH, DPI_VALUES, ScanSettings, mm_to_length
from .settings import parse_power_off

try:
    import tomllib as _tomllib            # Python 3.11+
except ImportError:                       # pragma: no cover - depends on Python version
    _tomllib = None

ENV_PREFIX = "IX1300_"

DEFAULTS = {
    "scanner": {
        "ip": "",                         # empty: find the scanner by broadcast
        "broadcast": "255.255.255.255",
        "name": "",                       # with broadcast: use only this scanner name or serial
        "host_id": "",
        "host_ip": "",                    # empty: detect
        "host_mac": "",                   # empty: detect
        "search_port": 52217,
        "trigger_port": 55265,
        "discovery_timeout": 3.0,
        "keepalive_interval": 15.0,
        "keepalive_failures": 3,          # intervals without a reply before "lost"
        "keepalive_wait": 3.0,            # seconds to wait for the reply in one interval
        "keepalive_repeat": 3,            # packets in one interval, 0.5 s apart
        "power_off_hold": 20.0,           # seconds without reserve attempts after POWER OFF
        "waiting_log_interval": 600.0,    # seconds between "still waiting" log lines
        "reserve_timeout": 30,
        "renew_after_job": False,         # one reserve for many jobs works [HW]
        "command_timeout": 30.0,
        "data_timeout": 60.0,             # seconds without image data before a scan error
        "frame_timeout": 120.0,           # maximum seconds for one reply frame
        "side_timeout": 600.0,            # maximum seconds for one side
        "max_side_mb": 64,                # maximum JPEG size of one side
        "retry_min": 2.0,
        "retry_max": 10.0,                # longest wait between reserve attempts (seconds)
        "yield_on_request": True,
        "power_off_minutes": None,        # None or "": do not change; 0: off; 15..1890 in steps of 15
    },
    "service": {
        "short_press_profile": "default",
        "long_press_profile": "",         # empty: same as the short press
        "work_dir": "/data",
        "log_level": "INFO",
        "deliver_partial": True,
        "min_free_mb": 200,               # no scan if work_dir has less free space
        "max_backlog": 3,                 # no scan if this number of jobs waits for processing
        "stop_timeout": 60.0,             # seconds for a clean stop (Docker: stop_grace_period)
        "stop_scan_wait": 15.0,           # at stop: seconds that a scan can continue before D8
        "retry_interval": 60.0,           # first wait before a new attempt for unsent jobs
        "retry_max_interval": 3600.0,     # longest wait between attempts
        "retry_max_attempts": 20,         # then only "ix1300 retry" sends the job
        "keep_raw": False,                # keep a copy of the scanner JPEGs in work_dir/raw
        "keep_raw_max_jobs": 20,
        "check_paper": True,              # read the paper sensor before a job; empty chute: no job
    },
    "pipeline": {
        "blank_method": "auto",           # auto, pixels, size
        "blank_ink_ratio": 0.004,         # pixels method: dark pixels / paper pixels
        "ink_level": 110,                 # gray level below this value = print
        "paper_level": 225,               # gray level above this value = paper (backing is gray)
        "blank_bytes_per_pixel": 0.01,    # size method (no decoder): only flat pages
        "receipt_ratio": 2.0,
        "rotate": "auto",                 # auto, 0, 90, 180, 270
        "osd_min_confidence": 2.0,
        "deskew": False,
        "ocr": False,
        "ocr_lang": "eng",
        "title_format": "Scan {profile} %Y-%m-%d %H:%M:%S",
        "filename_format": "scan-%Y%m%d-%H%M%S.pdf",
        "jpeg_quality": 90,
        "workers": 1,                     # threads for page work (blank test, OSD, deskew)
        "max_memory_mb": 64,              # JPEG data of all jobs in memory; more goes into work_dir files
        "tesseract_timeout": 60,          # seconds for each page
    },
    "outputs": {},
    "profiles": {
        "default": {},
    },
}

PROFILE_DEFAULTS = {
    "dpi": 300,
    "color": "auto",
    "duplex": True,
    "paper_face": "down",               # order only: down = back side first (normal load)
    "blank_skip": True,
    "length": "auto",                   # "auto" or millimetres
    "rotate": None,                     # None: use pipeline.rotate
    "deskew": None,
    "crop": "auto",                     # auto (receipts), true, false: cut the page to the paper
    "ocr": None,
    "receipt": "auto",                  # auto, yes, no
    "double_feed": True,                # double-feed detection (top chute)
    "title": "",
    "outputs": [],                      # empty: all outputs
}


class ConfigError(ValueError):
    pass


# --- Small TOML parser ----------------------------------------------------

class _Toml:
    _bare = re.compile(r"[A-Za-z0-9_-]+")
    _num = re.compile(r"[+-]?(0x[0-9A-Fa-f_]+|0o[0-7_]+|0b[01_]+|[0-9_]+(\.[0-9_]+)?([eE][+-]?[0-9_]+)?|inf|nan)")
    _esc = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}

    def __init__(self, text):
        self.s = text
        self.i = 0
        self.line = 1

    def err(self, msg):
        raise ConfigError("TOML line %d: %s" % (self.s.count("\n", 0, self.i) + 1, msg))

    def ws(self, newlines=False):
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in " \t" or (newlines and c in "\r\n"):
                self.i += 1
            elif c == "#":
                while self.i < len(self.s) and self.s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def eol(self):
        self.ws()
        if self.i < len(self.s) and self.s[self.i] not in "\r\n":
            self.err("text after value")

    def key(self):
        parts = []
        while True:
            self.ws()
            c = self.s[self.i:self.i + 1]
            if c == '"':
                parts.append(self.basic())
            elif c == "'":
                parts.append(self.literal())
            else:
                m = self._bare.match(self.s, self.i)
                if not m:
                    self.err("key expected")
                parts.append(m.group())
                self.i = m.end()
            self.ws()
            if self.s[self.i:self.i + 1] == ".":
                self.i += 1
                continue
            return parts

    def basic(self):
        self.i += 1
        out = []
        while True:
            if self.i >= len(self.s) or self.s[self.i] == "\n":
                self.err("string not closed")
            c = self.s[self.i]
            if c == '"':
                self.i += 1
                return "".join(out)
            if c == "\\":
                e = self.s[self.i + 1:self.i + 2]
                if e in self._esc:
                    out.append(self._esc[e])
                    self.i += 2
                elif e in ("u", "U"):
                    n = 4 if e == "u" else 8
                    out.append(chr(int(self.s[self.i + 2:self.i + 2 + n], 16)))
                    self.i += 2 + n
                else:
                    self.err("bad escape")
            else:
                out.append(c)
                self.i += 1

    def literal(self):
        end = self.s.find("'", self.i + 1)
        if end < 0 or "\n" in self.s[self.i:end]:
            self.err("string not closed")
        v = self.s[self.i + 1:end]
        self.i = end + 1
        return v

    def value(self):
        self.ws()
        c = self.s[self.i:self.i + 1]
        if self.s.startswith('"""', self.i) or self.s.startswith("'''", self.i):
            self.err("multi-line strings are not supported")
        if c == '"':
            return self.basic()
        if c == "'":
            return self.literal()
        if c == "[":
            self.i += 1
            arr = []
            while True:
                self.ws(True)
                if self.s[self.i:self.i + 1] == "]":
                    self.i += 1
                    return arr
                arr.append(self.value())
                self.ws(True)
                c = self.s[self.i:self.i + 1]
                if c == ",":
                    self.i += 1
                elif c != "]":
                    self.err("',' or ']' expected")
        if c == "{":
            self.i += 1
            tbl = {}
            self.ws()
            if self.s[self.i:self.i + 1] == "}":
                self.i += 1
                return tbl
            while True:
                k = self.key()
                if self.s[self.i:self.i + 1] != "=":
                    self.err("'=' expected")
                self.i += 1
                self._set(tbl, k, self.value())
                self.ws()
                c = self.s[self.i:self.i + 1]
                self.i += 1
                if c == "}":
                    return tbl
                if c != ",":
                    self.err("',' or '}' expected")
        for word, v in (("true", True), ("false", False)):
            if self.s.startswith(word, self.i):
                self.i += len(word)
                return v
        m = self._num.match(self.s, self.i)
        if not m or m.end() == self.i:
            self.err("value expected")
        self.i = m.end()
        t = m.group().replace("_", "")
        sign = -1 if t.startswith("-") else 1
        body = t.lstrip("+-")
        if body.startswith(("0x", "0o", "0b")):
            return sign * int(body, 0)
        if body in ("inf", "nan") or any(ch in body for ch in ".eE"):
            return float(t)
        return int(t)

    def _set(self, tbl, keys, val):
        for k in keys[:-1]:
            tbl = tbl.setdefault(k, {})
            if not isinstance(tbl, dict):
                self.err("key %r is not a table" % k)
        if keys[-1] in tbl:
            self.err("duplicate key %r" % keys[-1])
        tbl[keys[-1]] = val

    def parse(self):
        root = {}
        cur = root
        while True:
            self.ws(True)
            if self.i >= len(self.s):
                return root
            if self.s.startswith("[[", self.i):
                self.i += 2
                keys = self.key()
                if not self.s.startswith("]]", self.i):
                    self.err("']]' expected")
                self.i += 2
                tbl = root
                for k in keys[:-1]:
                    tbl = tbl.setdefault(k, {})
                    if isinstance(tbl, list):
                        tbl = tbl[-1]
                arr = tbl.setdefault(keys[-1], [])
                if not isinstance(arr, list):
                    self.err("key %r is not an array of tables" % keys[-1])
                cur = {}
                arr.append(cur)
            elif self.s[self.i] == "[":
                self.i += 1
                keys = self.key()
                if self.s[self.i:self.i + 1] != "]":
                    self.err("']' expected")
                self.i += 1
                cur = root
                for k in keys:
                    cur = cur.setdefault(k, {})
                    if isinstance(cur, list):
                        cur = cur[-1]
                    if not isinstance(cur, dict):
                        self.err("key %r is not a table" % k)
            else:
                keys = self.key()
                if self.s[self.i:self.i + 1] != "=":
                    self.err("'=' expected")
                self.i += 1
                self._set(cur, keys, self.value())
            self.eol()


def toml_loads(text):
    if _tomllib is not None:
        return _tomllib.loads(text)
    return _Toml(text).parse()


def mini_toml_loads(text):
    """The small parser (also on Python 3.11+, for tests)."""
    return _Toml(text).parse()


# --- Merge and overrides --------------------------------------------------

def _merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


# Types of the keys that are not in DEFAULTS (profiles and outputs). Other keys are text.
PROFILE_TYPES = {"dpi": int, "duplex": bool, "blank_skip": bool, "deskew": bool, "ocr": bool,
                 "double_feed": bool, "outputs": list, "crop": "text", "rotate": "text", "length": "text"}
OUTPUT_TYPES = {"retries": int, "retry_delay": float, "timeout": float, "max_time": float, "verify_tls": bool,
                "tags": list, "receipt_tags": list, "correspondent": int, "receipt_correspondent": int,
                "document_type": int, "receipt_document_type": int, "headers": dict, "fields": dict}


def _env_type(path):
    if len(path) == 2 and path[0] in ("scanner", "service", "pipeline"):
        v = DEFAULTS[path[0]].get(path[1])
        return type(v) if v is not None else "text"
    if len(path) == 3 and path[0] == "profiles":
        return PROFILE_TYPES.get(path[2], "text")
    if len(path) == 3 and path[0] == "outputs":
        return OUTPUT_TYPES.get(path[2], "text")
    return "text"


def _env_value(path, text):
    """Read TEXT for the setting PATH. Only typed settings are parsed."""
    t = _env_type(path)
    name = ENV_PREFIX + "__".join(p.upper() for p in path)
    if t in (bool, "text") or t is str:
        if t is bool:
            return _bool(text, name)
        return text
    try:
        v = json.loads(text)
    except ValueError:
        raise ConfigError("%s: %s value expected, got %r" % (name, t.__name__, text))
    if t in (int, float) and (isinstance(v, bool) or not isinstance(v, (int, float))):
        raise ConfigError("%s: number expected" % name)
    if t in (list, dict) and not isinstance(v, t):
        raise ConfigError("%s: %s expected (JSON)" % (name, "list" if t is list else "table"))
    return v


def apply_env(data, env):
    for name, text in sorted(env.items()):
        if not name.startswith(ENV_PREFIX) or name == ENV_PREFIX + "CONFIG":
            continue
        path = [p.lower() for p in name[len(ENV_PREFIX):].split("__") if p]
        if not path:
            continue
        if path[-1].endswith("_file") and len(path[-1]) > 5:
            path[-1] = path[-1][:-5]
            try:
                with open(text, encoding="utf-8") as fh:
                    text = fh.read().strip("\r\n")
            except OSError as e:
                raise ConfigError("%s: cannot read %s (%s)" % (name, text, e.strerror))
        tbl = data
        for k in path[:-1]:
            nxt = tbl.get(k)
            if not isinstance(nxt, dict):
                nxt = tbl[k] = {}
            tbl = nxt
        tbl[path[-1]] = _env_value(path, text)
    return data


# --- Typed views ----------------------------------------------------------

def _bool(v, name):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("1", "true", "yes", "on", "0", "false", "no", "off"):
        return v.lower() in ("1", "true", "yes", "on")
    if isinstance(v, int):
        return bool(v)
    raise ConfigError("%s: true or false expected, got %r" % (name, v))


@dataclass
class Profile:
    name: str
    dpi: int = 300
    color: str = "auto"
    duplex: bool = True
    paper_face: str = "down"
    blank_skip: bool = True
    length: object = "auto"
    rotate: object = None
    deskew: object = None
    crop: object = "auto"
    ocr: object = None
    receipt: str = "auto"
    title: str = ""
    outputs: List[str] = field(default_factory=list)
    double_feed: bool = True

    @property
    def length_units(self):
        if self.length in ("auto", None, 0, ""):
            return AUTO_LENGTH
        return min(AUTO_LENGTH, mm_to_length(float(self.length)))

    def scan_settings(self):
        return ScanSettings(dpi=self.dpi, color=self.color, length=self.length_units,
                            double_feed=self.double_feed)


def _check_profile_types(name, raw):
    """Check the original value types of a profile before normalization (no silent changes)."""
    v = raw.get("dpi")
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                          or v != int(v)):
        raise ConfigError("profile %s: dpi: integer expected, got %r" % (name, v))
    for k in ("color", "paper_face", "receipt", "title"):
        if k in raw and not isinstance(raw[k], str):
            raise ConfigError("profile %s: %s: text expected, got %r" % (name, k, raw[k]))
    outs = raw.get("outputs")
    if outs is not None and (not isinstance(outs, list) or not all(isinstance(o, str) for o in outs)):
        raise ConfigError("profile %s: outputs: list of output names expected, got %r" % (name, outs))


def _profile(name, raw):
    p = dict(PROFILE_DEFAULTS)
    unknown = set(raw) - set(p)
    if unknown:
        raise ConfigError("profile %s: unknown keys %s" % (name, sorted(unknown)))
    _check_profile_types(name, raw)
    p.update(raw)
    try:
        prof = Profile(name=name, dpi=int(p["dpi"]), color=str(p["color"]).lower(),
                       duplex=_bool(p["duplex"], "duplex"), paper_face=str(p["paper_face"]).lower(),
                       blank_skip=_bool(p["blank_skip"], "blank_skip"), length=p["length"],
                       rotate=p["rotate"], deskew=None if p["deskew"] is None else _bool(p["deskew"], "deskew"),
                       crop="auto" if str(p["crop"]).lower() == "auto" else _bool(p["crop"], "crop"),
                       ocr=None if p["ocr"] is None else _bool(p["ocr"], "ocr"),
                       receipt=str(p["receipt"]).lower(),
                       title=str(p["title"]), outputs=list(p["outputs"] or []),
                       double_feed=_bool(p["double_feed"], "double_feed"))
    except (TypeError, ValueError) as e:
        raise ConfigError("profile %s: %s" % (name, e))
    if prof.dpi not in DPI_VALUES:
        raise ConfigError("profile %s: dpi must be one of %s" % (name, DPI_VALUES))
    if prof.color not in ("auto", "color", "gray"):
        raise ConfigError("profile %s: color must be auto, color or gray" % name)
    if prof.paper_face not in ("down", "up"):
        raise ConfigError("profile %s: paper_face must be down or up" % name)
    if prof.receipt not in ("auto", "yes", "no"):
        raise ConfigError("profile %s: receipt must be auto, yes or no" % name)
    try:
        prof.rotate = _rotation(prof.rotate, allow_none=True)
    except ValueError as e:
        raise ConfigError("profile %s: %s" % (name, e))
    if prof.length != "auto":
        try:
            if not 10 <= float(prof.length) <= 895:
                raise ValueError
        except (TypeError, ValueError):
            raise ConfigError("profile %s: length must be \"auto\" or 10..895 (mm)" % name)
    return prof


def _rotation(v, allow_none=False):
    """Normalize a rotation: "auto", 0, 90, 180 or 270 (None if ALLOW_NONE)."""
    if v is None or v == "":
        if allow_none:
            return None
        raise ValueError("rotate must be auto, 0, 90, 180 or 270")
    if isinstance(v, str) and v.strip().lower() == "auto":
        return "auto"
    try:
        r = int(str(v).strip()) if not isinstance(v, bool) else None
    except ValueError:
        r = None
    if r not in (0, 90, 180, 270):
        raise ValueError("rotate must be auto, 0, 90, 180 or 270, not %r" % (v,))
    return r


# Value rules: (low, high). None = no limit. Numbers must be finite.
RANGES = {
    "scanner": {"search_port": (1, 65535), "trigger_port": (0, 65535), "discovery_timeout": (0.1, 600),
                "keepalive_interval": (0.05, 3600), "keepalive_failures": (1, 1000), "keepalive_wait": (0.05, 600),
                "keepalive_repeat": (1, 20), "power_off_hold": (0, 86400), "waiting_log_interval": (0.1, None),
                "reserve_timeout": (1, 255), "command_timeout": (0.1, 3600), "data_timeout": (0.1, 3600),
                "frame_timeout": (0.1, 3600), "side_timeout": (1, 86400), "max_side_mb": (1, 1024),
                "retry_min": (0.01, 3600), "retry_max": (0.01, 86400)},
    "service": {"min_free_mb": (0, None), "max_backlog": (1, 1000), "stop_timeout": (1, 3600),
                "stop_scan_wait": (0, 3600), "retry_interval": (1, None), "retry_max_interval": (1, None),
                "retry_max_attempts": (0, None), "keep_raw_max_jobs": (1, 100000)},
    "pipeline": {"blank_ink_ratio": (0, 1), "ink_level": (1, 255), "paper_level": (1, 255),
                 "blank_bytes_per_pixel": (0, None), "receipt_ratio": (0.1, None), "osd_min_confidence": (0, None),
                 "jpeg_quality": (1, 100), "workers": (1, 16), "max_memory_mb": (0, None),
                 "tesseract_timeout": (1, 86400)},
}
INTEGER_KEYS = {"search_port", "trigger_port", "keepalive_failures", "keepalive_repeat", "reserve_timeout",
                "max_side_mb", "max_backlog", "retry_max_attempts", "keep_raw_max_jobs", "ink_level", "paper_level",
                "jpeg_quality", "workers"}


def _ipv4(v, name):
    if v in ("", None):
        return ""
    try:
        return str(ipaddress.IPv4Address(str(v).strip()))
    except ValueError:
        raise ConfigError("%s: IPv4 address expected, got %r" % (name, v))


def _check_section(sec, tbl):
    """Check the types and values of one section. Normalize booleans and numbers in place."""
    defaults = DEFAULTS[sec]
    if not isinstance(tbl, dict):
        raise ConfigError("[%s] must be a table" % sec)
    unknown = set(tbl) - set(defaults)
    if unknown:
        raise ConfigError("[%s]: unknown keys %s" % (sec, sorted(unknown)))
    for k, v in list(tbl.items()):
        name = "%s.%s" % (sec, k)
        d = defaults[k]
        if isinstance(d, bool):
            tbl[k] = _bool(v, name)
        elif isinstance(d, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ConfigError("%s: number expected, got %r" % (name, v))
            if k in INTEGER_KEYS and v != int(v):
                raise ConfigError("%s: integer expected, got %r" % (name, v))
            low, high = RANGES.get(sec, {}).get(k, (None, None))
            if (low is not None and v < low) or (high is not None and v > high):
                raise ConfigError("%s: value %r not in %s..%s" % (name, v, low, "" if high is None else high))
            tbl[k] = int(v) if k in INTEGER_KEYS else v
        elif isinstance(d, str) and k != "rotate":
            if not isinstance(v, str):
                raise ConfigError("%s: text expected, got %r" % (name, v))


class Config:
    def __init__(self, data):
        self.data = data
        for sec in ("scanner", "service", "pipeline"):
            _check_section(sec, data.get(sec))
        self.scanner = data["scanner"]
        self.service = data["service"]
        self.pipeline = data["pipeline"]
        self.outputs = data.get("outputs") or {}
        if not isinstance(self.outputs, dict):
            raise ConfigError("[outputs] must be a table")
        profiles = data.get("profiles") or {}
        if not isinstance(profiles, dict) or not all(isinstance(r or {}, dict) for r in profiles.values()):
            raise ConfigError("[profiles] must have tables")
        self.profiles: Dict[str, Profile] = {n: _profile(n, r or {}) for n, r in profiles.items()}
        self._check()

    def _check(self):
        sc, sv, pl = self.scanner, self.service, self.pipeline
        for k in ("ip", "host_ip", "broadcast"):
            sc[k] = _ipv4(sc.get(k), "scanner." + k)
        if sc.get("host_mac"):
            from .net import parse_mac
            try:
                parse_mac(sc["host_mac"])
            except ValueError:
                raise ConfigError("scanner.host_mac: 6 bytes expected, for example 02:00:00:a1:b2:c3")
        try:
            sc["power_off_minutes"] = parse_power_off(sc.get("power_off_minutes"))
        except ValueError as e:
            raise ConfigError(str(e))
        if sc["retry_max"] < sc["retry_min"]:
            raise ConfigError("scanner.retry_max must be retry_min or more")
        if sc["keepalive_interval"] >= sc["reserve_timeout"]:
            raise ConfigError("scanner.keepalive_interval must be less than scanner.reserve_timeout "
                              "(the scanner ends the reserve after reserve_timeout + 10 s without keepalive)")
        probe = min(sc["keepalive_wait"], sc["keepalive_interval"] * 0.8)    # see daemon.Service.keepalive_once
        if sc["keepalive_interval"] + probe >= sc["reserve_timeout"] + 10:
            raise ConfigError("scanner.keepalive_interval + keepalive_wait must be less than "
                              "scanner.reserve_timeout + 10 s (the time between two keepalive replies)")
        if sv["stop_scan_wait"] >= sv["stop_timeout"]:
            raise ConfigError("service.stop_scan_wait must be less than service.stop_timeout")
        if not isinstance(logging.getLevelName(str(sv["log_level"]).upper()), int):
            raise ConfigError("service.log_level: DEBUG, INFO, WARNING or ERROR expected")
        if sv["retry_max_interval"] < sv["retry_interval"]:
            raise ConfigError("service.retry_max_interval must be retry_interval or more")
        if str(pl["blank_method"]).lower() not in ("auto", "pixels", "size"):
            raise ConfigError("pipeline.blank_method must be auto, pixels or size")
        try:
            pl["rotate"] = _rotation(pl["rotate"])
        except ValueError as e:
            raise ConfigError("pipeline.%s" % e)
        if pl["ink_level"] >= pl["paper_level"]:
            raise ConfigError("pipeline.ink_level must be less than pipeline.paper_level")
        for k in ("title_format", "filename_format"):
            if "\0" in pl[k]:
                raise ConfigError("pipeline.%s: NUL character" % k)
        hid = str(self.scanner.get("host_id") or "").replace(" ", "")
        if hid:
            try:
                if len(bytes.fromhex(hid)) != 8:
                    raise ValueError
            except ValueError:
                raise ConfigError("scanner.host_id must be 16 hex digits")
        self.scanner["host_id"] = hid.upper()
        for key in ("short_press_profile", "long_press_profile"):
            name = self.service.get(key)
            if name and name not in self.profiles:
                raise ConfigError("service.%s: no profile %r" % (key, name))
        for prof in self.profiles.values():
            for out in prof.outputs:
                if out not in self.outputs:
                    raise ConfigError("profile %s: no output %r" % (prof.name, out))
        for name, out in self.outputs.items():
            try:
                outputs_mod.validate(name, out)
            except (TypeError, ValueError) as e:
                raise ConfigError("output %s: %s" % (name, e))

    @property
    def host_id(self):
        return bytes.fromhex(self.scanner["host_id"]) if self.scanner["host_id"] else None

    def profile(self, name):
        if name not in self.profiles:
            raise ConfigError("no profile %r" % name)
        return self.profiles[name]


def load(path=None, env=None, text=None):
    """Read the configuration. PATH can be TOML or JSON. Missing PATH gives the defaults."""
    env = os.environ if env is None else env
    data = copy.deepcopy(DEFAULTS)
    path = path or env.get(ENV_PREFIX + "CONFIG")
    if text is None and path:
        try:
            with open(path, "rb") as fh:
                text = fh.read().decode("utf-8")
        except OSError as e:
            raise ConfigError("cannot read %s: %s" % (path, e.strerror or e))
        except UnicodeDecodeError:
            raise ConfigError("%s: not UTF-8 text" % path)
    if text:
        try:
            if (path or "").endswith(".json") or text.lstrip().startswith("{"):
                user = json.loads(text)
            else:
                user = toml_loads(text)
        except ConfigError:
            raise
        except ValueError as e:                  # json.JSONDecodeError and tomllib.TOMLDecodeError
            raise ConfigError("%s: %s" % (path or "configuration", e))
        if not isinstance(user, dict):
            raise ConfigError("the configuration must be a table")
        unknown = set(user) - set(DEFAULTS)
        if unknown:                              # for example a misspelled [pipline]
            raise ConfigError("%s: unknown sections %s (known: %s)" % (path or "configuration", sorted(unknown),
                                                                      ", ".join(sorted(DEFAULTS))))
        if "profiles" in user:
            data["profiles"] = {}
        _merge(data, user)
    apply_env(data, env)
    return Config(data)
