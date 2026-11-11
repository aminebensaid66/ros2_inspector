#!/usr/bin/env bash
# Build a self-contained ros2inspector .deb for the Ubuntu release this script runs on.
#
# The package bundles ros2inspector and its pinned Python dependencies in a private
# virtual environment under /opt/ros2inspector, so it does not need any python3-*
# packages beyond the interpreter. Run it inside a container of the target release
# (for example ubuntu:24.04), because the venv is tied to that release's python3.
#
# Usage: build-deb.sh --wheel dist/ros2inspector-X.Y.Z-py3-none-any.whl \
#                     --requirements requirements.txt [--out deb-out] [--revision 1]
set -euo pipefail

WHEEL=""
REQUIREMENTS=""
OUT_DIR="deb-out"
REVISION="1"
PREFIX="/opt/ros2inspector"
PACKAGE="ros2inspector"

while [ $# -gt 0 ]; do
  case "$1" in
    --wheel) WHEEL="$2"; shift 2 ;;
    --requirements) REQUIREMENTS="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --revision) REVISION="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -f "$WHEEL" ] || { echo "--wheel must point to a built ros2inspector wheel" >&2; exit 2; }
[ -f "$REQUIREMENTS" ] || { echo "--requirements must point to a hashed requirements file" >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
WHEEL="$(cd "$(dirname "$WHEEL")" && pwd)/$(basename "$WHEEL")"
REQUIREMENTS="$(cd "$(dirname "$REQUIREMENTS")" && pwd)/$(basename "$REQUIREMENTS")"
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

# Version from the wheel name: ros2inspector-0.2.0-py3-none-any.whl -> 0.2.0
UPSTREAM_VERSION="$(basename "$WHEEL" | cut -d- -f2)"
# shellcheck disable=SC1091
. /etc/os-release
CODENAME="${VERSION_CODENAME:?cannot read VERSION_CODENAME from /etc/os-release}"
ARCH="$(dpkg --print-architecture)"
DEB_VERSION="${UPSTREAM_VERSION}-${REVISION}~${CODENAME}"

PY_MAJOR_MINOR="$(/usr/bin/python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
PY_NEXT="$(/usr/bin/python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1] + 1}")')"

echo "Building $PACKAGE $DEB_VERSION for $CODENAME/$ARCH with Python $PY_MAJOR_MINOR"

# The venv must be created at its final path: its scripts hard-code the interpreter path.
rm -rf "$PREFIX"
/usr/bin/python3 -m venv "$PREFIX"
"$PREFIX/bin/python" -m pip install --quiet --no-cache-dir --upgrade "pip>=24"
"$PREFIX/bin/python" -m pip install --no-cache-dir --prefer-binary --require-hashes -r "$REQUIREMENTS"
"$PREFIX/bin/python" -m pip install --no-cache-dir --no-deps "$WHEEL"
"$PREFIX/bin/python" -m pip uninstall --quiet --yes pip setuptools wheel 2>/dev/null || true
rm -f "$PREFIX"/bin/pip* "$PREFIX"/bin/activate* "$PREFIX"/bin/Activate.ps1
# Precompile so nothing tries to write __pycache__ under /opt at run time.
"$PREFIX/bin/python" -m compileall -q -j 0 "$PREFIX/lib" >/dev/null

"$PREFIX/bin/ros2inspector" --version

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE$(dirname "$PREFIX")" "$STAGE/usr/bin" "$STAGE/usr/share/doc/$PACKAGE" "$STAGE/DEBIAN"
cp -a "$PREFIX" "$STAGE$PREFIX"
ln -s "$PREFIX/bin/ros2inspector" "$STAGE/usr/bin/ros2inspector"

{
  echo "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"
  echo "Upstream-Name: ros2inspector"
  echo "Source: https://github.com/aminebensaid66/ros2_inspector"
  echo
  echo "Files: *"
  echo "License: MIT"
  echo
  echo "Comment: Third-party Python packages bundled under $PREFIX keep their own"
  echo " licenses; see the *.dist-info directories in $PREFIX/lib."
  echo
  echo "License: MIT"
  sed 's/^$/./; s/^/ /' "$REPO_ROOT/LICENSE"
} > "$STAGE/usr/share/doc/$PACKAGE/copyright"
gzip -9n -c "$REPO_ROOT/CHANGELOG.md" > "$STAGE/usr/share/doc/$PACKAGE/changelog.gz"

INSTALLED_SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"

cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: $PACKAGE
Version: $DEB_VERSION
Architecture: $ARCH
Maintainer: Amine Bensaid <aminebensaid66@gmail.com>
Installed-Size: $INSTALLED_SIZE
Depends: python3 (>= ${PY_MAJOR_MINOR}~), python3 (<< ${PY_NEXT}), libc6
Section: devel
Priority: optional
Homepage: https://ros2-inspector-theta.vercel.app/
Description: static ROS 2 architecture analysis, visualization and policy checking
 ros2inspector reads a ROS 2 workspace without running it and reports its
 packages, nodes, topics, services, actions and launch structure. It renders
 interactive graphs, writes architecture docs, audits the workspace and checks
 it against architecture policies.
 .
 This package bundles its Python dependencies in a private environment under
 $PREFIX and does not need a ROS 2 installation.
CONTROL

# Remove files Python writes at run time (for example after a manual pip install into the venv).
cat > "$STAGE/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
  rm -rf /opt/ros2inspector
fi
POSTRM
chmod 0755 "$STAGE/DEBIAN/postrm"

DEB_FILE="$OUT_DIR/${PACKAGE}_${DEB_VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group --build -Zxz "$STAGE" "$DEB_FILE"
dpkg-deb --info "$DEB_FILE"
echo "Built $DEB_FILE"
