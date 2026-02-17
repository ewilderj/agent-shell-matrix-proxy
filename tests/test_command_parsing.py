#!/usr/bin/env python3
"""
Test command parsing (!return, !close, etc.) without Matrix.

Tests:
- Command recognition in messages
- Brief command formats (mobile-friendly)
- Edge cases (typos, partial matches)

Usage:
    python3 tests/test_command_parsing.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class CommandParser:
    """Parse room messages for handoff commands."""
    
    COMMANDS = {
        "!return": {
            "name": "return",
            "action": "handoff_end",
            "description": "Hand session back to Emacs"
        },
        "!close": {
            "name": "close",
            "action": "close_session",
            "description": "Archive and close session"
        },
        "!status": {
            "name": "status",
            "action": "session_status",
            "description": "Show current session status"
        },
        "!help": {
            "name": "help",
            "action": "help",
            "description": "Show available commands"
        }
    }
    
    @classmethod
    def parse(cls, message: str) -> dict:
        """
        Parse message for commands.
        Returns: {
            "is_command": bool,
            "command": str (e.g., "!return"),
            "action": str (e.g., "handoff_end"),
            "args": [str],
            "raw": str
        }
        """
        message = message.strip()
        
        # Check if starts with !
        if not message.startswith("!"):
            return {"is_command": False, "raw": message}
        
        # Split command and args
        parts = message.split(None, 1)
        command = parts[0]
        args = parts[1].split() if len(parts) > 1 else []
        
        if command in cls.COMMANDS:
            return {
                "is_command": True,
                "command": command,
                "action": cls.COMMANDS[command]["action"],
                "args": args,
                "raw": message
            }
        
        # Unknown command
        return {
            "is_command": True,
            "command": command,
            "action": None,
            "args": args,
            "raw": message,
            "error": f"Unknown command: {command}"
        }


def test_command_parsing():
    """Test command parser."""
    test_cases = [
        ("Hello world", False, None),
        ("!return", True, "handoff_end"),
        ("!return ", True, "handoff_end"),  # Trailing space
        ("  !return  ", True, "handoff_end"),  # Leading/trailing spaces
        ("!close", True, "close_session"),
        ("!status", True, "session_status"),
        ("!help", True, "help"),
        ("!unknown", True, None),  # Unknown command (error)
        ("! return", True, None),  # Space after !
        ("return", False, None),  # No ! prefix
        ("!return extra args", True, "handoff_end"),  # With args
        ("!return   extra   args", True, "handoff_end"),  # Multiple spaces
    ]
    
    print("\n=== Command Parsing Tests ===\n")
    
    passed = 0
    failed = 0
    
    for message, should_be_command, expected_action in test_cases:
        result = CommandParser.parse(message)
        is_command = result.get("is_command", False)
        action = result.get("action")
        
        success = is_command == should_be_command and action == expected_action
        
        status = "✓" if success else "✗"
        passed += 1 if success else 0
        failed += 0 if success else 1
        
        print(f"{status} Message: {message!r}")
        print(f"  Is command: {is_command}, Action: {action}")
        if result.get("args"):
            print(f"  Args: {result['args']}")
        if result.get("error"):
            print(f"  Error: {result['error']}")
        print()
    
    print(f"=== Results: {passed} passed, {failed} failed ===\n")
    return failed == 0


if __name__ == "__main__":
    success = test_command_parsing()
    sys.exit(0 if success else 1)
