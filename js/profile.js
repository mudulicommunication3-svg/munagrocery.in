/* munnamobile :: js/profile.js  (extracted from index.html lines 5205-5282)
   profile screen, search handlers, OD/EN language */
    function renderProfile() {
      var s = getSessionUser();
      var u = getTgUser();
      var name = (s && s.name) || ((u && u.first_name) || 'Customer');
      var phone = (s && s.phone) || '';
      var tag = (s && s.provider) ? { otp: '📱 OTP', google: '🔵 Google', telegram: '✈️ Telegram', guest: '👋 Guest' }[s.provider] || '' : '';
      document.getElementById('profName').innerText = name;
      document.getElementById('profAvatar').innerText = name.charAt(0).toUpperCase();
      document.getElementById('profPhone').innerText = phone ? ('📞 ' + phone) : '';
      var pid = document.getElementById('profId');
      if (pid) pid.innerText = tag + ((s && s.tg_id) ? (' · TG ' + s.tg_id) : '');
      var card = document.getElementById('authRowCard');
      if (card) {
        card.innerHTML = s
          ? '<div class="flex items-center justify-between gap-2">' +
            '<span class="text-xs text-slate-600">👋 <b class="text-slate-800">' + name + '</b>' +
            (tag ? ' <span class="ml-1 text-[10px] bg-emerald-50 text-emerald-700 font-bold px-1.5 py-0.5 rounded">' + tag + '</span>' : '') + '</span>' +
            '<button onclick="logoutUser()" class="px-3 py-1.5 rounded-lg bg-rose-50 hover:bg-rose-100 text-rose-600 border border-rose-200 text-[11px] font-black flex items-center gap-1"><i class="ph-bold ph-sign-out"></i> Logout</button>' +
            '</div>'
          : '<button onclick="openLoginModal()" class="w-full py-2.5 rounded-xl bg-[#1864ab] hover:bg-blue-800 text-white font-extrabold text-xs flex items-center justify-center gap-1.5"><i class="ph-bold ph-user-circle"></i> ଲଗଇନ୍ / ସାଇନ୍-ଅପ୍ କରନ୍ତୁ (Login)</button>';
      }
      var myOrders = getMyOrders();
      var ocount = (s && s.phone)
        ? myOrders.filter(function (o) { return String(o.phone || '').indexOf(s.phone) >= 0; }).length
        : myOrders.length;
      document.getElementById('profOrderCount').innerText = ocount;
      var myAddrs = getAddresses();
      var acount = (s && s.phone)
        ? myAddrs.filter(function (a) { return String(a.phone || '').trim() === s.phone; }).length
        : myAddrs.length;
      document.getElementById('profAddrCount').innerText = acount;

      // Admin visibility: the Panel tile only appears for a logged-in admin;
      // the portal shows [Admin Login] or [Logout Admin] accordingly.
      var adminOn = isAdmin_();
      var tile = document.getElementById('profAdminTile');
      if (tile) tile.classList.toggle('hidden', !adminOn);
      var loginBtn = document.getElementById('profAdminLoginBtn');
      if (loginBtn) loginBtn.classList.toggle('hidden', adminOn);
      var logoutBtn = document.getElementById('profAdminLogoutBtn');
      if (logoutBtn) logoutBtn.classList.toggle('hidden', !adminOn);

      // Language selector highlight (OD / EN) in the Profile card.
      var od = document.getElementById('langBtnOD');
      var en = document.getElementById('langBtnEN');
      if (od && en) {
        od.className = 'px-3 py-1.5 rounded-lg text-[11px] font-black border transition-all ' +
          (isEnglish ? 'bg-slate-50 text-slate-500 border-slate-200'
                     : 'bg-[#1864ab] text-white border-[#1864ab]');
        en.className = 'px-3 py-1.5 rounded-lg text-[11px] font-black border transition-all ' +
          (isEnglish ? 'bg-[#1864ab] text-white border-[#1864ab]'
                     : 'bg-slate-50 text-slate-500 border-slate-200');
      }
    }

    function handleSearch(v) { query = v || ""; document.getElementById('clearSearchBtn').classList.toggle('hidden', !query); render(); }
    function clearSearch() { document.getElementById('searchInput').value = ""; query = ""; document.getElementById('clearSearchBtn').classList.add('hidden'); render(); }
    // Language selector (Profile screen): explicit OD / EN set.
    function setLang(english) {
      isEnglish = !!english;
      document.getElementById('lblStoreName').innerText = isEnglish ? "MUNNA MOBILE & GROCERY" : "ମୁନ୍ନା ମୋବାଇଲ୍ ଆଣ୍ଡ୍ ଗ୍ରୋସରୀ";
      try { localStorage.setItem('munna_lang', isEnglish ? 'en' : 'od'); } catch (e) {}
      renderProfile(); // refresh the OD/EN button highlight
    }
    // Back-compat alias (old header chip called toggleLang()).
    function toggleLang() { setLang(!isEnglish); }

    // --- PRODUCT SCANNER (EAN/QR: camera + photo + manual) ---
    var scannerCtx = 'customer';   // 'customer' | 'admin' | 'quickbill'
    var html5Qr = null;
    var camRunning = false;
    // Debounce: html5-qrcode fires the SAME code ~10x/sec while the camera
    // stays on it, so a single scan would add many items. Ignore a repeat of
    // the same code within this window (1s) => one scan = one add.
    var _lastScanText = '';
    var _lastScanAt = 0;
    var qbLines = [];              // store-side Quick Bill running lines

