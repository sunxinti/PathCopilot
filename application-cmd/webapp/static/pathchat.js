/**
 * PathGPT JavaScript
 * 
 * 主要功能：
 * 1. 文件上传和管理
 * 2. OpenSeadragon WSI查看器
 * 3. ROI选择和分析
 * 4. GPT多模态聊天
 */

function normalizeViewerWheelSteps(originalEvent, fallbackDirection = 0, viewportHeight = 800) {
    const event = originalEvent || {};
    let steps = 0;
    const legacyDelta = Number(event.wheelDeltaY ?? event.wheelDelta);
    if (Number.isFinite(legacyDelta) && legacyDelta !== 0) {
        steps = legacyDelta / 120;
    } else {
        let pixels = Number(event.deltaY);
        if (Number.isFinite(pixels) && pixels !== 0) {
            if (Number(event.deltaMode) === 1) pixels *= 40;
            else if (Number(event.deltaMode) === 2) pixels *= Math.max(1, Number(viewportHeight) || 800);
            steps = -pixels / 100;
        } else {
            steps = Number(fallbackDirection) || 0;
        }
    }
    return Math.max(-2, Math.min(2, steps));
}

class PathChatApp {
    constructor() {
        // 应用状态
        this.currentFile = null;
        this.viewer = null;
        this.currentROI = null;
        this.isSelecting = false;
        this.selectionOverlay = null;
        this.screenshotMode = false;
        this.clickPlaceRoiMode = false;
        this._userRoiSerial = 0;
        this.selectedUserRois = [];
        this._uploadedFilesById = {};
        this._specimenSitePromise = null;
        this._specimenSiteFileId = '';
        
        // 聊天状态
        this.chatHistory = [];
        this.isAnalyzing = false;
        this.currentHighlightedMessage = null;  // 当前高亮的消息元素
        this._pendingAnalysisChoice = null;
        this._historyChooserGeneration = 0;
        this._chatTurnSerial = 0;
        this._activeChatTurnId = 0;
        this.patientAnalysisAwaitingQuestion = null;
        this.patientIsRunning = false;
        this.patientStopRequested = false;
        this.patientWsiExecutionMode = 'parallel';
        try {
            this.currentPatientId = window.sessionStorage?.getItem('pathcopilot.patient.active_id') || null;
            this.patientRunSessionId = window.sessionStorage?.getItem('pathcopilot.patient.active_session') || '';
        } catch (_) {
            this.currentPatientId = null;
            this.patientRunSessionId = '';
        }
        
        // DOM元素
        this.elements = {
            // 文件上传
            uploadArea: document.getElementById('uploadArea'),
            fileInput: document.getElementById('fileInput'),
            uploadProgress: document.getElementById('uploadProgress'),
            progressFill: document.getElementById('progressFill'),
            progressText: document.getElementById('progressText'),
            filesList: document.getElementById('filesList'),
            refreshFiles: document.getElementById('refreshFiles'),
            
            // 查看器
            viewerContainer: document.getElementById('openseadragon-viewer'),
            viewerEmpty: document.getElementById('viewerEmpty'),
            viewerInfo: document.getElementById('viewerInfo'),
            zoomLevel: document.getElementById('zoomLevel'),
            imageDimensions: document.getElementById('imageDimensions'),
            roiStatus: document.getElementById('roiStatus'),
            
            // 查看器控制
            zoomInBtn: document.getElementById('zoomInBtn'),
            zoomOutBtn: document.getElementById('zoomOutBtn'),
            homeBtn: document.getElementById('homeBtn'),
            fullscreenBtn: document.getElementById('fullscreenBtn'),
            
            // 聊天
            chatMessages: document.getElementById('chatMessages'),
            chatInput: document.getElementById('chatInput'),
            sendBtn: document.getElementById('sendBtn'),
            roiSelectionInfo: document.getElementById('roiSelectionInfo'),
            clearRoiBtn: document.getElementById('clearRoiBtn'),
            
            // 状态指示器
            statusIndicator: document.getElementById('statusIndicator'),
            loadingOverlay: document.getElementById('loadingOverlay'),
            loadingText: document.getElementById('loadingText'),
            notification: document.getElementById('notification')
        };
        
        this.init();
    }
    
    /**
     * 初始化应用
     */
    init() {
        this.setupEventListeners();
        this.bindPatientReviewActions();
        this.setupTabSwitching();
        this.loadFilesList({ restoreActiveRuntime: true });
        this.updateStatus('ready');
        
        // 初始化推理路径树状图
        this.initReasoningTree();
        
        // 存储彩色ROI覆盖层
        this.roiOverlays = [];  // 存储所有标注框
        this.roiAnalysisData = {};  // 存储每个ROI的分析数据
        
        // ⭐ 初始化可拖拽分割面板
        this.initPanelResizers();
        
        console.log('🔬 PathCopilot 已启动');
    }

    /**
     * Bind the Patient Level collaborative review actions once on a stable
     * ancestor. Review cards are refreshed by progress polling, so handlers
     * attached to individual buttons would otherwise be discarded whenever
     * the card is re-rendered.
     */
    bindPatientReviewActions() {
        if (document.documentElement.dataset.patientReviewActionsBound === 'true') return;
        document.documentElement.dataset.patientReviewActionsBound = 'true';
        document.addEventListener('click', (event) => {
            const target = event.target instanceof Element ? event.target : null;
            const button = target?.closest('[data-review-action]');
            if (!button || button.disabled) return;
            event.preventDefault();
            event.stopPropagation();
            this.submitPatientSlideReview(
                button.dataset.fileId,
                Number(button.dataset.index),
                button.dataset.reviewAction
            );
        }, true);
    }
    
    /**
     * ⭐ 初始化可拖拽分割面板
     */
    initPanelResizers() {
        const leftResizer = document.getElementById('leftResizer');
        const rightResizer = document.getElementById('rightResizer');
        const leftPanel = document.querySelector('.left-panel');
        const rightPanel = document.querySelector('.right-panel');
        const centerPanel = document.querySelector('.center-panel');
        const mainContent = document.querySelector('.main-content');
        const workbench = document.getElementById('v2WorkbenchShell');
        
        if (!leftResizer || !rightResizer || !leftPanel || !rightPanel || !centerPanel || !mainContent) {
            console.warn('拖拽分割面板元素未找到');
            return;
        }
        
        // 默认宽度（用于双击重置）
        const defaultLeftWidth = 276;
        const defaultRightWidth = 430;
        
        // 从 localStorage 恢复上次的宽度
        const savedLeftWidth = localStorage.getItem('pathchat_v2_leftPanelWidth');
        const savedRightWidth = localStorage.getItem('pathchat_v2_rightPanelWidth');
        
        const toNumber = (value, fallback) => {
            const parsed = Number.parseFloat(value);
            return Number.isFinite(parsed) ? parsed : fallback;
        };

        const getRightWidthBounds = () => {
            const rightStyle = window.getComputedStyle(rightPanel);
            const minWidth = Math.max(300, toNumber(rightStyle.minWidth, 340));
            const leftWidth = workbench?.classList.contains('v2-workbench-sidebar-open')
                ? leftPanel.getBoundingClientRect().width
                : 0;
            const dividerWidth = rightResizer.getBoundingClientRect().width;
            // The assistant may expand across almost the entire workbench. Keep
            // only a slim strip of the slide area so the divider remains easy
            // to find and drag back.
            const centerHandleWidth = 24;
            const available = mainContent.clientWidth - leftWidth - dividerWidth - centerHandleWidth;
            return {
                min: minWidth,
                max: Math.max(minWidth, available)
            };
        };

        const clampRightWidth = (value) => {
            const bounds = getRightWidthBounds();
            return Math.max(bounds.min, Math.min(bounds.max, value));
        };

        const setRightWidth = (value) => {
            const width = clampRightWidth(value);
            document.documentElement.style.setProperty('--right-panel-width', `${width}px`);
            rightPanel.style.width = '';
            return width;
        };

        if (savedLeftWidth) {
            const restoredLeft = Math.max(220, Math.min(380, toNumber(savedLeftWidth, defaultLeftWidth)));
            document.documentElement.style.setProperty('--left-panel-width', `${restoredLeft}px`);
        }
        const cssRightWidth = toNumber(
            window.getComputedStyle(document.documentElement).getPropertyValue('--right-panel-width'),
            defaultRightWidth
        );
        setRightWidth(toNumber(savedRightWidth, cssRightWidth));
        
        // 拖拽状态
        let isResizing = false;
        let currentResizer = null;
        let startX = 0;
        let startWidth = 0;
        
        // 通知 OpenSeadragon 重新计算布局
        const notifyViewerResize = () => {
            if (this.viewer) {
                setTimeout(() => {
                    if (typeof this.viewer.forceResize === 'function') {
                        this.viewer.forceResize();
                    } else if (this.viewer.viewport && typeof this.viewer.viewport.resize === 'function') {
                        this.viewer.viewport.resize();
                    }
                    if (typeof this.viewer.forceRedraw === 'function') this.viewer.forceRedraw();
                }, 50);
            }
        };
        
        // 保存宽度到 localStorage
        const saveWidths = () => {
            localStorage.setItem('pathchat_v2_leftPanelWidth', leftPanel.offsetWidth);
            localStorage.setItem('pathchat_v2_rightPanelWidth', rightPanel.offsetWidth);
        };
        
        // 开始拖拽
        const startResize = (e, resizer, panel, isLeft) => {
            isResizing = true;
            currentResizer = resizer;
            startX = e.clientX;
            startWidth = panel.offsetWidth;
            
            resizer.classList.add('dragging');
            document.body.classList.add('resizing');
            
            const onMouseMove = (e) => {
                if (!isResizing) return;
                
                const delta = e.clientX - startX;
                let newWidth;
                
                if (isLeft) {
                    newWidth = startWidth + delta;
                } else {
                    newWidth = startWidth - delta;
                }
                
                // 限制宽度范围
                if (isLeft) {
                    newWidth = Math.max(220, Math.min(380, newWidth));
                    document.documentElement.style.setProperty('--left-panel-width', `${newWidth}px`);
                    leftPanel.style.width = '';
                } else {
                    newWidth = setRightWidth(newWidth);
                }
            };
            
            const onMouseUp = () => {
                if (!isResizing) return;
                
                isResizing = false;
                currentResizer.classList.remove('dragging');
                document.body.classList.remove('resizing');
                
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
                
                // 通知 OpenSeadragon 重新计算
                notifyViewerResize();
                
                // 保存宽度
                saveWidths();
                
                console.log(`📐 面板宽度已调整: 左侧=${leftPanel.offsetWidth}px, 右侧=${rightPanel.offsetWidth}px`);
            };
            
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        };
        
        // 左侧拖拽条
        leftResizer.addEventListener('mousedown', (e) => {
            e.preventDefault();
            startResize(e, leftResizer, leftPanel, true);
        });
        
        // 右侧拖拽条
        rightResizer.addEventListener('mousedown', (e) => {
            e.preventDefault();
            startResize(e, rightResizer, rightPanel, false);
        });
        
        // 双击重置为默认宽度
        leftResizer.addEventListener('dblclick', () => {
            document.documentElement.style.setProperty('--left-panel-width', `${defaultLeftWidth}px`);
            leftPanel.style.width = '';
            notifyViewerResize();
            saveWidths();
            console.log('📐 左侧面板已重置为默认宽度');
        });
        
        rightResizer.addEventListener('dblclick', () => {
            setRightWidth(defaultRightWidth);
            notifyViewerResize();
            saveWidths();
            console.log('📐 右侧面板已重置为默认宽度');
        });

        window.addEventListener('resize', () => {
            const clampedWidth = clampRightWidth(rightPanel.offsetWidth);
            if (Math.abs(rightPanel.offsetWidth - clampedWidth) > 1) {
                setRightWidth(clampedWidth);
                saveWidths();
            }
            notifyViewerResize();
        });
        
        console.log('✅ 可拖拽分割面板已初始化');
    }
    
    /**
     * 初始化推理路径树状图
     */
    initReasoningTree() {
        if (typeof ReasoningTreeVisualization !== 'undefined') {
            this.reasoningTree = new ReasoningTreeVisualization('treeVisualization');
            
            // 设置节点点击回调（跳转到对应视图）
            this.reasoningTree.onNodeClick = (nodeData) => {
                this.jumpToROIView(nodeData);
            };
            
            console.log('🌳 推理路径树已初始化');
        } else {
            console.warn('⚠️ 推理路径树组件未加载');
        }
        
        // 设置节点详情关闭按钮
        const closeBtn = document.getElementById('closeNodeDetails');
        if (closeBtn) {
            closeBtn.addEventListener('click', () => {
                document.getElementById('nodeDetails').style.display = 'none';
            });
        }
    }
    
    /**
     * 跳转到ROI视图
     */
    jumpToROIView(nodeData) {
        if (!this.viewer) return;
        
        console.log('📍 跳转到ROI视图:', nodeData);
        
        // 计算视口坐标
        const viewportRect = this.viewer.viewport.imageToViewportRectangle(
            nodeData.rect.x,
            nodeData.rect.y,
            nodeData.rect.w,
            nodeData.rect.h
        );
        
        // 计算合适的缩放级别以适配ROI
        const imageRect = this.viewer.viewport.getBounds();
        const zoom = Math.min(
            imageRect.width / viewportRect.width,
            imageRect.height / viewportRect.height
        ) * 0.8;
        
        // 平滑移动到ROI位置
        this.viewer.viewport.fitBounds(viewportRect, true);
        
        // 高亮显示对应的标注框
        this.highlightROI(nodeData.id);
    }
    
    /**
     * 设置事件监听器
     */
    setupEventListeners() {
        // 文件上传 - 支持多文件检测
        this.elements.uploadArea.addEventListener('click', () => {
            this.elements.fileInput.click();
        });
        
        // 获取文件夹输入元素
        this.elements.folderInput = document.getElementById('folderInput');
        
        this.elements.fileInput.addEventListener('change', (e) => {
            const files = e.target.files;
            if (files.length > 0) {
                this.handleFileSelection(files);
            }
        });
        
        // 文件夹选择
        if (this.elements.folderInput) {
            this.elements.folderInput.addEventListener('change', (e) => {
                const files = e.target.files;
                if (files.length > 0) {
                    this.handleFileSelection(files, true);
                }
            });
        }
        
        // 拖拽上传
        this.elements.uploadArea.addEventListener('dragover', (e) => {
            e.preventDefault();
            this.elements.uploadArea.classList.add('dragover');
        });
        
        this.elements.uploadArea.addEventListener('dragleave', () => {
            this.elements.uploadArea.classList.remove('dragover');
        });
        
        this.elements.uploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            this.elements.uploadArea.classList.remove('dragover');
            
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                this.handleFileSelection(files);
            }
        });
        
        // Patient Level 弹窗事件
        this.setupPatientLevelModal();
        
        // Patient Level 重新汇总按钮
        const plRerunBtn = document.getElementById('plRerunOrchestrator');
        if (plRerunBtn) {
            plRerunBtn.addEventListener('click', () => this._rerunOrchestrator());
        }
        
        // 文件列表刷新
        this.elements.refreshFiles.addEventListener('click', () => {
            this.loadFilesList();
        });
        
        // 查看器控制按钮
        this.elements.zoomInBtn.addEventListener('click', () => {
            if (this.viewer) {
                this.viewer.viewport.zoomBy(1.5);
            }
        });
        
        this.elements.zoomOutBtn.addEventListener('click', () => {
            if (this.viewer) {
                this.viewer.viewport.zoomBy(0.67);
            }
        });
        
        this.elements.homeBtn.addEventListener('click', () => {
            if (this.viewer) {
                this.viewer.viewport.goHome();
            }
        });
        
        this.elements.fullscreenBtn.addEventListener('click', () => {
            if (this.viewer) {
                this.viewer.setFullScreen(!this.viewer.isFullScreen());
            }
        });
        
        // 树状图按钮
        const treeViewBtn = document.getElementById('treeViewBtn');
        if (treeViewBtn) {
            treeViewBtn.addEventListener('click', () => {
                this.openTreeModal();
            });
        }
        
        // 保存按钮：先写入应用缓存（与「从缓存加载」同一份），再可选另存 JSON 副本
        const saveStateBtn = document.getElementById('btnSaveState');
        if (saveStateBtn) {
            saveStateBtn.addEventListener('click', () => {
                this.saveAnalysisState({ exportToUserPath: true });
            });
        }
        
        // 加载分析状态按钮 — 下拉菜单
        const loadStateBtn = document.getElementById('btnLoadState');
        const loadStateMenu = document.getElementById('loadStateMenu');
        if (loadStateBtn && loadStateMenu) {
            loadStateBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                loadStateMenu.classList.toggle('show');
            });
            document.addEventListener('click', (e) => {
                if (!e.target.closest('#loadStateDropdown')) {
                    loadStateMenu.classList.remove('show');
                }
            });
            document.getElementById('loadFromCache')?.addEventListener('click', async () => {
                loadStateMenu.classList.remove('show');
                if (this.patientLevelUploadResults?.length > 0) {
                    await this._loadPatientLevelWithHistory();
                } else if (this.currentFile) {
                    await this._loadWithHistorySelection(this.currentFile);
                } else {
                    this.showNotification('warning', __t('need_load_wsi'));
                }
            });
            document.getElementById('loadFromFile')?.addEventListener('click', async () => {
                loadStateMenu.classList.remove('show');
                await this.loadFromLocalJsonFile();
            });
        }
        
        // 聊天功能
        this.elements.chatInput.addEventListener('input', () => {
            this.updateSendButton();
        });
        
        this.elements.chatInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.sendMessage();
            }
        });
        
        this.elements.sendBtn.addEventListener('click', () => {
            this.sendMessage();
        });
        
        this.elements.clearRoiBtn.addEventListener('click', () => {
            this.clearROI();
        });
        
        // ⭐ 聊天消息中的特殊链接处理（事件委托）
        this.elements.chatMessages.addEventListener('click', (e) => {
            const link = e.target.closest('a');
            if (link) {
                const href = link.getAttribute('href');
                // 处理 #show-tree 链接 - 打开树状图
                if (href === '#show-tree') {
                    e.preventDefault();
                    this.openTreeModal();
                }
                // 处理外部链接 - 在新窗口打开
                else if (href && href.startsWith('/outputs/')) {
                    e.preventDefault();
                    window.open(href, '_blank');
                }
            }
        });
        
        // 快捷操作按钮
        document.querySelectorAll('.quick-action-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const prompt = btn.dataset.prompt;
                this.elements.chatInput.value = prompt;
                this.updateSendButton();
            });
        });
        
        // 通知关闭
        document.querySelector('.notification-close').addEventListener('click', () => {
            this.hideNotification();
        });
        
        // ⭐ 截图询问功能
        const screenshotBtn = document.getElementById('screenshotAskBtn');
        if (screenshotBtn) {
            screenshotBtn.addEventListener('click', () => {
                this.toggleScreenshotMode();
            });
        }
        
        // 清除截图按钮
        const clearScreenshotBtn = document.getElementById('clearScreenshotBtn');
        if (clearScreenshotBtn) {
            clearScreenshotBtn.addEventListener('click', () => {
                this.clearScreenshot();
            });
        }
        
        // ⭐ 导出截图按钮（保存当前视图为PNG）
        // 左键点击：快速截图（所见即所得），右键点击：高清截图（从原始WSI提取，原始分辨率）
        const exportScreenshotBtn = document.getElementById('exportScreenshotBtn');
        if (exportScreenshotBtn) {
            exportScreenshotBtn.addEventListener('click', (e) => {
                this.exportViewerScreenshot();  // 左键：快速截图
            });
            exportScreenshotBtn.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                this.exportViewerScreenshotHD();  // 右键：后端高清截图
            });
            // 添加 tooltip 提示
            exportScreenshotBtn.title = __t('screenshot_export');
        }
    }
    
    /**
     * 📷 导出当前视图截图（简单可靠版本）
     * 合成 OpenSeadragon canvas + DOM overlay
     */
    async exportViewerScreenshot() {
        const viewerContainer = document.getElementById('openseadragon-viewer');
        if (!viewerContainer) {
            this.showNotification('error', (window.__lang||'zh')==='en' ? 'Viewer not loaded' : '视图未加载');
            return;
        }
        
        // 兼容多种 viewer 引用方式
        const viewer = this.viewer || window.pathchat?.viewer;
        if (!viewer) {
            this.showNotification('error', (window.__lang||'zh')==='en' ? 'Please load a WSI file first' : '请先加载WSI文件');
            return;
        }
        
        try {
            this.showNotification('info', (window.__lang||'zh')==='en' ? 'Generating screenshot...' : '正在生成截图...');
            
            // 获取 OpenSeadragon 的 canvas
            const osdCanvas = viewer.drawer.canvas;
            if (!osdCanvas) {
                this.showNotification('error', (window.__lang||'zh')==='en' ? 'Cannot access viewer canvas' : '无法获取视图画布');
                return;
            }
            
            // 获取容器和 canvas 尺寸
            const containerRect = viewerContainer.getBoundingClientRect();
            const scale = 2; // 2倍分辨率
            const width = containerRect.width * scale;
            const height = containerRect.height * scale;
            
            // 创建合成画布
            const compositeCanvas = document.createElement('canvas');
            compositeCanvas.width = width;
            compositeCanvas.height = height;
            const ctx = compositeCanvas.getContext('2d');
            
            // 1. 绘制白色背景
            ctx.fillStyle = '#ffffff';
            ctx.fillRect(0, 0, width, height);
            
            // 2. 绘制 OpenSeadragon canvas（WSI 图像）
            ctx.drawImage(osdCanvas, 0, 0, width, height);
            
            // 3. 绘制所有 ROI 框（DOM overlay 元素）
            const overlayElements = viewerContainer.querySelectorAll('[data-roi-id]');
            console.log(`📷 找到 ${overlayElements.length} 个 ROI overlay`);
            
            overlayElements.forEach(el => {
                const elRect = el.getBoundingClientRect();
                const x = (elRect.left - containerRect.left) * scale;
                const y = (elRect.top - containerRect.top) * scale;
                const w = elRect.width * scale;
                const h = elRect.height * scale;
                
                // Resolve the display palette without changing canonical ROI data.
                let roiLevel = 'level1';
                const borderWidth = 4 * scale;
                
                // 10×=Level 1, 40×=Level 2, 160×=Level 3.
                if (el.classList.contains('roi-overlay-40x') || 
                    el.classList.contains('roi-overlay-blue') || 
                    el.dataset.level === 'blue') {
                    roiLevel = 'level2';
                } else if (el.classList.contains('roi-overlay-160x') || 
                           el.classList.contains('roi-overlay-green') || 
                           el.dataset.level === 'green') {
                    roiLevel = 'level3';
                }
                const borderColor = window.PathCopilotRoiPalette?.getColor(roiLevel) ||
                    ({ level1: '#FF0000', level2: '#0066FF', level3: '#00A63D' })[roiLevel];
                
                // 绘制矩形框
                ctx.strokeStyle = borderColor;
                ctx.lineWidth = borderWidth;
                ctx.setLineDash(
                    roiLevel === 'level2' ? [10 * scale, 6 * scale] :
                    roiLevel === 'level3' ? [2 * scale, 5 * scale] : []
                );
                ctx.strokeRect(x, y, w, h);
                ctx.setLineDash([]);
            });
            
            // 生成文件名
            const timestamp = new Date().toISOString().slice(0, 19).replace(/[:-]/g, '');
            const filename = `PathCopilot_Screenshot_${timestamp}.png`;
            
            // 下载
            compositeCanvas.toBlob((blob) => {
                if (blob) {
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = filename;
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                    
                    const sizeMB = (blob.size / 1024 / 1024).toFixed(2);
                    this.showNotification('success', (window.__lang||'zh')==='en' ? `Screenshot saved: ${filename} (${sizeMB}MB)` : `截图已保存: ${filename} (${sizeMB}MB)`);
                    console.log('📷 截图已保存:', filename, `(${width}x${height})`);
                } else {
                    this.showNotification('error', (window.__lang||'zh')==='en' ? 'Screenshot generation failed' : '截图生成失败');
                }
            }, 'image/png', 1.0);
            
        } catch (error) {
            console.error('截图导出失败:', error);
            this.showNotification('error', (window.__lang||'zh')==='en' ? 'Screenshot export failed: ' + error.message : '截图导出失败: ' + error.message);
        }
    }
    
    /**
     * 📷 后端高清截图（原始分辨率）
     * 右键点击截图按钮时调用，从原始 WSI 提取高分辨率图像
     */
    async exportViewerScreenshotHD() {
        const viewerContainer = document.getElementById('openseadragon-viewer');
        const viewer = this.viewer || window.pathchat?.viewer;
        // 正确获取 fileId：从 currentFile 对象中获取
        const fileId = this.currentFile?.file_id || window.pathchat?.currentFile?.file_id;
        
        if (!viewerContainer || !viewer || !fileId) {
            this.showNotification('error', (window.__lang||'zh')==='en' ? 'Please load a WSI file first' : '请先加载WSI文件');
            return;
        }
        
        try {
            this.showNotification('info', (window.__lang||'zh')==='en' ? 'Generating HD screenshot from original WSI...' : '正在生成高清截图（从原始WSI提取）...');
            
            // 1. 获取当前视图范围（原始像素坐标）
            const tiledImage = viewer.world.getItemAt(0);
            if (!tiledImage) {
                this.showNotification('error', (window.__lang||'zh')==='en' ? 'Cannot retrieve image info' : '无法获取图像信息');
                return;
            }
            
            const viewportBounds = viewer.viewport.getBounds();
            const imageRect = tiledImage.viewportToImageRectangle(viewportBounds);
            
            const viewport = {
                x: Math.max(0, Math.round(imageRect.x)),
                y: Math.max(0, Math.round(imageRect.y)),
                width: Math.round(imageRect.width),
                height: Math.round(imageRect.height)
            };
            
            console.log('📷 [HD] 当前视图范围:', viewport);
            
            // 2. 收集当前显示的 ROI 框信息
            const overlays = [];
            const containerRect = viewerContainer.getBoundingClientRect();
            
            viewerContainer.querySelectorAll('[data-roi-id]').forEach(el => {
                const elRect = el.getBoundingClientRect();
                
                // ⭐ 使用 OpenSeadragon 原生坐标转换（修复右键截图框偏移问题）
                // 屏幕像素 → viewport坐标 → WSI原始像素坐标
                const topLeft = viewer.viewport.windowToViewportCoordinates(
                    new OpenSeadragon.Point(elRect.left, elRect.top)
                );
                const bottomRight = viewer.viewport.windowToViewportCoordinates(
                    new OpenSeadragon.Point(elRect.right, elRect.bottom)
                );
                const imgTopLeft = tiledImage.viewportToImageCoordinates(topLeft);
                const imgBottomRight = tiledImage.viewportToImageCoordinates(bottomRight);
                
                const origX = imgTopLeft.x;
                const origY = imgTopLeft.y;
                const origW = imgBottomRight.x - imgTopLeft.x;
                const origH = imgBottomRight.y - imgTopLeft.y;
                
                // 判断颜色
                let color = 'red';
                if (el.classList.contains('roi-overlay-40x') || el.dataset.level === 'blue') {
                    color = 'blue';
                } else if (el.classList.contains('roi-overlay-160x') ||
                           el.classList.contains('roi-overlay-green') ||
                           el.dataset.level === 'green' ||
                           el.dataset.roiLevel === 'level3') {
                    color = 'green';
                }
                
                overlays.push({
                    rect: {
                        x: Math.round(origX),
                        y: Math.round(origY),
                        w: Math.round(origW),
                        h: Math.round(origH)
                    },
                    color: color
                });
            });
            
            console.log('📷 [HD] 收集到', overlays.length, '个 overlay');
            
            // 3. 调用后端 API（使用已有的高清截图接口）
            // 添加超时控制（30秒）
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 30000);
            
            this.showNotification('info', (window.__lang||'zh')==='en' ? 'Extracting HD image from original WSI, please wait...' : '正在从原始WSI提取高清图像，请稍候...');
            
            try {
                const response = await fetch('/api/export_hd_screenshot', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        file_id: fileId,
                        viewport: viewport,
                        overlays: overlays,
                        target_width: 2048  // 适中的输出尺寸（平衡质量和速度）
                    }),
                    signal: controller.signal
                });
                
                clearTimeout(timeoutId);
                
                if (!response.ok) {
                    const errorText = await response.text();
                    console.error('📷 [HD] 服务器响应错误:', errorText);
                    throw new Error(errorText || '服务器错误');
                }
                
                // 4. 下载图片
                const blob = await response.blob();
                const timestamp = new Date().toISOString().slice(0, 19).replace(/[:-]/g, '');
                const filename = `PathCopilot_HD_${timestamp}.png`;
                
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
                
                const sizeMB = (blob.size / 1024 / 1024).toFixed(2);
                this.showNotification('success', (window.__lang||'zh')==='en' ? `HD screenshot saved: ${filename} (${sizeMB}MB)` : `高清截图已保存: ${filename} (${sizeMB}MB)`);
                console.log('📷 [HD] 高清截图已保存:', filename);
                
            } catch (fetchError) {
                clearTimeout(timeoutId);
                if (fetchError.name === 'AbortError') {
                    throw new Error('请求超时（30秒），请尝试缩小视图范围后重试');
                }
                throw fetchError;
            }
            
        } catch (error) {
            console.error('高清截图失败:', error);
            this.showNotification('error', (window.__lang||'zh')==='en' ? 'HD screenshot failed: ' + error.message : '高清截图失败: ' + error.message);
        }
    }
    
    /**
     * 上传文件
     */
    async uploadFile(file) {
        try {
            const maxSize = 10 * 1024 * 1024 * 1024;
            if (file.size > maxSize) {
                this.showNotification('error', (window.__lang||'zh')==='en' ? 'File exceeds 10GB limit' : '文件大小超过10GB限制');
                return;
            }
            
            const supportedFormats = ['.svs', '.ndpi', '.tiff', '.tif', '.vms', '.vmu', '.scn', '.mrxs', '.bif'];
            const fileExt = '.' + file.name.split('.').pop().toLowerCase();
            if (!supportedFormats.includes(fileExt)) {
                this.showNotification('error', (window.__lang||'zh')==='en' ? `Unsupported format. Supported: ${supportedFormats.join(', ')}` : `不支持的文件格式。支持格式：${supportedFormats.join(', ')}`);
                return;
            }
            
            this.updateStatus('processing');
            this.showUploadProgress(true);
            
            const formData = new FormData();
            formData.append('file', file);
            formData.append('batch_id', this._genUUID());
            
            const result = await this._uploadWithProgress('/api/upload', formData, (pct, speed, eta) => {
                this._updateUploadStats(pct, speed, eta);
            });
            
            if (result.success) {
                this._updateUploadStats(100, 0, 0, true);
                this.showNotification('success', (window.__lang||'zh')==='en' ? `File uploaded: ${result.filename}` : `文件上传成功：${result.filename}`);
                this.loadFilesList();
                setTimeout(() => this.loadWSI(result.file_id), 1000);
            } else {
                this.showNotification('error', (window.__lang||'zh')==='en' ? `Upload failed: ${result.error}` : `上传失败：${result.error}`);
            }
            
        } catch (error) {
            console.error('上传失败:', error);
            this.showNotification('error', (window.__lang||'zh')==='en' ? `Upload failed: ${error.message}` : `上传失败：${error.message}`);
        } finally {
            this.showUploadProgress(false);
            this.updateStatus('ready');
            this.elements.fileInput.value = '';
        }
    }

    /**
     * XHR upload with real progress events. Returns a Promise resolving to the JSON response.
     */
    _uploadWithProgress(url, formData, onProgress) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            const startTime = Date.now();
            let smoothSpeed = 0;

            xhr.upload.addEventListener('progress', (e) => {
                if (!e.lengthComputable) return;
                const pct = (e.loaded / e.total) * 100;
                const elapsedSec = (Date.now() - startTime) / 1000;
                const avgSpeed = elapsedSec > 0.5 ? e.loaded / elapsedSec : 0;
                smoothSpeed = smoothSpeed > 0 ? smoothSpeed * 0.7 + avgSpeed * 0.3 : avgSpeed;
                const remaining = smoothSpeed > 0 ? (e.total - e.loaded) / smoothSpeed : 0;
                if (onProgress) onProgress(pct, smoothSpeed, remaining);
            });

            xhr.addEventListener('load', () => {
                try { resolve(JSON.parse(xhr.responseText)); }
                catch { reject(new Error('Invalid server response')); }
            });
            xhr.addEventListener('error', () => reject(new Error('Network error')));
            xhr.addEventListener('abort', () => reject(new Error('Upload cancelled')));

            xhr.open('POST', url);
            xhr.send(formData);
        });
    }

    _formatBytes(bytes) {
        if (bytes < 1024) return bytes.toFixed(0) + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
        return (bytes / 1073741824).toFixed(2) + ' GB';
    }

    _formatETA(seconds) {
        if (!isFinite(seconds) || seconds <= 0) return '--';
        if (seconds < 60) return Math.ceil(seconds) + 's';
        if (seconds < 3600) return Math.floor(seconds / 60) + 'm ' + Math.ceil(seconds % 60) + 's';
        return Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm';
    }

    _updateUploadStats(pct, speed, eta, done = false) {
        this.elements.progressFill.style.width = Math.min(pct, 100) + '%';
        if (done) {
            this.elements.progressText.textContent = __t('processing');
        } else {
            const isEn = (window.__lang || 'zh') === 'en';
            const speedStr = this._formatBytes(speed) + '/s';
            const etaStr = this._formatETA(eta);
            this.elements.progressText.textContent = `${Math.round(pct)}%  ·  ${speedStr}  ·  ${isEn ? 'ETA' : '剩余'} ${etaStr}`;
        }
    }

    /**
     * Show / hide the upload progress bar.
     */
    showUploadProgress(show) {
        this.elements.uploadProgress.style.display = show ? 'block' : 'none';
        if (show) {
            this.elements.progressFill.style.width = '0%';
            this.elements.progressText.textContent = (window.__lang||'zh')==='en' ? 'Preparing upload...' : '准备上传...';
        }
    }
    
    // ================== Patient Level 多文件处理 ==================
    
    /**
     * 处理文件选择（支持多文件检测）
     */
    handleFileSelection(files, isFolder = false) {
        // 过滤出WSI文件
        const supportedFormats = ['.svs', '.ndpi', '.tiff', '.tif', '.vms', '.vmu', '.scn', '.mrxs', '.bif'];
        const wsiFiles = Array.from(files).filter(file => {
            const ext = '.' + file.name.split('.').pop().toLowerCase();
            return supportedFormats.includes(ext);
        });
        
        if (wsiFiles.length === 0) {
            this.showNotification('error', (window.__lang||'zh')==='en' ? 'No supported WSI file format detected' : '未检测到支持的WSI文件格式');
            return;
        }
        
        // 如果只有一个文件，走原来的单文件上传流程
        if (wsiFiles.length === 1) {
            this.uploadFile(wsiFiles[0]);
            return;
        }
        
        // 多个文件 - 显示Patient Level确认弹窗
        this.pendingPatientLevelFiles = wsiFiles;
        this.pendingFolderName = isFolder ? this.extractFolderName(files) : this.generateFolderName(wsiFiles);
        this.showPatientLevelModal(wsiFiles);
    }
    
    /**
     * 从文件列表提取文件夹名称
     */
    extractFolderName(files) {
        if (files.length > 0 && files[0].webkitRelativePath) {
            const path = files[0].webkitRelativePath;
            return path.split('/')[0] || 'Patient';
        }
        return 'Patient';
    }
    
    /**
     * 生成默认文件夹名称
     */
    generateFolderName(files) {
        // 尝试从文件名提取共同前缀
        if (files.length > 0) {
            const firstName = files[0].name;
            // 尝试提取患者ID等信息
            const match = firstName.match(/^(\d+[-_]?\w*)/);
            if (match) {
                return match[1];
            }
        }
        return 'Patient_' + new Date().toISOString().slice(0, 10);
    }
    
    /**
     * 从文件名提取部位信息，返回 "Slide X: 部位名" 格式
     * @param {string} filename - 文件名
     * @param {number} idx - 文件索引（0-based），默认 0
     */
    extractPartInfo(filename, idx = 0) {
        const stem = filename.replace(/\.[^.]+$/, '');
        
        // 中文命名: "数字-部位名" (如 123456-部位1 → "部位1")
        const cnMatch = stem.match(/^\d+[-_](.+)$/);
        if (cnMatch) {
            return `Slide ${idx + 1}: ${cnMatch[1]}`;
        }
        
        // 英文命名
        const patterns = [
            /Part[_\s-]?([A-Z0-9]+)/i,
            /Block[_\s-]?([A-Z0-9]+)/i,
            /DX([1-9])/i
        ];
        for (const pattern of patterns) {
            const match = filename.match(pattern);
            if (match) {
                return `Slide ${idx + 1}: ${match[1].toUpperCase()}`;
            }
        }
        
        return `Slide ${idx + 1}`;
    }
    
    /**
     * 设置Patient Level弹窗事件
     */
    setupPatientLevelModal() {
        const modal = document.getElementById('patientLevelModal');
        const optionPatientLevel = document.getElementById('optionPatientLevel');
        const optionSingleMode = document.getElementById('optionSingleMode');
        const confirmBtn = document.getElementById('patientLevelConfirm');
        const cancelBtn = document.getElementById('patientLevelCancel');
        
        if (!modal) return;
        
        this.patientLevelChoice = 'patient-level'; // 默认选择
        
        // 选项点击
        if (optionPatientLevel) {
            optionPatientLevel.addEventListener('click', () => {
                this.patientLevelChoice = 'patient-level';
                optionPatientLevel.classList.add('selected');
                optionSingleMode?.classList.remove('selected');
            });
        }
        
        if (optionSingleMode) {
            optionSingleMode.addEventListener('click', () => {
                this.patientLevelChoice = 'single';
                optionSingleMode.classList.add('selected');
                optionPatientLevel?.classList.remove('selected');
            });
        }
        
        // 确认按钮
        if (confirmBtn) {
            confirmBtn.addEventListener('click', () => {
                this.hidePatientLevelModal();
                if (this.patientLevelChoice === 'patient-level') {
                    // 切换到Patient Level面板
                    this.switchToPatientLevelPanel();
                    // 开始上传
                    this.startPatientLevelUpload();
                } else {
                    // 逐个上传模式 - 只上传第一个
                    if (this.pendingPatientLevelFiles && this.pendingPatientLevelFiles.length > 0) {
                        this.uploadFile(this.pendingPatientLevelFiles[0]);
                        this.showNotification('info', (window.__lang||'zh')==='en' ? `First file uploaded, please upload remaining ${this.pendingPatientLevelFiles.length - 1} files manually` : `已上传第1个文件，其余${this.pendingPatientLevelFiles.length - 1}个文件请手动上传`);
                    }
                }
            });
        }
        
        // 返回文件管理按钮
        const backBtn = document.getElementById('backToFileManager');
        if (backBtn) {
            backBtn.addEventListener('click', () => {
                this.switchToFileManager();
            });
        }
        
        // Patient Level 开始分析按钮
        const plStartBtn = document.getElementById('plStartAnalysis');
        if (plStartBtn) {
            plStartBtn.addEventListener('click', () => {
                this.startPatientLevelAnalysis();
            });
        }
        const plStopBtn = document.getElementById('plStopAnalysis');
        if (plStopBtn) {
            plStopBtn.addEventListener('click', () => this.stopPatientLevelAnalysis());
        }

        document.querySelectorAll('[data-patient-mode]').forEach((button) => {
            button.addEventListener('click', () => {
                if (button.disabled) return;
                this.setPatientLevelMode(button.dataset.patientMode);
            });
        });
        document.querySelectorAll('[data-patient-schedule]').forEach((button) => {
            button.addEventListener('click', () => {
                if (button.disabled) return;
                this.setPatientWsiExecutionMode(button.dataset.patientSchedule);
            });
        });
        
        // 取消按钮
        if (cancelBtn) {
            cancelBtn.addEventListener('click', () => {
                this.hidePatientLevelModal();
                this.pendingPatientLevelFiles = null;
            });
        }
        
        // 点击背景关闭
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                this.hidePatientLevelModal();
            }
        });
    }
    
    /**
     * 显示Patient Level确认弹窗
     */
    showPatientLevelModal(files) {
        const modal = document.getElementById('patientLevelModal');
        const messageEl = document.getElementById('plModalMessage');
        const previewEl = document.getElementById('detectedFilesPreview');
        const optionPatientLevel = document.getElementById('optionPatientLevel');
        const optionSingleMode = document.getElementById('optionSingleMode');
        
        if (!modal) return;
        
        // 更新弹窗消息（使用 i18n）
        if (messageEl) {
            messageEl.innerHTML = __t('pl_modal_message', files.length);
        }
        
        // 生成文件预览列表
        if (previewEl) {
            const previewHtml = files.slice(0, 5).map((file, idx) => {
                const partInfo = this.extractPartInfo(file.name, idx);
                const sizeStr = this.formatFileSize(file.size);
                return `
                    <div class="preview-file-item">
                        <i class="fas fa-file-medical"></i>
                        <span class="file-name" title="${file.name}">${file.name.length > 30 ? file.name.substring(0, 27) + '...' : file.name}</span>
                        <span class="part-badge">${partInfo}</span>
                        <span class="file-size">${sizeStr}</span>
                    </div>
                `;
            }).join('');
            
            const moreCount = files.length - 5;
            const moreLabel = (window.__lang || 'zh') === 'en' ? `+${moreCount} more` : `还有 ${moreCount} 个文件`;
            const moreHtml = moreCount > 0 ? `<div class="preview-more">... ${moreLabel}</div>` : '';
            
            previewEl.innerHTML = previewHtml + moreHtml;
        }
        
        // 重置选择状态
        this.patientLevelChoice = 'patient-level';
        optionPatientLevel?.classList.add('selected');
        optionSingleMode?.classList.remove('selected');
        
        modal.style.display = 'flex';
    }
    
    /**
     * 隐藏Patient Level确认弹窗
     */
    hidePatientLevelModal() {
        const modal = document.getElementById('patientLevelModal');
        if (modal) modal.style.display = 'none';
    }
    
    /**
     * 格式化文件大小
     */
    formatFileSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
    }

    /**
     * 生成 UUID（兼容 HTTP 非安全上下文，不依赖 crypto.randomUUID）
     */
    _genUUID() {
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
            const r = Math.random() * 16 | 0;
            return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        });
    }
    
    /**
     * 切换到Patient Level面板
     */
    switchToPatientLevelPanel() {
        const fileManager = document.getElementById('fileManagerPanel');
        const patientLevel = document.getElementById('patientLevelPanel');
        
        if (fileManager) fileManager.style.display = 'none';
        if (patientLevel) patientLevel.style.display = 'flex';
        
        // 更新面板信息
        this.updatePatientLevelPanelInfo();
        this.setPatientLevelMode(this.patientLevelMode || 'ai');
        this.setPatientWsiExecutionMode(this.patientWsiExecutionMode || 'parallel');
        if (window.pathChatAgent) {
            window.pathChatAgent.isPatientLevelMode = true;
        }
        window.PathCopilotV2?.setWorkbenchSidebarOpen?.(true);
        this.syncPatientSlideReportNav();
    }

    /**
     * Re-open an already uploaded Patient Level set from the home recents list.
     * File stubs keep the original filenames so upload reuse can skip the bytes.
     */
    openPatientLevelFromExistingFiles(fileRecords, folderName) {
        const records = Array.isArray(fileRecords)
            ? fileRecords.filter((item) => item && item.filename)
            : [];
        if (!records.length) return Promise.resolve(false);
        const stubs = records.map((record) => new File(
            [new Uint8Array(0)],
            record.filename,
            { type: 'application/octet-stream', lastModified: Date.now() }
        ));
        this.pendingPatientLevelFiles = stubs;
        this.pendingFolderName = folderName || this.generateFolderName(stubs);
        this.patientLevelChoice = 'patient-level';
        this.switchToPatientLevelPanel();
        window.PathCopilotV2?.setWorkbenchSidebarOpen?.(true);
        return this.startPatientLevelUpload();
    }

    ensurePatientProgressVisible() {
        this.switchToPatientLevelPanel();
        window.PathCopilotV2?.setWorkbenchSidebarOpen?.(true);
    }

    setPatientLevelMode(mode) {
        const normalizedMode = mode === 'collaborative' ? 'collaborative' : 'ai';
        this.patientLevelMode = normalizedMode;

        document.querySelectorAll('[data-patient-mode]').forEach((button) => {
            const active = button.dataset.patientMode === normalizedMode;
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
        });

        const description = document.getElementById('plModeDescription');
        if (description) {
            const isEnglish = (window.__lang || 'zh') === 'en';
            description.textContent = normalizedMode === 'collaborative'
                ? (isEnglish
                    ? 'Agent Swarm reuses the same per-slide gates as a single WSI. After every slide, the patient-level summary is reviewed separately.'
                    : 'Agent Swarm 每张切片复用单张 WSI 的分阶段审阅；全部完成后还会审阅患者级综合报告。')
                : (isEnglish
                    ? 'Analyze every slide and synthesize the patient-level report automatically.'
                    : '自动分析全部切片并直接生成患者级汇总。');
        }

        if (window.pathChatAgent) {
            window.pathChatAgent.currentMode = normalizedMode;
            window.pathChatAgent.isCollaborativeMode = normalizedMode === 'collaborative';
        }
    }

    setPatientWsiExecutionMode(executionMode) {
        const normalized = executionMode === 'serial' ? 'serial' : 'parallel';
        this.patientWsiExecutionMode = normalized;

        document.querySelectorAll('[data-patient-schedule]').forEach((button) => {
            const active = button.dataset.patientSchedule === normalized;
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
        });

        const description = document.getElementById('plScheduleDescription');
        if (description) {
            description.textContent = normalized === 'serial'
                ? window.__t?.('pl_schedule_description_serial')
                : window.__t?.('pl_schedule_description_parallel');
        }
    }

    setPatientLevelModeLocked(locked) {
        document.querySelectorAll('[data-patient-mode]').forEach((button) => {
            button.disabled = Boolean(locked);
        });
        document.getElementById('plModeSection')?.classList.toggle('locked', Boolean(locked));
        document.querySelectorAll('[data-patient-schedule]').forEach((button) => {
            button.disabled = Boolean(locked);
        });
        document.getElementById('plScheduleSection')?.classList.toggle('locked', Boolean(locked));
    }
    
    /**
     * 切换回文件管理面板
     */
    switchToFileManager() {
        const fileManager = document.getElementById('fileManagerPanel');
        const patientLevel = document.getElementById('patientLevelPanel');
        
        if (fileManager) fileManager.style.display = 'block';
        if (patientLevel) patientLevel.style.display = 'none';
        
        // 清理Patient Level状态
        this.pendingPatientLevelFiles = null;
        this.patientLevelUploadResults = [];
        this.setPatientLevelModeLocked(false);
        if (window.pathChatAgent) {
            window.pathChatAgent.isPatientLevelMode = false;
        }
    }
    
    /**
     * 更新Patient Level面板信息
     */
    updatePatientLevelPanelInfo() {
        const files = this.pendingPatientLevelFiles || [];
        const folderName = this.pendingFolderName || 'Patient';
        
        // 更新文件夹名称
        const folderNameEl = document.getElementById('plFolderName');
        if (folderNameEl) folderNameEl.textContent = folderName;
        
        // 更新切片数量
        const wsiCountEl = document.getElementById('plWsiCount');
        if (wsiCountEl) wsiCountEl.textContent = `${files.length} ${__t('pl_wsi_suffix')}`;
        
        // Folder name / slide count / list only. Switching the panel (including
        // ensurePatientProgressVisible during a live run) must not rewind the
        // header to "准备上传 0%".
        const runActive = Boolean(
            this.patientIsRunning || window.pathChatAgent?._activePatientRunToken
        );
        const alreadyUploaded = (this.patientLevelUploadResults || []).length > 0;
        if (!runActive && !alreadyUploaded) {
            this.updatePlProgress(0, __t('prepare_upload'));
        }
        
        // 渲染切片列表
        this.renderPatientLevelWsiList(files);
    }

    _formatPatientSlideMeta(file = {}) {
        const parts = [];
        const dims = file.dimensions;
        if (Array.isArray(dims) && dims.length >= 2) {
            const width = Number(dims[0]) || 0;
            const height = Number(dims[1]) || 0;
            if (width && height) {
                parts.push(`${Math.round(width / 1000)}k × ${Math.round(height / 1000)}k`);
            }
        }
        if (file.level_count) {
            const count = Number(file.level_count) || 0;
            const isEn = (window.__lang || 'zh') === 'en';
            if (count) {
                parts.push(isEn
                    ? `${count} ${count === 1 ? 'level' : 'levels'}`
                    : `${count} 层`);
            }
        }
        const size = Number(file.file_size || 0) || Number(file.size || 0);
        // File stubs used to reuse registered WSIs are 1 byte; hide that.
        if (size >= 1024) parts.push(this.formatFileSize(size));
        return parts.join(' · ');
    }

    _patientSlideFileId(index) {
        const n = Number(index);
        const uploaded = this.patientLevelUploadResults?.find((item) => Number(item.index) === n);
        if (uploaded?.file_id) return String(uploaded.file_id);
        const info = this.plAllFileInfos?.find((item) => Number(item.index) === n) || this.plAllFileInfos?.[n];
        return String(info?.file_id || '');
    }

    _applyPatientSlideThumb(index, options = {}) {
        const card = document.querySelector(`#plWsiList .pl-wsi-card[data-index="${index}"]`);
        const fileId = this._patientSlideFileId(index);
        if (!card || !fileId) return;
        const uploaded = this.patientLevelUploadResults?.find((item) => Number(item.index) === Number(index));
        const meta = this._patientFileMetaById?.[fileId] || uploaded;
        const thumbEl = card.querySelector('.wsi-thumb');
        if (!thumbEl) return;
        const thumbUrl = meta?.thumbnail_url || `/api/thumbnail/${fileId}`;
        const existing = thumbEl.querySelector('img');
        if (
            existing
            && !options.force
            && existing.dataset.fileId === fileId
            && existing.naturalWidth > 0
        ) {
            return;
        }
        const attempt = Number(options.attempt || 0);
        const image = document.createElement('img');
        image.alt = '';
        image.dataset.fileId = fileId;
        image.addEventListener('error', () => {
            if (attempt < 5) {
                window.setTimeout(
                    () => this._applyPatientSlideThumb(index, { force: true, attempt: attempt + 1 }),
                    500 * (attempt + 1)
                );
                return;
            }
            thumbEl.innerHTML = '<i class="fas fa-microscope"></i>';
        }, { once: true });
        image.src = attempt > 0
            ? `${thumbUrl}${thumbUrl.includes('?') ? '&' : '?'}retry=${attempt}`
            : thumbUrl;
        thumbEl.replaceChildren(image);
        const metaEl = card.querySelector('.wsi-meta-line');
        if (metaEl) {
            metaEl.textContent = this._formatPatientSlideMeta(meta);
        }
    }

    _applyAllPatientSlideThumbs() {
        const infos = this.plAllFileInfos || this.patientLevelUploadResults || [];
        const count = Math.max(infos.length, document.querySelectorAll('#plWsiList .pl-wsi-card').length);
        for (let index = 0; index < count; index += 1) {
            this._applyPatientSlideThumb(index);
        }
    }
    
    /**
     * 渲染 Patient Level 切片列表（与首页最近病例同一套缩略图行）
     */
    renderPatientLevelWsiList(files) {
        const listEl = document.getElementById('plWsiList');
        if (!listEl) return;
        this.patientSlideReviews = {};
        
        // 初始化切片分析状态存储
        this.plWsiAnalysisData = files.map((file, idx) => ({
            index: idx,
            filename: file.name,
            part: file.user_specimen_site || '',
            size: file.size,
            status: 'pending',
            redBoxes: [],
            expanded: false
        }));
        
        listEl.innerHTML = files.map((file, idx) => {
            const name = file.name || file.filename || '';
            const safeName = this.escapeHtml(name);
            const meta = this._formatPatientSlideMeta(file);
            
            return `
                <div class="pl-wsi-card" data-index="${idx}">
                    <div class="pl-wsi-header" data-index="${idx}">
                        <div class="wsi-thumb" aria-hidden="true">
                            <i class="fas fa-microscope"></i>
                        </div>
                        <div class="wsi-info">
                            <div class="wsi-name" title="${safeName}">${safeName}</div>
                            <small class="wsi-meta-line">${this.escapeHtml(meta)}</small>
                        </div>
                        <div class="wsi-status">
                            <div class="status-icon pending">
                                <i class="fas fa-clock"></i>
                            </div>
                        </div>
                    </div>
                    <div class="pl-wsi-body" data-index="${idx}" hidden>
                        <div class="analysis-tree"></div>
                        <div class="patient-slide-review-slot" data-index="${idx}"></div>
                    </div>
                </div>
            `;
        }).join('');
        
        // 绑定点击事件
        this.bindPlWsiItemClicks();
        this._reapplyPatientSlideRailStatuses();
        this._applyAllPatientSlideThumbs();
        this.syncPatientSlideReportNav();
    }
    
    /**
     * 绑定切片项点击事件
     */
    bindPlWsiItemClicks() {
        const headers = document.querySelectorAll('.pl-wsi-header');
        headers.forEach(header => {
            header.addEventListener('click', (e) => {
                if (e.target.closest('.wsi-rerun-btn')) {
                    e.stopPropagation();
                    return;
                }
                const index = parseInt(header.dataset.index);
                this.selectPlWsiItem(index);
            });
        });
        
        document.querySelectorAll('.wsi-rerun-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const index = parseInt(btn.dataset.index);
                this._rerunSingleWsi(index);
            });
        });
    }
    
    /**
     * 展开/收起切片详情
     * @param {number} index - WSI索引
     * @param {boolean} forceExpand - 是否强制展开（可选）
     */
    togglePlWsiExpand(_index, _forceExpand = null) {
        // The Patient Level case rail is a compact thumbnail list, matching
        // the home "recent cases" rows. Review and analysis trees stay in chat.
        return;
    }
    
    /**
     * 选中切片项并加载WSI
     */
    _usableOverlayRect(rect) {
        if (!rect || typeof rect !== 'object') return null;
        if (rect.x === undefined || rect.y === undefined) return null;
        return rect;
    }

    _flattenPatientAnalysisOverlays(analysisData) {
        const byId = new Map();
        const add = (id, rect, color, magnification) => {
            const usable = this._usableOverlayRect(rect);
            if (!id || !usable) return;
            byId.set(String(id), { id, rect: usable, color, magnification });
        };
        (analysisData?.redBoxes || []).forEach((redBox) => {
            add(redBox.id, redBox.rect, '#FF0000', 10);
            (redBox.blueBoxes || []).forEach((blueBox) => {
                add(blueBox.id, blueBox.rect, '#0066FF', 40);
                (blueBox.greenBoxes || []).forEach((greenBox) => {
                    add(greenBox.id, greenBox.rect, '#00CC00', 160);
                });
            });
        });
        const cached = Array.isArray(analysisData?.roiOverlays) ? analysisData.roiOverlays : [];
        cached.forEach((item) => {
            const data = item?.data || item;
            const mag = Number(data?.magnification) || 10;
            const color = data?.color || (mag >= 160 ? '#00CC00' : mag >= 40 ? '#0066FF' : '#FF0000');
            add(data?.id || item?.id, data?.rect, color, mag);
        });
        if (!byId.size && Array.isArray(analysisData?.trajectory) && analysisData.trajectory.length) {
            this._roamOverlaysFromTrajectory(analysisData.trajectory)
                .map((item) => item?.data || item)
                .forEach((data) => {
                    const mag = Number(data?.magnification) || 10;
                    const color = data?.color || (mag >= 160 ? '#00CC00' : mag >= 40 ? '#0066FF' : '#FF0000');
                    add(data?.id, data?.rect, color, mag);
                });
        }
        return [...byId.values()];
    }

    _persistViewingWsiOverlays(wsiIndex) {
        const agent = window.pathChatAgent;
        if (!agent?.isPatientLevelMode) return;
        const idx = Number(wsiIndex);
        if (!Number.isInteger(idx)) return;
        // Parallel: never copy the current canvas onto another slide.
        if (agent._isPatientParallelRun?.() && agent.currentViewingWsiIndex !== idx) return;
        const expectedFile = String(agent._fileIdForPatientWsi?.(idx) || '');
        const currentId = String(this.currentFile?.file_id || this.currentFile?.id || '');
        if (agent._isPatientParallelRun?.() && expectedFile && currentId && expectedFile !== currentId) return;
        const overlays = (this.roiOverlays || []).map((item) => {
            const data = item?.data || item;
            if (!item?.id || !this._usableOverlayRect(data?.rect)) return null;
            return { id: item.id, data: { ...data, id: item.id } };
        }).filter(Boolean);
        if (!overlays.length) return;
        if (!agent.patientLevelAnalysisData) agent.patientLevelAnalysisData = {};
        const current = agent.patientLevelAnalysisData[idx] || { redBoxes: [] };
        current.roiOverlays = overlays;
        agent.patientLevelAnalysisData[idx] = current;
    }

    selectPlWsiItem(index) {
        // 更新选中状态
        const cards = document.querySelectorAll('.pl-wsi-card');
        cards.forEach((card, i) => {
            card.classList.toggle('selected', i === index);
        });
        
        // ⭐⭐⭐ 关键修复：分离"查看"和"分析"两个概念 ⭐⭐⭐
        // - currentViewingWsiIndex: 用户当前正在查看的 WSI（由用户点击控制）
        // - currentAnalyzingWsiIndex: 后端当前正在分析的 WSI（只由 wsi_started 事件控制）
        // 
        // 修复前的 Bug：用户点击会修改 currentAnalyzingWsiIndex，导致后端发送的
        // 分析数据（红框/蓝框结果）被存储到错误的 WSI 数据结构中。
        if (window.pathChatAgent) {
            const previous = window.pathChatAgent.currentViewingWsiIndex;
            if (Number.isInteger(previous) && previous !== index) {
                this._persistViewingWsiOverlays(previous);
            }
            // ⭐ 只更新 currentViewingWsiIndex，不修改 currentAnalyzingWsiIndex！
            window.pathChatAgent.currentViewingWsiIndex = index;
            console.log(`[Patient Level] 👁️ 切换查看到 WSI index=${index} (正在分析的 WSI index=${window.pathChatAgent.currentAnalyzingWsiIndex})`);
            window.pathChatAgent._focusPatientActivityForWsi?.(index);
            this._schedulePatientSlideReportNavSync();
        }
        
        // 如果已上传，加载到查看器（跳过重复加载同一张 WSI）
        const uploadedFile = this.patientLevelUploadResults?.find(f => f.index === index);
        if (uploadedFile && uploadedFile.file_id) {
            // ⭐ 检查当前查看器是否已加载同一张 WSI，避免重复加载（节省 DZI + 瓦片请求）
            if (this.currentFile && this.currentFile.file_id === uploadedFile.file_id) {
                console.log(`[Patient Level] WSI ${uploadedFile.file_id} 已在查看器中，跳过重新加载`);
                this.restoreWsiOverlays(index);
            } else {
                // Never block the whole bench with "正在加载WSI" while a
                // patient run is in flight — generate_dzi/files can wait
                // behind a busy Agent loop and freeze the overlay.
                this.loadWSI(uploadedFile.file_id, {
                    silent: true,
                    fileInfo: uploadedFile,
                }).then(() => {
                    this.restoreWsiOverlays(index);
                });
            }
        }
    }
    
    /**
     * ⭐ 恢复指定WSI的overlay（切换WSI时调用）
     */
    restoreWsiOverlays(wsiIndex) {
        if (!window.pathChatAgent) return;
        const agent = window.pathChatAgent;

        // Incremental syncROIOverlays is for live parallel writes on one
        // slide. Switching the viewing slide must replace the canvas, or
        // leftover R8/R7 boxes from the previous WSI stay on screen.
        this.clearAllROIOverlays();

        let overlays = [];
        const analysisData = agent.patientLevelAnalysisData?.[wsiIndex];
        if (analysisData) {
            overlays = this._flattenPatientAnalysisOverlays(analysisData);
        }

        // Single Agent roam stores its boxes in the roam event store, which is
        // never mirrored into patientLevelAnalysisData during a live run. If we
        // don't pull from there, the just-drawn roam boxes get wiped by the
        // clear above and the slide comes back blank ("没有 map"). Scoped to this
        // slide's file so no other slide's coordinates bleed in.
        if (!overlays.length && agent.activeAnalysisStrategy === 'single_agent_roam') {
            overlays = agent.roamOverlaysForWsi?.(wsiIndex) || [];
        }

        if (!overlays.length) {
            console.log(`[Patient Level] WSI index=${wsiIndex} 没有可恢复的 overlay`);
            return;
        }
        console.log(`[Patient Level] 恢复WSI index=${wsiIndex} 的 ${overlays.length} 个overlay`);
        this.syncROIOverlays(overlays);
    }
    
    /**
     * 更新切片的分析树（红框、蓝框等）
     * ⭐ 显示模型的分析输出
     */
    updatePlWsiAnalysisTree(index, analysisData, overrideTreeEl = null) {
        let treeEl;
        if (overrideTreeEl) {
            treeEl = overrideTreeEl;
        } else {
            const body = document.querySelector(`.pl-wsi-body[data-index="${index}"]`);
            if (!body) return;
            treeEl = body.querySelector('.analysis-tree');
            if (!treeEl) return;
        }
        // Single Agent roam has no Subagent R/B/G tree. Painting Reason +
        // "正在分析红框" here is what made the left rail look highlighted
        // after a JSON reload.
        if (window.pathChatAgent?.activeAnalysisStrategy === 'single_agent_roam') {
            treeEl.innerHTML = '';
            return;
        }
        
        // ⭐ 在重新渲染前保存当前的展开/收起状态
        const expandedStates = {};
        treeEl.querySelectorAll('.tree-branch').forEach(branch => {
            const toggleIcon = branch.querySelector('.toggle-icon');
            if (toggleIcon) {
                const toggleId = toggleIcon.dataset.toggle;
                // 检查是否折叠（图标是右箭头表示折叠）
                expandedStates[toggleId] = toggleIcon.classList.contains('fa-chevron-down');
            }
        });
        
        // ⭐⭐⭐ 新增：保存当前高亮状态（解决高亮消失问题）⭐⭐⭐
        let highlightedRoiId = null;
        const highlightedNode = treeEl.querySelector('.roi-selected-glow[data-roi-id], .clickable-roi.active[data-roi-id]');
        if (highlightedNode) {
            highlightedRoiId = highlightedNode.dataset.roiId;
            console.log(`[updatePlWsiAnalysisTree] 保存高亮状态: ${highlightedRoiId}`);
        }
        
        // 更新存储的数据
        if (!this.plWsiAnalysisData) {
            this.plWsiAnalysisData = {};
        }
        this.plWsiAnalysisData[index] = analysisData;
        
        // ⭐⭐⭐ 核心修复：即使红框为空，只要有 globalReasoning，就应该显示它
        // 解决问题：AI 全局分析完成后，红框列表可能还没到达，但全局分析理由应该先显示
        if (!analysisData.redBoxes || analysisData.redBoxes.length === 0) {
            // 检查是否有全局分析理由
            if (analysisData.globalReasoning) {
                // 有全局分析理由，显示它 + 等待红框的提示
                treeEl.innerHTML = `
                    <div class="tree-branch global-reasoning-branch expanded" style="margin-bottom:4px;">
                        <div class="tree-item global-reasoning-item" style="font-weight:600; color:#af52de; background:rgba(175,82,222,0.08); border-radius:4px; padding:4px 6px; font-size:11px;">
                            <i class="fas fa-chevron-down toggle-icon" data-toggle="global-reasoning"></i>
                            <i class="fas fa-brain" style="color:#af52de; font-size:10px;"></i>
                            <span class="box-label">AI ${__t('section_global_obs')} 【Reason】</span>
                        </div>
                        <div class="tree-children" data-parent="global-reasoning">
                            <div class="box-observation red-observation" style="border-left-color:#af52de; white-space:pre-wrap; margin:2px 0 2px 0; padding:4px 6px;">${analysisData.globalReasoning}</div>
                        </div>
                    </div>
                    <div class="analysis-empty" style="margin-top:8px;">
                        <i class="fas fa-spinner fa-spin"></i>
                        <span>正在分析红框区域...</span>
                    </div>
                `;
                // 绑定展开/收起事件
                this.bindPlWsiTreeClickEvents(treeEl);
                console.log('[updatePlWsiAnalysisTree] 显示 globalReasoning + 等待红框');
            } else {
                // 没有任何数据，显示等待状态
                treeEl.innerHTML = `
                    <div class="analysis-empty">
                        <i class="fas fa-spinner fa-spin"></i>
                        <span>${__t('observation_analyzing')}</span>
                    </div>
                `;
            }
            return;
        }
        
        // ⭐ 辅助函数：从ID中提取显示名称
        // 例如 "roi_10x_r2" -> "R2", "R2-B5-G1" -> "R2-B5-G1"
        const extractDisplayName = (id, fallback) => {
            if (!id) return fallback;
            
            // 如果ID已经是标准格式 (R1, R1-B2, R1-B2-G3)，直接使用
            if (/^R\d+(-B\d+)?(-G\d+)?$/i.test(id)) {
                return id.toUpperCase();
            }
            
            // 尝试从 roi_10x_r2 格式提取
            const roiMatch = id.match(/roi_\d+x_r(\d+)/i);
            if (roiMatch) {
                return `R${roiMatch[1]}`;
            }
            
            // 尝试从 R2_sub_r1_c2 格式提取 (蓝框)
            const blueMatch = id.match(/R(\d+)[-_]sub[-_]r(\d+)[-_]c(\d+)/i);
            if (blueMatch) {
                return `R${blueMatch[1]}-B${(parseInt(blueMatch[2]) - 1) * 4 + parseInt(blueMatch[3]) + 1}`;
            }
            
            // 尝试从 R2-B5 或类似格式提取
            const simpleMatch = id.match(/(R\d+(?:-B\d+)?(?:-G\d+)?)/i);
            if (simpleMatch) {
                return simpleMatch[1].toUpperCase();
            }
            
            return fallback;
        };
        
        // ⭐ 渲染全局红框选择理由（AI 全局观察，类似单张自动模式的顶部展示）
        let globalReasoningHtml = '';
        if (analysisData.globalReasoning) {
            const globalToggleId = 'global-reasoning';
            // 默认展开，让用户第一时间看到 AI 的全局分析
            const globalExpanded = expandedStates[globalToggleId] !== undefined ? expandedStates[globalToggleId] : true;
            globalReasoningHtml = `
                <div class="tree-branch global-reasoning-branch ${globalExpanded ? 'expanded' : 'collapsed'}" style="margin-bottom:4px;">
                    <div class="tree-item global-reasoning-item" style="font-weight:600; color:#af52de; background:rgba(175,82,222,0.08); border-radius:4px; padding:4px 6px; font-size:11px;">
                        <i class="fas fa-chevron-${globalExpanded ? 'down' : 'right'} toggle-icon" data-toggle="${globalToggleId}"></i>
                        <i class="fas fa-brain" style="color:#af52de; font-size:10px;"></i>
                        <span class="box-label">AI ${__t('section_global_obs')} 【Reason】</span>
                    </div>
                    <div class="tree-children" data-parent="${globalToggleId}" style="${globalExpanded ? '' : 'display: none;'}">
                        <div class="box-observation red-observation" style="border-left-color:#af52de; white-space:pre-wrap; margin:2px 0 2px 0; padding:4px 6px;">${analysisData.globalReasoning}</div>
                    </div>
                </div>
            `;
        }
        
        // ⭐ 渲染红框树，包含模型输出和坐标数据
        // 使用后端传递的实际ID作为名称
        treeEl.innerHTML = globalReasoningHtml + analysisData.redBoxes.map((redBox, rIdx) => {
            // ⭐ 使用实际ID作为名称，而非重新编号
            const redName = extractDisplayName(redBox.id, `R${rIdx + 1}`);
            const redToggleId = `red-${rIdx}`;
            
            // ⭐ 恢复之前保存的展开状态（默认展开）
            const redExpanded = expandedStates[redToggleId] !== undefined ? expandedStates[redToggleId] : true;
            
            // ⭐ 参考单张WSI自动模式：完整显示观察结果，不截断
            const observationSummary = redBox.observation || __t('observation_analyzing');
            
            // 红框坐标 JSON
            const redRectJson = redBox.rect ? JSON.stringify(redBox.rect).replace(/"/g, '&quot;') : '';
            
            // 蓝框列表
            const blueBoxesHtml = (redBox.blueBoxes || []).map((blueBox, bIdx) => {
                // ⭐ 使用实际ID作为名称，而非重新编号
                const blueName = extractDisplayName(blueBox.id, `${redName}-B${bIdx + 1}`);
                const blueToggleId = `blue-${rIdx}-${bIdx}`;
                
                // ⭐ 恢复之前保存的展开状态（默认展开，参考单张WSI自动模式）
                const blueExpanded = expandedStates[blueToggleId] !== undefined ? expandedStates[blueToggleId] : true;
                
                // ⭐ 参考单张WSI自动模式：完整显示蓝框解读
                // 优先使用 observation，如果为空则使用 reasoning，最后使用默认提示
                let blueObservation = blueBox.observation || blueBox.reasoning || '';
                const hasGreen = blueBox.greenBoxes && blueBox.greenBoxes.length > 0;
                
                // ⭐ 如果蓝框没有观察结果，根据状态显示不同的提示
                if (!blueObservation) {
                    const isEn = (window.__lang || 'zh') === 'en';
                    if (blueBox.status === 'completed') {
                        blueObservation = hasGreen 
                            ? (isEn ? `Selected ${blueBox.greenBoxes.length} green box(es) for 160× high-power analysis` 
                                    : `已选择 ${blueBox.greenBoxes.length} 个绿框进行160×高倍镜分析`)
                            : (isEn ? '✅ Analysis complete, no further magnification needed'
                                    : '✅ 分析完成，该区域无需进一步放大至160×高倍镜');
                    } else if (blueBox.status === 'analyzing') {
                        blueObservation = hasGreen
                            ? (isEn ? `🔄 Analyzing ${blueBox.greenBoxes.length} green box(es)...`
                                    : `🔄 正在分析 ${blueBox.greenBoxes.length} 个绿框...`)
                            : (isEn ? '🔄 Analyzing this region...'
                                    : '🔄 正在分析该区域...');
                    } else {
                        // pending 或其他状态
                        blueObservation = '⏳ 等待分析...';
                    }
                }
                
                // 蓝框坐标 JSON
                const blueRectJson = blueBox.rect ? JSON.stringify(blueBox.rect).replace(/"/g, '&quot;') : '';
                
                // 绿框列表
                const greenBoxesHtml = (blueBox.greenBoxes || []).map((greenBox, gIdx) => {
                    // ⭐ 使用实际ID作为名称，而非重新编号
                    const greenName = extractDisplayName(greenBox.id, `${blueName}-G${gIdx + 1}`);
                    const greenRectJson = greenBox.rect ? JSON.stringify(greenBox.rect).replace(/"/g, '&quot;') : '';
                    
                    // ⭐ 参考单张WSI自动模式：完整显示绿框解读，不截断
                    const greenObservation = greenBox.observation || '';
                    
                    return `
                    <div class="tree-branch green-box-branch">
                        <div class="tree-item green-box-item clickable-roi" data-wsi="${index}" data-red="${rIdx}" data-blue="${bIdx}" data-green="${gIdx}" data-rect="${greenRectJson}" data-roi-id="${greenBox.id || ''}">
                            <span class="toggle-placeholder"></span>
                            <i class="fas fa-square" style="color: #34c759; font-size: 8px;"></i>
                            <span class="box-label">${greenName}</span>
                            <span class="box-status ${greenBox.status || 'pending'}">
                                ${greenBox.status === 'completed' ? '✓' : ''}
                            </span>
                        </div>
                        ${greenObservation ? `<div class="box-observation green-observation">${greenObservation}</div>` : ''}
                    </div>
                `;
                }).join('');
                
                // 是否有绿框子节点
                const hasGreenBoxes = blueBox.greenBoxes && blueBox.greenBoxes.length > 0;
                const greenCount = hasGreenBoxes ? blueBox.greenBoxes.length : 0;
                
                return `
                    <div class="tree-branch blue-box-branch ${blueExpanded ? 'expanded' : 'collapsed'}">
                        <div class="tree-item blue-box-item clickable-roi" data-wsi="${index}" data-red="${rIdx}" data-blue="${bIdx}" data-rect="${blueRectJson}" data-roi-id="${blueBox.id || ''}">
                            ${hasGreenBoxes ? `<i class="fas fa-chevron-${blueExpanded ? 'down' : 'right'} toggle-icon" data-toggle="${blueToggleId}"></i>` : '<span class="toggle-placeholder"></span>'}
                            <i class="fas fa-square" style="color: #007aff; font-size: 10px;"></i>
                            <span class="box-label">${blueName}</span>
                            ${hasGreenBoxes ? `<span class="box-count">${greenCount} ${__t('green_box')}</span>` : ''}
                            <span class="box-status ${blueBox.status || 'pending'}">
                                ${blueBox.status === 'completed' ? '✓' : blueBox.status === 'analyzing' ? '...' : ''}
                            </span>
                        </div>
                        ${blueObservation ? `<div class="box-observation blue-observation">${blueObservation}</div>` : ''}
                        ${greenBoxesHtml ? `<div class="tree-children green-children" data-parent="${blueToggleId}" style="${blueExpanded ? '' : 'display: none;'}">${greenBoxesHtml}</div>` : ''}
                    </div>
                `;
            }).join('');
            
            // 是否有蓝框子节点
            const hasBlueBoxes = redBox.blueBoxes && redBox.blueBoxes.length > 0;
            const blueCount = hasBlueBoxes ? redBox.blueBoxes.length : 0;
            
            return `
                <div class="tree-branch red-box-branch ${redExpanded ? 'expanded' : 'collapsed'}">
                    <div class="tree-item red-box-item clickable-roi" data-wsi="${index}" data-red="${rIdx}" data-rect="${redRectJson}" data-roi-id="${redBox.id || ''}">
                        ${hasBlueBoxes ? `<i class="fas fa-chevron-${redExpanded ? 'down' : 'right'} toggle-icon" data-toggle="${redToggleId}"></i>` : '<span class="toggle-placeholder"></span>'}
                        <i class="fas fa-square" style="color: #ff3b30;"></i>
                        <span class="box-label">${redName}</span>
                        <span class="box-count">${blueCount} ${__t('blue_box')}</span>
                        <span class="box-status ${redBox.status || 'pending'}">
                            ${redBox.status === 'completed' ? '✓' : ''}
                        </span>
                    </div>
                    <div class="box-observation red-observation">${observationSummary}</div>
                    <div class="tree-children blue-children" data-parent="${redToggleId}" style="${redExpanded ? '' : 'display: none;'}">
                        ${blueBoxesHtml}
                    </div>
                </div>
            `;
        }).join('');
        
        // ⭐ 绑定点击事件（跳转到对应 ROI 区域）
        this.bindPlWsiTreeClickEvents(treeEl);
        
        // ⭐⭐⭐ 新增：恢复高亮状态（解决高亮消失问题）⭐⭐⭐
        if (highlightedRoiId) {
            const nodeToHighlight = treeEl.querySelector(`.clickable-roi[data-roi-id="${highlightedRoiId}"]`);
            if (nodeToHighlight) {
                nodeToHighlight.classList.add('roi-selected-glow', 'active');
                const branch = nodeToHighlight.closest('.tree-branch');
                if (branch) {
                    branch.classList.add('roi-selected-glow');
                    const observation = branch.querySelector('.box-observation');
                    if (observation) {
                        observation.classList.add('roi-selected-glow');
                    }
                }
                console.log(`[updatePlWsiAnalysisTree] 恢复高亮状态: ${highlightedRoiId}`);
            }
        }
    }
    
    /**
     * ⭐ 绑定 Patient Level 分析树的点击事件
     */
    bindPlWsiTreeClickEvents(treeEl) {
        if (!treeEl || typeof treeEl.querySelectorAll !== 'function') return;
        // 绑定收起/展开图标点击事件
        const toggleIcons = treeEl.querySelectorAll('.toggle-icon');
        toggleIcons.forEach(icon => {
            icon.addEventListener('click', (e) => {
                e.stopPropagation();
                const toggleId = icon.dataset.toggle;
                const childrenEl = treeEl.querySelector(`.tree-children[data-parent="${toggleId}"]`);
                const branchEl = icon.closest('.tree-branch');
                
                if (childrenEl) {
                    const isExpanded = childrenEl.style.display !== 'none';
                    childrenEl.style.display = isExpanded ? 'none' : '';
                    icon.classList.toggle('fa-chevron-down', !isExpanded);
                    icon.classList.toggle('fa-chevron-right', isExpanded);
                    branchEl.classList.toggle('expanded', !isExpanded);
                    branchEl.classList.toggle('collapsed', isExpanded);
                }
            });
        });
        
        // 绑定 ROI 点击跳转事件
        const clickableItems = treeEl.querySelectorAll('.clickable-roi');
        clickableItems.forEach(item => {
            item.addEventListener('click', (e) => {
                // 如果点击的是展开/收起图标或其子元素，不执行跳转
                if (e.target.classList.contains('toggle-icon') || 
                    e.target.closest('.toggle-icon') ||
                    e.target.classList.contains('fa-chevron-down') ||
                    e.target.classList.contains('fa-chevron-right')) {
                    return;
                }
                
                e.stopPropagation();
                
                const rectJson = item.dataset.rect;
                const roiId = item.dataset.roiId;
                
                if (!rectJson || rectJson === '') {
                    console.log('⚠️ 该节点没有坐标数据');
                    return;
                }
                
                try {
                    const rect = JSON.parse(rectJson);
                    console.log('📍 点击跳转到 ROI:', roiId, rect);
                    
                    // 调用跳转函数
                    this.jumpToROIView({ id: roiId, rect: rect });
                    
                    // ⭐ 参考单张WSI自动模式：添加黄色高亮效果
                    // 清除所有高亮
                    treeEl.querySelectorAll('.clickable-roi').forEach(el => {
                        el.classList.remove('active', 'roi-selected-glow');
                    });
                    treeEl.querySelectorAll('.tree-branch').forEach(el => {
                        el.classList.remove('roi-selected-glow');
                    });
                    treeEl.querySelectorAll('.box-observation').forEach(el => {
                        el.classList.remove('roi-selected-glow');
                    });
                    
                    // 添加高亮到当前节点
                    item.classList.add('active', 'roi-selected-glow');
                    
                    // 高亮整个分支和观察结果
                    const branch = item.closest('.tree-branch');
                    if (branch) {
                        branch.classList.add('roi-selected-glow');
                        // 高亮相邻的观察结果
                        const observation = branch.querySelector('.box-observation');
                        if (observation) {
                            observation.classList.add('roi-selected-glow');
                        }
                    }
                    
                    // ⭐ 高亮WSI上对应的框
                    if (roiId) {
                        this.highlightWSIOverlay(roiId);
                    }
                } catch (err) {
                    console.error('解析坐标失败:', err);
                }
            });
        });
    }
    
    /**
     * ⭐ 高亮WSI上的对应框（点击左侧树节点时调用）
     */
    highlightWSIOverlay(roiId) {
        if (!roiId || !this.viewer) return;
        
        // 获取 viewer 上的 overlay，不要命中中间栏树节点
        const allOverlays = document.querySelectorAll('.roi-overlay[data-roi-id]');
        allOverlays.forEach(overlay => {
            overlay.classList.remove('roi-selected-glow', 'overlay-highlight');
        });
        
        // 高亮匹配的overlay
        const escaped = (window.CSS && typeof window.CSS.escape === 'function')
            ? CSS.escape(roiId)
            : String(roiId).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
        const targetOverlay = document.querySelector(`.roi-overlay[data-roi-id="${escaped}"]`);
        if (targetOverlay) {
            targetOverlay.classList.add('roi-selected-glow', 'overlay-highlight');
        }
    }
    
    /**
     * 更新Patient Level面板中的进度
     */
    updatePlProgress(percentage, statusText) {
        const progressFill = document.getElementById('plProgressFill');
        const progressPercent = document.getElementById('plProgressPercent');
        const progressStatus = document.getElementById('plProgressStatus');
        const headerStatus = document.getElementById('plStatus');
        
        if (progressFill) progressFill.style.width = `${percentage}%`;
        if (progressPercent) progressPercent.textContent = `${Math.round(percentage)}%`;
        if (progressStatus) progressStatus.textContent = statusText;
        if (headerStatus && statusText) headerStatus.textContent = statusText;
    }
    
    /**
     * 更新Patient Level面板中的WSI状态
     */
    updatePlWsiStatus(index, status, icon) {
        const remembered = this.patientSlideNavStates?.[index];
        if (
            (status === 'pending' || status === 'uploaded')
            && remembered
            && !['pending', 'uploaded'].includes(remembered)
        ) {
            this._applyPatientSlideThumb(index);
            this._schedulePatientSlideReportNavSync();
            return;
        }
        if (
            status === 'analyzing'
            && ['final', 'confirmed', 'review', 'completed'].includes(remembered)
        ) {
            this._applyPatientSlideThumb(index);
            this._schedulePatientSlideReportNavSync();
            return;
        }
        this._rememberPatientSlideNavState(index, status);
        // 支持新的卡片结构
        const card = document.querySelector(`.pl-wsi-card[data-index="${index}"]`);
        const header = document.querySelector(`.pl-wsi-header[data-index="${index}"]`);
        
        if (header) {
            const statusIcon = header.querySelector('.status-icon');
            if (statusIcon) {
                statusIcon.className = `status-icon ${status}`;
                statusIcon.innerHTML = `<i class="fas fa-${icon}"></i>`;
            }
            this._applyPatientSlideThumb(index);
        }
        
        // 更新卡片样式
        if (card) {
            card.classList.remove('analyzing', 'completed', 'review', 'confirmed', 'error', 'stopped');
            if (status === 'analyzing') {
                card.classList.add('analyzing');
            }
            if (status === 'completed') {
                card.classList.add('completed');
                const rerunBtn = card.querySelector('.wsi-rerun-btn');
                if (rerunBtn) rerunBtn.style.display = '';
            }
            if (status === 'review') card.classList.add('review');
            if (status === 'confirmed') card.classList.add('confirmed');
            if (status === 'error') card.classList.add('error');
            if (status === 'stopped') card.classList.add('stopped');
        }
        
        // 兼容旧结构
        const oldItem = document.querySelector(`.pl-wsi-item[data-index="${index}"]`);
        if (oldItem) {
            const statusIcon = oldItem.querySelector('.status-icon');
            if (statusIcon) {
                statusIcon.className = `status-icon ${status}`;
                statusIcon.innerHTML = `<i class="fas fa-${icon}"></i>`;
            }
            oldItem.classList.remove('analyzing');
            if (status === 'analyzing') {
                oldItem.classList.add('analyzing');
            }
        }
        
        // 更新内部状态
        if (this.plWsiAnalysisData && this.plWsiAnalysisData[index]) {
            this.plWsiAnalysisData[index].status = status;
        }
        this._schedulePatientSlideReportNavSync();
    }

    /**
     * Render the mandatory per-slide review gate used only by Patient Level
     * collaborative mode. A generated WSI report is always a draft until a
     * pathologist explicitly confirms it.
     */
    renderPatientSlideReview(payload) {
        const review = payload?.review || payload;
        if (!review || review.index === undefined || review.index === null) return;
        const index = Number(review.index);
        const chatHost = this._ensurePatientReviewChatHost(index, review.file_id);
        if (!chatHost) return;
        const preservedFeedback = chatHost.querySelector('.patient-review-feedback')?.value || '';

        if (!this.patientSlideReviews) this.patientSlideReviews = {};
        this.patientSlideReviews[review.file_id] = review;
        const isEn = (window.__lang || 'zh') === 'en';
        const statusMap = {
            pending_review: isEn ? 'Needs review' : '待医生复核',
            revising: isEn ? 'Revising' : '正在按反馈修订',
            revised_pending_confirmation: isEn ? 'Revised · confirm again' : '已修订 · 请再次确认',
            confirmed: isEn ? 'Confirmed' : '已确认',
            revision_failed: isEn ? 'Revision failed' : '修订失败',
            error: isEn ? 'Analysis failed' : '分析失败'
        };
        const statusLabel = statusMap[review.status] || review.status || '';
        const report = this.escapeHtml(review.current_report || '');
        const error = this.escapeHtml(review.error || '');
        const feedbackDisabled = review.status === 'revising';
        const confirmDisabled = !['pending_review', 'revised_pending_confirmation'].includes(review.status);
        const renderKey = JSON.stringify([
            review.status || '',
            Number(review.review_version || 1),
            review.source || '',
            review.current_report || '',
            review.error || ''
        ]);
        if (
            chatHost.dataset.patientReviewRenderKey === renderKey
            && chatHost.querySelector('.patient-slide-review-card')
        ) {
            return;
        }
        chatHost.dataset.patientReviewRenderKey = renderKey;

        chatHost.innerHTML = `
            <section class="patient-slide-review-card status-${review.status || 'pending'}"
                     data-file-id="${this.escapeHtml(review.file_id || '')}"
                     data-run-token="${this.escapeHtml(review.run_token || '')}">
                <div class="patient-review-heading">
                    <div>
                        <span class="patient-review-kicker">${isEn ? 'PATHOLOGIST REVIEW' : '病理医生复核'}</span>
                        <strong>${statusLabel}</strong>
                    </div>
                    <span class="patient-review-version">v${Number(review.review_version || 1)}</span>
                </div>
                ${error ? `<div class="patient-review-error">${error}</div>` : ''}
                <details class="patient-review-report" ${review.status === 'revised_pending_confirmation' ? 'open' : ''}>
                    <summary>${isEn ? 'Review report' : '查看复核报告'}</summary>
                    <div class="patient-review-report-text">${report || (isEn ? 'No report available.' : '暂无报告。')}</div>
                </details>
                ${review.source === 'cache' ? `
                    <div class="patient-review-cache-note">
                        <i class="fas fa-history"></i>
                        ${isEn ? 'Loaded from cache; confirmation is still required.' : '该结果来自缓存，仍需医生重新确认。'}
                    </div>` : ''}
                <label class="patient-review-feedback-label" for="patientReviewFeedback-${index}">
                    ${isEn ? 'Feedback / correction' : '反馈或纠正意见'}
                </label>
                <textarea id="patientReviewFeedback-${index}" class="patient-review-feedback"
                          rows="3" ${feedbackDisabled ? 'disabled' : ''}
                          placeholder="${isEn ? 'e.g. This focus is not tumor; revise the conclusion…' : '例如：该区域并非肿瘤，请据此修订结论……'}">${this.escapeHtml(preservedFeedback)}</textarea>
                <div class="patient-review-actions">
                    <button type="button" class="patient-review-btn secondary"
                            data-review-action="revise" data-index="${index}"
                            data-file-id="${this.escapeHtml(review.file_id || '')}"
                            ${feedbackDisabled ? 'disabled' : ''}>
                        <i class="fas fa-pen"></i>${isEn ? 'Revise from feedback' : '按反馈修订'}
                    </button>
                    <button type="button" class="patient-review-btn secondary"
                            data-review-action="add_roi" data-index="${index}"
                            data-file-id="${this.escapeHtml(review.file_id || '')}"
                            ${feedbackDisabled ? 'disabled' : ''}>
                        <i class="fas fa-crop-alt"></i>${isEn ? 'Analyze selected ROI' : '分析新增 ROI'}
                    </button>
                    <button type="button" class="patient-review-btn primary"
                            data-review-action="confirm" data-index="${index}"
                            data-file-id="${this.escapeHtml(review.file_id || '')}"
                            ${confirmDisabled ? 'disabled' : ''}>
                        <i class="fas fa-check"></i>${isEn ? 'Confirm slide' : '确认该切片'}
                    </button>
                </div>
                ${review.status === 'confirmed' ? `
                    <div class="patient-review-confirmed-note">
                        <i class="fas fa-check-circle"></i>
                        ${isEn ? 'This reviewed report will be used by the patient-level Orchestrator.' : '患者级 Orchestrator 将使用这份已确认报告。'}
                    </div>` : ''}
            </section>
        `;
        if (review.status === 'confirmed') {
            this.updatePlWsiStatus(index, 'confirmed', 'check');
        } else if (review.status === 'revising') {
            this.updatePlWsiStatus(index, 'analyzing', 'spinner fa-spin');
        } else if (review.status === 'error' || review.status === 'revision_failed') {
            this.updatePlWsiStatus(index, 'error', 'exclamation-triangle');
        } else {
            this.updatePlWsiStatus(index, 'review', 'user-check');
            this.revealPatientSlideReview(index);
        }
        window.pathChatAgent?._syncPatientChatGatesVisibility?.(index);
        this.syncPatientSlideReportNav();
    }

    _ensurePatientReviewChatHost(index, fileId) {
        const hostId = `patientReviewChat-${index}`;
        let host = document.getElementById(hostId);
        if (host) return host;
        const chat = this.elements?.chatMessages || document.getElementById('chatMessages');
        if (!chat) return null;
        const wrap = document.createElement('div');
        wrap.className = 'message assistant-message patient-review-chat-wrap';
        wrap.dataset.patientReviewIndex = String(index);
        wrap.dataset.wsiIndex = String(index);
        wrap.dataset.fileId = String(fileId || '');
        wrap.innerHTML = `<div class="message-content" id="${hostId}"></div>`;
        chat.appendChild(wrap);
        window.pathChatAgent?._syncPatientChatGatesVisibility?.();
        this.scrollToBottom?.();
        return wrap.querySelector('.message-content');
    }

    revealPatientSlideReview(index) {
        const host = document.getElementById(`patientReviewChat-${index}`);
        const target = host?.closest('.patient-review-chat-wrap') || host;
        target?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }

    _isPatientLevelUiActive() {
        if (window.pathChatAgent?.isPatientLevelMode) return true;
        const panel = document.getElementById('patientLevelPanel');
        return Boolean(panel && panel.style.display && panel.style.display !== 'none');
    }

    _schedulePatientSlideReportNavSync() {
        if (this._patientSlideNavSyncTimer) {
            cancelAnimationFrame(this._patientSlideNavSyncTimer);
        }
        this._patientSlideNavSyncTimer = requestAnimationFrame(() => {
            this._patientSlideNavSyncTimer = null;
            this.syncPatientSlideReportNav();
        });
    }

    _ensurePatientChatPin() {
        const chat = this.elements?.chatMessages || document.getElementById('chatMessages');
        if (!chat) return null;
        let pin = document.getElementById('patientChatPin');
        if (!pin) {
            pin = document.createElement('div');
            pin.id = 'patientChatPin';
            pin.className = 'patient-chat-pin';
            chat.prepend(pin);
        } else if (pin.parentElement !== chat) {
            chat.prepend(pin);
        }
        const board = document.getElementById('v2SubagentBoard');
        if (board && board.parentElement !== pin) {
            pin.appendChild(board);
        }
        return pin;
    }

    _ensurePatientSlideReportNav() {
        const pin = this._ensurePatientChatPin();
        if (!pin) return null;
        let nav = document.getElementById('patientSlideReportNav');
        if (!nav) {
            nav = document.createElement('nav');
            nav.id = 'patientSlideReportNav';
            nav.className = 'patient-slide-report-nav';
            nav.hidden = true;
            nav.setAttribute('aria-label', window.__t?.('pl_slide_report_nav') || '单张报告');
            pin.prepend(nav);
            nav.addEventListener('click', (event) => {
                const chip = event.target.closest('.patient-slide-report-item, .patient-slide-report-chip');
                if (!chip || !nav.contains(chip)) return;
                if (chip.dataset.kind === 'composite') {
                    this.jumpToPatientCompositeReport();
                    return;
                }
                const index = Number(chip.dataset.index);
                if (Number.isInteger(index)) this.jumpToPatientSlideReport(index);
            });
        } else if (nav.parentElement !== pin) {
            pin.prepend(nav);
        }
        return nav;
    }

    _patientSlideIndexFromFileId(fileId) {
        const id = String(fileId || '').trim();
        if (!id) return null;
        const infos = this.plAllFileInfos || this.patientLevelUploadResults || [];
        const found = infos.find((info) => String(info?.file_id || '') === id);
        if (found && Number.isInteger(found.index)) return found.index;
        const idx = infos.findIndex((info) => String(info?.file_id || '') === id);
        return idx >= 0 ? idx : null;
    }

    _rememberPatientSlideNavState(index, status) {
        if (!Number.isInteger(index)) return;
        if (!this.patientSlideNavStates) this.patientSlideNavStates = {};
        const mapped = {
            analyzing: 'analyzing',
            review: 'review',
            confirmed: 'final',
            completed: 'completed',
            error: 'error',
            stopped: 'stopped',
            revising: 'revising',
            pending: 'pending',
            uploaded: 'pending'
        }[status] || status;
        this.patientSlideNavStates[index] = mapped;
    }

    _reapplyPatientSlideRailStatuses() {
        const states = this.patientSlideNavStates || {};
        Object.entries(states).forEach(([idx, state]) => {
            const index = Number(idx);
            const card = document.querySelector(`.pl-wsi-card[data-index="${index}"]`);
            const header = document.querySelector(`.pl-wsi-header[data-index="${index}"]`);
            if (!card) return;
            card.classList.remove('analyzing', 'completed', 'review', 'confirmed', 'error', 'stopped');
            const rail = state === 'final' ? 'confirmed'
                : state === 'completed' ? 'completed'
                : state === 'review' || state === 'revising' ? 'review'
                : state === 'analyzing' ? 'analyzing'
                : state === 'error' ? 'error'
                : state === 'stopped' ? 'stopped'
                : '';
            if (rail) card.classList.add(rail);
            const icon = header?.querySelector('.status-icon');
            if (!icon) return;
            const iconName = rail === 'analyzing' ? 'spinner fa-spin'
                : rail === 'confirmed' || rail === 'completed' ? 'check'
                : rail === 'review' ? 'user-check'
                : rail === 'error' ? 'times'
                : rail === 'stopped' ? 'stop'
                : 'clock';
            icon.className = `status-icon ${rail || 'pending'}`;
            icon.innerHTML = `<i class="fas fa-${iconName}"></i>`;
            this._applyPatientSlideThumb(index);
        });
        this._applyAllPatientSlideThumbs();
    }

    markPatientSlidesForRunStart() {
        const infos = this.plAllFileInfos || this.patientLevelUploadResults || [];
        const parallel = this.patientWsiExecutionMode === 'parallel';
        infos.forEach((info, i) => {
            const index = Number.isInteger(info?.index) ? info.index : i;
            const remembered = this.patientSlideNavStates?.[index];
            if (['final', 'review', 'completed', 'error', 'revising'].includes(remembered)) return;
            const review = this.patientSlideReviews?.[info?.file_id];
            if (review?.status === 'confirmed') {
                this.updatePlWsiStatus(index, 'confirmed', 'check');
                return;
            }
            if (['pending_review', 'revised_pending_confirmation'].includes(review?.status)) {
                this.updatePlWsiStatus(index, 'review', 'user-check');
                return;
            }
            const card = document.querySelector(`.pl-wsi-card[data-index="${index}"]`);
            if (card && (card.classList.contains('confirmed') || card.classList.contains('review') || card.classList.contains('completed') || card.classList.contains('error'))) {
                return;
            }
            if (!info?.file_id) return;
            if (parallel || index === 0) {
                this.updatePlWsiStatus(index, 'analyzing', 'spinner fa-spin');
            }
        });
        this._applyAllPatientSlideThumbs();
        this.syncPatientSlideReportNav();
    }

    resetPatientSlideNavStates() {
        this.patientSlideNavStates = {};
    }

    _patientSlideShortName(info, index) {
        const raw = String(info?.part || info?.filename || info?.name || '')
            .replace(/^Slide\s+\d+:\s*/i, '')
            .trim();
        const noExt = raw.replace(/\.(ndpi|svs|tiff?|mrxs)$/i, '');
        const stripped = noExt.replace(/^\d{4,}-/, '');
        return stripped || `${(window.__lang || 'zh') === 'en' ? 'Slide' : '切片'} ${index + 1}`;
    }

    _patientWsiIndexFromPayload(data = {}) {
        if (Number.isInteger(data.wsi_index)) return data.wsi_index;
        if (Number.isInteger(data.index)) return data.index;
        const fromMeta = Number(data.metadata?.wsi_index);
        if (Number.isInteger(fromMeta)) return fromMeta;
        const fromFile = this._patientSlideIndexFromFileId(
            data.file_id || data.metadata?.file_id || ''
        );
        if (Number.isInteger(fromFile)) return fromFile;
        const analyzing = window.pathChatAgent?.currentAnalyzingWsiIndex;
        return Number.isInteger(analyzing) ? analyzing : null;
    }

    _collectPatientSlideReportEntries() {
        const infos = this.plAllFileInfos || this.patientLevelUploadResults || [];
        const runActive = Boolean(
            this.patientIsRunning || window.pathChatAgent?._activePatientRunToken
        );
        const parallel = this.patientWsiExecutionMode === 'parallel'
            || window.pathChatAgent?.patientWsiExecutionMode === 'parallel';
        return infos.map((info, i) => {
            const index = Number.isInteger(info?.index) ? info.index : i;
            const fileId = String(info?.file_id || '');
            const review = this.patientSlideReviews?.[fileId];
            const remembered = this.patientSlideNavStates?.[index] || '';
            const card = document.querySelector(`.pl-wsi-card[data-index="${index}"]`);
            const map = document.querySelector(`.patient-slide-map[data-wsi-index="${index}"]`);
            const mapHasWork = Boolean(map && map.querySelector(
                '.v2-agent-activity.is-running, .v2-subagent-stream.is-running, '
                + '.phase-result-card.is-running, .phase-header'
            ));
            const railStatus = card ? (
                card.classList.contains('confirmed') ? 'confirmed'
                : card.classList.contains('review') ? 'review'
                : card.classList.contains('completed') ? 'completed'
                : card.classList.contains('analyzing') ? 'analyzing'
                : card.classList.contains('error') ? 'error'
                : 'pending'
            ) : (this.plWsiAnalysisData?.[index]?.status || remembered || 'pending');
            const reviewStatus = String(review?.status || '');
            const reviewHost = document.getElementById(`patientReviewChat-${index}`);
            const reviewCard = reviewHost?.querySelector('.patient-slide-review-card');
            const phase5 = document.querySelector(
                `.phase-confirm-message[data-phase="5"][data-wsi-index="${index}"]`
            );
            const phase5Resolved = Boolean(
                phase5?.classList.contains('is-resolved')
                || phase5?.querySelector('.phase-confirmed-status')
            );
            const hasConfirmGate = Boolean(
                (reviewCard && !['confirmed'].includes(reviewStatus))
                || (phase5 && !phase5Resolved)
            );
            let state = 'pending';
            if (reviewStatus === 'confirmed' || railStatus === 'confirmed' || remembered === 'final' || phase5Resolved) {
                state = 'final';
            } else if (reviewStatus === 'revising' || remembered === 'revising') {
                state = 'revising';
            } else if (
                ['pending_review', 'revised_pending_confirmation'].includes(reviewStatus)
                || railStatus === 'review'
                || remembered === 'review'
                || hasConfirmGate
            ) {
                state = 'review';
            } else if (railStatus === 'completed' || remembered === 'completed') {
                // A restored roam/swarm map still has a stream. That is
                // finished work, not "3 slides analyzing".
                state = hasConfirmGate ? 'review' : 'final';
            } else if (railStatus === 'error' || remembered === 'error' || reviewStatus === 'error' || reviewStatus === 'revision_failed') {
                state = 'error';
            } else if (
                railStatus === 'analyzing'
                || remembered === 'analyzing'
                || (mapHasWork && runActive)
            ) {
                state = 'analyzing';
            } else if (runActive && fileId && (parallel || window.pathChatAgent?.currentAnalyzingWsiIndex === index)) {
                state = 'analyzing';
            }
            if (
                (state === 'pending' || state === 'analyzing')
                && document.querySelector(
                    `[data-patient-slide-report="final"][data-wsi-index="${index}"], #patient-slide-report-anchor-${index}`
                )
            ) {
                state = 'final';
            }
            return {
                index,
                fileId,
                part: this._patientSlideShortName(info, index),
                state
            };
        });
    }

    _hasPatientCompositeReport() {
        return Boolean(
            document.querySelector('.patient-report-msg, .patient-report-review-card, #patient-composite-report-anchor')
        );
    }

    syncPatientSlideReportNav() {
        const nav = this._ensurePatientSlideReportNav();
        if (!nav) return;
        this._adoptExistingPatientSlideReports();
        const entries = this._collectPatientSlideReportEntries();
        const showNav = this._isPatientLevelUiActive() && entries.length > 0;
        if (!showNav) {
            nav.hidden = true;
            nav.innerHTML = '';
            return;
        }
        const isEn = (window.__lang || 'zh') === 'en';
        const finalCount = entries.filter((entry) => entry.state === 'final').length;
        const viewing = Number.isInteger(window.pathChatAgent?.currentViewingWsiIndex)
            ? window.pathChatAgent.currentViewingWsiIndex
            : null;
        const stateLabel = {
            pending: window.__t?.('pl_slide_report_state_pending') || (isEn ? 'Pending' : '未开始'),
            analyzing: window.__t?.('pl_slide_report_state_analyzing') || (isEn ? 'Running' : '分析中'),
            review: window.__t?.('pl_slide_report_state_review') || (isEn ? 'Needs confirm' : '待确认'),
            revising: window.__t?.('pl_slide_report_state_revising') || (isEn ? 'Revising' : '修订中'),
            final: window.__t?.('pl_slide_report_state_final') || (isEn ? 'Finalized' : '已定稿'),
            error: window.__t?.('pl_slide_report_state_error') || (isEn ? 'Failed' : '失败')
        };
        const analyzingCount = entries.filter((entry) => entry.state === 'analyzing' || entry.state === 'revising').length;
        const items = entries.map((entry) => {
            const active = viewing === entry.index ? ' is-active' : '';
            const label = stateLabel[entry.state] || entry.state;
            const title = `${entry.index + 1}. ${entry.part} · ${label}`;
            return `<button type="button" class="patient-slide-report-item${active}" data-index="${entry.index}" data-state="${entry.state}" title="${this.escapeHtml(title)}" aria-label="${this.escapeHtml(title)}">
                <span class="item-index">${entry.index + 1}</span>
            </button>`;
        });
        let countText = typeof window.__t === 'function'
            ? window.__t('pl_slide_report_nav_count', finalCount, entries.length)
            : (isEn ? `${finalCount}/${entries.length} finalized` : `已定稿 ${finalCount}/${entries.length}`);
        if (analyzingCount) {
            const runningText = typeof window.__t === 'function'
                ? window.__t('pl_slide_report_nav_running', analyzingCount)
                : (isEn ? `${analyzingCount} running` : `${analyzingCount} 张分析中`);
            countText = `${countText} · ${runningText}`;
        }
        nav.hidden = false;
        nav.innerHTML = `
            <div class="patient-slide-report-nav-head">
                <strong>${this.escapeHtml(window.__t?.('pl_slide_report_nav') || (isEn ? 'Slides' : '切片'))}</strong>
                <span class="patient-slide-report-nav-count">${this.escapeHtml(countText)}</span>
            </div>
            <div class="patient-slide-report-track">${items.join('')}</div>
        `;
    }

    _flashPatientSlideReportTarget(target) {
        if (!target) return;
        const el = target.closest?.('.message, .chat-message, .patient-review-chat-wrap, .patient-slide-review-card, .wsi-analysis-msg, .phase-confirm-message, .patient-report-msg, .patient-report-review-card') || target;
        el.classList.add('patient-slide-report-flash');
        window.setTimeout(() => el.classList.remove('patient-slide-report-flash'), 1600);
        el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }

    _findPatientSlideReportAnchor(index, state = '') {
        const idx = String(index);
        const reviewHost = document.getElementById(`patientReviewChat-${index}`);
        const reviewWrap = reviewHost?.closest('.patient-review-chat-wrap') || reviewHost;
        const reviewCard = reviewHost?.querySelector('.patient-slide-review-card');
        const phase5 = document.querySelector(`.phase-confirm-message[data-phase="5"][data-wsi-index="${idx}"]`);
        const tagged = document.querySelector(`[data-patient-slide-report][data-wsi-index="${idx}"]`);
        const success = document.querySelector(`.wsi-analysis-msg.success[data-wsi-index="${idx}"]`);
        const named = document.getElementById(`patient-slide-report-anchor-${index}`);
        if (state === 'review' || state === 'revising') {
            return reviewCard || reviewWrap || phase5 || named || tagged || success;
        }
        return named || tagged || success || reviewCard || reviewWrap || phase5;
    }

    jumpToPatientSlideReport(index) {
        if (!Number.isInteger(index)) return;
        this.selectPlWsiItem?.(index);
        const entry = this._collectPatientSlideReportEntries().find((item) => item.index === index);
        const target = this._findPatientSlideReportAnchor(index, entry?.state || '');
        if (target) this._flashPatientSlideReportTarget(target);
        this.syncPatientSlideReportNav();
    }

    jumpToPatientCompositeReport() {
        const target = document.getElementById('patient-composite-report-anchor')
            || document.querySelector('.patient-report-review-card, .patient-report-msg');
        if (target) this._flashPatientSlideReportTarget(target);
    }

    _markPatientSlideReportAnchor(el, index, kind = 'report') {
        if (!el || !Number.isInteger(index)) return;
        el.dataset.wsiIndex = String(index);
        el.dataset.patientSlideReport = kind;
        if (!el.id) el.id = `patient-slide-report-anchor-${index}`;
    }

    _looksLikePatientSlideFinalReport(text) {
        return /最终病理报告|Final Pathology Report/i.test(String(text || ''));
    }

    tagIncomingPatientSlideReport(messageElement, data = {}) {
        if (!messageElement || !this._isPatientLevelUiActive()) return;
        const content = String(data.content || messageElement.textContent || '');
        const isFinal = data.metadata?.message_type === 'final_report'
            || this._looksLikePatientSlideFinalReport(content);
        if (!isFinal) return;
        const index = this._patientWsiIndexFromPayload(data);
        if (Number.isInteger(index)) {
            this._markPatientSlideReportAnchor(messageElement, index, 'final');
        } else {
            messageElement.id = messageElement.id || 'patient-composite-report-anchor';
            messageElement.dataset.patientCompositeReport = '1';
        }
        this._schedulePatientSlideReportNavSync();
    }

    _tagPatientChatArtifacts(messageEl) {
        if (!messageEl) return;
        messageEl.querySelectorAll('.wsi-analysis-msg.success[data-wsi-index]').forEach((el) => {
            if (!el.id) el.id = `patient-slide-report-anchor-${el.dataset.wsiIndex}`;
        });
        const report = messageEl.querySelector('.patient-report-msg, .patient-report-review-card');
        if (report) {
            if (!report.id) report.id = 'patient-composite-report-anchor';
            report.dataset.patientCompositeReport = '1';
        }
        const wrap = messageEl.classList.contains('patient-review-chat-wrap')
            ? messageEl
            : messageEl.querySelector('.patient-review-chat-wrap');
        if (wrap && wrap.dataset.patientReviewIndex && !wrap.dataset.wsiIndex) {
            wrap.dataset.wsiIndex = wrap.dataset.patientReviewIndex;
        }
    }

    _adoptExistingPatientSlideReports() {
        const chat = this.elements?.chatMessages || document.getElementById('chatMessages');
        if (!chat) return;
        const infos = this.plAllFileInfos || this.patientLevelUploadResults || [];
        chat.querySelectorAll('.wsi-analysis-msg.success').forEach((el) => {
            if (el.dataset.wsiIndex) {
                if (!el.id) el.id = `patient-slide-report-anchor-${el.dataset.wsiIndex}`;
                return;
            }
            const text = el.textContent || '';
            infos.forEach((info, idx) => {
                const index = Number.isInteger(info?.index) ? info.index : idx;
                const filename = String(info?.filename || info?.name || '');
                const part = this._patientSlideShortName(info, index);
                if ((filename && text.includes(filename)) || (part && text.includes(part))) {
                    el.dataset.wsiIndex = String(index);
                    if (!el.id) el.id = `patient-slide-report-anchor-${index}`;
                }
            });
        });
        const used = new Set(
            Array.from(chat.querySelectorAll('[data-patient-slide-report="final"][data-wsi-index]'))
                .map((el) => Number(el.dataset.wsiIndex))
                .filter((value) => Number.isInteger(value))
        );
        const finals = Array.from(chat.querySelectorAll('.message, .chat-message')).filter((el) => (
            this._looksLikePatientSlideFinalReport(el.textContent)
            && !el.dataset.patientCompositeReport
            && !el.querySelector('.patient-report-msg, .patient-report-review-card')
        ));
        finals.forEach((el) => {
            if (el.dataset.patientSlideReport === 'final' && el.dataset.wsiIndex) {
                used.add(Number(el.dataset.wsiIndex));
                return;
            }
            let matched = null;
            infos.forEach((info, idx) => {
                const index = Number.isInteger(info?.index) ? info.index : idx;
                const part = this._patientSlideShortName(info, index);
                const filename = String(info?.filename || '').replace(/\.[^.]+$/, '');
                if (part && el.textContent.includes(part)) matched = index;
                else if (filename && filename.length > 4 && el.textContent.includes(filename)) matched = index;
            });
            if (Number.isInteger(matched)) {
                this._markPatientSlideReportAnchor(el, matched, 'final');
                used.add(matched);
            }
        });
        const leftover = finals.filter((el) => el.dataset.patientSlideReport !== 'final');
        const confirmed = this._collectPatientSlideReportEntries()
            .filter((entry) => entry.state === 'final' && !used.has(entry.index))
            .map((entry) => entry.index);
        leftover.forEach((el, i) => {
            if (Number.isInteger(confirmed[i])) {
                this._markPatientSlideReportAnchor(el, confirmed[i], 'final');
            }
        });
        chat.querySelectorAll('.patient-report-msg, .patient-report-review-card').forEach((el) => {
            if (!el.id) el.id = 'patient-composite-report-anchor';
            el.dataset.patientCompositeReport = '1';
        });
    }

    updatePatientReviewGate(data) {
        if (!data) return;
        const isEn = (window.__lang || 'zh') === 'en';
        const total = Number(data.total || 0);
        const confirmed = Number(data.confirmed || 0);
        const revising = Number(data.revising || 0);
        const statusEl = document.getElementById('plStatus');
        if (statusEl) {
            if (data.status === 'ready') {
                statusEl.textContent = isEn
                    ? 'All slides confirmed · generating patient report'
                    : '全部切片已确认 · 正在生成患者级报告';
            } else {
                statusEl.textContent = isEn
                    ? `${confirmed}/${total} slides confirmed${revising ? ` · ${revising} revising` : ''}`
                    : `已确认 ${confirmed}/${total} 张${revising ? ` · ${revising} 张修订中` : ''}`;
            }
        }
        if (total > 0) {
            const status = String(data.status || '');
            const reviewPhase = ['awaiting_review', 'awaiting_slide_review', 'ready'].includes(status)
                || confirmed > 0
                || revising > 0;
            // Snapshot restore during analyzing_slides used to jump the rail to 75%
            // before any slide had finished. Keep that band for the real review gate.
            if (reviewPhase) {
                this.updatePlProgress(
                    75 + (confirmed / total) * 15,
                    statusEl?.textContent || ''
                );
            }
        }
        this.syncPatientSlideReportNav();
    }

    async submitPatientSlideReview(fileId, index, action) {
        const isEn = (window.__lang || 'zh') === 'en';
        const feedbackEl = document.getElementById(`patientReviewFeedback-${index}`);
        const feedback = (feedbackEl?.value || '').trim();
        if ((action === 'revise' || action === 'add_roi') && !feedback) {
            this.showNotification(
                'warning',
                isEn ? 'Please enter the pathologist feedback first.' : '请先填写病理医生反馈。'
            );
            feedbackEl?.focus();
            return;
        }

        let imageData = '';
        if (action === 'add_roi') {
            const activeFileId = this.currentFile?.file_id || '';
            if (!activeFileId || activeFileId !== fileId) {
                this.showNotification(
                    'warning',
                    isEn
                        ? 'Open this slide in the viewer before adding a new ROI.'
                        : '请先在查看器中打开该切片，再添加新的 ROI。'
                );
                return;
            }
            const rawImage = this.currentScreenshot?.imageData || '';
            if (!rawImage) {
                this.toggleClickPlaceRoiMode(true);
                this.showNotification(
                    'info',
                    isEn
                        ? 'Click the slide to place a new ROI, then analyze it.'
                        : '请先在切片上点击放置新增 ROI，然后再分析。'
                );
                return;
            }
            imageData = rawImage.startsWith('data:image/')
                ? rawImage
                : `data:image/jpeg;base64,${rawImage}`;
        }

        const card = Array.from(
            document.querySelectorAll('.patient-slide-review-card[data-file-id]')
        ).find(element => element.dataset.fileId === fileId);
        const reviewRunToken = String(card?.dataset.runToken || '');
        if (
            !reviewRunToken
            || reviewRunToken !== String(window.pathChatAgent?._activePatientRunToken || '')
        ) {
            this.showNotification(
                'warning',
                isEn
                    ? 'This review card belongs to an older run.'
                    : '该审阅卡属于上一轮分析，不能提交。'
            );
            return;
        }
        card?.querySelectorAll('button').forEach(button => { button.disabled = true; });
        try {
            const response = await fetch(
                `/api/patient/${encodeURIComponent(this.currentPatientId)}/slides/${encodeURIComponent(fileId)}/review`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        action,
                        feedback,
                        image_data: imageData,
                        run_token: reviewRunToken
                    })
                }
            );
            const result = await response.json();
            if (!response.ok || !result.success) {
                throw new Error(result.error || `HTTP ${response.status}`);
            }
            if (result.review) this.renderPatientSlideReview(result.review);
            if (result.summary) this.updatePatientReviewGate(result.summary);
            if (action === 'add_roi') this.clearScreenshot();
            this.showNotification(
                'success',
                action === 'confirm'
                    ? (isEn ? 'Slide confirmed.' : '该切片已确认。')
                    : (isEn ? 'Revision started.' : '已开始按反馈修订。')
            );
        } catch (error) {
            card?.querySelectorAll('button').forEach(button => { button.disabled = false; });
            this.showNotification(
                'error',
                (isEn ? 'Review update failed: ' : '复核操作失败：') + error.message
            );
        }
    }
    
    /**
     * 重新分析 Patient Level 中的单张切片
     */
    async _rerunSingleWsi(index) {
        const info = this.plAllFileInfos?.[index];
        if (!info) return;
        const fileId = info.file_id;
        const filename = info.filename || info.name || `Slide ${index + 1}`;
        const isEn = (window.__lang || 'zh') === 'en';
        
        const confirmed = await this._showSimpleConfirmDialog(
            isEn ? `Reanalyze "${filename}"?\nThis will overwrite previous results for this slide.`
                 : `确定重新分析「${filename}」？\n该切片的旧分析结果将被覆盖。`
        );
        if (!confirmed) return;
        
        this.updatePlWsiStatus(index, 'analyzing', 'spinner fa-spin');
        const rerunBtn = document.querySelector(`.wsi-rerun-btn[data-index="${index}"]`);
        if (rerunBtn) rerunBtn.style.display = 'none';
        
        try {
            const mode = window.pathChatAgent?.isCollaborativeMode ? 'collaborative' : 'ai';
            const resp = await fetch('/api/agent/rerun-single-wsi', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    patient_id: this.currentPatientId,
                    file_id: fileId,
                    file_info: info,
                    mode: mode,
                    lang: window.__lang || 'zh',
                    session_id: window.pathChatAgent?.socket?.id || '',
                    folder_name: this.pendingFolderName || '',
                    total_wsi: this.plAllFileInfos?.length || 1
                })
            });
            const result = await resp.json();
            if (!result.success) throw new Error(result.error);
            
            this.addChatMessage('assistant', isEn
                ? `🔄 Reanalyzing <strong>${filename}</strong>...`
                : `🔄 正在重新分析 <strong>${filename}</strong>...`);
        } catch (err) {
            console.error('Rerun single WSI failed:', err);
            this.updatePlWsiStatus(index, 'error', 'times');
            if (rerunBtn) rerunBtn.style.display = '';
            this.showNotification('error', isEn ? `Reanalysis failed: ${err.message}` : `重新分析失败：${err.message}`);
        }
    }
    
    /**
     * 重新运行 Orchestrator 汇总
     */
    async _rerunOrchestrator() {
        const isEn = (window.__lang || 'zh') === 'en';
        
        const { confirmed, instruction } = await new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.45);z-index:99999;display:flex;align-items:center;justify-content:center;';
            const box = document.createElement('div');
            box.style.cssText = 'background:#fff;border-radius:12px;padding:28px 32px 20px;max-width:480px;width:90%;box-shadow:0 8px 40px rgba(0,0,0,.25);font-family:system-ui,sans-serif;';
            box.innerHTML = `
                <h3 style="margin:0 0 12px;font-size:16px;color:#111;">${__t('rerun_orchestrator_title')}</h3>
                <p style="font-size:13px;color:#666;margin:0 0 12px;">${__t('rerun_orchestrator_hint')}</p>
                <textarea id="_rerunInstructionInput" rows="3" style="width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid #d1d5db;border-radius:6px;font-size:13px;resize:vertical;margin-bottom:16px;" placeholder="${isEn ? 'e.g. Focus on the evidence relevant to my question...' : '如：请重点关注与当前问题相关的证据...'}"></textarea>
                <div style="display:flex;justify-content:flex-end;gap:10px;">
                    <button id="_rerunCancel" style="padding:8px 20px;border:1px solid #d1d5db;border-radius:8px;background:#fff;color:#374151;cursor:pointer;font-size:14px;">
                        ${isEn ? 'Cancel' : '取消'}
                    </button>
                    <button id="_rerunConfirm" style="padding:8px 20px;border:none;border-radius:8px;background:#f59e0b;color:#fff;cursor:pointer;font-size:14px;font-weight:600;">
                        ${isEn ? 'Regenerate' : '重新汇总'}
                    </button>
                </div>
            `;
            overlay.appendChild(box);
            document.body.appendChild(overlay);
            box.querySelector('#_rerunConfirm').addEventListener('click', () => {
                const val = box.querySelector('#_rerunInstructionInput').value.trim();
                overlay.remove();
                resolve({ confirmed: true, instruction: val });
            });
            box.querySelector('#_rerunCancel').addEventListener('click', () => { overlay.remove(); resolve({ confirmed: false, instruction: '' }); });
            overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); resolve({ confirmed: false, instruction: '' }); } });
            box.querySelector('#_rerunConfirm').focus();
        });
        
        if (!confirmed) return;
        
        const btn = document.getElementById('plRerunOrchestrator');
        if (btn) { btn.disabled = true; btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${__t('rerun_orchestrator_running')}`; }
        
        try {
            const mode = window.pathChatAgent?.isCollaborativeMode ? 'collaborative' : 'ai';
            const resp = await fetch('/api/agent/rerun-orchestrator', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    patient_id: this.currentPatientId,
                    folder_name: this.pendingFolderName || '',
                    file_infos: this.plAllFileInfos || [],
                    mode: mode,
                    lang: window.__lang || 'zh',
                    user_instruction: instruction
                })
            });
            const result = await resp.json();
            if (!result.success) throw new Error(result.error);
            
            const reportContent = document.getElementById('plReportContent');
            if (reportContent && window.pathChatAgent) {
                reportContent.innerHTML = window.pathChatAgent.formatMarkdown(result.summary);
            }
            
            if (result.html_report_url) {
                const dlBtn = document.getElementById('plDownloadReport');
                if (dlBtn) { dlBtn.href = result.html_report_url; dlBtn.style.display = 'inline-flex'; }
            }
            
            this.addChatMessage('assistant', `
                <div class="patient-report-msg">
                    <h4>🔄 ${isEn ? 'Regenerated Comprehensive Report' : '重新生成的综合诊断报告'}</h4>
                    <div class="report-summary">${window.pathChatAgent?.formatMarkdown(result.summary) || result.summary}</div>
                    ${result.html_report_url ? `<div style="margin-top:12px;"><a href="${result.html_report_url}" target="_blank" style="display:inline-flex;align-items:center;gap:6px;padding:8px 16px;background:linear-gradient(135deg,#667eea,#764ba2);color:white;border-radius:6px;text-decoration:none;font-size:14px;font-weight:500;"><i class="fas fa-file-medical-alt"></i> ${isEn ? 'View Report' : '查看报告'}</a></div>` : ''}
                </div>
            `);
            
            this.showNotification('success', __t('rerun_orchestrator_success'));
        } catch (err) {
            console.error('Rerun orchestrator failed:', err);
            this.showNotification('error', isEn ? `Regeneration failed: ${err.message}` : `重新汇总失败：${err.message}`);
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = `<i class="fas fa-redo"></i> ${__t('rerun_orchestrator')}`; }
        }
    }
    
    /**
     * 开始Patient Level批量上传（逐个顺序上传，大文件场景下比并行更快）
     */
    async startPatientLevelUpload() {
        const files = this.pendingPatientLevelFiles;
        const folderName = this.pendingFolderName;
        
        if (!files || files.length === 0) {
            this.showNotification('error', __t('no_files_to_analyze'));
            return;
        }
        
        console.log(`[Patient Level] 开始顺序上传 ${files.length} 个文件，文件夹: ${folderName}`);
        
        // 更新状态显示
        const statusEl = document.getElementById('plStatus');
        if (statusEl) statusEl.textContent = __t('uploading_status');
        
        // 存储上传结果
        this.patientLevelUploadResults = [];
        this.patientLevelFolderName = folderName;
        this.plUploadComplete = false;          // ⭐ 标记上传是否全部完成
        this.plAnalysisStartedEarly = false;    // ⭐ 标记是否在上传过程中就已启动分析
        // ⭐ 整个 Patient Level 文件夹共用同一个批次 ID，清理时作为一组处理
        this.patientLevelBatchId = this._genUUID();
        
        // ⭐ 预生成所有文件的 part 信息（用于提前启动分析时传给后端）
        this.plAllFileInfos = files.map((file, idx) => ({
            index: idx,
            filename: file.name,
            part: this.extractPartInfo(file.name, idx),
            file_id: null  // 上传成功后填入
        }));
        
        // 设置所有文件为待上传状态
        files.forEach((_, i) => {
            this.updatePlWsiStatus(i, 'pending', 'clock');
        });
        
        this.updatePlProgress(2, `准备上传 ${files.length} 个文件...`);
        
        const startBtn = document.getElementById('plStartAnalysis');
        
        // ⭐ 查询服务器已有文件，按文件名匹配实现"免重复上传"
        let existingFilesMap = {};
        try {
            const resp = await fetch('/api/files');
            const result = await resp.json();
            if (result.files) {
                result.files.forEach(f => {
                    existingFilesMap[f.filename] = f;
                });
            }
            console.log(`[Patient Level] 已查询服务器文件列表: ${Object.keys(existingFilesMap).length} 个已有文件`);
        } catch (e) {
            console.warn('[Patient Level] 查询已有文件列表失败，将全部重新上传', e);
        }
        
        let reusedCount = 0;  // 跳过上传直接复用的数量
        const reusedFileIds = [];  // 收集复用文件的 ID，稍后统一更新 batch_id
        
        // ⭐ 逐个处理文件（已有的跳过上传直接复用，新文件顺序上传）
        for (let i = 0; i < files.length; i++) {
            const file = files[i];
            let partInfo = this.plAllFileInfos[i].part;
            
            // ⭐ 检查服务器是否已有同名文件 —— 有则跳过上传，直接复用
            const existingFile = existingFilesMap[file.name];
            if (existingFile) {
                reusedCount++;
                reusedFileIds.push(existingFile.file_id);
                this._patientFileMetaById = this._patientFileMetaById || {};
                this._patientFileMetaById[existingFile.file_id] = existingFile;
                if (existingFile.user_specimen_declared === true && existingFile.user_specimen_site) {
                    partInfo = `Slide ${i + 1}: ${existingFile.user_specimen_site}`;
                    this.plAllFileInfos[i].part = partInfo;
                }
                const uploadEntry = {
                    file_id: existingFile.file_id,
                    filename: existingFile.filename,
                    part: partInfo,
                    index: i,
                    is_demo: existingFile.is_demo === true,
                    needs_specimen_site: existingFile.needs_specimen_site === true,
                    user_specimen_declared: existingFile.user_specimen_declared === true,
                    user_specimen_unknown: existingFile.user_specimen_unknown === true,
                    user_specimen_site: existingFile.user_specimen_site || '',
                };
                this.patientLevelUploadResults.push(uploadEntry);
                this.plAllFileInfos[i].file_id = existingFile.file_id;
                this.updatePlWsiStatus(i, 'uploaded', 'check');
                console.log(`[Patient Level] ♻️ 文件已存在，跳过上传: ${file.name} -> ${existingFile.file_id}`);
                
                // 复用的文件也走"就绪"逻辑
                this._onFileReady(i, files.length, startBtn);
                
                // 更新进度
                const progress = 5 + ((i + 1) / files.length) * 25;
                const uploadingCount = (i + 1) - reusedCount;
                this.updatePlProgress(progress, 
                    reusedCount > 0 
                        ? __t('pl_reuse_upload_status', reusedCount, uploadingCount)
                        : __t('pl_upload_status', i + 1, files.length));
                continue;
            }
            
            this.updatePlWsiStatus(i, 'uploading', 'spinner fa-spin');
            const fileIdx = i, totalFiles = files.length;
            const baseLabel = reusedCount > 0
                ? __t('pl_reuse_upload_status', reusedCount, fileIdx + 1 - reusedCount)
                : __t('pl_upload_status', fileIdx + 1, totalFiles);
            this.updatePlProgress(
                5 + (fileIdx / totalFiles) * 25,
                baseLabel + `: ${file.name}`
            );
            
            try {
                const result = await this.uploadFileForPatientLevel(file, this.patientLevelBatchId, (pct, speed, eta) => {
                    const overallPct = 5 + ((fileIdx + pct / 100) / totalFiles) * 25;
                    const speedStr = this._formatBytes(speed) + '/s';
                    const etaStr = this._formatETA(eta);
                    const isEn = (window.__lang || 'zh') === 'en';
                    this.updatePlProgress(overallPct,
                        `${baseLabel}: ${file.name}  ·  ${Math.round(pct)}%  ·  ${speedStr}  ·  ${isEn ? 'ETA' : '剩余'} ${etaStr}`
                    );
                });
                
                if (result.success) {
                    const uploadEntry = {
                        file_id: result.file_id,
                        filename: result.filename,
                        part: partInfo,
                        index: i,
                        is_demo: result.is_demo === true,
                        needs_specimen_site: result.needs_specimen_site !== false,
                        user_specimen_declared: result.user_specimen_declared === true,
                        user_specimen_unknown: result.user_specimen_unknown === true,
                        user_specimen_site: result.user_specimen_site || '',
                    };
                    this.patientLevelUploadResults.push(uploadEntry);
                    this.plAllFileInfos[i].file_id = result.file_id;
                    this._patientFileMetaById = this._patientFileMetaById || {};
                    this._patientFileMetaById[result.file_id] = {
                        file_id: result.file_id,
                        filename: result.filename,
                        dimensions: result.dimensions,
                        level_count: result.level_count,
                        file_size: result.file_size,
                        thumbnail_url: result.thumbnail_url || `/api/thumbnail/${result.file_id}`,
                    };
                    this.updatePlWsiStatus(i, 'uploaded', 'check');
                    console.log(`[Patient Level] ✅ 文件 ${i + 1}/${files.length} 上传成功: ${file.name}`);
                    
                    // 文件就绪（启用按钮、通知后端等）
                    this._onFileReady(i, files.length, startBtn);
                } else {
                    this.updatePlWsiStatus(i, 'error', 'times');
                    console.error(`[Patient Level] ❌ 文件 ${i + 1}/${files.length} 上传失败: ${result.error}`);
                }
            } catch (error) {
                console.error(`[Patient Level] ❌ 文件 ${i + 1}/${files.length} 上传异常: ${file.name}`, error);
                this.updatePlWsiStatus(i, 'error', 'times');
            }
            
            // 更新进度
            const progress = 5 + ((i + 1) / files.length) * 25;
            const uploadingCount = (i + 1) - reusedCount;
            this.updatePlProgress(progress, 
                reusedCount > 0 
                    ? __t('pl_reuse_upload_status', reusedCount, uploadingCount)
                    : __t('pl_upload_status', i + 1, files.length));
        }
        
        if (reusedCount > 0) {
            const newUploaded = this.patientLevelUploadResults.length - reusedCount;
            this.showNotification('info', __t('pl_file_reused', reusedCount, newUploaded));
            
            // ⭐ 将复用文件的 batch_id 更新为当前 Patient Level 批次，防止被自动清理误删
            if (reusedFileIds.length > 0) {
                try {
                    await fetch('/api/update_batch', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ file_ids: reusedFileIds, batch_id: this.patientLevelBatchId })
                    });
                    console.log(`[Patient Level] ✅ 已将 ${reusedFileIds.length} 个复用文件的 batch_id 更新为当前批次`);
                } catch (e) {
                    console.warn('[Patient Level] ⚠️ 更新复用文件 batch_id 失败:', e);
                }
            }
        }
        
        // ⭐ 标记上传全部完成
        this.plUploadComplete = true;
        
        // 按index排序结果
        this.patientLevelUploadResults.sort((a, b) => a.index - b.index);
        
        // 上传完成，刷新文件列表
        this.loadFilesList();
        
        if (this.plAnalysisStartedEarly) {
            // 分析已提前启动，仅更新状态
            if (statusEl) statusEl.textContent = __t('upload_done_analyzing');
            this.updatePlProgress(35, __t('all_uploaded_analyzing'));
            console.log(`[Patient Level] 所有文件上传完成，分析已在运行中`);
        } else if (this.patientLevelUploadResults.length === files.length) {
            this.showNotification('success', __t('pl_all_uploaded', files.length));
            if (statusEl) statusEl.textContent = __t('pending_analysis');
            this.updatePlProgress(30, __t('upload_done_start'));
            if (startBtn) {
                startBtn.disabled = false;
                startBtn.innerHTML = `<i class="fas fa-play"></i> ${__t('pl_start_analysis')}`;
            }
            if (!this.patientAnalysisAwaitingQuestion && !this.patientIsRunning) {
                this.startPatientLevelAnalysis();
            }
        } else {
            this.showNotification('warning', __t('pl_partial_fail', this.patientLevelUploadResults.length, files.length));
            if (statusEl) statusEl.textContent = __t('partial_upload_failed');
            if (this.patientLevelUploadResults.length > 0 && startBtn) {
                startBtn.disabled = false;
                startBtn.innerHTML = `<i class="fas fa-play"></i> ${__t('pl_start_analysis')}`;
            }
        }
        
        // 清理输入
        this.elements.fileInput.value = '';
    }
    
    /**
     * ⭐ 文件就绪后的公共逻辑（上传成功 或 复用已有文件时调用）
     * 处理：启用按钮、加载查看器、通知后端等
     */
    _onFileReady(fileIndex, totalFiles, startBtn) {
        const files = this.pendingPatientLevelFiles;
        
        // 第一个文件就绪后，立即启用按钮 + 加载到查看器
        if (this.patientLevelUploadResults.length === 1) {
            this.selectPlWsiItem(0);
            if (startBtn) {
                startBtn.disabled = false;
                startBtn.innerHTML = `<i class="fas fa-play"></i> ${__t('pl_ready_count', this.patientLevelUploadResults.length, totalFiles)}`;
            }
            if (totalFiles > 1) {
                this.showNotification('info', __t('pl_first_ready'));
            }
        } else if (!this.plAnalysisStartedEarly && startBtn && !startBtn.disabled) {
            startBtn.innerHTML = `<i class="fas fa-play"></i> ${__t('pl_ready_count', this.patientLevelUploadResults.length, totalFiles)}`;
        }
        
        // 如果分析已提前启动，通知后端新文件已就绪
        if (this.plAnalysisStartedEarly && this.currentPatientId) {
            const entry = this.patientLevelUploadResults.find(r => r.index === fileIndex);
            if (entry) {
                console.log(`[Patient Level] 通知后端新文件就绪: ${entry.file_id}`);
                this.notifyBackendFileReady(this.currentPatientId, entry.file_id, entry.filename, entry.part, fileIndex);
            }
        }
    }
    
    /**
     * ⭐ 通知后端新文件已上传就绪（分析提前启动时使用）
     */
    notifyBackendFileReady(patientId, fileId, filename, part, index) {
        const socket = window.pathChatAgent?.socket;
        if (socket) {
            socket.emit('patient_level_file_ready', {
                patient_id: patientId,
                file_id: fileId,
                filename: filename,
                part: part,
                index: index
            });
        }
    }
    
    /**
     * 为Patient Level上传单个文件
     */
    async uploadFileForPatientLevel(file, batchId, onProgress) {
        const formData = new FormData();
        formData.append('file', file);
        if (batchId) formData.append('batch_id', batchId);

        return this._uploadWithProgress('/api/upload', formData, onProgress);
    }
    
    /**
     * 显示Patient Level进度面板
     */
    showPatientLevelProgressPanel(files, folderName) {
        const panel = document.getElementById('patientLevelProgressPanel');
        const folderDisplay = document.getElementById('patientFolderDisplay');
        const wsiCountDisplay = document.getElementById('patientWsiCountDisplay');
        const progressList = document.getElementById('wsiProgressList');
        
        if (!panel) return;
        
        // 设置信息
        if (folderDisplay) folderDisplay.textContent = __t('pl_patient_label', folderName);
        if (wsiCountDisplay) wsiCountDisplay.textContent = __t('pl_slide_label', `0/${files.length}`);
        
        // 生成WSI进度列表
        if (progressList) {
            progressList.innerHTML = files.map((file, idx) => {
                const partInfo = this.extractPartInfo(file.name, idx);
                return `
                    <div class="wsi-progress-item" data-index="${idx}">
                        <div class="wsi-info">
                            <span class="wsi-name" title="${file.name}">${file.name.length > 25 ? file.name.substring(0, 22) + '...' : file.name}</span>
                            <span class="wsi-part">${partInfo}</span>
                        </div>
                        <div class="wsi-status">
                            <span class="status-badge pending">${__t('wait_upload')}</span>
                        </div>
                    </div>
                `;
            }).join('');
        }
        
        // 重置总进度
        this.updatePatientOverallProgress(0, __t('prepare_upload_dots'));
        
        // 绑定关闭按钮
        const closeBtn = document.getElementById('closeProgressPanel');
        if (closeBtn) {
            closeBtn.onclick = () => {
                panel.style.display = 'none';
            };
        }
        
        panel.style.display = 'block';
    }
    
    /**
     * 更新WSI进度项状态
     */
    updateWsiProgressItem(index, status, text) {
        const item = document.querySelector(`.wsi-progress-item[data-index="${index}"]`);
        if (!item) return;
        
        const badge = item.querySelector('.status-badge');
        if (badge) {
            badge.className = `status-badge ${status}`;
            badge.textContent = text;
        }
    }
    
    /**
     * 更新Patient Level总进度
     */
    updatePatientOverallProgress(percentage, text) {
        const progressFill = document.getElementById('patientOverallProgress');
        const progressText = document.getElementById('patientProgressText');
        const wsiCountDisplay = document.getElementById('patientWsiCountDisplay');
        
        if (progressFill) progressFill.style.width = `${percentage}%`;
        if (progressText) progressText.textContent = text;
    }

    async _routePendingPatientAnalysis(question, analysisMessage, isCurrentTurn) {
        const pending = this.patientAnalysisAwaitingQuestion;
        if (!pending) return false;
        const fileIds = (pending.uploadResults || []).map((result) => result.file_id);
        if (!(await this.ensureSpecimenSitesForFiles(fileIds, { required: true }))) {
            throw new Error(
                (window.__lang || 'zh') === 'en'
                    ? 'Please confirm the sampling site before patient-level analysis.'
                    : '开始患者级分析前，请先确认取材部位。'
            );
        }
        if (!isCurrentTurn()) return true;
        const response = await fetch('/api/agent/patient_route', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: pending.agentSessionId,
                file_ids: pending.uploadResults.map(result => result.file_id),
                file_infos: pending.allFileInfos,
                total_expected_wsi: pending.totalFiles,
                diagnostic_question: question,
                // A prior run's long assistant report would make the primary
                // router collapse this into action=chat and skip strategy cards.
                history: [],
                lang: window.__lang || 'zh',
            }),
        });
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error(result.error || 'Patient route request failed');
        }
        if (!isCurrentTurn()) return true;
        this.updateMessageStream(analysisMessage, result.reply || (
            (window.__lang || 'zh') === 'en'
                ? 'I need one more detail before starting this patient-level analysis.'
                : '开始患者级分析前，我还需要确认一个细节。'
        ));
        if (result.action !== 'analyze_patient') {
            // Clarification or a non-visual reply does not consume the frozen
            // patient scope.  The next plain-text turn may still launch it.
            return true;
        }

        const strategyChoice = await this.requestPatientLevelStrategy(
            result.swarm_reason || '',
            '',
            result.swarm_recommended === true
        );
        if (!isCurrentTurn() || !strategyChoice || strategyChoice.cancelled) {
            return true;
        }
        strategyChoice.swarmConsentId = result.swarm_consent_id || '';
        strategyChoice.swarmConsentGranted =
            strategyChoice.analysisStrategy === 'agent_swarm';

        const started = await this.startPatientLevelAnalysis({
            diagnosticQuestion: question,
            strategyChoice,
            frozenScope: pending,
        });
        if (started) this.patientAnalysisAwaitingQuestion = null;
        return true;
    }
    
    /**
     * 启动Patient Level分析
     */
    async startPatientLevelAnalysis(options = {}) {
        const frozenScope = options.frozenScope || null;
        const uploadResults = frozenScope?.uploadResults || this.patientLevelUploadResults;
        const folderName = frozenScope?.folderName || this.patientLevelFolderName;
        
        if (!uploadResults || uploadResults.length === 0) {
            this.showNotification('error', (window.__lang||'zh')==='en' ? 'No files to analyze' : '没有可分析的文件');
            return false;
        }
        this.plAnalysisStartedEarly = false;
        
        const totalFiles = frozenScope?.totalFiles
            || this.pendingPatientLevelFiles?.length
            || uploadResults.length;
        const isEarlyStart = frozenScope?.isEarlyStart ?? (
            !this.plUploadComplete && uploadResults.length < totalFiles
        );
        const agentSessionId = frozenScope?.agentSessionId || (window.pathChatAgent
            ? window.pathChatAgent.sessionId
            : ('session_' + Date.now()));
        const allFileInfos = frozenScope?.allFileInfos || (this.plAllFileInfos || []).map(info => ({
            file_id: info.file_id,
            filename: info.filename,
            part: info.part,
            index: info.index,
        }));
        const diagnosticQuestion = String(options.diagnosticQuestion || '').trim();
        if (!diagnosticQuestion) {
            this.patientAnalysisAwaitingQuestion = {
                uploadResults: uploadResults.map(result => ({ ...result })),
                folderName,
                totalFiles,
                isEarlyStart,
                agentSessionId,
                allFileInfos: allFileInfos.map(info => ({ ...info })),
            };
            this.addMessage(
                'assistant',
                (window.__lang || 'zh') === 'en'
                    ? 'The patient slides are ready. Please ask the concrete patient-level pathology question in the chat. I will then ask you to choose Agent Swarm or Single Agent per slide.'
                    : '患者切片已就绪。请在右侧聊天区提出本次患者级病理问题，随后请您选择 Agent Swarm 或每张切片单 Agent，以及自动/协作、并行/串行。'
            );
            this.elements.chatInput?.focus?.();
            return true;
        }
        const strategyChoice = options.strategyChoice;
        if (!strategyChoice?.analysisStrategy || strategyChoice.userSelectedRoute !== true) {
            this.showNotification(
                'error',
                (window.__lang || 'zh') === 'en'
                    ? 'Choose Agent Swarm or Single Agent on the chat cards before patient-level analysis can start.'
                    : '请先在聊天卡片上选择 Agent Swarm 或单 Agent，患者级分析才会开始。'
            );
            return false;
        }

        // Only after the user has chosen a route is this an active Patient run.
        this.plAnalysisStartedEarly = isEarlyStart;
        const currentMode = strategyChoice.analysisMode === 'collaborative'
            ? 'collaborative'
            : 'ai';
        this.setPatientLevelMode(currentMode);
        this.setPatientWsiExecutionMode(
            strategyChoice.wsiExecutionMode === 'serial' ? 'serial' : 'parallel'
        );
        console.log(
            `[Patient Level] 启动分析: strategy=${strategyChoice.analysisStrategy}, ` +
            `mode=${currentMode}, schedule=${this.patientWsiExecutionMode}, ` +
            `已上传 ${uploadResults.length}/${totalFiles}` +
            `${isEarlyStart ? ' (提前启动)' : ''}`
        );

        // ⭐⭐⭐ 分析前缓存预检：批量检查历史分析结果 ⭐⭐⭐
        const isCollabMode = currentMode === 'collaborative';
        let cacheAction = 'rerun_all';
        let skipFileIds = [];

        // ⭐ 断点续传：如果之前已从历史加载了部分切片，直接跳过缓存弹窗
        if (this.plLoadedFromCacheFileIds && this.plLoadedFromCacheFileIds.length > 0) {
            skipFileIds = [...this.plLoadedFromCacheFileIds];
            cacheAction = 'use_selected';
            this.plLoadedFromCacheFileIds = null;
            console.log(`[Patient Level] 断点续传：跳过 ${skipFileIds.length} 张已加载的缓存切片`);
        } else if (strategyChoice.analysisStrategy !== 'single_agent_roam') {
            try {
                const fileIds = uploadResults.map(r => r.file_id);
                const filenameMap = {};
                uploadResults.forEach(r => { filenameMap[r.file_id] = r.filename || ''; });

                const cacheResp = await fetch('/api/batch_check_analysis_cache', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ file_ids: fileIds, filenames: filenameMap, mode: currentMode })
                });
                const cacheResult = await cacheResp.json();

                if (cacheResult.success && cacheResult.cached_count > 0) {
                    const choice = await this._showAnalysisLoadOptionsDialog(cacheResult.cached_files);

                    if (!choice) {
                        console.log('[Patient Level] 用户取消了加载方式选择');
                        return;
                    }

                    if (choice.action === 'load_patient_history') {
                        // Patient Level：使用按运行分组的历史选择弹窗
                        await this._loadPatientLevelWithHistory();
                        return;
                    } else if (choice.action === 'load_selected') {
                        skipFileIds = choice.selectedIds;
                        cacheAction = 'use_selected';
                        if (skipFileIds.length === fileIds.length) {
                            this._plCacheDecision = 'skip_all';
                            await this.loadPatientLevelAnalysisState();
                            return;
                        }
                    } else if (choice.action === 'load_json') {
                        await this.loadFromLocalJsonFile();
                        return;
                    }
                    // 'rerun_all' falls through to normal analysis
                }
            } catch (err) {
                console.warn('[Patient Level] 缓存预检失败，继续正常分析:', err);
            }
        } else {
            // A Single-Agent trajectory is a different evidence object from an
            // R-B-G Swarm tree; never silently reuse a Swarm cache for it.
            cacheAction = 'rerun_all';
            skipFileIds = [];
        }

        // 记录前端已做缓存决定，后端不再弹窗
        this._plCacheDecision = cacheAction;
        
        // ⭐ 断点续传：如果已有 currentPatientId（从历史加载），复用它；否则生成新的
        let patientId;
        if (this.currentPatientId && cacheAction === 'use_selected') {
            patientId = this.currentPatientId;
            console.log(`[Patient Level] 断点续传：复用 patient_id = ${patientId}`);
        } else {
            const rawFolder = this.patientLevelFolderName || 'unknown';
            const safeFolder = rawFolder.replace(/[^\w\u4e00-\u9fff\-]/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '').substring(0, 30) || 'unknown';
            let plRunNum = 1;
            try {
                const cntResp = await fetch(`/api/count-output-dirs?prefix=patient_${encodeURIComponent(safeFolder)}`);
                const cntResult = await cntResp.json();
                plRunNum = (cntResult.max_num || cntResult.count || 0) + 1;
            } catch (e) {
                console.warn('[Patient Level] count-output-dirs failed, using num1:', e);
            }
            patientId = `patient_${safeFolder}_num${plRunNum}`;
            this.currentPatientId = patientId;
        }
        
        // 更新状态
        const statusEl = document.getElementById('plStatus');
        if (statusEl) statusEl.textContent = isEarlyStart ? __t('analyzing_uploading') : __t('analyzing');
        
        // 禁用开始按钮
        const startBtn = document.getElementById('plStartAnalysis');
        if (startBtn) {
            startBtn.disabled = true;
            startBtn.classList.add('running');
            startBtn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${__t('pl_analysis_running')}`;
        }
        this.setPatientLevelModeLocked(true);
        
        // 准备请求数据
        console.log('[Patient Level] 使用 session_id:', agentSessionId);
        
        const requestData = {
            patient_id: patientId,
            session_id: agentSessionId,
            file_ids: uploadResults.map(r => r.file_id),
            file_infos: allFileInfos,
            total_expected_wsi: totalFiles,
            folder_name: folderName,
            mode: currentMode,
            wsi_execution_mode: this.patientWsiExecutionMode,
            analysis_strategy: strategyChoice.analysisStrategy,
            collaboration_review_policy:
                strategyChoice.collaborationReviewPolicy || null,
            swarm_consent_id: strategyChoice.swarmConsentId || '',
            swarm_consent_granted: strategyChoice.swarmConsentGranted === true,
            diagnostic_question: diagnosticQuestion,
            lang: window.__lang || 'zh',
            cache_action: cacheAction,
            skip_file_ids: skipFileIds
        };
        
        try {
            this.resetPatientSlideNavStates();
            this.markPatientSlidesForRunStart();
            
            this.updatePlProgress(35, __t('launch_analysis'));
            
            const response = await fetch('/api/agent/run_patient_level', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(requestData)
            });
            
            const result = await response.json();
            
            if (result.success) {
                // ⭐ 尽早设置 Patient Level 标志，防止单张缓存弹窗抢先触发
                if (window.pathChatAgent) {
                    window.pathChatAgent.isPatientLevelMode = true;
                    window.pathChatAgent.activeAnalysisStrategy = (
                        result.analysis_strategy
                        || strategyChoice.analysisStrategy
                        || 'agent_swarm'
                    );
                    window.pathChatAgent._setEvidencePaneMode?.(
                        window.pathChatAgent.activeAnalysisStrategy
                    );
                    window.pathChatAgent.patientWsiExecutionMode = (
                        result.wsi_execution_mode
                        || this.patientWsiExecutionMode
                        || 'parallel'
                    );
                    window.pathChatAgent.collaborationReviewPolicy = (
                        strategyChoice.collaborationReviewPolicy
                        || (isCollabMode
                            ? (strategyChoice.analysisStrategy === 'single_agent_roam'
                                ? 'final'
                                : 'staged')
                            : null)
                    );
                    // An idempotent retry (created=false) is still the
                    // authoritative response for this execution.  Install its
                    // token without clearing an already restored trajectory.
                    if (result.created !== false) {
                        window.pathChatAgent.beginPatientLevelRun?.(
                            patientId,
                            result.run_token || ''
                        );
                    } else {
                        window.pathChatAgent.bindPatientLevelRun?.(
                            patientId,
                            result.run_token || ''
                        );
                    }
                }
                this._setPatientLevelRunState(true, {
                    patientId,
                    sessionId: agentSessionId,
                });
                this.ensurePatientProgressVisible();
                this.markPatientSlidesForRunStart();
                // The HTTP 200 can race a fail-closed worker stop.  Reconcile
                // from the authoritative snapshot so the toolbar does not stay
                // "running" after an immediate cancellation.
                window.pathChatAgent?.restorePatientLevelSnapshot?.(patientId);
                this.showNotification('success', (window.__lang||'zh')==='en' ? 'Patient Level analysis started' : 'Patient Level分析已启动');
                this.updatePlProgress(40, __t('analysis_in_progress'));
                
                // 在聊天中显示消息
                const isSingleRoam = strategyChoice.analysisStrategy === 'single_agent_roam';
                const modeLabel = isSingleRoam
                    ? ((window.__lang || 'zh') === 'en'
                        ? (isCollabMode
                            ? 'Single Agent roam · collaborative · confirm each slide'
                            : 'Single Agent roam · one independent route per slide')
                        : (isCollabMode
                            ? '单 Agent 游走 · 人机协作 · 每张结束后确认'
                            : '单 Agent 游走 · 每张切片一条独立路线'))
                    : (isCollabMode ? __t('collab_mode_label') : __t('auto_mode_label'));
                const modeHint = isSingleRoam
                    ? ((window.__lang || 'zh') === 'en'
                        ? (isCollabMode
                            ? 'No ROI Subagents and no path tree. Collaboration is the pathologist review after each roam — confirm that slide before the patient-level report.'
                            : 'No ROI Subagents will be dispatched. The patient report will aggregate the completed slide trajectories.')
                        : (isCollabMode
                            ? '不会派发 ROI Subagent，也不会出现分析路径树。协作闸门是每张游走结束后的「病理医生复核 / 确认该切片」。'
                            : '不会派发 ROI Subagent；患者报告将汇总各切片已完成的游走轨迹。'))
                    : (isCollabMode
                        ? (
                            strategyChoice.collaborationReviewPolicy === 'final'
                                ? (__t('pl_collab_final_hint') || __t('collab_confirm_hint'))
                                : (__t('pl_collab_staged_hint') || __t('collab_confirm_hint'))
                        )
                        : (isEarlyStart
                            ? __t('auto_hint_early')
                            : (this.patientWsiExecutionMode === 'parallel'
                                ? __t('auto_hint_parallel')
                                : __t('auto_hint'))));
                const earlyTag = isEarlyStart ? __t('pl_early_start', uploadResults.length, totalFiles) : '';
                const scheduleLabel = this.patientWsiExecutionMode === 'serial'
                    ? ((window.__lang || 'zh') === 'en' ? 'Serial' : '串行')
                    : ((window.__lang || 'zh') === 'en' ? 'Parallel' : '并行');
                const navHint = this.patientWsiExecutionMode === 'serial'
                    ? __t('pl_serial_nav_hint')
                    : __t('pl_parallel_sync_hint');
                this.addChatMessage('assistant', `
                    <div class="patient-level-start-msg">
                        <h4>🔬 ${__t('pl_analysis_started')}</h4>
                        <p><strong>${__t('pl_folder_label')}:</strong> ${folderName}</p>
                        <p><strong>${__t('pl_slide_count_label')}:</strong> ${totalFiles} ${__t('pl_wsi_suffix')}${earlyTag}</p>
                        <p><strong>${__t('pl_mode_label')}:</strong> ${modeLabel}</p>
                        <p><strong>${__t('pl_schedule_label')}:</strong> ${scheduleLabel}</p>
                        <p class="hint">${modeHint}</p>
                        <p class="pl-schedule-nav-hint">${navHint}</p>
                    </div>
                `);
                return true;
            } else {
                this.plAnalysisStartedEarly = false;
                this.showNotification('error', `${__t('pl_failed_start')}: ${result.error}`);
                if (startBtn) {
                    startBtn.disabled = false;
                    startBtn.classList.remove('running');
                    startBtn.innerHTML = `<i class="fas fa-play"></i> ${__t('pl_retry')}`;
                }
                this.setPatientLevelModeLocked(false);
                this._setPatientLevelRunState(false);
                this.resetPatientSlideNavStates();
                this.syncPatientSlideReportNav();
                return false;
            }
            
        } catch (error) {
            this.plAnalysisStartedEarly = false;
            console.error('启动Patient Level分析失败:', error);
            this.showNotification('error', (window.__lang||'zh')==='en' ? `Failed to start analysis: ${error.message}` : `启动分析失败: ${error.message}`);
            if (startBtn) {
                startBtn.disabled = false;
                startBtn.classList.remove('running');
                startBtn.innerHTML = `<i class="fas fa-play"></i> ${__t('pl_retry')}`;
            }
            this.setPatientLevelModeLocked(false);
            this._setPatientLevelRunState(false);
            this.resetPatientSlideNavStates();
            this.syncPatientSlideReportNav();
            return false;
        }
    }

    _setPatientLevelRunState(isRunning, { patientId = '', sessionId = '', stopping = false } = {}) {
        this.patientIsRunning = Boolean(isRunning);
        this.patientStopRequested = Boolean(stopping);
        // The mode and scheduling controls describe the accepted run contract.
        // Reconnect/refresh restoration must lock them just like the first
        // launch, otherwise the UI can drift from the authoritative backend.
        this.setPatientLevelModeLocked(Boolean(isRunning));
        if (patientId) this.currentPatientId = patientId;
        if (sessionId) this.patientRunSessionId = sessionId;
        const startBtn = document.getElementById('plStartAnalysis');
        const stopBtn = document.getElementById('plStopAnalysis');
        const restoreFocusToStart = !isRunning && stopBtn && document.activeElement === stopBtn;
        if (startBtn) {
            startBtn.disabled = Boolean(isRunning);
            startBtn.classList.toggle('running', Boolean(isRunning));
            startBtn.setAttribute('aria-busy', isRunning ? 'true' : 'false');
            if (!isRunning) {
                startBtn.innerHTML = `<i class="fas fa-play"></i> ${__t('pl_start_analysis')}`;
            }
        }
        if (stopBtn) {
            stopBtn.hidden = !isRunning;
            stopBtn.disabled = Boolean(stopping);
            stopBtn.setAttribute('aria-busy', stopping ? 'true' : 'false');
            const stopLabel = stopping
                ? ((window.__lang || 'zh') === 'en' ? 'Finishing…' : '结束中…')
                : (__t('pl_stop_short') || ((window.__lang || 'zh') === 'en' ? 'Stop' : '停止'));
            const stopAria = stopping
                ? ((window.__lang || 'zh') === 'en' ? 'Finishing current request' : '正在结束当前请求')
                : ((window.__lang || 'zh') === 'en' ? 'Stop patient analysis' : '停止患者分析');
            stopBtn.innerHTML = stopping
                ? `<i class="fas fa-spinner fa-spin"></i><span>${stopLabel}</span>`
                : `<i class="fas fa-stop"></i><span>${stopLabel}</span>`;
            stopBtn.setAttribute('aria-label', stopAria);
            stopBtn.title = stopAria;
        }
        if (!isRunning) {
            this.patientRunSessionId = '';
            if (restoreFocusToStart && startBtn && !startBtn.hidden) {
                window.requestAnimationFrame(() => startBtn.focus({ preventScroll: true }));
            }
        }
        try {
            const storage = window.sessionStorage;
            if (storage && isRunning && this.currentPatientId) {
                storage.setItem('pathcopilot.patient.active_id', this.currentPatientId);
                storage.setItem('pathcopilot.patient.active_session', this.patientRunSessionId || '');
            } else if (storage && !isRunning) {
                storage.removeItem('pathcopilot.patient.active_id');
                storage.removeItem('pathcopilot.patient.active_session');
            }
        } catch (_) {
            // Storage is an optional reconnect aid; live analysis must continue.
        }
        if (isRunning) {
            this.markPatientSlidesForRunStart();
        } else {
            this.syncPatientSlideReportNav();
        }
        window.pathChatAgent?.updateButtons?.();
    }

    _announcePatientStopStatus(message, { alert = false } = {}) {
        const live = document.getElementById('plStopLiveStatus');
        if (!live) return;
        live.setAttribute('role', alert ? 'alert' : 'status');
        live.setAttribute('aria-live', alert ? 'assertive' : 'polite');
        // Clear first so repeated state transitions are announced reliably.
        live.textContent = '';
        window.requestAnimationFrame(() => {
            live.textContent = String(message || '');
        });
    }

    async stopPatientLevelAnalysis() {
        if (this.patientAnalysisAwaitingQuestion && !this.patientIsRunning) {
            this.patientAnalysisAwaitingQuestion = null;
            this.addMessage(
                'assistant',
                (window.__lang || 'zh') === 'en'
                    ? 'Patient-level analysis setup was cancelled; no Agent was started.'
                    : '已取消患者级分析准备；尚未启动任何 Agent。'
            );
            return;
        }
        if (!this.patientIsRunning || !this.currentPatientId || this.patientStopRequested) return;
        const sessionId = this.patientRunSessionId || window.pathChatAgent?.sessionId || '';
        const requestedRunToken = String(window.pathChatAgent?._activePatientRunToken || '');
        window.pathChatAgent?.pausePatientRoamForStop?.();
        this._setPatientLevelRunState(true, {
            patientId: this.currentPatientId,
            sessionId,
            stopping: true,
        });
        try {
            const response = await fetch(
                `/api/patient/${encodeURIComponent(this.currentPatientId)}/stop`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ session_id: sessionId }),
                }
            );
            const result = await response.json();
            if (!response.ok || !result.success) {
                throw new Error(result.error || 'Stop request failed');
            }
            const resultRunToken = String(result.run_token || '');
            if (requestedRunToken && resultRunToken !== requestedRunToken) {
                console.warn('[Patient Level] Ignored stale stop response', {
                    requestedRunToken,
                    resultRunToken,
                });
                this.patientStopRequested = false;
                window.pathChatAgent?.restorePatientRoamAfterStopFailure?.();
                this._setPatientLevelRunState(true, {
                    patientId: this.currentPatientId,
                    sessionId,
                    stopping: false,
                });
                const staleMessage = (window.__lang || 'zh') === 'en'
                    ? 'The stop response did not match the active run. Please retry.'
                    : '停止响应与当前运行不匹配，请重试。';
                this.showNotification('error', staleMessage);
                this._announcePatientStopStatus(staleMessage, { alert: true });
                return;
            }
            if (result.already_terminal) {
                const terminalStatus = String(result.status || '').toLowerCase();
                if (terminalStatus === 'stopped' || terminalStatus === 'cancelled') {
                    window.pathChatAgent?.handlePatientStopped?.({
                        patient_id: this.currentPatientId,
                        run_token: resultRunToken,
                        status: terminalStatus,
                    });
                } else {
                    this._setPatientLevelRunState(false, { patientId: this.currentPatientId });
                    const terminalText = terminalStatus === 'completed'
                        ? ((window.__lang || 'zh') === 'en' ? 'Analysis already completed.' : '分析已完成。')
                        : ((window.__lang || 'zh') === 'en' ? `Analysis is already ${terminalStatus || 'finished'}.` : `分析已处于${terminalStatus || '终止'}状态。`);
                    this.updatePlProgress(100, terminalText);
                    this._announcePatientStopStatus(terminalText);
                }
                return;
            }
            this.updatePlProgress(0, (window.__lang || 'zh') === 'en'
                ? 'Finishing the current model request…'
                : '正在结束当前模型请求，之后不会继续生成患者级报告…');
            this._announcePatientStopStatus((window.__lang || 'zh') === 'en'
                ? 'Stop accepted. Finishing the current model request.'
                : '停止请求已接受，正在结束当前模型请求。');
        } catch (error) {
            this.patientStopRequested = false;
            window.pathChatAgent?.restorePatientRoamAfterStopFailure?.();
            this._setPatientLevelRunState(true, {
                patientId: this.currentPatientId,
                sessionId,
                stopping: false,
            });
            this.showNotification(
                'error',
                (window.__lang || 'zh') === 'en'
                    ? `Unable to stop: ${error.message}`
                    : `停止失败：${error.message}`
            );
            this._announcePatientStopStatus(
                (window.__lang || 'zh') === 'en'
                    ? `Unable to stop: ${error.message}`
                    : `停止失败：${error.message}`,
                { alert: true }
            );
        }
    }
    
    /**
     * 添加聊天消息（简易版本）
     */
    addChatMessage(role, content) {
        const chatMessages = this.elements.chatMessages;
        if (!chatMessages) return;
        
        const messageEl = document.createElement('div');
        messageEl.className = `message ${role}-message`;
        messageEl.innerHTML = `
            <div class="message-content">
                ${content}
            </div>
        `;
        chatMessages.appendChild(messageEl);
        this._tagPatientChatArtifacts(messageEl);
        this._schedulePatientSlideReportNavSync();
        
        // 滚动到底部
        chatMessages.scrollTop = chatMessages.scrollHeight;
        return messageEl;
    }
    
    // ================== End Patient Level 多文件处理 ==================
    
    /**
     * 加载文件列表
     */
    async loadFilesList(options = {}) {
        try {
            const response = await fetch('/api/files');
            const result = await response.json();
            
            this.renderFilesList(result.files);
            if (options.restoreActiveRuntime === true) {
                await this._restoreActiveRuntimeFile(result.files || []);
            }
            
        } catch (error) {
            console.error('加载文件列表失败:', error);
            this.showNotification('error', (window.__lang||'zh')==='en' ? 'Failed to load file list' : '加载文件列表失败');
        }
    }

    async _restoreActiveRuntimeFile(files = []) {
        if (this._activeRuntimeAutoRestoreAttempted) return;
        this._activeRuntimeAutoRestoreAttempted = true;
        let runId = '';
        let fileId = '';
        try {
            runId = String(sessionStorage.getItem('pathcopilot.runtime.v2.run_id') || '');
            fileId = String(sessionStorage.getItem('pathcopilot.runtime.v2.run_file_id') || '');
        } catch (_) {}
        if (!runId || !fileId || !files.some(file => String(file.file_id || '') === fileId)) return;
        try {
            const statusResponse = await fetch(
                `/api/agent/runtime/${encodeURIComponent(runId)}/status`,
                { cache: 'no-store' }
            );
            if (!statusResponse.ok) return;
            const status = await statusResponse.json();
            const state = String(status?.state || status?.status || '').toLowerCase();
            if (['succeeded', 'failed', 'cancelled', 'completed', 'stopped'].includes(state)) return;
            await this.loadWSI(fileId);
        } catch (error) {
            console.debug('Active Runtime file auto-restore skipped:', error);
        }
    }
    
    /**
     * 渲染文件列表
     */
    renderFilesList(files) {
        if (files.length === 0) {
            this.elements.filesList.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-file-medical"></i>
                    <p>${__t('no_files')}</p>
                </div>
            `;
            return;
        }
        
        this._uploadedFilesById = {};
        files.forEach((file) => {
            if (file?.file_id) this._uploadedFilesById[file.file_id] = file;
        });
        this.elements.filesList.innerHTML = files.map(file => `
            <div class="file-item ${this.currentFile && this.currentFile.file_id === file.file_id ? 'active' : ''}" 
                 data-file-id="${file.file_id}">
                <div class="file-header">
                    <img src="${file.thumbnail_url}" alt="thumbnail" class="file-thumbnail" 
                         onerror="this.src='data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNDgiIGhlaWdodD0iNDgiIHZpZXdCb3g9IjAgMCA0OCA0OCIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHJlY3Qgd2lkdGg9IjQ4IiBoZWlnaHQ9IjQ4IiBmaWxsPSIjRjNGNEY2Ii8+CjxwYXRoIGQ9Ik0yNCAzMkMyOC40MTgzIDMyIDMyIDI4LjQxODMgMzIgMjRDMzIgMTkuNTgxNyAyOC40MTgzIDE2IDI0IDE2QzE5LjU4MTcgMTYgMTYgMTkuNTgxNyAxNiAyNEMxNiAyOC40MTgzIDE5LjU4MTcgMzIgMjQgMzJaIiBmaWxsPSIjOUNBM0FGIi8+CjwvcmVnPgo='">
                    <div class="file-info">
                        <h5>${file.filename}</h5>
                        <div class="file-meta">
                            <span><i class="fas fa-expand-arrows-alt"></i> ${file.dimensions[0]}×${file.dimensions[1]}</span>
                            <span><i class="fas fa-layer-group"></i> ${file.level_count} ${(window.__lang||'zh')==='en'?'Levels':'层级'}</span>
                            <span><i class="fas fa-weight-hanging"></i> ${this.formatFileSize(file.file_size)}</span>
                            <span><i class="fas fa-clock"></i> ${this.formatTime(file.upload_time)}</span>
                            ${this._specimenSiteMetaHtml(file)}
                        </div>
                    </div>
                </div>
                <div class="file-actions">
                    <button class="action-btn" title="${(window.__lang||'zh')==='en'?'View':'查看'}" onclick="pathchat.loadWSI('${file.file_id}')">
                        <i class="fas fa-eye"></i>
                    </button>
                    <button class="action-btn danger" title="${(window.__lang||'zh')==='en'?'Delete':'删除'}" onclick="pathchat.deleteFile('${file.file_id}')">
                        <i class="fas fa-trash"></i>
                    </button>
                </div>
            </div>
        `).join('');
        
        // 添加点击事件
        this.elements.filesList.querySelectorAll('.file-item').forEach(item => {
            item.addEventListener('click', (e) => {
                if (!e.target.closest('.file-actions')) {
                    const fileId = item.dataset.fileId;
                    this.loadWSI(fileId);
                }
            });
        });
    }
    
    /**
     * 加载WSI文件到查看器
     */
    _nextWsiLoadId() {
        this._wsiLoadSerial = (this._wsiLoadSerial || 0) + 1;
        return this._wsiLoadSerial;
    }

    _isAbortError(error) {
        const name = String(error?.name || '');
        const message = String(error?.message || '');
        return name === 'AbortError'
            || /signal is aborted|aborted without reason|The operation was aborted/i.test(message);
    }

    async _fetchJsonWithTimeout(url, ms = 8000) {
        const ctrl = new AbortController();
        const timer = setTimeout(() => {
            try {
                ctrl.abort('timeout');
            } catch (_err) {
                ctrl.abort();
            }
        }, ms);
        try {
            const resp = await fetch(url, { signal: ctrl.signal });
            return await resp.json();
        } catch (error) {
            if (this._isAbortError(error)) {
                const timeout = new Error('timeout');
                timeout.name = 'AbortError';
                timeout.code = 'timeout';
                throw timeout;
            }
            throw error;
        } finally {
            clearTimeout(timer);
        }
    }

    _applyLoadedWsi(fileId, fileInfo, dziUrl) {
        this.clearAllROIOverlays();
        this.initViewer(dziUrl, fileInfo);
        this.currentFile = fileInfo;
        // viewer 标题以正在查看的切片为准（并行时「正在分析 X」标签不再可靠）。
        window.PathCopilotV2?.setSlideTitle?.(fileInfo?.filename || fileId);
        this.updateFileListSelection(fileId);
        this.hideViewerEmpty();
        this.showViewerInfo();
        this.updateImageInfo(fileInfo);
    }

    _cachedDziUrl(fileId, fileInfo = null) {
        const cacheId = fileInfo?.dzi_cache_id || fileId;
        return `/dzi/${fileId}/${cacheId}.dzi`;
    }

    async loadWSI(fileId, options = {}) {
        const loadId = this._nextWsiLoadId();
        const silent = options.silent === true
            || Boolean(window.pathChatAgent?.isPatientLevelMode);
        const known = options.fileInfo
            || (this.patientLevelUploadResults || []).find((f) => f?.file_id === fileId)
            || (this.currentFile?.file_id === fileId ? this.currentFile : null);
        const meta = this._patientFileMetaById?.[fileId] || {};
        try {
            if (this.currentFile?.file_id && this.currentFile.file_id !== fileId) {
                this._cancelPendingAnalysisChoice('file_changed');
                this._invalidateHistoryChooser('file_changed');
            }
            if (!silent) {
                this.showLoading(__t('loading_wsi'));
            }
            this.updateStatus('processing');
            
            // ⭐⭐⭐ 仅在非 Patient Level 模式下清除患者状态 ⭐⭐⭐
            // Patient Level 模式下 selectPlWsiItem 会调用 loadWSI 来切换查看不同切片，
            // 此时不能清除 currentPatientId，否则路径树无法加载患者级别数据
            if (!window.pathChatAgent?.isPatientLevelMode) {
                this.currentPatientId = null;
                this.patientTreeUrl = null;
                console.log('🧹 [单张WSI模式] 已清除 Patient Level 状态');
            }

            let dziUrl = null;
            let fileInfo = known ? { ...known, file_id: fileId } : null;
            const canUseLocalDzi = Boolean(
                fileInfo && (fileInfo.dzi_ready || fileInfo.dzi_generated || fileInfo.dimensions)
            );
            if (canUseLocalDzi) {
                dziUrl = this._cachedDziUrl(fileId, fileInfo);
            } else {
                const dziResult = await this._fetchJsonWithTimeout(
                    `/api/generate_dzi/${fileId}`,
                    silent ? 6000 : 20000
                );
                if (!dziResult.success) {
                    throw new Error(dziResult.error);
                }
                dziUrl = dziResult.dzi_url;
                const filesResult = await this._fetchJsonWithTimeout(
                    '/api/files',
                    silent ? 6000 : 20000
                );
                fileInfo = (filesResult.files || []).find((f) => f.file_id === fileId);
            }
            
            if (!fileInfo) {
                throw new Error('文件信息不存在');
            }
            if (loadId !== this._wsiLoadSerial) return;
            
            this._applyLoadedWsi(fileId, fileInfo, dziUrl);
            console.log('🧹 已清空旧的 overlays');
            
            const inPatientLevel = Boolean(
                window.pathChatAgent?.isPatientLevelMode
                || this.pendingPatientLevelFiles?.length
                || this.patientLevelUploadResults?.length
            );
            if (!inPatientLevel) {
                this.showNotification('success', (window.__lang||'zh')==='en' ? `Loaded: ${fileInfo.filename}` : `已加载：${fileInfo.filename}`);
                this.checkAndLoadAnalysisCache(fileId);
            }
            
        } catch (error) {
            if (loadId !== this._wsiLoadSerial) return;
            // Parallel roam holds _openslide_lock; generate_dzi /api/files
            // then hits the 6s silent abort. That is not a failed slide.
            if (this._isAbortError(error) && fileId) {
                const fallback = known
                    ? { ...known, file_id: fileId }
                    : { file_id: fileId };
                this._applyLoadedWsi(fileId, fallback, this._cachedDziUrl(fileId, fallback));
                return;
            }
            console.error('加载WSI失败:', error);
            this.showNotification('error', (window.__lang||'zh')==='en' ? `Load failed: ${error.message}` : `加载失败：${error.message}`);
        } finally {
            if (loadId === this._wsiLoadSerial) {
                this.hideLoading();
                this.updateStatus('ready');
            }
        }
        if (this.currentFile && this.currentFile.file_id === fileId) {
            this.maybeAskSpecimenSite({ required: false, fileInfo: this.currentFile });
        }
    }

    _specimenSiteMetaHtml(file) {
        if (!file || file.is_demo) return '';
        if (file.user_specimen_declared && file.user_specimen_unknown) {
            return `<span><i class="fas fa-map-marker-alt"></i> ${this.escapeHtml(__t('specimen_site_unknown_short'))}</span>`;
        }
        if (file.user_specimen_declared && file.user_specimen_site) {
            return `<span><i class="fas fa-map-marker-alt"></i> ${this.escapeHtml(file.user_specimen_site)}</span>`;
        }
        return `<span><i class="fas fa-map-marker-alt"></i> ${this.escapeHtml(__t('specimen_site_missing'))}</span>`;
    }

    _applySpecimenSiteToLocalFile(fileId, payload) {
        const patch = {
            is_demo: payload.is_demo === true,
            needs_specimen_site: payload.needs_specimen_site === true,
            user_specimen_declared: payload.user_specimen_declared === true,
            user_specimen_unknown: payload.user_specimen_unknown === true,
            user_specimen_site: String(payload.user_specimen_site || '').trim(),
        };
        if (this.currentFile && this.currentFile.file_id === fileId) {
            Object.assign(this.currentFile, patch);
        }
        if (this._uploadedFilesById && this._uploadedFilesById[fileId]) {
            Object.assign(this._uploadedFilesById[fileId], patch);
        }
        (this.patientLevelUploadResults || []).forEach((entry) => {
            if (entry && entry.file_id === fileId) Object.assign(entry, patch);
        });
    }

    _fileNeedsSpecimenSite(file) {
        if (!file || !file.file_id || file.is_demo) return false;
        if (file.user_specimen_declared) return false;
        return file.needs_specimen_site !== false;
    }

    maybeAskSpecimenSite({ required = false, fileInfo = null } = {}) {
        const file = fileInfo || this.currentFile;
        if (!this._fileNeedsSpecimenSite(file)) return Promise.resolve(true);
        return this._promptSpecimenSite({ required, fileInfo: file });
    }

    async ensureSpecimenSiteDeclared({ required = false, fileInfo = null } = {}) {
        const file = fileInfo || this.currentFile;
        if (!file?.file_id) return true;
        if (!this._fileNeedsSpecimenSite(file)) return true;
        return this._promptSpecimenSite({ required, fileInfo: file });
    }

    async ensureSpecimenSitesForFiles(fileIds, { required = true } = {}) {
        const ids = [...new Set((fileIds || []).map((id) => String(id || '').trim()).filter(Boolean))];
        for (const fileId of ids) {
            const info = (this.currentFile?.file_id === fileId)
                ? this.currentFile
                : (this._uploadedFilesById || {})[fileId]
                || (this.patientLevelUploadResults || []).find((entry) => entry && entry.file_id === fileId)
                || { file_id: fileId };
            const ok = await this.ensureSpecimenSiteDeclared({ required, fileInfo: info });
            if (!ok) return false;
        }
        return true;
    }

    _specimenSiteChipLabels() {
        return (window.__lang || 'zh') === 'en'
            ? ['Uterus', 'Cervix', 'Ovary', 'Breast', 'Lung', 'Stomach', 'Colon', 'Prostate', 'Kidney', 'Liver', 'Skin', 'Lymph node']
            : ['子宫', '宫颈', '卵巢', '乳腺', '肺', '胃', '结肠', '前列腺', '肾', '肝', '皮肤', '淋巴结'];
    }

    _bindSpecimenSitePromptOnce() {
        if (this._specimenSiteBound) return;
        const overlay = document.getElementById('specimenSitePrompt');
        const input = document.getElementById('specimenSiteInput');
        const later = document.getElementById('specimenSiteLater');
        const unknown = document.getElementById('specimenSiteUnknown');
        const confirm = document.getElementById('specimenSiteConfirm');
        if (!overlay || !input || !later || !unknown || !confirm) return;
        this._specimenSiteBound = true;
        later.addEventListener('click', () => this._finishSpecimenSitePrompt('later'));
        unknown.addEventListener('click', () => this._finishSpecimenSitePrompt('unknown'));
        confirm.addEventListener('click', () => this._finishSpecimenSitePrompt('confirm'));
        input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                this._finishSpecimenSitePrompt('confirm');
            }
        });
        overlay.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                event.preventDefault();
                if (!this._specimenSiteRequired) this._finishSpecimenSitePrompt('later');
            }
        });
    }

    _promptSpecimenSite({ required = false, fileInfo = null } = {}) {
        const file = fileInfo || this.currentFile;
        if (!file?.file_id) return Promise.resolve(true);
        if (this._specimenSitePromise && this._specimenSiteFileId === file.file_id) {
            if (required && !this._specimenSiteRequired) {
                this._specimenSiteRequired = true;
                const laterBtn = document.getElementById('specimenSiteLater');
                if (laterBtn) laterBtn.hidden = true;
            }
            return this._specimenSitePromise;
        }
        this._bindSpecimenSitePromptOnce();
        const overlay = document.getElementById('specimenSitePrompt');
        const input = document.getElementById('specimenSiteInput');
        const later = document.getElementById('specimenSiteLater');
        const filenameEl = document.getElementById('specimenSiteFilename');
        const chipsEl = document.getElementById('specimenSiteChips');
        if (!overlay || !input) return Promise.resolve(!required);

        this._specimenSiteFileId = file.file_id;
        this._specimenSiteRequired = required === true;
        if (filenameEl) filenameEl.textContent = file.filename || file.file_id;
        if (later) later.hidden = this._specimenSiteRequired;
        input.value = String(file.user_specimen_site || '').trim();
        if (chipsEl) {
            chipsEl.innerHTML = '';
            this._specimenSiteChipLabels().forEach((label) => {
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.className = 'specimen-site-chip';
                chip.textContent = label;
                chip.addEventListener('click', () => {
                    input.value = label;
                    input.focus();
                });
                chipsEl.appendChild(chip);
            });
        }
        overlay.hidden = false;
        document.body.classList.add('specimen-site-open');
        window.requestAnimationFrame(() => input.focus());

        this._specimenSitePromise = new Promise((resolve) => {
            this._specimenSiteResolve = resolve;
        });
        return this._specimenSitePromise;
    }

    async _finishSpecimenSitePrompt(action) {
        const overlay = document.getElementById('specimenSitePrompt');
        const input = document.getElementById('specimenSiteInput');
        const fileId = this._specimenSiteFileId;
        const resolve = this._specimenSiteResolve;
        const required = this._specimenSiteRequired === true;
        if (!fileId || !resolve) return;

        if (action === 'later') {
            if (required) return;
            this._closeSpecimenSitePrompt(true);
            return;
        }

        const body = action === 'unknown'
            ? { unknown: true, lang: window.__lang || 'zh' }
            : { site: String(input?.value || '').trim(), lang: window.__lang || 'zh' };
        if (action === 'confirm' && !body.site) {
            this.showNotification(
                'warning',
                (window.__lang || 'zh') === 'en'
                    ? 'Enter a sampling site, or choose Unknown.'
                    : '请填写取材部位，或选择暂不清楚。'
            );
            input?.focus();
            return;
        }

        try {
            const response = await fetch(`/api/files/${encodeURIComponent(fileId)}/specimen_site`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const result = await response.json();
            if (!response.ok || !result.success) {
                throw new Error(result.error || 'Failed to save sampling site');
            }
            this._applySpecimenSiteToLocalFile(fileId, result);
            this._closeSpecimenSitePrompt(true);
        } catch (error) {
            this.showNotification(
                'error',
                (window.__lang || 'zh') === 'en'
                    ? `Could not save sampling site: ${error.message}`
                    : `取材部位保存失败：${error.message}`
            );
            if (!required) this._closeSpecimenSitePrompt(false);
        }
    }

    _closeSpecimenSitePrompt(ok) {
        const overlay = document.getElementById('specimenSitePrompt');
        if (overlay) overlay.hidden = true;
        document.body.classList.remove('specimen-site-open');
        const resolve = this._specimenSiteResolve;
        this._specimenSitePromise = null;
        this._specimenSiteResolve = null;
        this._specimenSiteFileId = '';
        this._specimenSiteRequired = false;
        if (typeof resolve === 'function') resolve(ok !== false);
    }
    
    /**
     * Load a pre-placed demo WSI from the server (no upload needed).
     */
    async loadDemo(demoId) {
        try {
            this.showLoading(__t('loading_demo'));
            const response = await fetch('/api/load_demo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ demo_id: demoId })
            });
            const result = await response.json();
            if (result.success) {
                await this.loadFilesList();
                await this.loadWSI(result.file_id);
            } else {
                throw new Error(result.error);
            }
        } catch (error) {
            console.error('loadDemo failed:', error);
            this.showNotification('error', error.message);
        } finally {
            this.hideLoading();
        }
    }

    _isLiveAnalysisBlockingHistoryChooser(fileId = '') {
        const agent = window.pathChatAgent;
        if (!agent) return false;
        if (agent.isRunning || agent.isStarting || agent.isStopping) return true;
        const runId = String(agent.runtimeRunId || '').trim();
        if (!runId) return false;
        const runFile = String(agent.runtimeRunFileId || '').trim();
        if (!fileId) return true;
        return !runFile || runFile === String(fileId);
    }

    _invalidateHistoryChooser(reason = '') {
        this._historyChooserGeneration = (this._historyChooserGeneration || 0) + 1;
        document.querySelectorAll('.history-results-chooser').forEach((el) => el.remove());
        if (reason) console.log(`[HistoryChooser] aborted: ${reason}`);
    }

    /**
     * 检查并加载分析缓存
     * ⭐ 支持按模式检查缓存（协作模式/自动模式）
     */
    async checkAndLoadAnalysisCache(fileId) {
        const chooserGen = this._historyChooserGeneration || 0;
        const stale = () => (
            chooserGen !== (this._historyChooserGeneration || 0)
            || this._isLiveAnalysisBlockingHistoryChooser(fileId)
        );
        try {
            // A live Runtime is the authoritative state for this slide.  On a
            // refresh/re-entry, do not cover its pending workflow/approval UI
            // with an unrelated historical-cache chooser.
            if (stale()) {
                console.log('Active analysis running; historical analysis chooser suppressed.');
                return;
            }
            const plPanel = document.getElementById('patientLevelPanel');
            const isInPatientLevel = window.pathChatAgent?.isPatientLevelMode || 
                                     (plPanel && plPanel.style.display !== 'none');
            if (isInPatientLevel) {
                console.log('📭 [Patient Level] 跳过单张缓存检查（由 Patient Level 缓存机制统一管理）');
                return;
            }
            
            const isCollaborativeMode = window.pathChatAgent?.isCollaborativeMode || false;
            const isSingleAgentRoam = window.pathChatAgent?.activeAnalysisStrategy === 'single_agent_roam';
            const mode = isSingleAgentRoam
                ? 'single_agent_roam'
                : (isCollaborativeMode ? 'collaborative' : 'ai');
            const modeName = isSingleAgentRoam
                ? ((window.__lang || 'zh') === 'en' ? 'Single Agent Roam' : '单 Agent 游走')
                : (isCollaborativeMode ? __t('collab_mode_tag') : __t('auto_mode_tag'));
            const filename = this.currentFile?.filename || '';
            
            if (filename) {
                const histResp = await fetch(`/api/list-analysis-history?filename=${encodeURIComponent(filename)}&mode=${mode}`);
                const histData = await histResp.json();
                if (stale()) return;
                
                if (histData.success && histData.results && histData.results.length > 1) {
                    console.log(`📦 发现 ${histData.results.length} 条历史记录，弹出选择弹窗`);
                    await this._loadWithHistorySelection({ file_id: fileId, filename });
                    return;
                }
            }
            
            const response = await fetch(`/api/check_analysis_cache/${fileId}?mode=${mode}`);
            const result = await response.json();
            if (stale()) return;
            
            if (result.success && result.has_cache) {
                console.log(`📦 Found ${modeName} cached results:`, result);
                
                const isEn = (window.__lang || 'zh') === 'en';
                let cacheInfo = isEn 
                    ? `Found ${modeName} analysis results`
                    : `发现${modeName}的分析结果`;
                if (result.cached_at) {
                    cacheInfo += isEn 
                        ? `\nCached at: ${new Date(result.cached_at).toLocaleString()}`
                        : `\n缓存时间：${new Date(result.cached_at).toLocaleString()}`;
                }
                if (result.is_legacy) {
                    cacheInfo += isEn
                        ? `\n(Legacy format, will be auto-converted)`
                        : `\n(旧版本格式，将自动转换)`;
                }
                
                const loadCache = await this._showCacheConfirmDialog(cacheInfo);
                
                if (loadCache) {
                    await this.loadAnalysisState(fileId);
                }
            } else {
                console.log(`📭 没有找到${modeName}的缓存`);
            }
        } catch (error) {
            console.error('检查缓存失败:', error);
        }
    }
    
    /**
     * 通用自定义确认弹窗（替代原生 confirm，按钮文字跟随语言设置）
     */
    _showSimpleConfirmDialog(message) {
        return new Promise((resolve) => {
            const isEn = (window.__lang || 'zh') === 'en';
            const overlay = document.createElement('div');
            overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.45);z-index:99999;display:flex;align-items:center;justify-content:center;';
            const box = document.createElement('div');
            box.style.cssText = 'background:#fff;border-radius:12px;padding:28px 32px 20px;max-width:420px;width:90%;box-shadow:0 8px 40px rgba(0,0,0,.25);font-family:system-ui,sans-serif;';
            const msg = document.createElement('div');
            msg.style.cssText = 'font-size:14px;color:#333;line-height:1.6;margin-bottom:20px;';
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

    /**
     * 自定义缓存确认弹窗（替代原生 confirm，按钮文字跟随语言设置）
     */
    _showCacheConfirmDialog(cacheInfo) {
        return new Promise((resolve) => {
            const isEn = (window.__lang || 'zh') === 'en';
            const overlay = document.createElement('div');
            overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.45);z-index:99999;display:flex;align-items:center;justify-content:center;';
            const box = document.createElement('div');
            box.style.cssText = 'background:#fff;border-radius:12px;padding:28px 32px 20px;max-width:420px;width:90%;box-shadow:0 8px 40px rgba(0,0,0,.25);font-family:system-ui,sans-serif;';
            const msg = document.createElement('div');
            msg.style.cssText = 'white-space:pre-wrap;font-size:14px;color:#333;line-height:1.6;margin-bottom:20px;';
            msg.textContent = `${cacheInfo}\n\n${__t('cache_confirm')}`;
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

    /**
     * 三选一弹窗：选择历史分析结果加载方式
     * @param {Array} cachedFiles - [{file_id, filename, has_cache, cached_at}]
     * @returns {Promise<{action: 'load_selected'|'rerun_all'|'load_json', selectedIds: string[]}>}
     */
    _showAnalysisLoadOptionsDialog(cachedFiles) {
        return new Promise((resolve) => {
            const isEn = (window.__lang || 'zh') === 'en';
            const cachedItems = cachedFiles.filter(f => f.has_cache);
            const cachedCount = cachedItems.length;

            const overlay = document.createElement('div');
            overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.45);z-index:99999;display:flex;align-items:center;justify-content:center;';

            const box = document.createElement('div');
            box.style.cssText = 'background:#fff;border-radius:14px;padding:28px 32px 24px;max-width:520px;width:92%;box-shadow:0 8px 40px rgba(0,0,0,.25);font-family:system-ui,sans-serif;';

            const title = document.createElement('div');
            title.style.cssText = 'font-size:16px;font-weight:600;color:#1f2937;margin-bottom:16px;';
            title.textContent = __t('detected_history_results').replace('{0}', cachedCount);

            // Option A: load selected
            const optABtn = document.createElement('button');
            optABtn.style.cssText = 'display:flex;align-items:center;gap:8px;width:100%;padding:12px 16px;border:1px solid #e5e7eb;border-radius:10px;background:#fff;color:#374151;cursor:pointer;font-size:14px;text-align:left;margin-bottom:8px;transition:all .15s;';
            optABtn.innerHTML = `<i class="fas fa-list-check" style="color:#2563eb;width:20px;text-align:center;"></i> ${__t('option_load_selected')}`;
            optABtn.addEventListener('mouseenter', () => { optABtn.style.borderColor = '#2563eb'; optABtn.style.background = '#eff6ff'; });
            optABtn.addEventListener('mouseleave', () => { optABtn.style.borderColor = '#e5e7eb'; optABtn.style.background = '#fff'; });

            // Slide checklist (hidden initially)
            const listContainer = document.createElement('div');
            listContainer.style.cssText = 'display:none;margin-bottom:12px;border:1px solid #e5e7eb;border-radius:10px;padding:12px;max-height:240px;overflow-y:auto;';

            const selectAllRow = document.createElement('div');
            selectAllRow.style.cssText = 'display:flex;gap:12px;margin-bottom:8px;';
            const selAllBtn = document.createElement('a');
            selAllBtn.href = '#';
            selAllBtn.textContent = __t('select_all');
            selAllBtn.style.cssText = 'font-size:12px;color:#2563eb;text-decoration:none;';
            const deselAllBtn = document.createElement('a');
            deselAllBtn.href = '#';
            deselAllBtn.textContent = __t('deselect_all');
            deselAllBtn.style.cssText = 'font-size:12px;color:#6b7280;text-decoration:none;';
            selectAllRow.append(selAllBtn, deselAllBtn);
            listContainer.appendChild(selectAllRow);

            const checkboxes = [];
            cachedItems.forEach(item => {
                const row = document.createElement('label');
                row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:6px 4px;cursor:pointer;font-size:13px;color:#374151;border-radius:6px;';
                row.addEventListener('mouseenter', () => { row.style.background = '#f9fafb'; });
                row.addEventListener('mouseleave', () => { row.style.background = 'transparent'; });
                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.checked = true;
                cb.value = item.file_id;
                cb.style.cssText = 'accent-color:#2563eb;';
                checkboxes.push(cb);
                const timeStr = item.cached_at ? new Date(item.cached_at).toLocaleString() : '';
                const nameSpan = document.createElement('span');
                nameSpan.style.cssText = 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
                nameSpan.textContent = item.filename || item.file_id;
                const timeSpan = document.createElement('span');
                timeSpan.style.cssText = 'font-size:11px;color:#9ca3af;white-space:nowrap;';
                timeSpan.textContent = timeStr;
                row.append(cb, nameSpan, timeSpan);
                listContainer.appendChild(row);
            });

            selAllBtn.addEventListener('click', (e) => { e.preventDefault(); checkboxes.forEach(c => c.checked = true); });
            deselAllBtn.addEventListener('click', (e) => { e.preventDefault(); checkboxes.forEach(c => c.checked = false); });

            const confirmLoadBtn = document.createElement('button');
            confirmLoadBtn.style.cssText = 'display:none;width:100%;padding:10px;border:none;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer;font-size:14px;margin-bottom:12px;';
            confirmLoadBtn.textContent = __t('confirm_load');

            optABtn.addEventListener('click', () => {
                // Patient Level 模式：直接使用按运行分组的历史弹窗
                if (this.patientLevelUploadResults?.length > 0) {
                    overlay.remove();
                    resolve({ action: 'load_patient_history' });
                    return;
                }
                const showing = listContainer.style.display !== 'none';
                listContainer.style.display = showing ? 'none' : 'block';
                confirmLoadBtn.style.display = showing ? 'none' : 'block';
            });

            confirmLoadBtn.addEventListener('click', () => {
                const selected = checkboxes.filter(c => c.checked).map(c => c.value);
                overlay.remove();
                resolve({ action: 'load_selected', selectedIds: selected });
            });

            // Option B: rerun all
            const optBBtn = document.createElement('button');
            optBBtn.style.cssText = 'display:flex;align-items:center;gap:8px;width:100%;padding:12px 16px;border:1px solid #e5e7eb;border-radius:10px;background:#fff;color:#374151;cursor:pointer;font-size:14px;text-align:left;margin-bottom:8px;transition:all .15s;';
            optBBtn.innerHTML = `<i class="fas fa-redo" style="color:#f59e0b;width:20px;text-align:center;"></i> ${__t('option_rerun_all')}`;
            optBBtn.addEventListener('mouseenter', () => { optBBtn.style.borderColor = '#f59e0b'; optBBtn.style.background = '#fffbeb'; });
            optBBtn.addEventListener('mouseleave', () => { optBBtn.style.borderColor = '#e5e7eb'; optBBtn.style.background = '#fff'; });
            optBBtn.addEventListener('click', () => {
                overlay.remove();
                resolve({ action: 'rerun_all', selectedIds: [] });
            });

            // Option C: load from JSON
            const optCBtn = document.createElement('button');
            optCBtn.style.cssText = 'display:flex;align-items:center;gap:8px;width:100%;padding:12px 16px;border:1px solid #e5e7eb;border-radius:10px;background:#fff;color:#374151;cursor:pointer;font-size:14px;text-align:left;transition:all .15s;';
            optCBtn.innerHTML = `<i class="fas fa-file-import" style="color:#10b981;width:20px;text-align:center;"></i> ${__t('option_load_json')}`;
            optCBtn.addEventListener('mouseenter', () => { optCBtn.style.borderColor = '#10b981'; optCBtn.style.background = '#ecfdf5'; });
            optCBtn.addEventListener('mouseleave', () => { optCBtn.style.borderColor = '#e5e7eb'; optCBtn.style.background = '#fff'; });
            optCBtn.addEventListener('click', () => {
                overlay.remove();
                resolve({ action: 'load_json', selectedIds: [] });
            });

            box.append(title, optABtn, listContainer, confirmLoadBtn, optBBtn, optCBtn);
            overlay.appendChild(box);
            document.body.appendChild(overlay);
            overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); resolve(null); } });
        });
    }

    /**
     * 从本地 JSON 文件加载分析结果
     */
    async loadFromLocalJsonFile() {
        return new Promise((resolve) => {
            const input = document.createElement('input');
            input.type = 'file';
            input.accept = '.json';
            input.style.display = 'none';
            document.body.appendChild(input);

            input.addEventListener('change', async (e) => {
                const file = e.target.files[0];
                input.remove();
                if (!file) { resolve(false); return; }

                try {
                    this.showLoading(__t('processing'));
                    const text = await file.text();
                    const data = JSON.parse(text);

                    if (data && data.schema === 'pathcopilot_patient_workspace_v1') {
                        if (data.folder_name && !this.patientLevelFolderName) {
                            this.patientLevelFolderName = data.folder_name;
                        }
                        if (data.patient_id && !this.currentPatientId) {
                            this.currentPatientId = data.patient_id;
                        }
                    }

                    // Patient Level JSON: array of states, or { slides: [...] }.
                    const rawSlides = Array.isArray(data)
                        ? data
                        : Array.isArray(data?.slides) ? data.slides : null;
                    const states = rawSlides
                        ? rawSlides.map((item) => {
                            if (!item || typeof item !== 'object') return item;
                            if (item.file_id && item.data && typeof item.data === 'object') {
                                return {
                                    file_id: item.file_id,
                                    filename: item.filename || item.data.filename || '',
                                    ...item.data,
                                };
                            }
                            return item;
                        })
                        : (data?.file_id ? [data] : null);

                    if (!states || states.length === 0) {
                        this.showNotification('error', __t('invalid_json_format'));
                        resolve(false);
                        return;
                    }

                    const isPatientWorkspace = Boolean(
                        data?.schema === 'pathcopilot_patient_workspace_v1'
                        || data?.kind === 'patient_level'
                        || (states.length > 1 && this.patientLevelUploadResults?.length > 1)
                    );
                    if (isPatientWorkspace) {
                        const restored = await this.applyPatientWorkspaceDocument(
                            data?.schema || data?.kind
                                ? data
                                : {
                                    schema: 'pathcopilot_patient_workspace_v1',
                                    kind: 'patient_level',
                                    slides: states,
                                    chatHistory: data?.chatHistory || [],
                                    leftPanelHtml: data?.leftPanelHtml || '',
                                    analysis_strategy: data?.analysis_strategy || data?.mode || '',
                                    mode: data?.mode || '',
                                    patient_id: data?.patient_id || '',
                                    folder_name: data?.folder_name || '',
                                }
                        );
                        if (!restored) {
                            resolve(false);
                            return;
                        }
                    } else if (states.length === 1 && !this.patientLevelUploadResults?.length) {
                        const state = states[0];
                        const fileId = state.file_id || this.currentFile?.file_id;
                        const mode = state.mode || 'ai';
                        const isSingleAgentRoam = (state.analysisStrategy || '') === 'single_agent_roam'
                            || mode === 'single_agent_roam';
                        // ⭐ 与服务器缓存加载路径保持一致：还原 output_dir / 策略 / 模式。
                        // 少了 output_dir，路径树接口会退化为 outputs 全目录扫描——
                        // 又慢（一直「处理中」）又可能拿到别的 run 的树。
                        if (window.pathChatAgent) {
                            window.pathChatAgent.currentOutputDirName = String(state.output_dir || '');
                            window.pathChatAgent.activeAnalysisStrategy = isSingleAgentRoam
                                ? 'single_agent_roam'
                                : 'agent_swarm';
                            window.pathChatAgent.isCollaborativeMode = mode === 'collaborative';
                            window.pathChatAgent.currentMode = mode === 'collaborative' ? 'collaborative' : 'auto';
                        }
                        this._treeLoadedForId = null;
                        this.analysisTreeData = null;
                        await this.applyAnalysisState(
                            state,
                            fileId,
                            mode === 'collaborative',
                            isSingleAgentRoam ? 'single_agent_roam' : mode
                        );
                    } else {
                        // Patient Level: upload each state to server cache, then load
                        for (const state of states) {
                            if (!state.file_id) continue;
                            try {
                                await fetch(`/api/save_analysis_state/${state.file_id}`, {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify(state)
                                });
                            } catch (err) {
                                console.warn(`Failed to import state for ${state.file_id}:`, err);
                            }
                        }
                        await this.loadPatientLevelAnalysisState();
                    }

                    this.showNotification('success', __t('json_load_success'));
                    resolve(true);
                } catch (err) {
                    console.error('JSON file load failed:', err);
                    this.showNotification('error', __t('invalid_json_format'));
                    resolve(false);
                } finally {
                    this.hideLoading();
                }
            });

            input.click();
        });
    }

    _matchPatientWorkspaceSlide(slide, uploadResults = []) {
        const results = Array.isArray(uploadResults) ? uploadResults : [];
        const fileId = String(slide?.file_id || '').trim();
        if (fileId) {
            const byId = results.find((item) => String(item.file_id) === fileId);
            if (byId) return byId;
        }
        const filename = String(slide?.filename || '').trim();
        if (filename) {
            const byName = results.find((item) => String(item.filename || '') === filename);
            if (byName) return byName;
        }
        if (Number.isInteger(slide?.index)) {
            const byIndex = results.find((item) => Number(item.index) === Number(slide.index));
            if (byIndex) return byIndex;
        }
        return null;
    }

    _rewritePatientWorkspaceHtml(html, idMap) {
        let out = String(html || '');
        (idMap || []).forEach(([oldId, newId]) => {
            if (!oldId || !newId || oldId === newId) return;
            out = out.split(String(oldId)).join(String(newId));
        });
        return out;
    }

    _patientWorkspaceMapHtml(doc, slides = []) {
        const candidates = [doc?.leftPanelHtml, ...slides.map((item) => item?.leftPanelHtml)]
            .map((html) => String(html || ''))
            .filter(Boolean);
        return candidates.find((html) => html.includes('v2-agent-activity'))
            || candidates.find((html) => html.includes('v2-subagent-stream'))
            || candidates.find((html) => html.includes('patient-slide-map'))
            || candidates.find((html) => html.includes('route-tree'))
            || candidates.find((html) => html.includes('analysis-tree') && html.length > 80)
            || '';
    }

    _compactAnalysisText(text, maxChars) {
        const compact = String(text || '')
            .replace(/[ \t]+\n/g, '\n')
            .replace(/\n{3,}/g, '\n\n')
            .trim();
        if (!maxChars || compact.length <= maxChars) return compact;
        return `${compact.slice(0, maxChars)}\n…`;
    }

    _slideLabelForIndex(index) {
        const entry = (this.patientLevelUploadResults || []).find((item) => Number(item.index) === Number(index));
        const raw = entry?.part || entry?.filename || '';
        const cleaned = String(raw).replace(/^Slide\s+\d+:\s*/i, '').trim();
        if (cleaned) return cleaned;
        const isEn = (window.__lang || 'zh') === 'en';
        return isEn ? `Slide ${Number(index) + 1}` : `切片 ${Number(index) + 1}`;
    }

    _formatRoiObservation(box, magnification) {
        const text = String(box?.observation || box?.analysis || '').trim();
        if (!text) return '';
        const id = String(box?.id || '').trim() || 'ROI';
        const mag = magnification ? ` ${magnification}` : '';
        return `【${id}${mag}】\n${text}`;
    }

    _formatWsiAnalysisText(wsiData = {}, { maxRoiChars = 1800, maxTotal = 12000 } = {}) {
        const parts = [];
        const overview = String(wsiData.globalReasoning || '').trim();
        const report = String(wsiData.report || '').trim();
        if (overview) parts.push(`【总览】\n${overview}`);
        if (report && report !== overview) parts.push(`【报告】\n${report}`);
        (wsiData.redBoxes || []).forEach((redBox) => {
            const redText = this._formatRoiObservation(redBox, '10×');
            if (redText) parts.push(this._compactAnalysisText(redText, maxRoiChars));
            (redBox.blueBoxes || []).forEach((blueBox) => {
                const blueText = this._formatRoiObservation(blueBox, '40×');
                if (blueText) parts.push(this._compactAnalysisText(blueText, maxRoiChars));
                (blueBox.greenBoxes || []).forEach((greenBox) => {
                    const greenText = this._formatRoiObservation(greenBox, '160×');
                    if (greenText) parts.push(this._compactAnalysisText(greenText, maxRoiChars));
                });
            });
        });
        (wsiData.trajectory || []).forEach((step) => {
            const note = String(
                step?.observation || step?.reasoning || step?.analysis || step?.caption || step?.content || ''
            ).trim();
            if (!note) return;
            const zoom = step?.zoom || step?.magnification || '';
            const label = step?.step || step?.id || '';
            parts.push(this._compactAnalysisText(
                `【游走 ${label}${zoom ? ` ${zoom}×` : ''}】\n${note}`,
                maxRoiChars
            ));
        });
        const roiMap = wsiData.roiAnalysisData || {};
        Object.entries(roiMap).forEach(([id, value]) => {
            const text = String(value?.analysis || value?.observation || '').trim();
            if (!text) return;
            const already = parts.some((part) => part.includes(`【${id}`) && part.includes(text.slice(0, 40)));
            if (!already) parts.push(this._compactAnalysisText(`【${id}】\n${text}`, maxRoiChars));
        });
        // Collaborative serial writes the real narrative into the middle-pane
        // HTML, not redBox.observation. Keep that text so a cache reload can
        // rebuild chat context even when ROI observations are empty.
        if (this._compactAnalysisText(parts.join('\n\n')).length < 80) {
            const htmlText = this._htmlToAnalysisText(wsiData.leftPanelHtml || '', maxTotal);
            if (htmlText) parts.push(htmlText);
        }
        return this._compactAnalysisText(parts.join('\n\n'), maxTotal);
    }

    _formatRoiAnalysisMap(roiAnalysisData = {}, maxRoiChars = 1800) {
        return Object.entries(roiAnalysisData || {})
            .map(([id, value]) => {
                const text = String(value?.analysis || value?.observation || '').trim();
                return text ? this._compactAnalysisText(`【${id}】\n${text}`, maxRoiChars) : '';
            })
            .filter(Boolean)
            .join('\n\n');
    }

    _htmlToAnalysisText(html, maxChars = 20000) {
        const holder = document.createElement('div');
        holder.innerHTML = String(html || '');
        holder.querySelectorAll('script, style').forEach((el) => el.remove());
        return this._compactAnalysisText(holder.textContent || '', maxChars);
    }

    _extractSlideAnalysisFromWorkspace(slide = {}, { maxRoiChars = 1600, maxTotal = 12000 } = {}) {
        const parts = [];
        const reasoning = String(slide.globalReasoning || slide.report || '').trim();
        if (reasoning) parts.push(reasoning);
        const roi = this._formatRoiAnalysisMap(slide.roiAnalysisData || {}, maxRoiChars);
        if (roi) parts.push(roi);
        const htmlText = this._htmlToAnalysisText(slide.leftPanelHtml || '', maxTotal);
        if (htmlText) parts.push(htmlText);
        return this._compactAnalysisText(parts.join('\n\n'), maxTotal);
    }

    _extractWorkspaceDocumentContext(doc) {
        const slides = Array.isArray(doc?.slides) ? doc.slides : (Array.isArray(doc) ? doc : []);
        const isEn = (window.__lang || 'zh') === 'en';
        const parts = [];
        const prior = (Array.isArray(doc?.chatHistory) ? doc.chatHistory : [])
            .filter((msg) => msg && (msg.role === 'user' || msg.role === 'assistant'))
            .map((msg) => {
                const role = msg.role === 'user' ? (isEn ? 'User' : '用户') : (isEn ? 'Assistant' : '助手');
                return `${role}: ${this._compactAnalysisText(msg.content || '', 4000)}`;
            })
            .filter((line) => line.length > 8);
        if (prior.length) {
            parts.push(
                (isEn ? '=== Saved conversation and reports ===\n' : '=== 已保存的对话与报告 ===\n')
                + prior.join('\n\n')
            );
        }
        slides.forEach((slide, index) => {
            const inner = slide?.data && typeof slide.data === 'object' ? { ...slide, ...slide.data } : slide;
            const name = inner?.part || inner?.filename || (isEn ? `Slide ${index + 1}` : `切片 ${index + 1}`);
            const body = this._extractSlideAnalysisFromWorkspace(inner || {}, {
                maxRoiChars: 1600,
                maxTotal: 10000,
            });
            if (body) parts.push(`===== ${name} =====\n${body}`);
        });
        if (parts.length < 2) {
            const htmlText = this._htmlToAnalysisText(doc?.leftPanelHtml || '', 20000);
            if (htmlText) parts.push(htmlText);
        }
        return this._compactAnalysisText(parts.join('\n\n'), 60000);
    }

    _workspaceJsonWithoutUiHtml(doc) {
        if (!doc || typeof doc !== 'object') return {};
        const omitHtml = (item) => {
            if (!item || typeof item !== 'object') return item;
            const { leftPanelHtml, ...rest } = item;
            if (rest.data && typeof rest.data === 'object') {
                const { leftPanelHtml: _ignored, ...dataRest } = rest.data;
                rest.data = dataRest;
            }
            return rest;
        };
        const out = omitHtml(doc);
        if (Array.isArray(out.slides)) out.slides = out.slides.map(omitHtml);
        return out;
    }

    _workspaceJsonAsContext(doc) {
        if (!doc || typeof doc !== 'object') return '';
        try {
            return JSON.stringify(this._workspaceJsonWithoutUiHtml(doc));
        } catch (error) {
            console.warn('工作区 JSON 序列化失败:', error);
            return '';
        }
    }

    _rememberWorkspaceJson(doc) {
        this._restoredWorkspaceJson = doc || null;
        this._restoredWorkspaceContext = '';
        this._analysisRestoredFromCache = Boolean(doc);
        if (doc) this.patientAnalysisAwaitingQuestion = null;
        return doc;
    }

    _rememberWorkspaceContext(text) {
        return this._rememberWorkspaceJson(
            text && typeof text === 'object' ? text : { note: String(text || '') }
        );
    }

    _collectStructuredPatientAnalysis({ maxSlideChars = 8000, maxRoiChars = 1600 } = {}) {
        const data = window.pathChatAgent?.patientLevelAnalysisData || {};
        const keys = Object.keys(data)
            .map((key) => Number(key))
            .filter((idx) => Number.isInteger(idx))
            .sort((a, b) => a - b);
        const parts = [];
        keys.forEach((idx) => {
            const body = this._formatWsiAnalysisText(data[idx] || {}, {
                maxRoiChars,
                maxTotal: maxSlideChars,
            });
            if (body) parts.push(`===== ${this._slideLabelForIndex(idx)} =====\n${body}`);
        });
        return parts;
    }

    _scrapeRestoredAnalysisDom({ maxChars = 8000 } = {}) {
        const parts = [];
        const maps = document.querySelectorAll('#routeStepsList > .patient-slide-map');
        if (maps.length) {
            maps.forEach((map) => {
                const label = map.querySelector(':scope > .patient-slide-map-label')?.textContent?.trim() || '';
                const stream = map.querySelector('.v2-subagent-stream');
                const text = this._compactAnalysisText((stream || map).textContent || '', maxChars);
                if (text.length > 40) parts.push(label ? `【${label}】\n${text}` : text);
            });
            return parts;
        }
        const stream = document.querySelector('#routeStepsList .v2-subagent-stream');
        const streamText = this._compactAnalysisText(stream?.textContent || '', maxChars);
        if (streamText.length > 40) parts.push(streamText);
        return parts;
    }

    _formatCurrentSlideAnalysisText({ maxChars = 8000 } = {}) {
        const parts = [];
        const leftover = String(this._lastRestoredGlobalReasoning || '').trim();
        if (leftover) parts.push(`【总览】\n${leftover}`);
        const mapped = this._formatRoiAnalysisMap(this.roiAnalysisData || {});
        if (mapped) parts.push(mapped);
        const agent = window.pathChatAgent;
        const viewIndex = Number.isInteger(agent?.currentViewingWsiIndex)
            ? agent.currentViewingWsiIndex
            : this.patientLevelUploadResults?.find((item) => String(item.file_id) === String(this.currentFile?.file_id))?.index;
        if (Number.isInteger(viewIndex) && agent?.patientLevelAnalysisData?.[viewIndex]) {
            const body = this._formatWsiAnalysisText(agent.patientLevelAnalysisData[viewIndex], {
                maxTotal: maxChars,
            });
            if (body) parts.push(body);
        }
        return this._compactAnalysisText(parts.join('\n\n'), maxChars);
    }

    _chatHistoryHasAnalysis(messages) {
        const items = Array.isArray(messages) ? messages : [];
        if (items.some((msg) => msg?.restoredAnalysis)) return true;
        const joined = items
            .filter((msg) => msg && msg.role === 'assistant')
            .map((msg) => String(msg.content || '').trim())
            .filter(Boolean)
            .join('\n');
        if (joined.length < 180) return false;
        return /【总览】|【Reason】|【Plan】|【Evidence】|已加载的切片分析|Restored slide analysis|【红框|10×|40×|160×|游走视野|Subagent/.test(joined);
    }

    _patientAnalysisLooksComplete() {
        const data = window.pathChatAgent?.patientLevelAnalysisData || {};
        return Object.values(data).some((wsi) => {
            if (String(wsi?.globalReasoning || wsi?.report || '').trim().length >= 40) return true;
            if ((wsi?.redBoxes || []).some((box) => String(box?.observation || '').trim().length >= 20)) return true;
            if ((wsi?.trajectory || []).length >= 1) return true;
            if (String(wsi?.leftPanelHtml || '').trim().length >= 80) return true;
            if (Object.values(wsi?.roiAnalysisData || {}).some((item) => (
                String(item?.analysis || item?.observation || '').trim().length >= 20
            ))) return true;
            return false;
        }) || Object.values(this.roiAnalysisData || {}).some((item) => (
            String(item?.analysis || item?.observation || '').trim().length >= 20
        ));
    }

    _buildAnalysisTranscriptMessagesForSlide(index) {
        const body = this._formatWsiAnalysisText(
            window.pathChatAgent?.patientLevelAnalysisData?.[index] || {},
            { maxTotal: 8000, maxRoiChars: 1600 }
        );
        if (!body) return [];
        return [{
            role: 'assistant',
            content: `===== ${this._slideLabelForIndex(index)} =====\n${body}`,
            restoredAnalysis: true,
            timestamp: new Date(),
        }];
    }

    _buildAnalysisTranscriptMessages() {
        const isEn = (window.__lang || 'zh') === 'en';
        const parts = [];
        if (this._restoredWorkspaceJson) {
            const extracted = this._extractWorkspaceDocumentContext(this._restoredWorkspaceJson);
            if (extracted) parts.push(extracted);
        }
        if (this._compactAnalysisText(parts.join('\n\n')).length < 80) {
            this._collectStructuredPatientAnalysis({
                maxSlideChars: 8000,
                maxRoiChars: 1600,
            }).forEach((part) => parts.push(part));
        }
        if (this._compactAnalysisText(parts.join('\n\n')).length < 80) {
            this._scrapeRestoredAnalysisDom({ maxChars: 8000 }).forEach((part) => parts.push(part));
        }
        const current = this._formatCurrentSlideAnalysisText({ maxChars: 8000 });
        if (current && !parts.some((part) => part.includes(current.slice(0, 80)))) {
            parts.push(current);
        }
        const body = this._compactAnalysisText(parts.join('\n\n'), 24000);
        if (!body || body.length < 40) return [];
        const header = isEn
            ? 'The following is the restored slide analysis. Follow-up questions will use this as context; you do not need to re-analyze.'
            : '以下为已加载的切片分析过程。后续提问会把这些内容作为上下文，无需重新分析。';
        return [{
            role: 'assistant',
            content: `${header}\n\n${body}`,
            restoredAnalysis: true,
            timestamp: new Date(),
        }];
    }

    _rememberLoadedPatientCacheAsWorkspace() {
        const agent = window.pathChatAgent;
        const data = agent?.patientLevelAnalysisData || {};
        const uploads = this.patientLevelUploadResults || [];
        const slides = uploads.map((entry) => {
            const wsi = data[entry.index] || {};
            const hist = (agent?._collectedChatHistories || []).find(
                (item) => Number(item.slideIndex) === Number(entry.index)
            );
            return {
                file_id: entry.file_id,
                filename: entry.filename || '',
                part: entry.part || '',
                index: entry.index,
                globalReasoning: wsi.globalReasoning || '',
                report: wsi.report || '',
                leftPanelHtml: wsi.leftPanelHtml || '',
                roiAnalysisData: wsi.roiAnalysisData || {},
                roiOverlays: wsi.roiOverlays || [],
                redBoxes: wsi.redBoxes || [],
                trajectory: wsi.trajectory || [],
                chatHistory: hist?.messages || [],
            };
        }).filter((slide) => (
            String(slide.globalReasoning || '').trim()
            || String(slide.leftPanelHtml || '').trim()
            || (slide.redBoxes || []).length
            || (slide.roiOverlays || []).length
            || (slide.trajectory || []).length
            || Object.keys(slide.roiAnalysisData || {}).length
        ));
        if (!slides.length) {
            this._analysisRestoredFromCache = true;
            return null;
        }
        return this._rememberWorkspaceJson({
            schema: 'pathcopilot_patient_workspace_v1',
            kind: 'patient_level',
            patient_id: this.currentPatientId || '',
            folder_name: this.patientLevelFolderName || '',
            chatHistory: this.chatHistory || [],
            leftPanelHtml: document.getElementById('routeStepsList')?.innerHTML || '',
            slides,
        });
    }

    _chatHistoryForSlideCache(entry, { isCurrent = false, currentSnapshot = null, histEntry = null } = {}) {
        const live = isCurrent
            ? (currentSnapshot?.chatHistory || this.chatHistory || [])
            : (histEntry?.messages || []);
        if (this._chatHistoryHasAnalysis(live)) return live;
        const synthesized = Number.isInteger(entry?.index)
            ? this._buildAnalysisTranscriptMessagesForSlide(entry.index)
            : [];
        return synthesized.length ? synthesized.concat(live) : live;
    }

    _workspaceChatHistoryForExport() {
        if (this._chatHistoryHasAnalysis(this.chatHistory)) return this.chatHistory || [];
        const synthesized = this._buildAnalysisTranscriptMessages();
        return synthesized.length ? synthesized.concat(this.chatHistory || []) : (this.chatHistory || []);
    }

    _createRestoredChatMessageElement(msg) {
        if (!msg || msg.role === 'system-divider') return null;
        const messageElement = document.createElement('div');
        messageElement.className = `message ${msg.role}-message`;
        if (msg.restoredAnalysis) messageElement.setAttribute('data-restored-analysis', '1');
        if (msg.roiId) messageElement.setAttribute('data-roi-id', msg.roiId);
        const avatar = msg.role === 'assistant'
            ? '<i class="fas fa-robot"></i>'
            : '<i class="fas fa-user"></i>';
        messageElement.innerHTML = `
            <div class="message-avatar">${avatar}</div>
            <div class="message-content">
                <div class="message-text">${this.formatMessageContent(msg.content || '')}</div>
                <div class="message-time">${msg.timestamp ? new Date(msg.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : ''}</div>
            </div>
        `;
        return messageElement;
    }

    _ensureRestoredAnalysisInChat() {
        this._analysisRestoredFromCache = true;
        this.patientAnalysisAwaitingQuestion = null;
        if (this._chatHistoryHasAnalysis(this.chatHistory)) return;
        const transcript = this._buildAnalysisTranscriptMessages();
        if (!transcript.length) return;
        const existing = Array.isArray(this.chatHistory) ? this.chatHistory.slice() : [];
        this.chatHistory = transcript.concat(existing);
        const container = this.elements?.chatMessages;
        if (!container) return;
        const frag = document.createDocumentFragment();
        transcript.forEach((msg) => {
            const el = this._createRestoredChatMessageElement(msg);
            if (el) frag.appendChild(el);
        });
        if (container.firstChild) container.insertBefore(frag, container.firstChild);
        else container.appendChild(frag);
        this.scrollToBottom?.();
    }

    _renderRestoredChatHistory(messages = []) {
        const chatContainer = this.elements?.chatMessages;
        if (!chatContainer) return;
        chatContainer.innerHTML = '';
        this.chatHistory = Array.isArray(messages) ? messages.slice() : [];
        this.chatHistory.forEach((msg) => {
            const messageElement = this._createRestoredChatMessageElement(msg);
            if (messageElement) chatContainer.appendChild(messageElement);
        });
        this.scrollToBottom?.();
    }

    async applyPatientWorkspaceDocument(doc) {
        const slides = Array.isArray(doc?.slides) ? doc.slides : (Array.isArray(doc) ? doc : []);
        const uploadResults = this.patientLevelUploadResults || [];
        if (!slides.length) {
            this.showNotification('error', __t('invalid_json_format'));
            return false;
        }
        if (!uploadResults.length) {
            this.showNotification(
                'warning',
                (window.__lang || 'zh') === 'en'
                    ? 'Open this patient case first, then load the JSON.'
                    : '请先打开该患者的全部切片，再加载这份 JSON。'
            );
            return false;
        }

        const strategy = String(
            doc.analysis_strategy || doc.mode || slides[0]?.analysisStrategy || slides[0]?.mode || ''
        );
        const isRoam = strategy === 'single_agent_roam';
        const isCollaborative = String(doc.mode || '') === 'collaborative';
        const agent = window.pathChatAgent;
        if (!agent) {
            this.showNotification('error', (window.__lang || 'zh') === 'en' ? 'Agent not initialized' : 'Agent 尚未初始化');
            return false;
        }

        this.switchToPatientLevelPanel();
        if (doc.folder_name) this.patientLevelFolderName = doc.folder_name;
        if (doc.patient_id) this.currentPatientId = doc.patient_id;

        agent.isPatientLevelMode = true;
        agent.activeAnalysisStrategy = isRoam ? 'single_agent_roam' : 'agent_swarm';
        agent.isCollaborativeMode = isCollaborative;
        agent.currentMode = isCollaborative ? 'collaborative' : 'ai';
        agent.patientLevelAnalysisData = {};
        agent._collectedChatHistories = [];
        this._rememberWorkspaceJson(doc);
        agent.isRunning = false;
        agent.isStarting = false;
        agent._patientWorkspaceHtmlRestored = true;
        agent.subagentUiActivated = true;
        if (typeof agent._patientRoamByFile?.clear === 'function') {
            agent._patientRoamByFile.clear();
        }
        if (typeof agent._patientActivitySyncedFiles?.clear === 'function') {
            agent._patientActivitySyncedFiles.clear();
        }

        const idMap = [];
        let matchedCount = 0;
        for (const slide of slides) {
            const entry = this._matchPatientWorkspaceSlide(slide, uploadResults);
            if (!entry?.file_id) {
                console.warn('[Patient Level] 无法匹配切片', slide?.filename || slide?.file_id);
                continue;
            }
            matchedCount += 1;
            if (slide.file_id) idMap.push([String(slide.file_id), String(entry.file_id)]);
            const overlays = Array.isArray(slide.roiOverlays) && slide.roiOverlays.length
                ? slide.roiOverlays
                : this._roamOverlaysFromTrajectory(slide.trajectory || [], entry.index);
            const redBoxes = isRoam
                ? []
                : this._redBoxesFromCachedOverlays({
                    roiOverlays: overlays,
                    roiAnalysisData: slide.roiAnalysisData || {},
                });
            agent.patientLevelAnalysisData[entry.index] = {
                globalReasoning: slide.globalReasoning || slide.report || '',
                report: slide.report || '',
                trajectory: Array.isArray(slide.trajectory) ? slide.trajectory : [],
                roiOverlays: overlays,
                roiAnalysisData: slide.roiAnalysisData || {},
                redBoxes,
            };
            if (isRoam) {
                agent._ensurePatientRoamStoreForFile?.(entry.file_id, entry.index);
            }
            if (slide.chatHistory?.length) {
                agent._collectedChatHistories.push({
                    slideIndex: entry.index,
                    slideName: entry.part || entry.filename || slide.filename,
                    messages: slide.chatHistory,
                });
            }
            const cachePayload = {
                ...slide,
                file_id: entry.file_id,
                filename: entry.filename || slide.filename || '',
                index: entry.index,
                patient_id: this.currentPatientId || '',
                roiOverlays: overlays,
            };
            fetch(`/api/save_analysis_state/${entry.file_id}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(cachePayload),
            }).then(async (resp) => {
                if (resp.ok) return;
                const retry = { ...cachePayload, patient_id: '' };
                await fetch(`/api/save_analysis_state/${entry.file_id}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(retry),
                });
            }).catch((err) => console.warn('[Patient Level] 导入缓存失败', entry.filename, err));
        }

        if (!matchedCount) {
            this.showNotification(
                'warning',
                (window.__lang || 'zh') === 'en'
                    ? 'None of the saved slides match the currently open case.'
                    : '这份 JSON 里的切片与当前打开的患者病例对不上。'
            );
            return false;
        }

        if (typeof agent.resumeAnalysisLayout === 'function') {
            agent.resumeAnalysisLayout();
        } else {
            const rp = document.getElementById('analysisRoutePanel');
            if (rp) rp.style.display = 'flex';
            window.PathCopilotV2?.setSubagentPaneOpen?.(true);
        }
        if (typeof agent._setEvidencePaneMode === 'function') {
            agent._setEvidencePaneMode(agent.activeAnalysisStrategy);
        }

        const modeName = isRoam
            ? ((window.__lang || 'zh') === 'en' ? 'Single Agent Roam' : '单 Agent 游走')
            : (isCollaborative ? __t('collab_mode_tag') : __t('auto_mode_tag'));
        const modeTag = document.getElementById('routeModeTag');
        if (modeTag) {
            if (isRoam) {
                modeTag.innerHTML = `<i class="fas fa-person-walking"></i> ${modeName}`;
                modeTag.className = 'route-mode-tag single-agent-mode';
            } else if (isCollaborative) {
                modeTag.innerHTML = `<i class="fas fa-users"></i> ${modeName}`;
                modeTag.className = 'route-mode-tag collaborative-mode';
            } else {
                modeTag.innerHTML = `<i class="fas fa-wand-magic-sparkles"></i> ${modeName}`;
                modeTag.className = 'route-mode-tag auto-mode';
            }
        }

        const routeStepsList = document.getElementById('routeStepsList');
        const mapHtml = this._rewritePatientWorkspaceHtml(
            this._patientWorkspaceMapHtml(doc, slides),
            idMap
        );
        const currentId = String(this.currentFile?.file_id || '');
        const currentEntry = uploadResults.find((item) => String(item.file_id) === currentId)
            || uploadResults[0];
        const viewIndex = currentEntry?.index ?? 0;
        agent.currentViewingWsiIndex = viewIndex;

        if (isRoam) {
            // Rebuild from trajectory. Replaying saved HTML keeps the last
            // clicked FOV yellow and also lights the left-rail tree.
            agent._restorePatientRoamMapsFromCache?.(viewIndex);
        } else if (routeStepsList && mapHtml) {
            routeStepsList.innerHTML = mapHtml;
            const activity = routeStepsList.querySelector('#v2AgentActivity');
            if (activity) {
                activity.classList.remove('is-running');
                activity.classList.add('is-completed');
                const percent = activity.querySelector('.v2-agent-current-percent');
                const fill = activity.querySelector('.v2-agent-progress-fill');
                const status = activity.querySelector('.v2-agent-current-text');
                if (percent) percent.textContent = '100%';
                if (fill) fill.style.width = '100%';
                if (status) {
                    status.textContent = (window.__lang || 'zh') === 'en'
                        ? 'Workspace restored'
                        : '已从工作区缓存恢复';
                }
            }
            agent._hydrateRoamActivityMapping?.(routeStepsList);
            agent._tagPatientActivityItems?.(routeStepsList);
            this.rebindLeftPanelClickEvents(routeStepsList);
        }

        if (!isRoam && !mapHtml) {
            agent._restorePatientSwarmMapsFromCache?.(viewIndex);
        }

        let chatMessages = Array.isArray(doc.chatHistory) ? doc.chatHistory : [];
        if (!chatMessages.length) {
            slides.forEach((slide) => {
                if (Array.isArray(slide.chatHistory) && slide.chatHistory.length) {
                    chatMessages = chatMessages.concat(slide.chatHistory);
                }
            });
        }
        this._renderRestoredChatHistory(chatMessages);
        this._ensureRestoredAnalysisInChat();
        await this._appendPatientLevelReportsToChat(uploadResults, this.currentPatientId, {
            includeSlideReports: false,
        });
        agent.currentViewingWsiIndex = viewIndex;
        this.clearAllROIOverlays();
        this.restoreWsiOverlays(viewIndex);
        if (typeof this.selectPlWsiItem === 'function') {
            this.selectPlWsiItem(viewIndex);
        }
        this.updatePlWsiStatus?.(viewIndex, 'completed', 'check');
        uploadResults.forEach((item) => {
            this.updatePlWsiStatus?.(item.index, 'completed', 'check');
            this._rememberPatientSlideNavState?.(item.index, 'completed');
        });
        this._setPatientLevelRunState?.(false);
        if (window.pathChatAgent) window.pathChatAgent._activePatientRunToken = null;
        this.syncPatientSlideReportNav?.();
        this.updatePlProgress?.(
            100,
            (window.__lang || 'zh') === 'en' ? 'Workspace restored' : '已从工作区缓存恢复'
        );
        console.log(`[Patient Level] ✅ 已从工作区 JSON 恢复 ${matchedCount} 张切片`);
        return true;
    }

    /**
     * 应用分析状态（从 JSON 文件或缓存加载共用）
     * ⭐ 完整版：所见即所得恢复，包括 ROI、左侧面板、聊天、模式标签、报告链接
     */
    async applyAnalysisState(state, fileId, isCollaborativeMode, loadedMode = null) {
        if (!state || !fileId) return;
        const restoredMode = String(loadedMode || state.mode || (isCollaborativeMode ? 'collaborative' : 'ai'));
        const isSingleAgentRoam = restoredMode === 'single_agent_roam';
        const modeName = isSingleAgentRoam
            ? ((window.__lang || 'zh') === 'en' ? 'Single Agent Roam' : '单 Agent 游走')
            : (isCollaborativeMode ? __t('collab_mode_tag') : __t('auto_mode_tag'));
        const isEn = (window.__lang || 'zh') === 'en';
        console.log('📦 应用分析状态:', state);
        if (state.savedAt) console.log('📅 保存时间:', state.savedAt);
        this._rememberWorkspaceJson(state);
        
        this.roiAnalysisData = state.roiAnalysisData || {};
        this._lastRestoredGlobalReasoning = String(state.globalReasoning || state.report || '').trim();
            
            // 恢复ROI覆盖层（⭐ 使用分批渲染避免浏览器崩溃）
            if (state.roiOverlays && state.roiOverlays.length > 0) {
                this.clearAllROIOverlays();
                
                const BATCH_SIZE = 15;
                const overlaysToRestore = state.roiOverlays;
                
                if (overlaysToRestore.length <= BATCH_SIZE) {
                    overlaysToRestore.forEach(overlay => {
                        this.drawColoredROI(overlay.data);
                    });
                    console.log(`✅ 恢复 ${overlaysToRestore.length} 个ROI覆盖层`);
                } else {
                    console.log(`🔄 分批恢复 ${overlaysToRestore.length} 个ROI覆盖层...`);
                    let batchIndex = 0;
                    const self = this;
                    
                    const restoreBatch = () => {
                        const start = batchIndex * BATCH_SIZE;
                        const end = Math.min(start + BATCH_SIZE, overlaysToRestore.length);
                        const batch = overlaysToRestore.slice(start, end);
                        
                        batch.forEach(overlay => {
                            self.drawColoredROI(overlay.data);
                        });
                        
                        batchIndex++;
                        
                        if (end < overlaysToRestore.length) {
                            requestAnimationFrame(restoreBatch);
                        } else {
                            console.log(`✅ 分批恢复完成: ${overlaysToRestore.length} 个ROI覆盖层`);
                        }
                    };
                    
                    requestAnimationFrame(restoreBatch);
                }
            }
            
            // ⭐ 恢复左侧分析面板（优先使用 analysis_tree.json，降级到 leftPanelHtml）
            const routeStepsList = document.getElementById('routeStepsList');
            if (routeStepsList) {
                let leftPanelRendered = false;
                
                try {
                    if (isSingleAgentRoam) throw new Error('single_agent_roam_uses_saved_timeline');
                    const treeResp = await fetch(this._analysisTreeRequestUrl());
                    const treeResult = await treeResp.json();
                    
                    if (treeResult.success && treeResult.data) {
                        const treeNodes = treeResult.data.tree_nodes || [];
                        const metadata = treeResult.data.metadata || {};
                        
                        const analysisData = {
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
                            analysisData.redBoxes.push(redBox);
                        });
                        
                        routeStepsList.innerHTML = '<div class="analysis-tree"></div>';
                        const treeEl = routeStepsList.querySelector('.analysis-tree');
                        this.updatePlWsiAnalysisTree(0, analysisData, treeEl);
                        
                        // ⭐ 保存快照里的 roiOverlays 只含保存那一刻画布上的框，
                        // 蓝/绿框经常缺失；这里按树数据把缺的补画到 viewer 上。
                        this._backfillOverlaysFromTreeBoxes(analysisData.redBoxes);
                        
                        leftPanelRendered = true;
                        console.log(`✅ 从 analysis_tree.json 恢复左侧面板: ${analysisData.redBoxes.length} 红框`);
                    }
                } catch (err) {
                    console.warn('⚠️ analysis_tree.json 加载失败，降级到 leftPanelHtml:', err);
                }
                
                if (!leftPanelRendered && state.leftPanelHtml) {
                    routeStepsList.innerHTML = state.leftPanelHtml;
                    window.pathChatAgent?._hydrateRoamActivityMapping?.(routeStepsList);
                    this.rebindLeftPanelClickEvents(routeStepsList);
                    leftPanelRendered = true;
                    console.log(`✅ 从 leftPanelHtml 恢复左侧面板（HTML长度: ${state.leftPanelHtml.length}）`);
                }
                
                if (!leftPanelRendered) {
                    console.warn('⚠️ 没有可用的左侧面板数据');
                    this.showNotification('warning', __t('cache_too_old'));
                }
            }
            
            // ⭐ 只要有ROI数据就显示分析面板
            const hasRoiData = (state.roiOverlays && state.roiOverlays.length > 0) || 
                              (state.leftPanelHtml && state.leftPanelHtml.length > 0);
            
            if (hasRoiData) {
                const analysisPanel = document.getElementById('analysisRoutePanel');
                const filePanel = document.getElementById('patientLevelPanel');
                
                if (analysisPanel) {
                    analysisPanel.style.display = 'flex';
                    console.log('✅ 分析面板已显示 (display: flex)');
                }
                if (filePanel) {
                    filePanel.style.display = 'none';
                    console.log('✅ 文件列表面板已隐藏');
                }
                
                const modeTag = document.getElementById('routeModeTag');
                if (modeTag) {
                    if (isSingleAgentRoam) {
                        modeTag.innerHTML = `<i class="fas fa-person-walking"></i> ${modeName}`;
                        modeTag.className = 'route-mode-tag single-agent-mode';
                    } else if (isCollaborativeMode) {
                        modeTag.innerHTML = `<i class="fas fa-users"></i> ${__t('collab_mode_tag')}`;
                        modeTag.className = 'route-mode-tag collaborative-mode';
                        if (window.pathChatAgent) {
                            window.pathChatAgent.isCollaborativeMode = true;
                            window.pathChatAgent.currentMode = 'collaborative';
                        }
                    } else {
                        modeTag.innerHTML = `<i class="fas fa-wand-magic-sparkles"></i> ${__t('auto_mode_tag')}`;
                        modeTag.className = 'route-mode-tag auto-mode';
                        if (window.pathChatAgent) {
                            window.pathChatAgent.isCollaborativeMode = false;
                            window.pathChatAgent.currentMode = 'auto';
                        }
                    }
                    console.log(`✅ 模式标签已更新为: ${modeName}`);
                }
                
                const slideNameEl = document.getElementById('routeSlideName');
                if (slideNameEl && this.currentFile) {
                    slideNameEl.textContent = this.currentFile.filename || __t('slide_loaded');
                }
                
                const progressFill = document.getElementById('routeProgressFill');
                const progressText = document.getElementById('routeProgressText');
                if (progressFill) progressFill.style.width = '100%';
                if (progressText) {
                    const typingDots = progressText.querySelector('.typing-dots');
                    if (typingDots) typingDots.style.display = 'none';
                    
                    const messageSpan = progressText.querySelector('.progress-message');
                    if (messageSpan) {
                        messageSpan.textContent = __t('analysis_loaded_cache');
                    } else {
                        progressText.textContent = __t('analysis_loaded_cache');
                    }
                }
            }
            
            // ⭐ 恢复ROI映射关系
            if (state.roiMappings) {
                this.roiMappings = state.roiMappings;
                console.log(`✅ ROI映射关系已恢复（${Object.keys(state.roiMappings).length}个映射）`);
            }
            
            // 恢复聊天历史。先权威清空，避免空历史或较短历史残留上一次 Run 的报告。
            this.elements.chatMessages.innerHTML = '';
            this.chatHistory = Array.isArray(state.chatHistory) ? state.chatHistory : [];
            if (this.chatHistory.length > 0) {
                console.log(`📝 恢复${state.chatHistory.length}条聊天消息...`);
                
                this.chatHistory.forEach((msg, index) => {
                    const messageElement = document.createElement('div');
                    messageElement.className = `message ${msg.role}-message`;
                    
                    let roiId = msg.roiId;
                    
                    if (!roiId && msg.content) {
                        const roiMatch = msg.content.match(/roi_\d+x_r\d+_c\d+(?:_sub_r\d+_c\d+)*/);
                        if (roiMatch) {
                            roiId = roiMatch[0];
                        }
                    }
                    
                    if (roiId) {
                        messageElement.setAttribute('data-roi-id', roiId);
                        
                        const contentLength = msg.content.length;
                        const content = msg.content;
                        
                        const isStartMessage = content.includes('开始分析') || 
                                              content.includes('Begin analysis') ||
                                              (content.includes('🔴') && contentLength < 50) ||
                                              (content.includes('🔵') && contentLength < 50) ||
                                              (content.includes('🟢') && contentLength < 50);
                        
                        const hasResultKeywords = content.includes('分析结果') || 
                                                 content.includes('分析完成') ||
                                                 content.includes('组织') ||
                                                 content.includes('细胞') ||
                                                 content.includes('病理') ||
                                                 content.includes('特征') ||
                                                 content.includes('结构');
                        
                        const isRedFrameResult = roiId.includes('roi_10x_r') && 
                                                !roiId.includes('_sub_') && 
                                                contentLength > 80 && 
                                                !isStartMessage;
                        
                        const isSubFrameResult = roiId.includes('_sub_') && 
                                                contentLength > 50 && 
                                                hasResultKeywords && 
                                                !isStartMessage;
                        
                        const isResult = isRedFrameResult || isSubFrameResult;
                        
                        const laterSameRoi = this.chatHistory.slice(index + 1).some(laterMsg => {
                            const laterRoiId = laterMsg.roiId || (laterMsg.content && laterMsg.content.match(/roi_\d+x_r\d+_c\d+(?:_sub_r\d+_c\d+)*/)?.[0]);
                            if (laterRoiId !== roiId) return false;
                            
                            const laterContent = laterMsg.content;
                            const laterIsStart = laterContent.includes('开始分析') || 
                                                (laterContent.includes('🔴') && laterContent.length < 50);
                            const laterHasKeywords = laterContent.includes('分析结果') || 
                                                    laterContent.includes('组织') ||
                                                    laterContent.includes('细胞') ||
                                                    laterContent.includes('病理');
                            const laterIsResult = laterContent.length > 80 && laterHasKeywords && !laterIsStart;
                            
                            return laterIsResult;
                        });
                        
                        if (isResult && !laterSameRoi) {
                            messageElement.id = `message-${roiId}`;
                            console.log(`✅ 设置消息ID: message-${roiId} (内容长度: ${contentLength}, 是红框: ${isRedFrameResult})`);
                        }
                    }
                    
                    const avatar = msg.role === 'assistant' ? '<i class="fas fa-robot"></i>' : '<i class="fas fa-user"></i>';
                    
                    messageElement.innerHTML = `
                        <div class="message-avatar">
                            ${avatar}
                        </div>
                        <div class="message-content">
                            <div class="message-text">
                                ${this.formatMessageContent(msg.content)}
                            </div>
                            <div class="message-time">${msg.timestamp ? new Date(msg.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : ''}</div>
                        </div>
                    `;
                    
                    this.elements.chatMessages.appendChild(messageElement);
                });
                
                this.scrollToBottom();
                
                console.log('📋 所有设置了消息ID的元素:');
                document.querySelectorAll('[id^="message-"]').forEach(msg => {
                    const roiId = msg.id.replace('message-', '');
                    const contentPreview = msg.querySelector('.message-text')?.textContent.substring(0, 50) || '';
                    console.log(`  - ${msg.id} (内容: ${contentPreview}...)`);
                });
            }
            this._ensureRestoredAnalysisInChat();
            
            // ⭐ 查找并添加 HTML 报告链接
            try {
                const reportMode = isSingleAgentRoam
                    ? 'single_agent_roam'
                    : (isCollaborativeMode ? 'collaborative' : 'ai');
                const reportResp = await fetch('/api/agent/find-reports', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        file_ids: [fileId],
                        mode: reportMode,
                        output_dir: window.pathChatAgent?.currentOutputDirName || ''
                    })
                });
                const reportResult = await reportResp.json();
                if (reportResult.success && reportResult.reports && reportResult.reports[fileId]) {
                    const _rd = reportResult.reports[fileId];
                    const reportUrl = typeof _rd === 'string' ? _rd : _rd?.url;
                    const chatMessages = this.elements.chatMessages;
                    if (chatMessages) {
                        const btnLabel = isEn ? 'View Full Report' : '查看完整报告';
                        const fname = this.currentFile?.filename || fileId;
                        const reportEl = document.createElement('div');
                        reportEl.className = 'message assistant-message';
                        reportEl.innerHTML = `
                            <div class="message-avatar"><i class="fas fa-robot"></i></div>
                            <div class="message-content">
                                <div class="message-text">
                                    <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                                        <span>📄 <strong>${fname}</strong></span>
                                        <a href="${reportUrl}" target="_blank" 
                                           style="display:inline-flex; align-items:center; gap:4px; padding:6px 14px; 
                                                  background:linear-gradient(135deg,#667eea 0%,#764ba2 100%); color:white; 
                                                  border-radius:6px; text-decoration:none; font-size:13px; font-weight:500;"
                                           onmouseover="this.style.opacity='0.85'" onmouseout="this.style.opacity='1'">
                                            <i class="fas fa-file-medical-alt"></i> ${btnLabel}
                                        </a>
                                    </div>
                                </div>
                            </div>
                        `;
                        chatMessages.appendChild(reportEl);
                        
                        
                        this.scrollToBottom();
                    }
                }
            } catch (reportErr) {
                console.warn('⚠️ 查找报告失败:', reportErr);
            }
    }

    sanitizeExportStem(filename) {
        const stripped = String(filename || 'analysis').replace(/\.[^.]+$/, '');
        const cleaned = stripped
            .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')
            .replace(/\s+/g, '_')
            .replace(/_+/g, '_')
            .replace(/^_+|_+$/g, '');
        return (cleaned || 'analysis').slice(0, 72);
    }

    suggestedAnalysisExportJsonName() {
        const stem = this.sanitizeExportStem(this.currentFile?.filename || 'analysis');
        return `${stem}_analysis.json`;
    }

    suggestedPatientExportJsonName() {
        const stem = this.sanitizeExportStem(
            this.patientLevelFolderName || this.currentPatientId || 'patient'
        );
        return `${stem}_patient_analysis.json`;
    }

    _collectRoamTrajectoryForFile(fileId, wsiIndex) {
        const agent = window.pathChatAgent;
        const roamEntry = agent?._patientRoamByFile?.get(String(fileId || ''));
        const fromEvents = (roamEntry?.events || [])
            .filter((item) => item?.kind === 'step')
            .map((item) => ({ kind: 'step', ...(item.payload || {}) }));
        const wsiData = agent?.patientLevelAnalysisData?.[wsiIndex] || {};
        const fromData = Array.isArray(wsiData.trajectory) ? wsiData.trajectory.slice() : [];
        if (fromData.length >= fromEvents.length && fromData.length) return fromData;
        return fromEvents;
    }

    _roamOverlaysFromTrajectory(trajectory, wsiIndex) {
        return (trajectory || [])
            .filter((item) => item?.rect)
            .map((item) => {
                const step = Number(item.step) || 0;
                const id = window.pathChatAgent?._roamOverlayId?.(step, { wsi_index: wsiIndex })
                    || `ROAM-${step}`;
                const zoom = Number(item.zoom) || 10;
                return {
                    id,
                    data: {
                        id,
                        rect: item.rect,
                        magnification: zoom,
                        color: zoom >= 160 ? '#00CC00' : (zoom >= 40 ? '#0066FF' : '#FF0000'),
                    },
                };
            });
    }

    _swarmOverlaysFromWsiData(wsiData = {}) {
        const overlays = [];
        const roiAnalysis = {};
        (wsiData.redBoxes || []).forEach((redBox) => {
            if (redBox.rect) {
                overlays.push({
                    id: redBox.id,
                    data: { id: redBox.id, rect: redBox.rect, color: '#FF0000', magnification: 10 },
                });
                if (redBox.observation) {
                    roiAnalysis[redBox.id] = {
                        analysis: redBox.observation,
                        confidence: redBox.confidence || 0,
                    };
                }
            }
            (redBox.blueBoxes || []).forEach((blueBox) => {
                if (blueBox.rect) {
                    overlays.push({
                        id: blueBox.id,
                        data: { id: blueBox.id, rect: blueBox.rect, color: '#0066FF', magnification: 40 },
                    });
                    if (blueBox.observation) {
                        roiAnalysis[blueBox.id] = {
                            analysis: blueBox.observation,
                            confidence: blueBox.confidence || 0,
                        };
                    }
                }
                (blueBox.greenBoxes || []).forEach((greenBox) => {
                    if (greenBox.rect) {
                        overlays.push({
                            id: greenBox.id,
                            data: { id: greenBox.id, rect: greenBox.rect, color: '#00CC00', magnification: 160 },
                        });
                        if (greenBox.observation) {
                            roiAnalysis[greenBox.id] = {
                                analysis: greenBox.observation,
                                confidence: greenBox.confidence || 0,
                            };
                        }
                    }
                });
            });
        });
        return { overlays, roiAnalysis };
    }

    _buildPatientSlideCachePayload(entry, {
        isCurrent = false,
        mode = 'ai',
        isSingleAgentRoam = false,
        currentSnapshot = null,
    } = {}) {
        const agent = window.pathChatAgent;
        const wsiData = agent?.patientLevelAnalysisData?.[entry.index] || {};
        const histEntry = (agent?._collectedChatHistories || []).find(
            (item) => item.slideIndex === entry.index
        );
        const plTreeEl = document.querySelector(`.pl-wsi-body[data-index="${entry.index}"] .analysis-tree`);
        const base = {
            file_id: entry.file_id,
            filename: entry.filename || '',
            part: entry.part || '',
            index: entry.index,
            analysisComplete: true,
            savedAt: new Date().toISOString(),
            mode,
            analysisStrategy: isSingleAgentRoam ? 'single_agent_roam' : 'agent_swarm',
            protocol_version: isSingleAgentRoam ? 'single_agent_roam_v2' : 'agent_swarm_v1',
            diagnostic_question: String(agent?.activeDiagnosticQuestion || '').trim(),
            patient_id: this.currentPatientId || '',
            output_dir: agent?.currentOutputDirName || '',
            metadata: { version: '2.3', source: 'patient_level_manual_save' },
            overview: {
                file_id: entry.file_id,
                filename: entry.filename || '',
                part: entry.part || '',
                index: entry.index,
            },
        };
        if (isCurrent && currentSnapshot) {
            const trajectory = isSingleAgentRoam
                ? this._collectRoamTrajectoryForFile(entry.file_id, entry.index)
                : [];
            return {
                ...base,
                ...currentSnapshot,
                file_id: entry.file_id,
                filename: entry.filename || currentSnapshot.filename || '',
                part: entry.part || '',
                index: entry.index,
                trajectory,
                overview: base.overview,
                report: currentSnapshot.report || wsiData.report || currentSnapshot.globalReasoning || '',
                chatHistory: this._chatHistoryForSlideCache(entry, {
                    isCurrent: true,
                    currentSnapshot,
                    histEntry,
                }),
            };
        }
        if (isSingleAgentRoam) {
            const trajectory = this._collectRoamTrajectoryForFile(entry.file_id, entry.index);
            return {
                ...base,
                roiOverlays: this._roamOverlaysFromTrajectory(trajectory, entry.index),
                roiAnalysisData: {},
                chatHistory: this._chatHistoryForSlideCache(entry, { histEntry }),
                leftPanelHtml: document.getElementById('routeStepsList')?.innerHTML
                    || (plTreeEl ? plTreeEl.outerHTML : ''),
                globalReasoning: wsiData.globalReasoning || wsiData.report || '',
                report: wsiData.report || '',
                trajectory,
                roiMappings: {},
            };
        }
        const { overlays, roiAnalysis } = this._swarmOverlaysFromWsiData(wsiData);
        return {
            ...base,
            roiOverlays: overlays,
            roiAnalysisData: roiAnalysis,
            chatHistory: this._chatHistoryForSlideCache(entry, { histEntry }),
            leftPanelHtml: document.getElementById('routeStepsList')?.innerHTML
                || (plTreeEl ? plTreeEl.outerHTML : ''),
            globalReasoning: wsiData.globalReasoning || '',
            roiMappings: {},
        };
    }

    _patientWorkspaceExportDocument(slides, { mode, isSingleAgentRoam } = {}) {
        return {
            schema: 'pathcopilot_patient_workspace_v1',
            kind: 'patient_level',
            patient_id: this.currentPatientId || '',
            folder_name: this.patientLevelFolderName || '',
            analysis_strategy: isSingleAgentRoam ? 'single_agent_roam' : 'agent_swarm',
            mode,
            savedAt: new Date().toISOString(),
            slide_count: slides.length,
            chatHistory: this._workspaceChatHistoryForExport(),
            leftPanelHtml: document.getElementById('routeStepsList')?.innerHTML || '',
            slides,
        };
    }

    siblingExportHtmlName(jsonPathOrName) {
        const base = String(jsonPathOrName || 'analysis')
            .replace(/\\/g, '/')
            .split('/')
            .pop();
        const withoutJson = String(base || 'analysis').replace(/\.json$/i, '');
        return `${this.sanitizeExportStem(withoutJson)}_report.html`;
    }

    htmlReportFileToUrl(absPath) {
        const normalized = String(absPath || '').replace(/\\/g, '/');
        const match = normalized.match(/\/outputs\/(.+)$/i);
        return match ? `/outputs/${match[1]}` : '';
    }

    async fetchHtmlReportText(absPath) {
        const candidates = [];
        const fromAbs = this.htmlReportFileToUrl(absPath);
        if (fromAbs) candidates.push(fromAbs);
        const fromAgent = String(window.pathChatAgent?.currentHtmlReportUrl || '').trim();
        if (fromAgent && !candidates.includes(fromAgent)) candidates.push(fromAgent);
        for (const url of candidates) {
            try {
                const resp = await fetch(url);
                if (resp.ok) return await resp.text();
            } catch (error) {
                console.warn('读取 HTML 报告失败:', url, error);
            }
        }
        return '';
    }

    async readSavedAnalysisJsonText(fileId, mode) {
        try {
            const resp = await fetch(
                `/api/load_analysis_state/${encodeURIComponent(fileId)}?mode=${encodeURIComponent(mode || 'ai')}`
            );
            const data = await resp.json();
            if (!data?.success || !data.data) return '';
            return JSON.stringify(data.data, null, 2);
        } catch (error) {
            console.warn('读取已保存分析 JSON 失败:', error);
            return '';
        }
    }

    triggerBrowserDownload(filename, content, mime) {
        const blob = new Blob([content], { type: mime || 'application/octet-stream' });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = filename;
        anchor.rel = 'noopener';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1500);
    }

    async beginAnalysisExport(suggestedName) {
        const desktop = window.pathcopilotDesktop;
        if (desktop?.isDesktop && typeof desktop.chooseAnalysisExportPath === 'function') {
            const picked = await desktop.chooseAnalysisExportPath({ suggestedName });
            if (!picked || picked.canceled) return { canceled: true };
            return { kind: 'desktop-path', jsonPath: picked.jsonPath, canceled: false };
        }
        if (typeof window.showSaveFilePicker === 'function') {
            try {
                const handle = await window.showSaveFilePicker({
                    suggestedName,
                    id: 'pathcopilot-analysis-export',
                    startIn: 'documents',
                    types: [{
                        description: (window.__lang || 'zh') === 'en'
                            ? 'PathCopilot workspace cache'
                            : 'PathCopilot 工作区缓存',
                        accept: { 'application/json': ['.json'] }
                    }]
                });
                return { kind: 'file-handle', handle, canceled: false };
            } catch (error) {
                if (error && error.name === 'AbortError') return { canceled: true };
                console.warn('showSaveFilePicker 不可用，改为浏览器下载:', error);
            }
        }
        return { kind: 'download', canceled: false };
    }

    async writeSiblingHtmlExport(jsonHandle, htmlFile) {
        try {
            if (jsonHandle && typeof jsonHandle.getParent === 'function') {
                const dir = await jsonHandle.getParent();
                const htmlHandle = await dir.getFileHandle(htmlFile.name, { create: true });
                const writable = await htmlHandle.createWritable();
                await writable.write(htmlFile.content);
                await writable.close();
                return htmlHandle.name || htmlFile.name;
            }
        } catch (error) {
            console.warn('无法写入同目录 HTML 报告，改为下载:', error);
        }
        this.triggerBrowserDownload(htmlFile.name, htmlFile.content, htmlFile.mime || 'text/html');
        return htmlFile.name;
    }

    async finishAnalysisExport(target, files) {
        const jsonFile = files[0];
        const htmlFile = files[1] || null;
        if (!jsonFile) return { canceled: true };
        if (target.kind === 'desktop-path' && window.pathcopilotDesktop?.writeAnalysisExportFiles) {
            const result = await window.pathcopilotDesktop.writeAnalysisExportFiles({
                jsonPath: target.jsonPath,
                files
            });
            if (!result || result.canceled) return { canceled: true };
            if (!result.ok) throw new Error(result.error || 'export failed');
            const written = result.files || [];
            return {
                ok: Boolean(result.ok),
                jsonPath: written[0] || target.jsonPath,
                htmlPath: written[1] || '',
                canceled: false
            };
        }
        if (target.kind === 'file-handle' && target.handle) {
            const writable = await target.handle.createWritable();
            await writable.write(jsonFile.content);
            await writable.close();
            let htmlPath = '';
            if (htmlFile) {
                htmlPath = await this.writeSiblingHtmlExport(target.handle, htmlFile);
            }
            return {
                ok: true,
                jsonPath: target.handle.name || jsonFile.name,
                htmlPath,
                canceled: false
            };
        }
        for (const file of files) {
            this.triggerBrowserDownload(file.name, file.content, file.mime);
        }
        return {
            ok: true,
            jsonPath: jsonFile.name,
            htmlPath: htmlFile ? htmlFile.name : '',
            fallbackDownload: true,
            canceled: false
        };
    }

    /**
     * 保存分析状态
     * 工具栏磁盘按钮：先把当前完整工作区写入应用缓存（ROI、中间栏、聊天、映射），
     * 再把同一份快照另存为 JSON，供「从文件加载」。取消另存不影响缓存。
     * Patient Level（≥2 张切片）一次缓存并导出全部切片，而不是只导出当前正在看的那一张。
     * Agent 完成后的自动保存仍只写应用缓存，不弹窗。
     */
    async saveAnalysisState(options = {}) {
        const silent = options?.silent === true;
        const exportToUserPath = options?.exportToUserPath === true;
        if (!this.currentFile) {
            const _isEn = (window.__lang || 'zh') === 'en';
            if (!silent) {
                this.showNotification('warning', _isEn ? 'No file loaded' : '没有当前文件');
            }
            return;
        }

        try {
            const isCollaborativeMode = window.pathChatAgent?.isCollaborativeMode || false;
            const isSingleAgentRoam = window.pathChatAgent?.activeAnalysisStrategy === 'single_agent_roam';
            const mode = isSingleAgentRoam
                ? 'single_agent_roam'
                : (isCollaborativeMode ? 'collaborative' : 'ai');
            const modeName = isSingleAgentRoam
                ? ((window.__lang || 'zh') === 'en' ? 'Single Agent Roam' : '单 Agent 游走')
                : (isCollaborativeMode ? __t('collab_mode_tag') : __t('auto_mode_tag'));
            
            console.log(`💾 开始保存分析状态 (${modeName})...`);
            
            // ⭐ 保存当前 WSI 的完整状态（左侧面板 + 映射 + 聊天 + 覆盖层）
            const routeStepsList = document.getElementById('routeStepsList');
            const leftPanelHtml = routeStepsList ? routeStepsList.innerHTML : '';
            
            const roiMappings = {};
            if (routeStepsList) {
                const allCards = routeStepsList.querySelectorAll('[data-roi-id]');
                allCards.forEach(card => {
                    const roiId = card.dataset.roiId;
                    if (roiId) {
                        roiMappings[roiId] = {
                            level: card.dataset.level || '',
                            treeId: card.dataset.treeId || '',
                            className: card.className
                        };
                    }
                });
            }
            
            // ⭐ 当前WSI也保存 globalReasoning（Patient Level 时从内存获取）
            const currentIndex = this.patientLevelUploadResults?.find(r => r.file_id === this.currentFile.file_id)?.index;
            const currentGlobalReasoning = (currentIndex != null && window.pathChatAgent?.patientLevelAnalysisData?.[currentIndex]?.globalReasoning) || '';

            const analysisData = {
                roiOverlays: this.roiOverlays.map(overlay => ({
                    id: overlay.id,
                    data: overlay.data
                })),
                roiAnalysisData: this.roiAnalysisData,
                chatHistory: this.chatHistory,
                leftPanelHtml: leftPanelHtml,
                globalReasoning: currentGlobalReasoning,
                roiMappings: roiMappings,
                analysisComplete: true,
                savedAt: new Date().toISOString(),
                mode: mode,
                analysisStrategy: isSingleAgentRoam ? 'single_agent_roam' : 'agent_swarm',
                diagnostic_question: String(window.pathChatAgent?.activeDiagnosticQuestion || '').trim(),
                protocol_version: isSingleAgentRoam ? 'single_agent_roam_v2' : 'agent_swarm_v1',
                filename: this.currentFile.filename || '',
                patient_id: this.currentPatientId || '',
                output_dir: window.pathChatAgent?.currentOutputDirName || ''
            };
            
            console.log(`💾 保存数据 (${modeName}): ${this.roiOverlays.length}个ROI, ${Object.keys(roiMappings).length}个映射, 左侧面板HTML长度=${leftPanelHtml.length}`);

            const plUploadResults = this.patientLevelUploadResults || [];
            const isPatientLevelSave = plUploadResults.length > 1 && plUploadResults.some(
                (entry) => String(entry.file_id) === String(this.currentFile.file_id)
            );
            const slidePayloads = [];
            let result = { success: true, total_rois: analysisData.roiOverlays.length };
            let totalSaved = 0;

            if (isPatientLevelSave) {
                for (const entry of plUploadResults) {
                    const isCurrent = String(entry.file_id) === String(this.currentFile.file_id);
                    const payload = this._buildPatientSlideCachePayload(entry, {
                        isCurrent,
                        mode,
                        isSingleAgentRoam,
                        currentSnapshot: isCurrent ? analysisData : null,
                    });
                    slidePayloads.push(payload);
                    try {
                        const slideResponse = await fetch(`/api/save_analysis_state/${entry.file_id}`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(payload),
                        });
                        const slideResult = await slideResponse.json();
                        if (slideResult.success) {
                            totalSaved += 1;
                            if (isCurrent) result = slideResult;
                            console.log(`💾 [Patient Level] 已保存: ${entry.part || entry.filename} (${entry.file_id})`);
                        } else {
                            console.warn(`💾 [Patient Level] 保存失败: ${entry.part || entry.filename}`, slideResult.error);
                        }
                    } catch (err) {
                        console.warn(`💾 [Patient Level] 保存失败: ${entry.part || entry.filename}`, err);
                    }
                }
                if (totalSaved < 1) {
                    throw new Error((window.__lang || 'zh') === 'en'
                        ? 'No patient slides could be saved'
                        : '没有切片被成功保存');
                }
            } else {
                const response = await fetch(`/api/save_analysis_state/${this.currentFile.file_id}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(analysisData)
                });
                result = await response.json();
                if (!result.success) throw new Error(result.error);
                totalSaved = 1;
            }
            
            const _isEn = (window.__lang || 'zh') === 'en';
            let saveSummary = '';
            if (isPatientLevelSave) {
                saveSummary = _isEn
                    ? `${modeName} saved: ${totalSaved}/${plUploadResults.length} slides`
                    : `${modeName}已保存：${totalSaved}/${plUploadResults.length} 张切片`;
            } else {
                const mappingCount = Object.keys(roiMappings).length;
                saveSummary = exportToUserPath
                    ? (_isEn
                        ? `${modeName} restorable state saved (${result.total_rois} ROIs, ${mappingCount} mappings)`
                        : `${modeName}可恢复状态已保存（共${result.total_rois}个ROI，${mappingCount}个映射关系）`)
                    : (_isEn
                        ? `${modeName} results saved (${result.total_rois} ROIs, ${mappingCount} mappings)`
                        : `${modeName}结果已保存（共${result.total_rois}个ROI，${mappingCount}个映射关系）`);
            }
            if (exportToUserPath) {
                let exportTarget = null;
                const suggestedName = isPatientLevelSave
                    ? this.suggestedPatientExportJsonName()
                    : this.suggestedAnalysisExportJsonName();
                try {
                    exportTarget = await this.beginAnalysisExport(suggestedName);
                } catch (error) {
                    console.warn('选择导出路径失败:', error);
                    this.showNotification(
                        'warning',
                        _isEn
                            ? 'App cache saved. Could not open the save dialog for a local copy.'
                            : '应用缓存已保存。无法打开本地副本的保存对话框。'
                    );
                    return;
                }
                if (!exportTarget || exportTarget.canceled) {
                    this.showNotification(
                        'success',
                        _isEn
                            ? 'App cache updated. Load it later with “Load from cache”.'
                            : '应用缓存已更新。下次可用「从缓存加载」恢复当前工作区。'
                    );
                    return;
                }
                const jsonName = suggestedName;
                const jsonText = isPatientLevelSave
                    ? JSON.stringify(
                        this._patientWorkspaceExportDocument(slidePayloads, {
                            mode,
                            isSingleAgentRoam,
                        }),
                        null,
                        2
                    )
                    : ((await this.readSavedAnalysisJsonText(this.currentFile.file_id, mode))
                        || JSON.stringify({
                            file_id: this.currentFile.file_id,
                            ...analysisData
                        }, null, 2));
                const exportFiles = [{
                    name: jsonName,
                    content: jsonText,
                    mime: 'application/json'
                }];
                try {
                    const written = await this.finishAnalysisExport(exportTarget, exportFiles);
                    if (written?.canceled) {
                        this.showNotification(
                            'info',
                            _isEn
                                ? 'App cache updated. Save canceled.'
                                : '应用缓存已更新，已取消保存到本地。'
                        );
                        return;
                    }
                    this.showSavedStateNotification({
                        cacheFile: written.jsonPath || jsonName,
                        htmlReportFile: '',
                        summary: saveSummary,
                        exported: true,
                        fallbackDownload: Boolean(written.fallbackDownload),
                        note: written.fallbackDownload
                            ? (_isEn
                                ? (isPatientLevelSave
                                    ? 'App cache is already updated. Confirm the JSON copy in the download bar; it contains every slide in this patient run.'
                                    : 'App cache is already updated. Confirm the JSON copy in the download bar; it is the same snapshot.')
                                : (isPatientLevelSave
                                    ? '应用缓存已更新。请在下载栏确认 JSON 副本，其中包含本次患者级全部切片。'
                                    : '应用缓存已更新。请在下载栏确认 JSON 副本，内容与缓存是同一份工作区快照。'))
                            : (_isEn
                                ? (isPatientLevelSave
                                    ? 'App cache is already updated. This JSON is a portable copy of every slide in the patient workspace.'
                                    : 'App cache is already updated. This JSON is a portable copy of the same workspace snapshot.')
                                : (isPatientLevelSave
                                    ? '应用缓存已更新。此 JSON 是本次患者级全部切片（视野/ROI、聊天、轨迹）的一份本地副本。'
                                    : '应用缓存已更新。此 JSON 是同一份工作区快照的本地副本（ROI、中间栏、聊天、映射）。'))
                    });
                } catch (exportError) {
                    console.error('导出到所选路径失败:', exportError);
                    this.showNotification(
                        'warning',
                        _isEn
                            ? `Cache saved, but export failed: ${exportError.message}`
                            : `应用缓存已保存，但导出失败：${exportError.message}`
                    );
                }
                console.log(`✅ 分析状态保存成功 (${modeName}): 共${totalSaved}张切片`);
                return;
            }
            if (!silent) {
                this.showSavedStateNotification({
                    cacheFile: result.cache_file,
                    htmlReportFile: result.html_report_file,
                    summary: saveSummary
                });
            }
            console.log(`✅ 分析状态保存成功 (${modeName}): 共${totalSaved}张切片`);
            
        } catch (error) {
            console.error('保存分析状态失败:', error);
            const _isEn = (window.__lang || 'zh') === 'en';
            if (!silent) {
                this.showNotification('error', _isEn ? `Save failed: ${error.message}` : `保存失败：${error.message}`);
            }
        }
    }
    
    /**
     * 查询历史分析结果，多条则弹窗让用户选择，单条或无则直接加载
     */
    async _loadWithHistorySelection(fileInfo) {
        const mode = 'all';
        const filename = fileInfo.filename || '';
        
        if (!filename) {
            return this.loadAnalysisState(fileInfo.file_id);
        }
        
        try {
            const resp = await fetch(`/api/list-analysis-history?filename=${encodeURIComponent(filename)}&mode=${mode}`);
            const data = await resp.json();
            if (this._isLiveAnalysisBlockingHistoryChooser(fileInfo.file_id)) {
                console.log('[HistoryChooser] analysis already running; skip restore dialog');
                return;
            }
            
            if (!data.success || !data.results || data.results.length === 0) {
                return this.loadAnalysisState(fileInfo.file_id);
            }
            
            if (data.results.length === 1) {
                return this.loadAnalysisState(fileInfo.file_id, data.results[0].cache_name);
            }
            
            const isEn = (window.__lang || 'zh') === 'en';
            const results = data.results;
            
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay history-results-chooser';
            overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:10000;display:flex;align-items:center;justify-content:center;';
            
            let listHtml = results.map((r, i) => {
                const srcLabel = r.source === 'patient_level_auto_save' || r.source === 'patient_level_final_save' || r.source === 'patient_level_manual_save'
                    ? (isEn ? 'Patient Level' : '多切片') : (isEn ? 'Single slide' : '单切片');
                const analysisModeLabel = r.mode === 'collaborative'
                    ? (isEn ? 'Collaborative' : '协作')
                    : (r.mode === 'single_agent_roam'
                        ? (isEn ? 'Single Agent Roam' : '单 Agent 游走')
                        : (isEn ? 'Auto Swarm' : '自动 Swarm'));
                const panelTag = r.has_left_panel ? (isEn ? ' | panel saved' : ' | 含面板') : '';
                return `<button type="button" class="history-item" data-idx="${i}" style="display:block;width:100%;text-align:left;padding:12px 16px;border:1px solid #e0e0e0;border-radius:8px;margin-bottom:8px;cursor:pointer;transition:all 0.2s;background:#fff;"
                    onmouseover="this.style.background='#f0f4ff';this.style.borderColor='#667eea'" onmouseout="this.style.background='#fff';this.style.borderColor='#e0e0e0'">
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <strong style="color:#333;">num${r.num}</strong>
                        <span style="color:#888;font-size:13px;">${r.saved_at}</span>
                    </div>
                    <div style="margin-top:4px;font-size:13px;color:#666;">
                        ${r.total_rois} ROIs | ${r.total_messages} ${isEn ? 'messages' : '条消息'} | ${srcLabel} · ${analysisModeLabel}${panelTag}
                    </div>
                </button>`;
            }).join('');
            
            overlay.innerHTML = `
                <div style="background:#fff;border-radius:12px;padding:24px;max-width:480px;width:90%;max-height:70vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.2);">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                        <h3 style="margin:0;color:#333;">${isEn ? 'Analysis History' : '历史分析结果'}</h3>
                        <button id="historyCancel" style="padding:6px 16px;border:1px solid #ddd;border-radius:6px;background:#fff;cursor:pointer;color:#666;flex-shrink:0;">${isEn ? 'Cancel' : '取消'}</button>
                    </div>
                    <p style="margin:0 0 16px;color:#888;font-size:14px;">${isEn
                        ? `Found ${results.length} analysis results for this sample. Choose one to load:`
                        : `检测到该样本有 ${results.length} 次分析结果，请选择加载哪一次：`}</p>
                    <div class="history-list" style="overflow-y:auto;flex:1;min-height:0;">${listHtml}</div>
                </div>
            `;
            
            document.body.appendChild(overlay);
            
            return new Promise((resolve) => {
                overlay.querySelector('#historyCancel').addEventListener('click', () => {
                    overlay.remove();
                    resolve();
                });
                overlay.addEventListener('click', (e) => {
                    if (e.target === overlay) { overlay.remove(); resolve(); }
                });
                overlay.querySelectorAll('.history-item').forEach(item => {
                    item.addEventListener('click', async () => {
                        const idx = parseInt(item.dataset.idx);
                        const selected = results[idx];
                        overlay.remove();
                        await this.loadAnalysisState(fileInfo.file_id, selected.cache_name);
                        resolve();
                    });
                });
            });
            
        } catch (err) {
            console.warn('查询历史失败，直接加载:', err);
            return this.loadAnalysisState(fileInfo.file_id);
        }
    }
    
    /**
     * 加载分析状态
     * @param {string} fileId - 当前文件 ID
     * @param {string} [cacheName] - 指定缓存文件名（用户从历史选择），不传则按 file_id 查找
     */
    async loadAnalysisState(fileId, cacheName) {
        try {
            const requestedCollaborative = window.pathChatAgent?.isCollaborativeMode || false;
            const mode = requestedCollaborative ? 'collaborative' : 'ai';
            
            this.showLoading(__t('processing'));
            
            let response;
            if (cacheName) {
                response = await fetch(`/api/load-analysis-by-name?cache_name=${encodeURIComponent(cacheName)}`);
            } else {
                response = await fetch(`/api/load_analysis_state/${fileId}?mode=${mode}`);
            }
            const result = await response.json();
            
            if (!result.success) {
                throw new Error(result.error);
            }
            
            const state = result.data;
            const loadedMode = String(result.output_mode || result.mode || state.mode || mode);
            const isCollaborativeMode = loadedMode === 'collaborative';
            const isSingleAgentRoam = loadedMode === 'single_agent_roam';
            if (window.pathChatAgent) {
                window.pathChatAgent.currentOutputDirName = String(
                    result.output_dir_name || state.output_dir || ''
                );
                window.pathChatAgent.activeAnalysisStrategy = isSingleAgentRoam
                    ? 'single_agent_roam'
                    : 'agent_swarm';
                window.pathChatAgent.isCollaborativeMode = isCollaborativeMode;
                window.pathChatAgent.currentMode = isCollaborativeMode ? 'collaborative' : 'auto';
                window.pathChatAgent._setEvidencePaneMode?.(
                    window.pathChatAgent.activeAnalysisStrategy
                );
            }
            this._treeLoadedForId = null;
            this.analysisTreeData = null;
            await this.applyAnalysisState(state, fileId, isCollaborativeMode, loadedMode);
            
            const roiCount = result.total_rois || 0;
            const mappingCount = state.roiMappings ? Object.keys(state.roiMappings).length : 0;
            const isEn = (window.__lang || 'zh') === 'en';
            const leftPanelMsg = state.leftPanelHtml 
                ? (isEn ? ', left panel restored' : '，左侧面板已恢复') : '';
            this.showNotification('success', isEn
                ? `Loaded analysis (${roiCount} ROIs, ${mappingCount} mappings)${leftPanelMsg}`
                : `已加载分析结果（${roiCount}个ROI，${mappingCount}个映射）${leftPanelMsg}`);
            console.log('✅ Analysis state loaded');
            
        } catch (error) {
            if (this._isAbortError(error)) return;
            console.error('Load analysis state failed:', error);
            this.showNotification('error', (window.__lang||'zh')==='en' ? `Load failed: ${error.message}` : `加载失败：${error.message}`);
        } finally {
            this.hideLoading();
        }
    }
    
    /**
     * Patient Level 加载入口：先查历史，多条则弹窗让用户选择
     */
    async _loadPatientLevelWithHistory() {
        const folderName = this.patientLevelFolderName || '';
        if (!folderName) {
            return this.loadPatientLevelAnalysisState();
        }
        
        // The saved run's strategy is unknown here: the doctor may have used
        // Swarm, roam or collaboration. Asking for one mode hid roam folders
        // and listed them as "0 slides".
        const mode = 'all';
        const isEn = (window.__lang || 'zh') === 'en';
        
        try {
            const resp = await fetch(`/api/list-analysis-history?filename=${encodeURIComponent(folderName)}&mode=${mode}&patient=true`);
            const data = await resp.json();
            
            if (!data.success || !data.results || data.results.length === 0) {
                return this.loadPatientLevelAnalysisState();
            }
            
            if (data.results.length === 1) {
                return this._loadPatientLevelFromDir(data.results[0].patient_dir);
            }
            
            const results = data.results;
            const overlay = document.createElement('div');
            overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:10000;display:flex;align-items:center;justify-content:center;';
            
            const modeLabels = isEn
                ? { ai: 'Agent Swarm', collaborative: 'Collaborative', single_agent_roam: 'Single Agent roam' }
                : { ai: 'Agent Swarm', collaborative: '人机协作', single_agent_roam: '单 Agent 游走' };
            let listHtml = results.map((r, i) => {
                const modeText = (r.modes || []).map((m) => modeLabels[m] || m).join(' / ');
                return `<div class="history-item" data-idx="${i}" style="padding:12px 16px;border:1px solid #e0e0e0;border-radius:8px;margin-bottom:8px;cursor:pointer;transition:all 0.2s;background:#fff;"
                    onmouseover="this.style.background='#f0f4ff';this.style.borderColor='#667eea'" onmouseout="this.style.background='#fff';this.style.borderColor='#e0e0e0'">
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <strong style="color:#333;">num${r.num}</strong>
                        <span style="color:#888;font-size:13px;">${r.saved_at}</span>
                    </div>
                    <div style="margin-top:4px;font-size:13px;color:#666;">
                        ${r.wsi_count} ${isEn ? 'slides' : '张切片'} | ${r.total_rois} ROIs${modeText ? ` | ${modeText}` : ''}
                    </div>
                </div>`;
            }).join('');
            
            overlay.innerHTML = `
                <div style="background:#fff;border-radius:12px;padding:24px;max-width:480px;width:90%;max-height:70vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,0.2);">
                    <h3 style="margin:0 0 8px;color:#333;">${isEn ? 'Patient Analysis History' : '患者分析历史'}</h3>
                    <p style="margin:0 0 16px;color:#888;font-size:14px;">${isEn
                        ? `Found ${results.length} analyses for this patient. Choose one to load:`
                        : `检测到该患者有 ${results.length} 次分析结果，请选择加载哪一次：`}</p>
                    <div class="history-list">${listHtml}</div>
                    <div style="text-align:right;margin-top:16px;">
                        <button id="plHistoryCancel" style="padding:8px 20px;border:1px solid #ddd;border-radius:6px;background:#fff;cursor:pointer;color:#666;">${isEn ? 'Cancel' : '取消'}</button>
                    </div>
                </div>
            `;
            
            document.body.appendChild(overlay);
            
            return new Promise((resolve) => {
                overlay.querySelector('#plHistoryCancel').addEventListener('click', () => { overlay.remove(); resolve(); });
                overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); resolve(); } });
                overlay.querySelectorAll('.history-item').forEach(item => {
                    item.addEventListener('click', async () => {
                        const idx = parseInt(item.dataset.idx);
                        const selected = results[idx];
                        overlay.remove();
                        await this._loadPatientLevelFromDir(selected.patient_dir);
                        resolve();
                    });
                });
            });
        } catch (err) {
            console.warn('[Patient Level] 查询历史失败，使用默认加载:', err);
            return this.loadPatientLevelAnalysisState();
        }
    }
    
    _redBoxesFromCachedOverlays(stateData = {}) {
        const cachedAnalysis = stateData.roiAnalysisData || {};
        const redMap = {};
        (stateData.roiOverlays || []).forEach((o) => {
            const d = o.data || o;
            if (d.color === '#FF0000' && d.id) {
                redMap[d.id] = {
                    id: d.id,
                    rect: d.rect,
                    status: 'completed',
                    observation: cachedAnalysis[d.id]?.analysis || '',
                    confidence: cachedAnalysis[d.id]?.confidence || 0,
                    blueBoxes: [],
                };
            }
        });
        (stateData.roiOverlays || []).forEach((o) => {
            const d = o.data || o;
            if (d.color !== '#0066FF' || !d.id) return;
            const parentId = d.id.split('-')[0];
            if (!redMap[parentId]) return;
            redMap[parentId].blueBoxes.push({
                id: d.id,
                rect: d.rect,
                status: 'completed',
                observation: cachedAnalysis[d.id]?.analysis || '',
                confidence: cachedAnalysis[d.id]?.confidence || 0,
                greenBoxes: [],
            });
        });
        (stateData.roiOverlays || []).forEach((o) => {
            const d = o.data || o;
            if (d.color !== '#00CC00' || !d.id) return;
            const parts = d.id.split('-');
            const parentBlueId = parts.slice(0, 2).join('-');
            const parentRedId = parts[0];
            const blueBox = redMap[parentRedId]?.blueBoxes?.find((b) => b.id === parentBlueId);
            if (!blueBox) return;
            blueBox.greenBoxes.push({
                id: d.id,
                rect: d.rect,
                status: 'completed',
                observation: cachedAnalysis[d.id]?.analysis || '',
                confidence: cachedAnalysis[d.id]?.confidence || 0,
            });
        });
        return Object.values(redMap);
    }

    async _fetchPatientSlideAnalysisTree(fileId, filename = '', patientDir = '') {
        if (!fileId) return null;
        const params = new URLSearchParams();
        if (filename) params.set('filename', filename);
        const outputDir = window.pathChatAgent?.currentOutputDirName || '';
        if (outputDir) params.set('output_dir', outputDir);
        // Without the patient folder the search returns the newest tree for
        // this slide, which may belong to a different run and mode.
        const scopedPatient = patientDir || this.currentPatientId || '';
        if (scopedPatient) params.set('patient_dir', scopedPatient);
        const strategy = window.pathChatAgent?.activeAnalysisStrategy || '';
        const mode = strategy === 'single_agent_roam'
            ? 'single_agent_roam'
            : (window.pathChatAgent?.isCollaborativeMode ? 'collaborative' : 'ai');
        if (mode) params.set('mode', mode);
        const qs = params.toString();
        const url = `/api/agent/analysis-tree/${encodeURIComponent(fileId)}${qs ? `?${qs}` : ''}`;
        const treeResp = await fetch(url);
        const treeResult = await treeResp.json();
        if (!treeResult?.success || !treeResult.data) return null;
        return treeResult.data;
    }

    _patientCacheHtmlLooksLikeRouteMap(html) {
        const s = String(html || '');
        return /id=["']v2AgentActivity["']/.test(s)
            || /route-tree-node/.test(s)
            || /v2-agent-activity/.test(s)
            || /v2-subagent-stream/.test(s)
            || /patient-slide-map/.test(s)
            || /route-tree-blue-node/.test(s);
    }

    _patientCacheHtmlLooksLikeAnalysisTree(html) {
        const s = String(html || '');
        return /analysis-tree/.test(s) || /clickable-roi/.test(s) || /tree-branch/.test(s);
    }

    /**
     * Restore a cached slide panel. Disk saves store #routeStepsList HTML in
     * leftPanelHtml; the hidden left-rail .analysis-tree must not receive it.
     */
    _restorePatientLevelCachedPanel(index, stateData = {}, wsiData = {}) {
        if (window.pathChatAgent?.activeAnalysisStrategy === 'single_agent_roam') {
            const treeEl = document.querySelector(`.pl-wsi-body[data-index="${index}"] .analysis-tree`);
            if (treeEl) treeEl.innerHTML = '';
            return;
        }
        const html = String(stateData.leftPanelHtml || '');
        const looksLikeMap = this._patientCacheHtmlLooksLikeRouteMap(html);
        const looksLikeTree = this._patientCacheHtmlLooksLikeAnalysisTree(html);
        if (html.length > 10 && looksLikeTree && !looksLikeMap) {
            const body = document.querySelector(`.pl-wsi-body[data-index="${index}"]`);
            if (body) {
                let treeEl = body.querySelector('.analysis-tree');
                if (!treeEl) {
                    treeEl = document.createElement('div');
                    treeEl.className = 'analysis-tree';
                    body.appendChild(treeEl);
                }
                treeEl.outerHTML = html;
                this.bindPlWsiTreeClickEvents(body.querySelector('.analysis-tree'));
            }
            return;
        }
        if (typeof this.updatePlWsiAnalysisTree === 'function') {
            this.updatePlWsiAnalysisTree(index, wsiData);
        }
    }

    /**
     * 从指定的患者缓存目录加载所有 WSI 状态
     */
    async _loadPatientLevelFromDir(patientDir) {
        // Read whichever strategy wrote the folder; the response reports the
        // resolved mode per slide so the UI can restore the right pane.
        const mode = 'all';
        
        this.showLoading(__t('processing'));
        try {
            const resp = await fetch(`/api/load-patient-cache?patient_dir=${encodeURIComponent(patientDir)}&mode=${mode}`);
            const result = await resp.json();
            
            if (!result.success || !result.slides || result.slides.length === 0) {
                this.showNotification('warning', (window.__lang||'zh')==='en' ? 'No cached data found' : '未找到缓存数据');
                return;
            }
            
            this.currentPatientId = patientDir;
            
            const uploadResults = this.patientLevelUploadResults || [];
            if (window.pathChatAgent) {
                if (!window.pathChatAgent.patientLevelAnalysisData) window.pathChatAgent.patientLevelAnalysisData = {};
                window.pathChatAgent.isPatientLevelMode = true;
                const loadedRoam = result.mode === 'single_agent_roam'
                    || result.slides.some((slide) => {
                        const data = slide?.data || {};
                        return slide?.mode === 'single_agent_roam'
                            || data.mode === 'single_agent_roam'
                            || data.analysisStrategy === 'single_agent_roam'
                            || (Array.isArray(data.trajectory) && data.trajectory.length > 0);
                    });
                window.pathChatAgent.activeAnalysisStrategy = loadedRoam
                    ? 'single_agent_roam'
                    : 'agent_swarm';
                window.pathChatAgent._setEvidencePaneMode?.(
                    window.pathChatAgent.activeAnalysisStrategy
                );

                
                for (const slide of result.slides) {
                    const stateData = slide.data;
                    const entry = uploadResults.find(r => r.file_id === slide.file_id || r.filename === slide.filename);
                    const index = entry ? entry.index : uploadResults.indexOf(uploadResults.find(r => r.filename === slide.filename));
                    if (index < 0) continue;
                    
                    if (typeof window.pathchat?.updatePlWsiStatus === 'function') {
                        this.updatePlWsiStatus(index, 'completed', 'check');
                    }

                    const overlayBoxes = this._redBoxesFromCachedOverlays(stateData);
                    let wsiData = {
                        globalReasoning: stateData.globalReasoning || stateData.report || '',
                        report: stateData.report || '',
                        redBoxes: overlayBoxes,
                        leftPanelHtml: stateData.leftPanelHtml || '',
                        roiAnalysisData: stateData.roiAnalysisData || {},
                        roiOverlays: stateData.roiOverlays || [],
                        // Roam replays its middle-pane steps from trajectory;
                        // dropping it left the progress pane empty on reload.
                        trajectory: Array.isArray(stateData.trajectory) ? stateData.trajectory : [],
                        overview: stateData.overview || null,
                    };
                    window.pathChatAgent.patientLevelAnalysisData[index] = wsiData;

                    // Roam has no red/blue/green Subagent tree; hydrating one
                    // painted a 10x red box that the run never produced.
                    if (slide.file_id && !loadedRoam) {
                        try {
                            const treeData = await this._fetchPatientSlideAnalysisTree(
                                slide.file_id,
                                slide.filename || '',
                                patientDir
                            );
                            const treeNodes = treeData?.tree_nodes || [];
                            const treeReasoning = treeData?.metadata?.global_analysis || '';
                            if (treeNodes.length) {
                                const hydrated = window.pathChatAgent._hydratePatientSwarmDataFromResult?.(index, {
                                    tree_nodes: treeNodes,
                                    global_reasoning: treeReasoning || wsiData.globalReasoning,
                                });
                                if (hydrated) wsiData = hydrated;
                            } else if (treeReasoning && !wsiData.globalReasoning) {
                                wsiData.globalReasoning = treeReasoning;
                                window.pathChatAgent.patientLevelAnalysisData[index] = wsiData;
                            }
                        } catch (err) {
                            console.warn(`⚠️ [Patient Level] WSI #${index} analysis_tree.json 兜底失败:`, err);
                        }
                    }
                    
                    this._restorePatientLevelCachedPanel(index, stateData, wsiData);
                }
            }
            
            this._rememberWorkspaceJson({
                schema: 'pathcopilot_patient_workspace_v1',
                kind: 'patient_level',
                patient_id: patientDir || this.currentPatientId || '',
                chatHistory: result.chatHistory || [],
                leftPanelHtml: result.leftPanelHtml || '',
                slides: (result.slides || []).map((slide) => ({
                    ...(slide.data || {}),
                    file_id: slide.file_id,
                    filename: slide.filename || slide.data?.filename || '',
                })),
            });

            // 恢复聊天记录和报告
            if (window.pathChatAgent) {
                window.pathChatAgent._collectedChatHistories = [];
                for (const slide of result.slides) {
                    const stateData = slide.data;
                    if (stateData.chatHistory && stateData.chatHistory.length > 0) {
                        const entry = uploadResults.find(r => r.file_id === slide.file_id || r.filename === slide.filename);
                        window.pathChatAgent._collectedChatHistories.push({
                            slideIndex: entry ? entry.index : 0,
                            slideName: slide.filename || `Slide`,
                            messages: stateData.chatHistory
                        });
                    }
                }
            }
            
            const cachedFileIds = new Set(result.slides.map(s => s.file_id).filter(Boolean));
            const slideInfos = uploadResults
                .filter(r => cachedFileIds.has(r.file_id))
                .map(r => ({ file_id: r.file_id, filename: r.filename || '' }));
            await this._restorePatientLevelChatHistory(slideInfos, patientDir);

            const currentId = String(this.currentFile?.file_id || '');
            const currentEntry = uploadResults.find((item) => String(item.file_id) === currentId);
            const focusIndex = Number.isInteger(currentEntry?.index)
                ? currentEntry.index
                : (Number.isInteger(uploadResults[0]?.index) ? uploadResults[0].index : 0);
            if (window.pathChatAgent) {
                window.pathChatAgent.currentViewingWsiIndex = focusIndex;
                if (window.pathChatAgent.activeAnalysisStrategy === 'single_agent_roam') {
                    window.pathChatAgent._restorePatientRoamMapsFromCache?.(focusIndex);
                } else {
                    window.pathChatAgent._restorePatientSwarmMapsFromCache?.(focusIndex);
                }
            }
            if (typeof this.selectPlWsiItem === 'function') {
                this.selectPlWsiItem(focusIndex);
            }
            this.restoreWsiOverlays?.(focusIndex);
            uploadResults.forEach((item) => {
                this.updatePlWsiStatus?.(item.index, 'completed', 'check');
                this._rememberPatientSlideNavState?.(item.index, 'completed');
            });
            this._setPatientLevelRunState?.(false);
            if (window.pathChatAgent) window.pathChatAgent._activePatientRunToken = null;
            this.syncPatientSlideReportNav?.();
            
            const isEn = (window.__lang || 'zh') === 'en';
            this.showNotification('success', isEn
                ? `Loaded ${result.total_slides} slides from ${patientDir}`
                : `已从 ${patientDir} 加载 ${result.total_slides} 张切片的分析结果`);
            
            // ⭐ 记录已从缓存加载的 file_id，供 startPatientLevelAnalysis 跳过缓存弹窗
            const loadedFileIds = result.slides.map(s => s.file_id).filter(Boolean);
            this.plLoadedFromCacheFileIds = loadedFileIds;
            
            const totalSlides = uploadResults.length;
            const remaining = totalSlides - loadedFileIds.length;
            const startBtn = document.getElementById('plStartAnalysis');
            if (startBtn && remaining > 0) {
                startBtn.disabled = false;
                startBtn.classList.remove('running');
                startBtn.innerHTML = `<i class="fas fa-play"></i> ${isEn ? `Continue (${remaining} remaining)` : `继续分析 (剩余 ${remaining} 张)`}`;
            }
            console.log(`[Patient Level] ✅ 已加载 ${loadedFileIds.length} 张缓存，剩余 ${remaining} 张待分析`);
            
        } catch (error) {
            if (this._isAbortError(error)) return;
            console.error('[Patient Level] 加载患者缓存失败:', error);
            this.showNotification('error', (window.__lang||'zh')==='en' ? `Load failed: ${error.message}` : `加载失败：${error.message}`);
        } finally {
            this.hideLoading();
        }
    }
    
    /**
     * Patient Level 模式下加载所有 WSI 的分析缓存（旧逻辑，逐个 file_id 加载）
     * 复用 pathchat-agent.js 中 handlePatientCacheCheck 的加载逻辑
     */
    async loadPatientLevelAnalysisState() {
        const uploadResults = this.patientLevelUploadResults || [];
        if (uploadResults.length === 0) {
            this.showNotification('warning', __t('pl_no_slides_info'));
            return;
        }
        
        this.showLoading(__t('processing'));
        
        try {
            // ⭐ 生成 patient_id（与 startPatientLevelAnalysis 一致: patient_{folderName}_num{X}）
            const rawFolder = this.patientLevelFolderName || 'unknown';
            const safeFolder = rawFolder.replace(/[^\w\u4e00-\u9fff\-]/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '').substring(0, 30) || 'unknown';
            let plRunNum = 1;
            try {
                const cntResp = await fetch(`/api/count-output-dirs?prefix=patient_${encodeURIComponent(safeFolder)}`);
                const cntResult = await cntResp.json();
                plRunNum = (cntResult.max_num || cntResult.count || 0) + 1;
            } catch (e) {
                console.warn('[Patient Level] count-output-dirs failed, using num1:', e);
            }
            const patientId = `patient_${safeFolder}_num${plRunNum}`;
            this.currentPatientId = patientId;
            
            const cachedSlides = uploadResults.map(r => ({
                file_id: r.file_id,
                index: r.index,
                filename: r.filename,
                part: r.part
            }));
            
            if (window.pathChatAgent?.loadCachedPatientSlides) {
                await window.pathChatAgent.loadCachedPatientSlides(cachedSlides, cachedSlides.length);
            } else {
                this.showNotification('error', (window.__lang||'zh')==='en' ? 'Agent not initialized' : 'Agent 尚未初始化');
                return;
            }
            
            // ⭐ 调用后端聚合 API 生成 patient_analysis_tree.json（含报告查找）
            const currentMode = (window.pathChatAgent?.isCollaborativeMode) ? 'collaborative' : 'ai';
            const fileInfos = uploadResults.map(r => ({
                file_id: r.file_id,
                filename: r.filename,
                part: r.part
            }));
            
            try {
                const aggResp = await fetch('/api/agent/aggregate-patient-tree', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        patient_id: patientId,
                        session_id: window.pathChatAgent?.sessionId || '',
                        folder_name: this.patientLevelFolderName || '',
                        file_infos: fileInfos,
                        mode: currentMode
                    })
                });
                const aggResult = await aggResp.json();
                if (aggResult.success) {
                    this.patientTreeUrl = aggResult.tree_url;
                    this._treeLoadedForId = null;
                    console.log(`[Patient Level] ✅ 聚合树已生成: ${aggResult.tree_url}, WSI数: ${aggResult.wsi_count}, 有报告: ${aggResult.has_report}`);
                }
            } catch (aggErr) {
                console.warn('[Patient Level] ⚠️ 聚合树生成失败，树功能可能受限:', aggErr);
            }
            
            // ⭐ 恢复聊天记录（合并所有 WSI 的聊天历史 + 查找报告链接）
            await this._restorePatientLevelChatHistory(uploadResults);
            
        } catch (error) {
            if (this._isAbortError(error)) return;
            console.error('Patient Level 加载分析状态失败:', error);
            this.showNotification('error', (window.__lang||'zh')==='en' ? `Load failed: ${error.message}` : `加载失败：${error.message}`);
        } finally {
            this.hideLoading();
        }
    }
    
    /**
     * 恢复 Patient Level 模式下各 WSI 的聊天记录到右侧聊天面板
     * @param {Array} slideInfos - [{file_id, filename, ...}]
     */
    async _restorePatientLevelChatHistory(slideInfos, patientDir) {
        const chatContainer = this.elements?.chatMessages;
        if (!chatContainer) return;
        
        const collected = window.pathChatAgent?._collectedChatHistories;
        
        chatContainer.innerHTML = '';
        this.chatHistory = [];
        
        // ⭐ 渲染聊天消息
        if (collected && collected.length > 0) {
            const allMessages = [];
            collected.forEach(({ slideIndex, slideName, messages }) => {
                allMessages.push({ role: 'system-divider', content: slideName, slideIndex });
                messages.forEach(msg => allMessages.push(msg));
            });
            
            this.chatHistory = allMessages.filter(m => m.role !== 'system-divider');
            
            allMessages.forEach(msg => {
                if (msg.role === 'system-divider') {
                    const divider = document.createElement('div');
                    divider.className = 'message system-message';
                    divider.style.cssText = 'text-align:center; padding:8px; margin:12px 0; border-top:1px solid #e0e0e0; color:#888; font-size:12px;';
                    divider.innerHTML = `<span style="background:#f5f5f5; padding:2px 12px; border-radius:10px;">📋 ${msg.content}</span>`;
                    chatContainer.appendChild(divider);
                    return;
                }
                const messageElement = document.createElement('div');
                messageElement.className = `message ${msg.role}-message`;
                if (msg.roiId) messageElement.setAttribute('data-roi-id', msg.roiId);
                const avatar = msg.role === 'assistant' ? '<i class="fas fa-robot"></i>' : '<i class="fas fa-user"></i>';
                messageElement.innerHTML = `
                    <div class="message-avatar">${avatar}</div>
                    <div class="message-content">
                        <div class="message-text">${this.formatMessageContent(msg.content)}</div>
                        <div class="message-time">${msg.timestamp ? new Date(msg.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : ''}</div>
                    </div>
                `;
                chatContainer.appendChild(messageElement);
            });
            
            console.log(`[Patient Level] ✅ 已恢复 ${collected.length} 张切片的聊天记录，共 ${this.chatHistory.length} 条消息`);
        }

        this._ensureRestoredAnalysisInChat();
        await this._appendPatientLevelReportsToChat(slideInfos, patientDir);
    }

    _patientReportLookupMode() {
        if (window.pathChatAgent?.isCollaborativeMode) return 'collaborative';
        if (window.pathChatAgent?.activeAnalysisStrategy === 'single_agent_roam') {
            return 'single_agent_roam';
        }
        return 'ai';
    }

    _patientReportModeSubdir(mode) {
        if (mode === 'collaborative') return 'collab';
        if (mode === 'single_agent_roam') return 'single_agent_roam';
        return 'auto';
    }

    /**
     * 在已有聊天后面追加患者级综合报告 / Word / 重新汇总，不清空视野记录。
     */
    async _appendPatientLevelReportsToChat(slideInfos, patientDir, options = {}) {
        const chatContainer = this.elements?.chatMessages;
        if (!chatContainer) return;

        const includeSlideReports = options.includeSlideReports !== false;
        const alreadyHasReport = Boolean(chatContainer.querySelector('.patient-report-msg'));
        const alreadyHasRegen = Boolean(chatContainer.querySelector('.regen-report-msg'));
        if (alreadyHasReport && alreadyHasRegen && !includeSlideReports) return;

        const isEn = (window.__lang || 'zh') === 'en';
        const mode = this._patientReportLookupMode();
        const modeSubdir = this._patientReportModeSubdir(mode);
        const effectivePid = patientDir || this.currentPatientId;
        const pid = effectivePid
            ? (String(effectivePid).startsWith('patient_') ? effectivePid : `patient_${effectivePid}`)
            : '';

        let reportMap = {};
        if (includeSlideReports && slideInfos && slideInfos.length > 0) {
            const fileIds = slideInfos.map(s => s.file_id).filter(Boolean);
            if (fileIds.length > 0) {
                try {
                    const payload = { file_ids: fileIds, mode };
                    if (patientDir) {
                        payload.patient_dir = patientDir;
                    }
                    const resp = await fetch('/api/agent/find-reports', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    const result = await resp.json();
                    if (result.success) reportMap = result.reports || {};
                } catch (e) {
                    console.warn('[Patient Level] 查找报告失败:', e);
                }
            }
        }

        let patientReportUrl = null;
        let wordReportUrl = null;
        if (pid) {
            const searchOrder = [modeSubdir, 'single_agent_roam', 'auto', 'collab']
                .filter((item, index, arr) => arr.indexOf(item) === index);
            for (const ms of searchOrder) {
                const candidateUrl = `/outputs/${pid}/${ms}/patient_report.html`;
                try {
                    const headResp = await fetch(candidateUrl, { method: 'HEAD' });
                    if (headResp.ok) {
                        patientReportUrl = candidateUrl;
                        break;
                    }
                } catch (e) { /* ignore */ }
            }

            if (!patientReportUrl) {
                try {
                    const payload = {
                        folder_name: this.patientLevelFolderName || '',
                        mode,
                        patient_dir: pid,
                    };
                    const resp = await fetch('/api/agent/find-patient-report', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    const result = await resp.json();
                    if (result.success && result.report_url) patientReportUrl = result.report_url;
                } catch (e) { /* ignore */ }
            }
        }

        let summaryText = '';
        try {
            const sumPayload = {
                patient_id: pid || effectivePid || '',
                mode
            };
            if (!patientDir) {
                sumPayload.folder_name = this.patientLevelFolderName || '';
            }
            const sumResp = await fetch('/api/agent/patient-summary', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(sumPayload)
            });
            const sumResult = await sumResp.json();
            if (sumResult.success && sumResult.data) {
                summaryText = sumResult.data.summary || '';
                if (!patientReportUrl && sumResult.data.html_report_url) {
                    patientReportUrl = sumResult.data.html_report_url;
                }
                if (sumResult.data.report_url) {
                    wordReportUrl = sumResult.data.report_url;
                }
                if (summaryText) {
                    console.log(`[Patient Level] ✅ 从 patient_summary.json 恢复汇总文本 (${summaryText.length} 字符)`);
                }
            }
        } catch (e) {
            console.warn('[Patient Level] patient_summary.json 加载失败:', e);
        }

        if (!alreadyHasReport && (summaryText || patientReportUrl || wordReportUrl)) {
            const patientBtnLabel = isEn ? 'View Patient Report' : '查看患者综合报告';
            let buttonsHtml = '';
            if (patientReportUrl) {
                buttonsHtml += `
                    <a href="${patientReportUrl}" target="_blank"
                       style="display:inline-flex; align-items:center; gap:4px; padding:8px 18px;
                              background:linear-gradient(135deg,#667eea 0%,#764ba2 100%); color:white;
                              border-radius:8px; text-decoration:none; font-size:14px; font-weight:600;
                              box-shadow:0 2px 8px rgba(102,126,234,0.3);"
                       onmouseover="this.style.opacity='0.85'" onmouseout="this.style.opacity='1'">
                        <i class="fas fa-file-medical-alt"></i> ${patientBtnLabel}
                    </a>`;
            }
            if (wordReportUrl) {
                const wordLabel = isEn ? 'Download Word Report' : '下载 Word 报告';
                buttonsHtml += `
                    <a href="${wordReportUrl}" target="_blank" class="report-btn word-btn"
                       style="display:inline-flex; align-items:center; gap:6px; padding:8px 16px;
                              background:linear-gradient(135deg,#2196F3 0%,#1976D2 100%); color:white;
                              border-radius:6px; text-decoration:none; font-size:14px; font-weight:500;"
                       onmouseover="this.style.opacity='0.85'" onmouseout="this.style.opacity='1'">
                        📄 ${wordLabel}
                    </a>`;
            }
            const summaryHtml = summaryText ? `<div class="report-summary">${this.formatMessageContent(summaryText)}</div>` : '';
            const patientEl = document.createElement('div');
            patientEl.className = 'message assistant-message';
            patientEl.innerHTML = `
                <div class="message-avatar"><i class="fas fa-robot"></i></div>
                <div class="message-content">
                    <div class="message-text">
                        <div class="patient-report-msg">
                            <h4>🏥 ${isEn ? 'Patient Level Comprehensive Report' : '患者级别综合诊断报告'}</h4>
                            ${summaryHtml}
                            <div class="patient-report-buttons" style="margin-top:12px; display:flex; gap:10px; flex-wrap:wrap;">
                                ${buttonsHtml}
                            </div>
                        </div>
                    </div>
                </div>
            `;
            chatContainer.appendChild(patientEl);
            console.log(`[Patient Level] ✅ 已恢复患者综合报告${summaryText ? '（含汇总文本）' : '（仅链接）'}`);
        }

        const reportCount = Object.keys(reportMap).length;
        if (includeSlideReports && reportCount > 0) {
            const btnLabel = isEn ? 'View Full Report' : '查看完整报告';
            (slideInfos || []).forEach(info => {
                const reportData = reportMap[info.file_id];
                if (!reportData) return;
                const fid = info.file_id;
                const name = info.filename || info.part || fid;
                const url = typeof reportData === 'string' ? reportData : reportData?.url;
                const txt = typeof reportData === 'string' ? '' : (reportData?.txt || '');

                const upload = (this.patientLevelUploadResults || []).find(
                    (item) => String(item.file_id) === String(fid)
                );
                const slideIndex = Number.isInteger(upload?.index) ? upload.index : null;
                const heading = window.pathChatAgent?._patientSlideResultHeading?.(
                    slideIndex, info.part, name
                ) || name;
                const htmlBtn = window.pathChatAgent?._patientSlideHtmlButton?.(url, btnLabel)
                    || (url
                        ? `<a href="${url}" target="_blank" class="btn patient-slide-html-report">${btnLabel}</a>`
                        : '');
                const reportEl = document.createElement('div');
                reportEl.className = 'message assistant-message';
                reportEl.innerHTML = `
                    <div class="message-avatar"><i class="fas fa-robot"></i></div>
                    <div class="message-content">
                        <div class="message-text">
                            <div class="patient-slide-result" data-wsi-index="${slideIndex ?? ''}">
                                <p class="patient-slide-result-lead">${heading}</p>
                                ${txt ? `<div class="report-summary" data-wsi-index="${slideIndex ?? ''}" data-patient-slide-report="draft" style="white-space:pre-wrap; font-size:13px; line-height:1.6; max-height:400px; overflow-y:auto; padding:10px; background:#f8f9fa; border-radius:8px; border:1px solid #e9ecef;">${this.formatMessageContent(txt)}</div>` : ''}
                                <div class="wsi-analysis-msg success patient-slide-html-row" data-wsi-index="${slideIndex ?? ''}">
                                    <span class="wsi-icon">✅</span>
                                    <span><strong>${name}</strong></span>
                                    ${htmlBtn}
                                </div>
                            </div>
                        </div>
                    </div>
                `;
                chatContainer.appendChild(reportEl);
            });
            console.log(`[Patient Level] ✅ 已添加 ${reportCount} 个报告（含文本内容）`);
        }

        if (!alreadyHasRegen && this.currentPatientId) {
            const dlPid = this.currentPatientId.startsWith('patient_')
                ? this.currentPatientId
                : `patient_${this.currentPatientId}`;
            if (window.pathChatAgent && typeof window.pathChatAgent._buildRegenReportBtn === 'function') {
                const regenEl = document.createElement('div');
                regenEl.className = 'message assistant-message';
                const regenHtml = window.pathChatAgent._buildRegenReportBtn(dlPid, mode);
                regenEl.innerHTML = `
                    <div class="message-avatar"><i class="fas fa-robot"></i></div>
                    <div class="message-content">
                        <div class="message-text">${regenHtml}</div>
                    </div>
                `;
                chatContainer.appendChild(regenEl);
            }
        }

        this.scrollToBottom();
    }
    
    /**
     * 初始化OpenSeadragon查看器
     */
    initViewer(dziUrl, fileInfo) {
        // 清理现有查看器
        if (this.viewer) {
            this.viewer.destroy();
            this.viewer = null;
        }
        
        // 创建新的查看器
        this.viewer = OpenSeadragon({
            id: 'openseadragon-viewer',
            prefixUrl: '/static/vendor/openseadragon/images/',
            tileSources: dziUrl,
            
            // 查看器配置
            showNavigator: true,
            navigatorPosition: 'BOTTOM_RIGHT',
            navigatorSizeRatio: 0.15,
            
            // 隐藏默认控件
            showNavigationControl: false,
            showZoomControl: false,
            showHomeControl: false,
            showFullPageControl: false,
            showRotationControl: false,
            
            // 手势设置
            gestureSettingsMouse: {
                clickToZoom: false,
                dblClickToZoom: true,
                pinchToZoom: true,
                flickEnabled: true,
                flickMomentum: 0.8
            },
            
            // 性能优化
            immediateRender: true,  // 立即渲染，减少模糊时间
            blendTime: 0.3,  // 增加混合时间，让过渡更平滑
            alwaysBlend: false,
            
            // 切片加载优化
            minPixelRatio: 0.8,  // 降低最小像素比，更快显示清晰图像
            timeout: 120000,  // 增加超时时间到120秒
            
            // 缩放设置
            visibilityRatio: 0.5,
            minZoomLevel: 0.1,
            maxZoomLevel: 40,
            zoomPerClick: 1.4,
            zoomPerScroll: 1.25,
            minScrollDeltaTime: 0,
            
            // 平移设置
            panHorizontal: true,
            panVertical: true,
            constrainDuringPan: false,
            wrapHorizontal: false,
            wrapVertical: false
        });
        
        this.setupViewerEvents();
        
        // ⭐ 通知 pathchat-agent.js viewer 已更新
        if (window.pathChatAgent && typeof window.pathChatAgent.setViewer === 'function') {
            window.pathChatAgent.setViewer(this.viewer);
            console.log('✅ 已通知 pathChatAgent viewer 更新');
        }
    }
    
    /**
     * 设置查看器事件
     */
    setupViewerEvents() {
        if (!this.viewer) return;

        // Chromium reports different wheel-event frequencies in Electron and
        // a normal browser. Normalize the physical delta, coalesce one frame,
        // and retain the same 1.25 zoom factor in both hosts.
        this.viewer.addHandler('canvas-scroll', (event) => {
            event.preventDefaultAction = true;
            const canvasHeight = this.viewer?.canvas?.clientHeight || 800;
            const steps = normalizeViewerWheelSteps(
                event.originalEvent,
                event.scroll,
                canvasHeight
            );
            if (!Number.isFinite(steps) || Math.abs(steps) < 0.001) return;

            this._wheelZoomAccumulator = Math.max(
                -2,
                Math.min(2, (this._wheelZoomAccumulator || 0) + steps)
            );
            if (event.position && this.viewer?.viewport) {
                this._wheelZoomReference = this.viewer.viewport.pointFromPixel(event.position, true);
            }
            if (this._wheelZoomFrame) return;

            this._wheelZoomFrame = window.requestAnimationFrame(() => {
                this._wheelZoomFrame = null;
                const activeViewer = this.viewer;
                const normalizedSteps = this._wheelZoomAccumulator || 0;
                const reference = this._wheelZoomReference || null;
                this._wheelZoomAccumulator = 0;
                this._wheelZoomReference = null;
                if (!activeViewer?.viewport || Math.abs(normalizedSteps) < 0.001) return;
                activeViewer.viewport.zoomBy(Math.pow(1.25, normalizedSteps), reference);
                activeViewer.viewport.applyConstraints();
            });
        });
        
        // 缩放事件
        this.viewer.addHandler('zoom', (event) => {
            const zoom = Math.round(event.zoom * 100);
            if (this.elements.zoomLevel) {
                this.elements.zoomLevel.textContent = zoom + '%';
            }
        });
        
        // 平移事件
        this.viewer.addHandler('pan', (event) => {
            // 可以在这里添加平移相关的逻辑
        });
        
        // 左键：浏览/点选已有框；若处于点击放置模式则落下新 ROI。
        this.viewer.addHandler('canvas-click', (event) => {
            console.log('🖱️ Canvas被点击了');
            if (!event.quick) return;
            if (this.screenshotMode) return;
            const original = event.originalEvent;
            if (original?.target?.closest?.('.viewer-quick-menu')) return;
            const viewportPoint = this.viewer.viewport.pointFromPixel(event.position);
            const imagePoint = this.viewer.viewport.viewportToImageCoordinates(viewportPoint);
            console.log('📍 点击位置（图像坐标）:', imagePoint.x, imagePoint.y);
            if (this.clickPlaceRoiMode) {
                event.preventDefaultAction = true;
                this.placeUserRoiAt(imagePoint.x, imagePoint.y);
                return;
            }
            this.checkROIClick(imagePoint.x, imagePoint.y);
        });

        this.viewer.addHandler('canvas-contextmenu', (event) => {
            event.preventDefault = true;
            event.originalEvent?.preventDefault?.();
            this.openViewerQuickMenuFromPointer(event);
        });
        
        // 设置选择模式切换
        this.setupSelectionMode();
        
        // 查看器准备就绪
        this.viewer.addHandler('open', () => {
            console.log('🔬 WSI查看器已就绪');
            const openedFileId = String(this.currentFile?.file_id || '');
            window.pathChatAgent?.notifyViewerOpenedForFile?.(openedFileId);
            
            // ⭐ Patient Level模式：viewer准备好后恢复对应WSI的overlay
            if (window.pathChatAgent && window.pathChatAgent.isPatientLevelMode) {
                // ⭐ 使用 currentViewingWsiIndex（用户正在查看的 WSI），而非 currentAnalyzingWsiIndex
                const wsiIndex = window.pathChatAgent.currentViewingWsiIndex ?? window.pathChatAgent.currentAnalyzingWsiIndex;
                if (wsiIndex !== undefined) {
                    // 延迟一点以确保viewer完全就绪
                    setTimeout(() => {
                        this.restoreWsiOverlays(wsiIndex);
                    }, 100);
                }
            }
        });
        
        // 全屏事件
        this.viewer.addHandler('full-screen', (event) => {
            const icon = this.elements.fullscreenBtn.querySelector('i');
            if (event.fullScreen) {
                icon.className = 'fas fa-compress';
            } else {
                icon.className = 'fas fa-expand';
            }
        });
        
        // 集成Agent功能
        const waitForAgent = () => {
            if (window.pathChatAgent && typeof window.pathChatAgent.setViewer === 'function') {
                try {
                    window.pathChatAgent.setViewer(this.viewer);
                    if (this.currentFile) {
                        window.pathChatAgent.setCurrentFile(this.currentFile.file_id);
                    }
                    console.log('Agent集成成功');
                } catch (error) {
                    console.error('Agent集成失败:', error);
                }
            } else {
                // 如果Agent还没有初始化，等待200ms再试
                setTimeout(waitForAgent, 200);
            }
        };
        
        // 延迟一点时间确保Agent完全初始化
        setTimeout(waitForAgent, 100);
    }
    


    /**
     * 设置选择模式切换
     */
    setupSelectionMode() {
        this.isSelectionMode = false;
        
        // 添加键盘事件监听
        document.addEventListener('keydown', (e) => {
            // 按下S键进入/退出选择模式
            if ((e.key === 's' || e.key === 'S') && !e.ctrlKey && !e.metaKey && !e.altKey) {
                // 检查是否在输入框中
                if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
                    return;
                }
                
                e.preventDefault();
                this.toggleSelectionMode();
            }
            
            // 按ESC键退出选择 / 点击放置 / 截图模式
            if (e.key === 'Escape') {
                this.exitSelectionMode();
                if (this.clickPlaceRoiMode) this.toggleClickPlaceRoiMode(false);
                if (this.screenshotMode) this.toggleScreenshotMode();
            }
        });
        
        // 添加模式提示（已禁用）
        // this.addModeHint();
    }
    
    /**
     * 切换选择模式
     */
    toggleSelectionMode() {
        if (this.isSelectionMode) {
            this.exitSelectionMode();
        } else {
            this.enterSelectionMode();
        }
    }
    
    /**
     * 进入选择模式
     */
    enterSelectionMode() {
        console.log('🎯 进入选择模式...');
        this.isSelectionMode = true;
        
        // 更新提示
        this.updateModeHint('选择模式 - 拖拽选择ROI区域 (ESC退出)');
        
        // 每次进入选择模式都重新设置ROI功能，确保事件正确绑定
        this.setupROISelection();
        
        // 改变鼠标样式
        const viewerElement = document.getElementById('openseadragon-viewer');
        if (viewerElement) {
            viewerElement.style.cursor = 'crosshair';
        }
        
        console.log('✅ 选择模式已激活', {
            isSelectionMode: this.isSelectionMode,
            cursor: viewerElement?.style.cursor
        });
    }
    
    /**
     * 退出选择模式
     */
    exitSelectionMode() {
        this.isSelectionMode = false;
        
        // 更新提示
        this.updateModeHint('浏览模式 - 按 S 键进入选择模式');
        
        // 清除当前选择
        this.clearROI();
        
        // 恢复鼠标样式
        const viewerElement = document.getElementById('openseadragon-viewer');
        if (viewerElement) {
            viewerElement.style.cursor = '';
        }
        
        console.log('👁️ 退出选择模式');
    }
    
    /**
     * 添加模式提示
     */
    addModeHint() {
        const hintElement = document.createElement('div');
        hintElement.id = 'mode-hint';
        hintElement.className = 'mode-hint';
        hintElement.textContent = __t('browse_mode_hint');
        
        const viewerContainer = document.getElementById('openseadragon-viewer');
        if (viewerContainer) {
            viewerContainer.appendChild(hintElement);
        }
    }
    
    /**
     * 更新模式提示
     */
    updateModeHint(text) {
        const hintElement = document.getElementById('mode-hint');
        if (hintElement) {
            hintElement.textContent = text;
            hintElement.className = this.isSelectionMode ? 'mode-hint selection-mode' : 'mode-hint';
        }
    }

    /**
     * 设置ROI选择功能 - 稳定的矩形模式
     */
    setupROISelection() {
        console.log('🔧 设置ROI选择功能...');
        
        // 先清理之前的事件监听器（如果存在）
        if (this.roiCleanup) {
            console.log('🧹 清理之前的ROI事件监听器');
            this.roiCleanup();
        }
        
        // 统一的ROI状态管理
        this.isDrawing = false;
        this.roiStartPoint = null;
        this.currentROIRect = null;
        
        // 禁用OpenSeadragon默认的点击事件，防止画点
        this.viewer.gestureSettingsMouse.clickToZoom = false;
        this.viewer.gestureSettingsMouse.dblClickToZoom = false;
        this.viewer.gestureSettingsMouse.clickDistThreshold = -1; // 禁用点击
        
        // 鼠标按下事件 - 开始绘制矩形
        this.roiPressHandler = (event) => {
            console.log('🖱️ canvas-press事件触发', {
                isSelectionMode: this.isSelectionMode,
                button: event.originalEvent?.button,
                position: event.position
            });
            
            if (!this.isSelectionMode) {
                console.log('❌ 不在选择模式，忽略事件');
                return;
            }
            
            // 只响应左键
            if (event.originalEvent && event.originalEvent.button !== 0) {
                console.log('❌ 非左键点击，忽略事件');
                return;
            }
            
            // 开始绘制ROI矩形 - 先设置状态，再清理
            this.roiStartPoint = this.viewer.viewport.pointFromPixel(event.position);
            this.isDrawing = true;
            this.currentROIRect = null;
            
            // 清除之前的ROI显示（但保持当前绘制状态）
            this.clearTemporaryROI();
            if (this.finalROIOverlay) {
                this.viewer.removeOverlay(this.finalROIOverlay);
                this.finalROIOverlay = null;
            }
            this.clearROIControlPanel();
            this.clearROIConfirmationTip();
            
            // 阻止默认行为
            event.preventDefaultAction = true;
            
            console.log('🎯 开始ROI矩形绘制', {
                startPoint: this.roiStartPoint,
                isDrawing: this.isDrawing
            });
        };
        
        // 鼠标拖拽事件 - 动态绘制矩形
        this.roiDragHandler = (event) => {
            if (!this.isDrawing || !this.roiStartPoint) {
                console.log('🖱️ canvas-drag: 跳过', {
                    isDrawing: this.isDrawing,
                    hasStartPoint: !!this.roiStartPoint
                });
                return;
            }
            
            const currentPoint = this.viewer.viewport.pointFromPixel(event.position);
            
            // 计算矩形区域
            const rect = {
                x: Math.min(this.roiStartPoint.x, currentPoint.x),
                y: Math.min(this.roiStartPoint.y, currentPoint.y),
                width: Math.abs(currentPoint.x - this.roiStartPoint.x),
                height: Math.abs(currentPoint.y - this.roiStartPoint.y)
            };
            
            this.currentROIRect = rect;
            
            console.log('🖱️ 拖拽更新矩形', rect);
            
            // 动态更新ROI矩形显示
            this.updateROIRectangle(rect);
            
            event.preventDefaultAction = true;
        };
        
        // 鼠标释放事件 - 完成矩形选择
        this.roiReleaseHandler = (event) => {
            if (!this.isDrawing) return;
            
            this.isDrawing = false;
            
            if (this.currentROIRect && 
                this.currentROIRect.width > 0.01 && 
                this.currentROIRect.height > 0.01) {
                
                // 完成ROI选择
                this.finalizeROIRectangle(this.currentROIRect);
                console.log('✅ ROI矩形选择完成');
            } else {
                // 矩形太小，清除
                this.clearROI();
                console.log('❌ ROI矩形太小，已清除');
            }
            
            // 重置状态
            this.roiStartPoint = null;
            this.currentROIRect = null;
        };
        
        // 键盘事件处理
        this.roiKeyHandler = (event) => {
            // 避免在输入框中触发
            const activeElement = document.activeElement;
            if (activeElement && (activeElement.tagName === 'INPUT' || activeElement.tagName === 'TEXTAREA')) {
                return;
            }
            
            if (event.key === 'Escape') {
                if (this.isDrawing) {
                    // 取消正在绘制的ROI
                    this.isDrawing = false;
                    this.roiStartPoint = null;
                    this.currentROIRect = null;
                    this.clearROI();
                    console.log('🚫 ESC取消ROI绘制');
                } else if (this.currentROI) {
                    // 清除已完成的ROI
                    this.clearROI();
                    console.log('🗑️ ESC清除ROI');
                }
            } else if (event.key === 'Enter' && this.currentROI && !this.isDrawing) {
                // Enter键确认ROI
                this.confirmROISelection();
                console.log('✅ Enter键确认ROI');
            }
        };
        
        // 绑定事件处理器
        console.log('🔗 绑定ROI事件处理器...');
        this.viewer.addHandler('canvas-press', this.roiPressHandler);
        this.viewer.addHandler('canvas-drag', this.roiDragHandler);
        this.viewer.addHandler('canvas-release', this.roiReleaseHandler);
        document.addEventListener('keydown', this.roiKeyHandler);
        
        // 存储清理函数
        this.roiCleanup = () => {
            console.log('🧹 清理ROI事件处理器...');
            this.viewer.removeHandler('canvas-press', this.roiPressHandler);
            this.viewer.removeHandler('canvas-drag', this.roiDragHandler);
            this.viewer.removeHandler('canvas-release', this.roiReleaseHandler);
            document.removeEventListener('keydown', this.roiKeyHandler);
            
            // 恢复OpenSeadragon默认行为
            this.viewer.gestureSettingsMouse.clickToZoom = true;
            this.viewer.gestureSettingsMouse.dblClickToZoom = true;
            this.viewer.gestureSettingsMouse.clickDistThreshold = 5;
        };
        
        console.log('✅ ROI选择功能设置完成', {
            isSelectionMode: this.isSelectionMode,
            clickToZoom: this.viewer.gestureSettingsMouse.clickToZoom,
            clickDistThreshold: this.viewer.gestureSettingsMouse.clickDistThreshold
        });
    }
    
    /**
     * 创建选择信息显示
     */
    createSelectionInfo(webPoint) {
        // 创建选择信息浮窗
        this.selectionInfoDiv = document.createElement('div');
        this.selectionInfoDiv.className = 'selection-info-overlay';
        this.selectionInfoDiv.innerHTML = `
            <div class="selection-info-content">
                <div class="selection-header">Region Size:</div>
                <div class="selection-size" id="selectionSize">0px × 0px</div>
                <div class="selection-header">Maximum Size:</div>
                <div class="selection-max-size" id="selectionMaxSize">1000px × 1000px</div>
                <div class="selection-actions">
                    <button class="selection-btn expand-btn" title="展开到最大">
                        <i class="fas fa-expand-arrows-alt"></i>
                    </button>
                    <button class="selection-btn delete-btn" title="删除选择">
                        <i class="fas fa-trash"></i>
                    </button>
                </div>
            </div>
        `;
        
        // 添加到查看器容器
        const viewerContainer = document.getElementById('openseadragon-viewer');
        viewerContainer.appendChild(this.selectionInfoDiv);
        
        // 初始位置
        this.updateSelectionInfoPosition(webPoint);
    }
    
    /**
     * 更新选择信息显示
     */
    updateSelectionInfo(webPoint, rect) {
        if (!this.selectionInfoDiv) return;
        
        // 转换为像素尺寸
        const imageSize = this.viewer.world.getItemAt(0).getContentSize();
        const pixelWidth = Math.round(rect.width * imageSize.x);
        const pixelHeight = Math.round(rect.height * imageSize.y);
        
        // 更新尺寸显示
        const sizeElement = this.selectionInfoDiv.querySelector('#selectionSize');
        if (sizeElement) {
            sizeElement.textContent = `${pixelWidth}px × ${pixelHeight}px`;
        }
        
        // 更新位置
        this.updateSelectionInfoPosition(webPoint);
    }
    
    /**
     * 更新选择信息位置
     */
    updateSelectionInfoPosition(webPoint) {
        if (!this.selectionInfoDiv) return;
        
        const viewerContainer = document.getElementById('openseadragon-viewer');
        const containerRect = viewerContainer.getBoundingClientRect();
        
        // 计算相对于容器的位置
        const x = webPoint.x + 20; // 偏移避免遮挡鼠标
        const y = webPoint.y - 80; // 在鼠标上方显示
        
        this.selectionInfoDiv.style.left = Math.min(x, containerRect.width - 200) + 'px';
        this.selectionInfoDiv.style.top = Math.max(y, 10) + 'px';
    }
    
    /**
     * 隐藏选择信息
     */
    hideSelectionInfo() {
        if (this.selectionInfoDiv) {
            this.selectionInfoDiv.remove();
            this.selectionInfoDiv = null;
        }
    }
    
    /**
     * 计算选择矩形
     */
    calculateSelectionRect(startPoint, endPoint) {
        return {
            x: Math.min(startPoint.x, endPoint.x),
            y: Math.min(startPoint.y, endPoint.y),
            width: Math.abs(endPoint.x - startPoint.x),
            height: Math.abs(endPoint.y - startPoint.y)
        };
    }
    
    /**
     * 更新ROI矩形显示 - 动态绘制过程中
     */
    updateROIRectangle(rect) {
        console.log('🔄 updateROIRectangle被调用', rect);
        
        // 清除之前的临时矩形
        this.clearTemporaryROI();
        
        // 创建临时ROI矩形元素 (PathChat风格)
        // ⭐ Bug修复：去掉背景填充，避免影响视觉选择
        const roiElement = document.createElement('div');
        roiElement.className = 'roi-rectangle-drawing';
        roiElement.style.cssText = `
            border: 2px solid #7C3AED;
            background: transparent;
            border-radius: 6px;
            pointer-events: none;
            box-sizing: border-box;
            box-shadow: 0 4px 12px rgba(124, 58, 237, 0.3);
            opacity: 0.8;
        `;
        
        console.log('🎨 创建ROI元素', roiElement);
        
        // 使用OpenSeadragon.Rect添加覆盖层
        const osRect = new OpenSeadragon.Rect(rect.x, rect.y, rect.width, rect.height);
        console.log('📐 OpenSeadragon.Rect', osRect);
        
        try {
            this.viewer.addOverlay({
                element: roiElement,
                location: osRect,
                placement: OpenSeadragon.Placement.TOP_LEFT
            });
            console.log('✅ 成功添加覆盖层');
        } catch (error) {
            console.error('❌ 添加覆盖层失败', error);
        }
        
        // 存储临时覆盖层引用
        this.temporaryROIOverlay = roiElement;
    }
    
    /**
     * 完成ROI矩形选择
     */
    finalizeROIRectangle(rect) {
        // 清除临时ROI
        this.clearTemporaryROI();
        
        // 创建最终ROI矩形元素 (PathChat风格)
        // ⭐ Bug修复：去掉背景填充，避免影响视觉选择
        const roiElement = document.createElement('div');
        roiElement.className = 'roi-rectangle-final';
        roiElement.style.cssText = `
            border: 2px solid #7C3AED;
            background: transparent;
            border-radius: 6px;
            pointer-events: none;
            box-sizing: border-box;
            box-shadow: 0 4px 12px rgba(124, 58, 237, 0.3);
        `;
        
        // 使用OpenSeadragon.Rect添加覆盖层
        const osRect = new OpenSeadragon.Rect(rect.x, rect.y, rect.width, rect.height);
        this.viewer.addOverlay({
            element: roiElement,
            location: osRect,
            placement: OpenSeadragon.Placement.TOP_LEFT
        });
        
        // 转换为图像坐标
        const imageRect = this.viewer.viewport.viewportToImageRectangle(osRect);
        this.currentROI = {
            x: Math.round(imageRect.x),
            y: Math.round(imageRect.y),
            width: Math.round(imageRect.width),
            height: Math.round(imageRect.height)
        };
        
        // 存储最终覆盖层引用
        this.finalROIOverlay = roiElement;
        
        // 显示ROI控制面板
        this.showROIControlPanel(rect);
        
        // 更新UI状态
        this.updateROIStatus();
        
        console.log('🎯 ROI坐标 (图像像素):', this.currentROI);
    }
    
    /**
     * 创建ROI矩形元素
     * @param {boolean} isTemporary - 是否为临时状态（拖拽中）
     */
    createROIElement(isTemporary = false) {
        const element = document.createElement('div');
        
        if (isTemporary) {
            // 临时状态（拖拽中）
            element.className = 'roi-rectangle-drawing';
            element.id = 'roi-overlay-temp';
        } else {
            // 最终状态（选择完成）
            element.className = 'roi-rectangle-final';
            element.id = 'roi-overlay-final';
        }
        
        // PathChat风格样式
        // ⭐ Bug修复：去掉背景填充，避免影响视觉选择
        element.style.cssText = `
            border: 2px solid #7C3AED;
            background: transparent;
            border-radius: 6px;
            pointer-events: none;
            box-sizing: border-box;
            box-shadow: 0 4px 12px rgba(124, 58, 237, 0.3);
            opacity: ${isTemporary ? '0.8' : '1'};
        `;
        
        return element;
    }
    
    /**
     * 清除临时ROI
     */
    clearTemporaryROI() {
        if (this.temporaryROIOverlay) {
            this.viewer.removeOverlay(this.temporaryROIOverlay);
            this.temporaryROIOverlay = null;
        }
    }
    

    

    
    /**
     * 计算选择矩形
     */
    calculateSelectionRect(startPoint, endPoint) {
        return {
            x: Math.min(startPoint.x, endPoint.x),
            y: Math.min(startPoint.y, endPoint.y),
            width: Math.abs(endPoint.x - startPoint.x),
            height: Math.abs(endPoint.y - startPoint.y)
        };
    }
    
    /**
     * 完成ROI选择
     */

    
    /**
     * 显示ROI控制面板
     */
    showROIControlPanel(rect) {
        // 清除之前的面板
        this.clearROIControlPanel();
        
        // 计算面板位置（ROI右上角）
        const cornerX = rect.x + rect.width;
        const cornerY = rect.y;
        const cornerWebPoint = this.viewer.viewport.pixelFromPoint(new OpenSeadragon.Point(cornerX, cornerY));
        
        // 创建控制面板 (PathChat风格)
        const panel = document.createElement('div');
        panel.className = 'roi-control-panel-pathchat';
        panel.innerHTML = `
            <div class="roi-info-pathchat">
                <div class="roi-title-pathchat">选中区域</div>
                <div class="roi-size-pathchat">${this.currentROI.width}px × ${this.currentROI.height}px</div>
            </div>
            <div class="roi-actions-pathchat">
                <button class="roi-analyze-btn-pathchat" id="roi-add-btn">
                    <span class="btn-icon">✓</span>
                    <span class="btn-text">添加</span>
                </button>
                <button class="roi-cancel-btn-pathchat" id="roi-cancel-btn">
                    <span class="btn-icon">✕</span>
                    <span class="btn-text">取消</span>
                </button>
            </div>
        `;
        
        // Apple Style 样式
        panel.style.cssText = `
            background: rgba(255, 255, 255, 0.95);
            color: #1d1d1f;
            border-radius: 12px;
            padding: 12px 16px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.15);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 0.5px solid rgba(0, 0, 0, 0.1);
            min-width: 180px;
            font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', sans-serif;
        `;
        
        // 设置面板位置（右上角，向右偏移10px，向上偏移10px）
        panel.style.position = 'absolute';
        panel.style.left = `${cornerWebPoint.x + 10}px`;
        panel.style.top = `${cornerWebPoint.y - 10}px`;
        panel.style.transform = 'translate(0, -100%)';
        panel.style.zIndex = '1001';
        
        // 添加到查看器容器
        const viewerContainer = document.getElementById('openseadragon-viewer');
        if (viewerContainer) {
            viewerContainer.appendChild(panel);
            this.roiControlPanel = panel;
            
            // 添加按钮事件监听器
            const addBtn = panel.querySelector('#roi-add-btn');
            const cancelBtn = panel.querySelector('#roi-cancel-btn');
            
            if (addBtn) {
                addBtn.addEventListener('click', () => {
                    console.log('➕ 点击添加按钮');
                    this.addROIToChat();
                });
            }
            
            if (cancelBtn) {
                cancelBtn.addEventListener('click', () => {
                    console.log('✕ 点击取消按钮');
                    this.clearROI();
                });
            }
        }
    }
    
    /**
     * 清除ROI控制面板
     */
    clearROIControlPanel() {
        if (this.roiControlPanel) {
            this.roiControlPanel.remove();
            this.roiControlPanel = null;
        }
    }
    
    /**
     * 确认ROI选择
     */
    addROIToChat() {
        if (!this.currentROI) {
            console.warn('⚠️ 没有当前ROI可添加');
            return;
        }
        
        // 显示确认提示
        this.showROIConfirmationTip();
        
        // 清除控制面板
        this.clearROIControlPanel();
        
        // 输出ROI坐标到控制台（用于调试）
        console.log('✅ ROI已添加到聊天', this.currentROI);
        
        // 在聊天界面显示ROI已添加的消息
        this.addROIMessage();
    }
    
    /**
     * 在聊天界面添加ROI已添加的消息
     */
    addROIMessage() {
        const chatMessages = document.getElementById('chat-messages');
        if (!chatMessages) {
            console.error('❌ 找不到聊天消息容器');
            return;
        }
        
        // 创建系统消息元素
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message system-message';
        
        const now = new Date();
        const timeStr = now.toLocaleTimeString('zh-CN', { 
            hour12: false, 
            hour: '2-digit', 
            minute: '2-digit' 
        });
        
        messageDiv.innerHTML = `
            <div class="message-header">
                <span class="message-sender">📋 系统</span>
                <span class="message-time">${timeStr}</span>
            </div>
            <div class="message-content">
                <div class="roi-added-message">
                    <span class="roi-icon">🎯</span>
                    <span class="roi-text">ROI区域已添加 (${this.currentROI.width}px × ${this.currentROI.height}px)</span>
                </div>
                <div class="roi-hint">您现在可以输入问题来分析这个区域</div>
            </div>
        `;
        
        // 添加到聊天消息容器
        chatMessages.appendChild(messageDiv);
        
        // 滚动到最新消息
        chatMessages.scrollTop = chatMessages.scrollHeight;
        
        console.log('📝 已在聊天界面添加ROI消息');
    }
    
    confirmROISelection() {
        // 保留兼容性，调用新的添加函数
        this.addROIToChat();
    }
    
    /**
     * 显示ROI确认提示 - PathChat风格
     */
    showROIConfirmationTip() {
        // 清除之前的提示
        this.clearROIConfirmationTip();
        
        // 创建提示元素
        const tip = document.createElement('div');
        tip.className = 'roi-confirmation-tip';
        tip.innerHTML = `
            <span class="tip-icon">✓</span>
            <span class="tip-text">ROI 区域已添加</span>
            <button class="tip-close-btn" onclick="app.clearROI()">✕</button>
        `;
        
        // Apple Style 样式
        tip.style.cssText = `
            background: rgba(0, 122, 255, 0.08);
            color: #007AFF;
            border: 0.5px solid rgba(0, 122, 255, 0.2);
            border-radius: 8px;
            padding: 8px 12px;
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 13px;
            font-weight: 500;
            opacity: 0;
            transform: translateY(-10px);
            transition: all 0.3s ease;
        `;
        
        // 添加到聊天面板上方
        const chatPanel = document.querySelector('.chat-panel');
        const chatInput = document.querySelector('.chat-input');
        const chatMessages = document.querySelector('.chat-messages');
        
        // 尝试多种选择器找到正确的聊天容器
        let targetContainer = chatPanel || chatInput?.parentElement || chatMessages?.parentElement;
        
        if (targetContainer) {
            targetContainer.insertBefore(tip, targetContainer.firstChild);
            this.roiConfirmationTip = tip;
            
            // 添加提示按钮事件监听器
            const closeBtn = tip.querySelector('.tip-close-btn');
            if (closeBtn) {
                closeBtn.addEventListener('click', () => {
                    console.log('✕ 点击提示关闭按钮');
                    this.clearROI();
                });
            }
            
            // 2秒淡入动画
            setTimeout(() => {
                tip.style.opacity = '1';
                tip.style.transform = 'translateY(0)';
            }, 100);
            
            console.log('✅ ROI确认提示已显示');
        } else {
            console.warn('⚠️ 未找到聊天容器，尝试添加到body');
            document.body.appendChild(tip);
            this.roiConfirmationTip = tip;
            
            // 如果添加到body，需要调整样式
            tip.style.position = 'fixed';
            tip.style.bottom = '100px';
            tip.style.right = '20px';
            tip.style.zIndex = '9999';
            
            setTimeout(() => {
                tip.style.opacity = '1';
                tip.style.transform = 'translateY(0)';
            }, 100);
        }
    }
    
    /**
     * 清除ROI确认提示
     */
    clearROIConfirmationTip() {
        if (this.roiConfirmationTip) {
            this.roiConfirmationTip.remove();
            this.roiConfirmationTip = null;
        }
    }
    
    /**
     * 分析ROI (保持兼容性)
     */
    analyzeROI() {
        this.confirmROISelection();
    }
    
    /**
     * 发送ROI到API
     */
    async sendROIToAPI(roi) {
        try {
            const response = await fetch('/api/analyze_roi', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    file_id: this.currentFileId,
                    roi: roi,
                    question: '请分析这个区域的组织形态学特征',
                    history: (this.chatHistory || []).slice(0, -1),
                    analysis_context: this.collectAnalysisContext?.() || '',
                    lang: window.__lang || 'zh'
                })
            });
            
            if (response.ok) {
                const result = await response.json();
                console.log('✅ ROI分析成功:', result);
                
                // 显示分析结果
                this.displayAnalysisResult(result);
                
                // 清除ROI选择
                this.clearROI();
            } else {
                console.error('❌ ROI分析失败:', response.statusText);
                this.showMessage('ROI分析失败', 'error');
            }
        } catch (error) {
            console.error('❌ ROI分析错误:', error);
            this.showMessage('ROI分析错误', 'error');
        }
    }
    
    /**
     * 显示分析结果
     */
    displayAnalysisResult(result) {
        // 添加到聊天窗口
        this.addMessage('ai', result.analysis || '分析完成');
        
        // 滚动到最新消息
        const chatMessages = document.getElementById('chat-messages');
        if (chatMessages) {
            chatMessages.scrollTop = chatMessages.scrollHeight;
        }
    }
    
    /**
     * 创建永久的ROI信息显示
     */
    createPermanentROIInfo(rect) {
        // 清除临时信息
        this.hideSelectionInfo();
        
        // 创建永久ROI信息
        const imageSize = this.viewer.world.getItemAt(0).getContentSize();
        const pixelWidth = Math.round(rect.width * imageSize.x);
        const pixelHeight = Math.round(rect.height * imageSize.y);
        
        this.permanentROIInfo = document.createElement('div');
        this.permanentROIInfo.className = 'permanent-roi-info';
        this.permanentROIInfo.innerHTML = `
            <div class="roi-info-content">
                <div class="roi-header">Selected Region</div>
                <div class="roi-size">${pixelWidth}px × ${pixelHeight}px</div>
                <div class="roi-actions">
                    <button class="roi-btn analyze-btn" onclick="pathchat.analyzeCurrentROI()" title="分析区域">
                        <i class="fas fa-search"></i> 分析
                    </button>
                    <button class="roi-btn clear-btn" onclick="pathchat.clearROI()" title="清除选择">
                        <i class="fas fa-times"></i>
                    </button>
                </div>
            </div>
        `;
        
        // 添加到查看器容器
        const viewerContainer = document.getElementById('openseadragon-viewer');
        viewerContainer.appendChild(this.permanentROIInfo);
        
        // 定位到选择区域附近
        const centerPoint = this.viewer.viewport.viewportToViewerElementCoordinates(
            new OpenSeadragon.Point(rect.x + rect.width / 2, rect.y + rect.height / 2)
        );
        
        this.permanentROIInfo.style.left = Math.min(centerPoint.x + 20, viewerContainer.clientWidth - 200) + 'px';
        this.permanentROIInfo.style.top = Math.max(centerPoint.y - 60, 10) + 'px';
    }
    
    /**
     * 分析当前ROI
     */
    analyzeCurrentROI() {
        if (!this.currentROI) {
            this.showNotification('warning', (window.__lang||'zh')==='en' ? 'Please select an ROI region first' : '请先选择ROI区域');
            return;
        }
        
        // 触发分析
        const defaultQuestion = '请分析这个区域的组织形态学特征';
        this.elements.chatInput.value = defaultQuestion;
        this.sendMessage();
    }
    
    /**
     * 清除ROI覆盖层
     */
    clearROIOverlay() {
        if (this.selectionOverlay && this.viewer) {
            this.viewer.removeOverlay(this.selectionOverlay.element);
            this.selectionOverlay = null;
        }
    }
    
    /**
     * 清除ROI选择
     */
    /**
     * 清除ROI选择 - 统一清理
     */
    clearROI() {
        this.currentROI = null;
        
        // 重置统一的ROI状态
        this.isDrawing = false;
        this.roiStartPoint = null;
        this.currentROIRect = null;
        
        // 清除临时ROI
        this.clearTemporaryROI();
        
        // 清除最终ROI覆盖层
        if (this.finalROIOverlay) {
            this.viewer.removeOverlay(this.finalROIOverlay);
            this.finalROIOverlay = null;
        }
        
        // 清除旧的覆盖层（兼容性）
        this.clearROIOverlay();
        
        // 清除控制面板
        this.clearROIControlPanel();
        
        // 清除确认提示
        this.clearROIConfirmationTip();
        
        // 更新UI状态
        this.updateROIStatus();
        this.hideROISelectionInfo();
        
        // 清除所有ROI相关的显示元素
        this.hideSelectionInfo();
        if (this.permanentROIInfo) {
            this.permanentROIInfo.remove();
            this.permanentROIInfo = null;
        }
        
        console.log('🗑️ 已清除ROI矩形');
    }
    
    /**
     * 更新ROI状态显示
     */
    updateROIStatus() {
        if (!this.elements.roiStatus) return;
        
        if (this.currentROI) {
            this.elements.roiStatus.textContent = `${this.currentROI.width}×${this.currentROI.height}`;
        } else {
            this.elements.roiStatus.textContent = __t('status_no_region');
        }
    }
    
    /**
     * 显示ROI选择信息
     */
    showROISelectionInfo() {
        this.elements.roiSelectionInfo.style.display = 'block';
        this.updateSendButton();
    }
    
    /**
     * 隐藏ROI选择信息
     */
    hideROISelectionInfo() {
        this.elements.roiSelectionInfo.style.display = 'none';
        this.updateSendButton();
    }
    
    async consumeStreamingResponse(response, onDelta) {
        if (!response.ok) {
            let message = `HTTP ${response.status}`;
            let noApiKey = false;
            try {
                const body = await response.json();
                message = body.error || message;
                noApiKey = Boolean(body.no_api_key || body.requires_api_key);
            } catch (_) {}
            const failure = new Error(message);
            failure.noApiKey = noApiKey;
            throw failure;
        }
        if (!response.body?.getReader) return response.json();

        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        let finalResult = null;

        const consumeLine = (line) => {
            const value = String(line || '').trim();
            if (!value) return;
            const event = JSON.parse(value);
            if (event.type === 'delta') {
                onDelta?.(String(event.content || ''));
            } else if (event.type === 'error') {
                throw new Error(event.error || '流式响应失败');
            } else if (event.type === 'done') {
                finalResult = event;
            } else if (Object.prototype.hasOwnProperty.call(event, 'success')) {
                // Compatibility with a server process that has not reloaded yet.
                finalResult = event;
            }
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
        return finalResult || { success: true };
    }

    /**
     * 发送消息
     */
    async sendMessage() {
        const message = this.elements.chatInput.value.trim();
        // Freeze the attachment for this turn. The composer should clear as
        // soon as the request is accepted, while the in-flight request keeps
        // using this immutable snapshot.
        const selectedRois = (this.selectedUserRois || []).filter((item) => item?.imageData);
        const capturedScreenshot = selectedRois.length
            ? { imageData: selectedRois[selectedRois.length - 1].imageData, rect: selectedRois[selectedRois.length - 1].rect }
            : this.currentScreenshot;
        const hasScreenshot = !!(capturedScreenshot && capturedScreenshot.imageData);
        const roiOutgoing = selectedRois.length
            ? this._outgoingUserRoiMessage(message, selectedRois)
            : '';
        const chatMessage = message || (selectedRois.length ? this._shortUserRoiLabel(selectedRois) : '');
        const cancelledOldChoice = this._cancelPendingAnalysisChoice('new_message');
        if (this.isAnalyzing && !cancelledOldChoice) {
            return;
        }
        const chatTurnId = ++this._chatTurnSerial;
        this._activeChatTurnId = chatTurnId;
        const isCurrentTurn = () => this._activeChatTurnId === chatTurnId;
        
        // ⭐⭐⭐ 检查是否有引用分析结果（协作模式下的 AI 重新解读）
        // 注意：AgentController 实例是 window.pathChatAgent
        if (window.pathChatAgent && window.pathChatAgent.currentQuote && message) {
            console.log('📝 检测到引用状态，发送引用消息');
            const sent = window.pathChatAgent.sendQuotedMessage(message);
            if (sent) {
                // 清空输入框
                this.elements.chatInput.value = '';
                this.updateSendButton();
                return;  // 由 pathChatAgent 处理，不走普通发送流程
            }
        }
        
        if (!message && !this.currentROI && !hasScreenshot) return;

        let analysisMessage = null;
        try {
            // 添加用户消息到聊天（包括截图预览）
            if (message || hasScreenshot) {
                if (hasScreenshot) {
                    // 带截图的消息
                    this.addMessageWithScreenshot(
                        'user',
                        chatMessage,
                        selectedRois.length ? selectedRois : capturedScreenshot.imageData
                    );
                } else if (message) {
                    this.addMessage('user', message);
                }
            }
            
            // 清空输入框
            this.elements.chatInput.value = '';

            // 用户操作发生后再检查模型配置：先保留用户消息，再以引导卡片
            // 告知如何前往 Settings，避免突然跳转打断当前上下文。
            if (
                window.PathCopilotApiKeyGuard
                && !(await window.PathCopilotApiKeyGuard.ensureConfigured())
            ) return;
            if (!isCurrentTurn()) return;
            if (!(await this.ensureSpecimenSiteDeclared({ required: true }))) return;
            if (!isCurrentTurn()) return;

            // The screenshot now belongs to the user message above. Remove
            // the composer preview immediately instead of leaving a stale
            // attachment visible throughout the model response.
            if (hasScreenshot && this.currentScreenshot === capturedScreenshot) {
                this.clearScreenshot();
            } else if (selectedRois.length) {
                this.clearScreenshot();
            }

            this.isAnalyzing = true;
            this.updateSendButton();
            
            // 显示处理中状态
            analysisMessage = this.addMessage('assistant',
                window.__lang === 'en' ? 'Thinking' : '正在思考', true);
            this.updateStatus('processing');
            
            let response, result;

            // 二次加载后的追问必须先走已有分析，不能被「等待患者级首问」
            // 截进 /api/agent/patient_route（那里会清空 history）。
            // 第一次现场分析仍走下面的选择卡。
            if ((this._analysisRestoredFromCache || this._restoredWorkspaceJson) && !hasScreenshot) {
                const existingAnalysis = this.collectAnalysisContext();
                const analysisReady = Boolean(
                    (existingAnalysis && existingAnalysis.length > 80)
                    || this.hasCompletedSlideAnalysis()
                );
                if (analysisReady) {
                    this.patientAnalysisAwaitingQuestion = null;
                    this.currentROI = null;
                    await this._streamMemoryChat(message, analysisMessage, isCurrentTurn);
                    return;
                }
            }

            // Patient Level starts from the right-hand conversation.  The
            // toolbar button only freezes the current slide set and asks for
            // a concrete question; this turn lets the primary Agent decide
            // whether Single Agent is enough before any Swarm consent appears.
            if (this.patientAnalysisAwaitingQuestion && !hasScreenshot) {
                // Leftover ROI/screenshot from the previous slide run must not
                // steal this turn into /api/chat while a patient question is pending.
                this.currentROI = null;
                await this._routePendingPatientAnalysis(
                    message,
                    analysisMessage,
                    isCurrentTurn
                );
                return;
            }
            
            // ⭐ 统一使用 /api/chat，支持带截图和不带截图
            if (hasScreenshot && this.currentFile) {
                // 有截图：调用统一聊天API，带上截图和分析上下文
                const analysisContext = this.collectAnalysisContext();
                
                response = await fetch('/api/chat', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        message: roiOutgoing || message || '请描述这个区域的特征',
                        image_data: capturedScreenshot.imageData,
                        images: selectedRois.map((item) => ({
                            data: item.imageData,
                            mime: 'image/jpeg',
                            name: item.parentPath ? `${item.id}@${item.parentPath}` : (item.id || 'ROI'),
                        })),
                        history: this.chatHistory || [],
                        file_id: this.currentFile.file_id,
                        analysis_context: [analysisContext, roiOutgoing].filter(Boolean).join('\n\n'),
                        lang: window.__lang || 'zh',
                        stream: true
                    })
                });
                let liveStream = null;
                result = await this.consumeStreamingResponse(response, (delta) => {
                    if (!liveStream) liveStream = this.createLiveMessageStream(analysisMessage);
                    liveStream?.append(delta);
                });
                if (!isCurrentTurn()) return;
                
                if (result.success) {
                    if (liveStream) liveStream.finish(result.reply || '');
                    else this.updateMessageStream(analysisMessage, result.reply);
                    this.showNotification('success', (window.__lang||'zh')==='en' ? 'Response complete' : '回复完成');
                    
                } else {
                    this.updateMessage(analysisMessage, (window.__lang||'zh')==='en' ? `Response failed: ${result.error}` : `回复失败：${result.error}`);
                    this.showNotification('error', (window.__lang||'zh')==='en' ? `Response failed: ${result.error}` : `回复失败：${result.error}`);
                }
            } else if (this.currentROI && this.currentFile) {
                // 有ROI和文件：调用分析API
                response = await fetch('/api/analyze_roi', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        file_id: this.currentFile.file_id,
                        roi: this.currentROI,
                        question: message || '请分析这个病理切片区域',
                        // ⭐ ROI 追问带上会话记忆：最近对话 + 已有分析上下文。
                        // 否则模型只能孤立看图，无法回答「此处对我们的结果有影响吗」。
                        history: (this.chatHistory || []).slice(0, -1),
                        analysis_context: this.collectAnalysisContext(),
                        lang: window.__lang || 'zh'
                    })
                });
                
                result = await response.json();
                
                if (result.success) {
                    this.updateMessageStream(analysisMessage, result.analysis);
                    this.showNotification('success', (window.__lang||'zh')==='en' ? 'Analysis complete' : '分析完成');
                } else {
                    this.updateMessage(analysisMessage, (window.__lang||'zh')==='en' ? `Analysis failed: ${result.error}` : `分析失败：${result.error}`);
                    this.showNotification('error', (window.__lang||'zh')==='en' ? `Analysis failed: ${result.error}` : `分析失败：${result.error}`);
                }
            } else {
                // 没有 ROI/截图时，先由主 Agent 理解语义并决定是否需要查看当前 WSI。
                // 前端不再使用寒暄词或病理关键词正则充当路由器。
                const openFileId = this.currentFile?.file_id || this.currentFileId || '';
                const routeHistory = (this.chatHistory || []).slice(0, -1);

                response = await fetch('/api/agent/route', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        message: message,
                        history: routeHistory,
                        file_id: openFileId,
                        session_id: window.pathChatAgent?.sessionId || '',
                        lang: window.__lang || 'zh',
                        stream: true,
                        analysis_context: this.collectAnalysisContext(),
                        has_completed_analysis: this.hasCompletedSlideAnalysis()
                    })
                });
                // Intent routing only. Do not paint the router's draft answer;
                // chat/search will produce the real reply, and visual analysis
                // has its own transition line after the action is known.
                result = await this.consumeStreamingResponse(response);
                if (!isCurrentTurn()) return;
                 
                if (result.success) {
                    if (result.action === 'analyze_wsi') {
                        const canRunAgent = Boolean(
                            openFileId && window.pathChatAgent &&
                            typeof window.pathChatAgent.runAgent === 'function'
                        );
                        if (canRunAgent) {
                            // The primary Agent has already interpreted the user's intent.
                            // Show its task-specific plan instead of a fixed frontend phrase,
                            // then keep a separate pulsing placeholder for thumbnail inference.
                            const transitionReply = String(result.reply || '').trim() || (
                                (window.__lang || 'zh') === 'en'
                                    ? 'I will inspect the whole-slide thumbnail before deciding the next evidence step.'
                                    : '我会先查看整张切片的缩略图，再决定下一步取证方式。'
                            );
                            this.updateMessageStream(analysisMessage, transitionReply);

                            // The primary Agent decides whether visual evidence
                            // is needed and may recommend a route.  Execution is
                            // never started until the user chooses explicitly.
                            const executionChoice = await this.requestAgentSwarmConsent(
                                result.swarm_reason || result.reason || '',
                                '',
                                result.swarm_recommended === true
                            );
                            if (!isCurrentTurn()) return;
                            if (executionChoice?.cancelled) {
                                return;
                            }
                            executionChoice.swarmConsentId = result.swarm_consent_id || '';
                            executionChoice.swarmConsentGranted = (
                                executionChoice.analysisStrategy === 'agent_swarm'
                            );
                            if (this.currentFile?.file_id !== openFileId) {
                                this.showNotification(
                                    'warning',
                                    (window.__lang || 'zh') === 'en'
                                        ? 'The open slide changed. Please ask the question again.'
                                        : '当前切片已切换，请针对新切片重新提问。'
                                );
                                return;
                            }
                            const thumbnailToolMessage = this.addToolActivity(
                                'Python · OpenSlide',
                                (window.__lang || 'zh') === 'en'
                                    ? 'Reading the WSI thumbnail'
                                    : '读取 WSI 缩略图',
                                'running'
                            );
                            const thumbnailThinkingMessage = this.addMessage(
                                'assistant',
                                (window.__lang || 'zh') === 'en' ? 'Thinking' : '正在思考',
                                true
                            );
                            const started = await window.pathChatAgent.runAgent(message, {
                                ...executionChoice,
                                userSelectedRoute: true,
                                expectedFileId: openFileId,
                                thinkingMessage: thumbnailThinkingMessage,
                                toolMessage: thumbnailToolMessage
                            });
                            if (!isCurrentTurn()) return;
                            if (!started) {
                                this.updateToolActivity(
                                    thumbnailToolMessage,
                                    'Python · OpenSlide',
                                    (window.__lang || 'zh') === 'en'
                                        ? 'Could not read the WSI thumbnail'
                                        : '未能读取 WSI 缩略图',
                                    'error'
                                );
                                this.updateMessage(
                                    thumbnailThinkingMessage,
                                    (window.__lang || 'zh') === 'en'
                                        ? 'The slide analysis could not be started.'
                                        : '暂时无法启动切片分析。'
                                );
                            }
                        } else {
                            this.updateMessageStream(analysisMessage, result.reply);
                            this.showNotification(
                                'warning',
                                (window.__lang || 'zh') === 'en'
                                    ? 'The primary Agent requested slide review, but no WSI is available.'
                                    : '主 Agent 判断需要查看切片，但当前没有可用的 WSI。'
                            );
                        }
                    } else if (result.action === 'chat' && this.currentFile) {
                        const bubble = analysisMessage?.querySelector('.message-text');
                        if (bubble) {
                            bubble.textContent = (window.__lang || 'zh') === 'en' ? 'Thinking' : '正在思考';
                        }
                        await this._streamMemoryChat(message, analysisMessage, isCurrentTurn);
                    } else {
                        this.updateMessageStream(analysisMessage, result.reply);
                    }
                } else {
                    this.updateMessage(analysisMessage, (window.__lang||'zh')==='en' ? `Response failed: ${result.error}` : `回复失败：${result.error}`);
                    this.showNotification('error', (window.__lang||'zh')==='en' ? `Response failed: ${result.error}` : `回复失败：${result.error}`);
                }
            }
            
        } catch (error) {
            console.error('发送消息失败:', error);
            if (error?.noApiKey) {
                window.PathCopilotApiKeyGuard?.markConfigured(false);
                window.PathCopilotApiKeyGuard?.requireNow();
                analysisMessage?.remove?.();
                return;
            }
            if (analysisMessage) {
                this.updateMessage(
                    analysisMessage,
                    (window.__lang || 'zh') === 'en'
                        ? `Request failed: ${error.message}`
                        : `请求失败：${error.message}`
                );
            }
            this.showNotification('error', (window.__lang||'zh')==='en' ? `Send failed: ${error.message}` : `发送失败：${error.message}`);
        } finally {
            if (isCurrentTurn()) {
                this.isAnalyzing = false;
                this.updateSendButton();
                this.updateStatus('ready');
            }
        }
    }
    
    _buildScreenshotMessageHtml(text, imageData) {
        const rawItems = Array.isArray(imageData) ? imageData : (imageData ? [imageData] : []);
        const images = rawItems.map((item) => {
            if (typeof item === 'string') return { data: item, id: '' };
            return {
                data: item?.imageData || item?.data || '',
                id: item?.id || item?.name || '',
            };
        }).filter((item) => item.data);
        let content = '';
        if (images.length) {
            content += `<div class="message-screenshot-row">`;
            images.forEach((item) => {
                const src = String(item.data).startsWith('data:image/')
                    ? item.data
                    : `data:image/jpeg;base64,${item.data}`;
                const label = this.escapeHtml(item.id || '');
                content += `
                    <div class="message-screenshot">
                        ${label ? `<span class="message-screenshot-id">${label}</span>` : ''}
                        <img src="${src}" alt="${label || '截图'}" class="screenshot-thumbnail">
                    </div>
                `;
            });
            content += `</div>`;
        }
        if (text) {
            content += `
                <div class="message-text">${this.escapeHtml(text)}</div>
            `;
        }
        return content;
    }

    /**
     * 添加带截图的消息
     */
    addMessageWithScreenshot(role, text, imageData) {
        const chatMessages = this.elements.chatMessages;
        chatMessages.querySelector('.welcome-message')?.remove();
        const messageEl = document.createElement('div');
        messageEl.className = `message ${role}-message`;
        const content = this._buildScreenshotMessageHtml(text, imageData);
        
        messageEl.innerHTML = `
            <div class="message-avatar">
                <i class="fas ${role === 'user' ? 'fa-user' : 'fa-robot'}"></i>
            </div>
            <div class="message-content">
                ${content}
            </div>
        `;
        
        chatMessages.appendChild(messageEl);
        chatMessages.scrollTop = chatMessages.scrollHeight;
        
        // 添加到聊天历史
        this.chatHistory.push({
            role: role,
            content: text,
            hasImage: !!imageData
        });
        
        return messageEl;
    }
    
    /**
     * HTML转义
     */
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    
    /**
     * True after a visual run has already produced findings for this slide.
     */
    _collectVisibleAnalysisText(maxChars = 40000) {
        const chunks = [];
        const push = (el) => {
            // 隐藏的面板只剩「准备开始分析」之类的占位壳，不能混进分析上下文。
            if (!el || el.getClientRects().length === 0) return;
            const text = this._compactAnalysisText(el.textContent || '', maxChars);
            if (text.length > 40) chunks.push(text);
        };
        push(document.getElementById('analysisRoutePanel'));
        push(document.getElementById('routeStepsList'));
        document.querySelectorAll('.v2-subagent-pane, .v2-subagent-stream, .patient-slide-map').forEach(push);
        this._collectStructuredPatientAnalysis({
            maxSlideChars: 8000,
            maxRoiChars: 1600,
        }).forEach((part) => chunks.push(part));
        return this._compactAnalysisText(chunks.join('\n\n'), maxChars);
    }

    hasCompletedSlideAnalysis() {
        const agent = window.pathChatAgent;
        // 只认真信号：真的加载过这张片子的分析 JSON，或本次会话真的跑完过分析。
        // 不再用「助手消息够长」「面板文字够多」去猜——猜错会把首问逼成 chat。
        if (this._analysisRestoredFromCache || this._restoredWorkspaceJson) return true;
        if (agent?.isPatientLevelMode) {
            if (this._patientAnalysisLooksComplete()) return true;
            if (document.querySelector('.patient-report-msg, .regen-report-msg, .patient-report-review-card')) {
                return true;
            }
            const nav = document.querySelector('.patient-slide-report-nav, #patientSlideReportNav');
            if (nav && /已定稿|Finalized|3\/3/.test(nav.textContent || '')) return true;
        }
        if (agent?.isRunning || agent?.isStarting) return false;
        const panelText = this._collectVisibleAnalysisText(800);
        if (panelText.length >= 80 && /Reason|Plan|Evidence|缩略图解读|总览|红框|10×|已生成切片概览/.test(panelText)) {
            return true;
        }
        const activityItems = Array.from(document.querySelectorAll('.v2-agent-activity-item'));
        return activityItems.some((item) => (
            /complete|完成|report|报告|overview complete|概览完成/i.test(item.textContent || '')
        ));
    }

    async _streamMemoryChat(message, analysisMessage, isCurrentTurn) {
        const analysisContext = this.collectAnalysisContext();
        const history = (this.chatHistory || []).slice(0, -1);
        // 有无已完成分析都走同一条证据 Agent：它自己决定检索文献、看缩略图，还是只根据
        // analysis_context 回答。禁止再用 hasAnalysis 关掉 web_chat，否则「最新文献检索」
        // 会被逼成纯工作区闲聊，口头声称查了文献却从不调用 search_literature。
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message,
                history,
                file_id: this.currentFile?.file_id || '',
                analysis_context: analysisContext,
                lang: window.__lang || 'zh',
                mode: 'web_chat',
                web_search: true,
                stream: true,
                privacy_scope: 'medical_concepts_only'
            })
        });
        const v2 = window.PathCopilotV2;
        if (typeof v2?.consumeHomeWebSearchResponse === 'function') {
            analysisMessage?.classList?.remove('temporary', 'is-streaming');
            analysisMessage?._pathcopilotStream?.cancel?.();
            const pending = {
                row: analysisMessage,
                bubble: analysisMessage?.querySelector('.message-text') || analysisMessage
            };
            if (pending.bubble) {
                pending.bubble.textContent = (window.__lang || 'zh') === 'en' ? 'Thinking' : '正在思考';
            }
            let research = null;
            let answerText = '';
            let currentSources = [];
            const result = await v2.consumeHomeWebSearchResponse(response, (event) => {
                if (!isCurrentTurn?.()) return;
                if (event.type === 'route') {
                    if (event.web_search === true && !research) {
                        research = v2.createHomeWebSearchView?.(pending);
                    } else if (event.web_search === false && research) {
                        analysisMessage?.classList?.remove('is-web-search');
                        pending.bubble?.replaceChildren?.();
                        research = null;
                    }
                } else if (event.type === 'status' && event.phase === 'tool_selection') {
                    return;
                } else if (event.type === 'status' && event.phase === 'inspect_slide') {
                    if (!research && pending.bubble) {
                        pending.bubble.textContent = event.message
                            || ((window.__lang || 'zh') === 'en'
                                ? 'Inspecting the 2000px whole-slide overview'
                                : '正在查看切片概览');
                    } else {
                        v2.updateHomeWebSearchStatus?.(research, event);
                    }
                    return;
                } else if (event.type === 'status' || event.type === 'search') {
                    if (!research) research = v2.createHomeWebSearchView?.(pending);
                    v2.updateHomeWebSearchStatus?.(research, event);
                } else if (event.type === 'sources') {
                    if (!research) research = v2.createHomeWebSearchView?.(pending);
                    currentSources = Array.isArray(event.sources) ? event.sources : [];
                    v2.renderHomeWebSources?.(research, currentSources);
                } else if (event.type === 'delta') {
                    answerText += String(event.content || '');
                    analysisMessage?.classList?.remove('temporary', 'is-streaming');
                    if (research) {
                        research.answer.hidden = false;
                        v2.renderHomeAssistantContent?.(research.answer, answerText, currentSources);
                    } else if (pending.bubble) {
                        pending.bubble.innerHTML = this.formatMessageContent(answerText);
                    }
                    this.scrollToBottom();
                }
            });
            if (!isCurrentTurn?.()) return result;
            if (result.success) {
                const finalReply = result.reply || answerText;
                const finalSources = result.sources || currentSources;
                if (research && result.web_search !== false) {
                    research.activity.classList.remove('is-running');
                    research.activity.classList.add('is-complete');
                    const elapsed = Number(result.elapsed_seconds);
                    const isEn = (window.__lang || 'zh') === 'en';
                    const completeLabel = isEn ? 'Web search complete' : '联网检索完成';
                    const sourceLabel = isEn ? 'sources' : '个来源';
                    research.status.textContent = `${completeLabel} · ${(finalSources || []).length} ${sourceLabel}${Number.isFinite(elapsed) ? ` · ${elapsed}s` : ''}`;
                    research.answer.hidden = false;
                    v2.renderHomeAssistantContent?.(research.answer, finalReply, finalSources);
                    v2.renderHomeWebSources?.(research, finalSources);
                } else if (pending.bubble) {
                    pending.bubble.innerHTML = this.formatMessageContent(finalReply);
                }
                this.chatHistory.push({ role: 'assistant', content: finalReply, timestamp: new Date() });
                this.scrollToBottom();
            } else {
                this.updateMessage(
                    analysisMessage,
                    (window.__lang || 'zh') === 'en'
                        ? `Response failed: ${result.error || 'unknown error'}`
                        : `回复失败：${result.error || '未知错误'}`
                );
            }
            return result;
        }

        let liveStream = null;
        const result = await this.consumeStreamingResponse(response, (delta) => {
            if (!isCurrentTurn?.()) return;
            if (!liveStream) liveStream = this.createLiveMessageStream(analysisMessage);
            liveStream?.append(delta);
        });
        if (!isCurrentTurn?.()) return result;
        if (result.success) {
            if (liveStream) liveStream.finish(result.reply || '');
            else this.updateMessageStream(analysisMessage, result.reply);
        } else {
            this.updateMessage(
                analysisMessage,
                (window.__lang || 'zh') === 'en'
                    ? `Response failed: ${result.error || 'unknown error'}`
                    : `回复失败：${result.error || '未知错误'}`
            );
        }
        return result;
    }

    /**
     * 收集当前WSI的所有分析结果作为聊天上下文
     */
    collectAnalysisContext() {
        const contextParts = [];
        
        try {
            if (this._restoredWorkspaceJson) {
                const jsonText = this._restoredWorkspaceContext
                    || this._workspaceJsonAsContext(this._restoredWorkspaceJson);
                this._restoredWorkspaceContext = jsonText;
                if (jsonText) contextParts.push(jsonText);
                const extracted = this._extractWorkspaceDocumentContext(this._restoredWorkspaceJson);
                if (extracted) contextParts.push(extracted);
            }
            const visible = this._collectVisibleAnalysisText(40000);
            if (visible) contextParts.push(visible);

            // 1. 收集自动模式的树状结构分析结果
            const routeStepsList = document.getElementById('routeStepsList');
            if (routeStepsList) {
                // 红框分析结果
                const redNodes = routeStepsList.querySelectorAll('.route-tree-node');
                redNodes.forEach(node => {
                    const roiId = node.dataset.treeId || node.dataset.roiId || '';
                    const content = node.querySelector('.route-tree-content');
                    if (content && content.textContent.trim()) {
                        contextParts.push(`【红框 ${roiId} (10×)】\n${content.textContent.trim()}`);
                    }
                    
                    // 蓝框分析结果
                    const blueNodes = node.querySelectorAll('.route-tree-blue-node');
                    blueNodes.forEach(blueNode => {
                        const blueId = blueNode.dataset.treeId || blueNode.dataset.roiId || '';
                        const blueContent = blueNode.querySelector('.route-tree-blue-content');
                        if (blueContent && blueContent.textContent.trim()) {
                            contextParts.push(`【蓝框 ${blueId} (40×)】\n${blueContent.textContent.trim()}`);
                        }
                        
                        // 绿框分析结果
                        const greenItems = blueNode.querySelectorAll('.route-tree-green-item');
                        greenItems.forEach(greenItem => {
                            const greenId = greenItem.dataset.roiId || '';
                            const greenContent = greenItem.querySelector('.route-tree-green-content');
                            if (greenContent && greenContent.textContent.trim()) {
                                contextParts.push(`【绿框 ${greenId} (160×)】\n${greenContent.textContent.trim()}`);
                            }
                        });
                    });
                });
                
                // 平铺卡片（降级方案）
                const flatCards = routeStepsList.querySelectorAll('.route-step-card');
                flatCards.forEach(card => {
                    const roiId = card.dataset.roiId || '';
                    const level = card.dataset.level || '';
                    const content = card.querySelector('.route-step-content');
                    if (content && content.textContent.trim()) {
                        const levelText = level === 'red' ? '10×' : level === 'blue' ? '40×' : level === 'green' ? '160×' : '';
                        contextParts.push(`【${level === 'red' ? '红框' : level === 'blue' ? '蓝框' : '绿框'} ${roiId} (${levelText})】\n${content.textContent.trim()}`);
                    }
                });
            }
            
            // 2. 收集协作模式的卡片分析结果
            const collabCards = document.querySelectorAll('.collab-task-card');
            collabCards.forEach(card => {
                const roiId = card.dataset.roiId || '';
                const analysisContent = card.querySelector('.analysis-result, .result-content, .card-content');
                if (analysisContent && analysisContent.textContent.trim()) {
                    contextParts.push(`【协作分析 ${roiId}】\n${analysisContent.textContent.trim()}`);
                }
            });
            
            // 3. 收集右侧面板的诊断结果摘要
            const diagnosisSummary = document.querySelector('.diagnosis-summary, .report-summary, #reportContent');
            if (diagnosisSummary && diagnosisSummary.textContent.trim()) {
                contextParts.push(`【诊断摘要】\n${diagnosisSummary.textContent.trim()}`);
            }
            
            // 4. 收集关键发现
            const keyFindings = document.querySelector('.key-findings, #keyFindings');
            if (keyFindings && keyFindings.textContent.trim()) {
                contextParts.push(`【关键发现】\n${keyFindings.textContent.trim()}`);
            }

            // 5. Single-Agent roam timeline in the middle progress rail
            document.querySelectorAll('.v2-agent-activity-item').forEach((item) => {
                const title = item.querySelector('strong')?.textContent?.trim() || '';
                const detail = item.querySelector('p')?.textContent?.trim() || '';
                const text = [title, detail].filter(Boolean).join('\n');
                if (text) contextParts.push(`【分析进度】\n${text.slice(0, 2000)}`);
            });

            // 6. Recent assistant reports already in chat
            (this.chatHistory || [])
                .filter((item) => item?.role === 'assistant' && String(item.content || '').trim().length >= 80)
                .slice(-3)
                .forEach((item, index) => {
                    contextParts.push(
                        `【对话中的先前分析 ${index + 1}】\n${String(item.content).trim().slice(0, 6000)}`
                    );
                });
            
        } catch (error) {
            console.error('收集分析上下文失败:', error);
        }
        
        // Gemini 3 有超大上下文窗口（100万token），不需要截断
        const fullContext = contextParts.join('\n\n');
        console.log(`[Chat] 收集到分析上下文: ${contextParts.length}个区域, ${fullContext.length}字符`);
        return fullContext;
    }
    
    /**
     * Add a truthful, non-conversational tool activity row.
     * Tool rows are intentionally excluded from chat history.
     */
    addToolActivity(toolName, detail, state = 'running') {
        this.elements.chatMessages.querySelector('.welcome-message')?.remove();
        const messageElement = document.createElement('div');
        messageElement.className = `message assistant-message tool-activity-message is-${state}`;
        messageElement.innerHTML = `
            <div class="message-content">
                <div class="tool-activity-line">
                    <span class="tool-activity-icon" aria-hidden="true"><i class="fas fa-terminal"></i></span>
                    <span class="tool-activity-label"></span>
                    <span class="tool-activity-detail"></span>
                </div>
            </div>
        `;
        this.elements.chatMessages.appendChild(messageElement);
        this.updateToolActivity(messageElement, toolName, detail, state);
        this.scrollToBottom();
        return messageElement;
    }

    updateToolActivity(messageElement, toolName, detail, state = 'done') {
        if (!messageElement) return;
        const isEn = (window.__lang || 'zh') === 'en';
        const labels = {
            running: isEn ? 'Calling tool' : '调用工具',
            done: isEn ? 'Tool completed' : '已调用工具',
            stopped: isEn ? 'Tool stopped' : '工具调用已停止',
            error: isEn ? 'Tool failed' : '工具调用失败'
        };
        messageElement.classList.remove('is-running', 'is-done', 'is-stopped', 'is-error');
        messageElement.classList.add(`is-${state}`);
        const icon = messageElement.querySelector('.tool-activity-icon i');
        if (icon) {
            icon.className = state === 'done' ? 'fas fa-check-circle' :
                (state === 'error' ? 'fas fa-exclamation-circle' :
                    (state === 'stopped' ? 'fas fa-stop-circle' : 'fas fa-terminal'));
        }
        const label = messageElement.querySelector('.tool-activity-label');
        const detailElement = messageElement.querySelector('.tool-activity-detail');
        if (label) label.textContent = labels[state] || labels.done;
        if (detailElement) detailElement.textContent = `${toolName}${detail ? ` — ${detail}` : ''}`;
        this.scrollToBottom();
    }

    /**
     * Append a compact, live execution update without polluting model history.
     */
    addAgentStatus(agentId, detail, state = 'done') {
        this.elements.chatMessages.querySelector('.welcome-message')?.remove();
        const messageElement = document.createElement('div');
        messageElement.className = `message assistant-message agent-status-message is-${state}`;
        messageElement.innerHTML = `
            <div class="message-content">
                <div class="agent-status-line">
                    <span class="agent-status-dot" aria-hidden="true"></span>
                    <span class="agent-status-name"></span>
                    <span class="agent-status-detail"></span>
                </div>
            </div>
        `;
        const name = messageElement.querySelector('.agent-status-name');
        const detailElement = messageElement.querySelector('.agent-status-detail');
        if (name) name.textContent = agentId;
        if (detailElement) detailElement.textContent = detail;
        this.elements.chatMessages.appendChild(messageElement);
        this.scrollToBottom();
        return messageElement;
    }

    _cancelPendingAnalysisChoice(reason = 'cancelled') {
        const pending = this._pendingAnalysisChoice;
        if (!pending) return false;
        this._pendingAnalysisChoice = null;
        pending.card.classList.remove('is-pending');
        pending.card.classList.add('is-cancelled');
        pending.card.querySelectorAll('button').forEach((button) => {
            button.disabled = true;
        });
        pending.status.textContent = (window.__lang || 'zh') === 'en'
            ? 'Analysis choice cancelled'
            : '已取消本次分析选择';
        pending.card.setAttribute('aria-label', pending.status.textContent);
        pending.resolve({ cancelled: true, reason });
        queueMicrotask(() => this.elements.chatInput?.focus?.());
        return true;
    }

    _requestAnalysisChoice({ testId, title, detail = '', definitions, flowId = '' }) {
        this._cancelPendingAnalysisChoice('superseded');
        return new Promise((resolve) => {
            this.elements.chatMessages.querySelector('.welcome-message')?.remove();
            document.querySelectorAll('.analysis-consent-card.is-pending').forEach((oldCard) => {
                oldCard.classList.remove('is-pending');
                oldCard.classList.add('is-cancelled');
                oldCard.querySelectorAll('button').forEach((button) => { button.disabled = true; });
            });

            const card = document.createElement('div');
            card.className = 'message assistant-message analysis-consent-card is-pending';
            card.dataset.testid = testId;
            card.dataset.choiceFlowId = flowId;

            const avatar = document.createElement('div');
            avatar.className = 'message-avatar';
            avatar.innerHTML = '<i class="fas fa-robot"></i>';

            const content = document.createElement('div');
            content.className = 'message-content';
            const titleElement = document.createElement('div');
            titleElement.className = 'analysis-consent-title';
            titleElement.textContent = title;
            const detailElement = document.createElement('div');
            detailElement.className = 'analysis-consent-detail';
            detailElement.textContent = detail;
            const choices = document.createElement('div');
            choices.className = 'analysis-consent-actions';
            choices.setAttribute('role', 'group');
            choices.setAttribute('aria-label', title);
            const status = document.createElement('div');
            status.className = 'analysis-consent-status';
            status.setAttribute('aria-live', 'polite');

            let settled = false;
            const finish = (definition, selectedButton) => {
                if (settled) return;
                settled = true;
                if (this._pendingAnalysisChoice?.card === card) {
                    this._pendingAnalysisChoice = null;
                }
                card.classList.remove('is-pending');
                card.classList.add('is-resolved');
                if (definition.value === 'back') card.classList.add('is-navigation');
                card.dataset.selectedLabel = definition.label;
                card.querySelectorAll('button').forEach((button) => {
                    const selected = button === selectedButton;
                    button.disabled = true;
                    button.classList.toggle('is-selected', selected);
                    button.classList.toggle('is-unselected', !selected);
                });
                status.textContent = definition.label;
                status.setAttribute('title', definition.label);
                status.tabIndex = -1;
                card.setAttribute('aria-label', `${title}: ${definition.label}`);
                resolve(definition.value);
                queueMicrotask(() => status.focus?.({ preventScroll: true }));
            };
            definitions.forEach((definition) => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = definition.primary
                    ? 'analysis-consent-btn is-primary'
                    : 'analysis-consent-btn';
                button.textContent = definition.label;
                button.addEventListener('click', () => finish(definition, button));
                choices.appendChild(button);
            });

            content.append(titleElement, detailElement, choices, status);
            card.append(avatar, content);
            this.elements.chatMessages.appendChild(card);
            this._pendingAnalysisChoice = { card, status, resolve };
            this.scrollToBottom();
            queueMicrotask(() => choices.querySelector('button')?.focus?.({ preventScroll: true }));
        });
    }

    _supersedeAnalysisChoiceFlow(flowId) {
        if (!flowId) return;
        this.elements.chatMessages
            .querySelectorAll(`.analysis-consent-card[data-choice-flow-id="${CSS.escape(flowId)}"]`)
            .forEach((card) => card.classList.add('is-superseded'));
    }

    _finalizeAnalysisChoiceFlow(flowId, summary) {
        if (!flowId) return;
        const cards = Array.from(this.elements.chatMessages.querySelectorAll(
            `.analysis-consent-card[data-choice-flow-id="${CSS.escape(flowId)}"]`
        ));
        const anchor = cards.at(-1);
        cards.forEach((card) => {
            if (card !== anchor) card.classList.add('is-superseded');
        });
        if (!anchor) return;
        anchor.classList.remove('is-superseded', 'is-navigation');
        anchor.classList.add('is-flow-summary', 'is-resolved');
        const title = anchor.querySelector('.analysis-consent-title');
        const status = anchor.querySelector('.analysis-consent-status');
        const isEn = (window.__lang || 'zh') === 'en';
        if (title) title.textContent = isEn ? 'Analysis route' : '分析路线';
        if (status) {
            status.textContent = summary;
            status.setAttribute('title', summary);
        }
        anchor.setAttribute('aria-label', `${isEn ? 'Analysis route' : '分析路线'}: ${summary}`);
    }

    /**
     * Always ask the user to choose Agent Swarm or Single Agent before any
     * visual analysis starts. The primary Agent may recommend a route, but it
     * never starts one. Authorization, execution mode, and collaboration
     * review policy remain three separate decisions.
     */
    async requestAgentSwarmConsent(reason = '', existingFlowId = '', swarmRecommended = true) {
        const isEn = (window.__lang || 'zh') === 'en';
        const flowId = existingFlowId || `analysis-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        const consent = await this._requestAnalysisChoice({
            flowId,
            testId: 'agent-swarm-consent',
            title: swarmRecommended
                ? (isEn
                    ? 'To answer this question, I recommend Agent Swarm mode. Do you agree?'
                    : '要回答您这个问题，我建议使用 Agent Swarm 模式，请问您是否同意？')
                : (isEn
                    ? 'This question requires inspecting the slide. Choose an analysis route.'
                    : '这个问题需要查看当前切片，请选择分析路线。'),
            detail: String(reason || '').trim() || (swarmRecommended
                ? (isEn
                    ? 'Multiple fields or scales should be compared before the final answer.'
                    : '需要比较多个视野或倍率后再形成最终结论。')
                : (isEn
                    ? 'Use Single Agent for sequential roaming, or Agent Swarm for multi-field comparison.'
                    : '可选择单 Agent 顺序游走，或使用 Agent Swarm 进行多视野比较。')),
            definitions: [
                {
                    label: isEn ? 'Agree · Use Agent Swarm' : '同意 · 使用 Agent Swarm',
                    value: 'agent_swarm',
                },
                {
                    label: isEn ? 'No · Use Single Agent' : '不同意 · 单 Agent 游走',
                    value: 'single_agent_roam',
                },
                {
                    label: isEn ? 'Cancel for now' : '暂不分析',
                    value: { cancelled: true, reason: 'user_cancelled' },
                },
            ],
        });
        if (consent?.cancelled) return consent;
        if (consent === 'single_agent_roam') {
            this._finalizeAnalysisChoiceFlow(
                flowId,
                isEn ? 'Single Agent roam' : '单 Agent 游走'
            );
            return {
                analysisStrategy: 'single_agent_roam',
                analysisMode: 'ai',
                userSelectedRoute: true,
            };
        }

        while (true) {
            const swarmMode = await this._requestAnalysisChoice({
            flowId,
            testId: 'agent-swarm-mode',
            title: isEn ? 'Choose an Agent Swarm mode' : '请选择 Agent Swarm 运行模式',
            detail: isEn
                ? 'Automatic mode needs no human approval. Collaborative mode pauses according to the review policy you choose next.'
                : '自动模式无需人工批准；协作模式将按下一步选择的审阅策略暂停。',
            definitions: [
                {
                    label: isEn ? 'Automatic mode' : '自动模式',
                    value: 'ai',
                    primary: true,
                },
                {
                    label: isEn ? 'Human–AI collaborative mode' : '人机协作模式',
                    value: 'collaborative',
                },
                {
                    label: isEn ? 'Back' : '返回上一步',
                    value: 'back',
                },
            ],
            });
            if (swarmMode?.cancelled) return swarmMode;
            if (swarmMode === 'back') {
                this._supersedeAnalysisChoiceFlow(flowId);
                return this.requestAgentSwarmConsent(reason, flowId, swarmRecommended);
            }
            if (swarmMode === 'ai') {
                this._finalizeAnalysisChoiceFlow(
                    flowId,
                    isEn ? 'Agent Swarm · Automatic' : 'Agent Swarm · 自动模式'
                );
                return {
                    analysisStrategy: 'agent_swarm',
                    analysisMode: 'ai',
                    userSelectedRoute: true,
                };
            }

            const reviewPolicy = await this._requestAnalysisChoice({
            flowId,
            testId: 'collaboration-review-policy',
            title: isEn ? 'Choose a collaboration review policy' : '请选择协作审阅策略',
            detail: isEn
                ? 'Approve every stage, or let the Agent run intermediate stages and review once before the final report.'
                : '可以逐阶段批准，也可以让 Agent 自动完成中间步骤，仅在生成终报前审阅一次。',
            definitions: [
                {
                    label: isEn ? 'Approve every stage' : '每阶段批准',
                    value: 'staged',
                    primary: true,
                },
                {
                    label: isEn ? 'Final review only' : '仅终末审阅',
                    value: 'final',
                },
                {
                    label: isEn ? 'Back' : '返回上一步',
                    value: 'back',
                },
            ],
            });
            if (reviewPolicy?.cancelled) return reviewPolicy;
            if (reviewPolicy === 'back') {
                this._supersedeAnalysisChoiceFlow(flowId);
                continue;
            }
            this._finalizeAnalysisChoiceFlow(
                flowId,
                reviewPolicy === 'final'
                    ? (isEn ? 'Agent Swarm · Collaborative · Final review' : 'Agent Swarm · 协作 · 终末审阅')
                    : (isEn ? 'Agent Swarm · Collaborative · Stage approvals' : 'Agent Swarm · 协作 · 每阶段批准')
            );
            return {
                analysisStrategy: 'agent_swarm',
                analysisMode: 'collaborative',
                collaborationReviewPolicy: reviewPolicy,
                userSelectedRoute: true,
            };
        }
    }

    /**
     * Patient Level is an explicit multi-slide workflow. It still requires the
     * same Swarm consent, but its collaborative contract is one review after
     * each slide is complete rather than five per-magnification gates. That
     * per-slide review is available for both Agent Swarm and Single Agent.
     */
    async requestPatientLevelStrategy(reason = '', existingFlowId = '', swarmRecommended = true) {
        const isEn = (window.__lang || 'zh') === 'en';
        const flowId = existingFlowId || `patient-analysis-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        const consent = await this._requestAnalysisChoice({
            flowId,
            testId: 'patient-swarm-consent',
            title: swarmRecommended
                ? (isEn
                    ? 'This patient-level comparison benefits from Agent Swarm. Do you agree?'
                    : '本次患者级多切片比较建议使用 Agent Swarm，是否同意？')
                : (isEn
                    ? 'Choose a route for this patient-level analysis.'
                    : '请选择本次患者级分析的执行路线。'),
            detail: String(reason || '').trim(),
            definitions: [
                {
                    label: isEn ? 'Agree · Use Agent Swarm' : '同意 · 使用 Agent Swarm',
                    value: 'agent_swarm',
                    primary: true,
                },
                {
                    label: isEn ? 'No · Single Agent per slide' : '不同意 · 每张切片单 Agent 游走',
                    value: 'single_agent_roam',
                },
                {
                    label: isEn ? 'Cancel for now' : '暂不分析',
                    value: { cancelled: true, reason: 'user_cancelled' },
                },
            ],
        });
        if (consent?.cancelled) return consent;
        const analysisStrategy = consent === 'single_agent_roam'
            ? 'single_agent_roam'
            : 'agent_swarm';

        const mode = await this._requestAnalysisChoice({
            flowId,
            testId: analysisStrategy === 'single_agent_roam'
                ? 'patient-single-mode'
                : 'patient-swarm-mode',
            title: isEn ? 'Choose the Patient Level mode' : '请选择 Patient Level 运行模式',
            detail: analysisStrategy === 'single_agent_roam'
                ? (isEn
                    ? 'Automatic mode aggregates after each roam. Collaborative mode still uses one Agent per slide — no Subagents or path tree. After each roam you confirm that slide; after every slide is confirmed, the patient-level report is reviewed. Serial waits for that confirmation before the next slide.'
                    : '自动模式在游走结束后直接汇总。协作模式仍是每张切片一名 Agent（不创建 Subagent、没有路径树）；每张游走结束后请确认该片，全部确认后再审阅患者级综合报告。串行时确认前不会开始下一张。')
                : (isEn
                    ? 'Automatic mode aggregates directly. Collaborative Agent Swarm reuses the same per-slide gates as a single WSI; all slides then get one patient-level report review.'
                    : '自动模式直接汇总；协作模式下 Agent Swarm 每张切片复用单张 WSI 的分阶段审阅，全部完成后再审阅患者级综合报告。'),
            definitions: [
                { label: isEn ? 'Automatic mode' : '自动模式', value: 'ai', primary: true },
                {
                    label: isEn ? 'Human–AI collaborative mode' : '人机协作模式',
                    value: 'collaborative',
                },
                { label: isEn ? 'Back' : '返回上一步', value: 'back' },
            ],
        });
        if (mode?.cancelled) return mode;
        if (mode === 'back') {
            this._supersedeAnalysisChoiceFlow(flowId);
            return this.requestPatientLevelStrategy(reason, flowId, swarmRecommended);
        }

        let collaborationReviewPolicy = null;
        if (mode === 'collaborative' && analysisStrategy === 'agent_swarm') {
            const reviewPolicy = await this._requestAnalysisChoice({
                flowId,
                testId: 'patient-collaboration-review-policy',
                title: isEn ? 'Choose a collaboration review policy' : '请选择协作审阅策略',
                detail: isEn
                    ? 'Approve every stage on each slide, or let intermediate stages run and review once before that slide’s report. After every slide, the patient-level summary is reviewed separately.'
                    : '可以在每张切片上逐阶段批准，也可以让中间步骤自动完成、仅在该片终报前审阅一次。全部切片完成后，还会再审阅患者级综合报告。',
                definitions: [
                    {
                        label: isEn ? 'Approve every stage' : '每阶段批准',
                        value: 'staged',
                        primary: true,
                    },
                    {
                        label: isEn ? 'Final review only' : '仅终末审阅',
                        value: 'final',
                    },
                    { label: isEn ? 'Back' : '返回上一步', value: 'back' },
                ],
            });
            if (reviewPolicy?.cancelled) return reviewPolicy;
            if (reviewPolicy === 'back') {
                this._supersedeAnalysisChoiceFlow(flowId);
                return this.requestPatientLevelStrategy(reason, flowId, swarmRecommended);
            }
            collaborationReviewPolicy = reviewPolicy;
        } else if (mode === 'collaborative') {
            collaborationReviewPolicy = 'final';
        }

        const schedule = await this._requestAnalysisChoice({
            flowId,
            testId: 'patient-schedule',
            title: isEn ? 'Analyze slides in parallel or one by one?' : '请选择切片调度方式',
            detail: isEn
                ? (mode === 'collaborative' && collaborationReviewPolicy === 'staged'
                    ? 'Stage-by-stage review is clearer one slide at a time. Parallel starts independent slides together.'
                    : 'Parallel starts independent slides together. Serial finishes one slide before the next begins.')
                : (mode === 'collaborative' && collaborationReviewPolicy === 'staged'
                    ? '分阶段审阅时建议串行，一次只审一张。并行会同时分析相互独立的切片。'
                    : '并行同时分析相互独立的切片；串行一次只分析一张，资源占用更可预期。'),
            definitions: [
                {
                    label: isEn ? 'Serial' : '串行',
                    value: 'serial',
                    primary: mode === 'collaborative' && collaborationReviewPolicy === 'staged',
                },
                {
                    label: isEn ? 'Parallel' : '并行',
                    value: 'parallel',
                    primary: !(mode === 'collaborative' && collaborationReviewPolicy === 'staged'),
                },
                { label: isEn ? 'Back' : '返回上一步', value: 'back' },
            ],
        });
        if (schedule?.cancelled) return schedule;
        if (schedule === 'back') {
            this._supersedeAnalysisChoiceFlow(flowId);
            return this.requestPatientLevelStrategy(reason, flowId, swarmRecommended);
        }
        const wsiExecutionMode = schedule === 'serial' ? 'serial' : 'parallel';
        this.setPatientWsiExecutionMode(wsiExecutionMode);

        let summary;
        if (analysisStrategy === 'single_agent_roam') {
            summary = mode === 'collaborative'
                ? (isEn
                    ? 'Patient Level · Single Agent per slide · Collaborative final review'
                    : 'Patient Level · 每张切片单 Agent 游走 · 每张切片终末审阅')
                : (isEn
                    ? 'Patient Level · Single Agent per slide · Automatic'
                    : 'Patient Level · 每张切片单 Agent 游走 · 自动模式');
        } else if (mode === 'collaborative') {
            summary = collaborationReviewPolicy === 'final'
                ? (isEn
                    ? 'Patient Level · Agent Swarm · Collaborative final review'
                    : 'Patient Level · Agent Swarm · 每张切片终末审阅')
                : (isEn
                    ? 'Patient Level · Agent Swarm · Stage approvals'
                    : 'Patient Level · Agent Swarm · 每张切片分阶段审阅');
        } else {
            summary = isEn
                ? 'Patient Level · Agent Swarm · Automatic'
                : 'Patient Level · Agent Swarm · 自动模式';
        }
        summary = wsiExecutionMode === 'serial'
            ? (isEn ? `${summary} · Serial` : `${summary} · 串行`)
            : (isEn ? `${summary} · Parallel` : `${summary} · 并行`);
        this._finalizeAnalysisChoiceFlow(flowId, summary);
        return {
            analysisStrategy,
            analysisMode: mode,
            wsiExecutionMode,
            collaborationReviewPolicy: mode === 'collaborative' ? collaborationReviewPolicy : null,
            userSelectedRoute: true,
        };
    }

    /**
     * 添加消息到聊天
     */
    addMessage(role, content, isTemporary = false, roiId = null) {
        this.elements.chatMessages.querySelector('.welcome-message')?.remove();
        const messageElement = document.createElement('div');
        messageElement.className = `message ${role}-message`;
        if (isTemporary) {
            messageElement.classList.add('temporary');
        }
        
        const avatar = role === 'assistant' ? '<i class="fas fa-robot"></i>' : '<i class="fas fa-user"></i>';
        
        messageElement.innerHTML = `
            <div class="message-avatar">
                ${avatar}
            </div>
            <div class="message-content">
                <div class="message-text">
                    ${this.formatMessageContent(content)}
                </div>
                <div class="message-time">${this.formatCurrentTime()}</div>
            </div>
        `;
        
        this.elements.chatMessages.appendChild(messageElement);
        this.scrollToBottom();
        if (!isTemporary) {
            this.tagIncomingPatientSlideReport(messageElement, { content });
            this._tagPatientChatArtifacts(messageElement);
        }
        
        // 保存到聊天历史
        if (!isTemporary) {
            const historyEntry = { role, content, timestamp: new Date() };
            if (roiId) {
                historyEntry.roiId = roiId;
            }
            this.chatHistory.push(historyEntry);
        }
        
        return messageElement;
    }

    /**
     * Commit a reply that is already complete.  Do not imitate streaming here:
     * artificial character playback makes the conversation lag behind the
     * concurrently running pathology workflow.
     */
    streamMessage(messageElement, content, options = {}) {
        if (!messageElement) return Promise.resolve(null);
        const messageText = messageElement.querySelector('.message-text');
        if (!messageText) return Promise.resolve(messageElement);

        const value = String(content || '');
        const recordHistory = options.recordHistory !== false;

        if (messageElement._pathcopilotStream?.cancel) {
            messageElement._pathcopilotStream.cancel();
        }
        messageElement.classList.remove('temporary');
        messageElement.classList.remove('is-streaming');
        messageText.innerHTML = this.formatMessageContent(value);
        delete messageElement._pathcopilotStream;
        if (recordHistory) {
            this.chatHistory.push({
                role: 'assistant',
                content: value,
                timestamp: new Date()
            });
        }
        this.scrollToBottom();
        this.tagIncomingPatientSlideReport(messageElement, { content: value });
        return Promise.resolve(messageElement);
    }

    createLiveMessageStream(messageElement, options = {}) {
        if (!messageElement) return null;
        const messageText = messageElement.querySelector('.message-text');
        if (!messageText) return null;
        if (messageElement._pathcopilotStream?.cancel) messageElement._pathcopilotStream.cancel();

        const recordHistory = options.recordHistory !== false;
        let rendered = '';
        let cancelled = false;
        let completed = false;
        let resolveDone = null;
        const done = new Promise(resolve => { resolveDone = resolve; });

        const complete = (content = '') => {
            if (completed || cancelled) return;
            completed = true;
            const value = String(content || '') || rendered;
            messageText.innerHTML = this.formatMessageContent(value);
            messageElement.classList.remove('is-streaming');
            delete messageElement._pathcopilotStream;
            if (recordHistory) {
                this.chatHistory.push({ role: 'assistant', content: value, timestamp: new Date() });
            }
            this.scrollToBottom();
            this.tagIncomingPatientSlideReport(messageElement, { content: value });
            resolveDone(messageElement);
        };

        const controller = {
            append: (chunk) => {
                const text = String(chunk || '');
                if (!text || cancelled || completed || !messageElement.isConnected) return;
                // Render each upstream delta immediately.  Never replay an
                // already-complete answer through an artificial character
                // queue while the pathology workflow continues elsewhere.
                rendered += text;
                messageText.textContent = rendered;
                this.scrollToBottom();
            },
            finish: (content = '') => {
                complete(content);
            },
            cancel: () => {
                if (cancelled || completed) return;
                cancelled = true;
                messageElement.classList.remove('is-streaming');
                delete messageElement._pathcopilotStream;
                resolveDone(messageElement);
            },
            done
        };
        messageElement._pathcopilotStream = controller;
        messageElement.classList.remove('temporary');
        messageElement.classList.add('is-streaming');
        messageText.textContent = '';
        return controller;
    }

    updateMessageStream(messageElement, content, options = {}) {
        return this.streamMessage(messageElement, content, options);
    }

    addStreamingMessage(role, content, roiId = null, options = {}) {
        if (role !== 'assistant') return this.addMessage(role, content, false, roiId);
        const messageElement = this.addMessage(role, '', true, roiId);
        this.streamMessage(messageElement, content, options);
        return messageElement;
    }
    
    /**
     * 更新消息内容
     */
    updateMessage(messageElement, content) {
        if (messageElement?._pathcopilotStream?.cancel) {
            messageElement._pathcopilotStream.cancel();
            delete messageElement._pathcopilotStream;
        }
        messageElement.classList.remove('is-streaming');
        const messageText = messageElement.querySelector('.message-text');
        messageText.innerHTML = this.formatMessageContent(content);
        
        // 移除临时标记
        messageElement.classList.remove('temporary');
        
        // 更新聊天历史
        this.chatHistory.push({ 
            role: 'assistant', 
            content, 
            timestamp: new Date() 
        });
        
        this.scrollToBottom();
    }
    
    /**
     * 格式化消息内容（支持Markdown）
     */
    formatMessageContent(content) {
        // 先处理标题（在换行之前）
        let formatted = content
            // H2 标题
            .replace(/^## (.+)$/gm, '<h2 style="margin: 10px 0 5px 0; font-size: 1.2em; font-weight: bold; color: #333;">$1</h2>')
            // H3 标题
            .replace(/^### (.+)$/gm, '<h3 style="margin: 8px 0 4px 0; font-size: 1.1em; font-weight: bold; color: #555;">$1</h3>')
            // 粗体
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            // 列表项
            .replace(/^- (.+)$/gm, '<li style="margin-left: 20px;">$1</li>')
            // ⭐ 特殊链接：#show-tree 打开树状图弹窗
            .replace(/\[(.+?)\]\(#show-tree\)/g, '<a href="javascript:void(0)" onclick="window.pathchat.openTreeModal()" style="color: #0066cc; text-decoration: underline; cursor: pointer;">$1</a>')
            // ⭐ 外部报告链接：在新窗口打开
            .replace(/\[(.+?)\]\((\/outputs\/[^)]+)\)/g, '<a href="$2" target="_blank" style="color: #0066cc; text-decoration: underline;">$1</a>')
            // 普通链接 [text](url)
            .replace(/\[(.+?)\]\((.+?)\)/g, '<a href="$2" style="color: #0066cc; text-decoration: underline;">$1</a>')
            // 换行
            .replace(/\n/g, '<br>');
        
        return formatted;
    }
    
    /**
     * 滚动到底部
     */
    scrollToBottom() {
        this.elements.chatMessages.scrollTop = this.elements.chatMessages.scrollHeight;
    }
    
    /**
     * 清空聊天
     */
    clearChat() {
        this.elements.chatMessages.innerHTML = '';
        this.chatHistory = [];
        
        // 清除高亮消息引用
        this.currentHighlightedMessage = null;
    }
    
    /**
     * 清除消息高亮
     */
    clearMessageHighlight() {
        if (this.currentHighlightedMessage) {
            this.currentHighlightedMessage.style.backgroundColor = '';
            this.currentHighlightedMessage.style.borderLeft = '';
            this.currentHighlightedMessage = null;
            console.log('🔄 已清除消息高亮');
        }
    }
    
    /**
     * 更新发送按钮状态
     */
    updateSendButton() {
        const hasMessage = this.elements.chatInput.value.trim().length > 0;
        const hasROI = this.currentROI !== null;
        const hasUserRois = (this.selectedUserRois || []).some((item) => item?.imageData);
        const hasScreenshot = !!(this.currentScreenshot && this.currentScreenshot.imageData)
            || hasUserRois;
        
        const canSend = (hasMessage || hasROI || hasUserRois)
            && (!this.isAnalyzing || Boolean(this.patientAnalysisAwaitingQuestion))
            && this.currentFile;
        
        this.elements.sendBtn.disabled = !canSend;
    }
    
    /**
     * 删除文件
     */
    async deleteFile(fileId) {
        const isEn = (window.__lang || 'zh') === 'en';
        const confirmed = await this._showSimpleConfirmDialog(
            isEn ? 'Are you sure you want to delete this file?' : '确定要删除这个文件吗？'
        );
        if (!confirmed) return;
        
        try {
            const response = await fetch(`/api/delete_file/${fileId}`, {
                method: 'DELETE'
            });
            
            const result = await response.json();
            
            if (result.success) {
                this.showNotification('success', (window.__lang||'zh')==='en' ? 'File deleted' : '文件删除成功');
                this.loadFilesList();
                
                // 如果删除的是当前文件，清空查看器
                if (this.currentFile && this.currentFile.file_id === fileId) {
                    this.clearViewer();
                }
            } else {
                this.showNotification('error', (window.__lang||'zh')==='en' ? `Delete failed: ${result.error}` : `删除失败：${result.error}`);
            }
            
        } catch (error) {
            console.error('删除文件失败:', error);
            this.showNotification('error', (window.__lang||'zh')==='en' ? `Delete failed: ${error.message}` : `删除失败：${error.message}`);
        }
    }
    
    /**
     * 清空查看器
     */
    clearViewer() {
        if (this.viewer) {
            this.viewer.destroy();
            this.viewer = null;
        }
        
        this.currentFile = null;
        this.currentROI = null;
        this.showViewerEmpty();
        this.hideViewerInfo();
        this.hideROISelectionInfo();
        this.updateFileListSelection(null);
    }
    
    /**
     * 更新文件列表选择状态
     */
    updateFileListSelection(fileId) {
        this.elements.filesList.querySelectorAll('.file-item').forEach(item => {
            item.classList.toggle('active', item.dataset.fileId === fileId);
        });
    }
    
    /**
     * 显示/隐藏查看器空状态
     */
    showViewerEmpty() {
        this.elements.viewerEmpty.style.display = 'block';
    }
    
    hideViewerEmpty() {
        this.elements.viewerEmpty.style.display = 'none';
    }
    
    /**
     * 显示/隐藏查看器信息
     */
    showViewerInfo() {
        if (this.elements.viewerInfo) {
            this.elements.viewerInfo.style.display = 'block';
        }
    }
    
    hideViewerInfo() {
        if (this.elements.viewerInfo) {
            this.elements.viewerInfo.style.display = 'none';
        }
    }
    
    /**
     * 更新图像信息
     */
    updateImageInfo(fileInfo) {
        if (this.elements.imageDimensions) {
            this.elements.imageDimensions.textContent = `${fileInfo.dimensions[0]}×${fileInfo.dimensions[1]}`;
        }
        if (this.elements.zoomLevel) {
            this.elements.zoomLevel.textContent = '100%';
        }
        if (this.elements.roiStatus) {
            this.elements.roiStatus.textContent = __t('status_no_region');
        }
    }
    
    /**
     * 更新状态指示器
     */
    updateStatus(status) {
        const statusDot = this.elements.statusIndicator.querySelector('.status-dot');
        const statusText = this.elements.statusIndicator.querySelector('span:last-child');
        
        statusDot.className = 'status-dot ' + status;
        
        const _e = (window.__lang||'zh')==='en';
        switch (status) {
            case 'ready':
                statusText.textContent = _e ? 'Ready' : '就绪';
                break;
            case 'processing':
                statusText.textContent = _e ? 'Processing' : '处理中';
                break;
            case 'error':
                statusText.textContent = _e ? 'Error' : '错误';
                break;
        }
        this.elements.statusIndicator.title = statusText.textContent;
        this.elements.statusIndicator.setAttribute('aria-label', statusText.textContent);
    }
    
    /**
     * 显示/隐藏加载指示器
     */
    showLoading(text = __t('processing')) {
        this.elements.loadingText.textContent = text;
        this.elements.loadingOverlay.style.display = 'flex';
    }
    
    hideLoading() {
        this.elements.loadingOverlay.style.display = 'none';
    }
    
    /**
     * 显示通知
     */
    showNotification(type, message, options = {}) {
        const notification = this.elements.notification;
        const icon = notification.querySelector('.notification-icon');
        const text = notification.querySelector('.notification-text');

        if (this._notificationTimer) {
            window.clearTimeout(this._notificationTimer);
            this._notificationTimer = null;
        }
        
        // 设置图标
        const icons = {
            success: 'fas fa-check-circle',
            error: 'fas fa-exclamation-circle',
            warning: 'fas fa-exclamation-triangle',
            info: 'fas fa-info-circle'
        };
        
        icon.className = `notification-icon ${icons[type] || icons.info}`;
        text.textContent = message;
        
        // 设置样式类
        notification.className = `notification ${type}`;
        notification.style.display = 'block';
        
        const duration = Number.isFinite(Number(options.duration))
            ? Number(options.duration)
            : 5000;
        if (duration > 0) {
            this._notificationTimer = window.setTimeout(() => {
                this.hideNotification();
            }, duration);
        }
    }

    async copyTextToClipboard(value) {
        const text = String(value || '').trim();
        if (!text) return false;
        try {
            if (window.pathcopilotDesktop?.copyText) {
                return Boolean(await window.pathcopilotDesktop.copyText(text));
            }
            if (navigator.clipboard?.writeText) {
                await navigator.clipboard.writeText(text);
                return true;
            }
            const textarea = document.createElement('textarea');
            textarea.value = text;
            textarea.setAttribute('readonly', '');
            textarea.style.position = 'fixed';
            textarea.style.opacity = '0';
            document.body.appendChild(textarea);
            textarea.select();
            const copied = document.execCommand('copy');
            textarea.remove();
            return copied;
        } catch (error) {
            console.warn('复制保存路径失败:', error);
            return false;
        }
    }

    showSavedStateNotification({
        cacheFile = '',
        htmlReportFile = '',
        summary = '',
        exported = false,
        fallbackDownload = false,
        note = ''
    } = {}) {
        const isEn = (window.__lang || 'zh') === 'en';
        const statePath = String(cacheFile || '').trim();
        const reportPath = String(htmlReportFile || '').trim();
        this.showNotification('success', '', { duration: 0 });

        const text = this.elements.notification.querySelector('.notification-text');
        const title = document.createElement('strong');
        title.className = 'notification-title';
        title.textContent = exported
            ? (isEn ? 'Restorable analysis saved' : '可恢复状态已保存')
            : (isEn ? 'Analysis saved' : '分析内容已保存');
        text.appendChild(title);

        if (summary) {
            const detail = document.createElement('span');
            detail.className = 'notification-summary';
            detail.textContent = summary;
            text.appendChild(detail);
        }
        if (note) {
            const extra = document.createElement('span');
            extra.className = 'notification-summary';
            extra.textContent = note;
            text.appendChild(extra);
        }

        const appendPath = (labelText, filePath) => {
            const group = document.createElement('div');
            group.className = 'notification-file-group';
            const label = document.createElement('span');
            label.className = 'notification-file-label';
            label.textContent = labelText;
            group.appendChild(label);

            const pathLine = document.createElement('code');
            pathLine.className = 'notification-path';
            pathLine.textContent = filePath;
            pathLine.title = filePath;
            group.appendChild(pathLine);

            const actions = document.createElement('div');
            actions.className = 'notification-actions';
            const canReveal = Boolean(
                window.pathcopilotDesktop?.isDesktop
                && window.pathcopilotDesktop?.showItemInFolder
                && (/^([a-zA-Z]:[\\/]|\/)/.test(filePath) && /[\\/]/.test(filePath.slice(2)))
            );
            if (canReveal) {
                const openButton = document.createElement('button');
                openButton.type = 'button';
                openButton.className = 'notification-action';
                openButton.innerHTML = `<i class="fas fa-folder-open" aria-hidden="true"></i><span>${isEn ? 'Show in folder' : '打开所在文件夹'}</span>`;
                openButton.addEventListener('click', async () => {
                    const opened = await window.pathcopilotDesktop.showItemInFolder(filePath);
                    if (!opened) this.showNotification('warning', isEn ? 'Could not open the folder' : '无法打开所在文件夹');
                });
                actions.appendChild(openButton);
            }

            const copyButton = document.createElement('button');
            copyButton.type = 'button';
            copyButton.className = 'notification-action';
            copyButton.innerHTML = `<i class="fas fa-copy" aria-hidden="true"></i><span>${isEn ? 'Copy path' : '复制路径'}</span>`;
            copyButton.addEventListener('click', async () => {
                const copied = await this.copyTextToClipboard(filePath);
                const buttonLabel = copyButton.querySelector('span');
                buttonLabel.textContent = copied
                    ? (isEn ? 'Copied' : '已复制')
                    : (isEn ? 'Copy failed' : '复制失败');
                window.setTimeout(() => {
                    buttonLabel.textContent = isEn ? 'Copy path' : '复制路径';
                }, 1600);
            });
            actions.appendChild(copyButton);
            group.appendChild(actions);
            text.appendChild(group);
        };

        if (statePath) appendPath(isEn ? 'Resumable state (JSON)' : '可恢复状态（JSON）', statePath);
        if (reportPath) {
            appendPath(isEn ? 'Pathology report (HTML)' : '病理报告（HTML）', reportPath);
        } else if (!exported) {
            const pending = document.createElement('span');
            pending.className = 'notification-report-pending';
            pending.textContent = isEn
                ? 'No HTML report is bound to this run yet.'
                : '本次运行尚未生成或绑定 HTML 报告。';
            text.appendChild(pending);
        }
    }
    
    /**
     * 隐藏通知
     */
    hideNotification() {
        if (this._notificationTimer) {
            window.clearTimeout(this._notificationTimer);
            this._notificationTimer = null;
        }
        this.elements.notification.style.display = 'none';
    }
    
    /**
     * 格式化文件大小
     */
    formatFileSize(bytes) {
        if (bytes === 0) return '0 B';
        
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    }
    
    /**
     * 格式化时间
     */
    formatTime(isoString) {
        const date = new Date(isoString);
        const now = new Date();
        const diff = now - date;
        const isEn = (window.__lang === 'en');

        if (diff < 60000)    return isEn ? 'Just now'                                    : '刚刚';
        if (diff < 3600000)  return isEn ? `${Math.floor(diff / 60000)} min ago`         : Math.floor(diff / 60000) + '分钟前';
        if (diff < 86400000) return isEn ? `${Math.floor(diff / 3600000)} hr ago`        : Math.floor(diff / 3600000) + '小时前';

        return date.toLocaleDateString(isEn ? 'en-US' : 'zh-CN');
    }
    
    /**
     * 格式化当前时间
     */
    formatCurrentTime() {
        const now = new Date();
        return now.toLocaleTimeString('zh-CN', { 
            hour: '2-digit', 
            minute: '2-digit' 
        });
    }
    
    _sameRoiRect(a, b) {
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

    // Grid IDs (roi_10x_r2_c1) are drawn first; later events rename the same
    // rect to R9.  Update the existing overlay instead of stacking a duplicate.
    /**
     * ⭐ 该坐标+层级上是否已有标注框（不比较 ID）。
     * 实时补画时用它判断「这个框是否真的缺失」：已存在就交给原有事件流
     * 管理 ID（含 _retargetRoiOverlay 的别名改名），绝不重复画、也不抢改 ID。
     */
    _hasOverlayAtRect(rect, magnification) {
        if (!rect) return false;
        const mag = Number(magnification);
        return (this.roiOverlays || []).some((item) => (
            this._sameRoiRect(item?.data?.rect, rect)
            && (!Number.isFinite(mag) || Number(item?.data?.magnification) === mag)
        ));
    }

    _retargetRoiOverlay(overlayData) {
        const nextId = overlayData?.id;
        const rect = overlayData?.rect;
        if (!nextId || !rect) return null;
        const nextMag = Number(overlayData.magnification);
        const twin = (this.roiOverlays || []).find((item) => (
            item?.id !== nextId
            && this._sameRoiRect(item?.data?.rect, rect)
            && (!Number.isFinite(nextMag) || Number(item?.data?.magnification) === nextMag)
        ));
        if (!twin) return null;
        const previousId = twin.id;
        twin.data = { ...(twin.data || {}), ...overlayData, original_id: twin.data?.original_id || previousId, id: nextId };
        twin.id = nextId;
        if (twin.element) {
            twin.element.dataset.roiId = nextId;
            twin.element.dataset.originalId = twin.data.original_id;
            twin.element.id = `roi-overlay-${nextId}`;
        }
        console.log(`🔄 红框 overlay 改名: ${previousId} → ${nextId}`);
        return twin.element;
    }

    /**
     * 绘制彩色ROI标注框
     */
    drawColoredROI(overlayData) {
        if (!this.viewer) {
            console.error('❌ viewer未初始化，无法绘制标注框');
            return null;
        }
        const agent = window.pathChatAgent;
        if (agent?._isPatientParallelRun?.()) {
            const overlayWsi = agent._asPatientWsiIndex(
                overlayData?.wsiIndex ?? overlayData?.wsi_index
            );
            const viewing = agent._asPatientWsiIndex(agent.currentViewingWsiIndex);
            if (Number.isInteger(overlayWsi) && Number.isInteger(viewing) && overlayWsi !== viewing) {
                return null;
            }
        }
        
        // ⭐ 检查是否已存在同ID的标注框，如果存在则直接跳过（避免闪烁）
        const existingOverlayIndex = this.roiOverlays.findIndex(o => o.id === overlayData.id);
        if (existingOverlayIndex !== -1) {
            // 已存在，直接返回现有元素，不重复绘制
            return this.roiOverlays[existingOverlayIndex].element;
        }

        const retargeted = this._retargetRoiOverlay(overlayData);
        if (retargeted) return retargeted;
        
        console.log('🎨 绘制新标注框:', overlayData.id);
        
        // 验证必要字段
        if (!overlayData.rect || overlayData.rect.x === undefined || overlayData.rect.y === undefined) {
            console.error('❌ overlayData缺少必要的rect字段:', overlayData);
            return null;
        }
        
        const rect = overlayData.rect;
        const color = overlayData.color || '#FF0000';
        const magnification = Number(overlayData.magnification || 10);
        
        console.log('📐 标注框参数:', {
            rect: rect,
            color: color,
            magnification: magnification,
            label: overlayData.label
        });
        
        // 转换为视口坐标
        const viewportRect = this.viewer.viewport.imageToViewportRectangle(
            rect.x, rect.y, rect.w, rect.h
        );
        
        // 创建标注框元素
        const overlayDiv = document.createElement('div');
        overlayDiv.className = `roi-overlay roi-overlay-${magnification}x roi-overlay-clickable`;
        if (overlayData.user_added) overlayDiv.classList.add('roi-overlay-user');
        overlayDiv.id = `roi-overlay-${overlayData.id}`;
        overlayDiv.dataset.roiId = overlayData.id;
        if (overlayData.original_id) overlayDiv.dataset.originalId = overlayData.original_id;
        overlayDiv.dataset.magnification = magnification;
        const semanticLevel = magnification >= 160 ? 'level3' : (magnification >= 40 ? 'level2' : 'level1');
        overlayDiv.dataset.roiLevel = semanticLevel;
        overlayDiv.dataset.level = semanticLevel === 'level1' ? 'red' : (semanticLevel === 'level2' ? 'blue' : 'green');
        overlayDiv.dataset.rect = JSON.stringify(rect);  // ⭐ 存储rect用于点击跳转
        const levelLabel = window.PathCopilotRoiPalette?.getLevelLabel(semanticLevel) ||
            ({ level1: 'Level 1 · 10×', level2: 'Level 2 · 40×', level3: 'Level 3 · 160×' })[semanticLevel];
        overlayDiv.setAttribute('aria-label', `${levelLabel} ROI ${overlayData.label || overlayData.id || ''}`.trim());
        overlayDiv.setAttribute('role', 'button');
        overlayDiv.tabIndex = 0;
        overlayDiv.title = levelLabel;
        
        // 设置基本样式（不设置颜色，让CSS类控制）
        overlayDiv.style.position = 'absolute';
        overlayDiv.style.pointerEvents = 'auto';
        overlayDiv.style.cursor = 'pointer';  // ⭐ 鼠标悬停显示手型
        overlayDiv.style.zIndex = '9999';  // ⭐ 确保在最上层
        
        // 创建标签（如果有的话）
        if (overlayData.label && overlayData.label.trim() !== '') {
            const label = document.createElement('div');
            label.className = 'roi-overlay-label';
            label.textContent = overlayData.label;
            // 标签背景色也由CSS类控制
            overlayDiv.appendChild(label);
        }
        
        // ⭐ 直接在overlay元素上绑定点击事件（解决Patient Level模式下点击无反应的问题）
        const activateOverlay = (e) => {
            e.stopPropagation();
            if (this.screenshotMode) return;
            if (this.clickPlaceRoiMode && this.viewer) {
                const viewerEl = document.getElementById('openseadragon-viewer');
                const box = viewerEl?.getBoundingClientRect();
                if (box) {
                    const pixel = new OpenSeadragon.Point(e.clientX - box.left, e.clientY - box.top);
                    const viewportPoint = this.viewer.viewport.pointFromPixel(pixel);
                    const imagePoint = this.viewer.viewport.viewportToImageCoordinates(viewportPoint);
                    this.placeUserRoiAt(imagePoint.x, imagePoint.y);
                }
                return;
            }
            const roiId = overlayDiv.dataset.roiId;
            console.log('📍 点击overlay:', roiId);
            
            if (window.pathChatAgent && typeof window.pathChatAgent.highlightLeftPanelCard === 'function') {
                window.pathChatAgent.highlightLeftPanelCard(roiId);
            }
            
            let parsedRect = null;
            if (overlayDiv.dataset.rect) {
                try {
                    parsedRect = JSON.parse(overlayDiv.dataset.rect);
                    this.jumpToROIView({ id: roiId, rect: parsedRect });
                } catch (err) {
                    console.warn('解析overlay坐标失败:', err);
                }
            }
        };
        overlayDiv.addEventListener('click', activateOverlay);
        overlayDiv.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            e.stopPropagation();
            let parsedRect = null;
            if (overlayDiv.dataset.rect) {
                try { parsedRect = JSON.parse(overlayDiv.dataset.rect); } catch (_) {}
            }
            this.showViewerQuickMenu({
                overlayData: { id: overlayDiv.dataset.roiId, rect: parsedRect },
                clientX: e.clientX,
                clientY: e.clientY,
            });
        });
        overlayDiv.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter' && e.key !== ' ') return;
            e.preventDefault();
            activateOverlay(e);
        });
        
        // 添加hover效果
        overlayDiv.addEventListener('mouseenter', () => {
            overlayDiv.style.opacity = '0.8';
            overlayDiv.style.filter = 'brightness(1.2)';
        });
        
        overlayDiv.addEventListener('mouseleave', () => {
            overlayDiv.style.opacity = '1';
            overlayDiv.style.filter = 'none';
        });
        
        // 添加到OpenSeadragon
        try {
            this.viewer.addOverlay({
                element: overlayDiv,
                location: new OpenSeadragon.Rect(
                    viewportRect.x,
                    viewportRect.y,
                    viewportRect.width,
                    viewportRect.height
                )
            });
            
            console.log('✅ 标注框已添加到OpenSeadragon:', overlayData.id);
            
            // 存储标注框信息
            this.roiOverlays.push({
                id: overlayData.id,
                element: overlayDiv,
                data: overlayData
            });
            
            console.log('📊 当前标注框总数:', this.roiOverlays.length);
            
            return overlayDiv;
            
        } catch (error) {
            console.error('❌ 添加标注框到OpenSeadragon失败:', error);
            return null;
        }
    }
    
    /**
     * ⭐ 按分析树补画 viewer 上缺失的红/蓝/绿框。
     * 运行中的框靠 socket 事件逐个画，并行线程漏一个事件就永远缺一个框；
     * 保存快照又只存画布残留。analysis_tree.json 是唯一完整来源，
     * 完成后和二次加载后都用它兜底补齐。drawColoredROI 自带按 ID 去重。
     */
    _backfillOverlaysFromTreeBoxes(redBoxes = []) {
        if (!this.viewer || !Array.isArray(redBoxes)) return 0;
        let added = 0;
        const draw = (box, magnification, color) => {
            if (!box || !box.id || !box.rect || box.rect.x === undefined || box.rect.y === undefined) return;
            const before = this.roiOverlays.length;
            try {
                // label 留空：与后端 create_roi_overlay 一致，纯线框不盖标签色块。
                this.drawColoredROI({
                    id: box.id,
                    rect: box.rect,
                    magnification,
                    color,
                    label: ''
                });
            } catch (err) {
                console.warn('⚠️ 补画标注框失败:', box.id, err);
                return;
            }
            if (this.roiOverlays.length > before) added += 1;
        };
        redBoxes.forEach((red) => {
            draw(red, 10, '#FF0000');
            (red.blueBoxes || []).forEach((blue) => {
                draw(blue, 40, '#0066FF');
                (blue.greenBoxes || []).forEach((green) => {
                    draw(green, 160, '#00CC00');
                });
            });
        });
        if (added > 0) {
            console.log(`🧩 依据分析树补画 ${added} 个缺失标注框（红/蓝/绿）`);
        }
        return added;
    }

    /**
     * 批量同步ROI标注框（增量模式 - 只添加新框，不移除已有框）
     * ⭐ 修复并行分析时每个红框发送的 overlays 互相覆盖的问题
     * ⭐ 优化：分批渲染，避免大量ROI一次性渲染导致浏览器崩溃
     */
    syncROIOverlays(overlays = []) {
        if (!this.viewer) {
            console.warn('❌ viewer未初始化，无法同步标注框');
            return;
        }
        
        const normalizedOverlays = Array.isArray(overlays) ? overlays : [];
        
        // ⭐ 获取已存在的框ID集合（用于跳过已存在的框）
        const existingIds = new Set(this.roiOverlays.map(o => o.id));
        
        // ⭐ 调试日志：统计各类型框的数量
        const typeCount = {red: 0, blue: 0, green: 0};
        normalizedOverlays.forEach(o => {
            if (o.magnification === 10) typeCount.red++;
            else if (o.magnification === 40) typeCount.blue++;
            else if (o.magnification === 160) typeCount.green++;
        });
        console.log(`📊 收到overlays: 红框${typeCount.red}个, 蓝框${typeCount.blue}个, 绿框${typeCount.green}个, 总计${normalizedOverlays.length}个`);
        
        // ⭐ 筛选出需要新增的标注框（已存在的跳过；同坐标改名则更新 ID）
        const newOverlays = [];
        let retargetedCount = 0;
        normalizedOverlays.forEach((overlayData) => {
            if (existingIds.has(overlayData.id)) return;
            if (this._retargetRoiOverlay(overlayData)) {
                retargetedCount += 1;
                existingIds.add(overlayData.id);
                return;
            }
            newOverlays.push(overlayData);
        });
        const skippedCount = normalizedOverlays.length - newOverlays.length - retargetedCount;
        
        if (skippedCount > 0) {
            console.log(`⏭️ 跳过 ${skippedCount} 个已存在的标注框`);
        }
        if (retargetedCount > 0) {
            console.log(`🔄 按坐标更新 ${retargetedCount} 个标注框 ID`);
        }
        
        // ⭐ 分批渲染优化：每批处理 15 个，避免浏览器崩溃
        const BATCH_SIZE = 15;
        
        const syncGen = this._roiOverlaySyncGen || 0;
        if (newOverlays.length <= BATCH_SIZE) {
            // 数量较少，直接同步渲染
            let addedCount = 0;
            newOverlays.forEach(overlayData => {
                try {
                    this.drawColoredROI(overlayData);
                    addedCount++;
                } catch (error) {
                    console.error('❌ 绘制标注框失败:', overlayData.id, error);
                }
            });
            if (addedCount > 0) {
                console.log(`✅ 新增 ${addedCount} 个标注框，当前总数: ${this.roiOverlays.length}`);
            }
        } else {
            // ⭐ 大量标注框：分批渲染，使用 requestAnimationFrame 避免阻塞
            console.log(`🔄 启用分批渲染: ${newOverlays.length} 个标注框，每批 ${BATCH_SIZE} 个`);
            
            let batchIndex = 0;
            let totalAdded = 0;
            const self = this;
            
            const renderBatch = () => {
                if (self._roiOverlaySyncGen !== syncGen) return;
                const start = batchIndex * BATCH_SIZE;
                const end = Math.min(start + BATCH_SIZE, newOverlays.length);
                const batch = newOverlays.slice(start, end);
                
                // 渲染当前批次
                batch.forEach(overlayData => {
                    if (self._roiOverlaySyncGen !== syncGen) return;
                    try {
                        self.drawColoredROI(overlayData);
                        totalAdded++;
                    } catch (error) {
                        console.error('❌ 绘制标注框失败:', overlayData.id, error);
                    }
                });
                
                batchIndex++;
                
                // 检查是否还有更多批次
                if (end < newOverlays.length) {
                    // 使用 requestAnimationFrame 安排下一批次，让浏览器有时间渲染
                    requestAnimationFrame(renderBatch);
                } else {
                    // 全部完成
                    console.log(`✅ 分批渲染完成: 新增 ${totalAdded} 个标注框，当前总数: ${self.roiOverlays.length}`);
                }
            };
            
            // 启动第一批渲染
            requestAnimationFrame(renderBatch);
        }
    }
    
    _roiOverlayRect(overlay) {
        const rect = overlay?.data?.rect || overlay?.rect;
        if (!rect || typeof rect !== 'object') return null;
        const x = Number(rect.x ?? rect.left);
        const y = Number(rect.y ?? rect.top);
        const w = Number(rect.w ?? rect.width);
        const h = Number(rect.h ?? rect.height);
        if (![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0) return null;
        return { x, y, w, h };
    }

    // Nested red/blue/green boxes share the same click point. Prefer the
    // smallest containing overlay so a 160× field is not stolen by its parent red.
    _pickInnermostRoiHit(x, y) {
        const hits = [];
        for (const overlay of this.roiOverlays || []) {
            const rect = this._roiOverlayRect(overlay);
            if (!rect) continue;
            if (x < rect.x || x > rect.x + rect.w || y < rect.y || y > rect.y + rect.h) continue;
            hits.push({
                overlay,
                overlayData: overlay.data || overlay,
                area: rect.w * rect.h,
                mag: Number(overlay?.data?.magnification) || 0,
            });
        }
        if (!hits.length) return null;
        hits.sort((a, b) => {
            if (a.area !== b.area) return a.area - b.area;
            return b.mag - a.mag;
        });
        return hits[0];
    }

    hideViewerQuickMenu() {
        document.getElementById('viewerQuickMenu')?.remove();
        document.removeEventListener('mousedown', this._viewerQuickMenuDismiss, true);
        this._viewerQuickMenuDismiss = null;
        document.removeEventListener('keydown', this._viewerQuickMenuKeydown, true);
        this._viewerQuickMenuKeydown = null;
    }

    openViewerQuickMenuFromPointer(event) {
        if (this.screenshotMode) return;
        if (window.pathChatAgent?.editMode || window.pathChatAgent?.mapEditMode) return;
        const original = event?.originalEvent || event;
        let overlayData = event?.overlayData || null;
        if (!overlayData && event?.position && this.viewer) {
            const viewportPoint = this.viewer.viewport.pointFromPixel(event.position);
            const imagePoint = this.viewer.viewport.viewportToImageCoordinates(viewportPoint);
            overlayData = this._pickInnermostRoiHit(imagePoint.x, imagePoint.y)?.overlayData || null;
        }
        this.showViewerQuickMenu({
            overlayData,
            clientX: original?.clientX,
            clientY: original?.clientY,
        });
    }

    showViewerQuickMenu({ overlayData = null, clientX = 0, clientY = 0 } = {}) {
        this.hideViewerQuickMenu();
        if (this.screenshotMode) return;
        if (window.pathChatAgent?.editMode || window.pathChatAgent?.mapEditMode) return;
        const isEn = (window.__lang || 'zh') === 'en';
        const roiId = String(overlayData?.id || overlayData?.roi_id || '').trim();
        const menu = document.createElement('div');
        menu.id = 'viewerQuickMenu';
        menu.className = 'viewer-quick-menu';
        const quoteDisabled = roiId ? '' : 'disabled';
        menu.innerHTML = `
            <div class="viewer-quick-menu-hint">${isEn ? 'Right-click menu' : '右键快捷菜单'}</div>
            <button type="button" data-quick="place">
                <i class="fas fa-plus-square"></i>
                ${isEn ? 'Click to place ROI' : '点击放置 ROI'}
            </button>
            <button type="button" data-quick="select">
                <i class="fas fa-crop-alt"></i>
                ${isEn ? 'Drag to capture a region' : '拖拽截取提问'}
            </button>
            <button type="button" data-quick="comment">
                <i class="fas fa-comment-dots"></i>
                ${isEn ? 'Comment here' : '发表评论'}
            </button>
            <button type="button" data-quick="quote" ${quoteDisabled}>
                <i class="fas fa-quote-left"></i>
                ${roiId
                    ? (isEn ? `Quote ${roiId} to chat` : `引用 ${roiId} 解读到聊天`)
                    : (isEn ? 'Quote interpretation' : '引用解读到聊天')}
            </button>
            <button type="button" data-quick="dismiss">
                <i class="fas fa-times"></i>
                ${isEn ? 'Cancel' : '取消'}
            </button>
        `;
        const left = Math.min(Math.max(8, Number(clientX) || 12), window.innerWidth - 240);
        const top = Math.min(Math.max(8, Number(clientY) || 12), window.innerHeight - 160);
        menu.style.left = `${left}px`;
        menu.style.top = `${top}px`;
        document.body.appendChild(menu);

        const run = (action) => {
            this.hideViewerQuickMenu();
            if (action === 'dismiss') return;
            if (action === 'place') {
                this.toggleClickPlaceRoiMode(true);
                return;
            }
            if (action === 'select') {
                if (!this.screenshotMode) this.toggleScreenshotMode();
                return;
            }
            const input = document.getElementById('chatInput');
            if (action === 'comment') {
                if (input) {
                    input.value = roiId
                        ? (isEn ? `About ${roiId}: ` : `关于 ${roiId}：`)
                        : (isEn ? 'About this field of view: ' : '关于当前视野：');
                    input.focus();
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                }
                return;
            }
            if (action === 'quote') {
                if (!roiId) return;
                const parts = roiId.split('-').filter(Boolean);
                const phase = parts.length >= 3 ? 4 : (parts.length === 2 ? 3 : 2);
                if (typeof window.pathChatAgent?.quoteAnalysisToChat === 'function') {
                    window.pathChatAgent.quoteAnalysisToChat(roiId, phase);
                } else if (input) {
                    input.value = isEn ? `Regarding ${roiId}: ` : `关于 ${roiId}：`;
                    input.focus();
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                }
            }
        };
        menu.addEventListener('click', (event) => {
            const button = event.target.closest('[data-quick]');
            if (!button || button.disabled) return;
            event.preventDefault();
            event.stopPropagation();
            run(button.dataset.quick);
        });
        this._viewerQuickMenuDismiss = (event) => {
            if (event.target.closest?.('#viewerQuickMenu')) return;
            this.hideViewerQuickMenu();
        };
        // Esc 也能关闭；「取消」按钮 + 点空白 + Esc 三条退出路径。
        this._viewerQuickMenuKeydown = (event) => {
            if (event.key !== 'Escape') return;
            event.stopPropagation();
            this.hideViewerQuickMenu();
        };
        window.setTimeout(() => {
            document.addEventListener('mousedown', this._viewerQuickMenuDismiss, true);
            document.addEventListener('keydown', this._viewerQuickMenuKeydown, true);
        }, 0);
    }

    /**
     * 检查点击位置是否在某个ROI内
     */
    checkROIClick(x, y) {
        if (window.pathChatAgent && (window.pathChatAgent.editMode || window.pathChatAgent.mapEditMode)) {
            console.log('🔍 跳过ROI点击检查（当前处于编辑模式: editMode=', window.pathChatAgent.editMode, ', mapEditMode=', window.pathChatAgent.mapEditMode, ')');
            return;
        }
        
        console.log('🔍 检查点击位置，当前ROI数量:', this.roiOverlays.length);
        console.log('📍 点击坐标: x=', x, ', y=', y);
        
        const hit = this._pickInnermostRoiHit(x, y);
        if (hit) {
            console.log('✅ 点击了最内层ROI:', hit.overlay.id, '倍率:', hit.overlayData.magnification);
            this.handleROIClick(hit.overlayData);
        } else {
            console.log('⚪ 点击位置不在任何ROI内');
        }
    }
    
    /**
     * 处理ROI标注框点击
     */
    handleROIClick(overlayData) {
        console.log('🔘 ROI标注框被点击:', overlayData);
        console.log('📦 当前存储的所有ROI分析数据keys:', Object.keys(this.roiAnalysisData));
        console.log('🔍 尝试获取的key:', overlayData.id);
        
        // ⭐ 自动放大到ROI区域（分步策略，避免模糊）
        if (this.viewer && overlayData.rect) {
            const rect = overlayData.rect;
            
            // 将图像坐标转换为视口坐标
            const viewportRect = this.viewer.viewport.imageToViewportRectangle(
                rect.x, rect.y, rect.w, rect.h
            );
            
            // 添加适当的边距（让ROI周围有一些上下文）
            const padding = 0.15; // 15%的边距
            const paddedRect = new OpenSeadragon.Rect(
                viewportRect.x - viewportRect.width * padding,
                viewportRect.y - viewportRect.height * padding,
                viewportRect.width * (1 + 2 * padding),
                viewportRect.height * (1 + 2 * padding)
            );
            
            // 获取当前缩放级别和目标缩放级别
            const currentZoom = this.viewer.viewport.getZoom();
            const bounds = this.viewer.viewport.getBounds();
            const targetZoom = Math.min(
                bounds.width / paddedRect.width,
                bounds.height / paddedRect.height
            );
            
            // 如果需要放大超过2倍，则采用分步策略
            if (targetZoom / currentZoom > 2) {
                console.log('🔍 采用分步放大策略，避免模糊');
                
                // 第一步：先移动到ROI中心位置（当前缩放级别）
                const centerX = paddedRect.x + paddedRect.width / 2;
                const centerY = paddedRect.y + paddedRect.height / 2;
                this.viewer.viewport.panTo(
                    new OpenSeadragon.Point(centerX, centerY),
                    true  // 平滑移动
                );
                
                // 第二步：延迟600ms后开始放大（让切片开始加载）
                setTimeout(() => {
                    // 中等缩放
                    const intermediateZoom = currentZoom * 1.8;
                    this.viewer.viewport.zoomTo(intermediateZoom, null, true);
                }, 600);
                
                // 第三步：再延迟600ms，放大到目标区域
                setTimeout(() => {
                    this.viewer.viewport.fitBounds(paddedRect, true);
                    console.log('✅ 分步放大完成');
                }, 1200);
                
            } else {
                // 放大倍数不大，直接一步到位
                this.viewer.viewport.fitBounds(paddedRect, true);
                console.log('🔍 直接放大到ROI区域');
            }
        }
        
        // ⭐⭐⭐ 关键修复：调用 highlightLeftPanelCard 来高亮左侧面板
        // 此函数同时支持单张WSI模式（routeStepsList）和 Patient Level 模式（analysis-tree）
        if (window.pathChatAgent && typeof window.pathChatAgent.highlightLeftPanelCard === 'function') {
            console.log('📍 [handleROIClick] 调用 highlightLeftPanelCard:', overlayData.id);
            window.pathChatAgent.highlightLeftPanelCard(overlayData.id);
        }
        
        // 高亮当前标注框
        this.highlightROI(overlayData.id);
    }
    
    /**
     * ⭐ 重新绑定左侧面板的点击事件（加载保存状态后使用）
     * 使用事件委托方式，避免破坏DOM结构和滚动功能
     */
    rebindLeftPanelClickEvents(container) {
        if (!container) return;
        
        // ⭐ 使用事件委托，只在容器上绑定一个事件监听器
        // 移除可能存在的旧监听器
        if (this._leftPanelClickHandler) {
            container.removeEventListener('click', this._leftPanelClickHandler);
        }
        
        // 创建新的点击处理器（处理ROI点击和展开/收起）
        this._leftPanelClickHandler = (e) => {
            // ⭐ 首先检查是否点击了展开/收起图标（Patient Level树形结构）
            const toggleIcon = e.target.closest('.toggle-icon');
            if (toggleIcon) {
                e.stopPropagation();
                const toggleId = toggleIcon.dataset.toggle;
                const childrenEl = container.querySelector(`.tree-children[data-parent="${toggleId}"]`);
                const branchEl = toggleIcon.closest('.tree-branch');
                
                if (childrenEl) {
                    const isExpanded = childrenEl.style.display !== 'none';
                    childrenEl.style.display = isExpanded ? 'none' : '';
                    toggleIcon.classList.toggle('fa-chevron-down', !isExpanded);
                    toggleIcon.classList.toggle('fa-chevron-right', isExpanded);
                    if (branchEl) {
                        branchEl.classList.toggle('expanded', !isExpanded);
                        branchEl.classList.toggle('collapsed', isExpanded);
                    }
                }
                console.log('📂 [恢复的面板] 展开/收起:', toggleId, childrenEl?.style.display);
                return; // 不继续处理ROI点击
            }
            
            // ⭐ 检查是否点击了自动模式树状结构的展开/收起按钮
            const routeTreeToggle = e.target.closest('.route-tree-toggle');
            if (routeTreeToggle) {
                e.stopPropagation();
                // 找到父header元素并切换collapsed状态
                const header = routeTreeToggle.closest('.route-tree-header, .route-tree-blue-header');
                if (header) {
                    header.classList.toggle('collapsed');
                    console.log('📂 [恢复的面板] 树节点展开/收起:', header.dataset.roiId);
                }
                return;
            }
            
            // 查找最近的带 data-roi-id 的元素
            const target = e.target.closest('[data-roi-id]');
            if (!target) return;
            
            const roiId = target.dataset.roiId;
            if (!roiId) return;
            
            e.stopPropagation();
            console.log('📍 [恢复的面板] 点击ROI:', roiId);
            
            // 高亮当前卡片
            this.highlightLeftRouteCard(roiId);
            
            // 跳转到对应的ROI区域
            const want = String(roiId || '').toLowerCase();
            const overlay = this.roiOverlays.find((o) => (
                String(o?.data?.id || o?.id || '').toLowerCase() === want
                || String(o?.data?.original_id || '').toLowerCase() === want
            ));
            if (overlay && overlay.data && overlay.data.rect) {
                this.jumpToROIView(overlay.data);
            } else if (target.dataset.rect) {
                try {
                    const rect = JSON.parse(target.dataset.rect);
                    const patientMatch = String(roiId).match(/^PATIENT-ROAM-(\d+)-/i);
                    const wsiIndex = patientMatch ? Number(patientMatch[1]) : null;
                    if (wsiIndex != null && window.pathChatAgent?.currentViewingWsiIndex !== wsiIndex) {
                        this.selectPlWsiItem(wsiIndex);
                    }
                    this.jumpToROIView({
                        id: roiId,
                        rect,
                        magnification: Number(target.dataset.magnification) || 10,
                    });
                } catch (err) {
                    console.warn('恢复的视野坐标无法跳转:', err);
                }
            }
            
            // 高亮图上的ROI
            this.highlightROI(overlay?.id || roiId);
        };
        
        // 绑定事件委托
        container.addEventListener('click', this._leftPanelClickHandler);
        
        const clickableCount = container.querySelectorAll('[data-roi-id]').length;
        const toggleCount = container.querySelectorAll('.toggle-icon').length;
        const routeToggleCount = container.querySelectorAll('.route-tree-toggle').length;
        console.log(`✅ 重新绑定完成: ${clickableCount} 个ROI, ${toggleCount + routeToggleCount} 个展开/收起按钮`);
    }
    
    /**
     * 高亮左侧路径卡片（三向映射）
     */
    highlightLeftRouteCard(roiId) {
        if (!roiId) return;
        if (window.pathChatAgent && typeof window.pathChatAgent._canonicalMappedRoiId === 'function') {
            roiId = window.pathChatAgent._canonicalMappedRoiId(roiId) || roiId;
        }

        if (window.pathChatAgent && typeof window.pathChatAgent._isRoamMappedRoiId === 'function'
            && window.pathChatAgent._isRoamMappedRoiId(roiId)
            && typeof window.pathChatAgent._highlightRoamActivityItem === 'function') {
            if (window.pathChatAgent._highlightRoamActivityItem(roiId)) return;
        }
        
        const routeStepsList = document.getElementById('routeStepsList');
        if (!routeStepsList) return;
        
        // ⭐ 清除之前的高亮（包括所有激活类）
        routeStepsList.querySelectorAll('.route-step-card, .collab-task-card, .v2-agent-activity-item, .route-tree-blue-node, .route-tree-green-item, .route-tree-header, .route-tree-blue-header').forEach(c => {
            c.classList.remove('active', 'active-viewing', 'card-active', 'roi-selected-glow');
        });
        
        // 查找对应的左侧卡片：Swarm 优先树节点
        let routeCard = (window.pathChatAgent && typeof window.pathChatAgent._findMappedTreeCard === 'function')
            ? window.pathChatAgent._findMappedTreeCard(roiId)
            : null;
        if (!routeCard) {
            routeCard = routeStepsList.querySelector(
                `.route-tree-green-item[data-roi-id="${roiId}"], .route-tree-blue-node[data-roi-id="${roiId}"], .route-tree-node[data-roi-id="${roiId}"], .route-step-card[data-roi-id="${roiId}"]`
            );
        }
        
        // 精确大小写兜底；不要用 startsWith，否则点蓝框会命中绿框 R5-B2-G11
        if (!routeCard) {
            const want = String(roiId || '').toLowerCase();
            const allCards = routeStepsList.querySelectorAll('.route-tree-blue-node[data-roi-id], .route-tree-green-item[data-roi-id], .route-tree-node[data-roi-id], .route-step-card[data-roi-id]');
            for (const card of allCards) {
                if (String(card.dataset.roiId || '').toLowerCase() === want) {
                    routeCard = card;
                    break;
                }
            }
        }
        
        if (routeCard) {
            // ⭐ 添加所有激活类，确保样式明显
            routeCard.classList.add('active', 'active-viewing', 'card-active');
            routeCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
            
            // ⭐ 添加闪烁效果
            routeCard.classList.add('flash-highlight');
            setTimeout(() => {
                routeCard.classList.remove('flash-highlight');
            }, 800);
            
            console.log('✅ 高亮左侧卡片:', roiId);
        } else {
            console.log('ℹ️ 该ROI暂无左侧卡片:', roiId);
        }
    }
    
    /**
     * 显示ROI分析结果
     */
    showROIAnalysis(overlayData) {
        // 获取分析数据
        const analysisData = this.roiAnalysisData[overlayData.id];
        
        if (!analysisData) {
            console.warn('没有找到分析数据:', overlayData.id);
            // 如果没有分析数据，显示基本信息
            this.showBasicROIInfo(overlayData);
            return;
        }
        
        // 解析GPT分析结果（假设格式包含不同的部分）
        const analysis = analysisData.gpt_analysis || __t('tree_analyzing');
        const parsedAnalysis = this.parseAnalysisContent(analysis);
        
        // 获取放大倍数信息
        const magnification = overlayData.magnification || analysisData.zoom_level || 'N/A';
        const levelName = overlayData.level_name || analysisData.level_name || this.getMagnificationLabel(magnification);
        const confidence = overlayData.confidence !== undefined ? overlayData.confidence : analysisData.confidence;
        
        // 创建分析结果消息 - 参考截图的卡片式设计
        const messageHTML = `
            <div class="roi-analysis-detail" style="margin: 10px 0;">
                <!-- 标题区域 -->
                <div style="margin-bottom: 15px; border-bottom: 2px solid #e0e0e0; padding-bottom: 8px;">
                    <h4 style="margin: 0; color: #333; font-size: 16px;">
                        🔬 ${levelName} 区域分析
                    </h4>
                    <div class="analysis-meta" style="margin-top: 8px; display: flex; gap: 10px;">
                        <span class="badge" style="background: #4a90e2; color: white; padding: 4px 10px; border-radius: 12px; font-size: 12px;">
                            放大倍数: ${magnification}×
                        </span>
                    </div>
                </div>
                
                <!-- ROI预览图 -->
                ${analysisData.image_path ? `
                    <div class="analysis-image" style="margin-bottom: 15px; text-align: center; background: #f8f9fa; padding: 10px; border-radius: 8px;">
                        <img src="/${analysisData.image_path}" alt="ROI图像" style="max-width: 100%; border-radius: 6px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
                    </div>
                ` : ''}
                
                <!-- 模型关注点 -->
                ${parsedAnalysis.keyPoints ? `
                    <div class="analysis-card" style="background: #e3f2fd; border-left: 4px solid #2196f3; padding: 12px; margin-bottom: 12px; border-radius: 6px;">
                        <div style="display: flex; align-items: center; margin-bottom: 8px;">
                            <span style="font-size: 18px; margin-right: 8px;">🔵</span>
                            <strong style="color: #1976d2; font-size: 14px;">模型关注点</strong>
                        </div>
                        <div style="color: #333; line-height: 1.6; font-size: 13px; white-space: pre-wrap;">${parsedAnalysis.keyPoints}</div>
                    </div>
                ` : ''}
                
                <!-- 区域分析 -->
                ${parsedAnalysis.regionAnalysis ? `
                    <div class="analysis-card" style="background: #e8f5e9; border-left: 4px solid #4caf50; padding: 12px; margin-bottom: 12px; border-radius: 6px;">
                        <div style="display: flex; align-items: center; margin-bottom: 8px;">
                            <span style="font-size: 18px; margin-right: 8px;">💚</span>
                            <strong style="color: #388e3c; font-size: 14px;">区域分析</strong>
                        </div>
                        <div style="color: #333; line-height: 1.6; font-size: 13px; white-space: pre-wrap;">${parsedAnalysis.regionAnalysis}</div>
                    </div>
                ` : ''}
                
                <!-- 区域总结 -->
                ${parsedAnalysis.summary ? `
                    <div class="analysis-card" style="background: #fff3e0; border-left: 4px solid #ff9800; padding: 12px; margin-bottom: 12px; border-radius: 6px;">
                        <div style="display: flex; align-items: center; margin-bottom: 8px;">
                            <span style="font-size: 18px; margin-right: 8px;">📋</span>
                            <strong style="color: #f57c00; font-size: 14px;">区域总结</strong>
                        </div>
                        <div style="color: #333; line-height: 1.6; font-size: 13px; white-space: pre-wrap;">${parsedAnalysis.summary}</div>
                    </div>
                ` : ''}
                
                <!-- 如果没有结构化数据，显示完整的分析内容 -->
                ${!parsedAnalysis.keyPoints && !parsedAnalysis.regionAnalysis && !parsedAnalysis.summary ? `
                    <div class="analysis-content" style="background: #f5f5f5; padding: 12px; border-radius: 6px; line-height: 1.6; color: #333; font-size: 13px;">
                        ${analysis}
                    </div>
                ` : ''}
                
                <!-- 指标信息 -->
                ${analysisData.metrics && Object.keys(analysisData.metrics).length > 0 ? `
                    <div class="analysis-metrics" style="margin-top: 12px; padding: 10px; background: #fafafa; border-radius: 6px;">
                        <strong style="color: #666; font-size: 12px;">📊 量化指标：</strong>
                        <div style="margin-top: 6px; display: flex; flex-wrap: wrap; gap: 8px;">
                            ${Object.entries(analysisData.metrics).map(([key, value]) => `
                                <span style="background: white; padding: 4px 8px; border-radius: 4px; font-size: 11px; color: #555; border: 1px solid #e0e0e0;">
                                    ${key}: ${typeof value === 'number' ? value.toFixed(2) : value}
                                </span>
                            `).join('')}
                        </div>
                    </div>
                ` : ''}
            </div>
        `;
        
        // 显示在聊天区域（不切换标签页）
        this.addMessage('assistant', messageHTML);
        
        // 滚动到最新消息
        this.scrollChatToBottom();
    }
    
    /**
     * 显示基本ROI信息（当没有分析数据时）
     */
    showBasicROIInfo(overlayData) {
        const magnification = overlayData.magnification || 'N/A';
        const levelName = overlayData.level_name || this.getMagnificationLabel(magnification);
        
        const messageHTML = `
            <div class="roi-analysis-detail" style="margin: 10px 0;">
                <div style="padding: 15px; background: #f0f0f0; border-radius: 8px; border-left: 4px solid #999;">
                    <h4 style="margin: 0 0 10px 0; color: #333;">🔬 ${levelName}</h4>
                    <p style="margin: 5px 0; color: #666; font-size: 13px;">
                        <strong>放大倍数:</strong> ${magnification}×
                    </p>
                    <p style="margin: 5px 0; color: #666; font-size: 13px;">
                        <strong>ROI ID:</strong> ${overlayData.id}
                    </p>
                    <p style="margin: 10px 0 0 0; color: #999; font-size: 12px; font-style: italic;">
                        该区域的详细分析数据暂未加载
                    </p>
                </div>
            </div>
        `;
        
        this.addMessage('assistant', messageHTML);
        this.scrollChatToBottom();
    }
    
    /**
     * 解析分析内容，尝试提取结构化信息
     */
    parseAnalysisContent(analysisText) {
        const result = {
            keyPoints: '',
            regionAnalysis: '',
            summary: ''
        };
        
        // 尝试按关键词分割内容
        const text = analysisText || '';
        
        // 匹配"模型关注点"或类似标题
        const keyPointsMatch = text.match(/(?:模型关注点|关键发现|Key\s*Points?)[：:]\s*([\s\S]*?)(?=(?:区域分析|区域总结|$))/i);
        if (keyPointsMatch) {
            result.keyPoints = keyPointsMatch[1].trim();
        }
        
        // 匹配"区域分析"
        const regionMatch = text.match(/(?:区域分析|Regional?\s*Analysis)[：:]\s*([\s\S]*?)(?=(?:区域总结|$))/i);
        if (regionMatch) {
            result.regionAnalysis = regionMatch[1].trim();
        }
        
        // 匹配"区域总结"
        const summaryMatch = text.match(/(?:区域总结|总结|Summary)[：:]\s*([\s\S]*?)$/i);
        if (summaryMatch) {
            result.summary = summaryMatch[1].trim();
        }
        
        // 如果没有找到结构化内容，尝试智能分段
        if (!result.keyPoints && !result.regionAnalysis && !result.summary) {
            // 按段落分割
            const paragraphs = text.split(/\n\n+/).filter(p => p.trim());
            
            if (paragraphs.length >= 3) {
                result.keyPoints = paragraphs[0];
                result.regionAnalysis = paragraphs.slice(1, -1).join('\n\n');
                result.summary = paragraphs[paragraphs.length - 1];
            } else if (paragraphs.length === 2) {
                result.regionAnalysis = paragraphs[0];
                result.summary = paragraphs[1];
            }
        }
        
        return result;
    }
    
    /**
     * 根据放大倍数获取标签
     */
    getMagnificationLabel(magnification) {
        const mag = parseInt(magnification);
        if (mag <= 10) return __t('mag_low');
        if (mag <= 40) return __t('mag_mid');
        if (mag <= 100) return __t('mag_high');
        return __t('mag_ultra');
    }
    
    /**
     * 滚动聊天框到底部
     */
    scrollChatToBottom() {
        const chatMessages = document.getElementById('chatMessages');
        if (chatMessages) {
            setTimeout(() => {
                chatMessages.scrollTop = chatMessages.scrollHeight;
            }, 100);
        }
    }
    
    /**
     * 高亮ROI标注框
     */
    highlightROI(roiId) {
        // 取消所有标注框的高亮
        this.roiOverlays.forEach(overlay => {
            overlay.element.style.transform = 'scale(1)';
            overlay.element.style.zIndex = '1';
        });
        
        // 高亮指定标注框
        const want = String(roiId || '').toLowerCase();
        let targetOverlay = this.roiOverlays.find((o) => (
            String(o?.id || '').toLowerCase() === want
            || String(o?.data?.id || '').toLowerCase() === want
            || String(o?.data?.original_id || '').toLowerCase() === want
            || String(o?.element?.dataset?.originalId || '').toLowerCase() === want
        ));
        if (!targetOverlay && window.pathChatAgent
            && typeof window.pathChatAgent._resolveRoiOverlayRecord === 'function') {
            const rec = window.pathChatAgent._resolveRoiOverlayRecord(roiId);
            targetOverlay = this.roiOverlays.find((o) => (
                o === rec || o?.element === rec?.element
                || String(o?.id || '').toLowerCase() === String(rec?.id || '').toLowerCase()
            )) || rec;
        }
        if (targetOverlay?.element) {
            targetOverlay.element.style.transform = 'scale(1.05)';
            targetOverlay.element.style.zIndex = '100';
            
            // 闪烁效果
            const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true;
            targetOverlay.element.style.animation = reduceMotion ? 'none' : 'overlay-pulse 1s 2';
        }
    }
    
    /**
     * 清除所有ROI标注框
     */
    clearAllROIOverlays() {
        this._roiOverlaySyncGen = (this._roiOverlaySyncGen || 0) + 1;
        if (!this.viewer) {
            this.roiOverlays = [];
            this.roiAnalysisData = {};
            return;
        }
        
        this.roiOverlays.forEach(overlay => {
            this.viewer.removeOverlay(overlay.element);
        });
        
        this.roiOverlays = [];
        this.roiAnalysisData = {};
    }
    
    /**
     * 存储ROI分析数据
     */
    storeROIAnalysisData(roiId, analysisData) {
        this.roiAnalysisData[roiId] = analysisData;
    }
    
    /**
     * 设置标签页切换功能
     */
    setupTabSwitching() {
        const tabButtons = document.querySelectorAll('.tab-btn');
        const tabPanes = document.querySelectorAll('.tab-pane');
        
        tabButtons.forEach(button => {
            button.addEventListener('click', () => {
                const targetTab = button.dataset.tab;
                
                // 更新按钮状态
                tabButtons.forEach(btn => btn.classList.remove('active'));
                button.classList.add('active');
                
                // 更新标签页内容
                tabPanes.forEach(pane => {
                    pane.classList.remove('active');
                    if (pane.id === targetTab + 'Tab') {
                        pane.classList.add('active');
                    }
                });
                
                // 如果切换到Agent标签页，初始化Agent相关功能
                if (targetTab === 'agent') {
                    const waitForAgent = () => {
                        if (window.pathChatAgent && typeof window.pathChatAgent.setViewer === 'function') {
                            try {
                                window.pathChatAgent.setViewer(this.viewer);
                                if (this.currentFile) {
                                    window.pathChatAgent.setCurrentFile(this.currentFile.file_id);
                                }
                                console.log('Agent标签页集成成功');
                            } catch (error) {
                                console.error('Agent标签页集成失败:', error);
                            }
                        } else {
                            setTimeout(waitForAgent, 200);
                        }
                    };
                    setTimeout(waitForAgent, 100);
                }
            });
        });
    }
    
    /**
     * 打开树状图模态窗口
     */
    openTreeModal() {
        console.log('🌳 打开树状图模态窗口');

        // 分析进行中时不允许打开树状图（会显示旧缓存数据）
        if (window.pathChatAgent && window.pathChatAgent.isRunning) {
            const isEn = (window.__lang || 'zh') === 'en';
            this.showNotification('warning',
                isEn ? 'Analysis in progress. Tree view will be available after completion.'
                     : '分析正在进行中，完成后即可查看树状图');
            return;
        }
        
        const modal = document.getElementById('treeModal');
        if (!modal) {
            console.error('❌ 未找到树状图模态窗口');
            return;
        }
        
        modal.style.display = 'block';
        modal.classList.add('active');
        this._treeReturnFocus = document.activeElement;
        
        const currentId = this.currentPatientId || [
            this.currentFile?.file_id || '',
            window.pathChatAgent?.currentOutputDirName || ''
        ].join(':');
        const alreadyLoaded = this._treeLoadedForId === currentId
            && this.analysisTreeData
            && document.getElementById('treeSvg');
        
        if (!alreadyLoaded) {
            this.loadTreeData();
            this._treeLoadedForId = currentId;
        } else {
            console.log('🌳 树状图已缓存，跳过重新加载');
        }
        
        this.setupTreeModalEvents();
        requestAnimationFrame(() => {
            document.getElementById('treeModalClose')?.focus({ preventScroll: true });
        });
    }
    
    /**
     * 关闭树状图模态窗口
     */
    closeTreeModal() {
        const modal = document.getElementById('treeModal');
        if (modal) {
            modal.classList.remove('active');
            setTimeout(() => {
                modal.style.display = 'none';
            }, 300);
            if (this._treeReturnFocus instanceof HTMLElement) {
                this._treeReturnFocus.focus({ preventScroll: true });
            }
        }
    }
    
    /**
     * 设置树状图模态窗口事件
     */
    setupTreeModalEvents() {
        // 关闭按钮
        const closeBtn = document.getElementById('treeModalClose');
        if (closeBtn) {
            closeBtn.onclick = () => this.closeTreeModal();
        }
        
        // 点击遮罩层关闭
        const overlay = document.getElementById('treeModalOverlay');
        if (overlay) {
            overlay.onclick = () => this.closeTreeModal();
        }
        
        // ESC键关闭
        const handleEscape = (e) => {
            if (e.key === 'Escape') {
                this.closeTreeModal();
                document.removeEventListener('keydown', handleEscape);
            }
        };
        document.addEventListener('keydown', handleEscape);
        
        // 树状图控制按钮
        const zoomInBtn = document.getElementById('treeZoomIn');
        const zoomOutBtn = document.getElementById('treeZoomOut');
        const resetBtn = document.getElementById('treeReset');
        const exportBtn = document.getElementById('treeExport');
        
        if (zoomInBtn) zoomInBtn.onclick = () => this.treeZoom(1.2);
        if (zoomOutBtn) zoomOutBtn.onclick = () => this.treeZoom(0.8);
        if (resetBtn) resetBtn.onclick = () => this.treeReset();
        if (exportBtn) exportBtn.onclick = () => this.exportTreeImage();
        
        // ⭐ 初始化分隔条拖拽
        this.initTreeResizer();
        
        // 初始化节点详情面板（显示默认提示）
        this.resetTreeNodeDetails();
    }
    
    /**
     * ⭐ 初始化树状图分隔条拖拽功能
     */
    initTreeResizer() {
        const resizer = document.getElementById('treeResizer');
        const leftPanel = document.querySelector('.tree-structure-panel');
        
        if (!resizer || !leftPanel) return;
        
        let isResizing = false;
        let startX = 0;
        let startWidth = 0;
        
        resizer.addEventListener('mousedown', (e) => {
            isResizing = true;
            startX = e.clientX;
            startWidth = leftPanel.offsetWidth;
            resizer.classList.add('dragging');
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        });
        
        document.addEventListener('mousemove', (e) => {
            if (!isResizing) return;
            
            const diff = e.clientX - startX;
            const newWidth = Math.max(200, Math.min(500, startWidth + diff));
            leftPanel.style.width = newWidth + 'px';
        });
        
        document.addEventListener('mouseup', () => {
            if (isResizing) {
                isResizing = false;
                resizer.classList.remove('dragging');
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
            }
        });
    }
    
    /**
     * 重置节点详情面板
     */
    resetTreeNodeDetails() {
        const panel = document.getElementById('pathDetailsPanel');
        const content = document.getElementById('pathDetailsContent');
        
        if (panel) {
            panel.style.display = 'none';
        }
        
        if (content) {
            content.innerHTML = '';
        }
    }
    
    /**
     * 加载树状图数据
     * ⭐ 优先从缓存的 analysis_tree.json 加载，无需重新分析
     */
    async loadTreeData() {
        console.log('📊 加载树状图数据');
        
        // ⭐ Patient Level 模式：优先从患者级别 API 加载完整树（左侧面板 + 右侧可视化）
        if (this.currentPatientId) {
            const patientUrl = this.patientTreeUrl || `/api/agent/patient-analysis-tree/${this.currentPatientId}`;
            try {
                console.log('🔍 [Patient Level] 尝试加载患者级别分析树:', patientUrl);
                const response = await fetch(patientUrl);
                if (response.ok) {
                    const result = await response.json();
                    if (result.success && result.data && result.data.tree_nodes && result.data.tree_nodes.length > 0) {
                        console.log('✅ [Patient Level] 成功加载患者级别分析树！WSI数:', result.data.tree_nodes.length);
                        if (!this.patientTreeUrl) this.patientTreeUrl = patientUrl;
                        this.analysisTreeData = result.data;
                        
                        const treeData = this.buildTreeDataFromCachePatientLevel(result.data);
                        this.updateTreeStats(treeData);
                        this.renderTreeStructure(treeData);
                        this.renderTreeVisualization(treeData);
                        
                        this.showNotification('success', (window.__lang||'zh')==='en' ? 'Patient-level analysis tree loaded' : '成功加载患者级别分析树');
                        return;
                    }
                }
            } catch (e) {
                console.warn('⚠️ [Patient Level] 加载失败，回退到单张模式:', e);
            }
        }
        
        // ⭐ 单张 WSI 模式：从缓存的分析树加载
        if (this.currentFile && this.currentFile.file_id) {
            try {
                console.log('🔍 尝试从缓存加载分析树:', this.currentFile.file_id);
                const response = await fetch(this._analysisTreeRequestUrl());
                if (response.ok) {
                    const result = await response.json();
                    if (result.success && result.data && result.data.tree_nodes && result.data.tree_nodes.length > 0) {
                        console.log('✅ 成功从缓存加载分析树！节点数:', result.data.tree_nodes.length);
                        this.analysisTreeData = result.data;
                        
                        const treeData = this.buildTreeDataFromCache(result.data);
                        this.updateTreeStats(treeData);
                        this.renderTreeStructure(treeData);
                        this.renderTreeVisualization(treeData);
                        
                        this.showNotification('success', (window.__lang||'zh')==='en' ? 'Analysis results loaded from cache' : '成功加载缓存的分析结果');
                        return;
                    }
                }
            } catch (e) {
                console.warn('⚠️ 从缓存加载失败:', e);
            }
        }
        
        // Fallback: 检查是否有ROI数据
        if (this.roiOverlays.length === 0) {
            console.warn('⚠️ 没有ROI数据，也没有缓存');
            this.showTreeEmptyState('请先完成WSI分析，或选择已分析过的文件');
            return;
        }
        
        // 从当前ROI数据构建
        const treeData = this.buildTreeData();
        
        // 更新统计信息
        this.updateTreeStats(treeData);
        
        // 渲染左侧层级结构
        this.renderTreeStructure(treeData);
        
        // 渲染右侧可视化树图
        this.renderTreeVisualization(treeData);
    }

    _analysisTreeRequestUrl() {
        const fileId = this.currentFile?.file_id || '';
        const outputDir = window.pathChatAgent?.currentOutputDirName || '';
        if (!outputDir) return `/api/agent/analysis-tree/${fileId}`;
        const strategy = window.pathChatAgent?.activeAnalysisStrategy || '';
        const mode = strategy === 'single_agent_roam'
            ? 'single_agent_roam'
            : (window.pathChatAgent?.isCollaborativeMode ? 'collaborative' : 'ai');
        const params = new URLSearchParams({ output_dir: outputDir, mode });
        return `/api/agent/analysis-tree/${fileId}?${params.toString()}`;
    }
    
    /**
     * ⭐ 从缓存的 analysis_tree.json 构建显示用的 treeData
     */
    buildTreeDataFromCache(cacheData) {
        const treeData = {
            levels: {
                '10x': { magnification: '10', rois: [] },
                '40x': { magnification: '40', rois: [] },
                '160x': { magnification: '160', rois: [] }
            },
            totalROIs: 0
        };
        
        const treeNodes = cacheData.tree_nodes || [];
        
        treeNodes.forEach(redNode => {
            // 红框
            treeData.levels['10x'].rois.push({
                id: redNode.roi_id,
                label: redNode.roi_id,
                magnification: '10',
                confidence: redNode.analysis?.confidence || 0,
                analysis: redNode.analysis,
                rect: redNode.rect
            });
            treeData.totalROIs++;
            
            // 蓝框
            (redNode.children || []).forEach(blueNode => {
                treeData.levels['40x'].rois.push({
                    id: blueNode.roi_id,
                    label: blueNode.roi_id,
                    magnification: '40',
                    confidence: blueNode.analysis?.confidence || 0,
                    analysis: blueNode.analysis,
                    rect: blueNode.rect
                });
                treeData.totalROIs++;
                
                // 绿框
                (blueNode.children || []).forEach(greenNode => {
                    treeData.levels['160x'].rois.push({
                        id: greenNode.roi_id,
                        label: greenNode.roi_id,
                        magnification: '160',
                        confidence: greenNode.analysis?.confidence || 0,
                        analysis: greenNode.analysis,
                        rect: greenNode.rect
                    });
                    treeData.totalROIs++;
                });
            });
        });
        
        console.log('📊 从缓存构建treeData:', treeData);
        return treeData;
    }
    
    /**
     * ⭐ 从患者级别分析树 JSON 构建左侧面板用的 treeData
     * Patient Level tree_nodes 结构: [{level:'wsi', children:[{红框, children:[{蓝框, children:[绿框]}]}]}]
     */
    buildTreeDataFromCachePatientLevel(cacheData) {
        const treeData = {
            levels: {
                '10x': { magnification: '10', rois: [] },
                '40x': { magnification: '40', rois: [] },
                '160x': { magnification: '160', rois: [] }
            },
            totalROIs: 0
        };
        
        const wsiNodes = cacheData.tree_nodes || [];
        
        wsiNodes.forEach(wsiNode => {
            (wsiNode.children || []).forEach(redNode => {
                treeData.levels['10x'].rois.push({
                    id: redNode.roi_id,
                    label: redNode.roi_id,
                    magnification: '10',
                    confidence: redNode.analysis?.confidence || 0,
                    analysis: redNode.analysis,
                    rect: redNode.rect
                });
                treeData.totalROIs++;
                
                (redNode.children || []).forEach(blueNode => {
                    treeData.levels['40x'].rois.push({
                        id: blueNode.roi_id,
                        label: blueNode.roi_id,
                        magnification: '40',
                        confidence: blueNode.analysis?.confidence || 0,
                        analysis: blueNode.analysis,
                        rect: blueNode.rect
                    });
                    treeData.totalROIs++;
                    
                    (blueNode.children || []).forEach(greenNode => {
                        treeData.levels['160x'].rois.push({
                            id: greenNode.roi_id,
                            label: greenNode.roi_id,
                            magnification: '160',
                            confidence: greenNode.analysis?.confidence || 0,
                            analysis: greenNode.analysis,
                            rect: greenNode.rect
                        });
                        treeData.totalROIs++;
                    });
                });
            });
        });
        
        console.log('📊 [Patient Level] 从患者级别树构建treeData:', treeData);
        return treeData;
    }
    
    /**
     * 构建树状结构数据
     */
    buildTreeData() {
        const treeData = {
            levels: {},
            totalROIs: 0
        };
        
        console.log('📊 开始构建树状数据');
        console.log('📊 当前ROI Overlays数量:', this.roiOverlays.length);
        console.log('📊 当前roiAnalysisData:', this.roiAnalysisData);
        
        // 按放大倍数分组ROI
        this.roiOverlays.forEach(overlay => {
            const mag = overlay.data.magnification || '10';
            const magKey = `${mag}x`;
            
            if (!treeData.levels[magKey]) {
                treeData.levels[magKey] = {
                    magnification: mag,
                    rois: []
                };
            }
            
            const analysisData = this.roiAnalysisData[overlay.data.id];
            console.log(`📊 ROI ${overlay.data.id} 的分析数据:`, analysisData);
            
            treeData.levels[magKey].rois.push({
                id: overlay.data.id,
                label: overlay.data.label || `ROI ${overlay.data.id}`,
                rect: overlay.data.rect,
                magnification: mag,
                confidence: overlay.data.confidence,
                analysis: analysisData
            });
            
            treeData.totalROIs++;
        });
        
        console.log('📊 构建完成，树状数据:', treeData);
        return treeData;
    }
    
    /**
     * 更新树状图统计信息
     */
    updateTreeStats(treeData) {
        const totalLayers = Object.keys(treeData.levels).length;
        const totalROIs = treeData.totalROIs;
        
        document.getElementById('totalLayers').textContent = totalLayers;
        document.getElementById('totalROIs').textContent = totalROIs;
    }
    
    /**
     * 渲染左侧层级结构
     */
    renderTreeStructure(treeData) {
        const container = document.getElementById('treeStructureContent');
        if (!container) return;
        
        // 清空容器
        container.innerHTML = '';
        
        // 按放大倍数排序
        const sortedLevels = Object.entries(treeData.levels).sort((a, b) => {
            return parseInt(a[1].magnification) - parseInt(b[1].magnification);
        });
        
        // 渲染每个层级
        sortedLevels.forEach(([magKey, level]) => {
            const levelElement = this.createTreeLevelElement(magKey, level);
            container.appendChild(levelElement);
        });
    }
    
    /**
     * 创建树状层级元素
     */
    createTreeLevelElement(magKey, level) {
        const div = document.createElement('div');
        div.className = 'tree-item';
        
        const levelClass = `level-${magKey.replace('x', '')}`;
        const levelName = this.getMagnificationLabel(level.magnification);
        
        div.innerHTML = `
            <div class="tree-item-header ${levelClass}">
                <div class="tree-item-title">
                    <i class="fas fa-layer-group"></i>
                    <span>${levelName}</span>
                    <span class="tree-item-badge">${level.rois.length} ROIs</span>
                </div>
                <i class="fas fa-chevron-down tree-item-toggle"></i>
            </div>
            <div class="tree-item-children"></div>
        `;
        
        // 添加子项
        const childrenContainer = div.querySelector('.tree-item-children');
        level.rois.forEach(roi => {
            const childElement = this.createTreeChildElement(roi);
            childrenContainer.appendChild(childElement);
        });
        
        // 添加展开/折叠事件
        const header = div.querySelector('.tree-item-header');
        const toggle = div.querySelector('.tree-item-toggle');
        header.setAttribute('role', 'button');
        header.setAttribute('tabindex', '0');
        header.setAttribute('aria-expanded', 'false');
        const toggleLevel = () => {
            childrenContainer.classList.toggle('expanded');
            toggle.classList.toggle('expanded');
            header.setAttribute(
                'aria-expanded',
                String(childrenContainer.classList.contains('expanded'))
            );
        };
        header.addEventListener('click', toggleLevel);
        header.addEventListener('keydown', event => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                toggleLevel();
            }
        });
        
        return div;
    }
    
    /**
     * 创建树状子项元素
     */
    createTreeChildElement(roi) {
        const div = document.createElement('div');
        div.className = 'tree-child-item';
        div.dataset.roiId = roi.id;
        div.setAttribute('role', 'button');
        div.setAttribute('tabindex', '0');
        
        const confidence = roi.confidence ? Math.round(roi.confidence * 100) : 'N/A';
        const description = roi.analysis?.gpt_analysis ? 
            roi.analysis.gpt_analysis.substring(0, 100) + '...' : 
            '暂无分析数据';
        
        div.innerHTML = `
            <div class="tree-child-info">
                <span class="tree-child-label">${roi.label}</span>
            </div>
            <div class="tree-child-description">${description}</div>
        `;
        
        // 点击子项时跳转到对应ROI并显示详情
        const activateChild = () => {
            this.jumpToROI(roi);
            this.showPathDetails(roi);
            
            // 高亮选中项
            document.querySelectorAll('.tree-child-item').forEach(item => {
                item.classList.remove('active');
            });
            div.classList.add('active');
        };
        div.addEventListener('click', activateChild);
        div.addEventListener('keydown', event => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                activateChild();
            }
        });
        
        return div;
    }
    
    /**
     * 跳转到ROI位置
     */
    jumpToROI(roi) {
        if (!this.viewer || !roi.rect) return;
        
        const rect = roi.rect;
        const viewportRect = this.viewer.viewport.imageToViewportRectangle(
            rect.x, rect.y, rect.w, rect.h
        );
        
        const padding = 0.2;
        const paddedRect = new OpenSeadragon.Rect(
            viewportRect.x - viewportRect.width * padding,
            viewportRect.y - viewportRect.height * padding,
            viewportRect.width * (1 + 2 * padding),
            viewportRect.height * (1 + 2 * padding)
        );
        
        this.viewer.viewport.fitBounds(paddedRect, true);
        
        // 高亮标注框
        this.highlightROI(roi.id);
    }
    
    /**
     * 显示路径详情（兼容新布局）
     * 将旧的ROI数据转换为新的node格式，调用showTreeNodeDetails
     */
    showPathDetails(roi) {
        // 将旧格式转换为新格式
        const node = {
            id: roi.label || roi.id,
            label: roi.label,
            magnification: String(roi.magnification),
            rect: roi.rect,
            image_path: roi.image_path,
            analysis: {
                ...roi.analysis,
                confidence: roi.confidence ? Math.round(roi.confidence * 100) : 0,
                reasoning: roi.analysis?.gpt_analysis || roi.analysis?.findings || '',
                pathology_type: roi.analysis?.pathology_type || 'uncertain'
            }
        };
        
        // 确定节点类型
        node.nodeType = roi.magnification === 10 ? 'red' : roi.magnification === 40 ? 'blue' : 'green';
        
        // 调用新的详情显示方法
        this.showTreeNodeDetails(node);
    }
    
    /**
     * 渲染右侧可视化树图
     * ⭐ Phase 2.1: 优先从 analysis_tree.json API 获取数据
     */
    async renderTreeVisualization(treeData) {
        console.log('🎨 开始渲染树状图可视化');
        
        const container = document.getElementById('treeVisualizationContent');
        if (!container) return;
        
        // ⭐ Patient Level 模式：优先使用 patient-analysis-tree API
        if (this.currentPatientId) {
            const patientUrl = this.patientTreeUrl || `/api/agent/patient-analysis-tree/${this.currentPatientId}`;
            try {
                const response = await fetch(patientUrl);
                if (response.ok) {
                    const result = await response.json();
                    if (result.success && result.data) {
                        console.log('✅ 从 patient-analysis-tree 加载患者级别树数据');
                        if (!this.patientTreeUrl) this.patientTreeUrl = patientUrl;
                        this.analysisTreeData = result.data;
                        this.renderTreeFromJson(container, result.data);
                        return;
                    }
                }
            } catch (e) {
                console.warn('⚠️ 无法从Patient Level API获取分析树:', e);
            }
        }
        
        // ⭐ 单张模式：从 analysis_tree API 获取
        if (this.currentFile && this.currentFile.file_id) {
            try {
                const response = await fetch(this._analysisTreeRequestUrl());
                if (response.ok) {
                    const result = await response.json();
                    if (result.success && result.data) {
                        console.log('✅ 从 analysis_tree.json 加载树数据');
                        console.log('📋 metadata:', result.data.metadata);
                        console.log('📋 global_analysis 长度:', result.data.metadata?.global_analysis?.length || 0);
                        console.log('📋 thumbnail_url:', result.data.metadata?.thumbnail_url || '无');
                        this.analysisTreeData = result.data;
                        this.renderTreeFromJson(container, result.data);
                        return;
                    }
                }
            } catch (e) {
                console.warn('⚠️ 无法从API获取分析树，使用降级方案:', e);
            }
        }
        
        // 降级方案：使用原有的 DOM 提取逻辑
        console.log('📦 使用降级方案渲染树状图');
        this.renderTreeFromDOM(container, treeData);
    }
    
    /**
     * ⭐ Phase 2.1: 从 analysis_tree.json 渲染树
     */
    renderTreeFromJson(container, treeJson) {
        // 弹窗刚打开时布局可能尚未完成，此刻 clientWidth/Height 为 0，
        // 直接渲染会把整棵树画进 0×0 的 SVG（表现为右侧空白）。
        // 所以量到 0 就等下一帧重试，超时后用兜底尺寸保证可见。
        const token = (this._treeVizRenderToken = (this._treeVizRenderToken || 0) + 1);
        const attemptRender = (triesLeft) => {
            if (token !== this._treeVizRenderToken) return;
            let width = container.clientWidth;
            let height = container.clientHeight;
            if ((!width || !height) && triesLeft > 0) {
                requestAnimationFrame(() => attemptRender(triesLeft - 1));
                return;
            }
            if (!width) width = 800;
            if (!height) height = 560;

            // 清空容器
            container.innerHTML = '<svg id="treeSvg"></svg>';

            const svg = document.getElementById('treeSvg');
            if (!svg) return;

            svg.setAttribute('width', width);
            svg.setAttribute('height', height);
            svg.setAttribute('viewBox', `0 0 ${width} ${height}`);

            // 从 JSON 构建树状数据结构
            const treeStructure = this.buildTreeFromJsonData(treeJson);

            // 渲染树状图
            this.drawTree(svg, treeStructure, width, height);

            // 保存当前缩放状态
            this.treeZoomLevel = 1;
            this.treePanX = 0;
            this.treePanY = 0;

            // 添加拖拽和缩放功能
            this.addTreeInteractivity(svg);
        };
        attemptRender(20);
    }
    
    /**
     * ⭐⭐⭐ 单张 WSI 模式：完全复制 V6 版本代码（经过验证的稳定版本）⭐⭐⭐
     * 这个函数专门用于单张 WSI 的树状图构建，与 Patient Level 完全隔离
     */
    buildTreeFromJsonDataSingleWSI(treeJson) {
        const metadata = treeJson.metadata || {};
        const treeNodes = treeJson.tree_nodes || [];
        
        // 创建根节点
        const root = {
            id: 'root',
            label: __t('label_thumbnail'),
            level: 0,
            magnification: __t('label_panorama'),
            children: []
        };
        
        // 遍历红框节点
        treeNodes.forEach(redNode => {
            const redChild = {
                id: redNode.roi_id,
                label: redNode.roi_id,
                magnification: '10',
                level: 1,
                confidence: redNode.analysis?.confidence || 0,
                rect: redNode.rect,
                image_path: redNode.image_path,
                image_url: redNode.image_url,
                analysis: redNode.analysis,
                nodeType: 'red',
                children: []
            };
            
            // 遍历蓝框节点
            (redNode.children || []).forEach(blueNode => {
                const blueChild = {
                    id: blueNode.roi_id,
                    label: blueNode.roi_id,
                    magnification: '40',
                    level: 2,
                    confidence: blueNode.analysis?.confidence || 0,
                    rect: blueNode.rect,
                    image_path: blueNode.image_path,
                    image_url: blueNode.image_url,
                    analysis: blueNode.analysis,
                    nodeType: 'blue',
                    children: []
                };
                
                // 遍历绿框节点
                (blueNode.children || []).forEach(greenNode => {
                    const greenChild = {
                        id: greenNode.roi_id,
                        label: greenNode.roi_id,
                        magnification: '160',
                        level: 3,
                        confidence: greenNode.analysis?.confidence || 0,
                        rect: greenNode.rect,
                        image_path: greenNode.image_path,
                        image_url: greenNode.image_url,
                        analysis: greenNode.analysis,
                        nodeType: 'green',
                        children: []
                    };
                    blueChild.children.push(greenChild);
                });
                
                redChild.children.push(blueChild);
            });
            
            root.children.push(redChild);
        });
        
        return root;
    }
    
    /**
     * ⭐⭐⭐ Patient Level 模式：专门用于多张 WSI 的树状图构建 ⭐⭐⭐
     */
    buildTreeFromJsonDataPatientLevel(treeJson) {
        const metadata = treeJson.metadata || {};
        const treeNodes = treeJson.tree_nodes || [];
        
        // 创建根节点
        const root = {
            id: 'root',
            label: 'Patient',
            level: 0,
            magnification: __t('label_panorama'),
            children: []
        };
        
        // Patient Level 模式：WSI → 红框 → 蓝框 → 绿框
        treeNodes.forEach((wsiNode, wsiIdx) => {
            const wsiChild = {
                id: wsiNode.roi_id || `Slide ${wsiIdx + 1}`,
                label: wsiNode.roi_id || `Slide ${wsiIdx + 1}`,
                level: 1,
                magnification: __t('slide_info'),
                nodeType: 'wsi',
                filename: wsiNode.filename || '',
                part_name: wsiNode.part_name || '',
                file_id: wsiNode.file_id || '',
                thumbnail_url: wsiNode.thumbnail_url || '',
                analysis: wsiNode.analysis,
                children: []
            };
            
            const wsiFileId = wsiNode.file_id || '';
            
            // 遍历该 WSI 下的红框节点
            (wsiNode.children || []).forEach(redNode => {
                const redChild = {
                    id: redNode.roi_id,
                    label: redNode.roi_id,
                    magnification: '10',
                    level: 2,
                    confidence: redNode.analysis?.confidence || 0,
                    rect: redNode.rect,
                    image_path: redNode.image_path,
                    image_url: redNode.image_url,
                    analysis: redNode.analysis,
                    nodeType: 'red',
                    _wsi_file_id: wsiFileId,
                    children: []
                };
                
                // 遍历蓝框节点
                (redNode.children || []).forEach(blueNode => {
                    const blueChild = {
                        id: blueNode.roi_id,
                        label: blueNode.roi_id,
                        magnification: '40',
                        level: 3,
                        confidence: blueNode.analysis?.confidence || 0,
                        rect: blueNode.rect,
                        image_path: blueNode.image_path,
                        image_url: blueNode.image_url,
                        analysis: blueNode.analysis,
                        nodeType: 'blue',
                        _wsi_file_id: wsiFileId,
                        children: []
                    };
                    
                    // 遍历绿框节点
                    (blueNode.children || []).forEach(greenNode => {
                        const greenChild = {
                            id: greenNode.roi_id,
                            label: greenNode.roi_id,
                            magnification: '160',
                            level: 4,
                            confidence: greenNode.analysis?.confidence || 0,
                            rect: greenNode.rect,
                            image_path: greenNode.image_path,
                            image_url: greenNode.image_url,
                            analysis: greenNode.analysis,
                            nodeType: 'green',
                            _wsi_file_id: wsiFileId,
                            children: []
                        };
                        blueChild.children.push(greenChild);
                    });
                    
                    redChild.children.push(blueChild);
                });
                
                wsiChild.children.push(redChild);
            });
            
            root.children.push(wsiChild);
        });
        
        return root;
    }
    
    /**
     * ⭐ 统一入口：根据当前模式选择对应的树构建函数
     */
    buildTreeFromJsonData(treeJson) {
        const treeNodes = treeJson.tree_nodes || [];
        
        // ⭐ 检测是否为 Patient Level 树结构（第一层是 WSI 节点）
        const isPatientLevel = treeNodes.length > 0 && treeNodes[0].level === 'wsi';
        
        if (isPatientLevel) {
            console.log('🌳 [树构建] 使用 Patient Level 模式');
            return this.buildTreeFromJsonDataPatientLevel(treeJson);
        } else {
            console.log('🌳 [树构建] 使用单张 WSI 模式（V6 版本）');
            return this.buildTreeFromJsonDataSingleWSI(treeJson);
        }
    }
    
    /**
     * ⭐ Phase 2.1: 降级方案 - 从 DOM 渲染树（原有逻辑）
     */
    renderTreeFromDOM(container, treeData) {
        // 清空容器
        container.innerHTML = '<svg id="treeSvg"></svg>';
        
        const svg = document.getElementById('treeSvg');
        if (!svg) return;
        
        // 获取容器尺寸（弹窗未完成布局时量到 0，用兜底尺寸避免空白树图）
        const width = container.clientWidth || 800;
        const height = container.clientHeight || 560;
        
        svg.setAttribute('width', width);
        svg.setAttribute('height', height);
        svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
        
        // 构建树状数据结构（原有逻辑）
        const treeStructure = this.buildTreeHierarchy(treeData);
        
        // 渲染树状图
        this.drawTree(svg, treeStructure, width, height);
        
        // 保存当前缩放状态
        this.treeZoomLevel = 1;
        this.treePanX = 0;
        this.treePanY = 0;
        
        // 添加拖拽和缩放功能
        this.addTreeInteractivity(svg);
    }
    
    /**
     * 构建树状层级结构
     */
    buildTreeHierarchy(treeData) {
        // 创建根节点
        const root = {
            id: 'root',
            label: __t('label_thumbnail'),
            level: 0,
            magnification: __t('label_panorama'),
            children: []
        };
        
        // 按放大倍数排序
        const sortedLevels = Object.entries(treeData.levels).sort((a, b) => {
            return parseInt(a[1].magnification) - parseInt(b[1].magnification);
        });
        
        // 构建层级结构
        sortedLevels.forEach(([magKey, level]) => {
            level.rois.forEach(roi => {
                const node = {
                    id: roi.id,
                    label: roi.label,
                    magnification: roi.magnification,
                    confidence: roi.confidence,
                    rect: roi.rect,
                    level: parseInt(roi.magnification),
                    children: [],
                    analysis: roi.analysis
                };
                
                // 根据放大倍数决定层级关系
                if (parseInt(roi.magnification) <= 10) {
                    // 低倍镜直接挂在根节点下
                    root.children.push(node);
                } else {
                    // 高倍镜挂在低倍镜节点下（简化处理）
                    if (root.children.length > 0) {
                        // 找到最近的父节点
                        const parent = root.children[root.children.length - 1];
                        if (!parent.children) parent.children = [];
                        parent.children.push(node);
                    } else {
                        root.children.push(node);
                    }
                }
            });
        });
        
        return root;
    }
    
    /**
     * 绘制树状图
     */
    drawTree(svg, root, width, height) {
        // 清空SVG
        svg.innerHTML = '';
        
        // 创建分组
        const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        g.setAttribute('id', 'treeGroup');
        svg.appendChild(g);
        
        // 计算树状布局
        const layout = this.calculateTreeLayout(root, width, height);
        
        // 绘制连线
        this.drawTreeLinks(g, layout.links);
        
        // 绘制节点
        this.drawTreeNodes(g, layout.nodes);
        
        console.log('✅ 树状图渲染完成');
    }
    
    /**
     * ⭐ 辅助函数：检测树结构中是否存在 level 4 的节点（Patient Level 模式特征）
     */
    hasLevel4Nodes(node, currentLevel = 0) {
        if (currentLevel >= 4) return true;
        if (!node.children || node.children.length === 0) return false;
        return node.children.some(child => this.hasLevel4Nodes(child, currentLevel + 1));
    }
    
    /**
     * 计算树状布局
     * ⭐ 根据模式自动选择配置：单张WSI使用V6版本配置，Patient Level使用扩展配置
     */
    calculateTreeLayout(root, width, height) {
        const nodes = [];
        const links = [];
        
        // ⭐ 检测是否为 Patient Level 模式（根节点标签为"患者"或存在level=4的节点）
        const isPatientLevel = root.label === '患者' || root.label === 'Patient' || this.hasLevel4Nodes(root);
        
        // ⭐⭐⭐ 根据模式选择不同的配置 ⭐⭐⭐
        let nodeSizeByLevel, defaultNodeSize;
        
        if (isPatientLevel) {
            // Patient Level 模式：WSI(level 1), 红框(level 2), 蓝框(level 3), 绿框(level 4)
            nodeSizeByLevel = {
                0: 70,   // 根节点（患者）
                1: 65,   // WSI - 大
                2: 50,   // 红框 - 中
                3: 40,   // 蓝框 - 小
                4: 35    // 绿框 - 最小
            };
            defaultNodeSize = 35;
            console.log('🌳 [树布局] 使用 Patient Level 配置');
        } else {
            // ⭐ 单张 WSI 模式：完全使用 V6 版本的配置（经过验证的稳定版本）
            nodeSizeByLevel = {
                0: 70,   // 根节点（缩略图）
                1: 65,   // 红框 - 大
                2: 50,   // 蓝框 - 中
                3: 40    // 绿框 - 小
            };
            defaultNodeSize = 40;  // ⭐ V6 版本使用 40
            console.log('🌳 [树布局] 使用单张 WSI 配置（V6 版本）');
        }
        
        const levelHeight = 110; // 层级间距
        const minHorizontalSpacing = 5;
        
        // 计算每层的节点数量和深度
        const calculateTreeMetrics = (node, level = 0, metrics = { maxLevel: 0, levelCounts: {} }) => {
            if (level > metrics.maxLevel) metrics.maxLevel = level;
            if (!metrics.levelCounts[level]) metrics.levelCounts[level] = 0;
            metrics.levelCounts[level]++;
            
            if (node.children && node.children.length > 0) {
                node.children.forEach(child => {
                    calculateTreeMetrics(child, level + 1, metrics);
                });
            }
            
            return metrics;
        };
        
        const metrics = calculateTreeMetrics(root);
        const maxNodesInLevel = Math.max(...Object.values(metrics.levelCounts));
        
        // ⭐ 根据最底层节点数量计算缩放比例（绿框最多，用最小尺寸计算）
        const smallestNodeSize = nodeSizeByLevel[3] || defaultNodeSize;
        const availableWidth = width - 100;
        const requiredWidth = maxNodesInLevel * smallestNodeSize + (maxNodesInLevel - 1) * minHorizontalSpacing;
        const scale = requiredWidth > availableWidth ? availableWidth / requiredWidth : 1;
        const actualSpacing = minHorizontalSpacing * scale;
        
        // ⭐ 根据层级获取节点尺寸（已缩放）
        const getNodeSize = (level) => {
            const baseSize = nodeSizeByLevel[level] || defaultNodeSize;
            return baseSize * scale;
        };
        
        // 计算节点占用的总宽度（用于居中对齐）
        const calculateSubtreeWidth = (node, level = 0) => {
            const nodeSize = getNodeSize(level);
            if (!node.children || node.children.length === 0) {
                return nodeSize;
            }
            
            const childrenWidth = node.children.reduce((sum, child) => {
                return sum + calculateSubtreeWidth(child, level + 1);
            }, 0);
            
            const spacingWidth = (node.children.length - 1) * actualSpacing;
            return Math.max(nodeSize, childrenWidth + spacingWidth);
        };
        
        // 布局节点（改进算法，确保子节点居中对齐父节点）
        const layoutNode = (node, level = 0, parentX = width / 2, parentY = 50, parentSize = 0, leftBound = 0, rightBound = width) => {
            // ⭐ 获取当前层级的节点尺寸
            const nodeSize = getNodeSize(level);
            
            // 计算当前节点位置
            let x, y;
            
            if (level === 0) {
                // 根节点居中
                x = width / 2;
                y = 60;
            } else {
                // 子节点在父节点正下方，根据兄弟节点数量调整
                x = (leftBound + rightBound) / 2;
                y = parentY + levelHeight;
            }
            
            // 添加节点
            const nodeData = {
                id: node.id,
                label: node.label,
                magnification: node.magnification,
                confidence: node.confidence,
                rect: node.rect,
                level: level,
                x: x,
                y: y,
                width: nodeSize,   // ⭐ 使用层级对应的尺寸
                height: nodeSize,  // ⭐ 正方形
                analysis: node.analysis
            };
            
            nodes.push(nodeData);
            
            // 如果有父节点，添加连线
            if (level > 0) {
                links.push({
                    source: { x: parentX, y: parentY + parentSize },  // ⭐ 使用父节点的尺寸
                    target: { x: x, y: y }
                });
            }
            
            // 递归处理子节点
            if (node.children && node.children.length > 0) {
                const childCount = node.children.length;
                // ⭐ 传递子节点层级来计算子树宽度
                const subtreeWidths = node.children.map(child => calculateSubtreeWidth(child, level + 1));
                const totalSubtreeWidth = subtreeWidths.reduce((a, b) => a + b, 0) + 
                                         (childCount - 1) * actualSpacing;
                
                // 子树起始位置（相对于当前节点居中）
                let currentX = x - totalSubtreeWidth / 2;
                
                node.children.forEach((child, index) => {
                    const childWidth = subtreeWidths[index];
                    const childLeft = currentX;
                    const childRight = currentX + childWidth;
                    
                    // ⭐ 传递当前节点尺寸作为 parentSize
                    layoutNode(child, level + 1, x, y, nodeSize, childLeft, childRight);
                    
                    currentX += childWidth + actualSpacing;
                });
            }
        };
        
        layoutNode(root, 0, width / 2, 50, getNodeSize(0), 0, width);
        
        return { nodes, links };
    }
    
    /**
     * 绘制树状连线
     */
    drawTreeLinks(container, links) {
        links.forEach(link => {
            const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            
            // 创建曲线路径（贝塞尔曲线）
            const midY = (link.source.y + link.target.y) / 2;
            const d = `M ${link.source.x} ${link.source.y} 
                       C ${link.source.x} ${midY}, 
                         ${link.target.x} ${midY}, 
                         ${link.target.x} ${link.target.y}`;
            
            path.setAttribute('d', d);
            path.setAttribute('fill', 'none');
            path.setAttribute('stroke', '#b5a79b');
            path.setAttribute('stroke-width', '2');
            path.setAttribute('opacity', '0.82');
            
            container.appendChild(path);
        });
    }
    
    /**
     * 绘制树状节点
     */
    drawTreeNodes(container, nodes) {
        nodes.forEach(node => {
            const nodeGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
            nodeGroup.setAttribute('class', 'tree-node');
            nodeGroup.setAttribute('data-node-id', node.id);
            nodeGroup.setAttribute('transform', `translate(${node.x - node.width/2}, ${node.y})`);
            nodeGroup.style.cursor = 'pointer';
            
            // 创建节点背景（正方形）
            const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            rect.setAttribute('width', node.width);
            rect.setAttribute('height', node.height);
            rect.setAttribute('rx', '5');
            rect.setAttribute('ry', '5');
            rect.setAttribute('fill', 'white');
            rect.setAttribute('stroke', this.getNodeColor(node.magnification));
            rect.setAttribute('stroke-width', '2');
            rect.setAttribute('filter', 'drop-shadow(0 2px 6px rgba(0, 0, 0, 0.15))');
            nodeGroup.appendChild(rect);
            
            // 尝试获取ROI图像并嵌入
            let imageUrl = this.getROIImageUrl(node);
            
            // ⭐ 所有尺寸按比例计算（基于节点高度）
            const padding = Math.max(2, node.height * 0.03);  // 3% padding
            const infoBarHeight = Math.max(12, node.height * 0.22);  // 22% 用于底部标签
            const imageHeight = node.height - padding * 2;  // 图片填满节点
            const imageWidth = node.width - padding * 2;
            const cornerRadius = Math.max(3, node.width * 0.07);  // 7% 圆角
            
            // ⭐ 处理需要异步获取的红框图片
            if (imageUrl && imageUrl.startsWith('__DEBUG_IMAGE__/')) {
                const parts = imageUrl.replace('__DEBUG_IMAGE__/', '').split('/');
                const fileId = parts[0];
                const roiId = parts[1];
                // 异步获取真正的图片URL
                this.fetchDebugImage(fileId, roiId, nodeGroup, padding, imageWidth, imageHeight, cornerRadius, node.id);
                imageUrl = null; // 先不显示图片，等API返回后再添加
            }
            
            if (imageUrl) {
                // 添加图像（填满整个节点）
                const image = document.createElementNS('http://www.w3.org/2000/svg', 'image');
                image.setAttribute('x', padding);
                image.setAttribute('y', padding);
                image.setAttribute('width', imageWidth);
                image.setAttribute('height', imageHeight);
                image.setAttribute('href', imageUrl);
                image.setAttribute('preserveAspectRatio', 'xMidYMid slice');
                
                // 添加圆角遮罩
                const clipId = `clip-${node.id}`;
                const clipPath = document.createElementNS('http://www.w3.org/2000/svg', 'clipPath');
                clipPath.setAttribute('id', clipId);
                const clipRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                clipRect.setAttribute('x', padding);
                clipRect.setAttribute('y', padding);
                clipRect.setAttribute('width', imageWidth);
                clipRect.setAttribute('height', imageHeight);
                clipRect.setAttribute('rx', cornerRadius);
                clipRect.setAttribute('ry', cornerRadius);
                clipPath.appendChild(clipRect);
                nodeGroup.appendChild(clipPath);
                
                image.setAttribute('clip-path', `url(#${clipId})`);
                nodeGroup.appendChild(image);
            }
            
            // ⭐ 底部半透明标签条（覆盖在图片底部）
            const infoBar = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            infoBar.setAttribute('x', padding);
            infoBar.setAttribute('y', node.height - infoBarHeight - padding);
            infoBar.setAttribute('width', imageWidth);
            infoBar.setAttribute('height', infoBarHeight);
            infoBar.setAttribute('fill', 'rgba(255, 255, 255, 0.85)');
            infoBar.setAttribute('rx', `0 0 ${cornerRadius} ${cornerRadius}`);
            nodeGroup.appendChild(infoBar);
            
            // ⭐ 简化标签：只显示当前层级名称（如 R11-B5 只显示 B5）
            const simplifyLabel = (fullLabel) => {
                if (!fullLabel) return '';
                // 如果是 R11-B5-G2 格式，只取最后一部分 G2
                // 如果是 R11-B5 格式，只取 B5
                // 如果是 R11 格式，保持 R11
                const parts = fullLabel.split('-');
                if (parts.length >= 2) {
                    return parts[parts.length - 1]; // 取最后一部分
                }
                return fullLabel;
            };
            
            const displayLabel = simplifyLabel(node.label);
            
            // ⭐ 标签位置和字体大小也按比例计算
            const fontSize = Math.max(8, Math.min(12, node.height * 0.18));  // 18% 字体大小
            const labelY = node.height - padding - infoBarHeight / 2 + fontSize * 0.35;  // 居中于信息栏
            
            // 添加节点标签（覆盖在图片底部的半透明条上）
            const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            label.setAttribute('x', node.width / 2);
            label.setAttribute('y', labelY);
            label.setAttribute('text-anchor', 'middle');
            label.setAttribute('fill', '#333');
            label.setAttribute('font-size', fontSize);
            label.setAttribute('font-weight', '600');
            label.textContent = displayLabel;
            nodeGroup.appendChild(label);
            
            // 添加点击事件
            nodeGroup.addEventListener('click', () => {
                this.onTreeNodeClick(node);
            });
            
            // 添加悬停效果
            nodeGroup.addEventListener('mouseenter', () => {
                rect.setAttribute('stroke-width', '4');
                rect.setAttribute('filter', 'drop-shadow(0 6px 12px rgba(0, 0, 0, 0.25))');
            });
            
            nodeGroup.addEventListener('mouseleave', () => {
                rect.setAttribute('stroke-width', '3');
                rect.setAttribute('filter', 'drop-shadow(0 4px 8px rgba(0, 0, 0, 0.15))');
            });
            
            container.appendChild(nodeGroup);
        });
    }
    
    /**
     * 获取ROI图像URL
     * ⭐ Phase 2.2: 优先使用 analysis_tree.json 中的 image_url/image_path
     */
    getROIImageUrl(node) {
        console.log('🖼️ 获取ROI图像URL:', node.id);
        
        // 如果是根节点，返回缩略图
        if (node.id === 'root' && this.currentFile) {
            console.log('✅ 根节点，使用缩略图:', this.currentFile.thumbnail_url);
            return this.currentFile.thumbnail_url;
        }
        
        // ⭐ Phase 2.2: 优先使用节点上的 image_url（已由API处理好）
        if (node.image_url) {
            console.log('✅ 使用节点 image_url:', node.image_url);
            return node.image_url;
        }
        
        // ⭐ Phase 2.2: 其次使用节点上的 image_path
        if (node.image_path) {
            const imagePath = node.image_path.startsWith('/') ? 
                node.image_path : '/' + node.image_path;
            console.log('✅ 使用节点 image_path:', imagePath);
            return imagePath;
        }
        
        // 尝试从分析数据中获取图像路径
        if (node.analysis && node.analysis.image_path) {
            const imagePath = node.analysis.image_path.startsWith('/') ? 
                node.analysis.image_path : '/' + node.analysis.image_path;
            console.log('✅ 使用分析数据中的图像路径:', imagePath);
            return imagePath;
        }
        
        // 尝试从roiAnalysisData中获取
        const analysisData = this.roiAnalysisData ? this.roiAnalysisData[node.id] : null;
        if (analysisData && analysisData.image_path) {
            const imagePath = analysisData.image_path.startsWith('/') ? 
                analysisData.image_path : '/' + analysisData.image_path;
            console.log('✅ 使用roiAnalysisData中的图像路径:', imagePath);
            return imagePath;
        }
        
        // 尝试从overlay数据中获取
        const overlay = this.roiOverlays ? this.roiOverlays.find(o => o.id === node.id) : null;
        if (overlay && overlay.data && overlay.data.image_path) {
            const imagePath = overlay.data.image_path.startsWith('/') ? 
                overlay.data.image_path : '/' + overlay.data.image_path;
            console.log('✅ 使用overlay数据中的图像路径:', imagePath);
            return imagePath;
        }
        
        // ⭐ Fallback: 根据ROI ID和倍率推断图像路径
        if (this.currentFile && node.id && node.magnification) {
            const inferredPath = this.inferImagePath(node.id, node.magnification);
            if (inferredPath) {
                console.log('🔍 推断图像路径:', inferredPath);
                return inferredPath;
            }
        }
        
        console.warn('⚠️ 未找到图像路径，node:', node.id);
        return null;
    }
    
    /**
     * 根据ROI ID和倍率推断图像路径
     * 支持多种ID格式：roi_10x_r{row}_c{col}、R11、R11-B10、R11-B10-G5
     * 
     * 实际文件结构：
     * - 绿框：/outputs/{file_id}/steps/{R}/R-B-G_{mag}x.jpg ✓
     * - 蓝框：/outputs/{file_id}/steps/{R}/R-B_{mag}x.jpg ✓
     * - 红框：不存在单独图片，使用缩略图
     */
    inferImagePath(roiId, magnification) {
        if (!this.currentFile) return null;
        
        const fileId = this.currentFile.file_id;
        
        // 格式1: 完整的 roi_10x_r{row}_c{col} 格式
        const redRoiMatch = roiId.match(/^roi_10x_r\d+_c\d+/);
        if (redRoiMatch) {
            const redRoiId = redRoiMatch[0];
            return `/outputs/${fileId}/steps/${redRoiId}/${roiId}_${magnification}x.jpg`;
        }
        
        // 格式2: 简化的 R{n}、R{n}-B{m}、R{n}-B{m}-G{k} 格式
        
        // 绿框：R11-B5-G2 — 通过 API 查找正确路径
        const greenMatch = roiId.match(/^(R\d+)-(B\d+)-(G\d+)$/);
        if (greenMatch) {
            console.log('🟢 绿框需要通过API获取图片:', roiId);
            return `__DEBUG_IMAGE__/${fileId}/${roiId}`;
        }
        
        // 蓝框：R11-B5 - 从 debug_images 获取带网格的候选区域图
        const blueMatch = roiId.match(/^(R\d+)-(B\d+)$/);
        if (blueMatch) {
            // ⭐ 蓝框也使用带网格的图片（显示AI如何选择绿框）
            console.log('🔵 蓝框需要通过API获取网格图:', roiId);
            return `__DEBUG_IMAGE__/${fileId}/${roiId}`;
        }
        
        // 红框：R11 - 从 debug_images 目录获取带编号的候选区域网格图
        const redMatch = roiId.match(/^R\d+$/);
        if (redMatch) {
            // ⭐ 使用特殊标记，让调用方知道需要异步获取
            // 格式: __DEBUG_IMAGE__/{fileId}/{roiId}
            console.log('🔴 红框需要通过API获取图片:', roiId);
            return `__DEBUG_IMAGE__/${fileId}/${roiId}`;
        }
        
        console.warn('⚠️ 无法推断图像路径:', roiId);
        return null;
    }
    
    /**
     * ⭐ 从 analysisTreeData 中查找完整节点信息
     */
    findNodeInAnalysisTree(nodeId) {
        if (!this.analysisTreeData || !this.analysisTreeData.tree_nodes) {
            return null;
        }
        
        const treeNodes = this.analysisTreeData.tree_nodes;
        const isPatientLevel = treeNodes.length > 0 && treeNodes[0].level === 'wsi';
        
        if (isPatientLevel) {
            // ⭐ Patient Level 模式：WSI → 红框 → 蓝框 → 绿框（4层）
            for (const wsiNode of treeNodes) {
                if (wsiNode.roi_id === nodeId) return wsiNode;
                
                for (const redNode of (wsiNode.children || [])) {
                    if (redNode.roi_id === nodeId) {
                        // ⭐ 附带 WSI 的 file_id，用于推断图片路径
                        return { ...redNode, _wsi_file_id: wsiNode.file_id };
                    }
                    
                    for (const blueNode of (redNode.children || [])) {
                        if (blueNode.roi_id === nodeId) {
                            return { ...blueNode, _wsi_file_id: wsiNode.file_id };
                        }
                        
                        for (const greenNode of (blueNode.children || [])) {
                            if (greenNode.roi_id === nodeId) {
                                return { ...greenNode, _wsi_file_id: wsiNode.file_id };
                            }
                        }
                    }
                }
            }
        } else {
            // 单张模式：红框 → 蓝框 → 绿框（3层）
            for (const redNode of treeNodes) {
                if (redNode.roi_id === nodeId) return redNode;
                
                for (const blueNode of (redNode.children || [])) {
                    if (blueNode.roi_id === nodeId) return blueNode;
                    
                    for (const greenNode of (blueNode.children || [])) {
                        if (greenNode.roi_id === nodeId) return greenNode;
                    }
                }
            }
        }
        
        return null;
    }
    
    /**
     * ⭐ 获取节点图片URL（统一方法）
     */
    getNodeImageUrl(node) {
        // 1. 优先使用 image_url（API 已处理好）
        if (node.image_url) {
            return node.image_url.startsWith('/') ? node.image_url : '/' + node.image_url;
        }
        
        // 2. 使用 image_path
        if (node.image_path) {
            return node.image_path.startsWith('/') ? node.image_path : '/' + node.image_path;
        }
        
        // 3. 从 analysis 中获取
        if (node.analysis && node.analysis.image_path) {
            const path = node.analysis.image_path;
            return path.startsWith('/') ? path : '/' + path;
        }
        
        // 4. 尝试从 roiOverlays 中查找
        if (this.roiOverlays) {
            const overlay = this.roiOverlays.find(o => 
                o.id === node.id || 
                o.id === node.roi_id ||
                o.label === node.id ||
                o.label === node.label
            );
            if (overlay && overlay.data && overlay.data.image_path) {
                const path = overlay.data.image_path;
                return path.startsWith('/') ? path : '/' + path;
            }
        }
        
        // 5. ⭐ 尝试推断路径（支持多种格式：roi_10x_...、R11、R11-B5、R11-B5-G2）
        const roiId = node.roi_id || node.id;
        if (roiId && roiId.match(/^R\d+/)) {
            const mag = node.magnification || (roiId.match(/^R\d+-B\d+-G\d+$/) ? '160' : roiId.match(/^R\d+-B\d+$/) ? '40' : '10');
            
            // ⭐ Patient Level 模式：使用节点上附带的 _wsi_file_id
            const wsiFileId = node._wsi_file_id || node.file_id;
            if (wsiFileId && this.currentPatientId) {
                return this.inferImagePathPatientLevel(roiId, mag, wsiFileId);
            }
            
            // 单张模式：使用 this.currentFile
            if (this.currentFile) {
                return this.inferImagePath(roiId, mag);
            }
        }
        
        // 6. ⭐ WSI 节点：返回缩略图
        if (node.thumbnail_url) {
            return node.thumbnail_url;
        }
        
        return null;
    }
    
    /**
     * ⭐ Patient Level 模式下推断图片路径
     * 路径格式：/outputs/patient_{patientId}/auto/wsi_{fileId}/steps/{R}/{roiId}_{mag}x.jpg
     */
    inferImagePathPatientLevel(roiId, magnification, wsiFileId) {
        const patientId = this.currentPatientId;
        if (!patientId || !wsiFileId) return null;
        
        const basePath = `/outputs/patient_${patientId}/auto/wsi_${wsiFileId}/steps`;
        
        // 绿框：R11-B5-G2 — 通过 API 查找正确路径
        const greenMatch = roiId.match(/^(R\d+)-(B\d+)-(G\d+)$/);
        if (greenMatch) {
            return `__DEBUG_IMAGE__/${wsiFileId}/${roiId}`;
        }
        
        // 蓝框：R11-B5 - 需要异步获取 debug 图片
        const blueMatch = roiId.match(/^(R\d+)-(B\d+)$/);
        if (blueMatch) {
            return `__DEBUG_IMAGE__/${wsiFileId}/${roiId}`;
        }
        
        // 红框：R11 - 需要异步获取 debug 图片
        const redMatch = roiId.match(/^R\d+$/);
        if (redMatch) {
            return `__DEBUG_IMAGE__/${wsiFileId}/${roiId}`;
        }
        
        return null;
    }
    
    /**
     * ⭐ 异步获取debug图片（红框/蓝框的候选区域网格图）
     */
    async fetchDebugImage(fileId, roiId, nodeGroup, padding, imageWidth, imageHeight, cornerRadius, nodeId) {
        try {
            const response = await fetch(`/api/debug-image/${fileId}/${roiId}`);
            const result = await response.json();
            
            if (result.success && result.image_url) {
                // 创建并添加图片元素
                const image = document.createElementNS('http://www.w3.org/2000/svg', 'image');
                image.setAttribute('x', padding);
                image.setAttribute('y', padding);
                image.setAttribute('width', imageWidth);
                image.setAttribute('height', imageHeight);
                image.setAttribute('href', result.image_url);
                image.setAttribute('preserveAspectRatio', 'xMidYMid slice');
                
                // 添加圆角遮罩
                const clipId = `clip-debug-${nodeId}`;
                const clipPath = document.createElementNS('http://www.w3.org/2000/svg', 'clipPath');
                clipPath.setAttribute('id', clipId);
                const clipRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                clipRect.setAttribute('x', padding);
                clipRect.setAttribute('y', padding);
                clipRect.setAttribute('width', imageWidth);
                clipRect.setAttribute('height', imageHeight);
                clipRect.setAttribute('rx', cornerRadius);
                clipRect.setAttribute('ry', cornerRadius);
                clipPath.appendChild(clipRect);
                nodeGroup.appendChild(clipPath);
                
                image.setAttribute('clip-path', `url(#${clipId})`);
                
                // 插入到背景rect之后（在标签之前）
                const firstText = nodeGroup.querySelector('text');
                if (firstText) {
                    nodeGroup.insertBefore(image, firstText);
                } else {
                    nodeGroup.appendChild(image);
                }
                
                console.log('✅ 成功加载debug图片:', roiId, result.image_url);
            } else {
                console.log('⚠️ 未找到debug图片:', roiId);
            }
        } catch (error) {
            console.error('❌ 获取debug图片失败:', roiId, error);
        }
    }
    
    /**
     * 获取节点颜色
     */
    getNodeColor(magnification) {
        // ⭐ Patient Level 模式下，WSI 节点使用紫色
        if (magnification === '切片' || magnification === 'Slide') return '#77668f';
        if (magnification === '全景' || magnification === 'Panorama' || magnification === __t('label_panorama')) return '#9b6b54';
        
        const mag = parseInt(magnification);
        if (mag <= 10) return '#cf5b4d';
        if (mag <= 40) return '#4f7fa5';
        return '#4f9568';
    }
    
    /**
     * 树状图节点点击事件
     * ⭐ Phase 2.3: 点击节点时在模态窗口内显示详情，不关闭窗口
     */
    onTreeNodeClick(node) {
        console.log('🖱️ 点击树状图节点:', node);
        
        if (node.id === 'root') {
            // ⭐ 检测是否为 Patient Level 模式
            const isPatientLevel = this.analysisTreeData?.tree_nodes?.[0]?.level === 'wsi';
            
            if (isPatientLevel) {
                // ⭐ Patient Level 根节点：展示最终综合报告
                console.log('🏥 点击患者根节点，显示 Patient Level 最终报告');
                this.showPatientRootDetails(node);
            } else {
                // 单张模式根节点：显示缩略图分析
                console.log('🖼️ 点击缩略图/根节点，显示全局分析');
                if (this.viewer) {
                    this.viewer.viewport.goHome();
                }
                this.showRootNodeDetails(node);
            }
            
            this.highlightTreeNode('root');
            return;
        }
        
        // ⭐ Patient Level WSI 节点：展示该切片的缩略图和全局分析
        if (node.nodeType === 'wsi') {
            console.log('📋 点击 WSI 节点，显示切片详情:', node.id);
            this.showWsiNodeDetails(node);
            this.highlightTreeNode(node.id);
            return;
        }
        
        // ⭐ Phase 2.3: 在模态窗口内显示详细的 Agent Loop 分析
        this.showTreeNodeDetails(node);
        
        // ⭐ Phase 2.4: 同时在WSI查看器中定位（不关闭模态窗口）
        this.jumpToROI(node);
        
        // 高亮选中的节点
        this.highlightTreeNode(node.id);
    }
    
    /**
     * ⭐ 显示根节点（缩略图）的详情
     * 包含全局分析结果和选择红框的Plan
     */
    showRootNodeDetails(node) {
        const panel = document.getElementById('pathDetailsPanel');
        const content = document.getElementById('pathDetailsContent');
        
        if (!panel || !content) return;
        
        // 显示面板
        panel.style.display = 'block';
        
        // 获取缩略图URL（确保有 / 前缀）
        let thumbnailUrl = '';
        if (this.analysisTreeData && this.analysisTreeData.metadata) {
            const rawUrl = this.analysisTreeData.metadata.thumbnail_url || '';
            if (rawUrl) {
                thumbnailUrl = rawUrl.startsWith('/') ? rawUrl : '/' + rawUrl;
            }
        }
        
        // 获取全局分析（红框选择的Reason和Plan）
        let globalAnalysis = '';
        let selectedRedBoxes = [];
        
        // ⭐ 调试日志：检查 analysisTreeData 结构
        console.log('📋 [showRootNodeDetails] analysisTreeData:', this.analysisTreeData);
        console.log('📋 [showRootNodeDetails] metadata:', this.analysisTreeData?.metadata);
        console.log('📋 [showRootNodeDetails] global_analysis长度:', this.analysisTreeData?.metadata?.global_analysis?.length || 0);
        
        if (this.analysisTreeData) {
            // 从 metadata 获取全局分析
            // ⭐ 兼容两种格式：自动模式=字符串，协作模式={reasoning,plan,evidence}
            if (this.analysisTreeData.metadata && this.analysisTreeData.metadata.global_analysis) {
                const raw = this.analysisTreeData.metadata.global_analysis;
                if (typeof raw === 'string') {
                    globalAnalysis = raw;
                } else if (typeof raw === 'object') {
                    // 协作模式下是 {reasoning, plan, evidence}
                    globalAnalysis = raw.reasoning || raw.analysis || JSON.stringify(raw);
                }
                console.log('✅ 找到 global_analysis，长度:', globalAnalysis.length);
            } else {
                console.warn('⚠️ global_analysis 为空或不存在！');
                console.warn('⚠️ metadata 字段:', Object.keys(this.analysisTreeData.metadata || {}));
            }
            
            // 统计选中的红框
            if (this.analysisTreeData.tree_nodes) {
                selectedRedBoxes = this.analysisTreeData.tree_nodes.map(n => n.roi_id);
            }
        } else {
            console.warn('⚠️ analysisTreeData 为空！');
        }
        
        // 构建详情HTML
        const detailsHtml = `
            <div class="inline-node-detail">
                <!-- 标题栏 -->
                <div class="inline-detail-header" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%)">
                    <span class="inline-node-title">🖼️ ${__t('label_thumbnail')} / Level 0</span>
                    <span class="inline-node-zoom">${__t('label_panorama')}</span>
                </div>
                
                <!-- 缩略图 -->
                <div class="inline-detail-image" id="detailImageContainer">
                    ${thumbnailUrl ? `
                        <img src="${thumbnailUrl}" alt="${__t('label_thumbnail')}" style="max-width: 100%; border-radius: 8px;" />
                    ` : `<div class="img-placeholder">${__t('label_panorama_thumb')}</div>`}
                </div>
                
                <!-- 全局分析内容 -->
                <div class="inline-detail-content">
                    ${globalAnalysis ? `
                        <div class="inline-section">
                            <div class="inline-section-title">🔍 ${__t('section_global_obs')} 【Reason】</div>
                            <div class="inline-section-body">${globalAnalysis.replace(/\n/g, '<br>')}</div>
                        </div>
                    ` : ''}
                    
                    ${selectedRedBoxes.length > 0 ? `
                        <div class="inline-section">
                            <div class="inline-section-title">📋 ${__t('section_plan_decision')} 【Plan】</div>
                            <div class="inline-section-body">
                                <strong>${__t('label_action')}:</strong> A (${__t('label_drill_down')})<br>
                                <strong>${__t('label_selected_red')}:</strong> ${selectedRedBoxes.join(', ')}<br>
                                <strong>${__t('label_total')}:</strong> ${selectedRedBoxes.length} ${__t('label_regions')}
                            </div>
                        </div>
                    ` : ''}
                    
                    <div class="inline-section">
                        <div class="inline-section-title">📊 ${__t('section_stats')}</div>
                        <div class="inline-section-body">
                            <strong>${__t('stat_red_box')}:</strong> ${this.analysisTreeData?.tree_nodes?.length || 0}${__t('stat_unit')}<br>
                            <strong>${__t('stat_blue_box')}:</strong> ${this.analysisTreeData?.tree_nodes?.reduce((sum, r) => sum + (r.children?.length || 0), 0) || 0}${__t('stat_unit')}<br>
                            <strong>${__t('stat_green_box')}:</strong> ${this.analysisTreeData?.tree_nodes?.reduce((sum, r) => sum + (r.children?.reduce((s, b) => s + (b.children?.length || 0), 0) || 0), 0) || 0}${__t('stat_unit')}
                        </div>
                    </div>
                </div>
            </div>
        `;
        
        content.innerHTML = detailsHtml;
        console.log('✅ 显示缩略图详情');
    }
    
    /**
     * ⭐ Patient Level 根节点详情：展示最终综合报告
     */
    showPatientRootDetails(node) {
        const panel = document.getElementById('pathDetailsPanel');
        const content = document.getElementById('pathDetailsContent');
        if (!panel || !content) return;
        
        panel.style.display = 'block';
        
        // 从 metadata 获取最终报告
        const metadata = this.analysisTreeData?.metadata || {};
        const aiFinalReport = metadata.ai_final_report || '';
        const folderName = metadata.folder_name || '未知患者';
        const totalWsi = metadata.total_wsi || 0;
        
        // 统计
        const treeNodes = this.analysisTreeData?.tree_nodes || [];
        const totalRed = treeNodes.reduce((sum, wsi) => sum + (wsi.children?.length || 0), 0);
        const totalBlue = treeNodes.reduce((sum, wsi) => sum + (wsi.children?.reduce((s, r) => s + (r.children?.length || 0), 0) || 0), 0);
        const totalGreen = treeNodes.reduce((sum, wsi) => sum + (wsi.children?.reduce((s, r) => s + (r.children?.reduce((s2, b) => s2 + (b.children?.length || 0), 0) || 0), 0) || 0), 0);
        
        const detailsHtml = `
            <div class="inline-node-detail">
                <div class="inline-detail-header" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%)">
                    <span class="inline-node-title">🏥 ${__t('stat_patient')}: ${folderName}</span>
                    <span class="inline-node-zoom">${totalWsi} ${__t('stat_slides')}</span>
                </div>
                
                <div class="inline-detail-content">
                    <div class="inline-section">
                        <div class="inline-section-title">📊 ${__t('stat_overall')}</div>
                        <div class="inline-section-body">
                            <strong>${__t('stat_slides')}:</strong> ${totalWsi}<br>
                            <strong>${__t('stat_red_box')}:</strong> ${totalRed}${__t('stat_unit')}<br>
                            <strong>${__t('stat_blue_box')}:</strong> ${totalBlue}${__t('stat_unit')}<br>
                            <strong>${__t('stat_green_box')}:</strong> ${totalGreen}${__t('stat_unit')}
                        </div>
                    </div>
                    
                    ${aiFinalReport ? `
                        <div class="inline-section">
                            <div class="inline-section-title">📋 ${__t('stat_diag_report')}</div>
                            <div class="inline-section-body" style="white-space: pre-wrap; line-height: 1.8; font-size: 13px;">${aiFinalReport.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</div>
                        </div>
                    ` : `
                        <div class="inline-section">
                            <div class="inline-section-title">📋 ${__t('stat_diag_report_short')}</div>
                            <div class="inline-section-body" style="color: #999;">${(window.__lang||'zh')==='en' ? 'Report not yet generated' : '报告尚未生成'}</div>
                        </div>
                    `}
                </div>
            </div>
        `;
        
        content.innerHTML = detailsHtml;
        console.log('✅ 显示 Patient Level 综合报告');
    }
    
    /**
     * ⭐ Patient Level WSI 节点详情：展示该切片的缩略图和全局分析
     */
    showWsiNodeDetails(node) {
        const panel = document.getElementById('pathDetailsPanel');
        const content = document.getElementById('pathDetailsContent');
        if (!panel || !content) return;
        
        panel.style.display = 'block';
        
        const analysis = node.analysis || {};
        const filename = node.filename || '';
        const partName = node.part_name || '';
        const thumbnailUrl = node.thumbnail_url || '';
        const globalAnalysis = analysis.findings || analysis.reasoning || '';
        
        // 统计该 WSI 的红/蓝/绿框数
        const redCount = node.children?.length || 0;
        const blueCount = (node.children || []).reduce((sum, r) => sum + (r.children?.length || 0), 0);
        const greenCount = (node.children || []).reduce((sum, r) => sum + (r.children?.reduce((s, b) => s + (b.children?.length || 0), 0) || 0), 0);
        
        const detailsHtml = `
            <div class="inline-node-detail">
                <div class="inline-detail-header node-tone-wsi">
                    <span class="inline-node-title">📋 ${node.label}</span>
                    <span class="inline-node-zoom">${partName}</span>
                </div>
                
                <!-- 缩略图 -->
                <div class="inline-detail-image" id="detailImageContainer">
                    ${thumbnailUrl ? `
                        <img src="${thumbnailUrl}" alt="${node.label}" style="max-width: 100%; border-radius: 8px;" 
                             onerror="this.parentElement.innerHTML='<div class=\\'img-placeholder\\'>${__t('img_not_found')}</div>'" />
                    ` : `<div class="img-placeholder">${__t('img_not_found')}</div>`}
                </div>
                
                <div class="inline-detail-content">
                    <div class="inline-section">
                        <div class="inline-section-title">📄 ${__t('slide_info', 'Slide Info')}</div>
                        <div class="inline-section-body">
                            <strong>${__t('label_filename', 'Filename')}:</strong> ${filename}<br>
                            <strong>${__t('label_part', 'Part')}:</strong> ${partName}<br>
                            <strong>${__t('red_box')}:</strong> ${redCount} | <strong>${__t('blue_box')}:</strong> ${blueCount} | <strong>${__t('green_box')}:</strong> ${greenCount}
                        </div>
                    </div>
                    
                    ${globalAnalysis ? `
                        <div class="inline-section">
                            <div class="inline-section-title">🔍 ${__t('section_global_obs')}</div>
                            <div class="inline-section-body">${globalAnalysis.replace(/\n/g, '<br>')}</div>
                        </div>
                    ` : ''}
                </div>
            </div>
        `;
        
        content.innerHTML = detailsHtml;
        console.log('✅ 显示 WSI 切片详情:', node.label);
    }
    
    /**
     * ⭐ 在左侧面板底部显示节点详情
     * 简化版：直接嵌入层级结构面板底部
     */
    showTreeNodeDetails(node) {
        const panel = document.getElementById('pathDetailsPanel');
        const content = document.getElementById('pathDetailsContent');
        
        if (!panel || !content) return;
        
        // 显示面板
        panel.style.display = 'block';
        
        // ⭐ 尝试从 analysisTreeData 中查找完整节点信息（包含 image_url）
        const fullNode = this.findNodeInAnalysisTree(node.id || node.label);
        if (fullNode) {
            console.log('✅ 从analysisTreeData找到完整节点:', fullNode.roi_id);
            // 合并数据
            node = { ...node, ...fullNode, id: node.id || fullNode.roi_id };
        }
        
        const analysis = node.analysis || {};
        const nodeType = node.nodeType || node.level || (node.magnification === '10' ? 'red' : node.magnification === '40' ? 'blue' : 'green');
        
        // 获取颜色和样式
        const colorEmoji = nodeType === 'red' ? '🔴' : nodeType === 'blue' ? '🔵' : '🟢';
        const colorName = nodeType === 'red' ? '10×' : nodeType === 'blue' ? '40×' : '160×';
        
        // 置信度和病理类型
        const confidence = analysis.confidence || node.confidence || 0;
        const pathologyType = analysis.pathology_type || 'uncertain';
        const typeEmoji = pathologyType === 'malignant' ? '⚠️' : pathologyType === 'benign' ? '✅' : '❓';
        const typeLabel = pathologyType === 'malignant' ? __t('type_malignant') : pathologyType === 'benign' ? __t('type_benign') : __t('type_uncertain');
        const typeClass = pathologyType === 'malignant' ? 'malignant' : pathologyType === 'benign' ? 'benign' : 'uncertain';
        
        // ⭐ 获取图片URL - 多种方式尝试
        let imgSrc = this.getNodeImageUrl(node);
        let needAsyncImage = false;
        let asyncImageParams = null;
        
        // ⭐ 处理需要异步获取的红框图片（优先从缓存读取）
        if (imgSrc && imgSrc.startsWith('__DEBUG_IMAGE__/')) {
            const parts = imgSrc.replace('__DEBUG_IMAGE__/', '').split('/');
            asyncImageParams = { fileId: parts[0], roiId: parts[1] };
            const cacheKey = `${parts[0]}/${parts[1]}`;
            if (this._detailImageCache && this._detailImageCache[cacheKey]) {
                imgSrc = this._detailImageCache[cacheKey];
            } else {
                needAsyncImage = true;
                imgSrc = null;
            }
        }
        
        console.log('📷 节点详情图片URL:', imgSrc, 'needAsync:', needAsyncImage, 'node:', node.id);
        
        // 构建简洁的详情HTML
        let detailsHtml = `
            <div class="inline-node-detail">
                <!-- 标题栏 -->
                <div class="inline-detail-header node-tone-${nodeType}">
                    <span class="inline-node-title">${colorEmoji} ${node.id || node.label}</span>
                    <span class="inline-node-zoom">${colorName}</span>
                </div>
                
                <!-- 图片容器（可能需要异步加载；死链接时按 file_id 重试一次） -->
                <div class="inline-detail-image" id="detailImageContainer">
                    ${imgSrc ? `
                        <img src="${imgSrc}" alt="${node.id}" 
                             onerror="window.pathchat?._retryDetailImage?.(this, '${this.currentFile?.file_id || ''}', '${node.id || ''}')" />
                    ` : (needAsyncImage ? `<div class="img-loading">${__t('img_loading')}</div>` : '')}
                </div>
                
                <!-- 分析内容 -->
                <div class="inline-detail-content">
        `;
        
        // Reason - 观察与推理
        const reasoning = analysis.reasoning || analysis.findings || '';
        if (reasoning) {
            detailsHtml += `
                <div class="inline-section">
                    <div class="inline-section-title"><i class="fas fa-brain"></i> ${__t('section_reasoning')}</div>
                    <div class="inline-section-body">${this.formatAgentLoopContent(reasoning)}</div>
                </div>
            `;
        }
        
        // Plan - 决策
        const planAction = analysis.plan_action;
        if (planAction) {
            detailsHtml += `
                <div class="inline-section">
                    <div class="inline-section-title"><i class="fas fa-tasks"></i> ${__t('section_plan')}</div>
                    <div class="inline-section-body"><strong>${planAction}</strong></div>
                </div>
            `;
        }
        
        // 关键特征
        const keyFeatures = analysis.key_features || [];
        if (keyFeatures.length > 0) {
            detailsHtml += `
                <div class="inline-section">
                    <div class="inline-section-title"><i class="fas fa-star"></i> ${__t('section_features')}</div>
                    <div class="inline-section-body">
                        ${keyFeatures.map(f => `<span class="inline-tag">${f}</span>`).join('')}
                    </div>
                </div>
            `;
        }
        
        detailsHtml += `</div></div>`;
        
        content.innerHTML = detailsHtml;
        
        // ⭐ 如果需要异步加载图片，现在加载
        if (needAsyncImage && asyncImageParams) {
            this.loadDetailImage(asyncImageParams.fileId, asyncImageParams.roiId);
        }
        
        // 滚动到详情
        panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    
    /**
     * ⭐ 树 JSON 里的 image_path 可能指向已失效的历史 debug 目录（旧 run 按
     * 文件名键保存）。图片 404 时按 file_id 走 /api/debug-image 重试一次。
     */
    _retryDetailImage(imgEl, fileId, roiId) {
        const container = imgEl?.parentElement;
        if (!container) return;
        if (container.dataset.retried === '1' || !fileId || !roiId) {
            container.innerHTML = `<div class="img-error">${__t('img_load_fail')}</div>`;
            return;
        }
        container.dataset.retried = '1';
        container.innerHTML = `<div class="img-loading">${__t('img_loading')}</div>`;
        this.loadDetailImage(fileId, roiId);
    }

    /**
     * ⭐ 异步加载详情面板中的图片
     */
    async loadDetailImage(fileId, roiId) {
        try {
            const container = document.getElementById('detailImageContainer');
            if (!container) return;
            
            const cacheKey = `${fileId}/${roiId}`;
            if (!this._detailImageCache) this._detailImageCache = {};
            
            if (this._detailImageCache[cacheKey]) {
                container.innerHTML = `
                    <img src="${this._detailImageCache[cacheKey]}" alt="${roiId}" 
                         onerror="this.parentElement.innerHTML='<div class=\\'img-error\\'>${__t('img_load_fail')}</div>'" />
                `;
                return;
            }
            
            const response = await fetch(`/api/debug-image/${fileId}/${roiId}`);
            const result = await response.json();
            
            if (result.success && result.image_url) {
                this._detailImageCache[cacheKey] = result.image_url;
                container.innerHTML = `
                    <img src="${result.image_url}" alt="${roiId}" 
                         onerror="this.parentElement.innerHTML='<div class=\\'img-error\\'>${__t('img_load_fail')}</div>'" />
                `;
                console.log('✅ 详情面板图片加载成功:', roiId);
            } else {
                container.innerHTML = `<div class="img-error">${__t('img_not_found')}</div>`;
            }
        } catch (error) {
            console.error('❌ 详情面板图片加载失败:', error);
            const container = document.getElementById('detailImageContainer');
            if (container) {
                container.innerHTML = `<div class="img-error">${__t('img_load_fail')}</div>`;
            }
        }
    }
    
    /**
     * ⭐ Phase 2.3: 格式化 Agent Loop 内容（处理 Markdown 风格的文本）
     */
    formatAgentLoopContent(text) {
        if (!text) return `<p>${__t('no_data')}</p>`;
        
        // 简单的 Markdown 转 HTML
        return text
            .split('\n')
            .map(line => {
                // 处理粗体
                line = line.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
                // 处理标题
                if (line.startsWith('### ')) {
                    return `<h5>${line.substring(4)}</h5>`;
                }
                if (line.startsWith('## ')) {
                    return `<h4>${line.substring(3)}</h4>`;
                }
                if (line.startsWith('# ')) {
                    return `<h3>${line.substring(2)}</h3>`;
                }
                // 处理列表
                if (line.match(/^\d+\.\s/)) {
                    return `<p class="list-item numbered">${line}</p>`;
                }
                if (line.startsWith('- ') || line.startsWith('* ')) {
                    return `<p class="list-item">${line.substring(2)}</p>`;
                }
                // 普通段落
                if (line.trim()) {
                    return `<p>${line}</p>`;
                }
                return '';
            })
            .join('');
    }
    
    /**
     * ⭐ Phase 2.3: 高亮选中的树节点
     */
    highlightTreeNode(nodeId) {
        // 移除所有节点的高亮
        document.querySelectorAll('#treeSvg .tree-node').forEach(node => {
            const rect = node.querySelector('rect');
            if (rect) {
                rect.setAttribute('stroke-width', '3');
            }
            node.classList.remove('selected');
        });
        
        // 高亮选中的节点
        const selectedNode = document.querySelector(`#treeSvg .tree-node[data-node-id="${nodeId}"]`);
        if (selectedNode) {
            const rect = selectedNode.querySelector('rect');
            if (rect) {
                rect.setAttribute('stroke-width', '5');
            }
            selectedNode.classList.add('selected');
        }
    }
    
    /**
     * 添加树状图交互功能
     */
    addTreeInteractivity(svg) {
        let isPanning = false;
        let startX = 0;
        let startY = 0;
        let currentX = 0;
        let currentY = 0;
        
        const treeGroup = svg.querySelector('#treeGroup');
        if (!treeGroup) return;
        
        // 鼠标滚轮缩放
        svg.addEventListener('wheel', (e) => {
            e.preventDefault();
            
            const delta = e.deltaY > 0 ? 0.9 : 1.1;
            this.treeZoomLevel *= delta;
            
            // 限制缩放范围
            this.treeZoomLevel = Math.max(0.3, Math.min(3, this.treeZoomLevel));
            
            this.updateTreeTransform(treeGroup);
        });
        
        // 鼠标拖拽平移
        svg.addEventListener('mousedown', (e) => {
            // 如果点击的是节点，不启动拖拽
            if (e.target.closest('.tree-node')) return;
            
            isPanning = true;
            startX = e.clientX - currentX;
            startY = e.clientY - currentY;
            svg.style.cursor = 'grabbing';
        });
        
        svg.addEventListener('mousemove', (e) => {
            if (!isPanning) return;
            
            currentX = e.clientX - startX;
            currentY = e.clientY - startY;
            
            this.treePanX = currentX;
            this.treePanY = currentY;
            
            this.updateTreeTransform(treeGroup);
        });
        
        svg.addEventListener('mouseup', () => {
            isPanning = false;
            svg.style.cursor = 'default';
        });
        
        svg.addEventListener('mouseleave', () => {
            isPanning = false;
            svg.style.cursor = 'default';
        });
    }
    
    /**
     * 更新树状图变换
     */
    updateTreeTransform(element) {
        element.setAttribute('transform', 
            `translate(${this.treePanX}, ${this.treePanY}) scale(${this.treeZoomLevel})`);
    }
    
    /**
     * 显示树状图空状态
     */
    showTreeEmptyState(message = '请先启动Agent分析生成ROI') {
        const container = document.getElementById('treeStructureContent');
        if (container) {
            container.innerHTML = `
                <div class="tree-empty-state">
                    <i class="fas fa-sitemap"></i>
                    <p>暂无分析数据</p>
                    <p style="font-size: 14px; margin-top: 10px;">${message}</p>
                </div>
            `;
        }
    }
    
    /**
     * 树状图缩放
     */
    treeZoom(factor) {
        console.log('🔍 树状图缩放:', factor);
        
        const svg = document.getElementById('treeSvg');
        const treeGroup = svg?.querySelector('#treeGroup');
        
        if (!treeGroup) return;
        
        this.treeZoomLevel = (this.treeZoomLevel || 1) * factor;
        this.treeZoomLevel = Math.max(0.3, Math.min(3, this.treeZoomLevel));
        
        this.updateTreeTransform(treeGroup);
    }
    
    /**
     * 重置树状图视图
     */
    treeReset() {
        console.log('🔄 重置树状图视图');
        
        const svg = document.getElementById('treeSvg');
        const treeGroup = svg?.querySelector('#treeGroup');
        
        if (!treeGroup) return;
        
        // 重置缩放和平移
        this.treeZoomLevel = 1;
        this.treePanX = 0;
        this.treePanY = 0;
        
        this.updateTreeTransform(treeGroup);
    }
    
    /**
     * 导出树状图图片
     */
    async exportTreeImage() {
        console.log('📥 导出树状图图片');
        
        try {
            const svg = document.getElementById('treeSvg');
            if (!svg) {
                this.showNotification('error', (window.__lang||'zh')==='en' ? 'Tree view not found' : '未找到树状图');
                return;
            }
            
            // 获取SVG的XML字符串
            const svgData = new XMLSerializer().serializeToString(svg);
            
            // 创建Blob
            const blob = new Blob([svgData], { type: 'image/svg+xml;charset=utf-8' });
            
            // 创建下载链接
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = `pathology_tree_${Date.now()}.svg`;
            
            // 触发下载
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            
            // 释放URL对象
            URL.revokeObjectURL(url);
            
            this.showNotification('success', (window.__lang||'zh')==='en' ? 'Tree exported as SVG' : '树状图已导出为SVG文件');
            
        } catch (error) {
            console.error('❌ 导出失败:', error);
            this.showNotification('error', (window.__lang||'zh')==='en' ? 'Export failed: ' + error.message : '导出失败：' + error.message);
        }
    }
    
    // ================== 点击放置 ROI（第一版点选） ==================

    toggleClickPlaceRoiMode(force) {
        if (!this.viewer) {
            this.showNotification('warning', (window.__lang || 'zh') === 'en' ? 'Please load a WSI file first' : '请先加载WSI文件');
            return;
        }
        const next = typeof force === 'boolean' ? force : !this.clickPlaceRoiMode;
        if (next && this.screenshotMode) this.toggleScreenshotMode();
        this.clickPlaceRoiMode = next;
        const viewerContainer = document.getElementById('openseadragon-viewer');
        viewerContainer?.classList.toggle('click-place-roi-mode', this.clickPlaceRoiMode);
        const isEn = (window.__lang || 'zh') === 'en';
        if (this.clickPlaceRoiMode) {
            this.showNotification(
                'info',
                isEn
                    ? 'Click-to-place ROI is on. Left-click drops a region; drag still pans. Esc exits.'
                    : '已进入点击放置 ROI：左键点击落下区域，拖拽仍可平移，Esc 退出。'
            );
        }
    }

    _normalizeSwarmPath(value) {
        const text = String(value || '').trim();
        if (!text) return '';
        const match = text.match(/R+\d+(?:-B\d+)?(?:-G\d+)?/i);
        if (!match) return '';
        const agent = window.pathChatAgent;
        if (typeof agent?._normalizeRoiCitationId === 'function') {
            return agent._normalizeRoiCitationId(match[0]);
        }
        return match[0].replace(/^R+/i, 'R');
    }

    _overlayLabelText(overlay) {
        const el = overlay?.element;
        if (!el) return '';
        return String(
            el.dataset?.roiId
            || el.getAttribute?.('aria-label')
            || el.querySelector?.('.roi-label')?.textContent
            || ''
        ).trim();
    }

    _citationPathFromParentId(parentId, seen) {
        const direct = this._normalizeSwarmPath(parentId);
        if (direct) return direct;
        const id = String(parentId || '').trim();
        if (!id) return '';
        const overlay = (this.roiOverlays || []).find((item) => {
            const data = item?.data || item;
            return String(data.id || item.id || '') === id;
        });
        return overlay ? this._citationPathFromOverlay(overlay, seen) : '';
    }

    _citationPathFromOverlay(overlay, seen) {
        const walked = seen instanceof Set ? seen : new Set();
        const data = overlay?.data || overlay || {};
        const key = String(data.id || overlay?.id || '');
        if (key) {
            if (walked.has(key)) return '';
            walked.add(key);
        }
        const candidates = [
            data.id,
            data.roi_id,
            data.label,
            data.original_id,
            overlay?.id,
            this._overlayLabelText(overlay),
        ];
        for (const raw of candidates) {
            const path = this._normalizeSwarmPath(raw);
            if (path) return path;
        }
        const rawId = String(data.id || overlay?.id || '').trim();
        const leaf = rawId.match(/^\[?([BG])(\d+)\]?$/i);
        if (leaf) {
            const parentPath = this._citationPathFromParentId(data.parent_id, walked);
            if (parentPath) return `${parentPath}-${leaf[1].toUpperCase()}${leaf[2]}`;
        }
        return this._citationPathFromParentId(data.parent_id, walked) || '';
    }

    _pickContainingAnalysisRoi(x, y) {
        const hits = [];
        for (const overlay of this.roiOverlays || []) {
            const data = overlay?.data || overlay || {};
            if (data.user_added) continue;
            const id = String(data.id || overlay?.id || '');
            if (/^U\d+$/i.test(id)) continue;
            const rect = this._roiOverlayRect(overlay);
            if (!rect) continue;
            if (x < rect.x || x > rect.x + rect.w || y < rect.y || y > rect.y + rect.h) continue;
            hits.push({
                overlay,
                overlayData: data,
                area: rect.w * rect.h,
                mag: Number(data.magnification) || 0,
            });
        }
        if (!hits.length) return null;
        hits.sort((a, b) => {
            if (a.area !== b.area) return a.area - b.area;
            return b.mag - a.mag;
        });
        return hits[0];
    }

    _currentSlideSiteLabel() {
        const fileId = this.currentFile?.file_id || '';
        const idx = this._patientSlideIndexFromFileId(fileId);
        if (Number.isInteger(idx)) {
            const labeled = this._slideLabelForIndex(idx);
            if (labeled) return labeled;
        }
        const name = this.currentFile?.filename || this.currentFile?.name || '';
        return String(name).replace(/\.[^.]+$/, '');
    }

    _currentSlidePathPrefix() {
        const idx = this._patientSlideIndexFromFileId(this.currentFile?.file_id || '');
        return Number.isInteger(idx) ? `S${idx + 1}` : '';
    }

    _roiRectNumbers(rect) {
        const box = rect || {};
        return {
            x: Math.round(Number(box.x || 0)),
            y: Math.round(Number(box.y || 0)),
            w: Math.round(Number(box.w ?? box.width ?? 0)),
            h: Math.round(Number(box.h ?? box.height ?? 0)),
        };
    }

    _formatUserRoiContext(rois, { isEn = (window.__lang || 'zh') === 'en' } = {}) {
        return (Array.isArray(rois) ? rois : []).map((item) => {
            const id = String(item?.id || 'U?').trim();
            const path = String(item?.parentPath || '').trim();
            const slidePrefix = String(item?.slidePrefix || '').trim();
            const evidence = path && slidePrefix && !path.startsWith('S')
                ? `${slidePrefix}-${path}`
                : path;
            const rect = this._roiRectNumbers(item?.rect);
            const mag = item?.magnification;
            const site = String(item?.slideLabel || '').trim();
            if (isEn) {
                return [
                    `New FOV ${id}`,
                    site ? `Slide / site: ${site}` : '',
                    path
                        ? `Analysis path: ${path}${evidence && evidence !== path ? ` (${evidence})` : ''} — this crop sits inside that RX-BX box, not a new specimen site`
                        : 'Not inside a prior RX-BX analysis box',
                    `Image coordinates: x=${rect.x}, y=${rect.y}, w=${rect.w}, h=${rect.h}`,
                    mag ? `Estimated magnification: ${mag}×` : '',
                ].filter(Boolean).join('\n');
            }
            return [
                `新增视野 ${id}`,
                site ? `切片/取材: ${site}` : '',
                path
                    ? `所属分析路径: ${path}${evidence && evidence !== path ? `（证据编号 ${evidence}）` : ''}。该框选落在此 RX-BX 框内，不是独立取材`
                    : '未落入已有 RX-BX 分析框',
                `图像坐标: x=${rect.x}, y=${rect.y}, w=${rect.w}, h=${rect.h}`,
                mag ? `估计倍率: ${mag}×` : '',
            ].filter(Boolean).join('\n');
        }).filter(Boolean).join('\n\n');
    }

    _shortUserRoiLabel(rois) {
        const isEn = (window.__lang || 'zh') === 'en';
        return (Array.isArray(rois) ? rois : []).map((item) => {
            const id = String(item?.id || '').trim() || 'U?';
            const path = String(item?.parentPath || '').trim();
            if (isEn) return path ? `Analyze new FOV ${id} (inside ${path})` : `Analyze new FOV ${id}`;
            return path ? `请分析新增视野 ${id}（位于 ${path}）` : `请分析新增视野 ${id}`;
        }).join(isEn ? '; ' : '；');
    }

    _outgoingUserRoiMessage(userText, rois) {
        const isEn = (window.__lang || 'zh') === 'en';
        const meta = this._formatUserRoiContext(rois, { isEn });
        const fallback = isEn
            ? 'Please analyze this new FOV in the context of its parent RX-BX path. Cite that path; do not treat it as an independent specimen site.'
            : '请分析这个新增视野，并把它归入所属的 RX-BX 分析路径；不要当成独立取材部位。';
        return [meta, String(userText || '').trim() || fallback].filter(Boolean).join('\n\n');
    }

    _userRoiAnalyzeButtonHtml() {
        const isEn = (window.__lang || 'zh') === 'en';
        const label = isEn ? 'Analyze this' : '新分析这个';
        return `<button type="button" class="user-roi-analyze-btn" data-user-roi-analyze>${this.escapeHtml(label)}</button>`;
    }

    _bindSelectedRoiPreviewActions(root) {
        const host = root?.querySelector?.('.message-content') || root;
        if (!host) return;
        host.querySelectorAll('[data-user-roi-analyze]').forEach((button) => {
            button.addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                this.sendMessage();
            });
        });
    }

    _clickPlaceBoxSize(imageX, imageY) {
        const containing = this._pickContainingAnalysisRoi(imageX, imageY)
            || this._pickInnermostRoiHit(imageX, imageY);
        const parent = containing ? this._roiOverlayRect(containing.overlay) : null;
        if (parent) {
            return Math.max(64, Math.round(Math.min(parent.w, parent.h) / 4));
        }
        if (!this.viewer) return 1024;
        const bounds = this.viewer.viewport.getBounds();
        const imageBounds = this.viewer.viewport.viewportToImageRectangle(bounds);
        const side = Math.round(Math.min(imageBounds.width, imageBounds.height) * 0.42);
        return Math.max(256, Math.min(4096, side));
    }

    _inferMagnificationFromSize(size, parentMag = 0) {
        if (parentMag === 10) return 40;
        if (parentMag === 40) return 160;
        if (size > 8000) return 10;
        if (size > 2500) return 40;
        return 160;
    }

    async placeUserRoiAt(imageX, imageY) {
        const agent = window.pathChatAgent;
        const phase = Number(agent?.currentPhaseData?.phase || agent?.mapEditPhase || 0);
        if (phase >= 1 && phase <= 3 && typeof agent.findUnselectedCandidateAtPoint === 'function') {
            const candidate = agent.findUnselectedCandidateAtPoint({ x: imageX, y: imageY }, phase);
            if (candidate) {
                agent.addROIToSelection(candidate, phase);
                const id = candidate.id || candidate.roi_id;
                await this.announceSelectedUserRoi(id, candidate.rect, {
                    parent_id: candidate.parent_id || '',
                    parentPath: this._normalizeSwarmPath(id) || this._citationPathFromParentId(candidate.parent_id),
                    magnification: candidate.magnification || candidate.zoom_level,
                    slideLabel: this._currentSlideSiteLabel(),
                    slidePrefix: this._currentSlidePathPrefix(),
                });
                return;
            }
        }

        const containing = this._pickContainingAnalysisRoi(imageX, imageY)
            || this._pickInnermostRoiHit(imageX, imageY);
        const parentRect = containing ? this._roiOverlayRect(containing.overlay) : null;
        const parentMag = Number(containing?.overlayData?.magnification) || 0;
        const parentPath = this._citationPathFromOverlay(containing?.overlay);
        const parentId = containing?.overlayData?.id || containing?.overlay?.id || '';
        const size = this._clickPlaceBoxSize(imageX, imageY);
        const rect = {
            x: Math.round(imageX - size / 2),
            y: Math.round(imageY - size / 2),
            w: Math.round(size),
            h: Math.round(size),
        };
        if (parentRect) {
            rect.x = Math.max(parentRect.x, Math.min(rect.x, parentRect.x + parentRect.w - rect.w));
            rect.y = Math.max(parentRect.y, Math.min(rect.y, parentRect.y + parentRect.h - rect.h));
        }
        this._userRoiSerial += 1;
        const id = `U${this._userRoiSerial}`;
        const magnification = this._inferMagnificationFromSize(size, parentMag);
        this.drawColoredROI({
            id,
            rect,
            color: '#7C3AED',
            magnification,
            label: id,
            parent_id: parentId,
            parentPath,
            user_added: true,
        });
        await this.announceSelectedUserRoi(id, rect, {
            parent_id: parentId,
            parentPath,
            magnification,
            slideLabel: this._currentSlideSiteLabel(),
            slidePrefix: this._currentSlidePathPrefix(),
        });
    }

    async announceSelectedUserRoi(roiId, rect, extra = {}) {
        const box = rect || {};
        this.currentROI = {
            x: Math.round(Number(box.x || 0)),
            y: Math.round(Number(box.y || 0)),
            width: Math.round(Number(box.w ?? box.width ?? 0)),
            height: Math.round(Number(box.h ?? box.height ?? 0)),
        };
        let imageData = '';
        if (this.currentROI.width > 0 && this.currentROI.height > 0) {
            imageData = await this.captureImageRect({
                x: this.currentROI.x,
                y: this.currentROI.y,
                width: this.currentROI.width,
                height: this.currentROI.height,
            }, { silent: true, skipPreview: true }) || '';
        }
        this.selectedUserRois = Array.isArray(this.selectedUserRois) ? this.selectedUserRois : [];
        this.selectedUserRois.push({
            id: String(roiId || '').trim(),
            rect: { ...this.currentROI },
            imageData,
            parent_id: extra.parent_id || '',
            parentPath: extra.parentPath || this._normalizeSwarmPath(roiId) || '',
            magnification: extra.magnification || null,
            slideLabel: extra.slideLabel || this._currentSlideSiteLabel(),
            slidePrefix: extra.slidePrefix || this._currentSlidePathPrefix(),
        });
        this.renderUserRoiTray();
        this._upsertSelectedRoiChatPreview();
        const input = document.getElementById('chatInput');
        if (input) {
            input.focus();
            input.dispatchEvent(new Event('input', { bubbles: true }));
        }
    }

    renderUserRoiTray() {
        const previewContainer = document.getElementById('screenshotPreview');
        const tray = document.getElementById('userRoiTray');
        const header = document.getElementById('userRoiTrayLabel');
        const fallbackImg = document.getElementById('screenshotImage');
        const rois = (this.selectedUserRois || []).filter((item) => item?.imageData);
        const isEn = (window.__lang || 'zh') === 'en';
        const fallbackWrap = fallbackImg?.closest('.screenshot-preview-image');
        if (!previewContainer) return;
        if (!rois.length) {
            if (this.currentScreenshot?.imageData && fallbackImg) {
                fallbackImg.hidden = false;
                fallbackImg.src = `data:image/jpeg;base64,${this.currentScreenshot.imageData}`;
                if (fallbackWrap) fallbackWrap.hidden = false;
                if (tray) {
                    tray.hidden = true;
                    tray.innerHTML = '';
                }
                previewContainer.style.display = 'block';
                if (header) header.textContent = isEn ? 'Region captured' : '已截取区域';
                this.updateSendButton();
                return;
            }
            previewContainer.style.display = 'none';
            this.hideROISelectionInfo();
            this.updateSendButton();
            return;
        }
        previewContainer.style.display = 'block';
        if (fallbackImg) {
            fallbackImg.hidden = true;
            fallbackImg.removeAttribute('src');
        }
        if (fallbackWrap) fallbackWrap.hidden = true;
        const ids = rois.map((item) => item.id).filter(Boolean).join(', ');
        if (header) {
            header.textContent = isEn
                ? `${rois.length} selected ROI${rois.length > 1 ? 's' : ''}: ${ids}`
                : `已选中 ${rois.length} 个新增 ROI：${ids}`;
        }
        const infoLabel = document.querySelector('#roiSelectionInfo .roi-info span');
        if (infoLabel) {
            infoLabel.textContent = isEn
                ? `Selected new ROI ${ids}`
                : `已选中新增 ROI ${ids}`;
        }
        this.showROISelectionInfo();
        if (!tray) {
            this.updateSendButton();
            return;
        }
        tray.hidden = false;
        tray.innerHTML = rois.map((item) => {
            const src = String(item.imageData || '').startsWith('data:image/')
                ? item.imageData
                : `data:image/jpeg;base64,${item.imageData}`;
            return `<button type="button" class="user-roi-thumb" data-roi-id="${this.escapeHtml(item.id)}" title="${this.escapeHtml(item.id)}">
                <img src="${src}" alt="${this.escapeHtml(item.id)}">
                <span>${this.escapeHtml(item.id)}</span>
            </button>`;
        }).join('');
        tray.querySelectorAll('.user-roi-thumb').forEach((button) => {
            button.addEventListener('click', () => {
                const roi = rois.find((item) => item.id === button.dataset.roiId);
                if (roi?.rect) this.jumpToROIView({ id: roi.id, rect: roi.rect });
            });
        });
        this.updateSendButton();
    }

    _upsertSelectedRoiChatPreview() {
        const rois = Array.isArray(this.selectedUserRois) ? this.selectedUserRois : [];
        if (!rois.length) return;
        const isEn = (window.__lang || 'zh') === 'en';
        const ids = rois.map((item) => item.id).filter(Boolean).join('、');
        const paths = [...new Set(rois.map((item) => item.parentPath).filter(Boolean))];
        const pathNote = paths.length
            ? (isEn ? ` Inside ${paths.join(', ')}.` : `（位于 ${paths.join('、')}）`)
            : '';
        const text = isEn
            ? `Selected new ROI${rois.length > 1 ? 's' : ''} ${ids}.${pathNote}`
            : `已选中新增 ROI ${ids}${pathNote}。`;
        const withImages = rois.filter((item) => item?.imageData);
        const existing = this._userRoiSelectionMsg;
        const previewHtml = `${this._buildScreenshotMessageHtml(
            text,
            withImages.length ? withImages : null
        )}${this._userRoiAnalyzeButtonHtml()}`;
        if (existing?.parentNode) {
            const content = existing.querySelector('.message-content');
            if (content) {
                content.innerHTML = previewHtml;
                this._bindSelectedRoiPreviewActions(existing);
                const chatMessages = this.elements?.chatMessages;
                if (chatMessages) chatMessages.scrollTop = chatMessages.scrollHeight;
                return;
            }
        }
        this._userRoiSelectionMsg = withImages.length
            ? this.addMessageWithScreenshot('assistant', text, withImages)
            : this.addMessage('assistant', text);
        const host = this._userRoiSelectionMsg?.querySelector?.('.message-content');
        if (host && !host.querySelector('[data-user-roi-analyze]')) {
            host.insertAdjacentHTML('beforeend', this._userRoiAnalyzeButtonHtml());
        }
        this._bindSelectedRoiPreviewActions(this._userRoiSelectionMsg);
    }

    // ================== 截图询问功能 ==================
    
    /**
     * 切换截图模式
     */
    toggleScreenshotMode() {
        if (!this.viewer) {
            this.showNotification('warning', (window.__lang||'zh')==='en' ? 'Please load a WSI file first' : '请先加载WSI文件');
            return;
        }
        
        this.screenshotMode = !this.screenshotMode;
        
        const screenshotBtn = document.getElementById('screenshotAskBtn');
        const viewerContainer = document.getElementById('openseadragon-viewer');
        
        if (this.screenshotMode) {
            if (this.clickPlaceRoiMode) this.toggleClickPlaceRoiMode(false);
            // 进入截图模式
            screenshotBtn.classList.add('active');
            viewerContainer.classList.add('screenshot-mode');
            this.showNotification('info', (window.__lang||'zh')==='en' ? 'Screenshot mode on. Drag to select a region.' : '截图模式已开启，请在视图中拖拽选择区域');
            
            // 禁用OpenSeadragon的默认拖拽行为
            this.viewer.setMouseNavEnabled(false);
            
            // 设置截图选择事件
            this.setupScreenshotSelection();
            
            console.log('📸 截图模式已开启');
        } else {
            // 退出截图模式
            screenshotBtn.classList.remove('active');
            viewerContainer.classList.remove('screenshot-mode');
            
            // 恢复OpenSeadragon的默认拖拽行为
            this.viewer.setMouseNavEnabled(true);
            
            // 清除截图选择事件
            this.removeScreenshotSelection();
            
            console.log('📸 截图模式已关闭');
        }
    }
    
    /**
     * 设置截图选择事件
     */
    setupScreenshotSelection() {
        const viewerContainer = document.getElementById('openseadragon-viewer');
        
        // 创建截图选择覆盖层
        this.screenshotOverlayElement = document.createElement('div');
        this.screenshotOverlayElement.className = 'screenshot-selection-overlay';
        this.screenshotOverlayElement.style.display = 'none';
        viewerContainer.appendChild(this.screenshotOverlayElement);
        
        // 截图选择状态
        this.screenshotSelecting = false;
        this.screenshotStartPoint = null;
        
        // 鼠标事件处理
        this._screenshotMouseDown = (e) => {
            if (!this.screenshotMode) return;
            
            e.preventDefault();
            e.stopPropagation();
            
            const rect = viewerContainer.getBoundingClientRect();
            this.screenshotStartPoint = {
                x: e.clientX - rect.left,
                y: e.clientY - rect.top
            };
            this.screenshotSelecting = true;
            
            this.screenshotOverlayElement.style.display = 'block';
            this.screenshotOverlayElement.style.left = this.screenshotStartPoint.x + 'px';
            this.screenshotOverlayElement.style.top = this.screenshotStartPoint.y + 'px';
            this.screenshotOverlayElement.style.width = '0px';
            this.screenshotOverlayElement.style.height = '0px';
        };
        
        this._screenshotMouseMove = (e) => {
            if (!this.screenshotSelecting || !this.screenshotStartPoint) return;
            
            const rect = viewerContainer.getBoundingClientRect();
            const currentPoint = {
                x: e.clientX - rect.left,
                y: e.clientY - rect.top
            };
            
            const left = Math.min(this.screenshotStartPoint.x, currentPoint.x);
            const top = Math.min(this.screenshotStartPoint.y, currentPoint.y);
            const width = Math.abs(currentPoint.x - this.screenshotStartPoint.x);
            const height = Math.abs(currentPoint.y - this.screenshotStartPoint.y);
            
            this.screenshotOverlayElement.style.left = left + 'px';
            this.screenshotOverlayElement.style.top = top + 'px';
            this.screenshotOverlayElement.style.width = width + 'px';
            this.screenshotOverlayElement.style.height = height + 'px';
        };
        
        this._screenshotMouseUp = async (e) => {
            if (!this.screenshotSelecting) return;
            
            this.screenshotSelecting = false;
            
            const rect = viewerContainer.getBoundingClientRect();
            const endPoint = {
                x: e.clientX - rect.left,
                y: e.clientY - rect.top
            };
            
            const left = Math.min(this.screenshotStartPoint.x, endPoint.x);
            const top = Math.min(this.screenshotStartPoint.y, endPoint.y);
            const width = Math.abs(endPoint.x - this.screenshotStartPoint.x);
            const height = Math.abs(endPoint.y - this.screenshotStartPoint.y);
            
            // 检查选择区域是否足够大
            if (width < 20 || height < 20) {
                this.showNotification('warning', (window.__lang||'zh')==='en' ? 'Selected region too small, please try again' : '选择区域太小，请重新选择');
                this.screenshotOverlayElement.style.display = 'none';
                return;
            }
            
            // 捕获选中区域
            await this.captureScreenshotArea(left, top, width, height);
            
            // 隐藏选择覆盖层
            this.screenshotOverlayElement.style.display = 'none';
            
            // 退出截图模式
            this.toggleScreenshotMode();
        };
        
        viewerContainer.addEventListener('mousedown', this._screenshotMouseDown);
        viewerContainer.addEventListener('mousemove', this._screenshotMouseMove);
        viewerContainer.addEventListener('mouseup', this._screenshotMouseUp);
    }
    
    /**
     * 移除截图选择事件
     */
    removeScreenshotSelection() {
        const viewerContainer = document.getElementById('openseadragon-viewer');
        
        if (this._screenshotMouseDown) {
            viewerContainer.removeEventListener('mousedown', this._screenshotMouseDown);
        }
        if (this._screenshotMouseMove) {
            viewerContainer.removeEventListener('mousemove', this._screenshotMouseMove);
        }
        if (this._screenshotMouseUp) {
            viewerContainer.removeEventListener('mouseup', this._screenshotMouseUp);
        }
        
        // 移除覆盖层
        if (this.screenshotOverlayElement) {
            this.screenshotOverlayElement.remove();
            this.screenshotOverlayElement = null;
        }
    }
    
    /**
     * 捕获截图区域
     */
    async captureScreenshotArea(left, top, width, height) {
        try {
            console.log('📸 捕获截图区域:', { left, top, width, height });
            
            // 方法1: 使用html2canvas捕获（如果可用）
            // 方法2: 使用OpenSeadragon的viewport转换并从后端获取图像
            
            // 获取OpenSeadragon视口信息
            const viewerContainer = document.getElementById('openseadragon-viewer');
            const containerRect = viewerContainer.getBoundingClientRect();
            
            // 转换屏幕坐标到视口坐标
            const viewportTopLeft = this.viewer.viewport.pointFromPixel(
                new OpenSeadragon.Point(left, top)
            );
            const viewportBottomRight = this.viewer.viewport.pointFromPixel(
                new OpenSeadragon.Point(left + width, top + height)
            );
            
            // 转换视口坐标到图像坐标
            const imageSize = this.viewer.world.getItemAt(0).getContentSize();
            const imageTopLeft = this.viewer.viewport.viewportToImageCoordinates(viewportTopLeft);
            const imageBottomRight = this.viewer.viewport.viewportToImageCoordinates(viewportBottomRight);
            
            const imageRect = {
                x: Math.max(0, Math.round(imageTopLeft.x)),
                y: Math.max(0, Math.round(imageTopLeft.y)),
                width: Math.min(Math.round(imageBottomRight.x - imageTopLeft.x), imageSize.x),
                height: Math.min(Math.round(imageBottomRight.y - imageTopLeft.y), imageSize.y)
            };
            await this.captureImageRect(imageRect);
        } catch (error) {
            console.error('❌ 截图失败:', error);
            this.showNotification('error', (window.__lang||'zh')==='en' ? 'Screenshot failed: ' + error.message : '截图失败：' + error.message);
        }
    }

    async captureImageRect(imageRect, options = {}) {
        try {
            if (!this.currentFile?.file_id || !imageRect) return;
            const response = await fetch('/api/capture_screenshot', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    file_id: this.currentFile.file_id,
                    rect: imageRect
                })
            });
            
            const result = await response.json();
            
            if (result.success) {
                this.currentScreenshot = {
                    imageData: result.image_data,
                    rect: imageRect,
                    timestamp: Date.now()
                };
                if (!options.skipPreview) {
                    if ((this.selectedUserRois || []).length) this.renderUserRoiTray();
                    else this.showScreenshotPreview(result.image_data);
                }
                if (!options.silent) {
                    this.showNotification('success', (window.__lang||'zh')==='en' ? 'Region captured. You can now type a question.' : '已截取区域，可以输入问题进行询问');
                }
                return result.image_data;
            }
            this.showNotification('error', (window.__lang||'zh')==='en' ? 'Screenshot failed: ' + result.error : '截图失败：' + result.error);
            return '';
        } catch (error) {
            console.error('❌ 截图失败:', error);
            this.showNotification('error', (window.__lang||'zh')==='en' ? 'Screenshot failed: ' + error.message : '截图失败：' + error.message);
            return '';
        }
    }
    
    /**
     * 显示截图预览
     */
    showScreenshotPreview(imageData) {
        if ((this.selectedUserRois || []).some((item) => item?.imageData)) {
            this.renderUserRoiTray();
            return;
        }
        const previewContainer = document.getElementById('screenshotPreview');
        const previewImage = document.getElementById('screenshotImage');
        const tray = document.getElementById('userRoiTray');
        
        if (previewContainer && previewImage) {
            previewImage.hidden = false;
            previewImage.src = `data:image/jpeg;base64,${imageData}`;
            const fallbackWrap = previewImage.closest('.screenshot-preview-image');
            if (fallbackWrap) fallbackWrap.hidden = false;
            if (tray) {
                tray.hidden = true;
                tray.innerHTML = '';
            }
            previewContainer.style.display = 'block';
            
            // 更新发送按钮状态
            this.updateSendButton();
        }
    }
    
    /**
     * 清除截图
     */
    clearScreenshot() {
        this.currentScreenshot = null;
        this.selectedUserRois = [];
        this._userRoiSelectionMsg = null;
        
        const previewContainer = document.getElementById('screenshotPreview');
        const previewImage = document.getElementById('screenshotImage');
        const tray = document.getElementById('userRoiTray');
        
        if (previewContainer) {
            previewContainer.style.display = 'none';
        }
        if (previewImage) {
            previewImage.src = '';
            previewImage.hidden = false;
            const fallbackWrap = previewImage.closest('.screenshot-preview-image');
            if (fallbackWrap) fallbackWrap.hidden = false;
        }
        if (tray) {
            tray.innerHTML = '';
            tray.hidden = true;
        }
        this.hideROISelectionInfo();
        
        // 更新发送按钮状态
        this.updateSendButton();
        
        console.log('🗑️ 已清除截图');
    }
}

// 全局实例
let pathchat = null;

// 导出到全局作用域（先指向null，占位避免引用错误）
window.pathchat = pathchat;

// 页面加载完成后初始化。脚本也可能在 DOMContentLoaded 之后由缓存、
// 桌面壳或测试路由装载，因此必须支持立即初始化。
function initializePathChatApp() {
    if (window.pathchat) return;
    pathchat = new PathChatApp();
    window.pathchat = pathchat;
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializePathChatApp, { once: true });
} else {
    initializePathChatApp();
}
