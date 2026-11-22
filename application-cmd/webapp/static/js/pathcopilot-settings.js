(function () {
    'use strict';

    const byId = id => document.getElementById(id);
    let currentDefaults = {};
    let pendingSetupDiagnosis = null;

    function lang() {
        return window.__lang === 'en' ? 'en' : 'zh';
    }

    function apiKeys() {
        return [...document.querySelectorAll('#apiKeysList .api-key-input')]
            .map(input => input.value.trim())
            .filter(Boolean);
    }

    function parseBulkApiKeys(raw) {
        const text = String(raw || '').trim();
        if (!text) return [];
        try {
            const parsed = JSON.parse(text);
            if (Array.isArray(parsed)) {
                return [...new Set(parsed.map(value => String(value).trim()).filter(Boolean))];
            }
        } catch (_) {}
        return [...new Set(text.split(/[\r\n,;]+/).map(value => value.trim()).filter(Boolean))];
    }

    function clearStaleConnectionState() {
        renderConnectionDiagnostic(null);
        const result = byId('testResult');
        if (result) {
            result.textContent = '';
            result.className = 'settings-test-result';
        }
    }

    function updateKeyCount() {
        const count = byId('keyCount');
        if (count) count.textContent = `(${apiKeys().length})`;
    }

    function addApiKeyRow(value) {
        const list = byId('apiKeysList');
        if (!list) return;
        const row = document.createElement('div');
        row.className = 'api-key-row';

        const input = document.createElement('input');
        input.type = 'password';
        input.className = 'api-key-input';
        input.value = String(value || '');
        input.placeholder = 'sk-or-v1-...';
        input.spellcheck = false;
        input.autocomplete = 'off';
        input.addEventListener('input', () => {
            updateKeyCount();
            clearStaleConnectionState();
        });
        input.addEventListener('paste', event => {
            const pasted = event.clipboardData?.getData('text') || '';
            const keys = parseBulkApiKeys(pasted);
            if (keys.length < 2) return;
            event.preventDefault();
            input.value = keys.shift();
            keys.forEach(addApiKeyRow);
            updateKeyCount();
            clearStaleConnectionState();
        });

        const toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'api-key-toggle';
        toggle.title = lang() === 'en' ? 'Show or hide key' : '显示或隐藏密钥';
        toggle.innerHTML = '<i class="fas fa-eye"></i>';
        toggle.addEventListener('click', () => toggleKeyVis(toggle));

        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'api-key-remove';
        remove.title = lang() === 'en' ? 'Remove key' : '删除密钥';
        remove.innerHTML = '<i class="fas fa-trash-alt"></i>';
        remove.addEventListener('click', () => removeKeyRow(remove));

        row.append(input, toggle, remove);
        list.appendChild(row);
        updateKeyCount();
    }

    function removeKeyRow(button) {
        const list = byId('apiKeysList');
        button.closest('.api-key-row')?.remove();
        if (list && !list.children.length) addApiKeyRow('');
        updateKeyCount();
    }

    function toggleKeyVis(button) {
        const input = button.previousElementSibling;
        const icon = button.querySelector('i');
        if (!input) return;
        const visible = input.type === 'text';
        input.type = visible ? 'password' : 'text';
        if (icon) icon.className = visible ? 'fas fa-eye' : 'fas fa-eye-slash';
    }

    function updateActiveNotice(config) {
        const endpoint = String(config?.api_url || '');
        const model = String(config?.model || '');
        let host = endpoint;
        try { host = new URL(endpoint).host; } catch (_) {}
        const provider = byId('effectiveProviderLabel');
        const notice = byId('activeLlmNotice');
        if (provider) provider.textContent = host;
        if (!notice) return;
        if (lang() === 'en') {
            notice.innerHTML = `Active configuration: <code>${model || 'not set'}</code> via ${host || 'the configured endpoint'}.`;
        } else {
            notice.innerHTML = `当前配置：通过 ${host || '已配置服务'} 使用 <code>${model || '未设置模型'}</code>。`;
        }
    }

    const DIAGNOSTIC_ZH = {
        invalid_url: ['API 地址格式不正确。', ['请填写以 http:// 或 https:// 开头的完整地址。']],
        missing_model: ['尚未填写模型标识。', ['请从服务商控制台复制准确的模型名称。']],
        missing_custom_key: ['自定义服务地址缺少对应的 API Key。', ['请输入由同一服务商签发的密钥。']],
        missing_api_key: ['尚未配置 API Key。', ['请在下方输入有效密钥，然后测试连接。']],
        invalid_api_key: ['身份验证失败，API Key 可能无效。', ['检查密钥是否完整，并确认它属于当前服务商。', '删除密钥首尾可能存在的空格。']],
        permission_denied: ['当前账户或密钥没有访问权限。', ['检查余额、模型权限、地区限制和组织权限。']],
        endpoint_or_model_not_found: ['没有找到接口路径或模型。', ['确认地址包含正确的 /v1/chat/completions 路径。', '从服务商模型列表复制准确标识。']],
        rate_limited: ['服务商正在限制请求频率。', ['稍后重试，或添加同一服务商的其他有效密钥。']],
        provider_unavailable: ['上游模型服务暂时不可用。', ['稍后重试，或检查服务商状态。']],
        timeout: ['模型连接超时。', ['检查服务是否运行、网络、代理、防火墙和 VPN。']],
        tls_error: ['TLS 证书校验失败。', ['检查系统时间、代理和服务端证书。']],
        dns_error: ['无法解析服务商域名。', ['检查网址拼写、DNS、代理和网络连接。']],
        connection_refused: ['目标服务拒绝连接。', ['若使用本地接口，请先启动本地模型或反向代理。', '检查主机名和端口。']],
        invalid_response: ['服务已响应，但返回格式不是 OpenAI chat-completions 格式。', ['确认接口路径和兼容格式。']],
        invalid_request: ['服务端拒绝了请求格式。', ['确认这是 OpenAI 兼容的 chat-completions 接口。']],
        unsupported_parameters: ['模型不支持部分测试参数。', ['尝试服务商提供的标准聊天模型。']],
        network_error: ['模型连接未能完成。', ['检查 API 地址、模型名称、API Key、网络及服务状态。']]
    };

    function renderConnectionDiagnostic(diagnosis) {
        const panel = byId('connectionDiagnostic');
        const summary = byId('connectionDiagnosticSummary');
        const upstream = byId('connectionDiagnosticUpstream');
        const suggestions = byId('connectionDiagnosticSuggestions');
        if (!panel || !summary || !upstream || !suggestions) return;
        if (!diagnosis) {
            panel.hidden = true;
            return;
        }
        const localized = lang() === 'zh' ? DIAGNOSTIC_ZH[diagnosis.code] : null;
        summary.textContent = localized?.[0] || diagnosis.summary || (lang() === 'en' ? 'Connection failed.' : '连接失败。');
        suggestions.replaceChildren();
        (localized?.[1] || diagnosis.suggestions || []).forEach(item => {
            const li = document.createElement('li');
            li.textContent = item;
            suggestions.appendChild(li);
        });
        upstream.textContent = diagnosis.upstream_message ? `Provider: ${diagnosis.upstream_message}` : '';
        upstream.hidden = !diagnosis.upstream_message;
        panel.hidden = false;
    }

    async function loadLlmSettings() {
        const result = byId('testResult');
        if (result) result.textContent = '';
        renderConnectionDiagnostic(null);
        try {
            const response = await fetch('/api/user/settings', { cache: 'no-store' });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            if (!data.success || !data.settings) return null;
            currentDefaults = data.defaults || data.effective || {};
            const effective = data.effective || currentDefaults;
            const urlInput = byId('settingsApiUrl');
            const modelInput = byId('settingsModel');
            if (urlInput) urlInput.value = data.settings.api_url || currentDefaults.api_url || '';
            if (modelInput) modelInput.value = data.settings.llm_model || currentDefaults.model || '';
            updateActiveNotice(effective);

            const list = byId('apiKeysList');
            if (list) {
                list.replaceChildren();
                let keys = [];
                try { keys = JSON.parse(data.settings.api_key || '[]'); } catch (_) {}
                if (typeof keys === 'string') keys = keys ? [keys] : [];
                if (!Array.isArray(keys)) keys = [];
                (keys.length ? keys : ['']).forEach(addApiKeyRow);
            }
            const configured = Boolean(data.has_api_key ?? (Number(effective.key_count) > 0));
            window.PathCopilotApiKeyGuard?.markConfigured(configured);
            return configured;
        } catch (_) {
            const list = byId('apiKeysList');
            if (list && !list.children.length) addApiKeyRow('');
            return null;
        }
    }

    function activateSettingsPanel(name, diagnosis) {
        const target = name || 'language';
        document.querySelectorAll('[data-settings-target]').forEach(button => {
            const active = button.dataset.settingsTarget === target;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-current', active ? 'page' : 'false');
        });
        document.querySelectorAll('[data-settings-panel]').forEach(panel => {
            panel.classList.toggle('is-active', panel.dataset.settingsPanel === target);
        });
        if (target !== 'model') return Promise.resolve();
        return loadLlmSettings().then(() => renderConnectionDiagnostic(diagnosis));
    }

    function openSettingsModal(initialPanel, diagnosis) {
        const overlay = byId('settingsModalOverlay');
        if (!overlay) return Promise.resolve();
        overlay.style.display = 'block';
        document.body.classList.add('product-settings-open');
        const search = byId('settingsSearchInput');
        if (search) search.value = '';
        document.querySelectorAll('[data-settings-target]').forEach(button => { button.hidden = false; });
        const ready = activateSettingsPanel(initialPanel || 'language', diagnosis);
        window.PathCopilotRoiPalette?.bindSettingsControls();
        window.PathCopilotRoiPalette?.syncSettingsControls();
        requestAnimationFrame(() => Promise.resolve(ready).finally(() => {
            if ((initialPanel || 'language') === 'model') document.querySelector('#apiKeysList .api-key-input')?.focus();
            else search?.focus();
        }));
        return ready;
    }

    function closeSettingsModal() {
        const overlay = byId('settingsModalOverlay');
        if (overlay) overlay.style.display = 'none';
        document.body.classList.remove('product-settings-open');
        byId('v2HomePrompt')?.focus();
    }

    function showApiKeySetupPrompt(diagnosis = null) {
        const prompt = byId('apiKeySetupPrompt');
        if (!prompt) return false;
        pendingSetupDiagnosis = diagnosis;
        prompt.hidden = false;
        document.body.classList.add('api-key-setup-open');
        requestAnimationFrame(() => prompt.querySelector('.api-key-setup-primary')?.focus());
        return false;
    }

    function dismissApiKeySetupPrompt() {
        const prompt = byId('apiKeySetupPrompt');
        if (prompt) prompt.hidden = true;
        document.body.classList.remove('api-key-setup-open');
    }

    function openApiKeySettings() {
        const diagnosis = pendingSetupDiagnosis;
        pendingSetupDiagnosis = null;
        dismissApiKeySetupPrompt();
        return openSettingsModal('model', diagnosis);
    }

    function currentSettingsPayload() {
        return {
            llm_provider: 'openai-compatible',
            api_url: byId('settingsApiUrl')?.value.trim() || '',
            llm_model: byId('settingsModel')?.value.trim() || '',
            api_key: JSON.stringify(apiKeys())
        };
    }

    function acceptSavedConfiguration(data, fallbackKeyCount, validated) {
        const effective = data?.effective || {};
        const keyCount = Math.max(Number(effective.key_count) || 0, Number(fallbackKeyCount) || 0);
        const configured = Boolean(data?.has_api_key || keyCount > 0);
        const guard = window.PathCopilotApiKeyGuard;
        guard?.markConfigured(configured);
        if (guard && configured) {
            const signature = [
                effective.api_url || byId('settingsApiUrl')?.value.trim() || '',
                effective.model || byId('settingsModel')?.value.trim() || '',
                keyCount
            ].join('|');
            guard.configSignature = signature;
            guard.validatedSignature = validated ? signature : '';
        }
        return configured;
    }

    async function saveSettings() {
        const keys = apiKeys();
        const payload = currentSettingsPayload();
        try {
            const response = await fetch('/api/user/settings', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
            });
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.error || `HTTP ${response.status}`);
            acceptSavedConfiguration(data, keys.length, false);
            closeSettingsModal();
            window.pathchat?.showNotification?.('success', lang() === 'en' ? 'Settings saved' : '设置已保存');
        } catch (error) {
            renderConnectionDiagnostic({
                code: 'network_error',
                summary: error.message,
                suggestions: lang() === 'en' ? ['Check the settings and try again.'] : ['检查设置后重试。']
            });
        }
    }

    async function testLlmConnection() {
        const button = byId('testLlmBtn');
        const result = byId('testResult');
        const keys = apiKeys();
        const settingsPayload = currentSettingsPayload();
        if (button) button.disabled = true;
        if (result) result.textContent = lang() === 'en' ? 'Testing connection…' : '正在测试连接…';
        renderConnectionDiagnostic(null);
        try {
            const response = await fetch('/api/user/test_llm', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    api_url: settingsPayload.api_url,
                    llm_model: settingsPayload.llm_model,
                    api_key: keys[0] || ''
                })
            });
            const data = await response.json();
            if (!response.ok || !data.success) {
                if (result) {
                    result.textContent = lang() === 'en' ? 'Connection failed' : '连接失败';
                    result.className = 'settings-test-result error';
                }
                renderConnectionDiagnostic(data.diagnosis || { code: 'network_error', summary: data.error });
                return false;
            }

            // A successful connection test is also an explicit confirmation of
            // the current form values. Persist the complete key pool so the
            // workspace guard and the analysis backend see the same config.
            const saveResponse = await fetch('/api/user/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(settingsPayload)
            });
            const saveData = await saveResponse.json();
            if (!saveResponse.ok || !saveData.success) {
                throw new Error(saveData.error || `HTTP ${saveResponse.status}`);
            }
            acceptSavedConfiguration(saveData, keys.length, true);
            updateActiveNotice(saveData.effective || {
                api_url: settingsPayload.api_url,
                model: settingsPayload.llm_model
            });
            if (result) {
                result.textContent = `${lang() === 'en' ? 'Connected and saved' : '连接成功并已保存'} · ${data.model || ''} · ${data.latency_ms || 0} ms`;
                result.className = 'settings-test-result success';
            }
            return true;
        } catch (error) {
            if (result) {
                result.textContent = lang() === 'en' ? 'Connection failed' : '连接失败';
                result.className = 'settings-test-result error';
            }
            renderConnectionDiagnostic({ code: 'network_error', upstream_message: error.message });
            return false;
        } finally {
            if (button) button.disabled = false;
        }
    }

    window.PathCopilotApiKeyGuard = {
        configured: null,
        pending: null,
        configSignature: '',
        validatedSignature: '',
        validationPending: null,
        markConfigured(value) {
            this.configured = Boolean(value);
            this.validatedSignature = '';
            if (this.configured) {
                pendingSetupDiagnosis = null;
                dismissApiKeySetupPrompt();
            }
        },
        async refresh() {
            if (this.pending) return this.pending;
            this.pending = fetch('/api/user/settings', { cache: 'no-store' })
                .then(response => {
                    if (!response.ok) throw new Error(`HTTP ${response.status}`);
                    return response.json();
                })
                .then(data => {
                    if (!data.success) return null;
                    const effective = data.effective || {};
                    const signature = [effective.api_url || '', effective.model || '', Number(effective.key_count) || 0].join('|');
                    if (signature !== this.configSignature) {
                        this.configSignature = signature;
                        this.validatedSignature = '';
                    }
                    this.configured = Boolean(data.has_api_key || (Number(effective.key_count) > 0));
                    return this.configured;
                })
                .catch(() => null)
                .finally(() => { this.pending = null; });
            return this.pending;
        },
        requireNow(diagnosis) {
            return showApiKeySetupPrompt(diagnosis);
        },
        async ensureConfigured() {
            const configured = await this.refresh();
            if (configured !== true) {
                this.requireNow(configured === false ? {
                    code: 'missing_api_key', summary: 'No API key is available.', suggestions: ['Enter a valid API key and test the connection.']
                } : {
                    code: 'network_error', summary: 'Could not read the current model configuration.'
                });
                return false;
            }
            // A persisted endpoint, model and key pool are sufficient to proceed.
            // Connectivity is checked by the real request, or explicitly via the
            // optional "Test Connection" button in Settings.
            return true;
        }
    };

    Object.assign(window, {
        addApiKeyRow, removeKeyRow, toggleKeyVis, updateKeyCount,
        loadLlmSettings, renderConnectionDiagnostic, openSettingsModal,
        closeSettingsModal, saveSettings, testLlmConnection,
        showApiKeySetupPrompt, dismissApiKeySetupPrompt, openApiKeySettings
    });

    document.querySelector('.product-settings-nav')?.addEventListener('click', event => {
        const button = event.target.closest('[data-settings-target]');
        if (button) activateSettingsPanel(button.dataset.settingsTarget);
    });
    byId('settingsApiUrl')?.addEventListener('input', clearStaleConnectionState);
    byId('settingsModel')?.addEventListener('input', clearStaleConnectionState);
    byId('settingsModalOverlay')?.addEventListener('click', event => {
        if (event.target === event.currentTarget) closeSettingsModal();
    });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && document.body.classList.contains('product-settings-open')) closeSettingsModal();
    });
    document.addEventListener('DOMContentLoaded', () => window.PathCopilotApiKeyGuard.refresh());
})();
