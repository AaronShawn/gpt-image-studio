"""Minimal, dependency-free client for OpenAI-compatible image APIs.

Works with the official OpenAI Images API as well as any third-party gateway
that speaks the same wire format:

  POST {base_url}/images/generations   -> text to image
  POST {base_url}/images/edits         -> image editing (multipart)

Only the Python standard library is used, so this module runs unchanged under
the official CPython, the Windows embeddable distribution, or the interpreter
bundled with ComfyUI.
"""

import base64
import json
import mimetypes
import os
import ssl
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-image-2.5-flare"


class APIError(Exception):
    """Raised for any non-2xx response or transport failure."""

    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__("HTTP %s: %s" % (status, str(body)[:500]))


def _opener():
    ctx = ssl.create_default_context()
    # Honours HTTP(S)_PROXY from the environment when present.
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def _post(url, data, headers, timeout):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with _opener().open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = raw
        raise APIError(e.code, body) from None
    except urllib.error.URLError as e:
        raise APIError(0, "network error: %s" % (e.reason,)) from None


def _multipart(fields, files):
    """Build a multipart/form-data body without any third-party library."""
    boundary = "----gptimagestudio" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        if v is None:
            continue
        out += ("--%s\r\n" % boundary).encode()
        out += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % k).encode()
        out += str(v).encode("utf-8") + b"\r\n"
    for name, filename, mime, blob in files:
        out += ("--%s\r\n" % boundary).encode()
        out += (
            'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
            % (name, filename)
        ).encode("utf-8")
        out += ("Content-Type: %s\r\n\r\n" % mime).encode()
        out += blob + b"\r\n"
    out += ("--%s--\r\n" % boundary).encode()
    return bytes(out), "multipart/form-data; boundary=%s" % boundary


def _download(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _opener().open(req, timeout=timeout) as resp:
        return resp.read()


def _extract(resp):
    """Return (image_bytes, extension) for either b64_json or url responses."""
    item = (resp.get("data") or [{}])[0]
    if item.get("b64_json"):
        blob = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        blob = _download(item["url"])
    else:
        raise APIError(0, "response contains neither b64_json nor url: %s" % str(resp)[:300])
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    elif blob[:2] == b"\xff\xd8":
        ext = ".jpg"
    elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        ext = ".webp"
    else:
        ext = ".png"
    return blob, ext


def list_models(base_url=DEFAULT_BASE, api_key="", timeout=30):
    """Return the model ids this endpoint exposes (GET {base_url}/models)."""
    url = (base_url or DEFAULT_BASE).rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + api_key,
        "User-Agent": "gpt-image-studio",
    })
    try:
        with _opener().open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise APIError(e.code, e.read().decode("utf-8", "replace")[:300]) from None
    except urllib.error.URLError as e:
        raise APIError(0, "network error: %s" % (e.reason,)) from None
    items = data.get("data") if isinstance(data, dict) else data
    ids = []
    for it in items or []:
        mid = it.get("id") if isinstance(it, dict) else it
        if isinstance(mid, str) and mid:
            ids.append(mid)
    return ids


def _fields(model, size, quality, background, response_format, moderation, user):
    return {
        "model": model or DEFAULT_MODEL,
        "size": size,
        "quality": quality,
        "background": background,
        "response_format": response_format or "b64_json",
        "moderation": moderation,
        "user": user,
    }


def generate(prompt, api_key, base_url=DEFAULT_BASE, model=DEFAULT_MODEL, size="auto",
             quality="high", background=None, response_format="b64_json",
             moderation=None, user=None, timeout=300):
    """Text to image. Returns dict with image / ext / usage / elapsed / raw."""
    started = time.time()
    body = _fields(model, size, quality, background, response_format, moderation, user)
    body["prompt"] = prompt
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    resp = _post(
        (base_url or DEFAULT_BASE).rstrip("/") + "/images/generations",
        data,
        {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        timeout,
    )
    blob, ext = _extract(resp)
    return {
        "image": blob,
        "ext": ext,
        "usage": resp.get("usage") or {},
        "elapsed": round(time.time() - started, 2),
        "raw": resp,
    }


def edit(prompt, images, api_key, base_url=DEFAULT_BASE, model=DEFAULT_MODEL, size="auto",
         quality="high", background=None, input_fidelity="high",
         response_format="b64_json", moderation=None, user=None, timeout=300):
    """Image editing with one or more reference images.

    images: [(filename, bytes), ...] — order matters, the first one is the subject.
    """
    if not images:
        raise ValueError("editing requires at least one reference image")
    started = time.time()
    fields = _fields(model, size, quality, background, response_format, moderation, user)
    fields["prompt"] = prompt
    fields["input_fidelity"] = input_fidelity
    files = []
    for i, (filename, blob) in enumerate(images):
        mime = mimetypes.guess_type(filename or ("ref%d.png" % i))[0] or "image/png"
        files.append(("image", filename or ("ref%d.png" % i), mime, blob))
    payload, ctype = _multipart(fields, files)
    resp = _post(
        (base_url or DEFAULT_BASE).rstrip("/") + "/images/edits",
        payload,
        {"Authorization": "Bearer " + api_key, "Content-Type": ctype},
        timeout,
    )
    blob, ext = _extract(resp)
    return {
        "image": blob,
        "ext": ext,
        "usage": resp.get("usage") or {},
        "elapsed": round(time.time() - started, 2),
        "raw": resp,
    }


def save(dest_dir, blob, ext=".png", prefix="img", meta=None):
    """Write the image plus an optional sidecar .json, return the path."""
    os.makedirs(dest_dir, exist_ok=True)
    name = "%s-%s%s" % (prefix, time.strftime("%Y%m%d-%H%M%S"), ext)
    path = os.path.join(dest_dir, name)
    with open(path, "wb") as f:
        f.write(blob)
    if meta is not None:
        try:
            with open(path + ".json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return path
