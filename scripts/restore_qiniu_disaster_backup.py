#!/usr/bin/env python3
"""Download and decrypt a Love Moments disaster bundle without touching live data."""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import tarfile
import sys
from io import BytesIO
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from love_moments.config import load_env
from love_moments.storage import QiniuKodoMediaStorage

def safe_extract(archive: tarfile.TarFile, output: Path) -> None:
    root = output.resolve()
    members = archive.getmembers()
    for member in members:
        if member.issym() or member.islnk() or member.isdev():
            raise ValueError("灾备包包含不安全链接或设备文件")
        target = (root / member.name).resolve()
        if target != root and root not in target.parents:
            raise ValueError("灾备包包含不安全路径")
    archive.extractall(root, members=members, filter="data")


def restore(object_key: str, output: Path) -> Path:
    load_env(ROOT / ".env")
    secret = os.getenv("BACKUP_ENCRYPTION_KEY", "").strip()
    if len(secret) < 32:
        raise ValueError("缺少有效的 BACKUP_ENCRYPTION_KEY")
    storage = QiniuKodoMediaStorage(
        os.getenv("QINIU_ACCESS_KEY", ""), os.getenv("QINIU_SECRET_KEY", ""),
        os.getenv("QINIU_BUCKET", ""), os.getenv("QINIU_S3_REGION", ""),
        endpoint=os.getenv("QINIU_S3_ENDPOINT", ""),
    )
    encrypted = storage.get(object_key)
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    try:
        payload = Fernet(key).decrypt(encrypted)
    except InvalidToken as exc:
        raise ValueError("灾备解密失败：密钥不匹配或文件已损坏") from exc
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    if os.name != "nt":
        output.chmod(0o700)
    with tarfile.open(fileobj=BytesIO(payload), mode="r:gz") as archive:
        safe_extract(archive, output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="下载并解密七牛云恋屿灾备（不会覆盖在线数据）")
    parser.add_argument("object_key", help="后台维护摘要中的七牛云对象键")
    parser.add_argument("output", type=Path, help="必须尚不存在的恢复演练目录")
    args = parser.parse_args()
    print(restore(args.object_key, args.output))


if __name__ == "__main__":
    main()
