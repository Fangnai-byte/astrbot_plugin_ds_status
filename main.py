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
import hashlib
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
MAX_MAIL_SEEN = 300


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


# ---------------------------------------------------------------- 邮件源

def _decode_mime(value: str) -> str:
    """解 MIME 编码的头（=?utf-8?B?...?=），失败就原样返回。"""
    if not value:
        return ""
    try:
        from email.header import decode_header

        out = []
        for text, enc in decode_header(value):
            if isinstance(text, bytes):
                out.append(text.decode(enc or "utf-8", "replace"))
            else:
                out.append(text)
        return "".join(out).strip()
    except Exception:
        return value.strip()


def _html_to_text(raw: str) -> str:
    """HTML 转纯文本。

    比 strip_html 多做三步：去掉 <style>/<script>/<head>（不然 CSS 会当正文）、
    去掉注释、清掉零宽字符（QQ 的邮件里夹着 U+200B 那些）。
    """
    text = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", raw or "")
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", text)
    text = strip_html(text)
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff\u2060]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _mail_body_text(msg) -> str:
    """取邮件正文：优先 text/plain，没有就把 text/html 去标签。

    DeepSeek 的通知邮件是 HTML + 纯文本双份，纯文本那份最干净。
    """
    plain, markup = "", ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, "replace")
        except Exception:
            text = payload.decode("utf-8", "replace")
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not markup:
            markup = text
    # DeepSeek 的通知邮件只有 HTML 一份，纯文本那半截没什么用
    if markup and len(strip_html(markup)) >= len(strip_html(plain or "")):
        return _html_to_text(markup)
    return strip_html(plain or markup)


def _first_url(text: str) -> str:
    m = re.search(r"https?://[^\s<>\"')]+", text or "")
    return m.group(0) if m else ""


def _link_from_msg(msg) -> str:
    """邮件里的「查看详情」是 <a href>，去标签后 URL 就没了，得从原始 HTML 取。"""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() != "text/html":
            continue
        try:
            raw = part.get_payload(decode=True)
        except Exception:
            continue
        if not raw:
            continue
        text = raw.decode(part.get_content_charset() or "utf-8", "replace")
        m = re.search(r'href="(https?://[^"]*(?:incident|maintenance)[^"]*)"', text, re.I)
        if m:
            return m.group(1)
        m = re.search(r'href="(https?://[^"]+)"', text, re.I)
        if m and "unsubscribe" not in m.group(1).lower():
            return m.group(1)
    return ""


def _mail_entries_on_conn(conn, from_filter: str, lookback_days: int, limit: int) -> list[dict[str, str]]:
    """在已登录的连接上拉一次。抽出来是为了让常驻连接和一次性调用共用同一段逻辑。"""
    import email
    from datetime import datetime as _dt, timedelta
    from email.utils import parsedate_to_datetime

    since = (_dt.now() - timedelta(days=max(1, lookback_days))).strftime("%d-%b-%Y")
    # 括号只能有一层：早先写成 (FROM "x" (SINCE d)) 这种嵌套，QQ 的 IMAP
    # 不报错也不回包，只能干等到超时。
    parts = []
    if from_filter:
        parts.append(f'FROM "{from_filter}"')
    parts.append(f"SINCE {since}")
    crit = "(" + " ".join(parts) + ")"

    entries: list[dict[str, str]] = []
    typ, data = conn.search(None, crit)
    if typ != "OK" or not data or not data[0]:
        return entries
    ids = data[0].split()
    # 只取最近 limit 封：老邮件没必要每轮解析
    for num in ids[-max(1, limit):]:
        typ, payload = conn.fetch(num, "(BODY.PEEK[])")
        if typ != "OK" or not payload or not isinstance(payload[0], tuple):
            continue
        msg = email.message_from_bytes(payload[0][1])
        subject = _decode_mime(msg.get("Subject", ""))
        body = _mail_body_text(msg)
        # 邮件正文第一行往往是「故障：<标题>」，跟 title 重复，去掉
        first_line, _, rest = body.partition("\n")
        if first_line.strip().startswith(("故障", "维护", "Incident", "Maintenance")) and (
            subject[:8] in first_line or first_line[:20] in subject
        ):
            body = rest.strip() or body
        raw_date = msg.get("Date", "")
        try:
            dt = parsedate_to_datetime(raw_date).astimezone(CN_TZ)
            updated = dt.strftime("%Y-%m-%d %H:%M")
            id_time = dt.strftime("%Y%m%d%H%M%S")
        except Exception:
            updated = raw_date
            id_time = raw_date
        link = _first_url(body) or _link_from_msg(msg)
        num = incident_number(link, body, msg.get("Message-ID", "") or "")
        st = status_of(body)
        base = f"incident:{num}" if num else (
            (msg.get("Message-ID", "") or "").strip() or f"mail|{subject}|{id_time}"
        )
        fp = st or hashlib.sha1(f"{subject}\n{body}".encode("utf-8")).hexdigest()[:8]
        body = dedupe_lines(body, subject)
        entries.append(
            {
                "id": f"{base}#{fp}",
                "title": subject or "（无主题）",
                "link": link,
                "updated": updated,
                "summary": body[:1200],
                "source": "邮件",
            }
        )
    # 同一次故障会连发好几封（监控中→监控中→…），同一 id 只留最新那封
    latest: dict[str, dict[str, str]] = {}
    for entry in entries:
        prev = latest.get(entry["id"])
        if prev is None or entry["updated"] >= prev["updated"]:
            latest[entry["id"]] = entry
    return list(latest.values())


class MailClient:
    """常驻的 IMAP 连接。

    QQ 邮箱在短时间多次登录后会静默不响应（连 search 都拿不到回包），
    所以这里只登录一次，之后的轮询复用；连接被服务端断掉时自动重连一次。
    """

    def __init__(self, host: str, port: int, user: str, auth_code: str,
                 folder: str, timeout: int = 20) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.auth_code = auth_code
        self.folder = folder or "INBOX"
        self.timeout = timeout
        self._conn = None
        self._selected = ""

    def _connect(self):
        import imaplib
        import ssl

        conn = imaplib.IMAP4_SSL(
            self.host, self.port,
            ssl_context=ssl.create_default_context(),
            timeout=self.timeout,
        )
        conn.login(self.user, self.auth_code)
        self._conn = conn
        self._selected = ""
        return conn

    def _ensure(self):
        if self._conn is None:
            return self._connect()
        try:
            # NOOP 探活：服务端已断开时这里会抛，触发重连
            self._conn.noop()
        except Exception:
            self.close()
            return self._connect()
        return self._conn

    def close(self) -> None:
        conn, self._conn, self._selected = self._conn, None, ""
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass

    def fetch(self, from_filter: str, lookback_days: int, limit: int) -> list[dict[str, str]]:
        for attempt in (1, 2):
            try:
                conn = self._ensure()
                if self._selected != self.folder:
                    conn.select(self.folder, readonly=True)
                    self._selected = self.folder
                return _mail_entries_on_conn(conn, from_filter, lookback_days, limit)
            except Exception:
                self.close()
                if attempt == 2:
                    raise
        return []


# ---------------------------------------------------------------- 事件去重

def incident_number(*texts: str) -> str:
    """从链接或 guid 里抠事件编号。RSS 的 guid 是 `urn:flashduty:change:7202016104287`，
    邮件里没有 guid，只能从「查看详情」的链接 /incidents/7202016104287 取。"""
    for text in texts:
        if not text:
            continue
        for pat in (r"/incidents?/(\d+)", r"change:(\d+)", r"incident[_\- ]?(\d{6,})"):
            m = re.search(pat, text, re.I)
            if m:
                return m.group(1)
    return ""


def status_of(text: str) -> str:
    """把中英文的状态描述归一成几个值。"""
    low = (text or "").lower()
    if "resolved" in low or "已恢复" in low or "已解决" in low:
        return "resolved"
    if "monitoring" in low or "监控" in low:
        return "monitoring"
    if "identified" in low or "已定位" in low:
        return "identified"
    if "investigating" in low or "调查中" in low:
        return "investigating"
    if "maintenance" in low or "维护" in low:
        return "maintenance"
    return ""


def dedupe_lines(text: str, digest: str) -> str:
    """去掉正文开头跟标题重复的那几行（邮件里固定会重复一遍）。"""
    lines = [x.strip() for x in (text or "").splitlines()]
    keep = []
    for line in lines:
        if not line:
            continue
        if not keep and (line == digest or (digest and (line in digest or digest in line))):
            continue
        if not keep and line in ("故障更新", "故障", "维护", "Incident Update", "Maintenance"):
            continue
        keep.append(line)
    return "\n".join(keep)

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
        # 去重键带内容指纹：同一 guid 的条目会被原站原地改写
        # （事件从 investigating → resolved，标题不变、正文变了），
        # 只按 guid 去重的话，后续状态变化全被当成「已推送」吞掉。
        num = incident_number(guid, link)
        st = status_of(summary) or status_of(title)
        base = f"incident:{num}" if num else (guid or link or f"{title}|{published}")
        fp = st or hashlib.sha1(f"{title}\n{summary}".encode("utf-8")).hexdigest()[:8]
        entries.append(
            {
                "id": f"{base}#{fp}",
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
    def mail_enabled(self) -> bool:
        return _as_bool(_cfg(self.config, "mail_enabled", False), False)

    @property
    def push_as_image(self) -> bool:
        return _as_bool(_cfg(self.config, "push_as_image", False), False)

    @property
    def mail_interval(self) -> int:
        return max(60, _as_int(_cfg(self.config, "mail_poll_interval_sec", 300), 300))

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

    def _mail_client(self) -> "MailClient":
        """按当前配置取（或新建）常驻 IMAP 连接。配置改了会重建。"""
        cfg = (
            str(_cfg(self.config, "mail_imap_host", "imap.qq.com") or "imap.qq.com").strip(),
            _as_int(_cfg(self.config, "mail_imap_port", 993), 993),
            str(_cfg(self.config, "mail_user", "") or "").strip(),
            str(_cfg(self.config, "mail_auth_code", "") or "").strip(),
            str(_cfg(self.config, "mail_folder", "INBOX") or "INBOX").strip(),
            max(5, _as_int(_cfg(self.config, "mail_timeout_sec", 20), 20)),
        )
        client = getattr(self, "_mail_conn", None)
        if client is None or client.key != cfg:
            if client is not None:
                client.close()
            client = MailClient(*cfg)
            client.key = cfg
            self._mail_conn = client
        return client

    async def fetch_mail(self) -> list[dict[str, str]]:
        """拉一次邮箱；error 由调用方统一处理。"""
        client = self._mail_client()
        return await asyncio.to_thread(
            client.fetch,
            str(_cfg(self.config, "mail_from_filter", "deepseek") or "").strip(),
            _as_int(_cfg(self.config, "mail_lookback_days", 3), 3),
            _as_int(_cfg(self.config, "mail_max_per_check", 5), 5),
        )

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

    def card_markdown(self, entries: list[dict[str, str]]) -> str:
        """把条目拼成一张卡片的 Markdown。"""
        max_chars = max(50, _as_int(_cfg(self.config, "max_content_chars", 300), 300))
        blocks = []
        for entry in entries:
            summary = (entry.get("summary") or "").strip()
            if len(summary) > max_chars:
                summary = summary[:max_chars].rstrip() + "……"
            head = f"## {entry.get('title', '（无标题）')}"
            meta = " · ".join(x for x in (entry.get("source", ""), entry.get("updated", "")) if x)
            body = [head, meta]
            if summary:
                body.append(summary)
            if _as_bool(_cfg(self.config, "include_link", True), True) and entry.get("link"):
                body.append(entry["link"])
            blocks.append("\n\n".join(body))
        return "\n\n---\n\n".join(blocks)

    async def _send_card(self, umo: str, entries: list[dict[str, str]]) -> bool:
        """把条目画成图片发出去。渲染失败不算错，由调用方回落到文本。"""
        path = await asyncio.to_thread(self._render_card_file, entries)
        if not path:
            return False
        return await self.context.send_message(
            umo, MessageChain([Comp.Image.fromFileSystem(path)])
        )

    def _render_card_file(self, entries: list[dict[str, str]]) -> str:
        """用插件自带的 Pillow 渲染器出图。

        不用 astrbot 的 text_to_image：那张卡的页眉是它的 logo，
        而且本机没有浏览器时只能走它的本地模板，样式改不了。
        """
        from importlib import util as _import_util

        def _load():
            try:
                from .card import render_card  # 正常安装（插件作为包加载）

                return render_card
            except Exception:
                path = Path(__file__).resolve().parent / "card.py"
                spec = _import_util.spec_from_file_location("ds_status_card", path)
                mod = _import_util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod.render_card

        render = _load()
        stamp = datetime.now(CN_TZ).strftime("%Y%m%d_%H%M%S_%f")
        out = self.data_dir / "cards" / f"ds_status_{stamp}.png"
        return render(entries, out)

    async def _send_entries(self, umo: str, entries: list[dict[str, str]]) -> bool:
        if self.push_as_image:
            try:
                if await self._send_card(umo, entries):
                    return True
                logger.warning(f"[{PLUGIN_NAME}] 卡片渲染没出图，回落到文本：{umo}")
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 卡片渲染失败（{exc}），回落到文本：{umo}")
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
            mail_error = ""
            if self.mail_enabled:
                try:
                    mail_entries = await self.fetch_mail()
                    if mail_entries:
                        # 邮件排在前面：它比 RSS 早，也更全；同 id 的以邮件为准
                        ids = {e["id"] for e in mail_entries}
                        entries = mail_entries + [e for e in entries if e["id"] not in ids]
                except Exception as exc:  # 邮箱挂了不该拖累 RSS
                    mail_error = f"邮箱拉取失败：{exc}"
                    logger.warning(f"[{PLUGIN_NAME}] {mail_error}")
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
            self.store.last_error = mail_error
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
            f"推送方式：{'图片卡片' if self.push_as_image else ('合并转发' if self.use_forward() else '普通文本')}",
            f"邮件源：{'开启' if self.mail_enabled else '关闭'}",
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
