/* munnamobile :: js/admin-settings.js  (extracted from index.html lines 3439-3854)
   banners, shop settings, save-all, trending, stock health */
    function renderAdminBanners() {
      var box = document.getElementById('adminBannerList');
      if (!banners.length) {
        box.innerHTML = '<p class="text-xs text-slate-400 text-center py-4">📢 ଏପର୍ଯ୍ୟନ୍ତ କୌଣସି ବ୍ୟାନର୍ ନାହିଁ — ଉପରେ ନୂଆ ବ୍ୟାନର୍ ଯୋଡ଼ନ୍ତୁ।<br><span class="text-[10px]">ଯୋଡ଼ିଲେ ସମସ୍ତ ଗ୍ରାହକଙ୍କ ଫୋନ୍ ରେ ଦେଖାଯିବ।</span></p>';
        return;
      }
      box.innerHTML = banners.map((b, idx) => `
        <div class="flex items-center justify-between p-2 bg-white rounded-xl border border-slate-200 text-xs">
          <div class="flex items-center gap-2">
            <img src="${b.img}" class="w-10 h-7 object-cover rounded">
            <span class="font-bold line-clamp-1">${b.title}</span>
          </div>
          <button onclick="deleteBanner(${idx})" class="text-rose-600 font-bold px-2 py-1"><i class="ph-bold ph-trash"></i></button>
        </div>
      `).join('');
    }

    function addBanner() {
      if (banners.length >= 5) { alert("ସର୍ବାଧିକ ୫ଟି ବ୍ୟାନର୍ ଅନୁମୋଦିତ।"); return; }
      var img = document.getElementById('newBannerImg').value.trim();
      var title = document.getElementById('newBannerTitle').value.trim();
      if (!img || !title) { alert("Image URL ଓ Title ଦିଅନ୍ତୁ!"); return; }
      banners.push({ id: Date.now(), title, img });
      try {
        localStorage.setItem('SHOP_BANNERS', JSON.stringify(banners));
        localStorage.setItem('SHOP_BANNERS_ADMIN', '1'); // this device set banners
      } catch (e) {}
      if (!pushBannersToSheet()) { // Sheet save failed -> warn (refresh-safe anyway)
        alert("⚠️ ବ୍ୟାନର୍ ଏହି ଫୋନ୍ ରେ ସେଭ୍ ହେଲା, କିନ୍ତୁ Sheet ରେ ପହଞ୍ଚିଲା ନାହିଁ!\nApps Script ନୂଆ version Deploy କରନ୍ତୁ (SAVE_CONFIG ଦରକାର)।");
        initCarousel();
        renderAdminBanners();
        document.getElementById('newBannerImg').value = '';
        document.getElementById('newBannerTitle').value = '';
        return;
      }
      initCarousel();
      renderAdminBanners();
      document.getElementById('newBannerImg').value = '';
      document.getElementById('newBannerTitle').value = '';
      alert("✅ ବ୍ୟାନର୍ ଯୋଡ଼ାଗଲା — ଏବେ ସମସ୍ତ ଗ୍ରାହକ ଦେଖିବେ!");
    }

    function deleteBanner(idx) {
      banners.splice(idx, 1);
      try {
        localStorage.setItem('SHOP_BANNERS', JSON.stringify(banners));
        localStorage.setItem('SHOP_BANNERS_ADMIN', '1');
      } catch (e) {}
      pushBannersToSheet(); // keep the shared Sheet in sync for ALL customers
      initCarousel();
      renderAdminBanners();
    }

    // --- ALL ADMIN CHANGES -> SHARED SHEET (every customer sees them) ---
    // Catalog add/edit/delete already post ADD_PRODUCT/EDIT_PRODUCT. This is
    // the generic helper for anything else the admin sets once for everyone.
    function pushAdminChange(key, value) {
      pushConfigToSheet(key, value);
    }

    // --- SHOP SETTINGS / TRENDING OFFERS / STOCK ENGINE / CANCEL (shared) ---
    function cacheShopConfig_() {
      try { localStorage.setItem('SHOP_SETTINGS', JSON.stringify(shopConfig)); } catch (e) {}
    }

    // Config tab keys -> shopConfig (delivery charge, coupon, trending list).
    function applyShopConfig_(cfg) {
      if (!cfg) return;
      var fee = parseFloat(cfg.delivery_fee); if (!isNaN(fee)) shopConfig.deliveryFee = Math.max(0, fee);
      var above = parseFloat(cfg.free_delivery_above); if (!isNaN(above)) shopConfig.freeAbove = Math.max(0, above);
      if (cfg.coupon_code !== undefined) shopConfig.couponCode = String(cfg.coupon_code || '').trim().toUpperCase();
      var pc = parseFloat(cfg.coupon_percent); if (!isNaN(pc)) shopConfig.couponPercent = Math.min(100, Math.max(0, pc));
      var mb = parseFloat(cfg.coupon_min_bill); if (!isNaN(mb)) shopConfig.couponMinBill = Math.max(0, mb);
      if (cfg.trending !== undefined) {
        shopConfig.trending = String(cfg.trending || '').split(',').map(function (x) { return String(x).trim(); }).filter(Boolean);
      }
      // Checkout & Delivery Settings (admin toggles from the shared Config).
      if (cfg.cod_active !== undefined) {
        shopConfig.codActive = !(String(cfg.cod_active) === '0' ||
          String(cfg.cod_active).toLowerCase() === 'false' ||
          String(cfg.cod_active).toLowerCase() === 'inactive');
      }
      if (cfg.cod_notice !== undefined) shopConfig.codNotice = String(cfg.cod_notice || '').trim() || 'Coming Soon';
      if (cfg.pickup_active !== undefined) {
        shopConfig.pickupActive = !(String(cfg.pickup_active) === '0' ||
          String(cfg.pickup_active).toLowerCase() === 'false' ||
          String(cfg.pickup_active).toLowerCase() === 'inactive');
      }
      if (cfg.pickup_note !== undefined) shopConfig.pickupNote = String(cfg.pickup_note || '').trim() || 'Free Pickup';
      if (cfg.pickup_slots !== undefined) {
        var slots = String(cfg.pickup_slots || '').split(',').map(function (x) { return String(x).trim(); })
          .filter(function (x) { return ['30 min', '1 hour', '2 hour'].indexOf(x) >= 0; });
        if (slots.length) shopConfig.pickupSlots = slots;
      }
      // Home-delivery timing (Home + Cart strip): ONE day + ONE slot.
      if (cfg.delivery_day !== undefined) {
        shopConfig.deliveryDay =
          (String(cfg.delivery_day || '').trim().toLowerCase() === 'tomorrow') ? 'Tomorrow' : 'Today';
      }
      if (cfg.delivery_slot !== undefined) {
        var dSlot = String(cfg.delivery_slot || '').trim();
        if (['30 min', '1 hour', '2 hour', '3 hour', '5 hour'].indexOf(dSlot) >= 0) {
          shopConfig.deliverySlot = dSlot;
        }
      }
      // Store Open / Closed status (sheet key: store_open).
      if (cfg.store_open !== undefined) {
        shopConfig.storeOpen = !(String(cfg.store_open) === '0' ||
          String(cfg.store_open).toLowerCase() === 'false' ||
          String(cfg.store_open).toLowerCase() === 'inactive' ||
          String(cfg.store_open).toLowerCase() === 'closed');
      }
      // Store Pickup charge toggle + amount.
      if (cfg.pickup_charge_on !== undefined) {
        shopConfig.pickupChargeOn = !(String(cfg.pickup_charge_on) === '0' ||
          String(cfg.pickup_charge_on).toLowerCase() === 'false' ||
          String(cfg.pickup_charge_on).toLowerCase() === 'inactive');
      }
      var pcfFee = parseFloat(cfg.pickup_charge_fee);
      if (!isNaN(pcfFee)) shopConfig.pickupChargeFee = Math.max(0, pcfFee);
      // Store Service Hours (shared Config).
      if (cfg.store_open !== undefined) {
        shopConfig.storeOpen = !(String(cfg.store_open) === '0' ||
          String(cfg.store_open).toLowerCase() === 'false');
      }
      if (typeof cfg.store_open_time === 'string' && cfg.store_open_time.trim()) {
        shopConfig.storeOpenTime = cfg.store_open_time.trim();
      }
      if (typeof cfg.store_close_time === 'string' && cfg.store_close_time.trim()) {
        shopConfig.storeCloseTime = cfg.store_close_time.trim();
      }
      // Editable closed banner message.
      if (cfg.store_open_message !== undefined) {
        shopConfig.storeClosedMessage = String(cfg.store_open_message || '').trim();
      }
      renderTrending();
      updateBillingSummary();
      try { renderDeliveryTiming(); } catch (eDT) {}
      // Re-render the checkout payment card so a toggle change (COD off / or
      // pickup off) is reflected the next time the customer opens Payment.
      try { if (document.getElementById('stepPayment') && !document.getElementById('stepPayment').classList.contains('hidden')) renderDeliveryOptions(); } catch (e) {}
      var ad = document.getElementById('adminDashboard');
      if (ad && !ad.classList.contains('hidden')) fillShopSettingsInputs();
    }

    function fillShopSettingsInputs() {
      var f = function (id, v) { var el = document.getElementById(id); if (el) el.value = v; };
      f('setDeliveryFee', shopConfig.deliveryFee);
      f('setFreeAbove', shopConfig.freeAbove);
      f('setCouponCode', shopConfig.couponCode);
      f('setCouponPercent', shopConfig.couponPercent || '');
      f('setCouponMinBill', shopConfig.couponMinBill || '');
      // Checkout & Delivery Settings.
      var codT = document.getElementById('setCodActive');
      if (codT) codT.checked = !!shopConfig.codActive;
      f('setCodNotice', shopConfig.codNotice || 'Coming Soon');
      var pkT = document.getElementById('setPickupActive');
      if (pkT) pkT.checked = !!shopConfig.pickupActive;
      f('setPickupNote', shopConfig.pickupNote || 'Free Pickup');
      ['30 min', '1 hour', '2 hour'].forEach(function (s) {
        var cb = document.getElementById('slotChk_' + s.replace(/\s+/g, '_'));
        if (cb) cb.checked = (shopConfig.pickupSlots || []).indexOf(s) >= 0;
      });
      // Delivery timing (Home + Cart) + Store Pickup charge.
      f('setDeliveryDay', shopConfig.deliveryDay === 'Tomorrow' ? 'Tomorrow' : 'Today');
      f('setDeliverySlot', shopConfig.deliverySlot || '1 hour');
      var soT = document.getElementById('setStoreOpen');
      if (soT) soT.checked = !!shopConfig.storeOpen;
      var soStatus = document.getElementById('storeOpenStatusText');
      if (soStatus) soStatus.innerText = shopConfig.storeOpen ? 'ଦୋକାନ ଖୋଲାଅଛି ✅' : 'ଦୋକାନ ବନ୍ଦ ❌';
      var pcT = document.getElementById('setPickupChargeOn');
      if (pcT) pcT.checked = !!shopConfig.pickupChargeOn;
      f('setPickupChargeFee', shopConfig.pickupChargeFee || 0);
      // Store Service Hours inputs.
      var soT = document.getElementById('setStoreOpen');
      if (soT) soT.checked = !!shopConfig.storeOpen;
      f('setStoreOpenTime', shopConfig.storeOpenTime || '10:00');
      f('setStoreCloseTime', shopConfig.storeCloseTime || '21:00');
      // Editable closed banner message.
      var scm = document.getElementById('setStoreClosedMessage');
      if (scm) scm.value = shopConfig.storeClosedMessage || '';
    }

    // Read the Checkout & Delivery Settings card into shopConfig.
    function readCheckoutSettingsInputs_() {
      var codT = document.getElementById('setCodActive');
      if (codT) shopConfig.codActive = !!codT.checked;
      var codN = document.getElementById('setCodNotice');
      if (codN) shopConfig.codNotice = (codN.value || '').trim() || 'Coming Soon';
      var pkT = document.getElementById('setPickupActive');
      if (pkT) shopConfig.pickupActive = !!pkT.checked;
      var pkN = document.getElementById('setPickupNote');
      if (pkN) shopConfig.pickupNote = (pkN.value || '').trim() || 'Free Pickup';
      var picked = ['30 min', '1 hour', '2 hour'].filter(function (s) {
        var cb = document.getElementById('slotChk_' + s.replace(/\s+/g, '_'));
        return cb && cb.checked;
      });
      // Keep at least one slot so Store Pickup always has a selectable option.
      shopConfig.pickupSlots = picked.length ? picked : ['30 min', '1 hour', '2 hour'];
      // Delivery timing (day + slot shown on Home & Cart).
      var dDay = document.getElementById('setDeliveryDay');
      if (dDay) shopConfig.deliveryDay = (dDay.value === 'Tomorrow' ? 'Tomorrow' : 'Today');
      var soT = document.getElementById('setStoreOpen');
      if (soT) shopConfig.storeOpen = !!soT.checked;
      var dSlot = document.getElementById('setDeliverySlot');
      if (dSlot && ['30 min', '1 hour', '2 hour', '3 hour', '5 hour'].indexOf(dSlot.value) >= 0) {
        shopConfig.deliverySlot = dSlot.value;
      }
      // Store Pickup charge toggle + amount.
      var pcT = document.getElementById('setPickupChargeOn');
      if (pcT) shopConfig.pickupChargeOn = !!pcT.checked;
      var pcF = document.getElementById('setPickupChargeFee');
      if (pcF) shopConfig.pickupChargeFee = Math.max(0, parseFloat(pcF.value) || 0);
      // Store Service Hours.
      var soT = document.getElementById('setStoreOpen');
      if (soT) shopConfig.storeOpen = !!soT.checked;
      var sot = document.getElementById('setStoreOpenTime');
      if (sot) shopConfig.storeOpenTime = sot.value || '10:00';
      var sct = document.getElementById('setStoreCloseTime');
      if (sct) shopConfig.storeCloseTime = sct.value || '21:00';
      // Editable closed banner message.
      var scm = document.getElementById('setStoreClosedMessage');
      if (scm) shopConfig.storeClosedMessage = scm.value.trim();
    }

    function saveShopSettings(quiet) {
      shopConfig.deliveryFee = Math.max(0, parseFloat((document.getElementById('setDeliveryFee') || {}).value) || 0);
      shopConfig.freeAbove = Math.max(0, parseFloat((document.getElementById('setFreeAbove') || {}).value) || 0);
      shopConfig.couponCode = (((document.getElementById('setCouponCode') || {}).value) || '').trim().toUpperCase();
      shopConfig.couponPercent = Math.min(100, Math.max(0, parseFloat((document.getElementById('setCouponPercent') || {}).value) || 0));
      shopConfig.couponMinBill = Math.max(0, parseFloat((document.getElementById('setCouponMinBill') || {}).value) || 0);
      // Checkout & Delivery Settings (toggles + notes + slots).
      readCheckoutSettingsInputs_();
      cacheShopConfig_();
      var ok = true;
      ok = pushConfigToSheet('delivery_fee', shopConfig.deliveryFee) && ok;
      ok = pushConfigToSheet('free_delivery_above', shopConfig.freeAbove) && ok;
      ok = pushConfigToSheet('coupon_code', shopConfig.couponCode) && ok;
      ok = pushConfigToSheet('coupon_percent', shopConfig.couponPercent) && ok;
      ok = pushConfigToSheet('coupon_min_bill', shopConfig.couponMinBill) && ok;
      // ALSO push the trending list so the same button saves EVERYTHING.
      ok = pushConfigToSheet('trending', (shopConfig.trending || []).join(',')) && ok;
      // Checkout & Delivery -> shared Config (every customer phone gets it).
      ok = pushConfigToSheet('cod_active', shopConfig.codActive ? '1' : '0') && ok;
      ok = pushConfigToSheet('cod_notice', shopConfig.codNotice || 'Coming Soon') && ok;
      ok = pushConfigToSheet('pickup_active', shopConfig.pickupActive ? '1' : '0') && ok;
      ok = pushConfigToSheet('pickup_note', shopConfig.pickupNote || 'Free Pickup') && ok;
      ok = pushConfigToSheet('pickup_slots', (shopConfig.pickupSlots || []).join(',')) && ok;
      // Delivery timing (Home + Cart) + Store Pickup charge -> shared Config.
      ok = pushConfigToSheet('delivery_day', shopConfig.deliveryDay === 'Tomorrow' ? 'Tomorrow' : 'Today') && ok;
      ok = pushConfigToSheet('delivery_slot', shopConfig.deliverySlot || '1 hour') && ok;
      ok = pushConfigToSheet('store_open', shopConfig.storeOpen ? '1' : '0') && ok;
      ok = pushConfigToSheet('store_open_time', shopConfig.storeOpenTime || '10:00') && ok;
      ok = pushConfigToSheet('store_close_time', shopConfig.storeCloseTime || '21:00') && ok;
      ok = pushConfigToSheet('store_open_message', shopConfig.storeClosedMessage || '') && ok;
      ok = pushConfigToSheet('pickup_charge_on', shopConfig.pickupChargeOn ? '1' : '0') && ok;
      ok = pushConfigToSheet('pickup_charge_fee', shopConfig.pickupChargeFee || 0) && ok;
      updateBillingSummary();
      try { renderDeliveryTiming(); } catch (eDT) {}
      renderTrending();
      if (quiet) return ok;
      alert(ok ? '✅ ସବୁ ସେଟିଂସ୍ ସେଭ୍ ହେଲା — ସମସ୍ତ ଗ୍ରାହକଙ୍କ ଫୋନ୍ ରେ ଲାଗୁ!'
               : '⚠️ ଏହି ଫୋନ୍ ରେ ସେଭ୍ ହେଲା, କିନ୍ତୁ Sheet ରେ ପହଞ୍ଚିଲା ନାହିଁ (Apps Script ନୂଆ version Deploy କରନ୍ତୁ)।');
      return ok;
    }

    // Auto-save ANY admin change: typing in the Shop settings fields pushes
    // every value to the shared Sheet 1.5s after the last keystroke, so a
    // change is never lost even without pressing "Save All to Sheet".
    (function wireShopAutoSave_() {
      var t = null;
      var schedule = function () {
        clearTimeout(t);
        t = setTimeout(function () { saveShopSettings(true); }, 1500);
      };
      ['setDeliveryFee', 'setFreeAbove', 'setCouponCode', 'setCouponPercent', 'setCouponMinBill',
       'setCodNotice', 'setPickupNote', 'setPickupChargeFee',
       'setStoreOpenTime', 'setStoreCloseTime', 'setStoreClosedMessage'].forEach(function (id) {
        var el = document.getElementById(id);
        if (!el || !el.addEventListener) return;
        el.addEventListener('input', schedule);
      });
      // Toggles + slot checkboxes save on change (they are not text fields).
      ['setCodActive', 'setPickupActive', 'setPickupChargeOn', 'setStoreOpen',
       'setDeliveryDay', 'setDeliverySlot',
       'slotChk_30_min', 'slotChk_1_hour', 'slotChk_2_hour',
       'setStoreOpen'].forEach(function (id) {
        var el = document.getElementById(id);
        if (!el || !el.addEventListener) return;
        el.addEventListener('change', schedule);
      });
    })();

    // 💾 MASTER SAVE — pushes EVERY admin change (settings + trending + banners)
    // to the shared Google Sheet so ALL customer phones update.
    async function saveAllToSheet() {
      // 1) Re-read the Shop Settings inputs so unsaved edits are included.
      try {
        shopConfig.deliveryFee = Math.max(0, parseFloat((document.getElementById('setDeliveryFee') || {}).value) || shopConfig.deliveryFee);
        shopConfig.freeAbove = Math.max(0, parseFloat((document.getElementById('setFreeAbove') || {}).value) || shopConfig.freeAbove);
        var cc = ((document.getElementById('setCouponCode') || {}).value || '').trim().toUpperCase();
        if (cc) shopConfig.couponCode = cc;
        var cp = parseFloat((document.getElementById('setCouponPercent') || {}).value);
        if (!isNaN(cp)) shopConfig.couponPercent = Math.min(100, Math.max(0, cp));
        var cmb = parseFloat((document.getElementById('setCouponMinBill') || {}).value);
        if (!isNaN(cmb)) shopConfig.couponMinBill = Math.max(0, cmb);
        readCheckoutSettingsInputs_();
      } catch (e) {}
      cacheShopConfig_();
      updateBillingSummary();
      try { renderDeliveryTiming(); } catch (eDT) {}

      var btn = document.getElementById('saveAllBtn');
      if (btn) { btn.disabled = true; btn.innerHTML = '<i class="ph-bold ph-spinner animate-spin"></i> Saving…'; }

      var ok = true;
      ok = pushConfigToSheet('delivery_fee', shopConfig.deliveryFee) && ok;
      ok = pushConfigToSheet('free_delivery_above', shopConfig.freeAbove) && ok;
      ok = pushConfigToSheet('coupon_code', shopConfig.couponCode) && ok;
      ok = pushConfigToSheet('coupon_percent', shopConfig.couponPercent) && ok;
      ok = pushConfigToSheet('coupon_min_bill', shopConfig.couponMinBill) && ok;
      ok = pushConfigToSheet('trending', (shopConfig.trending || []).join(',')) && ok;
      // Checkout & Delivery Settings (COD + Store Pickup toggles/slots).
      ok = pushConfigToSheet('cod_active', shopConfig.codActive ? '1' : '0') && ok;
      ok = pushConfigToSheet('cod_notice', shopConfig.codNotice || 'Coming Soon') && ok;
      ok = pushConfigToSheet('pickup_active', shopConfig.pickupActive ? '1' : '0') && ok;
      ok = pushConfigToSheet('pickup_note', shopConfig.pickupNote || 'Free Pickup') && ok;
      ok = pushConfigToSheet('pickup_slots', (shopConfig.pickupSlots || []).join(',')) && ok;
      // Delivery timing (Home + Cart) + Store Pickup charge -> shared Config.
      ok = pushConfigToSheet('delivery_day', shopConfig.deliveryDay === 'Tomorrow' ? 'Tomorrow' : 'Today') && ok;
      ok = pushConfigToSheet('delivery_slot', shopConfig.deliverySlot || '1 hour') && ok;
      ok = pushConfigToSheet('pickup_charge_on', shopConfig.pickupChargeOn ? '1' : '0') && ok;
      ok = pushConfigToSheet('pickup_charge_fee', shopConfig.pickupChargeFee || 0) && ok;
      // Store Service Hours -> shared Config (every customer phone gets it).
      ok = pushConfigToSheet('store_open', shopConfig.storeOpen ? '1' : '0') && ok;
      ok = pushConfigToSheet('store_open_time', shopConfig.storeOpenTime || '10:00') && ok;
      ok = pushConfigToSheet('store_close_time', shopConfig.storeCloseTime || '22:00') && ok;
      // Banners too (if any) so the carousel also syncs.
      try { ok = pushBannersToSheet() && ok; } catch (e) {}

      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ph-bold ph-floppy-disk"></i> 💾 Save All to Sheet'; }
      if (!ok) {
        alert('⚠️ ସେଭ୍ ହେଲା କିନ୍ତୁ Sheet ରେ ପହଞ୍ଚିଲା ନାହିଁ — Apps Script ନୂଆ version Deploy କରନ୍ତୁ।');
        return;
      }
      // ✅ Verify: re-fetch the Sheet config and confirm OUR values landed.
      // (no-cors POST can't return a response, so this read-back is the proof.)
      // On mismatch -> push trending ONCE more (heals an empty duplicate
      // Config row left by an old-script race) and re-check before failing.
      if (btn) { btn.disabled = true; btn.innerHTML = '<i class="ph-bold ph-spinner animate-spin"></i> Verifying…'; }
      var okVerify = false, vcfg = {};
      try {
        await new Promise(function (r) { setTimeout(r, 2500); }); // let Apps Script write
        var vurl = sheetHookUrl('action=GET_CONFIG&_=' + Date.now());
        var wantTrend = (shopConfig.trending || []).join(',');
        var wantFee = String(shopConfig.deliveryFee);
        for (var attempt = 0; attempt < 2 && !okVerify; attempt++) {
          if (!vurl) break;
          var vres = await fetch(vurl);
          var vdata = await vres.json();
          vcfg = (vdata && vdata.config) || {};
          var gotTrend = String(vcfg.trending || '').trim();
          var gotFee = (vcfg.delivery_fee !== undefined && vcfg.delivery_fee !== null) ? String(vcfg.delivery_fee) : '';
          if (gotTrend === wantTrend && (wantFee === '' || gotFee === wantFee)) {
            okVerify = true;
            break;
          }
          if (attempt === 0) {
            pushConfigToSheet('trending', wantTrend);
            await new Promise(function (r) { setTimeout(r, 2500); });
          }
        }
      } catch (e) {}
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="ph-bold ph-floppy-disk"></i> 💾 Save All to Sheet'; }
      if (okVerify) {
        applyShopConfig_(vcfg); cacheShopConfig_();
        alert('✅ ଯାଞ୍ଚ ହୋଇଗଲା — Sheet ରେ ସେଭ୍ ଅଛି!\n🔥 Trending · 🚚 ଡେଲିଭରୀ · 🎟️ କୁପନ୍ · 📢 Banners — ସମସ୍ତ ଗ୍ରାହକ ଫୋନ୍ ରେ ୨ ମିନିଟ୍ ମଧ୍ୟରେ ଦେଖାଯିବ!');
        return;
      }
      alert('📤 ସେଭ୍ ପଠାଗଲା କିନ୍ତୁ ଯାଞ୍ଚ ହେଲା ନାହିଁ।\n\n1️⃣ Apps Script ରେ ନୂଆ googlesheetwebapp.js ପେଷ୍ଟ୍ କରି Deploy କରନ୍ତୁ (ଏଥିରେ ଏହି auto-fix ଅଛି)।\n2️⃣ Config tab ରେ ଗୋଟିଏ key ର ୨ଟି ରୋ ଥିଲେ (ଯେପରି trending) ଗୋଟେ ଡିଲିଟ୍ କରନ୍ତୁ।');
    }

    // 🔥 Trending Offers strip on the home page (admin toggles per product).
    function renderTrending() {
      var sec = document.getElementById('trendingSection');
      var row = document.getElementById('trendingRow');
      if (!sec || !row) return;
      var list = (shopConfig.trending || []).map(function (ean) {
        return window.STORE_CATALOG.find(function (p) { return String(p.ean) === String(ean); });
      }).filter(Boolean).filter(function (p) {
        return (parseInt(p.stock_qty, 10) || 0) > 0;
      }).slice(0, 10);
      if (!list.length) { sec.classList.add('hidden'); return; }
      sec.classList.remove('hidden');
      document.getElementById('trendingCount').innerText = list.length + ' ଟି ଅଫର୍';
      row.innerHTML = list.map(function (p) {
        var mrpV = parseFloat(p.mrp) || 0, spV = parseFloat(p.selling_price) || 0;
        var off = mrpV > spV ? Math.round(((mrpV - spV) / mrpV) * 100) : 0;
        var tq = cart[p.ean] || 0;
        var qtyCtl = tq > 0
          ? `<div class="flex items-center gap-1 bg-slate-900 text-white rounded-lg px-1 py-0.5">
               <button onclick="setQty('${p.ean}', ${tq - 1})" class="w-5 h-5 rounded bg-slate-800 flex items-center justify-center"><i class="ph-bold ph-minus text-[10px]"></i></button>
               <span class="text-[11px] font-bold w-4 text-center">${tq}</span>
               <button onclick="setQty('${p.ean}', ${tq + 1})" class="w-5 h-5 rounded bg-[#1864ab] flex items-center justify-center"><i class="ph-bold ph-plus text-[10px]"></i></button>
             </div>`
          : `<button onclick="setQty('${p.ean}', 1)" class="w-6 h-6 rounded-lg bg-[#1864ab] text-white flex items-center justify-center"><i class="ph-bold ph-plus text-xs"></i></button>`;
        return `
        <div class="shrink-0 w-32 bg-white rounded-2xl border border-slate-200 p-2 relative">
          ${off > 0 ? `<span class="absolute top-1.5 left-1.5 bg-rose-600 text-white text-[9px] font-black px-1 py-0.5 rounded">${off}% OFF</span>` : ''}
          <img src="${p.image_url || 'https://images.unsplash.com/photo-1542838132-92c53300491e?w=300'}" class="w-full h-20 object-contain" alt="">
          <p class="text-[10px] font-bold text-slate-800 line-clamp-2 mt-1 leading-tight">${p.product_name}</p>
          <div class="flex items-center justify-between mt-1">
            <span class="text-xs font-black text-[#1864ab]">Rs.${money(p.selling_price)}</span>
            ${qtyCtl}
          </div>
        </div>`;
      }).join('');
    }

    function toggleTrending(ean) {
      ean = String(ean);
      var i = shopConfig.trending.indexOf(ean);
      if (i >= 0) shopConfig.trending.splice(i, 1); else shopConfig.trending.push(ean);
      cacheShopConfig_();
      // Auto-push immediately so the admin's star tap reaches the shared
      // Sheet right away (every customer phone picks it up on next poll).
      pushConfigToSheet('trending', (shopConfig.trending || []).join(','));
      renderTrending();
      renderAdminList();
    }

    // 📊 Stock counting engine: auto low-stock (<=5) / out-of-stock report.
    function renderStockHealth() {
      var low = [], out = [], okN = 0;
      window.STORE_CATALOG.forEach(function (p) {
        var q = parseInt(p.stock_qty, 10);
        if (isNaN(q)) q = 10;
        if (q <= 0) out.push(p);
        else if (q <= 5) low.push(p);
        else okN++;
      });
      var chips = document.getElementById('stockHealthChips');
      if (chips) {
        chips.innerHTML =
          `<span class="px-2 py-1 rounded-lg bg-emerald-50 text-emerald-700">✅ In stock: ${okN}</span>` +
          `<span class="px-2 py-1 rounded-lg bg-amber-50 text-amber-700">⚠️ Low (≤5): ${low.length}</span>` +
          `<span class="px-2 py-1 rounded-lg bg-rose-50 text-rose-700">❌ Out of stock: ${out.length}</span>`;
      }
      var listEl = document.getElementById('stockHealthList');
      if (!listEl) return;
      var rows = out.map(function (p) { return { p: p, kind: 'out' }; })
        .concat(low.map(function (p) { return { p: p, kind: 'low' }; }));
      listEl.innerHTML = rows.length ? rows.map(function (r) {
        var q = parseInt(r.p.stock_qty, 10) || 0;
        return `
        <div class="flex items-center justify-between text-[11px] px-2 py-1 rounded-lg ${r.kind === 'out' ? 'bg-rose-50' : 'bg-amber-50'}">
          <span class="font-bold ${r.kind === 'out' ? 'text-rose-700' : 'text-amber-700'} line-clamp-1">${r.p.product_name}</span>
          <span class="font-black shrink-0 ml-2">${r.kind === 'out' ? '❌ ସରିଗଲା' : ('⚠️ ' + q + ' ବଳିଛି')}</span>
        </div>`;
      }).join('') : '<p class="text-[11px] text-emerald-600 font-bold text-center py-2">✅ ସବୁ ପ୍ରୋଡକ୍ଟରେ ପର୍ଯ୍ୟାପ୍ତ ଷ୍ଟକ୍ ଅଛି!</p>';
    }

    // ❌ Customer-side cancel (allowed only BEFORE out for delivery).
    function cancelMyOrder(id) {
      if (!confirm('ଆପଣ ଏହି ଅର୍ଡର ବାତିଲ୍ କରିବାକୁ ଚାହୁଁଛନ୍ତି କି?')) return;
      var orders = getMyOrders();
      var o = orders.find(x => String(x.id) === String(id));
      if (!o) { alert('ଅର୍ଡର ମିଳିଲା ନାହିଁ।'); return; }
      if (['out_for_delivery', 'delivered', 'cancelled'].indexOf(o.status) >= 0) {
        alert('❌ ଅର୍ଡର ଡେଲିଭରୀ ପାଇଁ ବାହାରି ସାରିଛି — ଏବେ ବାତିଲ୍ କରିପାରିବେ ନାହିଁ।');
        return;
      }
      o.status = 'cancelled';
      o.stage = 5;
      setMyOrders(orders);
      postToSheet({ action: 'update_order_status', order_id: o.order_id, status: 'cancelled' });
      renderOrders();
      alert('❌ ଅର୍ଡର ବାତିଲ୍ ହେଲା। ଦୋକାନୀ ମଧ୍ୟ ଜାଣିପାରିବେ।');
    }

    // --- IMAGE LINK GENERATOR (upload -> public URL via ImgBB) ---
    function saveImgbbKey() {
      var k = document.getElementById('imgbbKeyInput').value.trim();
      localStorage.setItem('munna_imgbb_key', k);
      alert(k ? "✅ ImgBB API Key ସେଭ୍ ହେଲା! ଏବେ Banners ଟ୍ୟାବ୍ ରୁ ଫଟୋ ଅପଲୋଡ୍ କରି ଲିଙ୍କ୍ ତିଆରି କରନ୍ତୁ।" : "⚠️ ImgBB Key ହଟାଗଲା।");
    }

    function getImgbbKey() {
      // Saved override first, otherwise the built-in default key.
      return (localStorage.getItem('munna_imgbb_key') || '').trim() || DEFAULT_IMGBB_KEY;
    }

    async function generateImageLink() {
      var fileInput = document.getElementById('imgLinkFile');
      var btn = document.getElementById('imgLinkGenBtn');
      var file = fileInput.files && fileInput.files[0];
      if (!file) { alert("ପ୍ରଥମେ ଏକ ଫଟୋ ବାଛନ୍ତୁ!"); return; }

      var key = getImgbbKey(); // built-in default key; override via Sheets tab
      if (!key) {
        alert("⚙️ ପ୍ରଥମେ Sheets ଟ୍ୟାବ୍ ରେ ImgBB API Key ସେଟ୍ କରନ୍ତୁ।\n(ଫ୍ରି key: https://api.imgbb.com/)");
        switchAdminTab('sync');
        return;
      }

      btn.disabled = true;
      btn.innerHTML = '<i class="ph-bold ph-spinner animate-spin"></i> Uploading...';
      try {
        var url = await uploadToImgBB(file, key);
        if (url) {
          document.getElementById('imgLinkOutput').value = url;
          document.getElementById('imgLinkPreview').src = url;
          document.getElementById('imgLinkResult').classList.remove('hidden');
        } else {
          alert("❌ ଅପଲୋଡ୍ ବିଫଳ ହେଲା। ImgBB Key ଠିକ୍ ଅଛି କି ଯାଞ୍ଚ କରନ୍ତୁ।");
        }
      } catch (e) {
        alert("❌ ତ୍ରୁଟି: " + e.message);
      }
      btn.disabled = false;
      btn.innerHTML = '<i class="ph-bold ph-link-simple"></i> Generate Link';
    }

    // --- APP DOWNLOAD LINK (Android APK URL) — shown on Home as a floating
    // "Download App" button (webpage only, hidden inside the native app).
    // Saved to the shared Sheet Config tab (key: app_download_url) so a new
    // app version is instantly visible to every customer within ~2 minutes.
    // -------------------------------------------------------------------
    function showApkLinkStatus_(msg, ok) {
      var el = document.getElementById('apkLinkStatus');
      if (!el) return;
      el.classList.remove('hidden');
      el.style.color = ok ? '#059669' : '#e11d48';
      el.innerText = msg;
    }

    // Prefill the input with the CURRENT link whenever the Banners tab opens.
    function fillApkLinkInput_() {
      var el = document.getElementById('apkLinkInput');
      if (el && !el.value) el.value = (typeof _apkUrl !== 'undefined' && _apkUrl) ? _apkUrl : MUNNA_DEFAULT_APK_URL;
    }

    function saveApkLink() {
      if (!isAdmin_()) { openAdminModal(); return; }
      var el = document.getElementById('apkLinkInput');
      var url = el ? el.value.trim() : '';
      if (!url) { showApkLinkStatus_('❌ ପ୍ରଥମେ APK ଲିଙ୍କ୍ ଦିଅନ୍ତୁ!', false); return; }
      if (!/^https?:\/\//i.test(url)) { showApkLinkStatus_('❌ ଲିଙ୍କ୍ http/https ରୁ ଆରମ୍ଭ ହେବା ଉଚିତ୍।', false); return; }
      // Save to the shared Sheet Config tab so ALL customer devices pick it up.
      var ok = pushConfigToSheet('app_download_url', url);
      // Also update the local copy so THIS device sees the new link immediately.
      _apkUrl = url;
      try { localStorage.setItem('munna_apk_url', url); } catch (e) {}
      if (ok) {
        showApkLinkStatus_('✅ ଲିଙ୍କ୍ ସେଭ୍ ହେଲା! ସମସ୍ତ ଗ୍ରାହକ ହୋମ୍ ପେଜ୍ ରେ 2 ମିନିଟ୍ ମଧ୍ୟରେ ନୂଆ ଲିଙ୍କ୍ ପାଇବେ।', true);
      } else {
        showApkLinkStatus_('⚠️ ଲିଙ୍କ୍ ଏହି ଫୋନ୍ ରେ ସେଭ୍ ହେଲା, କିନ୍ତୁ Sheet ରେ ପହଞ୍ଚିଲା ନାହିଁ। Apps Script ନୂଆ version Deploy କରନ୍ତୁ (SAVE_CONFIG ଦରକାର)।', false);
      }
    }

    function resetApkLink() {
      if (!confirm('APK ଲିଙ୍କ୍ ଡିଫଲ୍ଟ ରେ ଫେରାଇବେ?')) return;
      _apkUrl = MUNNA_DEFAULT_APK_URL;
      try { localStorage.removeItem('munna_apk_url'); } catch (e) {}
      var el = document.getElementById('apkLinkInput');
      if (el) el.value = MUNNA_DEFAULT_APK_URL;
      pushConfigToSheet('app_download_url', MUNNA_DEFAULT_APK_URL); // also clear server-side
      showApkLinkStatus_('✅ ଡିଫଲ୍ଟ ଲିଙ୍କ୍ ରେ ଫେରିଗଲା।', true);
    }

