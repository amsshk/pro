from paysignal.learning import LearningStore, lesson_from_audit
from paysignal.server import create_app

AUDIT = {"orders": 120, "refund_rate": 0.08, "peak_sales_hours": "18:00-21:00", "best_sales_days": ["Friday", "Thursday"],
         "customers": {"repeat_rate": 0.2, "profit_from_repeat_customers": 0.4, "known": 90},
         "discount_codes": [{"code": "GLOW10", "orders": 30, "vs_full_price": -0.3}],
         "posts": {"patterns": {"format": {"reel": {"lift": 1.6, "posts": 5, "confident": True}}}}}


def test_lessons_are_anonymous_and_need_three_businesses(tmp_path):
    import sqlite3
    st = LearningStore(sqlite3.connect(":memory:"))
    lesson = lesson_from_audit(AUDIT)
    assert "GLOW10" not in str(lesson) and lesson["discount_effect"] == [-0.3]
    assert lesson_from_audit({"orders": 5}) is None
    st.keep("a", AUDIT); st.keep("b", AUDIT)
    assert st.benchmarks() is None                              # not enough businesses yet
    st.keep("c", {**AUDIT, "refund_rate": 0.2})
    b = st.benchmarks()
    assert b["businesses"] == 3 and b["median_refund_rate"] == 0.08 and b["post_patterns"]["format"]["reel"]["median_lift"] == 1.6
    st.forget("c")
    assert st.count() == 2


def test_clear_sales_can_keep_lessons_and_ai_gets_benchmarks(tmp_path):
    app = create_app({}, str(tmp_path / "x.db"), start_worker=False)
    st = app.extensions["paysignal"]
    c = app.test_client()
    tok = c.post("/auth/signup", json={"business": "Glow", "email": "o@glow.ae", "password": "first-password-1", "terms": True}).json["token"]
    rows = "\n".join(f"{i},2026-10-{1 + i % 28:02d},{100 + i},AED" for i in range(30))
    c.post("/import/glow", data="id,date,amount,currency\n" + rows + "\n", headers={"X-API-Key": tok})
    r = c.post("/data/glow/clear-sales", json={"confirm": "DELETE", "keep_lessons": True}, headers={"X-API-Key": tok}).json
    assert r["lessons_kept"] and st.learnings.count() == 1
    st.learnings.keep("x", AUDIT); st.learnings.keep("y", AUDIT)
    from paysignal.assistant import business_context
    with app.test_request_context():
        pass
    assert st.learnings.benchmarks()["businesses"] == 3
