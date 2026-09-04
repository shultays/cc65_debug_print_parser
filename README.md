# cc65 Debug Print Parser

Generate Mesen 2 Lua execution watchers from `@debug_print*` labels in a cc65/ld65 `.dbg` file.

The idea is to put debug logging directly in your ca65 source without adding runtime code to the NES program. The Python script reads the linker debug file, finds `@debug_print*` labels, resolves their source lines and CPU addresses, and generates a Mesen Lua script that installs execution callbacks at those addresses.

## Example

In ca65 source:

```asm
@debug_print: ; "collision %.d[c_x] %.d[c_y] %a"
```

When the CPU executes `@debug_print`, the generated Mesen Lua callback logs something like:

```text
collision 12 27 3F
```

The expressions inside the string use Mesen-style NES debugger syntax.

## Format syntax

`%` starts an expression.

```text
%EXPR       hexadecimal output (default)
%.dEXPR     decimal output
%.bEXPR     binary output
%%          literal %
```

Examples:

```asm
@debug_print: ; "A=%a X=%x Y=%y"
@debug_print: ; "frame=%.dframe"
@debug_print: ; "x=%[c_x] y=%.d[c_y]"
```

## Labels and memory

A bare label evaluates to its CPU address:

```text
%c_x
```

An 8-bit memory read uses square brackets:

```text
%[c_x]
%.d[c_x]
%.b[c_x]
```

A 16-bit little-endian memory read uses braces:

```text
%{temp_ptr}
```

If `temp_ptr` contains `$6000`, `%{temp_ptr}` prints `6000`.

Memory operators can be nested. To read the byte pointed to by `temp_ptr`:

```text
%[{temp_ptr}]
```

To read the next byte:

```text
%[{temp_ptr}+1]
```

A 32-bit little-endian memory read is also supported:

```text
%#{address}
```

## Expressions

Basic arithmetic, comparisons, logical operations, bitwise operations, and nested memory expressions are supported.

```text
+  -  *  /  %
<< >>
&  |  ^  ~
== != < <= > >=
&& || !
(...)
```

Examples:

```asm
@debug_print: ; "tile=%[{temp_ptr}+x]"
@debug_print: ; "next=%.d[{temp_ptr}+1]"
@debug_print: ; "indexed=%[array_base+x]"
```

## NES values

Common Mesen NES debugger values are supported directly, including:

```text
a x y sp pc ps
frame cycle scanline
v t nmi irq
pscarry pszero psinterrupt psdecimal psoverflow psnegative
sprite0hit verticalblank spriteoverflow
```

Examples:

```asm
@debug_print: ; "A=%a X=%x PC=%pc"
@debug_print: ; "frame=%.dframe cycle=%.dcycle"
@debug_print: ; "C=%.dpscarry Z=%.dpszero N=%.dpsnegative"
```

## Debug print labels

Any symbol whose name starts with `@debug_print` is treated as a watcher:

```asm
@debug_print:
@debug_print_collision:
@debug_print_stream_right:
```

The format string must be on the same source line as the label:

```asm
@debug_print_collision: ; "collision %.d[c_x] %.d[c_y] %a"
```

No separate argument list is needed; everything is embedded in the quoted expression string.

## Usage

Generate the `.dbg` file with ld65, for example:

```text
ld65 ... --dbgfile build/waymark.dbg
```

Then run:

```text
python cc65_debug_print_parser.py build/waymark.dbg
```

By default this creates:

```text
build/waymark_debug.lua
```

Load the generated Lua file in Mesen 2's Script Window.

If the `.dbg` file only stores source basenames, run the script from the project root or specify it explicitly:

```text
python cc65_debug_print_parser.py build/waymark.dbg --source-root .
```

Choose a custom Lua output path:

```text
python cc65_debug_print_parser.py build/waymark.dbg -o mesen_debug.lua
```

List all resolved watchers while generating:

```text
python cc65_debug_print_parser.py build/waymark.dbg --list
```

## How it works

The script parses these cc65 debug records:

```text
sym   -> @debug_print symbol address and definition id
line  -> source file id and line number
file  -> source filename
```

It then reads the source line, compiles the embedded Mesen-style expression syntax into Lua, and creates an `emu.callbackType.exec` callback for each debug-print address.

Mesen's Lua API does not expose the debugger's native expression evaluator directly, so this project implements the useful NES expression forms needed by the generated watchers rather than passing the expression string to Mesen itself.

## Requirements

- Python 3.9+
- cc65/ld65 `.dbg` output
- Mesen 2
