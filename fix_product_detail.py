import codecs

# Read the file as raw bytes to handle escaping properly
with open('js/product-detail.js', 'rb') as f:
    raw = f.read()

content = raw.decode('utf-8')

# Find the lines we need to replace by looking for unique markers
lines = content.split('\n')

# Find the start of renderSimilarProducts_ function
start_idx = None
for i, line in enumerate(lines):
    if 'function renderSimilarProducts_(p)' in line:
        start_idx = i
        break

if start_idx is None:
    print('FAIL: Could not find renderSimilarProducts_ function')
    sys.exit(1)

# Find the end of the map function (the line with "}).join('');")
end_idx = None
for i in range(start_idx, len(lines)):
    if "}).join('');" in lines[i] and i > start_idx:
        end_idx = i
        break

if end_idx is None:
    print('FAIL: Could not find end of map function')
    sys.exit(1)

print(f'Found function at lines {start_idx+1} to {end_idx+1}')

# Build the new function body
new_func = '''    function renderSimilarProducts_(p) {
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
        var fallback = 'icon-512.png';
        box.innerHTML = list.map(function (s) {
          var mrp = parseFloat(s.mrp) || 0, sp = parseFloat(s.selling_price) || 0;
          var off = mrp > sp ? Math.round(((mrp - sp) / mrp) * 100) : 0;
          var sq = parseInt(s.stock_qty, 10); if (isNaN(sq)) sq = 10;
          var cartQty = cart[s.ean] || 0;
          var addCtl = cartQty > 0
            ? '<div class="flex items-center gap-1 mt-1 bg-slate-900 text-white rounded-lg px-1 py-0.5">' +
              '<button onclick="event.stopPropagation(); setQty(\\' + String(s.ean).replace(/\\'/g, \\'') + \\', ' + (cartQty - 1) + ')" class="w-5 h-5 rounded bg-slate-800 flex items-center justify-center"><i class="ph-bold ph-minus text-[9px]"></i></button>' +
              '<span class="text-[9px] font-bold w-4 text-center">' + cartQty + '</span>' +
              '<button onclick="event.stopPropagation(); setQty(\\' + String(s.ean).replace(/\\'/g, \\'') + \\', ' + (cartQty + 1) + ')" class="w-5 h-5 rounded bg-[#1864ab] flex items-center justify-center"><i class="ph-bold ph-plus text-[9px]"></i></button>' +
            '</div>'
            : sq <= 0
              ? '<button disabled class="w-full py-1 mt-1 rounded-lg bg-rose-50 text-rose-400 border border-rose-200 text-[10px] font-bold cursor-not-allowed">Out</button>'
              : '<button onclick="event.stopPropagation(); setQty(\\' + String(s.ean).replace(/\\'/g, \\'') + \\', 1)" class="w-full py-1.5 mt-1 rounded-lg bg-[#1864ab] text-white text-[10px] font-bold flex items-center justify-center gap-1"><i class="ph-bold ph-plus"></i> କାର୍ଟ</button>';
          return '<div class="shrink-0 w-32 bg-white rounded-2xl border border-slate-200 shadow-xs p-2 flex flex-col">' +
            '<div class="relative w-full h-20 rounded-xl bg-slate-50 overflow-hidden cursor-pointer" onclick="openProductDetail(\\' + String(s.ean).replace(/\\'/g, \\'') + \\')">' +
              '<img src="' + (s.image_url || fallback) + '" alt="" class="w-full h-full object-contain" onerror="this.src=\\' + fallback + \\'">' +
              (off > 0 ? '<span class="absolute top-1 left-1 bg-orange-600 text-white text-[9px] font-black px-1 py-0.5 rounded">' + off + '% OFF</span>' : '') +
              (sq <= 0 ? '<span class="absolute bottom-1 left-1 bg-rose-600 text-white text-[9px] font-black px-1 py-0.5 rounded">OUT</span>' : '') +
            '</div>' +
            '<div class="mt-1 text-[10px] font-bold text-slate-800 leading-tight" style="display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:24px;">' + String(s.product_name || \\'\\') + '</div>' +
            '<div class="mt-0.5 text-[9px] font-black text-slate-900">Rs.' + money(s.selling_price) + '</div>' +
            addCtl +
          '</div>';
        }).join('');
      } catch (e) {
        box.innerHTML = '';
      }
    }'''

# Replace lines from start_idx to end_idx (inclusive)
new_lines = lines[:start_idx] + [new_func] + lines[end_idx+1:]

new_content = '\n'.join(new_lines)

with open('js/product-detail.js', 'wb') as f:
    f.write(new_content.encode('utf-8'))

print('SUCCESS: renderSimilarProducts_ function replaced')