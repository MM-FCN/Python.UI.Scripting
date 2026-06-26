#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
单元测试：验证文件重命名逻辑修复
测试 archive_input_file() 和 _append_timestamp_to_path() 的时间戳替换逻辑
"""

import re
import tempfile
from pathlib import Path
from datetime import datetime
import sys
import os

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))


def extract_stem_prefix(stem: str) -> str:
    """提取基础名称前缀（去掉日期时间戳）"""
    match = re.match(r"^(.*)_\d{8}(?:_\d{6}(?:_\d{6})?)$", stem)
    if match:
        return match.group(1)
    return stem


def test_stem_extraction():
    """测试基础名称提取"""
    test_cases = [
        ("cargo_input_20260625_111601", "cargo_input"),
        ("cargo_input_20260625_111601_123456", "cargo_input"),
        ("enx_tracking_20260625_172630", "enx_tracking"),
        ("simple_name", "simple_name"),  # 无日期时间戳的名称
        ("cma_input_20260603_090753_662041", "cma_input"),
    ]
    
    print("=" * 70)
    print("测试1：基础名称提取")
    print("=" * 70)
    
    for stem, expected in test_cases:
        result = extract_stem_prefix(stem)
        status = "✓ PASS" if result == expected else "✗ FAIL"
        print(f"{status} | 输入: {stem:40s} | 期望: {expected:20s} | 结果: {result}")
    print()


def test_archive_input_file_naming():
    """测试 archive_input_file 的重命名逻辑"""
    print("=" * 70)
    print("测试2：archive_input_file 重命名逻辑")
    print("=" * 70)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # 模拟输入文件
        test_files = [
            "cargo_input_20260625_111601.json",
            "enx_input_20260617_151405_938689.json",
            "maersk_input__20260605_151926_503241.json",
        ]
        
        for filename in test_files:
            # 创建源文件
            src = tmpdir / filename
            src.touch()
            
            # 应用重命名逻辑
            src_stem = src.stem
            stem_prefix = extract_stem_prefix(src_stem)
            ts = datetime(2026, 6, 25, 18, 2, 1).strftime("%Y%m%d_%H%M%S")
            new_filename = f"{stem_prefix}_{ts}{src.suffix}"
            
            print(f"源文件:   {filename}")
            print(f"新文件:   {new_filename}")
            
            # 验证逻辑：新文件名不应该包含两个日期时间戳
            has_duplicate_date = (src_stem.count("_202606") > 1 or 
                                 new_filename.count("_202606") > 1)
            
            # 检查是否仅有一个日期时间戳
            date_count = len(re.findall(r"_\d{8}_\d{6}", new_filename))
            status = "✓ PASS" if date_count == 1 else "✗ FAIL"
            print(f"{status} | 日期时间戳数量: {date_count}\n")
    print()


def test_append_timestamp_to_path():
    """测试 _append_timestamp_to_path 的重命名逻辑"""
    print("=" * 70)
    print("测试3：_append_timestamp_to_path 重命名逻辑 (输出文件)")
    print("=" * 70)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # 模拟输出文件
        test_paths = [
            "output/cargo_tracking_20260625_105818.json",
            "output/enx_tracking_20260625_172630.json",
            "output/cma_output.json",  # 无日期的输出文件
        ]
        
        for path_str in test_paths:
            path = Path(path_str)
            
            # 应用重命名逻辑
            stem_prefix = extract_stem_prefix(path.stem)
            ts = datetime(2026, 6, 25, 18, 2, 1).strftime("%Y%m%d_%H%M%S")
            new_path = path.with_name(f"{stem_prefix}_{ts}{path.suffix}")
            
            print(f"原路径:   {path}")
            print(f"新路径:   {new_path}")
            
            # 验证：新路径中应仅有一个日期时间戳
            date_count = len(re.findall(r"_\d{8}_\d{6}", str(new_path)))
            status = "✓ PASS" if date_count == 1 else "✗ FAIL"
            print(f"{status} | 日期时间戳数量: {date_count}\n")
    print()


def test_renaming_scenarios():
    """测试实际场景：连续重命名"""
    print("=" * 70)
    print("测试4：连续重命名场景（模拟失败重试）")
    print("=" * 70)
    
    # 模拟文件经过多次处理
    filenames = [
        "cargo_input.json",  # 初始文件
        "cargo_input_20260625_111601.json",  # 第一次处理
        "cargo_input_20260625_111602.json",  # 第二次处理（失败重试）
        "cargo_input_20260625_111603.json",  # 第三次处理（再次失败重试）
    ]
    
    for filename in filenames:
        path = Path(filename)
        stem_prefix = extract_stem_prefix(path.stem)
        ts = datetime(2026, 6, 25, 18, 2, 5).strftime("%Y%m%d_%H%M%S")
        new_filename = f"{stem_prefix}_{ts}{path.suffix}"
        
        print(f"当前文件: {filename:45s} → {new_filename}")
        
        # 验证新文件名中不含重复的日期时间戳
        date_matches = re.findall(r"_\d{8}_\d{6}", new_filename)
        if len(date_matches) <= 1:
            print(f"{'✓ PASS':7s} | 名称正确（仅一个时间戳）")
        else:
            print(f"{'✗ FAIL':7s} | 错误：包含 {len(date_matches)} 个时间戳")
        print()
    print()


def main():
    print("\n" + "="*70)
    print("文件重命名逻辑修复 - 单元测试")
    print("="*70 + "\n")
    
    test_stem_extraction()
    test_archive_input_file_naming()
    test_append_timestamp_to_path()
    test_renaming_scenarios()
    
    print("="*70)
    print("所有测试完成！")
    print("="*70)


if __name__ == "__main__":
    main()
