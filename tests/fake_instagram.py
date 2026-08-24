"""An in-memory stand-in for the Instagram Graph API.

The Instagram channel is built to be fully testable with no Meta credentials
-- the same reason `fake_shopify.py` exists. This is not a mock that asserts
on scripts of expected calls: it records every outbound call the client makes
(URL, headers, payload) and answers with configurable responses, so tests
exercise the real client code -- chunking, ordering, error naming, download
naming -- against a shelf that behaves like HTTP rather than like a plan.

Installed over `integrations.instagram.client`'s own `httpx` module
attribute, exactly as `test_gemini_provider.py` / `test_openrouter_provider.py`
stub theirs. No network anywhere.
"""

from __future__ import annotations

import json

from integrations.instagram import client as instagram_client


class FakeResponse:
    """Just the httpx.Response surface `InstagramClient` touches."""

    def __init__(self, status_code=200, body=None, content=b"", content_type="application/json"):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.content = content
        self.headers = {"content-type": content_type}
        if content:
            try:
                self.text = content.decode("utf-8")
            except UnicodeDecodeError:
                self.text = ""
        else:
            self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeInstagram:
    def __init__(self):
        #: Every call in order: {"method", "url", ...the kwargs the client passed}.
        self.calls: list[dict] = []
        #: Status/body every POST is answered with until one of the failure
        #: knobs below says otherwise.
        self.post_status = 200
        self.post_body: dict = {"recipient_id": "ignored"}
        #: Set to an int to make the next N POSTs fail with this status.
        self.fail_next_posts = 0
        #: The status those failed POSTs answer with.
        self.fail_status = 400
        #: Set to an exception type to raise from the next POST instead.
        self.raise_next_post: Exception | None = None
        #: What GET (attachment downloads) answers.
        self.download_status = 200
        self.download_content = b"binary-bytes"
        self.download_content_type = "image/jpeg"
        self.download_raise: Exception | None = None

    # -- install ----------------------------------------------------------

    def install(self, monkeypatch) -> FakeInstagram:
        monkeypatch.setattr(instagram_client.httpx, "post", self._post)
        monkeypatch.setattr(instagram_client.httpx, "get", self._get)
        return self

    # -- the fake transport -------------------------------------------------

    def _post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        if self.raise_next_post is not None:
            raise self.raise_next_post
        if self.fail_next_posts > 0:
            self.fail_next_posts -= 1
            return FakeResponse(
                status_code=self.fail_status,
                body={"error": {"message": "nope"}},
            )
        return FakeResponse(status_code=self.post_status, body=self.post_body)

    def _get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, **kwargs})
        if self.download_raise is not None:
            raise self.download_raise
        return FakeResponse(
            status_code=self.download_status,
            content=self.download_content,
            content_type=self.download_content_type,
        )

    # -- assertions helpers -------------------------------------------------

    @property
    def posts(self) -> list[dict]:
        return [c for c in self.calls if c["method"] == "POST"]

    def message_payloads(self) -> list[dict]:
        """The JSON bodies posted to the /messages endpoint, in order."""
        return [
            c["json"]
            for c in self.posts
            if c["url"].endswith("/messages") and "json" in c and isinstance(c["json"], dict)
        ]

    def texts(self) -> list[str]:
        out = []
        for payload in self.message_payloads():
            message = payload.get("message") or {}
            if "text" in message:
                out.append(message["text"])
        return out
