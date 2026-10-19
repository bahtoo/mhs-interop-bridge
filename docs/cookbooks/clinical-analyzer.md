# Cookbook: wrap an ASTM or HL7 analyzer

**You have:** an analyzer speaking ASTM E1381/E1394 or HL7 v2 (OUL^R22 results, OML^O33 orders).
**You get:** an MHS driver that normalises the vendor's dialect to LOINC, UCUM and canonical conditions, and accepts orders in the analyzer's own protocol.

## 1. Collect the dialect

From the vendor's host interface manual (respect its licence; never commit proprietary text), note:

* where the test code and specimen id sit (ASTM: component of R.3, field of O; HL7: OBX-3, SPM-2 or OBR-3);
* the unit sent for each test, and the unit the EHR expects;
* private result flags and what they mean physically (bubble, clot, short sample);
* QC materials, levels, target means and SDs in the vendor's unit.

## 2. Write the dialect profile

Copy `src/mhs_bridge/profiles/acme_chempro900.yaml` (ASTM) or `nordic_hema_h5.yaml` (HL7):

```yaml
device_id: chem-02
vendor: Your Vendor
model: Model X
device_type: clinical_chemistry_analyzer
protocol: astm
risk_class: specimen_processing
astm: {test_id_component: 4, specimen_field: 3, patient_field: 3}
tests:
  CREA:
    loinc: "2160-0"
    display: "Creatinine [Mass/volume] in Serum or Plasma"
    vendor_unit: "umol/L"
    unit: "mg/dL"
    factor: 0.011312
    decimals: 2
    ref_low: 0.7
    ref_high: 1.3
condition_flags: {"B": foam, "SS": short_sample}
qc:
  CREA:
    L1: {mean: 88.0, sd: 3.0}
tags:
  - "Results flagged B mean bubbles in the sample cup: a physical problem. Request a new specimen."
```

Rules of thumb:

* Every test needs a LOINC code. Unmapped codes are held, by design.
* A unit that is neither `vendor_unit` nor `unit` is held, never guessed.
* Every flag that implies a physical problem maps to a canonical condition, which arms the specimen quarantine.

## 3. Test it

Build a message with the simulator, or paste a captured and de-identified one, and assert the normalised values, units, interpretation and holds. See `tests/test_drivers.py` for the pattern.
