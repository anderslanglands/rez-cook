import unittest
from package_list import PackageList, PackageRequest


class TestPackageList(unittest.TestCase):
    def test_conflicts_with(self):
        a = PackageList(
            [PackageRequest(r) for r in ["platform-windows", "arch-AMD64", "vs"]]
        )
        b = PackageList(
            [PackageRequest(r) for r in ["platform-windows", "arch-AMD64", "vs-2017"]]
        )
        c = PackageList(
            [PackageRequest(r) for r in ["platform-windows", "arch-AMD64", "vs-2019"]]
        )
        c = PackageList(
            [PackageRequest(r) for r in ["platform-windows", "arch-AMD64", "vs-2018+"]]
        )

        self.assertFalse(a.has_conflicts_with(b))
        self.assertTrue(b.has_conflicts_with(c))
        self.assertTrue(b.has_conflicts_with(d))
        self.assertFalse(a.has_conflicts_with(b))


if __name__ == "__main__":
    unittest.main()
