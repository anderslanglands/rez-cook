Experimental package builder for rez. Works on my machine. Use at your own risk. 

Will install into $HOME/packages by default. I suggest moving your existing local packages out of the way for testing if you've already installed some there, or use the `--prefix`` flag to specify somewhere else to install them. I would still back up my local packages anyway if I were you.

Tested on Windows 11, Ubuntu 18.04, 20.04 and 22.04. Requires pwsh on Windows. 

Requires rez-python to be python 3.6+ (tested on 3.6 and 3.9)

If you don't yet have rez installed, do the following first:

```bash
git clone https://github.com/AcademySoftwareFoundation/rez.git
cd rez
python ./install.py # Note this must be python3.6+
rez-bind os
```

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

5. Now to install something, e.g. `usd` and watch it download, build and install all the dependencies too. 
```bash
# Build USD 21.08, constraining it to match the requirements of vfx reference platform 2022
rez-python ./rez-cook.py usd-21 -c vfxrp-2022 cfg-release

```


The supported arguments are:
```
-d/--dry-run: Just do the dependency resolve and display the result, don't actually build anythin
-c/--constrain: specify a list of package requests to use to constrain the dependencies of the package you want to build
-s/--search-path: a list of package repository paths to use for searching for installed packages
-p/--prefix: the package prefix path to install to
-y/--yes: don't ask for confirmation before installing
-bb/--verbose-build: print all build output
--debug: print extra debugging information
```
