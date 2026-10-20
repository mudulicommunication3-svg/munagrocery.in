/* munnamobile :: js/init.js  (extracted from index.html lines 6126-6216)
   startup initialisation (DOMContentLoaded guard) */
    function munnaStartInit_() {
      try {
        var sp = new URLSearchParams(window.location.search);
        var sUrl = sp.get('sheet');
        // ?sheet= deep-link always wins (the bot passes its webhook this way).
        if (sUrl) {
          localStorage.setItem('munna_sheet_webhook', sUrl);
        }
        // Default production webhook is built into window.MUNNA_DEFAULT_WEBHOOK
        // (head of this file), so fresh / cleared-data browsers already talk
        // to YOUR Sheet. Admin -> Sheets override still wins per-device.
        // ?pw=<password> / ?wppx=<payload> quick-login routing (replaces the
        // old mob://wx PHP hook). ?pw= pre-fills / (localhost only) auto-logs-in;
        // ?wppx= is stashed for the WhatsApp deep-link bridge (reserved).
        var pwQ = sp.get('pw') || '';
        var wppxQ = sp.get('wppx') || '';
        if (wppxQ) { try { localStorage.setItem('munna_wppx', wppxQ); } catch (e2) {} }
        if (pwQ) { demoPwAutoLogin_(pwQ); }
      } catch(e){}

      initCarousel();
      loadCart_(); // restore cart + applied coupon so refresh never empties it
      // (Telegram Mini App auto-login removed — shop runs on our own HTML.)
      // Restore the saved language (OD/EN) for the Profile selector.
      try { if (localStorage.getItem('munna_lang') === 'en') setLang(true); } catch (eL) {}
      updateHeaderAuth(); // no-op now (header chip removed) — kept for callers
      render();
      prefillOrderForm();
      renderProfile();
      updateCartBadge();
      try { renderDeliveryTiming(); } catch (eDT) {} // admin-set timing on Home + Cart
      renderScanHistory();
      // Fetch + render live inventory (localStorage webhook).
      syncFromGoogleSheet();
      // Admin-set offer banners are shared: Sheet first (ALL customers see the
      // same banners), local cache only until the Sheet answers.
      syncBannersFromSheet();
      // Customer-side admin broadcast notice (image + text, clickable).
      fetchAdminNotice();
      // App download floating button (webpage only) + fetch the admin-updated
      // APK link so a new app version is always one tap away.
      updateAppDownloadVisibility_();
      fetchApkLink_();
      setInterval(function () { syncBannersFromSheet(); syncFromGoogleSheet(); fetchAdminNotice(); fetchApkLink_(); }, 120000);
      // Keep the store open/closed icon + delivery/pickup times fresh every
      // minute so it auto-changes exactly when the store opens/closes.
      setInterval(function () { try { renderDeliveryTiming(); } catch (e) {} }, 60000);
      // Shared-database sync: Telegram (bot) orders + saved addresses, then
      // keep refreshing so stage/status changes made in the bot show up here.
      // Admin/full-store devices use a faster sync managed by
      // startAdminOrderPolling_; customers retain the 60-second account sync.
      // IMMEDIATE render from the persisted cache so the Orders tab is never
      // blank while the first Sheet pull is in flight.
      try { renderOrders(); } catch (eR) {}
      syncOrdersFromSheet();
      syncAddressesFromSheet();
      if (!isAdmin_()) setInterval(function () { syncOrdersFromSheet(); }, 60000);
      setInterval(function () { syncAddressesFromSheet(); }, 300000);
      // Resume the 15 s new-order ring polling if this device is already an
      // authenticated admin (isAdminLoggedIn persisted across reloads).
      if (isAdmin_()) startAdminOrderPolling_();
      renderAdminSyncStatus_('Live sync ready', true);

      // ---- NATIVE APP: notification-tap handoff (window.MunnaOrderBridge) ----
      // MainActivity.onNewIntent/onResume stashes the tapped order id; the
      // page consumes it here (and on every focus) so the FULL-SCREEN alert
      // overlay + 15 s ring restore on whichever screen is frontmost — the
      // same WhatsApp/Telegram "tap -> live conversation" behaviour.
      var _nativeNoticeTimer = null;
      window.munnaOnNativeOrderNotice = function (orderId, title, body) {
        try { if (_nativeNoticeTimer) clearTimeout(_nativeNoticeTimer); } catch (eT) {}
        _nativeNoticeTimer = setTimeout(function () {
          try {
            var id = String(orderId || '').trim();
            if (!id) return;
            // Clear the native pending flag so the handoff fires only once.
            if (window.MunnaOrderBridge &&
                typeof window.MunnaOrderBridge.consumePendingNotificationOrderId === 'function') {
              window.MunnaOrderBridge.consumePendingNotificationOrderId();
            }
            if (!isAdmin_()) return;      // overlay is admin-only
            var target = null;
            var list = getMyOrders();
            for (var i = 0; i < list.length; i++) {
              if (String(list[i].order_id) === id) { target = list[i]; break; }
            }
            if (target) {
              // Merge-first then show: re-open the live overlay + ring.
              showNewOrderAlert_(target);
              try { syncOrdersFromSheet(true); } catch (eS) {}
            } else {
              // Not in cache yet — pull the full store list and retry once.
              try { syncOrdersFromSheet(true); } catch (eS2) {}
              _nativeNoticeTimer = setTimeout(function () {
                var l2 = getMyOrders(), t2 = null;
                for (var j = 0; j < l2.length; j++) {
                  if (String(l2[j].order_id) === id) { t2 = l2[j]; break; }
                }
                if (t2 && !_activeNewOrderId) showNewOrderAlert_(t2);
              }, 1500);
            }
            try {
              if (window.MunnaOrderBridge &&
                  typeof window.MunnaOrderBridge.consumePendingNotificationOrderId === 'function') {
                window.MunnaOrderBridge.consumePendingNotificationOrderId();
              }
            } catch (eC) {}
          } catch (eN) {}
        }, 400);
      };
      // Also poll the bridge once on boot + every focus (covers a cold start
      // straight from a notification tap, where onResume fired before this
      // handler existed).
      var checkNativeNotice_ = function () {
        try {
          if (!window.MunnaOrderBridge ||
              typeof window.MunnaOrderBridge.consumePendingNotificationOrderId !== 'function') return;
          var id = String(window.MunnaOrderBridge.consumePendingNotificationOrderId() || '').trim();
          if (id && window.munnaOnNativeOrderNotice) window.munnaOnNativeOrderNotice(id, '', '');
        } catch (eCN) {}
      };
      setTimeout(checkNativeNotice_, 2500);
      window.addEventListener('focus', function () { setTimeout(checkNativeNotice_, 600); });
      // Foreground/online/refresh events always trigger a full server pull so
      // the app never remains stuck on an old order list.
      window.addEventListener('focus', function () {
        // Re-sync for EVERYONE on refocus so the Orders list revalidates fast.
        try { syncOrdersFromSheet(); } catch (eF0) {}
        if (!isAdmin_()) return;
        renderAdminSyncStatus_('App active — syncing...', true);
        try { startAdminOrderPolling_(); } catch (e) {}
      });
      window.addEventListener('online', function () {
        try { syncOrdersFromSheet(); } catch (eO0) {}
        if (!isAdmin_()) return;
        renderAdminSyncStatus_('Back online — syncing...', true);
        try { startAdminOrderPolling_(); } catch (e) {}
      });
      window.addEventListener('pageshow', function (e) {
        try { syncOrdersFromSheet(); } catch (eP0) {}
        if (!isAdmin_() || !e.persisted) return;
        renderAdminSyncStatus_('App resumed — syncing...', true);
        try { startAdminOrderPolling_(); } catch (e2) {}
      });

      // REFRESH KEEPS THE SAME PAGE: /?screen=... (manifest shortcuts) or the
      // #hash written by showScreen() (#cart, #orders, #address, #profile,
      // #home). ?item=<ean> is handled right below by handleItemDeepLink_().
      try {
        var scr = new URLSearchParams(window.location.search).get('screen') ||
                  (window.location.hash || '').replace(/^#/, '');
        if (scr && ['home', 'cart', 'orders', 'address', 'profile'].indexOf(scr) >= 0) {
          showScreen(scr);
        }
      } catch (e) {}

      // One Product One Page deep-link: /?item=<ean>. The live catalog loads
      // async (syncFromGoogleSheet), so retry a few times until it is ready.
      handleItemDeepLink_();

      // PWA: register the service worker (offline shell — see sw.js).
      if ('serviceWorker' in navigator) {
        window.addEventListener('load', function () {
          navigator.serviceWorker.register('sw.js').catch(function () {});
        });
      }
        }
    if (document.readyState === 'loading') {
      window.addEventListener('DOMContentLoaded', munnaStartInit_);
    } else {
      munnaStartInit_(); // pages+js loaded after DOM ready
    }

