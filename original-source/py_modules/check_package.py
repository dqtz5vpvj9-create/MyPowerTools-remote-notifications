#!/usr/bin/env python3
"""
Requirements Generator Tool
============================

生成人类可读的 requirements.txt 文件，基于当前环境中安装的顶层包。

功能:
- 从当前 Python 环境获取顶层依赖（不包含间接依赖）
- 支持多种输出格式：仅包名、包名+版本、包名+版本范围
- 支持跨平台可用性检查
- 保留原有 requirements 文件的包顺序

用法:
    python check_package.py requirements.txt                    # 更新现有文件
    python check_package.py requirements.txt --format versioned # 带版本号
    python check_package.py --generate requirements.txt         # 生成新文件
"""

from enum import Enum
import subprocess
import sys
import json
import argparse
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
from importlib.metadata import metadata, PackageNotFoundError
from packaging import version
from packaging.requirements import Requirement

# Constants
PIP_INDEX_AVAILABLE_PROMPT: str = "Available versions:"
MIN_PIP_VERSION = "22.2"


class Platform(Enum):
    """支持的目标平台"""
    WIN_AMD64 = "win_amd64"
    LINUX_X86_64 = "linux_x86_64"
    MACOS_ARM64 = "macosx_11_0_arm64"
    MACOS_X86_64 = "macosx_10_9_x86_64"


class OutputFormat(Enum):
    """输出格式选项"""
    NAME_ONLY = "name"           # 仅包名: requests
    VERSIONED = "versioned"      # 精确版本: requests==2.28.0
    COMPATIBLE = "compatible"    # 兼容版本: requests~=2.28.0
    MINIMUM = "minimum"          # 最低版本: requests>=2.28.0


@dataclass
class Package:
    """包信息"""
    name: str
    version: str
    
    def format(self, fmt: OutputFormat) -> str:
        """根据格式输出包信息"""
        match fmt:
            case OutputFormat.NAME_ONLY:
                return self.name
            case OutputFormat.VERSIONED:
                return f"{self.name}=={self.version}"
            case OutputFormat.COMPATIBLE:
                return f"{self.name}~={self.version}"
            case OutputFormat.MINIMUM:
                return f"{self.name}>={self.version}"
    
    def __eq__(self, other: object) -> bool:
        if isinstance(other, Package):
            return self.name.lower() == other.name.lower()
        return False
    
    def __hash__(self) -> int:
        return hash(self.name.lower())


def get_root_packages() -> list[Package]:
    """
    获取当前环境中的顶层包（不包含作为依赖安装的包）。
    
    Returns:
        顶层包列表
    
    Raises:
        RuntimeError: 如果 pipdeptree 运行失败
    """
    result = subprocess.run(
        [sys.executable, '-m', 'pipdeptree', '--json-tree'],
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"pipdeptree failed: {result.stderr}")
    
    tree = json.loads(result.stdout)
    packages = []
    
    for root in tree:
        pkg_name: str = root["key"]
        pkg_version: str = root["installed_version"]
        packages.append(Package(name=pkg_name, version=pkg_version))
    
    return packages


def check_pkg_available_on_platform(pkg: str, ver: str, platform: str) -> bool:
    """检查包的特定版本是否在目标平台上可用"""
    result = subprocess.run(
        [sys.executable, '-m', 'pip', 'index', 'versions', '--platform', platform, pkg],
        capture_output=True,
        text=True
    )
    
    if result.stdout:
        output = str(result.stdout)
        return PIP_INDEX_AVAILABLE_PROMPT in output and ver in output
    return False


def check_pkg_available(pkg: Package, platforms: list[Platform]) -> bool:
    """检查包是否在所有目标平台上可用"""
    if not platforms:
        return True
    return all(
        check_pkg_available_on_platform(pkg.name, pkg.version, p.value)
        for p in platforms
    )


def parse_requirements_file(filepath: str) -> list[Package]:
    """
    解析现有的 requirements 文件。
    
    Args:
        filepath: requirements 文件路径
        
    Returns:
        包列表（保持原始顺序）
    """
    packages = []
    
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # 跳过空行和注释
            if not line or line.startswith('#'):
                continue
            
            try:
                req = Requirement(line)
                pkg_name = req.name
                pkg_version = ""
                
                # 提取版本号
                for spec in req.specifier:
                    if spec.operator == "==":
                        pkg_version = spec.version
                        break
                    elif spec.operator in (">=", "~="):
                        pkg_version = spec.version
                
                packages.append(Package(name=pkg_name, version=pkg_version))
            except Exception:
                # 无法解析的行跳过
                continue
    
    return packages


def generate_requirements(
    output_file: str,
    fmt: OutputFormat = OutputFormat.NAME_ONLY,
    existing_file: Optional[str] = None,
    target_platforms: Optional[list[Platform]] = None,
    verbose: bool = False
) -> None:
    """
    生成 requirements 文件。
    
    Args:
        output_file: 输出文件路径
        fmt: 输出格式
        existing_file: 现有的 requirements 文件（用于保持顺序）
        target_platforms: 目标平台列表（用于检查可用性）
        verbose: 是否输出详细信息
    """
    platforms = target_platforms or []
    current_pkgs = get_root_packages()
    current_pkg_dict = {pkg.name.lower(): pkg for pkg in current_pkgs}
    
    ordered_pkgs: list[Package] = []
    
    # 如果有现有文件，保持其顺序
    if existing_file and Path(existing_file).exists():
        old_pkgs = parse_requirements_file(existing_file)
        
        for old_pkg in old_pkgs:
            pkg_key = old_pkg.name.lower()
            if pkg_key in current_pkg_dict:
                new_pkg = current_pkg_dict[pkg_key]
                ordered_pkgs.append(new_pkg)
                del current_pkg_dict[pkg_key]
                
                # 报告版本变化
                if verbose and old_pkg.version and old_pkg.version != new_pkg.version:
                    print(f"📦 {new_pkg.name}: {old_pkg.version} → {new_pkg.version}")
            else:
                if verbose:
                    print(f"🗑️  {old_pkg.name}: removed")
    
    # 添加新包
    for pkg in current_pkg_dict.values():
        if check_pkg_available(pkg, platforms):
            if verbose:
                print(f"✨ {pkg.name}: added ({pkg.version})")
            ordered_pkgs.append(pkg)
        else:
            if verbose:
                print(f"⚠️  {pkg.name}: skipped (not available on all platforms)")
    
    # 按名称排序新添加的包
    ordered_pkgs.sort(key=lambda p: p.name.lower())
    
    # 写入文件
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"# Auto-generated requirements file\n")
        f.write(f"# Generated by check_package.py\n")
        f.write(f"# Python {sys.version.split()[0]}\n\n")
        
        for pkg in ordered_pkgs:
            f.write(f"{pkg.format(fmt)}\n")
    
    print(f"\n✅ Generated {output_file} with {len(ordered_pkgs)} packages")


def check_pkg_installed(pkg: str, min_ver: Optional[str] = None) -> bool:
    """
    检查包是否已安装（可选：检查最低版本）。
    
    Args:
        pkg: 包名
        min_ver: 最低版本要求
        
    Returns:
        是否满足要求
    """
    try:
        mt = metadata(pkg)
    except PackageNotFoundError:
        return False
    
    if min_ver is not None:
        installed_ver = version.parse(mt["Version"])
        if installed_ver < version.parse(min_ver):
            return False
    
    return True


def check_dependencies() -> bool:
    """检查必要的依赖是否已安装"""
    missing = False
    
    if not check_pkg_installed("pip", MIN_PIP_VERSION):
        print(f"❌ pip >= {MIN_PIP_VERSION} is required")
        missing = True
    
    if not check_pkg_installed("pipdeptree"):
        print("❌ pipdeptree is required. Install with: pip install pipdeptree")
        missing = True
    
    return not missing


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Generate human-readable requirements.txt from current environment',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s requirements.txt                     # Update existing file (names only)
  %(prog)s requirements.txt --format versioned  # With exact versions
  %(prog)s --generate new_requirements.txt      # Generate new file
  %(prog)s requirements.txt -v                  # Verbose output
        """
    )
    
    parser.add_argument(
        'filename',
        type=str,
        help='Output requirements.txt file path'
    )
    
    parser.add_argument(
        '--format', '-f',
        type=str,
        choices=['name', 'versioned', 'compatible', 'minimum'],
        default='name',
        help='Output format (default: name)'
    )
    
    parser.add_argument(
        '--generate', '-g',
        action='store_true',
        help='Generate new file (ignore existing)'
    )
    
    parser.add_argument(
        '--platforms', '-p',
        type=str,
        nargs='+',
        choices=['win', 'linux', 'macos-arm', 'macos-x86'],
        help='Target platforms for availability check'
    )
    
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show detailed changes'
    )
    
    args = parser.parse_args()
    
    # 检查依赖
    if not check_dependencies():
        sys.exit(1)
    
    # 解析格式
    fmt_map = {
        'name': OutputFormat.NAME_ONLY,
        'versioned': OutputFormat.VERSIONED,
        'compatible': OutputFormat.COMPATIBLE,
        'minimum': OutputFormat.MINIMUM,
    }
    output_format = fmt_map[args.format]
    
    # 解析平台
    platform_map = {
        'win': Platform.WIN_AMD64,
        'linux': Platform.LINUX_X86_64,
        'macos-arm': Platform.MACOS_ARM64,
        'macos-x86': Platform.MACOS_X86_64,
    }
    platforms = [platform_map[p] for p in (args.platforms or [])]
    
    # 生成文件
    existing_file = None if args.generate else args.filename
    
    generate_requirements(
        output_file=args.filename,
        fmt=output_format,
        existing_file=existing_file,
        target_platforms=platforms,
        verbose=args.verbose
    )


if __name__ == "__main__":
    main()

