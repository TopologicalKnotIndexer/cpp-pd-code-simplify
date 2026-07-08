#!/usr/bin/env python3
"""Cross-platform build and packaging helper for the C++ project."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]


def host_platform() -> str:
    system = platform.system().lower()
    if system.startswith("windows") or os.name == "nt":
        return "windows"
    if system == "darwin":
        return "macos"
    return "linux"


def executable_suffix() -> str:
    return ".exe" if host_platform() == "windows" else ""


def shared_library_name() -> str:
    current = host_platform()
    if current == "windows":
        return "pdcode_simplify.dll"
    if current == "macos":
        return "libpdcode_simplify.dylib"
    return "libpdcode_simplify.so"


def default_compiler() -> str:
    env_cxx = os.environ.get("CXX")
    if env_cxx:
        return env_cxx
    if host_platform() == "windows":
        return shutil.which("g++") or shutil.which("clang++") or shutil.which("cl") or "g++"
    return shutil.which("c++") or shutil.which("g++") or shutil.which("clang++") or "c++"


def is_msvc(cxx: str) -> bool:
    name = Path(cxx).name.lower()
    return name in {"cl", "cl.exe"} or name.startswith("cl.exe")


def compiler_path(cxx: str) -> Optional[Path]:
    found = shutil.which(cxx)
    if found:
        return Path(found)
    path = Path(cxx)
    if path.exists():
        return path
    return None


def run(command: Sequence[str]) -> None:
    print("+ " + " ".join(str(part) for part in command), flush=True)
    subprocess.run([str(part) for part in command], cwd=str(ROOT), check=True)


def config_flags(config: str, msvc: bool) -> List[str]:
    if config.lower() == "debug":
        return ["/Od", "/Zi"] if msvc else ["-O0", "-g"]
    return ["/O2", "/DNDEBUG"] if msvc else ["-O3", "-DNDEBUG"]


def common_flags(config: str, msvc: bool) -> List[str]:
    if msvc:
        return ["/std:c++14", "/EHsc", "/Iinclude"] + config_flags(config, msvc)
    return [
        "-std=c++14",
        "-Wall",
        "-Wextra",
        "-Wpedantic",
        "-Iinclude",
    ] + config_flags(config, msvc)


def ensure_dirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def remove_tree(path: Path) -> None:
    resolved = path.resolve()
    root = ROOT.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"Refusing to remove path outside the repository: {path}")
    if path.exists():
        shutil.rmtree(path)


def build_executable(args: argparse.Namespace) -> Path:
    cxx = args.cxx
    msvc = is_msvc(cxx)
    build_dir = Path(args.build_dir)
    bin_dir = build_dir / "bin"
    ensure_dirs(bin_dir)
    output = bin_dir / ("pd_simplify" + executable_suffix())

    if msvc:
        run(
            [
                cxx,
                *common_flags(args.config, msvc),
                "src\\pdcode_simplify.cpp",
                "src\\main.cpp",
                f"/Fe:{output}",
            ]
        )
    else:
        run(
            [
                cxx,
                *common_flags(args.config, msvc),
                "src/pdcode_simplify.cpp",
                "src/main.cpp",
                "-o",
                str(output),
            ]
        )
    return output


def build_tests(args: argparse.Namespace) -> Path:
    cxx = args.cxx
    msvc = is_msvc(cxx)
    build_dir = Path(args.build_dir)
    bin_dir = build_dir / "bin"
    ensure_dirs(bin_dir)
    output = bin_dir / ("pdcode_simplify_tests" + executable_suffix())

    if msvc:
        run(
            [
                cxx,
                *common_flags(args.config, msvc),
                "src\\pdcode_simplify.cpp",
                "tests\\test_pdcode_simplify.cpp",
                f"/Fe:{output}",
            ]
        )
    else:
        run(
            [
                cxx,
                *common_flags(args.config, msvc),
                "src/pdcode_simplify.cpp",
                "tests/test_pdcode_simplify.cpp",
                "-o",
                str(output),
            ]
        )
    return output


def build_shared_library(args: argparse.Namespace) -> Path:
    cxx = args.cxx
    msvc = is_msvc(cxx)
    current = host_platform()
    build_dir = Path(args.build_dir)
    lib_dir = build_dir / "lib"
    ensure_dirs(lib_dir)
    output = lib_dir / shared_library_name()

    if msvc:
        import_lib = lib_dir / "pdcode_simplify.lib"
        run(
            [
                cxx,
                *common_flags(args.config, msvc),
                "/DPDCODE_SIMPLIFY_BUILD_SHARED",
                "/LD",
                "src\\pdcode_simplify.cpp",
                f"/Fe:{output}",
                "/link",
                f"/IMPLIB:{import_lib}",
            ]
        )
    else:
        command = [
            cxx,
            *common_flags(args.config, msvc),
            "-DPDCODE_SIMPLIFY_BUILD_SHARED",
            "src/pdcode_simplify.cpp",
        ]
        if current != "windows":
            command.append("-fPIC")
        if current == "macos":
            command.extend(["-dynamiclib", "-install_name", "@rpath/libpdcode_simplify.dylib"])
        else:
            command.append("-shared")
        if current == "windows":
            command.extend(["-Wl,--out-implib," + str(lib_dir / "libpdcode_simplify.dll.a")])
        command.extend(["-o", str(output)])
        run(command)
    return output


def run_tests(args: argparse.Namespace) -> None:
    build_executable(args)
    test_app = build_tests(args)
    run([str(test_app)])


def copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def copy_artifacts(source_dir: Path, destination_dir: Path) -> None:
    if not source_dir.exists():
        return
    destination_dir.mkdir(parents=True, exist_ok=True)
    for path in source_dir.iterdir():
        if path.is_file():
            shutil.copy2(path, destination_dir / path.name)


def copy_file_to_dir(source: Path, destination_dir: Path) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination_dir / source.name)


def parse_ldd_line(line: str) -> Optional[Path]:
    if "=>" in line:
        candidate = line.split("=>", 1)[1].strip().split(" ", 1)[0]
    else:
        candidate = line.strip().split(" ", 1)[0]
    if candidate and candidate != "not":
        path = Path(candidate)
        if path.is_absolute() and path.exists():
            return path
    return None


def is_system_library(path: Path) -> bool:
    normalized = str(path).replace("\\", "/")
    system_prefixes = (
        "/lib/",
        "/lib64/",
        "/usr/lib/",
        "/usr/lib64/",
        "/System/Library/",
        "/usr/lib/system/",
    )
    return normalized.startswith(system_prefixes)


def local_dependencies_unix(artifact: Path) -> List[Path]:
    current = host_platform()
    command = ["otool", "-L", str(artifact)] if current == "macos" else ["ldd", str(artifact)]
    try:
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        return []
    dependencies: List[Path] = []
    for line in proc.stdout.splitlines():
        candidate = parse_ldd_line(line)
        if candidate is not None and not is_system_library(candidate):
            dependencies.append(candidate)
    return dependencies


def copy_windows_runtime_dlls(cxx: str, destinations: Iterable[Path]) -> None:
    if host_platform() != "windows":
        return
    compiler = compiler_path(cxx)
    search_dirs: List[Path] = []
    if compiler is not None:
        search_dirs.append(compiler.parent)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            search_dirs.append(Path(entry))

    names = [
        "libstdc++-6.dll",
        "libgcc_s_seh-1.dll",
        "libgcc_s_dw2-1.dll",
        "libgcc_s_sjlj-1.dll",
        "libwinpthread-1.dll",
    ]
    copied = set()
    for name in names:
        source = next((directory / name for directory in search_dirs if (directory / name).exists()), None)
        if source is None:
            continue
        for destination in destinations:
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / name
            key = str(target.resolve())
            if key not in copied:
                shutil.copy2(source, target)
                copied.add(key)


def copy_local_runtime_dependencies(artifacts: Iterable[Path], destinations: Iterable[Path]) -> None:
    if host_platform() == "windows":
        return
    destination_list = list(destinations)
    for artifact in artifacts:
        for dependency in local_dependencies_unix(artifact):
            for destination in destination_list:
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dependency, destination / dependency.name)


def package_project(args: argparse.Namespace) -> Path:
    executable = build_executable(args)
    shared_library = build_shared_library(args)
    if args.run_tests:
        run_tests(args)

    package_dir = Path(args.package_dir)
    remove_tree(package_dir)
    ensure_dirs(package_dir)

    copy_file_to_dir(executable, package_dir / "bin")
    copy_artifacts(Path(args.build_dir) / "lib", package_dir / "lib")
    copy_tree(ROOT / "include", package_dir / "include")
    copy_tree(ROOT / "docs", package_dir / "docs")
    shutil.copy2(ROOT / "README.md", package_dir / "README.md")
    shutil.copy2(ROOT / "LICENSE", package_dir / "LICENSE")

    copy_windows_runtime_dlls(args.cxx, [package_dir / "bin", package_dir / "lib"])
    copy_local_runtime_dependencies(
        [executable, shared_library],
        [package_dir / "bin", package_dir / "lib"],
    )

    manifest = {
        "name": "cpp-pd-code-simplify",
        "platform": host_platform(),
        "machine": platform.machine(),
        "compiler": args.cxx,
        "config": args.config,
        "executable": "bin/" + executable.name,
        "shared_library": "lib/" + shared_library.name,
    }
    (package_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Packaged {package_dir}")
    return package_dir


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--build-dir", default="build", help="intermediate build directory")
    parser.add_argument("--config", default="release", choices=["release", "debug"], help="build configuration")
    parser.add_argument("--cxx", default=default_compiler(), help="C++14 compiler command")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")

    for command in ("build", "test"):
        sub = subparsers.add_parser(command)
        add_common_options(sub)

    package = subparsers.add_parser("package")
    add_common_options(package)
    package.add_argument("--package-dir", default="dist/cpp-pd-code-simplify", help="output package directory")
    package.add_argument("--run-tests", action="store_true", help="run unit tests before staging the package")

    clean = subparsers.add_parser("clean")
    clean.add_argument("--build-dir", default="build")
    clean.add_argument("--package-dir", default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "build"

    if command == "build":
        build_executable(args)
        build_shared_library(args)
    elif command == "test":
        run_tests(args)
    elif command == "package":
        package_project(args)
    elif command == "clean":
        remove_tree(Path(args.build_dir))
        if args.package_dir is not None:
            remove_tree(Path(args.package_dir))
    else:
        parser.error(f"unknown command: {command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
