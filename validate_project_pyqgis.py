#!/usr/bin/env python3
"""Validate QGIS project datasources and CRS using PyQGIS.

Run this script with a Python interpreter that has PyQGIS available,
for example via `qgis_process`, OSGeo shell, or a QGIS-provided python.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate .qgs/.qgz project datasource links and CRS via PyQGIS"
    )
    parser.add_argument("project", type=Path, help="Path to .qgs or .qgz project")
    parser.add_argument(
        "--expect-project-crs",
        default="EPSG:4326",
        help="Expected project CRS authid (default: EPSG:4326)",
    )
    parser.add_argument(
        "--expect-osm-crs",
        default="EPSG:3785",
        help="Expected OpenStreetMap layer CRS authid (default: EPSG:3785)",
    )
    parser.add_argument(
        "--expect-osm-name",
        default="OpenStreetMap",
        help="Expected OSM layer name (default: OpenStreetMap)",
    )
    return parser


def _load_pyqgis_or_exit() -> tuple[object, object]:
    try:
        from qgis.core import QgsApplication, QgsProject
    except Exception as exc:  # pragma: no cover - environment dependent
        print("ERROR: PyQGIS is not available in this Python environment.")
        print(f"DETAIL: {exc}")
        print("Run this script with a QGIS Python environment.")
        raise SystemExit(2)

    return QgsApplication, QgsProject


def main() -> int:
    args = _build_parser().parse_args()

    if not args.project.exists():
        print(f"ERROR: Project file not found: {args.project}")
        return 2

    QgsApplication, QgsProject = _load_pyqgis_or_exit()

    qgs = QgsApplication([], False)
    qgs.initQgis()

    try:
        project = QgsProject.instance()
        ok = project.read(str(args.project))
        if not ok:
            print(f"ERROR: Failed to read project: {args.project}")
            return 1

        errors: list[str] = []

        project_crs = project.crs().authid() or ""
        if project_crs != args.expect_project_crs:
            errors.append(
                f"Project CRS mismatch: expected {args.expect_project_crs}, got {project_crs or '<empty>'}"
            )

        layers = list(project.mapLayers().values())
        if not layers:
            errors.append("Project has no layers.")

        osm_found = False

        for layer in layers:
            layer_name = layer.name()
            layer_id = layer.id()
            layer_valid = layer.isValid()
            provider = layer.providerType()
            layer_crs = layer.crs().authid() or ""
            datasource = layer.dataProvider().dataSourceUri() if layer.dataProvider() else ""

            if not layer_valid:
                errors.append(f"Layer invalid: {layer_name} ({layer_id})")

            if provider in {"ogr", "gdal"} and not datasource:
                errors.append(f"Layer datasource missing: {layer_name} ({provider})")

            if layer_name == args.expect_osm_name:
                osm_found = True
                if layer_crs != args.expect_osm_crs:
                    errors.append(
                        f"OSM CRS mismatch: expected {args.expect_osm_crs}, got {layer_crs or '<empty>'}"
                    )
                if "tile.openstreetmap.org" not in datasource:
                    errors.append("OSM datasource does not reference tile.openstreetmap.org")

        if not osm_found:
            errors.append(f"OSM layer not found: {args.expect_osm_name}")

        if errors:
            print("VALIDATION FAILED")
            for err in errors:
                print(f" - {err}")
            return 1

        print("VALIDATION PASSED")
        print(f"Project: {args.project}")
        print(f"Project CRS: {project_crs}")
        print(f"Layers checked: {len(layers)}")
        return 0

    finally:
        qgs.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
