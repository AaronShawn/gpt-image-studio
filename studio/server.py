"""Local web workbench for OpenAI-compatible image APIs.

    python server.py                     # http://127.0.0.1:8756
    python server.py --port 9000 --open  # custom port, open the browser

Turns clicks in the browser into Images API calls, stores the results under
../outputs, and keeps the API key in a local config.json only — it is never
sent to the page and never embedded in the HTML.
"""

import argparse
import base64
import json
import mimetypes
import os
import queue
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
DEFAULT = {
    "base_url": api.DEFAULT_BASE,
    "api_key": "",
    "model": api.DEFAULT_MODEL,
    "max_parallel": 3,
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
RECENT = {}  # 防抖：(mode, prompt, count, size, quality) -> (ts, job_id)


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


def _job_public(job):
    return {
        "id": job["id"], "status": job["status"], "mode": job["mode"],
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
                if payload["mode"] == "edit":
                    res = api.edit(payload["prompt"], payload["files"], payload["api_key"], **payload["opts"])
                else:
                    res = api.generate(payload["prompt"], payload["api_key"], **payload["opts"])
                meta = {
                    "type": payload["mode"], "prompt": payload["prompt"],
                    "size": payload["opts"].get("size"), "quality": payload["opts"].get("quality"),
                    "usage": res["usage"], "elapsed": res["elapsed"], "job": jid,
                    "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
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
    cfg = dict(DEFAULT)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        except Exception:
            pass
    cfg["max_parallel"] = norm_parallel(cfg.get("max_parallel"), PARALLEL_DEFAULT)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def mask(key):
    if not key:
        return ""
    if len(key) <= 10:
        return key[:3] + "***"
    return key[:6] + "*" * 8 + key[-4:]


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


class Server(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 允许两个进程抢同一端口，必须关掉，
    # 否则第二次启动会"成功"但请求被另一个实例吃掉。
    allow_reuse_address = False
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "GPTImage25Studio/1.1"
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
            cfg = load_config()
            self.send_json(
                {
                    "base_url": cfg["base_url"],
                    "model": cfg["model"],
                    "has_key": bool(cfg["api_key"]),
                    "key_masked": mask(cfg["api_key"]),
                    "max_parallel": cfg["max_parallel"],
                    "parallel_choices": list(PARALLEL_CHOICES),
                    "parallel": GATE.snapshot(),
                }
            )
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
                cfg = load_config()
                if body.get("base_url"):
                    cfg["base_url"] = body["base_url"].strip().rstrip("/")
                if body.get("model"):
                    cfg["model"] = body["model"].strip()
                if body.get("api_key"):
                    cfg["api_key"] = body["api_key"].strip()
                if body.get("clear_key"):
                    cfg["api_key"] = ""
                if "max_parallel" in body:
                    cfg["max_parallel"] = norm_parallel(body.get("max_parallel"))
                    GATE.set_limit(cfg["max_parallel"])
                save_config(cfg)
                return self.send_json({
                    "ok": True,
                    "base_url": cfg["base_url"],
                    "model": cfg["model"],
                    "has_key": bool(cfg["api_key"]),
                    "key_masked": mask(cfg["api_key"]),
                    "max_parallel": cfg["max_parallel"],
                    "parallel_choices": list(PARALLEL_CHOICES),
                    "parallel": GATE.snapshot(),
                })

            if path == "/api/models":
                body = read_body(self)
                cfg = load_config()
                base = (body.get("base_url") or cfg["base_url"]).strip().rstrip("/")
                key = (body.get("api_key") or cfg["api_key"]).strip()
                if not key:
                    return self.send_json({"ok": False, "error": "请先填写 API Key"}, 200)
                ids = api.list_models(base, key, timeout=int(body.get("timeout") or 30))
                ids.sort(key=lambda m: (0 if "image" in m.lower() else 1, m))
                return self.send_json({"ok": True, "models": ids})

            if path == "/api/jobs":
                return self.handle_create_job(read_body(self))

            if path == "/api/generate":
                return self.handle_generate(read_body(self))

            if path == "/api/edit":
                return self.handle_edit(read_body(self))

            if path == "/api/open-folder":
                os.startfile(OUT_DIR)  # noqa: S606 (Windows only)
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
        if not cfg["api_key"]:
            return self.send_json({"ok": False, "error": "还没有填 API Key"}, 200)
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
        if mode == "edit":
            for i, item in enumerate(body.get("images") or []):
                b64, _ = data_url_to_bytes(item.get("data") or "")
                files.append((item.get("name") or ("ref%d.png" % i), base64.b64decode(b64)))
            if not files:
                return self.send_json({"ok": False, "error": "请先添加参考图"}, 200)
        opts = dict(
            base_url=cfg["base_url"], model=cfg["model"],
            size=body.get("size") or "auto",
            quality=body.get("quality") or "high",
            background=body.get("background") or None,
            response_format=body.get("response_format") or "url",
            timeout=int(body.get("timeout") or 300),
        )
        if mode == "edit":
            opts["input_fidelity"] = body.get("input_fidelity") or "high"

        # 手抖点两次同一个按钮时，直接把刚才那批任务还回去，避免重复扣费。
        now = time.time()
        sig = (mode, prompt, count, opts["size"], opts["quality"], len(files))
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
        job = {"id": jid, "mode": mode, "prompt": prompt, "total": count, "done": 0, "failed": 0,
               "status": "queued", "size": opts["size"], "quality": opts["quality"],
               "items": [], "errors": [], "created": time.time(), "finished": None}
        with JOBS_LOCK:
            JOBS[jid] = job
            JOB_ORDER.append(jid)
            RECENT[sig] = (now, jid)
            while len(JOB_ORDER) > MAX_JOBS:
                JOBS.pop(JOB_ORDER.pop(0), None)
        payload = {"mode": mode, "prompt": prompt, "api_key": cfg["api_key"], "opts": opts, "files": files}
        for _ in range(count):
            TASK_Q.put((jid, payload))
        return self.send_json({"ok": True, "job": _job_public(job), "queued": TASK_Q.qsize(),
                               "count": count, "limit": limit, "parallel": GATE.snapshot()})

    def handle_generate(self, body):
        cfg = load_config()
        if not cfg["api_key"]:
            return self.send_json({"ok": False, "error": "还没有填 API Key"}, 200)
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return self.send_json({"ok": False, "error": "prompt 不能为空"}, 200)
        opts = dict(
            base_url=cfg["base_url"],
            model=cfg["model"],
            size=body.get("size") or "auto",
            quality=body.get("quality") or "high",
            background=body.get("background") or None,
            response_format=body.get("response_format") or "url",
            timeout=int(body.get("timeout") or 300),
        )
        res = api.generate(prompt, cfg["api_key"], **opts)
        meta = {
            "type": "generate",
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
        if not cfg["api_key"]:
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
            cfg["api_key"],
            base_url=cfg["base_url"],
            model=cfg["model"],
            size=body.get("size") or "auto",
            quality=body.get("quality") or "high",
            background=body.get("background") or None,
            input_fidelity=body.get("input_fidelity") or "high",
            response_format=body.get("response_format") or "url",
            timeout=int(body.get("timeout") or 300),
        )
        meta = {
            "type": "edit",
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
    if not cfg["api_key"] and os.environ.get("APINEBULA_API_KEY"):
        cfg["api_key"] = os.environ["APINEBULA_API_KEY"]
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
    print("模型: %s @ %s" % (cfg["model"], cfg["base_url"]))
    print("并发生成: %d 路（页面里勾选可切到 5 路）" % GATE.snapshot()["limit"])
    print("关闭本窗口即停止服务。")
    if not cfg["api_key"]:
        print("提示: 尚未配置 API Key，请在页面右上角「设置」里填写。")
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
