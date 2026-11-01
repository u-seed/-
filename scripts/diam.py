"""Диаметр люстры из нескольких источников. Приоритет и пометка источника (dsrc):
  spec  — поле «Диаметр» в характеристиках карточки (точно)
  photo — надпись на фото («Диаметр 80 см»), распознана OCR
  model — размер в коде модели из ссылки (ABC 1888-800 -> 80 см)
  dims  — «Длина = Ширина» в характеристиках (у круглой люстры это диаметр, ≈)
  name  — размер из названия/ссылки вида «60 см» (грубо, ≈)"""
import re

KEEP = re.compile(r"диаметр|высота|ширина|длина|цоколь|количество|кол-во|число|материал|стиль|площадь", re.I)
OK_MM = {300, 350, 400, 450, 500, 550, 600, 650, 700, 750, 800, 850, 900, 1000, 1200}
APPROX = {"dims", "name"}


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


def slug_of(url):
    try:
        return (url or "").split("/p/")[1].strip("/")
    except IndexError:
        return ""


def from_specs(specs):
    """specs: [[имя, значение], ...] -> (явный диаметр, длина==ширина)"""
    explicit = dims = None
    L = W = None
    for n, v in specs or []:
        ln = (n or "").lower()
        if explicit is None and "диаметр" in ln:
            explicit = to_cm(v)
        elif ln == "длина":
            L = to_cm(v)
        elif ln == "ширина":
            W = to_cm(v)
    if L and W and abs(L - W) < 0.5 and 10 <= L <= 200:
        dims = L
    return explicit, dims


def _model_parts(url):
    """-> (токены до размера, размер в мм) для кода модели вида «abc-1888-800», иначе None"""
    toks = [t for t in slug_of(url).split("-") if t]
    if len(toks) < 3:
        return None
    toks = toks[:-1]  # последний токен — id товара
    nums = [(i, int(t)) for i, t in enumerate(toks) if t.isdigit()]
    ok = [(i, v) for i, v in nums if v in OK_MM]
    if len(ok) != 1:
        return None
    i, v = ok[0]
    if i == 0 or not re.fullmatch(r"\d{3,5}", toks[i - 1]) or int(toks[i - 1]) in OK_MM:
        return None
    if i + 1 < len(toks) and toks[i + 1].isdigit():
        return None
    return toks[:i], v


def from_model(url):
    """код модели вида «1888-800» в ссылке: размер в мм после номера модели"""
    r = _model_parts(url)
    return r[1] / 10.0 if r else None


def family(url):
    """семейство модели: одна и та же модель в разных размерах (ljustra-abc-1888) или None"""
    r = _model_parts(url)
    return "-".join(r[0]) if r else None


def from_name(url, title, kind):
    if kind != "chandelier":
        return None
    m = re.search(r"(?:диаметр|диам\.?)\s*[:=]?\s*(\d{2,3})\s*(?:см|cm)", title or "", re.I)
    if m and 15 <= int(m.group(1)) <= 130:
        return float(m.group(1))
    m = re.search(r"-(\d{2,3})-?(?:sm|cm)(?:-|$)", slug_of(url))
    if m and 20 <= int(m.group(1)) <= 130:
        return float(m.group(1))
    return None


def from_ocr_text(txt):
    """текст с фото -> диаметр, см"""
    t = " ".join((txt or "").split())
    for rx in (r"(?:диаметр|диам)\w*\.?\s*[:=\-–]?\s*\D{0,3}?(\d{2,3})(?!\d)",
               r"[øØ⌀]\s*(\d{2,3})\s*(?:см|cm|cw|ем)?"):
        m = re.search(rx, t, re.I)
        if m and 15 <= int(m.group(1)) <= 200:
            return float(m.group(1))
    return None


def choose(kind, url, title, specs, odiam=None):
    """-> (диаметр или None, источник или None)"""
    explicit, dims = from_specs(specs)
    if explicit:
        return explicit, "spec"
    if odiam:
        return odiam, "photo"
    model = from_model(url) if kind == "chandelier" else None
    if model:
        return model, "model"
    if dims and kind == "chandelier":
        return dims, "dims"
    nm = from_name(url, title, kind)
    if nm:
        return nm, "name"
    return None, None
