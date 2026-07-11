import json
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
import numpy as np
import networkx as nx
from pathlib import Path
import argparse
import os
from typing import List, Dict, Tuple
import matplotlib.font_manager as fm

import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
# 配置中文字体支持
plt.rcParams['font.sans-serif'] = ['SimHei', 'DejaVu Sans', 'Arial Unicode MS', 'Microsoft YaHei', 'WenQuanYi Micro Hei']
plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示问题

# 检查并列出可用的中文字体
def check_chinese_fonts():
    """检查系统中可用的中文字体"""
    fonts = [f.name for f in fm.fontManager.ttflist]
    chinese_fonts = []
    
    # 常见的中文字体名称
    chinese_font_names = [
        'SimHei', 'SimSun', 'Microsoft YaHei', 'Microsoft JhengHei',
        'WenQuanYi Micro Hei', 'WenQuanYi Zen Hei', 'Noto Sans CJK',
        'Source Han Sans', 'Arial Unicode MS', 'Hiragino Sans GB'
    ]
    
    for font_name in chinese_font_names:
        if font_name in fonts:
            chinese_fonts.append(font_name)
    
    print("可用的中文字体:")
    for font in chinese_fonts:
        print(f"  - {font}")
    
    return chinese_fonts

# 检查可用字体
available_fonts = check_chinese_fonts()

# 如果有可用的中文字体，设置为默认字体
if available_fonts:
    plt.rcParams['font.family'] = ['sans-serif']
    plt.rcParams['font.sans-serif'] = available_fonts + ['DejaVu Sans']
    print(f"\n已设置字体: {available_fonts[0]}")
else:
    print("\n警告: 未找到中文字体，可能仍会出现中文显示问题")
    # 尝试下载并使用Noto字体
    try:
        import requests
        import tempfile
        print("尝试下载Noto Sans CJK字体...")
    except ImportError:
        print("请安装requests库以自动下载字体，或手动安装中文字体")



class ProfileVisualizationTool:
    def __init__(self, profile_files: List[str], output_path: str = None):
        """
        初始化可视化工具
        
        Args:
            profile_files: profile_results.json文件路径列表
            output_path: 输出图片路径，默认为第一个profile文件所在目录
        """
        self.profile_files = profile_files
        self.profiles_data = []
        self.output_path = output_path or str(Path(profile_files[0]).parent / "profile_visualization.png")
        
        # 加载所有profile数据
        self._load_profiles()
        
        # 颜色配置
        self.colors = {
            'node': '#4CAF50',
            'start_node': '#2196F3', 
            'end_node': '#FF5722',
            'final_node': '#9C27B0',
            'edge': '#666666',
            'text': '#333333'
        }
        
    def _load_profiles(self):
        """加载所有profile数据"""
        for file_path in self.profile_files:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.profiles_data.append({
                        'file_path': file_path,
                        'data': data,
                        'name': Path(file_path).parent.name  # 使用父目录名作为任务名
                    })
            except Exception as e:
                print(f"加载文件 {file_path} 失败: {e}")
    
    def _format_duration(self, start_ns: int, end_ns: int) -> str:
        """格式化持续时间"""
        duration_ms = (end_ns - start_ns) / 1_000_000
        if duration_ms < 1000:
            return f"{duration_ms:.1f}ms"
        else:
            return f"{duration_ms/1000:.1f}s"
    
    def _format_resource_usage(self, stage_data: Dict) -> str:
        """格式化资源使用信息"""
        summary = stage_data.get('summary', {})
        mem_usage = stage_data.get('mem_usage', {})
        io_usage = stage_data.get('io_usage', {})
        
        # CPU使用 (转换为秒)
        big_core_s = summary.get('big_core', 0) / 1000
        little_core_s = summary.get('little_core', 0) / 1000
        
        # 内存使用 (页面数转换为MB，假设4KB页面)
        vmstat = mem_usage.get('vmstat', {})
        anon_pages_mb = vmstat.get('nr_anon_pages', {}).get('delta', 0) * 4 / 1024
        
        # IO使用
        io_read_mb = io_usage.get('block_read_mb', 0)
        io_write_mb = io_usage.get('block_write_mb', 0)
        
        lines = []
        if big_core_s > 0.01 or little_core_s > 0.01:
            lines.append(f"CPU: {big_core_s:.1f}s(大核) {little_core_s:.1f}s(小核)")
        if abs(anon_pages_mb) > 0.1:
            lines.append(f"内存: {anon_pages_mb:+.1f}MB")
        if io_read_mb > 0.1 or io_write_mb > 0.1:
            lines.append(f"IO: R{io_read_mb:.1f}MB W{io_write_mb:.1f}MB")
        
        return "\n".join(lines) if lines else "资源使用较少"
    
    def _create_subplot_graph(self, profile_data: Dict, ax, subplot_idx: int, global_max_duration: float, max_total_width: float) -> Tuple[float, float]:
        """
        为单个profile创建子图
        
        Args:
            global_max_duration: 所有子任务中最长的单步时长，用于归一化边长
            max_total_width: 所有子任务中最大的总宽度，用于统一X轴范围
        
        Returns:
            (final_x, final_y): 最终节点的坐标，用于连接到总终点
        """
        data = profile_data['data']
        task_name = profile_data['name']
        
        # 创建有向图
        G = nx.DiGraph()
        
        # 节点位置
        positions = {}
        node_labels = {}
        edge_labels = {}
        
        # 添加起始节点
        start_node = "start"
        G.add_node(start_node)
        positions[start_node] = (0, 0)
        node_labels[start_node] = "应用\n未启动"
        
        # 添加每个阶段的节点和边
        current_x = 0
        current_node = start_node
        
        for i, stage in enumerate(data):
            stage_duration = stage['end'] - stage['start']
            
            # 计算边长（基于全局最大时长归一化） - 移除最小长度限制确保严格等比
            edge_length = (stage_duration / global_max_duration) * 10  # 基础长度为10
            # 不设置最小长度，保持严格等比例
            
            # 调试输出
            print(f"  {task_name} - 阶段{i+1}: {stage_duration/1_000_000:.1f}ms -> 边长{edge_length:.2f}")
            
            # 创建新节点
            new_node = f"stage_{i}"
            current_x += edge_length
            positions[new_node] = (current_x, 0)
            
            # 节点标签 - 提取关键操作名
            description = stage['description']
            if "用户交互间隔" in description and "到" in description:
                # 提取操作名
                operation = description.split("到 ")[-1].rstrip(")")
                node_labels[new_node] = operation[:10] + "..." if len(operation) > 10 else operation
            elif "应用启动阶段" in description:
                node_labels[new_node] = "应用启动\n完成"
            elif "最后阶段" in description:
                node_labels[new_node] = "任务\n完成"
            else:
                node_labels[new_node] = f"阶段{i+1}"
            
            # 添加节点和边
            G.add_node(new_node)
            G.add_edge(current_node, new_node)
            
            # 边标签 - 显示时间和资源使用
            duration_str = self._format_duration(stage['start'], stage['end'])
            resource_str = self._format_resource_usage(stage)
            edge_labels[(current_node, new_node)] = f"{duration_str}\n{resource_str}"
            
            current_node = new_node
        
        # 绘制图
        ax.set_title(f"子任务: {task_name}", fontsize=12, fontweight='bold', pad=20)
        
        # 绘制节点
        node_colors = []
        for node in G.nodes():
            if node == "start":
                node_colors.append(self.colors['start_node'])
            elif node == current_node:  # 最后一个节点
                node_colors.append(self.colors['end_node'])
            else:
                node_colors.append(self.colors['node'])
        
        nx.draw_networkx_nodes(G, positions, ax=ax, 
                              node_color=node_colors, 
                              node_size=800,
                              alpha=0.8)
        
        # 绘制边
        nx.draw_networkx_edges(G, positions, ax=ax,
                              edge_color=self.colors['edge'],
                              arrows=True,
                              arrowsize=20,
                              arrowstyle='->',
                              width=2)
        
        # 绘制节点标签
        nx.draw_networkx_labels(G, positions, node_labels, ax=ax,
                               font_size=8,
                               font_color=self.colors['text'])
        
        # 绘制边标签 - 优化标签位置避免重叠
        edge_pos = {}
        for i, edge in enumerate(G.edges()):
            x1, y1 = positions[edge[0]]
            x2, y2 = positions[edge[1]]
            # 交替放置标签在上方和下方
            offset_y = 0.4 if i % 2 == 0 else -0.6
            edge_pos[edge] = ((x1 + x2) / 2, (y1 + y2) / 2 + offset_y)
        
        for edge, (x, y) in edge_pos.items():
            label = edge_labels.get(edge, "")
            ax.text(x, y, label, fontsize=6, ha='center', va='center',
                   bbox=dict(boxstyle="round,pad=0.2", facecolor="white", alpha=0.8, edgecolor='lightgray'))
        
        # 设置坐标轴 - 所有子图使用相同的X轴范围确保等比例
        ax.set_xlim(-0.5, max_total_width + 0.5)
        ax.set_ylim(-1.2, 1.2)
        ax.axis('off')
        
        print(f"  {task_name} 总宽度: {current_x:.2f}, 统一X轴范围: {max_total_width + 1:.2f}")
        
        # 返回最终节点坐标（转换为全局坐标系）
        return current_x, 0
    
    def create_visualization(self):
        """创建完整的可视化图表"""
        num_tasks = len(self.profiles_data)
        
        if num_tasks == 0:
            print("没有有效的profile数据")
            return
        
        # 计算全局最大单步时长，用于归一化所有边长
        global_max_duration = 0
        for profile_data in self.profiles_data:
            for stage in profile_data['data']:
                stage_duration = stage['end'] - stage['start']
                global_max_duration = max(global_max_duration, stage_duration)
        
        # 计算每个子任务的总宽度，找出最大宽度用于统一X轴范围
        max_total_width = 0
        for profile_data in self.profiles_data:
            total_width = 0
            for stage in profile_data['data']:
                stage_duration = stage['end'] - stage['start']
                edge_length = (stage_duration / global_max_duration) * 10
                total_width += edge_length
            max_total_width = max(max_total_width, total_width)
        
        print(f"全局最大单步时长: {global_max_duration / 1_000_000:.1f} ms")
        print(f"最大总宽度: {max_total_width:.2f}")
        
        # 计算子图布局 - 垂直排列
        if num_tasks == 1:
            rows, cols = 1, 1
            fig_height = 6
        elif num_tasks == 2:
            rows, cols = 2, 1  # 两个任务垂直排列！！！
            fig_height = 12  # 增加高度容纳两个垂直任务
        elif num_tasks <= 4:
            rows, cols = 4, 1  # 垂直排列
            fig_height = 16
        elif num_tasks <= 6:
            rows, cols = 6, 1  # 垂直排列
            fig_height = 20
        else:
            rows = num_tasks
            cols = 1
            fig_height = rows * 4 + 2
        
        # 创建图形 - 垂直排列布局，宽度增加以容纳汇总节点
        fig = plt.figure(figsize=(16, fig_height))
        
        # 存储每个子图的最终节点位置
        final_positions = []
        
        # 创建子图
        for i, profile_data in enumerate(self.profiles_data):
            ax = plt.subplot(rows, cols, i + 1)
            final_x, final_y = self._create_subplot_graph(profile_data, ax, i, global_max_duration, max_total_width)
            
            # 存储最终位置（需要转换为全局坐标）
            final_positions.append((ax, final_x, final_y))
        
        # 如果有多个任务，创建汇总部分
        if num_tasks > 1:
            self._add_summary_connection(fig, final_positions)
        
        # 添加图例
        self._add_legend(fig)
        
        # 添加总标题
        fig.suptitle('Android应用性能分析 - 多任务流程图（垂直排列）', fontsize=16, fontweight='bold', y=0.98)
        
        # 调整布局 - 为垂直排列优化，给汇总节点更多右侧空间
        if num_tasks <= 2:
            plt.tight_layout()
            plt.subplots_adjust(top=0.94, bottom=0.1, hspace=0.3, right=0.8)  # 更多右侧空间
        else:
            plt.tight_layout()
            plt.subplots_adjust(top=0.95, bottom=0.05, hspace=0.2, right=0.8)
        
        # 保存图片
        plt.savefig(self.output_path, dpi=300, bbox_inches='tight')
        print(f"可视化图表已保存到: {self.output_path}")
        
        # 显示图片
        plt.show()
    
    def _add_summary_connection(self, fig, final_positions):
        """添加汇总连接，将所有子任务的终点连接到总终点"""
        num_tasks = len(final_positions)
        
        # 计算所有子任务的中心位置
        total_y = 0
        for ax, final_x, final_y in final_positions:
            bbox = ax.get_position()
            center_y = bbox.y0 + bbox.height / 2
            total_y += center_y
        avg_y = total_y / num_tasks
        
        # 汇总节点位置：在所有子任务的右侧中间位置 - 往右移动更多
        summary_x = 0.92  # 更靠右的位置
        summary_y = avg_y  # 垂直中心位置
        
        # 添加汇总节点
        summary_circle = plt.Circle((summary_x, summary_y), 0.03, 
                                  color=self.colors['final_node'], 
                                  transform=fig.transFigure, 
                                  zorder=10)
        fig.patches.append(summary_circle)
        
        # 添加汇总节点标签 - 调整标签位置
        fig.text(summary_x + 0.05, summary_y, '所有任务\n完成汇总', 
                ha='left', va='center', fontsize=10, fontweight='bold',
                transform=fig.transFigure)
        
        # 连接每个子图的终点到汇总节点
        for ax, final_x, final_y in final_positions:
            # 获取子图在整个figure中的位置
            bbox = ax.get_position()
            
            # 将子图坐标转换为figure坐标
            ax_xlims = ax.get_xlim()
            ax_ylims = ax.get_ylim()
            
            # 计算最终节点在figure坐标系中的位置
            final_x_fig = bbox.x0 + (final_x - ax_xlims[0]) / (ax_xlims[1] - ax_xlims[0]) * bbox.width
            final_y_fig = bbox.y0 + (final_y - ax_ylims[0]) / (ax_ylims[1] - ax_ylims[0]) * bbox.height
            
            # 绘制连接线 - 从每个子任务终点到右侧汇总节点
            arrow = mpatches.FancyArrowPatch((final_x_fig, final_y_fig), (summary_x, summary_y),
                                           connectionstyle="arc3,rad=0.0",  # 直线连接
                                           arrowstyle='->', 
                                           mutation_scale=20,
                                           color=self.colors['edge'],
                                           linewidth=2,
                                           alpha=0.7,
                                           transform=fig.transFigure,
                                           zorder=5)
            fig.patches.append(arrow)
    
    def _add_legend(self, fig):
        """添加图例"""
        legend_elements = [
            plt.Line2D([0], [0], marker='o', color='w', markerfacecolor=self.colors['start_node'], 
                      markersize=10, label='应用启动'),
            plt.Line2D([0], [0], marker='o', color='w', markerfacecolor=self.colors['node'], 
                      markersize=10, label='操作节点'),
            plt.Line2D([0], [0], marker='o', color='w', markerfacecolor=self.colors['end_node'], 
                      markersize=10, label='任务完成'),
            plt.Line2D([0], [0], marker='o', color='w', markerfacecolor=self.colors['final_node'], 
                      markersize=10, label='总汇总'),
            plt.Line2D([0], [0], color=self.colors['edge'], linewidth=2, label='操作流程')
        ]
        
        fig.legend(handles=legend_elements, loc='lower center', ncol=5, 
                  bbox_to_anchor=(0.5, 0.02), fontsize=10)

    def generate_summary_report(self):
        """生成汇总报告"""
        print("\n" + "="*80)
        print("Android应用性能分析 - 汇总报告")
        print("="*80)
        
        for i, profile_data in enumerate(self.profiles_data):
            data = profile_data['data']
            task_name = profile_data['name']
            
            print(f"\n【子任务 {i+1}: {task_name}】")
            print("-" * 50)
            
            total_duration = sum((stage['end'] - stage['start']) for stage in data)
            total_cpu_big = sum(stage.get('summary', {}).get('big_core', 0) for stage in data)
            total_cpu_little = sum(stage.get('summary', {}).get('little_core', 0) for stage in data)
            total_io_read = sum(stage.get('io_usage', {}).get('block_read_mb', 0) for stage in data)
            total_io_write = sum(stage.get('io_usage', {}).get('block_write_mb', 0) for stage in data)
            
            print(f"总耗时: {total_duration / 1_000_000:.1f} ms")
            print(f"CPU使用: 大核 {total_cpu_big/1000:.1f}s, 小核 {total_cpu_little/1000:.1f}s")
            print(f"IO使用: 读取 {total_io_read:.1f}MB, 写入 {total_io_write:.1f}MB")
            print(f"操作步骤数: {len(data)}")
            
            print("\n各阶段详情:")
            for j, stage in enumerate(data):
                duration = (stage['end'] - stage['start']) / 1_000_000
                print(f"  {j+1}. {stage['description']}: {duration:.1f}ms")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='可视化Android应用性能分析结果')
    parser.add_argument('profile_files', nargs='+', help='profile_results.json文件路径')
    parser.add_argument('-o', '--output', help='输出图片路径')
    parser.add_argument('--report', action='store_true', help='显示详细报告')
    
    args = parser.parse_args()
    
    # 验证文件存在
    valid_files = []
    for file_path in args.profile_files:
        if os.path.exists(file_path):
            valid_files.append(file_path)
        else:
            print(f"警告: 文件不存在 {file_path}")
    
    if not valid_files:
        print("错误: 没有找到有效的profile文件")
        return
    
    # 创建可视化工具
    visualizer = ProfileVisualizationTool(valid_files, args.output)
    
    # 生成可视化
    visualizer.create_visualization()
    
    # 生成报告
    if args.report:
        visualizer.generate_summary_report()


if __name__ == "__main__":
    # 如果没有命令行参数，使用示例文件
    import sys
    if len(sys.argv) == 1:
        # 查找当前目录下的profile_results.json文件
        current_dir = Path.cwd()
        profile_files = list(current_dir.glob("**/profile_results.json"))
        
        if profile_files:
            print(f"找到 {len(profile_files)} 个profile文件:")
            for f in profile_files:
                print(f"  {f}")
            
            visualizer = ProfileVisualizationTool([str(f) for f in profile_files])
            visualizer.create_visualization()
            visualizer.generate_summary_report()
        else:
            print("当前目录下未找到profile_results.json文件")
            print("用法: python visualize_profile_results.py <profile_file1> [profile_file2] ...")
    else:
        main()
