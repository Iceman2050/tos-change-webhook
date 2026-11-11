# tos-change-webhook

Your vendors change their Terms of Service whenever they want — and you find out when a customer complains, a regulator knocks, or a "we may now share your data with third parties" clause has already been live for six months. Nobody re-reads the ToS every week.

**tos-change-webhook** watches the pages for you. Give it a list of vendor Terms / privacy-policy URLs; it fingerprints the visible text, and the moment the wording changes it prints a unified diff **and** fires a JSON webhook so your pipeline (Slack, PagerDuty, SIEM, a cron that yells) knows within minutes.

## 30-second quickstart

```bash
# 1. List the pages you care about
echo '{"AcmeCloud Terms": "https://acme.example.com/terms"}' > vendors.json

# 2. Record the current wording (no alerts on first run)
python tos_change_webhook.py baseline

# 3. Check for changes (run this from cron; alerts + webhook on change)
python tos_change_webhook.py watch --webhook https://your-endpoint.example.com/hook
```

The webhook URL can also live in the config (`{"webhook_url": "...", "vendors": {...}}`) or in the `TOS_CHANGE_WEBHOOK_URL` environment variable.

Other commands:

```bash
python tos_change_webhook.py diff "AcmeCloud Terms"   # replay the last recorded change
python tos_change_webhook.py watch --state /var/lib/tos/state.json --config /etc/tos/vendors.json
```

## Example output

Real run below — a demo vendor page ("AcmeCloud Terms") served locally, baselined, then edited to add one sentence. Captured verbatim from the tool (local demo-server ports):

```
$ python tos_change_webhook.py baseline
baseline: AcmeCloud Terms                recorded (197 chars, sha256 52ecb765be0e...)

$ python tos_change_webhook.py watch --webhook http://127.0.0.1:40225/hook
watch: AcmeCloud Terms                *** CHANGE DETECTED ***
--- AcmeCloud Terms (previous)
+++ AcmeCloud Terms (current)
@@ -1 +1 @@
-Terms of Service - AcmeCloud Terms of Service Last updated: January 2026 We respect your privacy. We will never sell your personal data. Paid plans may be cancelled at any time with 30 days notice.
+Terms of Service - AcmeCloud Terms of Service Last updated: March 2026 We respect your privacy. We will never sell your personal data. Paid plans may be cancelled at any time with 30 days notice. We may share aggregated usage data with third-party advertisers.
watch: webhook POSTed to http://127.0.0.1:40225/hook (HTTP 200)
watch: 1 change(s), 0 error(s)

Webhook payload received by the endpoint:
{
  "vendor": "AcmeCloud Terms",
  "url": "http://127.0.0.1:44731/terms",
  "detected_at": "2026-10-17T20:26:03+00:00",
  "diff_summary": "+1/-1 lines",
  "diff_excerpt": "--- AcmeCloud Terms (previous)\n+++ AcmeCloud Terms (current)\n@@ -1 +1 @@\n-Terms of Service - AcmeCloud Terms of Service Last updated: January 2026 We respect your privacy. We will never sell your personal data. Paid plans may be cancelled at any time with 30 days notice.\n+Terms of Service - AcmeCloud Terms of Service Last updated: March 2026 We respect your privacy. We will never sell your personal data. Paid plans may be cancelled at any time with 30 days notice. We may share aggregated usage data with third-party advertisers."
}
```

A second `watch` with no changes prints `no change` and sends nothing — silence is the steady state.

## How change detection works (the honest version)

1. **Fetch** the page with `urllib` (30s timeout, honest User-Agent).
2. **Extract** the main text with a deliberately simple heuristic:
   - delete `<script>`, `<style>`, `<nav>`, `<footer>`, `<header>`, `<aside>`, `<noscript>` blocks entirely,
   - delete HTML comments,
   - strip all remaining tags,
   - unescape common entities,
   - collapse all whitespace to single spaces.
3. **Hash** the normalized text with SHA-256 and compare against the local state file.
4. On mismatch: print a `difflib` unified diff, POST the webhook payload, and store the old text so `diff <name>` can replay it later.

## What it does NOT do

- **No JavaScript rendering.** Pages that build their policy text client-side will fingerprint to boilerplate and may produce noisy diffs. If your vendor does this, this tool is not the right watcher.
- **Not a legal service.** It tells you *that* the wording changed and *what* changed — it does not interpret the legal meaning.
- **No polling scheduler built in.** `watch` is one pass; run it from cron/systemd on whatever cadence you want.
- **Regex-based extraction** can mis-handle unusual markup. It is deterministic and documented above — inspect it before trusting it on a page that matters.
- **No retry/backoff sophistication.** A failed fetch is logged to stderr and counted; the run continues with the other vendors.

## Requirements & tests

- Python 3.8+ — **standard library only** (`urllib`, `hashlib`, `difflib`, `json`, `argparse`). No installs.
- Run the suite: `python -m unittest discover -s tests -v` (5 tests, all green — they spin up local HTTP servers for a fake ToS page and a capturing webhook endpoint; no internet needed).

## License

MIT — see [LICENSE](LICENSE).

## Built by Ingenious AI

Part of the [Ingenious AI](https://github.com/Iceman2050) security platform suite. Get early access:

- [DisclosureGuard](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20DisclosureGuard.)
- [ModelMinder](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20ModelMinder.)
- [TermsWatch](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20TermsWatch.)
- [Vibe-Code Audit](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Vibe-Code%20Audit.)
- [Rogue-Agent Defense](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Rogue-Agent%20Defense.)
- [BreachHunter](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20BreachHunter.)
- [ChatBreaker](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20ChatBreaker.)
- [Agent Flight Recorder](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Agent%20Flight%20Recorder.)
- [Agent Auth Gateway](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Agent%20Auth%20Gateway.)
- [DomainGuard](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20DomainGuard.)
- [ContentCraft AI](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20ContentCraft%20AI.)

