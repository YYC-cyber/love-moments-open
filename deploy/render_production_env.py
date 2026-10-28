#!/usr/bin/env python3
"""Render a private production env file without printing any secret values."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path


KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
DOMAIN_PATTERN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    order: list[str] = []
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not KEY_PATTERN.fullmatch(key):
            raise ValueError(f"环境变量名无效：{key}")
        if key not in values:
            order.append(key)
        values[key] = value.strip()
    return order, values


def render(source: Path, output: Path, domain: str, public_ipv4: str) -> list[str]:
    domain = domain.strip().lower().rstrip(".")
    if not DOMAIN_PATTERN.fullmatch(domain):
        raise ValueError("HTTPS 域名格式无效")
    if not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", public_ipv4.strip()):
        raise ValueError("服务器 IPv4 格式无效")

    order, values = read_env(source)
    public_url = f"https://{domain}"
    overrides = {
        "APP_HOST": "0.0.0.0",
        "APP_PORT": "52013",
        "DEMO_MODE": "false",
        "REGISTRATION_MODE": "closed",
        "PUBLIC_BASE_URL": public_url,
        "CORS_ALLOWED_ORIGINS": public_url,
        "COOKIE_SECURE": "true",
        "DATABASE_PATH": "/srv/love-moments/data/love_moments.db",
        "MEDIA_ROOT": "/srv/love-moments/media",
        "BACKUP_ROOT": "/srv/love-moments/backups",
        "BACKUP_MIRROR_ROOT": "",
        "MAINTENANCE_STATE_ROOT": "/srv/love-moments/maintenance",
        "LOG_ROOT": "/srv/love-moments/logs",
        "LOCAL_ADMIN_NOTIFICATIONS": "false",
        "WATCHDOG_AUTO_RESTART": "false",
        "WEB_PUSH_VAPID_SUBJECT": public_url,
        "TENCENT_CLOUD_DEPLOYMENT_STATUS": "deployed",
        "TENCENT_CLOUD_REGION": "ap-shanghai",
        "TENCENT_CLOUD_PUBLIC_IPV4": public_ipv4.strip(),
        "TENCENT_CLOUD_PRIVATE_IPV4": "",
        "TENCENT_CLOUD_APP_PORT": "52013",
        "TENCENT_CLOUD_TEST_BASE_URL": public_url,
        "TENCENT_CLOUD_OS": "Ubuntu Server 22.04 LTS 64bit",
        "TENCENT_CLOUD_DOMAIN": domain,
        "TENCENT_CLOUD_HTTPS_ENABLED": "true",
    }
    for key, value in overrides.items():
        if key not in values:
            order.append(key)
        values[key] = value

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text("".join(f"{key}={values[key]}\n" for key in order), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(output)
    os.chmod(output, 0o600)

    watched = (
        "LLM_API_KEY",
        "AMAP_WEB_KEY",
        "SHENGWANG_APP_ID",
        "SHENGWANG_APP_CERTIFICATE",
        "QINIU_ACCESS_KEY",
        "QINIU_SECRET_KEY",
    )
    return [key for key in watched if not values.get(key, "").strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="生成恋屿云服务器私有环境配置")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--public-ipv4", required=True)
    args = parser.parse_args()
    missing = render(args.source.resolve(), args.output.resolve(), args.domain, args.public_ipv4)
    print(f"已生成生产环境配置：{args.output.resolve()}")
    if missing:
        print("仍未配置的可选能力：" + ", ".join(missing))


if __name__ == "__main__":
    main()
