"""
BMS Monitor v13 — Cloud Edition
================================
Changes from v12:
  • headless=True (no display needed on cloud)
  • No persistent bms_profile/ — uses stealth launch context instead
  • process_status.json tracks uptime, last_check, next_check for /status
  • Startup/shutdown TG messages include uptime stats
  • Graceful SIGTERM handling (cloud stop signal)
  • LOG_FILE env var support (defaults to monitor.log)
  • Reduced wait times for faster cycles on cloud
"""

import sys, os, time, json, re, base64, requests, traceback, logging, argparse, signal
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright

# ══════════════════ CONFIG ══════════════════
TOKEN          = os.getenv("TG_TOKEN",   "8666314563:AAFXDLrKjlkWz41rLo9BLdkutJj4h1Y8JKA")
CHAT_IDS_RAW   = os.getenv("TG_CHAT_IDS","924367933,1707720927")
CHAT_IDS       = [int(x.strip()) for x in CHAT_IDS_RAW.split(",") if x.strip()]
EVENT_FILE     = os.getenv("EVENT_FILE", "events.json")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "180"))
PAGE_TIMEOUT   = 60_000
MAX_WORKERS    = 1
STATUS_FILE    = "process_status.json"
LOG_FILE       = os.getenv("LOG_FILE", "monitor.log")
# ════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)
state_cache  = {}
_start_time  = time.time()
_running     = True   # flipped by SIGTERM

# ── Status file helpers ───────────────────────────────────────────────────────

def _write_status(extra=None):
    d = {
        "pid":        os.getpid(),
        "started_at": datetime.fromtimestamp(_start_time).strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_s":   int(time.time() - _start_time),
        "last_check": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "next_check_in": CHECK_INTERVAL,
        "events_tracked": len(load_events()),
    }
    if extra:
        d.update(extra)
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(d, f, indent=2)
    except Exception as e:
        log.warning(f"status write: {e}")

def read_status():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except:
        return {}

# ── SIGTERM handler (cloud stop) ──────────────────────────────────────────────

def _on_sigterm(sig, frame):
    global _running
    log.info("SIGTERM received — shutting down")
    _running = False

signal.signal(signal.SIGTERM, _on_sigterm)
try:
    signal.signal(signal.SIGHUP, _on_sigterm)
except AttributeError:
    pass  # Windows

# ── pycryptodome ──────────────────────────────────────────────────────────────
_CRYPTO_OK = False
try:
    from Crypto.Cipher import AES as _AES
    from Crypto.Util.Padding import unpad as _unpad
    import hashlib as _hl
    _CRYPTO_OK = True
except ImportError:
    try:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "pycryptodome",
                        "--break-system-packages", "-q"], capture_output=True, timeout=60)
        from Crypto.Cipher import AES as _AES
        from Crypto.Util.Padding import unpad as _unpad
        import hashlib as _hl
        _CRYPTO_OK = True
    except Exception as _e:
        log.warning(f"pycryptodome unavailable ({_e}) — quantities disabled")


# ══════════════════ JS HOOKS ══════════════════

STEALTH = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
"""

XHR_HOOK = r"""
(function(){
    window.__bmsDecrypted=[];
    window.__bmsEncBlob='';
    window.__bmsKeyFound=[];
    window.__bmsCryptoHooked=false;
    let _cjs;

    function wrapAES(lib){
        if(!lib||!lib.AES||!lib.AES.decrypt||lib.__bmsW) return false;
        const orig=lib.AES.decrypt.bind(lib.AES);
        lib.AES.decrypt=function(ct,key,cfg){
            try{
                const k=(typeof key==='string')?key:JSON.stringify(key);
                if(k&&k.length>2&&k.length<200) window.__bmsKeyFound.push(k);
            }catch(e){}
            const r=orig(ct,key,cfg);
            try{
                const t=r.toString(lib.enc.Utf8);
                if(t&&t.length>20) window.__bmsDecrypted.push(t);
            }catch(e){}
            return r;
        };
        lib.__bmsW=true;
        window.__bmsCryptoHooked=true;
        return true;
    }

    try{
        Object.defineProperty(window,'CryptoJS',{
            configurable:true,enumerable:true,
            get:function(){return _cjs;},
            set:function(v){_cjs=v;wrapAES(v);}
        });
    }catch(e){}

    let p=0;
    const iv=setInterval(function(){
        p++;
        if(wrapAES(_cjs||window.CryptoJS||window.cryptoJs)){clearInterval(iv);return;}
        if(p>200) clearInterval(iv);
    },100);

    const _fetch=window.fetch;
    window.fetch=function(input,init){
        const url=(typeof input==='string'?input:(input&&input.url)||'');
        return _fetch.apply(this,arguments).then(function(res){
            if(url.toLowerCase().includes('showinfo')){
                res.clone().json().then(function(d){
                    if(d&&typeof d.data==='string'&&d.data.length>100)
                        window.__bmsEncBlob=d.data;
                }).catch(function(){});
            }
            return res;
        });
    };

    const _open=XMLHttpRequest.prototype.open;
    const _send=XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open=function(m,url){
        this.__bmsU=url||'';return _open.apply(this,arguments);
    };
    XMLHttpRequest.prototype.send=function(){
        if((this.__bmsU||'').toLowerCase().includes('showinfo')){
            this.addEventListener('load',function(){
                try{
                    const d=JSON.parse(this.responseText);
                    if(d&&typeof d.data==='string'&&d.data.length>100)
                        window.__bmsEncBlob=d.data;
                }catch(e){}
            });
        }
        return _send.apply(this,arguments);
    };
})();
"""

KEY_SCAN_JS = """
async () => {
    const found=[];
    const scripts=Array.from(document.querySelectorAll('script[src]'))
        .map(s=>s.src).filter(s=>s&&(s.includes('/static/js')||s.includes('bms')));
    for(const url of scripts.slice(0,15)){
        try{
            const r=await fetch(url);
            const text=await r.text();
            const pats=[
                /\\.decrypt\\s*\\(\\s*\\w[\\w.]*\\s*,\\s*["']([^"']{4,60})["']/g,
                /AES\\.decrypt\\s*\\(\\s*[^,]+,\\s*["']([^"']{4,60})["']/g,
            ];
            for(const p of pats){
                let m;
                while((m=p.exec(text))!==null){
                    const k=m[1];
                    if(k&&!k.includes('(')&&!k.includes('=>')&&k.length>3)
                        found.push(k);
                }
            }
        }catch(e){}
    }
    return [...new Set(found)];
}
"""


# ══════════════════ AES DECRYPTION ══════════════════

_KEY_CACHE = []

_KNOWN_KEYS = [
    "in.bookmyshow", "bookmyshow", "BookMyShow", "b0okmy5h0w",
    "in.bms.android", "bmsandroid", "BMS2023", "BMS2024", "BMS2025",
    "bms@2023", "bms@2024", "bms@2025",
    "ShowInfo", "showinfo", "seatLayout", "seatlayout",
]

def _try_decrypt(b64_blob, extra_keys=None):
    if not _CRYPTO_OK or not b64_blob:
        return None, None
    try:
        blob = base64.b64decode(b64_blob + "==")
    except Exception:
        return None, None
    if len(blob) < 32:
        return None, None

    iv         = blob[:16]
    ciphertext = blob[16:]
    if len(ciphertext) % 16 != 0:
        ciphertext = blob; iv = b'\x00' * 16

    all_keys = list(extra_keys or []) + _KEY_CACHE + _KNOWN_KEYS
    for raw in all_keys:
        if not raw: continue
        rb = raw.encode() if isinstance(raw, str) else raw
        for kb in [rb,
                   _hl.md5(rb).digest(),
                   _hl.sha256(rb).digest()[:16],
                   _hl.sha256(rb).digest()]:
            for kl in (16, 32, 24):
                key = (kb * (kl // len(kb) + 1))[:kl]
                try:
                    dec = _unpad(_AES.new(key, _AES.MODE_CBC, iv).decrypt(ciphertext), 16)
                    txt = dec.decode("utf-8")
                    if txt.startswith(("{", "[")):
                        if raw not in _KEY_CACHE:
                            _KEY_CACHE.insert(0, raw)
                            log.info(f"  [AES] ✅ Key cached: '{raw}'")
                        return txt, raw
                except: pass
    return None, None


# ══════════════════ PARSE showinfo ══════════════════

def _parse_showinfo(text):
    stands = []
    seen   = set()

    def add(name, price_raw, avail_raw, sold=False):
        name = str(name or "").strip()
        if not name: return
        try:   pf = float(str(price_raw).replace("₹","").replace(",",""))
        except: pf = 0
        price = f"₹{int(pf)}" if pf else "—"
        try:   av = int(avail_raw or 0)
        except: av = 0
        key = f"{name}|{price}"
        if key in seen: return
        seen.add(key)
        stands.append({"name": name, "price": price, "available": av,
                       "sold_out": sold or (av == 0 and pf > 0), "pf": pf})

    try:
        data = json.loads(text)
    except:
        return stands

    def try_parse(obj):
        if not isinstance(obj, dict): return False
        for sdk in ("ShowDetails","showDetails"):
            if sdk in obj:
                for show in (obj[sdk] if isinstance(obj[sdk], list) else [obj[sdk]]):
                    for ck in ("CategoryList","categoryList","categories"):
                        for cat in show.get(ck, []):
                            n  = cat.get("CategoryName") or cat.get("categoryName") or cat.get("name") or ""
                            p  = cat.get("Price") or cat.get("price") or cat.get("amount") or 0
                            a  = (cat.get("AvailableSeats") or cat.get("availableSeats") or
                                  cat.get("available") or cat.get("remaining") or
                                  cat.get("TotalAvailableSeats") or 0)
                            st = str(cat.get("Status") or cat.get("status") or "").lower()
                            add(n, p, a, "sold" in st or "houseful" in st)
                if stands: return True
        for ck in ("CategoryList","categoryList","categories","seatCategories"):
            if ck in obj and isinstance(obj[ck], list):
                for cat in obj[ck]:
                    n  = cat.get("CategoryName") or cat.get("categoryName") or cat.get("name") or ""
                    p  = cat.get("Price") or cat.get("price") or cat.get("amount") or 0
                    a  = (cat.get("AvailableSeats") or cat.get("availableSeats") or
                          cat.get("available") or cat.get("remaining") or
                          cat.get("TotalAvailableSeats") or 0)
                    sold = bool(cat.get("soldOut") or cat.get("SoldOut") or
                                "sold" in str(cat.get("status","")).lower())
                    add(n, p, a, sold)
                if stands: return True
        return False

    if not try_parse(data):
        for v in (data.values() if isinstance(data, dict) else []):
            if isinstance(v, dict) and try_parse(v): break

    if not stands:
        def walk(obj, depth=0):
            if depth > 10 or not isinstance(obj, (dict, list)): return
            if isinstance(obj, list):
                for i in obj: walk(i, depth+1)
            elif isinstance(obj, dict):
                kl = {k.lower(): k for k in obj}
                if any(k in kl for k in ("price","amount")) and any(k in kl for k in ("name","categoryname")):
                    nk = kl.get("name") or kl.get("categoryname")
                    pk = kl.get("price") or kl.get("amount")
                    ak = kl.get("availableseats") or kl.get("available") or kl.get("remaining")
                    sk = kl.get("status")
                    add(obj.get(nk,""), obj.get(pk,0),
                        obj.get(ak,0) if ak else 0,
                        "sold" in str(obj.get(sk,"")).lower() if sk else False)
                for v in obj.values(): walk(v, depth+1)
        walk(data)

    stands.sort(key=lambda x: x.get("pf", 0))
    return stands


# ══════════════════ PARSE __INITIAL_STATE__ (fallback) ══════════════════

def _parse_state_fallback(state):
    raw_stands = []
    seen = set()

    def add(name, price_raw, avail_raw=0, sold=False):
        name = str(name or "").strip()
        if not name: return
        try:   pf = float(str(price_raw).replace("₹","").replace(",",""))
        except: pf = 0
        price = f"₹{int(pf)}" if pf else "—"
        try:   av = int(avail_raw or 0)
        except: av = 0
        key = f"{name}|{price}"
        if key in seen: return
        seen.add(key)
        raw_stands.append({"name": name, "price": price, "available": av,
                           "sold_out": sold, "pf": pf})

    def walk(obj, depth=0):
        if depth > 12 or not isinstance(obj, (dict, list)): return
        if isinstance(obj, list):
            for i in obj: walk(i, depth+1)
        elif isinstance(obj, dict):
            kl = {k.lower(): k for k in obj}
            hp = any(k in kl for k in ("price","amount","ticketprice"))
            hn = any(k in kl for k in ("name","categoryname","title","label","blockname"))
            if hp and hn:
                nk = kl.get("name") or kl.get("categoryname") or kl.get("title") or kl.get("label") or kl.get("blockname")
                pk = kl.get("price") or kl.get("amount") or kl.get("ticketprice")
                ak = kl.get("availableseats") or kl.get("available") or kl.get("remaining") or kl.get("count")
                sk = kl.get("status") or kl.get("availability")
                nm   = obj.get(nk, ""); pr = obj.get(pk, 0)
                av   = obj.get(ak, 0) if ak else 0
                st   = str(obj.get(sk, "")).lower() if sk else ""
                sold = "sold" in st or "houseful" in st
                if nm and str(pr).replace(".","").isdigit():
                    add(nm, pr, av, sold)
            for v in obj.values(): walk(v, depth+1)

    walk(state)

    from collections import defaultdict
    tier_map = defaultdict(lambda: {"total": 0, "sold": 0, "avail": 0, "names": set()})

    for s in raw_stands:
        pf  = s["pf"]
        key = f"₹{int(pf)}" if pf else "—"
        tier_map[key]["total"]  += 1
        tier_map[key]["names"].add(s["name"])
        m = re.match(r'(BLOCK\s+[A-Z]+|[A-Z ]+?)(?:\s+BAY|\s+\d|\s+-)', s["name"], re.I)
        if m: tier_map[key]["names"].add(m.group(1).strip())
        if s["sold_out"]: tier_map[key]["sold"] += 1
        else:             tier_map[key]["avail"] += 1

    collapsed = []
    for price_str, d in sorted(tier_map.items(), key=lambda x: float(x[0].replace("₹","").replace(",","") or 0)):
        total = d["total"]; sold = d["sold"]; avail = d["avail"]
        pf    = float(price_str.replace("₹","").replace(",","") or 0)
        block_names = sorted({
            re.match(r'(BLOCK\s+[A-Z0-9]+)', n, re.I).group(1).upper()
            for n in d["names"] if re.match(r'BLOCK\s+[A-Z0-9]+', n, re.I)
        })
        label = ", ".join(block_names[:4]) if block_names else price_str
        if avail > 0:
            collapsed.append({"name": label, "price": price_str,
                               "available": avail, "sold_out": False,
                               "pf": pf, "total_bays": total, "sold_bays": sold})
        if sold > 0:
            collapsed.append({"name": label if avail == 0 else f"{label} (part sold)",
                               "price": price_str,
                               "available": 0, "sold_out": avail == 0,
                               "pf": pf, "total_bays": total, "sold_bays": sold})

    final = []
    seen_label = set()
    for s in collapsed:
        k = f"{s['name']}|{s['price']}"
        if k not in seen_label:
            seen_label.add(k)
            final.append(s)

    final.sort(key=lambda x: x["pf"])
    return final, True


# ══════════════════ BROWSER CONTEXT ══════════════════

def make_ctx(pw):
    """
    Cloud-safe: headless, no persistent profile.
    Anti-detection args still applied.
    """
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir="",          # empty = temp dir, no saved session needed
        headless=True,             # ← KEY CHANGE for cloud
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--single-process",    # saves memory on cloud
            "--no-zygote",
        ],
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        ignore_https_errors=True,
        viewport={"width": 1280, "height": 800},
    )
    ctx.add_init_script(STEALTH)
    ctx.add_init_script(XHR_HOOK)
    return ctx


# ══════════════════ STEP 2: GET STANDS ══════════════════

def get_stands(ctx, seat_layout_url, venue_code="", session_id="", event_code=""):
    page = ctx.new_page()
    stands  = []
    has_qty = False

    try:
        log.info(f"  [stands] {seat_layout_url}")
        page.goto(seat_layout_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        dec_list = page.evaluate("() => window.__bmsDecrypted || []")
        if dec_list:
            for txt in dec_list:
                parsed = _parse_showinfo(txt)
                if parsed:
                    stands  = parsed
                    has_qty = any(s["available"] > 0 for s in stands)
                    log.info(f"  [stands] ✅ CryptoJS hook: {len(stands)} stands qty={has_qty}")
                    break

        if not stands:
            blob = page.evaluate("() => window.__bmsEncBlob || ''")
            bundle_keys = []
            try:
                bundle_keys = page.evaluate(KEY_SCAN_JS) or []
                if bundle_keys:
                    log.info(f"  [stands] JS bundle keys: {bundle_keys}")
            except Exception as e:
                log.warning(f"  [stands] bundle scan: {e}")

            if blob:
                extra = bundle_keys + [venue_code, session_id, event_code,
                                       f"{venue_code}{session_id}"]
                log.info(f"  [stands] blob={len(blob)}b → decrypting...")
                txt, used_key = _try_decrypt(blob, extra)
                if txt:
                    parsed = _parse_showinfo(txt)
                    if parsed:
                        stands  = parsed
                        has_qty = any(s["available"] > 0 for s in stands)
                        log.info(f"  [stands] ✅ decrypt(key={used_key}): {len(stands)} stands qty={has_qty}")
            else:
                log.warning("  [stands] no encrypted blob captured")

        if not stands:
            state = page.evaluate("() => window.__INITIAL_STATE__ || null")
            if state:
                stands, _ = _parse_state_fallback(state)
                log.info(f"  [stands] fallback __INITIAL_STATE__: {len(stands)} price tiers")

    except Exception as e:
        log.warning(f"  [stands] {e}")
    finally:
        try: page.close()
        except: pass

    return stands, has_qty


# ══════════════════ FORMAT ══════════════════

SE = {
    "BOOK NOW":    "🟢",
    "IN QUEUE":    "🟡",
    "COMING SOON": "🔜",
    "SOLD OUT":    "🔴",
    "CLOSED":      "🔴",
    "NOTIFY ME":   "🔔",
    "UNKNOWN":     "❓",
}

def _fmt_stands(stands, has_qty):
    if not stands:
        return "  <i>No seat data</i>"
    avail = [s for s in stands if not s["sold_out"]]
    sold  = [s for s in stands if s["sold_out"]]
    lines = []
    if avail:
        lines.append(f"✅ <b>Available ({len(avail)} block(s))</b>")
        for s in avail:
            if has_qty and s["available"] > 0:
                qty = f" · Qty: <b>{s['available']}</b>"
            elif s.get("total_bays"):
                avail_bays = s.get("total_bays", 0) - s.get("sold_bays", 0)
                qty = f" · {avail_bays}/{s['total_bays']} bays avail"
            elif s["available"] > 0:
                qty = f" · {s['available']} seats"
            else:
                qty = ""
            lines.append(f"   • <b>{s['name']}</b>: {s['price']}{qty}")
    if sold:
        if avail: lines.append("")
        lines.append(f"🔴 <b>Sold Out ({len(sold)} block(s))</b>")
        for s in sold:
            bays = f" ({s.get('total_bays','?')} bays)" if s.get("total_bays") else ""
            lines.append(f"   • <s>{s['name']}</s>: {s['price']}{bays}")
    return "\n".join(lines)


def _safe_msg(parts, limit=4000):
    msg = "\n".join(parts)
    if len(msg) <= limit:
        return msg
    cut = msg.rfind("\n", 0, limit - 30)
    return msg[:cut] + "\n\n<i>…(truncated)</i>"


def build_msg(info, stands, has_qty, url):
    st = info["status"]
    ff = info["filling_fast"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if   st == "BOOK NOW"  and ff: hdr = "🚨⚡ <b>BOOKING OPEN + FILLING FAST!</b>"
    elif st == "BOOK NOW":         hdr = "🚨 <b>BOOKING IS NOW OPEN!</b>"
    elif st == "IN QUEUE"  and ff: hdr = "🟡⚡ <b>IN QUEUE + FILLING FAST!</b>"
    elif st == "IN QUEUE":         hdr = "🟡 <b>Virtual Queue Active</b>"
    elif st == "SOLD OUT":         hdr = "🔴 <b>SOLD OUT</b>"
    elif st == "COMING SOON":      hdr = "🔜 <b>Coming Soon</b>"
    elif ff:                       hdr = "⚡ <b>FILLING FAST!</b>"
    else:                          hdr = "🔔 <b>Seat Live Status</b>"

    urgent = st in ("BOOK NOW", "IN QUEUE") or ff

    parts = [hdr, "━━━━━━━━━━━━━━━━━━━━━━"]
    parts.append(f"🎭 <b>Event:</b> {info['event_name'] or url.split('/')[-1]}")
    if info["event_code"]:  parts.append(f"🔑 <b>Code:</b> {info['event_code']}")
    parts.append(f"🔗 {url}")
    if info["ticket_limit"]: parts.append(f"🎫 <b>Max Tickets:</b> {info['ticket_limit']}")
    parts.append(f"⏱ <b>Time:</b> {ts} IST")
    parts.append("━━━━━━━━━━━━━━━━━━━━━━")
    if info["date_str"]:   parts.append(f"📅 {info['date_str']}")
    if info["venue_str"]:  parts.append(f"📍 {info['venue_str']}")
    pl = info["price_onwards"]
    if ff and pl: pl += "  ⚡ <b>Filling Fast!</b>"
    if pl: parts.append(f"💰 {pl}")
    parts.append(f"📊 <b>Status:</b> {SE.get(st,'❓')} {st}")
    if info["seat_layout_url"]: parts.append(f"🗺 {info['seat_layout_url']}")
    parts.append("──────────────────────")
    parts.append(_fmt_stands(stands, has_qty))

    return _safe_msg(parts), urgent


# ══════════════════ CHANGE DETECTION ══════════════════

def _stands_sig(stands):
    return {s["name"]: (s["sold_out"], s["available"]) for s in stands}

def _stands_changed(old_stands, new_stands):
    if len(old_stands) != len(new_stands): return True
    old_sig = _stands_sig(old_stands)
    new_sig = _stands_sig(new_stands)
    for name, (sold, avail) in new_sig.items():
        prev = old_sig.get(name)
        if prev is None: return True
        if prev[0] != sold: return True
        if abs(prev[1] - avail) > 10: return True
    return False


# ══════════════════ EVENT PAGE ══════════════════

def get_event_info(page, url):
    info = {
        "event_name":"", "event_code":"", "status":"UNKNOWN",
        "filling_fast":False, "price_onwards":"", "date_str":"",
        "venue_str":"", "ticket_limit":"", "seat_layout_url":"",
        "venue_code":"", "session_id":"",
    }
    try:
        page.goto(url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        body = page.evaluate("() => document.body.innerText")
        if "you have been blocked" in body.lower():
            log.error("  CLOUDFLARE BLOCK"); return info, False

        state = page.evaluate("() => window.__INITIAL_STATE__ || null")
        if not state:
            return _fallback_info(info, page, url), True

        queries   = state.get("eventsSynopsisApi",{}).get("queries",{})
        prim_data = queries.get("getPrimaryData",{}).get("data",{})
        sess_data = queries.get("getPrimarySessionData",{}).get("data",{})

        if isinstance(prim_data, dict):
            ana = prim_data.get("meta",{}).get("analytics",{})
            info["event_code"] = ana.get("event_code","")
            es = ana.get("event_status","").lower()
            if   es == "active":                               info["status"] = "BOOK NOW"
            elif es in ("soldout","sold_out"):                  info["status"] = "SOLD OUT"
            elif es in ("upcoming","comingsoon","coming_soon"): info["status"] = "COMING SOON"
            elif es in ("closed","cancelled"):                  info["status"] = "CLOSED"

        if not isinstance(sess_data, dict): return info, True

        for tb in sess_data.get("header",{}).get("text",[]):
            for c in tb.get("components",[]):
                if c.get("elementType") == "h1":
                    info["event_name"] = c.get("text","")

        widgets = sess_data.get("widgets",{})
        if not isinstance(widgets, dict): return info, True

        for card in widgets.get("BOOK_CTA",{}).get("cards",[]):
            for tb in card.get("text",[]):
                for c in tb.get("components",[]):
                    txt  = c.get("text","").strip()
                    uuid = c.get("uuid","")
                    if not txt: continue
                    if "PRICE_DYNAMIC_TEXT" in uuid or "onwards" in txt.lower():
                        info["price_onwards"] = txt
                    if "AVAILABILITY_STATUS" in uuid or "STATUS_TEXT" in uuid:
                        tl = txt.lower()
                        if   "filling fast" in tl or "selling fast" in tl: info["filling_fast"] = True
                        elif "sold out" in tl or "houseful" in tl:          info["status"] = "SOLD OUT"
                        elif "coming soon" in tl:                           info["status"] = "COMING SOON"
            for btn in card.get("buttons",[]):
                for lc in btn.get("label",{}).get("components",[]):
                    if "book now" in lc.get("text","").lower():
                        if info["status"] not in ("SOLD OUT","COMING SOON","CLOSED"):
                            info["status"] = "BOOK NOW"
                cta = btn.get("cta",{}).get("url","")
                if "seat-layout" in cta:
                    info["seat_layout_url"] = cta
                    m = re.search(r"/seat-layout/[^/]+/([A-Z0-9]+)/(\d+)", cta, re.I)
                    if m:
                        info["venue_code"] = m.group(1).upper()
                        info["session_id"] = m.group(2)

        date_parts = []
        for wk in ("DESKTOP_EVENT_DETAILS","SESSION_DETAILS","EVENT_DETAILS"):
            for card in widgets.get(wk,{}).get("cards",[]):
                for tb in card.get("text",[]):
                    for c in tb.get("components",[]):
                        t = c.get("text","").strip()
                        if not t: continue
                        tl = t.lower()
                        if re.search(r"\b(mon|tue|wed|thu|fri|sat|sun)\b", tl):
                            date_parts.append(t)
                        elif re.search(r"\d{1,2}:\d{2}\s*(am|pm)", tl):
                            date_parts.append(t)
                        elif "ticket limit" in tl or "booking is" in tl:
                            m2 = re.search(r"\d+", t)
                            info["ticket_limit"] = m2.group() if m2 else t
                        elif ":" in t and not t.startswith("http") and 3 < len(t) < 60:
                            if not info["venue_str"]: info["venue_str"] = t
        if date_parts: info["date_str"] = " | ".join(date_parts)

        if not info["event_code"]:
            m3 = re.search(r"/(ET\d+)", url, re.I)
            if m3: info["event_code"] = m3.group(1).upper()

    except Exception as e:
        log.warning(f"  get_event_info: {e}")

    return info, True


def _fallback_info(info, page, url):
    try:
        body = page.evaluate("() => document.body.innerText").lower()
        btns = " ".join(page.evaluate("""() =>
            Array.from(document.querySelectorAll('button,a[role="button"]'))
            .map(e=>(e.innerText||'').trim().toLowerCase()).filter(Boolean)
        """))
        if   "sold out" in body or "houseful" in body:    info["status"] = "SOLD OUT"
        elif "book now" in btns or "book tickets" in btns:
            info["status"] = "BOOK NOW"
            info["filling_fast"] = "filling fast" in body or "selling fast" in body
        elif "coming soon" in body:                       info["status"] = "COMING SOON"
        m = re.search(r"/(ET\d+)", url, re.I)
        if m: info["event_code"] = m.group(1).upper()
    except: pass
    return info


# ══════════════════ HELPERS ══════════════════

def load_events():
    try:
        with open(EVENT_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except FileNotFoundError: return []
    except Exception as e: log.error(f"load_events: {e}"); return []


# ══════════════════ WORKER ══════════════════

def check_event(url):
    log.info(f"[CHECK] {url}")
    try:
        with sync_playwright() as pw:
            ctx  = make_ctx(pw)
            page = ctx.new_page()

            info, ok = get_event_info(page, url)
            if not ok:
                ctx.close(); return

            st = info["status"]
            log.info(f"  status={st} ff={info['filling_fast']} "
                     f"code={info['event_code']} layout={'✓' if info['seat_layout_url'] else '✗'}")

            stands  = []
            has_qty = False

            if info["seat_layout_url"]:
                stands, has_qty = get_stands(
                    ctx,
                    info["seat_layout_url"],
                    venue_code  = info["venue_code"],
                    session_id  = info["session_id"],
                    event_code  = info["event_code"],
                )
            elif st == "BOOK NOW":
                log.warning("  BOOK NOW but no seat_layout_url")

            if stands and all(s["sold_out"] for s in stands) and st == "BOOK NOW":
                info["status"] = "SOLD OUT"
                st = "SOLD OUT"

            ctx.close()

        old = state_cache.get(url)
        new = {"status": st, "ff": info["filling_fast"],
               "stands": stands, "has_qty": has_qty,
               "price": info["price_onwards"]}

        changed = (
            old is None
            or old["status"] != st
            or (info["filling_fast"] and not old.get("ff"))
            or _stands_changed(old.get("stands", []), stands)
        )

        label = info["event_code"] or info["event_name"][:20] or url[-15:]
        avail_count = len([s for s in stands if not s["sold_out"]])
        log.info(f"  [{label}] avail={avail_count} has_qty={has_qty} changed={changed}")

        if changed:
            state_cache[url] = new
            msg, urgent = build_msg(info, stands, has_qty, url)
            send_alert(msg, url if urgent else None, urgent)
            log.info(f"  ✅ Alert sent [{label}]")
        else:
            log.info(f"  ✓ No change [{label}]")

    except Exception as e:
        log.error(f"  Error [{url}]: {e}")
        traceback.print_exc()


# ══════════════════ TELEGRAM ══════════════════

def _tg(payload):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    for cid in CHAT_IDS:
        p = {**payload, "chat_id": cid}
        for _ in range(3):
            try:
                r = requests.post(url, json=p, timeout=15)
                if r.status_code == 200: break
                log.warning(f"TG {r.status_code}: {r.text[:120]}")
            except Exception as e: log.warning(f"TG: {e}")
            time.sleep(2)

def send_text(msg): _tg({"text": msg[:4096]})

def send_alert(msg, booking_url=None, urgent=False):
    p = {"text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
    if urgent and booking_url:
        p["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "🎟 BOOK NOW", "url": booking_url}]]})
    _tg(p)


# ══════════════════ MAIN LOOP ══════════════════

def run_cycle(by="auto", single_url=None):
    events = [single_url] if single_url else load_events()
    if not events:
        send_text("No events tracked. Use /addevent <url>"); return
    log.info(f"[CYCLE] {by} — {len(events)} event(s)")
    backup = dict(state_cache)
    if single_url: state_cache.pop(single_url, None)
    else:          state_cache.clear()
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(events))) as pool:
        for f in as_completed({pool.submit(check_event, u): u for u in events}):
            try: f.result()
            except Exception as e: log.error(f"  future: {e}")
    state_cache.update(backup)
    elapsed = time.time() - t0
    log.info(f"[CYCLE] done in {elapsed:.1f}s")
    return elapsed


def _uptime_str():
    s = int(time.time() - _start_time)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m}m {sec}s"


def main():
    global _running
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--url",  default=None)
    args = ap.parse_args()

    if args.once:
        log.info("="*50)
        log.info(f"BMS Monitor v13 — {'SINGLE' if args.url else 'FULL'} check")
        log.info("="*50)
        run_cycle("/monitor", single_url=args.url)
        return

    log.info("="*50)
    log.info("BMS Monitor v13 (Cloud) — started")
    log.info("="*50)
    send_text(
        f"☁️ <b>BMS Monitor v13 started (Cloud)</b>\n"
        f"Interval: {CHECK_INTERVAL}s | "
        f"Crypto: {'✅' if _CRYPTO_OK else '⚠️ no pycryptodome'} | "
        f"Headless: ✅"
    )
    _write_status({"state": "running"})

    while _running:
        try:
            events = load_events()
            if not events:
                log.info("No events — sleeping")
                _write_status({"state": "idle - no events"})
                time.sleep(CHECK_INTERVAL)
                continue

            _write_status({"state": "checking"})
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(events))) as pool:
                for f in as_completed({pool.submit(check_event, u): u for u in events}):
                    try: f.result()
                    except Exception as e: log.error(f"  future: {e}")

            sleep_for = max(0, CHECK_INTERVAL - (time.time()-t0))
            _write_status({"state": "sleeping", "next_check_in": int(sleep_for)})
            log.info(f"Cycle done — next in {sleep_for:.0f}s")

            # Sleep in small chunks so SIGTERM is handled quickly
            deadline = time.time() + sleep_for
            while _running and time.time() < deadline:
                time.sleep(min(5, deadline - time.time()))

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Main loop: {e}"); traceback.print_exc(); time.sleep(30)

    uptime = _uptime_str()
    log.info(f"Stopped — uptime {uptime}")
    send_text(f"🛑 <b>BMS Monitor v13 stopped</b>\nUptime: {uptime}")
    try: os.remove(STATUS_FILE)
    except: pass


if __name__ == "__main__":
    main()