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
- Supports temporal question types:
  - `date`
  - `time`
  - `dateTime`
- Supports conditional visibility via XLSForm `relevant`
- Builds QGIS project with:
  - project CRS `EPSG:4326`
  - OSM XYZ basemap (configured as `EPSG:3785`)
  - picklists (`ValueMap`) from XLSForm `choices`
  - multi-select picklists (`ValueRelation`) for `select_multiple`
  - photo capture widgets (`ExternalResource`) for XLSForm `image` fields
  - temporal input widgets (`DateTime`) for `date`, `time`, `dateTime`
  - field visibility rules from `relevant` (QGIS tab layout containers)
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

## Sample Forms and Outputs

The repository includes a set of progressively richer sample XLSForms.  
Each sample has an associated generated GeoPackage and QGIS project.

| Sample XLSForm | Purpose | Generated GeoPackage | Generated QGIS Project |
| --- | --- | --- | --- |
| `street_sign_points_sample.xlsx` | Baseline point survey (`geopoint`, `select_one`, `integer`) | `street_sign_points_project.gpkg` | `street_sign_points_project.qgs` |
| `street_sign_points_sample_select_multiple.xlsx` | Adds `select_multiple` support (multi-select via `ValueRelation`) | `street_sign_points_select_multiple_project.gpkg` | `street_sign_points_select_multiple_project.qgs` |
| `street_sign_points_sample_with_image.xlsx` | Adds photo capture (`image` -> `ExternalResource`) | `street_signs_photo_project.gpkg` | `street_signs_photo_project.qgs` |
| `street_sign_points_sample_temporal.xlsx` | Adds temporal fields (`date`, `time`, `dateTime`) | `street_sign_points_temporal_project.gpkg` | `street_sign_points_temporal_project.qgs` |
| `street_sign_points_sample_relevant.xlsx` | Adds conditional visibility using `relevant` | `street_sign_points_relevant_project.gpkg` | `street_sign_points_relevant_project.qgs` |

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

Photo fields:

- XLSForm `image` questions are exported as text attributes plus QGIS `ExternalResource` form widgets.
- In Mergin Maps, captured photos are stored as project attachments and the corresponding field stores the relative file path.

Temporal fields:

- `date`, `time`, and `dateTime` are stored as text attributes and configured with QGIS `DateTime` form widgets.
- Widget formats currently used:
  - `date` -> `yyyy-MM-dd`
  - `time` -> `HH:mm:ss`
  - `dateTime` -> `yyyy-MM-dd HH:mm:ss`

Relevant (conditional visibility):

- `relevant` expressions are translated to QGIS visibility expressions on form field containers.
- Currently supported expression patterns:
  - `${field} = 'value'`
  - `${field} != 'value'`
  - `${field} != ''`
  - combinations with `and` / `or`

## Troubleshooting

- `Validation error: ...`:
  - check required sheets (`survey`, `choices`, `settings`)
  - check required columns (`type`, `name`, etc.)
- Empty picklists in Mergin Maps:
  - regenerate project with latest script so ValueMap config is refreshed
- Multi-select list is empty in Mergin Maps:
  - ensure you regenerated with the latest script (uses `ValueRelation` + lookup layer for `select_multiple`)
- Photo captured but field stays NULL:
  - ensure you regenerated with the latest script (writes explicit `ExternalResource` storage config)
- Date/time widgets not shown:
  - ensure you regenerated with the latest script (writes `DateTime` editor config for temporal fields)
- Relevant logic not applied:
  - ensure you regenerated with the latest script (form layout switches to `tablayout` and writes visibility expressions)
  - verify `relevant` expressions use currently supported patterns
- Disconnected layer in QGIS:
  - ensure `.qgs` and `.gpkg` stay together in the same folder
