"""The frozen GUI's entry point (<APP_NAME>.exe). Not nexus/gui/__main__.py:
run by path, that one rewrites sys.path[0], which in a frozen app is
base_library.zip (the stdlib), and the next stdlib import fails."""
from nexus.gui import main

main()
