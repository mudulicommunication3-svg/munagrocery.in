/* munnamobile :: js/product-detail.js  (extracted from index.html lines 2165-2459)
   one-product page, toast, share, deep link */
    function openProductDetail(ean) {
      var p = window.STORE_CATALOG.find(function (x) { return String(x.ean) === String(ean); });
      if (!p) return;
      // Record product view for Live Viewers Tracker
      try { recordPageView(p.product_name); } catch (e) {}
      // Preserve the home grid scroll position for the Back button.
      window._homeScrollY = window.scrollY || window.pageYOffset || 0;
      detailState = { ean: String(p.ean), qty: Math.max(1, parseInt(cart[p.ean], 10) || 1) };
      currentItem = p; // exposed for shareCurrentProduct()

      var mrpVal = parseFloat(p.mrp) || 0;
      var spVal = parseFloat(p.selling_price) || 0;
      var off = mrpVal > spVal ? Math.round(((mrpVal - spVal) / mrpVal) * 100) : 0;
      var stockN = parseInt(p.stock_qty, 10); if (isNaN(stockN)) stockN = 10;
      // Server-hosted photo first (catalog image_url); same-origin server icon
      // as the guaranteed fallback — external CDNs may block or expire links.
      var fallback = 'icon-512.png';

      var img = document.getElementById('pdImage');
      img.src = p.image_url || fallback;
      img.alt = p.product_name;
      img.onerror = function () { this.src = fallback; };
      // No photo on the product? Auto-find one by EAN (OpenFoodFacts) and
      // swap it in as soon as it arrives.
      if (!p.image_url) {
        findProductImage_(p, function (u) {
          if (u && currentItem === p) img.src = u;
        });
      }

      var offB = document.getElementById('pdOffBadge');
      if (off > 0) { offB.classList.remove('hidden'); offB.innerText = off + '% OFF'; } else { offB.classList.add('hidden'); }

      document.getElementById('pdCategory').innerText = catOf(p);
      document.getElementById('pdTitle').innerText = p.product_name;
      document.getElementById('pdPrice').innerText = 'Rs.' + money(p.selling_price);
      document.getElementById('pdMrp').innerText = mrpVal > spVal ? ('Rs.' + money(p.mrp)) : '';
      document.getElementById('pdSavings').innerText = off > 0 ? ('ଆପଣ ବଞ୍ଚାଉଛନ୍ତି Rs.' + money(mrpVal - spVal)) : '';
      document.getElementById('pdRating').innerText = off > 0 ? ('ଏହା ଆପଣଙ୍କ ପାଇଁ ଉପଯୁକ୍ତ') : 'ଲୋକପ୍ରିୟ ପ୍ରୋଡକ୍ଟ';

      var stockEl = document.getElementById('pdStock');
      if (stockN <= 0) {
        stockEl.innerHTML = '<span class="text-xs font-extrabold text-rose-600">❌ ସରିଗଲା (Out of stock)</span>';
        document.getElementById('pdAddCartBtn').disabled = true;
        document.getElementById('pdBuyBtn').disabled = true;
        document.getElementById('pdAddCartBtn').classList.add('opacity-50', 'cursor-not-allowed');
        document.getElementById('pdBuyBtn').classList.add('opacity-50', 'cursor-not-allowed');
      } else {
        stockEl.innerHTML = stockN <= 5
          ? `<span class="text-xs font-extrabold text-amber-600">⚠️ କେବଳ ${stockN} ଟି ବଳିଛି</span>`
          : `<span class="text-xs font-semibold text-emerald-600">✅ ସ୍ଟକ୍ ଉପଲବ୍ଧ (${stockN} ଟି)</span>`;
        document.getElementById('pdAddCartBtn').disabled = false;
        document.getElementById('pdBuyBtn').disabled = false;
        document.getElementById('pdAddCartBtn').classList.remove('opacity-50', 'cursor-not-allowed');
        document.getElementById('pdBuyBtn').classList.remove('opacity-50', 'cursor-not-allowed');
      }

      document.getElementById('pdDesc').innerText =
        p.product_name + ' — ' + catOf(p) + ' ବର୍ଗର ଏହି ମାନଯୁକ୍ତ ପ୍ରୋଡକ୍ଟ ଆମ ଦୋକାନରେ ଉପଲବ୍ଧ। ' +
        'ପ୍ରତ୍ୟେକ ପ୍ୟାକେଟ୍ ତାଜା ଓ ଯାଞ୍ଚିତ। ' +
        (mrpVal > spVal ? ('ଏବେ ' + off + '% ରିହାତିରେ ମିଳୁଛି। ') : '') +
        '3 KM ମଧ୍ୟରେ ମାଗଣା ଡେଲିଭରୀ।';

      // ସମାନ କ୍ୟାଟେଗୋରୀର ଅନ୍ୟ ଆଇଟମ୍ (ନିଜକୁ ଛାଡ଼ି) — ଖାଲି ଥିଲେ ଅନ୍ୟ କ୍ୟାଟେଗୋରୀରୁ ଭର୍ତ୍ତି
      renderSimilarProducts_(p);

      document.getElementById('pdQty').innerText = detailState.qty;
      renderDetailBottomBar();
      document.getElementById('productDetailScreen').classList.remove('hidden');
      document.getElementById('productDetailScreen').scrollTop = 0;

      // Push a dedicated history state so the mobile hardware Back button
      // lands here first (closing this screen) instead of exiting the app.
      var detailUrl = '?item=' + encodeURIComponent(String(p.ean));
      var alreadyPushed = history.state && history.state.screen === 'product_detail' &&
        String(history.state.ean) === String(p.ean) &&
        window.location.search === detailUrl;
      if (!alreadyPushed) {
        history.pushState({ screen: 'product_detail', ean: String(p.ean) }, '', detailUrl);
      }
    }

    // syncHistory: pass false when called from the popstate handler — the
    // browser has ALREADY navigated one entry back, so calling
    // history.back() again would pop a second time (and could exit the app).
    // Called with no argument (UI back arrow / detailBuyNow), it also keeps
    // the URL + history stack in sync by stepping back over our pushed state.
    function closeProductDetail(syncHistory) {
      var pd = document.getElementById('productDetailScreen');
      if (!pd || pd.classList.contains('hidden')) return;
      pd.classList.add('hidden');
      // Restore the exact scroll position on the home grid.
      var y = window._homeScrollY || 0;
      window.scrollTo({ top: y, behavior: 'auto' });
      if (syncHistory !== false && history.state && history.state.screen === 'product_detail') {
        history.back(); // popstate then fires on an already-hidden screen -> no-op
      }
    }

    // ---- Universal hardware/browser Back navigation --------------------
    // One popstate handler unwinds whatever top-most layer is open, in a
    // fixed priority order (innermost first). Each branch closes its layer
    // and stops — it does NOT call history.back() again (the browser already
    // navigated one entry). This keeps the app alive on Back instead of
    // exiting, and preserves the home-catalog scroll position.
    var _backNavGuard = false;
    window.addEventListener('popstate', function () {
      // (0) 🚨 New Order Alert modal (top-most) — Back = Review (ack + close).
      var na = document.getElementById('newOrderAlertModal');
      if (na && !na.classList.contains('hidden')) { newOrderAlertAction('review'); return; }
      // (1) Scanner modal
      var scanner = document.getElementById('scannerModal');
      if (scanner && !scanner.classList.contains('hidden')) { closeScanner(); return; }
      // (2) Admin PIN modal
      var pin = document.getElementById('adminPinModal');
      if (pin && !pin.classList.contains('hidden')) { closeAdminPinModal(); return; }
      // (3) Admin dashboard
      var dash = document.getElementById('adminDashboard');
      if (dash && !dash.classList.contains('hidden')) { closeAdminDashboard(); return; }
      // (4) Login modal (+ optional WhatsApp-login modal if present)
      var login = document.getElementById('loginModal');
      if (login && !login.classList.contains('hidden')) { closeLoginModal(); return; }
      var waLogin = document.getElementById('waLoginModal');
      if (waLogin && !waLogin.classList.contains('hidden') && typeof closeWaLoginModal === 'function') { closeWaLoginModal(); return; }
      // (5) Product Detail screen -> back to home catalog (scroll preserved)
      var pd = document.getElementById('productDetailScreen');
      if (pd && !pd.classList.contains('hidden')) { closeProductDetail(false); return; }
      // (6) Cart / Orders / Address / Profile -> home
      var anyScreen = ['cart', 'orders', 'address', 'profile'].some(function (s) {
        var el = document.getElementById(s + 'Screen');
        return el && !el.classList.contains('hidden');
      });
      if (anyScreen) {
        _backNavGuard = true; // suppress showScreen('home') pushing a new entry
        try { showScreen('home'); } finally { _backNavGuard = false; }
        return;
      }
      // Nothing left open — let the browser/WebView handle it (exit/parent nav).
    });

    // Toggle sticky bottom bar: stepper vs Add-to-Cart button based on cart qty
    function renderDetailBottomBar() {
      var wrap = document.getElementById('pdStepperWrap');
      var btn = document.getElementById('pdAddCartBtn');
      var q = cart[detailState.ean] || 0;
      if (!wrap || !btn) return;
      if (q > 0) {
        wrap.classList.remove('hidden');
        btn.classList.add('hidden');
      } else {
        wrap.classList.add('hidden');
        btn.classList.remove('hidden');
      }
    }

    function detailQtyChange(delta) {
      var p = window.STORE_CATALOG.find(function (x) { return String(x.ean) === String(detailState.ean); });
      var max = p ? (parseInt(p.stock_qty, 10) || 1) : 1;
      detailState.qty = Math.max(1, Math.min(99, Math.min(detailState.qty + delta, max)));
      document.getElementById('pdQty').innerText = detailState.qty;
      setQty(detailState.ean, detailState.qty);
      renderDetailBottomBar();
    }

    function detailAddToCart() {
      if (!detailState.ean) return;
      setQty(detailState.ean, detailState.qty);
      renderDetailBottomBar();
      alert('✅ କାର୍ଟରେ ଯୋଡ଼ାଗଲା (' + detailState.qty + ' ଟି)! 🛒');
    }

    function detailBuyNow() {
      if (!detailState.ean) return;
      setQty(detailState.ean, detailState.qty);
      closeProductDetail();
      // Jump straight into the checkout flow.
      showScreen('cart');
      renderCartDrawer();
      goCheckoutAddress();
    }

    // Share: ଅଫିସିଆଲ୍ ସିଷ୍ଟମ୍ ସେୟାର ସିଟ୍ ପ୍ରଥମେ (WhatsApp, Facebook,
    // Gmail, Messages, Copy Link... ସବୁ ଆପ୍ ଆସିବ)। କୌଣସି WhatsApp-ଶୁଦ୍ଧା
    // ଡାଇରେକ୍ଟ ଲିଙ୍କ୍ ନାହିଁ — ସପୋର୍ଟ ନଥିଲେ ଲିଙ୍କ୍ କ୍ଲିପବୋର୍ଡରେ କପି ହେବ।
    function showToast(msg) {
      try {
        var old = document.getElementById('mgToast');
        if (old && old.parentNode) old.parentNode.removeChild(old);
        var t = document.createElement('div');
        t.id = 'mgToast';
        t.innerText = msg;
        t.style.cssText = 'position:fixed;left:50%;bottom:96px;transform:translateX(-50%);' +
          'background:#0f172a;color:#fff;font-size:12px;font-weight:700;' +
          'padding:10px 16px;border-radius:12px;z-index:9999;' +
          'box-shadow:0 8px 24px rgba(0,0,0,.25);max-width:88vw;text-align:center;';
        document.body.appendChild(t);
        setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 2200);
      } catch (e) { try { alert(msg); } catch (e2) {} }
    }

    // AUTO IMAGE FINDER: use the catalog (server) photo first; when a product
    // has NO photo (or a dead CDN link) look one up on OpenFoodFacts by EAN
    // and cache it in memory for this session — used by display AND share, so
    // a shared product link always carries its picture when one exists.
    function findProductImage_(p, cb) {
      window._munnaImgGuess = window._munnaImgGuess || {};
      var cached = p && (p.image_url || window._munnaImgGuess[String(p.ean)] || '');
      if (cached) { cb(cached); return; }
      if (!p || !p.ean) { cb(''); return; }
      var done = false;
      var finish = function (url) { if (done) return; done = true; cb(url || ''); };
      var t = setTimeout(function () { finish(''); }, 7000);
      try {
        fetch('https://world.openfoodfacts.org/api/v0/product/' +
              encodeURIComponent(String(p.ean)) + '.json')
          .then(function (r) { return r.json(); })
          .then(function (j) {
            clearTimeout(t);
            var u = (j && j.product && (j.product.image_front_url || j.product.image_url)) || '';
            if (u) window._munnaImgGuess[String(p.ean)] = u;
            finish(u);
          })
          .catch(function () { clearTimeout(t); finish(''); });
      } catch (e) { clearTimeout(t); finish(''); }
    }

    function shareCurrentProduct() {
      if (!currentItem || !currentItem.ean) return;
      var link = 'https://munagrocery.in/?item=' + currentItem.ean;
      // ଫଟୋ ବିନା CDN ଲିଙ୍କ୍ — ନେଟିଭ୍ ଆପ୍ ଫଟୋ ସିଧା ପଠାଇବ, WhatsApp ନିଜେ og:image କାର୍ଡ ଦେଖାଇବ
      var shareText = '🛒 Check out *' + currentItem.product_name + '* at Rs.' + money(currentItem.selling_price) + ' on MUNNA GROCERY:\n' + link;
      var shareData = {
        title: currentItem.product_name + ' - MUNNA GROCERY',
        text: shareText,
        url: link
      };
      // (1) ନେଟିଭ୍ ଆପ୍ ଭିତରେ: MunnaShare ବ୍ରିଜ୍ ଦେଇ ଫଟୋ ସହ ଅଫିସିଆଲ୍ ସିଟ୍ ଖୋଲେ
      try {
        if (window.MunnaShare && typeof window.MunnaShare.postImage === 'function') {
          shareViaNativeBridge_(shareText);
          return;
        }
      } catch (e) {}
      // (2) ବ୍ରାଉଜର: Web Share API — ସମ୍ଭବ ଥିଲେ ଫଟୋ attach କରି (ଲିଙ୍କ୍ + ଫଟୋ
      // ଏକାଥରେ), ନହେଲେ ଲିଙ୍କ୍ ସହ ଟେକ୍ସଟ୍। ଫଟୋ ନ ଥିଲେ ଆପେ ଖୋଜାଯିବ।
      if (navigator.share) {
        var sendPlain = function () {
          navigator.share(shareData).catch(function (err) {
            if (err && err.name !== 'AbortError') console.error('Share error:', err);
          });
        };
        findProductImage_(currentItem, function (imgUrl) {
          if (!imgUrl || !navigator.canShare) { sendPlain(); return; }
          try {
            fetch(imgUrl, { mode: 'cors' }).then(function (r) { return r.blob(); }).then(function (blob) {
              var file = new File([blob], String(currentItem.ean || 'product') + '.jpg',
                                  { type: blob.type || 'image/jpeg' });
              if (navigator.canShare({ files: [file] })) {
                navigator.share({ files: [file], text: shareText }).catch(function (err) {
                  if (err && err.name !== 'AbortError') sendPlain();
                });
              } else {
                sendPlain();
              }
            }).catch(function () { sendPlain(); });
          } catch (eF) { sendPlain(); }
        });
        return;
      }
      // (3) ଶେଷ ଉପାୟ: ଲିଙ୍କ୍ କ୍ଲିପବୋର୍ଡରେ କପି କର (WhatsApp-ଶୁଦ୍ଧା ରିଡାଇରେକ୍ଟ ନାହିଁ)
      var full = shareData.text + '\n' + shareData.url;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(full).then(function () {
          showToast('ଲିଙ୍କ୍ କପି ହୋଇଗଲା (Link Copied)!');
        }).catch(function () {
          showToast('Share not supported on this device.');
        });
      } else {
        showToast('Share not supported on this device.');
      }
    }

    // ନେଟିଭ୍ ବ୍ରିଜ୍ ସେୟାର: ପ୍ରୋଡକ୍ଟ ଫଟୋ fetch କରି base64 କରି Java କୁ ଦିଏ —
    // Java ତାକୁ FileProvider URI କରି ACTION_SEND ସିଟ୍ ଖୋଲେ (ଫଟୋ ପ୍ରିଭ୍ୟୁ ସହ)।
    // 10 ସେକେଣ୍ଡ ଭିତରେ ଫଟୋ ନଆସିଲେ ଖାଲି ଟେକ୍ସଟ୍ ପଠାଏ।
    function shareViaNativeBridge_(shareText) {
      var done = false;
      var finishTextOnly = function () {
        if (done) return; done = true;
        try { window.MunnaShare.postImage('', 'product.jpg', shareText); } catch (e) {}
      };
      var timer = setTimeout(finishTextOnly, 10000);
      var sendImage = function (imgUrl) {
        if (!imgUrl) { clearTimeout(timer); finishTextOnly(); return; }
        fetch(imgUrl, { mode: 'cors' }).then(function (r) {
          if (!r.ok) throw new Error('img ' + r.status);
          return r.blob();
        }).then(function (blob) {
          var rd = new FileReader();
          rd.onloadend = function () {
            if (done) return; done = true; clearTimeout(timer);
            try {
              var b64 = String(rd.result || '').split(',')[1] || '';
              var nm = String((currentItem && currentItem.ean) || 'product') + '.jpg';
              window.MunnaShare.postImage(b64, nm, shareText);
            } catch (e) { finishTextOnly(); }
          };
          rd.onerror = function () { clearTimeout(timer); finishTextOnly(); };
          rd.readAsDataURL(blob);
        }).catch(function () { clearTimeout(timer); finishTextOnly(); });
      };
      try {
        // Catalog (server) photo first; auto-find one by EAN when missing —
        // the share sheet then opens with the product picture + link.
        findProductImage_(currentItem, sendImage);
      } catch (e) { clearTimeout(timer); finishTextOnly(); }
    }

    // Similar Products ସ୍ଲାଇଡର୍ — ସମାନ କ୍ୟାଟେଗୋରୀ ପ୍ରଥମେ, ତାପରେ ଅନ୍ୟ କ୍ୟାଟେଗୋରୀ;
    // ନିଜ ଆଇଟମ୍ ବାଦ୍, ସର୍ବାଧିକ ୬ଟି; ହୋମ୍ କାର୍ଡ ଷ୍ଟାଇଲ୍ ସହ ତୁରନ୍ତ Add/View ବଟନ୍
    function renderSimilarProducts_(p) {
      var box = document.getElementById('pdSimilarRow');
      if (!box) return;
      try {
        var all = (window.STORE_CATALOG || []).filter(function (x) {
          return String(x.ean) !== String(p.ean);
        });
        var cat = catOf(p);
        var same = all.filter(function (x) { return catOf(x) === cat; });
        var rest = all.filter(function (x) { return catOf(x) !== cat; });
        var list = same.concat(rest).slice(0, 6);
        if (!list.length) {
          box.innerHTML = '<p class="text-[11px] text-slate-400 py-2">ଆଉ ସାମଗ୍ରୀ ନାହିଁ।</p>';
          return;
        }
        var fallback = 'icon-512.png'; // server-hosted — always loads
        box.innerHTML = list.map(function (s) {
          var mrp = parseFloat(s.mrp) || 0, sp = parseFloat(s.selling_price) || 0;
          var off = mrp > sp ? Math.round(((mrp - sp) / mrp) * 100) : 0;
          return '<div class="shrink-0 w-32 bg-white rounded-2xl border border-slate-200 shadow-xs p-2 flex flex-col">' +
            '<div class="relative w-full h-20 rounded-xl bg-slate-50 overflow-hidden cursor-pointer" onclick="openProductDetail(\'' + String(s.ean).replace(/'/g, '') + '\')">' +
              '<img src="' + (s.image_url || fallback) + '" alt="" class="w-full h-full object-contain" onerror="this.src=\'' + fallback + '\'">' +
              (off > 0 ? '<span class="absolute top-1 left-1 bg-orange-600 text-white text-[9px] font-black px-1 py-0.5 rounded">' + off + '% OFF</span>' : '') +
            '</div>' +
            '<div class="mt-1.5 text-[10px] font-bold text-slate-800 leading-tight" style="display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:26px;">' + String(s.product_name || '') + '</div>' +
            '<div class="mt-0.5 text-xs font-black text-slate-900">Rs.' + money(s.selling_price) + '</div>' +
            '<div id="pdSimilarCtl-' + String(s.ean) + '" class="mt-1.5 w-full"></div>' + 
          '</div>';
        }).join('');
        list.forEach(function (s) { renderSimilarCTL(s.ean); });
      } catch (e) {
        box.innerHTML = '';
      }
    // Live Add-to-Cart / stepper control for similar product cards
    function renderSimilarCTL(ean) {
      var ctl = document.getElementById('pdSimilarCtl-' + String(ean));
      if (!ctl) return;
      var tq = (cart && cart[ean]) || 0;
      if (tq > 0) {
        var esc = String(ean).replace(/'/g, "\\'");
        var dec = "setQty('" + esc + "', " + (tq - 1) + ")";
        var inc = "setQty('" + esc + "', " + (tq + 1) + ")";
        ctl.innerHTML = '<div class="flex items-center gap-1 bg-slate-900 text-white rounded-lg px-1 py-0.5">' +
          '<button onclick="event.stopPropagation();' + dec + '" class="w-5 h-5 rounded bg-slate-800 flex items-center justify-center"><i class="ph-bold ph-minus text-[10px]"></i></button>' +
          '<span class="text-[11px] font-bold w-4 text-center">' + tq + '</span>' +
          '<button onclick="event.stopPropagation();' + inc + '" class="w-5 h-5 rounded bg-[#1864ab] flex items-center justify-center"><i class="ph-bold ph-plus text-[10px]"></i></button>' +
          '</div>';
      } else {
        var esc = String(ean).replace(/'/g, "\\'");
        var add = "setQty('" + esc + "', 1)";
        ctl.innerHTML = '<button onclick="event.stopPropagation();' + add + '" class="w-full py-1 rounded-lg bg-[#1864ab] text-white text-[10px] font-bold flex items-center justify-center gap-1"><i class="ph-bold ph-plus"></i> Add</button>';
      }
    }

    var ean = '';
    try { ean = new URLSearchParams(window.location.search).get('item') || ''; } catch (e) {}
      if (!ean) return;
      var tries = 0;
      var timer = setInterval(function () {
        tries++;
        var p = (window.STORE_CATALOG || []).find(function (x) { return String(x.ean) === String(ean); });
        if (p) {
          clearInterval(timer);
          openProductDetail(ean);
        } else if (tries > 20) {
          clearInterval(timer); // catalog loaded but the item is absent
        }
      }, 300); // wait up to ~6s for the catalog to load
    }

    // ================= SCREEN NAVIGATION =================
