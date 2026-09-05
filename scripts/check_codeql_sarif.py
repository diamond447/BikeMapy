"""Fail a CodeQL job for error-level or high/critical SARIF findings.

CodeQL stores ``security-severity`` on the referenced driver rule rather than
on each result.  This small gate joins results to both driver and extension
rules so the CI policy is deterministic and testable without GitHub access.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class _Component:
    # ``index`` is the component-relative extension index.  The driver is
    # addressed by omission in ReportingDescriptorReference.toolComponent.
    index: int | None
    name: str
    guid: str
    rules: tuple[dict[str, Any], ...]


def _components(run: dict[str, Any]) -> tuple[_Component, ...]:
    tool = run.get("tool", {})
    if not isinstance(tool, dict):
        return ()
    driver = tool.get("driver", {})
    if not isinstance(driver, dict):
        return ()
    collections = [(None, driver), *enumerate(tool.get("extensions", []))]
    components: list[_Component] = []
    for index, collection in collections:
        if not isinstance(collection, dict):
            continue
        rules = tuple(
            rule
            for rule in collection.get("rules", [])
            if isinstance(rule, dict) and isinstance(rule.get("id"), str)
        )
        components.append(
            _Component(
                index=index,
                name=str(collection.get("name", "")),
                guid=str(collection.get("guid", "")),
                rules=rules,
            )
        )
    return tuple(components)


def _component_matches(
    components: tuple[_Component, ...], reference: dict[str, Any] | None
) -> tuple[_Component, ...]:
    if not reference:
        return components
    candidates = components
    if isinstance(reference.get("index"), int):
        # SARIF's ToolComponentReference.index is relative to extensions; the
        # driver has no component index and is selected by omission.
        candidates = tuple(
            item
            for item in candidates
            if item.index is not None and item.index == reference["index"]
        )
    if isinstance(reference.get("name"), str):
        candidates = tuple(item for item in candidates if item.name == reference["name"])
    if isinstance(reference.get("guid"), str):
        candidates = tuple(item for item in candidates if item.guid == reference["guid"])
    return candidates


def _resolve_rule(
    run: dict[str, Any], result: dict[str, Any]
) -> tuple[str, dict[str, Any] | None, bool]:
    """Return rule id, rule, and whether resolution was ambiguous/unresolved."""

    components = _components(run)
    reference = result.get("rule")
    if isinstance(reference, dict):
        rule_id = reference.get("id", "")
        index = reference.get("index")
        component_reference = reference.get("toolComponent")
        if component_reference is not None and not isinstance(component_reference, dict):
            return str(rule_id), None, True
        selected = (
            (components[0],)
            if component_reference is None and components
            else _component_matches(components, component_reference)
        )
        if len(selected) != 1:
            return str(rule_id), None, True
        component = selected[0]
        by_id = [rule for rule in component.rules if rule.get("id") == rule_id]
        by_index = (
            [component.rules[index]]
            if isinstance(index, int) and 0 <= index < len(component.rules)
            else []
        )
        if rule_id and by_id and by_index and by_id[0] is not by_index[0]:
            return str(rule_id), None, True
        matches = by_id or by_index
        if len(matches) != 1:
            return str(rule_id), None, True
        return str(matches[0]["id"]), matches[0], False

    # SARIF 2.1 legacy result.ruleId/ruleIndex identify the driver component.
    rule_id = result.get("ruleId", "")
    driver = components[0] if components else None
    if isinstance(rule_id, str) and rule_id:
        matches = [
            rule
            for component in components
            for rule in component.rules
            if rule.get("id") == rule_id
        ]
        if len(matches) == 1:
            return rule_id, matches[0], False
        if driver and isinstance(result.get("ruleIndex"), int):
            index = result["ruleIndex"]
            if 0 <= index < len(driver.rules) and driver.rules[index].get("id") == rule_id:
                return rule_id, driver.rules[index], False
        return rule_id, None, True
    if driver and isinstance(result.get("ruleIndex"), int):
        index = result["ruleIndex"]
        if 0 <= index < len(driver.rules):
            rule = driver.rules[index]
            return str(rule["id"]), rule, False
    return "", None, True


def _severity(result: dict[str, Any], rule: dict[str, Any] | None) -> float:
    properties: dict[str, Any] = {}
    properties.update(result.get("properties", {}))
    # Supporting result properties keeps the gate compatible with SARIF
    # producers that inline metadata, while the referenced rule remains
    # authoritative when both contain a value.
    if rule:
        properties.update(rule.get("properties", {}))
    try:
        return float(properties.get("security-severity", 0))
    except (TypeError, ValueError):
        return 0.0


def actionable_findings(document: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(document, dict) or not isinstance(document.get("runs"), list):
        raise ValueError("SARIF document must contain a runs array")
    findings: list[dict[str, Any]] = []
    for run in document.get("runs", []):
        if not isinstance(run, dict) or not isinstance(run.get("results", []), list):
            raise ValueError("SARIF run must contain a results array")
        for result in run.get("results", []):
            if not isinstance(result, dict):
                raise ValueError("SARIF result must be an object")
            rule_id, rule, unresolved = _resolve_rule(run, result)
            severity = _severity(result, rule)
            # Unknown or ambiguous rule metadata is actionable: silently
            # treating it as low severity would make the gate fail open.
            if result.get("level") == "error" or unresolved or severity >= 7:
                findings.append(
                    {
                        "ruleId": rule_id,
                        "level": result.get("level"),
                        "severity": severity if not unresolved else None,
                    }
                )
    return findings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} SARIF_FILE_OR_DIRECTORY", file=sys.stderr)
        return 2
    target = Path(argv[1])
    files = sorted(target.rglob("*.sarif")) if target.is_dir() else [target]
    if not files:
        print(f"No SARIF file found under {target}", file=sys.stderr)
        return 2
    findings: list[dict[str, Any]] = []
    try:
        for file in files:
            findings.extend(actionable_findings(json.loads(file.read_text(encoding="utf-8"))))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        print(f"Invalid SARIF input: {error}", file=sys.stderr)
        return 2
    if findings:
        print("Actionable CodeQL findings:")
        for finding in findings:
            print(
                f"- {finding['ruleId']} (level={finding['level']}, severity={finding['severity']})"
            )
        return 1
    print("No actionable CodeQL findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
