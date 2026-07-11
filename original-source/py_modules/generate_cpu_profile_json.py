import json
import traceback
from typing import OrderedDict, Optional
from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig
import pandas as pd
import numpy as np
import os
import re
import sys
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


class PerformanceProfiler:
    def __init__(self, trace_path: str, pkg_name: Optional[str] = None, app_name: Optional[str] = None, 
                 launch_activity: Optional[str] = None, trace_processor_bin: str = '/tmp/trace_processor_shell_51.2_release', 
                 multi_app_mode: bool = False):
        """
        初始化性能分析器
        
        Args:
            trace_path: perfetto trace文件路径
            pkg_name: 单应用模式的包名
            app_name: 单应用模式的应用名
            launch_activity: 单应用模式的启动Activity
            trace_processor_bin: trace processor二进制文件路径
            multi_app_mode: 是否为多应用模式
        """
        self.trace_path = trace_path
        self.pkg_name = pkg_name
        self.launch_activity = launch_activity
        self.multi_app_mode = multi_app_mode
        
        if not multi_app_mode:
            self.record_path = Path(trace_path).parent / f"{app_name}.log"
        else:
            # 多应用模式下，从trace路径推断应用列表
            self.app_configs = self._detect_multi_apps()
        
        # 初始化TraceProcessor
        self.tp = TraceProcessor(
            trace=trace_path,
            config=TraceProcessorConfig(
                bin_path=os.path.expanduser(trace_processor_bin), 
                ingest_ftrace_in_raw=True
            )
        )
        
        # 内部状态
        self.call_cnt = 0
        self.ams_msgs = None
        self.events = None
        self.first_events = None
        self.app_launch_start_ts = None
        self.app_launch_end_ts = None
        
        # 初始化数据
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
        app_configs = {
            'xiecheng': {
                'pkg_name': 'ctrip.android.view',
                'launch_activity': 'ctrip.business.splash.CtripSplashActivity'
            },
            'qunar': {
                'pkg_name': 'com.Qunar',
                'launch_activity': 'com.mqunar.splash.SplashActivity'
            },
            'trip': {
                'pkg_name': 'ctrip.english',
                'launch_activity': 'com.ctrip.ibu.myctrip.main.module.home.IBUHomeActivity'
            },
            'tongcheng': {
                'pkg_name': 'com.tongcheng.android',
                'launch_activity': '.LoadingActivity'
            }
        }
        
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
                    AND msg like 'Displayed {self.pkg_name}/{self.launch_activity}%'
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
        
        # 获取输入事件
        self.events = self.tp.query(
            "select * from slice where name like 'InputShell:%'"
        ).as_pandas_dataframe()
        
        # 解析点击记录
        if os.path.exists(self.record_path):
            self._parse_single_app_clicks()
        
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
        will_click_records = list(
            seq(click_records)
            .filter(lambda x: 'Will click' in x)
        )
        
        # 保留wall time信息和button名称
        self.click_wall_times = []
        button_names = []
        
        for i in range(len(will_click_records)):
            record = will_click_records[i]
            
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
                    continue
                    
        if len(button_names) == len(self.events) + 1:
            self.events['click_on'] = button_names[:-1]
            # 调整wall_times数组以匹配events
            self.events_wall_times = self.click_wall_times[:-1]
        elif len(button_names) == len(self.events):
            self.events['click_on'] = button_names
            self.events_wall_times = self.click_wall_times
        else:
            print(f"警告: 点击记录数量与事件数量不匹配! 预期 {len(self.events)}，实际 {len(button_names)}")
            for i in range(len(button_names)):
                print(f"点击记录 {i}: {button_names[i]}")
            for i in range(len(self.events)):
                print(f"事件 {i}: {self.events.iloc[i]['name']} at {self.events.iloc[i]['ts']}")
            assert False, "点击记录数量与事件数量不匹配"

        
        # 只保留每个click_on第一次出现的event
        self.first_events = self.events.drop_duplicates(
            subset=['click_on'], keep='first'
        ).reset_index(drop=True)
        
        # 为first_events保留对应的wall times
        self.first_events_wall_times = []
        for idx, row in self.first_events.iterrows():
            original_idx = self.events[self.events['ts'] == row['ts']].index[0]
            self.first_events_wall_times.append(self.events_wall_times[original_idx])
        
        # 添加 "Memory path completed - prompting LLM to extract final result" 事件
        click_records = open(self.record_path, "r").readlines()
        self._add_final_query_event(click_records)

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
        
        if reference_wall_time is None:
            print("警告: 参考事件的wall time为空，无法计算最终查询事件的ts")
            return
        
        # 计算时间差（以纳秒为单位）
        time_diff = (final_query_time - reference_wall_time).total_seconds() * 1_000_000_000
        final_query_ts = int(reference_ts + time_diff)
        
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

    def analyze_time_period(self, start_ts, end_ts, description=""):
        """
        分析指定时间段的调度切片数据
        
        Args:
            start_ts: 开始时间戳
            end_ts: 结束时间戳
            description: 时间段描述（可选）
        
        Returns:
            tuple: (analysis对象, 全局摘要, cpu_usage, mem_usage, io_usage, looper_usage)
        """
        print(f"\n>>>>=== 分析时间段 {description} ===<<<<")
        print(f"时间范围: {start_ts} -> {end_ts}")
        print(f"持续时间: {(end_ts - start_ts) / 1_000_000:.2f} ms")
        
        # 查询调度切片数据
        df = query_sched_slices(lambda query, x: self.tp.query(query).as_pandas_dataframe(), start_ts, end_ts)
        
        # 创建并运行分析器
        analysis = SchedSliceAnalyzer(df=df, start_time=start_ts, end_time=end_ts)
        analysis.run_full_analysis()
        
        # 获取并打印摘要
        summary = analysis.multi_analyzer.get_global_summary()
        cpu_usage = {"big_core": 0, "little_core": 0}
        df = analysis.multi_analyzer.aggregated_cpu_usage[['cpu', 'cpu_type', 'total_usage_ms']]
        cpu_usage["big_core"] = df[df['cpu_type'] == 'Big']['total_usage_ms'].sum()
        cpu_usage["little_core"] = df[df['cpu_type'] == 'Small']['total_usage_ms'].sum()
        
        # 分析内存使用情况
        mem_usage = self._analyze_memory(start_ts, end_ts)
        
        # 分析IO使用情况
        io_usage = self._analyze_io(start_ts, end_ts)
        
        # 分析Looper使用情况
        looper_usage = self._analyze_looper(start_ts, end_ts)
        
        # 分析CPU idle和应用线程占用情况
        cpu_idle_usage = self._analyze_cpu_idle_and_app_threads(start_ts, end_ts)
        
        return analysis, summary, cpu_usage, mem_usage, io_usage, looper_usage, cpu_idle_usage

    def _analyze_memory(self, start_ts, end_ts):
        """分析内存使用情况"""
        mem_usage = {"minor_fault": 0, "major_fault": 0}
        
        # 查询minor fault数据
        minor_fault_query = f"""
        SELECT c.ts AS ts, c.value AS min_flt_count
        FROM counters AS c
        LEFT JOIN counter_track AS t ON c.track_id = t.id
        WHERE t.name = 'pgfault'
              AND c.ts >= {start_ts} AND c.ts <= {end_ts}
        ORDER BY c.ts
        """
        minor_fault_df = self.tp.query(minor_fault_query).as_pandas_dataframe()
        
        # 查询major fault数据
        major_fault_query = f"""
        SELECT c.ts AS ts, c.value AS maj_flt_count
        FROM counters AS c
        LEFT JOIN counter_track AS t ON c.track_id = t.id
        WHERE t.name = 'pgmajfault'
              AND c.ts >= {start_ts} AND c.ts <= {end_ts}
        ORDER BY c.ts
        """
        major_fault_df = self.tp.query(major_fault_query).as_pandas_dataframe()
        
        # 计算fault增量
        if not minor_fault_df.empty:
            minor_fault_start = minor_fault_df.iloc[0]['min_flt_count']
            minor_fault_end = minor_fault_df.iloc[-1]['min_flt_count']
            mem_usage["minor_fault"] = minor_fault_end - minor_fault_start
        
        if not major_fault_df.empty:
            major_fault_start = major_fault_df.iloc[0]['maj_flt_count']
            major_fault_end = major_fault_df.iloc[-1]['maj_flt_count']
            mem_usage["major_fault"] = major_fault_end - major_fault_start
        
        # 查询vmstat数据
        vmstat_metrics = [
            'nr_free_pages', 'nr_zone_inactive_anon', 'nr_zone_active_anon', 
            'nr_zone_inactive_file', 'nr_zone_active_file', 'nr_zone_unevictable',
            'nr_anon_pages', 'nr_mapped', 'nr_file_pages', 'nr_dirty', 'nr_writeback',
            'nr_shmem', 'nr_slab_reclaimable', 'nr_slab_unreclaimable', 'nr_zspages',
            'workingset_refault', 'workingset_activate', 'pgpgin'
        ]
        
        vmstat_data = {}
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
                    start_val = df.iloc[0]['value']
                    end_val = df.iloc[-1]['value']
                    vmstat_data[metric] = {
                        'start': start_val,
                        'end': end_val,
                        'delta': end_val - start_val,
                        'data_points': len(df),
                    }
                else:
                    vmstat_data[metric] = {'start': 0, 'end': 0, 'delta': 0, 'data_points': 0}
            except Exception:
                vmstat_data[metric] = {'start': 0, 'end': 0, 'delta': 0, 'data_points': 0}
        
        mem_usage['vmstat'] = vmstat_data
        return mem_usage

    def _analyze_io(self, start_ts, end_ts):
        """分析IO使用情况"""
        io_usage = {
            "block_read_bytes": 0, "block_write_bytes": 0, "block_read_count": 0, 
            "block_write_count": 0, "avg_latency_ms": 0, "total_requests": 0
        }
        
        block_io_query = f"""
        SELECT ftrace_event.id as id, ftrace_event.ts as ts, 
               ftrace_event.name as name, to_ftrace(ftrace_event.id) as args 
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
                        return dev or 0, sector or 0, nr_sector or 0, rwbs or None
                    return None, None, None, None
                
                block_df[['dev', 'sector', 'nr_sector', 'rwbs']] = block_df['args'].apply(
                    lambda x: pd.Series(parse_block_args(x))
                )
                
                valid_block_df = block_df.dropna(subset=['dev', 'sector', 'nr_sector', 'rwbs'])
                
                if not valid_block_df.empty:
                    issue_events = valid_block_df[valid_block_df['name'] == 'block_rq_issue']
                    
                    for _, event in issue_events.iterrows():
                        if event['rwbs'] and event['nr_sector']:
                            try:
                                data_size = int(event['nr_sector']) * 512
                                if event['rwbs'].startswith('R'):
                                    io_usage["block_read_bytes"] += data_size
                                    io_usage["block_read_count"] += 1
                                elif event['rwbs'].startswith('W'):
                                    io_usage["block_write_bytes"] += data_size
                                    io_usage["block_write_count"] += 1
                            except (ValueError, TypeError):
                                continue
                    
                    io_usage["total_requests"] = len(issue_events)
        except Exception as e:
            print(f"IO分析出错: {e}")
        
        io_usage["block_read_mb"] = io_usage["block_read_bytes"] / (1024 * 1024)
        io_usage["block_write_mb"] = io_usage["block_write_bytes"] / (1024 * 1024)
        return io_usage

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
        分析CPU 4、5的idle比例以及给定应用主线程执行Choreographer#doFrame和RenderThread的占用比例
        
        Args:
            start_ts: 开始时间戳
            end_ts: 结束时间戳
            
        Returns:
            dict: 包含CPU idle和应用线程占用分析结果
        """
        cpu_idle_usage = {
            "cpu_4_idle_percent": 0.0,
            "cpu_5_idle_percent": 0.0,
            "app_main_thread_cpu_4_percent": 0.0,  # 主线程执行Choreographer#doFrame时的CPU占用
            "app_main_thread_cpu_5_percent": 0.0,  # 主线程执行Choreographer#doFrame时的CPU占用
            "app_render_thread_cpu_4_percent": 0.0,
            "app_render_thread_cpu_5_percent": 0.0,
            "app_main_thread_cpu_4_percent_exclude_idle": 0.0,
            "app_main_thread_cpu_5_percent_exclude_idle": 0.0,
            "app_render_thread_cpu_4_percent_exclude_idle": 0.0,
            "app_render_thread_cpu_5_percent_exclude_idle": 0.0,
            "choreographer_render_combined_cpu_4_percent": 0.0,  # Choreographer#doFrame和RenderThread在CPU4的总占用
            "choreographer_render_combined_cpu_5_percent": 0.0,  # Choreographer#doFrame和RenderThread在CPU5的总占用
            "choreographer_render_combined_cpu_4_percent_exclude_idle": 0.0,  # 不含idle的总占用
            "choreographer_render_combined_cpu_5_percent_exclude_idle": 0.0,  # 不含idle的总占用
            "choreographer_render_combined_total_cpu_percent": 0.0,  # Choreographer#doFrame+RenderThread占CPU4+5总使用量的百分比
            "choreographer_render_combined_total_cpu_percent_exclude_idle": 0.0,  # 不含idle的总体占用百分比
            "total_cpu_4_utilization": 0.0,
            "total_cpu_5_utilization": 0.0,
            # 添加原始时间记录（毫秒）
            "app_main_thread_cpu_4_ms": 0.0,  # 主线程在CPU4的原始时间
            "app_main_thread_cpu_5_ms": 0.0,  # 主线程在CPU5的原始时间  
            "app_render_thread_cpu_4_ms": 0.0,  # RenderThread在CPU4的原始时间
            "app_render_thread_cpu_5_ms": 0.0,  # RenderThread在CPU5的原始时间
            "period_duration_ms": 0.0,  # 当前时间段的总持续时间
        }
        
        duration_ns = end_ts - start_ts
        cpu_idle_usage["period_duration_ms"] = duration_ns / 1_000_000  # 记录当前时间段持续时间
        
        try:
            # 查询CPU 4和5的idle时间
            for cpu_id in [4, 5]:
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
            
            # 查询应用线程在CPU 4和5上的调度情况
            app_pkg_names = []
            if self.multi_app_mode and hasattr(self, 'app_configs'):
                app_pkg_names = [config['pkg_name'] for config in self.app_configs.values()]
            elif self.pkg_name:
                app_pkg_names = [self.pkg_name]
            
            for pkg_name in app_pkg_names:
                # 查询主线程执行Choreographer#doFrame时的CPU占用
                # 优化：先查询sched，然后再join slice
                main_thread_query = f"""
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
                    WHERE s.cpu IN (4, 5)
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
                
                main_thread_df = self.tp.query(main_thread_query).as_pandas_dataframe()
                
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
                WHERE s.cpu IN (4, 5)
                    AND s.ts + s.dur > {start_ts} AND s.ts < {end_ts}
                    AND pr.name = '{pkg_name}'
                    AND th.name = 'RenderThread'
                ORDER BY s.ts
                """
                
                render_thread_df = self.tp.query(render_thread_query).as_pandas_dataframe()
                
                # 分析主线程执行Choreographer#doFrame时的CPU占用
                if not main_thread_df.empty:
                    main_thread_df['overlap_start'] = main_thread_df['ts'].clip(lower=start_ts)
                    main_thread_df['overlap_end'] = (main_thread_df['ts'] + main_thread_df['dur']).clip(upper=end_ts)
                    main_thread_df['overlap_dur'] = (main_thread_df['overlap_end'] - main_thread_df['overlap_start']).clip(lower=0)
                    
                    for cpu_id in [4, 5]:
                        cpu_data = main_thread_df[main_thread_df['cpu'] == cpu_id]
                        if not cpu_data.empty:
                            total_usage_ns = cpu_data['overlap_dur'].sum()
                            total_usage_ms = total_usage_ns / 1_000_000  # 记录原始毫秒值
                            usage_percent = (total_usage_ns / duration_ns) * 100
                            
                            # 计算不考虑idle的占用比例
                            non_idle_time_ns = duration_ns * (1 - cpu_idle_usage[f"cpu_{cpu_id}_idle_percent"] / 100)
                            usage_percent_exclude_idle = (total_usage_ns / non_idle_time_ns) * 100 if non_idle_time_ns > 0 else 0
                            
                            cpu_idle_usage[f"app_main_thread_cpu_{cpu_id}_percent"] = usage_percent
                            cpu_idle_usage[f"app_main_thread_cpu_{cpu_id}_percent_exclude_idle"] = usage_percent_exclude_idle
                            cpu_idle_usage[f"app_main_thread_cpu_{cpu_id}_ms"] = total_usage_ms  # 记录原始时间
                            
                            print(f"主线程执行Choreographer#doFrame在CPU {cpu_id}占用: {total_usage_ms:.2f} ms ({usage_percent:.2f}%, 比例: {usage_percent_exclude_idle:.2f}%)")
                else:
                    print("主线程没有执行Choreographer#doFrame的调度活动")
                
                # 分析RenderThread占用
                if not render_thread_df.empty:
                    render_thread_df['overlap_start'] = render_thread_df['ts'].clip(lower=start_ts)
                    render_thread_df['overlap_end'] = (render_thread_df['ts'] + render_thread_df['dur']).clip(upper=end_ts)
                    render_thread_df['overlap_dur'] = (render_thread_df['overlap_end'] - render_thread_df['overlap_start']).clip(lower=0)
                    
                    for cpu_id in [4, 5]:
                        cpu_data = render_thread_df[render_thread_df['cpu'] == cpu_id]
                        if not cpu_data.empty:
                            total_usage_ns = cpu_data['overlap_dur'].sum()
                            total_usage_ms = total_usage_ns / 1_000_000  # 记录原始毫秒值
                            usage_percent = (total_usage_ns / duration_ns) * 100
                            
                            # 计算不考虑idle的占用比例
                            non_idle_time_ns = duration_ns * (1 - cpu_idle_usage[f"cpu_{cpu_id}_idle_percent"] / 100)
                            usage_percent_exclude_idle = (total_usage_ns / non_idle_time_ns) * 100 if non_idle_time_ns > 0 else 0
                            
                            cpu_idle_usage[f"app_render_thread_cpu_{cpu_id}_percent"] = usage_percent
                            cpu_idle_usage[f"app_render_thread_cpu_{cpu_id}_percent_exclude_idle"] = usage_percent_exclude_idle
                            cpu_idle_usage[f"app_render_thread_cpu_{cpu_id}_ms"] = total_usage_ms  # 记录原始时间
                            
                            print(f"RenderThread在CPU {cpu_id}占用: {total_usage_ms:.2f} ms ({usage_percent:.2f}%, 比例: {usage_percent_exclude_idle:.2f}%)")
            
            # 计算Choreographer#doFrame和RenderThread的总占用
            for cpu_id in [4, 5]:
                choreographer_percent = cpu_idle_usage[f"app_main_thread_cpu_{cpu_id}_percent"]
                render_percent = cpu_idle_usage[f"app_render_thread_cpu_{cpu_id}_percent"]
                choreographer_percent_exclude_idle = cpu_idle_usage[f"app_main_thread_cpu_{cpu_id}_percent_exclude_idle"]
                render_percent_exclude_idle = cpu_idle_usage[f"app_render_thread_cpu_{cpu_id}_percent_exclude_idle"]
                
                combined_percent = choreographer_percent + render_percent
                combined_percent_exclude_idle = choreographer_percent_exclude_idle + render_percent_exclude_idle
                
                cpu_idle_usage[f"choreographer_render_combined_cpu_{cpu_id}_percent"] = combined_percent
                cpu_idle_usage[f"choreographer_render_combined_cpu_{cpu_id}_percent_exclude_idle"] = combined_percent_exclude_idle
                
                print(f"Choreographer#doFrame + RenderThread在CPU {cpu_id}总占用: {combined_percent:.2f}% (比例: {combined_percent_exclude_idle:.2f}%)")
            
            # 计算总体CPU利用率
            for cpu_id in [4, 5]:
                total_utilization = 100 - cpu_idle_usage[f"cpu_{cpu_id}_idle_percent"]
                cpu_idle_usage[f"total_cpu_{cpu_id}_utilization"] = total_utilization
                
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
            return analysis_results
        
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
            
        start = max(trace_start, self.app_launch_start_ts)
        end = self.app_launch_end_ts
        time_periods.append((start, end, "应用启动阶段"))
        
        # 2. 用户交互事件之间的时间段
        if hasattr(self, 'first_events') and self.first_events is not None and not self.first_events.empty:
            current_start = self.app_launch_end_ts
            for idx, event in self.first_events.iterrows():
                event_end = event['ts']
                time_periods.append((current_start, event_end, f"用户交互间隔 {idx+1} (到 {event['click_on']})"))
                current_start = event_end
                
            # 3. 最后一个事件到trace结束
            trace_end = self.tp.query("select * from slice order by ts desc limit 1").as_pandas_dataframe()['ts'][0]
            time_periods.append((current_start, trace_end, "最后阶段到trace结束"))
        else:
            print("警告: 没有找到用户交互事件，只分析应用启动阶段")
            # 只有启动阶段，添加启动后到trace结束的时间段
            trace_end = self.tp.query("select * from slice order by ts desc limit 1").as_pandas_dataframe()['ts'][0]
            time_periods.append((self.app_launch_end_ts, trace_end, "启动后到trace结束"))
        
        print(f"=== 开始分析 {len(time_periods)} 个时间段 ===")
        
        # 批量分析所有时间段
        for start_ts, end_ts, description in time_periods:
            try:
                analysis, summary, cpu_usage, mem_usage, io_usage, looper_usage, cpu_idle_usage = self.analyze_time_period(start_ts, end_ts, description)
                analysis_results.append({
                    'start': start_ts,
                    'end': end_ts,
                    'description': description,
                    'analysis': analysis,
                    'analysis_summary': summary,
                    'summary': cpu_usage,
                    'mem_usage': mem_usage,
                    'io_usage': io_usage,
                    'looper_usage': looper_usage,
                    'cpu_idle_usage': cpu_idle_usage,
                })
            except Exception as e:
                print(f"分析时间段 '{description}' 时出错: {e}")
                continue
        
        print(f"\n=== 总共分析了 {len(analysis_results)} 个时间段 ===")
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
                return super(NpEncoder, self).default(obj)
        
        with open(output_path, 'w') as f:
            serializable_results = [
                {k: v for k, v in item.items() if k != 'analysis'}
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
            _print("CPU Usage:", result['summary'])
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
                _print(f"\nCPU Idle和应用线程占用分析:")
                _print(f"  时间段持续时间: {cpu_idle_usage.get('period_duration_ms', 0):.2f}ms")
                _print(f"  CPU 4 idle: {cpu_idle_usage.get('cpu_4_idle_percent', 0):.2f}%, 利用率: {cpu_idle_usage.get('total_cpu_4_utilization', 0):.2f}%")
                _print(f"  CPU 5 idle: {cpu_idle_usage.get('cpu_5_idle_percent', 0):.2f}%, 利用率: {cpu_idle_usage.get('total_cpu_5_utilization', 0):.2f}%")
                _print(f"  主线程执行Choreographer#doFrame时CPU 4占用: {cpu_idle_usage.get('app_main_thread_cpu_4_ms', 0):.2f}ms, 使用率：{cpu_idle_usage.get('app_main_thread_cpu_4_percent', 0):.2f}% (比例: {cpu_idle_usage.get('app_main_thread_cpu_4_percent_exclude_idle', 0):.2f}%)")
                _print(f"  主线程执行Choreographer#doFrame时CPU 5占用: {cpu_idle_usage.get('app_main_thread_cpu_5_ms', 0):.2f}ms, 使用率：{cpu_idle_usage.get('app_main_thread_cpu_5_percent', 0):.2f}% (比例: {cpu_idle_usage.get('app_main_thread_cpu_5_percent_exclude_idle', 0):.2f}%)")
                _print(f"  RenderThread CPU 4 占用: {cpu_idle_usage.get('app_render_thread_cpu_4_ms', 0):.2f}ms, 使用率：{cpu_idle_usage.get('app_render_thread_cpu_4_percent', 0):.2f}% (比例: {cpu_idle_usage.get('app_render_thread_cpu_4_percent_exclude_idle', 0):.2f}%)")
                _print(f"  RenderThread CPU 5 占用: {cpu_idle_usage.get('app_render_thread_cpu_5_ms', 0):.2f}ms, 使用率：{cpu_idle_usage.get('app_render_thread_cpu_5_percent', 0):.2f}% (比例: {cpu_idle_usage.get('app_render_thread_cpu_5_percent_exclude_idle', 0):.2f}%)")
                _print(f"  Choreographer#doFrame + RenderThread CPU 4 总占用: 使用率：{cpu_idle_usage.get('choreographer_render_combined_cpu_4_percent', 0):.2f}% (比例: {cpu_idle_usage.get('choreographer_render_combined_cpu_4_percent_exclude_idle', 0):.2f}%)")
                _print(f"  Choreographer#doFrame + RenderThread CPU 5 总占用: 使用率：{cpu_idle_usage.get('choreographer_render_combined_cpu_5_percent', 0):.2f}% (比例: {cpu_idle_usage.get('choreographer_render_combined_cpu_5_percent_exclude_idle', 0):.2f}%)")

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
        total_main_thread_cpu_4_ms = 0.0
        total_main_thread_cpu_5_ms = 0.0
        total_render_thread_cpu_4_ms = 0.0
        total_render_thread_cpu_5_ms = 0.0
        total_duration_ms = 0.0
        
        for result in results:
            cpu_idle_usage = result.get('cpu_idle_usage', {})
            total_main_thread_cpu_4_ms += cpu_idle_usage.get('app_main_thread_cpu_4_ms', 0.0)
            total_main_thread_cpu_5_ms += cpu_idle_usage.get('app_main_thread_cpu_5_ms', 0.0)
            total_render_thread_cpu_4_ms += cpu_idle_usage.get('app_render_thread_cpu_4_ms', 0.0)
            total_render_thread_cpu_5_ms += cpu_idle_usage.get('app_render_thread_cpu_5_ms', 0.0)
            total_duration_ms += cpu_idle_usage.get('period_duration_ms', 0.0)
        
        total_choreographer_render_ms = (total_main_thread_cpu_4_ms + total_main_thread_cpu_5_ms + 
                                       total_render_thread_cpu_4_ms + total_render_thread_cpu_5_ms)
        total_cpu_4_5_time_ms = total_duration_ms * 2
        
        if total_cpu_4_5_time_ms > 0:
            total_usage_percent = (total_choreographer_render_ms / total_cpu_4_5_time_ms) * 100
        else:
            total_usage_percent = 0.0
        
        total_non_idle_cpu_4_ms = 0.0
        total_non_idle_cpu_5_ms = 0.0
        
        for result in results:
            cpu_idle_usage = result.get('cpu_idle_usage', {})
            period_duration_ms = cpu_idle_usage.get('period_duration_ms', 0.0)
            cpu_4_idle_percent = cpu_idle_usage.get('cpu_4_idle_percent', 0.0)
            cpu_5_idle_percent = cpu_idle_usage.get('cpu_5_idle_percent', 0.0)
            cpu_4_non_idle_ms = period_duration_ms * (1 - cpu_4_idle_percent / 100)
            cpu_5_non_idle_ms = period_duration_ms * (1 - cpu_5_idle_percent / 100)
            total_non_idle_cpu_4_ms += cpu_4_non_idle_ms
            total_non_idle_cpu_5_ms += cpu_5_non_idle_ms
        
        total_non_idle_ms = total_non_idle_cpu_4_ms + total_non_idle_cpu_5_ms
        
        if total_non_idle_ms > 0:
            total_usage_percent_exclude_idle = (total_choreographer_render_ms / total_non_idle_ms) * 100
        else:
            total_usage_percent_exclude_idle = 0.0
        
        total_cpu_usage = {
            'total_main_thread_cpu_4_ms': total_main_thread_cpu_4_ms,
            'total_main_thread_cpu_5_ms': total_main_thread_cpu_5_ms,
            'total_render_thread_cpu_4_ms': total_render_thread_cpu_4_ms,
            'total_render_thread_cpu_5_ms': total_render_thread_cpu_5_ms,
            'total_choreographer_render_ms': total_choreographer_render_ms,
            'total_duration_ms': total_duration_ms,
            'total_cpu_4_5_time_ms': total_cpu_4_5_time_ms,
            'total_non_idle_cpu_4_ms': total_non_idle_cpu_4_ms,
            'total_non_idle_cpu_5_ms': total_non_idle_cpu_5_ms,
            'total_non_idle_ms': total_non_idle_ms,
            'choreographer_render_combined_total_cpu_percent': total_usage_percent,
            'choreographer_render_combined_total_cpu_percent_exclude_idle': total_usage_percent_exclude_idle
        }
        
        out(f"\n=== 总体CPU使用率分析 ===")
        out(f"总墙上时间: {total_duration_ms:.2f}ms")
        out(f"总CPU4+5时间: {total_cpu_4_5_time_ms:.2f}ms")
        out(f"总非idle时间: {total_non_idle_ms:.2f}ms")
        out(f"总CPU利用率：{(total_non_idle_ms / total_cpu_4_5_time_ms * 100) if total_cpu_4_5_time_ms else 0:.2f}%")
        out(f"总Choreographer#doFrame时间: CPU4={total_main_thread_cpu_4_ms:.2f}ms, CPU5={total_main_thread_cpu_5_ms:.2f}ms")
        out(f"总RenderThread时间: CPU4={total_render_thread_cpu_4_ms:.2f}ms, CPU5={total_render_thread_cpu_5_ms:.2f}ms")
        out(f"总渲染相关时间: {total_choreographer_render_ms:.2f}ms")
        out(f"Choreographer#doFrame + RenderThread占CPU4+5总时间百分比: {total_usage_percent:.2f}%")
        out(f"Choreographer#doFrame + RenderThread占CPU4+5非idle时间百分比: {total_usage_percent_exclude_idle:.2f}%")
        
        return total_cpu_usage

    def run_analysis(self, save_json=True, print_results=True):
        """运行完整的性能分析流程"""
        results = self.run_time_period_analysis()
        
        if save_json:
            self.save_results(results)
        
        if print_results:
            self.print_summary(results)
        
        return results

def main(trace_path: str, app_name: Optional[str] = None, reprocess: bool = False) -> None:
    # trace_path = "/home/lxr2/aosp_host_working_dir/aiagent_test/multiapp/agent_20250714_163824/ctrip.android.view/xiechengsequential_1_direc_2025-07-14-16-38-25/perfetto-ctrip.android.view-8777-1752482305.765387.trace"
    if app_name is None:
        app_names = ['xiecheng', 'qunar', 'trip', 'tongcheng']
    else:
        app_names = [app_name]
    # 检测是否为多应用模式
    trace_dir = Path(trace_path).parent
    
    # 检查是否有多个应用的log文件
    app_logs = []
    for app_name in app_names:
        log_path = trace_dir / f"{app_name}.log"
        if log_path.exists():
            app_logs.append(app_name)
    
    if len(app_logs) > 1:
        # 多应用模式
        print(f"检测到多应用模式，发现应用: {app_logs}")
        profiler = PerformanceProfiler(trace_path, multi_app_mode=True)
    else:
        # 单应用模式
        print("单应用模式")
        if 'xiecheng' in trace_path:
            pkg_name = "ctrip.android.view"
            app_name = "xiecheng"
            launch_activity = "ctrip.business.splash.CtripSplashActivity"
        elif 'qunar' in trace_path:
            pkg_name = "com.Qunar"
            app_name = "qunar"
            launch_activity = "com.mqunar.splash.SplashActivity"
        elif 'trip' in trace_path:
            pkg_name = "ctrip.english"
            app_name = "trip"
            launch_activity = "com.ctrip.ibu.myctrip.main.module.home.IBUHomeActivity"
        elif 'tongcheng' in trace_path:
            pkg_name = "com.tongcheng.android"
            app_name = "tongcheng"
            launch_activity = ".LoadingActivity"
        else:
            print("错误: 无法识别应用类型，请在路径中包含应用名称")
            sys.exit(1)
        
        profiler = PerformanceProfiler(trace_path, pkg_name=pkg_name, app_name=app_name, launch_activity=launch_activity)
    profile_results_path = Path(trace_dir / "profile_results.json")
    if reprocess or not profile_results_path.exists():
        results = profiler.run_analysis()
    else:
        results = json.loads(profile_results_path.read_text(encoding='utf-8'))
        profiler.print_summary(results)




# 使用示例
if __name__ == "__main__":
    # if len(sys.argv) > 1:
    #     trace_path = sys.argv[1]
    # if len(sys.argv) > 2:
    #     app_names = [sys.argv[1]]
    tapify(main) #type: ignore

    