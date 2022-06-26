from distutils.version import Version
import sys, subprocess as sp, platform, re, os, tempfile, shutil, itertools, argparse, traceback
from pathlib import Path
from urllib import request
from rez.vendor.version.version import VersionRange
from rez.utils.formatting import PackageRequest
from package_list import PackageList
import logging

LOG = logging.getLogger(__name__)

HOME = str(Path.home())
RECIPES_PATH = os.getenv("REZ_RECIPES_PATH") or f"{HOME}/code/rez-recipes"
LOCAL_PACKAGE_PATH = f"{HOME}/packages"

INSTALL_PACKAGE_PATH = LOCAL_PACKAGE_PATH
SEARCH_PACKAGE_PATH = LOCAL_PACKAGE_PATH

PLATFORM = platform.system().lower()
ARCH = platform.machine()
PLATFORM_VARIANT = [f"platform-{PLATFORM}", f"arch-{ARCH}"]

COOK_PATH = os.path.join(tempfile.gettempdir(), "rez-cook")

REQUESTED_VARIANT = PLATFORM_VARIANT  # + args


class RecipeNotFound(Exception):
    pass


class DependencyConflict(Exception):
    pass


def load_module(name, path, globals={}):
    import importlib.util
    import sys
    from rez.utils.sourcecode import early

    module_name = f"package-{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    setattr(mod, "early", early)

    for key, value in globals.items():
        setattr(mod, key, value)

    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def parse_variants(vstr):
    LOG.debug(f"Parsing variant {vstr}")
    rgx = r"PackageRequest\('([\w\.-]+)'\)"
    result = PackageList(
        [PackageRequest(match.group(1)) for match in re.finditer(rgx, vstr)]
    )
    LOG.debug(f"    Got {result}")
    return result


def conflicts_with_variant(pkg: PackageRequest, variant: "list[PackageRequest]"):
    for v in variant:
        if pkg.conflicts_with(v):
            return True

    return False


def variants_conflict(
    variant1: "list[PackageRequest]", variant2: "list[PackageRequest]"
):
    LOG.debug(f"v1: {variant1}, v2: {variant2}")
    for v1 in variant1:
        for v2 in variant2:
            if v1.conflicts_with(v2):
                return True

    return False


def find_recipe(
    pkg_req: PackageRequest,
    requested_variant: PackageList,
    installed_selections: list,
    package_search_path: str,
):
    LOG.debug("--------")
    LOG.debug(f'find_recipe("{pkg_req}", {requested_variant}, {installed_selections})')
    LOG.debug("--------")
    # First search the installed packages to see if one satisfies the request already
    # Set an environment variable to tell the package scripts that we're in a cook-search context
    # and what the variant we're looking for is
    sp_env = os.environ.copy()
    sp_env["REZ_COOK_VARIANT"] = " ".join(
        f"{r.name}-{r.range}" for r in requested_variant
    )
    result = sp.run(
        [
            "rez-search",
            str(pkg_req),
            "--format",
            "{name}:{version}:{variants}:{requires}:{build_requires}",
            "--paths",
            package_search_path,
        ],
        stdout=sp.PIPE,
        stderr=sp.PIPE,
        env=sp_env,
    )
    LOG.debug(f"rez-search returned:\n----\n{result.stdout}\n----")

    # FIXME: This fails when a broken package is found (i.e. dir there but no package file)
    if not b"No matching" in result.stderr and result.stdout:
        LOG.debug(f"find('{pkg_req}').stdout:\n{result.stdout}")
        for name, version, vstr, _, _ in [
            x.strip().split(":")
            for x in reversed(result.stdout.decode("utf-8").splitlines())
        ]:
            LOG.debug(f"checking installed {name}-{version} {vstr}")
            this_pkg = PackageRequest(f"{name}-{version}")
            # Check that this package isn't conflicting with the requested variant
            if conflicts_with_variant(this_pkg, requested_variant):
                LOG.debug(f"Conflicts")
                continue

            variants = [parse_variants(v) for v in filter(None, vstr.split("["))]

            LOG.debug(f"Parsed variants {[str(v) for v in variants]}")
            if len(variants) == 0:
                if ((name, version, variants)) not in installed_selections:
                    installed_selections.append((name, version, variants))
                    # don't want to add anything to the build
                    return {}
            else:
                for variant in variants:
                    if variants_conflict(variant, requested_variant):
                        continue

                    # all good, add it to the list
                    if ((name, version, variant)) not in installed_selections:
                        installed_selections.append((name, version, variant))

                    # don't want to add anything to the build
                    return {}

    # If we don't have one installed, search the recipes
    LOG.debug(f"No {pkg_req} installed. Searching for recipe...")
    sp_env = os.environ.copy()
    sp_env["REZ_COOK_VARIANT"] = " ".join(
        f"{r.name}-{r.range}" for r in requested_variant
    )
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
        env=sp_env,
    )

    LOG.debug(f"rez-search returned:\n----\n{result.stdout}\n----")
    if b"No matching" in result.stderr or not result.stdout:
        raise RecipeNotFound(f"No recipe satisfying {pkg_req} found in {RECIPES_PATH}")

    # Now if we've found some recipes to build, go through them, latest first and
    # make sure we've got/can build their dependencies
    to_cook = {}
    for line in reversed(result.stdout.decode("utf-8").splitlines()):
        (
            name,
            version_str,
            variant_str,
            requires_str,
            build_requires_str,
        ) = line.strip().split(":")
        version = VersionRange(version_str)
        variants = [parse_variants(v) for v in filter(None, variant_str.split("["))]
        LOG.debug(f"Found recipe for {name}-{version} {[str(v) for v in variants]}")

        # If the recipe defines an "any" variant (i.e. non-versioned), make sure that we have a constraint on it already
        # or we can't build it

        requires = filter(None, requires_str.split(" "))
        build_requires = filter(None, build_requires_str.split(" "))

        # Check whether any of the set of variants provided matches our variants list
        for variant in variants:
            LOG.debug(f"Checking {variant} against requested: {requested_variant}")
            # FIXME: choose the longest variant that matches, not just the first we find
            if not requested_variant.has_conflicts_with(variant):
                LOG.debug(
                    f"+++ Selected {name}-{version} {variant} for {pkg_req} {requested_variant}"
                )
                requested_variant = requested_variant.merged(variant)
                # recurisvely find all dependencies
                try:
                    for req_str in itertools.chain(requires, build_requires):
                        req = PackageRequest(req_str)
                        req_deps_list = find_recipe(
                            req,
                            requested_variant,
                            installed_selections,
                            package_search_path,
                        )

                        # Add each sub-dependency to the list if we don't have it already
                        # TODO: use versioning correctly here - need to find a single
                        # version for each package family that satisfies all requires
                        for dep_name, (
                            dep_version,
                            dep_variant,
                        ) in req_deps_list.items():
                            # print(f"dep_name={dep_name} dep_version={dep_version} dep_variants={dep_variants}")

                            LOG.debug(f"Current cook list: {to_cook}")
                            if dep_name in to_cook.keys():
                                LOG.debug(f"{dep_name} already in cook list")
                                # have already selected a range of this dependency, try
                                # and combine them
                                existing_range = to_cook[dep_name][0]
                                if dep_version.intersects(existing_range):
                                    # We can combine by narrowing the dependencies
                                    # todo - extend variants here?
                                    new_version = dep_version.intersection(
                                        existing_range
                                    )
                                    LOG.debug(f"Narrowing {dep_name} to {new_version}")
                                    to_cook[dep_name] = (
                                        new_version,
                                        dep_variant.merged(requested_variant),
                                    )
                                    LOG.debug(
                                        f"Placed {dep_name}-{new_version}, {dep_variant.merged(requested_variant)}"
                                    )
                                else:
                                    # no intersection - can't resolve
                                    raise DependencyConflict(
                                        f"{dep_name}-{dep_version} <--!--> {dep_name}-{existing_range}"
                                    )
                            else:
                                LOG.debug(f"{dep_name} not in cook list")
                                LOG.debug(
                                    f"Merging {dep_name} {dep_variant} and {requested_variant}"
                                )
                                to_cook[dep_name] = (
                                    dep_version,
                                    dep_variant.merged(requested_variant),
                                )
                                LOG.debug(
                                    f"Placed {dep_name}-{dep_version}, {dep_variant.merged(requested_variant)}"
                                )
                except DependencyConflict as e:
                    # FIXME: Why doesn't this chain properly?
                    raise DependencyConflict(f"Resolving {pkg_req}: {e}") from e
                except RecipeNotFound as e:
                    raise e

                # If we get here, all the dependencies are available, so add this variant
                # once we've merged it to agree with the requested variant
                this_req_info = (version, variant.merged(requested_variant))
                if name not in to_cook:
                    LOG.debug(f"{name} not in cook list")
                    to_cook[name] = this_req_info
                    LOG.debug(f"Placed {name}-{this_req_info[0]} {this_req_info[1]}")
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
        shutil.rmtree(path, ignore_errors=True)


def download_and_unpack(url, local_dir=None, move_up=True):
    import urllib.request, shutil, os

    if local_dir is None:
        local_dir = os.getcwd()

    print(f"Downloading {url}...")
    fn = os.path.join(local_dir, os.path.basename(url))
    with urllib.request.urlopen(url) as resp, open(fn, "wb") as f:
        shutil.copyfileobj(resp, f)

    files_before = os.listdir(".")
    shutil.unpack_archive(fn)
    files_after = os.listdir(".")
    new_files = list(set(files_after) - set(files_before))
    assert len(new_files) != 0

    if len(new_files) == 1 and move_up:
        archive_dir = new_files[0]

        for file in os.listdir(archive_dir):
            # Windows gets terribly confused with directories with the same name,
            # bless its cottons
            if file == "build" and os.path.isdir(file):
                for ff in os.listdir(os.path.join(archive_dir, "build")):
                    shutil.move(
                        os.path.join(archive_dir, "build", ff),
                        os.path.join("build", ff),
                    )
            else:
                shutil.move(os.path.join(archive_dir, file), file)


def fetch_repository(repo, branch=None):
    import subprocess as sp, os, shutil

    args = [
        "git",
        "clone",
        "--recursive",
        "--depth",
        "1",
        repo,
        "_clone",
    ]

    if branch is not None:
        args += ["-b", branch]

    sp.run(args)

    for f in os.listdir("_clone"):
        shutil.move(os.path.join("_clone", f), f)


def build_variant_path(variant: PackageList, path: str, comp: int):
    for f in os.listdir(path):
        if comp == len(variant) and f == "package.py":
            return os.path.join(path, "package.py")
        elif os.path.isdir(os.path.join(path, f)):
            pd = PackageRequest(f)
            vd = variant[comp]
            LOG.debug(f"Checking {pd} against {vd}")
            if vd.name == pd.name and vd.range.intersects(pd.range):
                LOG.debug("Intersects")
                path = os.path.join(path, f)
                return build_variant_path(variant, path, comp + 1)

    raise RuntimeError(
        f"No matching variant resource found for {name}-{version} {variant}"
    )


def find_recipe_resource(name: str, version: str, variant: PackageList):
    from copy import deepcopy

    """
    The recipe that we're trying to build might have an any variant (e.g. python packages)
    so we need to scan the directories under the version path and try and match them to 
    the requested variant
    """
    LOG.debug(f"Finding package.py for {name}-{version} {variant}")
    version_base = os.path.join(RECIPES_PATH, name, version)
    return build_variant_path(variant, version_base, 0)
    # comp = 0
    # path = deepcopy(version_base)
    # for root, dirs, files in os.walk(version_base):
    #     if comp == len(variant):
    #         LOG.debug(f"Checking for package.py in {path}")
    #         if "package.py" in files:
    #             return os.path.join(path, "package.py")
    #         else:
    #             raise RuntimeError(f"No package.py found for {name}-{version} {variant}")

    #     LOG.debug(f"DIRS: {dirs}")
    #     for d in dirs:
    #         pd = PackageRequest(d)
    #         vd = variant[comp]
    #         LOG.debug(f"Checking {pd} against {vd}")
    #         if vd.name == pd.name and vd.range.intersects(pd.range):
    #             LOG.debug("Intersects")
    #             path = os.path.join(path, d)
    #             comp += 1
    #             break
    #     else:
    #         # if we get here there's no matching variant
    #         raise RuntimeError(f"No matching variant resource found for {name}-{version} {variant}")


def cook_recipe(recipe, no_cleanup, verbose_build):
    # First, copy the resolved package.py to the build area
    name, version_range, variant = recipe
    version = str(version_range)
    str_variant = [str(v) for v in variant]
    pkg_subpath = os.path.join(name, version, *str_variant)
    staging_path = os.path.join(COOK_PATH, name, version)
    staging_package_py_path = os.path.join(staging_path, "package.py")
    recipe_package_py_path = find_recipe_resource(name, version, variant)
    LOG.debug(f"Found package.py at {recipe_package_py_path}")

    # blow away anything in the staging path already
    rmtree_for_real(staging_path)
    os.makedirs(staging_path)
    shutil.copyfile(recipe_package_py_path, staging_package_py_path)

    install_path = os.path.join(LOCAL_PACKAGE_PATH, pkg_subpath)
    install_root = os.path.join(LOCAL_PACKAGE_PATH, name, version)
    build_path = os.path.join(staging_path, "build", *[str(v) for v in variant])
    os.makedirs(build_path)

    # load the package and run pre_cook() if it's defined
    old_dir = os.getcwd()
    mod = load_module(
        f"{name}-{version}-{variant}",
        staging_package_py_path,
        globals={
            "cook_variant": str_variant,
            "root": staging_path,
            "name": name,
            "version": version,
            "variant": variant,
            "install_path": install_path,
            "install_root": install_root,
            "build_path": build_path,
        },
    )

    setattr(mod, "name", name)
    setattr(mod, "version", version)
    setattr(mod, "variant", variant)
    setattr(mod, "install_path", install_path)
    setattr(mod, "install_root", install_root)
    setattr(mod, "build_path", build_path)
    setattr(mod, "root", staging_path)
    setattr(mod, "download_and_unpack", download_and_unpack)
    setattr(mod, "fetch_repository", fetch_repository)

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
            sp_env = os.environ.copy()
            sp_env["REZ_COOK_VARIANT"] = str(str_variant)
            cmd = ["rez-build", "--install"]
            if "build_args" in dir(mod) and isinstance(mod.build_args, list):
                cmd += ["--build-args"] + mod.build_args

            print(f"Building {name}-{version} {variant}")
            if verbose_build:
                cmd += ["-vv"]
                sp.run(cmd, cwd=staging_path, check=True, env=sp_env)
            else:
                sp.run(
                    cmd,
                    cwd=staging_path,
                    check=True,
                    stderr=sp.PIPE,
                    stdout=sp.PIPE,
                    env=sp_env,
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
    parser.add_argument(
        "-c",
        "--constrain",
        type=str,
        nargs="+",
        help="Additional variant constraints",
    )
    parser.add_argument(
        "-s",
        "--search-path",
        type=str,
        nargs="+",
        help="Additional paths to search for installed packages",
    )
    parser.add_argument(
        "-p",
        "--install-path",
        type=str,
        help="Package path under which to install",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()
    if args.debug:
        print("Setting debug logging")
        logging.basicConfig(
            level=logging.DEBUG,
            format="[%(levelname)s] %(filename)s:%(lineno)d %(message)s",
        )
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="[%(levelname)s] %(message)s",
        )

    pkg_req = PackageRequest(args.package)
    installed_selections = []

    requested_variant = PackageList(REQUESTED_VARIANT + (args.constrain or []))

    package_search_path = args.search_path or SEARCH_PACKAGE_PATH

    recipes_to_cook = find_recipe(
        pkg_req, requested_variant, installed_selections, package_search_path
    )

    if not recipes_to_cook:
        if not installed_selections:
            with_str = ""
            if args.constrain:
                with_str = f" with {args.constrain}"
            LOG.error(
                f"Could not find a recipe or installed package for {pkg_req}{with_str}"
            )
            sys.exit(2)

        print(f"Nothing to do for {pkg_req} {requested_variant}")

    print("Package selection:")
    if installed_selections:
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
            cook_recipe((name, version, variant), args.no_cleanup, args.verbose_build)
