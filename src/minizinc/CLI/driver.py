#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import re
import subprocess
import sys
import warnings
from asyncio import create_subprocess_exec
from asyncio.subprocess import PIPE, Process
from dataclasses import fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Type, Union

import minizinc

from ..driver import Driver
from ..error import ConfigurationError, parse_error
from ..solver import Solver

#: MiniZinc version required by the python package
CLI_REQUIRED_VERSION = (2, 5, 0)


def to_python_type(mzn_type: dict) -> Type:
    """Converts MiniZinc JSON type to Type

    Converts a MiniZinc JSON type definition generated by the MiniZinc CLI to a
    Python Type object. This can be used on types that result from calling
    ``minizinc --model-interface-only``.

    Args:
        mzn_type (dict): MiniZinc type definition as resulting from JSON

    Returns:
        Type: Type definition in Python

    """
    basetype = mzn_type["type"]
    pytype: Type
    # TODO: MiniZinc does not report enumerated types correctly
    if basetype == "bool":
        pytype = bool
    elif basetype == "float":
        pytype = float
    elif basetype == "int":
        pytype = int
    elif basetype == "string":
        pytype = str
    elif basetype == "ann":
        pytype = str
    else:
        warnings.warn(
            f"Unable to determine minizinc type `{basetype}` assuming integer type",
            FutureWarning,
        )
        pytype = int

    if mzn_type.get("set", False):
        if pytype is int:
            pytype = Union[Set[int], range]  # type: ignore
        else:
            pytype = Set[pytype]  # type: ignore

    dim = mzn_type.get("dim", 0)
    while dim >= 1:
        # No typing support for n-dimensional typing
        pytype = List[pytype]  # type: ignore
        dim -= 1
    return pytype


class CLIDriver(Driver):
    """Driver that interfaces with MiniZinc through the command line interface.

    The command line driver will interact with MiniZinc and its solvers through
    the use of a ``minizinc`` executable. Driving MiniZinc using its executable
    is non-incremental and can often trigger full recompilation and might
    restart the solver from the beginning when changes are made to the instance.

    Attributes:
        executable (Path): The path to the executable used to access the MiniZinc Driver

    """

    _executable: Path
    _solver_cache: Optional[Dict[str, Solver]] = None

    def __init__(self, executable: Path):
        self._executable = executable
        assert self._executable.exists()

        super(CLIDriver, self).__init__()

        self.check_version()

    def make_default(self) -> None:
        from . import CLIInstance

        minizinc.default_driver = self
        minizinc.Instance = CLIInstance

    def run(
        self,
        args: List[Any],
        solver: Optional[Solver] = None,
    ):
        # TODO: Add documentation
        windows_spawn_options: Dict[str, Any] = {}
        if sys.platform == "win32":
            # On Windows, MiniZinc terminates its subprocesses by generating a
            # Ctrl+C event for its own console using GenerateConsoleCtrlEvent.
            # Therefore, we must spawn it in its own console to avoid receiving
            # that Ctrl+C ourselves.
            #
            # On POSIX systems, MiniZinc terminates its subprocesses by sending
            # SIGTERM to the solver's process group, so this workaround is not
            # necessary as we won't receive that signal.
            windows_spawn_options = {
                "startupinfo": subprocess.STARTUPINFO(
                    dwFlags=subprocess.STARTF_USESHOWWINDOW,
                    wShowWindow=subprocess.SW_HIDE,
                ),
                "creationflags": subprocess.CREATE_NEW_CONSOLE,
            }

        if solver is None:
            cmd = [str(self._executable), "--allow-multiple-assignments"] + [
                str(arg) for arg in args
            ]
            minizinc.logger.debug(f"CLIDriver:run -> command: \"{' '.join(cmd)}\"")
            output = subprocess.run(
                cmd,
                stdin=None,
                stdout=PIPE,
                stderr=PIPE,
                **windows_spawn_options,
            )
        else:
            with solver.configuration() as conf:
                cmd = [
                    str(self._executable),
                    "--solver",
                    conf,
                    "--allow-multiple-assignments",
                ] + [str(arg) for arg in args]
                minizinc.logger.debug(f"CLIDriver:run -> command: \"{' '.join(cmd)}\"")
                output = subprocess.run(
                    cmd,
                    stdin=None,
                    stdout=PIPE,
                    stderr=PIPE,
                    **windows_spawn_options,
                )
        if output.returncode != 0:
            raise parse_error(output.stderr)
        return output

    async def create_process(
        self, args: List[str], solver: Optional[str] = None
    ) -> Process:
        """Start an asynchronous driver process with given arguments

        Args:
            args (List[str]): direct arguments to the driver
            solver (Union[str, Path, None]): Solver configuration string
                guaranteed by the user to be valid until the process has ended.
        """

        windows_spawn_options: Dict[str, Any] = {}
        if sys.platform == "win32":
            # See corresponding comment in run()
            windows_spawn_options = {
                "startupinfo": subprocess.STARTUPINFO(
                    dwFlags=subprocess.STARTF_USESHOWWINDOW,
                    wShowWindow=subprocess.SW_HIDE,
                ),
                "creationflags": subprocess.CREATE_NEW_CONSOLE,
            }

        if solver is None:
            minizinc.logger.debug(
                f"CLIDriver:create_process -> program: {str(self._executable)} "
                f'args: "--allow-multiple-assignments '
                f"{' '.join(str(arg) for arg in args)}\""
            )
            proc = await create_subprocess_exec(
                str(self._executable),
                "--allow-multiple-assignments",
                *[str(arg) for arg in args],
                stdin=None,
                stdout=PIPE,
                stderr=PIPE,
                **windows_spawn_options,
            )
        else:
            minizinc.logger.debug(
                f"CLIDriver:create_process -> program: {str(self._executable)} "
                f'args: "--solver {solver} --allow-multiple-assignments '
                f"{' '.join(str(arg) for arg in args)}\""
            )
            proc = await create_subprocess_exec(
                str(self._executable),
                "--solver",
                solver,
                "--allow-multiple-assignments",
                *[str(arg) for arg in args],
                stdin=None,
                stdout=PIPE,
                stderr=PIPE,
                **windows_spawn_options,
            )
        return proc

    @property
    def minizinc_version(self) -> str:
        return self.run(["--version"]).stdout.decode()

    def check_version(self):
        output = self.run(["--version"])
        match = re.search(rb"version (\d+)\.(\d+)\.(\d+)", output.stdout)
        found = tuple([int(i) for i in match.groups()])
        if found < CLI_REQUIRED_VERSION:
            raise ConfigurationError(
                f"The MiniZinc driver found at '{self._executable}' has "
                f"version {found}. The minimal required version is "
                f"{CLI_REQUIRED_VERSION}."
            )

    def available_solvers(self, refresh=False):
        if not refresh and self._solver_cache is not None:
            return self._solver_cache

        # Find all available solvers
        output = self.run(["--solvers-json"])
        solvers = json.loads(output.stdout)

        # Construct Solver objects
        self._solver_cache = {}
        allowed_fields = set([f.name for f in fields(Solver)])
        for s in solvers:
            obj = Solver(
                **{key: value for (key, value) in s.items() if key in allowed_fields}
            )
            if obj.version == "<unknown version>":
                obj._identifier = obj.id
            else:
                obj._identifier = obj.id + "@" + obj.version

            names = s.get("tags", [])
            names.extend([s["id"], s["id"].split(".")[-1]])
            for name in names:
                self._solver_cache.setdefault(name, []).append(obj)

        return self._solver_cache
