#!/usr/bin/env python3
"""Tests for the deterministic AI breaking-news snapshot collector."""

import difflib
import hashlib
import importlib.util
import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "ai-breaking-news-snapshot.py"
CONFIG = pathlib.Path(__file__).parents[1] / "monitoring" / "major-ai-sources.json"


def load_module():
    spec = importlib.util.spec_from_file_location("ai_breaking_news_snapshot", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParsingTests(unittest.TestCase):
    def test_parse_now_rejects_timezone_less_input(self):
        module = load_module()
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            module.parse_now("2026-09-01T12:00:00")

    def test_parse_now_normalizes_equivalent_offsets_to_utc(self):
        module = load_module()
        utc = module.parse_now("2026-09-01T12:00:00Z")
        offset = module.parse_now("2026-09-01T08:00:00-04:00")
        self.assertEqual(offset, utc)
        self.assertEqual(offset.isoformat(), "2026-09-01T12:00:00+00:00")

    def test_canonicalizes_tracking_url(self):
        module = load_module()
        url = "HTTPS://Example.COM:443/a/../news/?utm_source=x&b=2&fbclid=z&a=1#top"
        self.assertEqual(
            module.canonicalize_url(url),
            "https://example.com/news?a=1&b=2",
        )

    def test_parses_rss_item(self):
        module = load_module()
        xml = b"""<?xml version='1.0'?>
        <rss version='2.0'><channel><item>
          <title>  A &amp; B   launch  </title>
          <link>https://example.com/posts/launch</link>
          <pubDate>Tue, 01 Sep 2026 12:00:00 +0000</pubDate>
          <description><![CDATA[<p>Big   news.</p>]]></description>
        </item></channel></rss>"""
        items = module.parse_document(
            xml,
            {"name": "Example", "type": "rss", "primary": True},
        )
        self.assertEqual(items, [{
            "id": "https://example.com/posts/launch",
            "source": "Example",
            "title": "A & B launch",
            "url": "https://example.com/posts/launch",
            "published": "2026-09-01T12:00:00Z",
            "summary": "Big news.",
            "primary": True,
        }])

    def test_parses_namespaced_rss_1_rdf_item(self):
        module = load_module()
        xml = b"""<?xml version='1.0'?>
        <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
                 xmlns='http://purl.org/rss/1.0/'
                 xmlns:dc='http://purl.org/dc/elements/1.1/'>
          <channel rdf:about='https://example.com/feed'><title>Example</title>
            <link>https://example.com/</link><description>News</description>
          </channel>
          <item rdf:about='https://example.com/posts/rdf-release'>
            <title>RDF Release</title>
            <link>https://example.com/posts/rdf-release</link>
            <dc:date>2026-09-01T12:15:00Z</dc:date>
            <description><![CDATA[<p>Namespaced news.</p>]]></description>
          </item>
        </rdf:RDF>"""
        self.assertEqual(module.parse_document(
            xml, {"name": "RDF Feed", "type": "rss", "primary": False}
        ), [{
            "id": "https://example.com/posts/rdf-release",
            "source": "RDF Feed",
            "title": "RDF Release",
            "url": "https://example.com/posts/rdf-release",
            "published": "2026-09-01T12:15:00Z",
            "summary": "Namespaced news.",
            "primary": False,
        }])

    def test_parses_atom_entry_with_alternate_link(self):
        module = load_module()
        xml = b"""<feed xmlns='http://www.w3.org/2005/Atom'>
          <entry><id>tag:example,2026:one</id><title>Release One</title>
          <link rel='self' href='https://example.com/feed/one'/>
          <link rel='alternate' href='https://example.com/releases/one'/>
          <updated>2026-09-01T12:30:00+00:00</updated>
          <summary type='html'>&lt;b&gt;Ready&lt;/b&gt; now</summary></entry>
        </feed>"""
        items = module.parse_document(
            xml,
            {"name": "Releases", "type": "rss", "primary": True},
        )
        self.assertEqual(items[0], {
            "id": "tag:example,2026:one",
            "source": "Releases",
            "title": "Release One",
            "url": "https://example.com/releases/one",
            "published": "2026-09-01T12:30:00Z",
            "summary": "Ready now",
            "primary": True,
        })

    def test_parses_and_filters_sitemap_urls(self):
        module = load_module()
        xml = b"""<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>https://example.com/news/new-model</loc><lastmod>2026-09-01</lastmod></url>
          <url><loc>https://example.com/legal/terms</loc><lastmod>2026-09-01</lastmod></url>
        </urlset>"""
        items = module.parse_document(xml, {
            "name": "Example", "type": "sitemap", "primary": True,
            "path_contains": "/news/",
        })
        self.assertEqual(items, [{
            "id": "https://example.com/news/new-model",
            "source": "Example",
            "title": "New Model",
            "url": "https://example.com/news/new-model",
            "published": "2026-09-01T00:00:00Z",
            "summary": "",
            "primary": True,
        }])

    def test_rss_guid_is_preferred_as_stable_id(self):
        module = load_module()
        xml = b"""<rss><channel><item><guid>release-123</guid><title>Release</title>
          <link>https://example.com/release</link><pubDate>Tue, 01 Sep 2026 11:00:00 +0000</pubDate>
        </item></channel></rss>"""
        item = module.parse_document(xml, {
            "name": "Example", "type": "rss", "primary": True,
        })[0]
        self.assertEqual(item["id"], "release-123")
        self.assertEqual(item["source"], "Example")

    def test_unsafe_item_urls_are_omitted_even_with_guids_and_boundary_is_allowed(self):
        module = load_module()
        unsafe = [
            "javascript:alert(1)", "data:text/plain,no", "file:///tmp/no",
            "/relative", "https://user:user@example.com/no",
            "https://example.com:bad/no",
            "https://example.com/" + "x" * module.MAX_ITEM_URL_CHARS,
        ]
        prefix = "https://example.com/"
        boundary = prefix + "b" * (module.MAX_ITEM_URL_CHARS - len(prefix))
        entries = "".join(
            f"<item><guid>unsafe-{n}</guid><title>Unsafe {n}</title>"
            f"<link>{url}</link></item>" for n, url in enumerate(unsafe)
        )
        entries += f"<item><guid>valid</guid><title>Valid</title><link>{boundary}</link></item>"
        items = module.parse_document(
            f"<rss><channel>{entries}</channel></rss>".encode(),
            {"name": "Example", "type": "rss", "primary": True},
        )
        self.assertEqual([item["id"] for item in items], ["valid"])
        self.assertEqual(items[0]["url"], boundary)


class SnapshotTests(unittest.TestCase):
    RSS = """<rss><channel><item><title>One</title>
      <link>https://example.com/one?utm_source=test</link>
      <pubDate>Tue, 01 Sep 2026 11:00:00 +0000</pubDate>
      <description>Summary</description></item></channel></rss>"""

    def run_cli(self, root, fixture="feed.xml", max_entries=10):
        config = root / "config.json"
        state = root / "state"
        fixtures = root / "fixtures"
        fixtures.mkdir(exist_ok=True)
        config.write_text(json.dumps({
            "schema_version": 1,
            "lookback_hours": 48,
            "max_total": 20,
            "max_output_bytes": 7500,
            "max_summary_chars": 200,
            "sources": [{
                "name": "Example", "url": "https://example.com/feed.xml",
                "type": "rss", "primary": True, "max_entries": max_entries,
                "fixture": fixture,
            }],
        }))
        return subprocess.run([
            sys.executable, str(SCRIPT), "--config", str(config),
            "--state-dir", str(state), "--fixture-dir", str(fixtures),
            "--now", "2026-09-01T12:00:00Z",
        ], check=True, capture_output=True)

    def test_repeated_runs_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "fixtures").mkdir()
            (root / "fixtures" / "feed.xml").write_text(self.RSS)
            first = self.run_cli(root)
            second = self.run_cli(root)
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual(first.stderr, b"")
            self.assertEqual(json.loads(first.stdout), {
                "items": [{
                    "id": "https://example.com/one",
                    "source": "Example",
                    "primary": True,
                    "published": "2026-09-01T11:00:00Z",
                    "summary": "Summary",
                    "title": "One",
                    "url": "https://example.com/one",
                }]
            })

    def test_changed_fixture_with_persistent_state_changes_cli_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            feed = fixtures / "feed.xml"
            feed.write_text(self.RSS)
            first = self.run_cli(root).stdout
            feed.write_text(self.RSS.replace("One", "Corrected One").replace(
                "11:00:00", "11:01:00"))
            second = self.run_cli(root).stdout
            self.assertNotEqual(hashlib.sha256(first).digest(), hashlib.sha256(second).digest())
            self.assertEqual(json.loads(second)["items"][0]["title"], "Corrected One")

    def test_corrected_title_same_url_is_one_updated_item_across_cli_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            feed = fixtures / "feed.xml"
            feed.write_text(self.RSS)
            self.run_cli(root)
            feed.write_text("""<rss><channel>
              <item><guid>new-guid</guid><title>Corrected One</title><link>https://example.com/one</link>
              <pubDate>Tue, 01 Sep 2026 11:05:00 +0000</pubDate></item>
              <item><guid>old-guid</guid><title>One</title><link>https://example.com/one</link>
              <pubDate>Tue, 01 Sep 2026 11:00:00 +0000</pubDate></item>
            </channel></rss>""")
            changed = self.run_cli(root).stdout
            items = json.loads(changed)["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual((items[0]["id"], items[0]["title"]),
                             ("new-guid", "Corrected One"))
            self.assertEqual(changed, self.run_cli(root).stdout)

    def test_feed_reordering_does_not_change_bounded_output(self):
        older = """<item><title>Older</title><link>https://example.com/older</link>
          <pubDate>Tue, 01 Sep 2026 10:00:00 +0000</pubDate></item>"""
        newer = """<item><title>Newer</title><link>https://example.com/newer</link>
          <pubDate>Tue, 01 Sep 2026 11:00:00 +0000</pubDate></item>"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            feed = fixtures / "feed.xml"
            feed.write_text(f"<rss><channel>{older}{newer}</channel></rss>")
            first = self.run_cli(root, max_entries=1).stdout
            feed.write_text(f"<rss><channel>{newer}{older}</channel></rss>")
            second = self.run_cli(root, max_entries=1).stdout
            self.assertEqual(first, second)
            self.assertEqual(json.loads(first)["items"][0]["title"], "Newer")

    def test_source_failure_retains_last_good_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            feed = fixtures / "feed.xml"
            feed.write_text(self.RSS)
            good = self.run_cli(root)
            feed.unlink()
            failed = self.run_cli(root)
            self.assertEqual(failed.stdout, good.stdout)
            self.assertEqual(failed.stderr, b"Example: fetch failed; using cached entries\n")

    def test_corrected_title_same_url_updates_instead_of_duplicate(self):
        module = load_module()
        old = {
            "id": "https://example.com/post", "url": "https://example.com/post",
            "title": "Modle released", "published": "2026-09-01T10:00:00Z",
            "summary": "old", "primary": True,
        }
        corrected = dict(old, title="Model released", summary="corrected",
                         published="2026-09-01T11:00:00Z")
        self.assertEqual(module.merge_items([corrected, old]), [corrected])

    def test_same_source_stable_id_deduplicates_changed_urls(self):
        module = load_module()
        old = {"source": "Example", "id": "stable-id", "title": "Old",
               "url": "https://example.com/old",
               "published": "2026-09-01T10:00:00Z", "summary": "",
               "primary": True}
        new = dict(old, title="New", url="https://example.com/new",
                   published="2026-09-01T11:00:00Z")
        self.assertEqual(module.merge_items([old, new]), [new])

    def test_snapshot_projects_items_to_exact_public_schema(self):
        module = load_module()
        item = {
            "source": "Example", "id": "one", "title": "One",
            "url": "https://example.com/one",
            "published": "2026-09-01T11:00:00Z", "summary": "Summary",
            "primary": True, "unexpected": "must not leak",
        }
        snapshot = module.build_snapshot([item], {
            "max_total": 10, "max_output_bytes": 1000,
            "max_summary_chars": 200, "max_title_chars": 180,
            "max_id_chars": 96,
        })
        self.assertEqual(set(snapshot["items"][0]), {
            "source", "id", "title", "url", "published", "summary", "primary",
        })

    def test_snapshot_omits_noncanonical_or_oversized_item_urls(self):
        module = load_module()
        base = {"source": "Example", "id": "one", "title": "One",
                "published": "2026-09-01T11:00:00Z", "summary": "",
                "primary": True}
        items = [dict(base, id=str(n), url=url) for n, url in enumerate((
            "javascript:alert(1)", "/relative", "https://user@example.com/no",
            "https://example.com/" + "x" * module.MAX_ITEM_URL_CHARS,
        ))]
        items.append(dict(base, id="good", url="http://example.com/good"))
        snapshot = module.build_snapshot(items, {
            "max_total": 10, "max_output_bytes": 2000,
            "max_item_url_chars": module.MAX_ITEM_URL_CHARS,
        })
        self.assertEqual([item["id"] for item in snapshot["items"]], ["good"])

    def test_shared_url_preserves_newest_representative_from_each_source(self):
        module = load_module()
        shared = "https://example.com/shared"
        items = [
            {"source": "Alpha", "id": "old", "title": "Old Alpha", "url": shared,
             "published": "2026-09-01T10:00:00Z", "summary": "", "primary": True},
            {"source": "Alpha", "id": "new", "title": "Corrected Alpha", "url": shared,
             "published": "2026-09-01T12:00:00Z", "summary": "", "primary": True},
            {"source": "Beta", "id": "beta", "title": "Beta coverage", "url": shared,
             "published": "2026-09-01T11:00:00Z", "summary": "", "primary": False},
        ]
        snapshot = module.build_snapshot(items, {
            "max_total": 10, "max_output_bytes": 2000,
            "max_summary_chars": 200, "max_title_chars": 180,
            "max_id_chars": 96,
        })
        self.assertEqual({item["source"] for item in snapshot["items"]},
                         {"Alpha", "Beta"})
        self.assertEqual([item["title"] for item in snapshot["items"]
                          if item["source"] == "Alpha"], ["Corrected Alpha"])

    def test_age_retention_is_deterministic_at_boundary(self):
        module = load_module()
        def item(url, published):
            return {"id": url, "url": url, "title": url, "published": published,
                    "summary": "", "primary": True}
        boundary = item("boundary", "2026-08-30T12:00:00Z")
        expired = item("expired", "2026-08-30T11:59:59Z")
        undated = item("undated", "")
        kept = module.retain_recent(
            [expired, undated, boundary], module.parse_now("2026-09-01T12:00:00Z"), 48
        )
        self.assertEqual(kept, [boundary])

    def test_real_new_item_changes_output(self):
        module = load_module()
        first = {"items": [{"id": "one"}]}
        with_new_item = {"items": [{"id": "two"}, {"id": "one"}]}
        self.assertNotEqual(module.snapshot_bytes(first), module.snapshot_bytes(with_new_item))

    def test_huge_newest_item_remains_mandatory_and_diff_visible(self):
        module = load_module()
        def item(source, marker, minute, **changes):
            value = {
                "id": f"{source}-{marker}", "source": source,
                "url": f"https://{source.lower()}.example/{marker}",
                "title": f"{source} {marker}",
                "published": f"2026-09-01T12:{minute:02d}:00Z",
                "summary": "summary", "primary": True,
            }
            value.update(changes)
            return value
        alpha_old = item("Alpha", "old", 1)
        alpha_new = item("Alpha", "new", 9, title="NEWEST " + "T" * 6000,
                         id="native-" + "I" * 6000, summary="S" * 6000)
        beta = item("Beta", "new", 8)
        gamma = item("Gamma", "new", 7)
        config = {"max_total": 3, "max_output_bytes": 900,
                  "max_summary_chars": 40, "max_title_chars": 80,
                  "max_id_chars": 96}
        before = module.snapshot_bytes(module.build_snapshot(
            [alpha_old, beta, gamma], config
        )).decode()
        after_snapshot = module.build_snapshot(
            [alpha_old, alpha_new, beta, gamma], config
        )
        after = module.snapshot_bytes(after_snapshot)
        parsed = json.loads(after)
        alpha_items = [entry for entry in parsed["items"] if entry["source"] == "Alpha"]
        self.assertEqual(len(alpha_items), 1)
        self.assertEqual(alpha_items[0]["url"], alpha_new["url"])
        self.assertTrue(alpha_items[0]["id"].startswith("sha256:"))
        self.assertEqual(len(alpha_items[0]["title"]), 80)
        self.assertEqual({entry["source"] for entry in parsed["items"]},
                         {"Alpha", "Beta", "Gamma"})
        self.assertLessEqual(len(after), config["max_output_bytes"])
        self.assertEqual(alpha_new["title"], "NEWEST " + "T" * 6000)
        diff = "".join(difflib.unified_diff(
            before.splitlines(True), after.decode().splitlines(True)
        ))
        self.assertIn(alpha_new["url"], diff[:4000])

    def test_oversized_item_url_is_rejected_before_output_budgeting(self):
        module = load_module()
        item = {"source": "Alpha", "id": "one", "title": "One",
                "url": "https://example.com/" + "x" * 2000,
                "published": "2026-09-01T12:00:00Z", "summary": "",
                "primary": True}
        snapshot = module.build_snapshot([item], {
            "max_total": 10, "max_output_bytes": 300,
            "max_summary_chars": 10, "max_title_chars": 80,
            "max_id_chars": 96,
        })
        self.assertEqual(snapshot, {"items": []})

    def test_output_budget_is_source_fair_and_caps_summaries(self):
        module = load_module()
        items = []
        for source_no, source in enumerate(("Alpha", "Beta", "Gamma")):
            for item_no in range(4):
                items.append({
                    "id": f"{source}-{item_no}", "source": source,
                    "url": f"https://{source.lower()}.example/{item_no}",
                    "title": f"{source} item {item_no}",
                    "published": f"2026-09-01T{12-source_no:02d}:0{item_no}:00Z",
                    "summary": "é" * 300, "primary": source != "Gamma",
                })
        config = {"max_total": 20, "max_output_bytes": 900, "max_summary_chars": 30}
        snapshot = module.build_snapshot(items, config)
        encoded = module.snapshot_bytes(snapshot)
        self.assertLessEqual(len(encoded), 900)
        self.assertEqual({item["source"] for item in snapshot["items"]},
                         {"Alpha", "Beta", "Gamma"})
        self.assertTrue(all(len(item["summary"]) <= 30 for item in snapshot["items"]))

    def test_serialization_has_one_item_per_line_and_focused_diff(self):
        module = load_module()
        def item(number, title=None):
            return {"id": str(number), "source": "Example", "primary": True,
                    "published": f"2026-09-01T11:{number:02d}:00Z", "summary": "summary",
                    "title": title or f"Item {number}",
                    "url": f"https://example.com/{number}"}
        before = module.snapshot_bytes({"items": [item(n) for n in range(34, -1, -1)]}).decode()
        after = module.snapshot_bytes({"items": [item(34, "Corrected important title")] +
                                                [item(n) for n in range(33, -1, -1)]}).decode()
        item_lines = [line for line in after.splitlines() if line.startswith('{"id"')]
        self.assertEqual(len(item_lines), 35)
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True)))
        boundary = diff[:4000]
        self.assertLess(len(diff), 4000)
        self.assertIn("Corrected important title", boundary)
        self.assertIn("https://example.com/34", boundary)

    def test_all_configured_sources_fit_as_representatives(self):
        module = load_module()
        config = json.loads(CONFIG.read_text())
        items = [{
            "id": source["name"], "source": source["name"],
            "url": f"https://example.com/{number}", "title": f"Latest from {source['name']}",
            "published": f"2026-09-01T{number:02d}:00:00Z",
            "summary": "A representative summary. " * 20, "primary": source["primary"],
        } for number, source in enumerate(config["sources"])]
        snapshot = module.build_snapshot(items, config)
        self.assertLessEqual(len(module.snapshot_bytes(snapshot)), config["max_output_bytes"])
        self.assertEqual({item["source"] for item in snapshot["items"]},
                         {source["name"] for source in config["sources"]})


class ConfigValidationTests(unittest.TestCase):
    def test_hard_deadline_must_be_finite_and_positive(self):
        module = load_module()
        base = {"schema_version": 1, "sources": [{"name": "One",
                "url": "https://one.example/feed", "type": "rss",
                "primary": True, "max_entries": 3}]}
        for value in (True, 0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.validate_config(dict(base, hard_deadline_seconds=value))

    def test_source_urls_must_be_https_without_credentials_or_unsafe_literal_hosts(self):
        module = load_module()
        base = {"schema_version": 1, "sources": [{"name": "One",
                "url": "https://one.example/feed", "type": "rss",
                "primary": True, "max_entries": 3}]}
        for url in ("http://one.example/feed", "https://user@example.com/feed",
                    "https://HOST_PLACEHOLDER/feed", "https://169.254.169.254/feed"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                module.validate_config(dict(
                    base, sources=[dict(base["sources"][0], url=url)]
                ))

    def test_integer_config_fields_reject_fractional_nonfinite_and_bool_before_fetch(self):
        module = load_module()
        base = {"schema_version": 1, "lookback_hours": 48, "max_total": 20,
                "max_output_bytes": 7500, "max_summary_chars": 200,
                "max_title_chars": 180, "max_id_chars": 96,
                "max_source_name_chars": 80, "fetch_workers": 2,
                "fetch_timeout_seconds": 1.5,
                "sources": [{"name": "One", "url": "https://one.example/feed",
                             "type": "rss", "primary": True, "max_entries": 3}]}
        integer_fields = ("lookback_hours", "max_total", "max_output_bytes",
                          "max_summary_chars", "max_title_chars", "max_id_chars",
                          "max_source_name_chars", "fetch_workers")
        for key in integer_fields:
            for invalid in (0.5, float("nan"), float("inf"), True):
                with self.subTest(key=key, invalid=invalid):
                    called = []
                    config = dict(base, **{key: invalid})
                    with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                        module.collect_sources(config, pathlib.Path(tmp),
                                               fetcher=lambda *args: called.append(True))
                    self.assertEqual(called, [])
        for invalid in (0.5, float("nan"), float("inf"), True, 0, -1):
            with self.subTest(key="max_entries", invalid=invalid):
                called = []
                source = dict(base["sources"][0], max_entries=invalid)
                with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                    module.collect_sources(dict(base, sources=[source]), pathlib.Path(tmp),
                                           fetcher=lambda *args: called.append(True))
                self.assertEqual(called, [])

    def test_timeout_rejects_nonfinite_bool_and_nonpositive_before_fetch(self):
        module = load_module()
        base = {"schema_version": 1,
                "sources": [{"name": "One", "url": "https://one.example/feed",
                             "type": "rss", "primary": True, "max_entries": 3}]}
        for invalid in (float("nan"), float("inf"), True, 0, -1):
            with self.subTest(invalid=invalid):
                called = []
                with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                    module.collect_sources(dict(base, fetch_timeout_seconds=invalid),
                                           pathlib.Path(tmp),
                                           fetcher=lambda *args: called.append(True))
                self.assertEqual(called, [])

    def test_invalid_source_contracts_fail_before_fetch(self):
        module = load_module()
        base = {"schema_version": 1, "lookback_hours": 48, "max_total": 20,
                "max_output_bytes": 7500, "max_summary_chars": 200,
                "sources": [{"name": "One", "url": "https://one.example/feed",
                             "type": "rss", "primary": True, "max_entries": 3}]}
        invalid = [
            dict(base, schema_version=2),
            dict(base, max_output_bytes=0),
            dict(base, max_title_chars=0),
            dict(base, max_id_chars=70),
            dict(base, max_source_name_chars=2),
            dict(base, sources=[dict(base["sources"][0], type="json")]),
            dict(base, sources=[dict(base["sources"][0], url="file:///tmp/feed")]),
            dict(base, sources=[dict(base["sources"][0], primary="yes")]),
            dict(base, sources=[dict(base["sources"][0], max_entries=0)]),
            dict(base, sources=[base["sources"][0], dict(base["sources"][0])]),
            dict(base, sources=[{"name": "Map", "url": "https://one.example/map",
                                 "type": "sitemap", "primary": True, "max_entries": 2}]),
        ]
        for config in invalid:
            with self.subTest(config=config):
                called = []
                with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                    module.collect_sources(config, pathlib.Path(tmp),
                                           fetcher=lambda *args: called.append(True))
                self.assertEqual(called, [])


class ConditionalFetchTests(unittest.TestCase):
    def test_hard_deadline_exits_blocked_process_with_deterministic_stderr(self):
        code = f"""import importlib.util, time
spec = importlib.util.spec_from_file_location('monitor', {str(SCRIPT)!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
with m.hard_process_deadline(0.15): time.sleep(10)
"""
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                timeout=3)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"hard deadline exceeded\n")
        self.assertLess(elapsed, 2)

    def test_normal_completion_disarms_hard_deadline(self):
        code = f"""import importlib.util, time
spec = importlib.util.spec_from_file_location('monitor', {str(SCRIPT)!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
with m.hard_process_deadline(0.15): time.sleep(0.01)
time.sleep(0.25)
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                timeout=3)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_atomic_writers_use_complete_unique_temporary_files(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "cache.json"
            values = [{"writer": n, "payload": str(n) * 2000} for n in range(40)]
            errors = []

            def write(value):
                try:
                    module.atomic_write_json(path, value)
                except Exception as error:
                    errors.append(error)

            threads = [threading.Thread(target=write, args=(value,)) for value in values]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertIn(json.loads(path.read_text()), values)
            self.assertEqual(list(path.parent.glob("cache.json.tmp*")), [])

    def test_redirect_policy_allows_same_host_https(self):
        module = load_module()
        policy = module.SafeSourceRedirectHandler("https://Example.com/feed")
        request = urllib.request.Request("https://example.com/feed")
        redirected = policy.redirect_request(
            request, None, 302, "Found", {}, "https://EXAMPLE.com/canonical"
        )
        self.assertEqual(redirected.full_url, "https://EXAMPLE.com/canonical")

    def test_redirect_policy_rejects_cross_host_http_and_unsafe_literal_ips(self):
        module = load_module()
        policy = module.SafeSourceRedirectHandler("https://example.com/feed")
        request = urllib.request.Request("https://example.com/feed")
        rejected = (
            "https://other.example/feed",
            "http://example.com/feed",
            "https://HOST_PLACEHOLDER/feed",
            "https://169.254.169.254/latest/meta-data",
        )
        for target in rejected:
            with self.subTest(target=target), self.assertRaises(urllib.error.HTTPError):
                policy.redirect_request(request, None, 302, "Found", {}, target)

    def test_configured_item_url_limit_is_shared_by_parser_and_cache(self):
        module = load_module()
        source = {"name": "Example", "url": "https://example.com/feed",
                  "type": "rss", "primary": True, "max_entries": 3}
        article = "https://example.com/" + "x" * 30
        xml = (f"<rss><channel><item><guid>one</guid><title>One</title>"
               f"<link>{article}</link></item></channel></rss>").encode()
        with self.assertRaisesRegex(ValueError, "no usable"):
            module.parse_document(xml, source, max_item_url_chars=40)
        item = {"source": "Example", "id": "one", "title": "One",
                "url": article, "published": "", "summary": "", "primary": True}
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "cache.json"
            module.atomic_write_json(path, {"items": [item]})
            self.assertEqual(module.load_cache(path, source, max_item_url_chars=40), {})

    def test_cache_items_require_exact_schema_and_source_attribution(self):
        module = load_module()
        source = {"name": "Example", "url": "https://example.com/feed",
                  "type": "rss", "primary": True, "max_entries": 3}
        valid = {"source": "Example", "id": "one", "title": "One",
                 "url": "https://example.com/one",
                 "published": "2026-09-01T11:00:00Z", "summary": "",
                 "primary": True}
        invalid_items = [
            [],
            [dict(valid, unexpected="no")],
            [dict(valid, source="Other")],
            [dict(valid, primary=False)],
            [dict(valid, url="HTTPS://EXAMPLE.COM:443/one/")],
            [dict(valid, id="")],
            [dict(valid, title="")],
            [dict(valid, source="")],
            [{key: value for key, value in valid.items() if key != "summary"}],
            [dict(valid, url="javascript:alert(1)")],
            [dict(valid, url="data:text/plain,no")],
            [dict(valid, url="file:///tmp/no")],
            [dict(valid, url="/relative")],
            [dict(valid, url="https://user:user@example.com/no")],
            [dict(valid, url="https://example.com/" + "x" * module.MAX_ITEM_URL_CHARS)],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            cached = module.cache_path(state, source)
            for items in invalid_items:
                with self.subTest(items=items):
                    module.atomic_write_json(cached, {"items": items})
                    self.assertEqual(module.load_cache(cached, source), {})

    def test_each_invalid_cache_contract_is_replaced_by_fresh_fetch(self):
        module = load_module()
        source = {"name": "Example", "url": "https://example.com/feed",
                  "type": "rss", "primary": True, "max_entries": 3}
        valid = {"source": "Example", "id": "one", "title": "One",
                 "url": "https://example.com/one",
                 "published": "2026-09-01T11:00:00Z", "summary": "",
                 "primary": True}
        invalid_items = [
            [], [dict(valid, unexpected="no")], [dict(valid, source="Other")],
            [dict(valid, primary=False)],
            [dict(valid, url="HTTPS://EXAMPLE.COM:443/one/")],
            [dict(valid, id="")], [dict(valid, title="")],
        ]
        xml = b"<rss><channel><item><title>Fresh</title><link>https://example.com/fresh</link><pubDate>Tue, 01 Sep 2026 11:00:00 +0000</pubDate></item></channel></rss>"
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size=-1): return xml
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            cached = module.cache_path(state, source)
            for items in invalid_items:
                with self.subTest(items=items):
                    module.atomic_write_json(cached, {"etag": '"stale"', "items": items})
                    fresh = module.fetch_live_source(
                        source, state, 2, lambda request, timeout: Response()
                    )
                    self.assertEqual(fresh[0]["title"], "Fresh")
                    self.assertEqual(module.load_cache(cached, source)["items"], fresh)

    def test_sources_are_fetched_concurrently(self):
        module = load_module()
        barrier = threading.Barrier(2)
        threads = set()
        def fetcher(source, state_dir, timeout):
            threads.add(threading.get_ident())
            barrier.wait(timeout=2)
            return [{"id": source["name"]}]
        config = {"schema_version": 1, "fetch_workers": 2, "fetch_timeout_seconds": 1,
                  "sources": [
            {"name": "one", "url": "https://one.example/feed", "type": "rss",
             "primary": True, "max_entries": 2},
            {"name": "two", "url": "https://two.example/feed", "type": "rss",
             "primary": True, "max_entries": 2},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            batches = module.collect_sources(config, pathlib.Path(tmp), None, fetcher)
        self.assertEqual(len(threads), 2)
        self.assertEqual([item["id"] for batch in batches for item in batch], ["one", "two"])

    def test_etag_is_persisted_and_304_reuses_cache(self):
        module = load_module()
        source = {"name": "Example", "url": "https://example.com/feed",
                  "type": "rss", "primary": True, "max_entries": 3}
        xml = b"""<rss><channel><item><title>One</title><link>https://example.com/one</link>
          <pubDate>Tue, 01 Sep 2026 11:00:00 +0000</pubDate></item></channel></rss>"""

        class Response:
            headers = {"ETag": '"v1"', "Last-Modified": "Tue, 01 Sep 2026 11:00:00 GMT"}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size=-1): return xml[:size] if size >= 0 else xml

        requests = []
        def first_open(request, timeout):
            requests.append(request)
            return Response()
        def not_modified(request, timeout):
            requests.append(request)
            raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, None)

        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            fresh = module.fetch_live_source(source, state, 2, first_open)
            cached = module.fetch_live_source(source, state, 2, not_modified)
            self.assertEqual(cached, fresh)
            self.assertEqual(requests[1].get_header("If-none-match"), '"v1"')
            self.assertEqual(requests[1].get_header("If-modified-since"),
                             "Tue, 01 Sep 2026 11:00:00 GMT")

    def test_empty_http_200_does_not_replace_last_good_cache(self):
        module = load_module()
        source = {"name": "Example", "url": "https://example.com/feed",
                  "type": "rss", "primary": True, "max_entries": 3}
        good_xml = b"<rss><channel><item><title>One</title><link>https://example.com/one</link><pubDate>Tue, 01 Sep 2026 11:00:00 +0000</pubDate></item></channel></rss>"
        class Response:
            headers = {}
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size=-1): return self.data
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            good = module.fetch_live_source(source, state, 2, lambda request, timeout: Response(good_xml))
            with self.assertRaises(ValueError):
                module.fetch_live_source(source, state, 2,
                                         lambda request, timeout: Response(b"<rss><channel/></rss>"))
            cached = json.loads(module.cache_path(state, source).read_text())["items"]
            self.assertEqual(cached, good)

    def test_wrong_root_does_not_replace_last_good_cache(self):
        module = load_module()
        source = {"name": "Example", "url": "https://example.com/feed",
                  "type": "rss", "primary": True, "max_entries": 3}
        cached = module.cache_path(pathlib.Path(tempfile.mkdtemp()), source)
        old = [{"id": "old", "source": "Example"}]
        module.atomic_write_json(cached, {"items": old})
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size=-1): return b"<html><body>not a feed</body></html>"
        with self.assertRaises(ValueError):
            module.fetch_live_source(source, cached.parent, 2, lambda request, timeout: Response())
        self.assertEqual(json.loads(cached.read_text())["items"], old)

    def test_malformed_port_cache_is_absent_on_fetch_failure(self):
        module = load_module()
        source = {"name": "Example", "url": "https://example.com/feed",
                  "type": "rss", "primary": True, "max_entries": 3}
        invalid = {"source": "Example", "id": "one", "title": "One",
                   "url": "https://example.com:bad/x",
                   "published": "2026-09-01T11:00:00Z", "summary": "",
                   "primary": True}
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            module.atomic_write_json(module.cache_path(state, source), {"items": [invalid]})
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                batches = module.collect_sources(
                    {"schema_version": 1, "sources": [source]}, state,
                    fetcher=lambda *args: (_ for _ in ()).throw(OSError())
                )
        self.assertEqual(batches, [[]])
        self.assertEqual(errors.getvalue(),
                         "Example: fetch failed; no cached entries\n")

    def test_malformed_port_cache_is_replaced_by_fresh_fetch(self):
        module = load_module()
        source = {"name": "Example", "url": "https://example.com/feed",
                  "type": "rss", "primary": True, "max_entries": 3}
        invalid = {"source": "Example", "id": "one", "title": "One",
                   "url": "https://example.com:bad/x",
                   "published": "2026-09-01T11:00:00Z", "summary": "",
                   "primary": True}
        xml = b"<rss><channel><item><title>Fresh</title><link>https://example.com/fresh</link><pubDate>Tue, 01 Sep 2026 11:00:00 +0000</pubDate></item></channel></rss>"
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size=-1): return xml
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            cached = module.cache_path(state, source)
            module.atomic_write_json(cached, {"etag": '"stale"', "items": [invalid]})
            fresh = module.fetch_live_source(
                source, state, 2, lambda request, timeout: Response()
            )
            self.assertEqual(fresh[0]["title"], "Fresh")
            self.assertEqual(module.load_cache(cached, source)["items"], fresh)

    def test_parseable_invalid_cache_is_absent_on_fetch_failure(self):
        module = load_module()
        source = {"name": "Example", "url": "https://example.com/feed",
                  "type": "rss", "primary": True, "max_entries": 3}
        invalid_states = [
            {"items": [1]},
            {"items": [{"id": "missing-required-fields"}]},
            {"items": [{"source": "Example", "id": "one", "title": "One",
                         "url": "https://example.com/one", "published": "not-a-date",
                         "summary": "", "primary": True}]},
            {"items": [{"source": "Example", "id": "one", "title": "One",
                         "url": "https://example.com/one", "published": "",
                         "summary": "", "primary": 1}]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            cached = module.cache_path(state, source)
            for invalid in invalid_states:
                with self.subTest(invalid=invalid):
                    module.atomic_write_json(cached, invalid)
                    errors = io.StringIO()
                    with contextlib.redirect_stderr(errors):
                        batches = module.collect_sources(
                            {"schema_version": 1, "sources": [source]}, state,
                            fetcher=lambda *args: (_ for _ in ()).throw(OSError())
                        )
                    self.assertEqual(batches, [[]])
                    self.assertEqual(errors.getvalue(),
                                     "Example: fetch failed; no cached entries\n")

    def test_parseable_invalid_cache_is_replaced_by_fresh_fetch(self):
        module = load_module()
        source = {"name": "Example", "url": "https://example.com/feed",
                  "type": "rss", "primary": True, "max_entries": 3}
        xml = b"<rss><channel><item><title>Fresh</title><link>https://example.com/fresh</link><pubDate>Tue, 01 Sep 2026 11:00:00 +0000</pubDate></item></channel></rss>"
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size=-1): return xml
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            cached = module.cache_path(state, source)
            module.atomic_write_json(cached, {"etag": '"stale"', "items": [1]})
            fresh = module.fetch_live_source(
                source, state, 2, lambda request, timeout: Response()
            )
            self.assertEqual(fresh[0]["title"], "Fresh")
            self.assertEqual(module.load_cache(cached, source)["items"], fresh)

    def test_corrupt_cache_does_not_block_fresh_fetch_or_fallback(self):
        module = load_module()
        source = {"name": "Example", "url": "https://example.com/feed",
                  "type": "rss", "primary": True, "max_entries": 3}
        xml = b"<rss><channel><item><title>One</title><link>https://example.com/one</link><pubDate>Tue, 01 Sep 2026 11:00:00 +0000</pubDate></item></channel></rss>"
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size=-1): return xml
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            cached = module.cache_path(state, source)
            cached.write_text("{broken")
            fresh = module.fetch_live_source(source, state, 2,
                                             lambda request, timeout: Response())
            self.assertEqual(fresh[0]["title"], "One")
            cached.write_text("{broken")
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                batches = module.collect_sources(
                    {"schema_version": 1, "sources": [source]}, state,
                    fetcher=lambda *args: (_ for _ in ()).throw(OSError())
                )
            self.assertEqual(batches, [[]])
            self.assertEqual(errors.getvalue(), "Example: fetch failed; no cached entries\n")


if __name__ == "__main__":
    unittest.main()
