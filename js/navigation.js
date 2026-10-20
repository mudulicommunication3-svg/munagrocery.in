/* munnamobile :: js/navigation.js  (extracted from index.html lines 2460-2584)
   screen navigation + customer orders list */
    function showScreen(name) {
      ['cart','orders','address','profile'].forEach(s => {
        var el = document.getElementById(s + 'Screen');
        if (el) el.classList.toggle('hidden', s !== name);
      });
      // ସବୁ ସେକେଣ୍ଡାରୀ ସ୍କ୍ରିନ (cart/orders/address/profile) ବନ୍ଦ ହେଲେ productDetail
      // ମଧ୍ୟ ବନ୍ଦ କର — ଯେପରି ତଳ ବଟନ ଦବାଇଲେ ହୋମ୍ କାଟାଲଗ୍ ଦେଖାଯାଏ (Flipkart ପରି)
      closeProductDetail(false);
      // OWN URL PER PAGE — refresh reopens exactly this page:
      //   home  -> clean URL (no hash, no ?item)
      //   other -> #cart / #orders / #address / #profile (init.js restores it
      //            after refresh); other params are preserved and ?item is
      //            dropped so a product link never leaks onto other screens.
      // (No push when arriving from popstate — already synced by the browser.)
      if (!_backNavGuard) {
        try {
          var u = new URL(window.location.href);
          var wantHash = (name === 'home') ? '' : ('#' + name);
          u.searchParams.delete('item');
          if (u.hash !== wantHash) {
            history.pushState({ screen: name }, '',
              u.pathname + (u.search ? u.search : '') + wantHash);
          }
        } catch (eU) {
          if (name !== 'home') history.pushState({ screen: name }, '', '#' + name);
        }
      }
      document.querySelectorAll('.nav-btn').forEach(b => {
        var on = b.dataset.nav === name;
        b.classList.toggle('text-[#1864ab]', on);
        b.classList.toggle('text-slate-500', !on);
      });
      // Floating cart bar belongs to the HOME screen only (and only when the
      // cart has items — renderBar() applies the count-based visibility + body
      // padding). On other screens it is hidden and padding shrinks to nav-only.
      var fbar = document.getElementById('floatingCartBar');
      if (name === 'home') {
        renderBar(); // toggles bar visibility + pb-32/pb-20 by item count
      } else if (fbar) {
        fbar.classList.add('hidden');
        document.body.classList.remove('pb-32');
        document.body.classList.add('pb-20');
      }
      if (name === 'cart') {
        // Re-entering with an empty cart (or after a placed order) restarts
        // the flow at Step 1; otherwise the last checkout step is kept.
        // BUGFIX: after an order the "Success" card stayed visible even after
        // new items were added for the NEXT order (checkout button hidden,
        // only the cached "order done" card showed) - so also restart at
        // Step 1 whenever the success card is the visible step.
        var _succEl = document.getElementById('stepSuccess');
        var _successShown = _succEl && !_succEl.classList.contains('hidden');
        if (!Object.keys(cart).length || _successShown) setStep('Cart');
        renderCartDrawer();
        var cs = document.getElementById('cartScreen');
        if (cs) cs.scrollTop = 0;
      }
      if (name === 'orders') {
        renderOrders();
        // Pull Telegram (bot) orders too - the Sheet is the shared database.
        syncOrdersFromSheet();
      }
      if (name === 'address') { renderAddressList(); syncAddressesFromSheet(); }
      if (name === 'profile') renderProfile();
      if (name === 'home') window.scrollTo({ top: 0, behavior: 'smooth' });
    }
    function closeScreen(name) { showScreen('home'); }

    // --- 5-STAGE LIVE ORDER TRACKING ---
    function renderOrders() {
      var box = document.getElementById('ordersList');
      // Auto-filter: the logged-in customer's phone always wins so they see
      // their live 5-stage tracking + past purchases. The manual box is only
      // used when logged out (tracking someone else's order).
      var sess = getSessionUser();
      var phoneEl = document.getElementById('trackOrderPhoneInput');
      var filterPhone = '';
      if (sess && sess.phone) {
        filterPhone = String(sess.phone).trim();
        if (phoneEl) phoneEl.value = filterPhone;
      } else {
        filterPhone = phoneEl ? phoneEl.value.trim() : '';
      }
      var orders = getMyOrders();

      if (filterPhone) {
        orders = orders.filter(o => String(o.phone || '').includes(filterPhone));
      }

      // LATEST ORDER FIRST — newest placed orders on top of the list.
      orders.sort(function (a, b) { return parseOrderTs_(b) - parseOrderTs_(a); });

      if (!orders.length) {
        box.innerHTML = '<div class="text-center py-16"><i class="ph-bold ph-receipt text-5xl text-slate-300"></i><p class="text-xs text-slate-400 mt-2">କୌଣସି ଅର୍ଡର ମିଳିଲା ନାହିଁ।</p></div>';
        return;
      }

      const stages = [
        { num: 1, label: "Order Placed", desc: "ଅର୍ଡର ଗ୍ରହଣ ହେଲା" },
        { num: 2, label: "Accepted", desc: "ଦୋକାନୀ ଅର୍ଡର ଗ୍ରହଣ କଲେ" },
        { num: 3, label: "Packing", desc: "ପ୍ୟାକିଂ ଓ ଷ୍ଟକ୍ ଯାଞ୍ଚ ସରିଲା" },
        { num: 4, label: "Out for Delivery", desc: "ଡେଲିଭରୀ ବାହାରିଛି" },
        { num: 5, label: "Delivered", desc: "ସଫଳତାର ସହ ପହଞ୍ଚିଲା" }
      ];

      box.innerHTML = orders.map(o => {
        var stNum = o.stage || 1;
        var stepperHtml = `<div class="stepper my-2.5"><div class="stepper-line"></div>` + 
          stages.map(st => `
            <div class="step-item ${stNum > st.num ? 'done' : ''} ${stNum === st.num ? 'active' : ''}">
              <div class="step-dot"></div>
              <div class="text-xs font-bold text-slate-800">${st.label}</div>
              <div class="text-[10px] text-slate-400">${st.desc}</div>
            </div>
          `).join('') + `</div>`;

        return `
          <div class="bg-white p-3.5 rounded-2xl border border-slate-200 shadow-xs">
            <div class="flex items-center justify-between">
              <span class="font-black text-slate-800 text-xs">#${o.id}</span>
              <span class="text-[9px] bg-blue-100 text-blue-800 font-extrabold px-2 py-0.5 rounded-full">Stage ${stNum}/5</span>
            </div>
            <p class="text-[10px] text-slate-400 mt-0.5">🕐 ଅର୍ଡର୍ ସମୟ: ${orderPlacedText_(o)} · ${o.payment}</p>
            <p class="text-[10px] font-bold text-emerald-700 mt-0.5">⏳ ଆଶାିତ ସମୟ: ${orderExpectedText_(o)}</p>
            <div class="my-2 p-2 bg-slate-50 rounded-xl space-y-1">
              ${(o.items||[]).map(i => `<div class="flex justify-between text-[11px]"><span class="text-slate-600">${i.name} x${i.qty}</span><span class="font-bold">Rs.${money((parseFloat(i.price)||0)*i.qty)}</span></div>`).join('')}
            </div>
            <div class="flex justify-between pt-1 border-t border-slate-100 text-xs font-black">
              <span>Total Bill</span>
              <span class="text-[#1864ab]">Rs.${money(o.total)}</span>
            </div>
            ${stepperHtml}
            ${['pending', 'accepted', 'packing'].indexOf(o.status) >= 0
              ? `<button onclick="cancelMyOrder('${o.id}')" class="mt-2 w-full py-1.5 rounded-xl bg-rose-50 text-rose-600 text-[11px] font-bold border border-rose-200">❌ ଅର୍ଡର ବାତିଲ୍ କରନ୍ତୁ (ଡେଲିଭରୀ ବାହାରିବା ଆଗରୁ)</button>`
              : (o.status === 'cancelled' ? '<p class="mt-2 text-center text-[11px] font-bold text-rose-600 bg-rose-50 rounded-xl py-1.5 border border-rose-200">❌ ଏହି ଅର୍ଡର ବାତିଲ୍ ହୋଇଛି</p>' : '')}
          </div>
        `;
      }).join('');
    }

    // ====================================================================
    // ADMIN AUTH (10-digit username + password, Google Sheet backed)
    // Session persisted as localStorage 'isAdminLoggedIn' = '1' so no later
    // feature/setting ever asks for a password again on this device.
    // ====================================================================
    var ADMIN_SESSION_KEY = 'isAdminLoggedIn';
