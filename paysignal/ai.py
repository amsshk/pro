"""Meta Model API client with a model router: every feature asks for a TASK,
and the router picks the model for it.

    campaign_builder -> muse-wonder-1.0   best model: plans whole campaigns
    chat             -> muse-wonder-1.0   best model: answers questions, explains results
    labelling        -> muse-spark-1.3    simple, high-volume job: strong and ~5x cheaper

If a model is unavailable or errors, the router falls back to the next one in
that task's chain, so a feature never stops working because one model is down.

Change any choice without touching code:
    PAYSIGNAL_MODELS='{"chat": ["muse-spark-1.3"]}'

API: https://api.meta.ai/v1/chat/completions, key in MODEL_API_KEY (dev.meta.ai).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("paysignal.ai")

URL = "https://api.meta.ai/v1/chat/completions"

# USD per 1M tokens (input, output), from the dev.meta.ai models page, Nov 2026
PRICES = {
    "muse-wonder-1.0": (4.00, 20.00),
    "muse-spark-1.3": (1.25, 4.25),
    "muse-spark-1.2": (1.25, 4.25),
    "muse-spark-1.1": (1.25, 4.25),
    "muse-spark-1.3-contributor": (0.10, 0.20),
}

# task -> (models to try in order, reasoning effort)
DEFAULT_ROUTES: dict[str, tuple[list[str], str]] = {
    "campaign_builder": (["muse-wonder-1.0", "muse-spark-1.3"], "high"),
    "chat":             (["muse-wonder-1.0", "muse-spark-1.3"], "medium"),
    "labelling":        (["muse-spark-1.3", "muse-spark-1.2"], "low"),
}

# Not chat models: kept here so every Meta model choice lives in one place.
MEDIA_MODELS = {
    "image_generation": "muse-image-1.0",            # ad images, $0.01 / image
    "transcription": "muse-voice-transcribe-1.0",     # speech in videos, $0.18 / hour
    "segmentation": "sam-3.1",                        # find products/people in images & video
}

# The Contributor tier is ~95% cheaper, but Meta trains on everything sent to it.
# Never allowed for client or payment data. Opt-in for labelling public captions only.
CONTRIBUTOR_OK_TASKS = {"labelling"}


@dataclass
class Reply:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class MetaAI:
    def __init__(self, api_key: Optional[str] = None, session=None, routes: Optional[dict] = None,
                 allow_contributor: Optional[bool] = None):
        self.api_key = api_key if api_key is not None else os.environ.get("MODEL_API_KEY", "")
        self.session = session
        self.routes = {k: (list(v[0]), v[1]) for k, v in DEFAULT_ROUTES.items()}
        override = os.environ.get("PAYSIGNAL_MODELS")
        if override:
            for task, models in json.loads(override).items():
                effort = self.routes.get(task, ([], "medium"))[1]
                self.routes[task] = (list(models), effort)
        for task, v in (routes or {}).items():
            self.routes[task] = (list(v[0]), v[1]) if isinstance(v, tuple) else (list(v), "medium")
        if allow_contributor is None:
            allow_contributor = os.environ.get("PAYSIGNAL_ALLOW_CONTRIBUTOR") == "1"
        self.allow_contributor = allow_contributor
        self.spent_usd = 0.0

    @property
    def available(self) -> bool:
        return bool(self.api_key) or bool(self.fallback_config())

    def models_for(self, task: str) -> tuple[list[str], str]:
        if task not in self.routes:
            raise ValueError(f"unknown task {task!r}; known: {', '.join(self.routes)}")
        models, effort = self.routes[task]
        if task in CONTRIBUTOR_OK_TASKS and self.allow_contributor:
            models = ["muse-spark-1.3-contributor"] + models
        else:   # guard: contributor models never run for other tasks, even if configured
            models = [m for m in models if not m.endswith("-contributor")]
        return models, effort

    def _http(self):
        if self.session is None:
            import requests
            self.session = requests.Session()
        return self.session

    # ---------- backup AI: used when Meta's AI is blocked (billing, outage) or not set up ----------
    @staticmethod
    def fallback_config() -> Optional[dict]:
        """Any OpenAI-compatible service: PAYSIGNAL_FALLBACK_URL (base URL ending in /v1 or similar),
        PAYSIGNAL_FALLBACK_KEY and PAYSIGNAL_FALLBACK_MODEL."""
        url, key, model = (os.environ.get(k, "").strip() for k in
                           ("PAYSIGNAL_FALLBACK_URL", "PAYSIGNAL_FALLBACK_KEY", "PAYSIGNAL_FALLBACK_MODEL"))
        cheap = os.environ.get("PAYSIGNAL_FALLBACK_CHEAP_MODEL", "").strip() or model
        return {"url": url.rstrip("/"), "key": key, "model": model, "cheap": cheap} if url and key and model else None

    def _fallback_chat(self, messages: list[dict], max_completion_tokens: int, why: str,
                       task: str = "chat", reasoning_effort: Optional[str] = None) -> Reply:
        """Same instructions and data as Meta's models get, so the backup knows ProAds' job. Planning and
        explaining use the smart model and think as hard as Muse Wonder would; labelling uses the cheap one."""
        cfg = self.fallback_config()
        effort = reasoning_effort or DEFAULT_ROUTES.get(task, DEFAULT_ROUTES["chat"])[1]
        model = cfg["cheap"] if effort == "low" else cfg["model"]
        log.warning("Meta AI unavailable (%s); using the backup AI %s", why[:200], model)
        # "developer" is Meta/OpenAI's name for the instructions; most other services only know "system"
        msgs = [{**m, "role": "system"} if m.get("role") == "developer" else m for m in messages]
        body = {"model": model, "messages": msgs, "max_tokens": min(max_completion_tokens, 32000),
                "reasoning_effort": effort}
        headers = {"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json"}
        fail = f"The backup AI ({model}) failed"
        help_ = "Check PAYSIGNAL_FALLBACK_URL, _KEY and _MODEL in Render. (Meta's AI is also unavailable.)"

        def send():
            try:
                return self._http().post(cfg["url"] + "/chat/completions", timeout=240, json=body, headers=headers)
            except Exception as e:                    # bad URL, network, timeout: never a bare server error
                raise RuntimeError(f"{fail}: couldn't reach {cfg['url']} ({type(e).__name__}). {help_}") from e
        r = send()
        if r.status_code == 400 and "reasoning" in (r.text or "").lower():
            body.pop("reasoning_effort")              # model can't take a thinking setting: ask plainly
            r = send()
        if r.status_code == 400 and "max_tokens" in (r.text or ""):
            body["max_completion_tokens"] = body.pop("max_tokens")
            r = send()
        if r.status_code >= 400:
            raise RuntimeError(f"{fail}: HTTP {r.status_code} {_reason(r)[:300]}. {help_}")
        try:
            data = r.json()
            if isinstance(data, list):
                data = data[0]
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except Exception as e:
            raise RuntimeError(f"{fail}: unreadable reply ({(r.text or '')[:200]}). {help_}") from e
        if isinstance(text, list):                    # some services return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        usage = data.get("usage") or {}
        self.last_backup = True
        return Reply(text, "backup:" + model, int(usage.get("prompt_tokens", 0) or 0),
                     int(usage.get("completion_tokens", 0) or 0), 0.0)

    def chat(self, task: str, messages: list[dict], max_completion_tokens: int = 8192,
             reasoning_effort: Optional[str] = None, **extra) -> Reply:
        """Send a chat request for a task: Meta's models first, then the backup AI if Meta's are blocked or down."""
        if not self.available:
            raise RuntimeError("MODEL_API_KEY is not set")
        self.last_backup = False
        if not self.api_key:
            return self._fallback_chat(messages, max_completion_tokens, "MODEL_API_KEY is not set", task, reasoning_effort)
        try:
            return self._meta_chat(task, messages, max_completion_tokens, reasoning_effort, **extra)
        except (PermissionError, RuntimeError) as e:
            if self.fallback_config():
                return self._fallback_chat(messages, max_completion_tokens, str(e), task, reasoning_effort)
            raise

    def _meta_chat(self, task: str, messages: list[dict], max_completion_tokens: int = 8192,
                   reasoning_effort: Optional[str] = None, **extra) -> Reply:
        models, effort = self.models_for(task)
        errors: list[str] = []
        for model in models:
            body = {"model": model, "messages": messages,
                    # Muse models always reason; the reasoning counts toward this limit
                    "max_completion_tokens": max_completion_tokens,
                    "reasoning_effort": reasoning_effort or effort, **extra}
            try:
                r = self._http().post(URL, timeout=180, json=body, headers={
                    "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
            except Exception as e:                 # network problem: try the next model
                log.warning("model %s unreachable for %s (%s), trying next", model, task, e)
                errors.append(f"{model}: unreachable ({e})")
                continue
            if r.status_code in (401, 403):        # bad key or no access: other models won't help
                raise PermissionError(f"Meta Model API refused the key (HTTP {r.status_code}): {_reason(r)}")
            if r.status_code == 402:               # billing problem: same for every model
                raise PermissionError(f"Meta Model API billing problem: {_reason(r)}")
            if r.status_code >= 400:               # model unavailable, bad request, overloaded
                why = f"{model}: HTTP {r.status_code} {_reason(r)}"
                log.warning("model failed for %s (%s), trying next", task, why)
                errors.append(why)
                continue
            try:
                data = r.json()
                text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                usage = data.get("usage") or {}
                tin, tout = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
                pin, pout = PRICES.get(model, (0.0, 0.0))
                cost = (tin * pin + tout * pout) / 1_000_000
                self.spent_usd += cost
                return Reply(text, model, tin, tout, round(cost, 6))
            except Exception as e:                 # reply was not the expected JSON
                why = f"{model}: unreadable reply ({e})"
                log.warning("model failed for %s (%s), trying next", task, why)
                errors.append(why)
        raise RuntimeError(f"all models failed for task {task}: " + " | ".join(errors))


    # ---------- images ----------
    def describe_image(self, jpeg: bytes, context: str = "") -> str:
        """A precise visual description of a photo, so a similar one can be generated."""
        import base64
        url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        prompt = ("Describe this photo for an image generator so it can recreate a very similar, high-quality version. "
                  "Be exact about the main subject: shape, colours, materials, text or labels visible, packaging, "
                  "setting, lighting and camera angle. Do not add anything that is not in the photo. One paragraph. "
                  + (f"Context: {context}" if context else ""))
        reply = self.chat("chat", [{"role": "user", "content": [{"type": "text", "text": prompt},
                                                                 {"type": "image_url", "image_url": {"url": url}}]}],
                          max_completion_tokens=2000, reasoning_effort="low")
        if not reply.text.strip():
            raise RuntimeError("the model returned no description")
        return reply.text.strip()

    def generate_image(self, prompt: str, size: str = "1080x1080") -> bytes:
        """One image from Meta's image model (muse-image). Address and model can be changed with
        PAYSIGNAL_IMAGE_URL / PAYSIGNAL_IMAGE_MODEL if Meta's API differs."""
        import base64
        if not self.available:
            raise RuntimeError("MODEL_API_KEY is not set")
        url = os.environ.get("PAYSIGNAL_IMAGE_URL", "https://api.meta.ai/v1/images/generations")
        model = os.environ.get("PAYSIGNAL_IMAGE_MODEL", MEDIA_MODELS["image_generation"])
        r = self._http().post(url, timeout=180, json={"model": model, "prompt": prompt[:3500], "n": 1, "size": size,
                                                      "response_format": "b64_json"},
                              headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        if r.status_code in (401, 403):
            raise PermissionError(f"Meta Model API refused image generation (HTTP {r.status_code}): {_reason(r)}")
        if r.status_code == 402:
            raise PermissionError(f"Meta Model API billing problem: {_reason(r)}")
        if r.status_code >= 400:
            raise RuntimeError(f"image generation failed: HTTP {r.status_code} {_reason(r)}")
        item = ((r.json() or {}).get("data") or [{}])[0]
        if item.get("b64_json"):
            self.spent_usd += 0.01
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            got = self._http().get(item["url"], timeout=60)
            if got.status_code == 200:
                self.spent_usd += 0.01
                return got.content
        raise RuntimeError("image generation returned no image")


def _reason(r) -> str:
    """Meta's own error message, shortened. Never contains our key."""
    try:
        body = r.json()
        if isinstance(body, list) and body:
            body = body[0]
        err = body.get("error", body) if isinstance(body, dict) else body
        msg = err.get("message") if isinstance(err, dict) else None
        return str(msg or err)[:300]
    except Exception:
        return (getattr(r, "text", "") or "")[:300]
