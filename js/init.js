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
      autoTelegramLogin_(); // Telegram WebApp -> silent profile login
      // Restore the saved language (OD/EN) for the Profile selector.
      try { if (localStorage.getItem('munna_lang') === 'en') setLang(true); } catch (eL) {}
      updateHeaderAuth(); // no-op now (header chip removed) — kept for callers
      render();
      prefillOrderForm();
      renderProfile();
      updateCartBadge();
      renderScanHistory();
      // Fetch + render live inventory (localStorage webhook).
      syncFromGoogleSheet();
      // Admin-set offer banners are shared: Sheet first (ALL customers see the
      // same banners), local cache only until the Sheet answers.
      syncBannersFromSheet();
      setInterval(function () { syncBannersFromSheet(); syncFromGoogleSheet(); }, 120000);
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

      // Manifest shortcuts: /?screen=orders|cart|address|profile
      try {
        var scr = new URLSearchParams(window.location.search).get('screen');
        if (scr && ['orders', 'cart', 'address', 'profile'].indexOf(scr) >= 0) showScreen(scr);
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

