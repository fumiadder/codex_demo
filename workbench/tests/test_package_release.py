import glob
import os
import unittest

from scripts import package_release


class PackageReleaseTests(unittest.TestCase):
    def test_every_root_runtime_module_is_packaged(self):
        # A new root module imported by the server must be added to the release
        # allowlist, otherwise the immutable production image starts without it
        # and crashes on import. This guard keeps the two lists in lockstep.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        root_modules = {
            os.path.basename(path)
            for path in glob.glob(os.path.join(root, "*.py"))
        }
        self.assertTrue(root_modules)
        missing = root_modules - set(package_release.ALLOW_FILES)
        self.assertEqual(
            missing,
            set(),
            f"根模块未加入发布清单 ALLOW_FILES，生产镜像会缺失：{sorted(missing)}",
        )

    def test_allowlist_entries_exist(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in package_release.ALLOW_FILES:
            self.assertTrue(
                os.path.isfile(os.path.join(root, name)),
                f"发布清单引用了不存在的文件：{name}",
            )


if __name__ == "__main__":
    unittest.main()
