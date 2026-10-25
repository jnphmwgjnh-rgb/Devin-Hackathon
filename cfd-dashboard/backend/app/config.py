from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_FUNCTION_OBJECTS = {
    "inlet_pressure": "inletPressure",
    "outlet_pressure": "outletPressure",
    "solid_temperature": "solidTemperature",
    "solid_temperature_avg": "solidTemperatureAvg",
}


@dataclass
class AppConfig:
    runs_dir: Path
    poll_interval: float = 2.0
    function_objects: dict[str, str] = field(
        default_factory=lambda: DEFAULT_FUNCTION_OBJECTS.copy()
    )
