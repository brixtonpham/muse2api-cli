"""The muse2api command: a thin layer over the library that prints one JSON line per command.

exit 0 = done (media saved), 2 = Muse answered without media (text in "reply"), 3 = a step failed ("error"),
4 = the account is out of quota (weekly limit full, no additional tokens).
"""
import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from ._core import ram_used_pct
from .client import LimitReached, Muse, MuseError, Store, login_browser, login_from_chrome, login_from_file


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False))


async def _usage(a, store, usage_log):
    out = {}
    for acc in store.accounts() if a.all else [a.account]:
        try:
            async with Muse(acc, home=store, usage_log=usage_log) as m:
                u = await m.usage()
            out[acc] = {**u.to_dict(), "exhausted": u.exhausted}
        except (MuseError, asyncio.TimeoutError) as e:
            out[acc] = {"error": str(e) or type(e).__name__}
    _print(out)
    return 0


async def _get(a, store):
    try:
        async with Muse(a.account, home=store) as m:
            await asyncio.sleep(5)  # let the chat history render (DOM fallback)
            files = await m.download_turn(a.events, a.out, a.thread)
        _print({"ok": True, "media": [f.to_dict() for f in files]})
        return 0
    except (MuseError, asyncio.TimeoutError) as e:
        _print({"ok": False, "error": str(e) or type(e).__name__})
        return 3


async def _gen(a, store, usage_log):
    try:
        async with Muse(a.account, home=store, usage_log=usage_log) as m:
            g = await m.generate(a.prompt, a.ref, a.out, timeout=a.timeout, thread=a.thread,
                                 new_chat=not a.main_chat, preamble=a.preamble, preamble_refs=a.preamble_ref)
        if a.events:
            Path(a.events).write_text(json.dumps(g.events, ensure_ascii=False))
        _print(g.to_dict())
        return 0 if g.ok else 2
    except LimitReached as e:
        _print({"ok": False, "error": str(e), "limit": True})
        return 4
    except MuseError as e:
        if a.events and getattr(e, "events", None):  # kept to see why the turn never closed
            Path(a.events).write_text(json.dumps(e.events, ensure_ascii=False))
        _print({"ok": False, "error": str(e), "preamble": getattr(e, "preamble", []), "thread": getattr(e, "thread", None)})
        return 3


def main(argv=None):
    # a killed run (SIGTERM) still closes its Chrome: the profile stays locked otherwise
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    ap = argparse.ArgumentParser(prog="muse2api", description=__doc__.split("\n")[0])
    ap.add_argument("--home", help="account folder (default $MUSE2API_HOME or ~/.config/muse2api)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login", help="save an account's muse.ai cookies")
    lg.add_argument("--account", default="main")
    lg.add_argument("--from-file", type=Path, help="a Cookie-Editor or CDP JSON export")
    lg.add_argument("--browser", action="store_true", help="open a Chrome window on the account's profile; "
                    "the account owner logs in there")
    lg.add_argument("--proxy", help="fixed proxy for this account, scheme://[user:pass@]host:port; kept for every run")
    lg.add_argument("--wait", type=int, default=900, help="seconds to wait for the login (--browser)")
    g = sub.add_parser("gen", help="generate media from a prompt (+ reference images)")
    g.add_argument("--account", default="main")
    g.add_argument("--prompt", required=True)
    g.add_argument("--ref", action="append", default=[], type=Path)
    g.add_argument("--out", required=True, type=Path)
    g.add_argument("--timeout", type=int, default=900)
    g.add_argument("--events", help="write the turn's raw chat events to this JSON file")
    g.add_argument("--main-chat", action="store_true", help="send in the main chat instead of a new side chat")
    g.add_argument("--thread", help="send in this existing side chat (URL from an earlier gen's \"thread\")")
    g.add_argument("--preamble", action="append", default=[], help="a message sent (and answered) before the prompt "
                   "in the same chat; repeatable")
    g.add_argument("--preamble-ref", action="append", default=[], type=Path, help="image attached to the first preamble")
    us = sub.add_parser("usage", help="weekly %% used and reset date")
    us.add_argument("--account", default="main")
    us.add_argument("--all", action="store_true", help="every saved account")
    gt = sub.add_parser("get", help="re-download a saved turn's media")
    gt.add_argument("--account", default="main")
    gt.add_argument("--events", required=True)
    gt.add_argument("--out", required=True, type=Path)
    gt.add_argument("--thread", help="the side chat URL the turn ran in (gen prints it as \"thread\")")
    a = ap.parse_args(argv)

    store = Store(a.home)
    usage_log = Path(os.environ.get("MUSE2API_USAGE_LOG") or store.home / "usage.jsonl")
    try:
        if a.cmd == "login":
            if a.browser:
                login_browser(a.account, store.home, a.proxy, a.wait)
            else:
                if a.proxy:
                    store.set_account(a.account, proxy=a.proxy)
                login_from_file(a.account, a.from_file, store.home) if a.from_file else login_from_chrome(a.account, store.home)
            return 0
    except (MuseError, asyncio.TimeoutError) as e:
        sys.exit(f"login failed: {e}")
    if a.cmd == "usage":
        return asyncio.run(_usage(a, store, usage_log))
    if a.cmd == "get":
        a.out.mkdir(parents=True, exist_ok=True)
        return asyncio.run(_get(a, store))
    for p in a.ref + a.preamble_ref:
        if not p.is_file():
            sys.exit(f"ref not found: {p}")
    a.out.mkdir(parents=True, exist_ok=True)
    limit = int(os.environ.get("MUSE2API_MAX_RAM", "80"))  # headless Chrome is heavy: don't start on a full machine
    if (used := ram_used_pct()) >= limit:
        sys.exit(f"RAM {used}% used (limit {limit}%, MUSE2API_MAX_RAM); not starting")
    return asyncio.run(_gen(a, store, usage_log))


def run():
    sys.exit(main())


if __name__ == "__main__":
    run()
