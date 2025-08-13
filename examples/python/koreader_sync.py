#!/usr/bin/env python3
"""
koreader_sync.py
Reference Python implementation of the (unofficial) KOReader Sync Protocol.

Features:
- User auth test
- Dual document ID strategies (filename | partial)
- Progress PUT & GET
- Conflict-aware sync helper
- Debounced push guard
- Simple local persistence hook
- Optional queued retry skeleton

License: MIT
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
import pathlib
import logging
import threading
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, Callable, List

import requests

# ---------------- Configuration ---------------- #

BASE_URL = "https://sync.koreader.rocks"
ACCEPT_HEADER = "application/vnd.koreader.v1+json"
DEFAULT_DEBOUNCE_SECONDS = 25
DEFAULT_TIMEOUT = 12

LOG = logging.getLogger("koreader_sync")
if not LOG.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    LOG.addHandler(h)
LOG.setLevel(logging.INFO)

# ------------- Utility & Core Algorithms ------------- #


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def password_md5(password: str) -> str:
    return md5_hex(password.encode("utf-8"))


def doc_id_from_filename(path: str | pathlib.Path) -> str:
    return md5_hex(pathlib.Path(path).name.encode("utf-8"))


def doc_id_partial_md5(path: str | pathlib.Path) -> str:
    """
    Replicates KOReader 'partial MD5' sampling:
    Offsets: 1024 << (2*i) for i in [-1..10]; read 1024 bytes each; break if EOF reached.
    """
    p = pathlib.Path(path)
    step = size = 1024
    m = hashlib.md5()
    with p.open("rb") as f:
        for i in range(-1, 11):
            offset = step << (2 * i)
            try:
                f.seek(offset)
            except OSError:
                break
            chunk = f.read(size)
            if not chunk:
                break
            m.update(chunk)
    return m.hexdigest()


def clamp_percentage(p: float) -> float:
    if p < 0.0:
        return 0.0
    if p > 1.0:
        return 1.0
    return p


def compute_percentage(current_page: int, total_pages: int) -> float:
    if total_pages <= 0:
        return 0.0
    return clamp_percentage(current_page / float(total_pages))


# ---------------- Data Classes ---------------- #

@dataclass
class ProgressRecord:
    document: str
    progress: str      # raw field sent to server
    percentage: float
    device_id: str
    device: str
    timestamp: Optional[int] = None  # filled when GET from server
    # Local metadata not sent:
    local_page: Optional[int] = None
    total_pages: Optional[int] = None
    last_push_ts: Optional[float] = None

    def to_payload(self) -> Dict[str, Any]:
        return {
            "progress": self.progress,
            "percentage": self.percentage,
            "device_id": self.device_id,
            "document": self.document,
            "device": self.device,
        }


# ---------------- Persistence Skeleton ---------------- #

class ProgressStore:
    """
    Extremely simple JSON-lines store.
    For production: replace with SQLite or app DB.
    """
    def __init__(self, root: str | pathlib.Path):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / "progress.json"
        self._cache: Dict[str, ProgressRecord] = {}
        self._load()

    def _load(self):
        if not self.file.exists():
            return
        try:
            raw = json.loads(self.file.read_text("utf-8"))
            for doc_id, j in raw.items():
                self._cache[doc_id] = ProgressRecord(**j)
        except Exception as e:
            LOG.warning("Failed to load progress store: %s", e)

    def _flush(self):
        try:
            serial = {k: asdict(v) for k, v in self._cache.items()}
            self.file.write_text(json.dumps(serial, indent=2), encoding="utf-8")
        except Exception as e:
            LOG.error("Failed to persist progress store: %s", e)

    def get(self, doc_id: str) -> Optional[ProgressRecord]:
        return self._cache.get(doc_id)

    def upsert(self, record: ProgressRecord):
        self._cache[record.document] = record
        self._flush()


# ---------------- KOReader Sync Client ---------------- #

class KOSyncClient:
    def __init__(
        self,
        username: str,
        password_plain: str,
        device_name: str = "OpenAudiobook",
        id_mode: str = "filename",
        work_dir: str | pathlib.Path = "~/.koreader_sync",
        debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS,
        session_factory: Callable[[], requests.Session] = requests.Session,
    ):
        self.username = username
        self.auth_key = password_md5(password_plain)
        self.device_name = device_name
        self.id_mode = id_mode  # "filename" | "partial"
        self.debounce_seconds = debounce_seconds

        self.work_dir = pathlib.Path(work_dir).expanduser()
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self._device_id_file = self.work_dir / "device_id"
        self.device_id = self._load_or_create_device_id()

        self.store = ProgressStore(self.work_dir)
        self.session = session_factory()
        self.session.headers.update({"Accept": ACCEPT_HEADER})
        self.lock = threading.Lock()

        # Simple retry queue skeleton
        self.retry_queue: List[ProgressRecord] = []
        self.max_retries = 3

    # ---------- Device ID ---------- #

    def _load_or_create_device_id(self) -> str:
        if self._device_id_file.exists():
            return self._device_id_file.read_text().strip()
        did = uuid.uuid4().hex.upper()
        self._device_id_file.write_text(did)
        return did

    # ---------- Headers ---------- #

    def _auth_headers(self, content_type: bool = False) -> Dict[str, str]:
        h = {
            "X-Auth-User": self.username,
            "X-Auth-Key": self.auth_key,
        }
        if content_type:
            h["Content-Type"] = "application/json"
        return h

    # ---------- Document ID ---------- #

    def document_id(self, path: str | pathlib.Path) -> str:
        if self.id_mode == "partial":
            return doc_id_partial_md5(path)
        return doc_id_from_filename(path)

    # ---------- Public API ---------- #

    def test_auth(self) -> bool:
        url = f"{BASE_URL}/users/auth"
        try:
            r = self.session.get(url, headers=self._auth_headers(), timeout=DEFAULT_TIMEOUT)
            LOG.debug("Auth response %s %s", r.status_code, r.text)
            return r.status_code == 200
        except requests.RequestException as e:
            LOG.error("Auth request error: %s", e)
            return False

    def get_progress(self, path: str | pathlib.Path) -> Optional[ProgressRecord]:
        doc_id = self.document_id(path)
        url = f"{BASE_URL}/syncs/progress/{doc_id}"
        try:
            r = self.session.get(url, headers=self._auth_headers(), timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as e:
            LOG.error("GET progress network error: %s", e)
            return None

        if r.status_code == 404:
            return None
        if r.status_code == 401:
            raise PermissionError("Authentication failed (401).")
        if r.status_code != 200:
            LOG.warning("Unexpected GET status %s body=%s", r.status_code, r.text)
            return None

        try:
            data = r.json()
        except Exception:
            LOG.error("Failed to parse JSON: %s", r.text)
            return None

        record = ProgressRecord(
            document=data["document"],
            progress=data["progress"],
            percentage=float(data["percentage"]),
            device_id=data["device_id"],
            device=data.get("device", ""),
            timestamp=int(data.get("timestamp", 0)),
        )
        # merge with local store if exists
        local = self.store.get(doc_id)
        if local:
            record.local_page = local.local_page
            record.total_pages = local.total_pages
            record.last_push_ts = local.last_push_ts
        return record

    def put_progress(self, record: ProgressRecord) -> bool:
        url = f"{BASE_URL}/syncs/progress"
        payload = record.to_payload()
        try:
            r = self.session.put(
                url,
                headers=self._auth_headers(content_type=True),
                json=payload,
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as e:
            LOG.warning("PUT progress network error: %s", e)
            self._enqueue_retry(record)
            return False

        if r.status_code == 200:
            record.last_push_ts = time.time()
            self.store.upsert(record)
            LOG.info("Progress synced: %s %.2f%%", record.progress, record.percentage * 100)
            return True
        if r.status_code == 401:
            raise PermissionError("Authentication failed (401).")
        LOG.warning("PUT status=%s body=%s", r.status_code, r.text)
        self._enqueue_retry(record)
        return False

    def sync_with_conflict(
        self,
        path: str | pathlib.Path,
        current_page: int,
        total_pages: int,
        page_to_progress: Optional[Callable[[int], str]] = None,
        adopt_remote_threshold: float = 0.02,
    ) -> int:
        """
        Returns the page number the caller should display after resolving conflict.
        """
        doc_id = self.document_id(path)
        remote = self.get_progress(path)

        local_pct = compute_percentage(current_page, total_pages)
        record = ProgressRecord(
            document=doc_id,
            progress=(page_to_progress(current_page) if page_to_progress else str(current_page)),
            percentage=local_pct,
            device_id=self.device_id,
            device=self.device_name,
            local_page=current_page,
            total_pages=total_pages,
        )

        # No remote record
        if not remote:
            LOG.info("No remote progress; pushing local page=%s (%.2f%%)", current_page, local_pct * 100)
            self.put_progress(record)
            return current_page

        # Remote ahead?
        remote_pg = int(remote.progress) if remote.progress.isdigit() else None
        remote_pct = remote.percentage
        delta = remote_pct - local_pct

        if delta > adopt_remote_threshold and remote_pg:
            LOG.info(
                "Adopting remote progress: local %.2f%% vs remote %.2f%% (page %d)",
                local_pct * 100,
                remote_pct * 100,
                remote_pg,
            )
            return remote_pg

        # Local ahead or similar -> push local
        if delta < -adopt_remote_threshold:
            LOG.info("Local ahead (%.2f%% vs %.2f%%); pushing.", local_pct * 100, remote_pct * 100)
        else:
            LOG.info("Percentages close (Δ=%.3f); reaffirming local.", delta)
        self.put_progress(record)
        return current_page

    # ---------- Debounced Update Helper ---------- #

    def debounced_put(
        self,
        path: str | pathlib.Path,
        current_page: int,
        total_pages: int,
        force: bool = False,
        min_page_delta: int = 1,
    ) -> bool:
        """
        Attempt to push progress if debounce interval and page delta conditions met.
        """
        doc_id = self.document_id(path)
        now = time.time()
        with self.lock:
            local = self.store.get(doc_id)
            last_push = local.last_push_ts if local else 0
            last_page = local.local_page if local and local.local_page is not None else 0

            if not force:
                if (now - last_push) < self.debounce_seconds:
                    return False
                if (current_page - last_page) < min_page_delta:
                    return False

            pct = compute_percentage(current_page, total_pages)
            record = ProgressRecord(
                document=doc_id,
                progress=str(current_page),
                percentage=pct,
                device_id=self.device_id,
                device=self.device_name,
                local_page=current_page,
                total_pages=total_pages,
            )
            return self.put_progress(record)

    # ---------- Retry Queue Skeleton ---------- #

    def _enqueue_retry(self, record: ProgressRecord):
        LOG.debug("Enqueued for retry: %s", record.document)
        self.retry_queue.append(record)

    def flush_retries(self):
        ok_queue: List[ProgressRecord] = []
        # naive implementation: attempt once
        for rec in list(self.retry_queue):
            if self.put_progress(rec):
                ok_queue.append(rec)
        # remove successes
        self.retry_queue = [r for r in self.retry_queue if r not in ok_queue]

    # ---------- CLI Convenience ---------- #

def _cli():
    import argparse

    ap = argparse.ArgumentParser(description="KOReader sync reference client")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True, help="Plain password (will be MD5 hashed client-side)")
    ap.add_argument("--file", required=True, help="Path to book file")
    ap.add_argument("--page", type=int, help="Current page for update")
    ap.add_argument("--total", type=int, help="Total pages for update")
    ap.add_argument("--mode", choices=["filename", "partial"], default="filename")
    ap.add_argument("--action", choices=["test-auth", "get", "put", "sync-conflict"], default="get")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.verbose:
        LOG.setLevel(logging.DEBUG)

    client = KOSyncClient(
        username=args.user,
        password_plain=args.password,
        id_mode=args.mode,
    )

    if args.action == "test-auth":
        ok = client.test_auth()
        print("AUTH:", "OK" if ok else "FAIL")
        return

    if args.action == "get":
        rec = client.get_progress(args.file)
        if not rec:
            print("No remote progress.")
        else:
            print(json.dumps(asdict(rec), indent=2))
        return

    if args.action == "put":
        if args.page is None or args.total is None:
            ap.error("--page and --total required for put")
        pct = compute_percentage(args.page, args.total)
        record = ProgressRecord(
            document=client.document_id(args.file),
            progress=str(args.page),
            percentage=pct,
            device_id=client.device_id,
            device=client.device_name,
            local_page=args.page,
            total_pages=args.total,
        )
        ok = client.put_progress(record)
        print("PUT:", "OK" if ok else "FAIL")
        return

    if args.action == "sync-conflict":
        if args.page is None or args.total is None:
            ap.error("--page and --total required for sync-conflict")
        resolved_page = client.sync_with_conflict(args.file, args.page, args.total)
        print(f"Resolved page: {resolved_page}")
        return


if __name__ == "__main__":
    _cli()
