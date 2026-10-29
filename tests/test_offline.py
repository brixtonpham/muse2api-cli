"""Offline tests: the parsing and path logic, no browser or account needed. Run: python3 -m unittest -v"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import muse2api as m  # noqa: E402


class Paths(unittest.TestCase):
    def test_sandbox_prefix_dropped(self):
        self.assertEqual(m.vm_path("sandbox://workspace/imagine_media/a.mp4"), "workspace/imagine_media/a.mp4")

    def test_absolute_made_relative(self):
        self.assertEqual(m.vm_path("/workspace/x.png"), "workspace/x.png")

    def test_ws_url_passthrough_and_json(self):
        self.assertEqual(m.ws_url("ws://127.0.0.1:9222/devtools/browser/abc"), "ws://127.0.0.1:9222/devtools/browser/abc")
        self.assertEqual(m.ws_url('{"ws": "ws://127.0.0.1:1/devtools/browser/x"}'), "ws://127.0.0.1:1/devtools/browser/x")

    def test_external(self):
        self.assertTrue(m.external({"cdp": "http://127.0.0.1:9222"}))
        self.assertTrue(m.external({"cdp_cmd": ["echo", "ws://x"]}))
        self.assertFalse(m.external({"proxy": "http://h:1"}))


class Cookies(unittest.TestCase):
    def test_normalize_keeps_muse_only(self):
        out = m.normalize([
            {"name": "hatch_sess", "value": "v", "domain": ".muse.ai", "path": "/", "expirationDate": 2e9,
             "sameSite": "no_restriction", "secure": True},
            {"name": "other", "value": "v", "domain": ".example.com"},
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["sameSite"], "None")
        self.assertEqual(out[0]["expires"], 2e9)


class Usage(unittest.TestCase):
    PANEL = "Free plan\nWeekly limit resets on Nov 4\n33% used\nAdditional tokens\n0% used (1B tokens left)"

    def test_parse(self):
        u = m.parse_usage(self.PANEL)
        self.assertEqual((u["plan"], u["weekly_pct"], u["resets"], u["extra_pct"], u["extra_left"]),
                         ("Free", 33, "Nov 4", 0, "1B"))
        self.assertFalse(m.exhausted(u))

    def test_full_without_extra_is_exhausted(self):
        self.assertTrue(m.exhausted(m.parse_usage("Free plan\nWeekly limit resets on Nov 4\n100% used")))

    def test_unreadable_raises(self):
        with self.assertRaises(m.MuseError):
            m.parse_usage("loading")


class Events(unittest.TestCase):
    def test_media_dedup_and_filter(self):
        item = {"path": "sandbox://workspace/a.mp4", "mime": "video/mp4", "byte_len": 10}
        evs = [{"event": "delta.presentation", "payload": {"data": {"items": [item, item,
                {"path": "sandbox://workspace/n.txt", "mime": "text/plain"}]}}}]
        self.assertEqual(m.media(evs), [item])

    def test_reply_from_live_and_history(self):
        live = [{"event": "delta.message_done", "payload": {"transcript": {"messages": [
            {"content": [{"type": "text", "text": "hi"}]}]}}}]
        hist = [{"event": "message.assistant", "payload": {"display_text": "Test received"}}]
        self.assertEqual(m.reply_text(live), "hi")
        self.assertEqual(m.reply_text(hist), "Test received")


if __name__ == "__main__":
    unittest.main()
