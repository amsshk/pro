from datetime import datetime, timezone

from paysignal import mailer
from paysignal.models import Payment
from paysignal.server import create_app


def test_proads_own_sale_emails_the_proads_inbox(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setenv("SMTP_HOST", "smtp.x"); monkeypatch.setenv("SMTP_FROM", "ProAds <a@x.net>")
    monkeypatch.setenv("SMTP_USER", "admin@x.net")
    monkeypatch.setattr(mailer, "send_checked", lambda to, s, t, html=None: (sent.append((to, s)) or True, ""))
    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "t.db"), start_worker=False)
    st = app.extensions["paysignal"]
    st.client_mails.installed_at()
    st.ingest("pro", [Payment("pro", "stripe", "ch_1WYUD6AA4", "purchase", 299.0, "AED", datetime.now(timezone.utc), fbclid="fb.1")])
    st.order_mails_all(); st.order_mails_all()
    assert len(sent) == 1 and sent[0][0] == "admin@x.net"



def test_paid_invoice_sends_receipt_once(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setenv("SMTP_HOST", "smtp.x"); monkeypatch.setenv("SMTP_FROM", "ProAds <a@x.net>")
    monkeypatch.setattr(mailer, "send_checked", lambda to, s, t, html=None: (sent.append((to, s, t)) or True, ""))
    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "t.db"), start_worker=False)
    st = app.extensions["paysignal"]
    inv = {"id": "in_1", "customer": "cus_x", "customer_email": "c@shop.ae", "amount_paid": 29900, "currency": "aed",
           "number": "PA-0001", "hosted_invoice_url": "https://invoice.stripe.com/i/1", "created": 1764300000,
           "lines": {"data": [{"description": "ProAds Pro"}]}}
    monkeypatch.delenv("SMTP_USER", raising=False)
    st.billing_mails({"type": "invoice.paid", "data": {"object": inv}}, None)
    st.billing_mails({"type": "invoice.paid", "data": {"object": inv}}, None)
    assert [x[0] for x in sent] == ["c@shop.ae"] and sent[0][0] == "c@shop.ae" and "AED 299" in sent[0][1].replace("299.00", "299")
    assert "https://invoice.stripe.com/i/1" in sent[0][2]


def test_receipt_copy_goes_to_admin(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setenv("SMTP_HOST", "smtp.x"); monkeypatch.setenv("SMTP_FROM", "ProAds <a@x.net>")
    monkeypatch.setenv("SMTP_USER", "admin@x.net")
    monkeypatch.setattr(mailer, "send_checked", lambda to, s, t, html=None: (sent.append(to) or True, ""))
    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "t.db"), start_worker=False)
    inv = {"id": "in_2", "customer": "cus_y", "customer_email": "c@shop.ae", "amount_paid": 29900, "currency": "aed"}
    app.extensions["paysignal"].billing_mails({"type": "invoice.paid", "data": {"object": inv}}, None)
    assert sent == ["c@shop.ae", "admin@x.net"]


def test_billing_auto_fixes_webhook_events_and_sends_missed_receipts(tmp_path, monkeypatch):
    sent, posts = [], []
    monkeypatch.setenv("SMTP_HOST", "smtp.x"); monkeypatch.setenv("SMTP_FROM", "ProAds <a@x.net>")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x"); monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.setattr(mailer, "send_checked", lambda to, s, t, html=None: (sent.append(to) or True, ""))
    from paysignal import billing
    monkeypatch.setattr(billing, "available", lambda: True)

    class R:
        def __init__(self, d): self.status_code, self._d = 200, d
        def json(self): return self._d

    class H:
        def get(self, url, params=None, **k):
            if url.endswith("webhook_endpoints"):
                return R({"data": [{"id": "we_1", "url": "https://pro.x/webhooks/proads-billing", "enabled_events": ["invoice.created"]}]})
            return R({"data": [{"id": "in_9", "customer": "cus_z", "customer_email": "p@shop.ae", "amount_paid": 29900, "currency": "aed"}]})
        def post(self, url, data=None, **k):
            posts.append((url, data)); return R({})

    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "t.db"), start_worker=False)
    st = app.extensions["paysignal"]
    st.stripe_http = H()
    st.billing_auto(); st.billing_auto()
    assert sent == ["p@shop.ae"]
    assert len(posts) == 1 and "invoice.paid" in posts[0][1]["enabled_events[]"]


def test_signup_and_client_email_copy_reach_admin(tmp_path, monkeypatch):
    import time
    sent = []
    monkeypatch.setenv("SMTP_HOST", "smtp.x"); monkeypatch.setenv("SMTP_FROM", "ProAds <a@x.net>")
    monkeypatch.setenv("SMTP_USER", "admin@x.net")
    monkeypatch.setattr(mailer, "send_checked", lambda to, s, t, html=None: (sent.append((to, s)) or True, ""))
    app = create_app({"pro": {"api_key": "admin-key-0123456789abc"}}, str(tmp_path / "t.db"), start_worker=False)
    c = app.test_client()
    c.post("/auth/signup", json={"business": "Glow Clinic", "email": "o@glow.ae", "password": "first-password-1", "terms": True, "phone": "0501234567"})
    st = app.extensions["paysignal"]
    assert st.deliver_client_mail("glow-clinic", "plan_ended", {}) is True
    time.sleep(0.3)
    subjects = [s for to, s in sent if to == "admin@x.net"]
    assert any(s.startswith("[ProAds] New sign-up: Glow Clinic") for s in subjects)
    assert any(s.startswith("[Copy]") for s in subjects)
