#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VINTED DEAL-FINDER
==================
Sucht laufend neue Vinted-Inserate zu deinen Marken, schätzt den Marktwert
(Vinted-Vergleichsinserate + eBay "verkauft"), rechnet deinen Gewinn aus und
schickt dir nur echte Schnäppchen als Push aufs Handy (ntfy).
Zusätzlich läuft eine kleine Web-App (Dashboard), die du dir aufs Handy legen kannst.

Schnellstart
------------
  pip install requests pillow
  1) ntfy-App installieren, Topic abonnieren, unten NTFY_TOPIC eintragen
  2) SEARCHES unten anpassen
  3) python vinted_sniper.py --test     (prüft alles einmal)
  4) python vinted_sniper.py            (läuft dauerhaft)
"""

import argparse
import io
import json
import logging
import os
import random
import re
import socket
import sqlite3
import statistics
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlparse

import requests

# =============================== KONFIG ===================================

# --- Benachrichtigung -------------------------------------------------------
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "vinted-deals-AENDERE-MICH-123xyz"   # lang & zufällig wählen, sonst kann jeder mitlesen
NTFY_SERVER = "https://ntfy.sh"

# --- Vinted -----------------------------------------------------------------
DOMAIN = "www.vinted.de"          # vinted.at, vinted.fr, vinted.nl ...
INTERVAL = 120                    # Sekunden zwischen den Durchläufen (nicht zu klein!)
PROXY = ""                        # optional, z. B. "http://user:pass@host:port"

# --- Dashboard (die "App" fürs Handy) --------------------------------------
DASHBOARD_PORT = 0 if os.environ.get("GITHUB_ACTIONS") else 8080   # 0 = Dashboard aus (automatisch aus bei GitHub Actions)
DASHBOARD_KEY = ""                # optionales Passwort: dann Aufruf mit  /?key=DEIN_KEY

# --- Gewinn-Rechnung (Näherungswerte, bitte bei Gelegenheit gegenchecken!) ---
BUYER_FEE_FIXED = 0.70            # Vinted Käuferschutz: Festbetrag
BUYER_FEE_PERCENT = 5.0           # ... plus Prozent vom Artikelpreis
SHIPPING_COST = 4.50              # was du beim Einkauf ca. für Versand zahlst
RESELL_FEE_PERCENT = 0.0          # Gebühren beim Wiederverkauf (Vinted-Verkäufer: 0 %)

MIN_PROFIT = 12.0                 # mind. so viel € Gewinn pro Teil
MIN_DISCOUNT = 0.30               # Einkauf mind. 30 % unter Marktwert
MIN_PRICE = 3.0                   # alles darunter ignorieren (Fake-/Spaßpreise)

# --- Marktwert --------------------------------------------------------------
USE_EBAY = True                   # eBay "Verkaufte Artikel" als zweite Quelle (best effort)
VINTED_ASK_FACTOR = 0.85          # Vinted-Angebotspreise sind höher als echte Verkaufspreise
MIN_COMPS = 6                     # mind. so viele Vergleichspreise, sonst kein Marktwert
COMP_CACHE_HOURS = 6              # Marktwerte so lange zwischenspeichern
NOTIFY_WITHOUT_MARKET_VALUE = True  # ohne Marktwert trotzdem melden, wenn Preis <= max_price

# --- "Schlechte Inserate" ---------------------------------------------------
# Bei >= 2 Auffälligkeiten (schlechtes Foto, kurzer Titel, Tippfehler ...) wird dein
# Gewinn-Limit um 30 % gesenkt, weil solche Teile oft unter Wert verkauft werden.
ANALYZE_PHOTOS = True             # braucht "pip install pillow"
DARK_THRESHOLD = 70               # mittlere Helligkeit (0-255) darunter = "zu dunkel"
BLUR_THRESHOLD = 25               # Kantenstärke darunter = "unscharf" (grober Wert)

# Wörter, bei denen ein Inserat sofort rausfliegt
GLOBAL_EXCLUDE = [
    "fake", "replica", "replika", "kopie", "inspired", "ähnlich", "stil wie",
    "kinder", "kids", "baby", "jungen", "boys", "girls", "mädchen",
    "defekt", "kaputt", "löcher", "flecken",
]

# --- Deine Suchen -----------------------------------------------------------
# queries        Suchbegriffe (inkl. Tippfehler-Varianten!), jeder wird einzeln abgefragt
# brands         Marken-Stichwörter: Marke ODER Titel muss eins davon enthalten
# must_contain_any  (optional) Titel muss eins dieser Wörter enthalten
# exclude        (optional) zusätzliche Ausschlusswörter
# typos          Schreibweisen, die als "Seller kennt sich nicht aus" zählen
# comp_query     Suchbegriff für den Marktwert (Standard: der jeweilige Suchbegriff)
# max_price      absolute Preisgrenze ("billig")
# min_profit / min_discount   (optional) überschreibt die globalen Werte
# url            (optional) statt queries: kopiere einen Vinted-Such-Link mit
#                gesetzten Filtern (Größe, Marke, Zustand ...) hier rein
# enabled        False = Suche pausieren
SEARCHES = [
    {
        "name": "Levi's Jeans",
        "queries": ["levis 501", "levi's jeans", "lewis jeans"],
        "comp_query": "levis 501 jeans",
        "brands": ["levi", "lewis"],
        "typos": ["lewis", "levies", "livis"],
        "max_price": 30,
    },
    {
        "name": "Diesel Jeans",
        "queries": ["diesel jeans"],
        "brands": ["diesel"],
        "max_price": 35,
    },
    {
        "name": "G-Star Jeans",
        "queries": ["g-star raw jeans"],
        "brands": ["g-star", "gstar", "g star"],
        "max_price": 30,
    },
    {
        "name": "Ralph Lauren Cable Knit",
        "queries": ["ralph lauren cable knit", "ralph lauren zopfmuster", "ralf lauren cable knit"],
        "comp_query": "polo ralph lauren cable knit pullover",
        "brands": ["ralph lauren", "polo", "ralf lauren"],
        "must_contain_any": ["cable", "zopf", "kabel", "aran", "fisherman"],
        "typos": ["ralf lauren", "ralp lauren", "polo lauren"],
        "max_price": 45,
    },
    {
        "name": "Carhartt Jacke",
        "queries": ["carhartt jacke"],
        "brands": ["carhartt"],
        "typos": ["carhart "],
        "max_price": 60,
    },
    {
        "name": "Patagonia Fleece",
        "queries": ["patagonia fleece"],
        "brands": ["patagonia"],
        "max_price": 45,
    },
    {
        "name": "Stone Island",
        "queries": ["stone island pullover"],
        "brands": ["stone island"],
        "max_price": 80,
        "min_profit": 25,
    },
]

# ============================ AB HIER NICHTS ÄNDERN =======================

BASE = Path(__file__).resolve().parent
DB_FILE = BASE / "vinted.db"
INF = float("inf")
LOG = logging.getLogger("vinted")
STATS = {"last_scan": None, "scans": 0, "checked": 0, "finds": 0, "status": "startet"}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


# ------------------------------- Datenbank ---------------------------------

@contextmanager
def conn():
    con = sqlite3.connect(DB_FILE, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, ts INTEGER);
            CREATE TABLE IF NOT EXISTS seeded (label TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS finds (
                id TEXT PRIMARY KEY, ts INTEGER, search TEXT, title TEXT, brand TEXT,
                size TEXT, price REAL, market REAL, profit REAL, discount REAL,
                score REAL, reasons TEXT, source TEXT, url TEXT, photo TEXT);
            """
        )


def is_seen(iid):
    with conn() as c:
        return c.execute("SELECT 1 FROM seen WHERE id=?", (iid,)).fetchone() is not None


def mark_seen(iid):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (iid, int(time.time())))


def is_seeded(label):
    with conn() as c:
        return c.execute("SELECT 1 FROM seeded WHERE label=?", (label,)).fetchone() is not None


def mark_seeded(label):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO seeded VALUES (?)", (label,))


def save_find(f):
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO finds VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["id"], f["ts"], f["search"], f["title"], f["brand"], f["size"], f["price"],
             f["market"], f["profit"], f["discount"], f["score"], json.dumps(f["reasons"], ensure_ascii=False),
             f["source"], f["url"], f["photo"]),
        )


def cleanup_db():
    cutoff = int(time.time()) - 14 * 86400
    with conn() as c:
        c.execute("DELETE FROM seen WHERE ts < ?", (cutoff,))
        c.execute("DELETE FROM finds WHERE ts < ?", (cutoff,))


# ------------------------------ Vinted-Zugriff -----------------------------

class Blocked(Exception):
    """Vinted blockt uns (gerade) oder liefert keine brauchbare Antwort."""


class Vinted:
    def __init__(self):
        self.s = None
        self.refresh()

    def refresh(self):
        s = requests.Session()
        s.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        })
        if PROXY:
            s.proxies = {"http": PROXY, "https": PROXY}
        s.get(f"https://{DOMAIN}/", timeout=20)   # holt die nötigen Cookies
        self.s = s

    def get_json(self, path, params=None):
        for attempt in range(4):
            r = self.s.get(f"https://{DOMAIN}{path}", params=params, timeout=20)
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    raise Blocked("Antwort war kein JSON (vermutlich Bot-Schutz)")
            if r.status_code in (401, 403):
                LOG.warning("Vinted antwortet %s – hole neue Cookies (Versuch %s)", r.status_code, attempt + 1)
                time.sleep(5 * (attempt + 1))
                self.refresh()
                continue
            if r.status_code == 429:
                wait = min(int(r.headers.get("Retry-After", 60) or 60), 300)
                LOG.warning("Zu viele Anfragen (429) – warte %ss", wait)
                time.sleep(wait)
                continue
            if r.status_code == 404:
                return {}
            r.raise_for_status()
        raise Blocked("Vinted blockt nach mehreren Versuchen")

    def fetch_detail(self, item_id):
        try:
            return (self.get_json(f"/api/v2/items/{item_id}") or {}).get("item") or {}
        except Exception as e:   # Details sind nur "nice to have"
            LOG.debug("Detail-Abruf fehlgeschlagen: %s", e)
            return {}


# ------------------------------ Hilfsfunktionen ----------------------------

def parse_price(item):
    p = item.get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    try:
        return float(str(p).replace(",", "."))
    except (TypeError, ValueError):
        return INF


def item_url(item):
    u = item.get("url") or f"/items/{item['id']}"
    return f"https://{DOMAIN}{u}" if u.startswith("/") else u


def robust_median(prices):
    if len(prices) < 3:
        return None
    m = statistics.median(prices)
    kept = [p for p in prices if 0.4 * m <= p <= 2.5 * m]   # Ausreißer raus
    return statistics.median(kept) if len(kept) >= 3 else m


def passes_filters(item, search):
    title = (item.get("title") or "").lower()
    brand = (item.get("brand_title") or "").lower()
    if any(w in title for w in GLOBAL_EXCLUDE + [w.lower() for w in search.get("exclude", [])]):
        return False
    brands = [b.lower() for b in search.get("brands", [])]
    if brands and not any(b in brand or b in title for b in brands):
        return False
    must = [w.lower() for w in search.get("must_contain_any", [])]
    if must and not any(w in title for w in must):
        return False
    return True


def calc_profit(price, market):
    cost = price + BUYER_FEE_FIXED + price * BUYER_FEE_PERCENT / 100 + SHIPPING_COST
    revenue = market * (1 - RESELL_FEE_PERCENT / 100)
    return revenue - cost, cost


# --------------------------------- Marktwert -------------------------------

EBAY_RE = re.compile(
    r"(?:s-item__price|s-card__price)[^>]*>(?:\s*<[^>]+>)*\s*EUR(?:\s|&nbsp;|\xa0)*([\d.]+,\d{2})"
)


def ebay_sold_prices(query):
    """Preise verkaufter eBay-Artikel. Best effort: eBay ändert sein HTML gelegentlich."""
    r = requests.get(
        "https://www.ebay.de/sch/i.html",
        params={"_nkw": query, "LH_Sold": "1", "LH_Complete": "1", "_ipg": "60"},
        headers={"User-Agent": random.choice(USER_AGENTS), "Accept-Language": "de-DE,de;q=0.9"},
        timeout=20,
    )
    r.raise_for_status()
    return [float(m.replace(".", "").replace(",", ".")) for m in EBAY_RE.findall(r.text)]


_COMPS = {}


def get_market_value(v, search, cq):
    now = time.time()
    cached = _COMPS.get(cq)
    if cached and now - cached["t"] < cached["ttl"]:
        return cached

    values, parts = [], []

    try:
        data = v.get_json("/api/v2/catalog/items",
                          [("search_text", cq), ("per_page", 48), ("page", 1), ("order", "relevance")])
        prices = []
        for i in data.get("items", []):
            p = parse_price(i)
            if MIN_PRICE <= p < INF and passes_filters(i, search):
                prices.append(p)
        m = robust_median(prices)
        if m and len(prices) >= MIN_COMPS:
            val = m * VINTED_ASK_FACTOR
            values.append(val)
            parts.append(f"Vinted {val:.0f} € ({len(prices)} Inserate)")
    except Exception as e:
        LOG.warning("Vinted-Vergleich für '%s' fehlgeschlagen: %s", cq, e)

    if USE_EBAY:
        try:
            prices = [p for p in ebay_sold_prices(cq) if p >= MIN_PRICE]
            m = robust_median(prices)
            if m and len(prices) >= MIN_COMPS:
                values.append(m)
                parts.append(f"eBay verkauft {m:.0f} € ({len(prices)})")
        except Exception as e:
            LOG.info("eBay-Vergleich für '%s' nicht verfügbar: %s", cq, e)

    if values:
        info = {"value": min(values), "source": " | ".join(parts), "t": now,
                "ttl": COMP_CACHE_HOURS * 3600}   # vorsichtig: immer die niedrigere Quelle
    elif cached and cached.get("value"):
        info = dict(cached, t=now, ttl=900)          # alten Wert noch eine Weile nutzen
    else:
        info = {"value": None, "source": "kein Marktwert", "t": now, "ttl": 600}
    _COMPS[cq] = info
    LOG.info("Marktwert '%s': %s", cq, f"{info['value']:.0f} € – {info['source']}" if info["value"] else "keiner")
    return info


# ------------------------- Auffälligkeiten im Inserat ----------------------

def listing_signals(item, search, detail=None):
    out = []
    title = item.get("title") or ""
    tl = title.lower()
    if len(title.split()) <= 2:
        out.append("sehr kurzer Titel")
    brands = [b.lower() for b in search.get("brands", [])]
    if brands and not any(b in tl for b in brands):
        out.append("Marke nicht im Titel")
    if any(t.lower() in tl for t in search.get("typos", [])):
        out.append("Tippfehler im Titel")
    photo = item.get("photo") or {}
    w, h = photo.get("width"), photo.get("height")
    if w and h and min(w, h) < 500:
        out.append("Foto niedrig aufgelöst")
    if detail:
        n = len(detail.get("photos") or [])
        if 0 < n <= 2:
            out.append(f"nur {n} Foto{'s' if n > 1 else ''}")
        if len((detail.get("description") or "").strip()) < 15:
            out.append("kaum Beschreibung")
    return out


def photo_quality(url):
    """Grobe Foto-Analyse: zu dunkel / überbelichtet / unscharf."""
    if not ANALYZE_PHOTOS or not url:
        return []
    try:
        from PIL import Image, ImageFilter, ImageStat
    except ImportError:
        return []
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENTS[0]}, timeout=15)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("L")
        im.thumbnail((500, 500))
        mean = ImageStat.Stat(im).mean[0]
        edge_var = ImageStat.Stat(im.filter(ImageFilter.FIND_EDGES)).var[0]
    except Exception as e:
        LOG.debug("Foto-Analyse fehlgeschlagen: %s", e)
        return []
    out = []
    if mean < DARK_THRESHOLD:
        out.append("Foto sehr dunkel")
    elif mean > 215:
        out.append("Foto überbelichtet")
    if edge_var < BLUR_THRESHOLD:
        out.append("Foto unscharf")
    return out


# ------------------------------- Benachrichtigung --------------------------

def notify(f):
    head = f"{f['price']:.0f} €"
    if f["profit"] is not None:
        head = f"+{f['profit']:.0f} € Gewinn ({head})"
    lines = [f["title"], " · ".join(x for x in (f["brand"], f["size"]) if x)]
    if f["market"]:
        lines.append(f"Marktwert ca. {f['market']:.0f} € (-{f['discount'] * 100:.0f} %)")
    if f["reasons"]:
        lines.append("Auffällig: " + ", ".join(f["reasons"]))
    payload = {
        "topic": NTFY_TOPIC,
        "title": f"{head} · {f['search']}",
        "message": "\n".join(x for x in lines if x),
        "click": f["url"],
        "tags": ["moneybag" if f["profit"] else "shopping_bags"],
        "priority": 5 if f["score"] >= 50 else 4 if f["score"] >= 25 else 3,
        "actions": [{"action": "view", "label": "Auf Vinted öffnen", "url": f["url"]}],
    }
    if f["photo"]:
        payload["attach"] = f["photo"]
    try:
        requests.post(NTFY_SERVER, json=payload, timeout=15).raise_for_status()
    except Exception as e:
        LOG.error("Push fehlgeschlagen: %s", e)


# ------------------------------- Kernlogik ---------------------------------

def process_item(v, search, item, cq, push):
    price = parse_price(item)
    if not (MIN_PRICE <= price < INF):
        return
    if search.get("max_price") and price > search["max_price"]:
        return
    if not passes_filters(item, search):
        return

    min_profit = search.get("min_profit", MIN_PROFIT)
    min_disc = search.get("min_discount", MIN_DISCOUNT)
    mv = get_market_value(v, search, cq)
    photo_url = (item.get("photo") or {}).get("url")
    signals = listing_signals(item, search)
    profit = discount = None

    if mv["value"]:
        profit, _ = calc_profit(price, mv["value"])
        discount = 1 - price / mv["value"]
        if profit < min_profit * 0.6:          # selbst mit Bonus aussichtslos
            return
        detail = v.fetch_detail(item["id"])
        signals = listing_signals(item, search, detail) + photo_quality(photo_url)
        bonus = len(signals) >= 2
        if profit < min_profit * (0.7 if bonus else 1.0):
            return
        if discount < min_disc * (0.8 if bonus else 1.0):
            return
    elif not NOTIFY_WITHOUT_MARKET_VALUE:
        return

    find = {
        "id": str(item["id"]), "ts": int(time.time()), "search": search["name"],
        "title": item.get("title") or "", "brand": item.get("brand_title") or "",
        "size": item.get("size_title") or "", "price": price, "market": mv["value"],
        "profit": profit, "discount": discount,
        "score": (profit or 0) + 5 * len(signals),
        "reasons": signals, "source": mv["source"], "url": item_url(item), "photo": photo_url or "",
    }
    save_find(find)
    STATS["finds"] += 1
    LOG.info("TREFFER: %s – %.0f € (Gewinn %s)", find["title"], price,
             f"{profit:.0f} €" if profit is not None else "?")
    if push:
        notify(find)


def build_params(search, query):
    params = []
    if search.get("url"):
        skip = {"page", "time", "search_id", "order", "per_page", "disabled_personalization",
                "search_by_image_uuid"}
        if search.get("max_price"):
            skip.add("price_to")
        params = [(k, val) for k, val in parse_qsl(urlparse(search["url"]).query) if k not in skip]
    else:
        params.append(("search_text", query))
        for sid in search.get("size_ids", []):
            params.append(("size_ids[]", sid))
    params += [("order", "newest_first"), ("per_page", 30), ("page", 1)]
    if search.get("max_price") and "price_to" not in dict(params):
        params.append(("price_to", search["max_price"]))
    return params


def search_runs(search):
    """Liefert (label, params, comp_query) für jede einzelne Abfrage."""
    if search.get("url"):
        st = dict(parse_qsl(urlparse(search["url"]).query)).get("search_text") or search["name"]
        yield search["url"], build_params(search, None), search.get("comp_query") or st
    else:
        for q in search["queries"]:
            yield q, build_params(search, q), search.get("comp_query") or q


def scan_once(v):
    STATS["status"] = "scannt"
    for search in SEARCHES:
        if search.get("enabled", True) is False:
            continue
        for label, params, cq in search_runs(search):
            data = v.get_json("/api/v2/catalog/items", params)
            items = data.get("items", [])
            full_label = f"{search['name']}|{label}"
            seeded = is_seeded(full_label)   # erster Lauf: nur speichern, nicht pushen
            for item in items:
                iid = str(item.get("id"))
                if not item.get("id") or is_seen(iid):
                    continue
                mark_seen(iid)
                STATS["checked"] += 1
                try:
                    process_item(v, search, item, cq, push=seeded)
                except Blocked:
                    raise
                except Exception:
                    LOG.exception("Fehler bei Inserat %s", iid)
            if not seeded:
                mark_seeded(full_label)
                LOG.info("Erster Durchlauf '%s' – bisherige Inserate ohne Push erfasst", full_label)
            time.sleep(random.uniform(2, 5))
    STATS["scans"] += 1
    STATS["last_scan"] = datetime.now().strftime("%H:%M:%S")
    STATS["status"] = "ok"


# ------------------------------- Dashboard ---------------------------------

ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" '
    'fill="#16a34a"/><text x="50" y="66" font-size="52" text-anchor="middle" fill="#fff" '
    'font-family="Arial" font-weight="700">€</text></svg>'
)

PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="refresh" content="60">
<link rel="manifest" href="/manifest.json"><link rel="icon" href="/icon.svg">
<meta name="theme-color" content="#16a34a"><meta name="apple-mobile-web-app-capable" content="yes">
<title>Vinted Deals</title><style>
:root{--bg:#f4f4f5;--card:#fff;--tx:#18181b;--mut:#71717a;--ok:#15803d;--chip:#eef2ff;--chiptx:#4338ca}
@media(prefers-color-scheme:dark){:root{--bg:#111113;--card:#1c1c1f;--tx:#f4f4f5;--mut:#a1a1aa;--ok:#4ade80;--chip:#27272a;--chiptx:#c7d2fe}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.35 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;
padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
header{padding:14px 14px 6px}h1{margin:0;font-size:20px}.st{color:var(--mut);font-size:12px;margin-top:2px}
nav{display:flex;gap:8px;padding:8px 14px}nav a{padding:6px 12px;border-radius:99px;background:var(--card);color:var(--tx);text-decoration:none;font-size:13px}
nav a.on{background:var(--ok);color:#fff}
main{padding:6px 14px 24px;display:grid;gap:10px;max-width:640px;margin:auto}
.card{display:flex;gap:12px;background:var(--card);border-radius:14px;padding:10px;color:inherit;text-decoration:none}
.card img{width:84px;height:112px;object-fit:cover;border-radius:10px;background:var(--bg);flex:none}
.b{min-width:0;flex:1}.t{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.m{color:var(--mut);font-size:12px;margin:2px 0 6px}.p{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.p b{font-size:20px}.pf{color:var(--ok);font-weight:700}.mv{color:var(--mut);font-size:12px}
.chips{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}.chips span{background:var(--chip);color:var(--chiptx);border-radius:99px;padding:2px 8px;font-size:11px}
.ago{color:var(--mut);font-size:11px;margin-top:6px}.empty{text-align:center;color:var(--mut);padding:40px 10px}
</style></head><body><header><h1>Vinted Deals</h1><div class="st">__STATUS__</div></header>
<nav><a class="__N1__" href="/?sort=new__KEY__">Neueste</a><a class="__N2__" href="/?sort=profit__KEY__">Bester Gewinn</a></nav>
<main>__ROWS__</main></body></html>"""


def time_ago(ts):
    m = max(0, int((time.time() - ts) / 60))
    if m < 1:
        return "gerade eben"
    if m < 60:
        return f"vor {m} Min"
    if m < 1440:
        return f"vor {m // 60} Std"
    return f"vor {m // 1440} Tg"


def render_dashboard(query):
    by_profit = query.get("sort") == "profit"
    with conn() as c:
        rows = c.execute(
            f"SELECT * FROM finds ORDER BY {'score' if by_profit else 'ts'} DESC LIMIT 100").fetchall()
    cards = []
    for r in rows:
        chips = "".join(f"<span>{escape(x)}</span>" for x in json.loads(r["reasons"] or "[]"))
        market = f'<span class="mv">Markt ~{r["market"]:.0f} €</span>' if r["market"] else ""
        profit = f'<span class="pf">+{r["profit"]:.0f} €</span>' if r["profit"] is not None else ""
        meta = " · ".join(escape(x) for x in (r["brand"], r["size"], r["search"]) if x)
        cards.append(
            f'<a class="card" href="{escape(r["url"])}" target="_blank" rel="noopener">'
            f'<img src="{escape(r["photo"] or "")}" loading="lazy" alt="">'
            f'<div class="b"><div class="t">{escape(r["title"])}</div><div class="m">{meta}</div>'
            f'<div class="p"><b>{r["price"]:.0f} €</b>{profit}{market}</div>'
            f'<div class="chips">{chips}</div><div class="ago">{time_ago(r["ts"])}</div></div></a>')
    body = "".join(cards) or '<div class="empty">Noch keine Treffer. Der Finder sucht im Hintergrund …</div>'
    status = (f"Status: {escape(STATS['status'])} · letzter Scan: {STATS['last_scan'] or '–'} · "
              f"Durchläufe: {STATS['scans']} · geprüft: {STATS['checked']} · Treffer: {STATS['finds']}")
    key = f"&key={quote(DASHBOARD_KEY)}" if DASHBOARD_KEY else ""
    return (PAGE.replace("__STATUS__", status).replace("__ROWS__", body).replace("__KEY__", key)
            .replace("__N1__", "" if by_profit else "on").replace("__N2__", "on" if by_profit else ""))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = dict(parse_qsl(u.query))
        key = f"/?key={quote(DASHBOARD_KEY)}" if DASHBOARD_KEY else "/"
        if u.path == "/icon.svg":
            return self._send(200, ICON_SVG, "image/svg+xml")
        if u.path == "/manifest.json":
            return self._send(200, json.dumps({
                "name": "Vinted Deals", "short_name": "Deals", "start_url": key, "display": "standalone",
                "background_color": "#111113", "theme_color": "#16a34a",
                "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}),
                "application/manifest+json")
        if DASHBOARD_KEY and q.get("key") != DASHBOARD_KEY:
            return self._send(403, "Zugriff verweigert (Passwort fehlt: /?key=...)")
        if u.path == "/api/finds":
            with conn() as c:
                rows = [dict(r) for r in c.execute("SELECT * FROM finds ORDER BY ts DESC LIMIT 100")]
            return self._send(200, json.dumps(rows, ensure_ascii=False), "application/json")
        if u.path == "/":
            return self._send(200, render_dashboard(q))
        self._send(404, "nicht gefunden")


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def start_dashboard():
    if not DASHBOARD_PORT:
        return None
    server = ThreadingHTTPServer(("0.0.0.0", DASHBOARD_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    key = f"/?key={DASHBOARD_KEY}" if DASHBOARD_KEY else ""
    LOG.info("Dashboard: http://%s:%s%s", local_ip(), DASHBOARD_PORT, key)
    return server


# ----------------------------------- Start ---------------------------------

def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    LOG.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    LOG.addHandler(sh)
    fh = RotatingFileHandler(BASE / "sniper.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    LOG.addHandler(fh)


def notify_blocked_once(reason):
    """Max. 1x pro Tag eine Warnung aufs Handy, wenn Vinted uns blockt."""
    label = "blocked-" + datetime.now().strftime("%Y-%m-%d")
    if is_seeded(label):
        return
    mark_seeded(label)
    try:
        requests.post(NTFY_SERVER, json={
            "topic": NTFY_TOPIC, "title": "Vinted-Finder wird geblockt", "tags": ["warning"],
            "message": f"{reason}\nVinted lässt diese Verbindung (IP) gerade nicht durch."}, timeout=15)
    except Exception:
        pass


def connect_forever(once=False):
    while True:
        try:
            return Vinted()
        except requests.RequestException as e:
            if once:
                raise
            LOG.warning("Keine Verbindung zu Vinted (%s) – neuer Versuch in 60 s", e)
            time.sleep(60)


def run_test():
    print("1) Test-Push wird gesendet …")
    try:
        requests.post(NTFY_SERVER, json={"topic": NTFY_TOPIC, "title": "Vinted Deal-Finder",
                                         "message": "Test erfolgreich – so sehen deine Alarme aus.",
                                         "tags": ["white_check_mark"]}, timeout=15).raise_for_status()
        print("   OK – ist die Nachricht auf dem Handy angekommen?")
    except Exception as e:
        print("   FEHLER:", e)
    print("2) Vinted-Abfragen …")
    try:
        v = Vinted()
    except Exception as e:
        print("   Verbindung fehlgeschlagen:", e)
        return
    for s in SEARCHES:
        for label, params, cq in search_runs(s):
            try:
                n = len(v.get_json("/api/v2/catalog/items", params).get("items", []))
                mv = get_market_value(v, s, cq)
                mtxt = f"Marktwert {mv['value']:.0f} € ({mv['source']})" if mv["value"] else "kein Marktwert"
                print(f"   {s['name']} / {label[:40]}: {n} Inserate, {mtxt}")
            except Exception as e:
                print(f"   {s['name']} / {label[:40]}: FEHLER {e}")
            time.sleep(random.uniform(2, 4))
    print("Fertig. Wenn überall Inserate ankommen, kannst du ohne --test starten.")


def main():
    ap = argparse.ArgumentParser(description="Vinted Deal-Finder")
    ap.add_argument("--test", action="store_true", help="Alles einmal prüfen und beenden")
    ap.add_argument("--once", action="store_true", help="Nur einen Durchlauf machen")
    args = ap.parse_args()

    setup_logging()
    if args.test:
        return run_test()

    init_db()
    start_dashboard()
    pause = (lambda s: None) if args.once else time.sleep   # im --once-Modus nie warten
    try:
        v = connect_forever(args.once)
    except requests.RequestException as e:
        LOG.warning("Keine Verbindung zu Vinted: %s", e)
        return
    LOG.info("Läuft. Strg+C zum Beenden." if not args.once else "Einzelner Durchlauf …")
    last_cleanup = 0
    while True:
        try:
            scan_once(v)
        except Blocked as e:
            STATS["status"] = "geblockt – pausiert"
            LOG.warning("%s", e)
            notify_blocked_once(str(e))
            pause(300)
            try:
                v.refresh()
            except Exception:
                pass
        except requests.RequestException as e:
            STATS["status"] = "Netzwerkfehler"
            LOG.warning("Netzwerkfehler: %s – Pause 60 s", e)
            pause(60)
        except Exception:
            STATS["status"] = "Fehler"
            LOG.exception("Unerwarteter Fehler")
            pause(30)
        if time.time() - last_cleanup > 86400:
            cleanup_db()
            last_cleanup = time.time()
        if args.once:
            break
        time.sleep(INTERVAL + random.uniform(0, INTERVAL * 0.25))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBeendet.")
