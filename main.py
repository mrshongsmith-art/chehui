import asyncio
import contextvars
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register

from . import config

ORIGINAL_METHOD_ATTR = "_astrbot_plugin_original_call_action"

# 配置常量
RESCUE_INTERVAL_BASE = 25
RESCUE_INTERVAL_STEP = 10
RESCUE_MAX_RETRIES = 3
HISTORY_MATCH_WINDOW = 120
RETRACT_ACTIONS = ("delete_msg", "delete_message", "recall")
CURRENT_RETRACTION: contextvars.ContextVar["PendingRetraction | None"] = (
    contextvars.ContextVar("chehui_current_retraction", default=None)
)


@dataclass(frozen=True, slots=True)
class RetractionRule:
    """撤回规则"""

    keywords: tuple[str, ...]
    delay: int


@dataclass(frozen=True, slots=True)
class PendingRetraction:
    """绑定到当前事件上下文的一次性撤回请求"""

    context_key: str
    delay: int
    trigger_text: str
    created_at: float


def _parse_rules(raw_rules: list[dict]) -> list[RetractionRule]:
    """解析配置规则为 RetractionRule 对象"""
    rules = []
    for rule in raw_rules:
        kw = rule.get("keywords", ())
        if isinstance(kw, (list, set, tuple)):
            keywords = tuple(kw)
        else:
            keywords = ()
        delay = int(rule.get("delay", 0))
        if keywords and delay > 0:
            rules.append(RetractionRule(keywords=keywords, delay=delay))
    return rules


@register(
    "根据关键词撤回插件(v2.2)",
    "樱小路真寻",
    "v2.2: 修复会话级待撤回导致的误撤回问题。",
    "2.2",
    "None",
)
class SelfMessageRetriever(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._rules = _parse_rules(config.retraction_rules)
        self._scheduled_ids: set[str] = set()
        self._bot_ids: dict[int, int] = {}
        self._active_tasks: set[asyncio.Task] = set()
        self._shutdown = False

        logger.info(f"SelfMessageRetriever v2.2 已加载，共 {len(self._rules)} 条规则。")
        asyncio.create_task(self._initialize_hooks())

    async def _initialize_hooks(self):
        await asyncio.sleep(5)
        if hasattr(self.context, "bots"):
            for bot in self.context.bots.values():  # type: ignore
                await self._wrap_bot_action(bot)

    @staticmethod
    def _unwrap_method(method: Any) -> Any:
        """剥离装饰器，获取原始函数"""
        while hasattr(method, "__wrapped__"):
            method = method.__wrapped__
        if hasattr(method, "func"):
            method = method.func
        return method

    async def _get_bot_id(self, bot: Any, method: Callable) -> int:
        """获取 Bot 真实 ID，带缓存"""
        bot_key = id(bot)
        if bot_key in self._bot_ids:
            return self._bot_ids[bot_key]

        try:
            info = await method("get_login_info")
            if info and "user_id" in info:
                real_id = int(info["user_id"])
                self._bot_ids[bot_key] = real_id
                logger.info(f"SelfMessageRetriever: Bot ID 校验成功: {real_id}")
                return real_id
        except Exception:
            pass
        return 0

    def _create_task(self, coro) -> asyncio.Task:
        """创建并跟踪异步任务"""
        task = asyncio.create_task(coro)
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        return task

    async def _wrap_bot_action(self, bot: Any):
        if not (hasattr(bot, "api") and hasattr(bot.api, "call_action")):
            return

        current = getattr(bot.api, ORIGINAL_METHOD_ATTR, None) or bot.api.call_action
        original = self._unwrap_method(current)
        setattr(bot.api, ORIGINAL_METHOD_ATTR, original)

        async def wrapper(*args, **kwargs):
            action = args[0] if args else kwargs.get("action", "")
            if not isinstance(action, str) or not action.startswith("send"):
                return await original(*args, **kwargs)

            payloads = (
                dict(args[1]) if len(args) > 1 and isinstance(args[1], dict) else {}
            )
            payloads.update({k: v for k, v in kwargs.items() if k != "action"})

            ctx_key = self._get_context_key(payloads=payloads)
            pending = CURRENT_RETRACTION.get()
            delay = None
            if pending and pending.context_key == ctx_key:
                # 撤回规则只消费当前触发事件中的第一条发送消息，避免误伤同会话内其它消息。
                delay = pending.delay
                CURRENT_RETRACTION.set(None)

            if delay is None:
                return await original(*args, **kwargs)

            target_id, is_group, text, has_img = self._analyze_payload(payloads)
            self_id = await self._get_bot_id(bot, original)

            try:
                result = await original(*args, **kwargs)
                msg_id = self._extract_message_id(result)
            except Exception as e:
                logger.warning(f"SelfMessageRetriever: 发送超时({e})，启动救援...")
                self._create_task(
                    self._rescue_and_schedule(
                        original, self_id, is_group, target_id, text, has_img, delay
                    )
                )
                return None

            if msg_id and str(msg_id) not in self._scheduled_ids:
                logger.info(f"SelfMessageRetriever: 锁定ID [{msg_id}]，{delay}s 后撤回")
                self._create_task(self._schedule_retraction(original, msg_id, delay))
            elif not msg_id:
                self._create_task(
                    self._rescue_and_schedule(
                        original, self_id, is_group, target_id, text, has_img, delay
                    )
                )

            return result

        wrapper.__wrapped__ = original
        bot.api.call_action = wrapper
        logger.info("SelfMessageRetriever: API 挂载完成。")

    async def _rescue_and_schedule(
        self,
        method: Callable,
        self_id: int,
        is_group: bool,
        target_id: int,
        text: str,
        has_img: bool,
        delay: int,
    ):
        for i in range(RESCUE_MAX_RETRIES):
            if self._shutdown:
                logger.info("SelfMessageRetriever: 插件关闭，取消救援任务")
                return

            await asyncio.sleep(RESCUE_INTERVAL_BASE + i * RESCUE_INTERVAL_STEP)

            if self._shutdown:
                return

            msg_id = await self._find_message_in_history(
                method, self_id, is_group, target_id, text, has_img
            )

            if msg_id:
                logger.info(
                    f"SelfMessageRetriever: 救援成功 ID [{msg_id}]，{delay}s 后撤回"
                )
                await self._schedule_retraction(method, msg_id, delay)
                return

            logger.info(f"SelfMessageRetriever: 救援第 {i + 1} 轮未找到，继续...")

        logger.error("SelfMessageRetriever: 救援失败，未找到匹配消息。")

    @staticmethod
    def _get_context_key(
        event: AstrMessageEvent | None = None, payloads: dict | None = None
    ) -> str | None:
        if event:
            if gid := event.get_group_id():
                return f"group_{gid}"
            if uid := event.get_sender_id():
                return f"private_{uid}"
        elif payloads:
            if gid := payloads.get("group_id"):
                return f"group_{gid}"
            if uid := payloads.get("user_id"):
                return f"private_{uid}"
        return None

    @filter.event_message_type(EventMessageType.ALL, priority=1)
    async def monitor_commands(self, event: AstrMessageEvent):
        try:
            msg = event.message_str.strip()
            ctx = self._get_context_key(event=event)
        except AttributeError:
            return

        if not ctx:
            return

        for rule in self._rules:
            if msg.startswith(rule.keywords):
                if rule.delay > 0:
                    pending = PendingRetraction(
                        context_key=ctx,
                        delay=rule.delay,
                        trigger_text=msg,
                        created_at=time.time(),
                    )
                    token = CURRENT_RETRACTION.set(pending)
                    event.set_extra("_chehui_retraction_token", token)
                    logger.info(
                        f"SelfMessageRetriever: [匹配] '{ctx}' 触发 '{msg}'，绑定本次事件(Delay:{rule.delay})"
                    )
                return

    @filter.after_message_sent()
    async def clear_retraction_context(self, event: AstrMessageEvent):
        token = event.get_extra("_chehui_retraction_token")
        if token is None:
            return
        try:
            CURRENT_RETRACTION.reset(token)
        except Exception:
            CURRENT_RETRACTION.set(None)

    @staticmethod
    def _analyze_payload(payloads: dict) -> tuple[int, bool, str, bool]:
        target_id = int(payloads.get("group_id") or payloads.get("user_id") or 0)
        is_group = "group_id" in payloads

        message = payloads.get("message", [])
        text_content = ""
        has_image = False

        if isinstance(message, str):
            text_content = message
        elif isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict):
                    if seg.get("type") == "text":
                        text_content += str(seg.get("data", {}).get("text", ""))
                    elif seg.get("type") == "image":
                        has_image = True
        return target_id, is_group, text_content.strip(), has_image

    @staticmethod
    def _normalize_text(text: str) -> str:
        return " ".join(str(text).split()).strip()

    @classmethod
    def _history_message_matches(
        cls,
        expected_text: str,
        hist_text: str,
        expected_has_img: bool,
        hist_has_img: bool,
    ) -> bool:
        if expected_has_img != hist_has_img:
            return False

        expected_norm = cls._normalize_text(expected_text)
        hist_norm = cls._normalize_text(hist_text)

        if expected_norm and hist_norm:
            if expected_norm == hist_norm:
                return True
            # 只对较长文本放宽为包含匹配，避免“你好”这类短文本误命中历史消息。
            if min(len(expected_norm), len(hist_norm)) >= 12:
                return expected_norm in hist_norm or hist_norm in expected_norm
            return False

        return not expected_norm and not hist_norm

    async def _find_message_in_history(
        self,
        method: Callable,
        self_id: int,
        is_group: bool,
        target_id: int,
        expected_text: str,
        has_img: bool,
    ) -> Any:
        if self_id == 0:
            return None

        try:
            if is_group:
                res = await method("get_group_msg_history", group_id=target_id)
            else:
                res = await method("get_friend_msg_history", user_id=target_id)
            history = res.get("messages", []) if res else []
        except Exception:
            return None

        curr_time = time.time()
        for msg in reversed(history):
            try:
                msg_id = str(msg.get("message_id"))
                if msg_id in self._scheduled_ids:
                    continue

                uid = int(msg.get("sender", {}).get("user_id", 0))
                if uid != self_id:
                    continue

                if curr_time - int(msg.get("time", 0)) > HISTORY_MATCH_WINDOW:
                    continue

                hist_text, hist_img = "", False
                raw = msg.get("message", [])
                if isinstance(raw, str):
                    hist_text = raw
                elif isinstance(raw, list):
                    for s in raw:
                        if s.get("type") == "text":
                            hist_text += str(s.get("data", {}).get("text", ""))
                        elif s.get("type") == "image":
                            hist_img = True

                if self._history_message_matches(
                    expected_text, hist_text, has_img, hist_img
                ):
                    return msg.get("message_id")
            except Exception:
                continue
        return None

    @staticmethod
    def _extract_message_id(result: Any) -> str | int | None:
        if not result:
            return None
        if isinstance(result, dict):
            return result.get("message_id") or result.get("data", {}).get("message_id")
        return getattr(result, "message_id", None)

    async def _schedule_retraction(self, method: Callable, message_id: Any, delay: int):
        msg_id_str = str(message_id)
        if msg_id_str in self._scheduled_ids:
            return

        self._scheduled_ids.add(msg_id_str)
        try:
            await asyncio.sleep(delay)

            if self._shutdown:
                logger.info(f"SelfMessageRetriever: 插件关闭，取消撤回 [{message_id}]")
                return

            for _ in range(3):
                for action in RETRACT_ACTIONS:
                    try:
                        await method(action, message_id=message_id)
                        logger.info(f"SelfMessageRetriever: 撤回成功 [{message_id}]")
                        return
                    except Exception:
                        continue
                await asyncio.sleep(1)

            logger.warning(f"SelfMessageRetriever: 撤回失败 [{message_id}]")
        finally:
            self._scheduled_ids.discard(msg_id_str)

    @filter.event_message_type(EventMessageType.ALL, priority=90)
    async def backup_hook_mechanism(self, event: AstrMessageEvent):
        if hasattr(event, "bot") and event.bot:  # type: ignore
            await self._wrap_bot_action(event.bot)  # type: ignore

    async def terminate(self):
        logger.info("SelfMessageRetriever 卸载中...")

        # 设置关闭标志，阻止新任务执行
        self._shutdown = True

        # 取消所有活跃的撤回任务
        if self._active_tasks:
            logger.info(
                f"SelfMessageRetriever: 取消 {len(self._active_tasks)} 个撤回任务"
            )
            for task in self._active_tasks:
                task.cancel()
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
            self._active_tasks.clear()

        # 清空已调度的消息ID
        self._scheduled_ids.clear()

        # 恢复原始 API
        if hasattr(self.context, "bots"):
            for bot in self.context.bots.values():  # type: ignore
                if hasattr(bot, "api") and hasattr(bot.api, ORIGINAL_METHOD_ATTR):
                    try:
                        original = getattr(bot.api, ORIGINAL_METHOD_ATTR)
                        bot.api.call_action = self._unwrap_method(original)
                        delattr(bot.api, ORIGINAL_METHOD_ATTR)
                    except Exception:
                        pass

        logger.info("SelfMessageRetriever 清理完成。")
