# -*- coding: utf-8 -*-
"""
PathCopilot 本地桌面模式补丁
在 app.py 启动前导入此模块即可自动跳过登录验证
"""
import os
import sys

# 设置本地模式标志
os.environ['PATHCOPILOT_LOCAL_MODE'] = '1'
