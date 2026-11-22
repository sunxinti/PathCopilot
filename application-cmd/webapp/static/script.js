/**
 * PathCopilot - 前端JavaScript功能
 * 处理WSI文件上传、ROI选择和GPT分析
 */

class PathChatApp {
    constructor() {
        this.currentFile = null;
        this.currentROI = null;
        this.isDrawing = false;
        this.startPoint = null;
        this.canvas = null;
        this.ctx = null;
        this.image = null;
        this.scale = 1;
        this.offsetX = 0;
        this.offsetY = 0;
        this.minScale = 0.1;
        this.maxScale = 20.0;  // 增加最大缩放比例
        this.isDragging = false;
        this.lastPanPoint = null;
        this.currentLevel = 0;
        this.levelImages = {};  // 缓存不同层级的图像
        this.loadingTiles = new Set();  // 正在加载的瓦片
        this.updateImageLevelTimeout = null;  // 防抖定时器
        
        this.init();
    }

    init() {
        this.setupEventListeners();
        this.loadExistingFiles();
    }

    setupEventListeners() {
        // 文件上传
        const uploadArea = document.getElementById('uploadArea');
        const fileInput = document.getElementById('fileInput');

        uploadArea.addEventListener('click', () => fileInput.click());
        uploadArea.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadArea.classList.add('drag-over');
        });
        uploadArea.addEventListener('dragleave', () => {
            uploadArea.classList.remove('drag-over');
        });
        uploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadArea.classList.remove('drag-over');
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                this.uploadFile(files[0]);
            }
        });

        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                this.uploadFile(e.target.files[0]);
            }
        });

        // 分析按钮
        document.getElementById('analyzeBtn').addEventListener('click', () => {
            this.analyzeROI();
        });

        // 工具栏按钮
        document.getElementById('zoomIn').addEventListener('click', () => {
            if (this.viewer) {
                this.viewer.viewport.zoomBy(1.5);
            } else {
                this.zoomIn();
            }
        });
        document.getElementById('zoomOut').addEventListener('click', () => {
            if (this.viewer) {
                this.viewer.viewport.zoomBy(0.67);
            } else {
                this.zoomOut();
            }
        });
        document.getElementById('clearROI').addEventListener('click', () => {
            this.clearROI();
        });
        document.getElementById('resetView').addEventListener('click', () => {
            if (this.viewer) {
                this.viewer.viewport.goHome();
            } else {
                this.resetView();
            }
        });

        // 结果面板
        document.getElementById('closeResults').addEventListener('click', () => {
            document.getElementById('resultsPanel').style.display = 'none';
        });
    }

    async uploadFile(file) {
        // 检查文件类型
        const allowedTypes = ['svs', 'ndpi', 'vms', 'vmu', 'scn', 'mrxs', 'tiff', 'tif'];
        const fileExtension = file.name.split('.').pop().toLowerCase();
        
        if (!allowedTypes.includes(fileExtension)) {
            this.showError('不支持的文件格式。请选择WSI文件。');
            return;
        }

        // 检查文件大小
        if (file.size > 2 * 1024 * 1024 * 1024) {
            this.showError('文件太大。请选择小于2GB的文件。');
            return;
        }

        // 显示上传进度
        this.showUploadProgress(true);
        
        const formData = new FormData();
        formData.append('file', file);

        try {
            const response = await fetch('/api/upload', {
                method: 'POST',
                body: formData
            });

            const result = await response.json();
            
            if (result.success) {
                this.currentFile = result;
                this.displayImage(result);
                this.updateFilesList();
                this.showUploadProgress(false);
                this.showSuccess('文件上传成功！');
            } else {
                throw new Error(result.error);
            }
        } catch (error) {
            this.showError(`上传失败: ${error.message}`);
            this.showUploadProgress(false);
        }
    }

    showUploadProgress(show) {
        const progressDiv = document.getElementById('uploadProgress');
        if (show) {
            progressDiv.style.display = 'block';
            // 不定进度动画（上传期间无实际进度数据）
            const progressFill = document.getElementById('progressFill');
            let progress = 0;
            const interval = setInterval(() => {
                progress += Math.random() * 10;
                if (progress >= 90) {
                    clearInterval(interval);
                    progress = 90;
                }
                progressFill.style.width = `${progress}%`;
            }, 200);
        } else {
            progressDiv.style.display = 'none';
            document.getElementById('progressFill').style.width = '0%';
        }
    }

    displayImage(fileInfo) {
        const imageViewer = document.getElementById('imageViewer');
        const placeholder = document.getElementById('placeholder');
        const canvas = document.getElementById('imageCanvas');
        const infoPanel = document.getElementById('infoPanel');

        // 隐藏占位符，显示画布
        placeholder.style.display = 'none';
        canvas.style.display = 'block';
        infoPanel.style.display = 'block';

        // 存储文件信息以便后续使用
        this.currentFile = fileInfo;
        
        // 首先生成DZI瓦片
        this.generateDZIAndInitViewer();
        
        // 初始化智能缩放系统
        this.levelImages = {};
        
        // 加载初始图像（使用现有缩略图）
        this.image = new Image();
        this.image.onload = () => {
            // 缓存初始图像
            this.levelImages['initial'] = this.image;
            this.fitImageToCanvas();
            this.setupCanvasEvents();
            this.showQualityInfo(this.image.width, this.image.height, 'initial');
            
            // 延迟加载更高质量的图像
            setTimeout(() => {
                this.updateImageLevel();
            }, 500);
        };
        this.image.src = fileInfo.thumbnail_url;

        // 更新信息面板
        document.getElementById('originalDimensions').textContent = 
            `${fileInfo.dimensions[0]} × ${fileInfo.dimensions[1]}`;
        document.getElementById('levelCount').textContent = fileInfo.level_count;
        document.getElementById('fileInfo').textContent = 
            `${fileInfo.filename} (${Math.round(fileInfo.dimensions[0] / 1000)}K × ${Math.round(fileInfo.dimensions[1] / 1000)}K)`;
    }

    fitImageToCanvas() {
        const container = this.canvas.parentElement;
        const containerWidth = container.clientWidth - 20;
        const containerHeight = container.clientHeight - 20;
        
        // 设置画布大小为容器大小，考虑设备像素比以获得更清晰的显示
        const dpr = window.devicePixelRatio || 1;
        this.canvas.width = containerWidth * dpr;
        this.canvas.height = containerHeight * dpr;
        
        // 设置CSS尺寸
        this.canvas.style.width = containerWidth + 'px';
        this.canvas.style.height = containerHeight + 'px';
        
        // 缩放上下文以匹配设备像素比
        this.ctx.scale(dpr, dpr);
        
        // 启用图像平滑以获得更好的质量
        this.ctx.imageSmoothingEnabled = true;
        this.ctx.imageSmoothingQuality = 'high';
        
        // 计算适合的缩放比例
        const scaleX = containerWidth / this.image.width;
        const scaleY = containerHeight / this.image.height;
        this.scale = Math.min(scaleX, scaleY);
        
        // 居中显示
        this.offsetX = (containerWidth - this.image.width * this.scale) / 2;
        this.offsetY = (containerHeight - this.image.height * this.scale) / 2;
        
        this.redrawCanvas();
    }

    setupCanvasEvents() {
        this.canvas.addEventListener('mousedown', (e) => this.onMouseDown(e));
        this.canvas.addEventListener('mousemove', (e) => this.onMouseMove(e));
        this.canvas.addEventListener('mouseup', (e) => this.onMouseUp(e));
        this.canvas.addEventListener('mouseleave', (e) => this.onMouseUp(e));
        
        // 滚轮缩放事件
        this.canvas.addEventListener('wheel', (e) => this.onWheel(e));
        
        // 禁用右键菜单
        this.canvas.addEventListener('contextmenu', (e) => e.preventDefault());
        
        // 触摸事件支持
        this.canvas.addEventListener('touchstart', (e) => {
            e.preventDefault();
            if (e.touches.length === 1) {
                const touch = e.touches[0];
                const mouseEvent = new MouseEvent('mousedown', {
                    clientX: touch.clientX,
                    clientY: touch.clientY
                });
                this.canvas.dispatchEvent(mouseEvent);
            } else if (e.touches.length === 2) {
                // 双指缩放开始
                this.handleTouchZoomStart(e);
            }
        });
        
        this.canvas.addEventListener('touchmove', (e) => {
            e.preventDefault();
            if (e.touches.length === 1) {
                const touch = e.touches[0];
                const mouseEvent = new MouseEvent('mousemove', {
                    clientX: touch.clientX,
                    clientY: touch.clientY
                });
                this.canvas.dispatchEvent(mouseEvent);
            } else if (e.touches.length === 2) {
                // 双指缩放
                this.handleTouchZoom(e);
            }
        });
        
        this.canvas.addEventListener('touchend', (e) => {
            e.preventDefault();
            const mouseEvent = new MouseEvent('mouseup', {});
            this.canvas.dispatchEvent(mouseEvent);
        });
    }

    onMouseDown(e) {
        const rect = this.canvas.getBoundingClientRect();
        const point = {
            x: e.clientX - rect.left,
            y: e.clientY - rect.top
        };
        
        // 右键或Ctrl+左键用于平移
        if (e.button === 2 || (e.button === 0 && e.ctrlKey)) {
            this.isDragging = true;
            this.lastPanPoint = point;
            this.canvas.style.cursor = 'grabbing';
            return;
        }
        
        // 左键用于ROI选择
        if (e.button === 0) {
            this.startPoint = point;
            this.isDrawing = true;
            this.canvas.style.cursor = 'crosshair';
        }
    }

    onMouseMove(e) {
        const rect = this.canvas.getBoundingClientRect();
        const currentPoint = {
            x: e.clientX - rect.left,
            y: e.clientY - rect.top
        };

        // 处理平移
        if (this.isDragging && this.lastPanPoint) {
            const deltaX = currentPoint.x - this.lastPanPoint.x;
            const deltaY = currentPoint.y - this.lastPanPoint.y;
            
            this.offsetX += deltaX;
            this.offsetY += deltaY;
            
            this.lastPanPoint = currentPoint;
            this.redrawCanvas();
            return;
        }

        // 处理ROI选择
        if (this.isDrawing && this.startPoint) {
            // 将屏幕坐标转换为图像坐标
            const startImagePoint = this.screenToImage(this.startPoint);
            const currentImagePoint = this.screenToImage(currentPoint);
            
            const roi = {
                x: Math.min(startImagePoint.x, currentImagePoint.x),
                y: Math.min(startImagePoint.y, currentImagePoint.y),
                width: Math.abs(currentImagePoint.x - startImagePoint.x),
                height: Math.abs(currentImagePoint.y - startImagePoint.y)
            };

            this.currentROI = roi;
            this.redrawCanvas();
            this.updateROIInfo();
        }
    }

    onMouseUp(e) {
        // 结束平移
        if (this.isDragging) {
            this.isDragging = false;
            this.lastPanPoint = null;
            this.canvas.style.cursor = 'default';
            return;
        }
        
        // 结束ROI选择
        if (this.isDrawing) {
            this.isDrawing = false;
            this.canvas.style.cursor = 'default';
            
            // 检查ROI是否有效
            if (this.currentROI && this.currentROI.width > 10 && this.currentROI.height > 10) {
                document.getElementById('analyzeBtn').disabled = false;
                document.getElementById('clearROI').disabled = false;
            }
        }
    }

    onWheel(e) {
        e.preventDefault();
        
        const rect = this.canvas.getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const mouseY = e.clientY - rect.top;
        
        // 计算缩放因子
        const scaleFactor = e.deltaY > 0 ? 0.9 : 1.1;
        const newScale = this.scale * scaleFactor;
        
        // 限制缩放范围
        if (newScale < this.minScale || newScale > this.maxScale) {
            return;
        }
        
        // 计算缩放中心点（鼠标位置）
        const imageX = (mouseX - this.offsetX) / this.scale;
        const imageY = (mouseY - this.offsetY) / this.scale;
        
        // 更新缩放比例
        this.scale = newScale;
        
        // 调整偏移量，使缩放以鼠标位置为中心
        this.offsetX = mouseX - imageX * this.scale;
        this.offsetY = mouseY - imageY * this.scale;
        
        this.redrawCanvas();
        this.updateZoomInfo();
        this.debouncedUpdateImageLevel();
    }

    // 根据缩放比例获取合适的图像尺寸
    getTargetImageSize() {
        const containerWidth = this.canvas.clientWidth;
        const containerHeight = this.canvas.clientHeight;
        const maxDim = Math.max(containerWidth, containerHeight);
        
        // 根据缩放比例计算需要的图像尺寸
        let targetSize = Math.round(maxDim * this.scale);
        
        // 限制在合理范围内
        targetSize = Math.max(512, Math.min(targetSize, 4096));
        
        console.log(`缩放 ${this.scale.toFixed(2)} -> 目标尺寸 ${targetSize}px`);
        return targetSize;
    }

    // 防抖的图像质量更新
    debouncedUpdateImageLevel() {
        // 清除之前的定时器
        if (this.updateImageLevelTimeout) {
            clearTimeout(this.updateImageLevelTimeout);
        }
        
        // 设置新的定时器
        this.updateImageLevelTimeout = setTimeout(() => {
            this.updateImageLevel();
        }, 300); // 300ms 防抖
    }

    // 更新图像质量
    async updateImageLevel() {
        if (!this.currentFile) return;
        
        const targetSize = this.getTargetImageSize();
        const cacheKey = `size_${targetSize}`;
        
        // 如果已经有合适质量的图像，就不需要重新加载
        if (this.levelImages[cacheKey]) {
            if (this.image !== this.levelImages[cacheKey]) {
                this.image = this.levelImages[cacheKey];
                this.redrawCanvas();
                console.log(`使用缓存图像: ${targetSize}px`);
            }
            return;
        }

        // 避免重复加载
        if (this.loadingTiles.has(cacheKey)) {
            return;
        }

        // 开始加载更高质量的图像
        await this.loadSmartThumbnail(targetSize, cacheKey);
    }

    // 加载智能缩略图
    async loadSmartThumbnail(targetSize, cacheKey) {
        this.loadingTiles.add(cacheKey);
        
        // 只在明显提升质量时才显示加载指示器
        const currentQuality = this.image ? Math.max(this.image.width, this.image.height) : 0;
        const qualityImprovement = targetSize / currentQuality;
        
        if (qualityImprovement > 1.5) {
            this.showLoadingIndicator(`正在加载更清晰的图像...`);
        }

        try {
            const thumbnailUrl = `/api/get_smart_thumbnail/${this.currentFile.file_id}/${targetSize}`;
            
            const img = new Image();
            img.onload = () => {
                this.levelImages[cacheKey] = img;
                this.image = img;
                this.redrawCanvas();
                this.loadingTiles.delete(cacheKey);
                
                // 更新信息显示
                this.showQualityInfo(img.width, img.height, targetSize);
                this.hideLoadingIndicator();
                
                console.log(`加载完成: ${img.width}x${img.height}px`);
            };
            
            img.onerror = () => {
                console.error(`加载缩略图失败: ${targetSize}px`);
                this.loadingTiles.delete(cacheKey);
                this.hideLoadingIndicator();
            };
            
            img.src = thumbnailUrl;
            
        } catch (error) {
            console.error('加载智能缩略图失败:', error);
            this.loadingTiles.delete(cacheKey);
            this.hideLoadingIndicator();
        }
    }

    // 显示加载指示器
    showLoadingIndicator(message) {
        let indicator = document.getElementById('loadingIndicator');
        if (!indicator) {
            indicator = document.createElement('div');
            indicator.id = 'loadingIndicator';
            indicator.className = 'loading-indicator';
            indicator.innerHTML = `
                <div class="loading-content">
                    <i class="fas fa-spinner fa-spin"></i>
                    <span id="loadingMessage">${message}</span>
                </div>
            `;
            document.body.appendChild(indicator);
        } else {
            document.getElementById('loadingMessage').textContent = message;
            indicator.style.display = 'flex';
        }
    }

    // 隐藏加载指示器
    hideLoadingIndicator() {
        const indicator = document.getElementById('loadingIndicator');
        if (indicator) {
            indicator.style.display = 'none';
        }
    }

    // 显示质量信息
    showQualityInfo(imageWidth, imageHeight, targetSize) {
        const qualityInfo = document.getElementById('imageQuality');
        if (!qualityInfo) {
            const infoPanel = document.getElementById('infoPanel');
            if (infoPanel) {
                const qualityDiv = document.createElement('div');
                qualityDiv.className = 'info-item';
                qualityDiv.innerHTML = `
                    <span>图像质量：</span>
                    <span id="imageQuality">${imageWidth}×${imageHeight}px</span>
                `;
                infoPanel.appendChild(qualityDiv);
            }
        } else {
            qualityInfo.textContent = `${imageWidth}×${imageHeight}px`;
        }
    }

    // 坐标转换函数
    screenToImage(screenPoint) {
        return {
            x: (screenPoint.x - this.offsetX) / this.scale,
            y: (screenPoint.y - this.offsetY) / this.scale
        };
    }

    imageToScreen(imagePoint) {
        return {
            x: imagePoint.x * this.scale + this.offsetX,
            y: imagePoint.y * this.scale + this.offsetY
        };
    }

    // 触摸缩放支持
    handleTouchZoomStart(e) {
        const touch1 = e.touches[0];
        const touch2 = e.touches[1];
        
        this.lastTouchDistance = Math.sqrt(
            Math.pow(touch2.clientX - touch1.clientX, 2) +
            Math.pow(touch2.clientY - touch1.clientY, 2)
        );
        
        const rect = this.canvas.getBoundingClientRect();
        this.lastTouchCenter = {
            x: (touch1.clientX + touch2.clientX) / 2 - rect.left,
            y: (touch1.clientY + touch2.clientY) / 2 - rect.top
        };
    }

    handleTouchZoom(e) {
        const touch1 = e.touches[0];
        const touch2 = e.touches[1];
        
        const currentDistance = Math.sqrt(
            Math.pow(touch2.clientX - touch1.clientX, 2) +
            Math.pow(touch2.clientY - touch1.clientY, 2)
        );
        
        const rect = this.canvas.getBoundingClientRect();
        const currentCenter = {
            x: (touch1.clientX + touch2.clientX) / 2 - rect.left,
            y: (touch1.clientY + touch2.clientY) / 2 - rect.top
        };
        
        if (this.lastTouchDistance && this.lastTouchCenter) {
            const scaleFactor = currentDistance / this.lastTouchDistance;
            const newScale = this.scale * scaleFactor;
            
            if (newScale >= this.minScale && newScale <= this.maxScale) {
                const imageX = (this.lastTouchCenter.x - this.offsetX) / this.scale;
                const imageY = (this.lastTouchCenter.y - this.offsetY) / this.scale;
                
                this.scale = newScale;
                this.offsetX = currentCenter.x - imageX * this.scale;
                this.offsetY = currentCenter.y - imageY * this.scale;
                
                this.redrawCanvas();
                this.updateZoomInfo();
            }
        }
        
        this.lastTouchDistance = currentDistance;
        this.lastTouchCenter = currentCenter;
    }

    redrawCanvas() {
        const containerWidth = this.canvas.clientWidth;
        const containerHeight = this.canvas.clientHeight;
        
        this.ctx.clearRect(0, 0, containerWidth, containerHeight);
        
        // 保存上下文状态
        this.ctx.save();
        
        // 确保图像平滑设置
        this.ctx.imageSmoothingEnabled = true;
        this.ctx.imageSmoothingQuality = 'high';
        
        // 应用变换（缩放和平移）
        this.ctx.translate(this.offsetX, this.offsetY);
        this.ctx.scale(this.scale, this.scale);
        
        // 绘制图像
        this.ctx.drawImage(this.image, 0, 0, this.image.width, this.image.height);
        
        // 绘制ROI选框
        if (this.currentROI) {
            this.ctx.strokeStyle = '#ff4444';
            this.ctx.lineWidth = 2 / this.scale; // 保持线宽不随缩放变化
            this.ctx.setLineDash([5 / this.scale, 5 / this.scale]); // 保持虚线间距
            this.ctx.strokeRect(
                this.currentROI.x,
                this.currentROI.y,
                this.currentROI.width,
                this.currentROI.height
            );
            
            // 绘制半透明覆盖
            this.ctx.fillStyle = 'rgba(255, 68, 68, 0.1)';
            this.ctx.fillRect(
                this.currentROI.x,
                this.currentROI.y,
                this.currentROI.width,
                this.currentROI.height
            );
            
            this.ctx.setLineDash([]);
        }
        
        // 恢复上下文状态
        this.ctx.restore();
    }

    updateROIInfo() {
        if (this.currentROI) {
            const roiInfo = `${Math.round(this.currentROI.width)} × ${Math.round(this.currentROI.height)}`;
            document.getElementById('currentROI').textContent = roiInfo;
        } else {
            document.getElementById('currentROI').textContent = '未选择';
        }
    }

    updateZoomInfo() {
        // 在信息面板添加缩放信息
        const zoomInfo = document.getElementById('zoomInfo');
        if (!zoomInfo) {
            // 如果不存在缩放信息元素，创建一个
            const infoPanel = document.getElementById('infoPanel');
            if (infoPanel) {
                const zoomDiv = document.createElement('div');
                zoomDiv.className = 'info-item';
                zoomDiv.innerHTML = `
                    <span>缩放比例：</span>
                    <span id="zoomInfo">${Math.round(this.scale * 100)}%</span>
                `;
                infoPanel.appendChild(zoomDiv);
            }
        } else {
            zoomInfo.textContent = `${Math.round(this.scale * 100)}%`;
        }
    }

    clearROI() {
        this.currentROI = null;
        this.redrawCanvas();
        this.updateROIInfo();
        document.getElementById('analyzeBtn').disabled = true;
        document.getElementById('clearROI').disabled = true;
    }

    zoomIn() {
        if (!this.image) return;
        
        const newScale = this.scale * 1.2;
        if (newScale <= this.maxScale) {
            const centerX = this.canvas.width / 2;
            const centerY = this.canvas.height / 2;
            
            const imageX = (centerX - this.offsetX) / this.scale;
            const imageY = (centerY - this.offsetY) / this.scale;
            
            this.scale = newScale;
            this.offsetX = centerX - imageX * this.scale;
            this.offsetY = centerY - imageY * this.scale;
            
            this.redrawCanvas();
            this.updateZoomInfo();
            this.debouncedUpdateImageLevel();
        }
    }

    zoomOut() {
        if (!this.image) return;
        
        const newScale = this.scale / 1.2;
        if (newScale >= this.minScale) {
            const centerX = this.canvas.width / 2;
            const centerY = this.canvas.height / 2;
            
            const imageX = (centerX - this.offsetX) / this.scale;
            const imageY = (centerY - this.offsetY) / this.scale;
            
            this.scale = newScale;
            this.offsetX = centerX - imageX * this.scale;
            this.offsetY = centerY - imageY * this.scale;
            
            this.redrawCanvas();
            this.updateZoomInfo();
            this.debouncedUpdateImageLevel();
        }
    }

    resetView() {
        if (this.image) {
            this.fitImageToCanvas();
            this.clearROI();
            this.updateZoomInfo();
            this.debouncedUpdateImageLevel();
        }
    }

    async analyzeROI() {
        if (!this.currentFile || !this.currentROI) {
            this.showError('请先选择文件和ROI区域');
            return;
        }

        const question = document.getElementById('analysisQuestion').value.trim();
        if (!question) {
            this.showError('请输入分析问题');
            return;
        }

        // 显示结果面板和加载状态
        const resultsPanel = document.getElementById('resultsPanel');
        const loading = document.getElementById('analysisLoading');
        const results = document.getElementById('analysisResults');
        
        resultsPanel.style.display = 'flex';
        loading.style.display = 'block';
        results.innerHTML = '';

        try {
            const response = await fetch('/api/analyze_roi', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    file_id: this.currentFile.file_id,
                    roi: this.currentROI,
                    question: question
                })
            });

            const result = await response.json();
            
            if (result.success) {
                this.displayAnalysisResults(result.analysis);
            } else {
                throw new Error(result.error);
            }
        } catch (error) {
            this.showError(`分析失败: ${error.message}`);
            resultsPanel.style.display = 'none';
        } finally {
            loading.style.display = 'none';
        }
    }

    displayAnalysisResults(analysis) {
        const results = document.getElementById('analysisResults');
        
        const html = `
            <div class="analysis-header">
                <h4>分析结果</h4>
                <div class="analysis-meta">
                    <span><i class="fas fa-clock"></i> ${new Date(analysis.timestamp).toLocaleString('zh-CN')}</span>
                    <span><i class="fas fa-crop"></i> 选择区域: ${analysis.roi.width}×${analysis.roi.height}px</span>
                </div>
                ${analysis.roi_extraction_info ? `
                <div class="extraction-info">
                    <h5><i class="fas fa-info-circle"></i> ROI提取信息</h5>
                    <div class="info-grid">
                        <span>原始坐标:</span><span>(${analysis.roi_extraction_info.original_coordinates[0]}, ${analysis.roi_extraction_info.original_coordinates[1]})</span>
                        <span>原始ROI尺寸:</span><span>${analysis.roi_extraction_info.original_roi_size[0]}×${analysis.roi_extraction_info.original_roi_size[1]}px</span>
                        <span>提取图像尺寸:</span><span>${analysis.roi_extraction_info.extracted_image_size[0]}×${analysis.roi_extraction_info.extracted_image_size[1]}px</span>
                        <span>数据源:</span><span>${analysis.roi_extraction_info.scale_used}</span>
                    </div>
                </div>
                ` : ''}
            </div>
            
            <div class="analysis-content">
                <div class="roi-image">
                    <img src="${analysis.roi_image_url}" alt="ROI区域" />
                    <p>分析的ROI区域</p>
                </div>
                
                <div class="analysis-text">
                    <h5>问题：</h5>
                    <p class="question">${analysis.question}</p>
                    
                    <h5>GPT分析结果：</h5>
                    <div class="analysis-result">${this.formatAnalysisText(analysis.analysis)}</div>
                </div>
            </div>
            
            <div class="analysis-actions">
                <button class="btn-secondary" onclick="pathChat.downloadReport('${analysis.id}')">
                    <i class="fas fa-download"></i> 下载报告
                </button>
                <button class="btn-primary" onclick="pathChat.continueAnalysis()">
                    <i class="fas fa-plus"></i> 继续分析
                </button>
            </div>
        `;
        
        results.innerHTML = html;
    }

    formatAnalysisText(text) {
        // 将文本格式化为HTML，保持换行和段落
        return text
            .replace(/\n\n/g, '</p><p>')
            .replace(/\n/g, '<br>')
            .replace(/^/, '<p>')
            .replace(/$/, '</p>')
            .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
            .replace(/\*(.*?)\*/g, '<em>$1</em>');
    }

    continueAnalysis() {
        document.getElementById('resultsPanel').style.display = 'none';
        this.clearROI();
    }

    async loadExistingFiles() {
        try {
            const response = await fetch('/api/files');
            const result = await response.json();
            
            if (result.files && result.files.length > 0) {
                this.updateFilesList(result.files);
            }
        } catch (error) {
            console.error('加载文件列表失败:', error);
        }
    }

    updateFilesList(files = null) {
        const filesList = document.getElementById('filesList');
        
        if (!files) {
            // 重新加载文件列表
            this.loadExistingFiles();
            return;
        }
        
        if (files.length === 0) {
            filesList.innerHTML = '<p class="no-files">暂无上传文件</p>';
            return;
        }
        
        const html = files.map(file => `
            <div class="file-item" data-file-id="${file.file_id}">
                <img src="${file.thumbnail_url}" alt="${file.filename}" class="file-thumbnail">
                <div class="file-info">
                    <div class="file-name">${file.filename}</div>
                    <div class="file-time">${new Date(file.upload_time).toLocaleString('zh-CN')}</div>
                </div>
                <button class="btn-load" onclick="pathChat.loadFile('${file.file_id}')">
                    <i class="fas fa-eye"></i>
                </button>
                <a href="/viewer/${file.file_id}" class="btn-professional" target="_blank" title="专业查看器">
                    <i class="fas fa-microscope"></i>
                </a>
            </div>
        `).join('');
        
        filesList.innerHTML = html;
    }

    async loadFile(fileId) {
        try {
            // 这里应该从服务器获取文件信息
            // 暂时使用当前文件信息
            console.log('加载文件:', fileId);
        } catch (error) {
            this.showError(`加载文件失败: ${error.message}`);
        }
    }

    showError(message) {
        document.getElementById('errorMessage').textContent = message;
        document.getElementById('errorModal').style.display = 'flex';
    }

    showSuccess(message) {
        // 创建成功提示
        const toast = document.createElement('div');
        toast.className = 'toast success';
        toast.innerHTML = `<i class="fas fa-check"></i> ${message}`;
        document.body.appendChild(toast);
        
        setTimeout(() => {
            toast.remove();
        }, 3000);
    }
    }

    // 生成DZI并初始化专业查看器
    async generateDZIAndInitViewer() {
        try {
            this.showLoadingIndicator('正在生成专业查看器...');
            
            const response = await fetch(`/api/generate_dzi/${this.currentFile.file_id}`);
            const result = await response.json();
            
            if (result.success) {
                this.hideLoadingIndicator();
                this.initOpenSeadragonViewer(result.dzi_url);
            } else {
                this.hideLoadingIndicator();
                console.error('DZI生成失败:', result.error);
                // 回退到原来的Canvas方式
                this.initCanvasViewer();
            }
        } catch (error) {
            this.hideLoadingIndicator();
            console.error('生成DZI失败:', error);
            // 回退到原来的Canvas方式
            this.initCanvasViewer();
        }
    }

    // 初始化OpenSeadragon专业查看器
    initOpenSeadragonViewer(dziUrl) {
        const canvas = document.getElementById('imageCanvas');
        const osContainer = document.getElementById('openseadragon-main');
        
        // 隐藏Canvas，显示OpenSeadragon容器
        canvas.style.display = 'none';
        osContainer.style.display = 'block';
        
        // 初始化OpenSeadragon
        this.viewer = OpenSeadragon({
            id: "openseadragon-main",
            prefixUrl: "/static/vendor/openseadragon/images/",
            tileSources: dziUrl,
            showNavigator: true,
            navigatorPosition: 'BOTTOM_RIGHT',
            navigatorSizeRatio: 0.15,
            showNavigationControl: false,
            showZoomControl: false,
            showHomeControl: false,
            showFullPageControl: false,
            gestureSettingsMouse: {
                clickToZoom: false,
                dblClickToZoom: true,
                pinchToZoom: true,
                flickEnabled: true
            }
        });
        
        this.setupOpenSeadragonEvents();
    }

    // 设置OpenSeadragon事件
    setupOpenSeadragonEvents() {
        // 缩放信息更新
        this.viewer.addHandler('zoom', (event) => {
            const zoom = Math.round(event.zoom * 100);
            this.updateZoomDisplay(zoom);
        });
        
        // ROI选择功能
        let isSelecting = false;
        let selectionStart = null;
        let selectionOverlay = null;
        
        this.viewer.addHandler('canvas-click', (event) => {
            if (!isSelecting) {
                // 开始选择ROI
                isSelecting = true;
                const webPoint = event.position;
                const viewportPoint = this.viewer.viewport.pointFromPixel(webPoint);
                selectionStart = viewportPoint;
                
                // 创建选择覆盖层
                selectionOverlay = document.createElement('div');
                selectionOverlay.style.position = 'absolute';
                selectionOverlay.style.border = '2px dashed #ff4444';
                selectionOverlay.style.backgroundColor = 'rgba(255, 68, 68, 0.1)';
                selectionOverlay.style.pointerEvents = 'none';
                this.viewer.canvas.appendChild(selectionOverlay);
                
                event.preventDefaultAction = true;
            }
        });
        
        this.viewer.addHandler('canvas-drag', (event) => {
            if (isSelecting && selectionStart && selectionOverlay) {
                const webPoint = event.position;
                const viewportPoint = this.viewer.viewport.pointFromPixel(webPoint);
                
                // 计算选择区域
                const rect = new OpenSeadragon.Rect(
                    Math.min(selectionStart.x, viewportPoint.x),
                    Math.min(selectionStart.y, viewportPoint.y),
                    Math.abs(viewportPoint.x - selectionStart.x),
                    Math.abs(viewportPoint.y - selectionStart.y)
                );
                
                // 转换为像素坐标更新覆盖层
                const pixelRect = this.viewer.viewport.viewportToViewerElementRectangle(rect);
                selectionOverlay.style.left = pixelRect.x + 'px';
                selectionOverlay.style.top = pixelRect.y + 'px';
                selectionOverlay.style.width = pixelRect.width + 'px';
                selectionOverlay.style.height = pixelRect.height + 'px';
                
                // 保存当前ROI
                this.currentROI = {
                    x: rect.x * this.currentFile.dimensions[0],
                    y: rect.y * this.currentFile.dimensions[1],
                    width: rect.width * this.currentFile.dimensions[0],
                    height: rect.height * this.currentFile.dimensions[1]
                };
            }
        });
        
        this.viewer.addHandler('canvas-release', (event) => {
            if (isSelecting) {
                isSelecting = false;
                
                if (this.currentROI && this.currentROI.width > 100 && this.currentROI.height > 100) {
                    document.getElementById('analyzeBtn').disabled = false;
                    document.getElementById('clearROI').disabled = false;
                    this.updateROIInfo();
                } else {
                    // 选择区域太小，清除
                    this.clearROI();
                }
            }
        });
    }

    // 回退到Canvas查看器
    initCanvasViewer() {
        const canvas = document.getElementById('imageCanvas');
        const osContainer = document.getElementById('openseadragon-main');
        
        // 显示Canvas，隐藏OpenSeadragon容器
        canvas.style.display = 'block';
        osContainer.style.display = 'none';
        
        // 原来的Canvas初始化逻辑
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        
        this.image = new Image();
        this.image.onload = () => {
            this.levelImages['initial'] = this.image;
            this.fitImageToCanvas();
            this.setupCanvasEvents();
            this.showQualityInfo(this.image.width, this.image.height, 'initial');
        };
        this.image.src = this.currentFile.thumbnail_url;
    }

    // 更新缩放显示
    updateZoomDisplay(zoom) {
        const zoomInfo = document.getElementById('zoomInfo');
        if (zoomInfo) {
            zoomInfo.textContent = `${zoom}%`;
        }
        
        // 更新信息面板中的缩放比例
        const zoomInfoPanel = document.getElementById('zoomInfoPanel');
        if (!zoomInfoPanel) {
            const infoPanel = document.getElementById('infoPanel');
            if (infoPanel) {
                const zoomDiv = document.createElement('div');
                zoomDiv.className = 'info-item';
                zoomDiv.innerHTML = `
                    <span>缩放比例：</span>
                    <span id="zoomInfoPanel">${zoom}%</span>
                `;
                infoPanel.appendChild(zoomDiv);
            }
        } else {
            zoomInfoPanel.textContent = `${zoom}%`;
        }
    }

    // 全局函数
function closeModal(modalId) {
    document.getElementById(modalId).style.display = 'none';
}

// 初始化应用
const pathChat = new PathChatApp();
