"""Uploading bytes to Shopify: the staged-upload dance, in one place.

Shopify never takes a file in the same call that uses it. It hands out a
signed target, the bytes go straight there over plain HTTP, and only then does
a mutation get told where they landed. That is three steps for every picture,
and this module is the one implementation of them -- `size_charts.py` (the
CLI's twelve diagrams) and `admin_products.py` (a product photo a staff member
picked off their laptop) both come through here.

Two destinations, and the difference matters:

- **Files** (`upload_to_files`) -- the shop's file library. Returns a gid,
  which is what a `file_reference` metafield stores, and a CDN url. This is
  where a size chart goes: it is referenced by a metafield, not owned by a
  product.
- **Staged only** (`stage`) -- returns the resource url and stops. A product
  photo goes this way, because `productCreateMedia` takes the staged url
  directly and the picture then belongs to the product rather than sitting
  loose in the library as well.

**A freshly created file has no url yet.** Shopify processes an upload
asynchronously, so `fileCreate` answers with `UPLOADED_NOT_PROCESSED` and a
null `image.url` for the first second or so. `poll_url` waits for it, briefly,
and gives up rather than blocking a dashboard request -- a caller that gets
None still has the gid, which is the part Shopify needs.
"""

from __future__ import annotations

import io
import logging
import time

import httpx

log = logging.getLogger("wanas.shopify.files")


class FileUploadError(RuntimeError):
    """Shopify refused an upload -- a bad mime type, a missing scope, a file
    too large. Distinct from `ShopifyUnavailable`: the store answered."""


STAGED_UPLOADS = """
mutation($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}
"""

FILE_CREATE = """
mutation($files: [FileCreateInput!]!) {
  fileCreate(files: $files) {
    files {
      id
      fileStatus
      ... on MediaImage { image { url } }
    }
    userErrors { field message }
  }
}
"""

FILE_URL_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on MediaImage { id fileStatus image { url } }
    ... on GenericFile { id fileStatus url }
  }
}
"""

#: How long `poll_url` will wait for Shopify to finish processing an upload.
#: Short on purpose: a staff member is watching a spinner, and the url is a
#: convenience -- the gid is what the metafield actually needs.
POLL_TRIES = 6
POLL_DELAY = 0.6


def _fail(block: dict | None, what: str) -> dict:
    if block is None:
        raise FileUploadError(f"Shopify returned no {what} block")
    errors = block.get("userErrors") or []
    if errors:
        raise FileUploadError("; ".join(e.get("message", "") for e in errors))
    return block


def stage(client, filename: str, data: bytes, mime: str, *, resource: str = "FILE") -> str:
    """Push `data` to a signed target and return the resource url.

    `resource` is Shopify's own vocabulary: `FILE` for the file library,
    `IMAGE` for something a product will own as media.
    """
    staged = client(
        STAGED_UPLOADS,
        {
            "input": [
                {
                    "resource": resource,
                    "filename": filename,
                    "mimeType": mime,
                    "httpMethod": "POST",
                    "fileSize": str(len(data)),
                }
            ]
        },
    )
    block = _fail(staged.get("stagedUploadsCreate"), "stagedUploadsCreate")
    target = (block.get("stagedTargets") or [None])[0]
    if not target:
        raise FileUploadError(f"Shopify offered no upload target for {filename}")

    form = {p["name"]: p["value"] for p in target.get("parameters") or []}
    response = httpx.post(
        target["url"],
        data=form,
        files={"file": (filename, io.BytesIO(data), mime)},
        timeout=120.0,
    )
    if response.status_code >= 400:
        raise FileUploadError(f"upload of {filename} failed: HTTP {response.status_code}")
    return target["resourceUrl"]


def upload_to_files(client, filename: str, data: bytes, mime: str, *, alt: str = "") -> dict:
    """Put `data` in the shop's file library. Returns `{"id", "url"}`.

    `url` is None when Shopify has not finished processing in the second this
    is willing to wait -- see the module docstring.
    """
    resource_url = stage(client, filename, data, mime, resource="FILE")
    created = client(
        FILE_CREATE,
        {
            "files": [
                {
                    "alt": alt or filename,
                    "contentType": "IMAGE",
                    "originalSource": resource_url,
                }
            ]
        },
    )
    block = _fail(created.get("fileCreate"), "fileCreate")
    files = block.get("files") or []
    if not files:
        raise FileUploadError(f"Shopify accepted {filename} but returned no file")

    gid = files[0]["id"]
    url = ((files[0].get("image") or {}).get("url")) or None
    if url is None:
        url = poll_url(client, gid)
    return {"id": gid, "url": url}


def poll_url(client, gid: str, *, tries: int = POLL_TRIES, delay: float = POLL_DELAY) -> str | None:
    """The CDN url of a file Shopify is still processing, or None."""
    for attempt in range(tries):
        if attempt:
            time.sleep(delay)
        data = client(FILE_URL_QUERY, {"ids": [gid]})
        for node in data.get("nodes") or []:
            if not node or node.get("id") != gid:
                continue
            url = ((node.get("image") or {}).get("url")) or node.get("url")
            if url:
                return url
    log.info("Shopify has not finished processing %s yet; storing the gid without a url", gid)
    return None


__all__ = ["FileUploadError", "stage", "upload_to_files", "poll_url"]
