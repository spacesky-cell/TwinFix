"""JUnit XML 解析（pytest 与 Node test runner 共用）。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from twinfix.profiles.base import TestCase

_TESTCASE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
_ATTR = re.compile(r'(\w+)="([^"]*)"')


def parse_junit(xml_text: str) -> list[TestCase]:
    """解析 JUnit XML；报告损坏时退回按 <testcase> 提取，工具不整体报错。"""
    try:
        root = ET.fromstring(xml_text)  # noqa: S314
    except ET.ParseError:
        return _parse_junit_lenient(xml_text)
    cases: list[TestCase] = []
    for tc in root.iter("testcase"):
        classname = tc.get("classname", "")
        name = tc.get("name", "")
        status, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            node = tc.find(tag)
            if node is not None:
                status = "failed" if tag == "failure" else tag
                message = node.get("message") or (node.text or "").strip()[:2000]
                break
        cases.append(TestCase(name=f"{classname}#{name}" if classname else name, status=status, message=message))
    return cases


def _parse_junit_lenient(text: str) -> list[TestCase]:
    cases: list[TestCase] = []
    for attrs, _, body in _TESTCASE.findall(text):
        a = dict(_ATTR.findall(attrs))
        status, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            m = re.search(rf"<{tag}\b([^>]*)", body or "")
            if m:
                status = "failed" if tag == "failure" else tag
                message = dict(_ATTR.findall(m.group(1))).get("message", "")[:2000]
                break
        name = f"{a.get('classname', '')}#{a.get('name', '')}".lstrip("#")
        cases.append(TestCase(name=name, status=status, message=message))
    return cases
