#!/bin/bash
# Build a tight staging tree for cad.visitworld.me.
# Does not copy .env or API keys. Does not move original project folders.
# Works without rsync (uses cp). Disk on the production host is tight.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGING="${1:-/tmp/cad-studio-pack}"
DATA="$STAGING/data"

rm -rf "$STAGING"
mkdir -p "$STAGING/app" "$STAGING/web" "$STAGING/deploy" "$STAGING/scripts" "$DATA"

# App source
find "$ROOT/app" -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 |
  while IFS= read -r -d '' f; do
    rel="${f#"$ROOT/"}"
    mkdir -p "$STAGING/$(dirname "$rel")"
    cp -a "$f" "$STAGING/$rel"
  done

# Frontend including vendored fonts
cp -a "$ROOT/web/." "$STAGING/web/"
cp -a "$ROOT/deploy/." "$STAGING/deploy/"
cp -a "$ROOT/scripts/." "$STAGING/scripts/"
cp -a "$ROOT/requirements.txt" "$ROOT/README.md" "$ROOT/DESIGN.md" "$ROOT/.env.example" "$STAGING/"
mkdir -p "$STAGING/var/preview-cache" "$STAGING/var/overlays"

pack_project() {
  local src="$1"
  local dest="$2"
  mkdir -p "$dest/out"
  shopt -s nullglob
  for dxf in "$src"/out/*.dxf; do
    cp -a "$dxf" "$dest/out/"
  done
  shopt -u nullglob
  for f in INDEX.md index.json CHANGELOG.md changelog.json chat.json; do
    if [[ -f "$src/$f" ]]; then
      cp -a "$src/$f" "$dest/$f"
    fi
  done
  if [[ -d "$src/.studio" ]]; then
    mkdir -p "$dest/.studio"
    cp -a "$src/.studio/." "$dest/.studio/"
  fi
}

if [[ -d /workspace/cad-nl/projects/学生食堂 ]]; then
  pack_project /workspace/cad-nl/projects/学生食堂 "$DATA/学生食堂"
fi
if [[ -d /workspace/cad-nl/demo-t53 ]]; then
  pack_project /workspace/cad-nl/demo-t53 "$DATA/万商城3号楼给排水"
fi


# Default component library index (not the bulky source DWGs)
if [[ -d /workspace/cad-nl/libraries/default ]]; then
  mkdir -p "$DATA/libraries/default"
  for f in INDEX.md index.json CHANGELOG.md changelog.json; do
    if [[ -f /workspace/cad-nl/libraries/default/$f ]]; then
      cp -a /workspace/cad-nl/libraries/default/$f "$DATA/libraries/default/"
    fi
  done
fi


# Curated indexes for known drawings (matched by file hash on upload)
if [[ -d "$ROOT/known-indexes" ]]; then
  mkdir -p "$STAGING/known-indexes"
  cp -a "$ROOT/known-indexes/." "$STAGING/known-indexes/"
fi

echo "staging: $STAGING"
du -sh "$STAGING" "$STAGING/web" "$STAGING/app" "$DATA"/* 2>/dev/null || true
echo
echo "On the deploy host (parent will do this; do not pack .env):"
echo "  scp/rsync $STAGING/ to /opt/cad-studio/current/"
echo "  write /opt/cad-studio/.env mode 600 (MAAS_*, ACCESS_*, SESSION_SECRET, CAD_DATA_DIR=/opt/cad-studio/current/data, SESSION_SECURE=1 after HTTPS)"
echo "  python3 -m venv /opt/cad-studio/current/.venv && .venv/bin/pip install -r requirements.txt"
echo "  apt install fonts-noto-cjk   # overlay labels need NotoSansCJK-Regular.ttc"
echo "  create user cad, install deploy/cad-studio.service, install HTTP nginx conf, start"
