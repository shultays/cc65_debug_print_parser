#!/usr/bin/env python3
"""Generate Mesen 2 Lua execution watchers from cc65/ld65 .dbg debug-print labels.

Source syntax:
    @debug_print_collision: ; "collision %d %d %x" c_x, c_y, !a

Supported format specifiers:
    %d  direct value, decimal
    %x  direct value, hexadecimal (minimum 2 digits)
    %b  direct value, 8-bit binary
    %p  16-bit pointer variable value, hexadecimal (4 digits)
    %v  byte at address stored in a 16-bit pointer variable, decimal
    %a  byte at (address stored in pointer variable + index), decimal
        Consumes TWO arguments: pointer variable, index
    %%  literal percent sign

Direct arguments (%d/%x/%b and %a index):
    symbol      read one byte from the symbol's CPU address
    !a !x !y    CPU registers
    !sp !pc !f  stack pointer, program counter, processor status
    123         decimal constant
    $7F         hexadecimal constant (ca65 style)
    0x7F        hexadecimal constant

Pointer arguments (%p/%v and first argument of %a):
    symbol      the symbol is treated as the address of a 2-byte little-endian
                pointer variable (e.g. temp_ptr)
    123/$20/... numeric literals are treated as the CPU address containing
                the 2-byte pointer variable

Example:
    @debug_print: ; "collision %d %d A=%x ptr=%p value=%v arr=%a" c_x, c_y, !a, temp_ptr, temp_ptr, temp_ptr, !x
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


DEBUG_PREFIX = "@debug_print"
FORMAT_SPECS = {"d", "x", "b", "p", "v", "a"}
REGISTER_EXPRESSIONS = {
    "!a": 'state["cpu.a"]',
    "!x": 'state["cpu.x"]',
    "!y": 'state["cpu.y"]',
    "!sp": 'state["cpu.sp"]',
    "!pc": 'state["cpu.pc"]',
    "!f": 'state["cpu.status"]',
}


class GeneratorError(Exception):
    pass


@dataclass(frozen=True)
class SourceLocation:
    file_id: int
    line_number: int


@dataclass(frozen=True)
class Symbol:
    name: str
    value: int
    definition_id: Optional[int]
    size: Optional[int]
    raw_fields: Dict[str, str]


@dataclass(frozen=True)
class DebugPrint:
    symbol: Symbol
    source_file: Path
    source_line_number: int
    source_text: str
    format_string: str
    arguments: Tuple[str, ...]


@dataclass(frozen=True)
class FormatPiece:
    literal: Optional[str] = None
    spec: Optional[str] = None


def parse_dbg_fields(line: str) -> Tuple[str, Dict[str, str]]:
    """Parse one cc65 .dbg line.

    The current .dbg format uses comma-separated key=value fields. Quoted file
    and symbol names do not normally contain commas, but this parser still
    handles commas inside quotes.
    """
    if "\t" not in line:
        raise GeneratorError(f"Malformed .dbg line (missing tab): {line!r}")

    record_type, data = line.rstrip("\r\n").split("\t", 1)
    fields: Dict[str, str] = {}

    current: List[str] = []
    parts: List[str] = []
    in_quotes = False
    escaped = False

    for ch in data:
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if ch == "\\" and in_quotes:
            current.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
            continue
        if ch == "," and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))

    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
            value = value.replace('\\"', '"').replace("\\\\", "\\")
        fields[key] = value

    return record_type, fields


def parse_int(text: str) -> int:
    text = text.strip()
    if text.startswith("$"):
        return int(text[1:], 16)
    return int(text, 0)


def parse_dbg(path: Path) -> Tuple[Dict[int, str], Dict[int, SourceLocation], List[Symbol], Dict[str, List[Symbol]]]:
    files: Dict[int, str] = {}
    lines: Dict[int, SourceLocation] = {}
    symbols: List[Symbol] = []
    symbols_by_name: Dict[str, List[Symbol]] = {}

    with path.open("r", encoding="utf-8") as f:
        for line_number, raw_line in enumerate(f, start=1):
            raw_line = raw_line.rstrip("\r\n")
            if not raw_line:
                continue

            try:
                record_type, fields = parse_dbg_fields(raw_line)
            except GeneratorError as exc:
                raise GeneratorError(f"{path}:{line_number}: {exc}") from exc

            try:
                if record_type == "file":
                    files[int(fields["id"])] = fields["name"]

                elif record_type == "line":
                    lines[int(fields["id"])] = SourceLocation(
                        file_id=int(fields["file"]),
                        line_number=int(fields["line"]),
                    )

                elif record_type == "sym" and "name" in fields and "val" in fields:
                    symbol = Symbol(
                        name=fields["name"],
                        value=parse_int(fields["val"]),
                        definition_id=int(fields["def"].split("+", 1)[0]) if "def" in fields else None,
                        size=int(fields["size"]) if "size" in fields else None,
                        raw_fields=fields,
                    )
                    symbols.append(symbol)
                    symbols_by_name.setdefault(symbol.name, []).append(symbol)

            except (KeyError, ValueError) as exc:
                raise GeneratorError(
                    f"{path}:{line_number}: malformed {record_type!r} record: {raw_line}"
                ) from exc

    return files, lines, symbols, symbols_by_name


class SourceResolver:
    def __init__(self, source_root: Path):
        self.source_root = source_root.resolve()
        self._basename_index: Optional[Dict[str, List[Path]]] = None
        self._line_cache: Dict[Path, List[str]] = {}

    def _build_index(self) -> None:
        index: Dict[str, List[Path]] = {}
        skip_dirs = {".git", ".hg", ".svn", "__pycache__"}

        for root, dirs, filenames in os.walk(self.source_root):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            root_path = Path(root)
            for filename in filenames:
                index.setdefault(filename, []).append((root_path / filename).resolve())

        self._basename_index = index

    def resolve(self, dbg_name: str) -> Path:
        dbg_path = Path(dbg_name)

        # First honor a relative path recorded in the .dbg file.
        candidate = (self.source_root / dbg_path).resolve()
        if candidate.is_file():
            return candidate

        # Then fall back to basename lookup. cc65 often records only the basename.
        if self._basename_index is None:
            self._build_index()

        assert self._basename_index is not None
        matches = self._basename_index.get(dbg_path.name, [])

        if not matches:
            raise GeneratorError(
                f"Could not find source file {dbg_name!r} under {self.source_root}. "
                f"Use --source-root to point at the project root."
            )
        if len(matches) > 1:
            choices = "\n    ".join(str(p) for p in matches)
            raise GeneratorError(
                f"Source file {dbg_name!r} is ambiguous under {self.source_root}:\n    {choices}"
            )
        return matches[0]

    def read_line(self, path: Path, line_number: int) -> str:
        if path not in self._line_cache:
            try:
                self._line_cache[path] = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                self._line_cache[path] = path.read_text(encoding="latin-1").splitlines()

        lines = self._line_cache[path]
        if line_number < 1 or line_number > len(lines):
            raise GeneratorError(
                f"{path}:{line_number}: line is outside the file (file has {len(lines)} lines)"
            )
        return lines[line_number - 1]


def split_arguments(text: str) -> Tuple[str, ...]:
    if not text.strip():
        return ()
    args = tuple(part.strip() for part in text.split(" "))
    if any(not arg for arg in args):
        raise GeneratorError(f"Malformed argument list: {text!r}")
    return args


def parse_debug_comment(source_text: str, location: str) -> Tuple[str, Tuple[str, ...]]:
    # Everything after the assembly comment marker is our mini-language.
    if ";" not in source_text:
        raise GeneratorError(f'{location}: debug-print line has no `; "format" ...` comment')

    comment = source_text.split(";", 1)[1].strip()
    match = re.fullmatch(r'"((?:[^"\\]|\\.)*)"\s*(.*)', comment)
    if not match:
        raise GeneratorError(
            f'{location}: expected comment syntax: ; "format string" arg1, arg2'
        )

    format_string = match.group(1)
    # Interpret the two escapes that are useful here without turning arbitrary
    # backslashes into Python escapes.
    format_string = format_string.replace('\\"', '"').replace('\\\\', '\\')
    arguments = split_arguments(match.group(2))
    return format_string, arguments


def parse_format_string(fmt: str, location: str) -> Tuple[FormatPiece, ...]:
    pieces: List[FormatPiece] = []
    literal: List[str] = []
    i = 0

    def flush_literal() -> None:
        if literal:
            pieces.append(FormatPiece(literal="".join(literal)))
            literal.clear()

    while i < len(fmt):
        ch = fmt[i]
        if ch != "%":
            literal.append(ch)
            i += 1
            continue

        if i + 1 >= len(fmt):
            raise GeneratorError(f"{location}: trailing '%' in format string")

        spec = fmt[i + 1]
        if spec == "%":
            literal.append("%")
            i += 2
            continue
        if spec not in FORMAT_SPECS:
            raise GeneratorError(f"{location}: unsupported format specifier %{spec}")

        flush_literal()
        pieces.append(FormatPiece(spec=spec))
        i += 2

    flush_literal()
    return tuple(pieces)


def resolve_unique_symbol(name: str, symbols_by_name: Dict[str, List[Symbol]], location: str) -> Symbol:
    candidates = symbols_by_name.get(name)
    if not candidates:
        raise GeneratorError(f"{location}: unknown symbol {name!r}")

    # Multiple debug records can legitimately describe the same absolute symbol.
    values = {s.value for s in candidates}
    if len(values) != 1:
        details = ", ".join(f"0x{s.value:04X}" for s in candidates)
        raise GeneratorError(f"{location}: symbol {name!r} is ambiguous ({details})")

    return candidates[0]


def numeric_literal(token: str) -> Optional[int]:
    token = token.strip()
    try:
        if token.startswith("$"):
            return int(token[1:], 16)
        if re.fullmatch(r"[0-9]+", token):
            return int(token, 10)
        if re.fullmatch(r"0[xX][0-9a-fA-F]+", token):
            return int(token, 16)
    except ValueError:
        return None
    return None


def direct_value_expr(token: str, symbols_by_name: Dict[str, List[Symbol]], location: str) -> str:
    token = token.strip().lower() if token.strip().startswith("!") else token.strip()

    if token in REGISTER_EXPRESSIONS:
        return REGISTER_EXPRESSIONS[token]

    if token.startswith("!"):
        supported = ", ".join(REGISTER_EXPRESSIONS)
        raise GeneratorError(f"{location}: unknown register {token!r}; supported: {supported}")

    constant = numeric_literal(token)
    if constant is not None:
        return str(constant)

    symbol = resolve_unique_symbol(token, symbols_by_name, location)
    return f"read8(0x{symbol.value:04X})"


def pointer_storage_expr(token: str, symbols_by_name: Dict[str, List[Symbol]], location: str) -> str:
    token = token.strip()
    if token.startswith("!"):
        raise GeneratorError(
            f"{location}: {token!r} cannot be used as a pointer variable; "
            "use a 2-byte symbol such as temp_ptr"
        )

    constant = numeric_literal(token)
    if constant is not None:
        if not 0 <= constant <= 0xFFFF:
            raise GeneratorError(f"{location}: pointer storage address out of range: {token}")
        return f"0x{constant:04X}"

    symbol = resolve_unique_symbol(token, symbols_by_name, location)
    if symbol.size is not None and symbol.size < 2:
        raise GeneratorError(
            f"{location}: {token!r} has size {symbol.size}, but pointer formats require 2 bytes"
        )
    return f"0x{symbol.value:04X}"


def lua_quote(text: str) -> str:
    return (
        '"'
        + text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        + '"'
    )


def compile_format(
    fmt: str,
    arguments: Sequence[str],
    symbols_by_name: Dict[str, List[Symbol]],
    location: str,
) -> List[str]:
    pieces = parse_format_string(fmt, location)
    lua_parts: List[str] = []
    arg_index = 0

    for piece in pieces:
        if piece.literal is not None:
            if piece.literal:
                lua_parts.append(lua_quote(piece.literal))
            continue

        assert piece.spec is not None
        spec = piece.spec
        needed = 2 if spec == "a" else 1
        if arg_index + needed > len(arguments):
            raise GeneratorError(
                f"{location}: %{spec} needs {needed} argument(s), but the argument list ended early"
            )

        if spec in {"d", "x", "b"}:
            expr = direct_value_expr(arguments[arg_index], symbols_by_name, location)
            arg_index += 1
            if spec == "d":
                lua_parts.append(f"tostring({expr})")
            elif spec == "x":
                lua_parts.append(f"hex({expr})")
            else:
                lua_parts.append(f"bin8({expr})")

        elif spec in {"p", "v"}:
            storage = pointer_storage_expr(arguments[arg_index], symbols_by_name, location)
            arg_index += 1
            if spec == "p":
                lua_parts.append(f"hex16(read16({storage}))")
            else:
                lua_parts.append(f"tostring(read8(read16({storage})))")

        elif spec == "a":
            storage = pointer_storage_expr(arguments[arg_index], symbols_by_name, location)
            index_expr = direct_value_expr(arguments[arg_index + 1], symbols_by_name, location)
            arg_index += 2
            lua_parts.append(
                f"tostring(read8((read16({storage}) + ({index_expr})) % 0x10000))"
            )

    if arg_index != len(arguments):
        extras = ", ".join(arguments[arg_index:])
        raise GeneratorError(f"{location}: unused debug-print argument(s): {extras}")

    return lua_parts


def collect_debug_prints(
    files: Dict[int, str],
    lines: Dict[int, SourceLocation],
    symbols: Sequence[Symbol],
    resolver: SourceResolver,
) -> List[DebugPrint]:
    result: List[DebugPrint] = []

    for symbol in symbols:
        if not symbol.name.startswith(DEBUG_PREFIX):
            continue
        if symbol.definition_id is None:
            raise GeneratorError(f"{symbol.name}: debug-print symbol has no def= entry")

        source_loc = lines.get(symbol.definition_id)
        if source_loc is None:
            raise GeneratorError(
                f"{symbol.name}: def={symbol.definition_id} does not match a line record"
            )

        dbg_filename = files.get(source_loc.file_id)
        if dbg_filename is None:
            raise GeneratorError(
                f"{symbol.name}: line record references unknown file id {source_loc.file_id}"
            )

        source_file = resolver.resolve(dbg_filename)
        source_text = resolver.read_line(source_file, source_loc.line_number)
        location = f"{source_file}:{source_loc.line_number}"
        fmt, args = parse_debug_comment(source_text, location)

        result.append(
            DebugPrint(
                symbol=symbol,
                source_file=source_file,
                source_line_number=source_loc.line_number,
                source_text=source_text,
                format_string=fmt,
                arguments=args,
            )
        )

    return result


LUA_HEADER = r'''-- Auto-generated by generate_mesen_debug.py. Do not edit by hand.
-- Mesen 2 debug-print execution watchers.

local function read8(address)
    return emu.read(address % 0x10000, emu.memType.nesDebug)
end

local function read16(address)
    local lo = read8(address)
    local hi = read8((address + 1) % 0x10000)
    return lo + hi * 0x100
end

local function hex(value)
    value = value % 0x10000
    if value <= 0xFF then
        return string.format("%02X", value)
    end
    return string.format("%04X", value)
end

local function hex16(value)
    return string.format("%04X", value % 0x10000)
end

local function bin8(value)
    value = value % 0x100
    local chars = {}
    for bit = 7, 0, -1 do
        local divisor = 2 ^ bit
        chars[#chars + 1] = (math.floor(value / divisor) % 2 == 1) and "1" or "0"
    end
    return table.concat(chars)
end

'''


def generate_lua(debug_prints: Sequence[DebugPrint], symbols_by_name: Dict[str, List[Symbol]]) -> str:
    out: List[str] = [LUA_HEADER]

    for item in debug_prints:
        location = f"{item.source_file}:{item.source_line_number}"
        parts = compile_format(item.format_string, item.arguments, symbols_by_name, location)

        out.append(f"-- {item.symbol.name} @ 0x{item.symbol.value:04X}")
        out.append(f"-- {item.source_file.name}:{item.source_line_number}: {item.source_text.strip()}")
        out.append("emu.addMemoryCallback(function()")
        out.append("    local state = emu.getState()")
        if parts:
            joined = ", ".join(parts)
            out.append(f"    emu.log(table.concat({{{joined}}}))")
        else:
            out.append('    emu.log("")')
        out.append(f"end, emu.callbackType.exec, 0x{item.symbol.value:04X})")
        out.append("")

    out.append(f'emu.log("Installed {len(debug_prints)} debug-print watcher(s)")')
    out.append("")
    return "\n".join(out)


def default_output_path(dbg_path: Path) -> Path:
    return dbg_path.with_name(dbg_path.stem + "_debug.lua")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate Mesen 2 Lua watchers from @debug_print* labels in a cc65 .dbg file."
    )
    parser.add_argument("dbg", type=Path, help="Path to the cc65/ld65 .dbg file")
    parser.add_argument(
        "-s",
        "--source-root",
        type=Path,
        default=Path.cwd(),
        help="Project/source root used to locate source files (default: current directory)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output Lua path (default: <dbg stem>_debug.lua next to the .dbg file)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print resolved watchers while generating them",
    )
    args = parser.parse_args(argv)

    dbg_path = args.dbg.resolve()
    output_path = (args.output or default_output_path(dbg_path)).resolve()

    try:
        files, lines, symbols, symbols_by_name = parse_dbg(dbg_path)
        resolver = SourceResolver(args.source_root)
        debug_prints = collect_debug_prints(files, lines, symbols, resolver)

        if not debug_prints:
            raise GeneratorError(f"No {DEBUG_PREFIX}* symbols found in {dbg_path}")

        lua = generate_lua(debug_prints, symbols_by_name)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(lua, encoding="utf-8", newline="\n")

        if args.list:
            for item in debug_prints:
                print(
                    f"{item.symbol.name} 0x{item.symbol.value:04X} "
                    f"{item.source_file}:{item.source_line_number}\n"
                    f"    {item.source_text.strip()}"
                )

        print(f"Generated {len(debug_prints)} watcher(s): {output_path}")
        return 0

    except (OSError, GeneratorError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
