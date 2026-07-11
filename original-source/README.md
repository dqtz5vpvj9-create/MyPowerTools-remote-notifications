# AndroidTools

此仓库包含了安卓开发相关的一系列工具

## 备份工具

安卓开发相关代码使用基于git和基于文件两种模式备份
基于文件的备份： 在/etc/crontab中有以下项，使用restic工具每日将指定文件夹备份到实验室终端机的机械硬盘上
```
/home/linuxbrew/.linuxbrew/bin/restic -r /mnt/android_nfs_backup/restic/android/ -p /android/restic_password.txt --verbose backup --files-from /android/restic_include.txt --exclude-file /android/duplicity_exclude.txt > /home/lixr/restic.log
```

## 测试工具

测试相关工具分为三类

### 开发测试

1. test_tools/setup_zram.ps1: 开启或重置设备端zram设备
2. test_tools/zram_insmod.ps1: 插入内核模块
3. test_tools/test_art|prctl|compressfd_stress|rosalloc.ps1: 等待设备端程序启动并为之设置memory control group

### App测试

1. test_script_micro.ps1: 执行一次指定应用并收集数据
2. test_micro_full.ps1: 执行N轮指定应用并分析收集到的所有数据

### ART及Native测试
步骤：
1. 打开ci_code.ipynb
2. 创建tmux

## 调试工具

1. android_debug_app.ps1: 一个大而全的，用于启动设备端程序并等待lldb调试的脚本，支持launch/attach到native binary/app
2. debug_tools/decode_panic_address.ps1: 反编译内核二进制文件并搜索符号。其实用不到，用gdb vmlinux然后用disa反汇编就行了

## 编译工具

1. aosp_compile_flash.ps1
2. aosp_update_ko.ps1

## Python模块

## conda环境配置

```shell
# 安装主机对应版本的miniconda
wget https://repo.continuum.io/miniconda/Miniconda3-latest-Linux-x86_64.sh
sh Miniconda3-latest-Linux-x86_64.sh
# 创建依赖的conda环境
conda env create --file environment.yml
conda activate android_automatic
pip install -r requirements.txt
```

### 依赖解决方案

简单地使用pip freeze或conda export导出项目依赖存在以下问题：
1. 导出非跨平台的包（如pywin32）
2. 导出的依赖项是扁平的，无法从中看出根依赖，因而也难以升级依赖项
3. 导出的包过多，导入时因部分包不存在而出错的概率增加，且这种问题往往是已经安装大量包后才发生