# XLSForm to GeoPackage + QGIS Project

Convert an XLSForm schema into:

- a GeoPackage (`.gpkg`) with layer/table structure
- a QGIS project (`.qgs`) configured for Mergin Maps

The converter is schema-only. It does not import submissions.

## Features

- Parses XLSForm `survey`, `choices`, and `settings` sheets
- Creates GeoPackage layers from top-level + repeat groups
- Maps geometry types:
  - `geopoint` -> `POINT`
  - `geotrace` -> `LINESTRING`
  - `geoshape` -> `POLYGON`
- Builds QGIS project with:
  - project CRS `EPSG:4326`
  - OSM XYZ basemap (configured as `EPSG:3785`)
  - picklists (`ValueMap`) from XLSForm `choices`
- Includes a PyQGIS validator script for project integrity checks

## Files

- `xlsform_to_gpkg.py`: main converter
- `validate_project_pyqgis.py`: validate `.qgs/.qgz` with PyQGIS
- `requirements.txt`: Python dependencies (main script currently stdlib-only)

## Quick Start

```bash
python xlsform_to_gpkg.py <input.xlsx> <output.gpkg> --overwrite
```

This writes:

- `<output.gpkg>`
- `<output.qgs>` (same base name)

## Usage Examples

### 1) Generate project from sample XLSForm

```bash
python xlsform_to_gpkg.py street_sign_points_sample.xlsx street_sign_points_project.gpkg --overwrite
```

### 2) Specify output project path

```bash
python xlsform_to_gpkg.py street_sign_points_sample.xlsx street_sign_points_project.gpkg --project my_project.qgs --overwrite
```

### 3) Validate generated project with PyQGIS

```bash
python validate_project_pyqgis.py street_sign_points_project.qgs
```

Optional explicit expectations:

```bash
python validate_project_pyqgis.py street_sign_points_project.qgs \
  --expect-project-crs EPSG:4326 \
  --expect-osm-crs EPSG:3785
```

## Environment Notes

- The converter can run without PyQGIS.
- PyQGIS is only needed for `validate_project_pyqgis.py`.
- In some environments (for example snap-injected shells), use a clean environment for headless PyQGIS:

```bash
env -i HOME="$HOME" USER="$USER" PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  QT_QPA_PLATFORM=offscreen \
  .venv/bin/python validate_project_pyqgis.py street_sign_points_project.qgs
```

## Output Overview

GeoPackage output includes:

- `gpkg_spatial_ref_sys`
- `gpkg_contents`
- `gpkg_geometry_columns`
- generated feature/attribute tables
- `xlsform_field_metadata` (question metadata)

## Troubleshooting

- `Validation error: ...`:
  - check required sheets (`survey`, `choices`, `settings`)
  - check required columns (`type`, `name`, etc.)
- Empty picklists in Mergin Maps:
  - regenerate project with latest script so ValueMap config is refreshed
- Disconnected layer in QGIS:
  - ensure `.qgs` and `.gpkg` stay together in the same folder

