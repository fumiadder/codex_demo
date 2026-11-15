import glob
import os
import re
import unittest

from scripts import package_release


class PackageReleaseTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.root_modules = {
            os.path.basename(path)
            for path in glob.glob(os.path.join(self.root, "*.py"))
        }
        self.assertTrue(self.root_modules)

    def test_every_root_runtime_module_is_packaged(self):
        # A new root module imported by the server must be added to the release
        # allowlist, otherwise the immutable production image starts without it
        # and crashes on import. This guard keeps the two lists in lockstep.
        missing = self.root_modules - set(package_release.ALLOW_FILES)
        self.assertEqual(
            missing,
            set(),
            f"根模块未加入发布清单 ALLOW_FILES，生产镜像会缺失：{sorted(missing)}",
        )

    def test_allowlist_entries_exist(self):
        for name in package_release.ALLOW_FILES:
            self.assertTrue(
                os.path.isfile(os.path.join(self.root, name)),
                f"发布清单引用了不存在的文件：{name}",
            )

    def test_dockerfile_copies_every_root_runtime_module(self):
        # The image build uses its own explicit COPY list; a module present in
        # the tarball but missing here still crashes the container at startup.
        with open(os.path.join(self.root, "Dockerfile"), encoding="utf-8") as handle:
            dockerfile = handle.read()
        copy_lines = [
            line for line in dockerfile.splitlines()
            if line.startswith("COPY ") and line.rstrip().endswith("/app/")
        ]
        self.assertTrue(copy_lines, "Dockerfile 中未找到应用模块 COPY 指令")
        copied = set(" ".join(copy_lines).split())
        missing = {m for m in self.root_modules if m not in copied}
        self.assertEqual(
            missing,
            set(),
            f"根模块未加入 Dockerfile COPY，容器启动会 ImportError：{sorted(missing)}",
        )

    def test_render_build_compiles_every_root_runtime_module(self):
        with open(os.path.join(self.root, "render.yaml"), encoding="utf-8") as handle:
            render = handle.read()
        match = re.search(r"buildCommand:\s*(.+)", render)
        self.assertIsNotNone(match, "render.yaml 缺少 buildCommand")
        command = match.group(1)
        missing = {m for m in self.root_modules if m not in command}
        self.assertEqual(
            missing,
            set(),
            f"根模块未加入 render.yaml 编译命令，测试站会漏模块：{sorted(missing)}",
        )


if __name__ == "__main__":
    unittest.main()
