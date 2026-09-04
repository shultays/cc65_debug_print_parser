#!/usr/bin/env python3
"""Generate Mesen 2 Lua execution watchers from cc65/ld65 .dbg debug-print labels.

New format syntax: the format string embeds Mesen-style expressions directly.

Examples:
    @debug_print: ; "collision %.d[c_x] %.d[c_y] %a"
    @debug_print_ptr: ; "ptr=%{temp_ptr} value=%[{temp_ptr}] next=%.d[{temp_ptr}+1]"

Formatting:
    %EXPR       evaluate EXPR and print hexadecimal (default)
    %.dEXPR     evaluate EXPR and print decimal
    %.bEXPR     evaluate EXPR and print binary
    %%          literal percent sign

Supported Mesen-like NES expressions include:
    Registers/state: a x y sp pc ps frame cycle scanline v t nmi irq
    Flags: pscarry pszero psinterrupt psdecimal psoverflow psnegative
           sprite0hit verticalblank spriteoverflow
    Labels: any cc65 symbol name from the .dbg file resolves to its CPU address
    Memory:
        [expr]     8-bit memory value at expr
        {expr}     16-bit little-endian memory value at expr
        #{expr}    32-bit little-endian memory value at expr
    Operators:
        + - * / % << >> & | ^ == != < <= > >= && || ! ~
        parentheses are supported

Notes:
- This is a practical Mesen-style expression compiler for generated Lua. Mesen's Lua API
  does not expose the debugger's native expression evaluator directly.
- Debug-print labels are any symbols whose name starts with @debug_print.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

DEBUG_PREFIX = "@debug_print"


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


# -----------------------------
# .dbg parsing
# -----------------------------

def parse_dbg_fields(line: str) -> Tuple[str, Dict[str, str]]:
    if "\t" not in line:
        raise GeneratorError(f"Malformed .dbg line (missing tab): {line!r}")

    record_type, data = line.rstrip("\r\n").split("\t", 1)
    fields: Dict[str, str] = {}
    parts: List[str] = []
    current: List[str] = []
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
            except (KeyError, ValueError, GeneratorError) as exc:
                raise GeneratorError(f"{path}:{line_number}: malformed record: {raw_line}\n{exc}") from exc

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
        candidate = (self.source_root / dbg_path).resolve()
        if candidate.is_file():
            return candidate
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
            raise GeneratorError(f"Source file {dbg_name!r} is ambiguous:\n    {choices}")
        return matches[0]

    def read_line(self, path: Path, line_number: int) -> str:
        if path not in self._line_cache:
            try:
                self._line_cache[path] = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                self._line_cache[path] = path.read_text(encoding="latin-1").splitlines()
        lines = self._line_cache[path]
        if not 1 <= line_number <= len(lines):
            raise GeneratorError(f"{path}:{line_number}: line outside file")
        return lines[line_number - 1]


# -----------------------------
# Debug-print source parsing
# -----------------------------

def parse_debug_comment(source_text: str, location: str) -> str:
    if ";" not in source_text:
        raise GeneratorError(f'{location}: expected ; "format string"')
    comment = source_text.split(";", 1)[1].strip()
    match = re.fullmatch(r'"((?:[^"\\]|\\.)*)"\s*', comment)
    if not match:
        raise GeneratorError(
            f'{location}: expected only a quoted format string after the comment marker'
        )
    fmt = match.group(1)
    return fmt.replace('\\"', '"').replace("\\\\", "\\")


def collect_debug_prints(
    files: Dict[int, str],
    lines: Dict[int, SourceLocation],
    symbols: Sequence[Symbol],
    resolver: SourceResolver,
) -> List[DebugPrint]:
    out: List[DebugPrint] = []
    for symbol in symbols:
        if not symbol.name.startswith(DEBUG_PREFIX):
            continue
        if symbol.definition_id is None:
            raise GeneratorError(f"{symbol.name}: missing def= entry")
        loc = lines.get(symbol.definition_id)
        if loc is None:
            raise GeneratorError(f"{symbol.name}: def={symbol.definition_id} has no line record")
        dbg_filename = files.get(loc.file_id)
        if dbg_filename is None:
            raise GeneratorError(f"{symbol.name}: unknown file id {loc.file_id}")
        source_file = resolver.resolve(dbg_filename)
        source_text = resolver.read_line(source_file, loc.line_number)
        location = f"{source_file}:{loc.line_number}"
        fmt = parse_debug_comment(source_text, location)
        out.append(DebugPrint(symbol, source_file, loc.line_number, source_text, fmt))
    return out


# -----------------------------
# Mesen-like expression compiler
# -----------------------------

@dataclass(frozen=True)
class Token:
    kind: str
    text: str


TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<HEX>\$[0-9A-Fa-f]+|0[xX][0-9A-Fa-f]+)|"
    r"(?P<DEC>[0-9]+)|"
    r"(?P<ID>[A-Za-z_@.][A-Za-z0-9_@.]*)|"
    r"(?P<OP><<|>>|<=|>=|==|!=|&&|\|\||[+\-*/%&|^~!<>()\[\]{}#])"
    r")"
)


class ExprParser:
    def __init__(self, text: str, symbols_by_name: Dict[str, List[Symbol]], location: str):
        self.text = text
        self.symbols_by_name = symbols_by_name
        self.location = location
        self.tokens = self._tokenize(text)
        self.i = 0

    def _tokenize(self, text: str) -> List[Token]:
        pos = 0
        out: List[Token] = []
        while pos < len(text):
            m = TOKEN_RE.match(text, pos)
            if not m:
                raise GeneratorError(f"{self.location}: invalid expression near {text[pos:]!r}")
            pos = m.end()
            kind = m.lastgroup
            assert kind is not None
            out.append(Token(kind, m.group(kind)))
        return out

    def peek(self, text: Optional[str] = None) -> bool:
        if self.i >= len(self.tokens):
            return False
        return text is None or self.tokens[self.i].text == text

    def take(self, text: Optional[str] = None) -> Token:
        if self.i >= len(self.tokens):
            raise GeneratorError(f"{self.location}: unexpected end of expression")
        tok = self.tokens[self.i]
        if text is not None and tok.text != text:
            raise GeneratorError(f"{self.location}: expected {text!r}, got {tok.text!r}")
        self.i += 1
        return tok

    def parse(self) -> str:
        expr = self.parse_or()
        if self.i != len(self.tokens):
            raise GeneratorError(f"{self.location}: unexpected token {self.tokens[self.i].text!r}")
        return expr

    def parse_or(self) -> str:
        left = self.parse_and()
        while self.peek("||"):
            self.take()
            right = self.parse_and()
            left = f"boolnum(({left}) ~= 0 or ({right}) ~= 0)"
        return left

    def parse_and(self) -> str:
        left = self.parse_bitor()
        while self.peek("&&"):
            self.take()
            right = self.parse_bitor()
            left = f"boolnum(({left}) ~= 0 and ({right}) ~= 0)"
        return left

    def parse_bitor(self) -> str:
        left = self.parse_bitxor()
        while self.peek("|"):
            self.take(); right = self.parse_bitxor(); left = f"bit_or({left}, {right})"
        return left

    def parse_bitxor(self) -> str:
        left = self.parse_bitand()
        while self.peek("^"):
            self.take(); right = self.parse_bitand(); left = f"bit_xor({left}, {right})"
        return left

    def parse_bitand(self) -> str:
        left = self.parse_compare()
        while self.peek("&"):
            self.take(); right = self.parse_compare(); left = f"bit_and({left}, {right})"
        return left

    def parse_compare(self) -> str:
        left = self.parse_shift()
        while self.peek() and self.tokens[self.i].text in {"==", "!=", "<", "<=", ">", ">="}:
            op = self.take().text
            right = self.parse_shift()
            luaop = "~=" if op == "!=" else op
            left = f"boolnum(({left}) {luaop} ({right}))"
        return left

    def parse_shift(self) -> str:
        left = self.parse_add()
        while self.peek() and self.tokens[self.i].text in {"<<", ">>"}:
            op = self.take().text; right = self.parse_add()
            fn = "bit_lshift" if op == "<<" else "bit_rshift"
            left = f"{fn}({left}, {right})"
        return left

    def parse_add(self) -> str:
        left = self.parse_mul()
        while self.peek() and self.tokens[self.i].text in {"+", "-"}:
            op = self.take().text; right = self.parse_mul(); left = f"(({left}) {op} ({right}))"
        return left

    def parse_mul(self) -> str:
        left = self.parse_unary()
        while self.peek() and self.tokens[self.i].text in {"*", "/", "%"}:
            op = self.take().text; right = self.parse_unary()
            if op == "/":
                left = f"math.floor(({left}) / ({right}))"
            else:
                left = f"(({left}) {op} ({right}))"
        return left

    def parse_unary(self) -> str:
        if self.peek("+"):
            self.take(); return self.parse_unary()
        if self.peek("-"):
            self.take(); return f"(-({self.parse_unary()}))"
        if self.peek("!"):
            self.take(); return f"boolnum(({self.parse_unary()}) == 0)"
        if self.peek("~"):
            self.take(); return f"bit_not({self.parse_unary()})"
        if self.peek("#"):
            self.take("#")
            self.take("{")
            inner = self.parse_or()
            self.take("}")
            return f"read32({inner})"
        if self.peek("["):
            self.take("[")
            inner = self.parse_or()
            self.take("]")
            return f"read8({inner})"
        if self.peek("{"):
            self.take("{")
            inner = self.parse_or()
            self.take("}")
            return f"read16({inner})"
        if self.peek("("):
            self.take("(")
            inner = self.parse_or()
            self.take(")")
            return f"({inner})"

        tok = self.take()
        if tok.kind == "HEX":
            if tok.text.startswith("$"):
                return str(int(tok.text[1:], 16))
            return str(int(tok.text, 16))
        if tok.kind == "DEC":
            return tok.text
        if tok.kind == "ID":
            return self.compile_identifier(tok.text)
        raise GeneratorError(f"{self.location}: unexpected token {tok.text!r}")

    def compile_identifier(self, name: str) -> str:
        key = name.lower()
        builtins = {
            "a": 'state["cpu.a"]',
            "x": 'state["cpu.x"]',
            "y": 'state["cpu.y"]',
            "sp": 'state["cpu.sp"]',
            "pc": 'state["cpu.pc"]',
            "ps": 'state["cpu.status"]',
            "frame": 'state["ppu.frameCount"]',
            "cycle": 'state["ppu.cycle"]',
            "scanline": 'state["ppu.scanline"]',
            "v": 'state["ppu.v"]',
            "t": 'state["ppu.t"]',
            "nmi": 'boolnum(state["cpu.nmiFlag"] and true or false)',
            "irq": 'boolnum(state["cpu.irqFlag"] and true or false)',
            "pscarry": 'flagbit(state["cpu.status"], 0)',
            "pszero": 'flagbit(state["cpu.status"], 1)',
            "psinterrupt": 'flagbit(state["cpu.status"], 2)',
            "psdecimal": 'flagbit(state["cpu.status"], 3)',
            "psoverflow": 'flagbit(state["cpu.status"], 6)',
            "psnegative": 'flagbit(state["cpu.status"], 7)',
            "sprite0hit": 'state["ppu.sprite0Hit"] and 1 or 0',
            "verticalblank": 'state["ppu.verticalBlank"] and 1 or 0',
            "spriteoverflow": 'state["ppu.spriteOverflow"] and 1 or 0',
        }
        if key in builtins:
            return builtins[key]

        candidates = self.symbols_by_name.get(name)
        if not candidates:
            raise GeneratorError(f"{self.location}: unknown Mesen expression identifier/label {name!r}")
        values = {s.value for s in candidates}
        if len(values) != 1:
            vals = ", ".join(f"0x{s.value:04X}" for s in candidates)
            raise GeneratorError(f"{self.location}: ambiguous label {name!r}: {vals}")
        return str(candidates[0].value)


@dataclass(frozen=True)
class FormatPiece:
    literal: Optional[str] = None
    expression: Optional[str] = None
    mode: str = "x"  # x, d, b


def scan_balanced_expression(fmt: str, start: int, location: str) -> Tuple[str, int]:
    """Scan one expression after %, respecting balanced (), [], {}.

    For bare expressions like %a or %frame, scan an identifier/number token and then
    continue through basic operators/terms until whitespace or a literal delimiter.
    """
    if start >= len(fmt):
        raise GeneratorError(f"{location}: '%' has no expression")

    openers = {"[": "]", "{": "}", "(": ")"}
    if fmt[start] in openers:
        stack = [openers[fmt[start]]]
        i = start + 1
        while i < len(fmt):
            ch = fmt[i]
            if ch in openers:
                stack.append(openers[ch])
            elif stack and ch == stack[-1]:
                stack.pop()
                if not stack:
                    return fmt[start:i + 1], i + 1
            i += 1
        raise GeneratorError(f"{location}: unclosed expression starting at {fmt[start:]!r}")

    # Bare expression: intentionally stop at whitespace/comma/semicolon/colon so
    # literal prose can follow naturally. Operators with no spaces are accepted.
    i = start
    depth_stack: List[str] = []
    while i < len(fmt):
        ch = fmt[i]
        if ch in openers:
            depth_stack.append(openers[ch])
        elif depth_stack and ch == depth_stack[-1]:
            depth_stack.pop()
        elif not depth_stack and ch in " \t,;:":
            break
        elif not depth_stack and ch == "%":
            break
        i += 1
    if i == start:
        raise GeneratorError(f"{location}: empty expression after %")
    return fmt[start:i], i


def parse_format_string(fmt: str, location: str) -> Tuple[FormatPiece, ...]:
    pieces: List[FormatPiece] = []
    literal: List[str] = []
    i = 0

    def flush() -> None:
        if literal:
            pieces.append(FormatPiece(literal="".join(literal)))
            literal.clear()

    while i < len(fmt):
        if fmt[i] != "%":
            literal.append(fmt[i]); i += 1; continue

        if i + 1 < len(fmt) and fmt[i + 1] == "%":
            literal.append("%"); i += 2; continue

        flush()
        i += 1
        mode = "x"
        if i + 1 < len(fmt) and fmt[i] == "." and fmt[i + 1] in {"d", "b", "x"}:
            mode = fmt[i + 1]
            i += 2

        expr, i = scan_balanced_expression(fmt, i, location)
        pieces.append(FormatPiece(expression=expr, mode=mode))

    flush()
    return tuple(pieces)


def lua_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


def compile_format(fmt: str, symbols_by_name: Dict[str, List[Symbol]], location: str) -> List[str]:
    out: List[str] = []
    for piece in parse_format_string(fmt, location):
        if piece.literal is not None:
            if piece.literal:
                out.append(lua_quote(piece.literal))
            continue
        assert piece.expression is not None
        lua_expr = ExprParser(piece.expression, symbols_by_name, location).parse()
        if piece.mode == "d":
            out.append(f"tostring({lua_expr})")
        elif piece.mode == "b":
            out.append(f"bin({lua_expr})")
        else:
            out.append(f"hex({lua_expr})")
    return out


LUA_HEADER = r'''-- Auto-generated by cc65_debug_print_parser_v2.py. Do not edit by hand.
-- Mesen 2 debug-print execution watchers.

local function read8(address)
    return emu.read(address % 0x10000, emu.memType.nesDebug)
end

local function read16(address)
    local lo = read8(address)
    local hi = read8(address + 1)
    return lo + hi * 0x100
end

local function read32(address)
    local b0 = read8(address)
    local b1 = read8(address + 1)
    local b2 = read8(address + 2)
    local b3 = read8(address + 3)
    return b0 + b1 * 0x100 + b2 * 0x10000 + b3 * 0x1000000
end

local function boolnum(v)
    return v and 1 or 0
end

local function flagbit(value, bit)
    return math.floor(value / (2 ^ bit)) % 2
end

local function bit_and(a, b)
    local result, bit = 0, 1
    a = math.floor(a); b = math.floor(b)
    while a > 0 or b > 0 do
        local aa = a % 2; local bb = b % 2
        if aa == 1 and bb == 1 then result = result + bit end
        a = math.floor(a / 2); b = math.floor(b / 2); bit = bit * 2
    end
    return result
end

local function bit_or(a, b)
    local result, bit = 0, 1
    a = math.floor(a); b = math.floor(b)
    while a > 0 or b > 0 do
        local aa = a % 2; local bb = b % 2
        if aa == 1 or bb == 1 then result = result + bit end
        a = math.floor(a / 2); b = math.floor(b / 2); bit = bit * 2
    end
    return result
end

local function bit_xor(a, b)
    local result, bit = 0, 1
    a = math.floor(a); b = math.floor(b)
    while a > 0 or b > 0 do
        local aa = a % 2; local bb = b % 2
        if aa ~= bb then result = result + bit end
        a = math.floor(a / 2); b = math.floor(b / 2); bit = bit * 2
    end
    return result
end

local function bit_not(a)
    return 0xFFFFFFFF - (math.floor(a) % 0x100000000)
end

local function bit_lshift(a, b)
    return (math.floor(a) * (2 ^ math.floor(b))) % 0x100000000
end

local function bit_rshift(a, b)
    return math.floor((math.floor(a) % 0x100000000) / (2 ^ math.floor(b)))
end

local function hex(value)
    value = math.floor(value)
    if value < 0 then value = value % 0x100000000 end
    if value <= 0xFF then return string.format("%02X", value) end
    if value <= 0xFFFF then return string.format("%04X", value) end
    return string.format("%X", value)
end

local function bin(value)
    value = math.floor(value)
    if value < 0 then value = value % 0x100000000 end
    local width = value <= 0xFF and 8 or (value <= 0xFFFF and 16 or 32)
    local chars = {}
    for bit = width - 1, 0, -1 do
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
        parts = compile_format(item.format_string, symbols_by_name, location)
        out.append(f"-- {item.symbol.name} @ 0x{item.symbol.value:04X}")
        out.append(f"-- {item.source_file.name}:{item.source_line_number}: {item.source_text.strip()}")
        out.append("emu.addMemoryCallback(function()")
        out.append("    local state = emu.getState()")
        if parts:
            out.append(f"    emu.log(table.concat({{{', '.join(parts)}}}))")
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
    parser.add_argument("dbg", type=Path)
    parser.add_argument("-s", "--source-root", type=Path, default=Path.cwd())
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--list", action="store_true")
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
                print(f"{item.symbol.name} 0x{item.symbol.value:04X} {item.source_file}:{item.source_line_number}")
                print(f"    {item.source_text.strip()}")
        print(f"Generated {len(debug_prints)} watcher(s): {output_path}")
        return 0
    except (OSError, GeneratorError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
