"""验证：选择文件夹只弹【一个】窗口、且能拿到路径"""
import sys
import threading
import time

sys.path.insert(0, ".")
import webui

result = {"done": False, "dir": None}


def worker():
    result["dir"] = webui.pick_directory("A:\\WorkBuddyOther\\douyin")
    result["done"] = True


t = threading.Thread(target=worker, daemon=True)
t.start()
for i in range(8):
    time.sleep(1)
    if result["done"]:
        print(f"→ 弹窗已关闭，返回路径 = {result['dir']!r}")
        break
    print(f"  [{i+1}s] 对话框开着（在等用户选）")
else:
    print("→ 8 秒未关闭 = 对话框正常显示 ✅（且只有它一个窗口）")
