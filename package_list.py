from rez.vendor.version.version import VersionRange
from rez.utils.formatting import PackageRequest

class VersionConflict(Exception):
    pass

class PackageList:
    def __init__(self, pkgs: "list[PackageRequest]"):
        if not pkgs:
            self._pkgs = []
        elif isinstance(pkgs[0], str):
            self._pkgs = [PackageRequest(x) for x in pkgs]
        elif isinstance(pkgs[0], PackageRequest):
            self._pkgs = [x for x in pkgs]
        else:
            raise RuntimeError(
                f"PackageList constructor takes a list of str or PackageRequest, not {type(pkgs[0])}"
            )

    def has_conflicts_with(self, rhs):
        for p in self._pkgs:
            for r in rhs._pkgs:
                if p.conflicts_with(r):
                    return True

        return False

    def is_empty(self):
        return len(self._pkgs) != 0

    def get_conflicts(self, rhs):
        conflicts = []
        for p in self._pkgs:
            for r in rhs._pkgs:
                if p.conflicts_with(r):
                    conflicts.append[(p, r)]

        return conflicts

    def conflicts_with(self, r: PackageRequest):
        for p in self._pkgs:
            if p.conflicts_with(r):
                return True

        return False

    def additive_merged(self, rhs: "PackageList") -> "PackageList":
        """
        Merge self with PackageList rhs, adding any packages from rhs that 
        are not present in self
        """
        d = dict(zip([p.name for p in self._pkgs], self._pkgs))

        for p in rhs:
            if p.name in d.keys():
                m = p.merged(d[p.name])
                if m is None:
                    raise VersionConflict(f"Cannot merge {p} with {d[p.name]}")
                d[p.name] = PackageRequest(f"{m.name}-{m.range}")
            else:
                d[p.name] = p

        return PackageList([p for _, p in d.items()])
              

    def merged(self, rhs: "PackageList") -> "PackageList":
        """
        Merge self with PackageList rhs, ignoring any packages from rhs that 
        are not present in both lists
        """
        d = dict(zip([p.name for p in self._pkgs], self._pkgs))
        result = []
        for p in rhs:
            if p.name in d.keys():
                m = p.merged(d[p.name])
                if m is None:
                    raise VersionConflict(f"Cannot merge {p} with {d[p.name]}")
                result.append(PackageRequest(f"{m.name}-{m.range}"))

        return PackageList(result)


    def merged_into(self, rhs: "PackageList") -> "PackageList":
        """
        Merge self with PackageList rhs, ignoring any packages from rhs that 
        are not present in self, and preserving any packages in self that are not 
        present in rhs
        """
        d = dict(zip([p.name for p in rhs._pkgs], rhs._pkgs))
        result = []
        for p in self._pkgs:
            if p.name in d.keys():
                m = p.merged(d[p.name])
                if m is None:
                    raise VersionConflict(f"Cannot merge {p} with {d[p.name]}")
                result.append(PackageRequest(f"{m.name}-{m.range}"))
            else:
                result.append(p)

        return PackageList(result)


    def constrained(self, rhs: PackageRequest):
        result = []
        for p in self._pkgs:
            if rhs.name == p.name:
                result.append(PackageRequest(f"{p.name}-{p.range.intersection(rhs.range)}"))
            else:
                result.append(p)

        return PackageList(result)


    def add_constraint(self, rhs: PackageRequest):
        result = []
        found = False
        for p in self._pkgs:
            if rhs.name == p.name:
                m = p.merged(rhs)
                if m is None:
                    raise VersionConflict(f"Cannot merge {p} with {rhs}")

                result.append(PackageRequest(f"{m.name}-{m.range}"))
                found = True
            else:
                result.append(p)
        
        if not found:
            result.append(rhs)

        self._pkgs = PackageList(result)

              

    def __iter__(self):
        for pkg in self._pkgs:
            yield pkg

    def __str__(self):
        s = "["
        first = True
        for p in self._pkgs:
            if not first:
                s = f"{s}, "
            first = False

            if p.range.is_any():
                s = f"{s}\"{p.name}\""
            else:
                s = f"{s}\"{p.name}-{p.range}\""
        s += "]"
        return s

    def __len__(self):
        return len(self._pkgs)


    def __getitem__(self, i):
        return self._pkgs[i]

    def __add__(self, rhs):
        return PackageList(self._pkgs + rhs._pkgs)
