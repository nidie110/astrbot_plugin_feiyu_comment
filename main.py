"""大肥鱼·答应才发（草稿 → 你点头 → 她发出去）。

设计要点（v1.1.0，按 Code Reviewer 复审意见收敛）：

1. **她能写，不能发**：唯一的 LLM 工具 `prepare_comment` 只落草稿。
2. **只有账号主人能点头**：`watch_approval` / `/发` / `/废` 要求 `event.is_admin()` 或 `owner_ids` 里的号；
   机器人自己发的消息一律不理（防回声自点头）。
3. **批准的就是你看到的那条**：草稿按会话（umo）隔离，同一会话只留最新一条；回执里带 id 与原文。
4. **状态机**：pending → posting → （成功删除 | uncertain 留档待人工核对），
   超时/读不到回执时不会静默重发，也不会假装撤回了。
5. **白名单用 urlsplit 判 host 边界**，只收 B 站；发布前比对页面最终 BV 与草稿一致。
6. **不阻塞事件循环**：整条发布链路丢进线程，内部超时之和 < 外层超时。
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parent))
_sys.modules.pop("_feiyu_kit", None)  # 顶层模块名，重载插件时先清缓存再导，避免用到旧字节
import _feiyu_kit as kit  # noqa: E402

PLUGIN_NAME = "astrbot_plugin_feiyu_comment"
SUBPROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

DEFAULT_CONFIG = {
    "enable": True,
    "dry_run": False,
    "owner_ids": "",
    "bsk_path": "",
    "bsk_browser": "",
    "max_chars": 180,
    "ttl_minutes": 120,
    "daily_limit": 5,
    "min_gap_seconds": 30,
}

AFFIRM = ("发出去吧", "就这样发", "可以发", "发出去", "就这个", "发吧", "发布", "发")
DENY = ("不发了", "不要发", "别发", "算了", "取消", "撤")
_PAD = r"[，。！!~～\s]*"
_AFFIRM_RE = re.compile(r"^" + _PAD + "(" + "|".join(AFFIRM) + r")" + _PAD + "$")
_DENY_RE = re.compile(r"^" + _PAD + "(" + "|".join(DENY) + r")" + _PAD + "$")
_REVISE_RE = re.compile(r"^(?:改[:：]|改\s*#\w+\s*[:：]|改成[:：]?)\s*(.+)$", re.S)
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_BILI_HOSTS = ("bilibili.com", "b23.tv", "bili2233.cn")

_READY_JS = """(() => {
  const s = window.__INITIAL_STATE__ || {};
  const oid = s.aid || (s.videoData && s.videoData.aid) || 0;
  return {ok: !!oid, oid: oid, href: location.href, uname: s.uname || ''};
})()"""

_POST_JS = """(async () => {
  const text = %s;
  const csrf = (document.cookie.match(/bili_jct=([^;]+)/) || [])[1] || '';
  if (!csrf) return {ok: false, code: -101, err: 'no-csrf'};
  const st = window.__INITIAL_STATE__ || {};
  const oid = st.aid || (st.videoData && st.videoData.aid);
  if (!oid) return {ok: false, err: 'no-aid'};
  const body = new URLSearchParams({type: '1', oid: String(oid), message: text, csrf: csrf});
  const r = await fetch('https://api.bilibili.com/x/v2/reply/add', {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: body});
  const j = await r.json().catch(() => ({code: -1, message: 'not-json'}));
  return {ok: j.code === 0, code: j.code, msg: j.message || '',
          rpid: (j.data && j.data.rpid) || 0, uname: st.uname || ''};
})()"""


def _now() -> float:
    return time.time()


class FeiyuComment(Star):
    """先把话写好给你看，你点头她才发——账号是你自己的，不能让她擅自开口。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.data_dir = Path("data/plugin_data") / PLUGIN_NAME
        try:
            from astrbot.core.star.star_tools import StarTools  # noqa: PLC0415

            self.data_dir = Path(str(StarTools.get_data_dir(PLUGIN_NAME)))
        except Exception:
            pass
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.data_dir / "config.json"
        self.draft_path = self.data_dir / "drafts.json"
        self.log_path = self.data_dir / "sent.json"
        self.plugin_cfg = dict(config or {})
        self.cfg = kit.merge_config(DEFAULT_CONFIG, self.plugin_cfg, self.cfg_path)
        if not self.cfg_path.exists():
            # 只在第一次落默认值；不把「合并后的生效值」写回去，否则 WebUI 清空会被老文件复活
            kit.save_json(self.cfg_path, DEFAULT_CONFIG, keep=0)
        self._lock = asyncio.Lock()

    # ---------------- 基础 ----------------

    def _load(self, path: Path) -> list:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as e:
            bak = path.with_name(f"{path.name}.corrupt-{int(_now())}")
            try:
                os.replace(path, bak)
            except Exception:
                pass
            logger.warning(f"[feiyu_comment] {path.name} 损坏，已备份为 {bak.name}: {e}")
            return []

    def _save(self, path: Path, data: list) -> None:
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(data[-200:], ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # ---------------- 草稿 ----------------

    def _ttl(self) -> int:
        return max(5, int(self.cfg.get("ttl_minutes", 120))) * 60

    def _pending(self, umo: str = "") -> list:
        drafts = self._load(self.draft_path)
        alive = [
            d for d in drafts
            if d.get("status") == "pending"
            and _now() - float(d.get("ts") or 0) < self._ttl()
            and (not umo or d.get("umo") == umo)
        ]
        return alive

    def _all_drafts(self) -> list:
        return self._load(self.draft_path)

    def _put_draft(self, umo: str, url: str, bv: str, text: str, note: str) -> dict:
        drafts = [d for d in self._load(self.draft_path) if d.get("status") != "pending" or d.get("umo") != umo]
        d = {
            "id": uuid.uuid4().hex[:6],
            "umo": umo,
            "url": url,
            "bv": bv,
            "text": text,
            "note": note,
            "ts": _now(),
            "status": "pending",
        }
        drafts.append(d)
        self._save(self.draft_path, drafts)
        return d

    def _set_status(self, did: str, status: str) -> None:
        drafts = self._load(self.draft_path)
        for d in drafts:
            if d.get("id") == did:
                d["status"] = status
                d["status_ts"] = _now()
        self._save(self.draft_path, drafts)

    def _drop_draft(self, did: str) -> None:
        self._save(self.draft_path, [d for d in self._load(self.draft_path) if d.get("id") != did])

    def _log_attempt(self, rec: dict) -> None:
        sent = self._load(self.log_path)
        sent.append(rec)
        self._save(self.log_path, sent)

    def _quota_error(self) -> str:
        """今天还能发吗？按「尝试」计数，失败也算——反复失败不能无限撞 B 站。"""
        sent = self._load(self.log_path)
        today = time.strftime("%Y-%m-%d")
        same_day = [r for r in sent if r.get("day") == today]
        limit = max(1, int(self.cfg.get("daily_limit", 5)))
        if len(same_day) >= limit:
            return f"今天已经试过 {len(same_day)} 次了（上限 {limit}），明天再说。"
        gap = max(0, int(self.cfg.get("min_gap_seconds", 30)))
        if same_day and gap:
            left = gap - (_now() - max(float(r.get("ts") or 0) for r in same_day))
            if left > 0:
                return f"刚发过一条，再等 {int(left)} 秒。"
        return ""

    # ---------------- 身份 ----------------

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        try:
            sid = str(event.get_sender_id() or "")
            if not sid:
                return False
            if hasattr(event, "get_self_id") and sid == str(event.get_self_id() or ""):
                return False  # 机器人自己说的话不算点头
            ids = {x.strip() for x in str(self.cfg.get("owner_ids") or "").replace("，", ",").split(",") if x.strip()}
            if ids:
                return sid in ids
            return bool(event.is_admin())
        except Exception as e:
            logger.warning(f"[feiyu_comment] 身份判断失败: {e}")
            return False

    # ---------------- 发布 ----------------

    @staticmethod
    def _check_url(url: str) -> tuple[str, str]:
        """只收 B 站链接；返回 (规范化 URL, BV 号或空)。"""
        u = (url or "").strip()
        if not u:
            raise ValueError("链接是空的")
        if _BV_RE.fullmatch(u):
            return f"https://www.bilibili.com/video/{u}", u
        if "://" not in u:
            u = "https://" + u
        sp = urlsplit(u)
        if sp.scheme not in ("http", "https"):
            raise ValueError("只认 http/https 链接")
        if "@" in (sp.netloc or ""):
            raise ValueError("链接里带 @，不敢用")
        host = (sp.hostname or "").lower().rstrip(".")
        ok = any(host == h or host.endswith("." + h) for h in _BILI_HOSTS)
        if not ok:
            raise ValueError("目前只接了 B 站（其他站点的接口各家不同，先别乱发）")
        bv = ""
        m = _BV_RE.search(sp.path or "")
        if m:
            bv = m.group(0)
        return u, bv

    def _wait_ready(self, bsk, sid: str, bv: str, seconds: float = 10.0) -> dict:
        deadline = _now() + seconds
        last = {}
        while _now() < deadline:
            last = bsk.evaluate(sid, _READY_JS, timeout=25)
            if last.get("ok"):
                href = str(last.get("href") or "")
                m = _BV_RE.search(href)
                final_bv = m.group(0) if m else ""
                if bv and final_bv and bv != final_bv:
                    raise RuntimeError("目标视频对不上（跳转到了别的视频）")
                return last
            time.sleep(0.5)
        raise RuntimeError("页面没打开（拿不到视频信息）")

    def _post_sync(self, url: str, text: str, bv: str) -> str:
        """同步执行，整体由线程跑：开会话 → 导航 → 校验 → 发布 → 关会话。"""
        bsk = kit.Bsk(self.cfg.get("bsk_path") or "", self.cfg.get("bsk_browser") or "")
        sid = ""
        try:
            sid = bsk.session_start()
            bsk.navigate(sid, url, timeout=60)
            self._wait_ready(bsk, sid, bv, 10.0)
            res = bsk.evaluate(sid, _POST_JS % json.dumps(text, ensure_ascii=False), timeout=60)
            if not res.get("ok"):
                code = res.get("code")
                if code == -101:
                    raise RuntimeError("B 站登录态失效了，去浏览器里重登一下")
                if code == -111:
                    raise RuntimeError("B 站说校验失败（-111），去浏览器里点进任意一个视频再试")
                if code == 12002:
                    raise RuntimeError("这个视频的评论区关了")
                if code == -412:
                    raise RuntimeError("被 B 站风控挡了（-412），过一阵子或者你手动发")
                if code in (-509, -352):
                    raise RuntimeError("发得太密被拦了，等会儿再试")
                raise RuntimeError(f"B 站没接受（code={code}）")
            return str(res.get("rpid") or "")
        finally:
            bsk.session_stop(sid)

    async def _send(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageChain([Comp.Plain(text)]))
        except Exception as e:
            logger.error(f"[feiyu_comment] 发送失败: {e}")

    async def _confirm(self, event: AstrMessageEvent, draft: dict) -> None:
        """你点头之后的唯一入口。"""
        if not self.cfg.get("enable", True):
            await self._send(event, "发评论这功能关着，这条没发。")
            return
        if draft.get("status") != "pending":
            await self._send(event, f"#{draft['id']} 现在不是待发状态（{draft.get('status')}），没动。")
            return
        async with self._lock:
            self._set_status(draft["id"], "posting")
            text, url, bv = draft["text"], draft["url"], draft.get("bv") or ""
            if self.cfg.get("dry_run", False):
                self._set_status(draft["id"], "pending")
                await self._send(event, f"（试运行，没真发）#{draft['id']}：{text}")
                return
            try:
                rpid = await asyncio.wait_for(
                    asyncio.to_thread(self._post_sync, url, text, bv), timeout=150
                )
            except asyncio.TimeoutError:
                self._set_status(draft["id"], "uncertain")
                self._log_attempt({"ts": _now(), "day": time.strftime("%Y-%m-%d"), "url": url,
                                   "text": text, "ok": "unknown", "rpid": ""})
                await self._send(event, f"#{draft['id']} 等回执超时了——很可能已经发出去了。"
                                        "先去 B 站看一眼，别急着重发。")
                return
            except asyncio.CancelledError:
                self._set_status(draft["id"], "uncertain")
                raise
            except Exception as e:
                self._set_status(draft["id"], "uncertain")
                self._log_attempt({"ts": _now(), "day": time.strftime("%Y-%m-%d"), "url": url,
                                   "text": text, "ok": "fail", "rpid": ""})
                logger.warning(f"[feiyu_comment] 发布失败 #{draft['id']}: {e}")
                await self._send(event, f"没发出去：{kit.user_hint(e)}")
                return
            self._drop_draft(draft["id"])
            self._log_attempt({"ts": _now(), "day": time.strftime("%Y-%m-%d"), "url": url,
                               "text": text, "ok": "ok", "rpid": rpid})
            await self._send(event, f"发出去了 #{draft['id']}（rpid={rpid or '未知'}）：{text}")

    # ---------------- 工具（她只能存草稿） ----------------

    @filter.llm_tool(name="prepare_comment")
    async def prepare_comment(self, event: AstrMessageEvent, url: str, text: str, note: str = "") -> str:
        """准备好一条要发到网上的评论，但**不发布**，等用户点头。

        什么时候用：用户让你去某个 B 站视频底下留言时。先把内容写好，用这个工具存成草稿，
        然后把原文贴给用户看，等他回「发」。

        Args:
            url(string): B 站视频页链接（或 BV 号）。动态、专栏还不支持。
            text(string): 评论原文，说人话，长度不超过配置的字数上限（默认 180）。
            note(string): 为什么这么写（可选，只给用户看）
        """
        if not self.cfg.get("enable", True):
            return "发评论这个功能关着。"
        try:
            norm, bv = self._check_url(url)
        except ValueError as e:
            return str(e)
        text = (text or "").strip()
        if not text:
            return "评论是空的。"
        limit = max(20, int(self.cfg.get("max_chars", 180)))
        if len(text) > limit:
            return f"太长了（{len(text)} 字），压到 {limit} 字以内再说。"
        if not self._is_owner(event):
            return "这条消息的发送者不是账号主人，我不能替他发言。"
        d = self._put_draft(event.unified_msg_origin, norm, bv, text, note)
        logger.info(f"[feiyu_comment] 草稿 #{d['id']} 已存（umo={event.unified_msg_origin}）")
        return (f"草稿 #{d['id']} 已存好（还没发）。\n评论：{text}\n目标：{norm}\n"
                f"把原文和目标原样发给用户，让他回「发」你才能发；他回「改：……」就改，回「算了」就作废。"
                "在此之前不要尝试任何发布手段。")

    # ---------------- 你点头才发 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def watch_approval(self, event: AstrMessageEvent):
        """盯着账号主人的回话：只有「发 / 改 / 算了」会动草稿。"""
        msg = (event.message_str or "").strip()
        if not msg or msg.startswith("/"):
            return
        if not self._is_owner(event):
            return
        pending = self._pending(event.unified_msg_origin)
        if not pending:
            return
        draft = pending[-1]
        if _AFFIRM_RE.match(msg):
            event.stop_event()
            await self._confirm(event, draft)
            return
        if _DENY_RE.match(msg):
            event.stop_event()
            async with self._lock:
                cur = next((d for d in self._all_drafts() if d.get("id") == draft["id"]), draft)
                if cur.get("status") == "posting":
                    await self._send(event, f"#{draft['id']} 已经在发了，撤不回来——去 B 站看一眼，需要就手动删。")
                    return
                self._drop_draft(draft["id"])
            await self._send(event, f"好，#{draft['id']} 作废了。")
            return
        m = _REVISE_RE.match(msg)
        if m:
            new = m.group(1).strip()
            limit = max(20, int(self.cfg.get("max_chars", 180)))
            event.stop_event()
            if len(new) > limit:
                await self._send(event, f"太长了，{limit} 字以内。")
                return
            drafts = self._load(self.draft_path)
            for d in drafts:
                if d.get("id") == draft["id"]:
                    d["text"] = new
                    d["ts"] = _now()
            self._save(self.draft_path, drafts)
            await self._send(event, f"改好了 #{draft['id']}：{new}\n还发吗？")

    # ---------------- 显式命令 ----------------

    @filter.command("发")
    async def cmd_send(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield event.plain_result("只有账号主人能让我发。")
            return
        event.stop_event()
        pending = self._pending(event.unified_msg_origin)
        if not pending:
            yield event.plain_result("这里没有待发的草稿。")
            return
        arg = (event.message_str or "").strip().split(maxsplit=1)
        want = arg[1].strip().lstrip("#") if len(arg) > 1 else ""
        draft = pending[-1]
        if want:
            hit = [d for d in pending if d["id"] == want]
            if not hit:
                yield event.plain_result(f"这个会话里没有 #{want} 这个草稿。")
                return
            draft = hit[0]
        await self._confirm(event, draft)

    @filter.command("草稿")
    async def cmd_list(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield event.plain_result("只有账号主人能看草稿。")
            return
        pending = self._pending(event.unified_msg_origin)
        if not pending:
            yield event.plain_result("这里没有草稿。")
            return
        lines = [f"#{d['id']}  {d['text'][:60]}  →  {d['url']}" for d in pending[-5:]]
        yield event.plain_result("待发的草稿：\n" + "\n".join(lines))

    @filter.command("废")
    async def cmd_drop(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield event.plain_result("只有账号主人能废草稿。")
            return
        pending = self._pending(event.unified_msg_origin)
        if not pending:
            yield event.plain_result("这里没有草稿可废。")
            return
        self._drop_draft(pending[-1]["id"])
        yield event.plain_result(f"已作废 #{pending[-1]['id']}。")

    @filter.command("发评论")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "怎么让我发评论：\n"
            "1. 你把 B 站视频链接发给她，让她写一句\n"
            "2. 她把原文贴出来等你点头\n"
            "3. 你回「发」就发出去，回「改：……」就改，回「算了」就作废\n"
            "只有账号主人能点头；指令：/草稿 看队列 · /废 作废 · /发 [id] 直接发"
        )

    async def terminate(self):
        """插件卸载/重载时收尾（发布会话是每条自开自关的，这里只留钩子）。"""
        logger.info("[feiyu_comment] 卸载")
