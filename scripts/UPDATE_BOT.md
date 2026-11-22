# zp update bot

`scripts/update_bot.py` updates the `.version`, `.url`, and `.hash` fields in each
`pkgs/<name>/<name>.zon`. Upstream discovery is deliberately configured per
package: release tags, source archives, and non-GitHub projects do not share a
reliable universal convention. A package may opt out with `{"type":"none"}`.

## Safety and behavior

- Resolve a candidate source and version first; compute SHA-256 from the actual
  new URL before changing metadata.
- Write updates atomically. `--dry-run` never writes package files.
- Reject unsupported URL schemes, embedded URL credentials, empty downloads,
  oversized metadata/downloads, and version downgrades by default.
- Retry transient HTTP errors and URL failures. GitHub API requests use
  `GITHUB_TOKEN` or `GH_TOKEN` if set, to raise rate limits.
- Releases marked prerelease are excluded by default. `channel` filters versions
  on component boundaries (for example, `"1"` matches `1.9`, not `10.0`).
- This tool does not prove that a source archive builds or that a release is
  trustworthy. Review upstream identity, checksum/signature policy, and build
  changes before accepting an update.

## Commands

From the repository root:

```sh
# Validate all package metadata and update.json files, without network access
python3 scripts/update_bot.py --validate-configs

# Resolve candidates and calculate hashes, but do not write files
python3 scripts/update_bot.py --dry-run

# Restrict a dry run or update to one or more packages
python3 scripts/update_bot.py --dry-run --package ripgrep
python3 scripts/update_bot.py --package ripgrep --package jq

# Update all packages (review the diff afterwards)
python3 scripts/update_bot.py
```

`--strict` returns a non-zero exit status if any package has an error. The
normal run reports package-level failures and continues, which is useful for
partial mirror/network outages. Disabled and unconfigured packages are reported
as skipped; they are not treated as update failures.

## `update.json` reference

Each property is optional unless listed as required for its provider. JSON
regular expressions use JSON escaping, e.g. `"\\\\.tar\\\\.xz"` represents
`\.tar\.xz` to the regex engine. Unknown properties are ignored.

| Property | Meaning |
| --- | --- |
| `type` | `github` (default/`auto`), `github-tag`, `gitlab`, `gitlab-tag`, `listing`/`regex`, `json`, `feed`/`rss`, or `none`. |
| `owner`, `repo` | GitHub repository. Usually inferred from a GitHub source-archive or release-asset `.url`; set explicitly when it cannot be inferred. |
| `repository` | GitHub `owner/repo` shorthand. |
| `project` | GitLab project path or numeric project ID. Inferred from a GitLab `/-/archive/` source URL when possible. |
| `api_url` | GitLab API root; defaults to `https://gitlab.com/api/v4`. |
| `index` | Required listing/JSON endpoint; also accepted as a feed endpoint. |
| `feed` | Alternative feed endpoint. |
| `pattern` | Listing regex. First capture group, or named `version`, supplies the version. |
| `version_pattern` | Regex used to extract a version from tags, JSON values, or feed title/link; first capture group or named `version` is used. |
| `version_template` | Optional Python format template using named regex groups, numbered groups (`{1}`, `{2}`), or `{version}`. |
| `tag_pattern` | Optional regex restricting tag names before version parsing. |
| `items_path` | Optional dotted/bracket JSON path to a list, e.g. `data.releases` or `results[0].versions`. |
| `version_path` | Required JSON path to each version value. |
| `tag_path` | Optional JSON path to a tag/identifier used by templates. |
| `url_path` | Optional JSON path to a direct download URL for that item. Relative links resolve against `index`. |
| `url_template` | Explicit download URL template. Supported fields: `{name}`, `{version}`, `{tag}`, `{cur_version}`, `{cur_tag}`, `{major}`, `{minor}`, `{patch}`, `{asset}`, `{base}`, `{project}`, `{owner}`, `{repo}`, `{channel}`, `{url}`. |
| `use_feed_link` | For a feed, use the selected item's link as the download URL rather than constructing a URL from `url_template`/current `.url`. |
| `channel` | Optional version prefix/channel selector such as `"1"`, `"3.5"`, or `"2026.1"`. |
| `include_prerelease` | Include prereleases; default `false`. For GitHub release API selection, set this to inspect all releases rather than only `/releases/latest`. |
| `include_unreleased` | Include GitLab release entries without `released_at`; default `false`. |
| `fallback_to_tags` | For archive URLs, fall back to repository tags when a GitHub/GitLab Releases endpoint has no usable release; default `true`. |
| `asset_name` | For GitHub release assets, exact expected asset name (may use template fields). |
| `asset_pattern` | Optional regex to select a GitHub release asset when the exact name does not match. Exactly one result is required. |
| `allow_asset_url_fallback` | Reconstruct the current GitHub release-asset URL if the release API does not list a matching asset; default `true`. Set `false` to require an API-listed asset. |
| `allow_downgrade` | Permit a version downgrade; default `false`. Avoid enabling without a deliberate rollback policy. |
| `ignore_case` | Case-insensitive listing regex; default `false`. |
| `timeout` | Per-request timeout in seconds; default 30. |
| `max_download_bytes` | Maximum source download size for hashing; default 8 GiB. |

Dotted JSON paths support object keys and numeric list indexes, e.g. `release.tag`
or `assets[0].browser_download_url`. This is a small path syntax, not full
JSONPath.

### GitHub release assets

Use the current source archive URL where possible; archives can be constructed
from tags even when the project has no Releases:

```json
{ "type": "github" }
```

For a repository that publishes tags but no GitHub Releases, use
`"type":"github-tag"` or rely on the default tag fallback for a GitHub source
archive. For a release asset rather than a source archive, set `asset_name` or
`asset_pattern` if the asset name cannot be safely derived from the current
`.url`. Prefer a `url_template` for projects whose asset naming convention is
not stable.

```json
{
  "type": "github",
  "asset_name": "tool_{version}_linux_amd64.tar.gz"
}
```

### Non-GitHub source listing

```json
{
  "type": "listing",
  "index": "https://ftp.gnu.org/gnu/example/",
  "pattern": "example-([0-9.]+)\\.tar\\.xz",
  "url_template": "https://ftp.gnu.org/gnu/example/example-{version}.tar.xz"
}
```

The regex is searched throughout the fetched text/HTML. The URL template is
recommended when the source name or compression suffix differs from the current
URL.

### JSON API

```json
{
  "type": "json",
  "index": "https://example.org/api/releases",
  "items_path": "releases",
  "version_path": "tag_name",
  "url_path": "assets[0].download_url",
  "version_pattern": "^v?([0-9]+\\.[0-9]+\\.[0-9]+)$"
}
```

When `url_path` is absent, the bot uses `url_template` or replaces the current
version/tag in `.url`. Make sure the API's ordering and version policy are
covered by the fields and filters in the configuration.

### RSS/Atom feed

```json
{
  "type": "feed",
  "index": "https://example.org/releases.atom",
  "version_pattern": "Release ([0-9]+\\.[0-9]+\\.[0-9]+)",
  "url_template": "https://example.org/downloads/example-{version}.tar.gz"
}
```

Set `use_feed_link: true` only if each feed item's link is itself the desired
source archive/download URL. Otherwise the entry link is used only as version
text fallback and URL construction follows the normal template/current-URL rule.

### GitLab

```json
{
  "type": "gitlab",
  "project": "group/subgroup/project"
}
```

GitLab's API is queried for release metadata; `gitlab-tag` queries repository
tags directly. Set `api_url` for a self-hosted GitLab instance. A source archive
URL containing `/-/archive/` normally supplies both project path and current
tag. If the archive layout is custom, configure `url_template`.

## Operational notes

The updater performs live HTTP requests and source downloads; a full dry run may
take several minutes and is subject to upstream rate limits and network access.
`--validate-configs` is the fast offline check. After a real update, inspect
`git diff`, validate changed URLs/hashes through the repository's usual checks,
and build affected packages before merging. The bot never changes build recipes
or dependency declarations.
