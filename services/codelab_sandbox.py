"""Runs model-written reference solutions for generated CodeLab problems.

Only code this backend asked an LLM to write is ever run here - learner
submissions never are (those are judged in the browser). The model's code is
still not trusted, so there are two independent layers:

1. A static allowlist check: plain Python, a handful of pure standard-library
   modules, no file/network/process access, and no way to reach an attribute
   by a name held in a string.
2. A separate, isolated interpreter with a hard time limit, an empty
   environment, a throwaway working directory, bounded output, and - where
   the OS supports it (POSIX) - a memory cap and a file-descriptor limit that
   makes opening any file or socket impossible even if layer 1 were bypassed.
"""

import ast
import asyncio
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass

ALLOWED_MODULES = frozenset(
    {"math", "itertools", "collections", "functools", "heapq", "bisect", "string", "re", "sys", "fractions"}
)
# The only things a solution needs from sys. Everything else on it
# (sys.modules, sys.path, ...) is a way out of the allowlist.
_ALLOWED_SYS_ATTRIBUTES = frozenset({"stdin", "stdout", "setrecursionlimit", "maxsize"})
# Includes everything that looks an attribute up by a *string* name
# (getattr, attrgetter, string.Formatter.get_field): with those gone, the
# attribute checks below cannot be sidestepped by building the name at runtime.
_FORBIDDEN_NAMES = frozenset(
    {
        "eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr", "setattr",
        "delattr", "breakpoint", "help", "exit", "quit", "memoryview", "__builtins__", "__loader__", "__spec__",
        "attrgetter", "methodcaller", "Formatter",
    }
)  # fmt: skip
# Attributes that reach interpreter internals from an ordinary object. Dunder
# and underscore-prefixed attributes are rejected wholesale below.
_FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "modules", "meta_path", "path_hooks", "path_importer_cache", "f_globals", "f_locals", "f_builtins",
        "f_back", "gi_frame", "cr_frame", "ag_frame", "tb_frame", "func_globals", "builtins", "system", "popen",
        "environ", "attrgetter", "methodcaller", "Formatter", "get_field", "vformat",
    }
)  # fmt: skip
_FORBIDDEN_NODES = (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.AsyncFor, ast.AsyncWith, ast.Await)
# A dunder name inside a string literal ("{0.__globals__}".format(f)) has no
# place in a solution; "__main__" is the one legitimate use.
_DUNDER_IN_STRING = re.compile(r"__[A-Za-z]+__")

MAX_SOURCE_BYTES = 20_000
MAX_STDIN_BYTES = 50_000  # comfortably under the OS pipe buffer, so writing it can never block
MAX_OUTPUT_BYTES = 20_000
MAX_STDERR_BYTES = 20_000
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
_READ_CHUNK_BYTES = 8192


class UnsafeCodeError(Exception):
    """The code uses something outside the allowlist and was not run."""


@dataclass(frozen=True)
class RunResult:
    ok: bool  # exited 0 within the time limit, with output under the cap
    stdout: str
    error: str = ""  # short reason when ok is False


def check_code_is_safe(source: str) -> None:
    """Raises UnsafeCodeError unless `source` is plain, allowlisted Python."""
    if len(source.encode("utf-8", errors="replace")) > MAX_SOURCE_BYTES:
        raise UnsafeCodeError("code is too long")
    try:
        tree = ast.parse(source)
        nodes = list(ast.walk(tree))
    except Exception as exc:
        # SyntaxError, null bytes, or pathologically nested code
        # (RecursionError/MemoryError) - none of it gets run.
        raise UnsafeCodeError(f"code does not parse: {type(exc).__name__}: {exc}") from exc

    for node in nodes:
        if isinstance(node, _FORBIDDEN_NODES):
            raise UnsafeCodeError(f"{type(node).__name__} is not allowed")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_MODULES:
                    raise UnsafeCodeError(f"import of {alias.name!r} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in ALLOWED_MODULES:
                raise UnsafeCodeError(f"import from {node.module!r} is not allowed")
            for alias in node.names:
                if alias.name == "*":
                    raise UnsafeCodeError("star imports are not allowed")
                if alias.name.startswith("_") or alias.name in _FORBIDDEN_NAMES or alias.name in _FORBIDDEN_ATTRIBUTES:
                    raise UnsafeCodeError(f"import of {alias.name!r} is not allowed")
                if node.module == "sys" and alias.name not in _ALLOWED_SYS_ATTRIBUTES:
                    raise UnsafeCodeError(f"sys.{alias.name} is not allowed")
        elif isinstance(node, ast.Name):
            # __name__ only so the usual `if __name__ == "__main__":` guard works.
            if node.id in _FORBIDDEN_NAMES or (node.id.startswith("__") and node.id != "__name__"):
                raise UnsafeCodeError(f"use of {node.id!r} is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in _FORBIDDEN_ATTRIBUTES:
                raise UnsafeCodeError(f"attribute {node.attr!r} is not allowed")
            if isinstance(node.value, ast.Name) and node.value.id == "sys" and node.attr not in _ALLOWED_SYS_ATTRIBUTES:
                raise UnsafeCodeError(f"sys.{node.attr} is not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value != "__main__" and _DUNDER_IN_STRING.search(node.value):
                raise UnsafeCodeError("dunder names inside strings are not allowed")


# Runs in the child interpreter before the solution. It loads everything the
# solution could legitimately need while files can still be opened, then (on
# POSIX) caps memory and lowers the open-file limit to the three standard
# streams - after which no file or socket can be opened at all - and only
# then executes the solution as __main__ in a fresh namespace.
_BOOTSTRAP = f"""
import sys
with open(sys.argv[1], encoding="utf-8") as _handle:
    _code = compile(_handle.read(), "main.py", "exec")
for _module in {sorted(ALLOWED_MODULES)!r} + [
    "collections.abc", "decimal", "numbers", "traceback", "linecache", "encodings.ascii", "encodings.latin_1",
]:
    try:
        __import__(_module)
    except Exception:
        pass
try:
    import resource as _resource
except ImportError:
    _resource = None
if _resource is not None:
    for _name, _limit in (
        ("RLIMIT_AS", {MEMORY_LIMIT_BYTES}), ("RLIMIT_FSIZE", 0), ("RLIMIT_NPROC", 0), ("RLIMIT_NOFILE", 3),
    ):
        try:
            _resource.setrlimit(getattr(_resource, _name), (_limit, _limit))
        except Exception:
            pass  # a host that refuses a limit must not stop solutions running
exec(_code, {{"__name__": "__main__", "__builtins__": __builtins__}})
"""


def _run_blocking(source: str, stdin: str, timeout_seconds: float) -> RunResult:
    """Never raises: whatever goes wrong while running the code comes back as
    a failed RunResult, so a bad solution can only ever fail its own draft."""
    try:
        return _run_in_child(source, stdin, timeout_seconds)
    except Exception as exc:
        return RunResult(ok=False, stdout="", error=f"could not run the code: {type(exc).__name__}")


def _drain(stream, limit: int, sink: bytearray, state: dict, process: subprocess.Popen) -> None:
    """Reads a child stream to EOF while keeping at most `limit` bytes. A
    child that produces more is killed straight away, so a runaway print
    loop can never grow this process's memory."""
    try:
        while True:
            chunk = stream.read1(_READ_CHUNK_BYTES)
            if not chunk:
                return
            if state["overflow"]:
                continue  # already over the cap: discard until the pipe closes
            if len(sink) + len(chunk) > limit:
                state["overflow"] = True
                process.kill()
                continue
            sink.extend(chunk)
    except Exception:
        return  # the pipe went away (child killed) - nothing more to read


def _run_in_child(source: str, stdin: str, timeout_seconds: float) -> RunResult:
    stdin_bytes = stdin.encode("utf-8", errors="replace")
    if len(stdin_bytes) > MAX_STDIN_BYTES:
        return RunResult(ok=False, stdout="", error="input is too large")

    with tempfile.TemporaryDirectory(prefix="codelab-run-", ignore_cleanup_errors=True) as workdir:
        script = os.path.join(workdir, "main.py")
        with open(script, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(source)
        # Windows refuses to start a process without SYSTEMROOT; nothing else
        # (no API keys, no database URL) is passed through.
        env = {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        if os.name == "nt" and "SYSTEMROOT" in os.environ:
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        try:
            process = subprocess.Popen(
                # -I: isolated mode (ignores env vars and user site-packages).
                [sys.executable, "-I", "-c", _BOOTSTRAP, script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workdir,
                env=env,
            )
        except OSError as exc:
            return RunResult(ok=False, stdout="", error=f"could not start the interpreter: {type(exc).__name__}")

        stdout, stderr = bytearray(), bytearray()
        out_state, err_state = {"overflow": False}, {"overflow": False}
        readers = [
            threading.Thread(target=_drain, args=(process.stdout, MAX_OUTPUT_BYTES, stdout, out_state, process), daemon=True),
            threading.Thread(target=_drain, args=(process.stderr, MAX_STDERR_BYTES, stderr, err_state, process), daemon=True),
        ]
        timed_out = False
        try:
            for reader in readers:
                reader.start()
            try:
                process.stdin.write(stdin_bytes)
                process.stdin.close()
            except OSError:
                pass  # the child exited before reading its input
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=5)
            except Exception:
                pass
            for reader in readers:
                reader.join(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    stream.close()
                except Exception:
                    pass

    if out_state["overflow"] or err_state["overflow"]:
        return RunResult(ok=False, stdout="", error="output is too large")
    if timed_out:
        return RunResult(ok=False, stdout="", error=f"timed out after {timeout_seconds:g}s")
    text = bytes(stdout).decode("utf-8", errors="replace")
    if process.returncode != 0:
        last_line = bytes(stderr).decode("utf-8", errors="replace").strip().splitlines()[-1:] or ["no error output"]
        return RunResult(ok=False, stdout=text, error=f"exited with code {process.returncode}: {last_line[0][:200]}")
    return RunResult(ok=True, stdout=text)


async def run_python(source: str, stdin: str, timeout_seconds: float = 4.0) -> RunResult:
    """Checks `source` against the allowlist, then runs it with `stdin` in an
    isolated interpreter. Raises UnsafeCodeError (without running anything) if
    the check fails."""
    check_code_is_safe(source)
    return await asyncio.to_thread(_run_blocking, source, stdin, timeout_seconds)


def normalize_output(text: str) -> str:
    """Program output as the judge compares it: line endings unified, trailing
    whitespace on each line and trailing blank lines ignored."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)
