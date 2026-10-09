"""Preview-only prose coloring and source coordinates; never modify review patches."""
from __future__ import annotations

import re

GREEN = "0.08,0.48,0.20"
STRUCTURAL = re.compile(r"\\(?:begin|end|documentclass|usepackage|RequirePackage|input|include|includeonly|"
                        r"(?:re)?newcommand|providecommand|Declare\w+|(?:re)?newenvironment|"
                        r"(?:g|e|x)?def|let|global|begingroup|endgroup|setcounter|addtocounter|item|verb|lstinline)\b")
VERBATIM = re.compile(r"\\begin\{(?:verbatim\*?|Verbatim|lstlisting|minted|comment)\}")


def uncomment(text: str) -> str:
    """Remove comments without treating an escaped percent as a comment."""
    lines = []
    for line in text.split("\n"):
        slashes = 0
        for index, char in enumerate(line):
            if char == "%" and slashes % 2 == 0:
                line = line[:index]
                break
            slashes = slashes + 1 if char == "\\" else 0
        lines.append(line)
    return "\n".join(lines)


def conditional_text_context(prefix: str) -> bool:
    """Recognize open text branches, without treating arbitrary macro arguments as prose.

    The first argument of ifthen/ifthenelse is a test. Only the second and
    third arguments may contain locally grouped color commands. Track those
    argument boundaries through nested groups and nested conditionals.
    """
    groups = []
    argument = None
    for token in re.findall(r"\\[a-zA-Z@]+\*?|\\.|[{}]|[^\\{}]+|\\$", prefix):
        if token in {r"\ifthen", r"\ifthenelse"}:
            argument = 1
        elif token == "{":
            groups.append(argument)
            argument = None
        elif token == "}":
            if not groups:
                return False
            finished = groups.pop()
            argument = finished + 1 if finished in {1, 2} else None
        elif token.strip():
            # A mandatory argument must immediately follow its command or
            # preceding argument (apart from spaces/comments).
            argument = None
    return bool(groups) and all(group in {2, 3} for group in groups) and argument is None


def safe_prose(text: str, prefix: str) -> bool:
    clean = uncomment(text)
    before = uncomment(prefix)
    if not clean.strip() or STRUCTURAL.search(clean) or VERBATIM.search(before):
        return False
    # Wrapping an incomplete group or math expression can change its meaning.
    def balance(value):
        braces, dollars = 0, None
        for token in re.findall(r"\\.|\$\$|[{}$]", value):
            if token == "{":
                braces += 1
            elif token == "}":
                braces -= 1
                if braces < 0:
                    return None
            elif token in {"$", "$$"}:
                dollars = None if dollars == token else token if dollars is None else "unbalanced"
        return braces, dollars
    prefix_balance = balance(before)
    if balance(clean) != (0, None) or prefix_balance is None or prefix_balance[1] is not None:
        return False
    if prefix_balance[0] and not conditional_text_context(before):
        return False
    if clean.count(r"\(") != clean.count(r"\)") or clean.count(r"\[") != clean.count(r"\]"):
        return False
    if before.count(r"\(") != before.count(r"\)") or before.count(r"\[") != before.count(r"\]"):
        return False
    for environment in ("math", "displaymath", "equation", "equation*", "align", "align*", "gather", "gather*",
                        "multline", "multline*", "array", "tabular", "tabularx", "tikzpicture"):
        if before.count("\\begin{" + environment + "}") != before.count("\\end{" + environment + "}"):
            return False
    # Preamble changes and non-text control lines should compile normally.
    if (r"\documentclass" in before and r"\begin{document}" not in before) or r"\end{document}" in before:
        return False
    if not re.match(r"(?:[A-Za-z]|\\(?:textbf|textit|emph|underline|textrm|textsf|texttt|LaTeX|TeX|"
                    r"ce|SI|qty|num|ref|eqref|cite\w*|autoref|parencite|textcite|footnote)\b)", clean.lstrip()):
        return False
    return bool(re.search(r"[A-Za-z]", clean))


def color_prose(text: str) -> str:
    """Place the closing group before any trailing comment, preserving line numbers."""
    lines = text.splitlines(keepends=True)
    first = next(i for i, line in enumerate(lines) if uncomment(line).strip())
    last = max(i for i, line in enumerate(lines) if uncomment(line).strip())
    lines[first] = "{\\color[rgb]{" + GREEN + "}\\relax " + lines[first]
    final = lines[last]
    at = min(len(uncomment(final).rstrip("\r\n")), len(final.rstrip("\r\n")))
    lines[last] = final[:at] + "}" + final[at:]
    return "".join(lines)


def paragraph_at(text: str, line: int) -> tuple[int, int, int]:
    """Source paragraph number and bounds, defined by blank lines in this file."""
    lines = text.splitlines()
    index = min(max(line - 1, 0), max(len(lines) - 1, 0))
    start = index
    while start > 0 and lines[start - 1].strip():
        start -= 1
    end = index
    while end + 1 < len(lines) and lines[end + 1].strip():
        end += 1
    number = 0
    in_paragraph = False
    for value in lines[:index + 1]:
        content = uncomment(value).strip()
        if not content:
            in_paragraph = False
        elif not in_paragraph:
            number += 1
            in_paragraph = True
    return max(1, number), start + 1, end + 1


def review_annotations(proposal: dict, selection: str) -> tuple[dict[str, str], list[dict]]:
    """Render selected edits with conservative green groups around pending prose."""
    outputs, changes = {}, []
    for file in proposal["files"]:
        base = file["before"].splitlines(keepends=True)
        rendered, cursor, line = [], 0, 1
        file_changes = []
        for hunk in file["hunks"]:
            context = "".join(base[cursor:hunk["start"]])
            rendered.append(context)
            line += len(base[cursor:hunk["start"]])
            included = hunk["decision"] == "accepted" or selection == "proposed" and hunk["decision"] == "pending"
            text = hunk["new"] if included else hunk["old"]
            pending = included and hunk["decision"] == "pending"
            prefix = "".join(base[:hunk["start"]])
            marked = pending and file["path"].endswith(".tex") and safe_prose(text, prefix)
            nonblank = next((i for i, value in enumerate(text.splitlines()) if uncomment(value).strip()), 0)
            change = {"id": hunk["id"], "path": file["path"], "decision": hunk["decision"],
                      "line": line + nonblank, "end_line": line + max(len(text.splitlines()) - 1, 0),
                      "highlighted": bool(marked), "deleted": included and not text.strip(), "page": None}
            if pending and text.strip() and not marked:
                change["note"] = "This source construct is not colored to preserve LaTeX behavior."
            file_changes.append(change)
            rendered.append(color_prose(text) if marked else text)
            line += len(text.splitlines(keepends=True))
            cursor = hunk["end"]
        rendered.append("".join(base[cursor:]))
        outputs[file["path"]] = "".join(rendered)
        # Paragraph numbers refer to the unmarked selected source, not TeX groups.
        clean = []
        cursor = 0
        for hunk in file["hunks"]:
            clean.extend(base[cursor:hunk["start"]])
            included = hunk["decision"] == "accepted" or selection == "proposed" and hunk["decision"] == "pending"
            clean.append(hunk["new"] if included else hunk["old"])
            cursor = hunk["end"]
        clean.extend(base[cursor:])
        for change in file_changes:
            change["paragraph"], change["paragraph_start"], change["paragraph_end"] = paragraph_at("".join(clean), change["line"])
        changes.extend(file_changes)
    return outputs, changes


def sync_records(output: str) -> list[dict]:
    """Parse SyncTeX's documented key:value records without executing editor commands."""
    records, current = [], {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if not separator or key in {"SyncTeX result begin", "SyncTeX result end"}:
            continue
        if key in {"Page", "Input"} and key in current:
            records.append(current)
            current = {}
        current[key] = value.strip()
    if current:
        records.append(current)
    return records
