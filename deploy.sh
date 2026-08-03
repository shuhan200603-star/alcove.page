#!/usr/bin/env bash
#
# 一步部署。用法：
#
#     sudo /opt/alcove/frontend/deploy.sh
#
# 拉最新代码 → 同步前端 → 后端变了才更新并重启。
#
set -euo pipefail

REPO=/opt/alcove/frontend
STATIC=/opt/alcove/static
BACKEND=/opt/alcove/server.py
SERVICE=alcove

cd "$REPO"

# --ff-only：本地要是有人动过文件就直接失败，而不是留下一堆冲突标记。
# 失败了不要在这里改，去问在写代码的那一边。
git pull --ff-only

# backend/ 和这些说明文件不进静态目录——那个目录是公开的。
rsync -a --delete \
      --exclude .git --exclude .gitignore \
      --exclude backend --exclude README.md --exclude deploy.sh \
      "$REPO"/ "$STATIC"/
echo "✅ 前端已同步 → $STATIC"

# 重启会掐断正在进行的对话，所以只有 server.py 真的变了才重启。
if cmp -s "$REPO/backend/server.py" "$BACKEND"; then
  echo "✅ 后端没变，不用重启"
else
  cp "$REPO/backend/server.py" "$BACKEND"
  systemctl restart "$SERVICE"
  echo "✅ 后端已更新并重启"
fi

systemctl is-active --quiet "$SERVICE" && echo "✅ $SERVICE 正在运行" \
  || echo "⚠️  $SERVICE 没在运行，跑 systemctl status $SERVICE --no-pager 看看"
