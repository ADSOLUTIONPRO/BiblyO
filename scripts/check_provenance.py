#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Contrôle de reprise de code : biblyO contre des empreintes d'un logiciel antérieur, sans aucun de ses fichiers.

Le jeu de référence (`scripts/provenance/reference.bin`) ne contient que des empreintes à sens unique (hachages de
40 bits) de suites de jetons. Aucun code, gabarit, texte ou nom n'en est extrait ni reconstructible. Deux mesures :

- **brute** : suites de 20 jetons à l'identique (noms, chaînes et valeurs compris) ; repère une copie ;
- **logique** : suites de 32 jetons où noms, chaînes et nombres sont remplacés par leur catégorie, retenues seulement si
  elles portent au moins quatre mots-clés ou opérateurs distincts ; repère une copie dont les noms ont été changés,
  sans signaler les tables de libellés ou les listes de champs, de même forme dans tout programme.

Le contrôle échoue sur toute suite commune de 30 jetons bruts ou de 60 jetons logiques, sauf si elle figure, examinée et
justifiée, dans `scripts/provenance/examines.json`. Un résultat favorable n'est pas une certification juridique : il
signale ce qui demande une revue humaine. Méthode et seuils : `docs/rapports/AUDIT-INDEPENDANCE-BIBLYO-V1.md`.

Usage : `check_provenance.py` (contrôle), `check_provenance.py --rapport` (toutes les suites communes, même courtes),
`check_provenance.py --reference <nom>=<dossier>...` (reconstruit la référence, hors dépôt, depuis des copies locales).
"""

from __future__ import annotations

import hashlib
import io
import json
import keyword
import re
import sys
import tokenize
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "scripts" / "provenance"
REFERENCE = HERE / "reference.bin"
REVIEWED = HERE / "examines.json"
SCANNED = ("src", "tests", "scripts", "deploy")
SKIPPED_DIRS = {"vendor", "fonts", "dist", "__pycache__", ".git", "node_modules", ".venv", "provenance"}
DIGEST = 5
MODES = {"brute": (20, 6), "logique": (32, 8)}
LIMITS = {"brute": 30, "logique": 60}
TRIVIAL = {",", ":", "(", ")", "[", "]", "{", "}", ".", "=", ";", "N", "S", "0", "T"}
Token = tuple[str, str, int]  # (catégorie, texte brut, ligne)

# --- Découpage en jetons, par langage ----------------------------------------------------------------------------


def _python(text: str) -> list[Token]:
    out: list[Token] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.NAME:
                kind = tok.string if keyword.iskeyword(tok.string) else "N"
            elif tok.type == tokenize.STRING:
                kind = "S"
            elif tok.type == tokenize.NUMBER:
                kind = "0"
            elif tok.type == tokenize.OP:
                kind = tok.string
            else:
                continue
            out.append((kind, tok.string, tok.start[0]))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


_JS = re.compile(
    r"""//[^\n]*|/\*.*?\*/|`(?:\\.|[^`])*`|"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|\d+(?:\.\d+)?|[A-Za-z_$][\w$]*"""
    r"""|===|!==|=>|&&|\|\||[{}()\[\];,.<>+\-*/%=!?:&|^~]""",
    re.S,
)
_JS_KEYWORDS = {
    *("break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete", "do", "else"),
    *("export", "extends", "finally", "for", "function", "if", "import", "in", "instanceof", "let", "new"),
    *("return", "super", "switch", "this", "throw", "try", "typeof", "var", "void", "while", "with", "yield"),
    *("async", "await", "null", "true", "false", "undefined", "of"),
}


def _javascript(text: str) -> list[Token]:
    out: list[Token] = []
    for match in _JS.finditer(text):
        raw = match.group(0)
        if raw.startswith(("//", "/*")):
            continue
        if raw[0] in "\"'`":
            kind = "S"
        elif raw[0].isdigit():
            kind = "0"
        elif raw[0].isalpha() or raw[0] in "_$":
            kind = raw if raw in _JS_KEYWORDS else "N"
        else:
            kind = raw
        out.append((kind, raw, text.count("\n", 0, match.start()) + 1))
    return out


def _css(text: str) -> list[Token]:
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    pattern = r"[\w-]+|#[0-9a-fA-F]{3,8}|[{}:;,>.()%]"
    return [(m.group(0), m.group(0), text.count("\n", 0, m.start()) + 1) for m in re.finditer(pattern, text)]


def _template(text: str) -> list[Token]:
    out: list[Token] = []
    for match in re.finditer(r"\{%.*?%\}|\{\{.*?\}\}|<[^>]+>|[^<{\s][^<{]*", text, re.S):
        raw = match.group(0).strip()
        line = text.count("\n", 0, match.start()) + 1
        if raw.startswith("<"):
            name = re.match(r"</?\s*([\w-]+)", raw)
            out.append(("<" + (name.group(1) if name else "?"), raw, line))
        elif raw.startswith("{%"):
            out.append(("{%" + (raw[2:].split() or [""])[0], raw, line))
        elif raw.startswith("{{"):
            out.append(("{{", raw, line))
        else:
            out.append(("T", raw, line))
    return out


TOKENIZERS: dict[str, Callable[[str], list[Token]]] = {
    ".py": _python,
    ".js": _javascript,
    ".css": _css,
    ".html": _template,
}


def tokens(path: Path) -> list[Token]:
    if path.suffix not in TOKENIZERS or path.name.endswith(".min.js"):
        return []
    return TOKENIZERS[path.suffix](path.read_text(encoding="utf-8", errors="ignore"))


# --- Empreintes ----------------------------------------------------------------------------------------------------


def _hash(gram: Iterable[str]) -> int:
    return int.from_bytes(hashlib.blake2b("\x1f".join(gram).encode(), digest_size=DIGEST).digest(), "big")


def fingerprints(toks: list[Token], mode: str) -> dict[int, int]:
    """Position → empreinte retenue (winnowing : le minimum de chaque fenêtre de w k-grammes)."""
    size, window = MODES[mode]
    seq = [raw if mode == "brute" else kind for kind, raw, _ in toks]
    hashes: list[int | None] = []
    for i in range(len(seq) - size + 1):
        gram = seq[i : i + size]
        if mode == "logique" and len({t for t in gram if t not in TRIVIAL}) < 4:
            hashes.append(None)
        else:
            hashes.append(_hash(gram))
    chosen: dict[int, int] = {}
    for start in range(max(0, len(hashes) - window + 1)):
        candidates = [(h, -k) for k in range(start, start + window) if (h := hashes[k]) is not None]
        if candidates:
            value, position = min(candidates)
            chosen[-position] = value
    return chosen


@dataclass(frozen=True)
class Run:
    mode: str
    path: str
    first_line: int
    last_line: int
    length: int
    key: str  # empreinte des jetons bruts de la suite, pour l'inscrire comme examinée

    def describe(self) -> str:
        return f"{self.path}:{self.first_line}-{self.last_line}: suite {self.mode} de {self.length} jetons ({self.key})"


def runs(path: Path, rel: str, reference: dict[str, set[int]]) -> list[Run]:
    toks = tokens(path)
    found: list[Run] = []
    for mode, (size, window) in MODES.items():
        hits = sorted(i for i, h in fingerprints(toks, mode).items() if h in reference[mode])
        i = 0
        while i < len(hits):
            j = i
            while j + 1 < len(hits) and hits[j + 1] - hits[j] <= window:
                j += 1
            first, last = hits[i], hits[j] + size - 1
            key = hashlib.sha256("\x1f".join(t[1] for t in toks[first : last + 1]).encode()).hexdigest()[:16]
            found.append(Run(mode, rel, toks[first][2], toks[last][2], last - first + 1, key))
            i = j + 1
    return found


# --- Référence -----------------------------------------------------------------------------------------------------


def corpus(root: Path, scanned: Iterable[str] | None = None) -> Iterable[tuple[Path, str]]:
    tops = [root / top for top in scanned] if scanned else [root]
    for top in tops:
        for path in sorted(top.rglob("*")):
            rel = path.relative_to(root)
            if path.is_file() and not (set(rel.parts[:-1]) & SKIPPED_DIRS) and path.suffix in TOKENIZERS:
                yield path, rel.as_posix()


def write_reference(specs: list[str], destination: Path = REFERENCE) -> None:
    sets: dict[str, set[int]] = {mode: set() for mode in MODES}
    sources = []
    for spec in specs:
        name, folder = spec.split("=", 1)
        count = 0
        for path, _ in corpus(Path(folder)):
            toks = tokens(path)
            for mode in MODES:
                sets[mode].update(fingerprints(toks, mode).values())
            count += 1
        sources.append({"nom": name, "fichiers": count})
    header = {
        "format": 1,
        "empreinte": f"blake2b-{DIGEST * 8}",
        "modes": {mode: {"k": MODES[mode][0], "w": MODES[mode][1], "nombre": len(sets[mode])} for mode in MODES},
        "sources": sources,
    }
    with destination.open("wb") as out:
        out.write(json.dumps(header, ensure_ascii=False, sort_keys=True).encode() + b"\n")
        for mode in MODES:
            for value in sorted(sets[mode]):
                out.write(value.to_bytes(DIGEST, "big"))


def read_reference(path: Path = REFERENCE) -> tuple[dict, dict[str, set[int]]]:
    data = path.read_bytes()
    end = data.index(b"\n")
    header = json.loads(data[:end])
    body = memoryview(data)[end + 1 :]
    sets: dict[str, set[int]] = {}
    offset = 0
    for mode in MODES:
        count = header["modes"][mode]["nombre"]
        chunk = body[offset : offset + count * DIGEST]
        sets[mode] = {int.from_bytes(chunk[i : i + DIGEST], "big") for i in range(0, len(chunk), DIGEST)}
        offset += count * DIGEST
    if offset != len(body):
        raise ValueError("référence d'empreintes tronquée ou altérée")
    return header, sets


# --- Contrôle ------------------------------------------------------------------------------------------------------


def check(root: Path = ROOT, reference: Path = REFERENCE, reviewed: Path = REVIEWED) -> tuple[list[str], list[Run]]:
    if not reference.exists():
        return [f"{reference}: référence d'empreintes absente"], []
    _, sets = read_reference(reference)
    accepted = {(e["fichier"], e["empreinte"]) for e in json.loads(reviewed.read_text(encoding="utf-8"))["examinees"]}
    problems, everything = [], []
    for path, rel in corpus(root, SCANNED):
        for run in runs(path, rel, sets):
            everything.append(run)
            if run.length >= LIMITS[run.mode] and (run.path, run.key) not in accepted:
                problems.append(run.describe() + " : non examinée")
    return problems, everything


def main(argv: list[str]) -> int:
    if "--reference" in argv:
        specs = argv[argv.index("--reference") + 1 :]
        write_reference(specs)
        print(f"Référence écrite : {REFERENCE.relative_to(ROOT)} ({', '.join(s.split('=')[0] for s in specs)})")
        return 0
    problems, everything = check()
    if "--rapport" in argv:
        for run in sorted(everything, key=lambda r: -r.length):
            print(run.describe())
    for problem in problems:
        print(problem)
    if problems:
        print(
            "Contrôle de reprise : refusé. Examiner chaque suite (voir docs/rapports/AUDIT-INDEPENDANCE-BIBLYO-V1.md)."
        )
        return 1
    longest = {mode: max((r.length for r in everything if r.mode == mode), default=0) for mode in MODES}
    print(
        f"Contrôle de reprise : aucune suite commune substantielle (plus longues : {longest['brute']} jetons bruts, "
        f"{longest['logique']} jetons logiques ; seuils {LIMITS['brute']} et {LIMITS['logique']})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
