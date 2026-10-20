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

        // PIN-IN-CENTRE PICKER: the map itself is the chooser. The red pin
        // overlay stays fixed in the CENTRE; panning the map moves the point.
        // Every pan/zoom ends -> the centre lat/lng is auto-read and the ROAD
        // distance is recalculated automatically (see updateMapDistance).
        deliveryMarker = L.marker([STORE_LAT, STORE_LNG], { opacity: 0, draggable: false }).addTo(checkoutMap);
        var pinHost = document.getElementById('checkoutDeliveryMap');
        if (pinHost && !document.getElementById('mapCenterPin')) {
          var pin = document.createElement('div');
          pin.id = 'mapCenterPin';
          pin.style.cssText = 'position:absolute;left:50%;top:50%;transform:translate(-50%,-100%);pointer-events:none;z-index:1000;';
          pin.innerHTML = '<svg viewBox="0 0 24 24" width="30" height="30" fill="#dc2626" stroke="#7f1d1d" stroke-width="1"><path d="M12 2C7.9 2 4.5 5.3 4.5 9.4 4.5 14.6 12 22 12 22s7.5-7.4 7.5-12.6C19.5 5.3 16.1 2 12 2z"/><circle cx="12" cy="9.3" r="2.8" fill="#fff"/></svg>';
          pinHost.appendChild(pin);
        }
        checkoutMap.on('moveend', function () {
          var c = checkoutMap.getCenter();
          updateMapDistance(c.lat, c.lng);
        });

        // GPS locate CONTROL (crosshair icon, top-right ON the map) — one tap
        // jumps to the customer's current location + drops the pin there.
        var gpsCtl = L.control({ position: 'topright' });
        gpsCtl.onAdd = function () {
          var btn = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
          btn.innerHTML = '<a href="#" title="ମୋ ଲୋକେସନ୍ ଦେଖାଅ" ' +
            'style="width:30px;height:30px;line-height:30px;text-align:center;padding:0;">' +
            '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="#1d4ed8" ' +
            'stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="6"/>' +
            '<path d="M12 1.5v4M12 18.5v4M1.5 12h4M18.5 12h4"/></svg></a>';
          btn.style.cursor = 'pointer';
          L.DomEvent.disableClickPropagation(btn);
          L.DomEvent.on(btn, 'click', function (e) {
            L.DomEvent.stop(e);
            locateUserGPS();
          });
          return btn;
        };
        gpsCtl.addTo(checkoutMap);
      }
      setTimeout(() => { checkoutMap.invalidateSize(); }, 300);
      updateMapDistance(STORE_LAT, STORE_LNG);
      // Show the customer's CURRENT LOCATION point automatically (blue dot +
      // pin) the first time the map opens. Silent on permission denial — the
      // shop marker + draggable pin stay usable; the GPS button retries.
      if (!window._munnaGeoTried) {
        window._munnaGeoTried = true;
        if (navigator.geolocation) {
          navigator.geolocation.getCurrentPosition(function (pos) {
            var la = pos.coords.latitude, ln = pos.coords.longitude;
            if (checkoutMap) checkoutMap.setView([la, ln], 15);
            markCurrentLocation_(la, ln);
            updateMapDistance(la, ln);
          }, function () { /* denied — keep shop default view */ },
          { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 });
        }
      }
    }

    // Blue "current location" dot (separate from the draggable delivery pin).
    function markCurrentLocation_(la, ln) {
      try {
        if (!checkoutMap) return;
        if (window._munnaGeoDot) {
          window._munnaGeoDot.setLatLng([la, ln]);
        } else {
          window._munnaGeoDot = L.circleMarker([la, ln], {
            radius: 8, color: '#1d4ed8', fillColor: '#3b82f6',
            fillOpacity: 0.9, weight: 2
          }).addTo(checkoutMap).bindPopup('<b>ଆପଣଙ୍କ ବର୍ତ୍ତମାନର ଲୋକେସନ୍</b><br>Current location');
        }
      } catch (eDot) {}
    }

    function updateMapDistance(lat, lng) {
      customerLat = lat;
      customerLng = lng;
      deliveryMarker.setLatLng([lat, lng]);
      var straight = calcDistance(STORE_LAT, STORE_LNG, lat, lng);
      // Instant feedback with the straight-line figure while the router runs.
      applyDistanceToMap_(straight, 'ସିଧା ' + straight + ' KM · ରାସ୍ତା ହିସାବ ହେଉଛି...');
      window._munnaDistTok = (window._munnaDistTok || 0) + 1;
      var tok = window._munnaDistTok;
      roadDistanceKm_(lat, lng).then(function (road) {
        // Ignore stale answers after a newer drag / GPS fix.
        if (tok !== window._munnaDistTok) return;
        if (road != null) applyDistanceToMap_(road, 'ସିଧା ' + straight + ' KM');
      });
    }

    // ROAD distance (door-to-door) via the public OSRM demo router — no API
    // key, CORS-open. Returns kilometres (1 decimal) or null when unreachable
    // (offline / router down) so the caller keeps the straight-line figure.
    function roadDistanceKm_(lat, lng) {
      var url = 'https://router.project-osrm.org/route/v1/driving/' +
        STORE_LNG + ',' + STORE_LAT + ';' + lng + ',' + lat +
        '?overview=false&alternatives=false&steps=false';
      var ctrl = null, timer = null;
      try {
        ctrl = new AbortController();
        timer = setTimeout(function () { try { ctrl.abort(); } catch (e) {} }, 8000);
      } catch (e) {}
      return fetch(url, ctrl ? { signal: ctrl.signal } : undefined)
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (timer) clearTimeout(timer);
          var m = j && j.routes && j.routes[0] && j.routes[0].distance;
          return (typeof m === 'number' && m > 0) ? Math.round((m / 1000) * 10) / 10 : null;
        })
        .catch(function () {
          if (timer) clearTimeout(timer);
          return null;
        });
    }

    // Paint the distance pill + enable/disable the next step. The ZONE CHECK
    // uses the road distance when available (straight-line is the fallback).
    function applyDistanceToMap_(dist, extra) {
      var pill = document.getElementById('mapStatusPill');
      var btn = document.getElementById('btnProceedToPayment');
      var tail = extra ? (' — ' + extra) : '';
      if (dist <= MAX_RADIUS_KM) {
        isWithinRadius = true;
        pill.className = "text-[10px] font-bold p-1.5 rounded-lg mb-2 text-center bg-emerald-50 text-emerald-800 border border-emerald-200";
        pill.innerText = `✅ ରାସ୍ତା ଦୂରତା: ${dist} KM${tail} (୩ କି.ମି. ଭିତରେ)`;
        btn.disabled = false;
        btn.classList.remove('opacity-50', 'cursor-not-allowed');
      } else {
        isWithinRadius = false;
        pill.className = "text-[10px] font-bold p-1.5 rounded-lg mb-2 text-center bg-rose-50 text-rose-800 border border-rose-200";
        pill.innerText = `❌ ରାସ୍ତା ଦୂରତା: ${dist} KM${tail} (୩ କି.ମି. ବାହାରେ ଡେଲିଭରୀ ନାହିଁ)`;
        btn.disabled = true;
        btn.classList.add('opacity-50', 'cursor-not-allowed');
      }
    }

    function locateUserGPS() {
      if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(pos => {
          checkoutMap.setView([pos.coords.latitude, pos.coords.longitude], 15);
          markCurrentLocation_(pos.coords.latitude, pos.coords.longitude);
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
      var isPickupSel = selectedDeliveryType === 'pickup';
      // Method-aware admin charges: Home Delivery = delivery fee (free above
      // the admin threshold); Store Pickup = pickup fee when the toggle is ON.
      // Never both — the chosen method decides which charge applies.
      var delivery = (!isPickupSel && subtotal > 0 && (freeAbove <= 0 || subtotal < freeAbove))
        ? (shopConfig.deliveryFee || 0) : 0;
      var pickup = (isPickupSel && shopConfig.pickupChargeOn && subtotal > 0)
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
      // First bill (cart page) = items ONLY (− coupon). The delivery/pickup
      // charge is shown on the FINAL Payment step only, after the address.
      var disRow = document.getElementById('summaryDiscountRow');
      if (disRow) disRow.classList.toggle('hidden', b.discount <= 0);
      set('summaryDiscount', '-Rs.' + money(b.discount));
      set('summaryTotal', 'Rs.' + money(Math.max(0, b.subtotal - b.discount)));
      updatePaymentBill();
    }

    // Payment step (Step 3): live bill for the CHOSEN delivery method —
    // COD -> admin delivery charge; Store Pickup -> admin pickup charge.
    function updatePaymentBill() {
      var b = cartBilling();
      var set = function (id, v) { var el = document.getElementById(id); if (el) el.innerText = v; };
      set('paySubtotal', 'Rs.' + money(b.subtotal));
      var charge = selectedDeliveryType === 'pickup' ? (b.pickup || 0) : (b.delivery || 0);
      set('payChargeLabel', selectedDeliveryType === 'pickup' ? '🛍️ Pickup ଚାର୍ଜ:' : '🚚 ଡେଲିଭରୀ ଚାର୍ଜ:');
      set('payCharge', charge > 0 ? ('Rs.' + money(charge)) : 'ମାଗଣା ✅');
      var disRow = document.getElementById('payDiscountRow');
      if (disRow) disRow.classList.toggle('hidden', !(b.discount > 0));
      set('payDiscount', '-Rs.' + money(b.discount));
      set('payTotal', 'Rs.' + money(b.total));
    }

    // Full-screen waiting screen while the order save + Sheet verification
    // round-trip runs (can take several seconds — never hang silently).
    function showOrderLoading_(on, msg) {
      var ov = document.getElementById('orderLoadingOverlay');
      if (!ov) return;
      ov.classList.toggle('hidden', !on);
      try { document.documentElement.style.overflow = on ? 'hidden' : ''; } catch (e) {}
      var st = document.getElementById('orderLoadingStep');
      if (st && msg) st.textContent = msg;
      if (window._munnaOrderLoadTimer) {
        clearTimeout(window._munnaOrderLoadTimer);
        window._munnaOrderLoadTimer = null;
      }
      if (on) {
        if (navigator.vibrate) { try { navigator.vibrate(30); } catch (eV) {} }
        // Safety net: never leave the overlay stuck if something throws.
        window._munnaOrderLoadTimer = setTimeout(function () { showOrderLoading_(false); }, 90000);
      }
    }

    // Home + Cart: show the admin-set home-delivery timing
    // (Today/Tomorrow · 30 min | 1 hour | 2 hour | 3 hour | 5 hour).
    // ALSO: Store Service Hours — auto-changing Open/Closed icons on Home,
    // and hide Delivery / Pickup boxes when the store is CLOSED.
    function renderDeliveryTiming() {
      var day = (shopConfig.deliveryDay === 'Tomorrow') ? 'ଆଉକାଲି' : 'ଆଜି';
      var txt = day + ' · ' + (shopConfig.deliverySlot || '1 hour');
      var set = function (id) {
        var el = document.getElementById(id);
        if (el) el.textContent = txt;
      };
      set('homeBoxTiming');      // Home page chip: Home Delivery + time
      set('cartTimingText');     // Cart page summary card
      set('payTimingText');      // Cart page -> Payment step (Home Delivery option)

      // --- STORE OPEN/CLOSED STATUS --------------------------------------
      var openNow = isStoreOpenNow_();
      var storeIcon = document.getElementById('homeStoreIcon');
      var storeLabel = document.getElementById('homeStoreLabel');
      var storeStatusBox = document.getElementById('homeStoreStatusBox');

      if (storeStatusBox) {
        if (openNow) {
          storeStatusBox.className = 'rounded-xl bg-emerald-50 border border-emerald-200 px-2 py-1.5 flex items-center gap-1.5 shadow-xs';
          storeIcon.innerHTML = '<i class="ph-bold ph-power text-emerald-600"></i>';
          storeLabel.innerHTML = '<span class="block text-[10px] font-black text-emerald-800">Store Open</span><span class="block text-[9px] font-bold text-emerald-600">ଦୋକାନ ଖୋଲାଅଛି ✅</span>';
        } else {
          storeStatusBox.className = 'rounded-xl bg-rose-50 border border-rose-200 px-2 py-1.5 flex items-center gap-1.5 shadow-xs';
          storeIcon.innerHTML = '<i class="ph-bold ph-power text-rose-600"></i>';
          storeLabel.innerHTML = '<span class="block text-[10px] font-black text-rose-800">Store Closed</span><span class="block text-[9px] font-bold text-rose-600">ଦୋକାନ ବନ୍ଦ ଅଛି ❌</span>';
        }
      }

      // Calculated expected delivery / pickup times (Open time + slot).
      var slotMins = slotToMinutes_(shopConfig.deliverySlot || '1 hour');
      var expText = expectedTimeText_(slotMins);
      var homeBoxTimingEl = document.getElementById('homeBoxTiming');
      if (homeBoxTimingEl) {
        homeBoxTimingEl.innerHTML = txt + '<br><span class="text-[8px] font-bold text-orange-600">ଡେଲିଭରୀ ଆସିବ: ' + expText + '</span>';
      }
      var pickupStateEl = document.getElementById('homePickupState');
      if (pickupStateEl) {
        pickupStateEl.innerHTML = 'ପିକଅପ୍ ଆସିବ: ' + expText;
      }

      // Home page chips: HIDE entirely when the admin turned that option OFF
      // OR when the store is CLOSED (both delivery & pickup unavailable).
      var closed = !openNow;
      var delBox = document.getElementById('homeDeliveryBox');
      if (delBox) delBox.classList.toggle('hidden', shopConfig.codActive === false || closed);
      var pkBox = document.getElementById('homePickupBox');
      if (pkBox) pkBox.classList.toggle('hidden', shopConfig.pickupActive === false || closed);
      // Always show the store status box (open or closed) — auto-changing icon.
      var storeBox = document.getElementById('homeStoreStatusBox');
      if (storeBox) storeBox.classList.remove('hidden');
    }

    // --- CUSTOMER CLOCK (AM/PM) + EXPECTED-BY HELPERS -------------------------
    function fmtClock_(d) {
      var h = d.getHours(), m = d.getMinutes();
      var ap = h >= 12 ? 'PM' : 'AM';
      var h12 = h % 12; if (h12 === 0) h12 = 12;
      return h12 + ':' + (m < 10 ? '0' : '') + m + ' ' + ap;
    }
    // "30 min" | "1 hour" | "2 hour" | "3 hour" | "5 hour" -> minutes.
    function slotToMinutes_(slot) {
      var s = String(slot || '').toLowerCase().trim();
      if (s.indexOf('30') === 0) return 30;
      if (s.indexOf('2') === 0) return 120;
      if (s.indexOf('5') === 0) return 300;
      if (s.indexOf('3') === 0) return 180;
      if (s.indexOf('1') === 0) return 60;
      return 60;
    }
    // Robust order-timestamp parse: Sheet Date strings, GMT strings and
    // "yyyy-MM-dd HH:mm:ss" all work (Safari needs the T-separator fallback).
    function parseOrderTs_(o) {
      var t = (o && (o.date || o.timestamp)) || '';
      if (!t) return 0;
      try {
        var d = new Date(t);
        if (isNaN(d.getTime())) d = new Date(String(t).replace(' ', 'T'));
        return isNaN(d.getTime()) ? 0 : d.getTime();
      } catch (eT) { return 0; }
    }
    // "ଆଜି 3:45 PM" (or the full date) for an order card.
    function orderPlacedText_(o) {
      var ts = parseOrderTs_(o);
      if (!ts) return '—';
      var d = new Date(ts), now = new Date();
      var sameDay = d.getDate() === now.getDate() && d.getMonth() === now.getMonth() &&
                    d.getFullYear() === now.getFullYear();
      return (sameDay ? 'ଆଜି ' : d.toLocaleDateString('en-IN') + ' ') + fmtClock_(d);
    }
    // Expected completion time = placed time + the admin slot
    // (Home Delivery Timing for COD orders, the pickup slot for Store Pickup).
    function orderExpectedText_(o) {
      var ts = parseOrderTs_(o);
      if (!ts) return '—';
      // Live tracking: the server-stored expected_by wins (same value on the
      // customer card, admin panel and Telegram message); fall back to the
      // local slot math for old orders that have no expected_by yet.
      var expRaw = String((o && (o.expected_by || o.expectedBy)) || '').trim();
      var expTs = expRaw ? parseStampStr_(expRaw) : 0;
      if (expTs) {
        var expD = new Date(expTs), nowE = new Date();
        var crossE = expD.getDate() !== nowE.getDate() || expD.getMonth() !== nowE.getMonth();
        return fmtClock_(expD) + (crossE ? ' (ଆଉକାଲି)' : '');
      }
      var isPk = String((o && o.delivery_type) || (o && o.payment) || '')
        .toLowerCase().indexOf('pickup') >= 0;
      var mins = slotToMinutes_(isPk ? ((o && o.pickup_slot) || '30 min')
                                     : (shopConfig.deliverySlot || '1 hour'));
      var exp = new Date(ts + mins * 60000), now = new Date();
      var crosses = exp.getDate() !== now.getDate() || exp.getMonth() !== now.getMonth();
      return fmtClock_(exp) + (crosses ? ' (ଆଉକାଲି)' : '') + ' · ' + mins + ' ମିନିଟ୍';
    }

    // --- LIVE ORDER TRACKING time helpers (shared by Orders + Admin panel) ---
    // Parse any stamp the Sheet carries: JS "YYYY-MM-DD HH:mm:ss" (what our
    // sheetTimestamp writes) AND Google-Sheets Date objects that arrive as
    // "Mon Oct 20 2026 14:35:12 GMT+0530".
    function parseStampStr_(s) {
      s = String(s || '').trim();
      if (!s) return 0;
      try {
        var d = new Date(s);
        if (!isNaN(d.getTime())) return d.getTime();
        d = new Date(s.replace(' ', 'T'));
        return isNaN(d.getTime()) ? 0 : d.getTime();
      } catch (eS) { return 0; }
    }
    // "3:45 PM" for a stage timestamp cell (or "" when not yet reached).
    function fmtStampClock_(s) {
      var t = parseStampStr_(s);
      return t ? fmtClock_(new Date(t)) : '';
    }
    // One tracking row for the stepper: icon + Odia label + the live time.
    // `time` is "" while the stage has not been reached yet.
    function trackRow_(icon, label, time) {
      return '<div class="flex items-start justify-between gap-2 py-1">' +
        '<span class="text-[11px] font-bold text-slate-600 shrink-0">' + icon + ' ' + label + '</span>' +
        '<span class="text-[11px] font-black ' + (time ? 'text-emerald-700' : 'text-slate-300') + '">' +
        (time || '⏳ ଅପେକ୍ଷା...') + '</span></div>';
    }
    // Full tracking UI block (5 stages, every stage time when it exists).
    function renderOrderTrackHtml_(o) {
      if (!o) return '';
      var placed = fmtStampClock_(o.placed_at || o.timestamp || o.date);
      var expected = orderExpectedText_(o);
      var accepted = fmtStampClock_(o.accepted_at);
      var packed = fmtStampClock_(o.packed_at);
      var outFor = fmtStampClock_(o.out_for_delivery_at);
      var delivered = fmtStampClock_(o.delivered_at);
      var cancelled = fmtStampClock_(o.cancelled_at);
      var isPickup = String(o.delivery_type || '') === 'Store Pickup';
      return '<div class="bg-slate-50 rounded-xl border border-slate-200 p-2.5 mt-1.5 space-y-0.5">' +
        trackRow_('🕐', 'ଅର୍ଡର୍ ସମୟ (Ordered)', placed) +
        trackRow_('⏳', 'ଆଶାିତ ସମୟ (Expected)', expected) +
        trackRow_('✅', 'ଗ୍ରହଣ ସମୟ (Accepted)', accepted) +
        trackRow_('📦', 'ପ୍ୟାକିଂ ସମୟ (Packed)', packed) +
        trackRow_(isPickup ? '🏪' : '🚚', isPickup ? 'ପିକଅପ୍ ସମୟ (Pickup)' : 'ଡେଲିଭରୀ ବାହାରିବା (Out)', outFor) +
        trackRow_('🎉', isPickup ? 'ପିକଅପ୍ ହେଲା (Delivered)' : 'ଡେଲିଭରୀ ସମୟ (Delivered)', delivered) +
        (cancelled ? trackRow_('❌', 'ବାତିଲ୍ ସମୟ (Cancelled)', cancelled) : '') +
        '</div>';
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
      cacheCart_(); // items + coupon survive a refresh (local mirror)
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
      // Expected-by: Store Pickup = order time + chosen slot; Home Delivery =
      // order time + the admin's Home Delivery Timing slot. Stored on the
      // Sheet row so the customer, admin and Telegram all show the SAME clock.
      var expectedByStamp = sheetTimestamp(new Date(Date.now() +
        slotToMinutes_(isPickup ? (pickupSlot || '30 min')
                                : (shopConfig.deliverySlot || '1 hour')) * 60000));

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
      // AUTHORITATIVE total = exactly what the bill on screen shows:
      // items + the chosen method's admin charge (delivery OR pickup) − coupon.
      try { updateSlotVisibility_(); } catch (eSync) {}
      total = cartBilling().total;
      // Waiting screen (store logo + spinner) while the Sheet save + verify
      // round-trip runs — hidden again on every exit path below.
      showOrderLoading_(true, 'ଅର୍ଡର୍ ନିଶ୍ଚିତ କରୁଛୁ... (order confirmation in progress)');

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
        expected_by: expectedByStamp,
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

      // ONE path only: the Apps Script webhook writes the Sheet row, and the
      // bot's web_sync loop alerts Telegram + tracks status. (The Telegram
      // Mini App sendData() path was REMOVED — this is our own HTML shop.)
      sendToBot({
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
      // Browser/webapp orders: write the Sheet row FIRST and wait for the
      // verified server response. NOTHING below (success screen, stock hold,
      // cart clear) runs until the row is confirmed in the Orders tab.
      {
        var hookMissing = !sheetHookUrl('action=GET_ORDERS');
        if (hookMissing) {
          renderAdminSyncStatus_('Sheet webhook ସେଟ୍ କରନ୍ତୁ — order ସେଭ୍ ହେଲା ନାହିଁ', false);
          alert('⚠️ ଏବେ ଅର୍ଡର୍ କରିହେବ ନାହିଁ! କିଛି ସମୟ ପରେ ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ।');
          showOrderLoading_(false);
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
          alert('❌ ଅର୍ଡର୍ ପଠାଯାଇପାରିଲା ନାହିଁ! ଇଣ୍ଟରନେଟ୍ ଯାଞ୍ଚ କରି ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ (ଡୁପ୍ଲିକେଟ୍ ହେବ ନାହିଁ)।');
          showOrderLoading_(false);
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
          alert('⚠️ ଅର୍ଡର୍ ନିଶ୍ଚିତ ହୋଇନାହିଁ। ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ — ଦୁଇଥର ଟଙ୍କା କଟିବ ନାହିଁ।');
          showOrderLoading_(false);
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

      // Order CONFIRMED — hide the waiting screen and show the FULL order
      // details on the success card (id, name/phone, address, items, method).
      showOrderLoading_(false);
      var payInfo = 'Rs.' + money(orderObj.total) +
        (isPickup ? (' · Store Pickup' + (pickupSlot ? ' — ' + pickupSlot : ''))
                  : ' · ଡେଲିଭରୀ (COD)');

      cart = {};
      appliedCoupon = '';
      clearCartCache_();
      // Clear the SERVER basket too — otherwise the next refresh would hydrate
      // the just-placed order's items straight back into the cart.
      try { pushCartToCloud_(); } catch (ePC) {}
      render();
      renderBar();
      updateCartBadge();
      renderCartDrawer();
      var sSet = function (id, v) { var el = document.getElementById(id); if (el) el.innerText = v; };
      sSet('successOrderId', orderObj.order_id);
      sSet('successOrderPay', payInfo);
      sSet('successName', (name || '') + (phone ? ' · ' + phone : ''));
      sSet('successAddr', address || '—');
      sSet('successItems', orderObj.items || '—');
      sSet('successMethod', isPickup
        ? ('🏪 Store Pickup' + (pickupSlot ? ' — ' + pickupSlot : ''))
        : ('🚚 Home Delivery (COD)' +
           (shopConfig.deliveryDay === 'Tomorrow' ? ' — ଆଉକାଲି' : ' — ଆଜି')));
      // Placed-at + expected-by clocks (AM/PM): COD uses the admin Home
      // Delivery Timing slot, Store Pickup uses the chosen pickup slot.
      var placedMs = Date.now();
      var expMins = isPickup ? slotToMinutes_(pickupSlot || '30 min')
                              : slotToMinutes_(shopConfig.deliverySlot || '1 hour');
      var expDate = new Date(placedMs + expMins * 60000);
      var crossesMidnight = expDate.getDate() !== new Date(placedMs).getDate();
      sSet('successPlacedAt', 'ଆଜି ' + fmtClock_(new Date(placedMs)));
      sSet('successExpectedAt', fmtClock_(expDate) +
        (crossesMidnight ? ' (ଆଉକାଲି)' : '') + ' · ' + expMins + ' ମିନିଟ୍ ଭିତରେ');
      // Live tracking UI block on the success card.
      var trackBox = document.getElementById('successTrackBlock');
      if (trackBox && typeof renderOrderTrackHtml_ === 'function') {
        var orderForTrack = {
          id: orderObj.order_id, order_id: orderObj.order_id,
          timestamp: orderObj.timestamp, date: orderObj.timestamp,
          placed_at: orderObj.timestamp,
          expected_by: orderObj.expected_by || '',
          accepted_at: '', packing: '', packed_at: '',
          out_for_delivery_at: '', delivered_at: '', cancelled_at: '',
          status: 'pending', stage: 1,
          delivery_type: orderObj.delivery_type || '',
          pickup_slot: orderObj.pickup_slot || '',
          items: orderObj.items || [],
          total: orderObj.total
        };
        trackBox.innerHTML = renderOrderTrackHtml_(orderForTrack);
      }
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

