from paysignal import ai


class R:
    def __init__(self, status, body):
        self.status_code, self._b = status, body
        self.text = str(body)

    def json(self):
        return self._b


class Http:
    def __init__(self, meta_status):
        self.meta_status, self.calls = meta_status, []

    def post(self, url, timeout=None, json=None, headers=None):
        self.calls.append((url, json, headers))
        if "backup.example" in url:
            return R(200, {"choices": [{"message": {"content": "from backup"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
        return R(self.meta_status, {"error": {"message": "Your account is restricted"}} if self.meta_status >= 400 else
                 {"choices": [{"message": {"content": "from meta"}}], "usage": {}})


def setup(monkeypatch, meta_status, backup=True, key="k"):
    if backup:
        monkeypatch.setenv("PAYSIGNAL_FALLBACK_URL", "https://backup.example/v1/")
        monkeypatch.setenv("PAYSIGNAL_FALLBACK_KEY", "bk")
        monkeypatch.setenv("PAYSIGNAL_FALLBACK_MODEL", "small-model")
    http = Http(meta_status)
    m = ai.MetaAI(api_key=key, session=http)
    return m, http


def test_meta_first_when_it_works(monkeypatch):
    m, http = setup(monkeypatch, 200)
    r = m.chat("chat", [{"role": "user", "content": "hi"}])
    assert r.text == "from meta" and not m.last_backup and len(http.calls) == 1


def test_billing_block_switches_to_backup(monkeypatch):
    m, http = setup(monkeypatch, 402)
    r = m.chat("chat", [{"role": "user", "content": "hi"}])
    assert r.text == "from backup" and r.model == "backup:small-model" and m.last_backup
    url, body, headers = http.calls[-1]
    assert url == "https://backup.example/v1/chat/completions" and body["model"] == "small-model"
    assert headers["Authorization"] == "Bearer bk"


def test_restricted_account_403_also_switches(monkeypatch):
    m, _ = setup(monkeypatch, 403)
    assert m.chat("chat", [{"role": "user", "content": "hi"}]).text == "from backup"


def test_no_backup_keeps_the_original_error(monkeypatch):
    m, _ = setup(monkeypatch, 402, backup=False)
    try:
        m.chat("chat", [{"role": "user", "content": "hi"}])
        assert False
    except PermissionError as e:
        assert "billing" in str(e)


def test_backup_alone_works_without_meta_key(monkeypatch):
    m, http = setup(monkeypatch, 200, key="")
    assert m.available and m.chat("chat", [{"role": "user", "content": "hi"}]).text == "from backup"


def test_campaign_planning_uses_smart_model_and_thinks_hard(monkeypatch):
    monkeypatch.setenv("PAYSIGNAL_FALLBACK_CHEAP_MODEL", "tiny")
    m, http = setup(monkeypatch, 402)
    m.chat("campaign_builder", [{"role": "system", "content": "You are ProAds"}, {"role": "user", "content": "plan"}])
    body = http.calls[-1][1]
    assert body["model"] == "small-model" and body["reasoning_effort"] == "high"
    assert body["messages"][0]["content"] == "You are ProAds"      # same instructions Muse gets
    m.chat("labelling", [{"role": "user", "content": "x"}])
    assert http.calls[-1][1]["model"] == "tiny"


def test_retries_without_reasoning_when_model_refuses_it(monkeypatch):
    m, http = setup(monkeypatch, 402)
    orig = http.post
    def post(url, timeout=None, json=None, headers=None):
        if "backup.example" in url and "reasoning_effort" in json:
            http.calls.append((url, dict(json), headers))
            return R(400, {"error": "Unrecognized request argument: reasoning_effort"})
        return orig(url, timeout=timeout, json=json, headers=headers)
    http.post = post
    assert m.chat("chat", [{"role": "user", "content": "hi"}]).text == "from backup"


def test_developer_role_sent_as_system_and_network_errors_are_clear(monkeypatch):
    m, http = setup(monkeypatch, 402)
    m.chat("campaign_builder", [{"role": "developer", "content": "rules"}, {"role": "user", "content": "x"}])
    assert http.calls[-1][1]["messages"][0]["role"] == "system"
    orig = http.post
    def post(url, **kw):
        if "backup.example" in url:
            raise ConnectionError("dns")
        return orig(url, **kw)
    http.post = post
    try:
        m.chat("chat", [{"role": "user", "content": "hi"}])
        assert False
    except RuntimeError as e:
        assert "couldn't reach" in str(e)
