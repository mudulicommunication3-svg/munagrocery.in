/* munnamobile :: js/image-search.js  (extracted from index.html lines 3855-4143)
   ImgBB image links + JioMart/OFF/Sheet product search */
    function uploadToImgBB(file, key) {
      return new Promise(function (resolve) {
        var reader = new FileReader();
        reader.onload = function () {
          var base64 = String(reader.result).split(',')[1];
          var body = new FormData();
          body.append('image', base64);
          fetch('https://api.imgbb.com/1/upload?key=' + encodeURIComponent(key), {
            method: 'POST', body: body
          })
            .then(function (r) { return r.json(); })
            .then(function (d) {
              resolve(d && d.data && d.data.url ? d.data.url : '');
            })
            .catch(function () { resolve(''); });
        };
        reader.onerror = function () { resolve(''); };
        reader.readAsDataURL(file);
      });
    }

    function copyImgLink() {
      var out = document.getElementById('imgLinkOutput');
      out.select();
      out.setSelectionRange(0, 99999);
      try {
        document.execCommand('copy');
        alert("✅ ଲିଙ୍କ୍ କପି ହେଲା!");
      } catch (e) {
        if (navigator.clipboard) navigator.clipboard.writeText(out.value);
      }
    }

    function useImgLinkForBanner() {
      var url = document.getElementById('imgLinkOutput').value.trim();
      if (!url) return;
      document.getElementById('newBannerImg').value = url;
      updateImagePreview(url);
      switchAdminTab('banners');
      document.getElementById('newBannerTitle').focus();
    }

    // --- JIOMART & WEB SMART PRODUCT SEARCH ---
    function setSearchSource(src) {
      currentSearchSource = src;
      var jioBtn = document.getElementById('toggleJioBtn');
      var webBtn = document.getElementById('toggleWebBtn');
      var pinBox = document.getElementById('pincodeWrapper');
      if (src === 'jiomart') {
        jioBtn.className = "px-2 py-0.5 rounded-md bg-[#1864ab] text-white";
        webBtn.className = "px-2 py-0.5 rounded-md text-slate-600";
        pinBox.classList.remove('hidden');
      } else {
        webBtn.className = "px-2 py-0.5 rounded-md bg-[#1864ab] text-white";
        jioBtn.className = "px-2 py-0.5 rounded-md text-slate-600";
        pinBox.classList.add('hidden');
      }
    }

    // --- JioMart search backends (bot method first: the Sheet SearchQueue bus
    //     hands each query to the bot PC, which runs the SAME engine the
    //     Telegram bot uses - jiomart_search.fetch_items - and writes the
    //     items back to the Sheet. No local proxy needed.) ---
    // Apps Script webhook: UrlFetchApp calls JioMart SERVER-SIDE with the
    // full header set, so CORS and JioMart's browser bot-blocking never
    // apply (backup when the bot PC is offline).
    async function searchJioMartViaHook(q, pin) {
      var url = sheetHookUrl('action=SEARCH_JIOMART&query=' + encodeURIComponent(q) + '&pincode=' + encodeURIComponent(pin));
      if (!url) return null;
      try {
        var res = await fetch(url);
        var data = await res.json();
        if (data && data.status === 'success' && Array.isArray(data.items)) {
          return data.items.filter(function (it) { return it && it.url && it.sellable !== false; });
        }
      } catch (e) {}
      return null;
    }
    // Public CORS relay (merged medias[] + price + hierarchy, like the bot).
    // Try AllOrigins first, then the thingproxy-style corsproxy.io fallback.
    async function fetchViaRelay_(proxyUrl, timeoutMs) {
      var ctrl = new AbortController();
      var timer = setTimeout(function () { ctrl.abort(); }, timeoutMs || 15000);
      try {
        var proxyRes = await fetch(proxyUrl, { signal: ctrl.signal });
        clearTimeout(timer);
        return await proxyRes.text();
      } catch (e) { clearTimeout(timer); throw e; }
    }
    function parseJioRelayText_(contents, q) {
      var out = [];
      var jioData = JSON.parse(contents);
      var items = jioData.items || (jioData.data && jioData.data.items) || [];
      (items || []).forEach(function (item) {
        if (!item || typeof item !== 'object') return;
        var imgUrl = '';
        ((item.medias || [])).forEach(function (m) {
          if (!imgUrl && m && m.url && (!m.type || String(m.type).toLowerCase() === 'image')) imgUrl = m.url;
        });
        if (!imgUrl && item.medias && item.medias[0] && item.medias[0].url) imgUrl = item.medias[0].url;
        var mrp = '', sp = '';
        try {
          var p = item.price || {};
          mrp = p.marked ? ((typeof p.marked === 'object') ? (p.marked.min || p.marked.max || '') : p.marked) : '';
          sp = p.effective ? ((typeof p.effective === 'object') ? (p.effective.min || p.effective.max || '') : p.effective) : '';
        } catch (e) {}
        var hier = item.hierarchy || {};
        var cat = ((hier.l3_category || {}).name) || ((hier.l2_category || {}).name) || 'Grocery';
        var sell = item.sellable;
        if (imgUrl) out.push({ title: item.name || q, url: imgUrl, mrp: String(mrp), sp: String(sp), cat: cat, sellable: sell === undefined ? true : sell, source: 'JioMart' });
      });
      return out;
    }
    async function searchJioMartViaAllOrigins(q) {
      var out = [];
      try {
        var targetJioUrl = 'https://www.jiomart.com/api/service/application/catalog/v1.0/products?q=' + encodeURIComponent(q) + '&page_size=12&page_no=1';
        var resText = await fetchViaRelay_('https://api.allorigins.win/get?url=' + encodeURIComponent(targetJioUrl), 15000);
        var proxyJson = JSON.parse(resText);
        if (proxyJson && proxyJson.contents) {
          out = parseJioRelayText_(proxyJson.contents, q);
        } else {
          lastBackendError = 'AllOrigins: empty relay response';
        }
      } catch (e) { lastBackendError = 'AllOrigins: ' + e.message; }
      return out;
    }
    // 4) Backup relay: corsproxy.io (?url=...) then raw allorigins (/raw).
    // OpenFoodFacts: grocery barcode/offer database search (CORS-open API) moved to openfoodfacts fallback below.
    async function searchJioMartViaCorsProxy(q) {
      var out = [];
      var targetJioUrl = 'https://www.jiomart.com/api/service/application/catalog/v1.0/products?q=' + encodeURIComponent(q) + '&page_size=12&page_no=1';
      var relays = [
        'https://corsproxy.io/?url=' + encodeURIComponent(targetJioUrl),
        'https://api.allorigins.win/raw?url=' + encodeURIComponent(targetJioUrl)
      ];
      for (var i = 0; i < relays.length && !out.length; i++) {
        try {
          var txt = await fetchViaRelay_(relays[i], 15000);
          try { out = parseJioRelayText_(txt, q); }
          catch (e2) {
            try { out = parseJioRelayText_(JSON.parse(txt).contents || '', q); } catch (e3) {}
          }
        } catch (e) { lastBackendError = 'Relay ' + (i + 1) + ': ' + e.message; }
      }
      return out;
    }

    // 5) OpenFoodFacts grocery search — CORS-open API, item name + photo.
    // Works even when JioMart blocks every relay (no MRP — admin types it).
    var lastBackendError = '';
    async function searchOpenFoodFacts(q) {
      var out = [];
      try {
        var ctrl = new AbortController();
        var timer = setTimeout(function () { ctrl.abort(); }, 15000);
        var url = 'https://world.openfoodfacts.org/cgi/search.pl?search_terms=' + encodeURIComponent(q) +
          '&search_simple=1&action=process&json=1&page_size=10';
        var res = await fetch(url, { signal: ctrl.signal, headers: { 'Accept': 'application/json' } });
        clearTimeout(timer);
        var data = await res.json();
        ((data && data.products) || []).forEach(function (p) {
          var img = p.image_front_url || p.image_url || '';
          var nm = p.product_name || '';
          if (!img || !nm) return;
          var brands = p.brands || '';
          out.push({
            title: (brands ? (nm + ' (' + brands.split(',')[0].trim() + ')') : nm),
            url: img,
            mrp: '', sp: '',
            cat: (p.categories_tags && p.categories_tags[0]) ? String(p.categories_tags[0]).replace(/^[a-z-]+:/, '') : 'Grocery',
            source: 'OpenFoodFacts'
          });
        });
      } catch (e) { lastBackendError = 'OpenFoodFacts: ' + e.message; }
      return out;
    }

    // 0) Bot-PC search bus (Sheet SearchQueue): POST a request, poll for the
    // bot PC's answer — the ONLY path proven to reach JioMart (bot's
    // jiomart_search.fetch_items). ~5-20s depending on the bot poll loop.
    async function searchBusViaSheet(q, pin) {
      var hook = window.munnaHookBase();
      if (!hook) return null;
      var rid = 'SQ-' + Date.now().toString(36).toUpperCase() + '-' +
        Math.random().toString(36).slice(2, 6).toUpperCase();
      try {
        var ctrl0 = new AbortController();
        var t0 = setTimeout(function () { ctrl0.abort(); }, 8000);
        // Same shape as postToSheet (proved working for create_order).
        await fetch(hook, {
          method: 'POST',
          mode: 'no-cors',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'PUT_SEARCH', request_id: rid, query: q, pincode: pin }),
          signal: ctrl0.signal
        });
        clearTimeout(t0);
      } catch (e) { lastBackendError = 'SearchBus POST: ' + e.message; return null; }
      var deadline = Date.now() + 40000;
      while (Date.now() < deadline) {
        await new Promise(function (r) { setTimeout(r, 3000); });
        try {
          var res = await fetch(sheetHookUrl('action=GET_SEARCH&request_id=' + encodeURIComponent(rid) + '&_=' + Date.now()));
          var data = await res.json();
          // Old Apps Script (not redeployed yet) has no GET_SEARCH and
          // answers with the catalog ARRAY -> bail out immediately instead
          // of burning the whole 40s budget.
          if (Array.isArray(data)) {
            lastBackendError = 'Apps Script ପୁରୁଣା ଅଛି — GET_SEARCH/PUT_SEARCH ନାହିଁ, ଥରେ redeploy କରନ୍ତୁ';
            return null;
          }
          if (data && data.status === 'done' && Array.isArray(data.items) && data.items.length) {
            return data.items.filter(function (it) { return it && it.url && it.sellable !== false; });
          }
          if (data && data.status === 'error') {
            lastBackendError = 'Bot search: ' + (data.error || 'failed');
            return null;
          }
        } catch (e) { lastBackendError = 'SearchBus GET: ' + e.message; }
      }
      lastBackendError = 'Bot search: 40sରେ ଉତ୍ତର ମିଳିଲା ନାହିଁ (ବଟ୍ PC ଅଫଲାଇନ୍?)';
      return null;
    }

    async function triggerLiveSearch() {
      var q = document.getElementById('newProdName').value.trim();
      var pin = document.getElementById('searchPincode').value.trim() || '752106';
      if (!q || q.length < 2) { alert("ସାମଗ୍ରୀର ନାମ ଲେଖନ୍ତୁ!"); return; }

      var searchBtn = document.getElementById('smartSearchBtn');
      searchBtn.disabled = true;
      searchBtn.innerHTML = '<i class="ph-bold ph-spinner animate-spin"></i> Searching...';

      var dropdown = document.getElementById('imageDropdownContainer');
      var grid = document.getElementById('imageGridResults');
      dropdown.classList.remove('hidden');
      grid.innerHTML = '<div class="col-span-full py-4 text-center text-xs text-blue-600 font-bold"><i class="ph-bold ph-spinner animate-spin"></i> JioMart ଲାଇଭ୍ ଡାଟା ଖୋଜା ଚାଲିଛି...</div>';

      // Try every backend in order until one returns products.
      var results = [], sourceUsed = '';
      window.lastBackendError = lastBackendError = '';
      // 1) Bot-PC search bus (Sheet SearchQueue) — THE bot method: the
      //    running bot (search_bus.py) answers with jiomart_search.fetch_items,
      //    the exact engine behind /product. Works from any network.
      var viaBus = await searchBusViaSheet(q, pin);
      if (viaBus && viaBus.length) { results = viaBus; sourceUsed = 'Bot PC'; }
      // 2) Apps Script webhook (server-side JioMart with pincode headers).
      if (!results.length) {
        var viaHook = await searchJioMartViaHook(q, pin);
        if (viaHook && viaHook.length) { results = viaHook; sourceUsed = 'Apps Script'; }
      }
      if (!results.length) {
        var viaOrigins = await searchJioMartViaAllOrigins(q);
        if (viaOrigins && viaOrigins.length) { results = viaOrigins; sourceUsed = 'AllOrigins'; }
      }
      if (!results.length) {
        var viaRelays = await searchJioMartViaCorsProxy(q);
        if (viaRelays && viaRelays.length) { results = viaRelays; sourceUsed = 'Web Relay'; }
      }
      if (!results.length) {
        var viaOff = await searchOpenFoodFacts(q);
        if (viaOff && viaOff.length) { results = viaOff; sourceUsed = 'OpenFoodFacts'; }
      }

      searchBtn.disabled = false;
      searchBtn.innerHTML = '<i class="ph-bold ph-magnifying-glass"></i> Search';
      document.getElementById('resultsHeaderTitle').innerText = 'JioMart Results' + (sourceUsed ? ' (' + sourceUsed + ')' : '') + ':';

      if (results.length === 0) {
        var hint = window.munnaHookBase()
          ? ' — webhook ଠିକ୍ ଅଛି କିନ୍ତୁ JioMart ରୁ ଫଳାଫଳ ମିଳିଲା ନାହିଁ; ଅନ୍ୟ ନାମ ଚେଷ୍ଟା କରନ୍ତୁ।'
          : ' — Apps Script Webhook ସେଟ୍ କରନ୍ତୁ ଏବଂ ବଟ୍ PC ରେ python main.py ଚାଲୁରଖନ୍ତୁ (ବଟ୍ search bus ମାଧ୍ୟମରେ JioMart ସର୍ଚ୍ଚ କରେ)।';
        var dbg = '';
        try { if (window.lastBackendError) dbg = '<br><span class="text-slate-500 font-normal">(' + window.lastBackendError + ')</span>'; } catch (e) {}
        grid.innerHTML = '<p class="col-span-full text-xs text-rose-500 font-bold text-center py-2">କୌଣସି ଫଳାଫଳ ମିଳିଲା ନାହିଁ' + hint + dbg + '</p>';
        return;
      }

      grid.innerHTML = results.map(item => `
        <div onclick="selectProductToForm('${encodeURIComponent(item.url)}', '${encodeURIComponent(item.title)}', '${encodeURIComponent(item.cat)}', '${item.mrp}', '${item.sp}')"
             class="cursor-pointer bg-white p-2 rounded-xl border border-slate-200 hover:border-blue-500 text-center flex flex-col items-center">
          <img src="${item.url}" class="w-full h-20 object-contain mb-1">
          <span class="text-[10px] font-bold text-slate-800 line-clamp-1">${item.title}</span>
          <span class="text-[10px] font-extrabold text-emerald-600 mt-0.5">Rs.${item.sp}</span>
        </div>
      `).join('');
    }

