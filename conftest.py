import os
import sys

# 让测试能直接 import 仓库根的 main.py（本仓库不是包结构）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
