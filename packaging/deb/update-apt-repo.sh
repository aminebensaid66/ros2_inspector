#!/usr/bin/env bash
# Add .deb files to a flat apt repository checkout, rebuild its indexes and sign them.
#
# Repository layout (served as static files, for example by GitHub Pages):
#   pool/<codename>/main/r/ros2inspector/*.deb
#   dists/<codename>/main/binary-<arch>/Packages{,.gz}
#   dists/<codename>/{Release,InRelease,Release.gpg}
#   ros2inspector-archive-keyring.gpg   (binary public key for /etc/apt/keyrings)
#   ros2inspector-archive-keyring.asc   (same key, ASCII armored)
#
# Each .deb must be named <package>_<upstream>-<rev>~<codename>_<arch>.deb, as
# build-deb.sh produces. The signing key must already be in the gpg keyring.
#
# Usage: update-apt-repo.sh --repo DIR --debs DIR --key FINGERPRINT [--keep 5]
set -euo pipefail

REPO=""
DEBS=""
KEY=""
KEEP=5
ARCHS="amd64 arm64"
PACKAGE="ros2inspector"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --debs) DEBS="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --keep) KEEP="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -d "$REPO" ] || { echo "--repo must be an existing directory" >&2; exit 2; }
[ -d "$DEBS" ] || { echo "--debs must be a directory of .deb files" >&2; exit 2; }
[ -n "$KEY" ] || { echo "--key must name the signing key fingerprint" >&2; exit 2; }
command -v apt-ftparchive >/dev/null || { echo "apt-ftparchive not found (install apt-utils)" >&2; exit 2; }

REPO="$(cd "$REPO" && pwd)"
shopt -s nullglob

# 1. Copy new packages into the per-codename pool.
added=0
for deb in "$DEBS"/*.deb; do
  name="$(basename "$deb")"
  version="$(dpkg-deb --field "$deb" Version)"
  codename="${version##*~}"
  if [ -z "$codename" ] || [ "$codename" = "$version" ]; then
    echo "skipping $name: version $version has no ~<codename> suffix" >&2
    continue
  fi
  dest="$REPO/pool/$codename/main/${PACKAGE:0:1}/$PACKAGE"
  mkdir -p "$dest"
  if [ -e "$dest/$name" ] && ! cmp -s "$deb" "$dest/$name"; then
    echo "refusing to replace published $name with different contents; bump the version" >&2
    exit 1
  fi
  cp "$deb" "$dest/$name"
  added=$((added + 1))
done
echo "Added $added package(s)"

cd "$REPO"

# 2. Keep only the newest $KEEP versions per codename and architecture.
for pool in pool/*/main/*/"$PACKAGE"; do
  for arch in $ARCHS; do
    mapfile -t old < <(ls "$pool"/*_"$arch".deb 2>/dev/null | sort -V | head -n "-$KEEP")
    for f in "${old[@]}"; do
      echo "pruning $f"
      rm -f "$f"
    done
  done
done

# 3. Rebuild Packages and a signed Release for every codename in the pool.
for pooldir in pool/*/; do
  codename="$(basename "$pooldir")"
  dist="dists/$codename"
  rm -rf "$dist"
  for arch in $ARCHS; do
    bin="$dist/main/binary-$arch"
    mkdir -p "$bin"
    apt-ftparchive --arch "$arch" packages "pool/$codename" > "$bin/Packages"
    gzip -9nkf "$bin/Packages"
  done
  apt-ftparchive \
    -o APT::FTPArchive::Release::Origin="$PACKAGE" \
    -o APT::FTPArchive::Release::Label="$PACKAGE" \
    -o APT::FTPArchive::Release::Suite="$codename" \
    -o APT::FTPArchive::Release::Codename="$codename" \
    -o APT::FTPArchive::Release::Architectures="$ARCHS" \
    -o APT::FTPArchive::Release::Components="main" \
    -o APT::FTPArchive::Release::Description="ros2inspector packages for Ubuntu $codename" \
    release "$dist" > "$dist/Release.tmp"
  mv "$dist/Release.tmp" "$dist/Release"
  gpg --batch --yes --local-user "$KEY" --clearsign --output "$dist/InRelease" "$dist/Release"
  gpg --batch --yes --local-user "$KEY" --armor --detach-sign --output "$dist/Release.gpg" "$dist/Release"
done

# 4. Publish the public key next to the indexes.
gpg --batch --yes --export "$KEY" > "$PACKAGE-archive-keyring.gpg"
gpg --batch --yes --armor --export "$KEY" > "$PACKAGE-archive-keyring.asc"
touch .nojekyll

echo "Repository updated:"
find dists -name Packages -exec sh -c 'printf "%s: %s package(s)\n" "$1" "$(grep -c "^Package:" "$1")"' _ {} \;
