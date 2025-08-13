#!/usr/bin/env python3
"""
test_sync.py
Lightweight test harness for the reference KOReader sync client.

These tests are integration-style: they will talk to the real server if credentials are provided
via environment variables:

    export KOREADER_SYNC_USER="username"
    export KOREADER_SYNC_PASS="plaintext_password"

(Password will be MD5 hashed client-side.)

USE A THROWAWAY TEST ACCOUNT.

Run:
    python test_sync.py                  (basic sequential run)
    python -m pytest test_sync.py        (pytest will discover the functions)

NOTE: This is intentionally simple (no external frameworks required).
"""

from __future__ import annotations

import os
import time
import random
import string
from pathlib import Path

from koreader_sync import KOSyncClient, compute_percentage


TEST_FILE = Path(__file__).parent / "dummy_book.epub"
TEST_FILE.write_text("DUMMY CONTENT FOR PARTIAL MD5 TEST\n" + "x" * 10000)


def rand_device_suffix(n=4):
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))


def require_env():
    user = os.environ.get("KOREADER_SYNC_USER")
    pwd = os.environ.get("KOREADER_SYNC_PASS")
    if not user or not pwd:
        raise RuntimeError("Please set KOREADER_SYNC_USER & KOREADER_SYNC_PASS for integration tests.")
    return user, pwd


def test_auth():
    user, pwd = require_env()
    client = KOSyncClient(user, pwd, device_name="PyTestDevice-" + rand_device_suffix())
    assert client.test_auth(), "Authentication failed"


def test_put_and_get_filename_mode():
    user, pwd = require_env()
    client = KOSyncClient(user, pwd, id_mode="filename", device_name="PyTestDevice-" + rand_device_suffix())
    page = 7
    total = 200
    pct = compute_percentage(page, total)

    # Put
    resolved = client.sync_with_conflict(TEST_FILE, page, total)
    assert resolved == page

    # Get
    remote = client.get_progress(TEST_FILE)
    assert remote is not None
    assert abs(remote.percentage - pct) < 1e-6
    assert remote.progress == str(page)


def test_put_and_get_partial_md5_mode():
    user, pwd = require_env()
    client = KOSyncClient(user, pwd, id_mode="partial", device_name="PyTestDevice-" + rand_device_suffix())
    page = 15
    total = 300
    client.debounced_put(TEST_FILE, page, total, force=True)
    remote = client.get_progress(TEST_FILE)
    assert remote is not None
    assert remote.progress == str(page)


def test_debounce_logic():
    user, pwd = require_env()
    client = KOSyncClient(user, pwd, id_mode="filename", device_name="PyTestDevice-" + rand_device_suffix(), debounce_seconds=5)
    page = 3
    total = 40
    ok1 = client.debounced_put(TEST_FILE, page, total, force=True)
    assert ok1
    # Immediate second push should be skipped
    ok2 = client.debounced_put(TEST_FILE, page + 1, total)
    assert not ok2
    time.sleep(5.2)
    ok3 = client.debounced_put(TEST_FILE, page + 1, total)
    assert ok3


if __name__ == "__main__":
    # Run tests manually if not using pytest
    for fn in [
        test_auth,
        test_put_and_get_filename_mode,
        test_put_and_get_partial_md5_mode,
        test_debounce_logic,
    ]:
        print(f"Running {fn.__name__} ...")
        fn()
    print("All tests completed.")
