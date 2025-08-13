# KOReader Sync Protocol – API Reference (Unofficial)

> Canonical endpoint & payload documentation for integrating third‑party reading / audiobook applications with KOReader’s sync server.  
> Status: Reverse‑engineered. Not affiliated with KOReader maintainers.  
> Version of documented server behavior: as observed 2025-08-13.

---

## Table of Contents
1. Conventions
2. Base URL & Versioning
3. Authentication
4. Data Model
5. Endpoints
   - 5.1 User Creation
   - 5.2 Authentication Check
   - 5.3 Upsert Progress
   - 5.4 Get Progress
6. Request & Response Schemas
7. Percentage & Progress Semantics (Normative)
8. Document Identification Modes
9. Error Handling & Status Codes
10. Rate Limiting & Client Throttling (Advised)
11. Security Considerations
12. Compatibility & Interop Notes
13. Extensibility Guidance
14. Change Tracking
15. Disclaimer

---

## 1. Conventions

| Term | Meaning |
|------|--------|
| MUST | Required for interoperable behavior. |
| SHOULD | Recommended unless a compelling reason exists. |
| MAY | Optional / discretionary. |
| Client | Your application integrating with KOReader sync. |
| Server | Public KOReader sync instance (`sync.koreader.rocks`). |

All JSON numbers are decimal. Timestamps are Unix epoch seconds (UTC).

---

## 2. Base URL & Versioning

| Aspect | Value |
|--------|-------|
| Base URL | `https://sync.koreader.rocks` |
| API Versioning | Via `Accept: application/vnd.koreader.v1+json` MIME |
| Content Type | `application/json` |

Clients MUST send `Accept: application/vnd.koreader.v1+json`.  
Server presently tolerates absence, but to future‑proof, include it.

---

## 3. Authentication

Stateless header-based scheme:

| Header | Description | Required |
|--------|-------------|----------|
| `X-Auth-User` | Username (plaintext) | Yes |
| `X-Auth-Key` | `MD5(password)` (lowercase hex) | Yes |
| `Accept` | Version MIME | Yes |
| `Content-Type` | `application/json` (for bodies) | Yes when body |

MD5 is not salted; confidentiality relies on TLS.

Password hashing example:
```bash
echo -n "mypassword" | md5sum
# -> 34819d7beeabb9260a5c854bc85b3e44
```

---

## 4. Data Model

### 4.1 Progress Resource

Represents *latest known reading position* for a (user, document) pair.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document` | string | Yes | Document ID (filename MD5 or partial MD5). |
| `progress` | string | Yes | Position marker (page number or xpointer-like path). |
| `percentage` | number | Yes | Float in [0.0, 1.0]; see Section 7. |
| `device_id` | string | Yes | Stable device identifier (client-chosen). |
| `device` | string | Yes | Human-friendly device name/model. |
| `timestamp` | integer | Server | Unix epoch (set by server on GET). |

Server stores one progress entry per `(user, document)`; PUT overwrites.

---

## 5. Endpoints

### 5.1 Create User

| Method | Path | Auth | Body | Returns |
|--------|------|------|------|---------|
| POST | `/users/create` | No | JSON | 201 or error |

**Request Body:**
```json
{
  "username": "alice",
  "password": "34819d7beeabb9260a5c854bc85b3e44"
}
```

**Responses:**
- 201 Created
- 400 Bad Request (username taken / invalid)
- 415 Unsupported Media Type (missing Content-Type)

---

### 5.2 Authentication Check

| Method | Path | Auth | Body |
|--------|------|------|------|
| GET | `/users/auth` | Yes | None |

**Success Response (200):**
```json
{ "authorized": "OK" }
```

**Failure:** 401 Unauthorized (empty or small JSON message).

---

### 5.3 Upsert Progress

| Method | Path | Auth | Body | Idempotent |
|--------|------|------|------|------------|
| PUT | `/syncs/progress` | Yes | JSON | Yes (last write wins) |

**Body (client supplied):**
```json
{
  "progress": "42",
  "percentage": 0.284,
  "device_id": "57F6829062A0403295432C1CD2CA1802",
  "document": "d1c83dca9ba4131e83e1a2cd9b16acf5",
  "device": "MyReaderApp"
}
```

**Responses:**
- 200 OK
- 400 Bad Request (malformed / missing)
- 401 Unauthorized

Server typically does not echo back the stored object (lean response).  
Clients SHOULD re‑GET only when necessary (avoid immediate GET for every PUT).

---

### 5.4 Get Progress

| Method | Path | Auth | Path Param | Returns |
|--------|------|------|------------|---------|
| GET | `/syncs/progress/{document_id}` | Yes | `document_id` | Progress JSON or 404 |

**Success (200):**
```json
{
  "device_id": "57F6829062A0403295432C1CD2CA1802",
  "progress": "42",
  "document": "d1c83dca9ba4131e83e1a2cd9b16acf5",
  "percentage": 0.284,
  "timestamp": 1755040495,
  "device": "MyReaderApp"
}
```

**Not Found (404):**
```json
{ "message": "Not found" }
```
(or empty body; implementation detail—handle both)

---

## 6. Request & Response Schemas

### 6.1 JSON Schema (Informal)

```json
{
  "ProgressUpsert": {
    "type": "object",
    "required": ["progress", "percentage", "device_id", "document", "device"],
    "properties": {
      "progress":   { "type": "string", "minLength": 1 },
      "percentage": { "type": "number", "minimum": 0, "maximum": 1 },
      "device_id":  { "type": "string", "minLength": 4, "maxLength": 128 },
      "document":   { "type": "string", "minLength": 8, "maxLength": 64 },
      "device":     { "type": "string", "minLength": 1, "maxLength": 64 }
    },
    "additionalProperties": false
  },
  "ProgressResponse": {
    "allOf": [
      { "$ref": "ProgressUpsert" },
      {
        "properties": {
          "timestamp": { "type": "integer", "minimum": 0 }
        },
        "required": ["timestamp"]
      }
    ]
  }
}
```

---

## 7. Percentage & Progress Semantics (Normative)

### 7.1 Percentage Definition

For KOReader (paged and reflowable EPUB):

```
percentage = current_virtual_page / total_virtual_pages
```

Edge conditions:
- If `total_virtual_pages <= 0`, value SHOULD default to `0.0`.
- Value MUST be clamped to `[0.0, 1.0]`.

Hidden flow mode (rare EPUB internal flows):
```
percentage = current_page_in_flow / total_pages_in_flow
```

Clients MAY ignore hidden flows and simply use main pagination if their engine has no concept of flows; KOReader users will still see approximately correct progress.

### 7.2 Progress Field

| Format | Example | Use Case |
|--------|---------|----------|
| Page number (string) | `"42"` | Simplicity; widely compatible. |
| Structural pointer | `"/body/DocFragment[12]/body/p[3]/text().57"` | High precision resume; KOReader native style. |

Clients lacking a DOM/XPointer engine SHOULD send page numbers only.

---

## 8. Document Identification Modes

Clients MUST allow selecting or automatically determining one of:

| Mode | Derivation | Pros | Cons |
|------|------------|------|------|
| Filename | `MD5(basename(file))` | Fast, simple | Collisions on duplicate names |
| Partial MD5 | Sampled content hash | Stable across renames; fewer collisions | File access needed; large sparse files corner cases |

Partial MD5 sampling offsets (bytes):
```
512, 2048, 8192, 32768, 131072, 524288,
2097152, 8388608, 33554432, 134217728,
536870912, 2147483648
```

Pseudo-code:
```python
offset = 1024 << (2*i)  # i in [-1..10]
```

Stop early if offset beyond EOF (no further samples).

---

## 9. Error Handling & Status Codes

| Code | Endpoint(s) | Meaning | Client Action |
|------|-------------|---------|---------------|
| 200 | GET /auth, PUT /progress, GET /progress | Success | Proceed |
| 201 | POST /users/create | User created | Store credentials |
| 400 | Any write | Malformed JSON / invalid field | Validate & retry |
| 401 | All (exc. create) | Bad credentials | Re-prompt user |
| 404 | GET /progress/{id} | No record for document | Initialize local |
| 415 | POST /users/create | Missing Content-Type | Add header |
| 429* | (Not observed) | Hypothetical rate limit | Backoff |
| 5xx | Any | Server issue | Retry with backoff |

`429` not currently observed, but design for it.

---

## 10. Rate Limiting & Client Throttling (Advised)

Server appears lightweight; polite clients SHOULD:

| Event | Action |
|-------|--------|
| Page turn | Do NOT sync each turn. |
| Significant progress (>=1 page delta or >=1% change) | Queue update. |
| Time debounce | Minimum 20–30s between pushes per document. |
| App background / suspend | Push pending progress immediately. |
| App resume / book open | GET remote progress. |

Recommended push logic (pseudo):
```python
if (now - last_push_ts) > 25 and (pages_advanced >= 1 or percent_delta >= 0.01):
    push()
```

---

## 11. Security Considerations

| Risk | Observation | Mitigation |
|------|-------------|------------|
| Credential replay | MD5 static token | Enforce HTTPS; user can rotate password. |
| Bruteforce | No documented rate limits | Add local exponential backoff on 401 loop. |
| Hash collisions | MD5 | Acceptable for non-sensitive progress; no integrity guarantee. |
| File enumeration | Document IDs not easily guessable (hash). | Use random-ish filenames if privacy needed. |
| Device spoofing | `device_id` arbitrary | Display and optionally warn on unknown devices in UI. |

---

## 12. Compatibility & Interop Notes

| Scenario | Recommendation |
|----------|----------------|
| Multiple devices, mixed modes | Standardize to filename mode for library with curated file names; or partial MD5 if filenames unstable. |
| Audiobook ↔ Text sync | Map audiobook time to nearest virtual page; update both `progress` (page) and `percentage`. |
| Re-pagination after font change | Recompute total pages, recalc `percentage`; keep original structural bookmark (if stored). |
| EPUB vs PDF | Uniform page arithmetic; treat PDF page as virtual page. |

---

## 13. Extensibility Guidance

Potential future (non-official) additions (namespaced to avoid conflicts):

| Field | Purpose |
|-------|---------|
| `_meta_client_version` | Client version string for debugging. |
| `_meta_capabilities` | List: `["xpointer","audiobook-time"]`. |
| `_a11y_voice_rate` | If syncing TTS states. |

Clients MUST NOT assume server preserves unknown fields (act as write-only hints).

---

## 14. Change Tracking

| Date | Change |
|------|--------|
| 2025-08-13 | Initial publication of API reference. |

---

## 15. Disclaimer

This specification is unofficial. All behaviors described are based on inspection of KOReader source and live interaction with the public sync server. Protocol may change without notice. Use at your own risk.

---

## Appendix A: Minimal Working cURL Session

```bash
USER=alice
PASS_HASH=$(echo -n "mypassword" | md5sum | cut -d' ' -f1)
DOC_ID=$(echo -n "Novel.epub" | md5sum | cut -d' ' -f1)

# Auth test
curl -H "Accept: application/vnd.koreader.v1+json" \
     -H "X-Auth-User: $USER" \
     -H "X-Auth-Key: $PASS_HASH" \
     https://sync.koreader.rocks/users/auth

# Upsert progress
curl -X PUT https://sync.koreader.rocks/syncs/progress \
  -H "Accept: application/vnd.koreader.v1+json" \
  -H "Content-Type: application/json" \
  -H "X-Auth-User: $USER" \
  -H "X-Auth-Key: $PASS_HASH" \
  -d "{\"progress\":\"17\",\"percentage\":0.1133,\"device_id\":\"DEV1234ABCDE\",\"document\":\"$DOC_ID\",\"device\":\"MyReaderApp\"}"

# Fetch progress
curl -H "Accept: application/vnd.koreader.v1+json" \
     -H "X-Auth-User: $USER" \
     -H "X-Auth-Key: $PASS_HASH" \
     https://sync.koreader.rocks/syncs/progress/$DOC_ID
```

---

## Appendix B: Reference Percentage Logic Snippet (Python)

```python
def compute_percentage(current_page: int, total_pages: int) -> float:
    if total_pages <= 0:
        return 0.0
    pct = current_page / total_pages
    if pct < 0: return 0.0
    if pct > 1: return 1.0
    return pct
```

---

Questions? Open an issue with reproducible steps and sample payloads.
