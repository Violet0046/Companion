/* Disposable, account-scoped copies. The server remains the source of truth. */
(() => {
  'use strict';
  const DB = 'tongxing-conversations', STORE = 'windows';
  const MAX_WINDOWS = 32, MAX_MESSAGES = 50, MAX_BYTES = 8 * 1024 * 1024;
  const MAX_RECORD_BYTES = 512 * 1024, MAX_AGE = 7 * 86400000;
  const memory = new Map(), revisions = new Map(), ownerEpochs = new Map();
  let database, queue = Promise.resolve();
  const keyFor = (owner, scope) => JSON.stringify([owner, scope]);
  const copy = value => value == null ? null : JSON.parse(JSON.stringify(value));
  const valid = row => row && row.version === 1 && Date.now() - row.savedAt < MAX_AGE;
  const bump = key => revisions.set(key, (revisions.get(key) || 0) + 1);

  function open() {
    if (database) return database;
    database = new Promise(resolve => {
      let settled = false;
      const finish = db => {
        if (settled) { db?.close(); return; }
        settled = true; clearTimeout(timer); resolve(db);
      };
      const timer = setTimeout(() => finish(null), 1500);
      try {
        if (!globalThis.indexedDB) { finish(null); return; }
        const request = indexedDB.open(DB, 1);
        request.onupgradeneeded = () => {
          const store = request.result.createObjectStore(STORE, {keyPath: 'key'});
          store.createIndex('owner', 'owner');
        };
        request.onerror = request.onblocked = () => finish(null);
        request.onsuccess = () => {
          const db = request.result;
          db.onversionchange = () => { db.close(); database = null; };
          finish(db);
        };
      } catch (_) { finish(null); }
    });
    return database;
  }

  function transaction(db, mode, action) {
    return new Promise(resolve => {
      try {
        const tx = db.transaction(STORE, mode);
        tx.oncomplete = () => resolve(true);
        tx.onabort = tx.onerror = () => resolve(false);
        action(tx.objectStore(STORE));
      } catch (_) { resolve(false); }
    });
  }

  // Serialize writes, deletes and logout cleanup. A late read cannot resurrect
  // a deleted entry; app request fences also prevent late network writes.
  function enqueue(action) {
    queue = queue.then(async () => {
      const db = await open();
      if (db) await action(db);
    }).catch(() => {});
    return queue;
  }

  function trim(rows, remove) {
    let bytes = 0, count = 0;
    rows.sort((a,b) => b.savedAt - a.savedAt).forEach(row => {
      bytes += row.bytes || 0;
      if (!valid(row) || ++count > MAX_WINDOWS || bytes > MAX_BYTES) remove(row.key);
    });
  }

  function peek(owner, scope) {
    const key = keyFor(owner, scope), row = memory.get(key);
    if (!valid(row)) { memory.delete(key); return null; }
    return copy(row.value);
  }

  async function get(owner, scope) {
    if (!owner) return null;
    const cached = peek(owner, scope);
    if (cached && Array.isArray(cached.messages)) return cached;
    const key = keyFor(owner, scope), revision = revisions.get(key), epoch = ownerEpochs.get(owner);
    await queue;
    const db = await open();
    if (!db) return null;
    let row;
    await transaction(db, 'readonly', store => {
      const request = store.get(key);
      request.onsuccess = () => { row = request.result; };
    });
    if (epoch !== ownerEpochs.get(owner)) return null;
    if (revision !== revisions.get(key)) return peek(owner, scope);
    if (!valid(row)) return null;
    memory.set(key, row);
    trim([...memory.values()].filter(r => r.owner === owner), k => memory.delete(k));
    return copy(row.value);
  }

  function put(owner, scope, patch) {
    if (!owner) return;
    const key = keyFor(owner, scope);
    const value = {...(peek(owner, scope) || {}), ...copy(patch)};
    if (Array.isArray(value.messages)) {
      const saved = value.messages.filter(m => Number.isSafeInteger(m.id) && !m.pending && !m.rejected && !m.localOnly);
      value.hasMore = !!value.hasMore || saved.length > MAX_MESSAGES;
      value.messages = saved.slice(-MAX_MESSAGES);
      // Trim whole older messages, never truncate the text of a message.
      while (value.messages.length && JSON.stringify(value).length * 2 > MAX_RECORD_BYTES) {
        value.messages.shift(); value.hasMore = true;
      }
      if (!value.messages.length && saved.length) value.messages=null;
    }
    const row = {key, owner, version: 1, savedAt: Date.now(), value};
    row.bytes = JSON.stringify(row).length * 2;
    bump(key); const revision = revisions.get(key); memory.set(key, row);
    trim([...memory.values()].filter(r => r.owner === owner), k => memory.delete(k));
    enqueue(db => transaction(db, 'readwrite', store => {
      // A list/profile refresh may arrive before this window is hydrated.
      // Merge in storage so a metadata-only write cannot erase its messages.
      const previous = store.get(key);
      previous.onsuccess = () => {
        const merged = {...row, value: {...(valid(previous.result) ? previous.result.value : {}), ...row.value}};
        merged.bytes = JSON.stringify(merged).length * 2;
        store.put(merged);
        if (revisions.get(key) === revision) {
          memory.set(key, merged);
          trim([...memory.values()].filter(r => r.owner === owner), k => memory.delete(k));
        }
        const request = store.index('owner').getAll(owner);
        request.onsuccess = () => trim(request.result, k => store.delete(k));
      };
    }));
  }

  function remove(owner, scope) {
    const key = keyFor(owner, scope);
    bump(key); memory.delete(key);
    return enqueue(db => transaction(db, 'readwrite', store => store.delete(key)));
  }

  function clear(owner) {
    if (!owner) return Promise.resolve();
    ownerEpochs.set(owner, (ownerEpochs.get(owner) || 0) + 1);
    for (const [key,row] of memory) if (row.owner === owner) { bump(key); memory.delete(key); }
    // Invalidate reads that have not yet populated memory, too.
    for (const key of revisions.keys()) if (JSON.parse(key)[0] === owner) bump(key);
    return enqueue(db => transaction(db, 'readwrite', store => {
      const request = store.index('owner').openCursor(owner);
      request.onsuccess = () => { const cursor = request.result; if (cursor) { cursor.delete(); cursor.continue(); } };
    }));
  }

  window.CompanionConversationCache = {peek, get, put, remove, clear};
})();
