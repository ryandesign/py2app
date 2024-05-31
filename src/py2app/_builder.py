import importlib.resources
import itertools
import marshal
import pathlib
import plistlib
import shutil
import sys
import types
import zipfile
from functools import singledispatch
from importlib.util import MAGIC_NUMBER
from itertools import chain
from typing import Any, Dict, Union, assert_never

from modulegraph2 import (
    BytecodeModule,
    ExtensionModule,
    NamespacePackage,
    Package,
    PyPIDistribution,
    Script,
    SourceModule,
)

from ._bundlepaths import BundlePaths, bundle_paths
from ._config import BundleOptions, Py2appConfiguration
from ._modulegraph import ModuleGraph
from ._progress import Progress
from ._recipes import process_recipes
from ._standalone import PythonStandalone
from .apptemplate.plist_template import (
    infoPlistDict as app_info_plist_dict,  # XXX: Replace
)
from .apptemplate.setup import main as app_stub_path  # XXX: Replace
from .bundletemplate.plist_template import (
    infoPlistDict as bundle_info_plist_dict,  # XXX: Replace
)
from .bundletemplate.setup import main as bundle_stub_path  # XXX: Replace
from .macho_audit import audit_macho_issues
from .util import find_converter  # XXX: Replace


def _pack_uint32(x):
    """Convert a 32-bit integer to little-endian."""
    return (int(x) & 0xFFFFFFFF).to_bytes(4, "little")


def code_to_bytes(code: types.CodeType) -> bytearray:
    """
    Serialize a code object into ".pyc" format
    """

    data = bytearray(MAGIC_NUMBER)
    data.extend(_pack_uint32(0))
    data.extend(_pack_uint32(0))
    data.extend(_pack_uint32(0))
    data.extend(marshal.dumps(code))

    return data


#
# Storing nodes into a bundle
#


def relpath_for_script(node: Script) -> str:
    return f"bundle-scripts/{node.identifier.split('/')[-1]}"


# XXX: What to do about ".dylib" (and the ".dylibs" folder in a lot of wheels...)
# XXX: Recipes should be able to affect this:
#      - Exclude/include specific resources (e.g. email/architecture.rst)
#      - Mark packages as not having resources
# XXX: Should do something with filesystem rights bits as well?
# XXX: Handle subfolders (both iterating and returning)
EXCL_EXTENSIONS = {
    ".py",
    ".pyi",
    ".so",
}
EXCL_NAMES = {".svn"}


def iter_resources(node: Union[Package, NamespacePackage]):
    """
    Yield all resources in a package, including those in subdirectories.
    """
    try:
        for resource in importlib.resources.files(node.identifier).iterdir():
            if resource.name in EXCL_NAMES:
                continue

            if any(resource.name.endswith(ext) for ext in EXCL_EXTENSIONS):
                continue

            if resource.is_file():
                yield resource.name, resource.read_bytes()

            else:
                # Directories are annoying. These could subfolders with resources,
                # but can also be subpackages (which will be handled themselves as needed)
                pass
    except AttributeError:
        pass


# 1. Zipfile variant


@singledispatch
def zip_node(
    node: object, zf: zipfile.ZipFile, more_extensions: Dict[str, ExtensionModule]
) -> None:
    """
    Include a single modulegraph2 node into the Python library
    zipfile for a bundle.
    """
    assert_never(node)


@zip_node.register(SourceModule)
@zip_node.register(BytecodeModule)
def zip_py_node(
    node: Union[SourceModule, BytecodeModule],
    zf: zipfile.ZipFile,
    more_extensions: Dict[str, ExtensionModule],
) -> None:
    """
    Include the compiled version of a SourceModule into
    the zipfile for a bundle.
    """
    if node.filename.stem == "__init__":
        path = node.identifier.replace(".", "/") + "/__init__.pyc"
    else:
        path = node.identifier.replace(".", "/") + ".pyc"
    zf.writestr(path, code_to_bytes(node.code))


@zip_node.register
def zip_script_node(
    node: Script, zf: zipfile.ZipFile, more_extensions: Dict[str, ExtensionModule]
) -> None:
    """
    Include the compiled version of a script into the zipfile.
    """
    zf.writestr(relpath_for_script(node), code_to_bytes(node.code))


@zip_node.register
def zip_ext_node(
    node: ExtensionModule,
    zf: zipfile.ZipFile,
    more_extensions: Dict[str, ExtensionModule],
) -> None:
    """
    Include an ExtensionModule into the zipfile.

    macOS cannot load shared libraries from memory, especially not
    when code signing is used. Therefore the extension is copied to
    a separate directory where it is picked up by a custom importlib
    Finder.
    """
    more_extensions[f"{node.identifier}.so"] = node


@zip_node.register(Package)
@zip_node.register(NamespacePackage)
def zip_package_node(
    node: Package, zf: zipfile.ZipFile, more_extensions: Dict[str, ExtensionModule]
) -> None:
    path = node.identifier.replace(".", "/")
    zf.mkdir(path)

    if isinstance(node, Package):
        zip_node(node.init_module, zf, more_extensions)

    # Copy resource data (using importlib API!)
    for relname, data in iter_resources(node):
        zf.writestr(f"{path}/{relname}", data)


EXCL_DIST_INFO = {"RECORD", "INSTALLER", "WHEEL"}


def get_dist_info(value):
    parts = value.split("/")
    for idx, p in enumerate(parts):
        if p.endswith(".dist-info"):
            if parts[idx + 1] in EXCL_DIST_INFO:
                return None
            return "/".join(parts[idx:])
    return None


@zip_node.register
def zip_distribution(
    node: PyPIDistribution,
    zf: zipfile.ZipFile,
    more_extensions: Dict[str, ExtensionModule],
) -> None:
    # XXX: This needs work, in particular this  shouldn't read
    #      metadata from the filesystem.
    for fn in node.files:
        relpath = get_dist_info(fn)
        if relpath is None:
            continue
        data = pathlib.Path(fn).read_bytes()

        zf.writestr(relpath, data)


# 2. Filesystem variant (primarily used for nodes that are not zipsafe)


@singledispatch
def fs_node(node: object, root: pathlib.Path) -> None:
    assert_never(node)


@fs_node.register(SourceModule)
@fs_node.register(BytecodeModule)
def fs_py_node(node: Union[SourceModule, BytecodeModule], root: pathlib.Path) -> None:
    if node.filename.stem == "__init__":
        path = node.identifier.replace(".", "/") + "/__init__.pyc"
    else:
        path = node.identifier.replace(".", "/") + ".pyc"

    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(code_to_bytes(node.code))


@fs_node.register
def fs_script_node(node: Script, root: pathlib.Path) -> None:
    path = root / relpath_for_script(node)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(code_to_bytes(node.code))


@fs_node.register
def fs_ext_node(node: ExtensionModule, root: pathlib.Path) -> None:
    # XXX: Copying should be separate function.
    # XXX: Handle extensions in packages, subdiretory might not be here yet
    ext_path = root / (node.identifier.replace(".", "/") + ".so")

    ext_path.parent.mkdir(parents=True, exist_ok=True)

    ext_path.write_bytes(node.filename.read_bytes())


@fs_node.register(Package)
@fs_node.register(NamespacePackage)
def fs_package_node(node: Union[Package, NamespacePackage], root: pathlib.Path) -> None:
    # XXX: To be impolemented
    path = node.identifier.replace(".", "/")

    (root / path).mkdir(parents=True, exist_ok=True)

    if isinstance(node, Package):
        fs_node(node.init_module, root)

    # Copy resource data (using importlib API!)
    for relname, data in iter_resources(node):
        (root / path / relname).write_bytes(data)


BUNDLE_FOLDERS = (
    "Contents/MacOS",
    "Contents/Resources",
    "Contents/Frameworks",
)


def create_bundle_structure(bundle: BundleOptions, progress: Progress) -> pathlib.Path:
    """
    Create the directory structure for a bundle and return the
    path to the root of the tree.
    """

    root = pathlib.Path("dist2") / f"{bundle.name}{bundle.extension}"

    # XXX: Should clear this directory first.

    # XXX: These paths are also in _bundlepaths.py
    for relpath in progress.iter_task(
        BUNDLE_FOLDERS, "Create bundle structure", lambda n: n
    ):
        (root / relpath).mkdir(parents=True, exist_ok=True)
    return root


def add_iconfile(
    root: pathlib.Path, plist: Dict[str, Any], bundle: BundleOptions, progress: Progress
) -> None:
    """
    Add an icon file to the bundle if one is available.
    """

    task_id = progress.add_task("Add bundle icon file", count=1)
    if bundle.iconfile is None:
        # XXX: Switch to own default icon?
        iconfile = (
            pathlib.Path(sys.base_prefix)
            / "Resources/Python.app/Contents/Resources/PythonApplet.icns"
        )
        if not iconfile.is_file():
            return
    else:
        iconfile = bundle.iconfile

    data = iconfile.read_bytes()
    (root / f"Contents/Resources/{bundle.name}.icns").write_bytes(data)
    plist["CFBundleIconFile"] = f"{bundle.name}.icns"
    progress.step_task(task_id)


def add_loader(root: pathlib.Path, bundle: BundleOptions, progress: Progress) -> None:
    """
    Add stub executables for the main executable and additional scripts
    """
    task_id = progress.add_task("Add stub executable", count=1)
    if bundle.plugin:
        stub = pathlib.Path(bundle_stub_path(arch=bundle.macho_arch.value))
    else:
        stub = pathlib.Path(app_stub_path(arch=bundle.macho_arch.value))

    main_path = root / f"Contents/MacOS/{bundle.name}"
    main_path.write_bytes(stub.read_bytes())
    main_path.chmod(0o755)
    progress.step_task(task_id)

    if bundle.extra_scripts:
        for script in progress.iter_task(
            bundle.extra_scripts, "Add stubs for extra-scripts", lambda n: n.name
        ):
            stub = pathlib.Path(
                app_stub_path(arch=bundle.macho_arch.value, secondary=True)
            )
            exe_path = root / f"Contents/MacOS/{script.stem}"
            exe_path.write_bytes(stub.read_bytes())
            exe_path.chmod(0o755)


def add_plist(root: pathlib.Path, plist: Dict[str, Any], progress: Progress) -> None:
    """
    Create the Info.plist file in the output.
    """
    task_id = progress.add_task("Add Info.plist", count=1)
    info_plist = root / "Contents/Info.plist"
    with open(info_plist, "wb") as stream:
        plistlib.dump(plist, stream)
    progress.step_task(task_id)


def add_bootstrap(
    root: pathlib.Path, bundle: BundleOptions, progress: Progress
) -> None:
    bootstrap_path = root / "Contents/Resources/__boot__.py"

    with open(bootstrap_path, "w") as stream:
        stream.write(
            importlib.resources.files("py2app.bootstrap")
            .joinpath("boot_app.py")
            .read_text(encoding="utf-8")
        )
        stream.write("\n")

        stream.write(f'DEFAULT_SCRIPT = "{bundle.script}"\n')
        stream.write("SCRIPT_MAP = {}\n")
        stream.write("_run()\n")


# Filter function for shutil.copytree ignoring SCM directories,
# backup files and temporary files.
ignore_filter = shutil.ignore_patterns(".git", ".svn", "*.sv", "*.bak", "*~", "._*.swp")


def add_resources(
    paths: BundlePaths, bundle: BundleOptions, progress: Progress
) -> None:
    # XXX: Add a mechanisme for recipes to add resources as well.
    if not bundle.resources:
        return

    for rsrc in progress.iter_task(
        bundle.resources, "Copy resources", lambda n: str(n)
    ):
        for src in rsrc.sources:
            converter = find_converter(src)
            if converter is not None:
                converter(src, paths.resources / rsrc.destination / src.name)
            elif src.is_file():
                shutil.copy2(
                    src,
                    paths.resources / rsrc.destination / src.name,
                    follow_symlinks=False,
                )
            else:
                shutil.copytree(
                    src,
                    paths.resources / rsrc.destination / src.name,
                    ignore=ignore_filter,
                    symlinks=True,
                )


def get_info_plist(bundle: BundleOptions) -> Dict[str, Any]:
    """
    Get the base Info.plist contents for the bundle, based
    on the template for the bundle kind and the specified
    Info.plist contents.
    """
    # XXX: This uses code from the 'old' codebase and needs to
    #      be replaced:
    #      1. Use resource files for the Info.plist templates
    #      2. Move merging into this function
    if bundle.plugin:
        return bundle_info_plist_dict(bundle.name, bundle.plist)
    else:
        return app_info_plist_dict(bundle.name, bundle.plist)


def collect_python(
    bundle: BundleOptions, paths: BundlePaths, graph: ModuleGraph, progress: Progress
) -> Dict[pathlib.Path, pathlib.Path]:
    # XXX: This isn't really 'Scanning' any more
    #
    # XXX: ExtensionModules need more work to be able to
    #      handle @rpath, @loader_path (but this requires
    #      rewriting modulegraph as well...)
    #
    # XXX: semi-standalone and alias should not include stdlib
    # XXX: recipes and bundle-templates must be able to replace
    #      the source of a python module (e.g. site.py)

    zip_nodes, unzip_nodes = graph.collect_nodes()

    # XXX: Creating the directory structure should be elsewhere?
    #      "Elsewhere" should also be responsible for clearing any
    #      preexisting data.
    paths.pylib_zipped.parent.mkdir(parents=True, exist_ok=True)
    paths.extlib.mkdir(parents=True, exist_ok=True)
    paths.pylib.mkdir(parents=True, exist_ok=True)

    more_extensions: Dict[str, ExtensionModule] = {}
    included_distributions = {
        node.distribution.name: node.distribution
        for node in chain(zip_nodes, unzip_nodes)
        if node.distribution is not None
    }

    zf = zipfile.ZipFile(paths.pylib_zipped, "w")

    if included_distributions:
        for node in progress.iter_task(
            included_distributions.values(), "Collect dist-info", lambda n: n.name
        ):
            zip_node(node, zf, more_extensions)

    if zip_nodes:
        for node in progress.iter_task(
            zip_nodes, "Collect site-packages.zip", lambda n: n.identifier
        ):
            zip_node(node, zf, more_extensions)

    if unzip_nodes:
        for node in progress.iter_task(
            unzip_nodes, "Collect site-packages directory", lambda n: n.identifier
        ):
            fs_node(node, paths.pylib)

    ext_map = {}
    if more_extensions:
        for ext_name, node in progress.iter_task(
            more_extensions.items(),
            "Collect zipped extensions",
            lambda n: n[1].identifier,
        ):
            (paths.extlib / ext_name).write_bytes(node.filename.read_bytes())
            ext_map[paths.extlib / ext_name] = node.filename
    return ext_map


def make_readonly(
    root: pathlib.Path, bundle: BundleOptions, progress: Progress
) -> None:
    """
    Make the bundle read only.
    """
    # XXX: To be implemented
    ...


def get_module_graph(bundle: BundleOptions, progress: Progress) -> ModuleGraph:
    def node_done(graph, node):
        nonlocal scan_count

        progress.update(task_id, current=node.identifier)
        progress.step_task(task_id)
        scan_count += 1

        if isinstance(node, (Package, NamespacePackage)):
            if (
                node.identifier in bundle.py_full_package
                or node.identifier == "encodings"
            ):
                graph.import_package(node, node.identifier)

    graph = ModuleGraph()
    graph.add_post_processing_hook(node_done)
    task_id = progress.add_task("Scanning Python dependencies", count=None)
    scan_count = 0
    graph.add_excludes(bundle.py_exclude)

    for script in itertools.chain((bundle.script,), bundle.extra_scripts):
        graph.add_script(script)

    for module_name in bundle.py_include:
        graph.add_module(module_name)

    progress.task_done(task_id)
    return graph


def macho_standalone(
    root: pathlib.Path,
    graph: ModuleGraph,
    bundle: BundleOptions,
    ext_map: Dict[pathlib.Path, pathlib.Path],
    progress: Progress,
) -> None:
    standalone = PythonStandalone(root, graph, bundle.build_type, ext_map, progress)
    standalone.run()


def build_bundle(
    config: Py2appConfiguration, bundle: BundleOptions, progress: Progress
) -> bool:
    """
    Build the output for *bundle*. Returns *True* if successful and *False* otherwise.
    """
    # XXX: There is no nice way to report errors at this point
    #      (ok, errors, fatal errors) other than in progress output.

    graph = get_module_graph(bundle, progress)
    graph.add_module("zipfile")

    process_recipes(graph, config.recipe, progress)

    # XXX: Warn about various types of "missing" nodes in
    #      the graph.

    # XXX: Consider dynamically calculating the order of
    #      steps by adding a decorator that documents
    #      dependencies between steps.
    root = create_bundle_structure(bundle, progress)
    paths = bundle_paths(root, bundle.build_type)
    plist = get_info_plist(bundle)
    add_iconfile(root, plist, bundle, progress)
    add_loader(root, bundle, progress)
    add_resources(paths, bundle, progress)
    ext_map = collect_python(bundle, paths, graph, progress)
    add_bootstrap(
        root, bundle, progress
    )  # XXX: Needs more info which is collected in collect_python
    add_plist(root, plist, progress)

    macho_standalone(root, graph, bundle, ext_map, progress)

    # - Run machostandalone
    # XXX: Does machostandalone affect other stuff?
    # XXX: Longer term replace 'macholib' by 'macholib2' with
    #      a nicer interface.
    #
    # - Run codesigning:
    #   - Strip signatures
    #   - Add ad-hoc signature for arm64
    #   - or sign using given identity (with signing modes!)
    #     only allowed for "standalone" bundles.

    make_readonly(root, bundle, progress)

    # XXX: The information is printed *before* the progress bars, not after
    architecture, deployment_target, warnings = audit_macho_issues(root)
    progress.info(f"Common architectures: {architecture}")
    progress.info(f"Deployment target: macOS {deployment_target}")
    progress.info("")
    for w in warnings:
        progress.warning(w)

    # XXX: Print summary about the bundle
