/**
 * PathCopilot Agent 前端控制器
 * 独立运行，不依赖全局app变量
 */

function _copilotConfirm(message) {
    return new Promise((resolve) => {
        const isEn = (window.__lang || 'zh') === 'en';
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.45);z-index:99999;display:flex;align-items:center;justify-content:center;';
        const box = document.createElement('div');
        box.style.cssText = 'background:#fff;border-radius:12px;padding:28px 32px 20px;max-width:480px;width:90%;box-shadow:0 8px 40px rgba(0,0,0,.25);font-family:system-ui,sans-serif;';
        const msg = document.createElement('div');
        msg.style.cssText = 'white-space:pre-wrap;font-size:14px;color:#333;line-height:1.6;margin-bottom:20px;';
        msg.textContent = message;
        const btnRow = document.createElement('div');
        btnRow.style.cssText = 'display:flex;justify-content:flex-end;gap:10px;';
        const btnCancel = document.createElement('button');
        btnCancel.textContent = isEn ? 'Cancel' : '取消';
        btnCancel.style.cssText = 'padding:8px 20px;border:1px solid #d1d5db;border-radius:8px;background:#fff;color:#374151;cursor:pointer;font-size:14px;';
        const btnOk = document.createElement('button');
        btnOk.textContent = isEn ? 'OK' : '确定';
        btnOk.style.cssText = 'padding:8px 20px;border:none;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer;font-size:14px;';
        btnRow.append(btnCancel, btnOk);
        box.append(msg, btnRow);
        overlay.appendChild(box);
        document.body.appendChild(overlay);
        btnOk.addEventListener('click', () => { overlay.remove(); resolve(true); });
        btnCancel.addEventListener('click', () => { overlay.remove(); resolve(false); });
        overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); resolve(false); } });
        btnOk.focus();
    });
}

class PathChatAgentController {
    constructor() {
        this.socket = null;
        this.runtimeStoragePrefix = 'pathcopilot.runtime.v2';
        this.sessionId = (
            this._readRuntimeStorage('session_id')
            || this.generateSessionId()
        );
        this.currentFileId = null;
        this.runtimeRunId = this._readRuntimeStorage('run_id') || null;
        this.runtimeRunFileId = this._readRuntimeStorage('run_file_id') || null;
        this._workflowRestorePromise = null;
        this._workflowRestoreKey = null;
        this.runtimeRequestId = (
            this._readRuntimeStorage('pending_request_id')
            || null
        );
        this.runtimeRequestFileId = (
            this._readRuntimeStorage('pending_request_file_id')
            || null
        );
        try {
            this.runtimeRequestPayload = JSON.parse(
                this._readRuntimeStorage('pending_request_payload') || 'null'
            );
        } catch (_) {
            this.runtimeRequestPayload = null;
        }
        this.runtimeEventCursor = Math.max(
            0,
            Number.parseInt(this._readRuntimeStorage('event_cursor') || '0', 10)
            || 0
        );
        this.runtimeReplayEventIds = new Set();
        this._retiredRuntimeRunIds = new Set();
        this.viewer = null;
        this.isRunning = false;
        this.isStarting = false;
        this.isStopping = false;
        this.isFollowing = true;
        this.followTimeout = null;
        this.traceData = [];
        this.currentOverlays = [];
        this.activityStartedAt = null;
        this.activityTimer = null;
        this.activityLastKey = '';
        this.activityDetailKeys = new Set();
        this.activityImageKeys = new Set();
        this.activityOverviewImageShown = false;
        this.subagentStates = new Map();
        this.pendingBlueAgentAliases = new Map();
        this.subagentExpectedCount = 0;
        this.primaryAgentState = 'idle';
        this.thumbnailPlanAnnounced = false;
        this.thumbnailRootCompleted = false;
        this.thumbnailThinkingMessage = null;
        this.thumbnailToolMessage = null;
        this._lastOverviewImageUrl = '';
        this._thumbnailChatMessage = null;
        this._thumbnailAnnouncedKeys = new Set();
        this._thumbnailAnnounceFileId = '';
        this._patientParallelBriefed = false;
        this.chatSubagentStatusKeys = new Set();
        this.subagentUiActivated = false;
        this.pendingActivityItems = [];
        this.pendingActivityUpdate = null;
        this.activeDiagnosticQuestion = '';
        this.activeAnalysisStrategy = 'agent_swarm';
        this.collaborationReviewPolicy = null;
        this._programmaticViewportMove = false;
        this._roamViewportTimer = null;
        this._roamViewportGuardTimer = null;
        this._lastRoamTarget = null;
        this._pendingRoamTarget = null;
        this._pendingRuntimeSocketEvents = [];
        this._reviewQueuesByFile = new Map();
        this._reviewQueueMessageKeys = new Set();
        this._reviewHighlightedIds = new Set();
        this._reviewHighlightEpoch = 0;
        this._pendingSelectionConfirmations = new Map();
        this._pendingPhaseConfirmations = new Map();
        this._pendingAnalysisEdits = new Map();
        this._draftReportReview = null;
        this._draftReportReviews = new Map();
        this._confirmModalReturnFocus = null;
        this._confirmModalKeyHandler = null;
        this._confirmModalInertState = [];
        this._roamUiEventKeys = new Set();
        this._roamMaxStepByRun = new Map();
        this._roamSnapshotEpoch = 0;
        this._collaborationSnapshotEpoch = 0;
        this._collaborationLiveSerial = 0;
        this._activeRoamRunUiId = null;
        this._activePatientRoamRunToken = null;
        this._activePatientRunToken = null;
        this._patientRoamByFile = new Map();
        this._patientRoamEventKeys = new Set();
        this._patientRoamDisplayedKeys = new Set();
        this._patientRoamPausedKeys = new Set();
        this._patientActivitySyncedFiles = new Set();
        this._patientSwarmMapWsiIndex = null;
        this._patientWorkspaceHtmlRestored = false;
        this._patientTerminalUiKeys = new Set();
        this._patientStopLatch = false;
        this._activePatientTrajectoryOwner = '';
        try {
            const storedPauseKeys = JSON.parse(
                window.sessionStorage?.getItem('pathcopilot.patient.roam_paused') || '[]'
            );
            if (Array.isArray(storedPauseKeys)) {
                this._patientRoamPausedKeys = new Set(
                    storedPauseKeys.filter(value => typeof value === 'string' && value)
                );
            }
        } catch (_) {
            this._patientRoamPausedKeys = new Set();
        }
        this.isPatientLevelMode = Boolean(
            this._readRuntimeStorage('active_patient_id')
            || (() => {
                try { return window.sessionStorage?.getItem('pathcopilot.patient.active_id'); }
                catch (_) { return null; }
            })()
        );
        this.roiHierarchyObserver = null;
        this.isRepairingHierarchy = false;
        
        // 人机协作模式相关
        this.currentMode = 'ai';  // 'ai' 或 'collaborative'
        this.isCollaborativeMode = false;  // ⭐ 便捷属性，用于判断是否协作模式
        this.isManualSelecting = false;
        this.manualSelections = [];
        this.currentConfirmationId = null;
        this.currentParentRect = null;
        this.currentEditingRegions = [];
        this.selectedRegions = [];
        this.confirmationOverlays = [];
        this._persistRuntimeState();
        
        console.log('PathCopilot Agent Controller 初始化', { sessionId: this.sessionId });
        
        this.init();
    }
    
    init() {
        this.initWebSocket();
        this.bindEvents();
        this.updateButtons();
        this.detectViewer();
        // V2 starts directly in automatic analysis mode; no opening mode chooser.
        this.currentMode = 'ai';
        this.isCollaborativeMode = false;
        this.bindConfirmationEvents();
        this._observeRoiHierarchy();
        let activePatientId = '';
        try {
            activePatientId = window.sessionStorage?.getItem('pathcopilot.patient.active_id') || '';
        } catch (_) {}
        if (activePatientId) {
            window.setTimeout(() => this.restorePatientLevelSnapshot(activePatientId), 0);
        }
    }
    
    generateSessionId() {
        return 'session_' + Math.random().toString(36).substr(2, 9) + '_' + Date.now();
    }

    _runtimeStorageKey(name) {
        return `${this.runtimeStoragePrefix}.${name}`;
    }

    _readRuntimeStorage(name) {
        try {
            return window.sessionStorage?.getItem(this._runtimeStorageKey(name));
        } catch (_) {
            return null;
        }
    }

    _persistPatientRoamPausedKeys() {
        try {
            window.sessionStorage?.setItem(
                'pathcopilot.patient.roam_paused',
                JSON.stringify(Array.from(this._patientRoamPausedKeys).slice(-64))
            );
        } catch (_) {
            // Viewport preference persistence is best-effort only.
        }
    }

    _writeRuntimeStorage(name, value) {
        try {
            const storage = window.sessionStorage;
            if (!storage) return;
            const key = this._runtimeStorageKey(name);
            if (value === null || value === undefined || value === '') {
                storage.removeItem(key);
            } else {
                storage.setItem(key, String(value));
            }
        } catch (_) {
            // Private browsing or a restrictive Electron policy can disable
            // sessionStorage. Live Socket.IO remains fully functional.
        }
    }

    _persistRuntimeState() {
        this._writeRuntimeStorage('session_id', this.sessionId);
        this._writeRuntimeStorage('run_id', this.runtimeRunId);
        this._writeRuntimeStorage('run_file_id', this.runtimeRunFileId);
        this._writeRuntimeStorage(
            'event_cursor',
            Math.max(0, Number(this.runtimeEventCursor) || 0)
        );
        this._writeRuntimeStorage(
            'pending_request_id',
            this.runtimeRequestId
        );
        this._writeRuntimeStorage(
            'pending_request_file_id',
            this.runtimeRequestFileId
        );
        this._writeRuntimeStorage(
            'pending_request_payload',
            this.runtimeRequestPayload
                ? JSON.stringify(this.runtimeRequestPayload)
                : null
        );
    }

    _clearPendingRuntimeRequest() {
        this.runtimeRequestId = null;
        this.runtimeRequestFileId = null;
        this.runtimeRequestPayload = null;
        this._persistRuntimeState();
    }

    _clearRuntimeRun() {
        if (this.runtimeRunId) {
            this._retiredRuntimeRunIds.add(String(this.runtimeRunId));
            if (this._retiredRuntimeRunIds.size > 16) {
                this._retiredRuntimeRunIds.delete(this._retiredRuntimeRunIds.values().next().value);
            }
        }
        this.runtimeRunId = null;
        this.runtimeRunFileId = null;
        this.runtimeEventCursor = 0;
        this.runtimeReplayEventIds.clear();
        this.runtimeRequestId = null;
        this.runtimeRequestFileId = null;
        this.runtimeRequestPayload = null;
        this._persistRuntimeState();
    }

    _isRuntimeTerminalState(state) {
        return ['succeeded', 'failed', 'partial', 'cancelled'].includes(
            String(state || '').toLowerCase()
        );
    }

    async _rebindManagedRuntimeIfNeeded(runtime) {
        if (
            !runtime
            || !['created', 'ready', 'waiting'].includes(runtime.root_task_state)
            || !this.runtimeRequestPayload
            || !this.runtimeRequestId
        ) {
            return;
        }
        const response = await fetch('/api/agent/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(this.runtimeRequestPayload)
        });
        if (!response.ok) return;
        const payload = await response.json();
        const reboundRunId = payload.run_id || payload.runtime?.run_id;
        if (payload.success && reboundRunId === this.runtimeRunId) {
            this._persistRuntimeState();
        }
    }

    _ackRuntimeSocketEvent(payload) {
        const runtime = payload?.runtime;
        if (!runtime) return;
        if (
            runtime.run_id
            && this.runtimeRunId
            && runtime.run_id !== this.runtimeRunId
        ) return;
        if (Number.isInteger(runtime.seq)) {
            this.runtimeEventCursor = Math.max(
                this.runtimeEventCursor,
                runtime.seq
            );
        }
        this._persistRuntimeState();
    }

    _runtimeSocketEventApplies(payload) {
        const incomingRunId = String(payload?.runtime?.run_id || '');
        if (!incomingRunId) return true;
        if (this._retiredRuntimeRunIds.has(incomingRunId)) return false;
        return !this.runtimeRunId || incomingRunId === this.runtimeRunId;
    }

    _flushPendingRuntimeSocketEvents(acceptedRunId) {
        const pending = this._pendingRuntimeSocketEvents.splice(0);
        pending.forEach(({ eventName, payload, handler }) => {
            if (String(payload?.runtime?.run_id || '') !== String(acceptedRunId || '')) return;
            Promise.resolve(handler(payload))
                .then(() => this._ackRuntimeSocketEvent(payload))
                .catch((error) => console.error(`Agent event handler failed: ${eventName}`, error));
        });
    }
    
    initWebSocket() {
        try {
            this.socket = io(window.location.origin, {
                path: '/socket.io'
            });
            
            this.socket.on('connect', () => {
                console.log('WebSocket连接成功');
                this.joinRoom();
            });

            const bindAgent = (eventName, handler) => {
                this.socket.on(eventName, (payload) => {
                    try {
                        if (
                            this.isStarting
                            && !this.runtimeRunId
                            && payload?.runtime?.run_id
                        ) {
                            this._pendingRuntimeSocketEvents.push({ eventName, payload, handler });
                            return;
                        }
                        if (!this._runtimeSocketEventApplies(payload)) {
                            console.debug(`Ignored stale Agent event: ${eventName}`, payload?.runtime?.run_id);
                            return;
                        }
                        Promise.resolve(handler(payload))
                            .then(() => this._ackRuntimeSocketEvent(payload))
                            .catch((error) => {
                                console.error(
                                    `Agent event handler failed: ${eventName}`,
                                    error
                                );
                            });
                    } catch (error) {
                        console.error(
                            `Agent event handler failed: ${eventName}`,
                            error
                        );
                    }
                });
            };
            
            this.socket.on('disconnect', () => {
                console.log('WebSocket连接断开');
                this._releasePendingSelectionConfirmations('disconnect');
                this._releasePendingPhaseConfirmations('disconnect');
                this._releasePendingAnalysisEdits('disconnect');
            });
            
            this.socket.on('joined', (data) => {
                console.log('已加入房间:', data);
                // The room join is the delivery barrier.  Restoring before
                // this ACK leaves a gap in which a new approval gate can be
                // emitted after the snapshot but before this socket is in the
                // room.  Join first, then reconcile snapshot + Runtime events.
                if (data?.status === 'success') this.syncRuntimeAfterReconnect();
            });
            
            // 监听Agent事件
            bindAgent('agent.viewport', (data) => this.handleViewportEvent(data));
            bindAgent('agent.trace', (data) => this.handleTraceEvent(data));
            bindAgent('agent.progress', (data) => this.handleProgressEvent(data));
            bindAgent('agent.done', (data) => this.handleDoneEvent(data));
            bindAgent('agent.error', (data) => this.handleErrorEvent(data));
            bindAgent('agent.message', (data) => this.handleMessageEvent(data));
            bindAgent('agent.report_ready', (data) => this.handleReportReady(data));
            bindAgent('agent.roam_started', (data) => this.handleRoamStarted(data));
            bindAgent('agent.roam_action', (data) => this.handleRoamAction(data));
            bindAgent('agent.roam_step', (data) => this.handleRoamStep(data));
            bindAgent('agent.roam_done', (data) => this.handleRoamDone(data));
            
            // ⭐ 人机协作模式：监听确认请求
            bindAgent('agent.confirm_selection', (data) => this.handleConfirmRequest(data));
            
            // ⭐ 人机协作模式：监听解读确认请求（右侧面板）
            bindAgent('agent.confirm_analysis', (data) => this.handleAnalysisConfirmRequest(data));
            
            // ⭐ 人机协作模式：监听"所有蓝框选取完成"事件（统一确认）
            bindAgent('agent.all_blue_selected', (data) => this.handleAllBlueSelected(data));
            
            // ⭐ 人机协作模式：监听"所有绿框选取完成"事件（统一确认）
            bindAgent('agent.all_green_selected', (data) => this.handleAllGreenSelected(data));
            
            // ⭐ Patient Level 模式：监听多WSI分析事件
            bindAgent('agent.patient_progress', (data) => this.handlePatientProgress(data));
            bindAgent('agent.patient_stopping', (data) => this.handlePatientStopping(data));
            bindAgent('agent.patient_stopped', (data) => this.handlePatientStopped(data));
            bindAgent('agent.patient_roam_started', (data) => this.handlePatientRoamEvent('started', data));
            bindAgent('agent.patient_roam_action', (data) => this.handlePatientRoamEvent('action', data));
            bindAgent('agent.patient_roam_step', (data) => this.handlePatientRoamEvent('step', data));
            bindAgent('agent.patient_roam_done', (data) => this.handlePatientRoamEvent('done', data));
            bindAgent('agent.patient_cache_check', (data) => this.handlePatientCacheCheck(data));
            bindAgent('agent.patient_level_all_cached', (data) => this.handlePatientLevelAllCached(data));
            bindAgent('agent.wsi_started', (data) => this.handleWsiStarted(data));
            bindAgent('agent.wsi_completed', (data) => this.handleWsiCompleted(data));
            bindAgent('agent.patient_report', (data) => this.handlePatientReport(data));
            bindAgent('agent.patient_slide_review_ready', (data) => this.handlePatientSlideReview(data));
            bindAgent('agent.patient_review_updated', (data) => this.handlePatientReviewUpdated(data));
            bindAgent('agent.patient_review_gate', (data) => this.handlePatientReviewGate(data));
            
            // ⭐ Patient Level 模式：监听红框分析结果事件
            bindAgent('agent.red_analysis', (data) => this.handleRedAnalysis(data));
            
            // ⭐ Patient Level 模式：监听蓝框分析结果事件
            bindAgent('agent.blue_analysis', (data) => this.handleBlueAnalysis(data));
            
            // ⭐ Patient Level 模式：监听绿框分析结果事件
            bindAgent('agent.green_analysis', (data) => this.handleGreenAnalysis(data));
            
            // ⭐⭐⭐ 五阶段协作模式事件监听 ⭐⭐⭐
            bindAgent('agent.phase_result', (data) => this.handlePhaseResult(data));
            bindAgent('agent.review_queue', (data) => this.handleReviewQueue(data));
            bindAgent('agent.phase2_progress', (data) => this.handlePhase2Progress(data));  // ⭐ 实时蓝框进度
            bindAgent('agent.phase3_progress', (data) => this.handlePhase3Progress(data));  // ⭐ 实时绿框进度
            bindAgent('agent.orchestrator_reply', (data) => this.handleOrchestratorReply(data));
            this.socket.on('confirmation_received', (data) => this.handleSelectionConfirmReceived(data));
            this.socket.on('phase_confirm_received', (data) => this.handlePhaseConfirmReceived(data));
            
            // ⭐ AI 评估修改结果事件
            bindAgent('agent.modification_review', (data) => this.handleModificationReview(data));
            this.socket.on('user_decision_received', (data) => this.handleUserDecisionReceived(data));
            
            // ⭐⭐⭐ AI 重新解读分析结果（引用消息功能）
            this.socket.on('reanalysis_result_received', (data) => this.handleReanalysisResult(data));

            // Direct collaborative edits are committed only after a scoped
            // server ACK. A disconnected/expired room must never look saved.
            this.socket.on('analysis_edit_received', (data) => this.handleAnalysisEditReceived(data));
            
            // ⭐⭐⭐ AI 修改诊断报告（Phase 5 引用功能）
            this.socket.on('report_revision_result', (data) => this.handleReportRevisionResult(data));
            
            // ⭐⭐⭐ Patient Level 协作模式：综合报告审阅确认
            bindAgent('agent.patient_report_review', (data) => this.handlePatientReportReview(data));
            
        } catch (error) {
            console.error('WebSocket初始化失败:', error);
        }
    }
    
    joinRoom() {
        if (this.socket && this.sessionId) {
            this.socket.emit('join', {
                room: this.sessionId,
                run_id: this.runtimeRunId
            });
            console.log('发送join请求:', this.sessionId);
        }
    }

    _runtimeReplayTitle(event) {
        const stage = String(event?.payload?.stage || '');
        const isEn = (window.__lang || 'zh') === 'en';
        const labels = {
            'legacy.v1.progress': ['分析进度已更新', 'Analysis progress updated'],
            'legacy.v1.viewport.updated': ['切片视野已更新', 'Slide viewport updated'],
            'legacy.v1.message': ['Agent 已更新分析', 'Agent analysis updated'],
            'legacy.v1.orchestrator.reply': ['主 Agent 已更新计划', 'Orchestrator plan updated'],
            'legacy.v1.roi.red.completed': ['红框 Agent 已完成一层解读', 'Red ROI Agent completed'],
            'legacy.v1.roi.blue.completed': ['蓝框 Subagent 已完成解读', 'Blue ROI Subagent completed'],
            'legacy.v1.roi.green.completed': ['绿框 Subagent 已完成解读', 'Green ROI Subagent completed'],
            'legacy.v1.wsi.started': ['全切片分析已开始', 'Whole-slide analysis started'],
            'legacy.v1.wsi.completed': ['全切片分析已完成', 'Whole-slide analysis completed'],
            'agent.v1.roam.started': ['单 Agent 游走已开始', 'Single Agent navigation started'],
            'agent.v1.roam.step': ['单 Agent 已观察一个视野', 'Single Agent inspected a FOV'],
            'agent.v1.roam.completed': ['单 Agent 游走已完成', 'Single Agent navigation completed'],
            'legacy.v1.report.ready': ['病理报告已生成', 'Pathology report is ready'],
            'legacy.v1.patient.report.ready': ['患者级报告已生成', 'Patient-level report is ready'],
            'legacy.v1.completed': ['分析已完成', 'Analysis completed'],
            'legacy.v1.error': ['分析发生错误', 'Analysis error']
        };
        const pair = labels[stage];
        if (pair) return pair[isEn ? 1 : 0];
        return String(event?.payload?.summary || (
            isEn ? 'Agent activity restored' : '已恢复 Agent 活动'
        ));
    }

    _applyRuntimeReplayEvent(event) {
        if (!event || !event.event_id || this.runtimeReplayEventIds.has(event.event_id)) {
            return;
        }
        this.runtimeReplayEventIds.add(event.event_id);
        const payload = event.payload || {};
        const stage = String(payload.stage || '');
        const details = payload.details || {};
        const sourceEvent = String(details.source_event || '');
        const terminal = (
            stage === 'legacy.v1.completed'
            || stage === 'legacy.v1.error'
            || sourceEvent === 'agent.done'
            || sourceEvent === 'agent.error'
        );
        const completed = (
            terminal
            || /\.completed$/.test(stage)
            || /\.ready$/.test(stage)
            || /\.result$/.test(stage)
        );
        const shouldOpenSubagentPane = (
            stage.includes('.roi.')
            || stage.includes('.phase.')
            || stage.includes('.wsi.')
            || stage.includes('.report.')
        );
        if (shouldOpenSubagentPane) this._activateSubagentUi();
        this._appendActivityItem({
            title: this._runtimeReplayTitle(event),
            detail: '',
            state: stage === 'legacy.v1.error'
                ? 'error'
                : (completed ? 'done' : 'running'),
            key: `runtime-replay:${event.event_id}`
        });
    }

    _workflowStateRank(state) {
        switch (String(state || '')) {
            case 'pending': return 0;
            case 'review':
            case 'running': return 1;
            case 'done':
            case 'stopped': return 2;
            case 'error': return 3;
            default: return -1;
        }
    }

    _mergeWorkflowState(current, incoming) {
        if (this._workflowStateRank(incoming) > this._workflowStateRank(current)) {
            return incoming;
        }
        return current || incoming;
    }

    _mapRuntimeAgentToWorkflowState(node, { isRoot = false } = {}) {
        const state = String(node?.state || '').toLowerCase();
        const waitReason = String(node?.wait_reason || '').toLowerCase();
        if (state === 'failed') return 'error';
        if (state === 'cancelled' || state === 'skipped') return 'stopped';
        if (state === 'succeeded') return 'done';
        if (isRoot) return 'running';
        if (state === 'waiting' && waitReason === 'children') return 'done';
        if (state === 'waiting' || state === 'running' || state === 'cancelling') {
            return 'running';
        }
        if (state === 'created' || state === 'blocked' || state === 'ready') {
            return 'pending';
        }
        return 'running';
    }

    _setWorkflowBoardVisible(visible) {
        const board = document.getElementById('v2SubagentBoard');
        if (board) board.hidden = !visible;
    }

    _applyRuntimeAgentTree(tree) {
        const roots = Array.isArray(tree?.agents) ? tree.agents : [];
        if (!roots.length) return;
        const root = roots.find(node => node.task_id === tree.root_task_id) || roots[0];
        if (root) {
            this.primaryAgentState = this._mergeWorkflowState(
                this.primaryAgentState,
                this._mapRuntimeAgentToWorkflowState(root, { isRoot: true })
            );
        }
        const visit = (node, isRootNode) => {
            const agentId = String(node?.agent_id || '').trim();
            if (!isRootNode && agentId) {
                const mapped = this._mapRuntimeAgentToWorkflowState(node, { isRoot: false });
                const merged = this._mergeWorkflowState(
                    this.subagentStates.get(agentId),
                    mapped
                );
                const stableBlueMatch = agentId.match(/^(.*)-B(\d+)$/);
                if (stableBlueMatch) {
                    this._consumePendingBlueAlias(
                        stableBlueMatch[1],
                        stableBlueMatch[2],
                        agentId
                    );
                }
                this.subagentStates.set(agentId, merged);
            }
            (node?.children || []).forEach(child => visit(child, false));
        };
        roots.forEach(node => visit(node, node === root));
        this.subagentExpectedCount = Math.max(
            this.subagentExpectedCount,
            this.subagentStates.size
        );
        this._activateSubagentUi();
        this._renderSubagentBoard();
    }

    async _restoreWorkflowFromRuntimeAgents(runId) {
        if (!runId) return;
        if (
            this.runtimeRunFileId
            && this.currentFileId
            && this.runtimeRunFileId !== this.currentFileId
        ) {
            this._setWorkflowBoardVisible(false);
            return;
        }
        const response = await fetch(
            `/api/agent/runtime/${encodeURIComponent(runId)}/agents`
        );
        if (!response.ok) return;
        const tree = await response.json();
        if (this.runtimeRunId !== runId) return;
        if (
            this.runtimeRunFileId
            && this.currentFileId
            && this.runtimeRunFileId !== this.currentFileId
        ) {
            this._setWorkflowBoardVisible(false);
            return;
        }
        this._setWorkflowBoardVisible(true);
        this._applyRuntimeAgentTree(tree);
    }

    _scheduleWorkflowRestore(runId = this.runtimeRunId) {
        if (!runId) return Promise.resolve();
        const fileId = this.currentFileId || this.runtimeRunFileId || '';
        const key = `${runId}::${fileId}`;
        if (this._workflowRestoreKey === key && this._workflowRestorePromise) {
            return this._workflowRestorePromise;
        }
        this._workflowRestoreKey = key;
        const promise = this._restoreWorkflowFromRuntimeAgents(runId)
            .catch((error) => {
                console.debug('Workflow snapshot restore skipped:', error);
            })
            .finally(() => {
                if (
                    this._workflowRestoreKey === key
                    && this._workflowRestorePromise === promise
                ) {
                    this._workflowRestorePromise = null;
                }
            });
        this._workflowRestorePromise = promise;
        return promise;
    }

    async _restoreCollaborationSnapshot(runId, expectedFileId) {
        const restoreEpoch = ++this._collaborationSnapshotEpoch;
        const liveSerialAtStart = this._collaborationLiveSerial;
        const response = await fetch(
            `/api/agent/runtime/${encodeURIComponent(runId)}/collaboration`,
            { cache: 'no-store' }
        );
        if (!response.ok) return;
        const snapshot = await response.json();
        if (
            restoreEpoch !== this._collaborationSnapshotEpoch
            || liveSerialAtStart !== this._collaborationLiveSerial
            ||
            this.runtimeRunId !== runId
            || String(this.currentFileId || this.runtimeRunFileId || '') !== String(expectedFileId || '')
            || snapshot.run_id !== runId
            || snapshot.active !== true
            || String(snapshot.file_id || '') !== String(expectedFileId || '')
        ) return;

        this.currentMode = 'collaborative';
        this.isCollaborativeMode = true;
        this.activeAnalysisStrategy = 'agent_swarm';
        this.collaborationReviewPolicy = snapshot.review_policy === 'final'
            ? 'final' : 'staged';

        // Rebuild the public Phase 1–4 ROI rows before restoring the current
        // approval gate. These are the exact payloads already shown live and
        // are rendered as completed, never as duplicate approval requests.
        const history = snapshot.phase_history && typeof snapshot.phase_history === 'object'
            ? snapshot.phase_history : {};
        const pendingPhaseNumber = (
            snapshot.phase_status === 'pending'
            && snapshot.phase
            && Number.isInteger(Number(snapshot.phase.phase))
        ) ? Number(snapshot.phase.phase) : 0;
        Object.keys(history)
            .map((key) => Number(key))
            .filter((phaseNumber) => Number.isInteger(phaseNumber) && phaseNumber >= 1 && phaseNumber <= 4)
            .sort((a, b) => a - b)
            .forEach((phaseNumber) => {
                // The current pending phase is rendered once below with its
                // live gate. Rendering the history copy first creates a
                // duplicate, inert Phase card after refresh.
                if (phaseNumber === pendingPhaseNumber) return;
                const payload = history[String(phaseNumber)];
                if (!payload || typeof payload !== 'object') return;
                this.handlePhaseResult({
                    ...payload,
                    file_id: snapshot.file_id,
                    requires_confirmation: false,
                    phase_gate_id: '',
                    runtime: { run_id: runId },
                    _restored_from_snapshot: true,
                    _restored_phase_history: true,
                });
            });

        if (this._reviewQueueIsPublished(snapshot, snapshot.review_queue)) {
            this.handleReviewQueue({
                items: Array.isArray(snapshot.review_queue) ? snapshot.review_queue : [],
                file_id: snapshot.file_id,
                review_policy: snapshot.review_policy,
                review_queue_published: true,
                published: true,
                runtime: { run_id: runId },
                _restored_from_snapshot: true,
            });
        }
        if (
            snapshot.phase
            && snapshot.phase_status === 'pending'
            && snapshot.requires_confirmation === true
            && snapshot.phase_gate_id
        ) {
            const phase = {
                ...snapshot.phase,
                file_id: snapshot.file_id,
                requires_confirmation: true,
                phase_gate_id: snapshot.phase_gate_id,
                runtime: { run_id: runId },
                _restored_from_snapshot: true,
            };
            this.handlePhaseResult(phase);
            if (snapshot.phase_submission_in_flight === true) {
                const card = document.querySelector(
                    `.phase-confirm-message[data-gate-id="${CSS.escape(snapshot.phase_gate_id)}"]`
                );
                if (card) {
                    this._setPhaseConfirmationSubmitting(card, true);
                    card.setAttribute('data-confirm-state', 'submitting');
                    const isEn = (window.__lang || 'zh') === 'en';
                    const status = card.querySelector('.phase-submission-status')
                        || document.createElement('p');
                    status.className = 'phase-submission-status';
                    status.textContent = isEn
                        ? 'Server is evaluating your proposed changes…'
                        : '服务器正在评估您的修改…';
                    card.querySelector('.message-content')?.appendChild(status);
                }
            }
        }
    }

    async _restoreRoamSnapshot(runId, expectedFileId) {
        const restoreEpoch = ++this._roamSnapshotEpoch;
        const response = await fetch(
            `/api/agent/runtime/${encodeURIComponent(runId)}/roam`,
            { cache: 'no-store' }
        );
        if (!response.ok) return;
        const snapshot = await response.json();
        if (
            restoreEpoch !== this._roamSnapshotEpoch
            ||
            this.runtimeRunId !== runId
            || String(this.currentFileId || this.runtimeRunFileId || '') !== String(expectedFileId || '')
            || snapshot.run_id !== runId
            || snapshot.active !== true
            || snapshot.analysis_strategy !== 'single_agent_roam'
            || String(snapshot.file_id || '') !== String(expectedFileId || '')
        ) return;

        const timeline = Array.isArray(snapshot.timeline) ? snapshot.timeline : [];
        const snapshotDeclaredMaxStep = timeline.reduce(
            (maximum, entry) => Math.max(
                maximum,
                Number(entry?.payload?.step) || 0
            ),
            Number(snapshot.current_action?.step) || 0
        );
        const liveStepBeforeRestore = this._roamMaxStepByRun.get(String(runId)) || 0;
        if (liveStepBeforeRestore > snapshotDeclaredMaxStep) return;

        this.handleRoamStarted({
            ...(snapshot.started || {}),
            file_id: snapshot.file_id,
            runtime: { run_id: runId },
            _restored_from_snapshot: true,
        });
        let lastRect = null;
        let lastZoom = null;
        let snapshotMaxStep = 0;
        timeline.forEach((entry) => {
            const payload = {
                ...(entry?.payload || {}),
                runtime: { run_id: runId },
                _restored_from_snapshot: true,
                _restored_no_animation: true,
            };
            if (entry?.kind === 'action') this.handleRoamAction(payload);
            if (entry?.kind === 'step') this.handleRoamStep(payload);
            snapshotMaxStep = Math.max(snapshotMaxStep, Number(payload.step) || 0);
            if (payload.rect) {
                lastRect = payload.rect;
                lastZoom = payload.zoom;
            }
        });
        const currentAction = snapshot.current_action;
        if (currentAction?.rect) {
            lastRect = currentAction.rect;
            lastZoom = currentAction.zoom;
            snapshotMaxStep = Math.max(snapshotMaxStep, Number(currentAction.step) || 0);
        }
        const liveMaxStep = this._roamMaxStepByRun.get(String(runId)) || 0;
        if (
            restoreEpoch === this._roamSnapshotEpoch
            && liveMaxStep <= snapshotMaxStep
            && lastRect
        ) this._animateRoamViewport(lastRect, lastZoom);
    }

    async syncRuntimeAfterReconnect() {
        if (!this.runtimeRunId) return;
        try {
            const runId = this.runtimeRunId;
            const expectedFileId = String(
                this.currentFileId || this.runtimeRunFileId || ''
            );
            const isCurrent = () => Boolean(
                this.runtimeRunId === runId
                && String(this.currentFileId || this.runtimeRunFileId || '') === expectedFileId
            );
            const statusResponse = await fetch(
                `/api/agent/runtime/${encodeURIComponent(runId)}/status`
            );
            if (statusResponse.status === 404) {
                this._clearRuntimeRun();
                return;
            }
            if (!statusResponse.ok) return;
            const statusPayload = await statusResponse.json();
            if (!isCurrent()) return;
            const state = statusPayload.runtime?.run_state;
            const startedTerminal = this._isRuntimeTerminalState(state);
            if (!startedTerminal) {
                await this._rebindManagedRuntimeIfNeeded(
                    statusPayload.runtime
                );
                if (!isCurrent()) return;
                if (!this.activityStartedAt) this._startChatActivity();
                this.isRunning = true;
                this.isStarting = false;
                window.pathchat?._invalidateHistoryChooser?.('runtime_restored');
                this.isStopping = Boolean(statusPayload.runtime?.cancel_requested);
                this.updateButtons();
                await this._scheduleWorkflowRestore(runId);
                if (!isCurrent()) return;
                await this._restoreRoamSnapshot(runId, expectedFileId);
                if (!isCurrent()) return;
                await this._restoreCollaborationSnapshot(runId, expectedFileId);
                if (!isCurrent()) return;
            }

            // Events can include INTERNAL rows that are intentionally omitted
            // from the response. The server cursor still advances over them.
            // Follow has_more until the high watermark is reached.
            let hasMore = true;
            let pages = 0;
            while (hasMore && pages < 100) {
                const query = new URLSearchParams({
                    after_seq: String(this.runtimeEventCursor || 0),
                    limit: '500'
                });
                const eventsResponse = await fetch(
                    `/api/agent/runtime/${encodeURIComponent(runId)}/events?${query}`
                );
                if (!eventsResponse.ok) break;
                const eventsPayload = await eventsResponse.json();
                if (!isCurrent()) return;
                (eventsPayload.events || []).forEach(
                    event => this._applyRuntimeReplayEvent(event)
                );
                const nextCursor = Number(eventsPayload.next_after_seq);
                if (Number.isInteger(nextCursor)) {
                    if (nextCursor <= this.runtimeEventCursor && eventsPayload.has_more) {
                        break;
                    }
                    this.runtimeEventCursor = Math.max(
                        this.runtimeEventCursor,
                        nextCursor
                    );
                    this._persistRuntimeState();
                }
                hasMore = Boolean(eventsPayload.has_more);
                pages += 1;
            }

            if (this.runtimeRunId !== runId) return;
            if (!startedTerminal) {
                this._workflowRestoreKey = null;
                this._workflowRestorePromise = null;
                await this._scheduleWorkflowRestore(runId);
                if (!isCurrent()) return;
                // Re-read the collaboration snapshot after the Runtime cursor
                // is caught up.  Combined with waiting for the room-join ACK,
                // this closes the refresh gap around a newly-created phase
                // gate or final review queue.
                await this._restoreCollaborationSnapshot(runId, expectedFileId);
                if (!isCurrent()) return;
            }
            let nowTerminal = startedTerminal;
            if (!startedTerminal) {
                const finalStatusResponse = await fetch(
                    `/api/agent/runtime/${encodeURIComponent(runId)}/status`
                );
                if (!isCurrent()) return;
                if (finalStatusResponse.ok) {
                    const finalPayload = await finalStatusResponse.json();
                    if (!isCurrent()) return;
                    nowTerminal = this._isRuntimeTerminalState(
                        finalPayload.runtime?.run_state
                    );
                }
            }
            if (nowTerminal) {
                if (!isCurrent()) return;
                this.isRunning = false;
                this.isStarting = false;
                this.isStopping = false;
                this.updateButtons();
                this._clearRuntimeRun();
            }
        } catch (error) {
            // Live Socket.IO remains the mature UI path. Runtime replay is an
            // additive reconnect aid and must not interrupt slide analysis.
            console.debug('Runtime reconnect sync skipped:', error);
        }
    }
    
    detectViewer() {
        // 检测OpenSeadragon viewer
        const checkViewer = () => {
            if (window.pathchat && window.pathchat.viewer) {
                this.viewer = window.pathchat.viewer;
                console.log('检测到OpenSeadragon viewer');
                this.setupViewerEvents();
            } else {
                // 每500ms检查一次
                setTimeout(checkViewer, 500);
            }
        };
        checkViewer();
    }
    
    // 外部调用接口，用于pathchat.js设置viewer
    setViewer(viewer) {
        this.viewer = viewer;
        console.log('通过setViewer设置OpenSeadragon viewer');
        if (viewer) {
            this.setupViewerEvents();
        }
    }
    
    // 外部调用接口，用于设置当前文件ID
    setCurrentFile(fileId) {
        const previousFileId = this.currentFileId;
        if (previousFileId && previousFileId !== fileId) {
            this._cancelRoamViewportAnimation();
            this.isFollowing = false;
            this._clearRoamFollowResumeControl();
            this._clearCollaborativeReviewHighlights();
        }
        this.currentFileId = fileId || null;
        this._syncReviewQueueActions();
        this._restorePatientRoamWhenViewerReady(this.currentFileId);
        if (window.pathchat?.currentPatientId) {
            this.restorePatientLevelSnapshot(window.pathchat.currentPatientId);
        }
        console.log('设置当前文件ID:', fileId);
        if (!fileId || !this.runtimeRunId) return;
        if (
            !this.isPatientLevelMode
            && this.runtimeRunFileId
            && this.runtimeRunFileId !== fileId
        ) {
            this._setWorkflowBoardVisible(false);
            return;
        }
        this._setWorkflowBoardVisible(true);
        this._scheduleWorkflowRestore(this.runtimeRunId);
        const cachedReview = this._reviewQueuesByFile.get(String(fileId));
        if (cachedReview) {
            this.handleReviewQueue({ ...cachedReview, _restored_from_cache: true });
        }
    }
    
    setupViewerEvents() {
        if (this.viewer) {
            if (this._viewerEventsBoundTo === this.viewer) return;
            if (this._viewerEventsBoundTo) {
                try {
                    this._viewerEventsBoundTo.removeHandler('pan', this._viewerPanHandler);
                    this._viewerEventsBoundTo.removeHandler('zoom', this._viewerZoomHandler);
                } catch (error) {
                    console.debug('旧查看器事件解绑已跳过:', error);
                }
            }
            if (this._viewerRawIntentTarget && this._viewerRawIntentHandler) {
                this._viewerRawIntentTarget.removeEventListener(
                    'pointerdown', this._viewerRawIntentHandler, true
                );
                this._viewerRawIntentTarget.removeEventListener(
                    'wheel', this._viewerRawIntentHandler, true
                );
            }
            this._viewerPanHandler = () => {
                if (this._programmaticViewportMove) return;
                if (this.isFollowing && (this.isRunning || this._activePatientRoamRunToken)) {
                    this.temporarilyPauseFollow();
                }
            };
            this._viewerZoomHandler = () => {
                if (this._programmaticViewportMove) return;
                if (this.isFollowing && (this.isRunning || this._activePatientRoamRunToken)) {
                    this.temporarilyPauseFollow();
                }
            };
            // 监听用户手动操作
            this.viewer.addHandler('pan', this._viewerPanHandler);
            this.viewer.addHandler('zoom', this._viewerZoomHandler);
            this._viewerEventsBoundTo = this.viewer;

            // OpenSeadragon emits the same pan/zoom callbacks for its own
            // spring animation and for a real hand gesture.  During an
            // automatic roam animation the callbacks are intentionally
            // filtered, so listen one level lower for actual user intent.
            // A pointer or wheel gesture must always win immediately.
            const rawTarget = this.viewer.canvas
                || document.getElementById('openseadragon-viewer');
            if (rawTarget) {
                this._viewerRawIntentHandler = () => {
                    if (
                        (this.isRunning || this._activePatientRoamRunToken)
                        && this.isFollowing
                        && this.activeAnalysisStrategy === 'single_agent_roam'
                    ) this.temporarilyPauseFollow();
                };
                rawTarget.addEventListener(
                    'pointerdown', this._viewerRawIntentHandler, true
                );
                rawTarget.addEventListener(
                    'wheel', this._viewerRawIntentHandler,
                    { capture: true, passive: true }
                );
                this._viewerRawIntentTarget = rawTarget;
            }
            
            // ⭐ 监听红框点击事件（通过事件委托）
            this.setupRedFrameClickHandler();
        }
    }
    
    // ⭐ 设置地图框点击事件处理（红框+蓝框+绿框）
    setupRedFrameClickHandler() {
        const viewerElement = document.getElementById('openseadragon-viewer');
        if (!viewerElement) return;
        if (viewerElement.dataset.roiClickHandlerBound === 'true') return;
        viewerElement.dataset.roiClickHandlerBound = 'true';
        
        // 监听所有overlay点击
        viewerElement.addEventListener('click', (e) => {
            // ⭐⭐⭐ 关键修复：如果处于协作模式的编辑状态（增加/删除），跳过红框点击处理
            // 防止在添加蓝框时点击红框区域导致界面跳转回红框阶段
            // ⭐ 同时检查 editMode（单步编辑）和 mapEditMode（协作模式阶段编辑）
            if (this.editMode === 'add' || this.editMode === 'delete' || this.mapEditMode) {
                console.log(`[Map] 跳过红框点击处理，当前处于编辑模式: editMode=${this.editMode}, mapEditMode=${this.mapEditMode}`);
                return;  // 不处理，让 _mapClickHandler 或 handleMapClickForAddROI 处理
            }
            
            const overlay = e.target.closest('[data-roi-id]');
            if (overlay) {
                const roiId = overlay.dataset.roiId;
                console.log('点击WSI上的区域:', roiId);
                
                // ⭐ 扩展：高亮左侧对应的卡片（routeStepsList）
                this.highlightLeftPanelCard(roiId);
                
                // 判断是红框还是子框
                if (roiId && roiId.startsWith('roi_10x_r') && !roiId.includes('_sub_')) {
                    this.handleRedFrameClick(roiId);
                }
            }
        });
        
        console.log('地图框点击事件处理器已设置');
    }
    
    _normalizeMappedRoiId(id) {
        return String(id || '')
            .replace(/^(editable-region-|overlay-|roi-overlay-)/i, '')
            .trim()
            .toLowerCase();
    }

    _parseMappedRoiRect(value) {
        if (!value) return null;
        if (typeof value === 'string') {
            try { return JSON.parse(value); } catch (_) { return null; }
        }
        return typeof value === 'object' ? value : null;
    }

    _sameMappedRoiRect(a, b) {
        if (window.pathchat && typeof window.pathchat._sameRoiRect === 'function') {
            return window.pathchat._sameRoiRect(a, b);
        }
        if (!a || !b) return false;
        const ax = Number(a.x ?? a.left);
        const ay = Number(a.y ?? a.top);
        const aw = Number(a.w ?? a.width);
        const ah = Number(a.h ?? a.height);
        const bx = Number(b.x ?? b.left);
        const by = Number(b.y ?? b.top);
        const bw = Number(b.w ?? b.width);
        const bh = Number(b.h ?? b.height);
        if (![ax, ay, aw, ah, bx, by, bw, bh].every(Number.isFinite)) return false;
        return Math.abs(ax - bx) < 1 && Math.abs(ay - by) < 1
            && Math.abs(aw - bw) < 1 && Math.abs(ah - bh) < 1;
    }

    _isSimpleRoiId(id) {
        return /^R\d+(?:-B\d+(?:-G\d+)?)?$/i.test(String(id || '').trim());
    }

    _preferCanonicalRoiId(item, fallback) {
        const primary = item?.data?.id || item?.id || fallback;
        if (this._isSimpleRoiId(primary)) return primary;
        const rect = item?.data?.rect || this._parseMappedRoiRect(item?.element?.dataset?.rect);
        const list = document.getElementById('routeStepsList');
        if (!list || !rect) return primary || fallback;
        const nodes = list.querySelectorAll(
            '.route-tree-node[data-roi-id], .route-tree-blue-node[data-roi-id], .route-tree-green-item[data-roi-id], .route-step-card[data-roi-id]'
        );
        for (const node of nodes) {
            const nodeRect = this._parseMappedRoiRect(node.dataset.rect);
            if (nodeRect && this._sameMappedRoiRect(rect, nodeRect)) {
                return node.dataset.roiId || primary || fallback;
            }
        }
        return primary || fallback;
    }

    _canonicalMappedRoiId(roiId) {
        const want = this._normalizeMappedRoiId(roiId);
        if (!want) return roiId;
        const overlays = window.pathchat?.roiOverlays || [];
        for (const item of overlays) {
            const aliases = [
                item?.id,
                item?.data?.id,
                item?.data?.original_id,
                item?.element?.dataset?.roiId,
                item?.element?.dataset?.originalId
            ];
            if (aliases.some((alias) => this._normalizeMappedRoiId(alias) === want)) {
                return this._preferCanonicalRoiId(item, roiId);
            }
        }
        const tree = this._findMappedTreeCard(roiId);
        const treeRect = this._parseMappedRoiRect(tree?.dataset?.rect);
        if (treeRect) {
            const twin = overlays.find((item) => this._sameMappedRoiRect(item?.data?.rect, treeRect));
            if (twin) return this._preferCanonicalRoiId(twin, tree.dataset.roiId || roiId);
        }
        return roiId;
    }

    _escapeRoiSelector(roiId) {
        const raw = String(roiId || '');
        if (window.CSS && typeof window.CSS.escape === 'function') return CSS.escape(raw);
        return raw.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    }

    _isRoamMappedRoiId(roiId) {
        return /^(ROAM-|PATIENT-ROAM-)/i.test(String(roiId || '').trim());
    }

    // Viewer overlays only.  Tree / activity cards also carry data-roi-id, so a
    // document-wide querySelector often returns the panel node (no rect) and
    // blue-box jumps silently fail.
    _resolveRoiOverlayRecord(roiId) {
        const want = this._normalizeMappedRoiId(roiId);
        if (!want) return null;
        const overlays = window.pathchat?.roiOverlays || [];
        const fromList = overlays.find((item) => (
            this._normalizeMappedRoiId(item?.id) === want
            || this._normalizeMappedRoiId(item?.data?.id) === want
            || this._normalizeMappedRoiId(item?.data?.original_id) === want
            || this._normalizeMappedRoiId(item?.element?.dataset?.originalId) === want
        ));
        if (fromList) return fromList;
        const tree = this._findMappedTreeCard(roiId);
        const treeRect = this._parseMappedRoiRect(tree?.dataset?.rect);
        if (treeRect) {
            const byRect = overlays.find((item) => this._sameMappedRoiRect(item?.data?.rect, treeRect));
            if (byRect) return byRect;
        }
        const escaped = this._escapeRoiSelector(this._canonicalMappedRoiId(roiId) || roiId);
        const el = document.querySelector(`.roi-overlay[data-roi-id="${escaped}"]`);
        if (!el) return null;
        let rect = null;
        if (el.dataset.rect) {
            try { rect = JSON.parse(el.dataset.rect); } catch (_) {}
        }
        return { id: roiId, element: el, data: { id: roiId, rect } };
    }

    _resolveRoiRect(roiId, fallback = null) {
        const hasFallback = fallback && typeof fallback === 'object'
            && (fallback.x !== undefined || fallback.left !== undefined);
        if (hasFallback) return fallback;
        const rec = this._resolveRoiOverlayRecord(roiId);
        let rect = rec?.data?.rect || null;
        if (!rect && rec?.element?.dataset?.rect) {
            try { rect = JSON.parse(rec.element.dataset.rect); } catch (_) {}
        }
        return rect || null;
    }

    _findMappedTreeCard(roiId) {
        const list = document.getElementById('routeStepsList');
        if (!list || !roiId) return null;
        return this._findTreeNodeByRoiId(list, roiId, '.collab-task-card[data-roi-id]')
            || this._findTreeNodeByRoiId(list, roiId, '.route-tree-green-item[data-roi-id]')
            || this._findTreeNodeByRoiId(list, roiId, '.route-tree-blue-node[data-roi-id]')
            || this._findTreeNodeByRoiId(list, roiId, '.route-tree-node[data-roi-id]')
            || this._findTreeNodeByRoiId(list, roiId, '.route-step-card[data-roi-id]')
            || this._findTreeNodeByRoiId(list, roiId, '.collab-tree-node[data-roi-id]');
    }

    _expandMappedTreeAncestors(card) {
        let node = card;
        const list = document.getElementById('routeStepsList');
        while (node && node !== list) {
            node.querySelectorAll(':scope > .route-tree-header, :scope > .route-tree-blue-header')
                .forEach((header) => header.classList.remove('collapsed'));
            node = node.parentElement?.closest('.route-tree-node, .route-tree-blue-node');
        }
    }

    _treeCardIsHighlighted(card) {
        if (!card) return false;
        if (card.classList.contains('roi-selected-glow')
            || card.classList.contains('active-viewing')
            || card.classList.contains('card-active')) {
            return true;
        }
        let header = null;
        if (card.classList.contains('route-tree-blue-node')) {
            header = card.querySelector('.route-tree-blue-header');
        } else if (card.classList.contains('route-tree-node')) {
            header = card.querySelector('.route-tree-header');
        }
        if (header && (
            header.classList.contains('roi-selected-glow')
            || header.classList.contains('active-viewing')
        )) {
            return true;
        }
        // 高亮经常落在正文（内容超过 40 字时 highlightTarget 是 content），
        // 不检查正文会导致「再点同一卡片无法取消高亮」。
        const body = card.querySelector(
            ':scope > .route-tree-content, :scope > .route-tree-blue-content, :scope > .route-tree-green-content'
        );
        return Boolean(body && (
            body.classList.contains('roi-selected-glow')
            || body.classList.contains('active-viewing')
        ));
    }

    _backfillTreeNodeRectsFromOverlays(overlays = []) {
        const list = document.getElementById('routeStepsList');
        if (!list || !overlays.length) return;
        overlays.forEach((overlay) => {
            const id = overlay?.id;
            const rect = overlay?.rect;
            if (!id || !rect) return;
            const node = this._findMappedTreeCard(id)
                || this._findMappedTreeCard(overlay.original_id);
            if (node && !node.dataset.rect) {
                try { node.dataset.rect = JSON.stringify(rect); } catch (_) {}
            }
        });
    }

    _roamOverlayId(step, data = {}) {
        const n = Number(step) || 0;
        if (n <= 0) return '';
        if (data?.wsi_index !== undefined && data?.wsi_index !== null && String(data.wsi_index) !== '') {
            return `PATIENT-ROAM-${data.wsi_index}-${n}`;
        }
        return `ROAM-${n}`;
    }

    _hydrateRoamActivityMapping(container = document.getElementById('routeStepsList')) {
        this._tagPatientActivityItems(container);
        const scope = container || document;
        scope.querySelectorAll?.('.v2-agent-activity-item')?.forEach((item) => {
            const title = item.querySelector('strong')?.textContent || '';
            const match = title.match(/FOV\s+(\d+)/i) || title.match(/视野\s+(\d+)/);
            const step = Number(match?.[1] || 0);
            const wsi = this._parsePatientActivityWsiIndex(item);
            if (Number.isInteger(wsi)) item.dataset.wsiIndex = String(wsi);
            const wsiPayload = Number.isInteger(wsi) ? { wsi_index: wsi } : {};
            if (item.dataset.roiId) {
                if (Number.isInteger(wsi) && step > 0 && /^ROAM-\d+$/i.test(item.dataset.roiId)) {
                    item.dataset.roiId = this._roamOverlayId(step, wsiPayload);
                }
                return;
            }
            if (step <= 0) return;
            item.dataset.roiId = this._roamOverlayId(step, wsiPayload);
            item.dataset.roamKind = /inspect|观察/.test(title)
                ? 'step'
                : (/Navigate|前往/.test(title) ? 'action' : 'step');
        });
    }

    _findRoamActivityItem(roiId) {
        this._hydrateRoamActivityMapping();
        if (!roiId) return null;
        const want = this._normalizeMappedRoiId(roiId);
        if (!want) return null;
        // Patient roam keeps one activity list per slide. querySelector() would
        // always hit slide 1's hidden list, so clicks on slide 2/3 never glow.
        const lists = Array.from(document.querySelectorAll(
            '#routeStepsList .v2-agent-activity-list, .v2-agent-activity-list'
        ));
        if (!lists.length) return null;
        const patientMatch = String(roiId).match(/^PATIENT-ROAM-(\d+)-/i);
        const mappedWsi = patientMatch
            ? Number(patientMatch[1])
            : this._asPatientWsiIndex(this.currentViewingWsiIndex);
        const preferred = Number.isInteger(mappedWsi)
            ? lists.filter((list) => {
                const map = list.closest('.patient-slide-map');
                return map && Number(map.dataset.wsiIndex) === mappedWsi;
            })
            : [];
        for (const list of (preferred.length ? preferred : lists)) {
            const matches = Array.from(list.querySelectorAll('.v2-agent-activity-item[data-roi-id]'))
                .filter((el) => this._normalizeMappedRoiId(el.dataset.roiId) === want);
            const hit = matches.find((el) => el.dataset.roamKind === 'step')
                || matches[matches.length - 1];
            if (hit) return hit;
        }
        return null;
    }

    _jumpToRoamOverlay(roiId, sourceEl = null) {
        const pc = window.pathchat;
        if (!pc || typeof pc.jumpToROIView !== 'function' || !roiId) return;
        const patientMatch = String(roiId).match(/^PATIENT-ROAM-(\d+)-/i);
        const mappedWsi = patientMatch
            ? Number(patientMatch[1])
            : this._parsePatientActivityWsiIndex(sourceEl);
        if (
            this.isPatientLevelMode
            && Number.isInteger(mappedWsi)
            && Number(this.currentViewingWsiIndex) !== mappedWsi
            && typeof pc.selectPlWsiItem === 'function'
        ) {
            pc.selectPlWsiItem(mappedWsi);
        }
        const want = this._normalizeMappedRoiId(roiId);
        const overlay = (pc.roiOverlays || []).find((item) => (
            this._normalizeMappedRoiId(item?.id) === want
            || this._normalizeMappedRoiId(item?.data?.id) === want
        ));
        if (overlay?.data?.rect) {
            pc.jumpToROIView(overlay.data);
            return;
        }
        const raw = sourceEl?.dataset?.rect;
        if (!raw) return;
        try {
            pc.jumpToROIView({ id: roiId, rect: JSON.parse(raw) });
        } catch (_) {}
    }

    _highlightRoamActivityItem(roiId) {
        const target = this._findRoamActivityItem(roiId);
        if (!target) return false;
        const siblings = document.querySelectorAll('.v2-agent-activity-item');
        const already = target.classList.contains('roi-selected-glow')
            || target.classList.contains('active-viewing');
        siblings.forEach((el) => {
            el.classList.remove('roi-selected-glow', 'active-viewing', 'active', 'card-active', 'flash-highlight');
        });
        if (already) {
            console.log(`[Map→Left] 取消单 Agent 进度高亮: ${roiId}`);
            return true;
        }
        const root = target.closest('#v2AgentActivity, .v2-root-agent-node');
        if (root?.classList.contains('is-collapsed')) {
            root.classList.remove('is-collapsed');
            root.querySelector('.v2-root-agent-header')?.setAttribute('aria-expanded', 'true');
        }
        target.classList.add('roi-selected-glow', 'active-viewing');
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        target.classList.add('flash-highlight');
        setTimeout(() => target.classList.remove('flash-highlight'), 800);
        console.log(`[Map→Left] 高亮单 Agent 进度: ${roiId}`);
        return true;
    }

    // ⭐ 高亮左侧面板对应的卡片（点击地图框时调用）
    highlightLeftPanelCard(roiId, options = {}) {
        console.log(`[Map→Left] 查找卡片: roiId=${roiId}`);
        roiId = this._canonicalMappedRoiId(roiId) || roiId;

        // 单 Agent 的 ROAM 进度才走 activity item。Swarm 蓝/绿框若先命中
        // activity，会抢走树节点；蓝框父节点里若已有高亮绿框，还会被误判成 toggle。
        if (this._isRoamMappedRoiId(roiId) && this._highlightRoamActivityItem(roiId)) return;

        // Patient Swarm / collab maps live in #routeStepsList. The left-rail
        // .analysis-tree is hidden, so only fall back to it when the middle
        // map has no matching node.
        const mappedSwarmCard = this._findMappedTreeCard(roiId);
        if (!mappedSwarmCard) {
            const analysisTree = document.querySelector('.analysis-tree');
            const plPanel = document.getElementById('patientLevelPanel');
            const isPatientLevelMode = (analysisTree && analysisTree.children.length > 0)
                || (plPanel && plPanel.offsetParent !== null);
            if (isPatientLevelMode) {
                console.log(`[Map→Left] 中间栏无树节点，回退 highlightPatientLevelTreeNode`);
                this.highlightPatientLevelTreeNode(roiId);
                return;
            }
        }
        
        const routeStepsList = document.getElementById('routeStepsList');
        if (!routeStepsList) return;
        
        // ⭐ 辅助函数：规范化 roi_id 进行比较
        const normalizeRoiId = (id) => {
            if (!id) return '';
            // 移除常见前缀，统一格式
            return id.replace('editable-region-', '')
                     .replace('overlay-', '')
                     .replace('roi-overlay-', '')
                     .replace('manual-', 'manual_')  // 统一 manual 格式
                     .toLowerCase();
        };
        
        // ⭐ 辅助函数：检查两个 roi_id 是否匹配（只用于精确匹配）
        const roiIdMatch = (id1, id2) => {
            if (!id1 || !id2) return false;
            const n1 = normalizeRoiId(id1);
            const n2 = normalizeRoiId(id2);
            return n1 === n2;  // 只精确匹配，不使用 includes
        };
        
        // 查找匹配的卡片（支持多种匹配方式）
        let targetCard = null;
        const normalizedClickId = normalizeRoiId(roiId);
        
        // ⭐⭐⭐ 优先查找最新阶段的分析结果（.collab-tree-node），而不是早期阶段的列表项（.region-item）
        // 这样点击红框时会链接到 Phase 2 的分析结果，而不是 Phase 1 的选中区域列表
        
        // 1. 首先在最新的 phase 卡片中查找 .collab-tree-node（分析结果）
        const phaseCards = routeStepsList.querySelectorAll('.phase-result-card');
        if (phaseCards.length > 0) {
            // 从最新的 phase 开始查找（倒序遍历）
            for (let i = phaseCards.length - 1; i >= 0; i--) {
                const phaseCard = phaseCards[i];
                // 优先查找分析结果节点（.collab-tree-node）
                const treeNode = phaseCard.querySelector(`.collab-tree-node[data-roi-id="${roiId}"]`);
                if (treeNode) {
                    targetCard = treeNode;
                    console.log(`[Map→Left] 方法0: 在 Phase ${phaseCard.dataset.phase} 找到分析结果节点`);
                    break;
                }
            }
        }
        
        // 2. Swarm 树节点：精确匹配绿/蓝/红，避免命中 header 或 activity 碎片
        if (!targetCard) {
            targetCard = this._findMappedTreeCard(roiId);
            if (targetCard) {
                console.log(`[Map→Left] 方法1: 树节点精确匹配成功`);
            }
        }

        // Swarm R/B/G cards hold the full analysis. Activity items are 220-char
        // progress stubs and must not steal the click.
        if (!targetCard && !this._isSimpleRoiId(roiId) && this._highlightRoamActivityItem(roiId)) return;
        
        // 3. 如果没找到，尝试大小写不敏感的精确匹配
        if (!targetCard) {
            const allCards = routeStepsList.querySelectorAll('[data-roi-id]');
            for (const card of allCards) {
                if (card.classList.contains('v2-agent-activity-item')) continue;
                const cardRoiId = card.dataset.roiId;
                if (roiIdMatch(cardRoiId, roiId)) {
                    targetCard = card;
                    console.log(`[Map→Left] 方法2: 大小写不敏感匹配成功: ${cardRoiId}`);
                    break;
                }
            }
        }
        
        // 3. ⭐ 如果仍没找到，可能蓝框在红框的子容器中，尝试查找嵌套结构
        if (!targetCard) {
            // 解析层级：R6-B6 -> parent=R6, R6-B6-G3 -> parent=R6-B6
            const parts = roiId.split('-');
            if (parts.length >= 2) {
                // 这是蓝框或绿框，查找父节点下的子元素
                const parentId = parts.slice(0, -1).join('-');  // R6-B6 -> R6, R6-B6-G3 -> R6-B6
                const parentNode = routeStepsList.querySelector(`[data-tree-id="${parentId}"], [data-roi-id="${parentId}"]`);
                if (parentNode) {
                    // 在父节点的子容器中查找
                    targetCard = parentNode.querySelector(`[data-roi-id="${roiId}"]`);
                    if (targetCard) {
                        console.log(`[Map→Left] 方法3: 在父节点 ${parentId} 下找到子节点 ${roiId}`);
                    }
                }
            }
        }
        
        // 5. ⭐ 最后尝试：如果点击的是子框但卡片还没创建，高亮其父节点
        if (!targetCard) {
            const parts = roiId.split('-');
            if (parts.length >= 2) {
                // 正确计算父节点：R6-B6 -> R6，R6-B8-G5 -> R6-B8
                const parentId = parts.slice(0, -1).join('-');
                targetCard = routeStepsList.querySelector(`[data-roi-id="${parentId}"], [data-tree-id="${parentId}"]`);
                if (targetCard) {
                    console.log(`[Map→Left] 方法5: 子节点 ${roiId} 尚未创建，暂时高亮父节点 ${parentId}`);
                } else if (parts.length >= 3) {
                    // 如果父节点（蓝框）也不存在，尝试祖父节点（红框）
                    const grandparentId = parts[0];
                    targetCard = routeStepsList.querySelector(`[data-roi-id="${grandparentId}"], [data-tree-id="${grandparentId}"]`);
                    if (targetCard) {
                        console.log(`[Map→Left] 方法5b: 父节点 ${parentId} 也不存在，高亮祖父节点 ${grandparentId}`);
                    }
                }
            }
        }
        
        // 6. ⭐ 协作模式：检查当前正在编辑的卡片
        if (!targetCard && this.currentEditingCard) {
            if (this.currentEditingRegions) {
                const found = this.currentEditingRegions.some(r => roiIdMatch(r.roi_id, roiId));
                if (found) {
                    targetCard = this.currentEditingCard;
                    console.log(`[Map→Left] 方法6: 通过currentEditingRegions匹配`);
                }
            }
        }
        
        // 7. ⭐ 协作模式：查找待确认卡片
        if (!targetCard) {
            const pendingCards = routeStepsList.querySelectorAll('.collab-task-card.status-pending');
            if (pendingCards.length === 1) {
                targetCard = pendingCards[0];
                console.log(`[Map→Left] 方法7: 选中唯一待确认卡片`);
            }
        }
        
        if (targetCard) {
            // ⭐ Toggle：如果再次点击已高亮的卡片/区域，取消高亮
            // 只看自身或蓝/红 header，不要把子绿框的高亮当成“蓝框已选中”
            const isAlreadyHighlighted = this._treeCardIsHighlighted(targetCard);
            
            // ⭐ 清除范围必须包含正文（.route-tree-*-content）：高亮经常加在正文上，
            // 漏掉它们会导致点击下一个区域后旧位置的黄色高亮永远残留。
            const HIGHLIGHTABLE_SELECTOR = '.collab-task-card, .route-step-card, .route-tree-node, .route-tree-blue-node, .route-tree-green-item, .route-tree-header, .route-tree-blue-header, .route-tree-content, .route-tree-blue-content, .route-tree-green-content, .region-item, .collab-tree-node';
            // 封存版式的兜底：按高亮类名找元素，谁亮着清谁，
            // 以后即使高亮目标再变化也不会残留。
            const clearAllHighlights = () => {
                routeStepsList.querySelectorAll(HIGHLIGHTABLE_SELECTOR).forEach(card => {
                    card.classList.remove('active', 'active-viewing', 'card-active', 'roi-selected-glow');
                });
                routeStepsList.querySelectorAll('.roi-selected-glow, .active-viewing, .card-active').forEach(el => {
                    el.classList.remove('roi-selected-glow', 'active-viewing', 'card-active');
                });
            };
            if (isAlreadyHighlighted && !options.force) {
                clearAllHighlights();
                console.log(`[Map→Left] 🔄 取消高亮: ${roiId}`);
                return;
            }
            
            // 移除所有元素的高亮状态（包括树状结构 + 协作模式区域项 + 正文）
            clearAllHighlights();
            
            // ⭐ 判断节点类型并确定高亮目标
            let highlightTarget = targetCard;
            const isRedNode = targetCard.classList.contains('route-tree-node');
            const isBlueNode = targetCard.classList.contains('route-tree-blue-node');
            const isGreenNode = targetCard.classList.contains('route-tree-green-item');
            const isRegionItem = targetCard.classList.contains('region-item');
            const isCollabTreeNode = targetCard.classList.contains('collab-tree-node');
            const isTreeNode = isRedNode || isBlueNode || isGreenNode;
            const isCollabElement = isRegionItem || isCollabTreeNode;
            
            this._expandMappedTreeAncestors(targetCard);

            // 对于红框和蓝框：只高亮头部区域，避免整个树被高亮
            if (isRedNode) {
                const header = targetCard.querySelector('.route-tree-header');
                if (header) highlightTarget = header;
            } else if (isBlueNode) {
                const header = targetCard.querySelector('.route-tree-blue-header');
                if (header) highlightTarget = header;
            }
            const fullBody = targetCard.querySelector(
                ':scope > .route-tree-content, :scope > .route-tree-blue-content, :scope > .route-tree-green-content'
            );
            if (fullBody && String(fullBody.textContent || '').trim().length > 40) {
                highlightTarget = fullBody;
            }
            
            // ⭐ 统一使用黄色高亮 (roi-selected-glow)，无论是自动模式还是协作模式
            if (isTreeNode || isCollabElement) {
                highlightTarget.classList.add('roi-selected-glow');
                console.log(`[Map→Left] 🟡 添加黄色高亮 roi-selected-glow (目标: ${highlightTarget.className.split(' ')[0]})`);
            } else {
                // 其他卡片：使用紫色高亮 (active-viewing)
                highlightTarget.classList.add('active', 'active-viewing', 'card-active');
                console.log(`[Map→Left] 🟣 添加紫色高亮 active-viewing`);
            }
            
            // 滚动到可视区域（滚动高亮的目标元素，而不是整个容器）
            highlightTarget.scrollIntoView({ behavior: 'smooth', block: 'center' });
            
            // 添加闪烁高亮效果
            highlightTarget.classList.add('flash-highlight');
            setTimeout(() => {
                highlightTarget.classList.remove('flash-highlight');
            }, 800);
            
            console.log(`[Map→Left] ✅ 高亮左侧卡片成功: ${roiId} -> ${targetCard.dataset.roiId || targetCard.dataset.treeId}`);
        } else {
            console.log(`[Map→Left] ❌ 未找到对应卡片: ${roiId}`);
        }
    }
    
    // ⭐ 处理红框点击事件
    handleRedFrameClick(roiId) {
        console.log('处理红框点击:', roiId);
        
        // 1. 高亮左侧对应的结果卡片
        const resultsList = document.getElementById('resultsList');
        if (resultsList) {
            resultsList.querySelectorAll('.result-item').forEach(item => {
                item.classList.remove('active');
                if (item.dataset.roiId && item.dataset.roiId.includes(roiId)) {
                    item.classList.add('active');
                    item.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
            });
        }
        
        // 2. 高亮右侧对应的确认卡片并显示蓝框
        this.highlightCorrespondingCards(roiId);
        
        // 3. 找到对应的确认卡片数据并显示蓝框
        const confirmationCards = document.querySelectorAll('.confirmation-card');
        confirmationCards.forEach(card => {
            const cardData = card._confirmData;
            if (cardData && cardData.parentRoiId === roiId) {
                this.highlightCardRegions(cardData);
            }
        });
    }
    
    bindEvents() {
        // 病例/工作区切换会重新渲染工具栏。直接把监听器绑在当前按钮上，
        // 新生成的 #btnAgentRun 会失去点击行为；用稳定的 document 委托，
        // 让同一 Controller 在 SPA 重绘后仍能启动或停止分析。
        if (!this._agentRunClickHandler) {
            this._agentRunClickHandler = (event) => {
                const btnRun = event.target?.closest?.('#btnAgentRun');
                if (!btnRun) return;
                if (this._isCollaborativeDraftReviewActive()) {
                    const chatInput = window.pathchat?.elements?.chatInput;
                    chatInput?.focus?.();
                    this.showNotification(
                        (window.__lang || 'zh') === 'en'
                            ? 'The draft is waiting for sign-off. Agree to finalize, or keep chatting, before starting a new analysis.'
                            : '拟报告待定稿。请先同意定稿或继续沟通，再开始新的分析。',
                        'info'
                    );
                    return;
                }
                if (window.pathchat?.patientIsRunning) {
                    window.pathchat.stopPatientLevelAnalysis?.();
                    return;
                }
                if (this.isRunning) this.stopAgent();
                else {
                    const chatInput = window.pathchat?.elements?.chatInput;
                    chatInput?.focus?.();
                    this.showNotification(
                        (window.__lang || 'zh') === 'en'
                            ? 'Ask your slide question in the chat, then choose Agent Swarm or Single Agent on the cards.'
                            : '请在右侧聊天区提出切片问题，然后在卡片上选择 Agent Swarm 或单 Agent。',
                        'info'
                    );
                }
            };
            document.addEventListener('click', this._agentRunClickHandler);
        }
        
        // 收起右侧 Subagent 结果栏
        const routeBackBtn = document.getElementById('routeBackBtn');
        if (routeBackBtn) {
            routeBackBtn.addEventListener('click', () => this.exitAnalysisLayout());
        }
        
        console.log('Agent按钮事件绑定完成');
    }
    
    // ==================== 路径规划面板布局管理 ====================
    
    // 进入分析布局：病例栏保持独立，Subagent 结果显示在对话左侧
    enterAnalysisLayout(mode) {
        // ⭐ 确保模式属性同步更新
        this.currentMode = mode;
        this.isCollaborativeMode = (mode === 'collaborative');
        console.log(`📐 进入分析布局: mode=${mode}, isCollaborativeMode=${this.isCollaborativeMode}`);
        
        const fm = document.getElementById('fileManagerPanel');
        const rp = document.getElementById('analysisRoutePanel');
        
        if (!(this.isPatientLevelMode && this._patientLevelPanelIsActive()) && fm?.style.display === 'none') {
            fm.style.removeProperty('display');
        }
        if (rp) rp.style.display = 'flex';
        document.getElementById('returnToAnalysisBtn')?.remove();
        window.PathCopilotV2?.setSubagentPaneOpen?.(true);
        
        // ⭐ 根据模式切换容器类名（协作模式 vs 自动模式）
        if (rp) {
            if (mode === 'collaborative') {
                rp.classList.add('layout-mode-collab');
                rp.classList.remove('layout-mode-auto');
            } else {
                rp.classList.add('layout-mode-auto');
                rp.classList.remove('layout-mode-collab');
            }
        }
        
        // 设置模式标签
        const tag = document.getElementById('routeModeTag');
        if (tag) {
            if (mode === 'collaborative') {
                tag.innerHTML = `<i class="fas fa-users"></i> ${__t('collab_mode_tag')}`;
                tag.className = 'route-mode-tag collaborative-mode';
            } else {
                tag.innerHTML = `<i class="fas fa-wand-magic-sparkles"></i> ${__t('auto_mode_tag')}`;
                tag.className = 'route-mode-tag auto-mode';
            }
        }
        
        // 设置当前切片名
        const nameSpan = document.getElementById('routeSlideName');
        if (nameSpan) {
            const fileName = window.pathchat?.currentFile?.filename ||
                           window.pathchat?.currentFile?.name || 
                           window.pathchat?.currentSlideName || 
                           __t('current_slide');
            nameSpan.textContent = fileName;
        }
        
        // 清空步骤列表
        const list = document.getElementById('routeStepsList');
        if (list) {
            const isEn = (window.__lang || 'zh') === 'en';
            list.innerHTML = `
                <div class="route-empty-state v2-subagent-empty">
                    <i class="fas fa-layer-group" aria-hidden="true"></i>
                    <p>${isEn ? 'Subagent results will appear here when they are ready.' : 'Subagent 结果生成后将在这里显示。'}</p>
                </div>
            `;
        }
        
        // 重置进度条
        const progressFill = document.getElementById('routeProgressFill');
        const progressText = document.getElementById('routeProgressText');
        if (progressFill) progressFill.style.width = '0%';
        if (progressText) {
            const messageSpan = progressText.querySelector('.progress-message');
            if (messageSpan) {
                messageSpan.textContent = __t('prepare_analysis');
            } else {
                progressText.textContent = __t('prepare_analysis');
            }
        }
        
        console.log(`进入分析布局 (${mode}模式)`);
    }
    
    // ⭐ 恢复分析布局（不清空已有结果，仅切换面板可见性）
    resumeAnalysisLayout() {
        const fm = document.getElementById('fileManagerPanel');
        const rp = document.getElementById('analysisRoutePanel');

        if (!(this.isPatientLevelMode && this._patientLevelPanelIsActive()) && fm?.style.display === 'none') {
            fm.style.removeProperty('display');
        }
        if (rp) rp.style.display = 'flex';
        document.getElementById('returnToAnalysisBtn')?.remove();
        window.PathCopilotV2?.setSubagentPaneOpen?.(true);

        console.log('恢复分析布局（保留已有结果）');
    }

    // 收起右侧 Subagent 结果栏，结果仍保留并可从对话栏标题处恢复
    exitAnalysisLayout() {
        const fm = document.getElementById('fileManagerPanel');
        const rp = document.getElementById('analysisRoutePanel');
        
        if (!(this.isPatientLevelMode && this._patientLevelPanelIsActive()) && fm?.style.display === 'none') {
            fm.style.removeProperty('display');
        }
        if (rp) rp.style.display = 'none';
        window.PathCopilotV2?.setSubagentPaneOpen?.(false);
        
        // 清理高亮覆盖层
        this.clearConfirmationHighlights();
        
        // 在右侧对话标题处保留恢复入口，不再污染最左侧病例栏
        this.showReturnToAnalysisButton();
        
        console.log('已收起右侧 Subagent 结果栏');
    }
    
    // 旧版会在最左侧插入紫色按钮；新版只使用右侧标题栏入口
    showReturnToAnalysisButton() {
        document.getElementById('returnToAnalysisBtn')?.remove();
        window.PathCopilotV2?.setSubagentPaneOpen?.(false);
    }
    
    // ⭐ 更新路径进度条
    updateRouteProgress(percentage, text) {
        const progressFill = document.getElementById('routeProgressFill');
        const progressText = document.getElementById('routeProgressText');
        
        if (progressFill) progressFill.style.width = `${percentage}%`;
        if (progressText) {
            // ⭐ 分析完成时隐藏闪烁动画
            const typingDots = progressText.querySelector('.typing-dots');
            if (typingDots) {
                // 100%或包含"完成"时停止动画
                if (percentage >= 100 || text.includes('完成') || text.includes('Completed') || text.includes('complete') || text.includes(__t('status_completed'))) {
                    typingDots.style.display = 'none';
                } else {
                    typingDots.style.display = 'inline-flex';
                }
            }
            
            // 更新文字部分
            const messageSpan = progressText.querySelector('.progress-message');
            if (messageSpan) {
                messageSpan.textContent = text;
            } else {
                progressText.textContent = text;
            }
        }
    }
    
    _shouldPaintCollabAsAutoTree() {
        // The Subagent column already streams Evidence. Colorful workstation
        // cards (R6 / R6-B12) were a second, inconsistent map. Later slides
        // still need a clickable Auto-style tree so Map / 地图联动 works.
        return Boolean(document.querySelector(
            '#v2WorkbenchShell .right-panel.has-subagent-pane .v2-subagent-pane'
        ));
    }

    // ⭐ 添加路径步骤到左侧面板
    appendRouteStep(step) {
        const list = this._patientMapList(this._asPatientWsiIndex(step?.wsiIndex));
        if (!list) return;
        
        // 移除空状态提示
        const emptyState = list.querySelector('.route-empty-state');
        if (emptyState) emptyState.remove();
        
        if (this.currentMode === 'ai' || this._shouldPaintCollabAsAutoTree()) {
            return this._appendRouteStepAutoMode(step, list);
        }
        return this._appendRouteStepCollabMode(step, list);
    }
    
    // ⭐ 自动模式：树状层级结构（重构版）
    _appendRouteStepAutoMode(step, list) {
        const roiId = step.roi_id || '';
        // 解析ROI层级关系
        const hierarchy = this._parseRoiHierarchy(roiId);
        const declaredLevel = String(step.level || '').toLowerCase();
        // ROI ID is the reliable source of ancestry.  Some legacy events label
        // every recursive worker as "red", even when the ID clearly belongs to
        // a child ROI such as R2_sub_r2_c1.
        const level = ['blue', 'green'].includes(hierarchy.level)
            ? hierarchy.level
            : (['red', 'blue', 'green'].includes(declaredLevel) ? declaredLevel : hierarchy.level || 'red');

        this._repairMisplacedRoiNodes(list);
        
        // 根据层级决定显示方式
        if (level === 'red' || hierarchy.level === 'red') {
            // 红框：创建树节点
            return this._createRedTreeNode(step, list);
        } else if (level === 'blue' || hierarchy.level === 'blue') {
            // 蓝框：添加到父红框下
            return this._addBlueToTree(step, list, hierarchy);
        } else if (level === 'green' || hierarchy.level === 'green') {
            // 绿框：添加到父蓝框下
            return this._addGreenToTree(step, list, hierarchy);
        } else {
            // 其他类型：使用原有平铺方式
            return this._createFlatCard(step, list);
        }
    }
    
    // ⭐ 解析ROI ID层级关系
    _parseRoiHierarchy(roiId) {
        if (!roiId) return { level: 'unknown', parent: null, grandparent: null };

        const normalizedId = String(roiId).trim();

        // Runtime format produced by the recursive ROI workers:
        // R2 -> R2_sub_r2_c1 -> R2_sub_r2_c1_sub_r3_c2
        const recursiveParts = normalizedId.split('_sub_').filter(Boolean);
        if (recursiveParts.length === 2) {
            return {
                level: 'blue',
                id: normalizedId,
                parent: recursiveParts[0],
                grandparent: null
            };
        }
        if (recursiveParts.length >= 3) {
            return {
                level: 'green',
                id: normalizedId,
                parent: recursiveParts.slice(0, -1).join('_sub_'),
                grandparent: recursiveParts[0]
            };
        }

        // 格式：R3, R3-B1, R3-B1-G2
        const parts = normalizedId.split('-');
        
        if (parts.length === 1) {
            // 红框：R3
            return { level: 'red', id: normalizedId, parent: null, grandparent: null };
        } else if (parts.length === 2) {
            // 蓝框：R3-B1
            return { level: 'blue', id: normalizedId, parent: parts[0], grandparent: null };
        } else if (parts.length >= 3) {
            // 绿框：R3-B1-G2
            return { level: 'green', id: normalizedId, parent: `${parts[0]}-${parts[1]}`, grandparent: parts[0] };
        }
        
        return { level: 'unknown', parent: null, grandparent: null };
    }

    // Repair nodes restored from an older session or created by a legacy event
    // that incorrectly placed recursive child IDs at the top level.
    _repairMisplacedRoiNodes(list = document.getElementById('routeStepsList')) {
        if (!list) return 0;
        const misplaced = Array.from(list.children)
            .filter(node => node.classList?.contains('route-tree-node'))
            .map(node => ({ node, hierarchy: this._parseRoiHierarchy(node.dataset.treeId || node.dataset.roiId || '') }))
            .filter(item => item.hierarchy.level === 'blue' || item.hierarchy.level === 'green')
            .sort((a, b) => String(a.hierarchy.id).split('_sub_').length - String(b.hierarchy.id).split('_sub_').length);

        misplaced.forEach(({ node, hierarchy }) => {
            const roiId = hierarchy.id;
            const badge = node.querySelector('.route-tree-badge');
            const content = node.querySelector('.route-tree-content')?.textContent?.trim() || '';
            let rect = null;
            if (node.dataset.rect) {
                try { rect = JSON.parse(node.dataset.rect); } catch (_) {}
            }
            const step = {
                roi_id: roiId,
                level: hierarchy.level,
                title: roiId,
                reasoning: content,
                rect,
                status: badge?.classList.contains('completed') ? 'completed' : 'in-progress'
            };
            node.remove();
            if (hierarchy.level === 'blue') this._addBlueToTree(step, list, hierarchy);
            else this._addGreenToTree(step, list, hierarchy);
        });
        return misplaced.length;
    }

    _observeRoiHierarchy() {
        const list = document.getElementById('routeStepsList');
        if (!list || this.roiHierarchyObserver) return;
        this.roiHierarchyObserver = new MutationObserver(() => {
            if (this.isRepairingHierarchy) return;
            this.isRepairingHierarchy = true;
            try {
                this._repairMisplacedRoiNodes(list);
            } finally {
                this.isRepairingHierarchy = false;
            }
        });
        this.roiHierarchyObserver.observe(list, { childList: true });
        this._repairMisplacedRoiNodes(list);
    }

    // 将自动模式中的 ROI 进度写回红→蓝→绿树，而不是追加到全局活动流底部。
    _appendRoiHierarchyProgress(data = {}, content = '') {
        if (this.currentMode !== 'ai' && !this._shouldPaintCollabAsAutoTree()) return false;
        if (this._isPatientSwarmMapMode() && this._patientSwarmMapWsiIndex == null) {
            const scoped = this._patientSwarmMapWsiIndexFromEvent(data);
            if (Number.isInteger(scoped) || this._isPatientParallelRun()) {
                return this._withPatientSwarmMapContext(data, () => (
                    this._appendRoiHierarchyProgressUnlocked(data, content)
                ));
            }
        }
        return this._appendRoiHierarchyProgressUnlocked(data, content);
    }

    _appendRoiHierarchyProgressUnlocked(data = {}, content = '') {

        const metadata = data.metadata || {};
        const roiId = String(data.roi_id || metadata.roi_id || '').trim();
        if (!roiId || roiId === 'AI-Red-Selection') return false;

        let level = String(data.level || '').toLowerCase();
        const hierarchy = this._parseRoiHierarchy(roiId);
        if (!['red', 'blue', 'green'].includes(level)) level = hierarchy.level;
        if (!['red', 'blue', 'green'].includes(level)) return false;

        const message = String(content || data.content || '').trim();
        const completed = /完成|complete|selected|已选择/i.test(message);
        const status = completed ? 'completed' : 'in-progress';
        const rect = data.rect || metadata.rect || null;
        const reasoning = data.reasoning || '';
        const regions = Array.isArray(data.regions)
            ? data.regions
            : (Array.isArray(metadata.regions) ? metadata.regions : []);
        const stepWsi = this._patientSwarmMapWsiIndexFromEvent(data);

        if (level === 'red') {
            this.appendRouteStep({
                level: 'red',
                roi_id: roiId,
                title: message || `${roiId} (10×)`,
                content: reasoning,
                reasoning,
                rect,
                status,
                wsiIndex: stepWsi
            });
            return true;
        }

        if (level === 'blue') {
            const parentRedId = String(
                data.parent_id || metadata.parent_id ||
                (hierarchy.level === 'red' ? roiId : hierarchy.parent) || ''
            ).trim();
            if (!parentRedId) return false;

            // 蓝框选择属于对应红框；先更新红框，再把每个蓝框放进它的 children。
            this.appendRouteStep({
                level: 'red',
                roi_id: parentRedId,
                title: `${parentRedId} (10×)`,
                content: reasoning || message,
                reasoning: reasoning || message,
                rect: hierarchy.level === 'red' ? rect : null,
                status: completed ? 'completed' : 'in-progress',
                wsiIndex: stepWsi
            });

            const blueRegions = regions.length
                ? regions
                : (hierarchy.level === 'blue' ? [{ roi_id: roiId, rect }] : []);
            blueRegions.forEach((region, index) => {
                const childId = String(region.roi_id || region.id || `${parentRedId}-B${index + 1}`).trim();
                this.appendRouteStep({
                    level: 'blue',
                    roi_id: childId,
                    parent_id: parentRedId,
                    title: childId,
                    rect: region.rect || null,
                    status,
                    wsiIndex: stepWsi
                });
            });
            return true;
        }

        const parentBlueId = String(
            data.parent_id || metadata.parent_id ||
            (hierarchy.level === 'blue' ? roiId : hierarchy.parent) || ''
        ).trim();
        if (!parentBlueId) return false;

        // 绿框选择/分析属于对应蓝框。开始分析的进度文案只挂在绿框上，
        // 不要覆盖父蓝框已经写好的 40× 解读。
        this.appendRouteStep({
            level: 'blue',
            roi_id: parentBlueId,
            title: parentBlueId,
            rect: hierarchy.level === 'blue' ? rect : this._resolveRoiRect(parentBlueId),
            status: completed ? 'completed' : 'in-progress',
            wsiIndex: stepWsi
        });

        const greenRegions = regions.length
            ? regions
            : (hierarchy.level === 'green' ? [{ roi_id: roiId, rect }] : []);
        greenRegions.forEach((region, index) => {
            const childId = String(region.roi_id || region.id || `${parentBlueId}-G${index + 1}`).trim();
            this.appendRouteStep({
                level: 'green',
                roi_id: childId,
                parent_id: parentBlueId,
                title: childId,
                rect: region.rect || null,
                status,
                wsiIndex: stepWsi
            });
        });
        return true;
    }
    
    // ⭐ 创建红框树节点
    _createRedTreeNode(step, list) {
        const roiId = step.roi_id || '';
        
        // 检查是否已存在该红框节点
        const wsiSuffix = this._patientSwarmWsiSelectorSuffix();
        let existingNode = list.querySelector(`[data-tree-id="${roiId}"]${wsiSuffix}`);
        if (existingNode) {
            this._tagPatientSwarmTreeNode(existingNode);
            // 更新状态
            this._updateTreeNodeStatus(existingNode, step);
            return existingNode;
        }
        
        // 创建新的树节点
        const node = document.createElement('div');
        node.className = 'route-tree-node';
        node.dataset.treeId = roiId;
        node.dataset.roiId = roiId;
        node.dataset.level = 'red';
        this._tagPatientSwarmTreeNode(node);
        if (step.rect) {
            node.dataset.rect = JSON.stringify(step.rect);
        }
        
        // 状态
        const statusClass = step.status === 'completed' ? 'completed' : 'in-progress';
        const statusText = step.status === 'completed' ? __t('status_completed') : __t('status_in_progress');
        
        // 标题处理
        let title = step.title || step.text || roiId;
        // Backend event titles may already begin with one or more red-circle
        // markers.  The tree header renders its own red ROI icon, so strip
        // those prefixes to avoid showing duplicate red circles.
        title = String(title).replace(/^(?:\s*\u{1F534}\s*)+/u, '').trim();
        // 如果标题包含"选中X个红框"，提取简化
        const selectMatch = title.match(/选中\s*(\d+)\s*个红框/);
        if (selectMatch) {
            title = `AI选中 ${selectMatch[1]} 个红框`;
        }
        
        // 获取内容（优先使用reasoning，否则使用content）
        const nodeContent = this._escapeHtmlContent(step.reasoning || step.content || '');
        
        node.innerHTML = `
            <div class="route-tree-header" data-roi-id="${roiId}">
                <span class="route-tree-toggle"><i class="fas fa-chevron-down"></i></span>
                <span class="route-tree-title">
                    <span class="tree-icon">🔴</span>
                    ${title}
                </span>
                <span class="route-tree-badge ${statusClass}">${statusText}</span>
                <span class="route-tree-count" data-count-type="blue">0</span>
            </div>
            ${nodeContent ? `<div class="route-tree-content">${nodeContent}</div>` : ''}
            <div class="route-tree-children"></div>
        `;
        
        // 点击头部 → 展开/收起 + 聚焦
        const header = node.querySelector('.route-tree-header');
        header.addEventListener('click', (e) => {
            const toggleOnly = Boolean(e.target.closest('.route-tree-toggle'));
            header.classList.toggle('collapsed');
            if (!toggleOnly) this._focusTreeNode(node, step);
        });
        
        list.appendChild(node);
        
        // ⭐ 移除自动滚动 - 让用户自己控制滚动位置
        // setTimeout(() => {
        //     node.scrollIntoView({ behavior: 'smooth', block: 'end' });
        // }, 50);
        
        return node;
    }
    
    // ⭐ 添加蓝框到树结构
    _addBlueToTree(step, list, hierarchy) {
        const roiId = step.roi_id || '';
        const parentId = hierarchy.parent || this._extractParentFromTitle(step.title || step.text);
        
        // 查找父红框节点
        const wsiSuffix = this._patientSwarmWsiSelectorSuffix();
        let parentNode = list.querySelector(`[data-tree-id="${parentId}"]${wsiSuffix}`);
        
        // 如果父节点不存在，创建一个
        if (!parentNode) {
            parentNode = this._createRedTreeNode({
                roi_id: parentId,
                level: 'red',
                title: `红框 ${parentId}`,
                status: 'in-progress'
            }, list);
        }
        
        const childrenContainer = parentNode.querySelector('.route-tree-children');
        if (!childrenContainer) return null;
        
        // 检查是否已存在该蓝框
        let existingBlue = childrenContainer.querySelector(`[data-tree-id="${roiId}"]`);
        if (existingBlue) {
            this._updateBlueNodeContent(existingBlue, step);
            if (!existingBlue.dataset.rect) {
                const filled = this._resolveRoiRect(roiId, step.rect);
                if (filled) existingBlue.dataset.rect = JSON.stringify(filled);
            }
            return existingBlue;
        }
        
        // 创建蓝框节点
        const blueNode = document.createElement('div');
        blueNode.className = 'route-tree-blue-node';
        blueNode.dataset.treeId = roiId;
        blueNode.dataset.roiId = roiId;
        blueNode.dataset.level = 'blue';
        blueNode.dataset.parentId = parentId;
        this._tagPatientSwarmTreeNode(blueNode);
        const blueRect = step.rect || this._resolveRoiRect(roiId);
        if (blueRect) {
            blueNode.dataset.rect = JSON.stringify(blueRect);
        }
        
        // 提取简短标题
        let title = roiId;
        const content = step.content || '';
        // 获取内容（优先使用reasoning，否则使用content）
        const nodeContent = this._escapeHtmlContent(step.reasoning || content);
        
        // 状态
        const statusClass = step.status === 'completed' ? 'completed' : 'in-progress';
        const statusText = step.status === 'completed' ? __t('status_completed') : __t('status_in_progress');
        
        blueNode.innerHTML = `
            <div class="route-tree-blue-header" data-roi-id="${roiId}">
                <span class="route-tree-toggle"><i class="fas fa-chevron-down"></i></span>
                <span class="route-tree-blue-title">🔵 ${title}</span>
                <span class="route-tree-badge ${statusClass}">${statusText}</span>
            </div>
            ${nodeContent ? `<div class="route-tree-blue-content">${nodeContent}</div>` : ''}
            <div class="route-tree-green-children"></div>
        `;
        
        // 点击事件
        const blueHeader = blueNode.querySelector('.route-tree-blue-header');
        blueHeader.addEventListener('click', (e) => {
            const toggleOnly = Boolean(e.target.closest('.route-tree-toggle'));
            blueHeader.classList.toggle('collapsed');
            if (!toggleOnly) this._focusTreeNode(blueNode, step);
        });
        
        childrenContainer.appendChild(blueNode);
        
        // 更新父节点的蓝框计数
        this._updateTreeCount(parentNode, 'blue');
        
        // ⭐ 移除自动滚动 - 让用户自己控制滚动位置
        // setTimeout(() => {
        //     blueNode.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        // }, 50);
        
        return blueNode;
    }
    
    // ⭐ 添加绿框到树结构
    _addGreenToTree(step, list, hierarchy) {
        const roiId = step.roi_id || '';
        const parentBlueId = hierarchy.parent || step.parent_id;
        const grandparentRedId = hierarchy.grandparent;
        
        // 查找父蓝框节点
        const wsiSuffix = this._patientSwarmWsiSelectorSuffix();
        let parentBlueNode = list.querySelector(`[data-tree-id="${parentBlueId}"]${wsiSuffix}`);
        
        // 如果父蓝框不存在，尝试找到红框并创建蓝框
        if (!parentBlueNode && grandparentRedId) {
            const parentRedNode = list.querySelector(`[data-tree-id="${grandparentRedId}"]${wsiSuffix}`);
            if (parentRedNode) {
                parentBlueNode = this._addBlueToTree({
                    roi_id: parentBlueId,
                    level: 'blue',
                    title: `蓝框 ${parentBlueId}`,
                    status: 'in-progress'
                }, list, { level: 'blue', parent: grandparentRedId });
            }
        }
        
        if (!parentBlueNode) {
            // 降级：使用平铺方式
            return this._createFlatCard(step, list);
        }
        
        const greenContainer = parentBlueNode.querySelector('.route-tree-green-children');
        if (!greenContainer) return null;
        
        // 检查是否已存在
        let existingGreen = this._findTreeNodeByRoiId(greenContainer, roiId, '.route-tree-green-item[data-roi-id]');
        if (existingGreen) {
            const titleEl = existingGreen.querySelector('.route-tree-green-title');
            if (titleEl) {
                titleEl.textContent = `🟢 ${roiId} ${step.status === 'completed' ? __t('status_completed') : __t('status_in_progress')}`;
            }
            const displayContent = this._escapeHtmlContent(step.reasoning || step.content || '');
            if (displayContent) {
                let contentEl = existingGreen.querySelector('.route-tree-green-content');
                if (!contentEl) {
                    contentEl = document.createElement('div');
                    contentEl.className = 'route-tree-green-content';
                    existingGreen.appendChild(contentEl);
                }
                contentEl.innerHTML = displayContent;
            }
            return existingGreen;
        }
        
        // 创建绿框项
        const greenItem = document.createElement('div');
        greenItem.className = 'route-tree-green-item';
        greenItem.dataset.roiId = roiId;
        greenItem.dataset.level = 'green';
        greenItem.dataset.parentId = parentBlueId;
        this._tagPatientSwarmTreeNode(greenItem);
        if (step.rect) {
            greenItem.dataset.rect = JSON.stringify(step.rect);
        }
        
        const content = step.content || '';
        // 获取内容
        const nodeContent = this._escapeHtmlContent(step.reasoning || content);
        
        greenItem.innerHTML = `
            <div class="route-tree-green-title">🟢 ${roiId} ${step.status === 'completed' ? __t('status_completed') : __t('status_in_progress')}</div>
            ${nodeContent ? `<div class="route-tree-green-content">${nodeContent}</div>` : ''}
        `;
        
        // 点击聚焦
        greenItem.addEventListener('click', () => {
            this._focusTreeNode(greenItem, step);
        });
        
        greenContainer.appendChild(greenItem);
        
        // 更新蓝框的绿框计数
        this._updateTreeCount(parentBlueNode, 'green');
        
        return greenItem;
    }
    
    // ⭐ 创建平铺卡片（降级方案）
    _createFlatCard(step, list) {
        let icon, levelClass;
        switch (step.level) {
            case 'red': icon = '🔴'; levelClass = 'route-red'; break;
            case 'blue': icon = '🔵'; levelClass = 'route-blue'; break;
            case 'green': icon = '🟢'; levelClass = 'route-green'; break;
            default: icon = '📋'; levelClass = '';
        }
        
        const card = document.createElement('div');
        card.className = `route-step-card ${levelClass}`;
        card.dataset.roiId = step.roi_id || '';
        card.dataset.level = step.level || '';
        this._tagPatientSwarmTreeNode(card);
        if (step.rect) {
            card.dataset.rect = JSON.stringify(step.rect);
        }
        
        const statusClass = step.status === 'completed' ? 'completed' : 
                           step.status === 'pending' ? 'pending' : 
                           step.status === 'skipped' ? 'skipped' : 'waiting';
        const statusText = step.status === 'completed' ? __t('status_completed') :
                          step.status === 'pending' ? __t('status_pending_confirm') : 
                          step.status === 'skipped' ? __t('status_no_region') : __t('status_in_progress');
        
        card.innerHTML = `
            <div class="route-step-header">
                <span class="route-step-title">
                    <span class="step-icon">${icon}</span>
                    ${step.title || step.text}
                </span>
                <span class="route-step-badge ${statusClass}">${statusText}</span>
            </div>
            ${step.content ? `<div class="route-step-content">${step.content}</div>` : ''}
        `;
        
        card.addEventListener('click', () => {
            list.querySelectorAll('.route-step-card.active').forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            this._focusTreeNode(card, step);
        });
        
        list.appendChild(card);
        
        // ⭐ 移除自动滚动 - 让用户自己控制滚动位置
        // setTimeout(() => {
        //     card.scrollIntoView({ behavior: 'smooth', block: 'end' });
        // }, 50);
        
        return card;
    }
    
    // ⭐ 聚焦树节点
    _focusTreeNode(node, step) {
        const roiId = node.dataset.roiId || step.roi_id;
        let stored = null;
        if (node.dataset.rect) {
            try {
                stored = JSON.parse(node.dataset.rect);
            } catch (e) {}
        }
        const rectData = this._resolveRoiRect(roiId, stored || step.rect);
        if (rectData && !node.dataset.rect) {
            try { node.dataset.rect = JSON.stringify(rectData); } catch (_) {}
        }

        this.focusOnRoiFromMeta({
            level: node.dataset.level || step.level,
            roi_id: roiId,
            rect: rectData
        });
        
        // ⭐ 已删除右侧跳转：右侧聊天面板只用于最终报告和用户自由发言
    }
    
    // ⭐ 更新树节点状态
    _updateTreeNodeStatus(node, step) {
        const badge = node.querySelector('.route-tree-badge');
        if (badge && step.status === 'completed') {
            badge.className = 'route-tree-badge completed';
            badge.textContent = __t('status_completed');
        }
        if (step.status === 'completed') {
            const roiId = node.dataset.roiId || node.dataset.treeId || '';
            const titleEl = node.querySelector('.route-tree-title');
            if (titleEl && /开始分析|Analyzing red box|Analyzing blue box/i.test(titleEl.textContent || '')) {
                titleEl.innerHTML = `<span class="tree-icon">🔴</span> ${roiId} ${__t('status_completed')}`;
            }
        }
        if (step.rect) {
            node.dataset.rect = JSON.stringify(step.rect);
        }
        
        // ⭐ Bug修复：如果有内容（reasoning或content），更新或添加内容区域
        const displayContent = this._escapeHtmlContent(step.reasoning || step.content || '');
        if (displayContent) {
            let contentDiv = node.querySelector('.route-tree-content');
            const existingText = String(contentDiv?.textContent || '').trim();
            const incomingText = String(step.reasoning || step.content || '').trim();
            const keepFuller = existingText.length > 80 && incomingText.length + 20 < existingText.length;
            if (contentDiv) {
                if (!keepFuller) contentDiv.innerHTML = displayContent;
            } else {
                const header = node.querySelector('.route-tree-header');
                if (header) {
                    contentDiv = document.createElement('div');
                    contentDiv.className = 'route-tree-content';
                    contentDiv.innerHTML = displayContent;
                    header.insertAdjacentElement('afterend', contentDiv);
                }
            }
        }
    }
    
    // ⭐ 更新蓝框内容
    _updateBlueNodeContent(node, step) {
        const contentDiv = node.querySelector('.route-tree-blue-content');
        // ⭐ Bug修复：使用 reasoning（与创建时保持一致），而不是 content
        const displayContent = step.reasoning || step.content || '';
        if (contentDiv && displayContent) {
            // ⭐ 完整显示分析结果（可折叠，不截断）
            contentDiv.textContent = displayContent;
        }
        if (step.rect) {
            node.dataset.rect = JSON.stringify(step.rect);
        }
        const badge = node.querySelector('.route-tree-badge');
        if (badge) {
            const isCompleted = step.status === 'completed';
            badge.className = `route-tree-badge ${isCompleted ? 'completed' : 'in-progress'}`;
            badge.textContent = isCompleted ? __t('status_completed') : __t('status_in_progress');
        }
    }
    
    // ⭐ 更新树节点计数
    _updateTreeCount(node, type) {
        const countSpan = node.querySelector(`[data-count-type="${type}"]`);
        if (!countSpan) return;
        
        let count = 0;
        if (type === 'blue') {
            count = node.querySelectorAll('.route-tree-blue-node').length;
        } else if (type === 'green') {
            count = node.querySelectorAll('.route-tree-green-item').length;
        }
        
        countSpan.textContent = count > 0 ? count : '';
    }
    
    // ⭐ 从标题中提取父级ID
    _extractParentFromTitle(title) {
        if (!title) return null;
        // 尝试匹配 R1, R2 等
        const match = title.match(/R\d+/);
        return match ? match[0] : null;
    }
    
    // ⭐ 协作模式：交互式工作台卡片（全新逻辑）
    _appendRouteStepCollabMode(step, list) {
        const roiId = String(step.roi_id || '').trim();
        if (roiId && list) {
            const existing = this._findTreeNodeByRoiId(list, roiId, '.collab-task-card[data-roi-id]');
            if (existing) {
                existing._stepData = { ...(existing._stepData || {}), ...step };
                this._tagPatientSwarmTreeNode(existing);
                if (step.rect) existing.dataset.rect = JSON.stringify(step.rect);
                const alreadyDone = existing.classList.contains('status-done');
                const nextStatus = step.status === 'completed' || alreadyDone
                    ? 'completed'
                    : (step.status === 'pending' ? 'pending' : 'in-progress');
                const incomingText = String(step.reasoning || step.content || '').trim();
                const existingText = String(existing.querySelector('.collab-card-analysis')?.textContent || '').trim();
                const text = incomingText.length >= existingText.length ? incomingText : existingText;
                this._updateCollabCardStatus(existing, nextStatus, text);
                return existing;
            }
        }
        // 确定图标
        let levelIcon;
        switch (step.level) {
            case 'red':
                levelIcon = '🔴';
                break;
            case 'blue':
                levelIcon = '🔵';
                break;
            case 'green':
                levelIcon = '🟢';
                break;
            default:
                levelIcon = '📋';
        }
        
        // 确定状态类名
        let statusClass;
        if (step.status === 'completed') {
            statusClass = 'status-done';
        } else if (step.status === 'pending') {
            statusClass = 'status-pending';
        } else if (step.status === 'skipped') {
            statusClass = 'status-skipped';
        } else {
            statusClass = 'status-active';
        }
        
        // 生成状态文本
        let statusText;
        if (step.level === 'red') {
            statusText = step.status === 'completed' ? `✅ ${__t('status_completed')}` : `⏳ ${__t('status_in_progress')}...`;
        } else {
            statusText = step.status === 'completed' ? `✅ ${__t('status_confirmed')}` : 
                        step.status === 'skipped' ? `⏭️ ${__t('status_skipped')}` :
                        step.status === 'pending' ? `⏳ ${__t('status_wait_confirm')}` : `🔄 ${__t('status_processing')}`;
        }
        
        // 判断是否需要显示操作按钮（蓝框/绿框 + pending状态）
        const showButtons = (step.level === 'blue' || step.level === 'green') && step.status === 'pending';
        
        // 创建任务卡片
        const card = document.createElement('div');
        card.className = `collab-task-card ${statusClass}`;
        card.dataset.roiId = step.roi_id || '';
        card.dataset.level = step.level || '';
        card.dataset.confirmationId = step.confirmation_id || '';
        if (step.rect) {
            card.dataset.rect = JSON.stringify(step.rect);
        }
        if (step.regions) {
            card.dataset.regions = JSON.stringify(step.regions);
        }
        
        // 存储完整的step数据供后续使用
        card._stepData = step;
        this._tagPatientSwarmTreeNode(card);
        const analysisText = String(step.reasoning || step.content || '').trim();
        
        // 构建卡片HTML
        let buttonsHtml = '';
        if (showButtons) {
            buttonsHtml = `
                <div class="collab-btn-group">
                    <button class="collab-mini-btn collab-btn-accept" data-action="accept" title="${__t('title_accept_ai')}">
                        <i class="fas fa-check"></i> ${__t('btn_accept')}
                    </button>
                    <button class="collab-mini-btn collab-btn-add" data-action="add" title="${__t('title_add_box')}">
                        <i class="fas fa-plus"></i> ${__t('btn_add')}
                    </button>
                    <button class="collab-mini-btn collab-btn-delete" data-action="delete" title="${__t('title_delete_box')}">
                        <i class="fas fa-trash"></i> ${__t('btn_delete')}
                    </button>
                </div>
            `;
        }
        
        card.innerHTML = `
            <div class="collab-card-header">
                <div class="collab-card-title">
                    <span class="level-icon">${levelIcon}</span>
                    <span class="title-text">${step.title || step.text}</span>
                </div>
                <div class="collab-card-status">${statusText}</div>
            </div>
            ${step.regions ? `<div class="collab-card-count">${__t('sub_region_count', step.regions.length)}</div>` : ''}
            ${analysisText ? `<div class="collab-card-analysis"></div>` : ''}
            ${buttonsHtml}
        `;
        if (analysisText) {
            const analysisEl = card.querySelector('.collab-card-analysis');
            if (analysisEl) analysisEl.textContent = analysisText;
        }
        
        // ⭐ 点击卡片头部区域 → 地图聚焦 + 高亮
        const header = card.querySelector('.collab-card-header');
        if (header) {
            header.style.cursor = 'pointer';
            header.addEventListener('click', (e) => {
                // 阻止冒泡
                e.stopPropagation();
                // ⭐ Bug修复：使用 card._stepData 获取最新数据
                const latestStep = card._stepData || step;
                this._handleCollabCardClick(card, latestStep, list);
            });
        }
        
        // ⭐ 绑定操作按钮事件
        if (showButtons) {
            const buttons = card.querySelectorAll('.collab-mini-btn');
            buttons.forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation(); // 防止触发卡片点击
                    const action = btn.dataset.action;
                    // ⭐ Bug修复：使用 card._stepData 获取最新数据
                    const latestStep = card._stepData || step;
                    this._handleCollabButtonClick(card, latestStep, action, list);
                });
            });
        }
        
        list.appendChild(card);
        
        // ⭐ 移除自动滚动 - 让用户自己控制滚动位置
        // setTimeout(() => {
        //     card.scrollIntoView({ behavior: 'smooth', block: 'end' });
        // }, 50);
        
        return card;
    }
    
    // ⭐ 处理协作卡片点击（聚焦 + 高亮，支持 toggle 取消）
    _handleCollabCardClick(card, step, list) {
        // ⭐ Toggle：如果点击的是已经高亮的卡片，取消高亮
        if (card.classList.contains('active-viewing')) {
            card.classList.remove('active', 'active-viewing');
            this.clearFocusHighlight();
            this.clearConfirmationHighlights();
            console.log('[Collab] 取消卡片高亮');
            return;
        }
        
        // 移除其他卡片的高亮状态
        list.querySelectorAll('.collab-task-card.active, .collab-task-card.active-viewing').forEach(c => {
            c.classList.remove('active', 'active-viewing');
        });
        card.classList.add('active', 'active-viewing');
        
        // 从 dataset 中读取最新的 rect
        let rectData = null;
        if (card.dataset.rect) {
            try {
                rectData = JSON.parse(card.dataset.rect);
            } catch (e) {}
        }
        
        // 1. 地图联动：缩放到该区域（复用Auto模式的逻辑）
        this.focusOnRoiFromMeta({
            level: card.dataset.level || step.level,
            roi_id: card.dataset.roiId || step.roi_id,
            rect: rectData || step.rect
        });
        
        // 2. 高亮联动：显示可编辑区域
        if (card.dataset.level === 'blue' || card.dataset.level === 'green') {
            // 获取regions数据
            let regions = step.regions;
            if (!regions && card.dataset.regions) {
                try {
                    regions = JSON.parse(card.dataset.regions);
                } catch (e) {}
            }
            
            // 如果有regions，显示高亮
            if (regions && regions.length > 0) {
                // ⭐ Bug修复：传入正确的frameType参数（第2个参数）
                const frameType = card.dataset.level || step.level || 'blue';
                this.highlightEditableRegions(regions, frameType, rectData || step.rect, step.roi_id);
                // 同时更新当前编辑状态
                this.currentEditingRegions = JSON.parse(JSON.stringify(regions));
                this.selectedRegions = this.currentEditingRegions;
                this.currentParentRect = rectData || step.rect;
            }
        }
        
        // 3. 设置当前确认ID（为按钮操作做准备）
        if (step.confirmation_id) {
            this.currentConfirmationId = step.confirmation_id;
        }
        
        console.log(`[Collab] 卡片点击聚焦: ${step.roi_id}`, { rect: rectData });
    }
    
    // ⭐ 处理协作按钮点击（接受/调整/跳过）
    _handleCollabButtonClick(card, step, action, list) {
        // ⭐ Bug修复：始终使用 card._stepData 获取最新数据，避免闭包问题
        const latestStep = card._stepData || step;
        
        console.log(`[Collab] 按钮点击: action=${action}, roi_id=${latestStep.roi_id}`);
        
        // ⭐ Bug修复：add/delete模式不自动聚焦放大，只设置卡片状态
        if (action === 'add' || action === 'delete') {
            // 只设置卡片激活状态，不触发地图聚焦放大
            list.querySelectorAll('.collab-task-card.active, .collab-task-card.active-viewing').forEach(c => {
                c.classList.remove('active', 'active-viewing');
            });
            card.classList.add('active', 'active-viewing');
        } else {
            // 其他操作（如accept）才聚焦到该区域
            this._handleCollabCardClick(card, latestStep, list);
        }
        
        // ⭐ 设置确认ID（优先从dataset获取最新值）
        const confirmationId = card.dataset.confirmationId || latestStep.confirmation_id;
        if (confirmationId) {
            this.currentConfirmationId = confirmationId;
        }
        
        console.log(`[Collab] confirmation_id: ${confirmationId}, dataset: ${card.dataset.confirmationId}, step: ${latestStep.confirmation_id}`);
        
        // ⭐ 新方案：每个卡片创建时就有confirmation_id，用户可任意顺序确认
        if (!confirmationId && action !== 'edit') {
            // 如果没有confirmation_id，可能是旧消息或数据丢失，显示警告
            this.showNotification((window.__lang||'zh')==='en' ? '⚠️ Incomplete region data, please refresh' : '⚠️ 该区域数据不完整，请刷新页面重试', 'warning');
            console.warn(`[Collab] 未找到confirmation_id: ${latestStep.roi_id}`);
            return;
        }
        
        // ⭐ 获取regions数据（优先从dataset获取最新值）
        let regions = [];
        if (card.dataset.regions) {
            try {
                regions = JSON.parse(card.dataset.regions);
            } catch (e) {}
        }
        if (regions.length === 0 && latestStep.regions) {
            regions = latestStep.regions;
        }
        this.currentEditingRegions = JSON.parse(JSON.stringify(regions));
        this.selectedRegions = this.currentEditingRegions;
        
        // ⭐ Bug修复：同步更新 currentParentRect（从卡片或step获取）
        if (card.dataset.rect) {
            try {
                this.currentParentRect = JSON.parse(card.dataset.rect);
                console.log(`[Collab] 从card.dataset.rect更新currentParentRect`, this.currentParentRect);
            } catch (e) {}
        } else if (latestStep.rect) {
            this.currentParentRect = latestStep.rect;
            console.log(`[Collab] 从latestStep.rect更新currentParentRect`, this.currentParentRect);
        }
        
        switch (action) {
            case 'accept':
                // 接受AI选择
                this.sendConfirmResult('accept', this.selectedRegions).then((result) => {
                    if (result?.status === 'success') {
                        this._updateCollabCardToCompleted(card, (window.__lang||'zh')==='en' ? '✅ Accepted' : '✅ 已接受');
                        this._syncRightPanelStatus(step.roi_id, action);
                    }
                });
                break;
                
            case 'add':
                // 进入增加模式：点击地图上的位置添加新框
                this._startCollabAddMode(card, latestStep);
                break;
                
            case 'delete':
                // 进入删除模式：点击地图上的框删除它
                this._startCollabDeleteMode(card, latestStep);
                break;
                
            case 'edit':
                // 兼容旧代码
                this._startCollabEditMode(card, latestStep);
                break;
        }
        
        // 同步更新右侧聊天记录中的状态
        if (action !== 'accept') this._syncRightPanelStatus(step.roi_id, action);
    }
    
    // ⭐ 进入手动调整模式
    _startCollabEditMode(card, step) {
        // 显示调整提示
        this.showNotification(__t('adjust_hint'), 'info');
        
        // 获取按钮组
        const btnGroup = card.querySelector('.collab-btn-group');
        if (btnGroup) {
            // 替换按钮为"确认调整"和"取消"
            btnGroup.innerHTML = `
                <button class="collab-mini-btn collab-btn-confirm-edit" data-action="confirm-edit">
                    <i class="fas fa-check-double"></i> ${__t('btn_confirm_adjust')}
                </button>
                <button class="collab-mini-btn collab-btn-cancel-edit" data-action="cancel-edit">
                    <i class="fas fa-times"></i> ${__t('btn_cancel')}
                </button>
            `;
            
            // 绑定新按钮事件
            btnGroup.querySelector('[data-action="confirm-edit"]').addEventListener('click', (e) => {
                e.stopPropagation();
                this.sendConfirmResult('override', this.currentEditingRegions).then((result) => {
                    if (result?.status === 'success') {
                        this._updateCollabCardToCompleted(card, (window.__lang||'zh')==='en' ? '✏️ Adjusted' : '✏️ 已调整');
                    }
                });
            });
            
            btnGroup.querySelector('[data-action="cancel-edit"]').addEventListener('click', (e) => {
                e.stopPropagation();
                // 恢复原按钮
                this._restoreCollabCardButtons(card, step);
            });
        }
        
        // 启用地图上的编辑模式
        this.enableMapEditMode(true);
    }
    
    // ⭐ 进入增加模式：点击地图上的位置添加新框
    _startCollabAddMode(card, step) {
        // ⭐ 核心修复：从卡片获取 level 并写入 step，确保后续函数能正确识别
        const frameLevel = step.level || card.dataset.level || 'blue';
        step.level = frameLevel;  // ⭐ 确保 step.level 被设置
        
        // 保存当前编辑的卡片
        this.currentEditingCard = card;
        this.currentEditingStep = step;
        this.editMode = 'add';
        
        // ⭐ Bug修复：设置currentConfirmationId（从卡片或step中获取）
        this.currentConfirmationId = card.dataset.confirmationId || step.confirmation_id || null;
        console.log(`[Collab] 增加模式 confirmationId: ${this.currentConfirmationId}`);
        
        // ⭐ 首先初始化regions数据（后面推算rect需要用到）
        let regions = [];
        if (card.dataset.regions) {
            try {
                regions = JSON.parse(card.dataset.regions);
            } catch (e) {
                console.warn('解析regions数据失败:', e);
            }
        }
        if (regions.length === 0 && step.regions) {
            regions = step.regions;
        }
        this.currentEditingRegions = JSON.parse(JSON.stringify(regions));
        console.log(`[Collab] 增加模式初始化regions: ${this.currentEditingRegions.length}个`);
        
        // ⭐ Bug修复：多级回退获取父框rect
        // 优先级：step.rect > card.dataset.rect > this.currentParentRect > 从regions推算
        if (!step.rect && card.dataset.rect) {
            try {
                step.rect = JSON.parse(card.dataset.rect);
                console.log(`[Collab] 增加模式：从card.dataset.rect获取rect`, step.rect);
            } catch (e) {
                console.warn('[Collab] 解析card.dataset.rect失败:', e);
            }
        }
        if (!step.rect && this.currentParentRect) {
            step.rect = this.currentParentRect;
            console.log(`[Collab] 增加模式：从this.currentParentRect获取rect`, step.rect);
        }
        
        // ⭐ 最后的回退：从已有的regions推算父框范围
        if (!step.rect && this.currentEditingRegions && this.currentEditingRegions.length > 0) {
            const regionsForCalc = this.currentEditingRegions;
            if (regionsForCalc[0].rect) {
                // 从已有子框推算父框：找到所有子框的边界
                let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
                regionsForCalc.forEach(r => {
                    if (r.rect) {
                        minX = Math.min(minX, r.rect.x);
                        minY = Math.min(minY, r.rect.y);
                        maxX = Math.max(maxX, r.rect.x + r.rect.w);
                        maxY = Math.max(maxY, r.rect.y + r.rect.h);
                    }
                });
                // 扩展边界作为父框（扩展50%）
                const w = maxX - minX;
                const h = maxY - minY;
                step.rect = {
                    x: Math.round(minX - w * 0.25),
                    y: Math.round(minY - h * 0.25),
                    w: Math.round(w * 1.5),
                    h: Math.round(h * 1.5)
                };
                console.log(`[Collab] 增加模式：从regions推算父框rect`, step.rect);
            }
        }
        
        const frameTypeName = frameLevel === 'green' ? __t('green_box') : __t('blue_box');
        const parentTypeName = frameLevel === 'green' ? __t('blue_box') : __t('red_box');
        this.showNotification(__t('add_frame_hint', parentTypeName, frameTypeName), 'info');
        
        console.log(`[Collab] _startCollabAddMode: level=${frameLevel}, step.level=${step.level}, card.level=${card.dataset.level}, rect=`, step.rect);
        
        // 获取按钮组
        const btnGroup = card.querySelector('.collab-btn-group');
        if (btnGroup) {
            // 替换按钮为"完成增加"和"取消"
            btnGroup.innerHTML = `
                <button class="collab-mini-btn collab-btn-finish-add" data-action="finish-add">
                    <i class="fas fa-check-double"></i> ${__t('btn_done_adding')}
                </button>
                <button class="collab-mini-btn collab-btn-cancel" data-action="cancel">
                    <i class="fas fa-times"></i> ${__t('btn_cancel')}
                </button>
            `;
            
            // 绑定新按钮事件
            btnGroup.querySelector('[data-action="finish-add"]').addEventListener('click', (e) => {
                e.stopPropagation();
                this._finishAddMode(card, step);
            });
            
            btnGroup.querySelector('[data-action="cancel"]').addEventListener('click', (e) => {
                e.stopPropagation();
                this._cancelEditMode(card, step);
            });
        }
        
        // 高亮当前卡片
        card.classList.add('editing-mode', 'add-mode');
        
        // 启用地图上的点击添加模式
        this._enableMapClickToAdd(step);
    }
    
    // ⭐ 进入删除模式：点击地图上的框删除它
    _startCollabDeleteMode(card, step) {
        // ⭐ 核心修复：从卡片获取 level 并写入 step，确保后续函数能正确识别
        const frameLevel = step.level || card.dataset.level || 'blue';
        step.level = frameLevel;  // ⭐ 确保 step.level 被设置
        
        const frameTypeName = frameLevel === 'green' ? '绿框' : '蓝框';
        this.showNotification(__t('delete_frame_hint', frameTypeName), 'info');
        
        console.log(`[Collab] _startCollabDeleteMode: level=${frameLevel}, step.level=${step.level}, card.level=${card.dataset.level}`);
        
        // 保存当前编辑的卡片
        this.currentEditingCard = card;
        this.currentEditingStep = step;
        this.editMode = 'delete';
        
        // ⭐ Bug修复：设置currentConfirmationId（从卡片或step中获取）
        this.currentConfirmationId = card.dataset.confirmationId || step.confirmation_id || null;
        console.log(`[Collab] 删除模式 confirmationId: ${this.currentConfirmationId}`);
        
        // ⭐ Bug修复：初始化当前编辑的regions数据（从卡片或step中获取）
        let regions = [];
        if (card.dataset.regions) {
            try {
                regions = JSON.parse(card.dataset.regions);
            } catch (e) {
                console.warn('解析regions数据失败:', e);
            }
        }
        if (regions.length === 0 && step.regions) {
            regions = step.regions;
        }
        this.currentEditingRegions = JSON.parse(JSON.stringify(regions));
        console.log(`[Collab] 删除模式初始化regions: ${this.currentEditingRegions.length}个`);
        
        // 获取按钮组
        const btnGroup = card.querySelector('.collab-btn-group');
        if (btnGroup) {
            const _isEn = (window.__lang || 'zh') === 'en';
            btnGroup.innerHTML = `
                <button class="collab-mini-btn collab-btn-finish-delete" data-action="finish-delete">
                    <i class="fas fa-check-double"></i> ${_isEn ? 'Done Deleting' : '完成删除'}
                </button>
                <button class="collab-mini-btn collab-btn-cancel" data-action="cancel">
                    <i class="fas fa-times"></i> ${_isEn ? 'Cancel' : '取消'}
                </button>
            `;
            
            // 绑定新按钮事件
            btnGroup.querySelector('[data-action="finish-delete"]').addEventListener('click', (e) => {
                e.stopPropagation();
                this._finishDeleteMode(card, step);
            });
            
            btnGroup.querySelector('[data-action="cancel"]').addEventListener('click', (e) => {
                e.stopPropagation();
                this._cancelEditMode(card, step);
            });
        }
        
        // 高亮当前卡片
        card.classList.add('editing-mode', 'delete-mode');
        
        // 启用地图上的点击删除模式
        this._enableMapClickToDelete(step);
    }
    
    // ⭐ 完成增加模式
    _finishAddMode(card, step) {
        console.log(`[Collab] 完成增加模式: confirmationId=${this.currentConfirmationId}, regions=${this.currentEditingRegions?.length || 0}`);
        
        // 确保regions存在
        if (!this.currentEditingRegions) {
            this.currentEditingRegions = [];
        }
        
        // 更新卡片的regions数据
        card.dataset.regions = JSON.stringify(this.currentEditingRegions);
        if (card._stepData) {
            card._stepData.regions = JSON.parse(JSON.stringify(this.currentEditingRegions));
        }
        
        // 更新卡片显示的区域数量
        this._updateCardRegionsCount(card, this.currentEditingRegions.length);
        
        // Do not retire the editing UI until the server confirms this exact
        // confirmation_id.  A lost Socket packet remains safely retryable.
        this.sendConfirmResult('override', this.currentEditingRegions).then((result) => {
            if (result?.status !== 'success') return;
            this._restoreCollabCardButtons(card, card._stepData || step, false);
            card.classList.remove('editing-mode', 'add-mode');
            this.editMode = null;
            this._disableMapEditMode();
            this.showNotification((window.__lang||'zh')==='en'
                ? `✅ Added, total ${this.currentEditingRegions.length} regions`
                : `✅ 已增加，共 ${this.currentEditingRegions.length} 个区域`, 'success');
        });
    }
    
    // ⭐ 完成删除模式
    _finishDeleteMode(card, step) {
        console.log(`[Collab] 完成删除模式: confirmationId=${this.currentConfirmationId}, regions=${this.currentEditingRegions?.length || 0}`);
        
        // 确保regions存在
        if (!this.currentEditingRegions) {
            this.currentEditingRegions = [];
        }
        
        // 更新卡片的regions数据
        card.dataset.regions = JSON.stringify(this.currentEditingRegions);
        if (card._stepData) {
            card._stepData.regions = JSON.parse(JSON.stringify(this.currentEditingRegions));
        }
        
        // 更新卡片显示的区域数量
        this._updateCardRegionsCount(card, this.currentEditingRegions.length);
        
        this.sendConfirmResult('override', this.currentEditingRegions).then((result) => {
            if (result?.status !== 'success') return;
            this._restoreCollabCardButtons(card, card._stepData || step, false);
            card.classList.remove('editing-mode', 'delete-mode');
            this.editMode = null;
            this._disableMapEditMode();
            this.showNotification((window.__lang||'zh')==='en'
                ? `✅ Deleted, ${this.currentEditingRegions.length} remaining`
                : `✅ 已删除，剩余 ${this.currentEditingRegions.length} 个区域`, 'success');
        });
    }
    
    // ⭐ 取消编辑模式
    _cancelEditMode(card, step) {
        // 恢复原按钮
        this._restoreCollabCardButtons(card, step);
        
        // 清理状态
        card.classList.remove('editing-mode', 'add-mode', 'delete-mode');
        this.editMode = null;
        this._disableMapEditMode();
        
        this.showNotification(__t('cancelled'), 'info');
    }
    
    // ⭐ 启用地图点击添加模式
    _enableMapClickToAdd(step) {
        // ⭐ 确保获取到viewer
        const viewer = this.viewer || (window.pathchat && window.pathchat.viewer);
        if (!viewer) {
            console.error('[Collab] 增加模式：无法获取viewer');
            this.showNotification((window.__lang||'zh')==='en' ? '⚠️ Cannot enable add mode, please refresh' : '⚠️ 无法启用增加模式，请刷新页面', 'error');
            return;
        }
        this.viewer = viewer; // 保存引用
        
        // 获取父框的rect
        const parentRect = step.rect;
        if (!parentRect) {
            console.warn('[Collab] 无法获取父框区域');
            this.showNotification((window.__lang||'zh')==='en' ? '⚠️ Cannot get parent region' : '⚠️ 无法获取父框区域', 'error');
            return;
        }
        
        // ⭐ 核心修复：从多个来源获取 level，确保正确识别绿框模式
        // 优先级：step.level > currentEditingCard.dataset.level > currentEditingStep.level > 'blue'
        let frameLevel = step.level;
        if (!frameLevel && this.currentEditingCard) {
            frameLevel = this.currentEditingCard.dataset.level;
        }
        if (!frameLevel && this.currentEditingStep) {
            frameLevel = this.currentEditingStep.level;
        }
        frameLevel = frameLevel || 'blue';
        
        console.log(`[Collab] 增加模式: 检测到 level=${frameLevel}, step.level=${step.level}, card.level=${this.currentEditingCard?.dataset.level}`);
        
        let boxSize;
        
        if (frameLevel === 'green') {
            // ⭐ 绿框模式：优先使用已有绿框的大小，确保一致性
            if (this.currentEditingRegions && this.currentEditingRegions.length > 0) {
                const existingGreen = this.currentEditingRegions[0];
                if (existingGreen.rect) {
                    boxSize = Math.min(existingGreen.rect.w, existingGreen.rect.h);
                    console.log(`[Collab] 绿框模式：✅ 使用已有绿框大小 ${boxSize}`);
                }
            }
            // ⭐ 修复：如果没有已有绿框，使用 parentRect / 4
            // 原因：绿框的 parentRect 是蓝框（后端正确传递），蓝框/4=绿框
            if (!boxSize) {
                boxSize = Math.min(parentRect.w, parentRect.h) / 4;
                console.log(`[Collab] 绿框模式：计算绿框大小 ${boxSize} (父蓝框的1/4)`);
            }
        } else {
            // 蓝框模式：使用父红框的 1/4
            boxSize = Math.min(parentRect.w, parentRect.h) / 4;
            console.log(`[Collab] 蓝框模式：计算蓝框大小 ${boxSize} (父红框的1/4)`);
        }
        
        const frameTypeName = frameLevel === 'green' ? '绿框' : '蓝框';
        console.log(`[Collab] 启用增加模式，level=${frameLevel}, 父框:`, parentRect, `新框大小: ${boxSize}`);
        
        // ⭐ 禁用OpenSeadragon的默认点击缩放行为
        this._originalClickToZoom = viewer.gestureSettingsMouse.clickToZoom;
        viewer.gestureSettingsMouse.clickToZoom = false;
        
        // 添加点击事件监听
        this._mapClickHandler = (event) => {
            // ⭐ 阻止事件继续传播，防止触发其他点击行为
            event.preventDefaultAction = true;
            
            // 获取点击的图像坐标
            const webPoint = event.position;
            const viewportPoint = viewer.viewport.pointFromPixel(webPoint);
            const imagePoint = viewer.viewport.viewportToImageCoordinates(viewportPoint);
            
            const clickX = Math.round(imagePoint.x);
            const clickY = Math.round(imagePoint.y);
            
            // 检查点击是否在父框范围内
            if (clickX >= parentRect.x && clickX <= parentRect.x + parentRect.w &&
                clickY >= parentRect.y && clickY <= parentRect.y + parentRect.h) {
                
                // 创建新的框（在点击位置为中心）
                const newBox = {
                    roi_id: `manual_${Date.now()}`,
                    rect: {
                        x: Math.round(clickX - boxSize / 2),
                        y: Math.round(clickY - boxSize / 2),
                        w: Math.round(boxSize),
                        h: Math.round(boxSize)
                    }
                };
                
                // 添加到regions
                this.currentEditingRegions.push(newBox);
                
                // 更新地图显示（根据level使用对应颜色）
                this._addOverlayToMap(newBox, frameLevel);
                
                // 更新卡片显示
                this._updateCardRegionsCount(this.currentEditingCard, this.currentEditingRegions.length);
                
                this.showNotification((window.__lang||'zh')==='en' 
                    ? `✅ Added ${frameTypeName} (${this.currentEditingRegions.length} total)` 
                    : `✅ 已添加${frameTypeName} (共${this.currentEditingRegions.length}个)`, 'success');
            } else {
                this.showNotification((window.__lang||'zh')==='en' ? '⚠️ Click inside the parent box region' : '⚠️ 请点击父框区域内的位置', 'warning');
            }
        };
        
        viewer.addHandler('canvas-click', this._mapClickHandler);
        console.log(`[Collab] ${frameTypeName}增加模式点击事件已绑定`);
    }
    
    // ⭐ 启用地图点击删除模式
    _enableMapClickToDelete(step) {
        // ⭐ 确保获取到viewer
        const viewer = this.viewer || (window.pathchat && window.pathchat.viewer);
        if (!viewer) {
            console.error('[Collab] 删除模式：无法获取viewer');
            this.showNotification((window.__lang||'zh')==='en' ? '⚠️ Cannot enable delete mode, please refresh' : '⚠️ 无法启用删除模式，请刷新页面', 'error');
            return;
        }
        this.viewer = viewer; // 保存引用
        
        // ⭐ 核心修复：根据 level 确定框类型名称
        const frameLevel = step.level || 'blue';
        const frameTypeName = frameLevel === 'green' ? '绿框' : '蓝框';
        
        console.log(`[Collab] 启用${frameTypeName}删除模式，当前regions: ${this.currentEditingRegions?.length || 0}个`);
        
        // ⭐ 禁用OpenSeadragon的默认点击缩放行为
        this._originalClickToZoom = viewer.gestureSettingsMouse.clickToZoom;
        viewer.gestureSettingsMouse.clickToZoom = false;
        
        // 添加点击事件监听
        this._mapClickHandler = (event) => {
            // ⭐ 阻止事件继续传播，防止触发其他点击行为
            event.preventDefaultAction = true;
            
            // 获取点击的图像坐标
            const webPoint = event.position;
            const viewportPoint = viewer.viewport.pointFromPixel(webPoint);
            const imagePoint = viewer.viewport.viewportToImageCoordinates(viewportPoint);
            
            const clickX = Math.round(imagePoint.x);
            const clickY = Math.round(imagePoint.y);
            
            console.log(`[Collab] ${frameTypeName}删除模式点击: (${clickX}, ${clickY}), regions数量: ${this.currentEditingRegions?.length || 0}`);
            
            // 查找点击位置对应的框
            if (!this.currentEditingRegions || this.currentEditingRegions.length === 0) {
                this.showNotification((window.__lang||'zh')==='en' 
                    ? `⚠️ No ${frameTypeName} to delete` 
                    : `⚠️ 没有可删除的${frameTypeName}`, 'warning');
                return;
            }
            
            const clickedIndex = this.currentEditingRegions.findIndex(region => {
                const rect = region.rect;
                if (!rect) return false;
                const inBox = clickX >= rect.x && clickX <= rect.x + rect.w &&
                       clickY >= rect.y && clickY <= rect.y + rect.h;
                if (inBox) {
                    console.log(`[Collab] 找到匹配的${frameTypeName}: ${region.roi_id}`, rect);
                }
                return inBox;
            });
            
            if (clickedIndex >= 0) {
                const removed = this.currentEditingRegions.splice(clickedIndex, 1)[0];
                console.log(`[Collab] 删除${frameTypeName}: ${removed.roi_id}`);
                
                // 从地图移除overlay
                this._removeOverlayFromMap(removed.roi_id);
                
                // 更新卡片显示
                this._updateCardRegionsCount(this.currentEditingCard, this.currentEditingRegions.length);
                
                this.showNotification((window.__lang||'zh')==='en' 
                    ? `🗑️ Deleted ${frameTypeName} (${this.currentEditingRegions.length} remaining)` 
                    : `🗑️ 已删除${frameTypeName} (剩余${this.currentEditingRegions.length}个)`, 'success');
            } else {
                console.log(`[Collab] 未找到匹配的${frameTypeName}，点击位置: (${clickX}, ${clickY})`);
                this.showNotification((window.__lang||'zh')==='en' 
                    ? `⚠️ Click the ${frameTypeName} to delete it` 
                    : `⚠️ 请点击要删除的${frameTypeName}`, 'warning');
            }
        };
        
        viewer.addHandler('canvas-click', this._mapClickHandler);
        console.log(`[Collab] ${frameTypeName}删除模式点击事件已绑定，已禁用点击缩放`);
    }
    
    // ⭐ 禁用地图编辑模式
    _disableMapEditMode() {
        const viewer = this.viewer || (window.pathchat && window.pathchat.viewer);
        
        if (viewer && this._mapClickHandler) {
            viewer.removeHandler('canvas-click', this._mapClickHandler);
            this._mapClickHandler = null;
        }
        
        // ⭐ 恢复OpenSeadragon的默认点击缩放行为
        if (viewer && this._originalClickToZoom !== undefined) {
            viewer.gestureSettingsMouse.clickToZoom = this._originalClickToZoom;
            this._originalClickToZoom = undefined;
            console.log('[Collab] 已恢复点击缩放行为');
        }
    }
    
    // ⭐ 添加overlay到地图
    _addOverlayToMap(region, level) {
        if (!this.viewer || !window.pathchat) return;
        
        const rect = region.rect;
        
        // 创建overlay元素 - 只有边框，无背景填充（与其他框保持一致）
        const overlayEl = document.createElement('div');
        overlayEl.id = `overlay-${region.roi_id}`;
        overlayEl.className = `roi-overlay roi-overlay-${level}`;
        // ⭐ 核心修复：添加 data-roi-id 属性，使点击事件能识别
        overlayEl.dataset.roiId = region.roi_id;
        overlayEl.dataset.level = level;
        const semanticLevel = window.PathCopilotRoiPalette?.normalizeLevel(level) ||
            (level === 'green' ? 'level3' : level === 'blue' ? 'level2' : 'level1');
        const displayColor = window.PathCopilotRoiPalette?.getColor(semanticLevel) ||
            (semanticLevel === 'level3' ? '#00A63D' : semanticLevel === 'level2' ? '#0066FF' : '#FF0000');
        overlayEl.dataset.roiLevel = semanticLevel;
        overlayEl.setAttribute('aria-label', `${window.PathCopilotRoiPalette?.getLevelLabel(semanticLevel) || semanticLevel} ROI`);
        // ⭐ 使用 !important 确保不被其他样式覆盖
        overlayEl.style.cssText = `
            border: 4px solid ${displayColor} !important;
            background: transparent !important;
            background-color: transparent !important;
            box-sizing: border-box;
            cursor: pointer;
        `;
        
        // 转换坐标
        const tiledImage = this.viewer.world.getItemAt(0);
        if (tiledImage) {
            const imageRect = tiledImage.imageToViewportRectangle(rect.x, rect.y, rect.w, rect.h);
            this.viewer.addOverlay({
                element: overlayEl,
                location: imageRect
            });
        }
    }
    
    // ⭐ 从地图移除overlay
    _removeOverlayFromMap(roiId) {
        const viewer = this.viewer || (window.pathchat && window.pathchat.viewer);
        if (!viewer) {
            console.warn('[Collab] 无法移除overlay：viewer不存在');
            return;
        }
        
        console.log(`[Collab] 尝试移除overlay: ${roiId}`);
        
        // 尝试多种方式查找并移除overlay
        let removed = false;
        
        // 方式1: 通过ID查找
        const overlayEl = document.getElementById(`overlay-${roiId}`);
        if (overlayEl) {
            viewer.removeOverlay(overlayEl);
            removed = true;
            console.log(`[Collab] 通过ID移除overlay成功: overlay-${roiId}`);
        }
        
        // 方式2: 通过roi-overlay-{id}查找
        const overlayEl2 = document.getElementById(`roi-overlay-${roiId}`);
        if (overlayEl2) {
            viewer.removeOverlay(overlayEl2);
            removed = true;
            console.log(`[Collab] 通过roi-overlay-ID移除overlay成功: roi-overlay-${roiId}`);
        }
        
        // 方式3: 遍历所有overlay查找
        const overlays = viewer.currentOverlays || [];
        overlays.forEach(overlay => {
            if (overlay.element) {
                const elId = overlay.element.id || '';
                const elRoiId = overlay.element.dataset?.roiId || '';
                if (elId.includes(roiId) || elRoiId === roiId) {
                    viewer.removeOverlay(overlay.element);
                    removed = true;
                    console.log(`[Collab] 通过遍历移除overlay成功: ${elId || elRoiId}`);
                }
            }
        });
        
        // 方式4: 查找所有带data-roi-id属性的元素
        document.querySelectorAll(`[data-roi-id="${roiId}"]`).forEach(el => {
            try {
                viewer.removeOverlay(el);
                removed = true;
                console.log(`[Collab] 通过data-roi-id移除overlay成功`);
            } catch (e) {
                // 忽略错误
            }
        });
        
        if (!removed) {
            console.warn(`[Collab] 未找到要移除的overlay: ${roiId}`);
        }
    }
    
    // ⭐ 更新卡片的regions数量显示
    _updateCardRegionsCount(card, count) {
        if (!card) return;
        
        let countEl = card.querySelector('.collab-card-count');
        if (countEl) {
            countEl.textContent = __t('sub_region_count', count);
        } else {
            countEl = document.createElement('div');
            countEl.className = 'collab-card-count';
            countEl.textContent = __t('sub_region_count', count);
            const header = card.querySelector('.collab-card-header');
            if (header) {
                header.after(countEl);
            }
        }
        
        // 更新dataset
        card.dataset.regions = JSON.stringify(this.currentEditingRegions);
    }
    
    // ⭐ 恢复卡片按钮
    // @param showCancelNotification - 是否显示"已取消调整"通知（默认true，完成增加/删除时传false）
    _restoreCollabCardButtons(card, step, showCancelNotification = true) {
        const btnGroup = card.querySelector('.collab-btn-group');
        if (btnGroup) {
            btnGroup.innerHTML = `
                <button class="collab-mini-btn collab-btn-accept" data-action="accept" title="接受AI选择">
                    <i class="fas fa-check"></i> 接受
                </button>
                <button class="collab-mini-btn collab-btn-add" data-action="add" title="点击地图添加框">
                    <i class="fas fa-plus"></i> 增加
                </button>
                <button class="collab-mini-btn collab-btn-delete" data-action="delete" title="点击地图删除框">
                    <i class="fas fa-trash"></i> 删除
                </button>
            `;
            
            // 重新绑定事件
            const list = document.getElementById('routeStepsList');
            btnGroup.querySelectorAll('.collab-mini-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    this._handleCollabButtonClick(card, step, btn.dataset.action, list);
                });
            });
        }
        
        this.enableMapEditMode(false);
        
        // 只在取消时显示通知，完成增加/删除时不显示
        if (showCancelNotification) {
            this.showNotification(__t('cancel_adjust'), 'info');
        }
    }
    
    // ⭐ 更新卡片为完成状态
    _updateCollabCardToCompleted(card, statusText) {
        // ⭐ 统一确认模式：不删除卡片，只更新状态显示
        const isUnifiedMode = this.pendingBlueConfirmations && Object.keys(this.pendingBlueConfirmations).length > 0;
        
        card.classList.remove('status-pending', 'status-active', 'status-skipped');
        card.classList.add('status-done');
        
        // ⭐ 问题1修复：更新标题文本，显示正确的框选类型
        const titleEl = card.querySelector('.collab-card-title .title-text');
        if (titleEl) {
            const level = card.dataset.level;
            const roiId = card.dataset.roiId || '';
            let newTitle = titleEl.textContent;
            
            // 根据级别生成正确的标题
            if (level === 'blue' || roiId.includes('_sub_')) {
                // 从roi_id中提取红框编号（如 roi_10x_r1_c2）
                const match = roiId.match(/roi_10x_r(\d+)_c(\d+)/);
                const redNum = match ? `#${parseInt(match[1]) * 4 + parseInt(match[2]) + 1}` : '';
                newTitle = `蓝框框选完成 ${redNum}`;
            } else if (level === 'green') {
                newTitle = '绿框框选完成';
            }
            titleEl.textContent = newTitle;
        }
        
        // 更新状态文本
        const statusEl = card.querySelector('.collab-card-status');
        if (statusEl) {
            // 统一确认模式下显示不同的状态文本
            const pendingTag = (window.__lang||'zh')==='en' ? '(pending batch confirm)' : '(待统一确认)';
            statusEl.textContent = isUnifiedMode ? `${statusText} ${pendingTag}` : statusText;
        }
        
        // ⭐ 统一确认模式：保留按钮组，允许用户继续调整
        if (isUnifiedMode) {
            // 保留按钮，添加"已处理"标记
            card.dataset.processed = 'true';
            card.classList.add('processed-pending');
        } else {
            // 移除按钮组
            const btnGroup = card.querySelector('.collab-btn-group');
            if (btnGroup) {
                btnGroup.remove();
            }
        }
        
        // 添加完成动画
        card.classList.add('flash-highlight');
        setTimeout(() => card.classList.remove('flash-highlight'), 600);
        
        // 清理高亮
        this.clearConfirmationHighlights();
        
        // ⭐ 非统一确认模式：3秒后自动删除已确认的卡片
        if (!isUnifiedMode) {
            setTimeout(() => {
                if (card && card.parentNode) {
                    card.classList.add('fade-out');
                    setTimeout(() => {
                        if (card && card.parentNode) {
                            card.remove();
                            console.log('[Collab] 已删除确认完成的卡片');
                        }
                    }, 300);
                }
            }, 3000);
        }
    }
    
    // ⭐ 更新卡片为跳过状态
    _updateCollabCardToSkipped(card) {
        // ⭐ 统一确认模式：不删除卡片，只更新状态
        const isUnifiedMode = this.pendingBlueConfirmations && Object.keys(this.pendingBlueConfirmations).length > 0;
        
        card.classList.remove('status-pending', 'status-active', 'status-done');
        card.classList.add('status-skipped');
        
        const statusEl = card.querySelector('.collab-card-status');
        if (statusEl) {
            const _e = (window.__lang||'zh')==='en';
            const skipLabel = _e ? '⏭️ Skipped' : '⏭️ 已跳过';
            const pendingTag = _e ? ' (pending batch confirm)' : ' (待统一确认)';
            statusEl.textContent = isUnifiedMode ? skipLabel + pendingTag : skipLabel;
        }
        
        // ⭐ 统一确认模式：保留按钮组
        if (isUnifiedMode) {
            card.dataset.processed = 'true';
            card.classList.add('processed-pending');
        } else {
            // 移除按钮组
            const btnGroup = card.querySelector('.collab-btn-group');
            if (btnGroup) {
                btnGroup.remove();
            }
        }
        
        // 清理高亮
        this.clearConfirmationHighlights();
    }
    
    // ⭐ 同步更新右侧聊天记录状态
    _syncRightPanelStatus(roiId, action) {
        if (!roiId) return;
        
        // 查找右侧对应的确认卡片
        const selectors = [
            `.confirmation-card[data-parent-roi-id="${roiId}"]`,
            `.confirmation-card[data-roi-id="${roiId}"]`
        ];
        
        let rightCard = null;
        for (const selector of selectors) {
            rightCard = document.querySelector(selector);
            if (rightCard) break;
        }
        
        if (rightCard) {
            // 添加状态标记
            const header = rightCard.querySelector('.confirmation-card-header');
            if (header && !header.querySelector('.confirmation-status')) {
                const _e = (window.__lang||'zh')==='en';
                const statusText = _e 
                    ? { 'accept': '✅ Accepted', 'override': '✏️ Adjusted', 'skip': '⏩ Skipped' }
                    : { 'accept': '✅ 已接受', 'override': '✏️ 已调整', 'skip': '⏩ 已跳过' };
                header.innerHTML += `<span class="confirmation-status">${statusText[action] || (_e ? 'Done' : '已处理')}</span>`;
            }
            
            // 禁用右侧按钮
            const buttons = rightCard.querySelectorAll('.confirmation-btn');
            buttons.forEach(btn => {
                btn.disabled = true;
                btn.style.opacity = '0.5';
                btn.style.cursor = 'not-allowed';
            });
        }
    }
    
    // ⭐ 启用/禁用地图编辑模式
    enableMapEditMode(enable) {
        // 如果有专门的编辑模式逻辑，在这里调用
        if (enable) {
            // 可以在这里启用拖拽、绘制等功能
            console.log('[Collab] 启用地图编辑模式');
        } else {
            console.log('[Collab] 禁用地图编辑模式');
        }
    }
    
    // ⭐ 协作模式专用：滚动到右侧聊天区的确认消息
    // 注意：highlightEditableRegions 函数已统一到第3288行的4参数版本
    scrollToChatConfirmation(roiId) {
        if (!roiId) return;
        
        // 查找对应的确认卡片（多种选择器兼容）
        const selectors = [
            `.confirmation-card[data-parent-roi-id="${roiId}"]`,
            `.confirmation-card[data-roi-id="${roiId}"]`,
            `.message-item[data-roi-id="${roiId}"]`,
            `.chat-message[data-roi-id="${roiId}"]`,
            `#message-${roiId}`
        ];
        
        let targetElement = null;
        for (const selector of selectors) {
            targetElement = document.querySelector(selector);
            if (targetElement) break;
        }
        
        if (targetElement) {
            // 滚动到目标元素
            targetElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
            
            // 添加闪烁高亮效果
            targetElement.classList.add('flash-highlight');
            setTimeout(() => {
                targetElement.classList.remove('flash-highlight');
            }, 1000);
        }
        
        // ⭐ 已删除右侧跳转：右侧聊天面板只用于最终报告和用户自由发言
    }
    
    // ⭐ 更新路径步骤状态（支持更新 rect 用于点击跳转）
    _normalizeTreeRoiId(roiId) {
        return String(roiId || '').trim().toUpperCase().replace(/[^A-Z0-9]/g, '');
    }

    _findTreeNodeByRoiId(root, roiId, selector = '[data-roi-id]') {
        if (!root || !roiId) return null;
        const target = this._normalizeTreeRoiId(roiId);
        const nodes = Array.from(root.querySelectorAll(selector)).filter(node =>
            this._normalizeTreeRoiId(node.dataset.roiId) === target
        );
        if (!nodes.length) return null;
        const scoped = this._patientSwarmTreeWsiIndex();
        if (Number.isInteger(scoped)) {
            const hit = nodes.find(node => Number(node.dataset.wsiIndex) === scoped);
            if (hit) return hit;
        }
        const viewing = Number(this.currentViewingWsiIndex);
        if (this._isPatientSwarmMapMode() && Number.isInteger(viewing)) {
            const hit = nodes.find(node => Number(node.dataset.wsiIndex) === viewing);
            if (hit) return hit;
        }
        if (this._isPatientParallelRun()) return null;
        return nodes[0];
    }

    _getStoredRoiInterpretation(roiId) {
        const store = window.pathchat?.roiAnalysisData || {};
        const target = this._normalizeTreeRoiId(roiId);
        const matchedKey = Object.keys(store).find(key => this._normalizeTreeRoiId(key) === target);
        const value = matchedKey ? store[matchedKey] : null;
        if (typeof value === 'string') return value;
        return value?.gpt_analysis || value?.analysis || value?.reasoning ||
               value?.content || value?.summary || value?.result || '';
    }

    _completeGreenTreeNode(card, content = null, reasoning = null) {
        if (!card || !card.classList.contains('route-tree-green-item')) return;
        const roiId = card.dataset.roiId || '';
        const titleEl = card.querySelector('.route-tree-green-title');
        if (titleEl) {
            titleEl.textContent = `\u{1F7E2} ${roiId} ${__t('status_completed')}`;
        }
        card.classList.remove('in-progress', 'waiting');
        card.classList.add('completed');

        const rawContent = reasoning || content || this._getStoredRoiInterpretation(roiId);
        const displayContent = this._escapeHtmlContent(rawContent);
        if (!displayContent) return;
        let contentEl = card.querySelector('.route-tree-green-content');
        if (!contentEl) {
            contentEl = document.createElement('div');
            contentEl.className = 'route-tree-green-content';
            card.appendChild(contentEl);
        } else if (!reasoning) {
            // ⭐ 防降级：done 时的批量补全不带 reasoning，只有存储的摘要；
            // 卡片上已有更长的完整解读时只补状态，不覆盖正文。
            const existingText = String(contentEl.textContent || '').trim();
            if (existingText.length > String(rawContent || '').trim().length) return;
        }
        contentEl.innerHTML = displayContent;
    }

    updateRouteStepStatus(roiId, status, content = null, rect = null, reasoning = null) {
        const list = document.getElementById('routeStepsList');
        if (!list) return;
        
        const card = this._findMappedTreeCard(roiId)
            || this._findTreeNodeByRoiId(list, roiId, '.collab-task-card[data-roi-id], .route-tree-green-item[data-roi-id], .route-tree-blue-node[data-roi-id], .route-tree-node[data-roi-id], .route-step-card[data-roi-id], .collab-tree-node[data-roi-id]');
        if (!card) return;
        
        // ⭐ 分流：协作模式 vs 自动模式
        if ((this.currentMode === 'collaborative' || this.isCollaborativeMode) && card.classList.contains('collab-task-card')) {
            // 协作模式：更新工作站卡片
            this._updateCollabCardStatus(card, status, reasoning || content);
        } else {
            // 自动模式：保持原有逻辑
            if (status === 'completed' && card.classList.contains('route-tree-green-item')) {
                this._completeGreenTreeNode(card, content, reasoning);
            }
            const badge = card.querySelector('.route-step-badge, .route-tree-badge');
            if (badge) {
                badge.className = badge.className.includes('route-tree') ? 
                    `route-tree-badge ${status === 'completed' ? 'completed' : 'in-progress'}` :
                    `route-step-badge ${status}`;
                badge.textContent = status === 'completed' ? __t('status_completed') :
                                   status === 'pending' ? __t('status_pending_confirm') : __t('status_in_progress');
            }
            
            // 更新内容（优先使用reasoning）
            const displayContent = this._escapeHtmlContent(reasoning || content);
            if (displayContent) {
                let contentDiv = card.querySelector('.route-tree-content, .route-tree-blue-content, .route-tree-green-content, .route-step-content');
                if (contentDiv) {
                    // 更新已存在的内容区域
                    contentDiv.innerHTML = displayContent;
                } else {
                    // ⭐ Bug修复：如果内容区域不存在，创建一个新的
                    const header = card.querySelector('.route-tree-header, .route-tree-blue-header, .route-tree-green-title, .route-step-header');
                    if (header) {
                        contentDiv = document.createElement('div');
                        // 根据卡片类型决定内容区域的class
                        if (card.classList.contains('route-tree-node')) {
                            contentDiv.className = 'route-tree-content';
                        } else if (card.dataset.level === 'blue') {
                            contentDiv.className = 'route-tree-blue-content';
                        } else if (card.classList.contains('route-tree-green-item')) {
                            contentDiv.className = 'route-tree-green-content';
                        } else {
                            contentDiv.className = 'route-step-content';
                        }
                        contentDiv.innerHTML = displayContent;
                        header.insertAdjacentElement('afterend', contentDiv);
                        console.log(`✅ 为 ${roiId} 创建了内容区域`);
                    }
                }
            }
        }
        
        // ⭐ 更新 rect 信息（用于点击跳转）
        if (rect) {
            card.dataset.rect = JSON.stringify(rect);
        }
    }
    
    _writeCollabCardAnalysis(card, content) {
        const analysisText = String(content || '').trim();
        if (!card || !analysisText) return;
        let body = card.querySelector('.collab-card-analysis');
        if (!body) {
            body = document.createElement('div');
            body.className = 'collab-card-analysis';
            const header = card.querySelector('.collab-card-header');
            const count = card.querySelector('.collab-card-count');
            (count || header)?.insertAdjacentElement('afterend', body);
        }
        body.textContent = analysisText;
        if (card._stepData) {
            card._stepData.reasoning = analysisText;
            card._stepData.content = analysisText;
        }
    }

    // 协作模式：更新卡片状态。已确认 ≠ 删除，解读必须留在卡上。
    _updateCollabCardStatus(card, status, content) {
        // 移除旧状态类
        card.classList.remove('status-pending', 'status-active', 'status-done');
        
        // 添加新状态类
        let statusClass, statusIcon, statusText;
        if (status === 'completed') {
            statusClass = 'status-done';
            statusIcon = 'fa-check-circle';
            statusText = __t('status_completed_collab');
        } else if (status === 'pending') {
            statusClass = 'status-pending';
            statusIcon = 'fa-hourglass-half';
            statusText = __t('status_waiting_confirm');
        } else {
            statusClass = 'status-active';
            statusIcon = 'fa-spinner';
            statusText = __t('status_processing');
        }
        
        card.classList.add(statusClass);
        this._writeCollabCardAnalysis(card, content);
        
        // 更新状态图标
        const statusIconEl = card.querySelector('.card-status i');
        if (statusIconEl) {
            statusIconEl.className = `fas ${statusIcon}`;
        }
        
        // 更新状态文本
        const subtitleEl = card.querySelector('.card-subtitle');
        if (subtitleEl) {
            subtitleEl.textContent = statusText;
        }
        
        // 如果是完成状态，添加完成动画
        if (status === 'completed') {
            card.classList.add('flash-highlight');
            setTimeout(() => {
                card.classList.remove('flash-highlight');
            }, 600);
        }
    }
    
    // ⭐ 统一的ROI聚焦函数（左/右点击都用这个）
    focusOnRoiFromMeta(meta) {
        const viewer = window.pathchat?.viewer;
        if (!viewer || !meta) return;
        
        // 只从 viewer overlay / roiOverlays 补 rect，不要命中中间栏树节点
        let rect = this._resolveRoiRect(meta.roi_id, meta.rect);
        
        if (!rect) {
            console.warn('无法聚焦：缺少rect信息', meta);
            return;
        }
        
        // 确定框颜色
        const color = meta.level === 'red' ? '#FF4444' :
                     meta.level === 'blue' ? '#00AAFF' : '#00FF88';
        
        // 清除旧的焦点高亮
        this.clearFocusHighlight();
        
        // 添加新的焦点高亮
        const highlightId = 'route-focus-' + (meta.roi_id || Date.now());
        this.addFocusHighlight(viewer, rect, highlightId, color);
        
        // 缩放到区域
        const vpRect = viewer.viewport.imageToViewportRectangle(
            rect.x, rect.y, rect.w, rect.h
        );
        const padded = new OpenSeadragon.Rect(
            vpRect.x - vpRect.width * 0.15,
            vpRect.y - vpRect.height * 0.15,
            vpRect.width * 1.3,
            vpRect.height * 1.3
        );
        viewer.viewport.fitBounds(padded, true);

        if (meta.roi_id && typeof window.pathchat.highlightROI === 'function') {
            window.pathchat.highlightROI(meta.roi_id);
        }
        
        console.log('聚焦到区域:', meta.roi_id || meta.level);
    }
    
    // ⭐ 滚动到左侧面板对应的树节点（中→左跳转）
    scrollToLeftTreeNode(roiId) {
        const list = document.getElementById('routeStepsList');
        if (!list || !roiId) return;
        
        // 查找匹配的节点（支持 data-tree-id 和 data-roi-id）
        let node = list.querySelector(`[data-tree-id="${roiId}"]`) ||
                   list.querySelector(`[data-roi-id="${roiId}"]`);
        
        // 如果精确匹配找不到，尝试模糊匹配（子框ID可能包含父框ID）
        if (!node) {
            const allNodes = list.querySelectorAll('[data-tree-id], [data-roi-id]');
            for (const n of allNodes) {
                const treeId = n.getAttribute('data-tree-id') || '';
                const nodeRoiId = n.getAttribute('data-roi-id') || '';
                if (roiId.includes(treeId) || treeId.includes(roiId) ||
                    roiId.includes(nodeRoiId) || nodeRoiId.includes(roiId)) {
                    node = n;
                    break;
                }
            }
        }
        
        if (node) {
            // 清除之前的高亮（发光黄色效果）
            list.querySelectorAll('.roi-selected-glow').forEach(n => {
                n.classList.remove('roi-selected-glow');
            });
            
            // 滚动并添加发光黄色高亮
            node.scrollIntoView({ behavior: 'smooth', block: 'center' });
            node.classList.add('roi-selected-glow');
            
            console.log('✅ 已跳转到左侧节点:', roiId);
        } else {
            console.warn('⚠️ 未找到左侧对应节点:', roiId);
        }
    }
    
    // ⭐ 高亮右侧对应的消息（三向映射：左→中→右）
    highlightRightMessage(roiId) {
        if (!roiId) return;
        
        // 清除之前的高亮
        document.querySelectorAll('.roi-highlighted').forEach(el => {
            el.classList.remove('roi-highlighted');
            el.style.backgroundColor = '';
            el.style.borderLeft = '';
        });
        
        // ⭐ 查找对应的消息（优先精确匹配，其次模糊匹配）
        let messageElement = null;
        
        // 方法1: 通过ID精确查找
        messageElement = document.getElementById(`message-${roiId}`);
        
        // 方法2: 通过 data-roi-id 精确查找（只在聊天区域）
        if (!messageElement) {
            const chatMessages = document.querySelectorAll('.message-item[data-roi-id], .chat-message[data-roi-id]');
            for (const msg of chatMessages) {
                if (msg.getAttribute('data-roi-id') === roiId) {
                    messageElement = msg;
                    break;
                }
            }
        }
        
        // 方法3: 模糊匹配 - 查找包含该ROI ID的消息（例如：roi_10x_r0_c0_sub_r1_c2 匹配 roi_10x_r0_c0）
        if (!messageElement) {
            const chatMessages = document.querySelectorAll('.message-item[data-roi-id], .chat-message[data-roi-id]');
            for (const msg of chatMessages) {
                const msgRoiId = msg.getAttribute('data-roi-id');
                // 检查是否是父子关系（子ROI包含父ROI的ID前缀）
                if (msgRoiId && (roiId.startsWith(msgRoiId) || msgRoiId.startsWith(roiId))) {
                    messageElement = msg;
                    break;
                }
            }
        }
        
        if (messageElement) {
            messageElement.classList.add('roi-highlighted');
            messageElement.style.backgroundColor = 'rgba(0, 170, 255, 0.15)';
            messageElement.style.borderLeft = '4px solid #00AAFF';
            messageElement.style.transition = 'all 0.3s ease';
            messageElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
            console.log('✅ 高亮右侧消息:', roiId);
        } else {
            console.log('ℹ️ 该ROI暂无右侧消息:', roiId, '(进度消息不在右侧显示)');
        }
    }
    
    // ⭐ 添加焦点高亮框
    // ⭐ Bug修复：去掉背景填充，只加粗边框
    // ⭐ 使用 !important 确保不被其他样式覆盖
    addFocusHighlight(viewer, rect, id, color) {
        const overlay = document.createElement('div');
        overlay.id = id;
        overlay.className = 'route-focus-highlight';
        overlay.style.cssText = `
            border: 6px solid ${color} !important;
            background: transparent !important;
            background-color: transparent !important;
            box-sizing: border-box;
            pointer-events: none;
            animation: route-focus-pulse 1s ease-out;
            box-shadow: 0 0 12px ${color}80;
        `;
        
        const vpRect = viewer.viewport.imageToViewportRectangle(
            rect.x, rect.y, rect.w, rect.h
        );
        
        viewer.addOverlay({
            element: overlay,
            location: vpRect
        });
        
        // 记录以便清理
        if (!this.focusHighlightId) this.focusHighlightId = [];
        this.focusHighlightId.push(id);
    }
    
    // ⭐ 清除焦点高亮
    clearFocusHighlight() {
        const viewer = window.pathchat?.viewer;
        if (!viewer || !this.focusHighlightId) return;
        
        this.focusHighlightId.forEach(id => {
            const el = document.getElementById(id);
            if (el) viewer.removeOverlay(el);
        });
        this.focusHighlightId = [];
    }
    
    // ⭐ 添加结果到左侧面板（只显示真正的分析结果）
    addResultToLeftPanel(data) {
        const resultsList = document.getElementById('resultsList');
        if (!resultsList) return;
        
        const content = data.content || '';
        const roiId = data.roi_id || '';
        
        // ⭐ 过滤掉"开始分析"类消息，只显示真正的结果
        const isStartMessage = content.includes('开始分析') || 
                              content.includes('Begin analysis') ||
                              content.length < 30 ||
                              (content.includes('🔴') && content.length < 50) ||
                              (content.includes('🔵') && content.length < 50) ||
                              (content.includes('🟢') && content.length < 50);
        
        // 必须包含实际分析内容
        const hasRealContent = content.includes('分析结果') ||
                              content.includes('组织') ||
                              content.includes('腺体') ||
                              content.includes('细胞') ||
                              content.includes('病理') ||
                              content.includes('结构') ||
                              content.includes('形态') ||
                              content.includes('浸润') ||
                              content.length > 80;
        
        if (isStartMessage || !hasRealContent) {
            console.log('⏭️ 跳过非结果消息:', content.substring(0, 30));
            return;
        }
        
        // 移除空状态提示
        const emptyState = resultsList.querySelector('.results-empty');
        if (emptyState) emptyState.remove();
        
        // 判断框类型
        let frameType = 'red-frame';
        let frameLabel = '红框';
        let frameIcon = '🔴';
        
        if (roiId.includes('_sub_r') && roiId.includes('_c')) {
            if (roiId.split('_sub_').length === 2) {
                frameType = 'blue-frame';
                frameLabel = '蓝框';
                frameIcon = '🔵';
            } else {
                frameType = 'green-frame';
                frameLabel = '绿框';
                frameIcon = '🟢';
            }
        }
        
        // 提取红框编号
        const redMatch = roiId.match(/roi_10x_r(\d+)/);
        const redIndex = redMatch ? parseInt(redMatch[1]) + 1 : '?';
        
        // 检查是否已存在相同roiId的卡片
        const existingItem = resultsList.querySelector(`[data-roi-id="${roiId}"]`);
        if (existingItem) {
            const bodyEl = existingItem.querySelector('.result-item-body p');
            const badgeEl = existingItem.querySelector('.result-item-badge');
            if (bodyEl) bodyEl.textContent = content.length > 150 ? content.substring(0, 150) + '...' : content;
            if (badgeEl) {
                badgeEl.textContent = __t('status_completed');
                badgeEl.classList.add('completed');
            }
            return;
        }
        
        // 创建结果卡片
        const resultItem = document.createElement('div');
        resultItem.className = `result-item ${frameType}`;
        resultItem.dataset.roiId = roiId;
        
        const shortContent = content.length > 150 ? content.substring(0, 150) + '...' : content;
        
        resultItem.innerHTML = `
            <div class="result-item-header">
                <span class="result-item-title">
                    ${frameIcon} ${frameLabel} #${redIndex}
                </span>
                <span class="result-item-badge completed">完成</span>
            </div>
            <div class="result-item-body">
                <p>${shortContent}</p>
            </div>
        `;
        
        // 点击结果卡片时高亮
        resultItem.addEventListener('click', () => {
            resultsList.querySelectorAll('.result-item.active').forEach(item => {
                item.classList.remove('active');
            });
            resultItem.classList.add('active');
            this.highlightCorrespondingCards(roiId);
            this.zoomToRoiRegion(roiId);
            console.log('点击左侧结果卡片:', roiId);
        });
        
        resultsList.appendChild(resultItem);
        resultsList.scrollTop = resultsList.scrollHeight;
    }
    
    // ⭐ 高亮右侧对应的确认卡片
    highlightCorrespondingCards(roiId) {
        const redMatch = roiId.match(/roi_10x_r(\d+)/);
        if (!redMatch) return;
        
        const redIndex = parseInt(redMatch[1]);
        
        document.querySelectorAll('.confirmation-card').forEach(card => {
            card.classList.remove('active-viewing');
            
            const cardData = card._confirmData;
            if (cardData && cardData.parentRoiId) {
                const cardRedMatch = cardData.parentRoiId.match(/roi_10x_r(\d+)/);
                if (cardRedMatch && parseInt(cardRedMatch[1]) === redIndex) {
                    card.classList.add('active-viewing');
                    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
            }
        });
    }
    
    // ⭐ 定位到WSI上的ROI区域
    zoomToRoiRegion(roiId) {
        if (!window.pathchat || !window.pathchat.viewer) return;
        
        const rect = this._resolveRoiRect(roiId);
        if (rect) {
            try {
                const viewer = window.pathchat.viewer;
                const viewportRect = viewer.viewport.imageToViewportRectangle(
                    rect.x, rect.y, rect.w, rect.h
                );
                const padding = 0.15;
                const paddedRect = new OpenSeadragon.Rect(
                    viewportRect.x - viewportRect.width * padding,
                    viewportRect.y - viewportRect.height * padding,
                    viewportRect.width * (1 + 2 * padding),
                    viewportRect.height * (1 + 2 * padding)
                );
                viewer.viewport.fitBounds(paddedRect, true);
            } catch (e) {
                console.warn('无法解析区域坐标:', e);
            }
        }
    }
    
    getCurrentFileId() {
        // 优先使用设置的文件ID
        if (this.currentFileId) {
            return this.currentFileId;
        }
        
        // 从pathchat获取当前文件ID
        if (window.pathchat && window.pathchat.currentFile) {
            return window.pathchat.currentFile.file_id;
        }
        
        return null;
    }
    
    async runAgent(diagnosticQuestion = '', options = {}) {
        console.log('点击Run Agent按钮');
        const normalizedQuestion = String(diagnosticQuestion || '').trim();

        if (this.isRunning || this.isStarting) {
            this.showNotification(__t('analysis_in_progress'), 'info');
            return false;
        }
        // Acquire the launch lock before the first await so two synchronous
        // callers cannot both pass the guard and POST separate commands.
        this.isStarting = true;
        this.isStopping = false;
        this.updateButtons();
        const releaseLaunchLock = () => {
            this.isStarting = false;
            this.updateButtons();
        };

        if (
            window.PathCopilotApiKeyGuard
            && !(await window.PathCopilotApiKeyGuard.ensureConfigured())
        ) {
            releaseLaunchLock();
            return false;
        }
        
        const fileId = this.getCurrentFileId();
        if (!fileId) {
            this.showNotification(__t('need_load_wsi'), 'warning');
            releaseLaunchLock();
            return false;
        }
        if (
            window.pathchat
            && typeof window.pathchat.ensureSpecimenSiteDeclared === 'function'
            && !(await window.pathchat.ensureSpecimenSiteDeclared({ required: true }))
        ) {
            releaseLaunchLock();
            return false;
        }
        if (options?.expectedFileId && String(fileId) !== String(options.expectedFileId)) {
            this.showNotification(
                (window.__lang || 'zh') === 'en'
                    ? 'The open slide changed. Please ask the question again.'
                    : '当前切片已切换，请针对新切片重新提问。',
                'warning'
            );
            releaseLaunchLock();
            return false;
        }

        const strategyChoice = options?.analysisStrategy
            ? {
                strategy: options.analysisStrategy,
                mode: options.analysisMode || 'ai',
                reviewPolicy: options.collaborationReviewPolicy || 'staged',
                swarmConsentId: options.swarmConsentId || '',
                swarmConsentGranted: options.swarmConsentGranted === true
            }
            : null;
        if (!strategyChoice || options?.userSelectedRoute !== true) {
            this.showNotification(
                (window.__lang || 'zh') === 'en'
                    ? 'Choose Agent Swarm or Single Agent on the chat cards before analysis can start.'
                    : '请先在聊天卡片上选择 Agent Swarm 或单 Agent，分析才会开始。',
                'warning'
            );
            releaseLaunchLock();
            return false;
        }
        const analysisStrategy = typeof strategyChoice === 'string'
            ? strategyChoice
            : strategyChoice.strategy;
        const analysisMode = analysisStrategy === 'single_agent_roam'
            ? 'ai'
            : (strategyChoice.mode || this.currentMode || 'ai');
        if (!['agent_swarm', 'single_agent_roam'].includes(analysisStrategy)) {
            this.showNotification(
                (window.__lang || 'zh') === 'en'
                    ? 'Unknown analysis strategy'
                    : '未知的分析策略',
                'error'
            );
            releaseLaunchLock();
            return false;
        }
        if (!['ai', 'collaborative'].includes(analysisMode)) {
            this.showNotification(
                (window.__lang || 'zh') === 'en' ? 'Unknown Swarm mode' : '未知的 Swarm 模式',
                'error'
            );
            releaseLaunchLock();
            return false;
        }
        this.currentMode = analysisMode;
        this.isCollaborativeMode = analysisMode === 'collaborative';
        this.hideDraftReportModal({ all: true });
        this.activeAnalysisStrategy = analysisStrategy;
        this._setEvidencePaneMode(analysisStrategy);
        this.collaborationReviewPolicy = analysisMode === 'collaborative'
            ? (strategyChoice.reviewPolicy === 'final' ? 'final' : 'staged')
            : null;

        // A new Run owns a fresh review queue even when the same slide remains
        // open. Historical chat stays visible, but its ROI actions are retired.
        const reviewFileKey = String(fileId || '');
        this._releasePendingPhaseConfirmations('superseded');
        this._roamSnapshotEpoch += 1;
        this._collaborationSnapshotEpoch += 1;
        this._roamMaxStepByRun.clear();
        this._clearCollaborativeReviewHighlights();
        this.collaborativeReviewQueue = [];
        if (reviewFileKey) this._reviewQueuesByFile.delete(reviewFileKey);
        document.querySelectorAll('.review-queue-actions').forEach((actions) => {
            if (!reviewFileKey || actions.dataset.reviewFileId === reviewFileKey) {
                actions.querySelectorAll('button').forEach((button) => { button.disabled = true; });
                actions.classList.add('is-retired');
                actions.setAttribute('aria-label', (window.__lang || 'zh') === 'en'
                    ? 'Previous run review queue'
                    : '上一轮分析的审阅清单');
            }
        });

        // A new analysis run owns a fresh visualization layer.  In particular,
        // do not leave ROAM-* boxes from a completed single-Agent run visible
        // underneath a subsequent Swarm/collaborative run (or vice versa).
        // Reconnect/restore does not call this branch; it keeps its saved ROIs.
        if (window.pathchat && typeof window.pathchat.clearAllROIOverlays === 'function') {
            window.pathchat.clearAllROIOverlays();
        }
        this.currentOverlays = [];

        this.thumbnailThinkingMessage = options?.thinkingMessage || null;
        this.thumbnailToolMessage = options?.toolMessage || null;
        if (!this.thumbnailToolMessage && window.pathchat?.addToolActivity) {
            this.thumbnailToolMessage = window.pathchat.addToolActivity(
                'Python · OpenSlide',
                (window.__lang || 'zh') === 'en' ? 'Reading the WSI thumbnail' : '读取 WSI 缩略图',
                'running'
            );
        }
        if (!this.thumbnailThinkingMessage && window.pathchat?.addMessage) {
            this.thumbnailThinkingMessage = window.pathchat.addMessage(
                'assistant',
                (window.__lang || 'zh') === 'en' ? 'Thinking' : '正在思考',
                true
            );
        }
        this._startChatActivity();
        this.activeDiagnosticQuestion = normalizedQuestion;
        if (normalizedQuestion) {
            const isEn = (window.__lang || 'zh') === 'en';
            this._appendActivityItem({
                title: isEn ? 'Whole-slide question received' : '已接收全切片问题',
                detail: normalizedQuestion,
                state: 'done',
                key: `diagnostic-question:${normalizedQuestion}`
            });
            const isRoam = analysisStrategy === 'single_agent_roam';
            this._appendActivityItem({
                title: isRoam
                    ? (isEn ? 'Single Agent is reading the slide overview' : '单 Agent 正在读取切片概览')
                    : (isEn ? 'Orchestrator is reading the slide thumbnail' : 'Orchestrator 正在读取切片缩略图'),
                detail: isRoam
                    ? (isEn
                        ? 'One Agent will navigate one field at a time; no ROI Subagents will be created.'
                        : '一个 Agent 将逐视野自主游走，不会创建 ROI Subagent。')
                    : (isEn
                        ? 'It will report the whole-slide observation, then dispatch ROI Sub-agents only where further evidence is needed.'
                        : '将先给出全局观察，再按需要派发 ROI Subagent 下钻取证。'),
                state: 'running',
                key: `${isRoam ? 'single-roam' : 'orchestrator-thumbnail'}:${this.sessionId}`
            });
        }
        if (!normalizedQuestion) {
            const isEn = (window.__lang || 'zh') === 'en';
            const isRoam = analysisStrategy === 'single_agent_roam';
            this._appendActivityItem({
                title: isRoam
                    ? (isEn ? 'Single Agent is reading the slide overview' : '单 Agent 正在读取切片概览')
                    : (isEn ? 'Orchestrator is reading the slide thumbnail' : 'Orchestrator 正在读取切片缩略图'),
                detail: isRoam
                    ? (isEn ? 'No Subagents will be created.' : '本路线不会创建 Subagent。')
                    : (isEn ? 'ROI Subagents are dispatched only when needed.' : '仅在需要时派发 ROI Subagent。'),
                state: 'running',
                key: `${isRoam ? 'single-roam' : 'orchestrator-thumbnail'}:${this.sessionId}`
            });
        }
        
        // Reuse the same request ID after an uncertain network failure. The
        // Runtime API is idempotent, so a retry cannot create or charge a
        // second analysis. A different slide always receives a fresh ID.
        if (
            !this.runtimeRequestId
            || this.runtimeRequestFileId !== String(fileId)
        ) {
            this.runtimeRequestId = (
                globalThis.crypto?.randomUUID?.()
                || `web-${Date.now()}-${Math.random().toString(16).slice(2)}`
            );
            this.runtimeRequestFileId = String(fileId);
        }
        this._persistRuntimeState();
        const requestData = {
            file_id: fileId,
            session_id: this.sessionId,
            request_id: this.runtimeRequestId,
            max_steps: 12,
            strategy: 'default',
            mode: analysisStrategy === 'single_agent_roam' ? 'ai' : this.currentMode,
            analysis_strategy: analysisStrategy,
            collaboration_review_policy: this.collaborationReviewPolicy,
            swarm_consent_id: strategyChoice.swarmConsentId,
            swarm_consent_granted: strategyChoice.swarmConsentGranted,
            lang: window.__lang || 'zh',  // ⭐ 语言设置
            diagnostic_question: normalizedQuestion
        };
        this.runtimeRequestPayload = { ...requestData };
        this._persistRuntimeState();
        
        console.log('发送Agent启动请求:', requestData);
        
        fetch('/api/agent/run', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(requestData)
        })
        .then(async (response) => {
            const data = await response.json();
            data.__httpStatus = response.status;
            return data;
        })
        .then(data => {
            console.log('Agent启动响应:', data);
            if (data.success) {
                const acceptedRunId = (
                    data.run_id || data.runtime?.run_id || null
                );
                const sameRun = Boolean(
                    acceptedRunId
                    && this.runtimeRunId
                    && acceptedRunId === this.runtimeRunId
                );
                this.runtimeRunId = acceptedRunId;
                if (acceptedRunId) this._retiredRuntimeRunIds.delete(String(acceptedRunId));
                this.runtimeRunFileId = fileId || this.runtimeRequestFileId || this.runtimeRunFileId;
                // A fast Socket event can arrive before this HTTP response.
                // Preserve its acknowledged cursor when both refer to the same
                // durable Run instead of replaying/overwriting live progress.
                if (!sameRun) {
                    this.runtimeEventCursor = 0;
                    this.runtimeReplayEventIds.clear();
                }
                this._persistRuntimeState();
                this._flushPendingRuntimeSocketEvents(acceptedRunId);
                this.isRunning = true;
                window.pathchat?._invalidateHistoryChooser?.('agent_started');
                if (!sameRun) this.clearTimeline();
                this.showNotification((window.__lang||'zh')==='en' ? 'Agent started' : 'Agent启动成功', 'success');

                // 清除旧的树状图缓存，防止新分析期间显示旧结果
                if (window.pathchat) {
                    window.pathchat._treeLoadedForId = null;
                    window.pathchat.analysisTreeData = null;
                }
                // Reconcile immediately in case the durable task completed
                // between Socket delivery and the submit response.
                this.syncRuntimeAfterReconnect();
            } else if (data.no_api_key) {
                this._pendingRuntimeSocketEvents.length = 0;
                this._clearPendingRuntimeRequest();
                this._finishChatActivity('error', data.error);
                window.PathCopilotApiKeyGuard?.markConfigured(false);
                window.PathCopilotApiKeyGuard?.requireNow();
            } else {
                this._pendingRuntimeSocketEvents.length = 0;
                // A 5xx response can be returned after the server committed
                // the idempotent submission. Keep the request ID so a retry
                // asks for the same Run instead of creating a duplicate.
                if ((Number(data.__httpStatus) || 0) < 500) {
                    this._clearPendingRuntimeRequest();
                }
                this._finishChatActivity('error', data.error);
                this.showNotification(((window.__lang||'zh')==='en' ? 'Agent failed: ' : 'Agent启动失败: ') + data.error, 'error');
            }
        })
        .catch(error => {
            this._pendingRuntimeSocketEvents.length = 0;
            console.error('Agent start failed:', error);
            this._finishChatActivity('error', error.message);
            this.showNotification((window.__lang||'zh')==='en' ? 'Agent start failed' : 'Agent启动失败', 'error');
        })
        .finally(() => {
            this.isStarting = false;
            this.updateButtons();
        });
        return true;
    }

    async _settleRuntimeAfterLegacySignal(runId) {
        if (!runId) return;
        try {
            const runtime = await this._waitForRuntimeTerminal(runId, {
                timeoutMs: 30000,
                intervalMs: 200
            });
            if (
                runtime
                && this._isRuntimeTerminalState(runtime.run_state)
                && this.runtimeRunId === runId
            ) {
                this.isRunning = false;
                this.isStarting = false;
                this.isStopping = false;
                this.updateButtons();
                this._clearRuntimeRun();
            }
        } catch (error) {
            // Keep the durable identity so reconnect/status replay can finish
            // reconciliation rather than losing a still-settling Run.
            console.debug('Runtime terminal settlement pending:', error);
            setTimeout(() => this.syncRuntimeAfterReconnect(), 1000);
        }
    }

    async _waitForRuntimeTerminal(runId, {
        timeoutMs = 120000,
        intervalMs = 500
    } = {}) {
        if (!runId) return null;
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            const response = await fetch(
                `/api/agent/runtime/${encodeURIComponent(runId)}/status`,
                { cache: 'no-store' }
            );
            if (response.status === 404) return null;
            if (!response.ok) {
                throw new Error(`Runtime status HTTP ${response.status}`);
            }
            const payload = await response.json();
            const runtime = payload.runtime || {};
            if (this._isRuntimeTerminalState(runtime.run_state)) return runtime;
            await new Promise(resolve => setTimeout(resolve, intervalMs));
        }
        throw new Error(
            (window.__lang || 'zh') === 'en'
                ? 'Cancellation is still being finalized'
                : '取消请求仍在等待任务安全退出'
        );
    }

    _finishStoppedUi(stoppingSessionId) {
        this._cancelRoamViewportAnimation();
        this._clearRoamFollowResumeControl();
        this._pendingRoamTarget = null;
        this._clearCollaborativeReviewHighlights();
        this.isRunning = false;
        this.isStarting = false;
        this.isStopping = false;
        this.isFollowing = false;
        if (this.followTimeout) {
            clearTimeout(this.followTimeout);
            this.followTimeout = null;
        }

        document.querySelector('#routeStepsList .analyzing-card')?.remove();
        const progressDiv = document.getElementById('agentProgress');
        if (progressDiv) progressDiv.style.display = 'none';
        this.updateRouteProgress(100, __t('analysis_stopped'));
        this.updateButtons();
        this._finishChatActivity('stopped', __t('analysis_stopped'));
        this._clearPendingBlueAliases();
        // The durable Runtime has reached a terminal cancelled state. Reflect
        // that transition in the Workflow card as well, instead of leaving the
        // last in-flight snapshot visible as "N running".
        this.subagentStates.forEach((state, id) => {
            if (state !== 'done' && state !== 'error') {
                this.subagentStates.set(id, 'stopped');
            }
        });
        if (this.primaryAgentState !== 'done' && this.primaryAgentState !== 'error') {
            this.primaryAgentState = 'stopped';
        }
        this._renderSubagentBoard();

        this.socket?.emit('leave', { room: stoppingSessionId });
        this._clearRuntimeRun();
        this.sessionId = this.generateSessionId();
        this._persistRuntimeState();
        this.joinRoom();
        this.showNotification(__t('analysis_stopped'), 'success');
    }

    stopAgent() {
        if (!this.isRunning || this.isStopping) return;

        const isEn = (window.__lang || 'zh') === 'en';
        const stoppingSessionId = this.sessionId;
        const progressMessage = document.querySelector('#routeProgressText .progress-message');
        const previousProgressText = progressMessage?.textContent || '';
        const previousProgressWidth = Number.parseFloat(
            document.getElementById('routeProgressFill')?.style.width
        ) || 0;

        this.isStopping = true;
        this._cancelRoamViewportAnimation();
        this._clearRoamFollowResumeControl();
        this._releasePendingPhaseConfirmations('stopping');
        this.updateButtons();
        const isRoam = this.activeAnalysisStrategy === 'single_agent_roam';
        this.updateRouteProgress(
            Math.max(1, previousProgressWidth),
            isRoam
                ? (isEn ? 'Finishing the current FOV…' : '正在结束当前视野…')
                : (isEn ? 'Stopping analysis…' : '正在停止分析…')
        );

        fetch('/api/agent/stop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: stoppingSessionId,
                run_id: this.runtimeRunId,
                file_id: this.currentFileId
            })
        })
        .then(async (response) => {
            const data = await response.json();
            if (!response.ok || !data.success) {
                throw new Error(data.error || `HTTP ${response.status}`);
            }
            const runtime = data.runtime || null;
            if (
                runtime
                && !this._isRuntimeTerminalState(runtime.run_state)
            ) {
                await this._waitForRuntimeTerminal(
                    data.run_id || this.runtimeRunId
                );
            }
            this._finishStoppedUi(stoppingSessionId);
        })
        .catch((error) => {
            console.error('Agent stop failed:', error);
            this.isStopping = false;
            this.updateRouteProgress(
                previousProgressWidth,
                previousProgressText || (isEn ? 'Analysis in progress…' : '分析进行中…')
            );
            this.updateButtons();
            this.showNotification(
                (isEn ? 'Could not stop analysis: ' : '停止分析失败：') + error.message,
                'error'
            );
        });
    }
    
    
    pauseFollow() {
        this.isFollowing = false;
        this._cancelRoamViewportAnimation();
        this._showRoamFollowResumeControl();
        this.showNotification((window.__lang||'zh')==='en' ? 'Follow paused' : '已暂停跟随', 'info');
    }
    
    resumeFollow() {
        if (this._activePatientRoamRunToken && this.currentFileId) {
            this._patientRoamPausedKeys.delete(
                `${this.currentFileId}:${this._activePatientRoamRunToken}`
            );
            this._persistPatientRoamPausedKeys();
        }
        this._patientStopLatch = false;
        this.isFollowing = true;
        this._clearRoamFollowResumeControl();
        const pending = this._pendingRoamTarget;
        if (pending) this._animateRoamViewport(pending.rect, pending.zoom);
        this.showNotification((window.__lang||'zh')==='en' ? 'Follow resumed' : '已恢复跟随', 'info');
    }
    
    temporarilyPauseFollow() {
        if (!this.isFollowing) return;
        console.log('用户手动操作，停止自动跟随，等待显式恢复');
        this.isFollowing = false;
        if (this._activePatientRoamRunToken && this.currentFileId) {
            this._patientRoamPausedKeys.add(
                `${this.currentFileId}:${this._activePatientRoamRunToken}`
            );
            this._persistPatientRoamPausedKeys();
        }
        this._cancelRoamViewportAnimation();
        this._showRoamFollowResumeControl();
        this.showNotification(
            (window.__lang || 'zh') === 'en'
                ? 'Automatic viewport following paused'
                : '已暂停自动视野跟随',
            'info'
        );
    }

    _clearRoamFollowResumeControl() {
        this._roamFollowResumeCard?.remove?.();
        this._roamFollowResumeCard = null;
    }

    _showRoamFollowResumeControl() {
        if (this._patientStopLatch) return;
        const patientRoamRunning = Boolean(
            window.pathchat?.patientIsRunning
            && this._activePatientRoamRunToken
            && this.currentFileId
        );
        if (
            this.activeAnalysisStrategy !== 'single_agent_roam'
            || (!this.isRunning && !patientRoamRunning)
        ) return;
        if (this._roamFollowResumeCard?.isConnected) return;
        const isEn = (window.__lang || 'zh') === 'en';
        const card = window.pathchat?.addAgentStatus?.(
            isEn ? 'Viewport follow paused' : '视野跟随已暂停',
            isEn ? 'You are controlling the slide.' : '当前由您控制切片视野。',
            'stopped'
        );
        if (!card) return;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'analysis-consent-btn roam-resume-follow-btn';
        button.textContent = isEn ? 'Resume automatic follow' : '恢复自动跟随';
        button.addEventListener('click', () => this.resumeFollow(), { once: true });
        card.querySelector('.message-content')?.appendChild(button);
        this._roamFollowResumeCard = card;
    }

    pausePatientRoamForStop() {
        this._patientStopLatch = true;
        this.isFollowing = false;
        this._cancelRoamViewportAnimation();
        this._clearRoamFollowResumeControl();
    }

    beginPatientLevelRun(patientId, runToken = '') {
        const normalizedPatientId = String(patientId || '');
        this.isPatientLevelMode = true;
        this._serialReviewHoldAnnounced = false;
        this._activePatientRunToken = String(runToken || '') || null;
        this._patientStopLatch = false;
        this._clearRoamFollowResumeControl();
        for (const key of Array.from(this._patientTerminalUiKeys)) {
            if (key.startsWith(`stopped:${normalizedPatientId}:`)) {
                this._patientTerminalUiKeys.delete(key);
            }
        }
        if (this._activePatientTrajectoryOwner !== normalizedPatientId) {
            this._patientRoamByFile.clear();
            this._patientRoamEventKeys.clear();
            this._patientRoamDisplayedKeys.clear();
        }
        this._activePatientTrajectoryOwner = normalizedPatientId;
        this._patientWorkspaceHtmlRestored = false;
        this._patientActivitySyncedFiles = new Set();
        const viewer = this.viewer || window.pathchat?.viewer;
        try { viewer?.clearOverlays?.(); } catch (_) {}
        this.subagentStates.clear();
        this.subagentExpectedCount = 0;
        this._renderSubagentBoard();
        const routeList = document.getElementById('routeStepsList');
        if (routeList) routeList.innerHTML = '';
        if (this.activityTimer) {
            clearInterval(this.activityTimer);
            this.activityTimer = null;
        }
        this.activityStartedAt = null;
        this.subagentUiActivated = false;
        this.pendingActivityItems = [];
        this.pendingActivityUpdate = null;
        this.activityDetailKeys = new Set();
        this.activityImageKeys = new Set();
        this.activityOverviewImageShown = false;
        this.activityLastKey = '';
        this._setEvidencePaneMode(this.activeAnalysisStrategy);
        this._startChatActivity();
        this._activateSubagentUi();
        // Patient Level workers do not share a single-WSI Runtime run id.
        // A leftover id would stamp approval cards and then be rejected as
        // stale_runtime_run against empty worker state.
        this._clearRuntimeRun();
        if (this.activeAnalysisStrategy === 'single_agent_roam') {
            this.isFollowing = true;
            this._clearRoamFollowResumeControl();
        }
        window.pathchat?.resetPatientSlideNavStates?.();
        window.pathchat?.markPatientSlidesForRunStart?.();
        this._keepPatientSlideMapOnViewing();
    }

    bindPatientLevelRun(patientId, runToken = '') {
        const normalizedPatientId = String(patientId || '');
        const normalizedRunToken = String(runToken || '');
        if (!normalizedPatientId || !normalizedRunToken) return;
        this._activePatientTrajectoryOwner = normalizedPatientId;
        this._activePatientRunToken = normalizedRunToken;
    }

    restorePatientRoamAfterStopFailure() {
        this._patientStopLatch = false;
        this.isFollowing = false;
        this._showRoamFollowResumeControl();
    }
    
    updateButtons() {
        const btnRun = document.getElementById('btnAgentRun');
        if (!btnRun) return;

        const isEn = (window.__lang || 'zh') === 'en';
        const draftIdle = this._isCollaborativeDraftReviewActive();
        const patientRunning = Boolean(window.pathchat?.patientIsRunning);
        const patientStopping = Boolean(window.pathchat?.patientStopRequested);
        const showStop = (this.isRunning || patientRunning) && !draftIdle;
        btnRun.classList.toggle('is-stop-action', showStop);
        btnRun.classList.toggle('is-stopping', this.isStopping || patientStopping);
        btnRun.disabled = this.isStarting || this.isStopping || patientStopping;

        if (this.isStarting) {
            const label = isEn ? 'Starting analysis…' : '正在启动分析…';
            btnRun.innerHTML = '<i class="fas fa-spinner fa-spin" aria-hidden="true"></i>';
            btnRun.title = label;
            btnRun.setAttribute('aria-label', label);
        } else if (this.isStopping) {
            const isRoam = this.activeAnalysisStrategy === 'single_agent_roam';
            const label = isRoam
                ? (isEn ? 'Finishing the current FOV…' : '正在结束当前视野…')
                : (isEn ? 'Stopping analysis…' : '正在停止分析…');
            btnRun.innerHTML = '<i class="fas fa-spinner fa-spin" aria-hidden="true"></i>';
            btnRun.title = label;
            btnRun.setAttribute('aria-label', label);
        } else if (showStop) {
            const label = patientRunning
                ? (isEn ? 'Stop patient analysis' : '停止患者分析')
                : (isEn ? 'Stop analysis' : '停止分析');
            btnRun.innerHTML = '<i class="fas fa-stop" aria-hidden="true"></i>';
            btnRun.title = label;
            btnRun.setAttribute('aria-label', label);
        } else {
            const label = isEn ? 'Run Agent analysis' : '启动 Agent 分析';
            btnRun.innerHTML = '<i class="fas fa-play" aria-hidden="true"></i>';
            btnRun.title = label;
            btnRun.setAttribute('aria-label', label);
        }
    }

    _setEvidencePaneMode(strategy) {
        const isRoam = strategy === 'single_agent_roam';
        const isEn = (window.__lang || 'zh') === 'en';
        const heading = document.querySelector('#v2SubagentResultsHeading strong');
        const pane = document.getElementById('v2SubagentPane');
        const toggle = document.getElementById('v2SubagentToggle');
        const title = isRoam
            ? (isEn ? 'Single Agent progress' : '单 Agent 进度')
            : (isEn ? 'Subagent results' : 'Subagent 结果');
        if (heading) heading.textContent = title;
        if (pane) pane.setAttribute('aria-label', title);
        if (toggle) {
            toggle.title = isRoam
                ? (isEn ? 'Show Single Agent progress' : '显示单 Agent 进度')
                : (isEn ? 'Show Subagent results' : '显示 Subagent 结果');
            toggle.setAttribute('aria-label', toggle.title);
        }
        const rootTitle = document.querySelector('#v2AgentActivity .v2-root-agent-title');
        if (rootTitle) {
            rootTitle.textContent = isRoam
                ? (isEn ? 'Single Agent navigation' : '单 Agent 自主游走')
                : (isEn ? 'Whole-slide thumbnail analysis' : '全片缩略图分析');
        }
    }

    _findExistingChatActivity(preferredIndex = null) {
        // The write target comes first: viewing is where the doctor looks, and
        // using it would file another slide's steps under the viewed slide.
        const want = this._asPatientWsiIndex(preferredIndex)
            ?? (this._isPatientActivityMapMode() ? this._patientMapWriteIndex() : null)
            ?? this._asPatientWsiIndex(this.currentViewingWsiIndex)
            ?? this._asPatientWsiIndex(this.currentAnalyzingWsiIndex);
        if (Number.isInteger(want)) {
            const named = document.getElementById(`v2AgentActivity-wsi-${want}`);
            if (named) return named;
            const inMap = document.querySelector(
                `#routeStepsList > .patient-slide-map[data-wsi-index="${want}"] .v2-subagent-stream`
            );
            if (inMap) return inMap;
            // Patient level owns one node per slide. Falling through to any
            // stream would hand slide 2 the node slide 1 already renamed to
            // v2AgentActivity-wsi-0, so slide 2 would never get its own.
            if (this.isPatientLevelMode) {
                return document.getElementById('v2AgentActivity');
            }
        }
        return document.getElementById('v2AgentActivity')
            || document.querySelector('#routeStepsList .v2-subagent-stream');
    }

    _collapseDuplicateChatActivities(keep) {
        if (!keep) return keep;
        const map = keep.closest('.patient-slide-map');
        const extras = map
            ? map.querySelectorAll('.v2-subagent-stream')
            : document.querySelectorAll('#routeStepsList > .v2-subagent-stream');
        extras.forEach((el) => {
            if (el !== keep) el.remove();
        });
        return keep;
    }

    _ensureChatActivity() {
        const reused = this._findExistingChatActivity();
        if (reused) return this._collapseDuplicateChatActivities(reused);
        if (this._isPatientActivityMapMode()) {
            const index = this._patientMapWriteIndex();
            if (Number.isInteger(index)) {
                this._adoptGlobalActivityIntoSlideMap(index);
                const map = this._ensurePatientSlideMap(index);
                const existing = map?.querySelector('.v2-subagent-stream');
                if (existing) return this._collapseDuplicateChatActivities(existing);
            } else if (this._isPatientParallelRun()) {
                // Parallel must not create a shared stream; the last
                // wsi_started would otherwise own every other slide's map.
                return null;
            }
        }

        // Visual evidence and execution details belong in the Subagent pane,
        // not inside the doctor/Agent conversation.  Keep the right side as a
        // clean chat surface and use the adjacent pane as the durable stream.
        const writeIndex = this._isPatientActivityMapMode() ? this._patientMapWriteIndex() : null;
        if (this._isPatientParallelRun() && !Number.isInteger(writeIndex)) return null;
        const resultsList = Number.isInteger(writeIndex)
            ? this._ensurePatientSlideMap(writeIndex)
            : document.getElementById('routeStepsList');
        if (!resultsList) return null;
        resultsList.querySelector('.v2-subagent-empty')?.remove();

        const activity = document.createElement('section');
        activity.id = Number.isInteger(writeIndex)
            ? `v2AgentActivity-wsi-${writeIndex}`
            : 'v2AgentActivity';
        activity.className = `v2-subagent-stream v2-root-agent-node ${this.thumbnailRootCompleted ? 'is-completed' : 'is-running'}`;
        const isEn = (window.__lang || 'zh') === 'en';
        const isRoam = this.activeAnalysisStrategy === 'single_agent_roam';
        const rootStatus = isRoam
            ? (isEn ? 'Opening the slide and choosing the first FOV…' : '正在打开切片并根据概览选择第一个视野…')
            : (this.thumbnailRootCompleted
                ? (isEn ? 'Thumbnail interpretation complete' : '缩略图解读完成')
                : (isEn ? 'Reading the whole-slide thumbnail…' : '正在读取全片缩略图…'));
        const rootPercentage = isRoam ? 2 : (this.thumbnailRootCompleted ? 100 : 0);
        activity.innerHTML = `
            <button class="v2-root-agent-header" type="button" aria-expanded="true">
                <span class="v2-root-agent-toggle" aria-hidden="true"><i class="fas fa-chevron-down"></i></span>
                <span class="v2-root-agent-title">${isRoam
                    ? (isEn ? 'Single Agent navigation' : '单 Agent 自主游走')
                    : (isEn ? 'Whole-slide thumbnail analysis' : '全片缩略图分析')}</span>
                <span class="v2-agent-elapsed">00:00</span>
            </button>
            <div class="v2-root-agent-body">
                <div class="v2-agent-current">
                    <span class="v2-agent-current-dot" aria-hidden="true"></span>
                    <span class="v2-agent-current-text">${rootStatus}</span>
                    <span class="v2-agent-current-percent">${rootPercentage}%</span>
                </div>
                <div class="v2-agent-progress-track" aria-hidden="true">
                    <span class="v2-agent-progress-fill" style="width:${rootPercentage}%"></span>
                </div>
                <div class="v2-agent-activity-list" aria-live="polite"></div>
            </div>
        `;
        const header = activity.querySelector('.v2-root-agent-header');
        header?.addEventListener('click', () => {
            const collapsed = activity.classList.toggle('is-collapsed');
            header.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        });
        const label = resultsList.querySelector(':scope > .patient-slide-map-label');
        if (label) resultsList.insertBefore(activity, label.nextSibling);
        else resultsList.prepend(activity);
        this._keepPatientSlideMapOnViewing();
        return activity;
    }

    _startChatActivity() {
        document.querySelector('#chatMessages .welcome-message')?.remove();
        document.getElementById('v2AgentActivity')?.remove();
        document.querySelectorAll('#routeStepsList > .patient-slide-map, [id^="v2AgentActivity-wsi-"]').forEach((el) => el.remove());
        document.getElementById('v2SubagentBoard')?.remove();
        const routePanel = document.getElementById('analysisRoutePanel');
        if (routePanel) routePanel.style.display = 'none';
        window.PathCopilotV2?.setSubagentPaneOpen?.(false);
        if (this.activityTimer) clearInterval(this.activityTimer);
        this.activityStartedAt = Date.now();
        this.activityLastKey = '';
        this.activityDetailKeys = new Set();
        this.activityImageKeys = new Set();
        this.activityOverviewImageShown = false;
        this.subagentStates = new Map();
        this.pendingBlueAgentAliases = new Map();
        this.subagentExpectedCount = 0;
        this.primaryAgentState = 'running';
        this.thumbnailPlanAnnounced = false;
        this.thumbnailRootCompleted = false;
        this._thumbnailChatMessage = null;
        this._thumbnailAnnouncedKeys = new Set();
        this._thumbnailAnnounceFileId = '';
        this._patientParallelBriefed = false;
        this.chatSubagentStatusKeys = new Set();
        this._redResultReceivedIds = new Set();
        this._swarmRedTotal = 0;
        this.subagentUiActivated = false;
        this.pendingActivityItems = [];
        this.pendingActivityUpdate = null;
        this.activityTimer = setInterval(() => this._updateActivityElapsed(), 1000);
    }

    _activateSubagentUi() {
        if (this.subagentUiActivated) return;
        this.subagentUiActivated = true;
        this.enterAnalysisLayout(this.currentMode);

        const activity = this._ensureChatActivity();
        if (!activity) return;

        const queuedUpdate = this.pendingActivityUpdate;
        const queuedItems = this.pendingActivityItems.slice();
        this.pendingActivityUpdate = null;
        this.pendingActivityItems = [];
        queuedItems.forEach(item => this._appendActivityItem(item));
        if (queuedUpdate) this._updateChatActivity(queuedUpdate);
        this._updateActivityElapsed();
    }

    _ensureSubagentBoard() {
        let board = document.getElementById('v2SubagentBoard');
        if (board) return board;
        const chatMessages = document.getElementById('chatMessages');
        if (!chatMessages) return null;

        board = document.createElement('section');
        board.id = 'v2SubagentBoard';
        board.className = 'v2-subagent-board is-running';
        board.innerHTML = `
            <div class="v2-subagent-board-head">
                <strong>Workflow</strong>
                <span class="v2-subagent-count">0 agents</span>
                <span class="v2-subagent-board-elapsed">00:00</span>
            </div>
            <div class="v2-subagent-board-summary"></div>
            <div class="v2-subagent-grid" aria-label="Subagent status"></div>
        `;
        const pin = window.pathchat?._ensurePatientChatPin?.();
        if (pin) {
            const nav = document.getElementById('patientSlideReportNav');
            if (board.parentElement !== pin) {
                if (nav && nav.nextSibling !== board) {
                    pin.insertBefore(board, nav.nextSibling);
                } else if (!nav) {
                    pin.appendChild(board);
                } else {
                    pin.appendChild(board);
                }
            }
            return board;
        }
        chatMessages.prepend(board);
        return board;
    }

    _configureSubagentBoard(ids = [], expectedCount = 0, initialState = 'running', options = {}) {
        const normalizedIds = Array.from(new Set((ids || []).map(id => String(id || '').trim()).filter(Boolean)));
        const authoritativeLevel = String(options?.authoritativeLevel || '');
        const levelPatterns = {
            red: /^R\d+$/,
            blue: /^R\d+-B\d+$/,
            green: /^R\d+-B\d+-G\d+$/,
        };
        const authoritativePattern = levelPatterns[authoritativeLevel] || null;
        if (normalizedIds.length) {
            const nextStates = new Map();
            // IDs arrive when their API jobs have already been dispatched.  Keep
            // any existing terminal state, otherwise count them as running now.
            if (authoritativePattern) {
                this.subagentStates.forEach((state, id) => {
                    if (!authoritativePattern.test(id) && !/^S\d+$/.test(id)) {
                        nextStates.set(id, state);
                    }
                });
            }
            normalizedIds.forEach(id => nextStates.set(id, this.subagentStates.get(id) || initialState));
            this.subagentStates.forEach((state, id) => {
                if (
                    !nextStates.has(id)
                    && !/^S\d+$/.test(id)
                    && !authoritativePattern
                ) nextStates.set(id, state);
            });
            this.subagentStates = nextStates;
        } else if (!this.subagentStates.size && expectedCount > 0) {
            for (let index = 1; index <= expectedCount; index += 1) {
                this.subagentStates.set(`S${index}`, initialState);
            }
        }
        this.subagentExpectedCount = authoritativePattern
            ? this.subagentStates.size
            : Math.max(expectedCount || 0, this.subagentStates.size);
        this._renderSubagentBoard();
    }

    _registerPendingBlueAliases(parentId, ids = []) {
        const normalizedParentId = String(parentId || '').trim();
        const aliases = Array.from(new Set(
            (ids || [])
                .map(id => String(id || '').trim())
                .filter(id => id && id.includes('_sub_'))
        ));
        if (!normalizedParentId || !aliases.length) return;

        this.pendingBlueAgentAliases.set(normalizedParentId, aliases);
        let reconciled = false;
        aliases.forEach((aliasId, index) => {
            const stableId = `${normalizedParentId}-B${index + 1}`;
            if (this.subagentStates.has(stableId)) {
                reconciled = this._consumePendingBlueAlias(
                    normalizedParentId,
                    index + 1,
                    stableId
                ) || reconciled;
            }
        });
        if (reconciled) {
            this.subagentExpectedCount = this.subagentStates.size;
            this._renderSubagentBoard();
        }
    }

    _consumePendingBlueAlias(parentId, blueIndex, stableId) {
        const normalizedParentId = String(parentId || '').trim();
        const normalizedStableId = String(stableId || '').trim();
        const normalizedIndex = Number.parseInt(blueIndex, 10);
        const aliases = this.pendingBlueAgentAliases.get(normalizedParentId);
        if (!aliases || !normalizedStableId || !Number.isInteger(normalizedIndex) || normalizedIndex < 1) {
            return false;
        }

        const aliasId = aliases[normalizedIndex - 1];
        if (!aliasId) return false;
        if (aliasId !== normalizedStableId) this.subagentStates.delete(aliasId);
        aliases[normalizedIndex - 1] = '';
        if (aliases.every(id => !id)) this.pendingBlueAgentAliases.delete(normalizedParentId);
        else this.pendingBlueAgentAliases.set(normalizedParentId, aliases);
        return true;
    }

    _clearPendingBlueAliases() {
        this.pendingBlueAgentAliases.forEach(aliases => {
            aliases.forEach(aliasId => {
                if (aliasId) this.subagentStates.delete(aliasId);
            });
        });
        this.pendingBlueAgentAliases.clear();
        this.subagentExpectedCount = this.subagentStates.size;
    }

    _setSubagentState(id, state = 'running', parentId = null, childIndex = null) {
        const normalizedId = String(id || '').trim();
        if (!normalizedId || normalizedId === 'AI-Red-Selection') return;
        const stableBlueMatch = normalizedId.match(/^(.*)-B(\d+)$/);
        const aliasParentId = String(parentId || stableBlueMatch?.[1] || '').trim();
        const aliasChildIndex = childIndex ?? stableBlueMatch?.[2];
        const reconciledAlias = this._consumePendingBlueAlias(
            aliasParentId,
            aliasChildIndex,
            normalizedId
        );
        // A delayed progress event must not move a completed job back to running.
        if (this.subagentStates.get(normalizedId) === 'done' && state !== 'done') return;
        this.subagentStates.set(normalizedId, state);
        this.subagentExpectedCount = reconciledAlias
            ? this.subagentStates.size
            : Math.max(this.subagentExpectedCount, this.subagentStates.size);
        this._renderSubagentBoard();
    }

    _setPendingSubagentsRunning() {
        this.subagentStates.forEach((state, id) => {
            if (state === 'pending') this.subagentStates.set(id, 'running');
        });
        this._renderSubagentBoard();
    }

    _renderSubagentBoard() {
        if (!this.subagentUiActivated) return;
        let board = document.getElementById('v2SubagentBoard');
        if (!this.subagentStates.size && !this.subagentExpectedCount) {
            if (board) {
                const countEl = board.querySelector('.v2-subagent-count');
                const summaryEl = board.querySelector('.v2-subagent-board-summary');
                const grid = board.querySelector('.v2-subagent-grid');
                if (countEl) countEl.textContent = '0 agents';
                if (summaryEl) summaryEl.textContent = '';
                if (grid) grid.replaceChildren();
                if (this.activeAnalysisStrategy === 'single_agent_roam') {
                    board.hidden = true;
                }
            }
            return;
        }
        board = board || this._ensureSubagentBoard();
        if (!board) return;
        board.hidden = false;

        const subagentEntries = Array.from(this.subagentStates.entries());
        const displayPrimary = this._displayPrimaryAgentState();
        const primaryEntries = displayPrimary !== 'idle'
            ? [['Primary Agent', displayPrimary]]
            : [];
        const entries = [...primaryEntries, ...subagentEntries];
        const total = Math.max(this.subagentExpectedCount, subagentEntries.length) + primaryEntries.length;
        const counts = entries.reduce((result, [, state]) => {
            result[state] = (result[state] || 0) + 1;
            return result;
        }, {});
        const isEn = (window.__lang || 'zh') === 'en';
        const countEl = board.querySelector('.v2-subagent-count');
        const summaryEl = board.querySelector('.v2-subagent-board-summary');
        const grid = board.querySelector('.v2-subagent-grid');
        if (countEl) countEl.textContent = `${total} agents`;
        if (summaryEl) {
            const running = counts.running || 0;
            const done = counts.done || 0;
            const pending = counts.pending || 0;
            const review = counts.review || 0;
            const stopped = counts.stopped || 0;
            const errors = counts.error || 0;
            const summaryParts = [];
            if (running > 0) summaryParts.push(isEn ? `${running} running` : `${running} 个运行中`);
            if (done > 0) summaryParts.push(isEn ? `${done} done` : `${done} 个已完成`);
            if (pending > 0) summaryParts.push(isEn ? `${pending} queued` : `${pending} 个等待中`);
            if (review > 0) summaryParts.push(isEn ? 'waiting for sign-off' : '待医生定稿');
            if (stopped > 0) summaryParts.push(isEn ? `${stopped} stopped` : `${stopped} 个已停止`);
            if (errors > 0) summaryParts.push(isEn ? `${errors} failed` : `${errors} 个失败`);
            summaryEl.textContent = summaryParts.join(' · ');
        }
        if (grid) {
            grid.replaceChildren();
            entries.forEach(([id, state]) => {
                const cell = document.createElement('span');
                cell.className = `v2-subagent-cell is-${state}`;
                if (id === 'Primary Agent') cell.classList.add('is-primary');
                cell.title = `${id}: ${state}`;
                cell.setAttribute('aria-label', `${id}: ${state}`);
                grid.appendChild(cell);
            });
        }

        const hasActiveJobs = entries.some(([, state]) => state === 'running' || state === 'pending');
        board.classList.toggle('is-completed', entries.length > 0 && entries.every(([, state]) => state === 'done'));
        board.classList.toggle('is-stopped', entries.some(([, state]) => state === 'stopped'));
        board.classList.toggle('is-running', hasActiveJobs);
        board.classList.toggle('is-review', !hasActiveJobs && entries.some(([, state]) => state === 'review'));
    }

    _isCollaborativeDraftReviewActive() {
        if (!(this.currentMode === 'collaborative' || this.isCollaborativeMode)) return false;
        if (this.isPatientLevelMode) {
            this._adoptPendingDraftReviewsFromDom();
            for (const review of this._draftReportReviews.values()) {
                if (
                    String(review.current || review.original || '').trim()
                    && this._isDraftCardPending(review.confirmMsg)
                ) return true;
            }
            return false;
        }
        return Boolean(this._draftReportReview
            && String(this._draftReportReview.current || this._draftReportReview.original || '').trim());
    }

    _displayPrimaryAgentState() {
        if (this._isCollaborativeDraftReviewActive()) return 'review';
        return this.primaryAgentState;
    }

    _thumbnailReasonSummary(reasoning = '', maxLength = 520) {
        let source = reasoning;
        if (source && typeof source === 'object') {
            source = source.reasoning || source.analysis || source.observation || source.summary || source.content || '';
            if (!source) {
                try {
                    source = JSON.stringify(reasoning);
                } catch (_) {
                    source = '';
                }
            }
        }
        let value = String(source || '').trim();
        value = value.replace(/【([^】]+)】/g, '[$1]');
        const reasonMatch = value.match(/\[Reason[^\]]*\]\s*([\s\S]*?)(?=\n\s*\[(?:Plan|Evidence|Action)[^\]]*\]|$)/i);
        if (reasonMatch?.[1]) value = reasonMatch[1];
        value = value
            .replace(/\[(?:Reason|Plan|Evidence|Action)[^\]]*\]/gi, ' ')
            .replace(/[*#`]/g, '')
            .replace(/\s+/g, ' ')
            .trim();
        const limit = Number(maxLength);
        if (Number.isFinite(limit) && limit > 0 && value.length > limit) {
            const clipped = value.slice(0, limit);
            const boundary = Math.max(clipped.lastIndexOf('。'), clipped.lastIndexOf('. '), clipped.lastIndexOf('；'));
            value = `${clipped.slice(0, boundary > 240 ? boundary + 1 : Math.max(limit - 20, 1)).trim()}…`;
        }
        return value.replace(/\s*-{3,}\s*$/g, '').trim();
    }

    _thumbnailContextFromEvent(data = {}) {
        const fileId = String(
            data.file_id
            || data.fileId
            || data.wsi_context?.file_id
            || this._thumbnailAnnounceFileId
            || ''
        ).trim();
        const rawIndex = data.wsi_index ?? data.wsiIndex;
        let wsiIndex = this._asPatientWsiIndex(rawIndex);
        if (wsiIndex == null) wsiIndex = this._indexForPatientFile(fileId);
        if (!Number.isInteger(wsiIndex)) wsiIndex = this.currentAnalyzingWsiIndex;
        return {
            fileId,
            part: String(data.part || data.wsi_context?.part_name || data.part_name || '').trim(),
            wsiIndex
        };
    }

    _thumbnailAnnounceKey(context = {}) {
        if (!this.isPatientLevelMode) return 'single';
        const fileId = String(
            context.fileId
            || context.file_id
            || this._thumbnailAnnounceFileId
            || ''
        ).trim();
        const rawIndex = context.wsiIndex ?? context.wsi_index ?? this.currentAnalyzingWsiIndex;
        const wsiIndex = this._asPatientWsiIndex(rawIndex) ?? '';
        return `patient:${this._activePatientRunToken || ''}:${fileId || `idx:${wsiIndex}`}`;
    }

    _isPatientParallelRun() {
        if (!this.isPatientLevelMode) return false;
        return String(
            window.pathchat?.patientWsiExecutionMode
            || this.patientWsiExecutionMode
            || ''
        ) === 'parallel';
    }

    _shouldPostPatientSlideDetailToChat() {
        return !this._isPatientParallelRun();
    }

    _patientSlideResultHeading(index, part, filename) {
        const isEn = (window.__lang || 'zh') === 'en';
        const n = Number.isInteger(Number(index)) ? Number(index) + 1 : null;
        const partClean = String(part || '').replace(/^Slide\s+\d+:\s*/i, '').trim();
        const name = String(filename || '').trim() || partClean
            || (n ? (isEn ? `Slide ${n}` : `切片 ${n}`) : '');
        if (isEn) {
            return n
                ? `Slide ${n} result received (${name}), as follows:`
                : `Slide result received (${name}), as follows:`;
        }
        return n
            ? `切片 ${n} 的结果已经收到（${name}），如下：`
            : `${name} 的结果已经收到，如下：`;
    }

    _patientSlideHtmlReportUrl(data = {}, result = {}) {
        return String(
            data.html_report_url
            || data.report_html
            || result.html_report_url
            || result.report_html
            || result.output_paths?.report_html
            || ''
        ).trim();
    }

    _patientSlideHtmlButton(url, label) {
        const href = String(url || '').trim();
        if (!href) return '';
        return `<a href="${href}" target="_blank" class="btn patient-slide-html-report" style="display:inline-block; margin-top:6px; padding:5px 14px; background:linear-gradient(135deg,#667eea,#764ba2); color:#fff; border-radius:16px; text-decoration:none; font-size:13px;">📄 ${label}</a>`;
    }

    async _ensurePatientSlideHtmlButton(anchorEl, fileId) {
        if (!anchorEl || anchorEl.querySelector('.patient-slide-html-report')) return;
        const fid = String(fileId || '').trim();
        if (!fid || !window.pathchat) return;
        const mode = window.pathchat._patientReportLookupMode?.() || 'ai';
        const patientDir = String(window.pathchat.currentPatientId || '');
        try {
            const payload = { file_ids: [fid], mode };
            if (patientDir) payload.patient_dir = patientDir;
            const resp = await fetch('/api/agent/find-reports', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const found = await resp.json();
            const report = found?.reports?.[fid];
            const url = typeof report === 'string' ? report : report?.url;
            if (!url || anchorEl.querySelector('.patient-slide-html-report')) return;
            const isEn = (window.__lang || 'zh') === 'en';
            const head = anchorEl.querySelector('.patient-slide-html-row')
                || anchorEl.querySelector('.wsi-analysis-msg.success')
                || anchorEl;
            head.insertAdjacentHTML(
                'beforeend',
                this._patientSlideHtmlButton(url, isEn ? 'View Full Report' : '查看完整报告')
            );
        } catch (error) {
            console.warn('[Patient Level] 查找切片 HTML 报告失败:', error);
        }
    }

    _keepPatientSlideMapOnViewing() {
        if (!this.isPatientLevelMode) return;
        if (Number.isInteger(this.currentViewingWsiIndex)) {
            this._showPatientSlideMap(this.currentViewingWsiIndex);
            return;
        }
        this._syncPatientSlideMapVisibility();
    }

    _announcePatientParallelBriefingOnce() {
        if (this._patientParallelBriefed) return;
        this._patientParallelBriefed = true;
        if (!window.pathchat?.addChatMessage) return;
        const isEn = (window.__lang || 'zh') === 'en';
        const infos = window.pathchat?.plAllFileInfos || window.pathchat?.patientLevelUploadResults || [];
        const names = (infos || []).map((info) => String(info?.part || '')
            .replace(/^Slide\s+\d+:\s*/i, '')
            .trim()).filter(Boolean);
        const count = Math.max(infos?.length || 0, names.length);
        const nameText = names.length ? (isEn ? ` (${names.join(', ')})` : `（${names.join('、')}）`) : '';
        const text = isEn
            ? `I will analyze ${count || 'these'} slides in parallel${nameText}. Thumbnail interpretations and Subagent details stay in the middle panel — click a slide on the left to review that slide.`
            : `我将并行分析 ${count || '多'} 张切片${nameText}。各切片的缩略图解读和 Subagent 细节只在中间栏展示，请点击左侧对应切片查看。`;
        window.pathchat.addChatMessage('assistant', `<div class="patient-parallel-brief">${text}</div>`);
    }

    _preparePatientSlideThumbnailAnnouncement(data = {}) {
        const fileId = String(data.file_id || data.fileId || '').trim();
        if (fileId) this._thumbnailAnnounceFileId = fileId;
        const serialRun = !data.parallel && !this._isPatientParallelRun();
        // Parallel later slides keep their own announce key; do not clobber
        // the in-flight first-slide thumbnail message or overview image.
        if (!serialRun && Number(data.index) > 0) {
            if (Number.isInteger(Number(data.index))) this._ensurePatientSlideMap(Number(data.index));
            this._keepPatientSlideMapOnViewing();
            return;
        }
        // Each Patient Level slide must write its own thumbnail interpretation.
        // Slide 1 sets thumbnailPlanAnnounced; without a reset, later slides
        // only show drill-down status lines and never the overview write-up.
        this.thumbnailPlanAnnounced = false;
        this.thumbnailRootCompleted = false;
        this.thumbnailThinkingMessage = null;
        this._thumbnailChatMessage = null;
        this._lastOverviewImageUrl = '';
        this.activityOverviewImageShown = false;
        this.chatSubagentStatusKeys = new Set();
        this.currentPhase = 0;
        this.currentPhaseGateId = '';
        this.primaryAgentState = 'running';

        if (Number.isInteger(Number(data.index))) {
            const started = Number(data.index);
            if (
                serialRun
                || !Number.isInteger(this.currentViewingWsiIndex)
                || this.currentViewingWsiIndex === started
            ) {
                this._showPatientSlideMap(started);
            } else {
                this._ensurePatientSlideMap(started);
                this._keepPatientSlideMapOnViewing();
            }
        }
        const activity = this._ensureChatActivity();
        if (activity) {
            activity.classList.remove('is-completed', 'is-stopped', 'is-error');
            activity.classList.add('is-running');
        }

        if (serialRun) {
            this.subagentStates = new Map();
            this.pendingBlueAgentAliases = new Map();
            this.subagentExpectedCount = 0;
            this._renderSubagentBoard();
        }
    }

    _announceThumbnailPlan(reasoning, ids = [], expectedCount = 0, imageUrl = '', context = {}) {
        if (!this._thumbnailAnnouncedKeys) this._thumbnailAnnouncedKeys = new Set();
        const announceKey = this._thumbnailAnnounceKey(context);
        if (this._thumbnailAnnouncedKeys.has(announceKey)) return false;
        if (!this.isPatientLevelMode && this.thumbnailPlanAnnounced) return false;
        const summary = this._thumbnailReasonSummary(reasoning, 0) || String(reasoning || '').replace(/\s+/g, ' ').trim();
        const normalizedIds = (ids || []).map(id => String(id || '').trim()).filter(Boolean);
        const count = Math.max(expectedCount || 0, normalizedIds.length);
        if (!summary && !count) return false;
        // 记录本轮第一层 Subagent 总数，供「结果已收到 (k/N)」计数使用。
        if (!this.isPatientLevelMode && count) this._swarmRedTotal = count;

        const isEn = (window.__lang || 'zh') === 'en';
        const idText = normalizedIds.length ? normalizedIds.join('、') : '';
        const part = String(
            context.part
            || context.part_name
            || this.plCollabCurrentWsiPart
            || ''
        ).replace(/^Slide\s+\d+:\s*/i, '').trim();
        const rawIndex = context.wsiIndex ?? context.wsi_index ?? this.currentAnalyzingWsiIndex;
        const wsiIndex = this._asPatientWsiIndex(rawIndex);
        const isLaterSlide = this.isPatientLevelMode && Number.isInteger(wsiIndex) && wsiIndex > 0;
        const slideHint = part
            || (Number.isInteger(wsiIndex)
                ? (isEn ? `Slide ${wsiIndex + 1}` : `切片 ${wsiIndex + 1}`)
                : '');
        let interpretation;
        if (summary && isLaterSlide) {
            interpretation = isEn
                ? `I will now continue with the next slide${slideHint ? ` (${slideHint})` : ''}. Thumbnail interpretation: ${summary}`
                : `接下来我将进行下一张切片${slideHint ? `（${slideHint}）` : ''}。缩略图的结果表明：${summary}`;
        } else if (summary) {
            interpretation = isEn ? `Thumbnail interpretation: ${summary}` : `缩略图解读：${summary}`;
        } else {
            interpretation = isEn ? 'Whole-slide thumbnail review is complete.' : '全片缩略图解读已完成。';
        }
        const dispatch = count
            ? (isEn
                ? `Based on the whole-slide overview, I will now start ${count} first-wave Subagents${idText ? ` (${idText})` : ''} for region-level analysis, then synthesize their evidence as the primary Agent.`
                : `基于全片概览，我将启动 ${count} 个第一层 Subagent${idText ? `（${idText}）` : ''}分别进行区域下钻分析，随后由主 Agent 汇总它们的证据并回答你的问题。`)
            : '';

        this.thumbnailRootCompleted = true;
        this._appendActivityItem({
            title: isEn ? 'Thumbnail interpretation' : '缩略图解读结果',
            detail: summary,
            imageUrl: imageUrl || this._lastOverviewImageUrl || '',
            state: 'done',
            key: `thumbnail-interpretation:${announceKey}:${summary}`,
            wsiIndex: Number.isInteger(wsiIndex) ? wsiIndex : null
        });
        this._keepPatientSlideMapOnViewing();

        if (!this._shouldPostPatientSlideDetailToChat()) {
            this._thumbnailAnnouncedKeys.add(announceKey);
            this.thumbnailPlanAnnounced = true;
            this.thumbnailThinkingMessage = null;
            return true;
        }

        if (this.thumbnailToolMessage?.isConnected && window.pathchat?.updateToolActivity) {
            window.pathchat.updateToolActivity(
                this.thumbnailToolMessage,
                'Python · OpenSlide',
                isEn ? 'Whole-slide thumbnail generated' : '已生成全片缩略图',
                'done'
            );
        }
        this.thumbnailToolMessage = null;
        let interpretationStream = null;
        let chatMessage = this.thumbnailThinkingMessage;
        if (chatMessage?.isConnected && window.pathchat?.updateMessageStream) {
            interpretationStream = window.pathchat.updateMessageStream(chatMessage, interpretation);
        } else if (chatMessage?.isConnected && window.pathchat?.updateMessage) {
            window.pathchat.updateMessage(chatMessage, interpretation);
        } else if (window.pathchat?.addStreamingMessage) {
            chatMessage = window.pathchat.addStreamingMessage('assistant', interpretation);
        } else {
            chatMessage = window.pathchat?.addMessage?.('assistant', interpretation, false) || null;
        }
        this._thumbnailChatMessage = chatMessage;
        this._attachChatOverviewThumb(chatMessage, imageUrl || this._lastOverviewImageUrl);
        this.thumbnailThinkingMessage = null;
        try {
            chatMessage?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
        } catch (_) {}
        const appendDispatch = () => {
            if (!dispatch) return;
            if (window.pathchat?.addStreamingMessage) {
                window.pathchat.addStreamingMessage('assistant', dispatch);
            } else {
                window.pathchat?.addMessage?.('assistant', dispatch, false);
            }
        };
        if (interpretationStream?.then) interpretationStream.then(appendDispatch);
        else appendDispatch();
        this._thumbnailAnnouncedKeys.add(announceKey);
        this.thumbnailPlanAnnounced = true;
        return true;
    }

    _attachChatOverviewThumb(messageEl, url) {
        if (!messageEl || !url) return;
        const content = messageEl.querySelector('.message-content');
        if (!content || content.querySelector('.chat-overview-thumb')) return;
        const img = document.createElement('img');
        img.className = 'chat-overview-thumb';
        img.src = url;
        img.alt = (window.__lang || 'zh') === 'en' ? 'Whole-slide thumbnail' : '全片缩略图';
        const time = content.querySelector('.message-time');
        if (time) content.insertBefore(img, time);
        else content.appendChild(img);
    }

    _updateActivityElapsed() {
        if (!this.activityStartedAt) return;
        const elapsed = Math.max(0, Math.floor((Date.now() - this.activityStartedAt) / 1000));
        const minutes = String(Math.floor(elapsed / 60)).padStart(2, '0');
        const seconds = String(elapsed % 60).padStart(2, '0');
        document.querySelectorAll('.v2-subagent-stream .v2-agent-elapsed').forEach((target) => {
            target.textContent = `${minutes}:${seconds}`;
        });
        const boardElapsed = document.querySelector('#v2SubagentBoard .v2-subagent-board-elapsed');
        if (boardElapsed) boardElapsed.textContent = `${minutes}:${seconds}`;
    }

    _appendActivityItem({ title, detail = '', state = 'running', imageUrl = '', key = '', roiId = '', rect = null, magnification = null, roamKind = '', meta = '', wsiIndex = null }) {
        if (!title) return null;
        if (!this.subagentUiActivated) {
            const queuedKey = key || `${state}:${title}:${detail}:${imageUrl}:${meta}`;
            if (!this.pendingActivityItems.some(item => (item.key || `${item.state}:${item.title}:${item.detail}:${item.imageUrl}:${item.meta || ''}`) === queuedKey)) {
                this.pendingActivityItems.push({ title, detail, state, imageUrl, key, roiId, rect, magnification, roamKind, meta, wsiIndex });
                if (this.pendingActivityItems.length > 48) this.pendingActivityItems.shift();
            }
            return null;
        }
        let resolvedWsiIndex = this._asPatientWsiIndex(wsiIndex);
        if (resolvedWsiIndex == null) {
            const fromImage = this._inferPatientWsiFromImageUrl(imageUrl);
            if (Number.isInteger(fromImage)) resolvedWsiIndex = fromImage;
        }
        const prevMapIndex = this._patientSwarmMapWsiIndex;
        if (this._isPatientActivityMapMode() && resolvedWsiIndex != null) {
            this._patientSwarmMapWsiIndex = resolvedWsiIndex;
        }
        let activity;
        try {
            activity = this._ensureChatActivity();
        } finally {
            this._patientSwarmMapWsiIndex = prevMapIndex;
        }
        const list = activity?.querySelector('.v2-agent-activity-list');
        if (!list) return null;

        const itemKey = key || `${state}:${title}:${detail}:${imageUrl}:${meta}`;
        if (this.activityDetailKeys.has(itemKey)) return null;
        this.activityDetailKeys.add(itemKey);

        // Swarm can emit both a viewport packet and a trace packet for the same
        // whole-slide preview.  Single-Agent roam keeps one overview per slide.
        if (
            imageUrl
            && !this.thumbnailPlanAnnounced
            && this.activeAnalysisStrategy !== 'single_agent_roam'
        ) {
            if (this.activityOverviewImageShown) imageUrl = '';
            else this.activityOverviewImageShown = true;
        }
        if (imageUrl) {
            let imageKey = String(imageUrl);
            try {
                const parsed = new URL(imageKey, window.location.href);
                imageKey = `${parsed.pathname}${parsed.search}`;
            } catch (_) {}
            if (this.activityImageKeys.has(imageKey)) imageUrl = '';
            else this.activityImageKeys.add(imageKey);
        }

        const item = document.createElement('article');
        item.className = `v2-agent-activity-item is-${state}`;
        if (resolvedWsiIndex != null) item.dataset.wsiIndex = String(resolvedWsiIndex);
        if (itemKey.startsWith('thumbnail-interpretation:')) item.classList.add('is-pinned');
        if (roiId) {
            item.dataset.roiId = roiId;
            if (roamKind) item.dataset.roamKind = roamKind;
            if (rect) {
                item.dataset.rect = typeof rect === 'string' ? rect : JSON.stringify(rect);
            }
            if (magnification != null && magnification !== '') {
                item.dataset.magnification = String(magnification);
            }
            item.setAttribute('role', 'button');
            item.tabIndex = 0;
            item.addEventListener('click', (e) => {
                e.stopPropagation();
                this.highlightLeftPanelCard(roiId);
                this._jumpToRoamOverlay(roiId, item);
            });
            item.addEventListener('keydown', (e) => {
                if (e.key !== 'Enter' && e.key !== ' ') return;
                e.preventDefault();
                e.stopPropagation();
                this.highlightLeftPanelCard(roiId);
                this._jumpToRoamOverlay(roiId, item);
            });
        }
        const rail = document.createElement('span');
        rail.className = 'v2-agent-activity-rail';
        const content = document.createElement('div');
        content.className = 'v2-agent-activity-content';
        const heading = document.createElement('strong');
        heading.textContent = title;
        content.appendChild(heading);

        if (detail) {
            const paragraph = document.createElement('p');
            paragraph.textContent = detail;
            content.appendChild(paragraph);
        }

        const visibleMeta = String(meta || '').trim();
        if (visibleMeta) {
            const metaEl = document.createElement('div');
            metaEl.className = 'v2-agent-activity-meta';
            metaEl.textContent = visibleMeta;
            content.appendChild(metaEl);
        }

        if (imageUrl) {
            const image = document.createElement('img');
            image.className = 'v2-agent-activity-thumbnail';
            image.src = imageUrl;
            image.alt = (window.__lang || 'zh') === 'en' ? 'Analysis evidence thumbnail' : '分析证据缩略图';
            image.loading = 'lazy';
            image.addEventListener('error', () => image.remove(), { once: true });
            content.appendChild(image);
        }

        item.append(rail, content);
        if (item.classList.contains('is-pinned')) {
            const firstUnpinned = Array.from(list.children).find(
                (child) => !child.classList.contains('is-pinned')
            );
            if (firstUnpinned) list.insertBefore(item, firstUnpinned);
            else list.appendChild(item);
        } else {
            list.appendChild(item);
        }
        const viewingIndex = Number(this.currentViewingWsiIndex);
        if (
            this.isPatientLevelMode
            && !this._isPatientActivityMapMode()
            && Number.isInteger(viewingIndex)
            && resolvedWsiIndex != null
        ) {
            // Only the single-list layout needs per-item hiding. With one map
            // per slide this would hide every step of the slides not on screen,
            // so opening them later showed an empty pane.
            item.hidden = resolvedWsiIndex !== viewingIndex;
        }
        if (this.isPatientLevelMode && resolvedWsiIndex != null) {
            const sameSlide = Array.from(list.children).filter(
                (child) => Number(child.dataset?.wsiIndex) === resolvedWsiIndex
            );
            while (sameSlide.length > 48) {
                const removable = sameSlide.find((child) => !child.classList.contains('is-pinned'));
                if (!removable) break;
                removable.remove();
                sameSlide.splice(sameSlide.indexOf(removable), 1);
            }
        } else {
            while (list.children.length > 48) {
                const removable = Array.from(list.children).find(child => !child.classList.contains('is-pinned'));
                if (!removable) break;
                removable.remove();
            }
        }
        const shouldFollowScroll = resolvedWsiIndex == null
            || !Number.isInteger(viewingIndex)
            || resolvedWsiIndex === viewingIndex;
        if (shouldFollowScroll && !item.hidden) {
            item.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
        this._scrubPatientSlideMaps();
        return item;
    }

    _patientActivityListEl(container = null) {
        if (container?.querySelector) {
            return container.querySelector('.v2-agent-activity-list') || (
                container.classList?.contains('v2-agent-activity-list') ? container : null
            );
        }
        return this._currentChatActivity()?.querySelector('.v2-agent-activity-list')
            || document.querySelector('#routeStepsList .v2-agent-activity-list')
            || document.querySelector('.v2-agent-activity-list');
    }

    _parsePatientActivityWsiIndex(el) {
        if (!el) return null;
        if (el.dataset?.wsiIndex != null && el.dataset.wsiIndex !== '') {
            const n = Number(el.dataset.wsiIndex);
            if (Number.isInteger(n)) return n;
        }
        const img = el.querySelector?.('img.v2-agent-activity-thumbnail, img');
        const src = String(img?.getAttribute?.('src') || img?.src || '');
        const fromPath = this._inferPatientWsiFromImageUrl(src);
        if (Number.isInteger(fromPath)) return fromPath;
        const thumb = src.match(/\/api\/thumbnail\/([^/?#]+)/i);
        if (thumb) {
            try {
                const idx = this._indexForPatientFile(decodeURIComponent(thumb[1]));
                if (Number.isInteger(idx)) return idx;
            } catch (_) {}
        }
        const roi = String(el.dataset?.roiId || '');
        const roiMatch = roi.match(/^PATIENT-ROAM-(\d+)-/i);
        if (roiMatch) return Number(roiMatch[1]);
        const title = el.querySelector?.('strong')?.textContent || '';
        const slideMatch = title.match(/切片\s*(\d+)/) || title.match(/Slide\s+(\d+)/i);
        if (slideMatch) {
            const display = Number(slideMatch[1]);
            if (Number.isInteger(display) && display >= 1) return display - 1;
        }
        return null;
    }

    _tagPatientActivityItems(container = null) {
        const list = this._patientActivityListEl(container) || container;
        if (!list?.querySelectorAll) return;
        list.querySelectorAll('.v2-agent-activity-item').forEach((el) => {
            if (el.dataset?.wsiIndex != null && el.dataset.wsiIndex !== '') return;
            const wsi = this._parsePatientActivityWsiIndex(el);
            if (Number.isInteger(wsi)) el.dataset.wsiIndex = String(wsi);
            else if (
                this._isPatientSwarmMapMode()
                && Number.isInteger(this.currentAnalyzingWsiIndex)
                && (el.dataset.wsiIndex == null || el.dataset.wsiIndex === '')
            ) {
                el.dataset.wsiIndex = String(this.currentAnalyzingWsiIndex);
            }
        });
    }

    _patientActivityCountForWsi(index) {
        const list = this._patientActivityListEl();
        if (!list) return 0;
        this._tagPatientActivityItems(list);
        const wsiIndex = Number(index);
        return Array.from(list.querySelectorAll('.v2-agent-activity-item'))
            .filter((el) => this._parsePatientActivityWsiIndex(el) === wsiIndex).length;
    }

    _filterPatientActivityToWsi(index) {
        const wsiIndex = Number(index);
        if (!Number.isInteger(wsiIndex)) return;
        if (this._isPatientActivityMapMode()) {
            // Roam's steps live in per-slide maps too, so hiding items inside
            // one list can no longer switch slides: swap the visible map.
            this._showPatientSlideMap(wsiIndex);
            return;
        }
        this._tagPatientActivityItems();
        const list = this._patientActivityListEl();
        if (!list) return;
        let firstMatch = null;
        list.querySelectorAll('.v2-agent-activity-item').forEach((el) => {
            const isMatch = this._parsePatientActivityWsiIndex(el) === wsiIndex;
            el.hidden = !isMatch;
            el.classList.toggle('is-focused-slide', isMatch);
            if (isMatch && !firstMatch) firstMatch = el;
        });
        firstMatch?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    _highlightPatientActivityForWsi(index) {
        const wsiIndex = Number(index);
        if (!Number.isInteger(wsiIndex)) return;
        const list = this._patientActivityListEl();
        if (!list) return;
        const items = Array.from(list.querySelectorAll('.v2-agent-activity-item[data-wsi-index]'));
        if (!items.length) return;
        let firstMatch = null;
        items.forEach((el) => {
            const isMatch = Number(el.dataset.wsiIndex) === wsiIndex;
            el.classList.toggle('is-focused-slide', isMatch);
            if (isMatch && !firstMatch) firstMatch = el;
        });
        firstMatch?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    _isPatientSwarmMapMode() {
        return Boolean(
            this.isPatientLevelMode
            && this.activeAnalysisStrategy !== 'single_agent_roam'
        );
    }

    _isPatientActivityMapMode() {
        // Which slide owns an activity node, not whether a Subagent tree is
        // drawn. Roam has no Subagents but still needs one node per slide,
        // because _showPatientSlideMap moves its node into the slide map.
        return Boolean(this.isPatientLevelMode);
    }

    _patientSlideMapLabel(index) {
        const infos = window.pathchat?.plAllFileInfos || window.pathchat?.patientLevelUploadResults || [];
        const info = (infos || []).find((item) => Number(item?.index) === Number(index)) || {};
        const part = String(info.part || '').replace(/^Slide\s+\d+:\s*/i, '').trim();
        const isEn = (window.__lang || 'zh') === 'en';
        if (!Number.isInteger(Number(index))) return part;
        const slide = isEn ? `Slide ${Number(index) + 1}` : `切片 ${Number(index) + 1}`;
        return part ? `${slide} · ${part}` : slide;
    }

    _asPatientWsiIndex(value) {
        // Number(null) === 0, so Number.isInteger(Number(null)) is a trap:
        // omitted indexes would silently become slide 1.
        if (value === null || value === undefined || value === '') return null;
        if (typeof value === 'boolean') return null;
        const n = Number(value);
        return Number.isInteger(n) ? n : null;
    }

    _patientMapWriteIndex() {
        // Never fall back to the slide the doctor is viewing. Parallel
        // workers would otherwise dump every overview into that map.
        if (Number.isInteger(this._patientSwarmMapWsiIndex)) return this._patientSwarmMapWsiIndex;
        // Parallel wsi_started overwrites currentAnalyzingWsiIndex with the
        // last starter. Serial still has exactly one analyzing slide.
        if (this._isPatientParallelRun()) return null;
        if (Number.isInteger(this.currentAnalyzingWsiIndex)) return this.currentAnalyzingWsiIndex;
        return null;
    }

    _syncPatientSlideMapVisibility(preferredIndex = null) {
        const host = document.getElementById('routeStepsList');
        if (!host || !this.isPatientLevelMode) return;
        const want = this._asPatientWsiIndex(preferredIndex)
            ?? this._asPatientWsiIndex(this.currentViewingWsiIndex);
        const maps = Array.from(host.querySelectorAll(':scope > .patient-slide-map'));
        if (!maps.length) return;
        const fallback = want == null ? Number(maps[0].dataset.wsiIndex) : want;
        maps.forEach((map) => {
            const match = Number(map.dataset.wsiIndex) === fallback;
            map.hidden = !match;
            map.classList.toggle('is-active-slide', match);
        });
    }

    _ensurePatientSlideMap(index) {
        const host = document.getElementById('routeStepsList');
        const wsi = this._asPatientWsiIndex(index);
        if (!host || wsi == null) return host;
        let map = host.querySelector(`:scope > .patient-slide-map[data-wsi-index="${wsi}"]`);
        if (!map) {
            map = document.createElement('div');
            map.className = 'patient-slide-map';
            map.dataset.wsiIndex = String(wsi);
            const label = document.createElement('div');
            label.className = 'patient-slide-map-label';
            label.textContent = this._patientSlideMapLabel(wsi);
            map.appendChild(label);
            host.appendChild(map);
        } else {
            const label = map.querySelector(':scope > .patient-slide-map-label');
            if (label) label.textContent = this._patientSlideMapLabel(wsi);
        }
        host.querySelector('.v2-subagent-empty')?.remove();
        this._syncPatientSlideMapVisibility(this.currentViewingWsiIndex);
        return map;
    }

    _patientMapList(preferredIndex) {
        if (!this._isPatientActivityMapMode()) {
            return document.getElementById('routeStepsList');
        }
        const index = this._asPatientWsiIndex(preferredIndex) ?? this._patientMapWriteIndex();
        if (!Number.isInteger(index)) {
            return this._isPatientParallelRun() ? null : document.getElementById('routeStepsList');
        }
        return this._ensurePatientSlideMap(index);
    }

    _currentChatActivity() {
        if (this._isPatientActivityMapMode()) {
            const viewing = Number.isInteger(this.currentViewingWsiIndex)
                ? this.currentViewingWsiIndex
                : this._patientMapWriteIndex();
            if (Number.isInteger(viewing)) {
                const viewed = document.getElementById(`v2AgentActivity-wsi-${viewing}`)
                    || document.querySelector(
                        `#routeStepsList > .patient-slide-map[data-wsi-index="${viewing}"] .v2-subagent-stream`
                    );
                if (viewed) return viewed;
            }
            const write = this._patientMapWriteIndex();
            if (Number.isInteger(write)) {
                const writing = document.getElementById(`v2AgentActivity-wsi-${write}`)
                    || document.querySelector(
                        `#routeStepsList > .patient-slide-map[data-wsi-index="${write}"] .v2-subagent-stream`
                    );
                if (writing) return writing;
            }
            return document.getElementById('v2AgentActivity');
        }
        return document.getElementById('v2AgentActivity')
            || document.querySelector('#routeStepsList .v2-subagent-stream');
    }

    _adoptGlobalActivityIntoSlideMap(index) {
        const global = document.getElementById('v2AgentActivity');
        if (!global || this._asPatientWsiIndex(index) == null) return;
        const owned = String(global.id || '').match(/^v2AgentActivity-wsi-(\d+)$/);
        if (owned && Number(owned[1]) !== Number(index)) return;
        const currentMap = global.closest?.('.patient-slide-map');
        if (currentMap && Number(currentMap.dataset.wsiIndex) !== Number(index)) return;
        const map = this._ensurePatientSlideMap(index);
        if (!map || global.parentElement === map) return;
        global.id = `v2AgentActivity-wsi-${index}`;
        const label = map.querySelector(':scope > .patient-slide-map-label');
        if (label?.nextSibling) map.insertBefore(global, label.nextSibling);
        else map.appendChild(global);
    }

    _updatePatientSlideMapHeading(index) {
        const heading = document.querySelector('#v2SubagentResultsHeading strong');
        if (!heading) return;
        const isEn = (window.__lang || 'zh') === 'en';
        const base = this.activeAnalysisStrategy === 'single_agent_roam'
            ? (isEn ? 'Single Agent progress' : '单 Agent 进度')
            : (isEn ? 'Subagent results' : 'Subagent 结果');
        if (!this._isPatientActivityMapMode() || this._asPatientWsiIndex(index) == null) {
            heading.textContent = base;
            return;
        }
        const label = this._patientSlideMapLabel(index);
        heading.textContent = label ? `${base} · ${label}` : base;
    }

    _showPatientSlideMap(index) {
        const host = document.getElementById('routeStepsList');
        const want = this._asPatientWsiIndex(index);
        if (!host || !this.isPatientLevelMode || want == null) return;
        if (
            this._isPatientSwarmMapMode()
            || this.activeAnalysisStrategy === 'single_agent_roam'
        ) {
            this._adoptGlobalActivityIntoSlideMap(want);
        }
        this._ensurePatientSlideMap(want);
        this._scrubPatientSlideMaps();
        this._syncPatientSlideMapVisibility(want);
        // The map now scopes by slide, so any per-item hiding left by the older
        // single-list layout would blank out the map we just revealed.
        document.querySelectorAll(
            `#routeStepsList > .patient-slide-map[data-wsi-index="${want}"] .v2-agent-activity-item[hidden]`
        ).forEach((el) => { el.hidden = false; });
        // Legacy unscoped nodes stay in the host; hide them so they cannot
        // leak another slide's R7/R1 tree into the current map.
        Array.from(host.children).forEach((el) => {
            if (el.classList.contains('patient-slide-map')) return;
            if (el.classList.contains('v2-subagent-empty')) return;
            el.hidden = true;
        });
        this._updatePatientSlideMapHeading(want);
    }

    _isKnownPatientWsiIndex(value) {
        const n = this._asPatientWsiIndex(value);
        if (n == null) return false;
        const lists = [
            window.pathchat?.plAllFileInfos,
            window.pathchat?.patientLevelUploadResults,
        ];
        for (const list of lists) {
            if ((list || []).some((item) => Number(item?.index) === n)) return true;
        }
        return !(lists[0]?.length || lists[1]?.length) && n >= 0;
    }

    _patientSwarmMapWsiIndexFromEvent(data) {
        const resolved = this._resolvePatientWsiIndex(data);
        if (Number.isInteger(resolved)) return resolved;
        if (data && data.index !== undefined && data.index !== '') {
            const n = Number(data.index);
            if (this._isKnownPatientWsiIndex(n)) return n;
        }
        const fromThumb = this._inferPatientWsiFromImageUrl(
            data?.thumbnail_path || data?.thumbnail || data?.image_url || ''
        );
        if (Number.isInteger(fromThumb)) return fromThumb;
        if (Number.isInteger(this._patientSwarmMapWsiIndex)) return this._patientSwarmMapWsiIndex;
        if (this._isPatientParallelRun()) return null;
        if (Number.isInteger(this.currentAnalyzingWsiIndex)) return this.currentAnalyzingWsiIndex;
        return null;
    }

    _patientSwarmTreeWsiIndex() {
        if (!this._isPatientSwarmMapMode()) return null;
        if (Number.isInteger(this._patientSwarmMapWsiIndex)) return this._patientSwarmMapWsiIndex;
        if (Number.isInteger(this.currentViewingWsiIndex)) return this.currentViewingWsiIndex;
        if (this._isPatientParallelRun()) return null;
        if (Number.isInteger(this.currentAnalyzingWsiIndex)) return this.currentAnalyzingWsiIndex;
        return null;
    }

    _patientSwarmWsiSelectorSuffix() {
        const wsi = this._patientSwarmTreeWsiIndex();
        return Number.isInteger(wsi) ? `[data-wsi-index="${wsi}"]` : '';
    }

    _tagPatientSwarmTreeNode(node) {
        if (!node) return node;
        const wsi = this._patientSwarmTreeWsiIndex();
        if (Number.isInteger(wsi)) node.dataset.wsiIndex = String(wsi);
        return node;
    }

    _withPatientSwarmMapContext(data, fn) {
        const prev = this._patientSwarmMapWsiIndex;
        const next = this._patientSwarmMapWsiIndexFromEvent(data);
        if (Number.isInteger(next)) this._patientSwarmMapWsiIndex = next;
        try {
            return fn();
        } finally {
            this._patientSwarmMapWsiIndex = prev;
        }
    }

    _patientSwarmRouteTreeNodes(list = null) {
        const host = list
            || this._patientMapList(this.currentViewingWsiIndex)
            || document.getElementById('routeStepsList');
        if (!host) return [];
        return Array.from(host.querySelectorAll(
            ':scope > .route-tree-node, :scope > .route-step-card, :scope > .collab-task-card, :scope > .phase-result-card'
        ));
    }

    _filterPatientSwarmMapToWsi(wsiIndex) {
        this._showPatientSlideMap(wsiIndex);
    }

    _patientSwarmMapCountForWsi(wsiIndex) {
        if (!Number.isInteger(Number(wsiIndex))) return 0;
        const map = this._ensurePatientSlideMap(Number(wsiIndex));
        if (!map) return 0;
        return this._patientSwarmRouteTreeNodes(map).length;
    }

    _removePatientSwarmMapForWsi(wsiIndex, options = {}) {
        if (!Number.isInteger(Number(wsiIndex))) return;
        const map = document.querySelector(
            `#routeStepsList > .patient-slide-map[data-wsi-index="${Number(wsiIndex)}"]`
        );
        if (!map) return;
        this._patientSwarmRouteTreeNodes(map).forEach((el) => {
            if (options.keepPhaseCards && el.classList.contains('phase-result-card')) return;
            el.remove();
        });
    }

    _refreshCollabCardAnalysesFromData(wsiIndex) {
        const data = this.patientLevelAnalysisData?.[wsiIndex];
        const reds = Array.isArray(data?.redBoxes) ? data.redBoxes : [];
        reds.forEach((red) => {
            this._applyRoiAnalysisToMapCard(red.id, red.observation, red.rect, red.status);
            (red.blueBoxes || []).forEach((blue) => {
                this._applyRoiAnalysisToMapCard(blue.id, blue.observation, blue.rect, blue.status);
                (blue.greenBoxes || []).forEach((green) => {
                    this._applyRoiAnalysisToMapCard(green.id, green.observation, green.rect, green.status);
                });
            });
        });
    }

    _applyRoiAnalysisToMapCard(roiId, observation, rect, status) {
        if (!roiId) return;
        const card = this._findMappedTreeCard(roiId);
        if (!card) return;
        const nextStatus = status === 'completed' ? 'completed' : (status || 'in-progress');
        if (card.classList.contains('collab-task-card')) {
            this._updateCollabCardStatus(card, nextStatus, observation);
        } else if (observation) {
            this.updateRouteStepStatus(roiId, nextStatus, observation, rect, observation);
            return;
        }
        if (rect) {
            try { card.dataset.rect = JSON.stringify(rect); } catch (_) {}
        }
    }

    _seedOrPreservePatientSlideMap(wsiIndex, options = {}) {
        if (!Number.isInteger(Number(wsiIndex))) return;
        const map = this._ensurePatientSlideMap(Number(wsiIndex));
        const hasPhaseCards = Boolean(map?.querySelector(':scope > .phase-result-card'));
        if (hasPhaseCards && (this.isCollaborativeMode || this.currentMode === 'collaborative')) {
            this._refreshCollabCardAnalysesFromData(Number(wsiIndex));
            const viewing = Number.isInteger(this.currentViewingWsiIndex)
                ? this.currentViewingWsiIndex
                : Number(wsiIndex);
            this._filterPatientSwarmMapToWsi(viewing);
            return;
        }
        this._seedPatientSwarmMapFromData(wsiIndex, {
            ...options,
            keepPhaseCards: true,
            replace: options.replace !== false && !hasPhaseCards
        });
    }

    _seedPatientSwarmMapFromData(wsiIndex, options = {}) {
        if (!this._isPatientSwarmMapMode()) return;
        if (!Number.isInteger(Number(wsiIndex))) return;
        const wsi = Number(wsiIndex);
        this._activateSubagentUi();
        const list = document.getElementById('routeStepsList');
        if (!list) return;
        const data = this.patientLevelAnalysisData?.[wsi];
        const reds = Array.isArray(data?.redBoxes) ? data.redBoxes : [];
        if (options.replace !== false) {
            this._removePatientSwarmMapForWsi(wsi, { keepPhaseCards: options.keepPhaseCards === true });
        }
        if (!reds.length) {
            this._filterPatientSwarmMapToWsi(
                Number.isInteger(this.currentViewingWsiIndex) ? this.currentViewingWsiIndex : wsi
            );
            return;
        }
        const prev = this._patientSwarmMapWsiIndex;
        this._patientSwarmMapWsiIndex = wsi;
        try {
            reds.forEach((red) => {
                const redDone = red.status === 'completed';
                const redText = red.observation || '';
                this.appendRouteStep({
                    level: 'red',
                    roi_id: red.id,
                    title: `${red.id} (10×)`,
                    content: redText,
                    reasoning: redText,
                    rect: red.rect || null,
                    status: redDone ? 'completed' : 'in-progress',
                    wsiIndex: wsi
                });
                (red.blueBoxes || []).forEach((blue) => {
                    const blueDone = blue.status === 'completed';
                    const blueText = blue.observation || '';
                    this.appendRouteStep({
                        level: 'blue',
                        roi_id: blue.id,
                        parent_id: red.id,
                        title: blue.id,
                        content: blueText,
                        reasoning: blueText,
                        rect: blue.rect || null,
                        status: blueDone ? 'completed' : 'in-progress',
                        wsiIndex: wsi
                    });
                    (blue.greenBoxes || []).forEach((green) => {
                        const greenDone = green.status === 'completed';
                        const greenText = green.observation || '';
                        this.appendRouteStep({
                            level: 'green',
                            roi_id: green.id,
                            parent_id: blue.id,
                            title: green.id,
                            content: greenText,
                            reasoning: greenText,
                            rect: green.rect || null,
                            status: greenDone ? 'completed' : 'in-progress',
                            wsiIndex: wsi
                        });
                    });
                });
            });
        } finally {
            this._patientSwarmMapWsiIndex = prev;
        }
        const map = this._ensurePatientSlideMap(wsi);
        this._dedupeCollabCardsIn(map);
        const viewing = Number.isInteger(this.currentViewingWsiIndex)
            ? this.currentViewingWsiIndex
            : wsi;
        this._filterPatientSwarmMapToWsi(viewing);
    }

    _dedupeCollabCardsIn(list) {
        if (!list) return;
        const groups = new Map();
        Array.from(list.querySelectorAll('.collab-task-card[data-roi-id]')).forEach((card) => {
            const id = String(card.dataset.roiId || '').trim();
            if (!id) return;
            if (!groups.has(id)) groups.set(id, []);
            groups.get(id).push(card);
        });
        groups.forEach((cards) => {
            if (cards.length < 2) return;
            const keep = cards.reduce((best, card) => {
                const len = String(card.querySelector('.collab-card-analysis')?.textContent || '').length;
                const bestLen = String(best.querySelector('.collab-card-analysis')?.textContent || '').length;
                if (len !== bestLen) return len > bestLen ? card : best;
                if (card.classList.contains('status-done') && !best.classList.contains('status-done')) return card;
                return best;
            });
            cards.forEach((card) => {
                if (card !== keep) card.remove();
            });
        });
    }

    _markPatientSwarmCacheRestored() {
        this.thumbnailRootCompleted = true;
        this.thumbnailPlanAnnounced = true;
        if (this.activityTimer) {
            clearInterval(this.activityTimer);
            this.activityTimer = null;
        }
        const activity = this._ensureChatActivity();
        if (!activity) return;
        activity.classList.remove('is-running', 'is-stopped', 'is-error');
        activity.classList.add('is-completed');
        const isEn = (window.__lang || 'zh') === 'en';
        const currentText = activity.querySelector('.v2-agent-current-text');
        if (currentText) {
            currentText.textContent = isEn
                ? 'Thumbnail interpretation complete'
                : '缩略图解读完成';
        }
        const currentPercent = activity.querySelector('.v2-agent-current-percent');
        const fill = activity.querySelector('.v2-agent-progress-fill');
        if (currentPercent) currentPercent.textContent = '100%';
        if (fill) fill.style.width = '100%';
    }

    _clearRestoredSelectionHighlights() {
        // Swarm reload shows every box and the tree, but selects nothing
        // until the doctor clicks. Roam JSON used to replay the last click
        // (yellow card + glowing viewer box + leftover left-rail tree).
        document.querySelectorAll(
            '.v2-agent-activity-item.roi-selected-glow, '
            + '.v2-agent-activity-item.active-viewing, '
            + '.v2-agent-activity-item.active, '
            + '.v2-agent-activity-item.card-active, '
            + '.v2-agent-activity-item.flash-highlight, '
            + '.v2-agent-activity-item.is-focused-slide'
        ).forEach((el) => {
            el.classList.remove(
                'roi-selected-glow',
                'active-viewing',
                'active',
                'card-active',
                'flash-highlight',
                'is-focused-slide'
            );
        });
        document.querySelectorAll(
            '.roi-overlay.roi-selected-glow, .roi-overlay.overlay-highlight'
        ).forEach((el) => {
            el.classList.remove('roi-selected-glow', 'overlay-highlight');
            el.style.transform = '';
            el.style.animation = '';
        });
        document.querySelectorAll(
            '.pl-wsi-body .analysis-tree .roi-selected-glow, '
            + '.pl-wsi-body .analysis-tree .active'
        ).forEach((el) => el.classList.remove('roi-selected-glow', 'active'));
    }

    _markRestoredRoamNodeCompleted(index) {
        // A restored slide the doctor has not opened yet would otherwise keep
        // the live "opening the slide… 2%" header and stay collapsed.
        const node = document.getElementById(`v2AgentActivity-wsi-${index}`);
        if (!node) return;
        node.querySelectorAll('.v2-agent-activity-item[hidden]')
            .forEach((el) => { el.hidden = false; });
        node.classList.remove('is-running', 'is-stopped', 'is-error');
        node.classList.add('is-completed');
        const isEn = (window.__lang || 'zh') === 'en';
        const currentText = node.querySelector('.v2-agent-current-text');
        if (currentText) {
            currentText.textContent = isEn
                ? 'Single Agent navigation complete'
                : '单 Agent 游走已完成';
        }
        const percent = node.querySelector('.v2-agent-current-percent');
        if (percent) percent.textContent = '100%';
        const fill = node.querySelector('.v2-agent-progress-fill');
        if (fill) fill.style.width = '100%';
    }

    _restorePatientRoamMapsFromCache(focusIndex) {
        if (this.activeAnalysisStrategy !== 'single_agent_roam') return;
        this.thumbnailRootCompleted = true;
        this.thumbnailPlanAnnounced = true;
        this._activateSubagentUi();
        const focus = this._asPatientWsiIndex(focusIndex);
        if (focus != null) this.currentViewingWsiIndex = focus;
        // Stale nodes would keep their old slide, and every rebuilt step would
        // be deduped away against them.
        document.querySelectorAll(
            '#routeStepsList > .patient-slide-map, [id^="v2AgentActivity-wsi-"]'
        ).forEach((el) => el.remove());
        this._patientRoamDisplayedKeys?.clear?.();
        this.activityDetailKeys = new Set();
        this.activityImageKeys = new Set();
        // Rebuild every slide's steps now: a doctor clicking through the case
        // rail should find each slide's walk already there.
        const data = this.patientLevelAnalysisData || {};
        const prevMapIndex = this._patientSwarmMapWsiIndex;
        try {
            Object.keys(data).forEach((key) => {
                const index = this._asPatientWsiIndex(key);
                if (index == null) return;
                if (!Array.isArray(data[index]?.trajectory) || !data[index].trajectory.length) return;
                const fileId = this._fileIdForPatientWsi(index);
                if (!fileId) return;
                // Pin the write target: without it every slide's steps land in
                // the viewed slide's map.
                this._patientSwarmMapWsiIndex = index;
                this._ensurePatientRoamStoreForFile(fileId, index);
                this._restorePatientRoamForFile(fileId, { forceRebuild: true, skipOverlays: true });
                this._markRestoredRoamNodeCompleted(index);
            });
        } finally {
            this._patientSwarmMapWsiIndex = prevMapIndex;
        }
        this._markPatientSwarmCacheRestored();
        if (focus != null) this._filterPatientActivityToWsi(focus);
        this._clearRestoredSelectionHighlights();
    }

    _restorePatientSwarmMapsFromCache(focusIndex) {
        if (!this._isPatientSwarmMapMode()) return;
        this.thumbnailRootCompleted = true;
        this.thumbnailPlanAnnounced = true;
        if (Number.isInteger(Number(focusIndex))) {
            this.currentViewingWsiIndex = Number(focusIndex);
        }
        this._activateSubagentUi();
        this._markPatientSwarmCacheRestored();
        const data = this.patientLevelAnalysisData || {};
        Object.keys(data).forEach((key) => {
            const idx = Number(key);
            if (!Number.isInteger(idx)) return;
            if (!Array.isArray(data[idx]?.redBoxes) || !data[idx].redBoxes.length) return;
            this._seedOrPreservePatientSlideMap(idx, { replace: true });
        });
        const viewing = Number.isInteger(this.currentViewingWsiIndex)
            ? this.currentViewingWsiIndex
            : Number(focusIndex);
        if (Number.isInteger(viewing)) this._filterPatientSwarmMapToWsi(viewing);
        this._markPatientSwarmCacheRestored();
    }

    _usableRoiRect(...candidates) {
        for (const rect of candidates) {
            if (rect && typeof rect === 'object' && rect.x !== undefined && rect.y !== undefined) {
                return rect;
            }
        }
        return null;
    }

    _patientBoxesById(boxes) {
        const map = new Map();
        (boxes || []).forEach((box) => {
            const id = String(box?.id || '');
            if (id) map.set(id, box);
        });
        return map;
    }

    _hydratePatientSwarmDataFromResult(index, result = {}) {
        if (!result || !Array.isArray(result.tree_nodes) || !result.tree_nodes.length) return null;
        const existing = this.patientLevelAnalysisData?.[index] || {};
        const existingReds = this._patientBoxesById(existing.redBoxes);
        const wsiData = {
            globalReasoning: result.global_reasoning || existing.globalReasoning || '',
            redBoxes: []
        };
        result.tree_nodes.forEach((redNode) => {
            const redId = redNode.roi_id || redNode.id;
            const prevRed = existingReds.get(String(redId));
            const existingBlues = this._patientBoxesById(prevRed?.blueBoxes);
            const redBox = {
                id: redId,
                rect: this._usableRoiRect(redNode.rect, redNode.analysis?.rect, prevRed?.rect),
                status: 'completed',
                observation: redNode.analysis?.reasoning || redNode.analysis?.findings || prevRed?.observation || '',
                confidence: redNode.analysis?.confidence || prevRed?.confidence || 0,
                blueBoxes: []
            };
            (redNode.children || []).forEach((blueNode) => {
                const blueId = blueNode.roi_id || blueNode.id;
                const prevBlue = existingBlues.get(String(blueId));
                const existingGreens = this._patientBoxesById(prevBlue?.greenBoxes);
                const blueBox = {
                    id: blueId,
                    rect: this._usableRoiRect(blueNode.rect, blueNode.analysis?.rect, prevBlue?.rect),
                    status: 'completed',
                    observation: blueNode.analysis?.reasoning || blueNode.analysis?.findings || prevBlue?.observation || '',
                    confidence: blueNode.analysis?.confidence || prevBlue?.confidence || 0,
                    greenBoxes: []
                };
                (blueNode.children || []).forEach((greenNode) => {
                    const greenId = greenNode.roi_id || greenNode.id;
                    const prevGreen = existingGreens.get(String(greenId));
                    blueBox.greenBoxes.push({
                        id: greenId,
                        rect: this._usableRoiRect(greenNode.rect, greenNode.analysis?.rect, prevGreen?.rect),
                        status: 'completed',
                        observation: greenNode.analysis?.reasoning || greenNode.analysis?.findings || prevGreen?.observation || '',
                        confidence: greenNode.analysis?.confidence || prevGreen?.confidence || 0
                    });
                });
                redBox.blueBoxes.push(blueBox);
            });
            wsiData.redBoxes.push(redBox);
        });
        if (!this.patientLevelAnalysisData) this.patientLevelAnalysisData = {};
        this.patientLevelAnalysisData[index] = {
            ...existing,
            ...wsiData,
            roiOverlays: existing.roiOverlays,
            trajectory: existing.trajectory,
        };
        if (window.pathchat && typeof window.pathchat.updatePlWsiAnalysisTree === 'function') {
            window.pathchat.updatePlWsiAnalysisTree(index, wsiData);
        }
        return this.patientLevelAnalysisData[index];
    }

    _syncPatientActivityMapForWsi(index) {
        const wsiIndex = Number(index);
        if (!Number.isInteger(wsiIndex) || !this.isPatientLevelMode) return;
        if (!this._patientActivitySyncedFiles) this._patientActivitySyncedFiles = new Set();
        if (this.activeAnalysisStrategy !== 'single_agent_roam') {
            this._showPatientSlideMap(wsiIndex);
            this._tagPatientActivityItems();
            this._filterPatientActivityToWsi(wsiIndex);
            const data = this.patientLevelAnalysisData?.[wsiIndex];
            const hasData = Array.isArray(data?.redBoxes) && data.redBoxes.length > 0;
            if (hasData && this._patientSwarmMapCountForWsi(wsiIndex) === 0) {
                this._seedPatientSwarmMapFromData(wsiIndex, { replace: false });
            }
            return;
        }
        const fileId = this._fileIdForPatientWsi(wsiIndex);
        this._ensurePatientRoamStoreForFile(fileId, wsiIndex);
        this._tagPatientActivityItems();
        const count = this._patientActivityCountForWsi(wsiIndex);
        const entry = this._patientRoamByFile.get(String(fileId || ''));
        const needsRebuild = Boolean(entry?.events?.length) && (
            (this._patientWorkspaceHtmlRestored && !this._patientActivitySyncedFiles.has(fileId))
            || count === 0
        );
        if (needsRebuild && fileId) {
            this._restorePatientRoamForFile(fileId, { forceRebuild: true, skipOverlays: true });
            this._patientActivitySyncedFiles.add(fileId);
            return;
        }
        this._filterPatientActivityToWsi(wsiIndex);
        if (fileId && count > 0) this._patientActivitySyncedFiles.add(fileId);
    }

    _focusPatientActivityForWsi(index) {
        this._syncPatientActivityMapForWsi(index);
        this._activateDraftReviewForWsi(index);
    }

    _updateChatActivity(data = {}) {
        if (!this.subagentUiActivated) {
            this.pendingActivityUpdate = { ...data };
            return;
        }
        const scopedWsi = this._asPatientWsiIndex(data.wsiIndex);
        const prevMap = this._patientSwarmMapWsiIndex;
        if (this._isPatientActivityMapMode() && scopedWsi != null) {
            this._patientSwarmMapWsiIndex = scopedWsi;
        }
        let activity;
        try {
            activity = this._ensureChatActivity();
        } finally {
            this._patientSwarmMapWsiIndex = prevMap;
        }
        if (!activity) return;
        if (
            this.thumbnailRootCompleted
            && activity.classList.contains('is-completed')
            && !this.isPatientLevelMode
        ) return;

        const percentage = Math.max(0, Math.min(100, Number(data.percentage) || 0));
        const message = String(data.message || ((window.__lang || 'zh') === 'en' ? 'Analyzing…' : '正在分析…')).trim();
        const currentText = activity.querySelector('.v2-agent-current-text');
        const currentPercent = activity.querySelector('.v2-agent-current-percent');
        const fill = activity.querySelector('.v2-agent-progress-fill');
        if (currentText) currentText.textContent = message;
        if (currentPercent) currentPercent.textContent = `${Math.round(percentage)}%`;
        if (fill) fill.style.width = `${percentage}%`;

        const key = `${data.status || ''}:${message}:${Math.round(percentage)}`;
        const writeWsi = this._asPatientWsiIndex(data.wsiIndex)
            ?? (this._isPatientSwarmMapMode() ? this._patientMapWriteIndex() : null);
        // Roam already has a live status bar. Re-appending every 2%/4%
        // heartbeat created a new "单 Agent 自主游走" stack in the map.
        if (
            this.activeAnalysisStrategy !== 'single_agent_roam'
            && key !== this.activityLastKey
        ) {
            this.activityLastKey = key;
            this._appendActivityItem({
                title: message,
                state: percentage >= 100 || /完成|complete|done/i.test(message) ? 'done' : 'running',
                key: `progress:${Number.isInteger(writeWsi) ? writeWsi : ''}:${key}`,
                wsiIndex: Number.isInteger(writeWsi) ? writeWsi : null
            });
        } else {
            this.activityLastKey = key;
        }
        this._keepPatientSlideMapOnViewing();
    }

    _appendChatThinking(text, title = '') {
        const value = String(text || '').trim();
        if (!value) return;
        const label = title || ((window.__lang || 'zh') === 'en' ? 'Analysis approach' : 'AI 分析思路');
        this._appendActivityItem({
            title: label,
            detail: value,
            state: 'thinking',
            key: `thinking:${label}:${value}`
        });
    }

    _activityImageUrl(rawPath) {
        const value = String(rawPath || '').trim().replace(/\\/g, '/');
        if (!value) return '';
        if (/^(https?:|data:|blob:)/i.test(value)) return value;
        if (value.startsWith('/')) return value;
        for (const marker of ['outputs/', 'debug_images/', 'static/']) {
            const index = value.toLowerCase().indexOf(marker.toLowerCase());
            if (index >= 0) return `/${value.slice(index)}`;
        }
        return `/${value}`;
    }

    _appendTraceActivity(data = {}) {
        const evidence = Array.isArray(data.evidence) ? data.evidence[0] : null;
        const title = String(data.observation || data.decision || '').trim();
        const detail = data.decision && data.decision !== title ? String(data.decision) : '';
        const imageUrl = this._activityImageUrl(evidence?.thumb || data.thumbnail_path || '');
        if (!title && !imageUrl) return;
        const traceWsi = this._patientSwarmMapWsiIndexFromEvent(data);
        this._appendActivityItem({
            title: title || ((window.__lang || 'zh') === 'en' ? 'Visual evidence generated' : '已生成视觉证据'),
            detail,
            imageUrl,
            state: 'done',
            key: `trace:${Number.isInteger(traceWsi) ? traceWsi : ''}:${data.step || ''}:${title}:${imageUrl}`,
            wsiIndex: Number.isInteger(traceWsi) ? traceWsi : null
        });
    }

    _parseLoopActionRoiId(content) {
        const text = String(content || '').trim();
        const match = text.match(/\b([A-Za-z][A-Za-z0-9._-]{0,40})\s*[:：]/);
        return match ? match[1] : '';
    }

    _isLoopDecisionChatStatus(data = {}, content = '') {
        const action = String(data.loop_action || data.metadata?.loop_action || '').toUpperCase();
        if (action === 'B' || action === 'C') return true;
        if (String(data.chat_style || data.metadata?.chat_style || '') === 'agent_status') {
            return action === 'B' || action === 'C' || /停止继续放大|触发回溯|已回溯|stop further|backtrack/i.test(content);
        }
        return /停止继续放大|触发回溯|当前视野无诊断价值/.test(content);
    }

    _appendLoopDecisionChatStatus(data = {}, content = '') {
        if (!this._isLoopDecisionChatStatus(data, content)) return false;
        // Parallel: ROI ids repeat across slides and this line has no slide
        // label. Keep the decision in the middle map; do not post to chat.
        if (!this._shouldPostPatientSlideDetailToChat()) return true;
        if (!window.pathchat?.addAgentStatus) return false;

        const action = String(data.loop_action || data.metadata?.loop_action || '').toUpperCase();
        const text = String(content || '');
        const isBacktrack = action === 'C' || /触发回溯|已回溯|backtrack/i.test(text);
        const roiId = String(data.roi_id || data.metadata?.roi_id || this._parseLoopActionRoiId(text) || '').trim();
        const isEn = (window.__lang || 'zh') === 'en';
        const seeLeft = isEn ? 'details on the left' : '详情参见左侧';
        const detail = isBacktrack
            ? (isEn ? `backtrack · ${seeLeft}` : `已回溯 · ${seeLeft}`)
            : (isEn ? `stop zooming · ${seeLeft}` : `停止继续放大 · ${seeLeft}`);
        window.pathchat.addAgentStatus(roiId || 'Subagent', detail, 'done');
        return true;
    }

    _appendRedSubagentChatStatus(roiId, childCount = 0) {
        if (!this._shouldPostPatientSlideDetailToChat()) return;
        const normalizedId = String(roiId || '').trim();
        if (!normalizedId || !window.pathchat?.addAgentStatus) return;
        const count = Math.max(0, Number(childCount) || 0);
        const key = `${normalizedId}:${count}`;
        if (this.chatSubagentStatusKeys.has(key)) return;
        this.chatSubagentStatusKeys.add(key);
        const isEn = (window.__lang || 'zh') === 'en';
        const detail = count > 0
            ? (isEn
                ? `interpretation complete · dispatched ${count} child Subagents for deeper review`
                : `解读完毕 · 派发 ${count} 个下级 Subagent 继续下钻`)
            : (isEn
                ? 'interpretation complete · no deeper review needed at this level'
                : '解读完毕 · 当前层无需继续下钻');
        window.pathchat.addAgentStatus(normalizedId, detail, 'done');
    }

    /**
     * ⭐ 单切片 Swarm 自动模式：每个红框子树完成时，右侧给一行
     * 「Rx 结果已收到 (k/N)」小字状态，对齐患者级的进度体验。
     * 患者级/协作/游走模式各有自己的进度通道，不走这里。
     */
    _appendRedResultReceivedStatus(data, level) {
        if (data?.is_result !== true || level !== 'red') return;
        if (this.isPatientLevelMode || this.isCollaborativeMode) return;
        if (this.activeAnalysisStrategy === 'single_agent_roam') return;
        const roiId = String(data.roi_id || data.metadata?.roi_id || '').trim();
        if (!roiId || roiId === 'AI-Red-Selection') return;
        if (!window.pathchat?.addAgentStatus) return;
        if (!this._redResultReceivedIds) this._redResultReceivedIds = new Set();
        if (this._redResultReceivedIds.has(roiId)) return;
        this._redResultReceivedIds.add(roiId);
        const received = this._redResultReceivedIds.size;
        const total = Math.max(Number(this._swarmRedTotal) || 0, received);
        const isEn = (window.__lang || 'zh') === 'en';
        const detail = isEn
            ? `result received (${received}/${total})`
            : `结果已收到 (${received}/${total})`;
        window.pathchat.addAgentStatus(roiId, detail, 'done');
    }

    _finishChatActivity(status = 'completed', detail = '') {
        if (this.activityTimer) {
            clearInterval(this.activityTimer);
            this.activityTimer = null;
        }

        if (!this.thumbnailPlanAnnounced && this.thumbnailThinkingMessage?.isConnected && window.pathchat?.updateMessage) {
            const isEn = (window.__lang || 'zh') === 'en';
            const fallbackLabels = {
                completed: isEn ? 'Thumbnail review completed.' : '缩略图分析已完成。',
                stopped: isEn ? 'Analysis stopped.' : '分析已停止。',
                error: isEn ? 'Thumbnail review was interrupted.' : '缩略图分析已中断。'
            };
            const fallback = status === 'error' && detail
                ? `${fallbackLabels.error} ${detail}`
                : (fallbackLabels[status] || fallbackLabels.completed);
            window.pathchat.updateMessage(this.thumbnailThinkingMessage, fallback);
            this.thumbnailThinkingMessage = null;
        }

        if (this.thumbnailToolMessage?.isConnected && window.pathchat?.updateToolActivity) {
            const isEn = (window.__lang || 'zh') === 'en';
            const state = status === 'error' ? 'error' : (status === 'stopped' ? 'stopped' : 'done');
            const toolDetail = state === 'done'
                ? (isEn ? 'Whole-slide thumbnail generated' : '已生成全片缩略图')
                : (state === 'stopped'
                    ? (isEn ? 'WSI thumbnail reading stopped' : 'WSI 缩略图读取已停止')
                    : (isEn ? 'WSI thumbnail reading interrupted' : 'WSI 缩略图读取中断'));
            window.pathchat.updateToolActivity(
                this.thumbnailToolMessage,
                'Python · OpenSlide',
                toolDetail,
                state
            );
            this.thumbnailToolMessage = null;
        }

        const activity = this._currentChatActivity();
        if (!activity) return;
        this._updateActivityElapsed();

        const isEn = (window.__lang || 'zh') === 'en';
        const labels = {
            completed: isEn ? 'Analysis complete' : '分析完成',
            stopped: isEn ? 'Analysis stopped' : '分析已停止',
            error: isEn ? 'Analysis interrupted' : '分析已中断'
        };
        const label = detail || labels[status] || labels.completed;
        activity.classList.remove('is-running', 'is-completed', 'is-stopped', 'is-error');
        activity.classList.add(`is-${status}`);
        const currentText = activity.querySelector('.v2-agent-current-text');
        if (currentText) currentText.textContent = label;
        if (status === 'completed') {
            const currentPercent = activity.querySelector('.v2-agent-current-percent');
            const fill = activity.querySelector('.v2-agent-progress-fill');
            if (currentPercent) currentPercent.textContent = '100%';
            if (fill) fill.style.width = '100%';
        }
        this._appendActivityItem({
            title: label,
            detail: status === 'error' ? detail : '',
            state: status === 'completed' ? 'done' : status,
            key: `finish:${status}:${label}`
        });
    }
    
    // WebSocket事件处理
    handleViewportEvent(data) {
        console.log('🔔 收到viewport事件:', data);
        console.log('📊 overlays数量:', data.overlays ? data.overlays.length : 0);
        if (data?.is_patient_level && !this._patientEventMatchesActiveRun(data)) return;

        if (data.thumbnail_path) {
            const imageUrl = this._activityImageUrl(data.thumbnail_path);
            const viewportWsi = this._patientSwarmMapWsiIndexFromEvent(data)
                ?? this._inferPatientWsiFromImageUrl(imageUrl);
            this._lastOverviewImageUrl = imageUrl || this._lastOverviewImageUrl;
            this._attachChatOverviewThumb(this._thumbnailChatMessage, this._lastOverviewImageUrl);
            this._appendActivityItem({
                title: (window.__lang || 'zh') === 'en' ? 'Slide overview generated' : '已生成切片概览',
                detail: (window.__lang || 'zh') === 'en' ? 'The current field of view and candidate regions are ready.' : '当前视野与候选区域已准备完成。',
                imageUrl,
                state: 'done',
                key: `viewport-thumbnail:${Number.isInteger(viewportWsi) ? viewportWsi : ''}:${imageUrl}`,
                wsiIndex: Number.isInteger(viewportWsi) ? viewportWsi : null
            });
        }
        
        // ⭐ 直接从 window.pathchat 获取 viewer（确保是最新的）
        const currentViewer = window.pathchat && window.pathchat.viewer;
        if (currentViewer && !this.viewer) {
            this.viewer = currentViewer;
            console.log('🔄 更新 viewer 引用');
        }
        
        const applyToViewer = this._patientLiveEventMatchesViewer(data);
        
        // ⭐ 只有跟随模式且有zoom_level时才改变视口位置
        if (applyToViewer && this.isFollowing && currentViewer && data.zoom_level !== undefined) {
            this.followToPosition(data.center_px, data.zoom_level);
        }
        
        // ⭐ 重要修复：无论是否跟随，都要绘制 overlays（只要 viewer 存在）
        const viewportWsiForOverlay = this._patientSwarmMapWsiIndexFromEvent(data);
        const overlays = (data.overlays || []).map((overlay) => (
            Number.isInteger(viewportWsiForOverlay)
                ? { ...overlay, wsiIndex: viewportWsiForOverlay }
                : overlay
        ));
        if (applyToViewer && overlays.length > 0 && currentViewer) {
            console.log('🎨 准备绘制', overlays.length, '个标注框, viewer:', !!currentViewer);
            
            const canSyncOverlays = window.pathchat && typeof window.pathchat.syncROIOverlays === 'function';
            const canDrawColored = window.pathchat && typeof window.pathchat.drawColoredROI === 'function';
            
            console.log('🔍 可用方法: syncROIOverlays=', canSyncOverlays, ', drawColoredROI=', canDrawColored);
            
            if (canSyncOverlays) {
                try {
                    window.pathchat.syncROIOverlays(overlays);
                    console.log('✅ overlays 同步完成');
                    this._backfillTreeNodeRectsFromOverlays(overlays);
                } catch (error) {
                    console.error('❌ syncROIOverlays 执行失败，回退到旧方式:', error);
                    this.updateOverlays(overlays);
                }
            } else if (canDrawColored) {
                // 先清除旧的 overlays
                if (window.pathchat.clearROI) {
                    window.pathchat.clearROI();
                }
                overlays.forEach((overlay, index) => {
                    console.log(`🔹 标注框 ${index + 1}:`, overlay);
                    try {
                        window.pathchat.drawColoredROI(overlay);
                    } catch (error) {
                        console.error(`❌ 标注框 ${index + 1} 绘制失败:`, error);
                    }
                });
                console.log('✅ drawColoredROI 绘制完成');
                this._backfillTreeNodeRectsFromOverlays(overlays);
            } else {
                console.warn('⚠️ 未找到彩色标注绘制方法，使用默认样式');
                this.updateOverlays(overlays);
            }
        } else if (overlays.length === 0) {
            console.log('ℹ️ 没有overlays数据');
        } else if (!currentViewer) {
            console.warn('⚠️ viewer不存在，无法绘制overlays。window.pathchat=', !!window.pathchat);
        }
    }
    
    handleTraceEvent(data) {
        console.log('收到trace事件:', data);
        if (data?.is_patient_level && !this._patientEventMatchesActiveRun(data)) return;
        this.addToTimeline(data);
        this.traceData.push(data);
        this._appendTraceActivity(data);

        if (Array.isArray(data.selected_red_rois) && data.selected_red_rois.length > 0) {
            const ids = data.selected_red_rois.map(roi => roi.id).filter(Boolean);
            const announced = this._announceThumbnailPlan(
                data.global_reasoning,
                ids,
                data.selected_red_rois.length,
                '',
                this._thumbnailContextFromEvent(data)
            );
            if (announced || this.thumbnailPlanAnnounced) {
                // The doctor sees the actual thumbnail interpretation and the
                // dispatch statement first; only then reveal execution UI.
                this._activateSubagentUi();
                this._configureSubagentBoard(ids, data.selected_red_rois.length);
                const dispatchWsi = this._patientSwarmMapWsiIndexFromEvent(data);
                this._appendActivityItem({
                    title: (window.__lang || 'zh') === 'en'
                        ? `Dispatched ${data.selected_red_rois.length} ROI Sub-agents`
                        : `已派发 ${data.selected_red_rois.length} 个 ROI Subagent`,
                    detail: ids.length ? ids.join(' · ') : '',
                    state: 'running',
                    key: `red-subagent-dispatch:${Number.isInteger(dispatchWsi) ? dispatchWsi : ''}:${ids.join(',')}`,
                    wsiIndex: Number.isInteger(dispatchWsi) ? dispatchWsi : null
                });
            }
        }
        
        // 添加树状节点并递归处理子节点
        if (data.tree_node && window.pathchat) {
            // 转换并添加树节点（包括所有子节点）
            if (window.pathchat.reasoningTree) {
                this.addTreeNodeRecursively(data.tree_node, data);
            }
            
            // 存储当前节点（红框）的分析数据
            const analysisData = {
                gpt_analysis: data.observation || '',
                image_path: data.evidence && data.evidence[0] ? data.evidence[0].thumb : null,
                metrics: data.results || {},
                confidence: data.confidence || 0,
                zoom_level: data.zoom_level,
                level_name: data.level_name
            };
            const roiId = data.tree_node.roi_id || data.tree_node.id;
            window.pathchat.storeROIAnalysisData(roiId, analysisData);
            console.log('📦 存储分析数据:', roiId, 'zoom:', data.zoom_level);
            
            // 递归存储所有子节点（蓝框、绿框）的分析数据
            this.storeChildrenAnalysisData(data.tree_node);
        }
        
        // ⭐⭐⭐ 关键修复：如果检测到 selected_red_rois，强制识别为 Patient Level 模式
        // 解决竞争条件：trace 事件可能在 wsi_started 事件处理完成之前到达
        if (data.is_patient_level && data.selected_red_rois && data.selected_red_rois.length > 0) {
            console.log(`🔧 [修复] 检测到 selected_red_rois (${data.selected_red_rois.length}个)，强制启用 Patient Level 模式`);
            
            // ⭐⭐⭐ 核心修复：强制设置 Patient Level 模式（不依赖 DOM 推断）
            // selected_red_rois 只会在 Patient Level 模式下发送，所以这里强制设置是安全的
            if (!this.isPatientLevelMode) {
                this.isPatientLevelMode = true;
                console.log('🔧 [修复] 强制启用 Patient Level 模式（收到 selected_red_rois）');
            }
            
            // 如果 currentAnalyzingWsiIndex 未设置，尝试从展开的 WSI 卡片推断
            if (this.currentAnalyzingWsiIndex === undefined) {
                const inferred = this._resolvePatientWsiIndex(data);
                if (inferred !== undefined) {
                    this.currentAnalyzingWsiIndex = inferred;
                    console.log(`🔧 [修复] 从事件 wsi_index 推断 WSI index=${this.currentAnalyzingWsiIndex}`);
                }
            }
            if (this.currentAnalyzingWsiIndex === undefined) {
                const expandedCard = document.querySelector('.pl-wsi-card.expanded');
                if (expandedCard) {
                    const body = expandedCard.querySelector('.pl-wsi-body');
                    if (body && body.dataset.index !== undefined) {
                        this.currentAnalyzingWsiIndex = parseInt(body.dataset.index) || 0;
                        console.log(`🔧 [修复] 从展开的卡片推断 WSI index=${this.currentAnalyzingWsiIndex}`);
                    }
                }
                // 如果还是找不到，默认使用 0
                if (this.currentAnalyzingWsiIndex === undefined) {
                    this.currentAnalyzingWsiIndex = 0;
                    console.log('🔧 [修复] 默认使用 WSI index=0');
                }
            }
            
            // ⭐⭐⭐ 核心修复：初始化 patientLevelAnalysisData（解决事件竞争）
            if (!this.patientLevelAnalysisData) {
                this.patientLevelAnalysisData = {};
            }
            if (!this.patientLevelAnalysisData[this.currentAnalyzingWsiIndex]) {
                this.patientLevelAnalysisData[this.currentAnalyzingWsiIndex] = { redBoxes: [] };
                console.log(`🔧 [修复] 初始化 WSI#${this.currentAnalyzingWsiIndex} 分析数据存储`);
            }
        }
        
        // ⭐ Patient Level 模式：更新左侧 WSI 卡片的分析树（无论是否有 tree_node）
        const patientWsiIndex = this._patientSwarmMapWsiIndexFromEvent(data);
        if (this.isPatientLevelMode && Number.isInteger(patientWsiIndex)) {
            this.updatePatientLevelFromTrace(data, patientWsiIndex);
        }
    }
    
    /**
     * ⭐ Patient Level 模式：从 trace 事件更新左侧 WSI 卡片的分析树
     * 与单张模式使用相同的数据源和逻辑
     */
    updatePatientLevelFromTrace(traceData, wsiIndex = this._resolvePatientWsiIndex(traceData)) {
        if (!Number.isInteger(wsiIndex)) {
            wsiIndex = this._patientSwarmMapWsiIndexFromEvent(traceData);
        }
        if (!Number.isInteger(wsiIndex) && !this._isPatientParallelRun()) {
            wsiIndex = this.currentAnalyzingWsiIndex;
        }
        if (!Number.isInteger(wsiIndex)) return;
        
        // 确保有该 WSI 的数据存储
        if (!this.patientLevelAnalysisData) {
            this.patientLevelAnalysisData = {};
        }
        if (!this.patientLevelAnalysisData[wsiIndex]) {
            this.patientLevelAnalysisData[wsiIndex] = { redBoxes: [] };
        }
        
        const wsiData = this.patientLevelAnalysisData[wsiIndex];
        
        // ⭐ Phase 1 完成时：红框选定但尚未开始逐个分析，立即在左侧面板显示红框列表 + 全局分析理由
        if (traceData.selected_red_rois && traceData.selected_red_rois.length > 0) {
            console.log(`📊 [Patient Level] Phase 1 完成，收到 ${traceData.selected_red_rois.length} 个红框选定`);
            
            // ⭐ 保存全局红框选择理由（AI 为什么选这些红框）
            if (traceData.global_reasoning) {
                wsiData.globalReasoning = traceData.global_reasoning;
                console.log(`📊 [Patient Level] 全局分析理由: ${traceData.global_reasoning.substring(0, 100)}...`);
            }
            
            traceData.selected_red_rois.forEach(roi => {
                let existing = wsiData.redBoxes.find(r => r.id === roi.id);
                if (!existing) {
                    wsiData.redBoxes.push({
                        id: roi.id,
                        rect: roi.rect,
                        status: 'analyzing',
                        observation: '🔄 正在进行多尺度分析...',
                        confidence: 0,
                        blueBoxes: []
                    });
                }
            });
            
            // ⭐⭐⭐ 核心修复：确保卡片已展开，然后更新 UI（解决 DOM 未就绪问题）
            const tryUpdateUI = () => {
                // 先确保卡片展开
                if (window.pathchat && typeof window.pathchat.togglePlWsiExpand === 'function') {
                    window.pathchat.togglePlWsiExpand(wsiIndex, true);  // 强制展开
                }
                
                // 更新分析树
                if (window.pathchat && typeof window.pathchat.updatePlWsiAnalysisTree === 'function') {
                    const body = document.querySelector(`.pl-wsi-body[data-index="${wsiIndex}"]`);
                    if (body && body.querySelector('.analysis-tree')) {
                        window.pathchat.updatePlWsiAnalysisTree(wsiIndex, wsiData);
                        console.log(`📊 [Patient Level] UI 更新成功 - WSI#${wsiIndex}`);
                        return true;
                    }
                }
                return false;
            };
            
            // 立即尝试更新
            if (!tryUpdateUI()) {
                // DOM 未就绪，延迟重试（解决事件竞争问题）
                console.log(`⏳ [Patient Level] DOM 未就绪，100ms 后重试 - WSI#${wsiIndex}`);
                setTimeout(() => {
                    if (!tryUpdateUI()) {
                        console.log(`⏳ [Patient Level] 第二次重试 - WSI#${wsiIndex}`);
                        setTimeout(() => tryUpdateUI(), 200);
                    }
                }, 100);
            }
            
            // Draw reds only on the slide the user is actually viewing.
            // Parallel traces from other WSIs stay in patientLevelAnalysisData
            // and appear when restoreWsiOverlays runs on switch.
            const currentViewer = window.pathchat && window.pathchat.viewer;
            const eventFile = this._fileIdForPatientWsi(wsiIndex);
            if (
                currentViewer
                && window.pathchat.drawColoredROI
                && this._patientLiveEventMatchesViewer({
                    file_id: eventFile,
                    wsi_index: wsiIndex,
                })
            ) {
                traceData.selected_red_rois.forEach(roi => {
                    if (roi.rect && roi.rect.x !== undefined) {
                        try {
                            window.pathchat.drawColoredROI({
                                id: roi.id,
                                rect: roi.rect,
                                color: '#FF0000',
                                magnification: 10,
                                wsiIndex
                            });
                        } catch (e) {
                            console.warn(`⚠️ 绘制红框 ${roi.id} 失败:`, e);
                        }
                    }
                });
                console.log(`🔴 [Patient Level] 立即绘制 ${traceData.selected_red_rois.length} 个红框 overlay`);
            }

            this._seedPatientSwarmMapFromData(wsiIndex, { replace: false });
            return;
        }
        
        const treeNode = traceData.tree_node;
        
        if (!treeNode) return;
        
        const nodeId = treeNode.id || treeNode.roi_id || '';
        const parentId = treeNode.parent_id || '';
        const zoomLevel = treeNode.magnification || traceData.zoom_level || 10;
        const observation = traceData.observation || treeNode.summary || '';
        const confidence = traceData.confidence || treeNode.confidence || 0;
        const levelName = traceData.level_name || treeNode.level_name || '';
        const rect = treeNode.rect || null;  // ⭐ 获取坐标数据
        
        console.log(`📊 [Patient Level Trace] WSI#${wsiIndex}, 节点=${nodeId}, zoom=${zoomLevel}, level=${levelName}`);
        
        // 根据 zoom_level 判断是红框、蓝框还是绿框
        if (zoomLevel === 10) {
            // 红框（10X）
            let redBox = wsiData.redBoxes.find(r => r.id === nodeId);
            if (!redBox) {
                redBox = {
                    id: nodeId,
                    rect: rect,  // ⭐ 保存坐标
                    status: 'completed',
                    observation: observation,
                    confidence: confidence,
                    levelName: levelName,
                    blueBoxes: []
                };
                wsiData.redBoxes.push(redBox);
            } else {
                redBox.status = 'completed';
                redBox.observation = observation;
                redBox.confidence = confidence;
                redBox.levelName = levelName;
                if (rect) redBox.rect = rect;  // ⭐ 更新坐标
            }
        } else if (zoomLevel === 40) {
            // 蓝框（40X）- 查找父红框
            let parentRedBox = null;
            for (const redBox of wsiData.redBoxes) {
                if (parentId.startsWith(redBox.id) || redBox.id === parentId) {
                    parentRedBox = redBox;
                    break;
                }
            }
            
            if (!parentRedBox && wsiData.redBoxes.length > 0) {
                // 如果找不到，使用最后一个红框
                parentRedBox = wsiData.redBoxes[wsiData.redBoxes.length - 1];
            }
            
            if (parentRedBox) {
                let blueBox = parentRedBox.blueBoxes.find(b => b.id === nodeId);
                if (!blueBox) {
                    blueBox = {
                        id: nodeId,
                        rect: rect,  // ⭐ 保存坐标
                        status: 'completed',
                        observation: observation,
                        levelName: levelName,
                        greenBoxes: []
                    };
                    parentRedBox.blueBoxes.push(blueBox);
                } else {
                    blueBox.status = 'completed';
                    blueBox.observation = observation;
                    blueBox.levelName = levelName;
                    if (rect) blueBox.rect = rect;  // ⭐ 更新坐标
                }
            }
        } else if (zoomLevel >= 160) {
            // 绿框（160X）- 查找父蓝框
            for (const redBox of wsiData.redBoxes) {
                for (const blueBox of redBox.blueBoxes) {
                    if (parentId.startsWith(blueBox.id) || blueBox.id === parentId) {
                        let greenBox = blueBox.greenBoxes.find(g => g.id === nodeId);
                        if (!greenBox) {
                            greenBox = {
                                id: nodeId,
                                rect: rect,  // ⭐ 保存坐标
                                status: 'completed',
                                observation: observation,
                                levelName: levelName
                            };
                            blueBox.greenBoxes.push(greenBox);
                        } else {
                            greenBox.status = 'completed';
                            if (rect) greenBox.rect = rect;  // ⭐ 更新坐标
                            greenBox.observation = observation;
                            greenBox.levelName = levelName;
                        }
                        break;
                    }
                }
            }
        }
        
        // 更新 UI
        if (window.pathchat && typeof window.pathchat.updatePlWsiAnalysisTree === 'function') {
            window.pathchat.updatePlWsiAnalysisTree(wsiIndex, wsiData);
        }
        this._seedPatientSwarmMapFromData(wsiIndex, { replace: false });
    }
    
    
    /**
     * 递归添加树节点到推理树
     * @param {Object} treeNode - 后端返回的树节点
     * @param {Object} traceData - trace事件数据（仅用于根节点）
     */
    addTreeNodeRecursively(treeNode, traceData = null) {
        if (!treeNode || !window.pathchat || !window.pathchat.reasoningTree) return;
        
        // 转换节点数据格式为推理树组件期望的格式
        const nodeData = {
            id: treeNode.roi_id || treeNode.id,
            magnification: treeNode.zoom_level || 10,
            level_name: this.getLevelName(treeNode.level, treeNode.zoom_level),
            confidence: treeNode.analysis?.confidence || (traceData ? traceData.confidence : 0.5) || 0.5,
            summary: treeNode.analysis?.findings || (traceData ? traceData.observation : __t('observation_analyzing')) || __t('observation_analyzing'),
            rect: treeNode.analysis?.rect || null,
            metrics: treeNode.analysis?.metrics || (traceData ? traceData.results : {}) || {}
        };
        
        // 添加到推理树
        window.pathchat.reasoningTree.addNode(nodeData);
        console.log('🌳 添加树节点:', nodeData);
        
        // 递归处理所有子节点
        if (treeNode.children && treeNode.children.length > 0) {
            treeNode.children.forEach(childNode => {
                this.addTreeNodeRecursively(childNode, null);
            });
        }
    }
    
    /**
     * 递归存储子节点的分析数据
     * @param {Object} node - 树节点
     */
    storeChildrenAnalysisData(node) {
        if (!node || !node.children || node.children.length === 0) {
            return;
        }
        
        // 遍历所有子节点
        node.children.forEach(childNode => {
            if (!childNode || !window.pathchat) return;
            
            // 获取子节点的分析数据
            const childAnalysis = childNode.analysis || {};
            const roiId = childNode.roi_id || childNode.id;
            
            // 构建分析数据对象
            const analysisData = {
                gpt_analysis: childAnalysis.findings || childAnalysis.observation || '',
                image_path: childAnalysis.image_path || null,
                metrics: childAnalysis.metrics || {},
                confidence: childAnalysis.confidence || 0,
                zoom_level: childNode.zoom_level || 10,
                level_name: this.getLevelName(childNode.level, childNode.zoom_level)
            };
            
            // 存储分析数据
            window.pathchat.storeROIAnalysisData(roiId, analysisData);
            console.log(`📦 存储${childNode.level}框分析数据:`, roiId, analysisData);
            
            // 递归处理子节点的子节点
            if (childNode.children && childNode.children.length > 0) {
                this.storeChildrenAnalysisData(childNode);
            }
        });
    }
    
    /**
     * 根据level和zoom_level获取层级名称
     * @param {String} level - 层级（red/blue/green）
     * @param {Number} zoomLevel - 放大倍数
     * @returns {String} - 层级名称
     */
    getLevelName(level, zoomLevel) {
        if (level === 'red' || zoomLevel === 10) {
            return '低倍镜视野';
        } else if (level === 'blue' || zoomLevel === 40) {
            return '中倍镜视野';
        } else if (level === 'green' || Number(zoomLevel) >= 160) {
            return '高倍镜视野';
        }
        return '未知视野';
    }
    
    handleProgressEvent(data) {
        console.log('收到progress事件:', data);
        this.updateProgress(data);
        
        // ⭐ 不再显示右下角进度条（左侧路径面板已有进度显示，避免遮挡视图）
        // const progressDiv = document.getElementById('agentProgress');
        // if (progressDiv) {
        //     progressDiv.style.display = 'block';
        // }
        
        // ⭐ 更新路径规划面板进度
        const percentage = data.percentage || 0;
        const message = data.message || __t('observation_analyzing');
        this.updateRouteProgress(percentage, message);
        this._updateChatActivity({
            percentage,
            message,
            status: data.status,
            wsiIndex: this._patientSwarmMapWsiIndexFromEvent(data)
        });
        if (data.status === 'phase_start' && Number(data.phase) >= 2) {
            this._setPendingSubagentsRunning();
        }
        
        // ⭐⭐⭐ Patient Level 模式：Phase 开始时立即更新显示（不等待 phase_result）⭐⭐⭐
        if (this.isPatientLevelMode && data.status === 'phase_start' && data.phase) {
            const phase = data.phase;
            const phaseName = this.getPhaseName(phase);
            const partName = `Part ${(this.currentAnalyzingWsiIndex || 0) + 1}`;
            
            console.log(`[Patient Level] Phase ${phase} 开始，立即更新显示`);
            
            // 更新左上角进度显示（Part X - 阶段名称）
            const currentWsiEl = document.getElementById('plCurrentWsiName');
            if (currentWsiEl) {
                currentWsiEl.textContent = `${partName} - ${phaseName}`;
            }
            
            // 更新总状态标签
            const statusEl = document.getElementById('plStatus');
            if (statusEl) {
                statusEl.textContent = `Phase ${phase}/5`;
            }
            
            // 更新进度状态文字
            const progressStatus = document.getElementById('plProgressStatus');
            if (progressStatus) {
                progressStatus.textContent = `Phase ${phase}/5: ${phaseName}`;
            }
            
            // 更新左下角按钮显示当前 Phase
            const startBtn = document.getElementById('plStartAnalysis');
            if (startBtn) {
                startBtn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> Phase ${phase}/5`;
            }
        }
        
        // ⭐⭐⭐ 关键优化：当 AI 正在分析时，在左侧面板显示一个临时的"分析中"卡片
        // 这样用户在等待 LLM 响应期间能看到系统正在工作
        if (data.status === 'analyzing' && data.phase) {
            this._showAnalyzingCard(data.phase, data.message);
        }
        
        // 中间栏承载 Subagent 结果、视觉证据与执行细节；右侧只保留对话。
    }
    
    // ⭐ 显示"AI 分析中"的临时卡片（在 LLM 响应返回前显示）
    _showAnalyzingCard(phase, message) {
        const phaseName = this.getPhaseName(phase);
        this._appendChatThinking(
            message || ((window.__lang || 'zh') === 'en' ? 'AI is analyzing the current evidence.' : 'AI 正在分析当前证据。'),
            `${(window.__lang || 'zh') === 'en' ? 'Thinking' : '思考中'} · ${phaseName}`
        );
    }

    _patientRoamSlideLabel(data = {}) {
        if (!this.isPatientLevelMode) return '';
        const index = this._resolvePatientWsiIndex(data);
        const infos = window.pathchat?.plAllFileInfos || window.pathchat?.patientLevelUploadResults || [];
        const info = Number.isInteger(index)
            ? (infos.find((item) => Number(item?.index) === Number(index)) || {})
            : {};
        const part = String(data?.part || info.part || '').replace(/^Slide\s+\d+:\s*/i, '').trim();
        const isEn = (window.__lang || 'zh') === 'en';
        if (!Number.isInteger(index)) return part;
        const slide = isEn ? `Slide ${index + 1}` : `切片 ${index + 1}`;
        return part ? `${slide} · ${part}` : slide;
    }

    _roamEvidenceMeta(data = {}) {
        const parts = [];
        const zoom = Number(data?.zoom);
        if (Number.isFinite(zoom) && zoom > 0) parts.push(`${zoom}×`);
        let x = Number(data?.x);
        let y = Number(data?.y);
        const rect = data?.rect;
        if ((!Number.isFinite(x) || !Number.isFinite(y)) && rect) {
            const rx = Number(rect.x);
            const ry = Number(rect.y);
            const rw = Number(rect.w);
            const rh = Number(rect.h);
            if (Number.isFinite(rx) && Number.isFinite(ry)) {
                x = rx + (Number.isFinite(rw) ? rw / 2 : 0);
                y = ry + (Number.isFinite(rh) ? rh / 2 : 0);
            }
        }
        if (Number.isFinite(x) && Number.isFinite(y)) {
            parts.push(`x:${Math.round(x)}, y:${Math.round(y)}`);
        }
        if (rect && Number.isFinite(Number(rect.w)) && Number.isFinite(Number(rect.h))) {
            parts.push(`${Math.round(rect.w)}×${Math.round(rect.h)} px`);
        }
        return parts.join(' · ');
    }

    _roamStepImageUrl(data = {}, step = 0) {
        const raw = String(data?.thumbnail_path || data?.image_url || data?.image || '').trim();
        if (raw && (/^(https?:|data:|blob:|\/)/i.test(raw) || raw.includes('/outputs/') || raw.includes('/api/'))) {
            return this._activityImageUrl(raw);
        }
        if (Number(step) === 0) {
            const fileId = String(this._patientRoamResolvedFileId(data) || '');
            return fileId ? `/api/thumbnail/${encodeURIComponent(fileId)}` : '';
        }
        return '';
    }

    handleRoamStarted(data) {
        const incomingRunId = String(data?.runtime?.run_id || this.runtimeRunId || 'legacy');
        if (
            !this.isPatientLevelMode
            && (!data?._restored_from_snapshot || this._activeRoamRunUiId !== incomingRunId)
        ) {
            this._roamUiEventKeys.clear();
            this._activeRoamRunUiId = incomingRunId;
        }
        this.activeAnalysisStrategy = 'single_agent_roam';
        this._setEvidencePaneMode('single_agent_roam');
        if (!this.activityStartedAt) this._startChatActivity();
        this._activateSubagentUi();
        const applyToViewer = this._patientLiveEventMatchesViewer(data);
        if (applyToViewer) {
            this.isFollowing = true;
            this._cancelRoamViewportAnimation();
            this._clearRoamFollowResumeControl();
            this._lastRoamTarget = null;
            this._pendingRoamTarget = null;
            this._activeRoamSessionId = this.sessionId;
            this._activeRoamFileId = String(data?.file_id || this.currentFileId || '');
            this.primaryAgentState = 'running';
            const roamViewer = this.viewer || window.pathchat?.viewer;
            if (!data?._restored_from_snapshot) {
                try { roamViewer?.clearOverlays?.(); } catch (_) {}
            }
            this.subagentStates.clear();
            this.subagentExpectedCount = 0;
            this._renderSubagentBoard();
        }
        const isEn = (window.__lang || 'zh') === 'en';
        const slideLabel = this._patientRoamSlideLabel(data);
        const fileId = String(this._patientRoamResolvedFileId(data) || '');
        this._appendActivityItem({
            title: slideLabel
                ? (isEn ? `Single Agent navigation started · ${slideLabel}` : `单 Agent 自主游走已开始 · ${slideLabel}`)
                : (isEn ? 'Single Agent navigation started' : '单 Agent 自主游走已开始'),
            detail: isEn
                ? 'This independent route will inspect one FOV at a time and will not create Subagents.'
                : '这是独立路线：逐视野观察，不创建任何 Subagent。',
            state: 'running',
            key: `single-roam-start:${fileId}`,
            wsiIndex: this._resolvePatientWsiIndex(data)
        });
        this._updateChatActivity({
            percentage: 2,
            message: isEn
                ? 'Opening the slide and building the overview…'
                : '正在打开切片并生成整片概览…',
            status: 'single_agent_roam',
            wsiIndex: this._resolvePatientWsiIndex(data)
        });
    }

    _cancelRoamViewportAnimation() {
        if (this._roamViewportTimer) {
            clearTimeout(this._roamViewportTimer);
            this._roamViewportTimer = null;
        }
        const viewer = this.viewer || window.pathchat?.viewer;
        if (this._roamViewportGuardTimer) {
            clearTimeout(this._roamViewportGuardTimer);
            this._roamViewportGuardTimer = null;
        }
        // OpenSeadragon emits pan/zoom while stop() settles.  Keep those
        // programmatic events from masquerading as a manual user gesture.
        this._programmaticViewportMove = true;
        try { viewer?.viewport?.stop?.(); } catch (error) {
            console.debug('停止视野动画已跳过:', error);
        }
        this._roamViewportGuardTimer = setTimeout(() => {
            this._roamViewportGuardTimer = null;
            if (!this._roamViewportTimer) this._programmaticViewportMove = false;
        }, 140);
    }

    _animateRoamViewport(rect, zoom) {
        const viewer = this.viewer || window.pathchat?.viewer;
        if (!viewer || !rect) return;
        const nextRect = {
            x: Number(rect.x), y: Number(rect.y),
            w: Number(rect.w), h: Number(rect.h),
            zoom: Number(zoom) || 10
        };
        if (![nextRect.x, nextRect.y, nextRect.w, nextRect.h].every(Number.isFinite)) return;
        this._pendingRoamTarget = { rect: { ...nextRect }, zoom: nextRect.zoom };
        if (!this.isFollowing) return;

        this._cancelRoamViewportAnimation();
        const expectedSessionId = this._activeRoamSessionId || this.sessionId;
        const expectedFileId = this._activeRoamFileId || String(this.currentFileId || '');
        const mayMove = () => Boolean(
            this.isFollowing
            && !this.isStopping
            && !this._patientStopLatch
            && (this.isRunning || this.isStarting || this._activePatientRoamRunToken)
            && this.activeAnalysisStrategy === 'single_agent_roam'
            && this.sessionId === expectedSessionId
            && String(this.currentFileId || '') === expectedFileId
        );
        const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true;
        const previous = this._lastRoamTarget;
        const nextCenterX = nextRect.x + nextRect.w / 2;
        const nextCenterY = nextRect.y + nextRect.h / 2;
        const nested = Boolean(previous)
            && nextRect.zoom >= previous.zoom
            && nextCenterX >= previous.x && nextCenterX <= previous.x + previous.w
            && nextCenterY >= previous.y && nextCenterY <= previous.y + previous.h;
        const vp = viewer.viewport.imageToViewportRectangle(
            nextRect.x, nextRect.y, nextRect.w, nextRect.h
        );
        const padded = new OpenSeadragon.Rect(
            vp.x - vp.width * 0.12,
            vp.y - vp.height * 0.12,
            vp.width * 1.24,
            vp.height * 1.24
        );
        const focus = () => {
            if (!mayMove()) {
                this._cancelRoamViewportAnimation();
                return;
            }
            this._programmaticViewportMove = true;
            viewer.viewport.fitBounds(padded, reducedMotion);
            this._roamViewportTimer = setTimeout(() => {
                this._programmaticViewportMove = false;
                this._roamViewportTimer = null;
            }, reducedMotion ? 50 : 1200);
        };
        if (!previous || nested || reducedMotion) {
            if (mayMove()) focus();
        } else {
            if (!mayMove()) return;
            this._programmaticViewportMove = true;
            viewer.viewport.goHome(false);
            this._roamViewportTimer = setTimeout(focus, 650);
        }
        this._lastRoamTarget = nextRect;
    }

    handleRoamAction(data) {
        const step = Number(data?.step) || 0;
        const zoom = Number(data?.zoom) || 10;
        const runId = String(data?.runtime?.run_id || this.runtimeRunId || 'legacy');
        this._roamMaxStepByRun.set(
            runId,
            Math.max(this._roamMaxStepByRun.get(runId) || 0, step)
        );
        const eventKey = `${runId}:action:${step}`;
        if (this._roamUiEventKeys.has(eventKey)) return;
        this._roamUiEventKeys.add(eventKey);
        const why = String(data?.why || '').trim();
        const isEn = (window.__lang || 'zh') === 'en';
        const slideLabel = this._patientRoamSlideLabel(data);
        const fileId = String(this._patientRoamResolvedFileId(data) || '');
        const meta = this._roamEvidenceMeta(data);
        const detail = why || (isEn
            ? `Navigating to FOV ${step} at ${zoom}×.`
            : `正在前往视野 ${step}（${zoom}×）。`);
        // Parallel: same rule as Swarm Subagent lines — live FOV actions stay
        // in the middle map. Interleaving three slides' "动作 N" in chat is noise.
        if (window.pathchat?.addAgentStatus && this._shouldPostPatientSlideDetailToChat()) {
            window.pathchat.addAgentStatus(
                isEn ? `Single Agent · Action ${step}` : `单 Agent · 动作 ${step}`,
                detail,
                'running'
            );
        }
        this._appendActivityItem({
            title: slideLabel
                ? (isEn ? `Navigate to FOV ${step} · ${zoom}× · ${slideLabel}` : `前往视野 ${step} · ${zoom}× · ${slideLabel}`)
                : (isEn ? `Navigate to FOV ${step} · ${zoom}×` : `前往视野 ${step} · ${zoom}×`),
            detail,
            meta,
            state: 'running',
            key: `single-roam-action:${fileId}:${step}`,
            roiId: this._roamOverlayId(step, data),
            rect: data?.rect || null,
            magnification: zoom,
            roamKind: 'action',
            wsiIndex: this._resolvePatientWsiIndex(data)
        });
        if (!data?._restored_no_animation && this._patientLiveEventMatchesViewer(data)) {
            this._animateRoamViewport(data?.rect, zoom);
        }
    }

    handleRoamStep(data) {
        const step = Number(data?.step) || 0;
        const zoom = Number(data?.zoom) || null;
        const runId = String(data?.runtime?.run_id || this.runtimeRunId || 'legacy');
        this._roamMaxStepByRun.set(
            runId,
            Math.max(this._roamMaxStepByRun.get(runId) || 0, step)
        );
        const eventKey = `${runId}:step:${step}`;
        if (this._roamUiEventKeys.has(eventKey)) return;
        this._roamUiEventKeys.add(eventKey);
        const observation = String(data?.observation || '').trim();
        const roamRoiId = this._roamOverlayId(step, data);
        const isEn = (window.__lang || 'zh') === 'en';
        const slideLabel = this._patientRoamSlideLabel(data);
        const fileId = String(this._patientRoamResolvedFileId(data) || '');
        const imageUrl = this._roamStepImageUrl(data, step);
        const meta = this._roamEvidenceMeta(data);
        const overviewTitle = isEn ? 'Overview inspected' : '整片概览已观察';
        const fovTitle = isEn
            ? `Single Agent inspected FOV ${step}${Number.isFinite(Number(zoom)) ? ` · ${zoom}×` : ''}`
            : `单 Agent 已观察视野 ${step}${Number.isFinite(Number(zoom)) ? ` · ${zoom}×` : ''}`;
        this._appendActivityItem({
            title: slideLabel
                ? `${step === 0 ? overviewTitle : fovTitle} · ${slideLabel}`
                : (step === 0 ? overviewTitle : fovTitle),
            detail: observation,
            meta,
            imageUrl,
            state: 'done',
            key: `single-roam-step:${fileId}:${step}`,
            roiId: roamRoiId,
            rect: data?.rect || null,
            magnification: zoom,
            roamKind: step > 0 ? 'step' : 'overview',
            wsiIndex: this._resolvePatientWsiIndex(data)
        });
        if (observation && window.pathchat?.addMessage && this._shouldPostPatientSlideDetailToChat()) {
            const title = step === 0
                ? (isEn ? 'Whole-slide overview' : '整片概览')
                : (isEn
                    ? `FOV ${step}${Number.isFinite(Number(zoom)) ? ` · ${zoom}×` : ''} observation`
                    : `视野 ${step}${Number.isFinite(Number(zoom)) ? ` · ${zoom}×` : ''} 观察`);
            const heading = slideLabel ? `${title} · ${slideLabel}` : title;
            const chatMessage = window.pathchat.addMessage('assistant', `**${heading}**\n\n${observation}`, false, step > 0 ? (roamRoiId || `ROAM-${step}`) : null);
            if (imageUrl) this._attachChatOverviewThumb(chatMessage, imageUrl);
        }
        if (
            step > 0
            && data?.rect
            && window.pathchat
            && typeof window.pathchat.drawColoredROI === 'function'
            && this._patientLiveEventMatchesViewer(data)
        ) {
            window.pathchat.drawColoredROI({
                id: roamRoiId || `ROAM-${step}`,
                rect: data.rect,
                magnification: zoom,
                label: `S${step}`
            });
        }
    }

    handleRoamDone(data) {
        if (this._patientLiveEventMatchesViewer(data)) {
            this._cancelRoamViewportAnimation();
            this._clearRoamFollowResumeControl();
            this._activeRoamSessionId = null;
            this._activeRoamFileId = null;
            this.primaryAgentState = data?.status === 'stopped' ? 'stopped' : 'done';
            this._renderSubagentBoard();
        }
        const fileId = String(this._patientRoamResolvedFileId(data) || '');
        const slideLabel = this._patientRoamSlideLabel(data);
        const isEn = (window.__lang || 'zh') === 'en';
        this._appendActivityItem({
            title: slideLabel
                ? (isEn ? `Single Agent navigation completed · ${slideLabel}` : `单 Agent 自主游走已完成 · ${slideLabel}`)
                : (isEn ? 'Single Agent navigation completed' : '单 Agent 自主游走已完成'),
            detail: isEn
                ? `${Number(data?.steps) || 0} FOV(s); stopped by ${data?.stopped_by || 'agent'}.`
                : `共观察 ${Number(data?.steps) || 0} 个视野；停止原因：${data?.stopped_by || 'agent'}。`,
            state: data?.status === 'stopped' ? 'stopped' : 'done',
            key: `single-roam-done:${fileId}`,
            wsiIndex: this._resolvePatientWsiIndex(data)
        });
    }

    _clearCollaborativeReviewHighlights() {
        this._reviewHighlightEpoch += 1;
        document.querySelectorAll('.review-required-glow').forEach((element) => {
            element.classList.remove('review-required-glow');
            delete element.dataset.reviewReasons;
        });
        this._reviewHighlightedIds.clear();
        this.collaborativeReviewQueue = [];
    }

    _reviewReasonLabel(code, isEn) {
        const labels = {
            decisive_for_report: ['终报关键依据', 'Decisive for the draft report'],
            low_confidence: ['证据置信度较低', 'Low evidence confidence'],
            indeterminate_pathology: ['病变性质尚不确定', 'Indeterminate finding'],
            uncertain_classification: ['病变分类尚不确定', 'Uncertain classification'],
            secondary_review_rejected: ['二次复核未支持首次主张', 'Secondary review rejected the first-pass claim'],
            secondary_review_indeterminate: ['二次复核仍不确定', 'Secondary review remains indeterminate'],
            secondary_review_skip: ['二次复核未执行', 'Secondary review was not completed'],
            secondary_review_not_run: ['二次复核未运行', 'Secondary review was not run'],
            analysis_failed: ['该区域分析失败', 'ROI analysis failed'],
        };
        const pair = labels[String(code || '').toLowerCase()];
        return pair ? pair[isEn ? 1 : 0] : String(code || '').replaceAll('_', ' ');
    }

    _reviewQueueIsPublished(data, items = null) {
        const queueItems = Array.isArray(items)
            ? items
            : (Array.isArray(data?.items)
                ? data.items
                : (Array.isArray(data?.review_queue) ? data.review_queue : []));
        if (data?.review_queue_published === true || data?.published === true) return true;
        if (queueItems.length > 0) return true;
        if (data?._restored_from_snapshot || data?._restored_from_cache) return false;
        // Live Phase-4 emits always include `items`, even when the list is
        // empty. Collaboration snapshots use `review_queue` and must set the
        // published flag before an empty list counts as a conclusion.
        return Array.isArray(data?.items);
    }

    _reviewQueueActionsAreCurrent(actions) {
        if (!actions || actions.classList?.contains('is-retired')) return false;
        const reviewFileId = String(actions.dataset?.reviewFileId || '');
        const reviewRunId = String(actions.dataset?.reviewRunId || '');
        const currentFileId = String(this.currentFileId || '');
        const currentRunId = String(this.runtimeRunId || '');
        if (!reviewFileId || !currentFileId || reviewFileId !== currentFileId) return false;
        // A completed Run may already have been cleared from the live Runtime
        // identity.  Keep its review buttons usable on the same slide until a
        // new Run explicitly retires them, but never project one Run onto
        // another active Run of that slide.
        if (reviewRunId && currentRunId && reviewRunId !== currentRunId) return false;
        return true;
    }

    _syncReviewQueueActions() {
        const isEn = (window.__lang || 'zh') === 'en';
        document.querySelectorAll('.review-queue-actions').forEach((actions) => {
            const enabled = this._reviewQueueActionsAreCurrent(actions);
            actions.querySelectorAll('button').forEach((button) => {
                button.disabled = !enabled;
                if (!enabled) {
                    button.title = isEn
                        ? 'This review item belongs to another slide or analysis run.'
                        : '该复核项属于另一张切片或另一轮分析。';
                } else {
                    button.removeAttribute('title');
                }
            });
        });
    }

    handleReviewQueue(data) {
        if (!data?._restored_from_snapshot && !data?._restored_from_cache) {
            this._collaborationLiveSerial += 1;
            this._collaborationSnapshotEpoch += 1;
        }
        const incomingRunId = String(data?.runtime?.run_id || '');
        const fileId = String(data?.file_id || this.currentFileId || '');
        if (incomingRunId && this.runtimeRunId && incomingRunId !== this.runtimeRunId) return;
        const items = Array.isArray(data?.items) ? data.items : [];
        if (!this._reviewQueueIsPublished(data, items)) return;
        const cached = {
            items,
            file_id: fileId,
            review_policy: data?.review_policy || this.collaborationReviewPolicy,
            review_queue_published: true,
            runtime: incomingRunId ? { run_id: incomingRunId } : undefined,
        };
        if (fileId) {
            if (items.length) this._reviewQueuesByFile.set(fileId, cached);
            else this._reviewQueuesByFile.delete(fileId);
        }
        if (fileId && this.currentFileId && fileId !== String(this.currentFileId)) return;

        this._clearCollaborativeReviewHighlights();
        const highlightEpoch = this._reviewHighlightEpoch;
        const highlightRunId = String(incomingRunId || this.runtimeRunId || '');
        const highlightFileId = String(fileId || '');
        this.collaborativeReviewQueue = items;
        // The draft already names these ROIs. Do not also print a
        // review-queue-roi-btn list in chat — it duplicates the report and
        // clips behind the composer.
        const reviewViewerReady = Boolean(
            window.pathchat?.viewer?.world?.getItemCount?.() > 0
        );
        if (!reviewViewerReady) return;
        items.forEach(item => {
            if (item?.rect && window.pathchat?.drawColoredROI) {
                window.pathchat.drawColoredROI({
                    id: item.roi_id,
                    rect: item.rect,
                    magnification: item.level === 'green' ? 160 : (item.level === 'blue' ? 40 : 10),
                    label: item.roi_id,
                    review_required: true
                });
            }
        });
        setTimeout(() => {
            if (
                highlightEpoch !== this._reviewHighlightEpoch
                || String(this.runtimeRunId || '') !== highlightRunId
                || String(this.currentFileId || '') !== highlightFileId
            ) return;
            items.forEach(item => {
                document.querySelectorAll(`.roi-overlay[data-roi-id="${CSS.escape(item.roi_id)}"]`).forEach(el => {
                    el.classList.add('review-required-glow');
                    el.dataset.reviewReasons = (item.reason_codes || []).join(',');
                    this._reviewHighlightedIds.add(item.roi_id);
                });
            });
        }, 0);
    }
    
    handleDoneEvent(data) {
        console.log('收到done事件:', data);

        const settlingRunId = data?.runtime?.run_id || this.runtimeRunId;
        const runtimeTracked = Boolean(settlingRunId);
        // The Socket event is emitted just before the managed Attempt commits
        // its terminal state.  Keep the durable identity until status confirms
        // that commit; otherwise reconnect/cancel would lose the Run.
        this.isRunning = runtimeTracked;
        this.isStarting = false;
        this.isStopping = false;
        this.primaryAgentState = data.status === 'stopped' ? 'stopped' : 'done';
        if (!runtimeTracked) this._clearRuntimeRun();
        else this._settleRuntimeAfterLegacySignal(settlingRunId);
        this.updateButtons();
        
        const message = data.status === 'stopped' ? __t('analysis_stopped') : __t('agent_done');
        this.showNotification(message, 'success');
        this._finishChatActivity(data.status === 'stopped' ? 'stopped' : 'completed', message);
        this._clearPendingBlueAliases();
        this.subagentStates.forEach((state, id) => {
            if (data.status !== 'stopped' && state !== 'error') this.subagentStates.set(id, 'done');
            if (data.status === 'stopped' && (state === 'running' || state === 'pending')) this.subagentStates.set(id, 'stopped');
        });
        this._renderSubagentBoard();
        
        // 隐藏进度条
        const progressDiv = document.getElementById('agentProgress');
        if (progressDiv) {
            progressDiv.style.display = 'none';
        }
        
        // ⭐ 更新路径规划面板为完成状态
        const statusText = data.status === 'stopped' ? __t('analysis_stopped') : `✅ ${__t('status_completed')}`;
        this.updateRouteProgress(100, statusText);
        
        // ⭐ 更新所有树节点状态为"完成"
        if (data.status !== 'stopped') {
            this._markAllTreeNodesComplete();
        }
        
        if (window.pathchat) {
            window.pathchat._treeLoadedForId = null;
        }
        
        // ⭐ 从 done 事件的 report_path 直接提取 output 目录名（最可靠的方式）
        if (!this.isPatientLevelMode && data.report_path) {
            const _rpMatch = data.report_path.match(/outputs\/([^/]+)\//);
            if (_rpMatch) {
                this.currentOutputDirName = _rpMatch[1];
                console.log('📁 从 report_path 提取 outputDirName:', this.currentOutputDirName);
            }
        }
        
        // ⭐ 单切片分析完成后，用 analysis_tree.json 补画事件流漏掉的蓝/绿框，
        // 这样画布完整，随后的自动保存快照也会包含全部框。
        if (data.status !== 'stopped') {
            this._backfillOverlaysFromAnalysisTree();
        }
        
        // 自动保存分析结果
        if (data.status !== 'stopped' && window.pathchat) {
            console.log('💾 Agent完成，自动保存分析结果...');
            setTimeout(() => {
                // 部分 ROI 结果会比 done 事件稍晚写入前端缓存；保存前再做一次
                // 完成状态与正文回填，避免叶节点永久停留在“进行中”。
                this._markAllTreeNodesComplete();
                // Completion persistence is background work: write app cache
                // only. The toolbar Save button is a separate user export.
                window.pathchat.saveAnalysisState({ silent: true });
            }, 1000);
        }
        
    }
    
    /**
     * ⭐ 单切片模式完成后按 analysis_tree.json 补画缺失的红/蓝/绿框。
     * 并行线程的 viewport 事件可能丢失，导致画布上蓝/绿框不全；
     * 树文件是唯一完整来源。患者级有自己的恢复通道（restoreWsiOverlays），
     * 单 Agent 游走画的是编号视野框，两者都不走这里。
     */
    async _backfillOverlaysFromAnalysisTree() {
        if (this.isPatientLevelMode) return;
        if (this.activeAnalysisStrategy === 'single_agent_roam') return;
        const pathchat = window.pathchat;
        if (!pathchat?.viewer || typeof pathchat._analysisTreeRequestUrl !== 'function') return;
        if (typeof pathchat._backfillOverlaysFromTreeBoxes !== 'function') return;
        try {
            const resp = await fetch(pathchat._analysisTreeRequestUrl());
            if (!resp.ok) return;
            const result = await resp.json();
            const treeNodes = result?.data?.tree_nodes;
            if (!result?.success || !Array.isArray(treeNodes) || !treeNodes.length) return;
            const redBoxes = treeNodes.map((redNode) => ({
                id: redNode.roi_id || redNode.id,
                rect: redNode.rect,
                blueBoxes: (redNode.children || []).map((blueNode) => ({
                    id: blueNode.roi_id || blueNode.id,
                    rect: blueNode.rect,
                    greenBoxes: (blueNode.children || []).map((greenNode) => ({
                        id: greenNode.roi_id || greenNode.id,
                        rect: greenNode.rect
                    }))
                }))
            }));
            pathchat._backfillOverlaysFromTreeBoxes(redBoxes);
        } catch (err) {
            console.warn('⚠️ 完成后按分析树补画标注框失败:', err);
        }
    }

    // ⭐ 将所有树节点标记为完成状态
    _markAllTreeNodesComplete() {
        const list = document.getElementById('routeStepsList');
        if (!list) return;
        
        // 更新所有树节点的badge
        const allBadges = list.querySelectorAll('.route-tree-badge, .route-step-badge');
        allBadges.forEach(badge => {
            if (badge.textContent === __t('status_in_progress') || badge.textContent === 'In Progress' || badge.textContent === '进行中' || badge.classList.contains('in-progress')) {
                badge.className = badge.className.replace('in-progress', 'completed').replace('waiting', 'completed');
                if (!badge.classList.contains('completed')) {
                    badge.classList.add('completed');
                }
                badge.textContent = __t('status_completed');
            }
        });

        // 绿框叶节点没有 badge，不能依赖上面的 badge 查询完成回填。
        // 使用已缓存的 ROI 分析正文同步修正标题、状态和结果内容。
        list.querySelectorAll('.route-tree-green-item[data-roi-id]').forEach(card => {
            this._completeGreenTreeNode(card);
        });
        
        console.log('[Tree] 所有节点已标记为完成');
    }
    
    // ⭐ 处理HTML报告就绪事件
    handleReportReady(data) {
        console.log('📄 HTML报告已就绪:', data);
        
        const htmlPath = data.html_path;
        if (!htmlPath) return;
        this.currentHtmlReportUrl = htmlPath;
        
        // 记录 output 目录名，供 saveAnalysisState 使用以统一 _num
        if (!this.isPatientLevelMode) {
            const _dm = htmlPath.match(/\/outputs\/([^/]+)\//);
            if (_dm) this.currentOutputDirName = _dm[1];
        }
        
        // ⭐ 更新右侧面板的按钮状态（从"正在生成..."变为可点击）
        const waitingActionsDiv = document.querySelector('.phase5-review-actions[data-waiting-report="true"]');
        if (waitingActionsDiv) {
            waitingActionsDiv.removeAttribute('data-waiting-report');
            const _e = (window.__lang||'zh')==='en';
            waitingActionsDiv.innerHTML = `
                <button class="btn-view-report-final">
                    <i class="fas fa-file-medical-alt"></i> ${_e ? 'View Full Report' : '查看完整报告'}
                </button>
                <button class="btn-export-report-final">
                    <i class="fas fa-download"></i> ${_e ? 'Export Report' : '导出报告'}
                </button>
            `;
            
            // 绑定按钮事件
            waitingActionsDiv.querySelector('.btn-view-report-final')?.addEventListener('click', () => {
                window.open(htmlPath, '_blank');
            });
            
            waitingActionsDiv.querySelector('.btn-export-report-final')?.addEventListener('click', () => {
                window.open(htmlPath, '_blank');
                this.showNotification((window.__lang||'zh')==='en' ? 'HTML report opened in new tab' : '已在新标签页打开HTML报告，可直接保存', 'success');
            });
            
            // 更新状态消息
            const confirmMsg = waitingActionsDiv.closest('.message');
            if (confirmMsg) {
                const messageText = confirmMsg.querySelector('.message-text');
                if (messageText) {
                    messageText.innerHTML = `
                        <p><strong>🎉 ${__t('report_title')}</strong></p>
                        <p>${__t('report_generated')}</p>
                        <p style="color: #4ade80; font-weight: 600;">✅ ${__t('report_approved')}</p>
                    `;
                }
            }
        }
        
        // 在左侧面板添加报告链接卡片
        const list = document.getElementById('routeStepsList');
        if (list) {
            const reportCard = document.createElement('div');
            reportCard.className = 'route-step-card report-card';
            reportCard.innerHTML = `
                <div class="route-step-header">
                    <span class="route-step-title">
                        <span class="step-icon">📄</span>
                        ${__t('pathology_report')}
                    </span>
                    <span class="route-step-badge completed">${__t('status_completed')}</span>
                </div>
                <div class="route-step-content">
                    <button class="view-report-btn" onclick="window.open('${htmlPath}', '_blank')">
                        🔍 ${__t('btn_view_report')}
                    </button>
                </div>
            `;
            list.appendChild(reportCard);
            
            // ⭐ 移除自动滚动 - 让用户自己控制滚动位置
            // setTimeout(() => {
            //     reportCard.scrollIntoView({ behavior: 'smooth', block: 'end' });
            // }, 100);
        }
        
        // 不再在聊天区域添加冗余消息（HTML报告链接已整合到完成消息中）
        
        this.showNotification((window.__lang||'zh')==='en' ? '📄 Report generated, click to view' : '📄 病理报告已生成，点击查看', 'success');
    }
    
    handleErrorEvent(data) {
        console.error('Agent错误:', data);

        const settlingRunId = data?.runtime?.run_id || this.runtimeRunId;
        const runtimeTracked = Boolean(settlingRunId);
        this.isRunning = runtimeTracked;
        this.isStarting = false;
        this.isStopping = false;
        this.primaryAgentState = 'error';
        if (!runtimeTracked) this._clearRuntimeRun();
        else this._settleRuntimeAfterLegacySignal(settlingRunId);
        this.updateButtons();
        this._finishChatActivity('error', data.message || '');
        this._clearPendingBlueAliases();
        this.subagentStates.forEach((state, id) => {
            if (state === 'running' || state === 'pending') this.subagentStates.set(id, 'error');
        });
        this._renderSubagentBoard();
        this.showNotification(((window.__lang||'zh')==='en' ? 'Agent error: ' : 'Agent错误: ') + data.message, 'error');
    }
    
    // ⭐ 新增：处理聊天消息事件
    handleMessageEvent(data) {
        if (data?.is_patient_level && !this._patientEventMatchesActiveRun(data)) return;
        // 先判断事件是否已经携带最终 ROI 结果。后端的历史兼容事件有时仍将
        // 最终结果标为 progress；若直接在 progress 分支返回，绿框正文就会丢失。
        const msgContent = String(data.content || '');
        const msgLength = msgContent.length;
        const roiId = data.roi_id || (data.metadata && data.metadata.roi_id) || null;
        const terminalStatus = String(
            data.status || (data.metadata && data.metadata.status) || ''
        ).toLowerCase();
        const terminalResultEvent = Boolean(
            data.is_result === true ||
            (data.metadata && data.metadata.is_result === true) ||
            ['completed', 'complete', 'done', 'success', 'succeeded'].includes(terminalStatus) ||
            msgContent.includes('分析结果') ||
            /analysis result/i.test(msgContent) ||
            (msgLength > 150 && roiId)
        );

        // 进度与视觉证据进入中间 Subagent 结果栏，右侧保持纯对话。
        if (data.type === 'progress' && !terminalResultEvent) {
            const progressRoiId = data.roi_id || data.metadata?.roi_id;
            if (progressRoiId && progressRoiId !== 'AI-Red-Selection') {
                // The backend emits this event immediately before submitting the
                // ROI API job, so it must enter the running count before return.
                this._setSubagentState(progressRoiId, 'running');
            }
            const progressText = data.content || ((window.__lang || 'zh') === 'en' ? 'Analysis in progress…' : '分析进行中…');
            const placedInTree = this._appendRoiHierarchyProgress(data, progressText);
            if (!placedInTree) {
                const progressWsi = this._patientSwarmMapWsiIndexFromEvent(data);
                this._appendActivityItem({
                    title: progressText,
                    state: 'running',
                    key: `message-progress:${Number.isInteger(progressWsi) ? progressWsi : ''}:${data.roi_id || ''}:${progressText}`,
                    wsiIndex: Number.isInteger(progressWsi) ? progressWsi : null
                });
            }
            return;
        }
        
        // ⭐ 判断消息类型（中英文双语关键词）
        // 进度消息：包含"开始分析"、"选取中"、"选取完成"、"筛选"等，只显示在左侧
        const isProgressMessage = !terminalResultEvent && (
                                  msgContent.includes('开始分析') ||
                                  msgContent.includes('Analyzing red box') ||
                                  msgContent.includes('Analyzing blue box') ||
                                  msgContent.includes('Analyzing green box') ||
                                  msgContent.includes('选取中') ||
                                  msgContent.includes('选取完成') ||
                                  msgContent.includes('selection complete') ||
                                  msgContent.includes('开始筛选') ||
                                  msgContent.includes('筛选绿框') ||
                                  msgContent.includes('筛选蓝框') ||
                                  (msgContent.includes('已选择') && msgContent.includes('个')) ||
                                  (msgContent.includes('regions selected') && msgContent.includes('selected')) ||
                                  msgContent.includes('Begin analysis') ||
                                  data.is_progress === true ||
                                  (data.metadata && data.metadata.is_progress === true)
                                  );
        
        // 开始分析消息
        const isStartMessage = (msgContent.includes('开始分析') || msgContent.includes('Begin analysis') ||
                                msgContent.includes('Analyzing red box') || msgContent.includes('Analyzing blue box') ||
                                msgContent.includes('Analyzing green box'))
                               && !msgContent.includes('分析结果');

        if (isStartMessage && roiId && roiId !== 'AI-Red-Selection') {
            this._setSubagentState(roiId, 'running');
        }
        
        // 分析结果消息：包含"分析结果"，内容较长
        const isResultMessage = terminalResultEvent;
        
        // ⭐ 确定框的级别（优先使用后端传来的level，否则根据消息内容判断）
        let level = data.level || 'red';
        if (!data.level) {
            if (msgContent.includes('🟢') || msgContent.includes('绿框')) {
                level = 'green';
            } else if (msgContent.includes('🔵') || msgContent.includes('蓝框')) {
                level = 'blue';
            } else if (msgContent.includes('🔴') || msgContent.includes('红框')) {
                level = 'red';
            } else if (roiId && roiId.includes('_sub_')) {
                level = roiId.split('_sub_').length > 2 ? 'green' : 'blue';
            }
        }
        
        // ⭐ Patient Level：捕获红框选择的全局 reasoning（AI 全局观察）
        // 后端在 agent.message 中发送 roi_id='AI-Red-Selection' + reasoning=全局分析
        // ⚠️ 单切片 Swarm 也会发同名消息！这里绝不能只凭消息名就强制启用
        // Patient Level 模式——否则单切片跑到红框选择完成那一刻就被误切进
        // 患者级，之后所有 viewport/蓝框/绿框绘制都被「切片匹配」过滤拦掉
        // （事件不带 file_id，匹配必然失败），表现为蓝绿框永远画不出来。
        // 只有存在真实患者级上下文（事件标记 / 患者切片列表非空）才允许翻转；
        // 真正的患者级运行本来就在 beginPatientLevelRun 里已置位，这里只是
        // 事件竞争时的兜底。
        const hasPatientContext = data.is_patient_level === true
            || (window.pathchat?.patientLevelUploadResults || []).length > 0;
        if (roiId === 'AI-Red-Selection' && data.reasoning && !this.isCollaborativeMode && hasPatientContext) {
            console.log(`📊 [Patient Level] 收到 AI-Red-Selection 消息，强制启用 Patient Level 模式`);
            
            // ⭐ 强制设置 Patient Level 模式（解决事件竞争）
            if (!this.isPatientLevelMode) {
                this.isPatientLevelMode = true;
                console.log('🔧 [handleMessageEvent] 强制启用 Patient Level 模式');
            }
            
            // ⭐ 推断 WSI index：并行时绝不能用“正在看的切片”或卡住的 analyzing=0。
            let wsiIndex = this._patientSwarmMapWsiIndexFromEvent(data);
            if (!Number.isInteger(wsiIndex)) wsiIndex = this.currentAnalyzingWsiIndex;
            if (wsiIndex === undefined || wsiIndex === null) {
                const expandedCard = document.querySelector('.pl-wsi-card.expanded');
                if (expandedCard) {
                    const body = expandedCard.querySelector('.pl-wsi-body');
                    if (body && body.dataset.index !== undefined) {
                        wsiIndex = parseInt(body.dataset.index) || 0;
                        this.currentAnalyzingWsiIndex = wsiIndex;
                    }
                }
                if (wsiIndex === undefined) {
                    wsiIndex = 0;
                    this.currentAnalyzingWsiIndex = 0;
                }
                console.log(`🔧 [handleMessageEvent] 推断 WSI index=${wsiIndex}`);
            }
            
            // ⭐ 初始化数据存储
            if (!this.patientLevelAnalysisData) {
                this.patientLevelAnalysisData = {};
            }
            if (!this.patientLevelAnalysisData[wsiIndex]) {
                this.patientLevelAnalysisData[wsiIndex] = { redBoxes: [] };
            }
            
            // ⭐ 保存全局分析理由
            this.patientLevelAnalysisData[wsiIndex].globalReasoning = data.reasoning;
            console.log(`📊 [Patient Level] 保存全局分析理由 (${data.reasoning.length} 字)`);
            
            // ⭐ 先展开卡片，再更新 UI
            if (window.pathchat && typeof window.pathchat.togglePlWsiExpand === 'function') {
                window.pathchat.togglePlWsiExpand(wsiIndex, true);
            }
            
            // ⭐ 延迟更新 UI（确保 DOM 就绪）
            const wsiData = this.patientLevelAnalysisData[wsiIndex];
            const doUpdateFromMessage = () => {
                if (window.pathchat && typeof window.pathchat.updatePlWsiAnalysisTree === 'function') {
                    window.pathchat.updatePlWsiAnalysisTree(wsiIndex, wsiData);
                    console.log(`📊 [handleMessageEvent] UI 更新成功 - globalReasoning`);
                }
            };
            
            // 延迟 50ms 确保 DOM 更新
            setTimeout(doUpdateFromMessage, 50);
        }
        
        // ⭐ 添加到左侧路径规划面板
        const levelName = level === 'red' ? '红框' : level === 'blue' ? '蓝框' : '绿框';
        const levelIcon = level === 'red' ? '🔴' : level === 'blue' ? '🔵' : '🟢';
        
        // ⭐ 新方案：进度消息（is_progress=true）只在左侧显示，不在右侧聊天框显示
        const isProgressOnly = data.is_progress === true || (data.metadata && data.metadata.is_progress === true);
        
        if (isProgressMessage || isProgressOnly) {
            const placedInTree = this._appendRoiHierarchyProgress({ ...data, level }, msgContent);
            if (!placedInTree) {
                const messageWsi = this._patientSwarmMapWsiIndexFromEvent(data);
                this._appendActivityItem({
                    title: msgContent,
                    state: /完成|complete|selected/i.test(msgContent) ? 'done' : 'running',
                    key: `message-progress:${Number.isInteger(messageWsi) ? messageWsi : ''}:${roiId || ''}:${msgContent}`,
                    wsiIndex: Number.isInteger(messageWsi) ? messageWsi : null
                });
            }
            if (data.reasoning && !placedInTree && roiId !== 'AI-Red-Selection') {
                this._appendChatThinking(
                    data.reasoning,
                    (window.__lang || 'zh') === 'en' ? 'Thumbnail observation and dispatch rationale' : '缩略图观察与派发依据'
                );
            }

            const selectionFinished = msgContent.includes('选取完成') || msgContent.includes('已选择') ||
                msgContent.includes('selection complete') || msgContent.includes('regions selected');
            if (selectionFinished) {
                const dispatchedRegions = Array.isArray(data.regions)
                    ? data.regions
                    : (Array.isArray(data.metadata?.regions) ? data.metadata.regions : []);
                const dispatchedIds = dispatchedRegions
                    .map(region => region?.id || region?.roi_id)
                    .filter(Boolean);
                if (dispatchedIds.length) {
                    const selectionState = (this.currentMode === 'collaborative' || this.isCollaborativeMode)
                        ? 'pending'
                        : 'running';
                    this._configureSubagentBoard(dispatchedIds, 0, selectionState);
                }
            }
            const keepAsResult = Boolean(
                data.metadata?.requires_confirmation ||
                (selectionFinished && this.currentMode === 'collaborative' && (level === 'blue' || level === 'green'))
            );
            if (!keepAsResult) return;

            // 提取标题（直接使用消息内容，去除emoji）
            let title = msgContent.replace(/[🔴🔵🟢✅]/g, '').trim();
            // 截断过长的标题
            if (title.length > 30) {
                title = title.substring(0, 30) + '...';
            }
            
            // ⭐ Bug1 修复：从消息中提取选中数量，数量为0时不应标记为completed
            let selectedCount = -1;  // -1表示未找到数量
            const countMatch = msgContent.match(/已选择\s*(\d+)\s*个/) || 
                              msgContent.match(/选中\s*(\d+)\s*个/) ||
                              msgContent.match(/(\d+)\s*regions?\s*selected/) ||
                              (data.metadata && data.metadata.count !== undefined ? { 1: data.metadata.count } : null);
            if (countMatch) {
                selectedCount = parseInt(countMatch[1], 10);
            }
            if (roiId === 'AI-Red-Selection' && selectedCount > 0) {
                const knownIds = Array.from(this.subagentStates.keys()).filter(id => !/^S\d+$/.test(id));
                const announced = this._announceThumbnailPlan(
                    data.reasoning,
                    knownIds,
                    selectedCount,
                    '',
                    this._thumbnailContextFromEvent(data)
                );
                if (announced || this.thumbnailPlanAnnounced) {
                    this._activateSubagentUi();
                    this._configureSubagentBoard(knownIds, selectedCount);
                }
            }
            
            // ⭐ 确定状态（协作模式下"选取完成"应为pending，等待用户确认）
            let status = 'waiting';
            const isSelectionComplete = msgContent.includes('选取完成') || msgContent.includes('已选择') ||
                                        msgContent.includes('selection complete') || msgContent.includes('regions selected');
            const isBlueOrGreen = level === 'blue' || level === 'green';
            const requiresConfirmation = data.metadata && data.metadata.requires_confirmation;
            
            // ⭐ 协作模式优化：蓝框/绿框选取完成后，只删除"选取中"卡片
            // 注意：需要保留"选取完成"卡片用于显示确认按钮！
            if (this.currentMode === 'collaborative' && isSelectionComplete) {
                const allCards = document.querySelectorAll('#routeStepsList .collab-task-card, #routeStepsList .route-step-card');
                allCards.forEach(card => {
                    const cardText = card.textContent || '';
                    // 只删除"选取中"卡片，保留"选取完成"用于确认
                    if (cardText.includes('选取中') || cardText.includes('Selecting')) {
                        console.log(`[Collab] 删除"选取中"临时状态卡片`);
                        card.remove();
                    }
                });
                // 继续创建"选取完成"卡片，状态为pending，供用户确认
                console.log(`[Collab] 创建"选取完成"卡片，等待用户确认`);
            }
            
            // ⭐ Bug1 修复：如果选中数量为0，显示特殊状态（跳过）
                if (selectedCount === 0) {
                status = 'skipped';  // 选中0个，标记为跳过
                title = title.replace('选取完成', '无满足条件的区域').replace('selection complete', 'No qualifying regions');
                console.log(`[Selection] 选中数量为0，标记为跳过: ${title}`);
            } else if (requiresConfirmation || (isSelectionComplete && this.currentMode === 'collaborative' && isBlueOrGreen)) {
                // ⭐ 需要用户确认，设为 pending
                status = 'pending';
                console.log(`[Collab] 选取完成，等待用户确认: ${title}`);
            } else if (isSelectionComplete || msgContent.includes('完成') || msgContent.includes('complete')) {
                // 自动模式或其他"完成"消息，直接设为完成
                status = 'completed';
            }
            
            // ⭐ Bug修复：只有当有有效roiId时才创建卡片，避免生成"红框 progress"等无效卡片
            if (roiId) {
                const newCard = this._withPatientSwarmMapContext(data, () => this.appendRouteStep({
                    level: level,
                    title: title,
                    roi_id: roiId,
                    rect: data.rect || (data.metadata && data.metadata.rect),
                    regions: data.regions || [],  // ⭐ Bug修复：传入蓝框/绿框的regions数据
                    confirmation_id: data.confirmation_id || null,  // ⭐ 新增：直接从消息获取confirmation_id
                    reasoning: data.reasoning || data.content || '',  // ⭐ AI思考过程
                    status: status,
                    wsiIndex: this._patientSwarmMapWsiIndexFromEvent(data)
                }));
            } else {
                console.log(`[Progress] 跳过无roiId的进度消息: ${title.substring(0, 30)}...`);
            }
        }
        
        if (isResultMessage && !isProgressMessage) {
            if (roiId && roiId !== 'AI-Red-Selection') {
                this._setSubagentState(roiId, 'done');
            }
            // ⭐ 协作模式优化：红框分析完成后，删除对应的"开始分析"卡片
            if (this.currentMode === 'collaborative' && level === 'red' && roiId) {
                // 查找并删除包含该roiId的"开始分析"卡片
                const startCards = document.querySelectorAll(`#routeStepsList [data-roi-id*="${roiId}"]`);
                startCards.forEach(card => {
                    const titleText = card.textContent || '';
                    // 只删除"开始分析"类型的卡片，保留其他卡片
                    if (titleText.includes('开始分析') || titleText.includes('正在分析')) {
                        console.log(`[Collab] 删除"开始分析"卡片: ${roiId}`);
                        card.remove();
                    }
                });
            }
            
            // ⭐ 使用 roiId 作为标题（R1, B2, G3 格式）
            let title = roiId ? `${roiId} ${__t('status_completed')}` : `${levelName} ${__t('status_completed')}`;
            
            // 提取完整解读，不要把 60 字摘要写进树节点
            const rectData = data.rect || (data.metadata && data.metadata.rect);
            const reasoningData = data.reasoning || data.content || msgContent || '';
            if (roiId) {
                this._withPatientSwarmMapContext(data, () => {
                    const existingCard = this._findMappedTreeCard(roiId)
                        || this._findTreeNodeByRoiId(
                            this._patientMapList(this._patientSwarmMapWsiIndexFromEvent(data))
                                || document.getElementById('routeStepsList'),
                            roiId,
                            '.route-tree-green-item[data-roi-id], .route-tree-blue-node[data-roi-id], .route-tree-node[data-roi-id], .route-step-card[data-roi-id], .collab-tree-node[data-roi-id]'
                        );
                    if (existingCard) {
                        // ⭐「✅ R8 分析完成 + 结构摘要」这类 is_result 消息没有 reasoning
                        // 字段，退回 content 时只是个占位摘要。agent.red_analysis 早已把
                        // 完整 10× 解读写进正文——已有正文更长时只更新状态徽标，不覆盖内容。
                        const bodyEl = existingCard.querySelector(
                            '.route-tree-content, .route-tree-blue-content, .route-tree-green-content, .route-step-content'
                        );
                        const existingText = String(bodyEl?.textContent || '').trim();
                        const wouldDowngrade = !data.reasoning
                            && existingText.length > String(reasoningData || '').trim().length;
                        this.updateRouteStepStatus(
                            roiId,
                            'completed',
                            wouldDowngrade ? null : reasoningData,
                            rectData,
                            wouldDowngrade ? null : reasoningData
                        );
                    } else {
                        this.appendRouteStep({
                            level: level,
                            title: title,
                            content: reasoningData,
                            reasoning: reasoningData,
                            roi_id: roiId,
                            rect: rectData,
                            status: 'completed',
                            wsiIndex: this._patientSwarmMapWsiIndexFromEvent(data)
                        });
                    }
                });
            }
        }
        
        // ⭐ 存储ROI分析数据
        if (roiId && msgContent && window.pathchat) {
            if (!window.pathchat.roiAnalysisData) {
                window.pathchat.roiAnalysisData = {};
            }
            
            const needStore = !window.pathchat.roiAnalysisData[roiId] || isResultMessage;
            if (needStore) {
                window.pathchat.roiAnalysisData[roiId] = {
                    // 优先存完整解读：msgContent 只是「🟢 Gx 分析结果 + findings 摘要」，
                    // done 时的批量补全和「引用解读到聊天」都读这里，存摘要会把
                    // 卡片上的完整正文降级覆盖。
                    gpt_analysis: data.reasoning || msgContent,
                    zoom_level: data.level === 'red' ? 10 : (data.level === 'blue' ? 40 : 160),
                    level_name: data.level === 'red' ? '宏观结构' : (data.level === 'blue' ? '病灶特征' : '细胞证据'),
                    parent_id: data.parent_id,
                    index: data.index
                };
            }
        }

        // 最终绿框消息到达时立即完成对应叶节点，并展示完整解读正文。
        if (isResultMessage && roiId) {
            const resultCard = this._findTreeNodeByRoiId(
                document.getElementById('routeStepsList'),
                roiId
            );
            if (resultCard && resultCard.classList.contains('route-tree-green-item')) {
                this._completeGreenTreeNode(
                    resultCard,
                    msgContent,
                    data.reasoning || msgContent
                );
            }
        }
        
        // ⭐ 如果是进度消息（开始分析、选取中、选取完成等），不发到右侧聊天框
        if (isProgressMessage) {
            return; // 不发到右侧聊天框，只在左侧显示
        }
        
        // ⭐ 结果消息统一在左侧显示，不发到右侧聊天框；
        // 但单切片 Swarm 在右侧补一行「结果已收到 (k/N)」小字状态。
        if (data.is_result === true) {
            this._appendRedResultReceivedStatus(data, level);
            return; // 结果消息只在左侧显示
        }
        
        // B/C 停止、回溯：右侧用与「解读完毕」相同的小字状态行，不再贴大段原因。
        if (this._appendLoopDecisionChatStatus(data, msgContent)) {
            return;
        }

        // 将消息添加到聊天框（只有真正的分析结果）
        // pathchat.addMessage(role, content, isTemporary, roiId)
        if (window.pathchat && typeof window.pathchat.addMessage === 'function') {
            const messageElement = window.pathchat.addMessage(data.type || 'assistant', data.content, false, roiId);
            window.pathchat.tagIncomingPatientSlideReport?.(messageElement, data);
            
            // Collaborative Phase 5 owns regenerate/finalize on the input dock.
            // Do not add a second 同意定稿 / 导出报告 pair inside the chat bubble.
            if (
                messageElement
                && data.metadata
                && data.metadata.needs_confirmation
                && !(this.currentMode === 'collaborative' || this.isCollaborativeMode)
            ) {
                this.addReportConfirmationButtons(messageElement, data.metadata.revised_report);
            }
            
            // ⭐ 给消息元素添加ROI ID，供点击框时定位
            if (roiId && messageElement) {
                messageElement.setAttribute('data-roi-id', roiId);
                
                // ⭐ 更准确地判断是否是分析结果消息
                const contentLength = data.content.length;
                const content = data.content;
                
                // 判断是否包含分析关键词（用于识别分析结果）
                const hasResultKeywords = content.includes('分析结果') || 
                                         content.includes('分析完成') ||
                                         content.includes('组织') ||
                                         content.includes('细胞') ||
                                         content.includes('病理') ||
                                         content.includes('特征') ||
                                         content.includes('结构');
                
                // ⭐ Bug2 修复：排除"开始分析"类消息，但不应该误判包含"分析结果"关键词的消息
                // 如果消息明确包含"分析结果"，即使有emoji也不是开始消息
                const isStartMessage = !content.includes('分析结果') && (
                                      content.includes('开始分析') || 
                                      content.includes('Begin analysis') ||
                                      (content.includes('🔴') && contentLength < 50 && !hasResultKeywords) ||
                                      (content.includes('🔵') && contentLength < 50 && !hasResultKeywords) ||
                                      (content.includes('🟢') && contentLength < 50 && !hasResultKeywords)
                                      );
                
                // ⭐ Bug2 修复：对于红框（10倍镜），包含"分析结果"关键词即可认定为结果消息
                const isRedFrameResult = roiId.includes('roi_10x_r') && 
                                        !roiId.includes('_sub_') && 
                                        (contentLength > 80 || content.includes('分析结果')) && 
                                        !isStartMessage;
                
                // 对于蓝框和绿框
                const isSubFrameResult = roiId.includes('_sub_') && 
                                        contentLength > 50 && 
                                        hasResultKeywords && 
                                        !isStartMessage;
                
                const isResult = isRedFrameResult || isSubFrameResult;
                
                if (isResult) {
                    // 移除旧消息的ID（如果有的话）
                    const oldMessage = document.getElementById(`message-${roiId}`);
                    if (oldMessage) {
                        oldMessage.removeAttribute('id');
                        console.log(`🔄 移除旧消息ID: message-${roiId}`);
                    }
                    // 给新消息设置ID
                    messageElement.id = `message-${roiId}`;
                    console.log(`✅ 设置新消息ID: message-${roiId} (内容长度: ${contentLength}, 是红框: ${isRedFrameResult}, 是子框: ${isSubFrameResult})`);
                } else if (!document.getElementById(`message-${roiId}`)) {
                    // 如果是较短的消息且还没有消息占用这个ID，暂时设置它
                    messageElement.id = `message-${roiId}`;
                    console.log(`📌 临时设置消息ID: message-${roiId} (等待更详细的分析结果, 长度: ${contentLength})`);
                } else {
                    console.log(`⏭️ 跳过设置ID: ${roiId} (可能是开始分析消息)`);
                }
                
                // ⭐ 添加点击事件：点击右侧消息 → 聚焦到对应ROI区域
                messageElement.style.cursor = 'pointer';
                messageElement.addEventListener('click', (e) => {
                    // 如果点击的是按钮，不触发跳转
                    if (e.target.closest('.result-action-btn')) return;
                    
                    // 确定level
                    let level = 'red';
                    if (roiId.includes('_sub_')) {
                        level = roiId.split('_sub_').length > 2 ? 'green' : 'blue';
                    }
                    
                    // 调用聚焦函数
                    this.focusOnRoiFromMeta({
                        level: level,
                        roi_id: roiId,
                        rect: data.rect
                    });
                    
                    // 同时高亮左侧对应的路径卡片
                    const routeCard = document.querySelector(`#routeStepsList [data-roi-id="${roiId}"]`);
                    if (routeCard) {
                        document.querySelectorAll('.route-step-card.active').forEach(c => c.classList.remove('active'));
                        routeCard.classList.add('active');
                        routeCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    }
                });
                
                // ⭐ 不再为中间分析结果添加确认按钮，只有最终报告才需要确认
                // 用户说：默认不编辑或重新生成就算同意
                // if (isResult && this.currentMode === 'collaborative') {
                //     this._addResultActionButtons(messageElement, roiId, data.rect, level);
                // }
            }
            
            // ⭐ 如果消息包含元数据（如报告路径、树结构等），添加点击事件处理
            if (data.metadata && messageElement) {
                this.attachMessageEventHandlers(messageElement, data.metadata);
            }
        } else {
            console.warn('pathchat.addMessage 方法不可用，尝试直接添加到DOM');
            this.addMessageToChatbox(data.content, data.type || 'assistant');
        }
    }
    
    // ⭐ 为消息中的链接附加事件处理
    attachMessageEventHandlers(messageElement, metadata) {
        // 处理所有链接
        const links = messageElement.querySelectorAll('a');
        links.forEach(link => {
            const href = link.getAttribute('href');
            
            if (href === '#tree') {
                // 查看树结构
                link.addEventListener('click', (e) => {
                    e.preventDefault();
                    this.showTreeStructure(metadata.tree_structure || []);
                });
            } else if (href === '#download') {
                // 下载报告
                link.addEventListener('click', (e) => {
                    e.preventDefault();
                    this.downloadReport(metadata.report_path, metadata.file_id);
                });
            } else if (href.startsWith('/outputs/')) {
                // 查看报告
                link.addEventListener('click', (e) => {
                    e.preventDefault();
                    this.viewReport(href, metadata.file_id);
                });
            }
        });
    }
    
    // ⭐ 为分析结果消息添加确认/编辑/重新生成按钮（仅协作模式）
    _addResultActionButtons(messageElement, roiId, rect, level) {
        const messageContent = messageElement.querySelector('.message-content');
        if (!messageContent) return;
        
        // 检查是否已添加按钮
        if (messageElement.querySelector('.result-action-buttons')) return;
        
        // 创建按钮容器
        const btnContainer = document.createElement('div');
        btnContainer.className = 'result-action-buttons';
        btnContainer.innerHTML = `
            <button class="result-action-btn result-btn-accept" title="确认此分析结果">
                <i class="fas fa-check"></i> 确认
            </button>
            <button class="result-action-btn result-btn-edit" title="手动编辑解读内容">
                <i class="fas fa-edit"></i> 编辑
            </button>
            <button class="result-action-btn result-btn-reject" title="重新生成解读">
                <i class="fas fa-redo"></i> 重新生成
            </button>
        `;
        
        // 添加到消息内容区域
        messageContent.appendChild(btnContainer);
        
        // 绑定确认按钮事件
        const acceptBtn = btnContainer.querySelector('.result-btn-accept');
        acceptBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this._handleResultAccept(messageElement, roiId);
        });
        
        // 绑定编辑按钮事件
        const editBtn = btnContainer.querySelector('.result-btn-edit');
        editBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this._handleResultEdit(messageElement, roiId, rect, level);
        });
        
        // 绑定重新生成按钮事件
        const rejectBtn = btnContainer.querySelector('.result-btn-reject');
        rejectBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this._handleResultReject(messageElement, roiId, rect, level);
        });
    }
    
    // ⭐ 处理确认分析结果（协作模式 - 发送确认到后端）
    _handleResultAccept(messageElement, roiId) {
        console.log(`✅ 确认分析结果: ${roiId}`);
        
        // 获取当前解读确认ID（如果有）
        const confirmationId = messageElement.dataset.analysisConfirmationId || this.currentAnalysisConfirmationId;
        
        // ⭐ 发送确认到后端（触发硬闸门释放）
        if (this.socket && confirmationId) {
            this.socket.emit('agent.confirm_analysis_result', {
                session_id: this.sessionId,
                confirmation_id: confirmationId,
                action: 'accept'
            });
            console.log(`📤 发送解读确认: ${confirmationId}`);
        }
        
        // 移除按钮，添加确认标记
        const btnContainer = messageElement.querySelector('.result-action-buttons');
        if (btnContainer) {
            btnContainer.innerHTML = `<span class="result-status result-accepted"><i class="fas fa-check-circle"></i> ${__t('status_confirmed')}</span>`;
        }
        
        // 更新左侧卡片状态
        const routeCard = document.querySelector(`#routeStepsList [data-roi-id="${roiId}"]`);
        if (routeCard) {
            routeCard.classList.add('result-confirmed');
        }
        
        // 清理当前确认ID
        this.currentAnalysisConfirmationId = null;
        
        this.showNotification((window.__lang||'zh')==='en' ? 'Analysis result confirmed' : '已确认该分析结果', 'success');
    }
    
    // ⭐ 处理编辑分析结果 - 手动修改解读内容（仅协作模式）
    _handleResultEdit(messageElement, roiId, rect, level) {
        console.log(`✏️ 进入编辑模式: ${roiId}`);
        
        const messageContent = messageElement.querySelector('.message-content');
        if (!messageContent) return;
        
        // 获取当前解读文本（排除按钮区域）
        const btnContainer = messageElement.querySelector('.result-action-buttons');
        let originalHtml = messageContent.innerHTML;
        if (btnContainer) {
            originalHtml = messageContent.innerHTML.replace(btnContainer.outerHTML, '');
        }
        
        // 提取纯文本内容用于编辑
        const tempDiv = document.createElement('div');
        tempDiv.innerHTML = originalHtml;
        const originalText = tempDiv.textContent.trim();
        
        const _isEn = (window.__lang || 'zh') === 'en';
        messageContent.innerHTML = `
            <div class="result-edit-container">
                <textarea class="result-edit-textarea" rows="6">${originalText}</textarea>
                <div class="result-edit-actions">
                    <button class="result-edit-btn result-edit-save" title="${_isEn ? 'Save' : '保存修改'}">
                        <i class="fas fa-save"></i> ${_isEn ? 'Save' : '保存'}
                    </button>
                    <button class="result-edit-btn result-edit-cancel" title="${_isEn ? 'Cancel' : '取消编辑'}">
                        <i class="fas fa-times"></i> ${_isEn ? 'Cancel' : '取消'}
                    </button>
                </div>
            </div>
        `;
        
        // 保存原始内容用于取消时恢复
        messageElement._originalHtml = originalHtml;
        messageElement._roiId = roiId;
        messageElement._rect = rect;
        messageElement._level = level;
        
        // 自动聚焦到文本框
        const textarea = messageContent.querySelector('.result-edit-textarea');
        textarea.focus();
        textarea.setSelectionRange(textarea.value.length, textarea.value.length);
        
        // 绑定保存按钮事件
        const saveBtn = messageContent.querySelector('.result-edit-save');
        saveBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this._saveResultEdit(messageElement);
        });
        
        // 绑定取消按钮事件
        const cancelBtn = messageContent.querySelector('.result-edit-cancel');
        cancelBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this._cancelResultEdit(messageElement);
        });
        
        this.showNotification((window.__lang||'zh')==='en' ? '📝 Edit mode — modify and click Save' : '📝 进入编辑模式，修改后点击保存', 'info');
    }
    
    // ⭐ 保存编辑后的分析结果（协作模式 - 发送编辑确认到后端）
    _saveResultEdit(messageElement) {
        const messageContent = messageElement.querySelector('.message-content');
        const textarea = messageContent.querySelector('.result-edit-textarea');
        const editedText = textarea.value.trim();
        
        if (!editedText) {
            this.showNotification((window.__lang||'zh')==='en' ? 'Content cannot be empty' : '解读内容不能为空', 'warning');
            return;
        }
        
        const roiId = messageElement._roiId;
        console.log(`💾 保存编辑后的解读: ${roiId}`);
        
        // 获取当前解读确认ID
        const confirmationId = messageElement.dataset.analysisConfirmationId || this.currentAnalysisConfirmationId;
        
        // ⭐ 发送编辑确认到后端（触发硬闸门释放）
        if (this.socket && confirmationId) {
            this.socket.emit('agent.confirm_analysis_result', {
                session_id: this.sessionId,
                confirmation_id: confirmationId,
                action: 'edit',
                edited_content: editedText
            });
            console.log(`📤 发送编辑确认: ${confirmationId}`);
        }
        
        // 恢复为正常显示，显示编辑后的内容
        messageContent.innerHTML = `
            <p>${editedText}</p>
            <div class="result-action-buttons">
                <span class="result-status result-edited"><i class="fas fa-edit"></i> 已编辑确认</span>
            </div>
        `;
        
        // 更新左侧卡片状态
        const routeCard = document.querySelector(`#routeStepsList [data-roi-id="${roiId}"]`);
        if (routeCard) {
            routeCard.classList.add('result-confirmed', 'result-edited');
        }
        
        // 清理临时数据和确认ID
        delete messageElement._originalHtml;
        delete messageElement._roiId;
        delete messageElement._rect;
        delete messageElement._level;
        this.currentAnalysisConfirmationId = null;
        
        this.showNotification((window.__lang||'zh')==='en' ? '✅ Edit saved' : '✅ 已保存编辑内容', 'success');
    }
    
    // ⭐ 取消编辑，恢复原内容
    _cancelResultEdit(messageElement) {
        const messageContent = messageElement.querySelector('.message-content');
        const originalHtml = messageElement._originalHtml;
        const roiId = messageElement._roiId;
        const rect = messageElement._rect;
        const level = messageElement._level;
        
        // 恢复原始内容
        messageContent.innerHTML = originalHtml;
        
        // 重新添加按钮
        this._addResultActionButtons(messageElement, roiId, rect, level);
        
        // 清理临时数据
        delete messageElement._originalHtml;
        delete messageElement._roiId;
        delete messageElement._rect;
        delete messageElement._level;
        
        this.showNotification(__t('cancelled'), 'info');
    }
    
    // ⭐ 处理不认可分析结果 - 重新分析（协作模式 - 发送重新生成请求）
    async _handleResultReject(messageElement, roiId, rect, level) {
        console.log(`❌ 不认可分析结果，准备重新分析: ${roiId}`);
        
        const reanalyzeMsg = (window.__lang||'zh')==='en' 
            ? 'Re-analyze this region? AI will re-interpret.'
            : '是否要重新分析该区域？这将请求AI重新解读。';
        if (!(await _copilotConfirm(reanalyzeMsg))) {
            return;
        }
        
        // 获取当前解读确认ID
        const confirmationId = messageElement.dataset.analysisConfirmationId || this.currentAnalysisConfirmationId;
        
        // 更新按钮状态为"分析中"
        const btnContainer = messageElement.querySelector('.result-action-buttons');
        if (btnContainer) {
            btnContainer.innerHTML = `<span class="result-status result-reanalyzing"><i class="fas fa-spinner fa-spin"></i> ${__t('observation_analyzing')}</span>`;
        }
        
        // ⭐ 发送重新生成请求到后端（触发硬闸门释放，返回regenerate状态）
        if (this.socket && confirmationId) {
            this.socket.emit('agent.confirm_analysis_result', {
                session_id: this.sessionId,
                confirmation_id: confirmationId,
                action: 'regenerate'
            });
            console.log(`📤 发送重新生成请求: ${confirmationId}`);
            this.currentAnalysisConfirmationId = null;
            this.showNotification((window.__lang||'zh')==='en' ? 'Re-analysis requested' : '已发送重新分析请求', 'info');
        } else if (this.socket) {
            // 兼容旧逻辑：没有确认ID时直接发送重新分析请求
            this.socket.emit('agent.reanalyze_roi', {
                session_id: this.sessionId,
                roi_id: roiId,
                rect: rect,
                level: level,
                reason: 'user_rejected'
            });
            this.showNotification((window.__lang||'zh')==='en' ? 'Re-analysis requested' : '已发送重新分析请求', 'info');
        } else {
            this.showNotification((window.__lang||'zh')==='en' ? 'Cannot connect to server' : '无法连接服务器', 'error');
            // 恢复按钮
            if (btnContainer) {
                const _e = (window.__lang||'zh')==='en';
                btnContainer.innerHTML = `
                    <button class="result-action-btn result-btn-accept" title="${_e ? 'Confirm result' : '确认此分析结果'}">
                        <i class="fas fa-check"></i> ${_e ? 'Confirm' : '确认'}
                    </button>
                    <button class="result-action-btn result-btn-edit" title="${_e ? 'Edit interpretation' : '手动编辑解读内容'}">
                        <i class="fas fa-edit"></i> ${_e ? 'Edit' : '编辑'}
                    </button>
                    <button class="result-action-btn result-btn-reject" title="${_e ? 'Regenerate' : '重新生成解读'}">
                        <i class="fas fa-redo"></i> ${_e ? 'Regenerate' : '重新生成'}
                    </button>
                `;
                // 重新绑定事件
                this._bindResultActionButtons(btnContainer, messageElement, roiId, rect, level);
            }
        }
    }
    
    // ⭐ 绑定结果操作按钮事件（辅助函数）
    _bindResultActionButtons(btnContainer, messageElement, roiId, rect, level) {
        const acceptBtn = btnContainer.querySelector('.result-btn-accept');
        if (acceptBtn) {
            acceptBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this._handleResultAccept(messageElement, roiId);
            });
        }
        
        const editBtn = btnContainer.querySelector('.result-btn-edit');
        if (editBtn) {
            editBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this._handleResultEdit(messageElement, roiId, rect, level);
            });
        }
        
        const rejectBtn = btnContainer.querySelector('.result-btn-reject');
        if (rejectBtn) {
            rejectBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this._handleResultReject(messageElement, roiId, rect, level);
            });
        }
    }
    
    // 显示树结构
    showTreeStructure(treeStructure) {
        console.log('显示树结构:', treeStructure);
        
        if (!treeStructure || treeStructure.length === 0) {
            alert('暂无树结构数据');
            return;
        }
        
        // 切换到推理路径标签页
        const treeTab = document.querySelector('[data-tab="reasoning"]') || 
                        document.querySelector('.tab[onclick*="reasoning"]');
        if (treeTab) {
            treeTab.click();
            console.log('已切换到推理路径标签页');
        } else {
            console.warn('未找到推理路径标签页');
        }
        
        // 如果有推理树可视化组件，尝试手动添加节点
        if (window.pathchat && window.pathchat.reasoningTree) {
            console.log('推理树组件已加载，节点数量:', treeStructure.length);
            
            // ⭐ 安全检查：确保 nodes 存在
            if (window.pathchat.reasoningTree.nodes && window.pathchat.reasoningTree.nodes.length === 0) {
                treeStructure.forEach(node => {
                    try {
                        window.pathchat.reasoningTree.addNode({
                            id: node.roi_id,
                            level: node.level,
                            magnification: node.zoom_level,
                            rect: node.analysis?.rect,
                            summary: node.analysis?.findings || __t('observation_analyzing')
                        });
                    } catch (error) {
                        console.error('添加树节点失败:', error);
                    }
                });
            }
        } else {
            console.warn('推理树可视化组件未加载');
            
            // 创建简单的树结构模态框
            this.showTreeModal(treeStructure);
        }
    }
    
    // 显示树结构模态框（后备方案）- 新版本，类似文件管理器
    showTreeModal(treeStructure) {
        // 创建全屏模态框
        const modal = document.createElement('div');
        modal.id = 'treeStructureModal';
        modal.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: white;
            z-index: 10000;
            display: flex;
            flex-direction: column;
        `;
        
        // 头部工具栏
        const header = document.createElement('div');
        header.style.cssText = `
            padding: 15px 20px;
            background: #2c3e50;
            color: white;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #34495e;
        `;
        header.innerHTML = `
            <div>
                <h2 style="margin: 0; font-size: 1.3em;">📊 ${__t('tree_modal_title')}</h2>
                <p style="margin: 5px 0 0 0; font-size: 0.9em; opacity: 0.8;">${__t('tree_modal_sub')}</p>
            </div>
            <button id="closeTreeModalBtn" style="padding: 8px 20px; background: #e74c3c; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 1em;">✕ ${__t('btn_close')}</button>
        `;
        
        // 主体内容区域 - 分成左右两栏
        const mainContent = document.createElement('div');
        mainContent.style.cssText = `
            flex: 1;
            display: flex;
            overflow: hidden;
        `;
        
        // 左侧：文件树列表
        const leftPanel = document.createElement('div');
        leftPanel.style.cssText = `
            width: 40%;
            border-right: 1px solid #ddd;
            overflow-y: auto;
            background: #f8f9fa;
            padding: 10px;
        `;
        
        // 右侧：详情预览
        const rightPanel = document.createElement('div');
        rightPanel.id = 'treeDetailPanel';
        rightPanel.style.cssText = `
            flex: 1;
            padding: 20px;
            overflow-y: auto;
            background: white;
        `;
        rightPanel.innerHTML = `<div style="text-align: center; color: #999; padding: 50px;">${(window.__lang||'zh')==='en' ? 'Click a node on the left to view details' : '点击左侧节点查看详情'}</div>`;
        
        // 生成文件树HTML
        leftPanel.innerHTML = this.generateFileTreeHTML(treeStructure);
        
        mainContent.appendChild(leftPanel);
        mainContent.appendChild(rightPanel);
        
        modal.appendChild(header);
        modal.appendChild(mainContent);
        document.body.appendChild(modal);
        
        // 绑定事件
        document.getElementById('closeTreeModalBtn').addEventListener('click', () => {
            document.body.removeChild(modal);
        });
        
        // 绑定树节点点击事件
        this.attachTreeNodeClickEvents(treeStructure);
    }
    
    // 生成文件树HTML
    generateFileTreeHTML(treeStructure) {
        let html = '<div class="file-tree">';
        
        // 根节点
        html += `
            <div class="tree-node root-node" style="margin-bottom: 10px;">
                <div class="node-header" style="padding: 8px; background: #3498db; color: white; border-radius: 4px; cursor: pointer; font-weight: bold;">
                    📁 WSI_${new Date().getTime().toString().slice(-8)}
                </div>
            </div>
        `;
        
        // 红框节点
        treeStructure.forEach((redNode, rIdx) => {
            const redId = `red_${rIdx}`;
            html += `
                <div class="tree-node red-node" style="margin-left: 20px; margin-bottom: 10px;">
                    <div class="node-header" data-node-id="${redId}" data-node-data='${JSON.stringify(redNode).replace(/'/g, "&apos;")}' 
                         style="padding: 8px; background: #ff6b6b; color: white; border-radius: 4px; cursor: pointer; display: flex; justify-content: space-between; align-items: center;">
                        <span>
                            <span class="toggle" style="margin-right: 5px;">▼</span>
                            🔴 10_${redNode.roi_id || `red_${rIdx+1}`}_10x
                        </span>
                        <span style="font-size: 0.85em; opacity: 0.9;">关键路径</span>
                    </div>
                    <div class="node-children" style="margin-left: 20px;">
            `;
            
            // 蓝框节点
            if (redNode.children && redNode.children.length > 0) {
                redNode.children.forEach((blueNode, bIdx) => {
                    const blueId = `blue_${rIdx}_${bIdx}`;
                    html += `
                        <div class="tree-node blue-node" style="margin-bottom: 8px; margin-top: 8px;">
                            <div class="node-header" data-node-id="${blueId}" data-node-data='${JSON.stringify(blueNode).replace(/'/g, "&apos;")}'
                                 style="padding: 6px 8px; background: #4dabf7; color: white; border-radius: 4px; cursor: pointer; display: flex; justify-content: space-between; align-items: center;">
                                <span>
                                    <span class="toggle" style="margin-right: 5px;">▼</span>
                                    🔵 40_${blueNode.roi_id || `blue_${bIdx+1}`}_40x
                                </span>
                                <span style="font-size: 0.8em; opacity: 0.9;">${blueNode.children?.length || 0} 子节点</span>
                            </div>
                            <div class="node-children" style="margin-left: 20px;">
                    `;
                    
                    // 绿框节点
                    if (blueNode.children && blueNode.children.length > 0) {
                        blueNode.children.forEach((greenNode, gIdx) => {
                            const greenId = `green_${rIdx}_${bIdx}_${gIdx}`;
                            html += `
                                <div class="tree-node green-node" style="margin-bottom: 5px; margin-top: 5px;">
                                    <div class="node-header" data-node-id="${greenId}" data-node-data='${JSON.stringify(greenNode).replace(/'/g, "&apos;")}'
                                         style="padding: 5px 8px; background: #51cf66; color: white; border-radius: 4px; cursor: pointer; font-size: 0.9em;">
                                        🟢 160_${greenNode.roi_id || `green_${gIdx+1}`}_160x
                                    </div>
                                </div>
                            `;
                        });
                    }
                    
                    html += `
                            </div>
                        </div>
                    `;
                });
            }
            
            html += `
                    </div>
                </div>
            `;
        });
        
        html += '</div>';
        return html;
    }
    
    // 绑定树节点点击事件
    attachTreeNodeClickEvents(treeStructure) {
        const nodeHeaders = document.querySelectorAll('.node-header[data-node-id]');
        
        nodeHeaders.forEach(header => {
            header.addEventListener('click', (e) => {
                e.stopPropagation();
                
                const nodeData = JSON.parse(header.getAttribute('data-node-data'));
                const nodeId = header.getAttribute('data-node-id');
                
                // 高亮当前节点
                document.querySelectorAll('.node-header').forEach(h => {
                    h.style.boxShadow = 'none';
                });
                header.style.boxShadow = '0 0 10px rgba(0,0,0,0.3)';
                
                // 显示详情
                this.showNodeDetail(nodeData, nodeId);
                
                // 切换子节点显示
                const children = header.nextElementSibling;
                const toggle = header.querySelector('.toggle');
                if (children && toggle) {
                    if (children.style.display === 'none') {
                        children.style.display = 'block';
                        toggle.textContent = '▼';
                    } else {
                        children.style.display = 'none';
                        toggle.textContent = '▶';
                    }
                }
            });
        });
    }
    
    // 显示节点详情
    showNodeDetail(nodeData, nodeId) {
        const detailPanel = document.getElementById('treeDetailPanel');
        if (!detailPanel) return;
        
        const level = nodeData.level || 'unknown';
        const zoom = nodeData.zoom_level || 'N/A';
        const findings = nodeData.analysis?.findings || '-';
        const rect = nodeData.analysis?.rect || {};
        const imageUrl = nodeData.analysis?.image_path || '';
        
        let levelColor = '#666';
        let levelName = __t('level_unknown');
        if (level === 'red' || zoom === 10) {
            levelColor = '#ff6b6b';
            levelName = __t('level_red');
        } else if (level === 'blue' || zoom === 40) {
            levelColor = '#4dabf7';
            levelName = __t('level_blue');
        } else if (level === 'green' || Number(zoom) >= 160) {
            levelColor = '#51cf66';
            levelName = __t('level_green');
        }
        
        detailPanel.innerHTML = `
            <div style="border-left: 4px solid ${levelColor}; padding-left: 15px;">
                <h3 style="color: ${levelColor}; margin-top: 0;">${levelName}</h3>
                <p style="color: #666; margin: 5px 0;"><strong>${__t('node_id_label')}:</strong> ${nodeData.roi_id || nodeId}</p>
                <p style="color: #666; margin: 5px 0;"><strong>${__t('magnification_label')}:</strong> ${zoom}×</p>
                ${rect.w ? `<p style="color: #666; margin: 5px 0;"><strong>${__t('region_size_label')}:</strong> ${rect.w} × ${rect.h} px</p>` : ''}
                ${rect.x !== undefined ? `<p style="color: #666; margin: 5px 0;"><strong>${__t('position_label')}:</strong> (${rect.x}, ${rect.y})</p>` : ''}
            </div>
            
            <div style="margin-top: 20px; padding: 15px; background: #f8f9fa; border-radius: 8px; border-left: 3px solid ${levelColor};">
                <h4 style="margin-top: 0; color: #333;">📋 ${__t('findings_title')}</h4>
                <p style="line-height: 1.6; color: #555; white-space: pre-wrap;">${findings}</p>
            </div>
            
            ${imageUrl ? `
                <div style="margin-top: 20px;">
                    <h4 style="color: #333;">🔬 ${__t('region_preview')}</h4>
                    <img src="${imageUrl}" style="max-width: 100%; border-radius: 8px; border: 1px solid #ddd;" alt="ROI">
                </div>
            ` : ''}
            
            ${nodeData.children && nodeData.children.length > 0 ? `
                <div style="margin-top: 20px;">
                    <h4 style="color: #333;">🌳 ${__t('child_stats')}</h4>
                    <p style="color: #666;">${__t('child_count', nodeData.children.length)}</p>
                </div>
            ` : ''}
        `;
    }
    
    // 查看报告 — 直接在新标签页打开 HTML 报告
    viewReport(reportPath, fileId) {
        // 将 .txt / .md 路径替换为对应的 HTML 报告路径
        const htmlPath = reportPath
            .replace(/pathology_report\.(txt|md)$/, 'pathology_report.html')
            .replace(/\/auto\/pathology_report\.html$/, '/auto/pathology_report.html');
        window.open(htmlPath, '_blank');
    }
    
    // 下载报告
    downloadReport(reportPath, fileId) {
        console.log('下载报告:', reportPath);
        
        if (!reportPath) {
            alert('报告路径不可用');
            return;
        }
        
        // 创建下载链接
        const link = document.createElement('a');
        link.href = reportPath;
        link.download = `pathology_report_${fileId}.md`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    }
    
    // 直接添加消息到聊天框（后备方案）
    addMessageToChatbox(content, type) {
        const chatbox = document.getElementById('chatMessages') || document.querySelector('.chat-messages');
        if (!chatbox) {
            console.warn('未找到聊天框元素');
            return;
        }
        
        const messageDiv = document.createElement('div');
        messageDiv.className = `message message-${type}`;
        
        const contentDiv = document.createElement('div');
        contentDiv.className = 'message-content';
        contentDiv.innerHTML = this.parseMarkdown(content);
        
        messageDiv.appendChild(contentDiv);
        chatbox.appendChild(messageDiv);
        
        // 滚动到底部
        chatbox.scrollTop = chatbox.scrollHeight;
    }
    
    // 简单的Markdown解析
    parseMarkdown(text) {
        return text
            .replace(/^## (.+)$/gm, '<h2>$1</h2>')
            .replace(/^### (.+)$/gm, '<h3>$1</h3>')
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/\n/g, '<br>');
    }
    
    // 屏幕跟随实现
    followToPosition(centerPx, zoomLevel) {
        if (!this.viewer) return;
        
        try {
            const imageSize = this.viewer.world.getItemAt(0).getContentSize();
            const normalizedX = centerPx.x / imageSize.x;
            const normalizedY = centerPx.y / imageSize.y;
            
            // 计算目标缩放级别
            const baseZoom = this.viewer.viewport.getHomeZoom();
            const targetZoom = baseZoom * (zoomLevel / 10);
            
            console.log('跟随到位置:', { centerPx, zoomLevel, targetZoom });
            
            // 平滑移动
            this.viewer.viewport.panTo(
                new OpenSeadragon.Point(normalizedX, normalizedY),
                true // 启用动画
            );
            
            this.viewer.viewport.zoomTo(targetZoom, null, true);
            
        } catch (error) {
            console.error('跟随位置失败:', error);
        }
    }
    
    updateOverlays(overlays) {
        if (!this.viewer) return;
        
        // 清除现有overlays
        this.viewer.clearOverlays();
        
        // 添加新overlays
        overlays.forEach(overlay => {
            this.addOverlay(overlay);
        });
        
        this.currentOverlays = overlays;
    }
    
    addOverlay(overlay) {
        try {
            const rect = overlay.rect;
            const imageSize = this.viewer.world.getItemAt(0).getContentSize();
            
            // 转换为归一化坐标
            const x = rect.x / imageSize.x;
            const y = rect.y / imageSize.y;
            const width = rect.w / imageSize.x;
            const height = rect.h / imageSize.y;
            
            // 创建overlay元素
            const overlayElement = document.createElement('div');
            overlayElement.className = 'agent-overlay';
            
            // ⭐ 添加data-roi-id属性，用于点击事件识别
            if (overlay.roi_id) {
                overlayElement.dataset.roiId = overlay.roi_id;
                overlayElement.dataset.rect = JSON.stringify(rect);
            }
            
            overlayElement.innerHTML = `
                <div class="overlay-border"></div>
                <div class="overlay-label">
                    ${overlay.label}
                    <span class="overlay-confidence">${Math.round(overlay.confidence * 100)}%</span>
                </div>
            `;
            
            // ⭐ 设置可点击样式
            overlayElement.style.cursor = 'pointer';
            
            // 添加到viewer
            this.viewer.addOverlay({
                element: overlayElement,
                location: new OpenSeadragon.Rect(x, y, width, height),
                placement: OpenSeadragon.Placement.TOP_LEFT
            });
            
        } catch (error) {
            console.error('添加overlay失败:', error);
        }
    }
    
    // 时间线管理
    addToTimeline(traceData) {
        const timeline = document.getElementById('agentTimeline');
        if (!timeline) return;

        // Some trace packets are control events (for example the thumbnail
        // selection packet) and intentionally omit tools/results. They must
        // not abort the rest of handleTraceEvent before the chat announcement.
        const toolsCalled = Array.isArray(traceData?.tools_called) ? traceData.tools_called : [];
        const results = traceData?.results && typeof traceData.results === 'object'
            ? traceData.results
            : {};
        const observation = String(traceData?.observation || '').trim();
        const decision = String(traceData?.decision || '').trim();
        const confidence = Number(traceData?.confidence);
        if (!observation && !decision && !toolsCalled.length && !Object.keys(results).length && !Number.isFinite(confidence)) {
            return;
        }
        
        // 如果是第一个项目，清空占位内容
        const emptyState = timeline.querySelector('.timeline-empty');
        if (emptyState) {
            emptyState.remove();
        }
        
        const traceItem = document.createElement('div');
        traceItem.className = 'trace-item';
        traceItem.dataset.step = traceData.step;
        
        traceItem.innerHTML = `
            <div class="trace-header">
                <span class="trace-step">步骤 ${traceData.step}</span>
                <span class="trace-confidence">${Number.isFinite(confidence) ? `${Math.round(confidence * 100)}%` : ''}</span>
            </div>
            <div class="trace-observation">${observation}</div>
            <div class="trace-tools">
                分析工具: ${toolsCalled.join(', ')}
            </div>
            <div class="trace-results">
                ${Object.entries(results).map(([key, value]) => 
                    `<span class="result-item">${key}: ${typeof value === 'number' ? value.toFixed(2) : value}</span>`
                ).join('')}
            </div>
            <div class="trace-decision">${decision}</div>
        `;
        
        timeline.appendChild(traceItem);
        
        // 滚动到最新项
        traceItem.scrollIntoView({ behavior: 'smooth' });
    }
    
    clearTimeline() {
        const timeline = document.getElementById('agentTimeline');
        if (timeline) {
            timeline.innerHTML = `
                <div class="timeline-empty">
                    <i class="fas fa-robot"></i>
                    <p>${__t('observation_analyzing')}</p>
                </div>
            `;
        }
        this.traceData = [];
    }
    
    updateProgress(data) {
        const progressBar = document.getElementById('agentProgressBar');
        const progressText = document.getElementById('agentProgressText');
        
        if (progressBar) {
            progressBar.style.width = `${data.percentage}%`;
        }
        
        if (progressText) {
            const step = Number(data.step);
            const total = Number(data.total_steps);
            const hasCounter = Number.isFinite(step) && Number.isFinite(total) && total > 0;
            progressText.textContent = hasCounter
                ? `${data.message} (${step}/${total})`
                : String(data.message || '');
        }
    }
    
    showNotification(message, type = 'info') {
        console.log(`[${type.toUpperCase()}] ${message}`);
        
        if (window.pathchat && typeof window.pathchat.showNotification === 'function') {
            // pathchat.showNotification 签名是 (type, message)
            window.pathchat.showNotification(type, message);
        } else {
            if (type === 'error') {
                alert(((window.__lang||'zh')==='en' ? 'Error: ' : '错误: ') + message);
            }
        }
    }

    // ==================== 模式选择弹窗 ====================
    
    showModeSelectionModal() {
        const modal = document.getElementById('modeSelectionModal');
        if (modal) {
            modal.classList.remove('hidden');
            
            // 绑定模式卡片点击事件
            const modeCards = modal.querySelectorAll('.mode-card');
            modeCards.forEach(card => {
                card.addEventListener('click', () => {
                    const mode = card.dataset.mode;
                    this.selectMode(mode);
                    modal.classList.add('hidden');
                });
            });
        }
    }
    
    selectMode(mode) {
        this.currentMode = mode;
        this.isCollaborativeMode = (mode === 'collaborative');
        const _e = (window.__lang||'zh')==='en';
        const modeName = mode === 'collaborative' ? (_e ? 'Collaborative' : '人机协作') : (_e ? 'Auto AI' : '纯AI');
        console.log(`Mode: ${modeName}, isCollaborativeMode=${this.isCollaborativeMode}`);
        this.showNotification(_e ? `${modeName} mode selected` : `已选择${modeName}模式`, 'success');
        
        // 显示当前模式指示器
        this.showModeIndicator(mode);
    }
    
    showModeIndicator(mode) {
        // 不再显示顶部模式指示器（已删除）
        console.log(`当前模式: ${mode === 'collaborative' ? '人机协作' : '纯AI'}`);
    }
    
    // ==================== 人机协作确认对话框（硬闸门模式） ====================
    
    bindConfirmationEvents() {
        // ⭐ 保留原有底部栏按钮事件（兼容备用）
        const btnAccept = document.getElementById('btnConfirmAccept');
        const btnOverride = document.getElementById('btnConfirmOverride');
        const btnSkip = document.getElementById('btnConfirmSkip');
        const btnAuto = document.getElementById('btnConfirmAuto');
        
        // 手动选择相关按钮
        const btnBarDone = document.getElementById('btnBarDone');
        const btnBarCancel = document.getElementById('btnBarCancel');
        
        // ✅ 接受AI选择
        if (btnAccept) {
            btnAccept.addEventListener('click', () => {
                console.log('用户接受AI选择');
                this.sendConfirmResult('accept', this.selectedRegions);
            });
        }
        
        // ✏️ 使用当前调整后的选择
        if (btnOverride) {
            btnOverride.addEventListener('click', () => {
                console.log('用户确认调整后的选择');
                const regions = this.manualSelections.length > 0 
                    ? this.manualSelections 
                    : this.selectedRegions;
                this.sendConfirmResult('override', regions);
            });
        }
        
        // ⏩ 跳过该区域
        if (btnSkip) {
            btnSkip.addEventListener('click', () => {
                console.log('用户跳过当前区域');
                this.sendConfirmResult('skip', []);
            });
        }
        
        // 🔄 切换为全自动模式
        if (btnAuto) {
            btnAuto.addEventListener('click', () => {
                console.log('用户切换为全自动模式');
                this.sendConfirmResult('switch_to_auto', this.selectedRegions).then((result) => {
                    if (result?.status !== 'success') return;
                    this.currentMode = 'ai';
                    this.showNotification((window.__lang||'zh')==='en'
                        ? '🤖 Switched to full auto mode, no further confirmations'
                        : '🤖 已切换为全自动模式，后续不再询问确认', 'info');
                });
            });
        }
        
        // 手动选择完成
        if (btnBarDone) {
            btnBarDone.addEventListener('click', () => this.finishManualSelection());
        }
        
        // 取消手动选择
        if (btnBarCancel) {
            btnBarCancel.addEventListener('click', () => this.cancelManualSelection());
        }
        
        // ⭐ 新增：PathCopilot 弹窗式确认对话框事件
        this.bindModalConfirmationEvents();
        this.bindDraftReportModalEvents();
    }
    
    // ⭐ 绑定弹窗确认对话框事件
    bindModalConfirmationEvents() {
        const modal = document.getElementById('copilotConfirmModal');
        const overlay = document.getElementById('copilotConfirmOverlay');
        const closeBtn = document.getElementById('confirmModalClose');
        const btnAccept = document.getElementById('btnModalAccept');
        const btnEdit = document.getElementById('btnModalEdit');
        const btnSkip = document.getElementById('btnModalSkip');
        
        // 点击接受按钮
        if (btnAccept) {
            btnAccept.addEventListener('click', () => {
                console.log('[Modal] 用户点击接受');
                this.handleModalAction('accept');
            });
        }
        
        // 点击手动调整按钮
        if (btnEdit) {
            btnEdit.addEventListener('click', () => {
                console.log('[Modal] 用户点击手动调整');
                this.handleModalAction('edit');
            });
        }
        
        // 点击跳过按钮
        if (btnSkip) {
            btnSkip.addEventListener('click', () => {
                console.log('[Modal] 用户点击跳过');
                this.handleModalAction('skip');
            });
        }
        
        // Closing the dialog never approves a clinical workflow decision.
        if (closeBtn) {
            closeBtn.addEventListener('click', () => {
                console.log('[Modal] 用户关闭弹窗，保留待审批状态');
                this.hideConfirmModal({ preserveDecision: true });
            });
        }
        
        // Clicking the backdrop only dismisses the visual dialog.
        if (overlay) {
            overlay.addEventListener('click', () => {
                console.log('[Modal] 用户点击遮罩，保留待审批状态');
                this.hideConfirmModal({ preserveDecision: true });
            });
        }
        
        // Escape dismisses without approval. Enter/Space retain the browser's
        // native focused-button semantics, so Edit/Skip cannot become Accept.
        this._confirmModalKeyHandler = (e) => {
            if (!modal || modal.style.display === 'none') return;
            if (e.key === 'Escape') {
                e.preventDefault();
                console.log('[Modal] ESC关闭弹窗，保留待审批状态');
                this.hideConfirmModal({ preserveDecision: true });
                return;
            }
            if (e.key === 'Tab') {
                const focusable = Array.from(modal.querySelectorAll(
                    'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
                )).filter((node) => node.offsetParent !== null);
                if (!focusable.length) return;
                const first = focusable[0];
                const last = focusable[focusable.length - 1];
                if (e.shiftKey && document.activeElement === first) {
                    e.preventDefault();
                    last.focus();
                } else if (!e.shiftKey && document.activeElement === last) {
                    e.preventDefault();
                    first.focus();
                }
            }
        };
        document.addEventListener('keydown', this._confirmModalKeyHandler);
    }

    bindDraftReportModalEvents() {
        this._bindDraftReportDockEvents();
        const modal = document.getElementById('draftReportModal');
        if (!modal || modal.dataset.bound === 'true') return;
        modal.dataset.bound = 'true';
        document.getElementById('draftReportClose')?.addEventListener('click', () => {
            this.hideDraftReportModal({ preserveDecision: true });
        });
        document.getElementById('draftReportOverlay')?.addEventListener('click', () => {
            this.hideDraftReportModal({ preserveDecision: true });
        });
        document.getElementById('draftReportFinalize')?.addEventListener('click', () => {
            this.finalizeDraftReport();
        });
        document.getElementById('draftReportAddRoi')?.addEventListener('click', () => {
            this._regenerateDraftReport();
        });
        this._draftReportKeyHandler = (event) => {
            if (!modal || modal.style.display === 'none') return;
            if (event.key === 'Escape') {
                event.preventDefault();
                this.hideDraftReportModal({ preserveDecision: true });
            }
        };
        document.addEventListener('keydown', this._draftReportKeyHandler);
    }

    _normalizeRoiCitationId(value) {
        const match = String(value || '').trim().match(/^\[?R+(\d+(?:-B\d+)?(?:-G\d+)?)\]?$/i);
        return match ? `R${match[1]}` : String(value || '').trim();
    }

    _citedRoiIdsFromText(text) {
        const ids = [];
        String(text || '').replace(/\bR+\d+(?:-B\d+)?(?:-G\d+)?\b/g, (raw) => {
            const id = this._normalizeRoiCitationId(raw);
            if (id && !ids.includes(id)) ids.push(id);
            return raw;
        });
        return ids;
    }

    _linkifyRoiCitations(html, reportText) {
        const ids = this._citedRoiIdsFromText(reportText);
        if (!ids.length) return { html, ids };
        const matchPattern = ids
            .slice()
            .sort((a, b) => b.length - a.length)
            .map((id) => id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/^R/, 'R+'))
            .join('|');
        const tokens = [];
        let source = String(html || '').replace(/`+(\[?R+\d+(?:-B\d+)?(?:-G\d+)?\]?)`+/g, '$1');
        source = source.replace(new RegExp(`\\[?(?:${matchPattern})\\]?(?!-[BG\\d])`, 'g'), (raw) => {
            const id = this._normalizeRoiCitationId(String(raw).replace(/^\[|\]$/g, ''));
            if (!ids.includes(id)) return raw;
            const safe = this._escapeHtmlContent(id);
            const index = tokens.length;
            tokens.push(`<button type="button" class="draft-roi-cite" data-roi-id="${safe}" title="${safe}">[${safe}]</button>`);
            return `\u0000ROI${index}\u0000`;
        });
        const linked = tokens.reduce(
            (htmlOut, token, index) => htmlOut.replace(`\u0000ROI${index}\u0000`, token),
            source
        );
        return { html: linked, ids };
    }

    _normalizeQuotedRoiText(text) {
        return String(text || '').replace(/\bR+(\d+(?:-B\d+)?(?:-G\d+)?)\b/g, (_, rest) => `R${rest}`);
    }

    _formatDraftReportHtml(text) {
        const escaped = this._escapeHtmlContent(String(text || ''));
        const decorate = (value) => String(value || '').replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        const lines = escaped.split(/\r?\n/);
        const html = [];
        let listItems = [];
        const flushList = () => {
            if (!listItems.length) return;
            html.push(`<ul class="draft-report-list">${listItems.join('')}</ul>`);
            listItems = [];
        };
        lines.forEach((line) => {
            const heading = line.match(/^(#{1,4})\s+(.+)$/);
            const bullet = line.match(/^[-•]\s+(.+)$/);
            if (heading) {
                flushList();
                const level = Math.min(heading[1].length + 2, 5);
                html.push(`<h${level}>${decorate(heading[2])}</h${level}>`);
                return;
            }
            if (bullet) {
                listItems.push(`<li>${decorate(bullet[1])}</li>`);
                return;
            }
            flushList();
            if (line === '---') {
                html.push('<hr>');
                return;
            }
            if (!line.trim()) {
                html.push('<div class="draft-report-gap"></div>');
                return;
            }
            html.push(`<p>${decorate(line)}</p>`);
        });
        flushList();
        return html.join('');
    }

    _lookupRoiRect(roiId) {
        return this._resolveRoiRect(roiId)
            || (this.collaborativeReviewQueue || []).find(
                (item) => String(item?.roi_id) === String(roiId)
            )?.rect
            || null;
    }

    _phaseFromRoiId(roiId) {
        const id = String(roiId || '');
        if (/-G\d+/i.test(id)) return 4;
        if (/-B\d+/i.test(id)) return 3;
        if (/^R\d+$/i.test(id)) return 2;
        return 5;
    }

    _focusReportCitedRoi(roiId) {
        if (!roiId) return;
        this._activateSubagentUi();
        this.resumeAnalysisLayout?.();
        this.viewer = this.viewer || window.pathchat?.viewer || null;
        const rect = this._lookupRoiRect(roiId);
        if (window.pathchat?.jumpToROIView && rect) {
            window.pathchat.jumpToROIView({ id: roiId, rect });
        } else if (rect && this.viewer?.viewport) {
            try {
                const viewportRect = this.viewer.viewport.imageToViewportRectangle(
                    rect.x, rect.y, rect.w, rect.h
                );
                this.viewer.viewport.fitBounds(viewportRect, true);
            } catch (_) {}
        } else {
            this._jumpToRoamOverlay(roiId);
        }
        window.pathchat?.highlightROI?.(roiId);
        this.highlightLeftPanelCard(roiId, { force: true });
    }

    _citeDraftRoiToChat(roiId) {
        const id = String(roiId || '').trim();
        if (!id) return;
        this._focusReportCitedRoi(id);
        this.quoteAnalysisToChat(id, this._phaseFromRoiId(id));
    }

    _draftReviewKey(reviewOrCard = null) {
        if (!reviewOrCard) return '';
        const fileId = String(
            reviewOrCard.fileId
            || reviewOrCard.file_id
            || reviewOrCard.dataset?.fileId
            || reviewOrCard.confirmMsg?.dataset?.fileId
            || ''
        ).trim();
        if (fileId) return fileId;
        const wsi = this._asPatientWsiIndex(
            reviewOrCard.wsiIndex
            ?? reviewOrCard.dataset?.wsiIndex
            ?? reviewOrCard.confirmMsg?.dataset?.wsiIndex
        );
        return wsi == null ? '' : `__wsi:${wsi}`;
    }

    _storeDraftReview(review) {
        if (!this._draftReportReviews) this._draftReportReviews = new Map();
        if (!review) return review;
        const key = this._draftReviewKey(review) || '__default';
        if (!review.fileId && key && !key.startsWith('__')) review.fileId = key;
        this._draftReportReviews.set(key, review);
        return review;
    }

    _reviewForCard(card) {
        if (!card) return null;
        const key = this._draftReviewKey(card);
        if (key && this._draftReportReviews?.has(key)) return this._draftReportReviews.get(key);
        for (const review of this._draftReportReviews?.values?.() || []) {
            if (review?.confirmMsg === card) return review;
        }
        return null;
    }

    _isDraftCardPending(card) {
        if (!card) return false;
        if (card.classList.contains('is-resolved')) return false;
        const state = String(card.dataset.confirmState || '');
        if (['confirmed', 'modified', 'submitting', 'expired'].includes(state)) return false;
        return card.classList.contains('draft-report-chat-message')
            || Boolean(card.querySelector('textarea.draft-report-editor, article.draft-report-view'));
    }

    _pendingDraftCardForWsi(index) {
        const wsi = this._asPatientWsiIndex(index);
        if (wsi == null) return null;
        return Array.from(document.querySelectorAll(
            `.phase-confirm-message[data-phase="5"][data-wsi-index="${wsi}"]`
        )).find((card) => this._isDraftCardPending(card)) || null;
    }

    _draftTextFromCard(card) {
        const fromEditor = String(card?.querySelector?.('textarea.draft-report-editor')?.value || '').trim();
        if (fromEditor) return fromEditor;
        return String(card?.querySelector?.('article.draft-report-view')?.innerText || '').trim();
    }

    _adoptPendingDraftReviewsFromDom() {
        if (!this._draftReportReviews) this._draftReportReviews = new Map();
        document.querySelectorAll('.phase-confirm-message[data-phase="5"]').forEach((card) => {
            if (!this._isDraftCardPending(card)) return;
            const existing = this._reviewForCard(card);
            if (existing) {
                if (!existing.confirmMsg) existing.confirmMsg = card;
                if (!String(existing.current || '').trim()) {
                    existing.current = this._draftTextFromCard(card);
                    existing.original = existing.original || existing.current;
                }
                return;
            }
            const current = this._draftTextFromCard(card);
            if (!current && !card.classList.contains('draft-report-chat-message')) return;
            this._storeDraftReview({
                original: current,
                current,
                findings: [],
                excludedIds: [],
                events: [],
                confirmMsg: card,
                editing: false,
                fileId: String(card.dataset.fileId || ''),
            });
        });
    }

    _clearDraftReview(review, { preserveDecision = false } = {}) {
        if (!review || preserveDecision) return;
        const key = this._draftReviewKey(review);
        if (key) this._draftReportReviews?.delete(key);
        else {
            for (const [storedKey, value] of this._draftReportReviews || []) {
                if (value === review) this._draftReportReviews.delete(storedKey);
            }
        }
        if (this._draftReportReview === review) this._draftReportReview = null;
    }

    _patientReviewNeedsDoctor(el) {
        const card = el?.classList?.contains('patient-slide-review-card')
            ? el
            : el?.querySelector?.('.patient-slide-review-card');
        if (!card) return false;
        return [
            'status-pending_review',
            'status-revised_pending_confirmation',
            'status-revising',
            'status-revision_failed',
        ].some((cls) => card.classList.contains(cls));
    }

    _hasPendingPatientSlideReview(exceptIndex = null) {
        const skip = this._asPatientWsiIndex(exceptIndex);
        return [...document.querySelectorAll('.patient-review-chat-wrap')].some((wrap) => {
            if (!this._patientReviewNeedsDoctor(wrap)) return false;
            if (skip == null) return true;
            const idx = this._asPatientWsiIndex(
                wrap.dataset.wsiIndex || wrap.dataset.patientReviewIndex
            );
            return idx !== skip;
        });
    }

    _syncPatientChatGatesVisibility(preferredIndex = null) {
        if (!this.isPatientLevelMode) return;
        const want = this._asPatientWsiIndex(preferredIndex)
            ?? this._asPatientWsiIndex(this.currentViewingWsiIndex);
        if (want == null) return;
        document.querySelectorAll(
            '.patient-review-chat-wrap, .phase-confirm-message[data-wsi-index], .wsi-analysis-msg[data-wsi-index]'
        ).forEach((el) => {
            const idx = this._asPatientWsiIndex(el.dataset.wsiIndex || el.dataset.patientReviewIndex);
            if (idx == null) return;
            const keepReview = el.classList.contains('patient-review-chat-wrap')
                && this._patientReviewNeedsDoctor(el);
            const match = idx === want || keepReview;
            el.hidden = !match;
            el.classList.toggle('is-active-slide', match);
            el.classList.toggle('is-other-slide', !match);
            if (match) el.removeAttribute('inert');
            else el.setAttribute('inert', '');
        });
    }

    _activateDraftReviewForWsi(index) {
        this._adoptPendingDraftReviewsFromDom();
        const wsi = this._asPatientWsiIndex(index)
            ?? this._asPatientWsiIndex(this.currentViewingWsiIndex);
        const card = this._pendingDraftCardForWsi(wsi);
        if (card) {
            const current = this._draftTextFromCard(card);
            const review = this._reviewForCard(card) || {
                original: current,
                current,
                findings: [],
                excludedIds: [],
                events: [],
                confirmMsg: card,
                editing: false,
                fileId: String(card.dataset.fileId || ''),
            };
            review.confirmMsg = card;
            if (!String(review.current || '').trim()) review.current = current;
            this._storeDraftReview(review);
            this._draftReportReview = review;
        } else if (
            this._draftReportReview
            && this._asPatientWsiIndex(this._draftReportReview.confirmMsg?.dataset?.wsiIndex) !== wsi
        ) {
            this._draftReportReview = null;
        }
        this._syncPatientChatGatesVisibility(wsi);
        this._syncDraftReportDock();
        window.pathchat?.syncPatientSlideReportNav?.();
        return this._draftReportReview;
    }

    _currentDraftEditor() {
        return this._draftReportReview?.confirmMsg?.querySelector('textarea.draft-report-editor')
            || this._pendingDraftCardForWsi(this.currentViewingWsiIndex)
                ?.querySelector('textarea.draft-report-editor')
            || document.getElementById('draftReportEditor');
    }

    _currentDraftFeedback() {
        return this._draftReportReview?.confirmMsg?.querySelector('textarea.draft-report-feedback')
            || this._pendingDraftCardForWsi(this.currentViewingWsiIndex)
                ?.querySelector('textarea.draft-report-feedback')
            || document.getElementById('draftReportFeedback');
    }

    _currentDraftFindingsBox() {
        return this._draftReportReview?.confirmMsg?.querySelector('.draft-report-findings')
            || this._pendingDraftCardForWsi(this.currentViewingWsiIndex)
                ?.querySelector('.draft-report-findings')
            || document.getElementById('draftReportFindings');
    }

    _localizeDraftReportModal() {
        const setText = (id, value) => {
            const node = document.getElementById(id);
            if (node) node.textContent = value;
        };
        setText('draftReportKicker', __t('draft_report_kicker'));
        setText('draftReportTitle', __t('draft_report_title'));
        setText('draftReportEditorLabel', __t('draft_report_editor'));
        setText('draftReportFindingsTitle', __t('draft_report_findings'));
        setText('draftReportFeedbackLabel', __t('draft_report_feedback'));
        setText('draftReportHint', __t('draft_report_hint'));
        setText('draftReportAddRoiLabel', __t('draft_report_regenerate'));
        setText('draftReportDockRegenerateLabel', __t('draft_report_regenerate'));
        setText('draftReportDockFinalizeLabel', __t('draft_report_finalize'));
        setText('draftReportFinalizeLabel', __t('draft_report_finalize'));
        const feedback = document.getElementById('draftReportFeedback');
        if (feedback) feedback.placeholder = __t('draft_report_feedback_ph');
        document.getElementById('draftReportClose')?.setAttribute(
            'title',
            (window.__lang || 'zh') === 'en'
                ? 'Close without finalizing'
                : '关闭弹窗，不批准定稿'
        );
    }

    _renderDraftReportFindings(box = null, review = null) {
        const target = box || this._currentDraftFindingsBox();
        if (!target) return;
        const data = review || this._draftReportReview || {};
        const findings = data.findings || [];
        const excluded = new Set(data.excludedIds || []);
        if (!findings.length) {
            target.innerHTML = `<p>${(window.__lang || 'zh') === 'en'
                ? 'No ROI findings were attached to this draft.'
                : '本拟报告未附带可引用的 ROI finding。'}</p>`;
            return;
        }
        target.innerHTML = findings.map((item, index) => {
            const roiId = String(item.roi_id || '');
            const checked = excluded.has(roiId) ? 'checked' : '';
            const excludedClass = excluded.has(roiId) ? ' is-excluded' : '';
            const { html: citeHtml } = this._linkifyRoiCitations(
                this._escapeHtmlContent(roiId),
                roiId
            );
            return `<div class="draft-report-finding${excludedClass}">
                <input type="checkbox" data-finding-index="${index}" ${checked}
                       aria-label="${this._escapeHtmlContent((window.__lang || 'zh') === 'en'
                           ? `Exclude ${roiId}`
                           : `拒绝 ${roiId}`)}">
                <span class="draft-report-finding-body">${citeHtml}
                ${this._escapeHtmlContent(this._normalizeQuotedRoiText(String(item.summary || '')).slice(0, 180))}</span>
            </div>`;
        }).join('');
    }

    _bindDraftReportCardEvents(card) {
        if (!card || card.dataset.draftBound === 'true') return;
        card.dataset.draftBound = 'true';
        card.addEventListener('click', (event) => {
            const cite = event.target.closest('.draft-roi-cite');
            if (cite) {
                event.preventDefault();
                event.stopPropagation();
                this._citeDraftRoiToChat(cite.dataset.roiId);
                return;
            }
            const button = event.target.closest('[data-draft-action]');
            if (!button || button.disabled) return;
            const action = button.dataset.draftAction;
            if (action === 'finalize') this.finalizeDraftReport();
            else if (action === 'regenerate' || action === 'add_roi') this._regenerateDraftReport();
        });
        card.addEventListener('change', (event) => {
            const input = event.target.closest?.('.draft-report-finding input[type="checkbox"]');
            if (!input) return;
            input.closest('.draft-report-finding')?.classList.toggle('is-excluded', input.checked);
        });
    }

    _renderDraftReportCard(card, review = null) {
        if (!card) return;
        const data = review || this._reviewForCard(card) || this._draftReportReview || {};
        const report = String(data.current || '');
        const linked = this._linkifyRoiCitations(this._formatDraftReportHtml(report), report);
        const editorValue = this._escapeHtmlContent(report);
        card.classList.add('draft-report-chat-message');
        card.innerHTML = `
            <div class="message-avatar"><i class="fas fa-file-medical-alt"></i></div>
            <div class="message-content draft-report-chat">
                <div class="message-text">
                    <p><strong>${this._escapeHtmlContent(__t('draft_report_title'))}</strong></p>
                    <p class="draft-report-hint">${this._escapeHtmlContent(__t('draft_report_cite_hint'))}</p>
                    <p class="draft-report-hint">${this._escapeHtmlContent(__t('draft_report_chat_hint'))}</p>
                    <article class="draft-report-view">${linked.html}</article>
                    <details class="draft-report-edit-source">
                        <summary>${this._escapeHtmlContent(__t('draft_report_editor'))}</summary>
                        <textarea class="draft-report-editor" rows="8">${editorValue}</textarea>
                    </details>
                    <details class="draft-report-hafl">
                        <summary>${this._escapeHtmlContent(__t('draft_report_findings'))}</summary>
                        <div class="draft-report-findings"></div>
                    </details>
                </div>
            </div>
        `;
        this._renderDraftReportFindings(card.querySelector('.draft-report-findings'), data);
        const editor = card.querySelector('textarea.draft-report-editor');
        if (editor) {
            editor.addEventListener('input', () => {
                const target = this._reviewForCard(card) || data;
                if (target) target.current = editor.value;
            });
        }
        this._bindDraftReportCardEvents(card);
        this._syncDraftReportDock();
    }

    showDraftReportModal(options = {}) {
        if (!(this.currentMode === 'collaborative' || this.isCollaborativeMode)) {
            this.hideDraftReportModal({ all: true });
            return;
        }
        const confirmMsg = options.confirmMsg || null;
        const fileId = String(
            options.file_id
            || confirmMsg?.dataset?.fileId
            || this.currentFileId
            || this.runtimeRunFileId
            || this.getCurrentFileId()
            || ''
        );
        const existing = (fileId && this._draftReportReviews?.get(fileId))
            || this._reviewForCard(confirmMsg)
            || null;
        const report = String(
            options.report
            ?? existing?.current
            ?? this.currentPhaseData?.report
            ?? ''
        );
        const findings = Array.isArray(options.findings)
            ? options.findings
            : (this.currentPhaseData?.reportable_findings || existing?.findings || []);
        const review = this._storeDraftReview({
            original: existing?.original || report,
            current: report,
            findings,
            excludedIds: existing?.excludedIds || [],
            events: existing?.events || [],
            confirmMsg: confirmMsg || existing?.confirmMsg || null,
            editing: Boolean(options.keepEditing && existing?.editing),
            fileId,
        });
        const modal = document.getElementById('draftReportModal');
        if (modal) {
            modal.style.display = 'none';
            modal.hidden = true;
            modal.setAttribute('inert', '');
            modal.setAttribute('aria-hidden', 'true');
            modal.setAttribute('aria-modal', 'false');
        }
        const card = review.confirmMsg
            || (this.isPatientLevelMode
                ? this._pendingDraftCardForWsi(this.currentViewingWsiIndex)
                : document.querySelector('.phase-confirm-message[data-phase="5"]:not(.is-resolved)'));
        if (card) {
            review.confirmMsg = card;
            this._storeDraftReview(review);
            this._renderDraftReportCard(card, review);
        }
        const viewing = this._asPatientWsiIndex(this.currentViewingWsiIndex);
        const cardWsi = this._asPatientWsiIndex(card?.dataset?.wsiIndex);
        if (!this.isPatientLevelMode || viewing == null || cardWsi == null || cardWsi === viewing) {
            this._draftReportReview = review;
        }
        this._syncPatientChatGatesVisibility();
        this._syncDraftReportDock();
    }

    hideDraftReportModal({ preserveDecision = false, confirmMsg = null, all = false } = {}) {
        const modal = document.getElementById('draftReportModal');
        if (modal) {
            modal.style.display = 'none';
            modal.hidden = true;
            modal.setAttribute('inert', '');
            modal.setAttribute('aria-hidden', 'true');
            modal.setAttribute('aria-modal', 'false');
            // Hidden modal actions must not stay in the tab/a11y tree.
            // A leftover 同意定稿 here swallows clicks meant for the dock.
            modal.querySelectorAll('button').forEach((button) => {
                button.disabled = true;
                button.setAttribute('tabindex', '-1');
            });
        }
        if (all) {
            if (!preserveDecision) {
                this._draftReportReviews = new Map();
                this._draftReportReview = null;
            }
            this._syncDraftReportDock();
            return;
        }
        const target = confirmMsg
            ? (this._reviewForCard(confirmMsg) || this._draftReportReview)
            : this._draftReportReview;
        this._clearDraftReview(target, { preserveDecision });
        if (this.isPatientLevelMode) {
            this._activateDraftReviewForWsi(this.currentViewingWsiIndex);
            return;
        }
        this._syncDraftReportDock();
    }

    _selectedDraftFindings() {
        const findings = this._draftReportReview?.findings || [];
        const root = this._currentDraftFindingsBox();
        if (!root) return [];
        return Array.from(root.querySelectorAll('input[type="checkbox"]:checked'))
            .map((input) => findings[Number(input.dataset.findingIndex)])
            .filter(Boolean);
    }

    _recordDraftIntervention(event) {
        if (!this._draftReportReview) return;
        this._draftReportReview.events.push({
            ...event,
            at: new Date().toISOString(),
        });
    }

    _setDraftReportBusy(busy) {
        const roots = [
            this._draftReportReview?.confirmMsg,
            document.getElementById('draftReportModal'),
        ].filter(Boolean);
        roots.forEach((root) => {
            root.querySelectorAll('.draft-report-actions button, .phase5-review-actions button, #draftReportDock button').forEach((button) => {
                button.disabled = Boolean(busy);
            });
        });
        document.querySelectorAll('#draftReportDock button').forEach((button) => {
            button.disabled = Boolean(busy);
        });
    }

    _bindDraftReportDockEvents() {
        const dock = document.getElementById('draftReportDock');
        if (!dock || dock.dataset.bound === 'true') return;
        dock.dataset.bound = 'true';
        document.getElementById('draftReportDockRegenerate')?.addEventListener('click', () => {
            this._regenerateDraftReport();
        });
        document.getElementById('draftReportDockFinalize')?.addEventListener('click', () => {
            this.finalizeDraftReport();
        });
    }

    _syncDraftReportDock() {
        const dock = document.getElementById('draftReportDock');
        if (!dock) return;
        const collaborative = this.currentMode === 'collaborative' || this.isCollaborativeMode;
        const review = this._draftReportReview;
        const active = Boolean(
            collaborative
            && review
            && String(review.current || review.original || '').trim()
            && (!review.confirmMsg || this._isDraftCardPending(review.confirmMsg))
        );
        dock.hidden = !active;
        this._applyDraftChatPlaceholder(active);
        this.updateButtons();
        if (this.subagentUiActivated) this._renderSubagentBoard();
    }

    _applyDraftChatPlaceholder(active) {
        const input = document.getElementById('chatInput');
        if (!input || this.currentQuote) return;
        if (active) {
            input.placeholder = __t('draft_report_chat_ph');
            return;
        }
        const isEn = (window.__lang || 'zh') === 'en';
        input.placeholder = isEn
            ? "Describe what you want to analyze, or send 'Analyze selected region'..."
            : '请描述您想要分析的问题，或直接发送分析选中区域...';
    }

    _flushPendingDraftChatInput() {
        const input = window.pathchat?.elements?.chatInput || document.getElementById('chatInput');
        const text = String(input?.value || '').trim();
        if (!text) return '';
        window.pathchat?.addMessage?.('user', text);
        if (input) input.value = '';
        window.pathchat?.updateSendButton?.();
        return text;
    }

    _collectDraftChatFeedback() {
        const history = Array.isArray(window.pathchat?.chatHistory) ? window.pathchat.chatHistory : [];
        const skip = /正在思考|正在根据|Updating the draft|Regenerating the draft|Could not update|未能更新拟报告|重新生成拟报告/;
        const lines = [];
        history.forEach((item) => {
            const text = String(item?.content || '').trim();
            if (!text || skip.test(text)) return;
            const role = item.role === 'user'
                ? ((window.__lang || 'zh') === 'en' ? 'Pathologist' : '医生')
                : ((window.__lang || 'zh') === 'en' ? 'Assistant' : '助手');
            if (item.role === 'user' || item.role === 'assistant') {
                lines.push(`${role}: ${text}`);
            }
        });
        return lines.slice(-24).join('\n');
    }

    _buildRegenerateInstruction(chatFeedback, rois = []) {
        const isEn = (window.__lang || 'zh') === 'en';
        const ids = (rois || []).map((item) => item.id).filter(Boolean);
        const parts = [];
        if (chatFeedback) {
            parts.push(
                isEn
                    ? `Conversation while reviewing the draft:\n${chatFeedback}`
                    : `拟报告确认阶段的对话反馈：\n${chatFeedback}`
            );
        }
        if (ids.length) {
            const pathNote = typeof window.pathchat?._formatUserRoiContext === 'function'
                ? window.pathchat._formatUserRoiContext(rois, { isEn })
                : '';
            parts.push(
                isEn
                    ? `Also incorporate new ROI ${ids.join(', ')} shown in the attached images.`
                    : `同时把附图中的新增 ROI ${ids.join('、')} 融合进拟报告。`
            );
            if (pathNote) parts.push(pathNote);
        }
        if (!parts.length) {
            return isEn
                ? 'Please regenerate the complete draft from the current text. Do not invent findings.'
                : '请根据当前拟报告正文重新生成完整拟报告，不要虚构发现。';
        }
        parts.push(
            isEn
                ? 'Regenerate the complete draft from this conversation. Preserve still-supported evidence and do not invent findings.'
                : '请根据上述对话重新生成完整拟报告：保留仍成立的证据，不要虚构发现。'
        );
        return parts.join('\n\n');
    }

    reviseDraftFromSelectedRois(userMessage, rois = [], options = {}) {
        const items = (Array.isArray(rois) ? rois : []).filter((item) => item?.imageData);
        if (!items.length) {
            window.pathchat?.toggleClickPlaceRoiMode?.(true);
            this.showNotification(__t('draft_report_need_roi'), 'info');
            return false;
        }
        if (!this.socket?.connected) {
            this.showNotification(
                (window.__lang || 'zh') === 'en'
                    ? 'Approval could not be submitted. Check the connection and try again.'
                    : '审批尚未提交，请检查连接后重试。',
                'error'
            );
            return false;
        }
        const editor = this._currentDraftEditor();
        const current = String(editor?.value || this._draftReportReview?.current || '').trim();
        if (!current) {
            this.showNotification(
                (window.__lang || 'zh') === 'en'
                    ? 'No draft report to update yet.'
                    : '当前还没有可更新的拟报告。',
                'warning'
            );
            return false;
        }
        const ids = items.map((item) => item.id).filter(Boolean);
        const excluded = this._selectedDraftFindings();
        const pathNote = typeof window.pathchat?._formatUserRoiContext === 'function'
            ? window.pathchat._formatUserRoiContext(items)
            : '';
        let instruction = String(userMessage || '').trim()
            || ((window.__lang || 'zh') === 'en'
                ? `Please regenerate the draft using new ROI ${ids.join(', ')}.`
                : `请根据新增 ROI ${ids.join('、')} 的最新形态学解读重新生成拟报告。`);
        if (pathNote && !instruction.includes('所属分析路径') && !instruction.includes('Analysis path:')) {
            instruction += `\n\n${pathNote}`;
        }
        if (excluded.length) {
            const excludedIds = excluded.map((item) => item.roi_id).filter(Boolean).join('、');
            instruction += (window.__lang || 'zh') === 'en'
                ? `\nAlso exclude these findings: ${excludedIds}.`
                : `\n同时排除这些 Finding：${excludedIds}。`;
        }
        const images = items.map((item) => {
            const raw = String(item.imageData || '');
            return raw.startsWith('data:image/') ? raw : `data:image/jpeg;base64,${raw}`;
        });
        this._recordDraftIntervention({
            type: 'additional_roi',
            text: instruction,
            roi_ids: ids,
        });
        this._setDraftReportBusy(true);
        this.showNotification(__t('draft_report_revising'), 'info');
        this._pendingRevisionChatMessage = options.thinkingMessage || null;
        this.socket.emit('agent.revise_report', {
            session_id: this.sessionId,
            phase: 5,
            original_report: current,
            user_instruction: instruction,
            revision_kind: 'additional_roi',
            excluded_findings: excluded,
            roi_ids: ids,
            image_data: images,
            timestamp: new Date().toISOString(),
        });
        return true;
    }

    _regenerateDraftReport() {
        const editor = this._currentDraftEditor();
        if (editor && this._draftReportReview) {
            this._draftReportReview.current = editor.value;
        }
        const rois = (window.pathchat?.selectedUserRois || []).filter((item) => item?.imageData);
        const excluded = this._selectedDraftFindings();
        this._flushPendingDraftChatInput();
        const boxFeedback = (this._currentDraftFeedback()?.value || this._draftReportReview?.feedback || '').trim();
        const chatFeedback = [this._collectDraftChatFeedback(), boxFeedback].filter(Boolean).join('\n\n');
        const instruction = this._buildRegenerateInstruction(chatFeedback, rois);
        const thinking = window.pathchat?.addMessage?.(
            'assistant',
            (window.__lang || 'zh') === 'en'
                ? 'Regenerating the draft from the conversation…'
                : '正在根据对话反馈重新生成拟报告…',
            true
        ) || null;
        this._pendingRevisionChatMessage = thinking;
        if (rois.length) {
            const started = this.reviseDraftFromSelectedRois(instruction, rois, {
                thinkingMessage: thinking,
            });
            if (!started && thinking && window.pathchat?.updateMessage) {
                window.pathchat.updateMessage(
                    thinking,
                    (window.__lang || 'zh') === 'en'
                        ? 'Could not regenerate the draft. Check the connection and try again.'
                        : '未能重新生成拟报告，请检查连接后重试。'
                );
            }
            return;
        }
        if (window.pathchat?.currentScreenshot?.imageData) {
            if (this._draftReportReview) this._draftReportReview.feedback = instruction;
            const fb = this._currentDraftFeedback();
            if (fb) fb.value = instruction;
            this._reviseDraftReport('additional_roi');
            return;
        }
        if (excluded.length) {
            if (this._draftReportReview) this._draftReportReview.feedback = instruction;
            this._reviseDraftReport('finding_exclusion');
            return;
        }
        if (this._draftReportReview) this._draftReportReview.feedback = instruction;
        const fb = this._currentDraftFeedback();
        if (fb) fb.value = instruction;
        this._reviseDraftReport('feedback');
    }

    _reviseDraftReport(kind) {
        if (!this.socket?.connected) {
            this.showNotification(
                (window.__lang || 'zh') === 'en'
                    ? 'Approval could not be submitted. Check the connection and try again.'
                    : '审批尚未提交，请检查连接后重试。',
                'error'
            );
            return;
        }
        const editor = this._currentDraftEditor();
        const current = editor?.value || this._draftReportReview?.current || '';
        const feedback = (this._currentDraftFeedback()?.value || this._draftReportReview?.feedback || '').trim();
        let instruction = feedback;
        let excluded = [];
        let imageData = '';
        if (kind === 'finding_exclusion') {
            excluded = this._selectedDraftFindings();
            if (!excluded.length) {
                this.showNotification(__t('draft_report_no_finding'), 'warning');
                return;
            }
            instruction = instruction || excluded.map((item) => item.roi_id).join(', ');
            this._draftReportReview.excludedIds = Array.from(new Set([
                ...(this._draftReportReview.excludedIds || []),
                ...excluded.map((item) => String(item.roi_id || '')),
            ]));
            this._renderDraftReportFindings();
            this._recordDraftIntervention({
                type: 'finding_exclusion',
                roi_ids: excluded.map((item) => item.roi_id),
            });
        } else if (kind === 'additional_roi') {
            const rois = (window.pathchat?.selectedUserRois || []).filter((item) => item?.imageData);
            const rawImage = window.pathchat?.currentScreenshot?.imageData || '';
            if (!rois.length && !rawImage) {
                window.pathchat?.toggleClickPlaceRoiMode?.(true);
                this.showNotification(__t('draft_report_need_roi'), 'info');
                return;
            }
            const ids = rois.map((item) => item.id).filter(Boolean).join('、');
            instruction = instruction || (
                (window.__lang || 'zh') === 'en'
                    ? `Please update the draft using new ROI ${ids || 'selection'}.`
                    : `请根据新增 ROI ${ids || '选区'} 的最新形态学解读更新拟报告。`
            );
            if (rois.length) {
                this.reviseDraftFromSelectedRois(instruction, rois);
                return;
            }
            imageData = String(rawImage).startsWith('data:image/')
                ? rawImage
                : `data:image/jpeg;base64,${rawImage}`;
            this._recordDraftIntervention({ type: 'additional_roi', text: instruction });
        } else {
            if (!feedback) {
                this.showNotification(__t('draft_report_need_feedback'), 'warning');
                return;
            }
            this._recordDraftIntervention({ type: 'feedback', text: feedback });
        }
        this._setDraftReportBusy(true);
        this.showNotification(__t('draft_report_revising'), 'info');
        this.socket.emit('agent.revise_report', {
            session_id: this.sessionId,
            phase: 5,
            original_report: current,
            user_instruction: instruction,
            revision_kind: kind,
            excluded_findings: excluded,
            image_data: imageData,
            timestamp: new Date().toISOString(),
        });
    }

    finalizeDraftReport() {
        if (this.isPatientLevelMode) {
            this._activateDraftReviewForWsi(this.currentViewingWsiIndex);
        }
        const review = this._draftReportReview;
        const editor = this._currentDraftEditor();
        const current = String(editor?.value || review?.current || '');
        const original = String(review?.original || '');
        const events = [...(review?.events || [])];
        if (current.trim() !== original.trim()
            && !events.some((item) => item.type === 'revised_interpretation')) {
            events.push({ type: 'revised_interpretation', at: new Date().toISOString() });
        }
        const confirmMsg = review?.confirmMsg
            || (this.isPatientLevelMode
                ? this._pendingDraftCardForWsi(this.currentViewingWsiIndex)
                : document.querySelector('.phase-confirm-message[data-phase="5"]:not(.is-resolved)'));
        if (!confirmMsg) {
            this.showNotification(
                (window.__lang || 'zh') === 'en'
                    ? 'No draft is waiting on this slide.'
                    : '当前切片没有待定稿的拟报告。',
                'warning'
            );
            return;
        }
        const viewing = this._asPatientWsiIndex(this.currentViewingWsiIndex);
        const cardWsi = this._asPatientWsiIndex(confirmMsg.dataset?.wsiIndex);
        if (this.isPatientLevelMode && viewing != null && cardWsi != null && cardWsi !== viewing) {
            this.showNotification(
                (window.__lang || 'zh') === 'en'
                    ? 'Switch to that slide before finalizing its draft.'
                    : '请先切换到要定稿的切片。',
                'warning'
            );
            return;
        }
        if (current.trim() === original.trim() && !events.length) {
            this.confirmFinalReport(confirmMsg);
            return;
        }
        this.confirmPhase(5, 'modify', {
            edited_report: current,
            intervention_events: events,
        }, confirmMsg, () => {
            confirmMsg.classList.add('is-resolved');
            this.hideDraftReportModal({ confirmMsg });
            this._renderFinalReportGenerating(confirmMsg);
        });
    }
    
    // ⭐ 处理弹窗按钮动作
    handleModalAction(action) {
        if (action === 'edit') {
            this.hideConfirmModal();
            // 手动调整模式：不发送结果，让用户编辑区域后通过左侧面板确认
            this.showNotification((window.__lang||'zh')==='en' 
                ? '✏️ Adjust regions in viewer, then confirm in left panel' 
                : '✏️ 请在视图中调整选区，完成后点击左侧面板确认', 'info');
            // 显示原有的底部手动选择提示栏
            this.enterManualEditMode();
        } else if (action === 'accept') {
            // Keep the proposal visible until the server ACKs the exact
            // confirmation_id.  Network loss must never look like approval.
            this.sendConfirmResult('accept', this.selectedRegions);
        } else if (action === 'skip') {
            // 跳过当前区域
            this.sendConfirmResult('skip', []);
        }
    }
    
    // ⭐ 进入手动编辑模式
    enterManualEditMode() {
        // 标记为手动编辑模式
        this.isManualEditing = true;
        
        // 显示提示
        const manualBar = document.getElementById('manualModeBar');
        const confirmBar = document.getElementById('confirmationBar');
        if (confirmBar) confirmBar.style.display = 'block';
        if (manualBar) manualBar.style.display = 'flex';
        
        // 更新左侧面板卡片状态
        this._updateLeftPanelCardWithConfirmation({
            confirmation_id: this.currentConfirmationId,
            frame_type: this.currentFrameType,
            regions: this.currentEditingRegions,
            parent_rect: this.currentParentRect
        });
    }
    
    // ⭐ 显示确认弹窗
    showConfirmModal(data) {
        const modal = document.getElementById('copilotConfirmModal');
        if (!modal) {
            console.warn('[Modal] 找不到确认对话框元素');
            return;
        }
        
        const titleEl = document.getElementById('confirmModalTitle');
        const stepEl = document.getElementById('confirmModalStep');
        const thinkingEl = document.getElementById('confirmModalThinkingContent');
        const proposalEl = document.getElementById('confirmModalProposalContent');
        const thumbnailsEl = document.getElementById('confirmModalThumbnails');
        const iconEl = document.getElementById('confirmModalIcon');
        
        // 根据 frame_type 设置不同的主题和内容
        const frameConfig = {
            'blue': {
                title: __t('modal_blue_title'),
                icon: 'fas fa-search-plus',
                theme: 'blue-theme',
                thinking: __t('modal_blue_thinking')
            },
            'green': {
                title: __t('modal_green_title'),
                icon: 'fas fa-microscope',
                theme: 'green-theme',
                thinking: __t('modal_green_thinking')
            },
            'report': {
                title: __t('modal_report_title'),
                icon: 'fas fa-file-medical',
                theme: 'report-theme',
                thinking: __t('modal_report_thinking')
            }
        };
        
        const config = frameConfig[data.frame_type] || frameConfig['blue'];
        
        // 设置主题
        modal.className = 'copilot-confirm-modal ' + config.theme;
        
        // 设置标题
        if (titleEl) titleEl.textContent = config.title;
        
        // 设置步骤信息
        if (stepEl) {
            const progressText = data.progress_text || '';
            stepEl.textContent = progressText;
        }
        
        // 设置图标
        if (iconEl) {
            iconEl.innerHTML = `<i class="${config.icon}"></i>`;
        }
        
        // 设置AI思考内容
        if (thinkingEl) {
            const briefFinding = data.brief_finding || config.thinking;
            thinkingEl.textContent = briefFinding;
        }
        
        // 设置建议内容（区域列表）
        if (proposalEl) {
            const regions = data.regions || [];
            if (regions.length === 0) {
                proposalEl.innerHTML = `<p class="no-regions">${(window.__lang||'zh')==='en' ? 'No regions selected' : '未选择任何区域'}</p>`;
            } else {
                const frameClass = data.frame_type === 'blue' ? 'blue-region' : 
                                  data.frame_type === 'green' ? 'green-region' : '';
                const _isEn = (window.__lang||'zh')==='en';
                let html = `<p>${_isEn ? `AI recommends the following <strong>${regions.length}</strong> region(s) for analysis:` : `AI建议选择以下 <strong>${regions.length}</strong> 个区域进行分析：`}</p>`;
                html += '<ul class="region-list">';
                regions.forEach((region, idx) => {
                    const position = region.rect ? `(${region.rect.x}, ${region.rect.y})` : '';
                    const size = region.rect ? `${region.rect.w}×${region.rect.h}` : '';
                    html += `
                        <li class="region-item ${frameClass}">
                            <span class="region-index">${idx + 1}</span>
                            <span class="region-info">${_isEn ? `Region ${idx + 1}` : `区域 ${idx + 1}`}</span>
                            <span class="region-pos">${position}</span>
                        </li>
                    `;
                });
                html += '</ul>';
                proposalEl.innerHTML = html;
            }
        }
        
        // 设置预览缩略图（如果有）
        if (thumbnailsEl) {
            // 暂时显示占位内容
            thumbnailsEl.innerHTML = `<div class="preview-empty"><i class="fas fa-image"></i> ${(window.__lang||'zh')==='en' ? 'Selection preview highlighted in viewer' : '选区预览将在视图中高亮显示'}</div>`;
        }
        
        // 显示弹窗并将焦点约束在对话框内；背景同时离开可访问树。
        this._confirmModalReturnFocus = document.activeElement;
        this._setConfirmModalBackgroundInert(true, modal);
        modal.setAttribute('aria-hidden', 'false');
        modal.style.display = 'flex';
        window.requestAnimationFrame(() => {
            document.getElementById('btnModalAccept')?.focus?.({ preventScroll: true });
        });
        
        console.log('[Modal] 显示确认弹窗:', data.frame_type, data.confirmation_id);
    }
    
    // ⭐ 隐藏确认弹窗
    hideConfirmModal({ preserveDecision = false } = {}) {
        const modal = document.getElementById('copilotConfirmModal');
        if (modal) {
            modal.style.display = 'none';
            modal.setAttribute('aria-hidden', 'true');
        }
        this._setConfirmModalBackgroundInert(false, modal);
        const returnFocus = this._confirmModalReturnFocus;
        this._confirmModalReturnFocus = null;
        if (returnFocus && typeof returnFocus.focus === 'function') {
            queueMicrotask(() => returnFocus.focus({ preventScroll: true }));
        }
        if (preserveDecision) {
            this.showNotification(
                (window.__lang || 'zh') === 'en'
                    ? 'Approval is still pending. Reopen the approval card when ready.'
                    : '审批仍待处理；准备好后可从待审批卡重新打开。',
                'info'
            );
        }
    }

    _setConfirmModalBackgroundInert(enabled, modal) {
        if (enabled) {
            if (this._confirmModalInertState.length || !modal) return;
            const elements = new Set();
            Array.from(modal.parentElement?.children || []).forEach((element) => {
                if (element !== modal) elements.add(element);
            });
            Array.from(document.body.children || []).forEach((element) => {
                if (element !== modal.parentElement && element.tagName !== 'SCRIPT') elements.add(element);
            });
            this._confirmModalInertState = Array.from(elements).map((element) => ({
                element,
                wasInert: Boolean(element.inert),
            }));
            this._confirmModalInertState.forEach(({ element }) => { element.inert = true; });
            return;
        }
        this._confirmModalInertState.forEach(({ element, wasInert }) => {
            if (element?.isConnected) element.inert = wasInert;
        });
        this._confirmModalInertState = [];
    }
    
    _setSelectionConfirmationSubmitting(submitting) {
        const modal = document.getElementById('copilotConfirmModal');
        modal?.querySelectorAll('button').forEach((button) => {
            button.disabled = Boolean(submitting);
        });
        const bar = document.getElementById('confirmationBar');
        bar?.querySelectorAll('button').forEach((button) => {
            button.disabled = Boolean(submitting);
        });
    }

    _releasePendingSelectionConfirmations(reason = 'cancelled') {
        this._pendingSelectionConfirmations.forEach((pending) => {
            window.clearTimeout(pending.timeoutId);
            pending.resolve?.({ status: 'error', error: reason });
        });
        this._pendingSelectionConfirmations.clear();
        this._setSelectionConfirmationSubmitting(false);
    }

    handleSelectionConfirmReceived(data) {
        const confirmationId = String(data?.confirmation_id || '');
        const pending = confirmationId
            ? this._pendingSelectionConfirmations.get(confirmationId)
            : null;
        if (!pending) {
            console.debug('忽略过期或不匹配的选区确认回执:', data);
            return;
        }
        window.clearTimeout(pending.timeoutId);
        this._pendingSelectionConfirmations.delete(confirmationId);
        this._setSelectionConfirmationSubmitting(false);
        const successStatuses = new Set([
            pending.action, 'confirmed', 'manual', 'success'
        ]);
        if (successStatuses.has(String(data?.status || ''))) {
            this.clearConfirmationHighlights();
            const bar = document.getElementById('confirmationBar');
            if (bar) bar.style.display = 'none';
            this.hideConfirmModal();
            if (this.currentConfirmationId === confirmationId) {
                this.currentConfirmationId = null;
            }
            const isEn = (window.__lang || 'zh') === 'en';
            const messages = isEn ? {
                accept: '✅ Accepted, continuing...',
                override: '✏️ Adjustment confirmed, continuing...',
                skip: '⏩ Region skipped',
                switch_to_auto: '🤖 Switched to full auto mode',
            } : {
                accept: '✅ 已接受，继续分析...',
                override: '✏️ 已确认调整，继续分析...',
                skip: '⏩ 已跳过该区域',
                switch_to_auto: '🤖 已切换为全自动模式',
            };
            this.showNotification(messages[pending.action] || (isEn ? 'Done' : '已处理'), 'success');
            pending.resolve?.({ ...data, status: 'success' });
            return;
        }
        this.showNotification(
            (window.__lang || 'zh') === 'en'
                ? `Selection was not accepted: ${data?.error || 'unknown error'}`
                : `选区确认未提交：${data?.error || '未知错误'}，请重试。`,
            'error'
        );
        pending.resolve?.({ ...data, status: 'error' });
    }

    // ⭐ 发送确认结果到后端（硬闸门核心）
    sendConfirmResult(action, regions) {
        if (!this.currentConfirmationId) {
            console.warn('无法发送确认结果：缺少confirmation_id');
            this.showNotification((window.__lang||'zh')==='en' ? '⚠️ Missing confirmation ID, saved locally' : '⚠️ 缺少确认ID，操作已本地保存', 'warning');
            return Promise.resolve({ status: 'error', error: 'missing_confirmation_id' });
        }
        
        if (!this.socket?.connected) {
            console.warn('无法发送确认结果：socket未连接');
            this.showNotification((window.__lang||'zh')==='en' ? '⚠️ Network disconnected; reconnect and try again' : '⚠️ 网络未连接，请恢复连接后重试', 'warning');
            return Promise.resolve({ status: 'error', error: 'socket_disconnected' });
        }
        
        console.log(`发送确认结果: action=${action}, regions=${regions.length}, confirmationId=${this.currentConfirmationId}`);
        
        // ⭐ 统一确认模式：只更新本地状态，不立即发送到后端
        if (this.pendingBlueConfirmations && this.pendingBlueConfirmations[this.currentConfirmationId]) {
            this.updateBlueConfirmation(this.currentConfirmationId, action, regions);
            
            // 显示反馈（不同于立即发送模式）
            const _e = (window.__lang||'zh')==='en';
            const messages = _e ? {
                'accept': '✅ Marked as accepted (takes effect after "Confirm All")',
                'override': '✏️ Selection adjusted (takes effect after "Confirm All")',
                'skip': '⏩ Marked as skipped (takes effect after "Confirm All")'
            } : {
                'accept': '✅ 已标记为接受（点击"确认全部"后生效）',
                'override': '✏️ 已调整选区（点击"确认全部"后生效）',
                'skip': '⏩ 已标记为跳过（点击"确认全部"后生效）'
            };
            this.showNotification(messages[action] || (_e ? 'Updated' : '已更新'), 'info');
            
            // 清理当前编辑状态，但不清理confirmationId
            this.clearConfirmationHighlights();
            return Promise.resolve({ status: 'success', local_only: true });
        }
        
        // ⭐ 原有逻辑：立即发送到后端（用于绿框确认等场景）
        const confirmationId = String(this.currentConfirmationId);
        const existing = this._pendingSelectionConfirmations.get(confirmationId);
        if (existing?.promise) return existing.promise;
        this._setSelectionConfirmationSubmitting(true);
        let resolvePromise;
        const promise = new Promise((resolve) => { resolvePromise = resolve; });
        const timeoutId = window.setTimeout(() => {
            const pending = this._pendingSelectionConfirmations.get(confirmationId);
            if (!pending) return;
            this._pendingSelectionConfirmations.delete(confirmationId);
            this._setSelectionConfirmationSubmitting(false);
            pending.resolve?.({ status: 'timeout', confirmation_id: confirmationId });
            this.showNotification(
                (window.__lang || 'zh') === 'en'
                    ? 'Selection acknowledgement timed out. The decision was not confirmed; please try again.'
                    : '选区确认回执超时，本次操作尚未确认，请重试。',
                'warning'
            );
        }, 10000);
        this._pendingSelectionConfirmations.set(confirmationId, {
            action,
            timeoutId,
            resolve: resolvePromise,
            promise,
        });
        this.socket.emit('agent.confirm_selection_result', {
            session_id: this.sessionId,
            confirmation_id: confirmationId,
            action: action,
            regions: regions
        });
        return promise;
    }
    
    handleConfirmRequest(data) {
        console.log('🎯 收到确认请求:', data);
        
        // ⭐ 当进入绿框阶段时，清理之前的蓝框相关卡片，避免误点击
        if (data.frame_type === 'green') {
            this._clearPreviousPhaseCards('blue');
        }
        
        this.currentConfirmationId = data.confirmation_id;
        this.currentParentRect = data.parent_rect;
        this.currentFrameType = data.frame_type;
        
        // ⭐ 深拷贝regions到可编辑状态
        this.currentEditingRegions = JSON.parse(JSON.stringify(data.regions || []));
        this.selectedRegions = this.currentEditingRegions;
        
        // ⭐ 确保旧的底部浮动条隐藏
        const bar = document.getElementById('confirmationBar');
        if (bar) bar.style.display = 'none';
        
        // ⭐ 更新左侧面板中对应的卡片（关联 confirmation_id 和 regions）
        this._updateLeftPanelCardWithConfirmation(data);
        
        // ⭐ 高亮显示可编辑的区域（带删除按钮）
        this.highlightEditableRegions(data.regions, data.frame_type, data.parent_rect, data.parent_roi_id);
        
        // ⭐ 显示 PathCopilot 弹窗式确认对话框
        this.showConfirmModal(data);
    }
    
    // ⭐ 清理上一阶段的卡片（进入新阶段时调用）
    _clearPreviousPhaseCards(previousPhase) {
        const list = document.getElementById('routeStepsList');
        if (!list) return;
        
        console.log(`[Collab] 清理上一阶段的临时卡片（进入${previousPhase === 'blue' ? '绿框' : '下一'}阶段）`);
        
        // ⭐ 清理所有非当前阶段的卡片（临时状态卡片：开始分析、正在分析等）
        const allCards = list.querySelectorAll('.collab-task-card, .route-step-card');
        allCards.forEach(card => {
            const cardLevel = card.dataset.level;
            const cardText = card.textContent || '';
            
            // 保留条件：当前正在编辑的绿框卡片（status-pending）
            // 删除条件：红框、蓝框卡片，以及包含"开始分析"、"正在分析"、"处理中"的临时卡片
            const isTemporaryCard = cardText.includes('开始分析') || 
                                   cardText.includes('正在分析') || 
                                   cardText.includes('处理中') ||
                                   cardText.includes('选取中');
            const isOldPhaseCard = cardLevel === 'red' || cardLevel === 'blue';
            
            if (isTemporaryCard || isOldPhaseCard) {
                card.classList.add('fade-out');
                setTimeout(() => {
                    if (card.parentNode) card.remove();
                }, 200);
            }
        });
        
        // 删除"确认全部蓝框"按钮
        const confirmAllBlueBtn = document.getElementById('confirmAllBlueBtn');
        if (confirmAllBlueBtn) {
            confirmAllBlueBtn.remove();
        }
    }
    
    // ⭐ 清理所有临时卡片（用于绿框选取完成时清理干扰项）
    _cleanupTemporaryCards() {
        const list = document.getElementById('routeStepsList');
        if (!list) return;
        
        console.log('[Collab] 清理临时卡片（开始分析、选取中等）');
        
        const allCards = list.querySelectorAll('.collab-task-card, .route-step-card');
        allCards.forEach(card => {
            const cardText = card.textContent || '';
            const cardLevel = card.dataset.level;
            
            // ⭐ 删除条件：
            // 1. "开始分析"类卡片（已完成分析，不需要显示）
            // 2. "选取中"类卡片（选取已完成，不需要显示）
            // 3. "正在分析"/"处理中"类卡片（临时状态）
            // 保留条件：绿框选取完成的卡片（有"选取完成"且是green或包含"绿框"）
            const isTemporary = cardText.includes('开始分析') || cardText.includes('Analyzing') ||
                               cardText.includes('选取中') || cardText.includes('Selecting') ||
                               cardText.includes('正在分析') ||
                               cardText.includes('处理中') ||
                               cardText.includes('开始筛选');
            
            // 保留绿框选取完成的卡片
            const isGreenComplete = (cardText.includes('绿框') && cardText.includes('选取完成')) ||
                                   (cardLevel === 'green' && cardText.includes('选取完成')) ||
                                   (cardLevel === 'green' && cardText.includes('selection complete')) ||
                                   (cardText.includes('green') && cardText.includes('selection complete'));
            
            if (isTemporary && !isGreenComplete) {
                console.log(`[Collab] 删除临时卡片: ${cardText.substring(0, 30)}...`);
                card.classList.add('fade-out');
                setTimeout(() => {
                    if (card.parentNode) card.remove();
                }, 150);
            }
        });
    }
    
    // ⭐ 处理"所有蓝框选取完成"事件（协作模式 - 统一确认）
    handleAllBlueSelected(data) {
        console.log('🎯 所有蓝框选取完成，等待统一确认:', data);
        
        const totalFrames = data.total_red_frames || 0;
        const pendingList = data.pending_confirmations || [];
        
        // 保存待确认列表
        this.pendingBlueConfirmations = {};
        pendingList.forEach(item => {
            this.pendingBlueConfirmations[item.confirmation_id] = {
                ...item,
                action: 'accept',  // 默认接受
                regions: null      // null表示使用AI原始选择
            };
        });
        
        // 显示提示消息
        const _e = (window.__lang||'zh')==='en';
        this.showNotification(_e 
            ? `✅ AI selected blue boxes for ${totalFrames} red boxes. Review, then click "Confirm All".`
            : `✅ AI已完成 ${totalFrames} 个红框的蓝框选取，请检查后点击"确认全部"`, 'info');
        
        this._showConfirmAllButton(totalFrames);
        
        if (window.pathchat && typeof window.pathchat.addMessage === 'function') {
            const sysMsg = _e
                ? `📋 **Blue Box Selection Complete**\n\nAI has selected blue box regions for ${totalFrames} red boxes.\n\nReview each region in the left panel. Click a card to adjust.\n\nWhen ready, click **"✅ Confirm All Blue Boxes"** at the bottom.`
                : `📋 **蓝框选取阶段完成**\n\nAI已为 ${totalFrames} 个红框选取了蓝框区域。\n\n请在左侧面板中查看各区域，如需调整请点击对应卡片。\n\n确认无误后，请点击左侧面板底部的 **"✅ 确认全部蓝框"** 按钮继续。`;
            window.pathchat.addMessage('system', sysMsg);
        }
    }
    
    // ⭐ 在左侧面板显示"确认全部"按钮
    _showConfirmAllButton(totalFrames) {
        const list = document.getElementById('routeStepsList');
        if (!list) return;
        
        // 移除旧的确认按钮（如果存在）
        const oldBtn = document.getElementById('confirmAllBlueBtn');
        if (oldBtn) oldBtn.remove();
        
        // 创建确认按钮容器
        const btnContainer = document.createElement('div');
        btnContainer.id = 'confirmAllBlueBtn';
        btnContainer.className = 'confirm-all-blue-container';
        const _e = (window.__lang||'zh')==='en';
        btnContainer.innerHTML = `
            <div class="confirm-all-blue-header">
                <span class="confirm-all-icon">📋</span>
                <span class="confirm-all-title">${_e ? 'Blue Box Selection Complete' : '蓝框选取完成'}</span>
                <span class="confirm-all-count">${totalFrames} ${_e ? 'red boxes' : '个红框'}</span>
            </div>
            <div class="confirm-all-hint">
                ${_e ? 'Click cards below to preview/adjust blue box selections' : '点击下方各卡片可预览/调整蓝框选区'}
            </div>
            <button class="confirm-all-blue-btn" id="confirmAllBlueAction">
                <i class="fas fa-check-double"></i> ${_e ? 'Confirm All Blue Boxes & Continue' : '确认全部蓝框，继续分析'}
            </button>
        `;
        
        // 插入到列表底部
        list.appendChild(btnContainer);
        
        // 绑定点击事件
        const actionBtn = btnContainer.querySelector('#confirmAllBlueAction');
        actionBtn.addEventListener('click', () => this._handleConfirmAllBlue());
        
        // ⭐ Bug修复：使用setTimeout确保DOM更新后再滚动到底部
        setTimeout(() => {
            list.scrollTop = list.scrollHeight;
            // 额外确保滚动到底部
            btnContainer.scrollIntoView({ behavior: 'smooth', block: 'end' });
        }, 100);
    }
    
    // ⭐ 处理"确认全部蓝框"按钮点击
    _handleConfirmAllBlue() {
        console.log('🎯 用户点击确认全部蓝框');
        
        if (!this.pendingBlueConfirmations || Object.keys(this.pendingBlueConfirmations).length === 0) {
            this.showNotification((window.__lang||'zh')==='en' ? '❌ No blue boxes to confirm' : '❌ 没有待确认的蓝框', 'warning');
            return;
        }
        
        // 收集所有确认结果
        const confirmations = {};
        for (const [confId, confData] of Object.entries(this.pendingBlueConfirmations)) {
            confirmations[confId] = {
                action: confData.action || 'accept',
                regions: confData.regions || []  // 空数组表示使用AI原始选择
            };
        }
        
        // 发送统一确认到后端
        if (this.socket) {
            this.socket.emit('agent.confirm_all_blue', {
                session_id: this.sessionId,
                confirmations: confirmations
            });
            console.log('📤 发送统一确认:', confirmations);
        }
        
        // 更新UI
        const btnContainer = document.getElementById('confirmAllBlueBtn');
        if (btnContainer) {
            btnContainer.innerHTML = `
                <div class="confirm-all-blue-header confirmed">
                    <span class="confirm-all-icon">✅</span>
                    <span class="confirm-all-title">${typeof __t !== 'undefined' ? __t('status_confirmed') : '已确认'}</span>
                </div>
                <div class="confirm-all-hint">
                    ${(window.__lang||'zh')==='en' ? 'Proceeding to next analysis level...' : '正在执行下一级分析...'}
                </div>
            `;
        }
        
        // ⭐ 删除所有蓝框选取卡片（确认后不再需要显示）
        const allCards = document.querySelectorAll('#routeStepsList .collab-task-card');
        allCards.forEach(card => {
            // 只删除蓝框选取相关的卡片
            const cardText = card.textContent || '';
            if (cardText.includes('蓝框选取') || cardText.includes('蓝框框选')) {
                card.classList.add('fade-out');
                setTimeout(() => {
                    if (card.parentNode) card.remove();
                }, 300);
            }
        });
        
        // 清理待确认数据
        this.pendingBlueConfirmations = {};
        
        this.showNotification((window.__lang||'zh')==='en' 
            ? '✅ All blue boxes confirmed, starting analysis...' 
            : '✅ 已确认所有蓝框选择，开始分析...', 'success');
    }
    
    // ⭐ 更新单个蓝框的确认状态（用户调整后）
    updateBlueConfirmation(confirmationId, action, regions) {
        if (this.pendingBlueConfirmations && this.pendingBlueConfirmations[confirmationId]) {
            this.pendingBlueConfirmations[confirmationId].action = action;
            this.pendingBlueConfirmations[confirmationId].regions = regions;
            console.log(`📝 更新蓝框确认: ${confirmationId}, action=${action}, regions=${regions?.length || 0}`);
        }
    }
    
    // ⭐ 处理"所有绿框选取完成"事件（协作模式 - 统一确认）
    handleAllGreenSelected(data) {
        console.log('🟢 所有绿框选取完成，等待统一确认:', data);
        
        const totalBlueFrames = data.total_blue_frames || 0;
        const totalGreenCount = data.total_green_count || 0;
        const pendingList = data.pending_confirmations || [];
        
        // ⭐ Bug修复：清理所有临时卡片（开始分析蓝框、选取中等），避免干扰映射和用户操作
        this._cleanupTemporaryCards();
        
        // 保存待确认列表
        this.pendingGreenConfirmations = {};
        pendingList.forEach(item => {
            this.pendingGreenConfirmations[item.confirmation_id] = {
                ...item,
                action: 'accept',
                regions: null
            };
        });
        
        // 显示提示
        const _e = (window.__lang||'zh')==='en';
        this.showNotification(_e
            ? `🟢 AI selected ${totalGreenCount} green boxes. Review, then click "Confirm All".`
            : `🟢 AI已完成 ${totalGreenCount} 个绿框选取，请检查后点击"确认全部"`, 'info');
        
        this._showConfirmAllGreenButton(totalBlueFrames, totalGreenCount);
        
        if (window.pathchat && typeof window.pathchat.addMessage === 'function') {
            const sysMsg = _e
                ? `📋 **Green Box Selection Complete**\n\nAI selected ${totalGreenCount} green box regions from ${totalBlueFrames} blue boxes.\n\nWhen ready, click **"✅ Confirm All Green Boxes"** in the left panel.`
                : `📋 **绿框选取阶段完成**\n\nAI已为 ${totalBlueFrames} 个蓝框选取了 ${totalGreenCount} 个绿框区域。\n\n确认无误后，请点击左侧面板的 **"✅ 确认全部绿框"** 按钮继续分析。`;
            window.pathchat.addMessage('system', sysMsg);
        }
    }
    
    // ⭐ 在左侧面板显示"确认全部绿框"按钮
    _showConfirmAllGreenButton(totalBlueFrames, totalGreenCount) {
        const list = document.getElementById('routeStepsList');
        if (!list) return;
        
        // 移除旧按钮
        const oldBtn = document.getElementById('confirmAllGreenBtn');
        if (oldBtn) oldBtn.remove();
        
        // 创建确认按钮容器
        const btnContainer = document.createElement('div');
        btnContainer.id = 'confirmAllGreenBtn';
        btnContainer.className = 'confirm-all-green-container';
        const _e = (window.__lang||'zh')==='en';
        btnContainer.innerHTML = `
            <div class="confirm-all-green-header">
                <span class="confirm-all-icon">🟢</span>
                <span class="confirm-all-title">${_e ? 'Green Box Selection Complete' : '绿框选取完成'}</span>
                <span class="confirm-all-count">${totalGreenCount} ${_e ? 'green boxes' : '个绿框'}</span>
            </div>
            <div class="confirm-all-hint">
                ${_e ? `From ${totalBlueFrames} blue boxes. Click cards below to preview.` : `来自 ${totalBlueFrames} 个蓝框，点击下方卡片可预览`}
            </div>
            <button class="confirm-all-green-btn" id="confirmAllGreenAction">
                <i class="fas fa-check-double"></i> ${_e ? 'Confirm All Green Boxes & Continue' : '确认全部绿框，继续分析'}
            </button>
        `;
        
        // 插入到列表底部
        list.appendChild(btnContainer);
        
        // 绑定点击事件
        const actionBtn = btnContainer.querySelector('#confirmAllGreenAction');
        actionBtn.addEventListener('click', () => this._handleConfirmAllGreen());
        
        // 自动滚动到底部
        list.scrollTop = list.scrollHeight;
    }
    
    // ⭐ 处理"确认全部绿框"按钮点击
    _handleConfirmAllGreen() {
        console.log('🟢 用户点击确认全部绿框');
        
        if (!this.pendingGreenConfirmations || Object.keys(this.pendingGreenConfirmations).length === 0) {
            this.showNotification((window.__lang||'zh')==='en' ? '❌ No green boxes to confirm' : '❌ 没有待确认的绿框', 'warning');
            return;
        }
        
        // 收集确认结果
        const confirmations = {};
        for (const [confId, confData] of Object.entries(this.pendingGreenConfirmations)) {
            confirmations[confId] = {
                action: confData.action || 'accept',
                regions: confData.regions || []
            };
        }
        
        // 发送确认
        if (this.socket) {
            this.socket.emit('agent.confirm_all_green', {
                session_id: this.sessionId,
                confirmations: confirmations
            });
            console.log('📤 发送绿框统一确认:', confirmations);
        }
        
        // 更新UI
        const btnContainer = document.getElementById('confirmAllGreenBtn');
        if (btnContainer) {
            btnContainer.innerHTML = `
                <div class="confirm-all-green-header confirmed">
                    <span class="confirm-all-icon">✅</span>
                    <span class="confirm-all-title">${typeof __t !== 'undefined' ? __t('green_box_confirmed') : '绿框已确认'}</span>
                </div>
                <div class="confirm-all-hint">
                    正在执行高倍镜分析...
                </div>
            `;
        }
        
        // 删除绿框选取卡片
        const allCards = document.querySelectorAll('#routeStepsList .collab-task-card');
        allCards.forEach(card => {
            const cardText = card.textContent || '';
            if (cardText.includes('绿框选取') || cardText.includes('绿框框选')) {
                card.classList.add('fade-out');
                setTimeout(() => {
                    if (card.parentNode) card.remove();
                }, 300);
            }
        });
        
        this.pendingGreenConfirmations = {};
        this.showNotification((window.__lang||'zh')==='en' 
            ? '✅ All green boxes confirmed, starting analysis...' 
            : '✅ 已确认所有绿框选择，开始分析...', 'success');
    }
    
    // ⭐ 处理解读确认请求（协作模式 - 右侧面板确认）
    handleAnalysisConfirmRequest(data) {
        console.log('📝 收到解读确认请求:', data);
        
        // 保存当前解读确认ID
        this.currentAnalysisConfirmationId = data.confirmation_id;
        
        const roiId = data.roi_id;
        const analysisType = data.analysis_type;
        
        // 找到对应的右侧消息元素
        let messageElement = null;
        
        if (roiId === 'final_report') {
            // 最终报告：查找最后一条系统消息
            const messages = document.querySelectorAll('.message-item.assistant, .chat-message.assistant');
            if (messages.length > 0) {
                messageElement = messages[messages.length - 1];
            }
        } else {
            // ROI解读：根据roiId查找
            messageElement = document.getElementById(`message-${roiId}`);
            if (!messageElement) {
                // 尝试其他选择器
                messageElement = document.querySelector(`[data-roi-id="${roiId}"]`);
            }
        }
        
        if (messageElement) {
            // 保存确认ID到消息元素
            messageElement.dataset.analysisConfirmationId = data.confirmation_id;
            
            // ⭐ 只有最终报告才需要确认按钮，中间结果默认同意
            if (analysisType === 'final_report') {
                // 确保消息有确认按钮
                if (!messageElement.querySelector('.result-action-buttons')) {
                    // 添加按钮
                    const level = analysisType.replace('_analysis', '');
                    this._addResultActionButtons(messageElement, roiId, data.roi_rect, level);
                }
                
                // 高亮显示该消息
                messageElement.classList.add('pending-confirmation');
                messageElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
                
                // 显示提示
                this.showNotification((window.__lang||'zh')==='en' ? '📝 Please confirm the final pathology report' : '📝 请确认最终病理报告', 'info');
            }
            // 其他分析结果默认同意，不需要用户确认
        } else {
            console.warn(`找不到对应的消息元素: ${roiId}`);
            // 即使找不到也保存确认ID，等消息显示后再关联
        }
    }
    
    // ⭐ 更新左侧面板卡片，关联确认请求数据
    _updateLeftPanelCardWithConfirmation(data) {
        const roiId = data.parent_roi_id || data.roi_id;
        console.log(`[Collab] _updateLeftPanelCardWithConfirmation: 查找卡片, parent_roi_id=${data.parent_roi_id}, roi_id=${data.roi_id}, frame_type=${data.frame_type}`);
        
        // 查找左侧面板中对应的卡片
        const list = document.getElementById('routeStepsList');
        if (!list) return;
        
        // ⭐ 先移除其他卡片的"当前确认"状态
        list.querySelectorAll('.collab-task-card.current-confirm').forEach(c => {
            c.classList.remove('current-confirm');
        });
        
        // ⭐ 多种方式查找匹配的卡片
        let card = null;
        
        // 方法1：精确匹配 roi_id
        if (roiId) {
            card = list.querySelector(`[data-roi-id="${roiId}"]`);
            if (card) console.log(`[Collab] 方法1: 精确匹配 roi_id 成功`);
        }
        
        // 方法2：根据 frame_type 和 level 匹配最近的 pending 卡片
        if (!card) {
            const frameLevel = data.frame_type; // 'blue' 或 'green'
            const pendingCards = list.querySelectorAll(`.collab-task-card.status-pending[data-level="${frameLevel}"]`);
            if (pendingCards.length > 0) {
                // 选择最后一个（最新的）pending 卡片
                card = pendingCards[pendingCards.length - 1];
                console.log(`[Collab] 方法2: 通过 level=${frameLevel} 匹配到 pending 卡片`);
            }
        }
        
        // 方法3：查找任意 pending 状态的蓝框/绿框卡片
        if (!card) {
            const allPendingCards = list.querySelectorAll('.collab-task-card.status-pending');
            for (const c of allPendingCards) {
                const level = c.dataset.level;
                if (level === 'blue' || level === 'green') {
                    card = c;
                    console.log(`[Collab] 方法3: 匹配到任意 pending 卡片, level=${level}`);
                    break;
                }
            }
        }
        
        // 方法4：查找最近添加的蓝框/绿框卡片（不管状态）
        if (!card) {
            const frameLevel = data.frame_type;
            const allCards = list.querySelectorAll(`.collab-task-card[data-level="${frameLevel}"]`);
            if (allCards.length > 0) {
                card = allCards[allCards.length - 1];
                console.log(`[Collab] 方法4: 匹配最近的 ${frameLevel} 卡片`);
            }
        }
        if (card) {
            console.log(`[Collab] 🎯 关联确认数据到左侧卡片: ${roiId}, confirmation_id: ${data.confirmation_id}`);
            
            // 更新卡片数据
            card.dataset.confirmationId = data.confirmation_id;
            card.dataset.regions = JSON.stringify(data.regions || []);
            if (data.parent_rect) {
                card.dataset.rect = JSON.stringify(data.parent_rect);
            }
            
            // 更新 _stepData
            if (card._stepData) {
                card._stepData.confirmation_id = data.confirmation_id;
                card._stepData.regions = data.regions;
                card._stepData.status = 'pending';
                // ⭐ Bug修复：同步更新 rect，确保增加模式下使用正确的父框大小
                if (data.parent_rect) {
                    card._stepData.rect = data.parent_rect;
                }
            }
            
            // 更新卡片状态为 pending（等待确认）
            card.classList.remove('status-active', 'status-done', 'status-skipped');
            card.classList.add('status-pending', 'current-confirm');
            
            // 更新状态文字
            const statusEl = card.querySelector('.collab-card-status');
            if (statusEl) {
                statusEl.textContent = (window.__lang||'zh')==='en' ? '⏳ Click to confirm' : '⏳ 请点击确认';
            }
            
            // ⭐ 滚动到该卡片并添加高亮效果
            setTimeout(() => {
                card.scrollIntoView({ behavior: 'smooth', block: 'center' });
                // 添加闪烁动画
                card.classList.add('highlight-pulse');
                setTimeout(() => card.classList.remove('highlight-pulse'), 2000);
            }, 100);
            
            // 更新子区域计数
            let countEl = card.querySelector('.collab-card-count');
            if (data.regions && data.regions.length > 0) {
                if (countEl) {
                    countEl.textContent = __t('sub_region_count', data.regions.length);
                } else {
                    countEl = document.createElement('div');
                    countEl.className = 'collab-card-count';
                    countEl.textContent = __t('sub_region_count', data.regions.length);
                    const header = card.querySelector('.collab-card-header');
                    if (header) {
                        header.after(countEl);
                    }
                }
            }
            
            // 如果没有按钮组，添加按钮组
            if (!card.querySelector('.collab-btn-group')) {
                const btnGroup = document.createElement('div');
                btnGroup.className = 'collab-btn-group';
                btnGroup.innerHTML = `
                    <button class="collab-mini-btn collab-btn-accept" data-action="accept" title="接受AI选择">
                        <i class="fas fa-check"></i> 接受
                    </button>
                    <button class="collab-mini-btn collab-btn-edit" data-action="edit" title="手动调整选区">
                        <i class="fas fa-edit"></i> 调整
                    </button>
                    <button class="collab-mini-btn collab-btn-skip" data-action="skip" title="跳过此区域">
                        <i class="fas fa-forward"></i> 跳过
                    </button>
                `;
                card.appendChild(btnGroup);
                
                // 绑定按钮事件
                btnGroup.querySelectorAll('.collab-mini-btn').forEach(btn => {
                    btn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        const action = btn.dataset.action;
                        const step = card._stepData || {
                            roi_id: roiId,
                            confirmation_id: data.confirmation_id,
                            regions: data.regions,
                            rect: data.parent_rect,
                            level: data.frame_type
                        };
                        this._handleCollabButtonClick(card, step, action, list);
                    });
                });
            }
            
            // 闪烁高亮提示用户
            card.classList.add('flash-highlight');
            setTimeout(() => card.classList.remove('flash-highlight'), 600);
            
            // 滚动到该卡片
            card.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } else {
            console.log(`[Collab] 未找到左侧卡片: ${roiId}，将在后续创建`);
        }
    }
    
    // ⭐ 向聊天窗口追加确认卡片
    appendConfirmationMessage(data) {
        const chatbox = document.getElementById('chatMessages') || document.querySelector('.chat-messages');
        if (!chatbox) {
            console.warn('未找到聊天框元素');
            return;
        }
        
        // 生成标题
        const _e = (window.__lang||'zh')==='en';
        const frameNames = _e
            ? { 'blue': 'Blue Box (40×)', 'green': 'Green Box (160×)', 'report': 'Final Report' }
            : { 'blue': '蓝框(40×)', 'green': '绿框(160×)', 'report': '最终报告' };
        const frameIcons = { 'blue': '🔵', 'green': '🟢', 'report': '📋' };
        const frameName = frameNames[data.frame_type] || data.frame_type;
        const frameIcon = frameIcons[data.frame_type] || '📦';
        
        let title = _e
            ? `${frameIcon} Confirm: ${data.selected_count} ${frameName}`
            : `${frameIcon} 需确认：${data.selected_count} 个${frameName}`;
        if (data.progress_text) {
            title = _e
                ? `${frameIcon} [${data.progress_text}] ${data.selected_count} ${frameName}`
                : `${frameIcon} [${data.progress_text}] ${data.selected_count} 个${frameName}`;
        }
        
        // 创建确认卡片
        const cardId = `confirm-card-${data.confirmation_id}`;
        const card = document.createElement('div');
        card.id = cardId;
        card.className = 'message assistant-message confirmation-card';
        card.innerHTML = `
            <div class="message-content">
                <div class="confirmation-card-header">
                    <span class="confirmation-title">${title}</span>
                    <span class="confirmation-count" id="region-count-${data.confirmation_id}">${data.selected_count}个</span>
                </div>
                <div class="confirmation-card-body">
                    <p>${data.message || '请确认AI选择的区域，或进行调整'}</p>
                    ${data.brief_finding ? `<p class="brief-finding">💡 ${data.brief_finding}</p>` : ''}
                </div>
                <div class="confirmation-card-actions">
                    <button class="confirm-btn confirm-btn-accept" data-action="accept" data-id="${data.confirmation_id}">
                        <i class="fas fa-check"></i> 接受
                    </button>
                    <button class="confirm-btn confirm-btn-override" data-action="override" data-id="${data.confirmation_id}">
                        <i class="fas fa-edit"></i> 已调整
                    </button>
                    <button class="confirm-btn confirm-btn-add" data-action="add" data-id="${data.confirmation_id}">
                        <i class="fas fa-plus"></i> 添加框
                    </button>
                    <button class="confirm-btn confirm-btn-skip" data-action="skip" data-id="${data.confirmation_id}">
                        <i class="fas fa-forward"></i> 跳过
                    </button>
                </div>
            </div>
        `;
        
        // ⭐ 存储数据到卡片元素
        card.dataset.confirmationId = data.confirmation_id;
        card.dataset.frameType = data.frame_type;
        card._confirmData = {
            regions: JSON.parse(JSON.stringify(data.regions || [])),
            parentRect: data.parent_rect,
            parentRoiId: data.parent_roi_id,
            frameType: data.frame_type
        };
        
        chatbox.appendChild(card);
        
        // ⭐ 添加悬停事件
        card.addEventListener('mouseenter', () => {
            const cardData = card._confirmData;
            if (cardData && cardData.parentRect) {
                console.log('悬停卡片，显示区域:', cardData.parentRoiId);
                
                document.querySelectorAll('.confirmation-card.active-viewing').forEach(c => {
                    c.classList.remove('active-viewing');
                });
                
                card.classList.add('active-viewing');
                this.highlightCardRegions(cardData);
            }
        });
        
        // ⭐ 添加点击事件
        card.addEventListener('click', (e) => {
            if (e.target.closest('.confirm-btn')) return;
            
            const cardData = card._confirmData;
            if (cardData && cardData.parentRect) {
                console.log('点击卡片，跳转到区域:', cardData.parentRoiId);
                this.highlightCardRegions(cardData);
                
                if (!card.querySelector('.confirmation-status')) {
                    this.currentConfirmationId = card.dataset.confirmationId;
                    this.currentFrameType = cardData.frameType;
                    this.currentParentRect = cardData.parentRect;
                    this.currentEditingRegions = JSON.parse(JSON.stringify(cardData.regions));
                }
            }
        });
        
        // 绑定按钮事件
        card.querySelectorAll('.confirm-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                
                const action = btn.dataset.action;
                const confirmId = btn.dataset.id;
                
                if (action === 'add') {
                    const cardData = card._confirmData;
                    if (cardData) {
                        this.currentConfirmationId = confirmId;
                        this.currentFrameType = cardData.frameType;
                        this.currentParentRect = cardData.parentRect;
                        this.currentEditingRegions = JSON.parse(JSON.stringify(cardData.regions));
                        this.highlightCardRegions(cardData);
                    }
                    this.startManualAddMode();
                    return;
                }
                
                const cardData = card._confirmData;
                const regionsToSend = this.currentConfirmationId === confirmId 
                    ? this.currentEditingRegions 
                    : (cardData ? cardData.regions : []);
                
                this.currentConfirmationId = confirmId;
                card.querySelectorAll('.confirm-btn').forEach(b => {
                    b.disabled = true;
                    b.classList.add('disabled');
                });
                this.sendConfirmResult(action, regionsToSend).then((result) => {
                    if (result?.status !== 'success') {
                        card.querySelectorAll('.confirm-btn').forEach(b => {
                            b.disabled = false;
                            b.classList.remove('disabled');
                        });
                        return;
                    }
                    const statusText = { 'accept': '✅ 已接受', 'override': '✏️ 已调整', 'skip': '⏩ 已跳过' };
                    card.querySelector('.confirmation-card-header').innerHTML +=
                        `<span class="confirmation-status">${statusText[action] || '已处理'}</span>`;
                    this.clearConfirmationHighlights();
                });
            });
        });
        
        chatbox.scrollTop = chatbox.scrollHeight;
    }
    
    // ⭐ 高亮显示卡片对应的区域
    highlightCardRegions(cardData) {
        if (!window.pathchat || !window.pathchat.viewer) {
            console.warn('OpenSeadragon viewer not available');
            return;
        }
        
        this.clearConfirmationHighlights();
        
        const viewer = window.pathchat.viewer;
        const { regions, parentRect, parentRoiId, frameType } = cardData;
        
        // 高亮父框
        if (parentRect) {
            this.addHighlightOverlay(viewer, parentRect, 'parent-highlight', '#FFD700', 5, true);
        }
        
        // 显示子框
        const color = frameType === 'blue' ? '#00AAFF' : '#00FF88';
        if (regions && regions.length > 0) {
        regions.forEach((region, idx) => {
            if (region.rect) {
                    this.addEditableOverlay(viewer, region, idx, color, frameType);
            }
        });
        }
        
        // 缩放到父框区域
        if (parentRect && window.pathchat.slideInfo) {
            const viewportRect = viewer.viewport.imageToViewportRectangle(
                parentRect.x, parentRect.y, parentRect.w, parentRect.h
            );
            const padding = 0.15;
            const paddedRect = new OpenSeadragon.Rect(
                viewportRect.x - viewportRect.width * padding,
                viewportRect.y - viewportRect.height * padding,
                viewportRect.width * (1 + 2 * padding),
                viewportRect.height * (1 + 2 * padding)
            );
            viewer.viewport.fitBounds(paddedRect, true);
        }
    }
    
    // ⭐ 高亮显示可编辑的区域
    highlightEditableRegions(regions, frameType, parentRect, parentRoiId) {
        console.log('高亮显示可编辑区域:', regions.length, '父框:', parentRoiId);
        
        this.clearConfirmationHighlights();
        
        if (!window.pathchat || !window.pathchat.viewer) {
            console.warn('OpenSeadragon viewer not available');
            return;
        }
        
        const viewer = window.pathchat.viewer;
        
        if (parentRect) {
            this.addHighlightOverlay(viewer, parentRect, 'parent-highlight', '#FFD700', 4, true);
        }
        
        const color = frameType === 'blue' ? '#00AAFF' : '#00FF88';
        regions.forEach((region, idx) => {
            if (region.rect) {
                this.addEditableOverlay(viewer, region, idx, color, frameType);
            }
        });
        
        if (parentRect && window.pathchat.slideInfo) {
            const viewportRect = viewer.viewport.imageToViewportRectangle(
                parentRect.x, parentRect.y, parentRect.w, parentRect.h
            );
            const padding = 0.2;
            const paddedRect = new OpenSeadragon.Rect(
                viewportRect.x - viewportRect.width * padding,
                viewportRect.y - viewportRect.height * padding,
                viewportRect.width * (1 + 2 * padding),
                viewportRect.height * (1 + 2 * padding)
            );
            viewer.viewport.fitBounds(paddedRect, true);
        }
        
        this.showNotification((window.__lang||'zh')==='en'
            ? `🔍 Editable: ${regions.length} ${frameType === 'blue' ? 'blue' : 'green'} box(es) — click ✕ to remove`
            : `🔍 可编辑: ${regions.length} 个${frameType === 'blue' ? '蓝' : '绿'}框（点击 ✕ 删除）`, 'info');
    }
    
    // ⭐ 添加可编辑的覆盖层
    addEditableOverlay(viewer, region, idx, color, frameType) {
        if (!window.pathchat.slideInfo) return;
        
        const rect = region.rect;
        const roiId = region.roi_id || `region-${idx}`;
        
        const overlay = document.createElement('div');
        overlay.id = `editable-region-${roiId}`;
        overlay.className = 'editable-region-overlay';
        overlay.dataset.roiId = roiId;
        overlay.dataset.index = idx;
        overlay.dataset.rect = JSON.stringify(rect);
        // ⭐ Bug修复：去掉背景填充色，只保留边框，避免影响视觉选择
        // ⭐ 使用 !important 确保不被其他样式覆盖
        overlay.style.cssText = `
            border: 5px solid ${color} !important;
            background: transparent !important;
            background-color: transparent !important;
            box-sizing: border-box;
            position: relative;
        `;
        
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'region-delete-btn';
        deleteBtn.innerHTML = '✕';
        deleteBtn.title = '删除此区域';
        deleteBtn.style.cssText = `
            position: absolute;
            top: 2px;
            right: 2px;
            width: 24px;
            height: 24px;
            border: none;
            border-radius: 50%;
            background: #e74c3c;
            color: white;
            font-size: 14px;
            font-weight: bold;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            transition: transform 0.2s, background 0.2s;
        `;
        
        deleteBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this.removeEditableRegion(roiId, overlay);
        });
        
        deleteBtn.addEventListener('mouseenter', () => {
            deleteBtn.style.transform = 'scale(1.2)';
            deleteBtn.style.background = '#c0392b';
        });
        
        deleteBtn.addEventListener('mouseleave', () => {
            deleteBtn.style.transform = 'scale(1)';
            deleteBtn.style.background = '#e74c3c';
        });
        
        overlay.appendChild(deleteBtn);
        
        const viewportRect = viewer.viewport.imageToViewportRectangle(rect.x, rect.y, rect.w, rect.h);
        viewer.addOverlay({
            element: overlay,
            location: viewportRect
        });
        
        if (!this.confirmationOverlays) this.confirmationOverlays = [];
        this.confirmationOverlays.push(overlay.id);
    }
    
    // ⭐ 删除一个可编辑区域
    removeEditableRegion(roiId, overlayElement) {
        console.log('删除区域:', roiId);
        
        const index = this.currentEditingRegions.findIndex(r => r.roi_id === roiId);
        if (index > -1) {
            this.currentEditingRegions.splice(index, 1);
        }
        
        if (window.pathchat && window.pathchat.viewer) {
            window.pathchat.viewer.removeOverlay(overlayElement);
        }
        
        if (this.confirmationOverlays) {
            const idx = this.confirmationOverlays.indexOf(overlayElement.id);
            if (idx > -1) this.confirmationOverlays.splice(idx, 1);
        }
        
        if (this.currentConfirmationId) {
            const countEl = document.getElementById(`region-count-${this.currentConfirmationId}`);
            if (countEl) {
                countEl.textContent = (window.__lang||'zh')==='en' ? `${this.currentEditingRegions.length}` : `${this.currentEditingRegions.length}个`;
            }
        }
        
        this.showNotification((window.__lang||'zh')==='en' 
            ? `Region deleted, ${this.currentEditingRegions.length} remaining` 
            : `已删除区域，剩余 ${this.currentEditingRegions.length} 个`, 'info');
    }
    
    // ⭐ 手动添加模式
    startManualAddMode() {
        console.log('进入手动添加模式');
        this.isManualSelecting = true;
        
        const viewerContainer = document.querySelector('.viewer-container');
        if (viewerContainer) {
            viewerContainer.classList.add('manual-drawing-mode');
        }
        
        this.enableManualDrawingForAdd();
        
        this.showNotification((window.__lang||'zh')==='en' ? '🖌️ Draw a new region by dragging' : '🖌️ 拖拽绘制新区域，完成后自动添加', 'info');
    }
    
    // ⭐ 启用手动绘制
    enableManualDrawingForAdd() {
        if (!window.pathchat || !window.pathchat.viewer) return;
        
        const viewer = window.pathchat.viewer;
        let isDrawing = false;
        let startPoint = null;
        let drawingOverlay = null;
        
        const canvas = viewer.canvas;
        
        const onMouseDown = (e) => {
            if (!this.isManualSelecting) return;
            
            isDrawing = true;
            const webPoint = new OpenSeadragon.Point(e.clientX, e.clientY);
            startPoint = viewer.viewport.windowToViewportCoordinates(webPoint);
            
            drawingOverlay = document.createElement('div');
            drawingOverlay.className = 'manual-drawing-overlay';
            drawingOverlay.style.cssText = `
                position: absolute;
                border: 3px dashed #e74c3c;
                background: rgba(231, 76, 60, 0.2);
                pointer-events: none;
            `;
            canvas.appendChild(drawingOverlay);
            e.preventDefault();
        };
        
        const onMouseMove = (e) => {
            if (!isDrawing || !startPoint || !drawingOverlay) return;
            
            const webPoint = new OpenSeadragon.Point(e.clientX, e.clientY);
            const currentPoint = viewer.viewport.windowToViewportCoordinates(webPoint);
            
            const startPixel = viewer.viewport.viewportToWindowCoordinates(startPoint);
            const currentPixel = viewer.viewport.viewportToWindowCoordinates(currentPoint);
            
            const canvasRect = canvas.getBoundingClientRect();
            const left = Math.min(startPixel.x, currentPixel.x) - canvasRect.left;
            const top = Math.min(startPixel.y, currentPixel.y) - canvasRect.top;
            const width = Math.abs(currentPixel.x - startPixel.x);
            const height = Math.abs(currentPixel.y - startPixel.y);
            
            drawingOverlay.style.left = `${left}px`;
            drawingOverlay.style.top = `${top}px`;
            drawingOverlay.style.width = `${width}px`;
            drawingOverlay.style.height = `${height}px`;
        };
        
        const onMouseUp = (e) => {
            if (!isDrawing || !startPoint) return;
            
            isDrawing = false;
            
            const webPoint = new OpenSeadragon.Point(e.clientX, e.clientY);
            const endPoint = viewer.viewport.windowToViewportCoordinates(webPoint);
            
            const startImage = viewer.viewport.viewportToImageCoordinates(startPoint);
            const endImage = viewer.viewport.viewportToImageCoordinates(endPoint);
            
            const x = Math.min(startImage.x, endImage.x);
            const y = Math.min(startImage.y, endImage.y);
            const w = Math.abs(endImage.x - startImage.x);
            const h = Math.abs(endImage.y - startImage.y);
            
            if (drawingOverlay) {
                drawingOverlay.remove();
                drawingOverlay = null;
            }
            
            if (w < 100 || h < 100) {
                this.showNotification((window.__lang||'zh')==='en' ? 'Region too small, try again' : '区域太小，请重新绘制', 'warning');
                return;
            }
            
            const newRegion = {
                roi_id: `manual-${Date.now()}`,
                rect: { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) },
                index: this.currentEditingRegions.length + 1,
                position: `(${Math.round(x)}, ${Math.round(y)})`,
                size: `${Math.round(w)}×${Math.round(h)}`
            };
            
            this.currentEditingRegions.push(newRegion);
            
            const color = this.currentFrameType === 'blue' ? '#00AAFF' : '#00FF88';
            this.addEditableOverlay(viewer, newRegion, this.currentEditingRegions.length - 1, color, this.currentFrameType);
            
            if (this.currentConfirmationId) {
                const countEl = document.getElementById(`region-count-${this.currentConfirmationId}`);
                if (countEl) {
                    countEl.textContent = (window.__lang||'zh')==='en' ? `${this.currentEditingRegions.length}` : `${this.currentEditingRegions.length}个`;
                }
            }
            
            this.showNotification((window.__lang||'zh')==='en' 
                ? `Region added, ${this.currentEditingRegions.length} total` 
                : `已添加新区域，共 ${this.currentEditingRegions.length} 个`, 'success');
            
            this.isManualSelecting = false;
            const viewerContainer = document.querySelector('.viewer-container');
            if (viewerContainer) {
                viewerContainer.classList.remove('manual-drawing-mode');
            }
            
            canvas.removeEventListener('mousedown', onMouseDown);
            canvas.removeEventListener('mousemove', onMouseMove);
            canvas.removeEventListener('mouseup', onMouseUp);
        };
        
        canvas.addEventListener('mousedown', onMouseDown);
        canvas.addEventListener('mousemove', onMouseMove);
        canvas.addEventListener('mouseup', onMouseUp);
    }
    
    // 添加高亮覆盖层
    addHighlightOverlay(viewer, rect, id, color, lineWidth, animate) {
        if (!window.pathchat.slideInfo) return;
        
        const overlay = document.createElement('div');
        overlay.id = `confirm-highlight-${id}`;
        overlay.className = 'confirmation-highlight' + (animate ? ' pulsing' : '');
        // ⭐ Bug修复：去掉背景填充色，只保留边框
        // ⭐ 使用 !important 确保不被其他样式覆盖
        overlay.style.cssText = `
            border: ${lineWidth}px solid ${color} !important;
            background: transparent !important;
            background-color: transparent !important;
            box-sizing: border-box;
            pointer-events: none;
            ${animate ? 'animation: pulse-border 1.5s ease-in-out infinite;' : ''}
        `;
        
        const viewportRect = viewer.viewport.imageToViewportRectangle(
            rect.x, rect.y, rect.w, rect.h
        );
        
        viewer.addOverlay({
            element: overlay,
            location: viewportRect
        });
        
        if (!this.confirmationOverlays) this.confirmationOverlays = [];
        this.confirmationOverlays.push(overlay.id);
    }
    
    // 清除确认相关的高亮
    // ⭐ Bug修复：增强清除逻辑，确保所有overlay都被清除
    clearConfirmationHighlights() {
        if (!window.pathchat || !window.pathchat.viewer) return;
        
        const viewer = window.pathchat.viewer;
        
        // 1. 清除confirmationOverlays数组中的overlay
        if (this.confirmationOverlays) {
            this.confirmationOverlays.forEach(id => {
                const el = document.getElementById(id);
                if (el) {
                    viewer.removeOverlay(el);
                }
            });
            this.confirmationOverlays = [];
        }
        
        // 2. ⭐ 额外清除所有可能遗留的高亮overlay（通过类名查找）
        const overlayClasses = [
            '.editable-region-overlay',
            '.confirmation-highlight',
            '[id^="editable-region-"]',
            '[id^="confirm-highlight-"]'
        ];
        
        overlayClasses.forEach(selector => {
            document.querySelectorAll(selector).forEach(el => {
                try {
                    viewer.removeOverlay(el);
                } catch (e) {
                    // 如果overlay不存在，忽略错误
                }
                // 确保元素从DOM中移除
                if (el.parentNode) {
                    el.parentNode.removeChild(el);
                }
            });
        });
    }
    
    // 手动选择相关
    startManualSelection() {
        console.log('开始手动选择');
        
        document.querySelector('.confirmation-bar-content').style.display = 'none';
        document.getElementById('manualModeBar').style.display = 'flex';
        document.getElementById('manualCount').textContent = (window.__lang||'zh')==='en' ? 'Selected: 0' : '已选: 0个';
        
        this.isManualSelecting = true;
        this.manualSelections = [];
        
        const viewerContainer = document.querySelector('.viewer-container');
        if (viewerContainer) {
            viewerContainer.classList.add('manual-drawing-mode');
        }
        
        this.enableManualDrawing();
        
        this.showNotification((window.__lang||'zh')==='en' ? '🖌️ Draw selections on the WSI viewer' : '🖌️ 在WSI视图中拖拽绘制选区', 'info');
    }
    
    cancelManualSelection() {
        console.log('取消手动选择');
        
        this.isManualSelecting = false;
        this.manualSelections = [];
        
        const viewerContainer = document.querySelector('.viewer-container');
        if (viewerContainer) {
            viewerContainer.classList.remove('manual-drawing-mode');
        }
        
        const drawingOverlays = document.querySelectorAll('.manual-drawing-overlay');
        drawingOverlays.forEach(o => o.remove());
        
        document.querySelector('.confirmation-bar-content').style.display = 'flex';
        document.getElementById('manualModeBar').style.display = 'none';
        
        this.showNotification(__t('cancelled'), 'info');
    }
    
    enableManualDrawing() {
        if (!this.viewer) return;
        
        let isDrawing = false;
        let startPoint = null;
        let drawingOverlay = null;
        
        const canvas = this.viewer.canvas;
        
        const onMouseDown = (e) => {
            if (!this.isManualSelecting) return;
            
            isDrawing = true;
            const webPoint = new OpenSeadragon.Point(e.clientX, e.clientY);
            startPoint = this.viewer.viewport.windowToViewportCoordinates(webPoint);
            
            drawingOverlay = document.createElement('div');
            drawingOverlay.className = 'manual-drawing-overlay';
            drawingOverlay.style.cssText = `
                position: absolute;
                border: 3px dashed #e74c3c;
                background: rgba(231, 76, 60, 0.2);
                pointer-events: none;
            `;
            canvas.appendChild(drawingOverlay);
            
            e.preventDefault();
        };
        
        const onMouseMove = (e) => {
            if (!isDrawing || !startPoint || !drawingOverlay) return;
            
            const webPoint = new OpenSeadragon.Point(e.clientX, e.clientY);
            const currentPoint = this.viewer.viewport.windowToViewportCoordinates(webPoint);
            
            const rect = new OpenSeadragon.Rect(
                Math.min(startPoint.x, currentPoint.x),
                Math.min(startPoint.y, currentPoint.y),
                Math.abs(currentPoint.x - startPoint.x),
                Math.abs(currentPoint.y - startPoint.y)
            );
            
            const pixelRect = this.viewer.viewport.viewportToViewerElementRectangle(rect);
            drawingOverlay.style.left = pixelRect.x + 'px';
            drawingOverlay.style.top = pixelRect.y + 'px';
            drawingOverlay.style.width = pixelRect.width + 'px';
            drawingOverlay.style.height = pixelRect.height + 'px';
        };
        
        const onMouseUp = (e) => {
            if (!isDrawing || !startPoint) return;
            
            isDrawing = false;
            
            const webPoint = new OpenSeadragon.Point(e.clientX, e.clientY);
            const endPoint = this.viewer.viewport.windowToViewportCoordinates(webPoint);
            
            const imageSize = this.viewer.world.getItemAt(0).getContentSize();
            const rect = {
                x: Math.min(startPoint.x, endPoint.x) * imageSize.x,
                y: Math.min(startPoint.y, endPoint.y) * imageSize.y,
                w: Math.abs(endPoint.x - startPoint.x) * imageSize.x,
                h: Math.abs(endPoint.y - startPoint.y) * imageSize.y
            };
            
            if (rect.w > 100 && rect.h > 100) {
                this.manualSelections.push(rect);
                console.log('添加手动选区:', rect);
                
                const countEl = document.getElementById('manualCount');
                if (countEl) {
                    countEl.textContent = (window.__lang||'zh')==='en' ? `Selected: ${this.manualSelections.length}` : `已选: ${this.manualSelections.length}个`;
                }
                
                this.showNotification((window.__lang||'zh')==='en' ? `✅ Selection ${this.manualSelections.length} added` : `✅ 已添加选区 ${this.manualSelections.length}`, 'success');
                
                if (drawingOverlay) {
                    drawingOverlay.style.border = '3px solid #2ecc71';
                    drawingOverlay.style.background = 'rgba(46, 204, 113, 0.2)';
                    const badge = document.createElement('div');
                    badge.className = 'selection-number-badge';
                    badge.textContent = this.manualSelections.length;
                    drawingOverlay.appendChild(badge);
                }
            } else {
                if (drawingOverlay) {
                    drawingOverlay.remove();
                }
                this.showNotification((window.__lang||'zh')==='en' ? '⚠️ Selection too small, try again' : '⚠️ 选区太小，请重新绘制', 'warning');
            }
            
            startPoint = null;
            drawingOverlay = null;
        };
        
        canvas.addEventListener('mousedown', onMouseDown);
        canvas.addEventListener('mousemove', onMouseMove);
        canvas.addEventListener('mouseup', onMouseUp);
        
        this.manualDrawingHandlers = { onMouseDown, onMouseMove, onMouseUp };
    }
    
    finishManualSelection() {
        console.log('完成手动选择, 选区数:', this.manualSelections.length);
        
        if (this.manualSelections.length === 0) {
            this.showNotification((window.__lang||'zh')==='en' ? 'Select at least one region' : '请至少选择一个区域', 'warning');
            return;
        }
        
        this.isManualSelecting = false;
        
        const viewerContainer = document.querySelector('.viewer-container');
        if (viewerContainer) {
            viewerContainer.classList.remove('manual-drawing-mode');
        }
        
        const drawingOverlays = document.querySelectorAll('.manual-drawing-overlay');
        drawingOverlays.forEach(o => o.remove());
        
        if (this.manualDrawingHandlers && this.viewer) {
            const canvas = this.viewer.canvas;
            canvas.removeEventListener('mousedown', this.manualDrawingHandlers.onMouseDown);
            canvas.removeEventListener('mousemove', this.manualDrawingHandlers.onMouseMove);
            canvas.removeEventListener('mouseup', this.manualDrawingHandlers.onMouseUp);
        }
        
        const selectionsCount = this.manualSelections.length;
        this.sendConfirmResult('override', this.manualSelections).then((result) => {
            if (result?.status === 'success') {
                this.showNotification(
                    (window.__lang||'zh')==='en'
                        ? `✅ Submitted ${selectionsCount} manual selection(s)`
                        : `✅ 已提交 ${selectionsCount} 个手动选区`,
                    'success'
                );
            }
        });
    }
    
    // ================== Patient Level 事件处理 ==================
    
    /**
     * ⭐ 断点续传：检测到已有缓存的切片，弹框让用户决定
     */
    async handlePatientCacheCheck(data) {
        console.log('[Patient Level] 检测到缓存:', data);
        if (!this._patientEventMatchesActiveRun(data)) return;
        const gatePatientId = String(data?.patient_id || '');
        const gateRunToken = String(data?.run_token || '');
        const cacheGateId = String(data?.cache_gate_id || '');
        if (!gatePatientId || !gateRunToken || !cacheGateId) {
            console.warn('[Patient Level] Ignored cache prompt without a bound gate');
            return;
        }
        
        // 如果前端已通过三选一弹窗做了决定，跳过后端旧弹窗
        if (window.pathchat?._plCacheDecision) {
            console.log('[Patient Level] 前端已做缓存决定，跳过后端弹窗');
            return;
        }
        
        const cachedSlides = data.cached_slides || [];
        const totalWsi = data.total_wsi || 0;
        const folderName = data.folder_name || '';
        const isEn = (window.__lang || 'zh') === 'en';
        const modeName = data.mode === 'collaborative' 
            ? (isEn ? 'Collaborative Mode' : '协作模式') 
            : (isEn ? 'Auto Mode' : '自动模式');
        
        let slideList = cachedSlides.map(s => {
            const time = s.cached_at ? ` (${s.cached_at})` : '';
            return `  · ${s.part || s.filename}${time}`;
        }).join('\n');
        
        const message = isEn
            ? `Found ${cachedSlides.length}/${totalWsi} slide(s) with existing ${modeName} results:\n\n` +
              `${slideList}\n\n` +
              `Click "OK" → Skip these slides, only analyze remaining\n` +
              `Click "Cancel" → Re-analyze all`
            : `检测到 ${cachedSlides.length}/${totalWsi} 张切片已有${modeName}的分析结果：\n\n` +
              `${slideList}\n\n` +
              `点击"确定"→ 跳过这些切片，只分析剩余的\n` +
              `点击"取消"→ 全部重新分析`;
        
        const skipCached = await _copilotConfirm(message);
        if (
            String(window.pathchat?.currentPatientId || '') !== gatePatientId
            || String(this._activePatientRunToken || '') !== gateRunToken
        ) {
            console.warn('[Patient Level] Ignored decision from a retired cache prompt');
            return;
        }
        
        // 发送用户决定给后端
        this.socket.emit('patient_cache_decision', {
            action: skipCached ? 'skip_cached' : 'rerun_all',
            patient_id: gatePatientId,
            run_token: gateRunToken,
            cache_gate_id: cacheGateId,
        });
        
        console.log(`[Patient Level] 用户选择: ${skipCached ? '跳过已有结果' : '全部重新分析'}`);
        
        // ⭐ 如果用户选择跳过，纯前端加载所有缓存（不依赖后端重新分析）
        if (skipCached && window.pathchat) {
            await this.loadCachedPatientSlides(cachedSlides, totalWsi);
            // ⭐ 恢复聊天记录
            if (typeof window.pathchat._restorePatientLevelChatHistory === 'function') {
                const slides = cachedSlides.map(s => ({ file_id: s.file_id, filename: s.filename || s.part || '' }));
                window.pathchat._restorePatientLevelChatHistory(slides);
            }
        }
    }
    
    /**
     * ⭐ 处理全部切片均来自缓存的事件（后端已聚合树并返回 tree_url）
     */
    _patientEventMatchesActiveRun(data) {
        const eventPatientId = String(data?.patient_id || '');
        const currentPatientId = String(window.pathchat?.currentPatientId || '');
        if (currentPatientId && eventPatientId && eventPatientId !== currentPatientId) {
            return false;
        }
        const activeToken = String(this._activePatientRunToken || '');
        const eventToken = String(data?.run_token || '');
        if (activeToken && eventToken !== activeToken) {
            console.warn('[Patient Level] Ignored event outside the active run', {
                eventPatientId,
                eventToken,
            });
            return false;
        }
        return true;
    }

    _patientLevelPanelIsActive() {
        const panel = document.getElementById('patientLevelPanel');
        return Boolean(panel && panel.style.display !== 'none');
    }

    _fileIdForPatientWsi(index) {
        const n = Number(index);
        if (!Number.isInteger(n)) return '';
        const lists = [
            window.pathchat?.patientLevelUploadResults,
            window.pathchat?.plAllFileInfos,
        ];
        for (const list of lists) {
            const hit = (list || []).find((item) => Number(item?.index) === n);
            if (hit) return String(hit.file_id || hit.id || '');
        }
        return '';
    }

    // Single Agent roam keeps its FOV boxes in the per-file roam event store
    // (_patientRoamByFile), not in patientLevelAnalysisData. Slide switching
    // clears the canvas and can only redraw from analysis data, so a live roam
    // slide comes back blank. Rebuild the box descriptors straight from the
    // roam store, scoped to this slide, so switching keeps each slide's own
    // trajectory boxes (and never leaks another slide's coordinates).
    roamOverlaysForWsi(index) {
        const wsiIndex = this._asPatientWsiIndex(index);
        const fileId = String(this._fileIdForPatientWsi(wsiIndex) || '');
        const entry = fileId ? this._patientRoamByFile?.get(fileId) : null;
        const events = entry?.events;
        if (!Array.isArray(events) || !events.length) return [];
        const byId = new Map();
        for (const item of events) {
            if (item?.kind !== 'step') continue;
            const payload = item.payload || {};
            const step = Number(payload.step) || 0;
            if (step <= 0 || !payload.rect) continue;
            const overlayWsi = Number.isInteger(Number(payload.wsi_index))
                ? Number(payload.wsi_index)
                : wsiIndex;
            const id = `PATIENT-ROAM-${overlayWsi}-${step}`;
            byId.set(id, {
                id,
                rect: payload.rect,
                magnification: Number(payload.zoom) || 10,
                label: `S${step}`,
                wsiIndex: overlayWsi,
            });
        }
        return [...byId.values()];
    }

    _indexForPatientFile(fileId) {
        const id = String(fileId || '');
        if (!id) return null;
        const lists = [
            window.pathchat?.patientLevelUploadResults,
            window.pathchat?.plAllFileInfos,
        ];
        for (const list of lists) {
            const hit = (list || []).find((item) => String(item?.file_id || item?.id || '') === id);
            if (hit && Number.isInteger(Number(hit.index))) return Number(hit.index);
        }
        return null;
    }

    _inferPatientWsiFromImageUrl(url = '') {
        let raw = String(url || '').trim();
        if (!raw) return null;
        try { raw = decodeURIComponent(raw); } catch (_) {}
        const api = raw.match(/\/api\/thumbnail\/([^/?#]+)/i);
        if (api) {
            const idx = this._indexForPatientFile(api[1]);
            if (Number.isInteger(idx)) return idx;
        }
        const lists = [
            window.pathchat?.plAllFileInfos,
            window.pathchat?.patientLevelUploadResults,
        ];
        let best = null;
        let bestLen = 0;
        for (const list of lists) {
            for (const info of list || []) {
                const names = [
                    info.filename,
                    info.name,
                    info.original_name,
                    String(info.part || '').replace(/^Slide\s+\d+:\s*/i, ''),
                    info.file_id,
                ].map((value) => String(value || '').trim()).filter(Boolean);
                for (const name of names) {
                    const stem = name.replace(/\.(ndpi|svs|tif|tiff|mrxs)$/i, '');
                    if (stem.length < 2 || !raw.includes(stem) || stem.length <= bestLen) continue;
                    const idx = Number(info.index);
                    if (!Number.isInteger(idx)) continue;
                    best = idx;
                    bestLen = stem.length;
                }
            }
        }
        return best;
    }

    _scrubPatientSlideMaps() {
        if (!this._isPatientSwarmMapMode() || this._scrubbingPatientMaps) return;
        const host = document.getElementById('routeStepsList');
        if (!host) return;
        this._scrubbingPatientMaps = true;
        try {
            host.querySelectorAll(':scope > .patient-slide-map').forEach((map) => {
                const list = map.querySelector('.v2-agent-activity-list');
                if (!list) return;
                let owner = this._asPatientWsiIndex(map.dataset.wsiIndex);
                Array.from(list.querySelectorAll(':scope > .v2-agent-activity-item')).forEach((item) => {
                    const stamped = this._asPatientWsiIndex(item.dataset?.wsiIndex);
                    const img = item.querySelector('img.v2-agent-activity-thumbnail, img');
                    const fromImg = this._inferPatientWsiFromImageUrl(
                        img?.getAttribute?.('src') || img?.src || ''
                    );
                    if (Number.isInteger(fromImg) && stamped == null) owner = fromImg;
                    const destIndex = Number.isInteger(stamped)
                        ? stamped
                        : (Number.isInteger(fromImg) ? fromImg : owner);
                    if (!Number.isInteger(destIndex)) return;
                    item.dataset.wsiIndex = String(destIndex);
                    const mapWsi = Number(map.dataset.wsiIndex);
                    if (destIndex === mapWsi) return;
                    const destList = this._ensurePatientSlideMap(destIndex)
                        ?.querySelector('.v2-agent-activity-list');
                    if (destList && item.parentElement !== destList) destList.appendChild(item);
                });
            });
        } finally {
            this._scrubbingPatientMaps = false;
            this._syncPatientSlideMapVisibility(this.currentViewingWsiIndex);
        }
    }

    _patientRoamResolvedFileId(data = {}) {
        const fromData = String(data?.file_id || data?.wsi_context?.file_id || '');
        if (fromData) return fromData;
        const fromIndex = this._fileIdForPatientWsi(this._resolvePatientWsiIndex(data));
        if (fromIndex) return fromIndex;
        const fromEvent = this._fileIdForPatientWsi(this._patientSwarmMapWsiIndexFromEvent(data));
        if (fromEvent) return fromEvent;
        if (!this.isPatientLevelMode) {
            return String(this._activeRoamFileId || this.currentFileId || '');
        }
        return '';
    }

    _resolvePatientWsiIndex(data) {
        const fileId = String(data?.file_id || data?.wsi_context?.file_id || '');
        if (fileId) {
            const fromFile = this._indexForPatientFile(fileId);
            if (Number.isInteger(fromFile)) return fromFile;
        }
        if (data && Number.isFinite(Number(data.wsi_index))) {
            return Number(data.wsi_index);
        }
        if (this.isPatientLevelMode) return null;
        return this.currentAnalyzingWsiIndex;
    }

    _patientLiveEventMatchesViewer(data) {
        if (!this.isPatientLevelMode) return true;
        const eventFile = String(this._patientRoamResolvedFileId(data) || '');
        const current = window.pathchat?.currentFile || {};
        const currentId = String(
            current.file_id || current.id || window.pathchat?.currentFileId || ''
        );
        const eventWsi = this._patientSwarmMapWsiIndexFromEvent(data);
        const viewing = this._asPatientWsiIndex(this.currentViewingWsiIndex);
        if (this._isPatientParallelRun()) {
            if (Number.isInteger(eventWsi) && Number.isInteger(viewing) && eventWsi !== viewing) {
                return false;
            }
            if (eventFile && currentId) return eventFile === currentId;
            if (Number.isInteger(eventWsi) && Number.isInteger(viewing)) return eventWsi === viewing;
            return false;
        }
        if (eventFile && currentId) return currentId === eventFile;
        if (Number.isInteger(eventWsi) && Number.isInteger(viewing)) return eventWsi === viewing;
        return false;
    }

    handlePatientLevelAllCached(data) {
        console.log('[Patient Level] 全部切片已从缓存加载:', data);
        if (!this._patientEventMatchesActiveRun(data)) return;
        
        const patientId = data.patient_id;
        const treeUrl = data.tree_url || null;
        
        if (window.pathchat) {
            if (patientId) window.pathchat.currentPatientId = patientId;
            window.pathchat.patientTreeUrl = treeUrl;
            window.pathchat._treeLoadedForId = null;
        }

        if (data.summary || data.html_report_url || data.report_url) {
            this.handlePatientReport({ ...data, success: true });
        } else {
            const terminalKey = `all-cached:${String(patientId || '')}:${String(data?.run_token || '')}`;
            const alreadyRendered = this._patientTerminalUiKeys.has(terminalKey);
            this._patientTerminalUiKeys.add(terminalKey);
            window.pathchat?._setPatientLevelRunState?.(false, { patientId });
            this._activePatientRunToken = null;
            window.pathchat?.updatePlProgress?.(
                100,
                (window.__lang || 'zh') === 'en'
                    ? 'Cached evidence loaded; no patient report is available.'
                    : '缓存证据已加载；当前没有可恢复的患者终报。'
            );
            if (!alreadyRendered) {
                window.pathchat?.addChatMessage?.('assistant', `
                    <div class="wsi-analysis-msg success">
                        <span class="wsi-icon">✓</span>
                        <span>${(window.__lang || 'zh') === 'en'
                            ? 'All slide evidence was restored from cache. No patient-level report was found.'
                            : '全部切片证据已从缓存恢复，但未找到患者级终报。'}</span>
                    </div>
                `);
            }
        }
        
        console.log(`[Patient Level] ✅ patientTreeUrl 已设置: ${treeUrl}`);
    }
    
    /**
     * ⭐ 加载 Patient Level 缓存数据（公共方法，供缓存弹窗和"加载分析结果"按钮共用）
     * @param {Array} cachedSlides - [{file_id, index, part, filename, cached_at}]
     * @param {number} totalWsi - 总 WSI 数量
     */
    async loadCachedPatientSlides(cachedSlides, totalWsi) {
        if (!window.pathchat || !cachedSlides || cachedSlides.length === 0) return;
        
        const allCached = cachedSlides.length === totalWsi;
        
        if (!this.patientLevelAnalysisData) this.patientLevelAnalysisData = {};
        this.isPatientLevelMode = true;
        this._collectedChatHistories = [];
        
        for (const slide of cachedSlides) {
            const fileId = slide.file_id;
            const index = slide.index;
            console.log(`[Patient Level] 📦 加载缓存: ${slide.part || slide.filename} (${fileId})`);
            
            if (typeof window.pathchat.updatePlWsiStatus === 'function') {
                window.pathchat.updatePlWsiStatus(index, 'completed', 'check');
            }
            
            try {
                const cacheMode = this.activeAnalysisStrategy === 'single_agent_roam'
                    ? 'single_agent_roam'
                    : (this.isCollaborativeMode ? 'collaborative' : 'ai');
                const loadCache = async (mode) => {
                    const cacheResp = await fetch(`/api/load_analysis_state/${fileId}?mode=${mode}`);
                    return cacheResp.json();
                };
                let cacheResult = await loadCache(cacheMode);
                if (!cacheResult?.success || !cacheResult.data) {
                    for (const fallback of ['single_agent_roam', 'ai', 'collaborative']) {
                        if (fallback === cacheMode) continue;
                        cacheResult = await loadCache(fallback);
                        if (cacheResult?.success && cacheResult.data) break;
                    }
                }
                const stateData = cacheResult?.data;
                const hasRoam = Boolean(
                    stateData
                    && (
                        stateData.mode === 'single_agent_roam'
                        || stateData.analysisStrategy === 'single_agent_roam'
                        || (Array.isArray(stateData.trajectory) && stateData.trajectory.length)
                    )
                );
                if (cacheResult?.success && stateData && hasRoam) {
                    this.activeAnalysisStrategy = 'single_agent_roam';
                    this.patientLevelAnalysisData[index] = {
                        globalReasoning: stateData.globalReasoning || stateData.report || '',
                        report: stateData.report || '',
                        trajectory: Array.isArray(stateData.trajectory) ? stateData.trajectory : [],
                        roiOverlays: stateData.roiOverlays || [],
                        leftPanelHtml: stateData.leftPanelHtml || '',
                        roiAnalysisData: stateData.roiAnalysisData || {},
                        redBoxes: [],
                    };
                    if (stateData.chatHistory && stateData.chatHistory.length > 0) {
                        this._collectedChatHistories.push({
                            slideIndex: index,
                            slideName: slide.part || slide.filename || `Slide ${index + 1}`,
                            messages: stateData.chatHistory
                        });
                    }
                    console.log(`[Patient Level] ✅ 从 analysis_cache 加载游走轨迹: ${slide.part || slide.filename} (${(stateData.roiOverlays || []).length} 个框)`);
                    continue;
                }
                
                if (cacheResult?.success && stateData && stateData.roiOverlays && stateData.roiOverlays.length > 0) {
                    const cachedAnalysis = stateData.roiAnalysisData || {};
                    const hasAnalysis = Object.keys(cachedAnalysis).length > 0;
                    const wsiData = { globalReasoning: stateData.globalReasoning || '', redBoxes: [] };
                    
                    const redMap = {};
                    stateData.roiOverlays.forEach(o => {
                        const d = o.data || o;
                        if (d.color === '#FF0000') {
                            const obs = cachedAnalysis[d.id]?.analysis || '';
                            const conf = cachedAnalysis[d.id]?.confidence || 0;
                            redMap[d.id] = { id: d.id, rect: d.rect, status: 'completed', observation: obs, confidence: conf, blueBoxes: [] };
                        }
                    });
                    stateData.roiOverlays.forEach(o => {
                        const d = o.data || o;
                        if (d.color === '#0066FF') {
                            const parentId = d.id.split('-')[0];
                            if (redMap[parentId]) {
                                const obs = cachedAnalysis[d.id]?.analysis || '';
                                const conf = cachedAnalysis[d.id]?.confidence || 0;
                                redMap[parentId].blueBoxes.push({ id: d.id, rect: d.rect, status: 'completed', observation: obs, confidence: conf, greenBoxes: [] });
                            }
                        }
                    });
                    stateData.roiOverlays.forEach(o => {
                        const d = o.data || o;
                        if (d.color === '#00CC00') {
                            const parts = d.id.split('-');
                            const parentBlueId = parts.slice(0, 2).join('-');
                            const parentRedId = parts[0];
                            if (redMap[parentRedId]) {
                                const blueBox = redMap[parentRedId].blueBoxes.find(b => b.id === parentBlueId);
                                if (blueBox) {
                                    const obs = cachedAnalysis[d.id]?.analysis || '';
                                    const conf = cachedAnalysis[d.id]?.confidence || 0;
                                    blueBox.greenBoxes.push({ id: d.id, rect: d.rect, status: 'completed', observation: obs, confidence: conf });
                                }
                            }
                        }
                    });
                    wsiData.redBoxes = Object.values(redMap);
                    wsiData.leftPanelHtml = stateData.leftPanelHtml || '';
                    wsiData.roiAnalysisData = cachedAnalysis;
                    wsiData.roiOverlays = stateData.roiOverlays || [];
                    wsiData.report = stateData.report || '';
                    this.patientLevelAnalysisData[index] = wsiData;
                    
                    // 只有当 analysis_cache 中没有分析内容时，才回退到 analysis_tree.json（兼容旧数据）
                    if (!hasAnalysis) {
                        const slideName = encodeURIComponent(slide.filename || '');
                        const treeResp2 = await fetch(`/api/agent/analysis-tree/${fileId}?filename=${slideName}`);
                        const treeResult2 = await treeResp2.json();
                        if (treeResult2.success && treeResult2.data) {
                            const treeNodes = treeResult2.data.tree_nodes || [];
                            const metadata = treeResult2.data.metadata || {};
                            const richWsiData = { globalReasoning: metadata.global_analysis || '', redBoxes: [] };
                            treeNodes.forEach(redNode => {
                                const redBox = { id: redNode.roi_id || redNode.id, rect: redNode.rect, status: 'completed',
                                    observation: redNode.analysis?.reasoning || redNode.analysis?.findings || '', confidence: redNode.analysis?.confidence || 0, blueBoxes: [] };
                                (redNode.children || []).forEach(blueNode => {
                                    const blueBox = { id: blueNode.roi_id || blueNode.id, rect: blueNode.rect, status: 'completed',
                                        observation: blueNode.analysis?.reasoning || blueNode.analysis?.findings || '', confidence: blueNode.analysis?.confidence || 0, greenBoxes: [] };
                                    (blueNode.children || []).forEach(greenNode => {
                                        blueBox.greenBoxes.push({ id: greenNode.roi_id || greenNode.id, rect: greenNode.rect, status: 'completed',
                                            observation: greenNode.analysis?.reasoning || greenNode.analysis?.findings || '', confidence: greenNode.analysis?.confidence || 0 });
                                    });
                                    redBox.blueBoxes.push(blueBox);
                                });
                                richWsiData.redBoxes.push(redBox);
                            });
                            richWsiData.leftPanelHtml = stateData.leftPanelHtml || '';
                            richWsiData.roiAnalysisData = cachedAnalysis;
                            richWsiData.roiOverlays = stateData.roiOverlays || [];
                            richWsiData.report = stateData.report || '';
                            this.patientLevelAnalysisData[index] = richWsiData;
                            if (typeof window.pathchat.updatePlWsiAnalysisTree === 'function') {
                                window.pathchat.updatePlWsiAnalysisTree(index, richWsiData);
                            }
                            console.log(`[Patient Level] ✅ 从 analysis_tree.json 补充分析内容: ${slide.part || slide.filename}`);
                        }
                    } else {
                        if (typeof window.pathchat.updatePlWsiAnalysisTree === 'function') {
                            window.pathchat.updatePlWsiAnalysisTree(index, wsiData);
                        }
                    }
                    
                    if (stateData.chatHistory && stateData.chatHistory.length > 0) {
                        this._collectedChatHistories.push({
                            slideIndex: index,
                            slideName: slide.part || slide.filename || `Slide ${index + 1}`,
                            messages: stateData.chatHistory
                        });
                    }
                    
                    console.log(`[Patient Level] ✅ 从 analysis_cache 加载: ${slide.part || slide.filename} (${stateData.roiOverlays.length} 个框, ${hasAnalysis ? '含分析内容' : '无分析内容(旧格式)'})`);
                    continue;
                }
                
                if (cacheResult?.success && stateData) {
                    const html = String(stateData.leftPanelHtml || '');
                    const overview = String(stateData.globalReasoning || stateData.report || '');
                    if (html.length > 40 || overview.length > 20) {
                        this.patientLevelAnalysisData[index] = {
                            ...(this.patientLevelAnalysisData[index] || {}),
                            globalReasoning: overview || this.patientLevelAnalysisData[index]?.globalReasoning || '',
                            report: stateData.report || this.patientLevelAnalysisData[index]?.report || '',
                            leftPanelHtml: html || this.patientLevelAnalysisData[index]?.leftPanelHtml || '',
                            roiAnalysisData: stateData.roiAnalysisData
                                || this.patientLevelAnalysisData[index]?.roiAnalysisData
                                || {},
                            roiOverlays: stateData.roiOverlays
                                || this.patientLevelAnalysisData[index]?.roiOverlays
                                || [],
                            redBoxes: this.patientLevelAnalysisData[index]?.redBoxes || [],
                        };
                    }
                    if (stateData.chatHistory && stateData.chatHistory.length > 0) {
                        this._collectedChatHistories.push({
                            slideIndex: index,
                            slideName: slide.part || slide.filename || `Slide ${index + 1}`,
                            messages: stateData.chatHistory
                        });
                    }
                }

                const slideName = encodeURIComponent(slide.filename || '');
                const treeResp = await fetch(`/api/agent/analysis-tree/${fileId}?filename=${slideName}`);
                const treeResult = await treeResp.json();
                
                if (treeResult.success && treeResult.data) {
                    const treeData = treeResult.data;
                    const treeNodes = treeData.tree_nodes || [];
                    const metadata = treeData.metadata || {};
                    
                    const wsiData = {
                        globalReasoning: metadata.global_analysis || '',
                        redBoxes: []
                    };
                    
                    treeNodes.forEach(redNode => {
                        const redBox = {
                            id: redNode.roi_id || redNode.id,
                            rect: redNode.rect,
                            status: 'completed',
                            observation: redNode.analysis?.reasoning || redNode.analysis?.findings || '',
                            confidence: redNode.analysis?.confidence || 0,
                            blueBoxes: []
                        };
                        
                        (redNode.children || []).forEach(blueNode => {
                            const blueBox = {
                                id: blueNode.roi_id || blueNode.id,
                                rect: blueNode.rect,
                                status: 'completed',
                                observation: blueNode.analysis?.reasoning || blueNode.analysis?.findings || '',
                                confidence: blueNode.analysis?.confidence || 0,
                                greenBoxes: []
                            };
                            
                            (blueNode.children || []).forEach(greenNode => {
                                blueBox.greenBoxes.push({
                                    id: greenNode.roi_id || greenNode.id,
                                    rect: greenNode.rect,
                                    status: 'completed',
                                    observation: greenNode.analysis?.reasoning || greenNode.analysis?.findings || '',
                                    confidence: greenNode.analysis?.confidence || 0
                                });
                            });
                            
                            redBox.blueBoxes.push(blueBox);
                        });
                        
                        wsiData.redBoxes.push(redBox);
                    });
                    
                    const prior = this.patientLevelAnalysisData[index] || {};
                    this.patientLevelAnalysisData[index] = {
                        ...prior,
                        ...wsiData,
                        leftPanelHtml: prior.leftPanelHtml || '',
                        roiAnalysisData: prior.roiAnalysisData || {},
                        roiOverlays: prior.roiOverlays || [],
                        report: prior.report || '',
                    };
                    
                    if (typeof window.pathchat.updatePlWsiAnalysisTree === 'function') {
                        window.pathchat.updatePlWsiAnalysisTree(index, wsiData);
                    }
                    
                    console.log(`[Patient Level] ✅ 已加载: ${slide.part || slide.filename} (${wsiData.redBoxes.length} 红框)`);
                }
            } catch (err) {
                console.warn(`[Patient Level] ⚠️ 加载失败: ${slide.part || slide.filename}`, err);
            }
        }
        
        this.currentViewingWsiIndex = 0;
        this.currentAnalyzingWsiIndex = undefined;
        
        if (cachedSlides.length > 0) {
            const firstIndex = cachedSlides[0].index;
            this.currentViewingWsiIndex = firstIndex;
            
            if (typeof window.pathchat.togglePlWsiExpand === 'function') {
                window.pathchat.togglePlWsiExpand(firstIndex, true);
            }
            
            if (typeof window.pathchat.selectPlWsiItem === 'function') {
                window.pathchat.selectPlWsiItem(firstIndex);
            }
        }
        
        if (allCached) {
            if (typeof window.pathchat.updatePlProgress === 'function') {
                window.pathchat.updatePlProgress(100, __t('pl_all_cache_loaded'));
            }
            if (typeof window.pathchat.showNotification === 'function') {
                window.pathchat.showNotification('success', __t('pl_loaded_slides', cachedSlides.length));
            }
        }

        if (this.activeAnalysisStrategy !== 'single_agent_roam') {
            this._restorePatientSwarmMapsFromCache?.(this.currentViewingWsiIndex);
        }
        window.pathchat?._rememberLoadedPatientCacheAsWorkspace?.();
    }
    
    /**
     * 处理Patient Level进度更新
     */
    handlePatientProgress(data) {
        const progressPatientId = String(data?.patient_id || '');
        if (progressPatientId && window.pathchat?.currentPatientId
            && progressPatientId !== String(window.pathchat.currentPatientId)) return;
        const progressRunToken = String(data?.run_token || '');
        if (this._activePatientRunToken && progressRunToken
            && progressRunToken !== this._activePatientRunToken) return;
        console.log('[Patient Level] 进度更新:', data);
        
        const percentage = data.percentage || 0;
        const message = data.message || __t('observation_analyzing');
        const completedWsi = data.completed_wsi || 0;
        const totalWsi = data.total_wsi || 0;
        
        // 更新左侧面板进度
        if (window.pathchat && typeof window.pathchat.updatePlProgress === 'function') {
            window.pathchat.updatePlProgress(percentage, message);
        }
        
        // 更新WSI计数（左侧面板）
        const wsiCountEl = document.getElementById('plWsiCount');
        if (wsiCountEl) {
            wsiCountEl.textContent = `${completedWsi}/${totalWsi} ${__t('pl_wsi_suffix')}`;
        }
        const statusEl = document.getElementById('plStatus');
        if (statusEl && window.pathchat?.patientIsRunning) {
            statusEl.textContent = message;
        }
        // `agent.patient_stopped` is the single terminal UI event.  The
        // progress copy only updates counters and must not duplicate chat.
    }

    handlePatientStopping(data) {
        if (data?.patient_id && window.pathchat?.currentPatientId
            && data.patient_id !== window.pathchat.currentPatientId) return;
        const eventRunToken = String(data?.run_token || '');
        if (this._activePatientRunToken && eventRunToken !== this._activePatientRunToken) {
            console.warn('[Patient Level] Ignored stopping event without the active run token');
            return;
        }
        this.pausePatientRoamForStop();
        window.pathchat?._setPatientLevelRunState?.(true, {
            patientId: data?.patient_id || '',
            sessionId: this.sessionId || '',
            stopping: true,
        });
        window.pathchat?.addChatMessage?.('assistant', `
            <div class="wsi-analysis-msg warning">
                <span class="wsi-icon">⏹</span>
                <span>${(window.__lang || 'zh') === 'en'
                    ? 'Stop accepted. The current model request will finish, then the patient workflow will stop without generating a final report.'
                    : '已接受停止请求。当前模型请求返回后，患者流程将停止且不会生成最终报告。'}</span>
            </div>
        `);
        window.pathchat?._announcePatientStopStatus?.(
            (window.__lang || 'zh') === 'en'
                ? 'Stop accepted. The current model request will finish before the workflow stops.'
                : '停止请求已接受；当前模型请求结束后，工作流将停止。'
        );
    }

    handlePatientStopped(data) {
        if (data?.patient_id && window.pathchat?.currentPatientId
            && data.patient_id !== window.pathchat.currentPatientId) return;
        const eventRunToken = String(data?.run_token || '');
        if (this._activePatientRunToken && eventRunToken !== this._activePatientRunToken) {
            console.warn('[Patient Level] Ignored stopped event without the active run token');
            return;
        }
        this.pausePatientRoamForStop();
        this._activePatientRoamRunToken = null;
        window.pathchat?._setPatientLevelRunState?.(false, {
            patientId: data?.patient_id || '',
        });
        const statusEl = document.getElementById('plStatus');
        if (statusEl) statusEl.textContent = (window.__lang || 'zh') === 'en' ? 'Stopped' : '已停止';
        const terminalKey = [
            'stopped',
            String(data?.patient_id || window.pathchat?.currentPatientId || ''),
            String(data?.run_token || 'legacy'),
        ].join(':');
        if (this._patientTerminalUiKeys.has(terminalKey)) return;
        this._patientTerminalUiKeys.add(terminalKey);
        window.pathchat?.addChatMessage?.('assistant', `
            <div class="wsi-analysis-msg warning">
                <span class="wsi-icon">✓</span>
                <span>${(window.__lang || 'zh') === 'en'
                    ? 'Patient-level analysis stopped. No final patient report was generated.'
                    : '患者级分析已停止，未生成患者级最终报告。'}</span>
            </div>
        `);
        window.pathchat?._announcePatientStopStatus?.(
            (window.__lang || 'zh') === 'en'
                ? 'Patient-level analysis stopped. No final patient report was generated.'
                : '患者级分析已停止，未生成患者级最终报告。'
        );
    }

    handlePatientRoamEvent(kind, data) {
        const patientId = String(data?.patient_id || '');
        const currentPatientId = String(window.pathchat?.currentPatientId || '');
        if (!patientId || !currentPatientId || patientId !== currentPatientId) return;
        const fileId = String(data?.file_id || '');
        const runToken = String(data?.run_token || '');
        if (!fileId || !runToken) return;
        if (this._activePatientRunToken && runToken !== this._activePatientRunToken) return;
        if (this._activePatientTrajectoryOwner && this._activePatientTrajectoryOwner !== patientId) {
            this._patientRoamByFile.clear();
            this._patientRoamEventKeys.clear();
            this._patientRoamDisplayedKeys.clear();
        }
        this._activePatientTrajectoryOwner = patientId;
        const step = Number(data?.step) || 0;
        const key = `${patientId}:${runToken}:${fileId}:${kind}:${step}`;
        if (this._patientRoamEventKeys.has(key)) return;
        this._patientRoamEventKeys.add(key);
        const existing = this._patientRoamByFile.get(fileId);
        const entry = existing?.runToken === runToken
            ? existing
            : { runToken, events: [], status: 'running' };
        entry.events.push({ kind, payload: { ...data } });
        entry.events = entry.events.slice(-100);
        entry.status = data?.status || (kind === 'done' ? 'completed' : 'running');
        this._patientRoamByFile.set(fileId, entry);

        const index = Number(data?.wsi_index);
        if (Number.isInteger(index) && window.pathchat?.updatePlWsiStatus) {
            const doneStatus = String(data?.status || 'completed');
            const cardStatus = kind === 'done'
                ? (doneStatus === 'failed'
                    ? 'error'
                    : (doneStatus === 'stopped' ? 'stopped' : 'completed'))
                : 'analyzing';
            window.pathchat.updatePlWsiStatus(
                index,
                cardStatus,
                kind === 'done'
                    ? (doneStatus === 'failed' ? 'times' : (doneStatus === 'stopped' ? 'stop' : 'check'))
                    : 'route'
            );
        }
        const matchesViewer = String(this.currentFileId || '') === fileId;
        const viewer = this.viewer || window.pathchat?.viewer;
        const viewerReady = Boolean(viewer?.world?.getItemCount?.());
        if (matchesViewer && !viewerReady) {
            // Cache only.  The viewer `open` callback replays this event after
            // the correct slide coordinate system is available.
            return;
        }

        const projected = {
            ...data,
            runtime: { run_id: `patient:${runToken}:${fileId}` },
        };
        this._patientRoamDisplayedKeys.add(key);
        if (matchesViewer) this._activePatientRoamRunToken = runToken;
        if (kind === 'started') this.handleRoamStarted(projected);
        if (kind === 'action') this.handleRoamAction(projected);
        if (kind === 'step') this.handleRoamStep(projected);
        if (kind === 'done') {
            this.handleRoamDone(projected);
            if (matchesViewer) {
                this._patientRoamPausedKeys.delete(`${fileId}:${runToken}`);
                this._persistPatientRoamPausedKeys();
                this._activePatientRoamRunToken = null;
            }
        }
    }

    _restorePatientRoamWhenViewerReady(fileId) {
        const normalizedFileId = String(fileId || '');
        if (!normalizedFileId || normalizedFileId !== String(this.currentFileId || '')) return;
        const viewer = this.viewer || window.pathchat?.viewer;
        const ready = Boolean(viewer?.world?.getItemCount?.() > 0);
        if (ready) {
            this._restorePatientRoamForFile(normalizedFileId, {
                skipOverlays: this._patientWorkspaceHtmlRestored === true,
            });
        }
    }

    notifyViewerOpenedForFile(fileId) {
        this._restorePatientRoamWhenViewerReady(fileId);
        const normalizedFileId = String(fileId || '');
        const cachedReview = this._reviewQueuesByFile.get(normalizedFileId);
        if (cachedReview) {
            this.handleReviewQueue({ ...cachedReview, _restored_from_cache: true });
        }
    }

    _eventsFromPatientTrajectory(fileId, wsiIndex) {
        const data = this.patientLevelAnalysisData?.[wsiIndex] || {};
        const raw = Array.isArray(data.trajectory) ? data.trajectory : [];
        if (!raw.length) return [];
        const infos = window.pathchat?.patientLevelUploadResults || window.pathchat?.plAllFileInfos || [];
        const info = infos.find((item) => Number(item?.index) === Number(wsiIndex)) || {};
        const part = info.part || info.filename || '';
        const base = {
            file_id: String(fileId || ''),
            wsi_index: Number(wsiIndex),
            part,
            filename: info.filename || '',
        };
        const steps = raw.map((item) => (item?.payload ? { ...item, ...item.payload } : item));
        const events = [];
        let emittedStart = false;
        steps.forEach((step) => {
            const payload = {
                ...base,
                step: Number(step.step) || 0,
                zoom: step.zoom,
                rect: step.rect,
                observation: step.observation || '',
                why: step.why || step.next?.why || '',
                x: step.x,
                y: step.y,
                label: step.label,
            };
            if (payload.step === 0) {
                if (!emittedStart) {
                    events.push({ kind: 'started', payload });
                    emittedStart = true;
                }
                events.push({ kind: 'step', payload });
                return;
            }
            events.push({ kind: 'action', payload });
            events.push({ kind: 'step', payload });
        });
        if (!emittedStart) {
            events.unshift({ kind: 'started', payload: { ...base, step: 0 } });
        }
        events.push({
            kind: 'done',
            payload: {
                ...base,
                steps: steps.filter((item) => Number(item.step) > 0).length,
                status: 'completed',
                stopped_by: 'agent',
            },
        });
        return events;
    }

    _ensurePatientRoamStoreForFile(fileId, wsiIndex) {
        const id = String(fileId || '');
        if (!id) return null;
        const existing = this._patientRoamByFile.get(id);
        if (existing?.events?.length) return existing;
        const events = this._eventsFromPatientTrajectory(id, wsiIndex);
        if (!events.length) return existing || null;
        const entry = {
            runToken: this._activePatientRunToken || 'workspace-restore',
            events,
            status: 'completed',
        };
        this._patientRoamByFile.set(id, entry);
        return entry;
    }

    _clearPatientActivityForWsi(index) {
        const wsiIndex = Number(index);
        const list = this._patientActivityListEl();
        if (!list || !Number.isInteger(wsiIndex)) return;
        this._tagPatientActivityItems(list);
        Array.from(list.querySelectorAll('.v2-agent-activity-item')).forEach((el) => {
            if (this._parsePatientActivityWsiIndex(el) === wsiIndex) el.remove();
        });
    }

    _clearPatientRoamUiForFile(fileId, wsiIndex) {
        const id = String(fileId || '');
        if (id) {
            for (const key of [...this._patientRoamDisplayedKeys]) {
                if (String(key).includes(`:${id}:`) || String(key).includes(`:${id}`)) {
                    this._patientRoamDisplayedKeys.delete(key);
                }
            }
            for (const key of [...this._roamUiEventKeys]) {
                if (String(key).includes(id)) this._roamUiEventKeys.delete(key);
            }
            if (this.activityDetailKeys) {
                for (const key of [...this.activityDetailKeys]) {
                    if (String(key).includes(id)) this.activityDetailKeys.delete(key);
                }
            }
            if (this.activityImageKeys) {
                const encoded = encodeURIComponent(id);
                for (const key of [...this.activityImageKeys]) {
                    if (String(key).includes(id) || String(key).includes(encoded)) {
                        this.activityImageKeys.delete(key);
                    }
                }
            }
        }
        this._clearPatientActivityForWsi(wsiIndex);
    }

    _restorePatientRoamForFile(fileId, options = {}) {
        const forceRebuild = options.forceRebuild === true;
        const skipOverlays = options.skipOverlays === true;
        const id = String(fileId || '');
        if (!id) return;
        if (this.activeAnalysisStrategy === 'agent_swarm') return;
        const wsiIndex = this._indexForPatientFile(id);
        this._ensurePatientRoamStoreForFile(id, wsiIndex);
        const entry = this._patientRoamByFile.get(id);
        if (!entry?.events?.length) {
            if (Number.isInteger(wsiIndex)) this._filterPatientActivityToWsi(wsiIndex);
            return;
        }
        if (
            !forceRebuild
            && !this._patientWorkspaceHtmlRestored
            && this._activePatientRunToken
            && entry.runToken
            && entry.runToken !== this._activePatientRunToken
            && entry.runToken !== 'workspace-restore'
        ) return;
        if (this._patientWorkspaceHtmlRestored && !forceRebuild) {
            this._tagPatientActivityItems();
            if (Number.isInteger(wsiIndex) && this._patientActivityCountForWsi(wsiIndex) > 0) {
                this._filterPatientActivityToWsi(wsiIndex);
                return;
            }
        }
        if (forceRebuild || (this._patientWorkspaceHtmlRestored && this._patientActivityCountForWsi(wsiIndex) === 0)) {
            this._clearPatientRoamUiForFile(id, wsiIndex);
        }
        this.activeAnalysisStrategy = 'single_agent_roam';
        this._activePatientRoamRunToken = entry.status === 'running' ? entry.runToken : null;
        this._activeRoamFileId = String(id);
        this._activeRoamSessionId = this.sessionId;
        const pauseKey = `${id}:${entry.runToken}`;
        const userPaused = this._patientRoamPausedKeys.has(pauseKey) || this._patientStopLatch;
        this.isFollowing = entry.status === 'running' && !userPaused;
        let lastAction = null;
        for (const item of entry.events) {
            const payload = item?.payload || {};
            if (item?.kind === 'action' && payload.rect) lastAction = payload;
            if (
                !skipOverlays
                && item?.kind === 'step'
                && Number(payload.step) > 0
                && payload.rect
                && window.pathchat?.drawColoredROI
            ) {
                window.pathchat.drawColoredROI({
                    id: `PATIENT-ROAM-${payload.wsi_index}-${payload.step}`,
                    rect: payload.rect,
                    magnification: Number(payload.zoom) || 10,
                    label: `S${payload.step}`,
                });
            }
            const step = Number(payload.step) || 0;
            const displayKey = `${window.pathchat?.currentPatientId || ''}:${entry.runToken}:${id}:${item?.kind}:${step}`;
            if (this._patientRoamDisplayedKeys.has(displayKey)) continue;
            const projected = {
                ...payload,
                file_id: payload.file_id || id,
                wsi_index: Number.isInteger(Number(payload.wsi_index)) ? Number(payload.wsi_index) : wsiIndex,
                runtime: { run_id: `patient:${entry.runToken}:${id}` },
                _restored_from_snapshot: true,
                _restored_no_animation: true,
            };
            if (item?.kind === 'started') this.handleRoamStarted(projected);
            if (item?.kind === 'action') this.handleRoamAction(projected);
            if (item?.kind === 'step') this.handleRoamStep(projected);
            if (item?.kind === 'done') this.handleRoamDone(projected);
            this._patientRoamDisplayedKeys.add(displayKey);
        }
        if (userPaused) {
            this.isFollowing = false;
            if (!this._patientStopLatch) this._showRoamFollowResumeControl();
        } else if (lastAction?.rect && entry.status === 'running' && this._patientLiveEventMatchesViewer({ file_id: id })) {
            this.isFollowing = true;
            this._animateRoamViewport(lastAction.rect, lastAction.zoom);
        }
        this._appendActivityItem({
            title: (window.__lang || 'zh') === 'en'
                ? 'Patient Single Agent trajectory restored'
                : '已恢复该切片的单 Agent 游走轨迹',
            detail: (window.__lang || 'zh') === 'en'
                ? `${entry.events.filter(item => item.kind === 'step').length} observed step(s).`
                : `已恢复 ${entry.events.filter(item => item.kind === 'step').length} 个观察步骤。`,
            state: entry.status === 'running'
                ? 'running'
                : (entry.status === 'stopped' ? 'stopped' : 'done'),
            key: `patient-roam-restore:${entry.runToken}:${id}`,
            wsiIndex: Number.isInteger(wsiIndex)
                ? wsiIndex
                : this._resolvePatientWsiIndex(entry.events.find((item) => item?.payload)?.payload || { file_id: id }),
        });
        if (Number.isInteger(wsiIndex)) this._filterPatientActivityToWsi(wsiIndex);
    }

    async restorePatientLevelSnapshot(patientId) {
        const expectedPatientId = String(patientId || '');
        if (!expectedPatientId) return;
        const epoch = (this._patientSnapshotEpoch || 0) + 1;
        this._patientSnapshotEpoch = epoch;
        try {
            const response = await fetch(
                `/api/patient/${encodeURIComponent(expectedPatientId)}/status`
            );
            const snapshot = await response.json();
            if (!response.ok || !snapshot?.success) {
                if (
                    response.status === 404
                    && String(window.pathchat?.currentPatientId || '') === expectedPatientId
                ) {
                    window.pathchat?._setPatientLevelRunState?.(false, {
                        patientId: expectedPatientId,
                    });
                }
                return;
            }
            if (
                epoch !== this._patientSnapshotEpoch
                || String(window.pathchat?.currentPatientId || '') !== expectedPatientId
            ) return;
            const terminal = ['completed', 'failed', 'stopped', 'cancelled'].includes(
                String(snapshot.status || '')
            );
            const snapshotRunToken = String(snapshot.run_token || '');
            if (snapshotRunToken) this._activePatientRunToken = snapshotRunToken;
            if (window.pathchat) {
                window.pathchat.setPatientLevelMode?.(snapshot.mode || 'ai');
                window.pathchat.setPatientWsiExecutionMode?.(
                    snapshot.wsi_execution_mode || 'parallel'
                );
            }
            // A terminal/stopping Patient snapshot may still contain a last
            // per-slide roam entry marked "running".  Restore its boxes, but
            // never let that stale child status restart camera following.
            this._patientStopLatch = terminal || snapshot.status === 'stopping';
            if (this._activePatientTrajectoryOwner
                && this._activePatientTrajectoryOwner !== expectedPatientId) {
                this._patientRoamByFile.clear();
                this._patientRoamEventKeys.clear();
                this._patientRoamDisplayedKeys.clear();
            }
            this._activePatientTrajectoryOwner = expectedPatientId;
            window.pathchat?._setPatientLevelRunState?.(!terminal, {
                patientId: expectedPatientId,
                sessionId: snapshot.session_id || this.sessionId,
                stopping: snapshot.status === 'stopping',
            });
            for (const [fileId, raw] of Object.entries(snapshot.roam_by_file || {})) {
                const runToken = String(raw?.run_token || snapshot.run_token || '');
                if (!runToken) continue;
                const entry = { runToken, events: [], status: raw?.status || 'running' };
                for (const item of raw?.events || []) {
                    if (!item?.kind || !item?.payload) continue;
                    entry.events.push({ kind: item.kind, payload: { ...item.payload } });
                    const step = Number(item.payload.step) || 0;
                    this._patientRoamEventKeys.add(
                        `${expectedPatientId}:${runToken}:${fileId}:${item.kind}:${step}`
                    );
                }
                this._patientRoamByFile.set(String(fileId), entry);
            }
            this._restorePatientRoamWhenViewerReady(this.currentFileId);
            const reviewSnapshot = snapshot.review_snapshot;
            if (reviewSnapshot && String(reviewSnapshot.run_token || '') === snapshotRunToken) {
                for (const review of reviewSnapshot.reviews || []) {
                    this.handlePatientSlideReview({
                        patient_id: expectedPatientId,
                        run_token: snapshotRunToken,
                        review,
                    });
                }
                this.handlePatientReviewGate({
                    ...reviewSnapshot,
                    patient_id: expectedPatientId,
                    run_token: snapshotRunToken,
                });
            }
            if (
                snapshot.status === 'awaiting_patient_report_review'
                && snapshot.pending_final_report
                && snapshotRunToken
            ) {
                this.handlePatientReportReview({
                    patient_id: expectedPatientId,
                    run_token: snapshotRunToken,
                    folder_name: snapshot.folder_name || expectedPatientId,
                    report: snapshot.pending_final_report,
                    total_wsi: snapshot.total_wsi || 0,
                    _restored_from_snapshot: true,
                });
            }
            const artifacts = snapshot.artifacts || {};
            if (snapshot.status === 'completed' && snapshotRunToken) {
                const completionPayload = {
                    patient_id: expectedPatientId,
                    run_token: snapshotRunToken,
                    status: 'completed',
                    tree_url: artifacts.tree_url || null,
                    report_url: artifacts.report_url || null,
                    html_report_url: artifacts.html_report_url || null,
                    summary: artifacts.summary || '',
                    _restored_from_snapshot: true,
                };
                if (artifacts.has_report) {
                    this.handlePatientReport(completionPayload);
                } else if (artifacts.all_cached) {
                    this.handlePatientLevelAllCached(completionPayload);
                } else {
                    window.pathchat?._setPatientLevelRunState?.(false, {
                        patientId: expectedPatientId,
                    });
                    if (window.pathchat) window.pathchat.patientTreeUrl = artifacts.tree_url || null;
                    this._activePatientRunToken = null;
                }
            }
            if (snapshot.status === 'stopped') {
                this.handlePatientStopped({
                    patient_id: expectedPatientId,
                    status: 'stopped',
                    run_token: snapshot.run_token || 'snapshot',
                    _restored_from_snapshot: true,
                });
            }
        } catch (error) {
            console.debug('Patient Level snapshot restore skipped:', error);
        }
    }

    /**
     * Patient-level collaborative mode: a completed WSI remains a draft until
     * the supervising pathologist explicitly confirms its review report.
     */
    handlePatientSlideReview(data) {
        console.log('[Patient Level] 切片复核项已就绪:', data);
        if (!this._patientEventMatchesActiveRun(data)) return;
        const review = { ...(data?.review || data) };
        review.run_token = String(data?.run_token || review.run_token || '');
        if (!review || !window.pathchat) return;
        if (typeof window.pathchat.renderPatientSlideReview === 'function') {
            window.pathchat.renderPatientSlideReview(review);
        }
        if (typeof window.pathchat.updatePatientReviewGate === 'function') {
            const knownReviews = Object.values(window.pathchat.patientSlideReviews || {});
            const total = window.pathchat.plAllFileInfos?.length || knownReviews.length;
            const confirmed = knownReviews.filter(item => item?.status === 'confirmed').length;
            const revising = knownReviews.filter(item => item?.status === 'revising').length;
            window.pathchat.updatePatientReviewGate({
                status: confirmed === total && total > 0 ? 'ready' : 'awaiting_review',
                total,
                confirmed,
                pending: Math.max(0, total - confirmed),
                revising
            });
        }
    }

    handlePatientReviewUpdated(data) {
        console.log('[Patient Level] 切片复核状态更新:', data);
        if (!this._patientEventMatchesActiveRun(data)) return;
        if (!window.pathchat) return;
        if (data?.review && typeof window.pathchat.renderPatientSlideReview === 'function') {
            window.pathchat.renderPatientSlideReview(data.review);
        }
        if (data?.summary && typeof window.pathchat.updatePatientReviewGate === 'function') {
            window.pathchat.updatePatientReviewGate(data.summary);
        }
    }

    handlePatientReviewGate(data) {
        console.log('[Patient Level] 逐片复核门控:', data);
        if (!this._patientEventMatchesActiveRun(data)) return;
        if (window.pathchat && typeof window.pathchat.updatePatientReviewGate === 'function') {
            window.pathchat.updatePatientReviewGate(data);
        }
        if (
            data?.hold_next_slide
            && data?.status === 'awaiting_review'
            && !this._serialReviewHoldAnnounced
            && window.pathchat?.addChatMessage
        ) {
            this._serialReviewHoldAnnounced = true;
            const isEn = (window.__lang || 'zh') === 'en';
            window.pathchat.addChatMessage(
                'assistant',
                isEn
                    ? 'Serial collaboration: confirm this slide before the next one starts.'
                    : '串行协作：请先确认当前切片，确认后才会开始下一张。'
            );
        }
    }
    
    /**
     * 处理单个WSI开始分析
     */
    _completeRedWorkspaceResult(data = {}) {
        return this._withPatientSwarmMapContext(data, () => {
        const redId = String(data.roi_id || data.red_id || '').trim();
        if (!redId) return;
        this._setSubagentState(redId, 'done');
        const observation = data.reasoning || data.observation || data.findings || '';
        const rect = data.rect || this._resolveRoiRect(redId);
        const list = document.getElementById('routeStepsList');
        const existing = this._findMappedTreeCard(redId)
            || this._findTreeNodeByRoiId(list, redId, '.route-tree-node[data-roi-id], .collab-task-card[data-roi-id]');
        if (existing) {
            this.updateRouteStepStatus(redId, 'completed', observation, rect, observation);
            this._completeActivityItemForRoi(redId, {
                title: `${redId} ${__t('status_completed')}`,
                detail: String(observation || '').replace(/\s+/g, ' ').slice(0, 220)
            });
            return;
        }
        if (this.currentMode === 'collaborative' || this.isCollaborativeMode) return;
        this.appendRouteStep({
            level: 'red',
            roi_id: redId,
            title: `${redId} (10×)`,
            content: observation,
            reasoning: observation,
            rect,
            status: 'completed'
        });
        this._completeActivityItemForRoi(redId, {
            title: `${redId} ${__t('status_completed')}`,
            detail: String(observation || '').replace(/\s+/g, ' ').slice(0, 220)
        });
        });
    }

    /**
     * ⭐ Patient Level 模式：处理红框分析结果
     */
    /**
     * ⭐ 让每个框在自己的「分析完成」事件里保证自己已画在 viewer 上。
     *
     * 单切片 Swarm 的蓝/绿框原先只靠 agent.viewport 的累积 overlays 画出来，
     * 红框是并行线程，任一 viewport 事件丢失/被覆盖就永久缺框（患者级与协作
     * 模式另有 phase2/phase3 专门画蓝绿框，所以只有单切片会缺）。
     *
     * 这里只填真正的空洞：该坐标已有框就跳过，不重复画、不改已有框的 ID，
     * 别名改名仍由 _retargetRoiOverlay 负责。
     */
    _drawLiveRoiBoxes(data, entries = []) {
        if (!Array.isArray(entries) || !entries.length) return;
        const pathchat = window.pathchat;
        if (!pathchat?.viewer || typeof pathchat.drawColoredROI !== 'function') return;
        if (typeof pathchat._hasOverlayAtRect !== 'function') return;
        if (this.isPatientLevelMode && !this._patientLiveEventMatchesViewer(data)) return;
        const wsiIndex = this._patientSwarmMapWsiIndexFromEvent(data);
        entries.forEach((entry) => {
            const roiId = String(entry?.id || '').trim();
            const rect = entry?.rect;
            const magnification = Number(entry?.magnification);
            if (!roiId || !rect || rect.x === undefined || rect.y === undefined) return;
            if (pathchat._hasOverlayAtRect(rect, magnification)) return;
            try {
                // label 留空：与后端 create_roi_overlay 一致，纯线框不盖标签色块。
                pathchat.drawColoredROI({
                    id: roiId,
                    rect,
                    magnification,
                    color: magnification >= 160
                        ? '#00CC00'
                        : (magnification >= 40 ? '#0066FF' : '#FF0000'),
                    label: '',
                    ...(Number.isInteger(wsiIndex) ? { wsiIndex } : {})
                });
                console.log(`🧩 实时补画缺失标注框: ${roiId} (${magnification}×)`);
            } catch (err) {
                console.warn(`⚠️ 实时补画标注框失败: ${roiId}`, err);
            }
        });
    }

    handleRedAnalysis(data) {
        console.log('[Patient Level] 红框分析结果:', data);
        this._completeRedWorkspaceResult(data);
        // 红框自身 + 它刚选出的蓝框，缺哪个补哪个（blue_rects 用的是规范 ID）。
        this._drawLiveRoiBoxes(data, [
            { id: data.roi_id, rect: data.rect, magnification: 10 },
            ...(Array.isArray(data.blue_rects)
                ? data.blue_rects.map((region) => ({
                    id: region?.id || region?.roi_id,
                    rect: region?.rect,
                    magnification: 40,
                }))
                : []),
        ]);
        const redRoiId = String(data.roi_id || '').trim();
        if (redRoiId) {
            this._setSubagentState(redRoiId, 'done');
            const childCount = data.blue_count ?? data.blue_rects?.length ?? data.blue_ids?.length ?? 0;
            this._appendRedSubagentChatStatus(redRoiId, childCount);
        }
        const dispatchedBlueIds = Array.from(new Set([
            ...(Array.isArray(data.blue_ids) ? data.blue_ids : []),
            ...(Array.isArray(data.blue_rects)
                ? data.blue_rects.map(region => region?.id || region?.roi_id)
                : [])
        ].map(id => String(id || '').trim()).filter(Boolean)));
        if (dispatchedBlueIds.length) {
            const blueState = (this.currentMode === 'collaborative' || this.isCollaborativeMode)
                ? 'pending'
                : 'running';
            this._configureSubagentBoard(dispatchedBlueIds, 0, blueState);
            this._registerPendingBlueAliases(redRoiId, dispatchedBlueIds);
        }
        
        // ⭐⭐⭐ 关键修复：从 DOM 推断 Patient Level 模式（解决事件竞争条件）⭐⭐⭐
        if (!this.isPatientLevelMode) {
            const plPanel = document.getElementById('patientLevelPanel');
            if (plPanel && plPanel.style.display !== 'none') {
                this.isPatientLevelMode = true;
                console.log('🔧 [handleRedAnalysis] 从 DOM 推断启用 Patient Level 模式');
            } else {
                return;  // 确实不是 Patient Level 模式
            }
        }
        
        // ⭐⭐⭐ 关键修复：从展开的 WSI 卡片推断 currentAnalyzingWsiIndex ⭐⭐⭐
        let wsiIndex = this._patientSwarmMapWsiIndexFromEvent(data);
        if (!Number.isInteger(wsiIndex)) {
            const expandedCard = document.querySelector('.pl-wsi-card.expanded');
            if (expandedCard) {
                const body = expandedCard.querySelector('.pl-wsi-body');
                if (body && body.dataset.index !== undefined) {
                    wsiIndex = parseInt(body.dataset.index) || 0;
                    this.currentAnalyzingWsiIndex = wsiIndex;
                    console.log(`🔧 [handleRedAnalysis] 从展开的卡片推断 WSI index=${wsiIndex}`);
                }
            }
            if (!Number.isInteger(wsiIndex)) {
                wsiIndex = 0;
                this.currentAnalyzingWsiIndex = 0;
                console.log('🔧 [handleRedAnalysis] 默认使用 WSI index=0');
            }
        }
        
        // 确保有该 WSI 的数据存储
        if (!this.patientLevelAnalysisData) {
            this.patientLevelAnalysisData = {};
        }
        if (!this.patientLevelAnalysisData[wsiIndex]) {
            this.patientLevelAnalysisData[wsiIndex] = { redBoxes: [] };
        }
        
        const wsiData = this.patientLevelAnalysisData[wsiIndex];
        const roiId = data.roi_id;
        const observation = data.observation || '';
        
        // 查找或创建红框
        let redBox = wsiData.redBoxes.find(r => r.id === roiId);
        if (!redBox) {
            redBox = {
                id: roiId,
                rect: data.rect,  // ⭐ 保存红框坐标
                status: 'completed',
                observation: observation,
                confidence: data.confidence || 0,
                blueBoxes: []
            };
            wsiData.redBoxes.push(redBox);
        } else {
            redBox.status = 'completed';
            redBox.observation = observation;
            redBox.confidence = data.confidence || 0;
            if (data.rect) redBox.rect = data.rect;  // ⭐ 更新坐标
        }
        
        // 添加蓝框（包含坐标）
        if (data.blue_rects && data.blue_rects.length > 0) {
            data.blue_rects.forEach(blueInfo => {
                let blueBox = redBox.blueBoxes.find(b => b.id === blueInfo.id);
                if (!blueBox) {
                    redBox.blueBoxes.push({
                        id: blueInfo.id,
                        rect: blueInfo.rect,  // ⭐ 保存蓝框坐标
                        status: 'analyzing',
                        observation: '',
                        greenBoxes: []
                    });
                } else if (blueInfo.rect) {
                    blueBox.rect = blueInfo.rect;  // ⭐ 更新坐标
                }
            });
        } else if (data.blue_ids && data.blue_ids.length > 0) {
            // 兼容旧格式
            data.blue_ids.forEach(blueId => {
                if (!redBox.blueBoxes.find(b => b.id === blueId)) {
                    redBox.blueBoxes.push({
                        id: blueId,
                        status: 'analyzing',
                        observation: '',
                        greenBoxes: []
                    });
                }
            });
        }
        
        // ⭐⭐⭐ 核心修复：确保 UI 更新成功（解决 DOM 未就绪问题）
        const tryUpdateRedUI = () => {
            if (window.pathchat && typeof window.pathchat.updatePlWsiAnalysisTree === 'function') {
                const body = document.querySelector(`.pl-wsi-body[data-index="${wsiIndex}"]`);
                if (body && body.querySelector('.analysis-tree')) {
                    window.pathchat.updatePlWsiAnalysisTree(wsiIndex, wsiData);
                    console.log(`📊 [handleRedAnalysis] UI 更新成功 - ${roiId}`);
                    return true;
                }
            }
            return false;
        };
        
        if (!tryUpdateRedUI()) {
            console.log(`⏳ [handleRedAnalysis] DOM 未就绪，100ms 后重试 - ${roiId}`);
            setTimeout(() => tryUpdateRedUI(), 100);
        }
        this._seedPatientSwarmMapFromData(wsiIndex, { replace: false });
    }
    
    /**
     * 绿框 160× 结果：单切片 Swarm 与 Patient Level 共用。
     * 必须先写入中间栏树，再决定是否更新多切片面板。
     */
    _completeActivityItemForRoi(roiId, { title = '', detail = '' } = {}) {
        const list = document.querySelector('#routeStepsList .v2-agent-activity-list')
            || document.querySelector('.v2-agent-activity-list');
        if (!list || !roiId) return;
        const escaped = (window.CSS && typeof window.CSS.escape === 'function')
            ? window.CSS.escape(roiId)
            : String(roiId).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
        const item = list.querySelector(`.v2-agent-activity-item[data-roi-id="${escaped}"]`);
        if (!item) return;
        item.classList.remove('is-running', 'is-pending');
        item.classList.add('is-done');
        const heading = item.querySelector('strong');
        if (heading && title) heading.textContent = title;
        if (detail) {
            let paragraph = item.querySelector('p');
            if (!paragraph) {
                paragraph = document.createElement('p');
                item.querySelector('.v2-agent-activity-content')?.appendChild(paragraph);
            }
            paragraph.textContent = detail;
        }
    }

    _completeGreenWorkspaceResult(data = {}) {
        return this._withPatientSwarmMapContext(data, () => {
        const greenId = String(data.green_id || data.roi_id || '').trim();
        if (!greenId) return;
        this._setSubagentState(greenId, 'done');
        const observation = data.reasoning || data.observation || data.findings || '';
        const rect = data.rect || null;
        const parentBlueId = data.parent_blue_id || data.parent_id || '';
        const list = document.getElementById('routeStepsList');
        const existing = this._findTreeNodeByRoiId(list, greenId);
        if (existing) {
            this.updateRouteStepStatus(greenId, 'completed', observation, rect, observation);
        } else {
            this.appendRouteStep({
                level: 'green',
                roi_id: greenId,
                parent_id: parentBlueId,
                title: `${greenId} ${__t('status_completed')}`,
                content: observation,
                reasoning: observation,
                rect,
                status: 'completed'
            });
        }
        this._completeActivityItemForRoi(greenId, {
            title: `${greenId} ${__t('status_completed')}`,
            detail: String(observation || '').replace(/\s+/g, ' ').slice(0, 220)
        });
        });
    }

    /**
     * ⭐ Patient Level 模式：处理绿框分析结果
     */
    handleGreenAnalysis(data) {
        console.log('[Green] 绿框分析结果:', data);
        this._completeGreenWorkspaceResult(data);
        this._drawLiveRoiBoxes(data, [
            { id: data.green_id, rect: data.rect, magnification: 160 },
        ]);
        
        const plPanel = document.getElementById('patientLevelPanel');
        if (!plPanel || plPanel.style.display === 'none') return;
        
        if (!this.isPatientLevelMode) {
            this.isPatientLevelMode = true;
            console.log('🔧 [handleGreenAnalysis] 从 DOM 推断启用 Patient Level 模式');
        }
        
        // ⭐⭐⭐ 关键修复：从展开的 WSI 卡片推断 currentAnalyzingWsiIndex ⭐⭐⭐
        let wsiIndex = this._patientSwarmMapWsiIndexFromEvent(data);
        if (!Number.isInteger(wsiIndex)) {
            const expandedCard = document.querySelector('.pl-wsi-card.expanded');
            if (expandedCard) {
                const body = expandedCard.querySelector('.pl-wsi-body');
                if (body && body.dataset.index !== undefined) {
                    wsiIndex = parseInt(body.dataset.index) || 0;
                    this.currentAnalyzingWsiIndex = wsiIndex;
                    console.log(`🔧 [handleGreenAnalysis] 从展开的卡片推断 WSI index=${wsiIndex}`);
                }
            }
            if (!Number.isInteger(wsiIndex)) {
                wsiIndex = 0;
                this.currentAnalyzingWsiIndex = 0;
                console.log('🔧 [handleGreenAnalysis] 默认使用 WSI index=0');
            }
        }
        
        // 确保有该 WSI 的数据存储
        if (!this.patientLevelAnalysisData) {
            this.patientLevelAnalysisData = {};
        }
        if (!this.patientLevelAnalysisData[wsiIndex]) {
            this.patientLevelAnalysisData[wsiIndex] = { redBoxes: [] };
        }
        
        const wsiData = this.patientLevelAnalysisData[wsiIndex];
        const parentBlueId = data.parent_blue_id;
        const greenId = data.green_id;
        const observation = data.observation || '';
        const rect = data.rect;
        
        // 查找父蓝框
        for (const redBox of wsiData.redBoxes) {
            for (const blueBox of redBox.blueBoxes) {
                if (blueBox.id === parentBlueId || parentBlueId.includes(blueBox.id)) {
                    // 查找或创建绿框
                    let greenBox = blueBox.greenBoxes.find(g => g.id === greenId);
                    if (!greenBox) {
                        greenBox = {
                            id: greenId,
                            rect: rect,
                            status: 'completed',
                            observation: observation
                        };
                        blueBox.greenBoxes.push(greenBox);
                    } else {
                        greenBox.status = 'completed';
                        greenBox.observation = observation;
                        if (rect) greenBox.rect = rect;
                    }
                    
                    // ⭐⭐⭐ 核心修复：确保 UI 更新成功（解决 DOM 未就绪问题）
                    const tryUpdateGreenUI = () => {
                        if (window.pathchat && typeof window.pathchat.updatePlWsiAnalysisTree === 'function') {
                            const body = document.querySelector(`.pl-wsi-body[data-index="${wsiIndex}"]`);
                            if (body && body.querySelector('.analysis-tree')) {
                                window.pathchat.updatePlWsiAnalysisTree(wsiIndex, wsiData);
                                console.log(`📊 [handleGreenAnalysis] UI 更新成功 - ${greenId}`);
                                return true;
                            }
                        }
                        return false;
                    };
                    
                    if (!tryUpdateGreenUI()) {
                        console.log(`⏳ [handleGreenAnalysis] DOM 未就绪，100ms 后重试 - ${greenId}`);
                        setTimeout(() => tryUpdateGreenUI(), 100);
                    }
                    this._seedPatientSwarmMapFromData(wsiIndex, { replace: false });
                    return;
                }
            }
        }
    }
    
    _completeBlueWorkspaceResult(data = {}) {
        return this._withPatientSwarmMapContext(data, () => {
        const blueId = String(data.blue_id || data.roi_id || '').trim();
        if (!blueId) return;
        this._setSubagentState(
            blueId,
            'done',
            data.parent_roi_id,
            data.blue_index
        );
        const observation = data.reasoning || data.observation || data.findings || '';
        const rect = data.rect || this._resolveRoiRect(blueId);
        const parentRedId = data.parent_roi_id || data.parent_id || '';
        const list = document.getElementById('routeStepsList');
        const existing = this._findMappedTreeCard(blueId)
            || this._findTreeNodeByRoiId(list, blueId, '.route-tree-blue-node[data-roi-id], .collab-task-card[data-roi-id]');
        if (existing) {
            this.updateRouteStepStatus(blueId, 'completed', observation, rect, observation);
            this._completeActivityItemForRoi(blueId, {
                title: `${blueId} ${__t('status_completed')}`,
                detail: String(observation || '').replace(/\s+/g, ' ').slice(0, 220)
            });
            return;
        }
        if (this.currentMode === 'collaborative' || this.isCollaborativeMode) return;
        this.appendRouteStep({
            level: 'blue',
            roi_id: blueId,
            parent_id: parentRedId,
            title: blueId,
            content: observation,
            reasoning: observation,
            rect,
            status: 'completed'
        });
        this._completeActivityItemForRoi(blueId, {
            title: `${blueId} ${__t('status_completed')}`,
            detail: String(observation || '').replace(/\s+/g, ' ').slice(0, 220)
        });
        });
    }

    /**
     * ⭐ Patient Level 模式：处理蓝框分析结果
     */
    handleBlueAnalysis(data) {
        console.log('[Blue] 蓝框分析结果:', data);
        this._completeBlueWorkspaceResult(data);
        this._drawLiveRoiBoxes(data, [
            { id: data.blue_id, rect: data.rect, magnification: 40 },
        ]);
        
        // ⭐⭐⭐ 协作模式有自己的 Phase UI，不需要这里更新分析树 ⭐⭐⭐
        if (this.isCollaborativeMode) {
            console.log('[handleBlueAnalysis] 协作模式，跳过自动模式分析树更新');
            return;
        }
        
        // ⭐⭐⭐ 关键修复：从 DOM 推断 Patient Level 模式（解决事件竞争条件）⭐⭐⭐
        const plPanel = document.getElementById('patientLevelPanel');
        if (!plPanel || plPanel.style.display === 'none') return;
        
        if (!this.isPatientLevelMode) {
            this.isPatientLevelMode = true;
            console.log('🔧 [handleBlueAnalysis] 从 DOM 推断启用 Patient Level 模式');
        }
        
        // ⭐⭐⭐ 关键修复：从展开的 WSI 卡片推断 currentAnalyzingWsiIndex ⭐⭐⭐
        let wsiIndex = this._patientSwarmMapWsiIndexFromEvent(data);
        if (!Number.isInteger(wsiIndex)) {
            // 尝试从当前展开的 WSI 卡片获取索引（修正选择器）
            const expandedCard = document.querySelector('.pl-wsi-card.expanded');
            if (expandedCard) {
                const body = expandedCard.querySelector('.pl-wsi-body');
                if (body && body.dataset.index !== undefined) {
                    wsiIndex = parseInt(body.dataset.index) || 0;
                    this.currentAnalyzingWsiIndex = wsiIndex;
                    console.log(`🔧 [handleBlueAnalysis] 从展开的卡片推断 WSI index=${wsiIndex}`);
                }
            }
            if (!Number.isInteger(wsiIndex)) {
                wsiIndex = 0;
                this.currentAnalyzingWsiIndex = 0;
                console.log('🔧 [handleBlueAnalysis] 默认使用 WSI index=0');
            }
        }
        
        // 确保有该 WSI 的数据存储
        if (!this.patientLevelAnalysisData) {
            this.patientLevelAnalysisData = {};
        }
        if (!this.patientLevelAnalysisData[wsiIndex]) {
            this.patientLevelAnalysisData[wsiIndex] = { redBoxes: [] };
        }
        
        const wsiData = this.patientLevelAnalysisData[wsiIndex];
        const parentRoiId = data.parent_roi_id;
        const blueId = data.blue_id;
        const observation = data.observation || '';
        const rect = data.rect;
        
        // 查找父红框
        let parentRedBox = wsiData.redBoxes.find(r => r.id === parentRoiId);
        if (!parentRedBox) {
            // 如果红框不存在，使用最后一个
            parentRedBox = wsiData.redBoxes[wsiData.redBoxes.length - 1];
        }
        
        if (parentRedBox) {
            // 查找或创建蓝框
            let blueBox = parentRedBox.blueBoxes.find(b => b.id === blueId);
            if (!blueBox) {
                blueBox = {
                    id: blueId,
                    rect: rect,
                    status: 'completed',
                    observation: observation,
                    greenBoxes: []
                };
                parentRedBox.blueBoxes.push(blueBox);
            } else {
                blueBox.status = 'completed';
                blueBox.observation = observation;
                if (rect) blueBox.rect = rect;
            }
            
            // ⭐⭐⭐ 核心修复：确保 UI 更新成功（解决 DOM 未就绪问题）
            const tryUpdateBlueUI = () => {
                if (window.pathchat && typeof window.pathchat.updatePlWsiAnalysisTree === 'function') {
                    const body = document.querySelector(`.pl-wsi-body[data-index="${wsiIndex}"]`);
                    if (body && body.querySelector('.analysis-tree')) {
                        window.pathchat.updatePlWsiAnalysisTree(wsiIndex, wsiData);
                        console.log(`📊 [handleBlueAnalysis] UI 更新成功 - ${blueId}`);
                        return true;
                    }
                }
                return false;
            };
            
            if (!tryUpdateBlueUI()) {
                console.log(`⏳ [handleBlueAnalysis] DOM 未就绪，100ms 后重试 - ${blueId}`);
                setTimeout(() => tryUpdateBlueUI(), 100);
            }
            this._seedPatientSwarmMapFromData(wsiIndex, { replace: false });
        }
    }
    
    /**
     * ⭐ Patient Level 模式：高亮左侧分析树中的节点（点击地图框时调用）
     * ⭐ 参考单张WSI自动模式的黄色高亮效果
     */
    highlightPatientLevelTreeNode(roiId) {
        if (!roiId) return;
        
        console.log(`[Patient Level] 🎯 高亮分析树节点: ${roiId}`);
        
        const plPanel = document.getElementById('patientLevelPanel');
        
        // ⭐ 核心修复：优先在【当前查看的WSI】中搜索，而不是【正在分析的WSI】
        // 方法1: 查找被选中的WSI卡片
        let currentViewingWsiIndex = null;
        const selectedCard = document.querySelector('.pl-wsi-card.selected');
        if (selectedCard) {
            currentViewingWsiIndex = parseInt(selectedCard.dataset.index);
        }
        
        // 方法2: 如果没有选中的卡片，尝试通过当前加载的文件ID来确定
        if (currentViewingWsiIndex === null && window.pathchat && window.pathchat.currentFile) {
            const currentFileId = window.pathchat.currentFile.file_id;
            const uploadResults = window.pathchat.patientLevelUploadResults || [];
            const match = uploadResults.find(f => f.file_id === currentFileId);
            if (match) {
                currentViewingWsiIndex = match.index;
            }
        }
        
        console.log(`[Patient Level] 当前查看的WSI index=${currentViewingWsiIndex}, 正在分析的WSI index=${this.currentAnalyzingWsiIndex}`);
        
        let targetAnalysisTrees = [];
        
        // ⭐⭐⭐ 关键修复：多切片模式下，只在当前查看的 WSI 的分析树中搜索 ⭐⭐⭐
        // 不 fallback 到其他 WSI 的分析树，避免跳到错误的切片
        let onlySearchCurrentWsi = false;
        
        if (currentViewingWsiIndex !== null) {
            // 在当前查看的WSI卡片的分析树中搜索
            const currentWsiCard = document.querySelector(`.pl-wsi-card[data-index="${currentViewingWsiIndex}"]`);
            if (currentWsiCard) {
                const currentTree = currentWsiCard.querySelector('.analysis-tree');
                if (currentTree) {
                    targetAnalysisTrees.push(currentTree);
                    onlySearchCurrentWsi = true;  // ⭐ 标记：只搜索当前 WSI
                    console.log(`[Patient Level] ✅ 只在当前查看的WSI(index=${currentViewingWsiIndex})的分析树中搜索`);
                }
            }
        }
        
        // ⭐ 只有当无法确定当前 WSI 时，才 fallback 到所有分析树
        // 这种情况通常不会发生，但作为安全保障
        if (targetAnalysisTrees.length === 0) {
            targetAnalysisTrees = Array.from(document.querySelectorAll('.analysis-tree'));
            console.log(`[Patient Level] ⚠️ 无法确定当前WSI，搜索所有 ${targetAnalysisTrees.length} 个 analysis-tree 容器`);
        }
        
        const allAnalysisTrees = targetAnalysisTrees;
        console.log(`[Patient Level] 搜索范围: ${allAnalysisTrees.length} 个 analysis-tree (onlySearchCurrentWsi=${onlySearchCurrentWsi})`);
        
        // ⭐⭐⭐ 不在这里清除高亮！先找到目标节点，再判断是否需要 toggle ⭐⭐⭐
        // 清除逻辑移到下面，在确认不是 toggle 取消时才执行
        
        // ⭐ 辅助函数：清理ID，去除各种前缀
        const cleanId = (id) => {
            if (!id) return '';
            return id.replace('roi-overlay-', '')
                     .replace('overlay-', '')
                     .replace('editable-region-', '')
                     .trim();
        };
        
        // ⭐ 调试：打印所有可用的节点ID（包括 .region-item）
        allAnalysisTrees.forEach((tree, idx) => {
            const nodes = tree.querySelectorAll('.clickable-roi[data-roi-id], .region-item[data-roi-id], .collab-tree-node[data-roi-id]');
            console.log(`[Patient Level] analysis-tree[${idx}] 中的节点ID列表:`, 
                Array.from(nodes).map(n => n.dataset.roiId));
        });
        
        // 获取点击的 overlay 元素的坐标信息
        const clickedOverlay = document.querySelector(`[data-roi-id="${roiId}"]`);
        let clickedRect = null;
        if (clickedOverlay) {
            if (clickedOverlay._roiRect) {
                clickedRect = clickedOverlay._roiRect;
            } else if (clickedOverlay.dataset.rect) {
                try {
                    clickedRect = JSON.parse(clickedOverlay.dataset.rect);
                } catch (e) {}
            }
        }
        
        const cleanedClickId = cleanId(roiId);
        console.log(`[Patient Level] 清理后的ID: ${cleanedClickId}, 原始ID: ${roiId}`);
        
        // 查找匹配的节点
        let targetNode = null;
        let bestMatch = null;
        let bestMatchLength = 0;
        
        // ⭐⭐⭐ 优先查找分析结果（.collab-tree-node），而不是选中区域列表（.region-item）
        // 这样进入 Phase 2 后，点击红框会跳转到分析结果，而不是 Phase 1 的选中区域
        // 参考单切片协作模式的实现
        for (const tree of allAnalysisTrees) {
            if (targetNode) break;
            
            // 获取所有 phase-result-card，从最新的开始倒序查找
            const phaseCards = tree.querySelectorAll('.phase-result-card');
            if (phaseCards.length > 0) {
                for (let i = phaseCards.length - 1; i >= 0; i--) {
                    const phaseCard = phaseCards[i];
                    // 优先查找分析结果节点（.collab-tree-node）
                    const treeNode = phaseCard.querySelector(`.collab-tree-node[data-roi-id="${roiId}"]`);
                    if (treeNode) {
                        targetNode = treeNode;
                        console.log(`[Patient Level] ✅ 优先匹配: 在 Phase ${phaseCard.dataset.phase} 找到分析结果节点 ${roiId}`);
                        break;
                    }
                }
            }
        }
        
        // ⭐ 如果没找到分析结果，再在所有 .analysis-tree 中搜索（后备方案）
        for (const tree of allAnalysisTrees) {
            if (targetNode) break;  // 已经找到，跳过
            
            // ⭐ 获取所有可点击的ROI节点（包括 .region-item 用于 Phase 1 的选中区域列表）
            const allNodes = tree.querySelectorAll('.clickable-roi[data-roi-id], .region-item[data-roi-id], .collab-tree-node[data-roi-id]');
            console.log(`[Patient Level] 在analysis-tree中找到 ${allNodes.length} 个节点`);
            
            for (const node of allNodes) {
                const nodeRoiId = node.dataset.roiId || '';
                const cleanedNodeId = cleanId(nodeRoiId);
                
                // 方法1: 精确匹配（清理后的ID）- 最高优先级
                if (cleanedNodeId === cleanedClickId) {
                    targetNode = node;
                    console.log(`[Patient Level] ✅ 精确匹配成功: ${nodeRoiId}`);
                    break;
                }
                
                // 方法2: 包含匹配 - 收集候选，选择最长匹配（最具体的节点）
                // 只有当节点ID是点击ID的前缀时才匹配（例如点击 R4-B11，可以匹配 R4-B11-G1）
                // 或者点击ID是节点ID的前缀（例如点击 R4，可以匹配 R4）
                if (cleanedClickId.startsWith(cleanedNodeId) || cleanedNodeId.startsWith(cleanedClickId)) {
                    // 选择更长的匹配（更具体的节点）
                    const matchLength = Math.min(cleanedNodeId.length, cleanedClickId.length);
                    if (matchLength > bestMatchLength) {
                        bestMatch = node;
                        bestMatchLength = matchLength;
                        console.log(`[Patient Level] 📌 候选匹配: ${nodeRoiId} (长度=${matchLength})`);
                    }
                }
            }
            
            // 如果没有精确匹配，使用最佳候选匹配
            if (!targetNode && bestMatch) {
                targetNode = bestMatch;
                console.log(`[Patient Level] ✅ 最佳前缀匹配成功: ${bestMatch.dataset.roiId}`);
            }
            
            // 方法3: 坐标匹配（如果ID匹配失败）
            if (!targetNode && clickedRect) {
                for (const node of allNodes) {
                    if (!node.dataset.rect) continue;
                    try {
                        const nodeRect = JSON.parse(node.dataset.rect);
                        const tolerance = 100;
                        if (Math.abs(nodeRect.x - clickedRect.x) < tolerance && 
                            Math.abs(nodeRect.y - clickedRect.y) < tolerance) {
                            targetNode = node;
                            console.log(`[Patient Level] ✅ 坐标匹配成功`);
                            break;
                        }
                    } catch (e) {}
                }
            }
        }
        
        // ⭐ 如果在 analysis-tree 中没找到，尝试在当前 WSI 卡片中搜索（不是整个 plPanel）
        if (!targetNode && plPanel && currentViewingWsiIndex !== null) {
            // ⭐⭐⭐ 关键修复：只在当前查看的 WSI 卡片中搜索，避免跳到其他切片
            const currentWsiCard = plPanel.querySelector(`.pl-wsi-card[data-index="${currentViewingWsiIndex}"]`);
            const searchContainer = currentWsiCard || plPanel;  // 如果找不到卡片，才 fallback 到整个 panel
            
            const allNodes = searchContainer.querySelectorAll('.clickable-roi[data-roi-id], .tree-item[data-roi-id], .region-item[data-roi-id], .collab-tree-node[data-roi-id]');
            for (const node of allNodes) {
                const nodeRoiId = node.dataset.roiId || '';
                const cleanedNodeId = cleanId(nodeRoiId);
                if (cleanedNodeId === cleanedClickId || 
                    cleanedClickId.includes(cleanedNodeId) || 
                    cleanedNodeId.includes(cleanedClickId)) {
                    targetNode = node;
                    console.log(`[Patient Level] ✅ 在当前WSI(index=${currentViewingWsiIndex})卡片中匹配成功: ${nodeRoiId}`);
                    break;
                }
            }
        }
        
        if (targetNode) {
            // ⭐⭐⭐ Toggle 逻辑：如果再次点击已高亮的节点，取消高亮 ⭐⭐⭐
            const isAlreadyHighlighted = targetNode.classList.contains('roi-selected-glow') || 
                                         targetNode.classList.contains('active');
            
            if (isAlreadyHighlighted) {
                // 取消高亮
                targetNode.classList.remove('roi-selected-glow', 'active');
                const targetBranch = targetNode.closest('.tree-branch');
                if (targetBranch) {
                    targetBranch.classList.remove('roi-selected-glow');
                    const observation = targetBranch.querySelector('.box-observation');
                    if (observation) {
                        observation.classList.remove('roi-selected-glow');
                    }
                }
                console.log(`[Patient Level] 🔄 取消高亮: ${roiId}`);
                return;  // 直接返回，不再添加高亮
            }
            
            // ⭐⭐⭐ 确认不是取消高亮后，清除其他节点的高亮 ⭐⭐⭐
            document.querySelectorAll('.roi-selected-glow').forEach(el => {
                el.classList.remove('roi-selected-glow');
            });
            document.querySelectorAll('.tree-item.active, .clickable-roi.active, .region-item.active, .collab-tree-node.active').forEach(el => {
                el.classList.remove('active');
            });
            document.querySelectorAll('.flash-highlight').forEach(el => {
                el.classList.remove('flash-highlight');
            });
            
            // ⭐ 添加黄色高亮类到节点（参考单张WSI自动模式）
            targetNode.classList.add('roi-selected-glow', 'active');
            
            // ⭐ 高亮整个分支
            const targetBranch = targetNode.closest('.tree-branch');
            if (targetBranch) {
                targetBranch.classList.add('roi-selected-glow');
                
                // ⭐ 高亮相邻的观察结果
                const observation = targetBranch.querySelector('.box-observation');
                if (observation) {
                    observation.classList.add('roi-selected-glow');
                }
            }
            
            // ⭐ 首先展开WSI卡片的body（如果被折叠）
            const wsiCard = targetNode.closest('.pl-wsi-card');
            if (wsiCard) {
                const wsiBody = wsiCard.querySelector('.pl-wsi-body');
                const wsiToggle = wsiCard.querySelector('.wsi-expand-toggle i');
                if (wsiBody && wsiBody.style.display === 'none') {
                    wsiBody.style.display = 'block';
                    if (wsiToggle) {
                        wsiToggle.className = 'fas fa-chevron-down';
                    }
                    wsiCard.classList.add('expanded');
                    console.log(`[Patient Level] 展开WSI卡片`);
                }
            }
            
            // 展开父节点（如果是收起状态）
            let parentBranch = targetNode.closest('.tree-branch');
            while (parentBranch) {
                const toggleIcon = parentBranch.querySelector(':scope > .tree-item > .toggle-icon');
                if (toggleIcon && toggleIcon.classList.contains('fa-chevron-right')) {
                    // 展开
                    const toggleId = toggleIcon.dataset.toggle;
                    const childrenEl = parentBranch.querySelector(`.tree-children[data-parent="${toggleId}"]`) ||
                                       plPanel?.querySelector(`.tree-children[data-parent="${toggleId}"]`);
                    if (childrenEl) {
                        childrenEl.style.display = '';
                        toggleIcon.classList.remove('fa-chevron-right');
                        toggleIcon.classList.add('fa-chevron-down');
                        parentBranch.classList.remove('collapsed');
                        parentBranch.classList.add('expanded');
                        console.log(`[Patient Level] 展开父节点: ${toggleId}`);
                    }
                }
                parentBranch = parentBranch.parentElement?.closest('.tree-branch');
            }
            
            // ⭐ 添加闪烁动画效果
            targetNode.classList.add('flash-highlight');
            setTimeout(() => {
                targetNode.classList.remove('flash-highlight');
            }, 800);
            
            // ⭐ 滚动到可视区域（让用户能看到高亮的节点）
            // 延迟一点以确保展开动画完成
            setTimeout(() => {
                targetNode.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }, 100);
            
            console.log(`[Patient Level] ✅ 高亮节点成功: ${roiId}`);
        } else {
            console.warn(`[Patient Level] ❌ 未找到匹配节点: ${roiId}，尝试匹配的坐标:`, clickedRect);
            
            // ⭐ 如果没找到精确匹配，尝试在所有WSI卡片中查找
            const allWsiCards = plPanel.querySelectorAll('.pl-wsi-card');
            for (const card of allWsiCards) {
                const analysisTree = card.querySelector('.analysis-tree');
                if (analysisTree) {
                    const nodes = analysisTree.querySelectorAll('.clickable-roi[data-rect]');
                    for (const node of nodes) {
                        try {
                            const nodeRect = JSON.parse(node.dataset.rect);
                            if (clickedRect && 
                                Math.abs(nodeRect.x - clickedRect.x) < 100 &&
                                Math.abs(nodeRect.y - clickedRect.y) < 100) {
                                node.classList.add('roi-selected-glow', 'active');
                                const branch = node.closest('.tree-branch');
                                if (branch) {
                                    branch.classList.add('roi-selected-glow');
                                    const obs = branch.querySelector('.box-observation');
                                    if (obs) obs.classList.add('roi-selected-glow');
                                }
                                node.scrollIntoView({ behavior: 'smooth', block: 'center' });
                                console.log(`[Patient Level] ✅ 通过宽松坐标匹配找到节点`);
                                return;
                            }
                        } catch (e) {}
                    }
                }
            }
        }
    }
    
    handleWsiStarted(data) {
        console.log('[Patient Level] WSI开始分析:', data);
        if (!this._patientEventMatchesActiveRun(data)) return;
        
        const index = data.index;
        const filename = data.filename;
        const part = data.part || `Part ${index + 1}`;
        
        // ⭐ 记录当前正在分析的 WSI index（用于 trace 事件更新左侧面板）
        this.currentAnalyzingWsiIndex = index;
        this.isPatientLevelMode = true;
        window.pathchat?.ensurePatientProgressVisible?.();
        this._setEvidencePaneMode(this.activeAnalysisStrategy);
        if (!this.activityStartedAt) this._startChatActivity();
        this._ensurePatientSlideMap(index);
        this._preparePatientSlideThumbnailAnnouncement(data);
        this._activateSubagentUi();
        
        // ⭐⭐⭐ 保存 patient_id（用于 HTML 报告路径 + 缓存保存路径）⭐⭐⭐
        if (data.patient_id) {
            this.patientId = data.patient_id;
            if (window.pathchat) {
                window.pathchat.currentPatientId = data.patient_id;
            }
            console.log(`[Patient Level] 保存 patient_id: ${data.patient_id}`);
        }
        
        // ⭐ 初始化该 WSI 的分析数据存储
        if (!this.patientLevelAnalysisData) {
            this.patientLevelAnalysisData = {};
        }
        this.patientLevelAnalysisData[index] = {
            redBoxes: [],
            currentRedIndex: -1
        };
        
        // 更新左侧面板WSI状态
        if (window.pathchat && typeof window.pathchat.updatePlWsiStatus === 'function') {
            window.pathchat.updatePlWsiStatus(index, 'analyzing', 'spinner fa-spin');
        }
        
        // 并行时不要抢走用户正在看的切片；串行或当前就是这张切片时才切查看器。
        // 协作串行若上一张仍待确认，也不要跳走——复核卡会跟着 viewer 被藏起来。
        const eventFileId = String(data.file_id || '');
        const currentFileId = String(window.pathchat?.currentFile?.file_id || '');
        const eventIsCurrent = Boolean(eventFileId && eventFileId === currentFileId);
        const parallel = Boolean(data.parallel) || this._isPatientParallelRun();
        const userPinnedView = Number.isInteger(this.currentViewingWsiIndex);
        const holdViewForReview = !parallel
            && this.isCollaborativeMode
            && this._hasPendingPatientSlideReview(index);
        if ((!parallel || eventIsCurrent || !userPinnedView) && !holdViewForReview) {
            window.pathchat?._persistViewingWsiOverlays?.(this.currentViewingWsiIndex);
            this.currentViewingWsiIndex = index;
            if (window.pathchat && typeof window.pathchat.selectPlWsiItem === 'function') {
                window.pathchat.selectPlWsiItem(index);
            }
        }
        this._keepPatientSlideMapOnViewing();
        
        // ⭐ 更新进度区域：显示当前正在分析的 WSI 名称
        const currentWsiEl = document.getElementById('plCurrentWsiName');
        const currentWsiSection = document.getElementById('plProgressCurrent');
        if (currentWsiEl) {
            currentWsiEl.textContent = (window.__lang||'zh')==='en' ? `Analyzing ${part}: ${filename}` : `正在分析 ${part}: ${filename}`;
        }
        if (currentWsiSection) {
            currentWsiSection.style.display = 'flex';
        }
        
        // ⭐⭐⭐ Patient Level 协作模式：初始化协作状态 ⭐⭐⭐
        if (this.isCollaborativeMode) {
            console.log(`[Patient Level 协作模式] WSI ${index} 开始协作分析`);
            this.plCollabCurrentWsiIndex = index;
            this.plCollabCurrentWsiPart = part;
            this.plCollabCurrentWsiFilename = filename;
            
            // 清空当前 WSI 卡片内的分析树，准备接收 Phase 结果
            const body = document.querySelector(`.pl-wsi-body[data-index="${index}"]`);
            if (body) {
                const treeEl = body.querySelector('.analysis-tree');
                if (treeEl) {
                    treeEl.innerHTML = `
                        <div class="analysis-empty">
                            <i class="fas fa-spinner fa-spin"></i>
                            <span>${__t('observation_analyzing')}</span>
                        </div>
                    `;
                }
            }
        }
        
        // 并行：聊天只说一次总调度，各片细节进中间栏。
        if (data.parallel || this._isPatientParallelRun()) {
            this._announcePatientParallelBriefingOnce();
        } else if (window.pathchat && typeof window.pathchat.addChatMessage === 'function') {
            const _e = (window.__lang||'zh')==='en';
            const roamCollab = this.activeAnalysisStrategy === 'single_agent_roam';
            const modeLabel = this.isCollaborativeMode
                ? (roamCollab
                    ? (_e ? ' (confirm after this roam)' : '（本片游走结束后请确认）')
                    : (_e ? ' (Collaborative)' : ' (协作模式)'))
                : '';
            window.pathchat.addChatMessage('assistant', `
                <div class="wsi-analysis-msg" data-wsi-index="${index}" role="button" tabindex="0"
                     onclick="window.pathchat && window.pathchat.selectPlWsiItem(${index})">
                    <span class="wsi-icon">🔬</span>
                    <span>${_e ? 'Analyzing' : '开始分析'} <strong>${part}</strong>: ${filename}${modeLabel}</span>
                </div>
            `);
        }
    }
    
    /**
     * 处理单个WSI分析完成
     */
    handleWsiCompleted(data) {
        console.log('[Patient Level] WSI分析完成:', data);
        if (!this._patientEventMatchesActiveRun(data)) return;
        
        const index = data.index;
        const filename = data.filename;
        const part = data.part || `Part ${index + 1}`;
        const result = data.result || {};
        const error = data.error;
        const mode = data.mode || 'ai';

        // Serial and parallel Patient Swarm both emit tree_nodes on
        // wsi_completed. Hydrate the visible #routeStepsList map, not only
        // the hidden left-rail .analysis-tree.
        if (
            !error
            && this.activeAnalysisStrategy !== 'single_agent_roam'
            && Array.isArray(result.tree_nodes)
            && result.tree_nodes.length
        ) {
            if (!this._isPatientParallelRun() || this.currentViewingWsiIndex === index) {
                window.pathchat?._persistViewingWsiOverlays?.(index);
            }
            this._hydratePatientSwarmDataFromResult(index, result);
            this._seedOrPreservePatientSlideMap(index, { replace: true });
        }
        
        // 更新左侧面板WSI状态
        if (window.pathchat && typeof window.pathchat.updatePlWsiStatus === 'function') {
            if (error) {
                window.pathchat.updatePlWsiStatus(index, 'error', 'times');
            } else if (data.requires_review && mode === 'collaborative') {
                window.pathchat.updatePlWsiStatus(index, 'review', 'user-check');
            } else {
                window.pathchat.updatePlWsiStatus(index, 'completed', 'check');
            }
        }
        
        // ⭐ 更新进度区域：显示当前 WSI 已完成
        const currentWsiEl = document.getElementById('plCurrentWsiName');
        if (currentWsiEl) {
            const isReviewDraft = data.requires_review && mode === 'collaborative';
            currentWsiEl.textContent = (window.__lang||'zh')==='en'
                ? `${part} ${isReviewDraft ? 'draft ready for review' : 'analysis complete'}`
                : `${part} ${isReviewDraft ? '草稿待医生复核' : '分析完成'}`;
        }
        
        const htmlReportUrl = this._patientSlideHtmlReportUrl(data, result);
        
        // ⭐⭐⭐ Patient Level 协作模式：清理当前 WSI 的协作状态 ⭐⭐⭐
        if (this.isCollaborativeMode && this.isPatientLevelMode) {
            console.log(`[Patient Level 协作模式] WSI ${index} 协作完成`);
            this.plCollabCurrentWsiIndex = null;
        }
        
        // 在聊天中显示
        if (window.pathchat && typeof window.pathchat.addChatMessage === 'function') {
            const isEn = (window.__lang || 'zh') === 'en';
            if (error) {
                window.pathchat.addChatMessage('assistant', `
                    <div class="wsi-analysis-msg error" data-wsi-index="${index}">
                        <span class="wsi-icon">❌</span>
                        <span><strong>${part}</strong> ${isEn ? 'analysis failed' : '分析失败'}: ${error}</span>
                    </div>
                `);
            } else {
                const reportTxt = String(
                    data.report_txt
                    || result.report
                    || result.global_reasoning
                    || ''
                ).trim();
                const heading = this._patientSlideResultHeading(index, part, filename);
                const modeTag = data.requires_review
                    ? (isEn ? ' (Draft · review required)' : '（草稿 · 待医生复核）')
                    : ((mode === 'collaborative')
                        ? (isEn ? ' (Collaborative)' : '（协作完成）')
                        : '');
                const reportBtnLabel = isEn ? 'View Full Report' : '查看完整报告';
                const reportBtn = this._patientSlideHtmlButton(htmlReportUrl, reportBtnLabel);
                const doneLabel = data.requires_review
                    ? (isEn ? 'draft generated' : '已生成单片草稿')
                    : (isEn ? 'analysis complete' : '分析完成');
                const reportBody = (reportTxt && window.pathchat.formatMessageContent)
                    ? `<div class="report-summary" data-wsi-index="${index}" data-patient-slide-report="draft" style="white-space:pre-wrap; font-size:13px; line-height:1.6; max-height:400px; overflow-y:auto; padding:10px; background:#f8f9fa; border-radius:8px; border:1px solid #e9ecef;">
                            ${window.pathchat.formatMessageContent(reportTxt)}
                        </div>`
                    : '';
                const card = window.pathchat.addChatMessage('assistant', `
                    <div class="patient-slide-result" data-wsi-index="${index}">
                        <p class="patient-slide-result-lead">${heading}</p>
                        ${reportBody}
                        <div class="wsi-analysis-msg success patient-slide-html-row" data-wsi-index="${index}" id="patient-slide-report-anchor-${index}">
                            <span class="wsi-icon">✅</span>
                            <span><strong>${filename || part}</strong> ${doneLabel}${modeTag}</span>
                            ${reportBtn}
                        </div>
                    </div>
                `);
                if (!htmlReportUrl && data.file_id) {
                    this._ensurePatientSlideHtmlButton(
                        card?.querySelector?.(`#patient-slide-report-anchor-${index}`) || card,
                        data.file_id
                    );
                }
            }
            window.pathchat.syncPatientSlideReportNav?.();
        }
        
        // ⭐ 在左侧面板的 WSI 卡片中也添加报告链接
        if (htmlReportUrl && window.pathchat && typeof window.pathchat.updatePlWsiReportLink === 'function') {
            window.pathchat.updatePlWsiReportLink(index, htmlReportUrl);
        }
        
        // ⭐⭐⭐ Patient Level：每完成一张切片就保存 overlays 到 analysis_cache（断点续传用）⭐⭐⭐
        if (!error && this.isPatientLevelMode && window.pathchat && data.file_id) {
            const fileId = data.file_id;
            const wsiData = this.patientLevelAnalysisData?.[index];
            if (this.activeAnalysisStrategy === 'single_agent_roam') {
                const trajectory = Array.isArray(result.trajectory) ? result.trajectory : [];
                const roamEntry = this._patientRoamByFile.get(String(fileId));
                const cachedTrajectory = trajectory.length
                    ? trajectory
                    : (roamEntry?.events || [])
                        .filter(item => item.kind === 'step')
                        .map(item => ({
                            kind: item.kind,
                            ...(item.payload || {}),
                        }));
                if (!this.patientLevelAnalysisData) this.patientLevelAnalysisData = {};
                this.patientLevelAnalysisData[index] = {
                    ...(this.patientLevelAnalysisData[index] || {}),
                    trajectory: cachedTrajectory,
                    report: result.report || data.report_txt || '',
                    globalReasoning: result.report || data.report_txt || '',
                };
                const saveData = {
                    roiOverlays: cachedTrajectory
                        .filter(item => item?.rect)
                        .map((item, stepIndex) => ({
                            id: `roam-${stepIndex + 1}`,
                            data: {
                                id: `roam-${stepIndex + 1}`,
                                rect: item.rect,
                                magnification: Number(item.zoom) || 10,
                                color: Number(item.zoom) >= 160 ? '#00CC00'
                                    : (Number(item.zoom) >= 40 ? '#0066FF' : '#FF0000'),
                            },
                        })),
                    roiAnalysisData: {},
                    chatHistory: [],
                    leftPanelHtml: '',
                    globalReasoning: result.report || '',
                    roiMappings: {},
                    analysisComplete: true,
                    analysisStrategy: 'single_agent_roam',
                    trajectory: cachedTrajectory,
                    report: result.report || data.report_txt || '',
                    diagnostic_question: result.diagnostic_question || data.diagnostic_question || '',
                    overview: { file_id: fileId, filename: data.filename || part || '' },
                    mode: 'single_agent_roam',
                    timestamp: new Date().toISOString(),
                    filename: data.filename || part || '',
                    patient_id: this.patientId || window.pathchat?.currentPatientId || '',
                    metadata: { version: '2.2', source: 'patient_single_roam_auto_save' },
                };
                fetch(`/api/save_analysis_state/${fileId}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(saveData),
                }).catch(error => console.warn('Single Agent trajectory cache save failed:', error));
            }
            
            if (
                this.activeAnalysisStrategy !== 'single_agent_roam'
                && wsiData
                && wsiData.redBoxes
            ) {
                // 从 patientLevelAnalysisData 构建 overlays（不依赖 this.roiOverlays，因为切换WSI时会变）
                const overlaysToSave = [];
                wsiData.redBoxes.forEach(redBox => {
                    if (redBox.rect) {
                        overlaysToSave.push({ id: redBox.id, data: { id: redBox.id, rect: redBox.rect, color: '#FF0000', magnification: 10 } });
                    }
                    (redBox.blueBoxes || []).forEach(blueBox => {
                        if (blueBox.rect) {
                            overlaysToSave.push({ id: blueBox.id, data: { id: blueBox.id, rect: blueBox.rect, color: '#0066FF', magnification: 40 } });
                        }
                        (blueBox.greenBoxes || []).forEach(greenBox => {
                            if (greenBox.rect) {
                                overlaysToSave.push({ id: greenBox.id, data: { id: greenBox.id, rect: greenBox.rect, color: '#00CC00', magnification: 160 } });
                            }
                        });
                    });
                });
                
                // 提取每个 ROI 的分析结论
                const roiAnalysis = {};
                wsiData.redBoxes.forEach(redBox => {
                    if (redBox.observation) roiAnalysis[redBox.id] = { analysis: redBox.observation, confidence: redBox.confidence || 0 };
                    (redBox.blueBoxes || []).forEach(blueBox => {
                        if (blueBox.observation) roiAnalysis[blueBox.id] = { analysis: blueBox.observation, confidence: blueBox.confidence || 0 };
                        (blueBox.greenBoxes || []).forEach(greenBox => {
                            if (greenBox.observation) roiAnalysis[greenBox.id] = { analysis: greenBox.observation, confidence: greenBox.confidence || 0 };
                        });
                    });
                });
                
                const wsiFilename = data.filename || part || '';

                // ⭐ 从DOM获取该WSI的完整左侧面板HTML
                const plTreeEl = document.querySelector(`.pl-wsi-body[data-index="${index}"] .analysis-tree`);
                const autoLeftPanelHtml = plTreeEl ? plTreeEl.outerHTML : '';

                // ⭐ 从内存获取 globalReasoning
                const autoGlobalReasoning = wsiData.globalReasoning || '';

                // ⭐ 从 _collectedChatHistories 获取聊天记录
                const autoHistEntry = (this._collectedChatHistories || []).find(h => h.slideIndex === index);
                const autoChatHistory = autoHistEntry ? autoHistEntry.messages : [];

                const saveData = {
                    roiOverlays: overlaysToSave,
                    roiAnalysisData: roiAnalysis,
                    chatHistory: autoChatHistory,
                    leftPanelHtml: autoLeftPanelHtml,
                    globalReasoning: autoGlobalReasoning,
                    roiMappings: {},
                    analysisComplete: true,
                    mode: window.pathChatAgent?.activeAnalysisStrategy === 'single_agent_roam'
                        ? 'single_agent_roam'
                        : (window.pathChatAgent?.isCollaborativeMode ? 'collaborative' : 'ai'),
                    timestamp: new Date().toISOString(),
                    filename: wsiFilename,
                    patient_id: this.patientId || window.pathchat?.currentPatientId || '',
                    metadata: { version: '2.1', source: 'patient_level_auto_save' }
                };
                
                const saveMode = window.pathChatAgent?.activeAnalysisStrategy === 'single_agent_roam'
                    ? 'single_agent_roam'
                    : (window.pathChatAgent?.isCollaborativeMode ? 'collaborative' : 'ai');
                fetch(`/api/save_analysis_state/${fileId}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(saveData)
                }).then(resp => resp.json()).then(result => {
                    if (result.success) {
                        console.log(`💾 [Patient Level] ✅ 切片状态已保存: ${part} (${fileId}, ${overlaysToSave.length} 个框)`);
                    }
                }).catch(err => {
                    console.warn(`💾 [Patient Level] 保存失败: ${part}`, err);
                });
            }
        }
    }
    
    /**
     * 处理Patient Level报告生成完成
     */
    handlePatientReport(data) {
        console.log('[Patient Level] 报告生成完成:', data);
        if (!this._patientEventMatchesActiveRun(data)) return;
        const terminalKey = `report:${String(data?.patient_id || '')}:${String(data?.run_token || '')}`;
        if (this._patientTerminalUiKeys.has(terminalKey)) {
            window.pathchat?._setPatientLevelRunState?.(false, {
                patientId: data?.patient_id,
            });
            this._activePatientRunToken = null;
            return;
        }
        this._patientTerminalUiKeys.add(terminalKey);
        
        const reportUrl = data.report_url;
        const summary = data.summary || '';
        const patientId = data.patient_id;
        const treeUrl = data.tree_url || null;
        
        // ⭐ 保存 patient_id 和 tree_url 供树可视化使用
        if (window.pathchat) {
            window.pathchat.currentPatientId = patientId;
            window.pathchat.patientTreeUrl = treeUrl;
            window.pathchat._setPatientLevelRunState?.(false, { patientId });
        }
        this._activePatientRunToken = null;
        
        // 更新左侧面板进度为100%
        if (window.pathchat && typeof window.pathchat.updatePlProgress === 'function') {
            window.pathchat.updatePlProgress(100, __t('pl_analysis_complete_tag') + '!');
        }
        
        const currentWsiSection = document.getElementById('plProgressCurrent');
        if (currentWsiSection) {
            currentWsiSection.style.display = 'none';
        }
        
        const statusEl = document.getElementById('plStatus');
        if (statusEl) statusEl.textContent = __t('pl_completed');
        
        // 恢复按钮
        const startBtn = document.getElementById('plStartAnalysis');
        if (startBtn) {
            startBtn.disabled = false;
            startBtn.classList.remove('running');
            startBtn.innerHTML = `<i class="fas fa-redo"></i> ${__t('pl_reanalyze')}`;
        }
        
        this.showNotification((window.__lang||'zh')==='en' 
            ? 'Patient Level analysis complete, report generated!' 
            : 'Patient Level分析完成，报告已生成！', 'success');
        
        // ⭐ 在左侧面板展示综合报告 + 下载按钮
        const reportSection = document.getElementById('plReportSection');
        const reportContent = document.getElementById('plReportContent');
        const downloadBtn = document.getElementById('plDownloadReport');
        if (reportSection && reportContent) {
            reportContent.innerHTML = this.formatMarkdown(summary);
            reportSection.style.display = 'block';
            if (downloadBtn && reportUrl) {
                downloadBtn.href = reportUrl;
                downloadBtn.style.display = 'inline-flex';
            } else if (downloadBtn) {
                downloadBtn.style.display = 'none';
            }
            
            // ⭐ 绑定折叠功能：点击标题区域可以收起/展开报告内容
            const reportHeader = document.getElementById('plReportHeader');
            const reportBody = document.getElementById('plReportBody');
            const toggleIcon = document.getElementById('plReportToggleIcon');
            
            if (reportHeader && reportBody && toggleIcon) {
                // 移除旧的事件监听器（如果存在）
                const newHeader = reportHeader.cloneNode(true);
                reportHeader.parentNode.replaceChild(newHeader, reportHeader);
                
                // 重新获取元素引用
                const newToggleIcon = newHeader.querySelector('#plReportToggleIcon');
                
                newHeader.addEventListener('click', () => {
                    const isCollapsed = reportBody.style.display === 'none';
                    if (isCollapsed) {
                        reportBody.style.display = 'block';
                        if (newToggleIcon) {
                            newToggleIcon.className = 'fas fa-chevron-down toggle-icon';
                        }
                    } else {
                        reportBody.style.display = 'none';
                        if (newToggleIcon) {
                            newToggleIcon.className = 'fas fa-chevron-right toggle-icon';
                        }
                    }
                });
            }
            
            // 滚动到报告区域
            reportSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
        
        // ⭐ 在聊天中显示报告 + HTML 报告查看按钮
        const htmlReportUrl = data.html_report_url || null;
        
        if (window.pathchat && typeof window.pathchat.addChatMessage === 'function') {
            // 构建按钮区域
            let buttonsHtml = '<div class="patient-report-buttons" style="margin-top: 15px; display: flex; gap: 10px; flex-wrap: wrap;">';
            
            if (htmlReportUrl) {
                buttonsHtml += `
                    <a href="${htmlReportUrl}" target="_blank" class="report-btn html-btn" 
                       style="display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; 
                              background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; 
                              border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 500;
                              transition: opacity 0.2s;" 
                       onmouseover="this.style.opacity='0.85'" onmouseout="this.style.opacity='1'">
                        📊 ${__t('btn_view_report')}
                    </a>`;
            }
            
            if (reportUrl) {
                buttonsHtml += `
                    <a href="${reportUrl}" target="_blank" class="report-btn word-btn"
                       style="display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; 
                              background: linear-gradient(135deg, #2196F3 0%, #1976D2 100%); color: white; 
                              border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 500;
                              transition: opacity 0.2s;"
                       onmouseover="this.style.opacity='0.85'" onmouseout="this.style.opacity='1'">
                        📄 ${__t('download_word')}
                    </a>`;
            }
            
            buttonsHtml += '</div>';
            
            let reportHtml = `
                <div class="patient-report-msg">
                    <h4>📋 ${__t('pathology_report')}</h4>
                    <div class="report-summary">${this.formatMarkdown(summary)}</div>
                    ${buttonsHtml}
                </div>
            `;
            
            window.pathchat.addChatMessage('assistant', reportHtml);
            
            // ⭐ 重新汇总按钮（使用已有切片 report.txt 重新生成患者级报告，无需重跑分析）
            const regenBtnHtml = this._buildRegenReportBtn(patientId, data.mode);
            window.pathchat.addChatMessage('assistant', regenBtnHtml);
        }
        
        // ⭐⭐⭐ Patient Level 分析完成后，自动保存所有 WSI 的分析状态 ⭐⭐⭐
        this._autoSaveAllPatientLevelStates();
    }

    /**
     * 构建"重新汇总"按钮 HTML
     */
    _buildRegenReportBtn(patientId, mode) {
        const isEn = (window.__lang || 'zh') === 'en';
        const btnId = `regen-report-btn-${Date.now()}`;
        return `
            <div class="regen-report-msg" style="text-align:center; padding: 12px 16px;
                 background: linear-gradient(135deg, #fdf4ff 0%, #f5f3ff 100%);
                 border-radius: 10px; border: 1px dashed #a855f7; margin-top: 8px;">
                <p style="margin: 0 0 10px; font-size: 13px; color: #6b7280;">
                    ${isEn
                        ? 'Click to re-aggregate the patient report using existing slide reports (no re-analysis needed).'
                        : '点击按钮使用现有切片报告重新生成患者级综合报告（无需重跑分析）。'}
                </p>
                <button id="${btnId}"
                    onclick="window.pathChatAgent && window.pathChatAgent._triggerRegenReport('${patientId}', '${mode || 'ai'}', this)"
                    style="display: inline-flex; align-items: center; gap: 8px; padding: 10px 24px;
                           background: linear-gradient(135deg, #a855f7 0%, #7c3aed 100%); color: white;
                           border: none; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer;
                           transition: all 0.2s ease; box-shadow: 0 2px 8px rgba(124,58,237,0.3);"
                    onmouseover="this.style.transform='translateY(-1px)';this.style.boxShadow='0 4px 12px rgba(124,58,237,0.4)'"
                    onmouseout="this.style.transform='';this.style.boxShadow='0 2px 8px rgba(124,58,237,0.3)'"
                >
                    <i class="fas fa-sync-alt"></i>
                    ${isEn ? '🔄 Re-aggregate Patient Report' : '🔄 重新生成患者级综合报告'}
                </button>
            </div>`;
    }

    /**
     * 触发重新汇总请求
     */
    async _triggerRegenReport(patientId, mode, btnEl) {
        const isEn = (window.__lang || 'zh') === 'en';
        if (btnEl) {
            btnEl.disabled = true;
            btnEl.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${isEn ? 'Processing...' : '正在汇总...'}`;
        }
        try {
            const room = this.room || this.socket?.id || '';
            const patientDirName = patientId?.startsWith('patient_') ? patientId : `patient_${patientId}`;
            const resp = await fetch('/api/agent/regenerate-patient-report', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    patient_dir: patientDirName,
                    mode: mode,
                    room: room,
                    lang: window.__lang || 'zh'
                })
            });
            const result = await resp.json();
            if (!result.success) {
                alert((isEn ? 'Re-aggregation failed: ' : '重新汇总失败：') + result.error);
                if (btnEl) {
                    btnEl.disabled = false;
                    btnEl.innerHTML = `<i class="fas fa-sync-alt"></i> ${isEn ? '🔄 Re-aggregate Patient Report' : '🔄 重新生成患者级综合报告'}`;
                }
            }
            // 成功后进度通过 agent.patient_progress + agent.patient_report 事件推送
        } catch (e) {
            alert((isEn ? 'Network error: ' : '网络错误：') + e.message);
            if (btnEl) {
                btnEl.disabled = false;
                btnEl.innerHTML = `<i class="fas fa-sync-alt"></i> ${isEn ? '🔄 Re-aggregate Patient Report' : '🔄 重新生成患者级综合报告'}`;
            }
        }
    }
    
    /**
     * Patient Level 分析完成后，把全部切片写入应用缓存。
     * 单 Agent 游走没有红框，不能再按 redBoxes 跳过。
     */
    _autoSaveAllPatientLevelStates() {
        if (!this.isPatientLevelMode || !window.pathchat?.saveAnalysisState) return;

        const uploadResults = window.pathchat.patientLevelUploadResults || [];
        if (uploadResults.length === 0) {
            console.warn('[Patient Level] ⚠️ 自动保存跳过：没有 uploadResults');
            return;
        }

        window.pathchat.saveAnalysisState({ silent: true });
        console.log(`💾 [Patient Level] 自动保存已触发: ${uploadResults.length} 张切片`);
    }
    
    /**
     * ⭐⭐⭐ Patient Level 协作模式：处理综合报告审阅确认
     * 后端发来 agent.patient_report_review 事件，前端在右侧聊天区展示报告供用户审阅/修改
     */
    handlePatientReportReview(data) {
        console.log('[Patient Level 协作模式] 收到综合报告审阅请求:', data);
        if (!this._patientEventMatchesActiveRun(data)) return;
        
        const { patient_id, folder_name, total_wsi } = data;
        const report = this._stripNeedDetailBlock(data.report);
        const reviewRunToken = String(data?.run_token || '');
        const existingCard = Array.from(
            document.querySelectorAll('.patient-report-review-card')
        ).find(card => (
            String(card.dataset.patientId || '') === String(patient_id || '')
            && String(card.dataset.runToken || '') === reviewRunToken
            && !card.classList.contains('is-retired')
        ));
        if (existingCard) return;
        
        // 隐藏左侧协作区域面板（已经没有单切片区域要显示了）
        const collabSection = document.getElementById('plCollabRegionsSection');
        if (collabSection) collabSection.style.display = 'none';
        
        // 在右侧聊天区显示综合报告 + 审阅按钮
        if (window.pathchat && typeof window.pathchat.addChatMessage === 'function') {
            const reportHtml = `
                <div class="patient-report-review-card" data-patient-id="${patient_id}" data-run-token="${reviewRunToken}">
                    <div class="review-header">
                        <h4><i class="fas fa-file-medical-alt"></i> ${(window.__lang||'zh')==='en' ? 'Comprehensive Diagnostic Report Draft (Pending Review)' : '综合诊断报告草稿（待审阅）'}</h4>
                        <p class="review-info">${__t('pl_patient_label', folder_name)} | ${__t('pl_slide_label', total_wsi)}</p>
                    </div>
                    <div class="review-report-content" id="plReviewReportContent">
                        ${this.formatMarkdown(report)}
                    </div>
                    <div class="review-edit-area" id="plReviewEditArea" style="display:none;">
                        <textarea id="plReviewReportTextarea" rows="12" style="width:100%; font-size:13px; line-height:1.6; padding:10px; border:1px solid #ddd; border-radius:6px; resize:vertical;">${report}</textarea>
                    </div>
                    <div class="review-actions">
                        <button class="pl-collab-btn pl-collab-btn-execute" id="plReviewConfirmBtn">
                            <i class="fas fa-check"></i> ${__t('btn_confirm_report')}
                        </button>
                        <button class="pl-collab-btn pl-collab-btn-modify" id="plReviewEditBtn">
                            <i class="fas fa-edit"></i> ${__t('btn_modify_report')}
                        </button>
                    </div>
                </div>
            `;
            window.pathchat.addChatMessage('assistant', reportHtml);
        }
        
        // 绑定按钮事件
        setTimeout(() => {
            const confirmBtn = document.getElementById('plReviewConfirmBtn');
            const editBtn = document.getElementById('plReviewEditBtn');
            const editArea = document.getElementById('plReviewEditArea');
            const reportContent = document.getElementById('plReviewReportContent');
            const textarea = document.getElementById('plReviewReportTextarea');
            
            if (confirmBtn) {
                confirmBtn.addEventListener('click', async () => {
                    // 检查是否有编辑
                    let action = 'execute';
                    let modifiedData = null;
                    
                    if (editArea && editArea.style.display !== 'none' && textarea) {
                        const editedText = textarea.value.trim();
                        if (editedText !== report) {
                            action = 'modify';
                            modifiedData = { report: editedText };
                        }
                    }

                    const submittedReport = (
                        action === 'modify' && modifiedData?.report
                            ? modifiedData.report
                            : report
                    );
                    this.pendingPatientReportReview = {
                        action,
                        report: submittedReport,
                        patientId: patient_id
                    };

                    // 提交后先等待 Patient-level HTTP 回执；WebSocket 仅用于
                    // 实时通知，不再作为最终报告确认的唯一传输通道。
                    confirmBtn.disabled = true;
                    const submittedLabel = (window.__lang||'zh')==='en' ? 'Submitted' : '已提交';
                    confirmBtn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${submittedLabel}`;
                    if (editBtn) editBtn.disabled = true;

                    try {
                        if (reviewRunToken !== String(this._activePatientRunToken || '')) {
                            throw new Error((window.__lang || 'zh') === 'en'
                                ? 'This review card belongs to an older run.'
                                : '该审阅卡属于上一轮分析，不能提交。');
                        }
                        const response = await fetch(
                            `/api/patient/${encodeURIComponent(patient_id)}/report/review`,
                            {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    action: action === 'modify' ? 'modify' : 'confirm',
                                    report: submittedReport,
                                    run_token: reviewRunToken,
                                })
                            }
                        );
                        const result = await response.json().catch(() => ({}));
                        if (!response.ok || !result.success) {
                            throw new Error(
                                result.error || `HTTP ${response.status}`
                            );
                        }

                        this.applyPatientReportConfirmation(submittedReport);
                        if (window.pathchat && typeof window.pathchat.addChatMessage === 'function') {
                            const confirmMsg = (window.__lang||'zh')==='en'
                                ? (action === 'modify' ? 'Report edited and confirmed' : 'Report confirmed')
                                : (action === 'modify' ? '已编辑并确认综合报告' : '已确认综合报告');
                            window.pathchat.addChatMessage('user', confirmMsg);
                        }
                    } catch (error) {
                        console.error('[Patient Level] 患者级报告确认失败:', error);
                        confirmBtn.disabled = false;
                        confirmBtn.innerHTML = `<i class="fas fa-check"></i> ${__t('btn_confirm_report')}`;
                        if (editBtn) editBtn.disabled = false;
                        this.pendingPatientReportReview = null;
                        this.showNotification(
                            (window.__lang||'zh')==='en'
                                ? `Confirmation failed: ${error.message}`
                                : `确认失败：${error.message}`,
                            'error'
                        );
                    }
                });
            }
            
            if (editBtn) {
                editBtn.addEventListener('click', () => {
                    if (editArea) {
                        editArea.style.display = editArea.style.display === 'none' ? 'block' : 'none';
                        if (reportContent) {
                            reportContent.style.display = editArea.style.display === 'none' ? 'block' : 'none';
                        }
                        editBtn.innerHTML = editArea.style.display === 'none' 
                            ? '<i class="fas fa-edit"></i> 编辑修改' 
                            : '<i class="fas fa-eye"></i> 预览';
                    }
                });
            }
        }, 200);
    }
    
    
    /**
     * 简单的Markdown格式化
     */
    _stripNeedDetailBlock(text) {
        return String(text || '')
            .replace(/\[NEED_DETAIL\][\s\S]*?\[\/NEED_DETAIL\]/gi, '')
            .replace(/\n{3,}/g, '\n\n')
            .trim();
    }

    formatMarkdown(text) {
        if (!text) return '';
        const cleaned = this._stripNeedDetailBlock(text);
        
        return cleaned
            .replace(/### (.*)/g, '<h5>$1</h5>')
            .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
            .replace(/\*(.*?)\*/g, '<em>$1</em>')
            .replace(/^---$/gm, '<hr>')
            .replace(/- (.*)/g, '<li>$1</li>')
            .replace(/\n/g, '<br>');
    }
    
    // ================== End Patient Level 事件处理 ==================
    
    // ==================== 五阶段协作模式事件处理 ====================
    
    /**
     * 阶段名称映射
     */
    getPhaseName(phase) {
        return __t(`phase_name_${phase}`) || `Phase ${phase}`;
    }
    
    /**
     * ⭐⭐⭐ 处理 Phase 2 实时进度（每完成一个红框立即显示其蓝框）
     */
    handlePhase2Progress(data) {
        console.log(`🔵 [Phase 2 进度] ${data.red_roi_id}: 选中 ${data.blue_count} 个蓝框 (${data.completed}/${data.total})`);
        if (data.red_roi_id) this._setSubagentState(data.red_roi_id, 'done');
        
        const canDrawOnViewer = !this.isPatientLevelMode || this._patientLiveEventMatchesViewer(data);
        if (canDrawOnViewer && data.blue_overlays && data.blue_overlays.length > 0 && window.pathchat) {
            data.blue_overlays.forEach(overlay => {
                window.pathchat.drawColoredROI(overlay);
            });
            this._backfillTreeNodeRectsFromOverlays(data.blue_overlays);
            console.log(`  ✅ 已绘制 ${data.blue_overlays.length} 个蓝框 overlay`);
        }
        
        // 更新进度显示
        const progressStatus = document.getElementById('plProgressStatus');
        if (progressStatus) {
            progressStatus.textContent = `Phase 2: ${data.message}`;
        }
    }

    applyPatientReportConfirmation(report) {
        const card = document.querySelector('.patient-report-review-card');
        const title = card?.querySelector('.review-header h4');
        const reportContent = document.getElementById('plReviewReportContent');
        const editArea = document.getElementById('plReviewEditArea');
        const textarea = document.getElementById('plReviewReportTextarea');
        const confirmBtn = document.getElementById('plReviewConfirmBtn');
        const editBtn = document.getElementById('plReviewEditBtn');
        const isEn = (window.__lang || 'zh') === 'en';

        card?.classList.add('is-confirmed');
        if (title) {
            title.innerHTML = `<i class="fas fa-file-medical-alt"></i> ${
                isEn
                    ? 'Comprehensive Diagnostic Report (Confirmed)'
                    : '综合诊断报告（已确认）'
            }`;
        }
        if (reportContent) {
            reportContent.innerHTML = this.formatMarkdown(report);
            reportContent.style.display = 'block';
        }
        if (editArea) editArea.style.display = 'none';
        if (textarea) {
            textarea.value = report;
            textarea.disabled = true;
        }
        if (confirmBtn) {
            confirmBtn.disabled = true;
            confirmBtn.innerHTML = `<i class="fas fa-check-circle"></i> ${
                isEn ? 'Confirmed' : '已确认'
            }`;
        }
        if (editBtn) editBtn.style.display = 'none';
        this.pendingPatientReportReview = null;
        this.showNotification(
            isEn
                ? 'Patient-level report confirmed'
                : '患者级报告已确认',
            'success'
        );
    }
    
    /**
     * ⭐⭐⭐ 处理 Phase 3 实时进度（每完成一个蓝框立即显示其绿框）
     */
    handlePhase3Progress(data) {
        console.log(`🟢 [Phase 3 进度] ${data.blue_roi_id}: 选中 ${data.green_count} 个绿框 (${data.completed}/${data.total})`);
        if (data.blue_roi_id) this._setSubagentState(data.blue_roi_id, 'done');
        
        const canDrawOnViewer = !this.isPatientLevelMode || this._patientLiveEventMatchesViewer(data);
        if (canDrawOnViewer && data.green_overlays && data.green_overlays.length > 0 && window.pathchat) {
            data.green_overlays.forEach(overlay => {
                window.pathchat.drawColoredROI(overlay);
            });
            console.log(`  ✅ 已绘制 ${data.green_overlays.length} 个绿框 overlay`);
        }
        
        // 更新进度显示
        const progressStatus = document.getElementById('plProgressStatus');
        if (progressStatus) {
            progressStatus.textContent = `Phase 3: ${data.message}`;
        }
    }
    
    /**
     * 处理阶段结果事件
     */
    _markCompletedPhaseSubagents(phaseNumber, subagentResults = []) {
        if (![2, 3, 4].includes(Number(phaseNumber)) || !Array.isArray(subagentResults)) return;
        subagentResults.forEach((result) => {
            const roiId = String(result?.roi_id || result?.id || '').trim();
            if (roiId) this._setSubagentState(roiId, 'done');
        });
    }

    handlePhaseResult(data) {
        if (!data?._restored_from_snapshot) this._collaborationLiveSerial += 1;
        console.log(`📋 [Phase ${data.phase}] 收到阶段结果:`, data);
        console.log(`📋 [Phase ${data.phase}] 详细数据:`, {
            reasoning_length: data.reasoning?.length || 0,
            plan_length: data.plan?.length || 0,
            evidence: data.evidence,
            selected_regions_count: data.selected_regions?.length || 0,
            wsi_context: data.wsi_context || null
        });
        
        const { phase, phase_name, reasoning, plan, evidence, selected_regions, all_candidates, subagent_results, report, metadata, wsi_context } = data;
        const reportableFindings = Array.isArray(data?.reportable_findings)
            ? data.reportable_findings
            : (Array.isArray(metadata?.reportable_findings) ? metadata.reportable_findings : []);
        const requiresConfirmation = data?.requires_confirmation !== false;
        const phaseNumber = Number.parseInt(phase, 10);
        // Phase N contains the completed analyses from level N-1 and the
        // selections that are merely pending for level N.  Live events set
        // these terminal states incrementally; a refresh must reconstruct
        // the same truth from the durable phase payload instead of rendering
        // every restored ROI as pending.
        this._markCompletedPhaseSubagents(phaseNumber, subagent_results);
        const thumbnailContext = this._thumbnailContextFromEvent(data);
        if (this.isPatientLevelMode && Number.isInteger(thumbnailContext.wsiIndex)) {
            this.currentAnalyzingWsiIndex = thumbnailContext.wsiIndex;
            if (thumbnailContext.fileId) this._thumbnailAnnounceFileId = thumbnailContext.fileId;
        }
        if (phaseNumber === 1) {
            const redIds = Array.isArray(selected_regions)
                ? selected_regions.map(region => region?.id || region?.roi_id).filter(Boolean)
                : [];
            this._announceThumbnailPlan(
                reasoning,
                redIds,
                Math.max(redIds.length, Array.isArray(selected_regions) ? selected_regions.length : 0),
                this._lastOverviewImageUrl || '',
                thumbnailContext
            );
        }
        if (phaseNumber === 4 && Array.isArray(subagent_results)) {
            // At an evidence gate the Orchestrator is waiting for the user;
            // it is not still executing 160x jobs.
            if (requiresConfirmation) {
                this.primaryAgentState = 'pending';
                this._renderSubagentBoard();
            }
        }
        this.currentPhaseGateId = String(data?.phase_gate_id || '');
        const duplicateGate = this.currentPhaseGateId
            ? document.querySelector(
                `.phase-confirm-message[data-gate-id="${CSS.escape(this.currentPhaseGateId)}"]`
            )
            : null;
        if (duplicateGate) {
            if (data?._restored_from_snapshot === true) {
                // Chat history restores HTML, not JavaScript listeners.  A
                // pending approval card that merely looks restored is inert
                // after refresh.  Rebuild this exact gate from the durable
                // collaboration snapshot so it owns fresh, gate-scoped
                // handlers; live duplicate events remain idempotent below.
                duplicateGate.remove();
            } else {
                console.debug(`Phase gate already rendered: ${this.currentPhaseGateId}`);
                return;
            }
        }
        // Make the phase available before rendering its confirmation UI; the
        // Phase-5 renderer needs the current WSI context immediately.
        this.currentPhaseData = {
            phase,
            reasoning,
            plan,
            evidence,
            selected_regions: selected_regions || [],
            originalSelection: JSON.parse(JSON.stringify(selected_regions || [])),
            allCandidates: all_candidates || [],
            subagent_results: subagent_results || [],
            report: report || '',
            reportable_findings: reportableFindings,
            metadata: metadata || {},
            wsi_context: wsi_context || null,
            phase_gate_id: this.currentPhaseGateId,
            requires_confirmation: requiresConfirmation,
            review_gate: data?.review_gate || '',
            file_id: String(data?.file_id || wsi_context?.file_id || '')
        };
        if ((Number(phase) === 2 || Number(phase) === 3) && Array.isArray(selected_regions)) {
            const nextIds = selected_regions.map(region => region?.id || region?.roi_id).filter(Boolean);
            if (nextIds.length) this._configureSubagentBoard(
                nextIds,
                0,
                'pending',
                { authoritativeLevel: Number(phase) === 2 ? 'blue' : 'green' }
            );
        }
        
        // ⭐⭐⭐ Patient Level 协作模式：更新各处显示当前 Phase ⭐⭐⭐
        if (this.isPatientLevelMode) {
            const partName = wsi_context?.part_name || `Part ${(this.currentAnalyzingWsiIndex || 0) + 1}`;
            const phaseName = phase_name || `Phase ${phase}`;
            
            // 更新左上角进度显示（Part X - 阶段名称）
            const currentWsiEl = document.getElementById('plCurrentWsiName');
            if (currentWsiEl) {
                currentWsiEl.textContent = `${partName} - ${phaseName}`;
                console.log(`[Phase ${phase}] 更新左上角状态: ${partName} - ${phaseName}`);
            }
            
            // 更新总状态标签
            const statusEl = document.getElementById('plStatus');
            if (statusEl) {
                statusEl.textContent = `Phase ${phase}/5`;
            }
            
            // ⭐⭐⭐ 更新进度状态文字（替换"分析进行中..."）⭐⭐⭐
            const progressStatus = document.getElementById('plProgressStatus');
            if (progressStatus) {
                progressStatus.textContent = `Phase ${phase}/5: ${phaseName}`;
            }
            
            // 更新左下角按钮显示当前 Phase
            const startBtn = document.getElementById('plStartAnalysis');
            if (startBtn) {
                startBtn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> Phase ${phase}/5`;
            }
        }
        
        // ⭐ 移除临时的"分析中"卡片（如果存在）
        const analyzingCard = document.querySelector('.analyzing-card');
        if (analyzingCard) {
            analyzingCard.remove();
            console.log(`[Phase ${phase}] 移除临时分析卡片`);
        }

        // 单片协作与 Patient Level Swarm 协作共用同一根 Agent 活动流。
        if (!this.isPatientLevelMode || this.isCollaborativeMode) {
            const isEn = (window.__lang || 'zh') === 'en';
            // Use the same product labels in both Auto and collaborative UI;
            // legacy backend names such as "缩略图分析" must not bring back
            // the old phase-card vocabulary.
            const phaseLabel = this.getPhaseName(phaseNumber) || phase_name || `Phase ${phaseNumber}`;
            const resultCount = Array.isArray(subagent_results) ? subagent_results.length : 0;
            const selectedCount = Array.isArray(selected_regions) ? selected_regions.length : 0;
            if (phaseNumber === 1 && selectedCount > 0) this.thumbnailRootCompleted = true;
            this._activateSubagentUi();
            const detail = phaseNumber === 1
                ? this._thumbnailReasonSummary(reasoning)
                : (isEn
                    ? `${resultCount} regions interpreted${selectedCount ? ` · ${selectedCount} regions selected for the next level` : ''}`
                    : `已完成 ${resultCount} 个区域解读${selectedCount ? ` · 选择 ${selectedCount} 个区域进入下一层` : ''}`);
            const waitingTitle = data?.review_gate === 'pre_report'
                ? (isEn ? 'Final evidence review · awaiting decision' : '终末证据审阅 · 等待审阅')
                : (phaseNumber === 5
                    ? (isEn ? 'Draft report · awaiting sign-off' : '拟报告 · 待定稿')
                    : `${phaseLabel} · ${isEn ? 'awaiting review' : '等待审阅'}`);
            this._appendActivityItem({
                title: requiresConfirmation
                    ? waitingTitle
                    : `${phaseLabel} · ${isEn ? 'complete' : '完成'}`,
                detail,
                state: requiresConfirmation ? 'thinking' : 'done',
                key: `collab-phase:${this.runtimeRunId || ''}:${phaseNumber}:${this.currentPhaseGateId || ''}:${Number.isInteger(thumbnailContext.wsiIndex) ? thumbnailContext.wsiIndex : ''}`,
                wsiIndex: Number.isInteger(thumbnailContext.wsiIndex) ? thumbnailContext.wsiIndex : null
            });
            if (phaseNumber === 1 && selectedCount > 0) {
                const redIds = selected_regions.map(region => region?.id || region?.roi_id).filter(Boolean);
                this._announceThumbnailPlan(
                    reasoning,
                    redIds,
                    selectedCount,
                    this._lastOverviewImageUrl || '',
                    thumbnailContext
                );
                this._configureSubagentBoard(
                    redIds,
                    selectedCount,
                    'pending',
                    { authoritativeLevel: 'red' }
                );
                this._updateChatActivity({
                    percentage: 100,
                    message: isEn ? 'Whole-slide thumbnail interpretation complete' : '全片缩略图解读完成',
                    wsiIndex: Number.isInteger(thumbnailContext.wsiIndex) ? thumbnailContext.wsiIndex : null
                });
            } else if (phaseNumber >= 2) {
                // Phase 1 already parks the root bar at 100%. Later gates must
                // do the same, otherwise the last incremental Phase 2/3
                // progress event (often ~20%) stays on screen while the
                // chat is already waiting for 执行 / 修改.
                this._updateChatActivity({
                    percentage: 100,
                    message: requiresConfirmation ? waitingTitle : `${phaseLabel} · ${isEn ? 'complete' : '完成'}`,
                    status: 'done',
                    wsiIndex: Number.isInteger(thumbnailContext.wsiIndex) ? thumbnailContext.wsiIndex : null
                });
            }
        } else if (this.isPatientLevelMode) {
            const isEn = (window.__lang || 'zh') === 'en';
            const partLabel = wsi_context?.part_name || data.part || '';
            const phaseLabel = this.getPhaseName(phaseNumber) || phase_name || `Phase ${phaseNumber}`;
            const resultCount = Array.isArray(subagent_results) ? subagent_results.length : 0;
            const selectedCount = Array.isArray(selected_regions) ? selected_regions.length : 0;
            this._activateSubagentUi();
            this._appendActivityItem({
                title: `${partLabel ? `${partLabel} · ` : ''}${phaseLabel} · ${isEn ? 'complete' : '完成'}`,
                detail: isEn
                    ? `${resultCount} regions interpreted`
                    : `已完成 ${resultCount} 个区域解读`,
                state: 'done',
                key: `patient-phase:${this._activePatientRunToken || ''}:${data.file_id || ''}:${phaseNumber}`
            });
            if (phaseNumber === 1 && selectedCount > 0) {
                const redIds = selected_regions.map(region => region?.id || region?.roi_id).filter(Boolean);
                this._configureSubagentBoard(redIds, selectedCount);
            }
        }
        
        // 1. 更新左侧面板：显示 Reasoning + Plan + Evidence
        this._withPatientSwarmMapContext(data, () => {
            this.updateLeftPanelPhaseResult(
                phase,
                reasoning,
                plan,
                evidence,
                selected_regions,
                subagent_results,
                report,
                requiresConfirmation
            );
        });
        
        // 2. 当前协作策略要求人工闸门时显示确认询问。
        // Patient Level Agent Swarm 复用单张 WSI 的分阶段确认；游走路线仍走切片终末审阅。
        if (this.isPatientLevelMode) {
            const gateFileId = this._patientRoamResolvedFileId(data);
            const gateIndex = this._indexForPatientFile(gateFileId);
            if (Number.isInteger(gateIndex)) {
                this.currentAnalyzingWsiIndex = gateIndex;
            }
            if (
                this.isCollaborativeMode
                && selected_regions
                && selected_regions.length > 0
            ) {
                this.syncPhaseResultToPatientLevelData(
                    phase,
                    selected_regions,
                    subagent_results,
                    this._patientSwarmMapWsiIndexFromEvent(data)
                        ?? thumbnailContext.wsiIndex
                );
            }
            const serialRun = (
                window.pathchat?.patientWsiExecutionMode
                || this.patientWsiExecutionMode
            ) === 'serial';
            if (
                Number.isInteger(gateIndex)
                && (requiresConfirmation || serialRun)
            ) {
                this.currentViewingWsiIndex = gateIndex;
                window.pathchat?.selectPlWsiItem?.(gateIndex);
            }
        }
        if (requiresConfirmation) {
            this.showPhaseConfirmMessage(phase, phase_name, selected_regions, report, data);
        }
        
        // 3. 在地图上高亮选中区域
        if (selected_regions && selected_regions.length > 0) {
            console.log(`[Phase ${phase}] 准备显示 overlays:`, {
                selected_regions_count: selected_regions.length,
                all_candidates_count: all_candidates?.length || 0,
                metadata_overlays_count: metadata?.overlays?.length || 0,
                first_region: selected_regions[0]
            });
            if (!this.isPatientLevelMode || this._patientLiveEventMatchesViewer(data)) {
                this.highlightPhaseRegions(selected_regions, metadata?.overlays);
            }
        } else {
            console.log(`[Phase ${phase}] 无 selected_regions，跳过 overlay 显示`);
        }
        
        // 4. 保存当前阶段数据用于修改（⭐ 包含完整候选框列表）
        this.currentPhaseData = {
            phase,
            reasoning,
            plan,
            evidence,  // ⭐ Agent Loop Evidence
            selected_regions: selected_regions || [],
            originalSelection: JSON.parse(JSON.stringify(selected_regions || [])),  // ⭐ 保存原始 AI 选择（深拷贝）
            allCandidates: all_candidates || [],  // ⭐ 完整候选框列表（用于点击添加）
            subagent_results: subagent_results || [],
            report: report || '',
            reportable_findings: reportableFindings,
            metadata: metadata || {},
            wsi_context: wsi_context || null,  // ⭐ Patient Level WSI 上下文
            phase_gate_id: this.currentPhaseGateId,
            requires_confirmation: requiresConfirmation,
            review_gate: data?.review_gate || '',
            file_id: String(data?.file_id || wsi_context?.file_id || '')
        };
        
        console.log(`[Phase ${phase}] 保存阶段数据: ${(all_candidates || []).length} 个候选框, ${(selected_regions || []).length} 个已选`);
    }
    
    /**
     * ⭐⭐⭐ Patient Level 协作模式：将 Phase 结果同步到 patientLevelAnalysisData
     * 确保切换 WSI 时能正确恢复 overlays
     */
    syncPhaseResultToPatientLevelData(phase, selected_regions, subagent_results, wsiIndex = null) {
        phase = Number(phase);
        wsiIndex = this._asPatientWsiIndex(wsiIndex)
            ?? this._asPatientWsiIndex(this.currentAnalyzingWsiIndex);
        if (!Number.isInteger(wsiIndex)) return;
        
        // 确保有该 WSI 的数据存储
        if (!this.patientLevelAnalysisData) {
            this.patientLevelAnalysisData = {};
        }
        if (!this.patientLevelAnalysisData[wsiIndex]) {
            this.patientLevelAnalysisData[wsiIndex] = { redBoxes: [] };
        }
        
        const wsiData = this.patientLevelAnalysisData[wsiIndex];
        
        if (phase === 1) {
            // Phase 1: 缩略图分析 - 存储红框选择
            console.log(`[Patient Level Collab] Phase 1 数据同步: ${selected_regions.length} 个红框`);
            
            selected_regions.forEach(region => {
                const redId = region.id || region.roi_id;
                const redRect = region.rect;
                
                // 查找或创建红框
                let redBox = wsiData.redBoxes.find(r => r.id === redId);
                if (!redBox) {
                    redBox = {
                        id: redId,
                        rect: redRect,
                        status: 'pending',
                        observation: '',
                        blueBoxes: []
                    };
                    wsiData.redBoxes.push(redBox);
                    console.log(`  ✅ 添加红框: ${redId}, rect:`, redRect);
                }
            });
        } else if (phase === 2) {
            // Phase 2: 红框分析 - 存储红框及其蓝框选择
            console.log(`[Patient Level Collab] Phase 2 数据同步: ${selected_regions.length} 个蓝框`);
            console.log(`[Patient Level Collab] Phase 2 subagent_results:`, subagent_results);
            
            // 从 subagent_results 中提取每个红框的蓝框信息
            if (subagent_results && subagent_results.length > 0) {
                subagent_results.forEach(result => {
                    const redId = result.roi_id;
                    const redRect = result.rect;
                    // ⭐ 修复：后端使用 blue_rois 而不是 selected_blues
                    const blueSelections = result.blue_rois || result.selected_blues || [];
                    console.log(`  📊 红框 ${redId}: rect=`, redRect, `蓝框数量=${blueSelections.length}`);
                    
                    // 查找或创建红框
                    let redBox = wsiData.redBoxes.find(r => r.id === redId);
                    if (!redBox) {
                        redBox = {
                            id: redId,
                            rect: redRect,
                            status: 'completed',
                            observation: result.reasoning || '',
                            blueBoxes: []
                        };
                        wsiData.redBoxes.push(redBox);
                    } else {
                        redBox.rect = redRect || redBox.rect;
                        redBox.observation = result.reasoning || redBox.observation;
                    }
                    
                    // 添加蓝框信息
                    blueSelections.forEach(blue => {
                        if (!redBox.blueBoxes.find(b => b.id === blue.id)) {
                            redBox.blueBoxes.push({
                                id: blue.id,
                                rect: blue.rect,
                                status: 'pending',
                                observation: '',
                                greenBoxes: []
                            });
                        }
                    });
                });
            } else {
                // 如果没有 subagent_results，从 selected_regions 构建
                selected_regions.forEach(region => {
                    const redId = region.id || region.roi_id;
                    let redBox = wsiData.redBoxes.find(r => r.id === redId);
                    if (!redBox) {
                        redBox = {
                            id: redId,
                            rect: region.rect,
                            status: 'completed',
                            observation: '',
                            blueBoxes: []
                        };
                        wsiData.redBoxes.push(redBox);
                    }
                });
            }
        } else if (phase === 3) {
            // Phase 3: 蓝框分析 → 产出绿框
            // selected_regions 是绿框列表，subagent_results 是每个蓝框的分析结果（含 green_rois）
            console.log(`[Patient Level Collab] Phase 3 数据同步: ${selected_regions.length} 个绿框, ${(subagent_results || []).length} 个蓝框结果`);
            
            if (subagent_results && subagent_results.length > 0) {
                subagent_results.forEach(result => {
                    const blueId = result.roi_id;
                    const greenRois = result.green_rois || [];
                    
                    for (const redBox of wsiData.redBoxes) {
                        const blueBox = redBox.blueBoxes.find(b => b.id === blueId);
                        if (blueBox) {
                            blueBox.status = 'completed';
                            blueBox.observation = result.reasoning || blueBox.observation;
                            if (result.rect) blueBox.rect = result.rect;
                            
                            greenRois.forEach(green => {
                                if (!blueBox.greenBoxes.find(g => g.id === green.id)) {
                                    blueBox.greenBoxes.push({
                                        id: green.id,
                                        rect: green.rect,
                                        status: 'pending',
                                        observation: ''
                                    });
                                }
                            });
                            console.log(`    🔵 ${blueId}: +${greenRois.length} 绿框`);
                            break;
                        }
                    }
                });
            }
            
            // 兜底：从 selected_regions（绿框列表）补充
            if (selected_regions && selected_regions.length > 0) {
                selected_regions.forEach(region => {
                    const greenId = region.id || region.roi_id;
                    const parentBlueId = region.parent_id || greenId.split('-').slice(0, 2).join('-');
                    
                    for (const redBox of wsiData.redBoxes) {
                        const blueBox = redBox.blueBoxes.find(b => b.id === parentBlueId);
                        if (blueBox) {
                            if (!blueBox.greenBoxes.find(g => g.id === greenId)) {
                                blueBox.greenBoxes.push({
                                    id: greenId,
                                    rect: region.rect,
                                    status: 'pending',
                                    observation: ''
                                });
                            }
                            break;
                        }
                    }
                });
            }
        } else if (phase === 4) {
            // Phase 4: 绿框细胞级分析 → 更新绿框观察结果
            console.log(`[Patient Level Collab] Phase 4 数据同步: ${(subagent_results || []).length} 个绿框分析`);
            
            if (subagent_results && subagent_results.length > 0) {
                subagent_results.forEach(result => {
                    const greenId = result.roi_id;
                    
                    for (const redBox of wsiData.redBoxes) {
                        let found = false;
                        for (const blueBox of redBox.blueBoxes) {
                            let greenBox = blueBox.greenBoxes.find(g => g.id === greenId);
                            if (greenBox) {
                                greenBox.status = 'completed';
                                greenBox.observation = result.reasoning || result.findings || '';
                                if (result.rect) greenBox.rect = result.rect;
                                found = true;
                                break;
                            }
                        }
                        if (found) break;
                    }
                });
            }
            
            // 兜底：从 selected_regions 补充未在 Phase 3 中同步的绿框
            if (selected_regions && selected_regions.length > 0) {
                selected_regions.forEach(region => {
                    const greenId = region.id || region.roi_id;
                    const parentBlueId = region.parent_id || greenId.split('-').slice(0, 2).join('-');
                    
                    let exists = false;
                    for (const redBox of wsiData.redBoxes) {
                        for (const blueBox of redBox.blueBoxes) {
                            if (blueBox.greenBoxes.find(g => g.id === greenId)) {
                                exists = true;
                                break;
                            }
                        }
                        if (exists) break;
                    }
                    
                    if (!exists && region.rect) {
                        for (const redBox of wsiData.redBoxes) {
                            const blueBox = redBox.blueBoxes.find(b => b.id === parentBlueId);
                            if (blueBox) {
                                blueBox.greenBoxes.push({
                                    id: greenId,
                                    rect: region.rect,
                                    status: 'completed',
                                    observation: ''
                                });
                                break;
                            }
                        }
                    }
                });
            }
        }
        
        // 验证数据完整性
        let totalGreen = 0;
        console.log(`[Patient Level Collab] 数据同步完成, WSI#${wsiIndex}:`);
        wsiData.redBoxes.forEach(r => {
            const greenCount = r.blueBoxes.reduce((sum, b) => sum + b.greenBoxes.length, 0);
            totalGreen += greenCount;
            console.log(`  🔴 ${r.id}: rect=${r.rect ? 'OK' : 'MISSING'}, blueBoxes=${r.blueBoxes.length}, greenBoxes=${greenCount}`);
            r.blueBoxes.forEach(b => {
                console.log(`    🔵 ${b.id}: rect=${b.rect ? 'OK' : 'MISSING'}, greenBoxes=${b.greenBoxes.length}`);
            });
        });
        console.log(`  📊 总计: ${wsiData.redBoxes.length}红 + ${wsiData.redBoxes.reduce((s,r) => s+r.blueBoxes.length, 0)}蓝 + ${totalGreen}绿`);
    }
    
    
    /**
     * 更新左侧面板阶段结果
     */
    updateLeftPanelPhaseResult(
        phase,
        reasoning,
        plan,
        evidence,
        selected_regions,
        subagent_results,
        report,
        requiresConfirmation = true
    ) {
        console.log(`🔧 [Phase ${phase}] updateLeftPanelPhaseResult 被调用:`, {
            reasoning: reasoning ? reasoning.substring(0, 50) + '...' : '(empty)',
            plan: plan,
            evidence: evidence,
            selected_regions_count: selected_regions?.length || 0
        });
        
        let list = null;
        
        // 单切片模式 / Patient Level：阶段卡进入当前切片自己的 Subagent map。
        // Patient Level 左侧病例栏只保留缩略图列表，不再展开分析树。
        if (!list) {
            list = this._patientMapList() || document.getElementById('routeStepsList');
        }
        
        if (!list) {
            console.error(`❌ [Phase ${phase}] 找不到目标容器!`);
            return;
        }
        
        // 移除空状态提示
        const emptyState = list.querySelector('.route-empty-state');
        if (emptyState) emptyState.remove();

        // Snapshot restore and Runtime event catch-up can legitimately deliver
        // the same phase result twice.  Keep one durable card per
        // run/file/phase/gate so a restored pending copy cannot survive after
        // the live card has already been confirmed.
        const phaseCardIdentity = {
            phase: String(phase),
            gateId: String(this.currentPhaseData?.phase_gate_id || this.currentPhaseGateId || ''),
            runId: String(this.runtimeRunId || ''),
            fileId: String(
                this.currentPhaseData?.file_id
                || this.currentPhaseData?.wsi_context?.file_id
                || this.getCurrentFileId()
                || ''
            )
        };
        const existingPhaseCard = Array.from(list.querySelectorAll('.phase-result-card')).find((candidate) => (
            String(candidate.dataset.phase || '') === phaseCardIdentity.phase
            && String(candidate.dataset.gateId || '') === phaseCardIdentity.gateId
            && String(candidate.dataset.runId || '') === phaseCardIdentity.runId
            && String(candidate.dataset.fileId || '') === phaseCardIdentity.fileId
        ));
        if (existingPhaseCard) {
            console.debug(`Phase ${phase} result card already rendered for this run/gate`);
            return existingPhaseCard;
        }
        
        // 创建阶段结果卡片
        const card = document.createElement('div');
        card.className = Number(phase) === 5
            ? 'phase-result-card'
            : 'phase-result-card collab-auto-parity';
        card.dataset.phase = phaseCardIdentity.phase;
        card.dataset.gateId = phaseCardIdentity.gateId;
        card.dataset.runId = phaseCardIdentity.runId;
        card.dataset.fileId = phaseCardIdentity.fileId;
        this._tagPatientSwarmTreeNode(card);
        
        // 根据阶段类型选择语义色。颜色只通过 CSS 变量暴露，避免旧版
        // inline style 覆盖当前暖色/高对比主题。
        const phaseColors = {
            1: 'var(--science-accent, #d97757)',  // Orchestrator / 全片
            2: '#ef4444',  // 红色
            3: 'var(--science-blue, #3679bc)',    // 蓝色
            4: 'var(--science-success, #4a9568)', // 绿色
            5: 'var(--science-warning, #b7792f)'  // 报告
        };
        const phaseColor = phaseColors[phase] || 'var(--science-accent, #d97757)';
        card.style.setProperty('--phase-accent', phaseColor);
        card.classList.add(`phase-tone-${phase}`);
        
        const phaseStatusClass = requiresConfirmation ? 'pending' : 'completed';
        const phaseStatusText = requiresConfirmation
            ? __t('status_pending')
            : __t('status_completed');
        let cardContent = `
            <div class="phase-header">
                <span class="phase-badge">${(window.__lang || 'zh') === 'en' ? 'STAGE' : '阶段'} ${phase}</span>
                <span class="phase-title">${this.getPhaseName(phase)}</span>
                <span class="phase-status ${phaseStatusClass}">${phaseStatusText}</span>
                <button type="button" class="phase-collapse-toggle" aria-expanded="true"
                        aria-label="${(window.__lang || 'zh') === 'en' ? 'Collapse stage details' : '收起阶段详情'}">
                    <i class="fas fa-chevron-up" aria-hidden="true"></i>
                </button>
            </div>
        `;
        
        // Phase 5 显示报告内容
        if (phase === 5 && report) {
            cardContent += `
                <div class="phase-report">
                    <h4><i class="fas fa-file-medical"></i> ${__t('diagnostic_report')}</h4>
                    <div class="report-content">${this.formatMarkdown(report)}</div>
                </div>
            `;
        } else if (phase === 1) {
            // ⭐ Phase 1: 直接显示 AI 的完整原始输出
            console.log(`🔍 [Phase 1] reasoning 完整内容 (长度: ${reasoning?.length || 0}):`, reasoning);
            if (reasoning) {
                cardContent += `
                    <div class="phase-ai-output">
                        <div class="ai-output-label"><i class="fas fa-brain"></i> ${__t('ai_global_analysis')}</div>
                        <div class="ai-output-content">${this.formatAIOutput(reasoning)}</div>
                    </div>
                `;
            }
            
            // 显示选中区域。查看态由上方 Auto 根活动流概括这一步；
            // 这里仅保留协作编辑所需的 checkbox 数据，避免再生成一套
            // 与 Phase 2 红框分析重复的可见红框树。
            if (selected_regions && selected_regions.length > 0) {
                cardContent += `
                    <div class="phase-regions">
                        <h4><i class="fas fa-map-marker-alt"></i> ${__t('selected_regions')} (${selected_regions.length})</h4>
                        <div class="regions-list">
                            ${selected_regions.map(r => `
                                <div class="region-item" data-roi-id="${r.id}" data-rect='${JSON.stringify(r.rect)}' data-parent-id="${r.parent_id || ''}">
                                    <input type="checkbox" checked disabled class="region-checkbox" data-original-checked="true">
                                    <button type="button" class="region-focus-button"
                                            aria-label="${(window.__lang || 'zh') === 'en' ? 'Focus region' : '定位区域'} ${this._escapeHtmlContent(String(r.id || ''))}">
                                        <span class="region-focus-icon" aria-hidden="true"><i class="fas fa-location-arrow"></i></span>
                                        <span class="region-id">${r.id}</span>
                                        <span class="region-info">${this._escapeHtmlContent(String(r.reason || ''))}</span>
                                        <i class="fas fa-chevron-right region-disclosure" aria-hidden="true"></i>
                                    </button>
                                </div>
                            `).join('')}
                        </div>
                    </div>
                `;
            }
        } else if (phase >= 2 && phase <= 4 && subagent_results && subagent_results.length > 0) {
            // ⭐⭐⭐ Phase 2-4: 完全参照自动模式的树状结构显示 ⭐⭐⭐
            // Phase 2 输入红框产出蓝框，Phase 3 输入蓝框产出绿框，Phase 4 分析绿框
            const phaseConfig = {
                2: { inputIcon: '🔴', inputName: __t('red_box'), outputIcon: '🔵', outputName: __t('blue_box'), outputColor: '#007aff' },
                3: { inputIcon: '🔵', inputName: __t('blue_box'), outputIcon: '🟢', outputName: __t('green_box'), outputColor: '#34c759' },
                4: { inputIcon: '🟢', inputName: __t('green_box'), outputIcon: '🔬', outputName: 'Cell', outputColor: '#34c759' }
            };
            const config = phaseConfig[phase] || phaseConfig[2];
            
            // 使用自动模式的树状结构
            cardContent += `<div class="collab-tree-container">`;
            
            subagent_results.forEach((result, index) => {
                const roiId = result.roi_id || `Region ${index + 1}`;
                const isSuccess = result.success !== false;
                // ⭐ 优先使用 reasoning（AI完整分析），这是自动模式显示的内容
                const aiOutputRaw = result.reasoning || result.findings || result.error || '';
                const selectedCount = result.blue_rois?.length || result.green_rois?.length || 0;
                
                console.log(`[Phase ${phase}] SubAgent ${roiId}: aiOutput_length=${aiOutputRaw?.length || 0}`);
                const statusClass = isSuccess ? 'completed' : 'error';
                const resultStatusText = isSuccess
                    ? __t('status_completed')
                    : ((window.__lang || 'zh') === 'en' ? 'Failed' : '失败');
                const rectStr = result.rect ? JSON.stringify(result.rect) : '';
                
                cardContent += `
                    <div class="route-tree-node collab-tree-node" data-tree-id="${roiId}" data-roi-id="${roiId}" data-rect='${rectStr}' data-level="${phase === 2 ? 'red' : (phase === 3 ? 'blue' : 'green')}">
                        <div class="route-tree-header collapsed" data-roi-id="${roiId}">
                            <span class="route-tree-toggle"><i class="fas fa-chevron-down"></i></span>
                            <span class="route-tree-title">
                                <span class="tree-icon">${config.inputIcon}</span>
                                ${roiId} ${resultStatusText}
                            </span>
                            <span class="route-tree-badge ${statusClass}">${resultStatusText}</span>
                            ${selectedCount > 0 ? `<span class="route-tree-count">${config.outputIcon} ${selectedCount}</span>` : ''}
                            <button class="btn-edit-analysis" data-roi-id="${roiId}" data-phase="${phase}"
                                    title="${(window.__lang || 'zh') === 'en' ? 'Edit analysis' : '编辑分析'}"
                                    aria-label="${(window.__lang || 'zh') === 'en' ? 'Edit analysis' : '编辑分析'} ${roiId}">
                                <i class="fas fa-pen"></i>
                            </button>
                            <button class="btn-quote-analysis" data-roi-id="${roiId}" data-phase="${phase}"
                                    title="${(window.__lang || 'zh') === 'en' ? 'Quote to chat' : '引用到对话'}"
                                    aria-label="${(window.__lang || 'zh') === 'en' ? 'Quote to chat' : '引用到对话'} ${roiId}">
                                <i class="fas fa-comment-medical"></i>
                            </button>
                        </div>
                        <div class="route-tree-content collab-phase-content" data-roi-id="${roiId}"></div>
                    </div>
                `;
            });
            
            cardContent += `</div>`;
            
            // 显示汇总的选中区域（可勾选修改）
            if (selected_regions && selected_regions.length > 0) {
                cardContent += `
                    <div class="phase-regions">
                        <h4><i class="fas fa-map-marker-alt"></i> ${__t('selected_regions')} (${selected_regions.length})</h4>
                        <div class="regions-list">
                            ${selected_regions.map(r => `
                                <div class="region-item" data-roi-id="${r.id}" data-rect='${JSON.stringify(r.rect)}' data-parent-id="${r.parent_id || ''}">
                                    <input type="checkbox" checked disabled class="region-checkbox" data-original-checked="true">
                                    <button type="button" class="region-focus-button"
                                            aria-label="${(window.__lang || 'zh') === 'en' ? 'Focus region' : '定位区域'} ${this._escapeHtmlContent(String(r.id || ''))}">
                                        <span class="region-focus-icon" aria-hidden="true"><i class="fas fa-location-arrow"></i></span>
                                        <span class="region-id">${r.id}</span>
                                        <span class="region-info">${r.parent_id ? __t('parent_region', r.parent_id) : ''}</span>
                                        <i class="fas fa-chevron-right region-disclosure" aria-hidden="true"></i>
                                    </button>
                                </div>
                            `).join('')}
                        </div>
                    </div>
                `;
            }
        }
        
        card.innerHTML = cardContent;

        // 将阶段内容包进可折叠区域。新阶段到达时仅折叠此前阶段，当前结果
        // 始终展开；审批按钮仍位于独立聊天 gate，不会被折叠吞掉。
        const phaseHeader = card.querySelector('.phase-header');
        const phaseBody = document.createElement('div');
        phaseBody.className = 'phase-card-body';
        phaseBody.id = `phase-body-${phase}-${Math.random().toString(36).slice(2, 9)}`;
        Array.from(card.children).forEach((child) => {
            if (child !== phaseHeader) phaseBody.appendChild(child);
        });
        card.appendChild(phaseBody);
        const collapseToggle = card.querySelector('.phase-collapse-toggle');
        collapseToggle?.setAttribute('aria-controls', phaseBody.id);
        Array.from(list.querySelectorAll('.phase-result-card')).forEach((previousCard) => {
            if (
                previousCard !== card
                && !previousCard.classList.contains('collab-auto-parity')
            ) {
                this._setPhaseCardCollapsed(previousCard, true);
            }
        });
        collapseToggle?.addEventListener('click', () => {
            this._setPhaseCardCollapsed(card, !card.classList.contains('is-collapsed'));
        });
        list.appendChild(card);
        
        // ⭐ 安全填充 AI 分析文本：用 textContent 避免 HTML 解析截断
        if (subagent_results && subagent_results.length > 0) {
            subagent_results.forEach((result) => {
                const roiId = result.roi_id || '';
                const rawText = result.reasoning || result.findings || result.error || '';
                if (rawText) {
                    const contentEl = card.querySelector(`.route-tree-content[data-roi-id="${roiId}"]`);
                    if (contentEl) {
                        contentEl.textContent = rawText;
                        contentEl.dataset.originalAnalysis = rawText;
                        const treeNode = contentEl.closest('.collab-tree-node');
                        if (treeNode) {
                            treeNode.dataset.originalAnalysis = rawText;
                        }
                    }
                }
            });
        }
        
        // ⭐ 1. 绑定区域点击事件（聚焦到 WSI）
        card.querySelectorAll('.region-focus-button').forEach(button => {
            button.addEventListener('click', () => {
                const item = button.closest('.region-item');
                const rect = JSON.parse(item.dataset.rect || '{}');
                const roiId = item.dataset.roiId;
                this.focusOnRegion(rect, roiId);
            });
        });
        
        // ⭐ 2. 绑定 checkbox change 事件（实时同步 WSI overlays）
        card.querySelectorAll('.region-checkbox').forEach(checkbox => {
            checkbox.addEventListener('change', (e) => {
                const regionItem = e.target.closest('.region-item');
                const roiId = regionItem?.dataset?.roiId || '';
                console.log(`[Phase ${phase}] checkbox 变化: ${roiId} → ${e.target.checked ? '选中' : '取消'}`);
                
                // ⭐ 修复：取消勾选时，同时移除左侧面板的黄色高亮
                if (!e.target.checked && regionItem) {
                    regionItem.classList.remove('roi-selected-glow');
                }
                
                this.updateOverlaysFromCheckboxes(phase);
            });
        });
        
        // ⭐ 3. 绑定树节点展开/收起事件（参照自动模式）
        card.querySelectorAll('.route-tree-header').forEach(header => {
            const titleControl = header.querySelector('.route-tree-title');
            const toggleControl = header.querySelector('.route-tree-toggle');
            if (titleControl) {
                titleControl.setAttribute('role', 'button');
                titleControl.tabIndex = 0;
                titleControl.setAttribute('aria-label', (window.__lang || 'zh') === 'en'
                    ? `Focus ${header.dataset.roiId || 'region'}`
                    : `定位区域 ${header.dataset.roiId || ''}`);
                titleControl.addEventListener('keydown', (e) => {
                    if (e.key !== 'Enter' && e.key !== ' ') return;
                    e.preventDefault();
                    const node = header.closest('.route-tree-node');
                    const rect = node?.dataset.rect ? JSON.parse(node.dataset.rect) : null;
                    if (rect) this.focusOnRegion(rect, header.dataset.roiId);
                });
            }
            if (toggleControl) {
                toggleControl.setAttribute('role', 'button');
                toggleControl.tabIndex = 0;
                toggleControl.setAttribute('aria-expanded', 'false');
                toggleControl.setAttribute('aria-label', (window.__lang || 'zh') === 'en'
                    ? 'Expand or collapse analysis details'
                    : '展开或收起分析详情');
                toggleControl.addEventListener('keydown', (e) => {
                    if (e.key !== 'Enter' && e.key !== ' ') return;
                    e.preventDefault();
                    e.stopPropagation();
                    header.classList.toggle('collapsed');
                    toggleControl.setAttribute('aria-expanded', String(!header.classList.contains('collapsed')));
                });
            }
            header.addEventListener('click', (e) => {
                // 如果点击的是展开/收起按钮
                if (e.target.closest('.route-tree-toggle')) {
                    header.classList.toggle('collapsed');
                    toggleControl?.setAttribute('aria-expanded', String(!header.classList.contains('collapsed')));
                    return;
                }
                // 如果点击的是编辑按钮，不处理（由编辑按钮自己的事件处理）
                if (e.target.closest('.btn-edit-analysis')) {
                    return;
                }
                // 否则聚焦到对应区域
                const roiId = header.dataset.roiId;
                const node = header.closest('.route-tree-node');
                const rect = node?.dataset.rect ? JSON.parse(node.dataset.rect) : null;
                if (rect) {
                    this.focusOnRegion(rect, roiId);
                }
            });
        });
        
        // ⭐⭐⭐ 4. 绑定编辑分析结果按钮事件（协作模式专用）⭐⭐⭐
        // 4a. 直接编辑按钮（笔图标）
        card.querySelectorAll('.btn-edit-analysis').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();  // 阻止事件冒泡到 header
                const roiId = btn.dataset.roiId;
                const btnPhase = parseInt(btn.dataset.phase) || phase;
                // ⭐ 重要：从按钮向上查找对应的树节点
                const treeNode = btn.closest('.collab-tree-node') || btn.closest('.route-tree-node');
                this.enterAnalysisEditMode(roiId, btnPhase, treeNode);
            });
        });
        
        // 4b. 引用到聊天按钮（对话图标）- 让 AI 重新解读
        card.querySelectorAll('.btn-quote-analysis').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();  // 阻止事件冒泡到 header
                const roiId = btn.dataset.roiId;
                const btnPhase = parseInt(btn.dataset.phase) || phase;
                const treeNode = btn.closest('.collab-tree-node') || btn.closest('.route-tree-node');
                this.quoteAnalysisToChat(roiId, btnPhase, treeNode);
            });
        });

        // Auto renders a real R → B → G hierarchy. Collaborative mode must
        // preserve the same parentage instead of presenting three flat phase
        // lists with decorative indentation. Move stable child nodes beneath
        // their exact parent after all handlers are attached.
        this._nestCollaborativePhaseNodes(list, card, phase, phaseColor);
        
        // 滚动到底部
        list.scrollTop = list.scrollHeight;
    }

    _nestCollaborativePhaseNodes(list, card, phase, phaseColor) {
        if (!list || !card || ![3, 4].includes(Number(phase))) return;
        const nodes = Array.from(card.querySelectorAll(':scope .collab-tree-node'));
        let moved = 0;
        nodes.forEach((node) => {
            const roiId = String(node.dataset.roiId || '');
            const parts = roiId.split('-').filter(Boolean);
            const parentId = Number(phase) === 3
                ? parts.slice(0, 1).join('-')
                : parts.slice(0, 2).join('-');
            if (!parentId || parentId === roiId) return;
            const parent = Array.from(list.querySelectorAll('.collab-tree-node')).find(
                (candidate) => String(candidate.dataset.roiId || '') === parentId
            );
            if (!parent || parent === node) return;
            let children = Array.from(parent.children).find(
                (child) => child.classList?.contains('route-tree-children')
            );
            if (!children) {
                children = document.createElement('div');
                children.className = 'route-tree-children collab-route-tree-children';
                children.dataset.parentId = parentId;
                parent.appendChild(children);
            }
            node.dataset.parentId = parentId;
            node.style.setProperty('--phase-accent', phaseColor);
            children.appendChild(node);
            moved += 1;
        });
        if (moved === nodes.length && nodes.length > 0) {
            card.classList.add('collab-hierarchy-anchor');
            card.setAttribute('aria-hidden', 'true');
        }
    }
    
    /**
     * 格式化 AI 输出（保留换行和基本格式）
     */
    formatAIOutput(text) {
        if (!text) return '';
        return text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/\n/g, '<br>')
            .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
            .replace(/\*(.*?)\*/g, '<em>$1</em>');
    }

    _escapeHtmlContent(text) {
        if (!text) return '';
        return text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }
    
    /**
     * 显示阶段确认消息（右侧聊天）
     */
    showPhaseConfirmMessage(phase, phase_name, selected_regions, report, phaseData = null) {
        const chatMessages = document.getElementById('chatMessages');
        if (!chatMessages) return;
        
        // ⭐ 确保 phase 是数字类型（避免 "5" === 5 为 false 的问题）
        const phaseNum = parseInt(phase, 10);
        const gateId = String(this.currentPhaseGateId || '');
        if (
            gateId
            && chatMessages.querySelector(
                `.phase-confirm-message[data-gate-id="${CSS.escape(gateId)}"]`
            )
        ) return;
        
        // 创建确认消息
        const confirmMsg = document.createElement('div');
        confirmMsg.className = 'chat-message assistant phase-confirm-message';
        confirmMsg.dataset.phase = phaseNum;
        confirmMsg.dataset.gateId = gateId;
        confirmMsg.dataset.runId = String(this.runtimeRunId || '');
        // A collaboration snapshot may arrive just before the visible slide is
        // rebound after refresh.  Use the Runtime's stable file identity rather
        // than freezing an empty transient UI value.
        confirmMsg.dataset.fileId = String(
            phaseData?.file_id
            || phaseData?.wsi_context?.file_id
            || this.currentFileId
            || this.runtimeRunFileId
            || this.getCurrentFileId()
            || ''
        );
        const confirmWsiIndex = window.pathchat?._patientSlideIndexFromFileId?.(confirmMsg.dataset.fileId);
        const resolvedWsiIndex = Number.isInteger(confirmWsiIndex)
            ? confirmWsiIndex
            : (Number.isInteger(this.currentAnalyzingWsiIndex) ? this.currentAnalyzingWsiIndex : null);
        if (Number.isInteger(resolvedWsiIndex)) {
            confirmMsg.dataset.wsiIndex = String(resolvedWsiIndex);
        }
        
        let summaryText = '';
        if (phaseNum === 5) {
            summaryText = __t('phase_report_done');
        } else if (selected_regions && selected_regions.length > 0) {
            summaryText = __t('phase_regions_found', selected_regions.length);
        } else {
            summaryText = __t('phase_analysis_done');
        }

        if (phaseData?.review_gate === 'pre_report') {
            const isEn = (window.__lang || 'zh') === 'en';
            confirmMsg.classList.add('pre-report-review-message');
            confirmMsg.innerHTML = `
                <div class="message-avatar"><i class="fas fa-user-check"></i></div>
                <div class="message-content">
                    <div class="message-text">
                        <p><strong>${isEn ? 'Final evidence review' : '终末证据审阅'}</strong></p>
                        <p>${isEn
                            ? 'Inspect the highlighted unresolved ROI evidence. The final report has not been generated yet.'
                            : '请检查已高亮的未解决 ROI 证据；此时最终报告尚未生成。'}</p>
                    </div>
                    <div class="phase-confirm-buttons">
                        <button class="btn-execute" data-phase="${phaseNum}">
                            <i class="fas fa-check-circle"></i>
                            ${isEn ? 'Review complete · Generate report' : '已完成审阅 · 生成终报'}
                        </button>
                    </div>
                </div>
            `;
            chatMessages.appendChild(confirmMsg);
            window.pathchat?.syncPatientSlideReportNav?.();
            confirmMsg.querySelector('.btn-execute')?.addEventListener('click', () => {
                this.confirmPhase(phaseNum, 'execute', null, confirmMsg, () => {
                    const messageText = confirmMsg.querySelector('.message-text');
                    if (messageText) {
                        messageText.innerHTML = `
                            <p><strong>${isEn ? 'Evidence review confirmed' : '证据审阅已确认'}</strong></p>
                            <p>${isEn
                                ? 'The Orchestrator is now generating the final report.'
                                : 'Orchestrator 正在据此生成最终报告。'}</p>
                        `;
                    }
                    const actions = confirmMsg.querySelector('.phase-confirm-buttons');
                    if (actions) {
                        actions.innerHTML = `<span class="phase-confirmed-status">
                            <i class="fas fa-check-circle"></i>
                            ${isEn ? 'Approved' : '已批准'}
                        </span>`;
                    }
                });
            });
            chatMessages.scrollTop = chatMessages.scrollHeight;
            return;
        }
        
        // ⭐ Phase 5 是最后一个阶段（诊断报告），需要用户审核确认
        if (phaseNum === 5) {
            // 获取文件ID用于HTML报告路径（根据模式使用不同的子目录）
            const fileId = String(
                confirmMsg.dataset.fileId
                || phaseData?.file_id
                || phaseData?.wsi_context?.file_id
                || this.getCurrentFileId()
                || ''
            );
            const modeSubdir = this.currentMode === 'collaborative' ? 'collab' : 'auto';
            
            // ⭐⭐⭐ Patient Level 模式：使用正确的路径格式 ⭐⭐⭐
            let htmlReportPath = null;
            if (this.isPatientLevelMode) {
                // Patient Level 模式：路径格式为 /outputs/patient_{patient_id}/{mode}/wsi_{file_id}/pathology_report.html
                const patientId = this.currentPhaseData?.wsi_context?.patient_id || 
                                  window.pathchat?.currentPatientId || 
                                  this.patientId;
                if (patientId && fileId) {
                    htmlReportPath = `/outputs/patient_${patientId}/${modeSubdir}/wsi_${fileId}/pathology_report.html`;
                }
                console.log(`[Phase 5] Patient Level HTML报告路径: ${htmlReportPath}`);
            } else {
                // 单张模式：路径格式为 /outputs/{file_id}/{mode}/pathology_report.html
                htmlReportPath = fileId ? `/outputs/${fileId}/${modeSubdir}/pathology_report.html` : null;
            }
            
            confirmMsg.classList.add('draft-report-chat-message');
            chatMessages.appendChild(confirmMsg);
            this._syncPatientChatGatesVisibility();
            window.pathchat?.syncPatientSlideReportNav?.();
            this.showDraftReportModal({
                report,
                findings: phaseData?.reportable_findings || this.currentPhaseData?.reportable_findings || [],
                confirmMsg,
            });
            chatMessages.scrollTop = chatMessages.scrollHeight;
            return;  // Phase 5 处理完毕，等待用户审核
        }
        
        // Phase 1-4: 显示"是否继续下一阶段"
        const slideLabel = this.isPatientLevelMode
            ? String(phaseData?.part || phaseData?.wsi_context?.part_name || phaseData?.filename || '').trim()
            : '';
        const phaseHeading = slideLabel ? `${slideLabel} · ${phase_name}` : phase_name;
        confirmMsg.innerHTML = `
            <div class="message-avatar">
                <i class="fas fa-robot"></i>
            </div>
            <div class="message-content">
                <div class="message-text">
                    <p><strong>${phaseHeading}</strong> ${__t('phase_analysis_complete')}</p>
                    <p>${summaryText}</p>
                    <p>${__t('phase_continue_next')}</p>
                </div>
                <div class="phase-confirm-buttons">
                    <button class="btn-execute" data-phase="${phaseNum}">
                        <i class="fas fa-play"></i> ${__t('btn_execute')}
                    </button>
                    <button class="btn-modify" data-phase="${phaseNum}">
                        <i class="fas fa-edit"></i> ${__t('btn_modify')}
                    </button>
                </div>
            </div>
        `;
        
        chatMessages.appendChild(confirmMsg);
        this._syncPatientChatGatesVisibility();
        
        // 绑定按钮事件
        confirmMsg.querySelector('.btn-execute').addEventListener('click', async () => {
            // ⭐⭐⭐ 优化：执行前检查是否有待提交的修改 ⭐⭐⭐
            if (this.hasPendingChanges()) {
                console.log(`[Phase ${phaseNum}] 检测到待提交的修改，先提交修改`);
                const result = await this.submitPendingChanges(phaseNum, confirmMsg);
                
                // 如果用户取消了修改，不继续执行
                if (result.cancelled) {
                    console.log(`[Phase ${phaseNum}] 用户取消了修改提交，不执行`);
                    return;
                }
                
                // ⭐⭐⭐ 修改提交后，后端会自动继续执行，不需要再发送 execute ⭐⭐⭐
                console.log(`[Phase ${phaseNum}] 修改已提交，后端将自动继续执行`);
                if (this.currentPhaseData) {
                    this.currentPhaseData.pendingChanges = null;
                }
                this.disablePhaseButtons(confirmMsg);
                return;  // ⭐ 不再发送 execute 消息
            }
            
            // 没有修改，直接发送 execute
            this.confirmPhase(phaseNum, 'execute', null, confirmMsg);
        });
        
        confirmMsg.querySelector('.btn-modify').addEventListener('click', () => {
            this.enterPhaseEditMode(phaseNum);
        });
        
        // 滚动到底部
        chatMessages.scrollTop = chatMessages.scrollHeight;
        window.pathchat?.syncPatientSlideReportNav?.();
    }
    
    /**
     * 发送阶段确认结果
     */
    _phaseApprovalContext(phase, messageElement = null) {
        const requestedPhase = Number(phase);
        const card = messageElement || (() => {
            const currentGate = String(
                this.currentPhaseData?.phase_gate_id
                || this.currentPhaseGateId
                || ''
            );
            return currentGate
                ? document.querySelector(
                    `.phase-confirm-message[data-gate-id="${CSS.escape(currentGate)}"]`
                )
                : document.querySelector(
                    `.phase-confirm-message[data-phase="${requestedPhase}"]:not(.is-resolved)`
                );
        })();
        const cardPhase = Number(card?.dataset?.phase || requestedPhase);
        let gateId = String(
            card?.dataset?.gateId
            || this.currentPhaseGateId
            || this.currentPhaseData?.phase_gate_id
            || ''
        );
        let runId = String(card?.dataset?.runId || '');
        let fileId = String(
            card?.dataset?.fileId
            || this.currentPhaseData?.file_id
            || this.currentPhaseData?.wsi_context?.file_id
            || this._thumbnailAnnounceFileId
            || ''
        );
        if (card) {
            if (gateId && !card.dataset.gateId) card.dataset.gateId = gateId;
            if (fileId && !card.dataset.fileId) card.dataset.fileId = fileId;
        }
        const currentRunId = String(this.runtimeRunId || '');
        const currentFileId = String(
            this.currentFileId
            || this.runtimeRunFileId
            || this.getCurrentFileId()
            || ''
        );
        const patientSlideMatches = Boolean(
            this.isPatientLevelMode
            && fileId
            && (
                this._indexForPatientFile(fileId) != null
                || fileId === currentFileId
                || fileId === String(this.currentPhaseData?.file_id || '')
            )
        );
        // Patient Level workers do not share a single-WSI Runtime run id.
        // A leftover id after refresh or a prior single-WSI run must not
        // block "Agree to finalize" on an otherwise valid slide gate.
        const runIdMatches = this.isPatientLevelMode
            ? (!runId || !currentRunId || runId === currentRunId)
            : (runId === currentRunId);
        const fileMatches = Boolean(
            patientSlideMatches
            || fileId === currentFileId
            || (this.isPatientLevelMode && fileId && !currentFileId)
        );
        const contextMatches = Boolean(
            gateId
            && cardPhase === requestedPhase
            && runIdMatches
            && fileMatches
            && (card || (this.isPatientLevelMode && fileId))
        );
        if (!contextMatches) {
            card?.setAttribute('data-confirm-state', 'expired');
            this._setPhaseConfirmationSubmitting(card, false);
            this.showNotification(
                (window.__lang || 'zh') === 'en'
                    ? 'This approval card is no longer current. Use the latest pending approval.'
                    : '此审批卡已过期，请使用最新的待审批卡。',
                'warning'
            );
            return null;
        }
        return {
            card,
            phase: requestedPhase,
            gateId,
            runId: this.isPatientLevelMode ? '' : runId,
            fileId,
        };
    }

    confirmPhase(phase, action, modifiedData = null, messageElement = null, onSuccess = null) {
        console.log(`📤 发送阶段确认: phase=${phase}, action=${action}`);
        const context = this._phaseApprovalContext(phase, messageElement);
        if (!context) {
            return Promise.resolve({ status: 'error', error: 'invalid_context' });
        }
        if (!this.socket?.connected) {
            this.showNotification(
                (window.__lang || 'zh') === 'en'
                    ? 'Approval could not be submitted. Check the connection and try again.'
                    : '审批尚未提交，请检查连接后重试。',
                'error'
            );
            return Promise.resolve({ status: 'error', error: 'disconnected' });
        }
        const { card, gateId: phaseGateId, runId, fileId } = context;
        const existing = this._pendingPhaseConfirmations.get(phaseGateId);
        if (existing?.promise) return existing.promise;
        this._setPhaseConfirmationSubmitting(card, true);
        let resolvePromise;
        const promise = new Promise((resolve) => {
            resolvePromise = resolve;
        });
        const timeoutId = window.setTimeout(() => {
            const pending = this._pendingPhaseConfirmations.get(phaseGateId);
            if (!pending) return;
            this._pendingPhaseConfirmations.delete(phaseGateId);
            this._setPhaseConfirmationSubmitting(pending.card, false);
            pending.card?.setAttribute('data-confirm-state', 'timeout');
            pending.resolve?.({ status: 'timeout', phase_gate_id: phaseGateId });
            this.showNotification(
                (window.__lang || 'zh') === 'en'
                    ? 'The server did not acknowledge this approval. Please try again.'
                    : '服务器未确认收到本次审批，请重试。',
                'warning'
            );
        }, 10000);
        this._pendingPhaseConfirmations.set(phaseGateId, {
            card,
            phase: Number(phase),
            action,
            runId,
            fileId,
            timeoutId,
            onSuccess,
            resolve: resolvePromise,
            promise,
        });
        this.socket.emit('agent.phase_confirm', {
            session_id: this.sessionId,
            runtime_run_id: runId,
            file_id: fileId,
            phase: phase,
            action: action,
            modified_data: modifiedData,
            phase_gate_id: phaseGateId
        });
        return promise;
    }

    _setPhaseConfirmationSubmitting(msgElement, submitting) {
        if (!msgElement) return;
        msgElement.dataset.confirmState = submitting ? 'submitting' : 'idle';
        msgElement.querySelectorAll(
            '.phase-confirm-buttons button, .phase5-review-actions button, '
            + '.edit-confirm-buttons button, .report-confirm-buttons button, '
            + '.draft-report-actions button'
        )
            .forEach((button) => {
                button.disabled = Boolean(submitting);
                button.style.opacity = submitting ? '0.55' : '';
            });
    }

    _resolvePhaseApprovalCard(msgElement, action = 'execute') {
        if (!msgElement) return;
        const isEn = (window.__lang || 'zh') === 'en';
        const modified = action === 'modify';
        msgElement.classList.add('is-resolved');
        msgElement.dataset.confirmState = modified ? 'modified' : 'confirmed';
        msgElement.querySelector('.phase-submission-status')?.remove();
        const actions = msgElement.querySelector('.phase-confirm-buttons, .phase5-review-actions');
        if (actions) {
            actions.innerHTML = `<span class="phase-confirmed-status" tabindex="-1">
                <i class="fas ${modified ? 'fa-pen' : 'fa-check-circle'}" aria-hidden="true"></i>
                ${modified
                    ? (isEn ? 'Changes submitted' : '修改已提交')
                    : (isEn ? 'Approved' : '已批准')}
            </span>`;
            queueMicrotask(() => actions.querySelector('.phase-confirmed-status')?.focus?.({ preventScroll: true }));
        }
    }

    _releasePendingPhaseConfirmations(reason = 'cancelled') {
        this._pendingPhaseConfirmations.forEach((pending) => {
            window.clearTimeout(pending.timeoutId);
            this._setPhaseConfirmationSubmitting(pending.card, false);
            pending.card?.setAttribute('data-confirm-state', reason);
            pending.resolve?.({ status: 'error', error: reason });
        });
        this._pendingPhaseConfirmations.clear();
    }

    _setPhaseCardCollapsed(card, collapsed, force = false) {
        const body = card?.querySelector('.phase-card-body');
        const toggle = card?.querySelector('.phase-collapse-toggle');
        if (!body || !toggle || (!force && card.classList.contains('editing'))) return;
        card.classList.toggle('is-collapsed', collapsed);
        body.hidden = collapsed;
        toggle.setAttribute('aria-expanded', String(!collapsed));
        toggle.setAttribute('aria-label', (window.__lang || 'zh') === 'en'
            ? (collapsed ? 'Expand stage details' : 'Collapse stage details')
            : (collapsed ? '展开阶段详情' : '收起阶段详情'));
        toggle.querySelector('i')?.classList.toggle('fa-chevron-down', collapsed);
        toggle.querySelector('i')?.classList.toggle('fa-chevron-up', !collapsed);
    }
    
    /**
     * 更新阶段卡片状态
     */
    updatePhaseCardStatus(phase, status, gateId = '') {
        // ⭐ 修复：Patient Level 模式下需要限定在当前WSI范围内查找
        let cards = [];
        if (gateId) {
            const escapedGateId = typeof CSS !== 'undefined' && CSS.escape
                ? CSS.escape(String(gateId))
                : String(gateId).replace(/["\\]/g, '\\$&');
            cards = Array.from(document.querySelectorAll(
                `.phase-result-card[data-phase="${phase}"][data-gate-id="${escapedGateId}"]`
            ));
        }
        if (!cards.length) {
            const fallbackCard = this.getPhaseCardForCurrentWsi(phase);
            if (fallbackCard) cards = [fallbackCard];
        }
        if (!cards.length) return;

        cards.forEach((card) => {
            const statusEl = card.querySelector('.phase-status');
            if (statusEl) {
                statusEl.className = `phase-status ${status}`;
                if (status === 'confirmed') {
                    const t = typeof __t !== 'undefined' ? __t('status_confirmed') : '已确认';
                    statusEl.textContent = t;
                    statusEl.innerHTML = '<i class="fas fa-check"></i> ' + t;
                } else if (status === 'modified') {
                    const t = typeof __t !== 'undefined' ? __t('status_modified') : '已修改';
                    statusEl.textContent = t;
                    statusEl.innerHTML = '<i class="fas fa-edit"></i> ' + t;
                }
            }
        });
    }
    
    /**
     * 禁用确认按钮
     */
    disablePhaseButtons(msgElement) {
        const buttons = msgElement.querySelectorAll('.phase-confirm-buttons button');
        buttons.forEach(btn => {
            btn.disabled = true;
            btn.style.opacity = '0.5';
        });
    }
    
    /**
     * ⭐ Phase 5 审核确认：用户确认最终诊断报告
     */
    confirmFinalReport(confirmMsg) {
        console.log('✅ 用户确认最终诊断报告');
        return this.confirmPhase(5, 'execute', {
            intervention_events: this._reviewForCard(confirmMsg)?.events
                || this._draftReportReview?.events
                || [],
        }, confirmMsg, () => {
            confirmMsg?.classList.add('is-resolved');
            this.hideDraftReportModal({ confirmMsg });
            this._renderFinalReportGenerating(confirmMsg);
        });
    }

    _renderFinalReportGenerating(confirmMsg) {
        if (!confirmMsg) return;
        confirmMsg.classList.add('is-resolved');
        // 更新消息显示为"正在生成报告"状态（等待 report_ready 事件）
        const messageText = confirmMsg.querySelector('.message-text');
        if (messageText) {
            messageText.innerHTML = `
                <p><strong>🎉 ${__t('phase_analysis_complete')}</strong></p>
                <p>${__t('phase_report_done')}</p>
                <p style="color: #fbbf24; font-weight: 600;">⏳ ${__t('generating_html_report')}</p>
            `;
        }
        
        // 更新按钮区域 - 先显示加载状态，等 report_ready 事件后才启用
        const actionsDiv = confirmMsg.querySelector('.phase5-review-actions');
        if (actionsDiv) {
            const _e = (window.__lang||'zh')==='en';
            actionsDiv.innerHTML = `
                <button class="btn-view-report-final" disabled style="opacity: 0.5;">
                    <i class="fas fa-spinner fa-spin"></i> ${_e ? 'Generating report...' : '正在生成报告...'}
                </button>
                <button class="btn-export-report-final" disabled style="opacity: 0.5;">
                    <i class="fas fa-download"></i> ${_e ? 'Export Report' : '导出报告'}
                </button>
            `;
            // 标记 actionsDiv，以便 report_ready 事件能找到它
            actionsDiv.setAttribute('data-waiting-report', 'true');
        }
        
        // 更新左侧面板状态
        this.updatePhaseCardStatus(5, 'confirmed');
        
        this.showNotification((window.__lang||'zh')==='en' 
            ? '⏳ Report confirmed, generating HTML report...' 
            : '⏳ 报告已确认，正在生成 HTML 报告...', 'info');
    }
    
    /**
     * ⭐ Phase 5 修改模式：显示两个选项 - 直接编辑 或 引用到对话框
     */
    enterPhase5EditMode() {
        console.log('✏️ 进入 Phase 5 报告修改模式');
        
        // 找到左侧面板的 Phase 5 卡片
        // ⭐ 修复：Patient Level 模式下需要限定在当前WSI范围内查找
        const card = this.getPhaseCardForCurrentWsi(5);
        if (!card) {
            this.showNotification((window.__lang||'zh')==='en' ? 'Report card not found' : '未找到诊断报告卡片', 'warning');
            return;
        }
        
        // 找到报告内容区域（在 .report-content 里）
        const reportContent = card.querySelector('.report-content');
        if (!reportContent) {
            this.showNotification((window.__lang||'zh')==='en' ? 'Report content not found' : '未找到报告内容', 'warning');
            return;
        }
        
        // 检查是否已经在编辑模式
        if (card.classList.contains('editing')) {
            this.showNotification((window.__lang||'zh')==='en' ? 'Already in edit mode' : '已在编辑模式', 'info');
            return;
        }
        
        // 报告卡即使被用户折叠，进入编辑时也必须显式展开。
        this._setPhaseCardCollapsed(card, false, true);

        // 进入编辑状态
        card.classList.add('editing');
        
        // 保存原始 HTML 内容用于取消
        card.dataset.originalHtml = reportContent.innerHTML;
        card.dataset.originalText = reportContent.innerText || reportContent.textContent;
        
        // 在报告内容下方添加编辑选项
        const existingBtns = card.querySelector('.phase5-edit-options');
        if (!existingBtns) {
            const editOptions = document.createElement('div');
            editOptions.className = 'phase5-edit-options';
            const _e = (window.__lang||'zh')==='en';
            editOptions.innerHTML = `
                <div class="edit-options-header">
                    <i class="fas fa-edit"></i> ${_e ? 'Choose Edit Method' : '选择修改方式'}
                </div>
                <div class="edit-options-buttons">
                    <button class="btn-direct-edit" title="${_e ? 'Edit report text directly' : '直接在文本框中编辑报告内容'}">
                        <i class="fas fa-pen"></i> ${_e ? 'Edit Directly' : '直接编辑文本'}
                    </button>
                    <button class="btn-quote-to-chat" title="${_e ? 'Quote to chat for AI-assisted editing' : '将报告引用到聊天框，让AI帮您修改'}">
                        <i class="fas fa-comment-medical"></i> ${_e ? 'Quote to Chat (AI Assist)' : '引用到对话（AI协助）'}
                    </button>
                    <button class="btn-cancel-edit-mode">
                        <i class="fas fa-times"></i> ${__t('btn_cancel')}
                    </button>
                </div>
            `;
            
            // 插入到报告内容后面
            reportContent.parentNode.insertBefore(editOptions, reportContent.nextSibling);
            
            // 绑定事件
            editOptions.querySelector('.btn-direct-edit').addEventListener('click', () => {
                this.enterPhase5DirectEdit(card, reportContent);
            });
            editOptions.querySelector('.btn-quote-to-chat').addEventListener('click', () => {
                this.quotePhase5ToChat(card, reportContent);
            });
            editOptions.querySelector('.btn-cancel-edit-mode').addEventListener('click', () => {
                this.cancelPhase5EditMode(card);
            });
        }
        
        this.showNotification((window.__lang||'zh')==='en' 
            ? 'Choose: edit directly or quote to chat for AI assistance'
            : '请选择修改方式：直接编辑 或 引用到对话框让AI协助修改', 'info');
    }
    
    /**
     * ⭐ Phase 5 直接编辑文本
     */
    enterPhase5DirectEdit(card, reportContent) {
        console.log('✏️ Phase 5 直接编辑模式');
        
        // 移除选项按钮
        const editOptions = card.querySelector('.phase5-edit-options');
        if (editOptions) editOptions.remove();
        
        // 获取纯文本内容
        const originalText = card.dataset.originalText || reportContent.innerText;
        
        // 替换为可编辑的文本框
        reportContent.innerHTML = `
            <textarea class="phase5-report-editor">${originalText}</textarea>
        `;
        
        // 添加保存/取消按钮
        const editBtns = document.createElement('div');
        editBtns.className = 'edit-confirm-buttons';
        const _e = (window.__lang||'zh')==='en';
        editBtns.innerHTML = `
            <button class="btn-save-edit" data-phase="5">
                <i class="fas fa-check"></i> ${_e ? 'Save' : '保存修改'}
            </button>
            <button class="btn-cancel-edit" data-phase="5">
                <i class="fas fa-times"></i> ${__t('btn_cancel')}
            </button>
        `;
        reportContent.parentNode.insertBefore(editBtns, reportContent.nextSibling);
        
        // 绑定事件
        editBtns.querySelector('.btn-save-edit').addEventListener('click', () => {
            this.savePhase5Edit(card, reportContent);
        });
        editBtns.querySelector('.btn-cancel-edit').addEventListener('click', () => {
            this.cancelPhase5Edit(card, reportContent);
        });
        
        // 聚焦到文本框
        const editor = reportContent.querySelector('.phase5-report-editor');
        if (editor) {
            editor.focus();
        }
    }
    
    /**
     * ⭐ Phase 5 引用到对话框（AI协助修改）
     */
    quotePhase5ToChat(card, reportContent) {
        console.log('💬 Phase 5 引用到对话框');
        
        // 移除选项按钮
        const editOptions = card.querySelector('.phase5-edit-options');
        if (editOptions) editOptions.remove();
        
        // 退出编辑模式
        card.classList.remove('editing');
        
        // 获取报告内容预览
        const fullContent = card.dataset.originalText || reportContent.innerText;
        const previewContent = fullContent.substring(0, 200) + (fullContent.length > 200 ? '...' : '');
        
        // 显示引用卡片
        this.showPhase5QuoteCard(previewContent, fullContent);
    }
    
    /**
     * ⭐ 显示 Phase 5 报告引用卡片
     */
    showPhase5QuoteCard(previewContent, fullContent) {
        // 存储当前引用信息
        this.currentQuote = {
            type: 'phase5_report',
            roiId: 'Phase5-Report',
            phase: 5,
            content: fullContent
        };
        
        // 创建或更新引用卡片
        let quoteCard = document.querySelector('.analysis-quote-card');
        if (!quoteCard) {
            quoteCard = document.createElement('div');
            quoteCard.className = 'analysis-quote-card';
            
            // 插入到聊天输入框上方
            const chatInput = document.querySelector('.chat-input-container') || 
                              document.querySelector('.chat-input') ||
                              document.getElementById('chatInput')?.parentElement;
            if (chatInput) {
                chatInput.parentElement.insertBefore(quoteCard, chatInput);
            }
        }
        
        quoteCard.innerHTML = `
            <div class="quote-card-header">
                <span class="quote-icon"><i class="fas fa-quote-left"></i></span>
                <span class="quote-title">${(window.__lang||'zh')==='en' ? 'Quote: Diagnostic Report (Phase 5)' : '引用: 诊断报告 (Phase 5)'}</span>
                <button class="quote-close-btn" title="${__t('btn_cancel')}">
                    <i class="fas fa-times"></i>
                </button>
            </div>
            <div class="quote-card-content">
                <div class="quote-preview">${previewContent}</div>
            </div>
            <div class="quote-card-hint">
                <i class="fas fa-lightbulb"></i> ${(window.__lang||'zh')==='en' 
                    ? 'Type your revision suggestions below, e.g. "Revise the conclusion using the documented morphology" or "Add immunohistochemistry recommendations"' 
                    : '请在下方输入修改建议，例如："请依据已记录的形态学证据修订结论" 或 "请补充免疫组化建议"'}
            </div>
        `;
        
        quoteCard.style.display = 'block';
        
        // 绑定关闭按钮事件
        quoteCard.querySelector('.quote-close-btn').addEventListener('click', () => {
            this.removeAnalysisQuoteCard();
        });
        
        // 聚焦到输入框
        const inputBox = document.getElementById('chatInput') || document.querySelector('.chat-input textarea');
        if (inputBox) {
            inputBox.focus();
            inputBox.placeholder = (window.__lang||'zh')==='en' 
                ? 'Enter your suggestions for the report...' 
                : '请输入对报告的修改建议...';
        }
        
        this.showNotification((window.__lang||'zh')==='en' 
            ? '💬 Report quoted. Describe your edits in the input box.' 
            : '💬 已引用诊断报告，请在输入框中描述您的修改需求', 'info');
    }
    
    /**
     * ⭐ 取消 Phase 5 编辑模式（未进入具体编辑时）
     */
    cancelPhase5EditMode(card) {
        // 移除选项按钮
        const editOptions = card.querySelector('.phase5-edit-options');
        if (editOptions) editOptions.remove();
        
        card.classList.remove('editing');
        this.showNotification(__t('cancelled'), 'info');
    }
    
    /**
     * ⭐ 保存 Phase 5 报告编辑
     */
    savePhase5Edit(card, reportContent) {
        const editor = reportContent.querySelector('.phase5-report-editor');
        if (!editor) return;
        
        const newContent = editor.value;
        
        return this.confirmPhase(
            5,
            'modify',
            { edited_report: newContent },
            card,
            () => {
                // Commit the edited report only after the exact gate ACK.
                reportContent.innerHTML = this.formatMarkdown(newContent);
                card.querySelector('.edit-confirm-buttons')?.remove();
                card.classList.remove('editing');
                this.updatePhaseCardStatus(5, 'modified');
                this.showNotification(
                    (window.__lang||'zh')==='en' ? '✅ Report changes saved' : '✅ 报告修改已保存',
                    'success'
                );
            },
        );
    }
    
    /**
     * ⭐ 取消 Phase 5 报告编辑
     */
    cancelPhase5Edit(card, reportContent) {
        const originalHtml = card.dataset.originalHtml || '';
        
        // 恢复原始内容
        reportContent.innerHTML = originalHtml;
        
        // 移除编辑按钮
        const editBtns = card.querySelector('.edit-confirm-buttons');
        if (editBtns) editBtns.remove();
        
        card.classList.remove('editing');
        
        this.showNotification(__t('cancel_adjust'), 'info');
    }
    
    /**
     * 进入阶段修改模式
     */
    enterPhaseEditMode(phase) {
        console.log(`✏️ 进入 Phase ${phase} 编辑模式`);
        
        // 保存当前阶段用于后续操作
        this.currentEditPhase = phase;
        
        // 1. 左侧面板区域列表变为可编辑
        // ⭐ 修复：Patient Level 模式下需要限定在当前WSI范围内查找
        const card = this.getPhaseCardForCurrentWsi(phase);
        if (card) {
            this._setPhaseCardCollapsed(card, false, true);
            card.classList.add('editing');
            // ⭐ 绿框阶段的节点被搬进树后，本卡片是 display:none 的锚点，
            // 但可编辑区域清单只在这张卡里。编辑期间由 .editing CSS 规则让它
            // 现身；这里同步移除 aria-hidden 并滚动过去，让用户能找到清单。
            if (card.classList.contains('collab-hierarchy-anchor')) {
                card.removeAttribute('aria-hidden');
            }
            requestAnimationFrame(() => {
                card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            });

            // Only the phase currently being edited may change selection.
            // Older phase cards remain visible as evidence, but allowing their
            // checkboxes to toggle here creates a visual-only "modification"
            // that is never part of the current phase payload.
            card.querySelectorAll('.region-checkbox').forEach(checkbox => {
                checkbox.disabled = false;
            });
            
            // ⭐⭐⭐ 为每个 checkbox 添加 change 监听器，实时同步 WSI overlays ⭐⭐⭐
            card.querySelectorAll('.region-checkbox').forEach(checkbox => {
                // 移除旧的监听器（如果有）
                checkbox.removeEventListener('change', this.handleCheckboxChange);
                // 添加新的监听器
                checkbox.addEventListener('change', (e) => this.handleCheckboxChange(e, phase));
            });
            
            // 显示编辑确认按钮
            const existingBtns = card.querySelector('.edit-confirm-buttons');
            if (!existingBtns) {
                const _isEn = (window.__lang || 'zh') === 'en';
                const editBtns = document.createElement('div');
                editBtns.className = 'edit-confirm-buttons';
                editBtns.innerHTML = `
                    <button class="btn-save-edit" data-phase="${phase}">
                        <i class="fas fa-check"></i> ${_isEn ? 'Confirm Changes' : '确认修改'}
                    </button>
                    <button class="btn-cancel-edit" data-phase="${phase}">
                        <i class="fas fa-times"></i> ${_isEn ? 'Cancel' : '取消'}
                    </button>
                `;
                card.appendChild(editBtns);
                
                // 绑定事件
                editBtns.querySelector('.btn-save-edit').addEventListener('click', () => {
                    this.savePhaseEdit(phase);
                });
                editBtns.querySelector('.btn-cancel-edit').addEventListener('click', () => {
                    this.cancelPhaseEdit(phase);
                });
            }
        }
        
        // 2. 地图进入编辑模式（支持点击添加候选框）
        this.enableMapEditMode(phase);
        
        // 3. 提示用户
        this.showNotification((window.__lang||'zh')==='en' 
            ? '✏️ Edit mode: check/uncheck regions or click map to add' 
            : '✏️ 已进入编辑模式，可勾选/取消勾选区域，或点击地图添加候选框', 'info');
    }
    
    /**
     * 处理 checkbox 变化事件，实时同步 WSI overlays
     */
    handleCheckboxChange(event, phase) {
        const regionItem = event.target.closest('.region-item');
        const roiId = regionItem?.dataset?.roiId || '';
        const isChecked = event.target.checked;
        console.log(`[Phase ${phase}] checkbox 状态变化: ${roiId} → ${isChecked ? '选中' : '取消'}`);
        
        // ⭐ 修复：取消勾选时，同时移除左侧面板的黄色高亮
        if (!isChecked && regionItem) {
            regionItem.classList.remove('roi-selected-glow');
            console.log(`[Phase ${phase}] ✅ 已移除区域 ${roiId} 的黄色高亮`);
        }
        
        this.updateOverlaysFromCheckboxes(phase);
    }
    
    /**
     * 根据当前勾选状态更新 WSI overlays
     * ⭐ 修复：先清除所有框，再重绘选中的框（而不是增量添加）
     */
    updateOverlaysFromCheckboxes(phase) {
        // ⭐⭐⭐ 收集所有阶段（1 到 phase）的选中框，而不是只收集当前阶段 ⭐⭐⭐
        const allSelectedOverlays = [];
        for (let p = 1; p <= phase; p++) {
            const phaseOverlays = this.getSelectedOverlaysFromCard(p);
            allSelectedOverlays.push(...phaseOverlays);
        }
        
        console.log(`[Phase ${phase}] 更新 overlays: 共 ${allSelectedOverlays.length} 个选中（所有阶段累积）`);
        
        // ⭐ 先清除所有现有的 overlays
        if (window.pathchat && typeof window.pathchat.clearAllOverlays === 'function') {
            window.pathchat.clearAllOverlays();
            console.log(`[Phase ${phase}] 已清除所有 overlays`);
        } else if (window.pathchat && window.pathchat.roiOverlays) {
            // 手动清除
            window.pathchat.roiOverlays.forEach(overlay => {
                if (window.pathchat.viewer) {
                    window.pathchat.viewer.removeOverlay(overlay.element);
                }
            });
            window.pathchat.roiOverlays = [];
            console.log(`[Phase ${phase}] 手动清除所有 overlays`);
        }
        
        // ⭐ 重新绘制所有阶段选中的框
        if (allSelectedOverlays.length > 0) {
            if (window.pathchat && typeof window.pathchat.drawColoredROI === 'function') {
                allSelectedOverlays.forEach(overlay => {
                    window.pathchat.drawColoredROI(overlay);
                });
                console.log(`[Phase ${phase}] 重绘 ${allSelectedOverlays.length} 个累积的 overlays`);
            } else if (this.viewer) {
                this.fallbackDrawOverlays(allSelectedOverlays);
            }
        }
    }
    
    /**
     * ⭐ 辅助函数：获取当前WSI对应的Phase卡片
     * Patient Level 模式下需要限定在当前查看的WSI范围内查找
     */
    getPhaseCardForCurrentWsi(phase) {
        // ⭐ Patient Level 模式：限定在当前查看的WSI范围内查找
        if (this.isPatientLevelMode) {
            // 优先使用当前查看的WSI索引，其次使用正在分析的WSI索引
            const wsiIndex = this.currentViewingWsiIndex !== undefined ? this.currentViewingWsiIndex : 
                             (this.currentAnalyzingWsiIndex !== undefined ? this.currentAnalyzingWsiIndex : 0);
            const wsiBody = document.querySelector(`.pl-wsi-body[data-index="${wsiIndex}"]`);
            if (wsiBody) {
                const card = wsiBody.querySelector(`.phase-result-card[data-phase="${phase}"]`);
                if (card) return card;
            }
            // 如果在当前WSI范围内找不到，尝试全局查找（兜底）
            console.log(`[getPhaseCardForCurrentWsi] WSI#${wsiIndex} 未找到 Phase ${phase} 卡片，尝试全局查找`);
        }
        // 单切片模式 或 兜底：直接全局查找
        return document.querySelector(`.phase-result-card[data-phase="${phase}"]`);
    }
    
    /**
     * 从左侧卡片获取当前选中的 overlays
     */
    getSelectedOverlaysFromCard(phase) {
        const card = this.getPhaseCardForCurrentWsi(phase);
        if (!card) return [];
        
        const selectedOverlays = [];
        card.querySelectorAll('.region-item').forEach(item => {
            const checkbox = item.querySelector('.region-checkbox');
            if (checkbox && checkbox.checked) {
                // 从 data 属性获取完整的 overlay 信息
                const rect = JSON.parse(item.dataset.rect || '{}');
                const roiId = item.dataset.roiId || '';
                
                // 根据阶段确定颜色和倍率
                let color = '#FF0000';
                let magnification = 10;
                if (phase === 2) {
                    color = '#3B82F6';  // 蓝色
                    magnification = 40;
                } else if (phase === 3 || phase === 4) {
                    color = '#10B981';  // 绿色
                    magnification = 160;
                }
                
                selectedOverlays.push({
                    id: roiId,
                    rect: rect,
                    color: color,
                    magnification: magnification,
                    label: '',  // ⭐ 不显示标签，保持界面干净
                    level: phase === 1 ? 'red' : (phase === 2 ? 'blue' : 'green')
                });
            }
        });
        
        return selectedOverlays;
    }
    
    /**
     * 保存阶段编辑 - ⭐ 优化：只暂存修改，不立即提交
     * 用户可以多次修改，最后点击"执行"时才提交
     */
    savePhaseEdit(phase) {
        console.log(`💾 暂存 Phase ${phase} 编辑`);
        
        // ⭐ 修复：Patient Level 模式下需要限定在当前WSI范围内查找
        const card = this.getPhaseCardForCurrentWsi(phase);
        if (!card) return;
        
        // 收集选中的区域
        const selectedRegions = [];
        card.querySelectorAll('.region-item').forEach(item => {
            const checkbox = item.querySelector('.region-checkbox');
            if (checkbox && checkbox.checked) {
                const regionData = {
                    roi_id: item.dataset.roiId,
                    rect: JSON.parse(item.dataset.rect || '{}'),
                    is_user_added: item.classList.contains('user-added'),
                    parent_id: item.dataset.parentId || ''
                };
                // ⭐⭐⭐ 详细日志：记录收集到的区域数据 ⭐⭐⭐
                console.log(`[savePhaseEdit] 收集区域: roi_id=${regionData.roi_id}, rect=${JSON.stringify(regionData.rect)}, parent_id=${regionData.parent_id}, is_user_added=${regionData.is_user_added}`);
                selectedRegions.push(regionData);
            }
        });
        
        // 分析修改内容 - ⭐ 使用原始 AI 选择进行比较
        const originalRegions = this.currentPhaseData?.originalSelection || [];
        const originalIds = new Set(originalRegions.map(r => r.id || r.roi_id));
        const selectedIds = new Set(selectedRegions.map(r => r.roi_id));
        
        const addedCount = [...selectedIds].filter(id => !originalIds.has(id)).length;
        const removedCount = [...originalIds].filter(id => !selectedIds.has(id)).length;
        
        console.log(`[Phase ${phase}] 修改检测: 原始=${originalIds.size}个, 当前=${selectedIds.size}个, 新增=${addedCount}, 删除=${removedCount}`);
        
        // ⭐⭐⭐ 暂存修改到 currentPhaseData，但不提交 ⭐⭐⭐
        this.currentPhaseData.selected_regions = selectedRegions;
        this.currentPhaseData.pendingChanges = {
            addedCount,
            removedCount,
            hasChanges: addedCount > 0 || removedCount > 0
        };
        
        // 退出编辑模式
        card.classList.remove('editing');
        card.querySelectorAll('.region-checkbox').forEach(checkbox => {
            checkbox.disabled = true;
        });
        const editBtns = card.querySelector('.edit-confirm-buttons');
        if (editBtns) editBtns.remove();
        
        this.disableMapEditMode();
        
        // 更新 overlays 显示
        this.updateOverlaysFromCheckboxes(phase);
        const heading = card.querySelector('.phase-regions h4');
        if (heading) {
            heading.innerHTML = `<i class="fas fa-map-marker-alt"></i> ${__t('selected_regions')} (${selectedRegions.length})`;
        }
        
        // 显示提示
        if (addedCount > 0 || removedCount > 0) {
            const msg = (window.__lang||'zh')==='en'
                ? `✅ Changes staged (added ${addedCount}, removed ${removedCount}). Continue editing or click "Execute" to submit.`
                : `✅ 修改已暂存（新增 ${addedCount}，删除 ${removedCount}）。可继续修改或点击"执行"提交`;
            this.showNotification(msg, 'success');
        } else {
            this.showNotification((window.__lang||'zh')==='en' ? 'No changes detected, exited edit mode' : '没有检测到修改，已退出编辑模式', 'info');
        }
    }
    
    /**
     * ⭐ 检查是否有待提交的修改
     */
    hasPendingChanges() {
        return this.currentPhaseData?.pendingChanges?.hasChanges || false;
    }
    
    /**
     * ⭐ 提交所有暂存的修改（在点击"执行"时调用）
     */
    submitPendingChanges(phase, messageElement = null) {
        if (!this.hasPendingChanges()) {
            console.log(`[Phase ${phase}] 没有待提交的修改`);
            return Promise.resolve({ submitted: false });
        }
        
        const changes = this.currentPhaseData.pendingChanges;
        const selectedRegions = this.currentPhaseData.selected_regions;
        
        const isEn = (window.__lang || 'zh') === 'en';
        const changesDesc = [];
        if (changes.addedCount > 0) changesDesc.push(isEn ? `Added ${changes.addedCount} region(s)` : `新增 ${changes.addedCount} 个区域`);
        if (changes.removedCount > 0) changesDesc.push(isEn ? `Removed ${changes.removedCount} region(s)` : `删除 ${changes.removedCount} 个区域`);
        
        return new Promise((resolve) => {
            this.showModificationReasonDialog(phase, {
                selectedRegions,
                addedCount: changes.addedCount,
                removedCount: changes.removedCount,
                changesDesc: changesDesc.join(isEn ? ', ' : '，'),
                messageElement,
                onSubmit: () => resolve({ submitted: true }),
                onCancel: () => resolve({ submitted: false, cancelled: true }),
            });
        });
    }
    
    /**
     * 显示修改理由输入对话框
     */
    showModificationReasonDialog(phase, changes) {
        // 移除已有的对话框
        const existingDialog = document.querySelector('.modification-reason-dialog');
        if (existingDialog) existingDialog.remove();
        
        const isEn = (window.__lang || 'zh') === 'en';
        const dialog = document.createElement('div');
        dialog.className = 'modification-reason-dialog';
        dialog.innerHTML = `
            <div class="dialog-overlay"></div>
            <div class="dialog-content">
                <div class="dialog-header">
                    <h3>${isEn ? 'Explain Your Changes' : '请说明修改理由'}</h3>
                    <button class="dialog-close">&times;</button>
                </div>
                <div class="dialog-body">
                    <p class="changes-summary">
                        <strong>${isEn ? 'Changes:' : '修改内容：'}</strong>${changes.changesDesc}
                    </p>
                    <textarea class="reason-input" placeholder="${isEn ? 'Enter your reason for the modification, e.g.: I believe this region has suspicious cellular morphology that requires closer attention...' : '请输入您修改的理由，例如：我认为这个区域有可疑的细胞形态需要重点关注...'}" rows="4"></textarea>
                </div>
                <div class="dialog-footer">
                    <button class="btn-cancel">${isEn ? 'Cancel' : '取消'}</button>
                    <button class="btn-confirm">${isEn ? 'Submit Changes' : '提交修改'}</button>
                </div>
            </div>
        `;
        
        document.body.appendChild(dialog);
        
        // 聚焦到输入框
        const textarea = dialog.querySelector('.reason-input');
        textarea.focus();
        
        // 绑定事件
        const closeDialog = (cancelled = false) => {
            dialog.remove();
            // ⭐ 如果是取消操作，调用 onCancel 回调
            if (cancelled && changes.onCancel) {
                changes.onCancel();
            }
        };
        
        dialog.querySelector('.dialog-close').addEventListener('click', () => closeDialog(true));
        dialog.querySelector('.btn-cancel').addEventListener('click', () => closeDialog(true));
        dialog.querySelector('.dialog-overlay').addEventListener('click', () => closeDialog(true));
        
        let submitting = false;
        const submitModification = async () => {
            if (submitting) return;
            submitting = true;
            const confirmButton = dialog.querySelector('.btn-confirm');
            if (confirmButton) confirmButton.disabled = true;
            const reason = textarea.value.trim();
            const result = await this.submitPhaseModification(
                phase,
                changes.selectedRegions,
                reason,
                changes.messageElement || null,
            );
            if (result?.status === 'success') {
                closeDialog();
                changes.onSubmit?.();
                return;
            }
            submitting = false;
            if (confirmButton) confirmButton.disabled = false;
            textarea.focus();
        };
        dialog.querySelector('.btn-confirm').addEventListener('click', submitModification);
        
        // Enter 键提交
        textarea.addEventListener('keydown', async (e) => {
            if (e.key === 'Enter' && e.ctrlKey) {
                e.preventDefault();
                await submitModification();
            }
        });
    }
    
    /**
     * 提交阶段修改
     * ⭐ 返回 Promise，等待后端确认后再 resolve
     */
    async submitPhaseModification(phase, selectedRegions, reason, messageElement = null) {
        console.log(`📤 提交 Phase ${phase} 修改，理由: ${reason || '未提供'}`);
        this.showNotification((window.__lang||'zh')==='en' ? '⏳ Submitting changes, waiting for AI evaluation...' : '⏳ 正在提交修改，等待 AI 评估...', 'info');
        const result = await this.confirmPhase(
            phase,
            'modify',
            { regions: selectedRegions, reason },
            messageElement,
        );
        if (result?.status !== 'success') return result;

        // Only a gate-scoped success acknowledgement may commit the editing UI.
        const card = this.getPhaseCardForCurrentWsi(phase);
        if (card) {
            card.classList.remove('editing');
            card.querySelector('.edit-confirm-buttons')?.remove();
        }
        this.disableMapEditMode();
        this.currentEditPhase = null;
        return result;
    }
    
    /**
     * 取消阶段编辑
     */
    cancelPhaseEdit(phase) {
        console.log(`❌ 取消 Phase ${phase} 编辑`);
        
        // ⭐ 修复：Patient Level 模式下需要限定在当前WSI范围内查找
        const card = this.getPhaseCardForCurrentWsi(phase);
        if (card) {
            card.classList.remove('editing');
            const editBtns = card.querySelector('.edit-confirm-buttons');
            if (editBtns) editBtns.remove();
            
            // 恢复所有复选框（恢复到原始选中状态）
            card.querySelectorAll('.region-checkbox').forEach(cb => {
                // 恢复原始选中状态
                cb.checked = cb.dataset.originalChecked !== 'false';
                cb.disabled = true;
            });
            
            // ⭐ 恢复原始 overlays
            this.updateOverlaysFromCheckboxes(phase);
        }
        
        this.disableMapEditMode();
        this.currentEditPhase = null;
        this.showNotification(__t('cancel_adjust'), 'info');
    }
    
    // ═══════════════════════════════════════════════════════════════════════════
    // ⭐⭐⭐ 协作模式：分析结果文本编辑功能 ⭐⭐⭐
    // ═══════════════════════════════════════════════════════════════════════════
    
    /**
     * 进入分析结果编辑模式 - ✏️ 直接编辑分析文本（原版功能）
     * @param {string} roiId - ROI ID
     * @param {number} phase - 阶段编号
     * @param {HTMLElement} targetNode - ⭐ 可选：直接传入的树节点元素
     */
    enterAnalysisEditMode(roiId, phase, targetNode = null) {
        // ⭐ 只在协作模式下允许编辑
        if (this.currentMode !== 'collaborative') {
            this.showNotification((window.__lang||'zh')==='en' ? 'Editing only available in collaborative mode' : '分析结果编辑仅在协作模式下可用', 'warning');
            return;
        }
        
        console.log(`✏️ 进入分析结果编辑模式: ${roiId} (Phase ${phase})`);
        
        // ⭐ 优先使用传入的节点，否则用 querySelector 查找
        const treeNode = targetNode || document.querySelector(`.collab-tree-node[data-roi-id="${roiId}"]`);
        if (!treeNode) {
            console.warn(`未找到 roiId=${roiId} 的树节点`);
            return;
        }

        // Auto 风格默认收起完成节点；用户明确进入编辑时再展开当前节点。
        const treeHeader = treeNode.querySelector('.route-tree-header');
        const treeToggle = treeHeader?.querySelector('.route-tree-toggle');
        treeHeader?.classList.remove('collapsed');
        treeToggle?.setAttribute('aria-expanded', 'true');
        
        // 找到内容区域
        const contentDiv = treeNode.querySelector('.route-tree-content');
        if (!contentDiv) {
            console.warn(`未找到 roiId=${roiId} 的内容区域`);
            return;
        }
        
        // 如果已经在编辑模式，跳过
        if (contentDiv.classList.contains('editing')) {
            return;
        }
        
        // 获取当前内容和原始内容
        const currentContent = contentDiv.textContent || contentDiv.innerText || '';
        const originalContent = treeNode.dataset.originalAnalysis || currentContent;
        
        // 标记为编辑模式
        contentDiv.classList.add('editing');
        
        // 保存原始 HTML 以便取消时恢复
        contentDiv.dataset.previousHtml = contentDiv.innerHTML;
        
        // 替换为编辑 UI
        const isEn = (window.__lang || 'zh') === 'en';
        contentDiv.innerHTML = `
            <div class="analysis-edit-container" role="region" aria-label="${isEn ? `Edit ${roiId} analysis` : `编辑 ${roiId} 分析`}">
                <div class="analysis-edit-heading">
                    <span class="analysis-edit-kicker">${isEn ? 'Human correction' : '人工修订'}</span>
                    <strong>${roiId}</strong>
                </div>
                <textarea class="analysis-edit-textarea" rows="7" aria-label="${isEn ? `Analysis text for ${roiId}` : `${roiId} 分析文本`}">${currentContent}</textarea>
                <div class="analysis-edit-actions">
                    <button class="btn-save-analysis" data-roi-id="${roiId}" data-phase="${phase}" type="button">
                        <i class="fas fa-check" aria-hidden="true"></i> ${isEn ? 'Save revision' : '保存修订'}
                    </button>
                    <button class="btn-cancel-analysis" data-roi-id="${roiId}" type="button">
                        <i class="fas fa-times" aria-hidden="true"></i> ${isEn ? 'Cancel' : '取消'}
                    </button>
                    ${treeNode.dataset.isEdited === 'true' ? `
                        <button class="btn-restore-analysis" data-roi-id="${roiId}" type="button" title="${isEn ? 'Restore the original AI analysis' : '恢复 AI 原始分析'}">
                            <i class="fas fa-undo" aria-hidden="true"></i> ${isEn ? 'Restore original' : '恢复原始'}
                        </button>
                    ` : ''}
                </div>
                <div class="analysis-original-hint">
                    <i class="fas fa-info-circle" aria-hidden="true"></i>
                    ${isEn ? 'The original AI analysis is preserved for audit and restoration.' : 'AI 原始分析会保留，用于审计和恢复。'}
                </div>
            </div>
        `;
        
        // 聚焦到 textarea
        const textarea = contentDiv.querySelector('.analysis-edit-textarea');
        if (textarea) {
            textarea.focus();
            textarea.setSelectionRange(textarea.value.length, textarea.value.length);
        }
        
        // 绑定按钮事件
        contentDiv.querySelector('.btn-save-analysis')?.addEventListener('click', (e) => {
            e.stopPropagation();
            this.saveAnalysisEdit(roiId, phase);
        });
        
        contentDiv.querySelector('.btn-cancel-analysis')?.addEventListener('click', (e) => {
            e.stopPropagation();
            this.cancelAnalysisEdit(roiId);
        });
        
        contentDiv.querySelector('.btn-restore-analysis')?.addEventListener('click', (e) => {
            e.stopPropagation();
            this.restoreOriginalAnalysis(roiId, phase);
        });
    }
    
    /**
     * 引用分析结果到聊天框 - 💬 让 AI 重新解读（新版功能）
     * @param {string} roiId - ROI ID
     * @param {number} phase - 阶段编号
     * @param {HTMLElement} targetNode - ⭐ 可选：直接传入的树节点元素
     */
    _escapeRoiQuoteSelector(roiId) {
        const raw = String(roiId || '');
        if (window.CSS && typeof window.CSS.escape === 'function') return CSS.escape(raw);
        return raw.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    }

    _lookupRoiAnalysisQuote(roiId, targetNode = null) {
        const id = String(roiId || '').trim();
        const node = targetNode || (() => {
            if (!id) return null;
            const escaped = this._escapeRoiQuoteSelector(id);
            return document.querySelector(
                `.collab-tree-node[data-roi-id="${escaped}"], ` +
                `.route-tree-node[data-roi-id="${escaped}"], ` +
                `.route-tree-blue-node[data-roi-id="${escaped}"], ` +
                `.route-tree-green-item[data-roi-id="${escaped}"]`
            );
        })();
        const contentDiv = node?.querySelector(
            '.route-tree-content, .route-tree-blue-content, .route-tree-green-content'
        );
        let text = String(
            contentDiv?.textContent || contentDiv?.innerText || node?.dataset?.originalAnalysis || ''
        ).trim();
        if (!text && id) {
            const analysis = window.pathchat?.roiAnalysisData?.[id];
            text = String(
                analysis?.gpt_analysis || analysis?.observation || analysis?.analysis || analysis?.reasoning || ''
            ).trim();
        }
        if (!text && id) {
            const escaped = this._escapeRoiQuoteSelector(id);
            const activity = document.querySelector(`.v2-agent-activity-item[data-roi-id="${escaped}"] .v2-agent-activity-content`);
            text = String(activity?.textContent || '').trim();
        }
        if (!text && id) {
            const finding = (this._draftReportReview?.findings || []).find(
                (item) => String(item.roi_id || '') === id
            );
            text = String(finding?.summary || finding?.findings || finding?.observation || '').trim();
        }
        return { node, text };
    }

    quoteAnalysisToChat(roiId, phase, targetNode = null) {
        console.log(`💬 引用分析结果到聊天: ${roiId} (Phase ${phase})`);
        const lookedUp = this._lookupRoiAnalysisQuote(roiId, targetNode);
        const treeNode = lookedUp.node;
        const currentContent = lookedUp.text
            || ((window.__lang || 'zh') === 'en'
                ? `ROI ${roiId} is cited.`
                : `已引用 ${roiId}。`);
        const originalContent = treeNode?.dataset?.originalAnalysis || lookedUp.text || currentContent;
        const previewSource = this._normalizeQuotedRoiText(currentContent);
        const fullSource = this._normalizeQuotedRoiText(originalContent);
        const previewContent = previewSource.length > 100
            ? previewSource.substring(0, 100) + '...'
            : previewSource;
        this.showAnalysisQuoteCard(roiId, phase, previewContent, fullSource, treeNode);
        treeNode?.classList.add('quote-active');
        this.showNotification((window.__lang||'zh')==='en'
            ? `Quoted ${roiId}. It now appears above the chat box.`
            : `已引用 ${roiId}，输入框上方会显示「已引用此处」。`, 'info');
    }
    
    /**
     * 显示分析结果引用卡片（在右侧聊天输入框上方）
     * @param {HTMLElement} treeNode - ⭐ 可选：树节点元素引用（用于后续更新）
     */
    showAnalysisQuoteCard(roiId, phase, previewContent, fullContent, treeNode = null) {
        // 移除已有的引用卡片
        this.removeAnalysisQuoteCard();
        
        // 找到输入框容器
        const inputContainer = document.querySelector('.chat-input-container') || 
                               document.querySelector('#chatInput')?.parentElement;
        if (!inputContainer) {
            console.warn('未找到聊天输入框容器');
            return;
        }
        
        // 创建引用卡片
        const quoteCard = document.createElement('div');
        quoteCard.className = 'analysis-quote-card';
        quoteCard.dataset.roiId = roiId;
        quoteCard.dataset.phase = phase;
        quoteCard.dataset.fullContent = fullContent;
        const quoteTitle = (window.__lang||'zh')==='en'
            ? `Quoted here · ${this._escapeHtmlContent(roiId)}`
            : `已引用此处 · ${this._escapeHtmlContent(roiId)}`;
        
        quoteCard.innerHTML = `
            <div class="quote-header">
                <span class="quote-icon"><i class="fas fa-quote-left"></i></span>
                <span class="quote-title">${quoteTitle}</span>
                <button class="quote-close" title="${__t('btn_cancel')}">
                    <i class="fas fa-times"></i>
                </button>
            </div>
            <div class="quote-content">${previewContent}</div>
            <div class="quote-hint">
                <i class="fas fa-lightbulb"></i> 
                ${(window.__lang||'zh')==='en'
                    ? 'This region is cited. Type below, then send or regenerate the draft.'
                    : '已引用该区域。请在下方输入，发送可继续沟通，或点「重新生成报告」。'}
            </div>
        `;
        
        const inputArea = inputContainer.querySelector('.input-area');
        if (inputArea) {
            inputContainer.insertBefore(quoteCard, inputArea);
        } else {
            inputContainer.insertBefore(quoteCard, inputContainer.firstChild);
        }
        
        // 绑定关闭按钮事件
        quoteCard.querySelector('.quote-close').addEventListener('click', () => {
            this.removeAnalysisQuoteCard();
        });
        
        // 保存当前引用状态
        this.currentQuote = {
            roiId: roiId,
            phase: phase,
            content: fullContent
        };
        
        // 聚焦到输入框
        const chatInput = document.getElementById('chatInput');
        if (chatInput) {
            chatInput.focus();
            chatInput.placeholder = (window.__lang||'zh')==='en' 
                ? `Enter revision suggestions for ${roiId} analysis...` 
                : `输入对 ${roiId} 分析的修改建议...`;
        }
    }
    
    /**
     * 移除分析结果引用卡片
     */
    removeAnalysisQuoteCard() {
        const existingCard = document.querySelector('.analysis-quote-card');
        if (existingCard) {
            const roiId = existingCard.dataset.roiId;
            existingCard.remove();
            
            // 移除左侧高亮
            const treeNode = document.querySelector(`.collab-tree-node[data-roi-id="${roiId}"]`);
            if (treeNode) {
                treeNode.classList.remove('quote-active');
            }
        }
        
        // 清除引用状态
        this.currentQuote = null;
        this._syncDraftReportDock();
    }
    
    /**
     * 发送带引用的消息（让 AI 重新解读）
     */
    sendQuotedMessage(userMessage) {
        if (!this.currentQuote) {
            console.warn('没有当前引用');
            return false;
        }
        
        const { type, roiId, phase, content } = this.currentQuote;
        
        console.log(`📤 发送引用消息: type=${type}, ${roiId}, 用户指令: ${userMessage}`);
        
        // 在聊天中显示用户消息（带引用样式）
        this.addQuotedUserMessage(roiId, content, userMessage);
        
        // ⭐ Phase 5 报告修改：使用专门的事件
        if (type === 'phase5_report') {
            this.socket.emit('agent.revise_report', {
                session_id: this.sessionId,
                phase: 5,
                original_report: content,
                user_instruction: userMessage,
                timestamp: new Date().toISOString()
            });
            
            // 显示加载状态
            this.showNotification((window.__lang||'zh')==='en' ? '🔄 AI is revising the report per your request...' : '🔄 AI 正在根据您的要求修改报告...', 'info');
            
            // 移除引用卡片
            this.removeAnalysisQuoteCard();
            
            return true;
        }
        
        // ⭐ 普通 ROI 重新解读
        this.socket.emit('agent.reanalyze_roi', {
            session_id: this.sessionId,
            roi_id: roiId,
            phase: phase,
            original_analysis: content,
            user_instruction: userMessage,
            // file_id 供后端在运行状态已清理时（患者级已完成切片）
            // 从本地 WSI + analysis_tree 重建 ROI 图像做视觉重读。
            file_id: window.pathchat?.currentFile?.file_id || this.currentFileId || '',
            timestamp: new Date().toISOString()
        });
        
        // 显示加载状态
        this.showNotification((window.__lang||'zh')==='en' ? `🔄 AI re-interpreting ${roiId}...` : `🔄 AI 正在重新解读 ${roiId}...`, 'info');
        
        // 移除引用卡片
        this.removeAnalysisQuoteCard();
        
        return true;
    }
    
    /**
     * 在聊天中添加带引用的用户消息
     */
    addQuotedUserMessage(roiId, quotedContent, userMessage) {
        const chatMessages = document.getElementById('chatMessages');
        if (!chatMessages) return;
        
        const previewContent = quotedContent.length > 80 
            ? quotedContent.substring(0, 80) + '...' 
            : quotedContent;
        
        const msgDiv = document.createElement('div');
        msgDiv.className = 'chat-message user quoted-message';
        msgDiv.innerHTML = `
            <div class="message-content">
                <div class="quoted-reference">
                    <div class="quoted-ref-header">
                        <i class="fas fa-quote-left"></i> ${(window.__lang||'zh')==='en' ? `Quote: ${roiId} analysis` : `引用 ${roiId} 的分析`}
                    </div>
                    <div class="quoted-ref-content">${previewContent}</div>
                </div>
                <div class="message-text">${userMessage}</div>
            </div>
            <div class="message-avatar">
                <i class="fas fa-user"></i>
            </div>
        `;
        
        chatMessages.appendChild(msgDiv);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }
    
    /**
     * 处理 AI 重新解读的结果
     */
    handleReanalysisResult(data) {
        console.log('📥 收到 AI 重新解读结果:', data);
        
        const { roi_id, phase, new_analysis, success, error } = data;
        
        if (!success) {
            this.showNotification((window.__lang||'zh')==='en' ? `❌ Re-interpretation failed: ${error || 'Unknown'}` : `❌ 重新解读失败: ${error || '未知错误'}`, 'error');
            return;
        }
        
        // 更新左侧面板的分析结果
        this.updateLeftPanelAnalysis(roi_id, new_analysis);
        
        // 在聊天中显示 AI 的新分析
        this.addReanalysisResultMessage(roi_id, new_analysis);
        
        this.showNotification((window.__lang||'zh')==='en' ? `✅ ${roi_id} re-interpreted` : `✅ ${roi_id} 已重新解读`, 'success');
    }
    
    /**
     * 更新左侧面板的分析结果
     */
    updateLeftPanelAnalysis(roiId, newAnalysis) {
        const treeNode = document.querySelector(`.collab-tree-node[data-roi-id="${roiId}"]`);
        if (!treeNode) {
            console.warn(`未找到 ${roiId} 的树节点`);
            return;
        }
        
        const contentDiv = treeNode.querySelector('.route-tree-content');
        if (contentDiv) {
            contentDiv.innerHTML = newAnalysis;
        }
        
        // 标记为已编辑
        treeNode.dataset.isEdited = 'true';
        treeNode.classList.add('user-edited');
        
        // 添加"AI已更新"标识
        const header = treeNode.querySelector('.route-tree-header');
        if (header && !header.querySelector('.edited-badge')) {
            const badge = document.createElement('span');
            badge.className = 'edited-badge ai-updated';
            badge.innerHTML = `<i class="fas fa-robot"></i> ${(window.__lang||'zh')==='en' ? 'AI Updated' : 'AI已更新'}`;
            badge.title = '根据您的建议重新解读';
            header.appendChild(badge);
        }
        
        // 高亮效果
        treeNode.classList.add('just-updated');
        setTimeout(() => {
            treeNode.classList.remove('just-updated');
        }, 2000);
    }
    
    /**
     * 在聊天中添加 AI 重新解读的结果
     */
    addReanalysisResultMessage(roiId, newAnalysis) {
        const chatMessages = document.getElementById('chatMessages');
        if (!chatMessages) return;
        
        const msgDiv = document.createElement('div');
        msgDiv.className = 'chat-message assistant reanalysis-result';
        msgDiv.innerHTML = `
            <div class="message-avatar">
                <i class="fas fa-robot"></i>
            </div>
            <div class="message-content">
                <div class="reanalysis-header">
                    <i class="fas fa-sync-alt"></i> ${roiId} 重新解读完成
                </div>
                <div class="message-text">${newAnalysis}</div>
                <div class="reanalysis-note">
                    <i class="fas fa-check-circle"></i> 左侧面板已同步更新
                </div>
            </div>
        `;
        
        chatMessages.appendChild(msgDiv);
        chatMessages.scrollTop = chatMessages.scrollHeight;

        // ⭐ 必须同步写入聊天历史：这张卡片若只进 DOM，后续追问
        // （如「检索下文献回答我」）随请求发送的 history 里就没有这段解读，
        // 证据 Agent 只能按整体病例作答，检索词也会跑偏。
        const plainText = msgDiv.querySelector('.message-text')?.textContent?.trim() || '';
        if (plainText && Array.isArray(window.pathchat?.chatHistory)) {
            window.pathchat.chatHistory.push({
                role: 'assistant',
                content: `${roiId} 重新解读完成：\n${plainText}`,
                timestamp: new Date(),
            });
        }
    }
    
    /**
     * ⭐ 处理 AI 修改诊断报告的结果
     */
    handleReportRevisionResult(data) {
        console.log('📥 收到 AI 报告修改结果:', data);
        
        const { success, error, revised_report, phase } = data;
        const pendingChat = this._pendingRevisionChatMessage;
        this._pendingRevisionChatMessage = null;
        const roiIds = Array.isArray(data?.roi_ids)
            ? data.roi_ids.map((id) => String(id || '').trim()).filter(Boolean)
            : [];
        const finishPendingChat = (text) => {
            if (!pendingChat || !window.pathchat?.updateMessage) return;
            window.pathchat.updateMessage(pendingChat, text);
        };
        
        if (!success) {
            this._setDraftReportBusy(false);
            this.showNotification((window.__lang||'zh')==='en' ? `❌ Report modification failed: ${error || 'Unknown'}` : `❌ 报告修改失败: ${error || '未知错误'}`, 'error');
            finishPendingChat(
                (window.__lang || 'zh') === 'en'
                    ? `Could not update the draft: ${error || 'Unknown error'}`
                    : `未能更新拟报告：${error || '未知错误'}`
            );
            return;
        }
        this._setDraftReportBusy(false);
        if (this._draftReportReview) {
            this._draftReportReview.current = revised_report;
            this._draftReportReview.editing = false;
        }
        if (this.currentPhaseData) this.currentPhaseData.report = revised_report;
        const chatEditor = this._currentDraftEditor();
        if (chatEditor) chatEditor.value = revised_report;
        const editor = document.getElementById('draftReportEditor');
        if (editor) editor.value = revised_report;
        const feedback = this._currentDraftFeedback() || document.getElementById('draftReportFeedback');
        if (feedback) feedback.value = '';
        if (this._draftReportReview?.confirmMsg) {
            this._renderDraftReportCard(this._draftReportReview.confirmMsg);
        }
        
        // 更新左侧面板的报告内容
        // ⭐ 修复：Patient Level 模式下需要限定在当前WSI范围内查找
        const card = this.getPhaseCardForCurrentWsi(5);
        if (card) {
            const reportContent = card.querySelector('.report-content');
            if (reportContent) {
                reportContent.innerHTML = this.formatMarkdown(revised_report);
                
                // 添加"AI已修改"标识
                const header = card.querySelector('.phase-header');
                if (header && !header.querySelector('.ai-revised-badge')) {
                    const badge = document.createElement('span');
                    badge.className = 'ai-revised-badge';
                    badge.innerHTML = `<i class="fas fa-robot"></i> ${(window.__lang||'zh')==='en' ? 'AI Modified' : 'AI已修改'}`;
                    badge.title = '根据您的建议修改';
                    badge.style.cssText = 'margin-left: 10px; color: #22c55e; font-size: 12px;';
                    header.appendChild(badge);
                }
                
                // 高亮效果
                card.classList.add('just-updated');
                setTimeout(() => {
                    card.classList.remove('just-updated');
                }, 2000);
            }
            
            // 更新状态
            this.updatePhaseCardStatus(5, 'modified');
        }
        
        this.showNotification((window.__lang||'zh')==='en' 
            ? '✅ Diagnostic report updated per your suggestions' 
            : '✅ 诊断报告已根据您的建议修改', 'success');
        const idText = roiIds.join((window.__lang || 'zh') === 'en' ? ', ' : '、');
        finishPendingChat(
            (window.__lang || 'zh') === 'en'
                ? (idText
                    ? `The draft has been regenerated with new ROI ${idText} and the conversation so far.`
                    : 'The draft has been regenerated from the conversation so far.')
                : (idText
                    ? `已根据对话反馈和新增 ROI ${idText} 重新生成拟报告。`
                    : '已根据对话反馈重新生成拟报告。')
        );
    }
    
    /**
     * ⭐ 为修改后的报告消息添加确认/继续修改按钮
     * @param {HTMLElement} messageElement - 消息元素
     * @param {string} revisedReport - 修改后的报告内容
     */
    addReportConfirmationButtons(messageElement, revisedReport) {
        // 创建按钮容器
        const btnContainer = document.createElement('div');
        btnContainer.className = 'report-confirm-buttons';
        btnContainer.style.cssText = 'margin-top: 15px; display: flex; gap: 10px; flex-wrap: wrap;';
        
        // 确认按钮
        const confirmBtn = document.createElement('button');
        confirmBtn.className = 'result-action-btn confirm-btn';
        confirmBtn.innerHTML = `<i class="fas fa-check"></i> ${__t('btn_confirm_report')}`;
        confirmBtn.style.cssText = 'background: #007AFF; color: white; border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; font-size: 13px; display: flex; align-items: center; gap: 6px;';  /* Apple Blue */
        messageElement.dataset.phase = '5';
        messageElement.dataset.gateId = String(this.currentPhaseData?.phase_gate_id || '');
        messageElement.dataset.runId = String(this.runtimeRunId || '');
        messageElement.dataset.fileId = String(this.getCurrentFileId() || '');
        confirmBtn.onclick = () => {
            this.confirmPhase(5, 'modify', { edited_report: revisedReport }, messageElement, () => {
                confirmBtn.disabled = true;
                confirmBtn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${(window.__lang||'zh')==='en' ? 'Confirming...' : '确认中...'}`;
                confirmBtn.style.opacity = '0.6';
                this.hideDraftReportModal();
                this.showNotification(
                    (window.__lang||'zh')==='en'
                        ? '✅ Report confirmed, generating final HTML...'
                        : '✅ 已确认报告，正在生成最终HTML报告...',
                    'success'
                );
            });
        };
        
        // 继续修改按钮
        const modifyBtn = document.createElement('button');
        modifyBtn.className = 'result-action-btn modify-btn';
        modifyBtn.innerHTML = `<i class="fas fa-edit"></i> ${__t('btn_modify_report')}`;
        modifyBtn.style.cssText = 'background: linear-gradient(135deg, #f59e0b, #d97706); color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; display: flex; align-items: center; gap: 6px;';
        modifyBtn.onclick = () => {
            // 滚动到输入框并聚焦
            const chatInput = document.getElementById('chatInput') || document.querySelector('.chat-input');
            if (chatInput) {
                chatInput.focus();
                chatInput.placeholder = (window.__lang||'zh')==='en' ? 'Enter your revision suggestions...' : '请输入您的修改建议...';
                chatInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
            this.showNotification((window.__lang||'zh')==='en' ? '💡 Enter your modification suggestions in the input box' : '💡 请在输入框中输入您的修改建议', 'info');
        };
        
        // 导出报告按钮
        const exportBtn = document.createElement('button');
        exportBtn.className = 'result-action-btn export-btn';
        exportBtn.innerHTML = `<i class="fas fa-download"></i> ${(window.__lang||'zh')==='en' ? 'Export Report' : '导出报告'}`;
        exportBtn.style.cssText = 'background: linear-gradient(135deg, #6366f1, #4f46e5); color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; display: flex; align-items: center; gap: 6px;';
        exportBtn.onclick = () => {
            // 导出 Markdown 报告
            const blob = new Blob([revisedReport], { type: 'text/markdown' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `pathology_report_${new Date().toISOString().slice(0, 10)}.md`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            this.showNotification((window.__lang||'zh')==='en' ? '✅ Report exported as Markdown' : '✅ 报告已导出为 Markdown 文件', 'success');
        };
        
        btnContainer.appendChild(confirmBtn);
        btnContainer.appendChild(modifyBtn);
        btnContainer.appendChild(exportBtn);
        
        // 添加到消息元素
        const contentDiv = messageElement.querySelector('.message-content') || messageElement;
        contentDiv.appendChild(btnContainer);
    }
    
    /**
     * 保存分析结果编辑
     */
    async saveAnalysisEdit(roiId, phase) {
        console.log(`💾 保存分析结果编辑: ${roiId}`);
        
        const treeNode = document.querySelector(`.collab-tree-node[data-roi-id="${roiId}"]`);
        const contentDiv = treeNode?.querySelector('.route-tree-content');
        const textarea = contentDiv?.querySelector('.analysis-edit-textarea');
        
        if (!textarea) {
            console.warn('未找到编辑的 textarea');
            return;
        }
        
        const newContent = textarea.value.trim();
        const originalContent = treeNode.dataset.originalAnalysis || contentDiv.dataset.originalAnalysis || '';
        
        if (!newContent) {
            this.showNotification((window.__lang||'zh')==='en' ? 'Analysis content cannot be empty' : '分析内容不能为空', 'warning');
            return;
        }
        
        // 检查是否有修改
        const isModified = newContent !== originalContent;

        const saveButton = contentDiv.querySelector('.btn-save-analysis');
        const previousSaveHtml = saveButton?.innerHTML || '';
        if (saveButton) {
            saveButton.disabled = true;
            saveButton.setAttribute('aria-busy', 'true');
            saveButton.innerHTML = `<i class="fas fa-spinner fa-spin" aria-hidden="true"></i> ${(window.__lang||'zh')==='en' ? 'Saving…' : '正在保存…'}`;
        }

        try {
            await this.submitAnalysisEdit(roiId, phase, newContent, originalContent);
        } catch (error) {
            if (saveButton) {
                saveButton.disabled = false;
                saveButton.removeAttribute('aria-busy');
                saveButton.innerHTML = previousSaveHtml;
            }
            this.showNotification(
                (window.__lang||'zh')==='en'
                    ? `Save failed. Your draft is still here: ${error?.message || 'please retry'}`
                    : `保存失败，草稿仍保留：${error?.message || '请重试'}`,
                'error'
            );
            textarea.focus();
            return;
        }
        
        // 退出编辑模式，显示新内容
        contentDiv.classList.remove('editing');
        contentDiv.innerHTML = newContent;
        
        // 标记为已编辑
        if (isModified) {
            treeNode.dataset.isEdited = 'true';
            treeNode.classList.add('user-edited');
            
            // 在标题后添加"已编辑"标识
            const header = treeNode.querySelector('.route-tree-header');
            if (header && !header.querySelector('.edited-badge')) {
                const badge = document.createElement('span');
                badge.className = 'edited-badge';
                badge.innerHTML = `<i class="fas fa-pen"></i> ${(window.__lang||'zh')==='en' ? 'Edited' : '已编辑'}`;
                badge.title = '点击编辑按钮可修改或恢复原始分析';
                header.appendChild(badge);
            }
        }
        
        // 保存到本地存储（用于后续提交到后端）
        if (!this.userAnalysisEdits) {
            this.userAnalysisEdits = {};
        }
        this.userAnalysisEdits[roiId] = {
            phase: phase,
            original: originalContent,
            modified: newContent,
            timestamp: new Date().toISOString()
        };
        
        this.showNotification((window.__lang||'zh')==='en' ? `✅ Analysis saved (${roiId})` : `✅ 分析结果已保存 (${roiId})`, 'success');
    }
    
    /**
     * 取消分析结果编辑
     */
    cancelAnalysisEdit(roiId) {
        console.log(`❌ 取消分析结果编辑: ${roiId}`);
        
        const treeNode = document.querySelector(`.collab-tree-node[data-roi-id="${roiId}"]`);
        const contentDiv = treeNode?.querySelector('.route-tree-content');
        
        if (!contentDiv) return;
        
        // 恢复之前的 HTML
        const previousHtml = contentDiv.dataset.previousHtml;
        if (previousHtml) {
            contentDiv.innerHTML = previousHtml;
        }
        
        contentDiv.classList.remove('editing');
        delete contentDiv.dataset.previousHtml;
        
        this.showNotification(__t('cancel_adjust'), 'info');
    }
    
    /**
     * 恢复原始 AI 分析
     */
    async restoreOriginalAnalysis(roiId, phase) {
        console.log(`🔄 恢复原始分析: ${roiId}`);
        
        const treeNode = document.querySelector(`.collab-tree-node[data-roi-id="${roiId}"]`);
        const contentDiv = treeNode?.querySelector('.route-tree-content');
        
        if (!treeNode || !contentDiv) return;
        
        const originalContent = treeNode.dataset.originalAnalysis || contentDiv.dataset.originalAnalysis;
        if (!originalContent) {
            this.showNotification((window.__lang||'zh')==='en' ? 'Original analysis content not found' : '未找到原始分析内容', 'warning');
            return;
        }
        
        try {
            await this.submitAnalysisEdit(roiId, phase, originalContent, originalContent, true);
        } catch (error) {
            this.showNotification(
                (window.__lang||'zh')==='en'
                    ? `Restore failed: ${error?.message || 'please retry'}`
                    : `恢复失败：${error?.message || '请重试'}`,
                'error'
            );
            return;
        }

        // 退出编辑模式，显示原始内容 only after server ACK
        contentDiv.classList.remove('editing');
        contentDiv.innerHTML = originalContent;
        
        // 移除已编辑标记
        treeNode.dataset.isEdited = 'false';
        treeNode.classList.remove('user-edited');
        
        // 移除"已编辑"标识
        const editedBadge = treeNode.querySelector('.edited-badge');
        if (editedBadge) {
            editedBadge.remove();
        }
        
        // 从本地存储中删除
        if (this.userAnalysisEdits && this.userAnalysisEdits[roiId]) {
            delete this.userAnalysisEdits[roiId];
        }
        
        this.showNotification((window.__lang||'zh')==='en' ? `✅ Original AI analysis restored (${roiId})` : `✅ 已恢复原始AI分析 (${roiId})`, 'success');
    }
    
    /**
     * 提交分析编辑到后端
     */
    submitAnalysisEdit(roiId, phase, newContent, originalContent, isRestore = false) {
        console.log(`📤 提交分析编辑到后端: ${roiId}, isRestore=${isRestore}`);
        if (!this.socket?.connected) {
            return Promise.reject(new Error(
                (window.__lang||'zh')==='en' ? 'Connection unavailable' : '连接不可用'
            ));
        }
        const editId = globalThis.crypto?.randomUUID?.()
            || `edit_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`;
        return new Promise((resolve, reject) => {
            const timer = window.setTimeout(() => {
                const pending = this._pendingAnalysisEdits.get(editId);
                if (!pending) return;
                this._pendingAnalysisEdits.delete(editId);
                pending.reject(new Error(
                    (window.__lang||'zh')==='en' ? 'Server acknowledgement timed out' : '服务器确认超时'
                ));
            }, 15000);
            this._pendingAnalysisEdits.set(editId, { resolve, reject, timer });
            this.socket.emit('agent.analysis_edit', {
                session_id: this.sessionId,
                runtime_run_id: this.runtimeRunId || '',
                file_id: String(
                    this.currentPhaseData?.file_id
                    || this.currentPhaseData?.wsi_context?.file_id
                    || this.getCurrentFileId()
                    || ''
                ),
                edit_id: editId,
                phase: phase,
                roi_id: roiId,
                new_content: newContent,
                original_content: originalContent,
                is_restore: isRestore,
                timestamp: new Date().toISOString()
            });
        });
    }

    handleAnalysisEditReceived(data = {}) {
        const editId = String(data.edit_id || '');
        const pending = editId ? this._pendingAnalysisEdits.get(editId) : null;
        if (!pending) return;
        if (
            data.runtime_run_id
            && this.runtimeRunId
            && String(data.runtime_run_id) !== String(this.runtimeRunId)
        ) return;
        window.clearTimeout(pending.timer);
        this._pendingAnalysisEdits.delete(editId);
        if (data.status === 'success') pending.resolve(data);
        else pending.reject(new Error(data.error || 'analysis_edit_rejected'));
    }

    _releasePendingAnalysisEdits(reason = 'cancelled') {
        for (const [editId, pending] of this._pendingAnalysisEdits.entries()) {
            window.clearTimeout(pending.timer);
            pending.reject(new Error(reason));
            this._pendingAnalysisEdits.delete(editId);
        }
    }
    
    /**
     * 获取所有用户编辑的分析结果（用于最终报告）
     */
    getUserAnalysisEdits() {
        return this.userAnalysisEdits || {};
    }
    
    /**
     * 启用地图编辑模式（支持点击添加候选框）
     */
    enableMapEditMode(phase) {
        if (!this.viewer) {
            console.log('[Map] viewer 未就绪，跳过编辑模式');
            return;
        }
        
        console.log(`[Map] 启用编辑模式 (Phase ${phase})`);
        this.mapEditMode = true;
        this.mapEditPhase = phase;
        
        // 添加点击事件监听器
        this.mapClickHandler = (event) => this.handleMapClickForAddROI(event, phase);
        this.viewer.addHandler('canvas-click', this.mapClickHandler);
    }
    
    /**
     * 禁用地图编辑模式
     */
    disableMapEditMode() {
        console.log('[Map] 禁用编辑模式');
        this.mapEditMode = false;
        this.mapEditPhase = null;
        
        // 移除点击事件监听器
        if (this.viewer && this.mapClickHandler) {
            this.viewer.removeHandler('canvas-click', this.mapClickHandler);
            this.mapClickHandler = null;
        }
        
        // 恢复只显示选中的 overlays
        if (this.currentEditPhase) {
            this.updateOverlaysFromCheckboxes(this.currentEditPhase);
        }
    }
    
    /**
     * 显示所有候选框（包括未选中的，用灰色/虚线显示）
     */
    showAllCandidateOverlays(phase) {
        // 获取所有候选框数据
        const allCandidates = this.currentPhaseData?.allCandidates || [];
        const selectedRegions = this.currentPhaseData?.selected_regions || [];
        
        if (allCandidates.length === 0) {
            console.log(`[Map] Phase ${phase} 无候选框数据，跳过显示候选框`);
            return;
        }
        
        console.log(`[Map] Phase ${phase} 显示所有候选框: ${allCandidates.length} 个候选, ${selectedRegions.length} 个已选`);
        
        // 获取已选中的 ID 集合（支持多种 ID 格式匹配）
        const selectedIds = new Set(selectedRegions.map(r => r.id || r.roi_id));
        
        // ⭐⭐⭐ 根据阶段设置颜色和倍率 ⭐⭐⭐
        const phaseConfig = {
            1: { selectedColor: '#FF0000', unselectedColor: '#808080', magnification: 10, level: 'red' },    // 红框
            2: { selectedColor: '#3B82F6', unselectedColor: '#9CA3AF', magnification: 40, level: 'blue' },   // 蓝框
            3: { selectedColor: '#10B981', unselectedColor: '#9CA3AF', magnification: 160, level: 'green' }  // 绿框
        };
        const config = phaseConfig[phase] || phaseConfig[1];
        
        // ⭐ 先收集所有已显示的非当前阶段的 overlays（保留之前阶段的框）
        const previousOverlays = [];
        for (let p = 1; p < phase; p++) {
            const prevPhaseOverlays = this.getSelectedOverlaysFromCard(p);
            previousOverlays.push(...prevPhaseOverlays);
        }
        
        // 构建当前阶段的 overlays（选中的实色，未选中的灰色）
        const currentPhaseOverlays = allCandidates.map(roi => {
            const roiId = roi.id || roi.roi_id;
            const isSelected = selectedIds.has(roiId) || 
                              selectedIds.has(roi.grid_id) ||
                              selectedIds.has(`R${roi.grid_id}`) ||  // Phase 1 格式
                              Array.from(selectedIds).some(id => id && id.includes && id.includes(`-B${roi.grid_id}`));  // Phase 2 格式
            return {
                id: roiId || `candidate_${roi.candidate_index || roi.grid_id}`,
                rect: roi.rect,
                color: isSelected ? config.selectedColor : config.unselectedColor,
                magnification: config.magnification,
                label: '',  // ⭐ 不显示标签，保持界面干净
                level: config.level,
                is_candidate: !isSelected,  // 标记为未选中候选
                candidate_index: roi.candidate_index,
                grid_id: roi.grid_id,
                parent_id: roi.parent_id
            };
        });
        
        // 合并所有 overlays
        const allOverlays = [...previousOverlays, ...currentPhaseOverlays];
        
        console.log(`[Map] Phase ${phase} 总共显示 ${allOverlays.length} 个 overlays (之前阶段: ${previousOverlays.length}, 当前阶段候选: ${currentPhaseOverlays.length})`);
        
        // 显示所有 overlays
        if (window.pathchat && typeof window.pathchat.syncROIOverlays === 'function') {
            window.pathchat.syncROIOverlays(allOverlays);
        }
    }
    
    /**
     * 处理点击地图添加候选框
     * ⭐ 只能从预设的候选框中选择（红框从 AI 候选中选，蓝框从 16 宫格中选）
     */
    handleMapClickForAddROI(event, phase) {
        if (!this.mapEditMode || phase !== this.mapEditPhase) return;
        
        // ⭐⭐⭐ 关键优化：区分拖拽和真正的点击 ⭐⭐⭐
        // OpenSeadragon 的 quick 属性表示是否是快速点击（而不是拖拽后释放）
        if (event.quick === false) {
            console.log('[Map] 检测到拖拽操作，跳过添加框处理');
            return;  // 拖拽操作，不处理
        }
        
        // 获取点击位置（viewport 坐标）
        const viewportPoint = this.viewer.viewport.pointFromPixel(event.position);
        const imagePoint = this.viewer.viewport.viewportToImageCoordinates(viewportPoint);
        
        console.log(`[Map] 点击位置: viewport(${viewportPoint.x.toFixed(4)}, ${viewportPoint.y.toFixed(4)}), image(${imagePoint.x.toFixed(0)}, ${imagePoint.y.toFixed(0)}), quick=${event.quick}`);
        
        // 检查点击位置是否在某个"未选中的候选框"内
        const hitROI = this.findUnselectedCandidateAtPoint(imagePoint, phase);
        
        if (hitROI) {
            console.log(`[Map] 命中候选框: ${hitROI.id}`);
            this.addROIToSelection(hitROI, phase);
        } else {
            // ⭐ 点击不在任何候选框内 - 不显示警告，允许用户自由点击地图
            console.log('[Map] 未命中任何候选框');
        }
    }
    
    /**
     * 在指定位置查找未选中的候选框
     */
    findUnselectedCandidateAtPoint(imagePoint, phase) {
        const allCandidates = this.currentPhaseData?.allCandidates || [];
        const selectedRegions = this.currentPhaseData?.selected_regions || [];
        
        // 获取已选中的 ID 集合（支持多种 ID 格式）
        const selectedIds = new Set(selectedRegions.map(r => r.id || r.roi_id));
        
        console.log(`[Map] 查找候选框: ${allCandidates.length} 个候选, ${selectedIds.size} 个已选`);
        
        // 遍历未选中的候选框，检查点击位置是否在其中
        for (const roi of allCandidates) {
            const roiId = roi.id || roi.roi_id;
            // ⭐ 支持多种 ID 匹配格式
            const isSelected = selectedIds.has(roiId) || 
                              selectedIds.has(roi.grid_id) ||
                              selectedIds.has(`R${roi.grid_id}`) ||  // Phase 1 格式
                              Array.from(selectedIds).some(id => id && roiId && id === roiId);  // 精确匹配
            
            if (isSelected) continue;  // 跳过已选中的
            
            const rect = roi.rect;
            if (!rect) continue;
            
            if (imagePoint.x >= rect.x && imagePoint.x <= rect.x + rect.w &&
                imagePoint.y >= rect.y && imagePoint.y <= rect.y + rect.h) {
                console.log(`[Map] 命中未选中候选框: ${roiId}`);
                return roi;
            }
        }
        
        return null;
    }
    
    /**
     * 添加候选框到选中列表
     */
    addROIToSelection(roi, phase) {
        // ⭐⭐⭐ 修复：根据 phase 使用正确的 ID 前缀 ⭐⭐⭐
        const prefix = phase === 1 ? 'R' : (phase === 2 ? 'B' : 'G');
        const gridId = roi.grid_id || roi.candidate_index;
        
        // 优先使用 roi.id（手动创建的 ROI 已有正确的 ID），否则基于 gridId 生成
        let displayId = roi.id;
        if (!displayId || (!displayId.startsWith('R') && !displayId.startsWith('B') && !displayId.startsWith('G'))) {
            displayId = gridId ? `${prefix}${gridId}` : (roi.id || roi.roi_id || `${prefix}${Date.now()}`);
        }
        
        // ⭐⭐⭐ 详细日志：记录候选框的完整信息 ⭐⭐⭐
        console.log(`[Map] 添加候选框到选中: ${displayId} (原始ID: ${roi.id}, phase: ${phase})`);
        console.log(`[Map] 候选框详情: rect=${JSON.stringify(roi.rect)}, parent_id=${roi.parent_id}, zoom_level=${roi.zoom_level}`);
        
        // 1. 更新 currentPhaseData.selected_regions
        if (!this.currentPhaseData.selected_regions) {
            this.currentPhaseData.selected_regions = [];
        }
        
        // 创建带有正确 ID 的 ROI 副本
        const roiWithCorrectId = {
            ...roi,
            id: displayId,  // 使用正确的 prefix{number} 格式
            original_id: roi.id,  // 保留原始 ID
            user_added: roi.user_added || true,  // 标记为用户添加
            is_user_added: roi.is_user_added || true  // 双重标记
        };
        this.currentPhaseData.selected_regions.push(roiWithCorrectId);
        
        // 2. 在左侧卡片添加新的区域项
        // ⭐ 修复：Patient Level 模式下需要限定在当前WSI范围内查找
        const card = this.getPhaseCardForCurrentWsi(phase);
        if (card) {
            const regionsContainer = card.querySelector('.phase-regions');
            if (regionsContainer) {
                const regionItem = this.createRegionItem(roiWithCorrectId, true, true);  // 第三个参数表示是用户添加的
                regionsContainer.appendChild(regionItem);
                
                // 为新添加的 checkbox 添加监听器
                const checkbox = regionItem.querySelector('.region-checkbox');
                if (checkbox) {
                    checkbox.addEventListener('change', (e) => this.handleCheckboxChange(e, phase));
                }
            }
        }
        
        // 3. 更新 WSI overlays
        this.updateOverlaysFromCheckboxes(phase);
        
        this.showNotification((window.__lang||'zh')==='en' ? `✅ Region ${displayId} added` : `✅ 已添加区域 ${displayId}`, 'success');
    }
    
    /**
     * 创建区域项元素
     * @param {Object} roi - ROI 数据
     * @param {boolean} checked - 是否选中
     * @param {boolean} isUserAdded - 是否是用户添加的
     */
    createRegionItem(roi, checked = true, isUserAdded = false) {
        const item = document.createElement('div');
        item.className = 'region-item' + (isUserAdded ? ' user-added' : '');
        item.dataset.roiId = roi.id || roi.roi_id || '';
        item.dataset.rect = JSON.stringify(roi.rect || {});
        // ⭐⭐⭐ 关键修复：保存 parent_id，用于后续传递给后端 ⭐⭐⭐
        item.dataset.parentId = roi.parent_id || '';
        
        // 获取显示信息
        const displayId = roi.id || roi.roi_id || '未命名';
        const parentInfo = roi.parent_id ? `父区域${roi.parent_id}` : '';
        const infoText = isUserAdded ? `(User add)` : (roi.reason || (roi.tissue_coverage ? `组织占比${(roi.tissue_coverage * 100).toFixed(1)}%` : parentInfo));
        
        item.innerHTML = `
            <input type="checkbox" class="region-checkbox" ${checked ? 'checked' : ''} data-original-checked="${checked}">
            <button type="button" class="region-focus-button"
                    aria-label="${(window.__lang || 'zh') === 'en' ? 'Focus region' : '定位区域'} ${this._escapeHtmlContent(String(displayId))}">
                <span class="region-focus-icon" aria-hidden="true"><i class="fas fa-location-arrow"></i></span>
                <span class="region-id">${this._escapeHtmlContent(String(displayId))}</span>
                <span class="region-info ${isUserAdded ? 'user-add-tag' : ''}">${this._escapeHtmlContent(String(infoText))}</span>
                <i class="fas fa-chevron-right region-disclosure" aria-hidden="true"></i>
            </button>
        `;
        item.querySelector('.region-focus-button')?.addEventListener('click', () => {
            this.focusOnRegion(roi.rect || {}, displayId);
        });
        
        return item;
    }
    
    /**
     * 高亮阶段选中区域
     */
    highlightPhaseRegions(regions, overlays) {
        // 准备要显示的 overlays - 优先使用 metadata.overlays，否则从 regions 构建
        let targetOverlays = [];
        
        if (overlays && overlays.length > 0) {
            console.log('[Phase] 使用 metadata.overlays');
            targetOverlays = overlays;
        } else if (regions && regions.length > 0) {
            console.log('[Phase] 从 regions 构建 overlays');
            targetOverlays = regions.map(region => {
                // 根据 zoom_level 确定 magnification 和颜色
                let magnification = 10;
                let color = '#FF0000';
                if (region.zoom_level === 40) {
                    magnification = 40;
                    color = '#3B82F6';
                } else if (Number(region.zoom_level) >= 160) {
                    magnification = 160;
                    color = '#22C55E';
                }
                return {
                    id: region.id,
                    rect: region.rect,
                    color: color,
                    magnification: magnification,  // ⭐ 关键：用于 CSS 类选择
                    label: ''  // ⭐ 不显示标签，保持界面干净
                };
            });
        }
        
        if (targetOverlays.length === 0) {
            console.log('[Phase] 无 overlays 数据');
            return;
        }
        
        console.log('[Phase] 准备显示', targetOverlays.length, '个 overlays');
        
        // ⭐⭐⭐ 关键修复：先清除所有现有 overlays，避免删除的框重新出现 ⭐⭐⭐
        if (window.pathchat && typeof window.pathchat.clearAllOverlays === 'function') {
            window.pathchat.clearAllOverlays();
            console.log('[Phase] 已清除所有现有 overlays');
        } else if (window.pathchat && window.pathchat.roiOverlays) {
            // 手动清除
            window.pathchat.roiOverlays.forEach(overlay => {
                if (window.pathchat.viewer) {
                    window.pathchat.viewer.removeOverlay(overlay.element);
                }
            });
            window.pathchat.roiOverlays = [];
            console.log('[Phase] 手动清除所有 overlays');
        }
        
        // ⭐ 重新绘制目标 overlays
        const canDrawColored = window.pathchat && typeof window.pathchat.drawColoredROI === 'function';
        
        if (canDrawColored) {
            targetOverlays.forEach((overlay, index) => {
                try {
                    window.pathchat.drawColoredROI(overlay);
                } catch (error) {
                    console.error(`[Phase] 标注框 ${index + 1} 绘制失败:`, error);
                }
            });
            console.log('[Phase] ✅ drawColoredROI 完成');
        } else {
            console.warn('[Phase] 未找到标准绘制方法，使用 fallback');
            this.fallbackDrawOverlays(targetOverlays);
        }
    }
    
    /**
     * 备用方法：直接使用 OpenSeadragon 绘制 overlays
     */
    fallbackDrawOverlays(overlays) {
        if (!this.viewer) {
            console.warn('[Phase] fallback: viewer 不存在，延迟重试');
            setTimeout(() => this.fallbackDrawOverlays(overlays), 500);
            return;
        }
        
        const tiledImage = this.viewer.world.getItemAt(0);
        if (!tiledImage || !tiledImage.getFullyLoaded()) {
            console.warn('[Phase] fallback: 图像未加载完成，延迟重试');
            setTimeout(() => this.fallbackDrawOverlays(overlays), 500);
            return;
        }
        
        // 清除旧的
        try {
            this.viewer.clearOverlays();
        } catch (e) {}
        
        // 添加新的
        overlays.forEach(overlay => {
            this.addPhaseOverlay(overlay);
        });
        
        console.log(`[Phase] fallback: 已显示 ${overlays.length} 个 overlays`);
    }
    
    /**
     * 添加阶段 overlay（五阶段协作模式专用）
     */
    addPhaseOverlay(overlay) {
        try {
            if (!this.viewer) {
                console.warn('[Phase] addPhaseOverlay: viewer 不存在');
                return;
            }
            
            const rect = overlay.rect;
            if (!rect || typeof rect.x === 'undefined') {
                console.warn('[Phase] addPhaseOverlay: rect 无效', overlay);
                return;
            }
            
            const imageSize = this.viewer.world.getItemAt(0)?.getContentSize();
            
            if (!imageSize) {
                console.warn('[Phase] addPhaseOverlay: 无法获取图像尺寸');
                return;
            }
            
            // 转换为归一化坐标
            const x = rect.x / imageSize.x;
            const y = rect.y / imageSize.y;
            const width = rect.w / imageSize.x;
            const height = rect.h / imageSize.y;
            
            console.log(`[Phase] 添加 overlay: ${overlay.id || overlay.roi_id}`, {
                rect,
                imageSize: { x: imageSize.x, y: imageSize.y },
                normalized: { x, y, width, height }
            });
            
            // Resolve display colors from the selected palette.
            const color = overlay.level ||
                (Number(overlay.magnification) >= 160 ? 'green' :
                    Number(overlay.magnification) >= 40 ? 'blue' :
                        overlay.color || 'red');
            const semanticLevel = window.PathCopilotRoiPalette?.normalizeLevel(color) ||
                (color === 'green' ? 'level3' : color === 'blue' ? 'level2' : 'level1');
            const borderColor = window.PathCopilotRoiPalette?.getColor(semanticLevel) ||
                (semanticLevel === 'level3' ? '#00A63D' : semanticLevel === 'level2' ? '#0066FF' : '#FF0000');
            const bgColor = window.PathCopilotRoiPalette?.withAlpha(borderColor, 0.12) ||
                (semanticLevel === 'level3' ? 'rgba(0, 166, 61, 0.12)' :
                 semanticLevel === 'level2' ? 'rgba(0, 102, 255, 0.12)' : 'rgba(255, 0, 0, 0.12)');
            
            // 创建 overlay 元素
            const overlayElement = document.createElement('div');
            overlayElement.className = 'phase-overlay';
            overlayElement.dataset.roiId = overlay.roi_id || overlay.id || '';
            overlayElement.dataset.rect = JSON.stringify(rect);
            overlayElement.dataset.level = color;
            overlayElement.dataset.roiLevel = semanticLevel;
            
            overlayElement.style.cssText = `
                position: absolute;
                border: 3px solid ${borderColor};
                background: ${bgColor};
                box-sizing: border-box;
                pointer-events: auto;
                cursor: pointer;
            `;
            
            // 添加标签
            const label = overlay.label || overlay.roi_id || overlay.id || '';
            if (label) {
                const labelEl = document.createElement('div');
                labelEl.className = 'phase-overlay-label';
                labelEl.style.cssText = `
                    position: absolute;
                    top: -24px;
                    left: 0;
                    background: ${borderColor};
                    color: white;
                    padding: 2px 8px;
                    font-size: 12px;
                    font-weight: 600;
                    border-radius: 4px;
                    white-space: nowrap;
                `;
                labelEl.textContent = label;
                overlayElement.appendChild(labelEl);
            }
            
            // 点击事件 - 聚焦到该区域
            overlayElement.addEventListener('click', (e) => {
                // ⭐⭐⭐ 关键修复：如果处于协作模式的编辑状态（增加/删除），跳过聚焦处理
                // 让点击事件继续传播到 canvas-click 处理器，以便添加蓝框/绿框
                // ⭐ 同时检查 editMode（单步编辑）和 mapEditMode（协作模式阶段编辑）
                if (this.editMode === 'add' || this.editMode === 'delete' || this.mapEditMode) {
                    console.log(`[Overlay] 跳过聚焦处理，当前处于编辑模式: editMode=${this.editMode}, mapEditMode=${this.mapEditMode}`);
                    return;  // 不聚焦，让 _mapClickHandler 或 handleMapClickForAddROI 处理
                }
                this.focusOnRegion(rect, overlay.roi_id || overlay.id);
            });
            
            // 添加到 viewer
            this.viewer.addOverlay({
                element: overlayElement,
                location: new OpenSeadragon.Rect(x, y, width, height),
                placement: OpenSeadragon.Placement.TOP_LEFT
            });
            
        } catch (error) {
            console.error('[Phase] 添加 overlay 失败:', error);
        }
    }
    
    /**
     * 根据倍率获取颜色
     */
    getColorByLevel(zoomLevel) {
        if (zoomLevel <= 10) return 'red';
        if (zoomLevel <= 40) return 'blue';
        return 'green';
    }
    
    /**
     * 聚焦到指定区域
     */
    focusOnRegion(rect, roiId) {
        if (!this.viewer || !rect) {
            console.warn(`[focusOnRegion] 无效参数: viewer=${!!this.viewer}, rect=`, rect);
            return;
        }
        
        console.log(`[focusOnRegion] 区域 ${roiId}:`, {
            rect,
            rectType: typeof rect,
            hasX: 'x' in rect,
            hasY: 'y' in rect,
            hasW: 'w' in rect,
            hasH: 'h' in rect
        });
        
        // ⭐⭐⭐ 使用与自动模式相同的方法进行 WSI 定位 ⭐⭐⭐
        // 使用 imageToViewportRectangle 将图像像素坐标转换为视口坐标
        const viewportRect = this.viewer.viewport.imageToViewportRectangle(
            rect.x,
            rect.y,
            rect.w,
            rect.h
        );
        
        console.log(`[focusOnRegion] 视口坐标:`, {
            viewportRect: {
                x: viewportRect.x.toFixed(6),
                y: viewportRect.y.toFixed(6),
                width: viewportRect.width.toFixed(6),
                height: viewportRect.height.toFixed(6)
            }
        });
        
        // 使用 fitBounds 平滑移动到 ROI 位置（与自动模式一致）
        this.viewer.viewport.fitBounds(viewportRect, true);
        
        console.log(`[focusOnRegion] ✅ 已聚焦到区域 ${roiId}`);
        
        // ⭐ 1. 高亮 WSI 上对应的标注框（闪烁效果）
        if (window.pathchat && typeof window.pathchat.highlightROI === 'function') {
            window.pathchat.highlightROI(roiId);
        }
        
        // ⭐ 2. 高亮左侧面板对应的列表项（黄色发光效果，与自动模式一致）
        this.highlightLeftPanelItem(roiId);
    }
    
    /**
     * 高亮左侧面板对应的列表项（黄色发光效果）
     * ⭐ 修复：添加 toggle 逻辑，点击已高亮的项时取消高亮
     */
    highlightLeftPanelItem(roiId) {
        // ⭐ 先检查目标元素是否已经高亮（用于 toggle）
        const regionItem = document.querySelector(`.region-item[data-roi-id="${roiId}"]`);
        const treeNode = document.querySelector(`.route-tree-node[data-roi-id="${roiId}"], .collab-tree-node[data-roi-id="${roiId}"]`);
        const targetElement = regionItem || treeNode;
        
        const isAlreadyHighlighted = targetElement && targetElement.classList.contains('roi-selected-glow');
        
        // 移除所有现有高亮
        document.querySelectorAll('.roi-selected-glow').forEach(el => {
            el.classList.remove('roi-selected-glow');
        });
        
        // ⭐ Toggle 逻辑：如果已经高亮，则只清除，不重新添加
        if (isAlreadyHighlighted) {
            console.log(`[highlightLeftPanel] 🔄 取消高亮: ${roiId}`);
            return;
        }
        
        // 查找并高亮对应的元素
        // 1. 区域列表项 (.region-item)
        if (regionItem) {
            regionItem.classList.add('roi-selected-glow');
            regionItem.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            console.log(`[highlightLeftPanel] ✅ 已高亮区域项: ${roiId}`);
            return;
        }
        
        // 2. 树节点 (.route-tree-node, .collab-tree-node)
        if (treeNode) {
            treeNode.classList.add('roi-selected-glow');
            treeNode.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            console.log(`[highlightLeftPanel] ✅ 已高亮树节点: ${roiId}`);
            return;
        }
        
        // 3. 尝试部分匹配（比如 R2-B6 可能匹配到父节点 R2）
        const parentId = roiId.split('-')[0];
        if (parentId !== roiId) {
            const parentNode = document.querySelector(`.route-tree-node[data-roi-id="${parentId}"], .region-item[data-roi-id="${parentId}"]`);
            if (parentNode) {
                parentNode.classList.add('roi-selected-glow');
                parentNode.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                console.log(`[highlightLeftPanel] ✅ 已高亮父节点: ${parentId}`);
            }
        }
    }
    
    /**
     * 处理 Orchestrator 确认回复
     */
    handleOrchestratorReply(data) {
        console.log('📨 收到 Orchestrator 回复:', data);
        
        const { action, message } = data;
        
        // 在聊天中显示 Orchestrator 的回复
        if (window.pathchat && typeof window.pathchat.addChatMessage === 'function') {
            window.pathchat.addChatMessage('assistant', `
                <div class="orchestrator-reply">
                    <i class="fas fa-robot"></i>
                    <span>${message}</span>
                </div>
            `);
        }
    }
    
    /**
     * 处理阶段确认接收
     */
    handlePhaseConfirmReceived(data) {
        console.log('✅ 阶段确认已接收:', data);
        if (Number(data?.phase) === 6 && this.pendingPatientReportReview) {
            const acknowledgedPatientId = String(data?.patient_id || '');
            const pendingPatientId = String(
                this.pendingPatientReportReview.patientId || ''
            );
            if (!acknowledgedPatientId || acknowledgedPatientId !== pendingPatientId) {
                console.warn('Ignoring stale patient report acknowledgement', data);
                return;
            }
        }
        const acknowledgedGateId = String(data?.phase_gate_id || '');
        let pendingEntry = acknowledgedGateId
            ? this._pendingPhaseConfirmations.get(acknowledgedGateId)
            : null;
        if (
            !pendingEntry
            && Number(data?.phase) === 6
            && Number.isFinite(Number(data?.phase))
        ) {
            pendingEntry = Array.from(this._pendingPhaseConfirmations.values())
                .find((entry) => Number(entry.phase) === Number(data.phase));
        }
        const pendingGateId = acknowledgedGateId || (
            pendingEntry
                ? Array.from(this._pendingPhaseConfirmations.entries())
                    .find(([, value]) => value === pendingEntry)?.[0]
                : ''
        );
        if (
            pendingEntry
            && (
                Number(pendingEntry.phase) !== Number(data?.phase)
                || (
                    !this.isPatientLevelMode
                    && String(pendingEntry.runId || '') !== String(this.runtimeRunId || '')
                )
                || (
                    !this.isPatientLevelMode
                    && String(pendingEntry.fileId || '') !== String(this.getCurrentFileId() || '')
                )
            )
        ) {
            console.warn('Ignoring stale or mismatched phase acknowledgement', data);
            return;
        }
        
        if (data.status === 'submitting') {
            if (pendingEntry) {
                window.clearTimeout(pendingEntry.timeoutId);
                pendingEntry.timeoutId = null;
                this._setPhaseConfirmationSubmitting(pendingEntry.card, true);
                pendingEntry.card?.setAttribute('data-confirm-state', 'submitting');
                const isEn = (window.__lang || 'zh') === 'en';
                let status = pendingEntry.card?.querySelector('.phase-submission-status');
                if (!status && pendingEntry.card) {
                    status = document.createElement('p');
                    status.className = 'phase-submission-status';
                    pendingEntry.card.querySelector('.message-content')?.appendChild(status);
                }
                if (status) status.textContent = isEn
                    ? 'Server is evaluating your proposed changes…'
                    : '服务器正在评估您的修改…';
            }
            return;
        }

        if (data.status === 'success') {
            const modifiedIds = Array.isArray(data?.selected_region_ids)
                ? data.selected_region_ids.map(id => String(id || '').trim()).filter(Boolean)
                : [];
            if (String(data?.action || '') === 'modify' && modifiedIds.length) {
                const phaseLevel = { 1: 'red', 2: 'blue', 3: 'green' }[Number(data?.phase)];
                if (phaseLevel) {
                    this._configureSubagentBoard(
                        modifiedIds,
                        modifiedIds.length,
                        'pending',
                        { authoritativeLevel: phaseLevel }
                    );
                }
            }
            if (pendingEntry) {
                if (pendingEntry.timeoutId) window.clearTimeout(pendingEntry.timeoutId);
                if (pendingGateId) this._pendingPhaseConfirmations.delete(pendingGateId);
                this._setPhaseConfirmationSubmitting(pendingEntry.card, true);
                pendingEntry.card?.setAttribute('data-confirm-state', 'confirmed');
                this.updatePhaseCardStatus(
                    pendingEntry.phase,
                    pendingEntry.action === 'execute' ? 'confirmed' : 'modified',
                    acknowledgedGateId || pendingGateId
                );
                if (typeof pendingEntry.onSuccess === 'function') {
                    pendingEntry.onSuccess(data);
                } else {
                    this._resolvePhaseApprovalCard(pendingEntry.card, pendingEntry.action);
                }
                pendingEntry.resolve?.(data);
            } else if (acknowledgedGateId) {
                // A refreshed page has no in-memory Promise, but the durable
                // gate ACK still owns the visible card.  Converge that card to
                // the server result instead of leaving it permanently busy.
                const restoredCard = document.querySelector(
                    `.phase-confirm-message[data-gate-id="${CSS.escape(acknowledgedGateId)}"]`
                );
                if (restoredCard) {
                    this._setPhaseConfirmationSubmitting(restoredCard, true);
                    restoredCard.setAttribute('data-confirm-state', 'confirmed');
                    this._resolvePhaseApprovalCard(restoredCard, String(data?.action || 'execute'));
                    this.updatePhaseCardStatus(
                        Number(data?.phase),
                        String(data?.action || 'execute') === 'modify' ? 'modified' : 'confirmed',
                        acknowledgedGateId
                    );
                }
            }
            if (Number(data.phase) === 6 && this.pendingPatientReportReview) {
                const pendingReview = this.pendingPatientReportReview;
                this.applyPatientReportConfirmation(pendingReview.report);
            }
            // 确认成功
            if (data.ai_approved) {
                this.showNotification((window.__lang||'zh')==='en' ? '✅ AI approved your changes, proceeding' : '✅ AI 认可您的修改，继续下一阶段', 'success');
            }
        } else if (data.status === 'pending_decision') {
            // AI 不认可，等待用户最终决定（UI 已在 handleModificationReview 中处理）
            console.log(`[Phase ${data.phase}] AI 不认可修改，等待用户决定`);
        } else if (data.status === 'error') {
            if (pendingEntry) {
                if (pendingEntry.timeoutId) window.clearTimeout(pendingEntry.timeoutId);
                if (pendingGateId) this._pendingPhaseConfirmations.delete(pendingGateId);
                this._setPhaseConfirmationSubmitting(pendingEntry.card, false);
                pendingEntry.card?.setAttribute('data-confirm-state', 'error');
                pendingEntry.resolve?.(data);
            } else if (acknowledgedGateId) {
                // A refresh during a slow modification restores the durable
                // submitting card without recreating its in-memory Promise.
                // If server-side evaluation fails later, release that restored
                // card so the still-pending gate can be retried.
                const restoredCard = document.querySelector(
                    `.phase-confirm-message[data-gate-id="${CSS.escape(acknowledgedGateId)}"]`
                );
                const restoredRunId = String(restoredCard?.dataset?.runId || '');
                const restoredFileId = String(restoredCard?.dataset?.fileId || '');
                const currentRunId = String(this.runtimeRunId || '');
                const currentFileId = String(this.getCurrentFileId() || '');
                const restoredPatientSlide = Boolean(
                    this.isPatientLevelMode
                    && restoredFileId
                    && this._indexForPatientFile(restoredFileId) != null
                );
                if (
                    restoredCard
                    && (!restoredRunId || restoredRunId === currentRunId)
                    && (
                        restoredPatientSlide
                        || !restoredFileId
                        || restoredFileId === currentFileId
                    )
                ) {
                    this._setPhaseConfirmationSubmitting(restoredCard, false);
                    restoredCard.setAttribute('data-confirm-state', 'error');
                    restoredCard.querySelector('.phase-submission-status')?.remove();
                }
            }
            if (Number(data.phase) === 6 && this.pendingPatientReportReview) {
                const confirmBtn = document.getElementById('plReviewConfirmBtn');
                const editBtn = document.getElementById('plReviewEditBtn');
                if (confirmBtn) {
                    confirmBtn.disabled = false;
                    confirmBtn.innerHTML = `<i class="fas fa-check"></i> ${
                        (window.__lang || 'zh') === 'en' ? 'Confirm' : '确认'
                    }`;
                }
                if (editBtn) editBtn.disabled = false;
                this.pendingPatientReportReview = null;
            }
            this.showNotification((window.__lang||'zh')==='en' ? `Confirmation failed: ${data.error}` : `确认失败: ${data.error}`, 'error');
        }
    }
    
    /**
     * 处理 AI 修改评估结果
     * ⭐ 智能人机协作设计：
     * - 只有用户提供理由时才显示 AI 评估
     * - AI 拒绝不阻止用户继续，只是提示
     */
    handleModificationReview(data) {
        console.log('🤖 收到 AI 修改评估:', data);
        
        const { phase, approved, ai_reason, suggestion, has_ai_review, user_modification, original_count, modified_count } = data;
        
        const _isEn = (window.__lang || 'zh') === 'en';
        // ⭐ 核心逻辑：只有经过 AI 审核时才显示评估结果
        if (!has_ai_review) {
            console.log(`[Phase ${phase}] 用户未提供理由，跳过 AI 评估显示`);
            this.showNotification(_isEn 
                ? `✅ Changes accepted (${original_count} → ${modified_count} regions)` 
                : `✅ 已接受您的修改 (${original_count} → ${modified_count} 个区域)`, 'success');
            return;
        }
        
        // 用户提供了理由，显示 AI 评估结果
        this.showAIReviewMessage(phase, approved, ai_reason, suggestion, {
            original_count,
            modified_count,
            user_reason: user_modification?.reason
        });
    }
    
    /**
     * 显示 AI 评估消息
     * ⭐ 智能人机协作设计：
     * - AI 同意：显示认可消息，自动继续
     * - AI 拒绝：显示理由和建议，但提供"忽略提示，继续"按钮（不阻止用户）
     */
    showAIReviewMessage(phase, approved, aiReason, suggestion, stats) {
        const chatMessages = document.querySelector('.chat-messages');
        if (!chatMessages) return;
        
        const isEn = (window.__lang || 'zh') === 'en';
        const reviewMsg = document.createElement('div');
        reviewMsg.className = `ai-review-message ${approved ? 'approved' : 'disapproved'}`;
        reviewMsg.dataset.phase = phase;
        
        if (approved) {
            reviewMsg.innerHTML = `
                <div class="ai-review-header">
                    <span class="status-icon">✓</span>
                    <span class="status-text">${isEn ? 'AI Approved Your Changes' : 'AI 认可您的修改'}</span>
                </div>
                <div class="ai-review-reason">${aiReason || (isEn ? 'No reason was provided by the AI.' : 'AI 未给出理由。')}</div>
                <div class="ai-review-stats">
                    <small>${isEn ? 'Region count:' : '区域数量变化:'} ${stats.original_count} → ${stats.modified_count}</small>
                </div>
            `;
            this.showNotification(isEn ? '✅ AI approved your changes, proceeding to next phase' : '✅ AI 认可您的修改，继续下一阶段', 'success');
        } else {
            reviewMsg.innerHTML = `
                <div class="ai-review-header">
                    <span class="status-icon">⚠️</span>
                    <span class="status-text">${isEn ? 'AI Has a Different Opinion' : 'AI 有不同看法'}</span>
                </div>
                <div class="ai-review-reason">${aiReason || (isEn ? 'AI suggests reconsidering this modification.' : 'AI 认为这个修改可能需要重新考虑。')}</div>
                ${suggestion ? `
                    <div class="ai-review-suggestion">
                        <strong>${isEn ? '💡 AI Suggestion:' : '💡 AI 建议：'}</strong>
                        ${suggestion}
                    </div>
                ` : ''}
                <div class="ai-review-stats">
                    <small>${isEn ? 'Your changes:' : '您的修改:'} ${stats.original_count} → ${stats.modified_count} ${isEn ? 'regions' : '个区域'}</small>
                    ${stats.user_reason ? `<br><small>${isEn ? 'Your reason:' : '您的理由:'} ${stats.user_reason}</small>` : ''}
                </div>
                <div class="ai-review-notice">
                    <i class="fas fa-info-circle"></i>
                    <span>${isEn ? 'AI suggestions are for reference only. You may choose to proceed.' : 'AI 的建议仅供参考，您可以选择忽略继续下一步'}</span>
                </div>
                <div class="ai-review-actions">
                    <button class="btn-dismiss-continue" data-phase="${phase}">
                        <i class="fas fa-arrow-right"></i> ${isEn ? 'Dismiss and Continue' : '忽略提示，继续下一步'}
                    </button>
                </div>
            `;
            
            // 绑定"忽略提示，继续"按钮事件
            setTimeout(() => {
                const dismissBtn = reviewMsg.querySelector('.btn-dismiss-continue');
                
                if (dismissBtn) {
                    dismissBtn.addEventListener('click', () => {
                        // 用户选择忽略 AI 建议，继续下一步
                        console.log(`[Phase ${phase}] 用户选择忽略 AI 建议，继续下一步`);
                        this.showNotification((window.__lang||'zh')==='en' ? 'AI suggestion dismissed, proceeding' : '已忽略 AI 建议，继续下一阶段', 'info');
                        this.disableReviewActions(reviewMsg);
                        // 标记消息为已处理
                        reviewMsg.classList.add('dismissed');
                    });
                }
            }, 0);
        }
        
        chatMessages.appendChild(reviewMsg);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }
    
    /**
     * 发送用户最终决定
     */
    sendUserFinalDecision(phase, insist) {
        console.log(`📤 发送用户最终决定: phase=${phase}, insist=${insist}`);
        
        this.socket.emit('agent.user_final_decision', {
            session_id: this.sessionId,
            phase: phase,
            insist: insist
        });
        
        if (insist) {
            this.showNotification((window.__lang||'zh')==='en' ? '⏳ Applying your changes...' : '⏳ 正在应用您的修改...', 'info');
        } else {
            this.showNotification((window.__lang||'zh')==='en' ? '⏳ Restoring AI original selection...' : '⏳ 正在恢复 AI 原始选择...', 'info');
        }
    }
    
    /**
     * 禁用评估消息中的操作按钮
     */
    disableReviewActions(reviewMsg) {
        const actions = reviewMsg.querySelector('.ai-review-actions');
        if (actions) {
            actions.querySelectorAll('button').forEach(btn => {
                btn.disabled = true;
                btn.style.opacity = '0.5';
                btn.style.cursor = 'not-allowed';
            });
        }
    }
    
    /**
     * 处理用户决定接收确认
     */
    handleUserDecisionReceived(data) {
        console.log('✅ 用户决定已接收:', data);
        
        if (data.status === 'success') {
            if (data.insist) {
                this.showNotification((window.__lang||'zh')==='en' ? '✅ Proceeding with your changes' : '✅ 已按照您的修改继续分析', 'success');
            } else {
                this.showNotification((window.__lang||'zh')==='en' ? '✅ AI original selection restored' : '✅ 已恢复 AI 原始选择', 'success');
            }
        } else if (data.status === 'error') {
            this.showNotification((window.__lang||'zh')==='en' ? `Decision handling failed: ${data.error}` : `决定处理失败: ${data.error}`, 'error');
        }
    }
    
    // ==================== End 五阶段协作模式事件处理 ====================
}

// DOM加载完成后初始化；同时兼容脚本在 DOMContentLoaded 之后装载。
function initializePathChatAgentController() {
    if (window.pathChatAgent) return;
    console.log('DOM加载完成，初始化Agent控制器');
    window.pathChatAgent = new PathChatAgentController();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializePathChatAgentController, { once: true });
} else {
    initializePathChatAgentController();
}
