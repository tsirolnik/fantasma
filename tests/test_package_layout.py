import unittest

from fantasma._compat import import_module_path
from fantasma.commands import tools
from fantasma.memory.mechanisms import FirstFinderBonus, build_mechanisms


class PackageLayoutTests(unittest.TestCase):
    def test_pre_0_3_module_paths_resolve_inside_package(self):
        self.assertIs(import_module_path('memory.mechanisms').FirstFinderBonus, FirstFinderBonus)
        built = build_mechanisms([{'class': 'memory.mechanisms:FirstFinderBonus'}])
        self.assertIsInstance(built[0], FirstFinderBonus)

    def test_unknown_modules_still_fail(self):
        with self.assertRaises(ModuleNotFoundError):
            import_module_path('memory.does_not_exist')
        with self.assertRaises(ModuleNotFoundError):
            import_module_path('not_a_fantasma_package.mod')

    def test_bundled_dockerfile_builds_from_stdin(self):
        self.assertTrue(tools.DOCKERFILE.is_file())
        self.assertEqual(tools.build_command('img:tag'), ['docker', 'build', '-t', 'img:tag', '-'])


if __name__ == '__main__':
    unittest.main()
