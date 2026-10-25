"""Minimal, dependency-free client for OpenAI-compatible image APIs.

Two wire formats are supported, both with the standard library only:

  GPT Image / any OpenAI-compatible gateway
    POST {base_url}/images/generations   -> text to image
    POST {base_url}/images/edits         -> image editing (multipart)

  Gemini image models ("Nano Banana") over the OpenAI-compatible chat route
    POST {base_url}/chat/completions     -> text + reference images in, image out
    (the gateway answers with a markdown data-URL inside message.content)

Only the Python standard library is used, so this module runs unchanged under
the official CPython, the Windows embeddable distribution, or the interpreter
bundled with ComfyUI.
"""

import base64
import json
import mimetypes
import os
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-image-2.5-flare"
DEFAULT_NANO_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_NANO_MODEL = "gemini-3.1-flash-image"

# ![image](data:image/jpeg;base64,...)  —— 网关把结果塞在 assistant 文本里
_MD_IMG = re.compile(
    r"!\[[^\]]*\]\(\s*(data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+|https?://[^)\s]+)\s*\)"
)
_DATA_IMG = re.compile(r"data:image/([a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)")


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
    except json.JSONDecodeError as e:
        raise APIError(0, "response is not JSON: %s" % e) from None


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


def _download(url, timeout=120, retries=2):
    """Fetch the image from the temporary CDN link.

    Some gateways reject requests without a browser-ish User-Agent, and slow
    links occasionally drop mid-transfer, so retry a couple of times.
    """
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _opener().open(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise APIError(0, "download failed after %d tries: %s" % (retries + 1, last))


def sniff_ext(blob):
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if blob[:2] == b"\xff\xd8":
        return ".jpg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".png"


def _extract(resp):
    """Return (image_bytes, extension) for either b64_json or url responses."""
    item = (resp.get("data") or [{}])[0]
    if item.get("b64_json"):
        blob = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        blob = _download(item["url"])
    else:
        raise APIError(0, "response contains neither b64_json nor url: %s" % str(resp)[:300])
    return blob, sniff_ext(blob)


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


# ---------------------------------------------------------------- Nano Banana
def _data_url(blob, filename="ref.png"):
    mime = mimetypes.guess_type(filename or "ref.png")[0] or "image/png"
    return "data:%s;base64,%s" % (mime, base64.b64encode(blob).decode("ascii"))


def _b64_blob(s):
    return base64.b64decode("".join(s.split()))


def _from_url(url, timeout=120):
    if url.startswith("data:"):
        return _b64_blob(url.split(",", 1)[1])
    return _download(url, timeout=timeout)


def _text_of(content):
    """Flatten an OpenAI chat `content` (string or part list) into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text") or "" for p in content if isinstance(p, dict))
    return ""


def _images_of(content, timeout=120):
    """Pull every image out of an assistant message (data URLs, links, parts)."""
    found = []
    if isinstance(content, str) or content is None:
        text = content or ""
    else:
        text = _text_of(content)
        for p in content if isinstance(content, list) else []:
            if not isinstance(p, dict):
                continue
            url = (p.get("image_url") or {}).get("url") if isinstance(p.get("image_url"), dict) else p.get("image_url")
            if isinstance(url, str) and (url.startswith("data:") or url.startswith("http")):
                found.append(_from_url(url, timeout))
            inline = p.get("inline_data") or p.get("inlineData")
            if isinstance(inline, dict) and inline.get("data"):
                found.append(_b64_blob(inline["data"]))
    if text:
        for m in _MD_IMG.finditer(text):
            found.append(_from_url(m.group(1), timeout))
        if not found:
            for m in _DATA_IMG.finditer(text):
                found.append(_b64_blob(m.group(2)))
    return found


def nano_chat(prompt, images=(), api_key="", base_url=DEFAULT_NANO_BASE,
              model=DEFAULT_NANO_MODEL, aspect=None, timeout=300):
    """Generate or edit through a Gemini image model on the chat route.

    images: [(filename, bytes), ...] — optional, sent as multimodal content.
    aspect: kept for the record only; these models read the ratio from the
            prompt text, and most gateways drop image_config/aspect_ratio.

    Returns dict with image / ext / usage / elapsed / text / raw. `text` is
    whatever the model wrote next to the picture (often empty).
    """
    started = time.time()
    parts = [{"type": "text", "text": prompt}]
    for i, (filename, blob) in enumerate(images or []):
        parts.append({"type": "image_url",
                      "image_url": {"url": _data_url(blob, filename or ("ref%d.png" % i))}})
    body = {
        "model": model or DEFAULT_NANO_MODEL,
        "messages": [{"role": "user", "content": parts}],
        "modalities": ["text", "image"],
    }
    resp = _post(
        (base_url or DEFAULT_NANO_BASE).rstrip("/") + "/chat/completions",
        json.dumps(body, ensure_ascii=False).encode("utf-8"),
        {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        timeout,
    )
    blobs = []
    choices = resp.get("choices") or []
    msg = (choices[0].get("message") if choices else {}) or {}
    blobs.extend(_images_of(msg.get("content"), timeout))
    extra = msg.get("images")
    if isinstance(extra, list) and extra:
        blobs.extend(_images_of(extra, timeout))
    if not blobs and resp.get("data"):  # some gateways answer in Images format
        blobs.append(_from_url((resp["data"][0] or {}).get("url") or
                               "data:%s" % (resp["data"][0] or {}).get("b64_json", ""), timeout))
    if not blobs:
        raise APIError(0, "no image in the reply: %s" % str(resp)[:400])
    note = _text_of(msg.get("content")).strip()
    note = _MD_IMG.sub("", note).strip()
    return {
        "image": blobs[0],
        "ext": sniff_ext(blobs[0]),
        "usage": resp.get("usage") or {},
        "elapsed": round(time.time() - started, 2),
        "text": note,
        "raw": resp,
    }


def save(dest_dir, blob, ext=".png", prefix="img", meta=None):
    """Write the image plus an optional sidecar .json, return the path.

    Concurrent workers often finish in the same second, so the filename is
    claimed with O_EXCL and a short random suffix is used on collision —
    otherwise two threads would fight over the same path on Windows.
    """
    os.makedirs(dest_dir, exist_ok=True)
    stem = "%s-%s" % (prefix, time.strftime("%Y%m%d-%H%M%S"))
    path = None
    for attempt in range(50):
        suffix = "" if attempt == 0 else "-%s" % uuid.uuid4().hex[:4]
        candidate = os.path.join(dest_dir, stem + suffix + ext)
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        path = candidate
        break
    if path is None:
        raise OSError("could not allocate a unique filename in %s" % dest_dir)
    if meta is not None:
        try:
            with open(path + ".json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return path
