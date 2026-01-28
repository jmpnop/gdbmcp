# GDB MCP Server

A Model Context Protocol (MCP) server that provides GDB debugging capabilities to LLM agents like Claude.

## Features

- Interactive GDB control via MCP protocol
- Process attach/detach
- Hardware and software breakpoints
- Memory and register reading
- Breakpoint hit detection
- Session persistence across tool calls

## Installation

Requires Python 3.8+ and GDB.

```bash
# Clone the repository
git clone https://github.com/jmpnop/gdbmcp.git
cd gdbmcp

# Make executable
chmod +x gdb_mcp_server.py
```

## Configuration

Add to Claude Code MCP settings (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "gdb": {
      "command": "python3",
      "args": ["/path/to/gdb_mcp_server.py"],
      "env": {}
    }
  }
}
```

## Available Tools

| Tool | Description |
|------|-------------|
| `gdb_start` | Start GDB process |
| `gdb_stop` | Stop GDB process |
| `gdb_attach` | Attach to process by PID |
| `gdb_detach` | Detach from process |
| `gdb_breakpoint` | Set breakpoint at address |
| `gdb_delete_breakpoint` | Remove breakpoint |
| `gdb_continue` | Continue execution |
| `gdb_interrupt` | Pause execution |
| `gdb_read_register` | Read CPU register |
| `gdb_read_memory` | Read memory bytes |
| `gdb_get_hits` | Get breakpoint hit events |
| `gdb_status` | Get session status |
| `gdb_command` | Execute raw GDB/MI command |

## Usage Example

```
1. gdb_start()
2. gdb_attach(pid=12345)
3. gdb_breakpoint(address="0x401000", hardware=true)
4. gdb_continue()
5. [wait for breakpoint hit]
6. gdb_get_hits()
7. gdb_read_register(register="rax")
8. gdb_read_memory(address="0x7fff...", size=256)
9. gdb_detach()
10. gdb_stop()
```

## Testing

```bash
python3 test_gdb_mcp.py
```

Note: Some tests require root privileges for ptrace.

## License

MIT
