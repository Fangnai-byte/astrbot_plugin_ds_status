"""AstrBot 插件 — DS 状态订阅

订阅 DeepSeek 状态页的 RSS（默认 https://status.deepseek.com/feed.rss），
按设定间隔拉取，出现新条目时推送到已订阅的会话。

命令：
    /ds订阅         订阅当前会话的状态更新推送
    /ds退订         取消当前会话的订阅
    /ds订阅列表     查看已订阅的会话
    /ds状态         立即拉取并查看最新条目（只读，不推送）
    /ds检查         立即轮询一次，有新条目就推送
    /ds测试         往当前会话推送一条测试消息
    /ds帮助         查看命令说明
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.star_tools import StarTools

CN_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_RSS_URL = "https://status.deepseek.com/feed.rss"
PLUGIN_NAME = "astrbot_plugin_ds_status"
MAX_SEEN = 300


# ---------------------------------------------------------------- 工具函数


def _cfg(config: Any, key: str, default: Any = None) -> Any:
    """安全读取插件配置，缺失或空值时回落到默认值。"""
    try:
        value = config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,\n;]", value)
        return [p.strip() for p in parts if p.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "是", "开"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否", "关"}:
        return False
    return default


def _http_hint(url: str, status: int) -> str:
    """把 HTTP 错误码翻译成一句人话提示。"""
    if status == 403:
        return "源站返回 403，可能被区域/IP 限制，请设置 proxy 或更换 rss_url"
    if status == 404:
        return f"源站返回 404，rss_url 可能已失效：{url}"
    if status >= 500:
        return f"源站返回 {status}，稍后会自动重试"
    return f"源站返回 HTTP {status}"


def strip_html(text: str) -> str:
    """去掉摘要里的标签与多余空白。"""
    if not text:
        return ""
    cleaned = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"[ \t\u00a0]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _find_text(node: ElementTree.Element, names: tuple[str, ...]) -> str:
    for child in node:
        if _local_name(child.tag) in names:
            text = (child.text or "").strip()
            if text:
                return text
    return ""


def _find_link(node: ElementTree.Element) -> str:
    """兼容 RSS 的 <link>文本</link> 与 Atom 的 <link href="..."/>。"""
    for child in node:
        if _local_name(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        if href:
            rel = (child.get("rel") or "alternate").lower()
            if rel in {"alternate", ""}:
                return href
        text = (child.text or "").strip()
        if text:
            return text
    return ""


def _format_time(raw: str) -> str:
    """把 RSS/Atom 时间转成东八区可读文本，失败就原样返回。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    parsed: datetime | None = None
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(raw)
    except Exception:
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            parsed = None
    if parsed is None:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M")


def parse_feed(xml_text: str) -> list[dict[str, str]]:
    """解析 RSS 2.0 / Atom，返回条目列表。"""
    root = ElementTree.fromstring(xml_text.strip())
    entries: list[dict[str, str]] = []

    nodes: list[ElementTree.Element] = []
    for child in root.iter():
        if _local_name(child.tag) in {"item", "entry"}:
            nodes.append(child)

    for node in nodes:
        title = strip_html(_find_text(node, ("title",)))
        link = _find_link(node)
        guid = _find_text(node, ("guid", "id"))
        published = _find_text(node, ("pubdate", "published", "updated", "date"))
        # RSS 2.0 的 description，Atom 的 summary / content
        summary = _find_text(node, ("description", "summary", "content"))
        summary = strip_html(summary)
        # 有些源把正文塞进 content:encoded，这里再兜一次
        if not summary:
            for child in node:
                if _local_name(child.tag) == "encoded":
                    summary = strip_html(child.text or "")
                    break
        entries.append(
            {
                "id": guid or link or f"{title}|{published}",
                "title": title or "（无标题）",
                "link": link,
                "updated": _format_time(published),
                "summary": summary,
            }
        )
    return entries


class StateStore:
    """订阅关系与已读条目记录。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.subscribers: list[str] = []
        self.seen: list[str] = []
        self.last_check: str = ""
        self.last_error: str = ""
        self.last_title: str = ""
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取状态失败：{exc}")
            return
        self.subscribers = [str(x) for x in data.get("subscribers", [])]
        self.seen = [str(x) for x in data.get("seen", [])]
        self.last_check = str(data.get("last_check", ""))
        self.last_error = str(data.get("last_error", ""))
        self.last_title = str(data.get("last_title", ""))

    def save(self) -> None:
        self.seen = self.seen[-MAX_SEEN:]
        payload = {
            "subscribers": self.subscribers,
            "seen": self.seen,
            "last_check": self.last_check,
            "last_error": self.last_error,
            "last_title": self.last_title,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 保存状态失败：{exc}")

    def mark_seen(self, entry_id: str) -> None:
        if entry_id and entry_id not in self.seen:
            self.seen.append(entry_id)

    def is_new(self, entry_id: str) -> bool:
        return bool(entry_id) and entry_id not in self.seen


# ---------------------------------------------------------------- 插件主体


class DsStatusPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        try:
            self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:
            self.data_dir = Path(__file__).resolve().parent / "data"
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(self.data_dir / "state.json")
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 配置读取

    @property
    def rss_url(self) -> str:
        return str(_cfg(self.config, "rss_url", DEFAULT_RSS_URL) or DEFAULT_RSS_URL).strip()

    @property
    def interval(self) -> int:
        return max(60, _as_int(_cfg(self.config, "poll_interval_sec", 600), 600))

    def _allowed(self, event: AstrMessageEvent) -> bool:
        ids = [str(x) for x in _as_list(_cfg(self.config, "allow_user_ids", []))]
        if not ids:
            return True
        return str(event.get_sender_id()) in ids

    def _deny_text(self, event: AstrMessageEvent, manage: bool = False) -> str:
        if not self._allowed(event):
            return "这个命令只对白名单用户开放哦。"
        if manage and _as_bool(_cfg(self.config, "admin_only", True), True):
            if not event.is_admin():
                return "订阅管理命令只给管理员用，抱歉啦。"
        return ""

    # ------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        logger.info(f"[{PLUGIN_NAME}] 已加载，订阅源：{self.rss_url}")
        if not _as_bool(_cfg(self.config, "enabled", True), True):
            logger.info(f"[{PLUGIN_NAME}] 自动轮询已关闭（配置 enabled=false）")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._poll_loop())

    async def terminate(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.store.save()
        logger.info(f"[{PLUGIN_NAME}] 已卸载")

    # ------------------------------------------------------------ 拉取与推送

    async def fetch_feed(self) -> list[dict[str, str]]:
        url = self.rss_url
        timeout = max(5, _as_int(_cfg(self.config, "timeout_sec", 20), 20))
        proxy = str(_cfg(self.config, "proxy", "") or "").strip() or None
        ua = str(
            _cfg(self.config, "user_agent", "Mozilla/5.0 (AstrBot DsStatus)")
            or "Mozilla/5.0 (AstrBot DsStatus)"
        )
        headers = {
            "User-Agent": ua,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "Cache-Control": "no-cache",
        }
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        try:
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.get(url, headers=headers, proxy=proxy) as resp:
                    if resp.status != 200:
                        raise RuntimeError(_http_hint(url, resp.status))
                    raw = await resp.read()
        except RuntimeError:
            raise
        except asyncio.TimeoutError:
            raise RuntimeError(f"拉取超时（{timeout}s），可在配置里调大 timeout_sec 或设置 proxy")
        except aiohttp.ClientConnectorSSLError:
            raise RuntimeError("TLS 握手失败，源站可能限制本机网络，请设置 proxy 或更换 rss_url")
        except aiohttp.ClientConnectorError:
            raise RuntimeError("无法连接源站，请检查网络或设置 proxy")
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"网络请求失败：{exc.__class__.__name__}")
        text = raw.decode("utf-8", errors="replace")
        entries = parse_feed(text)
        if not entries:
            raise RuntimeError("解析到 0 条条目，内容可能不是 RSS/Atom（例如被拦截页替换）")
        return entries

    def _match(self, entry: dict[str, str]) -> bool:
        haystack = f"{entry.get('title', '')}\n{entry.get('summary', '')}"
        ignore = _as_list(_cfg(self.config, "ignore_keywords", []))
        if any(word and word in haystack for word in ignore):
            return False
        keywords = _as_list(_cfg(self.config, "filter_keywords", []))
        if keywords and not any(word and word in haystack for word in keywords):
            return False
        return True

    def format_entry(self, entry: dict[str, str]) -> str:
        max_chars = max(50, _as_int(_cfg(self.config, "max_content_chars", 300), 300))
        lines = [f"【状态更新】{entry.get('title', '（无标题）')}"]
        if entry.get("updated"):
            lines.append(f"时间：{entry['updated']}")
        summary = (entry.get("summary") or "").strip()
        if summary:
            if len(summary) > max_chars:
                summary = summary[:max_chars].rstrip() + "……"
            lines.append(summary)
        if _as_bool(_cfg(self.config, "include_link", True), True) and entry.get("link"):
            lines.append(entry["link"])
        return "\n".join(lines)

    def format_entries(self, entries: list[dict[str, str]]) -> str:
        return "\n\n".join(self.format_entry(e) for e in entries)

    def use_forward(self) -> bool:
        return _as_bool(_cfg(self.config, "use_forward", True), True)

    def forward_name(self) -> str:
        return str(_cfg(self.config, "forward_name", "DS 状态订阅") or "DS 状态订阅").strip()

    def forward_nodes(self, entries: list[dict[str, str]], info: str = "") -> Any:
        """把多条条目打包成一个合并转发节点集合。"""
        name = self.forward_name()
        nodes = []
        if info:
            nodes.append(Comp.Node(content=[Comp.Plain(text=info)], name=name, uin="0"))
        for entry in entries:
            nodes.append(
                Comp.Node(
                    content=[Comp.Plain(text=self.format_entry(entry))],
                    name=name,
                    uin="0",
                )
            )
        return Comp.Nodes(nodes=nodes)

    async def _send_entries(self, umo: str, entries: list[dict[str, str]]) -> bool:
        if self.use_forward():
            try:
                sent = await self.context.send_message(
                    umo, MessageChain([self.forward_nodes(entries)])
                )
                if sent:
                    return True
                logger.warning(f"[{PLUGIN_NAME}] 合并转发未送达，回落到普通文本：{umo}")
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 合并转发失败（{exc}），回落到普通文本：{umo}")
        return await self.context.send_message(
            umo, MessageChain([Comp.Plain(text=self.format_entries(entries))])
        )

    async def push_to_subscribers(self, entries: list[dict[str, str]]) -> tuple[int, int]:
        ok = 0
        fail = 0
        if not entries:
            return 0, 0
        for umo in list(self.store.subscribers):
            try:
                sent = await self._send_entries(umo, entries)
                if sent:
                    ok += 1
                else:
                    fail += 1
                    logger.warning(f"[{PLUGIN_NAME}] 推送失败（找不到平台会话）：{umo}")
            except Exception as exc:
                fail += 1
                logger.warning(f"[{PLUGIN_NAME}] 推送异常 {umo}：{exc}")
        return ok, fail

    async def poll_once(self, push: bool = True) -> dict[str, Any]:
        """拉取一次。push=False 时只记录不推送（用于首次静默同步）。"""
        async with self._lock:
            entries = await self.fetch_feed()
            fresh = [e for e in entries if self.store.is_new(e["id"])]
            limit = max(1, _as_int(_cfg(self.config, "push_max_entries", 3), 3))
            first_run = not self.store.seen
            initial_sync = _as_bool(_cfg(self.config, "initial_sync", False), False)

            if first_run and not initial_sync:
                for entry in entries:
                    self.store.mark_seen(entry["id"])
                self.store.last_check = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")
                self.store.last_error = ""
                self.store.last_title = entries[0]["title"]
                self.store.save()
                return {"new": 0, "pushed": 0, "failed": 0, "skipped": True, "total": len(entries)}

            to_push = [e for e in fresh if self._match(e)] if push else []
            pushed = failed = 0
            send_list = to_push[:limit]
            if send_list:
                ok, fail = await self.push_to_subscribers(send_list)
                pushed += ok
                failed += fail
                for entry in send_list:
                    logger.info(f"[{PLUGIN_NAME}] 新条目已推送：{entry['title']}")

            for entry in fresh:
                self.store.mark_seen(entry["id"])
            self.store.last_check = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")
            self.store.last_error = ""
            self.store.last_title = entries[0]["title"]
            self.store.save()
            return {
                "new": len(fresh),
                "pushed": pushed,
                "failed": failed,
                "skipped": False,
                "total": len(entries),
            }

    async def _poll_loop(self) -> None:
        # 启动时先静默对齐一次，避免重启后把旧条目当新消息刷屏
        try:
            await self.poll_once(push=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 首次拉取失败：{exc}")
            self.store.last_error = str(exc)
            self.store.save()

        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                return
            except asyncio.TimeoutError:
                pass
            if not self.store.subscribers:
                continue
            try:
                result = await self.poll_once(push=True)
                if result.get("new"):
                    logger.info(
                        f"[{PLUGIN_NAME}] 本轮新增 {result['new']} 条，"
                        f"推送 {result['pushed']} 次，失败 {result['failed']} 次"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.last_error = str(exc)
                self.store.save()
                logger.warning(f"[{PLUGIN_NAME}] 轮询失败：{exc}")

    # ------------------------------------------------------------ 命令

    @filter.command("ds订阅")
    async def cmd_subscribe(self, event: AstrMessageEvent):
        """订阅当前会话的状态更新推送"""
        deny = self._deny_text(event, manage=True)
        if deny:
            yield event.plain_result(deny)
            return
        umo = event.unified_msg_origin
        if umo in self.store.subscribers:
            yield event.plain_result("这个会话已经在订阅列表里了。")
            return
        self.store.subscribers.append(umo)
        self.store.save()
        yield event.plain_result(
            f"订阅成功，之后有新动态会推送到这里。当前订阅会话数：{len(self.store.subscribers)}。"
        )

    @filter.command("ds退订")
    async def cmd_unsubscribe(self, event: AstrMessageEvent):
        """取消当前会话的订阅"""
        deny = self._deny_text(event, manage=True)
        if deny:
            yield event.plain_result(deny)
            return
        umo = event.unified_msg_origin
        if umo not in self.store.subscribers:
            yield event.plain_result("这个会话本来就没订阅哦。")
            return
        self.store.subscribers.remove(umo)
        self.store.save()
        yield event.plain_result("已退订，不再推送状态更新。")

    @filter.command("ds订阅列表")
    async def cmd_list(self, event: AstrMessageEvent):
        """查看已订阅的会话"""
        deny = self._deny_text(event, manage=True)
        if deny:
            yield event.plain_result(deny)
            return
        if not self.store.subscribers:
            yield event.plain_result("目前还没有任何会话订阅。")
            return
        lines = [f"共 {len(self.store.subscribers)} 个会话订阅："]
        lines.extend(f"{i}. {umo}" for i, umo in enumerate(self.store.subscribers, 1))
        yield event.plain_result("\n".join(lines))

    @filter.command("ds状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看最新状态条目"""
        deny = self._deny_text(event)
        if deny:
            yield event.plain_result(deny)
            return
        try:
            entries = await self.fetch_feed()
        except Exception as exc:
            yield event.plain_result(f"拉取失败：{exc}")
            return
        head = entries[:3]
        max_chars = max(50, _as_int(_cfg(self.config, "max_content_chars", 300), 300))
        blocks = []
        for entry in head:
            summary = (entry.get("summary") or "").strip()
            if len(summary) > max_chars:
                summary = summary[:max_chars].rstrip() + "……"
            block = f"{entry['title']}（{entry.get('updated') or '时间未知'}）"
            if summary:
                block += f"\n{summary}"
            if entry.get("link"):
                block += f"\n{entry['link']}"
            blocks.append(block)
        info = f"来源：{self.rss_url}\n上次检查：{self.store.last_check or '尚未检查'}"
        if self.use_forward():
            yield event.chain_result([self.forward_nodes(head, info=info)])
            return
        yield event.plain_result(f"{info}\n\n" + "\n\n".join(blocks))

    @filter.command("ds检查")
    async def cmd_check(self, event: AstrMessageEvent):
        """立即轮询一次并推送新条目"""
        deny = self._deny_text(event, manage=True)
        if deny:
            yield event.plain_result(deny)
            return
        try:
            result = await self.poll_once(push=True)
        except Exception as exc:
            yield event.plain_result(f"检查失败：{exc}")
            return
        if result.get("skipped"):
            yield event.plain_result(
                f"首次运行，已静默记录 {result['total']} 条现有条目，之后只推新动态。"
            )
            return
        if not result["new"]:
            yield event.plain_result("检查完成，没有新条目。")
            return
        yield event.plain_result(
            f"检查完成：新增 {result['new']} 条，推送成功 {result['pushed']} 次，失败 {result['failed']} 次。"
        )

    @filter.command("ds测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """往当前会话发一条测试推送"""
        deny = self._deny_text(event, manage=True)
        if deny:
            yield event.plain_result(deny)
            return
        entry = {
            "id": "test",
            "title": "这是一条测试推送",
            "updated": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"),
            "summary": "看到这条说明推送链路是通的。",
            "link": "",
        }
        try:
            sent = await self._send_entries(event.unified_msg_origin, [entry])
        except Exception as exc:
            yield event.plain_result(f"推送异常：{exc}")
            return
        if sent:
            yield event.plain_result("测试消息已推送，去上面看看收到没。")
        else:
            yield event.plain_result("推送失败，当前平台可能不支持主动发消息。")

    @filter.command("ds帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """查看 DS 状态订阅插件的命令说明"""
        lines = [
            "DS 状态订阅插件",
            f"订阅源：{self.rss_url}",
            f"轮询间隔：{self.interval} 秒",
            f"订阅会话数：{len(self.store.subscribers)}",
            f"上次检查：{self.store.last_check or '尚未检查'}",
            f"推送方式：{'合并转发' if self.use_forward() else '普通文本'}",
            "",
            "/ds订阅 订阅当前会话",
            "/ds退订 取消订阅",
            "/ds订阅列表 查看订阅的会话",
            "/ds状态 查看最新条目",
            "/ds检查 立即检查并推送新条目",
            "/ds测试 发送一条测试推送",
            "/ds帮助 显示这份说明",
        ]
        if self.store.last_error:
            lines.append(f"\n最近一次错误：{self.store.last_error}")
        yield event.plain_result("\n".join(lines))
