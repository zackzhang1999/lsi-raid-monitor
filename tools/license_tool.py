#!/usr/bin/env python3
"""DataMax-LSI 授权签发工具（厂商侧使用，不随产品分发）。

离线授权的核心是**非对称签名**：厂商用私钥签发，产品内置公钥验证。
私钥永远不出厂商的机房——客户机器上即使拿到全部代码，也无法伪造授权，
因为公钥只能验签、不能签名。

用法：
    python3 license_tool.py keygen  --out ./keys
    python3 license_tool.py issue   --key ./keys/private.pem \\
        --licensee "某某数据中心" --edition pro --days 365 --seats 4
    python3 license_tool.py verify  --pub ./keys/public.pem --file license.json
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
except ImportError:  # pragma: no cover
    print("需要 cryptography 库：pip install cryptography", file=sys.stderr)
    raise SystemExit(2)


def canonical(payload: dict) -> bytes:
    """签名的规范化字节串。双方必须用同一套序列化规则，
    否则同样内容的授权在两边算出的签名不一致。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def cmd_keygen(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    (out / "private.pem").write_bytes(priv_pem)
    (out / "public.pem").write_bytes(pub_pem)
    # 产品需要内置的是公钥的 base64（单行），方便直接贴进源码常量
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    (out / "public.b64").write_text(base64.b64encode(raw).decode(), encoding="utf-8")
    print(f"私钥: {out/'private.pem'}  ← 务必离线保管，不要随产品分发")
    print(f"公钥: {out/'public.pem'}")
    print(f"内置公钥(base64): {base64.b64encode(raw).decode()}")


def cmd_issue(args):
    priv = serialization.load_pem_private_key(Path(args.key).read_bytes(), password=None)
    now = datetime.now()
    # 默认序列号用 sha256 派生，不用内置 hash()：Python 的字符串哈希每个
    # 进程都带随机种子，同一个客户名两次签发会得到不同编号，做授权台账
    # 时对不上。sha256 是确定的。
    auto_serial = (
        f"DMX-{now:%Y%m%d}-"
        f"{int(hashlib.sha256(args.licensee.encode('utf-8')).hexdigest()[:8], 16) % 100000:05d}"
    )
    payload = {
        "product": "DataMax-LSI",
        "licensee": args.licensee,
        "edition": args.edition,
        "seats": int(args.seats),
        "serial": args.serial or auto_serial,
        "issued_at": now.strftime("%Y-%m-%d"),
        "expires_at": (now + timedelta(days=int(args.days))).strftime("%Y-%m-%d")
        if int(args.days) > 0
        else "",
    }
    sig = priv.sign(canonical(payload))
    doc = {"payload": payload, "sig": base64.b64encode(sig).decode()}
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}")
    print(text)


def cmd_verify(args):
    raw = base64.b64decode(Path(args.pub).read_text(encoding="utf-8").strip())
    pub = Ed25519PublicKey.from_public_bytes(raw)
    doc = json.loads(Path(args.file).read_text(encoding="utf-8"))
    try:
        pub.verify(base64.b64decode(doc["sig"]), canonical(doc["payload"]))
        print("签名有效")
        print(json.dumps(doc["payload"], ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"签名无效: {e}", file=sys.stderr)
        raise SystemExit(1)


def main():
    p = argparse.ArgumentParser(description="DataMax-LSI 授权签发工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("keygen", help="生成签名密钥对")
    g.add_argument("--out", default="./keys")
    g.set_defaults(func=cmd_keygen)

    i = sub.add_parser("issue", help="签发授权文件")
    i.add_argument("--key", required=True, help="私钥路径")
    i.add_argument("--licensee", required=True, help="被授权方名称")
    i.add_argument("--edition", default="pro", choices=["community", "pro", "enterprise"])
    i.add_argument("--days", default="365", help="有效天数，0 表示永久")
    i.add_argument("--seats", default="1", help="可管理的设备数")
    i.add_argument("--serial", default="")
    i.add_argument("--out", default="")
    i.set_defaults(func=cmd_issue)

    v = sub.add_parser("verify", help="校验授权文件")
    v.add_argument("--pub", required=True, help="public.b64 路径")
    v.add_argument("--file", required=True)
    v.set_defaults(func=cmd_verify)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
