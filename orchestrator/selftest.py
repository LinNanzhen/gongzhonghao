#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自检：用桩 provider 跑完一个 run，验证状态机与门禁。

不联网、不写真实案例（默认用临时案例目录 `_selftest_案例`），跑完清理。

    python3 orchestrator/selftest.py           # 跑一遍
    python3 orchestrator/selftest.py --keep    # 保留现场，便于排查
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
BASE = PKG.parent
sys.path.insert(0, str(PKG))

from orchestrator import loop  # noqa: E402

TEST_URL = "https://www.youtube.com/watch?v=SELFTEST0001"


def run(*argv: str) -> int:
    return loop.main(list(argv))


def _neutral_policy():
    """自检期间临时关闭自动放行，保证断言不受用户策略影响。"""
    import json
    path = HERE / "policy.json"
    backup = path.read_text(encoding="utf-8") if path.exists() else None

    def restore():
        if backup is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(backup, encoding="utf-8")

    if backup:
        data = json.loads(backup)
        data["auto_approve"] = {"topic": False, "publish": False}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return restore


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留产物与 run 状态")
    args = ap.parse_args()
    restore_policy = _neutral_policy()

    from orchestrator.state import run_id_for
    rid = run_id_for(TEST_URL)

    store = loop.RunStore(HERE / "runs")

    # 清场（含上次可能的残留案例目录）
    for old in store.all():
        if old.run_id == rid and old.case_dir:
            shutil.rmtree(loop.case_path(old), ignore_errors=True)
    if (HERE / "runs" / rid).exists():
        shutil.rmtree(HERE / "runs" / rid)

    print("① submit")
    assert run("submit", "--url", TEST_URL, "--title", "自检人物：一次真实选择") == 0
    case = loop.case_path(store.get(rid))

    print("② tick → 应停在选题闸门")
    run("tick", "--run-id", rid)
    st = store.get(rid)
    assert st and st.stage == "triaged" and st.waiting_gate == "topic", f"意外：{st}"

    print("③ approve topic → 再 tick，应因缺转写 blocked")
    run("approve", "--run-id", rid, "--gate", "topic")
    run("tick", "--run-id", rid)
    st = store.get(rid)
    assert st and st.status == "blocked", f"应当 blocked：{st.stage}/{st.status}"

    print("④ 补转写 + resume")
    (case / "转写").mkdir(parents=True, exist_ok=True)
    (case / "转写" / "transcript.json").write_text(
        '{"segments":[{"start":0.0,"end":3.0,"text":"selftest"}]}', encoding="utf-8")
    run("resume", "--run-id", rid)

    print("⑤ 循环推进直到人工发布闸门")
    reached = None
    for i in range(14):
        run("tick", "--run-id", rid)
        st = store.get(rid)
        if st.stage == "awaiting_publish" and st.waiting_gate == "publish":
            reached = st
            break
        if st.status == "blocked":
            print(f"✗ 被卡住：{st.error}")
            break

    if not reached:
        print("✗ 未到达 awaiting_publish")
        return 1

    print(f"✓ 到达 {reached.stage}（rework={reached.rework['count']}）")
    arts = {k: Path(v).name for k, v in reached.artifacts.items()}
    print("产物：", arts)

    print("⑥ approve publish → tick 归档")
    run("approve", "--run-id", rid, "--gate", "publish")
    run("tick", "--run-id", rid)
    st = store.get(rid)
    assert st.stage == "archived" and st.status == "done", f"未归档：{st.stage}/{st.status}"
    print("✓ 全链路通过：new → archived")

    restore_policy()
    if not args.keep:
        shutil.rmtree(case, ignore_errors=True)
        shutil.rmtree(HERE / "runs" / rid, ignore_errors=True)
        print("（现场已清理；--keep 可保留）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
