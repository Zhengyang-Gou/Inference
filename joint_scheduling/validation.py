"""Small, dependency-free helpers shared by all module configurations."""

import math
from dataclasses import fields
from typing import Mapping, get_type_hints


class ConfigError(ValueError):
    """An invalid experiment configuration."""


def require_mapping(value, path):
    if not isinstance(value, Mapping):
        raise ConfigError("{} must be an object".format(path))
    return dict(value)


def integer(value, path, minimum=0):
    if type(value) is not int or value < minimum:
        raise ConfigError("{} must be an integer >= {}".format(path, minimum))
    return value


def read_settings(cls, raw, section):
    """Read primitive dataclass fields, rejecting typos and invalid values."""
    raw = require_mapping(raw, section)
    public_fields = {f.name: f for f in fields(cls) if not f.metadata.get("internal")}
    unknown = set(raw) - set(public_fields)
    if unknown:
        raise ConfigError("{}: unknown fields: {}".format(section, ", ".join(sorted(unknown))))
    result = cls(**raw)
    hints = get_type_hints(cls)
    for name, field in public_fields.items():
        value = getattr(result, name)
        path = "{}.{}".format(section, name)
        kind = hints[name]
        if kind is int:
            if type(value) is not int:
                raise ConfigError("{} must be an integer".format(path))
        elif kind is float:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ConfigError("{} must be a finite number".format(path))
        elif kind is str:
            if not isinstance(value, str):
                raise ConfigError("{} must be a string".format(path))
        elif kind is bool:
            if type(value) is not bool:
                raise ConfigError("{} must be a boolean".format(path))
        else:
            raise TypeError("Unsupported configuration field: {}".format(path))
        if "min" in field.metadata and value < field.metadata["min"]:
            raise ConfigError("{} must be >= {}".format(path, field.metadata["min"]))
        if "gt" in field.metadata and value <= field.metadata["gt"]:
            raise ConfigError("{} must be > {}".format(path, field.metadata["gt"]))
        if "lt" in field.metadata and value >= field.metadata["lt"]:
            raise ConfigError("{} must be < {}".format(path, field.metadata["lt"]))
        if "choices" in field.metadata and value not in field.metadata["choices"]:
            raise ConfigError("{} must be one of {}".format(path, field.metadata["choices"]))
    return result
