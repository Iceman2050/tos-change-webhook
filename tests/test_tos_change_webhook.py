"""Tests for tos_change_webhook.

Spins up two local HTTP servers (stdlib http.server in threads):
  * a fake vendor ToS page whose content the test can change
  * a capturing webhook endpoint that records every POST body

Then: baseline -> change content -> watch -> assert a webhook payload
arrived containing the changed sentence; then a second watch with no
change -> assert silence.
"""

import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tos_change_webhook as tcw  # noqa: E402

PAGE_V1 = b"""<html><head><title>Terms</title></head>
<body><nav>Home | Pricing | Login</nav>
<h1>Terms of Service</h1>
<p>We respect your privacy and will never sell your personal data.</p>
<footer>Copyright 2026 ExampleCorp</footer>
<script>var x = 1;</script>
</body></html>"""

PAGE_V2 = b"""<html><head><title>Terms</title></head>
<body><nav>Home | Pricing | Login</nav>
<h1>Terms of Service</h1>
<p>We respect your privacy and will never sell your personal data.</p>
<p>We may share your data with third-party advertisers.</p>
<footer>Copyright 2026 ExampleCorp</footer>
<script>var x = 1;</script>
</body></html>"""


class QuietHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


class TosHandler(QuietHandler):
    content = PAGE_V1

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(TosHandler.content)


class HookHandler(QuietHandler):
    received = []  # list of raw POST bodies (bytes)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        HookHandler.received.append(body)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


def start_server(handler_cls):
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class TestTosChangeWebhook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tos_server = start_server(TosHandler)
        cls.hook_server = start_server(HookHandler)
        cls.tos_port = cls.tos_server.server_address[1]
        cls.hook_port = cls.hook_server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.tos_server.shutdown()
        cls.hook_server.shutdown()

    def setUp(self):
        TosHandler.content = PAGE_V1
        HookHandler.received = []
        self.tmp = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.tmp.name, "vendors.json")
        self.state = os.path.join(self.tmp.name, "state.json")
        self.webhook = "http://127.0.0.1:%d/hook" % self.hook_port
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(
                {"ExampleCorp Terms": "http://127.0.0.1:%d/tos" % self.tos_port},
                fh,
            )

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tcw.main(argv)
        return rc, buf.getvalue()

    def test_extract_strips_boilerplate(self):
        text = tcw.extract_text(PAGE_V1.decode("utf-8"))
        self.assertIn("never sell your personal data", text)
        self.assertNotIn("Login", text)          # nav stripped
        self.assertNotIn("Copyright", text)      # footer stripped
        self.assertNotIn("var x", text)          # script stripped
        self.assertNotIn("<", text)              # no tags left

    def test_baseline_records_hash_without_alerting(self):
        rc, out = self.run_cli(["--config", self.config, "--state", self.state, "baseline"])
        self.assertEqual(rc, 0)
        self.assertIn("recorded", out)
        self.assertEqual(HookHandler.received, [])
        with open(self.state, encoding="utf-8") as fh:
            state = json.load(fh)
        entry = state["ExampleCorp Terms"]
        self.assertEqual(entry["hash"], tcw.sha256_hex(tcw.extract_text(PAGE_V1.decode("utf-8"))))
        self.assertIsNone(entry["prev_text"])

    def test_change_triggers_webhook_with_changed_sentence(self):
        self.run_cli(["--config", self.config, "--state", self.state, "baseline"])
        TosHandler.content = PAGE_V2  # vendor edits the page
        rc, out = self.run_cli(
            ["--config", self.config, "--state", self.state,
             "watch", "--webhook", self.webhook]
        )
        self.assertEqual(rc, 0)
        # diff printed to stdout (extracted text is one collapsed line, so the
        # added diff line is the whole paragraph)
        self.assertIn("CHANGE DETECTED", out)
        self.assertIn("third-party advertisers", out)
        added_lines = [l for l in out.splitlines() if l.startswith("+") and "third-party" in l]
        self.assertTrue(added_lines, "expected an added diff line containing the change")
        # exactly one webhook payload received
        self.assertEqual(len(HookHandler.received), 1)
        payload = json.loads(HookHandler.received[0].decode("utf-8"))
        self.assertEqual(payload["vendor"], "ExampleCorp Terms")
        self.assertIn("tos", payload["url"])
        self.assertIn("detected_at", payload)
        self.assertIn("+1/-1", payload["diff_summary"])
        self.assertIn("third-party advertisers", payload["diff_excerpt"])
        # state now holds the previous text, so `diff` can replay it
        rc, out = self.run_cli(["--config", self.config, "--state", self.state,
                                "diff", "ExampleCorp Terms"])
        self.assertEqual(rc, 0)
        self.assertIn("third-party advertisers", out)

    def test_no_change_means_silence(self):
        self.run_cli(["--config", self.config, "--state", self.state, "baseline"])
        rc, out = self.run_cli(
            ["--config", self.config, "--state", self.state,
             "watch", "--webhook", self.webhook]
        )
        self.assertEqual(rc, 0)
        self.assertIn("no change", out)
        self.assertNotIn("CHANGE DETECTED", out)
        self.assertEqual(HookHandler.received, [])

    def test_webhook_env_var_is_used(self):
        self.run_cli(["--config", self.config, "--state", self.state, "baseline"])
        TosHandler.content = PAGE_V2
        os.environ["TOS_CHANGE_WEBHOOK_URL"] = self.webhook
        try:
            rc, out = self.run_cli(["--config", self.config, "--state", self.state, "watch"])
        finally:
            del os.environ["TOS_CHANGE_WEBHOOK_URL"]
        self.assertEqual(rc, 0)
        self.assertEqual(len(HookHandler.received), 1)


if __name__ == "__main__":
    unittest.main()
