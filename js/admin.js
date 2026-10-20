/* munnamobile :: js/admin.js  (extracted from index.html lines 2585-3438)
   admin auth, dashboard, orders, 15s ring, polling, users */
    function isAdmin_() { try { return localStorage.getItem(ADMIN_SESSION_KEY) === '1'; } catch (e) { return false; } }
    function setAdminSession_(on) {
      try { if (on) localStorage.setItem(ADMIN_SESSION_KEY, '1'); else localStorage.removeItem(ADMIN_SESSION_KEY); } catch (e) {}
      renderProfile(); // toggles the Admin Panel tile + portal buttons
    }

    // A REAL (readable) POST — postToSheet() now uses the readable path too,
    // but this helper remains for auth calls that need the same JSON shape.
    // Falls back to a no-cors fire-and-forget only if the readable fetch fails.
    // adminFetchHasHook_() is always true now (built-in default webhook) —
    // kept so callers don't break.
    function adminFetchHasHook_() {
      return true;
    }
    async function adminFetchJson_(payload) {
      var hook = window.munnaHookBase();
      if (!hook) return null;
      // CORS-SIMPLE content-type (application/json) => NO preflight, so this
      // works even where Apps Script's OPTIONS has no allow-origin header
      // (the text/plain path could be blocked by the browser preflight).
      try {
        var res = await fetch(hook, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json;charset=utf-8' },
          body: JSON.stringify(payload)
        });
        return await res.json();
      } catch (e) {
        // Fallback: legacy text/plain (some deployments prefer it).
        try {
          var res2 = await fetch(hook, {
            method: 'POST',
            headers: { 'Content-Type': 'text/plain;charset=utf-8' },
            body: JSON.stringify(payload)
          });
          return await res2.json();
        } catch (e2) { return null; }
      }
    }

    function openAdminModal() {
      // Already logged in on this device -> straight to the dashboard.
      if (isAdmin_()) { closeAdminPinModal(); openAdminDashboard_(); return; }
      document.getElementById('adminPinModal').classList.remove('hidden');
      var hint = document.getElementById('adminLoginHint');
      if (hint) { hint.classList.add('hidden'); hint.innerText = ''; }
      var u = document.getElementById('adminPinInput');
      if (u) { u.value = ''; u.focus(); }
      var p = document.getElementById('adminPassInput');
      if (p) p.value = '';
      if (!(history.state && history.state.modal === 'admin_pin')) history.pushState({ modal: 'admin_pin' }, '', window.location.href);
    }
    function closeAdminPinModal() { document.getElementById('adminPinModal').classList.add('hidden'); }

    // Verify 10-digit username + password.
    // Priority 1: local default 1111111111/1111111111 — works EVEN WITH NO
    // webhook (blank browser, no Google Sheet link, no Python, no PC).
    // Priority 2: Sheet webhook check_admin_auth (when webhook IS configured).
    async function verifyAdminPin() {
      var uEl = document.getElementById('adminPinInput');
      var pEl = document.getElementById('adminPassInput');
      var hint = document.getElementById('adminLoginHint');
      var u = String(uEl ? uEl.value : '').replace(/\D/g, '');
      var p = String(pEl ? pEl.value : '').replace(/\D/g, '');
      function fail(msg) {
        if (hint) { hint.classList.remove('hidden'); hint.innerText = '❌ ' + msg; }
      }
      if (u.length !== 10 || p.length !== 10) { fail('Username ଓ Password ଦୁଇଟି 10-ଅଙ୍କ ହେବା ଉଚିତ୍।'); return; }
      // --- Step 1: try the webhook (Sheet) when a webhook URL is saved ---
      var d = await adminFetchJson_({ action: 'check_admin_auth', username: u, password: p });
      var ok = d && (d.ok === true || d.status === 'success');
      if (ok) {
        setAdminSession_(true);
        closeAdminPinModal();
        openAdminDashboard_();
        startAdminOrderPolling_();
        return;
      }
      // Webhook OK but rejected? Then the Sheet says NO — respect it.
      if (d !== null) {
        fail((d && d.error) || 'ଭୁଲ୍ Username / Password!');
        return;
      }
      // --- Step 2: NO webhook configured -> local default check only ---
      // d is null = no webhook URL saved in this browser. Accept the built-in
      // default so the admin can get INSIDE and paste the webhook in Sheets tab.
      if (u === '1111111111' && p === '1111111111') {
        setAdminSession_(true);
        closeAdminPinModal();
        openAdminDashboard_();
        setTimeout(function () {
          try { alert('✅ Default login (1111111111)। ଏବେ Admin → Sheets ଟ୍ୟାବ୍ ରେ Webhook URL ଲଗାନ୍ତୁ, ତା\'ପରେ Sync ଚାପନ୍ତୁ।'); } catch (e) {}
        }, 400);
        return;
      }
      fail('ଭୁଲ୍ Username / Password! (Default: 1111111111 / 1111111111, କିମ୍ବା Sheet webhook ଲଗାନ୍ତୁ)');
    }

    // Change Admin Password (Sheet: update_admin_password).
    // Pure Apps Script save — no Python, no .env, no PC file needed.
    // A blank "new username" keeps the old one.
    async function changeAdminPassword() {
      var cur = String(((document.getElementById('admCurPass') || {}).value) || '').replace(/\D/g, '');
      var nw = String(((document.getElementById('admNewPass') || {}).value) || '').replace(/\D/g, '');
      var nu = String(((document.getElementById('admNewUser') || {}).value) || '').replace(/\D/g, '');
      var hint = document.getElementById('admPassHint');
      function fail(msg) { if (hint) { hint.classList.remove('hidden'); hint.style.color = ''; hint.innerText = '❌ ' + msg; } }
      function okMsg(msg) { if (hint) { hint.classList.remove('hidden'); hint.style.color = '#059669'; hint.innerText = '✅ ' + msg; } }
      if (hint) hint.style.color = '';
      if (cur.length !== 10 || nw.length !== 10) { fail('Current ଓ New ଦୁଇଟି 10-ଅଙ୍କ ହେବା ଉଚିତ୍।'); return; }
      if (nu.length !== 0 && nu.length !== 10) { fail('New Username (ଦେଲେ) 10-ଅଙ୍କ ହେବା ଉଚିତ୍।'); return; }
      var payload = { action: 'update_admin_password', currentPassword: cur, newPassword: nw };
      if (nu.length === 10) payload.newUsername = nu;
      if (!adminFetchHasHook_()) {
        fail('ପ୍ରଥମେ Admin → Sheets ଟ୍ୟାବ୍ ରେ Webhook URL ଲଗାନ୍ତୁ (କିମ୍ବା 1111111111 default ରେ login କରନ୍ତୁ)।');
        return;
      }
      var d = await adminFetchJson_(payload);
      if (d && (d.ok === true || d.status === 'success')) {
        okMsg('Password ବଦଳିଗଲା!');
        var c1 = document.getElementById('admCurPass'); if (c1) c1.value = '';
        var c2 = document.getElementById('admNewPass'); if (c2) c2.value = '';
        var c3 = document.getElementById('admNewUser'); if (c3) c3.value = '';
      } else {
        fail((d && d.error) || 'Password ବଦଳିଲା ନାହିଁ।');
      }
    }

    // Logout Admin: clear the persisted session, stop ringing, close panel.
    function logoutAdmin() {
      setAdminSession_(false);
      stopAdminOrderPolling_();
      stopRing_();
      closeNewOrderAlertModal_(true);
      closeAdminDashboard();
      renderAdminSyncStatus_('Live sync stopped', false);
      alert('👋 Admin Logout ହେଲା।');
    }

    function openAdminDashboard_() {
      if (!isAdmin_()) { openAdminModal(); return; }
      document.getElementById('adminDashboard').classList.remove('hidden');
      if (!(history.state && history.state.modal === 'admin_dash')) history.pushState({ modal: 'admin_dash' }, '', window.location.href);
      renderAdminOrders();
      renderAdminList();
      renderAdminBanners();
      try { syncOrdersFromSheet(true); } catch (e) {}
      try { syncUsersFromSheet(); } catch (e) {}
      try { syncBannersFromSheet(); } catch (e) {}
      var hook = window.munnaHookBase();
      document.getElementById('webhookInput').value = hook;
      document.getElementById('imgbbKeyInput').value = localStorage.getItem('munna_imgbb_key') || '';
      fillShopSettingsInputs();
      renderStockHealth();
      startAdminOrderPolling_();
      renderAdminSyncStatus_('Admin live sync started', true);
    }
    function closeAdminDashboard() { document.getElementById('adminDashboard').classList.add('hidden'); }

    function switchAdminTab(t) {
      ['orders','users','stock','banners','settings','sync'].forEach(tab => {
        document.getElementById(`admTab-${tab}`).classList.toggle('hidden', tab !== t);
        var b = document.getElementById(`admTabBtn-${tab}`);
        b.classList.toggle('border-[#1864ab]', tab === t);
        b.classList.toggle('text-[#1864ab]', tab === t);
        b.classList.toggle('border-transparent', tab !== t);
      });
      if (t === 'orders') renderAdminOrders();
      if (t === 'users') { renderAdminUsers(); syncUsersFromSheet(); }
      if (t === 'stock') { renderAdminList(); renderStockHealth(); }
      if (t === 'banners') { renderAdminBanners(); try { fillApkLinkInput_(); } catch (eApk) {} }
      if (t === 'settings') fillShopSettingsInputs();
    }

    function manualFullOrderSync() {
      if (!isAdmin_()) { openAdminModal(); return; }
      renderAdminSyncStatus_('Manual full live sync...', true);
      startAdminOrderPolling_();
      return syncOrdersFromSheet(true).then(function (ok) {
        if (ok) {
          renderAdminOrders();
          renderAdminSyncStatus_('Manual sync complete', true);
        } else {
          renderAdminSyncStatus_(_lastSyncReason || 'Manual sync failed — retrying', false);
        }
        return ok;
      });
    }

    function renderAdminOrders() {
      var box = document.getElementById('adminOrdersContainer');
      var orders = getMyOrders();
      if (!orders.length) {
        box.innerHTML = '<p class="text-xs text-slate-400 text-center py-6">No live orders loaded yet.<button onclick="manualFullOrderSync()" class="block mx-auto mt-2 px-3 py-1.5 rounded-lg bg-[#1864ab] text-white text-[10px] font-black">Fetch live orders</button></p>';
        return;
      }

      box.innerHTML = orders.map((o, oIdx) => {
        var isPickup = String(o.delivery_type || '') === 'Store Pickup';
        var mapsLink = `https://www.google.com/maps/dir/?api=1&origin=${STORE_LAT},${STORE_LNG}&destination=${o.lat||STORE_LAT},${o.lng||STORE_LNG}`;

        // Store Pickup: label + "Pickup in 30 min" countdown badge.
        var pickupBadgeHtml = '';
        if (isPickup) {
          var slotTxt = o.pickup_slot ? o.pickup_slot : '';
          var cd = pickupCountdownText_(o);
          pickupBadgeHtml = `
            <div class="flex items-center justify-between gap-2 bg-indigo-50 border border-indigo-200 rounded-lg px-2 py-1.5">
              <span class="text-[10px] font-black text-indigo-700 flex items-center gap-1">
                <i class="ph-bold ph-storefront"></i> 🏪 Store Pickup${slotTxt ? (' · Pickup in ' + slotTxt) : ''}
              </span>
              <span class="text-[10px] font-black px-2 py-0.5 rounded-md ${cd.cls}">${cd.txt}</span>
            </div>`;
        }

        var itemsEditorHtml = (o.items||[]).map((it, itIdx) => `
          <div class="flex items-center justify-between text-xs bg-slate-50 p-1.5 rounded-lg">
            <span class="font-bold line-clamp-1 flex-1">${it.name}</span>
            <div class="flex items-center gap-1.5 ml-2">
              <span class="text-slate-500">Qty: <b>${it.qty}</b></span>
              <button onclick="changeAdminOrderQty(${oIdx}, ${itIdx}, -1)" class="w-5 h-5 rounded bg-slate-200 font-bold">-</button>
              <button onclick="changeAdminOrderQty(${oIdx}, ${itIdx}, 1)" class="w-5 h-5 rounded bg-slate-200 font-bold">+</button>
              <button onclick="removeAdminOrderItem(${oIdx}, ${itIdx})" class="text-rose-600 font-bold ml-1"><i class="ph-bold ph-trash"></i></button>
            </div>
          </div>
        `).join('');

        return `
          <div class="bg-white p-3 rounded-2xl border border-slate-200 space-y-2 ${isPickup ? 'border-indigo-200' : ''}">
            <div class="flex justify-between items-center">
              <span class="font-black text-xs text-slate-900">#${o.id} - ${o.name}</span>
              <span class="text-[10px] bg-blue-100 text-[#1864ab] font-black px-2 py-0.5 rounded-md">${statusLabel(o.status)}</span>
            </div>
            ${pickupBadgeHtml}
            <div class="text-[11px] text-slate-600">${o.phone} · ${o.address}</div>
            <div class="space-y-1">${itemsEditorHtml}</div>
            <div class="flex justify-between items-center text-xs font-black pt-1 border-t border-slate-100">
              <a href="${mapsLink}" target="_blank" class="text-[#1864ab] flex items-center gap-1"><i class="ph-bold ph-navigation-arrow"></i> Google Maps Route</a>
              <span>Bill: Rs.${money(o.total)}</span>
            </div>
            ${adminOrderActionsHtml(o, oIdx)}
          </div>
        `;
      }).join('');
    }

    // "Pickup in 30 min" countdown (live) for a Store Pickup order.
    function pickupCountdownText_(o) {
      var st = String(o.status || '').toLowerCase();
      if (st === 'delivered' || st === 'rejected' || st === 'cancelled' ||
          st === 'expired / cancelled') {
        return { txt: statusLabel(o.status), cls: 'bg-slate-100 text-slate-500' };
      }
      var exp = Date.parse(String(o.pickup_expiry_time || '').replace(' ', 'T'));
      if (isNaN(exp)) return { txt: 'No expiry', cls: 'bg-slate-100 text-slate-500' };
      var diff = exp - Date.now();
      if (diff <= 0) return { txt: '⏰ Expired', cls: 'bg-rose-100 text-rose-700' };
      var mins = Math.floor(diff / 60000);
      var secs = Math.floor((diff % 60000) / 1000);
      var mm = String(mins).padStart(2, '0'), ss = String(secs).padStart(2, '0');
      return { txt: '⏳ ' + mm + ':' + ss, cls: 'bg-amber-100 text-amber-700' };
    }

    // Live 1s countdown refresh for open Store Pickup orders.
    setInterval(function () {
      var ad = document.getElementById('adminDashboard');
      if (!ad || ad.classList.contains('hidden')) return;
      var t = document.getElementById('admTab-orders');
      if (!t || t.classList.contains('hidden')) return;
      var anyPickupLive = getMyOrders().some(function (o) {
        if (String(o.delivery_type || '') !== 'Store Pickup') return false;
        var st = String(o.status || '').toLowerCase();
        return st === 'pending' || st === 'accepted' || st === 'packing' || st === 'out_for_delivery';
      });
      if (anyPickupLive) renderAdminOrders();
    }, 1000);

    // --- Frontend Store Pickup expiry sweep (mirrors the Apps Script trigger) ---
    // When a pickup order's PickupExpiryTime passes without Delivered/Picked Up,
    // give the LOCAL stock back + mark it Expired / Cancelled + tell the backend.
    function checkLocalPickupExpiry_() {
      var orders = getMyOrders();
      var changed = false;
      orders.forEach(function (o) {
        if (String(o.delivery_type || '') !== 'Store Pickup') return;
        var st = String(o.status || '').toLowerCase();
        if (st !== 'pending' && st !== 'accepted' && st !== 'packing' && st !== 'out_for_delivery') return;
        var exp = Date.parse(String(o.pickup_expiry_time || '').replace(' ', 'T'));
        if (isNaN(exp) || Date.now() <= exp) return;
        o.status = 'expired / cancelled';
        o.stage = 5;
        restoreLocalStockForOrder_(o); // local catalog (sets stock_restored='Y')
        postToSheet({ action: 'restore_pickup_stock', order_id: o.order_id }); // backend idempotent
        pushOrderStatus(o);
        changed = true;
      });
      if (changed) {
        setMyOrders(orders);
        try { renderAdminOrders(); } catch (e) {}
        try { renderOrders(); } catch (e) {}
      }
      return changed;
    }
    // Run on load + every 30s (the Apps Script 5-min trigger is the authority;
    // this keeps the local catalog honest between polls).
    setTimeout(checkLocalPickupExpiry_, 3000);
    setInterval(checkLocalPickupExpiry_, 30000);

    // ====================================================================
    // 🚨 15-SECOND RINGING NEW-ORDER ALERT (logged-in admins only)
    // Web Audio oscillator loop (no external asset) rings up to 15 s when a
    // pending + AdminAlertStatus=Unseen order appears. Accept/Reject/Review on
    // ANY device writes AdminAlertStatus=Reviewed -> every other device stops
    // its ring on the next poll. The modal auto-dismisses after 15 s.
    // ====================================================================
    var RING_SECONDS = 15;
    var RING_POLL_MS = 4500;           // faster ~4.5 s polling for live orders
    var _ringCtx = null, _ringOsc = null, _ringGain = null;
    var _ringTimer = null, _ringCountdown = null, _ringStopAt = 0, _ringVisibilityTimer = null;
    var _adminPollTimer = null, _adminPollBusy = false, _adminPollErrors = 0;
    var _activeNewOrderId = '';        // order id currently shown in the modal
    var _seenAlertIds = {};            // order ids already alerted on THIS device
    var _knownOrderIds = {};            // fallback: every order already seen on this device
    var _adminSyncBusy = false;
    var _lastServerSyncAt = 0;
    var _lastServerSyncOkAt = 0;
    var _lastSyncReason = '';
    // ===== Notification / Ring toggles (admin panel) =====
    // Default ON; persisted per-device in localStorage.
    var _notifOn = (localStorage.getItem('munna_admin_notif_on') !== '0');
    var _ringOn  = (localStorage.getItem('munna_admin_ring_on')  !== '0');
    function setNotifOn(v) { _notifOn = !!v; try { localStorage.setItem('munna_admin_notif_on', _notifOn ? '1' : '0'); } catch (e) {} }
    function setRingOn(v)  { _ringOn  = !!v; try { localStorage.setItem('munna_admin_ring_on',  _ringOn  ? '1' : '0'); } catch (e) {} }
    // Expose for inline HTML onclick in admin.html
    window.munnaSetNotifOn = setNotifOn;
    window.munnaSetRingOn  = setRingOn;
    window.munnaGetNotifOn = function () { return _notifOn; };
    window.munnaGetRingOn  = function () { return _ringOn; };
    // Desktop/in-page browser notification used ONLY when the native Android
    // bridge (MunnaOrderBridge.showNotification) is not available — e.g. a
    // normal Chrome tab. In the native app the bridge posts a system tray
    // notification that behaves like WhatsApp/Telegram.
    var _fallbackNoticeTimer = 0;

    function renderAdminSyncStatus_(text, ok) {
      var pill = document.getElementById('adminSyncStatus');
      var txt = document.getElementById('adminSyncStatusText');
      if (!pill || !txt) return;
      var adminSession = isAdmin_();
      pill.classList.toggle('hidden', !adminSession);
      if (!adminSession) return;
      txt.innerText = text;
      pill.classList.toggle('bg-rose-700', ok === false);
      pill.classList.toggle('bg-slate-950/85', ok !== false);
    }

    function vibrateNewOrder_() {
      try {
        if (!_ringOn) return;            // Ring toggle OFF -> no vibrate/ring
        if (!navigator.vibrate) return;
        // Phone-call style ring: long ring, pause, long ring, pause, long ring
        navigator.vibrate([1000, 500, 1000, 500, 1000]);
      } catch (e) {}
    }

    function showBrowserNewOrderNotice_(o) {
      if (!_notifOn) return;                // Notification toggle OFF -> no alert
      var msg = (o.customer_name || 'Customer') + ' · Rs.' + money(o.total || 0) +
        (String(o.delivery_type || '') === 'Store Pickup' ? ' · Store Pickup' : ' · Home Delivery');
      // 1) NATIVE app: prefer the Android bridge — it posts a REAL system
      //    tray notification (visible in the notification shade even when the
      //    app is backgrounded) with default sound + vibration, exactly like
      //    WhatsApp/Telegram. The bridge also opens this same alert overlay
      //    when the tray entry is tapped.
      try {
        if (window.MunnaOrderBridge &&
            typeof window.MunnaOrderBridge.showNotification === 'function') {
          window.MunnaOrderBridge.showNotification(
            '🚨 New Order #' + (o.order_id || ''),
            msg,
            String(o.order_id || o.id || '')
          );
        }
      } catch (eNative) {}
      // 2) FALLBACK (normal browser tab): Web Notification API. Debounced so
      //    rapid poll cycles cannot spam the notification centre.
      var now = Date.now();
      if (now - _fallbackNoticeTimer < 8000) return;
      _fallbackNoticeTimer = now;
      try { if (window.munnaShowOrderNotification) window.munnaShowOrderNotification('🚨 New Order #' + (o.order_id || ''), msg); } catch (e) {}
    }

    // ====================================================================
    // 📢 SEND IMAGE + TEXT NOTIFICATION TO ALL CUSTOMERS (admin broadcast)
    // POSTs to the Sheet webhook (action=SEND_CUSTOMER_NOTICE). Customers who
    // are logged in / have the webhook see it as a clickable card on Home.
    // Tapping opens the given URL in the same app page (overlay image view).
    // ====================================================================
    function sendCustomerNotification() {
      if (!isAdmin_()) { openAdminModal(); return; }
      var img  = document.getElementById('adminNoticeImg')  ? document.getElementById('adminNoticeImg').value.trim()  : '';
      var text = document.getElementById('adminNoticeText') ? document.getElementById('adminNoticeText').value.trim() : '';
      var url  = document.getElementById('adminNoticeUrl')  ? document.getElementById('adminNoticeUrl').value.trim()  : '';
      if (!text && !img) { alert('ପ୍ରଥମେ Text କିମ୍ବା Image URL ଦିଅନ୍ତୁ!'); return; }
      if (!url) url = window.location.href;
      var hook = window.munnaHookBase ? window.munnaHookBase() : '';
      if (!hook) { alert('⚠️ Sheet Webhook ସେଟ୍ କରନ୍ତୁ (Admin → Sheets ଟ୍ୟାବ୍)।'); return; }
      var payload = {
        action: 'SEND_CUSTOMER_NOTICE',
        title: text || 'ନୂତନ ଅଫର୍ / ଅପଡେଟ୍',
        body: text || '',
        image_url: img || '',
        click_url: url,
        ts: Date.now()
      };
      fetch(hook, {
        method: 'POST',
        headers: { 'Content-Type': 'text/plain;charset=utf-8' },
        body: JSON.stringify(payload)
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (d && d.ok) {
          alert('✅ ଗ୍ରାହକଙ୍କୁ ସୂଚନା ପଠାଯାଇଛି!');
          document.getElementById('adminNoticeImg').value = '';
          document.getElementById('adminNoticeText').value = '';
          document.getElementById('adminNoticeUrl').value = '';
        } else {
          alert('❌ ପଠାଇବାକୁ ବିଫଳ ହେଲା: ' + ((d && d.error) || 'unknown'));
        }
      }).catch(function (e) { alert('❌ ତ୍ରୁଟି: ' + e.message); });
    }

    function startRing_() {
      if (!_ringOn) return;                 // Ring toggle OFF -> no sound ring
      stopRing_(); // never stack oscillators
      // NATIVE APP: trigger the loud system ringtone (works even when the app
      // is later backgrounded — the ring keeps playing for 15 s). The Web Audio
      // oscillator below is the browser-tab fallback.
      try {
        if (window.MunnaOrderBridge && typeof window.MunnaOrderBridge.startRing === 'function') {
          window.MunnaOrderBridge.startRing();
        }
      } catch (eBridge) {}
      try {
        var AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        if (!_ringCtx) _ringCtx = new AC();
        if (_ringCtx.state === 'suspended') _ringCtx.resume();
        _ringOsc = _ringCtx.createOscillator();
        _ringGain = _ringCtx.createGain();
        _ringOsc.type = 'square';
        _ringOsc.frequency.value = 880;
        _ringGain.gain.value = 0.0001;
        _ringOsc.connect(_ringGain);
        _ringGain.connect(_ringCtx.destination);
        _ringOsc.start();
        // Beep pulse: square-wave gain blips ~2 per second for 15 s.
        var t = _ringCtx.currentTime;
        for (var i = 0; i < RING_SECONDS * 2; i++) {
          _ringGain.gain.setValueAtTime(0.0001, t + i * 0.5);
          _ringGain.gain.exponentialRampToValueAtTime(0.25, t + i * 0.5 + 0.02);
          _ringGain.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.5 + 0.28);
        }
        _ringStopAt = Date.now() + RING_SECONDS * 1000;
        _ringCountdown = setInterval(function () {
          var left = Math.max(0, Math.ceil((_ringStopAt - Date.now()) / 1000));
          var el = document.getElementById('newOrderRingTimer');
          if (el) el.innerText = left + 's';
          if (left <= 0) stopRing_();
        }, 250);
        _ringTimer = setTimeout(stopRing_, RINGSAFE_MS());
      } catch (e) {}
    }
    function RINGSAFE_MS() { return RING_SECONDS * 1000; }

    // Equivalent of audio.pause(); audio.currentTime = 0; for Web Audio:
    // kill the oscillator + reset state + clear all ring timers.
    function stopRing_() {
      // Stop the native Android ringtone too (accept/decline/review = stop).
      try {
        if (window.MunnaOrderBridge && typeof window.MunnaOrderBridge.stopRing === 'function') {
          window.MunnaOrderBridge.stopRing();
        }
      } catch (eBridgeOff) {}
      try {
        if (_ringOsc) { try { _ringOsc.stop(); } catch (e) {} _ringOsc.disconnect(); _ringOsc = null; }
        if (_ringGain) { try { _ringGain.disconnect(); } catch (e) {} _ringGain = null; }
        if (_ringCtx && _ringCtx.state === 'running') { try { _ringCtx.suspend(); } catch (e) {} }
      } catch (e) {}
      if (_ringTimer) { clearTimeout(_ringTimer); _ringTimer = null; }
      if (_ringCountdown) { clearInterval(_ringCountdown); _ringCountdown = null; }
      _ringStopAt = 0;
      var el = document.getElementById('newOrderRingTimer');
      if (el) el.innerText = RING_SECONDS + 's';
    }

    // Show the 🚨 modal for an unseen pending order + start the ring.
    function showNewOrderAlert_(o) {
      if (!o) return;
      _activeNewOrderId = String(o.order_id || o.id || '');
      var isPickup = String(o.delivery_type || '') === 'Store Pickup';
      var badge = document.getElementById('newOrderTypeBadge');
      if (badge) {
        badge.innerText = isPickup ? '🏪 Store Pickup' : '🚚 Home Delivery';
        badge.className = 'text-[10px] font-black px-2 py-0.5 rounded-md border ' +
          (isPickup ? 'bg-indigo-50 text-indigo-700 border-indigo-200'
                    : 'bg-blue-50 text-[#1864ab] border-blue-200');
      }
      var set = function (id, v) { var el = document.getElementById(id); if (el) el.innerText = v; };
      set('newOrderId', '#' + (o.order_id || o.id || ''));
      set('newOrderName', o.customer_name || o.name || 'Customer');
      set('newOrderPhone', o.phone || '—');
      set('newOrderAddr', o.address || '—');
      set('newOrderTotal', 'Rs.' + money(o.total || 0));
      set('newOrderTime', (o.date || o.timestamp || ''));
      var slotRow = document.getElementById('newOrderSlotRow');
      if (slotRow) {
        slotRow.classList.toggle('hidden', !isPickup);
        set('newOrderSlot', o.pickup_slot || '');
      }
      document.getElementById('newOrderAlertModal').classList.remove('hidden');
      try { window.munnaRequestOrderNotifications && window.munnaRequestOrderNotifications(); } catch (e) {}
      vibrateNewOrder_();
      showBrowserNewOrderNotice_(o);
      startRing_();
      // Auto-dismiss after 15 s if nobody acts (ring also stops then).
      setTimeout(function () {
        if (_activeNewOrderId === String(o.order_id || o.id || '')) {
          closeNewOrderAlertModal_(true);
        }
      }, RINGSAFE_MS());
    }

    function closeNewOrderAlertModal_(keepId) {
      document.getElementById('newOrderAlertModal').classList.add('hidden');
      stopRing_();
      if (!keepId) _activeNewOrderId = '';
    }

    // Accept / Reject / Review -> write AdminAlertStatus=Reviewed so ALL other
    // admin devices stop ringing on their next poll, then act on the order.
    function newOrderAlertAction(kind) {
      var id = _activeNewOrderId;
      if (!id) { closeNewOrderAlertModal_(false); return; }
      // Clear the native tray entry as soon as an admin acts on the order,
      // so the notification shade never shows a stale alert.
      try {
        if (window.MunnaOrderBridge && typeof window.MunnaOrderBridge.cancelNotification === 'function') {
          window.MunnaOrderBridge.cancelNotification(String(id));
        }
      } catch (eBridge) {}
      markOrderAlertReviewed_(id); // fire-and-forget POST (multi-device sync)
      closeNewOrderAlertModal_(false);
      var idx = -1;
      var list = getMyOrders();
      for (var i = 0; i < list.length; i++) {
        if (String(list[i].order_id) === String(id)) { idx = i; break; }
      }
      if (idx < 0) { syncOrdersFromSheet(); return; }
      if (kind === 'accept') { adminAcceptOrder(idx); }
      else if (kind === 'reject' || kind === 'decline') { adminRejectOrder(idx); }
      else { // review: keep status pending, just open the Orders tab
        switchAdminTab('orders');
        syncOrdersFromSheet();
      }
    }

    // POST AdminAlertStatus=Reviewed for one order (multi-admin ring sync).
    function markOrderAlertReviewed_(orderId) {
      if (!orderId) return;
      _seenAlertIds[orderId] = 1;
      postToSheet({ action: 'update_order_alert', order_id: orderId, alert: 'Reviewed' });
      // Also update the local copy so this device never re-rings it.
      try {
        var orders = getMyOrders();
        for (var i = 0; i < orders.length; i++) {
          if (String(orders[i].order_id) === String(orderId)) {
            orders[i].admin_alert_status = 'Reviewed';
            setMyOrders(orders);
            break;
          }
        }
      } catch (e) {}
    }

    // Poll GET_UNSEEN_ORDERS every ~4.5 s while an admin session is active.
    // Polling continues even while the dashboard is closed; it pauses only in
    // a hidden tab (Android/iOS browsers freeze background timers anyway) and
    // resumes instantly when the tab/app becomes visible again.
    function startAdminOrderPolling_() {
      if (!isAdmin_()) return;
      if (_adminPollTimer) return; // already running
      // Tell the native bridge to START background polling when this device
      // is an admin (keeps live-order alerts flowing while app is backgrounded).
      try {
        if (window.MunnaOrderBridge && typeof window.MunnaOrderBridge.setAdminMode === 'function') {
          window.MunnaOrderBridge.setAdminMode(true);
        }
      } catch (eBridgeOn) {}
      var tick = async function () {
        if (!isAdmin_()) { stopAdminOrderPolling_(); return; }
        // RING ALL THE TIME: keep polling even when app is minimized /
        // backgrounded / screen off — do NOT pause on document.hidden.
        if (_adminPollBusy) return;
        _adminPollBusy = true;
        try {
          var url = sheetHookUrl('action=GET_UNSEEN_ORDERS&_=' + Date.now());
          if (!url) return;
          var ctrl = new AbortController();
          var to = setTimeout(function () { try { ctrl.abort(); } catch (e) {} }, 7000);
          var res = await fetch(url, { cache: 'no-store', signal: ctrl.signal });
          clearTimeout(to);
          var data = await res.json();
          _adminPollErrors = 0;
          var list = (data && Array.isArray(data.orders)) ? data.orders : [];
          // Drop anything this device already alerted or that was Reviewed.
          var fresh = list.filter(function (r) {
            var id = String(r.order_id || r.id || '');
            if (!id || _seenAlertIds[id]) return false;
            if (String(r.admin_alert_status || '').trim() === 'Reviewed') return false;
            if (String(r.status || '').toLowerCase() !== 'pending') return false;
            if (_activeNewOrderId && id === _activeNewOrderId) return false; // keep current modal stable
            return true;
          });
          // Another admin already acted (Reviewed) -> stop our ring too.
          if (_activeNewOrderId && !fresh.some(function (r) {
              return String(r.order_id || r.id) === _activeNewOrderId; })) {
            markOrderAlertReviewed_(_activeNewOrderId); // local ack
            closeNewOrderAlertModal_(true);
          }
          if (fresh.length) {
            var target = normalizeOrder(fresh[0]);
            if (target) {
              _seenAlertIds[String(target.order_id)] = 1;
              _knownOrderIds[String(target.order_id)] = 1;
              // Merge into local cache so the admin Orders tab sees it.
              try {
                var byId = {};
                getMyOrders().forEach(function (o) { byId[o.order_id] = o; });
                byId[target.order_id] = target;
                setMyOrders(Object.keys(byId).map(function (k) { return byId[k]; }));
              } catch (e2) {}
              showNewOrderAlert_(target);
              var dashEl = document.getElementById('adminDashboard');
              if (dashEl && !dashEl.classList.contains('hidden')) renderAdminOrders();
            }
          }
        } catch (e) {
          // transient network error — retry with the normal interval
          _adminPollErrors++;
        } finally {
          _adminPollBusy = false;
        }
      };
      // Live admin panel refresh: GET_ORDERS also returns reviewed/older orders,
      // not only the pending alert rows. Always request the full store list for
      // admins so customer webapp orders appear immediately in the Orders tab.
      var syncOrdersTick = function () {
        if (!isAdmin_()) return;
        // Keep syncing even when minimized/backgrounded (ring all the time).
        if (_adminSyncBusy) return;
        _adminSyncBusy = true;
        renderAdminSyncStatus_('Syncing live orders...', true);
        syncOrdersFromSheet(true).then(function (ok) {
          _adminSyncBusy = false;
          _lastServerSyncAt = Date.now();
          if (ok) {
            _lastServerSyncOkAt = Date.now();
            renderAdminSyncStatus_('Live orders synced', true);
            return;
          }
          var stale = _lastSyncReason ||
            (_lastServerSyncOkAt && (Date.now() - _lastServerSyncOkAt > 15000)
              ? 'Server sync delayed'
              : 'Waiting for server...');
          renderAdminSyncStatus_(stale, false);
        }).catch(function () {
          _adminSyncBusy = false;
          renderAdminSyncStatus_('Server sync error', false);
        });
      };
      tick();
      syncOrdersTick();
      _adminPollTimer = setInterval(function () { tick(); syncOrdersTick(); }, RING_POLL_MS);
      // Mobile browsers pause background polling. When the tab/app comes back
      // to the foreground, poll immediately so an order placed meanwhile rings
      // without waiting for the next interval.
      document.addEventListener('visibilitychange', function () {
        if (!isAdmin_()) { stopAdminOrderPolling_(); return; }
        if (!document.hidden) {
          if (_ringVisibilityTimer) clearTimeout(_ringVisibilityTimer);
          _ringVisibilityTimer = setTimeout(function () { tick(); syncOrdersTick(); }, 300);
        }
      });
    }
    function stopAdminOrderPolling_() {
      if (_adminPollTimer) { clearInterval(_adminPollTimer); _adminPollTimer = null; }
      stopRing_();
      // Stop the native background poller too (admin logged out / session gone).
      try {
        if (window.MunnaOrderBridge && typeof window.MunnaOrderBridge.setAdminMode === 'function') {
          window.MunnaOrderBridge.setAdminMode(false);
        }
      } catch (eBridgeOff) {}
      renderAdminSyncStatus_('Live sync stopped', false);
    }

    // --- ALL CUSTOMERS (admin Users tab): local orders + Sheet Addresses ---
    function aggregateUsers_() {
      var map = {};
      function ensure(key, base) {
        key = String(key || '').trim();
        if (!key) return null;
        if (!map[key]) map[key] = { user_id: '', tg_id: '', name: '', phone: '', address: '', orders: 0, spent: 0, last: '' };
        var u = map[key];
        if (base) {
          if (!u.name && base.name) u.name = base.name;
          if (!u.phone && base.phone) u.phone = base.phone;
          if (!u.address && base.address) u.address = base.address;
          if (!u.tg_id && base.tg_id) u.tg_id = String(base.tg_id);
          if (!u.user_id && base.user_id) u.user_id = String(base.user_id);
        }
        return u;
      }
      // Every order on THIS device contributes (all customers of this store).
      getMyOrders().forEach(function (o) {
        var key = String(o.phone || '').trim() || String(o.telegram_user_id || '').trim();
        var u = ensure(key, { name: o.name, phone: o.phone, address: o.address, tg_id: o.telegram_user_id, user_id: o.telegram_user_id });
        if (!u) return;
        u.orders += 1;
        u.spent += (parseFloat(o.total) || 0);
        if (String(o.date || '') > u.last) {
          u.last = String(o.date || '');
          if (o.address) u.address = o.address;
          if (o.name) u.name = o.name;
        }
      });
      // Merge the Sheet-shared cache (every user, incl. Telegram bot orders).
      try {
        (JSON.parse(localStorage.getItem('MUNA_ALL_USERS') || '[]') || []).forEach(function (x) {
          var key = String(x.phone || '').trim() || String(x.user_id || '').trim();
          var u = ensure(key, x);
          if (!u) return;
          if (x.orders && !u.sheetCounted) { u.orders = Math.max(u.orders, x.orders); u.sheetCounted = true; }
          if (x.spent && x.orders) u.spent = Math.max(u.spent, x.spent);
          if (x.last && String(x.last) > u.last) u.last = String(x.last);
        });
      } catch (e) {}
      return map;
    }

    function getAllUsers_() {
      var map = aggregateUsers_();
      return Object.keys(map).map(function (k) { var u = map[k]; u.key = k; return u; })
        .sort(function (a, b) { return String(b.last || '').localeCompare(String(a.last || '')); });
    }

    // Pull ALL customers from the shared Sheet (GET_USERS action in
    // googlesheetwebapp.js) and cache them for the admin Users tab.
    async function syncUsersFromSheet() {
      var url = sheetHookUrl('action=GET_USERS&_=' + Date.now());
      if (url) {
        try {
          var res = await fetch(url);
          var data = await res.json();
          var users = (data && Array.isArray(data.users)) ? data.users : [];
          if (users.length) {
            try { localStorage.setItem('MUNA_ALL_USERS', JSON.stringify(users)); } catch (e) {}
          }
        } catch (e) {}
      }
      renderAdminUsers();
    }

    function renderAdminUsers() {
      var box = document.getElementById('adminUsersList');
      if (!box) return;
      var searchEl = document.getElementById('adminUserSearch');
      var q = searchEl ? String(searchEl.value || '').trim().toLowerCase() : '';
      var users = getAllUsers_();

      var stats = document.getElementById('adminUserStats');
      if (stats) {
        var totalOrders = users.reduce(function (a, u) { return a + (u.orders || 0); }, 0);
        var totalSpent = users.reduce(function (a, u) { return a + (u.spent || 0); }, 0);
        stats.innerHTML =
          '<span class="px-2 py-1 rounded-lg bg-blue-50 text-blue-700">👥 ମୋଟ ଗ୍ରାହକ: ' + users.length + '</span>' +
          '<span class="px-2 py-1 rounded-lg bg-amber-50 text-amber-700">📦 ମୋଟ ଅର୍ଡର: ' + totalOrders + '</span>' +
          '<span class="px-2 py-1 rounded-lg bg-emerald-50 text-emerald-700">💰 ବିକ୍ରୟ: Rs.' + money(totalSpent) + '</span>';
      }

      var filtered = users.filter(function (u) {
        if (!q) return true;
        return String(u.name || '').toLowerCase().indexOf(q) >= 0
          || String(u.phone || '').indexOf(q) >= 0
          || String(u.address || '').toLowerCase().indexOf(q) >= 0
          || String(u.tg_id || u.user_id || '').indexOf(q) >= 0;
      });

      if (!filtered.length) {
        box.innerHTML = '<p class="text-xs text-slate-400 text-center py-6">କୌଣସି ଗ୍ରାହକ ମିଳିଲା ନାହିଁ।<br><span class="text-[10px]">Sync ଚାପନ୍ତୁ କିମ୍ବା ଅର୍ଡର ଆସିବା ପରେ ଦେଖାଯିବ।</span></p>';
        return;
      }

      box.innerHTML = filtered.map(function (u) {
        var mapsLink = u.address
          ? ('https://www.google.com/maps/dir/?api=1&origin=' + STORE_LAT + ',' + STORE_LNG + '&destination=' + encodeURIComponent(u.address))
          : '';
        var lastStr = u.last ? String(u.last).substring(0, 16) : '—';
        var initial = (u.name || '?').charAt(0).toUpperCase();
        return `
          <div class="bg-white p-3 rounded-2xl border border-slate-200 text-xs space-y-1">
            <div class="flex items-center justify-between">
              <span class="flex items-center gap-2 min-w-0">
                <span class="w-8 h-8 rounded-full bg-blue-50 text-[#1864ab] font-black flex items-center justify-center shrink-0">${initial}</span>
                <span class="min-w-0">
                  <span class="font-black text-slate-800 block line-clamp-1">${u.name || 'Customer'}</span>
                  <span class="text-[11px] text-slate-500">📞 ${u.phone || '—'}${u.tg_id ? (' · TG ' + u.tg_id) : ''}</span>
                </span>
              </span>
              <span class="text-[10px] bg-blue-100 text-[#1864ab] font-black px-2 py-0.5 rounded-md shrink-0">${u.orders || 0} ଅର୍ଡର</span>
            </div>
            <div class="text-[11px] text-slate-600 flex items-start justify-between gap-2">
              <span class="line-clamp-2">📍 ${u.address || 'ଠିକଣା ନାହିଁ'}</span>
              ${mapsLink ? `<a href="${mapsLink}" target="_blank" class="text-[#1864ab] font-bold shrink-0 flex items-center gap-0.5"><i class="ph-bold ph-navigation-arrow"></i> Map</a>` : ''}
            </div>
            <div class="flex items-center justify-between pt-1.5 border-t border-slate-100 text-[11px]">
              <span class="text-slate-500">ଶେଷ ଅର୍ଡର: <b class="text-slate-700">${lastStr}</b></span>
              <span class="font-black text-emerald-700">ମୋଟ Rs.${money(u.spent)}</span>
            </div>
          </div>
        `;
      }).join('');
    }

    // Status -> human label + colour for the admin / customer views.
    var STATUS_LABELS = {
      pending: '🕐 Pending', accepted: '✅ Accepted', packing: '📦 Packing',
      out_for_delivery: '🚚 Out for delivery', delivered: '🎉 Delivered', cancelled: '❌ Cancelled',
      rejected: '🚫 Rejected', 'expired / cancelled': '⏰ Expired / Cancelled'
    };
    function statusLabel(s) { return STATUS_LABELS[s] || ('• ' + (s || 'pending')); }

    // Status-aware action buttons: Pending -> big Accept/Reject; active ->
    // progress buttons; finished -> nothing. Store Pickup orders get an extra
    // [ Mark Picked Up / Delivered ] and Reject restores the held stock.
    function adminOrderActionsHtml(o, oIdx) {
      var isPickup = String(o.delivery_type || '') === 'Store Pickup';
      var st = String(o.status || '').toLowerCase();

      if (o.status === 'pending') {
        if (isPickup) {
          return `
            <div class="grid grid-cols-3 gap-1.5 pt-1">
              <button onclick="adminAcceptOrder(${oIdx})" class="py-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white text-[11px] font-extrabold shadow-sm">✅ Accept</button>
              <button onclick="adminRejectOrder(${oIdx})" class="py-2 rounded-xl bg-rose-600 hover:bg-rose-700 text-white text-[11px] font-extrabold shadow-sm">🚫 Reject</button>
              <button onclick="adminMarkPickedUp(${oIdx})" class="py-2 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-[11px] font-extrabold shadow-sm">📦 Picked Up</button>
            </div>`;
        }
        return `
          <div class="flex gap-2 pt-1">
            <button onclick="adminAcceptOrder(${oIdx})" class="flex-1 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white text-[11px] font-extrabold shadow-sm">✅ Accept</button>
            <button onclick="adminRejectOrder(${oIdx})" class="flex-1 py-2 rounded-xl bg-rose-600 hover:bg-rose-700 text-white text-[11px] font-extrabold shadow-sm">❌ Reject</button>
          </div>`;
      }
      if (st === 'cancelled' || st === 'rejected' || st === 'expired / cancelled' || st === 'delivered') {
        return `<div class="text-[10px] text-center text-slate-400 font-bold pt-1">ଏହି ଅର୍ଡର ${statusLabel(o.status)} — ସରିଛି।</div>`;
      }
      if (isPickup) {
        // Accepted / packing -> [ Mark Picked Up / Delivered ] + Reject.
        return `
          <div class="grid grid-cols-2 gap-1.5 pt-1">
            <button onclick="setOrderFulfillmentStage(${oIdx}, 3)" class="py-2 rounded-xl bg-amber-500 text-white text-[11px] font-extrabold">📦 Packing</button>
            <button onclick="adminMarkPickedUp(${oIdx})" class="py-2 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-[11px] font-extrabold">✅ Mark Picked Up</button>
          </div>
          <button onclick="adminRejectOrder(${oIdx})" class="mt-1.5 w-full py-1.5 rounded-xl bg-rose-50 text-rose-600 text-[10px] font-bold border border-rose-200">🚫 Reject (restore stock)</button>`;
      }
      // accepted / packing / out_for_delivery -> keep the 5-stage progress
      // + a Cancel / Reject button (works on ANY live order).
      return `
        <div class="grid grid-cols-4 gap-1 pt-1">
          <button onclick="setOrderFulfillmentStage(${oIdx}, 2)" class="py-1 rounded bg-sky-600 text-white text-[10px] font-bold">2. Accept</button>
          <button onclick="setOrderFulfillmentStage(${oIdx}, 3)" class="py-1 rounded bg-amber-500 text-white text-[10px] font-bold">3. Packing</button>
          <button onclick="setOrderFulfillmentStage(${oIdx}, 4)" class="py-1 rounded bg-orange-600 text-white text-[10px] font-bold">4. Dispatch</button>
          <button onclick="setOrderFulfillmentStage(${oIdx}, 5)" class="py-1 rounded bg-emerald-600 text-white text-[10px] font-bold">5. Delivered</button>
        </div>
        <button onclick="adminRejectOrder(${oIdx})" class="mt-1.5 w-full py-1.5 rounded-xl bg-rose-50 text-rose-600 text-[10px] font-bold border border-rose-200">❌ Cancel / Reject Order</button>`;
    }

    function adminAcceptOrder(oIdx) {
      // markOrderAlertReviewed_ happens inside setOrderFulfillmentStage().
      setOrderFulfillmentStage(oIdx, 2);
    }

    // Store Pickup finalization: customer collected -> status Delivered,
    // stock deduction CONFIRMED (never restored).
    function adminMarkPickedUp(oIdx) {
      if (!confirm('✅ ଗ୍ରାହକ ପିକଅପ୍ କରିଛନ୍ତି କି? (Mark Picked Up / Delivered — ଷ୍ଟକ୍ କାଟା ଚୂଡ଼ାନ୍ତ)')) return;
      var orders = getMyOrders();
      if (!orders[oIdx]) return;
      orders[oIdx].status = 'delivered';
      orders[oIdx].stage = 5;
      setMyOrders(orders);
      markOrderAlertReviewed_(orders[oIdx].order_id); // ring sync (multi-device)
      pushOrderStatus(orders[oIdx]);
      renderAdminOrders();
      alert('✅ Order #' + orders[oIdx].id + ' Picked Up / Delivered — stock deduction confirmed.');
    }

    function adminRejectOrder(oIdx) {
      var orders = getMyOrders();
      var o = orders[oIdx];
      if (!o) return;
      var isPickup = String(o.delivery_type || '') === 'Store Pickup';
      if (!confirm('ଆପଣ ଏହି ଅର୍ଡର ବାତିଲ୍ କରିବାକୁ ଚାହୁଁଛନ୍ତି କି?' + (isPickup ? '\n(ଷ୍ଟକ୍ ତୁରନ୍ତ ଫେରିଯିବ)' : ''))) return;
      orders[oIdx].status = isPickup ? 'rejected' : 'cancelled';
      orders[oIdx].stage = 5;
      setMyOrders(orders);
      markOrderAlertReviewed_(orders[oIdx].order_id); // ring sync (multi-device)
      pushOrderStatus(orders[oIdx]); // -> Sheet -> bot edits customer's live message
      if (isPickup) {
        // Instantly give the held stock back (Sheet + local catalog).
        restorePickupStock_(orders[oIdx]);
        renderAdminOrders();
        alert('🚫 Order #' + orders[oIdx].id + ' Rejected — ଷ୍ଟକ୍ ଫେରିଗଲା (restored).');
        return;
      }
      renderAdminOrders();
      alert('❌ Order #' + orders[oIdx].id + ' ବାତିଲ୍ ହେଲା।');
    }

    function changeAdminOrderQty(oIdx, itIdx, delta) {
      var orders = getMyOrders();
      orders[oIdx].items[itIdx].qty += delta;
      if (orders[oIdx].items[itIdx].qty <= 0) {
        orders[oIdx].items.splice(itIdx, 1);
      }
      orders[oIdx].total = orders[oIdx].items.reduce((acc, curr) => acc + (parseFloat(curr.price||0) * curr.qty), 0);
      orders[oIdx].items_text = itemsToText(orders[oIdx].items);
      setMyOrders(orders);
      pushOrderBill(orders[oIdx]);
      renderAdminOrders();
    }

    function removeAdminOrderItem(oIdx, itIdx) {
      var orders = getMyOrders();
      orders[oIdx].items.splice(itIdx, 1);
      orders[oIdx].total = orders[oIdx].items.reduce((acc, curr) => acc + (parseFloat(curr.price||0) * curr.qty), 0);
      orders[oIdx].items_text = itemsToText(orders[oIdx].items);
      setMyOrders(orders);
      pushOrderBill(orders[oIdx]);
      renderAdminOrders();
    }

    function setOrderFulfillmentStage(oIdx, stageNum) {
      var orders = getMyOrders();
      if (!orders[oIdx]) return;
      orders[oIdx].stage = stageNum;
      orders[oIdx].status = STAGE_STATUS[stageNum - 1] || 'pending';
      setMyOrders(orders);
      // Any progress tap acknowledges the alert -> stop ringing everywhere.
      markOrderAlertReviewed_(orders[oIdx].order_id);
      // Push to the shared Sheet: the bot then edits the customer's live
      // Telegram status message and its own Admin Panel shows the same stage.
      pushOrderStatus(orders[oIdx]);
      renderAdminOrders();
      alert(`Order #${orders[oIdx].id} -> Stage ${stageNum} (${orders[oIdx].status})`);
    }

    // --- BANNER MANAGER ---
