"""
Hlídač volných míst na anglické prohlídce Mazda Museum (Hirošima).

Projde veřejný rezervační formulář Mazdy jen po krok 2 (výběr data),
přečte kalendář a pošle push notifikaci přes ntfy, když se na hlídaný
den uvolní dost míst. Nic nerezervuje a nevyplňuje žádné osobní údaje.

Nastavení přes proměnné prostředí (v GitHubu: Settings > Secrets and variables > Actions):
  NTFY_TOPIC   název ntfy kanálu (povinné; bez něj skript nic nedělá)
"""

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- nastavení
HLIDANE_DNY = [date(2026, 11, 26)]  # 27. 11. už máme zarezervované
POCET_OSOB = 2
JAZYK = "1"  # 0 = japonština, 1 = angličtina

BASE = "https://www2.mazda.com/en/about/museum/reservations"
LOGIN_URL = BASE + "/confirm/login.html"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
JST = timezone(timedelta(hours=9))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class FlowError(Exception):
    pass


# ------------------------------------------------------------ parsování
def flow_id(html):
    m = re.search(r'name="flow_id"\s+value="([^"]+)"', html)
    if not m:
        raise FlowError("na stránce chybí flow_id")
    return m.group(1)


def aktualni_krok(html):
    soup = BeautifulSoup(html, "html.parser")
    li = soup.select_one(".mm-lst-step li.is-current .mm-num")
    return li.get_text(strip=True) if li else None


def parse_kalendar(html, mesic):
    """Vrátí {den_v_mesici: (je_volno, text_stavu)} z kroku 2."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one('[data-input-type="calendar"] table')
    if not table:
        raise FlowError("na stránce není tabulka s termíny")
    # anglická prohlídka je jen 14:15–16:15 (japonské mají jiné časy)
    hlavicka = " ".join(table.select_one("thead").get_text(" ", strip=True).split()) \
        if table.select_one("thead") else ""
    sloty = table.select("thead th")
    if len(sloty) != 1 or "14:15" not in hlavicka:
        raise FlowError(f"kalendář není pro angličtinu (časy: {hlavicka})")
    zkratka = ["Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.", "Jul.", "Aug.",
               "Sep.", "Oct.", "Nov.", "Dec."][int(mesic[4:]) - 1]
    prvni = table.select_one("tbody th")
    if not prvni or not prvni.get_text(strip=True).startswith(zkratka):
        raise FlowError(f"kalendář není pro měsíc {mesic}")

    vysledek = {}
    for tr in table.select("tbody tr"):
        th, td = tr.find("th"), tr.find("td")
        if not th or not td:
            continue
        m = re.search(r"(\d+)", th.get_text(" ", strip=True))
        if not m:
            continue
        den = int(m.group(1))
        text = " ".join(td.get_text(" ", strip=True).split())
        ma_radio = td.find("input", {"type": "radio"}) is not None
        n = re.search(r"(\d+)\s*places?\s+available", text, re.I)

        nizko = text.lower()
        if (not text or text in ("―", "-", "—", "–", "×")
                or any(k in nizko for k in ("fully booked", "closed", "not available", "unavailable"))):
            mist = 0
        elif n:
            mist = int(n.group(1))
        else:
            # neznámý stav (např. jen tlačítko k výběru) – brát jako volno
            mist = POCET_OSOB
        if ma_radio:
            mist = max(mist, POCET_OSOB)
        vysledek[den] = (mist, text or "(volno)")
    if not vysledek:
        raise FlowError("tabulka s termíny je prázdná")
    return vysledek


# ------------------------------------------------------------ průchod webem
def popis_stranky(html):
    """Krátký popis stránky pro ladění, když se průchod webem nepovede."""
    soup = BeautifulSoup(html, "html.parser")
    hid = {i.get("name"): i.get("value") for i in soup.find_all("input", {"type": "hidden"})
           if i.get("name") != "flow_id"}
    radio = [f"{i.get('name')}={i.get('value')}" for i in soup.find_all("input", {"type": "radio"})
             if i.has_attr("checked")]
    sel = [f"{x.get('name')}={o.get('value')}" for x in soup.find_all("select")
           for o in x.find_all("option") if o.has_attr("selected")]
    chyby = [li.get_text(" ", strip=True) for li in soup.select(".mm-input-error li, p.mm-input-error")
             if "display: none" not in (li.get("style") or "")]
    radky = [" ".join(tr.get_text(" ", strip=True).split())
             for tr in soup.select('[data-input-type="calendar"] tbody tr')][:2]
    return (f"krok={aktualni_krok(html)} hidden={hid} radio={radio} select={sel} "
            f"chyby={chyby[:3]} radky={radky} delka={len(html)}")


def post(session, url, data, odkud):
    r = session.post(url, data=data, timeout=30,
                     headers={"Referer": odkud, "Origin": "https://www2.mazda.com"})
    r.raise_for_status()
    return r


def nacti_kalendar(session, mesic):
    url_terms = BASE + "/new/terms.html"
    url_input1 = BASE + "/new/input1.html"
    url_sched = BASE + "/new/schedule.html"

    r = session.get(url_terms, timeout=30)
    r.raise_for_status()
    fid = flow_id(r.text)

    # krok 0: souhlas s Privacy Policy
    r = post(session, url_input1, {"flow_id": fid, "agree_terms": "同意する"}, url_terms)
    if aktualni_krok(r.text) != "1":
        raise FlowError("nepodařilo se dostat do kroku 1 | " + popis_stranky(r.text))
    fid = flow_id(r.text)

    # krok 1: bez vozíčkáře, jednotlivci / skupina do 10, počet osob
    r = post(session, url_sched, {
        "flow_id": fid,
        "wheelchair_visit": "0",
        "subscribe_type": "1",
        "numberof_visit": str(POCET_OSOB),
        "numberof_bus": "",
    }, url_input1)
    if aktualni_krok(r.text) != "2":
        raise FlowError("nepodařilo se dostat do kroku 2 | " + popis_stranky(r.text))
    fid = flow_id(r.text)

    # krok 2: jazyk + měsíc (stejné jako přepnutí výběru na stránce)
    r = post(session, url_sched, {
        "flow_id": fid,
        "guide_lang": JAZYK,
        "specified_month_1": mesic,
    }, url_sched)
    try:
        return parse_kalendar(r.text, mesic)
    except FlowError as e:
        raise FlowError(f"{e} | " + popis_stranky(r.text)) from None


# ------------------------------------------------------------ notifikace
def posli(topic, nadpis, text, priorita="high", tagy="red_car", odkaz=None):
    headers = {"Title": nadpis.encode("utf-8"), "Priority": priorita, "Tags": tagy}
    if odkaz:
        headers["Click"] = odkaz
    requests.post(f"https://ntfy.sh/{topic}", data=text.encode("utf-8"),
                  headers=headers, timeout=30)


def nacti_stav():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def uloz_stav(stav):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(stav, f, ensure_ascii=False, indent=2)


def fmt(d):
    return f"{d.day}. {d.month}."


# ------------------------------------------------------------ hlavní běh
def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC není nastavený, hlídání je vypnuté.")
        return 0

    dnes = datetime.now(JST).date()
    dny = [d for d in HLIDANE_DNY if d >= dnes]
    if not dny:
        print("Všechny hlídané dny už jsou minulost, končím.")
        return 0

    stav_pred = nacti_stav()
    stav_pred = stav_pred or {}
    prvni_beh = not stav_pred.get("start_hlasen")
    stav = {"volno": {}, "chyby": 0, "chyba_hlasena": False, "start_hlasen": True}

    session = requests.Session()
    session.headers["User-Agent"] = UA

    try:
        mesice = sorted({d.strftime("%Y%m") for d in dny})
        kalendare = {m: nacti_kalendar(session, m) for m in mesice}
    except (FlowError, requests.RequestException) as e:
        stav = dict(stav_pred, chyby=stav_pred.get("chyby", 0) + 1)
        print(f"::warning title=Kontrola selhala::{e} (po sobě {stav['chyby']}x)")
        # ohlásit až po 4 neúspěšných bězích za sebou (~1 h), a jen jednou
        if stav["chyby"] >= 4 and not stav_pred.get("chyba_hlasena"):
            posli(topic, "Hlídač Mazdy nefunguje",
                  f"Kontrola volných míst opakovaně selhává: {e}. "
                  "Mrkni do GitHub Actions, případně na web Mazdy ručně.",
                  priorita="default", tagy="warning")
            stav["chyba_hlasena"] = True
        uloz_stav(stav)
        return 0

    plne, jedno = [], []  # nové / změněné dny s dostatkem míst, resp. jen 1 místem
    for d in dny:
        mist, text = kalendare[d.strftime("%Y%m")].get(d.day, (0, "?"))
        print(f"::notice title={fmt(d)}::{text} (volných míst: {mist})")
        klic = d.isoformat()
        if mist >= 1:
            stav["volno"][klic] = text
            if stav_pred.get("volno", {}).get(klic) != text:
                (plne if mist >= POCET_OSOB else jedno).append(f"{fmt(d)}: {text}")

    if stav_pred.get("chyba_hlasena") and not prvni_beh:
        posli(topic, "Hlídač Mazdy zase běží", "Kontrola volných míst už funguje.",
              priorita="low", tagy="white_check_mark")

    if plne:
        posli(topic, f"Mazda: volná {POCET_OSOB} místa v angličtině!",
              "\n".join(plne + jedno) + "\nProhlídka 14:15–16:15. "
              "Klepni a změň rezervaci (Booking ID + heslo).",
              priorita="urgent", odkaz=LOGIN_URL)
    elif jedno:
        posli(topic, "Mazda: zatím jen 1 místo v angličtině",
              "\n".join(jedno) + "\nProhlídka 14:15–16:15. Na vás oba to zatím nestačí.",
              priorita="high", tagy="hourglass", odkaz=LOGIN_URL)
    elif prvni_beh:
        posli(topic, "Hlídač Mazdy běží",
              "Hlídám anglickou prohlídku na " + " a ".join(fmt(d) for d in dny)
              + " Ozvu se, až se uvolní místo.",
              priorita="low", tagy="eyes")

    uloz_stav(stav)
    return 0


if __name__ == "__main__":
    sys.exit(main())
