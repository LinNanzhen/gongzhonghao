#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自检：用桩 provider 跑完一个 run，验证状态机与门禁。

不联网、不写真实案例（默认用临时案例目录 `_selftest_案例`），跑完清理。

    python3 orchestrator/selftest.py           # 跑一遍
    python3 orchestrator/selftest.py --keep    # 保留现场，便于排查

T1 新契约（渲染验收）：
- 本机有 Chrome → tick 带 `--render`，全链路 new → archived 必须通过；
- 本机没 Chrome → 渲染验收判 INCOMPLETE（未完成 ≠ 通过），run 应停在 `composed`
  且 blocked、原因含「渲染」。这属于预期行为，打印说明后正常返回 0。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
BASE = PKG.parent
sys.path.insert(0, str(PKG))

from orchestrator import loop  # noqa: E402

TEST_URL = "https://www.youtube.com/watch?v=SELFTEST0001"

# 与 流水线/gzh_pipeline.py 相同的常见安装位置；只做存在性判断，不 import 流水线。
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def run(*argv: str) -> int:
    return loop.main(list(argv))


def find_chrome() -> str | None:
    """探测 Chrome/Chromium 可执行文件；找不到返回 None（不抛、不联网）。"""
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def _neutral_policy():
    """自检期间临时关闭自动放行，保证断言不受用户策略影响；返回恢复函数。"""
    path = HERE / "policy.json"
    backup = path.read_text(encoding="utf-8") if path.exists() else None

    def restore():
        if backup is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(backup, encoding="utf-8")

    if backup:
        try:
            data = json.loads(backup)
        except json.JSONDecodeError:
            data = {}
        # T4a：publish 键已从 policy 删除，代码也不读；自检只关 topic
        data["auto_approve"] = {"topic": False}
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
    chrome = find_chrome()
    render = ["--render"] if chrome else []

    def tick(*extra: str) -> int:
        # 自检不推闸门通知（避免开发机配了 webhook 时误发）
        return run("tick", "--run-id", rid, "--skip-ingest", "--no-notify", *extra)

    def cleanup() -> None:
        restore_policy()
        if args.keep:
            return
        old = store.get(rid)
        if old is not None and old.case_dir:
            shutil.rmtree(loop.case_path(old), ignore_errors=True)
        shutil.rmtree(HERE / "runs" / rid, ignore_errors=True)
        print("（现场已清理；--keep 可保留）")

    try:
        # 清场（含上次可能的残留案例目录）
        for old in store.all():
            if old.run_id == rid and old.case_dir:
                shutil.rmtree(loop.case_path(old), ignore_errors=True)
        if (HERE / "runs" / rid).exists():
            shutil.rmtree(HERE / "runs" / rid)

        print("① submit")
        assert run("submit", "--url", TEST_URL, "--title", "自检人物：一次真实选择") == 0
        case = loop.case_path(store.get(rid))

        print("② tick → 应停在选题闸门（T2a：一次 tick 只推一步）")
        tick()
        st = store.get(rid)
        assert st and st.stage == "triaged" and st.waiting_gate == "topic", f"意外：{st}"

        print("③ approve topic → 再 tick，应因缺转写 blocked")
        run("approve", "--run-id", rid, "--gate", "topic")
        tick()
        st = store.get(rid)
        assert st and st.status == "blocked", f"应当 blocked：{st.stage}/{st.status}"

        print("④ 补转写 + resume")
        (case / "转写").mkdir(parents=True, exist_ok=True)
        (case / "转写" / "transcript.json").write_text(
            '{"segments":[{"start":0.0,"end":3.0,"text":"selftest"}]}', encoding="utf-8")
        run("resume", "--run-id", rid)

        if chrome:
            print(f"⑤ 循环推进直到人工发布闸门（Chrome 渲染验收：{chrome}）")
        else:
            print("⑤ 循环推进直到人工发布闸门（未检测到 Chrome：按新契约应停在 composed 并被 blocked）")

        reached = None
        for i in range(24):        # 默认 --steps 1，一次只推一步，循环次数要够
            tick(*render)
            st = store.get(rid)
            if st.stage == "awaiting_publish" and st.waiting_gate == "publish":
                reached = st
                break
            if st.status == "blocked":
                if not chrome:
                    # T1 新契约：渲染没跑 ≠ 通过；停在 composed、原因含「渲染」是预期行为
                    assert st.stage == "composed", \
                        f"无 Chrome 时应停在 composed，实际 {st.stage}（{st.error}）"
                    assert "渲染" in (st.error or ""), \
                        f"blocked 原因应含「渲染」：{st.error}"
                    print(f"✓ 无 Chrome：run 在 {st.stage} 被 blocked（{st.error}）")
                    print("  渲染验收未完成 ≠ 通过，符合门禁新契约；跳过「发布→归档」断言。")
                    return 0
                print(f"✗ 被卡住：{st.error}")
                return 1

        if not reached:
            print("✗ 未到达 awaiting_publish")
            return 1

        print(f"✓ 到达 {reached.stage}（rework={reached.rework['count']}）")
        arts = {k: Path(v).name for k, v in reached.artifacts.items()}
        print("产物：", arts)

        print("⑥ approve publish → tick 归档")
        run("approve", "--run-id", rid, "--gate", "publish")
        tick()
        st = store.get(rid)
        assert st.stage == "archived" and st.status == "done", f"未归档：{st.stage}/{st.status}"
        print("✓ 全链路通过：new → archived")
        return 0
    finally:
        cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
