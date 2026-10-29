def chunk_text(text: str, max_lines: int = 4) -> list[dict]:
    if max_lines < 1:
        raise ValueError("max_lines must be positive")
    lines = text.splitlines()
    chunks = []
    for start in range(0, len(lines) - len(lines) % max_lines, max_lines):
        group = lines[start:start + max_lines]
        if any(line.strip() for line in group):
            chunks.append({"start_line": start, "end_line": start + len(group) - 1,
                           "text": "\n".join(group)})
    return chunks
