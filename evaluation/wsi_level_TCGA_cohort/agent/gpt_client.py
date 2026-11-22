# -*- coding: utf-8 -*-
"""
Vision API客户端 - OpenRouter版本（支持Gemini等多模型）
"""
import os
import logging
import requests
import json
import time
import itertools
import threading

logger = logging.getLogger(__name__)

# ⭐ 全局会话ID，用于日志记录
_current_session_id = None
_session_lock = threading.Lock()

def set_current_session(session_id: str):
    """设置当前日志会话ID"""
    global _current_session_id
    with _session_lock:
        _current_session_id = session_id
        logger.debug(f"[LLM Session] 设置当前会话: {session_id}")

def get_current_session() -> str:
    """获取当前日志会话ID"""
    with _session_lock:
        return _current_session_id

def clear_current_session():
    """清除当前日志会话ID"""
    global _current_session_id
    with _session_lock:
        _current_session_id = None

def _log_llm_call(call_type, model, prompt, response, images_count, tokens_used, duration_ms, error, metadata):
    """内部函数：记录 LLM 调用到日志"""
    session_id = get_current_session()
    if not session_id:
        return  # 没有活跃会话，跳过日志记录
    
    try:
        from .llm_logger import log_llm_call
        log_llm_call(
            session_id=session_id,
            call_type=call_type,
            model=model,
            prompt=prompt,
            response=response or "",
            images_count=images_count,
            tokens_used=tokens_used,
            duration_ms=duration_ms,
            error=error,
            metadata=metadata
        )
    except Exception as e:
        logger.warning(f"日志记录失败: {e}")

# ============== API 提供商选择 ==============
# 开源快照只保留 OpenRouter；密钥仅从环境变量读取。
API_PROVIDER = "openrouter"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
logger.info("🔌 [Provider] OpenRouter")

# 支持单 Key 或逗号分隔 Key 池；仓库内没有任何默认 Key。
_env_keys = (
    os.getenv("OPENROUTER_API_KEYS", "").strip()
    or os.getenv("OPENROUTER_API_KEY", "").strip()
)
if _env_keys:
    API_KEYS = [k.strip() for k in _env_keys.split(",") if k.strip()]
    logger.info(f"✅ 从环境变量加载了 {len(API_KEYS)} 个 API Key")
else:
    API_KEYS = []
    logger.warning("⚠️ 未配置 OpenRouter API Key")

# API密钥轮询器（线程安全）
_api_key_cycle = itertools.cycle(API_KEYS)
_api_key_lock = threading.Lock()
_failed_keys = set()  # 记录失败的key，用于故障转移
_key_usage_count = {key: 0 for key in API_KEYS}  # 统计每个key的使用次数

# ⭐ 全局并发闸门：用 API_MAX_CONCURRENCY 配置；0 表示不限。
_def_conc = 0
try:
    _MAX_CONCURRENCY = int(os.getenv("API_MAX_CONCURRENCY", str(_def_conc)))
except ValueError:
    _MAX_CONCURRENCY = _def_conc
_API_SEMAPHORE = threading.Semaphore(_MAX_CONCURRENCY) if _MAX_CONCURRENCY > 0 else None
if _API_SEMAPHORE is not None:
    logger.info(f"🚦 [并发限流] 同时最多 {_MAX_CONCURRENCY} 个在飞请求 (provider={API_PROVIDER})")


def _post_throttled(url, headers, data, timeout=180):
    """所有出站 API 请求统一经过全局并发闸门(若启用)，避免超过服务商的并发上限。"""
    if _API_SEMAPHORE is None:
        return requests.post(url, headers=headers, json=data, timeout=timeout)
    with _API_SEMAPHORE:
        return requests.post(url, headers=headers, json=data, timeout=timeout)

def get_next_api_key():
    """
    获取下一个可用的API密钥（轮询 + 故障转移）
    
    策略：
    1. 轮询使用所有key，分散负载
    2. 跳过最近失败的key
    3. 如果所有key都失败，重置并重试
    """
    global _failed_keys

    if not API_KEYS:
        raise RuntimeError(
            "OpenRouter API key is not configured. Set OPENROUTER_API_KEY "
            "or OPENROUTER_API_KEYS."
        )

    with _api_key_lock:
        # 如果所有key都失败了，重置失败列表
        if len(_failed_keys) >= len(API_KEYS):
            logger.warning("⚠️ 所有API Key都曾失败，重置并重试...")
            _failed_keys.clear()
        
        # 尝试找到一个没有失败的key
        for _ in range(len(API_KEYS)):
            key = next(_api_key_cycle)
            if key not in _failed_keys:
                _key_usage_count[key] += 1
                return key
        
        # 如果都在失败列表中，返回第一个
        return API_KEYS[0]

def mark_key_failed(key):
    """标记某个key为失败状态"""
    with _api_key_lock:
        _failed_keys.add(key)
        logger.warning(f"⚠️ API Key 已标记为失败: {key[:20]}...")

def mark_key_success(key):
    """标记某个key为成功状态（从失败列表移除）"""
    with _api_key_lock:
        _failed_keys.discard(key)

def get_api_key_stats():
    """获取API Key使用统计"""
    with _api_key_lock:
        return {
            "total_keys": len(API_KEYS),
            "failed_keys": len(_failed_keys),
            "usage": {k[:20] + "...": v for k, v in _key_usage_count.items()}
        }

def get_headers_for_key(api_key):
    """根据API Key生成请求头"""
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "http://localhost:5000",
        "X-Title": "PathCopilot"
    }

# 默认使用第一个key的headers（兼容旧代码）
OPENROUTER_API_KEY = API_KEYS[0] if API_KEYS else ""
OPENROUTER_HEADERS = get_headers_for_key(OPENROUTER_API_KEY)
# 保留原正式 UCEC Run 使用的 Gemini 3.1 Pro 模型名。
DEFAULT_MODEL = "google/gemini-3.1-pro-preview"
GPT5_REASONING_EFFORT = None

# 环境变量可覆盖默认模型，不改变 API 提供商。
_env_model = os.getenv("PATHCOPILOT_MODEL", "").strip()
if _env_model:
    DEFAULT_MODEL = _env_model
    logger.info(f"🧠 [Model] 环境变量 PATHCOPILOT_MODEL 覆盖默认模型 → {DEFAULT_MODEL}")


def _maybe_add_reasoning(data, model):
    """对 GPT-5/o 系列推理模型注入 reasoning_effort(省钱)；其它模型不动。"""
    m = (model or "").lower()
    if GPT5_REASONING_EFFORT and ("gpt-5" in m or "o3" in m or "o4" in m):
        data["reasoning"] = {"effort": GPT5_REASONING_EFFORT}
    return data

# 兼容旧代码的别名
GPT_API_URL = OPENROUTER_API_URL

# Gemini 安全过滤设置：病理切片含人体组织/血液，容易触发安全过滤导致空响应
GEMINI_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

def call_gemini_mcp(
    messages,
    model=None,
    max_tokens=65536,
    max_retries=3,
    call_type="mcp",
    metadata=None,
    tools=None,
    tool_choice=None,
    return_message=False,
):
    """
    MCP风格的API调用 - 保持完整对话历史
    支持多API Key轮换和故障转移
    
    Args:
        messages: 完整的对话历史列表 [{"role": "user/assistant/system", "content": ...}, ...]
        model: 使用的模型
        max_tokens: 最大token数
        max_retries: 最大重试次数
        call_type: 调用类型（用于日志记录）
        metadata: 额外元数据（用于日志记录）
        tools: OpenAI-compatible API-native function tools
        tool_choice: Native function-calling policy, such as ``"auto"``
        return_message: Return the complete assistant message including tool calls
        
    Returns:
        tuple: (response_text, error_msg) - 成功时error_msg为None
    """
    if model is None:
        model = DEFAULT_MODEL
    
    # 提取用于日志记录的 prompt
    prompt_for_log = "\n".join([f"[{m.get('role', 'unknown')}]: {str(m.get('content', ''))[:500]}" for m in messages[-3:]])
    
    data = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    if tools:
        data["tools"] = tools
        data["tool_choice"] = tool_choice or "auto"
    if 'gemini' in (model or '').lower():
        data["safety_settings"] = GEMINI_SAFETY_SETTINGS
    _maybe_add_reasoning(data, model)
    
    for attempt in range(max_retries):
        # ⭐ 每次重试使用不同的API Key
        current_key = get_next_api_key()
        headers = get_headers_for_key(current_key)
        
        try:
            t_start = time.time()
            response = _post_throttled(
                OPENROUTER_API_URL,
                headers=headers,
                data=data,
                timeout=180
            )
            response.raise_for_status()
            result = response.json()
            
            if 'error' in result:
                error_obj = result['error']
                error_msg = error_obj.get('message', str(error_obj)) if isinstance(error_obj, dict) else str(error_obj)
                logger.warning(f"⚠️ MCP API错误 (Key: {current_key[:15]}...): {error_msg}")
                mark_key_failed(current_key)  # 标记失败
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                
                # ⭐ 记录失败日志
                _log_llm_call(call_type, model, prompt_for_log, None, 0, None, None, error_msg, metadata)
                return None, error_msg
            
            choices = result.get('choices') or []
            model_message = choices[0].get('message', {}) if choices else {}
            if not isinstance(model_message, dict):
                model_message = {}
            if (
                not model_message.get('tool_calls')
                and isinstance(model_message.get('function_call'), dict)
            ):
                model_message['tool_calls'] = [{
                    'id': 'call_legacy_function',
                    'type': 'function',
                    'function': model_message['function_call'],
                }]
            content = model_message.get('content') or ''
            if isinstance(content, list):
                content = ''.join(
                    str(part.get('text') or '')
                    for part in content
                    if isinstance(part, dict)
                )
            full_response = str(content).strip()
            log_response = full_response
            if model_message.get('tool_calls'):
                log_response = json.dumps(
                    model_message.get('tool_calls'),
                    ensure_ascii=False,
                )
            t_elapsed = time.time() - t_start
            
            # 成功，标记key可用
            mark_key_success(current_key)
            
            # Token使用情况
            usage = result.get('usage', {})
            logger.info(f"✅ MCP API成功 ({model}, {t_elapsed:.1f}s, Key#{API_KEYS.index(current_key)+1}) [Token] prompt={usage.get('prompt_tokens', '?')}, completion={usage.get('completion_tokens', '?')}")
            
            # ⭐ 记录成功日志
            tokens_used = {
                "prompt": usage.get('prompt_tokens'),
                "completion": usage.get('completion_tokens'),
                "total": usage.get('total_tokens')
            }
            _log_llm_call(call_type, model, prompt_for_log, log_response, 0, tokens_used, t_elapsed * 1000, None, metadata)
            
            return (model_message if return_message else full_response), None
            
        except Exception as e:
            error_detail = str(e)
            logger.warning(f"⚠️ MCP请求失败 (尝试 {attempt+1}/{max_retries}, Key: {current_key[:15]}...): {error_detail[:150]}")
            mark_key_failed(current_key)  # 标记失败，下次尝试用其他key
            if attempt < max_retries - 1:
                time.sleep(2)
    
    # ⭐ 记录最终失败日志
    _log_llm_call(call_type, model, prompt_for_log, None, 0, None, None, "所有重试均失败", metadata)
    return None, "所有重试均失败"


# Agent Runtime terminology; the historical name remains for compatibility.
call_agent_model = call_gemini_mcp


def call_gpt_vision(prompt, image_base64, model=None, max_tokens=65536):
    """
    调用Vision API分析图像（同步版本，默认使用Gemini模型）
    """
    if model is None:
        model = DEFAULT_MODEL
    return _call_gpt_vision_internal(prompt, image_base64, model, max_tokens)

def call_gpt_vision_batch(prompts_and_images, model=None, max_tokens=65536):
    """
    批量并行调用Vision API（高并发，使用OpenRouter）
    
    Args:
        prompts_and_images: [(prompt1, image_base64_1), (prompt2, image_base64_2), ...]
        
    Returns:
        list: 响应列表
    """
    import concurrent.futures
    
    if model is None:
        model = DEFAULT_MODEL
    
    logger.info(f"🚀 批量API调用: {len(prompts_and_images)}个请求，模型: {model}")
    
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(_call_gpt_vision_internal, prompt, image, model, max_tokens)
            for prompt, image in prompts_and_images
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                logger.error(f"并行API调用失败: {e}")
                results.append("")
    
    return results

def _call_gpt_vision_internal(prompt, image_base64, model=None, max_tokens=65536, call_type="vision", metadata=None):
    """
    调用Vision API分析图像（OpenRouter版本，支持Gemini等模型）
    支持多API Key轮换和故障转移
    
    Args:
        prompt: 文本提示
        image_base64: base64编码的图像
        model: 使用的模型（默认使用Gemini）
        max_tokens: 最大token数
        call_type: 调用类型（用于日志记录）
        metadata: 额外元数据（用于日志记录）
        
    Returns:
        str: API响应文本
    """
    if model is None:
        model = DEFAULT_MODEL
    
    # 最多重试次数：429并发限流较常见，需多重试几轮；可用 env API_MAX_RETRIES 覆盖
    try:
        max_retries = int(os.getenv("API_MAX_RETRIES", "10"))
    except ValueError:
        max_retries = 10
    
    for attempt in range(max_retries):
        # ⭐ 每次重试使用不同的API Key（轮换 + 故障转移）
        current_key = get_next_api_key()
        headers = get_headers_for_key(current_key)
        key_index = API_KEYS.index(current_key) + 1
        
        try:
            t_start = time.time()
            
            # 构建消息内容
            if image_base64:
                message_content = [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
                ]
            else:
                message_content = prompt
            
            data = {
                "model": model,
                "messages": [{"role": "user", "content": message_content}],
                "max_tokens": max_tokens,
                "temperature": 0.2
            }
            if 'gemini' in (model or '').lower():
                data["safety_settings"] = GEMINI_SAFETY_SETTINGS
            _maybe_add_reasoning(data, model)
            
            response = _post_throttled(
                OPENROUTER_API_URL,
                headers=headers,
                data=data,
                timeout=180
            )
            response.raise_for_status()
            result = response.json()
            
            # 检查API错误
            if 'error' in result:
                error_obj = result['error']
                error_msg = error_obj.get('message', str(error_obj)) if isinstance(error_obj, dict) else str(error_obj)
                error_code = error_obj.get('code', 'unknown') if isinstance(error_obj, dict) else 'unknown'
                logger.warning(f"⚠️ API错误 [{error_code}] (Key#{key_index}): {error_msg}")
                mark_key_failed(current_key)  # 标记失败
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                
                # ⭐ 记录失败日志
                _log_llm_call(call_type, model, prompt, None, 1 if image_base64 else 0, None, None, error_msg, metadata)
                return '{"selected_grids": []}'
            
            content = result['choices'][0]['message']['content']
            t_elapsed = time.time() - t_start
            
            # Gemini 3.1 Pro 安全过滤：content 可能为 None（completion_tokens=0）
            if content is None:
                usage = result.get('usage', {})
                finish_reason = result['choices'][0].get('finish_reason', 'unknown')
                logger.warning(f"⚠️ API返回空内容 (finish_reason={finish_reason}, Key#{key_index}, {t_elapsed:.1f}s, prompt_tokens={usage.get('prompt_tokens', '?')}), 可能触发安全过滤")
                mark_key_failed(current_key)
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                _log_llm_call(call_type, model, prompt, None, 1 if image_base64 else 0, None, None, f"content=None, finish_reason={finish_reason}", metadata)
                return None
            
            # 成功，标记key可用
            mark_key_success(current_key)
            
            # Token使用情况
            usage = result.get('usage', {})
            logger.info(f"✅ API成功 ({model}, {t_elapsed:.1f}s, Key#{key_index}) [Token] prompt={usage.get('prompt_tokens', '?')}, completion={usage.get('completion_tokens', '?')}")
            
            # ⭐ 记录成功日志
            tokens_used = {
                "prompt": usage.get('prompt_tokens'),
                "completion": usage.get('completion_tokens'),
                "total": usage.get('total_tokens')
            }
            _log_llm_call(call_type, model, prompt, content, 1 if image_base64 else 0, tokens_used, t_elapsed * 1000, None, metadata)
            
            return content
            
        except Exception as e:
            # 获取详细错误信息
            error_detail = str(e)
            error_type = type(e).__name__
            
            # 尝试获取HTTP响应中的详细错误
            if hasattr(e, 'response') and e.response is not None:
                try:
                    resp_text = e.response.text[:300]
                    status_code = e.response.status_code
                    error_detail = f"HTTP {status_code}: {resp_text}"
                except:
                    pass

            # ⭐ 余额不足：所有 key 共用同一账户，再换 key 也没钱→立即中止, 不再狂刷(避免预扣冻结+浪费)
            if ("insufficient_user_quota" in error_detail or "用户额度不足" in error_detail
                    or "余额不足" in error_detail):
                logger.error("❌ 账户余额不足(insufficient_quota)，立即停止重试。请充值后重跑(断点续跑会接上)。")
                _log_llm_call(call_type, model, prompt, None, 1 if image_base64 else 0, None, None, "insufficient_quota", metadata)
                return '{"selected_grids": []}'

            # ⭐ 429 并发/限流：不是某个key坏了(所有key共用同一上游账号的并发额度)，
            #   不标记key失败，退避后重试同样有效；避免把所有key误标失败导致提前放弃。
            _is_429 = ("429" in error_detail or "Concurrency limit" in error_detail
                       or "Too many pending" in error_detail or "rate_limit" in error_detail.lower())
            if not _is_429:
                mark_key_failed(current_key)  # 真失败才标记，下次换其他key
            
            if attempt < max_retries - 1:
                # 网关错误(502/503/504/HTML错误页)→指数退避；429并发→较长退避给账号喘息
                _is_gw = any(s in error_detail for s in ("502", "503", "504", "<!DOCTYPE html", "Bad Gateway", "Gateway"))
                if _is_429:
                    _wait = min(30, 4 + 2 * attempt)   # 429: 4,6,8...封顶30s
                elif _is_gw:
                    _wait = min(30, 3 * (attempt + 1))
                else:
                    _wait = 2
                logger.warning(f"⚠️ API失败 (尝试 {attempt+1}/{max_retries}, Key#{key_index}), 类型: {error_type}, 详情: {error_detail[:100]}, {_wait}s后重试...")
                time.sleep(_wait)
                continue
            else:
                logger.error(f"❌ API最终失败（已尝试{max_retries}个Key）")
                logger.error(f"   错误类型: {error_type}")
                logger.error(f"   错误详情: {error_detail[:200]}")
                
                # ⭐ 记录最终失败日志
                _log_llm_call(call_type, model, prompt, None, 1 if image_base64 else 0, None, None, f"{error_type}: {error_detail[:200]}", metadata)
                return '{"selected_grids": []}'
    
    return '{"selected_grids": []}'
