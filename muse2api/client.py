"""The public API: one Muse account, used as an async context manager."""
import asyncio
import json
import mimetypes
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ._core import (LimitReached, MuseError, Page, Store, cookies_from_chrome, exhausted, log, media, normalize,
                    parse_usage, reply_text)


@dataclass
class MediaFile:
    """A generated file saved to disk. `path` is its place in the account's Muse workspace (sandbox://...)."""
    file: Path
    bytes: int
    mime: str | None
    path: str

    def to_dict(self):
        return {**asdict(self), "file": str(self.file)}


@dataclass
class Usage:
    """Settings > Usage: % of the weekly limit used, when it resets, and the additional tokens."""
    plan: str | None
    weekly_pct: int
    resets: str | None
    extra_pct: int | None
    extra_left: str | None

    @property
    def exhausted(self):
        """No room left: weekly limit full and no additional tokens (or none left)."""
        return exhausted(asdict(self))

    def to_dict(self):
        return asdict(self)


@dataclass
class Turn:
    """One answered message: Muse's reply text and the media items it presented (not downloaded yet)."""
    reply: str
    items: list = field(default_factory=list)
    thread: str | None = None
    events: list = field(default_factory=list)


@dataclass
class Generation:
    """What `Muse.generate` made. ok is False when Muse answered without media (see `reply`)."""
    ok: bool
    media: list[MediaFile]
    reply: str
    thread: str | None = None
    preamble: list[str] = field(default_factory=list)
    usage: Usage | None = None
    usage_before: Usage | None = None
    events: list = field(default_factory=list)

    @property
    def cost_weekly_pct(self):
        if self.usage and self.usage_before:
            return self.usage.weekly_pct - self.usage_before.weekly_pct

    def to_dict(self):
        return {"thread": self.thread, "preamble": self.preamble, "ok": self.ok, "reply": self.reply,
                "media": [m.to_dict() for m in self.media], "usage": self.usage and self.usage.to_dict()}


def _usage(u):
    return Usage(**u)


def _thread_id(thread):
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", thread or "")
    if not m:
        raise MuseError(f"not a chat id: {thread}")
    return m.group(1)


class Muse:
    """One Muse account in a browser.

        async with Muse("main") as m:
            g = await m.generate("a cat surfing, 10 s video", refs=["cat.jpg"], out="out/")

    account: a name under `home` (cookies from `login_*`, or an external browser in accounts.json).
    home: the account folder (default $MUSE2API_HOME or ~/.config/muse2api).
    headless: False shows the Chrome window (own Chrome only; an external browser keeps its own setting).
    usage_log: a JSONL file; every generate appends the usage before and after, so each file's cost is known.
    """

    def __init__(self, account="main", *, home=None, headless=True, usage_log=None):
        self.account, self.store = account, home if isinstance(home, Store) else Store(home)
        self.usage_log = Path(usage_log) if usage_log else None
        self.page = Page(account, self.store, headless)
        self._open = False

    async def __aenter__(self):
        try:
            await self.open()
        except BaseException:  # a failed open still closes the Chrome/tab it started
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def open(self, login_wait=0):
        await self.page.open(login_wait)
        self._open = True

    async def close(self):
        """Close the tab (an external browser) or our Chrome, writing refreshed cookies back."""
        await self.page.close()
        self._open = False

    def _log_usage(self, at, u, **extra):
        if self.usage_log:
            self.usage_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.usage_log, "a") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "account": self.account, "at": at,
                                    **u.to_dict(), **extra}, ensure_ascii=False) + "\n")

    async def usage(self) -> Usage:
        u = _usage(await self.page.usage())
        self._log_usage("check", u)
        return u

    async def _goto_thread(self, thread):
        p = self.page
        await p.b.call("Page.navigate", {"url": thread}, p.sid)
        for _ in range(30):
            await asyncio.sleep(1)
            if await p.js("(()=>{const t=document.querySelector('textarea');return !!t&&!t.disabled})()"):
                break
        else:
            raise MuseError(f"thread did not load: {thread}")
        await asyncio.sleep(3)

    async def start_chat(self, refs=(), thread=None, new_chat=True):
        """Pick the chat the next `send` goes to: `thread` (an earlier chat's URL or id), a new side chat
        (default) or, with new_chat=False, the main chat. Returns the chat URL (None for the main chat)."""
        p = self.page
        p.api = await p.api_ready([Path(r).resolve() for r in refs])
        log("send: " + ("chat.stream (api)" if p.api else "composer (DOM)"))
        if thread:
            sid = _thread_id(thread)
            url = thread if thread.startswith("http") else f"https://muse.ai/thread/{sid}"
            await self._goto_thread(url)
            if p.api:
                p.chat_sid = sid
            return url
        if new_chat:
            return p.new_api_thread() if p.api else await p.new_side_chat()
        return None

    async def send(self, text, refs=(), timeout=900) -> Turn:
        """Send one message in the current chat (see `start_chat`) and wait for Muse's answer."""
        p = self.page
        await p.send(text, [Path(r).resolve() for r in refs])
        items, reply, evs = await p.wait(timeout)
        url = await p.js("location.href")
        thread = url if re.search(r"/thread/[0-9a-f-]{36}", url or "") else (
            f"https://muse.ai/thread/{p.chat_sid}" if p.chat_sid else None)
        return Turn(reply, items, thread, evs)

    async def download(self, item, out=".") -> MediaFile:
        """Save one media item (from a Turn's items or saved events), or a workspace path such as
        "sandbox://workspace/a.mp4", into the folder `out`. The size is checked against the event's byte_len."""
        if isinstance(item, str):
            item = {"path": item, "mime": mimetypes.guess_type(item)[0]}
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        return _media(await self.page.download(item, out))

    async def download_turn(self, events, out=".", thread=None) -> list[MediaFile]:
        """Re-download every file of a saved turn (`Generation.events`, or a JSON file of them)."""
        if isinstance(events, (str, Path)):
            events = json.loads(Path(events).read_text())
        if thread:  # a side chat: the DOM fallback needs the turn rendered in its own thread
            await self.page.b.call("Page.navigate", {"url": thread}, self.page.sid)
            await asyncio.sleep(10)
        return [await self.download(i, out) for i in media(events)]

    async def generate(self, prompt, refs=(), out=".", *, timeout=900, thread=None, new_chat=True, preamble=(),
                       preamble_refs=()) -> Generation:
        """Ask for media and save every file it presents into `out`.

        preamble: messages sent (and answered) first in the same chat, as context; preamble_refs go with the first.
        Raises LimitReached when the account is out of quota and MuseError when a step fails (its `thread`,
        `preamble` and `events` attributes hold what was done so far). Not retried: a resend could start a second
        paid generation.
        """
        g = Generation(ok=False, media=[], reply="")
        try:
            g.usage_before = await self.usage()
            if g.usage_before.exhausted:
                raise LimitReached(f"account '{self.account}' is out of quota: {g.usage_before.to_dict()}")
            g.thread = await self.start_chat(list(refs) + list(preamble_refs), thread, new_chat)
            for i, text in enumerate(preamble):
                t = await self.send(text, preamble_refs if i == 0 else (), min(timeout, 300))
                log(f"preamble {i + 1}: {t.reply[:300]!r}")
                g.preamble.append(t.reply)
            t = await self.send(prompt, refs, timeout)
            g.thread, g.reply, g.events = t.thread or g.thread, t.reply, t.events
            if t.items and g.thread and not self.page.api:  # a chat opened as /thread/new renders media after a reload
                await self.page.b.call("Page.navigate", {"url": g.thread}, self.page.sid)
                await asyncio.sleep(12)
            g.media = [await self.download(i, out) for i in t.items]
            g.ok = bool(g.media)
            g.usage = _usage(await self.page.usage())
            kinds = [str(m.mime or "").split("/")[0] for m in g.media]
            self._log_usage("after", g.usage, videos=kinds.count("video"), images=kinds.count("image"),
                            cost_weekly_pct=g.cost_weekly_pct,
                            cost_extra_pct=(g.usage.extra_pct or 0) - (g.usage_before.extra_pct or 0))
            return g
        except (MuseError, asyncio.TimeoutError) as e:
            err = e if isinstance(e, MuseError) else MuseError(str(e) or type(e).__name__)
            err.thread, err.preamble = g.thread, g.preamble
            err.events = g.events or getattr(self.page, "last_events", None) or []
            if err is e:
                raise
            raise err from e


def _media(d):
    return MediaFile(Path(d["file"]), d["bytes"], d.get("mime"), d["path"])


def generate(prompt, refs=(), out=".", account="main", **kw) -> Generation:
    """Blocking shortcut: open the account, generate, close."""
    opts = {k: kw.pop(k) for k in ("home", "headless", "usage_log") if k in kw}

    async def run():
        async with Muse(account, **opts) as m:
            return await m.generate(prompt, refs, out, **kw)
    return asyncio.run(run())


def accounts(home=None):
    """Every usable account name, main first."""
    return Store(home).accounts()


def login_from_file(account, cookies_file, home=None):
    """Save an account's cookies from a Cookie-Editor or CDP JSON export."""
    Store(home).save_cookies(account, normalize(json.loads(Path(cookies_file).read_text())))


def login_from_chrome(account, home=None):
    """Save an account's cookies from your running Chrome (remote debugging on, you click Allow)."""
    Store(home).save_cookies(account, normalize(asyncio.run(cookies_from_chrome())))


def login_browser(account, home=None, proxy=None, wait=900):
    """Open a visible Chrome on the account's own profile (and proxy); you log in there yourself. The cookies are
    saved once the composer loads. Raises MuseError if it does not within `wait` seconds."""
    store = Store(home)
    if proxy:
        store.set_account(account, proxy=proxy)

    async def run():
        m = Muse(account, home=store, headless=False)
        try:
            await m.open(login_wait=wait)
            log(f"logged in: account '{account}'")
        finally:
            await m.close()
    asyncio.run(run())


__all__ = ["Muse", "Generation", "Turn", "MediaFile", "Usage", "MuseError", "LimitReached", "generate", "accounts",
           "login_from_file", "login_from_chrome", "login_browser", "parse_usage"]
