/* munnamobile :: js/admin-catalog.js  (extracted from index.html lines 4144-4356)
   admin product form, catalog CRUD, webhook + manual sync */
    function selectProductToForm(url, title, cat, mrp, sp) {
      document.getElementById('newProdImg').value = decodeURIComponent(url);
      updateImagePreview(decodeURIComponent(url));
      document.getElementById('newProdName').value = decodeURIComponent(title);
      document.getElementById('newProdCat').value = decodeURIComponent(cat);
      if (mrp) document.getElementById('newProdMRP').value = mrp;
      if (sp) document.getElementById('newProdSP').value = sp;
      closeImageDropdown();
    }
    function closeImageDropdown() { document.getElementById('imageDropdownContainer').classList.add('hidden'); }
    function updateImagePreview(u) {
      var box = document.getElementById('imgPreviewBox');
      var img = document.getElementById('imgPreviewTag');
      if (u && u.startsWith('http')) { img.src = u; box.classList.remove('hidden'); } else { box.classList.add('hidden'); }
    }

    // --- CATALOG CRUD & SYNC ---
    async function handleAdminFormSubmit(e) {
      e.preventDefault();
      var isEdit = document.getElementById('editEan').value !== "";
      var eanField = document.getElementById('newProdEan');
      var ean = isEdit ? document.getElementById('editEan').value
                       : ((eanField && eanField.value.trim()) || ("EAN-" + Date.now()));
      if (!isEdit) {
        // EAN column: every product gets a scannable code (manual or from a scan).
        var dupScan = window.STORE_CATALOG.find(function (x) { return String(x.ean).trim() === String(ean).trim(); });
        if (dupScan) {
          alert("❌ ଏହି EAN ପୂର୍ବରୁ ଅଛି: " + dupScan.product_name + " — Edit କରନ୍ତୁ କିମ୍ବା ଅଲଗା EAN ଦିଅନ୍ତୁ।");
          return;
        }
      }
      var nm = document.getElementById('newProdName').value.trim();
      var mrp = document.getElementById('newProdMRP').value.trim();
      var sp = document.getElementById('newProdSP').value.trim();
      var st = document.getElementById('newProdStock').value.trim();
      var cat = document.getElementById('newProdCat').value.trim() || 'General';
      var img = document.getElementById('newProdImg').value.trim() || "https://images.unsplash.com/photo-1542838132-92c53300491e?w=400";

      var prodObj = { ean, product_name: nm, mrp, selling_price: sp, stock_qty: st, image_url: img, category: cat };

      if (isEdit) {
        var idx = window.STORE_CATALOG.findIndex(x => String(x.ean) === String(ean));
        if (idx !== -1) window.STORE_CATALOG[idx] = prodObj;
      } else {
        window.STORE_CATALOG.unshift(prodObj);
      }

      localStorage.setItem('munna_store_catalog', JSON.stringify(window.STORE_CATALOG));

      var hook = window.munnaHookBase();
      if (hook) {
        try {
          fetch(hook, {
            method: 'POST',
            mode: 'no-cors',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: isEdit ? "EDIT_PRODUCT" : "ADD_PRODUCT", ean, name: nm, mrp, sellingPrice: sp, stock: st, category: cat, image: img })
          });
        } catch(err){}
      }

      alert("✅ ସଫଳତାର ସହ ସେଭ୍ ହେଲା!");
      resetAdminForm();
      renderAdminList();
      render();
    }

    function resetAdminForm() {
      document.getElementById('editEan').value = '';
      document.getElementById('newProdName').value = '';
      document.getElementById('newProdMRP').value = '';
      document.getElementById('newProdSP').value = '';
      document.getElementById('newProdStock').value = '10';
      document.getElementById('newProdCat').value = '';
      document.getElementById('newProdImg').value = '';
      var eanReset = document.getElementById('newProdEan');
      if (eanReset) eanReset.value = '';
      updateImagePreview('');
      closeImageDropdown();
      document.getElementById('cancelEditBtn').classList.add('hidden');
    }

    function clearDbSearch() {
      document.getElementById('dbSearchInput').value = '';
      renderAdminList();
    }

    function renderAdminList() {
      var box = document.getElementById('adminInventoryList');
      var searchEl = document.getElementById('dbSearchInput');
      var clearBtn = document.getElementById('dbSearchClearBtn');
      var q = searchEl ? searchEl.value.trim().toLowerCase() : '';
      if (clearBtn) clearBtn.classList.toggle('hidden', !q);

      // Database search: filter by product name / category / EAN / MRP / SP (case-insensitive, partial match)
      var items = window.STORE_CATALOG || [];
      if (q) {
        items = items.filter(function (p) {
          var nm = String(p.product_name || '').toLowerCase();
          var cat = String(p.category || '').toLowerCase();
          var ean = String(p.ean || '').toLowerCase();
          var mrp = String(p.mrp || '').toLowerCase();
          var sp = String(p.selling_price || '').toLowerCase();
          return nm.indexOf(q) >= 0 || cat.indexOf(q) >= 0 || ean.indexOf(q) >= 0 || mrp.indexOf(q) >= 0 || sp.indexOf(q) >= 0;
        });
      }

      if (!items.length) {
        box.innerHTML = '<p class="text-xs text-slate-400 text-center py-6">' +
          (q ? '🔍 "' + q + '" ପାଇଁ କିଛି ମିଳିଲା ନାହିଁ।' : 'କୌଣସି ପ୍ରୋଡକ୍ଟ ନାହିଁ।') + '</p>';
        document.getElementById('adminTotalItems').innerText =
          q ? items.length + ' / ' + window.STORE_CATALOG.length + ' Items' : window.STORE_CATALOG.length + ' Items';
        return;
      }

      // Build rows using findIndex by EAN (more reliable than indexOf for objects)
      var rowsHtml = '';
      items.forEach(function (p) {
        var realIdx = window.STORE_CATALOG.findIndex(function (x) { return String(x.ean) === String(p.ean); });
        if (realIdx === -1) realIdx = 0; // fallback
        var isTrend = shopConfig.trending.indexOf(String(p.ean)) >= 0;
        var sq = parseInt(p.stock_qty, 10); if (isNaN(sq)) sq = 10;
        var stockBadge = sq <= 0
          ? '<span class="text-[9px] font-black text-rose-600">❌ OUT</span>'
          : (sq <= 5
            ? '<span class="text-[9px] font-black text-amber-600">⚠️ LOW (' + sq + ')</span>'
            : '<span class="text-[9px] text-slate-400 font-bold">Stock: ' + sq + '</span>');
        rowsHtml += '<div class="grid grid-cols-6 gap-1 items-center p-1.5 bg-white rounded-xl border border-slate-200 text-xs">' +
          '<div class="min-w-0 flex items-center gap-2">' +
            '<img src="' + (p.image_url || 'https://via.placeholder.com/40x40') + '" class="w-6 h-6 object-cover rounded border border-slate-100 bg-slate-50">' +
            '<div class="min-w-0">' +
              '<h5 class="font-bold text-slate-800 leading-tight truncate">' + (p.product_name || 'Unknown') + '</h5>' +
              '<span class="text-[9px] text-slate-400 truncate">EAN: ' + (p.ean || '-') + '</span>' +
            '</div>' +
          '</div>' +
          '<div class="text-slate-700 font-bold text-[10px]">₹' + (p.mrp || '0') + '</div>' +
          '<div class="text-[#1864ab] font-bold text-[10px]">₹' + (p.selling_price || '0') + '</div>' +
          '<div class="text-[10px]">' + stockBadge + '</div>' +
          '<div class="text-[9px] text-slate-500 truncate">' + (p.category || 'General') + '</div>' +
          '<div class="flex gap-1 items-center justify-end">' +
            '<button onclick="toggleTrending(\'' + (p.ean || '') + '\')" title="Trending ରେ ଯୋଡ଼ନ୍ତୁ/ହଟାନ୍ତୁ" class="w-5 h-5 rounded text-xs ' + (isTrend ? 'text-amber-500' : 'text-slate-300') + '">' + (isTrend ? '★' : '☆') + '</button>' +
            '<button onclick="editAdminProduct(' + realIdx + ')" title="Edit" class="px-1.5 py-0.5 bg-amber-500 text-white rounded font-bold text-[9px]">Edit</button>' +
            '<button onclick="deleteAdminProduct(' + realIdx + ')" title="Delete" class="px-1.5 py-0.5 bg-rose-50 text-rose-600 rounded font-bold text-[9px]">Delete</button>' +
          '</div>' +
        '</div>';
      });
      box.innerHTML = rowsHtml;
      document.getElementById('adminTotalItems').innerText =
        q ? items.length + ' / ' + window.STORE_CATALOG.length + ' Items' : window.STORE_CATALOG.length + ' Items';
    }

    function editAdminProduct(idx) {
      var p = window.STORE_CATALOG[idx];
      document.getElementById('editEan').value = p.ean;
      document.getElementById('newProdName').value = p.product_name;
      document.getElementById('newProdMRP').value = p.mrp;
      document.getElementById('newProdSP').value = p.selling_price;
      document.getElementById('newProdStock').value = p.stock_qty || 10;
      document.getElementById('newProdCat').value = p.category;
      document.getElementById('newProdImg').value = p.image_url;
      var eanFill = document.getElementById('newProdEan');
      if (eanFill) eanFill.value = p.ean || '';
      updateImagePreview(p.image_url);
      document.getElementById('cancelEditBtn').classList.remove('hidden');
      document.getElementById('adminFormCard').scrollIntoView({ behavior: 'smooth' });
    }

    function deleteAdminProduct(idx) {
      if (confirm("ଡିଲିଟ୍ କରିବାକୁ ଚାହାନ୍ତି କି?")) {
        // ଡିଲିଟ୍ ହେବା ଆଗରୁ EAN ରଖ — Sheet କୁ DELETE_PRODUCT ପଠାଇବା ପାଇଁ ଦରକାର
        var gone = (window.STORE_CATALOG || [])[idx];
        var goneEan = gone ? String(gone.ean || '') : '';
        window.STORE_CATALOG.splice(idx, 1);
        localStorage.setItem('munna_store_catalog', JSON.stringify(window.STORE_CATALOG));
        // Sheet ରୁ ମଧ୍ୟ ଡିଲିଟ୍ କର — ନହେଲେ ରିଫ୍ରେସ ହେଲେ syncFromGoogleSheet()
        // ପୁରୁଣା ଡାଟା ଆଣି ଡିଲିଟ୍ ଆଇଟମ୍ ଫେରାଇ ଦେବ (ଏହା ହିଁ ମୂଳ ବଗ୍ ଥିଲା)
        var hook = window.munnaHookBase();
        if (hook && goneEan) {
          try {
            fetch(hook, {
              method: 'POST',
              mode: 'no-cors',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ action: "DELETE_PRODUCT", ean: goneEan })
            });
          } catch(err){}
        }
        renderAdminList();
        render();
      }
    }

    function saveWebhook() {
      var u = document.getElementById('webhookInput').value.trim();
      localStorage.setItem('munna_sheet_webhook', u);
      alert("Webhook Saved!");
      syncFromGoogleSheet();
      syncOrdersFromSheet();
      syncAddressesFromSheet();
    }
    async function syncFromGoogleSheet() {
      // Default webhook is built in, so even fresh / cleared-data browsers
      // fetch YOUR Sheet's catalog on first load (no manual setup needed).
      var hook = window.munnaHookBase();
      if (!hook) return;
      try {
        var res = await fetch(hook);
        var data = await res.json();
        if (Array.isArray(data) && data.length > 0) {
          window.STORE_CATALOG = data;
          localStorage.setItem('munna_store_catalog', JSON.stringify(data));
          render();
          renderAdminList();
        }
      } catch(e){}
    }
    async function manualSync() {
      await syncFromGoogleSheet();
      await syncOrdersFromSheet();
      await syncAddressesFromSheet();
      alert("Google Sheets Sync Complete!");
    }

    // --- LOCAL STORAGE HELPERS ---
