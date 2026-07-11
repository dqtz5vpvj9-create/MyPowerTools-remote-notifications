import json
import traceback
"""
任务画像生成器（Perfetto → JSON 概览，极简版）

快速用法
- 单应用：python3 py_modules/generate_task_profile_json.py --trace-path /path/trace --app-name <AppName> --bind-cores 2-5
- 多应用：同目录存在 <app>.log 即自动识别多应用，直接运行以上命令（可省略 --app-name）。

关键参数（tapify 自动生成 CLI）
- --trace-path：Perfetto trace 路径（必填）
- --app-name：单应用名称（可选；不传则根据 tasks.json/日志推断）
- --reprocess：重新计算（忽略已有 profile_results.json）
- --bind-cores：参与分析的 CPU 列表，支持区间与逗号（例：2-5 或 2,3,4,5）

默认行为
- 自动识别多应用（同目录存在 <app>.log 则进入多应用模式）
- 读取 tasks.json 以获取包名与启动 Activity
- 以 1s 为步长聚合，分阶段输出指标

输出
- JSON：profile_results.json（每段 CPU/内存/IO/Looper/Idle 等摘要与时间线）
- 终端：按阶段打印摘要与总体占比

主要指标
- CPU 使用率与 CPU 时间（分段聚合，支持绑定核心）
- 内存：vmstat 指标与缺页（minor/major）
- IO：块设备读写量/次数（MB/次）
- Looper：Frame/非Frame 处理耗时
- CPU4/5 idle 与主线程/RenderThread 占用占比及合并占比

依赖
- perfetto Python 接口与 trace_processor 二进制（可在初始化参数中设置路径）
"""

from typing import OrderedDict, Optional, List, Dict, Any
from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig
import pandas as pd
import numpy as np
import os
import re
import sys
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path
from functional import seq
import importlib, sys
from pathlib import Path
from tap import tapify
import sys
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
from .lib_sched_analyzer import SchedSliceAnalyzer, query_sched_slices

tasks_data = {}
with open('/home/ysh/repo/autodroid/tasks.json', 'r') as f:
    tasks_data = json.load(f)
tasks = tasks_data['tasks']

class PerformanceProfiler:
    def __init__(self, trace_path: str, pkg_name: Optional[str] = None, app_name: Optional[str] = None, 
                 launch_activity: Optional[str] = None, trace_processor_bin: str = '~/.local/bin/trace_processor_shell_51.2_release', 
                 multi_app_mode: bool = False, bind_cores: Optional[List[int]] = None, 
                 strict_click_matching: bool = True, trace_processor: 'TraceProcessor' = None,
                 skip_initial_load: bool = False):
        """
        初始化性能分析器
        
        Args:
            trace_path: perfetto trace文件路径
            pkg_name: 单应用模式的包名
            app_name: 单应用模式的应用名
            launch_activity: 单应用模式的启动Activity
            trace_processor_bin: trace processor二进制文件路径
            multi_app_mode: 是否为多应用模式
            bind_cores: 绑定的CPU核心列表
            strict_click_matching: 是否严格要求点击记录匹配，设为False时会使用宽松模式
            trace_processor: 可选的现有TraceProcessor实例，用于复用避免重复加载trace
            skip_initial_load: 是否跳过初始数据加载（用于render_only快速分析）
        """
        self.trace_path = trace_path
        self.pkg_name = pkg_name
        self.launch_activity = launch_activity
        self.multi_app_mode = multi_app_mode
        self.bind_cores = bind_cores
        self.render_only = False  # 默认执行全量分析
        self.strict_click_matching = strict_click_matching
        
        if not multi_app_mode:
            self.record_path = Path(trace_path).parent / f"{app_name}.log"
        else:
            # 多应用模式下，从trace路径推断应用列表
            self.app_configs = self._detect_multi_apps()
        
        # 初始化TraceProcessor（可复用现有实例）
        if trace_processor is not None:
            self.tp = trace_processor
            self._owns_trace_processor = False  # 不负责关闭
        else:
            self.tp = TraceProcessor(
                trace=trace_path,
                config=TraceProcessorConfig(
                    bin_path=os.path.expanduser(trace_processor_bin), 
                    ingest_ftrace_in_raw=True
                )
            )
            self._owns_trace_processor = True  # 负责关闭
        
        # 内部状态
        self.call_cnt = 0
        self.ams_msgs = None
        self.events = None
        self.first_events = None
        self.app_launch_start_ts = None
        self.app_launch_end_ts = None
        
        # 初始化数据（可跳过用于render_only快速分析）
        if not skip_initial_load:
            self._load_initial_data()

    @staticmethod
    def parse_time_to_ns(time_str):
        """解析时间字符串为纳秒"""
        units = {'s': 1_000_000_000, 'ms': 1_000_000, 'us': 1_000, 'ns': 1}
        pattern = re.compile(r'(\d+)(s|ms|us|ns)')
        total_ns = 0
        for value, unit in pattern.findall(time_str):
            total_ns += int(value) * units[unit]
        return total_ns

    def _detect_multi_apps(self):
        """检测多应用模式下的应用配置"""
        trace_dir = Path(self.trace_path).parent
        
        # 应用配置映射
        try:
            app_configs = {
                task['name']: {
                    'pkg_name': task['package_name'],
                    'launch_activity': task['displayed_activity']
                } for task in tasks
            }
        except Exception as e:
            print(f"加载 tasks.json 失败: {e}")
            print(tasks)
            raise e
        
        detected_apps = {}
        for app_name, config in app_configs.items():
            log_path = trace_dir / f"{app_name}.log"
            if log_path.exists():
                detected_apps[app_name] = {
                    'pkg_name': config['pkg_name'],
                    'launch_activity': config['launch_activity'],
                    'log_path': log_path
                }
        
        print(f"检测到多应用模式，发现 {len(detected_apps)} 个应用: {list(detected_apps.keys())}")
        return detected_apps

    def _load_initial_data(self):
        """加载初始数据"""
        if self.multi_app_mode:
            self._load_multi_app_data()
        else:
            self._load_single_app_data()

    def _load_single_app_data(self):
        """加载单应用数据"""
        # 如果找不到启动的log，取消注释，自行观察最初显示的activity
        print(self.tp.query(f"""
            select id, ts, prio, utid, depth, tag, msg from (
                select
                  id, ts, prio, utid, tag, msg,
                  CASE
                    WHEN prio <= 3 THEN 0
                    WHEN prio = 4 THEN 1
                    WHEN prio = 5 THEN 2
                    WHEN prio = 6 THEN 3
                    WHEN prio = 7 THEN 4
                    ELSE -1
                  END as depth
                from android_logs
                WHERE tag = 'ActivityTaskManager'
                    AND prio = 4
                    AND msg like 'Displayed%'
                order by ts
            )
        """).as_pandas_dataframe().to_dict())

        # 获取应用启动消息
        self.ams_msgs = self.tp.query(f"""
            select id, ts, prio, utid, depth, tag, msg from (
                select
                  id, ts, prio, utid, tag, msg,
                  CASE
                    WHEN prio <= 3 THEN 0
                    WHEN prio = 4 THEN 1
                    WHEN prio = 5 THEN 2
                    WHEN prio = 6 THEN 3
                    WHEN prio = 7 THEN 4
                    ELSE -1
                  END as depth
                from android_logs
                WHERE tag = 'ActivityTaskManager'
                    AND prio = 4
                    AND msg like 'Displayed {self.launch_activity}%'
                order by ts
            )
        """).as_pandas_dataframe()
        
        # 计算应用启动时间
        if not self.ams_msgs.empty:
            self.app_launch_end_ts = self.ams_msgs.iloc[0]['ts']
            start_latency = self.parse_time_to_ns(
                self.ams_msgs.iloc[0]['msg'].split(' ')[-1].replace('+', '')
            )
            self.app_launch_start_ts = self.app_launch_end_ts - start_latency
            print(f"应用启动时间: {self.app_launch_start_ts} -> {self.app_launch_end_ts}")
        else:
            print("警告: 未找到应用启动消息")
        
        # 解析操作步骤
        self.events = self.tp.query(
            "select * from slice where name like 'InputShell:%'"
        ).as_pandas_dataframe()
        print(f"找到 {len(self.events)} 个InputShell事件: {self.events['name'].tolist()}")
        
        # 日志文件是可选的（render_only模式不需要日志）
        if os.path.exists(self.record_path):
            self._parse_single_app_clicks()
            
            # 添加 "Memory path completed - prompting LLM to extract final result" 事件
            click_records = open(self.record_path, "r").readlines()
            self._add_final_query_event(click_records)

            # 计算任务完成时间
            self._parse_task_end_time(click_records)
        else:
            print(f"日志文件不存在 ({self.record_path})，跳过日志相关分析（render_only模式不需要）")
 
    def _load_multi_app_data(self):
        """加载多应用数据"""
        # 获取所有InputShell事件
        self.events = self.tp.query(
            "select * from slice where name like 'InputShell:%'"
        ).as_pandas_dataframe()
        
        if self.events.empty:
            print("警告: 未找到任何InputShell事件")
            return
        
        # 解析所有应用的点击记录并匹配到InputShell事件
        self._parse_multi_app_clicks()
        
        # 设置应用启动时间（使用最早的启动时间）
        self._set_multi_app_launch_times()

    def _parse_single_app_clicks(self):
        """解析单应用的点击记录"""
        click_records = open(self.record_path, "r").readlines()
        will_input_records_in_log = list(
            seq(click_records)
            .filter(lambda x: 'Will click' in x or 'Will input' in x)
        )

        input_text_events_count = 0
        for record in will_input_records_in_log:
            if 'Will input' in record:
                input_text_events_count += 1

        # will_input_records_in_log = will_input_records_in_log[:-1] # 去掉最后一个多余的记录，其并没有对应的输入
        
        # 保留wall time信息和button名称
        self.click_wall_times = []
        button_names = []

        # Dining 适配
        if self.pkg_name == 'com.smile.gifmaker':
            no_action_buttons = [
                '奉贤区奉浦街道沪杭公路1588号凤创谷内入口处50米'
            ]
            for no_action_name in no_action_buttons:
                will_input_records_in_log = [
                    record for record in will_input_records_in_log 
                    if no_action_name not in record and not record.strip().startswith(no_action_name)
                ]
        
        for i in range(len(will_input_records_in_log)):
            record = will_input_records_in_log[i]
            
            # 提取wall time
            time_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})', record)
            if time_match:
                time_str = time_match.group(1)
                try:
                    from datetime import datetime
                    wall_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S,%f')
                    self.click_wall_times.append(wall_time)
                except ValueError:
                    # 尝试处理毫秒格式
                    time_str_ms = time_str.replace(',', '.')
                    wall_time = datetime.strptime(time_str_ms, '%Y-%m-%d %H:%M:%S.%f')
                    self.click_wall_times.append(wall_time)
            else:
                self.click_wall_times.append(None)
            
            # 提取button名称
            stripped = record.split('>')[1].split('<')[0].strip()
            if len(stripped) > 0:
                button_names.append(stripped)
            else:
                extracted = re.search(r"text='(.*?)'", record)
                if extracted and extracted.group(1) and extracted.group(1).strip():
                    button_names.append(extracted.group(1).strip())
                else:
                    button_names.append('blank') # 占位符

            # Dining 适配
            if self.pkg_name == 'com.smile.gifmaker':
                no_action_buttons = [
                    '奉贤区奉浦街道沪杭公路1588号凤创谷内入口处50米'
                ]
                for no_action_name in no_action_buttons:
                    button_names = [
                        name for name in button_names if name != no_action_name and not name.startswith(no_action_name)
                    ]
        
        print(f"找到 {len(button_names)} 个操作记录: {button_names}")
        
        if input_text_events_count > 0 and False:
            print(f"日志中包含 {input_text_events_count} 个文本输入事件，这些事件各自对应两次 InputShell。")
            # 过滤一下 InputShell 事件
            if (len(button_names) == len(self.events) - input_text_events_count + 1 
                    or len(button_names) == len(self.events) - input_text_events_count):
                idx_in_events = 0
                filtered_events = []
                for record in will_input_records_in_log:
                    if idx_in_events == len(self.events):
                        # 最后一个可能没有对应 InputShell 事件的点击记录
                        break
                    if 'Will input' in record:
                        idx_in_events += 1  # 跳过一个 InputShell 事件
                    filtered_events.append(self.events.iloc[idx_in_events])
                    idx_in_events += 1
                self.events = pd.DataFrame(filtered_events).reset_index(drop=True)


        if len(button_names) == len(self.events) + 1:
            self.events['click_on'] = button_names[:-1]
            # 调整wall_times数组以匹配events
            self.events_wall_times = self.click_wall_times[:-1]
        elif len(button_names) == len(self.events):
            self.events['click_on'] = button_names
            self.events_wall_times = self.click_wall_times
        else:
            print(f"警告: 点击记录数量与事件数量不匹配! 预期 {len(self.events)}，实际 {len(button_names)}")
            if self.strict_click_matching:
                for i in range(len(button_names)):
                    print(f"点击记录 {i}: {button_names[i]}")
                for i in range(len(self.events)):
                    print(f"事件 {i}: {self.events.iloc[i]['name']} at {self.events.iloc[i]['ts']}")
                assert False, "点击记录数量与事件数量不匹配"
            else:
                # 宽松模式：使用事件序号作为click_on标记
                print("宽松模式：使用事件序号作为操作标记")
                self.events['click_on'] = [f"event_{i}" for i in range(len(self.events))]
                # 使用事件的ts作为相对时间（无wall time）
                self.events_wall_times = list(range(len(self.events)))

        
        # 只保留每个click_on第一次出现的event
        # 仅在相同click_on连续出现时才去重
        # self.first_events = self.events.drop_duplicates(
        #     subset=['click_on'], keep='first'
        # ).reset_index(drop=True)
        first_events_list = []
        last_click_on = None
        for idx, row in self.events.iterrows():
            if row['click_on'] != last_click_on:
                first_events_list.append(row)
                last_click_on = row['click_on']
        self.first_events = pd.DataFrame(first_events_list).reset_index(drop=True)

        print(f'过滤后的操作事件： {self.first_events[["ts", "name", "click_on"]].to_dict(orient="records")}')
        
        # 为first_events保留对应的wall times
        self.first_events_wall_times = []
        for idx, row in self.first_events.iterrows():
            original_idx = self.events[self.events['ts'] == row['ts']].index[0]
            self.first_events_wall_times.append(self.events_wall_times[original_idx])

    def _parse_multi_app_clicks(self):
        """解析多应用的点击记录并匹配到InputShell事件"""
        from datetime import datetime
        import re
        
        # 收集所有应用的Will Click记录
        all_will_click_records = []
        
        for app_name, config in self.app_configs.items():
            log_path = config['log_path']
            try:
                with open(log_path, 'r') as f:
                    click_records = f.readlines()
                
                # 找到所有Will Click记录
                for line_num, line in enumerate(click_records):
                    if 'Will click' in line:
                        # 解析时间戳
                        time_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})', line)
                        if time_match:
                            time_str = time_match.group(1)
                            try:
                                wall_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S,%f')
                            except ValueError:
                                # 处理毫秒格式
                                time_str_ms = time_str.replace(',', '.')
                                wall_time = datetime.strptime(time_str_ms, '%Y-%m-%d %H:%M:%S.%f')
                            
                            # 提取button名称
                            button_name = None
                            if '>' in line and '<' in line:
                                stripped = line.split('>')[1].split('<')[0].strip()
                                if len(stripped) > 0:
                                    button_name = stripped
                            
                            if not button_name:
                                extracted = re.search(r"text='(.*?)'", line)
                                if extracted and extracted.group(1) and extracted.group(1).strip():
                                    button_name = extracted.group(1).strip()
                            
                            if button_name:
                                # 查找对应的input touchscreen事件
                                input_time = self._find_input_touchscreen_time(click_records, line_num)
                                
                                all_will_click_records.append({
                                    'app_name': app_name,
                                    'wall_time': wall_time,
                                    'input_time': input_time,
                                    'button_name': button_name,
                                    'pkg_name': config['pkg_name']
                                })
                                
            except Exception as e:
                print(f"解析 {app_name} 日志时出错: {e}")
        
        # 按时间排序
        all_will_click_records.sort(key=lambda x: x['input_time'] if x['input_time'] else x['wall_time'])
        
        print(f"找到 {len(all_will_click_records)} 个Will Click记录")
        for record in all_will_click_records:
            print(f"  {record['app_name']}: {record['button_name']} @ {record['input_time'] or record['wall_time']}")
        
        # 匹配到InputShell事件
        self._match_clicks_to_input_events(all_will_click_records)

    def _find_input_touchscreen_time(self, click_records, will_click_line_num):
        """在Will Click记录后查找input touchscreen的时间"""
        from datetime import datetime
        import re
        
        # 在Will Click后面几行内查找input touchscreen
        for i in range(will_click_line_num + 1, min(will_click_line_num + 10, len(click_records))):
            line = click_records[i]
            if 'input touchscreen' in line:
                # 提取时间戳，支持两种格式：
                # 格式1: 2025-07-15:20:33:28,112 (日期和时间间用冒号分隔)
                # 格式2: 2025-07-15 20:33:28,112 (日期和时间间用空格分隔)
                time_match = re.match(r'^(\d{4}-\d{2}-\d{2}[: ]\d{2}:\d{2}:\d{2},\d{3})', line)
                if time_match:
                    time_str = time_match.group(1)
                    try:
                        # 处理格式1: 2025-07-15:20:33:28,112 -> 2025-07-15 20:33:28,112
                        if time_str[10] == ':':  # 第11个字符（索引10）是冒号
                            time_str = time_str[:10] + ' ' + time_str[11:]
                        return datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S,%f')
                    except ValueError:
                        try:
                            # 尝试毫秒格式
                            time_str_ms = time_str.replace(',', '.')
                            if time_str_ms[10] == ':':  # 第11个字符（索引10）是冒号
                                time_str_ms = time_str_ms[:10] + ' ' + time_str_ms[11:]
                            return datetime.strptime(time_str_ms, '%Y-%m-%d %H:%M:%S.%f')
                        except ValueError:
                            continue
        return None

    def _match_clicks_to_input_events(self, will_click_records):
        """将Will Click记录匹配到InputShell事件"""
        if self.events.empty:
            print("警告: 没有InputShell事件可匹配")
            return
        
        # 为了匹配，需要建立时间基准
        # 使用第一个有input_time的记录作为基准
        reference_click = None
        for record in will_click_records:
            if record['input_time']:
                reference_click = record
                break
        
        if not reference_click:
            print("警告: 没有找到有效的input touchscreen时间，无法建立时间基准")
            return
        
        # 使用第一个InputShell事件作为基准
        reference_event = self.events.iloc[0]
        reference_ts = reference_event['ts']
        reference_wall_time = reference_click['input_time']
        
        print(f"时间基准: input touchscreen @ {reference_wall_time} -> InputShell @ {reference_ts}")
        
        # 为每个InputShell事件匹配最接近的Will Click记录
        matched_events = []
        used_clicks = set()
        
        for idx, event in self.events.iterrows():
            event_ts = event['ts']
            
            # 计算事件对应的wall time
            time_diff_ns = event_ts - reference_ts
            event_wall_time = reference_wall_time + pd.Timedelta(nanoseconds=time_diff_ns)
            
            # 找到时间最接近的Will Click记录
            best_match = None
            min_time_diff = float('inf')
            
            for i, click_record in enumerate(will_click_records):
                if i in used_clicks:
                    continue
                
                click_time = click_record['input_time'] if click_record['input_time'] else click_record['wall_time']
                time_diff = abs((event_wall_time - click_time).total_seconds())
                
                # 只考虑10秒内的匹配
                if time_diff < 10 and time_diff < min_time_diff:
                    min_time_diff = time_diff
                    best_match = (i, click_record)
            
            if best_match:
                click_idx, click_record = best_match
                used_clicks.add(click_idx)
                
                matched_event = event.copy()
                matched_event['click_on'] = f"[{click_record['app_name']}] {click_record['button_name']}"
                matched_event['app_name'] = click_record['app_name']
                matched_event['wall_time'] = click_record['input_time'] if click_record['input_time'] else click_record['wall_time']
                matched_events.append(matched_event)
                
                print(f"匹配: InputShell@{event_ts} -> [{click_record['app_name']}] {click_record['button_name']} (时间差: {min_time_diff:.2f}s)")
        
        # 更新events
        if matched_events:
            self.events = pd.DataFrame(matched_events)
            self.events = self.events.sort_values('ts').reset_index(drop=True)
            
            # 生成first_events（去重）
            self.first_events = self.events.drop_duplicates(
                subset=['click_on'], keep='first'
            ).reset_index(drop=True)
            
            print(f"成功匹配 {len(matched_events)} 个InputShell事件到Will Click记录")
        else:
            print("警告: 没有成功匹配任何事件")
            # 创建空的first_events以避免后续错误
            self.first_events = pd.DataFrame(columns=['ts', 'dur', 'track_id', 'name', 'click_on'])

    def _set_multi_app_launch_times(self):
        """设置多应用的启动时间"""
        # 查找所有应用的启动消息
        all_ams_msgs = []
        
        for app_name, config in self.app_configs.items():
            pkg_name = config['pkg_name']
            launch_activity = config['launch_activity']
            
            ams_query = f"""
                select id, ts, prio, utid, depth, tag, msg, '{app_name}' as app_name from (
                    select
                      id, ts, prio, utid, tag, msg,
                      CASE
                        WHEN prio <= 3 THEN 0
                        WHEN prio = 4 THEN 1
                        WHEN prio = 5 THEN 2
                        WHEN prio = 6 THEN 3
                        WHEN prio = 7 THEN 4
                        ELSE -1
                      END as depth
                    from android_logs
                    WHERE tag = 'ActivityTaskManager'
                        AND prio = 4
                        AND msg like 'Displayed {pkg_name}/{launch_activity}%'
                    order by ts
                )
            """
            
            try:
                ams_df = self.tp.query(ams_query).as_pandas_dataframe()
                if not ams_df.empty:
                    all_ams_msgs.append(ams_df.iloc[0])
                    print(f"找到 {app_name} 启动消息: {ams_df.iloc[0]['ts']}")
            except Exception as e:
                print(f"查询 {app_name} 启动消息时出错: {e}")
        
        if all_ams_msgs:
            # 使用最早的启动时间
            earliest_msg = min(all_ams_msgs, key=lambda x: x['ts'])
            self.app_launch_end_ts = earliest_msg['ts']
            start_latency = self.parse_time_to_ns(
                earliest_msg['msg'].split(' ')[-1].replace('+', '')
            )
            self.app_launch_start_ts = self.app_launch_end_ts - start_latency
            print(f"多应用启动时间: {self.app_launch_start_ts} -> {self.app_launch_end_ts}")
        else:
            print("警告: 未找到任何应用启动消息")
        
    def _add_final_query_event(self, click_records):
        """添加 'Memory path completed - prompting LLM to extract final result' 事件"""
        from datetime import datetime
        import re
        
        # 查找记录文件中的最终查询行
        final_query_line = None
        final_query_time = None
        
        for line in click_records:
            if "Memory path completed - prompting LLM to extract final result" in line:
                final_query_line = line.strip()
                # 解析时间戳，格式如: 2025-07-15 10:48:09,195
                time_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})', line)
                if time_match:
                    time_str = time_match.group(1)
                    try:
                        final_query_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S,%f')
                    except ValueError:
                        # 尝试处理毫秒格式
                        time_str_ms = time_str.replace(',', '.')
                        final_query_time = datetime.strptime(time_str_ms, '%Y-%m-%d %H:%M:%S.%f')
                break
        
        if not final_query_line or not final_query_time:
            print("警告: 未找到 'Memory path completed - prompting LLM to extract final result' 记录")
            return
        
        # 检查是否有可用的参考点
        if not hasattr(self, 'first_events_wall_times') or len(self.first_events_wall_times) == 0:
            print("警告: 没有可用的时间参考点，无法计算最终查询事件的ts")
            return
        
        # 使用最后一个已知事件作为参考点
        reference_event = self.first_events.iloc[-1]
        reference_ts = reference_event['ts']
        reference_wall_time = self.first_events_wall_times[-1]

        for idx, event in self.first_events.iterrows():
            print(f"First Event {idx}: ts={event['ts']}, click_on={event['click_on']} wall_time={self.first_events_wall_times[idx]}")

        if reference_wall_time is None:
            print("警告: 参考事件的wall time为空，无法计算最终查询事件的ts")
            return
        
        # 计算时间差（以纳秒为单位）
        time_diff = (final_query_time - reference_wall_time).total_seconds() * 1_000_000_000
        final_query_ts = int(reference_ts + time_diff)
        
        print(f"计算最终查询事件时间戳:")
        print(f"参考事件: {reference_event['click_on']}")
        print(f"参考wall time: {reference_wall_time}, 参考ts: {reference_ts}")
        print(f"最终查询wall time: {final_query_time}, 计算的ts: {final_query_ts}")
        print(f"时间差: {time_diff / 1_000_000:.2f} ms")
        
        # 创建最终查询事件
        final_event = pd.DataFrame({
            'ts': [final_query_ts],
            'dur': [0],  # 假设持续时间为0
            'track_id': [0],  # 使用默认track_id
            'name': ['InputShell:Query Final Result'],
            'click_on': ['Query Final Result']
        })
        
        # 添加到events中
        self.events = pd.concat([self.events, final_event], ignore_index=True)
        self.events = self.events.sort_values('ts').reset_index(drop=True)
        
        # 更新events_wall_times
        if hasattr(self, 'events_wall_times'):
            self.events_wall_times.append(final_query_time)
        
        # 添加到first_events中（因为这是唯一的事件）
        self.first_events = pd.concat([self.first_events, final_event], ignore_index=True)
        self.first_events = self.first_events.sort_values('ts').reset_index(drop=True)
        
        # 更新first_events_wall_times
        self.first_events_wall_times.append(final_query_time)
        
        print(f"成功添加最终查询事件: ts={final_query_ts}, 描述='Query Final Result'")

        print(f'最终操作事件： {self.first_events[["ts", "name", "click_on"]].to_dict(orient="records")}')

    def _parse_task_end_time(self, click_records):
        """解析任务完成时间"""
        from datetime import datetime
        import re
        
        task_end_time = None

        for line in click_records:
            if "Policy TaskPolicy finished" in line:
                # 解析时间戳，格式如: 2025-07-15 10:48:09,195
                time_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})', line)
                if time_match:
                    time_str = time_match.group(1)
                    try:
                        task_end_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S,%f')
                    except ValueError:
                        # 尝试处理毫秒格式
                        time_str_ms = time_str.replace(',', '.')
                        task_end_time = datetime.strptime(time_str_ms, '%Y-%m-%d %H:%M:%S.%f')
                break
        
        if not task_end_time:
            print("警告: 未找到 'Policy TaskPolicy finished' 记录")
            return
        
        # 检查是否有可用的参考点
        if not hasattr(self, 'first_events_wall_times') or len(self.first_events_wall_times) == 0:
            print("警告: 没有可用的时间参考点，无法计算任务完成时间ts")
            return
        
        # 使用最后一个已知事件作为参考点
        reference_event = self.first_events.iloc[-1]
        reference_ts = reference_event['ts']
        reference_wall_time = self.first_events_wall_times[-1]
        
        if reference_wall_time is None:
            print("警告: 参考事件的wall time为空，无法计算最终查询事件的ts")
            return
        
        # 检查 reference_wall_time 是否是 datetime 对象（宽松模式下可能是整数）
        if not isinstance(reference_wall_time, datetime):
            print("警告: 宽松模式下无法计算任务完成时间，跳过")
            return
        
        # 计算时间差（以纳秒为单位）
        time_diff = (task_end_time - reference_wall_time).total_seconds() * 1_000_000_000
        task_end_ts = int(reference_ts + time_diff)
        
        print(f"计算任务完成时间戳:")
        print(f"参考事件: {reference_event['click_on']}")
        print(f"参考wall time: {reference_wall_time}, 参考ts: {reference_ts}")
        print(f"任务完成wall time: {task_end_time}, 计算的ts: {task_end_ts}")
        print(f"时间差: {time_diff / 1_000_000:.2f} ms")
        
        # 保存下来即可
        self.task_end_ts = task_end_ts

    def analyze_time_period(self, start_ts, end_ts, description=""):
        """
        分析指定时间段的调度切片数据
        
        Args:
            start_ts: 开始时间戳
            end_ts: 结束时间戳
            description: 时间段描述（可选）
        
        Returns:
            tuple: (
                cpu_usage_summary,
                cpu_usage_timeline,
                cpu_time_summary,
                cpu_time_timeline,
                mem_usage_summary,
                mem_usage_timeline,
                io_usage_summary,
                io_usage_timeline,
                looper_usage,
                cpu_idle_usage,
            )
        """
        print(f"\n>>>>=== 分析时间段 {description} ===<<<<")
        print(f"时间范围: {start_ts} -> {end_ts}")
        print(f"持续时间: {(end_ts - start_ts) / 1_000_000:.2f} ms")
        
        # render_only 模式：只分析 CPU 时间和渲染线程
        if self.render_only:
            # 分析APP的CPU时间使用情况
            cpu_time_summary, cpu_time_timeline = self._analyze_cpu_time(start_ts, end_ts)
            # 分析渲染线程占用情况
            cpu_idle_usage = self._analyze_cpu_idle_and_app_threads(start_ts, end_ts)
            return (
                None,  # cpu_usage_summary
                None,  # cpu_usage_timeline
                cpu_time_summary,
                cpu_time_timeline,
                None,  # mem_usage_summary
                None,  # mem_usage_timeline
                None,  # io_usage_summary
                None,  # io_usage_timeline
                None,  # looper_usage
                cpu_idle_usage,
            )
        
        # 全量分析模式
        # 分析CPU占用率情况
        cpu_usage_summary, cpu_usage_timeline = self._analyze_cpu_usage(start_ts, end_ts)

        # 分析CPU时间使用情况
        cpu_time_summary, cpu_time_timeline = self._analyze_cpu_time(start_ts, end_ts)

        # 分析内存使用情况
        mem_usage_summary, mem_usage_timeline = self._analyze_memory(start_ts, end_ts)

        # 分析IO使用情况
        io_usage_summary, io_usage_timeline = self._analyze_io(start_ts, end_ts)
        
        # 分析Looper使用情况
        looper_usage = self._analyze_looper(start_ts, end_ts)
        
        # 分析CPU idle和应用线程占用情况
        cpu_idle_usage = self._analyze_cpu_idle_and_app_threads(start_ts, end_ts)

        return (
            cpu_usage_summary,
            cpu_usage_timeline,
            cpu_time_summary,
            cpu_time_timeline,
            mem_usage_summary,
            mem_usage_timeline,
            io_usage_summary,
            io_usage_timeline,
            looper_usage,
            cpu_idle_usage,
        )

    def _analyze_cpu_usage(self, start_ts, end_ts):
        if end_ts <= start_ts:
            return None, pd.DataFrame(columns=['core_id', 'seg_start', 'seg_end', 'seg_duraion_ns', 'usage'])

        segment_ns = int(1e9)
        duration = end_ts - start_ts
        if duration % segment_ns <=  segment_ns * 0.25:
            duration -= duration % segment_ns
        total_segments = int((duration + segment_ns - 1) // segment_ns)
        if total_segments <= 0:
            return None, pd.DataFrame(columns=['core_id', 'seg_start', 'seg_end', 'seg_duraion_ns', 'usage'])

        query_end_ts = start_ts + total_segments * segment_ns

        gcounters = self.tp.query("""
        SELECT id, name FROM counter_track
        WHERE (type = 'cpu_counter_track' or type = 'cpustat') AND name NOT IN ('cpuidle', 'cpufreq')
        ORDER BY id
        """).as_pandas_dataframe()

        num_cores = 8  # 8 cores
        metrics_per_core = len(gcounters) // 8  # Each core has 8 metrics

        cpu_metric_deltas: Dict[int, Dict[int, Dict[str, float]]] = {}

        min_ts = start_ts

        for cpu_core in range(num_cores):
            cpu_metric_deltas[cpu_core] = {}
            for idx in range(metrics_per_core):
                try:
                    row = gcounters.iloc[cpu_core * metrics_per_core + idx]
                    metric_name = row['name']
                    track_id = row['id']

                    data = self.tp.query(f"""
                        SELECT ts, value FROM counter 
                        WHERE track_id = {track_id} 
                        AND ts >= {min_ts} AND ts < {query_end_ts}
                        ORDER BY ts
                    """).as_pandas_dataframe()

                    if data.empty:
                        continue

                    data['ts'] = pd.to_numeric(data['ts'], downcast='integer')
                    data['ts_seg'] = ((data['ts'] - min_ts) // segment_ns).astype('int64')

                    for ts_seg, sub_df in data.groupby('ts_seg', as_index=False):
                        if ts_seg < 0 or ts_seg >= total_segments:
                            continue
                        sub_df = sub_df.sort_values(by='ts')
                        delta = sub_df.iloc[-1]['value'] - sub_df.iloc[0]['value']
                        if cpu_metric_deltas[cpu_core].get(ts_seg) is None:
                            cpu_metric_deltas[cpu_core][ts_seg] = {}
                        cpu_metric_deltas[cpu_core][ts_seg][metric_name] = delta

                except Exception as e:
                    print(f"Error extracting CPU {cpu_core}, metric {idx}: {e}")

        records = []
        for core in range(num_cores):
            if self.bind_cores and core not in self.bind_cores:
                continue
            core_metric_deltas = cpu_metric_deltas.get(core, {})
            for seg_idx in range(total_segments):
                metric_delta = core_metric_deltas.get(seg_idx, {})
                idle_ns = metric_delta.get('cpu.times.idle_ns', 0)
                user_ns = metric_delta.get('cpu.times.user_ns', 0)
                user_nice_ns = metric_delta.get('cpu.times.user_nice_ns', 0)
                system_ns = metric_delta.get('cpu.times.system_mode_ns', 0)
                io_wait_ns = metric_delta.get('cpu.times.io_wait_ns', 0)
                irq_ns = metric_delta.get('cpu.times.irq_ns', 0)
                softirq_ns = metric_delta.get('cpu.times.softirq_ns', 0)
                steal_ns = metric_delta.get('cpu.times.steal_ns', 0)

                total_ns = idle_ns + user_ns + user_nice_ns + system_ns + io_wait_ns + irq_ns + softirq_ns + steal_ns

                usage = 0
                if total_ns > 0:
                    usage = max(0, min(1, 1 - (idle_ns / total_ns)))

                seg_start = start_ts + seg_idx * segment_ns
                seg_end = min(seg_start + segment_ns, end_ts)

                records.append({
                    'core_id': core,
                    'seg_start': seg_start,
                    'seg_end': seg_end,
                    'seg_duration_ns': seg_end - seg_start,
                    'usage': usage,
                })

        cpu_usage_timeline = pd.DataFrame(records)

        summary = cpu_usage_timeline['usage'].mean()
        total_duration_ns = cpu_usage_timeline['seg_duration_ns'].sum()
        if total_duration_ns > 0:
            summary = (cpu_usage_timeline['usage'] * cpu_usage_timeline['seg_duration_ns']).sum() / total_duration_ns

        return summary, cpu_usage_timeline

    def _analyze_cpu_time(self, start_ts, end_ts):
        """分析APP线程的CPU时间使用情况"""
        if not self.pkg_name:
            print("警告: 未指定pkg_name，无法分析CPU时间")
            return None, pd.DataFrame(columns=['seg_start', 'seg_end', 'seg_duraion_ns', 'cpu_time_ns', 'cpu_time_ms'])

        if end_ts <= start_ts:
            return None, pd.DataFrame(columns=['seg_start', 'seg_end', 'seg_duraion_ns', 'cpu_time_ns', 'cpu_time_ms'])

        segment_ns = int(1e9)
        duration = end_ts - start_ts
        if duration % segment_ns <=  segment_ns * 0.25:
            duration -= duration % segment_ns
        pkg_name = self.pkg_name.replace("'", "''")
        process_filter = (
            f"(pr.name = '{pkg_name}' OR pr.name LIKE '{pkg_name}:%')"
        )

        query = f"""
        SELECT
            s.ts,
            s.dur,
            s.cpu
        FROM sched AS s
        JOIN thread AS th ON s.utid = th.utid
        JOIN process AS pr ON th.upid = pr.upid
        WHERE {process_filter}
          AND s.ts < {end_ts}
          AND (s.ts + s.dur) > {start_ts}
          AND s.dur != -1
          {"" if self.bind_cores is None else "AND s.cpu IN (" + ",".join(map(str, self.bind_cores)) + ")"}
        ORDER BY s.ts
        """

        try:
            df = self.tp.query(query).as_pandas_dataframe()
        except Exception as exc:
            print(f"CPU时间分析查询失败: {exc}")
            return None, pd.DataFrame(columns=['seg_start', 'seg_end', 'seg_duraion_ns', 'cpu_time_ns', 'cpu_time_ms'])

        if df.empty:
            total_segments = int((duration + segment_ns - 1) // segment_ns)
            seg_starts = [start_ts + i * segment_ns for i in range(total_segments)]
            seg_ends = [min(start_ts + (i + 1) * segment_ns, end_ts) for i in range(total_segments)]
            return None, pd.DataFrame({
                'seg_start': seg_starts,
                'seg_end': seg_ends,
                'seg_duration_ns': [seg_ends[i] - seg_starts[i] for i in range(total_segments)],
                'cpu_time_ns': [0] * total_segments,
                'cpu_time_ms': [0.0] * total_segments,
                'cpu_time_percent': [0.0] * total_segments,
            })

        df['ts'] = pd.to_numeric(df['ts'], downcast='integer')
        df['dur'] = pd.to_numeric(df['dur'], downcast='integer')

        segment_records: List[Dict[str, int]] = []

        for _, row in df.iterrows():
            slice_start = int(max(row['ts'], start_ts))
            slice_end = int(min(row['ts'] + row['dur'], end_ts))

            if slice_end <= slice_start:
                continue

            start_segment = (slice_start - start_ts) // segment_ns
            end_segment = (slice_end - 1 - start_ts) // segment_ns

            for seg_idx in range(int(start_segment), int(end_segment) + 1):
                seg_start = start_ts + seg_idx * segment_ns
                seg_end = min(seg_start + segment_ns, end_ts)
                overlap_start = max(slice_start, seg_start)
                overlap_end = min(slice_end, seg_end)

                if overlap_end <= overlap_start:
                    continue

                segment_records.append({
                    'seg_index': seg_idx,
                    'overlap_ns': overlap_end - overlap_start,
                })

        total_segments = int(((end_ts - start_ts) + segment_ns - 1) // segment_ns)
        seg_starts = [start_ts + i * segment_ns for i in range(total_segments)]
        seg_ends = [min(start_ts + (i + 1) * segment_ns, end_ts) for i in range(total_segments)]

        if segment_records:
            segment_df = pd.DataFrame(segment_records)
            aggregated = (
                segment_df.groupby('seg_index', as_index=False)['overlap_ns']
                .sum()
                .rename(columns={'overlap_ns': 'cpu_time_ns'})
            )
        else:
            aggregated = pd.DataFrame(columns=['seg_index', 'cpu_time_ns'])

        timeline = pd.DataFrame({
            'seg_index': range(total_segments),
            'seg_start': seg_starts,
            'seg_end': seg_ends,
            'seg_duration_ns': [seg_ends[i] - seg_starts[i] for i in range(total_segments)],
        })

        timeline = timeline.merge(aggregated, on='seg_index', how='left')
        timeline['cpu_time_ns'] = timeline['cpu_time_ns'].fillna(0).astype('int64')
        timeline['cpu_time_ms'] = timeline['cpu_time_ns'] / 1e6
        timeline['cpu_time_percent'] = timeline['cpu_time_ns'] / (timeline['seg_duration_ns'] * len(self.bind_cores))

        summary = {
            'cpu_time_ms': timeline['cpu_time_ms'].sum(),
            'cpu_time_percent': timeline['cpu_time_ns'].sum() / ((end_ts - start_ts) * len(self.bind_cores) )
        }

        return summary, timeline[['seg_start', 'seg_end', 'seg_duration_ns', 'cpu_time_ms', 'cpu_time_percent']]

    def _analyze_memory(self, start_ts, end_ts):
        """分析内存使用情况，返回整体摘要与逐秒时间线"""
        segment_ns = int(1e9)
        duration_ns = max(0, end_ts - start_ts)
        if duration_ns % segment_ns <= segment_ns * 0.25:
            duration_ns -= duration_ns % segment_ns
        total_segments = int((duration_ns + segment_ns - 1) // segment_ns)

        vmstat_metrics = [
            'nr_free_pages',
            'nr_zone_inactive_anon',
            'nr_zone_active_anon',
            'nr_zone_inactive_file',
            'nr_zone_active_file',
            'nr_zone_unevictable',
            'nr_anon_pages',
            'nr_mapped',
            'nr_file_pages',
            'nr_dirty',
            'nr_dirty_background_threshold',
            'nr_dirty_threshold',
            'nr_writeback',
            'nr_dirtied',
            'nr_written',
            'pswpin',
            'pswpout',
            'pgpgin',
            'pgpgout',
            'pgpgoutclean',
            'pgsteal_direct',
            'pgsteal_kswapd',
            'nr_vmscan_write',
            'nr_vmscan_immediate_reclaim',
            'workingset_refault',
            'workingset_activate',
        ]

        def _init_summary():
            summary = {
                'minor_fault': {'start': 0, 'end': 0, 'delta': 0, 'data_points': 0},
                'major_fault': {'start': 0, 'end': 0, 'delta': 0, 'data_points': 0},
                'vmstat': {},
                'rss': {},
            }
            for metric in vmstat_metrics:
                summary['vmstat'][metric] = {'start': 0, 'end': 0, 'delta': 0, 'data_points': 0}
            return summary

        def _empty_timeline():
            timeline_dict: Dict[str, List[Any]] = {
                'seg_start': [],
                'seg_end': [],
                'seg_duration_ns': [],
                'minor_fault_start': [],
                'minor_fault_end': [],
                'minor_fault_delta': [],
                'minor_fault_points': [],
                'major_fault_start': [],
                'major_fault_end': [],
                'major_fault_delta': [],
                'major_fault_points': [],
            }
            for metric in vmstat_metrics:
                timeline_dict[f'{metric}_start'] = []
                timeline_dict[f'{metric}_end'] = []
                timeline_dict[f'{metric}_delta'] = []
                timeline_dict[f'{metric}_points'] = []
            return pd.DataFrame(timeline_dict)

        summary = _init_summary()

        if total_segments == 0:
            return summary, _empty_timeline()

        seg_indices = list(range(total_segments))
        seg_starts = [start_ts + i * segment_ns for i in seg_indices]
        seg_ends = [min(start_ts + (i + 1) * segment_ns, end_ts) for i in seg_indices]

        timeline_dict: Dict[str, List[Any]] = {
            'seg_start': seg_starts,
            'seg_end': seg_ends,
            'seg_duration_ns': [seg_ends[i] - seg_starts[i] for i in range(total_segments)],
            'minor_fault_start': [0] * total_segments,
            'minor_fault_end': [0] * total_segments,
            'minor_fault_delta': [0.0] * total_segments,
            'minor_fault_points': [0] * total_segments,
            'major_fault_start': [0] * total_segments,
            'major_fault_end': [0] * total_segments,
            'major_fault_delta': [0.0] * total_segments,
            'major_fault_points': [0] * total_segments,
        }
        for metric in vmstat_metrics:
            timeline_dict[f'{metric}_start'] = [0] * total_segments
            timeline_dict[f'{metric}_end'] = [0] * total_segments
            timeline_dict[f'{metric}_delta'] = [0.0] * total_segments
            timeline_dict[f'{metric}_points'] = [0] * total_segments

        def _segment_stats(df: pd.DataFrame, value_column: str) -> Dict[int, Dict[str, Any]]:
            if df.empty:
                return {}
            local_df = df.copy()
            local_df['ts'] = pd.to_numeric(local_df['ts'], downcast='integer')
            local_df['ts_seg'] = ((local_df['ts'] - start_ts) // segment_ns).astype('int64')
            local_df = local_df[(local_df['ts_seg'] >= 0) & (local_df['ts_seg'] < total_segments)]
            stats: Dict[int, Dict[str, Any]] = {}
            for seg_idx, group in local_df.groupby('ts_seg'):
                ordered = group.sort_values('ts')
                if len(ordered) == 0:
                    continue
                start_val = float(ordered.iloc[0][value_column])
                end_val = float(ordered.iloc[-1][value_column])
                stats[int(seg_idx)] = {
                    'start': start_val,
                    'end': end_val,
                    'delta': float(end_val - start_val),
                    'points': int(len(ordered)),
                }
            return stats

        rss_packages: List[str] = []
        if self.multi_app_mode and hasattr(self, 'app_configs'):
            rss_packages = [cfg.get('pkg_name') for cfg in self.app_configs.values() if cfg.get('pkg_name')]
        elif self.pkg_name:
            rss_packages = [self.pkg_name]

        rss_packages = sorted({pkg for pkg in rss_packages if pkg})

        if rss_packages:
            clauses: List[str] = []
            for pkg in rss_packages:
                lowered = pkg.lower()
                escaped = lowered.replace("'", "''")
                clauses.append(f"LOWER(p.name) = '{escaped}'")
                clauses.append(f"LOWER(p.name) LIKE '{escaped}:%'")
            app_filter = f"AND ({' OR '.join(sorted(set(clauses)))})"

            rss_query = f"""
            SELECT
              c.ts AS ts,
              c.value AS value,
              COALESCE(ct.unit, '') AS unit,
              COALESCE(pct.name, '') AS counter_name,
              COALESCE(p.name, 'unknown') AS process_name,
              p.pid AS pid
            FROM counter c
            JOIN counter_track ct ON c.track_id = ct.id
            JOIN process_counter_track pct ON c.track_id = pct.id
            LEFT JOIN process p ON pct.upid = p.upid
            WHERE pct.name LIKE 'mem.rss%'
              AND c.ts >= {start_ts} AND c.ts <= {end_ts}
              {app_filter}
            ORDER BY c.ts
            """

            try:
                rss_df = self.tp.query(rss_query).as_pandas_dataframe()
            except Exception:
                rss_df = pd.DataFrame()

            if not rss_df.empty:
                rss_df['counter_name'] = rss_df['counter_name'].fillna('')
                rss_df['component'] = (
                  rss_df['counter_name']
                  .str.lower()
                  .str.replace('mem.rss.', '', regex=False)
                  .str.replace('mem.rss', '', regex=False)
                  .str.strip('._')
                )
                rss_df['component'] = rss_df['component'].replace({'': 'rss'})

                key_cols = ['process_name', 'pid', 'ts']
                base_keys = rss_df.loc[rss_df['component'] == 'rss', key_cols].drop_duplicates()
                if not base_keys.empty:
                    rss_df = rss_df.merge(base_keys.assign(_keep=True), on=key_cols, how='inner')
                    rss_df = rss_df.drop(columns=['_keep'])

                    def _value_to_bytes(unit: str, value: float) -> float:
                        unit = (unit or '').lower()
                        try:
                            numeric_value = float(value)
                        except (TypeError, ValueError):
                            return 0.0
                        if unit in {'pages', 'page'}:
                            return numeric_value * 4096
                        if unit in {'kb', 'kilobytes', 'kilobyte'}:
                            return numeric_value * 1024
                        if unit in {'mb', 'megabytes', 'megabyte'}:
                            return numeric_value * 1024 ** 2
                        if unit in {'gb', 'gigabytes', 'gigabyte'}:
                            return numeric_value * 1024 ** 3
                        return numeric_value

                    rss_df['rss_bytes'] = rss_df.apply(lambda row: _value_to_bytes(row['unit'], row['value']), axis=1)
                    rss_df['rss_mb'] = rss_df['rss_bytes'] / (1024 ** 2)
                    rss_df['process_group'] = rss_df['process_name'].str.split(':', n=1).str[0]

                    aggregated_rss = (
                        rss_df.groupby(['process_group', 'ts', 'component'], as_index=False)['rss_bytes']
                        .sum()
                    )
                    aggregated_rss['rss_mb'] = aggregated_rss['rss_bytes'] / (1024 ** 2)
                    aggregated_rss['ts'] = pd.to_numeric(aggregated_rss['ts'], downcast='integer')

                    mb_factor = float(1024 ** 2)

                    def _sanitize(name: str) -> str:
                        return re.sub(r'[^0-9a-zA-Z_]', '_', name.lower())

                    for proc_name, proc_df in aggregated_rss.groupby('process_group'):
                        proc_summary: Dict[str, Any] = {}
                        total_df = proc_df[proc_df['component'] == 'rss'].sort_values('ts')
                        if not total_df.empty:
                            start_bytes = float(total_df.iloc[0]['rss_bytes'])
                            end_bytes = float(total_df.iloc[-1]['rss_bytes'])
                            proc_summary['total'] = {
                                'start_bytes': start_bytes,
                                'end_bytes': end_bytes,
                                'delta_bytes': end_bytes - start_bytes,
                                'min_bytes': float(total_df['rss_bytes'].min()),
                                'max_bytes': float(total_df['rss_bytes'].max()),
                                'start_mb': start_bytes / mb_factor,
                                'end_mb': end_bytes / mb_factor,
                                'delta_mb': (end_bytes - start_bytes) / mb_factor,
                                'min_mb': float(total_df['rss_mb'].min()),
                                'max_mb': float(total_df['rss_mb'].max()),
                                'data_points': int(len(total_df)),
                            }
                        else:
                            proc_summary['total'] = None

                        component_summary: Dict[str, Any] = {}
                        for comp_name, comp_df in proc_df[proc_df['component'] != 'rss'].groupby('component'):
                            ordered = comp_df.sort_values('ts')
                            start_bytes = float(ordered.iloc[0]['rss_bytes'])
                            end_bytes = float(ordered.iloc[-1]['rss_bytes'])
                            component_summary[comp_name] = {
                                'start_bytes': start_bytes,
                                'end_bytes': end_bytes,
                                'delta_bytes': end_bytes - start_bytes,
                                'min_bytes': float(ordered['rss_bytes'].min()),
                                'max_bytes': float(ordered['rss_bytes'].max()),
                                'start_mb': start_bytes / mb_factor,
                                'end_mb': end_bytes / mb_factor,
                                'delta_mb': (end_bytes - start_bytes) / mb_factor,
                                'min_mb': float(ordered['rss_mb'].min()),
                                'max_mb': float(ordered['rss_mb'].max()),
                                'data_points': int(len(ordered)),
                            }
                        proc_summary['components'] = component_summary
                        summary['rss'][proc_name] = proc_summary

                        for comp_name, comp_df in proc_df.groupby('component'):
                            component_label = 'total' if comp_name == 'rss' or not comp_name else comp_name
                            prefix = f"rss_{_sanitize(component_label)}"
                            if f'{prefix}_start_mb' not in timeline_dict:
                                timeline_dict[f'{prefix}_start_mb'] = [0.0] * total_segments
                                timeline_dict[f'{prefix}_end_mb'] = [0.0] * total_segments
                                timeline_dict[f'{prefix}_delta_mb'] = [0.0] * total_segments
                                timeline_dict[f'{prefix}_points'] = [0] * total_segments

                            seg_df = comp_df[['ts', 'rss_mb']].rename(columns={'rss_mb': 'value'})
                            stats = _segment_stats(seg_df, 'value')
                            for seg_idx, seg_stats in stats.items():
                                timeline_dict[f'{prefix}_start_mb'][seg_idx] = seg_stats['start']
                                timeline_dict[f'{prefix}_end_mb'][seg_idx] = seg_stats['end']
                                timeline_dict[f'{prefix}_delta_mb'][seg_idx] = seg_stats['delta']
                                timeline_dict[f'{prefix}_points'][seg_idx] = seg_stats['points']
                else:
                    print("警告: 在选定的时间范围内未找到 mem.rss 基准数据，跳过RSS统计")
            if len(summary['rss']) == 1:
                summary['rss'] = summary['rss'][rss_packages[0]]

        # minor fault timeline
        minor_fault_query = f"""
        SELECT c.ts AS ts, c.value AS min_flt_count
        FROM counters AS c
        LEFT JOIN counter_track AS t ON c.track_id = t.id
        WHERE t.name = 'pgfault'
              AND c.ts >= {start_ts} AND c.ts <= {end_ts}
        ORDER BY c.ts
        """
        minor_fault_df = self.tp.query(minor_fault_query).as_pandas_dataframe()
        if not minor_fault_df.empty:
            minor_fault_df.sort_values('ts', inplace=True)
            start_val = float(minor_fault_df.iloc[0]['min_flt_count'])
            end_val = float(minor_fault_df.iloc[-1]['min_flt_count'])
            summary['minor_fault'] = {
                'start': start_val,
                'end': end_val,
                'delta': end_val - start_val,
                'data_points': int(len(minor_fault_df)),
            }
            minor_stats = _segment_stats(minor_fault_df, 'min_flt_count')
            for seg_idx, stats in minor_stats.items():
                timeline_dict['minor_fault_start'][seg_idx] = stats['start']
                timeline_dict['minor_fault_end'][seg_idx] = stats['end']
                timeline_dict['minor_fault_delta'][seg_idx] = stats['delta']
                timeline_dict['minor_fault_points'][seg_idx] = stats['points']

        # major fault timeline
        major_fault_query = f"""
        SELECT c.ts AS ts, c.value AS maj_flt_count
        FROM counters AS c
        LEFT JOIN counter_track AS t ON c.track_id = t.id
        WHERE t.name = 'pgmajfault'
              AND c.ts >= {start_ts} AND c.ts <= {end_ts}
        ORDER BY c.ts
        """
        major_fault_df = self.tp.query(major_fault_query).as_pandas_dataframe()
        if not major_fault_df.empty:
            major_fault_df.sort_values('ts', inplace=True)
            start_val = float(major_fault_df.iloc[0]['maj_flt_count'])
            end_val = float(major_fault_df.iloc[-1]['maj_flt_count'])
            summary['major_fault'] = {
                'start': start_val,
                'end': end_val,
                'delta': end_val - start_val,
                'data_points': int(len(major_fault_df)),
            }
            major_stats = _segment_stats(major_fault_df, 'maj_flt_count')
            for seg_idx, stats in major_stats.items():
                timeline_dict['major_fault_start'][seg_idx] = stats['start']
                timeline_dict['major_fault_end'][seg_idx] = stats['end']
                timeline_dict['major_fault_delta'][seg_idx] = stats['delta']
                timeline_dict['major_fault_points'][seg_idx] = stats['points']

        # vmstat metrics timeline
        for metric in vmstat_metrics:
            vmstat_query = f"""
            SELECT c.ts AS ts, c.value AS value
            FROM counters AS c
            LEFT JOIN counter_track AS t ON c.track_id = t.id
            WHERE t.name = '{metric}' AND t.type = 'vmstat'
                  AND c.ts >= {start_ts} AND c.ts <= {end_ts}
            ORDER BY c.ts
            """
            try:
                df = self.tp.query(vmstat_query).as_pandas_dataframe()
                if not df.empty:
                    df.sort_values('ts', inplace=True)
                    start_val = float(df.iloc[0]['value'])
                    end_val = float(df.iloc[-1]['value'])
                    summary['vmstat'][metric] = {
                        'start': start_val,
                        'end': end_val,
                        'delta': end_val - start_val,
                        'data_points': int(len(df)),
                    }
                    stats = _segment_stats(df, 'value')
                    for seg_idx, seg_stats in stats.items():
                        timeline_dict[f'{metric}_start'][seg_idx] = seg_stats['start']
                        timeline_dict[f'{metric}_end'][seg_idx] = seg_stats['end']
                        timeline_dict[f'{metric}_delta'][seg_idx] = seg_stats['delta']
                        timeline_dict[f'{metric}_points'][seg_idx] = seg_stats['points']
                else:
                    summary['vmstat'][metric] = {
                        'start': 0,
                        'end': 0,
                        'delta': 0,
                        'data_points': 0,
                    }
            except Exception:
                summary['vmstat'][metric] = {
                    'start': 0,
                    'end': 0,
                    'delta': 0,
                    'data_points': 0,
                }

        timeline_df = pd.DataFrame(timeline_dict)
        return summary, timeline_df

    def _analyze_io(self, start_ts, end_ts):
        """分析IO使用情况，返回整体摘要与逐秒时间线"""
        segment_ns = int(1e9)
        duration_ns = max(0, end_ts - start_ts)
        if duration_ns % segment_ns <= segment_ns * 0.25:
            duration_ns -= duration_ns % segment_ns
        total_segments = int((duration_ns + segment_ns - 1) // segment_ns) if duration_ns > 0 else 0

        summary = {
            "block_read_bytes": 0,
            "block_write_bytes": 0,
            "block_read_count": 0,
            "block_write_count": 0,
            "block_read_mb": 0.0,
            "block_write_mb": 0.0,
            # "avg_latency_ms": 0.0,
            "total_requests": 0
        }

        if total_segments == 0:
            timeline_columns = [
                "seg_index",
                "seg_start",
                "seg_end",
                "seg_duration_ns",
                "block_read_bytes",
                "block_write_bytes",
                "block_read_count",
                "block_write_count",
                "total_requests",
                "block_read_mb",
                "block_write_mb",
            ]
            return summary, pd.DataFrame(columns=timeline_columns)

        seg_indices = list(range(total_segments))
        seg_starts = [start_ts + i * segment_ns for i in seg_indices]
        seg_ends = [min(start_ts + (i + 1) * segment_ns, end_ts) for i in seg_indices]

        timeline_dict: Dict[str, List[Any]] = {
            "seg_start": seg_starts,
            "seg_end": seg_ends,
            "seg_duration_ns": [seg_ends[i] - seg_starts[i] for i in range(total_segments)],
            "block_read_bytes": [0] * total_segments,
            "block_write_bytes": [0] * total_segments,
            "block_read_count": [0] * total_segments,
            "block_write_count": [0] * total_segments,
            "total_requests": [0] * total_segments,
            "block_read_mb": [0.0] * total_segments,
            "block_write_mb": [0.0] * total_segments,
        }

        block_io_query = f"""
        SELECT ftrace_event.id as id,
               ftrace_event.ts as ts,
               ftrace_event.name as name,
               to_ftrace(ftrace_event.id) as args
        FROM ftrace_event
        WHERE ftrace_event.name LIKE 'block%'
              AND ftrace_event.ts >= {start_ts} AND ftrace_event.ts <= {end_ts}
        ORDER BY ftrace_event.ts
        """

        try:
            block_df = self.tp.query(block_io_query).as_pandas_dataframe().reset_index(drop=True)

            if not block_df.empty:
                def parse_block_args(args_str):
                    pattern = r'.*: dev=(\d+) sector=(\d+) nr_sector=(\d+) (?:bytes=(\d+) )?rwbs=(\S+)'
                    match = re.match(pattern, args_str)
                    if match:
                        dev, sector, nr_sector, bytes_val, rwbs = match.groups()
                        return dev or 0, sector or 0, nr_sector or 0, bytes_val or 0, rwbs or None
                    return None, None, None, None, None

                block_df[["dev", "sector", "nr_sector", "bytes_val", "rwbs"]] = block_df["args"].apply(
                    lambda x: pd.Series(parse_block_args(x))
                )

                valid_block_df = block_df.dropna(subset=["dev", "sector", "nr_sector", "rwbs"])

                if not valid_block_df.empty:
                    issue_events = valid_block_df[valid_block_df["name"] == "block_rq_issue"].copy()

                    if not issue_events.empty:
                        issue_events["ts"] = pd.to_numeric(issue_events["ts"], errors="coerce")
                        issue_events.dropna(subset=["ts"], inplace=True)

                        def _compute_bytes(row):
                            try:
                                if row["bytes_val"] not in (None, ""):
                                    return int(row["bytes_val"])
                                if row["nr_sector"] not in (None, ""):
                                    return int(row["nr_sector"]) * 512
                            except (ValueError, TypeError):
                                return 0
                            return 0

                        issue_events["bytes"] = issue_events.apply(_compute_bytes, axis=1).astype("int64")
                        issue_events["rwbs"] = issue_events["rwbs"].astype(str)
                        issue_events["seg_index"] = ((issue_events["ts"].astype("int64") - start_ts) // segment_ns).astype("int64")

                        issue_events = issue_events[(issue_events["seg_index"] >= 0) & (issue_events["seg_index"] < total_segments)]

                        if not issue_events.empty:
                            is_read = issue_events["rwbs"].str.startswith("R")
                            is_write = issue_events["rwbs"].str.startswith("W")

                            read_events = issue_events[is_read]
                            write_events = issue_events[is_write]

                            summary["block_read_bytes"] = int(read_events["bytes"].sum())
                            summary["block_write_bytes"] = int(write_events["bytes"].sum())
                            summary["block_read_count"] = int(len(read_events))
                            summary["block_write_count"] = int(len(write_events))
                            summary["total_requests"] = int(len(issue_events))

                            grouped = issue_events.groupby("seg_index")
                            for seg_idx, group in grouped:
                                seg_idx = int(seg_idx)
                                read_group = group[group["rwbs"].str.startswith("R")]
                                write_group = group[group["rwbs"].str.startswith("W")]

                                read_bytes = int(read_group["bytes"].sum())
                                write_bytes = int(write_group["bytes"].sum())

                                timeline_dict["block_read_bytes"][seg_idx] = read_bytes
                                timeline_dict["block_write_bytes"][seg_idx] = write_bytes
                                timeline_dict["block_read_count"][seg_idx] = int(len(read_group))
                                timeline_dict["block_write_count"][seg_idx] = int(len(write_group))
                                timeline_dict["total_requests"][seg_idx] = int(len(group))

        except Exception as e:
            print(f"IO分析出错: {e}")

        summary["block_read_mb"] = summary["block_read_bytes"] / (1024 * 1024) if summary["block_read_bytes"] else 0.0
        summary["block_write_mb"] = summary["block_write_bytes"] / (1024 * 1024) if summary["block_write_bytes"] else 0.0

        if summary["block_read_bytes"]:
            for idx, value in enumerate(timeline_dict["block_read_bytes"]):
                timeline_dict["block_read_mb"][idx] = value / (1024 * 1024)
        if summary["block_write_bytes"]:
            for idx, value in enumerate(timeline_dict["block_write_bytes"]):
                timeline_dict["block_write_mb"][idx] = value / (1024 * 1024)

        timeline_df = pd.DataFrame(timeline_dict)
        return summary, timeline_df

    def _analyze_looper(self, start_ts, end_ts):
        """分析Looper使用情况"""
        looper_usage = {
            "non_frame_handlers_ms": 0, "non_frame_handlers_count": 0, 
            "frame_handlers_ms": 0, "frame_handlers_count": 0
        }
        
        # 在多应用模式下，分析所有应用的Looper
        if self.multi_app_mode and hasattr(self, 'app_configs'):
            for app_name, config in self.app_configs.items():
                pkg_name = config['pkg_name']
                
                # 查询非Frame handlers的Looper
                try:
                    non_frame_loopers = self.tp.query(f"""
                    SELECT IMPORT('slices.with_context');
                    SELECT dur
                    FROM thread_or_process_slice
                    WHERE name LIKE 'Looper:%'
                          AND ts >= {start_ts} AND ts <= {end_ts}
                          AND process_name = '{pkg_name}'
                          AND pid = tid
                          AND name NOT LIKE 'Looper:FrameHandler:%'
                    """).as_pandas_dataframe()
                    
                    if not non_frame_loopers.empty:
                        looper_usage["non_frame_handlers_ms"] += non_frame_loopers.sum().values[0] // 1_000_000
                        looper_usage["non_frame_handlers_count"] += non_frame_loopers.count().values[0]
                    
                    # 查询Frame handlers的Looper
                    query = f"""
                    SELECT IMPORT('slices.with_context');
                    SELECT dur
                    FROM thread_or_process_slice
                    WHERE ts >= {start_ts} AND ts <= {end_ts}
                          AND process_name = '{pkg_name}'
                          AND pid = tid
                          AND name LIKE 'Choreographer#doFrame%'
                    """
                    frame_loopers = self.tp.query(query).as_pandas_dataframe()
                    
                    if not frame_loopers.empty:
                        looper_usage["frame_handlers_ms"] += frame_loopers.sum().values[0] // 1_000_000
                        looper_usage["frame_handlers_count"] += frame_loopers.count().values[0]
                        
                except Exception as e:
                    print(f"分析 {app_name} Looper时出错: {e}")
        else:
            # 单应用模式
            if not self.pkg_name:
                return looper_usage
                
            # 查询非Frame handlers的Looper
            try:
                non_frame_loopers = self.tp.query(f"""
                SELECT IMPORT('slices.with_context');
                SELECT dur
                FROM thread_or_process_slice
                WHERE name LIKE 'Looper:%'
                      AND ts >= {start_ts} AND ts <= {end_ts}
                      AND process_name = '{self.pkg_name}'
                      AND pid = tid
                      AND name NOT LIKE 'Looper:FrameHandler:%'
                """).as_pandas_dataframe()
                
                if not non_frame_loopers.empty:
                    looper_usage["non_frame_handlers_ms"] = non_frame_loopers.sum().values[0] // 1_000_000
                    looper_usage["non_frame_handlers_count"] = non_frame_loopers.count().values[0]
                
                # 查询Frame handlers的Looper
                frame_loopers = self.tp.query(f"""
                SELECT IMPORT('slices.with_context');
                SELECT dur
                FROM thread_or_process_slice
                WHERE ts >= {start_ts} AND ts <= {end_ts}
                      AND process_name = '{self.pkg_name}'
                      AND pid = tid
                      AND name LIKE 'Choreographer#doFrame%'
                """).as_pandas_dataframe()
                
                if not frame_loopers.empty:
                    looper_usage["frame_handlers_ms"] = frame_loopers.sum().values[0] // 1_000_000
                    looper_usage["frame_handlers_count"] = frame_loopers.count().values[0]
            except Exception as e:
                print(f"分析Looper时出错: {e}")
        
        return looper_usage

    def _analyze_cpu_idle_and_app_threads(self, start_ts, end_ts):
        """
        分析绑定核心的idle比例以及给定应用主线程执行Choreographer#doFrame和RenderThread的占用比例
        
        Args:
            start_ts: 开始时间戳
            end_ts: 结束时间戳
            
        Returns:
            dict: 包含CPU idle和应用线程占用分析结果
        """
        # 使用 bind_cores 或默认 [4, 5]
        bind_cores = self.bind_cores if self.bind_cores else [4, 5]
        
        # 动态构建结果字典
        cpu_idle_usage = {
            "bind_cores": bind_cores,  # 记录使用的核心列表
            "choreographer_render_combined_total_cpu_percent": 0.0,
            "choreographer_render_combined_total_cpu_percent_exclude_idle": 0.0,
            "period_duration_ms": 0.0,
            # 聚合指标 - 区分主线程总时间和Choreographer#doFrame时间
            "total_main_thread_bindcore_ms": 0.0,  # 主线程总CPU时间
            "total_main_thread_choreographer_bindcore_ms": 0.0,  # 主线程执行Choreographer#doFrame的时间
            "total_render_thread_bindcore_ms": 0.0,  # RenderThread总时间
            "total_bindcore_idle_percent": 0.0,
            "total_bindcore_utilization": 0.0,
        }
        
        # 为每个绑定核心初始化指标
        for cpu_id in bind_cores:
            cpu_idle_usage[f"cpu_{cpu_id}_idle_percent"] = 0.0
            # 主线程总时间（所有活动）
            cpu_idle_usage[f"main_thread_total_cpu_{cpu_id}_percent"] = 0.0
            cpu_idle_usage[f"main_thread_total_cpu_{cpu_id}_ms"] = 0.0
            # 主线程Choreographer#doFrame时间（渲染相关）
            cpu_idle_usage[f"main_thread_choreographer_cpu_{cpu_id}_percent"] = 0.0
            cpu_idle_usage[f"main_thread_choreographer_cpu_{cpu_id}_percent_exclude_idle"] = 0.0
            cpu_idle_usage[f"main_thread_choreographer_cpu_{cpu_id}_ms"] = 0.0
            # RenderThread时间
            cpu_idle_usage[f"render_thread_cpu_{cpu_id}_percent"] = 0.0
            cpu_idle_usage[f"render_thread_cpu_{cpu_id}_percent_exclude_idle"] = 0.0
            cpu_idle_usage[f"render_thread_cpu_{cpu_id}_ms"] = 0.0
            # 合并指标
            cpu_idle_usage[f"choreographer_render_combined_cpu_{cpu_id}_percent"] = 0.0
            cpu_idle_usage[f"choreographer_render_combined_cpu_{cpu_id}_percent_exclude_idle"] = 0.0
            cpu_idle_usage[f"total_cpu_{cpu_id}_utilization"] = 0.0
        
        duration_ns = end_ts - start_ts
        cpu_idle_usage["period_duration_ms"] = duration_ns / 1_000_000
        
        try:
            # 查询绑定核心的idle时间
            for cpu_id in bind_cores:
                # 查询该CPU上所有的调度活动
                cpu_sched_query = f"""
                SELECT
                    s.ts,
                    s.dur,
                    s.cpu
                FROM sched AS s
                WHERE s.cpu = {cpu_id}
                    AND s.ts + s.dur > {start_ts} AND s.ts < {end_ts}
                    AND not utid in (select utid from thread where is_idle)
                ORDER BY s.ts
                """
                
                try:
                    cpu_sched_df = self.tp.query(cpu_sched_query).as_pandas_dataframe()
                    
                    if not cpu_sched_df.empty:
                        # 计算重叠时间
                        cpu_sched_df['overlap_start'] = cpu_sched_df['ts'].clip(lower=start_ts)
                        cpu_sched_df['overlap_end'] = (cpu_sched_df['ts'] + cpu_sched_df['dur']).clip(upper=end_ts)
                        cpu_sched_df['overlap_dur'] = (cpu_sched_df['overlap_end'] - cpu_sched_df['overlap_start']).clip(lower=0)
                        
                        # 总的CPU活动时间
                        total_active_ns = cpu_sched_df['overlap_dur'].sum()
                        
                        # idle时间 = 总时间 - 活动时间
                        total_idle_ns = duration_ns - total_active_ns
                        idle_percent = (total_idle_ns / duration_ns) * 100
                        
                        cpu_idle_usage[f"cpu_{cpu_id}_idle_percent"] = idle_percent
                        print(f"CPU {cpu_id} 活动时间: {total_active_ns / 1_000_000:.2f} ms, idle时间: {total_idle_ns / 1_000_000:.2f} ms ({idle_percent:.2f}%)")
                    else:
                        # 如果没有调度活动，则认为CPU完全idle
                        cpu_idle_usage[f"cpu_{cpu_id}_idle_percent"] = 100.0
                        print(f"CPU {cpu_id} 没有调度活动，认为完全idle (100%)")
                        
                except Exception as e:
                    print(f"查询CPU {cpu_id} 调度数据时出错: {e}")
                    continue
            
            # 查询应用线程在绑定核心上的调度情况
            app_pkg_names = []
            if self.multi_app_mode and hasattr(self, 'app_configs'):
                app_pkg_names = [config['pkg_name'] for config in self.app_configs.values()]
            elif self.pkg_name:
                app_pkg_names = [self.pkg_name]
            
            bind_cores_str = ",".join(map(str, bind_cores))
            
            for pkg_name in app_pkg_names:
                # 1. 查询主线程总CPU时间（所有活动）
                main_thread_total_query = f"""
                SELECT
                    s.ts,
                    s.dur,
                    s.cpu,
                    th.name as thread_name,
                    pr.name as process_name
                FROM sched AS s
                JOIN thread AS th ON s.utid = th.utid
                JOIN process AS pr ON th.upid = pr.upid
                WHERE s.cpu IN ({bind_cores_str})
                    AND s.ts + s.dur > {start_ts} AND s.ts < {end_ts}
                    AND pr.name = '{pkg_name}'
                    AND th.is_main_thread = 1
                ORDER BY s.ts
                """
                
                main_thread_total_df = self.tp.query(main_thread_total_query).as_pandas_dataframe()
                
                # 分析主线程总CPU时间
                if not main_thread_total_df.empty:
                    main_thread_total_df['overlap_start'] = main_thread_total_df['ts'].clip(lower=start_ts)
                    main_thread_total_df['overlap_end'] = (main_thread_total_df['ts'] + main_thread_total_df['dur']).clip(upper=end_ts)
                    main_thread_total_df['overlap_dur'] = (main_thread_total_df['overlap_end'] - main_thread_total_df['overlap_start']).clip(lower=0)
                    
                    for cpu_id in bind_cores:
                        cpu_data = main_thread_total_df[main_thread_total_df['cpu'] == cpu_id]
                        if not cpu_data.empty:
                            total_usage_ns = cpu_data['overlap_dur'].sum()
                            total_usage_ms = total_usage_ns / 1_000_000
                            usage_percent = (total_usage_ns / duration_ns) * 100
                            
                            cpu_idle_usage[f"main_thread_total_cpu_{cpu_id}_percent"] += usage_percent
                            cpu_idle_usage[f"main_thread_total_cpu_{cpu_id}_ms"] += total_usage_ms
                            
                            print(f"主线程总时间在CPU {cpu_id}: {total_usage_ms:.2f} ms ({usage_percent:.2f}%)")
                
                # 2. 查询主线程执行Choreographer#doFrame时的CPU占用（渲染相关）
                # 优化：先查询sched，然后再join slice
                main_thread_choreographer_query = f"""
                SELECT IMPORT('slices.with_context');
                WITH main_thread_sched AS (
                    SELECT
                        s.ts,
                        s.dur,
                        s.cpu,
                        s.utid,
                        th.name as thread_name,
                        pr.name as process_name
                    FROM sched AS s
                    JOIN thread AS th ON s.utid = th.utid
                    JOIN process AS pr ON th.upid = pr.upid
                    WHERE s.cpu IN ({bind_cores_str})
                        AND s.ts + s.dur > {start_ts} AND s.ts < {end_ts}
                        AND pr.name = '{pkg_name}'
                        AND th.is_main_thread = 1
                ),
                frame_handler_slices AS (
                    SELECT utid, ts, dur
                    FROM thread_or_process_slice
                    WHERE name LIKE 'Choreographer#doFrame%'
                        AND ts + dur > {start_ts} AND ts < {end_ts}
                )
                SELECT
                    s.ts,
                    s.dur,
                    s.cpu,
                    s.thread_name,
                    s.process_name
                FROM main_thread_sched AS s
                JOIN frame_handler_slices AS sl ON s.utid = sl.utid 
                    AND sl.ts <= s.ts + s.dur 
                    AND sl.ts + sl.dur >= s.ts
                ORDER BY s.ts
                """
                
                main_thread_choreographer_df = self.tp.query(main_thread_choreographer_query).as_pandas_dataframe()
                
                # 查询RenderThread
                render_thread_query = f"""
                SELECT
                    s.ts,
                    s.dur,
                    s.cpu,
                    th.name as thread_name,
                    pr.name as process_name
                FROM sched AS s
                JOIN thread AS th ON s.utid = th.utid
                JOIN process AS pr ON th.upid = pr.upid
                WHERE s.cpu IN ({bind_cores_str})
                    AND s.ts + s.dur > {start_ts} AND s.ts < {end_ts}
                    AND pr.name = '{pkg_name}'
                    AND th.name = 'RenderThread'
                ORDER BY s.ts
                """
                
                render_thread_df = self.tp.query(render_thread_query).as_pandas_dataframe()
                
                # 分析主线程执行Choreographer#doFrame时的CPU占用（渲染相关）
                if not main_thread_choreographer_df.empty:
                    main_thread_choreographer_df['overlap_start'] = main_thread_choreographer_df['ts'].clip(lower=start_ts)
                    main_thread_choreographer_df['overlap_end'] = (main_thread_choreographer_df['ts'] + main_thread_choreographer_df['dur']).clip(upper=end_ts)
                    main_thread_choreographer_df['overlap_dur'] = (main_thread_choreographer_df['overlap_end'] - main_thread_choreographer_df['overlap_start']).clip(lower=0)
                    
                    for cpu_id in bind_cores:
                        cpu_data = main_thread_choreographer_df[main_thread_choreographer_df['cpu'] == cpu_id]
                        if not cpu_data.empty:
                            total_usage_ns = cpu_data['overlap_dur'].sum()
                            total_usage_ms = total_usage_ns / 1_000_000
                            usage_percent = (total_usage_ns / duration_ns) * 100
                            
                            # 计算不考虑idle的占用比例
                            non_idle_time_ns = duration_ns * (1 - cpu_idle_usage[f"cpu_{cpu_id}_idle_percent"] / 100)
                            usage_percent_exclude_idle = (total_usage_ns / non_idle_time_ns) * 100 if non_idle_time_ns > 0 else 0
                            
                            cpu_idle_usage[f"main_thread_choreographer_cpu_{cpu_id}_percent"] += usage_percent
                            cpu_idle_usage[f"main_thread_choreographer_cpu_{cpu_id}_percent_exclude_idle"] += usage_percent_exclude_idle
                            cpu_idle_usage[f"main_thread_choreographer_cpu_{cpu_id}_ms"] += total_usage_ms
                            
                            print(f"主线程Choreographer#doFrame在CPU {cpu_id}: {total_usage_ms:.2f} ms ({usage_percent:.2f}%, 比例: {usage_percent_exclude_idle:.2f}%)")
                else:
                    print("主线程没有执行Choreographer#doFrame的调度活动")
                
                # 分析RenderThread占用
                if not render_thread_df.empty:
                    render_thread_df['overlap_start'] = render_thread_df['ts'].clip(lower=start_ts)
                    render_thread_df['overlap_end'] = (render_thread_df['ts'] + render_thread_df['dur']).clip(upper=end_ts)
                    render_thread_df['overlap_dur'] = (render_thread_df['overlap_end'] - render_thread_df['overlap_start']).clip(lower=0)
                    
                    for cpu_id in bind_cores:
                        cpu_data = render_thread_df[render_thread_df['cpu'] == cpu_id]
                        if not cpu_data.empty:
                            total_usage_ns = cpu_data['overlap_dur'].sum()
                            total_usage_ms = total_usage_ns / 1_000_000
                            usage_percent = (total_usage_ns / duration_ns) * 100
                            
                            # 计算不考虑idle的占用比例
                            non_idle_time_ns = duration_ns * (1 - cpu_idle_usage[f"cpu_{cpu_id}_idle_percent"] / 100)
                            usage_percent_exclude_idle = (total_usage_ns / non_idle_time_ns) * 100 if non_idle_time_ns > 0 else 0
                            
                            cpu_idle_usage[f"render_thread_cpu_{cpu_id}_percent"] += usage_percent
                            cpu_idle_usage[f"render_thread_cpu_{cpu_id}_percent_exclude_idle"] += usage_percent_exclude_idle
                            cpu_idle_usage[f"render_thread_cpu_{cpu_id}_ms"] += total_usage_ms
                            
                            print(f"RenderThread在CPU {cpu_id}: {total_usage_ms:.2f} ms ({usage_percent:.2f}%, 比例: {usage_percent_exclude_idle:.2f}%)")
            
            # 计算Choreographer#doFrame和RenderThread的总占用
            for cpu_id in bind_cores:
                choreographer_percent = cpu_idle_usage[f"main_thread_choreographer_cpu_{cpu_id}_percent"]
                render_percent = cpu_idle_usage[f"render_thread_cpu_{cpu_id}_percent"]
                choreographer_percent_exclude_idle = cpu_idle_usage[f"main_thread_choreographer_cpu_{cpu_id}_percent_exclude_idle"]
                render_percent_exclude_idle = cpu_idle_usage[f"render_thread_cpu_{cpu_id}_percent_exclude_idle"]
                
                combined_percent = choreographer_percent + render_percent
                combined_percent_exclude_idle = choreographer_percent_exclude_idle + render_percent_exclude_idle
                
                cpu_idle_usage[f"choreographer_render_combined_cpu_{cpu_id}_percent"] = combined_percent
                cpu_idle_usage[f"choreographer_render_combined_cpu_{cpu_id}_percent_exclude_idle"] = combined_percent_exclude_idle
                
                print(f"渲染总时间(Choreographer#doFrame + RenderThread)在CPU {cpu_id}: {combined_percent:.2f}% (比例: {combined_percent_exclude_idle:.2f}%)")
            
            # 计算总体CPU利用率和聚合指标
            total_idle_percent = 0.0
            for cpu_id in bind_cores:
                total_utilization = 100 - cpu_idle_usage[f"cpu_{cpu_id}_idle_percent"]
                cpu_idle_usage[f"total_cpu_{cpu_id}_utilization"] = total_utilization
                total_idle_percent += cpu_idle_usage[f"cpu_{cpu_id}_idle_percent"]
                # 主线程总时间
                cpu_idle_usage["total_main_thread_bindcore_ms"] += cpu_idle_usage[f"main_thread_total_cpu_{cpu_id}_ms"]
                # 主线程Choreographer#doFrame时间（渲染相关）
                cpu_idle_usage["total_main_thread_choreographer_bindcore_ms"] += cpu_idle_usage[f"main_thread_choreographer_cpu_{cpu_id}_ms"]
                # RenderThread时间
                cpu_idle_usage["total_render_thread_bindcore_ms"] += cpu_idle_usage[f"render_thread_cpu_{cpu_id}_ms"]
            
            cpu_idle_usage["total_bindcore_idle_percent"] = total_idle_percent / len(bind_cores) if bind_cores else 0
            cpu_idle_usage["total_bindcore_utilization"] = 100 - cpu_idle_usage["total_bindcore_idle_percent"]
                
        except Exception as e:
            print(f"分析CPU idle和应用线程时出错: {e}")
            traceback.print_exc()
        
        return cpu_idle_usage

    def run_time_period_analysis(self):
        """运行时间段分析"""
        analysis_results = []
        time_periods = []
        
        # 检查是否有有效的启动时间和事件
        if not self.app_launch_start_ts or not self.app_launch_end_ts:
            print("警告: 没有找到有效的应用启动时间")

        # 1. 应用启动阶段：从trace开始到应用启动完成
        trace_start_df = self.tp.query("select * from slice order by ts limit 1").as_pandas_dataframe()
        trace_start = None 
        i = 0
        while trace_start is None and i < len(trace_start_df):
            trace_start = trace_start_df['ts'].dropna().iloc[i]
            i += 1
    
        if trace_start is None:
            print("警告: 无法找到trace开始时间")
            return analysis_results
        
        is_no_launch = False
        if self.app_launch_start_ts and self.app_launch_end_ts:
            launch_start = max(trace_start, self.app_launch_start_ts)
            launch_end = self.app_launch_end_ts
        else:
            launch_start = trace_start
            launch_end = launch_start 
            self.app_launch_start_ts = self.app_launch_end_ts = launch_start
            is_no_launch = True
        # time_periods.append((launch_start, launch_end, "Launch Start |&| Launch End"))
        
        # 2. 用户交互事件之间的时间段
        if hasattr(self, 'first_events') and self.first_events is not None and not self.first_events.empty:
            current_start = self.app_launch_start_ts
            last_event = 'App Launch' if not is_no_launch else 'Trace Start'
            for idx, event in self.first_events.iterrows():
                event_end = event['ts']
                time_periods.append((current_start, event_end, f"{last_event} |&| {event['click_on']}"))
                current_start = event_end
                last_event = event['click_on']
                
            # 3. 最后一个事件到trace结束
            task_end = self.tp.query("select * from slice order by ts desc limit 1").as_pandas_dataframe()['ts'][0]
            if hasattr(self, 'task_end_ts') and self.task_end_ts is not None:
                task_end = min(self.task_end_ts, task_end)
                print(f'taskend = min({self.task_end_ts}, {task_end}) = {task_end}')
                time_periods.append((current_start, self.task_end_ts, "Query Final Result |&| Task End"))
        else:
            print("警告: 没有找到用户交互事件，只分析应用启动阶段")
            # 只有启动阶段，添加启动后到trace结束的时间段
            trace_end = self.tp.query("select * from slice order by ts desc limit 1").as_pandas_dataframe()['ts'][0]
            time_periods.append((self.app_launch_end_ts, trace_end, "启动后到trace结束"))
        
        print(f"=== 开始分析 {len(time_periods)} 个时间段 ===")
        print(f'时间段列表:')
        for start_ts, end_ts, description in time_periods:
            print(f' - {description}: {start_ts} to {end_ts} ({(end_ts - start_ts) / 1_000_000_000:.2f}s)')
        
        # 批量分析所有时间段
        for start_ts, end_ts, description in time_periods:
            try:
                (
                    cpu_usage_avg,
                    cpu_usage_timeline,
                    cpu_time_ms,
                    cpu_time_timeline,
                    mem_usage_summary,
                    mem_usage_timeline,
                    io_usage_summary,
                    io_usage_timeline,
                    looper_usage,
                    cpu_idle_usage,
                ) = self.analyze_time_period(start_ts, end_ts, description)
                
                # 根据 render_only 模式构建结果
                if self.render_only:
                    analysis_results.append({
                        'start': start_ts,
                        'end': end_ts,
                        'wall_time': (end_ts - start_ts) / 1_000_000_000,  # seconds
                        'remaining_time': 0.0,
                        'description': description,
                        'cpu_time': cpu_time_ms,
                        'cpu_time_timeline': cpu_time_timeline,
                        'cpu_idle_usage': cpu_idle_usage,
                    })
                else:
                    analysis_results.append({
                        'start': start_ts,
                        'end': end_ts,
                        'wall_time': (end_ts - start_ts) / 1_000_000_000,  # seconds
                        'remaining_time': 0.0,
                        'description': description,
                        'cpu_usage': cpu_usage_avg,
                        'cpu_usage_timeline': cpu_usage_timeline,
                        'cpu_time': cpu_time_ms,
                        'cpu_time_timeline': cpu_time_timeline,
                        'mem_usage': mem_usage_summary,
                        'mem_usage_timeline': mem_usage_timeline,
                        'io_usage': io_usage_summary,
                        'io_usage_timeline': io_usage_timeline,
                        'cpu_idle_usage': cpu_idle_usage,
                    })
            except Exception as e:
                print(f"分析时间段 '{description}' 时出错: {e}")
                continue
        
        print(f"\n=== 总共分析了 {len(analysis_results)} 个时间段 ===")

        # 计算各个步骤的剩余时间
        # 逆序遍历
        remaining_time_s = 0
        for i in range(len(analysis_results)-1, -1, -1):
            remaining_time_s += analysis_results[i]['wall_time']
            analysis_results[i]['remaining_time'] = remaining_time_s

        total_cpu_time_ms = 0.0
        for result in analysis_results:
            cpu_time_timeline = result.get('cpu_time_timeline', pd.DataFrame())
            if not cpu_time_timeline.empty:
                total_cpu_time_ms += cpu_time_timeline['cpu_time_ms'].sum()
        print(f"所有时间段APP总CPU时间: {total_cpu_time_ms:.2f} ms")
        return analysis_results

    def save_results(self, results, output_filename="profile_results.json"):
        """保存分析结果到JSON文件"""
        output_path = Path(self.trace_path).parent / output_filename
        
        class NpEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, np.integer):
                    return int(obj)
                if isinstance(obj, np.floating):
                    return float(obj)
                if isinstance(obj, np.ndarray):
                    return obj.tolist()
                if isinstance(obj, pd.DataFrame):
                    return obj.to_dict(orient='records')
                if isinstance(obj, pd.Series):
                    return obj.tolist()
                return super(NpEncoder, self).default(obj)
        
        with open(output_path, 'w') as f:
            serializable_results = [
                {k: v for k, v in item.items() if k not in ['analysis']}
                for item in results
            ]
            json.dump(serializable_results, f, indent=4, ensure_ascii=False, cls=NpEncoder)
        
        print(f"结果已保存到: {output_path}")
        return output_path

    @staticmethod
    def print_summary(results, log_file_path=None):
        """打印分析结果摘要，同时可写入文件"""

        # 支持同时输出到文件
        class Tee:
            def __init__(self, *files):
                self.files = files
            def write(self, obj):
                for f in self.files:
                    f.write(obj)
            def flush(self):
                for f in self.files:
                    f.flush()

        log_file = None
        if log_file_path:
            log_file = open(log_file_path, "w", encoding="utf-8")
            out = Tee(sys.stdout, log_file)
        else:
            out = sys.stdout

        def _print(*args, **kwargs):
            print(*args, file=out, **kwargs)

        for idx, result in enumerate(results):
            start = result['start']
            end = result['end']
            _print(f"\n>>>>==== 时间段{idx+1} : {result['description']}, 开始: {start}, 结束: {end}, 持续: {(end - start) / 1_000_000:.2f} ms ====<<<<\n")

            looper_usage = result.get('looper_usage', {})
            non_frame_handlers_ms = looper_usage.get('non_frame_handlers_ms', 0)
            non_frame_handlers_count = looper_usage.get('non_frame_handlers_count', 0)
            frame_handlers_ms = looper_usage.get('frame_handlers_ms', 0)
            frame_handlers_count = looper_usage.get('frame_handlers_count', 0)
            _print("Non-Frame handlers ", non_frame_handlers_ms, "ms", f"({non_frame_handlers_count} times)")
            _print("Frame handlers ", frame_handlers_ms, "ms", f"({frame_handlers_count} times)")
            
            mem_usage = result.get('mem_usage', {})
            vmstat_data = mem_usage.get('vmstat', {})
            page_size_mb = 4 / 1024
            free_pages_delta = vmstat_data.get('nr_free_pages', {}).get('delta', 0)
            anon_pages_delta = vmstat_data.get('nr_anon_pages', {}).get('delta', 0)
            file_pages_delta = vmstat_data.get('nr_file_pages', {}).get('delta', 0)
            dirty_pages_delta = vmstat_data.get('nr_dirty', {}).get('delta', 0)
            zs_pages_delta = vmstat_data.get('nr_zspages', {}).get('delta', 0)
            # _print("CPU Usage:", result['summary'])
            _print(f"内存使用摘要:")
            _print(f"  free_pages_delta={free_pages_delta * page_size_mb: .2f} MB, anon_pages_delta={anon_pages_delta * page_size_mb: .2f}")
            _print(f"  file_pages_delta={file_pages_delta * page_size_mb: .2f} MB, dirty_pages_delta={dirty_pages_delta * page_size_mb: .2f} MB, zs_pages_delta={zs_pages_delta * page_size_mb: .2f} MB")
            _print(f"  workingset_refault: {vmstat_data.get('workingset_refault', {}).get('delta', 0)}")
            _print(f"  pgpgin: {vmstat_data.get('pgpgin', {}).get('delta', 0)}")
            
            io_usage = result.get('io_usage', {})
            _print(f"IO使用摘要: Block读 {io_usage['block_read_mb']:.2f}MB ({io_usage['block_read_count']}次), "
                f"Block写 {io_usage['block_write_mb']:.2f}MB ({io_usage['block_write_count']}次)")
            
            cpu_idle_usage = result.get('cpu_idle_usage', {})
            if cpu_idle_usage:
                bind_cores = cpu_idle_usage.get('bind_cores', [4, 5])
                _print(f"\nCPU Idle和应用线程占用分析 (绑定核心: {bind_cores}):")
                _print(f"  时间段持续时间: {cpu_idle_usage.get('period_duration_ms', 0):.2f}ms")
                _print(f"  绑定核心平均idle: {cpu_idle_usage.get('total_bindcore_idle_percent', 0):.2f}%, 平均利用率: {cpu_idle_usage.get('total_bindcore_utilization', 0):.2f}%")
                for cpu_id in bind_cores:
                    _print(f"  CPU {cpu_id} idle: {cpu_idle_usage.get(f'cpu_{cpu_id}_idle_percent', 0):.2f}%, 利用率: {cpu_idle_usage.get(f'total_cpu_{cpu_id}_utilization', 0):.2f}%")
                # 主线程总时间
                for cpu_id in bind_cores:
                    _print(f"  主线程总时间 CPU {cpu_id}: {cpu_idle_usage.get(f'main_thread_total_cpu_{cpu_id}_ms', 0):.2f}ms ({cpu_idle_usage.get(f'main_thread_total_cpu_{cpu_id}_percent', 0):.2f}%)")
                # 主线程Choreographer#doFrame时间（渲染相关）
                for cpu_id in bind_cores:
                    _print(f"  主线程Choreographer#doFrame CPU {cpu_id}: {cpu_idle_usage.get(f'main_thread_choreographer_cpu_{cpu_id}_ms', 0):.2f}ms ({cpu_idle_usage.get(f'main_thread_choreographer_cpu_{cpu_id}_percent', 0):.2f}%, 比例: {cpu_idle_usage.get(f'main_thread_choreographer_cpu_{cpu_id}_percent_exclude_idle', 0):.2f}%)")
                # RenderThread时间
                for cpu_id in bind_cores:
                    _print(f"  RenderThread CPU {cpu_id}: {cpu_idle_usage.get(f'render_thread_cpu_{cpu_id}_ms', 0):.2f}ms ({cpu_idle_usage.get(f'render_thread_cpu_{cpu_id}_percent', 0):.2f}%, 比例: {cpu_idle_usage.get(f'render_thread_cpu_{cpu_id}_percent_exclude_idle', 0):.2f}%)")
                # 渲染总时间
                for cpu_id in bind_cores:
                    _print(f"  渲染总时间(Choreographer+RenderThread) CPU {cpu_id}: {cpu_idle_usage.get(f'choreographer_render_combined_cpu_{cpu_id}_percent', 0):.2f}% (比例: {cpu_idle_usage.get(f'choreographer_render_combined_cpu_{cpu_id}_percent_exclude_idle', 0):.2f}%)")
                _print(f"  绑定核心汇总:")
                _print(f"    主线程总时间: {cpu_idle_usage.get('total_main_thread_bindcore_ms', 0):.2f}ms")
                _print(f"    主线程Choreographer#doFrame: {cpu_idle_usage.get('total_main_thread_choreographer_bindcore_ms', 0):.2f}ms")
                _print(f"    RenderThread: {cpu_idle_usage.get('total_render_thread_bindcore_ms', 0):.2f}ms")

        _print(f"\n=== 总体CPU使用率 ===")
        total_cpu_usage = PerformanceProfiler.calculate_total_cpu_usage(results, out=_print)

        if log_file:
            log_file.close()

    @staticmethod
    def calculate_total_cpu_usage(results, out=print):
        """
        计算所有阶段合并后的Choreographer#doFrame + RenderThread占CPU4+5总时间的百分比
        
        Args:
            results: 分析结果列表
            out: 打印函数
            
        Returns:
            dict: 包含总体CPU使用率的字典
        """
        # 从第一个结果中获取绑定核心列表
        bind_cores = [4, 5]  # 默认值
        if results and results[0].get('cpu_idle_usage'):
            bind_cores = results[0]['cpu_idle_usage'].get('bind_cores', [4, 5])
        
        # 动态初始化每个核心的累计值
        total_main_thread_total_per_cpu = {cpu_id: 0.0 for cpu_id in bind_cores}  # 主线程总时间
        total_main_thread_choreographer_per_cpu = {cpu_id: 0.0 for cpu_id in bind_cores}  # 主线程Choreographer#doFrame时间
        total_render_thread_per_cpu = {cpu_id: 0.0 for cpu_id in bind_cores}  # RenderThread时间
        total_non_idle_per_cpu = {cpu_id: 0.0 for cpu_id in bind_cores}
        total_duration_ms = 0.0
        
        for result in results:
            cpu_idle_usage = result.get('cpu_idle_usage', {})
            period_duration_ms = cpu_idle_usage.get('period_duration_ms', 0.0)
            total_duration_ms += period_duration_ms
            
            for cpu_id in bind_cores:
                total_main_thread_total_per_cpu[cpu_id] += cpu_idle_usage.get(f'main_thread_total_cpu_{cpu_id}_ms', 0.0)
                total_main_thread_choreographer_per_cpu[cpu_id] += cpu_idle_usage.get(f'main_thread_choreographer_cpu_{cpu_id}_ms', 0.0)
                total_render_thread_per_cpu[cpu_id] += cpu_idle_usage.get(f'render_thread_cpu_{cpu_id}_ms', 0.0)
                cpu_idle_percent = cpu_idle_usage.get(f'cpu_{cpu_id}_idle_percent', 0.0)
                total_non_idle_per_cpu[cpu_id] += period_duration_ms * (1 - cpu_idle_percent / 100)
        
        # 计算总和
        total_main_thread_total_ms = sum(total_main_thread_total_per_cpu.values())
        total_main_thread_choreographer_ms = sum(total_main_thread_choreographer_per_cpu.values())
        total_render_thread_ms = sum(total_render_thread_per_cpu.values())
        total_choreographer_render_ms = total_main_thread_choreographer_ms + total_render_thread_ms  # 渲染总时间
        total_bindcore_time_ms = total_duration_ms * len(bind_cores)
        total_non_idle_ms = sum(total_non_idle_per_cpu.values())
        
        if total_bindcore_time_ms > 0:
            total_usage_percent = (total_choreographer_render_ms / total_bindcore_time_ms) * 100
        else:
            total_usage_percent = 0.0
        
        if total_non_idle_ms > 0:
            total_usage_percent_exclude_idle = (total_choreographer_render_ms / total_non_idle_ms) * 100
        else:
            total_usage_percent_exclude_idle = 0.0
        
        total_cpu_usage = {
            'bind_cores': bind_cores,
            'total_main_thread_total_bindcore_ms': total_main_thread_total_ms,  # 主线程总时间
            'total_main_thread_choreographer_bindcore_ms': total_main_thread_choreographer_ms,  # 主线程Choreographer#doFrame时间
            'total_render_thread_bindcore_ms': total_render_thread_ms,  # RenderThread时间
            'total_choreographer_render_ms': total_choreographer_render_ms,  # 渲染总时间
            'total_duration_ms': total_duration_ms,
            'total_bindcore_time_ms': total_bindcore_time_ms,
            'total_non_idle_ms': total_non_idle_ms,
            'choreographer_render_combined_total_cpu_percent': total_usage_percent,
            'choreographer_render_combined_total_cpu_percent_exclude_idle': total_usage_percent_exclude_idle
        }
        
        # 添加每个核心的详细数据
        for cpu_id in bind_cores:
            total_cpu_usage[f'total_main_thread_total_cpu_{cpu_id}_ms'] = total_main_thread_total_per_cpu[cpu_id]
            total_cpu_usage[f'total_main_thread_choreographer_cpu_{cpu_id}_ms'] = total_main_thread_choreographer_per_cpu[cpu_id]
            total_cpu_usage[f'total_render_thread_cpu_{cpu_id}_ms'] = total_render_thread_per_cpu[cpu_id]
            total_cpu_usage[f'total_non_idle_cpu_{cpu_id}_ms'] = total_non_idle_per_cpu[cpu_id]
        
        out(f"\n=== 总体CPU使用率分析 (绑定核心: {bind_cores}) ===")
        out(f"总墙上时间: {total_duration_ms:.2f}ms")
        out(f"总绑定核心时间: {total_bindcore_time_ms:.2f}ms")
        out(f"总非idle时间: {total_non_idle_ms:.2f}ms")
        out(f"总CPU利用率：{(total_non_idle_ms / total_bindcore_time_ms * 100) if total_bindcore_time_ms else 0:.2f}%")
        out(f"\n--- 主线程总时间 (所有活动) ---")
        for cpu_id in bind_cores:
            out(f"  主线程总时间 CPU{cpu_id}: {total_main_thread_total_per_cpu[cpu_id]:.2f}ms")
        out(f"  主线程总时间合计: {total_main_thread_total_ms:.2f}ms")
        out(f"\n--- 渲染相关时间 ---")
        for cpu_id in bind_cores:
            out(f"  主线程Choreographer#doFrame CPU{cpu_id}: {total_main_thread_choreographer_per_cpu[cpu_id]:.2f}ms")
        for cpu_id in bind_cores:
            out(f"  RenderThread CPU{cpu_id}: {total_render_thread_per_cpu[cpu_id]:.2f}ms")
        out(f"  主线程Choreographer#doFrame合计: {total_main_thread_choreographer_ms:.2f}ms")
        out(f"  RenderThread合计: {total_render_thread_ms:.2f}ms")
        out(f"  渲染总时间(Choreographer#doFrame + RenderThread): {total_choreographer_render_ms:.2f}ms")
        out(f"\n--- 渲染占比 ---")
        out(f"渲染时间占绑定核心总时间百分比: {total_usage_percent:.2f}%")
        out(f"渲染时间占绑定核心非idle时间百分比: {total_usage_percent_exclude_idle:.2f}%")
        
        return total_cpu_usage

    def run_analysis(self, save_json=True, print_results=True):
        """运行完整的性能分析流程"""
        results = self.run_time_period_analysis()
        
        if save_json:
            self.save_results(results)
        
        if print_results:
            if self.render_only:
                self.print_render_summary(results)
            else:
                self.print_summary(results)
        
        return results
    
    def run_render_only_analysis(self, save_json=True, print_results=True):
        """只分析渲染开销的快速分析流程"""
        self.render_only = True
        results = self.run_time_period_analysis()
        
        if save_json:
            self.save_results(results, output_filename="render_profile_results.json")
        
        if print_results:
            self.print_render_summary(results)
        
        return results
    
    def print_render_summary(self, results):
        """打印渲染分析结果摘要（精简版）"""
        print("\n" + "=" * 60)
        print("渲染开销分析结果")
        print("=" * 60)
        
        # 从第一个结果获取绑定核心
        bind_cores = [4, 5]
        if results and results[0].get('cpu_idle_usage'):
            bind_cores = results[0]['cpu_idle_usage'].get('bind_cores', [4, 5])
        
        total_wall_time_ms = 0.0
        total_cpu_time_ms = 0.0
        total_main_thread_total_ms = 0.0  # 主线程总时间
        total_main_thread_choreographer_ms = 0.0  # 主线程Choreographer#doFrame时间
        total_render_thread_ms = 0.0  # RenderThread时间
        
        for idx, result in enumerate(results):
            wall_time_ms = result.get('wall_time', 0) * 1000
            total_wall_time_ms += wall_time_ms
            
            cpu_time = result.get('cpu_time', {})
            if cpu_time:
                total_cpu_time_ms += cpu_time.get('cpu_time_ms', 0)
            
            cpu_idle_usage = result.get('cpu_idle_usage', {})
            if cpu_idle_usage:
                main_thread_total_ms = cpu_idle_usage.get('total_main_thread_bindcore_ms', 0)
                main_thread_choreographer_ms = cpu_idle_usage.get('total_main_thread_choreographer_bindcore_ms', 0)
                render_thread_ms = cpu_idle_usage.get('total_render_thread_bindcore_ms', 0)
                total_main_thread_total_ms += main_thread_total_ms
                total_main_thread_choreographer_ms += main_thread_choreographer_ms
                total_render_thread_ms += render_thread_ms
                
                print(f"\n阶段 {idx+1}: {result.get('description', 'N/A')}")
                print(f"  墙上时间: {wall_time_ms:.2f}ms")
                print(f"  APP CPU时间: {cpu_time.get('cpu_time_ms', 0):.2f}ms" if cpu_time else "  APP CPU时间: N/A")
                print(f"  主线程总时间: {main_thread_total_ms:.2f}ms")
                print(f"  渲染时间: Choreographer#doFrame={main_thread_choreographer_ms:.2f}ms, RenderThread={render_thread_ms:.2f}ms")
        
        # 计算渲染总时间
        total_render_time_ms = total_main_thread_choreographer_ms + total_render_thread_ms
        
        print("\n" + "-" * 60)
        print("总计:")
        print(f"  总墙上时间: {total_wall_time_ms:.2f}ms")
        print(f"  总APP CPU时间 (绑定核心{bind_cores}): {total_cpu_time_ms:.2f}ms")
        print(f"  主线程总时间: {total_main_thread_total_ms:.2f}ms")
        print(f"  主线程Choreographer#doFrame时间: {total_main_thread_choreographer_ms:.2f}ms")
        print(f"  RenderThread时间: {total_render_thread_ms:.2f}ms")
        print(f"  渲染总时间 (Choreographer#doFrame + RenderThread): {total_render_time_ms:.2f}ms")
        
        if total_cpu_time_ms > 0:
            render_ratio = (total_render_time_ms / total_cpu_time_ms) * 100
            print(f"  渲染时间占APP CPU时间比例: {render_ratio:.2f}%")
        
        if total_main_thread_total_ms > 0:
            choreographer_ratio = (total_main_thread_choreographer_ms / total_main_thread_total_ms) * 100
            print(f"  Choreographer#doFrame占主线程总时间比例: {choreographer_ratio:.2f}%")
        
        if total_wall_time_ms > 0:
            cpu_ratio = (total_cpu_time_ms / total_wall_time_ms) * 100
            print(f"  APP CPU时间占墙上时间比例: {cpu_ratio:.2f}%")
        
        print("=" * 60)

def main(trace_path: str, app_name: Optional[str] = None, pkg_name: Optional[str] = None, reprocess: bool = False, bind_cores: str = '2-5', render_only: bool = False) -> None:
    # trace_path = "/home/lxr2/aosp_host_working_dir/aiagent_test/multiapp/agent_20250714_163824/ctrip.android.view/xiechengsequential_1_direc_2025-07-14-16-38-25/perfetto-ctrip.android.view-8777-1752482305.765387.trace"
    if app_name is None:
        app_names = [task['name'] for task in tasks]
    else:
        app_names = [app_name]
    # 检测是否为多应用模式
    trace_dir = Path(trace_path).parent

    # 解析 bind_cores 参数
    core_list = []
    for part in bind_cores.split(','):
        if '-' in part:
            start, end = map(int, part.split('-'))
            core_list.extend(range(start, end + 1))
        else:
            core_list.append(int(part))
    print(f"绑定CPU核心: {core_list}")
    
    # 检查是否有多个应用的log文件
    app_logs = []
    for app_name in app_names:
        log_path = trace_dir / f"{app_name}.log"
        if log_path.exists():
            app_logs.append(app_name)
    
    if len(app_logs) > 1:
        # 多应用模式：对每个应用分别进行分析
        print(f"检测到多应用模式，发现应用: {app_logs}")
        
        # 建立应用配置映射
        app_configs = {}
        for task in tasks:
            if task['name'] in app_logs:
                app_configs[task['name']] = {
                    'pkg_name': task['package_name'],
                    'launch_activity': task.get('displayed_activity', None)
                }
        
        all_results = {}
        for app_name_item in app_logs:
            if app_name_item not in app_configs:
                print(f"警告: 未找到应用 {app_name_item} 的配置，跳过")
                continue
            
            config = app_configs[app_name_item]
            print(f"\n{'='*60}")
            print(f"分析应用: {app_name_item} (包名: {config['pkg_name']})")
            print(f"{'='*60}")
            
            # 为每个应用创建独立的分析器
            profiler = PerformanceProfiler(
                trace_path, 
                pkg_name=config['pkg_name'], 
                app_name=app_name_item, 
                launch_activity=config['launch_activity'], 
                bind_cores=core_list,
                strict_click_matching=not render_only  # render_only模式下使用宽松匹配
            )
            
            # 根据模式选择输出文件名
            if render_only:
                profile_results_path = Path(trace_dir / f"{app_name_item}_render_profile_results.json")
            else:
                profile_results_path = Path(trace_dir / f"{app_name_item}_profile_results.json")
            
            if reprocess or not profile_results_path.exists():
                if render_only:
                    results = profiler.run_render_only_analysis()
                else:
                    results = profiler.run_analysis()
            else:
                results = json.loads(profile_results_path.read_text(encoding='utf-8'))
                if render_only:
                    profiler.print_render_summary(results)
                else:
                    profiler.print_summary(results)
            
            all_results[app_name_item] = results
        
        # 保存汇总结果
        summary_path = trace_dir / ("multi_app_render_summary.json" if render_only else "multi_app_summary.json")
        with open(summary_path, 'w', encoding='utf-8') as f:
            json.dump(all_results, f, indent=2)
        print(f"\n多应用汇总结果已保存到: {summary_path}")
        
    else:
        # 单应用模式
        print("单应用模式")
        has_app = False
        launch_activity = None
        
        # 如果直接指定了 pkg_name，跳过 tasks.json 查找
        if pkg_name is not None:
            if app_name is None:
                app_name = pkg_name  # 用包名作为显示名
            # 尝试从 tasks 中补全 launch_activity
            for task in tasks:
                if task['package_name'] == pkg_name:
                    launch_activity = task.get('displayed_activity', None)
                    if app_name == pkg_name:
                        app_name = task['name']
                    break
            has_app = True
        
        # 如果用户指定了 app_name，优先使用用户指定的
        if not has_app and app_name is not None:
            for task in tasks:
                if task['name'] == app_name:
                    pkg_name = task['package_name']
                    launch_activity = task.get('displayed_activity', None)
                    has_app = True
                    break
        
        # 如果没有指定或未找到，尝试从路径中自动匹配
        if not has_app:
            for task in tasks:
                if task['name'] in trace_path:
                    pkg_name = task['package_name']
                    app_name = task['name']
                    launch_activity = task.get('displayed_activity', None)
                    has_app = True
                    break
        
        if not has_app:
            print("错误: 无法识别应用类型，请在路径中包含应用名称或使用 --app_name 或 --pkg_name 指定")
            sys.exit(1)
        
        print(f"分析应用: {app_name}, 包名: {pkg_name}")
        profiler = PerformanceProfiler(
            trace_path, pkg_name=pkg_name, app_name=app_name, 
            launch_activity=launch_activity, bind_cores=core_list,
            strict_click_matching=not render_only  # render_only模式下使用宽松匹配
        )
    
        # 根据模式选择输出文件名
        if render_only:
            profile_results_path = Path(trace_dir / "render_profile_results.json")
        else:
            profile_results_path = Path(trace_dir / "profile_results.json")
        
        if reprocess or not profile_results_path.exists():
            if render_only:
                results = profiler.run_render_only_analysis()
            else:
                results = profiler.run_analysis()
        else:
            results = json.loads(profile_results_path.read_text(encoding='utf-8'))
            if render_only:
                profiler.print_render_summary(results)
            else:
                profiler.print_summary(results)


if __name__ == "__main__":
    tapify(main)
    