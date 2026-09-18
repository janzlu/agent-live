"""
Test script for Noise Gate RMS calculation and Global Hotkey listener.
"""
import numpy as np
import time

class NoiseGate:
    def __init__(self, threshold: float = 280.0, hangover_ms: int = 350, frame_duration_ms: int = 20):
        self.threshold = threshold
        self.hangover_frames = int(hangover_ms / frame_duration_ms)
        self.active_frames_remaining = 0
        self.is_gate_open = False

    def process(self, indata: np.ndarray) -> bool:
        """
        Processes an audio chunk (int16 numpy array).
        Returns True if gate is open (voice detected or in hangover), False if silent/noise.
        """
        # 计算当前帧 RMS 振幅 (0 ~ 32767)
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        
        if rms >= self.threshold:
            self.active_frames_remaining = self.hangover_frames
            self.is_gate_open = True
            return True
        elif self.active_frames_remaining > 0:
            self.active_frames_remaining -= 1
            self.is_gate_open = True
            return True
        else:
            self.is_gate_open = False
            return False

# 测试用例
def test_noise_gate():
    gate = NoiseGate(threshold=300.0, hangover_ms=60, frame_duration_ms=20)
    
    # 模拟底噪 (振幅 < 100)
    noise_frame = (np.random.randn(320) * 50).astype(np.int16)
    assert not gate.process(noise_frame), "底噪应被门限静音拦截"
    
    # 模拟说话 (振幅 > 500)
    speech_frame = (np.random.randn(320) * 800).astype(np.int16)
    assert gate.process(speech_frame), "语音应开启门限"
    
    # 模拟说话停顿第一帧 (在 hangover 范围内，应保持开启)
    assert gate.process(noise_frame), "停顿第一帧在 hangover 缓冲期内应保持放行"
    assert gate.process(noise_frame), "停顿第二帧在 hangover 缓冲期内应保持放行"
    assert gate.process(noise_frame), "停顿第三帧在 hangover 缓冲期内应保持放行"
    
    # 超过 hangover 范围后应静音关闭
    assert not gate.process(noise_frame), "超出 hangover 缓冲期后应平滑闭门静音"
    print("✓ NoiseGate 降噪门限算法单元测试 100% 通过！")

if __name__ == "__main__":
    test_noise_gate()
