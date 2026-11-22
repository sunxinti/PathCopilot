#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PathCopilot 启动脚本
提供更好的启动体验和配置管理
"""

import os
import sys
import argparse
from pathlib import Path

def check_dependencies():
    """检查依赖包"""
    try:
        import flask
        import openslide
        import PIL
        import numpy
        import requests
        print("✅ 所有依赖包已安装")
        return True
    except ImportError as e:
        print(f"❌ 缺少依赖包: {e}")
        print("请运行: pip install -r requirements.txt")
        return False

def check_openslide():
    """检查OpenSlide系统库"""
    try:
        import openslide
        # 检查版本信息
        version = getattr(openslide, '__version__', 'unknown')
        print(f"✅ OpenSlide 库正常 (版本: {version})")
        
        # 尝试访问基本功能
        openslide.OpenSlide
        return True
    except ImportError as e:
        print(f"❌ OpenSlide Python包未安装: {e}")
        print("请运行: pip install openslide-python")
        return False
    except Exception as e:
        print(f"❌ OpenSlide 库问题: {e}")
        print("请安装系统级OpenSlide库:")
        print("  Ubuntu/Debian: sudo apt-get install openslide-tools")
        print("  CentOS/RHEL: sudo yum install openslide")
        print("  macOS: brew install openslide")
        print("  Windows: 下载 OpenSlide Windows binaries")
        return False

def create_directories():
    """创建必要的目录"""
    dirs = ['uploads', 'dzi_files', 'thumbnails']
    for dir_name in dirs:
        Path(dir_name).mkdir(exist_ok=True)
        print(f"📁 目录已创建: {dir_name}")

def check_api_key():
    """检查环境变量或本地忽略文件中的 API 密钥。"""
    from agent.credential_loader import load_api_keys

    keys, source = load_api_keys()
    if keys:
        print(f"✅ 已从 {source} 加载 {len(keys)} 个 API 密钥")
        return True
    print("⚠️  尚未配置 API 密钥")
    print("   可设置 OPENROUTER_API_KEY，或复制")
    print("   config/api_keys.example.txt 为 config/api_keys.txt 后填写。")
    print("   也可以启动后在设置界面中配置。")
    return False

def main():
    parser = argparse.ArgumentParser(description='PathCopilot 病理切片智能分析系统')
    parser.add_argument('--port', type=int, default=5000, help='端口号 (默认: 5000)')
    parser.add_argument('--host', default='0.0.0.0', help='主机地址 (默认: 0.0.0.0)')
    parser.add_argument('--debug', action='store_true', help='启用调试模式')
    parser.add_argument('--skip-checks', action='store_true', help='跳过环境检查')
    
    args = parser.parse_args()
    
    print("🔬 PathCopilot - 病理切片智能分析系统")
    print("=" * 50)
    
    if not args.skip_checks:
        print("🔍 环境检查中...")
        
        # 检查依赖
        if not check_dependencies():
            sys.exit(1)
        
        # 检查OpenSlide
        if not check_openslide():
            sys.exit(1)
        
        # 创建目录
        create_directories()
        
        # 检查API密钥
        api_configured = check_api_key()
        if not api_configured:
            print("\n继续启动服务器，但GPT分析功能将不可用...")
            print("您可以在设置界面中添加 API 地址、模型和密钥，无需修改源码。")
        
        print("✅ 环境检查完成")
    
    print("=" * 50)
    print("🚀 启动服务器...")
    print(f"📍 地址: http://{args.host}:{args.port}")
    print(f"🔧 调试模式: {'开启' if args.debug else '关闭'}")
    print("=" * 50)
    print("功能特性:")
    print("📁 WSI文件上传和管理")
    print("🔍 OpenSeadragon专业查看器")
    print("🎯 ROI选择和标注")
    print("🧠 GPT多模态病理分析")
    print("💬 智能对话界面")
    print("=" * 50)
    print("💡 使用提示:")
    print("• 支持 .svs, .ndpi, .tiff 等WSI格式")
    print("• 最大文件大小: 2GB")
    print("• 鼠标滚轮缩放，拖拽平移")
    print("• 左键拖拽选择ROI区域")
    print("• Ctrl+C 停止服务器")
    print("=" * 50)
    
    # 导入并启动应用
    try:
        from app import app
        app.run(
            debug=args.debug,
            host=args.host,
            port=args.port,
            threaded=True
        )
    except KeyboardInterrupt:
        print("\n👋 服务器已停止")
    except Exception as e:
        print(f"❌ 启动失败: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
