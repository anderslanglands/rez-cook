import sys, subprocess as sp, platform, re, os, tempfile, shutil, argparse, traceback
from pathlib import Path
from urllib import request
from wget import download

from rez.vendor.version.version import VersionRange
from rez.utils.formatting import PackageRequest
from package_list import PackageList
from recipe import Recipe
import logging
from typing import List, Dict
from copy import deepcopy
import traceback

LOG = logging.getLogger(__name__)

HOME = str(Path.home())
RECIPES_PATH = os.getenv("REZ_RECIPES_PATH") or f"{HOME}/code/rez-recipes"

PLATFORM = platform.system().lower()
ARCH = platform.machine()
PLATFORM_VARIANT = [f"platform-{PLATFORM}", f"arch-{ARCH}"]

COOK_PATH = os.path.join(tempfile.gettempdir(), "rez-cook")

REQUESTED_VARIANT = PLATFORM_VARIANT  # + constraints


class RecipeNotFound(Exception):
    pass


class DependencyConflict(Exception):
    pass


def load_module(name: str, path: str, globals={}):
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

    for key, value in globals.items():
        setattr(mod, key, value)

    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def parse_variants(vstr: str) -> PackageList:
    """
    Parse a string containing a list of variants in the format returned by rez-search
    into a PackageList
    """
    rgx = r"PackageRequest\('([\w\.-]+)'\)"
    result = PackageList(
        [PackageRequest(match.group(1)) for match in re.finditer(rgx, vstr)]
    )

    return result


def has_dependency_conflict(
    recipe: Recipe,
    constraints: PackageList,
    RECIPES: Dict,
    failed_dependency_chain: List[str],
) -> bool:
    """
    Recursively check if the Recipe recipe, or any of its dependencies, has conflicts
    with any of the given constraints
    """

    constraints_conflict = recipe.conflicts_with_package_list(constraints)
    if constraints_conflict:
        failed_dependency_chain.append(constraints_conflict)
        return True

    merged = recipe.build_requires.additive_merged(recipe.requires)
    if len(merged) != 0:
        all_conflict = True

        for pkg in merged:
            if pkg.name in RECIPES.keys():
                for rec in RECIPES[pkg.name]:
                    if rec.pkg.conflicts_with(pkg):
                        continue

                    if not has_dependency_conflict(
                        rec, constraints, RECIPES, failed_dependency_chain
                    ):
                        all_conflict = False

        return all_conflict
    else:
        return False


def build_dependency_tree(
    recipe: Recipe, constraints: PackageList, RECIPES: Dict, chain: List = []
) -> List[Recipe]:
    """
    Return a depth-first sorted list of Recipes that satisfy the dependency tree
    of recipe, given the constraints
    """

    # Merge the requirements for this recipe
    merged_requires = recipe.build_requires.additive_merged(
        recipe.requires
    ).additive_merged(recipe.variant)

    # Constrain first based on all deps of this recipe
    for pkg in merged_requires:
        constraints.add_constraint(pkg)

    # Go through the dependencies and build the tree of their deps with versions
    # matching the constraints list
    new_deps = {}
    for pkg in merged_requires:
        if pkg.name not in RECIPES.keys():
            raise RuntimeError(f"Could not find a recipe for {pkg}")

        # Iterate over all variants and select the best one
        # Here we just assume best = latest non-conflicting
        failed_dependency_chains = []
        found_any = False
        for rec in RECIPES[pkg.name]:
            if rec.pkg.conflicts_with(pkg):
                continue

            # LOG.debug(f"Considering {rec}")
            failchain = deepcopy(chain)
            conflict = has_dependency_conflict(rec, constraints, RECIPES, failchain)
            if conflict:
                failed_dependency_chains.append(failchain)
                continue

            # LOG.debug(f"Recursing dependencies of {rec}")
            dchain = deepcopy(chain)
            dchain.append(str(rec.pkg))
            dep_recipes = build_dependency_tree(rec, constraints, RECIPES, dchain)

            found_any = True

            # Add the dependency and its deps
            # LOG.debug(f"Adding {rec}")
            for dep_name, dep_recs in dep_recipes.items():
                if dep_name in new_deps.keys():
                    existing_recs = new_deps[dep_name]
                    for dr in dep_recs:
                        if dr not in existing_recs:
                            existing_recs.append(dr)
                else:
                    new_deps[dep_name] = dep_recs

            if rec.pkg.name not in new_deps.keys():
                new_deps[rec.pkg.name] = [rec]
            else:
                if rec not in new_deps[rec.pkg.name]:
                    new_deps[rec.pkg.name].append(rec)

            # Don't break here we'll accumulate all the valid recipes later
        
        if not found_any:
            raise RuntimeError(f"Could not find suitable recipe for {pkg}. {failed_dependency_chains}")

    return new_deps


def find_recipe(
    pkg_req: PackageRequest,
    requested_variant: PackageList,
    RECIPES: Dict,
    installed: bool,
) -> List[Recipe]:
    """
    Return a list of recipes (both installed and uncooked) that satisfy the given
    PackageRequest and requested_variant constraints.
    """


    if not RECIPES or pkg_req.name not in RECIPES.keys():
        LOG.debug(f"No recipes in storage for {pkg_req.name}")
        for pkg_name, recs in RECIPES.items():
            LOG.debug(f"{pkg_name}:")
            for rec in recs:
                LOG.debug(f"    {rec}")
        return []

    recipes = RECIPES[pkg_req.name]

    found = []
    for recipe in recipes:
        # if recipe.installed != installed:
        #     continue

        if pkg_req.conflicts_with(recipe.pkg):
            LOG.debug(f"{pkg_req} conflict with {recipe.pkg}")
            continue

        failchain = [f"{recipe}"]
        if has_dependency_conflict(recipe, requested_variant, RECIPES, failchain):
            LOG.debug(f"Rejected {recipe} for dependency conflict {failchain}")
            continue

        LOG.debug(f"Found {recipe}")
        found.append(recipe)

    return found


def rmtree_for_real(path):
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
    from io import StringIO, BytesIO

    stream = BytesIO(patch_str.encode())
    patch = PatchSet(stream)

    patch.apply()


def build_variant_path(variant: PackageList, path: str, comp: int):
    """
    Find the package.py corresponding to the given `variant` under `path`
    """

    for f in os.listdir(path):
        if comp == len(variant) and f == "package.py":
            return os.path.join(path, "package.py")
        elif os.path.isdir(os.path.join(path, f)):
            pd = PackageRequest(f)
            vd = variant[comp]
            if vd.name == pd.name and vd.range.intersects(pd.range):
                path = os.path.join(path, f)
                return build_variant_path(variant, path, comp + 1)

    raise RuntimeError(f"No matching variant resource found for {variant} under {path}")


def find_recipe_resource(name: str, version: str, variant: PackageList):
    """
    The recipe that we're trying to build might have an any variant (e.g. python packages)
    so we need to scan the directories under the version path and try and match them to
    the requested variant
    """

    version_base = os.path.join(RECIPES_PATH, name, version)

    try:
        return build_variant_path(variant, version_base, 0)
    except RuntimeError:
        return os.path.join(version_base, "package.py")


def cook_recipe(
    recipe: Recipe, constraints: PackageList, prefix: str, no_cleanup: bool, verbose_build: bool
):
    """
    Cook `recipe`.

    This creates a staging area under a temp directory, copies the variant
    package.py there, then runs "pre_cook()", then builds and installs
    """

    LOG.debug(f"Cooking {recipe}")
    # First, copy the resolved package.py to the build area
    name = recipe.pkg.name
    version = str(recipe.pkg.range)
    str_variant = [str(v) for v in recipe.variant]
    pkg_subpath = os.path.join(name, version, *str_variant)
    staging_path = os.path.join(COOK_PATH, name, version)
    staging_package_py_path = os.path.join(staging_path, "package.py")
    recipe_package_py_path = find_recipe_resource(name, version, recipe.variant)
    LOG.debug(f"Found package.py at {recipe_package_py_path}")

    # blow away anything in the staging path already
    rmtree_for_real(staging_path)
    os.makedirs(staging_path)
    shutil.copyfile(recipe_package_py_path, staging_package_py_path)

    # print(f"Merging {recipe.variant} with {constraints}")
    cook_variant_expanded = recipe.variant.merged_into(constraints)
    # print(f"expanded is {cook_variant_expanded}")

    # Try and modify the requests to maj.min in the variant
    cook_variant = []
    for req in cook_variant_expanded:
        if req.range.is_any():
            versions = [str(rec.pkg.range) for rec in RECIPES[req.name]]
            es = f"Cannot cook with unconstrained variant '{req}': you must specify a version on the command line to constrain this, e.g. '-c {req.name}-{versions[0]}'"
            raise RuntimeError(es)

        # check if it's got dots in the range
        range_toks = str(req.range).split(".")
        if len(range_toks) <= 2:
            # no dots, or one (i.e. it's already in maj or maj.min format)
            cook_variant.append(req)
        else:
            # just take the first two components
            new_req = PackageRequest(f"{req.name}-{'.'.join(range_toks[:2])}")
            LOG.debug(f"contracting {req} to {new_req}")
            cook_variant.append(new_req)
    cook_variant = PackageList(cook_variant)

    
    print(f"Building with {cook_variant}")
    

    install_path = os.path.join(prefix, pkg_subpath)
    install_root = os.path.join(prefix, name, version)
    build_path = os.path.join(staging_path, "build", *[str(v) for v in cook_variant])
    os.makedirs(build_path)

    # load the package and run pre_cook() if it's defined
    old_dir = os.getcwd()
    mod = load_module(
        f"{name}-{version}-{recipe.variant}",
        staging_package_py_path,
        globals={
            "cook_variant": [str(v) for v in cook_variant],
            "root": staging_path,
            "name": name,
            "version": version,
            "variant": recipe.variant,
            "install_path": install_path,
            "install_root": install_root,
            "build_path": build_path,
        },
    )

    if getattr(mod, "hashed_variants", False):
        from hashlib import sha1
        vars_str = str(list(map(str, cook_variant)))
        h = sha1(vars_str.encode("utf8"))
        variant_subpath = h.hexdigest()

        install_path = os.path.join(install_root, variant_subpath)
        build_path = os.path.join(staging_path, "build", variant_subpath)

    mod = load_module(
        f"{name}-{version}-{recipe.variant}",
        staging_package_py_path,
        globals={
            "cook_variant": [str(v) for v in cook_variant],
            "root": staging_path,
            "name": name,
            "version": version,
            "variant": recipe.variant,
            "install_path": install_path,
            "install_root": install_root,
            "build_path": build_path,
        },
    )

    setattr(mod, "name", name)
    setattr(mod, "version", version)
    setattr(mod, "variant", recipe.variant)
    setattr(mod, "install_path", install_path)
    setattr(mod, "install_root", install_root)
    setattr(mod, "build_path", build_path)
    setattr(mod, "root", staging_path)
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
        try:
            sp_env = os.environ.copy()
            sp_env["REZ_COOK_VARIANT"] = str(cook_variant)
            cmd = ["rez-build", "--install", "--prefix", prefix]
            if "build_args" in dir(mod) and isinstance(mod.build_args, list):
                cmd += ["--build-args"] + mod.build_args

            print(f"Building {name}-{version} {cook_variant}")
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
            if e.stderr:
                print(e.stderr.decode("utf-8"))
            print(f"\nBuild failed for {name}-{version} {cook_variant}: {e}")
            raise e
        finally:
            os.chdir(old_dir)
            # if not no_cleanup:
            #     rmtree_for_real(staging_path)


def load_recipes(package_search_paths: str, recipe_search_paths: str) -> Dict:
    """
    Load all recipes both installed and uncooked into a dict for fast lookups
    later. Uses rez-search to find the recipes.
    """

    RECIPES = {}

    result = sp.run(
        [
            "rez-search",
            "-t",
            "package",
            "--format",
            "{name}:{version}:{variants}:{requires}:{build_requires}",
            "--paths",
            package_search_paths,
        ],
        stdout=sp.PIPE,
        stderr=sp.PIPE,
    )

    if not b"No matching" in result.stderr:
        try:
            for name, version, vstr, requires_str, build_requires_str in [
                x.strip().split(":")
                for x in reversed(result.stdout.decode("utf-8").splitlines())
            ]:
                # this_pkg = PackageRequest(f"{name}-{version}")
                variants = [parse_variants(v) for v in filter(None, vstr.split("["))]
                requires = PackageList([x for x in filter(None, requires_str.split(" "))])
                build_requires = PackageList(
                    [x for x in filter(None, build_requires_str.split(" "))]
                )

                if name not in RECIPES.keys():
                    RECIPES[name] = []

                if len(variants) != 0:
                    for variant in variants:
                        RECIPES[name].append(
                            Recipe(name, version, variant, requires, build_requires, True)
                        )
                else:
                    RECIPES[name].append(
                        Recipe(
                            name, version, PackageList([]), requires, build_requires, True
                        )
                    )
        except ValueError as e:
            for x in result.stdout.decode('utf-8').splitlines():
                print(x)
            raise e

    # Now do recipes
    result = sp.run(
        [
            "rez-search",
            "-t",
            "package",
            "--format",
            "{name}:{version}:{variants}:{requires}:{build_requires}",
            "--paths",
            recipe_search_paths,
        ],
        stdout=sp.PIPE,
        stderr=sp.PIPE,
    )

    if b"No matching" in result.stderr or not result.stdout:
        raise RuntimeError(f"Could not find any recipes in {recipe_search_paths}: {result.stderr}")

    for name, version, vstr, requires_str, build_requires_str in [
        x.strip().split(":")
        for x in reversed(result.stdout.decode("utf-8").splitlines())
    ]:
        # this_pkg = PackageRequest(f"{name}-{version}")
        variants = [parse_variants(v) for v in filter(None, vstr.split("["))]
        requires = PackageList([x for x in filter(None, requires_str.split(" "))])
        build_requires = PackageList(
            [x for x in filter(None, build_requires_str.split(" "))]
        )

        if name not in RECIPES.keys():
            RECIPES[name] = []

        if len(variants) != 0:
            for variant in variants:
                RECIPES[name].append(
                    Recipe(name, version, variant, requires, build_requires, False)
                )
        else:
            RECIPES[name].append(
                Recipe(name, version, PackageList([]), requires, build_requires, False)
            )

    return RECIPES


def get_constraints_from_package_recipes(pkg_req: PackageRequest, initial_constraints: PackageList, RECIPES: Dict) -> PackageList:
    available_recipes = find_recipe(
        pkg_req,
        initial_constraints,
        RECIPES,
        installed=False,
    )

    if not available_recipes:
        raise RuntimeError(f"Could not find any recipes for {pkg_req}")

    # First walk the dependencies of the constraints to further specify our build constraints
    for rec in available_recipes:
        if rec.conflicts_with_package_list(initial_constraints):
            print(f"{rec} conflicts with {initial_constraints}")
            continue

        constraints = deepcopy(initial_constraints)
        _ = build_dependency_tree(rec, constraints, RECIPES)

        # FIXME: We can't just return here, we need to figure out how to represent
        # all the different options we might have available and how to combine them
        # with the recipes we'll find later
        return constraints

    raise RuntimeError(f"Could not solve constraints with {pkg_req} vs {initial_constraints}")



        
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
    if args.debug:
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

    # use rez-config to get the package install prefix
    result = sp.run(["rez-config", "packages_path"], stdout=sp.PIPE)
    config_install_prefix = result.stdout.decode("utf-8").splitlines()[0].strip('- ')
    install_prefix = args.prefix or config_install_prefix
    package_search_path = args.search_path or install_prefix

    # Load all the recipes we can find into memory
    RECIPES = load_recipes(package_search_path, RECIPES_PATH)

    # Find all recipes compatible with the requested package
    available_recipes = find_recipe(
        pkg_req,
        requested_variant,
        RECIPES,
        installed=False,
    )

    trees = {}

    # Walk the dependencies of the supplied constraints to build a full tree of constraints
    requested_constraints = deepcopy(requested_variant)
    for req in requested_variant:
        constraints = get_constraints_from_package_recipes(req, requested_variant, RECIPES)
        if constraints is None:
            raise RuntimeError("None constraints")
        requested_constraints = requested_constraints.additive_merged(constraints)

    if args.debug:
        LOG.debug("Full resolved constraints list:")
        for pkg in requested_constraints:
            LOG.debug(f"    {pkg}")
        print()

    # Now do a full walk of the compatible recipes' dependency trees, constraining
    # them by the constraints implied in the constraint tree walk above, and building
    # the full list of constrained packages that need to be built
    found_any = False
    for rec in available_recipes:
        conflict = rec.conflicts_with_package_list(requested_constraints)
        if conflict:
            print(f"Rejecting {rec} because {conflict}")
            continue

        constraints = deepcopy(requested_constraints)
        tree = build_dependency_tree(rec, constraints, RECIPES)

        found_any = True

        if rec not in trees.keys():
            trees[rec] = tree

    if not found_any:
        print(f"Could not find an available recipe for {pkg_req} that is compatible with {requested_variant}")
        sys.exit(1)

    selected_installed = []
    selected_to_cook = []

    # Now split the list of found dependency recipes into ones that are already
    # installed and ones that need to be cooked
    for toplevel, deps in trees.items():
        for name, recs in deps.items():
            rec = recs[0]
            if rec.installed:
                if rec not in selected_installed and rec not in selected_to_cook:
                    selected_installed.append(rec)
            else:
                if rec not in selected_installed and rec not in selected_to_cook:
                    selected_to_cook.append(rec)

        if toplevel.installed:
            if toplevel not in selected_installed and toplevel not in selected_to_cook:
                selected_installed.append(toplevel)
        else:
            if toplevel not in selected_installed and toplevel not in selected_to_cook:
                selected_to_cook.append(toplevel)
        
        # ultimately we want logic for cooking multiple recipes at the same time, which
        # will also mean needing to handle the fact that we'll have both installed packages
        # and uncooked recipes in this list. For now just break on the first one (which will
        # be the installed version if there is one)
        break

    if args.debug:
        for toplevel, deps in trees.items():
            if toplevel.installed:
                status = "✔"
            else:
                status = "☐"

            print(f"\n{status} {toplevel}")

            for name, recs in deps.items():
                if name in ['platform', 'arch']:
                    continue 

                print(f"{name}:")
                for r in recs:
                    if r.installed:
                        status = "✔"
                    else:
                        status = "☐"
                    print(f"    {status} {r}")

    # Now walk the list again and figure out if we've got multiple versions of 
    # the recipe available. If there's only one recipe to build, then we can 
    # just use that below instead of asking the user to constrain it further
    # TODO: don't do all these separate iterations
    solved_deps = {}
    for toplevel, deps in trees.items():
        for name, recs in deps.items():
            for rec in recs:
                if name not in solved_deps.keys():
                    solved_deps[name] = [rec.pkg]
                else:
                    if rec.pkg not in solved_deps[name]:
                        solved_deps[name].append(rec.pkg)
                
    # print("Solved deps")
    # for name, versions in solved_deps.items():
    #     print(f"{name}")
    #     for v in versions:
    #         print(f"    {v}")

        
    # Go through and check for unconstrained varaints before we actually get to 
    # cooking
    unconstrained = []
    solved_constraints = []
    for rec in selected_to_cook:
        cook_variant_expanded = rec.variant.merged_into(requested_constraints)
        for req in cook_variant_expanded:
            if req.range.is_any():
                if req.name in solved_deps.keys() and len(solved_deps[req.name]) == 1:
                    solved_constraints.append(req.merged(solved_deps[req.name][0]))
                elif req not in unconstrained:
                    unconstrained.append(req)
            else:
                solved_constraints.append(req)

    if unconstrained:
        print()
        print("The following packages have multiple versions that could satisfy"
              f" the dependencies of {pkg_req}. You must choose "
                "versions for them with '--constrain'")
        example_strs = []
        for pkg in unconstrained:
            versions = []
            first = True
            for req in solved_deps[pkg.name]:
                versions.append(f"{req.range}")
                if first:
                    example_strs.append(str(req))
                    first = False

            print(f"    {pkg}: {', '.join(versions)}")

        print("\nFor example:")
        print(f"    --constrain {' '.join(example_strs)}")
        print()

        sys.exit(2)


    if selected_installed:
        print()
        print("Using already installed dependencies:")
        for rec in selected_installed:
            print(f"    {rec}")
    
    if selected_to_cook:
        print()
        print("Cooking:")
        for rec in selected_to_cook:
            cook_variant = rec.variant.merged_into(requested_constraints)
            print(f"    {rec.pkg}/{'/'.join([str(p) for p in cook_variant])}")
        print()
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

    # Finally, cook each selected recipe. They will be in reverse dependency order already
    for recipe in selected_to_cook:
        LOG.debug(f"Cooking {recipe} {recipe.requires} {recipe.build_requires} with {solved_constraints}")
        cook_recipe(recipe, solved_constraints, install_prefix, args.no_cleanup, args.verbose_build)
