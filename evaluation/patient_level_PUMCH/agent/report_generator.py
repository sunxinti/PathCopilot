"""
病理报告Word文档生成器
生成详细的病理分析报告，包含图片和解读
"""

import os
import logging
from pathlib import Path
from datetime import datetime
from PIL import Image
# 禁用 Pillow 的像素限制（WSI 图像通常很大）
Image.MAX_IMAGE_PIXELS = None
import io
import tempfile

logger = logging.getLogger(__name__)

# 尝试导入python-docx
try:
    from docx import Document
    from docx.shared import Inches, Pt, Cm, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.style import WD_STYLE_TYPE
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False
    logger.warning("python-docx 未安装，Word报告功能不可用。请运行: pip install python-docx")


def resize_image_for_doc(image_path: str, max_size: int = 800) -> str:
    """
    调整图片大小以适合Word文档
    
    Args:
        image_path: 原始图片路径
        max_size: 最大边长（像素）
    
    Returns:
        临时文件路径
    """
    try:
        img = Image.open(image_path)
        
        # 计算缩放比例
        width, height = img.size
        if width > max_size or height > max_size:
            ratio = min(max_size / width, max_size / height)
            new_size = (int(width * ratio), int(height * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        
        # 保存到临时文件
        temp_file = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
        img.save(temp_file.name, 'JPEG', quality=85)
        return temp_file.name
        
    except Exception as e:
        logger.warning(f"调整图片大小失败: {e}")
        return None


def generate_detailed_word_report(agent_state: dict, output_dir: Path, slide=None) -> str:
    """
    生成详细的Word病理报告（带图片）
    
    Args:
        agent_state: Agent状态，包含所有分析结果
        output_dir: 输出目录
        slide: OpenSlide对象（可选，用于提取ROI图片）
    
    Returns:
        生成的报告文件路径
    """
    if not DOCX_AVAILABLE:
        logger.error("python-docx 未安装，无法生成Word报告")
        return None
    
    temp_files = []  # 记录临时文件，最后清理
    
    try:
        doc = Document()
        
        # === 设置文档样式 ===
        style = doc.styles['Normal']
        style.font.name = '微软雅黑'
        style.font.size = Pt(11)
        
        # === 封面/标题 ===
        title = doc.add_heading('病理切片分析报告', 0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        
        # 文件信息
        filename = agent_state.get('filename', '未知文件')
        file_id = agent_state.get('file_id', 'unknown')
        
        info_para = doc.add_paragraph()
        info_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        info_para.add_run(f'\n切片文件: {filename}\n').bold = True
        info_para.add_run(f'分析ID: {file_id}\n')
        info_para.add_run(f'生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
        
        doc.add_paragraph()  # 空行
        
        # === 分析概要 ===
        doc.add_heading('一、分析概要', level=1)
        
        tree_nodes = agent_state.get('tree_nodes', [])
        total_red = len(tree_nodes)
        total_blue = sum(len(node.get('children', [])) for node in tree_nodes)
        total_green = sum(
            len(blue.get('children', [])) 
            for node in tree_nodes 
            for blue in node.get('children', [])
        )
        
        summary_para = doc.add_paragraph()
        summary_para.add_run(f'• 分析红框数量: {total_red} 个 (10×低倍镜)\n')
        summary_para.add_run(f'• 分析蓝框数量: {total_blue} 个 (40×中倍镜)\n')
        summary_para.add_run(f'• 分析绿框数量: {total_green} 个 (200×高倍镜)\n')
        summary_para.add_run(f'• 总计分析区域: {total_red + total_blue + total_green} 个\n')
        
        # 诊断结论
        final_report = agent_state.get('final_report', {})
        diagnosis = final_report.get('diagnosis', agent_state.get('diagnosis_result', {}).get('diagnosis', '待定'))
        
        doc.add_heading('二、诊断结论', level=1)
        diag_para = doc.add_paragraph()
        diag_run = diag_para.add_run(diagnosis)
        diag_run.bold = True
        diag_run.font.size = Pt(14)
        
        doc.add_page_break()
        
        # === 详细分析结果（带图片） ===
        doc.add_heading('三、详细分析结果', level=1)
        
        # 按位置排序红框（从左到右、从上到下）
        sorted_nodes = sort_rois_by_position(tree_nodes)
        
        # 构建steps目录路径
        steps_dir = output_dir / 'steps'
        
        for red_idx, red_node in enumerate(sorted_nodes, 1):
            roi_id = red_node.get('roi_id', f'红框{red_idx}')
            red_analysis = red_node.get('analysis', {})
            
            # === 红框标题 ===
            doc.add_heading(f'红框 #{red_idx}: {roi_id}', level=2)
            
            # === 添加红框图片 ===
            red_image_path = find_roi_image_in_steps(steps_dir, roi_id, '10x')
            if red_image_path:
                try:
                    # 缩小图片后插入
                    resized_path = resize_image_for_doc(str(red_image_path), max_size=600)
                    if resized_path:
                        temp_files.append(resized_path)
                        doc.add_picture(resized_path, width=Inches(5))
                        last_para = doc.paragraphs[-1]
                        last_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        # 添加图片说明
                        caption = doc.add_paragraph()
                        caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        caption_run = caption.add_run(f'图 {red_idx}: {roi_id} (10×低倍镜)')
                        caption_run.font.size = Pt(9)
                        caption_run.italic = True
                except Exception as e:
                    logger.warning(f"插入红框图片失败: {e}")
            
            doc.add_paragraph()  # 空行
            
            # 红框分析结果
            red_para = doc.add_paragraph()
            red_para.add_run('【10× 低倍镜分析结果】\n').bold = True
            red_findings = red_analysis.get('findings', red_analysis.get('finding', '暂无分析结果'))
            red_para.add_run(str(red_findings))
            
            doc.add_paragraph()  # 空行
            
            # === 蓝框分析（带图片） ===
            blue_children = red_node.get('children', [])
            if blue_children:
                blue_header = doc.add_paragraph()
                blue_header.add_run(f'【40× 中倍镜分析】（共{len(blue_children)}个蓝框）').bold = True
                
                for blue_idx, blue_node in enumerate(blue_children, 1):
                    blue_roi_id = blue_node.get('roi_id', f'蓝框{blue_idx}')
                    blue_analysis = blue_node.get('analysis', {})
                    blue_findings = blue_analysis.get('findings', '暂无')
                    
                    # 蓝框小标题
                    doc.add_paragraph().add_run(f'── 蓝框 {blue_idx}: {blue_roi_id}').bold = True
                    
                    # 添加蓝框图片
                    blue_image_path = find_roi_image_in_steps(steps_dir, blue_roi_id, '40x')
                    if blue_image_path:
                        try:
                            resized_path = resize_image_for_doc(str(blue_image_path), max_size=500)
                            if resized_path:
                                temp_files.append(resized_path)
                                doc.add_picture(resized_path, width=Inches(4))
                                last_para = doc.paragraphs[-1]
                                last_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        except Exception as e:
                            logger.warning(f"插入蓝框图片失败: {e}")
                    
                    # 蓝框分析文字
                    blue_para = doc.add_paragraph()
                    blue_para.add_run(str(blue_findings))
                    
                    # === 绿框分析 ===
                    green_children = blue_node.get('children', [])
                    if green_children:
                        green_header = doc.add_paragraph()
                        green_header.add_run(f'    └ 200× 高倍镜分析（{len(green_children)}个绿框）').italic = True
                        
                        for green_idx, green_node in enumerate(green_children, 1):
                            green_roi_id = green_node.get('roi_id', f'绿框{green_idx}')
                            green_analysis = green_node.get('analysis', {})
                            green_findings = green_analysis.get('findings', '暂无')
                            
                            # 添加绿框图片（较小）
                            green_image_path = find_roi_image_in_steps(steps_dir, green_roi_id, '200x')
                            if green_image_path:
                                try:
                                    resized_path = resize_image_for_doc(str(green_image_path), max_size=400)
                                    if resized_path:
                                        temp_files.append(resized_path)
                                        doc.add_picture(resized_path, width=Inches(2.5))
                                        last_para = doc.paragraphs[-1]
                                        last_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                                except Exception as e:
                                    logger.warning(f"插入绿框图片失败: {e}")
                            
                            green_para = doc.add_paragraph()
                            green_para.add_run(f'        绿框{green_idx}: ').bold = True
                            green_para.add_run(str(green_findings)[:300])
            
            # 分隔线
            doc.add_paragraph('─' * 50)
            
            # 每个红框后分页（内容较多）
            if red_idx < len(sorted_nodes):
                doc.add_page_break()
        
        # === 附录：分析树结构 ===
        doc.add_page_break()
        doc.add_heading('四、分析树结构总览', level=1)
        
        tree_text = generate_tree_text(sorted_nodes)
        tree_para = doc.add_paragraph()
        tree_run = tree_para.add_run(tree_text)
        tree_run.font.name = 'Consolas'
        tree_run.font.size = Pt(9)
        
        # === 保存文档 ===
        output_path = output_dir / f'病理分析报告_{datetime.now().strftime("%Y%m%d_%H%M%S")}.docx'
        doc.save(str(output_path))
        
        logger.info(f"✅ Word报告已生成: {output_path}")
        return str(output_path)
        
    except Exception as e:
        logger.error(f"生成Word报告失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return None
    
    finally:
        # 清理临时文件
        for temp_file in temp_files:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except:
                pass


def find_roi_image_in_steps(steps_dir: Path, roi_id: str, zoom_suffix: str) -> Path:
    """
    在steps目录中查找ROI图片
    
    Args:
        steps_dir: steps目录路径
        roi_id: ROI ID (如 roi_10x_r0_c0 或 roi_10x_r0_c0_sub_r1_c2)
        zoom_suffix: 放大倍率后缀 (10x, 40x, 200x)
    
    Returns:
        图片路径或None
    """
    try:
        if not steps_dir.exists():
            return None
        
        # 解析roi_id获取红框目录名
        # roi_id格式: roi_10x_r0_c0 或 roi_10x_r0_c0_sub_r1_c2
        parts = roi_id.split('_sub_')
        red_roi_id = parts[0]  # roi_10x_r0_c0
        
        # 红框目录
        red_dir = steps_dir / red_roi_id
        if not red_dir.exists():
            return None
        
        # 构建图片文件名
        image_name = f"{roi_id}_{zoom_suffix}.jpg"
        image_path = red_dir / image_name
        
        if image_path.exists():
            return image_path
        
        # 尝试查找匹配的图片
        for img_file in red_dir.glob(f"*{zoom_suffix}.jpg"):
            if roi_id in img_file.name:
                return img_file
        
        return None
        
    except Exception as e:
        logger.warning(f"查找ROI图片失败: {e}")
        return None


def sort_rois_by_position(tree_nodes: list) -> list:
    """
    按位置排序ROI（从左到右、从上到下）
    
    Args:
        tree_nodes: 红框节点列表
    
    Returns:
        排序后的节点列表
    """
    def get_position(node):
        roi_id = node.get('roi_id', '')
        # 解析如 "roi_10x_r1_c2" 格式
        try:
            parts = roi_id.split('_')
            row = 0
            col = 0
            for i, part in enumerate(parts):
                if part.startswith('r') and part[1:].isdigit():
                    row = int(part[1:])
                elif part.startswith('c') and part[1:].isdigit():
                    col = int(part[1:])
            return (row, col)
        except:
            return (999, 999)
    
    return sorted(tree_nodes, key=get_position)


def generate_tree_text(tree_nodes: list) -> str:
    """
    生成树状结构文本
    
    Args:
        tree_nodes: 树节点列表
    
    Returns:
        树状结构文本
    """
    lines = []
    
    for red_idx, red_node in enumerate(tree_nodes, 1):
        roi_id = red_node.get('roi_id', f'红框{red_idx}')
        lines.append(f"🔴 红框{red_idx}: {roi_id}")
        
        blue_children = red_node.get('children', [])
        for blue_idx, blue_node in enumerate(blue_children, 1):
            blue_id = blue_node.get('roi_id', f'蓝框{blue_idx}')
            is_last_blue = blue_idx == len(blue_children)
            prefix = "└─" if is_last_blue else "├─"
            lines.append(f"  {prefix} 🔵 蓝框{blue_idx}: {blue_id}")
            
            green_children = blue_node.get('children', [])
            for green_idx, green_node in enumerate(green_children, 1):
                green_id = green_node.get('roi_id', f'绿框{green_idx}')
                is_last_green = green_idx == len(green_children)
                green_prefix = "  └─" if is_last_green else "  ├─"
                parent_prefix = "    " if is_last_blue else "  │ "
                lines.append(f"{parent_prefix}{green_prefix} 🟢 绿框{green_idx}: {green_id}")
        
        lines.append("")  # 空行分隔红框
    
    return "\n".join(lines)


def generate_report_with_images(agent_state: dict, output_dir: Path, slide, debug_images_dir: Path = None) -> str:
    """
    生成带图片的详细Word报告
    
    Args:
        agent_state: Agent状态
        output_dir: 输出目录
        slide: OpenSlide对象
        debug_images_dir: 调试图片目录（包含已保存的ROI图片）
    
    Returns:
        报告文件路径
    """
    if not DOCX_AVAILABLE:
        logger.error("python-docx 未安装")
        return None
    
    try:
        doc = Document()
        
        # 设置样式
        style = doc.styles['Normal']
        style.font.name = '微软雅黑'
        style.font.size = Pt(11)
        
        # === 封面 ===
        title = doc.add_heading('病理切片详细分析报告', 0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        
        filename = agent_state.get('filename', '未知文件')
        file_id = agent_state.get('file_id', 'unknown')
        
        info_para = doc.add_paragraph()
        info_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        info_para.add_run(f'\n切片文件: {filename}\n').bold = True
        info_para.add_run(f'分析ID: {file_id}\n')
        info_para.add_run(f'生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
        
        doc.add_page_break()
        
        # === 获取树结构 ===
        tree_nodes = agent_state.get('tree_nodes', [])
        sorted_nodes = sort_rois_by_position(tree_nodes)
        
        # === 分析概要 ===
        doc.add_heading('分析概要', level=1)
        
        total_red = len(sorted_nodes)
        total_blue = sum(len(node.get('children', [])) for node in sorted_nodes)
        total_green = sum(
            len(blue.get('children', [])) 
            for node in sorted_nodes 
            for blue in node.get('children', [])
        )
        
        summary_table = doc.add_table(rows=4, cols=2)
        summary_table.style = 'Table Grid'
        summary_table.rows[0].cells[0].text = '红框 (10×)'
        summary_table.rows[0].cells[1].text = f'{total_red} 个'
        summary_table.rows[1].cells[0].text = '蓝框 (40×)'
        summary_table.rows[1].cells[1].text = f'{total_blue} 个'
        summary_table.rows[2].cells[0].text = '绿框 (200×)'
        summary_table.rows[2].cells[1].text = f'{total_green} 个'
        summary_table.rows[3].cells[0].text = '总计'
        summary_table.rows[3].cells[1].text = f'{total_red + total_blue + total_green} 个区域'
        
        doc.add_page_break()
        
        # === 详细分析（带图片） ===
        doc.add_heading('详细分析结果', level=1)
        
        for red_idx, red_node in enumerate(sorted_nodes, 1):
            roi_id = red_node.get('roi_id', f'红框{red_idx}')
            red_analysis = red_node.get('analysis', {})
            
            # 红框标题
            doc.add_heading(f'红框 #{red_idx}: {roi_id}', level=2)
            
            # 尝试添加红框图片
            if debug_images_dir:
                red_image_path = find_roi_image(debug_images_dir, roi_id, 'red')
                if red_image_path and os.path.exists(red_image_path):
                    try:
                        doc.add_picture(str(red_image_path), width=Inches(5))
                        last_para = doc.paragraphs[-1]
                        last_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    except Exception as e:
                        logger.warning(f"添加图片失败: {e}")
            
            # 红框分析结果
            doc.add_paragraph()
            red_para = doc.add_paragraph()
            red_para.add_run('【10× 低倍镜分析】\n').bold = True
            red_findings = red_analysis.get('findings', red_analysis.get('finding', '暂无分析结果'))
            red_para.add_run(str(red_findings))
            
            doc.add_paragraph()
            
            # 蓝框分析
            blue_children = red_node.get('children', [])
            if blue_children:
                doc.add_paragraph().add_run(f'【40× 中倍镜分析】（共{len(blue_children)}个蓝框）').bold = True
                
                for blue_idx, blue_node in enumerate(blue_children, 1):
                    blue_analysis = blue_node.get('analysis', {})
                    blue_id = blue_node.get('roi_id', f'蓝框{blue_idx}')
                    blue_findings = blue_analysis.get('findings', '暂无')
                    
                    blue_para = doc.add_paragraph()
                    blue_para.add_run(f'  蓝框{blue_idx} ({blue_id}): ').bold = True
                    blue_para.add_run(str(blue_findings)[:400])
                    
                    # 绿框分析
                    green_children = blue_node.get('children', [])
                    if green_children:
                        green_summary = doc.add_paragraph()
                        green_summary.add_run(f'    └ 200× 高倍镜分析（{len(green_children)}个绿框）:').italic = True
                        
                        for green_idx, green_node in enumerate(green_children, 1):
                            green_analysis = green_node.get('analysis', {})
                            green_findings = green_analysis.get('findings', '暂无')
                            
                            green_para = doc.add_paragraph()
                            green_para.add_run(f'        绿框{green_idx}: ')
                            green_para.add_run(str(green_findings)[:200])
            
            # 分隔线
            doc.add_paragraph('─' * 60)
            
            # 每2个红框后分页
            if red_idx % 2 == 0 and red_idx < len(sorted_nodes):
                doc.add_page_break()
        
        # === 保存 ===
        output_path = output_dir / f'病理详细报告_{datetime.now().strftime("%Y%m%d_%H%M%S")}.docx'
        doc.save(str(output_path))
        
        logger.info(f"✅ 详细Word报告已生成: {output_path}")
        return str(output_path)
        
    except Exception as e:
        logger.error(f"生成报告失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def find_roi_image(debug_dir: Path, roi_id: str, level: str) -> str:
    """
    查找ROI对应的调试图片
    
    Args:
        debug_dir: 调试图片目录
        roi_id: ROI ID
        level: 级别 (red/blue/green)
    
    Returns:
        图片路径或None
    """
    try:
        # 搜索匹配的图片
        for img_file in debug_dir.glob(f"*{roi_id}*.jpg"):
            return str(img_file)
        for img_file in debug_dir.glob(f"*{roi_id}*.png"):
            return str(img_file)
    except Exception as e:
        logger.warning(f"查找图片失败: {e}")
    
    return None


# ================== Patient Level 报告生成 ==================

def generate_patient_level_report(patient_state: dict, output_dir: Path) -> dict:
    """
    生成患者级别综合报告（汇总多个WSI的分析结果）
    
    Args:
        patient_state: 患者状态，包含所有WSI的分析结果
        output_dir: 输出目录
    
    Returns:
        dict: {'success': bool, 'report_path': str, 'summary': str}
    """
    if not DOCX_AVAILABLE:
        logger.error("python-docx 未安装，无法生成Word报告")
        return {'success': False, 'error': 'python-docx未安装', 'summary': '报告生成失败：缺少依赖'}
    
    temp_files = []
    
    try:
        doc = Document()
        
        # === 设置文档样式 ===
        style = doc.styles['Normal']
        style.font.name = '微软雅黑'
        style.font.size = Pt(11)
        
        # === 封面/标题 ===
        title = doc.add_heading('患者级别病理分析报告', 0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        
        # 患者信息
        folder_name = patient_state.get('folder_name', '未知患者')
        patient_id = patient_state.get('patient_id', 'unknown')
        total_wsi = patient_state.get('total_wsi', 0)
        completed_wsi = patient_state.get('completed_wsi', 0)
        
        info_para = doc.add_paragraph()
        info_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        info_para.add_run(f'\n患者文件夹: {folder_name}\n').bold = True
        info_para.add_run(f'分析切片数: {completed_wsi}/{total_wsi} 张\n')
        info_para.add_run(f'分析ID: {patient_id}\n')
        info_para.add_run(f'生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
        
        doc.add_paragraph()
        
        # === 分析概要 ===
        doc.add_heading('一、分析概要', level=1)
        
        file_infos = patient_state.get('file_infos', [])
        wsi_results = patient_state.get('wsi_results', {})
        
        summary_table = doc.add_table(rows=1, cols=4)
        summary_table.style = 'Table Grid'
        hdr_cells = summary_table.rows[0].cells
        hdr_cells[0].text = '序号'
        hdr_cells[1].text = '切片文件'
        hdr_cells[2].text = '部位'
        hdr_cells[3].text = '状态'
        
        for cell in hdr_cells:
            cell.paragraphs[0].runs[0].bold = True
        
        for idx, file_info in enumerate(file_infos, 1):
            row_cells = summary_table.add_row().cells
            row_cells[0].text = str(idx)
            row_cells[1].text = file_info.get('filename', f'WSI_{idx}')[:30]
            row_cells[2].text = file_info.get('part', f'Part {idx}')
            
            file_id = file_info.get('file_id')
            if file_id and file_id in wsi_results:
                row_cells[3].text = '已完成'
            else:
                row_cells[3].text = '未完成'
        
        doc.add_paragraph()
        
        # === 病理诊断报告 ===
        doc.add_heading('二、病理诊断报告', level=1)
        
        # ⭐ 优先使用 AI 生成的最终报告
        ai_final_report = patient_state.get('ai_final_report')
        
        if ai_final_report:
            # 使用 AI 生成的报告
            logger.info("📝 使用 AI 生成的最终报告")
            
            ai_report_para = doc.add_paragraph()
            ai_report_para.add_run("【AI 综合诊断报告】").bold = True
            ai_report_para.add_run("\n\n")
            
            # 逐行添加 AI 报告（保持格式）
            for line in ai_final_report.split('\n'):
                line_para = doc.add_paragraph()
                # 处理标题行
                if line.startswith('###'):
                    run = line_para.add_run(line.replace('###', '').strip())
                    run.bold = True
                    run.font.size = Pt(14)
                elif line.startswith('**') and line.endswith('**'):
                    run = line_para.add_run(line.replace('**', ''))
                    run.bold = True
                elif line.startswith('- '):
                    line_para.add_run('• ' + line[2:])
                else:
                    line_para.add_run(line)
            
            doc.add_paragraph()
            
            # ⭐ 当使用 AI 报告时，创建一个简化的 all_findings 用于摘要生成
            # 注意：完整的 AI 报告已经在上面添加到 Word 文档了
            all_findings = {
                'final_diagnosis': ai_final_report if ai_final_report else '分析完成',
                'staging_suggestion': ''
            }
        else:
            # AI 最终报告不可用：不生成任何诊断内容
            logger.error("❌ AI 最终报告不可用，患者级报告不包含诊断结论")
            unavailable_para = doc.add_paragraph()
            unavailable_para.add_run('AI 最终报告不可用，本报告不包含诊断结论。').bold = True
            all_findings = {
                'final_diagnosis': 'AI 最终报告不可用，未生成诊断结论。',
                'staging_suggestion': ''
            }
        
        doc.add_page_break()
        
        # === 分切片详细结果 ===
        doc.add_heading('三、分切片详细分析', level=1)
        
        for idx, file_info in enumerate(file_infos, 1):
            file_id = file_info.get('file_id')
            filename = file_info.get('filename', f'WSI_{idx}')
            part_name = file_info.get('part', f'Part {idx}')
            
            # 切片标题
            doc.add_heading(f'{part_name}: {filename}', level=2)
            
            if file_id and file_id in wsi_results:
                wsi_state = wsi_results[file_id]
                
                # 空值检查：分析失败时 wsi_state 可能为 None
                if not wsi_state or not isinstance(wsi_state, dict):
                    doc.add_paragraph(f'该切片分析未返回有效结果')
                    continue
                
                # 分析统计
                stats = wsi_state.get('statistics', {})
                total_red = stats.get('red_frames', 0)
                total_blue = stats.get('blue_frames', 0)
                total_green = stats.get('green_frames', 0)
                
                stats_para = doc.add_paragraph()
                stats_para.add_run(f'分析区域: 红框{total_red}个, 蓝框{total_blue}个, 绿框{total_green}个\n')
                
                # 主要发现
                findings = wsi_state.get('pathology_findings', [])
                if findings:
                    doc.add_paragraph().add_run('主要发现:').bold = True
                    for f_idx, finding in enumerate(findings[:5], 1):  # 最多显示5条
                        if finding:
                            finding_text = str(finding)[:800]  # 限制长度
                            doc.add_paragraph(f'{f_idx}. {finding_text}')
                
                # 尝试添加缩略图
                wsi_output_dir = Path(f"outputs/{file_id}")
                thumb_path = wsi_output_dir / 'steps' / 'step_0_thumbnail.jpg'
                if thumb_path.exists():
                    try:
                        resized_path = resize_image_for_doc(str(thumb_path), max_size=400)
                        if resized_path:
                            temp_files.append(resized_path)
                            doc.add_picture(resized_path, width=Inches(3.5))
                            caption = doc.add_paragraph()
                            caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            caption_run = caption.add_run(f'图: {part_name} 缩略图')
                            caption_run.font.size = Pt(9)
                            caption_run.italic = True
                    except Exception as e:
                        logger.warning(f"插入缩略图失败: {e}")
            else:
                doc.add_paragraph('该切片分析未完成或数据不可用。')
            
            doc.add_paragraph('─' * 50)
        
        # === 保存报告 ===
        report_filename = f'患者级别分析报告_{folder_name}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.docx'
        output_path = output_dir / report_filename
        doc.save(str(output_path))
        
        # 清理临时文件
        for temp_file in temp_files:
            try:
                os.unlink(temp_file)
            except:
                pass
        
        # 生成摘要
        summary = generate_patient_summary(patient_state, all_findings)
        
        logger.info(f"✅ 患者级别报告已生成: {output_path}")
        
        return {
            'success': True,
            'report_path': str(output_path),
            'summary': summary
        }
        
    except Exception as e:
        logger.error(f"生成患者级别报告失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        
        # 清理临时文件
        for temp_file in temp_files:
            try:
                os.unlink(temp_file)
            except:
                pass
        
        return {
            'success': False,
            'error': str(e),
            'summary': f'报告生成失败: {str(e)}'
        }


def generate_patient_summary(patient_state: dict, findings: dict) -> str:
    """
    生成患者级别分析摘要（用于前端显示）
    
    Args:
        patient_state: 患者状态
        findings: 综合诊断发现
    
    Returns:
        str: Markdown格式的摘要文本
    """
    folder_name = patient_state.get('folder_name', '未知')
    total_wsi = patient_state.get('total_wsi', 0)
    completed_wsi = patient_state.get('completed_wsi', 0)
    lang = patient_state.get('lang', 'zh')
    is_en = (lang == 'en')
    
    final_diagnosis = findings.get('final_diagnosis', 'Analyzing...' if is_en else '分析中...')
    
    if is_en:
        summary = f"""**Patient**: {folder_name} | **Slides Analyzed**: {completed_wsi}/{total_wsi}

{final_diagnosis}"""
    else:
        summary = f"""**患者**: {folder_name} | **分析切片**: {completed_wsi}/{total_wsi} 张

{final_diagnosis}"""
    
    return summary

# ================== End Patient Level 报告生成 ==================

