#!/usr/bin/env bash
# 生成 MoviePilot V3 可直接安装的插件发布包。
#
# 命名遵循宿主安装约定：
#   Release tag   : {插件类名}_v{版本}      例 HDWinGCrossSeed_v1.0.0
#   Release 资产  : {tag 全小写}.zip        例 hdwingcrossseed_v1.0.0.zip
#   zip 内部      : 根目录直接是插件文件（__init__.py 等），不含外层目录
#
# 用法：
#   bash install.sh            # 生成发布包
#   bash install.sh --publish  # 生成后用 gh 创建/更新 GitHub Release（需已 gh auth login）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PLUGIN="HDWinGCrossSeed"
SRC="$ROOT/plugins.v3/hdwingcrossseed"
STAGE="$ROOT/releases/v3"

[ -d "$SRC" ] || { echo "缺少 $SRC" >&2; exit 1; }

VERSION="$(python3 - "$ROOT/package.v3.json" "$PLUGIN" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fp:
    meta = json.load(fp)[sys.argv[2]]
print(meta["version"])
PY
)"

TAG="${PLUGIN}_v${VERSION}"
ASSET="$(printf '%s' "$TAG" | tr 'A-Z' 'a-z').zip"
ZIP="$STAGE/$ASSET"

mkdir -p "$STAGE"
rm -f "$STAGE"/*.zip

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/plugin"
cp -r "$SRC"/. "$TMP/plugin/"
find "$TMP" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$TMP" -name '*.pyc' -delete

python3 - "$TMP/plugin" "$ZIP" <<'PY'
import os, sys, zipfile
src, target = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
    for folder, dirs, files in os.walk(src):
        dirs.sort()
        for item in sorted(files):
            path = os.path.join(folder, item)
            bundle.write(path, os.path.relpath(path, src))
PY

# 图标自检：文件缺失或占位符未替换时给出提示（不阻断打包）
if [ ! -f "$ROOT/icons/hdwingcrossseed.png" ]; then
  echo "提示：未找到 icons/hdwingcrossseed.png，插件市场将显示默认图标" >&2
elif grep -q "GITHUB_USER" "$ROOT/package.v3.json" 2>/dev/null; then
  echo "提示：package.v3.json 的 icon 仍含占位符 GITHUB_USER，请替换为你的 GitHub 用户名" >&2
fi

echo "版本: $VERSION"
echo "Tag : $TAG"
echo "包  : $ZIP"
# macOS 默认没有 sha256sum，优先用 shasum
shasum -a 256 "$ZIP" 2>/dev/null || sha256sum "$ZIP"

python3 - "$ZIP" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as bundle:
    names = bundle.namelist()
print(f"共 {len(names)} 个文件：")
for name in names:
    print("  " + name)
PY

if [ "${1:-}" = "--publish" ]; then
  command -v gh >/dev/null 2>&1 || { echo "未找到 gh，请先安装 GitHub CLI" >&2; exit 1; }
  if gh release view "$TAG" >/dev/null 2>&1; then
    gh release upload "$TAG" "$ZIP" --clobber
  else
    gh release create "$TAG" "$ZIP" --title "$TAG" --notes "MoviePilot 插件 $PLUGIN $VERSION"
  fi
  echo "已发布 Release: $TAG"
else
  cat <<EOF

下一步（GitHub）：
  git tag $TAG && git push origin $TAG
  gh release create $TAG "$ZIP" --title "$TAG" --notes "MoviePilot 插件 $PLUGIN $VERSION"
EOF
fi
