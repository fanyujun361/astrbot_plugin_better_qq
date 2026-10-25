"""QQ 图片消息修复插件 v0.5

功能：
1. 补全 SnowLuma 上报图片缺失的 rkey（否则 AstrBot 下载失败）；
2. 图片与 @ 分两条消息发时，自动把最近群图片补进本次请求；
3. 引用（回复）某张图片时，也能把被引用的图片补进来；
4. 只 @ 不带文字时，附带最近群聊文字内容作为上下文。

所有参数可在 WebUI 插件配置里调节。不额外调用大模型。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
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
}

_cache: dict[str, tuple[str | None, float]] = {}
_last_history_fetch: dict[str, float] = {}


def _data_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _cfg_paths() -> list[str]:
    base = os.path.join(_data_dir(), "config")
    return [
        os.path.join(base, "astrbot_plugin_qq_image_fix_config.json"),
        os.path.join(base, "qq_image_fix_config.json"),
    ]


def _load_cfg() -> dict:
    user = {}
    for path in _cfg_paths():
        try:
            with open(path, encoding="utf-8") as f:
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
        try:
            _install_patch()
            logger.info("[better_qq] 补丁已安装")
        except Exception as e:
            logger.error(f"[better_qq] 安装补丁失败: {e}")

    @filter.on_decorating_result()
    async def on_decorating(self, event: AstrMessageEvent):
        """决定这条回复要不要引用：被 @ 必引用；每条新消息的首条回复必引用；其余按概率。"""
        try:
            import random

            cfg = _load_cfg()
            if not cfg.get("enabled", True):
                return
            is_private = not str(event.get_group_id() or "").strip()
            prob = float(
                cfg.get("quote_probability_private", 0.25)
                if is_private
                else cfg.get("quote_probability_group", 0.25)
            )

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
