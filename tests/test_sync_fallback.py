"""``/Msg/Sync`` 入站兜底：首次不回放、先派发后前进游标、失败不抛。

WPP Cloud 没有 vendor WS；回调只是通知。这里钉住三个会静默丢消息/发错消息的
判据：

1. 首次（没有游标）只保存 ``KeyBuf``，**一条都不派发**；
2. 派发完成后才写新游标（at-least-once，靠 msg_id 去重兜底）；
3. 读游标失败 ≠ 没有游标：跳过本轮，绝不当成「首次」吞掉一窗消息。
"""

import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

sync_mod = importlib.import_module("astrbot_plugin_wpp_cloud.wpp_sync")


def _resp(items=None, key="K1"):
    data = {"CmdList": {"Count": len(items or []), "List": list(items or [])}}
    if key is not None:
        data["KeyBuf"] = {"buffer": key, "iLen": len(key)}
    return {"Code": 0, "Success": True, "Data": data}


class _Store:
    def __init__(self, path: Path):
        self.sync_path = path
        self.saved: list[str] = []

    def save_sync_cursor(self, key: str) -> None:
        self.saved.append(key)
        self.sync_path.write_text(json.dumps({"key": key}), encoding="utf-8")


class _Api:
    def __init__(self, responses):
        self.responses = list(responses)
        self.cursors: list[str] = []

    async def sync_message(self, synckey=""):
        self.cursors.append(synckey)
        return self.responses.pop(0) if self.responses else _resp([], key=None)


class TestSyncFallback(unittest.IsolatedAsyncioTestCase):
    def _make(self, responses, tmp: str, dispatched=None):
        store = _Store(Path(tmp) / "sync.json")
        api = _Api(responses)
        dispatched = dispatched if dispatched is not None else []

        async def dispatch(item):
            dispatched.append(item)

        fb = sync_mod.WppSyncFallback("acct", api, dispatch, store, interval_s=5)
        return fb, api, store, dispatched

    async def test_first_run_only_stores_cursor(self):
        with tempfile.TemporaryDirectory() as d:
            fb, api, store, dispatched = self._make(
                [_resp([{"MsgId": "1"}, {"MsgId": "2"}], key="K1")], d
            )
            n = await fb.trigger("first")
            self.assertEqual(n, 0)
            self.assertEqual(dispatched, [], "首次绝不能回放历史")
            self.assertEqual(store.saved, ["K1"])
            self.assertEqual(api.cursors, [""])

    async def test_incremental_dispatches_then_advances_cursor(self):
        with tempfile.TemporaryDirectory() as d:
            fb, api, store, dispatched = self._make(
                [
                    _resp([], key="K1"),
                    _resp([{"MsgId": "m1"}, {"MsgId": "m2"}], key="K2"),
                ],
                d,
            )
            await fb.trigger("first")
            n = await fb.trigger("callback")
            self.assertEqual(n, 2)
            self.assertEqual([x["MsgId"] for x in dispatched], ["m1", "m2"])
            self.assertEqual(store.saved, ["K1", "K2"])
            self.assertEqual(api.cursors, ["", "K1"])

    async def test_dispatch_failure_holds_cursor_for_replay(self):
        with tempfile.TemporaryDirectory() as d:
            store = _Store(Path(d) / "sync.json")
            api = _Api(
                [
                    _resp([], key="K1"),
                    _resp([{"MsgId": "bad"}, {"MsgId": "ok"}], key="K2"),
                ]
            )
            seen = []

            async def dispatch(item):
                if item["MsgId"] == "bad":
                    raise RuntimeError("boom")
                seen.append(item["MsgId"])

            fb = sync_mod.WppSyncFallback("acct", api, dispatch, store, interval_s=5)
            await fb.trigger("first")
            await fb.trigger("callback")
            self.assertEqual(seen, ["ok"])
            # 有派发失败就不推进游标: 下一轮重拉这一页, 成功的条目靠 msg_id 去重挡住。
            # 推进游标会让这条消息永久丢失, 而 msg_id 去重已经保证重放安全。
            self.assertEqual(store.saved[-1], "K1", "派发失败时游标必须保持在原处")

    async def test_read_failure_skips_instead_of_treating_as_first_run(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sync.json"
            path.mkdir()  # 目录 ⇒ read_text 抛 IsADirectoryError
            store = _Store(path)
            api = _Api([_resp([{"MsgId": "1"}], key="K9")])
            fb = sync_mod.WppSyncFallback("acct", api, lambda i: None, store, interval_s=5)
            n = await fb.trigger("broken")
            self.assertEqual(n, 0)
            self.assertEqual(api.cursors, [], "读游标失败时本轮不应请求 /Msg/Sync")

    async def test_missing_keybuf_leaves_cursor_alone(self):
        with tempfile.TemporaryDirectory() as d:
            fb, api, store, _ = self._make(
                [_resp([], key="K1"), _resp([{"MsgId": "m"}], key=None)], d
            )
            await fb.trigger("first")
            await fb.trigger("callback")
            self.assertEqual(store.saved, ["K1"], "空 KeyBuf 不许覆盖已有游标")

    async def test_spawn_is_non_blocking_and_in_flight_guard_works(self):
        with tempfile.TemporaryDirectory() as d:
            gate = asyncio.Event()
            store = _Store(Path(d) / "sync.json")
            api = _Api([_resp([], key="K1")])

            async def slow_sync(synckey=""):
                api.cursors.append(synckey)
                await gate.wait()
                return _resp([], key="K1")

            api.sync_message = slow_sync  # type: ignore[assignment]
            fb = sync_mod.WppSyncFallback("acct", api, lambda i: None, store, interval_s=5)
            fb.spawn("a")
            # trigger 先 await to_thread 读游标，至少要让出一个完整调度周期。
            for _ in range(50):
                if api.cursors:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(api.cursors), 1)
            fb.spawn("b")  # in-flight ⇒ 跳过
            await asyncio.sleep(0.05)
            self.assertEqual(len(api.cursors), 1)
            gate.set()
            await asyncio.gather(*list(fb._tasks))


class TestExtractors(unittest.TestCase):
    def test_items_shapes(self):
        self.assertEqual(
            [x["a"] for x in sync_mod.extract_sync_items(
                {"Data": {"CmdList": {"List": [{"a": 1}, "bad"]}}}
            )],
            [1],
        )
        self.assertEqual(
            sync_mod.extract_sync_items({"Data": {"CmdList": [{"a": 2}]}}),
            [{"a": 2}],
        )
        self.assertEqual(sync_mod.extract_sync_items({"Data": {"List": [{"a": 3}]}}),
                         [{"a": 3}])
        self.assertEqual(sync_mod.extract_sync_items(None), [])

    def test_key_shapes(self):
        self.assertEqual(
            sync_mod.extract_sync_key({"Data": {"KeyBuf": {"buffer": "K"}}}), "K"
        )
        self.assertEqual(sync_mod.extract_sync_key({"Data": {"KeyBuf": "K2"}}), "K2")
        self.assertEqual(sync_mod.extract_sync_key({"Data": {}}), "")


if __name__ == "__main__":
    unittest.main()
