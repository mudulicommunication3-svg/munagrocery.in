/* munnamobile :: js/core.js  (extracted from index.html lines 1169-1484)
   state/config, cart cache, carousel, categories */
    // Store Location Coordinates: Niali Cinema Hall Road
    const STORE_LAT = 20.132475;
    const STORE_LNG = 86.059808;
    const MAX_RADIUS_KM = 3.0;


    // ImgBB API key - powers the Banners-tab Image Link Generator on every
    // device with no setup. Resolution order: localStorage override (Admin ->
    // Sheets tab) wins, otherwise this built-in default is used.
    const DEFAULT_IMGBB_KEY = "7608fb749170c4dd32e74a62823d776b";

    let customerLat = STORE_LAT;
    let customerLng = STORE_LNG;
    let isWithinRadius = true;
    let checkoutMap = null;
    let deliveryMarker = null;

    // Telegram Mini App SDK REMOVED — the shop is our own HTML (munagrocery.in)
    // and must NOT depend on Telegram. `tg` is kept as null for legacy callers.
    var tg = null;

    // Dynamic 5-Slide Banners
    let defaultBanners = [
      { id: 1, title: "ଅଟା, ତେଲ ଓ ଡାଲି ହୋଲସେଲ୍ ଦରରେ!", img: "https://images.unsplash.com/photo-1542838132-92c53300491e?w=700" },
      { id: 2, title: "ମୋବାଇଲ୍ ବ୍ୟାକ୍ କଭର୍ ଓ UV ଗ୍ଲାସ୍ ଫ୍ଲାଟ୍ ୫୦% ରିହାତି", img: "https://images.unsplash.com/photo-1584438784894-089d6a62b8fa?w=700" },
      { id: 3, title: "ଫ୍ରିଡମ୍ ସନଫ୍ଲାୱାର୍ ତେଲ ସ୍ପେଶାଲ୍ ଡିଲ୍", img: "https://images.unsplash.com/photo-1474979266404-7eaacbcd87c5?w=700" }
    ];
    // Start with the ADMIN-SET cache when this device ever saved banners
    // (flag SHOP_BANNERS_ADMIN) so a refresh never blanks the carousel.
    // Devices that only ever saw the old defaults start EMPTY and wait for
    // the shared Sheet (see syncBannersFromSheet).
    let banners = [];
    try {
      if (localStorage.getItem('SHOP_BANNERS_ADMIN') === '1') {
        banners = JSON.parse(localStorage.getItem('SHOP_BANNERS') || '[]') || [];
        if (!Array.isArray(banners)) banners = [];
      }
    } catch (e) { banners = []; }
    let currentBannerIdx = 0;
    let bannerInterval = null;

    // Shop-wide settings — admin sets ONCE (Sheet Config tab) -> EVERY phone
    // gets them: delivery charge, free-delivery threshold, coupon, trending,
    // plus the Checkout & Delivery toggles (COD + Store Pickup).
    let shopConfig = {
      deliveryFee: 30, freeAbove: 500, couponCode: '', couponPercent: 0, couponMinBill: 0, trending: [],
      // Checkout & Delivery Settings (admin-configurable)
      codActive: true, codNotice: 'Coming Soon',
      pickupActive: true, pickupNote: 'Free Pickup',
      pickupSlots: ['30 min', '1 hour', '2 hour'],
      // Home-delivery timing shown on the Home + Cart pages — the admin picks
      // ONE day and ONE slot (30 min / 1 hour / 2 hour / 3 hour / 5 hour).
      deliveryDay: 'Today',            // 'Today' | 'Tomorrow'
      deliverySlot: '1 hour',
      // Store Pickup charge (admin toggle; added to the bill when the customer
      // picks Store Pickup at checkout; OFF = free pickup).
      pickupChargeOn: false, pickupChargeFee: 0,
      // Store Open / Closed status — admin sets via Sheet Config tab.
      // When false, home page shows "ଦୋକାନ ବନ୍ଦ" banner and hides
      // delivery/pickup chips (date + timing removed).
      storeOpen: true,
      // Store Service Hours — default 10 AM to 9 PM, every day.
      // Admin can toggle ON/OFF and change open/close times.
      storeOpenTime: '10:00',     // HH:MM 24h — default 10:00 (10 AM)
      storeCloseTime: '21:00',    // HH:MM 24h — default 21:00 (9 PM)
      // Editable closed banner message shown on home page when store is closed.
      storeClosedMessage: ''
    };
    let appliedCoupon = ''; // coupon code applied inside the cart
    try {
      var cachedCfg = JSON.parse(localStorage.getItem('SHOP_SETTINGS') || 'null');
      if (cachedCfg && typeof cachedCfg === 'object') {
        ['deliveryFee', 'freeAbove', 'couponPercent', 'couponMinBill'].forEach(function (k) {
          var v = parseFloat(cachedCfg[k]);
          if (!isNaN(v)) shopConfig[k] = v;
        });
        if (typeof cachedCfg.couponCode === 'string') shopConfig.couponCode = cachedCfg.couponCode;
        if (Array.isArray(cachedCfg.trending)) shopConfig.trending = cachedCfg.trending;
        // Checkout & Delivery toggles (offline cache of the shared Config).
        if (typeof cachedCfg.codActive === 'boolean') shopConfig.codActive = cachedCfg.codActive;
        if (typeof cachedCfg.codNotice === 'string') shopConfig.codNotice = cachedCfg.codNotice;
        if (typeof cachedCfg.pickupActive === 'boolean') shopConfig.pickupActive = cachedCfg.pickupActive;
        if (typeof cachedCfg.pickupNote === 'string') shopConfig.pickupNote = cachedCfg.pickupNote;
        if (Array.isArray(cachedCfg.pickupSlots) && cachedCfg.pickupSlots.length) {
          shopConfig.pickupSlots = cachedCfg.pickupSlots;
        }
        if (typeof cachedCfg.deliveryDay === 'string') shopConfig.deliveryDay = cachedCfg.deliveryDay;
        if (typeof cachedCfg.deliverySlot === 'string') shopConfig.deliverySlot = cachedCfg.deliverySlot;
        if (typeof cachedCfg.pickupChargeOn === 'boolean') shopConfig.pickupChargeOn = cachedCfg.pickupChargeOn;
        var pcf = parseFloat(cachedCfg.pickupChargeFee);
        if (!isNaN(pcf)) shopConfig.pickupChargeFee = Math.max(0, pcf);
        // Store Service Hours cache (shared Config).
        if (typeof cachedCfg.storeOpen === 'boolean') shopConfig.storeOpen = cachedCfg.storeOpen;
        if (typeof cachedCfg.storeOpenTime === 'string') shopConfig.storeOpenTime = cachedCfg.storeOpenTime;
        if (typeof cachedCfg.storeCloseTime === 'string') shopConfig.storeCloseTime = cachedCfg.storeCloseTime;
        if (typeof cachedCfg.storeClosedMessage === 'string') shopConfig.storeClosedMessage = cachedCfg.storeClosedMessage;
      }
    } catch (e) {}

    // Catalog state
    window.STORE_CATALOG = [
      {"rowIndex": 2, "ean": "7533476", "product_name": "Freedom Refined Sunflower Oil 1 L", "mrp": "230", "selling_price": "169", "stock_qty": "10", "image_url": "https://cdn1.jiomartjcp.com/v2/catalog-cloud/jiomar/original/images/othe/egurhk/.j/pg/0000000004907689361.jpg.1947779d4b.jpg", "category": "Sunflower Oil"},
      {"rowIndex": 3, "ean": "7533477", "product_name": "Horlicks Classic Malt 1 kg", "mrp": "555", "selling_price": "390", "stock_qty": "10", "image_url": "https://images.unsplash.com/photo-1544787219-7f47ccb76574?w=400", "category": "Nutritional Drinks"},
      {"rowIndex": 4, "ean": "7533478", "product_name": "UV Curved Tempered Glass", "mrp": "299", "selling_price": "149", "stock_qty": "20", "image_url": "https://images.unsplash.com/photo-1584438784894-089d6a62b8fa?w=400", "category": "Mobile"}
    ];
    const localSaved = localStorage.getItem('munna_store_catalog');
    if (localSaved) {
      try { window.STORE_CATALOG = JSON.parse(localSaved); } catch(e){}
    }

    var cart = {};
    var activeCat = "All";
    var query = "";
    var isEnglish = false;
    var currentSearchSource = "jiomart";

    // --- STORE SERVICE HOURS + DELIVERY/PICKUP TIME CALCULATION ------------
    // "HH:MM" (24h) -> minutes since midnight.
    function hhmmToMinutes_(hhmm) {
      var parts = String(hhmm || '').split(':');
      var h = parseInt(parts[0], 10) || 0;
      var m = parseInt(parts[1], 10) || 0;
      return h * 60 + m;
    }
    // Is the store open RIGHT NOW based on admin set hours (every day)?
    function isStoreOpenNow_() {
      if (shopConfig.storeOpen === false) return false;
      var nowMins = new Date().getHours() * 60 + new Date().getMinutes();
      var openMins = hhmmToMinutes_(shopConfig.storeOpenTime);
      var closeMins = hhmmToMinutes_(shopConfig.storeCloseTime);
      // If close <= open (e.g. overnight), treat as open all day.
      if (closeMins <= openMins) return true;
      return nowMins >= openMins && nowMins < closeMins;
    }
    // Expected completion time (Date) = now + slot minutes, clamped by store hours.
    // If order placed before open time -> expected starts from open time.
    function expectedCompletionDate_(slotMinutes) {
      var now = new Date();
      var start = now;
      var openMins = hhmmToMinutes_(shopConfig.storeOpenTime);
      var openToday = new Date();
      openToday.setHours(Math.floor(openMins / 60), openMins % 60, 0, 0);
      // If current time is before opening time, compute from opening time.
      if (now < openToday) start = openToday;
      var exp = new Date(start.getTime() + slotMinutes * 60000);
      return exp;
    }
    // Format Date as "h:MM AM/PM".
    function fmtClock_(d) {
      var h = d.getHours(), m = d.getMinutes();
      var ap = h >= 12 ? 'PM' : 'AM';
      var h12 = h % 12; if (h12 === 0) h12 = 12;
      return h12 + ':' + (m < 10 ? '0' : '') + m + ' ' + ap;
    }
    // Human readable delivery/pickup expected time string in Odia.
    function expectedTimeText_(slotMinutes) {
      var exp = expectedCompletionDate_(slotMinutes);
      var now = new Date();
      var cross = exp.getDate() !== now.getDate() || exp.getMonth() !== now.getMonth();
      return fmtClock_(exp) + (cross ? ' (ଆଉକାଲି)' : '');
    }

    // Mathematical Haversine Distance Formula
    function calcDistance(lat1, lon1, lat2, lon2) {
      const R = 6371;
      const dLat = (lat2 - lat1) * Math.PI / 180;
      const dLon = (lon2 - lon1) * Math.PI / 180;
      const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                Math.sin(dLon/2) * Math.sin(dLon/2);
      const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
      return (R * c).toFixed(2);
    }

    // --- CAROUSEL SYSTEM ---
    // Loading shimmer until the shared (admin) banners arrive; the carousel
    // NEVER shows stale defaults — only admin-set banners scroll.
    function initCarousel() {
      const slides = document.getElementById('carouselSlides');
      const dots = document.getElementById('carouselDots');
      slides.innerHTML = '';
      dots.innerHTML = '';
      clearInterval(bannerInterval);

      if (!banners.length) {
        slides.innerHTML = '<div class="carousel-slide flex items-center justify-center bg-gradient-to-r from-blue-100 to-amber-100"><span class="text-[11px] font-bold text-slate-500"><i class="ph-bold ph-spinner animate-spin"></i> ଅଫର୍ ଲୋଡ୍ ହେଉଛି...</span></div>';
        return;
      }

      banners.slice(0, 5).forEach((b, idx) => {
        const slide = document.createElement('div');
        slide.className = 'carousel-slide';
        slide.innerHTML = `<img src="${b.img}" alt=""><div class="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/80 to-transparent text-white p-2 text-xs font-bold">${b.title}</div>`;
        slides.appendChild(slide);

        const dot = document.createElement('div');
        dot.className = `dot w-2 h-2 rounded-full bg-white/60 cursor-pointer ${idx === 0 ? 'active' : ''}`;
        dot.onclick = () => setSlide(idx);
        dots.appendChild(dot);
      });

      bannerInterval = setInterval(() => {
        currentBannerIdx = (currentBannerIdx + 1) % Math.min(banners.length, 5);
        setSlide(currentBannerIdx);
      }, 3500);
    }

    function setSlide(idx) {
      currentBannerIdx = idx;
      document.getElementById('carouselSlides').style.transform = `translateX(-${idx * 100}%)`;
      document.querySelectorAll('.dot').forEach((d, i) => d.classList.toggle('active', i === idx));
    }

    // --- CATALOG & CART RENDERING ---
    function money(v) { var n = parseFloat(v) || 0; return (Math.round(n * 100) / 100).toString(); }
    function catOf(p) { return (((p.category || "General") + "").trim()) || "General"; }

    function getCategories() {
      var seen = {}, out = ["All"];
      for (var i = 0; i < window.STORE_CATALOG.length; i++) {
        var c = catOf(window.STORE_CATALOG[i]);
        if (!seen[c]) { seen[c] = 1; out.push(c); }
      }
      return out;
    }

    function renderCategoryPills() {
      var box = document.getElementById("catsPillContainer");
      box.innerHTML = "";
      getCategories().forEach(function (c) {
        var b = document.createElement("button");
        b.className = (c === activeCat) 
          ? "shrink-0 px-3.5 py-1.5 rounded-full text-xs font-bold transition-all bg-[#1864ab] text-white shadow-xs"
          : "shrink-0 px-3.5 py-1.5 rounded-full text-xs font-bold transition-all bg-white text-slate-700 border border-slate-200 hover:bg-slate-50";
        b.textContent = c === "All" ? "ସବୁ ଜିନିଷ (All)" : c;
        b.onclick = function () { activeCat = c; render(); };
        box.appendChild(b);
      });
    }

    // =========================================================================
    // CART — IN-MEMORY ONLY (no localStorage persistence).
    // The basket lives in the `cart` variable with TWO persistence layers so a
    // REFRESH can never empty it:
    //   1) LOCAL copy (uidKey 'munna_cart') — written on every change, restored
    //      instantly at boot (works logged-out / offline too).
    //   2) SERVER copy (Users-tab CartJSON, debounced SAVE_USER_CART) — the
    //      cross-device source of truth. hydrateCartFromCloud_() pulls it at
    //      boot; the server WINS only when it actually holds items — an empty
    //      server cart never wipes a non-empty local basket (the local basket
    //      is then pushed back up instead).
    // =========================================================================
    function cacheCart_() {
      try {
        localStorage.setItem(uidKey('munna_cart'),
          JSON.stringify({ items: cart, coupon: appliedCoupon }));
      } catch (e) {}
    }
    function loadCart_() {
      // 1) LOCAL copy first: a refresh restores the basket instantly even when
      //    logged out or offline. Accepts {items,coupon} AND the legacy bare
      //    {ean: qty} map.
      try {
        var saved = JSON.parse(localStorage.getItem(uidKey('munna_cart')) || 'null');
        var src = (saved && saved.items && typeof saved.items === 'object') ? saved.items : saved;
        if (src && typeof src === 'object' && !Array.isArray(src)) {
          Object.keys(src).forEach(function (ean) {
            var q = parseInt(src[ean], 10);
            if (q > 0 && q <= 99) cart[ean] = q;
          });
          if (saved && typeof saved.coupon === 'string' && saved.coupon) appliedCoupon = saved.coupon;
        }
      } catch (e) {}
      // 2) Then hydrate the SERVER copy (logged-in): it wins when it has
      //    items; when empty the local basket is kept and pushed back up.
      hydrateCartFromCloud_();
    }
    function clearCartCache_() {
      cart = {};
      appliedCoupon = '';
      // Persist the EMPTY basket too — a refresh right after a placed order
      // must never resurrect it from the local copy.
      try {
        localStorage.setItem(uidKey('munna_cart'), JSON.stringify({ items: {}, coupon: '' }));
      } catch (e) {}
    }
    // Replace the in-memory cart with the server's saved basket (source of
    // truth). Used on login and on app boot when a session already exists.
    function applyCloudCart_(cloud) {
      try {
        if (!cloud || typeof cloud !== 'object' || Array.isArray(cloud)) return false;
        var cloudItems = {};
        Object.keys(cloud).forEach(function (ean) {
          var q = parseInt(cloud[ean], 10);
          if (q > 0) cloudItems[ean] = Math.min(99, q);
        });
        if (Object.keys(cloudItems).length > 0) {
          cart = cloudItems; // server holds a basket -> cross-device truth wins
        } else if (Object.keys(cart).length > 0) {
          // Server EMPTY + local basket present -> NEVER wipe it (refresh /
          // offline / login after local shopping). Push it back up instead so
          // the next refresh and the next device both see it.
          cacheCart_();
          pushCartToCloud_();
          return true;
        } else {
          cart = {};
        }
        cacheCart_(); // keep the local mirror in step with the server
        updateCartBadge();
        renderBar();
        renderCartDrawer();
        render();
        return true;
      } catch (e) { return false; }
    }
    // Fetch the logged-in customer's cloud cart (Users-tab CartJSON) and
    // hydrate the in-memory basket. Uses LOGIN_TOKEN/VALIDATE_TOKEN which
    // already returns { cart, orders, ... }. Fire-and-forget: a failure here
    // just means the basket starts empty for this session.
    var _cartHydrating = false;
    function hydrateCartFromCloud_() {
      if (_cartHydrating) return;
      var s = null;
      try { s = getSessionUser(); } catch (e) {}
      if (!s || !s.phone) return;
      var hook = pwHook_();
      if (!hook) return;
      // Prefer token revalidation (returns cart + orders in one round-trip).
      var body = null;
      if (s.login_token && s.login_token !== 'local') {
        body = { action: 'VALIDATE_TOKEN', token: s.login_token };
      } else {
        // No token — fall back to GET_ORDERS?phone= which now also returns
        // the user's cloud cart (see googlesheetwebapp.js GET_ORDERS).
        var url = sheetHookUrl('action=GET_ORDERS&phone=' + encodeURIComponent(s.phone) + '&_=' + Date.now());
        if (!url) return;
        _cartHydrating = true;
        fetch(url, { cache: 'no-store' })
          .then(function (r) { return r.json(); })
          .then(function (d) { if (d && d.cart) applyCloudCart_(d.cart); })
          .catch(function () {})
          .then(function () { _cartHydrating = false; });
        return;
      }
      _cartHydrating = true;
      fetch(hook, {
        method: 'POST',
        headers: { 'Content-Type': 'text/plain;charset=utf-8' },
        body: JSON.stringify(body)
      }).then(function (r) { return r.json(); })
        .then(function (d) {
          if (d && d.ok && d.cart) applyCloudCart_(d.cart);
        }).catch(function () {})
        .then(function () { _cartHydrating = false; });
    }
    // --- ACCOUNT-WISE CLOUD CART SYNC (multi-device) ---
    // setQty() calls scheduleCartSync_() on every change: a 1.2 s debounce then
    // POSTs SAVE_USER_CART { phone, cart } so the Users-tab CartJSON cell always
    // mirrors this customer's basket — the next device they log in on restores
    // it. No-ops when logged out or when no webhook is configured.
    var _cartSyncTimer = null;
    function scheduleCartSync_() {
      var u = null;
      try { u = getSessionUser(); } catch (e) {}
      if (!u || !u.phone) return;
      if (_cartSyncTimer) clearTimeout(_cartSyncTimer);
      _cartSyncTimer = setTimeout(function () {
        _cartSyncTimer = null;
        pushCartToCloud_();
      }, 1200);
    }
    function pushCartToCloud_() {
      var u = null;
      try { u = getSessionUser(); } catch (e) {}
      if (!u || !u.phone) return;
      var hook = pwHook_();
      if (!hook) return;
      try {
        fetch(hook, {
          method: 'POST',
          headers: { 'Content-Type': 'text/plain;charset=utf-8' },
          body: JSON.stringify({ action: 'SAVE_USER_CART', phone: u.phone, cart: cart })
        }).then(function (r) { return r.json(); }).catch(function () {});
      } catch (e) {}
    }
    function setQty(ean, qty) {
      qty = Math.max(0, Math.min(99, qty | 0));
      // Stock engine: never allow more than the available stock in the cart.
      var p = window.STORE_CATALOG.find(x => String(x.ean) === String(ean));
      if (p) {
        var stock = parseInt(p.stock_qty, 10);
        if (isNaN(stock)) stock = 10;
        if (qty > stock) {
          qty = Math.max(0, stock);
          if (stock <= 0) alert('❌ ' + (p.product_name || 'ସାମଗ୍ରୀ') + ' ଏବେ ସରିଯାଇଛି (Out of stock)!');
          else alert('⚠️ ' + (p.product_name || 'ସାମଗ୍ରୀ') + ' — କେବଳ ' + stock + ' ଟି ଷ୍ଟକ୍ ଅଛି, ଅଧିକ ଯୋଡ଼ିପାରିବେ ନାହିଁ।');
        }
      }
      if (qty <= 0) delete cart[ean]; else cart[ean] = qty;
      // Defensive: leaving the post-order "Success" card the moment a NEW
      // item is added, so the next order always lands on the Cart step and
      // the Checkout button is reachable again.
      if (qty > 0) {
        var _succEl2 = document.getElementById('stepSuccess');
        if (_succEl2 && !_succEl2.classList.contains('hidden')) setStep('Cart');
      }
      cacheCart_();
      scheduleCartSync_(); // account-wise cloud cart sync (debounced, logged-in only)
      render();
      renderCartDrawer();
    }

    // ====================================================================
    // 📢 CUSTOMER-SIDE: SHOW ADMIN BROADCAST NOTICE (image + text, clickable)
    // Fetches SEND_CUSTOMER_NOTICE from the Sheet webhook and renders it on Home.
    // Tapping opens the detail overlay; the detail overlay opens click_url in the
    // same page (admin notice page / offer page / product URL).
    // ====================================================================
    var _adminNotice = { img: '', text: '', url: '', title: '', ts: 0 };
    var _adminNoticeSeenTs = 0;

    function fetchAdminNotice() {
      // The notice is stored in the shared Config tab (key: customer_notice,
      // JSON) — pick it up via the normal GET_CONFIG read (same poll that
      // refills banners/shop settings), so NO extra backend action is needed.
      var hook = window.munnaHookBase ? window.munnaHookBase() : '';
      if (!hook) return;
      fetch(hook + (hook.indexOf('?') >= 0 ? '&' : '?') + 'action=GET_CONFIG&_=' + Date.now(),
            { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var cfg = (d && d.config) || {};
          var raw = cfg.customer_notice || '';
          var n = {};
          try { n = (typeof raw === 'string' ? JSON.parse(raw || '{}') : (raw || {})) || {}; } catch (eP) { n = {}; }
          if (!n.image_url && !n.text && !n.title) return;
          var nTs = parseInt(n.ts || 0, 10) || 0;
          if (nTs && nTs === _adminNoticeSeenTs) return; // already shown
        _adminNotice = {
          img: n.image_url || '',
          text: n.text || '',
          url: n.click_url || window.location.href,
          title: n.title || n.text || 'ନୂତନ ଅଫର୍ / ଅପଡେଟ୍',
          ts: nTs
        };
        renderAdminNotice();
      }).catch(function () {});
    }

    function renderAdminNotice() {
      var sec = document.getElementById('adminNoticeSection');
      var imgEl = document.getElementById('adminNoticeImgEl');
      var titleEl = document.getElementById('adminNoticeTitleEl');
      var textEl = document.getElementById('adminNoticeTextEl');
      if (!sec || !imgEl || !titleEl || !textEl) return;
      if (!_adminNotice.img && !_adminNotice.text) {
        sec.classList.add('hidden'); return;
      }
      if (_adminNotice.img) imgEl.src = _adminNotice.img;
      else imgEl.src = 'https://via.placeholder.com/700x300?text=Notice';
      imgEl.alt = _adminNotice.title;
      titleEl.innerText = _adminNotice.title;
      textEl.innerText = _adminNotice.text;
      sec.classList.remove('hidden');
      try { _adminNoticeSeenTs = _adminNotice.ts || Date.now(); } catch (e) {}
    }

    function openAdminNoticeDetail() {
      var ov = document.getElementById('adminNoticeDetail');
      var dImg = document.getElementById('adminNoticeDetailImg');
      var dTitle = document.getElementById('adminNoticeDetailTitle');
      var dText = document.getElementById('adminNoticeDetailText');
      var urlBtn = document.getElementById('adminNoticeUrlLabel');
      if (!ov) return;
      if (dImg) dImg.src = _adminNotice.img || 'https://via.placeholder.com/700x300?text=Notice';
      if (dTitle) dTitle.innerText = _adminNotice.title;
      if (dText) dText.innerText = _adminNotice.text;
      if (urlBtn) urlBtn.innerText = (_adminNotice.url ? 'ଲିଙ୍କ୍ ଖୋଲନ୍ତୁ' : 'ଲିଙ୍କ୍ ନାହିଁ');
      ov.classList.remove('hidden');
      document.body.style.overflow = 'hidden';
    }

    function closeAdminNoticeDetail() {
      var ov = document.getElementById('adminNoticeDetail');
      if (!ov) return;
      ov.classList.add('hidden');
      document.body.style.overflow = '';
    }

    function openAdminNoticeUrl() {
      if (!_adminNotice.url) return;
      // Open in the SAME app page (self) so the customer stays inside the shop.
      window.location.href = _adminNotice.url;
    }

    // ====================================================================
    // 📱 APP DOWNLOAD FLOATING BUTTON — shows ONLY on the webpage
    // (not inside the native Android app). Admin can update the APK link
    // anytime via the Sheet webhook (action=GET_CONFIG -> app_download_url).
    // Fallback: built-in GitHub Pages APK URL.
    // ====================================================================
    var MUNNA_DEFAULT_APK_URL = '/munagrocery.apk';
    var _apkUrl = MUNNA_DEFAULT_APK_URL;
    var _apkVersion = '';
    // Restore the locally-cached APK link so the button works even offline.
    try {
      var cachedApk = (localStorage.getItem('munna_apk_url') || '').trim();
      if (cachedApk) _apkUrl = cachedApk;
    } catch (eApkCache) {}

    function isNativeApp_() {
      return !!(window.MunnaOrderBridge &&
                typeof window.MunnaOrderBridge.showNotification === 'function');
    }

    function updateAppDownloadVisibility_() {
      var btn = document.getElementById('appDownloadBtn');
      if (!btn) return;
      // Hidden inside the native app; shown only in a normal browser/PWA tab.
      // The button lives in the header (flex row), so toggle flex/hidden.
      if (isNativeApp_()) { btn.classList.add('hidden'); btn.classList.remove('flex'); return; }
      btn.classList.remove('hidden');
      btn.classList.add('flex');
    }

    function downloadAndroidApp() {
      // Always serve the APK from THIS site's root so the newest build is
      // one tap away — same as <a href="/munagrocery.apk" download>.
      try { window.open('/munagrocery.apk', '_blank'); } catch (e) { window.location.href = '/munagrocery.apk'; }
    }

    // Fetch the admin-updated APK link from the Sheet Config tab
    // (key: app_download_url). Falls back to the built-in default.
    function fetchApkLink_() {
      var hook = window.munnaHookBase ? window.munnaHookBase() : '';
      if (!hook) return;
      fetch(hook + (hook.indexOf('?') >= 0 ? '&' : '?') + 'action=GET_CONFIG&_=' + Date.now(),
            { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var cfg = (d && d.config) || {};
          var u = String(cfg.app_download_url || '').trim();
          if (u) _apkUrl = u;
          var v = String(cfg.app_version || '').trim();
          if (v) {
            _apkVersion = v;
            var sub = document.querySelector('#appDownloadBtn span:last-child');
            if (sub) sub.innerText = 'Android APK · v' + v;
          }
        })
        .catch(function () {});
    }

