#!/usr/bin/env python3
"""Update zp package metadata from upstream release sources.

The bot is deliberately configuration-driven: there is no universally reliable
way to infer an upstream's release policy or download URL from a project name.
``pkgs/<name>/update.json`` selects a provider and documents the exact rule.

Supported providers:
  github       GitHub Releases; GitHub source archives fall back to tags when
               the repository has no Releases.
  github-tag   GitHub tags, useful for projects that publish tags but no Releases.
  gitlab       GitLab Releases (or tags as a fallback for archive URLs).
  gitlab-tag   GitLab repository tags.
  listing      Regex-discovered versions in an HTML/text directory listing.
  json         Versions and optional asset URLs in a JSON API.
  feed         RSS/Atom version feeds.
  none         Explicitly disable automated updates.

Examples and the full update.json schema are documented in
``scripts/UPDATE_BOT.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

USER_AGENT = "zp-update-bot/2.0 (+https://github.com/understratum/zp-repo)"
TIMEOUT = 30
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_HASH_BYTES = 8 * 1024 * 1024 * 1024
MAX_HTTP_RETRIES = 3
MAX_API_PAGES = 10
GITHUB_API = "https://api.github.com"

ALLOWED_SCHEMES = {"http", "https", "ftp"}
ARCHIVE_SUFFIX_RE = re.compile(r"(?P<tag>.+?)(?P<suffix>\.tar\.(?:gz|xz|bz2|zst)|\.tgz|\.zip)$", re.I)
PRE_RELEASE_RE = re.compile(
    r"(?:^|[._+\-])(alpha|beta|preview|pre|rc|dev|nightly|snapshot|canary|next|unstable)"
    r"(?:[._+\-]?\d|$)",
    re.I,
)


class UpstreamError(Exception):
    """A source or configuration could not be resolved safely."""


def validate_url(url: str, *, allow_ftp: bool = True) -> str:
    """Validate a configured/fetched URL and return its normalized string.

    FTP is retained for existing GNU mirrors. File URLs, embedded credentials,
    and unsupported schemes are rejected to avoid accidental local-file reads.
    """
    if not isinstance(url, str) or not url.strip():
        raise UpstreamError("URL пуст или не является строкой")
    url = url.strip()
    parts = urllib.parse.urlsplit(url)
    allowed = ALLOWED_SCHEMES if allow_ftp else {"http", "https"}
    if parts.scheme.lower() not in allowed or not parts.hostname:
        raise UpstreamError(f"недопустимый URL: {url!r}")
    if parts.username is not None or parts.password is not None:
        raise UpstreamError("URL с логином/паролем запрещён")
    return url


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    value = error.headers.get("Retry-After") if error.headers else None
    if value:
        try:
            return min(max(float(value), 0.0), 10.0)
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return min(max((date - datetime.now(timezone.utc)).total_seconds(), 0.0), 10.0)
            except (TypeError, ValueError, OverflowError):
                pass
    return min(0.5 * (2**attempt), 4.0)


def _request_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = TIMEOUT,
    max_bytes: int = MAX_METADATA_BYTES,
) -> tuple[bytes, Any, str]:
    url = validate_url(url)
    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    host = urllib.parse.urlsplit(url).hostname
    if token and host == "api.github.com":
        request_headers["Authorization"] = f"Bearer {token}"
        request_headers["X-GitHub-Api-Version"] = "2022-11-28"
        request_headers["Accept"] = "application/vnd.github+json"
    if headers:
        request_headers.update(headers)

    for attempt in range(MAX_HTTP_RETRIES + 1):
        request = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                final_url = validate_url(response.geturl())
                size = response.headers.get("Content-Length")
                if size and int(size) > max_bytes:
                    raise UpstreamError(f"ответ слишком большой ({size} байт): {url}")
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise UpstreamError(f"ответ превысил лимит {max_bytes} байт: {url}")
                return body, response.headers, final_url
        except urllib.error.HTTPError as error:
            if error.code in (408, 425, 429, 500, 502, 503, 504) and attempt < MAX_HTTP_RETRIES:
                time.sleep(_retry_delay(error, attempt))
                continue
            raise
        except urllib.error.URLError:
            if attempt >= MAX_HTTP_RETRIES:
                raise
            time.sleep(min(0.5 * (2**attempt), 4.0))
    raise UpstreamError(f"не удалось выполнить запрос: {url}")


def http_get(url: str, *, as_json: bool = False, timeout: int = TIMEOUT):
    """Fetch bounded text or JSON with retries and GitHub authentication."""
    body, _, final_url = _request_bytes(url, timeout=timeout)
    if as_json:
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpstreamError(f"невалидный JSON от {final_url}: {error}") from error
    return body.decode("utf-8", errors="replace")


def compute_sha256(url: str, *, max_bytes: int = MAX_HASH_BYTES, timeout: int = TIMEOUT) -> str:
    """Stream a download into SHA-256, failing on HTTP errors or oversized files."""
    url = validate_url(url)
    headers = {"User-Agent": USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and urllib.parse.urlsplit(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(MAX_HTTP_RETRIES + 1):
        request = urllib.request.Request(url, headers=headers)
        h = hashlib.sha256()
        total = 0
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                validate_url(response.geturl())
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise UpstreamError(f"файл превышает лимит {max_bytes} байт: {url}")
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise UpstreamError(f"файл превысил лимит {max_bytes} байт: {url}")
                    h.update(chunk)
        except urllib.error.HTTPError as error:
            if error.code in (408, 425, 429, 500, 502, 503, 504) and attempt < MAX_HTTP_RETRIES:
                time.sleep(_retry_delay(error, attempt))
                continue
            raise UpstreamError(f"HTTP {error.code} при скачивании {url}") from error
        except urllib.error.URLError as error:
            if attempt < MAX_HTTP_RETRIES:
                time.sleep(min(0.5 * (2**attempt), 4.0))
                continue
            raise UpstreamError(f"сеть недоступна при скачивании {url}: {error.reason}") from error
        if total == 0:
            raise UpstreamError(f"скачан пустой файл: {url}")
        return h.hexdigest()
    raise UpstreamError(f"не удалось скачать файл: {url}")


# --------------------------------------------------------------------------- #
# .zon editing
# --------------------------------------------------------------------------- #


def read_text(path: str | os.PathLike[str]) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text_atomic(path: str | os.PathLike[str], content: str) -> None:
    path = Path(path)
    old_mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, old_mode)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _zon_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def extract_field(content: str, field: str) -> str | None:
    match = re.search(rf'\.{re.escape(field)}\s*=\s*"((?:\\.|[^"\\])*)"', content)
    if not match:
        return None
    return re.sub(r"\\([\\\"])", r"\1", match.group(1))


def update_field(content: str, field: str, value: str) -> str:
    pattern = rf'(\.{re.escape(field)}\s*=\s*")((?:\\.|[^"\\])*)(")'
    escaped = _zon_escape(value)
    if not re.search(pattern, content):
        raise UpstreamError(f"не удалось найти поле .{field} для записи")
    return re.sub(pattern, lambda match: match.group(1) + escaped + match.group(3), content, count=1)


def set_field(content: str, field: str, value: str) -> str:
    if re.search(rf'\.{re.escape(field)}\s*=\s*"', content):
        return update_field(content, field, value)
    idx = content.rfind("}")
    if idx == -1:
        raise UpstreamError("повреждён .zon: отсутствует закрывающая скобка")
    insert = f'    .{field} = "{_zon_escape(value)}",\n'
    return content[:idx] + insert + content[idx:]


# --------------------------------------------------------------------------- #
# Version parsing and selectors
# --------------------------------------------------------------------------- #


def is_prerelease(version: str) -> bool:
    return bool(PRE_RELEASE_RE.search(version))


def version_key(version: str):
    """Natural ordering with prereleases below their corresponding final release."""
    value = version.strip().lower()
    prerelease = PRE_RELEASE_RE.search(value)
    if prerelease:
        core = value[:prerelease.start(1)].rstrip(".-_+")
        suffix = value[prerelease.start(1):]
    else:
        core, suffix = value, ""
    core_tokens = tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.findall(r"\d+|[a-z]+", core)
    )
    suffix_tokens = tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.findall(r"\d+|[a-z]+", suffix)
    )
    return (core_tokens, 0 if prerelease else 1, suffix_tokens, value)


def tag_affixes(cur_tag: str, cur_version: str):
    """Return a prefix/suffix mapping between an upstream tag and package version."""
    if cur_tag == cur_version:
        return "", ""
    if cur_version and cur_tag.endswith(cur_version):
        return cur_tag[: -len(cur_version)], ""
    if cur_version and cur_tag.startswith(cur_version):
        return "", cur_tag[len(cur_version):]
    return None, None


def version_for_tag(tag: str, cur_tag: str | None, cur_version: str, cfg: dict) -> str | None:
    pattern = cfg.get("version_pattern")
    if pattern:
        try:
            match = re.search(pattern, tag)
        except re.error as error:
            raise UpstreamError(f"невалидный version_pattern {pattern!r}: {error}") from error
        if not match:
            return None
        groups = match.groupdict()
        if "version" in groups:
            extracted = groups["version"]
        elif match.groups():
            extracted = match.group(1)
        else:
            extracted = match.group(0)
        template = cfg.get("version_template")
        if template:
            fields = {str(i): group for i, group in enumerate(match.groups(), 1)}
            fields.update({key: value for key, value in groups.items() if value is not None})
            fields["version"] = extracted
            try:
                return str(template).format_map(fields)
            except (KeyError, ValueError) as error:
                raise UpstreamError(f"не удалось применить version_template: {error}") from error
        return extracted

    if cur_tag:
        prefix, suffix = tag_affixes(cur_tag, cur_version)
        if prefix is not None and tag.startswith(prefix) and tag.endswith(suffix):
            stop = len(tag) - len(suffix) if suffix else len(tag)
            version = tag[len(prefix):stop]
            if version:
                return version
    if tag[:1] in ("v", "V") and len(tag) > 1 and tag[1].isdigit():
        return tag[1:]
    # Common project-prefixed tags: project-1.2.3, release-1.2.3.
    match = re.search(r"(\d+\.\d+(?:\.\d+)*(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?)$", tag)
    return match.group(1) if match else tag


def channel_matches(version: str, channel: Any) -> bool:
    """Match channels such as '1', '3.5', or '2026.1' at component boundaries."""
    if channel is None or str(channel).strip() == "":
        return True
    channel_text = str(channel).strip()
    version_text = version.strip()
    if channel_text[:1].lower() == "v":
        channel_text = channel_text[1:]
    if version_text[:1].lower() == "v":
        version_text = version_text[1:]
    if version_text == channel_text:
        return True
    return version_text.startswith(channel_text) and version_text[len(channel_text):len(channel_text) + 1] in (".", "-", "+", "_")


def _candidate_is_prerelease(version: str, entry: dict, cfg: dict) -> bool:
    if cfg.get("include_prerelease", False):
        return False
    return bool(entry.get("prerelease", False)) or is_prerelease(version)


def _select_highest(candidates: list[tuple[str, str, Any]], cfg: dict, source: str) -> tuple[str, str, Any]:
    eligible = []
    for version, tag, payload in candidates:
        if not version or _candidate_is_prerelease(version, payload if isinstance(payload, dict) else {}, cfg):
            continue
        if not channel_matches(version, cfg.get("channel")):
            continue
        eligible.append((version, tag, payload))
    if not eligible:
        channel = cfg.get("channel")
        suffix = f" для channel={channel!r}" if channel is not None else ""
        raise UpstreamError(f"{source}: не нашлось стабильной версии{suffix}")
    return max(eligible, key=lambda item: (version_key(item[0]), version_key(item[1])))


# --------------------------------------------------------------------------- #
# URL / config helpers
# --------------------------------------------------------------------------- #


def parse_github_url(url: str) -> dict | None:
    """Parse GitHub release-asset and archive URLs, including slash-containing tags."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "github.com":
        return None
    path = parts.path.strip("/").split("/")
    if len(path) < 4:
        return None
    owner, repo = urllib.parse.unquote(path[0]), urllib.parse.unquote(path[1])
    tail = path[2:]
    if len(tail) >= 4 and tail[:3] == ["archive", "refs", "tags"]:
        archive_path = "/".join(tail[3:])
        match = ARCHIVE_SUFFIX_RE.match(archive_path)
        if not match:
            return None
        return {
            "kind": "archive",
            "owner": owner,
            "repo": repo,
            "tag": urllib.parse.unquote(match.group("tag")),
            "suffix": match.group("suffix"),
            "base": f"https://github.com/{owner}/{repo}/archive/refs/tags/",
        }
    if len(tail) >= 2 and tail[0] == "archive":
        archive_path = "/".join(tail[1:])
        match = ARCHIVE_SUFFIX_RE.match(archive_path)
        if match:
            return {
                "kind": "archive-short",
                "owner": owner,
                "repo": repo,
                "tag": urllib.parse.unquote(match.group("tag")),
                "suffix": match.group("suffix"),
                "base": f"https://github.com/{owner}/{repo}/archive/",
            }
    if len(tail) >= 3 and tail[:2] == ["releases", "download"]:
        release_parts = tail[2:]
        asset = urllib.parse.unquote(release_parts[-1])
        tag = urllib.parse.unquote("/".join(release_parts[:-1]))
        if tag and asset:
            return {
                "kind": "release-asset",
                "owner": owner,
                "repo": repo,
                "tag": tag,
                "asset": asset,
                "base": f"https://github.com/{owner}/{repo}/releases/download/",
            }
    return None


def _template_fields(
    *, name: str, version: str, tag: str = "", cur_version: str = "", cur_tag: str = "",
    cfg: dict | None = None, asset: str = "", base: str = "", project: str = "", url: str = "",
) -> dict[str, str]:
    cfg = cfg or {}
    nums = re.findall(r"\d+", version)
    fields = {
        "name": name,
        "version": version,
        "tag": tag,
        "cur_version": cur_version,
        "cur_tag": cur_tag,
        "major": nums[0] if nums else "",
        "minor": nums[1] if len(nums) > 1 else "",
        "patch": nums[2] if len(nums) > 2 else "",
        "asset": asset,
        "base": base,
        "project": project or str(cfg.get("project", "")),
        "owner": str(cfg.get("owner", "")),
        "repo": str(cfg.get("repo", "")),
        "channel": str(cfg.get("channel", "")),
        "url": url,
    }
    return fields


def format_template_value(template: str, fields: dict[str, str], setting: str) -> str:
    try:
        value = template.format_map(fields)
    except (KeyError, ValueError) as error:
        raise UpstreamError(f"ошибка {setting} {template!r}: {error}") from error
    if not value:
        raise UpstreamError(f"пустое значение после обработки {setting}")
    return value


def format_url_template(template: str, fields: dict[str, str]) -> str:
    return validate_url(format_template_value(template, fields, "url_template"))


def _replace_current_version(cur_url: str, cur_version: str, new_version: str, cur_tag: str = "", new_tag: str = "") -> str:
    if cur_version and cur_version in cur_url:
        return cur_url.replace(cur_version, new_version)
    if cur_tag and cur_tag in cur_url:
        return cur_url.replace(cur_tag, new_tag or new_version)
    raise UpstreamError("нужен url_template: текущая версия/тег не найдены в .url")


def _github_archive_url(parsed: dict, tag: str) -> str:
    encoded = urllib.parse.quote(tag, safe="/")
    return validate_url(parsed["base"] + encoded + parsed["suffix"])


def _github_asset_url(
    parsed: dict, release: dict, cfg: dict, name: str, cur_version: str,
    new_version: str, new_tag: str,
) -> str:
    if cfg.get("url_template"):
        return format_url_template(
            cfg["url_template"],
            _template_fields(
                name=name, version=new_version, tag=new_tag, cur_version=cur_version,
                cur_tag=parsed["tag"], cfg=cfg, asset=parsed["asset"], base=parsed["base"],
            ),
        )
    assets = release.get("assets") or []
    desired = cfg.get("asset_name")
    if desired:
        desired = format_template_value(
            desired,
            _template_fields(
                name=name, version=new_version, tag=new_tag, cur_version=cur_version,
                cur_tag=parsed["tag"], cfg=cfg, asset=parsed["asset"], base=parsed["base"],
            ),
            "asset_name",
        )
        desired_name = urllib.parse.unquote(urllib.parse.urlsplit(desired).path.rsplit("/", 1)[-1])
    else:
        desired_name = parsed["asset"]
        if parsed["tag"] in desired_name:
            desired_name = desired_name.replace(parsed["tag"], new_tag)
        if cur_version and cur_version in desired_name:
            desired_name = desired_name.replace(cur_version, new_version)
    selected = [asset for asset in assets if asset.get("name") == desired_name]
    if not selected and cfg.get("asset_pattern"):
        try:
            pattern = re.compile(cfg["asset_pattern"])
        except re.error as error:
            raise UpstreamError(f"невалидный asset_pattern: {error}") from error
        selected = [asset for asset in assets if pattern.search(str(asset.get("name", "")))]
    if len(selected) > 1:
        raise UpstreamError(f"{name}: asset неоднозначен ({', '.join(a.get('name','?') for a in selected)})")
    if len(selected) == 1 and selected[0].get("browser_download_url"):
        return validate_url(selected[0]["browser_download_url"])
    # Some projects omit assets from the API response. Reconstruct only the
    # configured/current asset URL; the subsequent download+hash is authoritative.
    if cfg.get("allow_asset_url_fallback", True):
        new_asset = parsed["asset"]
        if parsed["tag"] in new_asset:
            new_asset = new_asset.replace(parsed["tag"], new_tag)
        if cur_version and cur_version in new_asset:
            new_asset = new_asset.replace(cur_version, new_version)
        return validate_url(parsed["base"] + urllib.parse.quote(new_tag, safe="/") + "/" + urllib.parse.quote(new_asset, safe="-._~"))
    raise UpstreamError(f"{name}: релиз {new_tag} не содержит ожидаемый asset {desired_name!r}")


def _format_version(tag: str, cfg: dict, cur_tag: str | None, cur_version: str) -> str | None:
    return version_for_tag(tag, cur_tag, cur_version, cfg)


def _github_tag_candidates(owner: str, repo: str, cur_tag: str, cur_version: str, cfg: dict) -> list[tuple[str, str, Any]]:
    candidates = []
    tag_filter = cfg.get("tag_pattern")
    try:
        tag_regex = re.compile(tag_filter) if tag_filter else None
    except re.error as error:
        raise UpstreamError(f"невалидный tag_pattern {tag_filter!r}: {error}") from error
    for page in range(1, MAX_API_PAGES + 1):
        url = f"{GITHUB_API}/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}/tags?per_page=100&page={page}"
        try:
            tags = http_get(url, as_json=True)
        except urllib.error.HTTPError as error:
            raise UpstreamError(f"{owner}/{repo}: GitHub tags API вернул HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise UpstreamError(f"{owner}/{repo}: GitHub tags API недоступен ({error.reason})") from error
        if not isinstance(tags, list):
            raise UpstreamError(f"{owner}/{repo}: неожиданный ответ GitHub tags API")
        if not tags:
            break
        for item in tags:
            tag = item.get("name")
            if not tag or (tag_regex and not tag_regex.search(tag)):
                continue
            version = _format_version(tag, cfg, cur_tag, cur_version)
            if version:
                candidates.append((version, tag, item))
        if len(tags) < 100:
            break
    return candidates


def _github_releases(owner: str, repo: str, cur_tag: str, cur_version: str, cfg: dict) -> list[tuple[str, str, Any]]:
    candidates = []
    tag_filter = cfg.get("tag_pattern")
    try:
        tag_regex = re.compile(tag_filter) if tag_filter else None
    except re.error as error:
        raise UpstreamError(f"невалидный tag_pattern {tag_filter!r}: {error}") from error
    for page in range(1, MAX_API_PAGES + 1):
        url = f"{GITHUB_API}/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}/releases?per_page=100&page={page}"
        try:
            releases = http_get(url, as_json=True)
        except urllib.error.HTTPError as error:
            raise UpstreamError(f"{owner}/{repo}: GitHub releases API вернул HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise UpstreamError(f"{owner}/{repo}: GitHub releases API недоступен ({error.reason})") from error
        if not isinstance(releases, list):
            raise UpstreamError(f"{owner}/{repo}: неожиданный ответ GitHub releases API")
        if not releases:
            break
        for item in releases:
            tag = item.get("tag_name")
            if not tag or item.get("draft"):
                continue
            if item.get("prerelease") and not cfg.get("include_prerelease", False):
                continue
            if tag_regex and not tag_regex.search(tag):
                continue
            version = _format_version(tag, cfg, cur_tag, cur_version)
            if version:
                candidates.append((version, tag, item))
        if len(releases) < 100:
            break
    return candidates


def resolve_github(cur_version: str, cur_url: str, cfg: dict | None = None, name: str = "package"):
    cfg = cfg or {}
    parsed = parse_github_url(cur_url)
    owner = cfg.get("owner") or (parsed or {}).get("owner")
    repo = cfg.get("repo") or (parsed or {}).get("repo")
    if not owner or not repo:
        repository = cfg.get("repository", "")
        if isinstance(repository, str) and "/" in repository:
            owner, repo = repository.split("/", 1)
    if not owner or not repo:
        raise UpstreamError(f"{name}: не могу определить GitHub owner/repo по .url; укажите owner/repo")
    cur_tag = (parsed or {}).get("tag", "")
    kind = cfg.get("type", "github")
    use_tags = kind == "github-tag"

    if use_tags:
        candidates = _github_tag_candidates(owner, repo, cur_tag, cur_version, cfg)
        version, tag, _ = _select_highest(candidates, cfg, f"{owner}/{repo} tags")
        release = {}
    elif cfg.get("channel") is not None or cfg.get("include_prerelease"):
        candidates = _github_releases(owner, repo, cur_tag, cur_version, cfg)
        if not candidates and parsed and parsed["kind"].startswith("archive") and cfg.get("fallback_to_tags", True):
            candidates = _github_tag_candidates(owner, repo, cur_tag, cur_version, cfg)
            version, tag, _ = _select_highest(candidates, cfg, f"{owner}/{repo} tags")
            release = {}
            use_tags = True
        else:
            version, tag, release = _select_highest(candidates, cfg, f"{owner}/{repo} releases")
    else:
        api_url = f"{GITHUB_API}/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}/releases/latest"
        try:
            release = http_get(api_url, as_json=True)
        except urllib.error.HTTPError as error:
            if error.code == 404 and parsed and parsed["kind"].startswith("archive") and cfg.get("fallback_to_tags", True):
                candidates = _github_tag_candidates(owner, repo, cur_tag, cur_version, cfg)
                version, tag, _ = _select_highest(candidates, cfg, f"{owner}/{repo} tags")
                release = {}
                use_tags = True
            else:
                detail = "нет GitHub Releases" if error.code == 404 else f"HTTP {error.code}"
                raise UpstreamError(f"{owner}/{repo}: {detail}") from error
        except urllib.error.URLError as error:
            raise UpstreamError(f"{owner}/{repo}: сеть недоступна ({error.reason})") from error
        else:
            tag = release.get("tag_name")
            if not tag:
                raise UpstreamError(f"{owner}/{repo}: в ответе GitHub release нет tag_name")
            if release.get("draft") or (release.get("prerelease") and not cfg.get("include_prerelease", False)):
                raise UpstreamError(f"{owner}/{repo}: latest release {tag} недоступен для стабильного канала")
            version = _format_version(tag, cfg, cur_tag, cur_version)
            if not version:
                raise UpstreamError(f"{owner}/{repo}: не удалось извлечь версию из тега {tag!r}")
            if not channel_matches(version, cfg.get("channel")):
                raise UpstreamError(f"{owner}/{repo}: latest release {version} не подходит под channel={cfg['channel']!r}; используйте канал с выбором релиза")

    if cfg.get("url_template"):
        new_url = format_url_template(
            cfg["url_template"],
            _template_fields(name=name, version=version, tag=tag, cur_version=cur_version,
                             cur_tag=cur_tag, cfg=cfg, base=(parsed or {}).get("base", "")),
        )
    elif parsed and parsed["kind"].startswith("archive"):
        new_url = _github_archive_url(parsed, tag)
    elif parsed and parsed["kind"] == "release-asset" and not use_tags:
        new_url = _github_asset_url(parsed, release, cfg, name, cur_version, version, tag)
    else:
        raise UpstreamError(f"{name}: для этого GitHub URL нужен url_template в update.json")
    return version, new_url


def _path_value(value: Any, path: str | None) -> Any:
    if not path or path == "$":
        return value
    value = value
    cleaned = path[2:] if path.startswith("$.") else path[1:] if path.startswith("$") else path
    tokens = re.findall(r"[^.\[\]]+|\[(\d+)\]", cleaned)
    if not tokens:
        return value
    # Regex alternation returns empty strings for unmatched groups; reparse
    # using a compact scanner to preserve both properties and list indices.
    tokens2 = re.findall(r"([^.[\]]+)|\[(\d+)\]", cleaned)
    for key, index in tokens2:
        try:
            value = value[int(index)] if index else value[key]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return value


def _version_from_value(value: Any, cfg: dict, current_tag: str | None = None, current_version: str = "") -> str | None:
    if value is None:
        return None
    text = str(value)
    pattern = cfg.get("version_pattern")
    if pattern:
        match = re.search(pattern, text)
        if not match:
            return None
        groups = match.groupdict()
        result = groups.get("version") or (match.group(1) if match.groups() else match.group(0))
        template = cfg.get("version_template")
        if template:
            fields = {str(i): value for i, value in enumerate(match.groups(), 1)}
            fields.update({key: value for key, value in groups.items() if value is not None})
            fields["version"] = result
            try:
                return str(template).format_map(fields)
            except (KeyError, ValueError) as error:
                raise UpstreamError(f"ошибка version_template: {error}") from error
        return result
    return version_for_tag(text, current_tag, current_version, cfg)


def _candidate_url(cfg: dict, name: str, version: str, tag: str, cur_version: str, cur_url: str,
                   cur_tag: str = "", item: Any = None, direct_url: str | None = None) -> str:
    if direct_url:
        return validate_url(urllib.parse.urljoin(cfg.get("index", cur_url), str(direct_url)))
    if cfg.get("url_template"):
        return format_url_template(
            cfg["url_template"],
            _template_fields(name=name, version=version, tag=tag, cur_version=cur_version,
                             cur_tag=cur_tag, cfg=cfg, url=cur_url),
        )
    parsed = parse_github_url(cur_url)
    if parsed and parsed["kind"].startswith("archive"):
        return _github_archive_url(parsed, tag)
    return validate_url(_replace_current_version(cur_url, cur_version, version, cur_tag, tag))


def resolve_listing(cfg: dict, name: str, cur_version: str, cur_url: str):
    index = cfg.get("index")
    pattern = cfg.get("pattern") or cfg.get("version_pattern")
    if not index or not pattern:
        raise UpstreamError("update.json: для type=listing нужны 'index' и 'pattern'")
    try:
        regex = re.compile(pattern, re.I if cfg.get("ignore_case") else 0)
        listing = http_get(validate_url(index), timeout=int(cfg.get("timeout", TIMEOUT)))
    except (re.error, ValueError) as error:
        raise UpstreamError(f"невалидная конфигурация listing: {error}") from error
    except Exception as error:
        raise UpstreamError(f"не удалось скачать индекс {index}: {error}") from error

    candidates = []
    for match in regex.finditer(listing):
        groups = match.groupdict()
        version = groups.get("version") or (match.group(1) if match.groups() else match.group(0))
        if not version or (is_prerelease(version) and not cfg.get("include_prerelease", False)):
            continue
        if not channel_matches(version, cfg.get("channel")):
            continue
        candidates.append((version, version, match.group(0)))
    if not candidates:
        raise UpstreamError(f"в {index} не найдено стабильных версий по шаблону {pattern!r}")
    version, tag, _ = _select_highest(candidates, cfg, index)
    new_url = _candidate_url(cfg, name, version, tag, cur_version, cur_url)
    return version, new_url


def resolve_json(cfg: dict, name: str, cur_version: str, cur_url: str):
    index = cfg.get("index")
    version_path = cfg.get("version_path", "version")
    if not index or not version_path:
        raise UpstreamError("update.json: для type=json нужны 'index' и 'version_path'")
    try:
        document = http_get(validate_url(index), as_json=True, timeout=int(cfg.get("timeout", TIMEOUT)))
    except Exception as error:
        raise UpstreamError(f"не удалось получить JSON {index}: {error}") from error
    items = _path_value(document, cfg.get("items_path")) if cfg.get("items_path") else document
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise UpstreamError(f"{index}: items_path должен указывать на список или объект")
    candidates = []
    for item in items:
        raw_version = _path_value(item, version_path)
        version = _version_from_value(raw_version, cfg, None, cur_version)
        if not version:
            continue
        tag = str(_path_value(item, cfg.get("tag_path")) or raw_version)
        if _candidate_is_prerelease(version, item if isinstance(item, dict) else {}, cfg):
            continue
        if not channel_matches(version, cfg.get("channel")):
            continue
        direct_url = _path_value(item, cfg.get("url_path")) if cfg.get("url_path") else None
        candidates.append((version, tag, (item, direct_url)))
    version, tag, payload = _select_highest(candidates, cfg, index)
    item, direct_url = payload
    new_url = _candidate_url(cfg, name, version, tag, cur_version, cur_url, item=item, direct_url=direct_url)
    return version, new_url


def _xml_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def resolve_feed(cfg: dict, name: str, cur_version: str, cur_url: str):
    feed_url = cfg.get("index") or cfg.get("feed")
    if not feed_url:
        raise UpstreamError("update.json: для type=feed нужен 'index' или 'feed'")
    try:
        body = http_get(validate_url(feed_url), timeout=int(cfg.get("timeout", TIMEOUT)))
        root = ET.fromstring(body)
    except Exception as error:
        raise UpstreamError(f"не удалось разобрать feed {feed_url}: {error}") from error
    pattern = cfg.get("version_pattern")
    if not pattern:
        raise UpstreamError("update.json: для type=feed укажите version_pattern")
    try:
        regex = re.compile(pattern)
    except re.error as error:
        raise UpstreamError(f"невалидный version_pattern: {error}") from error
    candidates = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1].lower() not in ("item", "entry"):
            continue
        title = _xml_text(next((child for child in item if child.tag.rsplit("}", 1)[-1].lower() == "title"), None))
        link_element = next((child for child in item if child.tag.rsplit("}", 1)[-1].lower() == "link"), None)
        link = ""
        if link_element is not None:
            link = link_element.attrib.get("href") or _xml_text(link_element)
        match = regex.search(title) or regex.search(link)
        if not match:
            continue
        version = match.groupdict().get("version") or (match.group(1) if match.groups() else match.group(0))
        if is_prerelease(version) and not cfg.get("include_prerelease", False):
            continue
        if not channel_matches(version, cfg.get("channel")):
            continue
        candidates.append((version, version, (title, link)))
    version, tag, payload = _select_highest(candidates, cfg, feed_url)
    _, link = payload
    direct_url = link if cfg.get("use_feed_link", False) else None
    new_url = _candidate_url(cfg, name, version, tag, cur_version, cur_url, direct_url=direct_url)
    return version, new_url


# --------------------------------------------------------------------------- #
# GitLab support
# --------------------------------------------------------------------------- #


def _gitlab_project_from_url(url: str) -> tuple[str, str] | None:
    parts = urllib.parse.urlsplit(url)
    path = parts.path.strip("/")
    if "/-/archive/" not in path:
        return None
    project, _ = path.split("/-/archive/", 1)
    archive = path.split("/-/archive/", 1)[1]
    tag = archive.split("/", 1)[0]
    return project, urllib.parse.unquote(tag)


def resolve_gitlab(cur_version: str, cur_url: str, cfg: dict, name: str):
    parsed = _gitlab_project_from_url(cur_url)
    project = cfg.get("project") or (parsed[0] if parsed else None)
    if not project:
        raise UpstreamError(f"{name}: укажите GitLab project в update.json")
    base_url = validate_url(str(cfg.get("api_url", "https://gitlab.com/api/v4")).rstrip("/"), allow_ftp=False)
    project_id = urllib.parse.quote(str(project), safe="")
    cur_tag = parsed[1] if parsed else ""
    kind = cfg.get("type")
    use_tags = kind == "gitlab-tag" or cfg.get("channel") is not None
    endpoint = "repository/tags" if use_tags else "releases"
    request_timeout = int(cfg.get("timeout", TIMEOUT))

    def fetch_pages(path: str) -> list[dict]:
        result = []
        for page in range(1, MAX_API_PAGES + 1):
            url = f"{base_url}/projects/{project_id}/{path}?per_page=100&page={page}"
            payload = http_get(validate_url(url), as_json=True, timeout=request_timeout)
            if not isinstance(payload, list):
                raise UpstreamError(f"GitLab API returned an unexpected response for {project}")
            result.extend(payload)
            if len(payload) < 100:
                break
        return result

    try:
        items = fetch_pages(endpoint)
    except Exception as error:
        if not use_tags and cfg.get("fallback_to_tags", True):
            try:
                items = fetch_pages("repository/tags")
                use_tags = True
            except Exception as second:
                raise UpstreamError(f"GitLab API error for {project}: {second}") from second
        else:
            raise UpstreamError(f"GitLab API error for {project}: {error}") from error
    candidates = []
    for item in items:
        tag = item.get("name") or item.get("tag_name")
        if not tag or item.get("upcoming_release"):
            continue
        if item.get("released_at") is None and not use_tags and not cfg.get("include_unreleased", False):
            continue
        version = version_for_tag(tag, cur_tag, cur_version, cfg)
        if version:
            candidates.append((version, tag, item))
    if not candidates and not use_tags and cfg.get("fallback_to_tags", True):
        try:
            items = fetch_pages("repository/tags")
            use_tags = True
            for item in items:
                tag = item.get("name") or item.get("tag_name")
                if not tag:
                    continue
                version = version_for_tag(tag, cur_tag, cur_version, cfg)
                if version:
                    candidates.append((version, tag, item))
        except Exception as error:
            raise UpstreamError(f"GitLab tags fallback failed for {project}: {error}") from error
    version, tag, payload = _select_highest(candidates, cfg, f"GitLab {project}")
    if cfg.get("url_template"):
        new_url = format_url_template(
            cfg["url_template"],
            _template_fields(name=name, version=version, tag=tag, cur_version=cur_version,
                             cur_tag=cur_tag, cfg=cfg, project=str(project), base=base_url),
        )
    else:
        new_url = validate_url(_replace_current_version(cur_url, cur_version, version, cur_tag, tag))
    return version, new_url


# --------------------------------------------------------------------------- #
# Update orchestration
# --------------------------------------------------------------------------- #


def load_update_config(pkg_dir: str) -> dict:
    path = os.path.join(pkg_dir, "update.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as stream:
            cfg = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise UpstreamError(f"update.json повреждён: {error}") from error
    if not isinstance(cfg, dict):
        raise UpstreamError("update.json должен содержать JSON-объект")
    return cfg


def validate_config(cfg: dict, package: str = "package") -> None:
    kind = cfg.get("type", "auto")
    supported = {"auto", "github", "github-release", "github-tag", "gitlab", "gitlab-tag", "listing", "regex", "json", "feed", "rss", "none"}
    if kind not in supported:
        raise UpstreamError(f"{package}: неизвестный type={kind!r}")
    if kind in ("listing", "regex"):
        if not cfg.get("index") or not (cfg.get("pattern") or cfg.get("version_pattern")):
            raise UpstreamError(f"{package}: listing требует index и pattern/version_pattern")
        re.compile(cfg.get("pattern") or cfg.get("version_pattern"))
    if kind == "json" and (not cfg.get("index") or not cfg.get("version_path")):
        raise UpstreamError(f"{package}: json требует index и version_path")
    if kind in ("feed", "rss") and not (cfg.get("index") or cfg.get("feed")):
        raise UpstreamError(f"{package}: feed требует index/feed")
    for key in ("version_pattern", "tag_pattern", "asset_pattern"):
        if cfg.get(key):
            re.compile(cfg[key])
    for key in ("index", "feed"):
        if cfg.get(key):
            validate_url(cfg[key])
    if cfg.get("api_url"):
        validate_url(cfg["api_url"], allow_ftp=False)


def resolve_update(cfg: dict, name: str, cur_version: str, cur_url: str):
    validate_config(cfg, name)
    kind = cfg.get("type", "auto")
    if kind in ("auto", "github", "github-release", "github-tag"):
        return resolve_github(cur_version, cur_url, cfg, name)
    if kind in ("gitlab", "gitlab-tag"):
        return resolve_gitlab(cur_version, cur_url, cfg, name)
    if kind in ("listing", "regex"):
        return resolve_listing(cfg, name, cur_version, cur_url)
    if kind == "json":
        return resolve_json(cfg, name, cur_version, cur_url)
    if kind in ("feed", "rss"):
        return resolve_feed(cfg, name, cur_version, cur_url)
    if kind == "none":
        raise UpstreamError("автообновление отключено")
    raise UpstreamError(f"неизвестный источник обновлений: {kind}")


def check_package(pkg_dir: str, name: str, dry_run: bool):
    """Return (status, message); status is updated|same|skip|error."""
    zon_path = os.path.join(pkg_dir, f"{name}.zon")
    if not os.path.exists(zon_path):
        return "skip", f"нет файла {name}.zon"
    try:
        content = read_text(zon_path)
        cur_version = extract_field(content, "version")
        cur_url = extract_field(content, "url")
        if not cur_version or not cur_url:
            return "skip", "не найдены .version или .url"
        cfg = load_update_config(pkg_dir)
        if cfg.get("type", "auto") == "none":
            return "skip", "обновления отключены (update.json)"
        new_version, new_url = resolve_update(cfg, name, cur_version, cur_url)
        if new_version == cur_version and new_url == cur_url:
            return "same", f"{cur_version} -- актуально"
        if version_key(new_version) < version_key(cur_version) and not cfg.get("allow_downgrade", False):
            return "skip", f"найден откат {cur_version} -> {new_version}; не применяю (allow_downgrade=false)"
        new_hash = compute_sha256(new_url, max_bytes=int(cfg.get("max_download_bytes", MAX_HASH_BYTES)),
                                  timeout=int(cfg.get("timeout", TIMEOUT)))
        direction = "обновление" if version_key(new_version) >= version_key(cur_version) else "ОТКАТ"
        print(f"    {direction}: {cur_version} -> {new_version}")
        new_content = set_field(content, "version", new_version)
        new_content = set_field(new_content, "url", new_url)
        new_content = set_field(new_content, "hash", new_hash)
        if new_content == content:
            return "same", f"{cur_version} -- актуально"
        if dry_run:
            print(f"    [dry-run] {zon_path} не записан (хэш: {new_hash})")
        else:
            write_text_atomic(zon_path, new_content)
        return "updated", f"{cur_version} -> {new_version}"
    except UpstreamError as error:
        return "error", str(error)
    except (urllib.error.URLError, urllib.error.HTTPError) as error:
        return "error", f"сетевая ошибка: {error}"
    except Exception as error:
        return "error", f"непредвиденная ошибка: {error}"


def validate_packages(pkgs_dir: str) -> list[str]:
    errors = []
    for name in sorted(os.listdir(pkgs_dir)):
        pkg_dir = os.path.join(pkgs_dir, name)
        if not os.path.isdir(pkg_dir) or name.startswith("."):
            continue
        try:
            cfg = load_update_config(pkg_dir)
            validate_config(cfg, name)
            content = read_text(os.path.join(pkg_dir, f"{name}.zon"))
            for field in ("name", "version", "url", "hash"):
                if extract_field(content, field) is None:
                    raise UpstreamError(f"нет .{field} в {name}.zon")
        except Exception as error:
            errors.append(f"{name}: {error}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Надёжное обновление версий пакетов zp")
    parser.add_argument("--pkgs-dir", default="pkgs", help="каталог с пакетами")
    parser.add_argument("--dry-run", action="store_true", help="не изменять .zon")
    parser.add_argument("--strict", action="store_true", help="вернуть ненулевой код, если есть ошибки")
    parser.add_argument("--validate-configs", action="store_true", help="проверить package/update.json без сети")
    parser.add_argument("--package", action="append", default=[], help="обновить только этот пакет; можно указать несколько раз")
    args = parser.parse_args()

    if not os.path.isdir(args.pkgs_dir):
        print(f"Директория {args.pkgs_dir} не найдена", file=sys.stderr)
        return 1
    if args.validate_configs:
        errors = validate_packages(args.pkgs_dir)
        if errors:
            print("Ошибки конфигураций:")
            for error in errors:
                print(f"  ! {error}")
            return 1
        print("Конфигурации пакетов корректны")
        return 0

    selected = set(args.package)
    print("Запуск zp update bot...")
    if args.dry_run:
        print("(dry-run: файлы не изменяются)")
    stats = {"updated": [], "same": 0, "skip": [], "error": []}
    for name in sorted(os.listdir(args.pkgs_dir)):
        pkg_dir = os.path.join(args.pkgs_dir, name)
        if not os.path.isdir(pkg_dir) or name.startswith(".") or (selected and name not in selected):
            continue
        print(f"\n[{name}]")
        status, message = check_package(pkg_dir, name, args.dry_run)
        print(f"    -> {message}")
        if status == "updated":
            stats["updated"].append(f"{name}: {message}")
        elif status == "same":
            stats["same"] += 1
        elif status == "error":
            stats["error"].append(f"{name}: {message}")
        else:
            stats["skip"].append(f"{name}: {message}")

    print("\n" + "=" * 60)
    print(f"Итог: обновлено {len(stats['updated'])}, актуально {stats['same']}, "
          f"пропущено {len(stats['skip'])}, с ошибками {len(stats['error'])}")
    if stats["updated"]:
        print("\nОбновления:")
        for line in stats["updated"]:
            print(f"  * {line}")
    if stats["error"]:
        print("\nОшибки:")
        for line in stats["error"]:
            print(f"  ! {line}")
    return 1 if args.strict and stats["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
