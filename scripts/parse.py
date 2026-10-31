"""Парсинг Kaspi Магазина: люстры и бра до PRICE_MAX ₸.
Результат -> data.json (каталог + хэши фото для режима «Найти такую же»).
Запуск: GitHub Actions (кнопка Run workflow) или по расписанию.
Если категория не найдена автоматически, смотри parse-log.txt и папку debug/ — там что отвечает Kaspi."""
import json, re, time, os, io, math, html as htmllib, urllib.request, urllib.parse, datetime
from concurrent.futures import ThreadPoolExecutor

try:
    from PIL import Image
except Exception:
    Image = None

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BASE = "https://kaspi.kz"
PRICE_MAX = 150000          # собираем с запасом, в приложении по умолчанию фильтр «до 100 000»
MIN_ITEMS_OK = 30           # если собрали меньше — старый data.json не перезаписываем
HASH_IMAGES = 2             # сколько фото на товар хэшируем
TODAY = (datetime.datetime.utcnow() + datetime.timedelta(hours=5)).strftime("%Y-%m-%d")  # дата по Актау

KINDS = {
    "chandelier": {
        "label": "Люстры",
        "texts": ["люстра подвесная", "люстра потолочная"],
        "codes": ["Chandeliers", "Chandelier", "Ceiling chandeliers", "Ceiling lights", "Pendant lights"],
        "title_re": r"люстр|светильник|подвес",
    },
    "sconce": {
        "label": "Бра",
        "texts": ["бра настенное", "бра"],
        "codes": ["Sconces", "Wall lamps", "Wall lights", "Wall sconces", "Sconce", "Wall light fixtures"],
        "title_re": r"\bбра\b|настенн",
    },
}
BAD_TITLE = re.compile(r"^\s*(лампа|лампочка|светодиодная лампа|лента|подвес для|цоколь|абажур|плафон|патрон|провод|шнур|пульт)\b", re.I)

os.makedirs("debug", exist_ok=True)
log_lines = []


def log(s):
    print(s, flush=True)
    log_lines.append(s)
    open("parse-log.txt", "w", encoding="utf-8").write("\n".join(log_lines))


def dump(name, obj):
    try:
        open("debug/%s.json" % re.sub(r"[^A-Za-z0-9_.-]+", "_", name), "w", encoding="utf-8").write(
            json.dumps(obj, ensure_ascii=False, indent=1)[:150000])
    except Exception as e:
        log("  [debug] не записалось %s: %s" % (name, e))


def get_raw(url, accept="application/json, text/*", tries=4, timeout=40):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": accept, "Accept-Language": "ru-RU,ru;q=0.9",
                "Referer": BASE + "/shop/c/chandeliers/all/"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def get(url, accept="application/json, text/*", tries=4):
    return get_raw(url, accept, tries).decode("utf-8", "replace")


def flt(q, text="", sort="price-asc", page=0):
    u = BASE + "/yml/product-view/pl/filters?q=%s&text=%s&sort=%s&qs=&page=%d&ui=d&i=-1&c=750000000" % (
        urllib.parse.quote(q), urllib.parse.quote(text), sort, page)
    return json.loads(get(u))["data"]


def res(q, text="", sort="price-asc", page=0):
    u = BASE + "/yml/product-view/pl/results?page=%d&q=%s&text=%s&sort=%s&qs=&ui=d&i=-1&c=750000000" % (
        page, urllib.parse.quote(q), urllib.parse.quote(text), sort)
    return json.loads(get(u))["data"]


def summary(d):
    cards = d.get("cards", []) if isinstance(d, dict) else []
    return {
        "total": d.get("total") if isinstance(d, dict) else None,
        "titles": [c.get("title") for c in cards[:12]],
        "card_keys": sorted(cards[0].keys()) if cards else [],
        "filters": [{"id": f.get("id"), "rows": [r.get("id") for r in f.get("rows", [])[:10]]}
                    for f in d.get("filters", [])] if isinstance(d, dict) else [],
    }


# ---------- 1. поиск кода категории ----------
def test_code(kind, cfg, code):
    try:
        d = flt(":category:" + code)
    except Exception as e:
        log("  код %s: ошибка %s" % (code, str(e)[:80]))
        return False
    s = summary(d)
    dump("cat_%s_%s" % (kind, code), s)
    titles = s["titles"]
    ok = sum(1 for t in titles if t and re.search(cfg["title_re"], t, re.I))
    log("  код %s: total=%s, подходящих названий %d из %d" % (code, s["total"], ok, len(titles)))
    return bool(s["total"] and s["total"] >= 30 and titles and ok / len(titles) >= 0.5)


def discover(kind, cfg):
    log("поиск категории: %s" % cfg["label"])
    for code in cfg["codes"]:
        if test_code(kind, cfg, code):
            return code
    found = []
    for text in cfg["texts"]:
        try:
            d = flt("", text=text)
        except Exception as e:
            log("  текстовый поиск '%s': ошибка %s" % (text, str(e)[:80]))
            continue
        dump("text_%s_%s" % (kind, text), summary(d))
        log("  текстовый поиск '%s': total=%s" % (text, d.get("total")))
        for f in d.get("filters", []):
            for r in f.get("rows", []):
                m = re.search(r"category:([^:]+)", str(r.get("id", "")))
                if m and m.group(1) not in found:
                    found.append(m.group(1))
    for code in found[:8]:
        if test_code(kind, cfg, code):
            return code
    return None


# ---------- 2. список ----------
def price_part(lo):
    f = lambda n: "{:,}".format(int(n)).replace(",", " ")
    return ":price:%s - %s т" % (f(lo), f(PRICE_MAX))


raw, seen_ids = [], set()


def good(c, kind, cfg, text_mode):
    t = htmllib.unescape(c.get("title", ""))
    if BAD_TITLE.search(t):
        return False
    if text_mode and not re.search(cfg["title_re"], t, re.I):
        return False
    return True


def crawl(kind, cfg, code):
    text_mode = code is None
    base_q = "" if text_mode else ":category:" + code
    texts = cfg["texts"] if text_mode else [""]
    got = 0
    for text in texts:
        lo = 0
        for rnd in range(10):
            q = base_q + (price_part(lo) if lo else "")
            try:
                d0 = flt(q, text=text, page=0)
            except Exception as e:
                log("  [%s] ошибка первой страницы: %s" % (cfg["label"], str(e)[:100]))
                break
            total = d0.get("total", 0)
            pages = (total + 11) // 12
            last = lo
            stopped_early = False
            for p in range(pages):
                try:
                    cards = d0.get("cards", []) if p == 0 else res(q, text=text, page=p)
                except Exception as e:
                    log("  [%s] стоп на странице %d из %d: %s" % (cfg["label"], p, pages, str(e)[:100]))
                    stopped_early = True
                    break
                if not cards:
                    stopped_early = True
                    break
                for c in cards:
                    last = max(last, c.get("unitPrice", 0))
                    if c["id"] in seen_ids or c.get("unitPrice", 0) > PRICE_MAX:
                        continue
                    if not good(c, kind, cfg, text_mode):
                        continue
                    seen_ids.add(c["id"])
                    c["_kind"] = kind
                    raw.append(c)
                    got += 1
                if cards[-1].get("unitPrice", 0) > PRICE_MAX:
                    break
                time.sleep(0.4)
            log("  [%s] раунд %d: total=%s, страниц %d, цена дошла до %s, собрано %d" % (
                cfg["label"], rnd + 1, total, pages, last, got))
            if stopped_early and last > lo and last < PRICE_MAX:
                lo = last  # лимит листания (~100 страниц): продолжаем с достигнутой цены
                continue
            break
    return got


for kind, cfg in KINDS.items():
    code = discover(kind, cfg)
    log("%s: код категории = %s" % (cfg["label"], code or "не найден, ищу по тексту"))
    n = crawl(kind, cfg, code)
    log("%s: собрано %d" % (cfg["label"], n))
log("всего карточек: %d" % len(raw))

# ---------- 3. прошлые данные (кэш характеристик, хэшей, истории цен) ----------
prev = {}
first_run = True
if os.path.exists("data.json"):
    try:
        pd = json.load(open("data.json", encoding="utf-8"))
        prev = {x["id"]: x for x in pd.get("items", [])}
        first_run = not prev
    except Exception as e:
        log("прошлый data.json не прочитался: %s" % e)


# ---------- 4. характеристики карточки ----------
SPEC_RE = re.compile(r'<h4 class="specifications-list__header">(.*?)</h4>|<dl key="[^"]*" class="specifications-list__spec"><dt[^>]*><span class="specifications-list__spec-term-text">(.*?)</span></dt><dd class="specifications-list__spec-definition">(.*?)</dd></dl>', re.S)


def clean(s):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def specs_of(page_html):
    out, section = [], ""
    for m in SPEC_RE.finditer(page_html):
        if m.group(1) is not None:
            section = clean(m.group(1))
        else:
            out.append((section, clean(m.group(2)), clean(m.group(3))))
    return out


def num(s):
    m = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)", s or "")
    return float(m.group(1).replace(" ", "").replace(",", ".")) if m else None


def to_cm(value):
    n = num(value)
    if n is None:
        return None
    v = (value or "").lower()
    if "мм" in v or n > 250:
        n = n / 10.0
    elif "м" in v and "см" not in v and n < 10:
        n = n * 100.0
    return round(n, 1) if 3 <= n <= 250 else None


KEEP_SPEC = re.compile(r"диаметр|высота|ширина|длина|цоколь|количество|кол-во|число|мощность|материал|стиль|цвет|площадь|тип", re.I)


def fetch_specs(c):
    url = BASE + "/shop" + c["shopLink"]
    try:
        return c, specs_of(get(url, "text/html,application/xhtml+xml")), None
    except Exception as e:
        return c, None, str(e)
    finally:
        time.sleep(0.3)


def derive(title, sp):
    d = {n: v for _, n, v in sp}
    diam = height = lamps = None
    socket = ""
    for n, v in d.items():
        ln = n.lower()
        if diam is None and "диаметр" in ln:
            diam = to_cm(v)
        if height is None and "высота" in ln:
            height = to_cm(v)
        if lamps is None and re.search(r"количество ламп|кол-во ламп|число ламп|количество плафонов|количество светильников", ln):
            m = re.search(r"\d+", v)
            lamps = int(m.group()) if m else None
        if not socket and "цоколь" in ln:
            socket = v
    t = htmllib.unescape(title)
    if diam is None:
        m = re.search(r"(?:диаметр|диам\.?|[dDøØ⌀])\s*[:=]?\s*(\d{2,3})\s*(?:см|cm)?", t)
        if m:
            diam = float(m.group(1))
    if lamps is None:
        m = re.search(r"ламп[^\d]{0,6}(\d{1,3})", t, re.I)
        if m:
            lamps = int(m.group(1))
    if not socket:
        m = re.search(r"\b(E\d{2}|G\d{1,2}|GU\d{1,2})\b", t)
        if m:
            socket = m.group(1)
    return diam, height, lamps, socket


# ---------- 5. хэши фото (dHash 16x16 = 256 бит) ----------
def dhash(data, size=16):
    im = Image.open(io.BytesIO(data))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        im = bg
    im = im.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(im.getdata())
    bits = 0
    for y in range(size):
        for x in range(size):
            bits = (bits << 1) | (1 if px[y * (size + 1) + x] > px[y * (size + 1) + x + 1] else 0)
    return "%0*x" % (size * size // 4, bits)


def hash_url(url):
    try:
        return dhash(get_raw(url, "image/*", tries=3, timeout=30))
    except Exception:
        return None


# ---------- 6. сборка ----------
def build(c, sp):
    diam, height, lamps, socket = derive(c["title"], sp or [])
    imgs = []
    for pi in (c.get("previewImages") or []):
        u = pi.get("medium") or pi.get("small") or pi.get("large")
        if u and u not in imgs:
            imgs.append(u)
    return {
        "id": c["id"], "kind": c.get("_kind", "chandelier"), "title": htmllib.unescape(c["title"]),
        "brand": c.get("brand", ""), "price": c["unitPrice"], "rating": c.get("rating"),
        "reviews": c.get("reviewsQuantity"), "url": BASE + "/shop" + c["shopLink"].split("?")[0],
        "images": imgs[:3], "diam": diam, "height": height, "lamps": lamps, "socket": socket,
        "specs": [[n, v] for _, n, v in (sp or []) if KEEP_SPEC.search(n)][:14],
    }


need = [c for c in raw if not (prev.get(c["id"]) and prev[c["id"]].get("specs") is not None)]
log("карточки: всего %d, из кэша %d, грузим %d" % (len(raw), len(raw) - len(need), len(need)))
sp_map, failed = {}, 0
with ThreadPoolExecutor(max_workers=4) as ex:
    for i, (c, sp, err) in enumerate(ex.map(fetch_specs, need)):
        if i % 100 == 0:
            log("  характеристики: %d / %d" % (i, len(need)))
        if sp is None:
            failed += 1
        else:
            sp_map[c["id"]] = sp
log("характеристики не загрузились у %d карточек" % failed)

items = []
for c in raw:
    p = prev.get(c["id"])
    if c["id"] in sp_map:
        it = build(c, sp_map[c["id"]])
    elif p and p.get("specs") is not None:
        it = build(c, [("", n, v) for n, v in p["specs"]])
        for k in ("diam", "height", "lamps", "socket"):
            it[k] = p.get(k)
        it["specs"] = p["specs"]
    else:
        it = build(c, [])
    hist = list(p.get("hist", [])) if p else []
    if not hist or hist[-1][1] != it["price"]:
        hist.append([TODAY, it["price"]])
    it["hist"] = hist[-12:]
    it["prev_price"] = it["hist"][-2][1] if len(it["hist"]) > 1 else None
    it["first_seen"] = p.get("first_seen", TODAY) if p else TODAY
    it["_prev_hashes"] = dict(zip(p.get("images", []), p.get("dh", []))) if p else {}
    items.append(it)

# хэши
if Image is None:
    log("Pillow не установлен — хэши фото пропущены")
    for it in items:
        it["dh"] = []
else:
    jobs = []
    for it in items:
        for u in it["images"][:HASH_IMAGES]:
            if not it["_prev_hashes"].get(u):
                jobs.append(u)
    log("хэширование фото: нужно %d" % len(jobs))
    done = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, (u, h) in enumerate(zip(jobs, ex.map(hash_url, jobs))):
            if i % 300 == 0:
                log("  фото: %d / %d" % (i, len(jobs)))
            done[u] = h
    bad = 0
    for it in items:
        dh, imgs = [], []
        for u in it["images"][:HASH_IMAGES]:
            h = it["_prev_hashes"].get(u) or done.get(u)
            if h:
                dh.append(h)
                imgs.append(u)
            else:
                bad += 1
        it["dh"] = dh
        it["hashed_images"] = imgs
    log("фото без хэша: %d" % bad)
for it in items:
    it.pop("_prev_hashes", None)

# ---------- 7. эталон по ссылке (необязательно) ----------
ref_url = os.environ.get("REF_URL", "").strip()
if ref_url:
    try:
        m = re.search(r"-(\d{5,})/?(?:[?#]|$)", ref_url) or re.search(r"(\d{6,})", ref_url)
        rid = m.group(1) if m else None
        if rid and not any(i["id"] == rid for i in items):
            page = get(ref_url.split("?")[0], "text/html,application/xhtml+xml")
            title = clean(re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S).group(1)) if re.search(r"<h1[^>]*>", page) else ""
            og = re.search(r'property="og:image"\s+content="([^"]+)"', page)
            price = None
            pm = re.search(r'"(?:lowPrice|price)"\s*:\s*"?(\d+)', page)
            if pm:
                price = int(pm.group(1))
            sp = specs_of(page)
            diam, height, lamps, socket = derive(title, sp)
            imgs = [og.group(1)] if og else []
            dh = [h for h in [hash_url(u) for u in imgs] if h] if Image else []
            items.append({"id": rid, "kind": "chandelier", "title": title or ("Эталон " + rid), "brand": "", "price": price or 0,
                          "rating": None, "reviews": None, "url": ref_url.split("?")[0], "images": imgs, "diam": diam,
                          "height": height, "lamps": lamps, "socket": socket, "specs": [[n, v] for _, n, v in sp if KEEP_SPEC.search(n)][:14],
                          "dh": dh, "hashed_images": imgs, "hist": [[TODAY, price or 0]], "prev_price": None,
                          "first_seen": TODAY, "ref": True})
            log("эталон добавлен: %s (%s ₸)" % (title, price))
        else:
            log("эталон уже в базе или id не найден в ссылке")
    except Exception as e:
        log("эталон по ссылке не добавился: %s" % str(e)[:120])

for it in items:
    hi = it.pop("hashed_images", None)
    if hi is not None:  # сначала фото с хэшем, чтобы dh[i] соответствовал images[i]
        it["images"] = hi + [u for u in it["images"] if u not in hi]
items.sort(key=lambda x: x["price"])
by_kind = {k: sum(1 for i in items if i["kind"] == k) for k in KINDS}
log("итого: %d (%s), с хэшем фото: %d, с диаметром: %d" % (
    len(items), by_kind, sum(1 for i in items if i.get("dh")), sum(1 for i in items if i.get("diam"))))

if len(items) < MIN_ITEMS_OK and prev:
    log("СЛИШКОМ МАЛО карточек (%d) — старый data.json сохранён, смотри debug/ и лог" % len(items))
elif len(items) < MIN_ITEMS_OK:
    log("СЛИШКОМ МАЛО карточек (%d) — data.json не записан, смотри debug/ и лог выше" % len(items))
else:
    json.dump({"generated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), "date": TODAY,
               "first_run": first_run, "count": len(items), "kinds": by_kind, "items": items},
              open("data.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    log("готово: data.json")
