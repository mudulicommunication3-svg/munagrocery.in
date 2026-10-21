/* munnamobile :: js/catalog.js  (extracted from index.html lines 1485-1595)
   product grid render + cart badges */
    function render() {
      renderCategoryPills();
      var grid = document.getElementById("grid");
      var empty = document.getElementById("emptyView");
      grid.innerHTML = "";

      var q = query.trim().toLowerCase();
      var filtered = window.STORE_CATALOG.filter(p => {
        if (activeCat !== "All" && catOf(p) !== activeCat) return false;
        if (q && (p.product_name || "").toLowerCase().indexOf(q) < 0) return false;
        return true;
      });

      document.getElementById("productCountBadge").innerText = `${filtered.length} ଟି ସାମଗ୍ରୀ`;

      if (filtered.length === 0) {
        empty.classList.remove("hidden");
      } else {
        empty.classList.add("hidden");
        filtered.forEach(p => {
          var qty = cart[p.ean] || 0;
          var mrpVal = parseFloat(p.mrp) || 0;
          var spVal = parseFloat(p.selling_price) || 0;
          var discount = mrpVal > spVal ? Math.round(((mrpVal - spVal) / mrpVal) * 100) : 0;

          var stockN = parseInt(p.stock_qty, 10); if (isNaN(stockN)) stockN = 10;
          var stockHtml = stockN <= 0
            ? '<span class="text-[10px] font-extrabold text-rose-600">❌ ସରିଗଲା (Out of stock)</span>'
            : (stockN <= 5
              ? `<span class="text-[10px] font-extrabold text-amber-600">⚠️ କେବଳ ${stockN} ବଳିଛି</span>`
              : `<span class="text-[10px] text-slate-400 font-semibold">Stock: ${stockN}</span>`);

          var card = document.createElement("div");
          card.className = "bg-white rounded-2xl p-2.5 border border-slate-200 shadow-xs flex flex-col justify-between relative";
          card.innerHTML = `
            ${discount > 0 ? `<span class="absolute top-2 left-2 z-10 bg-orange-600 text-white text-[10px] font-black px-1.5 py-0.5 rounded-md">${discount}% OFF</span>` : ''}
            <div onclick="openProductDetail('${p.ean}')" class="w-full h-32 rounded-xl bg-slate-50 flex items-center justify-center overflow-hidden cursor-pointer">
              <img src="${p.image_url || 'icon-512.png'}" alt="${p.product_name}" class="w-full h-full object-contain p-1" onerror="this.src='icon-512.png'">
            </div>
            <div onclick="openProductDetail('${p.ean}')" class="mt-2 flex-1 flex flex-col justify-between cursor-pointer">
              <h4 class="text-xs font-bold text-slate-800 line-clamp-2 leading-tight">${p.product_name}</h4>
              <div class="flex items-center gap-1 mt-1.5">
                <span class="bg-emerald-600 text-white text-[9px] font-bold px-1 rounded flex items-center gap-0.5">4.8 <i class="ph-fill ph-star text-[8px]"></i></span>
                ${stockHtml}
              </div>
              <div class="flex items-baseline gap-1 mt-1">
                <span class="text-sm font-black text-slate-900">Rs.${money(p.selling_price)}</span>
                ${mrpVal > spVal ? `<span class="text-[11px] text-slate-400 line-through">Rs.${money(p.mrp)}</span>` : ''}
              </div>
            </div>
            <div class="mt-2" id="action-box-${p.ean}"></div>
          `;

          var actionBox = card.querySelector(`#action-box-${p.ean}`);
          if (qty > 0) {
            actionBox.innerHTML = `
              <div class="flex items-center justify-between bg-slate-900 text-white rounded-xl px-1.5 py-1">
                <button onclick="event.stopPropagation();setQty('${p.ean}', ${qty - 1})" class="w-6 h-6 rounded bg-slate-800 flex items-center justify-center text-xs"><i class="ph-bold ph-minus"></i></button>
                <span class="text-xs font-bold">${qty}</span>
                <button onclick="event.stopPropagation();setQty('${p.ean}', ${qty + 1})" class="w-6 h-6 rounded bg-[#1864ab] flex items-center justify-center text-xs"><i class="ph-bold ph-plus"></i></button>
              </div>
            `;
          } else if (stockN <= 0) {
            actionBox.innerHTML = `
              <button disabled class="w-full py-1.5 rounded-xl bg-rose-50 text-rose-400 border border-rose-200 text-xs font-bold cursor-not-allowed flex items-center justify-center gap-1">
                <i class="ph-bold ph-x-circle"></i> Out of Stock (ସରିଗଲା)
              </button>
            `;
          } else {
            actionBox.innerHTML = `
              <button onclick="event.stopPropagation();setQty('${p.ean}', 1)" class="w-full py-1.5 rounded-xl bg-blue-50 text-[#1864ab] hover:bg-[#1864ab] hover:text-white border border-blue-200 text-xs font-bold transition-all flex items-center justify-center gap-1">
                <i class="ph-bold ph-plus"></i> କାର୍ଟରେ ଯୋଡ଼ନ୍ତୁ
              </button>
            `;
          }
          grid.appendChild(card);
        });
      }
      renderTrending();
      renderBar();
    }

    function renderBar() {
      var keys = Object.keys(cart), n = 0, total = 0;
      keys.forEach(k => {
        var p = window.STORE_CATALOG.find(x => String(x.ean) === String(k));
        if (p) {
          n += cart[k];
          total += (parseFloat(p.selling_price) || 0) * cart[k];
        }
      });
      document.getElementById("floatingCartBadge").innerText = n;
      document.getElementById("floatingCartTotal").innerText = "Rs." + money(total);
      updateCartBadge();
      // Flipkart pattern: the floating cart bar is shown ONLY when the basket
      // has items; when empty (0 items / Rs.0) it is hidden completely and the
      // body padding shrinks so the layout stays 100% clean.
      var hasItems = n > 0;
      var fbar = document.getElementById('floatingCartBar');
      if (fbar) fbar.classList.toggle('hidden', !hasItems);
      document.body.classList.toggle('pb-32', hasItems); // 128px: bar sits above nav
      document.body.classList.toggle('pb-20', !hasItems); // 80px: just the 54px nav
    }

    function updateCartBadge() {
      var n = 0; Object.keys(cart).forEach(k => n += cart[k]);
      var b = document.getElementById('navCartBadge');
      if (b) { b.innerText = n; b.classList.toggle('hidden', n === 0); }
    }

    // --- LEAFLET MAP & 3KM DELIVERY RADIUS CHECK ---
