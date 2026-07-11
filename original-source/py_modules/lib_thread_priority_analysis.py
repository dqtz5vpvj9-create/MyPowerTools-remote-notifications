import glob
import importlib
import math
from pathlib import Path
import sys
import pandas as pd
from collections import defaultdict
import logging
from typing import Dict, Tuple, Optional, Callable, List
import re, os
from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig
import concurrent.futures
import threading
import time


def import_parents(level: int = 1) -> None:
    global __package__
    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[level]
    
    sys.path.append(str(top))
    try:
        sys.path.remove(str(parent))
    except ValueError: # already removed
        pass

    __package__ = '.'.join(parent.parts[len(top.parts):])
    importlib.import_module(__package__) # won't be needed after that

if __name__ == '__main__' and (__package__ is None or len(__package__) == 0):
    import_parents()

import copy

from py_modules.logging_lib import setup_logging
from py_modules.lib_frame_processor_inner import process_frame_trace

def get_average_priority(
    tp: TraceProcessor,
    main_thread_utid: Optional[int],
    render_thread_utid: Optional[int],
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    logger=None
) -> tuple[Optional[float], Optional[float]]:
    """
    计算主线程和渲染线程的加权平均优先级。
    
    在Android中，优先级值越低通常表示实际优先级越高。
    此函数根据每个调度片段的持续时间计算加权平均值。
    
    Args:
        tp: TraceProcessor实例
        main_thread_utid: 主线程的UTID（如果不可用可为None）
        render_thread_utid: 渲染线程的UTID（如果不可用可为None）
        start_ts: 可选的开始时间戳，用于过滤调度片段（纳秒）
        end_ts: 可选的结束时间戳，用于过滤调度片段（纳秒）
        logger: 用于记录警告/错误的Logger实例（可选）
        
    Returns:
        包含(main_thread_avg_priority, render_thread_avg_priority)的元组
        如果相应的线程UTID为None或没有可用数据，则对应值将为None
    """
    def calculate_avg_priority(utid: int) -> Optional[float]:
        if utid is None:
            return None
        
        try:
            # 查询与我们的时间范围重叠的所有片段
            time_filter = ""
            if start_ts is not None and end_ts is not None:
                time_filter = f"AND (ts + dur > {start_ts} AND ts < {end_ts})"
            elif start_ts is not None:
                time_filter = f"AND ts + dur > {start_ts}"
            elif end_ts is not None:
                time_filter = f"AND ts < {end_ts}"
            
            query = f"""
            SELECT priority, ts, dur
            FROM sched
            WHERE utid = {utid}
            {time_filter}
            """
            data = tp.query(query).as_pandas_dataframe()
            
            if data.empty:
                return None
            
            # 计算加权平均值，调整跨越边界的片段的持续时间
            weighted_sum = 0
            total_dur = 0
            
            for _, row in data.iterrows():
                # 计算重叠持续时间
                slice_start = row['ts']
                slice_end = row['ts'] + row['dur']
                
                if start_ts is not None:
                    slice_start = max(slice_start, start_ts)
                if end_ts is not None:
                    slice_end = min(slice_end, end_ts)
                
                overlap_dur = max(0, slice_end - slice_start)
                
                # 加入加权总和
                weighted_sum += row['priority'] * overlap_dur
                total_dur += overlap_dur
            
            if total_dur > 0:
                return weighted_sum / total_dur
            return None
        
        except Exception as e:
            if logger:
                logger.warning(f"计算线程优先级时出错，utid {utid}: {e}")
            return None
    
    # 计算两个线程的平均优先级
    main_thread_avg_priority = calculate_avg_priority(main_thread_utid)
    render_thread_avg_priority = calculate_avg_priority(render_thread_utid)
    
    return main_thread_avg_priority, render_thread_avg_priority



def get_main_render_avg_priority(df, trace_path, main_proc_pid, target_process, logger, is_bg = False):
    """
    计算指定跟踪文件中主线程和渲染线程的加权平均优先级。
    
    Args:
        df: 包含帧信息的Pandas DataFrame
        trace_path: 跟踪文件的路径
        main_proc_pid: 主进程的PID
        target_process: 目标进程名称
        logger: 用于记录信息的Logger实例
        
    Returns:
        包含(main_thread_avg_priority, render_thread_avg_priority)的元组
    """
    # 初始化TraceProcessor
    try:
        tp = TraceProcessor(
            trace=trace_path, 
            config=TraceProcessorConfig(bin_path=os.path.expanduser('~/.local/bin/trace_processor'))
        )
    except Exception as e:
        logger.error(f"Error initializing TraceProcessor: {e}")
        return None, None
    
    # 如果从命令行参数中没有获取到PID，尝试从文件名中提取
    if not main_proc_pid and not is_bg:
        pid_match = re.search(r'perfetto-.*-(\d+)-\d+\.\d+\.trace', os.path.basename(trace_path))
        if pid_match:
            main_proc_pid = int(pid_match.group(1))
            logger.info(f"Extracted PID from trace file name: {main_proc_pid}")
        else:
            logger.error("Could not extract PID from trace file name")
            return None, None
    if main_proc_pid is not None:
        query = f"pid = {main_proc_pid}"
    else:
        query = f"name = '{target_process}'"
    
    # 查找主进程和线程
    try:
        # 查找主进程
        main_process_info = tp.query(f"""
            SELECT upid, pid, name FROM process
            WHERE {query}
            LIMIT 1
        """).as_pandas_dataframe()
        
        if main_process_info.empty:
            logger.warning(f"Could not find main process with PID {main_proc_pid}, file is {trace_path}")
            return None, None
            
        main_process_upid = main_process_info['upid'].iloc[0]
        if main_proc_pid is None:
            main_proc_pid = main_process_info['pid'].iloc[0]
        
        # 查找主线程
        main_thread_info = tp.query(f"""
            SELECT tid, utid, name FROM thread
            WHERE tid = {main_proc_pid} 
            LIMIT 1
        """).as_pandas_dataframe()
        
        main_thread_utid = main_thread_info['utid'].iloc[0] if not main_thread_info.empty else None
        
        if main_thread_utid is not None:
            logger.info(f"Found Main Thread: name='{main_thread_info['name'].iloc[0]}', utid={main_thread_utid}")
        else:
            logger.warning(f"Could not find main thread for process {main_proc_pid}")
            
        # 查找RenderThread
        render_thread_info = tp.query(f"""
            SELECT tid, utid FROM thread
            WHERE name = 'RenderThread'
                AND upid = {main_process_upid}
            LIMIT 1
        """).as_pandas_dataframe()
        
        render_thread_utid = render_thread_info['utid'].iloc[0] if not render_thread_info.empty else None
        
        if render_thread_utid is not None:
            logger.info(f"Found RenderThread: utid={render_thread_utid}")
        else:
            logger.warning(f"Could not find RenderThread for process {target_process}")
    
    except Exception as e:
        logger.error(f"Error finding thread UTIDs: {e}")
        return None, None
    
    # 如果没找到任一线程，则返回None
    if main_thread_utid is None and render_thread_utid is None:
        logger.warning("Could not find either main thread or render thread")
        return None, None
    
    # 确定分析的时间范围
    start_ts = None
    end_ts = None

    main_priority, render_priority = get_average_priority(
        tp,
        main_thread_utid,
        render_thread_utid,
        start_ts=start_ts,
        end_ts=end_ts,
        logger=logger
    )
    
    # logger.info(f"Main Thread Average Priority: {main_priority if main_priority is not None else 'N/A'}")
    # logger.info(f"Render Thread Average Priority: {render_priority if render_priority is not None else 'N/A'}")
    
    return main_priority, render_priority


