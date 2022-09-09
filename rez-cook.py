import argparse
import logging
import os
import platform
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Dict, List, Optional

import rez.config
from package_list import PackageList
from rez.build_process import BuildType, create_build_process
from rez.build_system import create_build_system
from rez.exceptions import BuildContextResolveError
from rez.package_search import ResourceSearchResult
from rez.packages import Variant, iter_packages
from rez.resolved_context import ResolvedContext
from rez.resolver import ResolverStatus
from rez.utils.formatting import PackageRequest
from rez.utils.resolve_graph import failure_detail_from_graph
from rez.vendor.version.requirement import Requirement, RequirementList
from rez.vendor.version.version import VersionRange
from wget import download

LOG = logging.getLogger("rez-cook")

HOME = str(Path.home())
RECIPES_PATH = os.getenv("REZ_RECIPES_PATH") or f"{HOME}/code/rez-recipes"

PLATFORM = platform.system().lower()
ARCH = platform.machine()
PLATFORM_VARIANT = [f"platform-{PLATFORM}", f"arch-{ARCH}"]

COOK_PATH = os.path.join(tempfile.gettempdir(), "rez-cook")

REQUESTED_VARIANT = PLATFORM_VARIANT  # + constraints


def load_module(name: str, path: str, global_vars: Optional[Dict] = None):
    """
    Load a package.py module and bung in the @early() decorator along with any other globals
    passed in the globals dict
    TODO: any other decorators etc?
    """
    import importlib.util
    import sys
    from rez.utils.sourcecode import early

    module_name = f"package-{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    setattr(mod, "early", early)

    if global_vars:
        for key, value in global_vars.items():
            setattr(mod, key, value)

    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def rmtree_for_real(path: Path):
    """
    Utility function to absolutely, positively delete a directory on both Windows and linux
    """
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


def download_and_unpack(
    url: str, local_dir: str = None, move_up: bool = True, format: str = None
):
    """
    Download and unpack an archive at `url`. If `move_up` is True, then strip the
    first component from the extracted archive (essentially what "tar xf --strip 1" does)
    """
    import shutil, os

    print(f"Downloading {url}\n")
    fn = download(url, local_dir)

    files_before = os.listdir(".")
    shutil.unpack_archive(fn, format=format)
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


def fetch_repository(repo: str, branch: str = None, local_dir = None):
    """
    Fetch the given branch from the given git repository, non-recusively with
    a depth of 1
    """

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

    if local_dir is None:
        local_dir = os.getcwd()

    for f in os.listdir("_clone"):
        shutil.move(os.path.join("_clone", f), os.path.join(local_dir, f))


def patch(patch_str: str):
    from patch import PatchSet
    from io import BytesIO

    stream = BytesIO(patch_str.encode())
    patch = PatchSet(stream)

    patch.apply()


def cook_recipe(
    recipe: Variant, prefix: str, no_cleanup: bool, verbose_build: bool
):
    """
    Cook `recipe`.

    This creates a staging area under a temp directory, copies the variant
    package.py there, then runs "pre_cook()", then builds and installs
    """

    LOG.debug(f"Cooking {recipe.qualified_package_name}{recipe.variant_text}")
    # First, copy the resolved package.py to the build area
    name = recipe.name
    version = str(recipe.version)
    cook_variant = [str(v) for v in recipe.variant_requires]

    # The subpath is a hash if hashed_variant == True
    # Else, it's the variant requires in order.
    # Ex: "3e537913d5fc8be53bd4460948eabdf3d86cc9b3" or "platform-windows\\arch-AMD64"
    pkg_subpath = recipe.subpath
    staging_path = Path(COOK_PATH, name, version)
    staging_package_py_path = os.path.join(staging_path, "package.py")

    recipe_package_py_path = os.path.join(recipe.root, "package.py")
    if not os.path.exists(recipe_package_py_path):
        recipe_package_py_path = recipe.parent.uri
    LOG.debug(f"Found package.py at {recipe_package_py_path}")

    # blow away anything in the staging path already
    rmtree_for_real(staging_path)
    os.makedirs(staging_path)
    shutil.copyfile(recipe_package_py_path, staging_package_py_path)

    print(f"Building with {cook_variant}")

    install_root = Path(prefix, name, version)
    install_path = install_root
    build_path = staging_path / "build"
    if pkg_subpath:
        install_path = install_path / pkg_subpath
        build_path = build_path / pkg_subpath
    os.makedirs(build_path)

    # load the package and run pre_cook() if it's defined
    old_dir = os.getcwd()
    variant_list = PackageList(recipe.variant_requires)
    mod = load_module(
        f"{name}-{version}-{recipe.variant_requires}",
        staging_package_py_path,
        global_vars={
            "cook_variant": cook_variant,
            "root": str(staging_path),
            "name": name,
            "version": version,
            "variant": variant_list,
            "install_path": str(install_path),
            "install_root": str(install_root),
            "build_path": str(build_path),
        },
    )

    setattr(mod, "name", name)
    setattr(mod, "version", version)
    setattr(mod, "variant", variant_list)
    setattr(mod, "install_path", str(install_path))
    setattr(mod, "install_root", str(install_root))
    setattr(mod, "build_path", str(build_path))
    setattr(mod, "root", str(staging_path))
    setattr(mod, "download", download)
    setattr(mod, "patch", patch)
    setattr(mod, "download_and_unpack", download_and_unpack)
    setattr(mod, "fetch_repository", fetch_repository)

    try:
        os.chdir(staging_path)
        if "pre_cook" in dir(mod):
            mod.pre_cook()
    except Exception as e:
        print(f"Pre-cooking failed for {name}-{version} {cook_variant}: {e}")
        traceback.print_exc()
        # if not no_cleanup:
        #     rmtree_for_real(staging_path)
        raise e
    finally:
        os.chdir(old_dir)

    # Now do the actual build
    # Use cook() if it's available, otherwise use the build_command
    if "cook" in dir(mod):
        try:
            os.chdir(build_path)
            os.makedirs(install_path)
            print(f"Cooking {name}-{version} {cook_variant}")
            mod.cook()
        except Exception as e:
            print(f"\nCook failed for {name}-{version} {cook_variant}: {e}")
            rmtree_for_real(install_path)
            raise e
        finally:
            os.chdir(old_dir)
            # if not no_cleanup:
            #     rmtree_for_real(staging_path)
    else:
        os.environ["REZ_COOK_VARIANT"] = str(cook_variant)
        build_args = []
        if "build_args" in dir(mod) and isinstance(mod.build_args, list):
            build_args = mod.build_args

        try:
            print(f"Building {name}-{version} {cook_variant}")
            buildsys = create_build_system(str(staging_path),
                                           verbose=verbose_build,
                                           build_args=build_args)

            builder = create_build_process(process_type="local",
                                           working_dir=str(staging_path),
                                           build_system=buildsys,
                                           verbose=verbose_build)
            builder.build(install_path=prefix,
                          install=True)
        except BuildContextResolveError as e:
            print(str(e), file=sys.stderr)

            raise e
        finally:
            os.chdir(old_dir)


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
        "--prefix",
        type=str,
        help="Package prefix path under which to install",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Don't ask for confirmation, just cook the selected recipes",
    )
    args = parser.parse_args()
    recipes_path = Path(RECIPES_PATH)

    # Rez overrides some logging config and breaks our logging.
    # Let's fix that.
    handler = logging.StreamHandler()
    LOG.addHandler(handler)
    if args.debug:
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(filename)s:%(lineno)d %(message)s"))
        LOG.setLevel(logging.DEBUG)
    else:
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        LOG.setLevel(logging.INFO)

    config = rez.config.config
    install_prefix = args.prefix or config.local_packages_path

    packages_path: List[str] = config.packages_path
    if install_prefix not in packages_path:
        # It's possible the install_prefix path is not in the config.
        # Add it to packages path so that the next builds can use the previous ones.
        packages_path.append(install_prefix)

        # Hack to get the build command to find the freshly built packages correctly if the
        # install_prefix path is not in rez's packages path.
        os.environ["REZ_PACKAGES_PATH"] = os.pathsep.join(packages_path)

    # Make sure the recipes path is removed from packages_path.
    # Else, if recipes path is in packages path, recipes that have a baked variant will be flagged as cooked.
    packages_path.remove(RECIPES_PATH)

    # Early check to see if the requested recipe exists.
    recipe_request = PackageRequest(args.package)
    it = iter_packages(recipe_request.name,
                       recipe_request.range,
                       paths=[RECIPES_PATH])
    available_recipes = sorted(it, key=lambda x: x.version)
    if not available_recipes:
        print(f"Could not find an available recipe for {recipe_request}")
        sys.exit(1)

    constraints = [PackageRequest(c) for c in args.constrain] if args.constrain else []

    # This is a context with the highest possible versions rez was able to come up with.
    # Recipes have a high priority.
    valid_recipe_context = ResolvedContext(package_paths=[RECIPES_PATH, *packages_path],
                                           package_requests=[*constraints, recipe_request],
                                           building=True)

    if valid_recipe_context.status != ResolverStatus.solved:
        requested_packages = [str(p) for p in valid_recipe_context.requested_packages()]
        LOG.error(f"Failed to resolve context: {requested_packages}")
        LOG.error(valid_recipe_context.failure_description)
        LOG.error(failure_detail_from_graph(valid_recipe_context.graph(as_dot=False)))
        sys.exit(1)

    valid_recipe = valid_recipe_context.get_resolved_package(recipe_request.name)

    # The pure recipe context gives us the packages required by the recipe, and only those.
    # This is required when we want to use constraints like vfxrp.
    # For example, if you want to build usd with vfxrp-2022 specs, you just want to constrain
    # usd requires to the vfxrp-2022 requires versions. You don't want to build missing packages
    # that vfxrp-2022 requires (like alembic, blosc, pyqt5, openvdb and vfxrp itself).
    pure_recipe_context = ResolvedContext(package_paths=[RECIPES_PATH, *packages_path],
                                          package_requests=[recipe_request, *valid_recipe.requires],
                                          building=True)

    if pure_recipe_context.status != ResolverStatus.solved:
        requested_packages = [str(p) for p in pure_recipe_context.requested_packages()]
        LOG.error(f"Failed to resolve context: {requested_packages}")
        LOG.error(pure_recipe_context.failure_description)
        LOG.error(failure_detail_from_graph(pure_recipe_context.graph(as_dot=False)))
        sys.exit(1)

    pure_recipe_requires = [p.name for p in pure_recipe_context.resolved_packages]


    # A set of required package requests.
    # We'll use that later to set up a requirement list that will help us to filter
    # recipes and installed dependencies.
    _recipe_requires = set(constraints)

    # Set up a dict that contains the recipes required.
    # We'll refer to it later when overriding the variant requires for each package of the recipe.
    possible_recipes = {}
    packages_without_recipe = {}
    for rec in valid_recipe_context.resolved_packages:
        rec: Variant
        for req in rec.get_requires(True):
            # Even if we won't build the current recipe, we still want to get its requires.
            # This is to get vfxrp constraints working as expected.
            _recipe_requires.add(req)
        if rec.name not in pure_recipe_requires:
            # This package is not required to run the built recipe we want to cook.
            # Useful when you want to constrain to vfxrp for example.
            continue
        if Path(rec.repository.location) == recipes_path:
            possible_recipes[rec.name] = rec
        else:
            packages_without_recipe[rec.name] = rec

    # Combine all the requires in a requirement list.
    requirement_list = RequirementList(_recipe_requires)

    selected_installed = {}
    # Add the packages that have no recipes.
    for variant_name, variant in packages_without_recipe.items():
        selected_installed[variant_name] = variant

    selected_to_cook = {}
    # Find every built recipes that satisfy the requirement list.
    for recipe_name, recipe in possible_recipes.items():
        resource_type: str
        result: List[ResourceSearchResult]
        recipe_require_request = requirement_list.get(recipe_name)
        if not recipe_require_request:
            installed_packages = sorted(iter_packages(recipe.name,
                                                      str(recipe.version),
                                                      paths=packages_path),
                                        key=lambda x: x.version)
        else:
            installed_packages = sorted(iter_packages(recipe_require_request.name,
                                                      recipe_require_request.range,
                                                      paths=packages_path),
                                        key=lambda x: x.version)
        if not installed_packages:
            # Package not found, we need to build the recipe.
            selected_to_cook[recipe_name] = recipe
            continue

        # Check if the requirement list is satisfied, starting from the newest package available.
        for package in reversed(installed_packages):
            build_found = False
            has_conflict = False
            for variant in package.iter_variants():
                variant_requires = variant.variant_requires

                # Some variants may not have the same number of requires.
                # For example, you can build usd with or without alembic.
                # So, if the recipe variant we want to build
                variant_requires_names = [r.name for r in variant_requires]
                if not all([recipe_req.name in variant_requires_names for recipe_req in recipe.variant_requires]):
                    continue

                for req in variant_requires:
                    constrained_req: Requirement = requirement_list.get(req.name)
                    if not constrained_req:
                        # This req has no influence on the build, we can ignore it.
                        continue
                    has_conflict = constrained_req.conflicts_with(req)
                    if has_conflict:
                        break
                    else:
                        possible_recipe = possible_recipes.get(req.name)
                        if not possible_recipe:
                            continue
                        if VersionRange(str(possible_recipe.version)) > req.range:
                            has_conflict = True
                            break

                if not has_conflict and variant.version >= recipe.version:
                    if not os.path.exists(variant.root):
                        selected_to_cook[recipe_name] = recipe
                        build_found = True
                        break
                    # The installed variant satisfies the constraints and is at least the same version as the recipe.
                    # We can add it to the selected installed recipes.
                    selected_installed[recipe_name] = variant
                    build_found = True
                    break
            if not has_conflict and build_found:
                # A build was found without any conflict, we can stop iterating here.
                break

        if recipe_name not in selected_installed:
            # No build were found, cook the recipe.
            selected_to_cook[recipe_name] = recipe

    if selected_installed:
        print()
        print("Using already installed dependencies:")
        for dep in selected_installed.values():
            dirs = [x.safe_str() for x in dep.variant_requires]
            subpath_str = os.path.join(*dirs) if dirs else None
            variant_text = ""
            if dep.subpath:
                variant_text = f"{os.path.sep}{subpath_str}"
                if dep.hashed_variants:
                    variant_text = f"{variant_text} ({dep.subpath})"
            print(f"    {dep.qualified_package_name}{variant_text}")

    if selected_to_cook:
        print()
        print("Cooking:")
        subpath_errors = []
        recipe_variant_requires = {}
        for rec in selected_to_cook.values():
            # Populate variant requires dict with precise versions.
            for req in rec.get_requires(build_requires=True):
                req: PackageRequest
                if req.name in recipe_variant_requires:
                    continue

                recipe_require = None
                if req.name in selected_installed:
                    recipe_require = selected_installed[req.name]
                elif req.name in selected_to_cook:
                    recipe_require = selected_to_cook[req.name]
                if not recipe_require:
                    # Something went wrong.
                    # The require is both not installed and not to be cooked.
                    print(f"{str(req)} is not installed and not to be cooked. Aborting.")
                    sys.exit(1)
                recipe_variant_requires[req.name] = recipe_require

            # Set the recipe variant requires to those precise versions.
            precise_variant_reqs = []
            for variant_req in rec.resource.variant_requires:
                full_package_name = recipe_variant_requires[variant_req.name].qualified_package_name
                precise_variant_reqs.append(PackageRequest(full_package_name))
            rec.resource.variant_requires = precise_variant_reqs
            if rec.parent.variants:
                rec.parent.variants[rec.index] = precise_variant_reqs

            # Compute the expected subpath.
            dirs = [x.safe_str() for x in rec.variant_requires]
            subpath_str = os.path.join(*dirs) if dirs else None

            if rec.hashed_variants:
                from hashlib import sha1

                vars_str = str(list(map(str, rec.variant_requires)))
                h = sha1(vars_str.encode("utf8"))
                expected_subpath = h.hexdigest()
            else:
                expected_subpath = subpath_str

            if rec.subpath != expected_subpath:
                # Cached subpath needs to be updated, invalidate it.
                del rec.resource.subpath
                if expected_subpath != rec.subpath:
                    subpath_errors.append((expected_subpath, rec))

            # Put the variant_text in rec, we'll use it to log while cooking.
            rec.variant_text = ""
            if subpath_str:
                rec.variant_text = f"{os.path.sep}{subpath_str}"
                if rec.hashed_variants:
                    rec.variant_text = f"{rec.variant_text} ({expected_subpath})"
            print(f"    {rec.qualified_package_name}{rec.variant_text}")
        print()

        if subpath_errors:
            LOG.error("Some package subpath are wrong:")
            for expected_subpath, rec in subpath_errors:
                LOG.error(f"  {rec.name}:")
                LOG.error(f"    Expected: {expected_subpath}")
                LOG.error(f"      Actual: {rec.subpath}")
            sys.exit(1)
    else:
        print("Nothing to cook")
        sys.exit(0)

    if args.dry_run:
        print("Dry run. Exiting.")
        sys.exit(0)

    if not args.yes:
        c = input("Proceed? (y/n): ")
        if c.lower() != "y":
            print("Exiting")
            sys.exit(0)

    for recipe in selected_to_cook.values():
        LOG.debug(f"Cooking {recipe.qualified_package_name}{recipe.variant_text}")
        LOG.debug(f"With: {', '.join([str(r) for r in recipe.get_requires(build_requires=True)])}")
        cook_recipe(recipe, install_prefix, args.no_cleanup, args.verbose_build)
