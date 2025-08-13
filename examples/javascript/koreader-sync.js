/**
 * koreader-sync.js
 * Unofficial KOReader Sync Protocol reference client (Node.js / browser-compatible with minor tweaks).
 *
 * Features:
 *  - Auth test
 *  - Dual document ID strategies (filename / partial)
 *  - PUT & GET progress
 *  - Conflict resolution helper
 *
 * Usage (Node 18+):
 *   import { KOSyncClient } from './koreader-sync.js';
 *
 *   const client = new KOSyncClient({
 *     username: process.env.KOREADER_SYNC_USER,
 *     password: process.env.KOREADER_SYNC_PASS, // plaintext; MD5 done internally
 *     deviceName: 'JSReader',
 *     idMode: 'filename', // or 'partial'
 *   });
 *
 *   const ok = await client.testAuth();
 *   console.log('Auth:', ok);
 *
 * NOTE: For partial MD5, this implementation reads the whole file
 *       (Node fs). Streaming just the sample ranges would be more efficient.
 *
 * License: MIT
 */

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const BASE_URL = 'https://sync.koreader.rocks';
const ACCEPT_HEADER = 'application/vnd.koreader.v1+json';

function md5Hex(buf) {
  return crypto.createHash('md5').update(buf).digest('hex');
}

function passwordMD5(password) {
  return md5Hex(Buffer.from(password, 'utf8'));
}

function docIdFromFilename(filePath) {
  return md5Hex(Buffer.from(path.basename(filePath), 'utf8'));
}

function docIdPartialMD5(filePath) {
  const step = 1024;
  const size = 1024;
  const m = crypto.createHash('md5');
  const fd = fs.openSync(filePath, 'r');

  try {
    for (let i = -1; i <= 10; i++) {
      const offset = step << (2 * i);
      const buf = Buffer.alloc(size);
      const bytes = fs.readSync(fd, buf, 0, size, offset);
      if (bytes <= 0) break;
      m.update(buf.subarray(0, bytes));
    }
  } finally {
    fs.closeSync(fd);
  }
  return m.digest('hex');
}

function computePercentage(currentPage, totalPages) {
  if (totalPages <= 0) return 0.0;
  let pct = currentPage / totalPages;
  if (pct < 0) pct = 0;
  if (pct > 1) pct = 1;
  return pct;
}

export class KOSyncClient {
  /**
   * @param {Object} opts
   * @param {string} opts.username
   * @param {string} opts.password - plaintext
   * @param {string} [opts.deviceName="JSReader"]
   * @param {"filename"|"partial"} [opts.idMode="filename"]
   * @param {string} [opts.deviceId] - optional stable ID; generated if absent
   * @param {number} [opts.debounceSeconds=25]
   * @param {Function} [opts.fetchImpl=globalThis.fetch]
   */
  constructor(opts) {
    this.username = opts.username;
    this.passwordHash = passwordMD5(opts.password);
    this.deviceName = opts.deviceName || 'JSReader';
    this.idMode = opts.idMode || 'filename';
    this.deviceId = opts.deviceId || crypto.randomUUID().replace(/-/g, '').toUpperCase();
    this.debounceSeconds = opts.debounceSeconds ?? 25;
    this.fetchImpl = opts.fetchImpl || globalThis.fetch;

    // Local memory store
    this.records = new Map(); // docId -> { lastPushTs, page, totalPages }
  }

  headers(includeContent = false) {
    const h = {
      'Accept': ACCEPT_HEADER,
      'X-Auth-User': this.username,
      'X-Auth-Key': this.passwordHash,
    };
    if (includeContent) h['Content-Type'] = 'application/json';
    return h;
  }

  documentId(filePath) {
    if (this.idMode === 'partial') {
      return docIdPartialMD5(filePath);
    }
    return docIdFromFilename(filePath);
  }

  async testAuth() {
    const res = await this.fetchImpl(`${BASE_URL}/users/auth`, {
      method: 'GET',
      headers: this.headers(),
    });
    return res.status === 200;
  }

  async getProgress(filePath) {
    const docId = this.documentId(filePath);
    const res = await this.fetchImpl(`${BASE_URL}/syncs/progress/${docId}`, {
      method: 'GET',
      headers: this.headers(),
    });
    if (res.status === 404) return null;
    if (res.status === 401) throw new Error('Auth failed (401)');
    if (res.status !== 200) {
      console.warn('Unexpected GET status', res.status);
      return null;
    }
    return await res.json();
  }

  async putProgress({ filePath, currentPage, totalPages }) {
    const docId = this.documentId(filePath);
    const pct = computePercentage(currentPage, totalPages);
    const payload = {
      progress: String(currentPage),
      percentage: pct,
      device_id: this.deviceId,
      document: docId,
      device: this.deviceName,
    };
    const res = await this.fetchImpl(`${BASE_URL}/syncs/progress`, {
      method: 'PUT',
      headers: this.headers(true),
      body: JSON.stringify(payload),
    });
    if (res.status === 200) {
      this.records.set(docId, {
        lastPushTs: Date.now() / 1000,
        page: currentPage,
        totalPages,
      });
      return true;
    }
    if (res.status === 401) throw new Error('Auth failed (401)');
    console.warn('PUT failed', res.status, await res.text());
    return false;
  }

  async debouncedPut({ filePath, currentPage, totalPages, force = false, minPageDelta = 1 }) {
    const docId = this.documentId(filePath);
    const rec = this.records.get(docId) || { lastPushTs: 0, page: 0 };
    const now = Date.now() / 1000;

    if (!force) {
      if (now - rec.lastPushTs < this.debounceSeconds) return false;
      if (currentPage - rec.page < minPageDelta) return false;
    }
    return this.putProgress({ filePath, currentPage, totalPages });
  }

  /**
   * Conflict strategy: adopt remote if remote percentage ahead by > threshold.
   * Otherwise push local page.
   */
  async syncWithConflict({ filePath, currentPage, totalPages, adoptRemoteThreshold = 0.02 }) {
    const remote = await this.getProgress(filePath);
    const localPct = computePercentage(currentPage, totalPages);

    if (!remote) {
      await this.putProgress({ filePath, currentPage, totalPages });
      return currentPage;
    }

    const remotePct = remote.percentage;
    const delta = remotePct - localPct;
    let remotePage = parseInt(remote.progress, 10);
    if (Number.isNaN(remotePage)) remotePage = null;

    if (delta > adoptRemoteThreshold && remotePage !== null) {
      return remotePage;
    }
    // Otherwise affirm/push local
    await this.putProgress({ filePath, currentPage, totalPages });
    return currentPage;
  }
}

// ------------- Simple CLI ------------- //
if (import.meta.url === `file://${process.argv[1]}`) {
  (async () => {
    const [,, action, filePath, pageStr, totalStr] = process.argv;
    if (!process.env.KOREADER_SYNC_USER || !process.env.KOREADER_SYNC_PASS) {
      console.error("Set KOREADER_SYNC_USER & KOREADER_SYNC_PASS in env.");
      process.exit(1);
    }
    const client = new KOSyncClient({
      username: process.env.KOREADER_SYNC_USER,
      password: process.env.KOREADER_SYNC_PASS,
      idMode: process.env.KOREADER_ID_MODE || 'filename',
      deviceName: 'JSRefClient',
    });

    switch (action) {
      case 'auth': {
        console.log('Auth:', await client.testAuth());
        break;
      }
      case 'get': {
        const prog = await client.getProgress(filePath);
        console.log(prog || 'No remote progress.');
        break;
      }
      case 'put': {
        const page = Number(pageStr);
        const total = Number(totalStr);
        if (!Number.isFinite(page) || !Number.isFinite(total)) {
          console.error("Provide page & total numbers.");
          process.exit(1);
        }
        const ok = await client.putProgress({ filePath, currentPage: page, totalPages: total });
        console.log('PUT:', ok);
        break;
      }
      case 'sync': {
        const page = Number(pageStr);
        const total = Number(totalStr);
        const resolved = await client.syncWithConflict({ filePath, currentPage: page, totalPages: total });
        console.log('Resolved page:', resolved);
        break;
      }
      default:
        console.log(`Usage:
  node koreader-sync.js auth
  node koreader-sync.js get <filePath>
  node koreader-sync.js put <filePath> <page> <totalPages>
  node koreader-sync.js sync <filePath> <page> <totalPages>`);
    }
  })().catch(e => {
    console.error(e);
    process.exit(1);
  });
}
