# Cookbook: wrap an instrument that has no API

Many instruments can only be automated through their vendor software's folders: write a command file, wait for an export, read a status file. The MHS preview describes the same situation for a plate reader. `drivers/filedrop.py` is the reusable pattern.

## The contract with the vendor software

| Path | Written by | Content |
|---|---|---|
| `inbox/cmd_NNNN.json` | driver | the command, for example `{"plate": "P1", "wavelength_nm": 562}` |
| `export/<plate>_NNNN.csv` | instrument | one row per well |
| `status.json` | instrument | `plates_present`, `saturation`, `last_error` |

If your instrument uses other names or formats, adapt `FileDropReaderDriver.write`, `read` and `precheck`. The shape stays the same.

## What the driver does

1. `precheck`: reads `status.json` and refuses `ReadPlate` if the plate is not present. That turns a confusing empty export into a clear `plate_location` block.
2. `write`: drops the command file, waits for a new export, and returns `NoExport` with the instrument's `last_error` if none appears.
3. `read("last_read:<plate>")`: parses the newest export.

In a lab the vendor software runs on its own. Pass a `process` callable that polls with a timeout instead of the simulator's tick.

## Profile

```yaml
device_id: reader-02
vendor: Your Vendor
model: Reader X
device_type: plate_reader
risk_class: research_automation
driver: filedrop
limits:
  wavelength_nm: {min: 340, max: 750}
tags:
  - "Absorbance at or above 3.0 is saturated. Compress the range rather than re-reading."
```
