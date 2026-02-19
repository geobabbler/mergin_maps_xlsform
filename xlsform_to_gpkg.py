#!/usr/bin/env python3
"""Convert XLSForm schema into a GeoPackage + QGIS project for Mergin Maps."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import re
import sqlite3
from typing import Any
import xml.etree.ElementTree as ET
import zipfile


class XLSFormValidationError(ValueError):
    """Raised when an XLSForm fails structural validation."""


@dataclass(frozen=True)
class Question:
    row_number: int
    question_type: str
    name: str
    label: str
    hint: str
    list_name: str | None
    repeat_path: tuple[str, ...]
    required: bool
    relevant: str | None
    calculation: str | None


@dataclass(frozen=True)
class ParsedXLSForm:
    source_path: Path
    questions: list[Question]
    choices: dict[str, dict[str, str]]
    settings: dict[str, str]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    sql_type: str
    source_question: Question | None


@dataclass(frozen=True)
class GeometrySpec:
    column_name: str
    geometry_type_name: str
    source_question: Question


@dataclass(frozen=True)
class LayerSchema:
    table_name: str
    repeat_path: tuple[str, ...]
    parent_table_name: str | None
    fields: list[FieldSpec]
    geometry_fields: list[GeometrySpec]


REQUIRED_SHEETS = ("survey", "choices", "settings")
REQUIRED_SURVEY_COLUMNS = ("type", "name")
REQUIRED_CHOICES_COLUMNS = ("list_name", "name", "label")
REQUIRED_SETTINGS_COLUMNS = ("form_id",)
SUPPORTED_BASE_TYPES = {
    "start",
    "end",
    "deviceid",
    "today",
    "text",
    "integer",
    "decimal",
    "select_one",
    "select_multiple",
    "geopoint",
    "geotrace",
    "geoshape",
    "date",
    "time",
    "dateTime",
    "image",
    "audio",
    "video",
    "note",
    "calculate",
    "begin_group",
    "end_group",
    "begin_repeat",
    "end_repeat",
}

GEOMETRY_TYPE_MAP = {
    "geopoint": "POINT",
    "geotrace": "LINESTRING",
    "geoshape": "POLYGON",
}

NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
NS_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
MAX_IDENTIFIER_LEN = 63

SRS_4326 = {
    "authid": "EPSG:4326",
    "description": "WGS 84",
    "projectionacronym": "longlat",
    "ellipsoidacronym": "EPSG:7030",
    "geographicflag": "true",
    "srid": "4326",
    "srsid": "3452",
    "proj4": "+proj=longlat +datum=WGS84 +no_defs",
    "wkt": (
        'GEOGCS["WGS 84",DATUM["WGS_1984",'
        'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
        'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0],'
        'UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]'
    ),
}

SRS_3785 = {
    "authid": "EPSG:3785",
    "description": "Popular Visualisation CRS / Mercator",
    "projectionacronym": "merc",
    "ellipsoidacronym": "EPSG:7030",
    "geographicflag": "false",
    "srid": "3785",
    "srsid": "3785",
    "proj4": (
        "+proj=merc +a=6378137 +b=6378137 +lat_ts=0.0 +lon_0=0.0 "
        "+x_0=0.0 +y_0=0.0 +k=1.0 +units=m +nadgrids=@null +wktext +no_defs"
    ),
    "wkt": (
        'PROJCS["Popular Visualisation CRS / Mercator",'
        'GEOGCS["Popular Visualisation CRS",DATUM["Popular_Visualisation_Datum",'
        'SPHEROID["Popular Visualisation Sphere",6378137,0]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],'
        'PROJECTION["Mercator_1SP"],PARAMETER["central_meridian",0],'
        'PARAMETER["scale_factor",1],PARAMETER["false_easting",0],'
        'PARAMETER["false_northing",0],UNIT["metre",1],AUTHORITY["EPSG","3785"]]'
    ),
}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _column_index(cell_ref: str) -> int:
    letters = ""
    for ch in cell_ref:
        if ch.isalpha():
            letters += ch
        else:
            break
    if not letters:
        return 0

    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch.upper()) - ord("A") + 1)
    return idx


def _load_sheet_path_map(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as zf:
        workbook_root = ET.fromstring(zf.read("xl/workbook.xml"))
        rel_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))

    rel_map: dict[str, str] = {}
    for rel in rel_root.findall(f"{NS_PKG_REL}Relationship"):
        rid = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if rid and target:
            rel_map[rid] = f"xl/{target}" if not target.startswith("xl/") else target

    sheet_map: dict[str, str] = {}
    for sheet in workbook_root.findall(f"{NS_MAIN}sheets/{NS_MAIN}sheet"):
        name = sheet.attrib.get("name")
        rid = sheet.attrib.get(f"{NS_REL}id")
        if name and rid and rid in rel_map:
            sheet_map[name] = rel_map[rid]
    return sheet_map


def _load_shared_strings(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        if "xl/sharedStrings.xml" not in zf.namelist():
            return []
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))

    strings: list[str] = []
    for si in root.findall(f"{NS_MAIN}si"):
        t = si.find(f"{NS_MAIN}t")
        if t is not None:
            strings.append(t.text or "")
            continue
        runs = si.findall(f"{NS_MAIN}r/{NS_MAIN}t")
        strings.append("".join((r.text or "") for r in runs))
    return strings


def _cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")

    if cell_type == "inlineStr":
        text_nodes = cell.findall(f"{NS_MAIN}is/{NS_MAIN}t")
        return "".join(node.text or "" for node in text_nodes).strip()

    if cell_type == "s":
        value_node = cell.find(f"{NS_MAIN}v")
        if value_node is None or value_node.text is None:
            return ""
        try:
            index = int(value_node.text)
            return _as_text(shared_strings[index])
        except (ValueError, IndexError):
            return ""

    value_node = cell.find(f"{NS_MAIN}v")
    if value_node is None:
        return ""
    return _as_text(value_node.text)


def _read_rows_by_header(path: Path, sheet_name: str, shared_strings: list[str]) -> list[dict[str, str]]:
    sheet_path_map = _load_sheet_path_map(path)
    if sheet_name not in sheet_path_map:
        raise XLSFormValidationError(f"Workbook is missing required sheet '{sheet_name}'.")

    with zipfile.ZipFile(path) as zf:
        ws_root = ET.fromstring(zf.read(sheet_path_map[sheet_name]))

    rows_elems = ws_root.findall(f"{NS_MAIN}sheetData/{NS_MAIN}row")
    if not rows_elems:
        raise XLSFormValidationError(f"Sheet '{sheet_name}' is empty.")

    parsed_rows: list[list[str]] = []
    max_col = 0
    for row_elem in rows_elems:
        row_values: dict[int, str] = {}
        for cell in row_elem.findall(f"{NS_MAIN}c"):
            ref = cell.attrib.get("r", "")
            col = _column_index(ref)
            if col == 0:
                continue
            row_values[col] = _cell_value(cell, shared_strings)
            if col > max_col:
                max_col = col

        if max_col == 0:
            continue

        row = [""] * max_col
        for col, value in row_values.items():
            row[col - 1] = value
        parsed_rows.append(row)

    if not parsed_rows:
        raise XLSFormValidationError(f"Sheet '{sheet_name}' has no readable rows.")

    headers = [_as_text(v).lower() for v in parsed_rows[0]]
    if not any(headers):
        raise XLSFormValidationError(f"Sheet '{sheet_name}' has an empty header row.")

    records: list[dict[str, str]] = []
    for row in parsed_rows[1:]:
        record: dict[str, str] = {}
        for i, key in enumerate(headers):
            if not key:
                continue
            record[key] = _as_text(row[i]) if i < len(row) else ""
        if any(record.values()):
            records.append(record)

    return records


def _ensure_columns(rows: list[dict[str, str]], required_columns: tuple[str, ...], sheet_name: str) -> None:
    if not rows:
        raise XLSFormValidationError(f"Sheet '{sheet_name}' has no data rows.")
    present = set(rows[0].keys())
    missing = [col for col in required_columns if col not in present]
    if missing:
        raise XLSFormValidationError(
            f"Sheet '{sheet_name}' is missing required column(s): {', '.join(missing)}."
        )


def _validate_sheet_names(path: Path) -> None:
    sheet_map = _load_sheet_path_map(path)
    present = set(sheet_map.keys())
    missing = [name for name in REQUIRED_SHEETS if name not in present]
    if missing:
        raise XLSFormValidationError(
            f"Workbook is missing required sheet(s): {', '.join(missing)}."
        )


def _question_base_type(question_type: str) -> str:
    return question_type.split()[0]


def _parse_questions(rows: list[dict[str, str]]) -> list[Question]:
    questions: list[Question] = []
    repeat_stack: list[str] = []
    seen_names: set[str] = set()

    for idx, row in enumerate(rows, start=2):
        qtype = row.get("type", "")
        name = row.get("name", "")
        label = row.get("label", "")
        hint = row.get("hint", "")
        required = row.get("required", "").lower() in {"yes", "true", "1"}
        relevant = row.get("relevant") or None
        calculation = row.get("calculation") or None

        if not qtype:
            raise XLSFormValidationError(f"survey row {idx}: 'type' is required.")

        parts = qtype.split()
        base_type = parts[0]
        list_name = None

        if base_type not in SUPPORTED_BASE_TYPES:
            raise XLSFormValidationError(f"survey row {idx}: unsupported type '{qtype}'.")

        if base_type in {"select_one", "select_multiple"}:
            if len(parts) < 2:
                raise XLSFormValidationError(
                    f"survey row {idx}: '{base_type}' must reference a choice list name."
                )
            list_name = parts[1]

        if base_type in {"begin_group", "begin_repeat"}:
            if not name:
                raise XLSFormValidationError(
                    f"survey row {idx}: '{base_type}' requires a non-empty 'name'."
                )
            if base_type == "begin_repeat":
                repeat_stack.append(name)
            continue

        if base_type == "end_repeat":
            if not repeat_stack:
                raise XLSFormValidationError(
                    f"survey row {idx}: unexpected 'end_repeat' without matching 'begin_repeat'."
                )
            repeat_stack.pop()
            continue

        if base_type == "end_group":
            continue

        if not name:
            raise XLSFormValidationError(f"survey row {idx}: 'name' is required for type '{qtype}'.")

        if name in seen_names:
            raise XLSFormValidationError(f"survey row {idx}: duplicate question name '{name}'.")
        seen_names.add(name)

        questions.append(
            Question(
                row_number=idx,
                question_type=qtype,
                name=name,
                label=label,
                hint=hint,
                list_name=list_name,
                repeat_path=tuple(repeat_stack),
                required=required,
                relevant=relevant,
                calculation=calculation,
            )
        )

    if repeat_stack:
        raise XLSFormValidationError(
            "survey: one or more 'begin_repeat' blocks are not closed by 'end_repeat'."
        )

    return questions


def _parse_choices(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    by_list: dict[str, dict[str, str]] = {}

    for idx, row in enumerate(rows, start=2):
        list_name = row.get("list_name", "")
        code = row.get("name", "")
        label = row.get("label", "")

        if not list_name or not code:
            raise XLSFormValidationError(
                f"choices row {idx}: both 'list_name' and 'name' are required."
            )

        list_bucket = by_list.setdefault(list_name, {})
        if code in list_bucket:
            raise XLSFormValidationError(
                f"choices row {idx}: duplicate choice name '{code}' in list '{list_name}'."
            )
        list_bucket[code] = label

    return by_list


def _validate_choice_references(questions: list[Question], choices: dict[str, dict[str, str]]) -> None:
    missing: list[str] = []
    for q in questions:
        if q.list_name and q.list_name not in choices:
            missing.append(
                f"survey row {q.row_number}: referenced list '{q.list_name}' was not found in choices."
            )
    if missing:
        raise XLSFormValidationError("\n".join(missing))


def _parse_settings(rows: list[dict[str, str]]) -> dict[str, str]:
    settings = rows[0]
    form_id = settings.get("form_id", "")
    if not form_id:
        raise XLSFormValidationError("settings row 2: 'form_id' is required.")
    return settings


def load_and_validate_xlsform(path: Path) -> ParsedXLSForm:
    if not path.exists():
        raise XLSFormValidationError(f"Input file does not exist: {path}")
    if path.suffix.lower() != ".xlsx":
        raise XLSFormValidationError(f"Input must be an .xlsx file: {path}")

    _validate_sheet_names(path)
    shared_strings = _load_shared_strings(path)

    survey_rows = _read_rows_by_header(path, "survey", shared_strings)
    choices_rows = _read_rows_by_header(path, "choices", shared_strings)
    settings_rows = _read_rows_by_header(path, "settings", shared_strings)

    _ensure_columns(survey_rows, REQUIRED_SURVEY_COLUMNS, "survey")
    _ensure_columns(choices_rows, REQUIRED_CHOICES_COLUMNS, "choices")
    _ensure_columns(settings_rows, REQUIRED_SETTINGS_COLUMNS, "settings")

    questions = _parse_questions(survey_rows)
    choices = _parse_choices(choices_rows)
    _validate_choice_references(questions, choices)
    settings = _parse_settings(settings_rows)

    return ParsedXLSForm(
        source_path=path,
        questions=questions,
        choices=choices,
        settings=settings,
    )


def _sqlite_identifier(name: str, fallback: str = "field") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = fallback
    if cleaned[0].isdigit():
        cleaned = f"f_{cleaned}"
    return cleaned[:MAX_IDENTIFIER_LEN]


def _map_sql_type(question: Question) -> str | None:
    base = _question_base_type(question.question_type)
    if base in GEOMETRY_TYPE_MAP:
        return None
    if base in {"integer"}:
        return "INTEGER"
    if base in {"decimal"}:
        return "REAL"
    if base in {
        "text",
        "note",
        "select_one",
        "select_multiple",
        "date",
        "time",
        "dateTime",
        "image",
        "audio",
        "video",
        "start",
        "end",
        "deviceid",
        "today",
        "calculate",
    }:
        return "TEXT"
    return "TEXT"


def _all_repeat_paths(questions: list[Question]) -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = {()}
    for q in questions:
        for i in range(1, len(q.repeat_path) + 1):
            paths.add(q.repeat_path[:i])
    return paths


def build_layer_schemas(parsed: ParsedXLSForm) -> list[LayerSchema]:
    form_id = parsed.settings.get("form_id", "xlsform_data")

    repeat_paths = sorted(_all_repeat_paths(parsed.questions), key=lambda p: (len(p), p))
    table_by_path: dict[tuple[str, ...], str] = {}

    for path in repeat_paths:
        if not path:
            table_name = _sqlite_identifier(form_id, fallback="xlsform_data")
        else:
            suffix = "__".join(path)
            table_name = _sqlite_identifier(f"{form_id}__{suffix}", fallback="repeat")

        base_name = table_name
        n = 2
        while table_name in table_by_path.values():
            table_name = _sqlite_identifier(f"{base_name}_{n}")
            n += 1
        table_by_path[path] = table_name

    schemas: list[LayerSchema] = []
    for path in repeat_paths:
        table_name = table_by_path[path]
        parent_table_name = table_by_path[path[:-1]] if path else None
        questions = [q for q in parsed.questions if q.repeat_path == path]

        used_column_names = {"fid"}
        fields: list[FieldSpec] = []

        if path:
            fields.append(FieldSpec(name="parent_fid", sql_type="INTEGER", source_question=None))
            fields.append(FieldSpec(name="repeat_index", sql_type="INTEGER", source_question=None))
            used_column_names.update({"parent_fid", "repeat_index"})

        geom_questions = [q for q in questions if _question_base_type(q.question_type) in GEOMETRY_TYPE_MAP]
        geometry_fields: list[GeometrySpec] = []
        geometry_question_names: set[str] = set()
        for gq in geom_questions:
            geom_col = _sqlite_identifier(gq.name, fallback="geom")
            if geom_col in used_column_names:
                geom_col = _sqlite_identifier(f"{geom_col}_geom", fallback="geom")
            used_column_names.add(geom_col)
            geometry_question_names.add(gq.name)
            geometry_fields.append(
                GeometrySpec(
                    column_name=geom_col,
                    geometry_type_name=GEOMETRY_TYPE_MAP[_question_base_type(gq.question_type)],
                    source_question=gq,
                )
            )

        for question in questions:
            if question.name in geometry_question_names:
                continue

            sql_type = _map_sql_type(question)
            if sql_type is None:
                continue

            col_name = _sqlite_identifier(question.name, fallback="field")
            if col_name in {"id", "fid"}:
                col_name = "field_id"

            base_name = col_name
            n = 2
            while col_name in used_column_names:
                col_name = _sqlite_identifier(f"{base_name}_{n}")
                n += 1

            used_column_names.add(col_name)
            fields.append(FieldSpec(name=col_name, sql_type=sql_type, source_question=question))

        schemas.append(
            LayerSchema(
                table_name=table_name,
                repeat_path=path,
                parent_table_name=parent_table_name,
                fields=fields,
                geometry_fields=geometry_fields,
            )
        )

    return schemas


def build_choice_lookup_tables(parsed: ParsedXLSForm, schemas: list[LayerSchema]) -> dict[str, str]:
    """Build lookup table names for select_multiple lists."""
    existing = {schema.table_name for schema in schemas}
    table_by_list: dict[str, str] = {}

    for q in parsed.questions:
        if _question_base_type(q.question_type) != "select_multiple":
            continue
        if not q.list_name:
            continue
        if q.list_name in table_by_list:
            continue

        candidate = _sqlite_identifier(f"vl_{q.list_name}", fallback="vl_choices")
        base = candidate
        n = 2
        while candidate in existing:
            candidate = _sqlite_identifier(f"{base}_{n}", fallback="vl_choices")
            n += 1
        existing.add(candidate)
        table_by_list[q.list_name] = candidate

    return table_by_list


def _create_gpkg_core_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (
            srs_name TEXT NOT NULL,
            srs_id INTEGER NOT NULL PRIMARY KEY,
            organization TEXT NOT NULL,
            organization_coordsys_id INTEGER NOT NULL,
            definition TEXT NOT NULL,
            description TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gpkg_contents (
            table_name TEXT NOT NULL PRIMARY KEY,
            data_type TEXT NOT NULL,
            identifier TEXT UNIQUE,
            description TEXT DEFAULT '',
            last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            min_x DOUBLE,
            min_y DOUBLE,
            max_x DOUBLE,
            max_y DOUBLE,
            srs_id INTEGER,
            CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gpkg_geometry_columns (
            table_name TEXT NOT NULL,
            column_name TEXT NOT NULL,
            geometry_type_name TEXT NOT NULL,
            srs_id INTEGER NOT NULL,
            z TINYINT NOT NULL,
            m TINYINT NOT NULL,
            PRIMARY KEY (table_name, column_name),
            CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
            CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
        )
        """
    )

    conn.execute(
        """
        INSERT OR REPLACE INTO gpkg_spatial_ref_sys
        (srs_name, srs_id, organization, organization_coordsys_id, definition, description)
        VALUES
        ('WGS 84 geodetic', 4326, 'EPSG', 4326,
         'GEOGCS["WGS 84",DATUM["World Geodetic System 1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
         'longitude/latitude coordinates in decimal degrees on the WGS 84 spheroid'),
        ('Undefined geographic SRS', 0, 'NONE', 0, 'undefined', 'undefined geographic coordinate reference system'),
        ('Undefined cartesian SRS', -1, 'NONE', -1, 'undefined', 'undefined cartesian coordinate reference system')
        """
    )


def _create_metadata_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS xlsform_field_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            column_name TEXT NOT NULL,
            question_name TEXT,
            question_type TEXT,
            label TEXT,
            hint TEXT,
            required INTEGER NOT NULL DEFAULT 0,
            relevant TEXT,
            calculation TEXT,
            repeat_path TEXT,
            list_name TEXT
        )
        """
    )


def _populate_metadata_table(conn: sqlite3.Connection, schemas: list[LayerSchema]) -> None:
    for schema in schemas:
        repeat_path_text = "/".join(schema.repeat_path)

        for field in schema.fields:
            q = field.source_question
            if q is None:
                continue
            conn.execute(
                """
                INSERT INTO xlsform_field_metadata
                (table_name, column_name, question_name, question_type, label, hint, required,
                 relevant, calculation, repeat_path, list_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schema.table_name,
                    field.name,
                    q.name,
                    q.question_type,
                    q.label,
                    q.hint,
                    1 if q.required else 0,
                    q.relevant,
                    q.calculation,
                    repeat_path_text,
                    q.list_name,
                ),
            )

        for geom in schema.geometry_fields:
            q = geom.source_question
            conn.execute(
                """
                INSERT INTO xlsform_field_metadata
                (table_name, column_name, question_name, question_type, label, hint, required,
                 relevant, calculation, repeat_path, list_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schema.table_name,
                    geom.column_name,
                    q.name,
                    q.question_type,
                    q.label,
                    q.hint,
                    1 if q.required else 0,
                    q.relevant,
                    q.calculation,
                    repeat_path_text,
                    q.list_name,
                ),
            )


def write_gpkg_schema(
    parsed: ParsedXLSForm,
    schemas: list[LayerSchema],
    choice_lookup_tables: dict[str, str],
    output_path: Path,
    overwrite: bool = False,
) -> None:
    if output_path.exists():
        if not overwrite:
            raise XLSFormValidationError(
                f"Output already exists: {output_path}. Use --overwrite to replace it."
            )
        output_path.unlink()

    conn = sqlite3.connect(output_path)
    try:
        conn.execute("PRAGMA application_id = 1196444487")
        conn.execute("PRAGMA user_version = 10300")
        _create_gpkg_core_tables(conn)
        _create_metadata_table(conn)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        for schema in schemas:
            column_sql = ['"fid" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL']
            for field in schema.fields:
                column_sql.append(f'"{field.name}" {field.sql_type}')
            for geom in schema.geometry_fields:
                column_sql.append(f'"{geom.column_name}" BLOB')

            conn.execute(f'CREATE TABLE "{schema.table_name}" ({", ".join(column_sql)})')

            data_type = "features" if schema.geometry_fields else "attributes"
            srs_id = 4326 if schema.geometry_fields else 0
            conn.execute(
                """
                INSERT INTO gpkg_contents
                (table_name, data_type, identifier, description, last_change, srs_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    schema.table_name,
                    data_type,
                    schema.table_name,
                    parsed.settings.get("form_title", "XLSForm layer"),
                    now,
                    srs_id,
                ),
            )

            for geom in schema.geometry_fields:
                conn.execute(
                    """
                    INSERT INTO gpkg_geometry_columns
                    (table_name, column_name, geometry_type_name, srs_id, z, m)
                    VALUES (?, ?, ?, 4326, 0, 0)
                    """,
                    (
                        schema.table_name,
                        geom.column_name,
                        geom.geometry_type_name,
                    ),
                )

            if schema.parent_table_name:
                conn.execute(
                    f'CREATE INDEX "idx_{schema.table_name}_parent_fid" '
                    f'ON "{schema.table_name}" ("parent_fid")'
                )

        for list_name, lookup_table in choice_lookup_tables.items():
            conn.execute(
                f"""
                CREATE TABLE "{lookup_table}" (
                    code TEXT PRIMARY KEY NOT NULL,
                    label TEXT NOT NULL
                )
                """
            )
            for code, label in parsed.choices.get(list_name, {}).items():
                conn.execute(
                    f'INSERT INTO "{lookup_table}" (code, label) VALUES (?, ?)',
                    (code, label),
                )
            conn.execute(
                """
                INSERT INTO gpkg_contents
                (table_name, data_type, identifier, description, last_change, srs_id)
                VALUES (?, 'attributes', ?, ?, ?, 0)
                """,
                (
                    lookup_table,
                    lookup_table,
                    f"Lookup values for {list_name}",
                    now,
                ),
            )

        _populate_metadata_table(conn, schemas)
        conn.commit()
    finally:
        conn.close()


def _stable_layer_id(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]


def _qgs_layer_tree_layer(
    parent: ET.Element,
    layer_id: str,
    name: str,
    provider: str,
    source: str,
) -> None:
    ET.SubElement(
        parent,
        "layer-tree-layer",
        {
            "id": layer_id,
            "name": name,
            "expanded": "1",
            "checked": "Qt::Checked",
            "providerKey": provider,
            "source": source,
        },
    )


def _is_select_multiple(question: Question | None) -> bool:
    if question is None:
        return False
    return _question_base_type(question.question_type) == "select_multiple"


def _is_image(question: Question | None) -> bool:
    if question is None:
        return False
    return _question_base_type(question.question_type) == "image"


def _temporal_kind(question: Question | None) -> str | None:
    if question is None:
        return None
    base = _question_base_type(question.question_type)
    if base in {"date", "time", "dateTime"}:
        return base
    return None


def _datetime_widget_config(kind: str) -> dict[str, object]:
    if kind == "date":
        return {
            "allow_null": True,
            "calendar_popup": True,
            "display_format": "yyyy-MM-dd",
            "field_format": "yyyy-MM-dd",
            "field_iso_format": False,
        }
    if kind == "time":
        return {
            "allow_null": True,
            "calendar_popup": False,
            "display_format": "HH:mm:ss",
            "field_format": "HH:mm:ss",
            "field_iso_format": False,
        }
    return {
        "allow_null": True,
        "calendar_popup": True,
        "display_format": "yyyy-MM-dd HH:mm:ss",
        "field_format": "yyyy-MM-dd HH:mm:ss",
        "field_iso_format": False,
    }


def _translate_relevant_expression(
    relevant: str | None,
    question_to_field: dict[str, str],
) -> str | None:
    """Translate a simple XLSForm relevant expression to a QGIS expression."""
    if not relevant:
        return None

    expr = relevant.strip()
    if not expr:
        return None

    def repl(match: re.Match[str]) -> str:
        qname = match.group(1)
        field_name = question_to_field.get(qname, qname)
        return f'"{field_name}"'

    expr = re.sub(r"\$\{([A-Za-z0-9_]+)\}", repl, expr)
    expr = expr.replace(" and ", " AND ").replace(" or ", " OR ")
    return expr


def _append_datetime_xml_config(option_root: ET.Element, kind: str) -> None:
    cfg = _datetime_widget_config(kind)
    for key, value in cfg.items():
        if isinstance(value, bool):
            ET.SubElement(
                option_root,
                "Option",
                {"name": key, "type": "bool", "value": "true" if value else "false"},
            )
        else:
            ET.SubElement(
                option_root,
                "Option",
                {"name": key, "type": "QString", "value": str(value)},
            )


def _external_resource_widget_config() -> dict[str, object]:
    return {
        "DocumentViewer": 1,
        "DocumentViewerHeight": 0,
        "DocumentViewerWidth": 0,
        "FileWidget": True,
        "FileWidgetButton": True,
        "FileWidgetFilter": "Images (*.png *.jpg *.jpeg *.bmp);;All files (*.*)",
        "PropertyCollection": {"name": "collection", "properties": {}, "type": "collection"},
        "RelativeStorage": 1,
        "StorageAuthConfigId": "",
        "StorageMode": 0,
        "StorageType": 0,
        "UseLink": False,
        "DefaultRoot": "",
    }


def _append_external_resource_xml_config(option_root: ET.Element) -> None:
    cfg = _external_resource_widget_config()
    for key, value in cfg.items():
        if isinstance(value, bool):
            ET.SubElement(
                option_root,
                "Option",
                {"name": key, "type": "bool", "value": "true" if value else "false"},
            )
        elif isinstance(value, int):
            ET.SubElement(
                option_root,
                "Option",
                {"name": key, "type": "int", "value": str(value)},
            )
        elif isinstance(value, dict):
            if key == "PropertyCollection":
                coll = ET.SubElement(option_root, "Option", {"name": key, "type": "Map"})
                ET.SubElement(coll, "Option", {"name": "name", "type": "QString", "value": "collection"})
                ET.SubElement(coll, "Option", {"name": "properties"})
                ET.SubElement(coll, "Option", {"name": "type", "type": "QString", "value": "collection"})
            else:
                ET.SubElement(option_root, "Option", {"name": key, "type": "Map"})
        else:
            ET.SubElement(
                option_root,
                "Option",
                {"name": key, "type": "QString", "value": str(value)},
            )


def _append_field_configuration(
    maplayer: ET.Element,
    schema: LayerSchema,
    choices: dict[str, dict[str, str]],
) -> None:
    field_config = ET.SubElement(maplayer, "fieldConfiguration")
    aliases = ET.SubElement(maplayer, "aliases")

    for idx, field in enumerate(schema.fields):
        question = field.source_question
        label = question.label if question and question.label else field.name
        ET.SubElement(aliases, "alias", {"field": field.name, "index": str(idx), "name": label})

        field_el = ET.SubElement(field_config, "field", {"name": field.name, "configurationFlags": "NoFlag"})
        widget_type = "TextEdit"
        temporal_kind = _temporal_kind(question)

        if _is_image(question):
            widget_type = "ExternalResource"
        elif temporal_kind:
            widget_type = "DateTime"
        elif question and question.list_name and question.list_name in choices:
            widget_type = "ValueMap"

        edit_widget = ET.SubElement(field_el, "editWidget", {"type": widget_type})
        config = ET.SubElement(edit_widget, "config")
        option_root = ET.SubElement(config, "Option", {"type": "Map"})

        if widget_type == "ValueMap" and question and question.list_name:
            value_map = ET.SubElement(option_root, "Option", {"name": "map", "type": "List"})
            for code, choice_label in choices[question.list_name].items():
                entry = ET.SubElement(value_map, "Option", {"type": "Map"})
                ET.SubElement(
                    entry,
                    "Option",
                    {
                        "name": choice_label,
                        "type": "QString",
                        "value": code,
                    },
                )
            if _is_select_multiple(question):
                ET.SubElement(
                    option_root,
                    "Option",
                    {
                        "name": "AllowMulti",
                        "type": "bool",
                        "value": "true",
                    },
                )
        elif widget_type == "ExternalResource":
            _append_external_resource_xml_config(option_root)
        elif widget_type == "DateTime" and temporal_kind:
            _append_datetime_xml_config(option_root, temporal_kind)


def _qgs_vector_maplayer(
    parent: ET.Element,
    layer_id: str,
    name: str,
    datasource: str,
    geometry: str,
    schema: LayerSchema,
    choices: dict[str, dict[str, str]],
) -> None:
    maplayer = ET.SubElement(parent, "maplayer", {"type": "vector", "geometry": geometry})
    ET.SubElement(maplayer, "id").text = layer_id
    ET.SubElement(maplayer, "layername").text = name
    ET.SubElement(maplayer, "datasource").text = datasource
    ET.SubElement(maplayer, "provider").text = "ogr"
    _append_field_configuration(maplayer, schema, choices)
    _append_srs(
        maplayer,
        srs_values=SRS_4326,
    )


def _append_srs(
    parent: ET.Element,
    srs_values: dict[str, str],
) -> None:
    srs = ET.SubElement(parent, "srs")
    spatial_ref = ET.SubElement(srs, "spatialrefsys", {"nativeFormat": "Wkt"})
    ET.SubElement(spatial_ref, "wkt").text = srs_values["wkt"]
    ET.SubElement(spatial_ref, "proj4").text = srs_values["proj4"]
    ET.SubElement(spatial_ref, "srsid").text = srs_values["srsid"]
    ET.SubElement(spatial_ref, "srid").text = srs_values["srid"]
    ET.SubElement(spatial_ref, "authid").text = srs_values["authid"]
    ET.SubElement(spatial_ref, "description").text = srs_values["description"]
    ET.SubElement(spatial_ref, "projectionacronym").text = srs_values["projectionacronym"]
    ET.SubElement(spatial_ref, "ellipsoidacronym").text = srs_values["ellipsoidacronym"]
    ET.SubElement(spatial_ref, "geographicflag").text = srs_values["geographicflag"]


def _qgs_raster_xyz_maplayer(parent: ET.Element, layer_id: str) -> None:
    maplayer = ET.SubElement(parent, "maplayer", {"type": "raster"})
    ET.SubElement(maplayer, "id").text = layer_id
    ET.SubElement(maplayer, "layername").text = "OpenStreetMap"
    ET.SubElement(maplayer, "datasource").text = (
        "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0&crs=EPSG:3785"
    )
    ET.SubElement(maplayer, "provider").text = "wms"
    _append_srs(
        maplayer,
        srs_values=SRS_3785,
    )


def _write_qgis_project_pyqgis(
    project_path: Path,
    gpkg_path: Path,
    schemas: list[LayerSchema],
    choices: dict[str, dict[str, str]],
    choice_lookup_tables: dict[str, str],
    project_title: str,
) -> bool:
    try:
        from qgis.core import (
            QgsApplication,
            QgsAttributeEditorContainer,
            QgsAttributeEditorField,
            QgsCoordinateReferenceSystem,
            QgsEditFormConfig,
            QgsEditorWidgetSetup,
            QgsExpression,
            QgsOptionalExpression,
            QgsProject,
            QgsRasterLayer,
            QgsVectorLayer,
        )
    except Exception:
        return False

    app = QgsApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QgsApplication([], False)
        app.initQgis()

    try:
        project = QgsProject.instance()
        project.clear()
        project.setTitle(project_title)
        project.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))

        root = project.layerTreeRoot()
        root.removeAllChildren()

        gpkg_uri = f"./{gpkg_path.name}"
        value_relation_layers: dict[str, str] = {}
        for list_name, lookup_table in choice_lookup_tables.items():
            lookup_uri = f"{gpkg_uri}|layername={lookup_table}"
            lookup_layer = QgsVectorLayer(lookup_uri, f"lookup__{list_name}", "ogr")
            if not lookup_layer.isValid():
                return False
            project.addMapLayer(lookup_layer, False)
            root.addLayer(lookup_layer)
            value_relation_layers[list_name] = lookup_layer.id()

        for schema in schemas:
            if schema.geometry_fields:
                # GeoPackage layers load reliably without a geometryname URI fragment.
                layer_name = f"{schema.table_name}__{schema.geometry_fields[0].column_name}"
            else:
                layer_name = schema.table_name

            uri = f"{gpkg_uri}|layername={schema.table_name}"
            layer = QgsVectorLayer(uri, layer_name, "ogr")
            if not layer.isValid():
                return False

            for field in schema.fields:
                idx = layer.fields().indexFromName(field.name)
                if idx < 0:
                    continue
                q = field.source_question
                if q and q.label:
                    layer.setFieldAlias(idx, q.label)
                if q and q.list_name and q.list_name in choices:
                    if _is_select_multiple(q) and q.list_name in value_relation_layers:
                        lookup_layer_id = value_relation_layers[q.list_name]
                        lookup_layer = project.mapLayer(lookup_layer_id)
                        layer_source = lookup_layer.source() if lookup_layer is not None else ""
                        layer_name = lookup_layer.name() if lookup_layer is not None else ""
                        layer_provider = lookup_layer.providerType() if lookup_layer is not None else "ogr"
                        layer.setEditorWidgetSetup(
                            idx,
                            QgsEditorWidgetSetup(
                                "ValueRelation",
                                {
                                    "AllowMulti": True,
                                    "AllowNull": True,
                                    "FilterExpression": "",
                                    "Key": "code",
                                    "Layer": lookup_layer_id,
                                    "LayerName": layer_name,
                                    "LayerSource": layer_source,
                                    "LayerProviderName": layer_provider,
                                    "NofColumns": 1,
                                    "OrderByValue": False,
                                    "UseCompleter": False,
                                    "Value": "label",
                                },
                            ),
                        )
                    else:
                        label_to_code = {label: code for code, label in choices[q.list_name].items()}
                        layer.setEditorWidgetSetup(
                            idx,
                            QgsEditorWidgetSetup("ValueMap", {"map": label_to_code}),
                        )
                elif _is_image(q):
                    layer.setEditorWidgetSetup(
                        idx,
                        QgsEditorWidgetSetup("ExternalResource", _external_resource_widget_config()),
                    )
                elif _temporal_kind(q):
                    layer.setEditorWidgetSetup(
                        idx,
                        QgsEditorWidgetSetup("DateTime", _datetime_widget_config(_temporal_kind(q) or "dateTime")),
                    )

            # Build explicit form layout so we can apply per-field visibility from XLSForm relevant.
            ef = layer.editFormConfig()
            ef.setLayout(QgsEditFormConfig.TabLayout)
            root_container = ef.invisibleRootContainer()
            root_container.clear()
            main_container = QgsAttributeEditorContainer("Main", root_container)
            root_container.addChildElement(main_container)

            question_to_field: dict[str, str] = {}
            for field in schema.fields:
                q = field.source_question
                if q:
                    question_to_field[q.name] = field.name

            for field in schema.fields:
                idx = layer.fields().indexFromName(field.name)
                if idx < 0:
                    continue
                q = field.source_question
                field_container = QgsAttributeEditorContainer(field.name, main_container)
                qgs_relevant = _translate_relevant_expression(q.relevant if q else None, question_to_field)
                if qgs_relevant:
                    field_container.setVisibilityExpression(
                        QgsOptionalExpression(QgsExpression(qgs_relevant), True)
                    )
                field_container.addChildElement(
                    QgsAttributeEditorField(field.name, idx, field_container)
                )
                main_container.addChildElement(field_container)

            layer.setEditFormConfig(ef)

            project.addMapLayer(layer, False)
            root.addLayer(layer)

        osm = QgsRasterLayer(
            "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0&crs=EPSG:3785",
            "OpenStreetMap",
            "wms",
        )
        if not osm.isValid():
            return False
        osm.setCrs(QgsCoordinateReferenceSystem("EPSG:3785"))
        project.addMapLayer(osm, False)
        root.addLayer(osm)

        return project.write(str(project_path))
    finally:
        if owns_app:
            app.exitQgis()


def _normalize_qgs_value_maps(
    project_path: Path,
    schemas: list[LayerSchema],
    choices: dict[str, dict[str, str]],
) -> None:
    """Rewrite ValueMap config into list form for better mobile compatibility."""
    table_field_choices: dict[str, dict[str, tuple[dict[str, str], bool]]] = {}
    for schema in schemas:
        per_field: dict[str, tuple[dict[str, str], bool]] = {}
        for field in schema.fields:
            q = field.source_question
            if q and q.list_name and q.list_name in choices and not _is_select_multiple(q):
                per_field[field.name] = (choices[q.list_name], _is_select_multiple(q))
        table_field_choices[schema.table_name] = per_field

    if not any(table_field_choices.values()):
        return

    tree = ET.parse(project_path)
    root = tree.getroot()

    for maplayer in root.findall("./projectlayers/maplayer"):
        if maplayer.attrib.get("type") != "vector":
            continue
        datasource = maplayer.findtext("datasource", default="")
        table_name = None
        if "|layername=" in datasource:
            table_name = datasource.split("|layername=", 1)[1].split("|", 1)[0]
        if not table_name or table_name not in table_field_choices:
            continue

        field_choices = table_field_choices[table_name]
        if not field_choices:
            continue

        field_cfg = maplayer.find("fieldConfiguration")
        if field_cfg is None:
            continue

        for field_el in field_cfg.findall("field"):
            field_name = field_el.attrib.get("name", "")
            if field_name not in field_choices:
                continue

            edit_widget = field_el.find("editWidget")
            if edit_widget is None:
                edit_widget = ET.SubElement(field_el, "editWidget")
            edit_widget.set("type", "ValueMap")

            config = edit_widget.find("config")
            if config is None:
                config = ET.SubElement(edit_widget, "config")
            for child in list(config):
                config.remove(child)

            option_root = ET.SubElement(config, "Option", {"type": "Map"})
            value_map = ET.SubElement(option_root, "Option", {"name": "map", "type": "List"})
            choices_map, is_multi = field_choices[field_name]
            for code, label in choices_map.items():
                entry = ET.SubElement(value_map, "Option", {"type": "Map"})
                ET.SubElement(
                    entry,
                    "Option",
                    {
                        "name": label,
                        "type": "QString",
                        "value": code,
                    },
                )
            if is_multi:
                ET.SubElement(
                    option_root,
                    "Option",
                    {
                        "name": "AllowMulti",
                        "type": "bool",
                        "value": "true",
                    },
                )

    ET.indent(tree, space="  ")
    tree.write(project_path, encoding="UTF-8", xml_declaration=True)


def write_qgis_project(
    project_path: Path,
    gpkg_path: Path,
    schemas: list[LayerSchema],
    choices: dict[str, dict[str, str]],
    choice_lookup_tables: dict[str, str],
    project_title: str,
    overwrite: bool = False,
) -> None:
    if project_path.exists() and not overwrite:
        raise XLSFormValidationError(
            f"Project file already exists: {project_path}. Use --overwrite to replace it."
        )

    if _write_qgis_project_pyqgis(
        project_path=project_path,
        gpkg_path=gpkg_path,
        schemas=schemas,
        choices=choices,
        choice_lookup_tables=choice_lookup_tables,
        project_title=project_title,
    ):
        _normalize_qgs_value_maps(project_path, schemas, choices)
        return

    relative_gpkg = f"./{gpkg_path.name}"

    qgis = ET.Element("qgis", {"version": "3.34.0", "projectname": project_title})
    ET.SubElement(qgis, "title").text = project_title
    project_crs = ET.SubElement(qgis, "projectCrs")
    spatial_ref = ET.SubElement(project_crs, "spatialrefsys", {"nativeFormat": "Wkt"})
    ET.SubElement(spatial_ref, "wkt").text = SRS_4326["wkt"]
    ET.SubElement(spatial_ref, "proj4").text = SRS_4326["proj4"]
    ET.SubElement(spatial_ref, "srsid").text = SRS_4326["srsid"]
    ET.SubElement(spatial_ref, "srid").text = SRS_4326["srid"]
    ET.SubElement(spatial_ref, "authid").text = SRS_4326["authid"]
    ET.SubElement(spatial_ref, "description").text = SRS_4326["description"]
    ET.SubElement(spatial_ref, "projectionacronym").text = SRS_4326["projectionacronym"]
    ET.SubElement(spatial_ref, "ellipsoidacronym").text = SRS_4326["ellipsoidacronym"]
    ET.SubElement(spatial_ref, "geographicflag").text = SRS_4326["geographicflag"]

    mapcanvas = ET.SubElement(qgis, "mapcanvas")
    ET.SubElement(mapcanvas, "units").text = "degrees"
    ET.SubElement(mapcanvas, "projections").text = "1"
    destination_srs = ET.SubElement(mapcanvas, "destinationsrs")
    canvas_spatial_ref = ET.SubElement(destination_srs, "spatialrefsys", {"nativeFormat": "Wkt"})
    ET.SubElement(canvas_spatial_ref, "wkt").text = SRS_4326["wkt"]
    ET.SubElement(canvas_spatial_ref, "proj4").text = SRS_4326["proj4"]
    ET.SubElement(canvas_spatial_ref, "srsid").text = SRS_4326["srsid"]
    ET.SubElement(canvas_spatial_ref, "srid").text = SRS_4326["srid"]
    ET.SubElement(canvas_spatial_ref, "authid").text = SRS_4326["authid"]
    ET.SubElement(canvas_spatial_ref, "description").text = SRS_4326["description"]
    ET.SubElement(canvas_spatial_ref, "projectionacronym").text = SRS_4326["projectionacronym"]
    ET.SubElement(canvas_spatial_ref, "ellipsoidacronym").text = SRS_4326["ellipsoidacronym"]
    ET.SubElement(canvas_spatial_ref, "geographicflag").text = SRS_4326["geographicflag"]

    project_layers = ET.SubElement(qgis, "projectlayers")
    layer_tree_root = ET.SubElement(qgis, "layer-tree-group", {"name": "", "expanded": "1", "checked": "Qt::Checked"})

    # Put data layers first in layer tree so OSM stays at the bottom.
    for schema in schemas:
        if schema.geometry_fields:
            for geom in schema.geometry_fields:
                layer_name = f"{schema.table_name}__{geom.column_name}"
                layer_id = _stable_layer_id(f"{layer_name}|ogr")
                datasource = f"{relative_gpkg}|layername={schema.table_name}"
                _qgs_vector_maplayer(
                    project_layers,
                    layer_id,
                    layer_name,
                    datasource,
                    geom.geometry_type_name.title(),
                    schema,
                    choices,
                )
                _qgs_layer_tree_layer(
                    layer_tree_root,
                    layer_id,
                    layer_name,
                    "ogr",
                    datasource,
                )
        else:
            layer_id = _stable_layer_id(f"{schema.table_name}|ogr")
            datasource = f"{relative_gpkg}|layername={schema.table_name}"
            _qgs_vector_maplayer(
                project_layers,
                layer_id,
                schema.table_name,
                datasource,
                "NoGeometry",
                schema,
                choices,
            )
            _qgs_layer_tree_layer(
                layer_tree_root,
                layer_id,
                schema.table_name,
                "ogr",
                datasource,
            )

    osm_layer_id = _stable_layer_id("OpenStreetMap|xyz")
    _qgs_raster_xyz_maplayer(project_layers, osm_layer_id)
    _qgs_layer_tree_layer(
        layer_tree_root,
        osm_layer_id,
        "OpenStreetMap",
        "wms",
        "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0&crs=EPSG:3785",
    )

    tree = ET.ElementTree(qgis)
    ET.indent(tree, space="  ")
    tree.write(project_path, encoding="UTF-8", xml_declaration=True)
    _normalize_qgs_value_maps(project_path, schemas, choices)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create schema-only GeoPackage and QGIS project from XLSForm."
    )
    parser.add_argument("input_xlsx", type=Path, help="Path to XLSForm .xlsx")
    parser.add_argument("output_gpkg", type=Path, help="Path to target GeoPackage output")
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help="Optional output .qgs path. Defaults to output_gpkg with .qgs extension.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite output GeoPackage/project if they already exist.",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    try:
        parsed = load_and_validate_xlsform(args.input_xlsx)
        schemas = build_layer_schemas(parsed)
        choice_lookup_tables = build_choice_lookup_tables(parsed, schemas)
        write_gpkg_schema(
            parsed,
            schemas,
            choice_lookup_tables,
            args.output_gpkg,
            overwrite=args.overwrite,
        )

        project_path = args.project if args.project else args.output_gpkg.with_suffix(".qgs")
        write_qgis_project(
            project_path=project_path,
            gpkg_path=args.output_gpkg,
            schemas=schemas,
            choices=parsed.choices,
            choice_lookup_tables=choice_lookup_tables,
            project_title=parsed.settings.get("form_title", parsed.settings.get("form_id", "XLSForm Project")),
            overwrite=args.overwrite,
        )

    except XLSFormValidationError as exc:
        print(f"Validation error: {exc}")
        return 2

    print(f"Loaded XLSForm: {parsed.source_path}")
    print(f"Layer tables created: {len(schemas)}")
    print(f"GeoPackage created: {args.output_gpkg}")
    print(f"QGIS project created: {project_path}")
    for schema in schemas:
        path_text = "/".join(schema.repeat_path) if schema.repeat_path else "main"
        geom_text = ",".join(g.geometry_type_name for g in schema.geometry_fields) if schema.geometry_fields else "none"
        print(
            f" - {schema.table_name} (path={path_text}, fields={len(schema.fields)}, geometry={geom_text})"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
