# Cookbook: bring a SiLA 2 instrument to MHS

**You have:** an instrument with a SiLA 2 server and its Feature Definition files (`*.sila.xml`).
**You get:** an MHS driver whose manifest, limits and errors come from those files, plus the operating knowledge you add.

## 1. Preview what the Feature Definition gives you

```bash
mhs-bridge sila-preview path/to/YourFeature-v1_0.sila.xml
```

Commands become procedures, Parameters become typed parameters, `MinimalInclusive`, `MaximalInclusive`, `Unit` and `Set` become enforced limits, Defined Execution Errors become documented errors, and Properties become readable states. SiLA 2 parameters are mandatory, so MHS rejects a call that leaves one out.

Not covered yet: Structures, Custom data types, Metadata, and intermediate responses of Observable commands. The preview fails loudly if your FDL uses them.

## 2. Write the device profile

The profile holds what the FDL cannot say. Copy `src/mhs_bridge/profiles/liquid_handler_lh96.yaml`:

```yaml
device_id: lh-02
vendor: Your Vendor
model: Model X
device_type: liquid_handler
risk_class: research_automation          # PROPOSAL field; patient_connected makes a device read-only
driver: sila2
features: ["YourFeature-v1_0.sila.xml"]

error_hints:                             # one line per defined error, written by someone who runs the instrument
  FoamDetected: >-
    Physical failure, not a software bug. Move to a clean well, lower the flow rate, reduce mix cycles.

interlocks:                              # reusable hardware knowledge, see drivers/interlocks.py
  - {type: quarantine_on_error, error: FoamDetected, params: [Source, Target], detail_key: Well}
  - {type: capacity, target: Target, volume: Volume, capacity_ul: 300.0}
  - {type: on_deck, params: [Source, Target]}

routines: [SerialDilution]               # chained steps, each checked

tags:                                    # operating knowledge, shown in the reference file
  - "Viscous protein solutions foam above roughly 50 uL/s; start near 10 uL/s."
```

Ask yourself what a new colleague gets wrong on their first day with this instrument. That is what goes in `tags` and `error_hints`.

## 3. Connect the driver to your server

The driver needs a client with two methods: `call_command(feature, command, params) -> dict` and `get_property(feature, property) -> str`. A Defined Execution Error is raised as `SiLA2ExecutionError(identifier, message, details)`. Wrap your SiLA 2 client library's generated stub in that shape:

```python
from mhs_bridge.drivers.sila2 import SiLA2Driver
from mhs_bridge.protocols.sila2 import SiLA2ExecutionError, load_feature

class MyClient:
    def __init__(self, stub):
        self.stub = stub                  # your SiLA 2 client library's connection
    def call_command(self, feature, command, params):
        try:
            return getattr(getattr(self.stub, feature), command)(**params)
        except Exception as exc:          # map your library's execution error type here
            raise SiLA2ExecutionError(getattr(exc, "identifier", "Unknown"), str(exc)) from exc
    def get_property(self, feature, prop):
        return getattr(getattr(self.stub, feature), f"get_{prop}")()

driver = SiLA2Driver(profile, MyClient(stub), [load_feature("YourFeature-v1_0.sila.xml")],
                     is_online=lambda: stub_is_connected())
runtime.register(driver)                  # with the MHS SDK: register through its driver interface instead
```

## 4. Check it

```bash
mhs-bridge describe lh-02                 # if added to the built-in profiles
pytest -q
```

Then write one eval for the failure mode your tags describe. See [physical-safety-eval.md](physical-safety-eval.md).
