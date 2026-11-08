#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""loop.py 的可运维语义单元测试（T2/T4）：

- T2a：一次 tick 默认只推一步，`--steps` 才多步；
- T2c：坏 state.json 在 tick 里告警但不改退出码，status 返回 1；
- T2d：文件锁并发时第二个 tick 直接跳过（退出码 0）；
- T4a：policy 写 publish=true 也不能推进到 published；
- T3c：artifacts 写侧归一化为相对 case_dir 的 posix 路径，composer 失败原地重试；
- T4b：tick 末尾推闸门通知，--no-notify 可关，12h 去重。

全部使用临时 runs/ 目录，不写真实 orchestrator/runs/ 与 policy.json。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import loop, notify  # noqa: E402
from orchestrator.agents.base import Result  # noqa: E402
from orchestrator.state import Source  # noqa: E402


class FakeAgent:
    """可控 agent：按指定 status 返回，避免单元测试触发真实 agent/网络。"""

    def __init__(self, name: str, to: str | None = None, *, status: str = "ok",
                 reason: str = "", artifacts=None):
        self.name = name
        self.to = to
        self.status = status
        self.reason = reason
        self._artifacts = artifacts
        self.calls = 0

    def run(self, ctx) -> Result:
        self.calls += 1
        if self.status == "rework":
            return Result.rework(self.reason or "打回")
        if self.status == "blocked":
            return Result.blocked(self.reason or "卡住")
        arts = self._artifacts(ctx) if callable(self._artifacts) else dict(self._artifacts or {})
        return Result.ok(note=f"{self.name} ok", artifacts=arts, to=self.to)


class LoopCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "runs").mkdir()
        self.base = self.root / "base"
        (self.base / "case").mkdir(parents=True)

        patches = [
            patch.object(loop, "HERE", self.root),
            patch.object(loop, "BASE", self.base),
            patch.object(loop, "load_policy", lambda: {}),
            # 测试机可能配了真实 webhook：默认清空，通知相关用例再单独覆盖
            patch.dict(os.environ, {"FEISHU_WEBHOOK": "", "GZH_FEISHU_WEBHOOK": ""}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        self.store = loop.RunStore(self.root / "runs")

    # -------------------------------------------------- 工具
    def make_run(self, stage: str = "new", rid: str = "abc123", *,
                 status: str = "active", title: str = "测试嘉宾",
                 gates: dict | None = None, case_dir: str = "case") -> None:
        run = self.store.create(rid, Source(title=title), case_dir)
        run.stage = stage
        run.status = status
        if gates is not None:
            run.gates = dict(gates)
        self.store.save(run)

    def tick(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = loop.main(["tick", "--skip-ingest", *argv])
        return code, buf.getvalue()

    def status(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = loop.main(["status", *argv])
        return code, buf.getvalue()


# ---------------------------------------------------------------- T2a


class TestSteps(LoopCase):
    def _agents(self):
        return {
            "ledger": FakeAgent("ledger", "ledgered"),
            "researcher": FakeAgent("researcher", "researched"),
            "writer": FakeAgent("writer", "drafted"),
        }

    def test_default_is_one_step(self):
        agents = self._agents()
        self.make_run(stage="transcribed")
        with patch.object(loop, "AGENTS", agents):
            code, out = self.tick("--run-id", "abc123")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.get("abc123").stage, "ledgered")
        self.assertEqual(agents["ledger"].calls, 1)
        self.assertEqual(agents["researcher"].calls, 0, "默认只应推一步")

    def test_steps_two(self):
        agents = self._agents()
        self.make_run(stage="transcribed")
        with patch.object(loop, "AGENTS", agents):
            code, out = self.tick("--run-id", "abc123", "--steps", "2")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.get("abc123").stage, "researched")

    def test_steps_stops_when_no_agent(self):
        """多步也不能越过没有 agent 的阶段。"""
        self.make_run(stage="transcribed")
        with patch.object(loop, "AGENTS", {"ledger": FakeAgent("ledger", "ledgered")}):
            code, out = self.tick("--run-id", "abc123", "--steps", "5")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.get("abc123").stage, "ledgered")


# ---------------------------------------------------------------- T2c


class TestBrokenState(LoopCase):
    def _break(self, run_id: str = "broken1") -> None:
        d = self.store.root / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text("{不是 JSON", encoding="utf-8")

    def test_tick_warns_but_exits_zero(self):
        self.make_run(stage="transcribed")
        self._break()
        with patch.object(loop, "AGENTS", {}):     # 不跑真实 agent
            code, out = self.tick("--run-id", "abc123")
        self.assertEqual(code, 0, "坏 state.json 不应拖垮定时任务")
        self.assertIn("[warn] state.json 解析失败", out)
        self.assertIn("broken=1", out)
        self.assertEqual(self.store.get("abc123").stage, "transcribed",
                         "其他 run 不受影响")

    def test_status_exits_one(self):
        self.make_run(stage="transcribed")
        self._break()
        code, out = self.status()
        self.assertEqual(code, 1)
        self.assertIn("[warn] state.json 解析失败", out)

    def test_status_json_has_usage_and_broken(self):
        self.make_run(stage="transcribed")
        run = self.store.get("abc123")
        run.add_usage({"input": 10, "output": 5, "total": 15, "calls": 1})
        self.store.save(run)
        code, out = self.status("--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("runs", data)
        self.assertIn("usage", data)
        self.assertIn("broken", data)
        self.assertEqual(data["runs"][0]["usage"]["total"], 15)
        self.assertEqual(data["usage"]["today"]["total"], 15)


# ---------------------------------------------------------------- T2d


class TestTickLock(LoopCase):
    def test_second_tick_skips(self):
        with loop.TickLock(self.store.root / ".lock") as locked:
            self.assertTrue(locked)
            code, out = self.tick()     # 同一进程第二个 fd 也会被 flock 拒绝
            self.assertEqual(code, 0)
            self.assertIn("另一个 tick 正在运行", out)
        code, out = self.tick()         # 锁释放后恢复正常
        self.assertEqual(code, 0)
        self.assertNotIn("另一个 tick 正在运行", out)

    def test_no_fcntl_is_noop(self):
        with patch.object(loop, "fcntl", None):
            with loop.TickLock(self.store.root / ".lock") as locked:
                self.assertTrue(locked)
            # no-op 分支不建锁文件
            self.assertFalse((self.store.root / ".lock").exists())


# ---------------------------------------------------------------- T4a


class TestPublishNeverAuto(LoopCase):
    def test_tick_with_policy_publish_true_still_waits(self):
        self.make_run(stage="awaiting_publish")
        policy = {"auto_approve": {"topic": True, "publish": True}}
        with patch.object(loop, "load_policy", lambda: policy):
            code, out = self.tick("--run-id", "abc123")
        self.assertEqual(code, 0, out)
        run = self.store.get("abc123")
        self.assertEqual(run.stage, "awaiting_publish", "发布永不能自动推进")
        self.assertFalse(run.gates["publish"])
        self.assertEqual(run.status, "active")

    def test_apply_auto_approve_ignores_publish(self):
        self.make_run(stage="awaiting_publish")
        run = self.store.get("abc123")
        changed = loop.apply_auto_approve(
            self.store, [run], {"auto_approve": {"publish": True}})
        self.assertEqual(changed, [])
        self.assertFalse(run.gates["publish"])
        self.assertEqual(run.stage, "awaiting_publish")

    def test_try_auto_gate_publish_returns_false(self):
        self.make_run(stage="awaiting_publish")
        run = self.store.get("abc123")
        self.assertFalse(loop.try_auto_gate(
            self.store, run, {"auto_approve": {"publish": True}}))
        self.assertEqual(run.gates["publish"], False)

    def test_topic_auto_approve_still_works(self):
        self.make_run(stage="triaged")
        run = self.store.get("abc123")
        self.assertTrue(loop.try_auto_gate(
            self.store, run, {"auto_approve": {"topic": True}}))
        self.assertTrue(run.gates["topic"])


# ---------------------------------------------------------------- T3c


class TestArtifactsNormalized(LoopCase):
    def test_inside_case_dir_becomes_relative_posix(self):
        inside = self.base / "case" / "台账.md"
        outside = self.root / "外部.txt"
        agent = FakeAgent("ledger", "ledgered", artifacts={
            "ledger": str(inside),
            "sub": "子目录/文件.md",
            "ext": str(outside),
        })
        self.make_run(stage="transcribed")
        with patch.object(loop, "AGENTS", {"ledger": agent}):
            code, out = self.tick("--run-id", "abc123")
        self.assertEqual(code, 0, out)
        arts = self.store.get("abc123").artifacts
        self.assertEqual(arts["ledger"], "台账.md")
        self.assertEqual(arts["sub"], "子目录/文件.md")
        self.assertEqual(arts["ext"], str(outside), "case_dir 之外的路径原样保留")

    def test_normalize_artifacts_helper(self):
        case = self.base / "case"
        self.assertEqual(loop.normalize_artifacts({"a": str(case / "x.md")}, case),
                         {"a": "x.md"})
        self.assertEqual(loop.normalize_artifacts({"a": "y/z.md"}, case),
                         {"a": "y/z.md"})
        self.assertEqual(loop.normalize_artifacts({}, case), {})


class TestReworkRouting(LoopCase):
    """T3c：按 agent 名分流打回，而不是按 stage 一刀切。"""

    def test_critic_rework_rewinds_researched(self):
        self.make_run(stage="drafted")
        agent = FakeAgent("critic", status="rework", reason="标题是话题词")
        with patch.object(loop, "AGENTS", {"critic": agent}):
            code, out = self.tick("--run-id", "abc123")
        run = self.store.get("abc123")
        self.assertEqual(code, 0, out)
        self.assertEqual(run.stage, "researched")
        self.assertEqual(run.rework["count"], 1)

    def test_gatekeeper_rework_rewinds_critiqued(self):
        self.make_run(stage="composed")
        agent = FakeAgent("gatekeeper", status="rework", reason="质检未通过")
        with patch.object(loop, "AGENTS", {"gatekeeper": agent}):
            code, out = self.tick("--run-id", "abc123")
        run = self.store.get("abc123")
        self.assertEqual(code, 0, out)
        self.assertEqual(run.stage, "critiqued")

    def test_composer_rework_retries_in_place(self):
        self.make_run(stage="critiqued")
        agent = FakeAgent("composer", status="rework", reason="流水线 FAIL")
        with patch.object(loop, "AGENTS", {"composer": agent}):
            code, out = self.tick("--run-id", "abc123")
        run = self.store.get("abc123")
        self.assertEqual(code, 0, out)
        self.assertEqual(run.stage, "critiqued", "composer 失败应原地重试")
        self.assertEqual(run.rework["count"], 1)


# ---------------------------------------------------------------- T4b


class TestTickNotify(LoopCase):
    def test_tick_pushes_waiting_gate(self):
        self.make_run(stage="awaiting_publish")
        calls: list[tuple[str, str]] = []
        env = {"FEISHU_WEBHOOK": "https://hook.example/x",
               "GZH_FEISHU_WEBHOOK": ""}
        with patch.dict(os.environ, env), \
                patch.object(notify, "_post", lambda url, text: calls.append((url, text))):
            code, out = self.tick("--run-id", "abc123")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 1, "等待闸门应推一条通知")
        self.assertEqual(calls[0][0], "https://hook.example/x")
        self.assertIn("abc123", calls[0][1])
        self.assertIn("发布", calls[0][1])

    def test_no_notify_flag(self):
        self.make_run(stage="awaiting_publish")
        calls: list[str] = []
        env = {"FEISHU_WEBHOOK": "https://hook.example/x",
               "GZH_FEISHU_WEBHOOK": ""}
        with patch.dict(os.environ, env), \
                patch.object(notify, "_post", lambda url, text: calls.append(text)):
            code, out = self.tick("--run-id", "abc123", "--no-notify")
        self.assertEqual(code, 0, out)
        self.assertEqual(calls, [])

    def test_webhook_failure_does_not_break_tick(self):
        self.make_run(stage="awaiting_publish")
        env = {"FEISHU_WEBHOOK": "https://hook.example/x",
               "GZH_FEISHU_WEBHOOK": ""}

        def boom(url, text):
            raise RuntimeError("webhook 挂了")

        with patch.dict(os.environ, env), patch.object(notify, "_post", boom):
            code, out = self.tick("--run-id", "abc123")
        self.assertEqual(code, 0, "通知失败不能拖垮 tick")
        self.assertIn("[warn] 闸门通知发送失败", out)


# ---------------------------------------------------------------- T4b：notify 单元


class NotifyCase(LoopCase):
    ENV_EMPTY = {"FEISHU_WEBHOOK": "", "GZH_FEISHU_WEBHOOK": ""}
    ENV_HOOK = {"FEISHU_WEBHOOK": "https://hook.example/x",
                "GZH_FEISHU_WEBHOOK": ""}

    def notify(self, *, dry_run: bool = False) -> bool:
        return notify.notify_gates(self.store, self.store.all(), dry_run=dry_run)

    def seen_file(self) -> Path:
        return self.store.root / ".notified.json"


class TestNotifyUnit(NotifyCase):
    def test_no_webhook_silent(self):
        self.make_run(stage="awaiting_publish")
        with patch.dict(os.environ, self.ENV_EMPTY):
            self.assertFalse(self.notify())
        self.assertFalse(self.seen_file().exists())

    def test_gzh_webhook_env_alias(self):
        self.make_run(stage="awaiting_publish")
        calls: list[str] = []
        env = {"FEISHU_WEBHOOK": "", "GZH_FEISHU_WEBHOOK": "https://hook.example/gzh"}
        with patch.dict(os.environ, env), \
                patch.object(notify, "_post", lambda url, text: calls.append(url)):
            self.assertTrue(self.notify())
        self.assertEqual(calls, ["https://hook.example/gzh"])

    def test_nothing_to_notify(self):
        self.make_run(stage="transcribed")
        with patch.dict(os.environ, self.ENV_HOOK), \
                patch.object(notify, "_post", lambda u, t: self.fail("不应推送")):
            self.assertFalse(self.notify())

    def test_dedupe_within_12h(self):
        self.make_run(stage="awaiting_publish")
        calls: list[str] = []
        with patch.dict(os.environ, self.ENV_HOOK), \
                patch.object(notify, "_post", lambda url, text: calls.append(text)):
            self.assertTrue(self.notify())
            self.assertFalse(self.notify(), "12h 内同签名不应重复推")
        self.assertEqual(len(calls), 1)
        seen = json.loads(self.seen_file().read_text(encoding="utf-8"))
        self.assertTrue(any("abc123" in k for k in seen), seen)

    def test_repush_after_12h(self):
        self.make_run(stage="awaiting_publish")
        calls: list[str] = []
        with patch.dict(os.environ, self.ENV_HOOK), \
                patch.object(notify, "_post", lambda url, text: calls.append(text)):
            self.assertTrue(self.notify())
            seen = json.loads(self.seen_file().read_text(encoding="utf-8"))
            stale = {k: time.time() - 13 * 3600 for k in seen}
            self.seen_file().write_text(json.dumps(stale), encoding="utf-8")
            self.assertTrue(self.notify())
        self.assertEqual(len(calls), 2)

    def test_stage_change_repushes(self):
        self.make_run(stage="awaiting_publish", rid="r1")
        calls: list[str] = []
        with patch.dict(os.environ, self.ENV_HOOK), \
                patch.object(notify, "_post", lambda url, text: calls.append(text)):
            self.assertTrue(self.notify())
            run = self.store.get("r1")
            run.status = "blocked"
            run.error = "缺转写"
            self.store.save(run)
            self.assertTrue(self.notify(), "阶段/状态变化是新签名")
        self.assertEqual(len(calls), 2)

    def test_dry_run_no_post_no_write(self):
        self.make_run(stage="awaiting_publish")
        with patch.dict(os.environ, self.ENV_HOOK), \
                patch.object(notify, "_post", lambda u, t: self.fail("dry_run 不应推送")):
            self.assertFalse(self.notify(dry_run=True))
        self.assertFalse(self.seen_file().exists())

    def test_post_failure_warns_and_returns_false(self):
        self.make_run(stage="awaiting_publish")
        buf = io.StringIO()
        with patch.dict(os.environ, self.ENV_HOOK), \
                patch.object(notify, "_post", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stdout(buf):
            self.assertFalse(self.notify())
        self.assertIn("[warn] 闸门通知发送失败", buf.getvalue())
        self.assertFalse(self.seen_file().exists(), "失败不应写去重记录")

    def test_blocked_run_included(self):
        self.make_run(stage="composed", status="blocked")
        run = self.store.get("abc123")
        run.error = "渲染验收未执行"
        self.store.save(run)
        calls: list[str] = []
        with patch.dict(os.environ, self.ENV_HOOK), \
                patch.object(notify, "_post", lambda url, text: calls.append(text)):
            self.assertTrue(self.notify())
        self.assertIn("blocked", calls[0])
        self.assertIn("渲染验收未执行", calls[0])


class TestPostPayload(unittest.TestCase):
    """`_post` 的 HTTP 层：飞书 JSON 结构、5s 超时、code≠0 判失败。"""

    def _fake_resp(self, body: dict):
        class _Resp:
            status = 200

            def read(self):
                return json.dumps(body).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()

    def test_payload_shape_and_timeout(self):
        with patch("orchestrator.notify.urllib.request.urlopen",
                   return_value=self._fake_resp({"code": 0})) as m:
            notify._post("https://hook.example/x", "测试消息")
        req = m.call_args.args[0]
        self.assertEqual(req.method, "POST")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body, {"msg_type": "text", "content": {"text": "测试消息"}})
        self.assertEqual(m.call_args.kwargs.get("timeout"), notify.TIMEOUT)
        self.assertLessEqual(notify.TIMEOUT, 5)

    def test_feishu_code_nonzero_raises(self):
        with patch("orchestrator.notify.urllib.request.urlopen",
                   return_value=self._fake_resp({"code": 19021, "msg": "keyword"})):
            with self.assertRaises(RuntimeError):
                notify._post("https://hook.example/x", "测试")


if __name__ == "__main__":
    unittest.main(verbosity=2)
