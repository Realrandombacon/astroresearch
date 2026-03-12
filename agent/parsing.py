"""
Response parsing — extract THOUGHT and TOOL lines from Qwen's output,
and deduplicate repeated blocks.
"""

import re
import json


def deduplicate_response(response):
    """Detect and remove repeated blocks from Qwen output.

    When Qwen gets 'excited' about a finding, it sometimes repeats its
    THOUGHT block many times, consuming all num_predict tokens without
    ever emitting TOOL: lines. This detects that pattern and truncates.

    Also detects repeated non-THOUGHT lines (Qwen sometimes repeats
    entire paragraphs without the THOUGHT: prefix).
    """
    lines = response.split("\n")

    # Count all non-empty line occurrences (not just THOUGHT: lines)
    line_counts = {}
    for line in lines:
        stripped = line.strip()
        if len(stripped) > 20:  # Only count substantial lines
            line_counts[stripped] = line_counts.get(stripped, 0) + 1

    # If any substantial line appears 3+ times, we have a repetition loop
    max_repeats = max(line_counts.values()) if line_counts else 0
    if max_repeats < 3:
        return response, False  # No repetition detected

    # Deduplicate: keep first occurrence of each line, always keep TOOL lines
    seen_lines = set()
    deduped_lines = []
    repeated = False

    for line in lines:
        stripped = line.strip()
        # Always keep TOOL: lines (never deduplicate actions)
        if stripped.upper().startswith("TOOL:"):
            deduped_lines.append(line)
            continue
        # Keep short lines (empty, separators, etc.)
        if len(stripped) <= 20:
            deduped_lines.append(line)
            continue
        # Deduplicate substantial repeated lines
        if stripped in seen_lines:
            repeated = True
            continue  # Skip duplicate
        seen_lines.add(stripped)
        deduped_lines.append(line)

    deduped = "\n".join(deduped_lines)
    return deduped, repeated


def parse_tool_calls(response):
    """Parse Qwen's response for THOUGHT and TOOL lines.

    Handles multiple formats Qwen uses:
      TOOL: tool_name(param1=value1, param2=value2)    # standard
      TOOL: tool_name param1=value1 param2=value2       # no parens
      TOOL: tool_name RA=83.63 Dec=22.01                # coordinate shorthand
      TOOL: tool_name({"param": "value"})               # JSON-like
      TOOL_ARGS: {"param": "value"}                     # separate args line
    """
    thoughts = []
    tool_calls = []
    pending_tool_name = None  # for TOOL_ARGS on next line

    for line in response.split("\n"):
        line = line.strip()

        if line.upper().startswith("THOUGHT:"):
            thoughts.append(line[8:].strip())
            continue

        # Handle TOOL_ARGS: on a separate line
        if line.upper().startswith("TOOL_ARGS:") and pending_tool_name:
            args_str = line[10:].strip()
            try:
                params = json.loads(args_str)
                # Normalize keys to lowercase
                params = {k.lower(): v for k, v in params.items()}
                tool_calls.append({"tool": pending_tool_name, "params": params})
            except (json.JSONDecodeError, Exception):
                pass
            pending_tool_name = None
            continue

        if not line.upper().startswith("TOOL:"):
            pending_tool_name = None
            continue

        call_str = line[5:].strip()

        # Format 1: tool_name(param1=val1, param2=val2) — standard
        match = re.match(r"(\w+)\((.*)\)", call_str)
        if match:
            tool_name = match.group(1)
            params_str = match.group(2)

            # Try JSON parse first (handles {"key": "val"} format)
            params = {}
            if params_str.strip().startswith("{"):
                try:
                    params = json.loads(params_str)
                    params = {k.lower(): v for k, v in params.items()}
                except (json.JSONDecodeError, Exception):
                    pass

            if not params:
                # Parse key=value pairs
                for param_match in re.finditer(r"(\w+)\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|([\'\"]?[^,\)]+))", params_str):
                    key = param_match.group(1).lower()
                    val = param_match.group(2) or param_match.group(3) or param_match.group(4)
                    if val:
                        val = val.strip().strip("\'\"\'")
                    try:
                        val = float(val)
                        if val == int(val):
                            val = int(val)
                    except (ValueError, TypeError):
                        pass
                    params[key] = val

            tool_calls.append({"tool": tool_name, "params": params})
            continue

        # Format 2: tool_name key=val key=val (no parentheses)
        parts = call_str.split()
        if len(parts) >= 1 and re.match(r"^\w+$", parts[0]):
            tool_name = parts[0]

            if len(parts) == 1:
                # Just a tool name with no args — might have TOOL_ARGS on next line
                pending_tool_name = tool_name
                # Also accept it as a no-arg call for tools like list_images
                tool_calls.append({"tool": tool_name, "params": {}})
                continue

            # Parse remaining as key=value pairs or "RA=val Dec=val" shorthand
            params = {}
            rest = " ".join(parts[1:])

            # Handle comma-separated or space-separated key=value
            for m in re.finditer(r"(\w+)\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|(\S+))", rest):
                key = m.group(1).lower()
                val = m.group(2) or m.group(3) or m.group(4)
                if val:
                    val = val.strip(",").strip()
                try:
                    val = float(val)
                    if val == int(val):
                        val = int(val)
                except (ValueError, TypeError):
                    pass
                params[key] = val

            if params:
                # Convert RA/Dec shorthand to search_region if needed
                if tool_name == "search_target" and "ra" in params and "dec" in params and "name" not in params:
                    # Qwen is using search_target with coords — redirect to search_region
                    tool_name = "search_region"
                    if "radius" not in params:
                        params["radius"] = 0.05  # default

                # Remove the bare tool_name call we might have added
                if tool_calls and tool_calls[-1]["tool"] == parts[0] and not tool_calls[-1]["params"]:
                    tool_calls.pop()

                tool_calls.append({"tool": tool_name, "params": params})

    # Post-process: fix search_target called with coordinate-like names
    for call in tool_calls:
        if call["tool"] == "search_target":
            name = call["params"].get("name", "")
            if isinstance(name, str) and re.match(r"RA\s*=\s*[\d.]+", name):
                # Extract RA and Dec from the name string
                ra_match = re.search(r"RA\s*=?\s*([\d.]+)", name)
                dec_match = re.search(r"Dec\s*=?\s*([-\d.]+)", name)
                if ra_match and dec_match:
                    call["tool"] = "search_region"
                    call["params"] = {
                        "ra": float(ra_match.group(1)),
                        "dec": float(dec_match.group(1)),
                        "radius": 0.05,
                    }

    return {
        "thoughts": thoughts,
        "tool_calls": tool_calls,
    }
