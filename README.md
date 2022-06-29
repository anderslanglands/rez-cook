Experimental package builder for rez. Works on my machine. Use at your own risk. 

Will install into $HOME/packages. I suggest moving your existing local packages out of the way for testing if you've already installed some there

Only tested on Windows so far.

Requires rez-python to be python 3 (tested lightly on 3.6 and 3.9)

To use:

1. Clone the rez-recipes repo: https://github.com/anderslanglands/rez-recipes
2. Set the `REZ_RECIPES_PATH` environment variable to point to your clone

3. Assuming you're starting with an empty or non-existent `~/packages` directory:
```bash
rez-bind os python
git clone git@github.com:anderslanglands/rez-cook.git
cd rez-cook
```

4. (on windows) create a visual studio package. This is used to set up the dev envioronment and is a prerequisite for everything else. Ultimately this wants to be a `rez-bind` too. There's also a `vs-2019` package which should work, but isn't tested. 
``` bash
rez-python ./rez-cook.py vs-2017
```

5. Now install something, e.g. `openimageio` and watch it download, build and install all the dependencies too. The `-c` flag specifies a series of constraints that you want to apply to the build. In this case we want to specify that we're building against python-3.7 and vs-2017. If you don't specify this it will choose the latest versions it can solve for, which may not be what you want. If you're feeling really daring and have time to kill, try building `usd`
```
rez-python ./rez-cook.py openimageio -c python-3.7 vs-2017
```