#!/usr/bin/env python3
"""
Test suite for GDB MCP Server.
"""

import json
import subprocess
import sys
import time
import unittest
import os


class TestMCPProtocol(unittest.TestCase):
    """Test MCP protocol compliance."""

    def setUp(self):
        self.server = subprocess.Popen(
            [sys.executable, "/root/gdb/gdb_mcp_server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def tearDown(self):
        self.server.terminate()
        self.server.wait()

    def send_request(self, method: str, params: dict = None, req_id: int = 1) -> dict:
        """Send a JSON-RPC request and get response."""
        request = {"jsonrpc": "2.0", "method": method, "id": req_id}
        if params:
            request["params"] = params

        self.server.stdin.write(json.dumps(request).encode() + b"\n")
        self.server.stdin.flush()

        response_line = self.server.stdout.readline()
        return json.loads(response_line)

    def test_initialize(self):
        """Test initialize handshake."""
        response = self.send_request(
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}},
        )

        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertIn("result", response)
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("capabilities", response["result"])
        self.assertIn("serverInfo", response["result"])

    def test_tools_list(self):
        """Test tools/list returns all expected tools."""
        response = self.send_request("tools/list")

        self.assertIn("result", response)
        tools = response["result"]["tools"]
        tool_names = {t["name"] for t in tools}

        expected_tools = {
            "gdb_start",
            "gdb_stop",
            "gdb_attach",
            "gdb_detach",
            "gdb_breakpoint",
            "gdb_delete_breakpoint",
            "gdb_continue",
            "gdb_interrupt",
            "gdb_read_register",
            "gdb_read_memory",
            "gdb_get_hits",
            "gdb_status",
            "gdb_command",
        }

        self.assertEqual(tool_names, expected_tools)

    def test_unknown_method(self):
        """Test error handling for unknown method."""
        response = self.send_request("unknown/method")

        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32601)


class TestGDBOperations(unittest.TestCase):
    """Test GDB operations through MCP."""

    def setUp(self):
        self.server = subprocess.Popen(
            [sys.executable, "/root/gdb/gdb_mcp_server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def tearDown(self):
        # Stop GDB if running
        self.call_tool("gdb_stop")
        self.server.terminate()
        self.server.wait()

    def call_tool(self, name: str, arguments: dict = None) -> dict:
        """Call a tool and return parsed result."""
        request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 1,
            "params": {"name": name, "arguments": arguments or {}},
        }

        self.server.stdin.write(json.dumps(request).encode() + b"\n")
        self.server.stdin.flush()

        response_line = self.server.stdout.readline()
        response = json.loads(response_line)

        if "result" in response and "content" in response["result"]:
            return json.loads(response["result"]["content"][0]["text"])
        return response

    def test_gdb_start_stop(self):
        """Test starting and stopping GDB."""
        # Start
        result = self.call_tool("gdb_start")
        self.assertIn("status", result)
        self.assertEqual(result["status"], "started")
        self.assertIn("pid", result)

        # Check status
        status = self.call_tool("gdb_status")
        self.assertTrue(status["running"])

        # Stop
        result = self.call_tool("gdb_stop")
        self.assertEqual(result["status"], "stopped")

        # Check status after stop
        status = self.call_tool("gdb_status")
        self.assertFalse(status["running"])

    def test_gdb_not_started_error(self):
        """Test error when GDB not started."""
        result = self.call_tool("gdb_attach", {"pid": 1})
        self.assertIn("error", result)
        self.assertIn("not started", result["error"].lower())

    def test_gdb_status_initial(self):
        """Test initial status."""
        status = self.call_tool("gdb_status")
        self.assertFalse(status["running"])
        self.assertIsNone(status["attached_pid"])
        self.assertEqual(status["breakpoints"], [])
        self.assertEqual(status["pending_hits"], 0)


class TestGDBAttachDebug(unittest.TestCase):
    """Test GDB attach and debugging with a real process."""

    def setUp(self):
        # Start a simple target process
        self.target = subprocess.Popen(
            ["sleep", "300"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.1)  # Let it start

        self.server = subprocess.Popen(
            [sys.executable, "/root/gdb/gdb_mcp_server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def tearDown(self):
        self.call_tool("gdb_stop")
        self.server.terminate()
        self.server.wait()
        self.target.terminate()
        self.target.wait()

    def call_tool(self, name: str, arguments: dict = None) -> dict:
        """Call a tool and return parsed result."""
        request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 1,
            "params": {"name": name, "arguments": arguments or {}},
        }

        self.server.stdin.write(json.dumps(request).encode() + b"\n")
        self.server.stdin.flush()

        response_line = self.server.stdout.readline()
        response = json.loads(response_line)

        if "result" in response and "content" in response["result"]:
            return json.loads(response["result"]["content"][0]["text"])
        return response

    def test_attach_detach(self):
        """Test attaching to and detaching from a process."""
        # Start GDB
        self.call_tool("gdb_start")

        # Attach
        result = self.call_tool("gdb_attach", {"pid": self.target.pid})
        self.assertEqual(result.get("status"), "attached")
        self.assertEqual(result.get("pid"), self.target.pid)

        # Check status
        status = self.call_tool("gdb_status")
        self.assertEqual(status["attached_pid"], self.target.pid)

        # Detach
        result = self.call_tool("gdb_detach")
        self.assertEqual(result.get("status"), "detached")

        # Check status after detach
        status = self.call_tool("gdb_status")
        self.assertIsNone(status["attached_pid"])

    def test_read_register(self):
        """Test reading CPU registers."""
        self.call_tool("gdb_start")
        self.call_tool("gdb_attach", {"pid": self.target.pid})

        # Read instruction pointer
        result = self.call_tool("gdb_read_register", {"register": "rip"})
        self.assertEqual(result.get("register"), "rip")
        self.assertIn("value", result)
        # Value should be a hex address
        self.assertTrue(result["value"].startswith("0x") or result["value"].isdigit())

    def test_read_memory(self):
        """Test reading memory."""
        self.call_tool("gdb_start")
        self.call_tool("gdb_attach", {"pid": self.target.pid})

        # Get current instruction pointer
        reg_result = self.call_tool("gdb_read_register", {"register": "rip"})
        rip = reg_result.get("value", "0")

        # Read memory at RIP
        result = self.call_tool("gdb_read_memory", {"address": rip, "size": 16})
        self.assertEqual(result.get("address"), rip)
        self.assertEqual(result.get("size"), 16)
        # Contents should be hex string
        self.assertIn("contents", result)

    def test_memory_size_limit(self):
        """Test memory read size limit."""
        self.call_tool("gdb_start")

        # Try to read more than 4096 bytes
        result = self.call_tool("gdb_read_memory", {"address": "0x0", "size": 8192})
        self.assertIn("error", result)
        self.assertIn("4096", result["error"])


class TestRawCommand(unittest.TestCase):
    """Test raw GDB command execution."""

    def setUp(self):
        self.server = subprocess.Popen(
            [sys.executable, "/root/gdb/gdb_mcp_server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def tearDown(self):
        self.call_tool("gdb_stop")
        self.server.terminate()
        self.server.wait()

    def call_tool(self, name: str, arguments: dict = None) -> dict:
        request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 1,
            "params": {"name": name, "arguments": arguments or {}},
        }
        self.server.stdin.write(json.dumps(request).encode() + b"\n")
        self.server.stdin.flush()
        response_line = self.server.stdout.readline()
        response = json.loads(response_line)
        if "result" in response and "content" in response["result"]:
            return json.loads(response["result"]["content"][0]["text"])
        return response

    def test_raw_command(self):
        """Test executing raw GDB commands."""
        self.call_tool("gdb_start")

        # Execute version command
        result = self.call_tool("gdb_command", {"command": "-gdb-version"})
        self.assertIn("lines", result)
        self.assertIsInstance(result["lines"], list)


if __name__ == "__main__":
    # Check if running as root (required for ptrace)
    if os.geteuid() != 0:
        print("Warning: Some tests require root privileges for ptrace")

    unittest.main(verbosity=2)
