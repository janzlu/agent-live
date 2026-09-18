"""
IDE Watcher module for Mode A (Copilot Co-Pilot & Navigator).
Monitors Antigravity IDE and Cursor agent transcripts in real-time,
providing chat context awareness, task execution state tracking,
zero-latency in-memory cache, and proactive error & completion event notifications to Gemini Live.
"""

import os
import re
import glob
import json
import time
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, Callable, Awaitable, List, Tuple

# 全局内存快照缓存（零延迟提供给 Gemini 工具调用）
GLOBAL_CACHED_SNAPSHOT: Dict[str, Any] = {}

_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL | re.IGNORECASE)


def workspace_to_cursor_project_slug(workspace_root: str) -> str:
    """Map absolute workspace path to Cursor projects slug, e.g. /Users/a/b -> Users-a-b."""
    resolved = str(Path(workspace_root).expanduser().resolve())
    return resolved.lstrip("/").replace("/", "-")


def get_active_ide_transcript() -> Optional[Dict[str, Any]]:
    """
    定位 Antigravity IDE 当前最活跃的会话路径与 ID。
    """
    base = os.path.expanduser("~/.gemini/antigravity-ide/brain")
    if not os.path.exists(base):
        return None

    pattern = os.path.join(base, "*/.system_generated/logs/transcript.jsonl")
    transcripts = glob.glob(pattern)
    valid = []
    for t in transcripts:
        conv_id = t.split("/")[-4]
        # 排除系统内部非 UUID 目录
        if len(conv_id) >= 30:
            try:
                mtime = os.path.getmtime(t)
                valid.append((mtime, t, conv_id))
            except OSError:
                continue

    if not valid:
        return None

    valid.sort(key=lambda x: x[0], reverse=True)
    mtime, transcript_path, conv_id = valid[0]
    return {
        "source": "antigravity",
        "conversation_id": conv_id,
        "transcript_path": transcript_path,
        "brain_dir": os.path.dirname(os.path.dirname(os.path.dirname(transcript_path))),
        "last_modified": mtime,
    }


def get_active_cursor_transcript(workspace_root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    定位 Cursor 当前最活跃的 agent-transcripts/*.jsonl。
    优先绑定 workspace_root 对应的 ~/.cursor/projects/<slug>/agent-transcripts。
    """
    projects_root = Path.home() / ".cursor" / "projects"
    if not projects_root.is_dir():
        return None

    candidates: List[Tuple[float, Path]] = []
    if workspace_root:
        slug = workspace_to_cursor_project_slug(workspace_root)
        scoped = projects_root / slug / "agent-transcripts"
        if scoped.is_dir():
            for path in scoped.rglob("*.jsonl"):
                try:
                    candidates.append((path.stat().st_mtime, path))
                except OSError:
                    continue
    if not candidates:
        for path in projects_root.glob("*/agent-transcripts/*/*.jsonl"):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    mtime, transcript_path = candidates[0]
    conv_id = transcript_path.stem
    return {
        "source": "cursor",
        "conversation_id": conv_id,
        "transcript_path": str(transcript_path),
        "brain_dir": str(transcript_path.parent),
        "last_modified": mtime,
        "workspace_slug": transcript_path.parts[-4] if len(transcript_path.parts) >= 4 else None,
    }


def _decode_cursor_message(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Cursor transcript message field into a dict with content[]."""
    msg = entry.get("message")
    if isinstance(msg, str):
        try:
            msg = json.loads(msg)
        except Exception:
            return {"content": [{"type": "text", "text": msg}]}
    if isinstance(msg, dict):
        return msg
    return {"content": []}


def extract_cursor_user_query(entry: Dict[str, Any]) -> Optional[str]:
    msg = _decode_cursor_message(entry)
    texts: List[str] = []
    for part in msg.get("content", []) or []:
        if part.get("type") == "text" and part.get("text"):
            texts.append(part["text"])
    blob = "\n".join(texts)
    m = _USER_QUERY_RE.search(blob)
    if m:
        return m.group(1).strip()
    cleaned = re.sub(r"<timestamp>.*?</timestamp>", "", blob, flags=re.DOTALL).strip()
    return cleaned or None


def extract_cursor_assistant_text(entry: Dict[str, Any]) -> Optional[str]:
    msg = _decode_cursor_message(entry)
    texts = [
        part.get("text", "").strip()
        for part in msg.get("content", []) or []
        if part.get("type") == "text" and part.get("text")
    ]
    if not texts:
        return None
    return "\n".join(texts).strip()


def extract_cursor_tool_actions(entry: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return list of (tool_name, short_desc) from a Cursor assistant entry."""
    msg = _decode_cursor_message(entry)
    actions: List[Tuple[str, str]] = []
    for part in msg.get("content", []) or []:
        if part.get("type") != "tool_use":
            continue
        name = part.get("name") or "tool"
        inp = part.get("input") or {}
        if isinstance(inp, str):
            try:
                inp = json.loads(inp)
            except Exception:
                inp = {"raw": inp}
        desc = (
            inp.get("command")
            or inp.get("path")
            or inp.get("target_directory")
            or inp.get("glob_pattern")
            or inp.get("pattern")
            or inp.get("query")
            or inp.get("search_term")
            or inp.get("url")
            or inp.get("old_string")
            or inp.get("description")
            or ""
        )
        if isinstance(desc, str):
            desc = desc.strip().split("\n")[0][:80]
        else:
            desc = str(desc)[:80]
        actions.append((name, desc))
    return actions


def sanitize_for_speech(text: str, max_chars: int = 180) -> str:
    """
    针对端到端实时语音合成 (Native Audio TTS) 的深度文本纯化器。
    杜绝 Markdown 符号、技术格式、文件长路径、URL 链接、堆栈代码等导致的文字乱念与机械杂音。
    """
    if not text:
        return ""
    import re
    # 1. 过滤 ANSI 转义符
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    # 2. 过滤 HTML/XML 标签与特殊标记 (如 <USER_REQUEST>, <SYSTEM_MESSAGE>)
    text = re.sub(r"<[^>]+>", "", text)
    # 3. 过滤 markdown 代码块 ```...```
    text = re.sub(r"```[\s\S]*?```", "[代码片段]", text)
    # 4. 转换 Markdown 链接 [描述](file:///...) -> 描述
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # 5. 过滤常见 URL 与 file:/// 协议
    text = re.sub(r"(?:https?|file)://\S+", "", text)
    # 6. 转换绝对长路径为纯文件名 (/a/b/c.py -> c.py)
    text = re.sub(r"/(?:[\w\.\-]+/)+([\w\.\-]+)", r"\1", text)
    # 7. 过滤 ISO 时间戳与日期 (如 2026-09-19T01:14:15+08:00)
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}\S*", "", text)
    # 8. 过滤表格与分割线
    text = re.sub(r"\|[^\n]+\|", "", text)
    text = re.sub(r"[-=]{3,}", "", text)
    # 9. 下划线转换为空格（避免将 GEMINI_API_KEY 拼读成乱码词）
    text = re.sub(r"_", " ", text)
    # 10. 过滤 markdown 语法符号与常用编程括号符号
    text = re.sub(r"[#*`~>\[\]{}<>]", "", text)
    # 11. 过滤常见工具前置元数据
    lines = []
    for l in text.split("\n"):
        l_str = l.strip()
        if not l_str:
            continue
        if any(l_str.lower().startswith(prefix) for prefix in ["created at:", "completed at:", "file path:", "total lines:", "total bytes:"]):
            continue
        lines.append(l_str)
    cleaned = " ".join(lines)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_chars:
        cut = cleaned[:max_chars]
        last_punc = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("；"), cut.rfind("，"))
        if last_punc > max_chars // 2:
            cleaned = cut[:last_punc + 1]
        else:
            cleaned = cut + "。"
    return cleaned


def extract_error_snippet(content: str, exit_code: Optional[int] = None) -> str:
    """
    从失败的命令或工具输出中提炼高价值的简明错误信息，并进行口语化纯化。
    """
    lines = [l.strip() for l in content.split("\n") if l.strip()]
    if not lines:
        return f"命令异常退出 (退出码 {exit_code})" if exit_code else "工具执行失败"

    # 忽略正常的用户中止或任务取消
    if any("context canceled by manage_task" in l or "task cancelled" in l.lower() for l in lines):
        return ""

    raw_snippet = ""
    fail_line = ""
    assert_line = ""
    for l in lines:
        if ("FAIL " in l or "Failed Tests" in l) and not fail_line:
            fail_line = l
        if any(kw in l for kw in ["AssertionError", "Error:", "target must exist", "SyntaxError:", "TS2"]) and not assert_line:
            assert_line = l

    if fail_line and assert_line and fail_line != assert_line:
        raw_snippet = f"{fail_line} | {assert_line}"
    elif assert_line:
        raw_snippet = assert_line
    elif fail_line:
        raw_snippet = fail_line
    else:
        for l in lines:
            if "Command failed with exit code" in l:
                raw_snippet = l
                break
        if not raw_snippet:
            raw_snippet = lines[-1]

    return sanitize_for_speech(raw_snippet, max_chars=120)


def _snapshot_from_cursor_transcript(meta: Dict[str, Any]) -> Dict[str, Any]:
    last_user_req = None
    is_working = False
    last_action = None
    last_completion = None
    pending_assistant_text = None

    try:
        with open(meta["transcript_path"], "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    entry = json.loads(line_str)
                except Exception:
                    continue

                if entry.get("type") == "turn_ended":
                    is_working = False
                    if entry.get("status") == "success" and pending_assistant_text:
                        last_completion = pending_assistant_text
                    continue

                role = entry.get("role")
                if role == "user":
                    req = extract_cursor_user_query(entry)
                    if req:
                        last_user_req = req
                        is_working = True
                        last_action = "长官在 Cursor 下达了新需求，实施工程师接单中..."
                        last_completion = None
                        pending_assistant_text = None
                elif role == "assistant":
                    tools = extract_cursor_tool_actions(entry)
                    if tools:
                        is_working = True
                        name, desc = tools[0]
                        last_action = f"{name}: {desc}"[:80]
                    text = extract_cursor_assistant_text(entry)
                    if text and len(text) >= 24:
                        pending_assistant_text = text
    except Exception as err:
        return {
            "conversation_id": meta["conversation_id"],
            "source": "cursor",
            "status": "ERROR_READING_TRANSCRIPT",
            "error": str(err),
            "is_working": False,
        }

    return {
        "conversation_id": meta["conversation_id"],
        "source": "cursor",
        "status": "OK",
        "last_user_request": last_user_req,
        "active_document": None,
        "is_working": is_working,
        "last_action": last_action,
        "last_completion_summary": sanitize_for_speech(last_completion, max_chars=180) if last_completion else None,
        "has_implementation_plan": False,
        "plan_path": None,
    }


def get_ide_chat_snapshot(use_cache: bool = True, workspace_root: Optional[str] = None) -> Dict[str, Any]:
    """
    解析当前 IDE 对话状态的即时快照。
    优先从全局高速缓存获取（0ms）；若无缓存则全量解析。
    数据源优先级：更新更晚的一方（Antigravity vs Cursor）。
    """
    global GLOBAL_CACHED_SNAPSHOT
    if use_cache and GLOBAL_CACHED_SNAPSHOT and GLOBAL_CACHED_SNAPSHOT.get("status") == "OK":
        return GLOBAL_CACHED_SNAPSHOT

    ag_meta = get_active_ide_transcript()
    cu_meta = get_active_cursor_transcript(workspace_root)

    chosen = None
    if ag_meta and cu_meta:
        chosen = ag_meta if ag_meta["last_modified"] >= cu_meta["last_modified"] else cu_meta
    else:
        chosen = ag_meta or cu_meta

    if not chosen:
        return {
            "conversation_id": None,
            "source": None,
            "status": "NO_ACTIVE_CONVERSATION",
            "last_user_request": None,
            "is_working": False,
            "last_action": None,
            "last_completion_summary": None,
            "has_implementation_plan": False,
            "plan_path": None,
        }

    if chosen.get("source") == "cursor":
        snap = _snapshot_from_cursor_transcript(chosen)
        GLOBAL_CACHED_SNAPSHOT = snap
        return snap

    meta = chosen
    transcript_path = meta["transcript_path"]
    brain_dir = meta["brain_dir"]

    last_user_req = None
    active_document = None
    is_working = False
    last_action = None
    last_completion = None

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue
            try:
                entry = json.loads(line_str)
                etype = entry.get("type")

                if etype == "USER_INPUT":
                    content = entry.get("content", "")
                    if "<USER_REQUEST>" in content:
                        parts = content.split("<USER_REQUEST>")[1].split("</USER_REQUEST>")
                        req_cand = parts[0].strip()
                    else:
                        req_cand = content.strip()
                    if req_cand:
                        last_user_req = req_cand

                    if "Active Document:" in content:
                        try:
                            active_document = content.split("Active Document:")[1].split("\n")[0].strip()
                        except Exception:
                            pass

                    is_working = True
                    last_action = "长官提交了新任务，IDE 正在规划与分析..."
                    last_completion = None

                elif etype == "PLANNER_RESPONSE":
                    tool_calls = entry.get("tool_calls", [])
                    if tool_calls:
                        is_working = True
                        call = tool_calls[0]
                        name = call.get("name", "tool")
                        args = call.get("args", {})
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except Exception:
                                args = {}
                        desc = args.get("toolAction") or args.get("CommandLine") or args.get("TargetFile") or ""
                        last_action = f"{name}: {desc}"[:80]
                    else:
                        is_working = False
                        content = entry.get("content", "").strip()
                        if content:
                            last_completion = content

            except Exception:
                continue

    except Exception as err:
        return {
            "conversation_id": meta["conversation_id"],
            "source": "antigravity",
            "status": "ERROR_READING_TRANSCRIPT",
            "error": str(err),
            "is_working": False,
        }

    plan_path = os.path.join(brain_dir, "implementation_plan.md")
    has_plan = os.path.exists(plan_path)

    snap = {
        "conversation_id": meta["conversation_id"],
        "source": "antigravity",
        "status": "OK",
        "last_user_request": last_user_req,
        "active_document": active_document,
        "is_working": is_working,
        "last_action": last_action,
        "last_completion_summary": sanitize_for_speech(last_completion, max_chars=180) if last_completion else None,
        "has_implementation_plan": has_plan,
        "plan_path": plan_path if has_plan else None,
    }
    GLOBAL_CACHED_SNAPSHOT = snap
    return snap


class ActionBatcher:
    """
    智能动作聚合器 (Smart Action Batcher)。
    维护 1.2 秒防抖窗口，将连续细碎的工程动作（查看文件、微调代码、跑测试）
    聚合成一到两句流畅自然的阶段性伴随解说，杜绝碎片化碎念与语音堆叠。
    """
    def __init__(self, debounce_sec: float = 1.2, on_flush: Optional[Callable[[str], Awaitable[None]]] = None):
        self.debounce_sec = debounce_sec
        self.on_flush = on_flush
        self._pending_actions: list[tuple[str, str]] = []
        self._timer_task: Optional[asyncio.Task] = None
        self._last_emitted_text: Optional[str] = None
        self._last_emitted_time: float = 0.0

    def add_action(self, tool_name: str, desc: str):
        self._pending_actions.append((tool_name, desc))
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = asyncio.create_task(self._debounce_timer())

    async def _debounce_timer(self):
        try:
            await asyncio.sleep(self.debounce_sec)
            await self.flush()
        except asyncio.CancelledError:
            pass

    async def flush(self):
        if not self._pending_actions:
            return
        actions = list(self._pending_actions)
        self._pending_actions.clear()

        # 智能语义提炼与合并
        tool_names = [a[0] for a in actions]
        descs = [a[1] for a in actions]

        narration = ""
        has_modify = any(
            t in (
                "replace_file_content",
                "multi_replace_file_content",
                "write_to_file",
                "Write",
                "StrReplace",
                "EditNotebook",
                "Delete",
            )
            for t in tool_names
        )
        has_test = any("test" in d.lower() for d in descs) or any(
            t in ("run_command", "Shell") and "test" in d.lower() for t, d in actions
        )
        has_git = any("git" in d.lower() for d in descs)
        has_view = any(
            t in (
                "view_file",
                "grep_search",
                "list_dir",
                "Read",
                "Grep",
                "Glob",
                "SemanticSearch",
                "WebFetch",
                "WebSearch",
            )
            for t in tool_names
        )
        has_shell = any(t in ("run_command", "Shell", "AwaitShell") for t in tool_names)

        if has_modify and has_test:
            files = [Path(d.split()[0]).name for t, d in actions if ("/" in d or "." in d) and not d.startswith("http")]
            target = f"「{files[0]}」" if files and files[0] else "相关文件"
            narration = f"已改完{target}等文件，正在拉起自动化测试做验证。"
        elif has_modify:
            files = [Path(d.split()[0]).name for t, d in actions if ("/" in d or "." in d) and not d.startswith("http")]
            uniq = []
            for f in files:
                if f and f not in uniq:
                    uniq.append(f)
            if len(uniq) >= 2:
                narration = f"正在连续修改{uniq[0]}、{uniq[1]}等文件并做语法校验。"
            elif uniq:
                narration = f"正在编辑修改{uniq[0]}，并检查改动是否自洽。"
            else:
                narration = "正在编辑核心代码并做语法校验。"
        elif has_test:
            narration = "正在运行目标单元测试与集成测试，稍后汇报通过情况。"
        elif has_git:
            narration = "正在核对当前分支、改动文件与提交状态。"
        elif has_shell:
            cmd = sanitize_for_speech(descs[-1] if descs else "工程命令", max_chars=36)
            narration = f"正在执行终端步骤：{cmd}。"
        elif has_view:
            files = [Path(d.split()[0]).name for t, d in actions if ("/" in d or "." in d) and not d.startswith("http")]
            uniq = []
            for f in files:
                if f and f not in uniq:
                    uniq.append(f)
            count = len(actions)
            if len(uniq) >= 2:
                narration = f"已定位{uniq[0]}与{uniq[1]}等{count}处上下文，继续深入分析。"
            elif uniq:
                narration = f"已定位{uniq[0]}相关逻辑，正在补充上下文分析。"
            else:
                narration = f"正在检索并阅读相关代码上下文，已处理{count}个动作。"
        else:
            latest_desc = sanitize_for_speech(descs[-1] if descs else "工程步骤", max_chars=40)
            narration = f"正在推进施工：{latest_desc}。"

        now = time.time()
        # 4 秒内完全相同的不重复播报；不同进展可更密一些
        if narration and (narration != self._last_emitted_text or (now - self._last_emitted_time > 4.0)):
            self._last_emitted_text = narration
            self._last_emitted_time = now
            if self.on_flush:
                try:
                    await self.on_flush(narration)
                except Exception as e:
                    print(f"[ActionBatcher flush 异常] {e}")

    def cancel(self):
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._pending_actions.clear()


class IDEWatcher:
    """
    异步后台任务监听器。
    毫秒级轮询 transcript.jsonl，捕获 IDE 实施工程师的任务状态跃迁，
    提供：
    1. 实时内存缓存（0ms 响应）
    2. 全程伴随智能防抖解说 (on_narration)
    3. 任务完成主动通报 (on_completed)
    4. 异常与失败主动预警 (on_error)
    5. 细粒度执行动作 (on_action)
    6. 长官新需求感知 (on_user_input)
    """

    def __init__(
        self,
        poll_interval: float = 0.5,
        on_completed: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_user_input: Optional[Callable[[str], Awaitable[None]]] = None,
        on_error: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_action: Optional[Callable[[str], Awaitable[None]]] = None,
        on_narration: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.poll_interval = poll_interval
        self.on_completed = on_completed
        self.on_user_input = on_user_input
        self.on_error = on_error
        self.on_action = on_action
        self.on_narration = on_narration

        self.batcher = ActionBatcher(debounce_sec=1.6, on_flush=self._handle_narration_flush)

        self._last_state_was_working = False
        self._last_user_req: Optional[str] = None
        self._last_action: Optional[str] = None
        self._last_reported_error: Optional[str] = None
        self._last_reported_error_time: float = 0.0
        self._last_seen_step_index: int = 0
        self._file_offset: int = 0
        self._is_running = False

    async def _handle_narration_flush(self, text: str):
        if self.on_narration:
            await self.on_narration(text)

    async def start(self, shutdown_event: asyncio.Event):
        self._is_running = True
        global GLOBAL_CACHED_SNAPSHOT

        # 初始化当前状态
        init_snap = get_ide_chat_snapshot(use_cache=False)
        self._last_state_was_working = init_snap.get("is_working", False)
        self._last_user_req = init_snap.get("last_user_request")
        self._last_action = init_snap.get("last_action")

        # 记录初始最大 step_index 与文件偏移量，避免初启就复读历史完成或全量解析
        meta = get_active_ide_transcript()
        if meta and os.path.exists(meta["transcript_path"]):
            try:
                self._file_offset = os.path.getsize(meta["transcript_path"])
                with open(meta["transcript_path"], "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line_str = line.strip()
                        if line_str:
                            try:
                                d = json.loads(line_str)
                                s_idx = d.get("step_index", 0)
                                if s_idx > self._last_seen_step_index:
                                    self._last_seen_step_index = s_idx
                            except Exception:
                                pass
            except Exception:
                pass

        while not shutdown_event.is_set():
            try:
                meta = get_active_ide_transcript()
                if not meta or not os.path.exists(meta["transcript_path"]):
                    await asyncio.sleep(self.poll_interval)
                    continue

                transcript_path = meta["transcript_path"]
                new_entries = []

                # 增量读取日志：只读自上次位移以来的新增行，杜绝全量开销
                file_size = os.path.getsize(transcript_path)
                if file_size < self._file_offset:
                    self._file_offset = 0

                with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._file_offset)
                    for line in f:
                        line_str = line.strip()
                        if not line_str:
                            continue
                        try:
                            entry = json.loads(line_str)
                            step_idx = entry.get("step_index", 0)
                            if step_idx > self._last_seen_step_index:
                                new_entries.append(entry)
                        except Exception:
                            continue
                    self._file_offset = f.tell()

                # 处理增量事件
                for entry in new_entries:
                    step_idx = entry.get("step_index", 0)
                    if step_idx > self._last_seen_step_index:
                        self._last_seen_step_index = step_idx

                    etype = entry.get("type")
                    status = entry.get("status")
                    exit_code = entry.get("exit_code")
                    content = entry.get("content", "")

                    # 1. 监测长官在 IDE 发送的新需求
                    if etype == "USER_INPUT":
                        req_text = content
                        if "<USER_REQUEST>" in content:
                            req_text = content.split("<USER_REQUEST>")[1].split("</USER_REQUEST>")[0].strip()
                        else:
                            req_text = content.strip()
                        if req_text:
                            self._last_user_req = req_text
                        self._last_state_was_working = True
                        if self.on_user_input and req_text:
                            try:
                                await self.on_user_input(req_text)
                            except Exception as e:
                                print(f"[IDE Watcher on_user_input 异常] {e}")

                    # 2. 监测动作流转（Action Started）
                    elif etype == "PLANNER_RESPONSE":
                        tool_calls = entry.get("tool_calls", [])
                        if tool_calls:
                            self._last_state_was_working = True
                            call = tool_calls[0]
                            name = call.get("name", "tool")
                            args = call.get("args", {})
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except Exception:
                                    args = {}
                            desc = args.get("toolAction") or args.get("CommandLine") or args.get("TargetFile") or ""
                            action_str = f"{name}: {desc}"[:80]
                            if action_str != self._last_action:
                                self._last_action = action_str
                                # 投递至智能聚合器，触发 1.2 秒防抖伴随解说
                                self.batcher.add_action(name, desc)
                                if self.on_action:
                                    try:
                                        await self.on_action(action_str)
                                    except Exception as e:
                                        print(f"[IDE Watcher on_action 异常] {e}")
                        else:
                            # 施工完毕，立刻截断并取消中间细碎解说，让位给最终战报
                            self.batcher.cancel()
                            self._last_state_was_working = False
                            resp_content = content.strip()
                            if self.on_completed and resp_content:
                                try:
                                    await self.on_completed(self._last_user_req or "开发任务", resp_content)
                                except Exception as e:
                                    print(f"[IDE Watcher on_completed 异常] {e}")

                    # 3. 监测异常与失败（Error / Assertion Failed / Exit Code != 0）
                    is_failure = (exit_code is not None and exit_code != 0) or (status == "ERROR")
                    if is_failure and self.on_error:
                        self.batcher.cancel()
                        err_snippet = extract_error_snippet(content, exit_code)
                        now = time.time()
                        # 避免 5 秒内对同一错误重复告警
                        if err_snippet and (err_snippet != self._last_reported_error or (now - self._last_reported_error_time > 5.0)):
                            self._last_reported_error = err_snippet
                            self._last_reported_error_time = now
                            action_label = self._last_action or f"步骤 {step_idx}"
                            try:
                                await self.on_error(action_label, err_snippet)
                            except Exception as e:
                                print(f"[IDE Watcher on_error 异常] {e}")

                # 刷新内存快照
                GLOBAL_CACHED_SNAPSHOT = {
                    "conversation_id": meta["conversation_id"],
                    "source": "antigravity",
                    "status": "OK",
                    "last_user_request": self._last_user_req,
                    "is_working": self._last_state_was_working,
                    "last_action": self._last_action,
                    "last_completion_summary": None,
                    "has_implementation_plan": os.path.exists(os.path.join(meta["brain_dir"], "implementation_plan.md")),
                    "plan_path": os.path.join(meta["brain_dir"], "implementation_plan.md") if os.path.exists(os.path.join(meta["brain_dir"], "implementation_plan.md")) else None,
                }

            except (asyncio.CancelledError, KeyboardInterrupt):
                break
            except Exception as loop_err:
                pass

            try:
                await asyncio.sleep(self.poll_interval)
            except (asyncio.CancelledError, KeyboardInterrupt):
                break


class CursorTranscriptWatcher:
    """
    监听 Cursor agent-transcripts/*.jsonl：
    - user_query → on_user_input
    - tool_use → 防抖伴随解说 on_narration / on_action
    - turn_ended success → on_completed（附最终助手摘要）
    - turn_ended error/failed → on_error
    """

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        poll_interval: float = 0.5,
        on_completed: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_user_input: Optional[Callable[[str], Awaitable[None]]] = None,
        on_error: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_action: Optional[Callable[[str], Awaitable[None]]] = None,
        on_narration: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.workspace_root = workspace_root
        self.poll_interval = poll_interval
        self.on_completed = on_completed
        self.on_user_input = on_user_input
        self.on_error = on_error
        self.on_action = on_action
        self.on_narration = on_narration

        self.batcher = ActionBatcher(debounce_sec=1.6, on_flush=self._handle_narration_flush)
        self._file_offset = 0
        self._active_path: Optional[str] = None
        self._last_user_req: Optional[str] = None
        self._last_action: Optional[str] = None
        self._pending_summary: Optional[str] = None
        self._last_reported_error: Optional[str] = None
        self._last_reported_error_time: float = 0.0
        self._bootstrap_done = False

    async def _handle_narration_flush(self, text: str):
        if self.on_narration:
            await self.on_narration(text)

    def _reset_to_path(self, transcript_path: str):
        self._active_path = transcript_path
        try:
            self._file_offset = os.path.getsize(transcript_path)
        except OSError:
            self._file_offset = 0
        self._pending_summary = None
        self.batcher.cancel()

    async def _handle_entry(self, entry: Dict[str, Any]):
        global GLOBAL_CACHED_SNAPSHOT

        if entry.get("type") == "turn_ended":
            status = (entry.get("status") or "").lower()
            self.batcher.cancel()
            summary = self._pending_summary or "任务已完成，详见 Cursor 对话区结论。"
            if status in ("error", "failed", "failure", "cancelled", "canceled"):
                if self.on_error:
                    err = entry.get("error") or entry.get("message") or f"Cursor 回合结束状态: {status}"
                    now = time.time()
                    snippet = sanitize_for_speech(str(err), max_chars=120)
                    if snippet and (snippet != self._last_reported_error or (now - self._last_reported_error_time > 5.0)):
                        self._last_reported_error = snippet
                        self._last_reported_error_time = now
                        try:
                            await self.on_error(self._last_action or "Cursor 任务", snippet)
                        except Exception as e:
                            print(f"[Cursor Watcher on_error 异常] {e}")
            elif self.on_completed:
                task = self._last_user_req or "Cursor 开发任务"
                try:
                    await self.on_completed(task, summary)
                except Exception as e:
                    print(f"[Cursor Watcher on_completed 异常] {e}")
            self._pending_summary = None
            GLOBAL_CACHED_SNAPSHOT = {
                "conversation_id": Path(self._active_path).stem if self._active_path else None,
                "source": "cursor",
                "status": "OK",
                "last_user_request": self._last_user_req,
                "is_working": False,
                "last_action": self._last_action,
                "last_completion_summary": sanitize_for_speech(summary, max_chars=420) if status == "success" else None,
                "has_implementation_plan": False,
                "plan_path": None,
            }
            return

        role = entry.get("role")
        if role == "user":
            req = extract_cursor_user_query(entry)
            if not req:
                return
            self._last_user_req = req
            self._pending_summary = None
            if self.on_user_input:
                try:
                    await self.on_user_input(req)
                except Exception as e:
                    print(f"[Cursor Watcher on_user_input 异常] {e}")
            GLOBAL_CACHED_SNAPSHOT = {
                "conversation_id": Path(self._active_path).stem if self._active_path else None,
                "source": "cursor",
                "status": "OK",
                "last_user_request": self._last_user_req,
                "is_working": True,
                "last_action": "长官在 Cursor 下达了新需求",
                "last_completion_summary": None,
                "has_implementation_plan": False,
                "plan_path": None,
            }
            return

        if role == "assistant":
            for name, desc in extract_cursor_tool_actions(entry):
                action_str = f"{name}: {desc}"[:80]
                if action_str != self._last_action:
                    self._last_action = action_str
                    self.batcher.add_action(name, desc)
                    if self.on_action:
                        try:
                            await self.on_action(action_str)
                        except Exception as e:
                            print(f"[Cursor Watcher on_action 异常] {e}")

            text = extract_cursor_assistant_text(entry)
            if text and len(text) >= 24:
                # 保留最新较长回复作为完成摘要候选；跳过纯过程短句
                self._pending_summary = text
            GLOBAL_CACHED_SNAPSHOT = {
                "conversation_id": Path(self._active_path).stem if self._active_path else None,
                "source": "cursor",
                "status": "OK",
                "last_user_request": self._last_user_req,
                "is_working": True,
                "last_action": self._last_action,
                "last_completion_summary": None,
                "has_implementation_plan": False,
                "plan_path": None,
            }

    async def start(self, shutdown_event: asyncio.Event):
        sys_stdout_boot = False
        while not shutdown_event.is_set():
            try:
                meta = get_active_cursor_transcript(self.workspace_root)
                if not meta or not os.path.exists(meta["transcript_path"]):
                    await asyncio.sleep(self.poll_interval)
                    continue

                transcript_path = meta["transcript_path"]
                if transcript_path != self._active_path:
                    self._reset_to_path(transcript_path)
                    if not sys_stdout_boot:
                        print(
                            f"\033[1;36m[Cursor 对话监听] 已接入: {transcript_path}\033[0m",
                            flush=True,
                        )
                        sys_stdout_boot = True
                    self._bootstrap_done = True

                file_size = os.path.getsize(transcript_path)
                if file_size < self._file_offset:
                    self._file_offset = 0

                new_entries: List[Dict[str, Any]] = []
                with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._file_offset)
                    for line in f:
                        line_str = line.strip()
                        if not line_str:
                            continue
                        try:
                            new_entries.append(json.loads(line_str))
                        except Exception:
                            continue
                    self._file_offset = f.tell()

                for entry in new_entries:
                    await self._handle_entry(entry)

            except (asyncio.CancelledError, KeyboardInterrupt):
                break
            except Exception:
                pass

            try:
                await asyncio.sleep(self.poll_interval)
            except (asyncio.CancelledError, KeyboardInterrupt):
                break
