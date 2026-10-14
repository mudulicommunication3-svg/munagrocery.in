// =========================================================================
// MUNNA MOBILE & Grocery - Google Apps Script Web App
// Shared database for the Telegram bot (db.py) AND the webapp (index.html).
//
// ONE unified Orders format (13 columns, must match db.ORDERS_HEADERS):
//   Timestamp | Order ID | Customer Name | Phone | Address | Items | Total |
//   Channel | Telegram User ID | Status | Status Msg ID | Location | Payment
//
// Items text format (same as the bot): "Oil x2 @ Rs.340; Salt x1 @ Rs.25"
// Channel: "webapp" (browser), "chat"/"miniapp"/"ai" (Telegram bot)
// Status: pending | accepted | packing | out_for_delivery | delivered | cancelled
//
// doGet  actions: SEARCH_JIOMART | GET_ORDERS | GET_ADDRESSES | GET_USERS | GET_CONFIG | (catalog)
// doPost actions: create_order | update_order_status | update_order_bill |
//                 save_address | SAVE_CONFIG | EDIT_PRODUCT | ADD_PRODUCT |
//                 PUT_SEARCH | (legacy)
// =========================================================================

var ORDER_HEADERS = [
  "Timestamp", "Order ID", "Customer Name", "Phone", "Address", "Items",
  "Total", "Channel", "Telegram User ID", "Status", "Status Msg ID",
  "Location", "Payment"
];
var ADDR_HEADERS = [
  "Telegram User ID", "Name", "Address", "Phone", "Location", "Updated At"
];
var ORDER_STATUSES = [
  "pending", "accepted", "packing", "out_for_delivery", "delivered", "cancelled"
];

function jsonResponse(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function nowStamp_() {
  try {
    return Utilities.formatDate(
      new Date(), Session.getScriptTimeZone() || "Asia/Kolkata", "yyyy-MM-dd HH:mm:ss"
    );
  } catch (e) {
    return new Date().toISOString().replace("T", " ").substring(0, 19);
  }
}

// =========================================================================
// କ. doGet: Sheet ଡାଟା ପଢ଼ିବା, Orders/Addresses sync ଏବଂ JioMart Pincode Search
// =========================================================================
function doGet(e) {
  var params = e ? e.parameter : {};
  // MUST be assigned before the GET_SEARCH bus below — with `var` hoisting a
  // later declaration would hand `undefined` into getSearch_ and crash its
  // insertSheet (TypeError: Cannot read properties of undefined).
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  // ବିକଳ୍ପ A0: Bot-PC search bus (SETTAB: SearchQueue) — ଫୋନ୍ PC ର live
  // JioMart search ବ୍ୟବହାର କରେ (bot ର jiomart_search.fetch_items ପ୍ରମାଣିତ)।
  // POST {"action":"PUT_SEARCH","request_id","query","pincode"} -> row append.
  // GET ?action=GET_SEARCH&request_id=... -> {status, items[]} (done/error/waiting).

  // ବିକଳ୍ପ A0: Bot-PC search bus — GET ?action=GET_SEARCH&request_id=...
  // (webapp ଫୋନ୍ PC ର live JioMart ଉତ୍ତର ପଢ଼େ; PUT_SEARCH ରୁ doPost ଲେଖେ)
  if (params && params.action === "GET_SEARCH") {
    return jsonResponse(getSearch_(ss, params));
  }
  if (params && params.action === "SEARCH_JIOMART") {
    var q = params.query || "";
    var pin = params.pincode || "752106";
    var results = fetchJioMartByPin(q, pin);
    // dbg=1 -> why-empty diagnosis (http code, body head) shown in browser.
    if (params.dbg === "1") {
      var d = diagJioMart_(q, pin);
      return jsonResponse({ status: "success", items: results, diag: d });
    }
    return jsonResponse({ status: "success", items: results });
  }

  // ବିକଳ୍ପ B: Orders tab -> webapp ରେ Telegram ଓ browser ଦୁଇ ପାଖର order ଦେଖାଇବ
  if (params && params.action === "GET_ORDERS") {
    return jsonResponse({ status: "success", orders: readAllOrders_(ss) });
  }

  // ବିକଳ୍ପ C: Addresses tab -> webapp ରେ bot ର ସେଭ୍ କରାଯାଇଥିବା ଠିକଣା ଆସିବ
  if (params && params.action === "GET_ADDRESSES") {
    return jsonResponse({
      status: "success",
      addresses: readAddresses_(ss, params.user_id || "")
    });
  }

  // ବିକଳ୍ପ C1: Users -> admin panel ର "ସମସ୍ତ ଗ୍ରାହକ" ଟ୍ୟାବ୍ (Orders + Addresses ରୁ unique)
  if (params && params.action === "GET_USERS") {
    return jsonResponse({ status: "success", users: readAllUsers_(ss) });
  }

  // ବିକଳ୍ପ C2: Config tab -> ସମସ୍ତ ଗ୍ରାହକଙ୍କ ପାଇଁ shared settings
  // (offer banners, store name, delivery fee, announcements...) — admin ଥରେ
  // set କଲେ ପ୍ରତି ଫୋନ୍ ରେ ଦେଖାଯିବ।
  if (params && params.action === "GET_CONFIG") {
    return jsonResponse({ status: "success", config: readConfig_(ss) });
  }

  // ବିକଳ୍ପ D: Sheet1 ରୁ କାଟାଲଗ୍ ପଢ଼ିବା
  var sheet = ss.getSheetByName("Sheet1");
  if (!sheet) {
    return jsonResponse([]);
  }
  var rows = sheet.getDataRange().getValues();
  var products = [];
  for (var i = 1; i < rows.length; i++) {
    var row = rows[i];
    if (row[0] && row[0].toString().trim() !== "") {
      products.push({
        rowIndex: i + 1,
        ean: String(row[4] || ("ean-" + i)).trim(),
        product_name: String(row[0]).trim(),
        mrp: String(row[1] || row[2] || "0").trim(),
        selling_price: String(row[2] || row[1] || "0").trim(),
        stock_qty: String(row[3] !== "" ? row[3] : "10").trim(),
        image_url: String(row[5] || "").trim(),
        category: String(row[6] || "General").trim()
      });
    }
  }
  return jsonResponse(products);
}

// =========================================================================
// ଖ. doPost: Orders / Addresses / Products ସମ୍ଭାଳିବା (unified format)
// =========================================================================
function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var prodSheet = ss.getSheetByName("Sheet1");

    // ବିକଳ୍ପ A0: Bot-PC search bus — webapp ଫୋନ୍ ଏଠାରେ request ପକାଏ
    if (data.action === "PUT_SEARCH") {
      return jsonResponse(putSearch_(ss, data));
    }

    // ବିକଳ୍ପ 1: ପ୍ରଡକ୍ଟ EDIT / UPDATE
    if (data.action === "EDIT_PRODUCT") {
      var rows = prodSheet.getDataRange().getValues();
      var targetRow = -1;
      for (var i = 1; i < rows.length; i++) {
        if (String(rows[i][4]).trim() === String(data.ean).trim() || (data.rowIndex && (i + 1) === Number(data.rowIndex))) {
          targetRow = i + 1;
          break;
        }
      }
      if (targetRow !== -1) {
        prodSheet.getRange(targetRow, 1).setValue(data.name || "");
        prodSheet.getRange(targetRow, 2).setValue(Number(data.mrp || 0));
        prodSheet.getRange(targetRow, 3).setValue(Number(data.sellingPrice || 0));
        prodSheet.getRange(targetRow, 4).setValue(Number(data.stock || 0));
        prodSheet.getRange(targetRow, 5).setValue(data.ean || "");
        prodSheet.getRange(targetRow, 6).setValue(data.image || "");
        prodSheet.getRange(targetRow, 7).setValue(data.category || "General");
        return jsonResponse({ status: "success", type: "product_updated" });
      }
    }

    // ବିକଳ୍ପ 2: ପ୍ରଡକ୍ଟ ADD
    if (data.action === "ADD_PRODUCT" || (data.mrp !== undefined && data.sellingPrice !== undefined)) {
      if (!prodSheet) prodSheet = ss.insertSheet("Sheet1");
      prodSheet.appendRow([
        data.name || "",
        Number(data.mrp || 0),
        Number(data.sellingPrice || 0),
        Number(data.stock || 10),
        data.ean || ("ean-" + Date.now()),
        data.image || "",
        data.category || "General"
      ]);
      return jsonResponse({ status: "success", type: "product_added" });
    }

    // ବିକଳ୍ପ 3: ନୂଆ order (webapp browser checkout) - ଠିକ୍ ସେହି same 13-column row
    // ଯାହା bot ର db.place_order ଲେଖେ, ତାହା Apps Script ମଧ୍ୟ ଲେଖିବ।
    if (data.action === "create_order") {
      return jsonResponse(writeNewOrder_(ss, data));
    }

    // ବିକଳ୍ପ 4: webapp admin panel ରୁ status ବଦଳ (bot ଏହା ଦେଖି customer msg edit କରିବ)
    if (data.action === "update_order_status") {
      var st = String(data.status || "").trim();
      if (ORDER_STATUSES.indexOf(st) < 0) {
        return jsonResponse({ status: "error", error: "Unknown status: " + st });
      }
      var shS = ensureSheet_(ss, "Orders", ORDER_HEADERS);
      var rowS = findOrderRow_(shS, data.order_id);
      if (rowS < 1) {
        return jsonResponse({ status: "error", error: "Order not found" });
      }
      shS.getRange(rowS, 10).setValue(st);
      return jsonResponse({ status: "success", type: "status_updated", order_id: String(data.order_id), status: st });
    }

    // ବିକଳ୍ପ 5: webapp admin panel ରୁ bill edit (items + total)
    if (data.action === "update_order_bill") {
      var shB = ensureSheet_(ss, "Orders", ORDER_HEADERS);
      var rowB = findOrderRow_(shB, data.order_id);
      if (rowB < 1) {
        return jsonResponse({ status: "error", error: "Order not found" });
      }
      shB.getRange(rowB, 6).setValue(String(data.items || ""));
      shB.getRange(rowB, 7).setValue(Number(data.total || 0) || 0);
      return jsonResponse({ status: "success", type: "bill_updated", order_id: String(data.order_id) });
    }

    // ବିକଳ୍ପ 6: address book sync (webapp -> same Addresses tab as the bot)
    if (data.action === "save_address") {
      var uid = String(data.user_id || data.telegram_user_id || "").trim();
      if (!uid) {
        return jsonResponse({ status: "error", error: "user_id required" });
      }
      upsertAddress_(ss, uid, data.name, data.address, data.phone, data.location);
      return jsonResponse({ status: "success", type: "address_saved", user_id: uid });
    }

    // ବିକଳ୍ପ 7 (legacy): flat keys ଥିବା ପୁରୁଣା payload - unified row ରେ ବଦଳାଇ ଲେଖାଯିବ
    var looksLikeOrder = (data.order_id || data.orderId || data.id) &&
      (data.items !== undefined || data.customer !== undefined || data.name !== undefined);
    if (looksLikeOrder) {
      return jsonResponse(writeNewOrder_(ss, data));
    }

    // Shared config (Config tab): admin sets ONCE -> every customer sees it
    // (offer banners, store name, delivery fee, notices...).
    if (data.action === "SAVE_CONFIG") {
      return jsonResponse(saveConfig_(ss, data.key || "", data.value || ""));
    }

    return jsonResponse({ status: "error", error: "Nothing matched" });
  } catch (err) {
    return jsonResponse({ status: "error", error: err.toString() });
  }
}

// =========================================================================
// Shared order/address helpers (unified format used by bot + webapp)
// =========================================================================

// Get or create a tab and make sure its header row matches `headers`
// (extends missing trailing columns; never shrinks data).
function ensureSheet_(ss, name, headers) {
  var sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
  }
  if (sh.getMaxColumns() < headers.length) {
    sh.insertColumnsAfter(sh.getMaxColumns(), headers.length - sh.getMaxColumns());
  }
  var needHeader = sh.getLastRow() < 1;
  if (!needHeader) {
    var cur = sh.getRange(1, 1, 1, headers.length).getValues()[0];
    for (var i = 0; i < headers.length; i++) {
      var val = cur[i] === undefined ? "" : String(cur[i]).trim();
      if (val !== headers[i]) { needHeader = true; break; }
    }
  }
  if (needHeader) {
    sh.getRange(1, 1, 1, headers.length).setValues([headers]);
  }
  return sh;
}

// One sheet row -> unified order object (pads older 9/12 column rows).
function rowToOrder_(row) {
  var r = (row || []).slice();
  while (r.length < ORDER_HEADERS.length) r.push("");
  return {
    timestamp: String(r[0] || ""),
    order_id: String(r[1] || ""),
    customer_name: String(r[2] || ""),
    phone: String(r[3] || ""),
    address: String(r[4] || ""),
    items: String(r[5] || ""),
    total: Number(r[6] || 0) || 0,
    channel: String(r[7] || "chat") || "chat",
    telegram_user_id: String(r[8] || ""),
    status: String(r[9] || "").trim() || "pending",
    status_msg_id: String(r[10] || ""),
    location: String(r[11] || ""),
    payment: String(r[12] || "").trim() || "COD"
  };
}

// Find an order row by Order ID (column B); returns 1-based row or -1.
function findOrderRow_(sh, orderId) {
  var target = String(orderId || "").trim();
  if (!target || sh.getLastRow() < 2) return -1;
  var ids = sh.getRange(2, 2, sh.getLastRow() - 1, 1).getValues();
  for (var i = 0; i < ids.length; i++) {
    if (String(ids[i][0]).trim() === target) return i + 2;
  }
  return -1;
}

// Normalise ANY incoming order payload (new or legacy keys) to unified form.
function normalizeOrder_(data) {
  var o = (data && data.order && typeof data.order === "object") ? data.order : (data || {});
  var orderId = String(o.order_id || o.orderId || o.id || "").trim();
  if (!orderId) {
    orderId = "MUN-" + Date.now().toString(12).toUpperCase();
  }
  var ts = String(o.timestamp || o.date || "");
  if (ts.indexOf("T") >= 0) ts = ts.replace("T", " ").substring(0, 19);
  if (!/^\d{4}-\d{2}-\d{2}/.test(ts)) ts = nowStamp_();

  // Items: unified text, or a structured array (legacy webapp / bot lines).
  var itemsText = "";
  if (typeof o.items === "string") itemsText = o.items;
  else if (typeof o.items_text === "string") itemsText = o.items_text;
  var lines = Array.isArray(o.lines) ? o.lines : (Array.isArray(o.items) ? o.items : []);
  if (!itemsText && lines.length) {
    if (lines[0] && lines[0].name !== undefined) {
      itemsText = lines.map(function (l) {
        return l.name + " x" + (Number(l.qty) || 1) + " @ Rs." + (Number(l.price) || 0);
      }).join("; ");
    } else {
      try { itemsText = JSON.stringify(lines); } catch (e) { itemsText = ""; }
    }
  }

  // Status: unified status, else derive from the legacy 1..5 stage.
  var status = String(o.status || "").trim();
  if (ORDER_STATUSES.indexOf(status) < 0) {
    var stage = Number(o.stage || 0);
    status = (stage >= 1 && stage <= 5) ? ORDER_STATUSES[stage - 1] : "pending";
  }

  var location = typeof o.location === "string" ? o.location : "";
  if (!location && o.lat !== undefined && o.lat !== "" && o.lng !== undefined && o.lng !== "") {
    location = o.lat + "," + o.lng;
  }

  return {
    timestamp: ts,
    order_id: orderId,
    customer_name: String(o.customer_name || o.name || o.customer || ""),
    phone: String(o.phone || ""),
    address: String(o.address || ""),
    items: itemsText,
    total: Number(String(o.total || 0).replace(/,/g, "")) || 0,
    channel: String(o.channel || "webapp").trim() || "webapp",
    telegram_user_id: String(o.telegram_user_id || ""),
    status: status,
    status_msg_id: String(o.status_msg_id || ""),
    location: location,
    payment: String(o.payment || "COD").trim() || "COD",
    lines: lines
  };
}

function orderRow_(o) {
  return [
    o.timestamp, o.order_id, o.customer_name, o.phone, o.address, o.items,
    o.total, o.channel, o.telegram_user_id, o.status, o.status_msg_id,
    o.location, o.payment
  ];
}

// Write one unified order row (dedupe by order id + deduct stock once).
function writeNewOrder_(ss, payload) {
  var o = normalizeOrder_(payload);
  var ordersSh = ensureSheet_(ss, "Orders", ORDER_HEADERS);
  var existing = findOrderRow_(ordersSh, o.order_id);
  if (existing > 0) {
    // Same order sent twice -> update in place, never duplicate the row.
    ordersSh.getRange(existing, 1, 1, ORDER_HEADERS.length).setValues([orderRow_(o)]);
    return { status: "success", type: "order_updated", order_id: o.order_id };
  }
  var eanLines = (o.lines || []).filter(function (l) { return l && l.ean; });
  if (eanLines.length) deductStock_(ss.getSheetByName("Sheet1"), eanLines);
  ordersSh.appendRow(orderRow_(o));
  // Register EVERY customer in Addresses so the admin "All Users" tab is
  // complete (Telegram id inside the bot, else the phone number as key).
  if (o.customer_name && o.address) {
    var uid2 = String(o.telegram_user_id || o.phone || "").trim();
    if (uid2) upsertAddress_(ss, uid2, o.customer_name, o.address, o.phone, o.location);
  }
  return { status: "success", type: "order_recorded", order_id: o.order_id };
}

// Deduct stock from Sheet1 by EAN (same rule as db.deduct_stock_by_ean).
function deductStock_(prodSheet, lines) {
  if (!prodSheet || !lines || !lines.length) return;
  var lastRow = prodSheet.getLastRow();
  if (lastRow < 2) return;
  var cols = Math.min(7, prodSheet.getMaxColumns());
  var rows = prodSheet.getRange(2, 1, lastRow - 1, cols).getValues();
  var index = {};
  for (var i = 0; i < rows.length; i++) {
    var ean = String(rows[i][4] || "").trim();
    if (ean && index[ean] === undefined) index[ean] = i;
  }
  for (var j = 0; j < lines.length; j++) {
    var ean2 = String(lines[j].ean || "").trim();
    var qty = Number(lines[j].qty || 0);
    var at = index[ean2];
    if (at === undefined || !qty) continue;
    var cur = Number(rows[at][3] || 0);
    prodSheet.getRange(at + 2, 4).setValue(Math.max(0, cur - qty));
  }
}

// One DEFAULT address per user (same Addresses tab the bot writes).
function upsertAddress_(ss, uid, name, address, phone, location) {
  var sh = ensureSheet_(ss, "Addresses", ADDR_HEADERS);
  var updated = nowStamp_();
  var lastRow = sh.getLastRow();
  if (lastRow >= 2) {
    var ids = sh.getRange(2, 1, lastRow - 1, 1).getValues();
    for (var i = 0; i < ids.length; i++) {
      if (String(ids[i][0]).trim() === String(uid)) {
        sh.getRange(i + 2, 1, 1, ADDR_HEADERS.length).setValues(
          [[String(uid), name || "", address || "", phone || "", location || "", updated]]
        );
        return true;
      }
    }
  }
  sh.appendRow([String(uid), name || "", address || "", phone || "", location || "", updated]);
  return true;
}

// All orders, newest first (webapp shows Telegram + browser orders together).
function readAllOrders_(ss) {
  var sh = ss.getSheetByName("Orders");
  var out = [];
  if (!sh || sh.getLastRow() < 2) return out;
  var rows = sh.getRange(
    2, 1, sh.getLastRow() - 1, Math.min(ORDER_HEADERS.length, sh.getMaxColumns())
  ).getValues();
  for (var i = rows.length - 1; i >= 0; i--) {
    if (!String(rows[i][1] || "").trim()) continue;
    out.push(rowToOrder_(rows[i]));
  }
  return out;
}

// Saved addresses (optionally only one user's).
function readAddresses_(ss, userId) {
  var sh = ss.getSheetByName("Addresses");
  var out = [];
  if (!sh || sh.getLastRow() < 2) return out;
  var rows = sh.getRange(
    2, 1, sh.getLastRow() - 1, Math.min(ADDR_HEADERS.length, sh.getMaxColumns())
  ).getValues();
  var want = String(userId || "").trim();
  for (var i = 0; i < rows.length; i++) {
    var uid = String(rows[i][0] || "").trim();
    if (!uid || (want && uid !== want)) continue;
    out.push({
      user_id: uid,
      name: String(rows[i][1] || ""),
      address: String(rows[i][2] || ""),
      phone: String(rows[i][3] || ""),
      location: String(rows[i][4] || ""),
      updated_at: String(rows[i][5] || "")
    });
  }
  return out;
}

// All unique customers for the admin panel "Users" tab (new HTML format):
// merged from Orders + Addresses, newest activity first.
//   { user_id, tg_id, name, phone, address, orders, spent, last }
function readAllUsers_(ss) {
  var map = {};
  function put(key, base) {
    key = String(key || "").trim();
    if (!key) return null;
    if (!map[key]) {
      map[key] = { user_id: "", tg_id: "", name: "", phone: "", address: "", orders: 0, spent: 0, last: "" };
    }
    var u = map[key];
    if (base) {
      if (!u.name && base.name) u.name = base.name;
      if (!u.phone && base.phone) u.phone = base.phone;
      if (!u.address && base.address) u.address = base.address;
      if (base.tg_id && !u.tg_id) u.tg_id = String(base.tg_id);
      if (base.user_id && !u.user_id) u.user_id = String(base.user_id);
    }
    return u;
  }

  readAllOrders_(ss).forEach(function (o) {
    var key = String(o.phone || "").trim() || String(o.telegram_user_id || "").trim();
    var u = put(key, {
      name: o.customer_name, phone: o.phone, address: o.address,
      tg_id: o.telegram_user_id, user_id: o.telegram_user_id
    });
    if (!u) return;
    u.orders += 1;
    u.spent += (Number(o.total) || 0);
    if (String(o.timestamp) > u.last) {
      u.last = String(o.timestamp);
      if (o.address) u.address = o.address;
      if (o.customer_name) u.name = o.customer_name;
    }
  });

  readAddresses_(ss, "").forEach(function (a) {
    var key = String(a.phone || "").trim() || String(a.user_id || "").trim();
    var u = put(key, {
      name: a.name, phone: a.phone, address: a.address,
      tg_id: a.user_id, user_id: a.user_id
    });
    if (u && a.updated_at && String(a.updated_at) > u.last) u.last = String(a.updated_at);
  });

  var out = [];
  Object.keys(map).forEach(function (k) {
    var u = map[k];
    u.user_id = u.user_id || u.tg_id || k;
    out.push(u);
  });
  out.sort(function (a, b) { return String(b.last).localeCompare(String(a.last)); });
  return out;
}

// =========================================================================
// =========================================================================
// A0. Bot-PC search bus (SearchQueue tab) — phone asks, bot PC answers.
//   doPost PUT_SEARCH  {"action":"PUT_SEARCH","request_id","query","pincode"}
//   doGet  GET_SEARCH  ?action=GET_SEARCH&request_id=...
// -> {status:"done"|"error"|"waiting", items:[{title,url,mrp,sp,cat}], error}
// The webapp polls GET_SEARCH; the bot's search_bus.py poll loop writes done.
// =========================================================================
var SEARCH_BUS_TAB = "SearchQueue";
var SEARCH_BUS_HEADERS = [
  "Request ID", "Query", "Pincode", "Status",
  "Items JSON", "Error", "Created At", "Updated At"
];

function searchBusSheet_(ss) {
  var sh = null;
  try { sh = ss.getSheetByName(SEARCH_BUS_TAB); } catch (e) {}
  if (!sh) {
    sh = ss.insertSheet(SEARCH_BUS_TAB);
    sh.getRange(1, 1, 1, SEARCH_BUS_HEADERS.length).setValues([SEARCH_BUS_HEADERS]);
  }
  return sh;
}

function putSearch_(ss, data) {
  var rid = String(data.request_id || "").trim();
  var q = String(data.query || "").trim();
  var pin = String(data.pincode || "752106").trim();
  if (!rid || !q) return { status: "error", message: "request_id + query required" };
  var sh = searchBusSheet_(ss);
  sh.appendRow([rid, q, pin, "pending", "", "", nowStamp_(), nowStamp_()]);
  // Cap the tab: keep the newest 15 requests only (Sheet quota safety).
  try {
    var n = sh.getLastRow();
    if (n > 16) sh.deleteRows(2, n - 16);
  } catch (e) {}
  return { status: "queued", request_id: rid };
}

function getSearch_(ss, params) {
  var rid = String((params && params.request_id) || "").trim();
  if (!rid) return { status: "error", message: "request_id required", items: [] };
  var sh = searchBusSheet_(ss);
  var rows = [];
  try { rows = sh.getDataRange().getValues(); } catch (e) { return { status: "waiting", items: [] }; }
  for (var i = rows.length - 1; i >= 1; i--) {
    if (String(rows[i][0] || "").trim() === rid) {
      var status = String(rows[i][3] || "").trim();
      if (status === "done") {
        var items = [];
        try { items = JSON.parse(rows[i][4] || "[]") || []; } catch (e) {}
        return { status: "done", items: items };
      }
      if (status === "error") {
        return { status: "error", items: [], error: String(rows[i][5] || "") };
      }
      return { status: "waiting", items: [] };
    }
  }
  return { status: "waiting", items: [] };
}

// ଗ. JioMart API Fetcher (jm1.8.0_3.py Headers ଓ Parsing Pattern)
// =========================================================================
function fetchJioMartByPin(query, pin) {
  var results = [];
  var cleanQ = (query || "").trim();
  if (!cleanQ) return results;

  var targetPin = String(pin || "752106").trim();

  try {
    var url = "https://www.jiomart.com/api/service/application/catalog/v1.0/products?q=" 
              + encodeURIComponent(cleanQ) + "&page_size=12&page_no=1";

    var locDetail = JSON.stringify({
      "country": "INDIA",
      "country_iso_code": "IN",
      "pincode": targetPin
    });

// =========================================================================

    var headers = {
      "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Mobile Safari/537.36",
      "Accept": "application/json, text/plain, */*",
      "Origin": "https://www.jiomart.com",
      "Referer": "https://www.jiomart.com/",
      "pincode": targetPin,
      "x-pincode": targetPin,
      "x-location-detail": locDetail,
      "visitor-id": Utilities.getUuid()
    };

    var res = UrlFetchApp.fetch(url, { method: "GET", headers: headers, muteHttpExceptions: true });

    if (res.getResponseCode() === 200) {
      var json = JSON.parse(res.getContentText());
      var body = (json && typeof json.data === "object") ? json.data : {};
      var items = json.items || body.items || [];

      items.forEach(function(item) {
        if (!item || typeof item !== "object") return;

        // Medias extract
        var imgUrl = "";
        var medias = item.medias || [];
        if (Array.isArray(medias) && medias.length > 0) {
          for (var m = 0; m < medias.length; m++) {
            var media = medias[m];
            if (media && media.url && (!media.type || String(media.type).toLowerCase() === "image")) {
              imgUrl = media.url;
              break;
            }
          }
        }

        // Price extract
        var mrp = 0;
        var sp = 0;
        var priceObj = item.price || {};
        if (priceObj.marked) {
          mrp = (typeof priceObj.marked === "object") ? (priceObj.marked.min || priceObj.marked.max || 0) : priceObj.marked;
        }
        if (priceObj.effective) {
          sp = (typeof priceObj.effective === "object") ? (priceObj.effective.min || priceObj.effective.max || 0) : priceObj.effective;
        }

        // Category extract
        var cat = "Grocery";
        if (item.hierarchy && item.hierarchy.l3_category) {
          cat = item.hierarchy.l3_category.name;
        } else if (item.hierarchy && item.hierarchy.l2_category) {
          cat = item.hierarchy.l2_category.name;
        }

        if (imgUrl) {
          results.push({
            title: item.name || "Product",
            url: imgUrl,
            mrp: mrp ? String(mrp) : "",
            sp: sp ? String(sp) : "",
            cat: cat,
            sellable: item.sellable !== undefined ? item.sellable : true,
            source: "JioMart"
          });
        }
      });
    }
  } catch(e) {
    Logger.log("JioMart Fetch Error: " + e.toString());
  }

  return results;
}

// Debug helper: raw HTTP status + body head from JioMart (no parsing).
// Call: ?action=SEARCH_JIOMART&query=oil&pincode=752106&dbg=1
function diagJioMart_(query, pin) {
  var out = { url: "", code: -1, len: 0, head: "", err: "" };
  try {
    var targetPin = String(pin || "752106").trim();
    out.url = "https://www.jiomart.com/api/service/application/catalog/v1.0/products?q="
      + encodeURIComponent(String(query || "").trim()) + "&page_size=3&page_no=1";
    var res = UrlFetchApp.fetch(out.url, {
      method: "GET",
      muteHttpExceptions: true,
      followRedirects: true,
      headers: {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Mobile Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.jiomart.com",
        "Referer": "https://www.jiomart.com/",
        "pincode": targetPin,
        "x-pincode": targetPin,
        "x-location-detail": JSON.stringify({ country: "INDIA", country_iso_code: "IN", pincode: targetPin }),
        "visitor-id": Utilities.getUuid()
      }
    });
    out.code = res.getResponseCode();
    var txt = res.getContentText() || "";
    out.len = txt.length;
    out.head = txt.substring(0, 300);
  } catch (e) {
    out.err = e.toString();
  }
  return out;
}

// ଘ. Shared Config (Config tab: Key | Value) — admin ଥରେ set କଲେ ସମସ୍ତ
// ଗ୍ରାହକଙ୍କ ଫୋନ୍ ରେ ଦେଖାଯିବ (offer banners, store name, delivery fee...)
// =========================================================================
var CONFIG_SHEET = "Config";
var CONFIG_HEADERS = ["Key", "Value"];

function readConfig_(ss) {
  var sh = ensureSheet_(ss, CONFIG_SHEET, CONFIG_HEADERS);
  var rows = [];
  try {
    rows = sh.getDataRange().getValues();
  } catch (e) {
    return {};
  }
  var out = {};
  for (var i = 1; i < rows.length; i++) {
    var k = String(rows[i][0] || "").trim();
    if (!k) continue;
    var v = String(rows[i][1] === undefined || rows[i][1] === null ? "" : rows[i][1]);
    // FIRST row of a key wins: a duplicate key row (historic race, e.g. an
    // empty "trending") must NEVER wipe the real value — saveConfig_ keeps
    // row 1 updated and deletes any duplicate rows it finds.
    if (!(k in out)) out[k] = v;
  }
  return out;
}

function saveConfig_(ss, key, value) {
  key = String(key || "").trim();
  if (!key) {
    return { status: "error", message: "config key required" };
  }
  var sh = ensureSheet_(ss, CONFIG_SHEET, CONFIG_HEADERS);
  var rows = sh.getDataRange().getValues();
  var matches = []; // 1-based sheet rows that hold this key
  for (var i = 1; i < rows.length; i++) {
    if (String(rows[i][0] || "").trim() === key) matches.push(i + 1);
  }
  var val = String(value === undefined || value === null ? "" : value);
  if (matches.length) {
    sh.getRange(matches[0], 1, 1, 2).setValues([[key, val]]);
    // Self-heal: remove duplicate rows of the same key (bottom-up), so a
    // past race (two concurrent saves both appending) can never poison
    // readConfig_ again.
    for (var d = matches.length - 1; d >= 1; d--) {
      sh.deleteRow(matches[d]);
    }
    return { status: "success", key: key };
  }
  sh.appendRow([key, val]);
  return { status: "success", key: key };
}
