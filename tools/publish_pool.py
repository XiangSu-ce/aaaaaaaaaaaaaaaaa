#!/usr/bin/env python3
"""发布工具：将池文件压缩+加密后放到发布目录，供公开仓库安全分发。

加密方案：gzip 压缩 → Fernet(AES-128-CBC + HMAC) 加密。
密钥来自环境变量 POOL_CRYPT_KEY；未设置时按明文输出并显式警告。
消费端用 tools/fetch_pool.py 拉取并解密。
"""
import argparse
import gzip
import os
import time


def encrypt_bytes(data: bytes, key: str) -> bytes:
    from cryptography.fernet import Fernet

    return Fernet(key.encode()).encrypt(gzip.compress(data, 9))


def main() -> None:
    ap = argparse.ArgumentParser(description="加密并整理代理池发布文件")
    ap.add_argument("--dir", default="data", help="池文件所在目录")
    ap.add_argument("--out", default="publish", help="发布输出目录")
    ap.add_argument("--files", default="pool.txt,pool.json", help="要发布的文件（逗号分隔）")
    args = ap.parse_args()

    key = os.environ.get("POOL_CRYPT_KEY", "").strip()
    os.makedirs(args.out, exist_ok=True)

    for name in [f.strip() for f in args.files.split(",") if f.strip()]:
        src = os.path.join(args.dir, name)
        if not os.path.exists(src):
            print(f"跳过（不存在）：{src}")
            continue
        with open(src, "rb") as f:
            data = f.read()
        if key:
            payload = encrypt_bytes(data, key)
            dst = os.path.join(args.out, name + ".enc")
            tag = "已加密"
        else:
            payload = data
            dst = os.path.join(args.out, name)
            tag = "警告：未设置 POOL_CRYPT_KEY，按明文发布！"
        with open(dst, "wb") as f:
            f.write(payload)
        print(f"发布：{dst}（{len(payload)} 字节）[{tag}]")

    with open(os.path.join(args.out, "updated_at.txt"), "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()) + "\n")


if __name__ == "__main__":
    main()
