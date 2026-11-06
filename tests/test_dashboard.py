from datetime import datetime, timedelta, timezone

from paysignal.server import create_app

ACCT = {"business": "Glow Clinic", "email": "owner@glow.ae", "password": "first-password-1", "terms": True}


def test_dashboard_compares_periods_and_guides_setup(tmp_path):
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    H = {"X-API-Key": tok}
    d = c.get("/dashboard/glow-clinic", headers=H).json
    assert not d["has_data"] and d["setup"][0]["done"] is False
    today = datetime.now(timezone.utc).date()
    rows = ["id,date,amount,currency,email,utm_source,utm_medium"]
    rows.append(f"1,{today},300,AED,a@x.com,instagram,organic")
    rows.append(f"2,{today - timedelta(days=1)},100,AED,a@x.com,,")
    rows.append(f"3,{today - timedelta(days=40)},200,AED,b@x.com,,")       # previous 30 days
    c.post("/import/glow-clinic", data="\n".join(rows) + "\n", headers=H)
    d = c.get("/dashboard/glow-clinic?period=30", headers=H).json
    m = {x["key"]: x for x in d["metrics"]}
    assert m["sales"]["value"] == 400 and m["sales"]["previous"] == 200 and m["sales"]["change"] == 1.0
    assert m["orders"]["value"] == 2 and m["aov"]["value"] == 200
    assert len(d["dates"]) == 30 and d["dates"][-1] == today.isoformat() and d["live"]
    assert d["series"]["sales"]["current"][-1] == 300 and sum(d["series"]["sales"]["previous"]) == 200
    assert {s["source"] for s in d["sources"]} == {"Instagram posts", "Direct / unknown"}
    assert d["returning_rate"] == 0.5
    assert d["setup"][0]["done"] and not d["setup"][3]["done"]


def test_old_uploads_show_their_last_period(tmp_path):
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    c.post("/import/glow-clinic", data="id,date,amount,currency\n1,2025-03-10,100,AED\n", headers={"X-API-Key": tok})
    d = c.get("/dashboard/glow-clinic?period=7", headers={"X-API-Key": tok}).json
    assert d["as_of"] == "2025-03-10" and not d["live"] and d["metrics"][0]["value"] == 100


def test_clear_sales_removes_example_data(tmp_path):
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    H = {"X-API-Key": tok}
    c.post("/import/glow-clinic", data="id,date,amount,currency\n1,2026-10-01,100,AED\n", headers=H)
    assert c.post("/data/glow-clinic/clear-sales", json={}, headers=H).status_code == 400
    assert c.post("/data/glow-clinic/clear-sales", json={"confirm": "DELETE"}, headers=H).json["sales_deleted"] == 1
    assert not c.get("/dashboard/glow-clinic", headers=H).json["has_data"]


def test_other_platform_exports_and_excel(tmp_path):
    import io
    from openpyxl import Workbook
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json=ACCT).json["token"]
    H = {"X-API-Key": tok}
    salla = ("Order Number,Order Date,Order Status,Customer Email,Mobile,Product Name,Order Total (AED)\n"
             "1001,2026-10-01 10:00,Delivered,a@x.com,0501111111,Serum,\"1,250.00\"\n"
             "1002,01/10/2026,Cancelled,b@x.com,,Cream,90\n"
             "1003,2026-10-03,Refunded,c@x.com,,Cream,90\n")
    r = c.post("/import/glow-clinic", data=salla, headers=H).json
    assert r["kind"] == "sales" and r["new"] == 3 and any("cancelled" in e for e in r["errors"])  # 2 sales + 1 refund
    wb = Workbook(); ws = wb.active
    ws.append(["Transaction ID", "Created", "Amount", "Currency"]); ws.append(["T1", "2026-10-05 12:00", 300, "AED"])
    buf = io.BytesIO(); wb.save(buf)
    r = c.post("/import/glow-clinic", data=buf.getvalue(), headers=H, content_type="application/octet-stream").json
    assert r["kind"] == "sales" and r["new"] == 1
    m = {x["key"]: x for x in c.get("/dashboard/glow-clinic?period=365", headers=H).json["metrics"]}
    assert m["sales"]["value"] == 1250 + 90 + 300
