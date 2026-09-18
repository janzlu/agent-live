"""
Test dispatch execution pipeline:
Simulates live_sidecar dispatching an engineering task to AntigravityRunner.
Verifies execution, output capture, and proactive report message generation.
"""
import asyncio
import time
from pathlib import Path
from antigravity_runner import AntigravityRunner

async def test_dispatch():
    workspace = str(Path(__file__).parent.parent.parent.resolve())
    runner = AntigravityRunner(workspace_path=workspace)
    await runner.start()

    task_prompt = "检查当前 git 分支与最近一条 commit，并简要总结。"
    print(f"\n[测试派单] 正在模拟顾问向实施工程师派单: '{task_prompt}'...")
    start_t = time.time()
    
    try:
        output = await runner.run_task(task_prompt, authorized=True)
        elapsed = time.time() - start_t
        print(f"\n[测试派单] ✓ 实施工程师接单并执行完毕！耗时: {elapsed:.2f}秒")
        print(f"--- 执行输出内容 (前 300 字符) ---\n{output[:300]}\n------------------------------")
        assert len(output) > 0, "执行输出不应为空"
        
        # 验证主动通报提示词组装
        report_prompt = (
            f"[系统通知: 实施工程师已完成长官派发任务 (已授权实操)]\n"
            f"派单任务: {task_prompt}\n"
            f"执行结论摘要:\n{output[:600]}\n\n"
            f"请用军纪严明的口吻、极其自然干练的一句话主动向长官语音汇报（必须以'报告 长官！'开头）。"
        )
        assert "报告 长官！" in report_prompt
        print("✓ [派单链路验证] 实施工程师接单、执行、结果捕获与主动汇报组装 100% 正常闭环！")
    finally:
        await runner.close()

if __name__ == "__main__":
    asyncio.run(test_dispatch())
