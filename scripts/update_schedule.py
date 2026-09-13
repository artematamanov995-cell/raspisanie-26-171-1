#!/usr/bin/env python3
"""
Обновляет index.html — расписание группы 26-171-1 с сайта nsuada.ru.
Всегда берёт последнюю (самую позднюю по номеру) доступную учебную неделю.

Если получить или разобрать данные не удалось — скрипт завершается с
ошибкой и НЕ трогает index.html, чтобы сайт не сломался из-за
временного сбоя источника.
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://nsuada.ru/obuchayushchemusya/raspisanie/zanyatiy/"
GROUP = "26-171-1"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "index.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

MONTHS_GEN = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


# ---------- сеть ----------

def fetch(session, params):
    resp = session.post(URL, data=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    return resp.text


def get_latest_week(session):
    html = fetch(session, {"TYP_RASP": "stud", "GROUP": GROUP})
    weeks = extract_week_options(html)
    if not weeks:
        raise RuntimeError("Список доступных недель пуст.")
    return max(weeks)


def extract_week_options(html):
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", {"name": "PARA_WEEK"})
    if not select:
        raise RuntimeError("Не найден список недель (PARA_WEEK) на странице сайта.")
    weeks = []
    for opt in select.find_all("option"):
        val = (opt.get("value") or "").strip()
        if val.isdigit():
            weeks.append(int(val))
    return weeks


# ---------- разбор расписания ----------

def cell_kind(td):
    """пр./л./без префикса — определяем по тексту перед первым <font>."""
    small = td.find("small")
    if small is None:
        return None
    for node in small.contents:
        if isinstance(node, str):
            text = node.replace("\xa0", " ").strip()
            if text:
                if text.startswith("л"):
                    return "lecture"
                if text.startswith("пр"):
                    return "practice"
                return "other"
        else:
            break
    return None


def parse_cell(td):
    names = [f.get_text(strip=True) for f in td.find_all("font", attrs={"color": "blue"})]
    if not names:
        return None
    rooms = [b.get_text(strip=True) for b in td.find_all("b")]
    teachers = [f.get_text(strip=True) for f in td.find_all("font", attrs={"color": "green"})]
    n = max(len(names), len(rooms), len(teachers))
    sessions = []
    for i in range(n):
        sessions.append({
            "name": names[i] if i < len(names) else names[-1],
            "room": rooms[i] if i < len(rooms) else "",
            "teacher": teachers[i] if i < len(teachers) else "",
        })
    grouped = []
    for s in sessions:
        if grouped and grouped[-1]["name"] == s["name"]:
            grouped[-1]["entries"].append({"room": s["room"], "teacher": s["teacher"]})
        else:
            grouped.append({"name": s["name"], "entries": [{"room": s["room"], "teacher": s["teacher"]}]})
    return {"kind": cell_kind(td), "subjects": grouped}


def parse_schedule(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="table-striped")
    if not table:
        raise RuntimeError("Не найдена таблица расписания (table-striped) в ответе сайта.")
    rows = table.find_all("tr")
    if not rows:
        raise RuntimeError("Таблица расписания пуста.")

    header_cells = rows[0].find_all("td")
    days = []
    for cell in header_cells:
        b = cell.find("b")
        small = cell.find("small")
        name = b.get_text(strip=True) if b else cell.get_text(strip=True)
        date_text = small.get_text(strip=True) if small else ""
        iso_date = ""
        date_label = date_text
        m = re.match(r"(\d{1,2})-(\d{1,2})-(\d{4})", date_text)
        if m:
            dd, mm, yyyy = m.groups()
            iso_date = f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
            date_label = f"{int(dd)} {MONTHS_GEN.get(int(mm), mm)}"
        days.append({
            "name": name,
            "date": iso_date,
            "dateLabel": date_label,
            "lessons": [],
        })

    for row in rows[1:]:
        th = row.find("th")
        if th is None:
            continue
        time_label = th.get_text(strip=True)
        if "-" not in time_label:
            continue
        start, end = [p.strip() for p in time_label.split("-", 1)]
        tds = row.find_all("td")
        for i, td in enumerate(tds):
            if i >= len(days):
                break
            parsed = parse_cell(td)
            if parsed is None:
                continue
            days[i]["lessons"].append({
                "start": start,
                "end": end,
                "kind": parsed["kind"],
                "subjects": parsed["subjects"],
            })

    # склеиваем подряд идущие пары одного и того же занятия (напр. лекция на 2 пары)
    for day in days:
        merged = []
        for lesson in day["lessons"]:
            if (merged
                    and merged[-1]["kind"] == lesson["kind"]
                    and merged[-1]["subjects"] == lesson["subjects"]):
                merged[-1]["end"] = lesson["end"]
            else:
                merged.append(dict(lesson))
        day["lessons"] = merged

    return [d for d in days if d["lessons"]]


def week_range_label(days):
    if not days:
        return ""
    first, last = days[0], days[-1]
    fm = re.match(r"(\d+) (\S+)", first["dateLabel"])
    lm = re.match(r"(\d+) (\S+)", last["dateLabel"])
    year = first["date"].split("-")[0] if first["date"] else ""
    if fm and lm:
        if fm.group(2) == lm.group(2):
            return f"{fm.group(1)}\u2013{lm.group(1)} {lm.group(2)} {year}"
        return f"{fm.group(1)} {fm.group(2)} \u2013 {lm.group(1)} {lm.group(2)} {year}"
    return f"{first['dateLabel']} \u2013 {last['dateLabel']}"


# ---------- рендер ----------

TEMPLATE = Path(__file__).with_name("template.html").read_text(encoding="utf-8")


def render(days, week_range, generated_label):
    html = TEMPLATE
    html = html.replace("__GROUP__", GROUP)
    html = html.replace("__WEEK_RANGE__", week_range)
    html = html.replace("__WEEK_JSON__", json.dumps(days, ensure_ascii=False, indent=2))
    html = html.replace("__GENERATED__", generated_label)
    return html


def main():
    session = requests.Session()
    latest_week = get_latest_week(session)
    html = fetch(session, {"TYP_RASP": "stud", "GROUP": GROUP, "PARA_WEEK": str(latest_week)})
    days = parse_schedule(html)
    if not days:
        raise RuntimeError("После разбора не осталось ни одного дня с занятиями — похоже, сайт изменил формат.")

    week_range = week_range_label(days)
    nsk_now = datetime.now(timezone.utc) + timedelta(hours=7)
    generated_label = nsk_now.strftime("%d.%m.%Y %H:%M") + " (Новосибирск)"

    output = render(days, week_range, generated_label)
    OUTPUT_PATH.write_text(output, encoding="utf-8")
    print(f"OK: неделя {latest_week}, дней с занятиями: {len(days)} -> {OUTPUT_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Ошибка обновления расписания: {exc}", file=sys.stderr)
        sys.exit(1)
