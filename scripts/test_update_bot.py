"""Offline regression tests for update_bot.py: python3 -m unittest scripts.test_update_bot."""

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts import update_bot as bot


class VersionTests(unittest.TestCase):
    def test_natural_order_and_prereleases(self):
        self.assertGreater(bot.version_key("1.10.0"), bot.version_key("1.9.9"))
        self.assertLess(bot.version_key("2.0.0-rc.2"), bot.version_key("2.0.0-rc.10"))
        self.assertLess(bot.version_key("2.0.0-rc.10"), bot.version_key("2.0.0"))
        self.assertTrue(bot.is_prerelease("v3.2.0-beta1"))
        self.assertFalse(bot.is_prerelease("3.2.0"))

    def test_tag_prefix_mapping_and_version_pattern(self):
        self.assertEqual(bot.version_for_tag("v2.4.0", "v2.3.0", "2.3.0", {}), "2.4.0")
        self.assertEqual(
            bot.version_for_tag("app-release-3.2", None, "3.1", {"version_pattern": r"release-(?P<version>[0-9.]+)"}),
            "3.2",
        )

    def test_channel_matches_component_boundary(self):
        self.assertTrue(bot.channel_matches("1.9.0", "1"))
        self.assertFalse(bot.channel_matches("10.0.0", "1"))

    def test_select_highest_ignores_prerelease_by_default(self):
        candidates = [("1.9.0", "v1.9.0", {}), ("2.0.0-rc1", "v2.0.0-rc1", {})]
        self.assertEqual(bot._select_highest(candidates, {}, "test")[0], "1.9.0")


class UrlAndMetadataTests(unittest.TestCase):
    def test_github_archive_and_asset_urls_are_parsed(self):
        parsed = bot.parse_github_url("https://github.com/acme/tool/archive/refs/tags/v1.2.3.tar.gz")
        self.assertEqual((parsed["owner"], parsed["repo"], parsed["tag"]), ("acme", "tool", "v1.2.3"))
        asset = bot.parse_github_url("https://github.com/acme/tool/releases/download/v1.2.3/tool-linux.tar.gz")
        self.assertEqual(asset["asset"], "tool-linux.tar.gz")

    def test_rejects_local_and_credential_urls(self):
        for url in ("file:///etc/passwd", "https://user:secret@example.org/file"):
            with self.subTest(url=url), self.assertRaises(bot.UpstreamError):
                bot.validate_url(url)

    def test_zon_field_updates_escape_values(self):
        source = '.version = "1.0",\n.url = "https://example.org/1.0",\n.hash = "old",\n'
        updated = bot.set_field(source, "url", 'https://example.org/a"b')
        self.assertEqual(bot.extract_field(updated, "url"), 'https://example.org/a"b')
        self.assertIn('a\\"b', updated)

    def test_atomic_write_preserves_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.zon"
            path.write_text("old", encoding="utf-8")
            path.chmod(0o640)
            bot.write_text_atomic(path, "new")
            self.assertEqual(path.read_text(encoding="utf-8"), "new")
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_download_hash_retries_transient_network_error(self):
        class Response:
            headers = {"Content-Length": "4"}

            def __enter__(self):
                return self

            def geturl(self):
                return "https://example.org/file"

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                if hasattr(self, "done"):
                    return b""
                self.done = True
                return b"data"

        with (
            patch.object(bot, "MAX_HTTP_RETRIES", 1),
            patch.object(bot.time, "sleep"),
            patch.object(bot.urllib.request, "urlopen", side_effect=[urllib.error.URLError("temporary"), Response()]) as open_url,
        ):
            self.assertEqual(bot.compute_sha256("https://example.org/file"), "3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7")
        self.assertEqual(open_url.call_count, 2)


class ProviderTests(unittest.TestCase):
    def test_json_path_and_relative_download_url(self):
        document = {"releases": [{"tag": "v2.5.1", "files": [{"url": "downloads/tool-2.5.1.tar.gz"}]}]}
        cfg = {
            "type": "json",
            "index": "https://example.org/api/releases",
            "items_path": "releases",
            "version_path": "tag",
            "version_pattern": r"^v(?P<version>[0-9.]+)$",
            "url_path": "files[0].url",
        }
        with patch.object(bot, "http_get", return_value=document):
            version, url = bot.resolve_json(cfg, "tool", "2.4.0", "https://example.org/downloads/tool-2.4.0.tar.gz")
        self.assertEqual(version, "2.5.1")
        self.assertEqual(url, "https://example.org/api/downloads/tool-2.5.1.tar.gz")

    def test_listing_uses_version_template(self):
        cfg = {
            "type": "listing",
            "index": "https://downloads.example.org/tool/",
            "pattern": r"tool-([0-9.]+)\.tar\.gz",
            "url_template": "https://downloads.example.org/tool-{version}.tar.gz",
        }
        with patch.object(bot, "http_get", return_value="tool-1.9.tar.gz tool-1.10.tar.gz"):
            version, url = bot.resolve_listing(cfg, "tool", "1.9", "https://downloads.example.org/tool-1.9.tar.gz")
        self.assertEqual(version, "1.10")
        self.assertEqual(url, "https://downloads.example.org/tool-1.10.tar.gz")

    def test_github_archive_falls_back_to_tags_without_releases(self):
        http_error = urllib.error.HTTPError("https://api.github.com/releases/latest", 404, "Not Found", {}, None)
        tags = [{"name": "v1.4.0"}, {"name": "v1.10.0"}]

        def fake_get(url, *, as_json=False, **kwargs):
            if url.endswith("/releases/latest"):
                raise http_error
            if "/tags?" in url:
                return tags
            raise AssertionError(f"unexpected URL: {url}")

        source = "https://github.com/acme/tool/archive/refs/tags/v1.3.0.tar.gz"
        with patch.object(bot, "http_get", side_effect=fake_get):
            version, url = bot.resolve_github("1.3.0", source, {"type": "github"}, "tool")
        self.assertEqual(version, "1.10.0")
        self.assertEqual(url, "https://github.com/acme/tool/archive/refs/tags/v1.10.0.tar.gz")

    def test_github_release_asset_name_template_selects_correct_asset(self):
        parsed = bot.parse_github_url("https://github.com/acme/tool/releases/download/v1.0.0/tool_1.0.0_linux_amd64.tar.gz")
        release = {"assets": [
            {"name": "tool_1.1.0_linux_arm64.tar.gz", "browser_download_url": "https://cdn.example.org/arm64"},
            {"name": "tool_1.1.0_linux_amd64.tar.gz", "browser_download_url": "https://cdn.example.org/amd64"},
        ]}
        url = bot._github_asset_url(
            parsed, release, {"asset_name": "tool_{version}_linux_amd64.tar.gz"},
            "tool", "1.0.0", "1.1.0", "v1.1.0",
        )
        self.assertEqual(url, "https://cdn.example.org/amd64")

    def test_feed_uses_explicit_link_when_enabled(self):
        feed = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Tool 2.1.0</title><link href="https://downloads.example.org/tool-2.1.0.tar.gz" /></entry></feed>'
        cfg = {"type": "feed", "index": "https://example.org/releases.atom", "version_pattern": r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)", "use_feed_link": True}
        with patch.object(bot, "http_get", return_value=feed):
            version, url = bot.resolve_feed(cfg, "tool", "2.0.0", "https://downloads.example.org/tool-2.0.0.tar.gz")
        self.assertEqual(version, "2.1.0")
        self.assertEqual(url, "https://downloads.example.org/tool-2.1.0.tar.gz")

    def test_gitlab_uses_tags_when_release_list_is_empty(self):
        replies = iter([[], [{"name": "v1.2.0"}, {"name": "v1.10.0"}]])
        with patch.object(bot, "http_get", side_effect=lambda *args, **kwargs: next(replies)):
            version, url = bot.resolve_gitlab(
                "1.0.0",
                "https://gitlab.com/group/project/-/archive/v1.0.0/project-v1.0.0.tar.gz",
                {"type": "gitlab"},
                "project",
            )
        self.assertEqual(version, "1.10.0")
        self.assertEqual(url, "https://gitlab.com/group/project/-/archive/v1.10.0/project-v1.10.0.tar.gz")

    def test_gitlab_channel_reads_tags_as_tags(self):
        with patch.object(bot, "http_get", return_value=[{"name": "v1.2.0"}]) as get:
            version, _url = bot.resolve_gitlab(
                "1.0.0",
                "https://gitlab.com/group/project/-/archive/v1.0.0/project-v1.0.0.tar.gz",
                {"type": "gitlab", "channel": "1"},
                "project",
            )
        self.assertEqual(version, "1.2.0")
        self.assertIn("repository/tags", get.call_args.args[0])

    def test_validate_configs_rejects_missing_provider_fields(self):
        with self.assertRaises(bot.UpstreamError):
            bot.validate_config({"type": "json", "index": "https://example.org/api"}, "bad")

    def test_all_repository_configs_validate_offline(self):
        errors = bot.validate_packages("pkgs")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
