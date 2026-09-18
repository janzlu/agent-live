"""
Test script for Global Hotkey hook with graceful fallback.
"""
import sys
import threading

def test_global_hotkey():
    try:
        from pynput import keyboard
        print("✓ pynput.keyboard 导入成功")

        def on_activate():
            print("★ [全局热键激活] Ctrl+Space 或 Cmd+Shift+Space 被触发！")

        hotkey_map = {
            '<ctrl>+<space>': on_activate,
        }

        # 测试实例化 GlobalHotKeys
        listener = keyboard.GlobalHotKeys(hotkey_map)
        print("✓ GlobalHotKeys 实例化成功！热键绑定: <ctrl>+<space>")
        # 不实际 start 阻塞，仅验证构造
    except Exception as e:
        print(f"[警告] pynput 初始化异常 (macOS 权限或其他原因): {e}")

if __name__ == "__main__":
    test_global_hotkey()
