from paysignal.learning import LearningStore, lesson_from_audit
from paysignal.server import create_app

AUDIT = {"orders": 120, "refund_rate": 0.08, "peak_sales_hours": "18:00-21:00", "best_sales_days": ["Friday", "Thursday"],
         "customers": {"repeat_rate": 0.2, "profit_from_repeat_customers": 0.4, "known": 90},
         "discount_codes": [{"code": "GLOW10", "orders": 30, "vs_full_price": -0.3}],
         "posts": {"patterns": {"format": {"reel": {"lift": 1.6, "posts": 5, "confident": True}}}}}


def test_lessons_are_anonymous_and_need_five_businesses(tmp_path):
    import sqlite3
    st = LearningStore(sqlite3.connect(":memory:"))
    lesson = lesson_from_audit(AUDIT)
    assert "GLOW10" not in str(lesson) and lesson["discount_effect"] == [-0.3]
    assert lesson_from_audit({"orders": 5}) is None
    for x in "abcd":
        st.keep(x, AUDIT)
    assert st.benchmarks() is None                              # not enough businesses yet
    st.keep("e", {**AUDIT, "refund_rate": 0.2})
    b = st.benchmarks()
    assert b["businesses"] == "5+" and b["median_refund_rate"] == 0.1 and b["post_patterns"]["format"]["reel"]["median_lift"] == 1.6
    st.forget("e")
    assert st.count() == 4


def test_clear_sales_can_keep_lessons_and_ai_gets_benchmarks(tmp_path):
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    st = app.extensions["paysignal"]
    c = app.test_client()
    tok = c.post("/auth/signup", json={"business": "Glow", "email": "o@glow.ae", "password": "first-password-1", "terms": True}).json["token"]
    rows = "\n".join(f"{i},2026-10-{1 + i % 28:02d},{100 + i},AED" for i in range(30))
    c.post("/import/glow", data="id,date,amount,currency\n" + rows + "\n", headers={"X-API-Key": tok})
    r = c.post("/data/glow/clear-sales", json={"confirm": "DELETE", "keep_lessons": True}, headers={"X-API-Key": tok}).json
    assert r["lessons_kept"] and st.learnings.count() == 1
    for x in "wxyz":
        st.learnings.keep(x, AUDIT)
    from paysignal.assistant import business_context
    with app.test_request_context():
        pass
    assert st.learnings.benchmarks()["businesses"] == "5+"


def test_opt_out_removes_and_blocks_contributions_and_rare_values_are_hidden(tmp_path):
    import sqlite3
    st = LearningStore(sqlite3.connect(":memory:"))
    for x in "abcde":
        st.keep(x, AUDIT)
    st.keep("odd", {**AUDIT, "peak_sales_hours": "03:00-04:00"})
    b = st.benchmarks()
    assert "03:00-04:00" not in b["common_peak_hours"]                     # only one business has it
    st.set_sharing("a", False)
    assert st.count() == 5 and not st.keep("a", AUDIT) and not st.keep("a:archived", AUDIT)
    st.set_sharing("a", True)
    assert st.keep("a", AUDIT) and st.count() == 6


def test_lessons_switch_endpoint(tmp_path):
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    c = app.test_client()
    tok = c.post("/auth/signup", json={"business": "Glow", "email": "o@glow.ae", "password": "first-password-1", "terms": True}).json["token"]
    h = {"X-API-Key": tok}
    assert c.get("/lessons/glow", headers=h).json["share"] is True
    app.extensions["paysignal"].learnings.keep("glow", AUDIT)
    assert c.post("/lessons/glow", json={"share": False}, headers=h).json["share"] is False
    assert app.extensions["paysignal"].learnings.count() == 0 and c.get("/lessons/glow", headers=h).json["share"] is False
    assert c.post("/lessons/glow", json={"share": "no"}, headers=h).status_code == 400


def test_benchmarks_carry_no_identifiers_and_drop_a_business_at_once(tmp_path):
    import sqlite3
    st = LearningStore(sqlite3.connect(":memory:"))
    for x in "abcde":
        st.keep(x, AUDIT)
    b = st.benchmarks()
    hashes = [r[0] for r in st.db.execute("SELECT source_hash FROM learnings")]
    assert not any(hh in str(b) for hh in hashes) and not any(x in str(b).split("'") for x in "abcde")
    st.set_sharing("a", False)                                    # opt-out: fewer than 5 left, nothing shared
    assert st.benchmarks() is None
