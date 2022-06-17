import sys, subprocess as sp, platform, re, os, tempfile, shutil, itertools, argparse, traceback
from pathlib import Path
from rez.vendor.version.requirement import VersionedObject
from rez.vendor.version.version import VersionRange

HOME = str(Path.home())
RECIPES_PATH = f"{HOME}/code/rez-recipes"
LOCAL_PACKAGE_PATH = f"{HOME}/packages"

PLATFORM = platform.system().lower()
ARCH = platform.machine()
PLATFORM_VARIANTS = [f"platform-{PLATFORM}", f"arch-{ARCH}"]

COOK_PATH = os.path.join(tempfile.gettempdir(), "rez-cook")

REQUESTED_VARIANTS = PLATFORM_VARIANTS  # + args

class RecipeNotFound(Exception):
    pass

class DependencyConflict(Exception):
    pass


class PackageRequest:
    def __init__(self, req: str):
        """
        Parse a package request of the form 'pkg-version'
        TODO: variants
        """
        toks = req.split("-")
        if len(toks) == 2:
            self.name = toks[0]
            self.version = VersionRange(toks[1])
        else:
            self.name = req
            self.version = VersionRange("")

    def __str__(self) -> str:
        if str(self.version):
            return f"{self.name}-{self.version}"
        else:
            return self.name


def load_module(name, path):
    import importlib.util
    import sys

    module_name = f"package-{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def parse_variants(vstr):
    rgx = r"PackageRequest\('([\w-]+)'\)"
    return [match.group(1) for match in re.finditer(rgx, vstr)]


def find_recipe(
    pkg_req: PackageRequest, requested_variants: "list[str]", installed_selections
):
    # First search the installed packages to see if one satisfies the request already
    result = sp.run(
        [
            "rez-search",
            str(pkg_req),
            "--format",
            "{name}:{version}:{variants}:{requires}:{build_requires}",
        ],
        stdout=sp.PIPE,
        stderr=sp.PIPE,
    )

    # FIXME: This fails when a broken package is found (i.e. dir there but no pacakge file)
    if not b"No matching" in result.stderr:
        # print(f"find('{pkg_req}').stdout:\n{result.stdout}")
        name, version, vstr, _, _ = next(
            (
                x.strip().split(":")
                for x in reversed(result.stdout.decode("utf-8").splitlines())
            ),
            None,
        )
        variants = [parse_variants(v) for v in filter(None, vstr.split("["))]
        if ((name, version, variants)) not in installed_selections:
            installed_selections.append((name, version, variants))

        return {}

    # If we don't have one installed, search the recipes
    result = sp.run(
        [
            "rez-search",
            str(pkg_req),
            "--paths",
            RECIPES_PATH,
            "--format",
            "{name}:{version}:{variants}:{requires}:{build_requires}",
        ],
        stdout=sp.PIPE,
        stderr=sp.PIPE,
    )

    if b"No matching" in result.stderr:
        raise RecipeNotFound(f"No recipe satisfying {pkg_req} found in {RECIPES_PATH}")

    # Now if we've found some recipes to build, go through them, latest first and 
    # make sure we've got/can build their dependencies
    to_cook = {}
    for line in reversed(result.stdout.decode("utf-8").splitlines()):
        name, version_str, variant_str, requires_str, build_requires_str = line.strip().split(":")
        version = VersionRange(version_str)
        variants = [parse_variants(v) for v in filter(None, variant_str.split("["))]
        # print(f"Found recipe for {name}-{version} {variants}")

        requires = filter(None, requires_str.split(" "))
        build_requires = filter(None, build_requires_str.split(" "))

        # Check whether any of the set of variants provided matches our variants list
        for variant in variants:
            if all(v in variant for v in requested_variants):
                # print(
                #     f"Selected {name}-{version} {variant} for {pkg_req} {requested_variants}"
                # )
                # recurisvely find all dependencies
                try:
                    for req_str in itertools.chain(requires, build_requires):
                        req = PackageRequest(req_str)
                        req_deps_list = find_recipe(
                            req, requested_variants, installed_selections
                        )

                        # Add each sub-dependency to the list if we don't have it already
                        # TODO: use versioning correctly here - need to find a single
                        # version for each package family that satisfies all requires
                        for dep_name, (dep_version, dep_variants) in req_deps_list.items():
                            # print(f"dep_name={dep_name} dep_version={dep_version} dep_variants={dep_variants}")

                            if dep_name in to_cook.keys():
                                # have already selected a range of this dependency, try
                                # and combine them
                                existing_range = to_cook[dep_name][0]
                                if dep_version.intersects(existing_range):
                                    # We can combine by narrowing the dependencies
                                    # todo - extend variants here?
                                    new_version = dep_version.intersection(existing_range)
                                    # print(f"Narrowing {dep_name} to {new_version}")
                                    to_cook[dep_name] = (new_version, dep_variants)
                                else:
                                    # no intersection - can't resolve
                                    raise DependencyConflict(f"{dep_name}-{dep_version} <--!--> {dep_name}-{existing_range}")
                            else:
                                to_cook[dep_name] = (dep_version, dep_variants)
                except DependencyConflict as e:
                    # FIXME: Why doesn't this chain properly?
                    raise DependencyConflict(f"Resolving {pkg_req}: {e}") from e
                except RecipeNotFound as e:
                    raise e

                # If we get here, all the dependencies are available, so add this variant
                # TODO: again, need to match versions properly
                this_req_info = (version, variant)
                if name not in to_cook:
                    to_cook[name] = this_req_info
                break

    return to_cook

def rmtree_for_real(path):
    import shutil

    # Isn't Windows wonderful? You cannot delete a hidden file, which means
    # you can't delete any of the .git stuff lots of releases check out when
    # building without first removing those attributes
    if os.name == "nt":
        def yes_i_really_mean_it(func, dpath, exc_info):
            # If it's a FileNotFound then just ignore it
            # Why use one error code when you can have two at twice the price?
            if exc_info[1].winerror in [2, 3]:
                return

            os.system(f"attrib -r -h -s {dpath}")
            func(dpath)

        shutil.rmtree(path, onerror=yes_i_really_mean_it)
    else:
        shutil.rmtree(path)


def build_recipe(recipe, no_cleanup, verbose_build):
    # First, copy the resolved package.py to the build area
    name, version_range, variant = recipe
    version = str(version_range)
    pkg_subpath = os.path.join(name, version, *variant)
    staging_path = os.path.join(COOK_PATH, name, version)
    staging_package_py_path = os.path.join(staging_path, "package.py")
    recipe_package_root = os.path.join(RECIPES_PATH, pkg_subpath)
    recipe_package_py_path = os.path.join(recipe_package_root, "package.py")

    # blow away anything in the staging path already
    rmtree_for_real(staging_path)
    os.makedirs(staging_path)
    shutil.copyfile(recipe_package_py_path, staging_package_py_path)

    # load the package and run pre_cook() if it's defined
    old_dir = os.getcwd()
    mod = load_module(f"{name}-{version}-{variant}", staging_package_py_path)
    install_path = os.path.join(LOCAL_PACKAGE_PATH, pkg_subpath)
    install_root = os.path.join(LOCAL_PACKAGE_PATH, name, version)
    build_path = os.path.join(staging_path, "build", *variant)
    os.makedirs(build_path)
    setattr(mod, "name", name)
    setattr(mod, "version", version)
    setattr(mod, "variant", variant)
    setattr(mod, "install_path", install_path)
    setattr(mod, "install_root", install_root)
    setattr(mod, "build_path", build_path)
    setattr(mod, "root", staging_path)

    try:
        os.chdir(staging_path)
        if "pre_cook" in dir(mod):
            mod.pre_cook()
    except Exception as e:
        print(f"Pre-cooking failed for {name}-{version} {variant}: {e}")
        traceback.print_exc()
        if not no_cleanup:
            rmtree_for_real(staging_path)
        raise e
    finally:
        os.chdir(old_dir)

    # Now do the actual build
    # Use cook() if it's available, otherwise use the build_command
    if "cook" in dir(mod):
        try:
            os.chdir(build_path)
            os.makedirs(install_path)
            print(f"Cooking {name}-{version} {variant}")
            mod.cook()
        except Exception as e:
            print(f"\nCook failed for {name}-{version} {variant}: {e}")
            rmtree_for_real(install_path)
            raise e
        finally:
            os.chdir(old_dir)
            if not no_cleanup:
                rmtree_for_real(staging_path)
    else:
        try:
            cmd = ["rez-build", "--install"]
            if 'build_args' in dir(mod) and isinstance(mod.build_args, list):
                cmd += ['--build-args'] + mod.build_args

            print(f"Building {name}-{version} {variant}")
            if verbose_build:
                sp.run(cmd, cwd=staging_path, check=True)
            else:
                sp.run(
                    cmd,
                    cwd=staging_path,
                    check=True,
                    stderr=sp.PIPE,
                    stdout=sp.PIPE,
                )
        except sp.CalledProcessError as e:
            if not verbose_build:
                print(e.stderr.decode("utf-8"))
            print(f"\nBuild failed for {name}-{version} {variant}: {e}")
            raise e
        finally:
            os.chdir(old_dir)
            if not no_cleanup:
                rmtree_for_real(staging_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch recipes and build packages from them"
    )
    parser.add_argument(
        "package",
        metavar="PKG",
        type=str,
        help="Package and version to fetch and build, e.g 'openexr', 'usd-20.08'",
    )
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help="Don't actually build the recipes, just print the cook list",
    )
    parser.add_argument(
        "-nc",
        "--no-cleanup",
        action="store_true",
        help="Don't clean up temporary directories on failure",
    )
    parser.add_argument(
        "-bb",
        "--verbose-build",
        action="store_true",
        help="Print all build output",
    )
    args = parser.parse_args()

    pkg_req = PackageRequest(args.package)
    installed_selections = []
    recipes_to_cook = find_recipe(pkg_req, REQUESTED_VARIANTS, installed_selections)

    if not recipes_to_cook:
        print(f"Nothing to do for {pkg_req} {REQUESTED_VARIANTS}")

    print("Package selection:")
    print("- Already installed")
    for name, version, variant in installed_selections:
        print(f"    {name:>16} {str(version):>8} {variant}")

    print()

    if recipes_to_cook:
        print("- Cooking")
        for name, (version, variant) in recipes_to_cook.items():
            print(f"    {name:>16} {str(version):>8} {variant}")

        print()

    if not args.dry_run:
        # build the flattened tree of recipes in depth-first order
        for name, (version, variant) in recipes_to_cook.items():
            build_recipe((name, version, variant), args.no_cleanup, args.verbose_build)
