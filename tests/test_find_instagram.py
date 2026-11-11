from paysignal import meta_connect


class R:
    def __init__(self, body, status=200):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


class Http:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, params=None, timeout=None):
        for k, v in self.routes.items():
            if k in url:
                return v
        return R({"error": {"message": "nope"}}, 400)


def test_found_on_page():
    h = Http({"/895": R({"id": "895", "instagram_business_account": {"id": "1784", "username": "proads"}})})
    assert meta_connect.find_instagram(h, "t", "895", "act_819")["id"] == "1784"


def test_falls_back_to_ad_account():
    h = Http({"/895": R({"id": "895"}), "act_819/instagram_accounts": R({"data": [{"id": "1784", "username": "p"}]})})
    r = meta_connect.find_instagram(h, "t", "895", "819")
    assert r["id"] == "1784" and r["how"] == "connected to the ad account"


def test_explains_when_missing():
    h = Http({"/895": R({"id": "895"}), "instagram_accounts": R({"data": []})})
    r = meta_connect.find_instagram(h, "t", "895", "act_819")
    assert "no Instagram" in r["why"] and "id" not in r
