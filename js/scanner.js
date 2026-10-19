/* munnamobile :: js/scanner.js  (extracted from index.html lines 5283-5612)
   barcode/QR scanner, quick bill POS, scan history */
    function openScanner(ctx) {
      scannerCtx = ctx || 'customer';
      document.getElementById('scannerModal').classList.remove('hidden');
      if (!(history.state && history.state.modal === 'scanner')) history.pushState({ modal: 'scanner' }, '', window.location.href);
      document.getElementById('scanResultBanner').classList.add('hidden');
      document.getElementById('quickBillBox').classList.toggle('hidden', scannerCtx !== 'quickbill');
      document.getElementById('scanManualInput').value = '';
      if (scannerCtx === 'quickbill') renderQuickBill();
      startCameraScan();
    }

    function closeScanner() {
      stopCameraScan();
      document.getElementById('scannerModal').classList.add('hidden');
    }

    function showScanBanner(main, actions) {
      var b = document.getElementById('scanResultBanner');
      b.innerHTML = '<div class="bg-emerald-50 border border-emerald-200 rounded-xl px-3 py-2 text-xs text-emerald-900 leading-snug">' + main + '</div>' +
        (actions ? '<div class="flex gap-2">' + actions + '</div>' : '');
      b.classList.remove('hidden');
    }

    function scannerBtn(label, fn) {
      return '<button onclick="' + fn + '" class="flex-1 py-2 rounded-xl bg-[#1864ab] hover:bg-blue-800 text-white text-[11px] font-extrabold">' + label + '</button>';
    }

    function startCameraScan() {
      var idle = document.getElementById('scannerIdleHint');
      if (typeof Html5Qrcode === 'undefined') {
        if (idle) { idle.style.display = ''; idle.innerText = '❌ Scanner library load ହେଲା ନାହିଁ — ଇଣ୍ଟରନେଟ୍ ଯାଞ୍ଚ କରି ତଳେ ଫଟୋ / EAN ବ୍ୟବହାର କରନ୍ତୁ'; }
        return;
      }
      if (idle) idle.style.display = 'none';
      if (camRunning) return;
      if (!html5Qr) html5Qr = new Html5Qrcode('scannerReader', { verbose: false });
      html5Qr.start(
        { facingMode: 'environment' },
        { fps: 10, qrbox: { width: 230, height: 160 } },
        function (decodedText) { onScanResult(decodedText); },
        function () {}
      ).then(function () { camRunning = true; })
       .catch(function () {
         camRunning = false;
         if (idle) { idle.style.display = ''; idle.innerText = '📷 Camera ଚାଲିଲା ନାହିଁ (permission / କ୍ୟାମେରା ନାହିଁ) — ତଳେ ଫଟୋ କିମ୍ବା ହାତରେ EAN ଲେଖନ୍ତୁ'; }
       });
    }

    function stopCameraScan() {
      if (html5Qr && camRunning) {
        var h = html5Qr;
        camRunning = false;
        h.stop().then(function () { try { h.clear(); } catch (e) {} html5Qr = null; }).catch(function () {});
      }
    }

    function scanPhotoFile(file) {
      if (typeof Html5Qrcode === 'undefined') { alert('❌ Scanner library load ହେଲା ନାହିଁ — ଇଣ୍ଟରନେଟ୍ ଯାଞ୍ଚ କରନ୍ତୁ।'); return; }
      var h = new Html5Qrcode('scannerReader', { verbose: false });
      var p;
      try {
        p = (typeof h.scanFileV2 === 'function') ? h.scanFileV2(file, false) : h.scanFile(file, false);
      } catch (e) { alert('❌ ସ୍କାନ୍ ତ୍ରୁଟି: ' + e.message); return; }
      p.then(function (r) {
        var txt = (typeof r === 'string') ? r : ((r && r.decodedText) || '');
        html5Qr = null;
        onScanResult(txt);
      }).catch(function () {
        alert('❌ ଫଟୋରେ ବାରକୋଡ୍ / QR ମିଳିଲା ନାହିଁ — ଅନ୍ୟ ଫଟୋ ଚେଷ୍ଟା କରନ୍ତୁ।');
        startCameraScan();
      });
    }
    (function wirePhotoScan_() {
      var f = document.getElementById('scanPhotoInput');
      if (!f || !f.addEventListener) return;
      f.addEventListener('change', function () {
        var file = f.files && f.files[0];
        f.value = '';
        if (!file) return;
        stopCameraScan();
        setTimeout(function () { scanPhotoFile(file); }, 400);
      });
    })();

    function manualScanGo() {
      var v = document.getElementById('scanManualInput').value.trim();
      if (!v) return;
      onScanResult(v);
    }

    // One scan/text -> route by context (customer search+cart, admin lookup, POS bill).
    function onScanResult(text) {
      text = String(text || '').trim();
      if (!text) return;
      // Debounce: the live camera re-fires the same QR every frame. Ignore a
      // repeat of the SAME code within 1s so one scan = exactly one add.
      var now = Date.now();
      if (text === _lastScanText && (now - _lastScanAt) < 1000) return;
      _lastScanText = text;
      _lastScanAt = now;
      if (scannerCtx === 'quickbill') { qbAddByCode(text); return; }
      var p = window.STORE_CATALOG.find(function (x) { return String(x.ean).trim() === text; });

      if (scannerCtx === 'customer') {
        if (p) {
          saveScanHistory_(p);
          handleSearch(p.product_name);                 // quick search: card visible
          var sEl = document.getElementById('searchInput');
          if (sEl) sEl.value = p.product_name;
          setQty(p.ean, (cart[p.ean] || 0) + 1);        // quick billing: one scan = one in cart
          showScanBanner(
            '<b>✅ ' + p.product_name + '</b><br>Rs.' + money(p.selling_price) + ' — କାର୍ଟରେ 1ଟି ଯୋଡ଼ାଗଲା',
            scannerBtn('📸 ଅନ୍ୟ ସ୍କାନ୍', 'startCameraScan()') +
            scannerBtn('🛒 କାର୍ଟ ଦେଖନ୍ତୁ', 'closeScanner(); openCartDrawer()'));
          return;
        }
        handleSearch(text);                              // name / QR text fallback search
        var s2 = document.getElementById('searchInput');
        if (s2) s2.value = text;
        showScanBanner('🔍 “' + text + '” ପାଇଁ ଖୋଜାଗଲା — ଫଳାଫଳ ତଳେ ଦେଖନ୍ତୁ',
          scannerBtn('📸 ଅନ୍ୟ ସ୍କାନ୍', 'startCameraScan()'));
        return;
      }

      // Admin / store side lookup
      if (p) {
        var dbEl = document.getElementById('dbSearchInput');
        if (dbEl) dbEl.value = text;
        renderAdminList();
        showScanBanner(
          '<b>' + p.product_name + '</b><br>Rs.' + money(p.selling_price) + ' · Stock: ' + (p.stock_qty || 0),
          scannerBtn('✏️ Edit', "editAdminProductByEan(decodeURIComponent('" + encodeURIComponent(String(p.ean)).replace(/'/g, '%27') + "'))") +
          scannerBtn('📸 ଅନ୍ୟ ସ୍କାନ୍', 'startCameraScan()'));
        return;
      }
      quickAddWithEanPrompt_(text);                     // unknown EAN -> quick add
    }

    // --- STORE-SIDE QUICK BILL (POS): scan -> running bill -> order in Sheet ---
    function qbAddByCode(code) {
      var p = window.STORE_CATALOG.find(function (x) { return String(x.ean).trim() === String(code).trim(); });
      if (!p) {
        showScanBanner('❌ EAN <b>' + code + '</b> କାଟାଲଗ୍‌ରେ ନାହିଁ — ପ୍ରଥମେ ଯୋଡ଼ନ୍ତୁ।',
          scannerBtn('➕ ପ୍ରୋଡକ୍ଟ ଯୋଡ଼ନ୍ତୁ', "quickAddWithEan(decodeURIComponent('" + encodeURIComponent(String(code)).replace(/'/g, '%27') + "'))") +
          scannerBtn('📸 ପୁଣି ସ୍କାନ୍', 'startCameraScan()'));
        return;
      }
      var line = qbLines.find(function (l) { return String(l.ean) === String(p.ean); });
      if (line) line.qty += 1;
      else qbLines.push({ ean: String(p.ean), name: p.product_name, price: parseFloat(p.selling_price) || 0, qty: 1 });
      renderQuickBill();
      showScanBanner('✅ <b>' + p.product_name + '</b> (+1) — Rs.' + money(p.selling_price),
        scannerBtn('📸 ଅନ୍ୟ ସ୍କାନ୍', 'startCameraScan()'));
    }

    function renderQuickBill() {
      var box = document.getElementById('qbItems');
      if (!box) return;
      var total = 0;
      if (!qbLines.length) {
        box.innerHTML = '<p class="text-[11px] text-slate-400 text-center py-1">ବାରକୋଡ୍ ସ୍କାନ୍ କରନ୍ତୁ — ଜିନିଷ ଏଠାରେ ଯୋଡ଼ିବ</p>';
      } else {
        box.innerHTML = qbLines.map(function (l, i) {
          total += l.price * l.qty;
          return '<div class="flex items-center justify-between text-[11px] bg-slate-50 px-2 py-1.5 rounded-lg gap-2">' +
            '<span class="font-bold flex-1 min-w-0 truncate">' + l.name + '</span>' +
            '<span class="flex items-center gap-1">' +
            '<button onclick="qbChange(' + i + ',-1)" class="w-5 h-5 rounded bg-slate-200 font-bold">-</button>' +
            '<b class="w-5 text-center">' + l.qty + '</b>' +
            '<button onclick="qbChange(' + i + ',1)" class="w-5 h-5 rounded bg-[#1864ab] text-white font-bold">+</button>' +
            '</span>' +
            '<span class="font-black text-[#1864ab] w-16 text-right">Rs.' + money(l.price * l.qty) + '</span>' +
            '<button onclick="qbRemove(' + i + ')" class="text-rose-600 font-bold px-1"><i class="ph-bold ph-trash"></i></button>' +
            '</div>';
        }).join('');
      }
      var t = document.getElementById('qbTotal');
      if (t) t.innerText = 'Rs.' + money(total);
    }

    function qbChange(i, d) {
      if (!qbLines[i]) return;
      qbLines[i].qty += d;
      if (qbLines[i].qty <= 0) qbLines.splice(i, 1);
      renderQuickBill();
    }

    function qbRemove(i) { qbLines.splice(i, 1); renderQuickBill(); }

    async function saveQuickBill() {
      if (!qbLines.length) { alert('⚡ ଖାଲି ବିଲ୍ — ପ୍ରଥମେ ବାରକୋଡ୍ ସ୍କାନ୍ କରନ୍ତୁ!'); return; }
      var name = (((document.getElementById('qbName') || {}).value) || '').trim() || 'Store Walk-in';
      var phone = (((document.getElementById('qbPhone') || {}).value) || '').trim();
      var lines = qbLines.slice();
      var total = lines.reduce(function (a, l) { return a + l.price * l.qty; }, 0);
      var orderObj = {
        order_id: makeOrderId(),
        timestamp: sheetTimestamp(new Date()),
        customer_name: name,
        phone: phone,
        address: 'Store counter (in-store billing)',
        items: itemsToText(lines),
        total: total,
        channel: 'webapp',
        telegram_user_id: '',
        status: 'pending',
        status_msg_id: '',
        location: '',
        payment: 'COD',
        lines: lines
      };
      // Sheet-FIRST, like placeMiniOrder: no local insert, no "saved" alert
      // until the row is verified in the Orders tab.
      if (!sheetHookUrl('action=GET_ORDERS')) {
        renderAdminSyncStatus_('Sheet webhook ସେଟ୍ କରନ୍ତୁ — bill ସେଭ୍ ହେଲା ନାହିଁ', false);
        alert('❌ Google Sheet webhook ସେଟ୍ ହୋଇନାହିଁ! Admin → Sheets ଟ୍ୟାବ୍ ରେ Webhook URL ଲଗାନ୍ତୁ।');
        return;
      }
      renderAdminSyncStatus_('Saving bill to Google Sheet...', true);
      var saveRes = null;
      try {
        // Direct create_order — the server repairs the header itself (no
        // separate repair pre-call; that raced with the write and lost rows).
        saveRes = await postToSheet({ action: 'create_order', order: orderObj });
      } catch (saveErr) {
        saveRes = { ok: false, json: null };
      }
      var saveBody = (saveRes && saveRes.json) || {};
      var savedOk = !!(saveRes && saveRes.ok &&
        (saveBody.status === 'success' || saveBody.type === 'order_recorded' || saveBody.type === 'order_updated'));
      if (!savedOk) {
        renderAdminSyncStatus_('Sheet bill save failed', false);
        alert('❌ Bill Sheet କୁ ଗଲା ନାହିଁ: ' + ((saveBody && saveBody.error) || 'save failed') + '। ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ।');
        return;
      }
      // Verified mirror + fresh pull, then the success alert.
      try {
        await syncOrdersFromSheet(true);
        var okRow = await verifyOrderOnSheet_(String(saveBody.order_id || orderObj.order_id || ''));
        if (!okRow) {
          renderAdminSyncStatus_('Sheet save pending — retry from Orders', false);
          alert('⚠️ Bill Sheet ରେ ଦେଖାଯାଉ ନାହିଁ। ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ।');
          return;
        }
      } catch (verifyErr) {
        renderAdminSyncStatus_('Sheet save pending — retry from Orders', false);
        alert('⚠️ Bill verify ହେଲା ନାହିଁ। ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ।');
        return;
      }
      try {
        var verifiedBills = getMyOrders();
        verifiedBills.unshift(orderObj);
        setMyOrders(verifiedBills);
      } catch (mirrorErr) {}
      renderAdminSyncStatus_('Bill saved to Google Sheet', true);
      qbLines = [];
      var nEl = document.getElementById('qbName'); if (nEl) nEl.value = '';
      var pEl = document.getElementById('qbPhone'); if (pEl) pEl.value = '';
      renderQuickBill();
      renderAdminOrders();
      alert('✅ ବିଲ୍ ସେଭ୍ ହେଲା!\nOrder: ' + orderObj.order_id + '\nମୋଟ: Rs.' + money(total) + '\n(ଷ୍ଟକ୍ ନିଜେ କମିବ)');
      closeScanner();
      switchAdminTab('orders');
    }

    // Unknown EAN on the store side -> offer instant "add product" with EAN pre-filled.
    function quickAddWithEanPrompt_(ean) {
      showScanBanner('🆕 EAN <b>' + ean + '</b> କାଟାଲଗ୍‌ରେ ନାହିଁ',
        scannerBtn('➕ ନୂଆ ପ୍ରୋଡକ୍ଟ ଯୋଡ଼ନ୍ତୁ', "quickAddWithEan(decodeURIComponent('" + encodeURIComponent(String(ean)).replace(/'/g, '%27') + "'))") +
        scannerBtn('📸 ପୁଣି ସ୍କାନ୍', 'startCameraScan()'));
    }

    function quickAddWithEan(ean) {
      closeScanner();
      switchAdminTab('stock');
      resetAdminForm();
      var el = document.getElementById('newProdEan');
      if (el) el.value = ean;
      var n = document.getElementById('newProdName');
      if (n && n.focus) n.focus();
      var card = document.getElementById('adminFormCard');
      if (card && card.scrollIntoView) card.scrollIntoView({ behavior: 'smooth' });
      alert('📋 EAN ବସିଗଲା — ନାଁ, ଦାମ୍, ଫଟୋ ଦେଇ Save କରନ୍ତୁ।');
    }

    function editAdminProductByEan(ean) {
      var idx = window.STORE_CATALOG.findIndex(function (x) { return String(x.ean) === String(ean); });
      if (idx < 0) return;
      closeScanner();
      switchAdminTab('stock');
      editAdminProduct(idx);
    }

    // --- SAVED SCANS (customer): recent EAN/Photo scans re-usable in one tap ---
    function saveScanHistory_(p) {
      try {
        var h = JSON.parse(localStorage.getItem('MUNA_SCAN_HISTORY') || '[]');
        if (!Array.isArray(h)) h = [];
        h = h.filter(function (x) { return String(x.ean) !== String(p.ean); });
        h.unshift({ ean: String(p.ean), name: p.product_name, price: p.selling_price, ts: Date.now() });
        localStorage.setItem('MUNA_SCAN_HISTORY', JSON.stringify(h.slice(0, 10)));
      } catch (e) {}
      renderScanHistory();
    }

    function renderScanHistory() {
      var row = document.getElementById('scanHistoryRow');
      if (!row) return;
      var h = [];
      try { h = JSON.parse(localStorage.getItem('MUNA_SCAN_HISTORY') || '[]') || []; } catch (e) {}
      if (!h.length) { row.classList.add('hidden'); row.innerHTML = ''; return; }
      row.classList.remove('hidden');
      row.innerHTML = '<span class="shrink-0 text-slate-400 font-bold py-1" title="ସାମ୍ପ୍ରତିକ ସ୍କାନ୍">🕘</span>' +
        h.map(function (x) {
          return '<button onclick="quickSearchFromHistory(\'' + encodeURIComponent(String(x.name)).replace(/'/g, '%27') + '\')" class="shrink-0 px-2.5 py-1 bg-white border border-slate-200 rounded-full text-[10px] font-bold text-slate-600 whitespace-nowrap">' + x.name + '</button>';
        }).join('');
    }

    function quickSearchFromHistory(name) {
      var n = decodeURIComponent(name);
      var el = document.getElementById('searchInput');
      if (el) el.value = n;
      handleSearch(n);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    // ================= CUSTOMER LOGIN & USER PANEL =================
    // Session: { name, phone, tg_id, provider } in 'active_store_user'
    // (the legacy 'MUNA_USER_SESSION' key is read as a fallback so older
    // sessions still resolve). Written on every WhatsApp/web login.
