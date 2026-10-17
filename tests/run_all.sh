#!/usr/bin/env bash
# 一键跑全部测试（本地 = CI 同一套）。CI 门禁跑的就是这个流程。
# 用法: bash tests/run_all.sh
set -uo pipefail

cd "$(dirname "$0")/.."
FAILED=0

echo "══════ 1/3 单元测试（纯函数 / 数据库 / 增量逻辑）══════"
# 只跑一次：输出与退出码必须来自同一次运行（早期跑两遍会"日志OK但判定失败"）
python -m unittest discover -s tests -p 'test_unit.py' > /tmp/unit_gate.log 2>&1
UNIT_RC=$?
tail -4 /tmp/unit_gate.log
[ "$UNIT_RC" -eq 0 ] || { FAILED=1; echo "✗ 单元测试失败（rc=$UNIT_RC）"; tail -40 /tmp/unit_gate.log; }

echo
echo "══════ 1.5/3 CI 工作流守卫（完整性判据 / force / 多档 cron）══════"
python tests/test_workflow_guard.py > /tmp/guard_gate.log 2>&1
GUARD_RC=$?
tail -3 /tmp/guard_gate.log
[ "$GUARD_RC" -eq 0 ] || { FAILED=1; echo "✗ 守卫测试失败（rc=$GUARD_RC）"; tail -40 /tmp/guard_gate.log; }

echo
echo "══════ 2/3 前端静态检查（零依赖，CI 第一道门禁）══════"
python tests/test_frontend_lint.py || FAILED=1

echo
echo "══════ 3/3 端到端测试（真实浏览器点一遍）══════"
# 只跑一次，用 PIPESTATUS 取真实退出码（避免与显示用的那次不一致）
python tests/test_e2e.py > /tmp/e2e_gate.log 2>&1
E2E_RC=$?
tail -6 /tmp/e2e_gate.log
[ "$E2E_RC" -eq 0 ] || { FAILED=1; echo "✗ E2E 失败（rc=$E2E_RC）"; tail -40 /tmp/e2e_gate.log; }

echo
if [ "$FAILED" -eq 0 ]; then
  echo "✓ 全部测试通过，可以部署"
else
  echo "✗ 有测试失败，禁止部署"
fi
exit $FAILED
