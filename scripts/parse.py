"""Парсинг Kaspi Магазина: люстры и бра.
Рабочая схема (как у телефонов): режем выдачу официальными фильтрами Kaspi (цена, комната, бренд…),
чтобы каждый срез влез в лимит листания (300 страниц), затем тянем характеристики карточек.
Прогресс сохраняется в data.json по ходу работы; повторный запуск продолжает с кэша.
Настройки приходят из переменных окружения (в GitHub Actions — поля запуска)."""
import json, re, time, os, io, shutil, subprocess, html as htmllib, urllib.request, urllib.parse, urllib.error, datetime
import diam as D
from concurrent.futures import ThreadPoolExecutor

try:
    from PIL import Image
except Exception:
    Image = None

START = time.time()


def env(name, default):
    v = os.environ.get(name, "").strip()
    return v if v else default


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BASE = "https://kaspi.kz"
PRICE_MIN = int(env("PRICE_MIN", "10000"))
PRICE_MAX = int(env("PRICE_MAX", "100000"))
ROOM = env("ROOM", "гостиная")                       # для люстр; "любая" — без фильтра комнаты
MAXPAGES = int(env("MAXPAGES", "300"))                # лимит листания Kaspi
CAP = MAXPAGES * 12
WORKERS = int(env("WORKERS", "8"))
MODE = env("MODE", "resume")                          # resume — не обходить список заново, если есть data.json; full — обойти заново
T_ALL = float(env("TOTAL_MINUTES", "150")) * 60       # общий бюджет времени
T_HASH = T_ALL * 0.45                                 # фото-хэши до 45% бюджета
T_OCR = T_ALL * 0.80                                  # чтение надписей на фото до 80%, дальше характеристики
OCR_WORKERS = int(env("OCR_WORKERS", str((os.cpu_count() or 2) + 1)))
HASH_IMAGES = int(env("HASH_IMAGES", "1"))
MIN_ITEMS_OK = 5
TODAY = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)).strftime("%Y-%m-%d")  # дата по Актау

KINDS = {
    "chandelier": {"label": "Люстры", "codes": ["Chandeliers", "Chandelier", "Ceiling lights", "Pendant lights"],
                   "title_re": r"люстр|светильник|подвес"},
    "sconce": {"label": "Бра", "codes": ["Wall lights", "Sconces", "Wall lamps", "Wall sconces"],
               "title_re": r"\bбра\b|настенн"},
}
BAD_TITLE = re.compile(r"^\s*(лампа|лампочка|светодиодная лампа|лента|подвес для|цоколь|абажур|плафон|патрон|провод|шнур|пульт)\b", re.I)
SPLIT_PRIORITY = ["manufacturerName", "*Installation", "*Colour", "*Base type", "*Shade material", "*Features"]

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


# ---------- сеть ----------
def get_raw(url, accept="application/json, text/*", tries=4, timeout=40):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": accept, "Accept-Language": "ru-RU,ru;q=0.9",
                "Referer": BASE + "/shop/c/chandeliers/all/"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 404):
                break  # повтор не поможет (лимит листания / нет страницы)
            time.sleep(2 * (i + 1))
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def get(url, accept="application/json, text/*", tries=4):
    return get_raw(url, accept, tries).decode("utf-8", "replace")


def flt(q, page=0):
    u = BASE + "/yml/product-view/pl/filters?q=%s&text=&sort=price-asc&qs=&page=%d&ui=d&i=-1&c=750000000" % (urllib.parse.quote(q), page)
    return json.loads(get(u))["data"]


def res(q, page=0):
    u = BASE + "/yml/product-view/pl/results?page=%d&q=%s&text=&sort=price-asc&qs=&ui=d&i=-1&c=750000000" % (page, urllib.parse.quote(q))
    return json.loads(get(u))["data"]


# ---------- 1. категории и срезы ----------
def facet_rows(d0, key):
    """строки фильтра (только с товарами); key '*Room' — по окончанию id"""
    for f in d0.get("filters", []):
        fid = f.get("id", "")
        if fid == key or (key.startswith("*") and fid.endswith(key)):
            return fid, [r for r in f.get("rows", []) if r.get("count", 0) > 0]
    return None, []


def summary(d):
    cards = d.get("cards", [])
    return {"total": d.get("total"), "titles": [c.get("title") for c in cards[:12]], "card_keys": sorted(cards[0].keys()) if cards else [],
            "filters": [{"id": f.get("id"), "rows": [[r.get("id"), r.get("count")] for r in f.get("rows", [])[:12]]} for f in d.get("filters", [])]}


def discover(kind, cfg):
    for code in cfg["codes"]:
        try:
            d = flt(":category:" + code)
        except Exception as e:
            log("  код %s: ошибка %s" % (code, str(e)[:80]))
            continue
        s = summary(d)
        dump("cat_%s_%s" % (kind, code), s)
        titles = s["titles"]
        ok = sum(1 for t in titles if t and re.search(cfg["title_re"], t, re.I))
        log("  код %s: total=%s, подходящих названий %d из %d" % (code, s["total"], ok, len(titles)))
        if s["total"] and s["total"] >= 30 and titles and ok / len(titles) >= 0.5:
            return code
    return None


def price_range(label):
    nums = [int(n.replace(" ", "")) for n in re.findall(r"\d[\d ]*\d|\d", label)]
    if label.startswith("до") and nums:
        return 0, nums[0]
    if label.startswith("более") and nums:
        return nums[0], 10 ** 12
    if len(nums) >= 2:
        return nums[0], nums[1]
    return 0, 10 ** 12


def slices(q, d0=None, depth=0):
    """рекурсивно режем запрос фильтрами, пока срез не влезет в лимит листания"""
    d0 = d0 or flt(q)
    total = d0.get("total", 0)
    if not total:
        return []
    if total <= CAP:
        return [(q, d0)]
    for key in SPLIT_PRIORITY:
        fid, rows = facet_rows(d0, key)
        if not rows or (":" + fid + ":") in q:
            continue
        log("  %sсрез %d шт. режу по '%s' (%d знач.)" % ("  " * depth, total, fid, len(rows)))
        out = []
        for r in rows:
            time.sleep(0.15)
            try:
                out += slices(q + r["id"], None, depth + 1)
            except Exception as e:
                log("  ошибка среза %s: %s" % (r["id"][:50], str(e)[:80]))
        return out
    log("  %sсрез %d шт. нечем резать дальше — берём первые %d" % ("  " * depth, total, CAP))
    return [(q, d0)]


def base_slices(kind, cfg, code):
    q0 = ":category:" + code
    d = flt(q0)
    if kind == "chandelier" and ROOM.lower() not in ("любая", "any", "все"):
        _, rows = facet_rows(d, "*Room")
        m = [r for r in rows if r["id"].lower().endswith(":" + ROOM.lower())]
        if m:
            q0 += m[0]["id"]
            log("  фильтр комнаты: %s" % ROOM)
        else:
            log("  комната '%s' не найдена среди %s — без фильтра комнаты" % (ROOM, [r["id"].split(":")[-1] for r in rows]))
    if kind == "sconce":
        _, rows = facet_rows(d, "*Purpose")
        m = [r for r in rows if "жил" in r["id"].lower()]
        if m:
            q0 += m[0]["id"]
            log("  фильтр назначения: для жилых помещений")
    d = flt(q0)
    log("  после предустановленных фильтров: %s шт." % d.get("total"))
    fid, rows = facet_rows(d, "price")
    out = []
    if rows:
        for r in rows:
            lo, hi = price_range(r["id"].split(":", 2)[2])
            if hi > PRICE_MIN and lo < PRICE_MAX:
                log("  ценовой диапазон %s (%d шт.)" % (r["id"].split(":", 2)[2], r["count"]))
                out += slices(q0 + r["id"])
    else:
        log("  ценовые фильтры не найдены — без разреза по цене")
        out = slices(q0, d)
    return out


raw, seen_ids = [], set()

# ---------- 0. прошлые данные ----------
prev, base_date, LFMT = {}, TODAY, None
if os.path.exists("data.json"):
    try:
        pd = json.load(open("data.json", encoding="utf-8"))
        prev = {x["id"]: x for x in pd.get("items", [])}
        base_date = pd.get("base_date") or pd.get("date") or TODAY
        LFMT = pd.get("lfmt")
    except Exception as e:
        log("прошлый data.json не прочитался: %s" % e)
RESUME = MODE == "resume" and len(prev) >= int(env("MIN_RESUME", "1000"))
log("режим: %s%s" % ("дозагрузка (список из прошлого запуска, %d карточек)" % len(prev) if RESUME else "полный обход списка",
                      "" if RESUME or MODE != "resume" else " [data.json пуст или мал — режим resume невозможен]"))


def good(c):
    return not BAD_TITLE.search(htmllib.unescape(c.get("title", "")))


def crawl(kind, cfg, q, d0):
    total = d0.get("total", 0)
    pages = min((total + 11) // 12, MAXPAGES)
    errs = [0]

    def page(p):
        if p == 0:
            return d0.get("cards", [])
        time.sleep(0.15)
        try:
            return res(q, page=p)
        except Exception:
            errs[0] += 1
            return []
    got = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for cards in ex.map(page, range(pages)):
            for c in cards:
                pr = c.get("unitPrice", 0)
                if c["id"] in seen_ids or not (PRICE_MIN <= pr <= PRICE_MAX) or not good(c):
                    continue
                seen_ids.add(c["id"])
                c["_kind"] = kind
                raw.append(c)
                got += 1
    if errs[0]:
        log("    страниц с ошибкой: %d из %d" % (errs[0], pages))
    return got


for kind, cfg in ([] if RESUME else KINDS.items()):
    log("== %s" % cfg["label"])
    code = discover(kind, cfg)
    if not code:
        log("%s: категория не найдена — смотри debug/" % cfg["label"])
        continue
    log("%s: код категории = %s" % (cfg["label"], code))
    sl = base_slices(kind, cfg, code)
    log("%s: срезов %d" % (cfg["label"], len(sl)))
    n = 0
    for i, (q, d0) in enumerate(sl):
        n += crawl(kind, cfg, q, d0)
        if i % 20 == 19:
            log("  срезов обработано %d / %d, собрано %d" % (i + 1, len(sl), n))
    log("%s: собрано %d" % (cfg["label"], n))
if RESUME:
    for p in prev.values():
        raw.append({"id": p["id"], "title": p["title"], "brand": p.get("brand", ""), "unitPrice": p["price"], "rating": p.get("rating"),
                    "shopLink": p["url"][len(BASE + "/shop"):], "previewImages": [{"medium": u} for u in p.get("images", [])], "_kind": p["kind"]})
log("всего карточек: %d" % len(raw))
for c in raw:   # формат «большой» картинки из выдачи (нужен для чтения надписей); в resume берём из прошлого запуска
    for pi in (c.get("previewImages") or [])[:1]:
        if pi.get("large") and not LFMT:
            m = re.search(r"format=([\w-]+)", pi["large"])
            LFMT = m.group(1) if m else LFMT
hash_map = {}
for p in prev.values():
    for u, h in zip(p.get("images", [])[:HASH_IMAGES], p.get("dh", [])):
        if h:
            hash_map[u] = h

# ---------- 3. характеристики ----------
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


def derive_other(title, sp):
    height = lamps = None
    socket = ""
    for n, v in sp:
        ln = n.lower()
        if height is None and "высота" in ln:
            height = D.to_cm(v)
        if lamps is None and re.search(r"количество ламп|кол-во ламп|число ламп|количество плафонов|количество светильников", ln):
            m = re.search(r"\d+", v)
            lamps = int(m.group()) if m else None
        if not socket and "цоколь" in ln:
            socket = v
    t = htmllib.unescape(title)
    if lamps is None:
        m = re.search(r"ламп[^\d]{0,6}(\d{1,3})", t, re.I)
        if m:
            lamps = int(m.group(1))
    if not socket:
        m = re.search(r"\b(E\d{2}|G\d{1,2}|GU\d{1,2})\b", t)
        if m:
            socket = m.group(1)
    return height, lamps, socket


def fetch_specs(c):
    try:
        sp = specs_of(get(BASE + "/shop" + c["shopLink"], "text/html,application/xhtml+xml"))
        time.sleep(0.1)
        return c, sp, None
    except Exception as e:
        return c, None, str(e)


sp_map = {}
ocr_res = {}   # id -> диаметр с фото (число) или 0 (читали, надписи нет)


def build_items():
    items = []
    for c in raw:
        p = prev.get(c["id"])
        sp = sp_map.get(c["id"])
        if sp is None and p and p.get("specs") is not None:
            sp = [(n, v) for n, v in p["specs"]]
        else:
            sp = [(n, v) for _, n, v in sp] if sp is not None else None
        height, lamps, socket = derive_other(c["title"], sp or [])
        if sp is None and p:   # характеристики ещё не загружены: берём прошлые значения, если были
            height, lamps, socket = p.get("height", height), p.get("lamps", lamps), p.get("socket", socket)
        ocr_tried = c["id"] in ocr_res or bool(p and p.get("ocr"))
        odiam = ocr_res.get(c["id"]) if c["id"] in ocr_res else (p.get("odiam") if p else None)
        url = BASE + "/shop" + c["shopLink"].split("?")[0]
        specs_kept = [[n, v] for n, v in sp if D.KEEP.search(n)][:14] if sp is not None else None
        diam, dsrc = D.choose(c.get("_kind", "chandelier"), url, htmllib.unescape(c["title"]), specs_kept, odiam or None)
        imgs = []
        for pi in (c.get("previewImages") or []):
            u = pi.get("medium") or pi.get("small") or pi.get("large")
            if u and u not in imgs:
                imgs.append(u)
        imgs = imgs[:2]
        price = c["unitPrice"]
        hist = list(p.get("hist", [])) if p else []
        if not hist or hist[-1][1] != price:
            hist.append([TODAY, price])
        hist = hist[-12:]
        items.append({
            "id": c["id"], "kind": c.get("_kind", "chandelier"), "title": htmllib.unescape(c["title"]), "brand": c.get("brand", ""),
            "price": price, "rating": c.get("rating"), "url": url, "images": imgs,
            "dh": [hash_map.get(u) for u in imgs[:HASH_IMAGES]], "diam": diam, "dsrc": dsrc, "fam": D.family(url), "height": height, "lamps": lamps, "socket": socket,
            "ocr": (1 if ocr_tried else None), "odiam": (odiam or None), "specs": specs_kept,
            "hist": hist, "prev_price": hist[-2][1] if len(hist) > 1 else None,
            "first_seen": p.get("first_seen", TODAY) if p else TODAY,
        })
    items.sort(key=lambda x: x["price"])
    return items


def save(tag):
    items = build_items()
    if len(items) < MIN_ITEMS_OK:
        log("[%s] мало карточек (%d) — data.json не трогаю" % (tag, len(items)))
        return items
    by_kind = {k: sum(1 for i in items if i["kind"] == k) for k in KINDS}
    with_specs = sum(1 for i in items if i["specs"] is not None)
    json.dump({"generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "date": TODAY, "base_date": base_date,
               "count": len(items), "kinds": by_kind, "with_specs": with_specs, "filters": {"price": [PRICE_MIN, PRICE_MAX], "room": ROOM}, "lfmt": LFMT,
               "items": items}, open("data.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    log("[%s] data.json: %d карточек, характеристики у %d, с диаметром %d, хэш фото у %d" % (
        tag, len(items), with_specs, sum(1 for i in items if i["diam"]), sum(1 for i in items if any(i["dh"]))))
    return items


save("после списка")


# ---------- 4. хэши фото (dHash 16x16) ----------
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
        return url, dhash(get_raw(url, "image/*", tries=2, timeout=30))
    except Exception:
        return url, None


if Image is None:
    log("Pillow не установлен — хэши фото пропущены")
else:
    jobs = []
    for c in raw:
        for pi in (c.get("previewImages") or [])[:1]:
            u = pi.get("medium") or pi.get("small") or pi.get("large")
            if u and u not in hash_map and u not in jobs:
                jobs.append(u)
    log("хэширование фото: нужно %d" % len(jobs))
    for i in range(0, len(jobs), 400):
        if time.time() - START > T_HASH:
            log("лимит времени на фото-хэши: осталось %d — продолжу при следующем запуске" % (len(jobs) - i))
            break
        with ThreadPoolExecutor(max_workers=WORKERS * 2) as ex:
            for u, h in ex.map(hash_url, jobs[i:i + 400]):
                if h:
                    hash_map[u] = h
        log("  фото: %d / %d" % (min(i + 400, len(jobs)), len(jobs)))
        if (i // 400) % 5 == 4:
            save("промежуточно")
save("после фото-хэшей")

# ---------- 5. надписи на фото («Диаметр 80 см») ----------
OCR_ATTEMPTS = [(420, "11"), (760, "6")]
OCR_FORMATS = [f for f in ([LFMT] if LFMT else []) + ["preview-large", "gallery-large", "gallery-medium", "preview-medium"] if f]
ocr_stat = {"скачано": 0, "не скачалось": 0, "с диаметром": 0, "без надписи": 0}
ocr_fmt_ok = []


def ocr_one(it):
    """-> (id, диаметр | 0 | None (не удалось скачать))"""
    base = it["images"][0] if it.get("images") else None
    if not base:
        return it["id"], None
    data = None
    for fmt in (ocr_fmt_ok[:1] + [f for f in OCR_FORMATS if f not in ocr_fmt_ok[:1]]):
        u = re.sub(r"format=[\w-]+", "format=" + fmt, base) if "format=" in base else base
        try:
            data = get_raw(u, "image/*", tries=1, timeout=30)
            if not ocr_fmt_ok:
                ocr_fmt_ok.append(fmt)
            break
        except Exception:
            continue
    if data is None:
        return it["id"], None
    try:
        im0 = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return it["id"], None
    for width, psm in OCR_ATTEMPTS:   # разные масштабы/режимы: единого лучшего нет, вместе ловят больше надписей
        try:
            im = im0.resize((width, max(1, int(im0.height * width / im0.width))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            out = subprocess.run(["tesseract", "stdin", "stdout", "-l", OCR_LANG, "--psm", psm], input=buf.getvalue(), capture_output=True,
                                 timeout=60, env=dict(os.environ, OMP_THREAD_LIMIT="1")).stdout.decode("utf-8", "replace")
        except Exception:
            continue
        d = D.from_ocr_text(out)
        if d:
            return it["id"], d
    return it["id"], 0


if Image is None or not shutil.which("tesseract"):
    log("tesseract или Pillow не найден — чтение надписей на фото пропущено")
else:
    OCR_LANG = "rus+eng" if "rus" in subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout else "eng"
    cur = build_items()
    todo = [i for i in cur if i["kind"] == "chandelier" and (not i["diam"] or i.get("dsrc") in D.APPROX) and not i.get("ocr") and i.get("images")]
    todo.sort(key=lambda i: i["price"])
    log("надписи на фото: нужно прочитать %d (форматы фото: %s, язык: %s, потоков %d)" % (len(todo), OCR_FORMATS[:3], OCR_LANG, OCR_WORKERS))
    for i in range(0, len(todo), 200):
        if time.time() - START > T_OCR:
            log("лимит времени на надписи: осталось %d — продолжу при следующем запуске" % (len(todo) - i))
            break
        with ThreadPoolExecutor(max_workers=OCR_WORKERS) as ex:
            for cid, res_ in ex.map(ocr_one, todo[i:i + 200]):
                if res_ is None:
                    ocr_stat["не скачалось"] += 1
                else:
                    ocr_res[cid] = res_
                    ocr_stat["с диаметром" if res_ else "без надписи"] += 1
        ocr_stat["скачано"] = ocr_stat["с диаметром"] + ocr_stat["без надписи"]
        log("  надписи: %d / %d %s формат фото: %s" % (min(i + 200, len(todo)), len(todo), ocr_stat, ocr_fmt_ok[:1]))
        if (i // 200) % 5 == 4:
            save("промежуточно")
    save("после надписей")

# ---------- 6. характеристики (для тех, у кого диаметра всё ещё нет) ----------
cur = build_items()
nodiam = {i["id"] for i in cur if not i["diam"]}
need = [c for c in raw if not (prev.get(c["id"]) and prev[c["id"]].get("specs") is not None)]
need.sort(key=lambda c: (c["id"] not in nodiam, c["unitPrice"]))
log("характеристики: всего %d, из кэша %d, грузим %d" % (len(raw), len(raw) - len(need), len(need)))
failed_total = 0
for i in range(0, len(need), 200):
    if time.time() - START > T_ALL:
        log("лимит времени на характеристики: осталось %d карточек — продолжу при следующем запуске" % (len(need) - i))
        break
    chunk = need[i:i + 200]
    bad = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for c, sp, err in ex.map(fetch_specs, chunk):
            if sp is None:
                bad += 1
            else:
                sp_map[c["id"]] = sp
    failed_total += bad
    log("  характеристики: %d / %d (ошибок в пачке %d)" % (min(i + 200, len(need)), len(need), bad))
    if bad > len(chunk) * 0.5:
        log("  много ошибок — вероятно ограничение скорости, пауза 60 с")
        time.sleep(60)
    if (i // 200) % 5 == 4:
        save("промежуточно")
log("характеристики не загрузились у %d карточек" % failed_total)
items = save("итог")
from collections import Counter
log("источники диаметра: %s" % dict(Counter(i.get("dsrc") for i in items)))
log("ГОТОВО. Без диаметра: %d из %d. Ещё без характеристик: %d" % (sum(1 for i in items if not i["diam"]), len(items), sum(1 for i in items if i["specs"] is None)))
