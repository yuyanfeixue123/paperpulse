#!/usr/bin/env bash
# 仓库创建后一次性完成：推送 + 设置仓库元信息 + Topics
# 用法：bash scripts/publish.sh <TOKEN>
# Token 仅通过环境变量传入，不会写入任何文件。

set -euo pipefail

REPO="yuyanfeixue123/paperpulse"
API="https://api.github.com"
LOCAL="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -z "${1:-}" ]]; then
  echo "用法：bash scripts/publish.sh <TOKEN>" >&2
  exit 1
fi
TOKEN="$1"

gh() {
  curl -sS --max-time 60 \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$@"
}

cd "$LOCAL"

echo "==> 1/5 检查远端仓库"
if gh "$API/repos/$REPO" | grep -q '"full_name"'; then
  echo "    仓库已存在"
else
  echo "    ✗ 仓库 $REPO 不存在，请先在 https://github.com/new 创建一个空仓库" >&2
  exit 1
fi

echo "==> 2/5 配置远端并推送"
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "https://github.com/$REPO.git"
else
  git remote add origin "https://github.com/$REPO.git"
fi
# 用带令牌的 URL 推送一次，避免交互式输入；URL 不落盘到 git config
git -c credential.helper= push "https://x-access-token:${TOKEN}@github.com/$REPO.git" \
    main:main --follow-tags

echo "==> 3/5 设置仓库描述与主页"
gh -X PATCH "$API/repos/$REPO" -d '{
  "description": "Self-hosted paper digest: LLM filters new papers by your interest profile and emails them daily. Runs on 1 vCPU / 1 GB.",
  "homepage": "https://github.com/yuyanfeixue123/paperpulse",
  "has_wiki": false,
  "has_projects": false
}' >/dev/null && echo "    ✓ 描述已设置"

echo "==> 4/5 设置 Topics"
gh -X PUT "$API/repos/$REPO/topics" \
  -H "Accept: application/vnd.github.mercy-preview+json" \
  -d '{"names":["paperpulse","literature-review","arxiv","openalex","research-tool","self-hosted","email-digest","llm","sqlite","python"]}' \
  >/dev/null && echo "    ✓ 10 个 Topics 已设置"

echo "==> 5/5 输出结果"
gh "$API/repos/$REPO" | "$LOCAL/.venv/bin/python" -c "
import sys, json
d = json.load(sys.stdin)
print(f\"    地址   : {d['html_url']}\")
print(f\"    可见性 : {'Private' if d['private'] else 'Public'}\")
print(f\"    默认分支: {d['default_branch']}\")
print(f\"    Stars  : {d['stargazers_count']}  Forks: {d['forks_count']}\")
" 2>/dev/null || echo "    仓库: https://github.com/$REPO"

echo
echo "完成。CI 会在 push 后自动运行：https://github.com/$REPO/actions"
echo "别忘了在仓库页确认 Description 与 Topics 已生效。"
