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

# Only deploy the canonical repository's main branch. A checkout of an older
# branch or a different origin used to make changes appear to "revert".
branch=$(git branch --show-current)
origin=$(git remote get-url origin)
if [[ "$branch" != main || ! "$origin" =~ (^|[:/])shuhan200603-star/alcove\.page(\.git)?$ ]]; then
  echo "❌ 当前检出不是 alcove.page 的 main：branch=$branch origin=$origin" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "❌ 仓库有未提交的本地修改，请先保存到 GitHub" >&2
  exit 1
fi

# --ff-only：本地要是有人动过文件就直接失败，而不是留下一堆冲突标记。
# 失败了不要在这里改，去问在写代码的那一边。
sudo -H -u "$(stat -c %U .git)" git pull --ff-only

service_user=$(systemctl show "$SERVICE" -p User --value)
service_user=${service_user:-root}
service_group=$(id -gn "$service_user")
install -d -m 700 -o "$service_user" -g "$service_group" \
  /opt/alcove/files /opt/alcove/files/inbox /opt/alcove/files/outbox

# Check the Python source before touching the running service.
python3 -m py_compile "$REPO/backend/server.py"

if ! cmp -s "$REPO/backend/server.py" "$BACKEND"; then
  backup=$(mktemp /opt/alcove/server.py.backup.XXXXXX)
  had_backend=0
  if [[ -f "$BACKEND" ]]; then
    cp "$BACKEND" "$backup"
    had_backend=1
  fi
  cp "$REPO/backend/server.py" "$BACKEND"
  if ! systemctl restart "$SERVICE" || ! systemctl is-active --quiet "$SERVICE"; then
    if [[ "$had_backend" == 1 ]]; then
      cp "$backup" "$BACKEND"
      systemctl restart "$SERVICE" || true
    else
      rm -f "$BACKEND"
    fi
    rm -f "$backup"
    echo "❌ 后端启动失败，已恢复旧版；静态文件尚未同步" >&2
    exit 1
  fi
  rm -f "$backup"
  echo "✅ 后端已更新并重启"
else
  echo "✅ 后端没变，不用重启"
fi

# backend/ 和这些说明文件不进静态目录——那个目录是公开的。
rsync -a --delete \
      --exclude .git --exclude .gitignore \
      --exclude backend --exclude README.md --exclude deploy.sh \
      "$REPO"/ "$STATIC"/
echo "✅ 前端已同步 → $STATIC"
echo "✅ 已部署提交 $(git rev-parse --short HEAD)"

systemctl is-active --quiet "$SERVICE" && echo "✅ $SERVICE 正在运行" \
  || echo "⚠️  $SERVICE 没在运行，跑 systemctl status $SERVICE --no-pager 看看"
