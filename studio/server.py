"""Local web workbench for OpenAI-compatible image APIs.

    python server.py                     # http://127.0.0.1:8756
    python server.py --port 9000 --open  # custom port, open the browser

Turns clicks in the browser into image API calls, stores the results under
../outputs, and keeps the API keys in a local config.json only — they are never
sent to the page and never embedded in the HTML.

Two providers share one workbench:

  gpt   OpenAI-style Images API   POST /images/generations | /images/edits
  nano  Gemini image models       POST /chat/completions  (modalities: text+image)
"""

import argparse
import base64
import json
import mimetypes
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(ROOT)
sys.path.insert(0, os.path.join(PROJ, "core"))

import image_api as api  # noqa: E402

CONFIG_PATH = os.path.join(PROJ, "config.json")
OUT_DIR = os.path.join(PROJ, "outputs")

PROVIDERS = ("gpt", "nano")
PROVIDER_LABEL = {"gpt": "GPT Image", "nano": "Nano Banana"}

DEFAULT_GPT = {"base_url": api.DEFAULT_BASE, "api_key": "", "model": api.DEFAULT_MODEL}
DEFAULT_NANO = {"base_url": api.DEFAULT_NANO_BASE, "api_key": "", "model": api.DEFAULT_NANO_MODEL}

# 每次调用的参考价（元）。仅用于界面显示，来自服务商公开定价页；
# 换网关后价格会变，可以直接在 config.json 里用 "prices": {"模型名": 数字} 覆盖。
PRICES_CNY = {
    "gemini-3.1-flash-image": 0.18,
    "gemini-3.1-flash-image-preview": 0.181,
    "gemini-3-pro-image-preview": 0.288,
    "gemini-nano-banana-2.1": 0.18,
    "gemini-2.5-flash-image": 0.048,
    "gemini-2.5-flash-image-preview": 0.06,
}
DEFAULT = {
    "provider": "gpt",
    "max_parallel": 3,
    "gpt": dict(DEFAULT_GPT),
    "nano": dict(DEFAULT_NANO),
    "prices": {},
}
LOCK = threading.Lock()

# ---------- 并发闸门 ----------
# 上限只有两档：默认 3 路；页面里勾上「允许 5 路并发」才放开到 5 路。
PARALLEL_CHOICES = (3, 5)
PARALLEL_DEFAULT = 3
MAX_WORKERS = max(PARALLEL_CHOICES)

# ---------- 任务队列 ----------
JOBS = {}
JOB_ORDER = []
JOBS_LOCK = threading.Lock()
TASK_Q = queue.Queue()
MAX_JOBS = 40
RECENT = {}  # 防抖：(provider, mode, model, prompt, count, size, quality, 参考图数) -> (ts, job_id)

# ---------- 模型列表缓存 ----------
MODELS_CACHE = {}  # (base_url, api_key) -> (ts, ids)
MODELS_TTL = 180
CONFIG_LOCK = threading.Lock()  # config.json 的读-改-写要串行，两套接口会并发拉模型


def gateway_models(base_url, api_key, timeout=30):
    """GET {base}/models with a short cache — the page asks on every provider switch."""
    key = (base_url, api_key)
    now = time.time()
    with LOCK:
        hit = MODELS_CACHE.get(key)
        if hit and now - hit[0] < MODELS_TTL:
            return hit[1]
    ids = api.list_models(base_url, api_key, timeout=timeout)
    with LOCK:
        MODELS_CACHE[key] = (now, ids)
    return ids


def looks_image(model_id):
    m = (model_id or "").lower()
    return any(t in m for t in ("image", "banana", "imagen", "dall-e", "flux", "seedream", "grok-2-image"))


class Gate:
    """可随时改上限的并发闸门（信号量）。

    worker 线程在开跑前 acquire()，跑完 release()。页面里切换勾选框时
    调用 set_limit()，正在跑的任务不会被中断，只是新任务能不能进来变了。
    """

    def __init__(self, limit):
        self._cond = threading.Condition()
        self._limit = max(1, int(limit))
        self._used = 0
        self._waiting = 0

    def acquire(self):
        with self._cond:
            self._waiting += 1
            try:
                while self._used >= self._limit:
                    self._cond.wait()
            finally:
                self._waiting -= 1
            self._used += 1

    def release(self):
        with self._cond:
            if self._used > 0:
                self._used -= 1
            self._cond.notify()

    def set_limit(self, limit):
        with self._cond:
            self._limit = max(1, int(limit))
            self._cond.notify_all()
            return self._limit

    def snapshot(self):
        with self._cond:
            return {"limit": self._limit, "running": self._used, "waiting": self._waiting}


GATE = Gate(PARALLEL_DEFAULT)


def norm_parallel(value, fallback=PARALLEL_DEFAULT):
    """只接受 3 / 5 两档，其他值夹到最近的合法档。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return fallback
    return min(PARALLEL_CHOICES, key=lambda c: (abs(c - n), c))


def norm_provider(value, fallback="gpt"):
    return value if value in PROVIDERS else fallback


def _job_public(job):
    return {
        "id": job["id"], "status": job["status"], "mode": job["mode"],
        "provider": job["provider"], "model": job["model"], "price": job["price"],
        "prompt": job["prompt"], "total": job["total"], "done": job["done"],
        "failed": job["failed"], "size": job["size"], "quality": job["quality"],
        "items": list(job["items"]), "errors": list(job["errors"])[:3],
        "created": job["created"], "finished": job["finished"],
    }


def list_jobs():
    with JOBS_LOCK:
        return [_job_public(JOBS[i]) for i in reversed(JOB_ORDER) if i in JOBS]


def worker_loop():
    while True:
        task = TASK_Q.get()
        if task is None:
            return
        jid, payload = task
        with JOBS_LOCK:
            job = JOBS.get(jid)
            if job is None:
                TASK_Q.task_done()
                continue
        # 等一个并发名额；名额由页面上的勾选框决定（3 或 5）
        GATE.acquire()
        try:
            with JOBS_LOCK:
                job = JOBS.get(jid)
                if job is None:
                    continue
                job["status"] = "running"
            try:
                opts = dict(payload["opts"])
                if payload["provider"] == "nano":
                    res = api.nano_chat(payload["prompt"], payload["files"], payload["api_key"], **opts)
                elif payload["mode"] == "edit":
                    res = api.edit(payload["prompt"], payload["files"], payload["api_key"], **opts)
                else:
                    res = api.generate(payload["prompt"], payload["api_key"], **opts)
                meta = {
                    "type": payload["mode"], "provider": payload["provider"],
                    "model": opts.get("model"), "prompt": payload["prompt"],
                    "size": opts.get("size") or opts.get("aspect"),
                    "quality": opts.get("quality"), "usage": res["usage"],
                    "elapsed": res["elapsed"], "job": jid, "price": job["price"],
                    "refs": [f[0] for f in payload["files"]],
                    "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                if res.get("note"):
                    meta["note"] = res["note"]
                path = api.save(OUT_DIR, res["image"], res["ext"], payload["mode"], meta)
                item = {"file": os.path.basename(path), "url": "/outputs/" + os.path.basename(path),
                        "elapsed": res["elapsed"], "usage": res["usage"]}
                with JOBS_LOCK:
                    job["items"].append(item)
                    job["done"] += 1
            except Exception as e:  # noqa: BLE001
                with JOBS_LOCK:
                    job["errors"].append("%s: %s" % (type(e).__name__, e))
                    job["failed"] += 1
            finally:
                with JOBS_LOCK:
                    job = JOBS.get(jid)
                    if job and job["done"] + job["failed"] >= job["total"]:
                        job["status"] = "failed" if (job["failed"] and not job["done"]) else "done"
                        job["finished"] = time.time()
        finally:
            GATE.release()
            TASK_Q.task_done()


def load_config():
    """Read config.json, migrating the old flat layout (base_url/api_key/model)."""
    cfg = {
        "provider": DEFAULT["provider"],
        "max_parallel": DEFAULT["max_parallel"],
        "gpt": dict(DEFAULT_GPT),
        "nano": dict(DEFAULT_NANO),
        "prices": {},
    }
    raw = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    # v1 的配置文件是平铺的：{base_url, api_key, model, max_parallel}
    if "gpt" not in raw and any(k in raw for k in ("base_url", "api_key", "model")):
        raw = dict(raw)
        raw["gpt"] = {"base_url": raw.get("base_url"), "api_key": raw.get("api_key"),
                      "model": raw.get("model")}
    for p in PROVIDERS:
        section = raw.get(p) or {}
        for k in ("base_url", "api_key", "model"):
            v = section.get(k)
            if isinstance(v, str) and v.strip():
                cfg[p][k] = v.strip()
    for p in PROVIDERS:
        cfg[p]["base_url"] = cfg[p]["base_url"].rstrip("/") or (
            DEFAULT_GPT if p == "gpt" else DEFAULT_NANO)["base_url"]
    cfg["provider"] = norm_provider(raw.get("provider"))
    cfg["max_parallel"] = norm_parallel(raw.get("max_parallel"), PARALLEL_DEFAULT)
    cfg["prices"] = normalize_prices(raw.get("prices"))
    return cfg


def guess_provider(model_id):
    """老配置里的价格是平铺的，按模型名猜它属于哪套接口。"""
    m = str(model_id or "").lower()
    if any(t in m for t in ("gemini", "banana", "imagen")):
        return "nano"
    return "gpt"


def normalize_prices(raw):
    """价格按 provider 分组存：{"gpt": {...}, "nano": {...}}；同时兼容旧的平铺写法。"""
    prices = {p: {} for p in PROVIDERS}
    if not isinstance(raw, dict):
        return prices
    for key, val in raw.items():
        if key in PROVIDERS and isinstance(val, dict):
            for mid, pv in val.items():
                if isinstance(pv, (int, float)) and pv > 0:
                    prices[key][str(mid)] = float(pv)
        elif isinstance(val, (int, float)) and val > 0:
            prices[guess_provider(key)][str(key)] = float(val)
    return prices


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def mask(key):
    if not key:
        return ""
    if len(key) <= 10:
        return key[:3] + "***"
    return key[:6] + "*" * 8 + key[-4:]


def custom_prices(cfg):
    """只返回用户自己填的价格（平铺），界面上用来区分「自定义」和「内置默认」。"""
    out = {}
    for p in PROVIDERS:
        out.update((cfg.get("prices") or {}).get(p) or {})
    return out


def price_map(cfg):
    prices = dict(PRICES_CNY)
    prices.update(custom_prices(cfg))
    return prices


def prune_prices(provider, model_ids):
    """拉取模型列表后对账：把已经不存在的模型的价格删掉。

    只在拿到非空列表时动手，避免网关抽风返回空列表把价格全清空。
    读-改-写全程持锁并重新读盘：两套接口的模型列表是并发拉的，
    各自 prune 一次，不重新读就会互相把对方刚删的东西写回去。
    返回 (dropped, prices, prices_custom)。
    """
    if not model_ids:
        return [], None, None
    with CONFIG_LOCK:
        cfg = load_config()
        custom = (cfg.get("prices") or {}).get(provider) or {}
        dropped = sorted(set(custom) - set(model_ids))
        if dropped:
            cfg["prices"][provider] = {m: v for m, v in custom.items() if m in model_ids}
            save_config(cfg)
        return dropped, price_map(cfg), custom_prices(cfg)


def public_config(cfg):
    return {
        "provider": cfg["provider"],
        "providers": {
            p: {
                "label": PROVIDER_LABEL[p],
                "base_url": cfg[p]["base_url"],
                "model": cfg[p]["model"],
                "has_key": bool(cfg[p]["api_key"]),
                "key_masked": mask(cfg[p]["api_key"]),
            }
            for p in PROVIDERS
        },
        "max_parallel": cfg["max_parallel"],
        "parallel_choices": list(PARALLEL_CHOICES),
        "parallel": GATE.snapshot(),
        "prices": price_map(cfg),
        "prices_custom": custom_prices(cfg),
        "prices_builtin": dict(PRICES_CNY),
    }


def read_body(handler):
    n = int(handler.headers.get("Content-Length") or 0)
    if not n:
        return {}
    return json.loads(handler.rfile.read(n).decode("utf-8"))


def data_url_to_bytes(s):
    if "," in s and s.strip().startswith("data:"):
        head, b64 = s.split(",", 1)
        mime = head[5:].split(";")[0] or "image/png"
        return b64, mime
    return s, "image/png"


def open_in_file_manager(path):
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])  # noqa: S603,S607
    else:
        subprocess.Popen(["xdg-open", path])  # noqa: S603,S607


class Server(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 允许两个进程抢同一端口，必须关掉，
    # 否则第二次启动会"成功"但请求被另一个实例吃掉。
    allow_reuse_address = False
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "GPTImageStudio/1.2"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---------- helpers ----------
    def send_json(self, obj, code=200):
        blob = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def send_file(self, path, download=False):
        if not os.path.isfile(path):
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        if download:
            self.send_header(
                "Content-Disposition", 'attachment; filename="%s"' % os.path.basename(path)
            )
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # ---------- routes ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self.send_file(os.path.join(ROOT, "index.html"))
        elif path.startswith("/outputs/"):
            name = os.path.basename(path)
            self.send_file(os.path.join(OUT_DIR, name), download="dl=1" in self.path)
        elif path == "/api/config":
            self.send_json(public_config(load_config()))
        elif path == "/api/history":
            self.send_json({"items": list_outputs()})
        elif path == "/api/jobs":
            self.send_json({"jobs": list_jobs(), "pending": TASK_Q.qsize(), "parallel": GATE.snapshot()})
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/config":
                body = read_body(self)
                with CONFIG_LOCK:
                    cfg = load_config()
                    # body.provider 指「要改哪一段配置」；只有 activate 才顺带记住当前界面用的是哪套
                    if body.get("activate"):
                        cfg["provider"] = norm_provider(body.get("provider"), cfg["provider"])
                    prov = norm_provider(body.get("provider") or cfg["provider"])
                    section = cfg[prov]
                    if body.get("base_url"):
                        section["base_url"] = body["base_url"].strip().rstrip("/")
                    if body.get("model"):
                        section["model"] = body["model"].strip()
                    if body.get("api_key"):
                        section["api_key"] = body["api_key"].strip()
                        with LOCK:
                            MODELS_CACHE.clear()  # 换 Key 后模型列表要重新拉
                    if body.get("clear_key"):
                        section["api_key"] = ""
                        with LOCK:
                            MODELS_CACHE.clear()
                    if "max_parallel" in body:
                        cfg["max_parallel"] = norm_parallel(body.get("max_parallel"))
                        GATE.set_limit(cfg["max_parallel"])
                    save_config(cfg)
                return self.send_json(dict(public_config(cfg), ok=True))

            if path == "/api/models":
                body = read_body(self)
                cfg = load_config()
                prov = norm_provider(body.get("provider") or cfg["provider"])
                base = (body.get("base_url") or cfg[prov]["base_url"]).strip().rstrip("/")
                key = (body.get("api_key") or cfg[prov]["api_key"]).strip()
                if not key:
                    return self.send_json({"ok": False, "error": "请先填写 API Key", "provider": prov}, 200)
                ids = gateway_models(base, key, timeout=int(body.get("timeout") or 30))
                dropped, prices, custom = prune_prices(prov, ids)  # 下架的模型，价格跟着删
                ids.sort(key=lambda m: (0 if looks_image(m) else 1, m))
                return self.send_json({
                    "ok": True, "provider": prov,
                    "models": [{"id": m, "image": looks_image(m)} for m in ids],
                    "dropped_prices": dropped,
                    "prices": prices if prices is not None else price_map(cfg),
                    "prices_custom": custom if custom is not None else custom_prices(cfg),
                })

            if path == "/api/prices":
                body = read_body(self)
                prov = norm_provider(body.get("provider") or load_config()["provider"])
                clean = {}
                for mid, val in (body.get("prices") or {}).items():
                    if val in (None, ""):
                        continue
                    try:
                        f = float(val)
                    except (TypeError, ValueError):
                        continue
                    if f > 0:
                        clean[str(mid)] = round(f, 4)
                with CONFIG_LOCK:
                    cfg = load_config()
                    cfg["prices"][prov] = clean
                    save_config(cfg)
                return self.send_json({
                    "ok": True, "provider": prov,
                    "prices": price_map(cfg),
                    "prices_custom": custom_prices(cfg),
                })

            if path == "/api/jobs":
                return self.handle_create_job(read_body(self))

            if path == "/api/generate":
                return self.handle_generate(read_body(self))

            if path == "/api/edit":
                return self.handle_edit(read_body(self))

            if path == "/api/open-folder":
                os.makedirs(OUT_DIR, exist_ok=True)
                open_in_file_manager(OUT_DIR)
                return self.send_json({"ok": True})

            if path == "/api/delete":
                body = read_body(self)
                name = os.path.basename(body.get("name") or "")
                for p in (os.path.join(OUT_DIR, name), os.path.join(OUT_DIR, name + ".json")):
                    if name and os.path.isfile(p):
                        os.remove(p)
                return self.send_json({"ok": True})

            self.send_error(404)
        except api.APIError as e:
            self.send_json({"ok": False, "error": str(e), "status": e.status, "detail": e.body}, 200)
        except Exception as e:  # noqa: BLE001
            self.send_json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 200)

    # ---------- business ----------
    def handle_create_job(self, body):
        cfg = load_config()
        prov = norm_provider(body.get("provider") or cfg["provider"])
        conf = cfg[prov]
        if not conf["api_key"]:
            return self.send_json(
                {"ok": False, "error": "%s 还没有填 API Key" % PROVIDER_LABEL[prov]}, 200)
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return self.send_json({"ok": False, "error": "prompt 不能为空"}, 200)
        mode = "edit" if body.get("mode") == "edit" else "generate"
        limit = cfg["max_parallel"]
        try:
            count = max(1, min(limit, int(body.get("count") or 1)))
        except (TypeError, ValueError):
            count = 1
        files = []
        for i, item in enumerate(body.get("images") or []):
            b64, _ = data_url_to_bytes(item.get("data") or "")
            files.append((item.get("name") or ("ref%d.png" % i), base64.b64decode(b64)))
        if prov == "nano":
            mode = "edit" if files else "generate"
        elif mode == "edit" and not files:
            return self.send_json({"ok": False, "error": "请先添加参考图"}, 200)

        model = (body.get("model") or conf["model"]).strip()
        opts = dict(base_url=conf["base_url"], model=model,
                    timeout=int(body.get("timeout") or 300))
        if prov == "nano":
            opts["aspect"] = body.get("aspect") or None
        else:
            opts.update(
                size=body.get("size") or "auto",
                quality=body.get("quality") or "high",
                background=body.get("background") or None,
                response_format=body.get("response_format") or "url",
            )
            if mode == "edit":
                opts["input_fidelity"] = body.get("input_fidelity") or "high"

        prices = price_map(cfg)
        price = prices.get(model)

        # 手抖点两次同一个按钮时，直接把刚才那批任务还回去，避免重复扣费。
        now = time.time()
        sig = (prov, mode, model, prompt, count, opts.get("size"), opts.get("quality"), len(files))
        with JOBS_LOCK:
            for k in [k for k, v in RECENT.items() if now - v[0] > 10]:
                RECENT.pop(k, None)
            last = RECENT.get(sig)
            if last and now - last[0] < 1.5:
                prev = JOBS.get(last[1])
                if prev is not None:
                    return self.send_json({"ok": True, "job": _job_public(prev),
                                           "queued": TASK_Q.qsize(), "deduped": True,
                                           "count": prev["total"], "limit": limit})

        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "mode": mode, "provider": prov, "model": model, "price": price,
               "prompt": prompt, "total": count, "done": 0, "failed": 0,
               "status": "queued", "size": opts.get("size") or opts.get("aspect"),
               "quality": opts.get("quality"),
               "items": [], "errors": [], "created": time.time(), "finished": None}
        with JOBS_LOCK:
            JOBS[jid] = job
            JOB_ORDER.append(jid)
            RECENT[sig] = (now, jid)
            while len(JOB_ORDER) > MAX_JOBS:
                JOBS.pop(JOB_ORDER.pop(0), None)
        payload = {"provider": prov, "mode": mode, "prompt": prompt,
                   "api_key": conf["api_key"], "opts": opts, "files": files}
        for _ in range(count):
            TASK_Q.put((jid, payload))
        return self.send_json({"ok": True, "job": _job_public(job), "queued": TASK_Q.qsize(),
                               "count": count, "limit": limit, "parallel": GATE.snapshot()})

    def handle_generate(self, body):
        cfg = load_config()
        conf = cfg["gpt"]
        if not conf["api_key"]:
            return self.send_json({"ok": False, "error": "还没有填 API Key"}, 200)
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return self.send_json({"ok": False, "error": "prompt 不能为空"}, 200)
        opts = dict(
            base_url=conf["base_url"],
            model=body.get("model") or conf["model"],
            size=body.get("size") or "auto",
            quality=body.get("quality") or "high",
            background=body.get("background") or None,
            response_format=body.get("response_format") or "url",
            timeout=int(body.get("timeout") or 300),
        )
        res = api.generate(prompt, conf["api_key"], **opts)
        meta = {
            "type": "generate", "provider": "gpt", "model": opts["model"],
            "prompt": prompt,
            "size": body.get("size"),
            "quality": body.get("quality"),
            "background": body.get("background"),
            "usage": res["usage"],
            "elapsed": res["elapsed"],
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        path = api.save(OUT_DIR, res["image"], res["ext"], "gen", meta)
        self.send_json(
            {
                "ok": True,
                "file": os.path.basename(path),
                "url": "/outputs/" + os.path.basename(path),
                "elapsed": res["elapsed"],
                "usage": res["usage"],
            }
        )

    def handle_edit(self, body):
        cfg = load_config()
        conf = cfg["gpt"]
        if not conf["api_key"]:
            return self.send_json({"ok": False, "error": "还没有填 API Key"}, 200)
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return self.send_json({"ok": False, "error": "prompt 不能为空"}, 200)
        files = []
        for i, item in enumerate(body.get("images") or []):
            b64, _ = data_url_to_bytes(item.get("data") or "")
            blob = base64.b64decode(b64)
            files.append((item.get("name") or ("ref%d.png" % i), blob))
        if not files:
            return self.send_json({"ok": False, "error": "请先添加参考图"}, 200)

        res = api.edit(
            prompt,
            files,
            conf["api_key"],
            base_url=conf["base_url"],
            model=body.get("model") or conf["model"],
            size=body.get("size") or "auto",
            quality=body.get("quality") or "high",
            background=body.get("background") or None,
            input_fidelity=body.get("input_fidelity") or "high",
            response_format=body.get("response_format") or "url",
            timeout=int(body.get("timeout") or 300),
        )
        meta = {
            "type": "edit", "provider": "gpt", "model": body.get("model") or conf["model"],
            "prompt": prompt,
            "refs": [f[0] for f in files],
            "size": body.get("size"),
            "quality": body.get("quality"),
            "input_fidelity": body.get("input_fidelity"),
            "usage": res["usage"],
            "elapsed": res["elapsed"],
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        path = api.save(OUT_DIR, res["image"], res["ext"], "edit", meta)
        self.send_json(
            {
                "ok": True,
                "file": os.path.basename(path),
                "url": "/outputs/" + os.path.basename(path),
                "elapsed": res["elapsed"],
                "usage": res["usage"],
            }
        )


def list_outputs(limit=60):
    if not os.path.isdir(OUT_DIR):
        return []
    names = [
        n
        for n in os.listdir(OUT_DIR)
        if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    ]
    names.sort(key=lambda n: os.path.getmtime(os.path.join(OUT_DIR, n)), reverse=True)
    items = []
    for n in names[:limit]:
        p = os.path.join(OUT_DIR, n)
        meta = {}
        if os.path.isfile(p + ".json"):
            try:
                with open(p + ".json", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                pass
        items.append(
            {
                "name": n,
                "url": "/outputs/" + n,
                "mtime": int(os.path.getmtime(p)),
                "size_kb": int(os.path.getsize(p) / 1024),
                "meta": meta,
            }
        )
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8756)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--workers", type=int, default=None,
                    help="初始并发数（3 或 5，默认用 config.json 里的设置；页面里可随时改）")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg["gpt"]["api_key"] and os.environ.get("APINEBULA_API_KEY"):
        cfg["gpt"]["api_key"] = os.environ["APINEBULA_API_KEY"]
        save_config(cfg)

    if args.workers is not None:
        cfg["max_parallel"] = norm_parallel(args.workers)
    GATE.set_limit(cfg["max_parallel"])

    os.makedirs(OUT_DIR, exist_ok=True)
    srv = None
    last_err = None
    for p in [args.port + i for i in range(21)]:  # 端口被占就往后挪，最多试 21 个
        try:
            srv = Server((args.host, p), Handler)
            args.port = p
            break
        except OSError as e:
            last_err = e
    if srv is None:
        print("启动失败：%s" % last_err)
        return
    srv.daemon_threads = True
    for _ in range(MAX_WORKERS):
        threading.Thread(target=worker_loop, daemon=True).start()
    url = "http://%s:%d/" % (args.host, args.port)
    print("=" * 52)
    print("  GPT Image Studio 已启动")
    print("  %s" % url)
    print("=" * 52)
    print("输出目录: %s" % OUT_DIR)
    print("GPT Image   : %s @ %s" % (cfg["gpt"]["model"], cfg["gpt"]["base_url"]))
    print("Nano Banana : %s @ %s" % (cfg["nano"]["model"], cfg["nano"]["base_url"]))
    print("并发生成: %d 路（页面里勾选可切到 5 路）" % GATE.snapshot()["limit"])
    print("关闭本窗口即停止服务。")
    if not cfg["gpt"]["api_key"] and not cfg["nano"]["api_key"]:
        print("提示: 尚未配置 API Key，请在页面右上角「设置」里填写。")
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
