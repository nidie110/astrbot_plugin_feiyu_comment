"""feiyu kit —— 大肥鱼几个插件共用的底层件。

只放「各插件反复各写一遍、且最容易出事」的东西：
  bsk 浏览器封装、媒体下载与抽帧、状态文件原子写、错误分层、配置合并、搜索。

设计规矩：
  * 不 import astrbot（保持可单测、也能在插件外跑）；
  * 不抛未分类异常给上层：bsk 失败一律 `KitError(kind=..., detail=...)`；
  * 日志走标准 logging，插件自己配置 handler；
  * 这是「一份源文件 + 同步到各插件目录」的 vendor 模式，改完跑 sync_kit.py。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

__version__ = "1.0.0"
logger = logging.getLogger("feiyu_kit")
_LOCK = threading.Lock()

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class KitError(RuntimeError):
    """带种类的失败，插件按 kind 决定跟用户说什么。"""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


# ---------------------------------------------------------------- bsk


def find_bsk(configured: str = "") -> str:
    cfg = str(configured or "").strip()
    if cfg and Path(cfg).is_file():
        return cfg
    found = shutil.which("bsk") or shutil.which("bsk.exe")
    if found:
        return found
    cand = Path.home() / ".local" / "bin" / "bsk.exe"
    return str(cand) if cand.is_file() else ""


def _last_json(text: str) -> dict:
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except Exception:
                continue
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise KitError("bsk-no-json", "bsk 没有返回 JSON")
    try:
        return json.loads(m.group(0))
    except Exception as e:
        raise KitError("bsk-no-json", str(e)) from e


def _daemon_alive() -> bool:
    """`~/.bsk/daemon.json` 里记的那个 pid 现在还活着吗。"""
    try:
        d = json.loads((Path.home() / ".bsk" / "daemon.json").read_text(encoding="utf-8"))
        pid = int(d.get("pid") or 0)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
        return str(pid) in (r.stdout or "")
    except Exception:
        return False


def ensure_daemon(bsk_exe: str, seconds: float = 25.0) -> bool:
    """bsk daemon 没在跑就自己拉起来。

    daemon 会自己死（进程崩、机器重启、被任务管理器带走），死了以后每条 bsk 命令都返回
    `ensure daemon is running`。这里用 DETACHED_PROCESS 起 `--foreground` 模式——
    bsk 自带的 detached 启动在某些宿主里会报 Job Object breakaway 失败，这条路径更稳。
    """
    if _daemon_alive():
        return True
    if not bsk_exe:
        return False
    logger.warning("[feiyu_kit] bsk daemon 没在跑，尝试自动拉起")
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [bsk_exe, "daemon", "start", "--foreground"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags, close_fds=True,
        )
    except Exception as e:
        logger.error("[feiyu_kit] 拉 bsk daemon 失败: %s", e)
        return False
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(0.8)
        if _daemon_alive():
            time.sleep(1.5)  # 再给它一点时间把 socket / 扩展握手弄好
            logger.info("[feiyu_kit] bsk daemon 已拉起")
            return True
    logger.error("[feiyu_kit] bsk daemon 拉起来了但没就绪（%.0fs）", seconds)
    return False


def _looks_like_daemon_dead(text: str) -> bool:
    t = (text or "").lower()
    return "daemon" in t or "pipe" in t or "connect" in t


class Bsk:
    """bsk 命令行的薄封装：所有调用都是同步的，调用方自己决定扔不扔线程。"""

    def __init__(self, exe: str = "", browser: str = "", timeout: int = 60):
        self.exe = find_bsk(exe)
        self.browser = str(browser or "").strip()
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.exe)

    def run(self, *args, timeout: int | None = None) -> str:
        return self._run(args, timeout, allow_retry=True)

    def _run(self, args, timeout, allow_retry: bool) -> str:
        if not self.exe:
            raise KitError("no-bsk", "没找到 bsk，先在配置里填 bsk_path")
        env = dict(os.environ)
        env["BSK_AUTO_START"] = "0"
        try:
            p = subprocess.run(
                [self.exe, *args], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout or self.timeout, env=env,
                creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as e:
            raise KitError("bsk-timeout", f"bsk {' '.join(args[:2])} 超时") from e
        except FileNotFoundError as e:
            raise KitError("no-bsk", str(e)) from e
        if p.returncode != 0:
            # 原始 stderr 只进日志，别拼进用户可见文案
            logger.debug("[feiyu_kit] bsk stderr: %s", (p.stderr or "")[:500])
            # daemon 死了是最常见的失败（每条命令都返回 ensure daemon is running），
            # 先自己拉一次再重试，别把「daemon 崩了」当成「插件坏了」递给用户。
            if allow_retry and _looks_like_daemon_dead(f"{p.stderr or ''}{p.stdout or ''}"):
                if ensure_daemon(self.exe):
                    logger.info("[feiyu_kit] 重试 bsk %s", " ".join(str(a) for a in args[:2]))
                    return self._run(args, timeout, allow_retry=False)
            raise KitError("bsk-failed", f"exit {p.returncode}")
        return p.stdout or ""

    def run_json(self, *args, timeout: int | None = None) -> dict:
        return _last_json(self.run(*args, timeout=timeout))

    def browser_id(self) -> str:
        if self.browser:
            return self.browser
        info = self.run_json("status", "--json", timeout=30)
        for b in info.get("browsers") or []:
            if b.get("instance_id"):
                self.browser = b["instance_id"]
                return self.browser
        raise KitError("no-browser", "没有可用的浏览器实例（先手动运行一次 bsk）")

    def session_start(self) -> str:
        info = self.run_json("session", "start", "--json", "--no-focus",
                             "--browser", self.browser_id(), timeout=30)
        sid = (info.get("session") or {}).get("session_id") or info.get("session_id") or ""
        if not sid:
            raise KitError("no-session", "浏览器会话没开起来")
        return sid

    def session_stop(self, sid: str) -> None:
        if not sid:
            return
        try:
            self.run("session", "stop", sid, timeout=20)
        except KitError as e:
            logger.warning("[feiyu_kit] 关会话失败 %s: %s", sid, e)

    def navigate(self, sid: str, url: str, timeout: int = 60) -> None:
        self.run("navigate", "--session", sid, "--json", url, timeout=timeout)

    def evaluate(self, sid: str, script: str, timeout: int = 60) -> dict:
        out = self.run_json("evaluate", "--session", sid, "--json", script, timeout=timeout)
        value = out.get("value")
        return value if isinstance(value, dict) else {"value": value}

    def screenshot(self, sid: str, out: Path, timeout: int = 60) -> Path:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        self.run("screenshot", "--session", sid, "--out", str(out), timeout=timeout)
        if not out.exists():
            raise KitError("shot-failed", "截图没落地")
        return out


# ---------------------------------------------------------------- 媒体


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg  # noqa: PLC0415

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg") or ""


def find_ytdlp(configured: str = "") -> str:
    cfg = str(configured or "").strip()
    if cfg and Path(cfg).is_file():
        return cfg
    for name in ("yt-dlp", "yt-dlp.exe"):
        found = shutil.which(name)
        if found:
            return found
    for cand in (
        Path.home() / ".agent-reach-venv" / "Scripts" / "yt-dlp.exe",
        Path.home() / ".local" / "bin" / "yt-dlp.exe",
        Path.home() / "AppData" / "Roaming" / "uv" / "tools" / "yt-dlp" / "Scripts" / "yt-dlp.exe",
        Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Scripts" / "yt-dlp.exe",
        Path.home() / "AppData" / "Roaming" / "Python" / "Scripts" / "yt-dlp.exe",
    ):
        if cand.is_file():
            return str(cand)
    return ""


FORMAT_CHAIN = "worstvideo[height<={h}]/worstvideo/worst/bv*/b"


def ytdlp_download(url: str, out: Path, *, seconds: int = 30, height: int = 480,
                   ytdlp: str = "", ffmpeg: str = "", timeout: int = 120) -> Path:
    """只下前 seconds 秒的小视频。返回落地文件；失败抛 KitError。"""
    exe = find_ytdlp(ytdlp)
    if not exe:
        raise KitError("no-ytdlp", "找不到 yt-dlp")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe, "--no-playlist", "--no-warnings", "--no-mtime", "--no-part",
        "--socket-timeout", "15", "--retries", "2",
        "-f", FORMAT_CHAIN.format(h=int(height or 480)),
        "--download-sections", f"*0-{max(1, int(seconds or 30))}",
        "-o", str(out),
    ]
    ff = ffmpeg or ffmpeg_exe()
    if ff:
        cmd += ["--ffmpeg-location", ff]
    cmd += ["--", url]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        raise KitError("ytdlp-timeout", "下载超时") from e
    if p.returncode != 0:
        logger.debug("[feiyu_kit] yt-dlp stderr: %s", (p.stderr or "")[-500:])
        raise KitError("ytdlp-failed", "yt-dlp 没能取到视频流")
    for _ in range(10):
        if out.exists() and out.stat().st_size >= 10_000:
            return out
        time.sleep(0.2)
    if out.exists() and out.stat().st_size > 0:
        raise KitError("ytdlp-failed", "下到的是错误页或空文件")
    raise KitError("ytdlp-failed", "下载完但没找到文件")


def video_duration(path: Path) -> float:
    import imageio_ffmpeg  # noqa: PLC0415

    try:
        reader = imageio_ffmpeg.read_frames(str(path))
        meta = next(reader)
        dur = float(meta.get("duration") or 0.0)
        try:
            reader.close()
        except Exception:
            pass
        return dur
    except Exception:
        return 0.0


def extract_frames(path: Path, outdir: Path, *, count: int = 4, width: int = 640,
                   span: float | None = None) -> list[Path]:
    """均匀抽 count 帧，落成 jpg。"""
    from PIL import Image  # noqa: PLC0415

    path, outdir = Path(path), Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    dur = video_duration(path)
    span = float(span or dur or 0)
    if span <= 0:
        span = 5.0
    n = max(1, int(count or 4))
    times = [span * (i + 1) / (n + 1) for i in range(n)]
    tmpdir = Path(tempfile.mkdtemp(prefix="feiyu_frames_"))
    shots: list[Path] = []
    try:
        for i, t in enumerate(times):
            target = tmpdir / f"f{i}.jpg"
            ok = _grab_frame(path, t, target, ffmpeg_exe())
            if not ok:
                continue
            try:
                with Image.open(target) as im:
                    if width and im.width > width:
                        h = max(1, round(im.height * width / im.width))
                        im = im.resize((width, h), Image.LANCZOS)
                    dst = outdir / f"f{i}.jpg"
                    im.convert("RGB").save(dst, quality=88)
                    shots.append(dst)
            except Exception as e:
                logger.warning("[feiyu_kit] 抽帧写入失败: %s", e)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return shots


def _grab_frame(src: Path, t: float, dst: Path, ffmpeg: str) -> bool:
    if not ffmpeg:
        return False
    cmd = [ffmpeg, "-y", "-ss", f"{max(0.0, t):.2f}", "-i", str(src),
           "-frames:v", "1", "-q:v", "3", str(dst)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False
    return p.returncode == 0 and dst.exists()


def crop_rect(img: Path, rect: dict, viewport: dict, shot_w: int, shot_h: int, out: Path) -> Path:
    """按「元素 ∩ 视口」裁出播放器区域；rect/viewport 都是 CSS px。"""
    from PIL import Image  # noqa: PLC0415

    vw = float(viewport.get("w") or 0) or float(shot_w)
    scale = shot_w / vw if vw else 1.0
    x0 = max(0.0, float(rect.get("x") or 0))
    y0 = max(0.0, float(rect.get("y") or 0))
    x1 = min(vw, float(rect.get("x") or 0) + float(rect.get("w") or 0))
    y1 = min(float(viewport.get("h") or shot_h / scale),
             float(rect.get("y") or 0) + float(rect.get("h") or 0))
    if x1 <= x0 or y1 <= y0:
        raise KitError("crop-empty", "播放器区域不在可视范围")
    box = (int(x0 * scale), int(y0 * scale), int(x1 * scale), int(y1 * scale))
    with Image.open(img) as im:
        im.crop(box).convert("RGB").save(out, quality=88)
    return out


def sweep_dir(path: Path, *, hours: float = 24.0) -> int:
    """删掉超过 hours 小时的文件，返回删除数。"""
    path = Path(path)
    if not path.exists():
        return 0
    cutoff = time.time() - hours * 3600
    n = 0
    for f in path.rglob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except Exception:
            continue
    return n


# ---------------------------------------------------------------- 端口


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    import socket  # noqa: PLC0415

    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def wait_port(port: int, seconds: float = 60.0, host: str = "127.0.0.1") -> bool:
    import time as _t  # noqa: PLC0415

    end = _t.time() + seconds
    while _t.time() < end:
        if port_open(port, host):
            return True
        _t.sleep(0.5)
    return False


# ---------------------------------------------------------------- 状态文件


def load_json_list(path: Path) -> list:
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        bak = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            os.replace(path, bak)
        except Exception:
            pass
        logger.warning("[feiyu_kit] %s 损坏，已备份 %s: %s", path.name, bak.name, e)
        return []


def load_json_dict(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        bak = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            os.replace(path, bak)
        except Exception:
            pass
        logger.warning("[feiyu_kit] %s 损坏，已备份 %s: %s", path.name, bak.name, e)
        return {}


def save_json(path: Path, data, keep: int = 200) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, list) and keep:
        data = data[-keep:]
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 配置


def merge_config(defaults: dict, plugin_cfg: dict | None, legacy_path: Path | None = None) -> dict:
    """默认值 ← 老配置 ← AstrBot 注入；空值不覆盖（避免 WebUI 空串冲掉老配置）。"""
    cfg = dict(defaults)
    if legacy_path and Path(legacy_path).exists():
        cfg.update(load_json_dict(legacy_path))
    if plugin_cfg:
        cfg.update({k: v for k, v in plugin_cfg.items() if v not in (None, "")})
    return cfg


# ---------------------------------------------------------------- 网络


def deepseek_search(query: str, *, count: int = 5, need_content: bool = True,
                    key: str = "", api: str = "", timeout: int = 20) -> list:
    """DeepSeek 搜索接口；返回 [{title,url,site_name,date,summary}]。"""
    import urllib.request  # noqa: PLC0415

    q = (query or "").strip()
    if not q:
        raise KitError("empty-query", "空查询")
    if not key:
        raise KitError("no-key", "没配搜索 key")
    body = json.dumps({"query": q, "count": max(1, min(20, int(count))),
                       "need_content": bool(need_content)}).encode()
    endpoint = str(api or "").strip() or "https://api.deepseek.com/v1/tools/search"
    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        raise KitError("search-failed", str(e)) from e
    hits = data.get("results") or data.get("data") or []
    out = []
    for h in hits:
        if not isinstance(h, dict):
            continue
        out.append({
            "title": str(h.get("title") or "").strip(),
            "url": str(h.get("url") or "").strip(),
            "site": str(h.get("site_name") or h.get("site") or "").strip(),
            "date": str(h.get("date") or "").strip(),
            "summary": str(h.get("summary") or h.get("snippet") or h.get("content") or "").strip(),
        })
    return out


# ---------------------------------------------------------------- 能力自述
# 各插件在 __init__ 里登记一句「我会做什么」，落成一个小 json 文件；
# persona_mind 每轮读它，拼成一段自述塞进 system prompt。
# 这样她对「自己有什么本事」是清楚的，不用等用户问「你能干嘛吗」。
# 走文件而不是内存：插件加载顺序不定，文件不受顺序影响，也能跨重启。

CAP_FILE = Path("data/plugin_data/_feiyu_caps.json")


def register_capability(key: str, sentence: str, tier: str = "all") -> None:
    """登记一项能力。tier: all=谁都能用；admin=只有账号主人。"""
    text = (sentence or "").strip()
    if not text:
        return
    try:
        CAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if CAP_FILE.exists():
            try:
                data = json.loads(CAP_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[str(key)] = {"sentence": text, "tier": "admin" if tier == "admin" else "all"}
        tmp = CAP_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CAP_FILE)
    except Exception:
        pass  # 登记失败不能影响插件本身


def capability_block(*, admin: bool = False, header: str = "") -> str:
    """拼一段「你手上的本事」。只读文件，读不到就返回空串（插件没装就什么都不说）。"""
    try:
        if not CAP_FILE.exists():
            return ""
        data = json.loads(CAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""
    lines = []
    for item in (data or {}).values():
        if not isinstance(item, dict):
            continue
        if item.get("tier") == "admin" and not admin:
            continue
        text = str(item.get("sentence") or "").strip()
        if text:
            lines.append(f"- {text}")
    if not lines:
        return ""
    head = header or "你手上确实有这些本事（都是你自己的，直接用，别解释自己是怎么做到的，也别把工具名说出去）："
    return head + "\n" + "\n".join(lines)


# ---------------------------------------------------------------- 日记素材
# feiyu_video / feiyu_explore 一直在写这个格式；现在浏览网页、发评论也往里写，
# 这样她自己做过的事会真的过夜、进日记、影响信念。


# ---------------------------------------------------------------- 动作注册表
# 主模型只看见一个入口工具 feiyu(action)，具体动作由各插件在 __init__ 里登记。
# 这样她的工具箱是「看视频/搜/读网页/发评论」这种动词，而不是十几个机械步骤。

ACTIONS: dict = {}


def register_action(key: str, handler, desc: str = "") -> None:
    """登记一个任务级动作（同进程内共享）。handler 形如 async (event, **kw) -> str。"""
    key = (key or "").strip()
    if not key or not callable(handler):
        return
    ACTIONS[key] = {"handler": handler, "desc": (desc or "").strip()}


def get_action(key: str):
    item = ACTIONS.get((key or "").strip())
    return item["handler"] if item else None


def action_help() -> str:
    return "、".join(k for k in ACTIONS) or "（还没登记）"


MOOD_FILE = Path("data/plugin_data/_feiyu_mood.jsonl")
_DIMS = ("P", "Ar", "D")


def record_mood(effects, *, umo: str = "", source: str = "") -> bool:
    """把她做过的事转成一次情绪冲量（P/Ar/D），排队交给 persona_mind 结算。

    走文件而不是直接 import：插件加载顺序不定，她也不该知道「谁在改我的心情」。
    冲量会先排队，等她下一次开口前统一衰减+合并，避免两条消息之间情绪乱跳。
    """
    if not isinstance(effects, dict):
        return False
    clean = {}
    for k in _DIMS:
        v = effects.get(k)
        if isinstance(v, bool):
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(num) and abs(num) > 1e-9:
            clean[k] = max(-8.0, min(8.0, num))
    if not clean:
        return False
    row = {"ts": time.time(), "umo": str(umo or ""), "source": str(source or ""),
           "effects": clean}
    with _LOCK:
        try:
            MOOD_FILE.parent.mkdir(parents=True, exist_ok=True)
            with MOOD_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + chr(10))
            return True
        except OSError:
            return False


def material_dir(override: str = "") -> Path:
    raw = (override or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path("data/plugin_data/astrbot_plugin_persona_mind/diary_materials")


def record_material(text: str, *, kind: str = "event", source: str = "",
                    umo: str = "", extra: dict | None = None, mood=None,
                    dir_override: str = "", max_per_day: int = 40) -> bool:
    """写一条日记素材（和 feiyu_video 同格式）。重复文本当天只记一次。

    读只用来判断（去重、当日上限），写入一律 append：
    persona_mind 的 Diary.add 也在往同一个文件追加，整文件重写在并发时会把它刚写的那行吞掉。
    """
    body = (text or "").strip()
    if not body:
        return False
    try:
        base = material_dir(dir_override)
        base.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y-%m-%d")
        path = base / f"{day}.jsonl"
        with _LOCK:
            return _append_locked(path, body, kind, source, umo, mood, extra, max_per_day)
    except Exception:
        return False


def _append_locked(path, body, kind, source, umo, mood, extra, max_per_day) -> bool:
    """调用方必须已持有 _LOCK：读-判-写整段不能拆开，否则并发会突破去重与日上限。"""
    try:
        rows: list = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        if body[:200] in [str(r.get("text") or "")[:200] for r in rows if isinstance(r, dict)]:
            return False
        if len(rows) >= max(1, int(max_per_day)):
            return False
        rec = {"ts": time.time(), "time": time.strftime("%H:%M"), "kind": kind,
               "text": body[:400], "source": source or "feiyu", "umo": umo}
        if mood:
            rec["mood"] = mood
        if extra:
            for k, v in extra.items():
                if k not in rec:
                    rec[k] = v
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + chr(10))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 错误文案


_HINTS = {
    "no-bsk": "这台机器上没找到 bsk，先在插件配置里填路径",
    "bsk-timeout": "浏览器超时了，可能卡住了",
    "bsk-failed": "浏览器那边出错了",
    "bsk-no-json": "浏览器返回的东西看不懂（可能版本不对）",
    "no-browser": "没连上浏览器（先手动开一次 bsk）",
    "no-session": "浏览器会话没开起来",
    "no-ytdlp": "这台机器上没找到 yt-dlp",
    "ytdlp-timeout": "下载视频超时了",
    "ytdlp-failed": "这个视频取不到流（可能要登录或有限制）",
    "shot-failed": "截图没成",
    "crop-empty": "播放器不在可视范围里",
    "no-key": "没配搜索 key",
    "search-failed": "这会儿搜不动",
    "empty-query": "查询是空的",
}


def user_hint(exc: BaseException) -> str:
    """把底层异常翻成一句能跟用户说的话，细节只进日志。

    兼容三种输入：KitError、`RuntimeError("no-ytdlp: ...")` 这类「kind: 细节」字符串、
    TimeoutError/TimeoutExpired。认不出就返回通用文案。
    """
    if isinstance(exc, KitError):
        return _HINTS.get(exc.kind, "出错了")
    if isinstance(exc, (TimeoutError,)) or exc.__class__.__name__ == "TimeoutExpired":
        return "等太久了，超时"
    kind = str(exc or "").split(":", 1)[0].strip()
    if kind in _HINTS:
        return _HINTS[kind]
    return "出错了"


def clean_title(s: str) -> str:
    t = re.sub(r"[_\-|]\s*(哔哩哔哩|bilibili|抖音|douyin|youtube)\s*$", "", (s or "").strip(), flags=re.I)
    t = re.sub(r"_哔哩哔哩_bilibili$", "", t)
    return t.strip() or (s or "").strip()


def clean_prose(s: str) -> str:
    t = re.sub(r"^\s*\d+[).、]\s*", "", (s or ""), flags=re.M)
    return re.sub(r"\s*\n+\s*", " ", t).strip()


# ---------------------------------------------------------------- 认人
#
# 背景：AstrBot 的 `event.is_admin()` 实现就是 `return self.role == "admin"`，
# 而 QQ **私聊**事件没有群角色，role 永远是空 —— 所以私聊里 is_admin() 恒为 False。
# 结果就是「主人在私聊里被当成普通用户」：读个公网页面被拒、管理员动作全不给。
# 正确做法是按号认人，这里做成一处，各插件别再各写一遍。

_ADMINS_CACHE = {"ts": 0.0, "ids": set()}


def global_admins(path: str | Path = "data/cmd_config.json") -> set[str]:
    """AstrBot 全局管理员号（cmd_config.json 的 admins_id），带 60 秒缓存。"""
    now = time.time()
    if now - _ADMINS_CACHE["ts"] < 60:
        return set(_ADMINS_CACHE["ids"])
    ids: set[str] = set()
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        for x in raw.get("admins_id") or []:
            x = str(x).strip()
            if x:
                ids.add(x)
    except Exception as e:
        logger.debug(f"读全局管理员列表失败（不影响主流程）: {e}")
    _ADMINS_CACHE["ts"] = now
    _ADMINS_CACHE["ids"] = set(ids)
    return ids


def is_owner(event, cfg=None, key: str = "owner_ids") -> bool:
    """这条消息是不是账号主人发的。

    判定顺序：插件配置的 owner_ids（填了就只认它）→ 全局 admins_id → event.is_admin()。
    机器人自己发的消息一律不算（防「自己的回复被当成人点头」）。
    """
    try:
        sid = str(event.get_sender_id() or "")
    except Exception:
        sid = ""
    if not sid:
        return False
    try:
        if hasattr(event, "get_self_id") and sid == str(event.get_self_id() or ""):
            return False
    except Exception:
        pass

    raw = ""
    try:
        if cfg is not None and hasattr(cfg, "get"):
            raw = str(cfg.get(key) or "")
    except Exception:
        raw = ""
    ids = {x.strip() for x in raw.replace("，", ",").replace(" ", ",").split(",") if x.strip()}
    if sid in ids:
        return True
    if ids:
        # 填了白名单就只认它：插件级比全局管理员更细，不让全局名单绕过
        return False
    if sid in global_admins():
        return True
    try:
        return bool(event.is_admin())
    except Exception:
        return False
