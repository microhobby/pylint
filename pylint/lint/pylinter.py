# Licensed under the GPL: https://www.gnu.org/licenses/old-licenses/gpl-2.0.html
# For details: https://github.com/pylint-dev/pylint/blob/main/LICENSE
# Copyright (c) https://github.com/pylint-dev/pylint/blob/main/CONTRIBUTORS.txt

from __future__ import annotations

import argparse
import collections
import contextlib
import functools
import os
import sys
import tokenize
import traceback
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from io import TextIOWrapper
from pathlib import Path
from re import Pattern
from types import ModuleType
from typing import Any, Protocol

import astroid
from astroid import nodes
from xonsh.execer import Execer
from xonsh.built_ins import XSH
from xonsh.main import setup
import ast

from pylint import checkers, exceptions, interfaces, reporters
from pylint.checkers.base_checker import BaseChecker
from pylint.config.arguments_manager import _ArgumentsManager
from pylint.constants import (
    MAIN_CHECKER_NAME,
    MSG_TYPES,
    MSG_TYPES_STATUS,
    WarningScope,
)
from pylint.interfaces import HIGH
from pylint.lint.base_options import _make_linter_options
from pylint.lint.caching import load_results, save_results
from pylint.lint.expand_modules import (
    _is_ignored_file,
    discover_package_path,
    expand_modules,
)
from pylint.lint.message_state_handler import _MessageStateHandler
from pylint.lint.parallel import check_parallel
from pylint.lint.report_functions import (
    report_messages_by_module_stats,
    report_messages_stats,
    report_total_messages_stats,
)
from pylint.lint.utils import (
    augmented_sys_path,
    get_fatal_error_message,
    prepare_crash_report,
)
from pylint.message import Message, MessageDefinition, MessageDefinitionStore
from pylint.reporters.base_reporter import BaseReporter
from pylint.reporters.text import TextReporter
from pylint.reporters.ureports import nodes as report_nodes
from pylint.typing import (
    DirectoryNamespaceDict,
    FileItem,
    ManagedMessage,
    MessageDefinitionTuple,
    MessageLocationTuple,
    ModuleDescriptionDict,
    Options,
)
from pylint.utils import ASTWalker, FileState, LinterStats, utils

MANAGER = astroid.MANAGER


## CUSTOM XONSH STUFF
def _analyze_xonsh_file(file_path = None, file_str = None):
    """Analyze a .xsh file and identify non-Python lines."""

    # Setup xonsh environment if not already done
    if not hasattr(XSH, 'execer') or XSH.execer is None:
        setup(shell_type="none")

    content = ""
    if file_path is None:
        file_path = "_pylint_"
        content = file_str
    else:
        # Read the file
        with open(file_path, 'r') as f:
            content = f.read()

    # Create an execer instance
    execer = Execer(filename=file_path, debug_level=0)

    # Parse with context-aware transformation
    try:
        # Create a basic context (builtin functions and variables)
        ctx = set(dir(__builtins__))

        # Parse the content - this will transform shell commands to subprocess calls
        tree = execer.parse(content, ctx, mode="exec", filename=file_path, transform=True)

        if tree is None:
            print("No executable code found")
            return

        # Analyze the AST to find subprocess calls
        analyzer = XonshCodeAnalyzer(content)
        analyzer.visit(tree)

        return analyzer.get_results()

    except SyntaxError as e:
        raise astroid.AstroidSyntaxError(
            f"Syntax error in {file_path}: {e}",
            modname=file_path,
            error=e,
            path=file_path,
        )

class XonshCodeAnalyzer(ast.NodeVisitor):
    """AST visitor to identify xonsh-specific constructs."""

    def __init__(self, source_code):
        self.source_lines = source_code.splitlines()
        self.shell_lines = set()
        self.env_var_lines = set()
        self.subprocess_lines = set()
        self.multiline_commands = set()  # Track multiline command ranges

        # Pre-scan for command substitution patterns that might not show up in AST
        self._prescan_command_substitutions()

    def visit_Call(self, node):
        """Visit function calls to identify subprocess calls."""
        # Check for subprocess calls (these are shell commands)
        if (hasattr(node.func, 'attr')):
            # Handle both regular subprocess calls and command substitution
            if (hasattr(node, 'lineno') and
                (node.func.attr == "subproc_captured_hiddenobject" or
                node.func.attr == "subproc_captured_stdout" or
                node.func.attr == "subproc_captured_object")):

                start_line = node.lineno
                end_line = getattr(node, 'end_lineno', start_line)

                # For multiline commands, mark all lines in the range
                if end_line and end_line > start_line:
                    for line_num in range(start_line, end_line + 1):
                        self.subprocess_lines.add(line_num)
                        self.multiline_commands.add(line_num)
                else:
                    self.subprocess_lines.add(start_line)

                # Also check for line continuations manually
                self._mark_continuation_lines(start_line)

        # Also check for function calls that might be command substitutions
        elif (hasattr(node.func, 'id') and
            hasattr(node, 'lineno')):
            # Check if this looks like a command substitution pattern
            func_name = getattr(node.func, 'id', '')
            if 'subproc' in func_name.lower():
                start_line = node.lineno
                end_line = getattr(node, 'end_lineno', start_line)

                if end_line and end_line > start_line:
                    for line_num in range(start_line, end_line + 1):
                        self.subprocess_lines.add(line_num)
                        self.multiline_commands.add(line_num)
                else:
                    self.subprocess_lines.add(start_line)

                self._mark_continuation_lines(start_line)

        self.generic_visit(node)

    def _mark_continuation_lines(self, start_line):
        """Mark lines that are part of a multiline command using backslash continuation or parentheses."""
        if start_line > len(self.source_lines):
            return

        current_line = start_line - 1  # Convert to 0-based indexing

        # First, check if this is a $(...) command substitution that spans multiple lines
        self._mark_command_substitution_lines(start_line)

        # Then handle backslash continuations
        # Look backward to find the actual start of the command
        while current_line > 0:
            line_content = self.source_lines[current_line - 1].rstrip()
            if line_content.endswith('\\'):
                self.subprocess_lines.add(current_line)  # Add 1-based line number
                current_line -= 1
            else:
                break

        # Look forward to find continuation lines
        current_line = start_line - 1  # Reset to original line (0-based)
        while current_line < len(self.source_lines):
            line_content = self.source_lines[current_line].rstrip()
            if line_content.endswith('\\'):
                self.subprocess_lines.add(current_line + 1)  # Add 1-based line number
                # Check the next line too
                if current_line + 1 < len(self.source_lines):
                    self.subprocess_lines.add(current_line + 2)  # Next line is also part of command
                current_line += 1
            else:
                # This is the last line of the command
                self.subprocess_lines.add(current_line + 1)  # Add 1-based line number
                break

    def _mark_command_substitution_lines(self, start_line):
        """Mark all lines that are part of a command substitution $(...) block."""
        if start_line > len(self.source_lines):
            return

        # Look for the pattern where a line contains $( and we need to find the matching )
        start_idx = start_line - 1  # Convert to 0-based

        # Look backward to find the start of the $( construct
        actual_start = start_idx
        for i in range(start_idx, -1, -1):
            line = self.source_lines[i]
            if '$(' in line or '@(' in line or '!(' in line:
                actual_start = i
                break
            # If we find a line that doesn't seem to be part of a continuation, stop
            if not line.rstrip().endswith('\\') and '=' not in line:
                break

        # Now find the matching closing parenthesis
        # Simple approach: look for $(...) blocks and mark all lines until the closing )
        paren_count = 0
        found_start = False

        for i in range(actual_start, len(self.source_lines)):
            line = self.source_lines[i]

            # Count command substitution starts
            for j, char in enumerate(line):
                if char == '(' and j > 0:
                    prev_char = line[j-1]
                    if prev_char in ['$', '@', '!']:
                        paren_count += 1
                        found_start = True
                elif char == ')' and found_start:
                    paren_count -= 1

            # Mark this line as a subprocess line
            if found_start:
                self.subprocess_lines.add(i + 1)  # Convert to 1-based
                self.multiline_commands.add(i + 1)

            # If we've closed all parentheses, we're done
            if found_start and paren_count == 0:
                break

    def visit_Subscript(self, node):
        """Visit subscript operations to identify environment variable access."""
        # Check for environment variable access like $VAR or ${VAR}
        if (hasattr(node.value, 'attr') and
            hasattr(node.value.value, 'id') and
            node.value.value.id == '__xonsh__' and
            node.value.attr == 'env'):

            if hasattr(node, 'lineno'):
                start_line = node.lineno
                end_line = getattr(node, 'end_lineno', start_line)

                # Handle multiline environment variable access
                if end_line and end_line > start_line:
                    for line_num in range(start_line, end_line + 1):
                        self.env_var_lines.add(line_num)
                else:
                    self.env_var_lines.add(start_line)

        self.generic_visit(node)

    def visit_Assign(self, node):
        """Visit assignments to identify environment variable assignments."""
        # Check for environment variable assignments
        for target in node.targets:
            if (isinstance(target, ast.Subscript) and
                hasattr(target.value, 'attr') and
                hasattr(target.value.value, 'id') and
                target.value.value.id == '__xonsh__' and
                target.value.attr == 'env'):

                if hasattr(node, 'lineno'):
                    start_line = node.lineno
                    end_line = getattr(node, 'end_lineno', start_line)

                    # Handle multiline environment variable assignments
                    if end_line and end_line > start_line:
                        for line_num in range(start_line, end_line + 1):
                            self.env_var_lines.add(line_num)
                    else:
                        self.env_var_lines.add(start_line)

        self.generic_visit(node)

    def get_results(self):
        """Return analysis results."""
        all_non_python = self.shell_lines | self.env_var_lines | self.subprocess_lines

        results = {
            'total_lines': len(self.source_lines),
            'shell_command_lines': sorted(self.subprocess_lines),
            'env_var_lines': sorted(self.env_var_lines),
            'all_non_python_lines': sorted(all_non_python),
            'pure_python_lines': [],
            'multiline_commands': sorted(self.multiline_commands)
        }

        # Identify pure Python lines (lines not in any non-Python category)
        for i, line in enumerate(self.source_lines, 1):
            if line.strip() and i not in all_non_python:
                results['pure_python_lines'].append(i)

        return results

    def _prescan_command_substitutions(self):
        """Pre-scan the source code to find command substitution patterns."""
        i = 0
        while i < len(self.source_lines):
            line = self.source_lines[i]

            # Look for command substitution patterns: $(...), @(...), !(...)
            if any(pattern in line for pattern in ['$(', '@(', '!(']):
                # Find the complete command substitution block
                paren_count = 0
                found_start = False

                # Process from current line onwards to find the complete block
                j = i
                while j < len(self.source_lines):
                    current_line = self.source_lines[j]

                    # Count command substitution starts and regular parentheses
                    for k, char in enumerate(current_line):
                        if char == '(' and k > 0:
                            prev_char = current_line[k-1]
                            if prev_char in ['$', '@', '!']:
                                paren_count += 1
                                found_start = True
                        elif char == ')' and found_start:
                            paren_count -= 1

                    # Mark this line as part of a command substitution
                    if found_start:
                        self.subprocess_lines.add(j + 1)  # Convert to 1-based
                        self.multiline_commands.add(j + 1)

                    # If we've balanced all parentheses, we're done with this block
                    if found_start and paren_count == 0:
                        i = j + 1  # Continue from the next line after this block
                        break

                    j += 1

                # If we didn't find a complete block, continue from next line
                if j >= len(self.source_lines):
                    i += 1
            else:
                i += 1

## CUSTOM XONSH STUFF


class GetAstProtocol(Protocol):
    def __call__(
        self, filepath: str, modname: str, data: str | None = None
    ) -> nodes.Module: ...


def _read_stdin() -> str:
    # See https://github.com/python/typeshed/pull/5623 for rationale behind assertion
    assert isinstance(sys.stdin, TextIOWrapper)
    sys.stdin = TextIOWrapper(sys.stdin.detach(), encoding="utf-8")
    return sys.stdin.read()


def _load_reporter_by_class(reporter_class: str) -> type[BaseReporter]:
    qname = reporter_class
    module_part = astroid.modutils.get_module_part(qname)
    module = astroid.modutils.load_module_from_name(module_part)
    class_name = qname.split(".")[-1]
    klass = getattr(module, class_name)
    assert issubclass(klass, BaseReporter), f"{klass} is not a BaseReporter"
    return klass  # type: ignore[no-any-return]


# Python Linter class #########################################################

# pylint: disable-next=consider-using-namedtuple-or-dataclass
MSGS: dict[str, MessageDefinitionTuple] = {
    "F0001": (
        "%s",
        "fatal",
        "Used when an error occurred preventing the analysis of a \
              module (unable to find it for instance).",
        {"scope": WarningScope.LINE},
    ),
    "F0002": (
        "%s: %s",
        "astroid-error",
        "Used when an unexpected error occurred while building the "
        "Astroid  representation. This is usually accompanied by a "
        "traceback. Please report such errors !",
        {"scope": WarningScope.LINE},
    ),
    "F0010": (
        "error while code parsing: %s",
        "parse-error",
        "Used when an exception occurred while building the Astroid "
        "representation which could be handled by astroid.",
        {"scope": WarningScope.LINE},
    ),
    "F0011": (
        "error while parsing the configuration: %s",
        "config-parse-error",
        "Used when an exception occurred while parsing a pylint configuration file.",
        {"scope": WarningScope.LINE},
    ),
    "I0001": (
        "Unable to run raw checkers on built-in module %s",
        "raw-checker-failed",
        "Used to inform that a built-in module has not been checked "
        "using the raw checkers.",
        {
            "scope": WarningScope.LINE,
            "default_enabled": False,
        },
    ),
    "I0010": (
        "Unable to consider inline option %r",
        "bad-inline-option",
        "Used when an inline option is either badly formatted or can't "
        "be used inside modules.",
        {
            "scope": WarningScope.LINE,
            "default_enabled": False,
        },
    ),
    "I0011": (
        "Locally disabling %s (%s)",
        "locally-disabled",
        "Used when an inline option disables a message or a messages category.",
        {
            "scope": WarningScope.LINE,
            "default_enabled": False,
        },
    ),
    "I0013": (
        "Ignoring entire file",
        "file-ignored",
        "Used to inform that the file will not be checked",
        {
            "scope": WarningScope.LINE,
            "default_enabled": False,
        },
    ),
    "I0020": (
        "Suppressed %s (from line %d)",
        "suppressed-message",
        "A message was triggered on a line, but suppressed explicitly "
        "by a disable= comment in the file. This message is not "
        "generated for messages that are ignored due to configuration "
        "settings.",
        {
            "scope": WarningScope.LINE,
            "default_enabled": False,
        },
    ),
    "I0021": (
        "Useless suppression of %s",
        "useless-suppression",
        "Reported when a message is explicitly disabled for a line or "
        "a block of code, but never triggered.",
        {
            "scope": WarningScope.LINE,
            "default_enabled": False,
        },
    ),
    "I0022": (
        'Pragma "%s" is deprecated, use "%s" instead',
        "deprecated-pragma",
        "Some inline pylint options have been renamed or reworked, "
        "only the most recent form should be used. "
        "NOTE:skip-all is only available with pylint >= 0.26",
        {
            "old_names": [("I0014", "deprecated-disable-all")],
            "scope": WarningScope.LINE,
            "default_enabled": False,
        },
    ),
    "E0001": (
        "%s",
        "syntax-error",
        "Used when a syntax error is raised for a module.",
        {"scope": WarningScope.LINE},
    ),
    "E0011": (
        "Unrecognized file option %r",
        "unrecognized-inline-option",
        "Used when an unknown inline option is encountered.",
        {"scope": WarningScope.LINE},
    ),
    "W0012": (
        "Unknown option value for '%s', expected a valid pylint message and got '%s'",
        "unknown-option-value",
        "Used when an unknown value is encountered for an option.",
        {
            "scope": WarningScope.LINE,
            "old_names": [("E0012", "bad-option-value")],
        },
    ),
    "R0022": (
        "Useless option value for '%s', %s",
        "useless-option-value",
        "Used when a value for an option that is now deleted from pylint"
        " is encountered.",
        {
            "scope": WarningScope.LINE,
            "old_names": [("E0012", "bad-option-value")],
        },
    ),
    "E0013": (
        "Plugin '%s' is impossible to load, is it installed ? ('%s')",
        "bad-plugin-value",
        "Used when a bad value is used in 'load-plugins'.",
        {"scope": WarningScope.LINE},
    ),
    "E0014": (
        "Out-of-place setting encountered in top level configuration-section '%s' : '%s'",
        "bad-configuration-section",
        "Used when we detect a setting in the top level of a toml configuration that"
        " shouldn't be there.",
        {"scope": WarningScope.LINE},
    ),
    "E0015": (
        "Unrecognized option found: %s",
        "unrecognized-option",
        "Used when we detect an option that we do not recognize.",
        {"scope": WarningScope.LINE},
    ),
}


# pylint: disable=too-many-instance-attributes,too-many-public-methods
class PyLinter(
    _ArgumentsManager,
    _MessageStateHandler,
    reporters.ReportsHandlerMixIn,
    checkers.BaseChecker,
):
    """Lint Python modules using external checkers.

    This is the main checker controlling the other ones and the reports
    generation. It is itself both a raw checker and an astroid checker in order
    to:
    * handle message activation / deactivation at the module level
    * handle some basic but necessary stats' data (number of classes, methods...)

    IDE plugin developers: you may have to call
    `astroid.MANAGER.clear_cache()` across runs if you want
    to ensure the latest code version is actually checked.

    This class needs to support pickling for parallel linting to work. The exception
    is reporter member; see check_parallel function for more details.
    """

    name = MAIN_CHECKER_NAME
    msgs = MSGS
    # Will be used like this : datetime.now().strftime(crash_file_path)
    crash_file_path: str = "pylint-crash-%Y-%m-%d-%H-%M-%S.txt"

    option_groups_descs = {
        "Messages control": "Options controlling analysis messages",
        "Reports": "Options related to output formatting and reporting",
    }

    def __init__(
        self,
        options: Options = (),
        reporter: reporters.BaseReporter | reporters.MultiReporter | None = None,
        option_groups: tuple[tuple[str, str], ...] = (),
        # TODO: Deprecate passing the pylintrc parameter
        pylintrc: str | None = None,  # pylint: disable=unused-argument
    ) -> None:
        _ArgumentsManager.__init__(self, prog="pylint")
        _MessageStateHandler.__init__(self, self)

        # Some stuff has to be done before initialization of other ancestors...
        # messages store / checkers / reporter / astroid manager

        # Attributes for reporters
        self.reporter: reporters.BaseReporter | reporters.MultiReporter
        if reporter:
            self.set_reporter(reporter)
        else:
            self.set_reporter(TextReporter())
        self._reporters: dict[str, type[reporters.BaseReporter]] = {}
        """Dictionary of possible but non-initialized reporters."""

        # Attributes for checkers and plugins
        self._checkers: defaultdict[str, list[checkers.BaseChecker]] = (
            collections.defaultdict(list)
        )
        """Dictionary of registered and initialized checkers."""
        self._dynamic_plugins: dict[str, ModuleType | ModuleNotFoundError | bool] = {}
        """Set of loaded plugin names."""

        # Attributes related to stats
        self.stats = LinterStats()

        # Attributes related to (command-line) options and their parsing
        self.options: Options = options + _make_linter_options(self)
        for opt_group in option_groups:
            self.option_groups_descs[opt_group[0]] = opt_group[1]
        self._option_groups: tuple[tuple[str, str], ...] = (
            *option_groups,
            ("Messages control", "Options controlling analysis messages"),
            ("Reports", "Options related to output formatting and reporting"),
        )
        self.fail_on_symbols: list[str] = []
        """List of message symbols on which pylint should fail, set by --fail-on."""
        self._error_mode = False

        reporters.ReportsHandlerMixIn.__init__(self)
        checkers.BaseChecker.__init__(self, self)
        # provided reports
        self.reports = (
            ("RP0001", "Messages by category", report_total_messages_stats),
            (
                "RP0002",
                "% errors / warnings by module",
                report_messages_by_module_stats,
            ),
            ("RP0003", "Messages", report_messages_stats),
        )

        # Attributes related to registering messages and their handling
        self.msgs_store = MessageDefinitionStore(self.config.py_version)
        self.msg_status = 0
        self._by_id_managed_msgs: list[ManagedMessage] = []

        # Attributes related to visiting files
        self.file_state = FileState("", self.msgs_store, is_base_filestate=True)
        self.current_name: str = ""
        self.current_file: str | None = None
        self._ignore_file = False
        self._ignore_paths: list[Pattern[str]] = []

        self.register_checker(self)

    def load_default_plugins(self) -> None:
        checkers.initialize(self)
        reporters.initialize(self)

    def load_plugin_modules(self, modnames: Iterable[str], force: bool = False) -> None:
        """Check a list of pylint plugins modules, load and register them.

        If a module cannot be loaded, never try to load it again and instead
        store the error message for later use in ``load_plugin_configuration``
        below.

        If `force` is True (useful when multiprocessing), then the plugin is
        reloaded regardless if an entry exists in self._dynamic_plugins.
        """
        for modname in modnames:
            if modname in self._dynamic_plugins and not force:
                continue
            try:
                module = astroid.modutils.load_module_from_name(modname)
                module.register(self)
                self._dynamic_plugins[modname] = module
            except ModuleNotFoundError as mnf_e:
                self._dynamic_plugins[modname] = mnf_e

    def load_plugin_configuration(self) -> None:
        """Call the configuration hook for plugins.

        This walks through the list of plugins, grabs the "load_configuration"
        hook, if exposed, and calls it to allow plugins to configure specific
        settings.

        The result of attempting to load the plugin of the given name
        is stored in the dynamic plugins dictionary in ``load_plugin_modules`` above.

        ..note::
            This function previously always tried to load modules again, which
            led to some confusion and silent failure conditions as described
            in GitHub issue #7264. Making it use the stored result is more efficient, and
            means that we avoid the ``init-hook`` problems from before.
        """
        for modname, module_or_error in self._dynamic_plugins.items():
            if isinstance(module_or_error, ModuleNotFoundError):
                self.add_message(
                    "bad-plugin-value", args=(modname, module_or_error), line=0
                )
            elif hasattr(module_or_error, "load_configuration"):
                module_or_error.load_configuration(self)

        # We re-set all the dictionary values to True here to make sure the dict
        # is pickle-able. This is only a problem in multiprocessing/parallel mode.
        # (e.g. invoking pylint -j 2)
        self._dynamic_plugins = {
            modname: not isinstance(val, ModuleNotFoundError)
            for modname, val in self._dynamic_plugins.items()
        }

    def _load_reporters(self, reporter_names: str) -> None:
        """Load the reporters if they are available on _reporters."""
        if not self._reporters:
            return
        sub_reporters = []
        output_files = []
        with contextlib.ExitStack() as stack:
            for reporter_name in reporter_names.split(","):
                reporter_name, *reporter_output = reporter_name.split(":", 1)

                reporter = self._load_reporter_by_name(reporter_name)
                sub_reporters.append(reporter)
                if reporter_output:
                    output_file = stack.enter_context(
                        open(reporter_output[0], "w", encoding="utf-8")
                    )
                    reporter.out = output_file
                    output_files.append(output_file)

            # Extend the lifetime of all opened output files
            close_output_files = stack.pop_all().close

        if len(sub_reporters) > 1 or output_files:
            self.set_reporter(
                reporters.MultiReporter(
                    sub_reporters,
                    close_output_files,
                )
            )
        else:
            self.set_reporter(sub_reporters[0])

    def _load_reporter_by_name(self, reporter_name: str) -> reporters.BaseReporter:
        name = reporter_name.lower()
        if name in self._reporters:
            return self._reporters[name]()

        try:
            reporter_class = _load_reporter_by_class(reporter_name)
        except (ImportError, AttributeError, AssertionError) as e:
            raise exceptions.InvalidReporterError(name) from e

        return reporter_class()

    def set_reporter(
        self, reporter: reporters.BaseReporter | reporters.MultiReporter
    ) -> None:
        """Set the reporter used to display messages and reports."""
        self.reporter = reporter
        reporter.linter = self

    def register_reporter(self, reporter_class: type[reporters.BaseReporter]) -> None:
        """Registers a reporter class on the _reporters attribute."""
        self._reporters[reporter_class.name] = reporter_class

    def report_order(self) -> list[BaseChecker]:
        reports = sorted(self._reports, key=lambda x: getattr(x, "name", ""))
        try:
            # Remove the current reporter and add it
            # at the end of the list.
            reports.pop(reports.index(self))
        except ValueError:
            pass
        else:
            reports.append(self)
        return reports

    # checkers manipulation methods ############################################

    def register_checker(self, checker: checkers.BaseChecker) -> None:
        """This method auto registers the checker."""
        self._checkers[checker.name].append(checker)
        for r_id, r_title, r_cb in checker.reports:
            self.register_report(r_id, r_title, r_cb, checker)
        if hasattr(checker, "msgs"):
            self.msgs_store.register_messages_from_checker(checker)
            for message in checker.messages:
                if not message.default_enabled:
                    self.disable(message.msgid)
        # Register the checker, but disable all of its messages.
        if not getattr(checker, "enabled", True):
            self.disable(checker.name)

    def enable_fail_on_messages(self) -> None:
        """Enable 'fail on' msgs.

        Convert values in config.fail_on (which might be msg category, msg id,
        or symbol) to specific msgs, then enable and flag them for later.
        """
        fail_on_vals = self.config.fail_on
        if not fail_on_vals:
            return

        fail_on_cats = set()
        fail_on_msgs = set()
        for val in fail_on_vals:
            # If value is a category, add category, else add message
            if val in MSG_TYPES:
                fail_on_cats.add(val)
            else:
                fail_on_msgs.add(val)

        # For every message in every checker, if cat or msg flagged, enable check
        for all_checkers in self._checkers.values():
            for checker in all_checkers:
                for msg in checker.messages:
                    if msg.msgid in fail_on_msgs or msg.symbol in fail_on_msgs:
                        # message id/symbol matched, enable and flag it
                        self.enable(msg.msgid)
                        self.fail_on_symbols.append(msg.symbol)
                    elif msg.msgid[0] in fail_on_cats:
                        # message starts with a category value, flag (but do not enable) it
                        self.fail_on_symbols.append(msg.symbol)

    def any_fail_on_issues(self) -> bool:
        return any(x in self.fail_on_symbols for x in self.stats.by_msg.keys())

    def disable_reporters(self) -> None:
        """Disable all reporters."""
        for _reporters in self._reports.values():
            for report_id, _, _ in _reporters:
                self.disable_report(report_id)

    def _parse_error_mode(self) -> None:
        """Parse the current state of the error mode.

        Error mode: enable only errors; no reports, no persistent.
        """
        if not self._error_mode:
            return

        self.disable_noerror_messages()
        self.disable("miscellaneous")
        self.set_option("reports", False)
        self.set_option("persistent", False)
        self.set_option("score", False)

    # code checking methods ###################################################

    def get_checkers(self) -> list[BaseChecker]:
        """Return all available checkers as an ordered list."""
        return sorted(c for _checkers in self._checkers.values() for c in _checkers)

    def get_checker_names(self) -> list[str]:
        """Get all the checker names that this linter knows about."""
        return sorted(
            {
                checker.name
                for checker in self.get_checkers()
                if checker.name != MAIN_CHECKER_NAME
            }
        )

    def prepare_checkers(self) -> list[BaseChecker]:
        """Return checkers needed for activated messages and reports."""
        if not self.config.reports:
            self.disable_reporters()
        # get needed checkers
        needed_checkers: list[BaseChecker] = [self]
        for checker in self.get_checkers()[1:]:
            messages = {msg for msg in checker.msgs if self.is_message_enabled(msg)}
            if messages or any(self.report_is_enabled(r[0]) for r in checker.reports):
                needed_checkers.append(checker)
        return needed_checkers

    # pylint: disable=unused-argument
    @staticmethod
    def should_analyze_file(modname: str, path: str, is_argument: bool = False) -> bool:
        """Returns whether a module should be checked.

        This implementation returns True for all python source files (.py and .pyi),
        indicating that all files should be linted.

        Subclasses may override this method to indicate that modules satisfying
        certain conditions should not be linted.

        :param str modname: The name of the module to be checked.
        :param str path: The full path to the source code of the module.
        :param bool is_argument: Whether the file is an argument to pylint or not.
                                 Files which respect this property are always
                                 checked, since the user requested it explicitly.
        :returns: True if the module should be checked.
        """
        if is_argument:
            return True
        return path.endswith((".py", ".pyi"))

    # pylint: enable=unused-argument

    def initialize(self) -> None:
        """Initialize linter for linting.

        This method is called before any linting is done.
        """
        self._ignore_paths = self.config.ignore_paths
        # initialize msgs_state now that all messages have been registered into
        # the store
        for msg in self.msgs_store.messages:
            if not msg.may_be_emitted(self.config.py_version):
                self._msgs_state[msg.msgid] = False

    def _discover_files(self, files_or_modules: Sequence[str]) -> Iterator[str]:
        """Discover python modules and packages in sub-directory.

        Returns iterator of paths to discovered modules and packages.
        """
        for something in files_or_modules:
            if os.path.isdir(something) and not os.path.isfile(
                os.path.join(something, "__init__.py")
            ):
                skip_subtrees: list[str] = []
                for root, _, files in os.walk(something):
                    if any(root.startswith(s) for s in skip_subtrees):
                        # Skip subtree of already discovered package.
                        continue

                    if _is_ignored_file(
                        root,
                        self.config.ignore,
                        self.config.ignore_patterns,
                        self.config.ignore_paths,
                    ):
                        skip_subtrees.append(root)
                        continue

                    if "__init__.py" in files:
                        skip_subtrees.append(root)
                        yield root
                    else:
                        yield from (
                            os.path.join(root, file)
                            for file in files
                            if file.endswith((".py", ".pyi"))
                        )
            else:
                yield something

    def check(self, files_or_modules: Sequence[str]) -> None:
        """Main checking entry: check a list of files or modules from their name.

        files_or_modules is either a string or list of strings presenting modules to check.
        """
        self.initialize()
        if self.config.recursive:
            files_or_modules = tuple(self._discover_files(files_or_modules))
        if self.config.from_stdin:
            if len(files_or_modules) != 1:
                raise exceptions.InvalidArgsError(
                    "Missing filename required for --from-stdin"
                )

        extra_packages_paths = list(
            dict.fromkeys(
                [
                    discover_package_path(file_or_module, self.config.source_roots)
                    for file_or_module in files_or_modules
                ]
            ).keys()
        )

        # TODO: Move the parallel invocation into step 3 of the checking process
        if not self.config.from_stdin and self.config.jobs > 1:
            original_sys_path = sys.path[:]
            check_parallel(
                self,
                self.config.jobs,
                self._iterate_file_descrs(files_or_modules),
                extra_packages_paths,
            )
            sys.path = original_sys_path
            return

        # 1) Get all FileItems
        with augmented_sys_path(extra_packages_paths):
            if self.config.from_stdin:
                fileitems = self._get_file_descr_from_stdin(files_or_modules[0])
                data: str | None = _read_stdin()
            else:
                fileitems = self._iterate_file_descrs(files_or_modules)
                data = None

        # The contextmanager also opens all checkers and sets up the PyLinter class
        with augmented_sys_path(extra_packages_paths):
            with self._astroid_module_checker() as check_astroid_module:
                # 2) Get the AST for each FileItem
                ast_per_fileitem = self._get_asts(fileitems, data)

                # 3) Lint each ast
                self._lint_files(ast_per_fileitem, check_astroid_module)

    def _get_asts(
        self, fileitems: Iterator[FileItem], data: str | None
    ) -> dict[FileItem, nodes.Module | None]:
        """Get the AST for all given FileItems."""
        ast_per_fileitem: dict[FileItem, nodes.Module | None] = {}

        for fileitem in fileitems:
            self.set_current_module(fileitem.name, fileitem.filepath)

            try:
                ast_per_fileitem[fileitem] = self.get_ast(
                    fileitem.filepath, fileitem.name, data
                )
            except astroid.AstroidBuildingError as ex:
                template_path = prepare_crash_report(
                    ex, fileitem.filepath, self.crash_file_path
                )
                msg = get_fatal_error_message(fileitem.filepath, template_path)
                self.add_message(
                    "astroid-error",
                    args=(fileitem.filepath, msg),
                    confidence=HIGH,
                )

        return ast_per_fileitem

    def check_single_file_item(self, file: FileItem) -> None:
        """Check single file item.

        The arguments are the same that are documented in _check_files

        initialize() should be called before calling this method
        """
        with self._astroid_module_checker() as check_astroid_module:
            self._check_file(self.get_ast, check_astroid_module, file)

    def _lint_files(
        self,
        ast_mapping: dict[FileItem, nodes.Module | None],
        check_astroid_module: Callable[[nodes.Module], bool | None],
    ) -> None:
        """Lint all AST modules from a mapping.."""
        for fileitem, module in ast_mapping.items():
            if module is None:
                continue
            try:
                self._lint_file(fileitem, module, check_astroid_module)
            except Exception as ex:  # pylint: disable=broad-except
                template_path = prepare_crash_report(
                    ex, fileitem.filepath, self.crash_file_path
                )
                msg = get_fatal_error_message(fileitem.filepath, template_path)
                if isinstance(ex, astroid.AstroidError):
                    self.add_message(
                        "astroid-error", args=(fileitem.filepath, msg), confidence=HIGH
                    )
                else:
                    self.add_message("fatal", args=msg, confidence=HIGH)

    def _lint_file(
        self,
        file: FileItem,
        module: nodes.Module,
        check_astroid_module: Callable[[nodes.Module], bool | None],
    ) -> None:
        """Lint a file using the passed utility function check_astroid_module).

        :param FileItem file: data about the file
        :param nodes.Module module: the ast module to lint
        :param Callable check_astroid_module: callable checking an AST taking the following
               arguments
        - ast: AST of the module
        :raises AstroidError: for any failures stemming from astroid
        """
        self.set_current_module(file.name, file.filepath)
        self._ignore_file = False
        self.file_state = FileState(file.modpath, self.msgs_store, module)
        # fix the current file (if the source file was not available or
        # if it's actually a c extension)
        self.current_file = module.file

        try:
            check_astroid_module(module)
        except Exception as e:
            raise astroid.AstroidError from e

        # warn about spurious inline messages handling
        spurious_messages = self.file_state.iter_spurious_suppression_messages(
            self.msgs_store
        )
        for msgid, line, args in spurious_messages:
            self.add_message(msgid, line, None, args)

    def _check_file(
        self,
        get_ast: GetAstProtocol,
        check_astroid_module: Callable[[nodes.Module], bool | None],
        file: FileItem,
    ) -> None:
        """Check a file using the passed utility functions (get_ast and
        check_astroid_module).

        :param callable get_ast: callable returning AST from defined file taking the
                                 following arguments
        - filepath: path to the file to check
        - name: Python module name
        :param callable check_astroid_module: callable checking an AST taking the following
               arguments
        - ast: AST of the module
        :param FileItem file: data about the file
        :raises AstroidError: for any failures stemming from astroid
        """
        self.set_current_module(file.name, file.filepath)
        # get the module representation
        ast_node = get_ast(file.filepath, file.name)
        if ast_node is None:
            return

        self._ignore_file = False

        self.file_state = FileState(file.modpath, self.msgs_store, ast_node)
        # fix the current file (if the source file was not available or
        # if it's actually a c extension)
        self.current_file = ast_node.file
        try:
            check_astroid_module(ast_node)
        except Exception as e:  # pragma: no cover
            raise astroid.AstroidError from e
        # warn about spurious inline messages handling
        spurious_messages = self.file_state.iter_spurious_suppression_messages(
            self.msgs_store
        )
        for msgid, line, args in spurious_messages:
            self.add_message(msgid, line, None, args)

    def _get_file_descr_from_stdin(self, filepath: str) -> Iterator[FileItem]:
        """Return file description (tuple of module name, file path, base name) from
        given file path.

        This method is used for creating suitable file description for _check_files when the
        source is standard input.
        """
        if _is_ignored_file(
            filepath,
            self.config.ignore,
            self.config.ignore_patterns,
            self.config.ignore_paths,
        ):
            return

        try:
            # Note that this function does not really perform an
            # __import__ but may raise an ImportError exception, which
            # we want to catch here.
            modname = ".".join(astroid.modutils.modpath_from_file(filepath))
        except ImportError:
            modname = os.path.splitext(os.path.basename(filepath))[0]

        yield FileItem(modname, filepath, filepath)

    def _iterate_file_descrs(
        self, files_or_modules: Sequence[str]
    ) -> Iterator[FileItem]:
        """Return generator yielding file descriptions (tuples of module name, file
        path, base name).

        The returned generator yield one item for each Python module that should be linted.
        """
        for descr in self._expand_files(files_or_modules).values():
            name, filepath, is_arg = descr["name"], descr["path"], descr["isarg"]
            if self.should_analyze_file(name, filepath, is_argument=is_arg):
                yield FileItem(name, filepath, descr["basename"])

    def _expand_files(
        self, files_or_modules: Sequence[str]
    ) -> dict[str, ModuleDescriptionDict]:
        """Get modules and errors from a list of modules and handle errors."""
        result, errors = expand_modules(
            files_or_modules,
            self.config.source_roots,
            self.config.ignore,
            self.config.ignore_patterns,
            self._ignore_paths,
        )
        for error in errors:
            message = modname = error["mod"]
            key = error["key"]
            self.set_current_module(modname)
            if key == "fatal":
                message = str(error["ex"]).replace(os.getcwd() + os.sep, "")
            self.add_message(key, args=message)
        return result

    def set_current_module(self, modname: str, filepath: str | None = None) -> None:
        """Set the name of the currently analyzed module and
        init statistics for it.
        """
        if not modname and filepath is None:
            return
        self.reporter.on_set_current_module(modname or "", filepath)
        self.current_name = modname
        self.current_file = filepath or modname
        self.stats.init_single_module(modname or "")

        # If there is an actual filepath we might need to update the config attribute
        if filepath:
            namespace = self._get_namespace_for_file(
                Path(filepath), self._directory_namespaces
            )
            if namespace:
                self.config = namespace or self._base_config

    def _get_namespace_for_file(
        self, filepath: Path, namespaces: DirectoryNamespaceDict
    ) -> argparse.Namespace | None:
        for directory in namespaces:
            if Path.is_relative_to(filepath, directory):
                namespace = self._get_namespace_for_file(
                    filepath, namespaces[directory][1]
                )
                if namespace is None:
                    return namespaces[directory][0]
        return None

    @contextlib.contextmanager
    def _astroid_module_checker(
        self,
    ) -> Iterator[Callable[[nodes.Module], bool | None]]:
        """Context manager for checking ASTs.

        The value in the context is callable accepting AST as its only argument.
        """
        walker = ASTWalker(self)
        _checkers = self.prepare_checkers()
        tokencheckers = [
            c for c in _checkers if isinstance(c, checkers.BaseTokenChecker)
        ]
        rawcheckers = [
            c for c in _checkers if isinstance(c, checkers.BaseRawFileChecker)
        ]
        for checker in _checkers:
            checker.open()
            walker.add_checker(checker)

        yield functools.partial(
            self.check_astroid_module,
            walker=walker,
            tokencheckers=tokencheckers,
            rawcheckers=rawcheckers,
        )

        # notify global end
        self.stats.statement = walker.nbstatements
        for checker in reversed(_checkers):
            checker.close()

    def get_ast(
        self, filepath: str, modname: str, data: str | None = None
    ) -> nodes.Module | None:
        """Return an ast(roid) representation of a module or a string.

        :param filepath: path to checked file.
        :param str modname: The name of the module to be checked.
        :param str data: optional contents of the checked file.
        :returns: the AST
        :rtype: astroid.nodes.Module
        :raises AstroidBuildingError: Whenever we encounter an unexpected exception
        """
        try:
            if data is None:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = f.read()
                _data_lines = data.splitlines()

                _xonsh = _analyze_xonsh_file(file_path=filepath, file_str=None)

                # comment the lines
                if _xonsh != None and len(_xonsh['all_non_python_lines']) > 0:
                    for _line in _xonsh['all_non_python_lines']:
                        _data_lines[_line -1] = "# " + _data_lines[_line -1]

                data = "\n".join(_data_lines)
            else:
                _data_lines = data.splitlines()
                _xonsh = _analyze_xonsh_file(file_path=None, file_str=data)

                # comment the lines
                if _xonsh != None and len(_xonsh['all_non_python_lines']) > 0:
                    for _line in _xonsh['all_non_python_lines']:
                        _data_lines[_line -1] = "# " + _data_lines[_line -1]

                data = "\n".join(_data_lines)

            return astroid.builder.AstroidBuilder(MANAGER).string_build(
                data, modname, filepath
            )
        except astroid.AstroidSyntaxError as ex:
            line = getattr(ex.error, "lineno", None)
            if line is None:
                line = 0
            self.add_message(
                "syntax-error",
                line=line,
                col_offset=getattr(ex.error, "offset", None),
                args=f"Parsing failed: '{ex.error}'",
                confidence=HIGH,
            )
        except astroid.AstroidBuildingError as ex:
            self.add_message("parse-error", args=ex)
        except Exception as ex:
            traceback.print_exc()
            # We raise BuildingError here as this is essentially an astroid issue
            # Creating an issue template and adding the 'astroid-error' message is handled
            # by caller: _check_files
            raise astroid.AstroidBuildingError(
                "Building error when trying to create ast representation of module '{modname}'",
                modname=modname,
            ) from ex
        return None

    def check_astroid_module(
        self,
        ast_node: nodes.Module,
        walker: ASTWalker,
        rawcheckers: list[checkers.BaseRawFileChecker],
        tokencheckers: list[checkers.BaseTokenChecker],
    ) -> bool | None:
        """Check a module from its astroid representation.

        For return value see _check_astroid_module
        """
        before_check_statements = walker.nbstatements

        retval = self._check_astroid_module(
            ast_node, walker, rawcheckers, tokencheckers
        )
        self.stats.by_module[self.current_name]["statement"] = (
            walker.nbstatements - before_check_statements
        )

        return retval

    def _check_astroid_module(
        self,
        node: nodes.Module,
        walker: ASTWalker,
        rawcheckers: list[checkers.BaseRawFileChecker],
        tokencheckers: list[checkers.BaseTokenChecker],
    ) -> bool | None:
        """Check given AST node with given walker and checkers.

        :param astroid.nodes.Module node: AST node of the module to check
        :param pylint.utils.ast_walker.ASTWalker walker: AST walker
        :param list rawcheckers: List of token checkers to use
        :param list tokencheckers: List of raw checkers to use

        :returns: True if the module was checked, False if ignored,
            None if the module contents could not be parsed
        """
        try:
            tokens = utils.tokenize_module(node)
        except tokenize.TokenError as ex:
            self.add_message(
                "syntax-error",
                line=ex.args[1][0],
                col_offset=ex.args[1][1],
                args=ex.args[0],
                confidence=HIGH,
            )
            return None

        if not node.pure_python:
            self.add_message("raw-checker-failed", args=node.name)
        else:
            # assert astroid.file.endswith('.py')
            # Parse module/block level option pragma's
            self.process_tokens(tokens)
            if self._ignore_file:
                return False
            # run raw and tokens checkers
            for raw_checker in rawcheckers:
                raw_checker.process_module(node)
            for token_checker in tokencheckers:
                token_checker.process_tokens(tokens)
        # generate events to astroid checkers
        walker.walk(node)
        return True

    def open(self) -> None:
        """Initialize counters."""
        MANAGER.always_load_extensions = self.config.unsafe_load_any_extension
        MANAGER.max_inferable_values = self.config.limit_inference_results
        MANAGER.extension_package_whitelist.update(self.config.extension_pkg_allow_list)
        MANAGER.module_denylist.update(self.config.ignored_modules)
        MANAGER.prefer_stubs = self.config.prefer_stubs
        if self.config.extension_pkg_whitelist:
            MANAGER.extension_package_whitelist.update(
                self.config.extension_pkg_whitelist
            )
        self.stats.reset_message_count()

    def generate_reports(self, verbose: bool = False) -> int | None:
        """Close the whole package /module, it's time to make reports !

        if persistent run, pickle results for later comparison
        """
        # Display whatever messages are left on the reporter.
        self.reporter.display_messages(report_nodes.Section())
        if not self.file_state._is_base_filestate:
            # load previous results if any
            previous_stats = load_results(self.file_state.base_name)
            self.reporter.on_close(self.stats, previous_stats)
            if self.config.reports:
                sect = self.make_reports(self.stats, previous_stats)
            else:
                sect = report_nodes.Section()

            if self.config.reports:
                self.reporter.display_reports(sect)
            score_value = self._report_evaluation(verbose)
            # save results if persistent run
            if self.config.persistent:
                save_results(self.stats, self.file_state.base_name)
        else:
            self.reporter.on_close(self.stats, LinterStats())
            score_value = None
        return score_value

    def _report_evaluation(self, verbose: bool = False) -> int | None:
        """Make the global evaluation report."""
        # check with at least a statement (usually 0 when there is a
        # syntax error preventing pylint from further processing)
        note = None
        previous_stats = load_results(self.file_state.base_name)
        if self.stats.statement == 0:
            return note

        # get a global note for the code
        evaluation = self.config.evaluation
        try:
            stats_dict = {
                "fatal": self.stats.fatal,
                "error": self.stats.error,
                "warning": self.stats.warning,
                "refactor": self.stats.refactor,
                "convention": self.stats.convention,
                "statement": self.stats.statement,
                "info": self.stats.info,
            }
            note = eval(evaluation, {}, stats_dict)  # pylint: disable=eval-used
        except Exception as ex:  # pylint: disable=broad-except
            msg = f"An exception occurred while rating: {ex}"
        else:
            self.stats.global_note = note
            msg = f"Your code has been rated at {note:.2f}/10"
            if previous_stats:
                pnote = previous_stats.global_note
                if pnote is not None:
                    msg += f" (previous run: {pnote:.2f}/10, {note - pnote:+.2f})"

            if verbose:
                checked_files_count = self.stats.node_count["module"]
                unchecked_files_count = self.stats.undocumented["module"]
                msg += f"\nChecked {checked_files_count} files, skipped {unchecked_files_count} files"

        if self.config.score:
            sect = report_nodes.EvaluationSection(msg)
            self.reporter.display_reports(sect)
        return note

    def _add_one_message(
        self,
        message_definition: MessageDefinition,
        line: int | None,
        node: nodes.NodeNG | None,
        args: Any | None,
        confidence: interfaces.Confidence | None,
        col_offset: int | None,
        end_lineno: int | None,
        end_col_offset: int | None,
    ) -> None:
        """After various checks have passed a single Message is
        passed to the reporter and added to stats.
        """
        message_definition.check_message_definition(line, node)

        # Look up "location" data of node if not yet supplied
        if node:
            if node.position:
                if not line:
                    line = node.position.lineno
                if not col_offset:
                    col_offset = node.position.col_offset
                if not end_lineno:
                    end_lineno = node.position.end_lineno
                if not end_col_offset:
                    end_col_offset = node.position.end_col_offset
            else:
                if not line:
                    line = node.fromlineno
                if not col_offset:
                    col_offset = node.col_offset
                if not end_lineno:
                    end_lineno = node.end_lineno
                if not end_col_offset:
                    end_col_offset = node.end_col_offset

        # should this message be displayed
        if not self.is_message_enabled(message_definition.msgid, line, confidence):
            self.file_state.handle_ignored_message(
                self._get_message_state_scope(
                    message_definition.msgid, line, confidence
                ),
                message_definition.msgid,
                line,
            )
            return

        # update stats
        msg_cat = MSG_TYPES[message_definition.msgid[0]]
        self.msg_status |= MSG_TYPES_STATUS[message_definition.msgid[0]]
        self.stats.increase_single_message_count(msg_cat, 1)
        self.stats.increase_single_module_message_count(self.current_name, msg_cat, 1)
        try:
            self.stats.by_msg[message_definition.symbol] += 1
        except KeyError:
            self.stats.by_msg[message_definition.symbol] = 1
        # Interpolate arguments into message string
        msg = message_definition.msg
        if args is not None:
            msg %= args
        # get module and object
        if node is None:
            module, obj = self.current_name, ""
            abspath = self.current_file
        else:
            module, obj = utils.get_module_and_frameid(node)
            abspath = node.root().file
        if abspath is not None:
            path = abspath.replace(self.reporter.path_strip_prefix, "", 1)
        else:
            path = "configuration"
        # add the message
        self.reporter.handle_message(
            Message(
                message_definition.msgid,
                message_definition.symbol,
                MessageLocationTuple(
                    abspath or "",
                    path,
                    module or "",
                    obj,
                    line or 1,
                    col_offset or 0,
                    end_lineno,
                    end_col_offset,
                ),
                msg,
                confidence,
            )
        )

    def add_message(
        self,
        msgid: str,
        line: int | None = None,
        node: nodes.NodeNG | None = None,
        args: Any | None = None,
        confidence: interfaces.Confidence | None = None,
        col_offset: int | None = None,
        end_lineno: int | None = None,
        end_col_offset: int | None = None,
    ) -> None:
        """Adds a message given by ID or name.

        If provided, the message string is expanded using args.

        AST checkers must provide the node argument (but may optionally
        provide line if the line number is different), raw and token checkers
        must provide the line argument.
        """
        if confidence is None:
            confidence = interfaces.UNDEFINED
        message_definitions = self.msgs_store.get_message_definitions(msgid)
        for message_definition in message_definitions:
            self._add_one_message(
                message_definition,
                line,
                node,
                args,
                confidence,
                col_offset,
                end_lineno,
                end_col_offset,
            )

    def add_ignored_message(
        self,
        msgid: str,
        line: int,
        node: nodes.NodeNG | None = None,
        confidence: interfaces.Confidence | None = interfaces.UNDEFINED,
    ) -> None:
        """Prepares a message to be added to the ignored message storage.

        Some checks return early in special cases and never reach add_message(),
        even though they would normally issue a message.
        This creates false positives for useless-suppression.
        This function avoids this by adding those message to the ignored msgs attribute
        """
        message_definitions = self.msgs_store.get_message_definitions(msgid)
        for message_definition in message_definitions:
            message_definition.check_message_definition(line, node)
            self.file_state.handle_ignored_message(
                self._get_message_state_scope(
                    message_definition.msgid, line, confidence
                ),
                message_definition.msgid,
                line,
            )

    def _emit_stashed_messages(self) -> None:
        for keys, values in self._stashed_messages.items():
            modname, symbol = keys
            self.linter.set_current_module(modname)
            for args in values:
                self.add_message(
                    symbol,
                    args=args,
                    line=0,
                    confidence=HIGH,
                )
        self._stashed_messages = collections.defaultdict(list)
