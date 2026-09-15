"""Render the review constraint profile and validate it with the SysML pilot kernel.

Compilation checks syntax, names and model well-formedness; solver feasibility and
stakeholder approval remain separate evidence. No LLM or remote repository calls.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any


_RELATIONS = {"le": "<=", "ge": ">=", "eq": "==", "lt": "<", "gt": ">"}
_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
# Only explicit, standard-library units are interpreted. Other unit strings are
# retained as documentation on a Real attribute, without invented unit semantics.
_UNITS = {
    "s": ("ISQ::DurationValue", "SI::s"),
    "seconds": ("ISQ::DurationValue", "SI::s"),
    "second": ("ISQ::DurationValue", "SI::s"),
    "m": ("ISQ::LengthValue", "SI::m"),
    "kg": ("ISQ::MassValue", "SI::kg"),
    "K": ("ISQ::ThermodynamicTemperatureValue", "SI::K"),
    "A": ("ISQ::ElectricCurrentValue", "SI::A"),
    "V": ("ISQ::ElectricPotentialValue", "SI::V"),
    "W": ("ISQ::PowerValue", "SI::W"),
    "J": ("ISQ::EnergyValue", "SI::J"),
    "Hz": ("ISQ::FrequencyValue", "SI::Hz"),
    "Pa": ("ISQ::PressureValue", "SI::Pa"),
}


def _identifier(value: Any, prefix: str) -> str:
    return prefix + re.sub(r"[^A-Za-z0-9_]", "_", str(value))[:100]


def _doc(value: Any) -> str:
    # Source documents are data, including when they contain SysML delimiters.
    return str(value).replace("*/", "* /").replace("/*", "/ *").replace("\x00", "")


def _number(value: Any) -> str:
    text = str(value)
    if not _DECIMAL.fullmatch(text):
        raise ValueError(f"Expected a finite exact decimal, got {text!r}.")
    # Decimal preserves the input value exactly while emitting plain SysML syntax.
    from decimal import Decimal
    decimal = Decimal(text)
    if abs(decimal.adjusted()) > 1000 or len(text) > 1024:
        raise ValueError("Numeric literal exceeds the supported size.")
    return format(decimal, "f")


def generate_sysml(name: str, requirements: list[dict], tlr: dict) -> dict:
    """Emit one shared system, quantity attributes and source-linked requirements.

    Quantitative predicates are copied from review_tlr/1. Unsupported requirements
    remain documentation-only requirements without synthetic truth predicates.
    """
    if tlr.get("schema") != "review_tlr/1":
        raise ValueError("Expected the review_tlr/1 schema.")
    clauses = tlr.get("requirements", [])
    source_by_id = {str(req["id"]): req for req in requirements}
    if len(source_by_id) != len(requirements):
        raise ValueError("Source requirement IDs must be unique.")
    clause_by_id = {str(req["id"]): req for req in clauses}
    if len(clause_by_id) != len(clauses) or set(clause_by_id) != set(source_by_id):
        raise ValueError("TLR and source requirements must have the same unique IDs.")

    symbols = tlr.get("symbols", [])
    symbol_by_name = {str(sym["name"]): sym for sym in symbols}
    if len(symbol_by_name) != len(symbols):
        raise ValueError("TLR symbol names must be unique.")
    names = {key: _identifier(key, "q_") for key in symbol_by_name}
    if len(set(names.values())) != len(names):
        raise ValueError("TLR symbols collide after SysML identifier normalization.")

    package = _identifier(name or "RequirementsReview", "Review_")
    lines = [
        f"package {package} {{",
        "    private import ScalarValues::*;",
        "    doc /* Requirements review: quantitative constraint profile.",
        "       Proposed satisfaction relationships are model claims for engineer review.",
        "       Compilation checks well-formedness; it does not prove feasibility or intent.",
        "       Timing attributes represent response-delay bounds under the documented context.",
        "       This profile does not encode command occurrence or an executable controller.",
        "    */",
        "    part def ReviewSystem {",
    ]
    elements: list[dict] = []
    physical_count = 0
    fallback_units: list[str] = []
    for key, symbol in symbol_by_name.items():
        unit = str(symbol.get("unit") or "")
        scalar_type, unit_ref = _UNITS.get(unit, ("ScalarValues::Real", ""))
        physical_count += bool(unit_ref)
        if unit not in {"", "1"} and not unit_ref and unit not in fallback_units:
            fallback_units.append(unit)
        label = names[key]
        unit_label = "dimensionless (unit 1)" if unit in {"", "1"} else unit
        description = (
            f"Subject: {symbol.get('subject', '')}; quantity: {symbol.get('quantity', key)}; "
            f"unit: {unit_label}; TLR symbol: {key}."
        )
        if unit not in {"", "1"} and not unit_ref:
            description += " Unit retained as metadata; no dimensional check for this unit."
        if unit == "%":
            description += " Values use the percent scale: 80 means 80 percent, not 0.8. No conversion to a unitless ratio is inferred."
        lines += [f"        attribute {label} : {scalar_type} {{", f"            doc /* {_doc(description)} */", "        }"]
        if symbol.get("minimum") is not None:
            bound = _number(symbol["minimum"])
            literal = f"{bound} [{unit_ref}]" if unit_ref else bound
            lines.append(f"        assert constraint domain_{label} {{ {label} >= {literal} }}")
        elements.append({"kind": "attribute", "name": f"{package}::ReviewSystem::{label}", "symbol": key, "unit": unit})
    lines += ["    }", "    part candidateSystem : ReviewSystem;", ""]

    supported = 0
    for index, source in enumerate(requirements, 1):
        req_id = str(source["id"])
        clause = clause_by_id[req_id]
        req_name = f"Req_{index}_" + _identifier(req_id, "")
        short_name = _doc(req_id).replace("'", "_").replace("\\", "_").replace("\n", " ").replace("\r", " ")
        origin = source.get("source") or {}
        documentation = [
            str(source.get("text", "")),
            f"Requirement ID: {req_id}",
            f"Source: {origin.get('document', '')}; location: {origin.get('location', '')}",
            f"Owner: {source.get('owner') or 'unassigned'}; authority: {source.get('authority') or 'unassigned'}",
            f"Formalization status: {clause.get('status', 'unsupported')}",
        ]
        for field in ("subject", "trigger", "response", "context", "reason"):
            if clause.get(field):
                documentation.append(f"{field.capitalize()}: {clause[field]}")
        for assumption in clause.get("assumptions", []):
            documentation.append(f"Assumption: {assumption}")
        lines += [f"    requirement <'{short_name}'> {req_name} {{", "        doc /* " + _doc("\n               ".join(documentation)) + " */"]
        encoded = clause.get("status") == "supported"
        if encoded:
            key = str(clause.get("symbol", ""))
            if key not in symbol_by_name:
                raise ValueError(f"Requirement {req_id} references unknown symbol {key!r}.")
            symbol_unit = str(symbol_by_name[key].get("unit") or "")
            if str(clause.get("unit") or "") != symbol_unit:
                raise ValueError(f"Requirement {req_id} unit differs from its symbol unit.")
            relation = _RELATIONS.get(clause.get("relation"))
            if relation is None:
                raise ValueError(f"Requirement {req_id} has an unsupported relation.")
            bound = _number(clause.get("value"))
            unit_ref = _UNITS.get(symbol_unit, ("", ""))[1]
            literal = f"{bound} [{unit_ref}]" if unit_ref else bound
            lines += ["        subject modeledSystem : ReviewSystem;", f"        require constraint {{ modeledSystem.{names[key]} {relation} {literal} }}"]
            supported += 1
        lines.append("    }")
        if encoded:
            lines.append(f"    satisfy {req_name} by candidateSystem;")
        lines.append("")
        elements.append({"kind": "requirement", "name": f"{package}::{req_name}", "requirement_id": req_id, "formalized": encoded, "source": origin})
    lines += ["}", ""]
    model = {
        "text": "\n".join(lines),
        "kind": "constraint-profile",
        "summary": {
            "requirements": len(requirements), "formalized": supported,
            "documentation_only": len(requirements) - supported,
            "quantity_attributes": len(symbols), "physical_quantity_attributes": physical_count,
            "units_as_metadata": fallback_units,
        },
        "elements": elements,
    }
    from review_model_index import build_model_inspection
    model["inspection"] = build_model_inspection(model, requirements, tlr)
    indexed = {entry["qualified_name"]: entry for entry in model["inspection"]["elements"] if entry.get("qualified_name")}
    for element in elements:
        entry = indexed.get(element["name"])
        if entry:
            for field in ("id", "start_line", "end_line", "start_column", "end_column", "start_offset", "end_offset", "source_requirement_ids", "tlr_symbols", "assumption_ids", "property_ids", "formalization"):
                element[field] = entry[field]
    return model


_JAVA_CHECKER = r'''
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.io.*;
import java.util.Base64;
import org.omg.sysml.interactive.SysMLInteractive;
import org.eclipse.xtext.validation.Issue;

public class ReviewSysmlCheck {
    public static void main(String[] args) throws Exception {
        PrintStream report = System.out;
        SysMLInteractive kernel = SysMLInteractive.createInstance();
        System.setOut(new PrintStream(OutputStream.nullOutputStream()));
        try { kernel.loadLibrary(args[1]); } finally { System.setOut(report); }
        // parse() alone has no resource and silently does nothing. next() is required.
        kernel.next(".sysml");
        kernel.parse(Files.readString(Path.of(args[0]), StandardCharsets.UTF_8));
        if (kernel.getResource() == null || kernel.getRootElement() == null) {
            throw new IllegalStateException("Parser did not produce a model resource.");
        }
        int errors = 0;
        for (Issue issue : kernel.validate()) {
            String severity = issue.getSeverity().toString();
            if (severity.equals("ERROR")) errors++;
            String message = Base64.getEncoder().encodeToString(issue.getMessage().getBytes(StandardCharsets.UTF_8));
            report.println("REVIEW_DIAG\t" + severity + "\t" + issue.getLineNumber() + "\t" + issue.getColumn() + "\t" + message);
        }
        report.println("REVIEW_RESULT\t" + errors);
        System.exit(errors == 0 ? 0 : 1);
    }
}
'''


def _compiler_paths() -> tuple[str | None, Path | None, Path | None]:
    java = os.getenv("SYSML_JAVA") or shutil.which("java")
    override = os.getenv("SYSML_KERNEL_JAR")
    if override:
        jar = Path(override).expanduser()
    else:
        candidates: list[Path] = []
        for conda in ("miniforge3", "miniconda3", "anaconda3"):
            base = Path.home() / conda
            roots = [base, *sorted((base / "envs").glob("*"))]
            for root in roots:
                candidates.extend((root / "share/jupyter/kernels/sysml").glob("jupyter-sysml-kernel-*-all.jar"))
        jar = sorted(candidates, reverse=True)[0] if candidates else None
    library_override = os.getenv("SYSML_LIBRARY_DIR")
    library = Path(library_override).expanduser() if library_override else (jar.parent / "sysml.library" if jar else None)
    return java, jar, library


def compiler_capability() -> dict:
    java, jar, library = _compiler_paths()
    absent = []
    if not java or not shutil.which(java):
        absent.append("Java executable")
    if jar is None or not jar.is_file():
        absent.append("SysML pilot kernel JAR (SYSML_KERNEL_JAR)")
    if library is None or not (library / "Systems Library/Requirements.sysml").is_file():
        absent.append("SysML standard library (SYSML_LIBRARY_DIR)")
    version_match = re.search(r"kernel-(.+)-all\.jar$", jar.name) if jar else None
    return {
        "available": not absent,
        "detail": "Missing " + ", ".join(absent) if absent else "Local SysML pilot parser and model validator available.",
        "tool": "SysML v2 Pilot Implementation",
        "version": version_match.group(1) if version_match else "unknown",
        "java": java, "jar": str(jar) if jar else None, "library": str(library) if library else None,
    }


def compile_sysml(path: Path, timeout: float = 90) -> dict:
    """Parse and validate the complete local model; never infer success from exit 0 alone."""
    capability = compiler_capability()
    result: dict = {"status": "not_run", "tool": capability["tool"], "version": capability["version"], "diagnostics": [], "command": []}
    if not capability["available"]:
        result["diagnostics"] = [{"severity": "info", "message": capability["detail"]}]
        return result
    path = Path(path).resolve()
    if not path.is_file():
        result.update(status="failed", diagnostics=[{"severity": "error", "message": f"Model file not found: {path}"}])
        return result
    result["model_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    result["adapter_sha256"] = hashlib.sha256(_JAVA_CHECKER.encode("utf-8")).hexdigest()
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mbse-sysml-check-") as directory:
        checker = Path(directory) / "ReviewSysmlCheck.java"
        checker.write_text(_JAVA_CHECKER, encoding="utf-8")
        command = [capability["java"], "--class-path", capability["jar"], str(checker), str(path), capability["library"]]
        result["command"] = command
        try:
            completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout, check=False, cwd=directory)
        except subprocess.TimeoutExpired:
            result.update(status="failed", diagnostics=[{"severity": "error", "message": f"Compiler exceeded the {timeout:g} second limit."}])
        except OSError as exc:
            result.update(status="not_run", diagnostics=[{"severity": "error", "message": str(exc)}])
        else:
            diagnostics = []
            completion = None
            for line in completed.stdout.splitlines():
                if line.startswith("REVIEW_DIAG\t"):
                    fields = line.split("\t", 4)
                    if len(fields) == 5:
                        diagnostics.append({"severity": fields[1].lower(), "line": int(fields[2]) if fields[2].isdigit() else None, "column": int(fields[3]) if fields[3].isdigit() else None, "message": base64.b64decode(fields[4]).decode("utf-8", "replace")})
                elif re.fullmatch(r"REVIEW_RESULT\t\d+", line):
                    completion = int(line.split("\t")[1])
            result["exit_code"] = completed.returncode
            result["status"] = "passed" if completion == 0 and completed.returncode == 0 else "failed"
            if completion is None or (completed.returncode != 0 and not diagnostics):
                diagnostics.append({"severity": "error", "message": "Compiler adapter did not complete validation. " + (completed.stderr + completed.stdout)[-5000:]})
            result["diagnostics"] = diagnostics
    result["duration_seconds"] = round(time.monotonic() - start, 3)
    return result
