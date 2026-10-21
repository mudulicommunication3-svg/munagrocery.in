/* munnamobile :: js/pull-refresh.js
   Chrome-style pull-to-refresh on EVERY screen (Home + Cart/Orders/
   Address/Profile/Product). Pull down from the top; past the threshold
   release to refresh all live data (catalog, banners, orders, addresses,
   cloud cart). Soft refresh - never location.reload() - so the in-memory
   basket & login survive. Bubble HTML lives in index.html (outside #app),
   styles in css/styles.css. Registered in MUNNA_SCRIPTS before init. */
(function initPullToRefresh_() {
      var ind = document.getElementById('ptrIndicator');
      var txt = document.getElementById('ptrText');
      var arrow = document.getElementById('ptrArrow');
      if (!ind) return;
      var THRESHOLD = 64;   // px of finger travel that counts as "enough"
      var MAX_PULL = 110;   // visual cap for the bubble slide-in
      var RESIST = 0.5;     // Chrome-like finger resistance
      var startY = 0, startX = 0, pulling = false, armed = false, dist = 0, busy = false;

      // The active scroller: the visible full-screen .screen (Cart/Orders/
      // Address/Profile/Product) or the window itself on Home.
      function activeScroller_() {
        var screens = document.querySelectorAll('.screen');
        for (var i = 0; i < screens.length; i++) {
          var s = screens[i];
          if (!s.classList.contains('hidden')) return s;
        }
        return document.scrollingElement || document.documentElement;
      }
      function modalOpen_() {
        return !!document.querySelector(
          '#loginModal:not(.hidden), #adminPinModal:not(.hidden), #scannerModal:not(.hidden), #newOrderAlertModal:not(.hidden), #adminDashboard:not(.hidden)'
        );
      }
      function atTop_() { return (activeScroller_().scrollTop || 0) <= 0; }
      function onControl_(t) {
        // Never hijack drags that start on a control the user is using
        // (inputs, buttons, Leaflet maps, horizontal carousels).
        return !!(t && t.closest && t.closest('input, textarea, select, button, .leaflet-container, [data-no-ptr]'));
      }

      function setBubble_(px) {
        ind.classList.add('ptr-visible');
        ind.style.transform = 'translate(-50%, ' + (Math.min(px, MAX_PULL) - 70) + 'px)';
        var ready = dist >= THRESHOLD;
        ind.classList.toggle('ptr-ready', ready);
        if (arrow) arrow.style.transform = 'rotate(' + (ready ? 180 : Math.min(170, dist * 2.4)) + 'deg)';
        if (txt) txt.textContent = ready ? 'ଛାଡନ୍ତୁ — ରିଫ୍ରେସ୍ ହେବ' : 'ତଳକୁ ଟାଣନ୍ତୁ — ରିଫ୍ରେସ୍ ପାଇଁ';
      }
      function hideBubble_() {
        ind.classList.remove('ptr-visible', 'ptr-ready');
        ind.style.transform = 'translate(-50%, -110px)';
        if (arrow) arrow.style.transform = '';
      }
      async function doRefresh_() {
        busy = true;
        ind.classList.add('ptr-visible', 'ptr-loading');
        ind.classList.remove('ptr-ready');
        ind.style.transform = 'translate(-50%, 6px)';
        if (txt) txt.textContent = 'ରିଫ୍ରେସ୍ ହେଉଛି...';
        try { var sc = activeScroller_(); if (sc.scrollTop) sc.scrollTop = 0; } catch (e0) {}
        try {
          // One round-trip refresh of EVERY live module (the same calls the
          // boot makes) - each failure is non-fatal, old data simply stays.
          await Promise.all([
            (typeof syncFromGoogleSheet === 'function' ? syncFromGoogleSheet() : Promise.resolve()).catch(function () {}),
            (typeof syncBannersFromSheet === 'function' ? syncBannersFromSheet() : Promise.resolve()).catch(function () {}),
            (typeof syncOrdersFromSheet === 'function' ? syncOrdersFromSheet().catch(function () {}) : Promise.resolve()),
            (typeof syncAddressesFromSheet === 'function' ? syncAddressesFromSheet().catch(function () {}) : Promise.resolve()),
            (typeof hydrateCartFromCloud_ === 'function' ? Promise.resolve().then(hydrateCartFromCloud_).catch(function () {}) : Promise.resolve())
          ]);
          try { render(); } catch (eR) {}
          if (txt) txt.textContent = '✅ ତାଜା ହେଲା!';
        } catch (e) {
          if (txt) txt.textContent = '⚠️ ରିଫ୍ରେସ୍ ବିଫଳ — ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ';
        }
        setTimeout(function () { hideBubble_(); busy = false; }, 800);
      }
      // --- Touch path (phones / tablets) ---
      document.addEventListener('touchstart', function (e) {
        if (busy || pulling || e.touches.length > 1) return;
        if (modalOpen_() || onControl_(e.target)) return;
        // Never pull-to-refresh while Product Detail screen is open
        var pd = document.getElementById('productDetailScreen');
        if (pd && !pd.classList.contains('hidden')) return;
        if (!atTop_()) return;
        startY = e.touches[0].clientY;
        startX = e.touches[0].clientX;
        pulling = true; armed = false; dist = 0;
      }, { passive: true });

      document.addEventListener('touchmove', function (e) {
        if (!pulling || busy) return;
        var dy = e.touches[0].clientY - startY;
        var dx = Math.abs(e.touches[0].clientX - startX);
        // Scrolling up / horizontal swipe (carousel) -> hands off.
        if (dy <= 0 || dx > dy) { pulling = false; hideBubble_(); return; }
        if (!atTop_()) { pulling = false; hideBubble_(); return; }
        armed = true;
        if (e.cancelable) e.preventDefault(); // stop native scroll while pulling
        dist = dy * RESIST;
        setBubble_(dist);
      }, { passive: false });

      function endPull_() {
        if (!pulling) return;
        pulling = false;
        if (armed && dist >= THRESHOLD) doRefresh_();
        else hideBubble_();
        dist = 0; armed = false;
      }
      document.addEventListener('touchend', endPull_, { passive: true });
      document.addEventListener('touchcancel', endPull_, { passive: true });

      // --- Mouse path (desktop preview / testing) ---
      var mouseDown = false;
      document.addEventListener('mousedown', function (e) {
        if (busy || modalOpen_() || e.button !== 0) return;
        if (onControl_(e.target) || !atTop_()) return;
        // Never pull-to-refresh while Product Detail screen is open
        var pd = document.getElementById('productDetailScreen');
        if (pd && !pd.classList.contains('hidden')) return;
        mouseDown = true; pulling = true; armed = false; dist = 0;
        startY = e.clientY; startX = e.clientX;
      });
      document.addEventListener('mousemove', function (e) {
        if (!mouseDown || !pulling || busy) return;
        var dy = e.clientY - startY;
        var dx = Math.abs(e.clientX - startX);
        if (dy <= 0 || dx > dy || !atTop_()) { mouseDown = false; pulling = false; hideBubble_(); return; }
        armed = true;
        dist = dy * RESIST;
        setBubble_(dist);
      });
      document.addEventListener('mouseup', function () {
        if (!mouseDown) return;
        mouseDown = false;
        endPull_();
      });
    })();
