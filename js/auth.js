/* munnamobile :: js/auth.js  (extracted from index.html lines 5613-6125)
   login/signup: password, Google, guest */
    function getSessionUser() {
      try {
        var s = JSON.parse(localStorage.getItem('active_store_user') || 'null');
        if (s) return s;
        return JSON.parse(localStorage.getItem('MUNA_USER_SESSION') || 'null');
      } catch (e) { return null; }
    }
    function setSessionUser(u) {
      try {
        if (u) {
          localStorage.setItem('active_store_user', JSON.stringify(u));
          localStorage.setItem('MUNA_USER_SESSION', JSON.stringify(u)); // legacy mirror
        } else {
          localStorage.removeItem('active_store_user');
          localStorage.removeItem('MUNA_USER_SESSION');
        }
      } catch (e) {}
      updateHeaderAuth();
    }
    function loginDone(u, msg) {
      if (!u.phone && u.provider !== 'guest') return false;
      setSessionUser(u);
      closeLoginModal();
      try {
        var addrs = getAddresses();
        var match = addrs.filter(function (a) { return String(a.phone || '').trim() === String(u.phone || '').trim(); })[0];
        if (match && u.name) {
          match.name = match.name || u.name;
          setAddresses(addrs);
        }
      } catch (e) {}
      // Sync the profile into the shared "Users" tab (dedup by phone).
      syncUserToSheet(u);
      prefillOrderForm();
      if (msg) alert(msg);
      return true;
    }
    function logoutUser() {
      setSessionUser(null);   // clears BOTH session keys + header -> "Login"
      try {
        // Reset every cached input so the next visitor inherits nothing.
        ['trackOrderPhoneInput', 'orderCustName', 'orderCustPhone', 'orderCustAddress',
         'loginPhone', 'loginPassword', 'loginName', 'loginAddr'].forEach(function (id) {
          var el = document.getElementById(id);
          if (el) el.value = '';
        });
        var h = document.getElementById('otpHint');
        if (h) { h.classList.add('hidden'); h.innerText = ''; }
      } catch (e) {}
      // Clear the account cart (memory + per-user localStorage) and refresh
      // the floating bar / badge so no basket leaks to the next visitor.
      try {
        cart = {};
        appliedCoupon = '';
        clearCartCache_();
        updateCartBadge();
        renderBar();
        renderCartDrawer();
      } catch (e) {}
      // Wipe the cached order history so only the next login's orders show.
      try { setMyOrders([]); } catch (e) {}
      renderProfile();
      // Return to Home cleanly (floating bar shows only on home).
      showScreen('home');
      var os = document.getElementById('ordersScreen');
      if (os && !os.classList.contains('hidden')) renderOrders();
    }
    function sessionFirstName() {
      var s = getSessionUser();
      var n = s && s.name ? String(s.name).trim().split(' ')[0] : '';
      return n || (s && s.phone ? '👤' : '');
    }

    // Header auth chip REMOVED (Login/Logout now live in the Profile screen's
    // authRowCard). Kept as a safe no-op so existing callers don't break.
    function updateHeaderAuth() { /* no-op: header chip removed */ }
    function onHeaderAuth() {
      // Old header pill behaviour -> route to the Profile screen instead.
      showScreen('profile');
    }

    // Login modal: reset to the Login (password) tab + clear fields on open.
    function openLoginModal() {
      document.getElementById('loginModal').classList.remove('hidden');
      if (!(history.state && history.state.modal === 'login')) history.pushState({ modal: 'login' }, '', window.location.href);
      document.getElementById('loginPhone').value = '';
      document.getElementById('loginPassword').value = '';
      var nm = document.getElementById('loginName');
      var ad = document.getElementById('loginAddr');
      if (nm) nm.value = '';
      if (ad) ad.value = '';
      var hint = document.getElementById('otpHint');
      if (hint) { hint.classList.add('hidden'); hint.innerText = ''; }
      switchLoginTab('pw');
      setTimeout(function () { var el = document.getElementById('loginPhone'); if (el) el.focus(); }, 80);
    }
    function closeLoginModal() { document.getElementById('loginModal').classList.add('hidden'); }

    // Login modal tabs: 'pw' = Login (phone + password), 'signup' = Register.
    function switchLoginTab(mode) {
      var isLogin = (mode !== 'signup');
      var loginTab = document.getElementById('loginTabPw');
      var signupTab = document.getElementById('loginTabSignup');
      var fName = document.getElementById('fName');
      var fAddr = document.getElementById('fAddr');
      var btnLogin = document.getElementById('btnLogin');
      var btnSignup = document.getElementById('btnSignup');
      if (loginTab) {
        loginTab.className = isLogin
          ? 'flex-1 py-2.5 text-center border-b-2 border-[#1864ab] text-[#1864ab]'
          : 'flex-1 py-2.5 text-center border-b-2 border-transparent';
      }
      if (signupTab) {
        signupTab.className = isLogin
          ? 'flex-1 py-2.5 text-center border-b-2 border-transparent'
          : 'flex-1 py-2.5 text-center border-b-2 border-[#25D366] text-[#128C7E]';
      }
      if (fName) fName.classList.toggle('hidden', isLogin);
      if (fAddr) fAddr.classList.toggle('hidden', isLogin);
      if (btnLogin) btnLogin.classList.toggle('hidden', !isLogin);
      if (btnSignup) btnSignup.classList.toggle('hidden', isLogin);
    }
    function validPhone_(ph) { return /^[6-9]\d{9}$/.test(String(ph || '').trim()); }

    // Push the profile into the shared "Users" tab (Apps Script SYNC_USER,
    // dedup by phone). Fire-and-forget; errors never block the login.
    function syncUserToSheet(u) {
      if (!u || !u.phone) return;
      try {
        var defAddr = '';
        var m = getAddresses().filter(function (a) { return String(a.phone || '').trim() === String(u.phone || '').trim(); })[0];
        if (m && m.address) defAddr = m.address;
        postToSheet({
          action: 'SYNC_USER',
          user_id: u.tg_id || String(u.phone),
          telegram_user_id: u.tg_id || '',
          name: u.name || '',
          phone: String(u.phone),
          address: defAddr,
          total_orders: (getMyOrders() || []).filter(function (o) { return String(o.phone) === String(u.phone); }).length
        });
      } catch (e) {}
    }

    // Password login: 100% serverless via the Apps Script webhook (LOGIN_PASSWORD).
    // The built-in default webhook means login works on fresh browsers too.
    function pwHook_() {
      return window.munnaHookBase();
    }
    // LOGIN button: verify phone + password against the Users store.
    // LOGIN button: send TRIMMED phone + password to the Apps Script webhook.
    // LOGIN_PASSWORD returns { ok, phone, name, address, orders, login_token }.
    function loginPassword_() {
      var phone = String(document.getElementById('loginPhone').value || '').replace(/\D/g, '').slice(-10);
      var password = String(document.getElementById('loginPassword').value || '').trim();
      if (!validPhone_(phone)) { alert('ଦୟାକରି ଠିକ୍ 10-ଅଙ୍କ ମୋବାଇଲ୍ ନମ୍ବର ଦିଅନ୍ତୁ।'); return; }
      if (!password || password.length < 6) { alert('❌ ପାସୱାର୍ଡ ଅନ୍ତତଃ 6 ଅକ୍ଷର ହେବା ଉଚିତ୍।'); return; }
      document.getElementById('loginPhone').value = phone;   // reflect sanitised value
      var hint = document.getElementById('otpHint');
      if (hint) { hint.classList.remove('hidden'); hint.innerText = '⏳ ଲଗଇନ୍ ହେଉଛି…'; }
      var hook = pwHook_();
      if (!hook) {
        // Local (no webhook) demo fallback — session still saves.
        loginDone_({ name: 'Customer', phone: phone, provider: 'password', login_token: 'local' });
        return;
      }
      fetch(hook, {
        method: 'POST',
        headers: { 'Content-Type': 'text/plain;charset=utf-8' },
        body: JSON.stringify({ action: 'LOGIN_PASSWORD', phone: phone, password: password })
      }).then(function (r) { return r.json(); }).then(function (d) {
        var ok = d && (d.ok === true || d.status === 'success' || d.success === true);
        if (ok) {
          loginDone_({
            name: d.name || 'Customer',
            phone: d.phone || phone,
            address: d.address || '',
            orders: Array.isArray(d.orders) ? d.orders : [],
            cart: (d.cart && typeof d.cart === 'object') ? d.cart : null,
            login_token: d.login_token || '',
            provider: 'password'
          });
        } else {
          var err = (d && d.error) || 'ଲଗଇନ୍ ବିଫଳ।';
          if (hint) { hint.classList.remove('hidden'); hint.innerText = '❌ ' + err; }
          alert('❌ ' + err);
        }
      }).catch(function () {
        loginDone_({ name: 'Customer', phone: phone, provider: 'password', login_token: 'local' });
      });
    }
    // SIGN-UP button: create the account (name + phone + password + address).
    // Sends the TRIMMED/normalised phone + password to REGISTER_USER.
    function registerUser_() {
      var name = String(document.getElementById('loginName').value || '').trim();
      var phone = String(document.getElementById('loginPhone').value || '').replace(/\D/g, '').slice(-10);
      var password = String(document.getElementById('loginPassword').value || '').trim();
      var addr = String(document.getElementById('loginAddr').value || '').trim();
      if (!name) { alert('ଦୟାକରି ଆପଣଙ୍କ ନାଁ ଲେଖନ୍ତୁ।'); return; }
      if (!validPhone_(phone)) { alert('ଦୟାକରି ଠିକ୍ 10-ଅଙ୍କ ମୋବାଇଲ୍ ନମ୍ବର ଦିଅନ୍ତୁ।'); return; }
      if (!password || password.length < 6) { alert('❌ ପାସୱାର୍ଡ ଅନ୍ତତଃ 6 ଅକ୍ଷର ହେବା ଉଚିତ୍।'); return; }
      document.getElementById('loginPhone').value = phone;
      var hint = document.getElementById('otpHint');
      if (hint) { hint.classList.remove('hidden'); hint.innerText = '⏳ Account ତିଆରି ହେଉଛି…'; }
      var hook = pwHook_();
      if (!hook) {
        loginDone_({ name: name, phone: phone, address: addr, provider: 'password', login_token: 'local' });
        return;
      }
      fetch(hook, {
        method: 'POST',
        headers: { 'Content-Type': 'text/plain;charset=utf-8' },
        body: JSON.stringify({ action: 'REGISTER_USER', name: name, phone: phone, password: password, address: addr })
      }).then(function (r) { return r.json(); }).then(function (d) {
        var ok = d && (d.ok === true || d.status === 'success' || d.success === true);
        if (ok) {
          loginDone_({
            name: name, phone: d.phone || phone, address: addr,
            orders: Array.isArray(d.orders) ? d.orders : [],
            login_token: d.login_token || '', provider: 'password'
          });
        } else {
          var err = (d && d.error) || 'Account ତିଆରି ବିଫଳ।';
          if (hint) { hint.classList.remove('hidden'); hint.innerText = '❌ ' + err; }
          alert('❌ ' + err);
        }
      }).catch(function () {
        loginDone_({ name: name, phone: phone, address: addr, provider: 'password', login_token: 'local' });
      });
    }
    // Shared success path: cache backend order history + saved address, save
    // the session (header shows "Name ✓"), refresh profile, land on Home.
    function loginDone_(user) {
      var typedName = ((document.getElementById('loginName') || {}).value || '').trim();
      var known = '';
      try {
        var m = getAddresses().filter(function (a) { return String(a.phone || '').trim() === String(user.phone || '').trim(); })[0];
        if (m && m.name) known = m.name;
      } catch (e) {}
      var tgu = getTgUser();
      var u = {
        name: typedName || user.name || known || ((tgu && tgu.first_name) || '') || 'Customer',
        phone: String(user.phone || '').replace(/\D/g, '').slice(-10),
        tg_id: (tgu && tgu.id) ? String(tgu.id) : '',
        address: user.address || '',
        login_token: user.login_token || '',
        provider: user.provider || 'password'
      };
      // 1. Merge the customer's past orders (LOGIN_PASSWORD orders[]) into cache
      //    so the Orders tab + 5-stage stepper show full history immediately on
      //    ANY device (renderOrders() is called right away, not only if visible).
      if (Array.isArray(user.orders) && user.orders.length) mergeOrdersFromBackend_(user.orders);
      // 2. Persist the backend's saved address into munna_addresses + checkout.
      if (u.address) saveProfileAddress_(u);
      // 2b. Restore the CLOUD CART (account-wise sync): the server's saved
      //     basket is the source of truth, so REPLACE the in-memory cart and
      //     refresh badge, floating bar, render() and drawer.
      if (user.cart && typeof user.cart === 'object' && !Array.isArray(user.cart)) {
        applyCloudCart_(user.cart);
      }
      // 3. Session -> 'active_store_user' + 'MUNA_USER_SESSION', header "Name ✓",
      //    close modal, sync Users tab, prefill checkout, success alert.
      loginDone(u, '✅ ଲଗଇନ୍ ସଫଳ ହେଲା! ସ୍ୱାଗତ, ' + u.name + '।');
      // 4. Refresh profile + point the Orders filter at THIS customer.
      renderProfile();
      var pi = document.getElementById('trackOrderPhoneInput');
      if (pi && u.phone) pi.value = u.phone;
      // 5. Land smoothly on the home screen.
      showScreen('home');
    }

    // Merge backend order history into the in-memory _allOrders (dedup by
    // order_id) and refresh the Orders tab / 5-stage stepper if visible.
    function mergeOrdersFromBackend_(remote) {
      try {
        if (!Array.isArray(remote) || !remote.length) return false;
        var byId = {};
        getMyOrders().forEach(function (o) { byId[o.order_id] = o; });
        remote.forEach(function (r) {
          var n = normalizeOrder(r);
          if (n) byId[n.order_id] = n;
        });
        var merged = Object.keys(byId).map(function (k) { return byId[k]; })
          .sort(function (a, b) { return String(b.date).localeCompare(String(a.date)); });
        setMyOrders(merged);
        renderOrders(); // refresh the Orders tab immediately on any device
        return true;
      } catch (e) { return false; }
    }

    // Save the backend's Default Address into munna_addresses (as default) so
    // prefillOrderForm() / checkout show it right after login.
    function saveProfileAddress_(u) {
      try {
        var name = String(u.name || '').trim();
        var phone = String(u.phone || '').trim();
        var addr = String(u.address || '').trim();
        if (!phone || !addr) return false;
        var list = getAddresses();
        var hit = list.find(function (x) { return String(x.phone || '').trim() === phone; });
        if (hit) { hit.name = hit.name || name; hit.address = addr; }
        else { list.unshift({ id: 'A' + Date.now(), name: name, phone: phone, address: addr }); }
        list.forEach(function (x) { x.isDefault = (String(x.phone || '').trim() === phone); });
        setAddresses(list);
        return true;
      } catch (e) { return false; }
    }
    // ?pw=<password> quick-login helper (replaces the old mob://wx PHP hook).
    // On localhost it auto-logs-in for fast testing (guarded so a production
    // URL can never log anyone in from a query string); elsewhere it only opens
    // the modal with the password pre-filled (never persisted).
    function demoPwAutoLogin_(pw) {
      try {
        var isLocal = (location.hostname === 'localhost' || location.hostname === '127.0.0.1' || location.hostname === '');
        var ph = document.getElementById('loginPhone');
        var pa = document.getElementById('loginPassword');
        if (pa) pa.value = pw;
        if (isLocal) {
          var s = getSessionUser();
          if (ph && s && s.phone && validPhone_(s.phone)) {
            ph.value = s.phone;
            loginPassword_();
            return;
          }
        }
        openLoginModal();
        if (pa) pa.value = pw;
      } catch (e) {}
    }


    // --- Google Sign-In (One-Tap / OAuth via Google Identity Services) ---
    function getGoogleClientId_() {
      var q = '';
      try { q = new URLSearchParams(window.location.search).get('gcid') || ''; } catch (e) {}
      var saved = '';
      try { saved = localStorage.getItem('munna_google_client_id') || ''; } catch (e) {}
      if (saved) return String(saved).trim();
      if (q) {
        try { localStorage.setItem('munna_google_client_id', String(q).trim()); } catch (e) {}
        return String(q).trim();
      }
      return '';
    }
    function fillGoogleTab() {
      var box = document.getElementById('googleBtnBox');
      var msg = document.getElementById('googleCfgMsg');
      if (!box || !msg) return;
      box.innerHTML = '';
      msg.innerText = '';
      var cid = getGoogleClientId_();
      if (!cid) {
        msg.innerHTML = '⚙️ Admin: Google Cloud Console → OAuth Client ID ବନାଇ URL ରେ <b>?gcid=YOUR_CLIENT_ID</b> ଦିଅନ୍ତୁ (ଥରେ ଦେଲେ ସେଭ୍ ହୋଇଯିବ) ।<br>ଏବେ ପାଇଁ ଡେମୋ ବଟନ୍ ବ୍ୟବହାର କରନ୍ତୁ ।';
        box.innerHTML = '<button onclick="googleDemoLogin()" class="px-4 py-2.5 rounded-xl bg-white border border-slate-300 shadow-sm text-xs font-bold text-slate-700 flex items-center gap-2"><span class="w-4 h-4 rounded-full bg-[#4285F4] text-white text-[10px] font-black flex items-center justify-center">G</span> Demo ଭାବେ Google ଲଗଇନ୍</button>';
        return;
      }
      try {
        msg.innerText = 'ଗୁଗଲ୍ ବଟନ୍ / One-Tap ଖୋଲୁଛି… (OAuth Client ID ସେଟ୍)';
        loadGisScript_(function (ok) { renderGoogleButton_(cid, ok); });
      } catch (e) {
        msg.innerText = '❌ Google script load ହେଲା ନାହିଁ — OTP ବ୍ୟବହାର କରନ୍ତୁ ।';
      }
    }
    function loadGisScript_(cb) {
      if (window.google && window.google.accounts && window.google.accounts.id) { cb(true); return; }
      var s = document.getElementById('gisScriptTag');
      if (s) { setTimeout(function () { cb(!!(window.google && window.google.accounts)); }, 4000); return; }
      var tag = document.createElement('script');
      tag.id = 'gisScriptTag';
      tag.src = 'https://accounts.google.com/gsi/client';
      tag.async = true;
      tag.defer = true;
      tag.onload = function () { cb(true); };
      tag.onerror = function () { cb(false); };
      document.head.appendChild(tag);
    }
    function renderGoogleButton_(cid, ok) {
      var box = document.getElementById('googleBtnBox');
      var msg = document.getElementById('googleCfgMsg');
      if (!ok || !window.google || !window.google.accounts) {
        if (box) box.innerHTML = '<button onclick="googleDemoLogin()" class="px-4 py-2 rounded-xl bg-white border border-slate-300 text-xs font-bold text-slate-700">Demo ଭାବେ ଲଗଇନ୍</button>';
        if (msg) msg.innerText = '❌ Google ଯୋଗାଯୋଗ ନାହିଁ — ଇଣ୍ଟରନେଟ୍ ଯାଞ୍ଚ କରନ୍ତୁ ।';
        return;
      }
      try {
        window.google.accounts.id.initialize({ client_id: cid, callback: googleCredential_, auto_select: false });
        window.google.accounts.id.renderButton(box, { theme: 'outline', size: 'large', width: 250, text: 'signin_with' });
        try { window.google.accounts.id.prompt(); } catch (e) {}
        if (msg) msg.innerText = 'ଆପଣଙ୍କ ଗୁଗଲ୍ ଆକାଉଣ୍ଟ୍ ବାଛନ୍ତୁ ।';
      } catch (e) {
        if (msg) msg.innerText = '❌ Google ସେଟଅପ୍ ତ୍ରୁଟି — Client ID ଯାଞ୍ଚ କରନ୍ତୁ ।';
      }
    }
    function googleCredential_(resp) {
      var payload = {};
      try {
        var parts = String((resp && resp.credential) || '').split('.');
        payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));
      } catch (e) { payload = {}; }
      var email = payload.email || '';
      try {
        var savedPh = localStorage.getItem('munna_google_phone_' + email) || '';
        if (!savedPh) {
          var ask = prompt('📱 ଗୁଗଲ୍ ଅର୍ଡର ପାଇଁ ମୋବାଇଲ୍ ନମ୍ବର ଦିଅନ୍ତୁ (10 ଅଙ୍କ):', '') || '';
          ask = String(ask).replace(/\D/g, '').slice(-10);
          if (/^[6-9]\d{9}$/.test(ask)) {
            savedPh = ask;
            try { localStorage.setItem('munna_google_phone_' + email, ask); } catch (e) {}
          }
        }
        if (!savedPh) { alert('ମୋବାଇଲ୍ ନମ୍ବର ବିନା ଅର୍ଡର କରିପାରିବେ ନାହିଁ ।'); return; }
        var tgu = getTgUser();
        loginDone({
          name: payload.name || payload.given_name || email.split('@')[0] || 'Customer',
          phone: savedPh, email: email,
          tg_id: (tgu && tgu.id) ? String(tgu.id) : '',
          provider: 'google'
        }, '✅ Google ଲଗଇନ୍ ସଫଳ! ସ୍ୱାଗତ ।');
        renderProfile();
      } catch (e) { alert('❌ Google ଲଗଇନ୍ ବିଫଳ ହେଲା ।'); }
    }
    function googleDemoLogin() {
      var nm = (prompt('📝 ଡେମୋ Google ଲଗଇନ୍ — ଆପଣଙ୍କ ନାଁ:', '') || '').trim();
      if (!nm) return;
      var ph = (prompt('📱 ମୋବାଇଲ୍ ନମ୍ବର (10 ଅଙ୍କ):', '') || '').replace(/\D/g, '').slice(-10);
      if (!/^[6-9]\d{9}$/.test(ph)) { alert('ଠିକ୍ ନମ୍ବର ଦିଅନ୍ତୁ ।'); return; }
      var tgu = getTgUser();
      loginDone({
        name: nm, phone: ph,
        tg_id: (tgu && tgu.id) ? String(tgu.id) : '',
        provider: 'google'
      }, '✅ (Demo) Google ଲଗଇନ୍ ସଫଳ! ସ୍ୱାଗତ, ' + nm + ' ।');
      renderProfile();
    }

    // --- Guest mode: browse only. Ordering now REQUIRES a login. ---
    // Keeping the function name so any stale onclick/caller still resolves,
    // but it no longer opens the cart for checkout — it asks for login.
    function guestCheckout() {
      closeLoginModal();
      try {
        if (getSessionUser()) {
          // Already logged in (OTP/Google) -> proceed to cart.
          showScreen('cart');
          return;
        }
      } catch (e) {}
      alert('🔒 ଅର୍ଡର କରିବାକୁ ଲଗଇନ୍ ଆବଶ୍ୟକ। ଦୟାକରି OTP / Google ରେ ଲଗଇନ୍ କରନ୍ତୁ।');
      openLoginModal();
    }

    // Login gate used at checkout entry + before placing an order.
    // Browsing/searching stays open; only ORDER actions require a session.
    function requireLoginForOrder_(nextStep) {
      try {
        if (getSessionUser()) {
          if (typeof nextStep === 'function') nextStep();
          return true;
        }
      } catch (e) {}
      alert('🔒 ଅର୍ଡର ପାଇଁ ଲଗଇନ୍ ଆବଶ୍ୟକ। ପ୍ରଥମେ OTP / Google ରେ ଲଗଇନ୍ କରନ୍ତୁ।');
      openLoginModal();
      return false;
    }

    // --- INITIALIZATION ---
