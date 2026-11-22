/* PathCopilot V2 workbench interactions. Functional analysis logic stays in pathchat.js. */
(function () {
    'use strict';

    const copy = {
        en: {
            workspace: 'Workspace', demo: 'Demo', settings: 'Settings', local: 'Local workspace',
            homeRegion: 'PathCopilot home', collapseSidebar: 'Collapse sidebar', expandSidebar: 'Expand sidebar',
            primaryActions: 'Primary actions', refreshThreads: 'Refresh conversations', refreshCases: 'Refresh cases',
            submitAnalysis: 'Start analysis',
            newCase: 'New case', demoCase: 'Demo case', caseFiles: 'Case files', taskHome: 'Task home',
            openCaseRail: 'Open case sidebar', closeCaseRail: 'Close case sidebar',
            subagentResults: 'Subagent results', showSubagentResults: 'Show Subagent results',
            hideSubagentResults: 'Hide Subagent results',
            pathologyWorkspace: 'Pathology workspace', newAnalysis: 'New analysis', uploadSlide: 'Upload slide',
            openDemoCase: 'Demo case', recentCases: 'Recent cases', loadingCases: 'Reading local cases…',
            recentChats: 'Recent chats', loadingChats: 'Reading local chats…', noChats: 'No saved chats yet',
            recentItems: 'Recents', loadingRecents: 'Reading recents…', noRecents: 'No recent conversations or cases yet',
            refreshRecents: 'Refresh',
            patientLevelMark: (count) => `... *${count} slides (patient level)`,
            chatTurns: 'messages', renameChat: 'Rename chat', newConversation: 'New pathology conversation',
            threadActions: 'Conversation actions', openConversation: 'Open conversation', copyChatTitle: 'Copy title',
            renameConversation: 'Rename conversation', cancel: 'Cancel', save: 'Save',
            localDoctor: 'Local doctor', privateWorkspace: 'Private local workspace', localOnly: 'Data stays on this device',
            ready: 'Ready', homeQuestion: 'Analyze a slide or ask a pathology question',
            newPathologyTask: 'Pathology chat and WSI analysis',
            homePromptPlaceholder: 'Ask a pathology question, or describe a WSI analysis task',
            selectSlideBeforePrompt: 'Upload or open a WSI before starting an analysis.',
            thinking: 'Thinking', homeChatFailed: 'Could not reply. Please try again.',
            attachImage: 'Add image or slide', removeImage: 'Remove image',
            imageTooLarge: 'Each image must be 15 MB or smaller.',
            imageUnsupported: 'Use a JPG, PNG, WebP, GIF, or BMP image here. Whole-slide files open in WSI analysis.',
            imageLimit: 'You can attach up to 4 images.', wsiLimit: 'You can attach up to 8 whole-slide files.',
            removeWsi: 'Remove slide', wsiReady: 'Whole-slide image ready',
            defaultImageQuestion: 'Describe the visible pathology findings and the image-quality limitations.',
            webPlanning: 'Thinking', webSearching: 'Searching medical literature',
            webReading: 'Reading sources', webComplete: 'Web research complete',
            webSources: 'Sources', webPrivacy: 'Only de-identified medical concepts are sent to public search services.',
            autoPathology: 'Pathology copilot', wholeSlideAnalysis: 'Whole-slide analysis',
            diagnosticReview: 'Diagnostic review', attachWSI: 'Attach WSI',
            clinicalDisclaimer: 'AI assists diagnosis; final conclusions require pathologist review.',
            dropToAnalyze: 'Drop to attach an image or whole-slide image', noCases: 'No local cases yet',
            caseLoadFailed: 'Could not read local cases', slideUnit: 'slide', slidesUnit: 'slides',
            slideStudio: 'Slide studio', noSlide: 'No slide selected', workspaceReady: 'Workspace ready',
            openSlide: 'Open a whole-slide image', copilot: 'Pathology copilot', caseInspector: 'Case inspector',
            messages: 'Messages', execution: 'Execution', evidence: 'Evidence', environment: 'Environment', review: 'Review',
            environmentTitle: 'Analysis environment', environmentHint: 'Runtime and pathology workflow configuration.',
            viewerEngine: 'Viewer engine', slideEngine: 'WSI engine', analysisScale: 'Analysis scale',
            workspaceMode: 'Workspace', localPrivate: 'Local & private', openSettings: 'Open model settings',
            reviewTitle: 'Human review', reviewHint: 'Validate every evidence layer before signing out.',
            macroReview: 'Low-power overview', lesionReview: 'Lesion evidence', cellReview: 'Cell-level evidence',
            reportReview: 'Diagnostic report', waitingAnalysis: 'Waiting for analysis',
            reviewNote: 'AI findings remain editable and require pathologist confirmation.',
            inProgress: 'Analysis in progress', evidenceReady: 'Evidence ready for review', reportReady: 'Report ready for review'
        },
        zh: {
            workspace: '工作台', demo: '演示', settings: '设置', local: '本地工作区',
            homeRegion: 'PathCopilot 启动页', collapseSidebar: '收起侧栏', expandSidebar: '展开侧栏',
            primaryActions: '主要操作', refreshThreads: '刷新对话', refreshCases: '刷新病例',
            submitAnalysis: '启动分析',
            newCase: '新建病例', demoCase: '演示病例', caseFiles: '病例文件', taskHome: '任务首页',
            openCaseRail: '打开病例栏', closeCaseRail: '关闭病例栏',
            subagentResults: 'Subagent 结果', showSubagentResults: '显示 Subagent 结果',
            hideSubagentResults: '收起 Subagent 结果',
            pathologyWorkspace: '病理工作区', newAnalysis: '新建分析', uploadSlide: '上传切片',
            openDemoCase: '演示病例', recentCases: '最近病例', loadingCases: '正在读取本地病例…',
            recentChats: '最近对话', loadingChats: '正在读取本地对话…', noChats: '还没有保存的对话',
            recentItems: '最近', loadingRecents: '正在读取最近项目…', noRecents: '还没有对话或病例',
            refreshRecents: '刷新',
            patientLevelMark: (count) => `... *${count}张（patient level）`,
            chatTurns: '条消息', renameChat: '重命名对话', newConversation: '新病理对话',
            threadActions: '对话操作', openConversation: '打开对话', copyChatTitle: '复制标题',
            renameConversation: '重命名对话', cancel: '取消', save: '保存',
            localDoctor: '本地医生', privateWorkspace: '本地私有工作区', localOnly: '数据仅保存在本机',
            ready: '就绪', homeQuestion: '分析切片，或咨询病理问题',
            newPathologyTask: '病理对话与 WSI 分析',
            homePromptPlaceholder: '咨询病理问题，或描述需要执行的 WSI 分析任务',
            selectSlideBeforePrompt: '请先上传或打开一个 WSI，再开始分析。',
            thinking: '正在思考', homeChatFailed: '暂时无法回复，请稍后重试。',
            attachImage: '添加图片或切片', removeImage: '移除图片',
            imageTooLarge: '单张图片不能超过 15 MB。',
            imageUnsupported: '此处支持 JPG、PNG、WebP、GIF 或 BMP；数字切片请使用“上传切片”。',
            imageLimit: '最多可添加 4 张图片。', wsiLimit: '最多可添加 8 张数字切片。',
            removeWsi: '移除切片', wsiReady: '数字切片已就绪',
            defaultImageQuestion: '请描述图中可见的病理形态，并说明图像质量带来的判断限制。',
            webPlanning: '正在思考', webSearching: '正在检索医学文献',
            webReading: '正在阅读来源', webComplete: '联网检索完成',
            webSources: '参考来源', webPrivacy: '仅去标识化医学概念会发送至公共检索服务。',
            autoPathology: '智能病理分析', wholeSlideAnalysis: '全切片分析',
            diagnosticReview: '诊断要点', attachWSI: '添加 WSI',
            clinicalDisclaimer: 'AI 结果用于辅助诊断，最终结论由病理医生审核。',
            dropToAnalyze: '松开以添加图片或数字切片', noCases: '还没有本地病例',
            caseLoadFailed: '无法读取本地病例', slideUnit: '张切片', slidesUnit: '张切片',
            slideStudio: '切片工作室', noSlide: '尚未选择切片', workspaceReady: '工作区已就绪',
            openSlide: '打开全视野病理切片', copilot: '病理智能助手', caseInspector: '病例检查器',
            messages: '对话', execution: '执行记录', evidence: '证据', environment: '环境', review: '审核',
            environmentTitle: '分析环境', environmentHint: '运行环境与病理分析流程配置。',
            viewerEngine: '查看器引擎', slideEngine: 'WSI 引擎', analysisScale: '分析倍率',
            workspaceMode: '工作空间', localPrivate: '本地与私有', openSettings: '打开模型设置',
            reviewTitle: '人工审核', reviewHint: '签发前逐层核对病理证据。',
            macroReview: '低倍整体观察', lesionReview: '病灶证据', cellReview: '细胞级证据',
            reportReview: '诊断报告', waitingAnalysis: '等待分析',
            reviewNote: 'AI 结论可以修改，并需由病理医生最终确认。',
            inProgress: '正在分析', evidenceReady: '证据已生成，等待审核', reportReady: '报告已生成，等待审核'
        }
    };

    let initialized = false;
    let homeCasesRefreshTimer = null;
    let homeThreadsRefreshTimer = null;
    let homeDragDepth = 0;
    let homeChatHistory = [];
    let homeChatBusy = false;
    let homeImageAttachments = [];
    let homeWsiAttachments = [];
    let homeThreadId = null;
    let homeThreads = [];
    let homeProjects = [];
    let homeThreadsLoaded = false;
    let homeCasesLoaded = false;
    let homeTurnRailFrame = 0;
    let subagentPanelObserver = null;

    const HOME_IMAGE_MIME_TYPES = new Set([
        'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/bmp'
    ]);
    const HOME_IMAGE_LIMIT = 4;
    const HOME_WSI_LIMIT = 8;
    const HOME_IMAGE_MAX_BYTES = 15 * 1024 * 1024;
    const HOME_IMAGE_MAX_DIMENSION = 2048;
    const WSI_EXTENSIONS = new Set([
        '.svs', '.ndpi', '.tiff', '.tif', '.vms', '.vmu', '.scn', '.mrxs', '.bif'
    ]);

    function language() {
        return window.__lang === 'en' ? 'en' : 'zh';
    }

    function t(key) {
        const lang = language();
        return copy[lang][key] || copy.en[key] || key;
    }

    function homeFileExtension(file) {
        const name = String(file?.name || '').toLowerCase();
        const dot = name.lastIndexOf('.');
        return dot >= 0 ? name.slice(dot) : '';
    }

    function isWsiFile(file) {
        return WSI_EXTENSIONS.has(homeFileExtension(file));
    }

    function notifyHomeAttachment(message) {
        if (window.pathchat && typeof window.pathchat.showNotification === 'function') {
            window.pathchat.showNotification('warning', message);
        } else {
            console.warn(`[PathCopilot V2] ${message}`);
        }
    }

    function readHomeImageFile(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onerror = () => reject(new Error(t('imageUnsupported')));
            reader.onload = async () => {
                try {
                    let dataUrl = String(reader.result || '');
                    let mime = String(file.type || '').toLowerCase();
                    const source = new Image();
                    source.src = dataUrl;
                    await source.decode();
                    const longest = Math.max(source.naturalWidth, source.naturalHeight);
                    if (longest > HOME_IMAGE_MAX_DIMENSION) {
                        const scale = HOME_IMAGE_MAX_DIMENSION / longest;
                        const canvas = document.createElement('canvas');
                        canvas.width = Math.max(1, Math.round(source.naturalWidth * scale));
                        canvas.height = Math.max(1, Math.round(source.naturalHeight * scale));
                        const context = canvas.getContext('2d', { alpha: mime !== 'image/jpeg' });
                        if (!context) throw new Error(t('imageUnsupported'));
                        context.imageSmoothingEnabled = true;
                        context.imageSmoothingQuality = 'high';
                        context.drawImage(source, 0, 0, canvas.width, canvas.height);
                        mime = mime === 'image/jpeg' ? 'image/jpeg' : 'image/png';
                        dataUrl = canvas.toDataURL(mime, mime === 'image/jpeg' ? 0.94 : undefined);
                    }
                    const comma = dataUrl.indexOf(',');
                    if (comma < 0) throw new Error(t('imageUnsupported'));
                    resolve({
                        id: (window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`),
                        name: String(file.name || `image-${Date.now()}`),
                        mime,
                        size: Number(file.size) || 0,
                        data: dataUrl.slice(comma + 1),
                        previewUrl: dataUrl
                    });
                } catch (error) {
                    reject(error instanceof Error ? error : new Error(t('imageUnsupported')));
                }
            };
            reader.readAsDataURL(file);
        });
    }

    function renderHomeImageAttachments() {
        const host = document.getElementById('v2HomeAttachments');
        const attachButton = document.getElementById('v2HomeAttach');
        if (attachButton) {
            attachButton.title = t('attachImage');
            attachButton.setAttribute('aria-label', t('attachImage'));
        }
        if (!host) return;
        host.replaceChildren();
        host.hidden = homeImageAttachments.length === 0 && homeWsiAttachments.length === 0;
        homeImageAttachments.forEach((attachment) => {
            const item = document.createElement('div');
            item.className = 'v2-home-attachment';
            item.title = attachment.name;
            const image = document.createElement('img');
            image.src = attachment.previewUrl;
            image.alt = attachment.name;
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'v2-home-attachment-remove';
            remove.setAttribute('aria-label', `${t('removeImage')}: ${attachment.name}`);
            remove.title = t('removeImage');
            remove.innerHTML = '<i class="fas fa-times"></i>';
            remove.addEventListener('click', () => {
                homeImageAttachments = homeImageAttachments.filter((candidate) => candidate.id !== attachment.id);
                renderHomeImageAttachments();
                void persistHomeActiveAttachments();
                document.getElementById('v2HomePrompt')?.dispatchEvent(new Event('input', { bubbles: true }));
            });
            item.append(image, remove);
            host.appendChild(item);
        });
        homeWsiAttachments.forEach((attachment) => {
            const item = document.createElement('div');
            item.className = 'v2-home-wsi-attachment';
            item.title = attachment.name;

            const icon = document.createElement('span');
            icon.className = 'v2-home-wsi-icon';
            icon.innerHTML = '<i class="far fa-file" aria-hidden="true"></i>';

            const copy = document.createElement('span');
            copy.className = 'v2-home-wsi-copy';
            const name = document.createElement('strong');
            name.textContent = attachment.name;
            const meta = document.createElement('small');
            meta.textContent = `${homeFileExtension(attachment.file).replace('.', '').toUpperCase()} · ${formatHomeFileSize(attachment.size)}`;
            copy.append(name, meta);

            const remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'v2-home-attachment-remove v2-home-wsi-remove';
            remove.setAttribute('aria-label', `${t('removeWsi')}: ${attachment.name}`);
            remove.title = t('removeWsi');
            remove.innerHTML = '<i class="fas fa-times"></i>';
            remove.addEventListener('click', () => {
                homeWsiAttachments = homeWsiAttachments.filter((candidate) => candidate.id !== attachment.id);
                renderHomeImageAttachments();
                document.getElementById('v2HomePrompt')?.dispatchEvent(new Event('input', { bubbles: true }));
            });
            item.append(icon, copy, remove);
            host.appendChild(item);
        });
    }

    function formatHomeFileSize(bytes) {
        const value = Number(bytes) || 0;
        if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(value >= 10 * 1024 ** 3 ? 0 : 1)} GB`;
        if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(value >= 10 * 1024 ** 2 ? 0 : 1)} MB`;
        if (value >= 1024) return `${Math.round(value / 1024)} KB`;
        return `${value} B`;
    }

    function addHomeWsiFiles(files) {
        const candidates = Array.from(files || []).filter(isWsiFile);
        let skippedForLimit = false;
        candidates.forEach((file) => {
            if (homeWsiAttachments.length >= HOME_WSI_LIMIT) {
                skippedForLimit = true;
                return;
            }
            const duplicate = homeWsiAttachments.some((item) => (
                item.name === file.name && item.size === file.size && item.lastModified === file.lastModified
            ));
            if (duplicate) return;
            homeWsiAttachments.push({
                id: homeClientId(),
                name: String(file.name || 'slide'),
                size: Number(file.size) || 0,
                lastModified: Number(file.lastModified) || 0,
                file
            });
        });
        if (skippedForLimit) notifyHomeAttachment(t('wsiLimit'));
        renderHomeImageAttachments();
        document.getElementById('v2HomePrompt')?.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function stageHomeFiles(files) {
        const selected = Array.from(files || []);
        const wsiFiles = selected.filter(isWsiFile);
        const imageFiles = selected.filter((file) => !isWsiFile(file) && String(file.type || '').startsWith('image/'));
        if (wsiFiles.length) addHomeWsiFiles(wsiFiles);
        if (imageFiles.length) void addHomeImageFiles(imageFiles);
        if (!wsiFiles.length && !imageFiles.length && selected.length) notifyHomeAttachment(t('imageUnsupported'));
    }

    async function addHomeImageFiles(files) {
        const candidates = Array.from(files || []);
        for (const file of candidates) {
            if (homeImageAttachments.length >= HOME_IMAGE_LIMIT) {
                notifyHomeAttachment(t('imageLimit'));
                break;
            }
            if (isWsiFile(file) || !HOME_IMAGE_MIME_TYPES.has(String(file.type || '').toLowerCase())) {
                notifyHomeAttachment(t('imageUnsupported'));
                continue;
            }
            if ((Number(file.size) || 0) > HOME_IMAGE_MAX_BYTES) {
                notifyHomeAttachment(t('imageTooLarge'));
                continue;
            }
            try {
                homeImageAttachments.push(await readHomeImageFile(file));
            } catch (error) {
                notifyHomeAttachment(String(error?.message || t('imageUnsupported')));
            }
        }
        renderHomeImageAttachments();
        document.getElementById('v2HomePrompt')?.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function applyTranslations() {
        document.querySelectorAll('[data-v2-i18n]').forEach((element) => {
            const key = element.getAttribute('data-v2-i18n');
            if (key && copy[language()][key]) {
                element.textContent = copy[language()][key];
            }
        });

        document.querySelectorAll('[data-v2-i18n-placeholder]').forEach((element) => {
            const key = element.getAttribute('data-v2-i18n-placeholder');
            if (key && copy[language()][key]) {
                element.setAttribute('placeholder', copy[language()][key]);
            }
        });

        // Dynamic workbench controls are created or mutated after the
        // template is translated.  Refresh their visible and accessible copy
        // on every language change instead of leaving a half-translated shell.
        const sidebarOpen = document.getElementById('v2WorkbenchShell')
            ?.classList.contains('v2-workbench-sidebar-open');
        setWorkbenchSidebarOpen(Boolean(sidebarOpen));
        const app = window.pathchat;
        const statusClass = document.querySelector('#statusIndicator .status-dot')?.className || '';
        const status = statusClass.includes('processing')
            ? 'processing'
            : (statusClass.includes('error') ? 'error' : 'ready');
        app?.updateStatus?.(status);
        const agent = window.pathChatAgent;
        agent?.updateButtons?.();
        agent?._setEvidencePaneMode?.(agent.activeAnalysisStrategy || 'agent_swarm');

        const routeBack = document.getElementById('routeBackBtn');
        if (routeBack) {
            const label = t('hideSubagentResults');
            routeBack.title = label;
            routeBack.setAttribute('aria-label', label);
        }
        const chatMessages = document.getElementById('chatMessages');
        if (chatMessages) {
            chatMessages.dataset.emptyHint = language() === 'en'
                ? 'Ask a question, or select a region on the slide to continue.'
                : '可以直接提问，或在切片上框选区域继续分析';
        }

        const home = document.getElementById('v2HomeShell');
        home?.setAttribute('aria-label', t('homeRegion'));
        const homeSidebarToggle = document.getElementById('v2HomeSidebarToggle');
        if (homeSidebarToggle) {
            const collapsed = home?.classList.contains('is-sidebar-collapsed');
            const label = t(collapsed ? 'expandSidebar' : 'collapseSidebar');
            homeSidebarToggle.title = label;
            homeSidebarToggle.setAttribute('aria-label', label);
        }
        document.querySelector('.v2-home-primary-nav')?.setAttribute(
            'aria-label', t('primaryActions')
        );
        const attributeCopy = [
            ['v2HomeRefreshThreads', 'refreshRecents'],
            ['v2HomeAttach', 'attachImage'],
            ['v2HomeSubmit', 'submitAnalysis'],
        ];
        attributeCopy.forEach(([id, key]) => {
            const element = document.getElementById(id);
            if (!element) return;
            element.title = t(key);
            element.setAttribute('aria-label', t(key));
        });
    }

    function homeClientId() {
        return window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

    async function homeFetchJson(url, options = {}) {
        const response = await fetch(url, {
            ...options,
            headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }
        });
        let result = {};
        try { result = await response.json(); } catch (_) {}
        if (!response.ok || result.success === false) {
            throw new Error(result.error || `HTTP ${response.status}`);
        }
        return result;
    }

    function setHomeConversationTitle(title = '') {
        const element = document.getElementById('v2HomeConversationTitle');
        if (!element) return;
        const value = String(title || '').trim();
        element.textContent = value || t('newConversation');
        element.title = value;
        if (value) element.removeAttribute('data-v2-i18n');
        else element.setAttribute('data-v2-i18n', 'newConversation');
    }

    function formatHomeThreadTime(value) {
        const date = new Date(value || 0);
        if (!Number.isFinite(date.getTime())) return '';
        return new Intl.DateTimeFormat(language() === 'zh' ? 'zh-CN' : 'en', {
            month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'
        }).format(date);
    }

    function homeThreadSummary(thread) {
        return {
            id: String(thread?.id || ''),
            title: String(thread?.title || t('newConversation')),
            created_at: thread?.created_at,
            updated_at: thread?.updated_at,
            message_count: Number(thread?.message_count ?? thread?.messages?.length ?? 0),
            manual_title: Boolean(thread?.manual_title),
            auto_title: Boolean(thread?.auto_title)
        };
    }

    function rememberHomeThread(thread) {
        const summary = homeThreadSummary(thread);
        if (!summary.id) return;
        homeThreads = [summary, ...homeThreads.filter((item) => item.id !== summary.id)]
            .sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0));
        renderHomeRecents();
    }

    function closeHomeThreadMenu() {
        document.getElementById('v2HomeThreadMenu')?.remove();
    }

    function beginHomeThreadRename(thread) {
        closeHomeThreadMenu();
        document.querySelector('.v2-home-rename-backdrop')?.remove();

        const backdrop = document.createElement('div');
        backdrop.className = 'v2-home-rename-backdrop';
        const form = document.createElement('form');
        form.className = 'v2-home-rename-dialog';
        form.setAttribute('role', 'dialog');
        form.setAttribute('aria-modal', 'true');
        form.setAttribute('aria-label', t('renameConversation'));

        const heading = document.createElement('h3');
        heading.textContent = t('renameConversation');
        const input = document.createElement('input');
        input.value = thread.title;
        input.maxLength = 100;
        input.setAttribute('aria-label', t('renameConversation'));
        const actions = document.createElement('div');
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'is-secondary';
        cancel.textContent = t('cancel');
        const save = document.createElement('button');
        save.type = 'submit';
        save.className = 'is-primary';
        save.textContent = t('save');
        actions.append(cancel, save);
        form.append(heading, input, actions);
        backdrop.appendChild(form);
        document.body.appendChild(backdrop);

        const close = () => backdrop.remove();
        cancel.addEventListener('click', close);
        backdrop.addEventListener('pointerdown', (event) => {
            if (event.target === backdrop) close();
        });
        form.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                event.preventDefault();
                close();
            }
        });
        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            const value = input.value.trim();
            if (!value || value === thread.title) {
                close();
                return;
            }
            save.disabled = true;
            try {
                const result = await homeFetchJson(`/api/chat/threads/${encodeURIComponent(thread.id)}`, {
                    method: 'PATCH', body: JSON.stringify({ title: value })
                });
                rememberHomeThread(result.thread);
                if (thread.id === homeThreadId) setHomeConversationTitle(result.thread.title);
                close();
            } catch (error) {
                save.disabled = false;
                notifyHomeAttachment(String(error?.message || t('homeChatFailed')));
            }
        });
        window.requestAnimationFrame(() => {
            input.focus();
            input.select();
        });
    }

    function openHomeThreadMenu(thread, anchor, pointerEvent = null) {
        closeHomeThreadMenu();
        const menu = document.createElement('div');
        menu.id = 'v2HomeThreadMenu';
        menu.className = 'v2-home-thread-menu';
        menu.setAttribute('role', 'menu');
        menu.setAttribute('aria-label', t('threadActions'));

        const addItem = (icon, label, action) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.setAttribute('role', 'menuitem');
            button.innerHTML = `<i class="${icon}" aria-hidden="true"></i><span></span>`;
            button.querySelector('span').textContent = label;
            button.addEventListener('click', () => {
                closeHomeThreadMenu();
                action();
            });
            menu.appendChild(button);
            return button;
        };

        addItem('far fa-folder-open', t('openConversation'), () => void loadHomeThread(thread.id));
        addItem('far fa-edit', t('renameConversation'), () => beginHomeThreadRename(thread));
        addItem('far fa-copy', t('copyChatTitle'), () => void navigator.clipboard?.writeText(thread.title));
        const separator = document.createElement('span');
        separator.className = 'v2-home-thread-menu-separator';
        menu.appendChild(separator);
        addItem('far fa-plus-square', t('newConversation'), () => document.getElementById('v2HomeNewTask')?.click());

        document.body.appendChild(menu);
        const anchorRect = anchor.getBoundingClientRect();
        const width = 188;
        const x = pointerEvent?.clientX ?? anchorRect.right;
        const y = pointerEvent?.clientY ?? anchorRect.bottom;
        menu.style.left = `${Math.max(8, Math.min(window.innerWidth - width - 8, x - width))}px`;
        menu.style.top = `${Math.max(8, Math.min(window.innerHeight - menu.offsetHeight - 8, y + 4))}px`;
        window.requestAnimationFrame(() => menu.querySelector('button')?.focus());
        menu.addEventListener('pointerdown', (event) => event.stopPropagation());
        document.addEventListener('pointerdown', closeHomeThreadMenu, { once: true });
    }

    function projectAffinityKey(filename) {
        const name = String(filename || '').trim();
        if (!name) return '';
        const accession = name.match(/^(\d{4,})[-_]/);
        if (accession) return `accession:${accession[1]}`;
        const tcga = name.match(/^(TCGA-[A-Z0-9]+-[A-Z0-9]+)/i);
        if (tcga) return `tcga:${tcga[1].toUpperCase()}`;
        return '';
    }

    function sortHomeFiles(files) {
        return [...files].sort((a, b) => String(b.upload_time || '').localeCompare(String(a.upload_time || '')));
    }

    function makeHomeProject(members, kind, key) {
        const files = sortHomeFiles(members);
        const lead = files[0] || {};
        return {
            id: key,
            kind,
            title: lead.filename || (language() === 'en' ? 'Untitled slide' : '未命名切片'),
            slide_count: files.length,
            updated_at: lead.upload_time || '',
            thumbnail_url: lead.thumbnail_url || '',
            files,
            lead_filename: lead.filename || ''
        };
    }

    function groupHomeCaseProjects(files) {
        const list = Array.isArray(files) ? files.filter((file) => file && file.file_id) : [];
        const used = new Set();
        const projects = [];
        const byBatch = new Map();
        list.forEach((file) => {
            const batch = String(file.batch_id || '').trim();
            if (!batch) return;
            if (!byBatch.has(batch)) byBatch.set(batch, []);
            byBatch.get(batch).push(file);
        });
        byBatch.forEach((members, batch) => {
            if (members.length < 2) return;
            projects.push(makeHomeProject(members, 'patient', `batch:${batch}`));
            members.forEach((file) => used.add(file.file_id));
        });
        const remaining = list.filter((file) => !used.has(file.file_id));
        const byAffinity = new Map();
        const singles = [];
        remaining.forEach((file) => {
            const key = projectAffinityKey(file.filename);
            if (!key) {
                singles.push(file);
                return;
            }
            if (!byAffinity.has(key)) byAffinity.set(key, []);
            byAffinity.get(key).push(file);
        });
        byAffinity.forEach((members, key) => {
            if (members.length >= 2) projects.push(makeHomeProject(members, 'patient', key));
            else singles.push(...members);
        });
        singles.forEach((file) => {
            projects.push(makeHomeProject([file], 'slide', `slide:${file.file_id}`));
        });
        return projects.sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
    }

    function homeProjectFolderName(project) {
        const lead = project?.files?.[0]?.filename || project?.title || 'Patient';
        const accession = String(lead).match(/^(\d{4,})[-_]/);
        if (accession) return accession[1];
        const tcga = String(lead).match(/^(TCGA-[A-Z0-9]+-[A-Z0-9]+)/i);
        if (tcga) return tcga[1].toUpperCase();
        return String(project?.title || 'Patient');
    }

    function patientLevelMark(count) {
        const value = copy[language()].patientLevelMark;
        return typeof value === 'function' ? value(count) : `... *${count} slides (patient level)`;
    }

    function recentsTimestamp(value) {
        const time = Date.parse(value || '');
        return Number.isFinite(time) ? time : 0;
    }

    function renderHomeThreadRow(thread) {
        const row = document.createElement('div');
        row.className = `v2-home-thread-row${thread.id === homeThreadId ? ' is-active' : ''}`;
        row.dataset.threadId = thread.id;

        const open = document.createElement('button');
        open.type = 'button';
        open.className = 'v2-home-thread-open';
        open.dataset.testid = `chat-thread-${thread.id}`;
        open.innerHTML = '<i class="far fa-comment-alt" aria-hidden="true"></i>';
        const copyEl = document.createElement('span');
        copyEl.className = 'v2-home-thread-copy';
        const title = document.createElement('strong');
        title.textContent = thread.title;
        copyEl.append(title);
        open.appendChild(copyEl);
        open.addEventListener('click', () => void loadHomeThread(thread.id));

        const actions = document.createElement('button');
        actions.type = 'button';
        actions.className = 'v2-home-thread-rename';
        actions.title = t('threadActions');
        actions.setAttribute('aria-label', `${t('threadActions')}：${thread.title}`);
        actions.innerHTML = '<i class="fas fa-ellipsis-h" aria-hidden="true"></i>';
        actions.addEventListener('click', (event) => {
            event.stopPropagation();
            openHomeThreadMenu(thread, actions);
        });
        row.addEventListener('contextmenu', (event) => {
            event.preventDefault();
            openHomeThreadMenu(thread, actions, event);
        });
        row.append(open, actions);
        return row;
    }

    function renderHomeProjectThumb(project) {
        const thumb = document.createElement('span');
        thumb.className = 'v2-home-case-thumb';
        if (project.thumbnail_url) {
            const image = document.createElement('img');
            image.src = project.thumbnail_url;
            image.alt = '';
            image.addEventListener('error', () => {
                thumb.replaceChildren();
                thumb.innerHTML = '<i class="fas fa-microscope"></i>';
            }, { once: true });
            thumb.appendChild(image);
        } else {
            thumb.innerHTML = '<i class="fas fa-microscope"></i>';
        }
        return thumb;
    }

    function openHomeProject(project) {
        const files = Array.isArray(project?.files) ? project.files : [];
        const isPatient = project?.kind === 'patient' || files.length > 1;
        if (isPatient && files.length > 1) {
            enterWorkbench({ keepSidebar: true });
            setWorkbenchSidebarOpen(true);
            if (typeof window.pathchat?.openPatientLevelFromExistingFiles === 'function') {
                return window.pathchat.openPatientLevelFromExistingFiles(
                    files,
                    homeProjectFolderName(project)
                );
            }
            console.warn('[PathCopilot V2] Patient Level opener missing; staying on the case rail.');
            return undefined;
        }
        const fileId = files[0]?.file_id;
        if (fileId && window.pathchat && typeof window.pathchat.loadWSI === 'function') {
            window.pathchat.loadWSI(fileId);
        }
        return undefined;
    }

    function renderHomeProjectRow(project) {
        const currentId = window.pathchat?.currentFile?.file_id || '';
        const isActive = (project.files || []).some((file) => file.file_id === currentId);
        const row = document.createElement('button');
        row.type = 'button';
        row.className = `v2-home-case-row${isActive ? ' is-active' : ''}`;
        row.dataset.projectId = project.id || '';
        row.title = project.title || '';

        const details = document.createElement('span');
        details.className = 'v2-home-case-copy';
        const name = document.createElement('strong');
        name.textContent = project.title || (language() === 'en' ? 'Untitled slide' : '未命名切片');
        const meta = document.createElement('small');
        if (project.kind === 'patient' && Number(project.slide_count) > 1) {
            meta.className = 'v2-home-patient-mark';
            meta.textContent = patientLevelMark(project.slide_count);
        } else {
            meta.textContent = formatCaseMeta(project.files?.[0] || {}) || (language() === 'en' ? 'Local WSI' : '本地 WSI');
        }
        details.append(name, meta);

        const arrow = document.createElement('i');
        arrow.className = 'fas fa-chevron-right';
        row.append(renderHomeProjectThumb(project), details, arrow);
        row.addEventListener('click', () => openHomeProject(project));
        return row;
    }

    function renderHomeRecents() {
        const list = document.getElementById('v2HomeThreadList');
        if (!list) return;
        if (!homeThreadsLoaded && !homeCasesLoaded) return;

        const items = [];
        homeThreads.forEach((thread) => {
            items.push({ kind: 'thread', at: recentsTimestamp(thread.updated_at), thread });
        });
        homeProjects.forEach((project) => {
            items.push({ kind: 'project', at: recentsTimestamp(project.updated_at), project });
        });
        items.sort((a, b) => b.at - a.at);

        list.replaceChildren();
        if (!items.length) {
            const empty = document.createElement('div');
            empty.className = 'v2-home-thread-empty';
            empty.textContent = (homeThreadsLoaded && homeCasesLoaded) ? t('noRecents') : t('loadingRecents');
            list.appendChild(empty);
            return;
        }
        items.slice(0, 50).forEach((item) => {
            list.appendChild(
                item.kind === 'thread'
                    ? renderHomeThreadRow(item.thread)
                    : renderHomeProjectRow(item.project)
            );
        });
    }

    function renderHomeThreads() {
        renderHomeRecents();
    }

    async function refreshHomeThreads({ restore = false } = {}) {
        const refresh = document.getElementById('v2HomeRefreshThreads');
        if (refresh) refresh.classList.add('is-loading');
        try {
            const result = await homeFetchJson('/api/chat/threads?limit=50', { cache: 'no-store' });
            homeThreads = Array.isArray(result.threads) ? result.threads.map(homeThreadSummary) : [];
            homeThreadsLoaded = true;
            renderHomeRecents();
            if (restore && !homeThreadId) {
                let remembered = '';
                try { remembered = localStorage.getItem('pathcopilot.activeChatThread') || ''; } catch (_) {}
                if (remembered && homeThreads.some((thread) => thread.id === remembered)) {
                    await loadHomeThread(remembered, { focus: false });
                }
            }
        } catch (error) {
            console.warn('[PathCopilot V2] Failed to load chat threads:', error);
            homeThreadsLoaded = true;
            if (!homeCasesLoaded) {
                const list = document.getElementById('v2HomeThreadList');
                if (list) list.innerHTML = `<div class="v2-home-thread-empty">${t('homeChatFailed')}</div>`;
            } else {
                renderHomeRecents();
            }
        } finally {
            if (refresh) refresh.classList.remove('is-loading');
        }
    }

    function scheduleHomeThreadsRefresh(delay = 120) {
        window.clearTimeout(homeThreadsRefreshTimer);
        homeThreadsRefreshTimer = window.setTimeout(() => refreshHomeThreads(), delay);
    }

    function mergeHomeActiveAttachments(thread) {
        // Attachments belong to the message that sent them. Never repopulate
        // the composer from an earlier turn when a thread is reopened.
        homeImageAttachments = [];
        homeWsiAttachments = [];
        renderHomeImageAttachments();
    }

    function renderStoredAssistantMessage(message) {
        const pending = appendHomeChatMessage('assistant', message.content || '', false);
        if (!pending || !message.web_search) return;
        const sources = citedSourcesForContent(message.content || '', message.sources);
        const research = createHomeWebSearchView(pending);
        research.activity.classList.remove('is-running');
        research.activity.classList.add('is-complete');
        research.status.textContent = `${language() === 'zh' ? '联网检索完成' : t('webComplete')} · ${sources.length} ${language() === 'zh' ? '个来源' : t('webSources')}`;
        research.answer.hidden = false;
        renderHomeAssistantContent(research.answer, message.content || '', sources);
        renderHomeWebSources(research, sources);
    }

    function homeHistoryContent(message) {
        const content = String(message?.content || '');
        const sources = citedSourcesForContent(content, message?.sources).slice(0, 12);
        if (!sources.length) return content;
        const evidence = sources.map((source) => (
            `[${source.id || 'S'}] ${source.title || source.journal || source.provider || ''}`
            + `${source.year ? ` (${source.year})` : ''}`
        )).join('\n');
        return `${content}\n\nPrior cited sources:\n${evidence}`;
    }

    function citedSourcesForContent(content, sources) {
        const sourceMap = new Map(
            (Array.isArray(sources) ? sources : [])
                .filter((source) => source && source.id)
                .map((source) => [String(source.id).toUpperCase(), source])
        );
        const cited = [];
        const seen = new Set();
        const pattern = /\[(S\d+)\]/gi;
        let match;
        while ((match = pattern.exec(String(content || ''))) !== null) {
            const sourceId = match[1].toUpperCase();
            if (seen.has(sourceId) || !sourceMap.has(sourceId)) continue;
            seen.add(sourceId);
            cited.push(sourceMap.get(sourceId));
        }
        return cited;
    }

    function renderLoadedHomeThread(thread) {
        const chat = ensureHomeChat();
        if (chat) chat.replaceChildren();
        homeChatHistory = [];
        (thread?.messages || []).forEach((message) => {
            const role = message.role === 'assistant' ? 'assistant' : 'user';
            homeChatHistory.push({
                role,
                content: String(message.content || ''),
                modelContent: role === 'assistant' ? homeHistoryContent(message) : String(message.content || '')
            });
            if (role === 'user') {
                const attachments = (message.attachments || []).map((item) => ({
                    ...item, previewUrl: item.url || '', stored: true
                }));
                appendHomeChatMessage('user', message.content || '', false, attachments);
            } else {
                renderStoredAssistantMessage(message);
            }
        });
        const hasMessages = Boolean(thread?.messages?.length);
        document.getElementById('v2HomeMain')?.classList.toggle('is-chatting', hasMessages);
        if (chat) chat.hidden = !hasMessages;
        mergeHomeActiveAttachments(thread);
    }

    async function loadHomeThread(threadId, options = {}) {
        if (!threadId || homeChatBusy) return;
        const result = await homeFetchJson(`/api/chat/threads/${encodeURIComponent(threadId)}`, { cache: 'no-store' });
        homeThreadId = result.thread.id;
        try { localStorage.setItem('pathcopilot.activeChatThread', homeThreadId); } catch (_) {}
        setHomeConversationTitle(result.thread.title);
        renderLoadedHomeThread(result.thread);
        rememberHomeThread(result.thread);
        showHome({ focus: options.focus !== false });
    }

    async function ensureHomeThread(initialMessage) {
        if (homeThreadId) return homeThreadId;
        const result = await homeFetchJson('/api/chat/threads', {
            method: 'POST',
            body: JSON.stringify({ title: initialMessage, lang: language() })
        });
        homeThreadId = result.thread.id;
        try { localStorage.setItem('pathcopilot.activeChatThread', homeThreadId); } catch (_) {}
        setHomeConversationTitle(result.thread.title);
        rememberHomeThread(result.thread);
        return homeThreadId;
    }

    function dataUrlFromBlob(blob) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onerror = () => reject(new Error(t('imageUnsupported')));
            reader.onload = () => resolve(String(reader.result || ''));
            reader.readAsDataURL(blob);
        });
    }

    async function hydrateHomeAttachment(attachment) {
        if (attachment.data) return { ...attachment };
        const source = attachment.url || attachment.previewUrl;
        if (!source) throw new Error(t('imageUnsupported'));
        const response = await fetch(source, { cache: 'no-store' });
        if (!response.ok) throw new Error(t('imageUnsupported'));
        const dataUrl = await dataUrlFromBlob(await response.blob());
        const comma = dataUrl.indexOf(',');
        if (comma < 0) throw new Error(t('imageUnsupported'));
        return { ...attachment, data: dataUrl.slice(comma + 1), previewUrl: source };
    }

    async function persistHomeThreadMessage(role, content, attachments = [], options = {}) {
        if (!homeThreadId) return null;
        const newImages = attachments
            .filter((item) => !item.stored && item.data)
            .map(({ id, data, mime, name }) => ({ id, data, mime, name }));
        const attachmentIds = attachments
            .filter((item) => item.stored || item.url)
            .map((item) => item.id);
        const result = await homeFetchJson(`/api/chat/threads/${encodeURIComponent(homeThreadId)}/messages`, {
            method: 'POST',
            body: JSON.stringify({
                role,
                content,
                client_message_id: options.clientMessageId || homeClientId(),
                images: newImages,
                attachment_ids: attachmentIds,
                sources: options.sources || [],
                web_search: Boolean(options.webSearch)
            })
        });
        rememberHomeThread(result.thread);
        setHomeConversationTitle(result.thread.title);
        return result;
    }

    async function persistHomeActiveAttachments(attachmentIds = homeImageAttachments.map((item) => item.id)) {
        if (!homeThreadId) return;
        try {
            const result = await homeFetchJson(`/api/chat/threads/${encodeURIComponent(homeThreadId)}`, {
                method: 'PATCH',
                body: JSON.stringify({ active_attachment_ids: attachmentIds })
            });
            rememberHomeThread(result.thread);
        } catch (error) {
            console.warn('[PathCopilot V2] Failed to update active image context:', error);
        }
    }

    function ensureHomeChat() {
        const stage = document.querySelector('#v2HomeMain .v2-home-stage');
        if (!stage) return null;
        let chat = document.getElementById('v2HomeChat');
        if (!chat) {
            chat = document.createElement('div');
            chat.id = 'v2HomeChat';
            chat.className = 'v2-home-chat';
            chat.setAttribute('aria-live', 'polite');
            chat.hidden = true;
            stage.appendChild(chat);
        }
        return chat;
    }

    function formatHomeAssistantContent(content) {
        const value = String(content || '');
        if (window.pathchat && typeof window.pathchat.formatMessageContent === 'function') {
            return window.pathchat.formatMessageContent(value);
        }
        const holder = document.createElement('div');
        holder.textContent = value;
        return holder.innerHTML.replace(/\n/g, '<br>');
    }

    function compactCitationTitle(source) {
        const raw = String(source?.title || source?.journal || source?.provider || source?.id || 'Source');
        const clean = raw.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();
        const characters = Array.from(clean);
        return characters.length > 48 ? `${characters.slice(0, 47).join('')}…` : clean;
    }

    function configureResearchSourceLink(link, url) {
        const desktop = window.pathcopilotDesktop;
        if (desktop?.isDesktop && typeof desktop.openResearchBrowser === 'function') {
            link.removeAttribute('target');
            link.rel = 'noopener noreferrer';
            link.addEventListener('click', (event) => {
                event.preventDefault();
                void desktop.openResearchBrowser(url);
            });
            return;
        }
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
    }

    function createInlineCitation(source) {
        const url = safeWebSourceUrl(source?.url);
        if (!url) return null;
        const fullTitle = String(source?.title || source?.journal || source?.provider || source?.id || url)
            .replace(/<[^>]*>/g, '')
            .replace(/\s+/g, ' ')
            .trim();
        const link = document.createElement('a');
        link.className = 'v2-inline-citation';
        link.href = url;
        configureResearchSourceLink(link, url);
        link.title = fullTitle;
        link.setAttribute('aria-label', `${language() === 'zh' ? '来源' : 'Source'}：${fullTitle}`);

        const icon = document.createElement('i');
        icon.className = 'fas fa-link';
        icon.setAttribute('aria-hidden', 'true');
        const title = document.createElement('span');
        title.textContent = compactCitationTitle(source);
        link.append(icon, title);
        return link;
    }

    function decorateHomeInlineCitations(host, sources) {
        if (!host || !Array.isArray(sources) || !sources.length) return;
        const sourceMap = new Map(
            sources
                .filter((source) => source && source.id && safeWebSourceUrl(source.url))
                .map((source) => [String(source.id).toUpperCase(), source])
        );
        if (!sourceMap.size) return;

        const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
                const parent = node.parentElement;
                if (!parent || parent.closest('a, code, pre, script, style')) {
                    return NodeFilter.FILTER_REJECT;
                }
                return /\[S\d+\]/i.test(node.nodeValue || '')
                    ? NodeFilter.FILTER_ACCEPT
                    : NodeFilter.FILTER_REJECT;
            }
        });
        const textNodes = [];
        while (walker.nextNode()) textNodes.push(walker.currentNode);

        textNodes.forEach((node) => {
            const text = node.nodeValue || '';
            const pattern = /\[(S\d+)\]/gi;
            const fragment = document.createDocumentFragment();
            let cursor = 0;
            let match;
            while ((match = pattern.exec(text)) !== null) {
                if (match.index > cursor) fragment.append(document.createTextNode(text.slice(cursor, match.index)));
                const source = sourceMap.get(match[1].toUpperCase());
                const citation = source ? createInlineCitation(source) : null;
                fragment.append(citation || document.createTextNode(match[0]));
                cursor = pattern.lastIndex;
            }
            if (cursor < text.length) fragment.append(document.createTextNode(text.slice(cursor)));
            node.replaceWith(fragment);
        });
    }

    function renderHomeAssistantContent(host, content, sources = []) {
        if (!host) return;
        host.innerHTML = formatHomeAssistantContent(content);
        decorateHomeInlineCitations(host, sources);
        if (host.closest('#v2HomeChat')) scheduleHomeTurnRail();
    }

    function homeTurnPreviewText(row) {
        return String(row?.textContent || '').replace(/\s+/g, ' ').trim();
    }

    function updateHomeTurnRailActive() {
        const chat = document.getElementById('v2HomeChat');
        const track = document.getElementById('v2HomeTurnTrack');
        if (!chat || !track) return;
        const buttons = Array.from(track.querySelectorAll('.v2-home-turn-button'));
        if (!buttons.length) return;
        const marker = chat.getBoundingClientRect().top + Math.min(180, chat.clientHeight * 0.32);
        let active = buttons[0];
        buttons.forEach((button) => {
            const row = document.getElementById(button.dataset.turnTarget || '');
            if (row && row.getBoundingClientRect().top <= marker) active = button;
        });
        buttons.forEach((button) => {
            const selected = button === active;
            button.classList.toggle('is-active', selected);
            button.setAttribute('aria-current', selected ? 'true' : 'false');
        });
    }

    function renderHomeTurnRail() {
        homeTurnRailFrame = 0;
        const rail = document.getElementById('v2HomeTurnRail');
        const track = document.getElementById('v2HomeTurnTrack');
        const chat = document.getElementById('v2HomeChat');
        if (!rail || !track || !chat) return;
        const userRows = Array.from(chat.querySelectorAll('.v2-home-chat-message.is-user'));
        track.replaceChildren();
        rail.hidden = userRows.length < 2;
        if (rail.hidden) return;

        userRows.forEach((row, index) => {
            const targetId = `v2-home-turn-${index + 1}`;
            row.id = targetId;
            const answerRow = row.nextElementSibling;
            const question = (homeTurnPreviewText(row) || (language() === 'zh' ? `第 ${index + 1} 轮` : `Turn ${index + 1}`)).slice(0, 140);
            const answer = answerRow?.classList.contains('is-assistant')
                ? (answerRow.classList.contains('is-temporary')
                    ? (language() === 'zh' ? '正在生成回复…' : 'Generating reply…')
                    : homeTurnPreviewText(answerRow).slice(0, 260))
                : (language() === 'zh' ? '该轮未记录独立回复' : 'No separate reply was recorded for this turn');

            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'v2-home-turn-button';
            button.dataset.turnTarget = targetId;
            button.setAttribute('aria-label', `${language() === 'zh' ? '跳转到' : 'Jump to'} ${question.slice(0, 48)}`);
            button.innerHTML = '<span class="v2-home-turn-tick" aria-hidden="true"></span>';

            const preview = document.createElement('span');
            preview.className = 'v2-home-turn-preview';
            const title = document.createElement('strong');
            title.textContent = question;
            const detail = document.createElement('small');
            detail.textContent = answer;
            preview.append(title, detail);
            button.appendChild(preview);
            button.addEventListener('click', () => {
                row.scrollIntoView({ behavior: 'smooth', block: 'start' });
                buttonsSetActive(button);
            });
            track.appendChild(button);
        });
        updateHomeTurnRailActive();
    }

    function buttonsSetActive(activeButton) {
        const track = document.getElementById('v2HomeTurnTrack');
        track?.querySelectorAll('.v2-home-turn-button').forEach((button) => {
            const selected = button === activeButton;
            button.classList.toggle('is-active', selected);
            button.setAttribute('aria-current', selected ? 'true' : 'false');
        });
    }

    function scheduleHomeTurnRail() {
        if (homeTurnRailFrame) return;
        homeTurnRailFrame = window.requestAnimationFrame(renderHomeTurnRail);
    }

    function appendHomeChatMessage(role, content, temporary = false, attachments = []) {
        const chat = ensureHomeChat();
        const homeMain = document.getElementById('v2HomeMain');
        if (!chat || !homeMain) return null;

        homeMain.classList.add('is-chatting');
        chat.hidden = false;

        const row = document.createElement('article');
        row.className = `v2-home-chat-message is-${role}${temporary ? ' is-temporary' : ''}`;

        if (role === 'assistant') {
            const avatar = document.createElement('span');
            avatar.className = 'v2-home-chat-avatar';
            avatar.innerHTML = '<i class="fas fa-microscope"></i>';
            row.appendChild(avatar);
        }

        const bubble = document.createElement('div');
        bubble.className = 'v2-home-chat-bubble';
        if (role === 'user') {
            const body = document.createElement('div');
            body.className = 'v2-home-chat-user-content';
            if (attachments.length) {
                const images = document.createElement('div');
                images.className = 'v2-home-chat-user-images';
                attachments.forEach((attachment) => {
                    const image = document.createElement('img');
                    image.src = attachment.previewUrl || attachment.url || `data:${attachment.mime};base64,${attachment.data}`;
                    image.alt = attachment.name || t('attachImage');
                    images.appendChild(image);
                });
                body.appendChild(images);
            }
            const text = document.createElement('span');
            text.textContent = String(content || '');
            body.appendChild(text);
            bubble.appendChild(body);
        } else {
            renderHomeAssistantContent(bubble, content);
        }
        row.appendChild(bubble);
        chat.appendChild(row);
        chat.scrollTop = chat.scrollHeight;
        scheduleHomeTurnRail();
        return { row, bubble };
    }

    function safeWebSourceUrl(value) {
        try {
            const url = new URL(String(value || ''));
            return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
        } catch (_) {
            return '';
        }
    }

    function createHomeWebSearchView(pending) {
        if (!pending) return null;
        pending.row.classList.add('is-web-search');
        pending.bubble.replaceChildren();

        const activity = document.createElement('div');
        activity.className = 'v2-web-search-activity is-running';
        const icon = document.createElement('i');
        icon.className = 'fas fa-globe-americas';
        const status = document.createElement('span');
        status.className = 'v2-web-search-status';
        status.textContent = t('webSearching');
        activity.append(icon, status);

        const query = document.createElement('div');
        query.className = 'v2-web-search-query';
        query.hidden = true;

        const answer = document.createElement('div');
        answer.className = 'v2-web-search-answer';
        answer.hidden = true;

        const sourceHost = document.createElement('div');
        sourceHost.className = 'v2-web-search-source-host';
        pending.bubble.append(activity, query, answer, sourceHost);
        return { activity, status, query, answer, sourceHost };
    }

    function updateHomeWebSearchStatus(view, event) {
        if (!view || !event) return;
        const message = String(event.message || '').trim();
        if (message) view.status.textContent = message;
        if (Array.isArray(event.queries) && event.queries.length) {
            view.query.textContent = event.queries.join(' · ');
            view.query.hidden = false;
        }
    }

    function renderHomeWebSources(view, sources) {
        if (!view) return;
        view.sourceHost.replaceChildren();
        const safeSources = (Array.isArray(sources) ? sources : []).filter((source) => (
            source && safeWebSourceUrl(source.url)
        ));
        if (!safeSources.length) return;

        const details = document.createElement('details');
        details.className = 'v2-web-search-sources';
        const summary = document.createElement('summary');
        summary.textContent = `${t('webSources')} (${safeSources.length})`;
        details.appendChild(summary);

        const list = document.createElement('div');
        list.className = 'v2-web-search-source-list';
        safeSources.forEach((source, index) => {
            const link = document.createElement('a');
            link.className = 'v2-web-search-source';
            link.href = safeWebSourceUrl(source.url);
            configureResearchSourceLink(link, link.href);

            const badge = document.createElement('span');
            badge.className = 'v2-web-search-source-id';
            badge.textContent = String(source.id || `S${index + 1}`);
            const copy = document.createElement('span');
            copy.className = 'v2-web-search-source-copy';
            const title = document.createElement('strong');
            title.textContent = String(source.title || source.domain || link.href);
            const meta = document.createElement('small');
            meta.textContent = [source.provider, source.journal, source.year]
                .map((value) => String(value || '').trim())
                .filter(Boolean)
                .join(' · ') || String(source.domain || '');
            copy.append(title, meta);
            const external = document.createElement('i');
            external.className = 'fas fa-external-link-alt';
            link.append(badge, copy, external);
            list.appendChild(link);
        });
        details.appendChild(list);
        view.sourceHost.appendChild(details);
    }

    async function consumeHomeWebSearchResponse(response, onEvent) {
        if (!response.ok) {
            let error = `HTTP ${response.status}`;
            let noApiKey = false;
            try {
                const body = await response.json();
                error = body.error || error;
                noApiKey = Boolean(body.no_api_key || body.requires_api_key);
            } catch (_) {}
            const failure = new Error(error);
            failure.noApiKey = noApiKey;
            throw failure;
        }
        if (!response.body?.getReader) throw new Error('Streaming response is unavailable');

        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        let finalResult = null;
        const consumeLine = (line) => {
            const value = String(line || '').trim();
            if (!value) return;
            const event = JSON.parse(value);
            if (event.type === 'error') throw new Error(event.error || t('homeChatFailed'));
            onEvent?.(event);
            if (event.type === 'done') finalResult = event;
        };

        while (true) {
            const { value, done } = await reader.read();
            buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
            const lines = buffer.split(/\r?\n/);
            buffer = lines.pop() || '';
            lines.forEach(consumeLine);
            if (done) break;
        }
        if (buffer.trim()) consumeLine(buffer);
        return finalResult || { success: false, error: t('homeChatFailed') };
    }

    function resetHomeChat() {
        homeChatHistory = [];
        homeChatBusy = false;
        homeImageAttachments = [];
        homeWsiAttachments = [];
        homeThreadId = null;
        try { localStorage.removeItem('pathcopilot.activeChatThread'); } catch (_) {}
        setHomeConversationTitle('');
        renderHomeThreads();
        renderHomeImageAttachments();
        const chat = ensureHomeChat();
        if (chat) {
            chat.replaceChildren();
            chat.hidden = true;
        }
        document.getElementById('v2HomeMain')?.classList.remove('is-chatting');
    }

    async function sendHomeChat(message, attachments = []) {
        if (homeChatBusy) return;
        const priorHistory = homeChatHistory.slice(-24).map(({ role, content, modelContent }) => ({
            role, content: modelContent || content
        }));
        const submit = document.getElementById('v2HomeSubmit');
        homeChatBusy = true;
        if (submit) submit.disabled = true;
        let pending = null;
        let research = null;

        try {
            const imageSnapshots = await Promise.all(
                attachments.map((attachment) => hydrateHomeAttachment(attachment))
            );
            const isVisionChat = imageSnapshots.length > 0;
            await ensureHomeThread(message);
            await persistHomeThreadMessage('user', message, imageSnapshots, {
                clientMessageId: homeClientId()
            });
            await persistHomeActiveAttachments([]);
            homeChatHistory.push({ role: 'user', content: message });
            appendHomeChatMessage('user', message, false, imageSnapshots);
            if (
                window.PathCopilotApiKeyGuard
                && !(await window.PathCopilotApiKeyGuard.ensureConfigured())
            ) return;
            pending = appendHomeChatMessage('assistant', t('thinking'), true);
            research = null;

            const response = await fetch('/api/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message,
                    history: priorHistory,
                    thread_id: homeThreadId,
                    file_id: '',
                    analysis_context: '',
                    lang: window.__lang || 'zh',
                    mode: isVisionChat ? 'vision_chat' : 'web_chat',
                    web_search: !isVisionChat,
                    images: imageSnapshots.map(({ id, data, mime, name }) => ({ id, data, mime, name })),
                    stream: true,
                    privacy_scope: 'medical_concepts_only'
                })
            });
            let answerText = '';
            let currentSources = [];
            const result = await consumeHomeWebSearchResponse(response, (event) => {
                if (event.type === 'route') {
                    if (event.web_search === true && !research) {
                        research = createHomeWebSearchView(pending);
                    } else if (event.web_search === false && research) {
                        pending?.row.classList.remove('is-web-search');
                        pending?.bubble.replaceChildren();
                        research = null;
                    }
                } else if (event.type === 'status') {
                    if (event.phase === 'tool_selection') return;
                    if (event.phase === 'inspect_slide') {
                        if (!research && pending?.bubble) {
                            pending.bubble.textContent = event.message || t('webSearching');
                        } else if (research) {
                            updateHomeWebSearchStatus(research, event);
                        }
                        return;
                    }
                    if (!research) research = createHomeWebSearchView(pending);
                    updateHomeWebSearchStatus(research, event);
                } else if (event.type === 'search') {
                    if (!research) research = createHomeWebSearchView(pending);
                    updateHomeWebSearchStatus(research, event);
                } else if (event.type === 'sources') {
                    if (!research) research = createHomeWebSearchView(pending);
                    currentSources = Array.isArray(event.sources) ? event.sources : [];
                    renderHomeWebSources(research, currentSources);
                } else if (event.type === 'delta') {
                    answerText += String(event.content || '');
                    if (pending) pending.row.classList.remove('is-temporary');
                    if (research) {
                        research.answer.hidden = false;
                        renderHomeAssistantContent(research.answer, answerText, currentSources);
                    } else if (pending) {
                        renderHomeAssistantContent(pending.bubble, answerText);
                    }
                }
            });
            if (!result.success) throw new Error(result.error || t('homeChatFailed'));
            if (pending) pending.row.classList.remove('is-temporary');
            const finalReply = result.reply || answerText;
            const finalSources = citedSourcesForContent(
                finalReply,
                result.sources || currentSources
            );
            if (research && result.web_search !== false) {
                research.activity.classList.remove('is-running');
                research.activity.classList.add('is-complete');
                const elapsed = Number(result.elapsed_seconds);
                const answerLang = result.answer_language === 'zh' ? 'zh' : 'en';
                const completeLabel = answerLang === 'zh' ? '联网检索完成' : t('webComplete');
                const sourceLabel = answerLang === 'zh' ? '个来源' : t('webSources');
                research.status.textContent = `${completeLabel} · ${(result.sources || currentSources).length} ${sourceLabel}${Number.isFinite(elapsed) ? ` · ${elapsed}s` : ''}`;
                research.answer.hidden = false;
                renderHomeAssistantContent(research.answer, finalReply, finalSources);
                renderHomeWebSources(research, finalSources);
            } else if (pending) {
                pending.row.classList.remove('is-web-search');
                renderHomeAssistantContent(pending.bubble, finalReply);
            }
            homeChatHistory.push({
                role: 'assistant',
                content: finalReply,
                modelContent: homeHistoryContent({ content: finalReply, sources: finalSources })
            });
            await persistHomeThreadMessage('assistant', finalReply, [], {
                clientMessageId: homeClientId(),
                sources: finalSources,
                webSearch: Boolean(result.web_search)
            });
        } catch (error) {
            console.error('[PathCopilot V2] Home chat failed:', error);
            if (error?.noApiKey) {
                window.PathCopilotApiKeyGuard?.markConfigured(false);
                window.PathCopilotApiKeyGuard?.requireNow();
            }
            if (pending) {
                pending.row.classList.remove('is-temporary');
                pending.row.classList.add('is-error');
                if (research) {
                    research.activity.classList.remove('is-running', 'is-complete');
                    research.activity.classList.add('is-error');
                    research.status.textContent = String(error?.message || t('homeChatFailed'));
                    research.answer.hidden = true;
                } else {
                    pending.bubble.textContent = error?.noApiKey ? '' : t('homeChatFailed');
                }
                if (error?.noApiKey) pending.row.remove();
            }
            if (!error?.noApiKey && window.pathchat && typeof window.pathchat.showNotification === 'function') {
                window.pathchat.showNotification('error', t('homeChatFailed'));
            }
        } finally {
            homeChatBusy = false;
            const prompt = document.getElementById('v2HomePrompt');
            if (submit) submit.disabled = !prompt?.value.trim() && homeImageAttachments.length === 0;
            const chat = ensureHomeChat();
            if (chat) chat.scrollTop = chat.scrollHeight;
        }
    }

    function formatFileSize(bytes) {
        const value = Number(bytes) || 0;
        if (!value) return '';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
        const amount = value / Math.pow(1024, index);
        return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
    }

    function formatCaseMeta(file) {
        const parts = [];
        if (Array.isArray(file.dimensions) && file.dimensions.length >= 2) {
            const width = Number(file.dimensions[0]) || 0;
            const height = Number(file.dimensions[1]) || 0;
            if (width && height) parts.push(`${Math.round(width / 1000)}k × ${Math.round(height / 1000)}k`);
        }
        if (file.level_count) parts.push(`${file.level_count} ${t(Number(file.level_count) === 1 ? 'slideUnit' : 'slidesUnit')}`);
        const size = formatFileSize(file.file_size);
        if (size) parts.push(size);
        return parts.join(' · ');
    }

    function forceViewerLayout() {
        window.requestAnimationFrame(() => {
            window.requestAnimationFrame(() => {
                const viewer = window.pathchat && window.pathchat.viewer;
                if (!viewer) return;
                if (typeof viewer.forceResize === 'function') viewer.forceResize();
                if (typeof viewer.forceRedraw === 'function') viewer.forceRedraw();
            });
        });
    }

    function applySubagentPaneState(isOpen) {
        const rightPanel = document.querySelector('#v2WorkbenchShell .right-panel');
        const pane = document.getElementById('v2SubagentPane');
        const route = document.getElementById('analysisRoutePanel');
        const toggle = document.getElementById('v2SubagentToggle');
        if (!rightPanel || !pane || !route) return;

        const routeList = document.getElementById('routeStepsList');
        const hasResults = Boolean(
            isOpen ||
            routeList?.children.length ||
            routeList?.textContent.trim()
        );

        rightPanel.classList.toggle('has-subagent-pane', Boolean(isOpen));
        pane.setAttribute('aria-hidden', isOpen ? 'false' : 'true');

        if (toggle) {
            const openTarget = document.getElementById('v2SubagentResultsHeading');
            const closedTarget = document.getElementById('v2ConversationPane');
            const target = isOpen ? openTarget : closedTarget;
            if (target && toggle.parentElement !== target) target.appendChild(toggle);
            toggle.hidden = !hasResults;
            toggle.classList.toggle('is-active', Boolean(isOpen));
            toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
            const label = t(isOpen ? 'hideSubagentResults' : 'showSubagentResults');
            toggle.setAttribute('aria-label', label);
            toggle.title = label;
        }

        if (isOpen) {
            const main = document.querySelector('#v2WorkbenchShell .main-content');
            const left = document.querySelector('#v2WorkbenchShell .left-panel');
            const divider = document.getElementById('rightResizer');
            const leftWidth = document.getElementById('v2WorkbenchShell')?.classList.contains('v2-workbench-sidebar-open')
                ? left?.getBoundingClientRect().width || 0
                : 0;
            const available = Math.max(
                0,
                (main?.clientWidth || window.innerWidth) - leftWidth - (divider?.getBoundingClientRect().width || 0) - 24
            );
            const currentWidth = rightPanel.getBoundingClientRect().width;
            const readableWidth = Math.min(720, available);
            if (readableWidth > currentWidth && currentWidth < 640) {
                document.documentElement.style.setProperty('--right-panel-width', `${readableWidth}px`);
            }
        }

        forceViewerLayout();
        window.setTimeout(forceViewerLayout, 190);
    }

    function syncSubagentPaneVisibility() {
        const route = document.getElementById('analysisRoutePanel');
        if (!route) return;
        applySubagentPaneState(window.getComputedStyle(route).display !== 'none');
    }

    function ensureInspectorWorkspace() {
        const rightPanel = document.querySelector('#v2WorkbenchShell .right-panel');
        const route = document.getElementById('analysisRoutePanel');
        if (!rightPanel || !route) return;

        let workspace = document.getElementById('v2InspectorWorkspace');
        let subagentPane = document.getElementById('v2SubagentPane');
        let conversationPane = document.getElementById('v2ConversationPane');

        if (!workspace) {
            workspace = document.createElement('div');
            workspace.id = 'v2InspectorWorkspace';
            workspace.className = 'v2-inspector-workspace';

            subagentPane = document.createElement('aside');
            subagentPane.id = 'v2SubagentPane';
            subagentPane.className = 'v2-subagent-pane';
            subagentPane.setAttribute('aria-label', t('subagentResults'));

            conversationPane = document.createElement('section');
            conversationPane.id = 'v2ConversationPane';
            conversationPane.className = 'v2-conversation-pane';

            Array.from(rightPanel.children).forEach((child) => conversationPane.appendChild(child));
            workspace.append(subagentPane, conversationPane);
            rightPanel.appendChild(workspace);
        }

        if (route.parentElement !== subagentPane) subagentPane.appendChild(route);

        // ⭐ 中间栏（Subagent 结果）与右侧对话之间的可拖拽分隔条：
        // 拖动调宽、双击恢复默认，宽度记入 localStorage。
        let paneResizer = document.getElementById('v2SubagentPaneResizer');
        if (!paneResizer) {
            paneResizer = document.createElement('div');
            paneResizer.id = 'v2SubagentPaneResizer';
            paneResizer.className = 'v2-subagent-pane-resizer';
            paneResizer.setAttribute('role', 'separator');
            paneResizer.setAttribute('aria-orientation', 'vertical');
            paneResizer.title = (window.__lang || 'zh') === 'en'
                ? 'Drag to resize; double-click to reset'
                : '拖拽调整宽度，双击恢复默认';

            // CSS 里有 max-width:48% 的默认约束，拖宽时需用内联样式一并覆盖
            const applyPaneWidth = (width) => {
                subagentPane.style.flex = `0 0 ${width}px`;
                subagentPane.style.width = `${width}px`;
                subagentPane.style.maxWidth = `${width}px`;
            };
            const savedWidth = Number(localStorage.getItem('pathchat_v2_subagentPaneWidth'));
            if (Number.isFinite(savedWidth) && savedWidth >= 220) {
                applyPaneWidth(savedWidth);
            }

            let paneDragging = false;
            paneResizer.addEventListener('pointerdown', (event) => {
                paneDragging = true;
                try { paneResizer.setPointerCapture(event.pointerId); } catch (_) {}
                document.body.style.cursor = 'col-resize';
                document.body.style.userSelect = 'none';
                event.preventDefault();
            });
            paneResizer.addEventListener('pointermove', (event) => {
                if (!paneDragging) return;
                const wsRect = workspace.getBoundingClientRect();
                const min = 220;
                const max = Math.max(min, wsRect.width - 320); // 给对话栏至少留 320px
                const width = Math.min(max, Math.max(min, event.clientX - wsRect.left));
                applyPaneWidth(width);
            });
            const endPaneDrag = () => {
                if (!paneDragging) return;
                paneDragging = false;
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
                const width = Math.round(subagentPane.getBoundingClientRect().width);
                if (width >= 220) {
                    localStorage.setItem('pathchat_v2_subagentPaneWidth', String(width));
                }
            };
            paneResizer.addEventListener('pointerup', endPaneDrag);
            paneResizer.addEventListener('pointercancel', endPaneDrag);
            paneResizer.addEventListener('dblclick', () => {
                subagentPane.style.flex = '';
                subagentPane.style.width = '';
                subagentPane.style.maxWidth = '';
                localStorage.removeItem('pathchat_v2_subagentPaneWidth');
            });
        }
        if (paneResizer.nextElementSibling !== conversationPane) {
            workspace.insertBefore(paneResizer, conversationPane);
        }

        // Remove the legacy inspector title/tabs even when Flask is still
        // serving a cached copy of the template.  The conversation starts at
        // the top edge; only the Subagent-results toggle remains available.
        conversationPane.querySelector('.v2-inspector-header')?.remove();

        const chatMessages = conversationPane.querySelector('#chatMessages');
        if (chatMessages) {
            chatMessages.dataset.emptyHint = (window.__lang || 'zh') === 'en'
                ? 'Ask a question, or select a region on the slide to continue.'
                : '可以直接提问，或在切片上框选区域继续分析';
            const syncEmptyState = () => {
                chatMessages.classList.toggle('is-empty', chatMessages.children.length === 0);
            };
            syncEmptyState();
            if (!chatMessages.dataset.emptyStateObserved) {
                new MutationObserver(syncEmptyState).observe(chatMessages, { childList: true });
                chatMessages.dataset.emptyStateObserved = 'true';
            }
        }

        let resultsHeading = document.getElementById('v2SubagentResultsHeading');
        if (!resultsHeading) {
            resultsHeading = document.createElement('div');
            resultsHeading.id = 'v2SubagentResultsHeading';
            resultsHeading.className = 'v2-subagent-results-heading';
            resultsHeading.innerHTML = `<i class="fas fa-layer-group" aria-hidden="true"></i><strong>${t('subagentResults')}</strong>`;
            subagentPane.insertBefore(resultsHeading, route);
        }

        const fileManager = document.getElementById('fileManagerPanel');
        if (fileManager?.style.display === 'none') fileManager.style.removeProperty('display');
        document.getElementById('returnToAnalysisBtn')?.remove();

        let toggle = document.getElementById('v2SubagentToggle');
        if (!toggle) {
            toggle = document.createElement('button');
            toggle.id = 'v2SubagentToggle';
            toggle.type = 'button';
            toggle.className = 'v2-subagent-toggle';
            toggle.hidden = true;
            toggle.innerHTML = '<i class="fas fa-list-check" aria-hidden="true"></i>';
            // When the pane is open, applySubagentPaneState moves this control
            // beside the "Subagent results" heading.  It returns here only
            // while the pane is closed, so users can reopen it.
            conversationPane.appendChild(toggle);
            toggle.addEventListener('click', () => {
                const isOpen = rightPanel.classList.contains('has-subagent-pane');
                if (isOpen) {
                    if (window.pathChatAgent?.exitAnalysisLayout) window.pathChatAgent.exitAnalysisLayout();
                    else {
                        route.style.display = 'none';
                        applySubagentPaneState(false);
                    }
                } else if (window.pathChatAgent?.resumeAnalysisLayout) {
                    window.pathChatAgent.resumeAnalysisLayout();
                } else {
                    route.style.display = 'flex';
                    applySubagentPaneState(true);
                }
            });
        }

        const routeBack = document.getElementById('routeBackBtn');
        if (routeBack) {
            const label = t('hideSubagentResults');
            routeBack.setAttribute('aria-label', label);
            routeBack.title = label;
        }

        if (subagentPanelObserver) subagentPanelObserver.disconnect();
        subagentPanelObserver = new MutationObserver(syncSubagentPaneVisibility);
        subagentPanelObserver.observe(route, { attributes: true, attributeFilter: ['style', 'class'] });
        syncSubagentPaneVisibility();
    }

    function setSubagentPaneOpen(isOpen) {
        ensureInspectorWorkspace();
        applySubagentPaneState(Boolean(isOpen));
    }

    function isPatientLevelPanelOpen() {
        const panel = document.getElementById('patientLevelPanel');
        if (!panel) return false;
        const display = window.getComputedStyle(panel).display;
        return display !== 'none';
    }

    function isWorkbenchVisible() {
        const workbench = document.getElementById('v2WorkbenchShell');
        return Boolean(workbench && !workbench.classList.contains('v2-workbench-is-hidden'));
    }

    function setWorkbenchSidebarOpen(isOpen) {
        const workbench = document.getElementById('v2WorkbenchShell');
        const toggle = document.getElementById('v2WorkbenchSidebarToggle');
        if (!workbench) return;
        workbench.classList.toggle('v2-workbench-sidebar-open', Boolean(isOpen));
        if (toggle) {
            toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
            const label = t(isOpen ? 'closeCaseRail' : 'openCaseRail');
            toggle.setAttribute('aria-label', label);
            toggle.title = label;
        }
        forceViewerLayout();
        window.setTimeout(forceViewerLayout, 210);
    }

    function enterWorkbench(options = {}) {
        const home = document.getElementById('v2HomeShell');
        const workbench = document.getElementById('v2WorkbenchShell');
        if (!home || !workbench) return;

        home.hidden = true;
        home.setAttribute('aria-hidden', 'true');
        workbench.classList.remove('v2-workbench-is-hidden');
        workbench.setAttribute('aria-hidden', 'false');
        document.documentElement.classList.add('v2-workbench-active');
        const keepSidebar = options.keepSidebar === true || isPatientLevelPanelOpen();
        if (!keepSidebar) setWorkbenchSidebarOpen(false);

        const prompt = String(options.prompt || '').trim();
        if (prompt) {
            const chatTab = document.querySelector('.v2-inspector-header .tab-btn[data-tab="chat"]');
            if (chatTab) chatTab.click();
            const chatInput = document.getElementById('chatInput');
            if (chatInput) {
                chatInput.value = prompt;
                chatInput.dispatchEvent(new Event('input', { bubbles: true }));
                if (options.send && window.pathchat && typeof window.pathchat.sendMessage === 'function') {
                    window.setTimeout(() => window.pathchat.sendMessage(), 0);
                }
            }
        }

        forceViewerLayout();
    }

    function showHome(options = {}) {
        const home = document.getElementById('v2HomeShell');
        const workbench = document.getElementById('v2WorkbenchShell');
        if (!home || !workbench) return;

        workbench.classList.add('v2-workbench-is-hidden');
        workbench.setAttribute('aria-hidden', 'true');
        setWorkbenchSidebarOpen(false);
        home.hidden = false;
        home.setAttribute('aria-hidden', 'false');
        document.documentElement.classList.remove('v2-workbench-active');
        scheduleHomeCasesRefresh(0);

        if (options.focus !== false) {
            window.requestAnimationFrame(() => document.getElementById('v2HomePrompt')?.focus());
        }
    }

    function renderHomeCases(files, projects) {
        if (Array.isArray(projects) && projects.length) {
            homeProjects = projects.map((project) => ({
                ...project,
                files: Array.isArray(project.files) ? project.files : []
            }));
        } else {
            homeProjects = groupHomeCaseProjects(files);
        }
        homeCasesLoaded = true;
        renderHomeRecents();
    }

    async function refreshHomeCases() {
        const refresh = document.getElementById('v2HomeRefreshThreads');
        if (refresh) refresh.classList.add('is-loading');
        try {
            const response = await fetch('/api/files', { cache: 'no-store' });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const result = await response.json();
            renderHomeCases(result.files || [], result.projects);
        } catch (error) {
            console.warn('[PathCopilot V2] Failed to load home cases:', error);
            homeCasesLoaded = true;
            if (!homeProjects.length) homeProjects = [];
            renderHomeRecents();
        } finally {
            if (refresh) refresh.classList.remove('is-loading');
        }
    }

    function scheduleHomeCasesRefresh(delay = 180) {
        window.clearTimeout(homeCasesRefreshTimer);
        homeCasesRefreshTimer = window.setTimeout(refreshHomeCases, delay);
    }

    function wrapWorkbenchEntrypoints() {
        const app = window.pathchat;
        if (!app || app.__v2WorkbenchEntrypointsWrapped) return;
        app.__v2WorkbenchEntrypointsWrapped = true;

        const wrapEntrypoint = (methodName, before) => {
            if (typeof app[methodName] !== 'function' || app[methodName].__v2Wrapped) return;
            const original = app[methodName].bind(app);
            const wrapped = (...args) => {
                before?.();
                return original(...args);
            };
            wrapped.__v2Wrapped = true;
            app[methodName] = wrapped;
        };
        wrapEntrypoint('loadWSI', () => {
            enterWorkbench({
                keepSidebar: isWorkbenchVisible() || isPatientLevelPanelOpen()
            });
        });
        wrapEntrypoint('loadDemo', () => enterWorkbench());
        wrapEntrypoint('openPatientLevelFromExistingFiles', () => {
            enterWorkbench({ keepSidebar: true });
            setWorkbenchSidebarOpen(true);
        });
    }

    function setSlideTitle(rawName) {
        const title = document.getElementById('v2SlideTitle');
        if (!title) return;
        const label = title.querySelector('span');
        if (!label) return;

        const normalized = String(rawName || '')
            .replace(/^Current slide\s*:\s*/i, '')
            .replace(/^当前切片\s*[：:]\s*/, '')
            .trim();

        if (!normalized || normalized === '--') return;
        label.textContent = normalized;
        label.removeAttribute('data-v2-i18n');
        title.title = normalized;
    }

    function initializeDesktopResearchBrowser() {
        const desktop = window.pathcopilotDesktop;
        if (!desktop?.isDesktop || typeof desktop.onResearchBrowserState !== 'function') return;

        const isChinese = language() === 'zh';
        const chrome = document.createElement('aside');
        chrome.className = 'v2-research-browser-chrome';
        chrome.hidden = true;
        chrome.setAttribute('aria-label', isChinese ? '文献浏览器工具栏' : 'Research browser toolbar');
        chrome.innerHTML = `
            <div class="v2-research-browser-nav">
                <button type="button" data-research-action="back" aria-label="${isChinese ? '后退' : 'Back'}" title="${isChinese ? '后退' : 'Back'}"><i class="fas fa-arrow-left"></i></button>
                <button type="button" data-research-action="forward" aria-label="${isChinese ? '前进' : 'Forward'}" title="${isChinese ? '前进' : 'Forward'}"><i class="fas fa-arrow-right"></i></button>
                <button type="button" data-research-action="reload" aria-label="${isChinese ? '重新加载' : 'Reload'}" title="${isChinese ? '重新加载' : 'Reload'}"><i class="fas fa-redo-alt"></i></button>
            </div>
            <div class="v2-research-browser-location">
                <span class="v2-research-browser-loading" aria-hidden="true"></span>
                <span class="v2-research-browser-title">${isChinese ? '文献浏览器' : 'Research browser'}</span>
                <small class="v2-research-browser-domain"></small>
            </div>
            <div class="v2-research-browser-actions">
                <button type="button" data-research-action="external" aria-label="${isChinese ? '在系统浏览器中打开' : 'Open in system browser'}" title="${isChinese ? '在系统浏览器中打开' : 'Open in system browser'}"><i class="fas fa-external-link-alt"></i></button>
                <button type="button" data-research-action="close" aria-label="${isChinese ? '关闭文献浏览器' : 'Close research browser'}" title="${isChinese ? '关闭文献浏览器' : 'Close research browser'}"><i class="fas fa-times"></i></button>
            </div>`;
        document.body.appendChild(chrome);

        let browserOpen = false;
        const back = chrome.querySelector('[data-research-action="back"]');
        const forward = chrome.querySelector('[data-research-action="forward"]');
        const title = chrome.querySelector('.v2-research-browser-title');
        const domain = chrome.querySelector('.v2-research-browser-domain');

        const applyState = (state = {}) => {
            browserOpen = Boolean(state.open);
            const width = Math.max(0, Number(state.width) || 0);
            document.documentElement.style.setProperty('--research-browser-width', `${width}px`);
            document.body.classList.toggle('v2-research-browser-open', browserOpen);
            chrome.hidden = !browserOpen;
            chrome.classList.toggle('is-loading', Boolean(state.loading));
            if (back) back.disabled = !state.canGoBack;
            if (forward) forward.disabled = !state.canGoForward;

            let host = '';
            try { host = new URL(String(state.url || '')).hostname; } catch (_error) { host = ''; }
            if (title) {
                title.textContent = String(state.title || host || (isChinese ? '文献浏览器' : 'Research browser'));
                title.title = String(state.title || state.url || '');
            }
            if (domain) domain.textContent = host;
        };

        const unsubscribe = desktop.onResearchBrowserState(applyState);
        const actions = {
            back: () => desktop.researchBrowserBack?.(),
            forward: () => desktop.researchBrowserForward?.(),
            reload: () => desktop.researchBrowserReload?.(),
            external: () => desktop.openResearchBrowserExternally?.(),
            close: () => desktop.closeResearchBrowser?.()
        };
        chrome.querySelectorAll('[data-research-action]').forEach((button) => {
            button.addEventListener('click', () => {
                const action = button.getAttribute('data-research-action');
                if (action && actions[action]) void actions[action]();
            });
        });
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && browserOpen) void desktop.closeResearchBrowser?.();
        });
        window.addEventListener('beforeunload', () => {
            if (typeof unsubscribe === 'function') unsubscribe();
        }, { once: true });
    }

    function selectedFileName(item) {
        if (!item) return '';
        const name = item.querySelector('.file-name, .filename, .file-info h5, [data-filename]');
        return (name && (name.getAttribute('data-filename') || name.textContent)) || item.dataset.filename || '';
    }

    function bindWorkspaceActions() {
        const newCase = document.getElementById('v2NewCaseBtn');
        const fileInput = document.getElementById('fileInput');
        const folderInput = document.getElementById('folderInput');
        const homeImageInput = document.getElementById('v2HomeImageInput');
        const homePrompt = document.getElementById('v2HomePrompt');
        const homeForm = document.getElementById('v2HomeComposer');
        const homeSubmit = document.getElementById('v2HomeSubmit');
        const homeMain = document.getElementById('v2HomeMain');

        if (newCase && fileInput) {
            newCase.addEventListener('click', () => fileInput.click());
        }

        document.getElementById('v2BackHomeBtn')?.addEventListener('click', () => showHome());
        document.getElementById('v2WorkbenchSidebarToggle')?.addEventListener('click', () => {
            const workbench = document.getElementById('v2WorkbenchShell');
            setWorkbenchSidebarOpen(!workbench?.classList.contains('v2-workbench-sidebar-open'));
        });
        document.querySelector('#v2WorkbenchShell .main-content')?.addEventListener('pointerdown', (event) => {
            const workbench = document.getElementById('v2WorkbenchShell');
            // 患者级模式下病例栏是工作列表，必须常开（与 enterWorkbench 的
            // keepSidebar 约定一致）；点「确认该切片」等聊天区按钮不得把它收起。
            if (isPatientLevelPanelOpen()) return;
            if (workbench?.classList.contains('v2-workbench-sidebar-open') && !event.target.closest('.left-panel')) {
                setWorkbenchSidebarOpen(false);
            }
        });
        document.getElementById('v2HomeNewTask')?.addEventListener('click', () => {
            showHome({ focus: false });
            resetHomeChat();
            if (homePrompt) {
                homePrompt.value = '';
                homePrompt.style.height = '';
                homePrompt.focus();
                homePrompt.dispatchEvent(new Event('input', { bubbles: true }));
            }
        });

        document.getElementById('v2HomeUpload')?.addEventListener('click', () => fileInput?.click());
        document.getElementById('v2HomeAttach')?.addEventListener('click', () => homeImageInput?.click());
        homeImageInput?.addEventListener('change', () => {
            stageHomeFiles(homeImageInput.files);
            homeImageInput.value = '';
        });

        document.getElementById('v2HomeDemo')?.addEventListener('click', () => {
            enterWorkbench();
            if (window.pathchat && typeof window.pathchat.loadDemo === 'function') {
                window.pathchat.loadDemo('tcga_public_demo');
            }
        });

        document.getElementById('v2HomeRefreshThreads')?.addEventListener('click', () => {
            void refreshHomeThreads();
            void refreshHomeCases();
        });
        document.getElementById('v2HomeSidebarToggle')?.addEventListener('click', (event) => {
            const shell = document.getElementById('v2HomeShell');
            const collapsed = shell?.classList.toggle('is-sidebar-collapsed') || false;
            const button = event.currentTarget;
            const label = t(collapsed ? 'expandSidebar' : 'collapseSidebar');
            button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
            button.setAttribute('aria-label', label);
            button.title = label;
        });

        const updateHomeComposer = () => {
            if (!homePrompt) return;
            homePrompt.style.height = 'auto';
            homePrompt.style.height = `${Math.min(homePrompt.scrollHeight, 170)}px`;
            if (homeSubmit) {
                homeSubmit.disabled = homeChatBusy || (
                    homePrompt.value.trim().length === 0
                    && homeImageAttachments.length === 0
                    && homeWsiAttachments.length === 0
                );
            }
        };

        if (homePrompt) {
            homePrompt.addEventListener('input', updateHomeComposer);
            homePrompt.addEventListener('paste', (event) => {
                const imageFiles = Array.from(event.clipboardData?.items || [])
                    .filter((item) => item.kind === 'file' && String(item.type || '').startsWith('image/'))
                    .map((item) => item.getAsFile())
                    .filter(Boolean);
                if (!imageFiles.length) return;
                event.preventDefault();
                void addHomeImageFiles(imageFiles);
            });
            homePrompt.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
                    event.preventDefault();
                    homeForm?.requestSubmit();
                }
            });
            updateHomeComposer();
        }

        homeForm?.addEventListener('submit', (event) => {
            event.preventDefault();
            const prompt = homePrompt?.value.trim() || (homeImageAttachments.length ? t('defaultImageQuestion') : '');
            if (!prompt && homeWsiAttachments.length === 0) return;
            if (homeWsiAttachments.length) {
                const stagedSlides = homeWsiAttachments.map((item) => item.file);
                homeWsiAttachments = [];
                homeImageAttachments = [];
                renderHomeImageAttachments();
                homePrompt.value = '';
                updateHomeComposer();
                enterWorkbench({ prompt, send: false });
                window.setTimeout(() => window.pathchat?.handleFileSelection?.(stagedSlides), 0);
                return;
            }
            // The home composer represents a context-free conversation. A slide that
            // happened to be open in the previous workbench must not silently capture
            // a new question. Enter WSI analysis only when the user explicitly stages
            // a WSI above or opens a case from the recent-case list.
            const attachmentsToSend = homeImageAttachments.slice();
            homeImageAttachments = [];
            renderHomeImageAttachments();
            homePrompt.value = '';
            updateHomeComposer();
            void sendHomeChat(prompt, attachmentsToSend);
        });

        document.querySelectorAll('[data-home-prompt]').forEach((button) => {
            button.addEventListener('click', () => {
                if (!homePrompt) return;
                homePrompt.value = button.getAttribute('data-home-prompt') || '';
                homePrompt.dispatchEvent(new Event('input', { bubbles: true }));
                homePrompt.focus();
                homePrompt.setSelectionRange(homePrompt.value.length, homePrompt.value.length);
            });
        });

        const revealForSelectedFiles = (event) => {
            if (event.target.files && event.target.files.length > 0) enterWorkbench();
        };
        fileInput?.addEventListener('change', revealForSelectedFiles, true);
        folderInput?.addEventListener('change', revealForSelectedFiles, true);

        if (homeMain) {
            const hasFiles = (event) => Array.from(event.dataTransfer?.types || []).includes('Files');
            homeMain.addEventListener('dragenter', (event) => {
                if (!hasFiles(event)) return;
                event.preventDefault();
                homeDragDepth += 1;
                homeMain.classList.add('is-dragging');
            });
            homeMain.addEventListener('dragover', (event) => {
                if (!hasFiles(event)) return;
                event.preventDefault();
                event.dataTransfer.dropEffect = 'copy';
            });
            homeMain.addEventListener('dragleave', (event) => {
                if (!hasFiles(event)) return;
                homeDragDepth = Math.max(0, homeDragDepth - 1);
                if (homeDragDepth === 0) homeMain.classList.remove('is-dragging');
            });
            homeMain.addEventListener('drop', (event) => {
                if (!hasFiles(event)) return;
                event.preventDefault();
                homeDragDepth = 0;
                homeMain.classList.remove('is-dragging');
                const files = event.dataTransfer.files;
                if (!files || !files.length) return;
                stageHomeFiles(files);
            });
        }

        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                setWorkbenchSidebarOpen(false);
            }
            if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'n') {
                event.preventDefault();
                document.getElementById('v2HomeNewTask')?.click();
                return;
            }
            if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'o') {
                event.preventDefault();
                if (fileInput) fileInput.click();
            }
        });
    }

    function bindInspectorTabs() {
        const tabs = Array.from(document.querySelectorAll('.v2-inspector-header .tab-btn'));
        tabs.forEach((tab) => {
            tab.addEventListener('click', () => {
                tabs.forEach((candidate) => candidate.setAttribute('aria-selected', candidate === tab ? 'true' : 'false'));
            });
        });
    }

    function bindSlideTitle() {
        const files = document.getElementById('filesList');
        if (files) {
            files.addEventListener('click', (event) => {
                const item = event.target.closest('.file-item');
                if (item) setSlideTitle(selectedFileName(item));
            });

            const fileObserver = new MutationObserver(() => {
                const selected = files.querySelector('.file-item.active, .file-item.selected');
                if (selected) setSlideTitle(selectedFileName(selected));
                scheduleHomeCasesRefresh();
            });
            fileObserver.observe(files, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
        }

        ['routeSlideName', 'plCurrentWsiName'].forEach((id) => {
            const source = document.getElementById(id);
            if (!source) return;
            const observer = new MutationObserver(() => {
                // plCurrentWsiName 是「正在分析 X」标签：并行时最后启动的切片会
                // 刷新它，但 viewer 标题必须跟随正在查看的切片，不匹配就不镜像。
                if (id === 'plCurrentWsiName') {
                    const viewing = String(window.pathchat?.currentFile?.filename || '').trim();
                    const text = String(source.textContent || '');
                    if (viewing && !text.includes(viewing)) return;
                }
                setSlideTitle(source.textContent);
            });
            observer.observe(source, { childList: true, subtree: true, characterData: true });
        });
    }

    function setReviewStage(stage, state, text) {
        const item = document.querySelector(`[data-review-stage="${stage}"]`);
        if (!item) return;
        const badge = item.querySelector('.v2-review-status');
        const detail = item.querySelector('small');
        if (badge) {
            badge.classList.remove('pending', 'running', 'complete');
            badge.classList.add(state);
            badge.innerHTML = state === 'complete'
                ? '<i class="fas fa-check"></i>'
                : state === 'running'
                    ? '<i class="fas fa-spinner fa-spin"></i>'
                    : '<i class="fas fa-circle"></i>';
        }
        if (detail && text) detail.textContent = text;
    }

    function refreshReviewState() {
        const timeline = document.getElementById('agentTimeline');
        const tree = document.getElementById('treeVisualization');
        const report = document.getElementById('agentReportSection');
        const route = document.getElementById('analysisRoutePanel');

        const timelineStarted = !!(timeline && !timeline.querySelector('.timeline-empty') && timeline.textContent.trim());
        const routeRunning = !!(route && getComputedStyle(route).display !== 'none');
        const evidenceBuilt = !!(tree && !tree.querySelector('.tree-empty') && tree.textContent.trim());
        const reportBuilt = !!(report && getComputedStyle(report).display !== 'none' && report.textContent.trim());

        if (timelineStarted || routeRunning) {
            setReviewStage('macro', evidenceBuilt ? 'complete' : 'running', evidenceBuilt ? t('evidenceReady') : t('inProgress'));
        }
        if (evidenceBuilt) {
            setReviewStage('macro', 'complete', t('evidenceReady'));
            setReviewStage('lesion', 'complete', t('evidenceReady'));
            setReviewStage('cell', 'complete', t('evidenceReady'));
        } else if (timelineStarted) {
            setReviewStage('lesion', 'running', t('inProgress'));
        }
        if (reportBuilt) {
            setReviewStage('report', 'complete', t('reportReady'));
        } else if (evidenceBuilt) {
            setReviewStage('report', 'running', t('inProgress'));
        }
    }

    function observeReviewState() {
        ['agentTimeline', 'treeVisualization', 'agentReportSection', 'analysisRoutePanel'].forEach((id) => {
            const target = document.getElementById(id);
            if (!target) return;
            const observer = new MutationObserver(refreshReviewState);
            observer.observe(target, { childList: true, subtree: true, attributes: true, characterData: true });
        });
        refreshReviewState();
    }

    function initialize() {
        if (initialized) return;
        initialized = true;
        document.documentElement.classList.add('pathcopilot-v2');
        initializeDesktopResearchBrowser();
        applyTranslations();
        ensureInspectorWorkspace();
        wrapWorkbenchEntrypoints();
        bindWorkspaceActions();
        bindInspectorTabs();
        bindSlideTitle();
        observeReviewState();
        document.getElementById('v2HomeChat')?.addEventListener('scroll', updateHomeTurnRailActive, { passive: true });
        window.addEventListener('resize', scheduleHomeTurnRail, { passive: true });
        showHome({ focus: false });
        scheduleHomeCasesRefresh(0);
        void refreshHomeThreads({ restore: true });

        window.PathCopilotV2 = Object.freeze({
            enterWorkbench,
            showHome,
            refreshHomeCases,
            setWorkbenchSidebarOpen,
            setSlideTitle,
            setSubagentPaneOpen,
            applyTranslations,
            consumeHomeWebSearchResponse,
            createHomeWebSearchView,
            updateHomeWebSearchStatus,
            renderHomeWebSources,
            renderHomeAssistantContent,
            groupHomeCaseProjects
        });

        window.addEventListener('pathcopilot:language-change', () => {
            applyTranslations();
            renderHomeRecents();
            refreshReviewState();
            scheduleHomeCasesRefresh(0);
            scheduleHomeThreadsRefresh(0);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize, { once: true });
    } else {
        initialize();
    }
})();
