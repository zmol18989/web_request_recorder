#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页请求录制 / 查看 / 回放工具（类似浏览器开发者工具的 Network 面板）

核心思路：
  1. record    用 Playwright 驱动真实浏览器，把页面发出的请求（尤其是表单 POST）
               连同请求头、表单字段、Cookie、响应内容一起存成 JSON；
  2. list/show 像 Network 面板一样查看、过滤、导出 cURL / Python 代码；
  3. replay    用 requests 按录制顺序重放，支持变量替换、Cookie 复用、结果保存；
  4. from-curl 如果你已经会按 F12 复制请求，可以直接把 "Copy as cURL" 粘进来；
  5. report    生成一个可搜索、可展开的 HTML 报告（离线可看）。

常用示例
--------
# 本文件位于 C:/code/tools/，在其它目录执行时请写完整路径，例如：
#   python "C:/code/tools/web_request_recorder.py" --help

# 录制：打开浏览器，手动登录/提交表单，回到控制台按回车结束
python web_request_recorder.py record --url https://example.com/login



# 只想看“点了按钮后提交了什么”：只保留带字段的请求，并实时打印字段值
python web_request_recorder.py record --url https://example.com/login --only-fields
python web_request_recorder.py record --url http://127.0.0.1:5000/login --only-fields

# 只录写操作（POST/PUT/PATCH/DELETE）
python web_request_recorder.py record --url https://example.com --forms-only

# 附加到已经开着的 Chrome（先用 chrome.exe --remote-debugging-port=9222 启动）
python web_request_recorder.py record --cdp http://127.0.0.1:9222

# 查看
python web_request_recorder.py list captures/rec-20261027-101500.json
python web_request_recorder.py forms captures/rec-20261027-101500.json
python web_request_recorder.py forms captures/rec-20261027-101500.json --csv forms.csv
python web_request_recorder.py show captures/rec-20261027-101500.json 3 --format curl

# 回放：替换动态字段（同名字段自动替换，正文里的 {{token}} 也会替换）
python web_request_recorder.py replay captures/rec-20261027-101500.json \
        --var username=admin --var password=123456 --save output/replay

# 从 devtools 复制的 cURL 生成可回放的录制文件
python web_request_recorder.py from-curl curl.txt -o captures/login.json

# 生成 HTML 报告
python web_request_recorder.py report captures/rec-20261027-101500.json --open

依赖：
  pip install playwright requests      # 录制需要 playwright
  playwright install chromium          # 机器上没有 Chrome/Edge 时执行一次
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import os
import re
import shlex
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent
CAPTURE_DIR = BASE_DIR / "captures"

VERSION = 1

# 回放时由 requests 自己生成/不应照抄的请求头
DROP_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "accept-encoding",
    "transfer-encoding", "upgrade", "http2-settings", "te", "trailer",
    "proxy-connection",
}

STATIC_EXTS = {
    ".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".svg", ".ico", ".bmp", ".avif", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".mp3", ".m4a", ".webm", ".ogg", ".wav", ".zip", ".rar", ".7z",
}

TEXT_HINTS = ("json", "text", "xml", "javascript", "x-www-form-urlencoded", "graphql")

# 名字看起来会变的字段（回放时通常需要 --var 覆盖）
DYNAMIC_FIELD = re.compile(
    r"(token|csrf|xsrf|sign|signature|nonce|timestamp|time_stamp|_t$|captcha|verify|ticket|session)",
    re.I,
)

PLACEHOLDER = re.compile(r"\{\{\s*([\w.\-]+)\s*\}\}")


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def human_size(n) -> str:
    if n is None:
        return "-"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return "-"


def truncate(text, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def header_get(headers: dict, name: str) -> str:
    name = name.lower()
    for key, value in (headers or {}).items():
        if key.lower() == name:
            return value or ""
    return ""


def collapse(pairs) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            if isinstance(out[key], list):
                out[key].append(value)
            else:
                out[key] = [out[key], value]
        else:
            out[key] = value
    return out


def parse_ids(text: str) -> set:
    ids: set = set()
    for chunk in str(text).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start, end = chunk.split("-", 1)
            try:
                ids.update(range(int(start), int(end) + 1))
            except ValueError:
                pass
        else:
            try:
                ids.add(int(chunk))
            except ValueError:
                pass
    return ids


def load_capture(path) -> dict:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"找不到文件: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):  # 兼容纯数组格式
        data = {"meta": {}, "requests": data}
    data.setdefault("requests", [])
    return data


def save_capture(path, data: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def render(text, variables: dict):
    if not text:
        return text

    def repl(match):
        key = match.group(1)
        return str(variables[key]) if key in variables else match.group(0)

    return PLACEHOLDER.sub(repl, text)


def missing_vars(text, variables: dict) -> list:
    return sorted({m.group(1) for m in PLACEHOLDER.finditer(text or "") if m.group(1) not in variables})


# --------------------------------------------------------------------------- #
# 表单解析
# --------------------------------------------------------------------------- #
def parse_multipart(body: bytes, content_type: str):
    """粗略解析 multipart/form-data，返回 (字段字典, 文件列表)。"""
    match = re.search(r'boundary="?([^";]+)"?', content_type or "", re.I)
    if not match or not body:
        return None, []
    boundary = ("--" + match.group(1)).encode()
    fields, files = [], []
    for chunk in body.split(boundary):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk in (b"--", b"--\r\n"):
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        head, data = chunk.split(b"\r\n\r\n", 1)
        head_text = head.decode("utf-8", "replace")
        name_match = re.search(r'\bname="([^"]*)"', head_text)
        file_match = re.search(r'\bfilename="([^"]*)"', head_text)
        name = name_match.group(1) if name_match else ""
        if file_match:
            files.append({"field": name, "filename": file_match.group(1), "size": len(data)})
            continue
        fields.append((name, data.decode("utf-8", "replace")))
    return collapse(fields), files


def extract_form(post_text, raw: bytes, content_type: str):
    """把请求体解析成表单字段，方便人工查看。"""
    content_type = content_type or ""
    low = content_type.lower()
    if not raw:
        return None, None
    try:
        if "application/x-www-form-urlencoded" in low:
            text = raw.decode("utf-8", "replace")
            return collapse(parse_qsl(text, keep_blank_values=True)), None
        if "multipart/form-data" in low:
            return parse_multipart(raw, content_type)
        if "application/json" in low or low.endswith("+json"):
            return None, None
    except Exception:
        pass
    return None, None


def try_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def detect_dynamic_fields(records) -> list:
    names = set()
    for rec in records:
        for container in (rec.get("form") or {}, rec.get("json_body") or {}):
            if isinstance(container, dict):
                for key in container.keys():
                    if DYNAMIC_FIELD.search(str(key)):
                        names.add(str(key))
    return sorted(names)


def value_text(value) -> str:
    """把字段值统一转成一行文本，便于终端/CSV 展示。"""
    if isinstance(value, list):
        return " , ".join(value_text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def collect_fields(rec: dict, include_query: bool = True) -> list:
    """汇总一条请求里“用户提交的字段”，返回 [{source, field, value}]。

    source:
      query  URL 查询参数（GET 查询按钮、查询条件）
      form   表单字段（application/x-www-form-urlencoded）
      json   JSON 请求体（嵌套值以 JSON 字符串展示）
      file   multipart 上传的文件（只记文件名和大小）
      body   无法解析的原始请求体（回退展示）
    """
    rows: list = []
    if include_query:
        url = rec.get("url") or ""
        for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
            rows.append({"source": "query", "field": key, "value": value})

    form = rec.get("form")
    if isinstance(form, dict):
        for key, value in form.items():
            rows.append({"source": "form", "field": str(key), "value": value_text(value)})

    json_body = rec.get("json_body")
    if isinstance(json_body, dict):
        for key, value in json_body.items():
            if isinstance(value, (dict, list)):
                text = json.dumps(value, ensure_ascii=False)
            else:
                text = value_text(value)
            rows.append({"source": "json", "field": str(key), "value": text})
    elif isinstance(json_body, list):
        rows.append({"source": "json", "field": "(body)",
                     "value": json.dumps(json_body, ensure_ascii=False)})

    for part in rec.get("multipart_parts") or []:
        rows.append({
            "source": "file",
            "field": str(part.get("field", "")),
            "value": f"[文件] {part.get('filename', '')} ({human_size(part.get('size'))})",
        })

    # 表单/JSON 都没解析出内容时，把原始请求体整体展示出来（例如 text/plain、GraphQL 字符串）
    if not any(row["source"] != "query" for row in rows) and rec.get("post_data"):
        rows.append({"source": "body", "field": "(body)", "value": str(rec["post_data"])})
    return rows


# --------------------------------------------------------------------------- #
# 录制
# --------------------------------------------------------------------------- #
class Capture:
    def __init__(self, path: Path, args):
        self.path = path
        self.args = args
        self.records: list = []
        self.cookies: list = []
        self.meta = {
            "tool": "web_request_recorder",
            "version": VERSION,
            "created_at": now_iso(),
            "started_at": now_iso(),
            "ended_at": None,
            "start_url": getattr(args, "url", None),
            "source": "playwright",
            "options": {
                "all": bool(getattr(args, "all", False)),
                "forms_only": bool(getattr(args, "forms_only", False)),
                "include": getattr(args, "include", None),
                "exclude": getattr(args, "exclude", None),
            },
        }
        self._seq = 0
        self._last_save = 0.0
        self._contexts: set = set()
        self._pages: set = set()
        self._page_tasks: dict = {}
        self._bodies: dict = {}
        self._tasks: set = set()
        self._printed = 0
        self.include_re = re.compile(args.include, re.I) if getattr(args, "include", None) else None
        self.exclude_re = re.compile(args.exclude, re.I) if getattr(args, "exclude", None) else None
        self.body_limit = int(getattr(args, "body_limit", 20000) or 0)
        self.only_fields = bool(getattr(args, "only_fields", False))
        self.echo_forms = bool(getattr(args, "echo_forms", False) or self.only_fields)

    # ---------- 过滤 ---------- #
    def keep(self, request) -> bool:
        url = request.url or ""
        low = url.lower()
        if low.startswith(("data:", "blob:", "about:", "chrome-extension:", "devtools:", "ws:", "wss:")):
            return False
        if self.include_re and not self.include_re.search(url):
            return False
        if self.exclude_re and self.exclude_re.search(url):
            return False
        method = (request.method or "").upper()
        if getattr(self.args, "forms_only", False):
            return method not in ("GET", "HEAD", "OPTIONS")
        if getattr(self.args, "all", False):
            return True
        if method not in ("GET", "HEAD", "OPTIONS"):
            return True
        rtype = (request.resource_type or "").lower()
        if rtype in ("document", "xhr", "fetch", "eventsource"):
            return True
        return os.path.splitext(urlsplit(url).path)[1].lower() not in STATIC_EXTS

    # ---------- 收集 ---------- #
    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def on_response(self, response, seq: int) -> None:
        try:
            request = response.request
            if not self.keep(request):
                return
            request_headers = {}
            try:
                request_headers = await request.all_headers()
            except Exception:
                request_headers = dict(request.headers or {})
            req_ct = header_get(request_headers, "content-type")

            raw = b""
            try:
                buf = request.post_data_buffer
                if buf:
                    raw = buf
            except Exception:
                raw = b""
            if not raw:
                try:
                    text = request.post_data
                except Exception:
                    text = None
                if text:
                    raw = text.encode("utf-8", "replace")

            method = (request.method or "").upper()
            cdp_body = self.take_body(method, request.url)
            if cdp_body is not None and (not raw or "multipart/form-data" in req_ct.lower()):
                raw = cdp_body
            post_data = raw.decode("utf-8", "replace") if raw else None

            post_b64 = None
            if raw:
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError:
                    post_b64 = base64.b64encode(raw).decode("ascii")

            form, files = extract_form(post_data, raw, req_ct)
            rec = {
                "id": seq,
                "time": request_time(request),
                "method": method,
                "url": request.url,
                "resource_type": request.resource_type,
                "content_type": req_ct,
                "request_headers": request_headers,
                "post_data": post_data if raw else None,
                "post_data_b64": post_b64,
                "body_size": len(raw) if raw else 0,
                "form": form,
                "multipart_parts": files or None,
                "json_body": try_json(post_data) if "json" in req_ct.lower() else None,
                "duration_ms": request_duration(request),
            }

            try:
                page = request.frame.page
                rec["page_url"] = page.url
            except Exception:
                pass

            resp_headers = {}
            try:
                resp_headers = await response.all_headers()
            except Exception:
                resp_headers = dict(response.headers or {})

            resp_ct = header_get(resp_headers, "content-type")
            resp_info = {
                "status": response.status,
                "status_text": response.status_text,
                "content_type": resp_ct,
                "mime": resp_ct.split(";")[0].strip(),
                "headers": resp_headers,
                "body_size": None,
                "body_preview": None,
            }
            if self.want_body(resp_ct, response.status, request.resource_type):
                try:
                    body = await response.body()
                    resp_info["body_size"] = len(body)
                    text = body.decode("utf-8", "replace")
                    if self.body_limit and len(text) > self.body_limit:
                        text = text[: self.body_limit] + "\n...[已截断]"
                    resp_info["body_preview"] = text
                except Exception as exc:
                    resp_info["body_error"] = str(exc)[:200]
            rec["response"] = resp_info

            # 只保留真正带提交/查询字段的请求（登录、查询、保存等按钮产生的请求）
            fields = collect_fields(rec) if (self.only_fields or self.echo_forms) else None
            if self.only_fields and not fields:
                return

            self.records.append(rec)
            self.print_live(rec, fields)
            self.maybe_save()
        except Exception as exc:  # 录制不能因为单条请求失败而中断
            if os.environ.get("WEB_RECORDER_DEBUG"):
                print(f"[debug] 处理响应失败: {exc}", file=sys.stderr)

    def want_body(self, content_type: str, status: int, resource_type: str) -> bool:
        if not self.body_limit or status in (204, 304):
            return False
        ct = (content_type or "").lower()
        if not any(hint in ct for hint in TEXT_HINTS):
            return False
        if (resource_type or "").lower() in ("image", "font", "media"):
            return False
        return True

    def print_live(self, rec: dict, fields=None) -> None:
        self._printed += 1
        if self._printed > 50:
            if self._printed == 51:
                print("...（请求很多，后续只记录、不逐条打印）")
            return
        resp = rec.get("response") or {}
        print(
            f"[{rec['id']:>3}] {rec['method']:<6} {resp.get('status', '-'):<4} "
            f"{truncate(rec['url'], 110)}"
        )
        if self.echo_forms:
            if fields is None:
                fields = collect_fields(rec)
            for row in fields:
                print(f"      {row['source']:<6} {row['field']} = {truncate(row['value'], 200)}")

    def maybe_save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < 1.0 and len(self.records) % 20:
            return
        self._last_save = now
        self.save()

    def save(self) -> None:
        self.meta["ended_at"] = now_iso()
        self.meta["count"] = len(self.records)
        payload = {
            "meta": self.meta,
            "cookies": self.cookies,
            "requests": sorted(self.records, key=lambda r: r.get("id", 0)),
        }
        save_capture(self.path, payload)

    def attach(self, context) -> None:
        if id(context) in self._contexts:
            return
        self._contexts.add(id(context))

        def on_response(response):
            seq = self.next_seq()
            task = asyncio.get_running_loop().create_task(self.on_response(response, seq))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        def on_failed(request):
            if not self.keep(request):
                return
            print(f"[失败] {request.method} {truncate(request.url, 100)} -> {request.failure}")

        context.on("response", on_response)
        context.on("requestfailed", on_failed)

        if not getattr(self.args, "no_body_capture", False):
            def on_page(page):
                if id(page) in self._page_tasks:
                    return
                task = asyncio.get_running_loop().create_task(self.attach_page(page))
                self._page_tasks[id(page)] = task

            context.on("page", on_page)
            for page in context.pages:
                on_page(page)

    async def wait_pages(self) -> None:
        tasks = [task for task in self._page_tasks.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_tasks(self, timeout: float = 5.0) -> None:
        """结束录制时等一下还在处理的响应，避免最后几条（常是提交请求）丢失。"""
        deadline = time.monotonic() + timeout
        while True:
            tasks = [task for task in self._tasks if not task.done()]
            if not tasks:
                # 给事件循环一点时间接收刚刚触发的 response 事件
                await asyncio.sleep(0.1)
                tasks = [task for task in self._tasks if not task.done()]
                if not tasks:
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.wait(tasks, timeout=remaining)

    # ---------- 用 CDP 拿完整的请求体（file 上传时 Playwright 拿不到） ---------- #
    async def attach_page(self, page) -> None:
        if id(page) in self._pages:
            return
        self._pages.add(id(page))
        try:
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Fetch.enable", {
                "patterns": [
                    {"urlPattern": "*", "resourceType": "XHR", "requestStage": "Request"},
                    {"urlPattern": "*", "resourceType": "Fetch", "requestStage": "Request"},
                    {"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"},
                ]
            })
        except Exception:
            return

        def on_paused(params):
            asyncio.get_running_loop().create_task(self.on_paused(cdp, params))

        try:
            cdp.on("Fetch.requestPaused", on_paused)
        except Exception:
            pass

    async def on_paused(self, cdp, params: dict) -> None:
        try:
            request = params.get("request") or {}
            method = (request.get("method") or "").upper()
            url = request.get("url") or ""
            if method not in ("GET", "HEAD", "OPTIONS"):
                body = None
                entries = request.get("postDataEntries") or []
                if entries:
                    try:
                        body = b"".join(
                            base64.b64decode(entry.get("bytes") or "") for entry in entries
                        )
                    except Exception:
                        body = None
                if body is None and request.get("postData") is not None:
                    body = str(request["postData"]).encode("utf-8")
                if body is not None:
                    queue = self._bodies.setdefault((method, url), [])
                    queue.append({"body": body, "time": time.time()})
                    if len(queue) > 20:
                        del queue[0]
        except Exception:
            pass
        finally:
            try:
                await cdp.send("Fetch.continueRequest", {"requestId": params["requestId"]})
            except Exception:
                pass

    def take_body(self, method: str, url: str):
        queue = self._bodies.get((method, url))
        if not queue:
            return None
        return queue.pop(0).get("body")


def cookies_from_headers(records) -> list:
    """从录到的 Cookie 请求头反推 Cookie（CDP 附加模式拿不到 cookie jar 时用）。"""
    out, seen = [], set()
    for rec in records:
        host = urlsplit(rec.get("url", "")).hostname or ""
        for key, value in (rec.get("request_headers") or {}).items():
            if key.lower() != "cookie":
                continue
            for pair in str(value).split(";"):
                if "=" not in pair:
                    continue
                name, val = pair.split("=", 1)
                name, val = name.strip(), val.strip()
                token = (name, host)
                if token in seen:
                    continue
                seen.add(token)
                out.append({"name": name, "value": val, "domain": host, "path": "/"})
    return out


def request_time(request) -> str:
    try:
        start = (request.timing or {}).get("startTime") or 0
        if start and start > 0:
            return datetime.fromtimestamp(start / 1000.0).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    except Exception:
        pass
    return now_iso()


def request_duration(request):
    try:
        timing = request.timing or {}
        request_start = timing.get("requestStart", -1)
        response_start = timing.get("responseStart", -1)
        response_end = timing.get("responseEnd", -1)
        if request_start is None or request_start < 0:
            return None
        if response_start and response_start > request_start:
            return round(response_start - request_start, 1)
        if response_end and response_end > request_start:
            return round(response_end - request_start, 1)
    except Exception:
        pass
    return None


async def launch_browser(playwright, args):
    """优先用系统已装的 Edge/Chrome，免得下载 Chromium。"""
    candidates = [args.channel] if args.channel else [None, "msedge", "chrome"]
    last_error = None
    for channel in candidates:
        try:
            kwargs = {"headless": args.headless}
            if channel:
                kwargs["channel"] = channel
            browser = await playwright.chromium.launch(**kwargs)
            name = channel or "bundled chromium"
            print(f"浏览器已启动: {name}（{'无头' if args.headless else '有界面'}）")
            return browser
        except Exception as exc:
            last_error = exc
            continue
    raise SystemExit(
        "无法启动浏览器。请任选其一:\n"
        "  1) 安装 Chrome/Edge；\n"
        "  2) 执行: python -m playwright install chromium\n"
        "  3) 用 --cdp http://127.0.0.1:9222 附加到已开着的浏览器\n"
        f"最后一次错误: {last_error}"
    )


def start_stdin_reader(stop: asyncio.Event) -> None:
    """在控制台按回车时结束录制（在非交互环境里自动跳过）。"""
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return
    except Exception:
        return
    loop = asyncio.get_running_loop()

    def worker():
        try:
            sys.stdin.readline()
        except Exception:
            return
        try:
            loop.call_soon_threadsafe(stop.set)
        except RuntimeError:
            pass

    threading.Thread(target=worker, daemon=True).start()


async def run_record(args, path: Path) -> Path:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise SystemExit(
            "录制功能需要 playwright:\n"
            "  python -m pip install playwright\n"
            "  python -m playwright install chromium   # 机器上已有 Chrome/Edge 时可省略\n"
            "如果你已经会从 F12 复制请求，也可以直接用 from-curl 子命令。"
        )

    capture = Capture(path, args)
    stop = asyncio.Event()

    async with async_playwright() as playwright:
        if args.cdp:
            browser = await playwright.chromium.connect_over_cdp(args.cdp)
            print(f"已附加到浏览器: {args.cdp}")
        else:
            browser = await launch_browser(playwright, args)

        browser.on("disconnected", lambda *_: stop.set())
        contexts = list(browser.contexts)
        if not contexts:
            contexts = [await browser.new_context()]
        for context in contexts:
            capture.attach(context)
        browser.on("context", capture.attach)

        page = contexts[0].pages[0] if contexts[0].pages else await contexts[0].new_page()
        await capture.wait_pages()
        if args.url:
            try:
                await page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:
                print(f"[警告] 打开 {args.url} 失败: {exc}")

        print("=" * 78)
        print("正在录制网页请求：请在浏览器里正常操作（登录、提交表单等）。")
        print("结束后回到这个窗口按回车；也可以直接关闭浏览器窗口。")
        print("=" * 78)

        pending_tasks = []
        if args.duration and args.duration > 0:

            async def auto_stop():
                await asyncio.sleep(args.duration)
                print(f"\n已达到录制时长 {args.duration}s，自动结束。")
                stop.set()

            pending_tasks.append(asyncio.create_task(auto_stop()))
        elif args.no_wait:
            print("[提示] 没有设置 --duration 又使用了 --no-wait，将一直录制到浏览器关闭。")
        if not args.no_wait:
            start_stdin_reader(stop)

        try:
            await stop.wait()
        finally:
            try:
                await capture.wait_tasks()
            except Exception:
                pass
            capture.save()  # 同步写盘，Ctrl+C 也不丢已录内容
            for context in contexts:
                try:
                    capture.cookies.extend(await context.cookies())
                except BaseException:
                    pass
            if not capture.cookies:
                capture.cookies = cookies_from_headers(capture.records)
            capture.save()
            for task in pending_tasks:
                task.cancel()

        try:
            # 附加模式下 close() 只是断开连接，不会关掉你自己的浏览器
            await browser.close()
        except Exception:
            pass

        # 关闭浏览器时可能还有最后一批响应事件，再等一次并落盘
        try:
            await capture.wait_tasks()
        except Exception:
            pass
        capture.save()

    return path


def cmd_record(args) -> int:
    path = Path(args.out) if args.out else CAPTURE_DIR / f"rec-{datetime.now():%Y%m%d-%H%M%S}.json"
    if path.suffix.lower() != ".json":
        path = path.with_suffix(".json")
    try:
        asyncio.run(run_record(args, path))
    except KeyboardInterrupt:
        print("\n录制被手动中断（已录到的内容会保留）。")
    except SystemExit:
        raise
    except Exception as exc:
        print(f"录制出错: {exc}")
        return 1

    if not path.exists():
        print("没有生成录制文件。")
        return 1
    data = load_capture(path)
    records = data.get("requests", [])
    writes = [r for r in records if r.get("method") not in ("GET", "HEAD", "OPTIONS")]
    with_fields = [r for r in records if collect_fields(r)]
    print("-" * 78)
    print(f"录制完成: {len(records)} 条请求（其中写操作 {len(writes)} 条，带提交/查询字段 {len(with_fields)} 条） -> {path}")
    dynamic = detect_dynamic_fields(records)
    if dynamic:
        print("可能是动态字段（回放时建议用 --var 覆盖）: " + ", ".join(dynamic))
    print(f"查看: {cli_cmd()} list \"{path}\"")
    print(f"只看提交字段: {cli_cmd()} forms \"{path}\"")
    if writes:
        first_id = writes[0]["id"]
        print(f"回放: {cli_cmd()} replay \"{path}\" --only {first_id} --var 字段=值")

    if not args.no_report:
        html = write_report(data, path.with_suffix(".html"), title=f"录制报告 {path.name}")
        print(f"报告: {html}")
        if args.open_report:
            webbrowser.open(html.as_uri())
    return 0


def relpath(p) -> str:
    try:
        return str(Path(p).resolve().relative_to(BASE_DIR))
    except Exception:
        return str(p)


def cli_cmd() -> str:
    """返回可直接粘贴到终端的本工具命令（绝对路径，避免换了目录就找不到）。"""
    return f'python "{Path(__file__).resolve()}"'


# --------------------------------------------------------------------------- #
# 查看 / 导出
# --------------------------------------------------------------------------- #
def find_record(data: dict, rid: int):
    for rec in data.get("requests", []):
        if int(rec.get("id", -1)) == int(rid):
            return rec
    return None


def cmd_list(args) -> int:
    data = load_capture(args.file)
    records = list(data.get("requests", []))
    if args.writes_only:
        records = [r for r in records if r.get("method") not in ("GET", "HEAD", "OPTIONS")]
    if args.url:
        regex = re.compile(args.url, re.I)
        records = [r for r in records if regex.search(r.get("url", ""))]
    if args.limit:
        records = records[: args.limit]

    if not records:
        print("没有匹配的请求。")
        return 0

    meta = data.get("meta") or {}
    print(f"文件: {args.file}")
    print(f"来源: {meta.get('source', '-')}  开始: {meta.get('started_at', '-')}  请求数: {meta.get('count', len(data.get('requests', [])))}")
    print()
    print(f"{'ID':>4}  {'METHOD':<7}{'状态':<5}{'类型':<10}{'大小':>8}  URL")
    print("-" * 110)
    for rec in records:
        resp = rec.get("response") or {}
        status = resp.get("status", "-")
        print(
            f"{rec.get('id', 0):>4}  {rec.get('method', '?'):<7}{status:<5}"
            f"{(rec.get('resource_type') or ''):<10}"
            f"{human_size(resp.get('body_size')):>8}  {truncate(rec.get('url', ''), 90)}"
        )
    print()
    print(f"查看详情: {cli_cmd()} show \"{args.file}\" ID")
    print(f"只看提交字段: {cli_cmd()} forms \"{args.file}\"")
    write_ids = [r['id'] for r in records if r.get('method') not in ("GET", "HEAD", "OPTIONS")]
    if write_ids:
        print(f"回放写操作: {cli_cmd()} replay \"{args.file}\" --only {','.join(map(str, write_ids[:10]))}")
    return 0


def cmd_forms(args) -> int:
    """只看“点击按钮后提交了什么”：字段名 = 字段值。"""
    data = load_capture(args.file)
    ids = parse_ids(args.only) if args.only else None
    methods = {m.strip().upper() for m in args.method.split(",")} if args.method else None
    url_re = re.compile(args.url, re.I) if args.url else None

    groups = []
    for rec in data.get("requests", []):
        if ids is not None and int(rec.get("id", -1)) not in ids:
            continue
        if methods and (rec.get("method") or "").upper() not in methods:
            continue
        if url_re and not url_re.search(rec.get("url", "")):
            continue
        fields = collect_fields(rec, include_query=not args.no_query)
        if not fields:
            continue
        groups.append((rec, fields))

    def status_of(rec):
        return (rec.get("response") or {}).get("status", "-")

    # JSON 导出
    if args.json:
        payload = []
        for rec, fields in groups:
            payload.append({
                "id": rec.get("id"),
                "method": rec.get("method"),
                "url": rec.get("url"),
                "status": status_of(rec),
                "fields": fields,
            })
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.json == "-":
            print(text)
        else:
            out = Path(args.json)
            if out.parent and str(out.parent) not in ("", "."):
                out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text + "\n", encoding="utf-8")
            print(f"JSON: {out.resolve()}")

    # CSV 导出（utf-8-sig，Excel 直接双击不乱码）
    if args.csv:
        out = Path(args.csv)
        if out.parent and str(out.parent) not in ("", "."):
            out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["id", "method", "url", "status", "source", "field", "value"])
            for rec, fields in groups:
                for row in fields:
                    writer.writerow([
                        rec.get("id"), rec.get("method"), rec.get("url"),
                        status_of(rec), row["source"], row["field"], row["value"],
                    ])
        print(f"CSV: {out.resolve()}", file=sys.stderr if args.json == "-" else sys.stdout)

    if args.json == "-":
        return 0

    if not groups:
        print("没有找到提交字段（可能这些请求没带表单/查询参数，或录制时被过滤了）。")
        return 0

    meta = data.get("meta") or {}
    print(f"文件: {args.file}")
    print(f"来源: {meta.get('source', '-')}  开始: {meta.get('started_at', '-')}")
    print(f"提交字段: {len(groups)} 条请求 / {sum(len(f) for _, f in groups)} 个字段")
    print()
    for rec, fields in groups:
        print(f"{rec.get('id', 0):>4}  {rec.get('method', '?'):<6} {str(status_of(rec)):<5} "
              f"{truncate(rec.get('url', ''), 100)}")
        for row in fields:
            value = row["value"] if args.full else truncate(row["value"], 400)
            print(f"      {row['source']:<6} {row['field']} = {value}")
        print()
    if not args.csv:
        print(f"导出 CSV: {cli_cmd()} forms \"{args.file}\" --csv forms.csv")
    return 0


def shell_quote(text: str) -> str:
    return "'" + str(text).replace("'", "'\\''") + "'"


def to_curl(rec: dict) -> str:
    lines = [f"curl -X {rec.get('method', 'GET')} {shell_quote(rec.get('url', ''))}"]
    for key, value in (rec.get("request_headers") or {}).items():
        if key.lower() in DROP_HEADERS:
            continue
        lines.append(f"  -H {shell_quote(f'{key}: {value}')}")
    body = rec.get("post_data")
    if body:
        lines.append(f"  --data-raw {shell_quote(body)}")
    return " \\\n".join(lines)


def to_python(rec: dict) -> str:
    headers = clean_headers(rec.get("request_headers") or {}, None, keep_cookie=True)
    lines = [
        "import requests",
        "",
        f"url = {json.dumps(rec.get('url', ''), ensure_ascii=False)}",
        "headers = " + json.dumps(headers, ensure_ascii=False, indent=4),
    ]
    body = rec.get("post_data")
    if body is not None:
        lines.append("data = " + json.dumps(body, ensure_ascii=False))
    lines.append("")
    call = f"resp = requests.request({json.dumps(rec.get('method', 'GET'))}, url, headers=headers"
    if body is not None:
        call += ", data=data"
    call += ", timeout=30)"
    lines.extend([call, "print(resp.status_code)", "print(resp.text[:1000])"])
    return "\n".join(lines)


def cmd_show(args) -> int:
    data = load_capture(args.file)
    rec = find_record(data, args.id)
    if not rec:
        print(f"找不到 ID={args.id} 的请求。")
        return 1
    if args.format == "json":
        print(json.dumps(rec, ensure_ascii=False, indent=2))
    elif args.format == "curl":
        print(to_curl(rec))
    elif args.format == "python":
        print(to_python(rec))
    else:  # pretty
        print(f"#{rec.get('id')}  {rec.get('method')} {rec.get('url')}")
        print(f"时间: {rec.get('time')}  类型: {rec.get('resource_type')}  耗时: {rec.get('duration_ms') or '-'}ms")
        resp = rec.get("response") or {}
        print(f"响应: {resp.get('status')} {resp.get('status_text', '')}  {resp.get('content_type', '')}")
        if rec.get("form"):
            print("\n表单字段:")
            for key, value in rec["form"].items():
                if isinstance(value, list):
                    value = " , ".join(map(str, value))
                print(f"  {key} = {value}")
        if rec.get("multipart_parts"):
            print("\n上传文件:")
            for part in rec["multipart_parts"]:
                print(f"  {part['field']}: {part['filename']} ({human_size(part['size'])})")
        print("\n请求头:")
        for key, value in (rec.get("request_headers") or {}).items():
            print(f"  {key}: {truncate(value, 200)}")
        if rec.get("post_data"):
            print("\n请求体:")
            print(truncate(rec["post_data"], 4000))
        if resp.get("headers"):
            print("\n响应头:")
            for key, value in resp["headers"].items():
                print(f"  {key}: {truncate(value, 200)}")
        if resp.get("body_preview"):
            print("\n响应内容:")
            print(truncate(resp["body_preview"], 4000))
    return 0


# --------------------------------------------------------------------------- #
# 回放
# --------------------------------------------------------------------------- #
def clean_headers(headers: dict, variables, keep_cookie: bool = False) -> dict:
    out = {}
    for key, value in (headers or {}).items():
        low = key.lower()
        if low.startswith(":"):
            continue
        if low in DROP_HEADERS:
            continue
        if low == "cookie" and not keep_cookie:
            continue
        out[key] = render(value, variables or {})
    return out


def body_bytes(rec: dict):
    if rec.get("post_data_b64"):
        try:
            return base64.b64decode(rec["post_data_b64"])
        except Exception:
            return None
    if rec.get("post_data") is not None:
        return rec["post_data"].encode("utf-8")
    return None


INDEXED_KEY = re.compile(r"^(.*)\[(\d+)\]$")


def apply_indexed_form_vars(raw: bytes, variables: dict) -> bytes:
    """支持 字段[序号]=值，精确修改重复出现的表单字段。

    例如请求体里有两次 amount：amount[0]=100 只改第一次，amount[1]=0 只改第二次。
    """
    indexed = []
    for key, value in variables.items():
        match = INDEXED_KEY.match(key)
        if match:
            indexed.append((match.group(1), int(match.group(2)), value))
    if not indexed:
        return raw
    try:
        pairs = parse_qsl(raw.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return raw
    for name, index, value in indexed:
        positions = [i for i, (key, _) in enumerate(pairs) if key == name]
        if 0 <= index < len(positions):
            pairs[positions[index]] = (name, str(value))
    return urlencode(pairs).encode("utf-8")


def apply_vars_to_body(rec: dict, raw: bytes, variables: dict) -> bytes:
    """把 {{name}} 占位符和同名字段的值替换掉。"""
    for key, value in variables.items():
        pattern = re.compile(rb"\{\{\s*" + re.escape(key.encode("utf-8")) + rb"\s*\}\}")
        raw = pattern.sub(lambda m, v=str(value): v.encode("utf-8"), raw)

    content_type = (rec.get("content_type") or "").lower()

    if "application/json" in content_type or content_type.endswith("+json"):
        try:
            obj = json.loads(raw.decode("utf-8"))
            if isinstance(obj, dict):
                changed = False
                for key, value in variables.items():
                    if key not in obj:
                        continue
                    old = obj[key]
                    if isinstance(old, bool):
                        obj[key] = str(value).strip().lower() in ("1", "true", "yes", "on")
                    elif isinstance(old, int):
                        try:
                            obj[key] = int(value)
                        except (TypeError, ValueError):
                            obj[key] = value
                    elif isinstance(old, float):
                        try:
                            obj[key] = float(value)
                        except (TypeError, ValueError):
                            obj[key] = value
                    else:
                        obj[key] = value
                    changed = True
                if changed:
                    return json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except Exception:
            pass
        return raw

    looks_form = (
        "application/x-www-form-urlencoded" in content_type
        or (not content_type and b"=" in raw and b"\r\n\r\n" not in raw)
    )
    if looks_form:
        raw = apply_indexed_form_vars(raw, variables)
        for key, value in variables.items():
            if INDEXED_KEY.match(key):
                continue
            key_bytes = key.encode("utf-8")
            pattern = re.compile(rb"(^|&)" + re.escape(key_bytes) + rb"=([^&]*)")
            raw = pattern.sub(
                lambda m, k=key_bytes, v=str(value): m.group(1) + k + b"=" + quote_plus(v).encode(),
                raw,
            )
        return raw

    if "multipart/form-data" in content_type:
        for key, value in variables.items():
            key_bytes = key.encode("utf-8")
            pattern = re.compile(
                rb'(name="' + re.escape(key_bytes) + rb'"[^\r\n]*\r\n(?:[^\r\n]+\r\n)*?\r\n)(.*?)(\r\n--)',
                re.S,
            )

            def repl(match, v=str(value)):
                if b"filename=" in match.group(1):  # 不动文件字段
                    return match.group(0)
                return match.group(1) + v.encode("utf-8") + match.group(3)

            raw = pattern.sub(repl, raw)
        return raw

    return raw


def fill_cookies(session, data: dict, records) -> int:
    count = 0
    for cookie in data.get("cookies") or []:
        try:
            session.cookies.set(
                cookie.get("name", ""),
                cookie.get("value", ""),
                domain=cookie.get("domain"),
                path=cookie.get("path") or "/",
            )
            count += 1
        except Exception:
            pass
    if count:
        return count
    for rec in records:
        for key, value in (rec.get("request_headers") or {}).items():
            if key.lower() != "cookie":
                continue
            for pair in str(value).split(";"):
                if "=" in pair:
                    name, val = pair.split("=", 1)
                    session.cookies.set(name.strip(), val.strip())
                    count += 1
    return count


def safe_name(text: str, limit: int = 60) -> str:
    text = re.sub(r"[^\w\-.]+", "_", text, flags=re.UNICODE).strip("_")
    return text[:limit] or "request"


def cmd_replay(args) -> int:
    try:
        import requests
    except ImportError:
        print("回放需要 requests: python -m pip install requests")
        return 1

    data = load_capture(args.file)
    records = list(data.get("requests", []))
    if args.only:
        ids = parse_ids(args.only)
        records = [r for r in records if int(r.get("id", 0)) in ids]
    elif not args.include_get:
        records = [r for r in records if r.get("method") not in ("GET", "HEAD", "OPTIONS")]
    if args.method:
        wanted = {m.strip().upper() for m in args.method.split(",") if m.strip()}
        records = [r for r in records if r.get("method") in wanted]
    if args.url:
        regex = re.compile(args.url, re.I)
        records = [r for r in records if regex.search(r.get("url", ""))]
    records.sort(key=lambda r: int(r.get("id", 0)))

    if not records:
        print("没有匹配的请求可回放（默认跳过 GET，需要时加 --include-get）。")
        return 1

    variables = {}
    if args.vars_file:
        try:
            variables.update(json.loads(Path(args.vars_file).read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"读取变量文件失败: {exc}")
            return 1
    for item in args.var or []:
        if "=" not in item:
            print(f"[忽略] 变量格式应为 key=value: {item}")
            continue
        key, value = item.split("=", 1)
        variables[key.strip()] = value

    session = requests.Session()
    session.verify = not args.no_verify
    if args.user_agent:
        session.headers["User-Agent"] = args.user_agent
    elif records and header_get(records[0].get("request_headers") or {}, "user-agent"):
        session.headers["User-Agent"] = header_get(records[0]["request_headers"], "user-agent")
    cookie_count = fill_cookies(session, data, records)

    print(f"准备回放 {len(records)} 条请求（Cookie {cookie_count} 个，变量 {len(variables)} 个）")
    if variables:
        print("变量: " + ", ".join(f"{k}={truncate(v, 30)}" for k, v in variables.items()))

    if args.save:
        Path(args.save).mkdir(parents=True, exist_ok=True)

    failed = 0
    for index, rec in enumerate(records, 1):
        raw = body_bytes(rec)
        if raw is not None:
            raw = apply_vars_to_body(rec, raw, variables)

        url = render(rec.get("url", ""), variables)
        headers = clean_headers(rec.get("request_headers") or {}, variables, keep_cookie=False)
        method = (rec.get("method") or "GET").upper()

        leftovers = missing_vars(url, variables) + missing_vars(
            json.dumps(headers, ensure_ascii=False), variables
        )
        if raw is not None:
            leftovers += missing_vars(raw.decode("utf-8", "replace"), variables)
        if leftovers:
            message = f"#{rec.get('id')} 还有未替换的占位符: {', '.join(sorted(set(leftovers)))}"
            if args.strict:
                print("[中止] " + message)
                return 2
            print("[警告] " + message)

        if args.dry_run:
            print(f"\n[{index}/{len(records)}] {method} {url}")
            print("  headers: " + json.dumps(headers, ensure_ascii=False))
            if raw:
                print("  body: " + truncate(raw.decode("utf-8", "replace"), 500))
            continue

        started = time.time()
        try:
            resp = session.request(
                method,
                url,
                headers=headers,
                data=raw,
                timeout=args.timeout,
                allow_redirects=args.follow,
            )
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(records)}] {method} 请求失败: {exc}")
            if args.stop_on_error:
                break
            continue

        elapsed = time.time() - started
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "charset" not in content_type and any(
            hint in content_type for hint in ("json", "text/", "javascript", "xml")
        ):
            resp.encoding = "utf-8"
        elif not resp.encoding:
            resp.encoding = "utf-8"
        print(
            f"[{index}/{len(records)}] {method} {resp.status_code} "
            f"{elapsed:.2f}s {truncate(url, 100)}"
        )

        if args.verbose and resp.text:
            print("    " + truncate(" ".join(resp.text.split()), 300))

        if resp.status_code >= 400:
            failed += 1
            if args.stop_on_error:
                print("遇到错误状态码，已停止。")
                break

        if args.save:
            info = {
                "id": rec.get("id"),
                "request": {"method": method, "url": url, "headers": headers,
                            "body": raw.decode("utf-8", "replace") if raw else None},
                "response": {
                    "status": resp.status_code,
                    "headers": dict(resp.headers),
                    "body": resp.text,
                },
            }
            stem = f"{index:03d}_{safe_name(f'{method}_{urlsplit(url).path}')}"
            out_json = Path(args.save) / f"{stem}.json"
            out_json.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"    已保存: {out_json}")

        if args.pause and index < len(records):
            time.sleep(args.pause)

    print("-" * 78)
    if args.dry_run:
        print(f"试运行结束：共 {len(records)} 条请求（未实际发送）")
        return 0
    print(f"回放结束：成功 {len(records) - failed} 条，失败 {failed} 条")
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------- #
# 从 cURL 导入
# --------------------------------------------------------------------------- #
def split_curl(text: str):
    windows_style = ("curl.exe" in text.lower()) or ("^\n" in text) or ("^\r\n" in text)
    tokens = shlex.split(text, posix=not windows_style)
    if windows_style:
        tokens = [t.strip('"') for t in tokens]
    return tokens


def parse_curl(text: str) -> dict:
    text = text.strip().lstrip("$").strip()
    text = re.sub(r"\\\r?\n", " ", text)
    text = re.sub(r"\^\r?\n", " ", text)
    text = re.sub(r"`\r?\n", " ", text)

    tokens = split_curl(text)
    if tokens and tokens[0].lower() in ("curl", "curl.exe"):
        tokens = tokens[1:]

    method = None
    headers = {}
    chunks = []
    url = None
    cookie_header = None
    has_form_upload = False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        low = token.lower()

        def take(default=""):
            nonlocal i
            i += 1
            return tokens[i] if i < len(tokens) else default

        if token == "-X" or low == "--request":
            method = take().upper()
        elif token == "-H" or low == "--header":
            raw = take()
            if ":" in raw:
                key, value = raw.split(":", 1)
                headers[key.strip()] = value.strip()
        elif low in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode"):
            chunks.append(take())
        elif token == "-b" or low == "--cookie":
            cookie_header = take()
        elif token == "-F" or low == "--form":
            take()
            has_form_upload = True
        elif token == "-A" or low == "--user-agent":
            headers["User-Agent"] = take()
        elif token == "-e" or low == "--referer":
            headers["Referer"] = take()
        elif token == "-u" or low == "--user":
            take()
        elif low in ("-o", "--output", "-w", "--write-out", "-m", "--max-time",
                     "--connect-timeout", "-x", "--proxy", "-c", "--cookie-jar",
                     "--cert", "--key", "--retry", "--retry-delay"):
            take()
        elif low.startswith("-"):
            pass
        elif url is None and low.startswith(("http://", "https://")):
            url = token
        i += 1

    if not url:
        raise SystemExit("没有在 cURL 里找到 URL。请确认复制的是完整命令。")

    body = None
    if chunks:
        joiner = "&" if all(("=" in c and not c.lstrip().startswith("{")) for c in chunks) else ""
        body = joiner.join(chunks)
    if method is None:
        method = "POST" if body is not None else "GET"
    if cookie_header and "Cookie" not in headers:
        headers["Cookie"] = cookie_header

    content_type = header_get(headers, "content-type")
    form = None
    if body is not None:
        raw = body.encode("utf-8", "replace")
        form, _files = extract_form(body, raw, content_type)

    return {
        "id": 1,
        "time": now_iso(),
        "method": method,
        "url": url,
        "resource_type": "fetch",
        "content_type": content_type,
        "request_headers": headers,
        "post_data": body,
        "post_data_b64": None,
        "body_size": len(body.encode("utf-8")) if body else 0,
        "form": form,
        "multipart_parts": None,
        "json_body": try_json(body) if "json" in content_type.lower() else None,
        "duration_ms": None,
        "response": None,
        "_has_form": has_form_upload,
    }


def cmd_from_curl(args) -> int:
    if args.file == "-":
        text = sys.stdin.read()
    else:
        text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        print("cURL 内容为空。")
        return 1

    record = parse_curl(text)
    if record.pop("_has_form", False):
        print("[提示] 该 cURL 里含 -F 文件上传，命令行无法还原完整文件内容；"
              "建议改用 record 模式录制这条请求。")
    cookies = []
    for name, value in (record.get("request_headers") or {}).items():
        if name.lower() == "cookie":
            for pair in value.split(";"):
                if "=" in pair:
                    key, val = pair.split("=", 1)
                    cookies.append({"name": key.strip(), "value": val.strip(),
                                    "domain": urlsplit(record["url"]).hostname,
                                    "path": "/"})

    out = Path(args.out) if args.out else CAPTURE_DIR / f"curl-{datetime.now():%Y%m%d-%H%M%S}.json"
    if out.suffix.lower() != ".json":
        out = out.with_suffix(".json")
    data = {
        "meta": {
            "tool": "web_request_recorder",
            "version": VERSION,
            "created_at": now_iso(),
            "started_at": now_iso(),
            "ended_at": now_iso(),
            "source": "curl",
            "count": 1,
        },
        "cookies": cookies,
        "requests": [record],
    }
    save_capture(out, data)
    print(f"已导入: {record['method']} {record['url']}")
    if record.get("form"):
        print("表单字段: " + ", ".join(record["form"].keys()))
    print(f"写入: {out}")
    print(f"回放: {cli_cmd()} replay \"{out}\" --include-get")
    return 0


# --------------------------------------------------------------------------- #
# HTML 报告
# --------------------------------------------------------------------------- #
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --line:#e3e6ea; --text:#1f2328; --muted:#66707a;
          --blue:#0969da; --green:#1a7f37; --red:#cf222e; --orange:#bc4c00; }
  * { box-sizing: border-box; }
  body { margin:0; font: 13px/1.5 "Segoe UI", "Microsoft YaHei", sans-serif; color:var(--text); background:var(--bg); }
  header { padding:16px 20px 12px; background:var(--card); border-bottom:1px solid var(--line); }
  h1 { margin:0 0 4px; font-size:17px; }
  .meta { color:var(--muted); font-size:12px; }
  .bar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; padding:10px 20px;
         background:var(--card); border-bottom:1px solid var(--line); position:sticky; top:0; z-index:5; }
  .bar input[type=text] { flex:1; min-width:220px; padding:6px 10px; border:1px solid var(--line);
         border-radius:6px; font-size:13px; }
  .bar select, .bar label { padding:6px 8px; border:1px solid var(--line); border-radius:6px;
         background:#fff; font-size:12px; }
  .bar label { border:none; padding:0; display:flex; gap:5px; align-items:center; color:var(--muted); }
  #stat { color:var(--muted); font-size:12px; }
  table { width:100%; border-collapse:collapse; background:var(--card); }
  thead th { text-align:left; font-size:11px; text-transform:uppercase; color:var(--muted);
             padding:8px 10px; border-bottom:1px solid var(--line); position:sticky; top:53px;
             background:var(--card); z-index:4; }
  tbody td { padding:6px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  tr.req { cursor:pointer; }
  tr.req:hover { background:#f0f4f9; }
  td.num { text-align:right; color:var(--muted); white-space:nowrap; }
  td.m { font-weight:600; white-space:nowrap; }
  td.m.GET { color:var(--blue); } td.m.POST, td.m.PUT, td.m.PATCH { color:var(--green); }
  td.m.DELETE { color:var(--red); }
  td.s.ok { color:var(--green); font-weight:600; } td.s.warn { color:var(--orange); font-weight:600; }
  td.s.bad { color:var(--red); font-weight:600; }
  td.u { font-family:Consolas, monospace; font-size:12px; word-break:break-all; }
  td.t { color:var(--muted); white-space:nowrap; font-size:11px; }
  tr.detail > td { background:#fbfcfd; padding:0; }
  .detail-box { padding:12px 16px 18px; border-left:3px solid var(--blue); }
  .detail-box h4 { margin:12px 0 6px; font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.4px; }
  .kv { border-collapse:collapse; width:100%; max-width:1100px; background:#fff; }
  .kv th, .kv td { border:1px solid var(--line); padding:4px 8px; text-align:left; font-size:12px; vertical-align:top; }
  .kv th { width:230px; font-weight:600; background:#fafbfc; color:#3a4149; word-break:break-all; }
  .kv td { font-family:Consolas, monospace; word-break:break-all; }
  pre { margin:0; padding:8px 10px; background:#fff; border:1px solid var(--line); border-radius:4px;
        max-height:320px; overflow:auto; font:12px/1.5 Consolas, monospace; white-space:pre-wrap; word-break:break-all; }
  .actions { display:flex; gap:8px; flex-wrap:wrap; }
  .actions button { border:1px solid var(--line); background:#fff; border-radius:6px; padding:4px 10px;
        cursor:pointer; font-size:12px; color:var(--blue); }
  .actions button:hover { background:#eef4fb; }
  .badge { display:inline-block; padding:1px 6px; border-radius:10px; background:#eef1f4;
           color:var(--muted); font-size:11px; margin-left:6px; }
  .empty { padding:40px; text-align:center; color:var(--muted); }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="meta">__SUBTITLE__</div>
</header>
<div class="bar">
  <input id="q" type="text" placeholder="过滤：URL / 方法 / 状态码 / 字段名（例如 username）">
  <select id="m"><option value="">全部方法</option>__METHOD_OPTIONS__</select>
  <label><input type="checkbox" id="w"> 只看写操作</label>
  <span id="stat"></span>
</div>
<table>
  <thead>
    <tr>
      <th style="width:44px">#</th><th style="width:88px">时间</th><th style="width:70px">方法</th>
      <th style="width:52px">状态</th><th style="width:82px">类型</th>
      <th style="width:170px">Content-Type</th><th style="width:70px">大小</th><th>URL</th>
    </tr>
  </thead>
  <tbody id="tb"></tbody>
</table>
<div id="empty" class="empty" style="display:none">没有匹配的请求</div>
<script>
const DATA = __DATA__;
const RECORDS = {};
(DATA.requests || []).forEach(r => RECORDS[r.id] = r);

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
function shortCt(ct) { return (ct || "").split(";")[0]; }
function size(r) {
  const n = r.response && r.response.body_size;
  if (n == null) return (r.body_size ? r.body_size : 0) + "B";
  if (n < 1024) return n + "B";
  if (n < 1048576) return (n / 1024).toFixed(1) + "KB";
  return (n / 1048576).toFixed(1) + "MB";
}
function kvTable(obj) {
  const keys = Object.keys(obj || {});
  if (!keys.length) return "<pre>(空)</pre>";
  return '<table class="kv">' + keys.map(k => {
    let v = obj[k];
    if (Array.isArray(v)) v = v.join(" | ");
    return `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`;
  }).join("") + "</table>";
}
function detail(r) {
  const resp = r.response || {};
  const parts = [];
  parts.push(`<div class="actions">
    <button onclick="copyText(toCurl(${r.id}))">复制 cURL</button>
    <button onclick="copyText(toPy(${r.id}))">复制 Python 代码</button>
    <button onclick="copyText(r$({r.id}).url)">复制 URL</button>
    ${r.form ? '<span class="badge">表单 ' + Object.keys(r.form).length + " 项</span>" : ""}
    ${r.multipart_parts ? '<span class="badge">含文件上传</span>' : ""}
  </div>`);
  parts.push(`<h4>请求行</h4><pre>${esc(r.method + " " + r.url)}</pre>`);
  parts.push("<h4>请求头</h4>" + kvTable(r.request_headers));
  if (r.form && Object.keys(r.form).length) {
    parts.push("<h4>表单字段</h4>" + kvTable(r.form));
  }
  if (r.multipart_parts && r.multipart_parts.length) {
    parts.push("<h4>上传文件</h4><table class=\"kv\">" + r.multipart_parts.map(p =>
      `<tr><th>${esc(p.field)}</th><td>${esc(p.filename)} (${esc(p.size)} B)</td></tr>`).join("") + "</table>");
  }
  if (r.post_data) {
    let body = r.post_data;
    try { body = JSON.stringify(JSON.parse(body), null, 2); } catch (e) {}
    parts.push("<h4>原始请求体</h4><pre>" + esc(body) + "</pre>");
  }
  if (resp.status) {
    parts.push(`<h4>响应 ${resp.status} ${esc(resp.status_text || "")}</h4>` + kvTable(resp.headers));
  }
  if (resp.body_preview) {
    let text = resp.body_preview;
    try { text = JSON.stringify(JSON.parse(text), null, 2); } catch (e) {}
    parts.push("<h4>响应内容</h4><pre>" + esc(text) + "</pre>");
  }
  return '<div class="detail-box">' + parts.join("") + "</div>";
}
const QT = "'";
function sh(s) { return QT + String(s).replace(/'/g, "'\\''") + QT; }
const DROP = /^(host|content-length|connection|accept-encoding|keep-alive|transfer-encoding|proxy-connection)$/i;
function toCurl(id) {
  const r = r$(id), lines = ["curl -X " + r.method + " " + sh(r.url)];
  Object.entries(r.request_headers || {}).forEach(([k, v]) => {
    if (DROP.test(k)) return;
    lines.push("  -H " + sh(k + ": " + v));
  });
  if (r.post_data) lines.push("  --data-raw " + sh(r.post_data));
  return lines.join(" \\\n");
}
function pyStr(s) { return JSON.stringify(s); }
function toPy(id) {
  const r = r$(id), hs = {};
  Object.entries(r.request_headers || {}).forEach(([k, v]) => { if (!DROP.test(k) && k.toLowerCase() !== "cookie") hs[k] = v; });
  const lines = ["import requests", "", "url = " + pyStr(r.url),
                 "headers = " + JSON.stringify(hs, null, 4)];
  if (r.post_data) lines.push("data = " + pyStr(r.post_data));
  let call = "resp = requests.request(" + pyStr(r.method) + ", url, headers=headers";
  if (r.post_data) call += ", data=data";
  call += ", timeout=30)";
  lines.push("", call, "print(resp.status_code)", "print(resp.text[:1000])");
  return lines.join("\n");
}
function r$(id) { return RECORDS[id]; }
function copyText(text) {
  const done = () => {};
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text));
  } else fallbackCopy(text);
}
function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text; document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); } catch (e) {}
  document.body.removeChild(ta);
}
function render() {
  const q = document.getElementById("q").value.trim().toLowerCase();
  const m = document.getElementById("m").value;
  const writesOnly = document.getElementById("w").checked;
  const list = (DATA.requests || []).filter(r => {
    if (m && r.method !== m) return false;
    if (writesOnly && ["GET", "HEAD", "OPTIONS"].includes(r.method)) return false;
    if (!q) return true;
    if ((r.url || "").toLowerCase().includes(q)) return true;
    if ((r.method || "").toLowerCase().includes(q)) return true;
    if (String((r.response || {}).status || "").includes(q)) return true;
    if (r.form && Object.keys(r.form).some(k => k.toLowerCase().includes(q))) return true;
    return false;
  });
  document.getElementById("stat").textContent = "显示 " + list.length + " / " + (DATA.requests || []).length + " 条";
  document.getElementById("empty").style.display = list.length ? "none" : "block";
  document.getElementById("tb").innerHTML = list.map(r => {
    const st = (r.response || {}).status || 0;
    const cls = st >= 400 ? "bad" : st >= 300 ? "warn" : "ok";
    return `<tr class="req" data-id="${r.id}">
      <td class="num">${r.id}</td>
      <td class="t">${esc((r.time || "").slice(11, 19))}</td>
      <td class="m ${esc(r.method)}">${esc(r.method)}</td>
      <td class="s ${cls}">${st || "-"}</td>
      <td>${esc(r.resource_type || "")}</td>
      <td>${esc(shortCt(r.content_type))}</td>
      <td class="num">${size(r)}</td>
      <td class="u" title="${esc(r.url)}">${esc(r.url)}</td>
    </tr>
    <tr class="detail" id="d-${r.id}" style="display:none"><td colspan="8">${detail(r)}</td></tr>`;
  }).join("");
}
document.getElementById("tb").addEventListener("click", e => {
  if (e.target.closest("button")) return;
  const row = e.target.closest("tr.req");
  if (!row) return;
  const d = document.getElementById("d-" + row.dataset.id);
  if (!d) return;
  d.style.display = d.style.display === "none" ? "" : "none";
});
document.getElementById("q").addEventListener("input", render);
document.getElementById("m").addEventListener("change", render);
document.getElementById("w").addEventListener("change", render);
render();
</script>
</body>
</html>
"""


def write_report(data: dict, out_path, title: str = "请求录制报告") -> Path:
    records = data.get("requests", [])
    meta = data.get("meta") or {}
    methods = sorted({r.get("method", "") for r in records if r.get("method")})
    payload = {"meta": meta, "requests": records}
    data_json = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")

    writes = [r for r in records if r.get("method") not in ("GET", "HEAD", "OPTIONS")]
    hosts = {urlsplit(r.get("url", "")).netloc for r in records if r.get("url")}
    subtitle = (
        f"来源: {meta.get('source', '-')} &nbsp;|&nbsp; 开始: {meta.get('started_at', '-')} "
        f"&nbsp;|&nbsp; 请求 {len(records)} 条（写操作 {len(writes)} 条） &nbsp;|&nbsp; "
        f"站点 {len(hosts)} 个 &nbsp;|&nbsp; 生成于 {now_iso()}"
    )
    method_options = "".join(
        f'<option value="{m}">{m}</option>' for m in methods
    )
    html = (
        HTML_TEMPLATE
        .replace("__TITLE__", title)
        .replace("__SUBTITLE__", subtitle)
        .replace("__METHOD_OPTIONS__", method_options)
        .replace("__DATA__", data_json)
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def cmd_report(args) -> int:
    data = load_capture(args.file)
    out = Path(args.out) if args.out else Path(args.file).with_suffix(".html")
    html = write_report(data, out, title=f"请求录制报告 - {Path(args.file).name}")
    print(f"已生成报告: {html}")
    if args.open:
        webbrowser.open(html.resolve().as_uri())
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    cmd = cli_cmd()
    parser = argparse.ArgumentParser(
        prog="web_request_recorder",
        description="网页请求录制 / 查看 / 回放工具（类似浏览器开发者工具 Network 面板）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            f"  {cmd} record --url https://example.com/login --only-fields\n"
            f"  {cmd} list captures/rec-xxx.json --writes-only\n"
            f"  {cmd} forms captures/rec-xxx.json\n"
            f"  {cmd} show captures/rec-xxx.json 3 --format curl\n"
            f"  {cmd} replay captures/rec-xxx.json --var username=admin\n"
            f"  {cmd} from-curl curl.txt -o captures/login.json\n"
            f"  {cmd} report captures/rec-xxx.json --open\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record", help="打开浏览器录制请求")
    p.add_argument("-o", "--out", help="保存的 JSON 路径（默认 captures/rec-时间.json）")
    p.add_argument("--url", help="启动后先打开的网址")
    p.add_argument("--cdp", help="附加到已开着的浏览器，例如 http://127.0.0.1:9222")
    p.add_argument("--channel", help="浏览器通道: msedge / chrome / chromium（默认自动）")
    p.add_argument("--headless", action="store_true", help="无头模式（不显示浏览器窗口）")
    p.add_argument("--all", action="store_true", help="连图片/CSS/字体等静态资源也记录")
    p.add_argument("--forms-only", action="store_true", help="只记录 POST/PUT/PATCH/DELETE")
    p.add_argument("--only-fields", action="store_true",
                   help="只保留带提交/查询字段的请求（登录、查询、保存），并实时打印字段值")
    p.add_argument("--echo-forms", action="store_true",
                   help="录制时实时打印提交的字段和值（--only-fields 默认开启）")
    p.add_argument("--include", help="只记录 URL 匹配该正则的请求")
    p.add_argument("--exclude", help="排除 URL 匹配该正则的请求")
    p.add_argument("--body-limit", type=int, default=20000, help="响应正文最多保存多少字符（0=不保存）")
    p.add_argument("--no-body-capture", action="store_true",
                   help="关闭 CDP 请求体捕获（默认开启，文件上传的原文靠它）")
    p.add_argument("--duration", type=float, default=0, help="录制多少秒后自动结束（0=按回车结束）")
    p.add_argument("--no-wait", action="store_true", help="不等待回车（配合 --duration 用）")
    p.add_argument("--no-report", action="store_true", help="结束后不生成 HTML 报告")
    p.add_argument("--open-report", action="store_true", help="结束后自动打开 HTML 报告")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("list", help="列出录到的请求")
    p.add_argument("file")
    p.add_argument("--writes-only", action="store_true", help="只看写操作")
    p.add_argument("--url", help="按 URL 正则过滤")
    p.add_argument("--limit", type=int, default=0, help="最多显示多少条")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("forms", help="只看提交的表单/查询字段（字段名 = 字段值）")
    p.add_argument("file")
    p.add_argument("--only", help="只看指定 ID，如 1,3-5")
    p.add_argument("--method", help="只看指定方法，如 POST 或 POST,PUT")
    p.add_argument("--url", help="按 URL 正则过滤")
    p.add_argument("--no-query", action="store_true", help="不显示 URL 上的查询参数，只看请求体字段")
    p.add_argument("--csv", metavar="PATH", help="导出 CSV（UTF-8 BOM，Excel 可直接打开）")
    p.add_argument("--json", nargs="?", const="-", metavar="PATH",
                   help="导出 JSON；不写 PATH 时直接输出到屏幕")
    p.add_argument("--full", action="store_true", help="屏幕显示完整字段值（默认长值截断）")
    p.set_defaults(func=cmd_forms)

    p = sub.add_parser("show", help="查看单条请求详情")
    p.add_argument("file")
    p.add_argument("id", type=int)
    p.add_argument("--format", choices=["pretty", "json", "curl", "python"], default="pretty")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("replay", help="回放请求")
    p.add_argument("file")
    p.add_argument("--only", help="只回放指定 ID，如 1,3-5")
    p.add_argument("--include-get", action="store_true", help="连 GET 请求一起回放")
    p.add_argument("--method", help="只回放指定方法，如 POST,PUT")
    p.add_argument("--url", help="只回放 URL 匹配该正则的请求")
    p.add_argument("--var", action="append", metavar="K=V",
                   help="变量/字段值，可重复；重复出现的字段用 字段[序号]=值 精确指定")
    p.add_argument("--vars-file", help="变量 JSON 文件")
    p.add_argument("--dry-run", action="store_true", help="只打印将要发送的请求")
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--pause", type=float, default=0.2, help="请求间隔秒数")
    p.add_argument("--save", help="把每次响应保存到该目录")
    p.add_argument("--follow", action="store_true", help="跟随 3xx 跳转（默认不跟随，更贴近录制内容）")
    p.add_argument("--strict", action="store_true", help="存在未替换的 {{占位符}} 时中止")
    p.add_argument("--stop-on-error", action="store_true", help="遇到请求失败或 4xx/5xx 立即停止")
    p.add_argument("--no-verify", action="store_true", help="跳过 HTTPS 证书校验")
    p.add_argument("--user-agent", help="覆盖 User-Agent")
    p.add_argument("--verbose", action="store_true", help="打印响应内容片段")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("from-curl", help="从浏览器复制的 cURL 生成录制文件")
    p.add_argument("file", help="cURL 文本文件，- 表示标准输入")
    p.add_argument("-o", "--out", help="输出的 JSON 路径")
    p.set_defaults(func=cmd_from_curl)

    p = sub.add_parser("report", help="生成 HTML 报告")
    p.add_argument("file")
    p.add_argument("-o", "--out", help="输出 HTML 路径（默认与 JSON 同名）")
    p.add_argument("--open", action="store_true", help="生成后自动打开")
    p.set_defaults(func=cmd_report)

    return parser


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
