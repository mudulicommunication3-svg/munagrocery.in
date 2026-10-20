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

    // Telegram SDK
    var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
    if (tg) { tg.ready(); tg.expand(); }

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
      pickupChargeOn: false, pickupChargeFee: 0
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
    // The basket lives in the `cart` variable and is the single source of
    // truth for this page session. Persistence is delegated to the server:
    //   * every change -> debounced SAVE_USER_CART POST (see below)
    //   * every login/app-boot -> hydrateCartFromCloud_() pulls the saved
    //     basket from the Users sheet (CartJSON column) and REPLACES `cart`.
    // The legacy localStorage 'munna_cart' key is no longer read or written;
    // the first load after this change migrates it to the server once and
    // then purges it so it can never be trusted again.
    // =========================================================================
    function cacheCart_() {
      // No-op kept so existing callers don't break. Cart is not persisted
      // locally anymore — server sync happens in setQty() / applyCoupon().
      try { localStorage.removeItem(uidKey('munna_cart')); } catch (e) {}
    }
    function loadCart_() {
      // One-time migration: if a legacy local basket exists, push it to the
      // server (so it survives the switch) and then purge the local copy.
      try {
        var legacy = JSON.parse(localStorage.getItem(uidKey('munna_cart')) || 'null');
        if (legacy) {
          var migrated = (legacy.items && typeof legacy.items === 'object') ? legacy.items : legacy;
          if (typeof migrated === 'object' && !Array.isArray(migrated)) {
            Object.keys(migrated).forEach(function (ean) {
              var q = parseInt(migrated[ean], 10);
              if (q > 0 && q <= 99) cart[ean] = q;
            });
            if (typeof legacy.coupon === 'string') appliedCoupon = legacy.coupon;
            localStorage.removeItem(uidKey('munna_cart'));
            scheduleCartSync_(); // push migrated basket to the server
          }
        }
      } catch (e) {}
      // Always hydrate from the cloud (logged-in users only; see below).
      hydrateCartFromCloud_();
    }
    function clearCartCache_() {
      try { localStorage.removeItem(uidKey('munna_cart')); } catch (e) {}
      cart = {};
      appliedCoupon = '';
    }
    // Replace the in-memory cart with the server's saved basket (source of
    // truth). Used on login and on app boot when a session already exists.
    function applyCloudCart_(cloud) {
      try {
        if (!cloud || typeof cloud !== 'object' || Array.isArray(cloud)) return false;
        cart = {};
        Object.keys(cloud).forEach(function (ean) {
          var q = parseInt(cloud[ean], 10);
          if (q > 0) cart[ean] = Math.min(99, q);
        });
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

