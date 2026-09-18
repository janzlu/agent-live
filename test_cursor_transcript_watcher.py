"""Cursor transcript listener unit tests (no Live API / mic required)."""
import asyncio
import json
import tempfile
from pathlib import Path

from ide_watcher import (
    workspace_to_cursor_project_slug,
    extract_cursor_user_query,
    extract_cursor_assistant_text,
    extract_cursor_tool_actions,
    get_active_cursor_transcript,
    get_ide_chat_snapshot,
    CursorTranscriptWatcher,
    GLOBAL_CACHED_SNAPSHOT,
)


def test_workspace_slug():
    assert workspace_to_cursor_project_slug("/Users/mon3xey/projects/xhmould") == "Users-mon3xey-projects-xhmould"
    print("✓ workspace slug mapping")


def test_parse_real_xhmould_transcript():
    meta = get_active_cursor_transcript("/Users/mon3xey/projects/xhmould")
    assert meta is not None, "应能定位 xhmould 的 Cursor transcript"
    assert meta["source"] == "cursor"
    assert Path(meta["transcript_path"]).exists()

    # clear cache and snapshot
    GLOBAL_CACHED_SNAPSHOT.clear()
    snap = get_ide_chat_snapshot(use_cache=False, workspace_root="/Users/mon3xey/projects/xhmould")
    assert snap["status"] == "OK"
    assert snap.get("source") in ("cursor", "antigravity")
    assert snap.get("last_user_request")
    print(f"✓ real transcript snap source={snap.get('source')} req={snap.get('last_user_request')[:40]!r}")


def test_extractors_on_fixture_entry():
    user_entry = {
        "role": "user",
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": "<timestamp>t</timestamp>\n<user_query>\n分析项目\n</user_query>",
                }
            ]
        },
    }
    assert extract_cursor_user_query(user_entry) == "分析项目"

    asst_entry = {
        "role": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "正在扫描仓库结构。"},
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"path": "/Users/mon3xey/projects/xhmould/package.json"},
                },
            ]
        },
    }
    assert "扫描" in extract_cursor_assistant_text(asst_entry)
    tools = extract_cursor_tool_actions(asst_entry)
    assert tools == [("Read", "/Users/mon3xey/projects/xhmould/package.json")]
    print("✓ extractors")


async def test_watcher_emits_process_and_completion():
    events = {"user": [], "action": [], "narration": [], "completed": [], "error": []}

    async def on_user(req):
        events["user"].append(req)

    async def on_action(a):
        events["action"].append(a)

    async def on_narration(n):
        events["narration"].append(n)

    async def on_completed(task, summary):
        events["completed"].append((task, summary))

    async def on_error(action, err):
        events["error"].append((action, err))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Fake Cursor projects layout
        slug = "Users-fake-demo"
        tdir = root / ".cursor" / "projects" / slug / "agent-transcripts" / "conv-1"
        tdir.mkdir(parents=True)
        tpath = tdir / "conv-1.jsonl"
        tpath.write_text("")

        # Monkeypatch home via env is hard; instead point watcher by writing real path
        # and temporarily patch get_active_cursor_transcript through workspace that won't match.
        # So we drive watcher by setting _active_path directly after constructing.
        watcher = CursorTranscriptWatcher(
            workspace_root="/Users/fake/demo",
            poll_interval=0.05,
            on_completed=on_completed,
            on_user_input=on_user,
            on_error=on_error,
            on_action=on_action,
            on_narration=on_narration,
        )

        # Feed entries through _handle_entry (unit path without filesystem discovery)
        await watcher._handle_entry(
            {
                "role": "user",
                "message": {"content": [{"type": "text", "text": "<user_query>分析项目</user_query>"}]},
            }
        )
        await watcher._handle_entry(
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "先摸清仓库结构。"},
                        {"type": "tool_use", "name": "Read", "input": {"path": "/tmp/package.json"}},
                    ]
                },
            }
        )
        await watcher.batcher.flush()
        await watcher._handle_entry(
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "**XH Mould** 是 Next.js 与 Payload 同仓的企业官网，三语言获客与 RFQ 转化完整。",
                        }
                    ]
                },
            }
        )
        await watcher._handle_entry({"type": "turn_ended", "status": "success"})

    assert events["user"] == ["分析项目"]
    assert any(a.startswith("Read:") for a in events["action"])
    assert events["narration"], "应产生过程伴随解说"
    assert events["completed"], "应产生完成摘要口播事件"
    assert "Next.js" in events["completed"][0][1] or "Payload" in events["completed"][0][1]
    print("✓ watcher process + completion events")


if __name__ == "__main__":
    test_workspace_slug()
    test_extractors_on_fixture_entry()
    test_parse_real_xhmould_transcript()
    asyncio.run(test_watcher_emits_process_and_completion())
    print("\n★ Cursor transcript 监听测试全部通过")
