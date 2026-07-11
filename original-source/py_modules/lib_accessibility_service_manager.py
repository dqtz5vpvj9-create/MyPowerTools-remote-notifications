#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import logging
import argparse
import subprocess
import socket
import json
from typing import Optional, Dict, Any, Union, List, Tuple
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

# 在宿主机上执行命令（不带adb shell前缀）
def run_host_cmd(cmd, stdout_no_print=False, timeout=30):
    """在宿主机上执行命令并返回输出"""
    try:
        if stdout_no_print:
            result = subprocess.run(
                cmd, shell=True, text=True, 
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                timeout=timeout
            )
            return result.stdout.strip()
        else:
            subprocess.run(cmd, shell=True, check=False, timeout=timeout)
            return ""
    except Exception as e:
        print(f"宿主机执行命令失败: {cmd}, 错误: {e}")
        return ""

class AccessibilityServiceManager:
    """辅助功能服务管理器类"""
    
    def __init__(self, logger, package_name="io.github.ylimit.droidbotapp", 
                 service_name="io.github.privacystreams.accessibility.PSAccessibilityService"):
        self.logger = logger
        self.package_name = package_name
        self.service_name = service_name
        self.full_service_name = f"{package_name}/{service_name}"
        self.device_tcp_port = 12345  # 服务使用的TCP端口
        self.host_tcp_port = accessibility_service_port
        
        # 确保端口转发已设置
        self.setup_port_forwarding()
        
    def setup_port_forwarding(self):
        """设置ADB端口转发 (在宿主机上执行)"""
        try:
            # 首先检查是否已经设置了转发
            result = run_host_cmd(f"adb -s {serial} forward --list | grep {serial} | grep {self.device_tcp_port}", stdout_no_print=True)
            if not result:
                self.logger.verbose(f"设置端口转发: {self.device_tcp_port}")
                run_host_cmd(f"adb -s {serial} forward tcp:{self.host_tcp_port} tcp:{self.device_tcp_port}")
            else:
                self.logger.debug(f"端口 {self.device_tcp_port} 已转发: {result}")
        except Exception as e:
            self.logger.error(f"设置端口转发失败: {e}")

    def is_service_enabled(self) -> bool:
        """检查辅助功能服务是否已启用"""
        try:
            cmd = "settings get secure enabled_accessibility_services"
            output = As(cmd, AsOption.STDOUT_NO_PRINT)
            self.logger.debug(f"已启用的辅助功能服务: {output}")
            
            if not output or output == "null":
                return False
            
            return self.full_service_name in output
        except Exception as e:
            self.logger.error(f"检查辅助功能服务状态时出错: {e}")
            return False
    
    def is_service_running(self) -> bool:
        """检查辅助功能服务是否正在运行"""
        try:
            # 方法1: 使用dumpsys检查服务状态
            cmd = f"dumpsys accessibility | grep -A 5 {self.service_name}"
            output = As(cmd, [AsOption.STDERR_TO_STDOUT, AsOption.STDOUT_NO_PRINT])
            self.logger.debug(f"辅助功能服务状态: {output}")
            
            if "bound=true" in output:
                self.logger.debug("通过dumpsys accessibility确认服务正在运行")
                return True
                
            # 方法2: 检查活动服务
            cmd = f"dumpsys activity services | grep -A 10 {self.full_service_name}"
            output = As(cmd, AsOption.STDOUT_NO_PRINT)
            
            if "ConnectionRecord" in output and self.full_service_name in output:
                self.logger.debug("通过dumpsys activity services确认服务正在运行")
                return True
            
            # 方法3: 尝试连接到服务的TCP端口
            if self._check_service_socket():
                self.logger.debug("通过TCP端口确认服务正在运行")
                return True
                
            return False
        except Exception as e:
            self.logger.error(f"检查辅助功能服务运行状态时出错: {e}")
            return False
    
    def _check_service_socket(self) -> bool:
        """尝试通过TCP连接检查服务是否运行"""
        try:
            # 先确保端口转发
            self.setup_port_forwarding()
            
            # 测试连接
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2)  # 2秒超时
                result = s.connect_ex(('127.0.0.1', self.host_tcp_port))
                if result == 0:
                    self.logger.debug(f"成功连接到服务TCP端口 {self.host_tcp_port}")
                    return True
                else:
                    self.logger.debug(f"无法连接到服务TCP端口 {self.host_tcp_port}: 错误代码 {result}")
                    return False
        except Exception as e:
            self.logger.debug(f"检查服务端口时出错: {e}")
            return False
    
    def enable_service(self) -> bool:
        """启用辅助功能服务"""
        try:

            # 启动包
            As(f"am start -W -n io.github.ylimit.droidbotapp/.SettingsActivity --display 0", [AsOption.STDERR_TO_STDOUT])
            # 首先获取当前已启用的服务
            cmd = "settings get secure enabled_accessibility_services"
            current_services = As(cmd, AsOption.STDOUT_NO_PRINT).strip()
            
            if current_services == "null" or not current_services:
                new_services = self.full_service_name
            else:
                # 如果服务已经在列表中，就不需要再添加了
                if self.full_service_name in current_services:
                    self.logger.info("辅助功能服务已经启用")
                    return True
                
                # 否则，将服务添加到列表中
                if current_services == "null":
                    new_services = self.full_service_name
                else:
                    if current_services.endswith(":"):
                        new_services = f"{current_services}{self.full_service_name}"
                    else:
                        new_services = f"{current_services}:{self.full_service_name}"
            
            # 更新设置
            cmd = f"settings put secure enabled_accessibility_services '{new_services}'"
            As(cmd, AsOption.STDOUT_NO_PRINT)
            
            # 确保辅助功能总开关已启用
            cmd = "settings put secure accessibility_enabled 1"
            As(cmd, AsOption.STDOUT_NO_PRINT)
            
            # 等待一段时间让服务启动
            time.sleep(2)
            
            # 验证服务是否已启用
            return self.is_service_enabled()
        except Exception as e:
            self.logger.error(f"启用辅助功能服务时出错: {e}")
            return False
    
    def disable_service(self) -> bool:
        """禁用辅助功能服务"""
        try:
            # 获取当前已启用的服务
            cmd = "settings get secure enabled_accessibility_services"
            current_services = As(cmd)
            
            if current_services == "null" or not current_services:
                self.logger.info("没有已启用的辅助功能服务")
                return True
            
            # 如果服务在列表中，将其移除
            if self.full_service_name in current_services:
                services_list = current_services.split(":")
                new_services_list = [s for s in services_list if self.full_service_name not in s]
                new_services = ":".join(new_services_list)
                
                if not new_services:
                    # 如果没有服务，设置为null
                    cmd = "settings put secure enabled_accessibility_services null"
                    # 同时关闭辅助功能总开关
                    As("settings put secure accessibility_enabled 0")
                else:
                    cmd = f"settings put secure enabled_accessibility_services '{new_services}'"
                
                As(cmd)
                
                # 等待一段时间让服务停止
                time.sleep(2)
                
                # 验证服务是否已禁用
                return not self.is_service_enabled()
            else:
                self.logger.info("辅助功能服务未启用")
                return True
        except Exception as e:
            self.logger.error(f"禁用辅助功能服务时出错: {e}")
            return False
    
    def restart_service(self) -> bool:
        """重启辅助功能服务"""
        try:
            self.logger.info("正在重启辅助功能服务...")
            # kill the app first
            self.force_stop_app()
            
            # 先禁用服务
            self.disable_service()
            time.sleep(2)
            
            # 然后重新启用
            success = self.enable_service()
            if success:
                self.logger.info("辅助功能服务已成功重启")
            else:
                self.logger.error("重启辅助功能服务失败")
            
            return success
        except Exception as e:
            self.logger.error(f"重启辅助功能服务时出错: {e}")
            return False
    
    def command_service(self, command: str) -> str:
        """向服务发送TCP命令"""
        try:
            # 确保端口转发
            self.setup_port_forwarding()
            
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)  # 5秒超时
                s.connect(('127.0.0.1', self.host_tcp_port))
                
                self.logger.debug(f"发送命令: {command}")
                s.sendall(f"{command}\n".encode())
                
                # 接收响应
                response = s.recv(65536).decode().strip()  # 增大缓冲区以接收完整响应
                self.logger.debug(f"接收到响应长度: {len(response)} 字节")
                
                return response
        except ConnectionRefusedError:
            self.logger.error(f"连接被拒绝，服务可能未运行或端口未正确转发")
            return f"ERROR: 连接被拒绝，服务未运行"
        except socket.timeout:
            self.logger.error("连接超时")
            return f"ERROR: 连接超时"
        except Exception as e:
            self.logger.error(f"向服务发送命令时出错: {e}")
            return f"ERROR: {str(e)}"
    
    def get_ui_info(self, displayId=0) -> str:
        """获取UI信息"""
        try:
            # 先确保服务在运行
            if not self.is_service_running():
                self.logger.warning("服务未运行，尝试启动服务...")
                self.enable_service()
                time.sleep(2)
                
                # 再次检查
                if not self.is_service_running():
                    return "ERROR: 服务未运行，无法获取UI信息"
            
            # 发送GET_UI命令
            return self.command_service(f"GET_UI_STATE {displayId}")
        except Exception as e:
            self.logger.error(f"获取UI信息时出错: {e}")
            return f"ERROR: {str(e)}"
    
    def get_window_info(self) -> str:
        """获取窗口信息"""
        try:
            # 获取窗口信息的方法1: 使用accessibility服务
            output1 = As(f"dumpsys accessibility", AsOption.STDOUT_NO_PRINT)
            
            # 提取窗口部分
            windows_section = ""
            recording = False
            
            for line in output1.split('\n'):
                if "Display[" in line:
                    recording = True
                    windows_section += line + "\n"
                elif recording and line.strip():
                    windows_section += line + "\n"
                elif recording and not line.strip():
                    recording = False
            
            # 获取窗口信息的方法2: 使用dumpsys window
            output2 = As(f"dumpsys window windows | grep -E 'Window #|mDisplayId|mSession|mOwnerUid|mAttrs'", AsOption.STDOUT_NO_PRINT)
            
            # 获取当前焦点窗口
            focus_info = As(f"dumpsys window | grep -E 'Focus|FocusedApp|mCurrentFocus'", AsOption.STDOUT_NO_PRINT)
            
            # 合并信息
            result = "=== Accessibility Windows ===\n"
            result += windows_section
            result += "\n=== Window Manager Windows ===\n"
            result += output2
            result += "\n=== Focus Information ===\n"
            result += focus_info
            
            return result
        except Exception as e:
            self.logger.error(f"获取窗口信息时出错: {e}")
            return f"ERROR: {str(e)}"
    
    def get_service_logs(self, lines=50) -> str:
        """获取服务的日志"""
        try:
            cmd = f"logcat -d -t {lines} | grep -E '{self.package_name}|{self.service_name}|PSAccessibilityService'"
            self.logger.debug(f"执行日志查询: {cmd}")
            return As(cmd, AsOption.STDOUT_NO_PRINT)
        except Exception as e:
            self.logger.error(f"获取服务日志时出错: {e}")
            return f"ERROR: {str(e)}"
    
    def clear_app_data(self) -> bool:
        """清除应用数据以解决潜在问题"""
        try:
            cmd = f"pm clear {self.package_name}"
            output = As(cmd)
            self.logger.info(f"清除应用数据结果: {output}")
            return "Success" in output
        except Exception as e:
            self.logger.error(f"清除应用数据时出错: {e}")
            return False
    
    def force_stop_app(self) -> bool:
        """强制停止应用"""
        try:
            cmd = f"am force-stop {self.package_name}"
            As(cmd)
            self.logger.info(f"已强制停止应用 {self.package_name}")
            return True
        except Exception as e:
            self.logger.error(f"强制停止应用时出错: {e}")
            return False
    
    def get_service_info(self) -> Dict[str, Any]:
        """获取服务的详细信息"""
        info = {
            "package": self.package_name,
            "service": self.service_name,
            "is_enabled": self.is_service_enabled(),
            "is_running": self.is_service_running(),
            "accessibility_enabled": False,
            "tcp_port_open": self._check_service_socket(),
            "pid": None,
            "memory_usage": None,
            "uptime": None
        }
        
        try:
            # 检查辅助功能总开关是否启用
            cmd = "settings get secure accessibility_enabled"
            output = As(cmd, AsOption.STDOUT_NO_PRINT)
            info["accessibility_enabled"] = output == "1"
            
            # 获取进程ID
            cmd = f"pidof {self.package_name}"
            output = As(cmd, AsOption.STDOUT_NO_PRINT)
            if output:
                info["pid"] = output
                
                # 获取内存使用情况
                cmd = f"dumpsys meminfo {self.package_name}"
                output = As(cmd, AsOption.STDOUT_NO_PRINT)
                for line in output.split("\n"):
                    if "TOTAL:" in line:
                        parts = line.split()
                        if len(parts) > 1:
                            info["memory_usage"] = parts[1] + "K"
                            break
                
                # 获取运行时间
                cmd = f"ps -p {info['pid']} -o etime="
                output = As(cmd, AsOption.STDOUT_NO_PRINT)
                if output:
                    info["uptime"] = output.strip()
        except Exception as e:
            self.logger.error(f"获取服务信息时出错: {e}")
        
        return info

def interactive_get_ui_mode(manager: AccessibilityServiceManager):
    """交互式get-ui模式"""
    print("=== 交互式 get-ui 模式 ===")
    print("输入 displayId 获取UI信息，输入 'quit' 或 'q' 退出")
    print("默认 displayId: 0")
    print("-" * 50)
    
    # 检查服务状态
    if not manager.is_service_running():
        print("警告: 辅助功能服务未运行，尝试启动...")
        if manager.enable_service():
            print("辅助功能服务已启动")
            time.sleep(2)
        else:
            print("启动辅助功能服务失败")
            return
    
    while True:
        try:
            # 提示用户输入
            user_input = input("\n请输入 displayId (默认0): ").strip()
            
            # 检查退出命令
            if user_input.lower() in ['quit', 'q', 'exit']:
                print("退出交互式模式")
                break
            
            # 处理空输入，使用默认值0
            if not user_input:
                display_id = 0
            else:
                try:
                    display_id = int(user_input)
                except ValueError:
                    print(f"错误: '{user_input}' 不是有效的displayId，请输入数字")
                    continue
            
            # 记录开始时间
            start_time = time.time()
            
            # 执行get-ui操作
            print(f"正在获取 displayId {display_id} 的UI信息...")
            ui_info = manager.get_ui_info(display_id)
            
            # 计算执行时间
            end_time = time.time()
            execution_time = end_time - start_time
            
            # 打印结果
            print(f"\n=== displayId {display_id} UI信息 ===")
            print(f"执行时间: {execution_time:.3f} 秒")
            print(f"响应长度: {len(ui_info)} 字符")
            print("-" * 50)
            
            if ui_info.startswith("ERROR:"):
                print(f"错误: {ui_info}")
            else:
                # 显示UI信息的前500个字符作为预览
                if len(ui_info) > 500:
                    print("UI信息预览 (前500字符):")
                    print(ui_info[:500])
                    print("...")
                    print(f"\n完整信息共 {len(ui_info)} 字符")
                    
                    # 询问是否显示完整信息
                    show_full = input("\n是否显示完整UI信息? (y/N): ").strip().lower()
                    if show_full in ['y', 'yes']:
                        print("\n=== 完整UI信息 ===")
                        print(ui_info)
                else:
                    print("UI信息:")
                    print(ui_info)
            
            print("-" * 50)
            
        except KeyboardInterrupt:
            print("\n\n检测到 Ctrl+C，退出交互式模式")
            break
        except EOFError:
            print("\n\n检测到 EOF，退出交互式模式")
            break
        except Exception as e:
            print(f"发生错误: {e}")
            continue

def main():
    """主函数，用于命令行测试和使用"""
    
    # 配置命令行参数解析
    parser = argparse.ArgumentParser(description='辅助功能服务管理工具')
    subparsers = parser.add_subparsers(dest='command', help='命令')
    
    # 检查状态命令
    status_parser = subparsers.add_parser('status', help='检查辅助功能服务状态')
    
    # 启用服务命令
    enable_parser = subparsers.add_parser('enable', help='启用辅助功能服务')
    
    # 禁用服务命令
    disable_parser = subparsers.add_parser('disable', help='禁用辅助功能服务')
    
    # 重启服务命令
    restart_parser = subparsers.add_parser('restart', help='重启辅助功能服务')
    
    # 获取UI信息命令
    get_ui_parser = subparsers.add_parser('get-ui', help='获取当前界面UI信息')
    get_ui_parser.add_argument('--display-id', '-d', type=int, default=0, help='指定displayId (默认: 0)')
    
    # 交互式get-ui模式
    interactive_parser = subparsers.add_parser('interactive', help='交互式get-ui模式')
    
    # 获取窗口信息命令
    get_window_parser = subparsers.add_parser('get-windows', help='获取当前窗口信息')
    
    # 发送命令命令
    send_parser = subparsers.add_parser('send', help='向辅助功能服务发送TCP命令')
    send_parser.add_argument('tcp_command', help='要发送的TCP命令')
    
    # 获取日志命令
    logs_parser = subparsers.add_parser('logs', help='获取辅助功能服务日志')
    logs_parser.add_argument('--lines', '-n', type=int, default=50, help='要获取的日志行数')
    
    # 清除数据命令
    clear_parser = subparsers.add_parser('clear', help='清除应用数据')
    
    # 强制停止命令
    stop_parser = subparsers.add_parser('stop', help='强制停止应用')
    
    # 获取详细信息命令
    info_parser = subparsers.add_parser('info', help='获取辅助功能服务的详细信息')
    
    # 通用选项
    parser.add_argument('--package', '-p', 
                        default='io.github.ylimit.droidbotapp',
                        help='应用包名')
    parser.add_argument('--service', '-s', 
                        default='io.github.privacystreams.accessibility.PSAccessibilityService',
                        help='服务组件名称')
    parser.add_argument('--debug', '-d', action='store_true', help='启用调试日志')
    
    args = parser.parse_args()
    
    # 设置日志级别
    log_level = logging.DEBUG if args.debug else logging.INFO
    logger = setup_logging(log_level)
    
    # 初始化服务管理器
    manager = AccessibilityServiceManager(logger, args.package, args.service)
    
    # 根据命令执行不同操作
    if args.command == 'status':
        enabled = manager.is_service_enabled()
        running = manager.is_service_running()
        print(f"辅助功能服务状态:")
        print(f"  已启用: {'是' if enabled else '否'}")
        print(f"  正在运行: {'是' if running else '否'}")
        
        # 显示端口转发状态 - 正确使用宿主机命令
        port_status = run_host_cmd(f"adb -s {serial} forward --list | grep {manager.device_tcp_port}", stdout_no_print=True)
        print(f"  端口转发: {port_status if port_status else '未设置'}")
    
    elif args.command == 'enable':
        success = manager.enable_service()
        print(f"启用辅助功能服务: {'成功' if success else '失败'}")
        if success:
            time.sleep(1)  # 等待服务启动
            running = manager.is_service_running()
            print(f"服务是否运行: {'是' if running else '否'}")
    
    elif args.command == 'disable':
        success = manager.disable_service()
        print(f"禁用辅助功能服务: {'成功' if success else '失败'}")
    
    elif args.command == 'restart':
        success = manager.restart_service()
        print(f"重启辅助功能服务: {'成功' if success else '失败'}")
        if success:
            time.sleep(1)  # 等待服务启动
            running = manager.is_service_running()
            print(f"服务是否运行: {'是' if running else '否'}")
    
    elif args.command == 'get-ui':
        start_time = time.time()
        ui_info = manager.get_ui_info(args.display_id)
        end_time = time.time()
        execution_time = end_time - start_time
        
        print(f"UI信息 (displayId: {args.display_id}):")
        print(f"执行时间: {execution_time:.3f} 秒")
        print("-" * 50)
        print(ui_info)
    
    elif args.command == 'interactive':
        interactive_get_ui_mode(manager)
    
    elif args.command == 'get-windows':
        window_info = manager.get_window_info()
        print("窗口信息:")
        print(window_info)
    
    elif args.command == 'send':
        response = manager.command_service(args.tcp_command)
        print(f"命令响应: {response}")
    
    elif args.command == 'logs':
        logs = manager.get_service_logs(args.lines)
        print("服务日志:")
        print(logs)
    
    elif args.command == 'clear':
        success = manager.clear_app_data()
        print(f"清除应用数据: {'成功' if success else '失败'}")
    
    elif args.command == 'stop':
        success = manager.force_stop_app()
        print(f"强制停止应用: {'成功' if success else '失败'}")
    
    elif args.command == 'info':
        info = manager.get_service_info()
        print("辅助功能服务详细信息:")
        for key, value in info.items():
            print(f"  {key}: {value}")
    
    else:
        print("请指定命令: status, enable, disable, restart, get-ui, interactive, get-windows, send, logs, clear, stop, 或 info")
        parser.print_help()

if __name__ == "__main__":
    main()