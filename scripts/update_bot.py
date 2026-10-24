#!/usr/bin/env python3
"""
zp update bot.

Обходит pkgs/*/*.zon, находит последнюю версию апстрима и обновляет .version и .url.

Источники:
  github   -- определяется автоматически по .url (GitHub Releases)
  listing  -- включается файлом pkgs/<pkg>/update.json
  none     -- пакет явно пропускается (update.json: {"type": "none"})

Запуск:  python3 scripts/update_bot.py [--dry-run]
"""

import hashlib

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

USER_AGENT = "zp-update-bot/1.0"
TIMEOUT = 30

GITHUB_RELEASE_URL_RE = re.compile(
    r"^(?P<base>https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/download/)"
    r"(?P<tag>[^/]+)(?P<asset>/.*)$"
)

def compute_sha256(url: str) -> str:
    """Качает файл по частям и возвращает sha256 hexdigest.

    Бросает исключение, если файл недоступен или сервер вернул ошибку --
    это важно, чтобы не записать хэш от битой/несуществующей ссылки.
    """
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        while True:
            chunk = resp.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def set_field(content: str, field: str, value: str) -> str:
    """Обновляет поле, а если его нет -- вставляет перед последней '}'."""
    if re.search(rf'\.{field}\s*=\s*"[^"]*"', content):
        return update_field(content, field, value)

    idx = content.rfind("}")
    if idx == -1:
        return content

    insert = f'    .{field} = "{value}",\n'
    return content[:idx] + insert + content[idx:]

class UpstreamError(Exception):
    """Не удалось получить данные об апстриме. Не роняем весь прогон."""




# --------------------------------------------------------------------------- #
#  Работа с .zon
# --------------------------------------------------------------------------- #

def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def extract_field(content: str, field: str):
    m = re.search(rf'\.{field}\s*=\s*"([^"]*)"', content)
    return m.group(1) if m else None


def update_field(content: str, field: str, value: str) -> str:
    # замена через lambda: value не интерпретируется как шаблон \g<...>
    return re.sub(
        rf'(\.{field}\s*=\s*")([^"]*)(")',
        lambda m: m.group(1) + value + m.group(3),
        content,
        count=1,
    )


def load_update_config(pkg_dir: str) -> dict:
    path = os.path.join(pkg_dir, "update.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except json.JSONDecodeError as e:
        print(f"    update.json повреждён ({e}), игнорирую")
        return {}


# --------------------------------------------------------------------------- #
#  HTTP / версии
# --------------------------------------------------------------------------- #

def http_get(url: str, *, as_json: bool = False):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json" if as_json else "*/*",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if as_json else raw


def version_key(v: str):
    """'3.7c' -> ((1,3,''),(1,7,''),(0,0,'c')) -- для корректной сортировки."""
    return tuple(
        (1, int(p), "") if p.isdigit() else (0, 0, p)
        for p in re.findall(r"\d+|[A-Za-z]+", v)
    )


def tag_affixes(cur_tag: str, cur_version: str):
    """
    Ищет prefix/suffix такие, что cur_tag == prefix + cur_version + suffix.

      ('jq-1.8.2',               '1.8.2')       -> ('jq-', '')
      ('release-2.1.13-stable',  '2.1.13-stable')-> ('release-', '')
      ('v1.3.2',                 '1.3.2')       -> ('v', '')
      ('3.5.3',                  '3.5.3')       -> ('', '')
    """
    if cur_tag == cur_version:
        return "", ""
    if cur_tag.endswith(cur_version):
        return cur_tag[: -len(cur_version)], ""
    if cur_version and cur_tag.startswith(cur_version):
        return "", cur_tag[len(cur_version):]
    return None, None


# --------------------------------------------------------------------------- #
#  Источники обновлений
# --------------------------------------------------------------------------- #

def latest_github_tag(owner: str, repo: str) -> str:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        data = http_get(url, as_json=True)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpstreamError(f"{owner}/{repo}: нет GitHub Releases (404)")
        if e.code in (403, 429):
            raise UpstreamError(f"{owner}/{repo}: rate limit (HTTP {e.code})")
        raise UpstreamError(f"{owner}/{repo}: HTTP {e.code}")
    except urllib.error.URLError as e:
        raise UpstreamError(f"{owner}/{repo}: сеть недоступна ({e.reason})")

    tag = data.get("tag_name")
    if not tag:
        raise UpstreamError(f"{owner}/{repo}: в ответе нет tag_name")
    if data.get("draft") or data.get("prerelease"):
        raise UpstreamError(f"{owner}/{repo}: {tag} -- draft/prerelease, пропускаю")
    return tag


def resolve_github(cur_version: str, cur_url: str):
    m = GITHUB_RELEASE_URL_RE.match(cur_url)
    if not m:
        raise UpstreamError(f"URL не похож на GitHub Releases: {cur_url}")

    owner, repo = m.group("owner"), m.group("repo")
    cur_tag = m.group("tag")

    new_tag = latest_github_tag(owner, repo)

    prefix, suffix = tag_affixes(cur_tag, cur_version)
    if prefix is None:
        raise UpstreamError(
            f"{owner}/{repo}: не могу связать тег '{cur_tag}' с версией '{cur_version}'"
        )

    if (
        new_tag.startswith(prefix)
        and new_tag.endswith(suffix)
        and len(new_tag) > len(prefix) + len(suffix)
    ):
        end = len(new_tag) - len(suffix)
        new_version = new_tag[len(prefix):end]
    else:
        print(f"    внимание: тег '{new_tag}' не подходит под шаблон '{prefix}<v>{suffix}'")
        new_version = new_tag

    # пересобираем URL: сначала тег в пути, потом версия в имени архива
    asset = m.group("asset")
    if cur_tag in asset:
        asset = asset.replace(cur_tag, new_tag)
    if cur_version in asset:
        asset = asset.replace(cur_version, new_version)

    return new_version, m.group("base") + new_tag + asset


def resolve_listing(cfg: dict, name: str, cur_version: str, cur_url: str):
    index = cfg.get("index")
    pattern = cfg.get("pattern")
    if not index or not pattern:
        raise UpstreamError("update.json: для type=listing нужны 'index' и 'pattern'")

    try:
        listing = http_get(index)
    except Exception as e:
        raise UpstreamError(f"не удалось скачать индекс {index}: {e}")

    found = {m.group(1) for m in re.finditer(pattern, listing)}
    if not found:
        raise UpstreamError(f"в {index} ничего не найдено по шаблону {pattern!r}")

    new_version = max(found, key=version_key)

    template = cfg.get("url_template")
    if template:
        parts = new_version.split(".")
        new_url = template.format(
            name=name,
            version=new_version,
            major=parts[0] if parts else "",
            minor=parts[1] if len(parts) > 1 else "",
        )
    else:
        new_url = cur_url.replace(cur_version, new_version)

    return new_version, new_url


# --------------------------------------------------------------------------- #
#  Основной цикл
# --------------------------------------------------------------------------- #

def check_package(pkg_dir: str, name: str, dry_run: bool):
    """Возвращает (status, message). status: updated|same|skip|error"""
    zon_path = os.path.join(pkg_dir, f"{name}.zon")
    if not os.path.exists(zon_path):
        return "skip", f"нет файла {name}.zon"

    content = read_text(zon_path)
    cur_version = extract_field(content, "version")
    cur_url = extract_field(content, "url")
    if not cur_version or not cur_url:
        return "skip", "не найдены .version или .url"

    cfg = load_update_config(pkg_dir)
    kind = cfg.get("type", "auto")

    if kind == "none":
        return "skip", "обновления отключены (update.json)"

        # Скачиваем новый архив и считаем его хэш.
    # Если скачать не удалось (404, нет ассета у релиза и т.п.) --
    # НЕ обновляем, чтобы не записать битый .url/.hash.
    try:
        new_hash = compute_sha256(new_url)
    except Exception as e:
        return "error", f"не удалось скачать {new_url}: {e}"

    new_content = set_field(content, "version", new_version)
    if new_url and new_url != cur_url:
        new_content = set_field(new_content, "url", new_url)
    new_content = set_field(new_content, "hash", new_hash)

    if new_content == content:
        return "skip", "нечего менять"

    if dry_run:
        print(f"    [dry-run] {zon_path} не записан (хэш: {new_hash})")
    else:
        write_text(zon_path, new_content)

    return "updated", f"{cur_version} -> {new_version}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Обновление версий пакетов zp")
    parser.add_argument("--pkgs-dir", default="pkgs")
    parser.add_argument("--dry-run", action="store_true", help="не записывать файлы")
    args = parser.parse_args()

    if not os.path.isdir(args.pkgs_dir):
        print(f"Директория {args.pkgs_dir} не найдена", file=sys.stderr)
        return 1

    print("Запуск бота обновления пакетов zp...")
    if args.dry_run:
        print("(режим dry-run -- файлы не изменяются)")

    stats = {"updated": [], "same": 0, "skip": [], "error": []}

    for name in sorted(os.listdir(args.pkgs_dir)):
        pkg_dir = os.path.join(args.pkgs_dir, name)
        if not os.path.isdir(pkg_dir) or name.startswith("."):
            continue

        print(f"\n[{name}]")
        status, msg = check_package(pkg_dir, name, args.dry_run)
        print(f"    -> {msg}")

        if status == "updated":
            stats["updated"].append(f"{name}: {msg}")
        elif status == "same":
            stats["same"] += 1
        elif status == "error":
            stats["error"].append(f"{name}: {msg}")
        else:
            stats["skip"].append(f"{name}: {msg}")

    print("\n" + "=" * 60)
    print(f"Итог: обновлено {len(stats['updated'])}, "
          f"актуально {stats['same']}, "
          f"пропущено {len(stats['skip'])}, "
          f"с ошибками {len(stats['error'])}")

    if stats["updated"]:
        print("\nОбновления:")
        for line in stats["updated"]:
            print(f"  * {line}")
    if stats["error"]:
        print("\nОшибки (требуют внимания):")
        for line in stats["error"]:
            print(f"  ! {line}")

    # не роняем workflow из-за проблем апстрима -- иначе PR не создастся вовсе
    return 0


if __name__ == "__main__":
    sys.exit(main())
