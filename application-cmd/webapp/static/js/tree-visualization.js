/**
 * 多尺度推理路径树状图可视化组件
 * 用于展示10x/40x/160x三级循证分析路径
 */

class ReasoningTreeVisualization {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.treeData = [];
        this.selectedNodeId = null;
        this.onNodeClick = null;  // 节点点击回调
        
        this.init();
    }
    
    init() {
        console.log('🌳 初始化推理路径树状图');
        
        // 清空容器
        if (!this.container) {
            console.error('树状图容器不存在');
            return;
        }
        
        // 创建树节点容器
        this.treeNodesContainer = document.createElement('div');
        this.treeNodesContainer.className = 'tree-nodes';
        this.container.appendChild(this.treeNodesContainer);
    }
    
    /**
     * 添加树节点
     * @param {Object} nodeData - 节点数据
     */
    addNode(nodeData) {
        console.log('🔹 添加树节点:', nodeData);
        
        // 添加到树数据
        this.treeData.push(nodeData);
        
        // 渲染节点
        this.renderNode(nodeData);
    }
    
    /**
     * 渲染单个节点
     * @param {Object} nodeData - 节点数据
     */
    renderNode(nodeData) {
        // 移除空状态提示
        const emptyState = this.container.querySelector('.tree-empty');
        if (emptyState) {
            emptyState.remove();
        }
        
        const magnification = nodeData.magnification;

        // 创建节点元素
        const nodeElement = document.createElement('div');
        nodeElement.className = `tree-node magnification-${magnification}x`;
        nodeElement.id = `tree-node-${nodeData.id}`;
        nodeElement.dataset.nodeId = nodeData.id;
        
        // 节点图标（显示倍率）
        const iconElement = document.createElement('div');
        iconElement.className = 'tree-node-icon';
        iconElement.textContent = `${magnification}×`;
        
        // 节点内容
        const contentElement = document.createElement('div');
        contentElement.className = 'tree-node-content';
        
        // 节点头部（标题和置信度）
        const headerElement = document.createElement('div');
        headerElement.className = 'tree-node-header';
        
        const titleElement = document.createElement('div');
        titleElement.className = 'tree-node-title';
        titleElement.textContent = nodeData.level_name || `${magnification}×`;
        
        headerElement.appendChild(titleElement);
        
        // 节点摘要
        const summaryElement = document.createElement('div');
        summaryElement.className = 'tree-node-summary';
        summaryElement.textContent = nodeData.summary || (typeof __t === 'function' ? __t('tree_analyzing') : 'Analysis in progress...');
        
        // 节点指标
        const metricsElement = document.createElement('div');
        metricsElement.className = 'tree-node-metrics';
        
        if (nodeData.metrics) {
            // 根据倍率显示关键指标
            if (nodeData.magnification === 10 && nodeData.metrics.tumor_area_ratio) {
                const badge = this.createMetricBadge(this.getMetricLabel('tumor_area_ratio'), 
                    `${(nodeData.metrics.tumor_area_ratio * 100).toFixed(1)}%`);
                metricsElement.appendChild(badge);
            }
            
            if (nodeData.magnification === 40 && nodeData.metrics.mitotic_activity) {
                const badge = this.createMetricBadge(this.getMetricLabel('mitotic_activity'), 
                    nodeData.metrics.mitotic_activity);
                metricsElement.appendChild(badge);
            }
            
            if (Number(nodeData.magnification) >= 160 && nodeData.metrics.mitosis_count !== undefined) {
                const badge = this.createMetricBadge(this.getMetricLabel('mitosis_count'), 
                    `${nodeData.metrics.mitosis_count}`);
                metricsElement.appendChild(badge);
            }
            
            if (nodeData.metrics.necrosis_ratio && nodeData.metrics.necrosis_ratio > 0.05) {
                const badge = this.createMetricBadge(this.getMetricLabel('necrosis_ratio'), 
                    `${(nodeData.metrics.necrosis_ratio * 100).toFixed(1)}%`);
                metricsElement.appendChild(badge);
            }
        }
        
        // 组装节点
        contentElement.appendChild(headerElement);
        contentElement.appendChild(summaryElement);
        if (metricsElement.children.length > 0) {
            contentElement.appendChild(metricsElement);
        }
        
        nodeElement.appendChild(iconElement);
        nodeElement.appendChild(contentElement);
        
        // 添加点击事件
        nodeElement.addEventListener('click', () => this.handleNodeClick(nodeData, nodeElement));
        
        // 添加到容器
        this.treeNodesContainer.appendChild(nodeElement);
        
        // 滚动到新节点
        setTimeout(() => {
            nodeElement.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }, 100);
    }
    
    /**
     * 创建指标徽章
     */
    createMetricBadge(label, value) {
        const badge = document.createElement('span');
        badge.className = 'metric-badge';
        badge.textContent = `${label}: ${value}`;
        return badge;
    }
    
    /**
     * 处理节点点击
     */
    handleNodeClick(nodeData, nodeElement) {
        console.log('🔘 节点被点击:', nodeData);
        
        // 取消其他节点的选中状态
        this.treeNodesContainer.querySelectorAll('.tree-node').forEach(node => {
            node.classList.remove('active');
        });
        
        // 标记当前节点为选中
        nodeElement.classList.add('active');
        this.selectedNodeId = nodeData.id;
        
        // 显示节点详情
        this.showNodeDetails(nodeData);
        
        // 触发回调（跳转到对应视图）
        if (this.onNodeClick) {
            this.onNodeClick(nodeData);
        }
    }
    
    /**
     * 显示节点详情
     */
    showNodeDetails(nodeData) {
        const detailsPanel = document.getElementById('nodeDetails');
        const detailsContent = document.getElementById('nodeDetailsContent');
        
        if (!detailsPanel || !detailsContent) return;
        
        // 构建详情内容
        let detailsHTML = `
            <div class="detail-section">
                <h6>${nodeData.level_name || `${nodeData.magnification}×`}</h6>
                <div class="detail-text">
                    <strong>${typeof __t === 'function' ? __t('magnification_label') : 'Magnification'}:</strong> ${nodeData.magnification}×<br>
                    <strong>${typeof __t === 'function' ? __t('position_label') : 'Position'}:</strong> (${nodeData.rect.x}, ${nodeData.rect.y})<br>
                    <strong>${typeof __t === 'function' ? __t('region_size_label') : 'Size'}:</strong> ${nodeData.rect.w} × ${nodeData.rect.h} px
                </div>
            </div>
        `;
        
        // 添加摘要
        if (nodeData.summary) {
            detailsHTML += `
                <div class="detail-section">
                    <h6>${typeof __t === 'function' ? __t('section_reasoning') : 'Observation & Reasoning'}</h6>
                    <div class="detail-text">${nodeData.summary}</div>
                </div>
            `;
        }
        
        // 添加指标
        if (nodeData.metrics && Object.keys(nodeData.metrics).length > 0) {
            detailsHTML += `
                <div class="detail-section">
                    <h6>${typeof __t === 'function' ? __t('metrics_label', 'Metrics') : 'Metrics'}</h6>
                    <div class="detail-metrics">
            `;
            
            for (const [key, value] of Object.entries(nodeData.metrics)) {
                const label = this.getMetricLabel(key);
                const formattedValue = this.formatMetricValue(key, value);
                
                detailsHTML += `
                    <div class="detail-metric-item">
                        <div class="detail-metric-label">${label}</div>
                        <div class="detail-metric-value">${formattedValue}</div>
                    </div>
                `;
            }
            
            detailsHTML += `
                    </div>
                </div>
            `;
        }
        
        // 添加图像
        if (nodeData.image_path) {
            detailsHTML += `
                <div class="detail-section">
                    <h6>${typeof __t === 'function' ? __t('region_preview') : 'Region Preview'}</h6>
                    <img src="/${nodeData.image_path}" alt="ROI" class="detail-image">
                </div>
            `;
        }
        
        detailsContent.innerHTML = detailsHTML;
        detailsPanel.style.display = 'block';
    }
    
    /**
     * 获取指标标签
     */
    getMetricLabel(key) {
        const isZh = typeof window !== 'undefined' && (window.__lang || 'zh') === 'zh';
        const labels_zh = {
            'tumor_area_ratio': '肿瘤占比',
            'mitosis_count': '核分裂象',
            'mitotic_activity': '核分裂活性',
            'necrosis_ratio': '坏死比例',
            'cell_density': '细胞密度',
            'nuclear_atypia_score': '核异型性',
            'tumor_boundary_clarity': '边界清晰度',
            'tissue_type': '组织类型',
            'viable_tissue_ratio': '存活组织比例',
            'necrosis_pattern': '坏死模式'
        };
        const labels_en = {
            'tumor_area_ratio': 'Tumor Ratio',
            'mitosis_count': 'Mitosis Count',
            'mitotic_activity': 'Mitotic Activity',
            'necrosis_ratio': 'Necrosis Ratio',
            'cell_density': 'Cell Density',
            'nuclear_atypia_score': 'Nuclear Atypia',
            'tumor_boundary_clarity': 'Boundary Clarity',
            'tissue_type': 'Tissue Type',
            'viable_tissue_ratio': 'Viable Tissue',
            'necrosis_pattern': 'Necrosis Pattern'
        };
        const labels = isZh ? labels_zh : labels_en;
        return labels[key] || key;
    }
    
    /**
     * 格式化指标值
     */
    formatMetricValue(key, value) {
        if (typeof value === 'number') {
            if (key.includes('ratio') || key.includes('clarity')) {
                return `${(value * 100).toFixed(1)}%`;
            } else if (key.includes('score')) {
                return value.toFixed(2);
            } else if (key.includes('density')) {
                return `${value.toFixed(0)} /mm²`;
            } else if (key.includes('count')) {
                return `${value}`;
            } else {
                return value.toFixed(2);
            }
        }
        return value;
    }
    
    /**
     * 清空树
     */
    clear() {
        this.treeData = [];
        this.selectedNodeId = null;
        this.treeNodesContainer.innerHTML = '';
        
        // 恢复空状态
        const emptyState = document.createElement('div');
        emptyState.className = 'tree-empty';
        emptyState.innerHTML = `
            <i class="fas fa-sitemap"></i>
            <p>${typeof __t === 'function' ? __t('tree_empty_hint') : 'Analysis path tree will appear after Agent starts'}</p>
        `;
        this.container.appendChild(emptyState);
        
        // 隐藏详情面板
        const detailsPanel = document.getElementById('nodeDetails');
        if (detailsPanel) {
            detailsPanel.style.display = 'none';
        }
    }
    
    /**
     * 根据节点ID跳转到视图
     */
    jumpToNode(nodeId) {
        const nodeData = this.treeData.find(n => n.id === nodeId);
        if (nodeData) {
            const nodeElement = document.getElementById(`tree-node-${nodeId}`);
            if (nodeElement) {
                this.handleNodeClick(nodeData, nodeElement);
            }
        }
    }
}

// 导出到全局作用域
window.ReasoningTreeVisualization = ReasoningTreeVisualization;
