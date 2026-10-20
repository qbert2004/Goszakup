#!/usr/bin/env python3
"""Разовая аналитика по открытым данным goszakup (две связанные выгрузки).

Этап 1 — КЛИЕНТЫ: заказчики, у кого в 2026 есть завершённая (состоявшаяся)
         закупка по ИТ или ИБ на сумму > 20 млн тг. По всему Казахстану.
Этап 2 — ПОСТАВЩИКИ: по этим заказчикам берём их договоры 2026 на сумму
         > 30 млн тг, у которых предмет НЕ ИТ и НЕ ИБ (стройматериалы, мебель,
         хозтовары, сантехника, ГСМ и т.п.). Выгружаем: поставщик (компания+БИН),
         что поставил (предмет), сумма, кому (заказчик).

Запуск (на VM, где настроен токен):
    cd /opt/goszakup-monitor && .venv/bin/python analyze_it_ib_suppliers.py

Результат рядом: analyze_it_ib_2026.xlsx (+ CSV-бэкапы).
Прогон длинный (обходит много заказчиков) — лучше через nohup, см. README команды.
"""
from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict

from app import config, portal
from app.goszakup import GoszakupClient, GoszakupError

YEAR = "2026"
YEAR_INT = 2026
CLIENT_MIN = 20_000_000   # этап 1: закупка ИТ/ИБ дороже этого
SUPPLY_MIN = 20_000_000   # этап 2: договор не-ИТ/ИБ (ТОЛЬКО ТОВАРЫ) дороже этого
LIMIT = 200
CAP = 25                  # максимум страниц на один запрос (предохранитель)

# --- Классификаторы по НАЗВАНИЮ лота/договора (точные термины, не морфокорни) ---
IB = re.compile(
    r"firewall|межсетев\w*\s+экран|файрвол|файервол|брандмау|ngfw|\bedr\b|\bxdr\b|антивирус|"
    r"\bdlp\b|утечк\w*\s+информац|защит\w*\s+от\s+утечк|\bsiem\b|\bips\b|\bids\b|"
    r"обнаружен\w*\s+вторжен|предотвращен\w*\s+вторжен|\bwaf\b|защит\w*\s+веб-?приложен|"
    r"криптошлюз|криптограф\w*\s+защит|\bскзи\b|средств\w*\s+криптограф|шифрован|\bvpn\b|"
    r"средств\w*\s+защиты\s+информац|\bсзи\b|информационн\w*\s+безопасн|кибербезопас|"
    r"сканер\w*\s+уязвим|анализ\s+защищ\w*|пентест|многофакторн\w*\s+аутентиф|двухфакторн",
    re.I)
IT = re.compile(
    r"\bсервер\w*|\bсхд\b|систем\w*\s+хранени\w*\s+данн|компьютер\w*|ноутбук\w*|моноблок\w*|"
    r"рабоч\w*\s+станци\w*|\bмонитор(?!инг)\w*|принтер\w*|\bмфу\b|многофункциональн\w*\s+устройств\w*|"
    r"оргтехник\w*|сканер\w*\s+документ\w*|\bибп\b|источник\w*\s+бесперебойн\w*\s+питани\w*|"
    r"коммутатор\w*|маршрутизатор\w*|\bроутер\w*|точк\w*\s+доступа|wi-?fi|сетев\w*\s+оборудован\w*|"
    r"структурированн\w*\s+кабельн\w*|\bскс\b|патч-?панел\w*|\bgpon\b|"
    r"серверн\w*\s+(оборудован\w*|шкаф\w*|стойк\w*)|\bцод\b|дата-?центр\w*|"
    r"программн\w*\s+обеспечени\w*|\bпо\b\s+(для|прикладн|системн)|"
    r"лицензи\w*\s+(microsoft|oracle|на\s+по|программн)|лицензионн\w*\s+программн|"
    r"субд|операционн\w*\s+систем\w*|прикладн\w*\s+программн\w*|"
    r"информационн\w*\s+систем\w*|автоматизированн\w*\s+систем\w*|"
    r"разработк\w*\s+(программн|информацион|по\b)|внедрени\w*\s+(программн|информацион)|"
    r"сопровожд\w*\s+(программн|информацион)|"
    r"видеонаблюден\w*|\bскуд\b|контрол\w*\s+доступа|турникет\w*|"
    r"\bатс\b|ip-?телефони\w*|call-?центр|"
    r"картридж\w*|тонер\w*|заправк\w*\s+картридж\w*|"
    r"\bит-?оборудован\w*|\bit-?оборудован\w*|\bит-?услуг\w*|\bit-?услуг\w*|вычислительн\w*\s+техник\w*|"
    r"планшет\w*|проектор\w*|интерактивн\w*\s+(панел\w*|доск\w*)",
    re.I)


def is_ib(name: str) -> bool:
    return bool(IB.search((name or "").lower()))


def is_it(name: str) -> bool:
    return bool(IT.search((name or "").lower()))


def is_itib(name: str) -> bool:
    return is_it(name) or is_ib(name)


# Ключевые слова для серверного поиска кандидатов (сам отбор — классификатором выше).
FETCH_KW = [
    # ИТ
    "сервер", "система хранения данных", "компьютер", "ноутбук", "моноблок",
    "рабочая станция", "принтер", "МФУ", "оргтехника", "источник бесперебойного питания",
    "коммутатор", "маршрутизатор", "точка доступа", "сетевое оборудование",
    "структурированная кабельная система", "серверное оборудование", "центр обработки данных",
    "программное обеспечение", "лицензии на программное обеспечение", "информационная система",
    "автоматизированная система", "видеонаблюдение", "система контроля доступа",
    "АТС", "IP-телефония", "картридж", "вычислительная техника", "планшет",
    "проектор", "интерактивная панель",
    # ИБ
    "межсетевой экран", "firewall", "антивирус", "средства защиты информации",
    "информационная безопасность", "DLP", "SIEM", "СКЗИ", "криптошлюз", "VPN",
    "WAF", "сканер уязвимостей", "EDR", "защита от утечек информации",
]

BAD_STATUS = ("не состоял", "отмен", "прекращ", "растор", "недейств", "аннулир")


def probe(client, q, v):
    try:
        client.query(q, v)
        return True
    except GoszakupError:
        return False


def main():
    s = config.load()
    with GoszakupClient(s.goszakup_token, timeout=180) as c:
        # --- что умеет схема ---
        lf = {"f": {"nameDescriptionRu": "сервер"}, "l": 1}
        lot_status = probe(c, "query($f:LotsFiltersInput,$l:Int){Lots(filter:$f,limit:$l){id TrdBuy{RefBuyStatus{nameRu}}}}", lf)
        lot_after = probe(c, "query($f:LotsFiltersInput,$l:Int,$a:Int){Lots(filter:$f,limit:$l,after:$a){id}}", {**lf, "a": 0})

        # finYear может ждать int или строку — определяем.
        fy = YEAR_INT
        base = "query($f:ContractFiltersInput,$l:Int){Contract(filter:$f,limit:$l){id}}"
        if not probe(c, base, {"f": {"finYear": YEAR_INT}, "l": 1}):
            if probe(c, base, {"f": {"finYear": YEAR}, "l": 1}):
                fy = YEAR
            else:
                print("!! Contract.finYear фильтр не принят — проверьте схему", file=sys.stderr)
        cf = {"f": {"finYear": fy}, "l": 1}
        sup_name = probe(c, "query($f:ContractFiltersInput,$l:Int){Contract(filter:$f,limit:$l){id Supplier{nameRu}}}", cf)
        con_status = probe(c, "query($f:ContractFiltersInput,$l:Int){Contract(filter:$f,limit:$l){id RefContractStatus{nameRu}}}", cf)
        subj_type = probe(c, "query($f:ContractFiltersInput,$l:Int){Contract(filter:$f,limit:$l){id RefSubjectType{nameRu}}}", cf)
        con_after = probe(c, "query($f:ContractFiltersInput,$l:Int,$a:Int){Contract(filter:$f,limit:$l,after:$a){id}}", {**cf, "a": 0})
        print(f"[probe] lot_status={lot_status} lot_after={lot_after} finYear={fy!r} "
              f"sup_name={sup_name} con_status={con_status} subj_type={subj_type} con_after={con_after}", file=sys.stderr)

        # ========================= ЭТАП 1: КЛИЕНТЫ =========================
        lot_tb = "id numberAnno itogiDatePublic" + (" RefBuyStatus{nameRu}" if lot_status else " refBuyStatusId")

        def lotq(use_after):
            a1 = ",$a:Int" if use_after else ""
            a2 = ",after:$a" if use_after else ""
            return (f"query($f:LotsFiltersInput,$l:Int{a1}){{Lots(filter:$f,limit:$l{a2})"
                    f"{{id nameRu amount customerNameRu customerBin trdBuyId TrdBuy{{{lot_tb}}}}}}}")

        def lot_ok(b):
            if lot_status:
                t = ((b.get("RefBuyStatus") or {}).get("nameRu") or "").lower()
                return not any(x in t for x in BAD_STATUS)
            return b.get("refBuyStatusId") not in (260, 360, 370)

        clients = {}   # bin -> {org, n, max, samples}
        seen = set()
        for i, kw in enumerate(FETCH_KW, 1):
            got = 0
            after = None
            pages = 0
            while pages < CAP:
                v = {"f": {"nameDescriptionRu": kw}, "l": LIMIT}
                ua = lot_after and after is not None
                if ua:
                    v["a"] = after
                try:
                    lots = c.query(lotq(ua), v).get("Lots") or []
                except GoszakupError as e:
                    print(f"  !L {kw}: {e}", file=sys.stderr)
                    break
                if not lots:
                    break
                for l in lots:
                    if l["id"] in seen:
                        continue
                    seen.add(l["id"])
                    b = l.get("TrdBuy") or {}
                    if (b.get("itogiDatePublic") or "")[:4] != YEAR:
                        continue
                    amt = float(l.get("amount") or 0)
                    if amt <= CLIENT_MIN:
                        continue
                    nm = l.get("nameRu") or ""
                    if not is_itib(nm):
                        continue
                    if not lot_ok(b):
                        continue
                    bn = str(l.get("customerBin") or "")
                    if not bn:
                        continue
                    cl = clients.setdefault(bn, {"org": l.get("customerNameRu") or "", "n": 0, "max": 0.0, "samples": []})
                    cl["n"] += 1
                    cl["max"] = max(cl["max"], amt)
                    tag = "ИБ" if is_ib(nm) else "ИТ"
                    if len(cl["samples"]) < 3:
                        cl["samples"].append(f"[{tag}] {nm[:45]} ({amt/1e6:.0f} млн)")
                    got += 1
                pages += 1
                if not lot_after or len(lots) < LIMIT:
                    break
                after = lots[-1]["id"]
            print(f"[1] {i}/{len(FETCH_KW)} {kw}: +{got} | клиентов всего: {len(clients)}", file=sys.stderr)
        print(f"[1] ИТОГО клиентов ИТ/ИБ >{CLIENT_MIN/1e6:.0f}млн за {YEAR}: {len(clients)}", file=sys.stderr)

        # ========================= ЭТАП 2: ПОСТАВЩИКИ =========================
        con_sup = " Supplier{nameRu}" if sup_name else ""
        con_st = " RefContractStatus{nameRu}" if con_status else ""
        con_subj = " RefSubjectType{nameRu}" if subj_type else ""

        def conq(use_after):
            a1 = ",$a:Int" if use_after else ""
            a2 = ",after:$a" if use_after else ""
            return (f"query($f:ContractFiltersInput,$l:Int{a1}){{Contract(filter:$f,limit:$l{a2})"
                    f"{{id trdBuyId trdBuyNameRu descriptionRu contractSum faktSum supplierBiin supplierFio "
                    f"customerBin customerFio signDate refSubjectTypeId{con_sup}{con_st}{con_subj}}}}}")

        def con_ok(ct):
            if not con_status:
                return True
            t = ((ct.get("RefContractStatus") or {}).get("nameRu") or "").lower()
            return not any(x in t for x in BAD_STATUS)

        def is_goods(ct):
            """Только ТОВАРЫ (не работы, не услуги). RefSubjectType.nameRu = Товар/Работа/Услуга;
            если имени нет — по refSubjectTypeId (1 = товар в справочнике goszakup)."""
            if subj_type:
                t = ((ct.get("RefSubjectType") or {}).get("nameRu") or "").lower()
                return "товар" in t
            return ct.get("refSubjectTypeId") == 1

        supplies = []
        clis = list(clients.items())
        for i, (bn, info) in enumerate(clis, 1):
            after = None
            pages = 0
            while pages < CAP:
                v = {"f": {"customerBin": bn, "finYear": fy}, "l": LIMIT}
                ua = con_after and after is not None
                if ua:
                    v["a"] = after
                try:
                    cons = c.query(conq(ua), v).get("Contract") or []
                except GoszakupError as e:
                    print(f"  !C {bn}: {e}", file=sys.stderr)
                    break
                if not cons:
                    break
                for ct in cons:
                    csum = float(ct.get("contractSum") or 0)
                    if csum <= SUPPLY_MIN:
                        continue
                    subj = ct.get("trdBuyNameRu") or ct.get("descriptionRu") or ""
                    if is_itib(subj):      # нам нужны НЕ ИТ/ИБ
                        continue
                    if not is_goods(ct):   # только ТОВАРЫ (без работ и услуг)
                        continue
                    if not con_ok(ct):
                        continue
                    supnm = ((ct.get("Supplier") or {}).get("nameRu") if sup_name else None) \
                        or ct.get("supplierFio") or ""
                    supplies.append({
                        "sup": supnm,
                        "sup_bin": str(ct.get("supplierBiin") or ""),
                        "subject": subj,
                        "sum": csum,
                        "fakt": float(ct.get("faktSum") or 0),
                        "cust": ct.get("customerFio") or info["org"],
                        "cust_bin": bn,
                        "sign": ct.get("signDate") or "",
                        "url": portal.ANNOUNCE_URL.format(id=ct.get("trdBuyId")) if ct.get("trdBuyId") else "",
                    })
                pages += 1
                if not con_after or len(cons) < LIMIT:
                    break
                after = cons[-1]["id"]
            if i % 25 == 0 or i == len(clis):
                print(f"[2] {i}/{len(clis)} заказчиков | строк поставок: {len(supplies)}", file=sys.stderr)

    write_outputs(clients, supplies, sup_name)


def write_outputs(clients: dict, supplies: list, sup_name: bool):
    # --- свод по поставщикам ---
    sup = defaultdict(lambda: {"name": "", "n": 0, "sum": 0.0, "custs": set(), "subjects": set()})
    for r in supplies:
        key = r["sup_bin"] or r["sup"]
        o = sup[key]
        o["name"] = r["sup"] or o["name"]
        o["n"] += 1
        o["sum"] += r["sum"]
        o["custs"].add(r["cust"])
        o["subjects"].add(r["subject"][:40])
    sup_rows = sorted(
        ({"bin": k, **v} for k, v in sup.items()), key=lambda x: -x["sum"])

    client_rows = sorted(
        ({"bin": b, **v} for b, v in clients.items()), key=lambda x: -x["max"])

    # --- CSV (бэкап, UTF-8 с BOM) ---
    with open("analyze_clients_it_ib.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Заказчик", "БИН", "ИТ/ИБ-закупок >20млн", "Макс сумма", "Примеры"])
        for c in client_rows:
            w.writerow([c["org"], c["bin"], c["n"], f'{c["max"]:.0f}', " | ".join(c["samples"])])
    with open("analyze_supplies_non_it.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Заказчик (ИТ/ИБ-клиент)", "БИН заказчика", "Что купил (товар)", "Сумма договора",
                    "Поставщик", "БИН поставщика", "Дата договора", "Ссылка"])
        for r in sorted(supplies, key=lambda x: -x["sum"]):
            w.writerow([r["cust"], r["cust_bin"], r["subject"], f'{r["sum"]:.0f}',
                        r["sup"], r["sup_bin"], r["sign"], r["url"]])

    # --- XLSX ---
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
        HEAD = Font(bold=True, color="FFFFFF")
        FILL = PatternFill("solid", fgColor="1F4E79")

        def mk(ws, headers, data, widths, money=(), url=None):
            ws.append(headers)
            for i, _ in enumerate(headers, 1):
                cz = ws.cell(1, i)
                cz.font = HEAD
                cz.fill = FILL
                ws.column_dimensions[get_column_letter(i)].width = widths[i - 1]
            for row in data:
                ws.append(row)
                for m in money:
                    ws.cell(ws.max_row, m).number_format = "#,##0"
                if url:
                    cc = ws.cell(ws.max_row, url)
                    if cc.value:
                        cc.hyperlink = cc.value
                        cc.font = Font(color="0563C1", underline="single")
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"

        wb = Workbook()
        # 1) Клиенты ИТ/ИБ — от кого пляшем
        s1 = wb.active
        s1.title = "Клиенты ИТ-ИБ"
        mk(s1, ["Заказчик (ИТ/ИБ-клиент)", "БИН", "ИТ/ИБ-закупок >20млн", "Макс сумма, тг", "Примеры ИТ/ИБ-закупок"],
           [[c["org"], c["bin"], c["n"], c["max"], " | ".join(c["samples"])] for c in client_rows],
           [50, 15, 13, 18, 70], money=(4,))

        # 2) Их не-ИТ закупки ТОВАРОВ: заказчик -> что купил -> поставщик
        s2 = wb.create_sheet("Закупки товаров (не-ИТ)")
        mk(s2, ["Заказчик (ИТ/ИБ-клиент)", "БИН заказчика", "Что купил (товар)", "Сумма, тг",
                "Поставщик", "БИН поставщика", "Дата", "Ссылка"],
           [[r["cust"], r["cust_bin"], r["subject"], r["sum"], r["sup"], r["sup_bin"], r["sign"], r["url"]]
            for r in sorted(supplies, key=lambda x: -x["sum"])],
           [44, 15, 50, 18, 38, 15, 18, 42], money=(4,), url=8)

        # 3) Свод по поставщикам товаров
        s3 = wb.create_sheet("Поставщики (свод)")
        mk(s3, ["Поставщик", "БИН", "Договоров", "Сумма всего, тг", "Кол-во заказчиков", "Что поставлял (примеры)"],
           [[o["name"], o["bin"], o["n"], o["sum"], len(o["custs"]), "; ".join(list(o["subjects"])[:4])]
            for o in sup_rows], [45, 15, 11, 18, 15, 60], money=(4,))

        wb.save("analyze_it_ib_2026.xlsx")
        print("XLSX: analyze_it_ib_2026.xlsx")
    except ImportError:
        print("openpyxl нет — только CSV", file=sys.stderr)

    print(f"\nГОТОВО. Клиентов ИТ/ИБ: {len(client_rows)} | "
          f"строк поставок (не-ИТ товары, >{SUPPLY_MIN/1e6:.0f}млн): {len(supplies)} | "
          f"поставщиков: {len(sup_rows)}")
    print("ТОП-15 поставщиков по сумме:")
    for o in sup_rows[:15]:
        nm = (o["name"] or o["bin"])[:48]
        print(f"  {o['sum']:>16,.0f} | дог {o['n']:>2} | {o['bin']:<12} {nm}".replace(",", " "))


if __name__ == "__main__":
    main()
