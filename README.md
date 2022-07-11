Experimental package builder for rez. Works on my machine. Use at your own risk. 

Will install into $HOME/packages by default. I suggest moving your existing local packages out of the way for testing if you've already installed some there, or use the `--prefix`` flag to specify somewhere else to install them. I would still back up my local packages anyway if I were you.

Tested on Windows 11 and Ubuntu 20.04. Requires pwsh on Windows. 

Requires rez-python to be python 3 (tested lightly on 3.6 and 3.9)

To use:

1. Clone the rez-recipes repo: https://github.com/anderslanglands/rez-recipes
2. Set the `REZ_RECIPES_PATH` environment variable to point to your local copy of the repo

3. If you're on Windows, set up your rez-config like so:
```python
{
    "default_shell": "pwsh",
    "plugins": {
        "build_system": {
            "cmake": {
                "build_system": "ninja",
                "cmake_args": [
                    "-Wno-dev",
                    "--no-warn-unused-cli"
                ]
            }
        }
    }
}
```
This is because we only support pwsh and rez's default of nmake is serial and takes foooorrrreeeevvvveeerrrrr.

4. Assuming you're starting with an empty or non-existent `~/packages` directory, first bind the platform and arch packages:
```bash
rez-bind os
git clone git@github.com:anderslanglands/rez-cook.git
cd rez-cook
```

6. Now to install something, e.g. `oiio` and watch it download, build and install all the dependencies too. 
```bash
# On Windows
rez-python ./rez-cook.py oiio -c python-3.7 vs-2017 cfg-release

# On Linux
rez-python ./rez-cook.py oiio -c python-3.7 cxx11abi=0 cfg-release

```The `-c` flag specifies a series of constraints that you want to apply to the build. In this case we want to specify that we're building against python-3.7 and vs-2017 on Windows, and specifying that we don't want to use the glibc cxx11 abi on Linux. If you don't specify this it will choose the latest versions it can solve for, which may not be what you want, or if it can't figure out an appropriate constraint from the dependency resolve, it will error and prompt you to specify the missing information. You can use `--dry-run` flag to just do the dependency resolve but not actually build anything.


The supported arguments are:
```
-d/--dry-run: Just do the dependency resolve and display the result, don't actually build anythin
-c/--constrain: specify a list of package requests to use to constrain the dependencies of the package you want to build
-s/--search-path: a list of package repository paths to use for searching for installed packages
-p/--prefix: the package prefix path to install to
--debug: print extra debugging information, including the build system output
-y/--yes: don't ask for confirmation before installing
```

If you're feeling really daring and have time to kill, try building `usd`. You can constrain it to use a particular vfx reference platform by specifying e.g. `-c vfxrp-2022`
