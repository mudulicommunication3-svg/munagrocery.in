/* munnamobile :: js/data-helpers.js  (extracted from index.html lines 4357-4652)
   session/address/order helpers + local order caches */
    function getTgUser() { try { return (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) ? tg.initDataUnsafe.user : null; } catch (e) { return null; } }
    function uidKey(base) { var u = getTgUser(); return base + '_' + (u && u.id ? u.id : 'anon'); }
    // =========================================================================
    // ADDRESSES — IN-MEMORY ONLY (no localStorage persistence).
    // `_addresses` is populated by syncAddressesFromSheet() (GET_ADDRESSES on
    // the Apps Script backend). The legacy localStorage 'munna_addresses' key
    // is migrated once on first read, then purged, so it is never trusted
    // after that.
    // =========================================================================
    var _addresses = [];
    var _addressesMigrated = false;
    function getAddresses() {
      if (!_addressesMigrated) {
        _addressesMigrated = true;
        try {
          var legacy = JSON.parse(localStorage.getItem(uidKey('munna_addresses')) || '[]') || [];
          if (legacy.length) _addresses = legacy;
        } catch (e) {}
        try { localStorage.removeItem(uidKey('munna_addresses')); } catch (e2) {}
      }
      return _addresses;
    }
    function setAddresses(a) { _addresses = a || []; }
    // --- UNIFIED ORDER MODEL (same format as the Telegram bot's Google Sheet) ---
    // Orders tab: Timestamp | Order ID | Customer Name | Phone | Address |
    // Items | Total | Channel | Telegram User ID | Status | Status Msg ID |
    // Location | Payment  (see db.ORDERS_HEADERS in the bot)
    var ORDER_STATUSES = ['pending', 'accepted', 'packing', 'out_for_delivery', 'delivered', 'cancelled', 'rejected', 'expired / cancelled'];
    var STATUS_STAGE = { pending: 1, accepted: 2, packing: 3, out_for_delivery: 4, delivered: 5, cancelled: 5, rejected: 5, 'expired / cancelled': 5 };
    var STAGE_STATUS = ['pending', 'accepted', 'packing', 'out_for_delivery', 'delivered'];

    // Order id shaped exactly like the bot's db.make_order_id(): MUN-YYYYMMDD-XXXXXX
    function makeOrderId() {
      var d = new Date(), p = function (n) { return String(n).padStart(2, '0'); };
      var stamp = '' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate());
      var chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789', rand = '';
      for (var i = 0; i < 6; i++) rand += chars.charAt(Math.floor(Math.random() * chars.length));
      return 'MUN-' + stamp + '-' + rand;
    }
    // Timestamp shaped like the bot's "%Y-%m-%d %H:%M:%S"
    function sheetTimestamp(d) {
      var p = function (n) { return String(n).padStart(2, '0'); };
      return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
        p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    }
    // Items text shaped like the bot: "Oil x2 @ Rs.340; Salt x1 @ Rs.25"
    function itemsToText(lines) {
      return (lines || []).map(function (l) {
        return l.name + ' x' + (parseInt(l.qty, 10) || 1) + ' @ Rs.' + (parseFloat(l.price) || 0);
      }).join('; ');
    }
    function parseItemsText(txt) {
      if (Array.isArray(txt)) return txt;
      var out = [];
      String(txt || '').split(';').forEach(function (seg) {
        seg = seg.trim();
        if (!seg) return;
        var m = seg.match(/^(.*?)\s*[xX]\s*(\d+)\s*@\s*Rs\.?\s*(.+)$/);
        if (m) out.push({ name: m[1].trim(), qty: parseInt(m[2], 10) || 1, price: parseFloat(m[3]) || 0 });
        else out.push({ name: seg, qty: 1, price: 0 });
      });
      return out;
    }
    // Accepts: Sheet rows (bot), our unified objects, and old local orders.
    // Every order reaches ONE shape, so the UI never cares where it came from.
    function normalizeOrder(o) {
      if (!o || typeof o !== 'object') return null;
      var id = String(o.order_id || o.id || '').trim();
      if (!id) return null;
      var itemsRaw = (o.items !== undefined && o.items !== null) ? o.items : (o.items_text || '');
      var lines = (Array.isArray(itemsRaw) && itemsRaw.length && typeof itemsRaw[0] === 'object' && itemsRaw[0] !== null)
        ? itemsRaw.map(function (it) {
            return { ean: it.ean || '', name: it.name || 'Item', qty: parseInt(it.qty, 10) || 1, price: parseFloat(it.price) || 0 };
          })
        : parseItemsText(itemsRaw);
      var status = String(o.status || '').trim();
      if (ORDER_STATUSES.indexOf(status) < 0) {
        var stageN = parseInt(o.stage, 10);
        status = (stageN >= 1 && stageN <= 5) ? STAGE_STATUS[stageN - 1] : 'pending';
      }
      var lat = o.lat, lng = o.lng;
      if ((lat === undefined || lat === '' || lat === null) && o.location) {
        var locParts = String(o.location).split(',');
        lat = parseFloat(locParts[0]);
        lng = parseFloat(locParts[1]);
      }
      var ts = o.timestamp || o.date || '';
      if (ts.indexOf('T') >= 0) ts = ts.replace('T', ' ').substring(0, 19);
      var name = o.customer_name || o.name || '';
      // Store Pickup fields (DeliveryType | PickupSlot | PickupExpiryTime).
      var deliveryType = String(o.delivery_type || o.deliveryType || '').trim();
      var pickupSlot = String(o.pickup_slot || o.pickupSlot || '').trim();
      if (!deliveryType && pickupSlot) deliveryType = 'Store Pickup';
      if (deliveryType === 'Store Pickup' && !pickupSlot) {
        var slotMatch = String(itemsRaw || '').match(/Store Pickup\s*\(([^)]+)\)/i);
        if (slotMatch) pickupSlot = String(slotMatch[1]).trim();
      }
      var pickupExpiry = String(o.pickup_expiry_time || o.pickupExpiryTime || o.pickup_expiry || '').trim();
      if (deliveryType === 'Store Pickup' && !pickupExpiry && pickupSlot) {
        var slotMins = pickupSlot === '30 min' ? 30 : (pickupSlot === '2 hour' ? 120 : 60);
        pickupExpiry = sheetTimestamp(new Date(Date.parse(ts.replace(' ', 'T')) + slotMins * 60000));
      }
      return {
        id: id, order_id: id, date: ts, timestamp: ts,
        name: name, customer_name: name,
        phone: o.phone || '', address: o.address || '',
        lat: lat, lng: lng,
        location: o.location || ((lat !== undefined && lat !== '' && !isNaN(lat)) ? (lat + ',' + lng) : ''),
        payment: o.payment || 'COD',
        delivery_type: deliveryType,
        pickup_slot: pickupSlot,
        pickup_expiry_time: pickupExpiry,
        stock_restored: String(o.stock_restored || '').trim(),
        admin_alert_status: String(o.admin_alert_status || o.adminAlertStatus || '').trim(),
        total: parseFloat(String(o.total || 0).replace(/,/g, '')) || 0,
        items: lines,
        items_text: (typeof itemsRaw === 'string' && itemsRaw) ? itemsRaw : itemsToText(lines),
        stage: STATUS_STAGE[status] || 1,
        status: status,
        channel: o.channel || 'webapp',
        telegram_user_id: o.telegram_user_id || '',
        status_msg_id: o.status_msg_id || ''
      };
    }
    // =========================================================================
    // ORDERS — SHEET ONLY. `_allOrders` holds ONLY rows verified to exist in
    // the Google Sheet (fetched via GET_ORDERS, or confirmed by the read-back
    // inside placeMiniOrder after create_order). NOTHING unverified may enter.
    // Legacy localStorage 'MUNA_ALL_ORDERS' is purged on first read and never
    // trusted afterwards.
    // =========================================================================
    var _allOrders = [];
    var _ordersMigrated = false;
    // Persistent Orders cache: `_allOrders` must survive a page refresh so the
    // Orders tab is NEVER blank while the background Sheet pull re-validates.
    // Keys: MUNA_ALL_ORDERS_CACHE (verified Sheet rows, newest first),
    //       MUNA_ALL_ORDERS_PHONE (the phone scope the cache was saved under).
    var ORDERS_CACHE_KEY = 'MUNA_ALL_ORDERS_CACHE';
    var ORDERS_CACHE_PHONE_KEY = 'MUNA_ALL_ORDERS_PHONE';
    function loadOrdersCache_(scopePhone) {
      try {
        var savedPhone = String(localStorage.getItem(ORDERS_CACHE_PHONE_KEY) || '');
        // The cache is account-scoped: ignore another account's rows.
        if (savedPhone && scopePhone && savedPhone !== scopePhone) return false;
        var raw = localStorage.getItem(ORDERS_CACHE_KEY);
        if (!raw) return false;
        var list = JSON.parse(raw) || [];
        if (!Array.isArray(list) || !list.length) return false;
        _allOrders = list.map(normalizeOrder).filter(Boolean);
        return true;
      } catch (e) { return false; }
    }
    function saveOrdersCache_() {
      try {
        if (!_allOrders.length) return; // never wipe a good cache with []
        localStorage.setItem(ORDERS_CACHE_KEY, JSON.stringify(_allOrders.slice(0, 200)));
        var su = null;
        try { su = getSessionUser(); } catch (e2) {}
        localStorage.setItem(ORDERS_CACHE_PHONE_KEY,
          (su && su.phone) ? String(su.phone).replace(/\D/g, '').slice(-10) : '');
      } catch (e) {}
    }
    function currentOrdersScopePhone_() {
      var su = null;
      try { su = getSessionUser(); } catch (e) {}
      return (su && su.phone) ? String(su.phone).replace(/\D/g, '').slice(-10) : '';
    }
    // Orders cache (localStorage persistence). On a cold boot (data cleared,
    // refreshed, app reopened), the Sheet fetch happens async in the background,
    // but the USER MUST SEE their orders immediately. So getMyOrders() forces
    // ONE quick sync call on first read (non-blocking) so the Orders tab is
    // NEVER left blank just because localStorage was cleared.
    function getMyOrders() {
      // One-time purge of the legacy cache. NOTE: legacy rows are NOT loaded
      // into memory anymore — the Sheet re-fetch (already running on boot)
      // repopulates history from the server within seconds.
      if (!_ordersMigrated) {
        _ordersMigrated = true;
        try { localStorage.removeItem('MUNA_ALL_ORDERS'); } catch (e) {}
      }
      // Boot on a fresh page / after a refresh: hydrate the persisted cache so
      // the Orders tab never flashes EMPTY while the first sync is in flight.
      if (!_allOrders.length) loadOrdersCache_(currentOrdersScopePhone_());
      // If the local cache is EMPTY (user cleared app data / refreshed cold),
      // STILL DO NOT SHOW A BLANK ORDERS TAB: do one quick non-blocking
      // server pull so the Orders tab shows the orders that ARE in the Sheet.
      // This is the fix for "after clearing app data the Orders tab stays empty".
      if (!_allOrders.length) {
        try {
          var su = null;
          try { su = getSessionUser(); } catch (eQ) {}
          var sessPhone = (su && su.phone) ? String(su.phone).replace(/\D/g, '').slice(-10) : '';
          var url = sheetHookUrl('action=GET_ORDERS' + (sessPhone ? '&phone=' + encodeURIComponent(sessPhone) : '') + '&_=' + Date.now());
          if (url) {
            var _coldCtrl = null, _coldTimer = null;
            try {
              _coldCtrl = new AbortController();
              _coldTimer = setTimeout(function () { try { _coldCtrl.abort(); } catch (e) {} }, 8000);
            } catch (eT) {}
            fetch(url, { cache: 'no-store', signal: _coldCtrl ? _coldCtrl.signal : undefined })
              .then(function (res) { if (_coldTimer) clearTimeout(_coldTimer); if (!res.ok) return; return res.json(); })
              .then(function (data) {
                var remote = (data && Array.isArray(data.orders)) ? data.orders : [];
                if (sessPhone) {
                  remote = remote.filter(function (r) {
                    return String(r.phone || '').replace(/\D/g, '').slice(-10) === sessPhone;
                  });
                }
                if (remote.length) {
                  setMyOrders(remote);
                  try { renderOrders(); } catch (eR) {}
                }
              })
              .catch(function () { if (_coldTimer) clearTimeout(_coldTimer); });
          }
        } catch (eC) {}
      }
      return _allOrders.map(normalizeOrder).filter(Boolean);
    }
    function setMyOrders(o) {
      _allOrders = (o || []).map(normalizeOrder).filter(Boolean);
      saveOrdersCache_();
    }

    // Orders whose server write could not be VERIFIED live ONLY here (save
    // retry queue). NEVER rendered, NEVER merged into history: the Orders
    // screen reads getMyOrders() (Sheet rows) exclusively.
    var PENDING_SHEET_ORDERS_KEY = 'MUNA_PENDING_SHEET_ORDERS';
    function getPendingSheetOrders_() {
      try { return JSON.parse(localStorage.getItem(PENDING_SHEET_ORDERS_KEY) || '[]') || []; }
      catch (e) { return []; }
    }
    function setPendingSheetOrders_(list) {
      try { localStorage.setItem(PENDING_SHEET_ORDERS_KEY, JSON.stringify(list || [])); } catch (e) {}
    }
    // Queue an order whose create_order write FAILED for an automatic retry.
    // The SAME order_id is resent later (server is idempotent — no duplicate).
    // Resend-only outbox: never rendered as history, never counted.
    function markPendingSheetOrder_(orderObj) {
      try {
        if (!orderObj || !orderObj.order_id) return;
        var pending = getPendingSheetOrders_().filter(function (p) {
          return String((p && p.order_id) || '') !== String(orderObj.order_id);
        });
        pending.unshift(orderObj);
        setPendingSheetOrders_(pending.slice(0, 25));
        renderAdminSyncStatus_('Sheet save pending — retry from Orders', false);
      } catch (e) {}
    }
    // Retry every queued order (same order_id -> server updates in place).
    async function resendPendingSheetOrders_() {
      var pending = getPendingSheetOrders_();
      if (!pending.length) return 0;
      var sent = 0;
      for (var i = 0; i < pending.length; i++) {
        var one = pending[i];
        if (!one || !one.order_id) continue;
        try {
          var r = await postToSheet({ action: 'create_order', order: one });
          var body = (r && r.json) || {};
          if (r && r.ok && (body.status === 'success' || body.type === 'order_recorded' || body.type === 'order_updated')) {
            clearPendingSheetOrder_(one.order_id);
            sent++;
          }
        } catch (e) {}
      }
      if (sent) {
        try { await syncOrdersFromSheet(true); } catch (e2) {}
        renderAdminSyncStatus_('Resent ' + sent + ' pending order(s)', true);
      }
      return sent;
    }
    function clearPendingSheetOrder_(orderId) {
      try {
        if (!orderId) return;
        setPendingSheetOrders_(getPendingSheetOrders_().filter(function (p) {
          return String((p && p.order_id) || '') !== String(orderId);
        }));
      } catch (e) {}
    }
    async function verifyOrderOnSheet_(orderId) {
      if (!orderId) return false;
      try {
        var url = sheetHookUrl('action=GET_ORDERS&_=' + Date.now());
        if (!url) return false;
        var res = await fetch(url, { cache: 'no-store' });
        var data = await res.json();
        var found = ((data && data.orders) || []).some(function (r) {
          return String((r && (r.order_id || r.id)) || '') === String(orderId);
        });
        if (found) clearPendingSheetOrder_(orderId);
        return found;
      } catch (e) { return false; }
    }


