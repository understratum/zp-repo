#!/usr/bin/env python3
# scripts/fix_update_json.py — перезаписывает update.json для всех listing-пакетов.
# Идемпотентен, версии не трогает (в update.json версий нет).
import json, pathlib

LISTING = {
    # партия 1
    "less":      ("https://www.greenwoodsoftware.com/less/download.html",
                  r"less-(\d+)\.tar\.gz",
                  "https://www.greenwoodsoftware.com/less/{name}-{version}.tar.gz"),
    "lua":       ("https://www.lua.org/ftp/",
                  r"lua-([0-9.]+)\.tar\.gz",
                  "https://www.lua.org/ftp/{name}-{version}.tar.gz"),
    "gzip":      ("https://ftp.gnu.org/gnu/gzip/",
                  r"gzip-([0-9.]+)\.tar\.gz",
                  "https://ftp.gnu.org/gnu/gzip/{name}-{version}.tar.gz"),
    "patch":     ("https://ftp.gnu.org/gnu/patch/",
                  r"patch-([0-9.]+)\.tar\.xz",
                  "https://ftp.gnu.org/gnu/patch/{name}-{version}.tar.xz"),
    "sed":       ("https://ftp.gnu.org/gnu/sed/",
                  r"sed-([0-9.]+)\.tar\.gz",
                  "https://ftp.gnu.org/gnu/sed/{name}-{version}.tar.gz"),
    "findutils": ("https://ftp.gnu.org/gnu/findutils/",
                  r"findutils-([0-9.]+)\.tar\.xz",
                  "https://ftp.gnu.org/gnu/findutils/{name}-{version}.tar.xz"),
    "grep":      ("https://ftp.gnu.org/gnu/grep/",
                  r"grep-([0-9.]+)\.tar\.xz",
                  "https://ftp.gnu.org/gnu/grep/{name}-{version}.tar.xz"),
    "ncurses":   ("https://ftp.gnu.org/gnu/ncurses/",
                  r"ncurses-([0-9.]+)\.tar\.gz",
                  "https://ftp.gnu.org/gnu/ncurses/{name}-{version}.tar.gz"),
    # nano: каталог dist/v9/ привязан к мажору, см. примечание ниже
    "nano":      ("https://www.nano-editor.org/dist/v9/",
                  r"nano-([0-9.]+)\.tar\.xz",
                  "https://www.nano-editor.org/dist/v9/{name}-{version}.tar.xz"),
    # партия 2
    "readline":  ("https://ftp.gnu.org/gnu/readline/",
                  r"readline-([0-9.]+)\.tar\.gz",
                  "https://ftp.gnu.org/gnu/readline/{name}-{version}.tar.gz"),
    "bash":      ("https://ftp.gnu.org/gnu/bash/",
                  r"bash-([0-9.]+)\.tar\.gz",
                  "https://ftp.gnu.org/gnu/bash/{name}-{version}.tar.gz"),
    "diffutils": ("https://ftp.gnu.org/gnu/diffutils/",
                  r"diffutils-([0-9.]+)\.tar\.xz",
                  "https://ftp.gnu.org/gnu/diffutils/{name}-{version}.tar.xz"),
    "tar":       ("https://ftp.gnu.org/gnu/tar/",
                  r"tar-([0-9.]+)\.tar\.gz",
                  "https://ftp.gnu.org/gnu/tar/{name}-{version}.tar.gz"),
    "make":      ("https://ftp.gnu.org/gnu/make/",
                  r"make-([0-9.]+)\.tar\.gz",
                  "https://ftp.gnu.org/gnu/make/{name}-{version}.tar.gz"),
    "m4":        ("https://ftp.gnu.org/gnu/m4/",
                  r"m4-([0-9.]+)\.tar\.gz",
                  "https://ftp.gnu.org/gnu/m4/{name}-{version}.tar.gz"),
    "ed":        ("https://ftp.gnu.org/gnu/ed/",
                  r"ed-([0-9.]+)\.tar\.lz",
                  "https://ftp.gnu.org/gnu/ed/{name}-{version}.tar.lz"),
    # партия 3
    "lzip":      ("https://download.savannah.gnu.org/releases/lzip/",
                  r"lzip-([0-9.]+)\.tar\.gz",
                  "https://download.savannah.gnu.org/releases/lzip/{name}-{version}.tar.gz"),
    "curl":      ("https://curl.se/download/",
                  r"curl-([0-9.]+)\.tar\.xz",
                  "https://curl.se/download/{name}-{version}.tar.xz"),
    "gawk":      ("https://ftp.gnu.org/gnu/gawk/",
                  r"gawk-([0-9.]+)\.tar\.xz",
                  "https://ftp.gnu.org/gnu/gawk/{name}-{version}.tar.xz"),
    "libcap":    ("https://www.kernel.org/pub/linux/libs/security/linux-privs/libcap2/",
                  r"libcap-([0-9.]+)\.tar\.xz",
                  "https://www.kernel.org/pub/linux/libs/security/linux-privs/libcap2/{name}-{version}.tar.xz"),
    "which":     ("https://ftp.gnu.org/gnu/which/",
                  r"which-([0-9.]+)\.tar\.gz",
                  "https://ftp.gnu.org/gnu/which/{name}-{version}.tar.gz"),
}

root = pathlib.Path("pkgs")
for name, (index, pattern, tmpl) in LISTING.items():
    d = root / name
    if not d.is_dir():
        print(f"[{name}] нет директории, пропуск")
        continue
    cfg = {"type": "listing", "index": index, "pattern": pattern, "url_template": tmpl}
    (d / "update.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"[{name}] записан")

# те, что не обновляются листингом
for name in ("bzip2", "sqlite"):
    d = root / name
    if d.is_dir():
        (d / "update.json").write_text(json.dumps({"type": "none"}, indent=2) + "\n", encoding="utf-8")
        print(f"[{name}] type=none")

# openssl: пин канала, см. раздел 4
op = root / "openssl"
if op.is_dir():
    (op / "update.json").write_text(
        json.dumps({"type": "github", "channel": "3.5"}, indent=2) + "\n", encoding="utf-8")
    print("[openssl] channel=3.5")
