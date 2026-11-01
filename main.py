"""QQ 图片消息修复插件 v0.5

功能：
1. 补全 SnowLuma 上报图片缺失的 rkey（否则 AstrBot 下载失败）；
2. 图片与 @ 分两条消息发时，自动把最近群图片补进本次请求；
3. 引用（回复）某张图片时，也能把被引用的图片补进来；
4. 只 @ 不带文字时，附带最近群聊文字内容作为上下文。

所有参数可在 WebUI 插件配置里调节。不额外调用大模型。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.star import Context, Star, register

SNOWLUMA_API = os.environ.get("SNOWLUMA_HTTP_API", "http://127.0.0.1:3000")
DEFAULT_TOKEN = ""
CACHE_TTL = 600.0
BAD_HOSTS = ("multimedia.nt.qq.com.cn", "gchat.qpic.cn", "multimedia.qpic.cn")
TMP_DIR = os.path.join(tempfile.gettempdir(), "qq_image_fix")
os.makedirs(TMP_DIR, exist_ok=True)

DEFAULTS = {
    "enabled": True,
    "snowluma_api": "http://127.0.0.1:3000",
    "snowluma_token": "",
    "fix_rkey": True,
    "inject_recent_images": True,
    "reply_image": True,
    "lookback_seconds": 180,
    "max_recent_images": 2,
    "empty_at_hint": True,
    "empty_at_context_count": 5,
    "inject_self_messages": True,
    "self_msg_window": 900,
    "self_msg_max": 3,
    "inject_self_messages_private": True,
    "self_msg_window_private": 900,
    "self_msg_max_private": 3,
    "quote_probability_group": 0.25,
    "quote_probability_private": 0.25,
    "quote_private_min_length": 15,
    "quote_private_quote_media": True,
    "scope_groups": ["194185111", "973978829"],
    # ---- 自动补话（合并自原 auto_followup 插件）----
    "followup_enable": False,
    "followup_private_only": True,
    "followup_delay_seconds": 1.6,
    "followup_cooldown_seconds": 120,
    "followup_min_questions": 2,
    "followup_min_lines": 2,
    "followup_min_chars": 12,
    "followup_max_reply_chars": 60,
    "followup_max_send_chars": 40,
    "followup_use_persona": True,
    "followup_extra_prompt": "",
    "followup_style": "auto",
    "followup_hourly_limit": 3,
    "followup_daily_limit": 10,
    "followup_quiet_hours": "1-8",
    # 多评论逐条回复
    "multi_reply_enable": True,
    "multi_reply_keywords": "分别回复,每条评论,每个评论,一条条,逐条,分别回",
    "multi_reply_max": 12,
    "multi_reply_delay": 3.0,
}

_cache: dict[str, tuple[str | None, float]] = {}
_last_history_fetch: dict[str, float] = {}


def _data_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _cfg_paths() -> list[str]:
    base = os.path.join(_data_dir(), "config")
    return [
        os.path.join(base, "astrbot_plugin_better_qq_config.json"),
        os.path.join(base, "astrbot_plugin_qq_image_fix_config.json"),
        os.path.join(base, "qq_image_fix_config.json"),
    ]


def _load_cfg() -> dict:
    user = {}
    for path in _cfg_paths():
        try:
            with open(path, encoding="utf-8-sig") as f:
                user = json.load(f)
            break
        except Exception:
            continue
    cfg = dict(DEFAULTS)

    def _apply(group) -> None:
        if not isinstance(group, dict):
            return
        for key, value in group.items():
            if isinstance(value, dict):
                _apply(value)
            elif key in cfg and value is not None:
                cfg[key] = value

    _apply(user or {})
    try:
        cfg["lookback_seconds"] = max(10, int(cfg["lookback_seconds"]))
        cfg["max_recent_images"] = max(0, int(cfg["max_recent_images"]))
        cfg["empty_at_context_count"] = max(0, min(20, int(cfg["empty_at_context_count"])))
        cfg["scope_groups"] = [str(x) for x in (cfg["scope_groups"] or [])]
    except Exception:
        pass
    return cfg


def _load_token() -> str:
    # 优先用配置里手填的 token，其次自动读取本机 SnowLuma 配置
    try:
        token = str(_load_cfg().get("snowluma_token") or "").strip()
        if token:
            return token
    except Exception:
        pass
    try:
        cfg_path = os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "QQBot", "SnowLuma", "config", "onebot.json"
        )
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        servers = (cfg.get("networks") or {}).get("httpServers") or []
        if servers and servers[0].get("accessToken"):
            return str(servers[0]["accessToken"])
    except Exception:
        pass
    return ""


def _base_url() -> str:
    try:
        return str(_load_cfg().get("snowluma_api") or SNOWLUMA_API).rstrip("/")
    except Exception:
        return SNOWLUMA_API


def _headers() -> dict:
    return {"Authorization": f"Bearer {_load_token()}"}


async def _resolve(filename: str) -> str | None:
    if not _load_token():
        return None
    now = time.time()
    hit = _cache.get(filename)
    if hit and now - hit[1] < CACHE_TTL:
        return hit[0]
    url = None
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{_base_url()}/get_image", json={"file": filename}, headers=_headers()
            ) as resp:
                payload = await resp.json(content_type=None)
        if payload.get("retcode") == 0:
            data = payload.get("data") or {}
            candidate = data.get("url") or data.get("file") or ""
            if "rkey=" in candidate:
                url = candidate
    except Exception as e:
        logger.warning(f"[better_qq] get_image 异常 {filename}: {type(e).__name__}: {e}")
    _cache[filename] = (url, now)
    return url


def _is_bad_url(v: str) -> bool:
    return v.startswith("http") and "rkey=" not in v and any(h in v for h in BAD_HOSTS)


async def _fix_image(img: "Image") -> None:
    if not _load_cfg().get("fix_rkey", True):
        return
    url = (getattr(img, "url", "") or "").strip()
    file = (getattr(img, "file", "") or "").strip()
    if _is_bad_url(url) and file and "://" not in file:
        fixed = await _resolve(file)
        if fixed:
            img.url = fixed
    elif not url and file and "://" not in file:
        fixed = await _resolve(file)
        if fixed:
            img.url = fixed


def _install_patch() -> None:
    if not hasattr(Image, "_qqf_orig_path"):
        Image._qqf_orig_path = Image.convert_to_file_path
        Image._qqf_orig_b64 = Image.convert_to_base64
    orig_to_path = Image._qqf_orig_path
    orig_to_b64 = Image._qqf_orig_b64

    async def patched_to_path(self):
        try:
            await _fix_image(self)
        except Exception as e:
            logger.warning(f"[better_qq] pre-process failed: {e}")
        return await orig_to_path(self)

    async def patched_to_b64(self):
        try:
            await _fix_image(self)
        except Exception as e:
            logger.warning(f"[better_qq] pre-process failed: {e}")
        return await orig_to_b64(self)

    Image.convert_to_file_path = patched_to_path
    Image.convert_to_base64 = patched_to_b64


async def _fetch_recent_messages(group_id: str, count: int = 20) -> list[dict]:
    if not _load_token():
        return []
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{_base_url()}/get_group_msg_history",
                json={"group_id": int(group_id), "count": count},
                headers=_headers(),
            ) as resp:
                payload = await resp.json(content_type=None)
        if payload.get("retcode") != 0:
            return []
        return (payload.get("data") or {}).get("messages") or []
    except Exception as e:
        logger.warning(f"[better_qq] 拉取群历史失败: {type(e).__name__}: {e}")
        return []


async def _fetch_friend_messages(user_id: str, count: int = 30) -> list[dict]:
    if not _load_token():
        return []
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{_base_url()}/get_friend_msg_history",
                json={"user_id": int(user_id), "count": count},
                headers=_headers(),
            ) as resp:
                payload = await resp.json(content_type=None)
        if payload.get("retcode") != 0:
            return []
        return (payload.get("data") or {}).get("messages") or []
    except Exception as e:
        logger.warning(f"[better_qq] 拉取好友历史失败: {type(e).__name__}: {e}")
        return []


async def _download(url: str) -> str | None:
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
        if not data:
            return None
        if data[:6] in (b"GIF87a", b"GIF89a"):
            ext = "gif"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = "png"
        else:
            ext = "jpg"
        path = os.path.join(TMP_DIR, hashlib.md5(data).hexdigest() + "." + ext)
        with open(path, "wb") as f:
            f.write(data)
        return path
    except Exception as e:
        logger.warning(f"[better_qq] 下载图片失败: {type(e).__name__}: {e}")
        return None


def _msg_images(msg: dict) -> list[str]:
    names = []
    for seg in msg.get("message") or []:
        if seg.get("type") != "image":
            continue
        filename = (seg.get("data") or {}).get("file") or ""
        if filename and "://" not in filename:
            names.append(str(filename))
    return names


async def _reply_image_paths(event: AstrMessageEvent, group_id: str) -> list[str]:
    """A3：把被引用（回复）消息里的图片取出来。"""
    reply_id = None
    for comp in event.get_messages() or []:
        if isinstance(comp, Reply):
            reply_id = getattr(comp, "id", None) or getattr(comp, "message_id", None)
            break
        if str(getattr(comp, "type", "")) == "reply":
            reply_id = getattr(comp, "id", None)
            break
    if not reply_id:
        return []
    messages = await _fetch_recent_messages(group_id, 30)
    for msg in reversed(messages):
        if str(msg.get("message_id")) != str(reply_id):
            continue
        paths = []
        for filename in _msg_images(msg)[:1]:
            url = await _resolve(filename)
            if not url:
                continue
            path = await _download(url)
            if path:
                paths.append(path)
        if paths:
            logger.info(f"[better_qq] 已补入被引用消息的图片 {len(paths)} 张")
        return paths
    return []


async def _recent_text_snippet(group_id: str, now: float, limit: int = 5) -> str:
    messages = await _fetch_recent_messages(group_id)
    lines: list[str] = []
    for msg in messages[-10:]:
        try:
            ts = float(msg.get("time") or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts and now - ts > 600:
            continue
        sender = (msg.get("sender") or {}).get("card") or (msg.get("sender") or {}).get("nickname") or ""
        texts = [
            str((seg.get("data") or {}).get("text") or "").strip()
            for seg in msg.get("message") or []
            if seg.get("type") == "text"
        ]
        text = " ".join(t for t in texts if t)
        if not text:
            continue
        if str(msg.get("sender", {}).get("user_id")) == "2192709418":
            continue
        lines.append(f"[{sender}] {text[:80]}")
    if limit <= 0:
        return ""
    return "\n".join(lines[-limit:])


async def _self_sent_messages(
    group_id: str, peer_id: str, now: float, window: float, maximum: int,
    self_id: str, known_texts: set[str]
) -> list[str]:
    """找出"机器人账号自己发过、但 AstrBot 不知道"的消息（通常是主人顶号发的），支持群聊和私聊。"""
    if not self_id:
        return []
    if group_id:
        messages = await _fetch_recent_messages(group_id, 30)
    elif peer_id:
        messages = await _fetch_friend_messages(peer_id, 30)
    else:
        return []
    if maximum <= 0:
        return []
    found: list[str] = []
    for msg in reversed(messages):
        try:
            ts = float(msg.get("time") or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts and now - ts > window:
            continue
        if str((msg.get("sender") or {}).get("user_id") or "") != self_id:
            continue
        parts = []
        for seg in msg.get("message") or []:
            if seg.get("type") == "text":
                text = str((seg.get("data") or {}).get("text") or "").strip()
                if text:
                    parts.append(text)
            elif seg.get("type") == "image":
                parts.append("[图片]")
        text = " ".join(parts).strip()
        if not text or text == "[图片]" or text.startswith("✅"):
            continue
        norm = text.replace(" ", "")
        if any(norm == k or (len(norm) > 4 and norm in k) for k in known_texts):
            continue
        found.append(text[:80])
        if len(found) >= maximum:
            break
    found.reverse()
    return found


async def _recent_image_paths(group_id: str, now: float, cfg: dict) -> list[str]:
    last = _last_history_fetch.get(group_id, 0.0)
    if now - last < 3:
        return []
    _last_history_fetch[group_id] = now
    messages = await _fetch_recent_messages(group_id)
    paths: list[str] = []
    window = float(cfg["lookback_seconds"])
    maximum = int(cfg["max_recent_images"])
    if maximum <= 0:
        return paths
    for msg in reversed(messages):
        try:
            ts = float(msg.get("time") or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts and now - ts > window:
            continue
        for filename in _msg_images(msg):
            url = await _resolve(filename)
            if not url:
                continue
            path = await _download(url)
            if path:
                paths.append(path)
            if len(paths) >= maximum:
                logger.info(f"[better_qq] 已把最近 {len(paths)} 张图补进本次请求")
                return paths
    if paths:
        logger.info(f"[better_qq] 已把最近 {len(paths)} 张图补进本次请求")
    return paths


@register("better_qq", "local", "更好的QQ体验", "1.0.0")
class QQImageFix(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._fu_pending: dict[str, tuple[str, str, float]] = {}
        self._fu_last: dict[str, float] = {}
        self._fu_tasks: set = set()
        self._fu_last_user: dict[str, float] = {}
        self._fu_ctx: dict[str, list[str]] = {}
        self._fu_hour: dict[tuple[str, str], int] = {}
        self._fu_day: dict[tuple[str, str], int] = {}
        self._mr_pending: dict[str, float] = {}
        self._mr_queue: dict[str, list[str]] = {}
        self._mr_tasks: set = set()
        try:
            _install_patch()
            logger.info("[better_qq] 补丁已安装")
        except Exception as e:
            logger.error(f"[better_qq] 安装补丁失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _fu_track_user(self, event: AstrMessageEvent):
        """记录用户最后发言时间，供补话前判断是否已有新消息。"""
        try:
            if _load_cfg().get("followup_enable", False):
                self._fu_last_user[event.unified_msg_origin] = time.time()
        except Exception:
            pass

    def _fu_in_quiet(self, cfg: dict) -> bool:
        try:
            raw = str(cfg.get("followup_quiet_hours", "") or "").strip()
            if not raw or "-" not in raw:
                return False
            a, b = raw.split("-", 1)
            start, end = int(a), int(b)
            h = time.localtime().tm_hour
            if start <= end:
                return start <= h < end
            return h >= start or h < end
        except Exception:
            return False

    def _fu_caps_ok(self, cfg: dict, umo: str) -> bool:
        try:
            now = time.localtime()
            hk = time.strftime("%Y-%m-%d %H", now)
            dk = time.strftime("%Y-%m-%d", now)
            hourly = int(cfg.get("followup_hourly_limit", 3) or 0)
            daily = int(cfg.get("followup_daily_limit", 10) or 0)
            if hourly > 0 and self._fu_hour.get((umo, hk), 0) >= hourly:
                return False
            if daily > 0 and self._fu_day.get((umo, dk), 0) >= daily:
                return False
            return True
        except Exception:
            return True

    def _fu_mark_sent(self, umo: str) -> None:
        now = time.localtime()
        hk = time.strftime("%Y-%m-%d %H", now)
        dk = time.strftime("%Y-%m-%d", now)
        self._fu_hour[(umo, hk)] = self._fu_hour.get((umo, hk), 0) + 1
        self._fu_day[(umo, dk)] = self._fu_day.get((umo, dk), 0) + 1
        if len(self._fu_hour) > 500:
            self._fu_hour.clear()
        if len(self._fu_day) > 500:
            self._fu_day.clear()

    @staticmethod
    def _fu_clean(text: str, max_chars: int) -> str:
        import re

        t = str(text or "").strip().strip('"').strip("“”").strip("「」")
        t = t.replace("<none>", "").strip()
        t = re.sub(r"[（(]\s*笑[\s）)]", "哈哈", t)
        t = re.sub(r"[（(]\s*笑哭[\s）)]", "哈哈", t)
        t = re.sub(r"[（(]\s*捂脸[\s）)]", "哈哈", t)
        for act in ("挠头", "小声", "认真", "凑过来", "比划", "低头", "笑了一下"):
            pat = re.compile(r"[（(]\s*" + act + r"\s*[）)]")
            matches = list(pat.finditer(t))
            while len(matches) > 1:
                m = matches[-1]
                t = t[: m.start()] + t[m.end():]
                matches = list(pat.finditer(t))
        t = t.replace("\n", " ").strip()
        return t[: max_chars + 10]

    @filter.on_decorating_result()
    async def on_decorating(self, event: AstrMessageEvent):
        """决定这条回复要不要引用：被 @ 必引用；每条新消息的首条回复必引用；其余按概率。"""
        try:
            import random
            import re

            cfg = _load_cfg()
            if not cfg.get("enabled", True):
                return
            is_private = not str(event.get_group_id() or "").strip()
            prob = float(
                cfg.get("quote_probability_private", 0.25)
                if is_private
                else cfg.get("quote_probability_group", 0.25)
            )

            # 多评论逐条回复：用户带截图并要求逐条回应
            if cfg.get("multi_reply_enable", True):
                try:
                    _txt = str(event.message_str or "").strip()
                    _kws = [k.strip() for k in str(cfg.get("multi_reply_keywords", "") or "").split(",") if k.strip()]
                    _has_img = any(isinstance(x, Image) for x in (event.get_messages() or []))
                    if _has_img and _kws and any(k in _txt for k in _kws):
                        self._mr_pending[event.unified_msg_origin] = time.time()
                        _hint = (
                            "\n[系统指令：对方要求你逐条回复他截图里的每一条评论。"
                            "请按截图从上到下的顺序逐条回应，每条之间用 ||| 分隔，每条不超过 60 字；"
                            "不要编号、不要解释、不要遗漏；只输出这些回复。]"
                        )
                        try:
                            req.prompt = (req.prompt or "") + _hint
                        except Exception:
                            pass
                        logger.info("[better_qq] 已启用逐条回复模式")
                except Exception:
                    pass
            comps = event.get_messages() or []
            has_media = any(isinstance(c, Image) for c in comps)
            plain_text = "".join(
                str(getattr(c, "text", "") or "") for c in comps if isinstance(c, Plain)
            )
            force = False
            self_id = ""
            try:
                self_id = str(event.get_self_id() or "")
            except Exception:
                pass
            # 群聊：被 @ 必引用
            if self_id:
                for comp in comps:
                    if isinstance(comp, At) and str(getattr(comp, "qq", "")) == self_id:
                        force = True
                        break

            if is_private:
                # 私聊：只对"长消息 / 带图消息"引用，短句不引用（群聊不受此规则影响）
                min_len = int(cfg.get("quote_private_min_length", 15))
                quote_media = bool(cfg.get("quote_private_quote_media", True))
                if not ((quote_media and has_media) or len(plain_text) >= min_len):
                    return
                force = True
            elif not force and (prob <= 0 or random.random() >= prob):
                return
            result = event.get_result()
            if result is None or not result.chain:
                return
            if any(isinstance(c, Reply) for c in result.chain):
                return
            if not all(isinstance(c, (Plain, Image)) for c in result.chain):
                return
            message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
            if not message_id:
                return
            result.chain.insert(0, Reply(id=message_id))
            logger.info(f"[better_qq] 本次回复引用（{"必引用" if force else f"概率{prob:.0%}"}）")
        except Exception as e:
            logger.warning(f"[better_qq] on_decorating error: {e}")

    @filter.on_llm_request()
    async def on_llm(self, event: AstrMessageEvent, req):
        try:
            cfg = _load_cfg()
            if not cfg.get("enabled", True):
                return
            # 自动补话用：记录最近几条对话上下文
            if cfg.get("followup_enable", False):
                try:
                    ctx = getattr(req, "contexts", None) or []
                    lines: list[str] = []
                    for m in ctx[-4:]:
                        role = str(m.get("role") or "")
                        content = m.get("content")
                        if isinstance(content, list):
                            content = "".join(
                                str(x.get("text") or "") for x in content if isinstance(x, dict)
                            )
                        content = str(content or "").strip().replace("\n", " ")
                        if content:
                            lines.append(("对方" if role == "user" else "你") + ": " + content[:80])
                    if lines:
                        self._fu_ctx[event.unified_msg_origin] = lines[-4:]
                except Exception:
                    pass
            comps = event.get_messages() or []
            has_text = any(str(getattr(c, "text", "") or "").strip() for c in comps)
            imgs = [c for c in comps if isinstance(c, Image)]
            group_id = str(event.get_group_id() or "").strip()
            now = time.time()
            if cfg["scope_groups"] and group_id and group_id not in cfg["scope_groups"]:
                return

            # 1) 当前消息没图：先看是否在引用某张图，再看最近发过的图
            if cfg.get("inject_recent_images", True) and not imgs and not req.image_urls and group_id:
                paths: list[str] = []
                if cfg.get("reply_image", True):
                    paths = await _reply_image_paths(event, group_id)
                if not paths:
                    paths = await _recent_image_paths(group_id, now, cfg)
                if paths:
                    req.image_urls = list(req.image_urls or []) + paths

            # 2) 只 @ 不带文字：补最近群聊文字上下文
            if cfg.get("empty_at_hint", True) and not has_text and not imgs and group_id:
                snippet = await _recent_text_snippet(
                    group_id, now, int(cfg["empty_at_context_count"])
                )
                hint = (
                    "\n[系统提示：对方只 @ 了你、没有打字。下面是群里刚才的聊天记录，"
                    "请自然地接着最近的话题回应，不要问“你想说什么”。]\n" + snippet
                )
                try:
                    from astrbot.core.agent.message import TextPart

                    req.extra_user_content_parts.append(TextPart(text=hint))
                except Exception:
                    req.prompt = (req.prompt or "") + hint
                logger.info("[better_qq] 空 @ 消息，已注入最近聊天上下文")

            # 3) 补入"机器人账号自己发过、但 AstrBot 不知道"的消息（主人顶号发的，群聊/私聊都支持）
            peer_id = ""
            if not group_id:
                try:
                    peer_id = str(event.get_sender_id() or "")
                except Exception:
                    peer_id = ""
            if group_id:
                self_enabled = bool(cfg.get("inject_self_messages", True))
                self_window = float(cfg.get("self_msg_window", 900))
                self_max = int(cfg.get("self_msg_max", 3))
            else:
                self_enabled = bool(cfg.get("inject_self_messages_private", True))
                self_window = float(cfg.get("self_msg_window_private", 900))
                self_max = int(cfg.get("self_msg_max_private", 3))
            if self_enabled and (group_id or peer_id):
                self_id = ""
                try:
                    self_id = str(event.get_self_id() or "")
                except Exception:
                    self_id = str(getattr(getattr(event, "message_obj", None), "self_id", "") or "")
                known: set[str] = set()
                for m in (req.contexts or [])[-15:]:
                    if not isinstance(m, dict) or str(m.get("role")) != "assistant":
                        continue
                    content = m.get("content")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        text = " ".join(
                            str(p.get("text") or "") for p in content if isinstance(p, dict)
                        )
                    norm = text.replace(" ", "").strip()[:200]
                    if norm:
                        known.add(norm)
                try:
                    self_texts = await _self_sent_messages(
                        group_id, peer_id, now, self_window, self_max, self_id, known
                    )
                except Exception:
                    self_texts = []
                if self_texts:
                    hint = (
                        "\n[系统提示：以下这些话是你这个账号刚才在群里发过的（可能是主人手动用手机发的，"
                        "不是系统自动生成的），请把它们当作你自己说过的话，自然地接着聊，不要重复说一遍：]\n"
                        + "\n".join(f"- {t}" for t in self_texts)
                    )
                    try:
                        from astrbot.core.agent.message import TextPart

                        req.extra_user_content_parts.append(TextPart(text=hint))
                    except Exception:
                        req.prompt = (req.prompt or "") + hint
                    logger.info(f"[better_qq] 已补入自己账号的 {len(self_texts)} 条消息")
        except Exception as e:
            logger.warning(f"[better_qq] on_llm error: {e}")
    # ------------------------------------------------------------------
    # 自动补话：对方一条消息里说了几件事/问了几件事的时候，正常回复之后
    # 再补一句，不用等对方再发消息。逻辑合并自独立插件 auto_followup。
    # ------------------------------------------------------------------
    @filter.on_llm_response()
    async def _fu_capture(self, event: AstrMessageEvent, response=None):
        try:
            if not _load_cfg().get("followup_enable", False):
                return
            bot = getattr(response, "completion_text", "") or ""
            if not bot:
                return
            user = ""
            try:
                user = event.message_str or ""
            except Exception:
                pass
            self._fu_pending[event.unified_msg_origin] = (user, bot, time.time())
        except Exception as e:
            logger.warning(f"[better_qq] followup capture: {e}")

    @filter.on_llm_response()
    async def _mr_capture(self, event: AstrMessageEvent, response=None):
        """逐条回复：拆分模型输出，只留第一条给主流程发，其余排队。"""
        try:
            umo = event.unified_msg_origin
            if umo not in self._mr_pending:
                return
            cfg = _load_cfg()
            text = getattr(response, "completion_text", "") or ""
            self._mr_pending.pop(umo, None)
            if "|||" not in text:
                return
            parts = [p.strip() for p in text.split("|||") if p.strip()]
            if len(parts) <= 1:
                return
            max_n = max(1, int(cfg.get("multi_reply_max", 12)))
            parts = parts[:max_n]
            try:
                response.completion_text = parts[0]
            except Exception:
                return
            self._mr_queue[umo] = parts[1:]
            self._fu_pending.pop(umo, None)
            logger.info(f"[better_qq] 逐条回复：共 {len(parts)} 条，先发第 1 条")
        except Exception as e:
            logger.warning(f"[better_qq] multi_reply capture: {e}")

    @filter.after_message_sent()
    async def _mr_after(self, event: AstrMessageEvent):
        try:
            umo = event.unified_msg_origin
            rest = self._mr_queue.pop(umo, None)
            if not rest:
                return
            cfg = _load_cfg()
            task = asyncio.create_task(self._mr_run(umo, rest, cfg))
            self._mr_tasks.add(task)
            task.add_done_callback(self._mr_tasks.discard)
        except Exception as e:
            logger.warning(f"[better_qq] multi_reply after: {e}")

    async def _mr_run(self, umo: str, parts: list[str], cfg: dict) -> None:
        """逐条发送剩余回复；中途用户发新消息就停。"""
        delay = max(0.5, float(cfg.get("multi_reply_delay", 3.0)))
        start = time.time()
        sent = 1
        try:
            for text in parts:
                await asyncio.sleep(delay)
                if self._fu_last_user.get(umo, 0) > start:
                    logger.info("[better_qq] 逐条回复：收到新消息，停止剩余发送")
                    return
                text = self._fu_clean(text, 80)
                if not text:
                    continue
                await self.context.send_message(umo, MessageChain([Plain(text=text)]))
                sent += 1
                logger.info(f"[better_qq] 逐条回复第 {sent} 条: {text[:40]}")
        except Exception as e:
            logger.warning(f"[better_qq] multi_reply run: {e}")

    @filter.after_message_sent()
    async def _fu_after(self, event: AstrMessageEvent):
        try:
            cfg = _load_cfg()
            if not cfg.get("followup_enable", False):
                return
            umo = event.unified_msg_origin
            item = self._fu_pending.pop(umo, None)
            if not item:
                return
            user, bot, _ts = item
            now = time.time()
            cooldown = float(cfg.get("followup_cooldown_seconds", 120))
            if now - self._fu_last.get(umo, 0) < cooldown:
                return
            is_private = not str(event.get_group_id() or "").strip()
            if cfg.get("followup_private_only", True) and not is_private:
                return
            group_id = str(event.get_group_id() or "").strip()
            if group_id and cfg.get("scope_groups") and group_id not in cfg["scope_groups"]:
                return
            if not self._fu_is_multi(cfg, user):
                return
            if "\n" in bot or len(bot) >= int(cfg.get("followup_max_reply_chars", 60)):
                return
            task = asyncio.create_task(self._fu_run(umo, user, bot, _ts))
            self._fu_tasks.add(task)
            task.add_done_callback(self._fu_tasks.discard)
        except Exception as e:
            logger.warning(f"[better_qq] followup after: {e}")

    def _fu_is_multi(self, cfg: dict, text: str) -> bool:
        try:
            s = text or ""
            questions = s.count("？") + s.count("?")
            lines = len([x for x in s.split("\n") if x.strip()])
            if questions >= int(cfg.get("followup_min_questions", 2)):
                return True
            if lines >= int(cfg.get("followup_min_lines", 2)) and len(s) >= int(cfg.get("followup_min_chars", 12)):
                return True
            return False
        except Exception:
            return False

    async def _fu_persona_prompt(self, umo: str) -> str:
        if not _load_cfg().get("followup_use_persona", True):
            return ""
        try:
            pm = getattr(self.context, "persona_manager", None)
            if pm is None:
                return ""
            persona = await pm.get_default_persona_v3(umo)
            return str(getattr(persona, "system_prompt", "") or "")
        except Exception:
            return ""

    async def _fu_run(self, umo: str, user: str, bot: str, capture_ts: float = 0.0) -> None:
        try:
            cfg = _load_cfg()
            await asyncio.sleep(float(cfg.get("followup_delay_seconds", 1.6)))
            # A：延迟期间用户又发了新消息，就不补了
            if capture_ts and self._fu_last_user.get(umo, 0) > capture_ts:
                logger.info("[better_qq] 补话取消：延迟期间已有新消息")
                return
            # F：夜间静音
            if self._fu_in_quiet(cfg):
                return
            # D：每小时/每天上限
            if not self._fu_caps_ok(cfg, umo):
                logger.info("[better_qq] 补话跳过：达到每小时/每天上限")
                return
            provider = self.context.get_using_provider(umo)
            if provider is None:
                return
            max_chars = int(cfg.get("followup_max_send_chars", 40))
            style = str(cfg.get("followup_style", "auto") or "auto").strip().lower()
            style_hint = {
                "ask": "如果对方有还没被你回应/没答的问题，追问一句；没有就只输出 <none>。",
                "care": "补一句关心对方的话（累不累、吃了没之类），自然一点别客套；没有合适的就只输出 <none>。",
                "share": "补一句你自己的近况或刚发生的小事，像顺口说的；没有就只输出 <none>。",
                "joke": "补一句轻松的笑话或吐槽，别冒犯；没有就只输出 <none>。",
            }.get(
                style,
                "如果对方还有没被你回应到的内容（比如第二个问题、另一件事），补一句很短的话；没有就只输出 <none>。",
            )
            ctx_lines = self._fu_ctx.get(umo) or []
            ctx_text = "\n".join(ctx_lines[-3:]) if ctx_lines else ""
            prompt = (
                "最近对话：\n" + (ctx_text or "（无）")
                + "\n\n对方刚发来的消息：\n" + (user or "")[:300]
                + "\n\n你已经回过：\n" + (bot or "")[:200]
                + "\n\n" + style_hint
                + f"\n要求：一句话、{max_chars} 字以内、像真人打字、不要重复刚才说过的、不用 emoji、不用（笑）。只输出这句话。"
            )
            system_prompt = await self._fu_persona_prompt(umo)
            extra = str(cfg.get("followup_extra_prompt", "") or "").strip()
            if extra:
                system_prompt = (system_prompt + "\n" + extra).strip()
            response = await provider.text_chat(prompt=prompt, system_prompt=system_prompt or None)
            text = self._fu_clean(getattr(response, "completion_text", "") or "", max_chars)
            if not text or len(text) > max_chars + 10:
                return
            if len(text) > max_chars:
                text = text[:max_chars]
            self._fu_last[umo] = time.time()
            self._fu_mark_sent(umo)
            logger.info(f"[better_qq] 自动补话: {text}")
            await self.context.send_message(umo, MessageChain([Plain(text=text)]))
        except Exception as e:
            logger.warning(f"[better_qq] followup run: {e}")
