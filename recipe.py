from rez.vendor.version.version import VersionRange
from rez.utils.formatting import PackageRequest
from package_list import PackageList


class Recipe:
    def __init__(
        self,
        name: str,
        range: VersionRange,
        variant: PackageList,
        requires: PackageList,
        build_requires: PackageList,
        installed: bool,
    ):
        if range is None:
            self.pkg = PackageRequest(f"{name}")
        else:
            self.pkg = PackageRequest(f"{name}-{range}")
        self.variant = variant
        self.requires = requires
        self.build_requires = build_requires
        self.installed = installed

    def conflicts_with_package(self, rhs: PackageRequest) -> bool:
        if self.pkg.name == rhs.name and not self.pkg.range.intersects(rhs.range):
            return f"{self.pkg} <-!-> {rhs}"

        for v in [self.variant, self.requires, self.build_requires]:
            vc = v.has_conflicts_with(rhs)
            if vc:
                return vc

        return False

    def conflicts_with_package_list(self, rhs: PackageList) -> bool:
        for p in rhs:
            if self.pkg.name == p.name and not self.pkg.range.intersects(p.range):
                # print(f"      recipe {self.pkg} conflicts with {p}")
                return f"{self.pkg} <-!-> {p}"

            for v in [self.variant, self.requires, self.build_requires]:
                vc = v.has_conflicts_with(rhs)
                if vc:
                    return vc

        return False

    def __str__(self):
        namever = f"{self.pkg.name}-{self.pkg.range}"
        return f"{namever}/{'/'.join([str(p) for p in self.variant])}"

