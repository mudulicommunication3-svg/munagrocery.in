/* munnamobile :: js/address.js  (extracted from index.html lines 5051-5204)
   address book CRUD + saved address picker */
    function prefillOrderForm() {
      var u = getTgUser();
      var s = getSessionUser();
      var addrs = getAddresses();
      var def = addrs.find(a => a.isDefault) || addrs[0];
      if (def) {
        // Session profile wins; saved default address fills the rest.
        document.getElementById('orderCustName').value = (s && s.name) || def.name || ((u && u.first_name) || '');
        document.getElementById('orderCustPhone').value = (s && s.phone) || def.phone;
        document.getElementById('orderCustAddress').value = def.address;
      } else if (s) {
        document.getElementById('orderCustName').value = s.name || '';
        document.getElementById('orderCustPhone').value = s.phone || '';
      } else if (u) {
        document.getElementById('orderCustName').value = u.first_name || '';
      }
    }

    function renderSavedAddressPick() {
      var box = document.getElementById('savedAddressPick');
      var list = getAddresses();
      if (!list.length) { box.innerHTML = ''; return; }
      box.innerHTML = list.slice(0,2).map(a => `
        <div onclick="pickSavedAddress('${a.id}')" class="p-2 rounded-xl border border-slate-200 bg-white cursor-pointer text-[11px]">
          <span class="font-bold">${a.name}</span> · <span class="text-slate-500">${a.phone}</span>
          <p class="text-slate-600 line-clamp-1">${a.address}</p>
        </div>
      `).join('');
    }

    function pickSavedAddress(id) {
      var a = getAddresses().find(x => x.id === id);
      if (!a) return;
      document.getElementById('orderCustName').value = a.name;
      document.getElementById('orderCustPhone').value = a.phone;
      document.getElementById('orderCustAddress').value = a.address;
    }

    // ✅ Auto-save the checkout address so it is pre-filled next time.
    // Called from placeMiniOrder after a successful order. Updates an
    // existing entry with the same phone, otherwise appends a new one.
    function autoSaveCheckoutAddress_(name, phone, address) {
      name = String(name || '').trim();
      phone = String(phone || '').trim();
      address = String(address || '').trim();
      if (!phone || !address) return false;
      if (!/^[6-9]\d{9}$/.test(phone)) return false;
      var list = getAddresses();
      var existing = list.find(function (x) {
        return String(x.phone || '').trim() === phone;
      });
      var saved;
      if (existing) {
        existing.name = name || existing.name;
        existing.address = address;
        saved = existing;
      } else {
        saved = { id: 'A' + Date.now(), name: name || 'Customer', phone: phone, address: address, isDefault: list.length === 0 };
        list.unshift(saved);
      }
      setAddresses(list);
      // Push to the shared Sheet (bot/admin read the same address book).
      try { pushAddressToSheet(saved); } catch (e) {}
      return true;
    }

    function openAddressForm(id) {
      document.getElementById('addressForm').classList.remove('hidden');
      document.getElementById('addrEditId').value = id || '';
    }
    function closeAddressForm() { document.getElementById('addressForm').classList.add('hidden'); }
    function saveAddressForm() {
      var id = document.getElementById('addrEditId').value;
      var name = document.getElementById('addrName').value.trim();
      var phone = document.getElementById('addrPhone').value.trim();
      var address = document.getElementById('addrText').value.trim();
      var isDefault = document.getElementById('addrDefault').checked;
      if (!name || !phone || !address) { alert("ସମସ୍ତ ତଥ୍ୟ ପୂରଣ କରନ୍ତୁ!"); return; }
      var list = getAddresses();
      if (isDefault) list.forEach(a => a.isDefault = false);
      if (id) {
        var a = list.find(x => x.id === id);
        if (a) { a.name = name; a.phone = phone; a.address = address; a.isDefault = isDefault; }
      } else {
        list.push({ id: 'A' + Date.now(), name, phone, address, isDefault: isDefault || list.length === 0 });
      }
      setAddresses(list);
      // If this phone matches the logged-in user, refresh their profile name too.
      try {
        var sess = getSessionUser();
        if (sess && String(sess.phone || '').trim() === String(phone || '').trim() && name) {
          sess.name = name;
          setSessionUser(sess);
        }
      } catch (e) {}
      // Sync to the shared database (Telegram bot reads the same entry).
      pushAddressToSheet((id && list.find(x => x.id === id)) || list[list.length - 1]);
      closeAddressForm();
      renderAddressList();
    }
    function renderAddressList() {
      var box = document.getElementById('addressList');
      var list = getAddresses();
      if (!list.length) { box.innerHTML = '<p class="text-center text-xs text-slate-400 py-6">କୌଣସି ଠିକଣା ସେଭ୍ ହୋଇନାହିଁ।</p>'; return; }
      box.innerHTML = list.map(a => `
        <div class="bg-white p-3 rounded-2xl border shadow-xs ${a.isDefault ? 'border-emerald-400 ring-1 ring-emerald-200' : 'border-slate-200'}">
          <div class="flex items-center justify-between gap-2">
            <div class="font-bold text-xs min-w-0">${a.name} · ${a.phone}</div>
            ${a.isDefault ? '<span class="text-[9px] bg-emerald-50 text-emerald-700 font-black px-1.5 py-0.5 rounded shrink-0">✓ DEFAULT</span>' : ''}
          </div>
          <div class="text-[11px] text-slate-600 mt-0.5">${a.address}</div>
          <div class="flex gap-1.5 mt-2">
            <button onclick="openAddressFormEdit_('${a.id}')" class="flex-1 py-1.5 rounded-lg bg-blue-50 text-[#1864ab] text-[10px] font-black border border-blue-100">✏️ Edit</button>
            ${a.isDefault ? '' : `<button onclick="setDefaultAddress_('${a.id}')" class="flex-1 py-1.5 rounded-lg bg-emerald-50 text-emerald-700 text-[10px] font-black border border-emerald-200">✓ Default କରନ୍ତୁ</button>`}
            <button onclick="deleteAddress_('${a.id}')" class="flex-1 py-1.5 rounded-lg bg-rose-50 text-rose-600 text-[10px] font-black border border-rose-200"><i class="ph-bold ph-trash"></i> Delete</button>
          </div>
        </div>
      `).join('');
    }
    // Address book: edit fill-in, default switching, delete.
    function openAddressFormEdit_(id) {
      var a = getAddresses().find(x => x.id === id);
      if (!a) return;
      openAddressForm(id);
      document.getElementById('addrName').value = a.name || '';
      document.getElementById('addrPhone').value = a.phone || '';
      document.getElementById('addrText').value = a.address || '';
      document.getElementById('addrDefault').checked = !!a.isDefault;
    }
    function setDefaultAddress_(id) {
      var list = getAddresses();
      list.forEach(function (a) { a.isDefault = (String(a.id) === String(id)); });
      setAddresses(list);
      var d = list.find(function (a) { return String(a.id) === String(id); });
      if (d) pushAddressToSheet(d);
      renderAddressList();
      prefillOrderForm();
    }
    function deleteAddress_(id) {
      var list = getAddresses();
      var gone = list.filter(function (a) { return String(a.id) === String(id); })[0];
      list = list.filter(function (a) { return String(a.id) !== String(id); });
      if (gone && gone.isDefault && list.length) list[0].isDefault = true;
      setAddresses(list);
      try {
        if (gone && gone.phone) {
          var sess = getSessionUser();
          if (sess && String(sess.phone || '').trim() === String(gone.phone || '').trim()) logoutUser();
        }
      } catch (e) {}
      renderAddressList();
      renderProfile();
    }

