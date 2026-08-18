"""Skill metadata parsing - extracts info from SKILL.md frontmatter."""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Regex pattern to match YAML frontmatter between --- delimiters
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Regex patterns for parsing simple key-value pairs from YAML-like frontmatter
KEY_VALUE_PATTERN = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$", re.MULTILINE)
LIST_PATTERN = re.compile(r"^\s+-\s+(.+)$", re.MULTILINE)

# Matches a `|`/`>` block-scalar indicator, e.g. `>-`, `|`, `|+` -- the
# real value lives on the indented lines that follow, not this line.
# Deliberately does NOT match an explicit indentation digit (`>2`, `|1-`):
# that's rare in hand-written SKILL.md files, and a half-implementation
# risks silently mis-indenting content. Unmatched indicators fall through
# to the plain-scalar path unchanged (pre-existing, known-limited
# behavior) rather than being "recognized but wrong".
BLOCK_SCALAR_PATTERN = re.compile(r"^[|>][+-]?$")


@dataclass
class SkillMetadata:
    """Parsed skill metadata from SKILL.md frontmatter."""

    name: str
    description: str
    path: Path
    version: Optional[str] = None
    author: Optional[str] = None
    tags: List[str] = field(default_factory=list)


def _unquote(value: str) -> str:
    """Remove quotes from a YAML string value if present."""
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def _consume_block_scalar(
    lines: List[str], start: int, indicator: str
) -> Tuple[str, int]:
    """Read a `key: |`/`key: >` block's indented lines starting at `start`.

    Returns (folded/joined value, index of the first unconsumed line).
    Covers the common cases, not the full YAML block-scalar spec: no
    explicit indentation digit (see BLOCK_SCALAR_PATTERN), and lines
    indented deeper than the block's base indent inside a folded (`>`)
    scalar are folded like everything else rather than kept literal.
    """
    style = indicator[0]  # '|' (literal) or '>' (folded)
    chomp = "-" if "-" in indicator else "+" if "+" in indicator else ""

    block_lines: List[str] = []
    indent: Optional[int] = None
    idx = start
    while idx < len(lines):
        raw_line = lines[idx]
        if raw_line.strip() == "":
            block_lines.append("")
            idx += 1
            continue
        # strip() (not lstrip(" ")) so tab-indented lines are measured
        # correctly instead of registering as indent 0 and truncating
        # the whole block.
        line_indent = len(raw_line) - len(raw_line.lstrip())
        if indent is None:
            if line_indent == 0:
                break  # No indented content -- empty block scalar.
            indent = line_indent
        if line_indent < indent:
            break
        block_lines.append(raw_line[indent:])
        idx += 1

    # Count trailing blanks separately from the content so "keep"
    # chomping can restore exactly that many trailing newlines instead of
    # always collapsing to zero or one.
    trailing_blanks = 0
    while block_lines and block_lines[-1] == "":
        block_lines.pop()
        trailing_blanks += 1

    if style == ">":
        # Fold single newlines to spaces; blank lines mark a paragraph break.
        folded_parts: List[str] = []
        for text_line in block_lines:
            if text_line == "":
                folded_parts.append("\n")
            elif folded_parts and folded_parts[-1] not in ("", "\n"):
                folded_parts[-1] = folded_parts[-1] + " " + text_line
            else:
                folded_parts.append(text_line)
        value = "".join(folded_parts)
    else:
        value = "\n".join(block_lines)

    if not value:
        return ("\n" * trailing_blanks if chomp == "+" else ""), idx
    if chomp == "+":
        return value + "\n" * (trailing_blanks + 1), idx
    if chomp == "-":
        return value, idx
    return value + "\n", idx  # clip (YAML default)


def parse_yaml_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from SKILL.md content.

    Frontmatter is between --- delimiters at the start of file.
    Uses simple regex parsing to avoid a heavy yaml dependency. Supports
    plain `key: value` pairs, `key:` + `- item` lists, and block scalars
    (`key: |`, `key: >-`, etc.).

    Returns:
        Dictionary of parsed frontmatter key-value pairs, or {} if no
        frontmatter is found.
    """
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        logger.debug("No frontmatter found in content")
        return {}

    lines = match.group(1).split("\n")
    result: dict = {}
    current_key: Optional[str] = None
    current_list: List[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip empty lines and comments
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # Check if this is a list item
        list_match = LIST_PATTERN.match(line)
        if list_match and current_key:
            current_list.append(_unquote(list_match.group(1)))
            i += 1
            continue

        # Check if this is a key-value pair
        kv_match = KEY_VALUE_PATTERN.match(line)
        if kv_match:
            # Save any accumulated list items from previous key
            if current_key and current_list:
                result[current_key] = current_list
                current_list = []

            key, value = kv_match.groups()
            key = key.strip()
            value = value.strip()

            if BLOCK_SCALAR_PATTERN.match(value):
                block_value, i = _consume_block_scalar(lines, i + 1, value)
                result[key] = block_value
                current_key = None
                continue

            # If value is empty, this might be a list start
            if not value:
                current_key = key
                result[key] = []  # Initialize as empty list
            else:
                result[key] = _unquote(value)
                current_key = None

        i += 1

    # Handle case where list items were at the end
    if current_key and current_list:
        result[current_key] = current_list

    return result


def parse_skill_metadata(skill_path: Path) -> Optional[SkillMetadata]:
    """Parse metadata from a skill's SKILL.md file.

    Args:
        skill_path: Path to the skill directory (not the SKILL.md file)

    Returns:
        SkillMetadata if successful, None if parsing fails.
    """
    if not skill_path.exists():
        logger.debug(f"Skill path does not exist: {skill_path}")
        return None

    skill_md_path = skill_path / "SKILL.md"
    if not skill_md_path.exists():
        logger.debug(f"SKILL.md not found in skill directory: {skill_path}")
        return None

    try:
        content = skill_md_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to read SKILL.md at {skill_md_path}: {e}")
        return None

    frontmatter = parse_yaml_frontmatter(content)
    if not frontmatter:
        logger.debug(f"No valid frontmatter found in {skill_md_path}")
        return None

    # Required fields
    name = frontmatter.get("name")
    if not name:
        logger.debug(
            f"'name' is required in frontmatter but not found in {skill_md_path}"
        )
        return None

    description = frontmatter.get("description")
    if not description:
        logger.debug(
            f"'description' is required in frontmatter but not found in {skill_md_path}"
        )
        return None

    # Handle tags - could be a list or a comma-separated string
    tags: List[str] = []
    raw_tags = frontmatter.get("tags", [])
    if isinstance(raw_tags, list):
        tags = raw_tags
    elif isinstance(raw_tags, str):
        tags = [tag.strip() for tag in raw_tags.split(",") if tag.strip()]

    return SkillMetadata(
        name=name,
        description=description,
        path=skill_path,
        version=frontmatter.get("version"),
        author=frontmatter.get("author"),
        tags=tags,
    )


def load_full_skill_content(skill_path: Path) -> Optional[str]:
    """Load the complete SKILL.md content for activation.

    Args:
        skill_path: Path to the skill directory

    Returns:
        Full file content as string, or None if not found.
    """
    if not skill_path.exists():
        logger.debug(f"Skill path does not exist: {skill_path}")
        return None

    skill_md_path = skill_path / "SKILL.md"
    if not skill_md_path.exists():
        logger.debug(f"SKILL.md not found in skill directory: {skill_path}")
        return None

    try:
        return skill_md_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to read SKILL.md at {skill_md_path}: {e}")
        return None


def get_skill_resources(skill_path: Path) -> List[Path]:
    """List all resource files bundled with a skill.

    Returns paths to all non-SKILL.md files in the skill directory.

    Args:
        skill_path: Path to the skill directory

    Returns:
        List of paths to resource files (excluding SKILL.md).
    """
    if not skill_path.exists():
        logger.debug(f"Skill path does not exist: {skill_path}")
        return []

    if not skill_path.is_dir():
        logger.warning(f"Skill path is not a directory: {skill_path}")
        return []

    resources: List[Path] = []
    try:
        for item in skill_path.iterdir():
            if item.is_file() and item.name != "SKILL.md":
                resources.append(item)
    except Exception as e:
        logger.error(f"Failed to list resources in {skill_path}: {e}")
        return []

    return sorted(resources)  # Sort for consistent ordering
