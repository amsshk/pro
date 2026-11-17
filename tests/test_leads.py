"""Meta Business Suite leads (chat sales) upload: English and Thai exports."""
import pytest

from paysignal.importers import detect
from paysignal.leads import parse
from paysignal.server import create_app

KEY = "leads-test-key-0123456789"
CLIENTS = {"clinic": {"api_key": KEY, "name": "Clinic", "currency": "THB"}}
H = {"X-API-Key": KEY}
THAI = ("﻿สร้างเมื่อ,ชื่อ,อีเมล,แหล่งที่มา,แบบฟอร์ม,ช่องทาง,ระยะ,เจ้าของ,ป้ายกำกับ,โทรศัพท์,หมายเลขโทรศัพท์รอง,หมายเลข WhatsApp\n"
        '06/20/2026 3:24am,A One,,เสียค่าใช้จ่าย,,Messenger,ซื้อแล้ว,Unassigned,"ไอซ์/มาย,ad_id.111,messenger_ads,การตอบสนองต่อโฆษณา",,,\n'
        '06/02/2026 1:05pm,B Two,,เสียค่าใช้จ่าย,,Messenger,ซื้อแล้ว,Unassigned,"ไอซ์/บูม,ad_id.111,สร้างคอนเวอร์ชั่นแล้ว",,,\n'
        '04/11/2026 9:00pm,C Three,,ออร์แกนิก,,,ซื้อแล้ว,Staff One,"ลูกค้าเก่า",,,\n')
ENGLISH = ("Created,Name,Email,Source,Form,Channel,Stage,Owner,Labels,Phone,Secondary phone number,WhatsApp number\n"
           '2026-10-01 10:00:00,D Four,d@x.com,Paid,,Messenger,Intake,Unassigned,"ad_id.222",+66 81 234 5678,,\n'
           '2026-10-02 11:00:00,E Five,,Paid,,Messenger,Converted,Unassigned,"ad_id.222,Purchased",,,\n')


def test_thai_and_english_exports_are_recognised():
    assert detect(THAI.lstrip("﻿")) == "leads" and detect(ENGLISH) == "leads"
    rows, errors = parse(THAI)
    assert not errors and len(rows) == 3
    assert rows[0]["source"] == "Paid ads" and rows[0]["stage"] == "Purchased" and rows[0]["ad_ids"] == ["111"]
    assert rows[0]["team"] == ["ไอซ์/มาย"] and rows[1]["flags"] == ["Conversion sent"] and rows[2]["owner"] == "Staff One"
    en, _ = parse(ENGLISH)
    assert en[0]["stage"] == "New" and en[1]["stage"] == "Converted" and en[0]["phone_hash"] and en[0]["email_hash"]


def test_no_names_or_contacts_are_stored(tmp_path):
    c = create_app(CLIENTS, str(tmp_path / "l.db"), start_worker=False).test_client()
    c.post("/import/clinic", data=ENGLISH.encode(), headers=H)
    import sqlite3
    dump = "\n".join(sqlite3.connect(str(tmp_path / "l.db")).iterdump())
    assert "D Four" not in dump and "d@x.com" not in dump and "234 5678" not in dump and "66812345678" not in dump


def test_upload_summary_price_and_clear(tmp_path):
    c = create_app(CLIENTS, str(tmp_path / "l.db"), start_worker=False).test_client()
    r = c.post("/import/clinic", data=THAI.encode(), headers=H).json
    assert r["kind"] == "leads" and r["new"] == 3
    assert c.post("/import/clinic", data=THAI.encode(), headers=H).json["duplicates"] == 3
    s = c.get("/leads/clinic", headers=H).json
    assert s["leads"] == 3 and s["buyers"] == 3 and s["sent_to_meta"] == 1 and s["value"] is None and s["currency"] == "THB"
    assert s["top_ads"][0] == {"ad_id": "111", "leads": 2, "buyers": 2}
    assert [m["month"] for m in s["months"]] == ["2026-04", "2026-05", "2026-06"] and s["months"][1]["leads"] == 0
    assert {t["label"] for t in s["team"]} == {"ไอซ์/มาย", "ไอซ์/บูม", "Staff One"}
    assert c.post("/leads/clinic/price", json={"price": 1500}, headers=H).json["value"] == 4500
    assert c.post("/leads/clinic/price", json={"price": -1}, headers=H).status_code == 400
    assert c.get("/leads/clinic").status_code == 401
    c.post("/data/clinic/clear-sales", json={"confirm": "DELETE"}, headers=H)
    assert c.get("/leads/clinic", headers=H).json["leads"] == 0


def test_a_sales_file_is_still_a_sales_file():
    assert detect("Created,Amount,Name\n2026-10-01,100,x\n") == "sales"


def test_meta_converted_stage_counts_and_a_newer_export_never_moves_a_lead_back(tmp_path):
    head = THAI.split("\n", 1)[0] + "\n"
    c = create_app(CLIENTS, str(tmp_path / "l.db"), start_worker=False).test_client()
    first = head + '05/01/2026 1:00pm,X,,เสียค่าใช้จ่าย,,Messenger,สร้างคอนเวอร์ชั่นแล้ว,Unassigned,"ad_id.9",,,\n' \
        + '05/02/2026 1:00pm,Y,,เสียค่าใช้จ่าย,,Messenger,มาใหม่,Unassigned,"ad_id.9,Intake",,,\n' \
        + '05/03/2026 1:00pm,Z,,เสียค่าใช้จ่าย,,Messenger,Spam,Unassigned,"",,,\n'
    later = head + '05/01/2026 1:00pm,X,,เสียค่าใช้จ่าย,,Messenger,มาใหม่,Unassigned,"ad_id.9",,,\n'
    c.post("/import/clinic", data=first.encode(), headers=H)
    c.post("/import/clinic", data=later.encode(), headers=H)
    s = c.get("/leads/clinic", headers=H).json
    assert s["leads"] == 2 and s["buyers"] == 1 and s["sent_to_meta"] == 1 and s["rate"] == 0.5
    assert [f["stage"] for f in s["funnel"]] == ["Converted", "New"] and s["team"] == [] or s["team"][0]["label"] != "Intake"
