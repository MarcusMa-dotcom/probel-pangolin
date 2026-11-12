# PyInstaller recipe for the macOS app "Probel Pangolin.app".
#
#   pyinstaller --noconfirm packaging/probel_pangolin.spec
#
# Build with a Python that carries Tk and targets an old enough macOS, for
# example a python.org installer or a python-build-standalone build. The app
# runs on the processor type of the Python it was built with.

import os

from PyInstaller.utils.hooks import collect_data_files

HERE = SPECPATH  # noqa: F821  (set by PyInstaller)
VERSION = "0.1.0"
ICON = os.path.join(HERE, "Probel Pangolin.icns")

analysis = Analysis(  # noqa: F821
    [os.path.join(HERE, "launcher.py")],
    datas=collect_data_files("probelpangolin_app"),      # templates and static files
    hiddenimports=["probelpangolin"],
    # Pure-Python fallback instead of the compiled speedup: one binary less to trust and sign.
    excludes=["markupsafe._speedups"],
)
archive = PYZ(analysis.pure)  # noqa: F821
executable = EXE(  # noqa: F821
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Probel Pangolin",
    console=False,
)
collected = COLLECT(executable, analysis.binaries, analysis.datas, name="Probel Pangolin")  # noqa: F821
app = BUNDLE(  # noqa: F821
    collected,
    name="Probel Pangolin.app",
    icon=ICON if os.path.exists(ICON) else None,
    bundle_identifier="de.marcusmathy.probelpangolin",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "Probel Pangolin",
        "CFBundleShortVersionString": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        "NSHumanReadableCopyright": "Copyright (c) 2026 Marcus Mathy. MIT License. Use at your own risk.",
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSLocalNetworkUsageDescription":
            "Probel Pangolin talks to routers and controllers on your network over SW-P-08.",
    },
)
