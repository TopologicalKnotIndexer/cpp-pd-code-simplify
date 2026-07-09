from __future__ import annotations

import argparse
import ast
import contextlib
import ctypes
import hashlib
import json
import os
import pathlib
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
from importlib import resources
from typing import Any, Optional, Sequence, Union

import cpp_simple_interface


PdInput = Union[str, Sequence[Sequence[int]]]
PdManyInput = Union[str, Sequence[PdInput]]


class PdCodeSimplifyInterfaceError(RuntimeError):
    """Raised when the C++ dynamic library cannot be built or called."""


def _format_pd(crossings: Sequence[Sequence[int]]) -> str:
    parts = []
    for crossing in crossings:
        values = list(crossing)
        if len(values) != 4:
            raise ValueError(f"PD crossing must have four entries: {crossing!r}")
        parts.append("X[{},{},{},{}]".format(*(int(value) for value in values)))
    return "PD[" + ",".join(parts) + "]"


def _parse_x_crossings(text: str) -> Optional[list[list[int]]]:
    pattern = r"X\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]"
    crossings = []
    for match in re.finditer(pattern, text):
        crossings.append([int(match.group(i)) for i in range(1, 5)])
    return crossings if crossings else None


def _as_crossings(pd_code: PdInput) -> list[list[int]]:
    if isinstance(pd_code, str):
        body = pd_code.strip()
        if ":" in body:
            body = body.split(":", 1)[1].strip()
        if body.replace(" ", "") in ("PD[]", "[]"):
            return []

        parsed = _parse_x_crossings(body)
        if parsed is not None:
            return parsed

        try:
            value = ast.literal_eval(body)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"unsupported PD-code string format: {pd_code!r}") from exc
    else:
        value = pd_code

    crossings = []
    for crossing in value:
        values = list(crossing)
        if len(values) != 4:
            raise ValueError(f"PD crossing must have four entries: {crossing!r}")
        crossings.append([int(item) for item in values])
    return crossings


def normalize_pd_code(pd_code: PdInput) -> str:
    """Normalize a supported PD-code value into standard ``PD[X[...],...]`` text."""

    return _format_pd(_as_crossings(pd_code))


def normalize_pd_codes(pd_codes: PdManyInput) -> list[str]:
    """Normalize one or more PD codes into standard ``PD[X[...],...]`` strings."""

    if isinstance(pd_codes, str):
        return [line.strip() for line in pd_codes.splitlines() if line.strip()]
    return [normalize_pd_code(pd_code) for pd_code in pd_codes]


def _label_for_line_prefix(before_block: str, label_prefix: str, index: int) -> str:
    if ":" in before_block:
        line_label = before_block.split(":", 1)[0].strip()
        if line_label:
            return f"{label_prefix}:{line_label}"
    if index == 0:
        return label_prefix
    return f"{label_prefix}#{index + 1}"


def _pd_file_jobs(path: str) -> list[tuple[str, str]]:
    jobs: list[tuple[str, str]] = []
    fallback_jobs: list[tuple[str, str]] = []
    current_label: Optional[str] = None
    current_block: list[str] = []
    bracket_depth = 0

    with pathlib.Path(path).open("r", encoding="utf-8") as input_file:
        for line in input_file:
            cleaned = line.strip()
            if not jobs and not current_block and cleaned and not cleaned.startswith("#"):
                label = path
                payload = cleaned
                if ":" in cleaned:
                    line_label, payload = cleaned.split(":", 1)
                    line_label = line_label.strip()
                    payload = payload.strip()
                    if line_label:
                        label = f"{path}:{line_label}"
                elif fallback_jobs:
                    label = f"{path}#{len(fallback_jobs) + 1}"
                fallback_jobs.append((label, payload))

            cursor = 0
            while cursor < len(line):
                if current_block:
                    char = line[cursor]
                    current_block.append(char)
                    if char == "[":
                        bracket_depth += 1
                    elif char == "]":
                        bracket_depth -= 1
                        if bracket_depth == 0:
                            jobs.append((current_label or f"{path}#{len(jobs) + 1}", "".join(current_block)))
                            current_label = None
                            current_block = []
                    cursor += 1
                    continue

                start = line.find("PD[", cursor)
                if start == -1:
                    break
                current_label = _label_for_line_prefix(line[:start], path, len(jobs))
                current_block = ["PD["]
                bracket_depth = 1
                cursor = start + 3

    if jobs:
        if current_block:
            jobs.append((f"{path}#{len(jobs) + 1}", "".join(current_block).strip()))
        if len(jobs) == 1:
            jobs[0] = (path, jobs[0][1])
        return jobs

    if current_block:
        fallback_jobs.append((f"{path}#{len(fallback_jobs) + 1}", "".join(current_block).strip()))
    return fallback_jobs


@contextlib.contextmanager
def _resource_paths():
    package = "cpp_pd_code_simplify_interface"
    resource_names = [
        resources.files(package) / "data" / "src" / "pdcode_simplify.cpp",
        resources.files(package) / "data" / "src" / "native_interface.cpp",
        resources.files(package) / "data" / "include" / "pdcode_simplify" / "pdcode_simplify.hpp",
    ]

    contexts = []
    paths: list[pathlib.Path] = []
    try:
        for resource in resource_names:
            context = resources.as_file(resource)
            contexts.append(context)
            path = pathlib.Path(context.__enter__())
            if not path.exists():
                break
            paths.append(path)
        if len(paths) == len(resource_names):
            yield paths
            return
    except FileNotFoundError:
        pass
    finally:
        while contexts:
            contexts.pop().__exit__(None, None, None)

    current = pathlib.Path(__file__).resolve()
    for parent in current.parents:
        candidate_cpp = parent / "src" / "pdcode_simplify.cpp"
        candidate_wrapper = (
            parent
            / "python_project"
            / "cpp-pd-code-simplify-interface"
            / "cpp_pd_code_simplify_interface"
            / "data"
            / "src"
            / "native_interface.cpp"
        )
        candidate_header = parent / "include" / "pdcode_simplify" / "pdcode_simplify.hpp"
        if candidate_cpp.exists() and candidate_wrapper.exists() and candidate_header.exists():
            yield [candidate_cpp, candidate_wrapper, candidate_header]
            return

    raise PdCodeSimplifyInterfaceError(
        "cpp-pd-code-simplify C++ sources were not found. Installed wheels "
        "include them under cpp_pd_code_simplify_interface/data; editable "
        "checkouts use the repository root src/ and include/ directories."
    )


def _cache_dir() -> pathlib.Path:
    env_value = os.environ.get("CPP_PD_CODE_SIMPLIFY_INTERFACE_CACHE_DIR")
    if env_value:
        root = pathlib.Path(env_value)
    elif sys.platform == "win32":
        root = pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home())) / "cpp-pd-code-simplify-interface"
    elif sys.platform == "darwin":
        root = pathlib.Path.home() / "Library" / "Caches" / "cpp-pd-code-simplify-interface"
    else:
        root = pathlib.Path(os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")) / "cpp-pd-code-simplify-interface"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _library_suffix() -> str:
    if platform.system() == "Windows":
        return ".dll"
    if platform.system() == "Darwin":
        return ".dylib"
    return ".so"


def _default_compile_flags(include_dir: pathlib.Path, library_path: pathlib.Path) -> list[str]:
    flags = ["-std=c++14", "-O3", "-DNDEBUG", "-I" + str(include_dir)]
    system = platform.system()
    if system != "Windows":
        flags.append("-fPIC")
    if system == "Darwin":
        flags.extend([
            "-dynamiclib",
            "-install_name",
            "@rpath/" + library_path.name,
            "-Wl,-rpath,@loader_path",
        ])
    else:
        flags.append("-shared")
        if system == "Linux":
            flags.append("-Wl,-rpath,$ORIGIN")
    native = os.environ.get("CPP_PD_CODE_SIMPLIFY_INTERFACE_NATIVE", "1").strip().lower()
    if native not in ("0", "false", "no", "off"):
        flags.append("-march=native")
    extra = os.environ.get("CPP_PD_CODE_SIMPLIFY_INTERFACE_CXXFLAGS", "").strip()
    if extra:
        flags.extend(shlex.split(extra))
    return flags


def _cache_key(source_bytes: bytes, flags: Sequence[str]) -> str:
    digest = hashlib.sha256()
    digest.update(source_bytes)
    digest.update("\0".join(flags).encode("utf-8"))
    digest.update(cpp_simple_interface.get_gpp_filepath().encode("utf-8"))
    digest.update(_runtime_fingerprint())
    digest.update(platform.platform().encode("utf-8"))
    return digest.hexdigest()[:20]


def _compiler_runtime_path_entries() -> list[pathlib.Path]:
    compiler = cpp_simple_interface.get_gpp_filepath().strip()
    if not compiler:
        return []

    candidates = []
    unquoted = compiler
    if len(unquoted) >= 2 and unquoted[0] == unquoted[-1] and unquoted[0] in ("'", '"'):
        unquoted = unquoted[1:-1]
    candidates.append(unquoted)

    try:
        candidates.extend(shlex.split(compiler, posix=True))
    except ValueError:
        pass

    paths: list[pathlib.Path] = []
    for candidate in candidates:
        path = pathlib.Path(candidate)
        if path.exists() and path.is_file() and path.parent not in paths:
            paths.append(path.parent)
            continue
        resolved = shutil.which(candidate)
        if resolved:
            resolved_path = pathlib.Path(resolved)
            if resolved_path.exists() and resolved_path.parent not in paths:
                paths.append(resolved_path.parent)
    return paths


def _path_entries() -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        path = pathlib.Path(entry)
        if path.exists() and path.is_dir() and path not in paths:
            paths.append(path)
    return paths


def _tool_path(names: Sequence[str], extra_dirs: Sequence[pathlib.Path] = ()) -> Optional[str]:
    for directory in extra_dirs:
        for name in names:
            candidate = directory / name
            if candidate.exists() and candidate.is_file():
                return str(candidate)
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return None


def _run_dependency_tool(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return ""
    if result.returncode != 0 and not result.stdout:
        return ""
    return result.stdout


def _objdump_dependency_names(path: pathlib.Path) -> list[str]:
    tool = _tool_path(["objdump.exe", "objdump"], _compiler_runtime_path_entries())
    if not tool:
        return []
    output = _run_dependency_tool([tool, "-p", str(path)])
    names: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("DLL Name:"):
            names.append(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("NEEDED"):
            parts = stripped.split()
            if len(parts) >= 2:
                names.append(parts[-1])
    return names


def _dumpbin_dependency_names(path: pathlib.Path) -> list[str]:
    tool = _tool_path(["dumpbin.exe", "dumpbin"])
    if not tool:
        return []
    output = _run_dependency_tool([tool, "/DEPENDENTS", str(path)])
    names: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.lower().endswith(".dll"):
            names.append(stripped)
    return names


def _ldd_dependency_paths(path: pathlib.Path) -> tuple[list[pathlib.Path], list[str]]:
    tool = _tool_path(["ldd"])
    if not tool:
        return [], []
    output = _run_dependency_tool([tool, str(path)])
    paths: list[pathlib.Path] = []
    missing: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if "not found" in stripped:
            missing.append(stripped.split()[0])
            continue
        candidate = ""
        if "=>" in stripped:
            right = stripped.split("=>", 1)[1].strip()
            candidate = right.split("(", 1)[0].strip()
        elif stripped.startswith("/"):
            candidate = stripped.split("(", 1)[0].strip()
        if candidate:
            dependency = pathlib.Path(candidate)
            if dependency.exists() and dependency not in paths:
                paths.append(dependency)
    return paths, missing


def _otool_dependency_paths(path: pathlib.Path) -> list[pathlib.Path]:
    tool = _tool_path(["otool"])
    if not tool:
        return []
    output = _run_dependency_tool([tool, "-L", str(path)])
    paths: list[pathlib.Path] = []
    for line in output.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        name = stripped.split(" ", 1)[0]
        dependency: Optional[pathlib.Path] = None
        if name.startswith("/"):
            dependency = pathlib.Path(name)
        elif name.startswith("@loader_path/"):
            dependency = path.parent / name.removeprefix("@loader_path/")
        elif name.startswith("@rpath/"):
            dependency = _find_dependency_file(
                pathlib.Path(name).name,
                _runtime_search_dirs(path.parent),
            )
        if dependency is not None and dependency.exists() and dependency not in paths:
            paths.append(dependency)
    return paths


def _is_platform_library(path: pathlib.Path) -> bool:
    if platform.system() == "Darwin":
        text = str(path)
        return text.startswith("/usr/lib/") or text.startswith("/System/Library/")
    return False


def _runtime_basename_is_cacheable(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered.startswith("libstdc++")
        or lowered.startswith("libgcc_s")
        or lowered.startswith("libwinpthread")
        or lowered.startswith("libgomp")
        or lowered.startswith("libquadmath")
        or lowered.startswith("libatomic")
        or lowered.startswith("libc++")
        or lowered.startswith("libc++abi")
    )


def _runtime_search_dirs(cache_dir: pathlib.Path) -> list[pathlib.Path]:
    directories = [cache_dir, *_compiler_runtime_path_entries(), *_path_entries()]
    result: list[pathlib.Path] = []
    for directory in directories:
        if directory.exists() and directory.is_dir() and directory not in result:
            result.append(directory)
    return result


def _find_dependency_file(name: str, directories: Sequence[pathlib.Path]) -> Optional[pathlib.Path]:
    wanted = name.lower()
    for directory in directories:
        candidate = directory / name
        if candidate.exists() and candidate.is_file():
            return candidate
        try:
            for item in directory.iterdir():
                if item.is_file() and item.name.lower() == wanted:
                    return item
        except OSError:
            continue
    return None


def _copy_file_if_needed(source: pathlib.Path, destination: pathlib.Path) -> None:
    try:
        if destination.exists() and source.resolve() == destination.resolve():
            return
    except OSError:
        pass
    copy_needed = True
    if destination.exists():
        try:
            source_stat = source.stat()
            destination_stat = destination.stat()
            copy_needed = (
                source_stat.st_size != destination_stat.st_size
                or source_stat.st_mtime_ns != destination_stat.st_mtime_ns
            )
        except OSError:
            copy_needed = True
    if copy_needed:
        shutil.copy2(source, destination)


def _windows_dependency_names(path: pathlib.Path) -> list[str]:
    names = _objdump_dependency_names(path) or _dumpbin_dependency_names(path)
    if names:
        return names
    return [
        "libstdc++-6.dll",
        "libgcc_s_seh-1.dll",
        "libgcc_s_sjlj-1.dll",
        "libgcc_s_dw2-1.dll",
        "libwinpthread-1.dll",
    ]


def _runtime_fingerprint() -> bytes:
    digest = hashlib.sha256()
    if platform.system() == "Windows":
        names = _windows_dependency_names(pathlib.Path("pdcode-simplify-placeholder.dll"))
    else:
        names = [
            "libstdc++.so",
            "libstdc++.so.6",
            "libgcc_s.so",
            "libgcc_s.so.1",
            "libc++.dylib",
            "libc++abi.dylib",
        ]
    for directory in _compiler_runtime_path_entries():
        for name in names:
            candidate = directory / name
            if not candidate.exists():
                continue
            try:
                stat = candidate.stat()
            except OSError:
                continue
            digest.update(str(candidate).encode("utf-8", errors="replace"))
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return digest.digest()


def _cache_runtime_dependencies(library: pathlib.Path) -> None:
    system = platform.system()
    if system == "Windows":
        search_dirs = _runtime_search_dirs(library.parent)
        pending = list(_windows_dependency_names(library))
        visited: set[str] = set()
        while pending:
            name = pending.pop(0)
            key = name.lower()
            if key in visited:
                continue
            visited.add(key)
            if not _runtime_basename_is_cacheable(name):
                continue
            source = _find_dependency_file(name, search_dirs)
            if source is None:
                continue
            destination = library.parent / source.name
            _copy_file_if_needed(source, destination)
            for imported in _windows_dependency_names(destination):
                if imported.lower() not in visited:
                    pending.append(imported)
        return

    if system == "Linux":
        paths, _ = _ldd_dependency_paths(library)
    elif system == "Darwin":
        paths = _otool_dependency_paths(library)
    else:
        paths = []

    for dependency in paths:
        if _is_platform_library(dependency):
            continue
        if _runtime_basename_is_cacheable(dependency.name):
            _copy_file_if_needed(dependency, library.parent / dependency.name)


def _cached_runtime_dependencies(library: pathlib.Path) -> list[pathlib.Path]:
    result: list[pathlib.Path] = []
    try:
        items = list(library.parent.iterdir())
    except OSError:
        return result
    for item in items:
        if item == library or not item.is_file():
            continue
        if _runtime_basename_is_cacheable(item.name):
            result.append(item)
    return result


def _missing_runtime_dependency_names(library: pathlib.Path) -> list[str]:
    system = platform.system()
    if system == "Windows":
        search_dirs = _runtime_search_dirs(library.parent)
        missing = []
        for name in _windows_dependency_names(library):
            if _runtime_basename_is_cacheable(name) and _find_dependency_file(name, search_dirs) is None:
                missing.append(name)
        return missing
    if system == "Linux":
        _, missing = _ldd_dependency_paths(library)
        return missing
    return []


def _load_failure_message(path: pathlib.Path, error: OSError) -> str:
    system = platform.system()
    missing = _missing_runtime_dependency_names(path)
    details = f"failed to load cached dynamic library {path}: {error}"
    if missing:
        details += "; missing dependencies: " + ", ".join(sorted(set(missing)))
    if system == "Windows":
        details += (
            ". The interface caches MinGW runtime DLLs next to the generated DLL; "
            "if this cache was created by an older package version, delete the cache directory "
            f"{path.parent} or call compile_simplifier(force=True)."
        )
    elif system == "Linux":
        details += (
            ". Ensure the compiler runtime is installed, or set LD_LIBRARY_PATH before "
            "starting Python when using a custom compiler runtime."
        )
    elif system == "Darwin":
        details += (
            ". Ensure the compiler runtime is installed, or set DYLD_LIBRARY_PATH before "
            "starting Python when using a custom compiler runtime."
        )
    return details


def _pe_machine_bits(path: pathlib.Path) -> Optional[int]:
    if platform.system() != "Windows":
        return None
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if len(data) < pe_offset + 6 or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        return None
    machine = struct.unpack_from("<H", data, pe_offset + 4)[0]
    if machine == 0x014C:
        return 32
    if machine in (0x8664, 0xAA64):
        return 64
    return None


def _validate_library_architecture(path: pathlib.Path) -> None:
    bits = _pe_machine_bits(path)
    if bits is None:
        return
    python_bits = struct.calcsize("P") * 8
    if bits != python_bits:
        raise PdCodeSimplifyInterfaceError(
            f"cached library is {bits}-bit but Python is {python_bits}-bit. "
            "Set CXX to a compiler whose target architecture matches Python, "
            "then delete the interface cache or call compile_simplifier(force=True)."
        )


def compile_simplifier(
    *,
    force: bool = False,
    cxx: Optional[str] = None,
    extra_flags: Optional[Sequence[str]] = None,
) -> pathlib.Path:
    """Compile the packaged C++ source as a cached dynamic library."""

    if cxx:
        cpp_simple_interface.set_gpp_filepath(cxx)

    with _resource_paths() as paths:
        pd_source, wrapper_source, header = paths
        include_dir = header.parents[1]
        source_bytes = pd_source.read_bytes() + b"\0" + wrapper_source.read_bytes() + b"\0" + header.read_bytes()

        cache = _cache_dir()
        placeholder = cache / ("pdcode-simplify-placeholder" + _library_suffix())
        flags = _default_compile_flags(include_dir, placeholder)
        if extra_flags:
            flags.extend(str(flag) for flag in extra_flags)
        library = cache / f"pdcode-simplify-{_cache_key(source_bytes, flags)}{_library_suffix()}"
        flags = _default_compile_flags(include_dir, library)
        if extra_flags:
            flags.extend(str(flag) for flag in extra_flags)

        if library.exists() and not force:
            _cache_runtime_dependencies(library)
            return library

        tmp_library = cache / f"{library.name}.tmp-{os.getpid()}{_library_suffix()}"
        if tmp_library.exists():
            tmp_library.unlink()

        success, message = cpp_simple_interface.compile_cpp_files(
            [str(pd_source), str(wrapper_source)],
            str(tmp_library),
            other_flags=flags,
        )
        if not success and "-march=native" in flags:
            fallback_flags = [flag for flag in flags if flag != "-march=native"]
            success, message = cpp_simple_interface.compile_cpp_files(
                [str(pd_source), str(wrapper_source)],
                str(tmp_library),
                other_flags=fallback_flags,
            )

        if not success:
            raise PdCodeSimplifyInterfaceError(message)
        if not tmp_library.exists():
            raise PdCodeSimplifyInterfaceError(f"compiled dynamic library was not created: {tmp_library}")
        os.replace(tmp_library, library)
        _cache_runtime_dependencies(library)
        return library


def get_simplifier_library() -> pathlib.Path:
    """Return the cached dynamic library path, compiling it first when necessary."""

    return compile_simplifier()


def get_simplifier_executable() -> pathlib.Path:
    """Backward-compatible alias returning the cached dynamic library path."""

    return get_simplifier_library()


_LOADED_LIBRARY_PATH: Optional[pathlib.Path] = None
_LOADED_LIBRARY: Optional[ctypes.CDLL] = None
_DLL_DIRECTORY_HANDLES: list[Any] = []


def _prepare_dll_search_path(path: pathlib.Path) -> None:
    if platform.system() != "Windows" or not hasattr(os, "add_dll_directory"):
        return
    for directory in _runtime_search_dirs(path.parent):
        try:
            handle = os.add_dll_directory(str(directory))
        except OSError:
            continue
        _DLL_DIRECTORY_HANDLES.append(handle)


def _preload_runtime_dependencies(path: pathlib.Path) -> None:
    if platform.system() == "Windows":
        return
    mode = getattr(ctypes, "RTLD_GLOBAL", 0)
    for dependency in _cached_runtime_dependencies(path):
        try:
            ctypes.CDLL(str(dependency), mode=mode)
        except OSError:
            continue


def _load_library() -> ctypes.CDLL:
    global _LOADED_LIBRARY_PATH, _LOADED_LIBRARY
    path = compile_simplifier()
    if _LOADED_LIBRARY is not None and _LOADED_LIBRARY_PATH == path:
        return _LOADED_LIBRARY

    _validate_library_architecture(path)
    _cache_runtime_dependencies(path)
    _prepare_dll_search_path(path)
    _preload_runtime_dependencies(path)
    try:
        library = ctypes.CDLL(str(path))
    except OSError as exc:
        raise PdCodeSimplifyInterfaceError(_load_failure_message(path, exc)) from exc
    library.pdcode_simplify_run_json.argtypes = [
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_ulonglong,
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_ulonglong,
    ]
    library.pdcode_simplify_run_json.restype = ctypes.c_void_p
    library.pdcode_simplify_free_string.argtypes = [ctypes.c_void_p]
    library.pdcode_simplify_free_string.restype = None
    _LOADED_LIBRARY_PATH = path
    _LOADED_LIBRARY = library
    return library


def _run_one_direct(
    pd_text: str,
    *,
    max_paths: int = -1,
    ban_heuristic: bool = False,
    reduction_round: int = -1,
    max_thread: int = -1,
    timeout: int = -1,
    verbose: bool = False,
    show_step_pd: bool = False,
    known_crossingless_components: int = 0,
    remove_crossings: Optional[Sequence[int]] = None,
) -> dict[str, Any]:
    if reduction_round < -1:
        raise ValueError("reduction_round must be -1 or a non-negative integer")
    if max_thread < -1 or max_thread == 0:
        raise ValueError("max_thread must be -1 or a positive integer")
    if timeout < -1 or timeout == 0:
        raise ValueError("timeout must be -1 or a positive integer")
    library = _load_library()
    removed_count = 0 if remove_crossings is None else len(remove_crossings)
    removed_array = None
    if removed_count:
        removed_array = (ctypes.c_int * removed_count)(*(int(value) for value in remove_crossings or []))

    pointer = library.pdcode_simplify_run_json(
        pd_text.encode("utf-8"),
        int(max_paths),
        1 if ban_heuristic else 0,
        int(reduction_round),
        int(max_thread),
        int(timeout),
        1 if verbose else 0,
        1 if show_step_pd else 0,
        int(known_crossingless_components),
        removed_array,
        int(removed_count),
    )
    if not pointer:
        raise PdCodeSimplifyInterfaceError("C++ interface returned a null JSON pointer")
    try:
        text = ctypes.string_at(pointer).decode("utf-8")
    finally:
        library.pdcode_simplify_free_string(pointer)

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PdCodeSimplifyInterfaceError(f"invalid simplifier JSON output: {text!r}") from exc
    if isinstance(result, dict) and "error" in result:
        raise PdCodeSimplifyInterfaceError(str(result["error"]))
    return result


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _run_one(
    pd_text: str,
    *,
    max_paths: int = -1,
    ban_heuristic: bool = False,
    reduction_round: int = -1,
    max_thread: int = -1,
    timeout: int = -1,
    verbose: bool = False,
    show_step_pd: bool = False,
    known_crossingless_components: int = 0,
    remove_crossings: Optional[Sequence[int]] = None,
) -> dict[str, Any]:
    if reduction_round < -1:
        raise ValueError("reduction_round must be -1 or a non-negative integer")
    if max_thread < -1 or max_thread == 0:
        raise ValueError("max_thread must be -1 or a positive integer")
    if timeout < -1 or timeout == 0:
        raise ValueError("timeout must be -1 or a positive integer")

    request = {
        "pd_text": pd_text,
        "max_paths": int(max_paths),
        "ban_heuristic": bool(ban_heuristic),
        "reduction_round": int(reduction_round),
        "max_thread": int(max_thread),
        "timeout": int(timeout),
        "verbose": bool(verbose),
        "show_step_pd": bool(show_step_pd),
        "known_crossingless_components": int(known_crossingless_components),
        "remove_crossings": [int(value) for value in remove_crossings or []],
    }
    protocol_output_path: Optional[pathlib.Path] = None
    if show_step_pd:
        fd, protocol_name = tempfile.mkstemp(
            prefix="cpp-pd-code-simplify-interface-",
            suffix=".json",
        )
        os.close(fd)
        protocol_output_path = pathlib.Path(protocol_name)
        request["protocol_output_path"] = str(protocol_output_path)
    env = os.environ.copy()
    project_root = str(pathlib.Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cpp_pd_code_simplify_interface._worker",
            ],
            cwd=str(pathlib.Path.cwd()),
            text=True,
            stdin=subprocess.PIPE,
            stdout=None if show_step_pd else subprocess.PIPE,
            stderr=None if verbose else subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = proc.communicate(json.dumps(request))
        except KeyboardInterrupt:
            _terminate_process(proc)
            raise

        if proc.returncode != 0:
            detail = (stderr or "").strip()
            raise PdCodeSimplifyInterfaceError(
                f"C++ interface worker failed with exit code {proc.returncode}"
                + (f": {detail}" if detail else "")
            )
        if protocol_output_path is not None:
            stdout = protocol_output_path.read_text(encoding="utf-8")
        try:
            envelope = json.loads(stdout or "")
        except json.JSONDecodeError as exc:
            raise PdCodeSimplifyInterfaceError(
                f"invalid interface worker JSON output: {stdout!r}"
            ) from exc
    finally:
        if protocol_output_path is not None:
            try:
                protocol_output_path.unlink()
            except FileNotFoundError:
                pass
    if not isinstance(envelope, dict):
        raise PdCodeSimplifyInterfaceError(
            f"invalid interface worker response type: {type(envelope)!r}"
        )
    if not envelope.get("ok", False):
        raise PdCodeSimplifyInterfaceError(str(envelope.get("error", "unknown worker error")))
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise PdCodeSimplifyInterfaceError("interface worker did not return a JSON object result")
    return result


def simplify(
    pd_code: PdInput,
    *,
    max_paths: int = -1,
    ban_heuristic: bool = False,
    reduction_round: int = -1,
    max_thread: int = -1,
    timeout: int = -1,
    verbose: bool = False,
    show_step_pd: bool = False,
    known_crossingless_components: int = 0,
    remove_crossings: Optional[Sequence[int]] = None,
) -> dict[str, Any]:
    """Run the C++ simplifier for one PD code and return its JSON result."""

    return _run_one(
        normalize_pd_code(pd_code),
        max_paths=max_paths,
        ban_heuristic=ban_heuristic,
        reduction_round=reduction_round,
        max_thread=max_thread,
        timeout=timeout,
        verbose=verbose,
        show_step_pd=show_step_pd,
        known_crossingless_components=known_crossingless_components,
        remove_crossings=remove_crossings,
    )


def simplify_many(
    pd_codes: PdManyInput,
    *,
    max_paths: int = -1,
    ban_heuristic: bool = False,
    reduction_round: int = -1,
    max_thread: int = -1,
    timeout: int = -1,
    verbose: bool = False,
    show_step_pd: bool = False,
    known_crossingless_components: int = 0,
    remove_crossings: Optional[Sequence[int]] = None,
) -> list[dict[str, Any]]:
    """Run the C++ simplifier for one or more PD codes and return JSON results."""

    return [
        _run_one(
            pd_text,
            max_paths=max_paths,
            ban_heuristic=ban_heuristic,
            reduction_round=reduction_round,
            max_thread=max_thread,
            timeout=timeout,
            verbose=verbose,
            show_step_pd=show_step_pd,
            known_crossingless_components=known_crossingless_components,
            remove_crossings=remove_crossings,
        )
        for pd_text in normalize_pd_codes(pd_codes)
    ]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run cpp-pd-code-simplify through the Python interface.")
    parser.add_argument("pd_code", nargs="?", help="PD code as PD[...] text or a Python-style list of crossings.")
    parser.add_argument("--pd-code", "-c", dest="pd_code_option", help="literal PD[...] string")
    parser.add_argument("--pd-file", "-f", help="read one file containing one or more labelled PD-code lines")
    parser.add_argument("--max-paths", type=int, default=-1)
    parser.add_argument("--ban-heuristic", action="store_true")
    parser.add_argument("--reduction-round", type=int, default=-1)
    parser.add_argument("--max-thread", type=int, default=-1)
    parser.add_argument("--timeout", type=int, default=-1)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--show-step-pd", action="store_true")
    parser.add_argument("--known-crossingless-components", type=int, default=0)
    parser.add_argument("--remove-crossings", help="comma-separated zero-based crossing indices")
    args = parser.parse_args(argv)
    if args.reduction_round < -1:
        parser.error("--reduction-round must be -1 or a non-negative integer")
    if args.max_thread < -1 or args.max_thread == 0:
        parser.error("--max-thread must be -1 or a positive integer")
    if args.timeout < -1 or args.timeout == 0:
        parser.error("--timeout must be -1 or a positive integer")
    if args.pd_code and args.pd_code_option:
        parser.error("pass either a positional PD code or --pd-code, not both")
    pd_code_text = args.pd_code_option or args.pd_code
    if args.pd_file and pd_code_text:
        parser.error("pass either a PD code or --pd-file, not both")
    if not args.pd_file and not pd_code_text:
        parser.error("a PD code or --pd-file is required")
    remove_crossings = None
    if args.remove_crossings:
        remove_crossings = [int(token) for token in re.findall(r"-?\d+", args.remove_crossings)]

    exit_code = 0
    if args.pd_file:
        try:
            jobs = _pd_file_jobs(args.pd_file)
        except KeyboardInterrupt:
            print(json.dumps({"error": "interrupted by Ctrl+C"}, indent=2))
            return 130
        except Exception as exc:
            print(json.dumps({"error": str(exc)}, indent=2))
            return 2
        batch_payload = []
        show_labels = len(jobs) > 1
        for label, line in jobs:
            try:
                item = simplify(
                    line,
                    max_paths=args.max_paths,
                    ban_heuristic=args.ban_heuristic,
                    reduction_round=args.reduction_round,
                    max_thread=args.max_thread,
                    timeout=args.timeout,
                    verbose=args.verbose,
                    show_step_pd=args.show_step_pd,
                    known_crossingless_components=args.known_crossingless_components,
                    remove_crossings=remove_crossings,
                )
                if show_labels:
                    item = {"label": label, **item}
                batch_payload.append(item)
                if item.get("timed_out"):
                    exit_code = 2
            except KeyboardInterrupt:
                exit_code = 130
                item = {"error": "interrupted by Ctrl+C"}
                if show_labels:
                    item = {"label": label, **item}
                batch_payload.append(item)
                break
            except Exception as exc:
                exit_code = 2
                item = {"error": str(exc)}
                if show_labels:
                    item = {"label": label, **item}
                batch_payload.append(item)
        payload: Any = batch_payload
    else:
        try:
            payload = simplify(
                pd_code_text or "",
                max_paths=args.max_paths,
                ban_heuristic=args.ban_heuristic,
                reduction_round=args.reduction_round,
                max_thread=args.max_thread,
                timeout=args.timeout,
                verbose=args.verbose,
                show_step_pd=args.show_step_pd,
                known_crossingless_components=args.known_crossingless_components,
                remove_crossings=remove_crossings,
            )
            if isinstance(payload, dict) and payload.get("timed_out"):
                exit_code = 2
        except KeyboardInterrupt:
            exit_code = 130
            payload = {"error": "interrupted by Ctrl+C"}
        except Exception as exc:
            exit_code = 2
            payload = {"error": str(exc)}

    print(json.dumps(payload, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
