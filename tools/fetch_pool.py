#!/usr/bin/env python3
"""消费端工具：从公开仓库拉取加密代理池并解密，或解密本地密文文件。

用法示例：
    # 从仓库拉取并解密为 pool.txt
    python tools/fetch_pool.py --repo yourname/yourrepo --out pool.txt

    # 解密本地已下载的密文文件
    python tools/fetch_pool.py --in-file pool.txt.enc --out pool.txt

密钥：--key 参数或环境变量 POOL_CRYPT_KEY。
"""
import argparse
import gzip
import os
import sys
import urllib.request


def decrypt_bytes(payload: bytes, key: str) -> bytes:
    from cryptography.fernet import Fernet

    return gzip.decompress(Fernet(key.encode()).decrypt(payload))


def main() -> None:
    ap = argparse.ArgumentParser(description="拉取/解密代理池")
    ap.add_argument("--repo", help="owner/repo（从 raw.githubusercontent.com 拉取）")
    ap.add_argument("--branch", default="dist", help="分支名，默认 dist")
    ap.add_argument("--file", default="pool.txt.enc", help="仓库内文件名，默认 pool.txt.enc")
    ap.add_argument("--url", help="直接指定完整 URL（优先于 --repo）")
    ap.add_argument("--in-file", dest="in_file", help="从本地密文文件解密（不联网）")
    ap.add_argument("--out", default="pool.txt", help="解密输出文件，默认 pool.txt")
    ap.add_argument("--key", help="Fernet 密钥，默认读环境变量 POOL_CRYPT_KEY")
    args = ap.parse_args()

    key = (args.key or os.environ.get("POOL_CRYPT_KEY", "")).strip()
    if not key:
        sys.exit("缺少密钥：用 --key 传入或设置环境变量 POOL_CRYPT_KEY")

    if args.in_file:
        with open(args.in_file, "rb") as f:
            payload = f.read()
    else:
        if args.url:
            url = args.url
        elif args.repo:
            url = f"https://raw.githubusercontent.com/{args.repo}/{args.branch}/{args.file}"
        else:
            ap.error("需要 --repo / --url / --in-file 三者之一")
            return
        print(f"下载：{url}")
        with urllib.request.urlopen(url, timeout=30) as resp:
            payload = resp.read()

    try:
        data = decrypt_bytes(payload, key)
    except Exception as exc:
        sys.exit(f"解密失败（密钥不对或文件非加密格式？）：{exc}")

    with open(args.out, "wb") as f:
        f.write(data)
    count = sum(1 for line in data.decode("utf-8", "replace").splitlines() if line.strip())
    print(f"已写入 {args.out}（{count} 行）")


if __name__ == "__main__":
    main()
