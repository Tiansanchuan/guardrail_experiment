"""Load explicit run settings without selecting models or resolving credentials."""

from pathlib import Path

import yaml

from va_experiment.models import RunConfig


def load_config(path: Path) -> RunConfig:
    config_path = Path(path).resolve()
    with config_path.open(encoding="utf-8") as stream:
        config = RunConfig.model_validate(yaml.safe_load(stream))
    output = config.output_dir
    if not output.is_absolute():
        output = config_path.parent / output
    return config.model_copy(update={"output_dir": output.resolve()})
