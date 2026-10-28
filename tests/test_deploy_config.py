import os
import tempfile
import unittest
from pathlib import Path

from deploy.render_production_env import read_env, render


class ProductionDeploymentTests(unittest.TestCase):
    def test_render_production_env_keeps_secrets_and_overrides_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / ".env"
            output = root / "production.env"
            source.write_text(
                "LLM_API_KEY=secret-value\n"
                "APP_HOST=127.0.0.1\n"
                "PUBLIC_BASE_URL=http://localhost:18765\n"
                "CORS_ALLOWED_ORIGINS=http://localhost:18765\n",
                encoding="utf-8",
            )

            missing = render(
                source,
                output,
                "love-moments.203-0-113-10.sslip.io",
                "203.0.113.10",
            )
            _, values = read_env(output)

            self.assertEqual(values["LLM_API_KEY"], "secret-value")
            self.assertEqual(values["APP_HOST"], "0.0.0.0")
            self.assertEqual(values["APP_PORT"], "52013")
            self.assertEqual(values["COOKIE_SECURE"], "true")
            self.assertEqual(values["DATABASE_PATH"], "/srv/love-moments/data/love_moments.db")
            self.assertEqual(
                values["PUBLIC_BASE_URL"],
                "https://love-moments.203-0-113-10.sslip.io",
            )
            self.assertNotIn("LLM_API_KEY", missing)
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)

    def test_render_rejects_invalid_domain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / ".env"
            source.write_text("APP_PORT=18765\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                render(source, root / "production.env", "https://bad domain", "203.0.113.10")

    def test_render_accepts_public_ipv4_as_https_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / ".env"
            output = root / "production.env"
            source.write_text("APP_PORT=18765\n", encoding="utf-8")

            render(source, output, "203.0.113.10", "203.0.113.10")
            _, values = read_env(output)

            self.assertEqual(values["PUBLIC_BASE_URL"], "https://203.0.113.10")
            self.assertEqual(values["CORS_ALLOWED_ORIGINS"], "https://203.0.113.10")
            self.assertEqual(values["TENCENT_CLOUD_DOMAIN"], "203.0.113.10")

    def test_ip_nginx_template_keeps_application_private(self):
        project_root = Path(__file__).resolve().parents[1]
        template = (
            project_root / "deploy/nginx/love-moments-ip-https.conf.template"
        ).read_text(encoding="utf-8")

        self.assertIn("listen 443 ssl http2 default_server", template)
        self.assertIn("proxy_pass http://127.0.0.1:52013", template)
        self.assertNotIn("0.0.0.0:52013", template)

    def test_short_lived_ip_certificate_renewal_skips_certbot_random_sleep(self):
        project_root = Path(__file__).resolve().parents[1]
        service = (
            project_root / "deploy/systemd/love-moments-cert-renew.service"
        ).read_text(encoding="utf-8")

        self.assertIn("--no-random-sleep-on-renew", service)
        self.assertIn("systemctl reload nginx", service)


if __name__ == "__main__":
    unittest.main()
