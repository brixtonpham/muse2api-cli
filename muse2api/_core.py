"""Low-level driver: a browser on muse.ai, the page's gateway calls and the DOM fallbacks. Use muse2api.Muse."""
import asyncio
import base64
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

import websockets

logger = logging.getLogger("muse2api")


def _chrome():
    """MUSE2API_CHROME, else the usual install path for this OS, else chrome/chromium on PATH."""
    if env := os.environ.get("MUSE2API_CHROME"):
        return env
    if sys.platform == "darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        if found := shutil.which(name):
            return found
    return r"C:\Program Files\Google\Chrome\Application\chrome.exe" if os.name == "nt" else "google-chrome"


CHROME = _chrome()
MUSE = "https://muse.ai/"
def _ua():
    """The installed Chrome's own user agent, without "Headless" (a hard-coded version went stale on 156)."""
    try:
        v = subprocess.run([CHROME, "--version"], capture_output=True, text=True, timeout=20).stdout.split()[-1]
        major = v.split(".")[0]
    except (OSError, IndexError, subprocess.TimeoutExpired):
        major = "156"
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36")


UA = _ua()
RUNNING = ("running", "in_progress", "queued", "started")

# Muse decodes every decrypted gateway message with JSON.parse; keep the chat events we need.
HOOK = r"""(()=>{if(window.__muse)return;const m=window.__muse={ev:[]};const P=JSON.parse;
JSON.parse=function(t,...a){const r=P.call(this,t,...a);try{if(r&&r.type==="event"&&typeof r.event==="string"&&
/^(delta\.presentation|delta\.message_done|task\.status|message\.user)$/.test(r.event))m.ev.push([Date.now(),r])}catch(e){}return r}})()"""

# The page's own gateway client (React context: connectionState, isReady, sendRequest, gatewayScopeKey, ...).
# sendRequest(method, params) is HTTP-over-Noise RPC to the agent VM, e.g. "fs.raw" (GET /fs/raw/{path}, a Blob),
# "fs.list", "chat.stream" (send a message), "chat.history", "sessions.get". Found by walking the React fiber tree.
GW = r"""(()=>{if(window.__museGw)return;window.__museGw=()=>{const ok=v=>v&&typeof v.sendRequest==='function'&&'gatewayScopeKey' in v;
if(ok(window.__museGwV))return window.__museGwV;const el=[...document.querySelectorAll('textarea,main,div')].find(e=>
Object.keys(e).some(k=>k.startsWith('__reactFiber')));if(!el)return null;let f=el[Object.keys(el).find(k=>k.startsWith('__reactFiber'))];
while(f.return)f=f.return;const st=[f];while(st.length){const x=st.pop();const v=x.memoizedProps&&x.memoizedProps.value;
if(ok(v))return window.__museGwV=v;if(x.child)st.push(x.child);if(x.sibling)st.push(x.sibling)}return null}})()"""
# what the web composer sends with chat.stream (bundle 2026-10-29: hotel_results + the chat capability list)
CAPS = ["hotel_results", "chat_cancel", "delta_stream", "custom_reactions", "custom_reactions_facebook_thumbs_up_v1"]
DONE = ("completed", "failed", "error", "idle", "cancelled", "canceled")  # sessions.get status of a finished turn
API_IMG = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


class MuseError(Exception):
    pass


class LimitReached(MuseError):
    pass


def log(*a):
    logger.info(" ".join(str(x) for x in a))


def normalize(cookies):
    """CDP cookies or a Cookie-Editor export -> CDP CookieParam list for muse.ai."""
    out = []
    for c in cookies:
        if not str(c.get("domain", "")).endswith("muse.ai"):
            continue
        p = {k: c[k] for k in ("name", "value", "domain", "path", "secure", "httpOnly") if k in c}
        exp = c.get("expires", c.get("expirationDate"))
        if exp and exp > 0:
            p["expires"] = float(exp)
        ss = str(c.get("sameSite", "")).lower()
        p["sameSite"] = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}.get(ss, "Lax")
        out.append(p)
    return out


class Store:
    """The account files under one folder (default $MUSE2API_HOME or ~/.config/muse2api):

    - <account>.json: the account's muse.ai cookies (mode 600), written back after every run
    - profiles/<account>/: its own Chrome profile, so Muse sees the same device every run
    - accounts.json (mode 600): per-account settings. {"proxy": "scheme://[user:pass@]host:port"} for our own Chrome,
      or an external browser: {"cdp": "http://127.0.0.1:9222" | "ws://..."} or {"cdp_cmd": [...]} printing that URL
      or {"ws": ...}, with optional "open_cmd" / "close_cmd" run before and after.
    """

    def __init__(self, home=None):
        self.home = Path(home or os.environ.get("MUSE2API_HOME") or Path.home() / ".config" / "muse2api")
        self.accounts_file = self.home / "accounts.json"

    def cookie_path(self, account):
        return self.home / f"{account}.json"

    def _conf(self):
        return json.loads(self.accounts_file.read_text()) if self.accounts_file.exists() else {}

    def account_conf(self, account):
        return self._conf().get(account, {})

    def accounts(self):
        """Every usable account, main first: a saved cookie file, or an external browser in accounts.json."""
        names = {p.stem for p in self.home.glob("*.json") if p != self.accounts_file}
        return sorted(names | {k for k, v in self._conf().items() if external(v)}, key=lambda x: (x != "main", x))

    def save_cookies(self, account, cookies):
        self.home.mkdir(parents=True, exist_ok=True)
        path = self.cookie_path(account)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cookies))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        names = {c["name"] for c in cookies}
        if "hatch_sess" not in names:
            raise MuseError(f"saved {len(cookies)} cookies but no hatch_sess: not logged in to muse.ai")
        exp = next(c.get("expires", 0) for c in cookies if c["name"] == "hatch_sess")
        log(f"saved {len(cookies)} cookies to {path}; hatch_sess expires {time.strftime('%Y-%m-%d', time.localtime(exp))}")

    def set_account(self, account, **settings):
        """Merge settings (e.g. proxy=..., cdp=...) into the account's entry in accounts.json."""
        self.home.mkdir(parents=True, exist_ok=True)
        conf = self._conf()
        conf.setdefault(account, {}).update(settings)
        self.accounts_file.write_text(json.dumps(conf, indent=1))
        os.chmod(self.accounts_file, 0o600)


def external(conf):
    """True when the account runs in a browser we attach to instead of our own Chrome."""
    return bool(conf.get("cdp") or conf.get("cdp_cmd"))


def run_cmd(cmd, lock_dir):
    """An account's open/close/cdp command. One at a time across runs: browser managers often break under parallel calls."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / "cmd.lock", "w") as lk:
        if os.name != "nt":
            import fcntl
            fcntl.flock(lk, fcntl.LOCK_EX)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise MuseError(f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def ws_url(endpoint):
    """A CDP endpoint as given (ws://..., http://host:port, or JSON {"ws": ...}) -> the browser's WebSocket URL."""
    endpoint = endpoint.strip()
    if endpoint.startswith("{"):
        endpoint = json.loads(endpoint)["ws"]
    if endpoint.startswith(("ws://", "wss://")):
        return endpoint
    with urlopen(endpoint.rstrip("/") + "/json/version", timeout=10) as r:
        return json.loads(r.read())["webSocketDebuggerUrl"]


def chrome_user_dir():
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data"
    return Path.home() / ".config/google-chrome"


async def cookies_from_chrome():
    port_file = chrome_user_dir() / "DevToolsActivePort"
    port, path = [l.strip() for l in port_file.read_text().splitlines()][:2]
    log("reading cookies from Chrome; click Allow if Chrome asks about remote debugging")
    async with websockets.connect(f"ws://127.0.0.1:{port}{path}", max_size=None, open_timeout=120) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            r = json.loads(await ws.recv())
            if r.get("id") == 1:
                return r["result"]["cookies"]


# ---------------- headless Chrome over CDP ----------------
class Browser:
    def __init__(self):
        self.proc = self.ws = None
        self.n, self.pending, self.handlers = 0, {}, []

    async def start(self, profile=None, headless=True, proxy=None):
        """profile = a kept user-data-dir (else a temp one, removed on stop); proxy = scheme://[user:pass@]host:port."""
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        self.temp = profile is None
        self.profile = tempfile.mkdtemp(prefix="muse2api-") if self.temp else str(profile)
        Path(self.profile).mkdir(parents=True, exist_ok=True)
        args = [CHROME, f"--remote-debugging-port={port}", f"--user-data-dir={self.profile}", "--no-first-run",
                "--no-default-browser-check", "--lang=en-US", "--window-size=1280,1100"]
        self.auth = None
        if proxy:
            u = urlsplit(proxy)
            args.append(f"--proxy-server={u.scheme}://{u.hostname}:{u.port}")
            if u.username:
                self.auth = {"username": unquote(u.username), "password": unquote(u.password or "")}
        if headless:
            args.append("--headless=new")
        self.proc = subprocess.Popen(args + ["about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                ver = json.loads(subprocess.run(["curl", "-s", f"http://127.0.0.1:{port}/json/version"],
                                                capture_output=True, text=True).stdout)
                break
            except ValueError:
                await asyncio.sleep(0.2)
        else:
            raise MuseError("headless Chrome did not start")
        self.ws = await websockets.connect(ver["webSocketDebuggerUrl"], max_size=None)
        self.reader = asyncio.create_task(self._read())

    async def attach(self, conf, lock_dir):
        """A browser someone else runs (a profile manager, your own Chrome with --remote-debugging-port): its
        profile, proxy and cookies stay its own. Runs "open_cmd" if set, then connects to its CDP endpoint."""
        self.ext, self.lock_dir, self.temp = conf, lock_dir, False
        if conf.get("open_cmd"):
            run_cmd(conf["open_cmd"], lock_dir)
        for _ in range(30):
            try:
                ws = ws_url(run_cmd(conf["cdp_cmd"], lock_dir) if conf.get("cdp_cmd") else conf["cdp"])
                break
            except (MuseError, OSError, ValueError, KeyError):
                await asyncio.sleep(1)
        else:
            raise MuseError(f"no CDP endpoint from {conf.get('cdp_cmd') or conf.get('cdp')}")
        self.ws = await websockets.connect(ws, max_size=None)
        self.reader = asyncio.create_task(self._read())

    async def proxy_auth(self, sid):
        """Answer the proxy's login challenge for this page (Chrome's --proxy-server takes no credentials)."""
        if not self.auth:
            return

        def on(msg):
            p, m = msg.get("params", {}), msg.get("method")
            if m == "Fetch.authRequired":
                asyncio.create_task(self.call("Fetch.continueWithAuth", {"requestId": p["requestId"],
                    "authChallengeResponse": {"response": "ProvideCredentials", **self.auth}}, sid))
            elif m == "Fetch.requestPaused":
                asyncio.create_task(self.call("Fetch.continueRequest", {"requestId": p["requestId"]}, sid))
        self.handlers.append(on)
        await self.call("Fetch.enable", {"handleAuthRequests": True, "patterns": [{"urlPattern": "*"}]}, sid)

    async def _read(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] in self.pending:
                self.pending.pop(msg["id"]).set_result(msg)
            else:
                for h in self.handlers:
                    h(msg)

    async def call(self, method, params=None, sid=None, timeout=60):
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.n] = fut
        msg = {"id": self.n, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        await self.ws.send(json.dumps(msg))
        r = await asyncio.wait_for(fut, timeout)
        if "error" in r:
            raise MuseError(f"{method}: {r['error'].get('message')}")
        return r["result"]

    async def stop(self):
        try:
            if self.ws:
                await self.ws.close()
        finally:
            if getattr(self, "ext", None) and self.ext.get("close_cmd"):  # its session and cookies are kept
                try:
                    run_cmd(self.ext["close_cmd"], self.lock_dir)
                except MuseError as e:
                    log(f"close_cmd: {e}")
            if self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            if getattr(self, "temp", False):
                shutil.rmtree(self.profile, ignore_errors=True)


class Page:
    """One muse.ai tab for one account: the low-level driver behind muse2api.Muse."""

    def __init__(self, account, store, headless=True):
        self.account, self.store, self.headless, self.b = account, store, headless, Browser()
        self.api = False  # True: send through the page's gateway (chat.stream) instead of typing in the composer
        self.chat_sid = None  # side chat the API send went to; its turn is read from chat.history, not live events

    async def js(self, expr, timeout=60):
        r = await self.b.call("Runtime.evaluate", {"expression": expr, "awaitPromise": True,
                                                   "returnByValue": True}, self.sid, timeout)
        if "exceptionDetails" in r:
            raise MuseError(f"js: {r['exceptionDetails'].get('text')} {str(r['exceptionDetails'])[:300]}")
        return r["result"].get("value")

    async def open(self, login_wait=0):
        """login_wait > 0: a person logs in in the (visible) window; wait up to that many seconds for the composer."""
        conf = self.store.account_conf(self.account)
        path, ext = self.store.cookie_path(self.account), external(conf)
        if ext:  # the browser is logged in already; no cookie file, proxy or user agent of ours
            await self.b.attach(conf, self.store.home)
        else:
            if not path.exists() and not login_wait:
                raise MuseError(f"no cookies for account '{self.account}': log in first (`muse2api login`)")
            await self.b.start(self.store.home / "profiles" / self.account, self.headless, conf.get("proxy"))
            if path.exists():
                await self.b.call("Storage.setCookies", {"cookies": normalize(json.loads(path.read_text()))})
        t = await self.b.call("Target.createTarget", {"url": "about:blank"})
        self.target = t["targetId"]
        self.sid = (await self.b.call("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True}))["sessionId"]
        if not ext:
            await self.b.proxy_auth(self.sid)
            await self.b.call("Network.setUserAgentOverride", {"userAgent": UA, "acceptLanguage": "en-US,en"}, self.sid)
        await self.b.call("Page.enable", {}, self.sid)
        await self.b.call("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK}, self.sid)
        await self.b.call("Page.navigate", {"url": MUSE}, self.sid)
        if login_wait:
            log(f"log in to muse.ai in the Chrome window (account '{self.account}'); waiting up to {login_wait // 60} min")
        for _ in range(max(120, login_wait)):  # hydrated composer = logged in and ready (slow loads seen up to ~60 s)
            await asyncio.sleep(1)
            try:
                if await self.js("""(()=>{const t=document.querySelector('textarea');
                        return !!t&&Object.keys(t).some(k=>k.startsWith('__reactProps'))})()"""):
                    break
            except MuseError:
                pass
        else:
            url = await self.js("location.href")
            raise MuseError(f"composer did not load at {url}: cookies expired? log in again (`muse2api login`)")
        await asyncio.sleep(2)

    async def new_side_chat(self):
        """A fresh side chat per generation: Muse carries refusals and earlier outputs along a chat's context."""
        before = await self.js("location.href")
        for i in range(12):  # the button sits in the Chats panel (2026-10-27: hidden until the panel is open)
            ok = await self.js("""(()=>{const b=[...document.querySelectorAll('button')].find(b=>
                    (b.getAttribute('aria-label')||b.innerText||'').trim()==='New side chat');if(!b)return false;setTimeout(()=>b.click(),0);return true})()""")
            if ok:
                break
            if i == 0:  # open the panel once (a second click would close it)
                await self.js("""(()=>{const b=[...document.querySelectorAll('button')].find(b=>
                    (b.innerText||'').trim().startsWith('Chats'));if(b)setTimeout(()=>b.click(),0)})()""")
            await asyncio.sleep(1)
        if not ok:
            raise MuseError("'New side chat' button not found")
        for _ in range(20):
            await asyncio.sleep(1)
            url = await self.js("location.href")
            if url != before and "/thread/" in url and await self.js(
                    "(()=>{const t=document.querySelector('textarea');return !!t&&!t.disabled})()"):
                await asyncio.sleep(2)
                log(f"new side chat {url}")
                return url
        raise MuseError("side chat did not open")

    async def gw(self, method, params, timeout=120):
        """One call through the page's own gateway client; returns its JSON answer."""
        r = await self.js("""(async()=>{%s;const g=window.__museGw();if(!g||!g.isReady)throw new Error('gateway not ready');
            return JSON.stringify(await g.sendRequest(%s,%s))})()""" % (GW, json.dumps(method), json.dumps(params)), timeout)
        return json.loads(r) if r else None

    async def api_ready(self, refs):
        """Can this turn go through chat.stream? Refs go inline as {type: image, data_base64}; that shape is read from the
        bundle but not yet proven with a real ref, so refs need MUSE2API_API_REFS=1 (else the composer path is used)."""
        if os.environ.get("MUSE2API_DOM_SEND") == "1":
            return False
        if refs and (os.environ.get("MUSE2API_API_REFS") != "1" or any(
                p.suffix.lower() not in API_IMG or p.stat().st_size > 4 << 20 for p in refs)):
            return False
        try:
            return bool(await self.js("""(()=>{%s;const g=window.__museGw();
                return !!g&&g.isReady&&!!localStorage.getItem('hatch:debug:device-id')})()""" % GW))
        except MuseError as e:
            log(f"gateway check failed: {e}")
            return False

    def new_api_thread(self):
        """A new side chat is just a fresh session id: the VM creates it on the first chat.stream."""
        self.chat_sid = str(uuid.uuid4())
        log(f"new side chat {self.chat_sid} (api)")
        return f"{MUSE}thread/{self.chat_sid}"

    async def send_api(self, prompt, refs):
        items = [{"type": "image", "mime_type": API_IMG[p.suffix.lower()], "filename": p.name,
                  "data_base64": base64.b64encode(p.read_bytes()).decode()} for p in refs]
        items.append({"type": "text", "text": prompt})
        self.t0 = await self.js("Date.now()")
        # No retry or composer fallback from here on: a failed answer may still have been delivered (a second send
        # would start a second generation).
        r = await self.js("""(async()=>{%s;const g=window.__museGw();const b={items:%s,
            node_id:localStorage.getItem('hatch:debug:device-id'),capabilities:%s};const s=%s;if(s)b.session_id=s;
            return JSON.stringify(await g.sendRequest('chat.stream',b))})()""" % (
            GW, json.dumps(items), json.dumps(CAPS), json.dumps(self.chat_sid)), timeout=120)
        r = json.loads(r or "{}")
        if not r.get("message_id"):
            raise MuseError(f"chat.stream gave no message_id: {str(r)[:200]}")
        self.msg_id = r["message_id"]
        if r.get("session_id") and self.chat_sid and r["session_id"] != self.chat_sid:
            raise MuseError(f"chat.stream answered for session {r['session_id']}, sent {self.chat_sid}")
        log(f"sent via chat.stream: message {self.msg_id}" + (f" in {self.chat_sid}" if self.chat_sid else ""))

    async def history_events(self):
        """The side chat's turn from chat.history (same payloads as the live events), plus one task.status from
        sessions.get: running until the session reads finished and an answer follows our message."""
        h = await self.gw("chat.history", {"session_id": self.chat_sid, "limit": 100}) or {}
        evs = [{"event": e.get("event_name"), "payload": e.get("payload") or {}} for e in h.get("chat_events") or []]
        idx = next((i for i, e in enumerate(evs) if e["event"] == "message.user"
                    and e["payload"].get("message_id") == self.msg_id), None)
        if idx is None:
            return []
        evs = evs[idx:]
        s = await self.gw("sessions.get", {"id": self.chat_sid}) or {}
        answered = any(e["event"] in ("message.assistant", "delta.presentation") for e in evs[1:])
        st = s.get("status") if answered and s.get("status") in DONE else "running"
        return evs + [{"event": "task.status", "payload": {"status": st, "session_status": s.get("status")}}]

    async def send(self, prompt, refs):
        self.prompt = prompt
        if self.api:
            return await self.send_api(prompt, refs)
        if refs:
            doc = await self.b.call("DOM.getDocument", {"depth": 0}, self.sid)
            node = await self.b.call("DOM.querySelector", {"nodeId": doc["root"]["nodeId"],
                                                           "selector": "input[type=file]"}, self.sid)
            if not node["nodeId"]:
                raise MuseError("composer file input not found")
            await self.b.call("DOM.setFileInputFiles", {"nodeId": node["nodeId"],
                                                        "files": [str(p) for p in refs]}, self.sid)
            for _ in range(20):
                await asyncio.sleep(1)
                n = await self.js("(()=>{const t=document.querySelector('textarea');let f=t;for(let i=0;i<8&&f;i++)f=f.parentElement;"
                                  "return f?f.querySelectorAll('img').length:-1})()")
                if n >= len(refs):
                    break
            log(f"attached {len(refs)} ref(s); composer previews={n}")
        await self.js("document.querySelector('textarea').focus()")
        await self.b.call("Input.insertText", {"text": prompt}, self.sid)
        await asyncio.sleep(0.5)
        if await self.js("document.querySelector('textarea').value.length") < len(prompt) * 0.9:
            raise MuseError("prompt did not reach the composer")
        self.t0 = await self.js("Date.now()")
        for kind in ("keyDown", "keyUp"):
            await self.b.call("Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter",
                                                         "windowsVirtualKeyCode": 13, "text": "\r" if kind == "keyDown" else ""}, self.sid)

    async def events(self):
        """This turn's events: those that arrive after our own message.user (history replays come before it)."""
        if self.chat_sid and self.api:  # an API side chat is not open in the page: no live events for it
            return await self.history_events()
        evs = json.loads(await self.js(
            f"JSON.stringify((window.__muse||{{ev:[]}}).ev.filter(e=>e[0]>={self.t0}).map(e=>e[1]))") or "[]")
        users = [i for i, e in enumerate(evs) if e["event"] == "message.user"]
        mine = [i for i in users if self.prompt.strip()[:60] in
                str(evs[i]["payload"].get("display_text") or evs[i]["payload"].get("content") or "")]
        idx = (mine or users or [None])[-1]
        return evs[idx:] if idx is not None else []

    async def wait(self, timeout):
        deadline, seen_user, last, settled_at = time.time() + timeout, False, "", None
        while time.time() < deadline:
            evs = await self.events()
            seen_user = seen_user or any(e["event"] == "message.user" for e in evs)
            status = [e["payload"].get("status") for e in evs if e["event"] == "task.status"]
            if status and status[-1] != last:
                last = status[-1]
                log(f"task {last}; media so far {len(media(evs))}")
            done = seen_user and status and status[-1] not in RUNNING
            # A refusal is a plain reply (delta.message_done) with no closing task.status: settle on it after 45 s.
            said = [e for e in evs if e["event"] == "delta.message_done"]
            if not done and seen_user and said and not media(evs) and (not status or status[-1] not in RUNNING):
                settled_at = settled_at or time.time()
                done = time.time() - settled_at > 45
            else:
                settled_at = None
            if done:
                await asyncio.sleep(3)  # presentations can trail the status
                evs = await self.events()
                return media(evs), reply_text(evs), evs
            await asyncio.sleep(8)
        self.last_events = evs  # kept for --events, to see why the turn never closed
        raise MuseError(f"no finished turn after {timeout}s")

    async def download_raw(self, item, out_dir):
        """fs.raw through the page's gateway: sandbox://workspace/x -> VM path workspace/x, the original bytes (the
        <vm>.metaaivm.com/media/raw/ URL in the event has no public DNS). Moved out in 4 MB base64 slices."""
        rel = vm_path(item["path"])
        dst = out_dir / rel.rsplit("/", 1)[-1]
        key = "__raw" + uuid.uuid4().hex[:8]
        n = await self.js("""(async()=>{%s;const g=window.__museGw();if(!g||!g.isReady)throw new Error('gateway not ready');
            const b=await g.sendRequest('fs.raw',{path:%s});if(!(b instanceof Blob))throw new Error('fs.raw: not a Blob');
            window.%s=new Uint8Array(await b.arrayBuffer());return window.%s.length})()""" % (GW, json.dumps(rel), key, key),
                          timeout=600)
        try:
            with open(dst, "wb") as f:
                for off in range(0, n, 4 << 20):
                    f.write(base64.b64decode(await self.js("""(()=>{const b=window.%s.subarray(%d,%d);let s='';
                        for(let i=0;i<b.length;i+=32768)s+=String.fromCharCode.apply(null,b.subarray(i,i+32768));
                        return btoa(s)})()""" % (key, off, off + (4 << 20)), timeout=120)))
        finally:
            await self.js(f"delete window.{key}")
        size = dst.stat().st_size
        if size == 0 or ("byte_len" in item and size != item["byte_len"]):
            dst.unlink(missing_ok=True)
            raise MuseError(f"{dst.name}: fs.raw gave {size} bytes, expected {item.get('byte_len')}")
        log(f"saved {dst.name} ({size} bytes, fs.raw)")
        return {"file": str(dst), "bytes": size, "mime": item.get("mime"), "path": item["path"]}

    async def show_thread(self):
        """The DOM fallbacks need the API side chat rendered in the page (once)."""
        if self.chat_sid and self.chat_sid not in (await self.js("location.href") or ""):
            await self.b.call("Page.navigate", {"url": f"{MUSE}thread/{self.chat_sid}"}, self.sid)
            await asyncio.sleep(12)

    async def download(self, item, out_dir):
        try:
            return await self.download_raw(item, out_dir)
        except (MuseError, asyncio.TimeoutError) as e:
            log(f"fs.raw failed for {item.get('path')}: {e or type(e).__name__}; trying the page's media controls")
        await self.show_thread()
        if str(item.get("mime", "")).startswith("image/"):
            return await self.download_image(item, out_dir)
        name = item["path"].rsplit("/", 1)[-1]
        dst = out_dir / name
        tmp = Path(tempfile.mkdtemp(prefix="muse2api-dl-"))
        done = asyncio.get_running_loop().create_future()

        def on(msg):
            p = msg.get("params", {})
            if msg.get("method") == "Browser.downloadProgress" and p.get("state") in ("completed", "canceled") \
                    and not done.done():
                done.set_result(p)
        self.b.handlers.append(on)
        try:
            await self.b.call("Browser.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": str(tmp),
                                                              "eventsEnabled": True})
            # the media's own control carries its file name; its message holds the Download button
            for _try in range(15):  # the media control can render a few seconds after the turn ends
                clicked = await self.js("""(()=>{const n=%s;
                    const el=[...document.querySelectorAll('[aria-label],img[alt]')].find(e=>
                        (e.getAttribute('aria-label')||e.getAttribute('alt')||'').includes(n));
                    if(!el)return 'no-media';let a=el;
                    for(let i=0;i<12&&a;i++,a=a.parentElement){const b=[...a.querySelectorAll('button[aria-label]')]
                        .find(b=>/download/i.test(b.getAttribute('aria-label')));if(b){b.click();return 'ok'}}
                    return 'no-button'})()""" % json.dumps(name))
                if clicked != "no-media":
                    break
                await asyncio.sleep(2)
            if clicked == "no-button":  # 2026-10-27: no Download button; the player holds the file as a blob
                return await self.download_blob(item, name, dst)
            if clicked != "ok":
                raise MuseError(f"{name}: download button {clicked}")
            p = await asyncio.wait_for(done, 300)
            if p["state"] != "completed":
                raise MuseError(f"{name}: download {p['state']}")
            shutil.move(str(tmp / p["guid"]), dst)
        finally:
            self.b.handlers.remove(on)
            shutil.rmtree(tmp, ignore_errors=True)
        size = dst.stat().st_size
        if size == 0 or ("byte_len" in item and size != item["byte_len"]):
            raise MuseError(f"{name}: got {size} bytes, expected {item.get('byte_len')}")
        return {"file": str(dst), "bytes": size, "mime": item.get("mime"), "path": item["path"]}

    async def download_blob(self, item, name, dst):
        """Read the <video> blob inside the media control named after the file, in 4 MB base64 chunks."""
        for _try in range(10):  # 2026-10-27: the player only gets its src once scrolled into view
            n = await self.js("""(async()=>{const el=[...document.querySelectorAll('[aria-label]')].find(e=>
                e.getAttribute('aria-label').includes(%s));if(el)el.scrollIntoView({block:'center'});
                const v=el&&(el.querySelector('video')||el.parentElement.querySelector('video'));
                if(v&&!v.src)v.load&&v.load();if(!v||!(v.currentSrc||v.src))return -1;
                window.__museBlob=new Uint8Array(await (await fetch(v.currentSrc||v.src)).arrayBuffer());
                return window.__museBlob.length})()""" % json.dumps(name), timeout=300)
            if n and n > 0:
                break
            await asyncio.sleep(3)
        if not n or n < 0:
            raise MuseError(f"{name}: no video player to read")
        with open(dst, "wb") as f:
            for off in range(0, n, 4 << 20):
                f.write(base64.b64decode(await self.js("""(()=>{const b=window.__museBlob.subarray(%d,%d);let s='';
                    for(let i=0;i<b.length;i+=32768)s+=String.fromCharCode.apply(null,b.subarray(i,i+32768));
                    return btoa(s)})()""" % (off, off + (4 << 20)), timeout=120)))
        await self.js("delete window.__museBlob")
        size = dst.stat().st_size
        if size == 0 or ("byte_len" in item and size != item["byte_len"]):
            raise MuseError(f"{name}: got {size} bytes, expected {item.get('byte_len')}")
        log(f"saved {dst.name} ({size} bytes, from the player)")
        return {"file": str(dst), "bytes": size, "mime": item.get("mime"), "path": item["path"]}

    async def download_image(self, item, out_dir):
        """Images are labelled by caption, not file name: read the rendered <img> bytes from the page."""
        name = item["path"].rsplit("/", 1)[-1]
        dst = out_dir / name
        key = await self.js("""(async()=>{const n=%s,l=%s;
            const btn=[...document.querySelectorAll('button[aria-label]')].filter(b=>{const a=b.getAttribute('aria-label');
                return a.includes(n)||(l&&a==='Open generated image '+l)}).pop();
            const img=btn&&btn.querySelector('img');
            if(!img)return null;const b=await (await fetch(img.currentSrc||img.src)).blob();
            const k='__img'+Math.random().toString(36).slice(2);window[k]=new Uint8Array(await b.arrayBuffer());return k})()"""
                            % (json.dumps(name), json.dumps(item.get("label") or "")), timeout=120)
        if not key:
            alts = await self.js("JSON.stringify([...document.querySelectorAll('img')].map(i=>i.alt).filter(Boolean).slice(-15))")
            raise MuseError(f"{name}: image not found on the page (img alts: {alts})")
        total = await self.js(f"window.{key}.length")
        buf = bytearray()
        for off in range(0, total, 1 << 20):  # 1 MB base64 slices through Runtime.evaluate
            chunk = await self.js(f"(()=>{{const a=window.{key}.subarray({off},{off + (1 << 20)});let s='';"
                                  "for(let i=0;i<a.length;i+=32768)s+=String.fromCharCode(...a.subarray(i,i+32768));return btoa(s)})()")
            buf += base64.b64decode(chunk)
        await self.js(f"delete window.{key}")
        dst.write_bytes(buf)
        if "byte_len" in item and len(buf) != item["byte_len"]:
            raise MuseError(f"{name}: got {len(buf)} bytes, expected {item['byte_len']} (page may show a resized copy)")
        return {"file": str(dst), "bytes": len(buf), "mime": item.get("mime"), "path": item["path"]}

    async def usage(self):
        """Read Settings > General > Usage: {plan, weekly_pct, resets, extra_pct, extra_left}. The numbers come over
        the encrypted gateway, so the rendered panel is the only place to read them."""
        await self.js("""(()=>{const b=document.querySelector('button[aria-label="Settings"]');
            ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(t=>b.dispatchEvent(new MouseEvent(t,{bubbles:true})))})()""")
        await asyncio.sleep(1.5)
        await self.js("(()=>{const i=[...document.querySelectorAll('[role=menuitem]')].find(e=>/^settings/i.test(e.innerText.trim()));if(i)i.click()})()")
        text = ""
        for i in range(25):
            await asyncio.sleep(1)
            text = await self.js("(()=>{const d=document.querySelector('[role=dialog]');return d?d.innerText:''})()") or ""
            if re.search(r"\d+% used", text):
                break
            if i in (5, 12):  # 2026-10-27: sometimes the panel opens without the usage block: go to General /
                # Usage and scroll the dialog (the numbers load late and sit below the fold)
                await self.js("""(()=>{const d=document.querySelector('[role=dialog]');if(!d)return;
                    const t=[...d.querySelectorAll('button,[role=tab],[role=menuitem],a,div')].find(e=>
                        /^(usage|general)$/i.test((e.innerText||'').trim()));if(t)t.click();
                    d.querySelectorAll('*').forEach(e=>{if(e.scrollHeight>e.clientHeight+20)e.scrollTop=e.scrollHeight})})()""")
        for kind in ("keyDown", "keyUp"):  # close the dialog
            await self.b.call("Input.dispatchKeyEvent", {"type": kind, "key": "Escape", "code": "Escape",
                                                         "windowsVirtualKeyCode": 27}, self.sid)
        await asyncio.sleep(0.5)
        return parse_usage(text)

    async def close(self):
        try:
            if self.b.ws and getattr(self.b, "ext", None) and getattr(self, "target", None):
                await self.b.call("Target.closeTarget", {"targetId": self.target})  # leave their browser as it was
            elif self.b.ws:  # keep the rolling session: write refreshed cookies back
                ck = (await self.b.call("Storage.getCookies"))["cookies"]
                ck = [c for c in ck if c["domain"].endswith("muse.ai")]
                if any(c["name"] == "hatch_sess" for c in ck):
                    self.store.save_cookies(self.account, ck)
        except Exception as e:
            log(f"cookie write-back skipped: {e}")
        await self.b.stop()


def vm_path(path):
    """sandbox://workspace/x (an event's path) -> workspace/x, the path fs.raw takes (relative to the VM home)."""
    return path.split("://", 1)[1] if path.startswith("sandbox://") else path.lstrip("/")


def parse_usage(text):
    u = {"plan": None, "weekly_pct": None, "resets": None, "extra_pct": None, "extra_left": None}
    if m := re.search(r"(\w+) plan\s+Weekly limit resets on ([^\n]+?)\s+(\d+)% used", text):
        u.update(plan=m.group(1), resets=m.group(2).strip(), weekly_pct=int(m.group(3)))
    if m := re.search(r"Additional tokens[\s\S]*?(\d+)% used(?: \(([\d.]+\s*[KMB]?) tokens left\))?", text):
        u.update(extra_pct=int(m.group(1)), extra_left=(m.group(2) or "").replace(" ", "") or None)
    if u["weekly_pct"] is None:
        raise MuseError(f"usage panel not readable: {text[:200]!r}")
    return u


def exhausted(u):
    """No room left: weekly limit full and no additional tokens (or none left)."""
    extra_empty = u["extra_left"] in (None, "0") or (u["extra_pct"] or 0) >= 100
    return u["weekly_pct"] >= 100 and extra_empty


def media(evs):
    """Every generated file (video or image) the turn presented, deduplicated by path."""
    out, seen = [], set()
    for e in evs:
        if e["event"] != "delta.presentation":
            continue
        for items in (e["payload"].get("data") or {}).values():
            if not isinstance(items, list):
                continue
            for it in items:
                if isinstance(it, dict) and isinstance(it.get("path"), str) and it["path"] not in seen \
                        and str(it.get("mime", "")).startswith(("video/", "image/")):
                    seen.add(it["path"])
                    out.append(it)
    return out


def reply_text(evs):
    """Assistant text of the turn, from delta.message_done transcripts."""
    texts = []
    for e in evs:
        if e["event"] != "delta.message_done":
            continue
        for msg in (e["payload"].get("transcript") or {}).get("messages") or []:
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") in ("text", "output_text") and c.get("text"):
                    texts.append(c["text"])
    if not texts:  # chat.history (API side chats) has message.assistant events instead
        texts = [e["payload"]["display_text"] for e in evs
                 if e["event"] == "message.assistant" and e["payload"].get("display_text")]
    return "\n".join(texts)[-2000:]


def ram_used_pct():
    """System RAM in use, %; 0 when it cannot be read (macOS memory_pressure or Linux /proc/meminfo)."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True).stdout
            m = re.search(r"free percentage:\s*(\d+)", out)
            return 100 - int(m.group(1)) if m else 0
        info = dict(l.split(":", 1) for l in Path("/proc/meminfo").read_text().splitlines() if ":" in l)
        total, avail = (int(info[k].split()[0]) for k in ("MemTotal", "MemAvailable"))
        return round(100 * (total - avail) / total)
    except (OSError, KeyError, ValueError):
        return 0
