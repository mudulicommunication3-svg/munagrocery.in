import json
import logging
import urllib.parse
import urllib.request
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Mobile Safari/537.36"

class JioMartHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed_url.query)
        query = params.get("q", [""])[0]
        pincode = params.get("pincode", ["752106"])[0]

        if not query:
            self._send_json({"status": "error", "message": "Query missing"})
            return

        results = self.fetch_jiomart(query, pincode)
        self._send_json({"status": "success", "items": results})

    def _send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def fetch_jiomart(self, query, pincode):
        url = f"https://www.jiomart.com/api/service/application/catalog/v1.0/products?q={urllib.parse.quote(query)}&page_size=12&page_no=1"
        loc_detail = json.dumps({"country": "INDIA", "country_iso_code": "IN", "pincode": str(pincode)})
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.jiomart.com",
            "Referer": "https://www.jiomart.com/",
            "pincode": str(pincode),
            "x-pincode": str(pincode),
            "x-location-detail": loc_detail,
            "visitor-id": str(uuid.uuid4())
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.error(f"Fetch failed: {e}")
            return []

        items = data.get("items") or (data.get("data") or {}).get("items") or []
        out = []
        for itm in items:
            img = ""
            for m in (itm.get("medias") or []):
                if m.get("url"):
                    img = m["url"]
                    break
            
            p_obj = itm.get("price") or {}
            mrp = (p_obj.get("marked") or {}).get("min") if isinstance(p_obj.get("marked"), dict) else (p_obj.get("marked") or 0)
            sp = (p_obj.get("effective") or {}).get("min") if isinstance(p_obj.get("effective"), dict) else (p_obj.get("effective") or 0)
            
            hier = itm.get("hierarchy") or {}
            cat = (hier.get("l3_category") or {}).get("name") or (hier.get("l2_category") or {}).get("name") or "Grocery"

            if img:
                out.append({
                    "title": itm.get("name") or "Product",
                    "url": img,
                    "mrp": str(mrp or ""),
                    "sp": str(sp or ""),
                    "cat": cat,
                    "source": "JioMart"
                })
        return out

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8085), JioMartHandler)
    print("🚀 JioMart API Search Server Running on port 8085...")
    server.serve_forever()