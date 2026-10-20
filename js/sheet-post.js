/* munnamobile :: js/sheet-post.js  (extracted from index.html lines 4653-4826)
   posting to Google Sheet webhook (retries, offline queue) */
    function sendToBot(payload) {
      // TELEGRAM MINI APP REMOVED — the shop is our own HTML served from
      // munagrocery.in. Orders/addresses always go through the Apps Script
      // webhook (postToSheet / create_order), never Telegram sendData().
      return false;
    }

    // --- SHARED-DATABASE HELPERS (Google Sheet via Apps Script webhook) ---
    // Webhook resolution: saved Admin -> Sheets override wins, otherwise the
    // built-in MUNNA_DEFAULT_WEBHOOK (head of this file). Fresh browsers and
    // cleared-data browsers therefore talk to YOUR Sheet immediately.
    function sheetHookUrl(suffix) {
      var hook = window.munnaHookBase();
      if (!hook) return '';
      return hook + (hook.indexOf('?') >= 0 ? '&' : '?') + (suffix || '');
    }
    // ===== Sheet POST — CORS-preflight immune =====
    // ROOT CAUSE (live-tested): a cross-origin POST with a custom header
    // (Content-Type: text/plain) triggers a CORS PREFLIGHT (OPTIONS). Apps Script
    // answers that OPTIONS with NO allow-origin header, so the browser BLOCKS the
    // real POST before it is sent -> "order never reaches Google Sheet". Node/curl
    // send no preflight, which is why direct tests pass while the site fails.
    //
    // FIX: 3-tier send until one VERIFIES (same order_id is idempotent server-side,
    // so duplicates are impossible):
    //   T1 (readable) application/json — CORS-SIMPLE => NO preflight; Apps Script
    //       returns ACAO:* so we read the JSON body directly.
    //   T2 (readable) text/plain — legacy path (works where preflight passes).
    //   T3 (opaque)   no-cors — no preflight, fire-and-forget, then a VERIFY GET
    //       on GET_ORDERS?phone= proves the row landed.
    // Always returns { ok, status, url, json, verified, mode, error? }.
    function postToSheetOnce_(payload, hook, timeoutMs) {
      var rawBody = JSON.stringify(payload);
      var ms = timeoutMs || 15000;
      var vPhone = '';
      try {
        var oo = (payload && payload.order) || payload;
        var p = String((oo && (oo.phone || oo.Phone)) || '').replace(/\D/g, '');
        if (p.length > 10) p = p.slice(-10);
        vPhone = p;
      } catch (eV) {}
      function readJson(res) {
        return res.text().then(function (txt) {
          var body = null;
          try { body = txt ? JSON.parse(txt) : null; } catch (pe) { body = null; }
          return { ok: res.ok, status: res.status, url: hook, json: body,
                   verified: false, mode: 'readable' };
        }).catch(function () {
          return { ok: res.ok, status: res.status, url: hook, json: null,
                   verified: false, mode: 'readable' };
        });
      }
      function attempt(mode, contentType, remaining) {
        var ctrl = null, timer = null;
        try {
          ctrl = new AbortController();
          timer = setTimeout(function () { try { ctrl.abort(); } catch (e) {} }, remaining);
        } catch (e) {}
        var opts = { method: 'POST', body: rawBody, signal: ctrl ? ctrl.signal : undefined };
        // MUST set mode: 'no-cors' for T3 — without it the browser would do a
        // normal CORS request and the preflight-block would still apply.
        if (mode !== 'no-cors') opts.headers = { 'Content-Type': contentType };
        else opts.mode = 'no-cors';
        return fetch(hook, opts)
          .then(function (res) {
            if (timer) clearTimeout(timer);
            if (mode !== 'no-cors') return readJson(res);
            // Opaque response (no-cors) => body unreadable. CONFIRM via GET: if
            // the server now returns our order (matched by order_id), it landed.
            var gp = vPhone ? ('&phone=' + encodeURIComponent(vPhone)) : '';
            return new Promise(function (r2) { setTimeout(r2, 1200); }).then(function () {
              var gctrl = null, gtimer = null;
              try {
                gctrl = new AbortController();
                gtimer = setTimeout(function () { try { gctrl.abort(); } catch (e) {} }, 8000);
              } catch (eG) {}
              return fetch(hook + '?action=GET_ORDERS' + gp + '&_=' + Date.now(),
                { cache: 'no-store', signal: gctrl ? gctrl.signal : undefined })
                .then(function (g) { if (gtimer) clearTimeout(gtimer); return g.json(); })
                .then(function (gj) {
                  var list = (gj && gj.orders) || [];
                  var target = String((payload && payload.order && payload.order.order_id) ||
                    (payload && payload.order_id) || '');
                  var hit = target && list.some(function (r) {
                    return String((r && (r.order_id || r.id)) || '') === target;
                  });
                  return { ok: !!hit, status: 0, url: hook,
                           json: hit ? { status: 'success', type: 'order_recorded' } : null,
                           verified: !!hit, mode: 'no-cors+verify' };
                })
                .catch(function () {
                  if (gtimer) clearTimeout(gtimer);
                  return { ok: false, status: 0, url: hook, json: null,
                           verified: false, mode: 'no-cors' };
                });
            });
          })
          .catch(function (e) {
            if (timer) clearTimeout(timer);
            var isTimeout = !!(ctrl && ctrl.signal && ctrl.signal.aborted);
            return { ok: false, status: 0, url: hook, json: null, verified: false,
                     mode: mode, error: isTimeout ? 'timeout' : String((e && e.message) || e) };
          });
      }
      function good(r) {
        var b = (r && r.json) || {};
        return !!(r && (r.verified || (r.ok && (b.status === 'success' ||
          b.type === 'order_recorded' || b.type === 'order_updated'))));
      }
      // T1: readable, NO preflight (application/json is CORS-simple).
      return attempt('cors', 'application/json;charset=utf-8', ms).then(function (r1) {
        if (good(r1)) return r1;
        // T2: readable legacy text/plain (no preflight where allowed).
        return attempt('cors', 'text/plain;charset=utf-8', ms).then(function (r2) {
          if (good(r2)) return r2;
          // T3: no preflight at all — fire-and-forget + GET verification.
          return attempt('no-cors', 'application/json', ms).then(function (r3) {
            if (good(r3)) return r3;
            return (r2 && r2.json) ? r2 : (r1 && r1.json ? r1 : r3);
          });
        });
      });
    }
    // Public wrapper: up to 3 attempts with a short backoff. create_order is
    // IDEMPOTENT on the server (same order_id updates in place, never a
    // duplicate row), so a retry after a timeout can never double-charge.
    // `verified` (T3 no-cors GET confirmation) also counts as success.
    function postToSheet(payload) {
      var hook = window.munnaHookBase();
      if (!hook) return Promise.resolve({ ok: false, status: 0, url: '', json: null });
      var attempt = function (n) {
        return postToSheetOnce_(payload, hook, 15000).then(function (r) {
          var body = (r && r.json) || {};
          var good = !!(r && (r.verified || (r.ok && (body.status === 'success' ||
            body.type === 'order_recorded' || body.type === 'order_updated'))));
          if (good || n >= 3) return r;
          return new Promise(function (res) { setTimeout(res, 700 * n); })
            .then(function () { return attempt(n + 1); });
        });
      };
      try {
        return attempt(1);
      } catch (e) {
        return Promise.resolve({ ok: false, status: 0, url: hook, json: null,
                                 error: String(e && e.message || e) });
      }
    }
    // Status change (webapp admin) -> shared Sheet -> bot edits the customer's
    // live Telegram status message.
    function pushOrderStatus(order) {
      if (!order) return;
      postToSheet({ action: 'update_order_status', order_id: order.order_id, status: order.status });
    }
    // Bill edit (webapp admin) -> shared Sheet (bot admin panel reads it).
    function pushOrderBill(order) {
      if (!order) return;
      postToSheet({
        action: 'update_order_bill',
        order_id: order.order_id,
        items: order.items_text || itemsToText(order.items),
        total: order.total
      });
    }
      // Address book -> shared Addresses tab (Telegram Mini App path removed;
      // this is our own HTML shop on munagrocery.in).
      function pushAddressToSheet(addr) {
        if (!addr) return;
        sendToBot({ action: 'SAVE_ADDRESS', customer: addr.name, name: addr.name, phone: addr.phone, address: addr.address }); // no-op (kept for API stability)
        var u = getTgUser();
        if (u && u.id) {
          postToSheet({ action: 'save_address', user_id: String(u.id), name: addr.name, phone: addr.phone, address: addr.address, location: '' });
        }
      }

    // Pull orders from the shared Sheet: Telegram (chat/miniapp/ai) + browser
    // (webapp) orders all show up here. The Sheet wins on conflicts because
    // the bot's Admin Panel advances status there.
