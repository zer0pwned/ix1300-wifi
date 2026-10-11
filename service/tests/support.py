"""Test set-up: import path, quiet logs, no tesseract (deterministic results)."""
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# The tests must not depend on a tesseract program on the test computer.
os.environ.setdefault("IX1300_TESSERACT", "ix1300-no-tesseract-for-tests")

logging.basicConfig(level=getattr(logging, os.environ.get("IX1300_TEST_LOG", "CRITICAL")),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

HOST_ID = "0123456789ABCDEF"
HOST_MAC = bytes.fromhex("020000a1b2c3")
HOST_IP = "127.0.0.1"
