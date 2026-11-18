#!/usr/bin/env python3
"""Collect a deterministic, cache-backed snapshot of recent AI news."""

import argparse
import concurrent.futures
import contextlib
import hashlib
import html
import ipaddress
import json
import math
import os
import posixpath
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone


ITEM_FIELDS = ("source", "id", "title", "url", "published", "summary", "primary")
MAX_ITEM_URL_CHARS = 1024
HARD_DEADLINE_EXIT_CODE = 124


@contextlib.contextmanager
def hard_process_deadline(seconds):
    """Terminate the process if wall-clock collection exceeds seconds."""
    def expire():
        try:
            os.write(2, b"hard deadline exceeded\n")
        finally:
            os._exit(HARD_DEADLINE_EXIT_CODE)

    timer = threading.Timer(float(seconds), expire)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        timer.join()


def _unsafe_literal_host(host):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not address.is_global


class SafeSourceRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow redirects only within one trusted HTTPS source hostname."""

    def __init__(self, source_url):
        super().__init__()
        self.source_host = (urllib.parse.urlsplit(source_url).hostname or "").lower()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        host = (parts.hostname or "").lower()
        try:
            parts.port
        except ValueError:
            host = ""
        if (parts.scheme.lower() != "https" or host != self.source_host
                or parts.username is not None or parts.password is not None
                or _unsafe_literal_host(host)):
            raise urllib.error.HTTPError(newurl, code, "unsafe source redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def project_item(item):
    return {field: item.get(field, False if field == "primary" else "")
            for field in ITEM_FIELDS}


def normalize_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]*>", " ", value)
    return " ".join(value.split())


def canonicalize_url(value, max_chars=MAX_ITEM_URL_CHARS):
    value = (value or "").strip()
    if not value or len(value) > max_chars:
        return ""
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except (TypeError, ValueError):
        return ""
    scheme = parts.scheme.lower()
    if (scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None):
        return ""
    host = (parts.hostname or "").lower()
    if any(character.isspace() for character in host):
        return ""
    if ":" in host:
        host = f"[{host}]"
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        host += f":{port}"
    path = posixpath.normpath(parts.path or "/")
    if parts.path.endswith("/") and path != "/":
        path += "/"
    if path != "/":
        path = path.rstrip("/")
    tracking = {"fbclid", "gclid", "dclid", "msclkid"}
    query = [
        (key, val) for key, val in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in tracking
        and not key.lower().startswith(("utm_", "mc_"))
    ]
    canonical = urllib.parse.urlunsplit(
        (scheme, host, path, urllib.parse.urlencode(sorted(query)), "")
    )
    return canonical if len(canonical) <= max_chars else ""


def normalize_date(value):
    if not value:
        return ""
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def child_text(node, *names):
    for child in node:
        if child.tag.rsplit("}", 1)[-1] in names:
            return child.text or ""
    return ""


def parse_document(data, source, max_item_url_chars=MAX_ITEM_URL_CHARS):
    root = ET.fromstring(data)
    kind = root.tag.rsplit("}", 1)[-1]
    if source.get("type") == "sitemap":
        if kind != "urlset":
            raise ValueError(f"expected sitemap urlset root, got {kind}")
        items = []
        for node in root:
            if node.tag.rsplit("}", 1)[-1] != "url":
                continue
            url = canonicalize_url(child_text(node, "loc"), max_item_url_chars)
            if source.get("path_contains") and source["path_contains"] not in url:
                continue
            slug = urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
            title = normalize_text(urllib.parse.unquote(slug).replace("-", " ")).title()
            items.append({
                "id": url,
                "source": source["name"],
                "title": title,
                "url": url,
                "published": normalize_date(child_text(node, "lastmod")),
                "summary": "",
                "primary": bool(source.get("primary", False)),
            })
        if not items:
            raise ValueError("document contains no usable sitemap entries")
        return items
    if kind not in {"rss", "feed", "RDF"}:
        raise ValueError(f"expected RSS or Atom root, got {kind}")
    node_name = "item" if kind in {"rss", "RDF"} else "entry"
    nodes = [node for node in root.iter()
             if node.tag.rsplit("}", 1)[-1] == node_name]
    items = []
    for node in nodes:
        title = normalize_text(child_text(node, "title"))
        if kind in {"rss", "RDF"}:
            url = canonicalize_url(child_text(node, "link"), max_item_url_chars)
            published = normalize_date(child_text(
                node, "pubDate", "published", "updated", "date"
            ))
            summary = normalize_text(child_text(node, "description", "summary", "content"))
        else:
            links = [child for child in node if child.tag.rsplit("}", 1)[-1] == "link"]
            alternate = next((link for link in links if link.get("rel", "alternate") == "alternate"), None)
            url = canonicalize_url(
                alternate.get("href", "") if alternate is not None else "",
                max_item_url_chars,
            )
            published = normalize_date(child_text(node, "published", "updated"))
            summary = normalize_text(child_text(node, "summary", "content"))
        feed_id = child_text(node, "guid") if kind in {"rss", "RDF"} else child_text(node, "id")
        identity = normalize_text(feed_id) or url or hashlib.sha256(
            (title + "\0" + published).encode()
        ).hexdigest()
        items.append({
            "id": identity,
            "source": source["name"],
            "title": title,
            "url": url,
            "published": published,
            "summary": summary,
            "primary": bool(source.get("primary", False)),
        })
    items = [item for item in items if item["title"] and item["url"]]
    if not items:
        raise ValueError("document contains no usable feed entries")
    return items


def parse_now(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must be a timezone-aware UTC ISO-8601 timestamp")
    return parsed.astimezone(timezone.utc)


def cache_path(state_dir, source):
    digest = hashlib.sha256(source["url"].encode()).hexdigest()[:16]
    return state_dir / f"source-{digest}.json"


def atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".tmp-", delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(value, handle, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        try:
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def load_cache(path, source, max_item_url_chars=MAX_ITEM_URL_CHARS):
    """Return valid cache state, or empty state when missing/corrupt."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if (not isinstance(value, dict) or not isinstance(value.get("items"), list)
            or not value["items"]):
        return {}
    string_fields = ITEM_FIELDS[:-1]
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != set(ITEM_FIELDS):
            return {}
        if any(not isinstance(item.get(field), str) for field in string_fields):
            return {}
        if type(item.get("primary")) is not bool:
            return {}
        if (item["source"] != source["name"]
                or item["primary"] is not source["primary"]
                or not item["id"].strip() or not item["title"].strip()):
            return {}
        url = item["url"]
        if not url or canonicalize_url(url, max_item_url_chars) != url:
            return {}
        published = item["published"]
        if published:
            try:
                if normalize_date(published) != published:
                    return {}
            except (TypeError, ValueError):
                return {}
    return value


def fetch_live_source(source, state_dir, timeout, opener=None):
    max_item_url_chars = int(source.get("_max_item_url_chars", MAX_ITEM_URL_CHARS))
    cached = cache_path(state_dir, source)
    prior = load_cache(cached, source, max_item_url_chars)
    headers = {
        "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9",
        "User-Agent": "Hermes-AI-News-Monitor/1.0",
    }
    if prior.get("etag"):
        headers["If-None-Match"] = prior["etag"]
    if prior.get("last_modified"):
        headers["If-Modified-Since"] = prior["last_modified"]
    request = urllib.request.Request(source["url"], headers=headers)
    if opener is None:
        opener = urllib.request.build_opener(
            SafeSourceRedirectHandler(source["url"])
        ).open
    try:
        with opener(request, timeout=timeout) as response:
            data = response.read(5 * 1024 * 1024 + 1)
            if len(data) > 5 * 1024 * 1024:
                raise ValueError("response exceeds 5 MiB")
            items = parse_document(data, source, max_item_url_chars)
            items.sort(key=lambda item: (item["published"], item["url"], item["title"]), reverse=True)
            value = {
                "etag": response.headers.get("ETag", ""),
                "last_modified": response.headers.get("Last-Modified", ""),
                "items": items[:int(source.get("max_entries", 10))],
            }
            atomic_write_json(cached, value)
            return value["items"]
    except urllib.error.HTTPError as error:
        if error.code == 304 and "items" in prior:
            return prior["items"]
        raise


def retain_recent(items, now, lookback_hours):
    cutoff = now.timestamp() - int(lookback_hours) * 3600
    return [
        item for item in items
        if item["published"] and parse_now(item["published"]).timestamp() >= cutoff
    ]


def merge_items(items):
    """Keep newest URL/identity duplicates within each source."""
    ranked = sorted(items, key=lambda item: (
        item["published"], item["title"], item["summary"], item["url"], item["id"]
    ), reverse=True)
    merged = []
    seen = set()
    for item in ranked:
        source = item.get("source")
        aliases = {(source, "id", item["id"])}
        if item.get("url"):
            aliases.add((source, "url", item["url"]))
        if aliases & seen:
            continue
        merged.append(item)
        seen.update(aliases)
    return sorted(
        merged,
        key=lambda item: (item["published"], item["url"], item["title"]),
        reverse=True,
    )


def validate_config(config):
    """Validate the complete source contract before any network operation."""
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    for key, default in (("lookback_hours", 72), ("max_total", 100),
                         ("max_output_bytes", 7500), ("max_summary_chars", 200),
                         ("max_title_chars", 180), ("max_id_chars", 96),
                         ("max_source_name_chars", 80), ("fetch_workers", 8),
                         ("max_item_url_chars", MAX_ITEM_URL_CHARS)):
        value = config.get(key, default)
        if type(value) is not int or value <= 0:
            raise ValueError(f"{key} must be a positive integer")
    timeout = config.get("fetch_timeout_seconds", 10)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("fetch_timeout_seconds must be finite and positive")
    deadline = config.get("hard_deadline_seconds", 90)
    if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline) or deadline <= 0):
        raise ValueError("hard_deadline_seconds must be finite and positive")
    if config.get("max_id_chars", 96) < 71:
        raise ValueError("max_id_chars must fit a SHA-256 token")
    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must be a non-empty list")
    names = set()
    urls = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("each source must be an object")
        name = source.get("name")
        url = source.get("url")
        if (not isinstance(name, str) or not name.strip() or name in names
                or len(name) > int(config.get("max_source_name_chars", 80))):
            raise ValueError("source names must be bounded, non-empty, and unique")
        try:
            parts = urllib.parse.urlsplit(url if isinstance(url, str) else "")
            parts.port
        except ValueError:
            parts = urllib.parse.SplitResult("", "", "", "", "")
        canonical = canonicalize_url(url) if parts.scheme.lower() == "https" else ""
        host = (parts.hostname or "").lower()
        if (not canonical or parts.username is not None or parts.password is not None
                or _unsafe_literal_host(host) or canonical in urls):
            raise ValueError("source URLs must be unique safe HTTPS URLs")
        if source.get("type") not in {"rss", "sitemap"}:
            raise ValueError("source type must be rss or sitemap")
        if type(source.get("primary")) is not bool:
            raise ValueError("source primary must be boolean")
        maximum = source.get("max_entries")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            raise ValueError("source max_entries must be a positive integer")
        if source["type"] == "sitemap" and (
            not isinstance(source.get("path_contains"), str) or not source["path_contains"]
        ):
            raise ValueError("sitemap sources require path_contains")
        names.add(name)
        urls.add(canonical)


def collect_sources(config, state_dir, fixture_dir=None, fetcher=None):
    validate_config(config)
    timeout = float(config.get("fetch_timeout_seconds", 10))
    max_item_url_chars = int(config.get("max_item_url_chars", MAX_ITEM_URL_CHARS))
    sources = [dict(source, _max_item_url_chars=max_item_url_chars)
               for source in config["sources"]]

    def fixture_fetch(source, target_state, unused_timeout):
        data = (fixture_dir / source.get("fixture", "")).read_bytes()
        items = parse_document(data, source, max_item_url_chars)
        items.sort(key=lambda item: (item["published"], item["url"], item["title"]), reverse=True)
        items = items[:int(source.get("max_entries", 10))]
        atomic_write_json(cache_path(target_state, source), {"items": items})
        return items

    operation = fetcher or (fixture_fetch if fixture_dir is not None else fetch_live_source)
    workers = max(1, min(int(config.get("fetch_workers", 8)), len(sources)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(operation, source, state_dir, timeout)
                   for source in sources]
        batches = []
        for source, future in zip(sources, futures):
            try:
                batches.append(future.result())
            except Exception:
                cached = cache_path(state_dir, source)
                prior = load_cache(cached, source, max_item_url_chars)
                if prior:
                    print(f"{source['name']}: fetch failed; using cached entries", file=sys.stderr)
                    batches.append(prior["items"])
                else:
                    print(f"{source['name']}: fetch failed; no cached entries", file=sys.stderr)
                    batches.append([])
    return batches


def snapshot_bytes(snapshot):
    """Stable JSON with each item isolated on one diff-friendly line."""
    lines = ['{"items":[']
    items = [project_item(item) for item in snapshot.get("items", [])]
    for index, item in enumerate(items):
        suffix = "," if index + 1 < len(items) else ""
        lines.append(json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + suffix)
    lines.append("]}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_snapshot(items, config):
    """Select a bounded snapshot, preserving the newest item per source."""
    max_total = int(config.get("max_total", 100))
    max_bytes = int(config.get("max_output_bytes", 7500))
    summary_chars = int(config.get("max_summary_chars", 200))
    title_chars = int(config.get("max_title_chars", 180))
    id_chars = int(config.get("max_id_chars", 96))
    url_chars = int(config.get("max_item_url_chars", MAX_ITEM_URL_CHARS))

    # Never mutate parser results or last-good cache dictionaries.
    safe_items = []
    for item in items:
        projected = project_item(item)
        if canonicalize_url(projected["url"], url_chars) == projected["url"]:
            safe_items.append(projected)
    ranked = merge_items(safe_items)
    for item in ranked:
        item["summary"] = item.get("summary", "")[:summary_chars]
        item["title"] = item.get("title", "")[:title_chars]
        identity = item.get("id", "")
        if len(identity) > id_chars:
            item["id"] = "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def ordered(values):
        return sorted(values, key=lambda item: (
            item["published"], item["url"], item["title"], item["source"]
        ), reverse=True)

    representatives = {}
    for item in ranked:
        representatives.setdefault(item["source"], item)
    selected = list(representatives.values())
    if len(selected) > max_total:
        raise ValueError("mandatory source representatives exceed max_total")

    if len(snapshot_bytes({"items": ordered(selected)})) > max_bytes:
        for item in selected:
            item["summary"] = ""
    if len(snapshot_bytes({"items": ordered(selected)})) > max_bytes:
        raise ValueError("mandatory source representatives exceed max_output_bytes")

    selected_ids = {id(item) for item in selected}
    for item in ranked:
        if len(selected) >= max_total or id(item) in selected_ids:
            continue
        candidate = ordered(selected + [item])
        if len(snapshot_bytes({"items": candidate})) <= max_bytes:
            selected.append(item)
            selected_ids.add(id(item))
    return {"items": ordered(selected)}


def collect_snapshot(config, state_dir, fixture_dir, now):
    collected = [item for batch in collect_sources(config, state_dir, fixture_dir) for item in batch]
    retained = retain_recent(collected, now, config.get("lookback_hours", 72))
    return build_snapshot(retained, config)


def parse_args(argv=None):
    profile = "/home/USER/.hermes/profiles/ai-researcher"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=f"{profile}/monitoring/major-ai-sources.json")
    parser.add_argument("--state-dir", default=f"{profile}/monitoring/state")
    parser.add_argument("--fixture-dir")
    parser.add_argument("--now", help="UTC ISO-8601 time; intended for deterministic verification")
    return parser.parse_args(argv)


def main(argv=None):
    from pathlib import Path
    args = parse_args(argv)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    validate_config(config)
    with hard_process_deadline(config.get("hard_deadline_seconds", 90)):
        now = parse_now(args.now) if args.now else datetime.now(timezone.utc)
        fixture_dir = Path(args.fixture_dir) if args.fixture_dir else None
        snapshot = collect_snapshot(config, Path(args.state_dir), fixture_dir, now)
        sys.stdout.buffer.write(snapshot_bytes(snapshot))


if __name__ == "__main__":
    main()
