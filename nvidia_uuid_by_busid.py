#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
列出 NVIDIA GPU 的 PCI Bus ID 与 UUID 对应关系。

RTX 等消费级显卡通常在 nvidia-smi 中无法读取 SN，而这个脚本通过 nvidia-smi
直接获取每张卡 immutable 的 UUID，方便按 Bus ID 定位物理卡。
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_FIELDS = ["pci.bus_id", "uuid", "name"]


def run_query(fields: list[str], timeout: int = 15) -> list[list[str]]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        raise RuntimeError("nvidia-smi not found in PATH")

    cmd = [
        nvidia_smi,
        f"--query-gpu={','.join(fields)}",
        "--format=csv,noheader",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        err = proc.stderr.strip() or "nvidia-smi returned non-zero"
        raise RuntimeError(err)

    rows: list[list[str]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        row = [cell.strip() for cell in csv.reader([line]).__next__()]
        if len(row) != len(fields):
            continue
        rows.append(row)
    return rows


def parse_rows(rows: list[list[str]], fields: list[str]) -> list[dict[str, str]]:
    headers = ["bus_id" if f == "pci.bus_id" else f for f in fields]
    return [dict(zip(headers, row)) for row in rows]


def print_table(gpus: list[dict[str, str]]) -> None:
    if not gpus:
        print("No NVIDIA GPUs detected.")
        return

    headers = ["Index", "Bus ID", "UUID", "GPU Name"]
    widths = [
        max(len(headers[0]), max(len(str(i)) for i, _ in enumerate(gpus))),
        max(len(headers[1]), max(len(g.get("bus_id", "")) for g in gpus)),
        max(len(headers[2]), max(len(g.get("uuid", "")) for g in gpus)),
        max(len(headers[3]), max(len(g.get("name", "")) for g in gpus)),
    ]

    separator = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    print(separator)
    print("| " + " | ".join(h.center(w) for h, w in zip(headers, widths)) + " |")
    print(separator)
    for i, g in enumerate(gpus):
        cells = [str(i), g.get("bus_id", ""), g.get("uuid", ""), g.get("name", "")]
        print("| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |")
    print(separator)
    print(f"Total: {len(gpus)} GPU(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List NVIDIA GPU UUIDs by PCI bus ID.")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    parser.add_argument(
        "--with-serial", action="store_true",
        help="同时查询序列号（RTX 消费卡通常为 N/A）"
    )
    parser.add_argument(
        "--timeout", type=int, default=15,
        help="nvidia-smi 超时时间（秒）"
    )
    args = parser.parse_args(argv)

    fields = list(DEFAULT_FIELDS)
    if args.with_serial:
        fields.append("serial")

    try:
        rows = run_query(fields, timeout=args.timeout)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    gpus = parse_rows(rows, fields)
    for i, g in enumerate(gpus):
        g["index"] = str(i)

    if args.json:
        print(json.dumps(gpus, indent=2, ensure_ascii=False))
    else:
        print_table(gpus)
    return 0


if __name__ == "__main__":
    sys.exit(main())
