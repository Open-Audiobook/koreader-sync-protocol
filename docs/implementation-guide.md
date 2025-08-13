# KOReader Sync Protocol – Implementation Guide

> Practical, step-by-step engineering guide for adding KOReader progress sync to a third‑party reading or audiobook application (text, TTS, hybrid). Complements `api-reference.md`.

---

## Table of Contents
1. Architecture Overview
2. Decision Matrix
3. Step 1: Credentials & Auth
4. Step 2: Device Identity
5. Step 3: Document ID Strategy
6. Step 4: Virtual Pagination (EPUB)
7. Step 5: Progress & Percentage Logic
8. Step 6: Sync Triggers & Debouncing
9. Step 7: Conflict Resolution
10. Step 8: Error Handling & Retries
11. Step 9: Data Persistence Layer
12. Step 10: UI/UX Patterns
13. Audiobook ↔ Text Bridging
14. Testing Strategy
15. Example Python Client (Full)
16. Future Enhancements
17. Checklist
18. Troubleshooting
19. Appendix: ASCII Sequence Diagrams
20. Appendix: Pseudo-code Summary

---

## 1. Architecture Overview

High-level flow:

```
+-----------+      PUT /syncs/progress      +-----------------------+
|  Client A |  --------------------------->  | KOReader Sync Server  |
| (Reader)  |                                +-----------------------+
|           |  <---------------------------  GET /syncs/progress/{id}
+-----------+      JSON Progress            (Latest progress state)

+-----------+
|  Client B |
| (KOReader)|
+-----------+
```

Both clients act symmetrically: last write wins; each stores device + timestamp.

---

## 2. Decision Matrix

| Aspect | Option A | Option B | Recommendation |
|--------|----------|----------|----------------|
| Document ID | Filename MD5 | Partial content MD5 | Support both (user setting / auto) |
| Progress granularity | Page number | Structural pointer | Start with page number |
| Push timing | Every change | Debounced batch | Debounced |
| Conflict resolution | Always remote | Smart merge | Smart (Section 9) |
| Offline mode | Fail | Queue & retry | Queue & retry |
| Auth caching | Plain MD5 | Encrypted at rest | Secure storage (OS keystore) |

---

## 3. Step 1: Credentials & Auth

1. Collect username + password (plaintext).
2. Store `md5(password)`—do *not* store plaintext if avoidable.
3. Save in secure storage (Keychain / Android Keystore / OS credential vault).
4. Provide "Forget account" deleting hash.

```python
def password_md5(pw: str) -> str:
    import hashlib
    return hashlib.md5(pw.encode()).hexdigest()
```

---

## 4. Step 2: Device Identity

Requirements:
- Stable across app restarts.
- Unique per install (renaming device should not break continuity).

Generation:
```python
import uuid, json, os, pathlib
CONFIG = pathlib.Path.home()/".myreaderapp/device.json"

def get_device_id():
    if CONFIG.exists():
        return json.loads(CONFIG.read_text())["device_id"]
    did = uuid.uuid4().hex.upper()
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"device_id": did}))
    return did
```

`device` string (human readable):  
- Use model name (`AndroidTablet`, `DesktopLinux`, `OpenAudiobook`).

---

## 5. Step 3: Document ID Strategy

Implement both:

### Filename Mode
```python
import hashlib, pathlib
def doc_id_filename(path):
    return hashlib.md5(pathlib.Path(path).name.encode()).hexdigest()
```

### Partial MD5 Mode
(Stops when offset > file size.)
```python
def partial_md5(path):
    import hashlib
    m = hashlib.md5()
    step = size = 1024
    with open(path, "rb") as f:
        for i in range(-1, 11):
            offset = step << (2*i)
            f.seek(offset)
            block = f.read(size)
            if not block:
                break
            m.update(block)
    return m.hexdigest()
```

Policy recommendation:
- Default = filename mode.
- Auto-switch to partial if collision detected (two different local files producing same ID).

Collision detection heuristic:
- Maintain map `doc_id -> local file path`.
- If new path maps to existing doc_id with different file size ±5%, prompt user to switch ID mode.

---

## 6. Step 4: Virtual Pagination (EPUB)

Essential for consistent percentage semantics.

Minimum viable approach:
1. Parse EPUB spine (ordered XHTML files).
2. Render each document fragment to layout boxes (use your HTML renderer).
3. Flow content into pages sized `(viewport_width - margins, viewport_height - margins)`.
4. Count final page count => `total_virtual_pages`.
5. Maintain mapping: `page_index -> (spine_index, offset)`.

Performance suggestions:
- Pre-paginate lazily (first N chapters).
- Background thread for remaining chapters.
- Cache per combination: `(font_size, font_family, line_height, width, height)`.

Caching key format example:
```
epub_pagination/<doc_digest>/<font_size>_<w>x<h>_<line_spacing>.json
```

---

## 7. Step 5: Progress & Percentage Logic

```python
def compute_percentage(page, total_pages):
    if total_pages <= 0:
        return 0.0
    pct = page / total_pages
    return max(0.0, min(1.0, pct))
```

Note: Use 0-based internal page counters if convenient, but convert to 1-based for user presentation while still using ratio `(current_page / total_pages)` with current_page as 1-based for parity with KOReader semantics.

---

## 8. Step 6: Sync Triggers & Debouncing

Trigger conditions (recommended):

| Event | Condition | Action |
|-------|-----------|--------|
| Page turn | +1 internal page | Mark dirty (no immediate push) |
| Idle | 10s since last input & dirty & debounce elapsed | Push |
| App background | Dirty | Push immediately |
| Book close | Any | Push |
| Explicit user action (Sync Now) | Always | Push & force GET verify |

Debounce:
```python
DEBOUNCE_SECONDS = 25
if (now - last_push) >= DEBOUNCE_SECONDS and dirty:
    push_progress()
```

---

## 9. Conflict Resolution

Scenario: Remote progress differs from local upon open/resume.

Algorithm:
1. `remote = GET(document_id)`
2. If `remote is None`: push local.
3. Else compute:
   - `delta_pages = remote_page - local_page`
   - `delta_pct = remote_pct - local_pct`
4. Policy:
   - If `delta_pct > 0.02` and remote newer → adopt remote.
   - If `delta_pct < -0.02` and user was active recently (< 60s) → keep local & push.
   - If ambiguous → show prompt:

Prompt example:
```
Resume?
Local: Page 120 (48.0%)
Remote (Tablet): Page 132 (52.8%) synced 5m ago
[Use Remote] [Keep Local]
```

Store last decision to reduce prompt fatigue (e.g., “Always prefer latest”).

---

## 10. Step 7: Error Handling & Retries

Pseudo-code:
```python
def safe_put(payload, attempt=1):
    try:
        r = session.put(url, json=payload, timeout=8)
    except (TimeoutError, OSError):
        queue_retry(payload)
        return
    if r.status_code in (500, 502, 503, 504):
        if attempt <= 3:
            backoff = 2 ** attempt
            schedule(backoff, safe_put, payload, attempt+1)
        else:
            queue_retry(payload)
    elif r.status_code == 401:
        mark_auth_invalid()
    elif r.status_code == 400:
        log("Bad payload", r.text)
    else:
        mark_synced()
```

Queue structure:
- Persistent queue file (JSON lines).
- Flush on network recovery / user manual sync.

---

## 11. Step 8: Data Persistence Layer

Minimum fields to persist per book:
| Field | Purpose |
|-------|---------|
| `local_page` | Last local page |
| `total_pages` | For percentage recalculation |
| `percentage` | Redundant but handy |
| `last_push_ts` | Debouncing |
| `doc_id` | Sync mapping |
| `id_mode` | Filename or partial |
| `remote_timestamp` | Conflict detect |
| `struct_pointer` (optional) | Future structural resume |

Storage options:
- Lightweight: SQLite table.
- Simpler: Single JSON per document in app data directory.

---

## 12. Step 9: UI / UX Patterns

Recommended indicators:
- Sync icon (idle / pending / error).
- “Resolve conflict” dialog (only when necessary).
- Settings:
  - Enable sync (toggle)
  - ID mode (auto / filename / partial)
  - Show device origin for last sync
  - Force re-sync

Diagnostics panel:
```
Username: alice
Device ID: 57F6...1802
Document ID Mode: filename
Last Push: 2025-08-13 10:48:22Z
Last Response: 200 OK
Queued Updates: 0
```

---

## 13. Audiobook ↔ Text Bridging

Goal: Seamless switching between listening (TTS) and visual reading.

Mapping strategy:
1. Preprocess EPUB: generate cumulative character offsets per virtual page.
2. Audiobook time → approximate char offset (using average chars/sec or alignment map).
3. Binary search page whose char span contains offset.
4. Sync using that page number.

Optional enhancement: store `_meta_audio_seconds` (NOT currently supported server-side; keep locally).

---

## 14. Testing Strategy

| Test | Description |
|------|-------------|
| Unit: partial_md5 | Known fixtures produce known digest |
| Unit: percentage clamp | Negative & overflow cases |
| Integration: auth failure | Wrong hash returns 401 |
| Integration: progress lifecycle | PUT then GET match |
| Conflict: remote ahead | Local replaced under policy |
| Resilience: offline queue | Disconnected pushes stored and flushed |
| Performance: pagination | Large EPUB ( > 5MB ) paginates under threshold (e.g. < 3s initial) |

Mocking:
- Provide mock server responding with canned JSON for CI (do not hammer real endpoint).

---

## 15. Example Python Client (Full)

```python
import requests, hashlib, uuid, time, pathlib, json, os

BASE = "https://sync.koreader.rocks"
ACCEPT = "application/vnd.koreader.v1+json"

class KOSyncClient:
    def __init__(self, username, password_plain, device_name="OpenAudiobook", id_mode="filename"):
        self.username = username
        self.auth_key = hashlib.md5(password_plain.encode()).hexdigest()
        self.device_id = self._get_or_create_device_id()
        self.device_name = device_name
        self.id_mode = id_mode
        self.last_push_ts = {}
        self.session = requests.Session()
        self.session.headers.update({"Accept": ACCEPT})

    def _get_or_create_device_id(self):
        p = pathlib.Path.home()/".openab_sync/device.json"
        if p.exists():
            return json.loads(p.read_text())["device_id"]
        did = uuid.uuid4().hex.upper()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"device_id": did}))
        return did

    def _headers(self):
        return {
            "X-Auth-User": self.username,
            "X-Auth-Key": self.auth_key,
            "Content-Type": "application/json"
        }

    def doc_id(self, path):
        if self.id_mode == "partial":
            return self._partial_md5(path)
        return hashlib.md5(pathlib.Path(path).name.encode()).hexdigest()

    def _partial_md5(self, path):
        step = size = 1024
        m = hashlib.md5()
        with open(path, "rb") as f:
            for i in range(-1, 11):
                offset = step << (2*i)
                f.seek(offset)
                chunk = f.read(size)
                if not chunk:
                    break
                m.update(chunk)
        return m.hexdigest()

    def compute_percentage(self, current_page, total_pages):
        if total_pages <= 0:
            return 0.0
        pct = current_page / total_pages
        return max(0.0, min(1.0, pct))

    def put_progress(self, path, current_page, total_pages):
        docid = self.doc_id(path)
        pct = self.compute_percentage(current_page, total_pages)
        payload = {
            "progress": str(current_page),
            "percentage": pct,
            "device_id": self.device_id,
            "document": docid,
            "device": self.device_name
        }
        r = self.session.put(f"{BASE}/syncs/progress", headers=self._headers(), json=payload, timeout=10)
        if r.status_code == 200:
            self.last_push_ts[docid] = time.time()
            return True
        elif r.status_code == 401:
            raise RuntimeError("Auth failed")
        else:
            print("Push error:", r.status_code, r.text)
            return False

    def get_progress(self, path):
        docid = self.doc_id(path)
        r = self.session.get(f"{BASE}/syncs/progress/{docid}", headers=self._headers(), timeout=10)
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 404:
            return None
        elif r.status_code == 401:
            raise RuntimeError("Auth failed")
        print("Get error:", r.status_code, r.text)
        return None

    def sync_with_conflict(self, path, local_page, total_pages):
        remote = self.get_progress(path)
        if not remote:
            self.put_progress(path, local_page, total_pages)
            return local_page
        remote_page = int(remote["progress"])
        remote_pct = remote["percentage"]
        local_pct = self.compute_percentage(local_page, total_pages)
        # Simple policy:
        if remote_pct > local_pct + 0.02:
            return remote_page  # adopt remote
        elif local_pct > remote_pct + 0.02:
            self.put_progress(path, local_page, total_pages)
            return local_page
        else:
            # Percentages close: keep local
            self.put_progress(path, local_page, total_pages)
            return local_page
```

---

## 16. Future Enhancements

| Feature | Description | Status |
|---------|-------------|--------|
| Multi-language clients | JS / Kotlin / Swift | Planned |
| Structural progress support | Mapping DOM pointer | TBD |
| Local diff viewer | Compare remote vs local context | TBD |
| Sync analytics | Histogram of push intervals | TBD |
| Embedding highlights (extension) | Hypothetical `_extras` field | Out-of-scope now |

---

## 17. Checklist

| Item | Done |
|------|------|
| Auth hashing implemented | ☐ |
| Device ID persisted | ☐ |
| Dual doc ID support | ☐ |
| EPUB pagination working | ☐ |
| Percentage calc with clamp | ☐ |
| Debounce logic (≥25s) | ☐ |
| Conflict policy implemented | ☐ |
| Offline queue | ☐ |
| UI sync indicators | ☐ |
| Unit & integration tests | ☐ |
| Error telemetry | ☐ |

---

## 18. Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| 401 on all calls | Bad MD5 or username | Re-enter password; verify hash |
| 200 on PUT, but no progress on GET | Wrong `document` ID derivation mode mismatch | Switch mode (filename ↔ partial) |
| Percent jumps backwards | Page numbering resets after font change | Recompute total pages before pushing |
| Frequent conflict prompts | Too small threshold | Increase `delta_pct` threshold (e.g. 0.05) |
| Colliding books | Same filename mode | Use partial MD5 mode |
| High battery/network usage | Over-synchronizing | Increase debounce interval |

---

## 19. Appendix: ASCII Sequence Diagrams

### Initial Sync (No Remote Record)

```
Client                  Server
  |  GET /progress/{doc} |
  |--------------------->| (404)
  |<---------------------|
  |  PUT /progress       |
  |--------------------->| (200)
  |<---------------------|
```

### Conflict (Remote Ahead)

```
Client A                Server               Client B
  | PUT page=10          |                       |
  |--------------------->| store(10)             |
  |                      |                       |
  |                      |<----------------------| PUT page=15
  |                      | store(15)             |
  | GET /progress        |                       |
  |--------------------->| returns page=15       |
  |<---------------------|                       |
  | adopt page=15        |                       |
```

---

## 20. Appendix: Pseudo-code Summary

```python
def open_book(path):
    paginate_if_needed(path)
    remote = sync.get_progress(path)
    if remote:
        local_page = load_local_page(path)
        decision = resolve_conflict(local_page, remote)
        if decision == "remote":
            jump_to(remote_page)
        else:
            sync.put_progress(path, local_page, total_pages)
    else:
        sync.put_progress(path, 1, total_pages)

def on_page_turn(new_page):
    mark_dirty(new_page)
    maybe_schedule_push()

def maybe_schedule_push():
    if dirty and (now - last_push_ts) > 25:
        push_progress()

def push_progress():
    sync.put_progress(path, current_page, total_pages)
    clear_dirty()
```

---

Need clarifications or want to contribute improvements? Open an issue with:
- Steps performed
- Expected vs actual behavior
- Example payload (sanitize credentials)

---

Happy integrating!
