#!/usr/bin/env bash
# 一键跑全部测试（本地 = CI 同一套）。CI 门禁跑的就是这个流程。
# 用法: bash tests/run_all.sh
set -uo pipefail

cd "$(dirname "$0")/.."
FAILED=0

echo "══════ 1/3 单元测试（纯函数 / 数据库 / 增量逻辑）══════"
if python -m unittest discover -s tests -p 'test_unit.py' 2>&1 | tail -4; then :; fi
python -m unittest discover -s tests -p 'test_unit.py' >/dev/null 2>&1 || FAILED=1

echo
echo "══════ 2/3 前端静态检查（零依赖，CI 第一道门禁）══════"
python tests/test_frontend_lint.py || FAILED=1

echo
echo "══════ 3/3 端到端测试（真实浏览器点一遍）══════"
python tests/test_e2e.py 2>&1 | tail -6 || FAILED=1
python tests/test_e2e.py >/dev/null 2>&1 || FAILED=1

echo
if [ "$FAILED" -eq 0 ]; then
  echo "✓ 全部测试通过，可以部署"
else
  echo "✗ 有测试失败，禁止部署"
fi
exit $FAILED
