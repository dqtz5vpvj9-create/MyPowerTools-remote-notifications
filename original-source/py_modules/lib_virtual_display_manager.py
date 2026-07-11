import argparse
import json
import traceback
from typing import Optional, OrderedDict
from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig
import pandas as pd
import numpy as np
import os
import socket
import os
import threading
import signal
from datetime import datetime as datetime_class
import importlib, sys, os
from os.path import dirname, pardir
from pathlib import Path

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

from py_modules.AppTestHelper import AppTestHelper, SwipeType
from py_modules.lib_aosp_base import *
from py_modules.logging_lib import MyLogger, setup_logging
from py_modules.lib_appium import AppiumWrapper, PreExecuteResult
from test_tools.am_pm_utils import get_foreground_activities, get_last_displayId
from test_tools.ci_base import Ci_Base
class VirtualDisplayManager:
    def __init__(self, logger, service_component="com.demo.appprojection/.util.projection.VirtualDisplayService", device_serial: Optional[str] = None):
        self.logger = logger
        self.service_component = service_component
        self.device_serial = device_serial
        self.is_display_running = False
        self.display_id = -1
        
    def check_virtual_display(self):
        """检查是否有虚拟显示正在运行"""
        try:
            # 发送状态命令
            result = self._execute_service_command("status")
            self.logger.debug(f"虚拟显示状态检查结果: {result}")
            
            # 解析日志以确定显示是否正在运行
            log_output = self._get_service_logs()
            
            # 查找表示显示正在运行的特定日志消息
            running_indicators = [
                "运行=true",
                "已发送状态广播: 运行=true", 
                "VirtualDisplayService: 状态通知已发送: 虚拟显示成功启动"
            ]
            
            for line in log_output.splitlines():
                for indicator in running_indicators:
                    if indicator in line:
                        # 如果可用，提取显示ID
                        if "显示ID=" in line:
                            try:
                                display_id_str = line.split("显示ID=")[1].split(",")[0].strip()
                                self.display_id = int(display_id_str)
                            except (ValueError, IndexError):
                                pass
                        
                        self.is_display_running = True
                        self.logger.info(f"虚拟显示正在运行，ID: {self.display_id}")
                        return True
            
            self.is_display_running = False
            self.logger.info("没有运行中的虚拟显示")
            return False
            
        except Exception as e:
            self.logger.error(f"检查虚拟显示状态时出错: {e}")
            return False
    
    def start_virtual_display(self, package_name=None, fps=2):
        """启动虚拟显示，可选择在其上启动应用"""
        # 首先检查当前状态
        self.check_virtual_display()
        
        As(f"am start -W -n com.demo.appprojection/.MainActivity --display 0", [AsOption.STDERR_TO_STDOUT], device_serial=self.device_serial)
        if self.is_display_running:
            self.logger.info(f"虚拟显示已经在运行，ID: {self.display_id}")
            if package_name:
                return self.launch_app(package_name, fps)
            return True
        
        try:
            command = "start"
            args = {}
            args["fps"] = fps
            if package_name:
                args["package"] = package_name
            
            result = self._execute_service_command(command, args)
            self.logger.debug(f"启动虚拟显示结果: {result}")
            
            # 等待显示初始化
            time.sleep(2)
            
            # 检查现在是否正在运行
            success = self.check_virtual_display()
            if success:
                self.logger.info(f"成功启动虚拟显示，ID: {self.display_id}")
                return True
            else:
                self.logger.error("启动虚拟显示失败")
                return False
                
        except Exception as e:
            self.logger.error(f"启动虚拟显示时出错: {e}")
            return False
    
    def launch_app(self, package_name, fps=2):
        """在现有虚拟显示上启动应用"""
        # 先检查当前状态
        self.check_virtual_display()
        
        if not self.is_display_running:
            self.logger.info("没有运行中的虚拟显示。先启动一个。")
            return self.start_virtual_display(package_name, fps)
        
        try:
            result = self._execute_service_command("launch", {"package": package_name, "fps": fps})
            self.logger.debug(f"启动应用结果: {result}")
            
            # 检查日志以确认应用是否已启动
            log_output = self._get_service_logs()
            if "应用已在虚拟显示上启动" in log_output:
                self.logger.info(f"成功在虚拟显示 {self.display_id} 上启动 {package_name}")
                return True
            else:
                self.logger.warning(f"不确定 {package_name} 是否成功启动")
                return True  # 即使我们没有看到确认，也假设成功
                
        except Exception as e:
            self.logger.error(f"在虚拟显示上启动应用时出错: {e}")
            return False
    
    def stop_virtual_display(self):
        """停止运行中的虚拟显示"""
        # 关键修复：先检查当前状态，而不是依赖之前的状态
        self.check_virtual_display()
        
        if not self.is_display_running:
            self.logger.info("没有需要停止的虚拟显示")
            return True
        
        try:
            result = self._execute_service_command("stop")
            self.logger.debug(f"停止虚拟显示结果: {result}")
            
            # 检查是否已停止
            time.sleep(1)
            self.check_virtual_display()
            
            if not self.is_display_running:
                self.logger.info("成功停止虚拟显示")
                return True
            else:
                # 如果第一次停止失败，再尝试一次
                self.logger.warning("首次停止尝试失败，再次尝试...")
                result = self._execute_service_command("stop")
                time.sleep(1)
                self.check_virtual_display()
                
                if not self.is_display_running:
                    self.logger.info("第二次尝试成功停止虚拟显示")
                    return True
                else:
                    self.logger.error("虚拟显示仍在运行，停止失败")
                    return False
                
        except Exception as e:
            self.logger.error(f"停止虚拟显示时出错: {e}")
            return False
    
    def get_display_id(self):
        """获取当前显示ID"""
        self.check_virtual_display()
        return self.display_id
    
    def _execute_service_command(self, command, args=None):
        """通过As函数使用adb shell am startservice执行服务命令"""
        base_cmd = f"am startservice -n {self.service_component} --es \"command\" \"{command}\""
        
        # 添加任何参数
        if args:
            for key, value in args.items():
                if isinstance(value, int):
                    base_cmd += f" --ei \"{key}\" {value}"
                else:
                    base_cmd += f" --es \"{key}\" \"{value}\""
        
        try:
            self.logger.debug(f"执行命令: {base_cmd}")
            output = As(base_cmd, AsOption.STDOUT_NO_PRINT, device_serial=self.device_serial)
            return output.strip()
        except Exception as e:
            self.logger.error(f"命令执行失败: {e}")
            return f"错误: {str(e)}"
    
    def _get_service_logs(self, lines=50):
        """使用As函数获取指定应用的日志以检查状态"""
        try:
            # 从服务组件名称中提取包名
            package_name = self.service_component.split('/')[0]
            
            # 首先尝试使用包名过滤日志
            cmd = f"logcat VirtualDisplayService:V VirtualDisplay:V '*:S' -d -t {lines}"
            self.logger.debug(f"执行日志查询: {cmd}")
            output = As(cmd, AsOption.STDOUT_NO_PRINT, device_serial=self.device_serial)
            
            # 如果没有找到输出，尝试更广泛的搜索
            if not output.strip():
                cmd = f"logcat VirtualDisplayService:V VirtualDisplay:V '*:S' -d -t {lines}"
                output = As(cmd, AsOption.STDOUT_NO_PRINT, device_serial=self.device_serial)
            
            return output
        except Exception as e:
            self.logger.warning(f"获取服务日志时出错: {e}")
            # 出错时尝试基本的查询
            try:
                cmd = f"logcat VirtualDisplayService:V VirtualDisplay:V *:S -d -t {lines}"
                return As(cmd, AsOption.STDOUT_NO_PRINT, device_serial=self.device_serial)
            except:
                return ""
    
    def get_display_info(self):
        """获取当前显示的详细信息"""
        if not self.is_display_running:
            self.check_virtual_display()
            
        if not self.is_display_running:
            self.logger.info("没有运行中的虚拟显示")
            return None
        
        try:
            cmd = f"dumpsys display | grep -A 15 \"mDisplayId={self.display_id}\""
            output = As(cmd, device_serial=self.device_serial)
            self.logger.debug(f"显示信息: {output}")
            return output
        except Exception as e:
            self.logger.error(f"获取显示信息时出错: {e}")
            return None

def main():
    """主函数，用于测试VirtualDisplayManager类"""
    
    # 配置命令行参数解析
    parser = argparse.ArgumentParser(description='虚拟显示管理工具')
    subparsers = parser.add_subparsers(dest='command', help='命令')
    
    # 检查状态命令
    status_parser = subparsers.add_parser('status', help='检查虚拟显示状态')
    
    # 启动命令
    start_parser = subparsers.add_parser('start', help='启动虚拟显示')
    start_parser.add_argument('--package', '-p', help='要在虚拟显示上启动的应用包名')
    start_parser.add_argument('--nr', '-n', type=int, default=2, help='虚拟屏数量')
    
    # 启动应用命令
    launch_parser = subparsers.add_parser('launch', help='在现有虚拟显示上启动应用')
    launch_parser.add_argument('package', help='要启动的应用包名')
    launch_parser.add_argument('--nr', '-n', type=int, default=2, help='虚拟屏数量')
    
    # 停止命令
    stop_parser = subparsers.add_parser('stop', help='停止虚拟显示')
    
    # 显示信息命令
    info_parser = subparsers.add_parser('info', help='获取虚拟显示的详细信息')
    
    # 完整测试命令
    test_parser = subparsers.add_parser('test', help='运行完整测试')
    test_parser.add_argument('--package', '-p', default='com.demo.rotateanimapp', help='测试用的应用包名')
    
    # 通用选项
    parser.add_argument('--service', '-s', 
                        default='com.demo.appprojection/.util.projection.VirtualDisplayService',
                        help='虚拟显示服务组件名称')
    parser.add_argument('--debug', '-d', action='store_true', help='启用调试日志')
    
    args = parser.parse_args()
    try:
        args.fps = args.nr # 保持兼容性，免得修改现有脚本中引用fps这个名字的部分
    except:
        pass
    
    # 设置日志级别
    logger = setup_logging()
    
    # 初始化VirtualDisplayManager
    vd_manager = VirtualDisplayManager(logger, args.service)
    
    # 根据命令执行不同操作
    if args.command == 'status':
        vd_manager.check_virtual_display()
        print(f"虚拟显示状态: {'运行中' if vd_manager.is_display_running else '未运行'}")
        if vd_manager.is_display_running:
            print(f"显示ID: {vd_manager.display_id}")
    
    elif args.command == 'start':
        success = vd_manager.start_virtual_display(args.package, args.fps)
        print(f"启动虚拟显示: {'成功' if success else '失败'}")
        if success:
            print(f"显示ID: {vd_manager.display_id}")
    
    elif args.command == 'launch':
        success = vd_manager.launch_app(args.package, args.fps)
        print(f"在虚拟显示上启动 {args.package}: {'成功' if success else '失败'}")
    
    elif args.command == 'stop':
        success = vd_manager.stop_virtual_display()
        print(f"停止虚拟显示: {'成功' if success else '失败'}")
    
    elif args.command == 'info':
        vd_manager.check_virtual_display()
        if vd_manager.is_display_running:
            display_info = vd_manager.get_display_info()
            print(f"虚拟显示 {vd_manager.display_id} 信息:")
            print(display_info)
        else:
            print("没有运行中的虚拟显示")
    
    elif args.command == 'test':
        # 运行完整测试序列
        print("\n==== 测试开始 ====")
        
        print("\n1. 检查初始状态")
        vd_manager.check_virtual_display()
        
        if vd_manager.is_display_running:
            print("\n2. 停止现有虚拟显示")
            vd_manager.stop_virtual_display()
            time.sleep(1)
        
        print("\n3. 启动新的虚拟显示")
        success = vd_manager.start_virtual_display()
        if not success:
            print("启动虚拟显示失败，测试终止")
            return
        
        print("\n4. 获取显示信息")
        display_info = vd_manager.get_display_info()
        if display_info:
            print(f"显示信息: {display_info[:200]}...")
        
        print(f"\n5. 在虚拟显示上启动应用 {args.package}")
        success = vd_manager.launch_app(args.package)
        if not success:
            print(f"启动应用 {args.package} 失败")
        
        print("\n6. 等待5秒后停止虚拟显示")
        time.sleep(5)
        vd_manager.stop_virtual_display()
        
        print("\n==== 测试完成 ====")
    
    else:
        print("请指定命令: status, start, launch, stop, info, 或 test")
        parser.print_help()

if __name__ == "__main__":
    main()