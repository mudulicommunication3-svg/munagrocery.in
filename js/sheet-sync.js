/* munnamobile :: js/sheet-sync.js  (extracted from index.html lines 4827-5050)
   pull orders/config/banners/addresses from Sheet */
    async function syncOrdersFromSheet(forceAdminSync) {
      // Account-wise: query with the logged-in customer's 10-digit phone so a
      // background refresh pulls THEIR orders on every device (the backend
      // filters server-side; the client filter below guarantees it anywhere).
      // Admin devices always fetch the FULL shared store order list so a
      // customer's new order appears in the admin panel immediately.
      var adminSync = forceAdminSync === true || isAdmin_();
      var su = null;
      try { su = getSessionUser(); } catch (e) {}
      var sessPhone = (!adminSync && su && su.phone)
        ? String(su.phone).replace(/\D/g, '').slice(-10) : '';
      var url = sheetHookUrl('action=GET_ORDERS' + (sessPhone ? '&phone=' + encodeURIComponent(sessPhone) : '') + '&_=' + Date.now());
      if (!url) {
        if (adminSync) {
          _lastSyncReason = 'Webhook missing';
          renderAdminSyncStatus_(_lastSyncReason, false);
        }
        return false;
      }
      try {
        var ctrl = new AbortController();
        var to = setTimeout(function () { try { ctrl.abort(); } catch (e) {} }, 8000);
        var res = await fetch(url, { cache: 'no-store', signal: ctrl.signal });
        clearTimeout(to);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        var data = await res.json();
        var remote = (data && Array.isArray(data.orders)) ? data.orders : [];
        if (sessPhone) {
          remote = remote.filter(function (r) {
            return String(r.phone || '').replace(/\D/g, '').slice(-10) === sessPhone;
          });
        }
        if (adminSync && !remote.length) {
          // The admin panel must not treat an empty response as a healthy
          // sync. Report it, then immediately verify with the public GET call.
          _lastSyncReason = 'Server returned 0 orders';
          renderAdminSyncStatus_(_lastSyncReason, false);
          try {
            var check = await window.munnaReadonlyGet('action=GET_ORDERS');
            if (check && check.ok && check.json && Array.isArray(check.json.orders) && check.json.orders.length) {
              remote = check.json.orders;
              _lastSyncReason = '';
              renderAdminSyncStatus_('Server verified — merging orders', true);
            } else {
              _lastSyncReason = 'GET_ORDERS server error' + (check && check.status ? ' (' + check.status + ')' : '');
              renderAdminSyncStatus_(_lastSyncReason, false);
            }
          } catch (verifyError) {
            _lastSyncReason = 'GET_ORDERS verification failed';
            renderAdminSyncStatus_(_lastSyncReason, false);
          }
        }
        if (!remote.length) {
          // An EMPTY success response must NEVER wipe an already-populated
          // list (quota / lag / partially-cleared tab). Keep the current list
          // + cache; the next poll re-validates. Only genuinely-empty accounts
          // show the "no orders" empty state (they had nothing to lose).
          if (adminSync) return false;
          if (getMyOrders().length) {
            setTimeout(function () { syncOrdersFromSheet(); }, 8000);
            return false;
          }
          return false;
        }
        var normalized = remote.map(normalizeOrder).filter(Boolean);
        if (adminSync) {
          // Admin: MERGE into the current list so the panel keeps any
          // not-yet-persisted local edits (reject/qty changes) across polls.
          var byId = {};
          getMyOrders().forEach(function (o) { byId[o.order_id] = o; });
          normalized.forEach(function (n) { byId[n.order_id] = n; });
          setMyOrders(Object.keys(byId).map(function (k) { return byId[k]; })
            .sort(function (a, b) { return String(b.date).localeCompare(String(a.date)); }));
          _lastSyncReason = '';
          _lastServerSyncOkAt = Date.now();
          renderAdminSyncStatus_('Live orders synced', true);
        } else {
          // Customer: REPLACE the in-memory list with the server result.
          // The sheet is the single source of truth — no local cache merge,
          // so a stale list can never mask a newer server status.
          setMyOrders(normalized);
        }
        var os = document.getElementById('ordersScreen');
        if (os && !os.classList.contains('hidden')) renderOrders();
        var ad = document.getElementById('adminDashboard');
        if (ad && !ad.classList.contains('hidden')) {
          renderAdminOrders();
          var ut = document.getElementById('admTab-users');
          if (ut && !ut.classList.contains('hidden')) renderAdminUsers();
        }
        return true;
      } catch (e) {
        if (adminSync) {
          _lastSyncReason = 'Server request failed — retrying';
          renderAdminSyncStatus_(_lastSyncReason, false);
          // One public read-only retry immediately repairs the panel when the
          // first POST/proxy-style response is blocked or times out.
          try {
            var retry = await window.munnaReadonlyGet('action=GET_ORDERS');
            if (retry && retry.ok && retry.json && Array.isArray(retry.json.orders) && retry.json.orders.length) {
              var retryBy = {};
              getMyOrders().forEach(function (o) { retryBy[o.order_id] = o; });
              retry.json.orders.forEach(function (r) {
                var n2 = normalizeOrder(r);
                if (n2) retryBy[n2.order_id] = n2;
              });
              var retryMerged = Object.keys(retryBy).map(function (k) { return retryBy[k]; })
                .sort(function (a, b) { return String(b.date).localeCompare(String(a.date)); });
              setMyOrders(retryMerged);
              _lastSyncReason = '';
              _lastServerSyncOkAt = Date.now();
              renderAdminSyncStatus_('Live orders synced (retry)', true);
              var retryDash = document.getElementById('adminDashboard');
              if (retryDash && !retryDash.classList.contains('hidden')) renderAdminOrders();
              return true;
            }
          } catch (retryError) {}
        } else {
          // CUSTOMER path: a failed / timed-out pull must never leave the
          // Orders tab EMPTY — re-sync shortly so a single 8 s timeout or
          // quota blip self-heals instead of blanking the screen.
          setTimeout(function () { syncOrdersFromSheet(); }, 10000);
        }
        return false;
      }
    }
    // --- SHARED ADMIN SETTINGS (Config tab) ---
    // Admin sets once -> EVERY customer sees it. Banners live here now
    // (Sheet wins; localStorage is only an offline cache).
    function pushConfigToSheet(key, value) {
      // Returns false when no webhook is configured, so callers can warn.
      if (!key) return false;
      var hook = window.munnaHookBase();
      if (!hook) return false;
      try {
        fetch(hook, {
          method: 'POST',
          mode: 'no-cors',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'SAVE_CONFIG', key: key, value: value })
        });
        return true;
      } catch (e) { return false; }
    }
    async function syncBannersFromSheet() {
      // Cache-buster: Apps Script can serve a stale
      // config for minutes. The timestamp makes EVERY poll a fresh read so a
      // newly-set admin banner appears on customer phones within ~2 minutes.
      var url = sheetHookUrl('action=GET_CONFIG&_=' + Date.now());
      if (!url) return false;
      try {
        var res = await fetch(url);
        var data = await res.json();
        var cfg = (data && data.config) || {};
        // ALWAYS apply shop settings + trending BEFORE banners, so the
        // trending strip / delivery / coupon update on EVERY poll.
        applyShopConfig_(cfg);
        cacheShopConfig_();
        if (cfg.banners) {
          var shared = [];
          try { shared = JSON.parse(cfg.banners) || []; } catch (e) { shared = []; }
          if (Array.isArray(shared) && shared.length) {
            // Shared banners REPLACE everything — old local defaults are
            // wiped so they can never scroll again on any device.
            banners = shared.slice(0, 5);
            try {
              localStorage.setItem('SHOP_BANNERS', JSON.stringify(banners));
              localStorage.setItem('SHOP_BANNERS_SHARED', '1');
            } catch (e) {}
            initCarousel();
            var ad = document.getElementById('adminDashboard');
            if (ad && !ad.classList.contains('hidden')) renderAdminBanners();
            return true;
          }
        }
      } catch (e) {}
      return false;
    }
    function pushBannersToSheet() {
      // True = POST queued to the Sheet; false = no webhook (caller warns).
      return pushConfigToSheet('banners', JSON.stringify(banners.slice(0, 5))) !== false;
    }

    // Pull the customer's saved addresses from the Sheet (Addresses tab).
    // Works for BOTH Telegram users (user_id) and phone-only web sessions
    // (phone param). The server result REPLACES the in-memory list so the
    // sheet stays the single source of truth (see getAddresses/setAddresses).
    async function syncAddressesFromSheet() {
      var u = getTgUser();
      var s = null;
      try { s = getSessionUser(); } catch (e) {}
      var userId = (u && u.id) ? String(u.id) : '';
      var phone = (s && s.phone) ? String(s.phone).replace(/\D/g, '').slice(-10) : '';
      if (!userId && !phone) return false;
      var qs = 'action=GET_ADDRESSES';
      if (userId) qs += '&user_id=' + encodeURIComponent(userId);
      if (phone) qs += '&phone=' + encodeURIComponent(phone);
      var url = sheetHookUrl(qs);
      if (!url) return false;
      try {
        var res = await fetch(url, { cache: 'no-store' });
        var data = await res.json();
        var remote = (data && Array.isArray(data.addresses)) ? data.addresses : [];
        if (!remote.length) return false;
        // Replace the in-memory list with the server's saved addresses,
        // normalized into the client's { id, name, phone, address, isDefault } shape.
        var local = remote.map(function (a, i) {
          return {
            id: 'A' + i + '_' + Date.now(),
            name: a.name || '',
            phone: a.phone || '',
            address: a.address || '',
            isDefault: false
          };
        });
        if (local.length) local[0].isDefault = true;
        setAddresses(local);
        var al = document.getElementById('addressScreen');
        if (al && !al.classList.contains('hidden')) renderAddressList();
        prefillOrderForm();
        return true;
      } catch (e) { return false; }
    }

