import io
from datetime import datetime, timedelta, timezone

from openpyxl import load_workbook

from paysignal.server import create_app


def _app(tmp_path):
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json={"business": "Bynawi", "email": "o@bynawi.ae", "password": "first-password-1", "terms": True}).json["token"]
    return app, c, {"X-API-Key": tok}


def test_export_formats(tmp_path):
    app, c, h = _app(tmp_path)
    now = datetime.now(timezone.utc)
    csv_text = "id,date,amount,currency,email\n" + "\n".join(
        f"o{i},{(now - timedelta(days=i)).strftime('%Y-%m-%dT%H:%M:%S')},{300 + i},AED,b{i}@x.ae" for i in range(12))
    r = c.post("/upload/csv/bynawi", data=csv_text, headers=h)
    assert r.status_code == 200, r.json
    r = c.get("/export/bynawi?format=csv&period=30", headers=h)
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert "attachment" in r.headers["Content-Disposition"] and ".csv" in r.headers["Content-Disposition"]
    text = r.data.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("Date,Order ID,Came from") and len(text.strip().splitlines()) == 13
    assert "@x.ae" not in text                                            # no customer emails in files
    r = c.get("/export/bynawi?format=xlsx&period=30", headers=h)
    wb = load_workbook(io.BytesIO(r.data))
    assert {"Summary", "Daily", "Orders", "Where sales came from", "Top products"} <= set(wb.sheetnames)
    assert wb["Orders"].max_row == 13 and wb["Summary"]["B1"].value == "ProAds sales report"
    r = c.get("/export/bynawi?format=pdf&period=30", headers=h)
    assert r.status_code == 200 and r.data[:5] == b"%PDF-" and len(r.data) > 3000
    assert c.get("/export/bynawi?format=doc", headers=h).status_code == 400
    assert c.get("/export/bynawi?format=pdf").status_code == 401


def test_export_empty_business(tmp_path):
    app, c, h = _app(tmp_path)
    for f in ("csv", "xlsx", "pdf"):
        assert c.get(f"/export/bynawi?format={f}", headers=h).status_code == 200
