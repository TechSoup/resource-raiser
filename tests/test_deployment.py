"""Static deployment contracts that must stay aligned with the async serving design."""
import importlib.util
import os
from pathlib import Path
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as stream:
        return stream.read()


class DeploymentTests(unittest.TestCase):
    def test_native_startup_requires_verified_release_and_one_worker_per_service(self):
        startup = read("deploy/start-webapp.sh")
        self.assertIn("registry/index.py verify --release", startup)
        self.assertEqual(startup.count("--workers 1"), 2)
        self.assertNotIn("tools/build_registry_release.py", startup)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "Dockerfile")))

    def test_app_service_is_native_python_non_sticky_and_health_checked(self):
        bicep = read("deploy/main.bicep")
        self.assertIn("clientAffinityEnabled: false", bicep)
        self.assertIn("healthCheckPath: '/healthz'", bicep)
        self.assertIn("linuxFxVersion: 'PYTHON|3.13'", bicep)
        self.assertIn("appCommandLine: 'bash deploy/start-webapp.sh'", bicep)
        self.assertIn("SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'", bicep)
        self.assertIn("WEBAPP_MAX_INSTANCES", bicep)
        self.assertNotIn("redis", bicep.lower())
        self.assertNotIn("docker", bicep.lower())
        self.assertNotIn("containerregistry", bicep.lower())
        self.assertNotIn("WEBSITE_RUN_FROM_PACKAGE", bicep)
        self.assertNotIn("CpuPercentage", bicep)
        self.assertIn("metricName: 'Requests'", bicep)

    def test_scale_ceiling_drives_both_autoscale_and_sec_pacing(self):
        bicep = read("deploy/main.bicep")
        self.assertIn("maximum: string(maxInstances)", bicep)
        self.assertIn("WEBAPP_MAX_INSTANCES: string(maxInstances)", bicep)
        self.assertIn("union(secretSettings, fixedSettings)", bicep)
        self.assertIn("SEC_FLEET_REQUESTS_PER_SECOND", bicep)

    def test_zip_packager_includes_generated_release_but_not_local_secrets(self):
        path = os.path.join(ROOT, "deploy", "package_webapp.py")
        spec = importlib.util.spec_from_file_location("package_webapp", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        files = module.archive_files(Path(ROOT))
        self.assertIn("registry/current/manifest.json", files)
        self.assertTrue(any(name.startswith("sources/census/") for name in files))
        self.assertIn("deploy/start-webapp.sh", files)
        self.assertNotIn("Dockerfile", files)
        self.assertNotIn("set_keys.sh", files)
        self.assertFalse(any(name.startswith(".venv/") for name in files))


if __name__ == "__main__":
    unittest.main()
