# -*- coding: utf-8 -*-
"""
LLM Logger (Tee) - 记录所有 LLM 交互到日志文件
支持纯文本和 JSON 两种格式
"""
import os
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
import threading

logger = logging.getLogger(__name__)

class LLMLogger:
    """
    LLM 交互日志记录器
    
    功能：
    - 记录所有 LLM API 调用（输入 prompt + 输出 response）
    - 记录用户聊天消息和 AI 回复
    - 支持纯文本和 JSON 两种格式
    - 按 WSI/Patient 分文件存储
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    instance._sessions = {}
                    instance._session_lock = threading.Lock()
                    cls._instance = instance
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self.logs_dir = Path("logs")
        self.logs_dir.mkdir(exist_ok=True)
        self._initialized = True

        logger.info(f"✅ LLMLogger 初始化完成，日志目录: {self.logs_dir.absolute()}")
    
    def start_session(self, session_id: str, metadata: Optional[Dict] = None) -> str:
        """
        开始一个新的日志会话
        
        Args:
            session_id: 会话ID (通常是 file_id 或 patient_id)
            metadata: 额外的元数据 (如 slide_id)
        
        Returns:
            session_id
        """
        with self._session_lock:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # 创建日志文件名
            safe_id = self._sanitize_filename(session_id)
            log_name = f"{timestamp}_{safe_id}"
            
            # 创建会话目录
            session_dir = self.logs_dir / log_name
            session_dir.mkdir(exist_ok=True)
            
            # 初始化会话数据
            session_data = {
                "session_id": session_id,
                "start_time": datetime.now().isoformat(),
                "metadata": metadata or {},
                "log_dir": session_dir,
                "txt_file": session_dir / "interaction.txt",
                "json_file": session_dir / "interaction.json",
                "entries": []
            }
            
            self._sessions[session_id] = session_data
            
            # 写入会话开始信息
            self._write_header(session_data)
            
            logger.info(f"📝 开始日志会话: {session_id} -> {session_dir}")
            return session_id
    
    def _sanitize_filename(self, name: str) -> str:
        """清理文件名，移除非法字符"""
        illegal_chars = '<>:"/\\|?*'
        for char in illegal_chars:
            name = name.replace(char, '_')
        return name[:50]  # 限制长度
    
    def _write_header(self, session_data: Dict):
        """写入日志文件头部"""
        txt_file = session_data["txt_file"]
        json_file = session_data["json_file"]
        
        # 纯文本格式头部
        header = f"""
{'='*80}
PathCopilot LLM 交互日志
{'='*80}
会话ID: {session_data['session_id']}
开始时间: {session_data['start_time']}
元数据: {json.dumps(session_data['metadata'], ensure_ascii=False, indent=2)}
{'='*80}

"""
        with open(txt_file, 'w', encoding='utf-8') as f:
            f.write(header)
        
        # JSON 格式初始化
        json_header = {
            "session_id": session_data['session_id'],
            "start_time": session_data['start_time'],
            "metadata": session_data['metadata'],
            "entries": []
        }
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(json_header, f, ensure_ascii=False, indent=2)
    
    def log_llm_call(
        self,
        session_id: str,
        call_type: str,
        model: str,
        prompt: str,
        response: str,
        metadata: Optional[Dict] = None,
        images_count: int = 0,
        tokens_used: Optional[Dict] = None,
        duration_ms: Optional[float] = None,
        error: Optional[str] = None
    ):
        """
        记录一次 LLM API 调用
        
        Args:
            session_id: 会话ID
            call_type: 调用类型 (如 "vision", "chat", "plan", "reason" 等)
            model: 模型名称
            prompt: 输入提示词 (系统提示 + 用户消息)
            response: LLM 回复内容
            metadata: 额外元数据 (如 roi_id, zoom_level 等)
            images_count: 图片数量
            tokens_used: Token 使用情况 {"prompt": x, "completion": y, "total": z}
            duration_ms: 调用耗时 (毫秒)
            error: 错误信息 (如果有)
        """
        if session_id not in self._sessions:
            # 自动创建会话
            self.start_session(session_id)
        
        session_data = self._sessions[session_id]
        timestamp = datetime.now()
        
        entry = {
            "timestamp": timestamp.isoformat(),
            "type": "llm_call",
            "call_type": call_type,
            "model": model,
            "prompt_length": len(prompt),
            "prompt_preview": prompt[:500] + "..." if len(prompt) > 500 else prompt,
            "prompt_full": prompt,
            "response_length": len(response) if response else 0,
            "response": response,
            "images_count": images_count,
            "tokens_used": tokens_used,
            "duration_ms": duration_ms,
            "error": error,
            "metadata": metadata or {}
        }
        
        session_data["entries"].append(entry)
        
        # 写入纯文本
        self._write_txt_entry(session_data, entry, timestamp)
        
        # 更新 JSON 文件
        self._update_json_file(session_data)
        
        # 控制台输出摘要
        status = "❌ 失败" if error else "✅ 成功"
        logger.debug(f"[LLMLog] {call_type} | {model} | {status} | {len(response) if response else 0}字符")
    
    def log_user_chat(
        self,
        session_id: str,
        user_message: str,
        ai_response: str,
        context_length: int = 0,
        metadata: Optional[Dict] = None
    ):
        """
        记录用户聊天交互
        
        Args:
            session_id: 会话ID
            user_message: 用户消息
            ai_response: AI 回复
            context_length: 上下文长度 (分析结果字符数)
            metadata: 额外元数据
        """
        if session_id not in self._sessions:
            self.start_session(session_id)
        
        session_data = self._sessions[session_id]
        timestamp = datetime.now()
        
        entry = {
            "timestamp": timestamp.isoformat(),
            "type": "user_chat",
            "user_message": user_message,
            "ai_response": ai_response,
            "context_length": context_length,
            "metadata": metadata or {}
        }
        
        session_data["entries"].append(entry)
        
        # 写入纯文本
        self._write_chat_txt_entry(session_data, entry, timestamp)
        
        # 更新 JSON 文件
        self._update_json_file(session_data)
        
        logger.debug(f"[LLMLog] 聊天 | 用户: {user_message[:30]}... | AI: {ai_response[:30]}...")
    
    def _write_txt_entry(self, session_data: Dict, entry: Dict, timestamp: datetime):
        """写入纯文本格式的 LLM 调用条目"""
        txt_file = session_data["txt_file"]
        
        time_str = timestamp.strftime("%H:%M:%S")
        
        content = f"""
[{time_str}] === LLM 调用: {entry['call_type']} ===
模型: {entry['model']}
图片数: {entry['images_count']}
元数据: {json.dumps(entry['metadata'], ensure_ascii=False)}
{'-'*40}
【输入提示词】({entry['prompt_length']} 字符)
{entry['prompt_full']}
{'-'*40}
【LLM 回复】({entry['response_length']} 字符)
{entry['response'] or '(无回复)'}
{'-'*40}
Token使用: {entry['tokens_used']}
耗时: {entry['duration_ms']}ms
错误: {entry['error'] or '无'}
{'='*80}

"""
        with open(txt_file, 'a', encoding='utf-8') as f:
            f.write(content)
    
    def _write_chat_txt_entry(self, session_data: Dict, entry: Dict, timestamp: datetime):
        """写入纯文本格式的聊天条目"""
        txt_file = session_data["txt_file"]
        
        time_str = timestamp.strftime("%H:%M:%S")
        
        content = f"""
[{time_str}] === 用户聊天 ===
上下文长度: {entry['context_length']} 字符
元数据: {json.dumps(entry['metadata'], ensure_ascii=False)}
{'-'*40}
【用户】
{entry['user_message']}
{'-'*40}
【AI 回复】
{entry['ai_response']}
{'='*80}

"""
        with open(txt_file, 'a', encoding='utf-8') as f:
            f.write(content)
    
    def _update_json_file(self, session_data: Dict):
        """更新 JSON 日志文件"""
        json_file = session_data["json_file"]
        
        json_data = {
            "session_id": session_data['session_id'],
            "start_time": session_data['start_time'],
            "metadata": session_data['metadata'],
            "entries": session_data['entries'],
            "last_updated": datetime.now().isoformat()
        }
        
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)
    
    def end_session(self, session_id: str, summary: Optional[Dict] = None):
        """
        结束日志会话
        
        Args:
            session_id: 会话ID
            summary: 会话总结信息
        """
        if session_id not in self._sessions:
            return
        
        session_data = self._sessions[session_id]
        end_time = datetime.now()
        
        # 写入会话结束信息
        footer = f"""
{'='*80}
会话结束
{'='*80}
结束时间: {end_time.isoformat()}
总条目数: {len(session_data['entries'])}
LLM调用数: {sum(1 for e in session_data['entries'] if e['type'] == 'llm_call')}
聊天交互数: {sum(1 for e in session_data['entries'] if e['type'] == 'user_chat')}
总结: {json.dumps(summary or {}, ensure_ascii=False)}
{'='*80}
"""
        with open(session_data["txt_file"], 'a', encoding='utf-8') as f:
            f.write(footer)
        
        # 更新 JSON
        session_data["end_time"] = end_time.isoformat()
        session_data["summary"] = summary
        self._update_json_file(session_data)
        
        logger.info(f"📝 日志会话结束: {session_id} | {len(session_data['entries'])} 条记录")
        
        # 清理会话
        with self._session_lock:
            del self._sessions[session_id]
    
    def get_session_log_dir(self, session_id: str) -> Optional[Path]:
        """获取会话的日志目录"""
        if session_id in self._sessions:
            return self._sessions[session_id]["log_dir"]
        return None


# 全局单例
_llm_logger: Optional[LLMLogger] = None

def get_llm_logger() -> LLMLogger:
    """获取全局 LLMLogger 实例"""
    global _llm_logger
    if _llm_logger is None:
        _llm_logger = LLMLogger()
    return _llm_logger


# 便捷函数
def start_log_session(session_id: str, metadata: Optional[Dict] = None) -> str:
    """开始日志会话"""
    return get_llm_logger().start_session(session_id, metadata)

def log_llm_call(
    session_id: str,
    call_type: str,
    model: str,
    prompt: str,
    response: str,
    **kwargs
):
    """记录 LLM 调用"""
    get_llm_logger().log_llm_call(session_id, call_type, model, prompt, response, **kwargs)

def log_user_chat(
    session_id: str,
    user_message: str,
    ai_response: str,
    context_length: int = 0,
    **kwargs
):
    """记录用户聊天"""
    get_llm_logger().log_user_chat(session_id, user_message, ai_response, context_length, **kwargs)

def end_log_session(session_id: str, summary: Optional[Dict] = None):
    """结束日志会话"""
    get_llm_logger().end_session(session_id, summary)
