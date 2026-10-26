#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# zero-bot 发布/更新到 GitHub（Termux 版）
# 用法:
#   bash /sdcard/BOTGitHub/push.sh llm-autonomous-trader
#     ↑ 仓库已存在 → 保留历史，把 v5 作为新提交盖上去
#     ↑ 仓库不存在 → 自动创建再推
# 依赖: ~/gh_token.txt 里存一行你的 GitHub token
# ============================================================
set -u

# 全局：VPN 环境多用 HTTP/1.1，治 SSL 断连（EOF）
export GIT_HTTP_VERSION=HTTP/1.1

REPO="${1:-llm-autonomous-trader}"
SOURCE="/sdcard/BOTGitHub"
WORK="$HOME/BOTGitHub-push"

# ---------- 0. 读 token ----------
TOKEN=""
if [ -f "$HOME/gh_token.txt" ]; then
  TOKEN=$(tr -d '[:space:]' < "$HOME/gh_token.txt")
fi
if [ -z "$TOKEN" ]; then
  echo "❌ 没找到 token，请先: echo '你的token' > ~/gh_token.txt"
  exit 1
fi

# ---------- 1. 依赖 ----------
command -v git >/dev/null 2>&1 || { echo "❌ 没装 git，先: pkg install -y git"; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "❌ 没装 curl，先: pkg install -y curl"; exit 1; }

# ---------- 2. 账号 ----------
echo "==> 读取 GitHub 账号..."
USER=$(curl -sS --max-time 25 -H "Authorization: Bearer $TOKEN" \
  https://api.github.com/user 2>/dev/null \
  | grep -o '"login"[^,]*' | head -1 | cut -d'"' -f4)
if [ -z "$USER" ]; then
  echo "❌ token 无效或网络不通（VPN 开着吗）"
  exit 1
fi
echo "    账号: $USER"

# ---------- 3. 仓库存在性 ----------
echo "==> 检查远程仓库 $USER/$REPO ..."
CODE=$(curl -sS -o /dev/null --max-time 25 -w '%{http_code}' \
  -H "Authorization: Bearer $TOKEN" \
  https://api.github.com/repos/$USER/$REPO 2>/dev/null)
if [ "$CODE" = "404" ]; then
  echo "    仓库不存在，自动创建（公开）..."
  curl -sS --max-time 25 -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$REPO\",\"private\":false,\"description\":\"LLM autonomous futures trader (Binance testnet, educational)\"}" \
    https://api.github.com/user/repos >/dev/null 2>&1
  echo "    已创建"
elif [ "$CODE" != "200" ]; then
  echo "⚠️  仓库检查 HTTP $CODE，继续尝试"
fi

# ---------- 4. 克隆远程仓库到干净目录（带重试） ----------
REMOTE_URL="https://x-access-token:${TOKEN}@github.com/${USER}/${REPO}.git"
echo "==> 克隆远程仓库到 $WORK ..."
rm -rf "$WORK"
mkdir -p "$WORK"
cd "$WORK" || exit 1

CLONE_OK=0
for i in 1 2 3; do
  if git clone -q "$REMOTE_URL" . 2>/dev/null; then
    CLONE_OK=1
    break
  fi
  echo "    （第 ${i} 次克隆失败，3 秒后重试）"
  sleep 3
done

if [ "$CLONE_OK" = "0" ]; then
  echo "    远程克隆失败，退化为空仓库初始化（稍后推送会再试）..."
  git init -q
  git checkout -q -B main 2>/dev/null
  git remote add origin "$REMOTE_URL" 2>/dev/null
fi

# 确保在 main 分支（克隆成功时不一定在 main）
if ! git rev-parse -q --verify main >/dev/null 2>&1; then
  git checkout -q -B main 2>/dev/null
else
  git checkout -q main 2>/dev/null
fi

# ---------- 5. 复制 v4 文件覆盖 ----------
echo "==> 用 v4 文件覆盖（保留历史）..."
cp -rf "$SOURCE"/. "$WORK"/ 2>/dev/null

# ---------- 6. 提交 ----------
git add -A
if git diff --cached --quiet; then
  echo "    （文件与上次无差异，跳过提交）"
else
  git -c user.name="$USER" -c user.email="$USER@users.noreply.github.com" \
    commit -q -m "v5: 网页看板 + 快捷指令 + 交易账本 + 日志自动清理"
fi

# ---------- 7. 推送（带重试） ----------
echo "==> 推送到 GitHub ..."
PUSH_OK=0
for i in 1 2 3 4 5; do
  if git push -q -u origin main 2>/dev/null; then
    PUSH_OK=1
    break
  fi
  echo "    （第 ${i} 次推送失败，3 秒后重试）"
  sleep 3
done

# 清理 remote 里的 token
git remote set-url origin "https://github.com/${USER}/${REPO}.git" 2>/dev/null

if [ "$PUSH_OK" = "1" ]; then
  echo ""
  echo "✅ 完成！ https://github.com/$USER/$REPO"
else
  echo ""
  echo "❌ 推送失败（网络或 VPN 不稳定）。"
  echo "   请确认 VPN 连接稳定后，重新运行本脚本即可。"
  echo "   提示：脚本支持断点续传，本地已提交，重跑不会丢改动。"
  exit 1
fi