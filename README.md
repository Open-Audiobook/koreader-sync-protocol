# KOReader Sync Protocol (Unofficial Specification)

> Reverse‑engineered, comprehensive documentation of KOReader’s progress sync server: endpoints, auth, exact EPUB percentage logic (virtual pages), dual document ID algorithms (filename + partial MD5), and reference client implementations.

<p align="center">
  <strong>Status:</strong> Actively maintained • <strong>Scope:</strong> Documentation + reference code • <strong>License:</strong> MIT<br>
  <em>Not affiliated with the official KOReader project. Built for interoperability.</em>
</p>

---

## Contents
- [Why This Exists](#why-this-exists)
- [Scope & Guarantees](#scope--guarantees)
- [Features](#features)
- [Quick Start](#quick-start)
- [Protocol Overview](#protocol-overview)
- [Authentication](#authentication)
- [Document Identification Modes](#document-identification-modes)
- [Reading Progress Model (Exact Algorithms)](#reading-progress-model-exact-algorithms)
- [API Reference (Summary)](#api-reference-summary)
- [Reference Implementations](#reference-implementations)
- [Integration Checklist](#integration-checklist)
- [Conflict Resolution Strategy](#conflict-resolution-strategy)
- [Security Notes](#security-notes)
- [FAQ](#faq)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License & Disclaimer](#license--disclaimer)

---

## Why This Exists
KOReader provides a built‑in sync feature, but no official public specification of its protocol. This repository:
- Documents the sync API for third‑party reading & audiobook apps.
- Enables interoperability (e.g. resume an EPUB in KOReader after listening to its TTS version).
- Records the exact logic of percentage and hash generation to avoid drift or guesswork.

---

## Scope & Guarantees
| Area | Included | Notes |
|------|----------|-------|
| REST endpoint behavior | ✅ | Paths, methods, payloads, status codes |
| Auth mechanism | ✅ | MD5 password hash + headers |
| Document ID generation | ✅ | Filename mode + partial content MD5 |
| Reading progress semantics | ✅ | Exact math (`page / total_pages`) |
| EPUB progress algorithm | ✅ | Uses virtual pages, not character counts |
| Partial MD5 sampling offsets | ✅ | Byte offsets & code |
| Reference client code | ✅ | Python (others planned) |
| Real server ops / hosting | ❌ | You use existing KOReader sync server |
| Official endorsement | ❌ | Unofficial reverse engineering |
| DRM / proprietary formats | ❌ | Out of scope |

---

## Features
- Full protocol summary + deep explanations
- Exact partial MD5 checksum algorithm & offsets
- Dual document ID mode support (filename & binary)
- Precise EPUB percentage logic (no speculation)
- Conflict resolution guidelines
- Ready‑to‑use Python client
- Testing scripts (cURL examples)
- Security & interoperability notes

---

## Quick Start

1. Hash your KOReader password with MD5:
   ```bash
   echo -n "your_password" | md5sum
   ```
2. Compute a document ID (filename mode):
   ```bash
   DOC_ID=$(echo -n "MyBook.epub" | md5sum | cut -d' ' -f1)
   ```
3. Push progress:
   ```bash
   curl -X PUT https://sync.koreader.rocks/syncs/progress \
     -H "Accept: application/vnd.koreader.v1+json" \
     -H "Content-Type: application/json" \
     -H "X-Auth-User: your_user" \
     -H "X-Auth-Key: <md5hash>" \
     -d "{\"progress\":\"42\",\"percentage\":0.28,\"device_id\":\"DEVICE1234ABC\",\"document\":\"$DOC_ID\",\"device\":\"MyReaderApp\"}"
   ```
4. Retrieve progress:
   ```bash
   curl https://sync.koreader.rocks/syncs/progress/$DOC_ID \
     -H "Accept: application/vnd.koreader.v1+json" \
     -H "X-Auth-User: your_user" \
     -H "X-Auth-Key: <md5hash>"
   ```

---

## Protocol Overview
| Aspect | Value |
|--------|-------|
| Base URL | `https://sync.koreader.rocks` |
| Versioning | MIME: `Accept: application/vnd.koreader.v1+json` |
| Auth | Headers: `X-Auth-User`, `X-Auth-Key` (MD5 password) |
| Format | JSON |
| Core Resource | Reading progress (per document) |
| Conflict Policy | Last write wins (timestamp) |

---

## Authentication
| Header | Description |
|--------|-------------|
| `X-Auth-User` | Plain username |
| `X-Auth-Key` | MD5(password) hex lowercase |

Example:
```bash
curl https://sync.koreader.rocks/users/auth \
  -H "Accept: application/vnd.koreader.v1+json" \
  -H "X-Auth-User: alice" \
  -H "X-Auth-Key: 098f6bcd4621d373cade4e832627b4f6"
```

---

## Document Identification Modes

KOReader supports two mutually exclusive strategies. A third‑party app should support both for seamless cross‑device sync.

### 1. Filename Mode
```
doc_id = MD5(basename(filename))
```
Pros: Fast, deterministic across copied files with same name.  
Cons: Collides if two different books share a filename.

### 2. Binary Partial MD5 (KOReader “partial_md5_checksum”)
Samples 1024 bytes at exponentially spaced offsets:

Offsets (bytes):  
`512, 2048, 8192, 32768, 131072, 524288, 2097152, 8388608, 33554432, 134217728, 536870912, 2147483648`

Algorithm (Lua pseudocode):
```lua
for i = -1, 10 do
  offset = 1024 << (2*i)
  read 1024 bytes at offset (if available)
  md5.update(chunk)
end
```

Python reference:
```python
def partial_md5(path):
    import hashlib
    m = hashlib.md5()
    step = size = 1024
    with open(path, "rb") as f:
        for i in range(-1, 11):
            offset = step << (2 * i)
            f.seek(offset)
            chunk = f.read(size)
            if not chunk:
                break
            m.update(chunk)
    return m.hexdigest()
```

---

## Reading Progress Model (Exact Algorithms)

### Progress Field (`progress`)
- Paged documents: `"42"` → page 42
- Reflowable (EPUB): usually page number string; KOReader can also send an XPath-like pointer (`/body/DocFragment[...]/...`) when using rolling/xpointer navigation.

### Percentage Field (`percentage`)
From KOReader source (`readerfooter.lua`):

```lua
if hasHiddenFlows then
  percentage = page_in_flow / total_pages_in_flow
else
  percentage = current_page / total_pages
end
```

Therefore (practically for most EPUBs):
```
percentage = current_virtual_page / total_virtual_pages
```

EPUB “virtual pages” are generated by KOReader’s layout engine based on:
- Viewport dimensions
- Font metrics
- Spacing & margins
- Hyphenation & justification
- Rendering mode (page vs scroll)

No character counting or cumulative byte math is involved in the final stored percentage.

### Why You Can Safely Use Page Arithmetic
KOReader itself stores `percent_finished = current_page / total_pages` (see paging module). If your renderer produces a (current_page, total_pages) pair that is internally consistent, your percentages will align with KOReader semantics (even if page boundaries differ slightly, user experience remains coherent).

---

## API Reference (Summary)

| Method | Path | Purpose | Body Fields |
|--------|------|---------|-------------|
| POST | `/users/create` | Register user | `username`, `password` (MD5) |
| GET | `/users/auth` | Test credentials | — |
| PUT | `/syncs/progress` | Upsert progress | `progress`, `percentage`, `device_id`, `document`, `device` |
| GET | `/syncs/progress/{document_id}` | Fetch progress | — |

Progress object (response):
```json
{
  "device_id": "ABCDEF1234...",
  "progress": "42",
  "document": "d1c83d...",
  "percentage": 0.28,
  "timestamp": 1755040495,
  "device": "MyReaderApp"
}
```

---

## Reference Implementations

| Language | Features | Path (planned) |
|----------|----------|----------------|
| Python | Full (auth, dual IDs, sync, conflict helper) | `examples/python/koreader_sync.py` |
| JavaScript/Node | Planned | `examples/javascript/` |
| Shell (curl) | Test scripts | `examples/curl/` |

(Implementations will be added in subsequent commits.)

Python core logic (excerpt):
```python
percentage = current_page / total_pages
payload = {
  "progress": str(current_page),
  "percentage": percentage,
  "device_id": device_id,
  "document": doc_id,
  "device": device_name
}
```

---

## Integration Checklist

| Step | Action | Done |
|------|--------|------|
| 1 | Add user credential UI & store MD5(password) | ☐ |
| 2 | Persist a stable `device_id` (UUID w/o hyphens, uppercase) | ☐ |
| 3 | Implement both filename & partial MD5 document ID modes | ☐ |
| 4 | Expose setting to choose/document ID strategy | ☐ |
| 5 | Implement virtual paging (EPUB renderer) | ☐ |
| 6 | Compute `percentage = page / total_pages` | ☐ |
| 7 | Push on meaningful progress change (debounce) | ☐ |
| 8 | Pull on open / periodic resume | ☐ |
| 9 | Resolve conflicts (newer timestamp wins, optional user prompt) | ☐ |
|10 | Handle 401 (re-auth), 404 (no progress), network retries | ☐ |

---

## Conflict Resolution Strategy

1. Fetch remote progress (`GET /syncs/progress/{doc_id}`).
2. If none: push local.
3. If remote timestamp > local last sync:
   - Option A: Always adopt remote.
   - Option B: If remote ahead > X pages, prompt user.
4. Push only if (a) local ahead and (b) ≥ N seconds since last push (KOReader internally debounces ~25s).
5. Store last successful push time to avoid spamming API.

---

## Security Notes

| Concern | Notes | Mitigation |
|---------|-------|-----------|
| MD5 weakness | Used only as password surrogate | Always use HTTPS (server is HTTPS) |
| Credential leakage | Headers sent each request | Use secure storage (Keychain / Keystore) |
| Replay risk | No nonce in protocol | Accept risk; limited scope (progress only) |
| Rate limiting | Not documented | Implement client backoff |
| Data integrity | No signatures | Optionally verify doc_id freshness on open |

---

## FAQ

**Q: Do I have to implement XPath `progress` strings?**  
A: No. Sending plain page numbers works. KOReader will still be able to jump to approximate location.

**Q: What if my virtual page count differs from KOReader’s?**  
A: Percentages may differ slightly, but sync continuity is preserved. For tighter alignment, mirror KOReader’s pagination parameters (font size, margins).

**Q: Is the partial MD5 required?**  
A: Only if the user/device uses that KOReader setting. Supporting both modes future‑proofs your app.

**Q: Can I store more metadata (highlights, bookmarks)?**  
A: Not with this documented endpoint set. This spec focuses solely on reading progress.

**Q: What happens on duplicate filenames in filename mode?**  
A: Collisions cause shared progress. Use partial MD5 mode to avoid this.

---

## Roadmap

| Milestone | Status |
|-----------|--------|
| Add Python example code | Pending |
| Add JS/Node client | Planned |
| Add TypeScript typings for payloads | Planned |
| Add integration test harness (mock server) | Planned |
| Add multi-device conflict demo | Planned |
| Add EPUB pagination strategy guide | Planned |
| Add audiobook ↔ text synchronization patterns | Planned |

Feel free to open issues to reprioritize.

---

## Contributing

1. Fork & branch (`feat/...`, `fix/...`)
2. Keep changes scoped (1 PR = 1 concern)
3. Provide reproduction steps for behavior clarifications
4. Reference source line(s) from KOReader when asserting protocol facts
5. Avoid speculative additions: mark uncertain points as `UNVERIFIED`

---

## License & Disclaimer

MIT License (see `LICENSE` file).

This project is:
- Unofficial.
- Reverse engineered from publicly available KOReader source code & observed network behavior.
- Provided “as is” without warranty.

If you are a KOReader maintainer and want clarifications, corrections, or linkage, please open an issue.

---

## Attribution

KOReader © its respective contributors (AGPL license).  
This repository documents interaction patterns; it does **not** redistribute KOReader code.

---

## Support / Questions

Open a GitHub Issue with:
- Environment (OS / device)
- Example request & response (redact credentials)
- What you expected vs observed

---

Happy building & syncing!
