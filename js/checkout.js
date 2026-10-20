/* munnamobile :: js/checkout.js  (extracted from index.html lines 1596-2164)
   cart drawer, billing, 4-step checkout, map, place order, stock hold */
    function initCheckoutMap() {
      if (!checkoutMap) {
        checkoutMap = L.map('checkoutDeliveryMap').setView([STORE_LAT, STORE_LNG], 14);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 18 }).addTo(checkoutMap);

        // Shop Marker
        L.marker([STORE_LAT, STORE_LNG]).addTo(checkoutMap).bindPopup("<b>MUNNA GROCERY (Shop)</b>").openPopup();

        // 3 KM Zone
        L.circle([STORE_LAT, STORE_LNG], { color: '#16a34a', fillColor: '#86efac', fillOpacity: 0.2, radius: 3000 }).addTo(checkoutMap);

        // Draggable Customer Marker
        deliveryMarker = L.marker([STORE_LAT, STORE_LNG], { draggable: true }).addTo(checkoutMap);
        deliveryMarker.on('dragend', function(e) {
          var pos = e.target.getLatLng();
          updateMapDistance(pos.lat, pos.lng);
        });
      }
      setTimeout(() => { checkoutMap.invalidateSize(); }, 300);
      updateMapDistance(STORE_LAT, STORE_LNG);
    }

    function updateMapDistance(lat, lng) {
      customerLat = lat;
      customerLng = lng;
      deliveryMarker.setLatLng([lat, lng]);
      var dist = calcDistance(STORE_LAT, STORE_LNG, lat, lng);
      var pill = document.getElementById('mapStatusPill');
      var btn = document.getElementById('btnProceedToPayment');

      if (dist <= MAX_RADIUS_KM) {
        isWithinRadius = true;
        pill.className = "text-[10px] font-bold p-1.5 rounded-lg mb-2 text-center bg-emerald-50 text-emerald-800 border border-emerald-200";
        pill.innerText = `✅ ଦୂରତା: ${dist} KM (୩ କି.ମି. ଭିତରେ - ୧୦-୧୫ ମିନିଟ୍‌ରେ ଡେଲିଭରୀ)`;
        btn.disabled = false;
        btn.classList.remove('opacity-50', 'cursor-not-allowed');
      } else {
        isWithinRadius = false;
        pill.className = "text-[10px] font-bold p-1.5 rounded-lg mb-2 text-center bg-rose-50 text-rose-800 border border-rose-200";
        pill.innerText = `❌ ଦୂରତା: ${dist} KM (୩ କି.ମି. ବାହାରେ ଡେଲିଭରୀ ସେବା ଉପଲବ୍ଧ ନାହିଁ)`;
        btn.disabled = true;
        btn.classList.add('opacity-50', 'cursor-not-allowed');
      }
    }

    function locateUserGPS() {
      if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(pos => {
          checkoutMap.setView([pos.coords.latitude, pos.coords.longitude], 15);
          updateMapDistance(pos.coords.latitude, pos.coords.longitude);
        }, () => { alert("GPS ଅନୁମତି ମିଳିଲା ନାହିଁ। ମ୍ୟାପ୍‌ରେ ପିନ୍ ଟାଣି ରଖନ୍ତୁ।"); });
      }
    }

    // --- CHECKOUT WORKFLOW (separate full-screen Cart page) ---
    function openCartDrawer() {
      showScreen('cart');
    }

    function closeCartDrawer() {
      showScreen('home');
    }

    function renderCartDrawer() {
      var container = document.getElementById('cartItemsContainer');
      var items = Object.keys(cart);
      // Delivery address quick row (saved default address).
      var addrEl = document.getElementById('cartAddrText');
      if (addrEl) {
        var addrs = getAddresses();
        var def = addrs.filter(a => a.isDefault)[0] || addrs[0];
        addrEl.innerText = def ? ('📍 ' + def.name + ' — ' + def.address) : '📍 ଡେଲିଭରୀ ଠିକଣା ଯୋଡ଼ନ୍ତୁ';
      }
      if (items.length === 0) {
        container.innerHTML = '<p class="text-center text-xs text-slate-400 py-6">କାର୍ଟ ଖାଲି ଅଛି (Cart is empty)</p>';
        updateBillingSummary();
        return;
      }
      container.innerHTML = items.map(ean => {
        var p = window.STORE_CATALOG.find(x => String(x.ean) === String(ean));
        if (!p) return '';
        var qty = cart[ean];
        var sub = (parseFloat(p.selling_price) || 0) * qty;
        var stockN = parseInt(p.stock_qty, 10); if (isNaN(stockN)) stockN = 10;
        return `
          <div class="bg-slate-50 p-2 rounded-xl text-xs">
            <div class="flex items-center justify-between">
              <span class="font-bold line-clamp-1 flex-1">${p.product_name}</span>
              <span class="font-black text-[#1864ab] pl-2">Rs.${money(sub)}</span>
            </div>
            <div class="flex items-center justify-between mt-1.5">
              <div class="flex items-center gap-1.5">
                <button onclick="setQty('${ean}', ${qty - 1})" class="w-6 h-6 rounded bg-slate-200 flex items-center justify-center text-xs font-bold"><i class="ph-bold ph-minus"></i></button>
                <span class="font-bold w-6 text-center">${qty}</span>
                <button onclick="setQty('${ean}', ${qty + 1})" class="w-6 h-6 rounded bg-[#1864ab] text-white flex items-center justify-center text-xs"><i class="ph-bold ph-plus"></i></button>
                <span class="text-[9px] text-slate-400 ml-1">${stockN <= 5 ? ('⚠️ ' + stockN + ' ବଳିଛି') : ('Stock: ' + stockN)}</span>
              </div>
              <button onclick="setQty('${ean}', 0)" class="text-rose-600 font-bold px-1"><i class="ph-bold ph-trash"></i></button>
            </div>
          </div>
        `;
      }).join('');
      updateBillingSummary();
    }

    // --- BILLING ENGINE: subtotal + admin delivery charge + coupon discount ---
    function cartBilling() {
      var subtotal = 0;
      Object.keys(cart).forEach(function (ean) {
        var p = window.STORE_CATALOG.find(x => String(x.ean) === String(ean));
        if (p) subtotal += (parseFloat(p.selling_price) || 0) * cart[ean];
      });
      var freeAbove = shopConfig.freeAbove || 0;
      var delivery = (subtotal > 0 && (freeAbove <= 0 || subtotal < freeAbove)) ? (shopConfig.deliveryFee || 0) : 0;
      // Store Pickup charge (admin toggle ON) — only while Store Pickup is the
      // selected delivery method; switching back to Home Delivery drops it.
      var pickup = (shopConfig.pickupChargeOn && selectedDeliveryType === 'pickup' && subtotal > 0)
        ? Math.max(0, shopConfig.pickupChargeFee || 0) : 0;
      var discount = 0;
      var code = String(appliedCoupon || '').trim().toUpperCase();
      if (code && subtotal >= (shopConfig.couponMinBill || 0)) {
        discount = Math.round(subtotal * (shopConfig.couponPercent || 0)) / 100;
      }
      return { subtotal: subtotal, delivery: delivery, pickup: pickup, discount: discount,
               total: Math.max(0, subtotal + delivery + pickup - discount) };
    }

    function updateBillingSummary() {
      var b = cartBilling();
      var set = function (id, v) { var el = document.getElementById(id); if (el) el.innerText = v; };
      set('summarySubtotal', 'Rs.' + money(b.subtotal));
      var dRow = document.getElementById('summaryDeliveryRow');
      if (dRow) dRow.classList.toggle('hidden', b.subtotal <= 0);
      set('summaryDelivery', b.delivery > 0 ? ('Rs.' + money(b.delivery)) : 'ମାଗଣା ✅');
      var pRow = document.getElementById('summaryPickupRow');
      if (pRow) pRow.classList.toggle('hidden', !(b.pickup > 0));
      set('summaryPickup', 'Rs.' + money(b.pickup || 0));
      var disRow = document.getElementById('summaryDiscountRow');
      if (disRow) disRow.classList.toggle('hidden', b.discount <= 0);
      set('summaryDiscount', '-Rs.' + money(b.discount));
      set('summaryTotal', 'Rs.' + money(b.total));
    }

    // Home + Cart: show the admin-set home-delivery timing
    // (Today/Tomorrow · 30 min | 1 hour | 2 hour | 3 hour | 5 hour).
    function renderDeliveryTiming() {
      var day = (shopConfig.deliveryDay === 'Tomorrow') ? 'ଆଉକାଲି' : 'ଆଜି';
      var txt = day + ' · ' + (shopConfig.deliverySlot || '1 hour');
      var set = function (id) {
        var el = document.getElementById(id);
        if (el) el.textContent = txt;
      };
      set('deliveryTimingText'); // Home page strip (header ke neeche)
      set('cartTimingText');     // Cart page summary card
      set('payTimingText');      // Cart page -> Payment step (Home Delivery option)
    }

    function applyCoupon() {
      var input = document.getElementById('couponInput');
      var code = String(input ? input.value : '').trim().toUpperCase();
      if (!code) { alert('🎟️ Coupon code ଲେଖନ୍ତୁ!'); return; }
      if (!shopConfig.couponCode) { alert('ଏବେ କୌଣସି coupon ଚାଲୁ ନାହିଁ।'); return; }
      if (code !== String(shopConfig.couponCode).toUpperCase()) { alert('❌ ଭୁଲ୍ coupon code!'); return; }
      var subtotal = cartBilling().subtotal;
      if (subtotal < (shopConfig.couponMinBill || 0)) {
        alert('⚠️ ଏହି coupon ପାଇଁ ସର୍ବନିମ୍ନ ବିଲ୍ Rs.' + money(shopConfig.couponMinBill) + ' ଦରକାର!');
        return;
      }
      appliedCoupon = code;
      scheduleCartSync_(); // keep server CartJSON consistent (coupon only lives in-memory)
      renderCartDrawer();
      alert('✅ Coupon ଲାଗିଗଲା! Rs.' + money(cartBilling().discount) + ' ଛାଡ଼ ମିଳିଲା।');
    }

    function setStep(name) {
      ['stepCart','stepAddress','stepPayment','stepSuccess'].forEach(s => {
        var el = document.getElementById(s);
        if (el) el.classList.toggle('hidden', s !== ('step' + name));
      });
    }

    // Smooth-scroll the Cart page to a checkout step card.
    function scrollToStep_(id) {
      var el = document.getElementById(id);
      var screen = document.getElementById('cartScreen');
      if (!el || !screen) return;
      setTimeout(function () {
        screen.scrollTo({ top: Math.max(0, el.offsetTop - 90), behavior: 'smooth' });
      }, 30);
    }
    function goCheckoutAddress() {
      if (!Object.keys(cart).length) { alert("Cart ଖାଲି ଅଛି!"); return; }
      // Ordering requires a login; browsing/searching above stays open.
      if (!requireLoginForOrder_()) return;
      prefillOrderForm();
      renderSavedAddressPick();
      setStep('Address');
      initCheckoutMap();
      scrollToStep_('stepAddress');
    }
    function backToCart() { setStep('Cart'); scrollToStep_('cartItemsContainer'); }

    // --- DELIVERY METHOD (Home Delivery vs Store Pickup) + COD/Pickup config ---
    var selectedDeliveryType = 'home';      // 'home' | 'pickup'
    var selectedPickupSlot = '';            // e.g. "30 min"
    var checkoutPickupCfg = { codActive: true, codNotice: 'Coming Soon', pickupActive: true, pickupNote: 'Free Pickup', slots: ['30 min', '1 hour', '2 hour'] };

    // Pull the admin Checkout & Delivery toggles into the checkout card.
    function syncCheckoutConfig_() {
      checkoutPickupCfg.codActive = shopConfig.codActive !== false;
      checkoutPickupCfg.codNotice = shopConfig.codNotice || 'Coming Soon';
      checkoutPickupCfg.pickupActive = shopConfig.pickupActive !== false;
      checkoutPickupCfg.pickupNote = shopConfig.pickupNote || 'Free Pickup';
      var slots = Array.isArray(shopConfig.pickupSlots) && shopConfig.pickupSlots.length
        ? shopConfig.pickupSlots.slice() : ['30 min', '1 hour', '2 hour'];
      checkoutPickupCfg.slots = slots;
      // A slot no longer offered can no longer be the selection.
      if (selectedPickupSlot && slots.indexOf(selectedPickupSlot) < 0) selectedPickupSlot = '';
    }

    // Rebuild the Delivery Method card from the live admin settings.
    function renderDeliveryOptions() {
      syncCheckoutConfig_();
      var codOn = checkoutPickupCfg.codActive;
      var codRadio = document.getElementById('payCodRadio');
      var codLabel = document.getElementById('codOptionLabel');
      var codNotice = document.getElementById('codNoticeBar');
      if (codRadio) {
        codRadio.disabled = !codOn; // visible but NOT clickable when off
        if (codLabel) {
          codLabel.classList.toggle('opacity-50', !codOn);
          codLabel.classList.toggle('cursor-not-allowed', !codOn);
          codLabel.classList.toggle('cursor-pointer', codOn);
        }
      }
      if (codNotice) {
        // Show the admin notice (default: Coming Soon) right below COD when OFF.
        codNotice.classList.toggle('hidden', codOn);
        var cn = document.getElementById('codNoticeText');
        if (cn) cn.textContent = checkoutPickupCfg.codNotice || 'Coming Soon';
      }
      // NOTE: UPI option removed — when COD is OFF there is NO selectable
      // Home Delivery method: the COD radio simply stays disabled (opacity-50
      // + cursor-not-allowed) with the admin notice shown, and the customer
      // can still choose Store Pickup if it's active.
      // Store Pickup visibility + note badge.
      var pkOpt = document.getElementById('optStorePickup');
      if (pkOpt) pkOpt.classList.toggle('hidden', !checkoutPickupCfg.pickupActive);
      var pkBadge = document.getElementById('pickupNoteBadge');
      if (pkBadge) pkBadge.textContent = checkoutPickupCfg.pickupNote || 'Free Pickup';
      // Pickup OFF while selected -> go back to Home Delivery (COD radio).
      var checked2 = document.querySelector('input[name="payMethod"]:checked');
      if (!checkoutPickupCfg.pickupActive && checked2 && checked2.value === 'STORE_PICKUP') {
        var home = document.getElementById('payCodRadio');
        if (home && !home.disabled) { home.checked = true; checked2 = home; }
        selectedDeliveryType = 'home';
      }
      // Slot buttons for the currently selected delivery type.
      renderPickupSlots_();
      updateSlotVisibility_();
      // Admin-set delivery timing badge on the Home Delivery option.
      renderDeliveryTiming();
    }

    // Draw the [ 30 min ] [ 1 hour ] [ 2 hour ] slot buttons (admin-configured).
    function renderPickupSlots_() {
      var box = document.getElementById('pickupSlotBox');
      if (!box) return;
      box.innerHTML = (checkoutPickupCfg.slots || []).map(function (s) {
        var active = selectedPickupSlot === s;
        return '<button type="button" data-slot="' + s + '" onclick="selectPickupSlot(\'' + s.replace(/'/g, "\\'") + '\')" ' +
          'class="px-2.5 py-1.5 rounded-lg text-[11px] font-black border transition-all ' +
          (active ? 'bg-[#1864ab] text-white border-[#1864ab] scale-105'
                  : 'bg-slate-50 text-slate-700 border-slate-200') + '">' + s + '</button>';
      }).join('');
    }

    function selectPickupSlot(slot) {
      selectedPickupSlot = slot;
      renderPickupSlots_();
    }

    // Show the slot selector only when Store Pickup is the chosen method.
    function updateSlotVisibility_() {
      var checked = document.querySelector('input[name="payMethod"]:checked');
      selectedDeliveryType = (checked && checked.value === 'STORE_PICKUP') ? 'pickup' : 'home';
      var wrap = document.getElementById('pickupSlotWrap');
      if (wrap) wrap.classList.toggle('hidden', selectedDeliveryType !== 'pickup');
      // The pickup-charge row on the bill follows the chosen method.
      try { updateBillingSummary(); } catch (eUB) {}
    }

    // Wire radio changes -> re-render (COD/Pickup toggles + slot visibility).
    (function wireDeliveryRadios_() {
      var attach = function () {
        var radios = document.querySelectorAll('input[name="payMethod"]');
        for (var i = 0; i < radios.length; i++) {
          radios[i].addEventListener('change', function () {
            updateSlotVisibility_();
            renderPickupSlots_();
          });
        }
      };
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', attach);
      else attach();
    })();

    function goCheckoutPayment() {
      if (!isWithinRadius) { alert("୩ କିଲୋମିଟର ବାହାରେ ସେବା ଉପଲବ୍ଧ ନାହିଁ!"); return; }
      if (!document.getElementById('orderCustAddress').value.trim() || !document.getElementById('orderCustPhone').value.trim()) {
        alert("କୃପା କରି ଠିକଣା ଓ ଫୋନ୍ ଦିଅନ୍ତୁ!"); return;
      }
      setStep('Payment');
      renderDeliveryOptions(); // COD/Pickup toggles + slot buttons + notices
      scrollToStep_('stepPayment');
    }
    function backToAddress() { setStep('Address'); scrollToStep_('stepAddress'); }

    async function placeMiniOrder() {
      // Ordering requires a logged-in session (browse stays open without it).
      if (!requireLoginForOrder_()) return;
      var name = document.getElementById('orderCustName').value.trim() || 'Customer';
      var phone = document.getElementById('orderCustPhone').value.trim();
      var address = document.getElementById('orderCustAddress').value.trim();
      // Refresh the live admin toggles before validating the selection.
      syncCheckoutConfig_();
      var payEl = document.querySelector('input[name="payMethod"]:checked');
      var rawMethod = payEl ? payEl.value : 'COD';

      // --- Delivery method resolution + validation ---
      var isPickup = rawMethod === 'STORE_PICKUP';
      if (!isPickup && rawMethod === 'COD' && !checkoutPickupCfg.codActive) {
        // COD was switched OFF after the card rendered — never allow it.
        alert('⚠️ Cash on Delivery ଏବେ ବନ୍ଦ ଅଛି: ' + (checkoutPickupCfg.codNotice || 'Coming Soon'));
        renderDeliveryOptions();
        return;
      }
      if (isPickup && !checkoutPickupCfg.pickupActive) {
        alert('⚠️ Store Pickup ଏବେ ବନ୍ଦ ଅଛି।');
        renderDeliveryOptions();
        return;
      }
      var pickupSlot = '';
      var pickupExpiry = '';
      if (isPickup) {
        if (!selectedPickupSlot) {
          alert('⏳ ଦୟାକରି ଗୋଟିଏ ପିକଅପ୍ ସମୟ ବାଛନ୍ତୁ (Pickup slot required)!');
          updateSlotVisibility_();
          scrollToStep_('pickupSlotWrap');
          return;
        }
        pickupSlot = selectedPickupSlot;
        var mins = pickupSlot === '30 min' ? 30 : (pickupSlot === '2 hour' ? 120 : 60);
        var expDate = new Date(Date.now() + mins * 60000);
        pickupExpiry = sheetTimestamp(expDate); // order time + slot
      }
      var payment = isPickup ? 'Store Pickup' : (rawMethod || 'COD');
      var deliveryType = isPickup ? 'Store Pickup' : '';

      // Unified line items: EAN+qty (for stock) + name/price (for the bill text)
      var lines = [], total = 0, outOfStock = '';
      Object.keys(cart).forEach(function (ean) {
        var p = window.STORE_CATALOG.find(x => String(x.ean) === String(ean));
        if (!p) return;
        var qty = cart[ean];
        var price = parseFloat(p.selling_price) || 0;
        total += price * qty;
        lines.push({ ean: String(p.ean), name: p.product_name, qty: qty, price: price });
        var available = parseInt(p.stock_qty, 10);
        if (!isNaN(available) && available < qty && !outOfStock) outOfStock = p.product_name;
      });
      if (!lines.length) { alert("Cart ଖାଲି ଅଛି!"); return; }
      if (outOfStock) { alert("ସ୍ଟକ୍ ନାହିଁ (Out of stock): " + outOfStock); return; }
      // Store Pickup charge (admin toggle ON): added to the order total only
      // when the customer actually selects Store Pickup at checkout.
      if (isPickup && shopConfig.pickupChargeOn && (shopConfig.pickupChargeFee > 0)) {
        total += Math.max(0, shopConfig.pickupChargeFee || 0);
      }

      var tgUser = getTgUser();
      var orderObj = {
        order_id: makeOrderId(),
        timestamp: sheetTimestamp(new Date()),
        customer_name: name,
        phone: phone,
        address: address,
        items: itemsToText(lines),
        total: total,
        channel: 'webapp',
        telegram_user_id: (tgUser && tgUser.id) ? String(tgUser.id) : '',
        status: 'pending',
        status_msg_id: '',
        location: (customerLat && customerLng) ? (customerLat + ',' + customerLng) : '',
        payment: payment,
        delivery_type: deliveryType,
        pickup_slot: pickupSlot,
        pickup_expiry_time: pickupExpiry,
        stock_restored: '',
        admin_alert_status: 'Unseen',
        lines: lines
      };

      // Do NOT cache the order locally before the server confirms it — the
      // Sheet is the single source of truth. Success screen + stock deduction
      // happen ONLY after create_order is verified (see below).

      // ✅ Auto-save the address clicked/typed at checkout: next order
      // pre-fills it (and syncs to the shared Sheet for the bot/admin).
      autoSaveCheckoutAddress_(name, phone, address);

      // Sync the customer profile into the shared "Users" tab on checkout
      // (updates order count + last-visit timestamp; dedup by phone).
      var sessU = getSessionUser();
      syncUserToSheet({ name: name, phone: phone, tg_id: (sessU && sessU.tg_id) || '' });

      // ONE path only - never both, or the order would be recorded twice:
      //  * Inside Telegram the BOT places it (stock deduction + the SAME
      //    Sheet row + admin alert + live customer status message).
      //  * In a normal browser the Apps Script webhook writes the SAME Sheet
      //    row and the bot's web_sync loop alerts Telegram + tracks status.
      var sentToBot = sendToBot({
        action: 'ORDER',
        orderId: orderObj.order_id,
        customer: name,
        name: name,
        phone: phone,
        address: address,
        payment: payment,
        location: orderObj.location,
        delivery_type: deliveryType,
        pickup_slot: pickupSlot,
        pickup_expiry_time: pickupExpiry,
        items: lines.map(function (l) { return { ean: l.ean, qty: l.qty }; }),
        total: total,
        order: orderObj
      });
      // Browser/admin webapp orders: write the Sheet row FIRST and wait for the
      // verified server response. NOTHING below (success screen, stock hold,
      // cart clear) runs until the row is confirmed in the Orders tab.
      // Telegram Mini App orders are handled by the bot through sendData above.
      if (!sentToBot) {
        var hookMissing = !sheetHookUrl('action=GET_ORDERS');
        if (hookMissing) {
          renderAdminSyncStatus_('Sheet webhook ସେଟ୍ କରନ୍ତୁ — order ସେଭ୍ ହେଲା ନାହିଁ', false);
          alert('❌ Google Sheet webhook ସେଟ୍ ହୋଇନାହିଁ! Admin → Sheets ଟ୍ୟାବ୍ ରେ Webhook URL ଲଗାନ୍ତୁ, ତା\'ପରେ ପୁଣି order କରନ୍ତୁ।');
          return;
        }
        renderAdminSyncStatus_('Saving order to Google Sheet...', true);
        var saveRes = null;
        try {
          saveRes = await postToSheet({ action: 'create_order', order: orderObj });
        } catch (saveErr) {
          saveRes = { ok: false, json: null, error: String((saveErr && saveErr.message) || saveErr) };
        }
        var saveBody = (saveRes && saveRes.json) || {};
        var savedOk = !!(saveRes && saveRes.ok &&
          (saveBody.status === 'success' || saveBody.type === 'order_recorded' || saveBody.type === 'order_updated'));
        if (!savedOk) {
          var failMsg = (saveBody && saveBody.error) || ((saveRes && saveRes.error) || 'Sheet order save failed');
          renderAdminSyncStatus_(failMsg, false);
          // Queue the SAME order_id for an automatic retry (idempotent on the
          // server — never a duplicate row). The outbox is resend-only: it is
          // never rendered as history, so Orders still shows only Sheet rows.
          try { markPendingSheetOrder_(orderObj); resendPendingSheetOrders_().catch(function () {}); } catch (eOut) {}
          alert('❌ Order Sheet କୁ ଗଲା ନାହିଁ: ' + failMsg + '। ଇଣ୍ଟରନେଟ୍ / webhook ଯାଞ୍ଚ କରି ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ — ଏହି order ସ୍ୱୟଂଚାଳିତ ଭାବେ retry ହେବ (ଡୁପ୍ଲିକେଟ୍ ହେବ ନାହିଁ)।');
          return; // cart + form stay intact so the customer can retry safely
        }
        // Server accepted it — now PROVE the row physically exists before
        // showing success. Two fresh pulls so a cached GET cannot fake it.
        var savedId = String(saveBody.order_id || orderObj.order_id || '');
        var verified = false;
        try {
          await syncOrdersFromSheet(true);
          await syncOrdersFromSheet(true);
          verified = await verifyOrderOnSheet_(savedId);
        } catch (verifyErr) { verified = false; }
        if (!verified) {
          renderAdminSyncStatus_('Sheet save pending — retry from Orders', false);
          alert('⚠️ Order Sheet ରେ ଦେଖାଯାଉ ନାହିଁ। ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ — ଦୁଇଥର ଟଙ୍କା/ଷ୍ଟକ୍ କଟିବ ନାହିଁ (order ID ସମାନ ରହିବ)।');
          return;
        }
        renderAdminSyncStatus_('Order saved to Google Sheet', true);
      }
      // The Sheet row is VERIFIED at this point — local stock hold + in-memory
      // order insert happen here (never before), so a failed save can never
      // show a deducted quantity or a phantom success screen.
      // ✅ Store Pickup: hold the ordered quantities in the LOCAL catalog
      // right away. The Apps Script webhook does the same to the shared Sheet;
      // Reject/expiry restores it (see restorePickupStock_).
      if (isPickup) applyLocalStockHold_(lines);
      // Mirror the verified order into the in-memory list so the Orders
      // screen shows it instantly (source: Sheet, not a pre-save guess).
      try {
        var verifiedOrders = getMyOrders();
        verifiedOrders.unshift(orderObj);
        setMyOrders(verifiedOrders);
      } catch (mirrorErr) {}
      // Alert THIS admin device immediately, then refresh the full admin
      // panel so the new order is present even before the next poll.
      _seenAlertIds[orderObj.order_id] = 1;
      _knownOrderIds[orderObj.order_id] = 1;
      if (isAdmin_()) {
        showNewOrderAlert_(orderObj);
        renderAdminSyncStatus_('New order queued — syncing...', true);
        try {
          syncOrdersFromSheet(true).then(function () {
            renderAdminSyncStatus_('New order received', true);
            var dash = document.getElementById('adminDashboard');
            if (dash && !dash.classList.contains('hidden')) renderAdminOrders();
          });
        } catch (e) {}
      }

      // FULL payable bill (items + delivery − coupon) on the success screen.
      var payInfo = (function () {
        var b = cartBilling();
        var note = b.delivery > 0 ? (' + ଡେଲିଭରୀ Rs.' + money(b.delivery)) : ' (ଡେଲିଭରୀ ମାଗଣା)';
        if (b.discount > 0) note += ' − କୁପନ୍ Rs.' + money(b.discount);
        return 'Rs.' + money(b.total) + note;
      })();

      cart = {};
      appliedCoupon = '';
      clearCartCache_();
      render();
      renderBar();
      updateCartBadge();
      renderCartDrawer();
      document.getElementById('successOrderId').innerText = orderObj.order_id;
      document.getElementById('successOrderPay').innerText = payInfo;
      setStep('Success');
      scrollToStep_('stepSuccess');
    }

    // --- STORE PICKUP stock hold/restore on the LOCAL catalog (offline-safe) ---
    // Deduct the ordered quantities now (a hold) so nobody oversells the same
    // unit while the pickup is pending. The Sheet does the same server-side.
    function applyLocalStockHold_(lines) {
      if (!Array.isArray(lines) || !lines.length) return;
      lines.forEach(function (l) {
        var p = window.STORE_CATALOG.find(function (x) { return String(x.ean) === String(l.ean); });
        if (!p) return;
        var cur = parseInt(p.stock_qty, 10);
        if (isNaN(cur)) cur = 10;
        p.stock_qty = String(Math.max(0, cur - (parseInt(l.qty, 10) || 0)));
      });
      try { localStorage.setItem('munna_store_catalog', JSON.stringify(window.STORE_CATALOG)); } catch (e) {}
      try { renderAdminList(); } catch (e) {}
    }

    // Give the quantities back to the LOCAL catalog (admin Reject / expiry).
    // Idempotent per order via the local stock_restored flag.
    function restoreLocalStockForOrder_(order) {
      if (!order || order.stock_restored === 'Y') return false;
      if (String(order.delivery_type || '') !== 'Store Pickup') return false;
      var lines = Array.isArray(order.items) && order.items.length && order.items[0] && order.items[0].ean
        ? order.items
        : parseItemsText(order.items_text || order.items || '');
      // Resolve EAN from name when only the text form is available.
      lines.forEach(function (l) {
        if (l.ean) return;
        var p = window.STORE_CATALOG.find(function (x) {
          return String(x.product_name || '').trim().toLowerCase() === String(l.name || '').trim().toLowerCase();
        });
        if (p) l.ean = String(p.ean);
      });
      var changed = false;
      lines.forEach(function (l) {
        if (!l.ean) return;
        var p = window.STORE_CATALOG.find(function (x) { return String(x.ean) === String(l.ean); });
        if (!p) return;
        var cur = parseInt(p.stock_qty, 10);
        if (isNaN(cur)) cur = 0;
        p.stock_qty = String(cur + (parseInt(l.qty, 10) || 0));
        changed = true;
      });
      if (changed) {
        try { localStorage.setItem('munna_store_catalog', JSON.stringify(window.STORE_CATALOG)); } catch (e) {}
        try { renderAdminList(); } catch (e) {}
      }
      order.stock_restored = 'Y';
      return true;
    }

    // Backend Restore (idempotent) + local catalog restore for a Store Pickup
    // order. Used by admin Reject and the expiry check.
    function restorePickupStock_(order, doneMsg) {
      if (!order) return;
      if (order.stock_restored !== 'Y') {
        postToSheet({ action: 'restore_pickup_stock', order_id: order.order_id });
        restoreLocalStockForOrder_(order);
        setMyOrders(getMyOrders()); // persist stock_restored = 'Y'
      }
      if (doneMsg) alert(doneMsg);
    }

    // ================= ONE PRODUCT ONE PAGE (Product Detail) =================
    var detailState = { ean: '', qty: 1 };
    var currentItem = null; // product object of the open detail screen (Share)

