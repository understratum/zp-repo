#!/usr/bin/env bash
# намеренно без -e: ошибки по пакетам собираются списком, а не обрывают партию
set -uo pipefail

# ./scripts/gen_batch3.sh              создать + sha256 + самопроверка
# ./scripts/gen_batch3.sh --force      перезаписать
# ./scripts/gen_batch3.sh --verify     сверить готовые .zon с сетью
# ./scripts/gen_batch3.sh --only bzip2
#
# URLS[name] — кандидаты через ПЕРЕНОС СТРОКИ или пробел, первый живой идёт в .url

pkgs=(lzip bzip2 curl libffi shadow gawk libcap snappy which sqlite)

declare -A VERSION=(
  [lzip]="1.25"     [bzip2]="1.0.8"    [curl]="8.23.0"   [libffi]="3.4.8"
  [shadow]="4.18.0" [gawk]="5.3.2"     [libcap]="2.75"   [snappy]="1.3.0"
  [which]="2.23"    [sqlite]="3.54.1"
)

declare -A LICENSE=(
  [lzip]="GPL-3.0-or-later"   [bzip2]="bzip2-1.0.6"   [curl]="curl"
  [libffi]="MIT"              [shadow]="BSD-3-Clause" [gawk]="GPL-3.0-or-later"
  [libcap]="GPL-2.0-or-later OR BSD-3-Clause"
  [snappy]="BSD-3-Clause"     [which]="GPL-3.0-or-later"
  [sqlite]="blessing"
)

# кандидаты: первый ответивший записывается в .url
declare -A URLS=(
  [lzip]="https://download.savannah.gnu.org/releases/lzip/lzip-1.25.tar.gz
         https://mirror.netcologne.de/savannah/lzip/lzip-1.25.tar.gz"

  [bzip2]="https://sourceware.org/pub/bzip2/bzip2-1.0.8.tar.gz
          https://deb.debian.org/debian/pool/main/b/bzip2/bzip2_1.0.8.orig.tar.gz
          https://fossies.org/linux/misc/bzip2-1.0.8.tar.gz
          https://repository.timesys.com/buildsources/b/bzip2/bzip2-1.0.8/bzip2-1.0.8.tar.gz"

  [curl]="https://curl.se/download/curl-8.23.0.tar.xz"
  [libffi]="https://github.com/libffi/libffi/releases/download/v3.4.8/libffi-3.4.8.tar.gz"
  [shadow]="https://github.com/shadow-maint/shadow/releases/download/4.18.0/shadow-4.18.0.tar.xz"
  [gawk]="https://ftp.gnu.org/gnu/gawk/gawk-5.3.2.tar.xz"
  [libcap]="https://www.kernel.org/pub/linux/libs/security/linux-privs/libcap2/libcap-2.75.tar.xz"
  [snappy]="https://github.com/google/snappy/archive/refs/tags/1.3.0.tar.gz"
  [which]="https://ftp.gnu.org/gnu/which/which-2.23.tar.gz"
  [sqlite]="https://www.sqlite.org/2026/sqlite-autoconf-3540100.tar.gz"
)

declare -A DEPS=(
  [lzip]=".{}"  [bzip2]=".{}"  [curl]='.{"zlib", "openssl"}'  [libffi]=".{}"
  [shadow]=".{}"  [gawk]=".{}"  [libcap]=".{}"  [snappy]=".{}"
  [which]=".{}"  [sqlite]='.{"readline"}'
)

declare -A INDEX=(
  [lzip]="https://download.savannah.gnu.org/releases/lzip/"
  [bzip2]="https://sourceware.org/pub/bzip2/"
  [curl]="https://curl.se/download/"
  [gawk]="https://ftp.gnu.org/gnu/gawk/"
  [libcap]="https://www.kernel.org/pub/linux/libs/security/linux-privs/libcap2/"
  [which]="https://ftp.gnu.org/gnu/which/"
)

declare -A PATTERN=(
  [lzip]='lzip-([0-9.]+)\.tar\.gz'
  [bzip2]='bzip2[-_]([0-9.]+)(\.orig)?\.tar\.gz'
  [curl]='curl-([0-9.]+)\.tar\.xz'
  [gawk]='gawk-([0-9.]+)\.tar\.xz'
  [libcap]='libcap-([0-9.]+)\.tar\.xz'
  [which]='which-([0-9.]+)\.tar\.gz'
)

declare -A TEMPLATE=(
  [lzip]="https://download.savannah.gnu.org/releases/lzip/{name}-{version}.tar.gz"
  [bzip2]="https://sourceware.org/pub/bzip2/{name}-{version}.tar.gz"
  [curl]="https://curl.se/download/{name}-{version}.tar.xz"
  [gawk]="https://ftp.gnu.org/gnu/gawk/{name}-{version}.tar.xz"
  [libcap]="https://www.kernel.org/pub/linux/libs/security/linux-privs/libcap2/{name}-{version}.tar.xz"
  [which]="https://ftp.gnu.org/gnu/which/{name}-{version}.tar.gz"
)

force=0 verify=0
declare -a only=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) force=1 ;;
    --verify) verify=1 ;;
    --only) shift; while [[ $# -gt 0 && "${1:0:1}" != "-" ]]; do only+=("$1"); shift; done; continue ;;
    -h|--help) sed -n '4,11p' "$0"; exit 0 ;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ ${#only[@]} -gt 0 ]]; then selected=("${only[@]}"); else selected=("${pkgs[@]}"); fi

declare -A RESOLVED=()
declare -a failed=() ok_list=() temps=()

cleanup() {
  if [[ ${#temps[@]} -gt 0 ]]; then
    rm -f "${temps[@]}"
  fi
  return 0
}
trap cleanup EXIT

sha_file() { sha256sum "$1" | awk '{print $1}'; }

# магические байты: не даёт записать в .zon хэш от HTML-страницы ошибки
is_archive() {
  local magic
  magic="$(head -c 6 "$1" | od -An -v -tx1 | tr -d ' \n')"
  case "$magic" in
    1f8b*) return 0 ;;              # gzip
    fd377a585a00*) return 0 ;;      # xz
  esac
  return 1
}

# ВАЖНО: вызывается БЕЗ $(...), иначе записи в RESOLVED/temps теряются в сабшелле.
# Путь кладёт в переменную, имя которой передано вторым аргументом.
resolve_archive() {
  local name=$1 outvar=$2
  local -a cands=()
  local u tmp rc

  # многострочные кандидаты: read -a берёт только первую строку, поэтому цикл
  while IFS= read -r u; do
    [[ -n "$u" ]] && cands+=("$u")
  done < <(printf '%s\n' "${URLS[$name]}" | tr -s '[:space:]' '\n')

  if [[ ${#cands[@]} -eq 0 ]]; then
    echo "  URLS[$name] пуст" >&2
    return 1
  fi

  tmp="$(mktemp "${TMPDIR:-/tmp}/zp-$name.XXXXXX")" || return 1
  temps+=("$tmp")

  for u in "${cands[@]}"; do
    printf '  %s <- %s\n' "$name" "$u" >&2
    : > "$tmp"

    curl -fsSL --connect-timeout 8 --max-time 300 --retry 1 --retry-delay 1 \
         -A 'zp-gen-batch3' -o "$tmp" "$u"
    rc=$?

    if [[ $rc -ne 0 ]]; then
      printf '    curl rc=%s\n' "$rc" >&2
      continue
    fi
    if [[ ! -s "$tmp" ]]; then
      echo "    пустой ответ" >&2
      continue
    fi
    if ! is_archive "$tmp"; then
      echo "    не похоже на tar.gz/tar.xz (вероятно HTML-ошибка), пробуем дальше" >&2
      continue
    fi

    RESOLVED[$name]="$u"
    printf -v "$outvar" '%s' "$tmp"
    return 0
  done
  return 1
}

zon_content() {
  local name=$1 hash=$2
  cat <<EOF
.{
    .name = "$name",
    .version = "${VERSION[$name]}",
    .license = "${LICENSE[$name]}",
    .url = "${RESOLVED[$name]}",
    .hash = "$hash",
    .deps = ${DEPS[$name]},
}
EOF
}

zon_header() {
  local name=$1
  cat <<EOF
#!/bin/sh
set -eu

NAME=$name
SRC="/var/zp/build/\$NAME"
DESTDIR="/var/zp/pkg/\$NAME"
JOBS="\$(nproc)"

cd "\$SRC"

EOF
}

buildfile_body() {
  case $1 in
    bzip2)
      cat <<'EOF'
make -f Makefile-libbz2_so -j"$JOBS"
make -j"$JOBS"
make install PREFIX=/usr DESTDIR="$DESTDIR"

# make install ставит только libbz2.a; shared-либу — руками
so="$(ls libbz2.so.* 2>/dev/null | head -1)"
test -n "$so"
install -Dm755 "$so" "$DESTDIR/usr/lib/$so"
ln -sf "$so" "$DESTDIR/usr/lib/libbz2.so.1.0"
ln -sf "$so" "$DESTDIR/usr/lib/libbz2.so"
EOF
      ;;
    libcap)
      cat <<'EOF'
make -j"$JOBS" prefix=/usr
# RAISE_SETFCAP=no — иначе install зовёт setcap на файл внутри DESTDIR
make install prefix=/usr RAISE_SETFCAP=no DESTDIR="$DESTDIR"
EOF
      ;;
    snappy)
      cat <<'EOF'
cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/usr \
  -DSNAPPY_BUILD_TESTS=OFF \
  -DSNAPPY_BUILD_BENCHMARKS=OFF

cmake --build build -j"$JOBS"
DESTDIR="$DESTDIR" cmake --install build
EOF
      ;;
    curl)
      cat <<'EOF'
./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var \
  --with-openssl --with-zlib --enable-threaded-resolver --disable-static
make -j"$JOBS"
make DESTDIR="$DESTDIR" install
EOF
      ;;
    sqlite)
      cat <<'EOF'
./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var \
  --enable-readline --enable-fts5 --disable-static
make -j"$JOBS"
make DESTDIR="$DESTDIR" install
EOF
      ;;
    libffi)
      cat <<'EOF'
./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var --disable-static
make -j"$JOBS"
make DESTDIR="$DESTDIR" install
EOF
      ;;
    shadow)
      cat <<'EOF'
./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var \
  --disable-static --enable-shadowgrp
make -j"$JOBS"
make DESTDIR="$DESTDIR" install

# passwd/su/login обязаны остаться setuid
for f in passwd su login chsh chfn; do
  [ -f "$DESTDIR/usr/bin/$f" ] && chmod u+s "$DESTDIR/usr/bin/$f"
done
EOF
      ;;
    *)
      cat <<'EOF'
./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var
make -j"$JOBS"
make DESTDIR="$DESTDIR" install
EOF
      ;;
  esac
}

read_zon_field() {
  awk -F'"' -v f="$2" '$0 ~ "^[[:space:]]*\\."f"[[:space:]]*=" {print $2; exit}' "$1"
}

[[ -d pkgs ]] || { echo "Нет pkgs/ — запускай из корня zp-repo." >&2; exit 1; }

for name in "${selected[@]}"; do
  dir="pkgs/$name"; zon="$dir/$name.zon"
  arc=""

  if [[ $verify -eq 1 ]]; then
    [[ -f "$zon" ]] || { echo "[$name] FAIL: нет $zon"; failed+=("$name"); continue; }
    expected="$(read_zon_field "$zon" hash)"
    [[ -n "$expected" && "$expected" != "TODO" ]] || { echo "[$name] SKIP: .hash пуст"; continue; }

    saved="${URLS[$name]}"
    URLS[$name]="$(read_zon_field "$zon" url)"
    if ! resolve_archive "$name" arc; then
      echo "[$name] FAIL: не скачался ${URLS[$name]}"
      failed+=("$name"); URLS[$name]="$saved"; continue
    fi
    URLS[$name]="$saved"

    actual="$(sha_file "$arc")"
    if [[ "${expected,,}" == "${actual,,}" ]]; then
      echo "[$name] OK   $actual"; ok_list+=("$name")
    else
      echo "[$name] FAIL sha256 расходится"
      echo "       expected: ${expected,,}"
      echo "       actual:   ${actual,,}"
      failed+=("$name")
    fi
    continue
  fi

  if [[ -f "$zon" && $force -eq 0 ]]; then
    echo "[$name] уже существует (--force для перезаписи)"; continue
  fi

  echo "[$name] ${VERSION[$name]}"

  if ! resolve_archive "$name" arc; then
    echo "[$name] FAIL: ни одно зеркало не答 не ответило"
    echo "        Поправь VERSION/URLS и повтори: --force --only $name"
    failed+=("$name"); continue
  fi

  hash="$(sha_file "$arc")"
  [[ ${#hash} -eq 64 ]] || { echo "[$name] FAIL: странный sha256: '$hash'"; failed+=("$name"); continue; }

  mkdir -p "$dir"
  zon_content "$name" "$hash" > "$zon"
  { zon_header "$name"; buildfile_body "$name"; } > "$dir/BuildFile"
  chmod +x "$dir/BuildFile"

  if [[ -n "${INDEX[$name]:-}" ]]; then
    cat > "$dir/update.json" <<EOF
{
  "type": "listing",
  "index": "${INDEX[$name]}",
  "pattern": "${PATTERN[$name]}",
  "url_template": "${TEMPLATE[$name]}"
}
EOF
  elif [[ "$name" == "sqlite" ]]; then
    printf '{\n  "type": "none"\n}\n' > "$dir/update.json"
  else
    rm -f "$dir/update.json"
  fi

  [[ "$(read_zon_field "$zon" hash)" == "$hash" ]] || { echo "[$name] FAIL: hash не записался"; failed+=("$name"); continue; }
  [[ "$(read_zon_field "$zon" url)" == "${RESOLVED[$name]}" ]] || { echo "[$name] FAIL: url не записался"; failed+=("$name"); continue; }

  printf '[%s] OK  %s\n      %s%s\n' "$name" "$hash" "${RESOLVED[$name]}" \
    "$([[ -n "${INDEX[$name]:-}" ]] && echo '   (+update.json)')"
  ok_list+=("$name")
done

echo
echo "Успешно: ${#ok_list[@]}, с ошибками: ${#failed[@]}"
[[ ${#failed[@]} -gt 0 ]] && { echo "Провалились: ${failed[*]}"; exit 1; }
exit 0
