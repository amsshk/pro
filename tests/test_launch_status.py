from paysignal import meta_ads


class R:
    def __init__(self, body, status=200):
        self.status_code, self._b = status, body
        self.text = str(body)

    def json(self):
        return self._b


class Http:
    def __init__(self, batch, single=None):
        self.batch, self.single = batch, single or {}

    def get(self, url, params=None, timeout=None):
        if "ids" in (params or {}):
            return self.batch
        cid = url.rstrip("/").split("/")[-1]
        return self.single.get(cid, R({"error": {"message": "Object with ID does not exist", "code": 100}}, 400))


S = {"token": "t", "ad_account": "act_1", "page_id": "p", "pixel_id": "x", "version": "v25.0"}


def states(batch, single=None):
    return meta_ads.Publisher(Http(batch, single), S).live_states(["c1", "c2", "c3", "c4", "c5"])


def test_maps_what_ads_manager_shows():
    ad = lambda *s: {"data": [{"effective_status": x} for x in s]}
    out = states(R({
        "c1": {"status": "ACTIVE", "effective_status": "ACTIVE", "ads": ad("ACTIVE")},
        "c2": {"status": "PAUSED", "effective_status": "PAUSED", "ads": ad("CAMPAIGN_PAUSED")},
        "c3": {"status": "ACTIVE", "effective_status": "ACTIVE", "ads": ad("PENDING_REVIEW")},
        "c4": {"status": "ACTIVE", "effective_status": "ACTIVE", "ads": ad("DISAPPROVED")},
        "c5": {"status": "DELETED", "effective_status": "DELETED"}}))
    assert out == {"c1": "ACTIVE", "c2": "PAUSED", "c3": "IN_REVIEW", "c4": "NOT_RUNNING", "c5": "REMOVED"}


def test_deleted_campaign_breaks_batch_then_checked_one_by_one():
    single = {"c1": R({"status": "PAUSED", "effective_status": "PAUSED"})}
    out = states(R({"error": {"message": "Some of the aliases you requested do not exist", "code": 100}}, 400), single)
    assert out["c1"] == "PAUSED" and out["c2"] == "REMOVED"


def test_removed_campaigns_disappear_from_list(tmp_path):
    import sqlite3
    st = meta_ads.LaunchStore(sqlite3.connect(tmp_path / "d.db", check_same_thread=False))
    st.add("a", {"campaign_id": "c1", "status": "ACTIVE", "ad_sets": [], "ads": []})
    st.add("a", {"campaign_id": "c2", "status": "ACTIVE", "ad_sets": [], "ads": []})
    st.set_status("a", "c2", "REMOVED")
    assert [l["campaign_id"] for l in st.list("a")] == ["c1"]
