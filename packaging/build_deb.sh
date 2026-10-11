#!/usr/bin/env bash
# Wrap the frozen app (dist/amvault, from packaging/build.py) in a .deb.
#
#   packaging/build_deb.sh 0.1.0
#
# The app goes to /opt/amvault because it is one self-contained folder, with a
# link on the PATH and a menu entry pointing at it.
set -euo pipefail

VERSION="${1:?usage: build_deb.sh <version>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="$HERE/build/deb"
ARCH="$(dpkg --print-architecture)"

rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/opt" "$PKG/usr/bin" \
         "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/512x512/apps"

cp -a "$HERE/dist/amvault" "$PKG/opt/amvault"
ln -s /opt/amvault/amvault "$PKG/usr/bin/amvault"
cp "$HERE/packaging/amvault.desktop" "$PKG/usr/share/applications/amvault.desktop"
cp "$HERE/packaging/icon.png" "$PKG/usr/share/icons/hicolor/512x512/apps/amvault.png"

cat > "$PKG/DEBIAN/control" <<CONTROL
Package: amvault
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Maintainer: reza-hashemian <noreply@github.com>
Depends: libegl1, libxkbcommon-x11-0, libxcb-cursor0, libdbus-1-3, libfontconfig1
Recommends: openssh-client
Installed-Size: $(du -sk "$PKG/opt" | cut -f1)
Description: Encrypted vault for passwords, SSH keys and network credentials
 Stores everything in the KeePass .kdbx format, so the same file opens on a
 phone. Includes one-click SSH sessions and a credential broker for the
 companion network tool.
CONTROL

dpkg-deb --build --root-owner-group "$PKG" "$HERE/dist/amvault_${VERSION}_${ARCH}.deb"
