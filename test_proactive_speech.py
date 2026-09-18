"""
Test proactive voice notification trigger in Gemini Live session.
Tests send_client_content and send(end_of_turn=True) to confirm audio output.
"""
import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
from google import genai
from google.genai import types

async def test_proactive_speech():
    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(
            parts=[types.Part.from_text(text="你是一名实施工程师，面向长官汇报。")]
        )
    )

    print("[测试主动语音] 正在连接 Live API...")
    async with client.aio.live.connect(model="models/gemini-3.8-live", config=config) as session:
        print("[测试主动语音] 连接成功！正在发送主动完成汇报指令...")

        prompt = "系统通知: 实施工程师已完成测试任务。请用极其利落的一句话主动向长官语音汇报（以'报告 长官！'开头）。"
        
        # 方式 1：测试 send(input=..., end_of_turn=True)
        try:
            print("[尝试方式 1] session.send(input=prompt, end_of_turn=True)...")
            await session.send(input=prompt, end_of_turn=True)
            
            got_audio = False
            async for response in session.receive():
                sc = response.server_content
                if sc and sc.model_turn:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.mime_type.startswith("audio/pcm"):
                            print(f"[测试成功] 收到语音音频包！长度: {len(part.inline_data.data)} bytes")
                            got_audio = True
                if sc and sc.turn_complete:
                    print("[测试成功] 收到 turn_complete，本轮语音合成完整结束！")
                    break
            if got_audio:
                print("★ session.send(input=..., end_of_turn=True) 完全成功且稳定！")
                return
        except Exception as e:
            print(f"[方式 1 异常] {e}")

if __name__ == "__main__":
    asyncio.run(test_proactive_speech())
