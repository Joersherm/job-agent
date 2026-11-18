"""Render a tailored text resume as a PDF in the style of Jake's Resume (Overleaf, MIT licence).

The tailored text has a header, then SUMMARY / TECHNICAL SKILLS / EXPERIENCE / PROJECTS / EDUCATION
sections. The output follows the author's own layout: Education, Experience, Projects, Technical
Skills, with no summary. Locations missing from the tailored text are filled in from the base resume.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from applypilot.config import RESUME_PATH

log = logging.getLogger(__name__)

PREAMBLE = r"""\documentclass[letterpaper,%(pt)spt]{article}
\usepackage{latexsym}
\usepackage[empty]{fullpage}
\usepackage{titlesec}
\usepackage{marvosym}
\usepackage[usenames,dvipsnames]{color}
\usepackage{verbatim}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\usepackage{fancyhdr}
\usepackage[english]{babel}
\usepackage{tabularx}
\pagestyle{fancy}
\fancyhf{}
\fancyfoot{}
\renewcommand{\headrulewidth}{0pt}
\renewcommand{\footrulewidth}{0pt}
\addtolength{\oddsidemargin}{-0.5in}
\addtolength{\evensidemargin}{-0.5in}
\addtolength{\textwidth}{1in}
\addtolength{\topmargin}{-.5in}
\addtolength{\textheight}{1.0in}
\urlstyle{same}
\raggedbottom
\raggedright
\setlength{\tabcolsep}{0in}
\titleformat{\section}{\vspace{-4pt}\scshape\raggedright\large}{}{0em}{}[\color{black}\titlerule \vspace{-5pt}]
\pdfgentounicode=1
\newcommand{\resumeItem}[1]{\item\small{#1 \vspace{-2pt}}}
\newcommand{\resumeSubheading}[4]{
  \vspace{-2pt}\item
    \begin{tabular*}{0.97\textwidth}[t]{l@{\extracolsep{\fill}}r}
      \textbf{#1} & #2 \\
      \textit{\small#3} & \textit{\small #4} \\
    \end{tabular*}\vspace{-7pt}
}
\newcommand{\resumeProjectHeading}[2]{
    \item
    \begin{tabular*}{0.97\textwidth}{l@{\extracolsep{\fill}}r}
      \small#1 & #2 \\
    \end{tabular*}\vspace{-7pt}
}
\renewcommand\labelitemii{$\vcenter{\hbox{\tiny$\bullet$}}$}
\newcommand{\resumeSubHeadingListStart}{\begin{itemize}[leftmargin=0.15in, label={}]}
\newcommand{\resumeSubHeadingListEnd}{\end{itemize}}
\newcommand{\resumeItemListStart}{\begin{itemize}}
\newcommand{\resumeItemListEnd}{\end{itemize}\vspace{-5pt}}
\linespread{%(ls).2f}
\begin{document}
"""

_SPECIALS = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
             "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def esc(text: str) -> str:
    text = text.replace("\u2013", "--").replace("\u2014", "---").replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"(?<=\w) - (?=\w)", " -- ", text)  # "Mar 2026 - Present"
    return "".join(_SPECIALS.get(c, c) for c in text)


def _sections(text: str) -> tuple[list[str], dict[str, list[str]]]:
    header: list[str] = []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.strip().splitlines():
        line = raw.rstrip()
        s = line.strip()
        if s and s == s.upper() and len(s) > 3 and not s.startswith("-") and re.fullmatch(r"[A-Z &/]+", s):
            current = s
            sections[current] = []
        elif current is None:
            if s:
                header.append(s)
        else:
            sections[current].append(line)
    return header, sections


def _entries(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    cur: dict | None = None
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if s.startswith("- "):
            if cur:
                cur["bullets"].append(s[2:].strip())
        elif cur is None or cur["bullets"]:
            cur = {"title": s, "meta": "", "bullets": []}
            out.append(cur)
        elif not cur["meta"]:
            cur["meta"] = s
    return out


def _base_locations() -> dict[str, str]:
    """{lowercase organisation name: 'City, ST'} from the base resume's em-dash lines."""
    locs: dict[str, str] = {}
    try:
        for line in Path(RESUME_PATH).read_text(encoding="utf-8").splitlines():
            parts = [p.strip() for p in line.split(" \u2014 ")]
            for p in parts:
                if re.fullmatch(r"[A-Z][A-Za-z .]+, [A-Z][A-Za-z]+", p) and parts:
                    locs[parts[0].lower()] = p
            if len(parts) >= 2 and ", " in parts[0] and re.search(r"[A-Z]{2}$", parts[0]):
                head = parts[0].rsplit(", ", 2)
                if len(head) == 3:
                    locs[head[0].lower()] = f"{head[1]}, {head[2]}"
    except OSError:
        pass
    return locs


def _split_org(title: str) -> tuple[str, str]:
    """('Role', 'Org') from 'Role at Org'."""
    if " at " in title:
        role, org = title.split(" at ", 1)
        return role.strip(), org.strip()
    return "", title


def _meta(meta: str) -> tuple[str, str]:
    """(technologies, dates) from 'tech | dates'."""
    if "|" in meta:
        tech, dates = meta.rsplit("|", 1)
        return tech.strip(), dates.strip()
    return "", meta.strip()


def _location_for(org: str, locs: dict[str, str]) -> str:
    low = org.lower()
    if low in locs:
        return locs[low]
    for key, loc in locs.items():
        if key in low or low in key:
            return loc
    return ""


def _bullets(bullets: list[str]) -> str:
    if not bullets:
        return ""
    return "\\resumeItemListStart\n" + "\n".join(f"\\resumeItem{{{esc(b)}}}" for b in bullets) + "\n\\resumeItemListEnd\n"


def build_latex(text: str, pt: int = 11, ls: float = 1.0) -> str:
    header, sections = _sections(text)
    locs = _base_locations()
    name = header[0] if header else ""
    contact = header[-1] if len(header) > 1 and ("@" in header[-1] or "|" in header[-1]) else ""
    contact = re.sub(r"https?://(www\.)?", "", contact)

    tex = PREAMBLE % {"pt": pt, "ls": ls}
    tex += (f"\\begin{{center}}\n\\textbf{{\\Huge \\scshape {esc(name)}}} \\\\ \\vspace{{1pt}}\n"
            f"\\small {esc(contact).replace(' | ', ' $|$ ')}\n\\end{{center}}\n\n")

    if "EDUCATION" in sections:
        tex += "\\section{Education}\n\\resumeSubHeadingListStart\n"
        for line in (l.strip() for l in sections["EDUCATION"] if l.strip()):
            school, _, rest = line.partition("|")
            school, rest = school.strip(), rest.strip()
            base = ""
            try:
                for b in Path(RESUME_PATH).read_text(encoding="utf-8").splitlines():
                    if b.startswith(school):
                        base = b
                        break
            except OSError:
                pass
            m = re.search(r"\u2014 ([A-Z][A-Za-z .]+, [A-Z]{2}) \u2014 (.+)$", base) or \
                re.search(r"^.*?, ([A-Z][A-Za-z .]+, [A-Z]{2}) \u2014 (.+)$", base)
            loc, dates = (m.group(1), m.group(2)) if m else ("", "")
            tex += f"\\resumeSubheading{{{esc(school)}}}{{{esc(dates.replace(' \u2013 ', ' -- '))}}}{{{esc(rest)}}}{{{esc(loc)}}}\n"
        tex += "\\resumeSubHeadingListEnd\n\n"

    if "EXPERIENCE" in sections:
        tex += "\\section{Experience}\n\\resumeSubHeadingListStart\n"
        for e in _entries(sections["EXPERIENCE"]):
            role, org = _split_org(e["title"])
            _, dates = _meta(e["meta"])
            tex += (f"\\resumeSubheading{{{esc(org)}}}{{{esc(dates)}}}{{{esc(role)}}}"
                    f"{{{esc(_location_for(org, locs))}}}\n")
            tex += _bullets(e["bullets"])
        tex += "\\resumeSubHeadingListEnd\n\n"

    if "PROJECTS" in sections:
        tex += "\\section{Projects}\n\\resumeSubHeadingListStart\n"
        for e in _entries(sections["PROJECTS"]):
            tech, dates = _meta(e["meta"])
            pname = e["title"].split(" - ")[0].strip()
            left = f"\\textbf{{{esc(pname)}}}" + ((f" $|$ {{\\scriptsize\\emph{{{esc(tech)}}}}}" if len(tech) > 45 else f" $|$ \\emph{{{esc(tech)}}}") if tech else "")
            tex += f"\\resumeProjectHeading{{{left}}}{{{esc(dates)}}}\n"
            tex += _bullets(e["bullets"])
        tex += "\\resumeSubHeadingListEnd\n\n"

    if "TECHNICAL SKILLS" in sections:
        tex += ("\\section{Technical Skills}\n\\begin{itemize}[leftmargin=0.15in, label={}]\n"
                "\\small{\\item{\n")
        rows = []
        for line in (l.strip() for l in sections["TECHNICAL SKILLS"] if l.strip()):
            cat, _, val = line.partition(":")
            rows.append(f"\\textbf{{{esc(cat.strip())}}}{{: {esc(val.strip())}}}")
        tex += " \\\\\n".join(rows) + "\n}}\n\\end{itemize}\n"

    return tex + "\\end{document}\n"


def is_resume(text: str) -> bool:
    return bool(re.search(r"^EXPERIENCE\s*$", text, re.M) and re.search(r"^EDUCATION\s*$", text, re.M))


def _pages(pdf: Path) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf.read_bytes()))


def render_resume_pdf(text: str, output_path: Path) -> Path:
    """Compile with pdflatex so the resume fills exactly one page.

    Font stays at 11pt: line spacing is stretched as far as it still fits on one page
    (up to 1.3x). If 11pt cannot fit even unstretched, step down to 10pt then 9pt.
    """
    pdflatex = shutil.which("pdflatex") or "/Library/TeX/texbin/pdflatex"

    def compile_(pt: int, ls: float):
        with tempfile.TemporaryDirectory() as d:
            tex = Path(d) / "resume.tex"
            tex.write_text(build_latex(text, pt, ls), encoding="utf-8")
            res = subprocess.run([pdflatex, "-interaction=nonstopmode", "-halt-on-error", tex.name],
                                 cwd=d, capture_output=True, text=True, timeout=120)
            pdf = Path(d) / "resume.pdf"
            if res.returncode != 0 or not pdf.exists():
                raise RuntimeError(f"pdflatex failed: {res.stdout[-400:]}")
            m = re.search(r"Output written on .*?\((\d+) pages?", res.stdout)
            n = int(m.group(1)) if m else _pages(pdf)
            return pdf.read_bytes() if n <= 1 else None

    for pt in (11, 10, 9):
        if compile_(pt, 1.0) is None and pt != 9:
            continue
        best = compile_(pt, 1.0)
        lo, hi = 1.0, 1.3  # largest line spacing that still fits one page
        for _ in range(5):
            mid = (lo + hi) / 2
            got = compile_(pt, mid)
            if got is not None:
                best, lo = got, mid
            else:
                hi = mid
        if best is None:  # 9pt overflow: keep it anyway
            with tempfile.TemporaryDirectory() as d:
                tex = Path(d) / "resume.tex"
                tex.write_text(build_latex(text, 9, 1.0), encoding="utf-8")
                subprocess.run([pdflatex, "-interaction=nonstopmode", tex.name], cwd=d, capture_output=True, timeout=120)
                best = (Path(d) / "resume.pdf").read_bytes()
        Path(output_path).write_bytes(best)
        return Path(output_path)
    return Path(output_path)
