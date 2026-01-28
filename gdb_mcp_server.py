#!/usr/bin/env python3
"""
GDB MCP Server - Model Context Protocol server for GDB debugging.

Provides interactive GDB control capabilities to LLM agents through
the MCP protocol, enabling automated debugging, memory inspection,
and breakpoint-based analysis.
"""

import json
import os
import re
import select
import subprocess
import sys
import threading
import time
from queue import Queue, Empty
from typing import Any, Optional


class GDBController:
    """Controls a GDB subprocess using the Machine Interface (MI) protocol."""

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.output_queue: Queue = Queue()
        self.reader_thread: Optional[threading.Thread] = None
        self.running = False
        self.breakpoints: dict = {}  # bp_num -> info
        self.bp_hits: list = []  # Pending breakpoint hits
        self.attached_pid: Optional[int] = None
        self.lock = threading.Lock()
        self.gdb_path = os.environ.get("GDB_PATH", "gdb")
        self.timeout = int(os.environ.get("GDB_TIMEOUT", "30"))

    def _reader_loop(self):
        """Background thread to read GDB output."""
        while self.running and self.process:
            try:
                line = self.process.stdout.readline()
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").rstrip()
                self.output_queue.put(line)

                # Check for breakpoint hits
                if line.startswith("*stopped"):
                    self._parse_stopped(line)
            except Exception:
                break

    def _parse_stopped(self, line: str):
        """Parse a *stopped notification for breakpoint hits."""
        if 'reason="breakpoint-hit"' in line:
            hit_info = {"raw": line, "timestamp": time.time()}

            # Extract breakpoint number
            match = re.search(r'bkptno="(\d+)"', line)
            if match:
                hit_info["bkptno"] = int(match.group(1))

            # Extract address
            match = re.search(r'addr="(0x[0-9a-fA-F]+)"', line)
            if match:
                hit_info["addr"] = match.group(1)

            # Extract frame info
            match = re.search(r'frame=\{([^}]+)\}', line)
            if match:
                hit_info["frame"] = match.group(1)

            self.bp_hits.append(hit_info)

    def _send_command(self, cmd: str) -> list:
        """Send a command to GDB and collect response lines."""
        if not self.process:
            raise RuntimeError("GDB not started")

        with self.lock:
            # Clear any pending output
            while True:
                try:
                    self.output_queue.get_nowait()
                except Empty:
                    break

            # Send command
            self.process.stdin.write(f"{cmd}\n".encode())
            self.process.stdin.flush()

            # Collect response until we see a result record
            lines = []
            start_time = time.time()
            while time.time() - start_time < self.timeout:
                try:
                    line = self.output_queue.get(timeout=0.1)
                    lines.append(line)

                    # Result records indicate command completion
                    if line.startswith("^"):
                        break
                except Empty:
                    continue

            return lines

    def _parse_result(self, lines: list) -> tuple[bool, str, dict]:
        """Parse MI result lines into success, message, and data."""
        result_line = None
        for line in lines:
            if line.startswith("^"):
                result_line = line
                break

        if not result_line:
            return False, "No result received", {}

        if result_line.startswith("^done"):
            # Parse key-value pairs from result
            data = {}
            # Simple extraction of common patterns
            for match in re.finditer(r'(\w+)="([^"]*)"', result_line):
                data[match.group(1)] = match.group(2)
            return True, "OK", data

        elif result_line.startswith("^error"):
            match = re.search(r'msg="([^"]*)"', result_line)
            msg = match.group(1) if match else "Unknown error"
            return False, msg, {}

        elif result_line.startswith("^running"):
            return True, "Running", {}

        return False, f"Unknown result: {result_line}", {}

    def start(self) -> dict:
        """Start the GDB process."""
        if self.process:
            return {"error": "GDB already running"}

        try:
            self.process = subprocess.Popen(
                [self.gdb_path, "--interpreter=mi3", "-q"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.running = True
            self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self.reader_thread.start()

            # Wait for initial prompt
            time.sleep(0.5)

            # Drain startup messages
            while True:
                try:
                    self.output_queue.get_nowait()
                except Empty:
                    break

            return {"status": "started", "pid": self.process.pid}
        except Exception as e:
            return {"error": str(e)}

    def stop(self) -> dict:
        """Stop the GDB process."""
        if not self.process:
            return {"error": "GDB not running"}

        self.running = False
        try:
            self.process.stdin.write(b"-gdb-exit\n")
            self.process.stdin.flush()
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()

        self.process = None
        self.attached_pid = None
        self.breakpoints = {}
        self.bp_hits = []
        return {"status": "stopped"}

    def attach(self, pid: int) -> dict:
        """Attach to a process."""
        if not self.process:
            return {"error": "GDB not started"}

        lines = self._send_command(f"-target-attach {pid}")
        success, msg, data = self._parse_result(lines)

        if success:
            self.attached_pid = pid
            return {"status": "attached", "pid": pid}
        return {"error": msg}

    def detach(self) -> dict:
        """Detach from current process."""
        if not self.process:
            return {"error": "GDB not started"}
        if not self.attached_pid:
            return {"error": "Not attached to any process"}

        lines = self._send_command("-target-detach")
        success, msg, _ = self._parse_result(lines)

        if success:
            self.attached_pid = None
            return {"status": "detached"}
        return {"error": msg}

    def set_breakpoint(self, address: str, hardware: bool = False) -> dict:
        """Set a breakpoint at an address."""
        if not self.process:
            return {"error": "GDB not started"}

        hw_flag = "-h " if hardware else ""
        # Use *address for absolute addresses
        addr = address if address.startswith("*") else f"*{address}"
        lines = self._send_command(f"-break-insert {hw_flag}{addr}")
        success, msg, data = self._parse_result(lines)

        if success:
            # Extract breakpoint number
            bp_num = None
            for line in lines:
                match = re.search(r'number="(\d+)"', line)
                if match:
                    bp_num = int(match.group(1))
                    break

            if bp_num:
                self.breakpoints[bp_num] = {
                    "address": address,
                    "hardware": hardware,
                    "number": bp_num,
                }
                return {"status": "breakpoint_set", "number": bp_num, "address": address}
            return {"status": "breakpoint_set", "address": address}
        return {"error": msg}

    def delete_breakpoint(self, number: int) -> dict:
        """Delete a breakpoint by number."""
        if not self.process:
            return {"error": "GDB not started"}

        lines = self._send_command(f"-break-delete {number}")
        success, msg, _ = self._parse_result(lines)

        if success:
            self.breakpoints.pop(number, None)
            return {"status": "breakpoint_deleted", "number": number}
        return {"error": msg}

    def continue_exec(self) -> dict:
        """Continue execution."""
        if not self.process:
            return {"error": "GDB not started"}

        lines = self._send_command("-exec-continue")
        success, msg, _ = self._parse_result(lines)

        if success:
            return {"status": "running"}
        return {"error": msg}

    def interrupt(self) -> dict:
        """Interrupt (pause) execution."""
        if not self.process:
            return {"error": "GDB not started"}

        lines = self._send_command("-exec-interrupt")
        success, msg, _ = self._parse_result(lines)

        if success:
            return {"status": "interrupted"}
        return {"error": msg}

    def read_register(self, register: str) -> dict:
        """Read a CPU register value."""
        if not self.process:
            return {"error": "GDB not started"}

        # Use data-evaluate-expression for register reading
        lines = self._send_command(f'-data-evaluate-expression ${register}')
        success, msg, data = self._parse_result(lines)

        if success:
            # Extract value from response
            value = data.get("value", "")
            if not value:
                for line in lines:
                    match = re.search(r'value="([^"]*)"', line)
                    if match:
                        value = match.group(1)
                        break
            return {"register": register, "value": value}
        return {"error": msg}

    def read_memory(self, address: str, size: int) -> dict:
        """Read memory at an address."""
        if not self.process:
            return {"error": "GDB not started"}
        if size > 4096:
            return {"error": "Size exceeds maximum (4096 bytes)"}

        lines = self._send_command(f"-data-read-memory-bytes {address} {size}")
        success, msg, data = self._parse_result(lines)

        if success:
            # Extract memory contents
            contents = ""
            for line in lines:
                match = re.search(r'contents="([0-9a-fA-F]*)"', line)
                if match:
                    contents = match.group(1)
                    break
            return {"address": address, "size": size, "contents": contents}
        return {"error": msg}

    def get_hits(self) -> dict:
        """Get pending breakpoint hits and clear them."""
        hits = self.bp_hits.copy()
        self.bp_hits = []
        return {"hits": hits, "count": len(hits)}

    def get_status(self) -> dict:
        """Get current GDB session status."""
        return {
            "running": self.process is not None,
            "attached_pid": self.attached_pid,
            "breakpoints": list(self.breakpoints.values()),
            "pending_hits": len(self.bp_hits),
        }

    def raw_command(self, command: str) -> dict:
        """Execute a raw GDB/MI command."""
        if not self.process:
            return {"error": "GDB not started"}

        lines = self._send_command(command)
        return {"lines": lines}


class MCPServer:
    """MCP Server implementing the Model Context Protocol."""

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self):
        self.gdb = GDBController()
        self.tools = {
            "gdb_start": {
                "description": "Start the GDB process",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            "gdb_stop": {
                "description": "Stop the GDB process",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            "gdb_attach": {
                "description": "Attach GDB to a process",
                "inputSchema": {
                    "type": "object",
                    "properties": {"pid": {"type": "integer", "description": "Process ID to attach to"}},
                    "required": ["pid"],
                },
            },
            "gdb_detach": {
                "description": "Detach GDB from current process",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            "gdb_breakpoint": {
                "description": "Set a breakpoint at an address",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "Address (e.g., 0x7f1234567890)"},
                        "hardware": {"type": "boolean", "description": "Use hardware breakpoint", "default": False},
                    },
                    "required": ["address"],
                },
            },
            "gdb_delete_breakpoint": {
                "description": "Delete a breakpoint by number",
                "inputSchema": {
                    "type": "object",
                    "properties": {"number": {"type": "integer", "description": "Breakpoint number"}},
                    "required": ["number"],
                },
            },
            "gdb_continue": {
                "description": "Continue execution of the debugged process",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            "gdb_interrupt": {
                "description": "Interrupt (pause) execution of the debugged process",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            "gdb_read_register": {
                "description": "Read a CPU register value",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "register": {
                            "type": "string",
                            "description": "Register name (e.g., rax, rbx, rip, xmm0)",
                        }
                    },
                    "required": ["register"],
                },
            },
            "gdb_read_memory": {
                "description": "Read memory at an address",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "Memory address (e.g., 0x7f1234567890)"},
                        "size": {"type": "integer", "description": "Number of bytes to read (max 4096)"},
                    },
                    "required": ["address", "size"],
                },
            },
            "gdb_get_hits": {
                "description": "Get pending breakpoint hit events",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            "gdb_status": {
                "description": "Get current GDB session status",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            "gdb_command": {
                "description": "Execute a raw GDB/MI command",
                "inputSchema": {
                    "type": "object",
                    "properties": {"command": {"type": "string", "description": "GDB/MI command to execute"}},
                    "required": ["command"],
                },
            },
        }

    def handle_request(self, request: dict) -> dict:
        """Handle an incoming JSON-RPC request."""
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")

        if method == "initialize":
            return self._initialize(req_id, params)
        elif method == "tools/list":
            return self._list_tools(req_id)
        elif method == "tools/call":
            return self._call_tool(req_id, params)
        elif method == "notifications/initialized":
            # Client notification, no response needed
            return None
        else:
            return self._error(req_id, -32601, f"Method not found: {method}")

    def _initialize(self, req_id: Any, params: dict) -> dict:
        """Handle initialize request."""
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "gdb-mcp-server", "version": "1.0.0"},
            },
        }

    def _list_tools(self, req_id: Any) -> dict:
        """Handle tools/list request."""
        tools_list = [
            {"name": name, "description": info["description"], "inputSchema": info["inputSchema"]}
            for name, info in self.tools.items()
        ]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools_list}}

    def _call_tool(self, req_id: Any, params: dict) -> dict:
        """Handle tools/call request."""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name not in self.tools:
            return self._error(req_id, -32602, f"Unknown tool: {tool_name}")

        try:
            result = self._execute_tool(tool_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]},
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}], "isError": True},
            }

    def _execute_tool(self, name: str, args: dict) -> dict:
        """Execute a tool and return the result."""
        if name == "gdb_start":
            return self.gdb.start()
        elif name == "gdb_stop":
            return self.gdb.stop()
        elif name == "gdb_attach":
            return self.gdb.attach(args["pid"])
        elif name == "gdb_detach":
            return self.gdb.detach()
        elif name == "gdb_breakpoint":
            return self.gdb.set_breakpoint(args["address"], args.get("hardware", False))
        elif name == "gdb_delete_breakpoint":
            return self.gdb.delete_breakpoint(args["number"])
        elif name == "gdb_continue":
            return self.gdb.continue_exec()
        elif name == "gdb_interrupt":
            return self.gdb.interrupt()
        elif name == "gdb_read_register":
            return self.gdb.read_register(args["register"])
        elif name == "gdb_read_memory":
            return self.gdb.read_memory(args["address"], args["size"])
        elif name == "gdb_get_hits":
            return self.gdb.get_hits()
        elif name == "gdb_status":
            return self.gdb.get_status()
        elif name == "gdb_command":
            return self.gdb.raw_command(args["command"])
        else:
            return {"error": f"Tool not implemented: {name}"}

    def _error(self, req_id: Any, code: int, message: str) -> dict:
        """Create an error response."""
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def run(self):
        """Main server loop - read from stdin, write to stdout."""
        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break

                line = line.strip()
                if not line:
                    continue

                request = json.loads(line)
                response = self.handle_request(request)

                if response:  # Notifications don't get responses
                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()

            except json.JSONDecodeError as e:
                error_response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {e}"},
                }
                sys.stdout.write(json.dumps(error_response) + "\n")
                sys.stdout.flush()
            except Exception as e:
                error_response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": f"Internal error: {e}"},
                }
                sys.stdout.write(json.dumps(error_response) + "\n")
                sys.stdout.flush()


def main():
    server = MCPServer()
    server.run()


if __name__ == "__main__":
    main()
