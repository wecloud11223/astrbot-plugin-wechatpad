"""WPP Cloud 入站增量同步（``/Msg/Sync``）。

WPP 托管模式下没有 vendor WebSocket，也没有 vendor 回调注册：

* **通知通道**：WPP 控制台把账号的「普通回调」指向本插件。回调通常只带
  ``sync_message`` 通知（可能没有消息体）⇒ 收到后立即触发一次 ``/Msg/Sync``。
* **兜底通道**：本模块每 ``interval_s`` 秒主动拉一次 ``/Msg/Sync``，保证回调
  丢失/网络抖动时消息仍能补回。
* **首次不回放**：库里没有游标时只保存厂商返回的 ``KeyBuf`` 作为基线，一条都
  不派发。AstrBot 的去重缓冲会随进程重启清空，回放历史会触发机器人回复旧消息。
* **先派发、后前进游标**：派发完一批才写游标；进程中途退出时下一轮会重新拉到
  同一批（at-least-once），由 ``WppAccount._process_src`` 的 msg_id 去重兜住。

失败只记录日志、不抛异常：同步是入站旁路，不能把整个平台拖停。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from astrbot import logger

DEFAULT_INTERVAL_S = 60.0
REASON_OPEN = "sync-open"
REASON_PUSH = "wpp-callback"
REASON_TIMER = "fallback-timer"

#: 读游标失败 ≠ 库里没有游标。前者跳过本轮，后者才走「首次只落基线」。
_READ_FAILED = object()


def extract_sync_items(resp: Mapping[str, Any] | None) -> list[dict]:
    """从 ``/Msg/Sync`` 响应取消息列表（``Data.CmdList.List`` 及已知变体）。"""
    data = (resp or {}).get("Data")
    if not isinstance(data, Mapping):
        return []
    cmd = data.get("CmdList")
    if isinstance(cmd, Mapping):
        arr = cmd.get("List")
        if isinstance(arr, list):
            return [x for x in arr if isinstance(x, dict)]
        return []
    if isinstance(cmd, list):
        return [x for x in cmd if isinstance(x, dict)]
    arr = data.get("List")
    if isinstance(arr, list):
        return [x for x in arr if isinstance(x, dict)]
    return []


def extract_sync_key(resp: Mapping[str, Any] | None) -> str:
    """从 ``/Msg/Sync`` 响应取新游标（``Data.KeyBuf.buffer``）。"""
    data = (resp or {}).get("Data")
    if not isinstance(data, Mapping):
        return ""
    kb = data.get("KeyBuf")
    if isinstance(kb, str):
        return kb
    if isinstance(kb, Mapping):
        buf = kb.get("buffer")
        return buf if isinstance(buf, str) else ""
    return ""


class WppSyncFallback:
    """单账号的 ``/Msg/Sync`` 轮询器。

    ``dispatch`` 接收一条厂商消息（``CmdList.List`` 的元素），返回的协程在
    游标前进之前必须完成。``store`` 需要提供 ``sync_path`` /
    ``load_sync_cursor()`` / ``save_sync_cursor(key)``（见 ``AccountStore``）。
    """

    def __init__(
        self,
        account_id: str,
        api: Any,
        dispatch: Callable[[dict], Awaitable[None]],
        store: Any,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self.account_id = account_id
        self.api = api
        self.dispatch = dispatch
        self.store = store
        self.interval_s = max(5.0, float(interval_s or DEFAULT_INTERVAL_S))
        self._timer_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._in_flight = False
        #: 拉取在途时收到的触发理由；本轮结束后据此补拉一趟（见 trigger）。
        self._pending_reason = ""
        self._stopped = False

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """启动周期任务，并立即拉一次（首次只建立游标）。"""
        self._stopped = False
        if self._timer_task is None:
            self._timer_task = asyncio.create_task(
                self._timer_loop(), name=f"wpp-sync-{self.account_id}"
            )
        self.spawn(REASON_OPEN)

    async def stop(self) -> None:
        """取消定时器与未完成的拉取。"""
        self._stopped = True
        task, self._timer_task = self._timer_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        pending = [t for t in self._tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    async def _timer_loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.trigger(REASON_TIMER)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP:{self.account_id} SYNC] 定时拉取失败: {e}")

    def spawn(self, reason: str) -> None:
        """非阻塞触发一次拉取（回调处理器里绝不能 await HTTP）。"""
        try:
            task = asyncio.create_task(
                self.trigger(reason), name=f"wpp-sync-{self.account_id}-{reason}"
            )
        except RuntimeError as e:
            logger.warning(f"[WPP:{self.account_id} SYNC] 无法触发（无事件循环）: {e}")
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ 拉取
    async def trigger(self, reason: str = "") -> int:
        """拉一趟 ``/Msg/Sync``；返回派发条数。永不抛异常。"""
        if self._in_flight:
            # 直接丢弃会让"低延迟通道"退化成等 60s 定时器；记下来等本轮结束立刻补一趟。
            self._pending_reason = reason or self._pending_reason or "push"
            logger.debug(f"[WPP:{self.account_id} SYNC] 已有拉取在跑，本轮结束后补拉（{reason}）")
            return 0
        self._in_flight = True
        try:
            prev = await asyncio.to_thread(self._read_cursor)
            if prev is _READ_FAILED:
                logger.warning(
                    f"[WPP:{self.account_id} SYNC] 读游标失败，本轮跳过（{reason}）"
                )
                return 0
            resp = await self.api.sync_message(prev or "")
            items = extract_sync_items(resp)
            new_key = extract_sync_key(resp)

            if not prev:
                # 首次：只落基线，绝不回放历史。
                if new_key:
                    await asyncio.to_thread(self.store.save_sync_cursor, new_key)
                    logger.info(
                        f"[WPP:{self.account_id} SYNC] 首次同步：已建立游标"
                        f"（厂商返回 {len(items)} 条历史，已跳过不回放）"
                    )
                else:
                    logger.warning(
                        f"[WPP:{self.account_id} SYNC] 首次同步未返回 KeyBuf，"
                        "游标仍为空（回调通知仍会触发重试）"
                    )
                return 0

            if items:
                logger.debug(
                    f"[WPP:{self.account_id} SYNC] 拉回 {len(items)} 条（{reason}）"
                )
            failed: list[str] = []
            for item in items:
                try:
                    await self.dispatch(item)
                except Exception as e:  # noqa: BLE001
                    mid = str(
                        (item or {}).get("MsgId")
                        or (item or {}).get("msgId")
                        or (item or {}).get("NewMsgId")
                        or "?"
                    )
                    failed.append(mid)
                    logger.warning(
                        f"[WPP:{self.account_id} SYNC] 派发失败 id={mid}: {e}"
                    )
            if failed:
                # 有派发失败就**不动**游标：下一轮会把这一页重新拉回来，
                # 已成功的那几条靠 msg_id 去重挡住，既不会重复回复也不会永久丢消息。
                logger.warning(
                    f"[WPP:{self.account_id} SYNC] {len(failed)} 条派发失败，游标保持在"
                    f"原处等待重拉: {','.join(failed[:10])}"
                )
            elif new_key:
                await asyncio.to_thread(self.store.save_sync_cursor, new_key)
            else:
                logger.debug(
                    f"[WPP:{self.account_id} SYNC] 未返回 KeyBuf，游标不动（{reason}）"
                )
            return len(items)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP:{self.account_id} SYNC] /Msg/Sync 失败（{reason}）: {e}")
            return 0
        finally:
            self._in_flight = False
            pending, self._pending_reason = self._pending_reason, ""
            if pending and not self._stopped:
                # 在途期间来的推送通知：立刻补一趟，别等 60s 定时器。
                asyncio.get_running_loop().call_soon(self.spawn, f"{pending}+补拉")

    # ------------------------------------------------------------------ 游标
    def _read_cursor(self) -> object:
        """同步读文件（调用方用 to_thread 包住）；区分「缺失」与「读失败」。"""
        path = getattr(self.store, "sync_path", None)
        if path is None:
            return self.store.load_sync_cursor() or None
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:  # noqa: BLE001
            return _READ_FAILED
        if isinstance(data, dict):
            return str(data.get("key") or "") or None
        return _READ_FAILED


def max_skew_seconds() -> int:
    """回调签名允许的时间偏差（秒，0 = 关闭）。"""
    raw = os.environ.get("WPP_WEBHOOK_MAX_SKEW_SEC", "").strip()
    try:
        return max(0, int(raw)) if raw else 300
    except ValueError:
        return 300


__all__ = [
    "DEFAULT_INTERVAL_S",
    "REASON_OPEN",
    "REASON_PUSH",
    "REASON_TIMER",
    "WppSyncFallback",
    "extract_sync_items",
    "extract_sync_key",
    "max_skew_seconds",
]
