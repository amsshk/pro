import time

from paysignal import ads_watch, client_mail


def g(**k):
    base = {"id": "1", "name": "GCC Broad", "campaign": "BYNAWI", "status": "ADGROUP_STATUS_DELIVERY_OK",
            "skip_learning": False, "created": "2026-11-20 10:00:00", "promote": False}
    return {**base, **k}


def test_balance_learning_off_on_done():
    inst = time.mktime((2026, 11, 1, 0, 0, 0, 0, 0, 0))
    kinds = [k for k, _, _ in ads_watch.due([g(status="ADVERTISER_ACCOUNT_BALANCE_EXCEED")], 0, inst, "2026-11-27")]
    assert kinds == ["tt_balance", "tt_learning_on"]
    assert [k for k, _, _ in ads_watch.due([g(skip_learning=True)], 60, inst, "d")] == ["tt_learning_off"]
    assert [k for k, _, _ in ads_watch.due([g(created="2026-10-01 00:00:00")], 50, inst, "d")] == ["tt_learning_done"]
    assert ads_watch.due([g(promote=True)], 50, inst, "d") == []


def test_every_kind_renders():
    for k in client_mail.KINDS:
        subject, text, page = client_mail.render(k, "Glow", client_mail.sample(k), "https://pro.example")
        assert (subject.startswith("Glow") or k == "receipt") and "ProAds" in text and "<p" in page
    assert "/app#/billing" in client_mail.render("payment_failed", "Glow", client_mail.sample("payment_failed"), "https://x")[1]
