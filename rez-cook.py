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

class PackageRequest:
    def __init__(self, req: str):
        '''
        Parse a package request of the form 'pkg-version'
        TODO: variants
        '''
        toks = req.split('-')
        if len(toks) == 2:
            self.name = toks[0]
            self.version = VersionRange(toks[1])
        else:
            self.name = req
            self.version = VersionRange('')


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


def find_recipe(requested_pkg, requested_variants, installed_selections):
    result = sp.run(
        [
            "rez-search",
            requested_pkg,
            "--format",
            "{name}:{version}:{variants}:{requires}:{build_requires}",
        ],
        stdout=sp.PIPE,
        stderr=sp.PIPE,
    )
    if not b"No matching" in result.stderr:
        name, version, vstr, _, _ = next(
            (
                x.strip().split(":")
                for x in reversed(result.stdout.decode("utf-8").splitlines())
            ),
            None,
        )
        variants = [parse_variants(v) for v in filter(None, vstr.split("["))]
        # print(
        #     f"Selected {name}-{version} {variants} for {requested_pkg} {requested_variants}"
        # )
        if ((name, version, variants)) not in installed_selections:
            installed_selections.append((name, version, variants))

        return []

    result = sp.run(
        [
            "rez-search",
            requested_pkg,
            "--paths",
            RECIPES_PATH,
            "--format",
            "{name}:{version}:{variants}:{requires}:{build_requires}",
        ],
        stdout=sp.PIPE,
        stderr=sp.PIPE,
    )

    if b"No matching" in result.stderr:
        print(f"{pkg} not found in recipes.")
        sys.exit(1)

    to_cook = []
    for line in reversed(result.stdout.decode("utf-8").splitlines()):
        name, version, vstr, requires_str, build_requires_str = line.strip().split(":")
        variants = [parse_variants(v) for v in filter(None, vstr.split("["))]
        # print(f"Found recipe for {name}-{version} {variants}")

        requires = filter(None, requires_str.split(" "))
        build_requires = filter(None, build_requires_str.split(" "))

        requires = filter(None, requires_str.split(" "))
        build_requires = filter(None, build_requires_str.split(" "))

        # Check whether any of the set of variants provided matches our variants list
        for variant in variants:
            if all(v in variant for v in requested_variants):
                # recurisvely find all dependencies
                # print(
                #     f"Selected {name}-{version} {variant} for {requested_pkg} {requested_variants}"
                # )
                for req in itertools.chain(requires, build_requires):
                    req_deps_list = find_recipe(
                        req, requested_variants, installed_selections
                    )

                    # Add each sub-dependency to the list if we don't have it already
                    # TODO: use versioning correctly here - need to find a single
                    # version for each package family that satisfies all requires
                    for dep_info in req_deps_list:
                        if dep_info is not None and dep_info not in to_cook:
                            to_cook.append(dep_info)

                # add this variant
                # TODO: again, need to match versions properly
                this_req_info = (name, version, variant)
                if this_req_info not in to_cook:
                    to_cook.append(this_req_info)

    return to_cook


def build_recipe(recipe, no_cleanup, verbose_build):
    # First, copy the resolved package.py to the build area
    name, version, variant = recipe
    pkg_subpath = os.path.join(name, version, *variant)
    staging_path = os.path.join(COOK_PATH, name, version)
    staging_package_py_path = os.path.join(staging_path, "package.py")
    recipe_package_root = os.path.join(RECIPES_PATH, pkg_subpath)
    recipe_package_py_path = os.path.join(recipe_package_root, "package.py")

    # blow away anything in the staging path already
    shutil.rmtree(staging_path, ignore_errors=True)
    os.makedirs(staging_path)
    shutil.copyfile(recipe_package_py_path, staging_package_py_path)

    # load the package and run pre_cook() if it's defined
    old_dir = os.getcwd()
    mod = load_module(f"{name}-{version}-{variant}", staging_package_py_path)
    install_path = os.path.join(LOCAL_PACKAGE_PATH, pkg_subpath)
    build_path = os.path.join(staging_path, "build", *variant)
    os.makedirs(build_path)
    setattr(mod, "name", name)
    setattr(mod, "version", version)
    setattr(mod, "variant", variant)
    setattr(mod, "install_path", install_path)
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
            shutil.rmtree(staging_path)
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
            shutil.rmtree(install_path)
            raise e
        finally:
            os.chdir(old_dir)
            if not no_cleanup:
                shutil.rmtree(staging_path)
    else:
        try:
            print(f"Building {name}-{version} {variant}")
            if verbose_build:
                sp.run(["rez-build", "--install"], cwd=staging_path, check=True)
            else:
                sp.run(
                    ["rez-build", "--install"],
                    cwd=staging_path,
                    check=True,
                    stderr=sp.PIPE,
                    stdout=sp.PIPE,
                )
        except sp.CalledProcessError as e:
            if not verbose_build:
                print(e.stderr.decode('utf-8'))
            print(f"\nBuild failed for {name}-{version} {variant}: {e}")
            raise e
        finally:
            os.chdir(old_dir)
            if not no_cleanup:
                shutil.rmtree(staging_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch recipes and build packages from them"
    )
    parser.add_argument(
        "package",
        metavar="PKG",
        type=str,
        nargs="+",
        help="Package versions to fetch and build, e.g 'openexr', 'usd-20.08'",
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

    recipes_to_cook = []
    installed_selections = []
    for pkg in args.package:
        this_recipes = find_recipe(pkg, REQUESTED_VARIANTS, installed_selections)
        for recipe in this_recipes:
            if recipe not in recipes_to_cook:
                recipes_to_cook.append(recipe)

    if not recipes_to_cook:
        print(f"Nothing to do for {pkg} {REQUESTED_VARIANTS}")

    print("Package selection:")
    print("- Already installed")
    for name, version, variant in installed_selections:
        print(f"    {name:>16} {version:>8} {variant}")

    print()

    print("- Cooking")
    for name, version, variant in recipes_to_cook:
        print(f"    {name:>16} {version:>8} {variant}")
    
    print()

    if not args.dry_run:
        # build the flattened tree of recipes in depth-first order
        for recipe in recipes_to_cook:
            build_recipe(recipe, args.no_cleanup, args.verbose_build)
