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
