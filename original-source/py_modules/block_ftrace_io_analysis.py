#!/usr/bin/env python3
"""
改进版Perfetto IO分析脚本
支持命令行参数配置，分析顺序/随机IO模式
"""

import pandas as pd
import numpy as np
import argparse
import sys
import os
from datetime import datetime
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from collections import defaultdict
import matplotlib.font_manager as fm

# Path to your font file
font_path = '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'

# Register the font with matplotlib's font manager
fm.fontManager.addfont(font_path)

# Create a FontProperties object from the font file
my_font = fm.FontProperties(fname=font_path)

# Get the internal font name (it should now be properly registered)
font_name = my_font.get_name()
print("Using font:", font_name)

# Set the font globally in rcParams using the registered name
plt.rcParams['font.family'] = font_name  # you can also use 'sans-serif'
plt.rcParams['font.sans-serif'] = [font_name]
plt.rcParams['axes.unicode_minus'] = False


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='分析Perfetto trace导出的IO数据，识别顺序和随机IO模式',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 基本用法
  python block_ftrace_io_analysis.py -f data.csv

  # 指定进程筛选和时间范围
  python block_ftrace_io_analysis.py -f data.csv -p com.taobao.taobao -s 10 -d 30

  # 严格顺序判断（扇区必须完全连续）
  python block_ftrace_io_analysis.py -f data.csv -t 0

  # 输出到文件
  python block_ftrace_io_analysis.py -f data.csv -o report.txt

  # 生成扇区条带图
  python block_ftrace_io_analysis.py -f data.csv --plot

  # 指定设备和输出文件
  python block_ftrace_io_analysis.py -f data.csv --plot --plot-device sda --plot-output myplot.png
        """
    )

    parser.add_argument('-f', '--file', required=True,
                       help='block_ftrace_process 脚本输出的 CSV 文件路径')

    parser.add_argument('-p', '--process',
                       help='筛选特定进程名的 I/O（支持部分匹配）')

    parser.add_argument('-s', '--start-time', type=float, default=0,
                       help='分析开始时间（秒，相对于数据开始时间，默认0）')

    parser.add_argument('-d', '--duration', type=float,
                       help='分析持续时间（秒，默认分析到数据结束）')

    parser.add_argument('-t', '--threshold', type=int, default=8,
                       help='顺序IO判断阈值（扇区数差值，默认8）')

    parser.add_argument('-o', '--output',
                       help='输出报告到文件（默认打印到控制台）')

    parser.add_argument('--sector-size', type=int, default=512,
                       help='扇区大小（字节，默认512）')

    parser.add_argument('--plot', action='store_true',
                       help='生成扇区条带图')

    parser.add_argument('--plot-output',
                       help='扇区条带图输出文件（默认为扇区条带图.png）')

    parser.add_argument('--plot-device',
                       help='指定要绘图的设备（默认为数据中最常见的设备）')

    parser.add_argument('-v', '--verbose', action='store_true',
                       help='显示详细处理信息')

    return parser.parse_args()

def load_data(file_path, verbose=False):
    """加载CSV数据"""
    if not os.path.exists(file_path):
        print(f"错误: 文件 '{file_path}' 不存在")
        sys.exit(1)

    try:
        if verbose:
            print(f"正在加载文件: {file_path}")
        df = pd.read_csv(file_path)

        if verbose:
            print(f"数据加载成功，共 {len(df)} 条记录")
            print(f"列名: {list(df.columns)}")

        return df
    except Exception as e:
        print(f"错误: 加载文件失败 - {e}")
        sys.exit(1)

def filter_by_process(df, process_name, verbose=False):
    """根据进程名筛选数据"""
    if not process_name:
        return df

    if verbose:
        print(f"正在筛选包含 '{process_name}' 的进程...")

    # 在process_issue和process_complete列中查找匹配的进程
    mask = (df['process_issue'].str.contains(process_name, na=False, case=False) |
            df['process_complete'].str.contains(process_name, na=False, case=False))

    filtered_df = df[mask].copy()

    if verbose:
        print(f"筛选后剩余 {len(filtered_df)} 条记录")

    return filtered_df

def filter_by_time_range(df, start_time_sec, duration_sec, verbose=False):
    """根据时间范围筛选数据"""
    if start_time_sec == 0 and duration_sec is None:
        return df

    # 确保按时间排序
    df_sorted = df.sort_values('issue_ts')

    # 获取最小时间戳（纳秒）
    min_ts = df_sorted['issue_ts'].min()

    # 转换开始时间到纳秒
    start_ts_ns = min_ts + (start_time_sec * 1e9)
    
    # 直接指定开始时间戳
    # start_ts_ns = 242630017206874

    if verbose:
        print(f"数据时间范围: {datetime.fromtimestamp(min_ts/1e9)} 到 {datetime.fromtimestamp(df_sorted['issue_ts'].max()/1e9)}")
        print(f"分析开始时间: {datetime.fromtimestamp(start_ts_ns/1e9)}")

    # 筛选开始时间后的数据
    mask = df_sorted['issue_ts'] >= start_ts_ns

    if duration_sec is not None:
        # 如果有持续时间，计算结束时间
        end_ts_ns = start_ts_ns + (duration_sec * 1e9)
        mask = mask & (df_sorted['issue_ts'] <= end_ts_ns)
        if verbose:
            print(f"分析结束时间: {datetime.fromtimestamp(end_ts_ns/1e9)}")

    filtered_df = df_sorted[mask].copy()

    if verbose:
        print(f"时间范围筛选后剩余 {len(filtered_df)} 条记录")

    return filtered_df

def analyze_io_patterns(df, threshold=8, sector_size=512, verbose=False, collect_segments=False):
    """分析IO模式（顺序vs随机）"""
    if verbose:
        print(f"正在分析IO模式（顺序判断阈值: {threshold} 扇区）...")

    # 首先需要获取分析的时间跨度
    if len(df) == 0:
        if collect_segments:
            return {}, {}
        else:
            return {}

    # 获取分析的时间范围
    first_request_time = df['issue_ts'].min()
    last_request_time = df['issue_ts'].max()

    # 计算总时长（转换为毫秒）
    total_duration_ms = (last_request_time - first_request_time) / 1e6  # 纳秒到毫秒

    if verbose:
        print(f"分析时间范围: {total_duration_ms:.2f} 毫秒")

    # 初始化统计变量
    stats = {
        'sequential': {'count': 0, 'size': 0, 'time': 0},
        'random': {'count': 0, 'size': 0, 'time': 0},
        'read': {'sequential': {'count': 0, 'size': 0, 'time': 0},
                 'random': {'count': 0, 'size': 0, 'time': 0}},
        'write': {'sequential': {'count': 0, 'size': 0, 'time': 0},
                  'random': {'count': 0, 'size': 0, 'time': 0}},
        'time_info': {
            'start_time': first_request_time,
            'end_time': last_request_time,
            'duration_ms': total_duration_ms
        }
    }

    # 如果需要收集用于绘图的分段数据
    segments_by_device = defaultdict(list) if collect_segments else None

    # 跟踪每个设备的上次访问位置
    last_sector = {}

    for idx, row in df.iterrows():
        dev = str(row['dev'])
        current_sector = int(row['sector'])
        nr_sectors = int(row['nr_sector'])
        rwbs = str(row['rwbs'])
        data_size = nr_sectors * sector_size
        latency_ms = float(row['latency_ms'])

        # 判断顺序/随机IO
        is_sequential = False
        if dev in last_sector:
            prev_end_sector = last_sector[dev]['sector'] + last_sector[dev]['nr_sectors']
            # 如果当前扇区与上次结束扇区的差值小于等于阈值，认为是顺序IO
            if abs(current_sector - prev_end_sector) <= threshold:
                is_sequential = True

        # 收集分段数据用于绘图
        if collect_segments:
            segment = {
                'start_sector': current_sector,
                'length': nr_sectors,
                'rwbs': rwbs,
                'is_sequential': is_sequential,
                'latency_ms': latency_ms
            }
            segments_by_device[dev].append(segment)

        # 更新设备最后访问位置
        last_sector[dev] = {'sector': current_sector, 'nr_sectors': nr_sectors}

        # 更新统计信息
        if is_sequential:
            stats['sequential']['count'] += 1
            stats['sequential']['size'] += data_size
            stats['sequential']['time'] += latency_ms
        else:
            stats['random']['count'] += 1
            stats['random']['size'] += data_size
            stats['random']['time'] += latency_ms

        # 按读写类型分类统计
        if 'R' in rwbs:  # 读操作
            if is_sequential:
                stats['read']['sequential']['count'] += 1
                stats['read']['sequential']['size'] += data_size
                stats['read']['sequential']['time'] += latency_ms
            else:
                stats['read']['random']['count'] += 1
                stats['read']['random']['size'] += data_size
                stats['read']['random']['time'] += latency_ms
        elif 'W' in rwbs:  # 写操作
            if is_sequential:
                stats['write']['sequential']['count'] += 1
                stats['write']['sequential']['size'] += data_size
                stats['write']['sequential']['time'] += latency_ms
            else:
                stats['write']['random']['count'] += 1
                stats['write']['random']['size'] += data_size
                stats['write']['random']['time'] += latency_ms

    if verbose:
        print(f"分析完成，共处理 {len(df)} 条IO记录")

    if collect_segments:
        return stats, segments_by_device
    else:
        return stats

def merge_segments(segments, threshold):
    """合并顺序的段，返回合并后的条带数据"""
    if not segments:
        return []

    # 按扇区号排序
    segments = sorted(segments, key=lambda x: x['start_sector'])

    merged_segments = []
    current_start = segments[0]['start_sector']
    current_length = segments[0]['length']
    current_end = current_start + current_length
    current_color_idx = 0

    for i in range(1, len(segments)):
        seg = segments[i]
        seg_start = seg['start_sector']
        seg_length = seg['length']

        # 如果当前段与上一段是顺序的，就合并它们
        if seg_start <= current_end + threshold:
            current_length += seg_length
            current_end = current_start + current_length
        else:
            # 保存之前的条带
            merged_segments.append({
                'start': current_start,
                'length': current_length,
                'color_idx': current_color_idx  # 交替颜色的索引
            })
            # 开始新的条带
            current_start = seg_start
            current_length = seg_length
            current_end = current_start + current_length
            current_color_idx = (current_color_idx + 1) % 2  # 交替颜色

    # 保存最后一个条带
    if current_length > 0:
        merged_segments.append({
            'start': current_start,
            'length': current_length,
            'color_idx': current_color_idx
        })

    return merged_segments


def plot_sector_strip_chart(segments_by_device, threshold=8, plot_device=None, plot_output=None, verbose=False):
    """绘制扇区条带图"""
    if verbose:
        print("正在生成扇区条带图...")

    # 如果没有指定设备，选择数据最多的设备
    if plot_device is None:
        plot_device = max(segments_by_device.keys(), key=lambda k: len(segments_by_device[k]))
        if verbose:
            print(f"自动选择设备 '{plot_device}' 进行绘图")

    if plot_device not in segments_by_device:
        print(f"警告: 指定设备 '{plot_device}' 在数据中不存在")
        return

    segments = segments_by_device[plot_device]

    if not segments:
        print(f"警告: 设备 '{plot_device}' 没有IO请求数据")
        return
    else:
        print(f"设备 '{plot_device}' 共 '{len(segments)}' 条记录")

    # 合并顺序段落
    merged_segments = merge_segments(segments, threshold)

    if not merged_segments:
        print(f"警告: 设备 '{plot_device}' 没有有效的条带数据")
        return
    else:
        print(f"合并后，设备 '{plot_device}' 共 '{len(merged_segments)}' 条记录")

    # 设置图形参数
    fig, ax = plt.subplots(figsize=(15, 6))

    # 定义交替使用的颜色
    colors = ['#FF6B6B', '#0164F9']  # 红色和蓝色交替

    y_pos = 0
    height = 0.8  # 条带高度

    # 绘制条带
    for seg in merged_segments:
        start = seg['start']
        length = seg['length']
        color_idx = seg['color_idx']

        # 创建矩形条带
        rect = patches.Rectangle(
            (start, y_pos),
            length if length > 100000 else 100000,
            height,
            facecolor=colors[color_idx],
            alpha=1
        )
        ax.add_patch(rect)

        # 在条带中间添加数据标签（如果条带足够大）
        # if length > 10000:
        #     ax.text(
        #         start + length/2,
        #         y_pos + height/2,
        #         f'{length}',
        #         ha='center',
        #         va='center',
        #         fontsize=8,
        #         color='white',
        #         fontweight='bold'
        #     )

    # 设置坐标轴
    ax.set_xlim(min(seg['start'] for seg in merged_segments) - 100,
                max(seg['start'] + seg['length'] for seg in merged_segments) + 100)
    ax.set_ylim(-0.2, 1.2)

    # 计算统计信息用于标题
    total_requests = len(segments)
    total_sectors = sum(seg['length'] for seg in segments)

    # 设置标签和标题
    ax.set_xlabel('扇区号', fontsize=12)
    # ax.set_ylabel('IO请求', fontsize=12)
    title = f'设备 {plot_device} 的扇区访问条带图\n({len(merged_segments)} 个合并段，颜色仅区分相邻段)'
    if verbose:
        title += f'\n原始请求：{total_requests} 条，总扇区：{total_sectors:,}'
    ax.set_title(title, fontsize=14, pad=20)

    # 移除y轴刻度
    ax.set_yticks([])

    # 添加网格
    ax.grid(True, alpha=0.3, linestyle='--')

    # 简单的视觉说明文本（不含图例）- 颜色仅用于区分相邻合并段
    ax.text(0.02, 0.95, '→颜色交替区分相邻段',
            transform=ax.transAxes,
            fontsize=9,
            verticalalignment='top',
            bbox=dict(boxstyle='round', facecolor='lightgray', alpha=0.3))

    # 调整布局
    plt.tight_layout()

    # 保存或显示图片
    if plot_output:
        try:
            plt.savefig(plot_output, dpi=150, bbox_inches='tight')
            print(f"扇区条带图已保存到: {plot_output}")
        except Exception as e:
            print(f"保存图片失败: {e}")
            plt.show()
    else:
        plt.show()

    plt.close()

    if verbose:
        print(f"扇区条带图生成完成")
        print(f"原始请求数: {len(segments)}")
        print(f"合并后的条带数: {len(merged_segments)}")
        total_sectors = sum(seg['length'] for seg in merged_segments)
        print(f"总共覆盖的扇区数: {total_sectors}")

def calc_throughput(size_bytes, duration_ms):
    """正确计算吞吐量 (MB/s)，使用总时间跨度"""
    if duration_ms <= 0:
        return 0
    return (size_bytes / (1024 * 1024)) / (duration_ms / 1000)

def format_bytes(bytes_val):
    """格式化字节数"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_val < 1024:
            return f"{bytes_val:.2f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.2f} TB"

def generate_report(stats, process_filter, file_path, threshold, args):
    """生成分析报告"""

    # 计算总数
    total_count = stats['sequential']['count'] + stats['random']['count']
    total_size = stats['sequential']['size'] + stats['random']['size']

    if total_count == 0:
        return "没有找到符合条件的IO记录\n"

    # 基本信息
    info_lines = [
        "Perfetto IO 性能分析报告",
        "=" * 50,
        f"数据文件: {file_path}",
        f"进程筛选: {process_filter if process_filter else '无'}",
        f"顺序IO阈值: {threshold} 扇区",
        f"分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ""
    ]

    # 如果指定了时间范围
    if args.start_time > 0 or args.duration is not None:
        time_info = f"时间范围: 从 {args.start_time}秒开始"
        if args.duration is not None:
            time_info += f", 持续 {args.duration}秒"
        info_lines.append(time_info)
        info_lines.append("")

    # 获取总分析时长（用于正确的吞吐量计算）
    if stats['time_info']['duration_ms'] > 0:
        # 基于实际时间跨度的吞吐量计算
        total_throughput = calc_throughput(total_size, stats['time_info']['duration_ms'])
        seq_throughput = calc_throughput(stats['sequential']['size'], stats['time_info']['duration_ms']) if stats['sequential']['count'] > 0 else 0
        ran_throughput = calc_throughput(stats['random']['size'], stats['time_info']['duration_ms']) if stats['random']['count'] > 0 else 0
    else:
        total_throughput = seq_throughput = ran_throughput = 0

    report_lines = info_lines + [
        "总体统计:",
        "-" * 30,
        f"总I/O操作数: {total_count}",
        f"总数据量: {format_bytes(total_size)}",
        f"分析时间跨度: {stats['time_info']['duration_ms']:.2f} 毫秒",
        f"总体带宽: {total_throughput:.2f} MB/s",
        "",
        "顺序I/O:",
        f"  操作次数: {stats['sequential']['count']} ({(stats['sequential']['count']/total_count*100):.1f}%)",
        f"  数据量: {format_bytes(stats['sequential']['size'])}",
        f"  总体带宽: {seq_throughput:.2f} MB/s",
        f"  平均请求时长: {(stats['sequential']['time']/stats['sequential']['count']):.2f} ms" if stats['sequential']['count'] > 0 else "  平均请求时长: 0.0 ms",
        f"  平均I/O大小: {(stats['sequential']['size']/stats['sequential']['count']/1024):.2f} KB (按次数)" if stats['sequential']['count'] > 0 else "  平均I/O大小: 0 KB",
        f"  【说明】平均请求时长基于所有请求简单平均，不同I/O大小的延迟对比需谨慎" if stats['sequential']['count'] > 0 else "
        "",
        "随机I/O:",
        f"  操作次数: {stats['random']['count']} ({(stats['random']['count']/total_count*100):.1f}%)",
        f"  数据量: {format_bytes(stats['random']['size'])}",
        f"  总体带宽: {ran_throughput:.2f} MB/s",
        f"  平均请求时长: {(stats['random']['time']/stats['random']['count']):.2f} ms" if stats['random']['count'] > 0 else "  平均请求时长: 0.0 ms",
        f"  平均I/O大小: {(stats['random']['size']/stats['random']['count']/1024):.2f} KB (按次数)" if stats['random']['count'] > 0 else "  平均I/O大小: 0 KB",
        "",
        "【说明】",
        "带宽=总数据量÷分析时间跨度（从第一个请求到最后一个请求）",
        "平均I/O大小=总数据量÷请求次数",
        "平均请求时长=请求延迟总和÷请求次数（用于性能分析）",
        "",
        "按操作类型细分:",
        "-" * 30,
        "读操作:"
    ]

    # 读操作统计
    read_seq_count = stats['read']['sequential']['count']
    read_ran_count = stats['read']['random']['count']
    read_total = read_seq_count + read_ran_count

    if read_total > 0:
        # 读写操作的吞吐量也应基于总时间跨度
        read_seq_throughput = calc_throughput(stats['read']['sequential']['size'], stats['time_info']['duration_ms']) if read_seq_count > 0 else 0
        read_ran_throughput = calc_throughput(stats['read']['random']['size'], stats['time_info']['duration_ms']) if read_ran_count > 0 else 0

        report_lines.extend([
            f"  顺序读:",
            f"    操作次数: {read_seq_count}",
            f"    占比: {(read_seq_count/read_total*100):.1f}%",
            f"    数据量: {format_bytes(stats['read']['sequential']['size'])}" if read_seq_count > 0 else "    数据量: 0 B",
            f"    总体带宽: {read_seq_throughput:.2f} MB/s" if read_seq_count > 0 else "    总体带宽: 0 MB/s",
            f"    平均请求时长: {(stats['read']['sequential']['time']/read_seq_count if read_seq_count > 0 else 0):.2f} ms" if read_seq_count > 0 else "    平均请求时长: 0.0 ms",
            f"    平均I/O大小: {(stats['read']['sequential']['size']/read_seq_count/1024):.2f} KB" if read_seq_count > 0 else "    平均I/O大小: 0 KB",
            f"  随机读:",
            f"    操作次数: {read_ran_count}",
            f"    占比: {(read_ran_count/read_total*100):.1f}%",
            f"    数据量: {format_bytes(stats['read']['random']['size'])}" if read_ran_count > 0 else "    数据量: 0 B",
            f"    总体带宽: {read_ran_throughput:.2f} MB/s" if read_ran_count > 0 else "    总体带宽: 0 MB/s",
            f"    平均请求时长: {(stats['read']['random']['time']/read_ran_count if read_ran_count > 0 else 0):.2f} ms" if read_ran_count > 0 else "    平均请求时长: 0.0 ms",
            f"    平均I/O大小: {(stats['read']['random']['size']/read_ran_count/1024):.2f} KB" if read_ran_count > 0 else "    平均I/O大小: 0 KB"
        ])
    else:
        report_lines.append("  无读操作")

    # 写操作统计
    write_seq_count = stats['write']['sequential']['count']
    write_ran_count = stats['write']['random']['count']
    write_total = write_seq_count + write_ran_count

    if write_total > 0:
        write_seq_throughput = calc_throughput(stats['write']['sequential']['size'], stats['time_info']['duration_ms']) if write_seq_count > 0 else 0
        write_ran_throughput = calc_throughput(stats['write']['random']['size'], stats['time_info']['duration_ms']) if write_ran_count > 0 else 0

        report_lines.extend([
            "",
            "写操作:"
        ])
        report_lines.extend([
            f"  顺序写:",
            f"    操作次数: {write_seq_count}",
            f"    占比: {(write_seq_count/write_total*100):.1f}%",
            f"    数据量: {format_bytes(stats['write']['sequential']['size'])}" if write_seq_count > 0 else "    数据量: 0 B",
            f"    总体带宽: {write_seq_throughput:.2f} MB/s" if write_seq_count > 0 else "    总体带宽: 0 MB/s",
            f"    平均请求时长: {(stats['write']['sequential']['time']/write_seq_count if write_seq_count > 0 else 0):.2f} ms" if write_seq_count > 0 else "    平均请求时长: 0.0 ms",
            f"    平均I/O大小: {(stats['write']['sequential']['size']/write_seq_count/1024):.2f} KB" if write_seq_count > 0 else "    平均I/O大小: 0 KB",
            f"  随机写:",
            f"    操作次数: {write_ran_count}",
            f"    占比: {(write_ran_count/write_total*100):.1f}%",
            f"    数据量: {format_bytes(stats['write']['random']['size'])}" if write_ran_count > 0 else "    数据量: 0 B",
            f"    总体带宽: {write_ran_throughput:.2f} MB/s" if write_ran_count > 0 else "    总体带宽: 0 MB/s",
            f"    平均请求时长: {(stats['write']['random']['time']/write_ran_count if write_ran_count > 0 else 0):.2f} ms" if write_ran_count > 0 else "    平均请求时长: 0.0 ms",
            f"    平均I/O大小: {(stats['write']['random']['size']/write_ran_count/1024):.2f} KB" if write_ran_count > 0 else "    平均I/O大小: 0 KB"
        ])
    else:
        report_lines.extend([
            "",
            "写操作:",
            "  无写操作"
        ])

    return "\n".join(report_lines) + "\n"

def main():
    """主函数"""
    args = parse_args()

    # 加载数据
    df = load_data(args.file, args.verbose)

    # 筛选进程
    df = filter_by_process(df, args.process, args.verbose)

    # 时间范围筛选
    df = filter_by_time_range(df, args.start_time, args.duration, args.verbose)

    # 分析IO模式并收集分段数据如果启用了绘图功能
    if args.plot:
        stats, segments_by_device = analyze_io_patterns(df, args.threshold, args.sector_size, args.verbose, collect_segments=True)
    else:
        stats = analyze_io_patterns(df, args.threshold, args.sector_size, args.verbose)

    # 生成报告
    report = generate_report(stats, args.process, args.file, args.threshold, args)

    # 如果启用了绘图功能，生成扇区条带图
    if args.plot:
        plot_output = args.plot_output or f"扇区条带图_{os.path.basename(args.file)}.png"
        plot_sector_strip_chart(segments_by_device, args.threshold,
                                args.plot_device, plot_output, args.verbose)

    # 输出结果
    if args.output:
        try:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(report)
            print(f"报告已保存到: {args.output}")
        except Exception as e:
            print(f"保存文件失败: {e}")
            print("将结果输出到控制台:")
            print(report)
    else:
        print(report)

if __name__ == "__main__":
    main()