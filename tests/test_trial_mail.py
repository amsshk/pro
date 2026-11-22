import time

from paysignal import trial_mail
from paysignal.server import create_app


def test_due_windows():
    now = 1_000_000
    acc = lambda end, plan=False: {"plan": plan, "trial_ends": end}
    assert trial_mail.due(acc(now + 3600), now) == "soon"
    assert trial_mail.due(acc(now - 3600), now) == "ended"
    assert trial_mail.due(acc(now - 4 * 86400), now) == "followup"
    assert trial_mail.due(acc(now - 2.5 * 86400), now) is None
    assert trial_mail.due(acc(now + 3 * 86400), now) is None
    assert trial_mail.due(acc(now - 3600, plan=True), now) is None
    assert trial_mail.due({"plan": False, "trial_ends": None}, now) is None


def test_job_sends_each_email_once(tmp_path, monkeypatch):
    sent = []
    from paysignal import mailer
    monkeypatch.setattr(mailer, "available", lambda: True)
    monkeypatch.setattr(mailer, "send_checked", lambda to, subject, text, html=None: (sent.append((to, subject)) or True, ""))
    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "t.db"), start_worker=False)
    c = app.test_client()
    c.post("/auth/signup", json={"business": "Glow Clinic", "email": "o@glow.ae", "password": "first-password-1", "terms": True, "phone": "0501234567"})
    st = app.extensions["paysignal"]
    days = st.billing.ads_access("glow-clinic")["trial_days"]
    st.billing.upsert("glow-clinic", ads_trial_started=int(time.time() - days * 86400 - 3600))   # ended an hour ago
    st.trial_mail_all()
    st.trial_mail_all()
    assert sent == [("o@glow.ae", "Glow Clinic: your free ad test has ended")]
    _, text, html = trial_mail.render("ended", "Glow Clinic", {"pro": "AED 299", "premium": "AED 799"}, "5%", "https://x.net")
    assert "https://x.net/app#/billing" in text and "AED 299" in text and "Add your plan" in html


def test_failed_reminder_is_retried_and_test_email_reports_why(tmp_path, monkeypatch):
    from paysignal import mailer
    tries = []
    monkeypatch.setattr(mailer, "available", lambda: True)
    monkeypatch.setattr(mailer, "send_checked", lambda to, s, t, html=None: (tries.append(s) or (len(tries) > 1, "" if len(tries) > 1 else "SMTPAuthenticationError: bad login")))
    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "t.db"), start_worker=False)
    c = app.test_client()
    c.post("/auth/signup", json={"business": "Glow Clinic", "email": "o@glow.ae", "password": "first-password-1", "terms": True, "phone": "0501234567"})
    st = app.extensions["paysignal"]
    days = st.billing.ads_access("glow-clinic")["trial_days"]
    st.billing.upsert("glow-clinic", ads_trial_started=int(time.time() - days * 86400 - 3600))
    st.trial_mail_all(); st.trial_mail_all(); st.trial_mail_all()
    assert len(tries) == 2                                    # failed once, retried, then marked sent
    r = c.post("/admin/pro/test-email", headers={"X-API-Key": "admin-key-0123456789abc"}, json={"to": "me@x.com"}).json
    assert r["sent"] is True and r["recent_reminders"][0]["client_id"] == "glow-clinic"
    r = c.post("/admin/pro/test-email", headers={"X-API-Key": "admin-key-0123456789abc"}, json={"to": "me@x.com", "kind": "ended"}).json
    assert r["sent"] is True and tries[-1] == "[Test] Test business: your free ad test has ended"
