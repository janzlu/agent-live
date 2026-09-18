"""
Text-based Integration Test for Antigravity + MCP Runner.
Use this script to verify that Antigravity Agent and MCP tools are functional
before enabling microphone and speaker voice streams.
"""

import os
import sys
import asyncio
from pathlib import Path
from dotenv import load_dotenv

# 加载当前目录的 .env
load_dotenv(Path(__file__).parent / ".env")

from antigravity_runner import AntigravityRunner


async def main():
    print("==================================================")
    print(" Antigravity + MCP 离线集成连通性测试")
    print("==================================================")

    # 默认工作区为当前项目的根目录
    workspace_root = str(Path(__file__).parent.parent.parent.resolve())
    print(f"工作区根目录: {workspace_root}")

    # 支持从命令行指定任务，若无则使用默认探测用例
    if len(sys.argv) > 1:
        test_prompt = " ".join(sys.argv[1:])
    else:
        test_prompt = (
            "你好！请执行以下两项检查并汇总告诉我：\n"
            "1. 当前代码库的 git 分支和最新的一条提交是什么？\n"
            "2. 你当前被挂载了哪些外部能力或 MCP 服务（如 Stitch 等）？"
        )

    runner = AntigravityRunner(workspace_path=workspace_root)
    await runner.start()

    try:
        print(f"\n[测试输入] >>> {test_prompt}\n")
        report = await runner.run_task(test_prompt)
        print("\n" + "="*50)
        print("✓ 测试执行成功！Antigravity 与 MCP 桥接就绪。")
        print("="*50)
    except Exception as e:
        print(f"\n[测试失败] 运行出现异常: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
    finally:
        await runner.close()


if __name__ == "__main__":
    asyncio.run(main())
